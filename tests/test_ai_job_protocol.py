# -*- coding: utf-8 -*-
"""Job 协议：把「发起任务」与「SSE 订阅」拆开（复测文档 AI-P0-01）。

## 这一组守的是什么

改动之前，`EventSource` 的 GET **既创建任务又承担进度流**：

* 同步中点击之后，浏览器补发的连接层 `error` 会把「等待同步」覆盖成「连接中断」——
  而库里那次点击好端端地登记着，页面与真实状态相反；
* 默认重连会**重新请求一个具有创建语义的 GET**，重连一次就是重复创建、重复付费；
* 刷新页面、关抽屉、断线重连**没有可恢复的身份**（只有「没有运行号所以无法确认」）。

现在的协议是：`POST /jobs` 建身份（不跑分析）→ `GET /jobs/<id>` 读身份 →
`GET /jobs/<id>/events` **只订阅**。本文件把三件事钉死：

1. **两条幂等键**都靠**唯一索引**裁决，不靠「先查再插」：同一个 `idempotency_key`
   重复 POST 只留一条 job；并发同 `active_key` 的创建也只留一条（下面用「第一个请求
   查的时候对手还没提交」精确模拟那个窗口）；
2. **订阅流不产生任何新行**（job / run / task 一条都不许多）—— 这是「重连不会创建
   第二条 run、不会再次调用模型」那条验收，用**按 job_id / target_key 过滤**的行数断言钉；
3. **创建任务的接口不是 GET**，老的那条 `/weekly/<id>/stream` 也不再创建任何东西。

## 变红意味着什么

* `test_two_creates_with_the_same_active_key_...`：裁决退回「先查再插」，用户连点两次
  或手工与定时同时触发时就会是两次真金白银的调用；
* `test_the_events_endpoint_creates_nothing`：订阅路径又开始建任务/建 run ——
  刷新一次页面就是一次付费调用；
* `test_creating_while_the_sync_is_writing_...`：同步没跑完时不再返回稳定的 `job_id`
  （回到「没有运行号所以无法确认」那一档），或者干脆没登记等待意图（用户反复点、
  每次都被同一句话挡回来）；
* `test_settle_from_run_...`：收口时不清 `active_key`，同一份输入的下一次分析
  **永远创建不出 job**（只能附着到一条已经跑完的 job 上），也就是「点了没反应」。
"""
from __future__ import annotations

# ruff: noqa: I001 —— 本文件的**导入顺序是语义要求**（见下面那段注释）：
# `services.task_worker_service` 必须排在 `services.task_worker_queue_service` 之前，
# 否则会在队列服务只加载了一半时去取它的名字（环形依赖）→ ImportError。
# isort 要的字母序恰好与这个要求相反，所以这里整文件放行 I001
# （与 `tests/test_task_lease_and_source.py` 同一条处置）。

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.ai.job_service as job_service
# **导入顺序有讲究**：`task_worker_service` 必须在 `task_worker_queue_service` **之前**
# 加载（两者互为环形依赖，见 tests/test_task_lease_and_source.py 的说明）。
import services.task_worker_service as worker
import services.task_worker_queue_service as queue_service
import services.task_worker_weekly_handlers as weekly_handlers
from app import app, create_tables, db
from models import BackgroundTask, Project, Repository, WeeklyVersionConfig
from models.ai_analysis import (
    ACTIVE_JOB_STATES,
    MODE_FULL,
    MODE_INCREMENTAL,
    SOURCE_MANUAL,
    STATE_DEGRADED,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    STATE_WAITING_SNAPSHOT,
    AiAnalysisJob,
    AiAnalysisRun,
    AiWeeklyAnalysisState,
)
from services.ai.project_config_source import build_weekly_group_key

_CREATED_IDS: dict = {"tasks": [], "jobs": [], "groups": []}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


