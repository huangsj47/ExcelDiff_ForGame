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

## 「完成统计」与「活动任务」是两个口径

这一页上的数字回答的是**已经花了多少**，而正在跑的那几次还没有结账：`_create_run`
建行时 token 三列全是 NULL（上游还没报），所以那条行混进聚合的后果是整屏的命中率、
费用与合计 token 一起变成「未上报」—— 一条刚点下去的任务就能让这一页看起来像坏了。
聚合器的口径（`services/ai/usage.py`：任一缺失不给比例、任一条算不出不给合计）是**对的**，
错的是把「还没结账」的运行混进了「已完成统计」。

所以读路径把两者分开：`completed_totals` 只统计**已经结束**的运行（口径与筛选范围一致），
`active_runs` 如实给出在途那些的条数、编号与（有的话）**临时**用量。判据只有一处
（`is_active_run`），页面上那句「当前筛选范围内有 N 个新任务运行中……」就是拿这两个口径拼出来的。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from models import Project, WeeklyVersionConfig, db
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace, AiWeeklyAnalysisState
from services.ai import run_progress
from services.ai.auto_sizing import derive_family_sizing
from services.ai.analysis_budget import (
    PERIOD_ALL_TIME,
    PERIOD_CHOICES,
    PERIOD_LABELS,
    PERIOD_MONTHLY,
    PERIOD_WEEKLY,
    SCOPE_PROJECT,    as_utc,
    budget_rows_for_overview,
    budget_status,
    platform_budget_status,
    redact_platform_scope,
)
from services.ai.budget import (
    COMPACT_AT_RATIO,
    context_reserved_chars,
    context_watermark_chars,
    effective_prompt_budget,
    resolve_context_window,
)
from services.ai.budget_plan import build_budget_plan, derive_tool_limits
from services.ai.engine import EngineLimits
from services.ai.job_service import UPGRADE_REASON_FIRST_RUN
from services.ai.platform_budget import platform_budget_public
from services.ai.pricing import amount_exact, amount_of, money
from services.ai.project_config_source import build_weekly_group_key, get_project_analysis_config
from services.ai.subagent import MIN_MEMBER_ROUNDS, MIN_MEMBER_TOOL_REQUESTS
from services.ai.trace_evidence import decode_evidence
from services.ai.usage import (
    ESTIMATE_SAMPLE_LIMIT,
    MODE_FULL,
    MODE_INCREMENTAL,
    aggregate_runs,
    estimate_analysis,
    estimation_sample,
    usage_from_run,
)
from services.ai.usage_statistics import (
    IN_FLIGHT_STATUSES,
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

# 下钻页每次最多列多少条运行记录。汇总数字仍然按**全部已完成**运行算（在途那几条
# 不进合计，见下面「完成统计 vs 活动任务」）—— 只截断列表，不截断统计，否则页面上的
# 合计会随着「看多少行」变化。
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
# 筛选项与 `models/ai_analysis/analysis_run.py::RUN_STATUSES` 同一套词：少一档的后果是
# 那一类运行**只能混在「全部状态」里看**，而 panel 上「这一周降级了多少次」是个要按
# 状态分组计数的问题（实测库里 13 条完成运行里 12 条是降级 —— 少了这一档，
# 「怎么几乎全是降级」这件事在界面上根本筛不出来）。
STATUS_CHOICES = (STATUS_ALL, "succeeded", "degraded", "failed", "running")
STATUS_LABELS = {
    STATUS_ALL: "全部状态",
    "succeeded": "成功",
    # 措辞比「历次结论」那边（`report_document.STATUS_LABELS` 的「降级完成」）短：
    # 这里是一个**筛选项**，那边是**一条结论的状态** —— 同义不同场景，不是两套说法。
    "degraded": "降级",
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


# ---------------------------------------------------------------------------
#  完成统计 vs 活动任务
# ---------------------------------------------------------------------------
# 「还在跑」的判据。**直接引用 `usage_statistics.IN_FLIGHT_STATUSES`，不在这里再写一份
# 字面量**：那边拿它挡「全量重置」，这边拿它把活动任务从完成统计里摘出去 —— 两处判据
# 不一致的表现是「面板说它还在跑、重置按钮却说可以重置」这类自相矛盾的界面。
ACTIVE_STATUSES = IN_FLIGHT_STATUSES

# `active_runs.runs` 最多列几条明细。**只截断列表，不截断计数**：页面上那句
# 「当前筛选范围内有 N 个新任务运行中」的 N 永远是全量，截断的那部分由 `truncated` 说明。
MAX_ACTIVE_RUNS = 50

# 排序时的兜底时刻（没有 `created_at` 的行排在最早）。带 tzinfo：与 `as_utc()` 的
# 返回值同口径，混着比不会抛 `TypeError`。
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def is_active_run(run: AiAnalysisRun) -> bool:
    """这条运行是不是**还没结账**的活动任务。

    只看库里的原值（`status`），**不看 `effective_status`** —— 后者会把超时的 running
    显示成 failed（那是给界面看的一眼，见 `models/ai_analysis/analysis_run.py`），而
    「这条记录会不会再被回写」的答案不受它影响：超时只是「看起来死了」，进程可能只是慢。
    这与 `usage_statistics.in_flight_runs()` 是同一条判据（那里挡的是全量重置）。

    代价是「僵尸 running」也会被算成活动任务、从而不进完成统计。这是刻意选的：
    另一条路（按 `effective_status` 把僵尸判成已完成）会让一条**永远不会有 token** 的行
    重新把整屏的费用判成「算不出」—— 正是这次要修的那个症状。
    """
    return str(getattr(run, "status", "") or "") in ACTIVE_STATUSES


def split_active_runs(
    runs: Iterable[AiAnalysisRun],
) -> tuple[list[AiAnalysisRun], list[AiAnalysisRun]]:
    """把一批运行分成 `(活动任务, 已完成的)`，**两边都保持传入顺序**。

    调用方拿到的两个列表拼起来就是原来那一批（没有一条被丢掉）—— 「分组」与「过滤」
    是两件事：在途的运行照样要在运行列表里如实出现（显示「未上报」），只是不进合计。
    """
    active: list[AiAnalysisRun] = []
    completed: list[AiAnalysisRun] = []
    for run in runs:
        (active if is_active_run(run) else completed).append(run)
    return active, completed


def _live_tokens(active_runs: Sequence[AiAnalysisRun]) -> dict[str, Any]:
    """活动任务**此刻**的临时用量（来自进程内的进度快照，见 `services/ai/run_progress`）。

    三条纪律照抄那个模块的：读不到就是 `None`（**不是 0**）、它只是**下界**（只含上游
    已经上报的部分）、它**永远**带 `partial=True`。这份数字只给界面显示「现在大概到哪了」，
    **一个 token 都不许混进完成统计** —— 混进去等于把一个随刷新变化的半截数字说成
    「已经花了多少」，而它还会在运行结束时以最终值再计一遍（重复计数）。
    """
    total = 0
    reported = 0
    for run in active_runs:
        snapshot = run_progress.snapshot(int(getattr(run, "id", 0) or 0))
        value = getattr(snapshot, "live_tokens", None) if snapshot is not None else None
        if value is None:
            continue
        reported += 1
        total += int(value)
    return {
        "partial": True,
        # 一条都没上报 → `None`（界面显示「还没有上报」），不补 0。
        "tokens": total if reported else None,
        "reported_runs": reported,
        "unreported_runs": max(0, len(active_runs) - reported),
    }


def active_block(
    active_runs: Sequence[AiAnalysisRun],
    names: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """这一屏里的**活动任务**（在途运行）那一块。

    它与 `completed_totals` 是**并列的两个口径**：一个答「已经花了多少」，一个答
    「现在还有什么在跑」。页面上那句「当前筛选范围内有 N 个新任务运行中，实时用量尚未
    计入；以下统计截至运行 #X」就是拿这两块拼出来的 —— 少了任何一块，那句话都说不圆。

    `latest_run_id` 是**最新一条在途运行**的编号（页面上用来核对是哪一次还没算进去）。
    """
    ordered = sorted(
        active_runs,
        key=lambda run: (
            # 两边都过 `as_utc`：同一个 session 里刚写进去的对象可能还带着 aware 的
            # tzinfo，与库里读出来的 naive 值直接比会抛 `TypeError`（同 `is_counted`）。
            as_utc(getattr(run, "created_at", None)) or _EPOCH,
            int(run.id or 0),
        ),
    )
    shown = ordered[-MAX_ACTIVE_RUNS:]
    name_map = names or {}
    latest_run_id = ordered[-1].id if ordered else None
    return {
        "count": len(ordered),
        "latest_run_id": int(latest_run_id) if latest_run_id is not None else None,
        "truncated": len(ordered) > len(shown),
        "runs": [
            {
                "run_id": run.id,
                "project_id": run.project_id,
                "project_name": name_map.get(run.project_id, f"项目 {run.project_id}"),
                "status": str(getattr(run, "status", "") or ""),
                "target_type": run.target_type,
                "target_id": run.target_id,
                "target_key": run.target_key or "",
                "created_at": _iso(getattr(run, "created_at", None)),
            }
            for run in shown
        ],
        # **临时值**：只给显示，永远标着「不完整」，绝不混进完成统计。
        "live": _live_tokens(ordered),
    }


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

    一次**已完成**运行都没有的项目**不参与**：它没有金额可算，也没有「算不出」这回事
    （`aggregate_runs([])` 的 `cost` 是 `None`）。把它当成「算不出」的话，一个刚建出来、
    第一次分析还在跑的项目会让**整个平台**的费用合计变成「算不出」—— 那正是这次要修的
    那个症状换个位置再犯一遍。
    """
    relevant = [entry for entry in entries if _int_or_zero(entry.get("runs")) > 0]
    costs = [entry.get("cost") for entry in relevant]
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


def _totals_cost_sample(entries: Sequence[dict]) -> dict[str, Any]:
    """跨项目的**已上报样本费用**（与 `_totals_cost` 并列的那一份）。

    `_totals_cost` 的口径是「有一个项目算不出，整个平台合计就是算不出」—— 它同样的
    理由同样成立，所以一个字都没改。但那条口径的后果是**一个项目的一条坏数据会让整个
    平台的费用消失**，所以这里再给一份「每个算得出的项目各自合计，然后相加」，
    并带上**覆盖度**（算得出的运行次数 / 总运行次数）：

    * 次数来自各项目自己的 `reported_samples.cost`（那是**按运行**数的），这里只是相加；
    * 金额按 `amount_exact` 相加（**不用展示串**：`<0.01` 解析不了，量化到分再相加会少算）；
    * 币种仍然必须一致 —— 混着两种币种加出来的数字比没有数字更糟（与 `_totals_cost` 同一条）。
    """
    total_runs = sum(_int_or_zero(entry.get("runs")) for entry in entries)
    reported_runs = 0
    unknown_runs = 0
    known_values: list[Mapping[str, Any]] = []
    for entry in entries:
        sample = (entry.get("reported_samples") or {}).get("cost") or {}
        reported_runs += _int_or_zero(sample.get("reported_runs"))
        unknown_runs += _int_or_zero(sample.get("unknown_runs"))
        value = sample.get("known_value")
        if value:
            known_values.append(value)

    known: Optional[dict[str, Any]] = None
    if known_values:
        currencies = {str(item.get("currency") or "") for item in known_values}
        amounts = [amount_of(item) for item in known_values]
        if len(currencies) == 1 and all(amount is not None for amount in amounts):
            total = sum((amount for amount in amounts if amount is not None), Decimal(0))
            notes = ["按各项目自己的价格表估算后相加（只含已上报样本）"]
            if unknown_runs:
                notes.append(
                    f"另有 {unknown_runs} 次运行没有上报可计算的费用，"
                    "这里是**已知最低费用**，不是总额"
                )
            known = {
                "amount": money(total),
                "amount_exact": amount_exact(total),
                "currency": currencies.pop(),
                "reason": "",
                "notes": notes,
                "lines": [],
            }
    return {
        "total_runs": total_runs,
        "reported_runs": reported_runs,
        "unknown_runs": unknown_runs,
        "known_value": known,
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

    ## 两个口径（见模块 docstring 末节）

    `completed_totals` / `projects[*]` 只统计**已经结束**的运行，`active_runs` 是这一刻
    还在跑的那些。分组只影响这一页怎么算、怎么摆；**预算那一档一个字都不改**
    （`_budget_block` 走的是闸门自己的判定，与这里的分组无关）。
    """
    active = filters or UsageFilters()
    query = _runs_query(accessible_project_ids)
    entries: list[dict[str, Any]] = []
    all_runs: list[AiAnalysisRun] = []
    active_runs: list[AiAnalysisRun] = []
    completed_runs: list[AiAnalysisRun] = []
    # 项目号 → 项目名。活动任务那一块也用它（界面上要说清「哪个项目的哪一次」）。
    names: dict[int, str] = {}
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
        # **分组在统计之前**：在途的运行不进合计（它的 token 三列还是 NULL，混进去会把
        # 命中率与费用判成「算不出」），但它在下面照样占一个项目行、照样进活动任务那一块。
        active_runs, completed_runs = split_active_runs(all_runs)
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
            # 这一行的数字只算**已完成的**那些；在途的条数单独报出去（`running_runs`），
            # 否则界面会出现「这一行写 3 次、点开却列了 4 条」这种对不上的读法。
            project_completed = [run for run in runs if not is_active_run(run)]
            stats = aggregate_runs(project_completed, price_table=table)
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
                    "running_runs": len(runs) - len(project_completed),
                    "collected_runs": stats["collected_runs"],
                    "tokens": stats["tokens"],
                    "cache": stats["cache"],
                    "tools": stats["tools"],
                    "missing_runs": stats["missing_runs"],
                    # 已上报样本 + 覆盖度（见 `services/ai/usage.py` 模块 docstring 末节）：
                    # 项目行那一格据此显示「已知费用 / 已知命中率」而不是整格「未上报」。
                    "reported_samples": stats["reported_samples"],
                    "cost": stats["cost"],
                    "pricing": _price_block(table, errors),
                    "budget": budget_block,
                    # 「最近一次」看的是这一行的**全部**运行（含在途那条）：它回答的是
                    # 「这个项目最近什么时候动过」，不是「最近什么时候结过账」。
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

    # 合计**只算已完成的运行**（`completed_runs`）：不带价格表算（各项目的表不同），
    # 费用另算 —— 见 `_totals_cost`。一条都没完成时它照样给出完整的形状（全 `None`），
    # 界面按「还没有数字」渲染，而不是消失。
    stats = aggregate_runs(completed_runs, price_table=None)
    # 跨项目的费用**只能在这里算**（各项目的价格表不同，聚合器那一层拿不到同一张表）。
    # 于是「已上报样本」那一块要换掉 `cost` 这一档：token 与命中率逐项目相加就等于全局
    # （同一个函数、同一套判据），费用不行。换成全局那一份之后，覆盖率仍然是**按运行**数的，
    # 与界面那句「N / M 次」同口径。
    samples = stats["reported_samples"]
    samples = {**samples, "cost": _totals_cost_sample(entries)}
    totals = {
        "runs": stats["runs"],
        # 这一批里**最新一条已完成**的编号：页面那句「以下统计截至运行 #X」就是它 ——
        # 用户拿它对照活动任务那个编号，就知道差的是哪一次。
        "latest_run_id": max((int(run.id) for run in completed_runs), default=None),
        "collected_runs": stats["collected_runs"],
        "tokens": stats["tokens"],
        "cache": stats["cache"],
        "tools": stats["tools"],
        "missing_runs": stats["missing_runs"],
        "reported_samples": samples,
        "cost": _totals_cost(entries),
    }

    return {
        "success": True,
        "projects": entries,
        # `completed_totals` 是这一屏的**完成统计**（口径与筛选范围一致）；
        # `totals` 是它的历史字段名，逐字等价 —— 老页面/老调用方读它照旧。
        "totals": totals,
        "completed_totals": totals,
        # 活动任务：还在跑的那些（不进上面的统计，但也不藏起来）。`names` 传进去让
        # 界面能直接说「哪个项目的哪一次」，不必自己再查一遍项目名。
        "active_runs": active_block(active_runs, names),
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


def _project_name(project_id: int) -> str:
    """项目名（读不到就给「项目 N」）。活动任务那一块要能说清是哪个项目。"""
    project = Project.query.filter_by(id=project_id).first()
    if project is None:
        return f"项目 {project_id}"
    return project.name or project.code or f"项目 {project_id}"


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


def parse_estimate_args(args: Mapping[str, Any]) -> tuple[Optional[int], dict[str, Any]]:
    """URL query → 估算参数 `(project_id, {mode, planned_files, baseline_reusable, notes})`。

    与 `parse_usage_filters` 同一条口径：**任何非法值都回落，绝不抛**。这些参数从地址栏
    来，让它们变成 500 的话，页面直接白屏而用户没有任何办法回到正常状态。

    `project_id` 是唯一一个「回落不了」的：估算必须知道是哪个项目（历史运行、价格表、
    分片数都是按项目取的），所以它缺失时返回 `None`，由路由回 400 —— 那不是一次非法
    访问，是一个缺少必填参数的请求。
    """
    notes: list[str] = []
    raw_project = _first_arg(args, "project", "project_id")
    project_id = _parse_int(raw_project)
    if raw_project and project_id is None:
        notes.append("项目编号不是有效数字。")

    raw_mode = _first_arg(args, "mode").lower()
    mode = raw_mode if raw_mode in (MODE_FULL, MODE_INCREMENTAL) else MODE_FULL
    if raw_mode and raw_mode not in (MODE_FULL, MODE_INCREMENTAL):
        notes.append(f"模式「{raw_mode}」认不出来，已按全量估算。")

    raw_files = _first_arg(args, "files", "planned_files")
    planned_files = _parse_int(raw_files)
    if raw_files and planned_files is None:
        notes.append(f"目标文件数「{raw_files}」不是有效数字，已按「不知道」处理。")

    raw_baseline = _first_arg(args, "baseline", "reusable").lower()
    baseline: Optional[bool] = None
    if raw_baseline:
        baseline = raw_baseline not in ("0", "false", "no", "off")

    raw_config = _first_arg(args, "config", "config_id")
    config_id = _parse_int(raw_config)
    if raw_config and config_id is None:
        notes.append("周版本配置编号不是有效数字，已按未指定分组处理。")

    return project_id, {
        "mode": mode,
        "planned_files": planned_files,
        "baseline_reusable": baseline,
        "config_id": config_id,
        "notes": notes,
    }


#: 「生效预算是怎么来的」的一个取值：**运行前不探测模型**。
#:
#: 预估端点是只读的（`test_it_is_get_only_and_produces_no_model_call`），所以它算出来的
#: 窗口只能是平台默认口径 —— 这个取值一路传到界面那一行「模型窗口来源」上，读者据此知道
#: 「600,000 是按默认窗口算的，不是端点说的」。
WINDOW_SOURCE_NOT_PROBED = "not_probed_default"


def _platform_prompt_chars_for(project_id: int) -> tuple[int, str]:
    """平台内置提示词一共多少字（`prompt.platform_prompt_chars`，与运行侧**同一个**函数）。

    返回 `(字数, 拿不到时的说明)`。拿不到时回 0 并且**明说**：悄悄回 0 等于告诉用户
    「平台一段内置提示词都不占」，那是一个假事实（而界面那一行正是拿它解释「生效值里
    为什么有一部分不属于你」）。加载失败本身不阻断估算 —— 估算只是几行数字。
    """
    from services.ai.prompt import platform_prompt_chars

    try:
        from services.ai_analysis_service import _load_project_skills

        loaded, failure = _load_project_skills(project_id)
    except Exception as exc:  # noqa: BLE001 —— 读不到内置提示词不该让估算变成 500
        return 0, f"平台内置提示词的字符数没有读出来（{exc}），下面的生效值里**不含**它。"
    if loaded is None:
        return 0, (
            "平台内置提示词的字符数没有读出来（"
            + str(failure or "分析协议未加载")
            + "），下面的生效值里**不含**它。"
        )
    return platform_prompt_chars(loaded), ""


def _estimate_window_note(
    *, clamp_note: str, window_tokens: int, watermark: int, platform_note: str
) -> str:
    """预估端点那一行「模型窗口来源」的原文（界面直接渲染它）。

    三件事按这个顺序说：**先说这次没问端点**（这是前提）、再说被压到哪里（如果有压）、
    最后说内置提示词那一份有没有算进来。顺序不能反 —— 先说「未声明窗口」会让读者以为
    平台问过了，而事实是这一条路径**没有探测**。
    """
    parts = [
        f"运行前不探测模型：这里的生效预算是按平台默认窗口 {window_tokens:,} token 的 "
        f"{int(COMPACT_AT_RATIO * 100)}% 水位（{watermark:,} 字）算的；"
        "任务真正启动前会向端点问一次真实窗口，届时以那一个为准（可能更小）。"
    ]
    if clamp_note:
        parts.append(clamp_note)
    if platform_note:
        parts.append(platform_note)
    return "".join(parts)


def _weekly_payload_facts(config_id: int, *, force_full: bool = False) -> dict[str, Any]:
    """这一轮会进输入的账，以及运行侧已经能确定的范围裁决。

    ## 为什么敢在**只读**端点上读它

    `build_weekly_payload` 是**平台自己的账**：读快照缓存行、按基线做差、把上轮没取到
    证据的文件按风险补回来（`scope_sampling`）。它不发任何模型请求，也没有副作用
    （不建 run、不写状态行）—— 与报告 §5.5 要求的「调用模型之前先看清要做什么」是同一件事。

    「不另立血缘」在这里是硬要求：补偿集与增量集的算法只有这一份实现（`_compensation_entries`
    / `_select_delta_entries`）。在这里重算一遍，两处迟早会在「风险排序」「焦点筛选」
    这些细节上分叉 —— 用户看到的预检数字与真正跑的内容对不上，比不给数字更坏。

    拿不到时回 `(None, None, 说明)`：界面如实写「没有算出来」并把原因转述出来。
    """
    from services.ai_analysis_service import build_weekly_payload

    try:
        payload, _state, skip_reason = build_weekly_payload(
            config_id, force_full=force_full
        )
    except Exception as exc:  # noqa: BLE001 —— 预检读不到账不该让整个估算变成 500
        return {"delta": None, "compensation": None, "planned": None,
                "scope": "", "reason": "",
                "note": f"本次的输入账没有算出来（{exc}）。"}
    if payload is None:
        return {"delta": None, "compensation": None, "planned": None,
                "scope": "", "reason": "",
                "note": "本次的输入账没有算出来（平台裁决："
                + str(skip_reason or "未知") + "）。"}
    summary = payload.get("summary") or {}
    delta = summary.get("delta_files")
    compensation = summary.get("compensation_files")
    policy = payload.get("policy") or {}
    return {
        "delta": int(delta) if isinstance(delta, int) else None,
        "compensation": int(compensation) if isinstance(compensation, int) else None,
        # 估算缩放必须用**真正会交给引擎的文件数**。它通常等于 delta，但全量预检
        # 会以 force_full 重建 payload，此时这里就是整窗文件数。由服务端算这一项，
        # 避免页面把上一次增量预检缓存的 files= 带进全量预检。
        "planned": len(payload.get("delta_files") or []),
        "scope": str(payload.get("scope") or ""),
        "reason": str(policy.get("reason") or ""),
        "note": "",
    }


def _weekly_action_facts(
    project_id: int, *, mode: str, target_type: str, config_id: Optional[int] = None
) -> dict[str, Any]:
    """本次动作的**事实**：增量文件数 / 补偿文件数 / 基线 run / 升级原因（E8）。

    **只查库、不探测模型**（这就是这个端点存在的护栏）：

    * **基线 run** 取自 `AiWeeklyAnalysisState.last_concluded_run_id` —— 与
      `job_service.create_or_attach_job` 的 `base_run_id` **同一个指针**（不另立血缘：
      预检说「基线是 Run 22」而建出来的 job 指向 Run 19，是最坏的一种不一致）；
    * **升级原因**用 `job_service._decide_effective_mode` **同一套判据**（同一份状态行的
      同一个指针），所以界面上的「这次会被升级为全量」就是建 job 时真的会发生的事；
    * **增量/补偿文件数**读平台的输入账（见 `_weekly_payload_facts`）。

    一个分组都没有 / 不是周版本 / 全量模式时，四个字段各自回它们的「不适用」值
    （`None` 或空串）—— **不是 0**：0 是「一个文件都不变」这个确定的结论。

    ## 哪个分组？—— 一个**说出来**的近似

    预检请求里只有项目号（`/usage/estimate` 的参数表里没有 config：路由那一层不归本模块，
    而 `parse_estimate_args` 多解析一个键也传不到这里）。所以这里取「**最近更新过的那条
    状态行**」所属的分组，并且在有歧义时**把这件事写进 `note`**（带上那个分组的窗口）。

    把近似说成事实是最坏的一种处置：用户在看 A 窗口的抽屉，而预检摆的是 B 窗口的基线 run
    —— 一句「取自最近更新过的分组（窗口 …）」能让这件事当场被认出来。项目只有一个分组时
    不加那句话（无话找话的说明会让真正的警告贬值）。
    """
    blank: dict[str, Any] = {
        "planned_files": None,
        "delta_files": None,
        "compensation_files": None,
        "baseline_run": None,
        "upgrade_reason": "",
        "note": "",
    }
    if target_type != "weekly":
        return blank

    exact_config = db.session.get(WeeklyVersionConfig, config_id) if config_id else None
    if exact_config is not None and exact_config.project_id != project_id:
        exact_config = None
    query = AiWeeklyAnalysisState.query.filter_by(project_id=project_id)
    if exact_config is not None:
        query = query.filter_by(group_key=build_weekly_group_key(exact_config))
    states = (
        query
        # 「最近更新过的那个分组」：`updated_at` 是状态行每次推进都会写的时刻（基线指针、
        # 水位线都写它）。按它排而不是按 `last_analyzed_at` —— 后者在降级运行时不推进，
        # 于是「刚跑完但降级」的分组会被排到后面去。
        .order_by(AiWeeklyAnalysisState.updated_at.desc(), AiWeeklyAnalysisState.id.desc())
        .all()
    )
    if not states:
        return blank
    state = states[0]
    ambiguous = [
        item for item in states[1:] if _int_or_zero(getattr(item, "last_concluded_run_id", None))
    ]
    ambiguity_note = ""
    if ambiguous and getattr(state, "start_time", None) and getattr(state, "end_time", None):
        ambiguity_note = (
            f"这个项目里有 {len(ambiguous) + 1} 个周版本分组有结论基线；上面的基线 run 与"
            f"输入账取自**最近更新过的那个分组**（窗口 {state.start_time:%Y-%m-%d} ~ "
            f"{state.end_time:%Y-%m-%d}）。如果你看的是另一个窗口，请以那一份为准。"
        )

    run_id = _int_or_zero(getattr(state, "last_concluded_run_id", None)) or None
    # 升级原因：用户要的就是全量时**没有**升级可言（那是他自己点的），
    # 只有「要增量但一个可复用基线都没有」才是平台替他改的那一种。
    if mode == MODE_INCREMENTAL and run_id is None:
        upgrade_reason = UPGRADE_REASON_FIRST_RUN
    else:
        upgrade_reason = ""

    if mode != MODE_INCREMENTAL:
        payload_facts = (
            _weekly_payload_facts(exact_config.id, force_full=True)
            if exact_config is not None else None
        )
        return {
            **blank,
            "planned_files": payload_facts["planned"] if payload_facts else None,
            "upgrade_reason": upgrade_reason,
            "note": (
                "这次是全量：平台不看增量基线，所以本次增量文件数与补偿文件数不适用。"
                + ((payload_facts or {}).get("note") or "")
            ),
        }
    if run_id is None:
        payload_facts = (
            _weekly_payload_facts(exact_config.id, force_full=True)
            if exact_config is not None else None
        )
        return {
            **blank,
            "planned_files": payload_facts["planned"] if payload_facts else None,
            "upgrade_reason": upgrade_reason,
            "note": (
                "还没有可复用的结论基线，所以没有「本次增量文件数」可言"
                "（平台会把这次增量升级为全量）。"
                + ((payload_facts or {}).get("note") or "")
            ),
        }

    run = db.session.get(AiAnalysisRun, run_id)
    if run is None:
        return {
            **blank,
            "upgrade_reason": upgrade_reason,
            "note": f"结论基线指针指向的运行 #{run_id} 已经不在了，这个分组的基线要重新建立。",
        }

    baseline_run = {
        "run_id": run.id,
        "created_at": _iso(run.created_at),
        "scope": str(run.scope or ""),
    }
    facts_config_id = exact_config.id if exact_config is not None else _int_or_zero(run.target_id)
    payload_facts = _weekly_payload_facts(facts_config_id)
    if mode == MODE_INCREMENTAL and payload_facts["scope"] == MODE_FULL:
        upgrade_reason = payload_facts["reason"] or "runtime_scope_upgrade"
    planned_for_effective_mode = payload_facts["planned"]
    if upgrade_reason and exact_config is not None:
        # 页面在看到 upgrade_reason 后会把确认结果明确转成 mode=full 再建任务；运行侧
        # 因而会 `force_full=True`，输入是整窗，而不是上面那份增量账。预检仍要展示真实
        # delta/补偿数，但 token 与耗时区间必须按**将要执行的全量输入**缩放。
        # 否则就会出现「弹窗写 221 个，点继续后实际跑 1196 个」这种数量级错误。
        full_facts = _weekly_payload_facts(exact_config.id, force_full=True)
        if full_facts["planned"] is not None:
            planned_for_effective_mode = full_facts["planned"]
    return {
        "planned_files": planned_for_effective_mode,
        "delta_files": payload_facts["delta"],
        "compensation_files": payload_facts["compensation"],
        "baseline_run": baseline_run,
        "upgrade_reason": upgrade_reason,
        "note": (payload_facts["note"] or "") + ambiguity_note,
    }


def analysis_estimate(
    project_id: int,
    *,
    mode: str = MODE_FULL,
    planned_files: Optional[int] = None,
    baseline_reusable: Optional[bool] = None,
    target_type: str = "weekly",
    config_id: Optional[int] = None,
    sample_limit: int = ESTIMATE_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """**执行前**的代价区间（AI-P1-03）：取数在这里，算术在 `services/ai/usage.py`。

    取数与算术分开不是分层洁癖：估算要能在**建 job 的那一刻**算出来（第二波的
    `planned_tokens_low/high`），那时不能有一条会读配置、会查时钟的链路横在里面。
    这里做的是「查最近同类运行 + 读配置里的分片数/轮次上限 + 解析价格表」，
    然后原样交给纯函数。

    ## 只把**有数**的运行当样本

    `failed` 且三列全 NULL 的运行（实测里 20 条里有 5 条）参与缩放只会把区间拉偏 ——
    它们没有 token 数，除了拖低「最近一次」什么也贡献不了。所以样本只取
    `usage_from_run(...)["collected"]` 为真的那些，并在 `notes` 里写明排除了多少次
    （不写的话，「最近一次实际值」看起来像是库里最新的那一次，可能对不上号）。

    ## 同类的判据

    `target_type`（默认 weekly）+ 运行模式（`scope`）。模式那一道由纯函数做：同模式的
    样本一个都没有时它回落全部样本，并把这件事写进 `notes` —— 增量没有历史是常态。
    """
    table, errors = project_price_table(project_id)
    config = get_project_analysis_config(project_id) or {}
    model = str(config.get("api_model") or "")
    query = AiAnalysisRun.query.filter_by(project_id=project_id)
    if target_type:
        query = query.filter(AiAnalysisRun.target_type == target_type)
    if target_type == "weekly" and config_id is not None:
        # 同一项目里可以同时有真实大周版本与 1～11 文件的 E2E 小版本。按项目混样本会
        # 把小版本的高固定开销当成「每文件强度」，再乘到 1000+ 文件，实测会从约 4M
        # 被夸到 90M token。config 已由路由校验归属；服务层直调时也只在归属吻合时
        # 收窄，避免一个错误 config 把样本静默清空。
        exact_config = db.session.get(WeeklyVersionConfig, config_id)
        if exact_config is not None and exact_config.project_id == project_id:
            query = query.filter(
                AiAnalysisRun.target_key == build_weekly_group_key(exact_config)
            )
    rows = query.order_by(AiAnalysisRun.created_at.desc()).limit(sample_limit).all()

    samples = [
        estimation_sample(run, price_table=table)
        for run in rows
        if usage_from_run(run, price_table=table)["collected"]
    ]
    # ------------------------------------------------------------------
    # 本次动作的三个事实（E8）：增量文件数 / 补偿文件数 / 基线 run
    #
    # **只读、不探测模型**（与这个端点的另外三条护栏同一条口径）：读的是平台自己的
    # 快照账与结论基线指针，没有任何一次出网调用。
    # ------------------------------------------------------------------
    facts = _weekly_action_facts(
        project_id, mode=mode, target_type=target_type, config_id=config_id
    )
    # 有精确 config 时，输入账比页面回传的 files= 更新、也更可信。页面变量会跨两次
    # 确认框存活：先看增量再切全量时，那个值仍是增量数，若让它覆盖服务端事实，区间会
    # 被缩小几个数量级。只有服务端算不出本次输入账时才回落调用方给的近似值。
    effective_planned_files = (
        facts["planned_files"]
        if facts.get("planned_files") is not None
        else planned_files
    )
    # 预检已判定会升级时，页面确认后发送的是 mode=full。样本也必须选 full；继续拿
    # incremental 的 7～47 文件运行算单文件强度，再乘 1196，会把固定提示词/汇总开销
    # 重复放大上百次（实测约 4M 会被报成 90M）。
    estimation_mode = (
        MODE_FULL
        if mode == MODE_INCREMENTAL and facts.get("upgrade_reason")
        else mode
    )
    engine_defaults = EngineLimits()
    configured_chars = (
        _int_or_zero(config.get("prompt_char_budget")) or engine_defaults.prompt_char_budget
    )
    window_tokens, _window_defaulted = resolve_context_window(None)
    watermark = context_watermark_chars(window_tokens)
    platform_chars, platform_note = _platform_prompt_chars_for(project_id)
    # 与运行侧 `_apply_model_window` 同一条口径：窗口管的是**整份提示词**，所以先把
    # 「配置值 + 平台内置」压到水位，再把内置那一段减掉还给用户的部分。差别只有一处：
    # 这里窗口是**未探测**的（预估端点不探模型），所以按 `DEFAULT_CONTEXT_TOKENS` 算。
    effective_total, clamp_note = effective_prompt_budget(
        configured_chars + platform_chars, window_tokens
    )
    effective_chars = min(
        configured_chars,
        effective_total if not clamp_note else max(0, effective_total - platform_chars),
    )
    # 周版本 + 子代理模式：**与运行侧同一份推导**（`derive_family_sizing`，配置面收敛
    # 2026-09-23）。预估侧不加载项目 skill，维度数按平台出厂 9 个算 —— 项目声明了更短
    # 清单时运行侧会少开几个分片，预估因此略偏高（方向保守）。不接同一份推导的话，
    # 确认框会说 5 片、预估按旧配置算 3 片（:1483 一类口径 bug 的同族）。
    sizing = (
        derive_family_sizing(
            effective_user_chars=effective_chars,
            # 文件数未知（调用方没带 `files` 参数、库里也没有可折算的历史）时按 0 推导：
            # 取样上限会落到 200 的下限，索取次数按「清单=下限」反解 —— 方向是**少估**
            # 而不是崩（崩在预估端点上表现为确认框打不开，比一个偏小的数糟糕得多）。
            file_count=int(
                facts["planned_files"]
                if facts.get("planned_files") is not None
                else (effective_planned_files or 0)
            ),
        )
        if (
            target_type == "weekly"
            and bool(config.get("subagent_enabled"))
        )
        else None
    )
    estimate = estimate_analysis(
        planned_files=effective_planned_files,
        mode=estimation_mode,
        baseline_reusable=baseline_reusable,
        recent_runs=samples,
        shard_count=(sizing.shard_count if sizing is not None else None),
        max_rounds=(sizing.rounds_per_shard if sizing is not None else None),
        max_tool_requests=(
            sizing.requests_per_shard if sizing is not None else None
        ),
        price_table=table,
        model=model,
        # 三个事实字段（见 `estimate_analysis` 末节）：原样穿过去、不进算术。
        delta_files=facts["delta_files"],
        compensation_files=facts["compensation_files"],
        baseline_run=facts["baseline_run"],
    )
    configured_rounds = (
        sizing.rounds_per_shard if sizing is not None else engine_defaults.max_rounds
    )
    configured_requests = (
        sizing.requests_per_shard if sizing is not None else engine_defaults.max_tool_requests
    )
    configured_shards = sizing.shard_count if sizing is not None else 1
    estimate["budget_plan"] = build_budget_plan(
        configured_prompt_chars=configured_chars,
        # **不许把配置值当成生效值。** 原先这里写的是 `effective_prompt_chars=configured_chars`
        # 与 `platform_chars=0`，于是配 2,000,000 的项目显示「2,000,000 配置 / 2,000,000
        # 当前预估生效」—— 那是一次必然被上游拒绝的调用被说成「预算够用」（E3 验收点名的
        # 那一条）。现在按未探测下的水位算，并如实标明来源。
        effective_prompt_chars=effective_chars,
        platform_chars=platform_chars,
        max_rounds=configured_rounds,
        max_tool_requests=configured_requests,
        shard_count=configured_shards,
        verify=bool(config.get("subagent_verify")),
        tool_limits=derive_tool_limits(
            prompt_char_budget=effective_chars,
            max_tool_requests=configured_requests,
        ),
        window_source=WINDOW_SOURCE_NOT_PROBED,
        reserved_output_chars=context_reserved_chars(window_tokens),
        window_note=_estimate_window_note(
            clamp_note=clamp_note,
            window_tokens=window_tokens,
            watermark=watermark,
            platform_note=platform_note,
        ),
        family_pool=(
            {
                "requests_pool": sizing.family_requests_pool,
                "rounds_pool": sizing.family_rounds_pool,
                "requests_nominal": sizing.requests_per_shard,
                "rounds_nominal": sizing.rounds_per_shard,
                # 预估侧给的是**理论上限**（池 + 汇总保底）；floor 直接引运行侧的
                # `MIN_MEMBER_*` 常量 —— 写死一个「同值」的 2 早晚漂移成 3 对 2。
                "synthesis_floor_requests": MIN_MEMBER_TOOL_REQUESTS,
                "synthesis_floor_rounds": MIN_MEMBER_ROUNDS,
                "note": sizing.note,
            }
            if sizing is not None
            else None
        ),
    )
    excluded = len(rows) - len(samples)
    if excluded:
        estimate["notes"] = [
            *estimate["notes"],
            f"最近 {len(rows)} 次运行里有 {excluded} 次没有上报用量（失败在半路），"
            "它们不参与估算，「最近一次实际值」指的也是最近一次**有上报**的那次。",
        ]
    if facts["note"]:
        # 「本次增量文件数 / 补偿文件数」没有算出来时，**必须**跟着一句为什么：
        # 界面上那一行写的是「见下面的说明」，空着就是让用户去猜。
        estimate["notes"] = [*estimate["notes"], facts["note"]]
    # 价格表的状态也一并带出去：界面据此决定「显示费用」还是「提示去配置」
    # （`DEFAULT_PRICE_TABLE` 是空表，这是常态，不是异常）。
    estimate["pricing"] = _price_block(table, errors)
    # 升级原因（E8 的预检要摆的第四样）：与 `job_service._decide_effective_mode` **同一个
    # 判据**（同一份状态行的同一个指针），所以界面看到的「这次会被升级为全量」与建 job
    # 时真正发生的事不可能分叉。全量不问这件事（用户自己要的，没有「升级」可言）。
    estimate["upgrade_reason"] = facts["upgrade_reason"]
    estimate["generated_at"] = _iso(datetime.now(timezone.utc))
    return estimate


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

    **但「同一批」是把活动任务算在外面说的**（见模块 docstring 末节）：合计、周版本
    维度的数字只算**已经结束**的运行，`active_runs` 单独给出还在跑的那几次 —— 否则
    一条刚建出来的 running 行（token 三列还是 NULL）会让这一页的命中率与费用一起变成
    「未上报」。运行列表那一份**照列全部**（含在途那条，它显示「未上报」是事实）。
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
    active_runs, completed_runs = split_active_runs(runs)
    stats = aggregate_runs(completed_runs, price_table=table)
    pricing = _price_block(table, errors)
    names = {project_id: _project_name(project_id)}

    # 分组按**全部**运行建（含在途那条）：只在跑的周版本不能因此从表里消失 ——
    # 那一行会显示「0 次运行 · 1 次运行中」，而不是不见了。
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
        group_completed = [run for run in group if not is_active_run(run)]
        group_stats = aggregate_runs(group_completed, price_table=table)
        config_id = group[0].target_id
        weekly_versions.append(
            {
                "group_key": group_key,
                "config_id": config_id,
                "config_name": config_names.get(config_id, "") if config_id else "",
                "runs": group_stats["runs"],
                "running_runs": len(group) - len(group_completed),
                "collected_runs": group_stats["collected_runs"],
                "tokens": group_stats["tokens"],
                "cache": group_stats["cache"],
                "tools": group_stats["tools"],
                "missing_runs": group_stats["missing_runs"],
                "reported_samples": group_stats["reported_samples"],
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

    totals = {
        "runs": stats["runs"],
        # 这一批里**最新一条已完成**的编号（页面那句「以下统计截至运行 #X」）。
        "latest_run_id": max((int(run.id) for run in completed_runs), default=None),
        "collected_runs": stats["collected_runs"],
        "tokens": stats["tokens"],
        "cache": stats["cache"],
        "tools": stats["tools"],
        "missing_runs": stats["missing_runs"],
        "reported_samples": stats["reported_samples"],
        "cost": stats["cost"],
    }
    return {
        "success": True,
        "project_id": project_id,
        # 与总览同一对字段：`completed_totals` 是显式口径名，`totals` 是历史字段名，
        # 逐字等价 —— 老页面/老调用方读它照旧。
        "totals": totals,
        "completed_totals": totals,
        # 还在跑的那几次：不进上面的合计，但如实露出来。
        "active_runs": active_block(active_runs, names),
        "weekly_versions": weekly_versions,
        # 运行列表**列全部**（含在途那条）：它在列表里显示「未上报」是事实，
        # 藏起来才会变成「这次分析没跑过」。
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
                "finish_reason": next(
                    (
                        part.removeprefix("finish_reason=")
                        for part in str(row.error or "").split("；")
                        if part.startswith("finish_reason=")
                    ),
                    "",
                ),
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
