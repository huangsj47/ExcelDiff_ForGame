# -*- coding: utf-8 -*-
"""后台 worker 的每个任务分支都必须在 Flask app context 内执行。

## 为什么需要这个测试

`background_task_worker()` 是通过 `threading.Thread(target=background_task_worker)`
启动的。**线程不继承主线程的 app context**（bootstrap 里那个
`with app.app_context(): start_background_task_worker()` 只包住了「启动线程」
这一瞬间），所以 `db.session` / `Model.query` 在 worker 里默认是不可用的
—— 会抛 `RuntimeError: Working outside of application context`。

各任务分支的处理方式**不一致**，于是出现了两处静默失败（实测复现）：

* `_handle_excel_diff_task` / `_handle_auto_sync_task_inner` /
  `_handle_weekly_ai_analysis_task` 自己开了 `with _app.app_context():` —— 正确。
* `cleanup_cache` 与 `regenerate_cache` 两个分支**没有**，而它们调用的
  `cleanup_old_cache` / `cleanup_expired_analysis_runs` /
  `regenerate_repository_cache` 用的都是模块级 `Model.query`。

后果（修复前实跑）：
    worker thread has_app_context: False
    cleanup_old_cache 返回: 0                      ← RuntimeError 被吞成 0
    cleanup_expired_analysis_runs 抛出: RuntimeError ...
    regenerate_repository_cache 返回: None
    → worker 打印: ✅ 缓存重新生成完成，已添加 None 个任务到队列

也就是说**每天 04:00 的缓存清理从未真正清理过任何数据**，而
`regenerate_cache` 失败时还会打印一句带 ✅ 的假成功日志。

## 本文件怎么测

不检查源码文本 —— 那种断言改个变量名就碎、却挡不住真实回归。
这里真的把任务塞进 `background_task_worker` 的队列、真的跑那个线程，
然后在**生产代码自己决定**要调用清理函数的那一刻，记录当时的
`flask.has_app_context()`。这正是出问题的那个瞬间。
"""
import queue
import threading
import time
from types import SimpleNamespace

import flask
import pytest

import services.task_worker_service as worker


class _CtxRecorder:
    """在被调用的那一刻记录 has_app_context()，并返回一个「成功」值。"""

    def __init__(self):
        self.contexts = []

    def __call__(self, *args, **kwargs):
        self.contexts.append(flask.has_app_context())
        return 0


def _run_worker_until(task, predicate, timeout=5.0):
    """把 task 塞进队列并启动真实的 worker 线程，等 predicate 成立或超时。"""
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
        # 清掉本测试塞进去的任务，避免污染后续测试
        with worker.background_task_queue.mutex:
            worker.background_task_queue.queue.clear()


@pytest.fixture()
def fake_app(monkeypatch):
    """把 worker 的 _app 换成任意 Flask app —— 分支只需要能 push context。"""
    app = flask.Flask("worker_ctx_probe")
    monkeypatch.setattr(worker, "_app", app, raising=False)
    return app


def test_cleanup_cache_branch_runs_inside_app_context(monkeypatch, fake_app):
    """cleanup_cache 分支调用的两个清理函数必须能看到 app context。"""
    old_cache_spy = _CtxRecorder()
    ai_spy = _CtxRecorder()

    monkeypatch.setattr(
        worker, "_excel_cache_service",
        SimpleNamespace(cleanup_old_cache=old_cache_spy),
    )
    monkeypatch.setattr(worker, "cleanup_expired_analysis_runs", ai_spy)

    _run_worker_until(
        {"type": "cleanup_cache", "days": 30},
        lambda: len(old_cache_spy.contexts) >= 1 and len(ai_spy.contexts) >= 1,
    )

    assert old_cache_spy.contexts, (
        'cleanup_cache 分支没有调用 cleanup_old_cache —— 任务没被处理'
    )
    assert old_cache_spy.contexts[0] is True, (
        'cleanup_old_cache 是在**没有 app context** 的情况下被调用的。\n'
        '它内部用模块级 DiffCache.query，会抛 RuntimeError 并被吞成 return 0 —— '
        '表现为「每天清理了 0 条」，与「本来就没东西可清」无法区分，'
        '于是缓存表只增不减而无人察觉。'
    )
    assert ai_spy.contexts and ai_spy.contexts[0] is True, (
        'cleanup_expired_analysis_runs 是在没有 app context 的情况下被调用的'
    )


