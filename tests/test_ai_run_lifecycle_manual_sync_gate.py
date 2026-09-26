# -*- coding: utf-8 -*-
"""手工分析路径缺同步闸门：同步还在写缓存时，点「开始分析」照样开跑。

## 缺陷形态（实测）

`run_weekly_analysis_background`（后台路径）有 `weekly_sync_in_flight(group_config_ids(config))`
这道闸，而手工路径（抽屉里那个按钮）**全函数体没有**。实测：run 13 于 10:50:08 创建时，
同组 config 2 的 `weekly_sync` 任务 355 正 processing（10:49:28→11:06:34）—— 那次分析
建立在一份**还会变的跨仓库缓存**上。

## P0-01 之后「手工路径」是哪里

那条老的手工流式入口（`stream_weekly_analysis`）**已经没有生产调用方了**，已被删除。
现在手工点击走的是 **`POST /ai-analysis/weekly/<id>/jobs` → `POST /jobs` →
`job_service.create_or_attach_job` → `job_service._dispatch`**，闸门在这一跳上
（`_dispatch` 里的 `weekly_sync_in_flight(group_config_ids(config))`，与后台那道
**同一个判据、同一份实现**）。所以本文件的入口从「读 SSE 文本」换成「建一条 job →
看它落到哪个状态 → 需要的话把它的任务真的跑一次」。

## 为什么这道闸必须按「整批」判

变更清单来自这一批**全部**仓库的缓存行（`_summarize_weekly_files` 按 config_ids 取），
所以只看自己那一个仓库的同步是拦不住的：另一个仓库还在写的时候照样会漏文件，
而漏文件是**静默**的（提示词里的「共 N 个文件」跟着变小）。

## 这一组守什么

1. 同步在跑时手工路径**不建 run、不发模型请求**，并且**明确说出**「等待 Diff 同步完成」
   —— 静默失败与「装作开始分析」都不许（后者会让用户以为花钱了，前者让用户以为按钮坏了）；
2. 闸门用整批判据（兄弟仓库的同步同样拦）；
3. 同步不在跑时不许被这道闸门挡住（否则手工分析永久停摆）；
4. 前端跟得上：两份周版本抽屉都认识这个信号。
"""
from __future__ import annotations

# ruff: noqa: I001 —— 本文件的**导入顺序是语义要求**（与 `tests/test_ai_job_protocol.py`
# 同一条处置）：`services.task_worker_service` 必须在 `services.task_worker_queue_service`
# **之前**加载。两者互为环形依赖 —— 队列服务在它的模块级（第 31 行）就
# `import services.task_worker_service as worker`，而 worker 要
# `from services.task_worker_queue_service import TASK_LEASE_SECONDS`，那个常量定义在队列
# 服务的第 78 行。队列先加载的话，worker 去取它时那一行还没执行到 → ImportError。
# isort 要的字母序恰好与这个要求相反。

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import services.ai.job_service as job_service
import services.task_worker_service as worker
import services.task_worker_queue_service as queue_service
import services.ai_analysis_service as ai_service
from app import app as flask_app
from app import create_tables, db
from models import BackgroundTask, Project, Repository, WeeklyVersionConfig
from models.ai_analysis import (
    MODE_INCREMENTAL,
    STATE_QUEUED,
    STATE_WAITING_SNAPSHOT,
    AiAnalysisJob,
    AiAnalysisRun,
)
from services.ai.project_config_source import build_weekly_group_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: 本文件建出来的 job 行（收尾时一并删掉，见 `_cleanup_runs`）。
_CREATED_JOBS: list = []
# 两份走周版本流的手工入口（第三份 commit_diff_new.html 走单提交流，不在这条路上）。
WEEKLY_TEMPLATES = (
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed(*, sync_on: str = "primary", sync_status: str = "processing",
          age_minutes: int = 2, busy_worker: bool = False) -> dict:
    """一个项目 + 两条同窗口的周版本配置（模拟**多仓库分组**），可选一条同步任务。

    两条共用同一个**版本名**（`f"{名字} - {仓库名}"`，与 `weekly_version_logic` 建
    多仓库配置时逐字同形）：批次判据是「同项目 + 同窗口 + **同版本名**」，两个各不
    相同的随机名字在平台眼里是两个版本，那样造出来的不是「一整批」。

    `sync_on="sibling"` 把同步任务挂在**另一条**配置上 —— 手工分析的入口是 primary，
    而闸门必须按整批判，否则这条用例就会漏过去。

    `busy_worker=True` 再补一条**正被 worker 拿着**的任务（`processing` + `started_at`）：
    单线程的 worker 被它占住时，排在后面的同步**不会开始写缓存** —— 真机实测里就是
    一条 AI 分析占住了它（任务 381 在 run 15 结束后 0.085 秒才开始）。
    """
    now = datetime.now(timezone.utc)
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("LC"), name="运行生命周期用例")
        db.session.add(project)
        db.session.flush()
        configs = []
        base_name = _uid("W")
        for index in range(2):
            repository = Repository(
                project_id=project.id, name=_uid(f"repo{index}"), type="git",
                url="https://example.invalid/r.git", branch="main",
                resource_type="code" if index else "table",
            )
            db.session.add(repository)
            db.session.flush()
            config = WeeklyVersionConfig(
                project_id=project.id, repository_id=repository.id,
                name=f"{base_name} - {repository.name}",
                description="", branch="main",
                start_time=now - timedelta(days=7), end_time=now,
                is_active=True, auto_sync=True, status="active",
            )
            db.session.add(config)
            configs.append(config)
        db.session.flush()

        task = None
        if sync_on:
            target = configs[0] if sync_on == "primary" else configs[1]
            task = BackgroundTask(
                task_type="weekly_sync", commit_id=str(target.id),
                priority=3, status=sync_status,
                created_at=now - timedelta(minutes=age_minutes),
            )
            db.session.add(task)
        busy = None
        if busy_worker:
            busy = BackgroundTask(
                task_type="weekly_ai_analysis", commit_id=str(uuid.uuid4().int % 100000),
                priority=6, status="processing", created_at=now, started_at=now,
            )
            db.session.add(busy)
        # **必须有 API key**：没有它，流会在「Project API key not configured」那道
        # 更早的闸门上停住 —— 于是「同步闸门拦住了」与「压根没配 key」在测试里长得
        # 一模一样，这一组用例会变成永远为真的空断言。
        ai_service.set_project_api_key(project.id, "sk-test", updated_by="tester")
        db.session.commit()
        return {
            "project_id": project.id,
            "config_ids": [cfg.id for cfg in configs],
            "primary_config_id": configs[0].id,
            "sibling_config_id": configs[1].id,
            "task_id": task.id if task else None,
            "busy_task_id": busy.id if busy else None,
        }


