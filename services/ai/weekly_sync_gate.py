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
    """这个批次里还在跑（或还没开跑）的周版本同步任务，返回最新的那一条。

    返回 `(task, 已跑秒数)`；没有就 `(None, 0)`。
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
    # 最新的那一条（查询已按 created_at 降序）。
    newest = rows[0]
    # 用 created_at 而不是 started_at：排队等着开跑同样是「还没写完」，
    # 而 started_at 在 pending 阶段是空的。
    created = _as_naive_utc(newest.created_at)
    age = (current - created).total_seconds() if created else 0.0
    return newest, max(age, 0.0)


def weekly_sync_in_flight(config_ids: Iterable[int], *, now: Optional[datetime] = None) -> str:
    """同步还在跑吗？是的话返回一句**给人看的原因**，否则返回空串。

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
