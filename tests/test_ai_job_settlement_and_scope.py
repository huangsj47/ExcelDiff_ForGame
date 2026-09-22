# -*- coding: utf-8 -*-
"""Job 协议收尾：没有 run 的结局要结清、`focus`/`requested_mode` 要走到执行侧。

## 这一组守什么（第二波 P0-01 留下的三个缺口）

### 一、没有 run 的结局没人结清（功能性阻塞）

`settle_job_from_result` 原先靠一条 `AiAnalysisRun` 行来映射终态。但**没有建出 run 的
那些结局**（开关关着 / 预算不足 / 同步闸门 / 没有变化直接复用）在 worker 里直接结束、
不带 run 回来 —— 于是那条 job **永远停在非终态**，而它的 `active_key` 也从没被清空过。
`uq_ai_job_active_key` 是**唯一索引**，所以后果不是「状态难看」，而是：

    同一个 target + focus 从此再也建不出任何 job。

用户点按钮会一直附着到那条永远不动的 job 上（`attached=True`、状态不变、什么都不发生），
也就是「点了没反应」那一类静默故障。

### 二、`focus` 与 `requested_mode` 在「POST → worker」这一跳丢了

`run_weekly_analysis_background` 的形参只有 `config_id / task_id / trigger_source`，
内部 `build_weekly_payload(config_id)` **不带 focus** —— 用户选的「仅配表」被静默丢掉，
平台按自己的排序策略跑了一份全量结论，而界面上写着「仅配表仓库」。

同一条路上 `requested_mode`：任务行上已经有它了（W3 落的，行权威、载荷兜底），
但执行侧不接受这个关键字，于是「用户点了全量」只留一行警告。

**验收落在行为上，不是字符串上**：带 `focus=table` 建一条 job、驱动到执行，断言那条 run
的 `request_payload.focus.label` 是「仅配表仓库」、且 `summary.total_files` 跟着筛过。

### 三、恢复扫描：兜住「连上面那条路都没走到」

进程被杀、任务行被人手工改成终态、job 建好了但排程那一步炸了 —— 这三种都没有任何
在途代码会去收口那条 job。扫描的三条判据必须是**保守**的：一条合法长跑了两小时的
分析不许被它扫成终态。

## 纪律

* 测试库是**会话级共用**的，所有计数与查询都按 `group_key` / `id` 过滤，绝不数全表；
* 每一个用例造出来的行自己收干净（`_CREATED` 那一套）。
"""
from __future__ import annotations

# ruff: noqa: I001 —— 导入顺序是语义要求（见 `tests/test_ai_job_protocol.py` 的说明）：
# `services.task_worker_service` 必须在 `services.task_worker_queue_service` 之前加载。

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.ai.job_service as job_service
import services.task_worker_service as worker
import services.task_worker_queue_service as queue_service
import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import BackgroundTask, Project, Repository, WeeklyVersionConfig
from models import WeeklyVersionDiffCache
from models.ai_analysis import (
    ACTIVE_JOB_STATES,
    MODE_FULL,
    MODE_INCREMENTAL,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_REUSED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    STATE_WAITING_SNAPSHOT,
    TERMINAL_JOB_STATES,
    AiAnalysisJob,
    AiAnalysisRun,
    AiWeeklyAnalysisState,
)
from services.ai.project_config_source import build_weekly_group_key

_CREATED: dict = {"jobs": [], "tasks": [], "groups": [], "projects": []}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