@pytest.fixture(autouse=True)
def _cleanup_rows_this_file_creates():
    """测试库是**会话级共用**的，没有逐用例重置 —— 造出来的东西自己收干净。"""
    yield
    # 内存账本里的入队名单是模块级状态，留着会把断言弄脏。
    weekly_handlers._enqueued_weekly_ai_task_ids.clear()
    queue_service._inflight_leases.clear()
    with app.app_context():
        if _CREATED_IDS["tasks"]:
            BackgroundTask.query.filter(
                BackgroundTask.id.in_(list(_CREATED_IDS["tasks"]))
            ).delete(synchronize_session=False)
        if _CREATED_IDS["jobs"]:
            AiAnalysisJob.query.filter(
                AiAnalysisJob.id.in_(list(_CREATED_IDS["jobs"]))
            ).delete(synchronize_session=False)
        for group_key in _CREATED_IDS["groups"]:
            AiAnalysisRun.query.filter(
                AiAnalysisRun.target_key == group_key
            ).delete(synchronize_session=False)
            AiWeeklyAnalysisState.query.filter(
                AiWeeklyAnalysisState.group_key == group_key
            ).delete(synchronize_session=False)
            BackgroundTask.query.filter(
                BackgroundTask.file_path == group_key
            ).delete(synchronize_session=False)
        db.session.commit()
        _CREATED_IDS["tasks"] = []
        _CREATED_IDS["jobs"] = []
        _CREATED_IDS["groups"] = []


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _make_group(file_count: int = 0, *, start=None, end=None, resource_type="table") -> dict:
    """一个周版本分组：项目 + 仓库 + 配置（可选几行缓存，本文件基本用不到）。"""
    now = datetime.now(timezone.utc)
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()
    repo = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        resource_type=resource_type,
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    cfg = WeeklyVersionConfig(
        project_id=project.id,
        repository_id=repo.id,
        name=_uid("weekly"),
        branch="main",
        start_time=start or (now - timedelta(days=7)),
        end_time=end or now,
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.commit()
    group_key = build_weekly_group_key(cfg)
    _CREATED_IDS["groups"].append(group_key)
    return {"project": project, "repo": repo, "cfg": cfg, "group_key": group_key}


def _track(job):
    if job is not None and job.id is not None:
        _CREATED_IDS["jobs"].append(job.id)
    return job


def _track_task(task_id):
    if task_id:
        _CREATED_IDS["tasks"].append(task_id)
    return task_id


def _login(client, *, admin: bool = True):
    with client.session_transaction() as session:
        session["is_admin"] = admin
        session["admin_user"] = "job-protocol-tester"
        session["_csrf_token"] = _uid("csrf")


def _csrf(client) -> str:
    with client.session_transaction() as session:
        return session["_csrf_token"]


def _post_job(client, config_id, **body):
    return client.post(
        f"/ai-analysis/weekly/{config_id}/jobs",
        json=body,
        headers={"X-CSRF-Token": _csrf(client)},
    )


def _counts(group_key: str) -> dict:
    """这个分组名下的行数。**按 group_key 过滤，绝不数全表** —— 测试库是会话级共用的。"""
    return {
        "jobs": AiAnalysisJob.query.filter(
            AiAnalysisJob.target_key == group_key
        ).count(),
        "runs": AiAnalysisRun.query.filter(
            AiAnalysisRun.target_key == group_key
        ).count(),
        "tasks": BackgroundTask.query.filter(
            BackgroundTask.file_path == group_key
        ).count(),
        "intents": BackgroundTask.query.filter(
            BackgroundTask.file_path == group_key,
            BackgroundTask.task_type == queue_service.WAITING_INTENT_TASK_TYPE,
        ).count(),
    }


def _sync_in_flight(cfg, *, status="processing"):
    """造一条「正在写缓存」的周版本同步任务（闸门的判据）。"""
    row = BackgroundTask(
        task_type="weekly_sync",
        repository_id=cfg.repository_id,
        commit_id=str(cfg.id),
        file_path=_uid("sync"),
        priority=3,
        status=status,
        started_at=datetime.now(timezone.utc),
    )
    db.session.add(row)
    db.session.commit()
    _CREATED_IDS["tasks"].append(row.id)
    return row


def _make_run(group, *, status="succeeded", payload=None, text="报告正文") -> AiAnalysisRun:
    run = AiAnalysisRun(
        project_id=group["project"].id,
        target_type="weekly",
        target_id=group["cfg"].id,
        target_key=group["group_key"],
        status=status,
        scope="incremental",
        trigger_source="manual",
        response_payload=json.dumps(
            payload if payload is not None else {"risk_level": "low", "usage": {"a": 1}}
        ),
        response_text=text,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(run)
    db.session.commit()
    return run


def _events(text: str) -> list:
    """把 SSE 文本切成 `(name, payload)`（`id:` 行忽略）。"""
    out = []
    for block in text.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):].strip()
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if name:
            out.append((name, json.loads(data) if data else {}))
    return out


# ===========================================================================
#  一、两条幂等键：唯一索引裁决（不靠「先查再插」）
# ===========================================================================


def test_the_same_idempotency_key_twice_creates_one_job():
    """同一个客户端动作重复 POST → 同一条 job（`attached` 第二次为真）。"""
    with app.app_context():
        group = _make_group()
        key = _uid("idem")
        first = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL, idempotency_key=key
        )
        _track(first.job)
        second = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL, idempotency_key=key
        )
        db.session.commit()

        assert first.created is True
        assert second.created is False
        assert second.job_id == first.job_id
        assert (
            AiAnalysisJob.query.filter(AiAnalysisJob.idempotency_key == key).count() == 1
        ), "同一个 idempotency_key 攒出了两条 job —— 「关掉抽屉再打开又点一下」就是第二次付费"


