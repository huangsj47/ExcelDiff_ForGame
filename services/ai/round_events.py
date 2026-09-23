#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐轮事件账本的写与读（模型见 `models/ai_analysis/round_event.py`）。

## 这一层解决什么

运行中，「这次跑到哪了、花了多少」原先只有两个来源，都不够用：

* `ai_analysis_job.progress_json` —— **只有最近 8 轮**（`run_progress.MAX_LIVE_ROUNDS`）
  的一个窗口，而且它是**整条覆盖写**的：早于窗口的轮次看不到，重启就没了；
* `ai_analysis_trace` —— **整次运行跑完**才批量落库，worker 中途被杀就一行都没有。

于是「每个分片各花了多少」这件事在运行中**根本量不出来**，中断之后更是一笔糊涂账。
这一层把每一轮的结束做成一条**独立、幂等、立刻提交**的事件行（见模型 docstring），
于是：运行中能从库里读出**全量**的逐轮进度与逐成员账；进程重启后**按事件恢复**，
不必重跑任何一次模型调用。

## 三个刻意的边界

1. **写事件不许弄挂分析。** `record` 的调用点在付费分析的回调里（
   `run_progress.publish`），所以它跟那一层同一条纪律：任何异常都吞掉、只记一条日志。
   进度可以丢一条，分析不能因为这个白跑。
2. **不在模型调用期间持有写事务。** 每次 `record` 自己开事务、自己提交、立刻结束
   （SQLite 上长时间持有写事务会把整台机器上的其它写挡在门外）。这一条有测试按
   「模型调用进行中，另一个连接能不能写库」的行为验，而不是看代码怎么写。
3. **`None` 是「未上报」，不是 0。** token / 缓存 / 工具计数四组都是：读不出来就是
   `None`，求和之后带一个「这是下界」的标记。`0` 只在库里真的存着 0 时出现。

## 谁调用它

* 写：`services/ai/run_progress.publish`（每一帧进度的唯一写入口，它知道 `run_id`）；
* 读：`run_progress.snapshot` 在内存快照读不到时**从库里补一份**（SSE 断开重连、
  刷新页面、worker 跑在别的进程 —— 这条路径让「重新打开抽屉」看得到已经跑完的轮次）；
* 恢复：`reconcile_interrupted_run` 把事件补成落库的 trace 与 run 汇总（不重跑模型）。
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional, Sequence

from utils.logger import log_print

# 一次快照里最多带几轮。**这个上限与 `run_progress.MAX_LIVE_ROUNDS`（8）不是一个理由**：
# 那个是「每 3 秒重发一次载荷」的省流上限，而这一份是**重新打开抽屉时补的那一次**全量
# 读取 —— 补得只剩 8 轮就等于「已完成的轮次丢了」。取 60 与落库那份 trace 的读取上限
# 同一量级（实测最长的一次运行 34 轮），超出时**如实标** `rounds_truncated`。
MAX_RESTORED_ROUNDS = 60

# 工具明细字段与事件列名的对应（计数）。改这里就要同时看模型那一列。
_COUNT_FIELDS = (
    ("requests", "tool_requests"),
    ("executed", "tool_executed"),
    ("failed", "tool_failed"),
    ("truncated", "tool_truncated"),
    ("refused", "tool_refused"),
    ("dropped", "tool_dropped"),
)

# 逐轮**诊断值**的列名（工作包 F）。这一份白名单与
# `request_fingerprint.RoundDiagnostics.event_fields()` 的键必须**逐字相同** ——
# 两边各写一份、谁也不检查谁，表现就是「界面上那几列永远是空的」而不报错。
# `tests/test_ai_usage_input_breakdown.py` 拿同一张集合把两份钉在一起。
_DIAGNOSTIC_COLUMNS = (
    "reasoning_tokens",
    "usage_source",
    "reasoning_source",
    "model_call_ms",
    "tool_fetch_ms",
    "index_build_ms",
    "request_fingerprint",
    "stable_prefix_fingerprint",
    "prefix_common_messages",
    "prefix_common_chars",
    "prefix_divergence_reason",
    "fingerprint_json",
)

# 成员角色（给界面说人话用）。判据只有位次与标签两条，见 `member_role`。
ROLE_MAIN = "main"
ROLE_SUBAGENT = "subagent"
ROLE_SYNTHESIS = "synthesis"
ROLE_VERIFY = "verify"

# 运行状态里「还活着」的那些（`models/ai_analysis/analysis_run.py` 的 ACTIVE_STATUSES
# 同义；这里**不 import 那一份**是为了让本模块在引擎导入期只依赖纯东西，见模块纪律）。
_LIVE_RUN_STATUSES = frozenset({"running", "pending", "queued", ""})


# ---------------------------------------------------------------------------
#  纯函数：一轮的计数（引擎的钩子用它）
# ---------------------------------------------------------------------------


