# -*- coding: utf-8 -*-
"""同步闸门判据的**两次反转**：`pending` 现在一律算「在跑」（2026-09-22 线程拆分后）。

## 这段历史必须留着，否则下一个人会再收窄一次

**2026-09-21 的收窄**（真机实测）：连续 22 次手工发起、跨度约 7 分钟，**全部**被回
`event: waiting`（`reason: sync_in_flight`），run 数一条没涨。机制查实：本地后台任务当时是
**单线程**的（`background_task_worker` 只有一个线程），一次 AI 分析就把它占满，调度器每
2 分钟补进来的 `weekly_sync` 只能停在 `pending`。证据精确到毫秒：任务 381 在
`12:52:30.079` 才开始，而 run 15 结束于 `12:52:29.994`（晚 0.085 秒）。既然分析期间
**缓存一个字都没被写**，把 `pending` 也算作「在跑」就是白拦 —— 于是加了
`_executor_busy()`：**执行侧忙时，pending 不算在跑**。

**2026-09-22 的反转**：`62b4746` 把队列拆成两条 —— `weekly_ai_analysis` 进
`ai_task_queue`、由**独立的 `ai_task_worker` 线程**执行
（`services/task_worker_service.py:984-987`，投递侧 `task_worker_queue_service._analysis_task_queue`）。
那次收窄赖以成立的前提就此消失：

* 分析跑在 **AI 线程**上，**通用线程是空的**；
* 那条 `pending` 的 `weekly_sync` 下一秒就会在**通用线程**上开跑、开始往缓存里写。

此时再放行，分析就是在一份**写了一半**的缓存上开跑 —— 变更清单静默缺文件，正是本模块
存在的唯一理由。所以判据回到「`pending` 与 `processing` **一律**算在跑」。

> **判据的取向是不对称的**：误拦的代价是分析晚几分钟（有 30 分钟上限兜底、下一周期重试、
> 而且同步**真的会跑完**，不像单线程时代会一直挂着）；误放的代价是一份**看起来完整、
> 实则缺文件**的风险报告。后者是静默的，所以要往拦的那边倒。

## 这一组守什么

* `pending` 被放行的那三条**已经反转**：AI 分析在跑时照样拦、后台入口照样被跳过、
  并且**任何别的任务在跑都不构成放行理由**（`_executor_busy` 已删除）；
* 不许过头的那几条一条都不许丢：`processing`、兄弟仓库的同步、30 分钟上限、
  以及上限之外的**任何一条**都不能成为放行依据（只要**还有一条**在上限内就继续拦）。
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
from services.ai_analysis_service import update_project_analysis_config


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
        # **显式打开自动分析开关。** 默认值是**关**（2026-09-22 起），而本文件测的是
        # **同步闸门**的判据 —— 前提是这次分析本来会被放行到闸门那一步。不声明这个前提，
        # 它们会全部停在「开关关闭」那道更靠前的闸后面，报出来的失败是「闸门没拦住」。
        # 走 `update_project_analysis_config`（用户点开关走的就是这条），不 monkeypatch。
        ok, message, errors = update_project_analysis_config(
            project.id, {"auto_weekly_enabled": True}, updated_by="tester"
        )
        assert ok, f"开关没打开，这组用例的前提就不成立：{message} {errors}"
        return {
            "project_id": project.id,
            "config_ids": [cfg.id for cfg in configs],
            "primary_config_id": configs[0].id,
            "sibling_config_id": configs[1].id,
            "sync_task_id": sync.id,
        }


@pytest.fixture
def busy_worker():
    """造一条 `processing` 且 `started_at` 有值的 **AI 分析**任务 = 「AI 线程正被占住」。

    线程拆分之后，这条行描述的是**另一条线程**上的忙（`weekly_ai_analysis` 由
    `ai_task_worker` 执行），通用线程仍然空着 —— 所以它**不再**是放行 `pending` 同步的
    理由。留着这个 fixture 是为了让「AI 分析正在跑」这个场景在用例里**显式可见**，
    而不是靠读者自己脑补。

    用完**删掉自己造的那一行**（测试库是会话级共用的）：留着它会让后面那些用例看到一条
    永远 `processing` 的任务，于是它们要么假绿要么假红。
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
#  一、线程拆分之后：`pending` 一律算在跑（2026-09-21 的放行已反转）
# ==========================================================================