@pytest.fixture(autouse=True)
def _cleanup_rows_this_file_creates():
    yield
    queue_service._inflight_leases.clear()
    with app.app_context():
        for job_id in _CREATED["jobs"]:
            row = db.session.get(AiAnalysisJob, job_id)
            if row is not None and row.active_key is not None:
                # 唯一索引会跨用例拦住后面的用例（这正是本文件要证的缺陷本身）——
                # 收尾时显式放开它，免得一条用例的残留把下一条染红。
                row.active_key = None
        db.session.commit()
        if _CREATED["tasks"]:
            BackgroundTask.query.filter(
                BackgroundTask.id.in_(list(_CREATED["tasks"]))
            ).delete(synchronize_session=False)
        if _CREATED["jobs"]:
            AiAnalysisJob.query.filter(
                AiAnalysisJob.id.in_(list(_CREATED["jobs"]))
            ).delete(synchronize_session=False)
        for group_key in _CREATED["groups"]:
            AiAnalysisRun.query.filter(
                AiAnalysisRun.target_key == group_key
            ).delete(synchronize_session=False)
            AiWeeklyAnalysisState.query.filter(
                AiWeeklyAnalysisState.group_key == group_key
            ).delete(synchronize_session=False)
            BackgroundTask.query.filter(
                BackgroundTask.file_path == group_key
            ).delete(synchronize_session=False)
        for project_id in _CREATED["projects"]:
            AiAnalysisRun.query.filter(
                AiAnalysisRun.project_id == project_id
            ).delete(synchronize_session=False)
        db.session.commit()
        _CREATED["jobs"] = []
        _CREATED["tasks"] = []
        _CREATED["groups"] = []
        _CREATED["projects"] = []


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _make_group(
    *, file_count: int = 0, table_file_count: int = 0, watermark: bool = False
) -> dict:
    """一个周版本分组：项目 + 1~2 个仓库 + 窗口内同组的配置（可选缓存行）。

    `watermark=True` 时给状态行写一条**时间水位线**，并把 `file_count` 那一批缓存行
    的 `updated_at` 放到水位线**之前** —— 于是默认裁决是增量（而不是「整个窗口都是新变化」
    的首跑）。这条是给「用户点了全量」那条用例做对照的：同一份输入平台自己要跑增量。
    """
    now = datetime.now(timezone.utc)
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()
    _CREATED["projects"].append(project.id)

    repo = Repository(
        project_id=project.id,
        name=_uid("repo_code"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        resource_type="code",
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    cfg = WeeklyVersionConfig(
        project_id=project.id,
        repository_id=repo.id,
        name=_uid("weekly"),
        branch="main",
        start_time=now - timedelta(days=7),
        end_time=now,
        is_active=True,
        auto_sync=False,
        status="active",
    )
    db.session.add(cfg)
    db.session.commit()
    group_key = build_weekly_group_key(cfg)
    _CREATED["groups"].append(group_key)

    old = now - timedelta(hours=3)
    for index in range(file_count):
        db.session.add(
            WeeklyVersionDiffCache(
                config_id=cfg.id,
                repository_id=repo.id,
                file_path=f"code/mod_{index}.lua",
                file_type="lua",
                latest_commit_id=f"{index:040d}",
                commit_count=1,
                updated_at=old if watermark else now,
            )
        )

    table_repo = None
    table_cfg = None
    if table_file_count:
        table_repo = Repository(
            project_id=project.id,
            name=_uid("repo_table"),
            type="git",
            url=f"https://example.com/{_uid('t')}.git",
            branch="main",
            resource_type="table",
            clone_status="completed",
        )
        db.session.add(table_repo)
        db.session.flush()
        table_cfg = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=table_repo.id,
            # **同一个 (project_id, start_time, end_time) 才算同一组**
            name=_uid("weekly_table"),
            branch="main",
            start_time=cfg.start_time,
            end_time=cfg.end_time,
            is_active=True,
            auto_sync=False,
            status="active",
        )
        db.session.add(table_cfg)
        db.session.flush()
        for index in range(table_file_count):
            db.session.add(
                WeeklyVersionDiffCache(
                    config_id=table_cfg.id,
                    repository_id=table_repo.id,
                    # **不带 `config/`**：那是平台默认的「关键路径」模式之一，命中它
                    # `_decide_scope` 会升成全量，于是「用户点了全量」那条用例的对照
                    # （平台自己要跑增量）就不成立了。
                    file_path=f"tables/{index}_奖励表_CfgReward{index}.xlsx",
                    file_type="excel",
                    latest_commit_id=f"t{index:039d}",
                    commit_count=1,
                    updated_at=now,
                )
            )
    db.session.commit()

    if watermark:
        state = AiWeeklyAnalysisState(
            project_id=project.id,
            group_key=group_key,
            base_name=cfg.name,
            last_analyzed_at=now - timedelta(hours=1),
        )
        db.session.add(state)
        db.session.commit()

    return {
        "project": project,
        "repo": repo,
        "cfg": cfg,
        "group_key": group_key,
        "table_repo": table_repo,
        "table_cfg": table_cfg,
    }


def _track(job):
    if job is not None and getattr(job, "id", None) is not None:
        _CREATED["jobs"].append(job.id)
    return job


def _track_task(task_id):
    if task_id:
        _CREATED["tasks"].append(task_id)
    return task_id


def _make_job(group, *, state=STATE_QUEUED, active_key=None, task_id=None, run_id=None,
              age_seconds=0, focus="all", started_at=None, lease_expires_at=None):
    """直接落一条 job 行（不走 `create_or_attach_job`）—— 扫描的判据要能单独构造。"""
    created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    row = AiAnalysisJob(
        project_id=group["project"].id,
        target_type="weekly",
        target_id=group["cfg"].id,
        target_key=group["group_key"],
        requested_mode=MODE_INCREMENTAL,
        effective_mode=MODE_FULL,
        state=state,
        trigger_source="manual",
        active_key=active_key or f"key-{_uid('a')}",
        focus=focus,
        task_id=task_id,
        run_id=run_id,
        created_at=created,
        started_at=started_at,
    )
    if lease_expires_at is not None:
        row.lease_expires_at = lease_expires_at
    db.session.add(row)
    db.session.commit()
    return _track(row)


def _make_task(group, *, job_id, status="pending", error_message=None):
    row = BackgroundTask(
        task_type="weekly_ai_analysis",
        repository_id=group["repo"].id,
        commit_id=str(group["cfg"].id),
        file_path=group["group_key"],
        priority=5,
        status=status,
        error_message=error_message,
    )
    if job_id is not None:
        row.job_id = job_id
    db.session.add(row)
    db.session.commit()
    _track_task(row.id)
    return row


def _make_run(group, *, status="running", started_at=None):
    run = AiAnalysisRun(
        project_id=group["project"].id,
        target_type="weekly",
        target_id=group["cfg"].id,
        target_key=group["group_key"],
        status=status,
        scope="incremental",
        trigger_source="manual",
        started_at=started_at or datetime.now(timezone.utc),
    )
    db.session.add(run)
    db.session.commit()
    return run


def _make_intent(group, *, status="pending", age_seconds=0):
    created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    row = BackgroundTask(
        task_type=queue_service.WAITING_INTENT_TASK_TYPE,
        repository_id=group["repo"].id,
        commit_id=str(group["cfg"].id),
        file_path=group["group_key"],
        priority=6,
        status=status,
        created_at=created,
    )
    db.session.add(row)
    db.session.commit()
    _track_task(row.id)
    return row


# ===========================================================================
#  一、`settle_without_run`：没有 run 的结局要按语义映射终态
# ===========================================================================


@pytest.mark.parametrize(
    "reason,expected",
    [
        # 有结论、零消耗 —— 与 succeeded 分开记，否则用量面板会把「没花钱的那一次」
        # 算成一次运行。
        ("no_change", STATE_REUSED),
        # 这次动作**没有被执行**，而且不是错误：开关关着 / 预算拦下 / 被闸门拦下。
        ("auto_weekly_disabled", STATE_CANCELLED),
        ("over_budget", STATE_CANCELLED),
        ("sync_in_flight", STATE_CANCELLED),
        ("already_running", STATE_CANCELLED),
        # 真正的错误。
        ("missing_api_key", STATE_FAILED),
        ("payload_empty", STATE_FAILED),
        # 认不出来的理由：**默认失败**，不许静默看起来像成功。
        ("something_nobody_thought_of", STATE_FAILED),
    ],
)
def test_settle_without_run_maps_each_reason_to_an_honest_state(reason, expected):
    with app.app_context():
        group = _make_group()
        job = _make_job(group, state=STATE_RUNNING)

        settled = job_service.settle_without_run(job.id, reason=reason)
        db.session.commit()

        assert settled is not None, f"reason={reason!r} 没有结清这条 job"
        assert settled.state == expected, (
            f"reason={reason!r} 映射成了 {settled.state}，应该是 {expected}"
        )
        assert settled.state in TERMINAL_JOB_STATES
        assert settled.finished_at is not None, "结清了却没写 finished_at"
        assert settled.error_message, "没有写下这次为什么没有结论（或为什么没跑）"
        assert settled.active_key is None, (
            "没有清 active_key —— 唯一索引会让同一个 target 再也建不出 job（点了没反应）"
        )


def test_settle_without_run_accepts_an_explicit_state_override():
    """调用方能给出更准的终态时以它为准（`reason` 只是短码，映射表是兜底）。"""
    with app.app_context():
        group = _make_group()
        job = _make_job(group, state=STATE_RUNNING)

        settled = job_service.settle_without_run(
            job.id, reason="whatever", state=STATE_CANCELLED
        )

        assert settled.state == STATE_CANCELLED


def test_settle_without_run_never_resurrects_a_terminal_job():
    """幂等：已经是终态就什么都不做、返回它 —— 不复活、不改终态、不覆盖原因。"""
    with app.app_context():
        group = _make_group()
        job = _make_job(group, state=STATE_SUCCEEDED)
        finished = datetime.now(timezone.utc) - timedelta(hours=1)
        job.finished_at = finished
        job.error_message = "原来的结论说明"
        db.session.commit()

        again = job_service.settle_without_run(job.id, reason="over_budget")
        db.session.commit()

        assert again.state == STATE_SUCCEEDED, "迟到的收口把一条跑完的 job 改成了别的终态"
        assert again.error_message == "原来的结论说明"
        assert again.finished_at.replace(tzinfo=None) == finished.replace(tzinfo=None), (
            "覆盖了原来的完成时刻"
        )


def test_settle_without_run_returns_none_for_an_unknown_job():
    with app.app_context():
        assert job_service.settle_without_run(99999999, reason="no_change") is None
        assert job_service.settle_without_run(None, reason="no_change") is None


# ===========================================================================
#  二、**这一条缺口的真正验收**：清掉 active_key 之后同一个 target 能再建出 job
# ===========================================================================


def test_a_job_that_ended_without_a_run_no_longer_blocks_the_next_create():
    """建一条 job、让它**没有 run** 地结束、再建一次 —— 拿到的是**新 job**。

    这就是「同一个 target + focus 从此再也建不出任何 job」那条阻塞的验收。
    缺陷状态下第二次创建会附着到第一条上（`created=False`、`job_id` 相同），
    用户看到的是「点了没反应」。
    """
    with app.app_context():
        group = _make_group()
        first = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(first.job)
        _track_task(first.job.task_id)
        db.session.commit()
        assert first.created is True, "第一条 job 就没建出来，这条用例的前提不成立"

        # 无 run 的结局：被同步闸门拦下（真实的 worker 路径之一是「开关关着」）。
        settled = job_service.settle_without_run(
            first.job_id, reason="sync_in_flight"
        )
        db.session.commit()
        assert settled.state == STATE_CANCELLED

        second = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(second.job)
        _track_task(second.job.task_id)
        db.session.commit()

        print(f"[结清后可再建] first_job_id={first.job_id} second_job_id={second.job_id}")
        assert second.created is True, (
            f"上一次没有 run 地结束之后，同一个 target 再也建不出 job 了 —— "
            f"第二次附着到了 #{second.job_id}（= {first.job_id}）"
        )
        assert second.job_id != first.job_id, (
            f"两次拿到的是同一条 job（{first.job_id}），等于「点了没反应」"
        )
        assert db.session.get(AiAnalysisJob, first.job_id).state == STATE_CANCELLED, (
            "第一条 job 的终态被后面那次创建改动了"
        )


# ===========================================================================
#  三、worker 那一跳：没有 run 行时调 `settle_without_run`
# ===========================================================================


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("no_change", STATE_REUSED),
        ("auto_weekly_disabled", STATE_CANCELLED),
        ("over_budget", STATE_CANCELLED),
        ("sync_in_flight", STATE_CANCELLED),
    ],
)
def test_the_handler_settles_a_job_that_came_back_without_a_run(reason, expected):
    """`settle_job_from_result` 拿到一条 skipped 的结果时，必须结清那条 job。

    `run_weekly_analysis_background` 只有在**真的建出了 run** 时才带 `run_id` 回来；
    其余结局（skipped）原先在这一跳什么也不做，job 于是永远停在非终态。
    """
    import services.task_worker_task_handlers as handlers

    with app.app_context():
        group = _make_group()
        job = _make_job(group, state=STATE_RUNNING)
        task = _make_task(group, job_id=job.id, status="processing")

        settled = handlers.settle_job_from_result(
            {"status": "skipped", "reason": reason}, task_id=task.id
        )
        db.session.commit()

        assert settled is not None, f"reason={reason!r} 时没有结清 job"
        assert settled.state == expected, settled.state
        assert settled.active_key is None