def round_counts(record: Any) -> Optional[dict]:
    """一轮的 `RoundRecord` → 计数（**纯函数**，不碰数据库）。

    引擎每一轮只调这一个函数（`services/ai/engine.py` 的 `_emit`），所以它必须**廉价且
    不会抛**：读不出来的字段就是 `None`，绝不猜一个 0。

    ## 为什么「取不到几条」必须在这里数出来，而不是在明细里数

    进「思考过程」的明细是**截断过的**（`trace_evidence.LIVE_LIST_MAX_ITEMS = 8`，
    而且 `executed` 只留 failed/empty 的那些）。一份 19 次索取、3 次取数的轮次，明细里
    看到的是 8 条和 0 条 —— 拿它做账必然错。所以计数在这里从**未截断的** `RoundRecord`
    上直接读，明细继续只负责「给人看」。

    `failed` 的判据与 `trace_evidence.summarize_executed` 逐字相同（`failure_notice`
    前缀 + `meta["tool_failed"]`）：那两种形态在计数上必须一致，否则同一个面板上
    「失败 3 条」与明细里列出的那 2 条对不上。
    """
    from services.ai import trace_evidence

    failed = 0
    for item in getattr(record, "executed", ()) or ():
        meta = dict(getattr(item, "meta", None) or {})
        if meta.get("tool_failed") or trace_evidence.failure_notice(
            getattr(item, "text", "")
        ):
            failed += 1
    candidates = getattr(record, "candidates", None)
    return {
        "requests": int(getattr(record, "request_count", 0) or 0),
        "executed": int(getattr(record, "item_count", 0) or 0),
        "failed": failed,
        "truncated": int(getattr(record, "truncated", 0) or 0),
        "refused": int(getattr(record, "refused_by_budget", 0) or 0),
        "dropped": len(getattr(record, "dropped", ()) or ()),
        # `None` = 这一轮不是交结论的那一轮（或引擎没报）—— 与「交了但一条都没有」（0）
        # 是两件事，见模型那一列的说明。
        "candidates": None if candidates is None else int(candidates),
    }


def member_role(member: str, index: int, total: int) -> str:
    """这个成员是干什么的。判据只有两条：**标签空不空**与**位次是不是最后**。

    家族结构是固定的（`services/ai/subagent.py` 的 `build_family_plan`）：分片 `S1..Sn`
    → 汇总（标签为空、位次 `n+1`）→ 可选的 `V1`（位次 `n+2`）。所以：

    * 标签空 + 家族里只有它一个 → **主分析者**（单代理运行也是这个形态）；
    * 标签空 + 还有别人 → **汇总**（它就是这个家族的主代理）；
    * 标签不空 + 位次在最后 → **对账/复核**（`V1`，只在开了 verify 时存在）；
    * 其余 → **分片**。

    **不写死 `"V1"` 这个字面量**：它属于 `services/ai/family_ledger.VERIFY_LABEL`，两边
    各写一份迟早会分叉（那正是这个仓库反复出问题的地方）。这里只用位次推断，因为家族的
    顺序本身就是契约（顺序执行，见 `subagent.run_family`）。
    """
    if not member:
        return ROLE_MAIN if total <= 1 else ROLE_SYNTHESIS
    if total > 1 and index >= total:
        return ROLE_VERIFY
    return ROLE_SUBAGENT


# ---------------------------------------------------------------------------
#  写：一轮一条
# ---------------------------------------------------------------------------


def _frame_value(progress: Any, name: str, default: Any = None) -> Any:
    """帧上的一个字段。**长得像进度就行**（老调用方、测试替身）。"""
    return getattr(progress, name, default)


