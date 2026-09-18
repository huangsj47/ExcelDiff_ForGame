#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 消耗统计的**口径**：从哪一刻起算（单行表 `ai_usage_statistics`）。

## 为什么需要「统计起点」

消耗面板上的数字是**全部历史**的累计。一旦有人在同一个库上反复试跑（调提示词、验证部署、
压测），那一屏的总量就永远带着这些噪音，而它恰恰是给人看「这个版本花了多少」的地方。
全量重置能清库，但它是不可逆的、而且会连分析报告一起删掉 —— 大多数时候想要只是
「从这里开始重新算」。

所以两件事分开：

* **统计起点**（本表）：只改口径，数据一条不删，随时可以恢复成「全部历史」；
* **全量重置**：真删记录，见 `services/ai/usage_statistics.purge_usage_statistics`。

## 为什么单独建表，而不是给 `ai_platform_budget` 加一列

那张表的行同时喂着**预算闸门**（`services/ai/analysis_budget.platform_budget_status`）：
超限就挡住下一次分析。而统计起点**绝不该影响闸门** —— 否则「点一下重置」等于把额度送了
回去，正是 `services/ai/analysis_budget.py` 模块 docstring 里点名警告过的那种用法
（「一个需要人工点的『重置』必然有人忘了点，或者被人当成『清账』按钮」）。
两张表分开之后，这条边界在 schema 上就是可见的：想接错线，得先把两张表拼起来。

## 时间口径：naive UTC（与 `ai_analysis_run.created_at` 同一套）

SQLite 绑定参数时会静默丢掉 tzinfo，所以库里存的都是 naive 墙钟；AI 这一族（`created_at`
/ `updated_at`）统一是 **naive UTC**，与 `WeeklyVersionConfig.start_time` 那类**北京墙钟**
不是一回事（见 `utils/timezone_utils` 的模块说明）。写入前用
`beijing_wallclock_to_utc_naive` 换算，展示时用 `utc_naive_to_beijing_wallclock` 转回来。
"""
from datetime import datetime, timezone

from .. import db

# 全表唯一的那一行的主键。理由与 `ai_platform_budget` 相同：多行立刻带来「哪一行生效」
# 的问题，而这个问题没有好答案。
SINGLETON_ID = 1


class AiUsageStatistics(db.Model):
    """消耗统计的口径。任何时刻最多一行，`id == SINGLETON_ID`。

    没有这一行时，语义是**「统计全部历史」**（与「有行但 `counted_since` 为 NULL」等价）：
    这条口径必须是「读不到就等同于没设过」，否则一次没跑过迁移的库会变成「什么都不统计」。
    """

    __tablename__ = "ai_usage_statistics"

    id = db.Column(db.Integer, primary_key=True)

    # 统计起点（**naive UTC**）。NULL = 统计全部历史。
    # 只影响消耗面板的读路径（`services/ai_usage_service.filter_runs`），不影响预算。
    counted_since = db.Column(db.DateTime)

    # 最近一次**全量重置**（真删记录）的时间与人。与起点分开记：起点会被反复调整，
    # 而「这个库被清过没有、谁清的」是一件事后必须答得上来的问题。
    reset_at = db.Column(db.DateTime)
    reset_by = db.Column(db.String(100))

    updated_by = db.Column(db.String(100))
    updated_at = db.Column(db.DateTime)


def now_utc_naive() -> datetime:
    """当前时间，写成库里约定的 **naive UTC**。

    与 `analysis_run.created_at` 的默认值（`datetime.now(timezone.utc)` 被 SQLite 丢掉
    tzinfo）落到同一个口径上 —— 两边必须逐字一致，否则起点与 `created_at` 的比较会差 8 小时，
    而那种错只有在跨时区部署或特定数据上才暴露。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
