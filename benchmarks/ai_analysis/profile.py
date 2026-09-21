#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段级剖析：按阶段（分片 / 汇总 / 复核）产出 token、耗时、覆盖率。

## 数据从哪来（不要再写第二套解析）

| 要什么 | 既有实现 |
|---|---|
| run 级 token / 缓存 / 费用 / 耗时 | `services.ai.usage.usage_from_run` |
| 阶段（分片/汇总/复核）那一行 | `services/ai_usage_service._subagent_rows`（读 `response_payload["subagents"]`） |
| 证据覆盖 / 截断 / 取数失败 | `services.ai.coverage_ledger.build_ledger`（纯函数） |
| trace 明细编解码 | `services.ai.trace_evidence.decode_evidence` / `summarize_executed` |

阶段账的**唯一来源**是 `response_payload["subagents"]`：run 上的
`tokens_input` / `rounds_used` 那些列是**一家子的合计**（n 个分片 + 汇总 + 复核加在
一起），没法从中拆出「汇总阶段花了多少」。所以复测文档 5.2 那张表只能从
`subagents` 里重建 —— 这也是为什么这里必须复用 `_subagent_rows` 而不是自己
`json.loads` 一遍：字段口径（`None` = 未上报、`rounds` 读不出来记 0）在那边定过。

## 阶段归类

`role` 是引擎给的（`services/ai/family_ledger.py`）：`subagent` / `synthesis` / `verify`。
按它归类比按 `label` 猜稳（`label` 是展示名，`S1`/`汇总`/`V1` 这些是约定，
不是契约）。归不进三类的落到 `other`，**不丢**。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

STAGE_ROLES = ("subagent", "synthesis", "verify")
ROLE_LABELS = {
    "subagent": "分片",
    "synthesis": "汇总",
    "verify": "复核",
    "other": "其它",
}


def stage_rows_from_payload(response_payload: Any) -> List[Dict[str, Any]]:
    """从 `response_payload` 取阶段行 —— 直接复用面板那一份实现。"""
    from types import SimpleNamespace

    from services.ai_usage_service import _subagent_rows

    payload = response_payload
    if payload is None:
        return []
    run_like = SimpleNamespace(response_payload=payload)
    return _subagent_rows(run_like)


def stage_rows(run_or_sample: Any) -> List[Dict[str, Any]]:
    """`run` 行或 `BenchmarkSample` 都能用（都只需要 `response_payload` 这一个属性）。"""
    payload = getattr(run_or_sample, "response_payload", None)
    if payload is None:
        run_row = getattr(run_or_sample, "run_row", None)
        if isinstance(run_row, dict):
            payload = run_row.get("response_payload")
        elif hasattr(run_or_sample, "run_like"):
            payload = getattr(run_or_sample.run_like(), "response_payload", None)
    return stage_rows_from_payload(payload)


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sum_with_missing(values: List[Optional[int]]) -> Dict[str, Any]:
    """`(和, 缺失条数)`。缺了就说缺了几条 —— 不折成 0（口径同 `services/ai/usage.py`）。"""
    total = 0
    missing = 0
    for value in values:
        if value is None:
            missing += 1
        else:
            total += value
    return {
        "total": total if missing < len(values) else None,
        "missing": missing,
        "reported": len(values) - missing,
    }


