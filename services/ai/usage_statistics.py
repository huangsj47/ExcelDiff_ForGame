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

import json
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from models import db
from models.ai_analysis import (
    IN_FLIGHT_STATUSES,
    SINGLETON_ID,
    AiAnalysisAnomaly,
    AiAnalysisRoundEvent,
    AiAnalysisRun,
    AiAnalysisTrace,
    AiUsageStatistics,
    AiWeeklyAnalysisState,
    now_utc_naive,
)
from services.ai.analysis_budget import as_utc
from services.ai.baseline import FORCE_FULL_REBUILD_REASON
from services.ai.job_service import (
    UPGRADE_REASON_FIRST_RUN,
    UPGRADE_REASON_NO_CONCLUSION_BASELINE,
)
from services.ai.usage import aggregate_runs
from utils.logger import log_print
from utils.timezone_utils import (
    beijing_wallclock_to_utc_naive,
    utc_naive_to_beijing_wallclock,
)

# 在途（还没结束）的运行状态。**定义搬到了模型层**（挨着 `RUN_STATUSES`，两个调用点
# 共用同一个枚举）；这里按旧名字回导一份，免得既有 import 点集体改一次。
#
# 这一处**只看库里的原值**这个口径没有变：`effective_status` 会把超时的 running 显示成
# failed，而重置要防的是「这条记录还会被回写」，超时但进程仍活着的那条照样会写。
# 与之相反的取法见删除那条路（`models/ai_analysis/analysis_run.py::IN_FLIGHT_STATUSES`
# 的注释写了两者为什么不同）。

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

    先子后父地删（`trace` / `anomaly` / **`round_event`** 再 `run`），不依赖 SQLite 的
    `PRAGMA foreign_keys` 是开是关 —— 换后端（MySQL）时外键一定生效，那时候靠顺序才删得掉。

    ## 这三张子表**一张都不能漏**（2026-09-26 复核补的第三张）

    它原先只删前两张，`ai_analysis_round_event` 一行都不删。那张表是本族里**唯一没有外键**
    的（见模型 docstring），所以删父行既不报错也不带走它 —— 而 `ai_analysis_run.id` 是裸
    `Integer` 主键、**表清空之后新 run 从 1 重新编号**，于是下一次运行的逐轮视图
    （`round_events.events_for_run` 只按 `run_id` 过滤）会把上一批早已删掉的事件行整批
    读进来。症状不是「多了几行垃圾」，是**错数据**：新运行的「思考过程」混入别人的轮次。

    它与 `run_cache_source.remove_analysis_runs`（按 id 删的那条路）是**两条**删除路径，
    所以 `tests/test_ai_run_deletion.py` 里有一条跟着 schema 走的护栏：任何一张带 `run_id`
    的表，两条路都必须点到名。
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
        # **第三张子表**：它没有外键，删父行既不报错也不带走它，而 run id 会被复用
        # （理由见 docstring）。这一行原先漏了。
        events = db.session.query(AiAnalysisRoundEvent).delete(synchronize_session=False)
        runs = db.session.query(AiAnalysisRun).delete(synchronize_session=False)
        # 周版本状态里那两个指针：删掉运行之后它们都是脏的。
        #
        # `last_concluded_run_id` 原先**漏了**（只清了 `last_analysis_run_id`）。它的后果
        # 不是「界面上多一个坏 id」，而是与保留期清理同一个坑：`_decide_effective_mode`
        # 只看「指针是不是 NULL」，于是下一次「增量」**不被升格成全量**、也没有升级原因，
        # 而那份报告是拿着一个已经被删掉的基线跑出来的。
        db.session.query(AiWeeklyAnalysisState).update(
            {
                AiWeeklyAnalysisState.last_analysis_run_id: None,
                AiWeeklyAnalysisState.last_concluded_run_id: None,
            },
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
        f"异常清单 {anomalies} 条、逐轮事件 {events} 条（操作人：{updated_by or '未知'}）",
        "AI",
        force=True,
    )
    return True, f"已清空全部消耗统计：删除 {runs} 次运行记录。", {
        "runs": int(runs or 0),
        "traces": int(traces or 0),
        "anomalies": int(anomalies or 0),
        # 第三张子表也报出来：界面与日志都按这个字典说「删了什么」，
        # 少一个键就等于少说一件事（而它恰恰是最容易被漏的那张）。
        "events": int(events or 0),
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


# ---------------------------------------------------------------------------
#  成本指标（契约 WP9）：三个**只读纯函数**
# ---------------------------------------------------------------------------
#
# 它们不查库、不读时钟、不碰会话：输入是一批 `AiAnalysisRun` / `AiAnalysisJob` 行（或
# 任何带同样属性的对象），输出是普通字典。与 `services/ai/usage.py` 的估算一节同一条
# 纪律 —— 调用方拿到的必须是「已经取好的数」，而不是一条会自己查库的链路。
#
# 本轮**不新增页面、不改路由**（契约 WP9 原文），所以这三个指标今天只有测试在读；它们
# 存在的意义是把「花了多少冤枉钱」变成可测量的数：同一份快照被重复跑了几次、多少运行
# 仍在整窗读、升级原因分布在哪一档。


def _payload_of(run: Any) -> dict:
    """`request_payload` → dict（JSON 文本 / 已解析对象 / 坏数据一律不抛）。"""
    raw = getattr(run, "request_payload", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _snapshot_digest_of(run: Any) -> str:
    """这次运行的**做差快照指纹**；读不出来返回空串（未知不当成 0）。

    优先目标快照 `request_payload.snapshot.content_digest`（2026-11 起每次运行都冻一份）；
    老运行没有那一块，退回 `baseline.content_digest`（那一次是拿哪份快照做差的）。
    """
    payload = _payload_of(run)
    for key in ("snapshot", "baseline"):
        section = payload.get(key)
        if isinstance(section, Mapping):
            digest = str(section.get("content_digest") or "").strip()
            if digest:
                return digest
    return ""


def _scope_of(item: Any) -> str:
    """输入口径：run 行读 `scope`，job 行读 `effective_mode`（两者都缺时看 payload）。"""
    for attr in ("scope", "effective_mode"):
        value = str(getattr(item, attr, "") or "").strip().lower()
        if value:
            return value
    return str(_payload_of(item).get("scope") or "").strip().lower()


def _canonical_upgrade_reason(code: str) -> str:
    """历史值 `first_run` → `no_conclusion_baseline`；其余原样（读侧兼容，契约 1.5）。"""
    return (
        UPGRADE_REASON_NO_CONCLUSION_BASELINE if code == UPGRADE_REASON_FIRST_RUN else code
    )


def _upgrade_reason_of(item: Any) -> str:
    """这条记录能读到的**升级原因**；读不出来返回空串。

    job 行有 `upgrade_reason` 列，直接用（真值）。run 行没有这一列，只能按 payload 还原：
    只有**整窗**运行才谈得上「为什么整窗」，且 `baseline.reason == force_full_rebuild`
    优先于 `policy.reason`（强制重建时后者仍写着首跑口径）。

    **已知边界**：run 行分不出「用户显式要全量」与「真首跑」（`requested_mode` 只在 job
    上）。所以统计升级原因时应尽量喂 job 行；喂 run 行得到的是「为什么整窗」的近似分布。
    """
    raw = str(getattr(item, "upgrade_reason", "") or "").strip().lower()
    if raw:
        return _canonical_upgrade_reason(raw)
    if _scope_of(item) != "full":
        return ""
    payload = _payload_of(item)
    baseline = payload.get("baseline")
    if isinstance(baseline, Mapping):
        base_reason = str(baseline.get("reason") or "").strip().lower()
        if base_reason == FORCE_FULL_REBUILD_REASON:
            return base_reason
    policy = payload.get("policy")
    if isinstance(policy, Mapping):
        return _canonical_upgrade_reason(str(policy.get("reason") or "").strip().lower())
    return ""


def _group_key_of(run: Any) -> str:
    """这条运行属于哪个周版本分组；读不出来返回空串。"""
    group = _payload_of(run).get("group")
    if not isinstance(group, Mapping):
        return ""
    return str(group.get("key") or "").strip()


def duplicate_snapshot_runs(runs: Sequence[Any], *, price_table: Any = None) -> dict[str, Any]:
    """同一份快照被跑了多次的那些组 —— 「同一快照重复付费」的度量。

    分组键是 payload 里的快照指纹（见 `_snapshot_digest_of`）。每组里**第一条**是必要的
    （它产出了结论），第二条起是同一份输入的重复付费：这就是 `wasted_*` 的口径。

    `price_table` 不传时只给 token、**不给金额**（`cost` / `wasted_cost` 是 `None`）：
    没有单价表就没有金额可算，写 0 会把「算不出」说成「没花钱」（与 `usage.aggregate_runs`
    同一条纪律）。没有任何重复时 `wasted_tokens` 是**真的 0**、`wasted_runs` 也是 0。

    返回的每个组都带 `group_keys`：`content_digest` 只哈希文件内容、不含分组键，带上它
    才能一眼看出「重复的是不是同一个周版本」。
    """
    buckets: dict[str, list] = {}
    missing_digest = 0
    for run in runs:
        digest = _snapshot_digest_of(run)
        if not digest:
            missing_digest += 1
            continue
        buckets.setdefault(digest, []).append(run)

    records: list[dict[str, Any]] = []
    wasted_runs: list = []
    for digest, members in buckets.items():
        if len(members) < 2:
            continue
        wasted = members[1:]
        wasted_runs.extend(wasted)
        total = aggregate_runs(members, price_table=price_table)
        wasted_total = aggregate_runs(wasted, price_table=price_table)
        records.append(
            {
                "content_digest": digest,
                "group_keys": sorted({key for key in map(_group_key_of, members) if key}),
                "run_ids": [getattr(run, "id", None) for run in members],
                "runs": len(members),
                "duplicate_runs": len(wasted),
                "tokens": total["tokens"]["total"],
                "wasted_tokens": wasted_total["tokens"]["total"],
                "cost": total["cost"],
                "wasted_cost": wasted_total["cost"],
            }
        )
    # 稳定排序：多用得多的组排前面，同样多时按指纹排 —— 面板/断言不依赖传入顺序。
    records.sort(key=lambda record: (-record["duplicate_runs"], record["content_digest"]))

    if wasted_runs:
        wasted_total = aggregate_runs(wasted_runs, price_table=price_table)
        wasted_tokens = wasted_total["tokens"]["total"]
        wasted_cost = wasted_total["cost"]
    else:
        # 没有重复运行 = 真的没有浪费（0 是确定的数）；金额仍算不出（没有可加的条目）。
        wasted_tokens, wasted_cost = 0, None
    return {
        "runs": len(runs),
        "missing_digest_runs": missing_digest,
        "groups": records,
        "duplicate_groups": len(records),
        "duplicate_runs": len(wasted_runs),
        "wasted_tokens": wasted_tokens,
        "wasted_cost": wasted_cost,
    }


def full_scope_share(runs: Sequence[Any]) -> dict[str, Any]:
    """整窗运行（`scope == "full"`）的占比 —— 「问题 B 还剩多少」的直接度量。

    增量链路的收益**主要不是省钱**，而是不再整窗重跑（审查报告 4.9 的结论修正），所以
    「还有多少比例在整窗读」要能一眼看到趋势。

    `full_share` 在**没有运行**时返回 `None`：分母为 0 没有比例可言，写 0 会被读成
    「一次全量都没有」。认不出口径的运行单独计数（`unknown_scope_runs`），不混进分子分母。
    """
    full = incremental = unknown = 0
    for run in runs:
        scope = _scope_of(run)
        if scope == "full":
            full += 1
        elif scope == "incremental":
            incremental += 1
        else:
            unknown += 1
    total = full + incremental + unknown
    known = full + incremental
    return {
        "runs": total,
        "full_runs": full,
        "incremental_runs": incremental,
        "unknown_scope_runs": unknown,
        # 认不出口径的不进分子分母 —— 把它算进分母会让比例看起来比实际低。
        "full_share": (full / known) if known else None,
    }


def upgrade_reason_distribution(runs: Sequence[Any]) -> dict[str, Any]:
    """升级原因（`upgrade_reason`）的分布 —— 「平台为什么把这次跑成全量」的账。

    原因短码的归一与写侧同一口径：`first_run` 折成 `no_conclusion_baseline`（读侧兼容）。
    没有原因的运行不编一个桶：它们是「不是升级」或「读不出来」，两者都不该冒充某一种原因，
    只计入 `no_reason_runs`（**等于 0 才是「每条都说得清」**）。
    """
    distribution: dict[str, int] = {}
    full_runs = 0
    for run in runs:
        if _scope_of(run) == "full":
            full_runs += 1
        reason = _upgrade_reason_of(run)
        if reason:
            distribution[reason] = distribution.get(reason, 0) + 1
    ordered = dict(sorted(distribution.items(), key=lambda item: (-item[1], item[0])))
    counted = sum(ordered.values())
    return {
        "runs": len(runs),
        "full_runs": full_runs,
        "distributed_runs": counted,
        "distribution": ordered,
        "no_reason_runs": len(runs) - counted,
    }
