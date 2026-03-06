import uuid
from pathlib import Path

from app import app, create_tables
from services.agent_release_service import (
    load_latest_release_manifest,
    publish_agent_release,
    rollback_latest_release,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _register_agent(client, shared_secret: str, agent_code: str) -> str:
    resp = client.post(
        "/api/agents/register",
        json={
            "agent_code": agent_code,
            "agent_name": f"{agent_code}-name",
            "project_codes": [],
            "default_admin_username": "admin",
        },
        headers={"X-Agent-Secret": shared_secret},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json() or {}
    assert data.get("success") is True
    return str(data.get("agent_token"))


def _build_fake_agent_source(base_dir: Path):
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "start_agent.py").write_text(
        "from runner_runtime import main\n\nif __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
    )
    (base_dir / "runner_runtime.py").write_text(
        "def main():\n    return 0\n",
        encoding="utf-8",
    )
    (base_dir / "config.py").write_text("class AgentSettings:\n    pass\n", encoding="utf-8")
    (base_dir / "requirements.txt").write_text("python-dotenv>=1.0.0\n", encoding="utf-8")
    (base_dir / ".env.example").write_text("PLATFORM_BASE_URL=http://127.0.0.1:8002\n", encoding="utf-8")


def test_publish_agent_release_creates_manifest(monkeypatch, tmp_path):
    release_root = tmp_path / "agent_releases"
    source_dir = tmp_path / "fake_agent"
    _build_fake_agent_source(source_dir)
    monkeypatch.setenv("AGENT_RELEASES_DIR", str(release_root))

    manifest = publish_agent_release(
        version="test-v1",
        source_dir=str(source_dir),
    )

    assert manifest.get("version") == "test-v1"
    assert manifest.get("package_size", 0) > 0
    assert (release_root / "latest.json").exists()
    assert (release_root / "releases" / "test-v1" / "manifest.json").exists()
    assert (release_root / "releases" / "test-v1" / manifest.get("package_file")).exists()


def test_publish_agent_release_skips_local_packaging_files(monkeypatch, tmp_path):
    release_root = tmp_path / "agent_releases"
    source_dir = tmp_path / "fake_agent"
    _build_fake_agent_source(source_dir)
    (source_dir / "build_zip.py").write_text("print('zip')\n", encoding="utf-8")
    (source_dir / "打包agent.bat").write_text("@echo off\r\necho build\r\n", encoding="utf-8")
    (source_dir / "agent.log").write_text("runtime log\n", encoding="utf-8")
    (source_dir / "venv" / "Scripts").mkdir(parents=True, exist_ok=True)
    (source_dir / "venv" / "Scripts" / "python.exe").write_text("fake", encoding="utf-8")
    monkeypatch.setenv("AGENT_RELEASES_DIR", str(release_root))

    manifest = publish_agent_release(
        version="test-skip-v1",
        source_dir=str(source_dir),
    )

    managed_files = set(manifest.get("managed_files") or [])
    assert "build_zip.py" not in managed_files
    assert "打包agent.bat" not in managed_files
    assert "agent.log" not in managed_files
    assert all(not path.startswith("venv/") for path in managed_files)


def test_agent_release_endpoints(monkeypatch, tmp_path):
    shared_secret = _uid("secret")
    agent_code = _uid("agent")
    release_root = tmp_path / "agent_releases"
    source_dir = tmp_path / "fake_agent"

    _build_fake_agent_source(source_dir)
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)
    monkeypatch.setenv("AGENT_RELEASES_DIR", str(release_root))

    manifest = publish_agent_release(
        version="test-v2",
        source_dir=str(source_dir),
    )

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            agent_token = _register_agent(client, shared_secret, agent_code)

            latest_resp = client.post(
                "/api/agents/releases/latest",
                json={
                    "agent_code": agent_code,
                    "agent_token": agent_token,
                    "current_version": "old-version",
                },
                headers={"X-Agent-Secret": shared_secret},
            )
            assert latest_resp.status_code == 200, latest_resp.get_data(as_text=True)
            latest_data = latest_resp.get_json() or {}
            assert latest_data.get("success") is True
            assert latest_data.get("has_update") is True
            assert latest_data.get("latest_version") == "test-v2"
            release = latest_data.get("release") or {}
            assert release.get("package_sha256") == manifest.get("package_sha256")
            download_path = release.get("download_path")
            assert download_path

            download_resp = client.get(
                download_path,
                headers={
                    "X-Agent-Secret": shared_secret,
                    "X-Agent-Code": agent_code,
                    "X-Agent-Token": agent_token,
                },
            )
            assert download_resp.status_code == 200, download_resp.get_data(as_text=True)
            assert len(download_resp.data or b"") > 0

            up_to_date_resp = client.post(
                "/api/agents/releases/latest",
                json={
                    "agent_code": agent_code,
                    "agent_token": agent_token,
                    "current_version": "test-v2",
                },
                headers={"X-Agent-Secret": shared_secret},
            )
            assert up_to_date_resp.status_code == 200, up_to_date_resp.get_data(as_text=True)
            up_to_date_data = up_to_date_resp.get_json() or {}
            assert up_to_date_data.get("has_update") is False
            assert up_to_date_data.get("status") == "up_to_date"


