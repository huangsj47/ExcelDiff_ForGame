# -*- coding: utf-8 -*-
"""等不到同步并发许可时，任务必须被**了结**，不能留下一行 pending 占着去重位。

## 缺陷形态

`_handle_auto_sync_task` 先要 `_sync_semaphore.acquire(timeout=120)`（最多 5 个仓库
同时更新）。超时分支原先只打一行日志就 `return`，而队列语义是「worker 取走即消费」：

* 单机模式下内存队列是**唯一**执行路径（worker 只从 `background_task_queue` 取任务），
  任务已经从队列里弹掉了，不会再被执行；
* 库里的行仍然是 `pending`，所以也不会被 `load_pending_tasks` 在本次进程内捞回来；
* 而 `create_auto_sync_task` 的去重条件是「同仓库 + `task_type='auto_sync'` +
  `status='pending'`」—— 这一行正好把去重位占住。

后果是**该仓库静默停止同步**：此后每一次「更新仓库 / 重试同步」都命中去重，
返回那个永远不会被执行的 `existing_task.id`，调用方以为任务已经派下去了。
唯一出路是重启进程。所以「跳过本次同步」这个意图，实现成了「永远不再同步」。

## 这个测试怎么测

真的建一行 `BackgroundTask`、真的调 `_handle_auto_sync_task`，并且**在 app context
之外调用** —— worker 是 `threading.Thread` 启动的，不继承主线程的 context，
这正是生产环境的形态。然后：

1. 断言那一行不再是 `pending`（去重位释放），且落下了失败原因；
2. 再真的调一次 `create_auto_sync_task`，断言它**建出了新任务** —— 这才是「去重位
   真的释放了」的行为证据，比在测试里复述一遍那条 filter 更可靠；
3. 反向自检：许可拿到手时必须照旧跑同步（否则「把闸门焊死」也能让上面两条变绿）。
"""
from __future__ import annotations

import uuid

import pytest

import services.task_worker_service as worker
from app import app, create_tables, db
from models import Project, Repository
from models.task import BackgroundTask


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _DeniedSemaphore:
    """永远拿不到许可的信号量，并记下调用方给的等待窗口。"""

    def __init__(self):
        self.timeouts = []
        self.released = 0

    def acquire(self, timeout=None):
        self.timeouts.append(timeout)
        return False

    def release(self):
        self.released += 1


class _GrantedSemaphore(_DeniedSemaphore):
    def acquire(self, timeout=None):
        self.timeouts.append(timeout)
        return True


@pytest.fixture
def wired(monkeypatch):
    """把 worker 的模块级依赖指到真库上，并按真库建一个仓库 + 一行 pending 任务。"""
    enqueued = []
    monkeypatch.setattr(worker, "_app", app)
    monkeypatch.setattr(worker, "_db", db)
    monkeypatch.setattr(worker, "_BackgroundTask", BackgroundTask)
    monkeypatch.setattr(worker, "_Repo", Repository, raising=False)
    monkeypatch.setattr(worker, "_Repository", Repository, raising=False)
    # 队列换成列表：断言「有没有真的入队」比污染模块级队列干净。
    monkeypatch.setattr(worker, "background_task_queue", type("Q", (), {"put": staticmethod(enqueued.append)})())
    monkeypatch.setattr(worker, "TaskWrapper", lambda priority, counter, data: (priority, data))

    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("sync-project"))
        db.session.add(project)
        db.session.flush()
        repo = Repository(
            project_id=project.id,
            name=_uid("repo"),
            type="git",
            url=f"https://example.com/{_uid('r')}.git",
            branch="main",
            resource_type="code",
            clone_status="completed",
        )
        db.session.add(repo)
        db.session.flush()
        task = BackgroundTask(
            task_type="auto_sync", repository_id=repo.id, priority=5, status="pending"
        )
        db.session.add(task)
        db.session.commit()
        ids = {"project": project.id, "repo": repo.id, "task": task.id}

    return ids, enqueued