def test_two_creates_with_the_same_active_key_leave_exactly_one_row(monkeypatch):
    """**打那个唯一索引的窗口**：第一个请求查的时候，对手刚插进去、还没提交。

    做法是让「第一次按 fingerprint 查」看不见那条行（模拟未提交/未可见），于是我们
    走到 INSERT 并被 `uq_ai_job_active_key` 拦下 —— 正确的处置是 `rollback()` 后回头
    查那一条并附着，而不是把 IntegrityError 抛给用户（那会变成 500，而库里那一条
    照旧存在，用户以为没成功就又点一次）。
    """
    with app.app_context():
        group = _make_group()
        active_key = job_service.active_key_for(
            target_type="weekly", target_key=group["group_key"], focus="all"
        )
        # 「对手」那条：内容与我们要插的一模一样（同一个目标 + 同一个范围）。
        rival = AiAnalysisJob(
            project_id=group["project"].id,
            target_type="weekly",
            target_id=group["cfg"].id,
            target_key=group["group_key"],
            requested_mode=MODE_INCREMENTAL,
            effective_mode=MODE_FULL,
            state=STATE_QUEUED,
            trigger_source=SOURCE_MANUAL,
            active_key=active_key,
            focus="all",
        )
        db.session.add(rival)
        db.session.commit()
        _track(rival)

        real = job_service._find_by_active_key
        calls = {"n": 0}

        def _blind_once(key):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(key)

        monkeypatch.setattr(job_service, "_find_by_active_key", _blind_once)
        result = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        db.session.commit()

        assert result.created is False, "撞了唯一索引却没有回头附着，而是新建了一条"
        assert result.job_id == rival.id
        assert calls["n"] >= 2, "冲突之后没有回查（那就是把异常吞掉了）"
        assert (
            AiAnalysisJob.query.filter(AiAnalysisJob.active_key == active_key).count() == 1
        ), "同一个输入指纹下攒出了两条活动 job —— 两份输入各跑一遍，两次付费"


def test_two_creates_with_the_same_idempotency_key_leave_exactly_one_row(monkeypatch):
    """同一条窗口打在**客户端幂等键**那条唯一索引上（`uq_ai_job_idempotency_key`）。"""
    with app.app_context():
        group = _make_group()
        key = _uid("idem")
        rival = AiAnalysisJob(
            project_id=group["project"].id,
            target_type="weekly",
            target_id=group["cfg"].id,
            target_key=group["group_key"],
            requested_mode=MODE_INCREMENTAL,
            state=STATE_QUEUED,
            trigger_source=SOURCE_MANUAL,
            idempotency_key=key,
            # 注意：**没有** active_key —— 这一次连输入指纹都还没算出来，
            # 唯一能拦住重复的就是幂等键那条索引。
            focus="all",
        )
        db.session.add(rival)
        db.session.commit()
        _track(rival)

        real = job_service._find_by_idempotency_key
        calls = {"n": 0}

        def _blind_once(value, *, target_key=None):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(value, target_key=target_key)

        monkeypatch.setattr(job_service, "_find_by_idempotency_key", _blind_once)
        result = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL, idempotency_key=key
        )
        db.session.commit()

        assert result.created is False
        assert result.job_id == rival.id
        assert AiAnalysisJob.query.filter(AiAnalysisJob.idempotency_key == key).count() == 1


def test_a_key_used_for_another_target_is_refused():
    """幂等键被另一个目标占用 → 报错，**不把别人那条 job 领走**。

    领走的后果是用户在这个周版本上看到了另一个周版本的分析结论（而且是付费过的），
    比一句「键被占用了」危险得多。
    """
    with app.app_context():
        first_group = _make_group()
        second_group = _make_group()
        key = _uid("idem")
        first = job_service.create_or_attach_job(
            config=first_group["cfg"], requested_mode=MODE_INCREMENTAL, idempotency_key=key
        )
        _track(first.job)
        db.session.commit()

        with pytest.raises(job_service.JobRequestError):
            job_service.create_or_attach_job(
                config=second_group["cfg"],
                requested_mode=MODE_INCREMENTAL,
                idempotency_key=key,
            )


# ===========================================================================
#  二、状态裁决：等同步 / 排队 / 首次升级为全量
# ===========================================================================


