"""用量口径：把 token / 缓存命中 / 工具调用 / 耗时 / 费用组装成同一个形状。

## 为什么要有这个模块

同一份「这次花了多少」要在三个地方出现：运行抽屉里的一行字、抽屉里点开的分项表、
以及消耗面板的跨项目/跨周版本汇总。三处各写一遍的后果不是代码重复，而是**口径漂移**：
抽屉把「未上报」渲染成 0，面板把它当 0 累加，于是「上游没报缓存」在汇总里变成了
「命中率 0%」——一个用户会当真、而事实相反的结论。

所以三个口径集中写在这里，界面与端点都从这里取：

1. **「缓存命中」指上游 provider 报的 prompt cache 命中 token。** 它与「工具结果在
   *一次分析内*的内存缓存命中」（`EngineOutcome.cache_hits`）是两件不同的事，
   界面上不许都叫「缓存」。
2. **`None` = 上游没报，`0` = 报了且确实是 0。** 命中率在三段里有 `None` 时返回
   `None`（界面显示「未上报」），**绝不返回 0** —— 「没上报」与「一次都没命中」对
   用户的含义正好相反。
3. **金额只由 `pricing.estimate_cost` 算**，算不出就是 `None` + 一句人话理由，
   绝不回落到 0。汇总时只要有**任何一次**运行算不出，合计就是「算不出」。

## 汇总时部分缺失怎么办

不是简单地「有一个 None 就整列 None」（那样一条老数据会让整张面板空白），也不是
「跳过缺失直接加」（那样用户会以为这就是全部）。做法是**如实相加 + 记账缺失**：
每列给出 `..._missing_runs` 计数，命中率只在分子分母都**没有**缺失时才给，否则返回
`None` 并由界面说明「部分运行未上报」。
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from services.ai.pricing import CostEstimate, PriceTable, estimate_cost, money

# 逐次运行的顶格键。前端按这些键读，改这里等于改接口。
USAGE_KEYS = (
    "collected",
    "tokens",
    "cache",
    "rounds",
    "requests",
    "tool_cache_hits",
    "context_chars",
    "duration_ms",
    "tools",
    "cost",
    "model",
    "pricing_version",
)


def cache_hit_rate(hit: int | None, total: int | None) -> float | None:
    """命中缓存的输入 token 占输入总数的比例；任一为 `None`（未上报）返回 `None`。

    输入为 0 时同样返回 `None`：0 做分母没有比例可言，返回 0 会被读成「一次都没命中」。
    """
    if hit is None or total is None or total <= 0:
        return None
    if hit < 0:
        return None
    return hit / total


def _int_or_none(value: Any) -> int | None:
    """把列里的值读成 int；`None` 保持 `None`，读不动也当 `None`（而不是 0）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decoded_tools(raw: Any) -> dict[str, dict[str, int]]:
    """`tool_stats_json` 列 → `{工具类型: {计数器: 值}}`。坏数据当空字典。"""
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        payload = raw
    else:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(payload, Mapping):
        return {}
    result: dict[str, dict[str, int]] = {}
    for kind, counters in payload.items():
        if not isinstance(counters, Mapping):
            continue
        clean = {
            str(name): int(value)
            for name, value in counters.items()
            if _int_or_none(value) is not None
        }
        result[str(kind)] = clean
    return result