def test_a_pending_sync_behind_a_running_analysis_still_blocks(busy_worker):
    """**本次回归**：一次 AI 分析正在 AI 线程上跑时，排队中的同步照样拦。

    2026-09-21 那条收窄在这里断言的是 `""`（放行），前提是「后台只有一个 worker 线程、
    分析把它占满、pending 的同步动不了、缓存没有被写」。`62b4746` 之后前提没了：分析在
    `ai_task_worker` 上，**通用线程是空的**，那条 pending 下一秒就在通用线程上开跑并开始
    写缓存。这时放行的后果是分析在一份写了一半的缓存上开跑 —— 变更清单静默缺文件。
    """
    seeded = _seed(sync_status="pending", age_minutes=3)
    busy_worker("weekly_ai_analysis")
    with flask_app.app_context():
        reason = weekly_sync_in_flight(seeded["config_ids"])

    assert reason, (
        "AI 分析跑在 AI 线程上、通用线程空着 —— 那条 pending 的同步马上就会开始写缓存，"
        "却放行了分析（这正是线程拆分引入的回归）"
    )
    assert str(seeded["sync_task_id"]) in reason, reason


def test_the_background_entry_is_skipped_for_a_pending_sync(busy_worker):
    """后台路径同样被这条 pending 拦住（同一道闸、同一判据）。

    走到底会停在「没配 API key」这类既有闸门上；这条用例自保的地方在于**它必须停在同步
    这一道**，否则就会真的发出模型请求（花钱）。
    """
    from services.ai_analysis_service import run_weekly_analysis_background

    seeded = _seed(sync_status="pending", age_minutes=3)
    busy_worker("weekly_ai_analysis")
    with flask_app.app_context():
        outcome = run_weekly_analysis_background(seeded["primary_config_id"])

    assert outcome.get("reason") == "sync_in_flight", (
        f"一条马上要写缓存的同步没能挡住自动分析：{outcome}"
    )


def test_another_generic_task_running_does_not_open_the_gate(idle_worker):
    """**任何别的任务在跑都不是放行理由** —— `_executor_busy` 已按线程拆分前的假设删除。

    通用线程上正跑着一条 `auto_sync`（`processing` + `started_at`）：它一结束，线程就交给
    队列里那条 pending 的 `weekly_sync`，而它可能在分析中途结束。所以「通用线程现在忙」
    同样不能证明「缓存不会被写」。
    """
    seeded = _seed(sync_status="pending", age_minutes=3)
    with flask_app.app_context():
        other = BackgroundTask(
            task_type="auto_sync", commit_id="0", priority=5, status="processing",
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
        )
        db.session.add(other)
        db.session.commit()
        try:
            reason = weekly_sync_in_flight(seeded["config_ids"])
        finally:
            db.session.delete(other)
            db.session.commit()

    assert reason, "另一条通用任务在跑被当成了「缓存不会被写」的理由，于是放行了分析"


def test_a_pending_sync_wedged_past_the_cap_still_gets_a_stuck_note(busy_worker):
    """排队超过上限的 pending 是真**卡住**了（worker 一直没取走它），该报出来。

    上限的语义没变：超过 `SYNC_IN_FLIGHT_MAX_SECONDS` 就不再拦（放行 + 一条醒目日志）。
    线程拆分之后 pending 也会走到这一支 —— 一条永远排不上的同步同样是病。
    """
    seeded = _seed(sync_status="pending", age_minutes=(SYNC_IN_FLIGHT_MAX_SECONDS // 60) + 5)
    busy_worker("weekly_ai_analysis")
    with flask_app.app_context():
        assert weekly_sync_in_flight(seeded["config_ids"]) == ""
        note = weekly_sync_stuck_note(seeded["config_ids"])

    assert note and "不再拦" in note, note
    assert "不完整" in note, f"放行时没说清代价：{note}"


# ==========================================================================
#  二、不许过头：这几条一条都不许丢
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


def test_a_wedged_sync_does_not_release_while_a_fresh_one_is_still_writing(idle_worker):
    """**上限的判据是「还有没有一条在上限内」，不是「有没有最老的一条超了上限」。**

    闸门按**整批**判（`group_config_ids`：同项目、同窗口的全部配置）。一批里可以同时有一条
    卡死 35 分钟的旧同步与一条刚开跑 2 分钟的新同步。旧实现「`processing` 优先」会拿那条
    35 分钟的算 age → 超过上限 → **放行** —— 而另一条正在往缓存里写，变更清单照样缺文件。
    上限的用途是「一条卡死的同步不能把分析永久挡住」，不是「只要有一条老的就可放行」。
    """
    wedged = _seed(sync_status="processing", age_minutes=(SYNC_IN_FLIGHT_MAX_SECONDS // 60) + 5)
    fresh = _seed(sync_status="pending", age_minutes=2)
    # 两批同窗口的配置各自成一组；这里把它们的 config_ids 合起来 = 一个批次里两条同步。
    batch = list(wedged["config_ids"]) + list(fresh["config_ids"])
    with flask_app.app_context():
        reason = weekly_sync_in_flight(batch)

    assert reason, "同批里还有一条刚开跑的同步在写缓存，却因为另一条卡死了就放行"
    assert str(fresh["sync_task_id"]) in reason, (
        f"该拦住分析的是**还在上限内**的那一条，报出来的却是别的：{reason}"
    )
