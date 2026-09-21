# -*- coding: utf-8 -*-
"""「点一次就够」：被同步闸门拦下之后**登记意图**，同步一跑完就自动开始。

## 这一组测试守的是什么

用户实测反馈：点了「重新分析」之后**反复**只看到

    等待 Diff 同步完成后再分析 —— 周版本同步还在跑（task_id=443，已 1 分钟）……

闸门判得**对**（同步确实在往缓存里写），但拦下之后**没有任何出路**：不排队、不自动重试、
不登记意图 —— 用户只能自己再点一次，撞上哪一段看运气。而大仓一轮同步要 340~420 秒，
调度器却每 2 分钟补一条，「有一条同步在写缓存」接近于稳态，所以「反复」不是偶发。

审计文档要求的那一半（原文）：「**同步中点击按钮时不应静默失败。创建 `waiting_snapshot`
任务并展示『等待 Diff 同步完成』，同步结束后再冻结快照**」。

## 四条硬要求（对应下面四组用例）

1. **登记意图本身不许产生任何模型调用**（不建 run、不进分析队列）；
2. 同步还在跑 → 不转交；同步一结束 → 自动转交（建一条真实的周版本分析后台任务）；
3. 用户在这期间**已经手工跑成了** → 登记的那次自动作废（不许变成第二次付费运行）；
4. 意图过期（同步一直没结束）→ 不许再挡着用户手动点。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func

from app import app as flask_app
from app import create_tables, db
from models import BackgroundTask, Project, Repository
from models.ai_analysis import AiAnalysisRun
from models.weekly_version import WeeklyVersionConfig
from services.ai_analysis_service import build_weekly_group_key


@pytest.fixture(autouse=True)
def _cleanup_runs_created_here():
    """本文件建出来的 run 必须自己收掉。

    **测试库是会话级共用的**，而 `tests/test_weekly_ai_auto_trigger_gate.py` 里有一条
    **全局计数**断言（`fail_orphaned_analysis_runs() == 1`，它数的是全库）。本文件有几条
    用例会留下 `running` 的 run（`describe` 与「已有一条在跑」那两条），留着它们就会让
    那条用例在「一起跑」时红、单独跑时绿 —— 与本仓库既有的 `_cleanup_runs` 同一条纪律。
    """
    with flask_app.app_context():
        create_tables()
        baseline = db.session.query(func.max(AiAnalysisRun.id)).scalar() or 0
    yield
    with flask_app.app_context():
        AiAnalysisRun.query.filter(AiAnalysisRun.id > baseline).delete(synchronize_session=False)
        db.session.commit()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed(*, with_sync_task: bool = False, sync_status: str = "pending"):
    """造一个项目 + 两条同窗口的周版本配置（模拟多仓库分组），可选一条同步任务。"""
    now = datetime.now(timezone.utc)
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("SW"), name="等待同步用例")
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
                priority=3, status=sync_status, created_at=now - timedelta(minutes=1),
            )
            db.session.add(task)
        db.session.commit()
        return {
            "project_id": project.id,
            "config_ids": [cfg.id for cfg in configs],
            "config_id": configs[0].id,
            "sibling_config_id": configs[1].id,
            "group_key": build_weekly_group_key(configs[0]),
            "sync_task_id": task.id if task else None,
        }


def _pending_intents(group_key: str):
    with flask_app.app_context():
        return BackgroundTask.query.filter_by(
            task_type="weekly_ai_waiting", file_path=group_key, status="pending"
        ).all()


def _analysis_tasks(group_key: str):
    with flask_app.app_context():
        return BackgroundTask.query.filter(
            BackgroundTask.task_type == "weekly_ai_analysis",
            BackgroundTask.file_path == group_key,
            BackgroundTask.status.in_(["pending", "processing"]),
        ).all()


# ==========================================================================
#  一、登记意图：一行记录，零模型调用
# ==========================================================================


class TestRegisteringTheIntent:
    def test_registering_creates_one_row_and_never_calls_a_model(self):
        """**登记意图不许产生任何模型调用**（这是这一版最硬的一条）。

        所以它不能是「排一条 AI 分析任务」—— 那会走进 `run_weekly_analysis_background`，
        在闸门失效的那一天变成一次真金白银的调用。它只能是一行**意图记录**：
        worker 不认识它、不取它、不执行它，由同步收尾时的唤醒逻辑决定要不要转交。

        断言三件事：库里多了一行意图、**没有**多出一行分析任务、**没有**多出一条 run。
        """
        from services.task_worker_queue_service import register_waiting_analysis_intent
        from services.task_worker_service import background_task_queue

        seeded = _seed()
        before_qsize = background_task_queue.qsize()
        with flask_app.app_context():
            task_id = register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            row = db.session.get(BackgroundTask, task_id)

            assert row is not None, "登记意图没有落库"
            assert row.task_type == "weekly_ai_waiting", row.task_type
            assert row.status == "pending", row.status
            assert row.file_path == seeded["group_key"], row.file_path
            assert row.commit_id == str(seeded["config_id"]), row.commit_id
            # 意图行不是可执行任务：没有入队，也没有建分析任务与 run。
            assert background_task_queue.qsize() == before_qsize, "登记意图往内存队列里塞了东西"
            assert _analysis_tasks(seeded["group_key"]) == [], "登记意图直接排了一条分析任务"
            assert (
                AiAnalysisRun.query.filter_by(target_key=seeded["group_key"]).count() == 0
            ), "登记意图建了一条 run（那就是一次要花钱的运行）"

    def test_registering_twice_keeps_a_single_pending_intent(self):
        """连点两次不该攒出两条意图（否则唤醒时会各转交一次）。"""
        from services.task_worker_queue_service import register_waiting_analysis_intent

        seeded = _seed()
        with flask_app.app_context():
            first = register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            second = register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])

            assert first == second, (first, second)
            assert len(_pending_intents(seeded["group_key"])) == 1


# ==========================================================================
#  二、唤醒：同步跑完才转交
# ==========================================================================


class TestWakingTheIntent:
    def test_it_waits_while_the_sync_still_writes_the_cache(self):
        """同步还在跑 → 意图保持 pending，**不转交**（转交了也是在闸门上再被挡一次）。"""
        from services.task_worker_queue_service import (
            register_waiting_analysis_intent,
            wake_waiting_analysis_intents,
        )

        seeded = _seed(with_sync_task=True, sync_status="processing")
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            # **不判全局计数**：测试库是会话级共用的，唤醒会扫到别的用例留下的意图。
            # 要判的是**这一组**的处置（与本文件其它用例同一口径）。
            outcome = wake_waiting_analysis_intents()

            assert outcome.get("checked", 0) >= 1, outcome
            assert len(_pending_intents(seeded["group_key"])) == 1, "意图被提前消费掉了"
            assert _analysis_tasks(seeded["group_key"]) == [], "同步还在跑却排了分析任务"

    def test_it_hands_off_once_the_sync_is_done(self):
        """同步一结束就自动开始 —— 用户点过一次，不该再点第二次。"""
        from services.task_worker_queue_service import (
            register_waiting_analysis_intent,
            wake_waiting_analysis_intents,
        )

        seeded = _seed(with_sync_task=True, sync_status="completed")
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            wake_waiting_analysis_intents()

            handed = _analysis_tasks(seeded["group_key"])
            assert len(handed) == 1, "同步结束了却没有自动开始"
            assert handed[0].commit_id == str(seeded["config_id"]), handed[0].commit_id

    def test_the_handed_off_task_says_it_was_the_user_who_asked(self, monkeypatch):
        """**「自动开始」的那一次是用户点出来的，不是系统自己跑的。**

        唤醒路径要显式传 `manual`：这条链子（`_wake_one_waiting_intent` → 任务载荷 →
        handler → 运行行）任何一环丢了标记，用量面板就会把用户付费的这次算成「定时」，
        而那正是「AI 自己偷偷花钱」的观感来源。
        """
        import services.task_worker_service as worker
        from services.task_worker_queue_service import (
            register_waiting_analysis_intent,
            wake_waiting_analysis_intents,
        )

        class _Recorder:
            def __init__(self):
                self.payloads = []

            def put(self, wrapper):
                self.payloads.append(getattr(wrapper, "task_data", None))

            def qsize(self):
                return len(self.payloads)

        seeded = _seed(with_sync_task=True, sync_status="completed")
        recorder = _Recorder()
        with flask_app.app_context():
            monkeypatch.setattr(worker, "background_task_queue", recorder)
            monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: False)
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            wake_waiting_analysis_intents()

        kinds = [p.get("trigger_source") for p in recorder.payloads if isinstance(p, dict)]
        assert kinds, f"唤醒之后没有任何分析任务进队列：{recorder.payloads}"
        assert "manual" in kinds, f"唤醒排出来的任务把「用户点的」丢了：{kinds}"

    def test_it_hands_off_only_once(self):
        """转交之后（分析任务已在队列里）再唤醒一次不要再排一条。"""
        from services.task_worker_queue_service import (
            register_waiting_analysis_intent,
            wake_waiting_analysis_intents,
        )

        seeded = _seed(with_sync_task=True, sync_status="completed")
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            wake_waiting_analysis_intents()
            wake_waiting_analysis_intents()

            assert len(_analysis_tasks(seeded["group_key"])) == 1, "同一份输入被转交了两条"


# ==========================================================================
#  二·补、唤醒钩子**必须自带应用上下文**（真机回归）
#
#  真机日志原文（修复前）：
#      ⚠️ 唤醒等待同步的分析意图失败（不影响这次同步）: Working outside of application context.
#
#  `_handle_weekly_sync_task` 的 `finally` 里调 `wake_waiting_analysis_intents_safely()`，
#  而那个位置已经在 `handle_weekly_sync_task_service` 内部的 `app_context()` **之外** ——
#  worker 线程裸调、第一次读库就抛，被宽 `except` 咽掉：**主路径静默失效**，每轮同步还往
#  日志里丢一行警告（功能上只剩 `:1412` 每分钟兜底在续命，最多等一个周期）。
#
#  之前的用例为什么没抓住：它们自己带着 `with app.app_context():`（或 flask fixture），
#  真机不带。所以这两条**必须在没有 ambient app context 的情况下**调用，并且两条断言
#  缺一不可 —— 只断言「没打警告」会让「函数被改成空实现」也绿；只断言「转交了」在没上下文
#  时是抛异常，红得指不出真原因。合起来才钉得住「在裸线程里也真的干成了活、且不吵」。
# ==========================================================================


class TestTheWakeHookBringsItsOwnAppContext:
    def _seed_with_intent(self):
        seeded = _seed(with_sync_task=True, sync_status="completed")
        with flask_app.app_context():
            from services.task_worker_queue_service import register_waiting_analysis_intent

            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
        return seeded

    def _assert_no_wake_failure_logged(self, lines):
        literal = [line for line in lines if "唤醒等待同步的分析意图失败" in line]
        assert not literal, (
            "唤醒意图又被宽 except 咽掉了（真机那条 Working outside of application context. "
            f"回来了）：{literal}"
        )
        # 换个说法也照样抓住：这条路径**任何**「唤醒 + 失败」的日志都是同一个故障。
        loose = [line for line in lines if "唤醒" in line and "失败" in line]
        assert not loose, f"唤醒路径报错了（换了措辞但故障相同）：{loose}"

    def test_it_works_in_a_bare_thread_with_no_ambient_app_context(self, monkeypatch):
        """真机形态：**没有** app context 时调用 —— 既不许吵，也必须真的转交。"""
        from flask import has_app_context

        import services.task_worker_service as worker

        lines: list = []
        monkeypatch.setattr(worker, "log_print", lambda msg, *a, **k: lines.append(str(msg)))
        seeded = self._seed_with_intent()

        # **前提断言**：本用例的价值全在「没有 ambient app context」上。将来谁加了一个
        # autouse 的 app-context fixture，这条会先红 —— 否则它会静默退化成「复现不了」。
        assert not has_app_context(), (
            "本用例要求裸调用（真机上 worker 线程就是裸的）；当前有 ambient app context，"
            "那样即使把修复撤掉也照样是绿的"
        )

        worker.wake_waiting_analysis_intents_safely()

        handed = _analysis_tasks(seeded["group_key"])
        assert handed, "同步已经跑完，意图却没有被转交 —— 唤醒主路径静默失效了"
        # 正向信号也钉在日志上：转交成功那行必须有（意图行此时**故意**还留 pending，
        # 由「同组已有分析任务」与「被已建的运行覆盖」两条判据收尾，见 `_wake_one_waiting_intent`）。
        assert any("同步已结束，等待的分析自动开始" in line for line in lines), (
            f"转交发生了却没有留下那行日志：{lines}"
        )
        self._assert_no_wake_failure_logged(lines)

    def test_it_is_safe_to_call_from_inside_an_existing_app_context(self, monkeypatch):
        """那个每分钟兜底（`schedule_weekly_ai_analysis_tasks`）是在 `with _app.app_context():`
        **里面**调的 —— 嵌套 push 必须同样安全，且同样不许吵。"""
        import services.task_worker_service as worker

        lines: list = []
        monkeypatch.setattr(worker, "log_print", lambda msg, *a, **k: lines.append(str(msg)))
        seeded = self._seed_with_intent()

        with flask_app.app_context():
            worker.wake_waiting_analysis_intents_safely()

        assert _analysis_tasks(seeded["group_key"]), "已经在上下文里时反而不转交了"
        self._assert_no_wake_failure_logged(lines)


# ==========================================================================
#  三、用户已经手工跑成了 → 意图自动作废
# ==========================================================================


class TestTheIntentIsRetiredWhenTheUserAlreadyRanIt:
    def test_a_run_created_after_the_intent_retires_it(self):
        """**不许变成第二次付费运行。**

        用户在等待期间从别处（另一个标签页、点过一次之后同步恰好结束）把同一份输入
        跑成了 —— 这条意图的使命就结束了，必须作废，而不是再跑一遍。
        """
        from services.task_worker_queue_service import (
            register_waiting_analysis_intent,
            wake_waiting_analysis_intents,
        )

        seeded = _seed(with_sync_task=True, sync_status="completed")
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            # 已经跑成的那一次：run 建在意图之后。
            db.session.add(
                AiAnalysisRun(
                    project_id=seeded["project_id"], target_type="weekly",
                    target_id=seeded["config_id"], target_key=seeded["group_key"],
                    status="succeeded", response_mode="streaming", trigger_source="manual",
                    created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
                )
            )
            db.session.commit()

            wake_waiting_analysis_intents()

            assert _analysis_tasks(seeded["group_key"]) == [], "已经跑成了却还排了一条分析任务"
            assert _pending_intents(seeded["group_key"]) == [], "作废的意图仍停在 pending"

    def test_an_active_run_makes_the_intent_stop_blocking_the_button(self):
        """已经有一次分析在跑（不管它什么时候建的）→ 意图不再拦着用户手动点。

        拦着的话用户只会看到「已登记，等着」而那次分析早就跑了 —— 页面上两句话互相矛盾。
        """
        from services.task_worker_queue_service import (
            effective_waiting_analysis_intent,
            register_waiting_analysis_intent,
        )

        seeded = _seed()
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            assert effective_waiting_analysis_intent(seeded["group_key"]) is not None

            db.session.add(
                AiAnalysisRun(
                    project_id=seeded["project_id"], target_type="weekly",
                    target_id=seeded["config_id"], target_key=seeded["group_key"],
                    status="running", response_mode="streaming", trigger_source="manual",
                    created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
                )
            )
            db.session.commit()

            assert effective_waiting_analysis_intent(seeded["group_key"]) is None, (
                "已经有一次分析在跑，意图还在挡着用户"
            )


# ==========================================================================
#  四、过期：同步一直没结束，不许把人扣在这儿
# ==========================================================================


class TestAnIntentCannotWaitForever:
    # 用**固定**的年龄（2 小时）当判据，而不是 `WAITING_INTENT_TTL_SECONDS + 60`：
    # 拿被测常量自己算年龄的话，把常量调到 10**9 这条用例照样绿（它跟着一起变了）——
    # 那正是「变异验证」要抓的东西。上限也一并钉住：等 2 小时就该放弃了。
    STALE_INTENT_AGE_SECONDS = 2 * 3600

    def test_the_intent_does_not_wait_for_more_than_two_hours(self):
        from services.task_worker_queue_service import WAITING_INTENT_TTL_SECONDS

        assert WAITING_INTENT_TTL_SECONDS <= self.STALE_INTENT_AGE_SECONDS, (
            f"等待意图的上限是 {WAITING_INTENT_TTL_SECONDS} 秒 —— 用户在页面上最多等这么久，"
            "调大它等于把用户扣在一句「正在等同步」上"
        )

    def test_an_old_intent_stops_blocking_and_gets_swept(self):
        from services.task_worker_queue_service import (
            effective_waiting_analysis_intent,
            register_waiting_analysis_intent,
            wake_waiting_analysis_intents,
        )

        seeded = _seed()
        stale = datetime.now(timezone.utc) - timedelta(seconds=self.STALE_INTENT_AGE_SECONDS)
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            BackgroundTask.query.filter_by(
                task_type="weekly_ai_waiting", file_path=seeded["group_key"], status="pending"
            ).update({"created_at": stale})
            db.session.commit()

            assert effective_waiting_analysis_intent(seeded["group_key"]) is None, (
                "过期的意图还在挡着用户手动点"
            )
            wake_waiting_analysis_intents()
            assert _pending_intents(seeded["group_key"]) == [], "过期的意图没有被收掉"

    def test_a_swept_intent_does_not_start_an_analysis(self):
        """过期 = 不再等 —— 但**也不许**因此偷偷开始一次分析（钱不能这么花）。"""
        from services.task_worker_queue_service import (
            register_waiting_analysis_intent,
            wake_waiting_analysis_intents,
        )

        seeded = _seed(with_sync_task=True, sync_status="completed")
        stale = datetime.now(timezone.utc) - timedelta(seconds=self.STALE_INTENT_AGE_SECONDS)
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            BackgroundTask.query.filter_by(
                task_type="weekly_ai_waiting", file_path=seeded["group_key"], status="pending"
            ).update({"created_at": stale})
            db.session.commit()

            wake_waiting_analysis_intents()

            assert _analysis_tasks(seeded["group_key"]) == [], "过期的意图被唤醒成了一次分析"


# ==========================================================================
#  五、状态查询：页面靠它把「那次运行」接上
# ==========================================================================


class TestTheStatusForThePage:
    def test_no_intent_and_no_run_means_nothing_to_wait_for(self):
        from services.task_worker_queue_service import describe_waiting_analysis

        seeded = _seed()
        with flask_app.app_context():
            status = describe_waiting_analysis(seeded["config_id"], seeded["group_key"])

        assert status["waiting"] is False, status
        assert status["run_id"] is None, status
        assert status["message"], "没有可说的也要给一句（否则页面只能自己拼文案）"

    def test_a_pending_intent_is_reported_as_waiting(self):
        from services.task_worker_queue_service import (
            describe_waiting_analysis,
            register_waiting_analysis_intent,
        )

        seeded = _seed()
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            status = describe_waiting_analysis(seeded["config_id"], seeded["group_key"])

        assert status["waiting"] is True, status
        assert status["run_id"] is None, status

    def test_a_started_run_is_reported_with_its_run_id(self):
        """页面要能拿到**运行号**：拿到它才能附着上去看进度与结论。"""
        from services.task_worker_queue_service import (
            describe_waiting_analysis,
            register_waiting_analysis_intent,
        )

        seeded = _seed()
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            run = AiAnalysisRun(
                project_id=seeded["project_id"], target_type="weekly",
                target_id=seeded["config_id"], target_key=seeded["group_key"],
                status="running", response_mode="blocking", trigger_source="scheduled",
                created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
            )
            db.session.add(run)
            db.session.commit()
            status = describe_waiting_analysis(seeded["config_id"], seeded["group_key"])

        assert status["waiting"] is True, status
        assert status["run_id"] == run.id, status


# ==========================================================================
#  六、接线：同步收尾真的会去唤醒，装载时真的不会把意图当任务排队
# ==========================================================================


class TestTheWiring:
    def test_finishing_a_sync_hands_the_pending_intent_off(self):
        """**行为断言**：跑完一条 `weekly_sync`，登记过的那次分析必须自己开始。

        这是整条链子的最后一公里：意图登记得再对，同步收尾时没人去看它，用户还是只能
        自己再点一次。
        """
        import services.task_worker_service as worker

        seeded = _seed(with_sync_task=False)
        with flask_app.app_context():
            from services.task_worker_queue_service import register_waiting_analysis_intent

            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            sync_row = BackgroundTask(
                task_type="weekly_sync", commit_id=str(seeded["config_id"]),
                priority=3, status="pending",
            )
            db.session.add(sync_row)
            db.session.commit()
            sync_task_id = sync_row.id

            original = worker._process_weekly_version_sync
            worker._process_weekly_version_sync = lambda config_id: None  # 结局按 completed
            try:
                worker._handle_weekly_sync_task(
                    {"type": "weekly_sync", "config_id": seeded["config_id"],
                     "task_id": sync_task_id}
                )
            finally:
                worker._process_weekly_version_sync = original

            assert _analysis_tasks(seeded["group_key"]), (
                "同步跑完了，登记的那次分析没有被唤醒 —— 用户还是只能自己再点一次"
            )

    def test_loading_pending_tasks_does_not_enqueue_the_intent(self):
        """意图行**不是**可执行任务：装载时不许把它当任务塞进内存队列。

        塞进去的后果有两个，都会**静默**：worker 的 if/elif 链没有这个类型，取到它只会
        打一行「后台任务完成」然后什么也不做；而那一行会一直停在 pending（没人写终态），
        于是下次调度去重时它永远占着位。
        """
        import services.task_worker_service as worker
        from services.task_worker_queue_service import register_waiting_analysis_intent

        class _RecordingQueue:
            def __init__(self):
                self.payloads = []

            def put(self, wrapper):
                self.payloads.append(getattr(wrapper, "task_data", None))

            def qsize(self):
                return len(self.payloads)

            def task_done(self):
                return None

        seeded = _seed(with_sync_task=False)
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            recorder = _RecordingQueue()
            original = worker.background_task_queue
            original_auto = worker.check_and_create_auto_sync_tasks
            worker.background_task_queue = recorder
            worker.check_and_create_auto_sync_tasks = lambda: None
            try:
                worker.load_pending_tasks()
            finally:
                worker.background_task_queue = original
                worker.check_and_create_auto_sync_tasks = original_auto

            kinds = [payload.get("type") for payload in recorder.payloads if isinstance(payload, dict)]
            assert "weekly_ai_waiting" not in kinds, (
                f"等待意图被当成可执行任务排进了内存队列：{kinds}"
            )


# ==========================================================================
#  七、手工入口：被拦下的时候把意图登记上，别再让用户自己再点
# ==========================================================================


class TestTheManualEntryRegistersTheIntent:
    def test_the_blocked_stream_registers_the_intent_and_says_so(self):
        """闸门拦下 → 登记意图 → SSE 里**如实说**「已登记、同步结束会自动开始」。

        这是审计文档那一句「不应静默失败」的落点：用户必须知道接下来会发生什么
        （会自动开始），而不是「等着，再点一次试试」。
        """
        from services.ai_analysis_service import set_project_api_key, stream_weekly_analysis

        seeded = _seed(with_sync_task=True, sync_status="processing")
        with flask_app.app_context():
            set_project_api_key(seeded["project_id"], "test-key")
            events = list(stream_weekly_analysis(seeded["config_id"], trigger_source="manual"))

            waiting = [line for line in events if line.startswith("event: waiting")]
            assert waiting, f"闸门没有发 waiting 事件：{events}"
            payload = json.loads(waiting[0].split("data: ", 1)[1])
            assert payload["reason"] == "sync_in_flight", payload
            assert payload.get("intent_registered") is True, (
                f"被拦下了却没有登记意图，用户只能自己再点一次：{payload}"
            )
            assert "自动" in payload["message"], payload["message"]

            assert len(_pending_intents(seeded["group_key"])) == 1, "SSE 说登记了，库里没有"
            assert _analysis_tasks(seeded["group_key"]) == [], "被拦下时不该排分析任务"
            assert (
                AiAnalysisRun.query.filter_by(target_key=seeded["group_key"]).count() == 0
            ), "被拦下却建了一条 run"

    def test_a_second_click_while_the_handoff_is_pending_does_not_start_a_run(self):
        """同步已结束、但那一次还没跑起来时再点一次：**只附着，不新建 run**。

        新建的后果是双份付费：排队的那条稍后执行时，手工那条已经跑完、认领也放开了，
        `_create_run` 于是照常建一条新的 —— 同一份输入跑两遍。
        """
        from services.ai_analysis_service import set_project_api_key, stream_weekly_analysis
        from services.task_worker_queue_service import register_waiting_analysis_intent

        seeded = _seed(with_sync_task=True, sync_status="completed")
        with flask_app.app_context():
            set_project_api_key(seeded["project_id"], "test-key")
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
            events = list(stream_weekly_analysis(seeded["config_id"], trigger_source="manual"))

            waiting = [line for line in events if line.startswith("event: waiting")]
            assert waiting, f"已登记的意图没有被认出来，手工路径新建了一次运行：{events}"
            payload = json.loads(waiting[0].split("data: ", 1)[1])
            assert payload.get("intent_registered") is True, payload
            assert (
                AiAnalysisRun.query.filter_by(target_key=seeded["group_key"]).count() == 0
            ), "已经登记过一次分析，却又建了一条 run（= 第二次付费）"


# ==========================================================================
#  八、前端跟得上：页面要认「已登记」这个事实，并在那次运行开始时接上去
# ==========================================================================

WEEKLY_TEMPLATES = (
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)


def _waiting_handler(source: str) -> str:
    """切出 `waiting` 监听器的函数体（边界用**下一个监听器**，与既有那条同一手法）。"""
    body = source[source.index("addEventListener('waiting'"):]
    boundary = body.find("addEventListener(", len("addEventListener('waiting'"))
    return body if boundary < 0 else body[:boundary]


def _strip_line_comments(text: str) -> str:
    """剥掉 `//` 行注释再断言。

    **必须先剥注释**：这一段的注释里逐字写着字段名（`（`intent_registered`）`），
    不剥的话「页面用了服务端这个事实」这条断言命中的是注释 —— 代码把分支删掉它照样绿。
    """
    kept = []
    for line in text.splitlines():
        cut = line.find("//")
        kept.append(line if cut < 0 else line[:cut])
    return "\n".join(kept)


class TestThePageFollowsTheIntent:
    def test_the_waiting_handler_uses_the_server_side_registration_fact(self):
        """服务端说「已经登记」时，页面必须照着做：保持禁用 + 开始轮询。

        判据用**服务端给的字段**（`payload.intent_registered`）而不是「reason 是不是
        sync_in_flight」—— 后者在「同步已结束、那次还没跑起来」时也是同一个 reason，
        而那时页面该做的是同一件事（继续等），只是不能自己再建一次运行。
        """
        root = Path(__file__).resolve().parents[1]
        for rel in WEEKLY_TEMPLATES:
            source = (root / rel).read_text(encoding="utf-8")
            handler = _strip_line_comments(_waiting_handler(source))
            assert "payload.intent_registered" in handler, (
                f"{rel} 的 waiting 分支不看服务端给的「已登记」事实 —— "
                "用户还是只能自己盯着按钮再点一次"
            )
            assert "startWeeklyAiWaitingPoll()" in handler, f"{rel} 没有开始轮询那次登记"
            assert "startWeeklyAiWaitingPoll" in source and "stopWeeklyAiWaitingPoll" in source
            assert "/waiting`" in source or "/waiting'" in source, (
                f"{rel} 没有问服务端「那次登记到哪一步了」"
            )
            # 轮询必须在「用户自己又发起一次」时停掉：不停的话它会一直轮询下去，
            # 而且会把一次手工发起的分析说成「等同步」。
            start_body = source[source.index("function startWeeklyAiAnalysis"):]
            assert "stopWeeklyAiWaitingPoll()" in start_body[:2000], (
                f"{rel} 重新发起分析时没有停掉等待轮询"
            )


# ==========================================================================
#  九、轮询端点：页面靠它知道「那次分析开始了没有」
# ==========================================================================


class TestTheWaitingEndpoint:
    """`GET /ai-analysis/weekly/<id>/waiting` —— 只读、要权限、说清三态。

    认证链挂在 `before_request` 上（`test_client` 走不到 view 就先被 401 拦住），所以像
    `tests/test_ai_config_routes.py` 那样**直接调 view**：本用例要守的是 view 内部那两条
    —— 权限判定与三态回答。
    """

    def _call(self, config_id):
        from flask import make_response

        from app import app as app_obj

        view = app_obj.view_functions["ai_analysis_routes.ai_weekly_waiting"]
        with app_obj.test_request_context(f"/ai-analysis/weekly/{config_id}/waiting"):
            return make_response(view(config_id=config_id))

    def test_it_reports_a_pending_intent_as_waiting(self, monkeypatch):
        import routes.ai_analysis_routes as ai_routes
        from services.task_worker_queue_service import register_waiting_analysis_intent

        seeded = _seed()
        with flask_app.app_context():
            register_waiting_analysis_intent(seeded["config_id"], seeded["group_key"])
        monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)

        body = self._call(seeded["config_id"]).get_json()

        assert body["success"] is True, body
        assert body["waiting"] is True, body
        assert body["run_id"] is None, body
        assert body["message"], "没有可说的也要给一句（页面不许自己拼文案）"

    def test_it_reports_the_run_id_once_the_analysis_started(self, monkeypatch):
        import routes.ai_analysis_routes as ai_routes

        seeded = _seed()
        with flask_app.app_context():
            run = AiAnalysisRun(
                project_id=seeded["project_id"], target_type="weekly",
                target_id=seeded["config_id"], target_key=seeded["group_key"],
                status="running", response_mode="blocking", trigger_source="scheduled",
            )
            db.session.add(run)
            db.session.commit()
            run_id = run.id
        monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: True)

        body = self._call(seeded["config_id"]).get_json()

        assert body["waiting"] is True, body
        assert body["run_id"] == run_id, body

    def test_it_refuses_without_project_access(self, monkeypatch):
        import routes.ai_analysis_routes as ai_routes

        seeded = _seed()
        monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: False)

        response = self._call(seeded["config_id"])

        assert response.status_code == 403, response.status_code
        assert response.get_json()["success"] is False


# ---------------------------------------------------------------------------
#  轮询的**行为**（用 node 真跑抽出来的那两个函数，与既有
#  tests/test_ai_drawer_stream_state.py 同一手法：假定时器 + 假 fetch）
# ---------------------------------------------------------------------------

_POLL_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');   // 从模板里抽出来的两个函数
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function runCase(kase) {
    const responses = kase.responses.slice();
    const timers = new Map();
    const fetched = [];
    let latest = 0;
    let seq = 0;
    const button = { disabled: true };
    const badge = [];
    const meta = [];
    const sandbox = {
        console: console,
        setInterval: (fn, ms) => { const id = ++seq; timers.set(id, fn); return id; },
        clearInterval: (id) => { timers.delete(id); },
        fetch: (url) => {
            fetched.push(url);
            const next = responses.shift() || { success: true, waiting: true, run_id: null };
            return Promise.resolve({ json: () => Promise.resolve(next) });
        },
        document: { getElementById: () => button },
        setWeeklyAiStatusBadge: (text, tone) => badge.push([text, tone]),
        setWeeklyAiOutput: (text) => meta.push(text),
        setWeeklyAiMeta: (text) => meta.push(text),
        loadWeeklyAiLatest: async () => { latest += 1; sandbox.weeklyAiRunActive = kase.run_still_active; return false; },
        refreshWeeklyAiLatest: async () => { latest += 1; sandbox.weeklyAiRunActive = kase.run_still_active; return false; },
        configId: 7,
        weeklyAiCurrentConfigId: 7,
        weeklyAiWaitingTimer: null,
        weeklyAiWaitingPolls: 0,
        weeklyAiRunActive: true,
        weeklyAiHasCached: false,
        WEEKLY_AI_WAITING_POLL_MS: 15000,
        WEEKLY_AI_WAITING_MAX_POLLS: 3,
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox, { filename: 'weekly_ai_waiting_poll.js' });
    // `startWeeklyAiWaitingPoll` 不是 async：它只注册定时器，第一帧由定时器回调触发。
    sandbox.startWeeklyAiWaitingPoll();
    return (async () => {
        for (let i = 0; i < kase.ticks; i += 1) {
            const pending = Array.from(timers.entries());
            if (!pending.length) break;
            await pending[0][1]();
        }
        return {
            polling: timers.size > 0,
            latest: latest,
            disabled: button.disabled,
            url: fetched[0] || '',
            badge: badge,
        };
    })();
}

(async () => {
    const out = {};
    for (const kase of cases) { out[kase.name] = await runCase(kase); }
    process.stdout.write(JSON.stringify(out));
})();
"""