def test_creating_while_the_sync_is_writing_the_cache_waits_and_keeps_a_stable_job_id():
    """验收 1：同步中点击 → 拿到**稳定的 job_id** + `waiting_snapshot`，且零消耗。"""
    with app.app_context():
        group = _make_group()
        _sync_in_flight(group["cfg"])
        before = _counts(group["group_key"])

        job = _track(
            job_service.create_or_attach_job(
                config=group["cfg"], requested_mode=MODE_INCREMENTAL
            ).job
        )
        db.session.commit()

        assert job.id is not None, "同步没跑完时没有返回 job_id —— 回到「无法确认」那一档"
        assert job.state == STATE_WAITING_SNAPSHOT
        assert job.run_id is None, "一次模型调用都不许发生，却已经有运行号了"
        after = _counts(group["group_key"])
        assert after["runs"] == before["runs"], "等同步的路径上建了 run（那是付费调用）"
        assert after["tasks"] - before["tasks"] == 1, "登记的不该是执行任务"
        intent = BackgroundTask.query.filter(
            BackgroundTask.file_path == group["group_key"],
            BackgroundTask.task_type == queue_service.WAITING_INTENT_TASK_TYPE,
        ).first()
        assert intent is not None, "没有登记等待意图 —— 同步收尾时没人会把这次分析跑起来"
        assert intent.job_id == job.id, (
            "意图行没有指回它服务的 job —— 同步收尾转交出去的那次分析与这次点击对不上"
        )


def test_the_sync_gate_covers_the_whole_group_not_just_this_config():
    """闸门判据是**整批**（同项目同窗口的全部仓库），不是这一个 config。

    只看自己那一个仓库的同步时，另一个仓库还在写缓存照样会漏文件，而漏文件是**静默**的
    （提示词里的「共 N 个文件」跟着变小，读报告的人以为那就是全量）。
    """
    with app.app_context():
        group = _make_group()
        # 同一个项目、同一个窗口、同一个**版本名**（= 同一个分组里的另一个 config）。
        # 名字必须共用：批次判据是「同项目 + 同窗口 + 同版本名」，随手起两个不同的
        # 随机名在平台眼里就是两个版本，那样造出来的根本不是「一批」。
        sibling = WeeklyVersionConfig(
            project_id=group["project"].id,
            repository_id=group["repo"].id,
            name=group["cfg"].name,
            branch="main",
            start_time=group["cfg"].start_time,
            end_time=group["cfg"].end_time,
            is_active=True,
            auto_sync=True,
            status="active",
        )
        db.session.add(sibling)
        db.session.commit()
        _CREATED_IDS["groups"].append(build_weekly_group_key(sibling))
        _sync_in_flight(sibling)

        job = _track(
            job_service.create_or_attach_job(
                config=group["cfg"], requested_mode=MODE_INCREMENTAL
            ).job
        )
        db.session.commit()

        assert job.state == STATE_WAITING_SNAPSHOT, (
            "只看了自己那一个仓库的同步：同批次另一个仓库还在写缓存，变更清单会缺文件"
        )


def test_a_clean_queue_puts_the_job_in_queued_and_creates_the_task():
    with app.app_context():
        group = _make_group()
        result = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(result.job)
        _track_task(result.job.task_id)
        db.session.commit()

        assert result.job.state == STATE_QUEUED
        assert result.job.task_id, "排队态却没有建执行任务 —— 这次点击永远不会被跑到"
        task = db.session.get(BackgroundTask, result.job.task_id)
        assert task.task_type == "weekly_ai_analysis"
        assert task.job_id == result.job.id, "任务行没有指回它服务的 job"
        assert task.trigger_source == SOURCE_MANUAL


def test_an_incremental_first_run_is_upgraded_to_full_and_that_is_recorded():
    """没有可复用的结论基线时，平台把增量升成全量 —— **必须落库可查**。"""
    with app.app_context():
        group = _make_group()
        result = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(result.job)
        _track_task(result.job.task_id)
        db.session.commit()

        assert result.job.requested_mode == MODE_INCREMENTAL, "用户要的是增量，别改他的账"
        assert result.job.effective_mode == MODE_FULL
        assert result.job.upgrade_reason == job_service.UPGRADE_REASON_FIRST_RUN


def test_a_concluded_baseline_keeps_the_incremental_mode():
    """有可复用的结论基线时，增量就是增量（不许无理由升级成全量）。"""
    with app.app_context():
        group = _make_group()
        run = _make_run(group)
        state = AiWeeklyAnalysisState(
            project_id=group["project"].id,
            group_key=group["group_key"],
            base_name=group["cfg"].name,
            last_concluded_run_id=run.id,
        )
        db.session.add(state)
        db.session.commit()

        result = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(result.job)
        _track_task(result.job.task_id)
        db.session.commit()

        assert result.job.effective_mode == MODE_INCREMENTAL
        assert result.job.upgrade_reason in (None, "")
        assert result.job.base_run_id == run.id, "结论基线的血缘没记下来"


def test_attaching_upgrades_the_requested_mode_to_full():
    """先点增量、再点全量 = **同一个动作被加强**，不是两次分析。"""
    with app.app_context():
        group = _make_group()
        first = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(first.job)
        _track_task(first.job.task_id)
        db.session.commit()

        second = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_FULL
        )
        db.session.commit()

        assert second.created is False, "同一个目标同一份输入攒出了两条 job（= 两次付费）"
        assert second.job_id == first.job_id
        assert second.upgraded is True
        db.session.expire_all()
        stored = db.session.get(AiAnalysisJob, first.job_id)
        assert stored.requested_mode == MODE_FULL, "升级没有落库（刷新页面就看不到了）"
        assert stored.effective_mode == MODE_FULL


