# -*- coding: utf-8 -*-
"""手工分析路径缺同步闸门：同步还在写缓存时，点「开始分析」照样开跑。

## 缺陷形态（实测）

`run_weekly_analysis_background`（后台路径）有 `weekly_sync_in_flight(group_config_ids(config))`
这道闸，`stream_weekly_analysis`（手工路径，抽屉里那个按钮走的就是它）**全函数体没有**。
实测：run 13 于 10:50:08 创建时，同组 config 2 的 `weekly_sync` 任务 355 正 processing
（10:49:28→11:06:34）—— 那次分析建立在一份**还会变的跨仓库缓存**上。

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

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import services.ai_analysis_service as ai_service
from app import app as flask_app
from app import create_tables, db
from models import BackgroundTask, Project, Repository, WeeklyVersionConfig
from models.ai_analysis import AiAnalysisRun

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 两份走周版本流的手工入口（第三份 commit_diff_new.html 走单提交流，不在这条路上）。
WEEKLY_TEMPLATES = (
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed(*, sync_on: str = "primary", sync_status: str = "processing",
          age_minutes: int = 2, busy_worker: bool = False) -> dict:
    """一个项目 + 两条同窗口的周版本配置（模拟多仓库分组），可选一条同步任务。

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


def _run_stream(config_id: int) -> list:
    return _events("".join(ai_service.stream_weekly_analysis(config_id)))


def _cleanup_runs(seeded: dict) -> None:
    """把这一组用例建出来的运行行删掉。

    **测试库是会话级共用的**（没有逐用例重置），而这一组的引擎桩**不会**走
    `_persist_outcome` —— 留下的 run 会一直是 `running`，然后被
    `tests/test_weekly_ai_auto_trigger_gate.py::test_a_run_interrupted_by_a_restart_is_marked_failed`
    那条**全局计数**断言（`fail_orphaned_analysis_runs() == 1`）算进去，让它在
    「一起跑」时红、单独跑时绿。
    """
    AiAnalysisRun.query.filter_by(target_id=seeded["primary_config_id"]).delete()
    db.session.commit()


# ==========================================================================
#  一、闸门本身（手工路径）
# ==========================================================================


def test_the_manual_stream_is_blocked_while_the_batch_sync_runs(monkeypatch):
    """**用户报的那一条**：同步正 processing 时点「开始分析」，必须被拦下。

    三件事一起断言，缺一条都会漏掉一种错法：
      * 没有 `run` 事件 —— 界面不会看到一个运行号，说明服务端没有建运行；
      * 没有 `result` 事件 —— 没有结论可交付；
      * 引擎一次都没被调用 —— 没有花钱。
    """
    seeded = _seed(sync_on="primary", sync_status="processing")
    calls: list = []
    with flask_app.app_context():
        _stub(monkeypatch, seeded, calls)
        events = _run_stream(seeded["primary_config_id"])
        names = [name for name, _ in events]

        assert "run" not in names, f"同步还在跑，却建了运行记录：{events}"
        assert "result" not in names, f"同步还在跑，却交付了结论：{events}"
        assert calls == [], "同步还在跑，却发起了模型调用"
        assert (
            AiAnalysisRun.query.filter_by(target_id=seeded["primary_config_id"]).count() == 0
        ), "被拦下了却还是留下了一条 run 记录"


def test_the_manual_stream_says_it_is_waiting_for_the_diff_sync(monkeypatch):
    """**不许静默失败**：页面必须明确看到「等待 Diff 同步完成」，而不是装作开始了。

    「装作开始」比报错更糟：徽章会变成「分析中」，用户以为钱已经花了。
    """
    seeded = _seed(sync_on="primary", sync_status="processing", age_minutes=3)
    calls: list = []
    with flask_app.app_context():
        _stub(monkeypatch, seeded, calls)
        events = _run_stream(seeded["primary_config_id"])

        waiting = [payload for name, payload in events if name == "waiting"]
        assert waiting, f"同步在跑，却没有任何「等待同步」的信号：{events}"
        payload = waiting[0]
        assert payload["reason"] == "sync_in_flight", payload
        message = payload.get("message") or ""
        assert "等待 Diff 同步完成" in message, message
        # 闸门本身那句话也要带上：是哪个任务在跑、跑了多久。
        assert str(seeded["task_id"]) in message, message
        assert "3 分钟" in message, message
        # 「没有花钱」这句必须说 —— 用户第一件想知道的就是这个。
        assert "没有" in message and "消耗" in message, message