def test_no_change_job_points_to_and_returns_the_reused_report():
    import services.task_worker_task_handlers as handlers

    with app.app_context():
        group = _make_group()
        reused = _make_run(group, status="succeeded")
        reused.response_payload = json.dumps({"report_markdown": "# 旧结论"}, ensure_ascii=False)
        reused.response_text = "# 旧结论"
        job = _make_job(group, state=STATE_RUNNING)
        task = _make_task(group, job_id=job.id, status="processing")

        settled = handlers.settle_job_from_result(
            {"status": "skipped", "reason": "no_change", "reused_run_id": reused.id},
            task_id=task.id,
        )
        db.session.commit()

        assert settled.state == STATE_REUSED
        assert settled.reused_run_id == reused.id
        assert job_service.result_payload(settled)["report_markdown"] == "# 旧结论"


def test_already_running_job_attaches_to_the_existing_run_and_tracks_its_scope():
    import services.task_worker_task_handlers as handlers

    with app.app_context():
        group = _make_group()
        active = _make_run(group, status="running")
        active.scope = MODE_FULL
        active.request_payload = json.dumps({"policy": {"reason": "critical_path_detected"}})
        job = _make_job(group, state=STATE_RUNNING)
        job.effective_mode = MODE_INCREMENTAL
        task = _make_task(group, job_id=job.id, status="processing")

        attached = handlers.settle_job_from_result(
            {"status": "skipped", "reason": "already_running", "run_id": active.id},
            task_id=task.id,
        )
        db.session.commit()

        assert attached.state == STATE_RUNNING
        assert attached.run_id == active.id
        assert attached.effective_mode == MODE_FULL
        assert attached.upgrade_reason == "critical_path_detected"
        assert attached.active_key is not None


