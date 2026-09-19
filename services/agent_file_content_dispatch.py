#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""platform+agent 模式下**取一个文件的正文或它某一条提交的 diff**：让 Agent 去取，平台只读结果。

## 为什么平台自己不取

代码文件没有单文件缓存（`DiffCache` / `ExcelDiffCacheService` 都是配表专用的），所以配表
的正文和 diff 平台都拿得到、代码的拿不到。平台**被显式禁止** clone
（`get_file_content_from_git` 在 platform/agent 模式下直接返回 None，日志里那句就是
「请由 Agent 节点提供数据」）—— 业务节点才是有工作副本、也才连得通代码服务器的那一端。

所以这两样只能由 Agent 取。这与 `commit_diff` 的分发是同一条路：
`AgentTask`（平台派发）→ Agent 在**自己的**工作副本上用 git 读（开源工具，无新依赖）
→ 结果回传落库 → 平台读到。

## 为什么 diff 也要走这条道（2026-09-19）

周版本分析读的是平台**已合并落库**的那一份 diff（覆盖整个窗口），这份在平台本地就有。
但模型有时要的是**某一条提交**改了什么，而实时算一条提交的 diff 需要工作副本 ——
platform/agent 模式下平台没有，那条路必然「取数失败」，报告里于是出现
「未读到任何代码 diff」。正文接上了 Agent、diff 没接，是漏的，不是刻意的。

## 为什么是「按需 + 有界等待」而不是「同步时全量预取」

* **按需**：模型只会问它真正需要的少数几个文件。全量预取意味着每个版本几百个代码文件的
  正文都要跨节点传一遍、还要在库里留一份（`temp_cache` 是有保留期的）—— 绝大多数永远
  不会被读。
* **有界等待**：Agent 的任务轮询间隔是秒级（`AGENT_TASK_POLL_INTERVAL_SECONDS`，默认 3s），
  所以一次取数通常几秒就回来了。但**等待必须有上限**：Agent 离线时不能让每一次
  索取都白等一遍（一次分析可能索取二十次，那就是几分钟的干等）。
  所以先看 Agent 在不在线：不在线就立刻给出说明并把任务留在队列里（它上线后会跑，
  下一次分析直接命中），在线才等，且等不过 `AGENT_FETCH_WAIT_SECONDS`。

结果落在 `AgentTask.result_summary`（平台的结果处理对没有专属分支的任务类型是原样落的），
所以不需要动 temp cache 那套（那是给超大 payload 用的，这里已经按窗口切过，远小于阈值）。

## 失败过的还会再试，但试到次数就停

「上次失败」常常是**配置问题**（工作副本还没建好、节点用错了数据库），而配置会被修好；
认了那条失败记录就等于把这条路永久堵死（表现是「问题已经修了，报告里还是那句读不到」，
且没人会发现）。所以失败过的会再派一次，连续失败到 `AGENT_FETCH_MAX_ATTEMPTS` 次才停手 ——
真的取不到时（这个提交里确实没有这个路径），不能每次分析都白等一轮上限。

## 两份请求共用一套等待/重试/复用逻辑

正文与 diff 的**差异只在 payload 与措辞**（一个带窗口与字符上限、一个什么都不带），
而等待上限、失败重试上限、任务复用、「同一份请求只等一次」这些容易写错的地方**必须只有
一份实现** —— 两份拷贝的那一天起，两份的修复速度就不会一样了。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from models import AgentNode, AgentProjectBinding, AgentTask, db
from services.agent_management_handlers import enqueue_agent_task
from utils.content_window import CONTENT_MAX_CHARS

FILE_CONTENT_TASK_TYPE = "file_content"
# 让 Agent 算**某一条提交**改了这个文件的什么（实时那条路的 Agent 版）。
FILE_DIFF_TASK_TYPE = "file_diff"

# 一次取回的正文上限（字符）。**这是省 token 的第一道闸**：模型要的是「改动周围长什么样」，
# 不是整份文件。超过就按窗口切，并把「还有多少行没给」如实写进结果。
#
# 取值就是预算层的单条上限（`utils.content_window.CONTENT_MAX_CHARS`，`ContextTools`
# 与 `PlatformContextProvider` 都引用它）：取数侧先切、预算层后切的话，后一刀会砍在
# 半行中间，而抬头里那句「第 a–b 行」也就成了假的。
FILE_CONTENT_MAX_CHARS = CONTENT_MAX_CHARS

