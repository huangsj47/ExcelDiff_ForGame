from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def _import_app_module_for_test():
    with patch("sys.stdout") as mock_stdout, patch("sys.stderr") as mock_stderr:
        mock_stdout.buffer = MagicMock()
        mock_stderr.buffer = MagicMock()
        import app  # noqa: WPS433
        return app


def _safe_import_app_or_skip():
    try:
        return _import_app_module_for_test()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"app import unavailable in this environment: {exc}")


class TestRemainingHighPriorityStaticChecks:
    def test_generate_merged_diff_data_no_placeholder_todo(self):
        content = _read("services/commit_diff_logic.py")
        assert "def generate_merged_diff_data(" in content
        assert "handle_consecutive_commits_merge_internal" in content
        assert "handle_non_consecutive_commits_merge_internal" in content
        assert "is_rename_suspected" in content
        assert "has_conflict_risk" in content
        assert "TODO: 实现实际的文件内容diff合并逻辑" not in content

    def test_models_package_has_standalone_db_instance(self):
        content = _read("models/__init__.py")
        assert "db = SQLAlchemy()" in content
        assert "from flask_sqlalchemy import SQLAlchemy" in content
        # init_app should only appear in comments/docstrings, not as actual code
        import re
        # Remove triple-quoted docstrings first
        code_only = re.sub(r'"""[\s\S]*?"""', '', content)
        code_only = re.sub(r"'''[\s\S]*?'''", '', code_only)
        for line in code_only.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue  # skip blank lines and comments
            assert "db.init_app" not in stripped, \
                f"db.init_app should not be called in models/__init__.py (found in: {stripped})"

    def test_repository_to_dict_no_invalid_clone_progress_field(self):
        content = _read("models/repository.py")
        assert "clone_progress" not in content

    def test_commit_list_branch_probe_is_async(self):
        # queue_missing_git_branch_refresh 已拆分到 services/task_worker_service.py
        tws_content = _read("services/task_worker_service.py")
        assert "def queue_missing_git_branch_refresh(" in tws_content
        content = _read("app.py")
        assert "missing_git_branch_repo_ids.append(repo.id)" in content
        assert "queue_missing_git_branch_refresh(project.id, missing_git_branch_repo_ids)" in content
        assert "git_service = ThreadedGitService(repo.url, repo.root_directory, repo.username, repo.token, repo)" not in content

    def test_excel_diff_previous_commit_todo_removed(self):
        content = _read("app.py")
        assert "TODO: 实现Excel文件的前一提交比较逻辑" not in content
        assert "diff_data = get_unified_diff_data(commit, previous_commit)" in content

    def test_async_repository_update_uses_app_context_and_id_reload(self):
        content = _read("app.py")
        assert "def run_repository_update_and_cache(repository_id):" in content
        assert "with app.app_context():" in content
        assert "repository = db.session.get(Repository, repository_id)" in content
        assert "threading.Thread(target=run_repository_update_and_cache, args=(repository_id,), daemon=True)" in content

    def test_async_refilter_reloads_repository_in_context(self):
        content = _read("app.py")
        assert "def async_refilter():" in content
        assert "with app.app_context():" in content
        assert "repo = db.session.get(Repository, repository_id)" in content

    def test_scheduler_runs_pending_with_app_context(self):
        # run_scheduled_tasks 已拆分到 services/task_worker_service.py
        content = _read("services/task_worker_service.py")
        assert "def run_scheduled_tasks():" in content
        assert "app_context()" in content
        assert "run_pending()" in content


class TestRemainingHighPriorityRuntimeChecks:
    def test_generate_merged_diff_data_segmented_strategy(self, monkeypatch):
        _safe_import_app_or_skip()
        import services.commit_diff_logic as cdl

        repo = SimpleNamespace(id=1, type="git")
        commit_1 = SimpleNamespace(
            id=1,
            commit_id="c0000001",
            author="alice",
            operation="M",
            commit_time=datetime(2026, 2, 1, 10, 0, 0, tzinfo=timezone.utc),
            path="foo/bar.txt",
            repository=repo,
        )
        commit_2 = SimpleNamespace(
            id=2,
            commit_id="c0000002",
            author="bob",
            operation="M",
            commit_time=datetime(2026, 2, 2, 10, 0, 0, tzinfo=timezone.utc),
            path="foo/bar.txt",
            repository=repo,
        )

        monkeypatch.setattr(cdl, "are_commits_consecutive_internal", lambda _commits: False)
        monkeypatch.setattr(
            cdl,
            "handle_non_consecutive_commits_merge_internal",
            lambda _commits: {
                "type": "segmented_diff",
                "segments": [
                    {"segment_info": {"segment_index": 1, "current": "c0000002", "previous": "c0000001"}}
                ],
            },
        )
        monkeypatch.setattr(cdl, "handle_consecutive_commits_merge_internal", lambda _commits: None)
        monkeypatch.setattr(cdl, "get_commit_pair_diff_internal", lambda *_: {"type": "text", "hunks": []})
        monkeypatch.setattr(cdl, "_get_unified_diff_data", lambda *_: {"type": "text", "hunks": []})

        result = cdl.generate_merged_diff_data(
            repository=repo,
            file_path="foo/bar.txt",
            base_commit=SimpleNamespace(commit_id="base0001"),
            latest_commit=commit_2,
            commits=[commit_1, commit_2],
        )

        assert result["merge_strategy"] == "segmented"
        assert result["has_conflict_risk"] is True
        assert result["total_segments"] == 1
        assert result["operations"] == ["M", "M"]

    def test_generate_merged_diff_data_operation_signals(self, monkeypatch):
        _safe_import_app_or_skip()
        import services.commit_diff_logic as cdl

        repo = SimpleNamespace(id=2, type="git")
        commit_1 = SimpleNamespace(
            id=10,
            commit_id="c1000001",
            author="alice",
            operation="added",
            commit_time=datetime(2026, 2, 3, 10, 0, 0, tzinfo=timezone.utc),
            path="foo/baz.txt",
            repository=repo,
        )
        commit_2 = SimpleNamespace(
            id=11,
            commit_id="c1000002",
            author="alice",
            operation="deleted",
            commit_time=datetime(2026, 2, 4, 10, 0, 0, tzinfo=timezone.utc),
            path="foo/baz.txt",
            repository=repo,
        )

        monkeypatch.setattr(cdl, "are_commits_consecutive_internal", lambda _commits: True)
        monkeypatch.setattr(cdl, "handle_consecutive_commits_merge_internal", lambda _commits: {"type": "text", "hunks": []})
        monkeypatch.setattr(cdl, "get_commit_pair_diff_internal", lambda *_: {"type": "text", "hunks": []})
        monkeypatch.setattr(cdl, "_get_unified_diff_data", lambda *_: {"type": "text", "hunks": []})

        result = cdl.generate_merged_diff_data(
            repository=repo,
            file_path="foo/baz.txt",
            base_commit=SimpleNamespace(commit_id="base0002"),
            latest_commit=commit_2,
            commits=[commit_1, commit_2],
        )

        assert result["merge_strategy"] == "consecutive"
        assert result["contains_added"] is True
        assert result["contains_deleted"] is True
        assert result["is_rename_suspected"] is True
