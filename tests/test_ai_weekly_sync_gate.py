# -*- coding: utf-8 -*-
"""「同步没跑完就先别分析」的闸门。

## 为什么要有它（用户报的那句话）

线上周版本分析的结论里写着：

    本轮仅取得少量配表 diff 与角色属性表/怪物仇恨表内容，
    战斗逻辑、吸灵器链、拓扑组队、支付等代码改动均未取到 diff 正文

而周版本同步是**按文件逐个**写缓存行的（`generate_weekly_merged_diff`：一个文件一行、
各自提交），AI 的变更清单又按 `WeeklyVersionDiffCache.updated_at > last_analyzed_at`
取 —— 于是**同步跑到一半时的快照只有已写好的那批文件**。用户看到的那句话不是
「取不到」，是那批文件当时还没进缓存。

而且它是**静默**的：提示词里的「共 N 个文件」也跟着变小，模型与读报告的人都以为那是
全量。所以这一组测试守两件事：

1. 同步在跑时**不排队、也不执行**（两个入口各一道，因为重启会把残留任务重新入队）；
2. 卡死的同步任务**不能把分析永久挡住**（有上限，超了就带日志放行）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import app as flask_app, create_tables, db
from models import BackgroundTask, Project, Repository
from models.weekly_version import WeeklyVersionConfig
from services.ai.weekly_sync_gate import (
    SYNC_IN_FLIGHT_MAX_SECONDS,
    group_config_ids,
    weekly_sync_in_flight,
    weekly_sync_stuck_note,
)
from services.ai_analysis_service import update_project_analysis_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed(*, with_sync_task: bool = True, status: str = "pending", age_minutes: int = 1):
    """造一个项目 + 两条同窗口的周版本配置（模拟多仓库分组），可选一条同步任务。"""
    now = datetime.now(timezone.utc)
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("GT"), name="闸门用例")
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

        task = None
        if with_sync_task:
            task = BackgroundTask(
                task_type="weekly_sync", commit_id=str(configs[0].id),
                priority=3, status=status,
                created_at=now - timedelta(minutes=age_minutes),
            )
            db.session.add(task)
        db.session.commit()
        # **显式打开自动分析开关。** 「周版本自动分析」的默认值是**关**（2026-09-22 起），
        # 而本文件测的是**同步闸门**（还有它后面那些闸）—— 前提是这次分析本来会跑。
        # 不声明这个前提的后果：它们会全部停在「开关关闭」那道更靠前的闸后面，
        # 报出来的失败是「同步没挡住分析」，而真正的原因是与被测对象无关的第一道闸。
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
            "task_id": task.id if task else None,
        }


# ==========================================================================
#  一、闸门本身
# ==========================================================================


class TestTheGate:
    def test_no_sync_task_means_no_gate(self):
        seeded = _seed(with_sync_task=False)
        with flask_app.app_context():
            assert weekly_sync_in_flight(seeded["config_ids"]) == ""

    def test_a_pending_sync_blocks_the_analysis(self):
        seeded = _seed(status="pending", age_minutes=3)
        with flask_app.app_context():
            reason = weekly_sync_in_flight(seeded["config_ids"])

        assert reason, "同步还在排队，却放行了分析"
        assert str(seeded["task_id"]) in reason, f"原因里没说是哪个任务：{reason}"
        assert "3 分钟" in reason, reason

    def test_a_running_sync_blocks_the_analysis(self):
        """`processing` 同样要拦 —— 它是「正在写」，比排队更接近出问题的时刻。"""
        seeded = _seed(status="processing", age_minutes=1)
        with flask_app.app_context():
            assert weekly_sync_in_flight(seeded["config_ids"])

    def test_a_finished_sync_does_not_block(self):
        seeded = _seed(status="completed", age_minutes=1)
        with flask_app.app_context():
            assert weekly_sync_in_flight(seeded["config_ids"]) == ""

    def test_a_stuck_sync_is_released_with_a_loud_note(self):
        """**卡死不能永久挡住分析。**

        同步任务卡在 `processing` 时没有任何现存机制会重置它（`schedule_weekly_sync_tasks`
        只重置超时的 `pending`），所以闸门必须自己有上限。放行时必须留下一条醒目日志 ——
        静默放行等于把「清单可能不完整」这件事藏起来。
        """
        seeded = _seed(status="processing", age_minutes=(SYNC_IN_FLIGHT_MAX_SECONDS // 60) + 5)
        with flask_app.app_context():
            assert weekly_sync_in_flight(seeded["config_ids"]) == "", "卡死的同步把分析永久挡住了"
            note = weekly_sync_stuck_note(seeded["config_ids"])

        assert note and "不再拦" in note, note
        assert "不完整" in note, f"放行时没说清代价：{note}"

    def test_a_fresh_sync_gets_no_stuck_note(self):
        seeded = _seed(status="processing", age_minutes=2)
        with flask_app.app_context():
            assert weekly_sync_stuck_note(seeded["config_ids"]) == ""

    def test_another_batch_sync_does_not_block_this_one(self):
        """只拦**本批次**的同步：别的项目/窗口在跑，不该影响这一组的分析。"""
        seeded = _seed(with_sync_task=False)
        other = _seed(status="processing", age_minutes=1)
        with flask_app.app_context():
            assert weekly_sync_in_flight(seeded["config_ids"]) == ""
            assert other["config_ids"][0] not in seeded["config_ids"]

    def test_a_naive_utc_timestamp_does_not_break_the_gate(self):
        """库里的时间是 naive-UTC，比较的另一头可能带时区 —— 混着减会抛 TypeError。

        抛出去的表现是「闸门每次都不拦」（调用方都在 try 外面）或者整次分析崩掉，
        两种都比不加闸更糟。
        """
        seeded = _seed(with_sync_task=False)
        with flask_app.app_context():
            naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
            aware_now = datetime.now(timezone.utc)
            assert weekly_sync_in_flight(seeded["config_ids"], now=naive_now) == ""
            assert weekly_sync_in_flight(seeded["config_ids"], now=aware_now) == ""
            assert weekly_sync_in_flight([], now=aware_now) == ""


# ==========================================================================
#  二、分组口径
# ==========================================================================


class TestTheGroupScope:
    def test_the_group_is_the_whole_batch_not_one_repository(self):
        """变更清单来自**整批**仓库的缓存行：只看自己那一个仓库的同步，另一个仓库
        还在写的时候照样会漏文件。"""
        seeded = _seed()
        with flask_app.app_context():
            config = db.session.get(WeeklyVersionConfig, seeded["primary_config_id"])
            ids = group_config_ids(config)

        assert set(ids) == set(seeded["config_ids"]), ids

    def test_a_missing_config_is_not_a_crash(self):
        assert group_config_ids(None) == []


# ==========================================================================
#  三、两个入口都设了闸
# ==========================================================================


class TestBothEntrypoints:
    def test_the_background_entry_skips_without_creating_a_run(self):
        """**行为断言**：后台入口（重启时会把残留任务重新入队的那个）必须真的跳过。

        跳过必须发生在建 run 之前 —— 建了 run 再跳过会留下一条「跑了但没结论」的记录，
        用量面板上还会多一条零消费运行，排查时看不出它是被闸门挡下的。
        """
        from models import AiAnalysisRun
        from services.ai_analysis_service import run_weekly_analysis_background

        seeded = _seed(status="processing", age_minutes=2)
        with flask_app.app_context():
            outcome = run_weekly_analysis_background(seeded["primary_config_id"])

            assert outcome.get("status") == "skipped", outcome
            assert outcome.get("reason") == "sync_in_flight", outcome
            assert "同步还在跑" in (outcome.get("message") or ""), outcome
            assert (
                AiAnalysisRun.query.filter_by(target_id=seeded["primary_config_id"]).count() == 0
            ), "跳过了却还是留下了一条 run 记录"

    def test_the_background_entry_proceeds_when_the_sync_is_done(self):
        """反面：同步不在跑时**不许**被这道闸门挡住（否则自动分析会永久停摆）。

        走到底会失败在「没有 API key」这类既有闸门上 —— 那正好说明它越过了新闸门。
        """
        from services.ai_analysis_service import run_weekly_analysis_background

        seeded = _seed(status="completed", age_minutes=1)
        with flask_app.app_context():
            outcome = run_weekly_analysis_background(seeded["primary_config_id"])

        assert outcome.get("reason") != "sync_in_flight", outcome

    def test_the_scheduler_queues_nothing_while_a_sync_runs(self):
        """调度器那一道用**结构断言**（与预算那条同款，理由也相同）：
        `schedule_weekly_ai_analysis_tasks` 要的上下文（app、活跃配置、分组、水位线、
        后台任务表）与这条性质无关，构造出来的测试只会验证构造本身。

        两条都要钉住：闸门在**建任务之前**、且跳过时**不推进水位线**（推进了就变成
        「跳过这一次，这一周都不再尝试」）。
        """
        source = (PROJECT_ROOT / "services" / "task_worker_service.py").read_text(encoding="utf-8")
        body = source[source.index("def schedule_weekly_ai_analysis_tasks"):]
        body = body[: body.index("def schedule_repository_sync_tasks")]

        assert "weekly_sync_in_flight" in body, "调度器没有查同步是否还在跑"
        gate_at = body.index("weekly_sync_in_flight")
        create_at = body.index("create_weekly_ai_analysis_task(")
        assert gate_at < create_at, (
            "同步闸门排在建任务之后 —— 那还是会产生一个注定被跳过的后台任务"
        )
        # 从闸门到建任务之间：必须跳过，且**不许**碰触发水位线。
        # 水位线在同一个函数里还有一处赋值（超预算那条分支），所以按区间判而不是全局搜 ——
        # 全局搜会命中那一条，把这条断言变成永远为真。
        window = body[gate_at:create_at]
        assert "continue" in window, "查到同步在跑却没有跳过"
        assert "last_triggered_at" not in window, (
            "跳过时把触发水位线也推进了 —— 下一个周期不会再试，这一周的改动就没人分析了"
        )

    def test_the_execution_gate_sits_before_the_run_is_created(self):
        """执行那道闸必须排在 `_create_run` 之前，理由同上面「不留零消费 run」。"""
        source = (PROJECT_ROOT / "services" / "ai_analysis_service.py").read_text(encoding="utf-8")
        body = source[source.index("def run_weekly_analysis_background"):]
        body = body[: body.index("def get_latest_weekly_result")]

        assert "weekly_sync_in_flight" in body, "后台入口没有查同步是否还在跑"
        assert body.index("weekly_sync_in_flight") < body.index("_create_run("), (
            "执行闸门排在建 run 之后 —— 会留下一条零消费的运行记录"
        )
