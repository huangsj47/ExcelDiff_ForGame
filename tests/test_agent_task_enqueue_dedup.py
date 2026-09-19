# -*- coding: utf-8 -*-
"""Agent 任务入队去重：同一把 `request_key` 同时只许有一条未完成的任务。

## 这一组盯的是什么

派发入口原来是「先查有没有活跃的同参数任务，没有再插一条」—— **查与查之间没有约束**，
两个并发请求会各自查到「没有」，各插一条。这不是推测，实测复现过：

    线程返回 (task_id, 新建?): [(1, True), (2, True)]
    同一个 commit 上的 pending 任务: [(1, 'pending'), (2, 'pending')] -> 2 条

平台是 `app.run(threaded=True)`（`bootstrap/runtime_entry.py`），并发是真的。
后果是同一份 diff / 同一份正文被两个 Agent 各取一遍、各写一遍缓存，
任务面板上同一件事出现两行。

去重的实现与「为什么用锁而不是唯一索引」写在
`services/agent_task_enqueue_service.py` 的模块 docstring 里。

## 怎么让这条用例**不是恒真**

直接开两个线程去调，多半会自然错开（第一个连提交都做完了第二个才开始），
于是旧的「查 → 插」写法**照样能绿**。所以这里把「查」这一步放慢
（`_active_commit_diff_task` 睡一会儿）：窗口被撑开之后，「判定与插入是不是
不可分割的一段」才真的被考到 —— 去掉那个锁，这条会红。
"""
from __future__ import annotations

import ast
import threading
import time
import uuid

import pytest

import services.agent_commit_diff_dispatch as dispatch_module
from app import app as flask_app
from app import create_tables, db
from models import Commit, Project, Repository
from models.agent import AgentTask
from services import agent_task_enqueue_service as enqueue_service


def _seed_commit(suffix: str):
    """建一个项目 + 仓库 + 提交，返回 `(project_id, repository_id, commit_id)`。

    `project.code` 有唯一约束，而**测试库是会话级共用的**（没有逐用例重置）——
    所以每建一次都得换一个 code，否则同一个用例被跑第二遍（或与别的用例撞上）就是
    `UNIQUE constraint failed: project.code`。
    """
    unique = uuid.uuid4().hex[:10]
    tag = f"{suffix}-{unique}"
    project = Project(code=f"dedup-{tag}", name=f"dedup-{tag}")
    db.session.add(project)
    db.session.flush()
    repository = Repository(
        project_id=project.id,
        name=f"dedup-repo-{tag}",
        type="git",
        url="https://example.com/test.git",
        branch="main",
    )
    db.session.add(repository)
    db.session.flush()
    commit = Commit(
        repository_id=repository.id,
        commit_id=unique.ljust(40, "0")[:40],
        path=f"config/{tag}.xlsx",
        operation="M",
    )
    db.session.add(commit)
    db.session.commit()
    return project.id, repository.id, commit.id


@pytest.fixture
def commit_row():
    with flask_app.app_context():
        create_tables()
        project_id, repository_id, commit_id = _seed_commit("dedupA")
        yield project_id, repository_id, commit_id
        AgentTask.query.filter_by(repository_id=repository_id).delete()
        db.session.commit()


def _pending_commit_diff_tasks(repository_id: int):
    return (
        AgentTask.query.filter_by(task_type="commit_diff", repository_id=repository_id)
        .order_by(AgentTask.id)
        .all()
    )


