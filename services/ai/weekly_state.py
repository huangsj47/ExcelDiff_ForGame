#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周版本分析分组的「水位线状态行」：拿到还是建一行。

## 为什么单独一个模块

`ai_weekly_analysis_state` 这一行有两个**互不相干**的写入方 —— 分析收尾
（`services/ai_analysis_service._update_weekly_state`）与调度器
（`services/task_worker_service` 那一 tick）—— 而它们必须用**同一份**建行逻辑，
否则并发首跑时两个都按「先查后插」写，后提交的必然撞唯一约束。

放在这里而不是编排层，还因为 `services/ai_analysis_service.py` 贴着文件长度硬上限
（2000 行），而这段只依赖模型层、不碰引擎。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError

from models import db
from models.ai_analysis import AiWeeklyAnalysisState

# 建行时唯一约束的名字（SQLite / 其他后端都把它写在异常文本里）。只用于注释与报错，
# 不做字符串匹配 —— 判据是「回滚后能不能重查到那一行」，不是文案。
UNIQUE_GROUP_KEY = "ai_weekly_analysis_state.group_key"


def get_or_create_weekly_state(
    *,
    project_id: Optional[int],
    group_key: str,
    base_name: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> AiWeeklyAnalysisState:
    """拿到这个周版本分组的状态行，**没有就建一行**。并发首跑时不会炸。

    ## 为什么不能让调用方各写一遍「先查后插」

    `group_key` 上有唯一约束，而「先查后插」在并发下必然有一个输家 —— 两个请求/线程
    都查到 `None`、都去 `add`，后提交的那个抛
    `IntegrityError: UNIQUE constraint failed`。而这个窗口**很宽**：状态行在分析
    **开跑前**读（`build_weekly_payload`），写入在分析**结束**时，中间隔着整轮 LLM
    调用（几十秒）。两个真实触发路径：

    * 同一个周版本分组并发发起两次手动分析（两个标签页 / 两次 SSE 请求。平台是
      `threaded=True`，两条流各在自己的请求线程里）；
    * 首次手动分析进行中，调度器那一 tick 又轮到同一分组 —— 此时没有 state，
      「间隔节流」判据（`last_triggered_at`）直接失效，于是它照常排队并建行。

    输的那一方的代价不只是「报一个错」：结论已经落库了，丢的是**交付** ——
    界面收到一条 error 事件、`result` 不发，而 `last_analyzed_at` 没推进，
    下一轮把全部文件当新变更重跑（白花一次 token）。调度器那条更重：它的
    `except SQLAlchemyError` 在 for 循环**之外**，一次撞约束会把这一 tick 剩下的
    分组全部放弃排队。

    ## 为什么不用锁

    `agent_task_enqueue_service` 的 docstring 已经说过：进程内的锁在多进程部署下失效
    （而且这里还有调度线程）。**让唯一约束自己裁决**是唯一跨进程正确的做法：
    输的一方回滚后重查，那一行一定已经在里面了。

    ## 为什么可以在这里单独 commit

    调用点都在「这一行之外没有别的待写」的位置：`_update_weekly_state` 走到这里时结论
    已经由 `_persist_outcome` 提交过；调度器那两个字段的更新紧跟着它。所以这次提交
    既不会把别的东西提前落库，也不会丢 —— 撞约束时只回滚这一行。
    """
    state = AiWeeklyAnalysisState.query.filter_by(group_key=group_key).first()
    if state is not None:
        return state

    state = AiWeeklyAnalysisState(
        project_id=project_id,
        group_key=group_key,
        base_name=base_name,
        start_time=start_time,
        end_time=end_time,
    )
    db.session.add(state)
    try:
        db.session.commit()
    except IntegrityError:
        # 另一个线程刚刚建了同一行。回滚掉我们这一条，重查它那条。
        db.session.rollback()
        state = AiWeeklyAnalysisState.query.filter_by(group_key=group_key).first()
        if state is None:
            # 不是并发首跑，而是别的完整性错误 —— 照旧抛出去，别把它咽掉。
            raise
    return state
