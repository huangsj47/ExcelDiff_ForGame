#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消耗统计的口径：读、设起点、全量重置（单行表 `ai_usage_statistics`）。

## 三条边界，改这块代码前先读

1. **起点只影响消耗面板**（`services/ai_usage_service.filter_runs`）。它**绝不许**接进
   预算闸门（`analysis_budget.platform_budget_status` / `budget_status`）：那样「点一下重置」
   就等于把额度送回去。`tests/test_ai_usage_statistics.py` 里有一条专门钉这件事的回归
   （设起点前后预算「已用」必须逐字相同）。

2. **全量重置是不可逆的**：它真删 `ai_analysis_run` / `_trace` / `_anomaly`，
   AI 报告正文随之消失，预算的「已用」也会归零（记录没了）。所以：
   * 需要确认词（路由层校验），且**在途运行存在时直接拒绝** —— 否则那条运行回写时会踩到
     已被删掉的行，用户看到的是「分析失败」而不是「你刚重置过」；
   * 删完之后「上一次分析」不存在了 → 下一次分析退化成**全量分析**
     （`baseline_source.previous_run` 查不到），「上次报过的问题」基线一并消失。
     这是重置的题中之义，但要在弹层与文档里说清。

3. **时间口径是 naive UTC**（与 `ai_analysis_run.created_at` 同一套，见
   `models/ai_analysis/usage_statistics.py` 的模块说明）。界面给的是**北京墙钟**，
   进出各换算一次，别在别处再换。

## 形状照 `services/ai/platform_budget.py`

`get_*` 永远返回完整字典（不返回 None 逼调用点各写一次兜底）、`set_*` 返回
`(ok, message, errors)`、落库失败 rollback 并回一句人话。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from models import db
from models.ai_analysis import (
    SINGLETON_ID,
    AiAnalysisAnomaly,
    AiAnalysisRun,
    AiAnalysisTrace,
    AiUsageStatistics,
    AiWeeklyAnalysisState,
    now_utc_naive,
)
from services.ai.analysis_budget import as_utc
from utils.logger import log_print
from utils.timezone_utils import (
    beijing_wallclock_to_utc_naive,
    utc_naive_to_beijing_wallclock,
)

# 在途（还没结束）的运行状态。与 `AiAnalysisRun.effective_status` 的判定同源，
# 但这里刻意只看**库里的原值**：`effective_status` 会把超时的 running 显示成 failed，
# 而重置要防的是「这条记录还会被回写」，超时但进程仍活着的那条照样会写。
IN_FLIGHT_STATUSES = ("pending", "running")

# 全量重置的确认词。**不是**安全边界（权限才是，见路由的 `@require_admin`），
# 它防的是误触：这个动作删的是报告历史，点错了没有撤销。
RESET_CONFIRM_WORD = "重置"


def _row() -> Optional[AiUsageStatistics]:
    return db.session.get(AiUsageStatistics, SINGLETON_ID)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """naive UTC → ISO 串（**不带时区后缀**，与库里存的是同一种墙钟）。"""
    return value.isoformat() if value else None


def _local_iso(value: Optional[datetime]) -> Optional[str]:
    """naive UTC → 北京墙钟 ISO 串（给界面直接显示/回填用）。"""
    local = utc_naive_to_beijing_wallclock(value)
    return local.isoformat() if local else None


def usage_baseline() -> Optional[datetime]:
    """统计起点（naive UTC）；没有行或没设过时返回 `None` = 统计全部历史。

    读路径每次请求调一次，很便宜（单行表按主键取，SQLAlchemy 有 identity map）。
    """
    row = _row()
    return getattr(row, "counted_since", None) if row is not None else None


def is_counted(run, baseline: Optional[datetime]) -> bool:
    """这条运行算不算在当前口径里（`baseline` 为 `None` = 全算）。

    **只有这一处判定**：面板的读路径（`services/ai_usage_service.filter_runs`）与
    「这一屏被排除了多少条」都调它，两处各写一遍的话，界面上就会出现「合计少算了它、
    却没说自己排除了它」的第三种状态。
    """
    if baseline is None:
        return True
    # 两边都过一遍 `as_utc`：库里的值是 naive UTC，但**同一个 session 里刚写进去的对象**
    # 可能还带着 aware 的 tzinfo（SQLite 的 tzinfo 是在落库那一刻才丢的），直接比会抛
    # `TypeError`；而「假设它是本地时间」会差 8 小时。naive 一律当 UTC 解释，只有一处口径。
    stamp = as_utc(getattr(run, "created_at", None))
    boundary = as_utc(baseline)
    # 没有 `created_at` 的记录一律不计入：它进不了任何时间窗，也不该悄悄混进合计。
    if stamp is None or boundary is None:
        return False
    return stamp >= boundary