class TestConcurrentDispatchCreatesOneTask:
    def test_two_concurrent_requests_share_one_task(self, commit_row, monkeypatch):
        """两个线程同时派发同一个 commit 的 diff —— 只许建出一条任务。"""
        project_id, repository_id, commit_id = commit_row

        # 把「查」这一步放慢，撑开旧写法里那条缝（见模块 docstring）。
        real_finder = dispatch_module._active_commit_diff_task

        def slow_finder(*args, **kwargs):
            time.sleep(0.3)
            return real_finder(*args, **kwargs)

        monkeypatch.setattr(dispatch_module, "_active_commit_diff_task", slow_finder)

        results = []
        errors = []
        start = threading.Barrier(2)

        def worker():
            try:
                with flask_app.app_context():
                    start.wait(timeout=10)
                    commit = db.session.get(Commit, commit_id)
                    task, created = dispatch_module._ensure_commit_diff_task(
                        commit, project_id, repository_id, priority=3
                    )
                    results.append((task.id, created))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert errors == [], f"并发派发抛异常了：{errors}"
        assert len(results) == 2, f"线程没跑完：{results}"

        task_ids = sorted({task_id for task_id, _created in results})
        assert len(task_ids) == 1, (
            f"两个并发请求各建了一条任务：{results} —— 去重没生效"
        )
        # 而且必须是「一个新建、一个复用」。
        assert sorted(created for _task_id, created in results) == [False, True], results

        with flask_app.app_context():
            pending = [t for t in _pending_commit_diff_tasks(repository_id) if t.status == "pending"]
            assert len(pending) == 1, (
                f"同一个 commit 上有 {len(pending)} 条 pending 任务："
                f"{[(t.id, t.status) for t in pending]}"
            )

    def test_a_finished_task_does_not_block_a_new_one(self, commit_row):
        """跑完的任务不该继续占着那把 key。

        这是**必须保留**的旧行为：失败后要能自动补派（`_ensure_commit_diff_task` 的调用方
        正是靠再派一次恢复的），缓存被清了也要能重新取一次。如果哪天改成
        「(task_type, request_key) 永久唯一」，这条会红。
        """
        project_id, repository_id, commit_id = commit_row

        with flask_app.app_context():
            commit = db.session.get(Commit, commit_id)
            first, created_first = dispatch_module._ensure_commit_diff_task(
                commit, project_id, repository_id, priority=3
            )
            assert created_first is True

            first.status = "completed"
            db.session.commit()

            second, created_second = dispatch_module._ensure_commit_diff_task(
                commit, project_id, repository_id, priority=3
            )

            assert created_second is True, "已完成的任务把新的派发挡住了"
            assert second.id != first.id


class TestTheTempCacheFetchIsDedupedToo:
    def test_two_requests_for_the_same_cache_key_share_one_task(self, commit_row):
        project_id, repository_id, _commit_id = commit_row

        with flask_app.app_context():
            first, created_first = dispatch_module._ensure_temp_cache_fetch_task(
                project_id, repository_id, "cache-key-dedup", "hash-1"
            )
            second, created_second = dispatch_module._ensure_temp_cache_fetch_task(
                project_id, repository_id, "cache-key-dedup", "hash-1"
            )

            assert created_first is True
            assert created_second is False, "同一把 cache_key 建出了第二条任务"
            assert first.id == second.id

            AgentTask.query.filter_by(
                task_type="temp_cache_fetch", repository_id=repository_id
            ).delete()
            db.session.commit()


