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
