#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计划的取数与落库（工作包 B 的接线层）。

## 为什么单独一个文件

`services/ai/auto_sizing.py` 是**纯函数**（不查库、不读文件地算计划），
`services/ai_analysis_service.py` 贴着 2000 行的 ERROR 闸门（新逻辑写进去只会把它顶穿）。
两者之间这一段 —— **从已落库的周版本缓存量出快照事实**、**把计划挂进 payload 再读回**、
**把单次硬上限接成子代理的 `should_skip`** —— 就放在这里。

## 一条口径：计划**只算一次**，两个读者读**同一份**

* 生产者：`build_weekly_payload`（`attach_plan`），算完写进 `payload["plan"]`；
* 读者一：`_run_engine_and_persist`（`plan_of`）—— 它决定这次跑单代理还是家族、几个分片、
  每片多少额度；
* 读者二：`ai_usage_service.analysis_estimate`（`plan_of`）—— 它把同一份计划摆给用户看。

这样「预估说 5 片、实际跑 3 片」这一类分叉在结构上就不可能出现。plan 落库的位置是
`AiAnalysisRun.request_payload`（`payload` 本来就是它的一部分），所以一次跑完的计划
**是可查的**：出问题时能回答「当时是凭什么这么分的工」。

## 差异载荷估算字符数：量的是**载荷**，不是渲染后的 Markdown（名字已如实改过来）

指引要的是「**实际渲染后**的 diff 字符数」。渲染层（表格 → 逐格文本）在本模块里跑不起：
对 1000+ 个文件把每份 `merged_diff_data` 渲染一遍是分钟级的开销，而这个函数在**预估端点
每次打开确认框**时都会被调一次。所以这里量的是**解码后载荷**的字符数
（`json.dumps(payload, ensure_ascii=False)`），并把结果标成 `chars_estimated`。

**字段与文案在 2026-09-24 改成了 `diff_payload_chars` / 「差异载荷估算」。** 原先它叫
`rendered_diff_chars`、各处文案写「渲染后 diff N 字」—— 那是**名不副实**：读的人
（包括实测时看计划理由的人）会以为平台真的渲染过一遍，于是拿这个数去对「模型看到的
diff 有多长」，怎么对都对不上。口径一个字没改，改的是它**自称是什么**：

* 它比渲染后的 Markdown **偏大**（含 JSON 键名与结构），所以门槛判定偏保守 ——
  宁可早一点分工，也不要把一次装不下的输入判成小批次；
* 大版本按**等距抽样**放大（`EVIDENCE_SAMPLE_FILES` 个样本），并把
  `chars_estimated=True` 与 `sample_size` 一起带上 —— 「量出来的」与「推出来的」不许
  看起来一样（与「未上报不许写成 0」同一条纪律）；
* 旧键名 `rendered_diff_chars` 读侧仍然认（`SnapshotFacts.from_mapping`）：已经在库里
  的那几十条运行（含实测的 run 45/46/52/53）的 `snapshot_facts` 都写着旧名字，
  不认就会把它们的事实读成 0，「当时凭什么这么分工」就成了一句空话。

## 不做的事

* 不发起那 1 次受限规划模型调用（它是**付费**的，由调用方在
  `should_consult_planner` 为真时发起，再把建议交回 `plan_analysis(proposal=…)`）；
* 不落新表、不加新列：计划跟着 payload 走（`AiAnalysisRun.request_payload` 是现成的
  `BigText`），`AiAnalysisJob` 上加一列要同时改 `services/db_migration_service.py`，
  那不在本工作包的文件范围内。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Callable, Mapping, Optional, Sequence

from models.weekly_version import WeeklyVersionDiffCache
from services.ai.auto_sizing import (
    AnalysisPlan,
    SnapshotFacts,
    plan_analysis,
    single_run_guard,
)
from services.ai.pricing import BudgetWeights
from utils.logger import log_print

