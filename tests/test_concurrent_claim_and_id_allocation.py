# -*- coding: utf-8 -*-
"""并发下的两处 read-modify-write 竞态：Agent 任务重复派发、仓库 ID 撞主键。

## 缺陷一：Agent 抢占任务不是原子的

`services/agent_management_handlers.py::agent_claim_task` 原先写的是

    task = AgentTask.query.filter(status == "pending", ...).first()
    task.status = "processing"
    task.assigned_agent_id = agent.id
    db.session.commit()

两个 Agent 同时轮询时会**各自读到同一条** pending 任务（读的时候都是 pending），
然后各自把「自己的 assigned_agent_id」写回去 —— 后提交的那个覆盖先提交的，
但**两边都以为自己抢到了**，于是同一条 `auto_sync` / `temp_cache_fetch` 任务
被执行两次（重复写库、重复拉取缓存、重复派发下游任务）。

修复：把「读-改-写」换成带条件的原子 UPDATE —— 只有目标行**仍是 pending** 时
才更新得到，`rowcount == 1` 才算抢到；抢不到就换下一条继续试（而不是直接返回
「没有任务」，那会让 Agent 在争抢时空转一轮）。

## 缺陷二：仓库 ID 分配不是原子的

`services/repository_creation_handlers.py` 两个创建入口各写了一遍

    counter = GlobalRepositoryCounter.query.first()
    new_repository_id = counter.max_repository_id + 1
    counter.max_repository_id = new_repository_id

并发提交创建表单时两个请求读到同一个 N、都算出 N+1，第二个 INSERT 撞
`repository.id` 主键 → IntegrityError → 500。

修复：抽成 `allocate_repository_id()`，用 `UPDATE ... SET x = x + 1` 原子自增
再把结果读回来（MySQL 上是加锁读、SQLite 上实测也能读到最新已提交值；SQLite
不支持 `SELECT ... FOR UPDATE`，所以不能用行锁方案）。

## 关于本文件的测试手法

「两个请求真的同时到达」在单进程测试里没法直接构造。这里用**确定性插入**代替：
在「读」与「写」之间用**另一条数据库连接**提交一次竞争写入，模拟另一个请求
抢先落库。这正是原实现会栽的那个时间窗口 —— 原实现在这个窗口里会覆盖对方，
新实现的带条件 UPDATE 会返回 0 行并改抢下一条。
"""
from __future__ import annotations

import ast
import json
import os
import sys
import uuid

