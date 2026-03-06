import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import app as app_module
from app import app, create_tables, db
from models import Commit, Project, Repository


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def test_get_diff_data_respects_explicit_previous_commit_none(monkeypatch):
    import services.commit_diff_logic as cdl
    import services.threaded_git_service as threaded_git_service

    class _ForbiddenQuery:
        def filter(self, *args, **kwargs):
            raise AssertionError("explicit previous_commit should skip query lookup")

    class _FakeCommitModel:
        query = _ForbiddenQuery()

    called = {"file_diff": 0}

    class _FakeThreadedGitService:
        def __init__(self, *args, **kwargs):
            pass

        def get_file_diff(self, commit_id, file_path):
            called["file_diff"] += 1
            return {"hunks": [{"lines": []}], "type": "code"}

        def get_performance_stats(self):
            return {}

    repository = SimpleNamespace(
        id=1,
        type="git",
        url="https://example.com/repo.git",
        root_directory=None,
        username=None,
        token=None,
    )
    commit = SimpleNamespace(
        id=1,
        repository=repository,
        commit_id="abcd1234ef",
        commit_time=datetime.now(timezone.utc),
        path="src/demo.txt",
    )

    monkeypatch.setattr(cdl, "Commit", _FakeCommitModel)
    monkeypatch.setattr(threaded_git_service, "ThreadedGitService", _FakeThreadedGitService)
    monkeypatch.setattr(cdl, "log_print", lambda *args, **kwargs: None)
    result = cdl.get_diff_data(commit, previous_commit=None)
    assert isinstance(result, dict)
    assert called["file_diff"] == 1


def test_get_diff_data_returns_structured_error_when_git_diff_unavailable(monkeypatch):
    import services.commit_diff_logic as cdl
    import services.threaded_git_service as threaded_git_service

    class _FakeThreadedGitService:
        def __init__(self, *args, **kwargs):
            pass

        def get_file_diff(self, commit_id, file_path):
            return None

        def clone_or_update_repository(self):
            return False, "update failed"

        def get_performance_stats(self):
            return {}

    repository = SimpleNamespace(
        id=1,
        type="git",
        url="https://example.com/repo.git",
        root_directory=None,
        username=None,
        token=None,
    )
    commit = SimpleNamespace(
        id=1,
        repository=repository,
        commit_id="abcd1234ef",
        commit_time=datetime.now(timezone.utc),
        path="src/demo.txt",
    )

    monkeypatch.setattr(threaded_git_service, "ThreadedGitService", _FakeThreadedGitService)
    monkeypatch.setattr(cdl, "_get_unified_diff_data", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cdl, "log_print", lambda *args, **kwargs: None)

    result = cdl.get_diff_data(commit, previous_commit=None)
    assert isinstance(result, dict)
    assert result.get("type") == "error"
    assert "无法获取代码差异" in str(result.get("message") or "")


def test_get_diff_data_retries_after_repo_update(monkeypatch):
    import services.commit_diff_logic as cdl
    import services.threaded_git_service as threaded_git_service

    called = {"file_diff": 0, "update": 0}

    class _FakeThreadedGitService:
        def __init__(self, *args, **kwargs):
            pass

        def get_file_diff(self, commit_id, file_path):
            called["file_diff"] += 1
            if called["file_diff"] == 1:
                return None
            return {
                "type": "code",
                "hunks": [
                    {
                        "header": "@@ -1,1 +1,1 @@",
                        "old_start": 1,
                        "new_start": 1,
                        "lines": [],
                    }
                ],
            }

        def clone_or_update_repository(self):
            called["update"] += 1
            return True, "ok"

        def get_performance_stats(self):
            return {}

    repository = SimpleNamespace(
        id=1,
        type="git",
        url="https://example.com/repo.git",
        root_directory=None,
        username=None,
        token=None,
    )
    commit = SimpleNamespace(
        id=1,
        repository=repository,
        commit_id="abcd1234ef",
        commit_time=datetime.now(timezone.utc),
        path="src/demo.txt",
    )

    monkeypatch.setattr(threaded_git_service, "ThreadedGitService", _FakeThreadedGitService)
    monkeypatch.setattr(cdl, "_get_unified_diff_data", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cdl, "log_print", lambda *args, **kwargs: None)

    result = cdl.get_diff_data(commit, previous_commit=None)
    assert isinstance(result, dict)
    assert result.get("type") == "code"
    assert result.get("file_path") == "src/demo.txt"
    assert called["update"] == 1
    assert called["file_diff"] == 2


