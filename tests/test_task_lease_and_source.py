# -*- coding: utf-8 -*-
"""队列所有权、平台侧租约与触发来源（复测文档 AI-P0-04 的前半）。

## 这一组守的是实测那一幕

手工等待意图 **477** 登记时，库里已经有一条**更早创建的 scheduled** 分析任务 **468**：

* `_wake_one_waiting_intent` 看到同组已有 pending 任务就 `return "skipped"` ——
  不升级来源、不建立关联、意图继续 pending；
* `create_weekly_ai_analysis_task` 去重命中时，单机模式下**什么都不做**（只打一行日志
  就 return），agent 模式下命中已有 AgentTask 也直接 `return False`（连载荷都不更新）；
* 执行侧 `trigger_source` 的**唯一来源是内存载荷** —— 而复用的那条任务载荷里没有它。

于是：队列复用了 468，run 20 落库 `trigger_source='scheduled'`，用户从点击到模型开始
隔了 **13 分 53 秒**，账单上写着「系统自己跑的」。而这三种形态互相掩护 —— 修掉任意
一个，另外两个照样能把账记错。

## 变红意味着什么（逐类）

* `TestTheSourceIsPersistedOnTheTaskRow` / `TestTheManualRequestAttaches…`：
  来源又只活在内存载荷里 —— 去重命中一条更早的任务时，用户点出来的那次又被记成「定时」；
* `TestAWaitedIntentAlwaysLandsSomewhere`：意图又能悬在 pending 上（不转交、不作废、
  也不建立关联），而页面会一直说「已登记，等着」；
* `TestThePlatformSideLease`：租约判据没了 —— 要么双 worker 把同一条任务跑两遍，
  要么一台机器死掉之后那条 `processing` 永远没人收，要么启动恢复把别人正拿着的任务抢走；
* `TestAManualAnalysisIsNotStarvedBySyncing`：优先级又回到「AI 排在两种同步之后」。
"""
from __future__ import annotations

# ruff: noqa: I001 —— 本文件的**导入顺序是语义要求**（见下面那段注释）：
# `services.task_worker_service` 必须排在 `services.task_worker_queue_service` 之前，
# 否则会在队列服务只加载了一半时去取它的名字（环形依赖）→ ImportError。
# isort 要的字母序恰好与这个要求相反，所以这里整文件放行 I001。

import queue
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.task_worker_priority as priority
# **导入顺序有讲究**：`task_worker_service` 必须在 `task_worker_queue_service` **之前**
# 导入。两者互为环形依赖，而 worker 那一侧是按名字 `from ... import` 取的 —— 反过来先导入
# 队列服务时，worker 会在队列服务**只加载了一半**的时刻去取那些名字，直接 ImportError。
# （这是既有的脆弱点，本文件不修它，只按既有顺序来：`tests/test_sync_wait_cadence.py`
# 等文件也都是先导 worker。）
import services.task_worker_service as worker  # noqa: I001 —— 顺序是语义要求
import services.task_worker_queue_service as queue_service
import services.task_worker_weekly_handlers as weekly_handlers
from app import app as flask_app
from app import create_tables, db
from models import BackgroundTask, Project, Repository
from models.ai_analysis import AiAnalysisRun
from models.weekly_version import WeeklyVersionConfig
from services.ai_analysis_service import build_weekly_group_key

# 测试库是会话级共用的（conftest 只守 IO，没有逐用例重置），造出来的东西必须自己收干净。
_CREATED_TASK_IDS: list = []
_CREATED_CONFIG_IDS: list = []
_CREATED_GROUP_KEYS: list = []


@pytest.fixture(autouse=True)
def _cleanup_rows_this_file_creates():
    # 续租名单是**进程级**的模块状态：别的文件（或本文件前一个用例）留下的条目会被
    # 算进 `renew_inflight_task_leases` 的计数里 —— 那正是「全量绿、子集红」的形态。
    # 进出各清一次：只有本用例自己登记的东西能影响本用例的断言。
    # 「已入队」账本同理（它是 `weekly_handlers` 的模块级集合，别的文件也会往里登记）。
    queue_service._inflight_leases.clear()
    weekly_handlers._enqueued_weekly_ai_task_ids.clear()
    yield
    weekly_handlers._enqueued_weekly_ai_task_ids.clear()
    queue_service._inflight_leases.clear()
    with flask_app.app_context():
        if _CREATED_TASK_IDS:
            BackgroundTask.query.filter(
                BackgroundTask.id.in_(list(_CREATED_TASK_IDS))
            ).delete(synchronize_session=False)
            _CREATED_TASK_IDS.clear()
        for group_key in _CREATED_GROUP_KEYS:
            AiAnalysisRun.query.filter(AiAnalysisRun.target_key == group_key).delete(
                synchronize_session=False
            )
        _CREATED_GROUP_KEYS.clear()
        if _CREATED_CONFIG_IDS:
            # 别留下活跃配置：调度器会为**库里所有**活跃配置建任务。
            WeeklyVersionConfig.query.filter(
                WeeklyVersionConfig.id.in_(list(_CREATED_CONFIG_IDS))
            ).update({"is_active": False}, synchronize_session=False)
            _CREATED_CONFIG_IDS.clear()
        db.session.commit()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed():
    """一个项目 + 两条同窗口的周版本配置（模拟多仓库分组）。"""
    now = datetime.now(timezone.utc)
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("LS"), name="租约与来源用例")
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
        db.session.commit()
        group_key = build_weekly_group_key(configs[0])
        _CREATED_CONFIG_IDS.extend(cfg.id for cfg in configs)
        _CREATED_GROUP_KEYS.append(group_key)
        return {
            "project_id": project.id,
            "config_ids": [cfg.id for cfg in configs],
            "config_id": configs[0].id,
            "group_key": group_key,
        }


def _track(task_row):
    """登记「这一行是本文件造出来的」，收尾时删掉。

    **必须在 commit 之后调**：`id` 还没分配的行追不到，会静默留在共用测试库里 ——
    而留下的行是**真数据**：一条 `created_at` 三小时前的 pending 任务会让
    `_starvation_yield_note` 认为「有人等太久了」，于是**别的文件**的调度器用例开始让路
    （实测：本文件的账本用例留下三行之后，`test_weekly_sync_dedup_blocks_starvation.py`
    的两条立刻变红，而单跑那个文件是绿的）。
    """
    if task_row is None or getattr(task_row, "id", None) is None:
        raise AssertionError(
            "登记清理时这一行还没有 id（没 commit）—— 它会留在共用测试库里污染别的用例"
        )
    if task_row.id not in _CREATED_TASK_IDS:
        _CREATED_TASK_IDS.append(task_row.id)
    return task_row


def _add_task_row(**fields):
    """造一行后台任务、落库、并登记清理（顺序不能反，见 `_track`）。"""
    row = BackgroundTask(**fields)
    db.session.add(row)
    db.session.commit()
    return _track(row)


def _active_analysis_tasks(group_key):
    with flask_app.app_context():
        rows = BackgroundTask.query.filter(
            BackgroundTask.task_type == "weekly_ai_analysis",
            BackgroundTask.file_path == group_key,
            BackgroundTask.status.in_(["pending", "processing"]),
        ).order_by(BackgroundTask.id.asc()).all()
        for row in rows:
            if row.id not in _CREATED_TASK_IDS:
                _CREATED_TASK_IDS.append(row.id)
        return rows


def _pending_intents(group_key):
    with flask_app.app_context():
        rows = BackgroundTask.query.filter_by(
            task_type="weekly_ai_waiting", file_path=group_key, status="pending"
        ).all()
        for row in rows:
            if row.id not in _CREATED_TASK_IDS:
                _CREATED_TASK_IDS.append(row.id)
        return rows


def _row(task_id):
    with flask_app.app_context():
        db.session.expire_all()
        return db.session.get(BackgroundTask, task_id)


def _single_mode(monkeypatch, recorder=None):
    """单机模式 + 可选的「内存队列」替身（真实入队会污染全局队列）。"""
    monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: False)
    if recorder is not None:
        monkeypatch.setattr(worker, "background_task_queue", recorder)
    return recorder


def _code_only(func):
    """函数源码**剥掉注释与字符串**之后的样子（排版保持原样）。

    静态断言必须先剥：注释里会原样引用「要被禁掉的写法」（本文件的注释就在解释
    `is_stale_sync_task` 与 `priority=3` 这些字面量），不剥就会假红；
    反过来，字符串（含 docstring）里也可能藏着要断言的内容，一起剥掉才是「只看代码」。
    按位置挖空而不是把 token 重新拼起来 —— 后者会把 `foo()` 拼成 `foo ( )`，
    断言就得写成排版敏感的形式。
    """
    import inspect
    import io
    import tokenize

    source = inspect.getsource(func)
    lines = source.splitlines(keepends=True)
    spans = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        start_row, start_col = token.start
        end_row, end_col = token.end
        if start_row == end_row:
            spans.append((start_row, start_col, end_col))
            continue
        spans.append((start_row, start_col, len(lines[start_row - 1]) - 1))
        for row in range(start_row + 1, end_row):
            spans.append((row, 0, len(lines[row - 1]) - 1))
        spans.append((end_row, 0, end_col))
    for row, start_col, end_col in spans:
        line = lines[row - 1]
        if start_col >= end_col:
            continue
        lines[row - 1] = line[:start_col] + " " * (end_col - start_col) + line[end_col:]
    return "".join(lines)


