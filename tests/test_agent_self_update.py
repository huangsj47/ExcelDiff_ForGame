import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

from agent import self_update


def _build_release_zip(zip_path: Path):
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        zf.writestr("start_agent.py", "from runner_runtime import main\nmain()\n")
        zf.writestr("runner_runtime.py", "def main():\n    return 0\n")
        zf.writestr("config.py", "class AgentSettings:\n    pass\n")
        zf.writestr("requirements.txt", "python-dotenv>=1.0.0\n")
        zf.writestr("new_module.py", "VALUE = 1\n")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def test_check_and_apply_update_no_update(monkeypatch, tmp_path):
    agent_root = tmp_path / "agent_runtime"
    agent_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(self_update, "_agent_root", lambda: str(agent_root))

    def _fake_post_json(url, payload, headers=None, timeout=10):
        return 200, {"success": True, "has_update": False}

    monkeypatch.setattr(self_update, "post_json", _fake_post_json)

    settings = SimpleNamespace(
        platform_base_url="http://127.0.0.1:8002",
        agent_code="agent-a",
        auto_update_request_timeout_seconds=15,
    )
    updated, message = self_update.check_and_apply_update(
        settings=settings,
        common_headers={"X-Agent-Secret": "secret"},
        agent_token="token-a",
        log_func=lambda msg: None,
    )
    assert updated is False
    assert message == "no update"
    assert not (agent_root / ".agent_release_state.json").exists()


def test_check_and_apply_update_success(monkeypatch, tmp_path):
    agent_root = tmp_path / "agent_runtime"
    agent_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(self_update, "_agent_root", lambda: str(agent_root))

    old_state = {
        "version": "old-v1",
        "managed_files": ["stale.py"],
    }
    (agent_root / ".agent_release_state.json").write_text(
        json.dumps(old_state, ensure_ascii=False),
        encoding="utf-8",
    )
    (agent_root / "stale.py").write_text("old=1\n", encoding="utf-8")

    release_zip = tmp_path / "release_source" / "release.zip"
    _build_release_zip(release_zip)
    release_sha256 = _sha256_file(release_zip)
    release_size = release_zip.stat().st_size

    def _fake_post_json(url, payload, headers=None, timeout=10):
        return (
            200,
            {
                "success": True,
                "has_update": True,
                "release": {
                    "version": "new-v2",
                    "commit_id": "abc12345",
                    "package_sha256": release_sha256,
                    "package_size": release_size,
                    "download_path": "/api/agents/releases/new-v2/package",
                },
            },
        )

    def _fake_download_file(url, target_path, headers=None, timeout=30):
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        Path(target_path).write_bytes(release_zip.read_bytes())
        return 200, {"success": True}

    monkeypatch.setattr(self_update, "post_json", _fake_post_json)
    monkeypatch.setattr(self_update, "download_file", _fake_download_file)
    monkeypatch.setattr(self_update, "_install_requirements_if_needed", lambda settings, extracted_root: None)

    logs = []
    settings = SimpleNamespace(
        platform_base_url="http://127.0.0.1:8002",
        agent_code="agent-a",
        auto_update_request_timeout_seconds=15,
        auto_update_download_timeout_seconds=120,
        auto_update_install_deps=False,
    )

    updated, message = self_update.check_and_apply_update(
        settings=settings,
        common_headers={"X-Agent-Secret": "secret"},
        agent_token="token-a",
        log_func=lambda msg: logs.append(msg),
    )

    assert updated is True
    assert message == "updated to new-v2"
    assert (agent_root / "start_agent.py").exists()
    assert (agent_root / "runner_runtime.py").exists()
    assert (agent_root / "new_module.py").exists()
    assert not (agent_root / "stale.py").exists()

    state = json.loads((agent_root / ".agent_release_state.json").read_text(encoding="utf-8"))
    assert state.get("version") == "new-v2"
    managed_files = set(state.get("managed_files") or [])
    assert "new_module.py" in managed_files
    assert "start_agent.py" in managed_files
    assert any("old-v1 -> new-v2" in line for line in logs)