#: 文件数不超过它时**逐个**量 diff 体量（`SMALL_BATCH_MAX_FILES + 1`：小批次判定的边界
#: 正好落在这里，多留一个是为了「9 个文件」时也能给出确切答案而不是估算）。
MAX_EXACT_DIFF_FILES = 9
#: 超过上面那一档时按**等距抽样**放大：抽这么多条，取平均再乘文件数。
EVIDENCE_SAMPLE_FILES = 20


def _count(value: Any) -> int:
    """「命中几个」：给列表就数长度，给数字就用它，给不出就是 0。

    `summary["critical_path_hits"]` 在两个版本里分别是**列表**（命中路径）与计数，
    而这里只关心「命中了几条关键路径」这一个语义 —— 直接 `int()` 会在列表上炸。
    """
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _decoded_chars(raw: Any) -> int:
    """一条 `merged_diff_data` 载荷解码后的字符数（量不出来回 0）。

    **必须解码再量**：库里那一坨是 ASCII 转义的 JSON（中文会写成 `\\uXXXX`），
    直接 `len(字符串)` 会让每个汉字算 6 个字 —— 实测口径下那会把一份 1.3 万字的中文
    配表差异量成 8 万字，正好跨过小批次门槛，于是「3 个文件」被误判成需要分工。
    """
    text = raw if isinstance(raw, str) else ""
    if not text:
        return 0
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        # 解不出来时退回原串长度（偏大，方向保守），并如实带上「这是原始长度」。
        return len(text)
    try:
        return len(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(text)


def _diff_sizes_by_row(row_ids: Sequence[int]) -> dict[int, int]:
    """按缓存行主键量体量。**一次查询**，不逐行查。

    坏行（解析失败/为空）回 0 —— 调用方据 0 走「按文件数摊」的兜底，而不是把它当成
    「这个文件不用读」。
    """
    if not row_ids:
        return {}
    rows = (
        WeeklyVersionDiffCache.query
        .filter(WeeklyVersionDiffCache.id.in_(list(row_ids)))
        .all()
    )
    return {int(row.id): _decoded_chars(row.merged_diff_data) for row in rows}


def _entries_from_delta_files(
    delta_files: Sequence[Mapping[str, Any]], sizes: Mapping[int, int]
) -> tuple[dict, ...]:
    """`payload["delta_files"]` + 体量表 → 每个文件一行元数据。

    **只留规划需要的键**（`auto_sizing._entry` 会再筛一遍）：路径、提交、来源、体量。
    正文一个字都不进来 —— 这是「规划输入不含整表」那句话的来源。
    """
    result: list[dict] = []
    for item in delta_files:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("file_path") or item.get("path") or "").strip()
        if not path:
            continue
        row_id = item.get("cache_row_id")
        chars = sizes.get(int(row_id)) if row_id is not None and int(row_id) in sizes else 0
        result.append(
            {
                "path": path,
                "commit": str(item.get("latest_commit_id") or "").strip(),
                "source": str(item.get("source") or "delta"),
                "chars": chars,
            }
        )
    return tuple(result)