# 等 Agent 回来的上限（秒）与轮询间隔。正文与 diff 共用一套：它们的往返代价同量级
# （都是「一个任务 + 一次 git 读」），而两条不同的等待上限只会让「为什么这次慢」多一种解释。
AGENT_FETCH_WAIT_SECONDS = 15.0
AGENT_FETCH_POLL_SECONDS = 0.5

# 同一份请求（仓库+提交+路径+窗口）连续失败多少次之后不再自动重派。
#
# 为什么不是「失败一次就永远不再派」：失败的原因常常是**配置**，而配置会被修好
# （工作副本补上了、节点换成了正确的数据库）。旧的失败记录把这条路堵死，表现就是
# 「问题已经修了，报告里却还是那句读不到」—— 而且没有任何人会发现。
# 为什么也不是「每次都重派」：真的取不到时（这个提交里确实没有这个路径），
# 每次分析都要白等一轮上限（15 秒）才拿到同一句失败原因。
AGENT_FETCH_MAX_ATTEMPTS = 3

# 旧名字：正文那一路的既有引用（`tests/test_content_window.py` 等）继续可用。
FILE_CONTENT_WAIT_SECONDS = AGENT_FETCH_WAIT_SECONDS
FILE_CONTENT_POLL_SECONDS = AGENT_FETCH_POLL_SECONDS
FILE_CONTENT_MAX_ATTEMPTS = AGENT_FETCH_MAX_ATTEMPTS


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def agent_for_project(project_id: int):
    """项目绑定的 Agent 节点（没绑定返回 None）。"""
    if not project_id:
        return None
    try:
        binding = AgentProjectBinding.query.filter_by(project_id=project_id).first()
    except Exception:  # noqa: BLE001
        return None
    if binding is None or not binding.agent_id:
        return None
    try:
        return db.session.get(AgentNode, binding.agent_id)
    except Exception:  # noqa: BLE001
        return None


def _normalize_utc(value) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def agent_online(agent) -> bool:
    """Agent 在不在线（与 `agent_commit_diff_dispatch._agent_online` 同一口径）。

    口径必须一致：离线判定偏乐观会让每一次索取都白等一轮上限，偏悲观则永远不去取。
    """
    if agent is None:
        return False
    if str(getattr(agent, "status", "") or "").strip().lower() != "online":
        return False
    heartbeat = _normalize_utc(getattr(agent, "last_heartbeat", None))
    if heartbeat is None:
        return False
    timeout = _env_float("AGENT_OFFLINE_TIMEOUT_SECONDS", 90.0, minimum=30.0)
    return (datetime.now(timezone.utc) - heartbeat).total_seconds() <= timeout