class _QueueRecorder:
    """只记录载荷的假队列（没有堆、没有锁 —— 与既有用例同一形态）。"""

    def __init__(self):
        self.payloads = []

    def put(self, wrapper):
        self.payloads.append(getattr(wrapper, "task_data", None))

    def qsize(self):
        return len(self.payloads)


def _ordered_memory_queue(monkeypatch):
    """**真的** `PriorityQueue` + 真的 `TaskWrapper`（测优先级排序必须用它）。"""
    real_queue = queue.PriorityQueue()
    monkeypatch.setattr(worker, "background_task_queue", real_queue)
    monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: False)
    return real_queue


# ==========================================================================
#  一、优先级与 aging（纯函数 + 真队列）
# ==========================================================================


class TestThePriorityTableItself:
    def test_a_manual_analysis_is_not_queued_behind_either_sync(self):
        """**这一条就是「AI 被同步饿死」的解药。**

        实测：同步结束之后又连续跑了 3 个 `auto_sync` 才开始 AI；用户点击到模型开始
        隔了 13 分 53 秒。数值小者优先，所以手工 AI 必须**小于**两种同步。
        """
        assert priority.WEEKLY_AI_MANUAL < priority.WEEKLY_SYNC, (
            "手工 AI 又排到周版本同步后面了 —— 用户点的那次会被同步的一轮轮补给饿死"
        )
        assert priority.WEEKLY_AI_MANUAL < priority.AUTO_SYNC, (
            "手工 AI 又排到 auto_sync 后面了 —— 实测就是被它连挡了 3 条"
        )
        assert priority.WEEKLY_AI_MANUAL < priority.WEEKLY_EXCEL_CACHE, (
            "手工 AI 又排到周版本缓存重建后面了"
        )
        assert priority.WEEKLY_AI_MANUAL < priority.WEEKLY_AI_ANALYSIS, (
            "手工与定时的 AI 分析同价了 —— 用户点的那次就不再有意义"
        )

    def test_the_page_driven_excel_diff_is_still_the_very_first(self):
        """页面驱动的 excel_diff（用户正等着看）仍是最高优先级，没被这次改动碰掉。"""
        everything = [
            priority.EXCEL_DIFF_PAGE,
            priority.WEEKLY_AI_MANUAL,
            priority.AGENT_COMMIT_DIFF,
            priority.AUTO_SYNC_FORCE_RETRY,
            priority.WEEKLY_SYNC,
            priority.COMMIT_DIFF,
            priority.AGENT_FILE_CONTENT,
            priority.AUTO_SYNC,
            priority.WEEKLY_EXCEL_CACHE,
            priority.WEEKLY_AI_ANALYSIS,
            priority.WAITING_INTENT,
            priority.BRANCH_REFRESH,
            priority.EXCEL_DIFF_DEFAULT,
            priority.MAINTENANCE_REBUILD,
            priority.CLEANUP_CACHE,
        ]
        assert min(everything) == priority.EXCEL_DIFF_PAGE

    def test_the_semantics_is_written_down_where_the_numbers_live(self):
        """「数值越小越优先」必须写在常量的家里（读的人第一眼就要看到）。"""
        import inspect

        source = inspect.getsource(priority)
        assert "数值越小越优先" in source, (
            "单一来源模块没有把语义写下来 —— 下一个人只能靠猜"
        )


class TestAging:
    def test_it_is_bounded_and_never_reaches_a_user_action(self):
        """aging **有界**：挂了两小时的维护任务也不许爬到用户动作前面。"""
        assert priority.aging_steps(10 ** 6) == priority.AGING_MAX_STEPS
        for base in (
            priority.WEEKLY_AI_ANALYSIS,
            priority.AUTO_SYNC,
            priority.WEEKLY_EXCEL_CACHE,
            priority.BRANCH_REFRESH,
            priority.MAINTENANCE_REBUILD,
            priority.CLEANUP_CACHE,
        ):
            aged = priority.effective_priority(base, 10 ** 6)
            assert aged >= priority.AGING_FLOOR, (
                f"优先级 {base} 等再久也被抬到了 {aged}（低于下限 "
                f"{priority.AGING_FLOOR}）—— 同步会被饿死"
            )
            assert aged > priority.WEEKLY_AI_MANUAL, (
                f"优先级 {base} 等久了会越过用户手工点的那一次"
            )

    def test_a_user_action_is_never_pushed_back(self):
        """aging 只向上提价，不许把用户动作**降**到后台任务那一档。"""
        assert priority.effective_priority(priority.EXCEL_DIFF_PAGE, 10 ** 6) == (
            priority.EXCEL_DIFF_PAGE
        )
        assert priority.effective_priority(priority.WEEKLY_AI_MANUAL, 10 ** 6) == (
            priority.WEEKLY_AI_MANUAL
        )

    def test_a_long_waiting_analysis_beats_a_freshly_created_sync(self):
        """**aging 的意义**：老任务压过新任务，队列才会被消化而不是被不断插队。

        真队列 + 真 `TaskWrapper`：先放一条 p6 的定时 AI（已等 15 分钟），再放一条刚建的
        同步（p3）。按原始优先级同步在前；重算过后 AI 必须被先取走。
        """
        real_queue = queue.PriorityQueue()
        aged_ai = worker.TaskWrapper(
            priority.WEEKLY_AI_ANALYSIS, 1, {"type": "weekly_ai_analysis", "task_id": 1}
        )
        aged_ai.enqueued_at = 1000.0
        fresh_sync = worker.TaskWrapper(
            priority.WEEKLY_SYNC, 2, {"type": "weekly_sync", "task_id": 2}
        )
        fresh_sync.enqueued_at = 1000.0 + 15 * 60
        real_queue.put(aged_ai)
        real_queue.put(fresh_sync)

        changed = priority.reweigh_queue(real_queue, now=1000.0 + 15 * 60)

        assert changed == 1, f"aging 没有生效（改了 {changed} 项）"
        first = real_queue.get()
        assert first.task_data["task_id"] == 1, (
            "等了 15 分钟的分析还是排在刚建的同步后面 —— 饿死循环原样回来了"
        )
        assert first.priority == priority.AGING_FLOOR, first.priority

    def test_a_fresh_sync_still_keeps_its_place_above_the_lower_tiers(self):
        """反向保险：aging 不许把**新**同步也一起提价（那会让它越过所有东西）。"""
        assert priority.effective_priority(priority.WEEKLY_SYNC, 0) == priority.WEEKLY_SYNC
        assert priority.effective_priority(priority.AUTO_SYNC, 0) == priority.AUTO_SYNC

    def test_aging_does_not_compound_on_each_pass(self):
        """反复重算不许一路往下加（`base_priority` 就是为此留的）。"""
        real_queue = queue.PriorityQueue()
        wrapper = worker.TaskWrapper(priority.CLEANUP_CACHE, 1, {"task_id": 5})
        wrapper.enqueued_at = 1000.0
        real_queue.put(wrapper)

        for _ in range(5):
            priority.reweigh_queue(real_queue, now=1000.0 + 10 ** 6)

        assert wrapper.priority == priority.effective_priority(
            priority.CLEANUP_CACHE, 10 ** 6
        ), f"优先级被反复叠加成了 {wrapper.priority}"

    def test_retuning_moves_the_copy_that_is_already_in_the_queue(self):
        """去重命中/升级成手工时，**内存队列里那一份**也要跟着改。

        只改库里那一行，worker 取到的还是旧的那一份 —— 库里写着「手工优先」、
        实际却排在最后，日志与行为对不上。
        """
        real_queue = queue.PriorityQueue()
        queued = worker.TaskWrapper(priority.WEEKLY_AI_ANALYSIS, 1, {"task_id": 77})
        real_queue.put(queued)
        real_queue.put(worker.TaskWrapper(priority.WEEKLY_SYNC, 2, {"task_id": 88}))

        changed = priority.retune_queued_task(real_queue, 77, priority=priority.WEEKLY_AI_MANUAL)

        assert changed == 1, f"队列里那一份没有被改（改了 {changed} 项）"
        assert queued.base_priority == priority.WEEKLY_AI_MANUAL
        assert real_queue.get().task_data["task_id"] == 77, (
            "改完优先级之后它还是没有排到同步前面"
        )

    def test_retuning_an_absent_task_reports_zero(self):
        """不在队列里就返回 0（调用方据此决定要不要补一次入队）。"""
        real_queue = queue.PriorityQueue()
        assert priority.retune_queued_task(real_queue, 999, priority=1) == 0


# ==========================================================================
#  二、触发来源与请求模式**落在任务行上**
# ==========================================================================