def snapshot_facts_from_payload(
    payload: Mapping[str, Any],
    *,
    fingerprint: str = "",
    critical_paths: int = 0,
) -> SnapshotFacts:
    """已建好的周版本 payload → 快照事实（**唯一的取数入口**）。

    量体量的那条查询在这里，调用方（`build_weekly_payload`）只管把它塞进 payload。
    """
    delta_files = [item for item in (payload.get("delta_files") or ()) if isinstance(item, Mapping)]
    total = len(delta_files)
    summary = payload.get("summary") or {}

    row_ids: list[int] = []
    for item in delta_files:
        row_id = item.get("cache_row_id")
        if row_id is not None:
            row_ids.append(int(row_id))

    if total <= MAX_EXACT_DIFF_FILES:
        # 小批次：**逐个**量。这一档正好是小批次判定的边界，必须准（估算会把 3 个文件的
        # 配置 3 误判成需要分工，而那正是这次要修的那个 bug）。
        sizes = _diff_sizes_by_row(row_ids)
        estimated = False
        sample_size = len(row_ids)
    else:
        # 大版本：等距抽样再放大。**不是**「随便抽前 20 个」—— 清单是按 (优先级, 提交数,
        # 路径) 排过序的，取前 20 个会把排序带来的偏置带进均值。
        step = max(1, total // EVIDENCE_SAMPLE_FILES)
        sampled = row_ids[::step][:EVIDENCE_SAMPLE_FILES]
        measured = _diff_sizes_by_row(sampled)
        per_file = int(sum(measured.values()) / len(measured)) if measured else 0
        # 全部按均值铺一遍，再把**真的量过**的那几条覆盖回去：总和 = 均值 × 文件数，
        # 而量过的那些保持实测值（免得「抽样」把已知的事实也抹成估计）。
        sizes = {row_id: per_file for row_id in row_ids}
        sizes.update(measured)
        estimated = True
        sample_size = len(measured)

    entries = _entries_from_delta_files(delta_files, sizes)
    chars = [int(item.get("chars") or 0) for item in entries]
    return SnapshotFacts.from_mapping(
        {
            "file_count": total,
            "diff_payload_chars": sum(chars),
            "max_file_diff_chars": max(chars) if chars else 0,
            # **输入被截断过时「没有超限」不成立**（清单取样、hunk 截断都会让它为真）。
            "truncated": bool(payload.get("delta_truncated")),
            "chars_estimated": estimated,
            "sample_size": sample_size,
            "fingerprint": str(fingerprint or ""),
            "entries": [dict(item) for item in entries],
            "critical_paths": _count(critical_paths if critical_paths else summary.get("critical_path_hits")),
            "table_files": sum(
                1 for item in entries if str(item.get("path") or "").lower().endswith(
                    (".xlsx", ".xls", ".csv")
                )
            ),
            "code_files": sum(
                1 for item in entries if str(item.get("path") or "").lower().endswith(
                    (".lua", ".py", ".js", ".ts", ".cs", ".cpp", ".h")
                )
            ),
        }
    )


def attach_plan(
    payload: dict,
    *,
    effective_budget: Any,
    dimensions: Sequence[str] = (),
    subagent_enabled: bool = True,
    verify: bool = False,
    proposal: Any = None,
    output_tokens: Optional[int] = None,
    cost_limit: Optional[str] = None,
    fingerprint: str = "",
    budget_weights: "BudgetWeights | None" = None,
) -> AnalysisPlan:
    """算出计划并挂到 payload 上（`payload["plan"]`），返回它。

    先挂**事实**（`payload["snapshot_facts"]`）再挂计划：事实是「这次要分析的是什么」，
    计划是「打算怎么分析」—— 两者都要能在落库的 payload 里读到，因为「为什么这么分工」
    的第一问永远是「当时的输入长什么样」。

    `fingerprint` 不给时用**内容指纹**（`SnapshotFacts.digest()`：文件、体量、路径、
    提交四样拼出来的 sha256）—— 它足够回答「预估那份计划与实际跑的是不是同一份输入」，
    而且是纯本地的（不查库、不看时钟）。

    `budget_weights` 是**记账倍率**（`pricing.budget_weights_from_config(项目配置)`）：
    挂到计划上（`thresholds.budget_weights`）之后，单次预算那一侧（引擎的账本、家族的
    `should_skip`）都从这里读 —— 于是「这次运行按哪份单价折算」与「预估端点显示的是
    哪一份计划」是同一处答案。没配价格表时传 `None`（= 全 1.0 的旧口径）。
    """
    facts = snapshot_facts_from_payload(payload, fingerprint=fingerprint)
    plan = plan_analysis(
        facts,
        effective_budget,
        dimensions,
        subagent_enabled=subagent_enabled,
        verify=verify,
        proposal=proposal,
        output_tokens=output_tokens,
        cost_limit=cost_limit,
    )
    if budget_weights is not None:
        plan.thresholds["budget_weights"] = budget_weights.to_dict()
        # 估算公式那句话也要跟着说 —— 否则界面上写着「单次上限 3,000,000」而运行中被
        # 折算过的数一撞就到顶，读的人只会觉得「上限没生效」。
        plan.estimate["formula"] = (
            str(plan.estimate.get("formula") or "")
            + "；运行中按单价折算为等效 token 记账（缓存命中与输出两档，见"
            " thresholds.budget_weights）"
        )
    payload["snapshot_facts"] = facts.to_dict()
    payload["plan"] = plan.to_dict()
    return plan


def single_member_limits(limits: Any, plan: AnalysisPlan) -> Any:
    """单代理路径的引擎额度 = **计划里那个成员的额度**（不是引擎的出厂默认）。

    「几百个文件却只有一个变更簇」那一档，计划会把额度抬到 10 轮 / ~50 次（小批次那一档
    才是 8/40，见 `auto_sizing._single_plan`）。引擎侧不跟着抬，那次分析会在第 8 轮饿死，
    报告里只能写「额度用尽」—— 而计划上写着 10 轮，又是一次「计划与实际不一致」。

    `max_items` 必须跟着抬：条数上限小于索取次数时，取回来的上下文会被 `enforce_budget`
    按条数**静默裁掉**（付了 50 次索取只带走 20 条）。家族路径不用管这里 —— 每片的额度
    由 `plan.limits` 带进 `run_family`。
    """
    member = plan.members[0]
    return replace(
        limits,
        max_rounds=max(1, int(member.max_rounds)),
        max_tool_requests=max(1, int(member.max_tool_requests)),
        max_items=max(int(getattr(limits, "max_items", 0)), int(member.max_tool_requests)),
    )


def plan_of(payload: Mapping[str, Any] | None, *, default: Any = None) -> Optional[AnalysisPlan]:
    """从 payload 里读回计划（坏数据回 `default`，不抛）。

    **没有计划时不要把 `None` 当成单代理**：调用方要自己决定退回什么（运行侧会现算一份
    并记一条日志 —— 老 payload、被裁过的 payload 都可能走到这里）。
    """
    data = payload if isinstance(payload, Mapping) else {}
    plan = AnalysisPlan.from_dict(data.get("plan"))
    return plan if plan is not None else default


def make_single_run_guard(plan: AnalysisPlan) -> Callable[[Any, int], str]:
    """单次硬上限 → 子代理的 `should_skip(member, spent_tokens)`。

    判据是「已花 + 本轮保守预留 + 收尾预留 > 单次上限」，超过时返回一句**可以直接写进
    报告信息缺口**的话，`run_family` 据此跳过这个成员（**不再新增模型调用**）。

    与 `analysis_budget.early_stop_guard`（月度/项目闸门）是**两把尺子**，调用方把两个
    都接上（任一返回非空就跳过）：那个管「这个月还有钱吗」，这个管「这一次还能花多少」。
    """

    def guard(member: Any, tokens: int) -> str:
        status = single_run_guard(plan, spent_tokens=max(0, int(tokens or 0)))
        if not status["blocked"]:
            return ""
        label = str(getattr(member, "label", "") or "分片")
        log_print(
            f"⏸️ AI 分析：单次预算早停 {label}: {status['reason']}",
            "AI",
            force=True,
        )
        return str(status["reason"])

    return guard


def compose_skip_guards(*guards: Callable[[Any, int], str]) -> Callable[[Any, int], str]:
    """把几个 `should_skip` 合成一个（**第一个非空的理由胜出**）。

    顺序即优先级。合成而不是「让 `run_family` 收一个列表」：那边的签名是既有的
    （`should_skip(member, tokens)`），改签名会同时影响 `analysis_budget` 与既有测试。
    """

    def combined(member: Any, tokens: int) -> str:
        for guard in guards:
            if guard is None:
                continue
            reason = guard(member, tokens)
            if reason:
                return reason
        return ""

    return combined
