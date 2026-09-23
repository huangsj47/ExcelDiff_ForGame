#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台级（全平台合计）的 AI 预算：一个月/一周内所有项目加起来最多花多少。

## 为什么要有这一层

项目级预算（`AiProjectAnalysisConfig.budget_*`）回答的是「这个项目能花多少」，但回答不了
**「这个平台一个月总共烧了多少」** —— 而后者才是真正会失控的那个数：每个项目都设了 100 元
上限、30 个项目，账单一万。所以两者是**叠加**关系，不是一个替代另一个：任何一档超了都
挡住下一次分析（见 `services/ai/analysis_budget.py` 的 `budget_status`）。

## 单行表

`id` 固定为 1（`SINGLETON_ID`），全表只有这一行。用单行表而不是「键值对配置表」：
这一层的字段是**有类型、有范围、有语义**的（周期是枚举、上限是整数/十进制金额），
塞进 `key/value` 之后，校验、默认值、NULL 语义全都要在读取侧重新实现一遍，而那正是
「同一个规则写两份、然后漂移」的老路。

## 未配置 = 不限制

平台预算由管理员显式配置；两列为 NULL 时平台档不限制。项目自身仍有 100M/月的
安全默认，因此普通用户忘记配置时不会无限累计。0 仍是非法配置。

* `_optional_int` / `_optional_money` / `_budget_period_or_default`。

**复用私有函数是有意的。** 这三个函数实现的是「NULL 怎么读」这条口径，而它一旦在两处
各写一份，就一定会有一天两边不一致 —— 那时界面上的上限与实际生效的上限不同，而且没人
看得出来。跨模块导入一个下划线开头的名字确实不好看，但比复制一份好。
"""
from datetime import datetime, timezone

from .. import db
from .project_config import (
    DEFAULT_BUDGET_COST_LIMIT,
    DEFAULT_BUDGET_PERIOD,
    DEFAULT_PLATFORM_BUDGET_TOKEN_LIMIT,
    _budget_period_or_default,
    _optional_int,
    _optional_money,
)

# 全表唯一的那一行的主键。**不要**改成自增多行：多行会立刻带来「哪一行生效」的问题，
# 而这个问题没有好答案（按更新时间？按 id？），最后必然变成一个人为事故。
SINGLETON_ID = 1


class AiPlatformBudget(db.Model):
    """平台总预算。任何时刻最多一行，`id == SINGLETON_ID`。"""

    __tablename__ = "ai_platform_budget"

    id = db.Column(db.Integer, primary_key=True)
    # 与项目档同一个枚举（`budget_period` 三选一），默认同样是「本月」。
    period = db.Column(db.String(20), default=DEFAULT_BUDGET_PERIOD)
    # NULL = 平台档不限制。见模块 docstring。
    token_limit = db.Column(db.BigInteger)
    cost_limit = db.Column(db.String(40))

    updated_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def resolved(self) -> dict:
        """读成「实际生效的配置」：平台档 NULL 表示不限制。

        与 `AiProjectAnalysisConfig.resolved()` 同一口径。`configured` 是给界面用的：
        「没配过」与「配了但不限制」在数据上难以区分（都是两列 NULL），而在界面上必须
        区分 —— 前者要引导用户去配置，后者要显示成「当前不限制」。
        """
        return {
            "period": _budget_period_or_default(self.period),
            "token_limit": (
                _optional_int(self.token_limit) or DEFAULT_PLATFORM_BUDGET_TOKEN_LIMIT
            ),
            "cost_limit": _optional_money(self.cost_limit),
            "configured": (
                _optional_int(self.token_limit) is not None
                or _optional_money(self.cost_limit) is not None
            ),
            "updated_by": self.updated_by or "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# 未配置时的默认值（与列默认值同源，界面回填与校验都用这两个）。
PLATFORM_BUDGET_DEFAULTS = {
    "period": DEFAULT_BUDGET_PERIOD,
    "token_limit": DEFAULT_PLATFORM_BUDGET_TOKEN_LIMIT,
    "cost_limit": DEFAULT_BUDGET_COST_LIMIT,
}