class TestTheSourceIsPersistedOnTheTaskRow:
    def test_a_new_manual_task_records_it_on_the_row(self, monkeypatch):
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], trigger_source="manual"
            )
            row = _track(db.session.get(BackgroundTask, task_id))

        assert row.trigger_source == "manual", (
            "手工来源没有落库 —— 重启或去重命中之后它就不见了"
        )
        assert row.priority == priority.WEEKLY_AI_MANUAL

    def test_a_scheduled_task_records_scheduled(self, monkeypatch):
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            row = _track(db.session.get(BackgroundTask, task_id))

        assert row.trigger_source == "scheduled", row.trigger_source

    def test_the_execution_side_reads_the_row_not_the_payload(self, monkeypatch):
        """**「去重复用旧任务」不会再记成 scheduled 的那一跳。**

        载荷说是 scheduled（那是复用来的那条任务的旧载荷），行上写着 manual（附着时改的）
        —— 执行侧必须听行的。
        """
        import services.task_worker_task_handlers as task_handlers

        seeded = _seed()
        seen = {}
        with flask_app.app_context():
            row = _add_task_row(
        task_type="weekly_ai_analysis", commit_id=str(seeded["config_id"]),
        file_path=seeded["group_key"], priority=priority.WEEKLY_AI_MANUAL,
        status="pending", trigger_source="manual",
        )
            monkeypatch.setattr(
                worker, "run_weekly_analysis_background",
                lambda config_id, task_id=None, trigger_source="scheduled": (
                    seen.update(trigger_source=trigger_source) or {"status": "succeeded"}
                ),
            )
            monkeypatch.setattr(worker, "update_task_status_with_retry", lambda *a, **k: None)
            worker._handle_weekly_ai_analysis_task(
                {"type": "weekly_ai_analysis", "config_id": seeded["config_id"],
                 "task_id": row.id, "trigger_source": "scheduled"}
            )

        assert seen.get("trigger_source") == "manual", (
            "执行侧又只看载荷了 —— 用户点出来的那次会记成「定时」"
        )
        assert task_handlers.trigger_source_for_task is not None  # 接线守卫

    def test_a_manual_payload_still_upgrades_a_row_without_a_source(self, monkeypatch):
        """老行（列上还是空的）＋载荷说 manual → 记 manual（不许把用户的那次降级）。"""
        from services.task_worker_task_handlers import trigger_source_for_task

        seeded = _seed()
        with flask_app.app_context():
            row = _add_task_row(
        task_type="weekly_ai_analysis", commit_id=str(seeded["config_id"]),
        file_path=seeded["group_key"], status="pending",
        )

            assert trigger_source_for_task(row.id, {"trigger_source": "manual"}) == "manual"
            assert trigger_source_for_task(row.id, {"trigger_source": "scheduled"}) == "scheduled"
            assert trigger_source_for_task(None, {"trigger_source": "manual"}) == "manual"
            assert trigger_source_for_task(None, None) == "scheduled"
            assert trigger_source_for_task(None, {"trigger_source": "胡说"}) == "scheduled"


# ==========================================================================
#  三、手工请求遇到旧的 scheduled 任务：确定策略（复测那一幕）
# ==========================================================================


class TestTheManualRequestAttachesToAnOlderScheduledTask:
    def test_it_upgrades_the_older_task_instead_of_abandoning_the_intent(self, monkeypatch):
        """**复测那一幕**：库里已有 scheduled 任务（468），用户点了（477）。

        修好之后：仍然只有一条任务、它被升级成 manual、来源准确、意图落终态并留下关联。
        """
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            older_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], trigger_source="scheduled"
            )
            _track(db.session.get(BackgroundTask, older_id))
            assert _row(older_id).trigger_source == "scheduled"

            intent_id = queue_service.register_waiting_analysis_intent(
                seeded["config_id"], seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, intent_id))
            outcome = queue_service.wake_waiting_analysis_intents()

            rows = _active_analysis_tasks(seeded["group_key"])
            assert len(rows) == 1, (
                f"应当只有一条分析任务（复用旧的），实际 {[(r.id, r.status) for r in rows]}"
            )
            assert rows[0].id == older_id, "没有复用那条更早创建的任务，而是另起了一条"
            assert rows[0].trigger_source == "manual", (
                "复用了旧任务但没有把它升级成 manual —— 账上又会写着「定时」"
            )
            assert rows[0].priority == priority.WEEKLY_AI_MANUAL, (
                "附着之后没有把优先级抬到手工那一档 —— 它仍会排在同步后面"
            )
            # **显式关联**：任务行上写着「这次分析是谁要的」，反着也能查到那条任务。
            # 第二波 P0-01 之后这一列的冻结语义是「服务于哪个 ai_analysis_job」；
            # 没有 job 的路径（本用例）退回意图行 id，让「谁要的」仍是一次查询的事。
            assert rows[0].job_id == intent_id, rows[0].job_id
            handed = queue_service._handed_off_task_of_intent(_row(intent_id))
            assert handed is not None and handed.id == older_id, (
                "转交出去的那条任务反着查不到了 —— 意图会被当成悬空、提前作废"
            )
            # `handed_off` 是全表计数（别的文件的意图也能填满它）；「转交」这件事在这里
            # 由**我这条任务行存在且带着我这次的身份**判出来，比计数强。
            assert "skipped" not in outcome, (
                f"唤醒又走了「原地放弃」那条路：{outcome}"
            )
            # 守卫没把「在等自己那条任务」的意图误当悬空：本组这条还在 pending 且有归属
            assert _row(intent_id).status == "pending", (
                f"守卫把合法等待的意图收掉了（{outcome}）"
            )
            # 意图要么已经落终态、要么还在**等它自己那条任务**（有归属）。
            # 「pending 且没有归属」才是这次要修的那个病。
            intent = _row(intent_id)
            assert intent.status != "pending" or handed is not None, (
                f"意图停在 pending 却没有任何归属（既不转交也不作废）：{intent.status}"
            )

    def test_the_copy_already_in_the_memory_queue_is_upgraded_too(self, monkeypatch):
        """库里改了、内存队列里那一份没改 = 日志与行为对不上（它仍排在同步后面）。"""
        seeded = _seed()
        real_queue = _ordered_memory_queue(monkeypatch)
        with flask_app.app_context():
            older_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], trigger_source="scheduled"
            )
            _track(db.session.get(BackgroundTask, older_id))
            assert weekly_handlers.is_weekly_ai_task_enqueued(older_id), (
                "新建的分析任务没有登记「已入队」账本"
            )
            before = list(real_queue.queue)
            assert len(before) == 1, before

            queue_service.register_waiting_analysis_intent(
                seeded["config_id"], seeded["group_key"]
            )
            queue_service.wake_waiting_analysis_intents()

            after = list(real_queue.queue)
            assert len(after) == 1, (
                f"附着之后内存队列里多出了一份（同一份输入会跑两遍）：{after}"
            )
            assert after[0].task_data["task_id"] == older_id
            assert after[0].priority == priority.WEEKLY_AI_MANUAL, (
                "内存队列里那一份没有跟着抬价 —— 它仍排在同步后面"
            )
            assert after[0].task_data.get("trigger_source") == "manual", (
                "内存队列的载荷里没有跟上「手动」这个标记"
            )

    def test_the_handed_off_task_survives_a_restart_with_its_source(self, monkeypatch):
        """**服务重启后不丢、不重复、不改变来源。**

        装载时来源从**列**上恢复（原先靠「这一组还有等待登记」去猜）。
        """
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], trigger_source="manual"
            )
            _track(db.session.get(BackgroundTask, task_id))

            recorder = _QueueRecorder()
            monkeypatch.setattr(worker, "background_task_queue", recorder)
            monkeypatch.setattr(worker, "check_and_create_auto_sync_tasks", lambda: None)
            worker.load_pending_tasks()

        payloads = [p for p in recorder.payloads if isinstance(p, dict)]
        mine = [p for p in payloads if p.get("task_id") == task_id]
        assert len(mine) == 1, f"重启后应当恰好恢复一条，实际 {mine}"
        assert mine[0].get("type") == "weekly_ai_analysis"
        assert mine[0].get("trigger_source") == "manual", (
            f"重启后来源变了（列上有 manual 却恢复成 {mine[0].get('trigger_source')}）"
        )

    def test_a_modeless_scheduled_request_attaches_to_the_users_task(self, monkeypatch):
        """调度器（不带模式）照旧复用同一组的那条 —— 不许因为用户点过就另起一条。"""
        seeded = _seed()
        recorder = _QueueRecorder()
        _single_mode(monkeypatch, recorder)
        with flask_app.app_context():
            manual_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], trigger_source="manual"
            )
            _track(db.session.get(BackgroundTask, manual_id))
            scheduled_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )

        assert scheduled_id == manual_id, "调度器为同一组另起了一条任务（会跑两遍）"
        assert len(_active_analysis_tasks(seeded["group_key"])) == 1

    def test_a_manual_request_never_downgrades_a_manual_row(self, monkeypatch):
        """反向：调度器排的那次**不许**把手上的手工标记降回 scheduled。"""
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            manual_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], trigger_source="manual"
            )
            _track(db.session.get(BackgroundTask, manual_id))
            queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], trigger_source="scheduled"
            )

            assert _row(manual_id).trigger_source == "manual", (
                "调度器的复用把手上的「手动」降级成了「定时」"
            )