def test_progress_payload_falls_back_to_the_persisted_job_snapshot():
    with app.app_context():
        group = _make_group()
        active = _make_run(group, status="running")
        job = _make_job(group, state=STATE_RUNNING, run_id=active.id)
        job.progress_json = json.dumps({"run_id": active.id, "round": 3, "job_tokens": 12345})
        db.session.commit()

        progress = job_service.progress_payload(job)
        assert progress["round"] == 3
        assert progress["job_tokens"] == 12345


def test_the_handler_still_does_nothing_when_there_is_no_job():
    """没有 job 的过渡路径（任务行上是意图 id、或根本没有引用）：不猜、不乱改。"""
    import services.task_worker_task_handlers as handlers

    with app.app_context():
        group = _make_group()
        unrelated = _make_job(group, state=STATE_RUNNING)
        task = _make_task(group, job_id=unrelated.id, status="processing")

        # 认不出来的引用（意图 id 与 job id 会撞车）→ 什么也不做。
        assert handlers.settle_job_from_result(
            {"status": "skipped", "reason": "no_change"}
        ) is None
        assert handlers.settle_job_from_result(None) is None
        db.session.commit()
        assert db.session.get(AiAnalysisJob, unrelated.id).state == STATE_RUNNING, (
            "没有 task_id 的那一次调用改了别人的 job"
        )
        assert task.id  # 这条任务与上面那次调用无关，只是把它造出来