def test_the_manual_stream_proceeds_while_a_pending_sync_waits_behind_a_busy_worker(monkeypatch):
    """**真机实测的那一条**：同步只是**在排队**、执行侧被别的任务占着（缓存没有被写）
    → 手工分析必须开跑。

    本地后台任务是单线程的：那一个 worker 正拿着别的任务时，排在后头的 `weekly_sync`
    不会开始写缓存（实测：任务 381 在 run 15 结束后 0.085 秒才开始）。拿它拦等于让手工
    分析永远起不来 —— 实测连续 22 次发起、跨度约 7 分钟，全部被回 `waiting`。
    """
    seeded = _seed(sync_on="primary", sync_status="pending", busy_worker=True)
    calls: list = []
    try:
        with flask_app.app_context():
            _stub(monkeypatch, seeded, calls)
            events = _run_stream(seeded["primary_config_id"])
            names = [name for name, _ in events]

            assert "waiting" not in names, (
                f"排队等 worker 的同步把手工分析拦下了（缓存没有被写）：{events}"
            )
            assert "run" in names, f"没建运行记录：{events}"
            assert calls == ["execute"], "没分析"
            _cleanup_runs(seeded)
    finally:
        _release_busy_worker(seeded)


def test_the_sibling_repository_sync_also_blocks_the_manual_stream(monkeypatch):
    """闸门按**整批**判：入口是 primary，而同步任务挂在兄弟配置上。

    只按单仓判的实现在这条用例下会放行，而那时变更清单正缺着兄弟仓库那半份文件。
    """
    seeded = _seed(sync_on="sibling", sync_status="processing")
    calls: list = []
    with flask_app.app_context():
        _stub(monkeypatch, seeded, calls)
        events = _run_stream(seeded["primary_config_id"])

        assert "run" not in [name for name, _ in events], events
        assert calls == [], "兄弟仓库的同步在跑，却发起了模型调用"


def test_the_manual_stream_proceeds_when_the_sync_is_done(monkeypatch):
    """反面：同步不在跑时**不许**被这道闸门挡住（否则手工分析永久停摆）。"""
    seeded = _seed(sync_on="primary", sync_status="completed")
    calls: list = []
    with flask_app.app_context():
        _stub(monkeypatch, seeded, calls)
        events = _run_stream(seeded["primary_config_id"])
        names = [name for name, _ in events]

        assert "waiting" not in names, f"同步已经跑完了，却还在等：{events}"
        assert "run" in names, f"同步跑完了却没建运行记录：{events}"
        assert calls == ["execute"], "同步跑完了却没分析"
        _cleanup_runs(seeded)


def test_the_manual_gate_sits_before_the_run_is_created():
    """结构断言：闸门必须排在 `_create_run` **之前**（与后台那道同一个理由）。

    建了 run 再跳过会留下一条「跑了但没结论」的记录，用量面板上还会多一条零消费运行，
    排查时看不出它是被闸门挡下的。
    """
    source = (PROJECT_ROOT / "services" / "ai_analysis_service.py").read_text(encoding="utf-8")
    body = source[source.index("def stream_weekly_analysis"):]
    body = body[: body.index("def run_weekly_analysis_background")]

    assert "weekly_sync_in_flight" in body, "手工入口没有查同步是否还在跑"
    assert body.index("weekly_sync_in_flight") < body.index("_create_run("), (
        "手工路径的闸门排在建 run 之后 —— 会留下一条零消费的运行记录"
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