class TestADifferentRequestedModeIsNotSilentlyReused:
    """模式不同**不许静默复用**：用户点「全量重跑」却拿到一份增量结论，比慢更坏。"""

    def test_two_explicit_modes_get_two_rows(self, monkeypatch):
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            incremental_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], requested_mode="incremental"
            )
            full_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"],
                requested_mode="full", trigger_source="manual",
            )
            rows = _active_analysis_tasks(seeded["group_key"])
            modes = sorted((r.requested_mode or "") for r in rows)

        assert full_id != incremental_id, "两个不同的请求被塞进了同一条任务"
        assert len(rows) == 2, f"分别排队的两条没有各自留下，实际 {modes}"
        assert modes == ["full", "incremental"], modes

    def test_the_same_mode_attaches(self, monkeypatch):
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            first = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], requested_mode="full"
            )
            second = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], requested_mode="full"
            )
            assert second == first, "同模式重复请求没有复用"
            assert len(_active_analysis_tasks(seeded["group_key"])) == 1

    def test_an_explicit_mode_upgrades_a_modeless_task(self, monkeypatch):
        """调度器排的那条没表达过模式偏好 → 用户的要求优先，附着并升级模式。"""
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
            again = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], requested_mode="full"
            )
            row = _row(task_id)

        assert again == task_id, "无理据地另起了一条"
        assert row.requested_mode == "full", (
            "用户在等待期间表达的「要全量」在队列那一跳丢了"
        )

    def test_an_unrecognized_mode_is_recorded_as_nothing_rather_than_guessed(self, monkeypatch):
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], requested_mode="fuller"
            )
            row = _track(db.session.get(BackgroundTask, task_id))

        assert row.requested_mode is None, (
            f"认不出来的模式被原样落库了：{row.requested_mode!r}"
        )


# ==========================================================================
#  四、等待意图**必然落到某个终态**（不许永久 pending）
# ==========================================================================


class TestAWaitedIntentAlwaysLandsSomewhere:
    def test_a_missing_config_retires_the_intent_in_one_pass(self, monkeypatch):
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            intent_id = queue_service.register_waiting_analysis_intent(
                seeded["config_id"], seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, intent_id))
            # 配置在等待期间被删了
            BackgroundTask.query.filter_by(id=intent_id).update(
                {"commit_id": "999999999"}
            )
            db.session.commit()

            outcome = queue_service.wake_waiting_analysis_intents()
            row = _row(intent_id)

        # 判据是**本用例这条意图**的终态与原因；`outcome["retired"]` 是全表计数，
        # 别的文件留下的意图也能把它填满（会话级共用库）—— 不拿它当判据。
        assert row.status != "pending", f"配置都没了，意图还悬着：{row.status}（{outcome}）"
        assert row.error_message, "作废时没有写下可读的原因"

    def test_the_guard_sweeps_an_intent_that_no_pass_resolved(self, monkeypatch):
        """**「意图不许永久 pending」的守卫本身。**

        把「一条意图的处置」换成一个永远不做事、也不报错的实现（模拟某个忘了写终态的
        早退分支），守卫必须当场把它收掉 —— 只打一行日志等下一个周期不算数。
        """
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            intent_id = queue_service.register_waiting_analysis_intent(
                seeded["config_id"], seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, intent_id))
            # 它等的配置已经不存在了 → 任何处置都必须把它收掉（不是「继续等」）
            BackgroundTask.query.filter_by(id=intent_id).update({"commit_id": "999999999"})
            db.session.commit()
            monkeypatch.setattr(
                queue_service, "_wake_one_waiting_intent",
                lambda intent, now=None: "blocked",
            )

            outcome = queue_service.wake_waiting_analysis_intents()
            row = _row(intent_id)

        # 守卫有没有收到**我这条**：处置被换成了永远返回 `blocked`，所以这条从 pending
        # 变成非 pending 只可能来自守卫。全局计数（`swept`）不加这一条判据 —— 它会被
        # 别的文件留下的悬空意图填满。
        assert row.status != "pending", (
            f"守卫跑过之后意图还停在 pending —— 用户会永远等在那里（{outcome}）"
        )

    def test_an_intent_that_already_handed_off_keeps_waiting_for_its_task(self, monkeypatch):
        """转交之后意图仍然在等 —— 但它等的**是那条任务**（显式关联），不是悬空。

        这条同时守住「第二次点击不会变成第二条付费运行」：手工入口靠
        `effective_waiting_analysis_intent` 认出「已经登记过」。
        """
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            intent_id = queue_service.register_waiting_analysis_intent(
                seeded["config_id"], seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, intent_id))
            queue_service.wake_waiting_analysis_intents()
            task_rows = _active_analysis_tasks(seeded["group_key"])
            assert len(task_rows) == 1
            task_id = task_rows[0].id

            # 第二趟：任务还在排队 → 意图保持等待，不重复转交
            second = queue_service.wake_waiting_analysis_intents()
            assert len(_active_analysis_tasks(seeded["group_key"])) == 1, (
                "同一份输入被转交了两条"
            )
            assert second["blocked"] >= 1, second
            assert second["swept"] == 0, (
                f"转交出去的那种等待被守卫误当成了悬空：{second}"
            )
            assert _row(intent_id).status == "pending", "转交后意图应当继续等它那条任务"
            assert queue_service.effective_waiting_analysis_intent(
                seeded["group_key"]
            ) is not None, "已经登记过的那次不再被认出来了 —— 用户再点一次就是第二次付费"

            # 那条任务跑完了（终态）→ 意图随之落终态
            BackgroundTask.query.filter_by(id=task_id).update({"status": "completed"})
            db.session.commit()
            third = queue_service.wake_waiting_analysis_intents()

            assert _row(intent_id).status != "pending", (
                "它等的那条任务已经结束了，意图还悬着"
            )
            assert third["swept"] >= 1 or third["retired"] >= 1, third

    def test_the_page_still_says_waiting_while_the_task_is_queued(self, monkeypatch):
        """转交出去、还没轮到的那一段，页面必须继续显示「在等」而不是「可以再点」。"""
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            queue_service.register_waiting_analysis_intent(
                seeded["config_id"], seeded["group_key"]
            )
            queue_service.wake_waiting_analysis_intents()
            task_rows = _active_analysis_tasks(seeded["group_key"])
            assert len(task_rows) == 1
            # 登记已经了结（被守卫/覆盖收掉）之后，靠**显式关联**继续回答「在等」
            BackgroundTask.query.filter_by(
                task_type="weekly_ai_waiting", file_path=seeded["group_key"]
            ).update({"status": "completed"})
            db.session.commit()

            status = queue_service.describe_waiting_analysis(
                seeded["config_id"], seeded["group_key"]
            )

        assert status["waiting"] is True, (
            f"那次分析还在队列里，页面却说「已经结束了，可以再点」：{status}"
        )
        assert status["run_id"] is None, status

    def test_an_intent_without_a_live_blocker_is_never_reported_as_skipped(self, monkeypatch):
        """**`skipped` 这个出口不许回来。**

        它是这次实测的直接病灶：同组已有 pending 任务 → 原地放弃（不升级来源、不关联、
        意图继续 pending）。这里把「已有一条 scheduled 任务」这个最能触发它的形态摆出来。
        """
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            older = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], trigger_source="scheduled"
            )
            _track(db.session.get(BackgroundTask, older))
            queue_service.register_waiting_analysis_intent(
                seeded["config_id"], seeded["group_key"]
            )
            outcome = queue_service.wake_waiting_analysis_intents()

        assert "skipped" not in outcome, f"「原地放弃」那条出口回来了：{outcome}"
        # 反着查要库（`_handed_off_task_of_intent`），所以这一段在 app context 里做。
        with flask_app.app_context():
            for intent in _pending_intents(seeded["group_key"]):
                # 「有归属」的判据是**反着查得到它那条任务**（第二波 P0-01 之后，意图行的
                # `job_id` 归 job_service 用：那一列存的是它服务的 job，不再是任务 id）。
                handed = queue_service._handed_off_task_of_intent(intent)
                assert handed is not None, (
                    "意图停在 pending 却没有任何归属（既不转交、也不作废、也没关联）—— "
                    "那正是这次实测里「用户永远等在那里」的形态"
                )
                assert handed.id == older, handed.id
                assert handed.job_id == intent.id, (
                    "任务行上没有写下「这次分析是谁要的」—— 页面按自己的身份查不到它"
                )

    def test_the_outcome_keys_are_the_four_terminals_plus_the_one_live_wait(self):
        """口径钉在常量上：只有「同步在写缓存」和「已转交、任务还在队列里」两种等待。"""
        assert set(queue_service.WAITING_INTENT_OUTCOMES) == {
            "handed_off", "blocked", "retired", "expired"
        }, (
            "唤醒的结局集合变了 —— 多出来的那个键多半就是「既不转交也不作废」"
        )


# ==========================================================================
#  五、平台侧租约
# ==========================================================================


def _processing_row(**kwargs):
    """造一行 `processing` 的后台任务（带租约），并登记清理。"""
    fields = {
        "task_type": "weekly_sync",
        "commit_id": str(uuid.uuid4().int % 900_000_000 + 1),
        "priority": priority.WEEKLY_SYNC,
        "status": "processing",
    }
    fields.update(kwargs)
    row = BackgroundTask(**fields)
    db.session.add(row)
    db.session.commit()
    return _track(row)


