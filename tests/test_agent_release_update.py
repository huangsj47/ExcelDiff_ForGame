import json
import os
import uuid
from pathlib import Path

import pytest

from app import app, create_tables
from services.agent_release_service import (
    detect_git_commit_id,
    get_release_package_path,
    load_latest_release_manifest,
    load_release_manifest,
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
            try:
                assert download_resp.status_code == 200, download_resp.get_data(as_text=True)
                assert len(download_resp.data or b"") > 0
            finally:
                download_resp.close()

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


def test_detect_git_commit_id_returns_empty_on_subprocess_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "services.agent_release_service.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git missing")),
    )
    assert detect_git_commit_id(str(tmp_path)) == ""


def test_load_latest_release_manifest_returns_none_for_invalid_json(monkeypatch, tmp_path):
    release_root = tmp_path / "agent_releases"
    latest_path = release_root / "latest.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("AGENT_RELEASES_DIR", str(release_root))

    assert load_latest_release_manifest() is None


def test_load_release_manifest_returns_none_for_invalid_json(monkeypatch, tmp_path):
    release_root = tmp_path / "agent_releases"
    manifest_path = release_root / "releases" / "release-v1" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("AGENT_RELEASES_DIR", str(release_root))

    assert load_release_manifest("release-v1") is None


def test_get_release_package_path_returns_none_for_invalid_version():
    assert get_release_package_path("../invalid") is None


# ---------------------------------------------------------------------------
# 版本号 = **单一安全路径段**
#
# 版本号会直接进 `os.path.join(releases, version)`，而 `publish_agent_release`
# 里紧跟着就是 `os.makedirs(...)`，`force=True` 时还有
# `shutil.rmtree(_release_dir(version))`。原来的正则
# `^[A-Za-z0-9._-]{1,64}$` **放行 `.` 与 `..`**（它们的字符全在类里）：
#
#   * `publish_agent_release(version="..")` 把 manifest.json / latest.json /
#     zip 写到 `releases/` **之外**；
#   * 带 `force=True` 时 `rmtree(releases/..)` 删的是发布根目录的**父目录**。
#
# 而 Agent 侧（`agent/self_update.py::_is_safe_release_version`）一直是
# 「首字符必须是字母数字」+ 显式挡 `.`/`..` —— 平台是这套协议的**服务端**，
# 判据不该比自己的客户端还松。
# ---------------------------------------------------------------------------

def test_every_version_the_platform_publishes_is_one_the_agent_accepts():
    """不变式是**单向蕴含**，不是「逐字相同」：

        平台放行的版本号 ⊆ Agent 放行的版本号

    平台是**生产方**（`publish_agent_release` 造包），Agent 是**消费方**
    （`_is_safe_release_version` 决定装不装）。平台比 Agent 严是安全的
    （只是少发几个包）；反过来平台能发出 Agent 不认的包，就是「发布成功但节点装不上」。
    两者的长度上限本来就不同（平台 64 / Agent 128），所以不写成相等。
    """
    from agent.self_update import _is_safe_release_version
    from services.agent_release_service import _safe_version

    cases = [
        ".", "..", "...", "..x", ".hidden", "-x", "_x", "a..b",
        "1.2.3", "v1.0", "20260919-abcdef1", "a", "0",
        "x" * 64, "x" * 65, "x" * 129, "", "   ", "../escape", "a/b", "a\b",
    ]
    for value in cases:
        try:
            _safe_version(value)
            platform_ok = True
        except ValueError:
            platform_ok = False
        agent_ok = _is_safe_release_version(value)
        assert not (platform_ok and not agent_ok), (
            f"平台放行了 {value!r}，而 Agent 侧会拒绝它 —— 发得出去但装不上"
        )


@pytest.mark.parametrize("bad", [".", "..", "..x", ".hidden", "-x", "_x", "../escape", "a/b"])
def test_the_dangerous_versions_are_refused_by_both_sides(bad):
    """危险形态两侧都必须拒 —— 这条挡住的是「某一侧被放宽」。"""
    from agent.self_update import _is_safe_release_version
    from services.agent_release_service import _safe_version

    with pytest.raises(ValueError):
        _safe_version(bad)
    assert _is_safe_release_version(bad) is False


@pytest.mark.parametrize("bad", [".", "..", "..x", ".hidden", "-x", "_x", "../escape", "a/b"])
def test_a_version_that_is_not_a_plain_segment_is_refused(bad):
    """版本号必须是**首字符为字母数字**的一段。

    `..x` / `.hidden` / `-x` / `_x` 在**路径层面**是无害的（当成目录名就是
    `releases/..x`），它们在这里被拒是因为**版本号的语义**：一个连首字符都不是
    字母数字的串不是版本号。所以这条只钉 `_safe_version`。
    """
    from services.agent_release_service import _safe_version

    with pytest.raises(ValueError):
        _safe_version(bad)


@pytest.mark.parametrize(
    "escaping",
    [".", "..", "../escape", "../../escape", "a/../../escape"],
    ids=["dot", "dotdot", "up", "up_up", "nested_up"],
)
def test_a_value_that_escapes_the_releases_dir_cannot_even_be_mapped_to_a_path(escaping):
    """**路径层面**真正越界的那些：连 `_release_dir` 都不许算出路径来。

    这是第二道防线 —— 独立于版本号正则，挡的是「将来正则被放宽」。

    注意 `a/b`、`a` **不在**这张表里：它们算出来是 `releases/a/b`，仍然在
    releases 之内（只是多了一层目录），路径这一层没有理由拒 ——
    拒它们的是**版本号语义**（首字符必须是字母数字、不许含分隔符），
    那条在 `test_a_version_that_is_not_a_plain_segment_is_refused` 里。
    把两种「不合格」混在一张表里，会让任何一侧漏改都看不出来。
    """
    from services.agent_release_service import _release_dir

    with pytest.raises(ValueError):
        _release_dir(escaping)