def _release_busy_worker(seeded: dict) -> None:
    """删掉 `busy_worker=True` 造出来的那一行。

    **留着它的后果是静默的**：测试库是会话级共用的，而「执行侧忙」会让后面所有
    「排队中的同步该拦」的用例看到「不忙」—— 它们要么假绿要么假红。
    """
    task_id = seeded.get("busy_task_id")
    if not task_id:
        return
    with flask_app.app_context():
        row = db.session.get(BackgroundTask, task_id)
        if row is not None:
            db.session.delete(row)
        db.session.commit()


def _events(text: str) -> list:
    """把 SSE 文本拆成 `[(event, payload), …]`（中文是 `\\uXXXX`，必须 json.loads）。"""
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name, payload = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[len("data:"):].strip())
        events.append((name, payload))
    return events


def _stub(monkeypatch, seeded: dict, calls: list) -> None:
    """把 payload 构造与引擎都换成桩：这一组只关心**闸门有没有放行**。

    引擎桩会**记账** —— 「同步还在跑却发起了模型调用」由 `calls == []` 判，
    这是一条响亮的断言；靠事后数 run 行是判不准的（「先建 run 再被拦」的实现
    同样会留下 run 行，但报出来的原因是错的）。
    """
    payload = {
        "mode": "weekly",
        "scope": "full",
        "focus": {"key": "all", "label": ""},
        "group": {
            "key": f"g-{seeded['primary_config_id']}",
            "base_name": "W",
            "project_id": seeded["project_id"],
            "config_ids": list(seeded["config_ids"]),
            "start_time": None,
            "end_time": None,
        },
        "summary": {"total_files": 3, "delta_files": 3},
    }
    monkeypatch.setattr(
        ai_service, "build_weekly_payload", lambda *a, **k: (payload, None, None)
    )

    def _execute(*_args, **_kwargs):
        calls.append("execute")
        return {"status": "succeeded", "report_markdown": "结论", "error_message": None}

    monkeypatch.setattr(ai_service, "_execute_analysis", _execute)


