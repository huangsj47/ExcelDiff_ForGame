#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Project-level AI analysis configuration."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .. import db

# ---------------------------------------------------------------------------
# 默认值的**唯一事实源**
# ---------------------------------------------------------------------------
# 这些数字此前散落在服务层的模块常量里，示例配置里还有另一份，两处漂移过 8 项
# （旧工具就是这样：改了一处忘了另一处，于是「文档说的默认值」与「实际生效的默认值」
# 不一致）。现在集中在这里，服务层从这里取。
#
# 另一件必须记住的事：`_migrate_table_columns` 用 `ALTER TABLE ... ADD COLUMN` 加列，
# **不会带 DEFAULT 子句**，所以已部署的库里老行的新列是 NULL。因此列的 `default=` 只对
# 「新建的行」有效，读取时必须用 `resolved()` 兜住 NULL —— 不能假设读出来就是默认值。
DEFAULT_AUTO_WEEKLY_ENABLED = True
DEFAULT_WEEKLY_INTERVAL_MINUTES = 60
DEFAULT_MAX_FILES_PER_RUN = 200
DEFAULT_MAX_ANALYSIS_ROUNDS = 8
DEFAULT_MAX_TOOL_REQUESTS = 20
# 与「索取次数 × 单条上限」互相自洽的预算值。
#
# 变更清单现在是**全量列出**的（只有超过 `ai_analysis_service.MAX_LIST_CHARS` 才退化成
# 取样），所以摘要本身的量级从「150 个提交 600 个文件约 39,000 字符」变成了「顶到清单
# 上限、240 个提交时实测 87,055 字符」。加上平台 skill 与项目知识包（约 12,000）与历史
# 结论基线（6,000），再给 20 次 × 11,000 的上下文留足额度：
#
#     12,000 + 87,055 + 6,000 + 20 × 11,000 = 325,055  ≤  360,000
#
# 旧的 200,000 配 20 次索取装不下（差 125,000），后果是模型索要 20 个文件、其中一半被
# 截断，而它分不清是预算不够还是文件就这么大。**改这个值必须同时看
# `DEFAULT_MAX_TOOL_REQUESTS` 与 `services/ai/context_tools.DEFAULT_TOOL_LIMITS`**，
# `test_the_prompt_budget_can_honor_the_request_budget` 会拦住只顾一个的改法。
DEFAULT_PROMPT_CHAR_BUDGET = 360_000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300
DEFAULT_MIN_SEVERITY = "high"
DEFAULT_MIN_CONFIDENCE = "high"
DEFAULT_MAX_ANOMALIES_PER_RUN = 10

# --- 提示词缓存标记（行为在 services/ai/prompt_cache.py，这里只存值）---
# 两栏合起来才决定「这次请求带不带缓存断点」，默认值是**不发**：
#
#     mode=auto + format=none       →  不发标记（默认）
#     mode=auto + format=anthropic  →  发
#     mode=explicit（任意 format）   →  发（用户明确声称端点接受）
#     mode=off                      →  永不发
#
# 默认不发是刻意的：`cache_control` 不是 OpenAI 协议的一部分，按主机名/模型名猜
# 「像不像 Anthropic」对**内网网关**天然失效，而猜错的代价是一次 400。要发的部署者
# 只需要声明他**知道**的那件事：这个端点接受哪种约定。
#
# **不发标记 ≠ 关掉缓存**：DeepSeek / OpenAI 这类端点做的是自动前缀缓存，什么都不
# 用声明，只要前缀稳定就命中（本机网关实测三轮 71.5% → 83.8%）。
#
# 值域在这里写死、在 `prompt_cache` 里再写一份，两边由
# `test_ai_models_and_migration.py` 的两条防漂移用例钉住 —— 与本文件其它默认值
# 一样的处理方式（模型层不 import 服务层，见文件顶部那段说明）。
PROMPT_CACHE_MODE_CHOICES = ("off", "auto", "explicit")
PROMPT_CACHE_FORMAT_CHOICES = ("none", "anthropic")
DEFAULT_PROMPT_CACHE_MODE = "auto"
DEFAULT_PROMPT_CACHE_FORMAT = "none"

# 取值范围。前后端共用同一组边界：界面上的 min/max 与服务端的校验必须一致，否则会出现
# 「前端允许填、后端悄悄改掉」这种用户看不懂的行为。
MAX_ANALYSIS_ROUNDS_RANGE = (1, 30)
MAX_TOOL_REQUESTS_RANGE = (0, 100)