def test_an_absolute_path_cannot_escape_either(tmp_path):
    """绝对路径同样不许：`os.path.join(base, "C:/x")` 会**整个丢掉 base**。"""
    from services.agent_release_service import _release_dir

    with pytest.raises(ValueError):
        _release_dir(str(tmp_path / "elsewhere"))


def test_a_normal_version_still_works():
    """**不能靠「一律拒绝」通过** —— 正常的版本号必须照旧。"""
    from services.agent_release_service import _release_dir, _safe_version

    assert _safe_version("1.2.3") == "1.2.3"
    assert _release_dir("1.2.3").endswith(os.path.join("releases", "1.2.3"))


def test_publishing_under_a_dot_version_cannot_escape_the_releases_dir(monkeypatch, tmp_path):
    """端到端：拿 `..` 当版本号，`publish_agent_release` 必须直接拒绝，
    而不是把文件写到 `releases/` 外面去。"""
    monkeypatch.setenv("AGENT_RELEASES_DIR", str(tmp_path))
    releases_root = tmp_path / "releases"
    releases_root.mkdir(parents=True)
    source = tmp_path / "agent_src"
    source.mkdir()
    (source / "runner.py").write_text("print('x')\n", encoding="utf-8")

    for bad in (".", ".."):
        with pytest.raises(ValueError):
            publish_agent_release(source_dir=str(source), version=bad)

    # 发布根目录的**父目录**里不许多出任何东西（原先 manifest/latest/zip 会落在这儿）
    stray = sorted(p.name for p in tmp_path.iterdir())
    assert stray == ["agent_src", "releases"], stray


def test_get_release_package_path_refuses_a_manifest_that_points_outside(monkeypatch, tmp_path):
    """`package_file` 是**从清单里读进来的数据**，不是这里算出来的。

    换过一份清单就可能带出 `../../x.zip` 或绝对路径。拼完之后必须再走一次包含性
    校验 —— 这条路的调用方是 `send_file`，拿到什么就会发什么。
    """
    monkeypatch.setenv("AGENT_RELEASES_DIR", str(tmp_path))
    release_dir = tmp_path / "releases" / "v9.9"
    release_dir.mkdir(parents=True)
    outside = tmp_path / "secret.zip"
    outside.write_bytes(b"PK")

    (release_dir / "manifest.json").write_text(
        json.dumps({"version": "v9.9", "package_file": "../../secret.zip"}),
        encoding="utf-8",
    )
    assert get_release_package_path("v9.9") is None

    (release_dir / "manifest.json").write_text(
        json.dumps({"version": "v9.9", "package_file": str(outside)}),
        encoding="utf-8",
    )
    assert get_release_package_path("v9.9") is None

    # 正常清单照旧能取到
    (release_dir / "agent_release_v9.9.zip").write_bytes(b"PK")
    (release_dir / "manifest.json").write_text(
        json.dumps({"version": "v9.9", "package_file": "agent_release_v9.9.zip"}),
        encoding="utf-8",
    )
    resolved = get_release_package_path("v9.9")
    assert resolved is not None
    assert os.path.realpath(resolved).startswith(os.path.realpath(str(release_dir)))


def test_a_symlinked_release_dir_does_not_break_the_release_list(tmp_path, monkeypatch):
    """`releases/` 里出现一个指向外部的软链接时，列表要能照常出，而不是 500。

    `list_release_manifests` 是拿 `os.listdir(releases/)` 的目录名逐个去
    `load_release_manifest` 的，而那条路径里现在有包含性校验（realpath + commonpath）。
    软链接 realpath 之后会落到 `releases/` 之外 —— 那一下必须被当作「这个版本读不了」，
    因为整张发布列表 500 会让**所有**版本都看不见。
    """
    import os as _os

    from services import agent_release_service as release_service

    root = tmp_path / "agent_releases"
    releases = root / "releases"
    (releases / "v-good").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text(
        json.dumps({"version": "v-evil"}), encoding="utf-8"
    )
    link = releases / "v-evil"
    try:
        _os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("这个环境建不了目录软链接（Windows 需要开发者模式或管理员）")

    monkeypatch.setenv("AGENT_RELEASES_DIR", str(root))

    assert release_service.load_release_manifest("v-evil") is None

    listed = release_service.list_release_manifests()
    assert [item["version"] for item in listed] == [], (
        "越界的目录不该被当成一个版本读出来"
    )


def test_a_manifest_path_that_cannot_be_resolved_reads_as_missing(monkeypatch):
    """路径解析抛 `ValueError` 时，`load_release_manifest` 要返回 `None`，不许往上抛。

    上面那条软链接用例是**真实触发**（在能建软链接的环境里跑，CI 的 ubuntu 就能），
    这条是同一个契约的**确定性**版本：本地 Windows 没有建软链接的权限时，
    那条会 skip，这条照样盯着「抛异常 = 整张发布列表 500」这个后果。
    """
    from services import agent_release_service as release_service

    def _boom(_version):
        raise ValueError("path escapes base directory")

    monkeypatch.setattr(release_service, "_release_manifest_path", _boom)

    assert release_service.load_release_manifest("v1.0") is None
