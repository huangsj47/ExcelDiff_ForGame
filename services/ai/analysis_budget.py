"""AI 分析的预算闸门：项目级的 token / 费用上限，超了就不让分析跑起来。

## 为什么单独一个模块

「超预算就不跑」这件事要在**四个地方**成立：

1. `stream_commit_analysis` / `stream_weekly_analysis`（两个手动 SSE 入口）；
2. `run_weekly_analysis_background`（后台任务执行前）；
3. `schedule_weekly_ai_analysis_tasks`（调度器排队前）。

四处各写一遍判定的后果不是代码重复，而是**判定漂移**：手动路径按「本月」算、
后台路径按「全部时间」算，于是同一份配置在界面上说「已超预算、按钮已禁用」，
而后台照样在跑。所以「怎么算用过多少、超没超」只写在这里，那四个地方只调
`budget_gate_reason()`。

## 三条纪律

1. **未配置 = 不限制。** 读不到上限（NULL）就是「没有上限」，既不回落到 0
   （那会把功能整个锁死：0 表示「一个 token 都不许花」），也不回落到任何拍脑袋的
   默认值。`AiProjectAnalysisConfig.resolved()` 把 NULL 读成 `None`，这里据此判定。
2. **算不出就不拦。** 上游没报 token、价格表算不出费用、配置读不出来、数据库报错 ——
   这些情况下**一律放行**，并把原因写进 `notes`。宁可放过一次超预算的分析，也不能
   因为算不出费用就把 AI 分析功能整个锁死；后者的表现是「所有项目突然都不能分析了，
   而且界面说不出为什么」。
3. **判定只看确定的数。** token 那一档用的是「已上报部分的合计」（一个**下界**）：
   下界已经超了，就一定超了；下界没超，就不能据此拦人。费用那一档更严 ——
   只要有**任何一次**运行算不出金额，合计就不给数字（这是 `services/ai/usage.py`
   的口径），于是这一档不做判定。

## 时段口径

`budget_period` 三选一：`monthly`（本月，默认）/ `weekly`（本周）/ `all_time`（全部）。
「本月」按**北京时间**的自然月切分（用户看的日历是北京时间），查询时换算成 UTC 与
库里的 `created_at` 同口径比较。跨月时预算自然回到 0，不需要任何「重置」动作 ——
这是刻意选的：一个需要人工点的「重置」必然有人忘了点，或者被人当成「清账」按钮。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from models.ai_analysis import AiAnalysisRun
from services.ai.pricing import DEFAULT_CURRENCY, money
from services.ai.usage import aggregate_runs, usage_from_run
from utils.logger import log_print
from utils.timezone_utils import BEIJING_TZ

# 预算周期。改动这里要同步 `models/ai_analysis/project_config.BUDGET_PERIOD_CHOICES`
# —— 那边是给界面渲染选项用的事实源，`test_ai_usage_filters_and_budget.py` 会钉住
# 两者一致。
PERIOD_MONTHLY = "monthly"
PERIOD_WEEKLY = "weekly"
PERIOD_ALL_TIME = "all_time"
PERIOD_CHOICES = (PERIOD_MONTHLY, PERIOD_WEEKLY, PERIOD_ALL_TIME)
PERIOD_LABELS = {
    PERIOD_MONTHLY: "本月",
    PERIOD_WEEKLY: "本周",
    PERIOD_ALL_TIME: "全部时间",
}
DEFAULT_PERIOD = PERIOD_MONTHLY

# 两档预算。**它们是叠加的，不是二选一**：任意一档超了就挡住下一次分析。
#
# * `project` —— 这个项目在它自己的周期里花了多少（`AiProjectAnalysisConfig.budget_*`）；
# * `platform` —— **所有项目加起来**花了多少（`AiPlatformBudget`，单行表）。
#
# 为什么要两档：项目级上限管不住总量（30 个项目各设 100 元，账单一万），而不设项目级
# 上限又会让某一个项目把整个额度吃掉。两档各自回答一个不同的问题。
SCOPE_PROJECT = "project"
SCOPE_PLATFORM = "platform"
SCOPE_LABELS = {SCOPE_PROJECT: "项目预算", SCOPE_PLATFORM: "平台总预算"}


def as_utc(value: datetime | None) -> datetime | None:
    """把库里的时间读成带 UTC 时区的 datetime。

    库里存的是 naive 的 UTC 墙钟（SQLite 会丢掉 tzinfo，口径见 utils/timezone_utils），
    直接与 aware 的「现在」相减会抛 `TypeError`；而**假设**它是本地时间会差 8 小时，
    在月初那几小时里把上个月的运行算进本月。所以：naive 一律当 UTC 解释。
    """
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_period(raw: Any) -> str:
    """认不出来的周期一律退回默认（本月），**不报错**：这是个读侧口径，
    一个脏值不该让整个面板或闸门挂掉。"""
    text = str(raw or "").strip().lower()
    return text if text in PERIOD_CHOICES else DEFAULT_PERIOD


def period_window(
    period: str, *, now: datetime | None = None
) -> tuple[datetime | None, datetime | None]:
    """这个周期的 `[since, until)`（UTC）。`all_time` 返回 `(None, None)`。

    `until` 是**排他**上界：本月 = `[本月 1 日 00:00, 下月 1 日 00:00)`。
    用排他上界而不是「本月最后一天 23:59:59」，是为了不出现「月末那一秒的记录漏掉」
    这种只在特定时刻复现的 bug。
    """
    key = normalize_period(period)
    if key == PERIOD_ALL_TIME:
        return None, None

    moment = as_utc(now) or datetime.now(timezone.utc)
    beijing = moment.astimezone(BEIJING_TZ)
    if key == PERIOD_WEEKLY:
        # 周一为一周之始（ISO 8601）。
        start = (beijing - timedelta(days=beijing.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(days=7)
    else:
        start = beijing.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # 先加 32 天再取 1 号：比「按月份长度算」少一个分支，也不会在 2 月出错。
        end = (start + timedelta(days=32)).replace(day=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def period_label(period: str) -> str:
    return PERIOD_LABELS.get(normalize_period(period), PERIOD_LABELS[DEFAULT_PERIOD])


def runs_in_window(
    runs: Iterable[AiAnalysisRun],
    since: datetime | None,
    until: datetime | None,
) -> list[AiAnalysisRun]:
    """按 `[since, until)` 过滤。**在 Python 里比、不在 SQL 里比** ——
    库里的 `created_at` 是 naive 的，拿 aware 的边界去比较在不同后端上行为不一致
    （SQLite 按字符串比，MySQL 按值比），而这种差异只在特定数据上才暴露。
    """
    result = []
    for run in runs:
        stamp = as_utc(getattr(run, "created_at", None))
        if stamp is None:
            # 没有时间的记录不进任何时间窗：把它算进「本月」是一个无法核对的数字。
            continue
        if since is not None and stamp < since:
            continue
        if until is not None and stamp >= until:
            continue
        result.append(run)
    return result


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _fmt_tokens(value: int | None) -> str:
    """token 数 → 12.3k / 1.25M。与前端 `aiuFmtTokens` 同一套口径。"""
    if value is None:
        return "未上报"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _fmt_money(value: Decimal | None, currency: str) -> str:
    text = money(value)
    if text is None:
        return "未配置"
    symbol = {"CNY": "¥", "USD": "$", "EUR": "€"}.get(currency or "", "")
    return f"{symbol}{text}" if symbol else f"{text} {currency or ''}".strip()


def _unlimited(reason: str, *, scope: str = SCOPE_PROJECT, **extra: Any) -> dict[str, Any]:
    """一张「不拦」的判定结果。任何一处读不出数都用它，见模块 docstring 第 2 条。"""
    payload: dict[str, Any] = {
        "scope": scope,
        "limited": False,
        "over": False,
        "blocks_analysis": False,
        "checked": False,
        "period": DEFAULT_PERIOD,
        "period_label": period_label(DEFAULT_PERIOD),
        "since": None,
        "until": None,
        "limits": {"tokens": None, "cost": None, "currency": ""},
        "used": {"tokens": None, "cost": None, "currency": "", "runs": 0},
        "ratios": {"tokens": None, "cost": None},
        "over_limits": [],
        "reason": "",
        "notes": [reason] if reason else [],
    }
    payload.update(extra)
    return payload


def _cost_totals(groups: Sequence[tuple[Sequence[AiAnalysisRun], Any]]) -> tuple[Decimal | None, str, str]:
    """把若干「一组运行 + 它们该用的价格表」的费用加起来。

    返回 `(金额, 币种, 算不出的原因)`。金额是 `None` 时原因一定是一句人话。

    **一组都算不出，合计就是算不出**（不是「把算得出的加起来」）：少算一个项目的费用，
    得到的那个数字看起来完全正常、实际偏小，而它会被拿去和上限比。混币种同理 ——
    人民币与美元相加没有意义。这两条是 `services/ai_usage_service._totals_cost` 的同一
    口径，跨项目合计只在这一处实现。
    """
    if not groups:
        return None, "", "这个周期内没有任何运行记录"
    total = Decimal(0)
    currency = ""
    for runs, table in groups:
        stats = aggregate_runs(runs, price_table=table)
        cost = (stats.get("cost") or {}) if stats else {}
        amount = cost.get("amount")
        if amount is None:
            return None, "", str(cost.get("reason") or "价格表或 token 数不可用")
        this_currency = str(cost.get("currency") or "")
        if currency and this_currency and this_currency != currency:
            return None, "", f"周期内混用了多种币种（{currency} / {this_currency}），无法相加"
        currency = currency or this_currency
        try:
            total += Decimal(str(amount))
        except (InvalidOperation, ValueError):
            return None, "", f"费用合计（{amount}）读不成数字"
    return total, currency, ""


def _evaluate_scope(
    *,
    scope: str,
    label: str,
    runs: Sequence[AiAnalysisRun],
    token_limit: int | None,
    cost_limit: Decimal | None,
    period: str,
    since: datetime | None,
    until: datetime | None,
    cost_groups: Sequence[tuple[Sequence[AiAnalysisRun], Any]],
    cost_detail_fallback: str = "",
) -> dict[str, Any]:
    """一段「谁在用、用了多少、超没超」的判定。**项目档与平台档共用这一个实现。**

    为什么要共用：这两档的判据必须逐字一致（token 取已上报部分的下界、费用算不出就不判），
    而各写一份的必然结果是某天开始两边松紧不同 —— 那时「平台说没超、项目说超了」，
    两个数字看上去都很正常。`scope` 只影响措辞与归因，不参与计算。
    """
    base: dict[str, Any] = {
        "scope": scope,
        "limited": True,
        "over": False,
        "blocks_analysis": False,
        "checked": True,
        "period": period,
        "period_label": label,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "limits": {
            "tokens": token_limit,
            "cost": money(cost_limit),
            # 币种来自价格表（金额只按价格表算）。拿不到价格表时先给默认币种：
            # 这一档此时本来就不做判定（见下面的费用分支），显示什么符号都不影响结论。
            "currency": DEFAULT_CURRENCY,
        },
        "used": {"tokens": None, "cost": None, "currency": "", "runs": len(runs)},
        "ratios": {"tokens": None, "cost": None},
        "over_limits": [],
        "reason": "",
        "notes": [],
    }

    notes: list[str] = []
    over_limits: list[str] = []
    reasons: list[str] = []

    # --- token 那一档：用**已上报部分的下界** ---
    if token_limit is not None:
        partial = 0
        missing = 0
        for run in runs:
            bucket = usage_from_run(run, price_table=None)["tokens"]
            for value in (bucket.get("input"), bucket.get("output")):
                if value is None:
                    missing += 1
                else:
                    partial += int(value)
        base["used"]["tokens"] = partial
        if missing:
            # 下界已经是「所有报上来的数之和」：它超了就是真超了；它没超时不能拦人。
            notes.append(
                f"有 {missing} 处 token 数上游未上报，已用量是下界（只统计报上来的部分）"
            )
        base["ratios"]["tokens"] = partial / token_limit if token_limit else None
        if partial > token_limit:
            over_limits.append("tokens")
            reasons.append(
                f"{label}的 token 已用 {_fmt_tokens(partial)}，超过上限 {_fmt_tokens(token_limit)}"
            )

    # --- 费用那一档：只要有**任何一处**算不出金额，就不判定 ---
    if cost_limit is not None:
        total, currency, detail = _cost_totals(cost_groups)
        if total is None:
            # 算不出费用 → **不许拦**。这是本模块最要紧的一条行为：价格表没配、
            # 模型名对不上、上游没报 token，都不该变成「分析按钮变灰且说不清原因」。
            notes.append(
                f"费用上限已配置，但{label}的金额算不出来"
                f"（{detail or cost_detail_fallback or '没有可用价格表或上游未上报 token'}），"
                "因此不按费用拦截"
            )
        else:
            base["used"]["cost"] = money(total)
            base["used"]["currency"] = currency or base["limits"]["currency"]
            base["ratios"]["cost"] = float(total / cost_limit) if cost_limit else None
            if total > cost_limit:
                over_limits.append("cost")
                reasons.append(
                    f"{label}的费用已用 {_fmt_money(total, currency)}，"
                    f"超过上限 {_fmt_money(cost_limit, currency)}"
                )

    base["over_limits"] = over_limits
    base["over"] = bool(over_limits)
    base["blocks_analysis"] = bool(over_limits)
    base["notes"] = notes
    base["reason"] = "；".join(reasons)
    return base


def _project_scope(project_id: int) -> dict[str, Any]:
    """项目档：这个项目在它自己的预算周期里用了多少、超没超。"""
    from services.ai_analysis_service import (  # 延迟导入：本模块被该模块依赖
        get_project_analysis_config,
    )

    try:
        config = get_project_analysis_config(project_id) or {}
    except Exception as exc:  # noqa: BLE001 —— 见 docstring「不抛异常」
        log_print(f"⚠️ AI 预算判定读配置失败，本次放行: project={project_id} {exc}", "AI", force=True)
        return _unlimited(f"读项目配置失败（{exc}），本次不按预算拦截")

    period = normalize_period(config.get("budget_period"))
    token_limit = _int_or_none(config.get("budget_token_limit"))
    cost_limit = _decimal_or_none(config.get("budget_cost_limit"))
    label = period_label(period)

    if token_limit is None and cost_limit is None:
        status = _unlimited("", period=period, period_label=label)
        status["checked"] = True
        status["notes"] = ["这个项目没有配置 token / 费用上限，不做限制。"]
        return status

    since, until = period_window(period)
    try:
        all_runs = AiAnalysisRun.query.filter_by(project_id=project_id).all()
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ AI 预算判定查运行记录失败，本次放行: project={project_id} {exc}", "AI", force=True)
        return _unlimited(
            f"读用量失败（{exc}），本次不按预算拦截", period=period, period_label=label
        )

    runs = runs_in_window(all_runs, since, until)
    table, price_errors = _price_table_for(project_id)
    fallback = ("价格表不可用：" + "；".join(price_errors)) if price_errors else ""
    return _evaluate_scope(
        scope=SCOPE_PROJECT,
        label=label,
        runs=runs,
        token_limit=token_limit,
        cost_limit=cost_limit,
        period=period,
        since=since,
        until=until,
        cost_groups=[(runs, table)],
        cost_detail_fallback=fallback or _first_reason(runs, table),
    )


def platform_budget_status(*, now: datetime | None = None) -> dict[str, Any]:
    """平台档：**所有项目加起来**在这个周期里用了多少、超没超。

    费用那一档的算法与项目档不同但同源：按项目分组，每组用**它自己**的价格表算，
    再要求「每组都算得出、且币种一致」才给合计（见 `_cost_totals`）。混币种相加得到的
    数字比没有数字更危险。

    **不抛异常**，与 `budget_status` 同一条纪律。
    """
    from services.ai.platform_budget import get_platform_budget

    try:
        config = get_platform_budget() or {}
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ 平台预算判定读配置失败，本次放行: {exc}", "AI", force=True)
        return _unlimited(f"读平台预算配置失败（{exc}），本次不按平台预算拦截", scope=SCOPE_PLATFORM)

    period = normalize_period(config.get("period"))
    token_limit = _int_or_none(config.get("token_limit"))
    cost_limit = _decimal_or_none(config.get("cost_limit"))
    label = period_label(period)

    if token_limit is None and cost_limit is None:
        # **没配平台总预算时不产出任何 note。** 平台没设上限是一件不需要在每一个项目的
        # 界面上说一遍的事，而且那会让「项目档的说明」被一句无关的话稀释掉。
        status = _unlimited("", scope=SCOPE_PLATFORM, period=period, period_label=label)
        status["checked"] = True
        return status

    since, until = period_window(period, now=now)
    try:
        all_runs = AiAnalysisRun.query.all()
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ 平台预算判定查运行记录失败，本次放行: {exc}", "AI", force=True)
        return _unlimited(
            f"读用量失败（{exc}），本次不按平台预算拦截",
            scope=SCOPE_PLATFORM,
            period=period,
            period_label=label,
        )

    runs = runs_in_window(all_runs, since, until)

    # 按项目分组：费用必须用各项目自己的价格表算。
    by_project: dict[int, list[AiAnalysisRun]] = {}
    for run in runs:
        by_project.setdefault(int(getattr(run, "project_id", 0) or 0), []).append(run)
    groups: list[tuple[Sequence[AiAnalysisRun], Any]] = []
    for project_id, group_runs in by_project.items():
        table, _errors = _price_table_for(project_id)
        groups.append((group_runs, table))

    return _evaluate_scope(
        scope=SCOPE_PLATFORM,
        label=label,
        runs=runs,
        token_limit=token_limit,
        cost_limit=cost_limit,
        period=period,
        since=since,
        until=until,
        cost_groups=groups,
        cost_detail_fallback=(
            "在所有项目里都找不到可用价格表" if groups else ""
        ),
    )


def _merge_scopes(project: dict[str, Any], platform: dict[str, Any]) -> dict[str, Any]:
    """把两档判定合成一张「要不要拦住下一次分析」的结果。

    **顶层字段仍然是项目档那一份**（`limits` / `used` / `ratios` / `period`）：界面上的
    「本期预算」列、`test_ai_usage_filters_and_budget.py` 的断言读的都是它，改语义会让
    「这一列到底在说什么」变得没有答案。平台档整份挂在 `platform` 键下，并且：

    * `over` / `blocks_analysis` / `limited` 是**两档的或**（任意一档超了就拦）；
    * `over_scopes` 说清是哪一档超的；
    * `notes` 里平台那一档的说明带上前缀，避免和项目档的说明混在一起分不出谁是谁；
    * `reason` 的结尾按「谁超了」给不同的去处（平台的上限在「AI 消耗」页面，不在项目配置里）。

    平台**没配**预算时，这一层等于原样返回项目档并附一份「不限制」的平台档 ——
    不产生任何 note，也不改 `reason`。
    """
    merged = dict(project)
    merged["platform"] = platform
    over_scopes = [
        name
        for name, scope in ((SCOPE_PROJECT, project), (SCOPE_PLATFORM, platform))
        if scope.get("over")
    ]
    merged["over_scopes"] = over_scopes

    if platform.get("limited") or over_scopes:
        # 配了就把平台档那几句带上（含「为什么没判」），否则用户会以为平台上限没生效。
        # **没配平台预算时这里一个 note 都不加**，行为与改动前逐字一致。
        merged["notes"] = [
            *project.get("notes", ()),
            *(f"平台总预算：{note}" for note in platform.get("notes", ())),
        ]

    if over_scopes:
        reasons = [
            scope.get("reason")
            for scope in (project, platform)
            if scope.get("over") and scope.get("reason")
        ]
        merged["limited"] = True
        merged["over"] = True
        merged["blocks_analysis"] = True
        merged["reason"] = "；".join(reasons) + _remedy_tail(over_scopes)
    return merged


def _remedy_tail(over_scopes: Sequence[str]) -> str:
    """「超了之后该怎么办」的那句话。**按谁超了给不同的去处** —— 平台的上限不在项目配置里，
    让用户去项目的「AI 分析配置」找平台总预算，他会找不到，然后以为是个 bug。"""
    if list(over_scopes) == [SCOPE_PLATFORM]:
        return "。已暂停 AI 分析，请在「AI 消耗」页面的「平台总预算」里调高上限，或等下个周期。"
    if SCOPE_PLATFORM in over_scopes:
        return (
            "。已暂停 AI 分析，请调高平台总预算（「AI 消耗」页面）或项目预算"
            "（项目的「AI 分析配置」），或等下个周期。"
        )
    return "。已暂停 AI 分析，请在项目的「AI 分析配置」里调高预算或等下个周期。"


def with_live_usage(
    status: Mapping[str, Any],
    *,
    tokens: int = 0,
    cost: Decimal | None = None,
    currency: str = "",
) -> dict[str, Any]:
    """把「**本次运行尚未落库**的用量」加进一份已有判定里，返回一份新的判定。

    用途只有一个：**分析正在跑的时候**告诉用户「现在就已经超了」（界面轮询，
    见 `services/ai/run_progress.py`）。它**不参与闸门** —— 闸门判的是「下一次能不能
    起跑」，在开跑前判一次就够（模块 docstring 第 2、3 条）。

    为什么要有这个函数而不是在路由里加一下数字：两档（项目 / 平台）都要加、`over` 与
    `over_scopes` 都要重算、`reason` 要重写，而**判据必须与闸门逐字一致** —— 在别处
    重写一遍的必然结果是某天开始「跑中提示说超了、闸门说没超」。

    三条与闸门一致的纪律：

    * **传进来的 token 也是下界**（`RoundProgress` 只累加上游报上来的部分）。下界加上
      已落库的下界仍然超了 → 确定超了；没超 → 什么都不说。
    * **金额算不出就不判**（`cost=None`）。宁可不说，也不给一个偏小的假超支。
    * **不修改入参**：调用方（路由）手里那份还要原样用。
    """
    # 深拷贝：`status` 里有嵌套的 dict（limits / used / ratios / notes / over_limits /
    # platform），浅拷贝之后改 `used` 会把调用方那份一起改掉 —— 而调用方（路由）手里
    # 那份还要原样用（它要同时回给界面做对比）。
    merged = copy.deepcopy(dict(status))
    live_tokens = max(0, int(tokens or 0))
    live_cost = cost if isinstance(cost, Decimal) else None
    if live_tokens == 0 and live_cost is None:
        return merged

    scopes = [SCOPE_PROJECT, SCOPE_PLATFORM]
    added: dict[str, Any] = {}
    for scope_name in scopes:
        node = merged if scope_name == SCOPE_PROJECT else merged.get("platform")
        if not isinstance(node, dict) or not node.get("limited"):
            continue
        limits = node.get("limits") or {}
        used = node.get("used") or {}
        over_limits = list(node.get("over_limits") or ())
        reasons: list[str] = []

        if live_tokens and limits.get("tokens") is not None:
            base_tokens = used.get("tokens")
            # 已落库那一档可能是 `None`（一处都没上报）—— 那它按 0 加，并在下面说明。
            used["tokens"] = int(base_tokens or 0) + live_tokens
            limit_tokens = limits.get("tokens")
            node["ratios"] = {**(node.get("ratios") or {})}
            node["ratios"]["tokens"] = (
                used["tokens"] / limit_tokens if limit_tokens else None
            )
            if used["tokens"] > limit_tokens and "tokens" not in over_limits:
                over_limits.append("tokens")
                reasons.append(
                    f"{node.get('period_label') or ''}的 token 已用 "
                    f"{_fmt_tokens(used['tokens'])}（含本次运行），超过上限 "
                    f"{_fmt_tokens(limit_tokens)}"
                )

        if live_cost is not None and limits.get("cost") is not None:
            try:
                limit_cost = Decimal(str(limits.get("cost")))
            except (InvalidOperation, ValueError):
                limit_cost = None
            if limit_cost is not None:
                base_cost = used.get("cost")
                try:
                    total_cost = (Decimal(str(base_cost)) if base_cost is not None else Decimal(0)) + live_cost
                except (InvalidOperation, ValueError):
                    total_cost = None
                if total_cost is not None:
                    used["cost"] = money(total_cost)
                    used["currency"] = used.get("currency") or currency or limits.get("currency") or ""
                    node["ratios"] = {**(node.get("ratios") or {})}
                    node["ratios"]["cost"] = float(total_cost / limit_cost) if limit_cost else None
                    if total_cost > limit_cost and "cost" not in over_limits:
                        over_limits.append("cost")
                        reasons.append(
                            f"{node.get('period_label') or ''}的费用已用 "
                            f"{_fmt_money(total_cost, used['currency'])}（含本次运行），"
                            f"超过上限 {_fmt_money(limit_cost, used['currency'])}"
                        )
                    node["used"] = used

        node["used"] = used
        node["over_limits"] = over_limits
        node["over"] = bool(over_limits)
        node["blocks_analysis"] = bool(over_limits)
        if reasons:
            node["reason"] = "；".join(reasons)
            added[scope_name] = reasons

    if not added:
        return merged

    over_scopes = [
        name
        for name, node in ((SCOPE_PROJECT, merged), (SCOPE_PLATFORM, merged.get("platform")))
        if isinstance(node, dict) and node.get("over")
    ]
    merged["over_scopes"] = over_scopes
    merged["over"] = bool(over_scopes)
    merged["blocks_analysis"] = bool(over_scopes)
    merged["limited"] = True
    reasons = [text for group in added.values() for text in group]
    merged["reason"] = "；".join(reasons) + _remedy_tail(over_scopes)
    merged["live_included"] = {
        "tokens": live_tokens,
        "cost": money(live_cost),
        # **必须写在给用户看的话里**：这个数还没落库，与「已用量」不是一个口径。
        "note": (
            f"其中含本次运行已消耗的 {_fmt_tokens(live_tokens)} token"
            "（尚未落库，是只统计上游已上报部分的下界）"
            if live_tokens else "其中含本次运行已消耗的费用（尚未落库）"
        ),
    }
    merged["notes"] = [
        *(merged.get("notes") or ()),
        merged["live_included"]["note"],
    ]
    return merged


def budget_status(project_id: int, *, platform: dict[str, Any] | None = None) -> dict[str, Any]:
    """这个项目当前的预算状态，以及「要不要拦住下一次分析」。

    返回体的关键字段（界面与闸门都读这几个）：

    * `limited`：配了至少一个上限（项目档**或**平台档）。未配置 = 不限制。
    * `over`：**确定**已经超了（任意一档，判据见模块 docstring 第 2、3 条）。
    * `used` / `limits`：**项目档**的已用量与上限（token 是整数，金额是十进制字符串）。
    * `platform`：平台档的同一套字段（`used` / `limits` / `over` / `period_label` / ...）。
    * `over_scopes`：超的是哪一档（`["project"]` / `["platform"]` / 两个都有）。
    * `reason`：一句可以直接显示给用户的中文；没超时是空串。

    `platform` 参数用于**批量场景**（消耗面板逐个项目渲染）：平台档跟项目无关，算一次就够，
    逐个项目重算一遍等于把同一份全表查询做 N 次。不传则自己算。

    **这个函数不抛异常。** 任何一步出问题都降级成「不拦 + 一句说明」——
    闸门要是会因为一个意外的脏数据抛异常，那条路径上的分析就再也没人跑得起来了。
    """
    scope = _project_scope(project_id)
    platform_scope = platform if platform is not None else platform_budget_status()
    return _merge_scopes(scope, platform_scope)


def _price_table_for(project_id: int):
    """项目的价格表。读不出来时返回 `(None, 错误)`，**不抛**。"""
    try:
        from services.ai_analysis_service import project_price_table

        return project_price_table(project_id)
    except Exception as exc:  # noqa: BLE001
        return None, (f"读价格表失败：{exc}",)


def _first_reason(runs: Sequence[AiAnalysisRun], table) -> str:
    """第一句能解释「为什么算不出费用」的话（取自 pricing 的 reason）。"""
    for run in runs:
        usage = usage_from_run(run, price_table=table)
        cost = usage.get("cost") or {}
        if cost.get("amount") is None and cost.get("reason"):
            return str(cost["reason"])
    return ""


def budget_gate_reason(project_id: int, *, entry: str = "") -> str | None:
    """超预算就返回一句拦下来的理由；否则返回 `None`。

    `entry` 只用于日志（`commit_manual` / `weekly_manual` / `weekly_background` /
    `weekly_schedule`），便于回答「这次到底是被哪条路径拦的」。
    """
    status = budget_status(project_id)
    if not status.get("blocks_analysis"):
        return None
    reason = str(status.get("reason") or "").strip()
    if not reason:
        reason = "已超出 AI 分析预算，已暂停分析。"
    log_print(
        f"⏸️ AI 分析被预算拦截[{entry or 'unknown'}] project={project_id}: {reason}",
        "AI",
        force=True,
    )
    return reason


def budget_rows_for_overview(project_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """给消耗面板用的批量判定：`{project_id: status}`。

    面板上「已用 / 上限 / 百分比」必须与闸门**用同一份判定**，否则会出现
    「面板显示已超预算，但按钮还能点」（或反过来）。所以这里直接复用
    `budget_status`，不另写一套算法。

    **平台档只算一次**，然后传给每一个项目（`budget_status(..., platform=...)`）：
    平台档要查全表运行记录，逐个项目重算一遍等于把同一份查询做 N 次。项目档之间没有
    共享的部分，仍然各算各的。
    """
    platform = platform_budget_status()
    result: dict[int, dict[str, Any]] = {}
    for project_id in project_ids:
        try:
            result[project_id] = budget_status(project_id, platform=platform)
        except Exception as exc:  # noqa: BLE001 —— 面板不能因为一个项目算不出来就整页报错
            log_print(f"⚠️ AI 预算判定失败（面板）: project={project_id} {exc}", "AI", force=True)
            result[project_id] = _merge_scopes(
                _unlimited("预算判定失败，这一行不做限制"), platform
            )
    return result