def test_the_handler_maps_a_failed_run_with_a_lost_row_to_failed():
    """`run_id` 有、但那条 run 读不到（行被清了）：按失败收口，不留悬空。"""
    import services.task_worker_task_handlers as handlers

    with app.app_context():
        group = _make_group()
        job = _make_job(group, state=STATE_RUNNING)
        task = _make_task(group, job_id=job.id, status="processing")

        settled = handlers.settle_job_from_result(
            {"status": "succeeded", "run_id": 987654321}, task_id=task.id
        )
        db.session.commit()

        assert settled is not None and settled.state == STATE_FAILED, settled
        assert settled.active_key is None


def test_an_exception_after_the_run_is_created_still_settles_the_job(monkeypatch):
    """**第三跳**：异常发生在 run 建出来**之后**（收尾那一段炸了）→ job 照样收口。

    这一跳原先没有：异常穿出 `run_weekly_analysis_background`，handler 接住之后只标了
    任务行，`settle_job_from_result` 那一跳根本没走到 —— 那条 job 于是带着 `active_key`
    停在非终态。而 `active_key` 是**唯一索引**：同一个 target 从此再也建不出 job。
    启动时的恢复扫描能兜住它，但那要等下一次重启 —— 一个功能性阻塞不该等重启。
    """
    import services.task_worker_task_handlers as handlers

    with app.app_context():
        group = _make_group(file_count=1)
        ai_service.set_project_api_key(group["project"].id, "sk-test")
        db.session.commit()

        created = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(created.job)
        _track_task(created.job.task_id)
        db.session.commit()
        job_id = created.job.id

        def _succeed(*_a, **_k):
            return {"status": "succeeded", "report_markdown": "结论", "error_message": None}

        monkeypatch.setattr(ai_service, "_execute_analysis", _succeed)

        def _explode(*_a, **_k):
            raise RuntimeError("水位线写不进去")

        monkeypatch.setattr(ai_service, "_update_weekly_state", _explode)

        handlers._handle_weekly_ai_analysis_task(
            {"type": "weekly_ai_analysis", "config_id": group["cfg"].id,
             "task_id": created.job.task_id}
        )
        db.session.commit()

        # run 确实建出来了（这条用例的前提：异常发生在它之后）
        assert (
            AiAnalysisRun.query.filter(
                AiAnalysisRun.target_key == group["group_key"]
            ).count() == 1
        ), "这次驱动没有建出 run，用例失去它所测的对象"

        job = db.session.get(AiAnalysisJob, job_id)
        assert job.state == STATE_FAILED, (
            f"收尾炸了之后 job 停在 {job.state} —— active_key 会永远占着唯一索引，"
            "同一个 target 再也建不出 job（而且它要等到下次重启才有人收）"
        )
        assert job.active_key is None
        assert "RuntimeError" in (job.error_message or ""), job.error_message


#: 扫描一次最多看多少条。生产默认 200（按 id 升序，**最老的先看** —— 最老的也最可能
#: 真的卡住了）。而**测试库是会话级共用**的：别的文件在这一轮里留下的活动 job 可能已经
#: 不止 200 条，那时我们要测的那条（id 最大、排在最后）根本轮不到 —— 断言就会假绿
#: 或假红。所以这一组显式放大它，并且**每个用例都断言自己那一条 job**，不靠全局计数。
_SCAN_LIMIT = 100_000


