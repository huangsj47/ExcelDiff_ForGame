# -*- coding: utf-8 -*-
"""队列饿死：同步任务去重必须连 `processing` 一起看。

## 缺陷形态（本机实测出来的）

队列只有一个 worker，调度器每 2 分钟 tick 一次、每 tick 都为活跃配置建一条
`weekly_sync`（优先级 3）。**去重只看 `pending` 时**，正在跑的那条是 `processing`，
于是每次 tick 都能再排一条新的 —— 而一条大仓库的同步要跑 4~5 分钟（820 个文件），
tick 却每 2 分钟就来一次，队列里**永远有一条优先级 3 在等**。worker 每次取优先级最小
的那条，优先级 ≥5 的于是**永远轮不到**。实测证据（2026-09-20）：

* `auto_sync`（5）—— 自 06:08 起 12 小时一次都没跑过 = **仓库再也没被 fetch 过**；
* `weekly_excel_cache`（5）—— 27 条挂了 12 小时，周版本 Excel 的 HTML 缓存从没生成过；
* `weekly_ai_analysis`（6）—— 库里的定时分析**一条都没真正跑过**。

## 这些测试变红意味着什么

* `test_a_processing_sync_*`：去重退回只看 `pending` —— 饿死循环回来了，而且这次是
  静默的（低优先级任务既不报错也不消失，只是永远「排队中」）。
* `test_a_finished_sync_*` / `test_a_failed_sync_*`：反向过头了 —— 把已结束的行也算成
  「还在跑」，同步从此**再也不建新任务**，周版本数据会停在某一刻不动。
* `test_a_pending_sync_*`：既有行为（pending 去重）被改坏。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.task_worker_service as worker
import services.task_worker_weekly_handlers as handlers
from models import BackgroundTask, db


@pytest.fixture(autouse=True)
def _clean_enqueue_ledger():
    """「已入队」账本是模块级集合：用例之间必须清干净。"""
    handlers._enqueued_weekly_sync_task_ids.clear()
    yield
    handlers._enqueued_weekly_sync_task_ids.clear()


@pytest.fixture()
def single_mode(monkeypatch):
    """单机模式 + 把内存队列换成记录器（真实入队会把任务留在全局队列里污染别的用例）。"""
    enqueued = []
    monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: False)
    from types import SimpleNamespace

    monkeypatch.setattr(worker, "background_task_queue", SimpleNamespace(put=enqueued.append))
    monkeypatch.setattr(worker, "TaskWrapper", lambda priority, counter, data: data)
    return enqueued


def _config_id() -> int:
    """每个用例一个独立 config_id —— 测试库是会话级共用的，没有逐用例重置。"""
    return uuid.uuid4().int % 900_000_000 + 1


def _add_task(config_id, status):
    task = BackgroundTask(
        task_type='weekly_sync',
        repository_id=None,
        commit_id=str(config_id),
        priority=3,
        status=status,
    )
    db.session.add(task)
    db.session.commit()
    return task


def _sync_rows(config_id):
    return BackgroundTask.query.filter_by(
        task_type='weekly_sync', commit_id=str(config_id)
    ).all()


def test_a_processing_sync_blocks_the_next_tick(app):
    """**这条就是饿死循环的解药**：正在跑的那条必须挡住新的一条。

    变红 = 每个 tick 都能再排一条优先级 3，队列里永远有它，低优先级任务永远轮不到。
    """
    with app.app_context():
        config_id = _config_id()
        running = _add_task(config_id, 'processing')
        running_id = running.id

        returned = worker.create_weekly_sync_task(config_id)

        assert returned == running_id, (
            f"配置 {config_id} 正在跑同步，却又建了新任务（返回 {returned}）—— "
            f"队列里会永远压着一条优先级 3"
        )
        rows = _sync_rows(config_id)
        assert len(rows) == 1, f"同一时刻出现了 {len(rows)} 条同步任务：{[r.id for r in rows]}"


def test_a_processing_sync_is_not_re_enqueued(app, single_mode):
    """跑着的那条**也不许补入队**：worker 正拿在手里，补一份 = 同一个同步跑两遍。"""
    with app.app_context():
        config_id = _config_id()
        running = _add_task(config_id, 'processing')

        worker.create_weekly_sync_task(config_id)

        assert single_mode == [], f"processing 的那条被重复入队了：{single_mode}"
        assert not handlers.is_weekly_sync_task_enqueued(running.id)


def test_a_pending_sync_still_blocks(app, single_mode):
    """反向自检：`pending` 去重是既有行为，不能被这次改动碰掉。"""
    with app.app_context():
        config_id = _config_id()
        waiting = _add_task(config_id, 'pending')
        waiting_id = waiting.id

        returned = worker.create_weekly_sync_task(config_id)

        assert returned == waiting_id
        assert len(_sync_rows(config_id)) == 1, "pending 的那条没能挡住新建"
        # 库里有 pending、内存队列里没有（进程重启过）→ 补一次入队，这是既有修复
        assert [item['task_id'] for item in single_mode] == [waiting_id], (
            f"内存队列丢了 pending 任务，却没有补入队：{single_mode}"
        )


def test_a_finished_sync_does_not_block_the_next_cycle(app, single_mode):
    """反向过头同样致命：已结束的行**不许**算「还在跑」，否则同步再也不建新任务。

    这一条与上面那条是一对：只看 `processing` 会让低优先级饿死，把 `completed` 也算进去
    则会让周版本数据永远停在某一刻。
    """
    with app.app_context():
        config_id = _config_id()
        _add_task(config_id, 'completed')

        worker.create_weekly_sync_task(config_id)

        rows = _sync_rows(config_id)
        assert len(rows) == 2, (
            f"上一轮同步已经结束了，却没有建新任务 —— 这个周版本不会再更新："
            f"{[(r.id, r.status) for r in rows]}"
        )
        fresh = [row for row in rows if row.status == 'pending']
        assert len(fresh) == 1, f"新建的那条不是 pending：{[(r.id, r.status) for r in rows]}"
        assert [item['task_id'] for item in single_mode] == [fresh[0].id]


def test_a_failed_sync_does_not_block_either(app, single_mode):
    """失败的行同理：它不会再被执行，必须让下一次调度照常建任务。"""
    with app.app_context():
        config_id = _config_id()
        _add_task(config_id, 'failed')

        worker.create_weekly_sync_task(config_id)

        assert len(_sync_rows(config_id)) == 2, "上一轮失败之后就不再建新任务了"


def test_a_sync_wedged_in_processing_no_longer_freezes_the_version(app, single_mode):
    """**把 `processing` 也算进去重之后新增的风险**：那一行如果永远不会被写终态
    （worker 以非 `NON_CRITICAL_*` 的异常死掉、或写终态一直失败），而进程还活着
    （`load_pending_tasks()` 不会跑），去重就会一直命中它 —— 同步永不重建、也不报错。

    超过阈值必须按「没人管了」处理：置 failed（留下原因）并照常重建。
    """
    with app.app_context():
        config_id = _config_id()
        wedged = _add_task(config_id, 'processing')
        wedged.started_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=worker.WEDGED_SYNC_PROCESSING_SECONDS + 60
        )
        db.session.commit()
        wedged_id = wedged.id

        worker.create_weekly_sync_task(config_id)

        rows = {row.id: row for row in _sync_rows(config_id)}
        assert len(rows) == 2, (
            f"卡在处理中的任务挡住了重建 —— 这个周版本再也不会同步："
            f"{[(r.id, r.status) for r in rows.values()]}"
        )
        assert rows[wedged_id].status == 'failed', "卡死的那条没有被置为失败"
        assert rows[wedged_id].error_message, "重置卡死任务时没有写入原因"
        fresh = [row for row in rows.values() if row.status == 'pending']
        assert len(fresh) == 1
        assert [item['task_id'] for item in single_mode] == [fresh[0].id]


def test_a_sync_that_just_started_is_never_treated_as_wedged(app, single_mode):
    """**反自检**：刚开始跑的同步不许被当成卡死 —— 否则每个 tick 都会另起一个同步，
    两个任务同时写同一份周版本缓存（那正是这次要修的另一头）。
    """
    with app.app_context():
        config_id = _config_id()
        running = _add_task(config_id, 'processing')
        running.started_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=90)
        db.session.commit()

        worker.create_weekly_sync_task(config_id)

        assert len(_sync_rows(config_id)) == 1, "刚跑了 90 秒的同步被判成卡死了"
        assert single_mode == [], "正在跑的同步被重复入队"


@pytest.fixture()
def app():
    import app as app_module

    with app_module.app.app_context():
        app_module.create_tables()
    return app_module.app