def _optional_int(value: Any) -> Optional[int]:
    """读一个整数；读不出来（含 `None`）就是 `None`。**不许兜成 0**（见模块纪律 3）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_int(entry: Any, key: str) -> Optional[int]:
    if not isinstance(entry, dict):
        return None
    return _optional_int(entry.get(key))


def _as_int(value: Any, default: int = 0) -> int:
    """读一个整数，读不出来就是 `default`。

    与 `_optional_int` 分开是刻意的：位次、轮次这类**位置**读不出来时给 0（「不知道是第几个」），
    而不是 `None` —— 它们要参与排序与唯一键。**但绝不能让它抛**：`event_fields` 在
    `run_progress.publish` 里是在 try 之外调的，一个脏值抛出去的后果是**整帧进度都不写**
    （快照与事件一起丢），而那正是「脏值只让一个字段缺失」这条纪律要防的事。
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def event_fields(progress: Any) -> Optional[dict]:
    """一帧进度 → 一条事件的列。**不是「一轮」就返回 `None`**（纯函数，不碰数据库）。

    判据只有一个：`index >= 1`。`index == 0` 是引擎的 `on_start` 那一帧 —— 它描述的是
    「还没有任何一轮跑完」，把它记成「第 0 轮」会让「一共跑过几轮」这个数永远是错的。

    ## 逐轮用量从 `round_entry` 读，不从帧上的累计值读

    帧上的 `prompt_tokens` / `completion_tokens` 是**这个成员已经累计的**量（引擎的
    `_sum_optional(prompt_reads)`），而事件是**一轮一行**：把它记进去就等于在每一次
    循环里都写一遍总和，求和时会翻好几倍。真正的本轮值在 `round_entry` 里
    （`trace_evidence.live_round_entry` 读的就是 `RoundRecord` 的本轮字段）。

    没有 `round_entry` 的帧（老调用方、测试替身）→ 用量与计数都记 `None`（未上报），
    **不编一个 0**：一次没上报的调用与一次没花的调用在界面上是两句不同的话。

    ## 诊断三组（指纹 / 推理 token / 三类耗时）同样从帧上读，缺了就是 `None`

    它们是工作包 F 加进来的观测值，来源与上面的用量分开：

    * `reasoning_tokens` / `usage_source` —— 上游报了什么（`None` = 没报）；
    * `model_call_ms` / `tool_fetch_ms` / `index_build_ms` —— 分开的耗时（`None` =
      这一轮没有这一类，例如没跑取数）；
    * 指纹那几列 —— 列名由本模块的 `_DIAGNOSTIC_COLUMNS` 白名单收口（键名不在这里
      拼，见那条常量的说明）。
    """
    index = _optional_int(_frame_value(progress, "index", 0))
    if index is None or index < 1:
        return None
    entry = _frame_value(progress, "round_entry", None)
    counts = _frame_value(progress, "round_counts", None)
    if not isinstance(counts, dict):
        counts = {}
    member = str(_frame_value(progress, "agent", "") or "")
    status = str(_frame_value(progress, "status", "") or "")
    row = {
        "member": member,
        "member_index": _as_int(_frame_value(progress, "agent_index", 0)),
        "member_total": _as_int(_frame_value(progress, "agent_total", 0)),
        "round": index,
        "status": status or None,
        # 与 trace 那一列同义：`unparsable` 是「这一轮没给出能解析的东西」。
        "parsed_ok": status != "unparsable",
        "tokens_input": _entry_int(entry, "tokens_input"),
        "tokens_output": _entry_int(entry, "tokens_output"),
        "cache_read_tokens": _entry_int(entry, "cache_read_tokens"),
        "cache_write_tokens": _entry_int(entry, "cache_write_tokens"),
        # 推理 token 与它的两个来源标记：同样是「上游没报就是 None」（见模块纪律 3）。
        # 真正**直接落列**的那几格在下面由 `_DIAGNOSTIC_COLUMNS` 一次填好；这里不再写一遍
        # （两处各写一份键名，迟早有一边漏掉，而漏掉的那一列在界面上只是永远空着）。
        "duration_ms": _entry_int(entry, "duration_ms"),
        "context_chars": _entry_int(entry, "context_chars"),
        "request_chars": _entry_int(entry, "request_chars"),
        "candidates": None,
        "entry_json": _encode_entry(entry),
    }
    # 诊断值（推理 token / 来源标记 / 三类耗时 / 请求指纹的哈希与计数）：整块来自
    # `RoundDiagnostics.event_fields`（列名与模型逐字对齐），**列名在这里是白名单**。
    diagnostics = _frame_value(progress, "round_diagnostics", None)
    sources = diagnostics if isinstance(diagnostics, dict) else {}
    for column in _DIAGNOSTIC_COLUMNS:
        row[column] = sources.get(column)
    for source, column in _COUNT_FIELDS:
        row[column] = _optional_int(counts.get(source)) if counts else None
    candidates = counts.get("candidates") if counts else None
    row["candidates"] = _optional_int(candidates)
    return row


