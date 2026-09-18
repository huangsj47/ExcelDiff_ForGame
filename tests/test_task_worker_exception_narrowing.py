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


def _fake_task_row(row_id, status, task_type='excel_diff'):
    return SimpleNamespace(
        id=row_id,
        status=status,
        started_at='2026-01-01T00:00:00+00:00' if status == 'processing' else None,
        task_type=task_type,
        repository_id=1,
        commit_id='c' * 40,
        file_path='config/30_goods/item.xlsx',
        priority=5,
    )


def test_load_pending_tasks_requeues_interrupted_tasks_in_the_same_call(monkeypatch):
    """重启时被中断（库里还停在 processing）的任务必须**在同一次启动里**重新入队。

    `load_pending_tasks` 曾经把「processing 改回 pending」放在装载循环之后，且只改库、
    不再入队。单机模式下内存队列是唯一执行路径（worker 只从队列取任务），所以那些任务
    要等到**下一次**重启才会跑；期间它们还是 pending，还会占着业务键堵住同键任务的重建
    （add_excel_diff_task 等按 pending/processing 去重）。

    这里断言的是**行为**，不是语句顺序：假库按行的实时 status 回答，所以把重置放回后面
    就会立刻红 —— 那时 pending 查询只看到 id=2，队列里也就只有它。
    """

    class _OrderColumn:
        def asc(self):
            return self

    class _Query:
        def filter_by(self, **kwargs):
            self._status = kwargs.get('status')
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def all(self):
            return [row for row in rows if row.status == self._status]

    class _FakeBackgroundTask:
        query = _Query()
        priority = _OrderColumn()
        created_at = _OrderColumn()

    class _FakeSession:
        def __init__(self):
            self.committed = 0
            self.rollback_called = 0

        def commit(self):
            self.committed += 1

        def rollback(self):
            self.rollback_called += 1

    interrupted = _fake_task_row(1, 'processing')
    waiting = _fake_task_row(2, 'pending')
    rows = [interrupted, waiting]

    enqueued = []
    fake_session = _FakeSession()
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeBackgroundTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))
    monkeypatch.setattr(worker, "background_task_queue", SimpleNamespace(put=enqueued.append))
    monkeypatch.setattr(worker, "TaskWrapper", lambda priority, counter, data: (priority, data))
    monkeypatch.setattr(worker, "check_and_create_auto_sync_tasks", lambda: None)

    worker.load_pending_tasks()

    queued_ids = sorted(entry[1]['task_id'] for entry in enqueued)
    assert queued_ids == [1, 2], "被中断的任务没有在同一次启动里重新入队"
    assert len(enqueued) == 2, "同一个任务被重复入队了"
    assert interrupted.status == 'pending', "库里那行没有被收回成 pending"
    assert interrupted.started_at is None
    assert fake_session.rollback_called == 0
