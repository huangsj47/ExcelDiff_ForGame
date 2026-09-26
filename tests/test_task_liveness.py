# -*- coding: utf-8 -*-
"""「这条后台任务的执行者还在不在」（`services/task_liveness.py`）。

## 这一层为什么存在

同一个事实（「这条同步还在跑吗」）原先在三处各写了一个**30 分钟墙钟**：同步闸门、
等待意图的上限、周版本文件页那条「陈旧任务」判据。墙钟分不出「慢」和「死」，而这两件事的
正确动作相反；三个 30 分钟还互相拆台 —— 大仓的同步（真机 2054 个提交 / 583 个文件，
十几分钟起步）跑到第 30 分钟时：闸门放行（AI 读半份缓存）、文件页把它置 failed、
意图作废。

租约分得出，而且**合法运行永远不会撞上租约到期** —— 到期只剩「执行者死了」一个含义。
所以这一层只回答一个问题，三处都来问它。

## 两套租约都要看

`BackgroundTask.lease_expires_at`（本进程 worker）与 `AgentTask.lease_expires_at`
（agent 派发模式下真正在跑的那条 —— 那个模式下平台只派发不执行，BackgroundTask 从头到尾
停在 `pending`、没有租约）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app import app as flask_app
from app import create_tables, db
from models import AgentTask, BackgroundTask, Project
from services import task_liveness


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _utc(**kwargs) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kwargs)


def _seed():
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("LV"), name="执行者存活用例")
        db.session.add(project)
        db.session.commit()
        return {"project_id": project.id}


def _background_task(project_id, **fields):
    row = BackgroundTask(
        task_type="weekly_sync",
        commit_id="1",
        priority=3,
        status=fields.pop("status", "processing"),
        **fields,
    )
    db.session.add(row)
    db.session.flush()
    return row


# --------------------------------------------------------------------------
#  一、本进程 worker 的租约
# --------------------------------------------------------------------------


class TestTheLocalLease:
    def test_a_live_lease_means_the_executor_is_alive(self):
        seeded = _seed()
        with flask_app.app_context():
            row = _background_task(seeded["project_id"], lease_expires_at=_utc(minutes=5))
            db.session.commit()

            assert task_liveness.executor_alive(row) is True

    def test_an_expired_lease_means_the_executor_is_gone(self):
        """**到期只剩这一个含义** —— 真的在跑的任务由 `renew_inflight_task_leases` 续租，
        所以它只会被「执行者死了」撞上（见模块 docstring）。"""
        seeded = _seed()
        with flask_app.app_context():
            row = _background_task(seeded["project_id"], lease_expires_at=_utc(minutes=-5))
            db.session.commit()

            assert task_liveness.executor_alive(row) is False

    def test_no_lease_at_all_is_not_alive(self):
        """老数据 / 从没被认领过的行都没有租约 —— 按「不在」处理。

        反过来（按在）等于让一条没有租约的僵尸任务**永久**拦住分析：调用方退回的年龄兜底
        是最后一道保底。
        """
        seeded = _seed()
        with flask_app.app_context():
            row = _background_task(seeded["project_id"], lease_expires_at=None)
            db.session.commit()

            assert task_liveness.executor_alive(row) is False

    def test_a_naive_utc_deadline_is_compared_correctly(self):
        """库里的时间是 naive-UTC，`now` 可能带时区 —— 混着减会抛 TypeError，
        而抛出去的表现是「闸门每次都不拦」（比不加闸更糟）。"""
        seeded = _seed()
        with flask_app.app_context():
            naive_deadline = _utc(minutes=5).astimezone(timezone.utc).replace(tzinfo=None)
            row = _background_task(seeded["project_id"], lease_expires_at=naive_deadline)
            db.session.commit()

            assert task_liveness.executor_alive(row, now=datetime.now(timezone.utc)) is True
            assert task_liveness.executor_alive(row, now=_utc(minutes=10)) is False

    def test_none_is_never_alive(self):
        assert task_liveness.executor_alive(None) is False


# --------------------------------------------------------------------------
#  二、agent 派发模式：租约在**另一条**任务上
# --------------------------------------------------------------------------


class TestTheDispatchedLease:
    """派发模式下平台**只派发、不执行** —— 那条 BackgroundTask 从头到尾停在 `pending`、
    没有租约（`create_weekly_sync_task` 不进本机队列，Agent 回传时才写终态），干活的是
    Agent 节点。

    只查 `BackgroundTask.lease_expires_at` 会让这一层在派发模式下退回墙钟 ——
    大仓同步照样在第 30 分钟被放行。所以要看 `AgentTask` 那条（靠 `source_task_id` 指回来）。
    """

    def _dispatch_pair(self, project_id, *, agent_lease, agent_status="processing"):
        source = _background_task(project_id, status="pending", lease_expires_at=None)
        db.session.flush()
        dispatched = AgentTask(
            task_type="weekly_sync",
            project_id=project_id,
            source_task_id=source.id,
            status=agent_status,
            lease_expires_at=agent_lease,
        )
        db.session.add(dispatched)
        db.session.commit()
        return source, dispatched

    def test_a_live_agent_lease_keeps_the_source_task_alive(self):
        seeded = _seed()
        with flask_app.app_context():
            source, _ = self._dispatch_pair(seeded["project_id"], agent_lease=_utc(minutes=5))

            assert task_liveness.executor_alive(source) is True, (
                "Agent 节点还在跑，平台侧却判执行者不在 —— 这一层会退回墙钟"
            )

    def test_an_expired_agent_lease_means_the_executor_is_gone(self):
        seeded = _seed()
        with flask_app.app_context():
            source, _ = self._dispatch_pair(seeded["project_id"], agent_lease=_utc(minutes=-5))

            assert task_liveness.executor_alive(source) is False

    def test_a_finished_agent_task_is_not_alive_even_with_a_stale_deadline(self):
        """Agent 已经交回结果（终态）但租约字段还留着 → **不算在跑**。

        判据是「状态还在跑 **且** 租约未过期」两个条件，缺一不可：只看租约会把一条已经
        结束、租约恰好还没到点的任务当成活的。
        """
        seeded = _seed()
        with flask_app.app_context():
            source, _ = self._dispatch_pair(
                seeded["project_id"], agent_lease=_utc(minutes=5), agent_status="completed"
            )

            assert task_liveness.executor_alive(source) is False

    def test_a_source_row_without_an_agent_task_is_not_alive(self):
        seeded = _seed()
        with flask_app.app_context():
            source = _background_task(seeded["project_id"], status="pending", lease_expires_at=None)
            db.session.commit()

            assert task_liveness.executor_alive(source) is False


# --------------------------------------------------------------------------
#  三、接线：周版本文件页那条「陈旧任务」判据真的用了它
#
#  **「接线了不等于被用了」**（见 [[wired-is-not-used]]）：这一层做好之后，
#  `weekly_version_files_api_helpers.should_treat_sync_task_as_stale` 必须真的问它 ——
#  否则页面照样在第 30 分钟把一条**正在写缓存**的同步置 failed（而它正是同步闸门在等的
#  那一条），本层就是一段死代码。
# --------------------------------------------------------------------------


class TestTheWeeklyFilePageUsesIt:
    @staticmethod
    def _now():
        """页面那条路传进来的 `now` 是 **naive-UTC**（`weekly_version_logic` 的
        `now_local`），年龄判据按 naive 相减 —— 这里照样造，别用带时区的。"""
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _task(self, project_id, *, status, age_seconds, lease):
        with flask_app.app_context():
            row = BackgroundTask(
                task_type="weekly_sync",
                commit_id="1",
                priority=3,
                status=status,
                created_at=_utc(seconds=-age_seconds),
                started_at=_utc(seconds=-age_seconds),
                lease_expires_at=lease,
            )
            db.session.add(row)
            db.session.commit()
            return row.id

    def test_a_long_running_sync_with_a_live_executor_is_not_stale(self):
        """大仓同步跑过 30 分钟、执行者还在 → 页面**不许**把它置 failed。"""
        from services.weekly_version_files_api_helpers import should_treat_sync_task_as_stale

        seeded = _seed()
        task_id = self._task(
            seeded["project_id"], status="processing", age_seconds=4000, lease=_utc(minutes=50)
        )
        with flask_app.app_context():
            task = db.session.get(BackgroundTask, task_id)
            assert should_treat_sync_task_as_stale(task, self._now(), is_enqueued=lambda _t: False) is False

    def test_a_long_running_sync_whose_executor_died_is_still_stale(self):
        """反自检：执行者失联的那种必须照旧判陈旧（否则页面永远不解锁）。"""
        from services.weekly_version_files_api_helpers import should_treat_sync_task_as_stale

        seeded = _seed()
        task_id = self._task(
            seeded["project_id"], status="processing", age_seconds=4000, lease=_utc(minutes=-50)
        )
        with flask_app.app_context():
            task = db.session.get(BackgroundTask, task_id)
            assert should_treat_sync_task_as_stale(task, self._now(), is_enqueued=lambda _t: False) is True

    def test_a_dispatched_sync_past_the_pending_window_is_not_stale(self):
        """派发模式下那条 BackgroundTask 一直是 `pending` 且没有租约 —— 只看年龄的话，
        页面会在 5 分钟后就把一条**正在被 Agent 执行**的同步置 failed（而调度器紧接着又
        建一条，两条并发写同一份缓存）。"""
        from services.weekly_version_files_api_helpers import should_treat_sync_task_as_stale

        seeded = _seed()
        with flask_app.app_context():
            task_id = self._task(
                seeded["project_id"], status="pending", age_seconds=4000, lease=None
            )
            source = db.session.get(BackgroundTask, task_id)
            db.session.add(
                AgentTask(
                    task_type="weekly_sync",
                    project_id=seeded["project_id"],
                    source_task_id=source.id,
                    status="processing",
                    lease_expires_at=_utc(minutes=5),
                )
            )
            db.session.commit()

            assert should_treat_sync_task_as_stale(
                source, self._now(), is_enqueued=lambda _t: False
            ) is False