def _encode_entry(entry: Any) -> Optional[str]:
    """这一轮的明细 → JSON 文本。写不进去就不写（明细不是账，账在列上）。"""
    if not entry:
        return None
    try:
        return json.dumps(entry, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def record(
    run_id: int,
    project_id: Optional[int],
    progress: Any,
    *,
    fields: Optional[dict] = None,
) -> bool:
    """把这一轮写进事件账本。**任何异常都吞掉**（见模块纪律 1）。

    返回「这一行真的写进去了」—— 调用方（`run_progress.publish`）不看这个返回值，
    它给测试与日志用。

    `fields` 是已经算好的 `event_fields(progress)`（调用方要拿同一份行去做内存账本，
    见 `run_progress._JobLedger.events`）。传了就**不再算第二遍**：同一个东西算两遍，
    迟早会有一边改了另一边没改。

    ## 幂等

    按 `(run_id, member, round)` 先查后写：同一个 (成员, 轮次) 重复上报只是**把那一条
    更新一遍**，不会多记一笔。重试、晚到的帧、以及 worker 重启后补发都会走到这条路上。

    ## 事务边界

    自己开、自己提交、立刻结束（模块纪律 2）。提交放在这里而不是留给调用方：调用方是
    引擎的回调，那一段的两侧都是**模型调用**（一次可以是几分钟），把写事务挂在那中间
    就是 SQLite 上最不该出现的那种持有。
    """
    row = fields if fields is not None else event_fields(progress)
    if row is None:
        return False
    try:
        from models import db
        from models.ai_analysis import AiAnalysisJob, AiAnalysisRoundEvent, AiAnalysisRun

        run = db.session.get(AiAnalysisRun, int(run_id))
        if run is None:
            # 库里没有这条运行（测试替身 / 已经过了保留期）：**不写**。事件的意义就是
            # 挂在一次真实运行上，凭空写一行会让「这次跑了多少」多出一笔无主的账。
            return False
        job = (
            AiAnalysisJob.query.filter(AiAnalysisJob.run_id == run.id)
            .order_by(AiAnalysisJob.id.desc())
            .first()
        )
        existing = AiAnalysisRoundEvent.query.filter_by(
            run_id=run.id, member=row["member"], round=row["round"]
        ).first()
        if existing is None:
            existing = AiAnalysisRoundEvent(run_id=run.id, member=row["member"], round=row["round"])
            db.session.add(existing)
        existing.job_id = job.id if job is not None else None
        existing.project_id = (
            int(project_id) if project_id is not None else getattr(run, "project_id", None)
        )
        for key, value in row.items():
            setattr(existing, key, value)
        existing.updated_at = _utcnow()
        db.session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 —— 见模块纪律 1：进度不许弄挂分析
        try:
            from models import db

            db.session.rollback()
        except Exception:  # noqa: BLE001 —— 回滚都失败时只剩日志可做
            pass
        log_print(
            f"⚠️ 写 AI 逐轮事件失败（不影响分析）: run={run_id} 第 {row.get('round')} 轮 "
            f"{type(exc).__name__}：{exc}",
            "AI",
            force=True,
        )
        return False


def reset_recovery_memo() -> None:
    """测试用：忘掉「这个 run 已经补过账了」（测试之间会重用 run id 与共享库）。"""
    with _recovered_lock:
        _recovered.clear()


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
#  读：逐轮、逐成员
# ---------------------------------------------------------------------------


def events_for_run(run_id: int) -> list:
    """这次运行的全部事件，按**家族顺序**排好（成员位次 → 成员内轮次）。

    家族是顺序跑的（`subagent.run_family`），所以「成员位次」就是时间顺序；而
    `round` 是成员内序号。两个键合起来是一条确定的顺序 —— 不需要再存一个「全局轮次」
    列（那个序号要等跑完才编得出来，正是运行中写不了 trace 的原因）。
    """
    try:
        from models.ai_analysis import AiAnalysisRoundEvent

        rows = (
            AiAnalysisRoundEvent.query.filter(AiAnalysisRoundEvent.run_id == int(run_id))
            .order_by(
                AiAnalysisRoundEvent.member_index.asc(),
                AiAnalysisRoundEvent.round.asc(),
                AiAnalysisRoundEvent.id.asc(),
            )
            .all()
        )
        return list(rows)
    except Exception as exc:  # noqa: BLE001 —— 读不到就是「没有事件」，不是错误
        log_print(f"⚠️ 读 AI 逐轮事件失败: run={run_id} {exc}", "AI", force=True)
        return []


def _sum_known(values: Sequence[Optional[int]]) -> tuple[Optional[int], bool]:
    """求和 → `(已知之和 | None, 是不是完整的)`。

    **一次都没上报时返回 `None`（不是 0）**；有缺项时返回已知之和 + `False`（下界）。
    这是全库同一条口径（见 `services/ai/usage.py` 与 `engine._sum_optional`）。
    """
    known = [int(value) for value in values if value is not None]
    if not known:
        return None, False
    return sum(known), len(known) == len(values)


def _field(row: Any, name: str, default: Any = None) -> Any:
    """一行事件的一个字段。**ORM 行与 `event_fields` 的字典都认**。

    两种行必须同时可读，因为逐成员的账有两条输入：写库那一份（重启后从库里读）与
    内存账本里那一份（正在跑、还没落库的）。两个来源过**同一个归约函数**
    （`member_totals`），才不会出现「跑动中显示一个数、重启后显示另一个数」。
    """
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def member_totals(run_id: int, *, rows: Optional[Sequence[Any]] = None) -> list[dict]:
    """这次运行里**每个成员**的账（分片 / 汇总 / 复核各一份）。

    需求里的那一行「每个成员、汇总、复核分别显示：输入/输出/缓存 token、工具请求/
    执行/失败/截断、耗时、候选数」就是读它。两个标记（`tokens_reported` /
    `counts_reported`）是 `None` 口径的落点：**不全就是下界**，界面必须说出来，
    否则「上游没报」会被读成「一个 token 都没花」。

    `rows` 可以传 ORM 行，也可以传内存账本里的字典行（见 `_field`）。
    """
    source = list(rows) if rows is not None else events_for_run(run_id)
    buckets: dict[tuple[str, int], list] = {}
    order: list[tuple[str, int]] = []
    for row in source:
        member = str(_field(row, "member", "") or "")
        round_no = _as_int(_field(row, "round", 0))
        member_key = (member, _as_int(_field(row, "member_index", 0)))
        if member_key not in buckets:
            buckets[member_key] = []
            order.append(member_key)
        buckets[member_key].append((round_no, row))

    out = []
    for member_key in order:
        member, index = member_key
        items = [row for _round_no, row in sorted(buckets[member_key], key=lambda item: item[0])]
        total = max(_as_int(_field(item, "member_total", 0)) for item in items)
        tokens_in, tokens_ok = _sum_known([_field(item, "tokens_input") for item in items])
        tokens_out, out_ok = _sum_known([_field(item, "tokens_output") for item in items])
        cache_read, _cache_read_ok = _sum_known(
            [_field(item, "cache_read_tokens") for item in items]
        )
        cache_write, _cache_write_ok = _sum_known(
            [_field(item, "cache_write_tokens") for item in items]
        )
        duration, _duration_ok = _sum_known([_field(item, "duration_ms") for item in items])
        # 推理 token 与模型调用耗时：逐成员各一份。推理这一格**只有上游报过的轮次才算
        # 得出来**（`None` = 一轮都没报，不是 0），所以它与 tokens 一样带「是不是完整的」
        # 判断；模型调用耗时是本地量的，取「所有轮次的已知值之和」。
        reasoning, reasoning_ok = _sum_known(
            [_field(item, "reasoning_tokens") for item in items]
        )
        # 候选数另有一条口径：**只有交结论的那一轮才有值**（其余轮是 `NULL` = 「这一轮不是
        # 交结论的那一轮」，不是「没上报」）。所以判据是「有没有哪一轮报过」，
        # 而不是「每一轮都报了」—— 否则每个成员都会显示成「候选数未上报」。
        candidate_values = [
            _field(item, "candidates") for item in items
            if _field(item, "candidates") is not None
        ]
        candidates = sum(candidate_values) if candidate_values else None
        candidates_ok = bool(candidate_values)
        last = items[-1]
        out.append({
            "member": member,
            "member_index": index,
            "member_total": total,
            "role": member_role(member, index, total),
            "rounds": len(items),
            "last_status": str(_field(last, "status", "") or ""),
            "tokens_input": tokens_in,
            "tokens_output": tokens_out,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            # 输入与输出都要齐全才算「这个成员的账是全的」。
            "tokens_reported": bool(tokens_ok and out_ok),
            # 输出里有多少是隐藏推理（`None` = 这个成员一轮都没报过；不全时是下界）。
            "reasoning_tokens": reasoning,
            "reasoning_reported": bool(reasoning_ok),
            "duration_ms": duration,
            # 逐成员的模型调用耗时（三类耗时里唯一能跨成员横比的那一项：指引实测本样本
            # 99.7% 的时间在模型调用链上，见 `services/ai/request_fingerprint.py` 的模块文档）。
            "model_call_ms": _sum_known([_field(item, "model_call_ms") for item in items])[0],
            # 候选数只在「交结论的那一轮」有值：一轮都没有就是 `None`（未上报），
            # 交过但一条都没有才是 0。
            "candidates": candidates,
            "candidates_reported": bool(candidates_ok),
            "tool_requests": _sum_known([_field(item, "tool_requests") for item in items])[0],
            "tool_executed": _sum_known([_field(item, "tool_executed") for item in items])[0],
            "tool_failed": _sum_known([_field(item, "tool_failed") for item in items])[0],
            "tool_truncated": _sum_known([_field(item, "tool_truncated") for item in items])[0],
            "tool_refused": _sum_known([_field(item, "tool_refused") for item in items])[0],
            "tool_dropped": _sum_known([_field(item, "tool_dropped") for item in items])[0],
            "counts_reported": all(
                _field(item, "tool_requests") is not None for item in items
            ),
            "context_chars": _sum_known([_field(item, "context_chars") for item in items])[0],
        })
    return out


def _rounds_from_events(rows: Sequence[Any]) -> tuple[tuple[Any, ...], int, bool]:
    """事件 → 抽屉渲染器认的那种逐轮条目（**取最近 `MAX_RESTORED_ROUNDS` 轮**）。

    返回 `(rounds, 一共几轮, 是不是被截断了)`。条目的形状来自落库的 `entry_json`，
    也就是 `trace_evidence.live_round_entry` 的产物 —— 与实时那一份逐字同形，所以
    抽屉里跑动中与跑完之后是同一种渲染（那条契约由
    `tests/test_ai_live_thinking_snapshot.py` 钉着）。
    """
    seen = len(rows)
    tail = list(rows)[-MAX_RESTORED_ROUNDS:]
    items = []
    for row in tail:
        entry = _decode_entry(_field(row, "entry_json"))
        if entry is None:
            entry = {}
        # 分片位次与成员内轮次由**事件行的列**补齐（`entry_json` 里可能有，也可能没有
        # —— 老帧、测试替身）。这与 `run_progress._merge_rounds` 补的是同一对键。
        entry.setdefault("round_index", int(_field(row, "round", 0) or 0))
        if not entry.get("agent"):
            entry["agent"] = str(_field(row, "member", "") or "")
        if not entry.get("agent_round"):
            entry["agent_round"] = int(_field(row, "round", 0) or 0)
        index = int(_field(row, "member_index", 0) or 0)
        if index > 0:
            entry.setdefault("agent_index", index)
        total = int(_field(row, "member_total", 0) or 0)
        if total > 0:
            entry.setdefault("agent_total", total)
        items.append(entry)
    return tuple(items), seen, seen > len(items)


def _decode_entry(raw: Any) -> Optional[dict]:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
#  从事件恢复一份进度快照（SSE 断开重连 / 重启后重新打开抽屉）
# ---------------------------------------------------------------------------


def restored_snapshot(run_id: int):
    """从事件账本拼一份 `run_progress.ProgressSnapshot`。**读不到就是 `None`**。

    ## 只服务「还没有落库结论」的运行

    跑完的运行，它的逐轮明细在 `ai_analysis_trace` 里（`_persist_outcome` 写的），界面
    该读那一份；再从事件拼一份进度出来，抽屉会在「分析中」与「已结束」之间来回跳。
    而「还没落库结论」有两种：**正在跑**（本进程没有它的内存快照：跑在别的 worker 里、
    或者页面刚刷新）与**被中断了**（worker 被杀，账只在事件里）。这两种正是要补快照的
    场合 —— 判据是「有没有落库的结论」（`response_payload`），不是「状态是不是 running」，
    因为被中断的那条运行在平台自己的启动扫描里已经被判成 failed 了
    （`services/ai/run_cache_source.fail_orphaned_analysis_runs`）。

    ## 恢复的账**不重跑任何模型调用**

    这一条是需求的硬要求（「不重做已完成的模型调用，别重复计费」）：本函数只读库。
    """
    try:
        from models import db
        from models.ai_analysis import AiAnalysisRun

        run = db.session.get(AiAnalysisRun, int(run_id))
        if run is None:
            return None
        if getattr(run, "response_payload", None):
            # 已经有落库的结论：进度那一份该让位（见 docstring）。
            return None
        rows = events_for_run(run_id)
        if not rows:
            return None
        snapshot = snapshot_from_events(run, rows)
        _recover_if_dead(run, run_id)
        return snapshot
    except Exception as exc:  # noqa: BLE001 —— 补不到快照就是「读不到进度」，不是错误
        log_print(f"⚠️ 从事件恢复 AI 进度失败: run={run_id} {exc}", "AI", force=True)
        return None


# 已经在这一个进程里恢复过的 run。**只是省一次查询**：真正的判据在
# `reconcile_interrupted_run` 的护栏里（幂等），这一层不是并发保护。
_recovered: set[int] = set()
_recovered_lock = threading.Lock()


def _recover_if_dead(run: Any, run_id: int) -> None:
    """**发现这条运行已经死了的那一刻**，把事件账本补成落库的 trace 与 run 汇总。

    ## 为什么触发点在这里

    「worker 被杀」这件事平台自己知道两个时机：启动扫描
    （`run_cache_source.fail_orphaned_analysis_runs` 把 running 判成 failed）与「有人再问起
    这条运行」——而后者就是这里：内存快照没有、库里只有事件，说明写它的那个进程不在了。
    在此之前 trace 一行都没有（`_persist_outcome` 是整次跑完才写的），run 上的 token 三列
    还是 NULL：用量页、覆盖账本、`/runs/<id>/usage` 全部读不到那份账。

    ## 只有「确定已经死了」才动手

    `reconcile_interrupted_run` 的第一条护栏就是「运行还活着（`running`）不动它」——
    跑在别的 worker 里的分析每次读进度都会经过这里，护栏保证它不会被本地补写一遍
    （补写会与那边最后落库的 trace 撞上 `uq_ai_trace_run_round`）。

    ## 不重跑模型

    `reconcile_interrupted_run` 只读事件、只写 trace/run，一次网络调用都没有 ——
    这就是需求里那句「重启后按事件恢复，不重做已完成的模型调用（别重复计费）」。

    ## 幂等由护栏兜，不由下面那个集合兜

    第二次调用会被护栏 2（「已经有 trace 行」）挡住；集合只是让**同一个进程里反复读进度**
    不再去查那两次库。第一次没恢复成（例如运行当时还活着）**不会**被记成「试过了」——
    记成试过了，这条运行在这一个进程里就再也没机会被补账了。
    """
    key = int(_field(run, "id", 0) or 0)
    with _recovered_lock:
        if key in _recovered:
            return
    outcome = reconcile_interrupted_run(key)
    if outcome.get("reconciled") or outcome.get("reason") in ("already_recorded",):
        with _recovered_lock:
            _recovered.add(key)


def _pending_usage(run: Any) -> bool:
    """这条运行的事件账**还没被预算那一档算进去**吗？（是 → 快照顶上可以报用量）

    ## 判据为什么是 run 上的那两列

    `analysis_budget._evaluate_scope` 算「本周期已用 token」就是
    Σ(run.tokens_input + run.tokens_output)（实测：本模块的
    `test_the_usage_is_not_folded_in_twice_once_it_is_persisted` 用它把「折一次」与
    「折两次」分开）。所以这两列**一有值**，事件里这份账就已经在判定里了；此时再把它当
    「本次运行尚未落库的用量」报出去，`routes/ai_analysis_routes.ai_run_progress` 会在
    它上面再加一遍 —— 同一笔用量算两次。

    跑动中这两列是 NULL（引擎是**整次跑完**才 `_persist_outcome` 的），所以正常跑的那条
    路上这个判据恒为真、行为与以前一字不差；被补过账的中断运行（`reconcile_interrupted_run`
    填的就是这两列）则自动不再折第二遍。

    ## 与「报账」分开

    这里**只管**顶上那两个「本次运行已用」的字段。逐成员的账（`members`）不受影响：
    它是这条运行已发生的账，落库前后都该照报（界面要靠它画「每个成员分别花了多少」）。
    """
    return (
        _field(run, "tokens_input") is None and _field(run, "tokens_output") is None
    )


def snapshot_from_events(run: Any, rows: Sequence[Any]):
    """事件行 → `ProgressSnapshot`。**纯拼装**（`run` 只用来读状态与项目号）。

    `rows` 可以是 ORM 行，也可以是内存账本里的字典行（见 `_field`）—— 两种输入拼出来的
    是同一份快照，界面上因此只有一个说法。

    ## 报账与「还没落库的那一份」是两件事

    逐成员的账（`members`）**总是**照报：它是这次分析已发生的账，与落没落库无关。
    但快照顶上那两个「本次运行已用」的字段（`job_tokens` / `prompt_tokens` …）另有口径：
    它们只报**预算那一档还没算进去的那部分**（见 `_pending_usage`）。混为一谈的后果是
    同一笔用量被算两次 —— 那正是这条运行被 `reconcile_interrupted_run` 补账之后的状态。
    """
    from services.ai.run_progress import ProgressSnapshot

    rounds, seen, truncated = _rounds_from_events(rows)
    totals = member_totals(int(_field(run, "id", 0) or 0), rows=rows)
    pending = _pending_usage(run)
    last = rows[-1]
    current = next(
        (
            item
            for item in totals
            if item["member"] == str(_field(last, "member", "") or "")
            and item["member_index"] == int(_field(last, "member_index", 0) or 0)
        ),
        None,
    )
    reported = [item for item in rows if _field(item, "tokens_input") is not None]
    job_tokens = (
        sum(
            int(_field(item, "tokens_input") or 0) + int(_field(item, "tokens_output") or 0)
            for item in rows
        )
        if (reported and pending)
        else None
    )
    partial = bool(pending and len(reported) != len(rows))
    status = str(_field(last, "status", "") or "")
    return ProgressSnapshot(
        run_id=int(_field(run, "id", 0) or 0),
        project_id=int(_field(run, "project_id", 0) or 0),
        # 「第几轮」= 最后完成的那一轮（当前成员内的序号）—— 界面那句「第 N/M 轮」
        # 读的就是它。`max_rounds` 不从事件里编：那个数只有引擎知道（它不在事件列上），
        # 给 0 时界面会只写「第 N 轮」，那是实话。
        index=int(_field(last, "round", 0) or 0),
        max_rounds=0,
        status=status,
        # **当前成员**的那几个数只在与 `job_tokens` 同一口径下给（见 `_pending_usage`）：
        # 路由是拿它们按价格表折算「本次运行尚未落库的费用」的，不一起收住的话，
        # token 那一档没收住、费用那一档照样会折第二遍。
        prompt_tokens=(current or {}).get("tokens_input") if pending else None,
        completion_tokens=(current or {}).get("tokens_output") if pending else None,
        cache_read_tokens=(current or {}).get("cache_read_tokens") if pending else None,
        cache_write_tokens=(current or {}).get("cache_write_tokens") if pending else None,
        requests_used=int((current or {}).get("tool_requests") or 0),
        requests_remaining=0,
        items_chars=int(_field(last, "context_chars", 0) or 0),
        # 墙钟耗时不可恢复（事件是逐轮记的），这里给的是**已知的模型调用耗时之和**的
        # 下界。界面不用这个字段（`ai_think_log` / `ai_stream_status` 都不读它）。
        elapsed_ms=int(
            sum(int(_field(item, "duration_ms") or 0) for item in rows)
        ),
        updated_at=_monotonic_now(),
        agent=str(_field(last, "member", "") or ""),
        agent_index=int(_field(last, "member_index", 0) or 0),
        agent_total=int(_field(last, "member_total", 0) or 0),
        rounds=rounds,
        rounds_seen=seen,
        rounds_truncated=truncated,
        job_tokens=job_tokens,
        job_tokens_partial=bool(partial),
        # 最后完成的那一轮不是结论轮 → 这个成员可能还有一次调用在飞（跑在别的进程里时
        # 我们看不见它）。是结论轮就说明这个成员不再发请求了。
        job_tokens_pending_call=status != "final",
        members=tuple(totals),
        source="ledger",
        run_status=str(_field(run, "status", "") or ""),
        run_finished=bool(_field(run, "finished_at", None)),
    )


def _monotonic_now() -> float:
    import time

    return time.monotonic()


# ---------------------------------------------------------------------------
#  恢复：把事件补成落库的账（不重跑模型）
# ---------------------------------------------------------------------------

# 被中断的那条运行在界面上要说的话。写成常量是因为它要进 `run.error_message`，而那一段
# 会被导出与面板一起读 —— 两处各写一句会分叉。
INTERRUPTED_MESSAGE = (
    "平台重启（worker 被中断），本次分析没有跑完。已经完成的轮次与用量已经按逐轮事件"
    "账本补记，**没有重跑任何模型调用**；可以重新发起一次分析。"
)


def reconcile_interrupted_run(run_id: int) -> dict:
    """把事件账本补成落库的 `ai_analysis_trace` 与 run 汇总。**不调用模型。**

    ## 为什么需要它

    `ai_analysis_trace` 是整次跑完才批量写的，所以 worker 被杀时它一行都没有；
    而 run 上的 token 三列也还是 NULL。事件账本是逐轮写的，于是**账还在**——这一步把它
    搬进那两处正常读取路径（用量页、覆盖账本、`/runs/<id>/usage` 都读 trace 与 run）。

    ## 三条护栏（每一条都对着一种「会重复计费 / 覆盖真数据」的错法）

    1. **运行还活着就不动它**（`running`）——另一个进程可能正在写它自己的 trace；
    2. **已经有 trace 行就不动它** —— 正常跑完的运行全部在一条事务里写过，所以
       「一行都没有」与「全都写了」是仅有的两种形态（半份不存在）；
    3. **run 上已有的账不覆盖**（只填 NULL）—— 覆盖等于把一次真实的账改小或改大。

    返回一份可进日志的账：`{"reconciled": bool, "reason": str, "rounds": N, ...}`。
    幂等：第二次调用走护栏 2，什么都不写。
    """
    try:
        from models import db
        from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace

        run = db.session.get(AiAnalysisRun, int(run_id))
        if run is None:
            return {"reconciled": False, "reason": "run_not_found"}
        status = str(getattr(run, "status", "") or "").strip().lower()
        if status in _LIVE_RUN_STATUSES:
            return {"reconciled": False, "reason": "still_running"}
        existing = AiAnalysisTrace.query.filter(AiAnalysisTrace.run_id == run.id).count()
        if existing:
            return {"reconciled": False, "reason": "already_recorded", "trace_rows": existing}
        rows = events_for_run(run.id)
        if not rows:
            return {"reconciled": False, "reason": "no_events"}

        totals = member_totals(run.id, rows=rows)
        # 逐轮的**家族全局**序号：与 `subagent._merge_rounds` 编出来的那一套同序
        # （按家族顺序遍历成员、成员内按轮次升序），所以 `uq_ai_trace_run_round` 对得上。
        for position, row in enumerate(rows, start=1):
            db.session.add(
                AiAnalysisTrace(
                    run_id=run.id,
                    round_index=position,
                    agent=str(getattr(row, "member", "") or "") or None,
                    agent_round=int(getattr(row, "round", 0) or 0) or None,
                    outcome=str(getattr(row, "status", "") or "") or None,
                    parsed_ok=bool(getattr(row, "parsed_ok", False)),
                    tokens_input=getattr(row, "tokens_input", None),
                    tokens_output=getattr(row, "tokens_output", None),
                    cache_read_tokens=getattr(row, "cache_read_tokens", None),
                    cache_write_tokens=getattr(row, "cache_write_tokens", None),
                    request_chars=getattr(row, "request_chars", None),
                    context_chars=getattr(row, "context_chars", None),
                    duration_ms=getattr(row, "duration_ms", None),
                    error="worker 被中断（平台重启），这一轮的账来自逐轮事件账本。",
                )
            )
        # run 上那几列只填 NULL（护栏 3）。**求和口径与 `_persist_outcome` 一致**：
        # 任一成员没上报，整次就是 None（不是 0）。
        every = {
            "tokens_input": _sum_known([item["tokens_input"] for item in totals])[0],
            "tokens_output": _sum_known([item["tokens_output"] for item in totals])[0],
            "cache_read_tokens": _sum_known([item["cache_read_tokens"] for item in totals])[0],
            "cache_write_tokens": _sum_known([item["cache_write_tokens"] for item in totals])[0],
            "tool_requests_used": _sum_known([item["tool_requests"] for item in totals])[0],
            "context_chars": _sum_known([item["context_chars"] for item in totals])[0],
        }
        filled = []
        for column, value in every.items():
            if getattr(run, column, None) is None and value is not None:
                setattr(run, column, value)
                filled.append(column)
        if getattr(run, "rounds_used", None) in (None, 0):
            run.rounds_used = len(rows)
            filled.append("rounds_used")
        if not str(getattr(run, "error_message", "") or "").strip():
            run.error_message = INTERRUPTED_MESSAGE
            filled.append("error_message")
        db.session.commit()
        log_print(
            f"🧾 AI 分析恢复：run={run.id} 从逐轮事件账本补回 {len(rows)} 轮"
            f"（{len(totals)} 个成员，字段 {filled}），未重跑任何模型调用",
            "AI",
            force=True,
        )
        return {
            "reconciled": True,
            "reason": "recovered",
            "rounds": len(rows),
            "members": len(totals),
            "filled": filled,
        }
    except Exception as exc:  # noqa: BLE001 —— 恢复失败不该把读进度那条路弄挂
        try:
            from models import db

            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        log_print(f"⚠️ AI 分析恢复失败: run={run_id} {exc}", "AI", force=True)
        return {"reconciled": False, "reason": f"error:{type(exc).__name__}"}


def forget_run(run_id: int) -> int:
    """删掉这次运行的全部事件行，返回删了几条。**幂等。**

    保留期清理（`services/ai/run_cache_source`）删 run 时会顺手清 trace / anomaly，
    本表没有外键（见模型 docstring），所以**需要显式调一次**。那一段不在本工作包的
    文件主权内，所以这里先把入口留好并在报告里点名 —— 没接上的后果不是错数据，而是
    被清理掉的运行会留下几行事件垃圾。
    """
    try:
        from models import db
        from models.ai_analysis import AiAnalysisRoundEvent

        rows = AiAnalysisRoundEvent.query.filter(
            AiAnalysisRoundEvent.run_id == int(run_id)
        ).all()
        if not rows:
            return 0
        for row in rows:
            db.session.delete(row)
        db.session.commit()
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        try:
            from models import db

            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        log_print(f"⚠️ 清 AI 逐轮事件失败: run={run_id} {exc}", "AI", force=True)
        return 0