def test_a_denied_permit_resolves_the_task_instead_of_leaving_it_pending(wired):
    """**核心回归**：超时后那一行不许再是 pending。"""
    ids, _enqueued = wired
    semaphore = _DeniedSemaphore()
    worker._sync_semaphore = semaphore

    # 关键：在 app context **之外**调用 —— worker 线程就是这个处境
    worker._handle_auto_sync_task({"repository_id": ids["repo"], "task_id": ids["task"]})

    with app.app_context():
        row = db.session.get(BackgroundTask, ids["task"])
        assert row.status != "pending", (
            "超时后任务仍是 pending —— 它已经不在内存队列里了，"
            "却还占着 create_auto_sync_task 的去重位：该仓库会静默停止同步"
        )
        assert row.status == "failed", f"期望标成 failed，实际 {row.status!r}"
        assert row.completed_at is not None, "没写完成时间，界面上看不出它是什么时候停的"
        assert "超时" in (row.error_message or ""), "没说清为什么没执行"
    assert semaphore.timeouts == [worker.SYNC_SEMAPHORE_TIMEOUT_SECONDS], "等待窗口被改掉了"
    assert semaphore.released == 0, "没拿到许可却 release 了信号量（会把并发上限悄悄放大）"


def test_the_dedupe_slot_is_really_free_after_a_timeout(wired):
    """行为证据：超时之后再触发一次，必须**建出新任务**，而不是被去重成空操作。"""
    ids, enqueued = wired
    worker._sync_semaphore = _DeniedSemaphore()

    worker._handle_auto_sync_task({"repository_id": ids["repo"], "task_id": ids["task"]})

    with app.app_context():
        new_id = worker.create_auto_sync_task(ids["repo"])
        assert new_id is not None, "没能建出新任务"
        assert new_id != ids["task"], (
            "创建任务被去重到了那个超时任务上 —— 调用方会以为任务已派下去，"
            "而它永远不会被执行"
        )
        pending = BackgroundTask.query.filter_by(
            repository_id=ids["repo"], task_type="auto_sync", status="pending"
        ).all()
        assert [p.id for p in pending] == [new_id]


def test_a_granted_permit_still_runs_the_sync(wired, monkeypatch):
    """反向自检：许可拿到手时照旧执行 —— 免得「把闸门焊死」也能让上面两条变绿。"""
    ids, _enqueued = wired
    semaphore = _GrantedSemaphore()
    worker._sync_semaphore = semaphore
    ran = []
    monkeypatch.setattr(worker, "_handle_auto_sync_task_inner", lambda task: ran.append(task))

    worker._handle_auto_sync_task({"repository_id": ids["repo"], "task_id": ids["task"]})

    assert [t["repository_id"] for t in ran] == [ids["repo"]], "拿到许可却没执行同步"
    assert semaphore.released == 1, "执行完没有释放许可"


def test_the_agent_dispatch_path_without_a_task_id_is_left_alone(wired, monkeypatch):
    """Agent 派发路径不传 task_id：那条路径的状态由 agent 侧管，不能替它改。"""
    ids, _enqueued = wired
    worker._sync_semaphore = _DeniedSemaphore()
    marked = []
    monkeypatch.setattr(
        worker, "update_task_status_with_retry", lambda *args, **kwargs: marked.append(args)
    )

    worker._handle_auto_sync_task({"repository_id": ids["repo"]})

    assert marked == [], "没有 task_id 却还是去改了任务状态"


def test_a_missing_row_does_not_explode(monkeypatch):
    """任务行被别处删掉时，超时分支只该留一行日志。"""
    logs = []
    monkeypatch.setattr(worker, "log_print", lambda message, *a, **k: logs.append(message))
    monkeypatch.setattr(worker, "_app", app)
    monkeypatch.setattr(worker, "_db", db)
    monkeypatch.setattr(worker, "_BackgroundTask", BackgroundTask)

    worker._abandon_timed_out_auto_sync_task(
        {"repository_id": 1, "task_id": 999999999}, "等待同步并发许可超时(120s)，本次同步未执行"
    )

    assert any("未找到任务" in message for message in logs), f"缺行时没有任何说明：{logs}"