class TestThePlatformSideLease:
    def test_taking_a_task_stamps_the_lease_and_finishing_clears_it(self, monkeypatch):
        """取走 = 起租；跑完（不论成败）= 退租。"""
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))

            worker.update_task_status_with_retry(task_id, "processing")
            taken = _row(task_id)
            assert taken.lease_expires_at is not None, "被取走却没有起租"
            assert not queue_service.lease_is_expired(taken), (
                "刚起的租约就被判成过期 —— 双 worker 会立刻把任务抢走重跑"
            )

            worker.update_task_status_with_retry(task_id, "completed")
            done = _row(task_id)
            assert done.lease_expires_at is None, "跑完之后租约没有退掉"

    def test_an_expired_lease_goes_back_to_pending_and_is_requeued(self, monkeypatch):
        """**「一小时前的任务不能无限 pending」的判据。**"""
        seeded = _seed()
        recorder = _QueueRecorder()
        _single_mode(monkeypatch, recorder)
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
            BackgroundTask.query.filter_by(id=task_id).update({
                "status": "processing",
                "started_at": datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=queue_service.TASK_LEASE_SECONDS + 60),
                "lease_expires_at": datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=60),
            })
            db.session.commit()

            outcome = queue_service.reclaim_expired_task_leases()
            row = _row(task_id)

        # **判据是本用例造的那一行**，不是 `reclaim_expired_task_leases` 的全表计数：
        # 测试库是会话级共用的，别的文件留下的过期行会被一起收掉（实测合跑时是
        # `{'checked': 3, 'requeued': 2}`），拿总数断言等于让判据靠用例顺序来保。
        assert row.status == "pending", f"过期租约没有被收回：{row.status}（{outcome}）"
        assert row.lease_expires_at is None
        assert row.started_at is None
        assert row.retry_count >= 1, "收回时没有记账（界面看不出它被收回过）"
        assert any(
            isinstance(p, dict) and p.get("task_id") == task_id for p in recorder.payloads
        ), f"收回之后没有重投 —— 它会永远停在 pending：{recorder.payloads}"

    def test_a_live_lease_is_never_stolen(self, monkeypatch):
        """租约还有效 = 执行者可能还活着 → 一个字都不许动。"""
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
            worker.update_task_status_with_retry(task_id, "processing")
            before = _row(task_id)
            before_retry = before.retry_count
            before_lease = before.lease_expires_at

            outcome = queue_service.reclaim_expired_task_leases()
            row = _row(task_id)

        assert row.status == "processing", "还有效的租约被抢走了 —— 同一条任务会跑两遍"
        # 过滤到**本用例这一行**：全表计数（`requeued` / `failed`）会被别的文件留下的
        # 过期行填满，靠它判「有没有动到我这条」是要看用例顺序的。
        assert row.retry_count == before_retry, (
            f"这条还有效的租约被记账收回了（{outcome}）"
        )
        assert row.lease_expires_at == before_lease, "这条的租约被别人的扫描改动了"

    def test_a_task_that_keeps_expiring_is_failed_instead_of_looping(self, monkeypatch):
        """每次都把执行者弄死的任务不能无限在 pending/processing 之间来回。"""
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
            BackgroundTask.query.filter_by(id=task_id).update({
                "status": "processing",
                "retry_count": queue_service.MAX_TASK_LEASE_RECLAIMS - 1,
                "lease_expires_at": datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=60),
            })
            db.session.commit()

            outcome = queue_service.reclaim_expired_task_leases()
            row = _row(task_id)

        assert row.status == "failed", (
            f"重试次数已经用尽却还是放回 pending —— 它会永远转圈：{row.status}"
            f"（{outcome}）"
        )
        assert row.error_message, "放弃重试时没有写下原因"

    def test_startup_recovery_keeps_a_task_whose_lease_is_still_valid(self, monkeypatch):
        """启动恢复不再一刀切：**别人正拿着的**任务不许被本进程抢走。"""
        seeded = _seed()
        recorder = _QueueRecorder()
        _single_mode(monkeypatch, recorder)
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
            worker.update_task_status_with_retry(task_id, "processing")
            monkeypatch.setattr(worker, "check_and_create_auto_sync_tasks", lambda: None)
            recorder.payloads.clear()   # 建任务那一次也入过队，这里只看「恢复」这一趟

            worker.load_pending_tasks()
            row = _row(task_id)

        assert row.status == "processing", (
            "启动恢复把租约还有效的任务抢走了 —— 双 worker 下同一条任务会跑两遍"
        )
        assert not any(
            isinstance(p, dict) and p.get("task_id") == task_id for p in recorder.payloads
        ), "被抢走的任务还被重新排进了内存队列"

    def test_startup_recovery_reclaims_an_expired_one(self, monkeypatch):
        """反向：上一个进程死了（租约过期 / 老行没有租约）→ 必须收回来重投。"""
        seeded = _seed()
        recorder = _QueueRecorder()
        _single_mode(monkeypatch, recorder)
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
            BackgroundTask.query.filter_by(id=task_id).update({
                "status": "processing",
                "started_at": datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=queue_service.TASK_LEASE_SECONDS * 3),
                "lease_expires_at": datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=queue_service.TASK_LEASE_SECONDS),
            })
            db.session.commit()
            monkeypatch.setattr(worker, "check_and_create_auto_sync_tasks", lambda: None)
            recorder.payloads.clear()   # 建任务那一次也入过队，这里只看「恢复」这一趟

            worker.load_pending_tasks()
            row = _row(task_id)

        assert row.status == "pending", "死掉的那个进程留下的任务没有被收回"
        assert any(
            isinstance(p, dict) and p.get("task_id") == task_id for p in recorder.payloads
        ), "收回之后没有重投 —— 它在本次进程里永远不会被执行"

    def test_a_legacy_row_without_a_lease_is_reclaimed(self, monkeypatch):
        """**租约为 NULL 的老行按「已过期」处理**（宁可让它进一次恢复，也不许永远占位）。"""
        seeded = _seed()
        recorder = _QueueRecorder()
        _single_mode(monkeypatch, recorder)
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
            BackgroundTask.query.filter_by(id=task_id).update({
                "status": "processing",
                "lease_expires_at": None,
                "started_at": datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=queue_service.TASK_LEASE_SECONDS * 2),
            })
            db.session.commit()
            monkeypatch.setattr(worker, "check_and_create_auto_sync_tasks", lambda: None)

            worker.load_pending_tasks()

            assert _row(task_id).status == "pending"

    def test_two_workers_racing_only_one_claims_the_row(self, monkeypatch):
        """**双 worker 竞争**：条件 UPDATE + rowcount，只有一个能取走。

        把「先查 status 再改成 processing」换成条件 UPDATE 之前，两个 worker 会各自
        读到 pending、各自 UPDATE —— 同一条任务被跑两遍（同一次同步往缓存写两遍、
        同一份输入付两次费），而两边都以为自己才是唯一执行者。
        """
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))

        results = []
        errors = []
        start = threading.Barrier(2)

        def claimer():
            try:
                with flask_app.app_context():
                    start.wait(timeout=10)
                    results.append(
                        queue_service.claim_task_row_for_execution(task_id)
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=claimer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert errors == [], f"并发抢占抛异常了：{errors}"
        assert len(results) == 2, f"线程没跑完：{results}"
        assert sorted(results) == [False, True], (
            f"两个 worker 都以为自己抢到了（或都没抢到）：{results} —— 条件 UPDATE 失效了"
        )
        with flask_app.app_context():
            row = _row(task_id)
        assert row.status == "processing"
        assert row.lease_expires_at is not None, "抢到的那次没有起租"

    def test_a_payload_with_a_task_id_that_does_not_exist_still_runs(self):
        """载荷里的 task_id 不在库里（畸形载荷 / Agent 回传 / 测试桩）→ 照旧交给处理器。

        在这里拦掉，那一行就**永远没人碰**（正是要修的另一个病：永久 pending）。

        **本用例故意在裸线程里调**（没有 ambient app context）—— 真机上 worker 主循环
        就是那么调的。抢占函数必须自己 push context，否则第一次读库就抛 RuntimeError，
        而任务已经从内存队列弹掉了：一行都没执行、库里还是 pending（静默消失）。
        """
        from flask import has_app_context

        assert not has_app_context(), (
            "本用例要求裸调用；当前有 ambient app context，那样即使把修复撤掉也照样是绿的"
        )

        assert queue_service.claim_task_row_for_execution(None) is True
        assert queue_service.claim_task_row_for_execution(999_999_999) is True

    def test_the_claim_brings_its_own_app_context(self, monkeypatch):
        """真的走一次库（这一条会踩到「没有 ambient context」那条路）。"""
        from flask import has_app_context

        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))

        assert not has_app_context()
        assert queue_service.claim_task_row_for_execution(task_id) is True
        with flask_app.app_context():
            row = _row(task_id)
        assert row.status == "processing"
        assert row.lease_expires_at is not None


# ==========================================================================
#  六、`weekly_ai_analysis` 也有「已入队」账本（与同步同一把尺子）
# ==========================================================================