# --- 子代理模式（2026-09，见 services/ai/subagent.py）---
# **默认关**。这是一条会让模型调用次数变成 (n+1) 倍的功能，必须由人主动打开；
# 而且它只对**周版本**分析生效（单提交的规模本来就不需要分工）。
DEFAULT_SUBAGENT_ENABLED = False
# 打开之后的默认成员数。1 = 退化成原来的单代理（那时不该走子代理这条路），所以
# 有效范围是 2~6；存 1 也允许（界面上的「关掉」有两处，这里不额外制造一种非法状态）。
DEFAULT_SUBAGENT_COUNT = 3
SUBAGENT_COUNT_RANGE = (1, 6)
# **对账轮**（找反证，见 `services/ai/subagent.py::build_verify_task`）：汇总出报告之后，
# 再把最高严重度的几条交给一次独立的核对请求，要求它去找反证。同样**默认关** ——
# 它是又一轮模型调用，而它带来的价值取决于读报告的人会不会去核那几条。
DEFAULT_SUBAGENT_VERIFY = False
PROMPT_CHAR_BUDGET_RANGE = (10_000, 2_000_000)
REQUEST_TIMEOUT_RANGE = (10, 3600)
MAX_FILES_PER_RUN_RANGE = (1, 5_000)
WEEKLY_INTERVAL_RANGE = (5, 10_080)
MAX_ANOMALIES_PER_RUN_RANGE = (0, 200)
SEVERITY_CHOICES = ("high", "critical")
CONFIDENCE_CHOICES = ("high", "very_high")

# --- 预算（AI 分析的闸门）---
# **未配置 = 不限制。** 这两个上限的默认值是 `None`，不是 0，也不是任何拍脑袋的数字：
# 0 会被读成「一个 token 都不许花」，把 AI 分析整个锁死；而一个编出来的默认上限
# 会在用户完全不知情的时候开始拦他的分析。
#
# 周期默认「本月」，也可以选「本周」或「全部时间」。跨周期时用量自然回到 0，
# 所以**不需要任何「重置」动作** —— 需要人工点的重置必然有人忘，或者被当成清账按钮。
DEFAULT_BUDGET_PERIOD = "monthly"
DEFAULT_BUDGET_TOKEN_LIMIT = None
DEFAULT_BUDGET_COST_LIMIT = None
BUDGET_PERIOD_CHOICES = ("monthly", "weekly", "all_time")
# token 上限的量级上限：1 万亿 token —— 有生之年撞不到，只是个防呆边界。
BUDGET_TOKEN_LIMIT_RANGE = (1, 1_000_000_000_000)
# 费用上限（十进制字符串，按价格表的币种）。同样只是个防呆边界。
BUDGET_COST_LIMIT_MAX = 1_000_000_000

# `resolved()` 里**允许返回 None** 的键。
#
# `None` 在别处是「忘了补默认值」的信号（`min_severity=None` 会让规则层什么都过滤不掉，
# 而且不报错），所以 `test_resolved_never_returns_none_for_any_key` 逐键拦着。这两栏
# 是例外，因为它们的 `None` 是一个**有意义的取值**：不限制。把它们改成 0 或者任何
# 拍脑袋的默认值，都会让「没配预算」变成「预算为 0 / 某个没人知道的数」。
NULLABLE_RESOLVED_KEYS = ("budget_token_limit", "budget_cost_limit")


def _int_or(value, fallback: int) -> int:
    """把可能是 NULL / 字符串的值读成整数，读不出来就用默认值。

    数据库里这些列在老库上是 NULL；在某些后端上读回来还可能是字符串。
    """
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _clamp_int(value, fallback: int, bounds: tuple[int, int]) -> int:
    """读一个**有范围**的整数：NULL / 读不动 → 默认值；超出范围 → 钳到边界。

    与 `_int_or` 分开：那个是「没有范围」的（`max_files_per_run` 这类），这个的上下界
    是功能语义的一部分（子代理数超过 6 就没意义），所以钳而不是照收。
    """
    number = _int_or(value, fallback)
    low, high = bounds
    return max(int(low), min(int(high), number))


def _optional_int(value) -> int | None:
    """读一个**可空**的整数上限：NULL / 空串 / 读不动 → `None`（= 不限制）。

    **不回落成 0，也不回落成任何默认值。** 这个函数是「未配置 = 不限制」这条口径的
    实现点：把它改成 `return 0`，所有没配预算的项目会在下一次分析时被全部拦掉，
    而且界面上找不到任何解释。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_money(value) -> str | None:
    """读一个可空的**金额**上限。存的是十进制字符串（金额不用 float，见 pricing）。

    非正数一律读成 `None`（不限制），与 `_optional_int` 同一口径：库里出现 0 只可能来自
    遗留数据或手工改库（配置校验不允许填 0），而「因为一个 0 把分析彻底锁死」的代价
    远大于「放过一次」。
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    return format(number, "f")


