import os
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

import services.task_worker_service as worker


def test_force_remove_repo_worktree_returns_false_when_fallback_delete_fails(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo_keep"
    repo_dir.mkdir()

    monkeypatch.setattr(
        worker.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("permission denied")),
    )
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.SubprocessError("rmdir failed")),
    )

    assert worker._force_remove_repo_worktree(str(repo_dir)) is False
    assert os.path.exists(repo_dir)


def test_cleanup_git_processes_handles_process_runtime_errors():
    class _FakeProc:
        def __init__(self):
            self.kill_called = False

        def poll(self):
            raise OSError("broken process handle")

        def kill(self):
            self.kill_called = True

    proc = _FakeProc()
    worker._active_git_processes = {proc}

    worker.cleanup_git_processes()

    assert proc.kill_called is True
    assert proc not in worker._active_git_processes


def test_update_task_status_with_retry_rolls_back_on_sqlalchemy_error(monkeypatch):
    class _FakeTask:
        def __init__(self):
            self.status = "pending"
            self.retry_count = 0
            self.started_at = None
            self.completed_at = None
            self.error_message = None

    class _FakeSession:
        def __init__(self):
            self.rollback_called = 0

        def get(self, _model, _task_id):
            return _FakeTask()

        def commit(self):
            raise SQLAlchemyError("commit failed")

        def rollback(self):
            self.rollback_called += 1

    fake_session = _FakeSession()
    fake_db = SimpleNamespace(session=fake_session)

    monkeypatch.setattr(worker, "_db", fake_db)
    monkeypatch.setattr(worker, "_BackgroundTask", object)

    with pytest.raises(SQLAlchemyError):
        worker.update_task_status_with_retry(123, "processing")

    assert fake_session.rollback_called == 1


def test_create_auto_sync_task_rolls_back_on_sqlalchemy_error(monkeypatch):
    class _FakeQuery:
        def filter_by(self, **_kwargs):
            return self

        def first(self):
            raise SQLAlchemyError("query failed")

    class _FakeBackgroundTask:
        query = _FakeQuery()

    class _FakeSession:
        def __init__(self):
            self.rollback_called = 0

        def rollback(self):
            self.rollback_called += 1

    fake_session = _FakeSession()
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeBackgroundTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))

    task_id = worker.create_auto_sync_task(101)
    assert task_id is None
    assert fake_session.rollback_called == 1


def test_load_pending_tasks_rolls_back_on_sqlalchemy_error(monkeypatch):
    class _OrderColumn:
        def asc(self):
            return self

    class _FakeQuery:
        def filter_by(self, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def all(self):
            raise SQLAlchemyError("list failed")

    class _FakeBackgroundTask:
        query = _FakeQuery()
        priority = _OrderColumn()
        created_at = _OrderColumn()

    class _FakeSession:
        def __init__(self):
            self.rollback_called = 0

        def rollback(self):
            self.rollback_called += 1

    fake_session = _FakeSession()
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeBackgroundTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))

    worker.load_pending_tasks()
    assert fake_session.rollback_called == 1


def test_create_weekly_sync_task_rolls_back_on_sqlalchemy_error(monkeypatch):
    class _FakeQuery:
        def filter_by(self, **_kwargs):
            return self

        def first(self):
            return None

    class _FakeBackgroundTask:
        query = _FakeQuery()

        def __init__(self, **kwargs):
            self.id = 999
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _FakeSession:
        def __init__(self):
            self.rollback_called = 0

        def add(self, _obj):
            return None

        def flush(self):
            raise SQLAlchemyError("flush failed")

        def rollback(self):
            self.rollback_called += 1

    fake_session = _FakeSession()
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeBackgroundTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))

    task_id = worker.create_weekly_sync_task(321)
    assert task_id is None
    assert fake_session.rollback_called == 1