def _function_source(script: str, name: str) -> str:
    """从模板的 `<script>` 里切出 `function <name>(…) { … }` 的完整源码。"""
    marker = f"function {name}("
    start = script.index(marker)
    depth = 0
    for index in range(script.index("{", start), len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[start: index + 1]
    raise AssertionError(f"{name} 的函数体没有闭合")


def _run_poll_in_node(rel_path: str, cases: list) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的断言")
    root = Path(__file__).resolve().parents[1]
    script = (root / rel_path).read_text(encoding="utf-8")
    source = "\n".join(
        _function_source(script, name)
        for name in ("stopWeeklyAiWaitingPoll", "startWeeklyAiWaitingPoll")
    )
    workdir = root / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "sync_wait_poll_driver.js"
    payload = workdir / "sync_wait_poll_cases.json"
    source_file = workdir / "sync_wait_poll_source.js"
    driver.write_text(_POLL_DRIVER, encoding="utf-8")
    source_file.write_text(source, encoding="utf-8")
    payload.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        ["node", str(driver), str(source_file), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, f"node 跑轮询失败：{result.stderr}"
    return json.loads(result.stdout)


class TestTheWaitingPollBehaviour:
    """轮询的三个出路各要在**真跑**里成立（静态断言只能说明「那句话在文件里」）。"""

    CASES = [
        {"name": "waiting", "ticks": 1, "run_still_active": True,
         "responses": [{"success": True, "waiting": True, "run_id": None}]},
        {"name": "started_running", "ticks": 1, "run_still_active": True,
         "responses": [{"success": True, "waiting": True, "run_id": 42}]},
        {"name": "started_finished", "ticks": 1, "run_still_active": False,
         "responses": [{"success": True, "waiting": True, "run_id": 42}]},
        {"name": "gone", "ticks": 1, "run_still_active": True,
         "responses": [{"success": True, "waiting": False, "run_id": None}]},
        {"name": "blip", "ticks": 2, "run_still_active": True,
         "responses": [{"success": False}, {"success": True, "waiting": True, "run_id": None}]},
    ]

    def test_the_poll_keeps_waiting_while_the_intent_stands(self):
        results = _run_poll_in_node(WEEKLY_TEMPLATES[0], [self.CASES[0], self.CASES[4]])
        waiting = results["waiting"]
        assert waiting["polling"] is True, "还在等却把轮询停了"
        assert waiting["latest"] == 0, "还没开始就去取结论"
        assert waiting["disabled"] is True, "还在等却把按钮放回可点 —— 用户又会去点"
        assert waiting["url"].endswith("/waiting"), waiting["url"]
        # 网络抖一下（success=false）不算登记结束：继续轮询，也不误报。
        assert results["blip"]["polling"] is True, "一次读失败就被当成「登记结束了」"

    def test_the_poll_attaches_when_the_analysis_starts(self):
        results = _run_poll_in_node(
            WEEKLY_TEMPLATES[0], [self.CASES[1], self.CASES[2]]
        )
        running = results["started_running"]
        assert running["polling"] is False, "那次分析已经开始了，还在轮询"
        assert running["latest"] == 1, "没有去把那次运行接上（取一次最新结论/进度）"
        assert running["disabled"] is True, "那次还在跑，按钮却被放回可点"
        finished = results["started_finished"]
        assert finished["polling"] is False, finished
        assert finished["latest"] == 1, finished
        assert finished["disabled"] is False, (
            "那次已经跑完了，按钮却还是灰的（用户只能以为页面卡住了）"
        )

    def test_the_poll_gives_the_button_back_when_nothing_is_left_to_wait_for(self):
        results = _run_poll_in_node(WEEKLY_TEMPLATES[0], [self.CASES[3]])
        gone = results["gone"]
        assert gone["polling"] is False, "登记已经结束，轮询还在跑"
        assert gone["disabled"] is False, "登记结束了却不把按钮放回可点"
        assert gone["badge"] and gone["badge"][0][0] == "等待同步", gone["badge"]


# ==========================================================================
#  十、被唤醒的那次运行要记成「手动」——是用户点出来的，不是系统自己排的
# ==========================================================================


class TestTheWokenRunIsRecordedAsManual:
    """用量面板按 `trigger_source` 显示「手动 / 定时」。

    被闸门挡下、再由登记唤醒的那一次，**是用户点出来的**，只是被推迟了。记成「定时」
    与「调度器自己跑的」在面板上长得一模一样 —— 与那行说谎的调度日志同一类问题。
    """

    def _stub_pipeline(self, monkeypatch, seeded):
        import services.ai_analysis_service as ai_service

        payload = {
            "mode": "weekly",
            "scope": "full",
            "focus": {"key": "all", "label": ""},
            "group": {
                "key": seeded["group_key"],
                "base_name": "W",
                "project_id": seeded["project_id"],
                "config_ids": list(seeded["config_ids"]),
                "start_time": None,
                "end_time": None,
            },
            "summary": {"total_files": 3, "delta_files": 3},
        }
        monkeypatch.setattr(ai_service, "build_weekly_payload", lambda *a, **k: (payload, None, None))
        monkeypatch.setattr(ai_service, "budget_gate_reason", lambda *a, **k: "")
        monkeypatch.setattr(ai_service, "weekly_sync_in_flight", lambda *a, **k: "")
        monkeypatch.setattr(ai_service, "_get_project_api_key", lambda _pid: "test-key")
        monkeypatch.setattr(
            ai_service, "_execute_analysis",
            lambda *a, **k: {"status": "succeeded", "report_markdown": ""},
        )

    def test_a_manual_trigger_is_recorded_as_manual(self, monkeypatch):
        import services.ai_analysis_service as ai_service

        seeded = _seed()
        self._stub_pipeline(monkeypatch, seeded)
        with flask_app.app_context():
            ai_service.run_weekly_analysis_background(seeded["config_id"], trigger_source="manual")
            run = AiAnalysisRun.query.filter_by(target_key=seeded["group_key"]).first()

        assert run is not None, "没有被唤醒的那次运行记录"
        assert run.trigger_source == "manual", (
            f"用户点出来的那一次被记成了「{run.trigger_source}」—— 用量面板上会显示成系统自己跑的"
        )

    def test_the_default_stays_scheduled(self, monkeypatch):
        """反面：调度器排的仍记「定时」（不改默认值，免得把自动分析说成用户点的）。"""
        import services.ai_analysis_service as ai_service

        seeded = _seed()
        self._stub_pipeline(monkeypatch, seeded)
        with flask_app.app_context():
            ai_service.run_weekly_analysis_background(seeded["config_id"])
            run = AiAnalysisRun.query.filter_by(target_key=seeded["group_key"]).first()

        assert run is not None, "没有那次运行记录"
        assert run.trigger_source == "scheduled", run.trigger_source

    def test_a_manual_run_is_not_blocked_by_the_auto_weekly_switch(self, monkeypatch):
        """「自动分析」开关只管**自动**：用户点出来的那一次不该被它否掉。

        被闸门推迟的那次分析如果被这个开关否掉，用户点过一次之后就什么也不会发生
        —— 「点一次就够」当场失效。
        """
        import services.ai_analysis_service as ai_service

        seeded = _seed()
        self._stub_pipeline(monkeypatch, seeded)
        monkeypatch.setattr(
            ai_service, "get_project_analysis_config",
            lambda _pid: {"auto_weekly_enabled": False},
        )
        with flask_app.app_context():
            manual = ai_service.run_weekly_analysis_background(
                seeded["config_id"], trigger_source="manual"
            )

        assert manual.get("reason") != "auto_weekly_disabled", manual

    def test_the_manual_marker_travels_with_the_task_payload(self, monkeypatch):
        """**这一跳最容易丢**：`manual` 要随任务载荷走到执行侧，否则前面记对了、
        真正落库的那一次还是「定时」。"""
        import services.task_worker_service as worker
        from services.task_worker_queue_service import create_weekly_ai_analysis_task

        class _Recorder:
            def __init__(self):
                self.payloads = []

            def put(self, wrapper):
                self.payloads.append(getattr(wrapper, "task_data", None))

            def qsize(self):
                return len(self.payloads)

        seeded = _seed()
        recorder = _Recorder()
        with flask_app.app_context():
            monkeypatch.setattr(worker, "background_task_queue", recorder)
            monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: False)
            create_weekly_ai_analysis_task(
                seeded["config_id"], group_key=seeded["group_key"], trigger_source="manual"
            )

            kinds = [p.get("trigger_source") for p in recorder.payloads if isinstance(p, dict)]
            assert "manual" in kinds, f"载荷里没有带上「手动」这个标记：{kinds}"

    def test_the_handler_passes_the_marker_to_the_run(self, monkeypatch):
        """执行侧那一跳：handler 要把载荷里的 `trigger_source` 交给 `run_weekly_analysis_background`。"""
        import services.task_worker_service as worker

        seen = {}

        def _fake_run(config_id, task_id=None, trigger_source="scheduled"):
            seen["trigger_source"] = trigger_source
            return {"status": "succeeded"}

        with flask_app.app_context():
            monkeypatch.setattr(worker, "run_weekly_analysis_background", _fake_run)
            monkeypatch.setattr(worker, "update_task_status_with_retry", lambda *a, **k: None)
            worker._handle_weekly_ai_analysis_task(
                {"type": "weekly_ai_analysis", "config_id": 1234, "task_id": None,
                 "trigger_source": "manual"}
            )

        assert seen.get("trigger_source") == "manual", (
            f"handler 把标记丢了（{seen}）—— 落库的那次运行还是「定时」"
        )
