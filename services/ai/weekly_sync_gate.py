#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「周版本同步还在跑」的闸门：同步没写完之前不要开始 AI 分析。

## 为什么必须有这道闸门

周版本同步是**按文件逐个**写缓存行的（`weekly_version_logic.process_weekly_version_sync`
的循环 → `generate_weekly_merged_diff`：一个文件一行、各自提交），而 AI 分析的变更清单
按 `WeeklyVersionDiffCache.updated_at > last_analyzed_at` 取（`_summarize_weekly_files`）。

于是**同步跑到一半时的快照只有已写好的那批文件**。用户看到的表现是这句话：

    本轮仅取得少量配表 diff 与角色属性表/怪物仇恨表内容，
    战斗逻辑、吸灵器链、拓扑组队、支付等代码改动均未取到 diff 正文

——不是「取不到」，是**那批文件当时还没被写进缓存**，所以根本没进清单。更糟的是它是
**静默**的：提示词里的「共 N 个文件」也跟着变小，模型与读报告的人都以为那就是全量。

为什么容易撞上：两个调度器周期不同（同步每 2 分钟、AI 每 1 分钟），启动时 AI 任务可以
先被创建并执行；而重启时 `load_pending_tasks` 还会把上次残留的 `processing` 任务改回
pending 再跑一次（见 `ai_analysis_service.run_weekly_analysis_background` 的说明）。

队列优先级本身是对的（`weekly_sync`=3 先于 `weekly_ai_analysis`=6），但那只在**两者都
已经排队**时才起作用。

## 边界

* **只看 `weekly_sync`。** `auto_sync`（每 2 分钟、所有仓库）不改变变更清单的构成
  （清单来自周版本缓存行），而它长期在跑 —— 拿它当闸门会让分析几乎永远不触发。
* **有上限。** 一个卡死的同步任务不能把分析永久挡住：超过 `SYNC_IN_FLIGHT_MAX_SECONDS`
  就带着一条醒目日志放行。卡死本身有别的机制兜（`schedule_weekly_sync_tasks` 会把
  超时的 pending 任务置 failed；重启时 `load_pending_tasks` 会把 processing 改回 pending），
  这道上限是最后一道保底。
* **`pending` 只在「它马上会开跑」时才算在跑**（2026-09-21 收窄，见 `_executor_busy`）：
  本地后台任务是**单线程**的，一次 AI 分析就把它占满，这时调度器每 2 分钟补进来的
  `weekly_sync` 只能停在 `pending` —— 而**缓存并没有在被写**。把它也算作「在跑」的后果
  是这期间的手工分析全部起不来（实测连续 22 次、跨度约 7 分钟全被回 `sync_in_flight`）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

# 同步任务在这个时长之内算「还在跑」，超过就认为是卡死了（放行并记一条醒目日志）。
#
# 取值依据：周版本同步要逐文件算合并 diff（大仓库上千个文件），分钟级是常态；
# 而 AI 分析的触发周期是 1 分钟 —— 等一会儿没有代价，等一小时才是问题。
SYNC_IN_FLIGHT_MAX_SECONDS = 30 * 60


