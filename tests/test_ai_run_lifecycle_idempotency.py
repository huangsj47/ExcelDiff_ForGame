# -*- coding: utf-8 -*-
"""运行创建没有幂等：同一个目标能被并发建出两条运行，两次都花钱。

## 缺陷形态（实测）

`_create_run()` 只有 `db.session.add(run)` + `commit()`，没有先查再插、没有 claim、
没有锁；`ai_analysis_run` 表上只有 `PRIMARY KEY(id)` 和一个**非唯一**索引
（直接查 `sqlite_master` 确认：**无任何 UNIQUE 约束**）。

前端那一层只有 `templates/weekly_version_diff.html` 的 `startBtn.disabled = true`，
而 `refreshWeeklyAiReadiness()` 在配置就绪且未超预算时**无条件**把按钮放回可点，
它又在打开抽屉时被调用 —— **同页关掉抽屉再打开就能重按**，而 `closeWeeklyAiStream()`
只关浏览器端连接，服务端那次分析还在跑。

## 这一组守什么

1. **数据库级**幂等：同一目标 + 同一输入同时只允许一条活动运行，由 UNIQUE 索引裁决
   —— 不是「先查再插」（那条路在查与插之间有一个窗口，连按两次正好落在窗口里）；
2. **输入确实变了**要允许新建（否则一次分析跑完之后这个目标再也分析不了，或者反过来
   —— 快照更新了却复用旧结论）；
3. 认领**跑完就释放**，且**僵尸/泄漏的认领不许把目标永久锁死**（那比不加约束更糟）；
4. 前端在分析进行中不得被重新启用（服务端那道闸是兜底，不是借口）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

import services.ai.job_service as job_service
import services.ai_analysis_service as ai_service
import services.task_worker_service as worker
from app import app as flask_app
from app import create_tables, db
from models import BackgroundTask, Project, Repository, WeeklyVersionConfig
from models.ai_analysis import (
    MODE_INCREMENTAL,
    STATE_CANCELLED,
    AiAnalysisJob,
    AiAnalysisRun,
    AiWeeklyAnalysisState,
)
from models.ai_analysis.analysis_run import STALE_RUNNING_SECONDS
from models.weekly_version import WeeklyVersionDiffCache
from services.ai_analysis_service import ActiveAnalysisConflict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)
# 「打开抽屉 → 按就绪状态决定按钮能不能按」的那一段。两份模板的函数名与边界都不同，
# 所以按锚点切区间；切到文件尾会把**别的**出口里的 `disabled = false` 算进来，
# 那几条断言就成了永远为真。
READINESS_WINDOW = {
    "templates/weekly_version_diff.html": (
        "async function refreshWeeklyAiReadiness",
        "async function loadWeeklyAiLatest",
    ),
    "templates/merged_project_view.html": (
        "function openAiDrawerFromRiskLabel",
        "function getVisibleConfigIds",
    ),
}


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed(*, with_cache_row: bool = True, commit_id: str = "c1") -> dict:
    """项目 + 一条周版本配置（+ 一行缓存，用来让「快照指纹」有内容）。"""
    now = datetime.now(timezone.utc)
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("ID"), name="幂等用例")
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id, name=_uid("repo"), type="git",
            url="https://example.invalid/r.git", branch="main", resource_type="code",
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
        db.session.flush()
        if with_cache_row:
            db.session.add(
                WeeklyVersionDiffCache(
                    config_id=config.id, repository_id=repository.id,
                    file_path="src/a.lua", file_type="code",
                    latest_commit_id=commit_id, commit_count=1,
                    updated_at=now,
                )
            )
        ai_service.set_project_api_key(project.id, "sk-test", updated_by="tester")
        db.session.commit()
        return {"project_id": project.id, "config_id": config.id,
                "repository_id": repository.id, "group_key": f"g-{config.id}"}


def _payload(seeded: dict, *, focus: str = "all") -> dict:
    return {
        "mode": "weekly",
        "scope": "full",
        "focus": {"key": focus, "label": ""},
        "group": {
            "key": seeded["group_key"], "base_name": "W",
            "project_id": seeded["project_id"],
            "config_ids": [seeded["config_id"]],
            "start_time": None, "end_time": None,
        },
        "summary": {"total_files": 3, "delta_files": 3},
    }


def _create(seeded: dict, *, focus: str = "all", trigger: str = "manual") -> AiAnalysisRun:
    return ai_service._create_run(
        project_id=seeded["project_id"],
        target_type="weekly",
        target_id=seeded["config_id"],
        target_key=seeded["group_key"],
        response_mode="streaming",
        scope="full",
        trigger_source=trigger,
        payload=_payload(seeded, focus=focus),
    )


def _cleanup(run_ids) -> None:
    for run_id in run_ids:
        AiAnalysisRun.query.filter_by(id=run_id).delete()
    db.session.commit()


# ==========================================================================
#  一、数据库级的活动运行唯一性
# ==========================================================================


def test_the_unique_claim_exists_in_the_database_not_just_in_python():
    """**这一条是整个方案的地基**：约束必须真的在库里。

    只在 Python 里「先查再插」是挡不住并发的（查与插之间有一个窗口），也挡不住
    另一个进程 —— 而平台是多进程部署的。
    """
    with flask_app.app_context():
        create_tables()
        rows = db.session.execute(
            sa_text("SELECT name, sql FROM sqlite_master WHERE tbl_name = 'ai_analysis_run'")
        ).fetchall()
        unique = [
            (name, sql) for name, sql in rows
            if sql and "UNIQUE" in sql.upper() and "active_key" in sql
        ]
        assert unique, f"ai_analysis_run 上没有任何针对 active_key 的 UNIQUE 约束：{rows}"


def test_two_creates_for_the_same_target_and_input_yield_one_run():
    """并发（或连按两次）只能建出一条运行。第二条是**幂等命中**，不是异常。"""
    seeded = _seed()
    with flask_app.app_context():
        first = _create(seeded)
        with pytest.raises(ActiveAnalysisConflict) as excinfo:
            _create(seeded)

        assert excinfo.value.run.id == first.id, (
            "第二个请求要能拿到**已有那条运行** —— 界面据此附着上去，而不是自己再跑一次"
        )
        assert AiAnalysisRun.query.filter_by(
            target_type="weekly", target_key=seeded["group_key"]
        ).count() == 1
        _cleanup([first.id])


def test_a_second_create_with_a_changed_snapshot_is_allowed():
    """**输入确实变了要允许新建**：快照内容身份变了就是新一轮。

    用一个「已经跑完」的第一条来隔离变量：这里量的不是认领，而是**幂等键本身**有没有
    把「同一输入」与「新输入」分开。不分开的话，快照更新之后这个目标再也分析不了。
    """
    seeded = _seed(commit_id="c1")
    with flask_app.app_context():
        first = _create(seeded)
        first.status = "succeeded"
        first.active_key = None
        db.session.commit()

        # 快照变了：同一个文件的合并 diff 输入换了（`weekly_snapshot_digest` 就是按
        # (file_path, base, latest, diff_version) 这个三元组取的内容身份）。
        row = WeeklyVersionDiffCache.query.filter_by(file_path="src/a.lua").first()
        row.latest_commit_id = "c2"
        db.session.commit()

        second = _create(seeded)
        assert second.id != first.id, "快照换了却复用了旧的幂等键 —— 这次分析会看不到新改动"
        _cleanup([first.id, second.id])


def test_a_different_focus_is_a_different_input():
    """「只看配表」与「只看代码」是两份不同的输入，不该互相顶掉。"""
    seeded = _seed()
    with flask_app.app_context():
        first = _create(seeded, focus="all")
        first.active_key = None
        db.session.commit()
        second = _create(seeded, focus="table")
        assert second.id != first.id
        _cleanup([first.id, second.id])


# ==========================================================================
#  二、认领的释放与泄漏
# ==========================================================================


def test_the_claim_is_released_when_the_run_finishes():
    """跑完就释放。不释放的后果是**这个目标从此再也分析不了**。"""
    seeded = _seed()
    with flask_app.app_context():
        from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome

        first = _create(seeded)
        assert first.active_key, "建运行的时候没有认领 —— 幂等键形同虚设"
        ai_service._persist_outcome(
            first, EngineOutcome(status=STATUS_SUCCEEDED, rounds=()), {"anomalies": []}
        )
        assert first.active_key is None, "跑完了还占着认领，下一次分析会被自己挡住"
        second = _create(seeded)
        assert second.id != first.id
        _cleanup([first.id, second.id])


def test_a_claim_held_by_a_dead_run_does_not_lock_the_target():
    """**死运行手里的认领必须能被接管。**

    进程被杀 / 平台重启（`fail_orphaned_analysis_runs` 只改 status，不认识这一列）/
    落库本身失败，都会留下一条**带着认领的死人**。只按「这一行在不在」判，
    那份输入从此再也发起不了分析 —— 一条永远解不开的死锁，比不加约束更糟。
    """
    seeded = _seed()
    with flask_app.app_context():
        dead = AiAnalysisRun(
            project_id=seeded["project_id"], target_type="weekly",
            target_id=seeded["config_id"], target_key=seeded["group_key"],
            status="failed", scope="full", trigger_source="manual",
            active_key=ai_service._analysis_claim_key(
                project_id=seeded["project_id"], target_type="weekly",
                target_id=seeded["config_id"], target_key=seeded["group_key"],
                scope="full", payload=_payload(seeded),
            ),
        )
        db.session.add(dead)
        db.session.commit()

        fresh = _create(seeded)
        assert fresh.id != dead.id
        db.session.refresh(dead)
        assert dead.active_key is None, "死运行手里的认领没有被清掉"
        _cleanup([dead.id, fresh.id])


def test_a_claim_held_by_a_stale_running_run_does_not_lock_the_target():
    """僵尸 running（超过 `STALE_RUNNING_SECONDS` 没有动静）同样不算持有者。

    这是**进程还活着但那次分析真的卡死了**那一类，重启清理救不了它。
    """
    seeded = _seed()
    with flask_app.app_context():
        stale_at = datetime.now(timezone.utc) - timedelta(
            seconds=STALE_RUNNING_SECONDS + 60
        )
        key = ai_service._analysis_claim_key(
            project_id=seeded["project_id"], target_type="weekly",
            target_id=seeded["config_id"], target_key=seeded["group_key"],
            scope="full", payload=_payload(seeded),
        )
        zombie = AiAnalysisRun(
            project_id=seeded["project_id"], target_type="weekly",
            target_id=seeded["config_id"], target_key=seeded["group_key"],
            status="running", scope="full", trigger_source="manual",
            active_key=key, started_at=stale_at, created_at=stale_at,
        )
        db.session.add(zombie)
        db.session.commit()

        fresh = _create(seeded)
        assert fresh.id != zombie.id
        _cleanup([zombie.id, fresh.id])


# ==========================================================================
#  三、两条入口都走同一把锁
# ==========================================================================


def _stub(monkeypatch, seeded: dict, calls: list) -> None:
    payload = _payload(seeded)
    monkeypatch.setattr(
        ai_service, "build_weekly_payload", lambda *a, **k: (payload, None, None)
    )

    def _execute(*_args, **_kwargs):
        calls.append("execute")
        return {"status": "succeeded", "report_markdown": "结论", "error_message": None}

    monkeypatch.setattr(ai_service, "_execute_analysis", _execute)


def _events(text: str) -> list:
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


def test_the_manual_entry_attaches_to_the_existing_run_for_the_same_input(monkeypatch):
    """手工路径撞上「同一输入已经在跑」：**不重复发起**，并附着现有 run。

    P0-01 之后手工入口是 `POST /jobs` → 任务 → `run_weekly_analysis_background`：
    那一次执行会撞上 `_create_run` 的 UNIQUE 约束（同一份输入的认领握在别人手里），
    于是按 `skipped/already_running` 结束 —— **一个模型请求都不发**。

    新 job 关联持有认领的 run，因此用户继续看到同一份进度和结果；run 终结时
    `settle_from_run` 会一次收口所有附着 job。它在此之前保持 running/active_key，
    正好阻止同一 target 再排出第三个后台任务。
    """
    seeded = _seed()
    calls: list = []
    with flask_app.app_context():
        holder = _create(seeded)
        _stub(monkeypatch, seeded, calls)

        config = db.session.get(WeeklyVersionConfig, seeded["config_id"])
        created = job_service.create_or_attach_job(
            config=config, requested_mode=MODE_INCREMENTAL, trigger_source="manual"
        )
        db.session.commit()
        job_id = created.job.id
        task_id = created.job.task_id
        try:
            assert task_id, "job 没有排出去任务，这一跳跑不起来"
            worker._handle_weekly_ai_analysis_task(
                {"type": "weekly_ai_analysis", "config_id": seeded["config_id"],
                 "task_id": task_id}
            )
            db.session.commit()

            assert calls == [], "重复发起了一次会花钱的模型调用"
            assert AiAnalysisRun.query.filter_by(
                target_type="weekly", target_key=seeded["group_key"]
            ).count() == 1, "又建了一条运行（= 同一份输入付两次费）"

            job = db.session.get(AiAnalysisJob, job_id)
            assert job.state == "running"
            assert job.run_id == holder.id
            assert job.active_key is not None

            holder.status = "succeeded"
            job_service.settle_from_run(holder)
            db.session.commit()
            db.session.refresh(job)
            assert job.state == "succeeded"
            assert job.active_key is None
        finally:
            _drop_job_and_state(job_id, seeded["group_key"], [holder.id])


def _drop_job_and_state(job_id, group_key, run_ids) -> None:
    """收掉本用例建出来的 job 行（`active_key` 非空会占着唯一索引）。"""
    row = db.session.get(AiAnalysisJob, job_id)
    if row is not None:
        if row.active_key is not None:
            row.active_key = None
            db.session.commit()
        db.session.delete(row)
        db.session.commit()
    AiWeeklyAnalysisState.query.filter_by(group_key=group_key).delete()
    BackgroundTask.query.filter(
        BackgroundTask.file_path == group_key
    ).delete(synchronize_session=False)
    _cleanup(run_ids)


def test_the_background_entry_skips_when_a_manual_run_holds_the_claim(monkeypatch):
    """后台路径撞上手工运行：**跳过且不推进水位线**。

    这正是实测里那条：「手工运行结束时，同一分组的后台 AI 任务仍在 pending；
    如果后续同步闸门解除，现有代码没有数据库幂等键阻止它再跑一次」。
    """
    seeded = _seed()
    calls: list = []
    with flask_app.app_context():
        holder = _create(seeded, trigger="manual")
        # 两条路都用同一份 payload 桩 —— 不然它们算出来的幂等键会因为**测试桩里的
        # 假 group_key** 而不相等，这条用例就变成在测「两个不同的键不会互相顶掉」。
        _stub(monkeypatch, seeded, calls)
        outcome = ai_service.run_weekly_analysis_background(seeded["config_id"])

        assert outcome.get("status") == "skipped", outcome
        assert outcome.get("reason") == "already_running", outcome
        assert calls == [], "后台任务对着同一份输入又发起了一次模型调用"
        assert AiAnalysisRun.query.filter_by(
            target_type="weekly", target_key=seeded["group_key"]
        ).count() == 1, "后台任务又建了一条运行"
        _cleanup([holder.id])


def test_the_migration_is_idempotent_and_creates_the_unique_index():
    """迁移脚本本身也要守：它是**唯一**一条能让「保留数据的库」拿到那道约束的路。

    幂等是硬要求 —— 它会（在被接进 `apply_schema_migrations` 之后）每次启动都跑一遍。
    重复执行报错就等于把启动链路炸掉。
    """
    from migrations.ai_run_claim_columns import apply
    from utils.logger import log_print

    with flask_app.app_context():
        create_tables()
        # 新库（`create_all`）已经带着两列一索引，所以这里应当**全部跳过**。
        first = apply(db, log_print)
        assert first["added_columns"] == [], first
        assert first["added_indexes"] == [], first
        # 跑第二遍同样不报错。
        second = apply(db, log_print)
        assert second["added_columns"] == [] and second["added_indexes"] == [], second

        rows = db.session.execute(
            sa_text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'ai_analysis_run'")
        ).fetchall()
        names = {row[0] for row in rows}
        assert "uq_ai_run_active_key" in names, names


def test_the_claim_is_declared_not_just_checked():
    """静态守卫：`_create_run` 里不许出现「先查再插」那种写法。

    这条断言的价值在于**它拦的是一种看起来更简单的改法** —— 加一句
    `if AiAnalysisRun.query.filter_by(...).first(): raise ...` 就「实现幂等」了，
    而它在并发下是失效的（查与插之间有窗口，连按两次正好落在窗口里）。
    真正的判据是那个 UNIQUE 索引 + `IntegrityError`。
    """
    source = (PROJECT_ROOT / "services" / "ai_analysis_service.py").read_text(encoding="utf-8")
    body = source[source.index("def _create_run("):]
    body = body[: body.index("def _execute_analysis(")]

    assert "except IntegrityError" in body, (
        "建运行没有接住 UNIQUE 冲突 —— 那道约束就只会在界面上变成一个 500"
    )
    assert "active_key=claim_key" in body, "建出来的运行没有认领这份输入"


def test_the_commit_stream_says_already_running_instead_of_replaying_itself(monkeypatch):
    """单提交那条路撞上「同一个提交已经在跑」时**说清楚**，而不是静默。

    这条路上没有「附着上去」这一步（`commit_diff_new.html` 不在这一版的改动范围内），
    所以服务端发的是它本来就认识的那个 `error` 事件 —— 那份抽屉的 `errorOutcome`
    会如实说「未开始 / 分析没有发起，没有产生消耗」。**不许静默断流**：那会被界面
    读成「与服务器的连接中断了」，而连接根本没断。
    """
    from models import Commit

    seeded = _seed()
    calls: list = []
    with flask_app.app_context():
        repository = db.session.get(Repository, seeded["repository_id"])
        commit = Commit(
            repository_id=repository.id, commit_id="a" * 40, path="code/a.lua",
            operation="M", author="tester", commit_time=datetime.now(timezone.utc),
            message="测试提交", status="pending",
        )
        db.session.add(commit)
        db.session.commit()

        monkeypatch.setattr(
            ai_service, "build_commit_payload",
            lambda *a, **k: {
                "mode": "commit",
                "scope": "full",
                "summary": {"total_files": 1, "delta_files": 1},
            },
        )

        def _execute(*_args, **_kwargs):
            calls.append("execute")
            return {"status": "succeeded", "report_markdown": "结论", "error_message": None}

        monkeypatch.setattr(ai_service, "_execute_analysis", _execute)

        first = _events("".join(ai_service.stream_commit_analysis(commit.id)))
        assert "run" in [name for name, _ in first], first
        second = _events("".join(ai_service.stream_commit_analysis(commit.id)))

        names = [name for name, _ in second]
        assert "run" not in names, f"同一个提交被并发跑了两遍：{second}"
        assert names[-1] == "error", f"第二次没有给出明确信号：{second}"
        assert "在进行中" in (second[-1][1].get("message") or ""), second[-1]
        assert calls == ["execute"], "重复发起了一次会花钱的模型调用"
        AiAnalysisRun.query.filter_by(target_type="commit", target_id=commit.id).delete()
        db.session.commit()


# ==========================================================================
#  四、前端在分析进行中不得被重新启用
# ==========================================================================


def test_the_readiness_check_cannot_re_enable_the_button_mid_run():
    """**用户报的那条**：关掉抽屉再打开就能重按。

    `refreshWeeklyAiReadiness()`（周版本抽屉）与 `openAiDrawerFromRiskLabel()`
    （合并视图抽屉）都在**每次打开抽屉**时按配置就绪与否把「开始分析」放回可点 ——
    而服务端那次分析还在跑、还在计费。

    服务端那道数据库闸是**兜底**，不是允许前端放行重按的借口：用户看到的
    「又能按了」本身就是错的。
    """
    for rel, (start_anchor, end_anchor) in READINESS_WINDOW.items():
        source = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        body = source[source.index(start_anchor):]
        body = body[: body.index(end_anchor)]

        assert "weeklyAiRunActive" in body, (
            f"{rel} 的 readiness 检查没有「正在跑」这一档 —— 它会把按钮无条件放回可点"
        )
        guard_at = body.index("weeklyAiRunActive")
        enable_at = body.index("disabled = false")
        assert guard_at < enable_at, (
            f"{rel} 的「正在跑」判断排在启用按钮之后 —— 进了那个分支按钮已经亮了"
        )


def test_the_running_state_is_cleared_on_every_terminal_path():
    """「正在跑」这个状态必须在**所有**出口被清掉。

    漏掉一处的表现是按钮永远灰着 —— 用户只能刷新页面。三份模板里都有过这种
    「所有出口都要做同一件事，在每处各写一遍必然漏一处」的教训。
    """
    for rel in TEMPLATES:
        source = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        clears = source.count("weeklyAiRunActive = false")
        assert clears >= 4, (
            f"{rel} 只在 {clears} 处清掉「正在跑」—— result / error / 连接断 / 轮询发现"
            "跑完这几条出口都要清"
        )