def count_runs_before(baseline: Optional[datetime], runs) -> int:
    """这批运行里有多少条在起点之前（用于界面上的「此前 N 次未计入」）。

    **只数传进来的这一批**：调用方给的是「当前筛选之后、且当前用户能看到的那些运行」，
    所以这个数既不会越过权限，也不会与眼前这一屏无关。
    """
    if baseline is None:
        return 0
    return sum(1 for run in runs if not is_counted(run, baseline))


def in_flight_runs() -> int:
    """还在跑（或还没起跑）的分析条数。全量重置要挡的就是它们。"""
    return int(
        AiAnalysisRun.query.filter(AiAnalysisRun.status.in_(IN_FLIGHT_STATUSES)).count() or 0
    )


def total_runs() -> int:
    """库里**全部**运行记录条数（不受任何筛选与起点影响）。

    这个数是给「全量重置」那个弹层用的：它删的是整张表，而用户眼前的表可能正筛着
    一个项目。弹层里写「这一页看到 24 次，它们都会被删掉」而实际删掉 300 条，
    是这个页面上最不能出现的那种不一致 —— 破坏性动作的后果必须按**它真的会删多少**来说。
    """
    return int(AiAnalysisRun.query.count() or 0)


def get_usage_statistics() -> dict[str, Any]:
    """给界面的一份完整口径（没有行时按「全部历史」）。"""
    row = _row()
    since = getattr(row, "counted_since", None) if row is not None else None
    reset_at = getattr(row, "reset_at", None) if row is not None else None
    return {
        "counted_since": _iso(since),
        # 展示/回填用：`<input type="datetime-local">` 认的就是北京墙钟的 `YYYY-MM-DDTHH:MM`。
        "counted_since_local": _local_iso(since),
        "reset_at": _iso(reset_at),
        "reset_at_local": _local_iso(reset_at),
        "reset_by": (getattr(row, "reset_by", "") or "") if row is not None else "",
        "updated_by": (getattr(row, "updated_by", "") or "") if row is not None else "",
    }


def parse_baseline_input(raw: Any) -> tuple[Optional[datetime], Optional[str]]:
    """把界面给的北京墙钟串解析成 naive UTC 起点。返回 `(值, 错误说明)`。

    * `None` / 空串 → `(None, None)` = 恢复「全部历史」；
    * 认不出来的格式 → 报错，**不**静默当成 None —— 那会把「我填错了」变成「起点被清掉了」；
    * 未来时间 → 报错：起点在未来会把之后的所有运行也排除掉，页面变成永远的空表，
      而用户以为自己在「重置」，看不出哪里错了。
    """
    if raw is None:
        return None, None
    text = str(raw).strip()
    if not text:
        return None, None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, f"时间格式看不懂：{text!r}（要的是 2026-09-19T14:30 这样）"

    since = beijing_wallclock_to_utc_naive(parsed)
    if since is None:
        return None, f"时间格式看不懂：{text!r}"
    if since > now_utc_naive():
        return None, "统计起点不能是未来时间 —— 那样之后的分析也会被排除掉。"
    return since, None


def set_usage_baseline(
    since: Optional[datetime], *, updated_by: str = ""
) -> tuple[bool, str, list[dict]]:
    """设置（或清除）统计起点。返回 `(成功, 一句话, 字段级错误)`。

    与预算保存同一形状：路由层不必为两处写两种错误处理。
    """
    try:
        row = _row()
        if row is None:
            row = AiUsageStatistics(id=SINGLETON_ID)
            db.session.add(row)
        row.counted_since = since
        row.updated_by = (updated_by or "")[:100]
        row.updated_at = now_utc_naive()
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 —— 落库失败要回一句人话，不是 500 堆栈
        db.session.rollback()
        return False, f"保存失败：{exc}", []

    if since is None:
        return True, "已恢复为统计全部历史。", []
    shown = utc_naive_to_beijing_wallclock(since)
    return True, f"统计起点已设为 {shown:%Y-%m-%d %H:%M}（北京时间）。", []