def test_commit_diff_route_passes_resolved_previous_commit_to_get_diff_data(monkeypatch):
    seen = {}
    monkeypatch.setattr(app_module.excel_cache_service, "is_excel_file", lambda _path: False)

    def _fake_get_diff_data(commit, previous_commit=None):
        seen["commit_id"] = commit.id
        seen["previous_commit_id"] = previous_commit.id if previous_commit else None
        return {"type": "code", "hunks": []}

    monkeypatch.setattr(app_module, "get_diff_data", _fake_get_diff_data)

    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="diff-route-project")
        db.session.add(project)
        db.session.flush()

        repository = Repository(
            project_id=project.id,
            name=_uid("repo"),
            type="git",
            url="https://example.com/repo.git",
            branch="main",
            clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()

        previous_commit = Commit(
            repository_id=repository.id,
            commit_id=_uid("cprev"),
            path="src/demo.txt",
            operation="M",
            author="alice",
            commit_time=datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
            message="prev",
        )
        current_commit = Commit(
            repository_id=repository.id,
            commit_id=_uid("ccur"),
            path="src/demo.txt",
            operation="M",
            author="bob",
            commit_time=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
            message="cur",
        )
        db.session.add(previous_commit)
        db.session.add(current_commit)
        db.session.commit()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["is_admin"] = True
                sess["admin_user"] = "admin"
            response = client.get(f"/commits/{current_commit.id}/diff")
            assert response.status_code == 200, response.get_data(as_text=True)

    assert seen.get("commit_id") == current_commit.id
    assert seen.get("previous_commit_id") == previous_commit.id


def test_commit_diff_route_records_performance_event_for_code_diff(monkeypatch):
    monkeypatch.setattr(app_module.excel_cache_service, "is_excel_file", lambda _path: False)
    monkeypatch.setattr(
        app_module,
        "get_diff_data",
        lambda *_args, **_kwargs: {"type": "code", "hunks": [{"header": "@@ -1,1 +1,1 @@", "old_start": 1, "new_start": 1, "lines": []}]},
    )

    perf_service = app_module.performance_metrics_service
    perf_service.clear()

    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="perf-project")
        db.session.add(project)
        db.session.flush()

        repository = Repository(
            project_id=project.id,
            name=_uid("repo"),
            type="git",
            url="https://example.com/repo.git",
            branch="main",
            clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()

        commit = Commit(
            repository_id=repository.id,
            commit_id=_uid("ccur"),
            path="src/demo.txt",
            operation="M",
            author="bob",
            commit_time=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
            message="cur",
        )
        db.session.add(commit)
        db.session.commit()

        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["is_admin"] = True
                sess["admin_user"] = "admin"
            resp = client.get(f"/commits/{commit.id}/diff")
            assert resp.status_code == 200, resp.get_data(as_text=True)

    data = perf_service.snapshot(window_minutes=60, recent_limit=50)
    recent = data.get("recent_events") or []
    matched = [event for event in recent if event.get("pipeline") == "api_commit_diff"]
    assert matched, data
    first = matched[0]
    assert first.get("success") is True
    assert (first.get("tags") or {}).get("source") == "realtime_non_excel"


def test_resolve_previous_commit_falls_back_to_vcs_when_db_missing(monkeypatch):
    import services.commit_diff_logic as cdl

    class _EmptyQuery:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def first(self):
            return None

    class _FakeCommitModel:
        class _Expr:
            def __eq__(self, other):
                return self

            def __lt__(self, other):
                return self

            def desc(self):
                return self

        repository_id = _Expr()
        path = _Expr()
        commit_time = _Expr()
        id = _Expr()
        query = _EmptyQuery()

    class _FakeGitService:
        def get_previous_file_commit(self, file_path, current_commit_id, max_count=5000):
            return {
                "commit_id": "prev1234567890",
                "author": "old-author",
                "message": "older commit",
                "commit_time": datetime(2025, 1, 1, 8, 0, 0, tzinfo=timezone.utc),
            }

    repository = SimpleNamespace(
        id=11,
        type="git",
        url="https://example.com/repo.git",
        root_directory=None,
        username=None,
        token=None,
    )
    commit = SimpleNamespace(
        id=22,
        repository_id=11,
        repository=repository,
        commit_id="01982546",
        commit_time=datetime(2026, 3, 5, 3, 7, 30, tzinfo=timezone.utc),
        path="src/modules/createRole/MakeupMod.lua",
    )

    monkeypatch.setattr(cdl, "Commit", _FakeCommitModel)
    monkeypatch.setattr(cdl, "_get_git_service", lambda _repo: _FakeGitService())
    monkeypatch.setattr(cdl, "log_print", lambda *args, **kwargs: None)

    previous = cdl.resolve_previous_commit(commit)
    assert previous is not None
    assert previous.id is None
    assert previous.commit_id == "prev1234567890"
    assert previous.author == "old-author"