def profile_stages(run_or_sample: Any) -> Dict[str, Any]:
    """按阶段汇总 token / 轮次 / 请求数 / 产出条数，并给出「合计对不对得上 run 行」。

    最后那一步（`reconciliation`）是**这一层最有用的东西**：分片阶段的 token 之和
    与 run 行落库的 token 长期对不上时，说明有一段的账没记下来，而单看任何一张表
    都看不出来 —— 复测文档 5.2 那张表的「合计」与 5.1 的 run 行必须能互相验证。
    """
    rows = stage_rows(run_or_sample)
    by_role: Dict[str, Dict[str, Any]] = {}
    for role in STAGE_ROLES:
        by_role[role] = {
            "label": ROLE_LABELS[role],
            "stages": 0,
            "rounds": 0,
            "requests": 0,
            "tokens_input": [],
            "tokens_output": [],
            "cache_read_tokens": [],
            "anomalies": 0,
            "report_chars": 0,
        }
    other = {
        "label": ROLE_LABELS["other"], "stages": 0, "rounds": 0, "requests": 0,
        "tokens_input": [], "tokens_output": [], "cache_read_tokens": [],
        "anomalies": 0, "report_chars": 0,
    }

    stage_list: List[Dict[str, Any]] = []
    for row in rows:
        role = str(row.get("role") or "")
        bucket = by_role.get(role) or other
        bucket["stages"] += 1
        bucket["rounds"] += _int_or_none(row.get("rounds")) or 0
        bucket["requests"] += _int_or_none(row.get("requests")) or 0
        bucket["tokens_input"].append(_int_or_none(row.get("tokens_input")))
        bucket["tokens_output"].append(_int_or_none(row.get("tokens_output")))
        bucket["cache_read_tokens"].append(_int_or_none(row.get("cache_read_tokens")))
        bucket["anomalies"] += _int_or_none(row.get("anomalies")) or 0
        bucket["report_chars"] += _int_or_none(row.get("report_chars")) or 0
        stage_list.append({
            "label": str(row.get("label") or ""),
            "role": role or "other",
            "status": str(row.get("status") or ""),
            "rounds": _int_or_none(row.get("rounds")),
            "requests": _int_or_none(row.get("requests")),
            "tokens_input": _int_or_none(row.get("tokens_input")),
            "tokens_output": _int_or_none(row.get("tokens_output")),
            "cache_read_tokens": _int_or_none(row.get("cache_read_tokens")),
            "anomalies": _int_or_none(row.get("anomalies")),
            "report_chars": _int_or_none(row.get("report_chars")),
            "skipped_reason": str(row.get("skipped_reason") or ""),
            "error": str(row.get("error") or ""),
        })

    groups: Dict[str, Any] = {}
    for role, bucket in list(by_role.items()) + [("other", other)]:
        if bucket["stages"] == 0:
            continue
        groups[role] = {
            "label": bucket["label"],
            "stages": bucket["stages"],
            "rounds": bucket["rounds"],
            "requests": bucket["requests"],
            "tokens_input": _sum_with_missing(bucket["tokens_input"]),
            "tokens_output": _sum_with_missing(bucket["tokens_output"]),
            "cache_read_tokens": _sum_with_missing(bucket["cache_read_tokens"]),
            "anomalies": bucket["anomalies"],
            "report_chars": bucket["report_chars"],
        }

    return {
        "stages": stage_list,
        "groups": groups,
        "reconciliation": _reconcile(run_or_sample, groups),
    }


def _reconcile(run_or_sample: Any, groups: Dict[str, Any]) -> Dict[str, Any]:
    """阶段之和 vs run 行落库值。对不上就把差额说出来。"""
    from services.ai.usage import usage_from_run

    if hasattr(run_or_sample, "run_like"):
        run_like = run_or_sample.run_like()
    else:
        run_like = run_or_sample
    usage = usage_from_run(run_like)
    run_tokens = usage.get("tokens") or {}

    stage_input = 0
    stage_output = 0
    missing = 0
    for group in groups.values():
        for key, target in (("tokens_input", "input"), ("tokens_output", "output")):
            block = group[key]
            if block.get("missing"):
                missing += block["missing"]
            value = block.get("total")
            if value is None:
                continue
            if target == "input":
                stage_input += value
            else:
                stage_output += value

    def _delta(run_value: Any, stage_value: int) -> Optional[int]:
        if run_value is None:
            return None
        return int(run_value) - stage_value

    return {
        "run_tokens_input": run_tokens.get("input"),
        "run_tokens_output": run_tokens.get("output"),
        "stage_tokens_input": stage_input,
        "stage_tokens_output": stage_output,
        "delta_input": _delta(run_tokens.get("input"), stage_input),
        "delta_output": _delta(run_tokens.get("output"), stage_output),
        "stages_missing_tokens": missing,
        "note": (
            "run 行的 token 列是「一家子合计」，与阶段之和的差额通常是工具调用/重试"
            "那一层没进成员账；差额本身不是错误，但**变大**说明有阶段漏记"
        ),
    }