def purge_usage_statistics(*, updated_by: str = "") -> tuple[bool, str, dict]:
    """**全量重置**：删掉全部 AI 运行记录及其轨迹与异常清单。不可逆。

    返回 `(成功, 一句话, 计数)`；拒绝时计数为空字典。

    先子后父地删（`trace` / `anomaly` 再 `run`），不依赖 SQLite 的 `PRAGMA foreign_keys`
    是开是关 —— 换后端（MySQL）时外键一定生效，那时候靠顺序才删得掉。
    """
    pending = in_flight_runs()
    if pending:
        return (
            False,
            f"还有 {pending} 次分析没有结束（进行中或排队中）。等它们跑完再重置 —— "
            "否则那条运行回写结果时会踩到已经被删掉的行，报出来的是「分析失败」，"
            "而不是「你刚重置过」。（卡死的那条会被调度器在超时后重置成失败，届时即可重置。）",
            {},
        )

    try:
        traces = db.session.query(AiAnalysisTrace).delete(synchronize_session=False)
        anomalies = db.session.query(AiAnalysisAnomaly).delete(synchronize_session=False)
        runs = db.session.query(AiAnalysisRun).delete(synchronize_session=False)
        # 周版本状态里那个「上一次运行」的指针：删掉运行之后它就是脏的
        # （这一列只写不读，但留着一个指向已删行的 id 迟早会有人当真）。
        db.session.query(AiWeeklyAnalysisState).update(
            {AiWeeklyAnalysisState.last_analysis_run_id: None},
            synchronize_session=False,
        )
        # 起点也一起清掉：一条运行都不剩了，起点没有意义，留着只会在界面上显示
        # 一个「此前的运行未计入」的话，而那个「此前」已经不存在。
        row = _row()
        if row is None:
            row = AiUsageStatistics(id=SINGLETON_ID)
            db.session.add(row)
        row.counted_since = None
        row.reset_at = now_utc_naive()
        row.reset_by = (updated_by or "")[:100]
        row.updated_by = (updated_by or "")[:100]
        row.updated_at = now_utc_naive()
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        return False, f"重置失败：{exc}", {}

    log_print(
        f"🧹 AI 消耗统计已全量重置：运行 {runs} 条、分轮轨迹 {traces} 条、"
        f"异常清单 {anomalies} 条（操作人：{updated_by or '未知'}）",
        "AI",
        force=True,
    )
    return True, f"已清空全部消耗统计：删除 {runs} 次运行记录。", {
        "runs": int(runs or 0),
        "traces": int(traces or 0),
        "anomalies": int(anomalies or 0),
    }


def statistics_public(*, can_manage: bool, excluded_runs: int = 0) -> dict[str, Any]:
    """给界面的一块：口径本身 + 这一屏被排除的条数 + 能不能改。

    `can_manage` 由路由按 `platform_scope_visible()` 决定，**不由这里猜**：
    起点是平台级口径，而「谁能改」是权限问题，两件事分开写才不会有一天不一致。

    `confirm_word` 也在这里下发（而不是写死在页面里）：确认词的事实源是
    `RESET_CONFIRM_WORD`，路由校验的就是它。两边各写一份的话，改词的那天界面会
    一直提交一个被拒的值，而报错看起来像是「后端坏了」。
    """
    payload = get_usage_statistics()
    payload["excluded_runs"] = int(excluded_runs or 0)
    payload["can_manage"] = bool(can_manage)
    payload["confirm_word"] = RESET_CONFIRM_WORD
    # 这两项都是**全平台**口径、也只有能按「全量重置」的人用得上，所以只给管理员：
    #
    # * `total_runs` 是「这一按会删掉多少条」；
    # * `in_flight_runs` 是「现在有几条还在跑（按下去会往已删的行里回写）」——
    #   界面只在 `canManage` 为真时才读它（`ai_usage_dashboard.renderStatistics`：
    #   `blocked = canManage && inFlight > 0`），发给非管理员是**发而不用**的数据。
    #
    # `in_flight_runs` 原先漏在闸门外面。它同样是全平台计数（`in_flight_runs()` 不按
    # 项目过滤），而 `/ai-analysis/usage/overview` 是**登录即可访问**的（不是 /admin/
    # 下的路径，见路由注释），所以任何登录用户都能读到「全平台正在跑几条」——隔壁
    # `total_runs` 正是为这一点被扣住的，两个数属于同一类，不能只扣一个。
    if can_manage:
        payload["in_flight_runs"] = in_flight_runs()
        payload["total_runs"] = total_runs()
    return payload
