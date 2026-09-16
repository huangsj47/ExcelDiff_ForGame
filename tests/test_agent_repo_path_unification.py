# -*- coding: utf-8 -*-
"""Agent 同步写入的仓库目录，必须与平台 Diff 读取的仓库目录**逐字符相同**。

## 缺陷（修复前）

两侧对「工作副本根目录」的解析规则不同：

* Agent `agent/handlers/auto_sync.py`：`os.path.abspath(settings.repos_base_dir)`
  —— 相对**当前工作目录**，默认 `agent_repos`；
* 平台 `services/git_service.py::GitService._get_local_path()`：调用
  `build_repository_local_path()` 不传 base_dir —— 落到默认 `repos`，相对**仓库根**。

Agent 节点上的 Diff 是平台代码在 **Agent 进程内**执行的（`agent/executor.py` 把
平台源码根加进 sys.path 后 `import app`，再调
`services.task_worker_service.execute_task_inline_for_agent`），读的就是平台算出来的
那个目录。于是同一个 repository 有两个落点。

**实测（修复前，`python -c` 复现，CWD 取 `start_agent.bat` 的 `cd /d "%~dp0"`）**：

    CWD = <仓库根>\\agent
      auto_sync 写入 : <仓库根>\\agent\\agent_repos\\G119_qz_pub_7
      diff   读取    : <仓库根>\\repos\\G119_qz_pub_7
      相同? False

    换 CWD（服务管理器/计划任务/手工指定脚本路径都可能不同）：
      cwd=<仓库根>   : <仓库根>\\agent_repos\\G119_qz_pub_7
      cwd=C:\\Windows\\Temp : C:\\Windows\\Temp\\agent_repos\\G119_qz_pub_7

后果：auto_sync 报 completed，紧接着的 Diff 判定「未克隆」并把仓库重新 clone 一遍；
换个启动方式还会在新位置再建一个 `agent_repos/`，旧目录成为孤儿。

## 本文件断言什么

1. 默认配置下，`execute_auto_sync` 写入的目录 == 平台 `build_repository_local_path()`
   解析出的目录（== `<仓库根>/repos`），且与 CWD 无关；
2. `AGENT_REPOS_BASE_DIR` 作为显式覆盖**两侧都生效**（平台侧读同一个变量）；
3. 绝对路径原样生效；
4. 平台默认值仍是 `repos`（环境变量没设时平台行为不变）；
5. agent 侧的锚点与 `utils.runtime_paths.repo_root()` 是同一个目录（防止两边
   规则各自漂移）；
6. 历史兼容不被破坏：`repo_{id}` 旧目录的回退/迁移仍然工作，`_temp_cache` 也锚定。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent import (
    local_temp_cache,  # noqa: E402
    repo_paths,  # noqa: E402
)
from agent.handlers import auto_sync as auto_sync_handler  # noqa: E402
from utils.path_security import build_repository_local_path  # noqa: E402
from utils.runtime_paths import default_repos_base_dir, repo_root  # noqa: E402

AGENT_DIR = os.path.join(PROJECT_ROOT, "agent")

# 唯一 id：避免与开发机上真实存在的工作副本（`repos/repo_<id>` 旧目录或同名命名
# 目录）撞上，从而把「路径解析」的断言污染成「磁盘上恰好有什么」。
_REPOSITORY_ID = 987654321
_PROJECT_CODE = "G119"
_REPOSITORY_NAME = "qz_pub"


def _expected_named_dir(base_dir: str) -> str:
    return os.path.join(base_dir, f"{_PROJECT_CODE}_{_REPOSITORY_NAME}_{_REPOSITORY_ID}")


def _run_auto_sync(monkeypatch, settings) -> str:
    """跑一次真实的 execute_auto_sync（只把 git/svn 调用换成桩），返回写入目录。"""
    captured = {}

    def _fake_sync_repo(local_repo_dir, remote_url, branch):
        captured["local_repo_dir"] = local_repo_dir

    monkeypatch.setattr(auto_sync_handler, "_sync_repo", _fake_sync_repo)
    monkeypatch.setattr(auto_sync_handler, "_collect_commits", lambda **kwargs: [])

    task = {
        "payload": {
            "repository_id": _REPOSITORY_ID,
            "repository": {
                "repository_id": _REPOSITORY_ID,
                "type": "git",
                "url": "https://example.com/repo.git",
                "branch": "main",
                "project_code": _PROJECT_CODE,
                "repository_name": _REPOSITORY_NAME,
            },
        }
    }

    status, _summary, error, _payload = auto_sync_handler.execute_auto_sync(task, settings)
    assert status == "completed"
    assert error is None
    assert captured.get("local_repo_dir"), "auto_sync 没有把本地目录交给同步函数"
    return captured["local_repo_dir"]


def _settings_from_env(monkeypatch):
    """按 `load_settings()` 的规则造一份最小 settings（只带本测试关心的字段）。"""
    return SimpleNamespace(
        repos_base_dir=repo_paths.resolve_repos_base_dir(
            os.environ.get("AGENT_REPOS_BASE_DIR") or ""
        )
    )


class TestWriteDirEqualsReadDir:
    def test_default_config_write_dir_equals_diff_read_dir(self, monkeypatch):
        """默认配置（CWD = agent 目录，即 start_agent.bat 的行为）。

        修复前实测：写入 `<仓库根>\\agent\\agent_repos\\G119_qz_pub_987654321`，
        读取 `<仓库根>\\repos\\G119_qz_pub_987654321` —— 本断言就是那次失败的固化。
        """
        monkeypatch.delenv("AGENT_REPOS_BASE_DIR", raising=False)
        monkeypatch.chdir(AGENT_DIR)

        settings = SimpleNamespace(repos_base_dir=default_repos_base_dir({}))
        written = _run_auto_sync(monkeypatch, settings)
        # 平台 Diff 读的目录（GitService._get_local_path 就是不传 base_dir）
        read = build_repository_local_path(_PROJECT_CODE, _REPOSITORY_NAME, _REPOSITORY_ID)

        assert written == read, (
            "auto_sync 写入的目录与平台 Diff 读取的目录不是同一个：\n"
            f"  写入 = {written}\n  读取 = {read}"
        )
        assert os.path.dirname(written) == os.path.join(repo_root(), "repos")

    def test_write_dir_is_stable_when_cwd_changes(self, monkeypatch, tmp_path):
        """修复前：换个 CWD 就换了一个 `agent_repos/`（旧工作副本成为孤儿）。"""
        monkeypatch.delenv("AGENT_REPOS_BASE_DIR", raising=False)

        monkeypatch.chdir(AGENT_DIR)
        first = _run_auto_sync(monkeypatch, SimpleNamespace(repos_base_dir="repos"))

        monkeypatch.chdir(tmp_path)
        second = _run_auto_sync(monkeypatch, SimpleNamespace(repos_base_dir="repos"))

        assert first == second, (
            "CWD 变化导致工作副本目录漂移：\n"
            f"  cwd={AGENT_DIR} -> {first}\n  cwd={tmp_path} -> {second}"
        )
        assert second == build_repository_local_path(
            _PROJECT_CODE, _REPOSITORY_NAME, _REPOSITORY_ID
        )


class TestExplicitOverrideAppliesToBothSides:
    def test_absolute_env_override_moves_both_sides(self, monkeypatch, tmp_path):
        """`AGENT_REPOS_BASE_DIR` 是绝对路径时：两侧都到该目录，且原样使用。"""
        override = os.path.join(str(tmp_path), "node_repos")
        monkeypatch.setenv("AGENT_REPOS_BASE_DIR", override)
        monkeypatch.chdir(AGENT_DIR)

        written = _run_auto_sync(monkeypatch, _settings_from_env(monkeypatch))
        read = build_repository_local_path(_PROJECT_CODE, _REPOSITORY_NAME, _REPOSITORY_ID)

        assert written == read == _expected_named_dir(override)

    def test_relative_env_override_is_anchored_and_not_cwd_dependent(self, monkeypatch, tmp_path):
        """显式覆盖写成相对路径时也必须锚定，不能随 CWD 漂移。

        这里把锚点替换成 tmp_path（模拟「agent 安装根 / 平台仓库根」），否则断言会
        落到真实仓库里 —— 解析规则本身与真实部署完全一致。
        """
        monkeypatch.setattr(repo_paths, "runtime_anchor", lambda: str(tmp_path))
        monkeypatch.chdir(tmp_path)
        assert repo_paths.resolve_repos_base_dir("node_repos") == os.path.join(
            str(tmp_path), "node_repos"
        )

        monkeypatch.chdir(AGENT_DIR)
        assert repo_paths.resolve_repos_base_dir("node_repos") == os.path.join(
            str(tmp_path), "node_repos"
        ), "相对覆盖的解析结果随 CWD 变化了"

    def test_legacy_agent_repos_value_still_agrees_on_both_sides(self, monkeypatch):
        """升级路径：老节点的 `.env` 里写死 `AGENT_REPOS_BASE_DIR=agent_repos`。

        升级后目录会从 `<安装根>/agent/agent_repos`（相对 CWD 时代的落点）变成
        `<安装根>/agent_repos`，但两侧仍然一致 —— 这是「不需要重新配置、只需重新
        同步一次」的依据。
        """
        monkeypatch.setenv("AGENT_REPOS_BASE_DIR", "agent_repos")

        agent_dir = repo_paths.resolve_repos_base_dir(os.environ["AGENT_REPOS_BASE_DIR"])
        platform_dir = os.path.dirname(
            build_repository_local_path(_PROJECT_CODE, _REPOSITORY_NAME, _REPOSITORY_ID)
        )

        assert agent_dir == platform_dir == os.path.join(repo_root(), "agent_repos")

    def test_absolute_path_is_kept_verbatim(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_REPOS_BASE_DIR", os.path.join(str(tmp_path), "ignored"))
        explicit = os.path.join(str(tmp_path), "explicit")

        assert repo_paths.resolve_repos_base_dir(explicit) == explicit
        assert build_repository_local_path(
            "proj", "repo", 7, base_dir=explicit
        ) == os.path.join(explicit, "proj_repo_7")


class TestPlatformDefaultIsUnchanged:
    def test_default_is_still_repos(self, monkeypatch):
        """环境变量没设时平台必须还是 `repos` —— 否则现有部署会全部重新 clone。"""
        monkeypatch.delenv("AGENT_REPOS_BASE_DIR", raising=False)

        assert default_repos_base_dir({}) == "repos"
        assert build_repository_local_path("proj", "repo", 7) == os.path.join(
            repo_root(), "repos", "proj_repo_7"
        )

    def test_blank_env_value_falls_back_to_repos(self, monkeypatch):
        monkeypatch.setenv("AGENT_REPOS_BASE_DIR", "   ")
        assert default_repos_base_dir() == "repos"
        assert build_repository_local_path("proj", "repo", 7) == os.path.join(
            repo_root(), "repos", "proj_repo_7"
        )

    def test_explicit_base_dir_argument_beats_env_override(self, monkeypatch, tmp_path):
        """显式传 base_dir 的调用方（Agent 侧、测试）不受环境变量影响。"""
        monkeypatch.setenv("AGENT_REPOS_BASE_DIR", os.path.join(str(tmp_path), "from_env"))
        explicit = os.path.join(str(tmp_path), "explicit")

        assert build_repository_local_path(
            "proj", "repo", 7, base_dir=explicit
        ) == os.path.join(explicit, "proj_repo_7")


class TestAnchorsAgree:
    def test_agent_anchor_is_the_platform_repo_root(self):
        """Agent 侧锚点必须与 `utils.runtime_paths.repo_root()` 是同一个目录。

        平台的 `resolve_runtime_path()` 锚 `utils/` 的父目录；agent 侧锚 `__file__`
        的上一级。两者是**两份实现**，这条断言就是防它们各自漂移的守卫。
        """
        assert repo_paths.platform_root() == repo_root()
        assert repo_paths.runtime_anchor() == repo_root()

    def test_agent_default_resolution_equals_platform_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_REPOS_BASE_DIR", raising=False)

        got = repo_paths.resolve_repos_base_dir("")
        expected = os.path.dirname(
            build_repository_local_path(_PROJECT_CODE, _REPOSITORY_NAME, _REPOSITORY_ID)
        )
        assert got == expected == os.path.join(repo_root(), "repos")

    def test_standalone_agent_deployment_anchors_to_agent_dir(self, monkeypatch, tmp_path):
        """`agent/` 单独打包部署（agent/build_zip.py）时的锚点。

        没有平台源码时不存在必须对齐的另一侧，但**仍然不能依赖 CWD**。
        """
        fake_agent_dir = os.path.join(str(tmp_path), "agent")
        os.makedirs(fake_agent_dir, exist_ok=True)
        monkeypatch.setattr(repo_paths, "AGENT_DIR", fake_agent_dir)
        monkeypatch.setattr(repo_paths, "platform_root", lambda: None)

        assert repo_paths.resolve_repos_base_dir("") == os.path.join(fake_agent_dir, "repos")
        assert repo_paths.resolve_repos_base_dir("node_repos") == os.path.join(
            fake_agent_dir, "node_repos"
        )


class TestHistoricalCompatibilityIsKept:
    def test_legacy_repo_id_dir_is_still_migrated(self, monkeypatch, tmp_path):
        """`repo_{id}` 旧目录的迁移逻辑不能因为锚点变化而失效。"""
        monkeypatch.setattr(repo_paths, "runtime_anchor", lambda: str(tmp_path))
        legacy_dir = os.path.join(str(tmp_path), "node_repos", f"repo_{_REPOSITORY_ID}")
        os.makedirs(legacy_dir, exist_ok=True)

        written = _run_auto_sync(monkeypatch, SimpleNamespace(repos_base_dir="node_repos"))

        assert written == _expected_named_dir(os.path.join(str(tmp_path), "node_repos"))
        assert os.path.isdir(written)
        assert not os.path.isdir(legacy_dir), "旧目录没有被迁移到命名目录"

    def test_temp_cache_root_follows_the_same_anchor(self, monkeypatch, tmp_path):
        monkeypatch.setattr(repo_paths, "runtime_anchor", lambda: str(tmp_path))
        settings = SimpleNamespace(repos_base_dir="node_repos")

        monkeypatch.chdir(tmp_path)
        first = local_temp_cache._cache_root(settings)
        monkeypatch.chdir(AGENT_DIR)
        second = local_temp_cache._cache_root(settings)

        assert first == second == os.path.join(str(tmp_path), "node_repos", "_temp_cache")


class TestModuleLoadRobustness:
    def test_loaded_by_file_path_still_resolves_the_anchored_dir(self, monkeypatch, tmp_path):
        """按**文件路径**直接加载本模块时（没有包上下文、`agent/` 也不在 sys.path），
        锚点解析仍然生效。

        `tests/test_credential_redaction.py` 的脱敏探针就是这么加载
        `agent/handlers/auto_sync.py` 的；修复过程中它一度 ImportError
        （`from .. import repo_paths` 无包上下文、`import repo_paths` 找不到模块），
        所以 `_load_sibling_repo_paths()` 这条按路径加载的兜底必须留着。
        """
        import importlib.util

        path = os.path.join(PROJECT_ROOT, "agent", "handlers", "auto_sync.py")
        spec = importlib.util.spec_from_file_location("agent_auto_sync_pathprobe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        monkeypatch.setattr(module.repo_paths, "runtime_anchor", lambda: str(tmp_path))
        monkeypatch.chdir(AGENT_DIR)

        assert module.repo_paths.platform_root() == repo_root()
        assert module.repo_paths.resolve_repos_base_dir("node_repos") == os.path.join(
            str(tmp_path), "node_repos"
        )
        assert module._build_repo_local_path_fallback(
            _PROJECT_CODE, _REPOSITORY_NAME, _REPOSITORY_ID, "node_repos"
        ) == _expected_named_dir(os.path.join(str(tmp_path), "node_repos"))


class TestSettingsResolveOnce:
    def test_load_settings_returns_an_absolute_anchored_dir(self, monkeypatch):
        """`load_settings()` 交出的必须已是绝对路径：消费方（metrics、缓存、
        auto_sync）不该再各自解析一遍、各自受 CWD 影响。"""
        from agent.config import load_settings

        monkeypatch.setenv("AGENT_HOST", "10.9.9.9")
        settings = load_settings()

        assert os.path.isabs(settings.repos_base_dir)
        assert settings.repos_base_dir == repo_paths.resolve_repos_base_dir(
            settings.repos_base_dir
        ), "解析不幂等"

    def test_fallback_impl_matches_platform_for_the_same_base(self, monkeypatch, tmp_path):
        """`agent/` 单独打包部署（没有平台 utils）走的是自带实现，它必须与平台
        算出同一个路径 —— 否则「独立部署正常、装上平台源码后目录就变了」。"""
        monkeypatch.setattr(repo_paths, "runtime_anchor", lambda: str(tmp_path))
        monkeypatch.setattr(auto_sync_handler, "build_repository_local_path", None)

        written = _run_auto_sync(monkeypatch, SimpleNamespace(repos_base_dir="node_repos"))

        base_dir = os.path.join(str(tmp_path), "node_repos")
        assert written == build_repository_local_path(
            _PROJECT_CODE, _REPOSITORY_NAME, _REPOSITORY_ID, base_dir=base_dir
        )