def test_attaching_never_downgrades_full_back_to_incremental():
    """反向**永远不降**：用户说过要全量，后面一次「增量」的点击不该把它降回去。"""
    with app.app_context():
        group = _make_group()
        first = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_FULL
        )
        _track(first.job)
        _track_task(first.job.task_id)
        db.session.commit()

        job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        db.session.commit()
        db.session.expire_all()
        stored = db.session.get(AiAnalysisJob, first.job_id)
        assert stored.requested_mode == MODE_FULL


# ===========================================================================
#  三、执行侧写回：mark_running / settle_from_run
# ===========================================================================


@pytest.mark.parametrize(
    "run_status,expected",
    [
        (STATE_SUCCEEDED, STATE_SUCCEEDED),
        # `degraded` 是**终态且有可复用结论**：折叠成 succeeded 会让用量面板多算一次
        # 「完整通过」，当成 failed 会让降级结论在界面上消失。
        (STATE_DEGRADED, STATE_DEGRADED),
        (STATE_FAILED, STATE_FAILED),
    ],
)
def test_settle_from_run_maps_the_status_and_clears_the_active_key(run_status, expected):
    with app.app_context():
        group = _make_group()
        result = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(result.job)
        _track_task(result.job.task_id)
        db.session.commit()

        run = _make_run(group, status=run_status, text="")
        if run_status == STATE_FAILED:
            run.error_message = "模型没答上来"
            db.session.commit()

        running = job_service.mark_running(
            result.job_id,
            run_id=run.id,
            task_id=result.job.task_id,
            lease_seconds=600,
        )
        db.session.commit()
        assert running.state == STATE_RUNNING
        assert running.lease_expires_at is not None

        settled = job_service.settle_from_run(run)
        db.session.commit()

        assert settled is not None
        assert settled.state == expected
        assert settled.finished_at is not None
        assert settled.active_key is None, (
            "收口时没清 active_key：同一份输入的下一次分析永远创建不出 job（点了没反应）"
        )
        if run_status == STATE_FAILED:
            assert settled.error_message == "模型没答上来"


def test_a_finished_job_does_not_block_the_next_create():
    """跑完就清空 → 同一份输入的**下一次**分析必须能建出新的 job。"""
    with app.app_context():
        group = _make_group()
        first = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(first.job)
        _track_task(first.job.task_id)
        db.session.commit()

        run = AiAnalysisRun(
            project_id=group["project"].id,
            target_type="weekly",
            target_id=group["cfg"].id,
            target_key=group["group_key"],
            status=STATE_SUCCEEDED,
            scope="incremental",
            trigger_source="manual",
        )
        db.session.add(run)
        db.session.commit()
        job_service.mark_running(first.job_id, run_id=run.id)
        job_service.settle_from_run(run)
        db.session.commit()

        second = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(second.job)
        _track_task(second.job.task_id)
        db.session.commit()

        assert second.created is True, "上一次跑完之后，这一份输入再也建不出 job 了"
        assert second.job_id != first.job_id


def test_mark_running_publishes_the_runtime_scope_before_the_run_finishes():
    """运行一创建，轮询端点就应看到真实 scope，不能等结算时才从 incremental 改 full。"""
    with app.app_context():
        group = _make_group()
        result = job_service.create_or_attach_job(
            config=group["cfg"], requested_mode=MODE_INCREMENTAL
        )
        _track(result.job)
        _track_task(result.job.task_id)
        db.session.commit()

        running = job_service.mark_running(
            result.job_id,
            run_id=987654,
            effective_mode=MODE_FULL,
            upgrade_reason="critical_path_detected",
        )
        db.session.commit()

        assert running.state == STATE_RUNNING
        assert running.effective_mode == MODE_FULL
        assert running.upgrade_reason == "critical_path_detected"


def test_a_late_mark_running_does_not_resurrect_a_finished_job():
    with app.app_context():
        group = _make_group()
        job = _track(
            job_service.create_or_attach_job(
                config=group["cfg"], requested_mode=MODE_INCREMENTAL
            ).job
        )
        _track_task(job.task_id)
        job.state = STATE_SUCCEEDED
        db.session.commit()

        again = job_service.mark_running(job.id, run_id=999)
        assert again.state == STATE_SUCCEEDED, "迟到的 mark_running 把跑完的 job 推回了「分析中」"


# ===========================================================================
#  四、端点：创建不是 GET / 读身份 / 只订阅
# ===========================================================================