import pytest
from sqlalchemy import text

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import services.agent_management_handlers as agent_handlers  # noqa: E402
from app import app, create_tables, db  # noqa: E402
from models import (  # noqa: E402
    AgentNode,
    AgentProjectBinding,
    AgentTask,
    GlobalRepositoryCounter,
    Project,
)
from services.repository_creation_handlers import allocate_repository_id  # noqa: E402


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _register_agent(client, shared_secret, agent_code, project_code):
    response = client.post(
        "/api/agents/register",
        json={
            "agent_code": agent_code,
            "agent_name": f"{agent_code}-name",
            "project_codes": [project_code],
            "default_admin_username": "admin",
        },
        headers={"X-Agent-Secret": shared_secret},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    data = response.get_json() or {}
    assert data.get("success") is True
    return str(data.get("agent_token"))


# ---------------------------------------------------------------------------
# 缺陷二：仓库 ID 分配
# ---------------------------------------------------------------------------

class TestRepositoryIdAllocationIsAtomic:
    def test_sequential_allocation_yields_distinct_increasing_ids(self):
        with app.app_context():
            create_tables()
            before = GlobalRepositoryCounter.query.first()
            if before is None:
                db.session.add(GlobalRepositoryCounter(max_repository_id=0))
                db.session.commit()
                before = 0
            else:
                before = before.max_repository_id

            allocated = [allocate_repository_id() for _ in range(3)]
            db.session.commit()

            assert allocated == [before + 1, before + 2, before + 3]
            assert len(set(allocated)) == 3
            assert GlobalRepositoryCounter.query.first().max_repository_id == before + 3

    def test_concurrent_writer_cannot_be_given_the_same_id(self):
        """核心用例：模拟「另一个请求在本请求读完之后、写回之前抢走了 N+1」。

        原实现会把 Python 里早已算好的 stale_max + 1 写回去，于是两个请求拿到同一个
        号；原子自增读的是**库内当前值**，因此会跳过被占用的那一个。
        """
        with app.app_context():
            create_tables()
            counter = GlobalRepositoryCounter.query.first()
            if counter is None:
                db.session.add(GlobalRepositoryCounter(max_repository_id=0))
                db.session.commit()
                counter = GlobalRepositoryCounter.query.first()

            start = counter.max_repository_id
            # 本请求「已经读过」counter（stale 值就是 start）
            assert counter.max_repository_id == start

            # 另一个请求用**独立连接**抢先把 start+1 用掉并提交
            with db.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE global_repository_counter "
                        "SET max_repository_id = :value WHERE id = :row_id"
                    ),
                    {"value": start + 1, "row_id": counter.id},
                )

            allocated = allocate_repository_id()
            db.session.commit()

            assert allocated != start + 1, (
                f"分配到了 {start+1}，但那个号已被并发请求占用 —— "
                f"两个仓库会用同一个主键，第二个 INSERT 会 IntegrityError。"
            )
            assert allocated == start + 2, (
                f"期望跳过被占用的 {start+1} 而拿到 {start+2}，实际 {allocated}"
            )

    def test_helper_is_used_by_both_creation_entry_points(self):
        """两个创建入口都必须走同一个原子分配函数，不能各写一遍。

        这里查 **AST** 而不是查源码文本 —— 分配函数自己的 docstring 里就写着那两种
        反例写法（"不要这么写"），文本匹配会把自己的说明当成违规。
        """
        path = os.path.join(PROJECT_ROOT, "services", "repository_creation_handlers.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        call_count = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "allocate_repository_id"
        )
        assert call_count >= 2, "git / svn 两个创建入口都应调用 allocate_repository_id()"

        assigns_to_counter = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "max_repository_id"
                for t in node.targets
            )
        ]
        assert not assigns_to_counter, (
            "出现了 `counter.max_repository_id = ...` 这种赋值 —— 那是 read-modify-write，"
            "并发下两个请求会拿到同一个号，第二个 INSERT 撞主键。"
        )

        increments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "max_repository_id"
            and isinstance(node.right, ast.Constant)
            and node.right.value == 1
        ]
        assert increments, (
            "找不到 `max_repository_id + 1` 这个原子自增表达式。"
        )
        for node in increments:
            # 自增必须发生在 update() 的赋值里（由数据库执行），
            # 不能在 Python 里算好再写回。
            chain = []
            cursor = node
            while cursor in parents:
                cursor = parents[cursor]
                chain.append(cursor)
            assert any(
                isinstance(anc, ast.Call)
                and isinstance(anc.func, ast.Attribute)
                and anc.func.attr == "update"
                for anc in chain
            ), (
                "`max_repository_id + 1` 没有出现在 update() 里 —— "
                "说明它是在 Python 里算的，而不是交给数据库原子自增。"
            )


# ---------------------------------------------------------------------------
# 缺陷一：Agent 任务抢占
# ---------------------------------------------------------------------------

class _SessionProxy:
    """在第一次 `db.session.query(...)` 之前插入一次「别人抢先提交」。"""

    def __init__(self, real_session, owner):
        self._real_session = real_session
        self._owner = owner

    def query(self, *args, **kwargs):
        if not self._owner.fired:
            self._owner.fired = True
            self._owner.on_query()
        return self._real_session.query(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real_session, name)


class _DbProxy:
    def __init__(self, real_db, on_query):
        self._real_db = real_db
        self.on_query = on_query
        self.fired = False

    @property
    def session(self):
        return _SessionProxy(self._real_db.session, self)

    def __getattr__(self, name):
        return getattr(self._real_db, name)