def _create_manual_job(config_id: int):
    """走**手工入口那一跳**（P0-01 之后是 `POST /jobs` → `create_or_attach_job`）。

    这一跳只落身份与排程：**不建 run、不发模型请求**。引擎有没有被调用由
    `_drive_the_job_task`（真的把排出去的任务跑一次）来判 —— 这正是原来那条
    「同步还在跑却发起了模型调用」的响亮断言。
    """
    config = db.session.get(WeeklyVersionConfig, config_id)
    result = job_service.create_or_attach_job(
        config=config, requested_mode=MODE_INCREMENTAL, trigger_source="manual"
    )
    if result.job is not None and result.job.id is not None:
        _CREATED_JOBS.append(result.job.id)
    db.session.commit()
    return result


def _analysis_tasks(group_key: str) -> list:
    if not group_key:
        return []
    return (
        BackgroundTask.query.filter(
            BackgroundTask.task_type == "weekly_ai_analysis",
            BackgroundTask.file_path == group_key,
        )
        .order_by(BackgroundTask.id.asc())
        .all()
    )


def _drive_the_job_task(cfg, group_key: str) -> None:
    """把这条 job 排出去的任务**真的跑一次**（worker 末端 → 后台执行入口）。"""
    tasks = _analysis_tasks(group_key)
    assert tasks, "没有排出去的任务，这一跳跑不起来"
    worker._handle_weekly_ai_analysis_task(
        {"type": "weekly_ai_analysis", "config_id": cfg.id, "task_id": tasks[-1].id}
    )


def _says_the_wait_costs_nothing(message: str) -> bool:
    """这句话里有没有「等着的这段时间不花钱」这个**主张**。

    判据落在主张上，不钉字面。同一件事在两个产地各写了一遍 —— 队列服务的日志里是
    「本次没有发起分析，也没有产生任何消耗」，页面拿到的是「当前等待过程不会产生消耗」，
    而**只改措辞**（2026-09-26 就是这么改的）不该让这条用例变红：要防的是
    「不说」和「说反」，不是用哪个字说。
    """
    return re.search(r"(没有|不会|不产生|无需|不必)[^。；]{0,16}消耗", message) is not None


def _cleanup_runs(seeded: dict) -> None:
    """把这一组用例建出来的行删掉。

    **测试库是会话级共用的**（没有逐用例重置），而这一组的引擎桩**不会**走
    `_persist_outcome` —— 留下的 run 会一直是 `running`，然后被
    `tests/test_weekly_ai_auto_trigger_gate.py::test_a_run_interrupted_by_a_restart_is_marked_failed`
    那条**全局计数**断言（`fail_orphaned_analysis_runs() == 1`）算进去，让它在
    「一起跑」时红、单独跑时绿。

    job / 意图行一并收掉，理由同上（一条 `active_key` 非空的 job 会占着唯一索引，
    让**别的**用例建不出同目标的 job）。
    """
    AiAnalysisRun.query.filter_by(target_id=seeded["primary_config_id"]).delete()
    for job_id in list(_CREATED_JOBS):
        row = db.session.get(AiAnalysisJob, job_id)
        if row is not None and row.active_key is not None:
            row.active_key = None
        if row is not None:
            db.session.delete(row)
    _CREATED_JOBS.clear()
    for cfg_id in seeded["config_ids"]:
        cfg = db.session.get(WeeklyVersionConfig, cfg_id)
        if cfg is None:
            continue
        group_key = build_weekly_group_key(cfg)
        for row in _analysis_tasks(group_key):
            db.session.delete(row)
        BackgroundTask.query.filter(
            BackgroundTask.file_path == group_key,
            BackgroundTask.task_type == queue_service.WAITING_INTENT_TASK_TYPE,
        ).delete(synchronize_session=False)
    db.session.commit()


# ==========================================================================
#  一、闸门本身（手工路径）
# ==========================================================================


def test_the_manual_entry_is_blocked_while_the_batch_sync_runs(monkeypatch):
    """**用户报的那一条**：同步正 processing 时点「开始分析」，必须被拦下。

    三件事一起断言，缺一条都会漏掉一种错法：
      * job 停在 `waiting_snapshot` —— 页面据此显示「等待同步」，不是「没有运行号」；
      * 没有排出去任何分析任务、没有建 run —— 服务端没有替用户开始花钱；
      * 引擎一次都没被调用 —— 这是最响亮的那条（「先建 run 再被拦」的实现同样会
        留下 run 行，但报出来的原因是错的）。
    """
    seeded = _seed(sync_on="primary", sync_status="processing")
    calls: list = []
    with flask_app.app_context():
        _stub(monkeypatch, seeded, calls)
        created = _create_manual_job(seeded["primary_config_id"])
        cfg = db.session.get(WeeklyVersionConfig, seeded["primary_config_id"])
        group_key = build_weekly_group_key(cfg)

        assert created.job.state == STATE_WAITING_SNAPSHOT, created.job.state
        assert _analysis_tasks(group_key) == [], "同步还在跑，却排出了分析任务"
        assert calls == [], "同步还在跑，却发起了模型调用"
        assert (
            AiAnalysisRun.query.filter_by(target_id=seeded["primary_config_id"]).count() == 0
        ), "被拦下了却还是留下了一条 run 记录"
        _cleanup_runs(seeded)