def _budget_period_or_default(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in BUDGET_PERIOD_CHOICES else DEFAULT_BUDGET_PERIOD


def _cache_choice_or_default(value, choices: tuple[str, ...], fallback: str) -> str:
    """读一个缓存标记相关的取值：不在值域里就用默认。

    **默认一律是「不发标记」那一侧的取值**（`auto` / `none`）。所以老库上的 NULL、
    手工改库改出来的怪值、将来被删掉的一个选项，全都退化成「不发标记」——
    而不会退化成「发一个谁也不认识的标记」。
    """
    text = str(value or "").strip().lower()
    return text if text in choices else fallback


class AiProjectAnalysisConfig(db.Model):
    __tablename__ = "ai_project_analysis_config"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), unique=True, nullable=False)

    auto_weekly_enabled = db.Column(db.Boolean, default=DEFAULT_AUTO_WEEKLY_ENABLED)
    weekly_interval_minutes = db.Column(db.Integer, default=DEFAULT_WEEKLY_INTERVAL_MINUTES)
    max_files_per_run = db.Column(db.Integer, default=DEFAULT_MAX_FILES_PER_RUN)
    # 语义已调整为「项目级补充指令」：整个提示词由平台内置 skill 承载，这一栏只做补充。
    # 历史项目里存过的自定义提示词照旧生效（降级为补充指令），不会丢配置。
    prompt_template = db.Column(db.Text)

    # --- 连接配置（OpenAI 兼容端点）---
    api_base_url = db.Column(db.String(500))
    api_model = db.Column(db.String(200))

    # --- 分析策略 ---
    max_analysis_rounds = db.Column(db.Integer, default=DEFAULT_MAX_ANALYSIS_ROUNDS)
    max_tool_requests = db.Column(db.Integer, default=DEFAULT_MAX_TOOL_REQUESTS)
    prompt_char_budget = db.Column(db.Integer, default=DEFAULT_PROMPT_CHAR_BUDGET)
    request_timeout_seconds = db.Column(db.Integer, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    # 提示词缓存标记（2026-09）。加列迁移不带 DEFAULT 子句，老行上是 NULL ——
    # 而 NULL 经 `resolved()` 读出来正好是「不发标记」那个保守默认值，不需要回填。
    prompt_cache_mode = db.Column(db.String(20), default=DEFAULT_PROMPT_CACHE_MODE)
    prompt_cache_format = db.Column(db.String(20), default=DEFAULT_PROMPT_CACHE_FORMAT)
    # 子代理模式（2026-09）。`subagent_enabled` 上 NULL 是**老行**（这一列之前没有），
    # 经 `resolved()` 读出来正是「关闭」那个默认值，所以不需要回填。
    # `subagent_count` 同理：NULL → 3。
    subagent_enabled = db.Column(db.Boolean, default=DEFAULT_SUBAGENT_ENABLED)
    subagent_count = db.Column(db.Integer, default=DEFAULT_SUBAGENT_COUNT)
    # 对账轮（2026-09）。NULL 同样是老行 → `resolved()` 读成「关闭」。
    subagent_verify = db.Column(db.Boolean, default=DEFAULT_SUBAGENT_VERIFY)

    # --- 告警门槛（规则侧，改这里不用改提示词）---
    min_severity = db.Column(db.String(20), default=DEFAULT_MIN_SEVERITY)
    min_confidence = db.Column(db.String(20), default=DEFAULT_MIN_CONFIDENCE)
    max_anomalies_per_run = db.Column(db.Integer, default=DEFAULT_MAX_ANOMALIES_PER_RUN)

    # --- 项目补充知识（在项目知识包之外追加，只补充不覆盖）---
    project_knowledge = db.Column(db.Text)

    # --- 模型单价表（算费用用，JSON 文本）---
    # 留空 = 用平台的默认表（`services/ai/pricing.py::DEFAULT_PRICE_TABLE`，出厂是空的）。
    # **单价必须由用户提供**：接口只回 token 数、不回金额，编一份单价出来会被当成真钱看。
    #
    # 这一列**不是**为兼容历史数据留的（这个配置项还没人用过），它就是这张表**唯一**的
    # 存储位置。变的是编辑入口：界面上不再出现在「AI 分析配置」里，而是搬到「AI 消耗」
    # 页面的「模型单价表」卡片上 —— 因为单价只影响那一页的费用数字，放在那里才对得上。
    # 读写仍然走同一个项目配置接口（`/ai-analysis/projects/<id>/config`），校验也仍然是
    # `endpoint_service.FIELD_RULES["model_price_table"]` 那一条，所以「一份事实源」这件事
    # 没有因为换入口而改变，也没有任何「读不到就回落到旧字段」的分支。
    model_price_table = db.Column(db.Text)

    # --- 预算闸门（超了就禁用 AI 分析，见 services/ai/analysis_budget.py）---
    # 这两列**可为 NULL**，NULL 的语义是「不限制」。加列迁移不带 DEFAULT 子句，
    # 所以老行上它们就是 NULL —— 与「不限制」正好一致，不需要额外的回填。
    budget_period = db.Column(db.String(20), default=DEFAULT_BUDGET_PERIOD)
    budget_token_limit = db.Column(db.BigInteger)
    budget_cost_limit = db.Column(db.String(40))

    updated_by = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", backref="ai_analysis_config")

    def resolved(self) -> dict:
        """把这一行读成「实际生效的配置」，NULL 用默认值补齐。

        **所有读取路径都必须走这里**。直接读 `config.min_severity` 在老库上会拿到
        `None`，而下游拿 `None` 当门槛的后果是「什么都过滤不掉」—— 报出一堆低置信度的
        条目，而且没有任何报错。
        """
        return {
            "auto_weekly_enabled": (
                DEFAULT_AUTO_WEEKLY_ENABLED
                if self.auto_weekly_enabled is None
                else bool(self.auto_weekly_enabled)
            ),
            "weekly_interval_minutes": _int_or(
                self.weekly_interval_minutes, DEFAULT_WEEKLY_INTERVAL_MINUTES
            ),
            "max_files_per_run": _int_or(self.max_files_per_run, DEFAULT_MAX_FILES_PER_RUN),
            "prompt_template": self.prompt_template or "",
            "api_base_url": (self.api_base_url or "").strip(),
            "api_model": (self.api_model or "").strip(),
            "max_analysis_rounds": _int_or(
                self.max_analysis_rounds, DEFAULT_MAX_ANALYSIS_ROUNDS
            ),
            "max_tool_requests": _int_or(self.max_tool_requests, DEFAULT_MAX_TOOL_REQUESTS),
            "prompt_char_budget": _int_or(self.prompt_char_budget, DEFAULT_PROMPT_CHAR_BUDGET),
            "request_timeout_seconds": _int_or(
                self.request_timeout_seconds, DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
            # 读不出来的值一律退回默认（保守＝不发标记），见 _cache_choice_or_default。
            "prompt_cache_mode": _cache_choice_or_default(
                self.prompt_cache_mode, PROMPT_CACHE_MODE_CHOICES, DEFAULT_PROMPT_CACHE_MODE
            ),
            "prompt_cache_format": _cache_choice_or_default(
                self.prompt_cache_format,
                PROMPT_CACHE_FORMAT_CHOICES,
                DEFAULT_PROMPT_CACHE_FORMAT,
            ),
            # 子代理模式：NULL（老行）读成「关闭」与 3。**关闭是唯一安全的默认值** ——
            # 打开它会让一次分析的模型调用次数变成 n+1 倍。
            "subagent_enabled": (
                DEFAULT_SUBAGENT_ENABLED
                if self.subagent_enabled is None
                else bool(self.subagent_enabled)
            ),
            "subagent_count": _clamp_int(
                self.subagent_count, DEFAULT_SUBAGENT_COUNT, SUBAGENT_COUNT_RANGE
            ),
            "subagent_verify": (
                DEFAULT_SUBAGENT_VERIFY
                if self.subagent_verify is None
                else bool(self.subagent_verify)
            ),
            "min_severity": (self.min_severity or DEFAULT_MIN_SEVERITY).strip().lower(),
            "min_confidence": (self.min_confidence or DEFAULT_MIN_CONFIDENCE).strip().lower(),
            "max_anomalies_per_run": _int_or(
                self.max_anomalies_per_run, DEFAULT_MAX_ANOMALIES_PER_RUN
            ),
            "project_knowledge": self.project_knowledge or "",
            # 空串 = 没有项目单价表（用平台默认表）。**不是「免费」**，读取侧据此返回
            # 「还没有配置价格表」而不是 0（见 services/ai/usage.py 的口径）。
            "model_price_table": self.model_price_table or "",
            # 预算：NULL 一律读成「不限制」，见 _optional_int / _optional_money。
            "budget_period": _budget_period_or_default(self.budget_period),
            "budget_token_limit": _optional_int(self.budget_token_limit),
            "budget_cost_limit": _optional_money(self.budget_cost_limit),
        }

    def __repr__(self):
        return f"<AiProjectAnalysisConfig project={self.project_id}>"