def _as_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """把库里的时间统一成 naive-UTC。

    SQLite 存的是 naive（ORM 默认写 `datetime.now(timezone.utc)`，丢 tzinfo），
    而比较的另一头可能带时区 —— 混着减会抛 `TypeError`，那会让闸门变成「每次都不拦」，
    也就是静默失效。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _in_flight_sync_task(config_ids: Iterable[int], *, now: Optional[datetime] = None):
    """这个批次里**算在跑**的周版本同步任务，返回最新的那一条。

    返回 `(task, 已跑秒数)`；没有就 `(None, 0)`。

    ## 「算在跑」是两种，不是三种（2026-09-21 收窄）

    * `processing` —— 真正在往缓存里写。**这是本模块存在的理由**，一律算；
    * `pending` **且执行侧空着** —— 它会在 worker 下一次取任务时立刻开跑（队列是内存
      队列、`get(timeout=1)`），所以与「正在写」没有区别，也算；
    * `pending` **而执行侧正忙** —— **不算**。单线程的 worker 被别的任务（实测：一次
      AI 分析）占住时，这条 pending 在整个分析期间都不会开始，**缓存没有在被写**；
      拿它拦只会让分析永远起不来。见 `_executor_busy`。
    """
    ids = [str(int(item)) for item in config_ids if item is not None]
    if not ids:
        return None, 0.0

    from models import BackgroundTask

    try:
        rows = (
            BackgroundTask.query.filter(
                BackgroundTask.task_type == "weekly_sync",
                BackgroundTask.commit_id.in_(ids),
                BackgroundTask.status.in_(("pending", "processing")),
            )
            # **降序**（=上面 docstring 说的「最新的那一条」）。原来是 `asc` 取 `rows[0]`
            # —— 那拿到的是**最旧**的一条：一批里同时有一条卡死 40 分钟的同步与一条刚起跑
            # 10 秒的同步时，闸门按旧那条算 age 已经超过 `SYNC_IN_FLIGHT_MAX_SECONDS`，
            # 于是**直接放行**，而另一个同步正在往缓存里写 —— 变更清单缺文件，正是这个
            # 模块存在的理由。改回降序之后，「只剩一条卡死的」仍然照旧放行（上限语义不变）。
            .order_by(BackgroundTask.created_at.desc())
            .all()
        )
    except Exception:  # noqa: BLE001 —— 查不动只是少一道闸，不该把分析卡死
        return None, 0.0
    if not rows:
        return None, 0.0

    current = _as_naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    # 「算在跑」的那一档：真正在写的优先（它比排队更接近出问题的时刻）。
    blocking = [row for row in rows if _status_of(row) == "processing"]
    if not blocking:
        # 全是 pending：只有执行侧空着，它们才真的马上会开跑。
        # 执行侧忙（单线程 worker 正拿着 AI 分析 / 别的同步 / 一堆 excel_diff）时返回
        # 「没有在跑」—— 这正是这次要修的那条：那段最不需要拦的时间里，闸门一直拦着。
        if _executor_busy():
            return None, 0.0
        blocking = list(rows)

    # 最新的那一条（两个列表都保持查询的 created_at 降序）。
    newest = blocking[0]
    # 用 created_at 而不是 started_at：排队等着开跑同样是「还没写完」，
    # 而 started_at 在 pending 阶段是空的。
    created = _as_naive_utc(newest.created_at)
    age = (current - created).total_seconds() if created else 0.0
    return newest, max(age, 0.0)


def _status_of(task) -> str:
    """任务行的状态（小写；取不动时当空串 —— 它两个分支都不进，也就是不拦）。"""
    return str(getattr(task, "status", "") or "").strip().lower()


def _executor_busy() -> bool:
    """执行侧（本进程那个**唯一**的后台任务 worker）正拿着别的任务吗？

    ## 为什么需要这一条

    本地后台任务是**单线程**的（`task_worker_service.background_task_worker` 只有一个
    线程，任务先进内存队列、再逐个执行）。一次 AI 分析就能把它占满，这时调度器每 2 分钟
    补进来的 `weekly_sync` 只能停在 `pending`。实测精确到毫秒：任务 381 在 `12:52:30.079`
    才开始，而 run 15 结束于 `12:52:29.994`（晚 0.085 秒）；382 又在 381 完成后 0.008 秒
    接上 —— 也就是说**分析期间缓存并没有在被写**，但旧判据把那条 pending 也算成「在跑」，
    于是闸门在那段最不需要拦的时间里一直拦着：连续 22 次手工发起、跨度约 7 分钟，
    全部被回 `event: waiting`（`reason: sync_in_flight`），run 数一条没涨。

    ## 判据为什么是「`processing` **且 `started_at` 有值**」

    * `processing`：只有被 worker 取走过的行才会是这个状态
      （`update_task_status_with_retry`）；
    * `started_at` 有值：**「被取走过」的凭据**。只翻状态列、不写这一列的写入路径
      （以及测试里手工造的行）不能证明执行侧忙；少了这个限定，一行永久卡在 `processing`
      的残留会把判据钉死在「忙」上，闸门于是再也不会在「马上要开跑」时拦人。

    ## 边界

    * **不区分任务类型**：AI 分析、`auto_sync`、`excel_diff` 都占着同一个线程；
    * agent 派发模式下任务由别的节点执行，本进程忙不忙说明不了什么 —— 那种部署里这条
      判据基本恒为「不忙」，也就是**退回旧的严判据**（`pending` 一律拦），防护不变弱；
    * 查不动时按「忙」处理：与 `_in_flight_sync_task` 的 `except` 同一条口径 ——
      少一道闸，不把分析卡死。
    """
    from models import BackgroundTask

    try:
        busy = (
            BackgroundTask.query.filter(
                BackgroundTask.status == "processing",
                BackgroundTask.started_at.isnot(None),
            ).first()
        )
    except Exception:  # noqa: BLE001 —— 见 docstring：少一道闸，不把分析卡死
        return True
    return busy is not None


def weekly_sync_in_flight(config_ids: Iterable[int], *, now: Optional[datetime] = None) -> str:
    """同步**正把缓存写在半路**吗？是的话返回一句**给人看的原因**，否则返回空串。

    判据是「算在跑」（见 `_in_flight_sync_task`）：真正在写的（`processing`）一律拦；
    排队等 worker 的（`pending`）只在**执行侧空着、它马上会开跑**时才拦。执行侧被别的
    任务占住时那条 pending 不会开始写，不拦 —— 见 `_executor_busy`。

    调用方拿到原因后应当**跳过这一次分析并保留触发水位线**（不要推进
    `last_triggered_at`/`last_analyzed_at`），下一个周期自然会重试。
    """
    task, age = _in_flight_sync_task(config_ids, now=now)
    if task is None:
        return ""
    if age > SYNC_IN_FLIGHT_MAX_SECONDS:
        return ""
    minutes = int(age // 60)
    return (
        f"周版本同步还在跑（task_id={task.id}，已 {minutes} 分钟）："
        "现在分析只能看到已经写进缓存的那部分文件，等它写完再分析"
    )


def weekly_sync_stuck_note(config_ids: Iterable[int], *, now: Optional[datetime] = None) -> str:
    """卡死的同步任务（超过上限仍在跑）：给一句醒目日志用的话，没有就空串。"""
    task, age = _in_flight_sync_task(config_ids, now=now)
    if task is None or age <= SYNC_IN_FLIGHT_MAX_SECONDS:
        return ""
    minutes = int(age // 60)
    return (
        f"⚠️ 周版本同步 task_id={task.id} 已跑 {minutes} 分钟仍未结束，"
        f"超过 {SYNC_IN_FLIGHT_MAX_SECONDS // 60} 分钟上限，不再拦着 AI 分析"
        "（这一轮看到的变更清单可能不完整）"
    )


def group_config_ids(config) -> list:
    """一个周版本批次（同一项目、同一窗口）里的全部配置 id。

    与 `ai_analysis_service.build_weekly_payload` 的分组口径一致：闸门要拦的是
    **整批**的同步，因为变更清单来自这一批的全部仓库（只看自己那一个仓库的同步，
    另一个仓库还在写的时候照样会漏文件）。
    """
    if config is None:
        return []
    from models.weekly_version import WeeklyVersionConfig

    try:
        rows = WeeklyVersionConfig.query.filter(
            WeeklyVersionConfig.project_id == config.project_id,
            WeeklyVersionConfig.start_time == config.start_time,
            WeeklyVersionConfig.end_time == config.end_time,
        ).all()
    except Exception:  # noqa: BLE001
        return [getattr(config, "id", None)] if getattr(config, "id", None) else []
    ids = [getattr(row, "id", None) for row in rows]
    return [item for item in ids if item]