def test_the_manual_entry_says_it_is_waiting_for_the_diff_sync(monkeypatch):
    """**不许静默失败**：页面必须明确看到「等着呢、会自动开始」，而不是装作开始了。

    「装作开始」比报错更糟：徽章会变成「分析中」，用户以为钱已经花了。这段话由
    **服务端给**（`job_service.notice_for_waiting` → `describe_waiting_analysis`），
    文案只有一份，页面不许自己编。

    「是哪个同步任务在跑、跑了多久」那两句在**闸门那一层**断言
    （`tests/test_ai_weekly_sync_gate.py`），这里不重复。
    """
    seeded = _seed(sync_on="primary", sync_status="processing", age_minutes=3)
    calls: list = []
    with flask_app.app_context():
        _stub(monkeypatch, seeded, calls)
        created = _create_manual_job(seeded["primary_config_id"])
        notice = job_service.notice_for_waiting(created.job)

        assert created.job.state == STATE_WAITING_SNAPSHOT
        assert notice["waiting"] is True, notice
        message = notice.get("message") or ""
        assert "自动" in message, message
        # 「没有花钱」这句必须说 —— 用户第一件想知道的就是这个。
        assert _says_the_wait_costs_nothing(message), message
        assert calls == [], "被拦下了却发起了模型调用"
        _cleanup_runs(seeded)


def test_the_manual_entry_waits_while_a_pending_sync_is_about_to_write(monkeypatch):
    """**2026-09-22 反转**：同步在排队、而 AI 分析跑在**另一条线程**上时，手工分析要等。

    2026-09-21 这条断言的是 `STATE_QUEUED`，前提是「后台只有一个 worker 线程，那一个被
    占住时排在后头的 `weekly_sync` 不会开始写缓存」（实测：任务 381 在 run 15 结束后
    0.085 秒才开始）。`62b4746` 把 `weekly_ai_analysis` 拆进 `ai_task_queue` + 独立的
    `ai_task_worker` 线程之后，前提没了：那条 pending 的同步下一秒就会在**通用线程**上
    开跑并开始写缓存，而分析是在 AI 线程上跑的、并不会占住通用线程。

    放行的后果不是「晚一点」，而是分析在一份**写了一半**的缓存上开跑 —— 变更清单静默
    缺文件。完整来龙去脉见 `tests/test_ai_weekly_sync_gate_scope.py` 的模块 docstring。
    """
    seeded = _seed(sync_on="primary", sync_status="pending", busy_worker=True)
    calls: list = []
    try:
        with flask_app.app_context():
            _stub(monkeypatch, seeded, calls)
            created = _create_manual_job(seeded["primary_config_id"])
            cfg = db.session.get(WeeklyVersionConfig, seeded["primary_config_id"])
            group_key = build_weekly_group_key(cfg)

            assert created.job.state == STATE_WAITING_SNAPSHOT, (
                f"一条马上要写缓存的同步没能挡住手工分析：{created.job.state}"
            )
            assert _analysis_tasks(group_key) == [], "同步还在排队，却排出了分析任务"
            assert calls == [], "同步还在排队，却发起了模型调用"
            _cleanup_runs(seeded)
    finally:
        _release_busy_worker(seeded)


def test_the_sibling_repository_sync_also_blocks_the_manual_entry(monkeypatch):
    """闸门按**整批**判：入口是 primary，而同步任务挂在兄弟配置上。

    只按单仓判的实现在这条用例下会放行，而那时变更清单正缺着兄弟仓库那半份文件。
    """
    seeded = _seed(sync_on="sibling", sync_status="processing")
    calls: list = []
    with flask_app.app_context():
        _stub(monkeypatch, seeded, calls)
        created = _create_manual_job(seeded["primary_config_id"])
        cfg = db.session.get(WeeklyVersionConfig, seeded["primary_config_id"])
        group_key = build_weekly_group_key(cfg)

        assert created.job.state == STATE_WAITING_SNAPSHOT, created.job.state
        assert _analysis_tasks(group_key) == [], "兄弟仓库的同步在跑，却排出了分析任务"
        assert calls == [], "兄弟仓库的同步在跑，却发起了模型调用"
        _cleanup_runs(seeded)


