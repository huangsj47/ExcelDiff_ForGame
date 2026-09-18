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

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

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


def _unlimited(reason: str, **extra: Any) -> dict[str, Any]:
    """一张「不拦」的判定结果。任何一处读不出数都用它，见模块 docstring 第 2 条。"""
    payload: dict[str, Any] = {
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


def budget_status(project_id: int) -> dict[str, Any]:
    """这个项目当前的预算状态，以及「要不要拦住下一次分析」。

    返回体的关键字段（界面与闸门都读这几个）：

    * `limited`：配了至少一个上限。未配置 = 不限制。
    * `over`：**确定**已经超了（判据见模块 docstring 第 2、3 条）。
    * `used` / `limits`：已用量与上限（token 是整数，金额是十进制字符串）。
    * `reason`：一句可以直接显示给用户的中文；没超时是空串。

    **这个函数不抛异常。** 任何一步出问题都降级成「不拦 + 一句说明」——
    闸门要是会因为一个意外的脏数据抛异常，那条路径上的分析就再也没人跑得起来了。
    """
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

    base: dict[str, Any] = {
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
        "used": {"tokens": None, "cost": None, "currency": "", "runs": 0},
        "ratios": {"tokens": None, "cost": None},
        "over_limits": [],
        "reason": "",
        "notes": [],
    }

    try:
        all_runs = AiAnalysisRun.query.filter_by(project_id=project_id).all()
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ AI 预算判定查运行记录失败，本次放行: project={project_id} {exc}", "AI", force=True)
        base.update(_unlimited(f"读用量失败（{exc}），本次不按预算拦截", period=period, period_label=label))
        return base

    runs = runs_in_window(all_runs, since, until)
    base["used"]["runs"] = len(runs)

    notes: list[str] = []
    over_limits: list[str] = []
    reasons: list[str] = []

    # --- token 那一档：用**已上报部分的下界** ---
    if token_limit is not None:
        reported = [
            item["tokens"] for item in (usage_from_run(run, price_table=None) for run in runs)
        ]
        partial = 0
        missing = 0
        for bucket in reported:
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
        ratio = partial / token_limit if token_limit else None
        base["ratios"]["tokens"] = ratio
        if partial > token_limit:
            over_limits.append("tokens")
            reasons.append(
                f"{label}的 token 已用 {_fmt_tokens(partial)}，超过上限 {_fmt_tokens(token_limit)}"
            )

    # --- 费用那一档：只要有**任何一次**算不出金额，就不判定 ---
    if cost_limit is not None:
        table, price_errors = _price_table_for(project_id)
        stats = aggregate_runs(runs, price_table=table)
        cost = (stats.get("cost") or {}) if stats else {}
        amount = cost.get("amount")
        currency = str(cost.get("currency") or "") or base["limits"]["currency"]
        if amount is None:
            # 算不出费用 → **不许拦**。这是本模块最要紧的一条行为：价格表没配、
            # 模型名对不上、上游没报 token，都不该变成「分析按钮变灰且说不清原因」。
            if price_errors:
                detail = "价格表不可用：" + "；".join(price_errors)
            else:
                detail = _first_reason(runs, table) or "没有可用价格表或上游未上报 token"
            notes.append(
                f"费用上限已配置，但{label}的金额算不出来（{detail}），因此不按费用拦截"
            )
        else:
            try:
                used = Decimal(str(amount))
            except (InvalidOperation, ValueError):
                used = None
            if used is None:
                notes.append(f"费用合计（{amount}）读不成数字，不按费用拦截")
            else:
                base["used"]["cost"] = money(used)
                base["used"]["currency"] = currency
                base["ratios"]["cost"] = float(used / cost_limit) if cost_limit else None
                if used > cost_limit:
                    over_limits.append("cost")
                    reasons.append(
                        f"{label}的费用已用 {_fmt_money(used, currency)}，"
                        f"超过上限 {_fmt_money(cost_limit, currency)}"
                    )

    base["over_limits"] = over_limits
    base["over"] = bool(over_limits)
    base["blocks_analysis"] = bool(over_limits)
    base["notes"] = notes
    base["reason"] = (
        "；".join(reasons) + "。已暂停 AI 分析，请在项目的「AI 分析配置」里调高预算或等下个周期。"
        if reasons else ""
    )
    return base


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
    """
    result: dict[int, dict[str, Any]] = {}
    for project_id in project_ids:
        try:
            result[project_id] = budget_status(project_id)
        except Exception as exc:  # noqa: BLE001 —— 面板不能因为一个项目算不出来就整页报错
            log_print(f"⚠️ AI 预算判定失败（面板）: project={project_id} {exc}", "AI", force=True)
            result[project_id] = _unlimited("预算判定失败，这一行不做限制")
    return result
