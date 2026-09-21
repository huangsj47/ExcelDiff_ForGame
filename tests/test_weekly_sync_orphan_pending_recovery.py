# -*- coding: utf-8 -*-
"""「周版本同步任务永久显示排队中」—— 两处缺陷叠加的回归保护。

## 线上症状

周版本配置页上的同步任务永远停在「排队中」：既不跑，也不结束。

## 根因（两处叠加，逐行核实过）

**缺陷 1：重启后恢复的 weekly_sync 任务必然失败，而且失败后库里那行仍留在 pending。**

* `load_pending_tasks()` 给 `weekly_excel_cache` / `weekly_ai_analysis` 都写了专属分支，
  **唯独漏了 `weekly_sync`** → 它落进最后的通用 `else`，载荷里只有
  `repository_id/commit_id/file_path`，**没有 `config_id`**；
* 处理器第一句就是 `task['config_id']`，而这一行在 `try:` **之外** → `KeyError`；
* 该异常被 worker 循环的 `NON_CRITICAL_WORKER_LOOP_ERRORS`（含 `KeyError`）吞掉，
  只打一行「后台任务处理异常」；而写状态的那句在 `try` 里面、执行不到
  → **库里那行从头到尾没被碰过，永远 pending**。

**缺陷 2：这行 pending 之后再也修不好。**

* `create_weekly_sync_task` 按 `status='pending'` 去重，命中就返回；而「重新入队」
  只发生在 `if _use_agent_dispatch():` 里面 —— 平台是 single 模式，所以每次都空转：
  内存队列会因重启丢失，数据库那行不会；
* 唯一能救它的调度器孤儿重置，整段被圈在 `if config.status == 'active':` 里；而版本
  窗口一结束，调度器先把配置置 `completed` 再 `continue`，此后每次运行都跳过它，
  重置与重建都不再发生。

## 这些测试变红意味着什么

* `test_a_payload_without_config_id_*`：畸形载荷又开始留下一行永久 pending（异常会
  再次被 worker 循环吞掉）。
* `test_load_pending_tasks_*`：重启恢复的 weekly_sync 载荷又丢了 config_id。
* `test_dedup_hit_*` / `test_the_ledger_*`：去重空转（任务永远不跑）或者反向的
  「无条件重入队」（同一条任务跑两遍）。
* `test_*_config_still_gets_its_orphan_*`：窗口结束后残留的 pending 又没人重置了。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import flask
import pytest

import services.task_worker_service as worker
import services.task_worker_weekly_handlers as handlers
from services.weekly_version_sync_status import WEEKLY_SYNC_COMPLETED
from utils.timezone_utils import BEIJING_TZ


# ---------------------------------------------------------------------------
#  公共桩
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_enqueue_ledger():
    """「已入队」账本是模块级集合：用例之间必须清干净，否则互相污染。"""
    handlers._enqueued_weekly_sync_task_ids.clear()
    yield
    handlers._enqueued_weekly_sync_task_ids.clear()


@pytest.fixture()
def bare_app(monkeypatch):
    """handler 只需要一个能 push context 的 app。"""
    app = flask.Flask("weekly_orphan_probe")
    monkeypatch.setattr(worker, "_app", app, raising=False)
    return app


def _run_worker_until(task, predicate, timeout=5.0):
    """把任务塞进内存队列、启动**真实的** worker 线程，等 predicate 成立或超时。"""
    with worker.background_task_queue.mutex:
        worker.background_task_queue.queue.clear()
        worker.background_task_queue.unfinished_tasks = 0
        worker.background_task_queue.all_tasks_done.notify_all()

    worker.background_task_running = True
    worker.background_task_queue.put(worker.TaskWrapper(1, 1, task))
    thread = threading.Thread(target=worker.background_task_worker, daemon=True)
    thread.start()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and not predicate():
            time.sleep(0.02)
    finally:
        worker.background_task_running = False
        thread.join(timeout=2)
        with worker.background_task_queue.mutex:
            worker.background_task_queue.queue.clear()


# ===========================================================================
#  一、畸形载荷（无 config_id）：必须把那一行标 failed，不再残留 pending
# ===========================================================================
def test_a_payload_without_config_id_is_marked_failed(monkeypatch, bare_app):
    """处理器第一句就取 config_id，取不到必须自己收口标 failed。

    修复前这一句是 `task['config_id']`，且在 `try:` 之外 → KeyError 被 worker 循环吞掉，
    写状态那句在 try 里执行不到 → 库里那行永久 pending。**这就是线上的症状。**
    """
    recorded = []
    sync_calls = []
    monkeypatch.setattr(
        worker, "update_task_status_with_retry",
        lambda task_id, status, *a, **k: recorded.append((task_id, status)),
    )
    monkeypatch.setattr(worker, "_process_weekly_version_sync",
                        lambda config_id: sync_calls.append(config_id))

    # 载荷形状 = 漏掉 weekly_sync 分支时通用 else 分支产出的那一种：没有 config_id
    worker._handle_weekly_sync_task({"type": "weekly_sync", "task_id": 7,
                                     "repository_id": None, "commit_id": "3"})

    assert recorded, "畸形载荷没有被标记失败 —— 这一行会永远留在 pending"
    assert recorded[-1] == (7, "failed"), f"应当把 task_id=7 标 failed，实际：{recorded}"
    assert not sync_calls, "载荷没有 config_id，却还是去跑同步了"


def test_the_failure_reason_says_what_is_missing(monkeypatch, bare_app):
    """原因要能回答「为什么这条任务失败了」—— 只有 `error_message` 一个文本字段。"""
    reasons = []
    monkeypatch.setattr(
        worker, "update_task_status_with_retry",
        lambda task_id, status, reason=None, *a, **k: reasons.append(reason),
    )

    worker._handle_weekly_sync_task({"type": "weekly_sync", "task_id": 8})

    assert reasons and reasons[-1], "标 failed 时没有写入原因"
    assert "config_id" in reasons[-1], f"原因里没说是缺了 config_id：{reasons[-1]}"
    assert "无法确定" in reasons[-1], f"原因没说清后果（无法确定同步哪个配置）：{reasons[-1]}"


def test_a_malformed_payload_no_longer_strands_the_row_through_the_worker_loop(monkeypatch, bare_app):
    """端到端：畸形载荷**从内存队列进 worker 线程**，那一行必须被标 failed。

    与上面两条的区别：这条走的是真实的 worker 循环 —— 修复前异常在
    `NON_CRITICAL_WORKER_LOOP_ERRORS` 里被吞成一行日志，`recorded` 会是空的。
    """
    recorded = []
    monkeypatch.setattr(
        worker, "update_task_status_with_retry",
        lambda task_id, status, *a, **k: recorded.append((task_id, status)),
    )

    _run_worker_until(
        {"type": "weekly_sync", "task_id": 4242},
        lambda: bool(recorded),
    )

    assert ("failed" in [status for _, status in recorded]), (
        f"畸形载荷经 worker 循环后仍没有被标 failed —— 那一行会永远 pending：{recorded}"
    )


def test_a_payload_without_task_id_does_not_crash(monkeypatch, bare_app):
    """没有 task_id 连「标谁」都无从谈起 —— 只许记日志，不许抛。"""
    worker._handle_weekly_sync_task({"type": "weekly_sync"})


def test_a_healthy_payload_still_runs_the_sync(monkeypatch, bare_app):
    """反向保险：补了畸形分支之后，正常载荷（有 config_id）必须照常跑完。"""
    recorded = []
    calls = []
    monkeypatch.setattr(
        worker, "update_task_status_with_retry",
        lambda task_id, status, *a, **k: recorded.append((task_id, status)),
    )
    monkeypatch.setattr(worker, "_process_weekly_version_sync",
                        lambda config_id: (calls.append(config_id), WEEKLY_SYNC_COMPLETED)[1])

    worker._handle_weekly_sync_task({"type": "weekly_sync", "config_id": 5, "task_id": 9})

    assert calls == [5], f"正常载荷没有同步 config_id=5：{calls}"
    assert ("completed" in [s for _, s in recorded]), f"正常载荷没有标 completed：{recorded}"


# ===========================================================================
#  二、load_pending_tasks：恢复出的 weekly_sync 载荷必须带 config_id
# ===========================================================================
class _OrderColumn:
    def asc(self):
        return self


class _PendingQuery:
    """按行实时的 status 回答 filter_by(status=...)，与既有用例同一形态。"""

    def __init__(self, rows):
        self._rows = rows
        self._status = None

    def filter_by(self, **kwargs):
        self._status = kwargs.get('status')
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return [row for row in self._rows if row.status == self._status]


def _weekly_sync_row(row_id, config_id, status='pending', age_seconds=0):
    return SimpleNamespace(
        id=row_id,
        status=status,
        started_at=None,
        task_type='weekly_sync',
        repository_id=None,
        commit_id=str(config_id),
        file_path=None,
        priority=3,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _install_pending_loader_stubs(monkeypatch, rows, enqueued):
    class _FakeTask:
        query = _PendingQuery(rows)
        priority = _OrderColumn()
        created_at = _OrderColumn()

    fake_session = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))
    monkeypatch.setattr(worker, "background_task_queue", SimpleNamespace(put=enqueued.append))
    monkeypatch.setattr(worker, "TaskWrapper", lambda priority, counter, data: data)
    monkeypatch.setattr(worker, "check_and_create_auto_sync_tasks", lambda: None)


def test_load_pending_tasks_builds_a_weekly_sync_payload_with_config_id(monkeypatch):
    """`config_id` 存在 `commit_id` 列里（是 `str(config_id)`），装载时要还原成 int。

    修复前 weekly_sync 落进通用 else 分支，载荷里根本没有 config_id —— 处理器随即
    KeyError，那一行永远 pending。
    """
    enqueued = []
    _install_pending_loader_stubs(monkeypatch, [_weekly_sync_row(11, 77)], enqueued)

    worker.load_pending_tasks()

    assert len(enqueued) == 1, f"应当恰好入队 1 条，实际：{enqueued}"
    payload = enqueued[0]
    assert payload['type'] == 'weekly_sync'
    assert payload['config_id'] == 77, f"载荷里没有（或不是 int 的）config_id：{payload}"
    assert payload['task_id'] == 11


def test_load_pending_tasks_keeps_an_unparseable_config_id_as_none_and_still_enqueues(monkeypatch):
    """commit_id 不是数字时 config_id 记 None，但**仍要入队**。

    不在这里抛：一抛就中断整次启动的待处理任务装载。交给处理器按「载荷畸形」标 failed。
    """
    enqueued = []
    bad_row = _weekly_sync_row(12, 0)
    bad_row.commit_id = 'not-an-int'
    _install_pending_loader_stubs(monkeypatch, [bad_row], enqueued)

    worker.load_pending_tasks()

    assert len(enqueued) == 1, f"解析不出 config_id 就不入队了：{enqueued}"
    assert enqueued[0]['config_id'] is None
    assert enqueued[0]['task_id'] == 12


def test_load_pending_tasks_registers_the_weekly_sync_task_in_the_ledger(monkeypatch):
    """恢复入队时也必须登记「已入队」账本。

    漏登记 → `create_weekly_sync_task` 会把这条**还在队列里**的任务当成「队列已丢」
    再补一份 → 同一条任务被执行两遍。
    """
    enqueued = []
    _install_pending_loader_stubs(monkeypatch, [_weekly_sync_row(13, 88)], enqueued)

    worker.load_pending_tasks()

    assert handlers.is_weekly_sync_task_enqueued(13), (
        "重新入队后没有登记账本 —— create_weekly_sync_task 会再补一份、跑两遍"
    )


# ===========================================================================
#  三、去重命中时的补入队（single 模式）
# ===========================================================================
class _DedupQuery:
    """`first()` 返回 holder 里那一行；新建的行会把自己写回 holder。

    这样「先新建、再调度一次」这一串才是真的走完了：第二次调用命中的是**第一次创建
    的那一行**（库里的 pending），而不是凭空又造一条。

    `filter` / `order_by` 是**照单全收的透传**：真实查询是
    `filter(task_type == …, commit_id == …, status.in_([...])).order_by(id.desc()).first()`，
    而这个桩不解释判据 —— 「status 判据到底选没选中该选的那一行」由真库用例
    （`tests/test_weekly_sync_dedup_blocks_starvation.py`）负责，这里的职责只有一个：
    命中之后**补不补入队 / 补几次**。
    """

    def __init__(self, holder):
        self._holder = holder

    def filter_by(self, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._holder[0]


class _AnyColumn:
    """查询判据里用到的模型列（`task_type == …` / `status.in_([...])`）的占位。

    桩不解释 SQL，所以 `==` 与 `in_()` 都返回真 —— 判据本身由真库用例
    （`tests/test_weekly_sync_dedup_blocks_starvation.py`）负责。
    """

    def __eq__(self, _other):
        return True

    def in_(self, _values):
        return True

    def desc(self):
        return self


def _install_create_stubs(monkeypatch, holder, enqueued):
    class _FakeTask:
        query = _DedupQuery(holder)
        task_type = _AnyColumn()
        commit_id = _AnyColumn()
        status = _AnyColumn()
        id = _AnyColumn()

        def __init__(self, **kwargs):
            self.id = 555
            for key, value in kwargs.items():
                setattr(self, key, value)
            holder[0] = self

    fake_session = SimpleNamespace(
        add=lambda _obj: None, flush=lambda: None, commit=lambda: None, rollback=lambda: None,
    )
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))
    monkeypatch.setattr(worker, "background_task_queue", SimpleNamespace(put=enqueued.append))
    monkeypatch.setattr(worker, "TaskWrapper", lambda priority, counter, data: data)
    monkeypatch.setattr(worker, "_use_agent_dispatch", lambda: False)


def test_dedup_hit_reenqueues_when_the_memory_queue_lost_the_task(monkeypatch):
    """库里是 pending、内存队列里没有（进程重启过）→ 必须补一次入队。

    修复前「重新入队」只在 `_use_agent_dispatch()` 分支里，single 模式下即使命中，
    也什么都不做 —— 任务永远不跑也永远不结束（线上「永久排队中」）。
    """
    enqueued = []
    holder = [SimpleNamespace(id=31, status='pending')]
    _install_create_stubs(monkeypatch, holder, enqueued)

    task_id = worker.create_weekly_sync_task(66)

    assert task_id == 31
    assert len(enqueued) == 1, f"去重命中但没有补入队（内存队列已丢）：{enqueued}"
    assert enqueued[0]['task_id'] == 31
    assert enqueued[0]['config_id'] == 66
    assert handlers.is_weekly_sync_task_enqueued(31), "补入队后没有登记账本"


def test_dedup_hit_does_not_enqueue_twice_when_it_is_already_in_the_queue(monkeypatch):
    """**不能无条件重入队**：还在队列里就必须维持现状，否则同一条任务跑两遍。"""
    enqueued = []
    holder = [SimpleNamespace(id=32, status='pending')]
    _install_create_stubs(monkeypatch, holder, enqueued)
    handlers.register_enqueued_weekly_sync_task(32)          # 模拟「已经在队列里」

    worker.create_weekly_sync_task(66)
    worker.create_weekly_sync_task(66)

    assert enqueued == [], f"同一条任务被重复入队了：{enqueued}"


def test_the_first_enqueue_registers_the_task_so_the_next_call_does_not_duplicate(monkeypatch):
    """新建任务这条路径也要登记 —— 否则下一次调度会再补一份。"""
    enqueued = []
    _install_create_stubs(monkeypatch, holder=[None], enqueued=enqueued)

    worker.create_weekly_sync_task(66)
    worker.create_weekly_sync_task(66)

    assert len(enqueued) == 1, f"新建后紧接着的调度又补了一份：{enqueued}"


def test_the_worker_loop_forgets_the_task_after_processing_it(monkeypatch, bare_app):
    """worker 处理完必须注销账本 —— 否则这条任务再也不会被补入队（又是永久排队中）。

    这条走真实 worker 线程：把注销写在 finally 里，中途抛异常也要生效。
    """
    recorded = []
    monkeypatch.setattr(
        worker, "update_task_status_with_retry",
        lambda task_id, status, *a, **k: recorded.append((task_id, status)),
    )
    monkeypatch.setattr(worker, "_process_weekly_version_sync",
                        lambda config_id: WEEKLY_SYNC_COMPLETED)
    handlers.register_enqueued_weekly_sync_task(4243)

    _run_worker_until(
        {"type": "weekly_sync", "config_id": 5, "task_id": 4243},
        lambda: not handlers.is_weekly_sync_task_enqueued(4243),
    )

    assert recorded, "任务压根没被处理"
    assert not handlers.is_weekly_sync_task_enqueued(4243), (
        "处理完之后任务还留在「已入队」账本里 —— 它再也不会被补入队"
    )


# ===========================================================================
#  四、孤儿 pending 的重置不该被「配置已 completed」挡住
# ===========================================================================
FROZEN_UTC = datetime(2026, 3, 10, 1, 0, 0, tzinfo=timezone.utc)
FROZEN_BEIJING = datetime(2026, 3, 10, 9, 0, 0, tzinfo=BEIJING_TZ)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_UTC if tz else FROZEN_UTC.replace(tzinfo=None)


class _ConfigQuery:
    def __init__(self, configs):
        self._configs = configs

    def filter_by(self, **_kwargs):
        return self

    def all(self):
        return list(self._configs)


class _TaskRowsQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter_by(self, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


def _run_scheduler(monkeypatch, config, stale_rows):
    """跑一次真实的 schedule_weekly_sync_tasks，只把「外部世界」换成桩。"""
    created = []
    app = flask.Flask("weekly_scheduler_probe")
    monkeypatch.setattr(worker, "_app", app, raising=False)
    monkeypatch.setattr(worker, "_WeeklyVersionConfig", SimpleNamespace(query=_ConfigQuery([config])))
    monkeypatch.setattr(worker, "_BackgroundTask", SimpleNamespace(query=_TaskRowsQuery(stale_rows)))
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=SimpleNamespace(
        commit=lambda: None, rollback=lambda: None,
    )))
    monkeypatch.setattr(worker, "datetime", _FrozenDatetime)
    monkeypatch.setattr(worker, "now_beijing", lambda: FROZEN_BEIJING)
    monkeypatch.setattr(worker, "create_weekly_sync_task", lambda config_id: created.append(config_id))

    worker.schedule_weekly_sync_tasks()
    return created


def _orphan_row(row_id=101, age_seconds=600):
    return SimpleNamespace(
        id=row_id,
        status='pending',
        error_message=None,
        created_at=FROZEN_UTC.replace(tzinfo=None) - timedelta(seconds=age_seconds),
    )


def test_a_completed_config_still_gets_its_orphan_pending_reset(monkeypatch):
    """版本窗口已结束（配置被置成 completed）时，残留的 pending **仍然**要被重置。

    修复前重置整段圈在 `if config.status == 'active':` 里，而窗口结束时调度器先置
    completed 再 continue —— 于是这个配置此后每次都被跳过，那行 pending 既不会被执行
    （内存队列早已随重启清空）也永远没人重置。
    """
    config = SimpleNamespace(id=7, name="已结束的版本", status='completed',
                             end_time=datetime(2026, 3, 9, 0, 0, 0))
    row = _orphan_row()

    created = _run_scheduler(monkeypatch, config, [row])

    assert row.status == 'failed', (
        "配置已 completed，孤儿 pending 没有被重置 —— 它会永远停在「排队中」"
    )
    assert row.error_message, "重置时没有写入原因"
    assert created == [], "配置已经结束了，不该再新建同步任务"


def test_an_active_config_still_gets_the_reset_and_still_creates_a_task(monkeypatch):
    """反向保险：活跃版本上原有的行为（重置 + 建任务）必须原样保留。"""
    config = SimpleNamespace(id=8, name="进行中的版本", status='active',
                             end_time=datetime(2026, 3, 11, 0, 0, 0))
    row = _orphan_row(row_id=102)

    created = _run_scheduler(monkeypatch, config, [row])

    assert row.status == 'failed', "活跃版本上的卡死任务没有被重置"
    assert created == [8], f"活跃版本应当照旧创建同步任务：{created}"


def test_a_fresh_pending_is_not_reset(monkeypatch):
    """别把「刚创建、正在排队」当成卡死 —— 否则每次调度都会误杀正常任务。"""
    config = SimpleNamespace(id=9, name="进行中的版本", status='active',
                             end_time=datetime(2026, 3, 11, 0, 0, 0))
    row = _orphan_row(row_id=103, age_seconds=60)

    _run_scheduler(monkeypatch, config, [row])

    assert row.status == 'pending', "刚创建 60 秒的 pending 被判成超时了"


# ===========================================================================
#  五、顺手一处：自动同步排队的「配表」判定不再手写扩展名
#
#  这一处**只能断源码**：它埋在 `_handle_auto_sync_task_inner`（几百行、要真 git 仓库
#  与一堆全局注入才能跑）里，为了一个扩展名清单去搭整套桩不值得。本仓库大量回归保护
#  就是这种形态，所以这里按同样的规矩来：**先剥注释再断言**。
# ===========================================================================
def _read_source(relative_path: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / relative_path).read_text(encoding="utf-8")


def _strip_py_comments(source: str) -> str:
    """按 tokenize 的判定去掉注释，而不是朴素截断 `#` 之后的内容。

    朴素截断会把字符串里的 `#` 也当成注释起点；而这里要断言的「不存在」恰恰是注释里
    原样引用过的写法（改动说明里就写着 `原先只写 ('.xlsx', '.xls')`）。
    """
    import io
    import tokenize

    lines = source.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        if start_row == end_row:
            line = lines[start_row - 1]
            lines[start_row - 1] = line[:start_col] + line[end_col:]
    return "\n".join(lines)


def _function_body(source: str, header: str) -> str:
    start = source.find(header)
    assert start >= 0, f"找不到 {header}"
    end = source.find("\ndef ", start + 1)
    return source[start:end] if end > 0 else source[start:]


def test_the_comment_stripping_is_not_a_no_op():
    """自检：不剥注释时，注释里引用的写法会把上面那条断言判成假红。"""
    sample = "x = 1  # 原先只写 ('.xlsx', '.xls')\n"
    assert "'.xlsx'" in sample
    assert "'.xlsx'" not in _strip_py_comments(sample)


def test_the_auto_sync_queue_uses_the_shared_excel_file_check():
    """自动同步排队时的「这是不是配表」必须问 `is_excel_file()`。

    原先手写 `('.xlsx', '.xls')`：`.xlsm/.xlsb/.csv` 的提交不会被排进 excel_diff 队列，
    而平台其他地方（本文件里标记 `is_excel` 的那处）是认它们的 —— 同一份「配表」在
    任务队列里少了一截，用户点进去只看得到「暂无数据」。
    """
    body = _function_body(
        _strip_py_comments(_read_source("services/task_worker_service.py")),
        "def _handle_auto_sync_task_inner(",
    )

    assert "is_excel_file(" in body, (
        "自动同步排队时没有走 is_excel_file() —— 扩展名清单又被手写了一遍"
    )
    for handwritten in ("'.xlsx'", '".xlsx"', "'.xls'", '".xls"'):
        assert handwritten not in body, (
            f"自动同步里又出现了手写的扩展名 {handwritten} —— 口径会再次与 "
            f"is_excel_file() 分叉"
        )