# ===========================================================================
#  四、恢复扫描：三条判据各一条 + 两条反例
# ===========================================================================


def test_the_scan_settles_a_job_whose_task_is_already_terminal():
    """判据 ①：关联的任务行已经是终态，而 job 不是 → 执行体结束了，job 不会再有下文。"""
    with app.app_context():
        group = _make_group()
        job = _make_job(group, state=STATE_RUNNING)
        task = _make_task(group, job_id=job.id, status="completed",
                          error_message="skipped:over_budget")
        job.task_id = task.id
        db.session.commit()

        outcome = job_service.recover_stale_jobs(limit=_SCAN_LIMIT)
        db.session.commit()

        # **按理由断言**（不靠全局计数：那个总数会把别的文件留下的行算进来）。
        assert outcome["by_reason"].get(job_service.REASON_OVER_BUDGET), outcome
        refreshed = db.session.get(AiAnalysisJob, job.id)
        assert refreshed.state == STATE_CANCELLED, refreshed.state
        assert refreshed.active_key is None
        assert "预算" in (refreshed.error_message or ""), refreshed.error_message


def test_the_scan_settles_a_job_whose_run_is_already_terminal():
    """判据 ②：关联的 run 已经是终态（`settle_from_run` 那一跳没走到）。"""
    with app.app_context():
        group = _make_group()
        run = _make_run(group, status="succeeded")
        job = _make_job(group, state=STATE_RUNNING, run_id=run.id)

        outcome = job_service.recover_stale_jobs(limit=_SCAN_LIMIT)
        db.session.commit()

        # 判据 ② 走的是 `settle_from_run`（状态由 `run.status` 映射，最准），
        # 所以它的理由短码是这一个 —— 它**不**经过 `WITHOUT_RUN_REASON_STATES`。
        assert outcome["by_reason"].get(job_service.REASON_RUN_ALREADY_TERMINAL), outcome
        refreshed = db.session.get(AiAnalysisJob, job.id)
        assert refreshed.state == STATE_SUCCEEDED, refreshed.state
        assert refreshed.active_key is None
        assert refreshed.finished_at is not None


def test_the_scan_settles_an_abandoned_job_with_no_task_and_no_run():
    """判据 ③：没有任何关联任务/run、`created_at` 超过 `JOB_STALE_SECONDS`、
    且它那个 target 上**没有**活的等待意图 → 建完就崩 / 排程那一步炸了这一类。"""
    with app.app_context():
        group = _make_group()
        stale = timedelta(seconds=job_service.JOB_STALE_SECONDS + 60)
        job = _make_job(group, state=STATE_QUEUED, age_seconds=stale.total_seconds())

        outcome = job_service.recover_stale_jobs(limit=_SCAN_LIMIT)
        db.session.commit()

        assert outcome["by_reason"].get(job_service.REASON_ABANDONED), outcome
        refreshed = db.session.get(AiAnalysisJob, job.id)
        assert refreshed.state == STATE_FAILED, refreshed.state
        assert refreshed.active_key is None


def test_the_scan_leaves_a_two_hour_old_running_analysis_alone():
    """**反例一**：一条合法的、跑了两小时还在跑的分析不许被扫成终态。

    它的 job 早就超过 `JOB_STALE_SECONDS` 了（租约也过期了），但它的 **run 还在跑** ——
    判据 ② 因此不成立。把 `is_stale` 单独当判据的实现在这条用例下会红。
    """
    with app.app_context():
        group = _make_group()
        two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
        run = _make_run(group, status="running", started_at=two_hours_ago)
        job = _make_job(
            group,
            state=STATE_RUNNING,
            run_id=run.id,
            age_seconds=2 * 60 * 60,
            started_at=two_hours_ago,
            lease_expires_at=two_hours_ago + timedelta(seconds=600),
        )
        # 执行体也还活着（worker 正拿着它）。
        _make_task(group, job_id=job.id, status="processing")

        job_service.recover_stale_jobs(limit=_SCAN_LIMIT)
        db.session.commit()

        refreshed = db.session.get(AiAnalysisJob, job.id)
        assert refreshed.state == STATE_RUNNING, (
            f"一条合法长跑的分析被扫成了 {refreshed.state} —— 用户再也等不到结论"
        )
        assert refreshed.active_key is not None, "扫描把还在跑的那条 job 的 active_key 清了"


