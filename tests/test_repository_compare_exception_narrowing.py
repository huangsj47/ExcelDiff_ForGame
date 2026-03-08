from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import services.repository_compare_helpers as compare_helpers


class _Args:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, key, default=None):
        return self._mapping.get(key, default)


def test_repository_compare_exception_tuples_are_declared():
    assert hasattr(compare_helpers, "REPOSITORY_COMPARE_TIME_PARSE_ERRORS")
    assert hasattr(compare_helpers, "REPOSITORY_COMPARE_DIFF_GENERATION_ERRORS")


def test_repository_compare_returns_redirect_when_time_parse_fails(monkeypatch):
    flash_records = []
    project = SimpleNamespace(id=1)
    source_repo = SimpleNamespace(id=11, name="src", project=project)
    target_repo = SimpleNamespace(id=12, name="dst", project=project)

    repo_map = {"11": source_repo, "12": target_repo}
    repository_model = SimpleNamespace(
        query=SimpleNamespace(get_or_404=lambda repo_id: repo_map[str(repo_id)])
    )
    commit_model = SimpleNamespace(query=None)

    monkeypatch.setattr(compare_helpers, "get_runtime_models", lambda *_args: (repository_model, commit_model))
    monkeypatch.setattr(
        compare_helpers,
        "request",
        SimpleNamespace(
            args=_Args(
                {
                    "source": "11",
                    "target": "12",
                    "start_time": "bad-time",
                    "end_time": "2026-03-08T00:00:00Z",
                    "interval": "5",
                }
            )
        ),
    )
    monkeypatch.setattr(compare_helpers, "_has_project_access", lambda _project_id: True)
    monkeypatch.setattr(compare_helpers, "flash", lambda message, category: flash_records.append((category, message)))
    monkeypatch.setattr(compare_helpers, "redirect", lambda url: {"redirect": url})
    monkeypatch.setattr(compare_helpers, "url_for", lambda endpoint: f"/{endpoint}")

    result = compare_helpers.repository_compare()

    assert result == {"redirect": "/index"}
    assert ("error", "时间格式错误") in flash_records


def test_generate_compare_diff_returns_fallback_payload_on_runtime_error(monkeypatch):
    logs = []
    active_processes = {}

    def _get_runtime_model(name):
        if name == "log_print":
            return lambda message, *_args, **_kwargs: logs.append(str(message))
        if name == "active_git_processes":
            return active_processes
        raise AssertionError(f"unexpected runtime model: {name}")

    monkeypatch.setattr(compare_helpers, "get_runtime_model", _get_runtime_model)

    fake_threaded_module = ModuleType("services.threaded_git_service")

    class _ThreadedGitService:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_commit_range_diff(self, *_args, **_kwargs):
            raise RuntimeError("diff failed")

    fake_threaded_module.ThreadedGitService = _ThreadedGitService
    monkeypatch.setitem(sys.modules, "services.threaded_git_service", fake_threaded_module)

    repository = SimpleNamespace(type="git", url="git@repo", root_directory=".", username="u", token="t")
    from_commit = SimpleNamespace(repository=repository, commit_id="a1", version="v1", path="foo.lua")
    to_commit = SimpleNamespace(repository=repository, commit_id="b2", version="v2", path="foo.lua")

    result = compare_helpers.generate_compare_diff(from_commit, to_commit, from_diff_data={}, to_diff_data={})

    assert result["type"] == "code"
    assert result["file_path"] == "foo.lua"
    assert any("diff生成失败" in line["content"] for line in result["lines"])
    assert any("生成对比diff失败" in log for log in logs)