class TestTheAnalysisKeepsAnInQueueLedgerToo:
    """判据必须与 `weekly_sync` 那份**同一把尺子**：在账本里 = 只是没排到，维持现状。

    原先 AI 这边只看年龄（`WEEKLY_AI_TASK_STALE_SECONDS`），于是同一条任务在同步那边算
    「还在排队」、在 AI 这边算「卡死了」—— 两套判据必然分叉，而分叉的后果是把一条**正常
    排队**的分析置 failed，然后重建一条（同步那边实测过：30 分钟重置 12 次、重建 12 次）。
    """

    def test_a_queued_analysis_is_not_reset_as_stale(self):
        seeded = _seed()
        with flask_app.app_context():
            row = _add_task_row(
        task_type="weekly_ai_analysis", commit_id=str(seeded["config_id"]),
        file_path=seeded["group_key"], priority=priority.WEEKLY_AI_ANALYSIS,
        status="pending",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3),
        )

            weekly_handlers.register_enqueued_weekly_ai_task(row.id)
            weekly_handlers.reset_stale_weekly_analysis_tasks(
                seeded["group_key"],
                datetime.now(timezone.utc).replace(tzinfo=None),
                db=db,
                background_task_model=BackgroundTask,
                log_print=lambda *_a, **_k: None,
            )
            stored = _row(row.id)

        assert stored.status == "pending", (
            "还在内存队列里的分析被当成卡死重置了 —— 重置→重建的循环会回来"
        )

    def test_an_analysis_lost_with_the_memory_queue_is_still_reset(self):
        """**反自检**：账本里没有它（进程重启丢的那一类）必须照旧按年龄重置。

        少了这一条，一个「永远不重置」的实现能让上面那条全绿 —— 而那正是「永久排队中」。
        """
        seeded = _seed()
        with flask_app.app_context():
            row = _add_task_row(
        task_type="weekly_ai_analysis", commit_id=str(seeded["config_id"]),
        file_path=seeded["group_key"], priority=priority.WEEKLY_AI_ANALYSIS,
        status="pending",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3),
        )
            weekly_handlers.forget_enqueued_weekly_ai_task(row.id)

            weekly_handlers.reset_stale_weekly_analysis_tasks(
                seeded["group_key"],
                datetime.now(timezone.utc).replace(tzinfo=None),
                db=db,
                background_task_model=BackgroundTask,
                log_print=lambda *_a, **_k: None,
            )
            stored = _row(row.id)

        assert stored.status == "failed", "内存队列已经丢了它，却没有人重置"
        assert stored.error_message, "重置时没有写入原因"

    def test_a_young_analysis_is_never_reset(self):
        """刚创建的不算卡死（阈值 7200 秒；同步那条是 300 秒，两者**有意不同**）。"""
        seeded = _seed()
        with flask_app.app_context():
            row = _add_task_row(
        task_type="weekly_ai_analysis", commit_id=str(seeded["config_id"]),
        file_path=seeded["group_key"], priority=priority.WEEKLY_AI_ANALYSIS,
        status="pending",
        created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=60),
        )

            weekly_handlers.reset_stale_weekly_analysis_tasks(
                seeded["group_key"],
                datetime.now(timezone.utc).replace(tzinfo=None),
                db=db,
                background_task_model=BackgroundTask,
                log_print=lambda *_a, **_k: None,
            )
            stored = _row(row.id)

        assert stored.status == "pending", "刚创建 60 秒的分析被判成卡死了"

    def test_the_scheduler_uses_the_ledger_aware_reset(self):
        """接线：调度器必须走那一份（不许在调度器里再手写一遍按年龄的重置）。"""
        import inspect

        source = inspect.getsource(worker.schedule_weekly_ai_analysis_tasks)
        assert "reset_stale_weekly_analysis_tasks(" in source, (
            "调度器没有走账本判据的清理 —— 它会把排在队列里的分析当成卡死杀掉"
        )

    def test_the_two_ledgers_have_the_same_shape(self):
        """两份账本的**形状**必须一致（各自有登记/注销/查询/入队四个入口），
        否则下一个人只会守一边 —— 而分叉的那一边会静默失守。"""
        pairs = (
            ("register_enqueued_weekly_sync_task", "register_enqueued_weekly_ai_task"),
            ("forget_enqueued_weekly_sync_task", "forget_enqueued_weekly_ai_task"),
            ("is_weekly_sync_task_enqueued", "is_weekly_ai_task_enqueued"),
            ("enqueue_weekly_sync_task", "enqueue_weekly_ai_analysis_task"),
        )
        for sync_name, ai_name in pairs:
            assert hasattr(weekly_handlers, sync_name), sync_name
            assert hasattr(weekly_handlers, ai_name), (
                f"AI 那份账本缺了 {ai_name}（同步那份有 {sync_name}）"
            )
        assert weekly_handlers.is_weekly_sync_task_enqueued(None) is False
        assert weekly_handlers.is_weekly_ai_task_enqueued(None) is False


# ==========================================================================
#  七、接线：worker 真的会用这些判据
# ==========================================================================


class TestTheWiring:
    def test_the_worker_loop_reweighs_before_taking_a_task(self):
        """aging 只有挂在**取任务之前**才生效（入队那一刻算等于没算）。"""
        import inspect

        source = inspect.getsource(worker.background_task_worker)
        assert "reweigh_queue(" in source, "worker 主循环没有重算优先级 —— 饿死循环会回来"
        assert source.index("reweigh_queue(") < source.index("background_task_queue.get("), (
            "重算排在取任务之后 —— 堆顶那条还是旧的，等于没算"
        )

    def test_the_worker_loop_claims_the_row_before_executing(self):
        import inspect

        source = inspect.getsource(worker.background_task_worker)
        assert "claim_task_row_for_execution(" in source, (
            "worker 取到任务后没有抢占库里那一行 —— 双 worker 会把同一条任务跑两遍"
        )

    def test_the_lease_sweep_is_registered_in_the_scheduler(self):
        """只靠启动恢复是不够的：进程一直活着的时候没人收那些过期的行。"""
        import sys

        from tests.test_sync_wait_cadence import _REGISTERED, _REGISTERED_FUNCS, _FakeSchedule

        fake = _FakeSchedule()
        saved = sys.modules.get("schedule")
        saved_initialized = worker._schedule_initialized
        sys.modules["schedule"] = fake
        try:
            worker._schedule_initialized = False
            worker.setup_schedule(include_cleanup=False)
        finally:
            worker._schedule_initialized = saved_initialized
            if saved is None:
                sys.modules.pop("schedule", None)
            else:
                sys.modules["schedule"] = saved

        assert worker.reclaim_expired_task_leases_safely in _REGISTERED_FUNCS, (
            "租约回收没有注册进定时器 —— 死了的 worker 留下的行永远是 processing"
        )

    def test_the_module_level_constants_do_not_drift_from_the_literal_that_another_file_pins(self):
        """`create_weekly_sync_task` 里那个字面量必须与常量一致。

        那一处**有意**保留字面量：`tests/test_business_flow_comprehensive.py` 按源码文本
        断言了 `"priority=3" in create_weekly_sync_task 的函数体`，而那个文件不在本次施工的
        文件主权清单里。这条守卫负责在有人改常量时立刻报警，而不是让两处静默分叉。
        """
        import inspect

        source = inspect.getsource(worker.create_weekly_sync_task)
        assert f"priority={priority.WEEKLY_SYNC}" in source, (
            f"常量 WEEKLY_SYNC={priority.WEEKLY_SYNC} 与 create_weekly_sync_task 里的字面量"
            "对不上了 —— 改这里的同时要改 tests/test_business_flow_comprehensive.py"
            "（或把那一处也换成常量）"
        )


# ==========================================================================
#  八、租约续期：**合法运行期间租约不许过期**
# ==========================================================================