def test_the_create_endpoint_is_not_a_get():
    """验收 4：创建任务的接口不许是 GET（GET 会被重放，而且绕过 CSRF）。"""
    with app.app_context():
        group = _make_group()
        with app.test_client() as client:
            _login(client)
            assert client.get(f"/ai-analysis/weekly/{group['cfg'].id}/jobs").status_code == 405
            created = _post_job(client, group["cfg"].id, analysis_mode="incremental")
            assert created.status_code == 200, created.get_json()

        body = created.get_json()
        assert body["success"] is True
        assert body["job_id"]
        assert body["state"] in (STATE_QUEUED, STATE_WAITING_SNAPSHOT)
        assert body["attached"] is False
        _track(db.session.get(AiAnalysisJob, body["job_id"]))
        job = db.session.get(AiAnalysisJob, body["job_id"])
        _track_task(job.task_id)
        db.session.commit()


def test_an_invalid_analysis_mode_is_rejected_not_silently_incremental():
    """非法模式 → 400。**静默当增量**会让「我点了全量」变成一句谎话。"""
    with app.app_context():
        group = _make_group()
        with app.test_client() as client:
            _login(client)
            response = _post_job(client, group["cfg"].id, analysis_mode="FULL SPEED")
        assert response.status_code == 400
        assert "analysis_mode" in response.get_json()["message"]
        assert _counts(group["group_key"])["jobs"] == 0, "被拒的请求却建了 job"


def test_the_job_can_be_read_back_by_id_and_is_404_for_strangers():
    """验收 2（服务端一半）：刷新 / 关抽屉再打开时能按 `job_id` 拿回同一条身份。"""
    with app.app_context():
        group = _make_group()
        with app.test_client() as client:
            _login(client)
            created = _post_job(client, group["cfg"].id, analysis_mode="incremental").get_json()
            job_id = created["job_id"]
            _track(db.session.get(AiAnalysisJob, job_id))
            job = db.session.get(AiAnalysisJob, job_id)
            _track_task(job.task_id)
            db.session.commit()

            first = client.get(f"/ai-analysis/jobs/{job_id}")
            second = client.get(f"/ai-analysis/jobs/{job_id}")

        assert first.status_code == 200
        assert second.get_json()["job"] == first.get_json()["job"], (
            "两次读到的身份不一致 —— 刷新之后页面拿不到同一次点击"
        )
        assert first.get_json()["job"]["job_id"] == job_id
        with app.test_client() as client:
            _login(client)
            assert client.get("/ai-analysis/jobs/99999999").status_code == 404

        import routes.ai_analysis_routes as ai_routes

        original = ai_routes._has_project_access
        ai_routes._has_project_access = lambda _pid: False
        try:
            with app.test_client() as client:
                _login(client)
                denied = client.get(f"/ai-analysis/jobs/{job_id}")
        finally:
            ai_routes._has_project_access = original
        assert denied.status_code == 403


def test_the_events_endpoint_creates_nothing_on_reconnect():
    """验收 3：**重连事件流不创建第二条 run、不再次调用模型**（用行数断言钉）。

    订阅两次（第二次带上 `Last-Event-ID`，就是浏览器自动重连的形状），
    前后按 `group_key` 过滤的行数必须**逐项相等**。
    """
    with app.app_context():
        group = _make_group()
        with app.test_client() as client:
            _login(client)
            created = _post_job(client, group["cfg"].id, analysis_mode="incremental").get_json()
            job_id = created["job_id"]
        _track(db.session.get(AiAnalysisJob, job_id))
        job = db.session.get(AiAnalysisJob, job_id)
        _track_task(job.task_id)
        db.session.commit()
        before = _counts(group["group_key"])

        with app.test_client() as client:
            _login(client)
            # 第一次订阅：读首帧就断开（模拟用户关抽屉）。
            stream = client.get(f"/ai-analysis/jobs/{job_id}/events", buffered=False)
            chunks = []
            for chunk in stream.response:
                chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
                if len(chunks) >= 1:
                    break
            stream.close()
            # 第二次订阅：浏览器自动重连会带上 Last-Event-ID。
            again = client.get(
                f"/ai-analysis/jobs/{job_id}/events",
                headers={"Last-Event-ID": "1"},
                buffered=False,
            )
            for chunk in again.response:
                break
            again.close()

        after = _counts(group["group_key"])
        assert after == before, f"订阅本身产生了新的行：{before} → {after}"
        assert "event: state" in "".join(chunks), chunks


