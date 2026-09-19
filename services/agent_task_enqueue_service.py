#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 任务入队的**去重**：同一把 `request_key` 只许有一条未完成的任务。

## 为什么单独一个模块

这段逻辑原先不存在 —— 两个派发入口各自写了「先查有没有活跃的同参数任务，没有再插一条」：

* `services/agent_commit_diff_dispatch.py::_ensure_commit_diff_task`
* `services/agent_file_content_dispatch.py::_request_from_agent`

「查 → 插」之间没有约束，于是**两个并发请求会各自查到「没有」，各插一条**。实测可复现
（两个线程，各自一个 session，用屏障把窗口撑开）：

    线程返回 (task_id, 新建?): [(1, True), (2, True)]
    同一个 commit 上的 pending 任务: [(1, 'pending'), (2, 'pending')] -> 2 条

平台是 `app.run(threaded=True)`（见 `bootstrap/runtime_entry.py`）—— 并发是真的。
后果是同一份 diff / 同一份正文被两个 Agent 各取一遍、各写一遍缓存，任务面板上同一件事
出现两行；没有数据损坏，但白花的算力与一份对不上号的面板。

放在这个新模块而不是 `agent_management_handlers.py`：那个文件已经 1983 行，
`scripts/check_file_length.py --strict` 的硬上限是 2000。

## 为什么用锁，而不是给库加唯一索引

这是本次修复里唯一有争议的决定，写清楚理由：

* **部分唯一索引**（只约束 `pending`/`processing` 那些行）能真正在库层面拦住，但
  **MySQL 不支持部分索引**，而 `services/db_migration_service.py` 是要给 MySQL 也建索引的
  —— 那条约束会在 MySQL 上**静默不存在**，「有约束」的错觉比没有约束更坏。
* **全量唯一索引**（`(task_type, request_key)` 永久唯一）在 SQLite/MySQL 都成立，但它会让
  「失败后自动补派」永远派不出去（`_ensure_commit_diff_task` 的调用方正是靠再派一次恢复的），
  也会堵死「缓存被清了要重新取一次」。
* **多一个 `active_request_key` 列**（未完成时非空、完成时置空）+ 全量唯一索引，是能兼顾的
  写法，但它要求**每一条状态迁移路径都记得把它置空**（`agent_management_handlers.py` 的
  抢占、`agent_task_result_service.py` 的两处落库、租约回收，以及将来新加的路径）。
  漏掉一处 = 那把 key 永久派不出去，比重复派发严重得多。

用锁是这个仓库已有的做法（`task_worker_service.py` 的 `excel_task_enqueue_lock` 用它抑制
重复入队），它把保证落在**一个函数**里，不需要别处配合。**代价是它只在一个进程内成立**：
哪天平台改成多 worker（gunicorn 多进程、多副本），这把锁就不再有效。那时应当换成上面第三种
写法（`active_request_key` + 唯一索引），而不是继续用锁 —— 这一点写在这里，免得将来有人
以为它天然成立。

## 锁必须一直持有到 commit

「查到没有 → 插一条 → 提交」这三步要**整段**在锁里。只锁到 `flush()` 是不够的：另一个线程
的 `SELECT` 仍然可能发生在本次 commit 之前，照样查不到那一行。所以下面这个函数**自己 commit**，
而不是像 `enqueue_agent_task` 那样把提交留给调用方 —— 换过来之后那几个调用点本来
也都是紧接着就 commit（`_ensure_temp_cache_fetch_task` 的调用方还多写了一句
`if created: db.session.commit()`，现在成了空操作），没有行为差异。
"""
from __future__ import annotations

import threading

from services.agent_management_handlers import enqueue_agent_task

# 「未完成」：这条任务还占着那把 request_key，后来者应当复用它而不是再建一条。
# 与 `agent_commit_diff_dispatch._PENDING_STATUSES` 同一个口径。
AGENT_TASK_ACTIVE_STATUSES = frozenset({"pending", "processing"})

_ENQUEUE_LOCK = threading.Lock()


def enqueue_agent_task_once(
    *,
    find_existing,
    task_type,
    project_id,
    repository_id=None,
    source_task_id=None,
    priority=10,
    payload=None,
):
    """去重入队，返回 `(task, created)`。

    `find_existing` 是个**无参可调用**：返回已有的活跃任务，没有就返回 `None`。
    各任务类型的「同参数」判定不一样（commit_diff 看 `commit_record_id`，
    取数任务看 `cache_key` / `commit_id` / `file_path`），所以判定留给调用方，
    这里只负责**把「判定 + 插入 + 提交」变成不可分割的一段**。

    `payload` 里的 `request_key` 只用于日志与排查 —— 判重靠的是 `find_existing`，
    不是这个字段（它躺在 JSON 里，库管不到）。
    """
    from models import db  # 延迟导入：与其它服务一致，避免循环 import

    with _ENQUEUE_LOCK:
        existing = find_existing()
        if existing is not None:
            return existing, False

        task = enqueue_agent_task(
            task_type=task_type,
            project_id=project_id,
            repository_id=repository_id,
            source_task_id=source_task_id,
            priority=priority,
            payload=payload,
        )
        # 提交也在锁内：锁外的提交会让另一个线程的「查」跑在本次提交之前，
        # 去重就白做了（见模块 docstring 的「锁必须一直持有到 commit」）。
        db.session.commit()
        return task, True