class TestAgentTaskClaimIsAtomic:
    def test_stolen_task_is_not_reported_as_claimed(self, monkeypatch):
        """核心用例：SELECT 与 UPDATE 之间被别人抢走时，不能再把它当成自己抢到的。

        原实现会在这个窗口里把 stale 的对象改成 processing 并提交，覆盖对方的
        assigned_agent_id，然后**把这条任务返回给自己** —— 两个 Agent 同时执行同一条。
        """
        with app.app_context():
            shared_secret = _uid("secret")
            agent_code = _uid("agent")
            project_code = _uid("P")
            monkeypatch.setenv("AGENT_SHARED_SECRET", shared_secret)
            create_tables()

            client = app.test_client()
            agent_token = _register_agent(client, shared_secret, agent_code, project_code)
            # 第二个 Agent：扮演「抢先一步把任务抢走」的那个。
            # 不绑项目（同一项目代号会被注册接口判为占用），只需要一个合法的
            # agent_nodes.id 来满足 agent_tasks.assigned_agent_id 的外键。
            thief_code = _uid("thief")
            _register_agent(client, shared_secret, thief_code, None)
            thief_node = AgentNode.query.filter_by(agent_code=thief_code).first()
            assert thief_node is not None
            thief_agent_id = thief_node.id

            project = Project.query.filter_by(code=project_code).first()
            assert project is not None

            first = AgentTask(
                task_type="weekly_sync", project_id=project.id, priority=1,
                payload=json.dumps({"seq": 0}), status="pending",
            )
            second = AgentTask(
                task_type="weekly_sync", project_id=project.id, priority=2,
                payload=json.dumps({"seq": 1}), status="pending",
            )
            db.session.add_all([first, second])
            db.session.commit()
            first_id, second_id = first.id, second.id

            def _steal_first():
                """模拟另一个 Agent 抢先提交：用独立连接把 first 置为它持有。"""
                with db.engine.begin() as conn:
                    conn.execute(
                        text(
                            "UPDATE agent_tasks SET status = 'processing', "
                            "assigned_agent_id = :thief WHERE id = :task_id"
                        ),
                        {"task_id": first_id, "thief": thief_agent_id},
                    )

            db_proxy = _DbProxy(db, _steal_first)
            real_get_runtime_models = agent_handlers.get_runtime_models

            def _patched_get_runtime_models(*names):
                values = list(real_get_runtime_models(*names))
                if "db" in names:
                    values[names.index("db")] = db_proxy
                return tuple(values)

            monkeypatch.setattr(agent_handlers, "get_runtime_models", _patched_get_runtime_models)

            response = client.post(
                "/api/agents/tasks/claim",
                json={"agent_code": agent_code, "agent_token": agent_token, "lease_seconds": 120},
                headers={"X-Agent-Secret": shared_secret},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            claimed = (response.get_json() or {}).get("task") or {}

            assert claimed, (
                "被抢走第一条之后应当改抢第二条，而不是返回「没有任务」。"
            )
            assert claimed.get("id") != first_id, (
                f"把已被别的 Agent 抢走的任务 {first_id} 当成自己抢到的返回了 —— "
                f"两个 Agent 会同时执行同一条任务。"
            )
            assert claimed.get("id") == second_id

            db.session.expire_all()
            assert db.session.get(AgentTask, first_id).status == "processing"

    def test_no_pending_task_returns_empty(self):
        with app.app_context():
            shared_secret = _uid("secret")
            agent_code = _uid("agent")
            project_code = _uid("P")
            os.environ["AGENT_SHARED_SECRET"] = shared_secret
            create_tables()

            client = app.test_client()
            agent_token = _register_agent(client, shared_secret, agent_code, project_code)

            response = client.post(
                "/api/agents/tasks/claim",
                json={"agent_code": agent_code, "agent_token": agent_token},
                headers={"X-Agent-Secret": shared_secret},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            data = response.get_json() or {}
            assert data.get("success") is True
            assert data.get("task") is None

    def test_claim_marks_only_the_claimed_task(self):
        """领取后只有被领走的那条变 processing，其余仍 pending。"""
        with app.app_context():
            shared_secret = _uid("secret")
            agent_code = _uid("agent")
            project_code = _uid("P")
            os.environ["AGENT_SHARED_SECRET"] = shared_secret
            create_tables()

            client = app.test_client()
            agent_token = _register_agent(client, shared_secret, agent_code, project_code)
            project = Project.query.filter_by(code=project_code).first()
            binding = AgentProjectBinding.query.filter_by(project_id=project.id).first()
            assert binding is not None

            tasks = []
            for index in range(3):
                task = AgentTask(
                    task_type="weekly_sync", project_id=project.id, priority=10 + index,
                    payload=json.dumps({"seq": index}), status="pending",
                )
                db.session.add(task)
                tasks.append(task)
            db.session.commit()
            task_ids = [t.id for t in tasks]

            response = client.post(
                "/api/agents/tasks/claim",
                json={"agent_code": agent_code, "agent_token": agent_token, "lease_seconds": 120},
                headers={"X-Agent-Secret": shared_secret},
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            claimed_id = ((response.get_json() or {}).get("task") or {}).get("id")
            assert claimed_id in task_ids

            db.session.expire_all()
            statuses = {
                tid: db.session.get(AgentTask, tid).status for tid in task_ids
            }
            processing = [tid for tid, status in statuses.items() if status == "processing"]
            assert processing == [claimed_id], (
                f"应当只有 {claimed_id} 变成 processing，实际 {statuses}"
            )
            owned = db.session.get(AgentTask, claimed_id)
            assert owned.assigned_agent_id == binding.agent_id
            assert owned.lease_expires_at is not None
            assert owned.started_at is not None