def test_the_scan_leaves_a_job_that_still_has_a_live_waiting_intent_alone():
    """**反例二**：target 上还有一条**活的等待意图**时不许动它。

    「同步还在写缓存、这次点击好端端地登记着」与「建完就崩」在 job 行上长得一样，
    区别就在那条意图上。误杀的后果是：同步一收尾，那次分析照跑，而 job 已经是终态了
    （页面显示「已取消」而钱照花）。
    """
    with app.app_context():
        group = _make_group()
        stale = timedelta(seconds=job_service.JOB_STALE_SECONDS + 60)
        job = _make_job(group, state=STATE_WAITING_SNAPSHOT, age_seconds=stale.total_seconds())
        _make_intent(group, status="pending", age_seconds=60)

        job_service.recover_stale_jobs(limit=_SCAN_LIMIT)
        db.session.commit()

        refreshed = db.session.get(AiAnalysisJob, job.id)
        assert refreshed.state == STATE_WAITING_SNAPSHOT, (
            f"target 上还有活的等待意图，job 却被扫成了 {refreshed.state}"
        )
        assert refreshed.active_key is not None


def test_the_scan_leaves_a_job_whose_handoff_task_is_still_queued_alone():
    """反例二的孪生形态：意图已经转交出去、那条任务还排着队（引用这条 job）。

    job 行上没有 `task_id`（转交发生在 job_service 之外），所以「没有关联任务」这句话
    如果只读 `job.task_id`，就会把这种**还活着**的 job 当成孤儿清掉。
    """
    with app.app_context():
        group = _make_group()
        stale = timedelta(seconds=job_service.JOB_STALE_SECONDS + 60)
        job = _make_job(group, state=STATE_WAITING_SNAPSHOT, age_seconds=stale.total_seconds())
        _make_task(group, job_id=job.id, status="pending")

        job_service.recover_stale_jobs(limit=_SCAN_LIMIT)
        db.session.commit()

        refreshed = db.session.get(AiAnalysisJob, job.id)
        assert refreshed.state == STATE_WAITING_SNAPSHOT, (
            f"转交出去的那条任务还排着队，job 却被扫成了 {refreshed.state}"
        )
        assert refreshed.active_key is not None


def test_the_scan_ignores_terminal_jobs_and_reports_its_counts():
    with app.app_context():
        group = _make_group()
        done = _make_job(group, state=STATE_SUCCEEDED)

        outcome = job_service.recover_stale_jobs(limit=_SCAN_LIMIT)
        db.session.commit()

        assert outcome["checked"] >= 0
        assert "settled" in outcome and "by_reason" in outcome, outcome
        assert db.session.get(AiAnalysisJob, done.id).state == STATE_SUCCEEDED


# ===========================================================================
#  五、`focus` / `requested_mode` 真的走到了执行侧（行为验收）
# ===========================================================================


def _drive_the_task(config_id: int, task_id: int):
    """走**生产那一条**：worker 的任务末端 → `run_weekly_analysis_background`。

    `run_weekly_analysis_background` 里没有配全接口（只有 Token、没有接口地址与模型），
    所以它会在建出 run 之后如实失败（`rounds_used == 0`，一次模型调用都不发生）——
    「建出了 run 并且 `request_payload` 是筛过的那一份」正是本文件要断言的东西。
    """
    worker._handle_weekly_ai_analysis_task(
        {"type": "weekly_ai_analysis", "config_id": config_id, "task_id": task_id}
    )


def _last_run_of_group(group) -> AiAnalysisRun:
    return (
        AiAnalysisRun.query.filter(AiAnalysisRun.target_key == group["group_key"])
        .order_by(AiAnalysisRun.id.desc())
        .first()
    )


def test_the_jobs_focus_reaches_the_run_the_worker_builds():
    """**行为验收**：带 `focus=table` 建 job，驱动到执行 → 那条 run 的
    `request_payload.focus.label` 是「仅配表仓库」、且 `summary.total_files` 跟着筛过。

    不许用「源码里有没有 `focus=` 这个字符串」当验收 —— 那只能证明字符串在，
    证明不了这一跳真的通了（本文件钉的就是这一跳）。
    """
    with app.app_context():
        group = _make_group(file_count=30, table_file_count=4)
        ai_service.set_project_api_key(group["project"].id, "sk-test")
        db.session.commit()

        created = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL, focus="table"
        )
        _track(created.job)
        _track_task(created.job.task_id)
        db.session.commit()
        assert created.job.focus == "table", "focus 没有落进 job 行"
        assert created.job.task_id, "job 没有排进任务，这一跳跑不起来"

        _drive_the_task(group["cfg"].id, created.job.task_id)
        db.session.commit()

        run = _last_run_of_group(group)
        assert run is not None, "这次驱动没有建出 run，用例失去了它所测的对象"
        payload = json.loads(run.request_payload)
        print(
            f"[focus 透传] run={run.id} focus={payload.get('focus')} "
            f"total_files={payload['summary'].get('total_files')} "
            f"delta_files={len(payload.get('delta_files') or [])}"
        )
        assert payload["focus"]["label"] == "仅配表仓库", (
            f"用户选的「仅配表」没有走到执行侧：run 的 focus 是 {payload['focus']}"
        )
        assert payload["focus"]["key"] == "table"
        # 计数要跟着筛选走 —— 不跟的话提示词会说「共 34 个文件」而模型只看得到 4 个。
        assert payload["summary"]["total_files"] == 4, payload["summary"]
        assert len(payload["delta_files"]) == 4
        assert all(
            str(item["file_path"]).startswith("tables/") for item in payload["delta_files"]
        )