def test_the_events_stream_of_a_finished_job_sends_the_result_and_closes():
    """终态 job：立刻发一条 `result`（与抽屉既有的那一份同形）然后收尾关闭。"""
    with app.app_context():
        group = _make_group()
        with app.test_client() as client:
            _login(client)
            created = _post_job(client, group["cfg"].id, analysis_mode="incremental").get_json()
            job_id = created["job_id"]
        job = db.session.get(AiAnalysisJob, job_id)
        _track(job)
        _track_task(job.task_id)
        run = _make_run(
            group,
            payload={"risk_level": "high", "usage": {"tokens": 5}, "degradation_label": "x"},
        )
        job_service.mark_running(job_id, run_id=run.id)
        job_service.settle_from_run(run)
        db.session.commit()

        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/jobs/{job_id}/events")
            text = response.get_data(as_text=True)

        events = _events(text)
        names = [name for name, _ in events]
        assert names[0] == "state", events
        assert names[-1] == "result", f"终态的事件流没有以 result 收尾：{names}"
        payload = events[-1][1]
        assert payload["run_id"] == run.id
        assert payload["status"] == STATE_SUCCEEDED
        # 抽屉的 `result` 处理器读的就是这几个键（`risk_level` / `usage`）。
        assert payload["risk_level"] == "high"
        assert payload["usage"] == {"tokens": 5}


def test_a_waiting_job_streams_the_waiting_event_with_the_server_message():
    """`waiting_snapshot` → 一条 `waiting`（文案由服务端给），且不建任何东西。"""
    with app.app_context():
        group = _make_group()
        _sync_in_flight(group["cfg"])
        with app.test_client() as client:
            _login(client)
            created = _post_job(client, group["cfg"].id, analysis_mode="incremental").get_json()
            job_id = created["job_id"]
        _track(db.session.get(AiAnalysisJob, job_id))
        before = _counts(group["group_key"])

        with app.test_client() as client:
            _login(client)
            stream = client.get(f"/ai-analysis/jobs/{job_id}/events", buffered=False)
            text = ""
            for chunk in stream.response:
                text += chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                if "event: waiting" in text:
                    break
            stream.close()

        events = _events(text)
        names = [name for name, _ in events]
        assert names[0] == "state", events
        assert events[0][1]["state"] == STATE_WAITING_SNAPSHOT
        assert "waiting" in names, f"等同步的订阅流没有发 waiting：{names}"
        waiting = [payload for name, payload in events if name == "waiting"][0]
        assert waiting["message"], "waiting 事件没有给页面一句话（页面只能自己编）"
        assert _counts(group["group_key"]) == before, "订阅等同步的流时建了新的行"


def test_the_old_weekly_stream_route_no_longer_creates_anything():
    """老路由改成**只订阅**：没有 job_id 时 400 并指路，有 job_id 时才有事件流。"""
    with app.app_context():
        group = _make_group()
        with app.test_client() as client:
            _login(client)
            before = _counts(group["group_key"])
            refused = client.get(f"/ai-analysis/weekly/{group['cfg'].id}/stream")

        assert refused.status_code == 400, (
            "老路由没有 job_id 就放行 —— 它又变成了一个「GET 即创建」的接口"
        )
        assert "/jobs" in refused.get_json()["message"], "拒绝的时候没有告诉调用方去哪儿创建"
        assert _counts(group["group_key"]) == before, "被拒的请求却建了行"

        with app.test_client() as client:
            _login(client)
            created = _post_job(client, group["cfg"].id, analysis_mode="incremental").get_json()
            job_id = created["job_id"]
        _track(db.session.get(AiAnalysisJob, job_id))
        job = db.session.get(AiAnalysisJob, job_id)
        _track_task(job.task_id)
        db.session.commit()

        with app.test_client() as client:
            _login(client)
            stream = client.get(
                f"/ai-analysis/weekly/{group['cfg'].id}/stream?job_id={job_id}",
                buffered=False,
            )
            for chunk in stream.response:
                break
            stream.close()
        # 别人的 job（属于另一个分组）一律 404：换个 config_id 读不到别人的进度。
        other = _make_group()
        with app.test_client() as client:
            _login(client)
            assert (
                client.get(
                    f"/ai-analysis/weekly/{other['cfg'].id}/stream?job_id={job_id}"
                ).status_code
                == 404
            )


# ===========================================================================
#  五、模板：点击 → POST /jobs → 用 job_id 订阅（静态断言，先剥注释）
# ===========================================================================


