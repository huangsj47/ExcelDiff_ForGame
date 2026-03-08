from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.exc import SQLAlchemyError

import services.repository_sync_status as sync_status


def test_record_sync_error_success_updates_fields_and_commits():
    commit_called = {"value": False}
    session = SimpleNamespace(commit=lambda: commit_called.__setitem__("value", True), rollback=lambda: None)
    repo = SimpleNamespace(id=1, name="demo", last_sync_error=None, last_sync_error_time=None)

    ok = sync_status.record_sync_error(session, repo, "sync failed", commit=True)

    assert ok is True
    assert commit_called["value"] is True
    assert repo.last_sync_error == "sync failed"
    assert isinstance(repo.last_sync_error_time, datetime)
    assert repo.last_sync_error_time.tzinfo == timezone.utc


def test_record_sync_error_returns_false_when_commit_raises_and_rollback_fails():
    rollback_called = {"value": False}

    def _rollback():
        rollback_called["value"] = True
        raise RuntimeError("rollback also failed")

    session = SimpleNamespace(
        commit=lambda: (_ for _ in ()).throw(SQLAlchemyError("db commit failed")),
        rollback=_rollback,
    )
    repo = SimpleNamespace(id=2, name="demo2", last_sync_error=None, last_sync_error_time=None)

    ok = sync_status.record_sync_error(session, repo, "sync failed", commit=True)

    assert ok is False
    assert rollback_called["value"] is True


def test_clear_sync_error_success_clears_fields_and_commits():
    commit_called = {"value": False}
    session = SimpleNamespace(commit=lambda: commit_called.__setitem__("value", True), rollback=lambda: None)
    repo = SimpleNamespace(id=3, name="demo3", last_sync_error="bad", last_sync_error_time=datetime.now(timezone.utc))

    ok = sync_status.clear_sync_error(session, repo, commit=True)

    assert ok is True
    assert commit_called["value"] is True
    assert repo.last_sync_error is None
    assert repo.last_sync_error_time is None


def test_clear_sync_error_returns_true_when_no_existing_error():
    session = SimpleNamespace(commit=lambda: (_ for _ in ()).throw(AssertionError("should not commit")), rollback=lambda: None)
    repo = SimpleNamespace(id=4, name="demo4", last_sync_error=None, last_sync_error_time=None)

    ok = sync_status.clear_sync_error(session, repo, commit=True)

    assert ok is True


def test_clear_sync_error_returns_false_when_commit_raises():
    rollback_called = {"value": False}

    def _rollback():
        rollback_called["value"] = True

    session = SimpleNamespace(
        commit=lambda: (_ for _ in ()).throw(SQLAlchemyError("db commit failed")),
        rollback=_rollback,
    )
    repo = SimpleNamespace(id=5, name="demo5", last_sync_error="bad", last_sync_error_time=datetime.now(timezone.utc))

    ok = sync_status.clear_sync_error(session, repo, commit=True)

    assert ok is False
    assert rollback_called["value"] is True