def test_the_manual_entry_proceeds_when_the_sync_is_done(monkeypatch):
    """反面：同步不在跑时**不许**被这道闸门挡住（否则手工分析永久停摆）。"""
    seeded = _seed(sync_on="primary", sync_status="completed")
    calls: list = []
    with flask_app.app_context():
        _stub(monkeypatch, seeded, calls)
        created = _create_manual_job(seeded["primary_config_id"])
        cfg = db.session.get(WeeklyVersionConfig, seeded["primary_config_id"])
        group_key = build_weekly_group_key(cfg)

        assert created.job.state == STATE_QUEUED, (
            f"同步已经跑完了，却还停在 {created.job.state}"
        )
        _drive_the_job_task(cfg, group_key)
        assert calls == ["execute"], "同步跑完了却没分析"
        _cleanup_runs(seeded)


def test_the_manual_gate_sits_before_the_run_is_created():
    """结构断言：闸门必须排在**排任务 / 建 run** 之前（与后台那道同一个理由）。

    建了 run 再跳过会留下一条「跑了但没结论」的记录，用量面板上还会多一条零消费运行，
    排查时看不出它是被闸门挡下的。

    P0-01 之后手工入口的闸门在 `job_service._dispatch` —— 那是**唯一**会为一次手工
    点击排任务的地方，而 job_service 本身**不建 run**（它连 `_create_run` 都没有），
    所以「先建 run 再被拦」在这个结构下不可能发生；要钉的是**闸门在排任务之前**。
    """
    source = (PROJECT_ROOT / "services" / "ai" / "job_service.py").read_text(encoding="utf-8")
    body = source[source.index("def _dispatch("):]
    body = body[: body.index("def _register_intent(")]

    assert "weekly_sync_in_flight" in body, "手工入口没有查同步是否还在跑"
    assert body.index("weekly_sync_in_flight") < body.index("_create_analysis_task("), (
        "手工路径的闸门排在建任务之后 —— 同步写到一半就会派出一次分析"
    )


# ==========================================================================
#  二、前端跟得上
# ==========================================================================


def _waiting_handler(source: str) -> str:
    """切出 `waiting` 监听器的函数体。

    边界用**下一个监听器**而不是第一个 `});` —— 后者会切在 `track({...});` 那一行上，
    于是断言只看到前半截，失败信息看起来像「服务端没说那句话」。
    """
    body = source[source.index("addEventListener('waiting'"):]
    boundary = body.find("addEventListener(", len("addEventListener('waiting'"))
    return body if boundary < 0 else body[:boundary]


def test_both_weekly_drawers_handle_the_waiting_signal():
    """两份走周版本流的抽屉都要处理 `waiting`。

    不处理的后果是**静默**：流结束 → `EventSource` 的 `error` 没有 `data` →
    抽屉说「与服务器的连接中断了」。用户看到的是一句假话（连接没断，是闸门拦下了），
    而且他无从知道该等同步。
    """
    for rel in WEEKLY_TEMPLATES:
        source = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "addEventListener('waiting'" in source, f"{rel} 不认 waiting 事件"
        handler = _waiting_handler(source)
        assert "sync_in_flight" in handler, f"{rel} 没把「等待同步」与别的拦阻分开说"
        assert "already_running" in handler, f"{rel} 没把「已经有一次在跑」分开说"


def test_the_waiting_badge_is_not_shared_with_failure():
    """「等待同步」不许复用「失败」的写法。

    闸门拦下时**一个请求都没发**，把它显示成失败既吓人又不准 —— 而用户下一次点
    「开始分析」很可能是会成功的。
    """
    for rel in WEEKLY_TEMPLATES:
        source = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        handler = _waiting_handler(source)
        assert "'等待同步'" in handler, f"{rel} 的 waiting 分支没有自己的徽章"
        assert "'danger'" not in handler, f"{rel} 把「等待同步」标成了危险色"


def test_attaching_to_a_running_analysis_keeps_the_button_disabled():
    """附着到「已经有一次在跑」时，按钮**保持禁用**。

    重新启用等于告诉用户「可以再按一次」—— 那正是这次要修的坑（关抽屉再打开就能重按）。
    """
    for rel in WEEKLY_TEMPLATES:
        source = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        handler = _waiting_handler(source)
        assert "weeklyAiRunActive = true" in handler, (
            f"{rel} 附着到在跑的那一次时没有把「正在跑」标上 —— 关掉抽屉再打开，"
            "按钮又会被放回可点"
        )
        assert "startBtn.disabled = false" not in handler.split("payload.run_id")[0], (
            f"{rel} 在附着之前就启用了按钮"
        )
