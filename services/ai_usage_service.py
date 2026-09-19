"""AI 用量面板的数据组装：跨项目总览、单项目（含周版本）下钻、单次运行明细。

## 三个端点共用这里的口径

金额与命中率的算法全在 `services/ai/usage.py` 与 `services/ai/pricing.py`，
本模块只做「查哪些行、怎么分组、给界面摆成什么形状」。这样界面上的三个层级
（平台 → 项目 → 周版本 → 单次运行）用的是同一套数，不会出现「总览的数字加起来
不等于下钻的数字」。

## 每个项目的价格表是各自的

单价表存在**项目配置**里（`ai_project_analysis_config.model_price_table`），编辑入口在
本页的「模型单价表」卡片上（写入走同一个配置接口）。所以跨项目总览的合计金额只有在
「每个项目都算得出、且币种一致」时才给 —— 否则给 `None` 加一句理由。混着几种币种
加出来的数字比没有数字更糟。

## 筛选在**服务端**做

项目 / 时间范围 / 触发来源 / 状态四个条件都由服务端解析并落到查询上，不把全量数据
拉到前端再过滤：那样「合计」会随浏览器里的数据量变化，而且权限过滤一旦漏在前端就是
数据泄露。筛选条件经 URL query 传入（刷新、分享、后退都不丢），非法值**回落默认**
而不是 500 —— 一个手改坏的 URL 不该变成一个错误页。

## 预算与筛选是两套时段

筛选里的「本月」决定**表里显示哪些运行**；预算那一列永远按项目自己配置的预算周期
（默认本月）算。两者刻意分开：预算数字必须与 `services/ai/analysis_budget.py` 的
闸门判定**逐字一致**，否则会出现「面板说已超预算，按钮却还能点」这种自相矛盾的界面。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from models import Project, WeeklyVersionConfig
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from services.ai.analysis_budget import (
    PERIOD_ALL_TIME,
    PERIOD_CHOICES,
    PERIOD_LABELS,
    PERIOD_MONTHLY,
    PERIOD_WEEKLY,
    SCOPE_PROJECT,
    as_utc,
    budget_rows_for_overview,
    budget_status,
    platform_budget_status,
    redact_platform_scope,
)
from services.ai.platform_budget import platform_budget_public
from services.ai.pricing import amount_exact, amount_of, money
from services.ai.trace_evidence import decode_evidence
from services.ai.usage import aggregate_runs, usage_from_run
from services.ai.usage_statistics import (
    count_runs_before,
    is_counted,
    statistics_public,
    usage_baseline,
)
from services.ai_analysis_service import project_price_table
from services.app_routing_bootstrap_service import MAX_INTEGER_ID
from utils.timezone_utils import BEIJING_TZ

# 单次运行明细里最多回多少轮。轮次上限由配置决定（默认 8，最大 30），这个数字只是
# 「别再多了」的兜底，避免一条异常数据把界面撑爆。
MAX_ROUNDS = 60

# 下钻页每次最多列多少条运行记录。汇总数字仍然按**全部**运行算 —— 只截断列表，
# 不截断统计，否则页面上的合计会随着「看多少行」变化。
MAX_RUNS = 200

# ---------------------------------------------------------------------------
# 筛选条件
# ---------------------------------------------------------------------------
RANGE_THIS_WEEK = "this_week"
RANGE_THIS_MONTH = "this_month"
RANGE_LAST_30D = "last_30d"
RANGE_ALL = "all"
RANGE_CUSTOM = "custom"
RANGE_CHOICES = (
    RANGE_THIS_WEEK,
    RANGE_THIS_MONTH,
    RANGE_LAST_30D,
    RANGE_CUSTOM,
    RANGE_ALL,
)
RANGE_LABELS = {
    RANGE_THIS_WEEK: "本周",
    RANGE_THIS_MONTH: "本月",
    RANGE_LAST_30D: "近 30 天",
    RANGE_CUSTOM: "自定义",
    RANGE_ALL: "全部",
}
DEFAULT_RANGE = RANGE_ALL

SOURCE_ALL = "all"
SOURCE_MANUAL = "manual"
SOURCE_SCHEDULED = "scheduled"
SOURCE_CHOICES = (SOURCE_ALL, SOURCE_MANUAL, SOURCE_SCHEDULED)
SOURCE_LABELS = {SOURCE_ALL: "全部来源", SOURCE_MANUAL: "手动", SOURCE_SCHEDULED: "定时"}

STATUS_ALL = "all"
STATUS_CHOICES = (STATUS_ALL, "succeeded", "failed", "running")
STATUS_LABELS = {
    STATUS_ALL: "全部状态",
    "succeeded": "成功",
    "failed": "失败",
    "running": "进行中",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class UsageFilters:
    """一次查询的筛选条件。**所有字段都已经过校验**，非法值在解析时就回落掉了。"""

    project_id: Optional[int] = None
    range_key: str = DEFAULT_RANGE
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    source: str = SOURCE_ALL
    status: str = STATUS_ALL
    notes: tuple[str, ...] = field(default=())

    @property
    def active(self) -> bool:
        """有没有偏离默认值。「清空筛选」按钮据此决定是否可点。"""
        return (
            self.project_id is not None
            or self.range_key != DEFAULT_RANGE
            or self.source != SOURCE_ALL
            or self.status != STATUS_ALL
        )

    def as_query(self) -> dict[str, str]:
        """规范化后的 query。前端据此改写地址栏 —— 只带非默认值，URL 才短。"""
        query: dict[str, str] = {}
        if self.project_id is not None:
            query["project"] = str(self.project_id)
        if self.range_key != DEFAULT_RANGE:
            query["range"] = self.range_key
        if self.range_key == RANGE_CUSTOM:
            if self.date_from:
                query["from"] = self.date_from.isoformat()
            if self.date_to:
                query["to"] = self.date_to.isoformat()
        if self.source != SOURCE_ALL:
            query["source"] = self.source
        if self.status != STATUS_ALL:
            query["status"] = self.status
        return query

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "range": self.range_key,
            "range_label": RANGE_LABELS.get(self.range_key, RANGE_LABELS[DEFAULT_RANGE]),
            "from": self.date_from.isoformat() if self.date_from else None,
            "to": self.date_to.isoformat() if self.date_to else None,
            "source": self.source,
            "status": self.status,
            "active": self.active,
            "notes": list(self.notes),
        }


def _first_arg(args: Mapping[str, Any], *names: str) -> str:
    for name in names:
        if name in args:
            value = args.get(name)
            if value is None:
                continue
            return str(value).strip()
    return ""


def _parse_int(raw: str) -> Optional[int]:
    """把查询参数里的整数读出来。**任何非法值都回落 `None`（= 不筛），绝不抛异常。**

    上界不是洁癖：工具把值直接绑给 pysqlite 时，超过 64 位有符号数的整数会抛
    `OverflowError: Python int too large to convert to SQLite INTEGER`，而那不是
    `SQLAlchemyError`、仓库里也没有兜它的处理器 —— 管理员把地址栏里的项目号改错一位
    就白屏。超出这个范围的 id 不可能存在，按「不筛」处理与按「筛不到」处理在这里等价，
    都能给出一个正常页面。
    """
    if not raw or raw.lower() in {"all", "none", "0"}:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= MAX_INTEGER_ID else None


def _parse_date(raw: str) -> Optional[date]:
    if not DATE_RE.match(raw or ""):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def parse_usage_filters(args: Mapping[str, Any]) -> UsageFilters:
    """把 URL query 解析成筛选条件。**任何非法值都回落默认，绝不抛异常。**

    这是刻意的：这些参数从地址栏来，用户会手改、会分享被截断的链接、会收藏一个
    半年前的范围。让它们变成 500 的话，页面直接白屏，而用户没有任何办法回到正常状态
    （筛选状态在 URL 里，改不回去就永远打不开）。回落的每一处都记进 `notes`，
    由界面如实显示出来 —— 悄悄换掉用户填的条件，和报错一样让人摸不着头脑。
    """
    notes: list[str] = []

    project_id = _parse_int(_first_arg(args, "project", "project_id"))
    if _first_arg(args, "project", "project_id") and project_id is None:
        notes.append("项目筛选值不是有效编号，已按「全部项目」显示。")

    raw_range = _first_arg(args, "range").lower()
    range_key = raw_range if raw_range in RANGE_CHOICES else DEFAULT_RANGE
    if raw_range and raw_range not in RANGE_CHOICES:
        notes.append(f"时间范围「{raw_range}」认不出来，已按「{RANGE_LABELS[DEFAULT_RANGE]}」显示。")

    raw_from = _first_arg(args, "from", "date_from", "start")
    raw_to = _first_arg(args, "to", "date_to", "end")
    date_from = _parse_date(raw_from)
    date_to = _parse_date(raw_to)
    if raw_from and date_from is None:
        notes.append(f"起始日期「{raw_from}」不是 YYYY-MM-DD，已忽略。")
    if raw_to and date_to is None:
        notes.append(f"结束日期「{raw_to}」不是 YYYY-MM-DD，已忽略。")

    if range_key == RANGE_CUSTOM:
        if date_from is None and date_to is None:
            # 选了「自定义」却没给日期：退回默认范围，并在界面上说清楚。
            range_key = DEFAULT_RANGE
            notes.append("自定义范围缺少起止日期，已按「全部」显示。")
        elif date_from and date_to and date_from > date_to:
            # 起止写反了。**不静默对调**：对调之后界面上输入的还是反的，
            # 显示的结果却是正的，用户没法从界面上看出发生了什么。
            range_key = DEFAULT_RANGE
            date_from = date_to = None
            notes.append("起止日期反了，已按「全部」显示。")

    raw_source = _first_arg(args, "source", "trigger").lower()
    source = raw_source if raw_source in SOURCE_CHOICES else SOURCE_ALL
    if raw_source and raw_source not in SOURCE_CHOICES:
        notes.append(f"触发来源「{raw_source}」认不出来，已按「全部来源」显示。")

    raw_status = _first_arg(args, "status").lower()
    status = raw_status if raw_status in STATUS_CHOICES else STATUS_ALL
    if raw_status and raw_status not in STATUS_CHOICES:
        notes.append(f"状态「{raw_status}」认不出来，已按「全部状态」显示。")

    return UsageFilters(
        project_id=project_id,
        range_key=range_key,
        date_from=date_from,
        date_to=date_to,
        source=source,
        status=status,
        notes=tuple(notes),
    )


def resolve_window(
    filters: UsageFilters, *, now: Optional[datetime] = None
) -> tuple[Optional[datetime], Optional[datetime]]:
    """筛选条件 → `[since, until)`（UTC）。`None` 表示这一头不设限。

    全部按**北京时间**的日历切分（用户看的日历是北京时间），再换算成 UTC 与库里的
    `created_at` 同口径比较。边界统一用**左闭右开**：`until` 是「结束日期的次日 00:00」，
    而不是「结束日期 23:59:59」—— 后者会把那一秒里的记录漏掉，而且只在特定时刻复现。
    """
    moment = as_utc(now) or datetime.now(timezone.utc)
    beijing = moment.astimezone(BEIJING_TZ)
    key = filters.range_key

    if key == RANGE_THIS_WEEK:
        start = (beijing - timedelta(days=beijing.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start.astimezone(timezone.utc), None
    if key == RANGE_THIS_MONTH:
        start = beijing.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc), None
    if key == RANGE_LAST_30D:
        return moment - timedelta(days=30), None
    if key == RANGE_CUSTOM and (filters.date_from or filters.date_to):
        since = (
            datetime.combine(filters.date_from, time.min, tzinfo=BEIJING_TZ).astimezone(timezone.utc)
            if filters.date_from
            else None
        )
        until = (
            datetime.combine(
                filters.date_to + timedelta(days=1), time.min, tzinfo=BEIJING_TZ
            ).astimezone(timezone.utc)
            if filters.date_to
            else None
        )
        return since, until
    return None, None


def run_matches(
    run: AiAnalysisRun,
    filters: UsageFilters,
    window: tuple[Optional[datetime], Optional[datetime]],
) -> bool:
    """一条运行是否符合筛选。时间比较在 Python 侧做，理由见 `analysis_budget.runs_in_window`。"""
    since, until = window
    stamp = as_utc(getattr(run, "created_at", None))
    if since is not None or until is not None:
        # 没有时间的记录不进任何时间窗；选「全部」时它照样显示。
        if stamp is None:
            return False
        if since is not None and stamp < since:
            return False
        if until is not None and stamp >= until:
            return False

    if filters.source == SOURCE_SCHEDULED:
        if str(getattr(run, "trigger_source", "") or "") != "scheduled":
            return False
    elif filters.source == SOURCE_MANUAL:
        # 「手动」= 不是定时。`trigger_source` 的值有一部分来自 URL 参数
        # （`/ai-analysis/weekly/<id>/stream?source=...`），所以不能写成
        # `== 'manual'`：那样一个拼错的 source 会让这次运行在任何筛选下都看不见。
        if str(getattr(run, "trigger_source", "") or "") == "scheduled":
            return False

    if filters.status != STATUS_ALL:
        if str(getattr(run, "effective_status", "") or "") != filters.status:
            return False
    return True


def filter_runs(
    runs: Iterable[AiAnalysisRun],
    filters: UsageFilters,
    *,
    now: Optional[datetime] = None,
    baseline: Optional[datetime] = None,
) -> list[AiAnalysisRun]:
    """按筛选条件过滤运行记录（保持传入顺序）。

    `baseline` 是**统计起点**（`services/ai/usage_statistics`）：起点之前的运行不进任何统计，
    面板的合计、各项目行、逐次明细全都是这一批运行算出来的，所以「上面写 5 次、下面列 30 条」
    这种对不上在这条路上不会出现。

    **它只作用于这里** —— 这条读路径只服务消耗面板。预算闸门走的是另一条
    （`analysis_budget.runs_in_window`），起点接不进去，也不许接（见 usage_statistics 的说明）。
    """
    window = resolve_window(filters, now=now)
    return [
        run
        for run in runs
        if run_matches(run, filters, window) and is_counted(run, baseline)
    ]


def _price_block(table, errors: Sequence[str]) -> dict[str, Any]:
    """这一层用的价格表状态。界面据此决定「显示费用」还是「提示去配置」。"""
    return {
        "version": table.version if table else "",
        "currency": table.currency if table else "",
        "source": table.source if table else "",
        "configured": bool(table and table.models),
        "errors": list(errors or ()),
    }


def _budget_block(
    project_id: int,
    status: Mapping[str, Any] | None = None,
    *,
    range_key: str = DEFAULT_RANGE,
    show_platform: bool = True,
) -> dict[str, Any]:
    """面板上的预算一格。

    **直接复用闸门那份判定**（`analysis_budget.budget_status`），不在这里另算一遍：
    两套算法的必然结果是某天开始互相矛盾 ——「面板说已超预算，按钮却还能点」，
    而两个数字看上去都很正常。

    `range_key` 只用于**说明口径**（见 `period_alignment`）：预算那一列永远按各项目
    自己的预算周期统计，与上面的筛选范围是两回事。把这件事写在行上，是因为「同一屏上
    出现两个『本月』」正是最容易读错的地方。
    """
    data = status if status is not None else budget_status(project_id)
    if not show_platform:
        # 平台合计只对平台管理员开放。**判定已经在上面算完了**，这里只抹数字：
        # `over` / `blocks_analysis` / `over_scopes` 一个字不改 —— 拦不拦与谁在看无关。
        data = redact_platform_scope(data)
    period = str(data.get("period") or "")
    platform = data.get("platform") or {}
    over_scopes = list(data.get("over_scopes") or ())
    return {
        "limited": bool(data.get("limited")),
        "over": bool(data.get("over")),
        "period": period,
        "period_label": str(
            data.get("period_label") or PERIOD_LABELS.get(period, "")
        ),
        "since": data.get("since"),
        "until": data.get("until"),
        "limits": data.get("limits") or {"tokens": None, "cost": None, "currency": ""},
        "used": data.get("used") or {"tokens": None, "cost": None, "currency": "", "runs": 0},
        "ratios": data.get("ratios") or {"tokens": None, "cost": None},
        "over_limits": list(data.get("over_limits") or ()),
        "reason": str(data.get("reason") or ""),
        "notes": list(data.get("notes") or ()),
        # 平台档：与项目档并列显示。没配平台总预算时 `limited=False`，界面据此不渲染。
        "platform": {
            # 无权看平台合计时为真：数字全空，界面据此换成一句说明（**不能画成 0** ——
            # 0 会被读成「一点都没用」，而事实是「你不知道」）。
            "hidden": bool(platform.get("hidden")),
            "limited": bool(platform.get("limited")),
            "over": bool(platform.get("over")),
            "period": str(platform.get("period") or ""),
            "period_label": str(
                platform.get("period_label")
                or PERIOD_LABELS.get(str(platform.get("period") or ""), "")
            ),
            "limits": platform.get("limits") or {"tokens": None, "cost": None, "currency": ""},
            "used": platform.get("used")
            or {"tokens": None, "cost": None, "currency": "", "runs": 0},
            "ratios": platform.get("ratios") or {"tokens": None, "cost": None},
            "over_limits": list(platform.get("over_limits") or ()),
        },
        # 超的是哪一档 —— 界面据此说清「是项目超了还是平台超了」。两档都超时两处都要说。
        "over_scopes": over_scopes,
        "align_range": budget_period_range(period),
        "matches_filter": budget_period_range(period) == range_key,
    }


# 预算周期 → 筛选里的「时间范围」。**这张表是联动的地基**：
# 预算说「本月」而筛选说「近 30 天」时，两列数字不是一回事，界面必须能说清、
# 并给出「一键切成同口径」的动作。没有对应关系的两个范围（近 30 天 / 自定义）
# 一律映射成 `None` —— 它们与任何预算周期都不同口径，这一点不能含糊。
PERIOD_TO_RANGE = {
    PERIOD_MONTHLY: RANGE_THIS_MONTH,
    PERIOD_WEEKLY: RANGE_THIS_WEEK,
    PERIOD_ALL_TIME: RANGE_ALL,
}
RANGE_TO_PERIOD = {value: key for key, value in PERIOD_TO_RANGE.items()}


def budget_period_range(period: str) -> Optional[str]:
    """预算周期对应哪个筛选范围；对不上（或认不出）返回 `None`。"""
    return PERIOD_TO_RANGE.get(str(period or "").strip().lower())


def period_alignment(
    filters: UsageFilters,
    periods: Sequence[str],
    *,
    platform_period: str = "",
) -> dict[str, Any]:
    """筛选范围与预算周期是不是同一个口径，以及「要同口径该切成哪个范围」。

    为什么要有这一层：面板上「时间范围：本月」管的是**表里列出哪些运行**，而「本期预算」
    那一列永远按**各项目自己配置的预算周期**统计（这是刻意的 —— 它必须与闸门判定逐字
    一致，否则会出现「面板说超了、按钮还能点」）。两个「本月」在同一屏上含义不同，
    读的人几乎一定会把它们当成一回事。所以：

    * 口径一致时明说一致；
    * 不一致时给出**一键对齐**的目标范围（`target_range`），而不是让用户自己去猜该选哪个；
    * 各项目周期不一致时 `mixed=True`、`target_range=None` —— 这时**没有**能一次对齐的
      选项，硬选一个（比如取最常见的）会让另外几个项目的数字继续不同口径，而界面上
      看起来却「已经对齐了」。
    """
    counts: dict[str, int] = {}
    for period in periods:
        key = str(period or "").strip().lower()
        if key:
            counts[key] = counts.get(key, 0) + 1
    if platform_period:
        key = str(platform_period).strip().lower()
        if key:
            counts[key] = counts.get(key, 0) + 1

    entries = [
        {"period": key, "label": PERIOD_LABELS.get(key, key), "count": count}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    mixed = len(counts) > 1
    target_range: Optional[str] = None
    if len(counts) == 1:
        target_range = budget_period_range(next(iter(counts)))
    aligned = target_range is not None and target_range == filters.range_key

    current_label = RANGE_LABELS.get(filters.range_key, RANGE_LABELS[DEFAULT_RANGE])
    if not counts:
        note = "这一屏里没有可判定的预算周期（没有任何项目配置了预算）。"
    elif mixed:
        detail = " / ".join(f"{item['label']}（{item['count']} 个）" for item in entries)
        note = (
            f"预算周期不统一（{detail}），没有能一次对齐的筛选范围；"
            "下面的「本期预算」逐行按各自周期统计。"
        )
    elif aligned:
        note = f"筛选范围（{current_label}）与预算周期一致，本页「已用」与筛选范围同口径。"
    else:
        label = entries[0]["label"]
        note = (
            f"筛选范围是「{current_label}」，而预算周期是「{label}」—— "
            "预算那一列永远按各项目自己的预算周期统计，与上面的筛选范围是两回事。"
        )
    return {
        "filter_range": filters.range_key,
        "filter_label": current_label,
        "periods": entries,
        "mixed": mixed,
        "aligned": aligned,
        "target_range": target_range,
        "target_label": RANGE_LABELS.get(target_range, "") if target_range else "",
        "note": note,
    }


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _runs_query(project_ids: Optional[Iterable[int]]):
    query = AiAnalysisRun.query
    if project_ids is not None:
        ids = list(project_ids)
        if not ids:
            return None
        query = query.filter(AiAnalysisRun.project_id.in_(ids))
    return query


def _totals_cost(entries: Sequence[dict]) -> Optional[dict[str, Any]]:
    """跨项目的费用合计。

    **只有每个项目都算得出、且币种一致时才给数字。** 少算一个项目、或把两种币种直接
    相加，得到的都是一个看起来正常、实际错的金额 —— 这比「算不出」危险得多。
    """
    costs = [entry.get("cost") for entry in entries]
    if not costs or any(amount_of(item) is None for item in costs):
        return None
    currencies = {str(item.get("currency") or "") for item in costs}
    if len(currencies) != 1:
        return None
    # 读**精确**金额而不是展示串：`amount` 在不足一分时写的是 `<0.01`，解析会抛
    # `InvalidOperation`（把整个总览打成 500），而拿已量化到分的值相加还会少算。
    total = sum((amount_of(item) for item in costs), Decimal(0))
    return {
        "amount": money(total),
        "amount_exact": amount_exact(total),
        "currency": currencies.pop(),
        "reason": "",
        "notes": ["按各项目自己的价格表估算后相加"],
        "lines": [],
    }


def _over_budget_project_count(
    accessible_project_ids: Optional[Iterable[int]], *, show_platform: bool
) -> int:
    """**项目档**已超预算、分析已被暂停的项目数。与当前筛选无关。

    卡片的口气是全局事实（「这些项目的 AI 分析已被暂停（手动与定时都停）」），而原先
    这个数是从 `entries` 里数出来的 —— `entries` 是**这一屏筛选之后**的结果（时间范围 /
    项目 / 来源 / 状态，再叠一道统计起点）。两个方向都会错：

    * 项目 A 本月超了预算 → 闸门停掉它的手动与定时分析 → 它**不再产生运行记录** →
      用户把范围改成「本周」时 A 根本不在这一屏里，KPI 显示 0 个，而 A 此刻确实是停着的
      —— 这条信息只能靠**特意去筛**才看得见，正好和它该起的作用相反；
    * 平台总预算一超，`over` 对**每一行**都为真（`over_scopes` 里是 `platform`），
      于是卡片报的其实是「这一屏有几个项目」，与「哪个项目被停了」不是一回事。

    所以这里按**权限范围内的全部项目**独立算一遍（`accessible_project_ids=None` =
    不限制，即能看全部），并且只数 `over_scopes` 里含 `project` 的那些 —— 后者才是
    「这个项目自己被停了」的判据。

    口径仍然与闸门同源：判定走 `budget_rows_for_overview` → `budget_status`，
    不另写一套算法（面板与闸门互相矛盾是最坏的一种不一致）。`show_platform=False` 时
    每一行的平台档数字被抹掉，而 `over_scopes` 一个字不改 —— 判定不受权限影响。

    代价是逐项目一次运行查询（项目数量级）。一个会随筛选变化的「有几个项目被停了」
    比没有这个数字更糟，所以这笔开销值得付。
    """
    if accessible_project_ids is None:
        project_ids = [row.id for row in Project.query.with_entities(Project.id).all()]
    else:
        project_ids = sorted({int(item) for item in accessible_project_ids})
    if not project_ids:
        return 0
    rows = budget_rows_for_overview(project_ids, show_platform=show_platform)
    return sum(
        1
        for status in rows.values()
        if SCOPE_PROJECT in (status.get("over_scopes") or ())
    )


def usage_overview(
    accessible_project_ids: Optional[Iterable[int]] = None,
    filters: Optional[UsageFilters] = None,
    *,
    show_platform: bool = True,
) -> dict[str, Any]:
    """跨项目总览。`accessible_project_ids=None` 表示全部（平台管理员）。

    权限过滤在这里做**查询级**过滤（`project_id IN (...)`），而不是查完再筛：
    后者一旦哪天有人漏了那一步，就是「无权用户看到别人的消耗」。

    `filters` 里的项目筛选**只能收窄**权限范围，不能扩大：它先与可访问清单求交，
    求不到就是空结果，不会因为 URL 里写了个别人的项目号而读到别人的数。
    """
    active = filters or UsageFilters()
    query = _runs_query(accessible_project_ids)
    entries: list[dict[str, Any]] = []
    all_runs: list[AiAnalysisRun] = []
    # 统计起点（平台级口径，见 services/ai/usage_statistics）。**它只在这里生效**：
    # 预算那一档的「已用」走 `platform_budget_status()`，与这个变量无关。
    baseline = usage_baseline()
    excluded_runs = 0
    allowed: Optional[set[int]] = (
        None if accessible_project_ids is None else set(accessible_project_ids)
    )

    if query is not None:
        if active.project_id is not None:
            if allowed is not None and active.project_id not in allowed:
                # 无权访问 / 不存在的项目：给空结果，而不是 403 —— 这是一个筛选条件，
                # 不是一次越权访问；而且页面要能正常渲染出「没有匹配的数据」。
                query = None
            else:
                query = query.filter(AiAnalysisRun.project_id == active.project_id)

    if query is not None:
        # 先按筛选条件过一遍，再按起点过一遍 —— 两步分开只为一件事：让「这一屏被起点排除了
        # 多少条」数得准。直接在筛选后的结果里数，筛选到上个月时那个数就与眼前这一屏无关了。
        matched_runs = filter_runs(query.order_by(AiAnalysisRun.created_at.desc()).all(), active)
        excluded_runs = count_runs_before(baseline, matched_runs)
        all_runs = [run for run in matched_runs if is_counted(run, baseline)]
        grouped: dict[int, list[AiAnalysisRun]] = {}
        for run in all_runs:
            grouped.setdefault(run.project_id, []).append(run)

        names = {
            project.id: (project.name or project.code or f"项目 {project.id}")
            for project in Project.query.filter(Project.id.in_(list(grouped))).all()
        }
        budgets = budget_rows_for_overview(list(grouped), show_platform=show_platform)
        for project_id, runs in grouped.items():
            table, errors = project_price_table(project_id)
            stats = aggregate_runs(runs, price_table=table)
            budget_block = _budget_block(
                project_id,
                budgets.get(project_id),
                range_key=active.range_key,
                show_platform=show_platform,
            )
            entries.append(
                {
                    "project_id": project_id,
                    "name": names.get(project_id, f"项目 {project_id}"),
                    "runs": stats["runs"],
                    "collected_runs": stats["collected_runs"],
                    "tokens": stats["tokens"],
                    "cache": stats["cache"],
                    "tools": stats["tools"],
                    "missing_runs": stats["missing_runs"],
                    "cost": stats["cost"],
                    "pricing": _price_block(table, errors),
                    "budget": budget_block,
                    "last_run_at": _iso(
                        max((run.created_at for run in runs if run.created_at), default=None)
                    ),
                }
            )

    entries.sort(key=lambda item: item["runs"], reverse=True)

    # 无权看平台合计时**连查都不查**：查出来再丢，等于把「不小心又发出去」的机会留在
    # 代码里（这一屏的数据全部由这个函数产出，少一次查询就少一处泄漏面）。
    platform_config = platform_budget_public() if show_platform else {}
    platform_status = platform_budget_status() if show_platform else {}
    # 联动只看**真的配了上限**的项目：一个没配预算的项目也有个默认周期（本月），
    # 把它算进来的话，界面会对一批根本没设预算的项目宣称「与预算周期一致」。
    linked_periods = [
        str(item["budget"].get("period") or "")
        for item in entries
        if item["budget"].get("limited")
    ]
    budget_link = period_alignment(
        active,
        linked_periods,
        platform_period=(
            str(platform_config.get("period") or "")
            if show_platform and platform_config.get("configured")
            else ""
        ),
    )

    totals = {"runs": 0, "tokens": {}, "cache": {}, "tools": {}, "cost": None}
    if all_runs:
        # 合计不带价格表算（各项目的表不同），费用另算 —— 见 _totals_cost。
        stats = aggregate_runs(all_runs, price_table=None)
        totals = {
            "runs": stats["runs"],
            "collected_runs": stats["collected_runs"],
            "tokens": stats["tokens"],
            "cache": stats["cache"],
            "tools": stats["tools"],
            "missing_runs": stats["missing_runs"],
            "cost": _totals_cost(entries),
        }

    return {
        "success": True,
        "projects": entries,
        "totals": totals,
        "filters": active.to_dict(),
        "filter_options": filter_options(),
        "project_options": _project_options(accessible_project_ids),
        "over_budget_projects": _over_budget_project_count(
            accessible_project_ids, show_platform=show_platform
        ),
        # 预算这一屏的两件事：平台总预算（配置 + 当前状态）与「筛选范围 vs 预算周期」的
        # 口径联动。两者都是**只读**的判定，写侧在 `/ai-analysis/usage/budget`。
        #
        # **平台合计只对平台管理员开放**：它把所有人的花费加起来，能从中反推出别的项目
        # 烧了多少钱。没权限时 `platform_budget` / `platform_status` 是 `null` ——
        # 不是空对象：界面要能区分「没配预算」（说「还没有配置（当前不限制）」）与
        # 「你看不到」（说「只有平台管理员可见」）。`platform_hidden` 就是干这个的。
        "platform_hidden": not show_platform,
        "platform_budget": platform_config if show_platform else None,
        "platform_status": None if not show_platform else {
            "limited": bool(platform_status.get("limited")),
            "over": bool(platform_status.get("over")),
            "period": str(platform_status.get("period") or ""),
            "period_label": str(platform_status.get("period_label") or ""),
            "limits": platform_status.get("limits")
            or {"tokens": None, "cost": None, "currency": ""},
            "used": platform_status.get("used")
            or {"tokens": None, "cost": None, "currency": "", "runs": 0},
            "ratios": platform_status.get("ratios") or {"tokens": None, "cost": None},
            "over_limits": list(platform_status.get("over_limits") or ()),
            "reason": str(platform_status.get("reason") or ""),
            "notes": list(platform_status.get("notes") or ()),
        },
        "budget_link": budget_link,
        # 统计口径：起点是什么、「这一屏」被它排除了多少条、以及**能不能改**。
        # 状态一栏对所有人下发（数字是怎么来的必须看得见），改的权限只看 `can_manage`
        # —— 起点是平台级口径，所以只有平台管理员能动（见路由的 `@require_admin`）。
        "statistics": statistics_public(
            can_manage=show_platform, excluded_runs=excluded_runs
        ),
        # 预算周期选项表**随接口下发**：事实源是服务端的 `PERIOD_CHOICES` / `PERIOD_LABELS`，
        # 界面照它渲染下拉即可。原先页面自带一份同内容的表（`AIU_PERIOD_LABELS`），
        # 靠一条测试钉住「两边逐字一致」—— 那等于把「服务端加一个周期」变成一次
        # 「改了服务端还要记得改前端，忘了就红」的联动。这一份仍是回落：老页面/缓存
        # 里的前端拿不到字段时照旧用自带的表。
        "periods": [
            {"value": key, "label": PERIOD_LABELS.get(key, key)} for key in PERIOD_CHOICES
        ],
        "generated_at": _iso(datetime.now(timezone.utc)),
    }


def _project_options(accessible_project_ids: Optional[Iterable[int]]) -> list[dict[str, Any]]:
    """筛选下拉用的项目清单。**永远是全量（按权限），不受当前筛选影响。**

    不能拿结果里的 `projects` 当下拉选项：一旦按项目筛过，那份清单就只剩一个项目，
    下拉里别的选项会消失 —— 用户换不回「全部项目」，只能改地址栏。
    """
    if accessible_project_ids is None:
        rows = Project.query.order_by(Project.id.asc()).all()
    else:
        ids = list(accessible_project_ids)
        if not ids:
            return []
        rows = Project.query.filter(Project.id.in_(ids)).order_by(Project.id.asc()).all()
    return [
        {"project_id": project.id, "name": project.name or project.code or f"项目 {project.id}"}
        for project in rows
    ]


def filter_options() -> dict[str, Any]:
    """筛选下拉的选项。由服务端下发而不是写死在模板里 —— 选项与解析规则必须同源，
    否则会出现「界面上有个选项、后端认不出来（回落默认）」，用户选了却没生效。"""
    return {
        "ranges": [
            {"value": key, "label": RANGE_LABELS[key]}
            for key in (RANGE_THIS_WEEK, RANGE_THIS_MONTH, RANGE_LAST_30D, RANGE_ALL, RANGE_CUSTOM)
        ],
        "sources": [
            {"value": key, "label": SOURCE_LABELS[key]} for key in SOURCE_CHOICES
        ],
        "statuses": [
            {"value": key, "label": STATUS_LABELS[key]} for key in STATUS_CHOICES
        ],
        "defaults": {"range": DEFAULT_RANGE, "source": SOURCE_ALL, "status": STATUS_ALL},
    }


def project_usage(
    project_id: int,
    filters: Optional[UsageFilters] = None,
    *,
    show_platform: bool = True,
) -> dict[str, Any]:
    """单项目下钻：周版本维度 + 逐次运行 + 按工具类型。

    「当前周版本」取**最近分析过**的那个 group_key（`is_latest=True`）—— 界面上写的是
    「最近分析的周版本」，不叫「当前周版本」：平台无法从分析记录反推出用户心里那个
    「当前」，用词必须与事实一致。

    筛选同样在服务端做：`totals`、`weekly_versions` 与 `runs` 三者用的是**同一批**
    过滤后的运行，不会出现「合计按全部算、明细按筛选列」这种对不上的情形。
    """
    active = filters or UsageFilters()
    table, errors = project_price_table(project_id)
    budget_block = _budget_block(
        project_id, range_key=active.range_key, show_platform=show_platform
    )
    all_runs = (
        AiAnalysisRun.query.filter_by(project_id=project_id)
        .order_by(AiAnalysisRun.created_at.desc())
        .all()
    )
    # 与总览同一条口径：起点之前的运行在这个项目的下钻里也不出现（合计、周版本维度、
    # 逐次明细三者用的是同一批运行，不会出现「合计少算、明细照列」）。
    runs = filter_runs(all_runs, active, baseline=usage_baseline())
    stats = aggregate_runs(runs, price_table=table)
    pricing = _price_block(table, errors)

    weekly_groups: dict[str, list[AiAnalysisRun]] = {}
    for run in runs:
        if run.target_type == "weekly" and run.target_key:
            weekly_groups.setdefault(run.target_key, []).append(run)

    config_ids = {
        group[0].target_id for group in weekly_groups.values() if group and group[0].target_id
    }
    config_names: dict[int, str] = {}
    if config_ids:
        config_names = {
            config.id: (config.name or f"周版本配置 {config.id}")
            for config in WeeklyVersionConfig.query.filter(
                WeeklyVersionConfig.id.in_(list(config_ids))
            ).all()
        }

    weekly_versions = []
    for group_key, group in weekly_groups.items():
        group_stats = aggregate_runs(group, price_table=table)
        config_id = group[0].target_id
        weekly_versions.append(
            {
                "group_key": group_key,
                "config_id": config_id,
                "config_name": config_names.get(config_id, "") if config_id else "",
                "runs": group_stats["runs"],
                "collected_runs": group_stats["collected_runs"],
                "tokens": group_stats["tokens"],
                "cache": group_stats["cache"],
                "tools": group_stats["tools"],
                "missing_runs": group_stats["missing_runs"],
                "cost": group_stats["cost"],
                "last_run_at": _iso(
                    max((run.created_at for run in group if run.created_at), default=None)
                ),
                "is_latest": False,  # 排序后按时间重新标记，见下面几行
            }
        )
    # 按「最近一次分析时间」倒序，「最近分析过的周版本」排在最前面并被标成 is_latest。
    weekly_versions.sort(key=lambda item: item["last_run_at"] or "", reverse=True)
    for index, item in enumerate(weekly_versions):
        item["is_latest"] = index == 0

    return {
        "success": True,
        "project_id": project_id,
        "totals": {
            "runs": stats["runs"],
            "collected_runs": stats["collected_runs"],
            "tokens": stats["tokens"],
            "cache": stats["cache"],
            "tools": stats["tools"],
            "missing_runs": stats["missing_runs"],
            "cost": stats["cost"],
        },
        "weekly_versions": weekly_versions,
        "runs": [_run_row(run, table) for run in runs[:MAX_RUNS]],
        "runs_truncated": len(runs) > MAX_RUNS,
        "pricing": pricing,
        "budget": budget_block,
        "filters": active.to_dict(),
        # 下钻页只有一个项目，联动目标就是它自己的预算周期（配了的话）。
        "budget_link": period_alignment(
            active,
            [str(budget_block["period"] or "")] if budget_block["limited"] else [],
        ),
        "generated_at": _iso(datetime.now(timezone.utc)),
    }


def _int_or_zero(value: Any) -> int:
    """脏值读成 0（**绝不抛**）。`int("很多")` 会让整页 500，而这里只是几个计数。"""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _subagent_rows(run: AiAnalysisRun) -> list[dict[str, Any]]:
    """这次运行里各分片代理的账（`response_payload["subagents"]`）。

    **读不出来就给空列表，绝不编造一条**：面板据此区分「这次不是分片跑的」与「跑了但
    没记」—— 两者都不是「一个成员都没跑」。老运行（这个功能之前）走的就是空列表这条。
    """
    payload = _json_object(getattr(run, "response_payload", None))
    rows = payload.get("subagents")
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        dimensions = item.get("dimensions")
        result.append(
            {
                "label": str(item.get("label") or ""),
                "role": str(item.get("role") or ""),
                "dimensions": [str(name) for name in dimensions]
                if isinstance(dimensions, list)
                else [],
                "status": str(item.get("status") or ""),
                # `int(...)` 要兜住非数值串：payload 是模型/引擎写进去的 JSON，
                # 一行 `"rounds": "很多"` 就能让 `/runs/<id>/usage` 抛 ValueError —— 而那个
                # 路由没有 try，整页 500。脏值读成 0 而不是「未上报」是刻意的：这里数的是
                # 「跑了几轮」，读不出来就是没跑成，与 token 那种「上游没报」不是一回事。
                "rounds": _int_or_zero(item.get("rounds")),
                "requests": _int_or_zero(item.get("requests")),
                # `None` 原样带出去（上游没上报），**不是 0** —— 与整页的口径一致。
                "tokens_input": item.get("tokens_input"),
                "tokens_output": item.get("tokens_output"),
                "cache_read_tokens": item.get("cache_read_tokens"),
                "cache_write_tokens": item.get("cache_write_tokens"),
                "anomalies": _int_or_zero(item.get("anomalies")),
                "report_chars": _int_or_zero(item.get("report_chars")),
                "skipped_reason": str(item.get("skipped_reason") or ""),
                "error": str(item.get("error") or ""),
            }
        )
    return result


def _json_object(raw: Any) -> dict[str, Any]:
    """把库里那列 JSON 文本读成 dict。读不出来给 `{}`（老行/坏行不该让整页炸掉）。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _run_row(run: AiAnalysisRun, table) -> dict[str, Any]:
    usage = usage_from_run(run, price_table=table)
    return {
        "run_id": run.id,
        "target_type": run.target_type,
        "target_id": run.target_id,
        "target_key": run.target_key,
        "status": run.effective_status,
        "scope": run.scope,
        "trigger_source": run.trigger_source,
        "model": run.model or "",
        "created_at": _iso(run.created_at),
        "finished_at": _iso(run.finished_at),
        "usage": usage,
    }