def _template_source(rel: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")


def _strip_js_comments(code: str) -> str:
    """剥掉 JS 注释（字符串字面量里的 `//` 不动）。

    **必须先剥再断言**：这些模板的注释里原样写着「以前是 stream?focus=」这类句子，
    不剥就会假红/假绿（见 `tests/test_ai_usage_dual_scope_ui.py` 的同一套说明）。
    """
    out: list = []
    index, length = 0, len(code)
    quote = ""
    while index < length:
        char = code[index]
        if quote:
            out.append(char)
            if char == "\\" and quote != "`":
                if index + 1 < length:
                    out.append(code[index + 1])
                    index += 2
                    continue
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and code[index + 1] == "/":
            while index < length and code[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and code[index + 1] == "*":
            index += 2
            while index + 1 < length and not (code[index] == "*" and code[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


@pytest.mark.parametrize(
    "rel",
    ["templates/weekly_version_diff.html", "templates/merged_project_view.html"],
)
def test_the_weekly_drawers_create_a_job_then_subscribe_by_id(rel):
    code = _strip_js_comments(_template_source(rel))

    assert "/jobs`" in code or "/jobs'" in code or "/jobs\"" in code, (
        f"{rel} 没有 POST 到 /jobs —— 抽屉还在靠流式 GET 创建任务"
    )
    assert "method: 'POST'" in code or 'method: "POST"' in code, (
        f"{rel} 的发起动作不是 POST"
    )
    assert "idempotency_key" in code, (
        f"{rel} 没有带客户端幂等键：同一次点击重试会变成第二次付费调用"
    )
    assert "/events" in code, f"{rel} 没有订阅 /jobs/<id>/events"
    # 旧的流式入口（GET 即创建）不许再留在抽屉里。
    assert "/stream?source=manual" not in code, (
        f"{rel} 仍然在用那条「GET 即创建任务」的流式入口"
    )


@pytest.mark.parametrize(
    "rel",
    ["templates/weekly_version_diff.html", "templates/merged_project_view.html"],
)
def test_the_drawers_keep_the_settled_guard_and_recover_by_job_id(rel):
    code = _strip_js_comments(_template_source(rel))

    assert "StreamSettled) return;" in code, (
        f"{rel} 的 error 处理器丢了「终结事件之后一律早退」的守卫 —— "
        "waiting 之后浏览器补发的那条 error 会把「等待同步」覆盖成「连接中断」"
    )
    # 刷新 / 关抽屉再打开要能用**已有的 job_id** 恢复。
    assert "ai_job" in code, f"{rel} 没有把 job_id 放进 URL：刷新之后找不到同一次点击"
    assert "history.replaceState" in code, (
        f"{rel} 没有把 job_id 写进地址栏（刷新即丢失这次点击的身份）"
    )


# ==========================================================================
#  六、模板里那段 JS 必须**能通过 node 语法检查**
#
#  这一条是**补上来的**：上面那些静态断言只看得见字符串，而 node 探针只取单个函数体
#  （`_function_source` / `_handler_body`），于是「整段脚本里有一处语法错误」这种缺陷
#  两头都漏 —— 而无头 Chrome 复核当场撞到了它（模板里少定义一个函数 + 一处字符串里
#  混进了真换行，表现是**整个抽屉都不响应**：语法错误的 script 块里所有监听器都不会注册）。
# ==========================================================================


@pytest.mark.parametrize(
    "rel",
    ["templates/weekly_version_diff.html", "templates/merged_project_view.html"],
)
def test_the_template_inline_scripts_are_valid_javascript(rel, tmp_path):
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过语法检查")
    import re

    html = _template_source(rel)
    # Jinja 占位符不是 JS：`{{ ... }}` 换成 `null`、`{% ... %}` 整段去掉 ——
    # 这一段量的是「去掉模板语法之后，JS 本身合不合语法」，不是渲染结果。
    html = re.sub(r"\{\{.*?\}\}", "null", html, flags=re.S)
    html = re.sub(r"\{%.*?%\}", "", html, flags=re.S)
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert blocks, f"{rel} 里找不到内联脚本"
    for index, block in enumerate(blocks):
        target = tmp_path / f"{rel.split('/')[-1]}.{index}.js"
        target.write_text(block, encoding="utf-8")
        result = subprocess.run(
            ["node", "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
        )
        assert result.returncode == 0, (
            f"{rel} 第 {index} 段内联脚本有语法错误 —— 那个 script 块里的监听器一个都不会"
            f"注册，表现是整个抽屉不响应：\n{result.stderr[:800]}"
        )


def test_the_events_stream_supports_last_event_id():
    """`Last-Event-ID`：断线重连时浏览器会带上最后收到的 `id`。

    这里的实现是**从它继续编号**，并把收到的值回显在首帧上（不做历史回放 ——
    每一帧都是「当前状态」的完整快照，重放历史只会让页面把同一件事演两遍）。
    """
    with app.app_context():
        group = _make_group()
        with app.test_client() as client:
            _login(client)
            created = _post_job(client, group["cfg"].id, analysis_mode="incremental").get_json()
            job_id = created["job_id"]
        job = db.session.get(AiAnalysisJob, job_id)
        _track(job)
        _track_task(job.task_id)
        job.state = STATE_SUCCEEDED
        db.session.commit()

        with app.test_client() as client:
            _login(client)
            response = client.get(
                f"/ai-analysis/jobs/{job_id}/events",
                headers={"Last-Event-ID": "7"},
            )
            text = response.get_data(as_text=True)

        assert "id: 8" in text, text[:200]
        assert '"last_event_id": "7"' in text, text[:300]
        events = _events(text)
        assert [name for name, _ in events][-1] == "result"