def test_rollback_latest_release_to_previous(monkeypatch, tmp_path):
    release_root = tmp_path / "agent_releases"
    source_dir = tmp_path / "fake_agent"
    _build_fake_agent_source(source_dir)
    monkeypatch.setenv("AGENT_RELEASES_DIR", str(release_root))

    publish_agent_release(version="rollback-v1", source_dir=str(source_dir))
    publish_agent_release(version="rollback-v2", source_dir=str(source_dir))
    latest_before = load_latest_release_manifest() or {}
    assert latest_before.get("version") == "rollback-v2"

    result = rollback_latest_release()
    assert result.get("changed") is True
    assert result.get("from_version") == "rollback-v2"
    assert result.get("to_version") == "rollback-v1"

    latest_after = load_latest_release_manifest() or {}
    assert latest_after.get("version") == "rollback-v1"


def test_admin_release_rollback_endpoint(monkeypatch, tmp_path):
    shared_secret = _uid("secret")
    admin_token = _uid("admin-token")
    agent_code = _uid("agent")
    release_root = tmp_path / "agent_releases"
    source_dir = tmp_path / "fake_agent"

    _build_fake_agent_source(source_dir)
    monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)
    monkeypatch.setenv("AGENT_RELEASES_DIR", str(release_root))

    publish_agent_release(version="ep-rollback-v1", source_dir=str(source_dir))
    publish_agent_release(version="ep-rollback-v2", source_dir=str(source_dir))

    with app.app_context():
        create_tables()
        with app.test_client() as client:
            _register_agent(client, shared_secret, agent_code)

            list_resp = client.get(
                "/api/agents/releases/admin/list",
                headers={"X-Admin-Token": admin_token},
            )
            assert list_resp.status_code == 200, list_resp.get_data(as_text=True)
            list_data = list_resp.get_json() or {}
            assert list_data.get("success") is True
            assert list_data.get("latest_version") == "ep-rollback-v2"
            assert int(list_data.get("count") or 0) >= 2

            rollback_resp = client.post(
                "/api/agents/releases/admin/rollback",
                json={"steps": 1},
                headers={"X-Admin-Token": admin_token},
            )
            assert rollback_resp.status_code == 200, rollback_resp.get_data(as_text=True)
            rollback_data = rollback_resp.get_json() or {}
            assert rollback_data.get("success") is True
            assert rollback_data.get("changed") is True
            assert rollback_data.get("from_version") == "ep-rollback-v2"
            assert rollback_data.get("to_version") == "ep-rollback-v1"

            latest_after = load_latest_release_manifest() or {}
            assert latest_after.get("version") == "ep-rollback-v1"