class TestTheLocalCacheFetchDispatchIsDedupedToo:
    """`_dispatch_agent_local_cache_fetch` 也走同一条去重。

    这条路径原来是**裸的** `enqueue_agent_task`（只 `add`，不去重），于是同一把
    `cache_key` 上能堆出多条取数任务：缓存页轮询一次派一条、两个标签页各派一条、
    同一个 commit 被重新 diff 后再派一条。每一份都由 Agent 取一遍、传一遍，
    白花算力，任务面板上同一件事出现两行。
    """

    def test_two_dispatches_for_the_same_cache_key_share_one_task(self, commit_row):
        from services import agent_management_handlers as handlers

        project_id, repository_id, _commit_id = commit_row

        with flask_app.app_context():
            first_id = handlers._dispatch_agent_local_cache_fetch(
                db=db, cache_key="local-cache-dedup", expected_hash="h1",
                project_id=project_id, repository_id=repository_id,
            )
            db.session.commit()
            second_id = handlers._dispatch_agent_local_cache_fetch(
                db=db, cache_key="local-cache-dedup", expected_hash="h1",
                project_id=project_id, repository_id=repository_id,
            )
            db.session.commit()

            assert first_id is not None
            assert second_id == first_id, "同一把 cache_key 又建了一条取数任务"
            assert (
                AgentTask.query.filter_by(
                    task_type="temp_cache_fetch", project_id=project_id
                ).count()
                == 1
            )

    def test_it_shares_the_finder_with_the_commit_diff_dispatcher(self, commit_row):
        """两条派发路径的判重口径必须是同一份。

        `agent_commit_diff_dispatch._ensure_temp_cache_fetch_task` 先建了一条，
        随后 `_dispatch_agent_local_cache_fetch` 拿同一把 key 来 —— 必须复用那一条。
        各写一套判重就会出现「一边认为还在跑、另一边认为已经结束」。
        """
        from services import agent_management_handlers as handlers

        project_id, repository_id, _commit_id = commit_row

        with flask_app.app_context():
            created, was_created = dispatch_module._ensure_temp_cache_fetch_task(
                project_id, repository_id, "shared-finder-key", "h1"
            )
            assert was_created is True

            reused_id = handlers._dispatch_agent_local_cache_fetch(
                db=db, cache_key="shared-finder-key", expected_hash="h1",
                project_id=project_id, repository_id=repository_id,
            )

            assert reused_id == created.id, "两条派发路径各建了一条任务"
            assert (
                AgentTask.query.filter_by(
                    task_type="temp_cache_fetch", project_id=project_id
                ).count()
                == 1
            )

    def test_it_does_not_commit_the_callers_transaction(self, commit_row, monkeypatch):
        """**回归闸门**：这个函数不许提交调用方的事务。

        换成去重入队时，最顺手的写法是用 `enqueue_agent_task_once` 的默认行为 ——
        它自己 commit（因为锁必须持有到提交，见那个模块的 docstring）。但本函数的
        调用方之一 `agent_task_result_service` 正处在**一段整体回滚**的事务中间：
        条件 UPDATE 抢占回传处理权 → 结果摘要 → 缓存 → 源任务状态，中途任何一步
        抛错都靠一条 `db.session.rollback()` 把整段撤掉、回 500 让 Agent 重报。
        在那里硬提交，「任务已 completed」就先落了库，Agent 重报时被判成 duplicate，
        **真正的结果被静默丢掉** —— 比多派一条幂等的取数任务重得多。

        所以这里直接盯「有没有人调 commit」，而不是事后看数据库：后者要另开连接才
        分得出提交与否，而这条判据本身就是「调用方的事务有没有被劈开」。
        """
        from services import agent_management_handlers as handlers

        project_id, repository_id, _commit_id = commit_row

        with flask_app.app_context():
            commits = []
            real_commit = db.session.commit
            monkeypatch.setattr(
                db.session, "commit", lambda: (commits.append(1), real_commit())[1]
            )

            handlers._dispatch_agent_local_cache_fetch(
                db=db, cache_key="must-not-commit", expected_hash="h1",
                project_id=project_id, repository_id=repository_id,
            )

            assert commits == [], (
                "取数派发提交了调用方的事务 —— 回传结果那段整体回滚被劈开了"
            )
            db.session.rollback()

    def test_a_blank_cache_key_dispatches_nothing(self, commit_row):
        """没有 cache_key 就取不到东西，别建一条注定被 Agent 拒掉的任务。"""
        from services import agent_management_handlers as handlers

        project_id, repository_id, _commit_id = commit_row

        with flask_app.app_context():
            before = AgentTask.query.filter_by(project_id=project_id).count()
            for blank in ("", "   ", None):
                assert handlers._dispatch_agent_local_cache_fetch(
                    db=db, cache_key=blank, expected_hash="h1",
                    project_id=project_id, repository_id=repository_id,
                ) is None
            db.session.commit()
            assert AgentTask.query.filter_by(project_id=project_id).count() == before


class TestTheLockCoversTheWholeStep:
    """结构断言：判定、插入、提交必须**整段**在锁里。

    只锁到 `flush()` 是不够的 —— 另一个线程的 SELECT 仍然可能跑在本次 commit 之前，
    照样查不到那一行，去重就白做了。这件事没法从外面观察，只能看代码形状。
    """

    def _locked_block(self) -> ast.With:
        source = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "services" / "agent_task_enqueue_service.py"
        ).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.With):
                names = ast.unparse(node.items[0].context_expr)
                if "_ENQUEUE_LOCK" in names:
                    return node
        raise AssertionError("enqueue_agent_task_once 里找不到 `with _ENQUEUE_LOCK:`")

    def test_the_check_is_inside_the_lock(self):
        block = ast.unparse(self._locked_block())

        assert "find_existing" in block, (
            "判定跑在锁外面 —— 两个线程会同时读到「没有活跃任务」"
        )

    def test_the_commit_is_inside_the_lock(self):
        block = ast.unparse(self._locked_block())

        assert "commit" in block, (
            "提交跑在锁外面 —— 另一个线程的判定仍然可能跑在本次提交之前"
        )

    def test_the_shared_status_set_is_the_one_the_dispatchers_use(self):
        """「未完成」的口径只能有一份。

        派发侧按它判重（`_active_commit_diff_task` 等），去重服务也按它判重；
        两处口径一旦漂移，就会出现「一边认为还在跑、另一边认为已经结束」。
        """
        assert enqueue_service.AGENT_TASK_ACTIVE_STATUSES == frozenset({"pending", "processing"})
        assert dispatch_module._PENDING_STATUSES == set(enqueue_service.AGENT_TASK_ACTIVE_STATUSES)