def test_the_default_focus_changes_nothing():
    """老调用方逐字保持原行为：不传 focus 时这一段不产生任何过滤。"""
    with app.app_context():
        group = _make_group(file_count=3, table_file_count=2)
        ai_service.set_project_api_key(group["project"].id, "sk-test")
        db.session.commit()

        created = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(created.job)
        _track_task(created.job.task_id)
        db.session.commit()

        _drive_the_task(group["cfg"].id, created.job.task_id)
        db.session.commit()

        payload = json.loads(_last_run_of_group(group).request_payload)
        assert payload["focus"] == {"key": "all", "label": ""}, payload["focus"]
        assert payload["summary"]["total_files"] == 5, payload["summary"]


def test_an_explicit_full_request_overrides_the_platforms_incremental_choice():
    """**行为验收**：`requested_mode=full` → 那条 run 的 `scope == "full"`。

    对照在同一份输入上先跑一次 `build_weekly_payload`（平台自己的裁决是**增量**）——
    否则「run 的 scope 是 full」可能只是因为平台本来就选全量，这条用例什么也没证。
    """
    with app.app_context():
        group = _make_group(file_count=30, table_file_count=4, watermark=True)
        ai_service.set_project_api_key(group["project"].id, "sk-test")
        db.session.commit()

        control, _state, skip = ai_service.build_weekly_payload(group["cfg"].id)
        assert skip is None, skip
        assert control["scope"] == "incremental", (
            f"这份输入平台自己选的是 {control['scope']}，这条用例的对照不成立"
        )

        created = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_FULL
        )
        _track(created.job)
        _track_task(created.job.task_id)
        db.session.commit()
        assert created.job.requested_mode == MODE_FULL

        _drive_the_task(group["cfg"].id, created.job.task_id)
        db.session.commit()

        run = _last_run_of_group(group)
        print(f"[全量透传] run={run.id} scope={run.scope} engine_status={run.status}")
        assert run.scope == "full", (
            f"用户点了全量，执行侧却按 {run.scope} 跑了 —— 这一跳又丢了"
        )


def test_the_job_row_is_the_source_of_the_focus_not_the_payload():
    """`focus` 的来源是**job 行**（任务行上有 `job_id`）—— 读不到就退回「全部」。

    任务行上没有 job（过渡路径 / 意图 id 撞车）时必须**什么都不筛**：拿意图 id 去
    `get_job` 认一次是 `job_id_for_task` 既有的口径，`focus_for_task` 用同一把尺子。
    """
    with app.app_context():
        group = _make_group(file_count=3)
        task = _make_task(group, job_id=None, status="processing")

        assert job_service.focus_for_task(task.id) is None
        assert job_service.focus_for_task(None) is None
        assert job_service.focus_for_task(99999999) is None


def test_focus_for_task_reads_the_job_row():
    with app.app_context():
        group = _make_group()
        job = _make_job(group, state=STATE_QUEUED, focus="table")
        task = _make_task(group, job_id=job.id, status="processing")

        assert job_service.focus_for_task(task.id) == "table"

        # 「全部」与空值都归一成 None —— 过滤那一支的默认值就是「不筛」。
        all_job = _make_job(group, state=STATE_QUEUED, focus="all")
        all_task = _make_task(group, job_id=all_job.id, status="processing")
        assert job_service.focus_for_task(all_task.id) is None


def test_a_task_whose_reference_is_an_intent_id_is_not_read_as_a_job():
    """意图 id 与 job id 会撞车：认不出来就当没有 job（与 `job_id_for_task` 同一口径）。"""
    with app.app_context():
        group = _make_group()
        unrelated = _make_job(group, state=STATE_QUEUED, focus="table")
        intent_like = _make_intent(group, status="pending")
        task = _make_task(group, job_id=intent_like.id, status="processing")

        # 意图 id 恰好指向一条**存在**的 job 时才算命中（数值撞车无法从 id 上分辨）；
        # 这里我们保证它指向的不是那条 focus=table 的 job。
        if intent_like.id == unrelated.id:
            pytest.skip("意图 id 与 job id 撞上了同一个数值，这条用例的前提被破坏")
        assert job_service.focus_for_task(task.id) != "table"
