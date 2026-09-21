# -*- coding: utf-8 -*-
"""同步闸门的**判据收窄**：排队等 worker 的同步不算「在跑」（2026-09-21 真机实测）。

## 缺陷形态（真机）

连续 22 次手工发起、跨度约 7 分钟，**全部**被回 `event: waiting`（`reason: sync_in_flight`），
run 数一条没涨。机制已查实：本地后台任务是**单线程**的
（`services/task_worker_service.background_task_worker` 只有一个线程），一次 AI 分析就把
它占满，调度器每 2 分钟补进来的 `weekly_sync` 只能停在 `pending`。证据精确到毫秒：
任务 381 在 `12:52:30.079` 才开始，而 run 15 结束于 `12:52:29.994`（晚 0.085 秒）；
382 又在 381 完成后 0.008 秒接上。

也就是说：**分析期间缓存并没有在被写**（worker 根本没空去写），但旧判据把 `pending`
也算作「在跑」，于是闸门在这段最不需要拦的时间里一直拦着。

## 收窄后的判据（两种算「在跑」）

1. `processing` —— 真正在往缓存里写，**一律拦**（这是这道闸门存在的理由，没有放松）；
2. `pending` **且执行侧空着** —— 它会在 worker 下一次取任务时立刻开跑，**拦**；
3. `pending` **而执行侧正忙** —— 不算：那条 pending 在整个分析期间都不会开始写缓存。

## 这一组守什么

* 收窄**真的生效**（忙的时候放行）—— 三条：`weekly_sync_in_flight` 本身、后台入口
  不再被它跳过、手工流不再被它拦下；
* 收窄**没有过头** —— `processing`、兄弟仓库的同步、30 分钟上限、以及「worker 空着时
  pending 照样拦」这四条一条都不许丢；
* 「执行侧忙」的判据是 `processing` **且 `started_at` 有值**（只翻状态列的行不算，
  见 `weekly_sync_gate._executor_busy`）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import app as flask_app
from app import create_tables, db
from models import BackgroundTask, Project, Repository
from models.weekly_version import WeeklyVersionConfig
from services.ai.weekly_sync_gate import (
    SYNC_IN_FLIGHT_MAX_SECONDS,
    weekly_sync_in_flight,
    weekly_sync_stuck_note,
)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed(*, sync_status: str = "pending", sync_on: str = "primary", age_minutes: int = 3):
    """一个项目 + 两条同窗口的周版本配置 + 一条同步任务（挂在 primary 或兄弟配置上）。"""
    now = datetime.now(timezone.utc)
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("GS"), name="闸门判据用例")
        db.session.add(project)
        db.session.flush()
        configs = []
        for index in range(2):
            repository = Repository(
                project_id=project.id, name=_uid(f"repo{index}"), type="git",
                url="https://example.invalid/r.git", branch="main",
                resource_type="code" if index else "table",
            )
            db.session.add(repository)
            db.session.flush()
            config = WeeklyVersionConfig(
                project_id=project.id, repository_id=repository.id, name=_uid("W"),
                branch="main", start_time=now - timedelta(days=7), end_time=now,
                is_active=True, auto_sync=True, status="active",
            )
            db.session.add(config)
            configs.append(config)
        db.session.flush()

        target = configs[0] if sync_on == "primary" else configs[1]
        sync = BackgroundTask(
            task_type="weekly_sync", commit_id=str(target.id),
            priority=3, status=sync_status,
            created_at=now - timedelta(minutes=age_minutes),
        )
        db.session.add(sync)
        db.session.commit()
        return {
            "project_id": project.id,
            "config_ids": [cfg.id for cfg in configs],
            "primary_config_id": configs[0].id,
            "sibling_config_id": configs[1].id,
            "sync_task_id": sync.id,
        }


@pytest.fixture
def busy_worker():
    """让**执行侧真的忙起来**：造一条 `processing` 且 `started_at` 有值的别的任务。

    `started_at` 是这条判据的凭据（`update_task_status_with_retry` 在把任务置为
    `processing` 时写它）：只翻状态列、不写这一列的行（以及测试里手工造的行）不能证明
    worker 被占着 —— 「忙」被误判的代价是闸门再也不拦人，方向更危险。

    用完**删掉自己造的那一行**（测试库是会话级共用的）：留着它会让后面那些「执行侧空着」
    的用例看到一条永远 `processing` 的任务，于是它们要么假绿要么假红。
    """
    created: list = []

    def _make(task_type: str = "weekly_ai_analysis", *, priority: int = 6) -> int:
        with flask_app.app_context():
            row = BackgroundTask(
                task_type=task_type, commit_id=str(uuid.uuid4().int % 100000),
                priority=priority, status="processing",
                created_at=datetime.now(timezone.utc),
                started_at=datetime.now(timezone.utc),
            )
            db.session.add(row)
            db.session.commit()
            created.append(row.id)
            return row.id

    yield _make

    with flask_app.app_context():
        for task_id in created:
            row = db.session.get(BackgroundTask, task_id)
            if row is not None:
                db.session.delete(row)
        db.session.commit()


@pytest.fixture
def idle_worker():
    """确保「执行侧空着」这个前提**真的成立**（库里可能有前面用例留下的行）。

    只收尾那些带 `started_at` 的残留（= 别的用例造出来的「忙」），不动只有状态列的行：
    后者是既有用例的构造方式，收掉它们会把那些用例的前提弄没了。
    """
    with flask_app.app_context():
        leftovers = BackgroundTask.query.filter(
            BackgroundTask.status == "processing",
            BackgroundTask.started_at.isnot(None),
        ).all()
        for row in leftovers:
            row.status = "completed"
            row.started_at = None
        if leftovers:
            db.session.commit()


# ==========================================================================
#  一、收窄真的生效：执行侧忙时，排队等 worker 的同步不再拦
# ==========================================================================


def test_a_pending_sync_behind_a_busy_worker_does_not_block(busy_worker):
    """**真机实测的那一条**：一条 AI 分析占住唯一那个 worker 时，手工分析必须放行。

    旧判据在这里回一句「周版本同步还在跑（已 N 分钟）」—— 而缓存一个字都没被写
    （worker 正忙着分析），于是这句话在整个分析期间一直成立：用户点 22 次、22 次被拦。
    """
    seeded = _seed(sync_status="pending", age_minutes=3)
    busy_worker("weekly_ai_analysis")
    with flask_app.app_context():
        assert weekly_sync_in_flight(seeded["config_ids"]) == "", (
            "执行侧正忙（缓存没有被写），排队等它的同步却把分析拦住了 —— 正是实测的 "
            "22/22 被回 sync_in_flight 的那个形态"
        )


def test_a_pending_sync_behind_a_busy_worker_gets_no_stuck_note(busy_worker):
    """同一情形下也不该冒出一句「卡死」的日志：它没卡死，只是在排队。"""
    seeded = _seed(sync_status="pending", age_minutes=(SYNC_IN_FLIGHT_MAX_SECONDS // 60) + 5)
    busy_worker("weekly_ai_analysis")
    with flask_app.app_context():
        assert weekly_sync_stuck_note(seeded["config_ids"]) == ""


def test_the_background_entry_is_no_longer_skipped_for_a_pending_sync(busy_worker):
    """后台路径同样不再被这条 pending 拦住（同一道闸、同一判据）。

    走到底会停在「没配 API key」这类既有闸门上 —— 那正好说明它越过了同步这一道。
    **没有 API key 也是这条用例的自保**：它保证这次不会真的发出模型请求（不花钱）。
    """
    from services.ai_analysis_service import run_weekly_analysis_background

    seeded = _seed(sync_status="pending", age_minutes=3)
    busy_worker("weekly_ai_analysis")
    with flask_app.app_context():
        outcome = run_weekly_analysis_background(seeded["primary_config_id"])

    assert outcome.get("reason") != "sync_in_flight", (
        f"自动分析被一条排队中的同步挡住了（缓存没有被写）：{outcome}"
    )


# ==========================================================================
#  二、收窄没有过头：这四条一条都不许丢
# ==========================================================================


def test_a_processing_sync_still_blocks_even_though_the_worker_is_busy(busy_worker):
    """真正在往缓存里写的那一条**照旧拦**（哪怕 worker 手上还有别的任务）。

    这是本模块存在的理由：跑到一半的同步写进缓存的只有已写好的那批文件，变更清单会
    **静默**变小（「代码改动均未取到 diff 正文」）。收窄判据时这一条绝不能跟着松动。
    """
    seeded = _seed(sync_status="processing", age_minutes=2)
    busy_worker("weekly_ai_analysis")
    with flask_app.app_context():
        reason = weekly_sync_in_flight(seeded["config_ids"])

    assert reason, "同步正在写缓存，却放行了分析"
    assert str(seeded["sync_task_id"]) in reason


def test_the_sibling_repository_processing_sync_still_blocks(busy_worker):
    """闸门按**整批**判这条不许丢：入口是 primary，而同步挂在兄弟配置上。

    只看自己那一个仓库的实现在这条用例下会放行，而那时变更清单正缺着兄弟仓库那半份文件。
    """
    seeded = _seed(sync_status="processing", sync_on="sibling", age_minutes=2)
    busy_worker("weekly_ai_analysis")
    with flask_app.app_context():
        assert weekly_sync_in_flight(seeded["config_ids"]), (
            "兄弟仓库的同步正在写缓存，却放行了分析（变更清单会缺那半份文件）"
        )


def test_a_pending_sync_with_a_free_worker_still_blocks(idle_worker):
    """**执行侧空着时 pending 照样拦**：它下一次取任务就会开跑（队列是内存队列）。

    反面（收窄过头）的表现是：闸门形同虚设，分析照样在一份还会变的缓存上开跑。
    """
    seeded = _seed(sync_status="pending", age_minutes=3)
    with flask_app.app_context():
        reason = weekly_sync_in_flight(seeded["config_ids"])

    assert reason, "worker 空着、这条同步马上就会开跑，却放行了分析"
    assert str(seeded["sync_task_id"]) in reason
    assert "3 分钟" in reason, reason


def test_the_age_limit_still_releases_a_wedged_processing_sync(busy_worker):
    """30 分钟上限那条既有设计不许丢：卡死的同步不能把分析永久挡住。

    它走的是 `processing` 那一支（真正在写却永远写不完），与判据收窄是两件事。
    """
    seeded = _seed(
        sync_status="processing", age_minutes=(SYNC_IN_FLIGHT_MAX_SECONDS // 60) + 5
    )
    busy_worker("weekly_ai_analysis")
    with flask_app.app_context():
        assert weekly_sync_in_flight(seeded["config_ids"]) == "", "卡死的同步把分析永久挡住了"
        note = weekly_sync_stuck_note(seeded["config_ids"])

    assert note and "不再拦" in note, note
    assert "不完整" in note, f"放行时没说清代价：{note}"


def test_only_a_started_task_counts_as_a_busy_worker(idle_worker):
    """「忙」的判据是 `processing` **且 `started_at` 有值**。

    只翻状态列、不写 `started_at` 的行不能证明 worker 被占着（真正取走任务的是
    `update_task_status_with_retry`，它会写这一列）。少了这个限定，一行永久卡在
    `processing` 的残留会把判据钉死在「忙」上 —— 闸门于是再也不会在「马上要开跑」时拦人，
    而这是**静默**的。
    """
    seeded = _seed(sync_status="pending", age_minutes=3)
    with flask_app.app_context():
        stale = BackgroundTask(
            task_type="auto_sync", commit_id="0", priority=5, status="processing",
            created_at=datetime.now(timezone.utc), started_at=None,
        )
        db.session.add(stale)
        db.session.commit()
        try:
            reason = weekly_sync_in_flight(seeded["config_ids"])
        finally:
            db.session.delete(stale)
            db.session.commit()

    assert reason, "一行没有 started_at 的 processing 残留把闸门永久关掉了"