class TestTheLeaseIsRenewedWhileTheTaskRuns:
    """没有续期时，「租约时长」就等价于「任务最长能跑多久」。

    那意味着一次合法的长跑会在中途被判死 → 放回 pending → 重新执行：同步是白跑一遍，
    **AI 分析是同一份输入付两次费**（静默的重复开销，比报错难发现得多）。
    所以验收不是「阈值够大」，而是「跑着的时候它就没有过期过」。
    """

    def _claimed_task(self, monkeypatch):
        """建一条分析任务并**真的抢占**它（真机 worker 就是这么取的：裸线程 + 自带 context）。"""
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
        assert queue_service.claim_task_row_for_execution(task_id) is True
        return task_id

    def test_a_legitimately_long_run_never_loses_its_lease(self, monkeypatch):
        """**这一条就是「租约不能在合法运行期间过期」的验收。**

        按真实节奏（每 10 分钟续一轮，即 `TASK_LEASE_RENEW_INTERVAL_SECONDS`）模拟一次
        连跑 3 小时的分析 —— 远远超过 1 小时的租约，但它一次都不该被判死。
        """
        task_id = self._claimed_task(monkeypatch)
        assert task_id in queue_service.inflight_task_ids(), "抢占成功却没有进续租名单"
        claimed_row = _row(task_id)
        claimed_retry = claimed_row.retry_count
        claimed_lease = claimed_row.lease_expires_at

        start = datetime.now(timezone.utc).replace(tzinfo=None)
        for minutes in range(10, 181, 10):
            moment = start + timedelta(minutes=minutes)
            outcome = queue_service.renew_inflight_task_leases(now=moment)
            assert outcome["renewed"] >= 1, (
                f"第 {minutes} 分钟这一轮没有续到租：{outcome} —— 长任务会在半路被判死重跑"
            )

        with flask_app.app_context():
            outcome = queue_service.reclaim_expired_task_leases(
                now=start + timedelta(minutes=181), requeue=False
            )
        row = _row(task_id)
        # **判据是本用例造的那一行，不是全表计数。** 测试库是会话级共用的，`requeued`
        # 里装着**别的文件**留下的过期行（实测合跑时是 `{'checked': 3, 'requeued': 2}`）——
        # 拿它断言等于让判据靠用例顺序来保，单跑绿、合跑红。
        assert row.status == "processing", (
            "一次合法的长跑被判成死任务回收了 —— 重启之后它会从头再跑一遍"
            f"（AI 分析就是同一份输入付两次费）；本次扫描：{outcome}"
        )
        assert row.retry_count == claimed_retry, (
            f"这一行被当成过期租约收回了（记账了）—— 本次扫描：{outcome}"
        )
        assert row.lease_expires_at == claimed_lease or row.lease_expires_at > start, (
            "这一行的租约没有被续上"
        )

    def test_the_same_row_without_renewal_is_reclaimed(self, monkeypatch):
        """**反自检**：把续租摘掉，同一条行在同一个时间点必须被回收。

        没有这一条，上面那条测试可能只是因为「租约判据根本没生效」而通过。
        """
        task_id = self._claimed_task(monkeypatch)
        before_retry = _row(task_id).retry_count
        start = datetime.now(timezone.utc).replace(tzinfo=None)

        with flask_app.app_context():
            outcome = queue_service.reclaim_expired_task_leases(
                now=start + timedelta(minutes=181), requeue=False
            )

        row = _row(task_id)
        # 判据是**这一行**被收回了（状态 + 记账），不是扫描器的全表计数。
        assert row.status == "pending" and row.retry_count > before_retry, (
            f"没人续租的 processing 行没有被回收（租约判据失效）: "
            f"status={row.status}, retry={row.retry_count}（原 {before_retry}）；"
            f"本次扫描：{outcome}"
        )

    def test_renewal_stops_when_the_row_changed_hands(self, monkeypatch):
        """CAS：这行已经不是我的人时**不许盲目续租**。

        盲目续等于替别人养着一条租约：那条真死掉的行的租约永远不到期，也就永远收不回来。
        两种换主人的形态都要认出来：

        * 被回收后重投（`status → pending`、租约被清空）；
        * 被回收之后**别人**抢走（状态还是 `processing`，但租约换成了别人的时刻）——
          这一条只能靠「租约还是不是我写的那个值」认出来，光看状态认不出。
        """
        requeued = self._claimed_task(monkeypatch)
        re_claimed = self._claimed_task(monkeypatch)
        ended = self._claimed_task(monkeypatch)
        with flask_app.app_context():
            BackgroundTask.query.filter_by(id=requeued).update(
                {"status": "pending", "lease_expires_at": None, "started_at": None},
                synchronize_session=False,
            )
            # 第二个还是 processing，但租约已经被别人换成新的了
            BackgroundTask.query.filter_by(id=re_claimed).update(
                {"lease_expires_at": datetime.now(timezone.utc).replace(tzinfo=None)
                 + timedelta(minutes=30)},
                synchronize_session=False,
            )
            # 第三个已经进终态，但租约还留着（陈旧清理那条路只改状态、不退租）——
            # 只看租约认不出这一条，必须同时看「还是不是 processing」。
            BackgroundTask.query.filter_by(id=ended).update(
                {"status": "failed"}, synchronize_session=False
            )
            db.session.commit()

        outcome = queue_service.renew_inflight_task_leases(
            now=datetime.now(timezone.utc).replace(tzinfo=None)
        )

        assert outcome == {"renewed": 0, "lost": 3}, outcome
        for task_id in (requeued, re_claimed, ended):
            assert task_id not in queue_service.inflight_task_ids(), (
                f"任务 {task_id} 已经换了主人 / 已经结束，本进程还在替它续租 —— "
                "那条真的死掉的任务永远收不回来"
            )

    def test_a_renewal_failure_degrades_to_natural_expiry(self, monkeypatch):
        """**续租失败必须安全退化**：不判死、不抛出去（抛出去就是值班线程也死了）。

        续不上是存储/上下文的问题，不是任务的问题；让它按自然到期走正常回收流程。
        """
        task_id = self._claimed_task(monkeypatch)

        def _boom(*args, **kwargs):
            raise RuntimeError("库不可用")

        monkeypatch.setattr(queue_service, "lease_deadline", _boom)

        outcome = queue_service.renew_inflight_task_leases(
            now=datetime.now(timezone.utc).replace(tzinfo=None)
        )

        assert outcome == {"renewed": 0, "lost": 0}, outcome
        assert task_id in queue_service.inflight_task_ids(), (
            "一次续租失败就把任务撤出名单 —— 后端恢复之后也没人再续了"
        )
        monkeypatch.undo()

        # 「退化」的落点：租约自然到期，由正常流程回收（而不是被这里的失败判死）。
        # 判据是本用例这一行回到了 pending（全表 `requeued` 会被别的文件的过期行填满）。
        start = datetime.now(timezone.utc).replace(tzinfo=None)
        with flask_app.app_context():
            outcome = queue_service.reclaim_expired_task_leases(
                now=start + timedelta(seconds=queue_service.TASK_LEASE_SECONDS + 60),
                requeue=False,
            )
        row = _row(task_id)
        assert row.status == "pending", (
            f"续租失败之后连自然到期都收不回去了：status={row.status}；本次扫描：{outcome}"
        )
        assert row.retry_count >= 1, "自然到期回收时没有记账"

    def test_stamping_again_does_not_move_a_live_lease(self, monkeypatch):
        """**起租只发生一次**：抢占之后 handler 还会再写一次 `processing`。

        第二次如果换掉了租约时刻，续租用的 CAS（`lease_expires_at == 我们写下的那个值`）
        就再也认不出「这行还是我的」—— 长任务会被自己的续租机制坑死。
        """
        task_id = self._claimed_task(monkeypatch)
        first = _row(task_id).lease_expires_at
        assert first is not None

        with flask_app.app_context():
            worker.update_task_status_with_retry(task_id, "processing")

        assert _row(task_id).lease_expires_at == first, (
            "再写一次 processing 就把租约换了时刻 —— 续租从此全部落空"
        )

    def test_an_expired_lease_can_be_stamped_again(self, monkeypatch):
        """反自检：真到期（回收后重投）时必须能重新起租，否则那条行再也不受保护。"""
        task_id = self._claimed_task(monkeypatch)
        expired = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
        with flask_app.app_context():
            BackgroundTask.query.filter_by(id=task_id).update(
                {"lease_expires_at": expired}, synchronize_session=False
            )
            db.session.commit()

            row = db.session.get(BackgroundTask, task_id)
            stamped = queue_service.stamp_task_lease(row)
            db.session.commit()

        assert stamped is not None and stamped > expired, "过期的租约没有重新起租"

    def test_the_renewer_thread_lives_outside_the_worker(self):
        """线程本体：起得来、名字对得上（重复调用不起第二个）、停得掉、停了不留名单。"""
        try:
            thread = queue_service.start_lease_renewer()
            assert thread.is_alive(), "续租线程没起来 —— 长任务再也没有人续租"
            assert thread.name == "task-lease-renewer"
            assert queue_service.start_lease_renewer() is thread, "起了第二个续租线程"
            queue_service.register_inflight_task(
                123456789, datetime.now(timezone.utc).replace(tzinfo=None)
            )
        finally:
            queue_service.stop_lease_renewer()

        assert not queue_service._lease_renewer_thread
        assert not thread.is_alive(), "停不掉的线程会一直在后台续租"
        assert queue_service.inflight_task_ids() == [], (
            "停线程时没有清空名单：进程都不跑了还留着「我在跑」"
        )

    def test_the_worker_starts_and_stops_the_renewer_and_forgets_inflight_tasks(self):
        """接线：起线程、停线程、跑完撤名单三处必须在（缺一处就是静默的长任务重跑）。"""
        assert "start_lease_renewer()" in _code_only(worker.start_background_task_worker), (
            "worker 起来的时候没有起续租线程"
        )
        assert "stop_lease_renewer()" in _code_only(worker.stop_background_task_worker), (
            "worker 停了却还在续租"
        )
        loop = _code_only(worker.background_task_worker)
        assert "forget_inflight_task(" in loop, (
            "跑完没有撤出续租名单 —— 执行者走了但租约一直被人续着，再也收不回来"
        )

    def test_the_agent_side_transition_also_registers_for_renewal(self, monkeypatch):
        """**agent 侧的长分析也必须有人续租。**

        agent 模式（平台部署模式）下执行者是**另一个进程**：它不经过本进程的内存队列，
        也就不会走 `claim_task_row_for_execution`。它开始跑的那一刻只调
        `update_task_status_with_retry(task_id, 'processing')`。登记如果只挂在队列那条路上，
        agent 侧的行就没人续租 —— 平台侧扫描会在一个租约之后把它判死、重新派发，
        同一份输入付两次费。
        """
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
            # 只写状态（模拟 agent 侧那一跳，没有抢占、没有内存队列）
            worker.update_task_status_with_retry(task_id, "processing")
            assert task_id in queue_service.inflight_task_ids(), (
                "进入 processing 却没有进续租名单 —— agent 侧的长分析会在半路被判死重跑"
            )
            before_retry = _row(task_id).retry_count
            start = datetime.now(timezone.utc).replace(tzinfo=None)

        # 跑过两个租约那么久（按真实节奏续），仍然不许被别人收回
        step = queue_service.TASK_LEASE_RENEW_INTERVAL_SECONDS
        horizon = queue_service.TASK_LEASE_SECONDS * 2 + 60
        with flask_app.app_context():
            for offset in range(step, horizon, step):
                renewed = queue_service.renew_inflight_task_leases(
                    now=start + timedelta(seconds=offset)
                )
                assert renewed["renewed"] == 1, (
                    f"第 {offset} 秒这一轮没有续到租：{renewed}"
                )
            outcome = queue_service.reclaim_expired_task_leases(
                now=start + timedelta(seconds=horizon), requeue=False
            )
        row = _row(task_id)
        assert row.status == "processing", (
            f"这条（agent 侧那条路）被当成过期租约收回了：{outcome}"
        )
        assert row.retry_count == before_retry, f"被记账收回了：{outcome}"

        # 而跑完之后名字也要撤掉（否则一条已经结束的行会被一直续下去）
        with flask_app.app_context():
            worker.update_task_status_with_retry(task_id, "completed")
        assert task_id not in queue_service.inflight_task_ids(), (
            "任务已经终态，名字还挂在续租名单上"
        )
        assert _row(task_id).lease_expires_at is None

    def test_resetting_a_stale_row_also_drops_its_lease(self, monkeypatch):
        """**不变式：进了终态就不许再有租约。**

        陈旧清理那条路（`_reset_stale_rows`）原先只改状态，租约留在行上 ——
        而「有租约」的意思就是「有人在跑它」。它会让排查与续租的 CAS 都看错这一行。
        """
        seeded = _seed()
        _single_mode(monkeypatch, _QueueRecorder())
        stale_moment = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        with flask_app.app_context():
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            # `_track` 有副作用（登记进清理名单），调用要留；返回值这条用例不用
            # （后面读的是 `_row(task_id)` / `stored`），所以不接变量。
            _track(db.session.get(BackgroundTask, task_id))
            BackgroundTask.query.filter_by(id=task_id).update(
                {
                    "status": "pending",
                    "created_at": stale_moment,
                    "lease_expires_at": datetime.now(timezone.utc).replace(tzinfo=None),
                },
                synchronize_session=False,
            )
            db.session.commit()

            reset_count = weekly_handlers._reset_stale_rows(
                [db.session.get(BackgroundTask, task_id)],
                now_utc_naive=datetime.now(timezone.utc).replace(tzinfo=None),
                timeout_seconds=300,
                is_enqueued=lambda _task_id: False,
                error_message="卡住了",
                label="AI 分析任务",
                db=db,
                log_print=lambda *_args, **_kwargs: None,
            )
            assert reset_count == 1, "陈旧的 pending 行没有被重置"
            stored = _row(task_id)

        assert stored.status == "failed"
        assert stored.lease_expires_at is None, (
            "置成 failed 了却还留着租约 —— 「有租约」= 有人在跑它，这一行会一直自相矛盾"
        )


