# -*- coding: utf-8 -*-
"""Agent 任务回传完整性回归（AS-B01 / AS-B02 复现 + 上一轮批次栅栏的可证伪检查）。

本文件只通过 HTTP 接口（register / claim / result）驱动被测代码，不直接改服务内部状态，
因此同一份用例既能在修复前的基线上复现问题，也能在修复后跑通。
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from app import app, create_tables, db
from models import AgentNode, AgentTask, BackgroundTask, Project

SECRET = "task-integrity-secret"


def _register(client, headers, code):
    response = client.post(
        "/api/agents/register",
        headers=headers,
        json={"agent_code": code, "project_codes": [code]},
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AGENT_SHARED_SECRET", SECRET)
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")
    with app.app_context():
        create_tables()
        client = app.test_client()
        headers = {"X-Agent-Secret": SECRET}
        code_a = "integ_a_" + uuid.uuid4().hex[:8]
        code_b = "integ_b_" + uuid.uuid4().hex[:8]
        data_a = _register(client, headers, code_a)
        data_b = _register(client, headers, code_b)
        yield SimpleNamespace(
            client=client,
            headers=headers,
            a={"agent_code": code_a, "agent_token": data_a["agent_token"]},
            b={"agent_code": code_b, "agent_token": data_b["agent_token"]},
            agent_a=AgentNode.query.filter_by(agent_code=code_a).one().id,
            project_a=Project.query.filter_by(code=code_a).one().id,
            project_b=Project.query.filter_by(code=code_b).one().id,
        )


def make_task(project_id, **kwargs):
    kwargs.setdefault("task_type", "noop")
    kwargs.setdefault("payload", "{}")
    task = AgentTask(project_id=project_id, **kwargs)
    db.session.add(task)
    db.session.commit()
    return task.id


def claim(env, who):
    response = env.client.post("/api/agents/tasks/claim", headers=env.headers, json=getattr(env, who))
    assert response.status_code == 200, response.get_json()
    return response.get_json()["task"]


def report(env, who, task_id, **kwargs):
    return env.client.post(
        f"/api/agents/tasks/{task_id}/result",
        headers=env.headers,
        json={**getattr(env, who), "status": "completed", **kwargs},
    )


def claim_own_task(env, who, project_id, **overrides):
    """登记一条属于该 Agent 项目的任务并领取，返回 (task_id, attempt)。"""
    task_id = make_task(project_id, status="pending", **overrides)
    task = claim(env, who)
    assert task is not None and task["id"] == task_id
    return task_id, int(task.get("attempt", 0))


def temp_cache_fetch_count():
    return AgentTask.query.filter_by(task_type="temp_cache_fetch").count()


class _ConcurrentWinner:
    """在回传接口的第一条 UPDATE agent_tasks 之前，用另一条连接抢先落库。"""

    def __init__(self, sql, params):
        self.sql = sql
        self.params = params
        self.fired = False

    def __enter__(self):
        event.listen(db.engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc):
        event.remove(db.engine, "before_cursor_execute", self._hook)

    def _hook(self, conn, cursor, statement, parameters, context, executemany):
        if self.fired or not statement.startswith("UPDATE agent_tasks"):
            return
        self.fired = True
        with db.engine.begin() as other:
            other.execute(text(self.sql), self.params)


# ---------------------------------------------------------------------------
# AS-B01：未领取（或属于其他项目）的任务不能被回传结果
# ---------------------------------------------------------------------------
def test_cross_project_agent_cannot_complete_unclaimed_task(env):
    """AS-B01：有效 Agent 访问其他项目尚未分配的任务，必须被拒绝且不落任何状态。"""
    source = BackgroundTask(task_type="noop", status="processing")
    db.session.add(source)
    db.session.flush()
    source_id = source.id
    # 任务属于 B 的项目，且尚未被任何 Agent 领取。
    task_id = make_task(env.project_b, status="pending", source_task_id=source_id)

    response = report(env, "a", task_id, status="completed")

    assert response.status_code == 403, response.get_json()
    db.session.expire_all()
    stored = db.session.get(AgentTask, task_id)
    assert stored.status == "pending"
    assert stored.assigned_agent_id is None
    src = db.session.get(BackgroundTask, source_id)
    assert src.status == "processing"
    assert src.retry_count in (0, None)


# ---------------------------------------------------------------------------
# AS-B02：终态不可覆盖 / 重复回传副作用幂等
# ---------------------------------------------------------------------------
def test_terminal_result_cannot_be_overwritten(env):
    """AS-B02：成功与失败互为终态，重复同终态回传必须幂等。"""
    task_id, attempt = claim_own_task(env, "a", env.project_a)

    first = report(env, "a", task_id, attempt=attempt, status="completed")
    assert first.status_code == 200, first.get_json()

    overwrite = report(env, "a", task_id, attempt=attempt, status="failed", error_message="late failure")
    assert overwrite.status_code == 409, overwrite.get_json()

    duplicate = report(env, "a", task_id, attempt=attempt, status="completed")
    assert duplicate.status_code == 200, duplicate.get_json()
    assert duplicate.get_json().get("duplicate") is True

    db.session.expire_all()
    stored = db.session.get(AgentTask, task_id)
    assert stored.status == "completed"
    assert stored.error_message is None


def test_duplicate_failed_report_counts_source_retry_once(env):
    """AS-B02：同一次失败被回传两次，源任务重试计数只能 +1，错误信息保留首次。"""
    source = BackgroundTask(task_type="noop", status="processing")
    db.session.add(source)
    db.session.flush()
    source_id = source.id
    task_id, attempt = claim_own_task(env, "a", env.project_a, source_task_id=source_id)

    first = report(env, "a", task_id, attempt=attempt, status="failed", error_message="first")
    assert first.status_code == 200, first.get_json()
    second = report(env, "a", task_id, attempt=attempt, status="failed", error_message="duplicate")
    assert second.status_code == 200, second.get_json()

    db.session.expire_all()
    assert db.session.get(BackgroundTask, source_id).retry_count == 1
    assert db.session.get(AgentTask, task_id).error_message == "first"


def test_duplicate_success_creates_single_prefetch_task(env):
    """AS-B02：同一次成功回传两次，只允许派发一条 temp_cache_fetch 预取任务。"""
    task_id, attempt = claim_own_task(env, "a", env.project_a, task_type="commit_diff")
    before = temp_cache_fetch_count()

    payload = {
        "cache_key": f"commit_diff:{task_id}:deadbeef",
        "payload_hash": "deadbeef",
        "payload_size": 128,
        "prefetch_platform_cache": True,
    }
    first = report(env, "a", task_id, attempt=attempt, result_payload=payload)
    assert first.status_code == 200, first.get_json()
    second = report(env, "a", task_id, attempt=attempt, result_payload=payload)
    assert second.status_code == 200, second.get_json()

    assert temp_cache_fetch_count() - before == 1


def test_losing_batch_does_not_apply_side_effects(env):
    """抢占失败的批次：不得派发预取任务，也不得改写源任务状态。"""
    source = BackgroundTask(task_type="noop", status="processing")
    db.session.add(source)
    db.session.flush()
    source_id = source.id
    task_id, attempt = claim_own_task(env, "a", env.project_a, task_type="commit_diff", source_task_id=source_id)
    before = temp_cache_fetch_count()

    with _ConcurrentWinner(
        "UPDATE agent_tasks SET status = 'completed', result_summary = 'winner' WHERE id = :id",
        {"id": task_id},
    ) as winner:
        response = report(
            env,
            "a",
            task_id,
            attempt=attempt,
            result_payload={
                "cache_key": f"commit_diff:{task_id}:cafebabe",
                "payload_hash": "cafebabe",
                "payload_size": 64,
                "prefetch_platform_cache": True,
            },
        )

    assert winner.fired
    assert response.status_code == 200, response.get_json()
    assert response.get_json().get("duplicate") is True
    assert temp_cache_fetch_count() - before == 0
    db.session.expire_all()
    assert db.session.get(AgentTask, task_id).result_summary == "winner"
    src = db.session.get(BackgroundTask, source_id)
    assert src.status == "processing"
    assert src.retry_count in (0, None)


def test_losing_failed_batch_does_not_mark_source_failed(env):
    """抢占失败的失败回传：不得把源任务改成 failed，也不得累加重试计数。"""
    source = BackgroundTask(task_type="noop", status="processing")
    db.session.add(source)
    db.session.flush()
    source_id = source.id
    task_id, attempt = claim_own_task(env, "a", env.project_a, source_task_id=source_id)

    with _ConcurrentWinner(
        "UPDATE agent_tasks SET status = 'completed' WHERE id = :id",
        {"id": task_id},
    ) as winner:
        response = report(env, "a", task_id, attempt=attempt, status="failed", error_message="stale")

    assert winner.fired
    assert response.status_code == 409, response.get_json()
    db.session.expire_all()
    src = db.session.get(BackgroundTask, source_id)
    assert src.status == "processing"
    assert src.retry_count in (0, None)
    assert db.session.get(AgentTask, task_id).status == "completed"


# ---------------------------------------------------------------------------
# 怀疑点 1：retry_count 复用为 attempt 是否与源任务失败计数互相污染
# ---------------------------------------------------------------------------
def test_attempt_fence_is_independent_of_source_retry_counter(env):
    """源任务失败计数 +1 属于 BackgroundTask，不得影响 AgentTask 的执行批次栅栏。"""
    source = BackgroundTask(task_type="noop", status="processing")
    db.session.add(source)
    db.session.flush()
    source_id = source.id

    first_id, first_attempt = claim_own_task(env, "a", env.project_a, source_task_id=source_id)
    assert report(env, "a", first_id, attempt=first_attempt, status="failed", error_message="boom").status_code == 200
    db.session.expire_all()
    assert db.session.get(BackgroundTask, source_id).retry_count == 1
    # 源任务计数递增不应该动到 AgentTask 自己的批次号。
    assert db.session.get(AgentTask, first_id).retry_count == first_attempt

    # 平台为同一个源任务重新下发的第二个批次（新 AgentTask，批次重新从 0 开始）。
    second_id = make_task(env.project_a, status="pending", source_task_id=source_id)
    second = claim(env, "a")
    assert second["id"] == second_id
    assert int(second["attempt"]) == 0
    assert report(env, "a", second_id, attempt=0, status="completed").status_code == 200

    db.session.expire_all()
    assert db.session.get(BackgroundTask, source_id).status == "completed"
    assert db.session.get(BackgroundTask, source_id).error_message is None


# ---------------------------------------------------------------------------
# 怀疑点 2：接口是否用陈旧快照判断终态
# ---------------------------------------------------------------------------
def test_live_batch_result_is_not_dropped_by_stale_terminal_snapshot(env):
    """会话身份映射里残留终态快照时，当前批次的有效回传不能被误判成 duplicate 丢弃。

    生产环境每个请求使用独立 Session，读取的就是库内当前行；本用例是防御性检查，
    锁定「结果接口必须按数据库当前行判定终态」这一行为。
    """
    task_id = make_task(
        env.project_a,
        status="completed",
        assigned_agent_id=env.agent_a,
        retry_count=1,
    )
    stale = db.session.get(AgentTask, task_id)
    assert stale.status == "completed"
    # 同一条任务行被重新下发为新批次：库里回到 processing，而会话里仍是终态快照。
    with db.engine.begin() as other:
        other.execute(
            text("UPDATE agent_tasks SET status = 'processing' WHERE id = :id"),
            {"id": task_id},
        )

    response = report(env, "a", task_id, attempt=1, status="completed", result_summary="live-batch")

    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    assert db.session.get(AgentTask, task_id).result_summary == "live-batch"


# ---------------------------------------------------------------------------
# 怀疑点 3：租约回收后旧批次不得再回传，副作用只能由获胜批次执行
# ---------------------------------------------------------------------------
def test_expired_batch_cannot_report_after_reclaim(env):
    task_id = make_task(env.project_a, status="pending")
    first = claim(env, "a")
    assert first["id"] == task_id
    first_attempt = int(first["attempt"])

    db.session.get(AgentTask, task_id).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()
    second = claim(env, "a")
    assert second["id"] == task_id
    second_attempt = int(second["attempt"])
    assert second_attempt > first_attempt

    stale = report(env, "a", task_id, attempt=first_attempt, status="completed", result_summary="stale")
    assert stale.status_code == 409, stale.get_json()
    fresh = report(env, "a", task_id, attempt=second_attempt, status="completed", result_summary="fresh")
    assert fresh.status_code == 200, fresh.get_json()

    db.session.expire_all()
    assert db.session.get(AgentTask, task_id).result_summary == "fresh"


def test_reclaim_cannot_overwrite_concurrent_completion(env):
    """回收与并发完成竞争：回收 UPDATE 必须重新校验 status，不能把已完成的任务打回 pending。"""
    task_id = make_task(
        env.project_a,
        status="processing",
        assigned_agent_id=env.agent_a,
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )

    with _ConcurrentWinner(
        "UPDATE agent_tasks SET status = 'completed', lease_expires_at = NULL WHERE id = :id",
        {"id": task_id},
    ) as winner:
        assert claim(env, "a") is None

    assert winner.fired
    db.session.expire_all()
    stored = db.session.get(AgentTask, task_id)
    assert stored.status == "completed"
    assert stored.retry_count == 0
    assert stored.assigned_agent_id == env.agent_a


# ---------------------------------------------------------------------------
# 4.3：心跳线程的续租与退出
# ---------------------------------------------------------------------------
def test_task_heartbeat_stops_when_batch_is_gone(monkeypatch):
    """批次被判失效（409）后心跳线程必须立即停止，不能对已回收的任务无限续租。"""
    import time as _time
    from types import SimpleNamespace as _NS

    from agent import task_heartbeat

    calls = []

    def post(url, payload, **kwargs):
        calls.append(payload)
        return 409, {"success": False}

    monkeypatch.setattr(task_heartbeat, "post_json", post)
    settings = _NS(
        platform_base_url="http://platform",
        agent_code="node",
        heartbeat_interval_seconds=0.01,
    )
    with task_heartbeat.TaskHeartbeat(settings, {"id": 42, "attempt": 3}, "token", {}) as pulse:
        deadline = _time.monotonic() + 2
        while _time.monotonic() < deadline and pulse.thread.is_alive():
            _time.sleep(0.01)
    assert not pulse.thread.is_alive(), "批次失效后心跳线程必须退出"
    assert calls and calls[0]["attempt"] == 3


def test_stale_task_heartbeat_still_refreshes_agent_liveness(env):
    """长任务期间任务被回收后，带 task_id 的心跳仍必须刷新节点存活信息。

    只保留「409 批次失效」的语义（Agent 侧靠这个状态码退出续租线程），但节点本身还活着
    在算，runtime 字段必须落库，否则管理页会把活节点显示成离线。
    """
    task_id, attempt = claim_own_task(env, "a", env.project_a)

    # 模拟租约过期被平台回收：回到 pending、解除归属、批次 +1。
    stale = db.session.get(AgentTask, task_id)
    stale.status = "pending"
    stale.assigned_agent_id = None
    stale.retry_count = attempt + 1
    stale.lease_expires_at = None
    agent = AgentNode.query.filter_by(id=env.agent_a).one()
    agent.last_heartbeat = None
    agent.status = "offline"
    db.session.commit()
    agent_id = env.agent_a

    response = env.client.post(
        "/api/agents/heartbeat",
        headers=env.headers,
        json={**env.a, "task_id": task_id, "attempt": attempt, "status": "online", "lease_seconds": 600},
    )

    # 批次失效语义不变：状态码与 success 标志都不能改。
    assert response.status_code == 409, response.get_json()
    assert response.get_json().get("success") is False

    db.session.expire_all()
    refreshed = db.session.get(AgentNode, agent_id)
    assert refreshed.last_heartbeat is not None, "批次失效的节点心跳也必须刷新 last_heartbeat"
    assert refreshed.status == "online"
    # 续租本身仍然必须拒绝：不能把已回收的任务重新挂回这个节点。
    stored = db.session.get(AgentTask, task_id)
    assert stored.status == "pending"
    assert stored.assigned_agent_id is None
    assert stored.lease_expires_at is None


# ---------------------------------------------------------------------------
# 怀疑点 5：源任务状态回写
# ---------------------------------------------------------------------------
def test_terminal_task_never_writes_back_to_source_task(env):
    """已结束的任务再次回传：源任务状态与重试计数都不能被改动。"""
    source = BackgroundTask(task_type="noop", status="processing")
    db.session.add(source)
    db.session.flush()
    source_id = source.id
    task_id, attempt = claim_own_task(env, "a", env.project_a, source_task_id=source_id)

    assert report(env, "a", task_id, attempt=attempt, status="completed").status_code == 200
    db.session.expire_all()
    assert db.session.get(BackgroundTask, source_id).status == "completed"

    late = report(env, "a", task_id, attempt=attempt, status="failed", error_message="late")
    assert late.status_code == 409, late.get_json()
    duplicate = report(env, "a", task_id, attempt=attempt, status="completed")
    assert duplicate.status_code == 200, duplicate.get_json()

    db.session.expire_all()
    src = db.session.get(BackgroundTask, source_id)
    assert src.status == "completed"
    assert src.error_message is None
    assert src.retry_count in (0, None)


# ---------------------------------------------------------------------------
# 4.5：auto_sync 结果应用的事务边界
# ---------------------------------------------------------------------------
def test_auto_sync_result_rolls_back_weekly_dispatch_atomically(env, monkeypatch):
    """周版本任务派发之后抛错时，提交记录、派生的 AgentTask 与主任务状态必须一起回滚。

    内部任何一次 commit 都会让「已提交的派生任务」残留下来，或在主任务还没落终态时
    就对外可见——这两种情况都会让故障注入后的断言失败。
    """
    from models import Commit, Repository, WeeklyVersionConfig
    from services import agent_management_handlers as handlers

    repository = Repository(
        project_id=env.project_a,
        name="integrity-repo",
        type="git",
        url="https://example.com/integrity.git",
        branch="main",
    )
    db.session.add(repository)
    db.session.flush()
    now = datetime.now(timezone.utc)
    config = WeeklyVersionConfig(
        project_id=env.project_a,
        repository_id=repository.id,
        name="integrity-weekly",
        branch="main",
        start_time=now,
        end_time=now + timedelta(days=7),
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(config)
    db.session.commit()
    repository_id = repository.id
    config_id = config.id

    # 直接构造已领取状态的任务：这样用例在「批次栅栏」上线前后都成立。
    task_id = make_task(
        env.project_a,
        task_type="auto_sync",
        status="processing",
        assigned_agent_id=env.agent_a,
        repository_id=repository_id,
    )

    def _fail_after_dispatch(*args, **kwargs):
        raise RuntimeError("simulated failure after weekly dispatch")

    monkeypatch.setattr(handlers, "clear_repository_sync_error", _fail_after_dispatch)
    response = report(
        env,
        "a",
        task_id,
        attempt=0,
        result_payload={
            "commits": [
                {
                    "commit_id": "abcdef123456",
                    "path": "config/a.xlsx",
                    "operation": "M",
                    "commit_time": now.isoformat(),
                }
            ]
        },
    )

    assert response.status_code == 500, response.get_json()
    db.session.expire_all()
    assert db.session.get(AgentTask, task_id).status == "processing"
    assert Commit.query.filter_by(repository_id=repository_id).count() == 0
    # 周版本同步任务也是同一事务里的派生数据，内部 commit 会把它留在库里。
    # 按本项目/本配置过滤，避免受同进程其它测试文件已提交的数据影响。
    assert BackgroundTask.query.filter_by(task_type="weekly_sync", commit_id=str(config_id)).count() == 0
    assert AgentTask.query.filter_by(task_type="weekly_sync", project_id=env.project_a).count() == 0
    assert AgentTask.query.filter_by(task_type="excel_diff", project_id=env.project_a).count() == 0