def encode_tools(tools: Mapping[str, Mapping[str, int]] | None) -> str | None:
    """`{工具类型: {计数器: 值}}` → 落库用的 JSON 文本。空的时候写 `None`。"""
    if not tools:
        return None
    payload = {
        str(kind): {str(name): int(value) for name, value in counters.items()}
        for kind, counters in tools.items()
        if counters
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else None


def _assemble(
    *,
    tokens_input: int | None,
    tokens_output: int | None,
    cache_read: int | None,
    cache_write: int | None,
    cache_source: str = "",
    rounds: int | None = None,
    requests: int | None = None,
    tool_cache_hits: int | None = None,
    context_chars: int | None = None,
    duration_ms: int | None = None,
    tools: Mapping[str, Mapping[str, int]] | None = None,
    cost: CostEstimate | None = None,
    model: str = "",
    pricing_version: str = "",
) -> dict[str, Any]:
    """逐次运行的用量字典。`collected=False` 表示这次运行没有采集到任何用量。"""
    total = None
    if tokens_input is not None and tokens_output is not None:
        total = tokens_input + tokens_output

    collected = any(
        value is not None
        for value in (tokens_input, tokens_output, cache_read, duration_ms, context_chars)
    ) or bool(tools)

    return {
        "collected": collected,
        "tokens": {
            "input": tokens_input,
            "output": tokens_output,
            "total": total,
            "cache_read": cache_read,
            "cache_write": cache_write,
        },
        "cache": {
            "read": cache_read,
            "write": cache_write,
            "source": cache_source or "",
            "hit_rate": cache_hit_rate(cache_read, tokens_input),
        },
        "rounds": rounds,
        "requests": requests,
        "tool_cache_hits": tool_cache_hits,
        "context_chars": context_chars,
        "duration_ms": duration_ms,
        "tools": {str(kind): dict(counters) for kind, counters in (tools or {}).items()},
        "cost": cost.to_dict() if cost is not None else None,
        "model": model or "",
        "pricing_version": pricing_version or "",
    }


def usage_from_outcome(outcome: Any) -> dict[str, Any]:
    """引擎产出的用量（实时路径：SSE 的 result 事件与刚跑完的那一次）。

    **不带费用**：这一层拿不到项目的价格表，而「算不出」与「这里不负责算」是两件事，
    混在一起会让抽屉显示一句莫名其妙的理由。费用由 `usage_from_run` 在读取侧按
    落库的 token 与当前价格表算。
    """
    return _assemble(
        tokens_input=_int_or_none(getattr(outcome, "prompt_tokens", None)),
        tokens_output=_int_or_none(getattr(outcome, "completion_tokens", None)),
        cache_read=_int_or_none(getattr(outcome, "cache_read_tokens", None)),
        cache_write=_int_or_none(getattr(outcome, "cache_write_tokens", None)),
        rounds=_int_or_none(getattr(outcome, "rounds_used", None)),
        requests=_int_or_none(getattr(outcome, "requests_used", None)),
        tool_cache_hits=_int_or_none(getattr(outcome, "cache_hits", None)),
        context_chars=_int_or_none(getattr(outcome, "context_chars", None)),
        duration_ms=_int_or_none(getattr(outcome, "duration_ms", None)),
        tools=getattr(outcome, "tool_stats", None),
    )


def usage_from_run(run: Any, *, price_table: PriceTable | None = None) -> dict[str, Any]:
    """一条 `AiAnalysisRun` 行的用量（读取侧）。

    历史（用量功能上线前）的行各列都是 NULL → `collected=False`，界面显示「用量未采集」，
    而不是一堆 0。费用按**当前**价格表算，并把该行落库时的 `pricing_version` 一并带出
    去，让界面能说清「这条记录当时的单价表版本」。
    """
    tokens_input = _int_or_none(getattr(run, "tokens_input", None))
    tokens_output = _int_or_none(getattr(run, "tokens_output", None))
    cache_read = _int_or_none(getattr(run, "cache_read_tokens", None))
    cache_write = _int_or_none(getattr(run, "cache_write_tokens", None))
    stored_pricing = str(getattr(run, "pricing_version", "") or "")
    model = str(getattr(run, "model", "") or "")

    cost: CostEstimate | None = None
    if price_table is not None:
        cost = estimate_cost(
            model,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cache_read=cache_read,
            cache_write=cache_write,
            table=price_table,
        )
        if cost.computable and stored_pricing and stored_pricing != cost.price_version:
            # 落库时的单价表与当前这份不是同一版。金额是按**当前**表算的，必须说出来，
            # 否则同一条历史运行在不同时期打开会显示不同金额，而没有任何解释。
            cost = CostEstimate(
                amount=cost.amount,
                currency=cost.currency,
                price_version=cost.price_version,
                matched_pattern=cost.matched_pattern,
                source=cost.source,
                lines=cost.lines,
                notes=(
                    *cost.notes,
                    f"这条记录落库时的单价表版本是 {stored_pricing}，这里是按当前版本"
                    f"（{cost.price_version}）重算的",
                ),
            )

    return _assemble(
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cache_read=cache_read,
        cache_write=cache_write,
        cache_source=str(getattr(run, "cache_source", "") or ""),
        rounds=_int_or_none(getattr(run, "rounds_used", None)),
        requests=_int_or_none(getattr(run, "tool_requests_used", None)),
        context_chars=_int_or_none(getattr(run, "context_chars", None)),
        duration_ms=_int_or_none(getattr(run, "duration_ms", None)),
        tools=_decoded_tools(getattr(run, "tool_stats_json", None)),
        cost=cost,
        model=model,
        pricing_version=stored_pricing,
    )


def _sum_with_missing(values: Iterable[int | None]) -> tuple[int, int]:
    """`(非空值之和, 缺失条数)`。缺失条数要一起报出去，见模块 docstring 末节。"""
    total = 0
    missing = 0
    for value in values:
        if value is None:
            missing += 1
        else:
            total += value
    return total, missing


def aggregate_runs(
    runs: Sequence[Any], *, price_table: PriceTable | None = None
) -> dict[str, Any]:
    """把多次运行汇成一行（跨项目总览 / 单项目 / 单个周版本都用它）。

    返回的形状与逐次运行**同名同义**，外加 `runs` 条数与各列的 `*_missing_runs` 计数。
    这样界面可以用同一段渲染逻辑处理「一行汇总」和「一次运行」，不会出现两套口径。
    """
    usages = [usage_from_run(run, price_table=price_table) for run in runs]

    tokens_input, input_missing = _sum_with_missing(
        item["tokens"]["input"] for item in usages
    )
    tokens_output, output_missing = _sum_with_missing(
        item["tokens"]["output"] for item in usages
    )
    cache_read, cache_read_missing = _sum_with_missing(
        item["tokens"]["cache_read"] for item in usages
    )
    cache_write, cache_write_missing = _sum_with_missing(
        item["tokens"]["cache_write"] for item in usages
    )
    context_chars, context_missing = _sum_with_missing(item["context_chars"] for item in usages)
    duration_ms, duration_missing = _sum_with_missing(item["duration_ms"] for item in usages)

    tools: dict[str, dict[str, int]] = {}
    for item in usages:
        for kind, counters in item["tools"].items():
            bucket = tools.setdefault(kind, {})
            for name, value in counters.items():
                bucket[name] = bucket.get(name, 0) + value

    # 费用：任何一次算不出，合计就不给数字。「部分算得出」的合计是最危险的那种错 ——
    # 它看起来像个完整的金额，实际漏掉了几次运行。
    uncomputable = [item["cost"] for item in usages if item and not _cost_ok(item["cost"])]
    cost = None
    if usages and not uncomputable:
        cost = _sum_costs([item["cost"] for item in usages], price_table)

    return {
        "runs": len(usages),
        "collected_runs": sum(1 for item in usages if item["collected"]),
        "tokens": {
            "input": tokens_input if usages else None,
            "output": tokens_output if usages else None,
            "total": (
                (tokens_input + tokens_output)
                if usages and not input_missing and not output_missing
                else None
            ),
            "cache_read": cache_read if usages else None,
            "cache_write": cache_write if usages else None,
        },
        "cache": {
            # 命中率只在分子分母都**没有**缺失时给出：混着几条未上报的运行算出来的
            # 比例是个偏低的假数字，比「未上报」更容易误导。
            "hit_rate": (
                cache_hit_rate(cache_read, tokens_input)
                if usages and not cache_read_missing and not input_missing
                else None
            ),
            "read": cache_read if usages else None,
            "missing_runs": cache_read_missing,
        },
        "context_chars": context_chars if usages else None,
        "duration_ms": duration_ms if usages else None,
        "tools": tools,
        "cost": cost,
        "missing_runs": {
            "input": input_missing,
            "output": output_missing,
            "cache_read": cache_read_missing,
            "cache_write": cache_write_missing,
            "context_chars": context_missing,
            "duration_ms": duration_missing,
        },
    }


def _cost_ok(cost: Mapping[str, Any] | None) -> bool:
    return bool(cost) and cost.get("amount") is not None


def _sum_costs(
    costs: Sequence[Mapping[str, Any] | None], table: PriceTable | None
) -> dict[str, Any] | None:
    """把逐次运行的费用按档相加（金额是字符串，用 Decimal 加，见 pricing 模块）。"""
    if not costs:
        return None
    lines: dict[str, dict[str, Any]] = {}
    total = Decimal(0)
    currency = ""
    versions: set[str] = set()
    patterns: set[str] = set()
    for item in costs:
        if not item:
            return None
        currency = currency or str(item.get("currency") or "")
        if item.get("price_version"):
            versions.add(str(item["price_version"]))
        if item.get("matched_pattern"):
            patterns.add(str(item["matched_pattern"]))
        total += Decimal(str(item["amount"]))
        for line in item.get("lines") or []:
            bucket = lines.setdefault(
                str(line["label"]),
                {
                    "label": line["label"],
                    "tokens": 0,
                    "unit_price": line.get("unit_price"),
                    "amount": Decimal(0),
                    "note": line.get("note") or "",
                },
            )
            bucket["tokens"] += int(line.get("tokens") or 0)
            bucket["amount"] += Decimal(str(line["amount"]))

    notes: list[str] = []
    if len(versions) > 1:
        notes.append("这几次运行用的不是同一版单价表：" + "、".join(sorted(versions)))
    return {
        "amount": money(total),
        "currency": currency,
        "reason": "",
        "price_version": "、".join(sorted(versions)),
        "matched_pattern": "、".join(sorted(patterns)),
        "source": "project" if table and table.source == "project" else "default",
        "lines": [
            {
                "label": bucket["label"],
                "tokens": bucket["tokens"],
                "unit_price": bucket["unit_price"],
                "amount": money(bucket["amount"]),
                "note": bucket["note"],
            }
            for bucket in lines.values()
        ],
        "notes": notes,
    }