def record_like(row: Any) -> Any:
    """把「一行 trace」包成 `decode_evidence` 认的形状。

    **这一步不能省。** `trace_evidence.decode_evidence` 是按 **ORM 属性**写的
    （`getattr(row, "executed_json", None)`）。导出的样本里 trace 行是 **dict**，
    `getattr(dict, "executed_json")` 永远取到默认值 `None` —— 于是每一行都被读成
    「这一轮没采集过明细」，证据覆盖整片变成未知，而且**一声不响**。
    """
    if isinstance(row, dict):
        from types import SimpleNamespace

        return SimpleNamespace(**row)
    return row


def coverage_from_traces(trace_rows: List[Dict[str, Any]], request_payload: Any = None,
                         tool_stats: Any = None) -> Dict[str, Any]:
    """用既有的纯函数重建证据覆盖（`coverage_ledger.build_ledger`）。

    `executed=None` 表示「没采集过明细」→ 覆盖报 `None`（未知）；`executed=[]` 表示
    「采集了但一条都没有」。这两者必须分开传，混成同一个值会让「这次没记明细」
    显示成「一个文件都没看」。
    """
    from services.ai.coverage_ledger import build_ledger
    from services.ai.trace_evidence import decode_evidence

    decoded = [decode_evidence(record_like(row)) for row in (trace_rows or [])]
    # 「没采集过明细」= 没有任何一行的 `executed_json` 不是 NULL。这时必须传
    # `executed=None`（未知），不能传 `[]`（采集了但一条都没有）——
    # `coverage_ledger` 对这两个值给的是不同结论，见本模块 docstring。
    if not decoded or not any(
        isinstance(row, dict) and row.get("executed_json") is not None
        for row in trace_rows
    ):
        collected = None
    else:
        collected = []
        for item in decoded:
            collected.extend(item.get("executed") or [])
    return build_ledger(request_payload=request_payload, executed=collected, tool_stats=tool_stats)


def evidence_files_from_traces(trace_rows: List[Dict[str, Any]]) -> List[str]:
    """本次**真的取到过内容**的文件清单（覆盖率分子的来源）。

    口径照抄 `coverage_ledger`：只有 `file_diff` / `file_content` 两类算「看过文件」，
    且必须既不 `failed` 也不 `empty`。少这两道过滤，`find_references` 的失败也会
    被算成「这个文件看过」。
    """
    from services.ai.coverage_ledger import FILE_EVIDENCE_KINDS
    from services.ai.trace_evidence import decode_evidence

    seen: Dict[str, None] = {}
    for row in trace_rows or []:
        if row.get("executed_json") is None:
            continue
        decoded = decode_evidence(record_like(row))
        for item in decoded.get("executed") or []:
            if str(item.get("kind") or "") not in FILE_EVIDENCE_KINDS:
                continue
            if item.get("failed") or item.get("empty"):
                continue
            path = _path_from_label(item.get("label"))
            if path:
                seen.setdefault(path, None)
    return list(seen)


def _path_from_label(label: Any) -> str:
    """逐轮标签 `file_diff <12位提交> <路径>` → 路径。

    用既有的 `parse_evidence_label` 切，不自己 `split()`：路径里有空格，
    `split()[2]` 会把 `【40】怪物表 Object 物件.xlsx` 截断。
    """
    from services.ai.coverage_ledger import parse_evidence_label

    try:
        _commit, path, _lines = parse_evidence_label(label)
    except Exception:
        return ""
    return str(path or "").strip()


def profile_run(run: Any, *, price_table: Any = None) -> Dict[str, Any]:
    """一次运行的全景：run 级用量 + 阶段剖析 + 证据覆盖。

    `run` 可以是 ORM 行、也可以是评测样本（`BenchmarkSample`）—— 后者没有 trace
    明细表，覆盖由样本自带的 `coverage` 直接给出。
    """
    from services.ai.usage import usage_from_run

    if hasattr(run, "run_like"):
        run_like = run.run_like()
    else:
        run_like = run
    usage = usage_from_run(run_like, price_table=price_table)
    profile = profile_stages(run)
    coverage = getattr(run, "coverage", None)
    if not coverage:
        coverage = coverage_from_traces(getattr(run, "trace_rows", []) or [],
                                        request_payload=usage.get("context_chars") and None)
    return {
        "usage": usage,
        "stages": profile,
        "coverage": coverage,
    }