def run_usage(run_id: int) -> Optional[dict[str, Any]]:
    """单次运行的明细：token 三段、命中率、费用、逐轮、按工具类型。

    权限判定在路由层按**这条运行自己的 `project_id`** 做 —— 不能只信 URL 里的项目号，
    否则换个项目号就能读到别人的运行明细。
    """
    run = AiAnalysisRun.query.filter_by(id=run_id).first()
    if run is None:
        return None

    table, errors = project_price_table(run.project_id)
    rounds = (
        AiAnalysisTrace.query.filter_by(run_id=run.id)
        .order_by(AiAnalysisTrace.round_index.asc())
        .limit(MAX_ROUNDS)
        .all()
    )
    return {
        "success": True,
        "run": {
            **_run_row(run, table),
            "rounds_used": run.rounds_used,
            "tool_requests_used": run.tool_requests_used,
            "anomalies_found": run.anomalies_found,
            "dropped_count": run.dropped_count,
            "error_message": run.error_message or "",
            "prompt_version": run.prompt_version or "",
            "skill_version": run.skill_version or "",
            "rules_version": run.rules_version or "",
        },
        # 逐轮：为什么后几轮更贵（提示词每轮重发上一轮的上下文），只有逐轮列出来才看得出。
        # `evidence` 那一块回答的是另一个问题：**这一轮到底看没看到东西** —— 计数分不出
        # 「取数失败」与「真的读了一份 diff」（见 `services/ai/trace_evidence.py`）。
        "rounds": [
            {
                "round_index": row.round_index,
                # 子代理模式：这一轮是哪个分片跑的（空 = 主代理自己那几轮），以及它在那个
                # 成员内部是第几轮。全局 `round_index` 在两个成员之间是连着的，所以
                # 「S1 的第 2 轮」只能靠这一对列说清楚（见 models/ai_analysis/trace.py）。
                "agent": row.agent or "",
                "agent_round": row.agent_round,
                "outcome": row.outcome or "",
                "parsed_ok": bool(row.parsed_ok),
                "tokens_input": row.tokens_input,
                "tokens_output": row.tokens_output,
                "cache_read_tokens": row.cache_read_tokens,
                "cache_write_tokens": row.cache_write_tokens,
                "request_chars": row.request_chars,
                "context_chars": row.context_chars,
                "duration_ms": row.duration_ms,
                "error": row.error or "",
                **decode_evidence(row),
            }
            for row in rounds
        ],
        "rounds_truncated": len(rounds) >= MAX_ROUNDS,
        # 子代理模式下**每个成员**的账（谁跑成了、谁没跑成、各花了多少）。
        # 没有这一块时给空列表 —— **不编造一条**：面板据此判断「这次不是分片跑的」，
        # 而编一条假的会让人以为分片跑过。
        "subagents": _subagent_rows(run),
        "pricing": _price_block(table, errors),
    }
