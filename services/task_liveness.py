#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「这条后台任务的执行者还在不在」—— 平台里**唯一**一处判据。

## 为什么要有这一层

平台有**两套**租约（`models/task.py` 上那条注释写了为什么两边都要有），而「这条任务还在
跑吗」这个问题在好几处被问过，每一处当初都拿**墙钟**当代理指标，于是同一个事实被写成了
好几个互不知情的 30 分钟：

* `services/ai/weekly_sync_gate.py`：跑了 30 分钟就当成卡死 → 放行 AI 分析，于是它在一份
  **写了一半的缓存**上出结论（变更清单静默缺文件 —— 那正是那道闸门存在的理由）；
* `services/weekly_version_files_api_helpers.py`：跑了 30 分钟就把页面上那条同步置
  `failed`，而它可能正在往缓存里写、正是闸门在等的那一条。

墙钟分不出「慢」和「死」，而这两件事的正确动作相反（继续等 / 放行）。租约分得出：
**真的在跑的任务由执行者不断续租，所以合法运行永远不会撞上租约到期** —— 到期只剩一个
含义，执行者（进程）死了。所以「跑了多久」只在**没有活执行者**时才该被拿出来用。

## 两套租约都要看

* `BackgroundTask.lease_expires_at` —— 本进程 worker 跑的任务：认领时起租
  （`task_worker_queue_service.stamp_task_lease`），运行期每 10 分钟 CAS 续租
  （`renew_inflight_task_leases`）。
* `AgentTask.lease_expires_at` —— **agent 派发模式**下真正在跑的那条。那个模式下平台
  **只派发、不执行**：`create_weekly_sync_task` 不进本机队列，那条 BackgroundTask 从头到尾
  停在 `pending`、没有租约（Agent 回传时才写终态），干活的是 Agent 节点，租约在它自己那条
  任务上（Agent 心跳时续租），靠 `source_task_id` 指回来。只看前者会让判据在派发模式下
  **退回墙钟** —— 大仓同步照样在第 30 分钟被放行。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from utils.logger import log_print

#: Agent 侧「这条任务正在被执行」的状态。与 `BackgroundTask` 那边同义。
_AGENT_RUNNING_STATUSES = ("processing",)


def _as_naive_utc(value) -> Optional[datetime]:
    """库里的时间是 naive-UTC，比较的另一头可能带时区 —— 混着减会抛 TypeError。

    不是 `datetime`（测试桩里的字符串、老数据里的怪值）时返回 `None` = 没有可用的时间。
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _lease_alive(value, current: datetime) -> bool:
    deadline = _as_naive_utc(value)
    return deadline is not None and deadline > current


def agent_executor_alive(background_task_id, *, now=None) -> bool:
    """这条后台任务**派发出去**的那条 Agent 任务，它的执行者还在吗。

    见模块 docstring 「两套租约都要看」：agent 派发模式下 BackgroundTask 一直没有租约，
    只有 `AgentTask` 有。读不到时返回 `False`（按「执行者不在」处理）—— 那种情况下调用方
    会退回它自己的墙钟兜底，而反过来会让一条僵尸任务**永久**拦住分析。
    """
    if background_task_id is None:
        return False
    current = _as_naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        from models import AgentTask

        row = (
            AgentTask.query.filter(
                AgentTask.source_task_id == background_task_id,
                AgentTask.status.in_(_AGENT_RUNNING_STATUSES),
            )
            .order_by(AgentTask.id.desc())
            .first()
        )
    except (SQLAlchemyError, RuntimeError) as exc:
        # 两种读不到：库读不动，以及**没有可用的应用上下文**（纯函数调用与单元测试的常态）。
        # 两种都按「执行者不在」处理 —— 调用方会退回它自己的年龄兜底（与加这一层之前
        # 逐字一致），而反过来按「在」会让一条僵尸任务**永久**拦住分析。
        # `RuntimeError` 是 Flask 在没有上下文时抛的（`AgentTask.query` 要 app）；
        # 只接这两种，别的异常照旧往上冒（宽 except 会把「忘了 import」也咽掉）。
        log_print(f"⚠️ 读不到 Agent 侧任务租约（这一条按「执行者不在」处理）: {exc}", "TASK")
        return False
    return row is not None and _lease_alive(getattr(row, "lease_expires_at", None), current)


def executor_alive(task, *, now=None) -> bool:
    """这条后台任务的执行者还在吗。`task` 为 None（或没读到时）返回 `False`。

    两条租约任一还活着就算在（见模块 docstring）：本进程 worker 的租约，或者它派发出去
    那条 Agent 任务的租约。
    """
    if task is None:
        return False
    current = _as_naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    if _lease_alive(getattr(task, "lease_expires_at", None), current):
        return True
    return agent_executor_alive(getattr(task, "id", None), now=current)