# ==========================================================================
#  九、「谁还活着」只有一个判据（去重那道与租约那道合并）
# ==========================================================================


class TestTheWedgedCriterionIsTheLeaseCriterion:
    """同一行 `processing` 不许被两套判据给出两个答案。

    分叉的形态是安静的：去重那边按「跑了 3600 秒」判死并重建一条同步，而租约那边
    （续租还替它刷着）说它还活着 —— 两个同步同时写同一份周版本缓存。
    """

    def _row_stub(self, *, status="processing", lease=None, started=None, created=None):
        from types import SimpleNamespace

        return SimpleNamespace(
            status=status, lease_expires_at=lease, started_at=started, created_at=created
        )

    def test_the_two_answers_are_the_same_answer(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        bound = worker.WEDGED_SYNC_PROCESSING_SECONDS
        cases = (
            # 名字, 行, 期望（True = 主人已经不在了）
            ("租约还有效（哪怕 started_at 很老）", self._row_stub(
                lease=now + timedelta(minutes=30),
                started=now - timedelta(hours=5)),
             False),
            ("租约过期（哪怕 started_at 很新）", self._row_stub(
                lease=now - timedelta(minutes=1), started=now - timedelta(seconds=5)),
             True),
            ("老行：无租约 + started_at 很老", self._row_stub(
                started=now - timedelta(seconds=bound + 60)),
             True),
            ("老行：无租约 + 只有 created_at 很老", self._row_stub(
                created=now - timedelta(seconds=bound + 60)),
             True),
            # 这一条是既有的反自检行为（`test_a_processing_sync_blocks_the_next_tick`）：
            # 刚建的行不许被判死，否则每个 tick 都会另起一个同步、两个抢着写同一份缓存。
            ("无租约 + 无 started_at + 刚建的 created_at", self._row_stub(created=now),
             False),
        )
        for label, row, expected in cases:
            wedged = worker._is_wedged_processing_sync_task(row)
            expired = queue_service.lease_is_expired(row, timeout_seconds=bound)
            assert wedged is expected, f"{label}：去重判据给出 {wedged}，期望 {expected}"
            assert expired is expected, f"{label}：租约判据给出 {expired}，期望 {expected}"

    def test_a_lease_that_keeps_being_renewed_is_not_wedged(self):
        """续租让「真的很慢」不再被当成「卡死」—— 这正是合并带来的新能力。

        合并前：同步跑过 3600 秒（`started_at` 很老）就被去重判死并重建，
        而它**还在跑**（租约一直被续）→ 两个任务抢着写同一份周版本缓存。
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        row = self._row_stub(
            lease=now + timedelta(minutes=30), started=now - timedelta(hours=5)
        )
        assert worker._is_wedged_processing_sync_task(row) is False

    def test_the_wedged_threshold_is_the_lease_bound(self):
        """两个界必须是同一个数：否则同一行在两边同时被看着时答案不同。"""
        assert worker.WEDGED_SYNC_PROCESSING_SECONDS == queue_service.TASK_LEASE_SECONDS

    def test_a_pending_row_is_never_wedged(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert worker._is_wedged_processing_sync_task(
            self._row_stub(status="pending", created=now)
        ) is False

    def test_the_wedged_predicate_delegates_instead_of_keeping_its_own_clock(self):
        source = _code_only(worker._is_wedged_processing_sync_task)
        assert "lease_is_expired(" in source, (
            "_is_wedged_processing_sync_task 又自己算年龄了 —— 「谁还活着」有两个实现"
        )
        assert "is_stale_sync_task(" not in source, (
            "旧的年龄判据还在这个函数里 —— 与租约判据会分叉"
        )


# ==========================================================================
#  十、已知的既有行为（**不是设计意图**，钉住给第二波看）
# ==========================================================================


class TestKnownPreexistingBehaviourPinnedForTheSecondWave:
    def test_agent_mode_load_pending_tasks_still_enqueues_into_the_local_memory_queue(self, monkeypatch):
        """**这是已知的既有行为，不是设计意图 —— 本用例只把它钉住，不改它。**

        platform/agent 模式下任务该由 AgentTask 派发出去执行，但 `load_pending_tasks`
        装载时仍然把每一行 pending 都**塞进本进程的内存队列**（`_enqueue_pending_row`
        里没有按派发模式分支）。于是本进程既是派发者又是执行者，agent 模式下也留着一份
        本地执行路径 —— 「谁在执行」这个问题有两份答案。

        第二波重构（agent 侧接管执行）时必须看到这一条。本分片**按裁决不改它**：
        改它会动到载荷构造与派发语义，超出文件主权。
        """
        import json

        seeded = _seed()
        recorder = _QueueRecorder()
        monkeypatch.setattr(worker, "background_task_queue", recorder)
        monkeypatch.setattr(worker, "check_and_create_auto_sync_tasks", lambda: None)
        with flask_app.app_context():
            # 先按单机模式建行（agent 模式建行会顺带派发出一条 AgentTask，
            # 而那条记录带着外键指着这一行 —— 收尾删行时会撞 FK。本用例要看的只是装载）。
            monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: False)
            task_id = queue_service.create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"]
            )
            _track(db.session.get(BackgroundTask, task_id))
            # 建行这一跳自己也会往队列里塞一份载荷（单机模式），先清掉 ——
            # 否则下面的断言会被这一份满足，装载那一跳有没有入队都看不出来。
            recorder.payloads.clear()
            # 只在装载这一步切到 agent 模式
            monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: True)
            worker.load_pending_tasks()

            assert _row(task_id).status == "pending", "装载时把行改成别的状态了"
        payloads = [p for p in recorder.payloads if isinstance(p, dict)]
        assert any(p.get("task_id") == task_id for p in payloads), (
            "agent 模式下这一行没有被装进本地内存队列 —— 该行为被改掉了"
            "（若是有意为之，请连本用例一起改）: "
            f"{json.dumps(payloads, ensure_ascii=False)}"
        )
        assert any(p.get("type") == "weekly_ai_analysis" for p in payloads), (
            "本地入队用的不是执行载荷（`type`）—— 形态变了，第二波重构要注意"
        )