def test_regenerate_cache_branch_runs_inside_app_context(monkeypatch, fake_app):
    """regenerate_cache 分支必须带 app context，否则会打印假的 ✅ 成功日志。"""
    contexts = []

    def fake_regenerate(repository_id):
        contexts.append(flask.has_app_context())
        return 7

    monkeypatch.setattr(worker, "regenerate_repository_cache", fake_regenerate)

    _run_worker_until(
        {"type": "regenerate_cache", "repository_id": 1},
        lambda: bool(contexts),
    )

    assert contexts, 'regenerate_cache 分支没有调用 regenerate_repository_cache'
    assert contexts[0] is True, (
        'regenerate_repository_cache 是在没有 app context 的情况下被调用的。\n'
        '它内部用 _db.session.get()，会抛 RuntimeError 并被自己的 except 吞掉、'
        '且不 return → 返回 None，调用方随即打印'
        '「✅ 缓存重新生成完成，已添加 None 个任务到队列」—— 失败被伪装成成功。'
    )


def test_regenerate_cache_failure_is_not_logged_as_success(monkeypatch, fake_app):
    """regenerate_repository_cache 返回 None 时不得打印 ✅ 成功日志。

    断言直接挂在 log_print 上而不是 capsys：日志器在 import 时就持有了原始
    stdout 的引用，capsys 抓不到它写入的内容（消息其实打出来了，只是不在 capsys 里）。
    监听 log_print 才是确定的。
    """
    messages = []
    monkeypatch.setattr(worker, "log_print", lambda *a, **k: messages.append(str(a[0]) if a else ""))
    monkeypatch.setattr(worker, "regenerate_repository_cache", lambda repo_id: None)

    _run_worker_until(
        {"type": "regenerate_cache", "repository_id": 42},
        lambda: any("重新生成缓存" in m for m in messages),
    )

    joined = "\n".join(messages)
    assert "已添加 None 个任务" not in joined, (
        'regenerate_cache 失败（返回 None）时仍然打印了「已添加 None 个任务到队列」。\n'
        '那句日志带 ✅，会让巡检以为缓存重建成功了。\n'
        f'实际日志：{joined}'
    )
    assert any("❌" in m for m in messages), (
        f'regenerate_cache 失败时应当有明确的失败日志，实际日志：{joined}'
    )


def test_regenerate_cache_success_still_reports_count(monkeypatch, fake_app):
    """反向保险：成功时仍要打出真实的条数（不要把成功也变成失败提示）。"""
    messages = []
    monkeypatch.setattr(worker, "log_print", lambda *a, **k: messages.append(str(a[0]) if a else ""))
    monkeypatch.setattr(worker, "regenerate_repository_cache", lambda repo_id: 7)

    _run_worker_until(
        {"type": "regenerate_cache", "repository_id": 7},
        lambda: any("已添加 7 个任务" in m for m in messages),
    )

    joined = "\n".join(messages)
    assert "已添加 7 个任务" in joined, f'成功路径应报出真实条数，实际日志：{joined}'
    assert "❌" not in joined, f'成功路径不应出现失败提示，实际日志：{joined}'


def test_excel_diff_task_with_missing_commit_is_marked_failed(monkeypatch, fake_app):
    """excel_diff 任务在「什么都没做」时必须标 failed，而不是无条件 completed。

    修复前 _handle_excel_diff_task 完全忽略 process_excel_diff_background 的返回值，
    无条件 update_task_status_with_retry(..., 'completed')，于是「仓库已删/提交查不到」
    与「真的生成成功」在任务表里完全同形 —— 管理员无法区分「已完成」和「根本没做」。
    """
    from services.excel_diff_cache_service import (
        BG_STATUS_COMMIT_MISSING,
        BG_STATUS_COMPLETED,
        BG_STATUS_REPOSITORY_MISSING,
    )

    recorded = []
    monkeypatch.setattr(worker, "update_task_status_with_retry",
                        lambda task_id, status, *a, **k: recorded.append((task_id, status)))

    def _run(status):
        recorded.clear()
        monkeypatch.setattr(
            worker, "_excel_cache_service",
            SimpleNamespace(process_excel_diff_background=lambda *a, **k: status),
        )
        worker._handle_excel_diff_task(
            {"repository_id": 1, "commit_id": "a" * 40, "file_path": "a.xlsx", "task_id": 9},
            1,
        )
        return [s for _, s in recorded]

    assert "failed" in _run(BG_STATUS_COMMIT_MISSING), (
        '提交不存在时任务被标成了 completed —— 管理员会以为 diff 已生成'
    )
    assert "failed" in _run(BG_STATUS_REPOSITORY_MISSING), (
        '仓库不存在时任务被标成了 completed'
    )
    assert "completed" in _run(BG_STATUS_COMPLETED), (
        '真正生成成功时任务应当标 completed'
    )