def _task_payload(task) -> dict:
    try:
        payload = json.loads(task.payload or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _task_result(task) -> Optional[dict]:
    try:
        summary = json.loads(task.result_summary or "null")
    except (TypeError, ValueError):
        return None
    return summary if isinstance(summary, dict) else None


def _matches(task, *, commit_id: str, file_path: str, lines: str) -> bool:
    """这个任务是不是同一份请求。

    `lines` 对 diff 请求恒为空串，而 diff 的 payload 里根本不写 `lines` ——
    `str(payload.get("lines") or "") == ""` 成立，所以两种请求都能用同一条判据
    （不需要一个「diff 用的 _matches」）。
    """
    payload = _task_payload(task)
    return (
        str(payload.get("commit_id") or "") == str(commit_id or "")
        and str(payload.get("file_path") or "") == str(file_path or "")
        and str(payload.get("lines") or "") == str(lines or "")
    )


def _recent_tasks(*, task_type: str, project_id: int, repository_id: int) -> list:
    """这个项目+仓库最近的取数任务（**无论成败**），新的在前。"""
    try:
        return (
            AgentTask.query.filter_by(
                task_type=task_type,
                project_id=project_id,
                repository_id=repository_id,
            )
            .order_by(AgentTask.id.desc())
            .limit(80)
            .all()
        )
    except Exception:  # noqa: BLE001
        return []


def _find_task(
    *, task_type: str, project_id: int, repository_id: int, commit_id: str, file_path: str, lines: str
):
    """最近一次同参数的取数任务（无论成败）。"""
    for task in _recent_tasks(
        task_type=task_type, project_id=project_id, repository_id=repository_id
    ):
        if _matches(task, commit_id=commit_id, file_path=file_path, lines=lines):
            return task
    return None


def _count_failed_attempts(
    *, task_type: str, project_id: int, repository_id: int, commit_id: str, file_path: str, lines: str
) -> int:
    """同一份请求已经失败过几次（用来决定还要不要再试）。"""
    return sum(
        1
        for task in _recent_tasks(
            task_type=task_type, project_id=project_id, repository_id=repository_id
        )
        if _matches(task, commit_id=commit_id, file_path=file_path, lines=lines)
        and str(task.status or "").lower() == "failed"
    )


def _cached_result(
    *, task_type: str, project_id: int, repository_id: int, commit_id: str, file_path: str, lines: str = ""
) -> Optional[dict]:
    """已经取回来过的那一份（**纯读**，不派发、不等待）。

    分析里模型常常对同一个文件问两次（第一轮看看、后面为了引用再要一次），第二次不该
    再等一次 Agent。
    """
    task = _find_task(
        task_type=task_type, project_id=project_id, repository_id=repository_id,
        commit_id=commit_id, file_path=file_path, lines=lines,
    )
    if task is None or str(task.status or "").lower() != "completed":
        return None
    return _task_result(task)


def cached_file_content(
    *, project_id: int, repository_id: int, commit_id: str, file_path: str, lines: str = ""
) -> Optional[dict]:
    """正文：已经取回来过的那一份（纯读）。"""
    return _cached_result(
        task_type=FILE_CONTENT_TASK_TYPE, project_id=project_id,
        repository_id=repository_id, commit_id=commit_id, file_path=file_path, lines=lines,
    )


def cached_file_diff(
    *, project_id: int, repository_id: int, commit_id: str, file_path: str
) -> Optional[dict]:
    """diff：已经取回来过的那一份（纯读）。"""
    return _cached_result(
        task_type=FILE_DIFF_TASK_TYPE, project_id=project_id,
        repository_id=repository_id, commit_id=commit_id, file_path=file_path,
    )


def _request_from_agent(
    *,
    task_type: str,
    noun: str,
    repository,
    commit_id: str,
    file_path: str,
    lines: str = "",
    extra_payload: Optional[dict] = None,
    request_key: str = "",
    wait_seconds: Optional[float] = None,
    sleep_func=time.sleep,
) -> dict:
    """让 Agent 取一份内容（正文或 diff），返回 `{status, ...}`。

    `status`：
      * `ready`       —— 拿到了（内容在返回值里，键由 Agent 侧决定）；
      * `pending`     —— 已经派出去、这次没等到（Agent 离线或太慢）。任务留在队列里，
                         它跑完就落库，下一次索取直接命中；
      * `unavailable` —— 取不了，且**不是**「还在路上」（项目没绑 Agent / 派发失败 / 取数失败），
                         由调用方把原因如实写进给模型的文本。

    **不抛异常**：取一次失败只该让这一条降级，不该让整次分析崩掉。

    「上一次失败了」不等于「这次也拿不到」：失败过的会**再派一次**（配置修好之后能自己恢复），
    但连续失败到 `AGENT_FETCH_MAX_ATTEMPTS` 次就停手，只如实回报原因。

    `noun` 只用于给模型看的措辞（「正文」/「差异」）：正文取不到与 diff 取不到，用户能做的
    事不一样（前者换个文件、后者可能是同步没跑完），所以那句话必须说对是哪一样。
    """
    project_id = getattr(repository, "project_id", None)
    repository_id = getattr(repository, "id", None)
    if not project_id or not repository_id:
        return {"status": "unavailable", "message": "仓库缺少项目信息，无法向 Agent 取数"}

    cached = _cached_result(
        task_type=task_type, project_id=project_id, repository_id=repository_id,
        commit_id=commit_id, file_path=file_path, lines=lines,
    )
    if cached is not None:
        return {"status": "ready", **cached}

    agent = agent_for_project(project_id)
    if agent is None:
        return {
            "status": "unavailable",
            "message": (
                "项目没有绑定 Agent 节点，platform/agent 模式下平台本地也没有这个仓库的"
                f"工作副本，读不到文件{noun}"
            ),
        }

    online = agent_online(agent)
    task = _find_task(
        task_type=task_type, project_id=project_id, repository_id=repository_id,
        commit_id=commit_id, file_path=file_path, lines=lines,
    )
    if task is not None and str(task.status or "").lower() == "failed":
        # 最近一次同参数的取数**失败了**。失败常常是配置问题（工作副本还没建好、节点用错了
        # 数据库），而配置会被修好 —— 一条旧的失败记录把这条路永久堵死，表现就是「问题已经
        # 修了，报告里却还是那句读不到」，且没人会发现。所以再试，但**有次数上限**：
        # 真的取不到时（这个提交里确实没有这个路径）不能每次分析都白等一轮上限。
        attempts = _count_failed_attempts(
            task_type=task_type, project_id=project_id, repository_id=repository_id,
            commit_id=commit_id, file_path=file_path, lines=lines,
        )
        if attempts >= AGENT_FETCH_MAX_ATTEMPTS:
            detail = str(task.error_message or "").strip()
            return {
                "status": "unavailable",
                "message": (
                    f"Agent 取数连续失败 {attempts} 次{('：' + detail) if detail else ''}"
                    "（不再自动重试；配置修好后需要人工重跑一次取数任务）"
                ),
            }
        task = None

    if task is None:
        try:
            task = enqueue_agent_task(
                task_type=task_type,
                project_id=project_id,
                repository_id=repository_id,
                source_task_id=None,
                priority=4,
                payload={
                    "repository_id": int(repository_id),
                    "project_id": int(project_id),
                    "commit_id": str(commit_id or ""),
                    "file_path": str(file_path or ""),
                    **(extra_payload or {}),
                    "request_key": request_key,
                },
            )
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            return {"status": "unavailable", "message": f"派发取数任务失败：{type(exc).__name__}: {exc}"}

    if task is None:
        return {"status": "unavailable", "message": "派发取数任务失败"}

    if not online:
        return {
            "status": "pending",
            "message": "Agent 当前离线，取数任务已排队（它上线后会取回来，下次索取直接命中）",
        }

    wait_budget = (
        AGENT_FETCH_WAIT_SECONDS if wait_seconds is None else max(0.0, float(wait_seconds))
    )
    deadline = time.monotonic() + wait_budget
    while True:
        try:
            db.session.expire(task)
            db.session.refresh(task)
        except Exception:  # noqa: BLE001 —— 刷新失败按「还没回来」继续等
            pass
        status = str(task.status or "").lower()
        if status == "completed":
            result = _task_result(task)
            if result is None:
                return {"status": "unavailable", "message": f"Agent 回传的{noun}结果读不出来"}
            return {"status": "ready", **result}
        if status == "failed":
            detail = str(task.error_message or "").strip()
            return {
                "status": "unavailable",
                "message": f"Agent 取数失败{('：' + detail) if detail else ''}",
            }
        if time.monotonic() >= deadline:
            return {
                "status": "pending",
                "message": (
                    f"已向 Agent 索取（task_id={task.id}），{wait_budget:g} 秒内"
                    "没等到；它跑完会落库，下次索取直接命中"
                ),
            }
        sleep_func(AGENT_FETCH_POLL_SECONDS)


def request_file_content(
    repository,
    *,
    commit_id: str,
    file_path: str,
    lines: str = "",
    max_chars: int = FILE_CONTENT_MAX_CHARS,
    max_rows: int = 0,
    wait_seconds: Optional[float] = None,
    sleep_func=time.sleep,
) -> dict:
    """让 Agent 取一份正文。

    `max_rows` 只在目标是配表时有意义（Agent 侧按它渲染工作表正文，与平台本地同一个
    渲染函数、同一个默认值）；平台传 0 表示「按默认」，与 Agent 侧的默认一致。
    """
    return _request_from_agent(
        task_type=FILE_CONTENT_TASK_TYPE,
        noun="正文",
        repository=repository,
        commit_id=commit_id,
        file_path=file_path,
        lines=lines,
        extra_payload={"lines": str(lines or ""), "max_chars": int(max_chars), "max_rows": int(max_rows)},
        request_key=f"file_content:{getattr(repository, 'id', '')}:{commit_id}:{file_path}:{lines}",
        wait_seconds=wait_seconds,
        sleep_func=sleep_func,
    )


def request_file_diff(
    repository,
    *,
    commit_id: str,
    file_path: str,
    wait_seconds: Optional[float] = None,
    sleep_func=time.sleep,
) -> dict:
    """让 Agent 算**这一条提交**改了这个文件的什么。

    返回的 `payload` 就是平台侧那份 diff 结构的原样（Agent 在它自己的工作副本上跑的是
    **同一个** `vcs_content_service.get_unified_diff_data`），所以平台拿到之后走同一个
    `render_diff_payload` 渲染 —— 模型看到的文本与单机模式逐字一致。

    **没有 `lines`**：diff 本身就是「改了哪几行」，再给窗口没有意义。
    """
    return _request_from_agent(
        task_type=FILE_DIFF_TASK_TYPE,
        noun="差异",
        repository=repository,
        commit_id=commit_id,
        file_path=file_path,
        extra_payload={},
        request_key=f"file_diff:{getattr(repository, 'id', '')}:{commit_id}:{file_path}",
        wait_seconds=wait_seconds,
        sleep_func=sleep_func,
    )

