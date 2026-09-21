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
   用户的含义正好相反。分子大于分母时也返回 `None`（见 `cache_hit_rate`）：那种数据
   说明上游的两个字段**不是同一口径**，此时任何比例都是错的，而「命中率 900%」看起来
   像我们算错了。
3. **金额只由 `pricing.estimate_cost` 算**，算不出就是 `None` + 一句人话理由，
   绝不回落到 0。汇总时只要有**任何一次**运行算不出，合计就是「算不出」。

## 汇总时部分缺失怎么办

不是简单地「有一个 None 就整列 None」（那样一条老数据会让整张面板空白），也不是
「跳过缺失直接加」（那样用户会以为这就是全部）。做法是**如实相加 + 记账缺失**：
每列给出 `..._missing_runs` 计数，命中率只在分子分母都**没有**缺失时才给，否则返回
`None` 并由界面说明「部分运行未上报」。

## 「全体齐全时的精确值」与「已上报样本」是并列的两组数（2026-09-21 补）

上面那套「缺失就不给值」的口径本身没错（跳过一条算不出的运行，得到的金额看起来完全
正常、实际漏掉了几次真花掉的钱），但它有一个真实世界的后果：**一个历史缺失值会永久
抹掉其余全部有效统计**。实测库里 20 次已完成运行中有 5 次（失败在半路）token 三列全是
NULL，于是 `tokens.total` / `cache.hit_rate` / `cost` **三者同时**变成 `None`，
整屏「未上报」—— 而另外 15 次的数据一直好好的。

所以 `aggregate_runs` 现在给出**两组并列**的数字，而不是把旧口径放宽：

| 字段 | 回答的问题 | 缺失时 |
|---|---|---|
| `tokens` / `cache` / `cost` | 「全体齐全时的精确合计」 | `None`（**一个字都没放宽**） |
| `reported_samples.*.known_value` | 「**已上报的那几次**合计是多少，占几次」 | 保证是**下界**，带覆盖度 |

两者的区别不是精度，是**主张**：前者主张「这就是总额」，所以它必须真的齐全；后者主张
「这是已知的部分，另有 N 次没上报」，所以它可以在不撒谎的前提下给出数字。界面显示后者的
时候必须同时显示 `reported_runs / total_runs`，费用那一格还要写明「已知最低费用」——
**拿未知当 0 仍然禁止**（那正是旧口径要挡的东西，它在这里换了个位置）。

`known_value` 的形状与旧字段逐字相同（token 是 int、命中率是 0~1 的 float、费用是
`CostEstimate.to_dict()`），前端可以用同一段渲染逻辑处理两者。

## 执行**前**的估算（AI-P1-03，同一个模块）

上面算的都是「已经花掉的」。`estimate_analysis()` 回答的是「下一次大概要花多少」——
它必须在**建 job、产生消耗之前**就能给出，所以：

* 它是一个**纯函数**：输入（目标文件数、有效模式、有没有可复用基线、最近同类运行、
  分片数、价格表）→ 输出区间。不查库、不读配置、不看时钟。第二波的 Job 协议要把
  `planned_tokens_low/high` 写进 `ai_analysis_job`，直接调它即可（取数由调用方做）。
* 它**只给区间，不给单点**。单点估算会被读成承诺，而实际用量取决于模型索取了几次、
  缓存命中了多少 —— 那是我们控制不了的部分。
* 价格表没配时**照样给 token 与时间区间**，费用那一格是「算不出 + 一句理由」。
  `DEFAULT_PRICE_TABLE` 是空的，这是常态而非异常（`pricing` 模块 docstring 第 1 条）。

区间怎么来的：**两端都锚在真实观测上**，不引入凭空的系数 —— 低端是「按文件数折算的
最省的一次」，高端是「最费的一次」，两者都取自同一批最近同类运行。唯一的例外是
增量没有历史样本时（这种情况很多：库里全是全量运行），高端取「该模式实测的最低值」，
理由是「最坏情况无非是它基本按全量跑了一遍」—— 这句话本身也要写进 `notes`。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

# 两种模式的字面量从 job 模块拿（合同第 2.3 节：`MODE_*` 只在那一处定义，别处不许重写）。
# 只读导入，不写那个模块。
from models.ai_analysis.job import MODE_FULL, MODE_INCREMENTAL
from services.ai.pricing import (
    CostEstimate,
    PriceTable,
    amount_exact,
    amount_of,
    estimate_cost,
    money,
)

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

    分母是 `tokens_input`（`prompt_tokens`），也就是 **pricing.estimate_cost 认的那个
    「输入总数」**（OpenAI / DeepSeek 的口径：`prompt_tokens` 已经含命中部分，
    `prompt_cache_hit_tokens + prompt_cache_miss_tokens == prompt_tokens`）。
    两处口径必须一致，否则「命中率 90%、费用却按全价算」这种自相矛盾的展示就会出现。

    输入为 0 时同样返回 `None`：0 做分母没有比例可言，返回 0 会被读成「一次都没命中」。

    **分子大于分母时也返回 `None`。** 命中率超过 100% 是不可能的，出现它只说明上游给的
    两个字段不是同一口径 —— 已知的一种是 Anthropic 风格：`usage.input_tokens` 不含命中
    部分（命中数在 `cache_read_input_tokens` 里），而我们的 `prompt_tokens` 是按
    OpenAI/DeepSeek 口径读的。**2026-09-18 在本机 DeepSeek 形态的网关上实测，
    `prompt_tokens` 恒等于命中 + 未命中，所以这条路径在本平台当前端点上不会触发。**
    但真触发时，拿它做比例会算出「命中率 900%」这种数字 —— 那比「未上报」更容易误导，
    因为用户会以为是我们算错了（其实确实是数据对不上）。所以宁可说「未上报」。

    顺带一提：同样的数据在费用那一侧被 `pricing.estimate_cost` 按「全部命中」处理并带了
    一句说明。这里不给比例、那边给个偏保守的金额，是因为费用可以说明「我做了假设」，
    而一个百分比没有地方挂那句话。
    """
    if hit is None or total is None or total <= 0:
        return None
    if hit < 0 or hit > total:
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


def _sample_counts(total_runs: int, reported_runs: int) -> dict[str, int]:
    """一组「已上报样本」的覆盖度。**三个口径各有一份**，不共用。

    不共用是刻意的：一次运行可能报了 token 却没报缓存字段 —— 它算 token 口径的
    「已上报」，不算缓存口径的。合成一个数字的后果是某一格的比例拿两批不同的运行去算，
    而那正是「部分缺失」最容易算错、又最看不出来的地方。
    """
    return {
        "total_runs": total_runs,
        "reported_runs": reported_runs,
        "unknown_runs": max(0, total_runs - reported_runs),
    }


def _token_sample(usages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """token 的已上报样本：**输入与输出都报了**的那几次。

    判据与 `tokens.total` 的（`not input_missing and not output_missing`）**同源**：
    两处若各写一份，会出现「精确值说有、样本值说没有」这种自相矛盾的展示。
    """
    reported = [
        item
        for item in usages
        if item["tokens"]["input"] is not None and item["tokens"]["output"] is not None
    ]
    known_input, _ = _sum_with_missing(item["tokens"]["input"] for item in reported)
    known_output, _ = _sum_with_missing(item["tokens"]["output"] for item in reported)
    return {
        **_sample_counts(len(usages), len(reported)),
        # 没上报任何一次时是 `None`（= 「还不知道」），**不是 0**：0 是「这几次一共没花
        # token」这个确定的结论，而这里的事实是「一次都没报」。
        "known_value": (known_input + known_output) if reported else None,
        "known_input": known_input if reported else None,
        "known_output": known_output if reported else None,
    }


def _cache_sample(usages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """缓存的已上报样本：**命中数与输入数都报了**的那几次。

    分子分母取自**同一批**运行。这是这个函数存在的全部理由：只把 15 次的命中数除以
    20 次的输入总数，会得到一个偏低的比例，而它看起来完全正常。
    """
    reported = [
        item
        for item in usages
        if item["tokens"]["cache_read"] is not None and item["tokens"]["input"] is not None
    ]
    known_read, _ = _sum_with_missing(item["tokens"]["cache_read"] for item in reported)
    known_input, _ = _sum_with_missing(item["tokens"]["input"] for item in reported)
    return {
        **_sample_counts(len(usages), len(reported)),
        # 与 `cache.hit_rate` 同一个函数、同一个判据（分子大于分母时它会返回 `None`，
        # 那说明上游两个字段不是同一口径，这时任何比例都是错的）。
        "known_value": cache_hit_rate(known_read, known_input) if reported else None,
        "known_input": known_input if reported else None,
        "known_cache_read": known_read if reported else None,
    }


def _cost_sample(
    usages: Sequence[Mapping[str, Any]], table: PriceTable | None
) -> dict[str, Any]:
    """费用的已上报样本：**算得出金额**的那几次合计。

    这是「已知**最低**费用」而不是「费用」—— 没上报的那几次同样烧了 token，只是我们
    没有它们的数。`known_value.notes` 里写明这一点，界面要照它显示。

    一次都算不出时 `reason` 带出**第一句现成的理由**（「还没有配置价格表」/「上游没有
    返回 token 数」），让界面能区分「没配价格表」与「上游没报」—— 两者对用户的下一步
    动作完全不同。
    """
    computable = [item["cost"] for item in usages if _cost_ok(item["cost"])]
    known = _sum_costs(computable, table) if computable else None
    reason = ""
    if known is None:
        for item in usages:
            candidate = item["cost"] or {}
            if candidate.get("reason"):
                reason = str(candidate["reason"])
                break
    elif len(computable) < len(usages):
        notes = list(known.get("notes") or [])
        notes.append(
            f"另有 {len(usages) - len(computable)} 次运行没有上报可计算的费用，"
            "这里的金额只含已上报的那几次（**已知最低费用**，不是总额）"
        )
        known = {**known, "notes": notes}
    return {
        **_sample_counts(len(usages), len(computable)),
        "known_value": known,
        "reason": reason,
    }


def reported_samples(usages: Sequence[Mapping[str, Any]], table: PriceTable | None) -> dict[str, Any]:
    """`aggregate_runs` 的第二组数：**已上报样本的合计 + 覆盖度**。

    「全体齐全时的精确值」与它并列给出，见模块 docstring。调用方（`ai_usage_service`）
    把这一块原样透传给界面，一个字都不改 —— 汇总、项目行、周版本行走的是同一个函数，
    三个层级的覆盖率才不会各算各的。
    """
    total_runs = len(usages)
    return {
        "total_runs": total_runs,
        "tokens": _token_sample(usages),
        "cache": _cache_sample(usages),
        "cost": _cost_sample(usages, table),
    }


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
        # **并列**的第二组数：已上报样本的合计 + 覆盖度（见模块 docstring）。
        # 上面那三个字段一个字都没放宽 —— 它们是「全体齐全时的精确值」。
        "reported_samples": reported_samples(usages, price_table),
    }


def _cost_ok(cost: Mapping[str, Any] | None) -> bool:
    return bool(cost) and cost.get("amount") is not None


def _sum_costs(
    costs: Sequence[Mapping[str, Any] | None], table: PriceTable | None
) -> dict[str, Any] | None:
    """把逐次运行的费用按档相加（金额用 Decimal 加，见 pricing 模块）。

    **读 `amount_exact` 而不是 `amount`**：后者是给人看的展示串，不足一分时写的是
    `<0.01` —— 拿它 `Decimal(...)` 会抛 `InvalidOperation`，把只读的消耗面板与预算闸门
    一起打成 500（而预算那条链的 docstring 明写「这个函数不抛异常」）；就算能解析，
    拿**已量化到分**的值相加也会少算（两次真实的 0.014 元应得 0.03，用展示值相加得 0.02）。

    取不回精确金额时返回 `None`（=「算不出」，界面有一句现成的话），**不跳过那一条**：
    少算一条得到的金额看起来完全正常，正是这个模块最想避免的那种错。
    """
    if not costs:
        return None
    lines: dict[str, dict[str, Any]] = {}
    total = Decimal(0)
    currency = ""
    versions: set[str] = set()
    patterns: set[str] = set()
    for item in costs:
        amount = amount_of(item)
        if not item or amount is None:
            return None
        currency = currency or str(item.get("currency") or "")
        if item.get("price_version"):
            versions.add(str(item["price_version"]))
        if item.get("matched_pattern"):
            patterns.add(str(item["matched_pattern"]))
        total += amount
        for line in item.get("lines") or []:
            line_amount = amount_of(line)
            if line_amount is None:
                return None
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
            bucket["amount"] += line_amount

    notes: list[str] = []
    if len(versions) > 1:
        notes.append("这几次运行用的不是同一版单价表：" + "、".join(sorted(versions)))
    return {
        "amount": money(total),
        "amount_exact": amount_exact(total),
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
                "amount_exact": amount_exact(bucket["amount"]),
                "note": bucket["note"],
            }
            for bucket in lines.values()
        ],
        "notes": notes,
    }


# ---------------------------------------------------------------------------
#  执行前的估算（AI-P1-03）
# ---------------------------------------------------------------------------
# 这一节全部是**纯函数**：输入是已经取好的数，输出是区间。取数（查最近同类运行、
# 读配置里的分片数与轮次上限、解析价格表）由调用方做 —— 第二波的 Job 协议要在建 job
# 的那一刻就把 `planned_tokens_low/high` 写进表里，它拿到的必须是一个可以直接调用的函数，
# 而不是一条会查库、会读时钟、会在事务里绕一圈的链路。

# 估算最多看多少次历史运行。**只影响缩放的基准，不影响正确性**：样本越多，区间的两端
# 越不容易被一次异常运行带跑偏，但太老的运行（改过参数、换过模型）参与进来反而是噪声。
ESTIMATE_SAMPLE_LIMIT = 20

# 估算样本的键（`estimation_sample` 的产物形状）。估算函数只认它们，所以调用方也可以
# 自己拼 —— 第二波的 Job 协议那边就是从 `ai_analysis_job` / 运行行拼出来的。
SAMPLE_KEYS = (
    "run_id",
    "created_at",
    "scope",
    "model",
    "tokens_input",
    "tokens_output",
    "cache_read",
    "duration_ms",
    "rounds",
    "requests",
    "files",
    "shards",
    "cost",
)


def _json_field(raw: Any, key: str) -> Any:
    """从一列 JSON 文本里取一个键（读不动 / 不是对象 / 没有这个键都给 `None`）。"""
    if not raw:
        return None
    if isinstance(raw, Mapping):
        payload: Any = raw
    else:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
    return payload.get(key) if isinstance(payload, Mapping) else None


def _stamp(value: Any) -> str | None:
    """时间列 → ISO 文本（读不动给 `None`）。

    估算样本里的 `created_at` 只用来挑「最近一次」，所以这里不做时区换算，只保证同一批
    样本的时间戳是**同一种写法**（naive 的按 UTC 认，与 `as_utc` 同口径）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.isoformat()
    return str(value) or None


def estimation_sample(run: Any, *, price_table: PriceTable | None = None) -> dict[str, Any]:
    """一条运行 → **估算样本**（`estimate_analysis` 的输入形状）。

    与 `usage_from_run` 分开写，因为两者回答的问题不同：那个是「这次花了多少」，
    这个是「下一次大概花多少」。估算额外要两样东西 —— **文件数**与**分片数** ——
    它们都不在 `ai_analysis_run` 的列里：

    * 文件数读 `delta_summary`（`{"delta_files": N, ...}`，`ai_analysis_service` 落库的）；
    * 分片数读 `response_payload["subagents"]` 里**真正的分片**条数 —— 那一列里还混着
      「汇总」（`synthesis`）与「对账」（`verify`）两个成员，它们**不是分片**：实测一次
      3 分片的运行在这里有 5 条（S1/S2/S3 + 汇总 + V1），把 5 当成分片数会让分片折算
      整体打个六折（估算出来的时间与 token 都偏小）。判据用 `family_ledger.ROLE_SUBAGENT`
      那个字面量（`services/ai/family_ledger.py:59`，那里是事实源）。

    两样读不到都**不编造**：给 `None`，估算那边据此降低分辨率（见返回值的 `basis`）。
    """
    usage = usage_from_run(run, price_table=price_table)
    delta_files = _int_or_none(_json_field(getattr(run, "delta_summary", None), "delta_files"))
    subagents = _json_field(getattr(run, "response_payload", None), "subagents")
    shards = None
    if isinstance(subagents, list) and subagents:
        roles = [item.get("role") for item in subagents if isinstance(item, Mapping)]
        # 老 payload 里可能没有 `role` 这一列（那时只有分片成员）：一个 `role` 都没有时
        # 按「全是分片」处理，不能因此判成 0 片（那会让折算整体失真）。
        if any(role for role in roles):
            shards = sum(1 for role in roles if role == "subagent") or None
        else:
            shards = len(subagents)
    return {
        "run_id": _int_or_none(getattr(run, "id", None)),
        "created_at": _stamp(getattr(run, "created_at", None)),
        "scope": str(getattr(run, "scope", "") or ""),
        "model": usage["model"],
        "tokens_input": usage["tokens"]["input"],
        "tokens_output": usage["tokens"]["output"],
        "cache_read": usage["tokens"]["cache_read"],
        "duration_ms": usage["duration_ms"],
        "rounds": usage["rounds"],
        "requests": usage["requests"],
        "files": delta_files,
        "shards": shards,
        "cost": usage["cost"],
    }


def _trimmed_range(values: Sequence[float | None]) -> tuple[float, float] | None:
    """一组实测值的 `(低端, 高端)`。**样本够多时各掐掉一个极值**。

    掐极值不是洁癖：一次卡住重试的运行能让「最费的一次」翻好几倍，而区间的高端会被它
    整个带跑偏。样本少于 5 个时不掐 —— 那时掐掉一个就只剩「两个样本里较小的那个」，
    比极值更没意义。
    """
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    if len(ordered) >= 5:
        ordered = ordered[1:-1]
    if not ordered:
        return None
    return ordered[0], ordered[-1]


def _ratio_of(numerator: int | None, denominator: int | None) -> float | None:
    """两个计数相除；分母缺失或为 0 时给 `None`（不做任何默认假设）。"""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _median(values: Sequence[float | None]) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _sample_total(item: Mapping[str, Any]) -> int | None:
    """样本的「输入 + 输出」。任一段缺失就是 `None`（与 `_token_sample` 同一条判据）。"""
    tokens_input = _int_or_none(item.get("tokens_input"))
    tokens_output = _int_or_none(item.get("tokens_output"))
    if tokens_input is None or tokens_output is None:
        return None
    return tokens_input + tokens_output


def _scale_range(low: float, high: float, *, factor: float) -> tuple[int, int]:
    """区间整体乘一个系数（分片数折算用）。**保证 low ≤ high**，两端都取非负整数。"""
    scaled_low = max(0, int(round(low * factor)))
    scaled_high = max(0, int(round(high * factor)))
    return min(scaled_low, scaled_high), max(scaled_low, scaled_high)


def _cost_range(
    *,
    total_low: int | None,
    total_high: int | None,
    output_ratio: float | None,
    cache_ratio: float | None,
    model: str,
    table: PriceTable | None,
) -> dict[str, Any]:
    """把一个 token 区间折成费用区间。

    三段 token 的拆分用的是**样本里实测的比例**（输出占输入的比例、缓存命中占输入的
    比例），不是凭空的假设 —— 折出来的金额因此与历史运行同一个量级，用户能拿它和
    「最近一次实际值」对照。

    算不出时 `low` / `high` 都是 `None`，`reason` 是 `estimate_cost` 给的那句人话
    （「还没有配置价格表」…）。**绝不回落到 0**：`0` 是「这次不花钱」这个确定的结论。
    """

    def _one(total: int | None) -> dict[str, Any] | None:
        if total is None:
            return None
        ratio = output_ratio if output_ratio is not None else 0.0
        output = int(round(total * ratio / (1 + ratio))) if ratio > 0 else 0
        tokens_input = max(0, total - output)
        cache_read = (
            int(round(tokens_input * cache_ratio)) if cache_ratio is not None else None
        )
        return estimate_cost(
            model,
            tokens_input=tokens_input,
            tokens_output=output,
            cache_read=cache_read,
            table=table,
        ).to_dict()

    low = _one(total_low)
    high = _one(total_high)
    computable = bool(low and low.get("amount") is not None)
    reason = ""
    if not computable:
        reason = str((low or {}).get("reason") or (high or {}).get("reason") or "")
        low = None
        high = None
    elif low and high and low.get("amount_exact") != high.get("amount_exact"):
        high = {
            **high,
            "notes": [
                *(high.get("notes") or []),
                "区间的高端是「最近同类运行里最费的一次」折算出来的",
            ],
        }
    return {
        "low": low,
        "high": high,
        "computable": computable,
        "reason": reason,
        "currency": str((low or high or {}).get("currency") or ""),
    }


def estimate_analysis(
    *,
    planned_files: int | None,
    mode: str = MODE_FULL,
    baseline_reusable: bool | None = None,
    recent_runs: Sequence[Mapping[str, Any]] = (),
    shard_count: int | None = None,
    max_rounds: int | None = None,
    max_tool_requests: int | None = None,
    price_table: PriceTable | None = None,
    model: str = "",
) -> dict[str, Any]:
    """**执行前**的代价区间：预计 token / 预计时间 / 最近一次实际值 / 是否命中可复用基线。

    纯函数（见模块 docstring 末节）：不查库、不读配置、不看时钟。第二波的 Job 协议直接
    拿它填 `planned_tokens_low/high`。

    ## 区间从哪来

    * 有「同类运行 + 文件数」→ 单文件强度（token/文件、毫秒/文件）的**修剪极值区间**
      乘以目标文件数。这是主力路径。
    * 有同类运行但没有文件数（老行）→ 直接拿这些运行实测总量的区间，**不缩放**
      （缩放的依据都没有，硬缩放等于编）。`basis.source` 如实说是哪一种。
    * 一条同类运行都没有 → 全 `None` + 一句说明。**不给一个凭空的数字**。

    `mode` 只影响挑哪一批样本；同模式的样本一个都没有时回落全部样本，并把这件事写进
    `notes`（增量没有历史是常态：库里全是全量运行）。

    ## 为什么 `baseline_reusable` 不参与算术

    「命中可复用基线」的后果是**不用建这次付费 run 了**（见 AI-P0-02），而不是「便宜一点」。
    把它折成一个折扣系数会让区间凭空变小，而那个折扣没有任何观测支撑。所以它只如实出现
    在 `baseline` 里，由界面决定怎么说（「命中基线：这次不需要新建付费运行」）。
    """
    wanted = str(mode or "").strip().lower() or MODE_FULL
    samples = [item for item in recent_runs if isinstance(item, Mapping)]
    same_mode = [
        item for item in samples if str(item.get("scope") or "").strip().lower() == wanted
    ]
    notes: list[str] = []
    mode_samples_missing = False
    if same_mode:
        usable = same_mode
    elif samples:
        usable = samples
        mode_samples_missing = True
        notes.append(
            f"**{wanted} 没有实测样本**：这里参照的是全量运行的区间（最近 {len(samples)} 次），"
            "不是这个模式的实测值 —— 换了模式之后文件数与轮次都会变，实际可能明显偏离。"
        )
    else:
        usable = []

    # 分片是**串行**的（一个跑完再跑下一个），所以分片数变了要按比例折算 —— 这是实测
    # 结论（见 docs/AI分析使用说明 与 ai-analysis-sizing 技能），不是经验系数。
    sample_shards = _median(
        [float(value) for value in (_int_or_none(item.get("shards")) for item in usable) if value]
    )
    shard_factor = 1.0
    if shard_count and sample_shards and sample_shards > 0:
        shard_factor = float(shard_count) / float(sample_shards)
        if abs(shard_factor - 1.0) > 0.01:
            notes.append(
                f"按 {shard_count} 个分片折算（样本是 {int(sample_shards)} 个分片；"
                "分片是串行的，墙钟与总 token 随之成比例变化）。"
            )
    elif shard_count and shard_count > 1 and usable and sample_shards is None:
        notes.append("历史运行没有记录分片数，这次的分片数没有折进区间。")

    files = [int(value) for value in (_int_or_none(item.get("files")) for item in usable) if value]
    target = planned_files if planned_files and planned_files > 0 else None

    token_intensity: list[float] = []
    time_intensity: list[float] = []
    for item in usable:
        count = _int_or_none(item.get("files"))
        total = _sample_total(item)
        if count and count > 0 and total:
            token_intensity.append(total / count)
        duration = _int_or_none(item.get("duration_ms"))
        if count and count > 0 and duration:
            time_intensity.append(duration / count)

    basis_source = "none"
    token_low: int | None = None
    token_high: int | None = None
    # 「同类」这两个字在**借来的**区间里是假话：那时样本是别的模式的运行。措辞必须跟着
    # 事实走 —— 卡片上写「增量：参照全量」而 notes 里写「按同类运行折算」，读的人会
    # 以为两者是同一批数据，而它们正是被区分开的那两件事。
    sample_word = "运行" if mode_samples_missing else "同类运行"
    if target and token_intensity:
        span = _trimmed_range(token_intensity)
        assert span is not None
        basis_source = "files"
        token_low, token_high = _scale_range(
            span[0] * target, span[1] * target, factor=shard_factor
        )
        notes.append(
            f"按最近 {len(token_intensity)} 次{sample_word}的单文件用量折算"
            f"（每文件 {int(span[0]):,} ~ {int(span[1]):,} token，目标 {target:,} 个文件）"
            + ("；这些运行不是本模式的，只是拿来参照。" if mode_samples_missing else "。")
        )
    else:
        span = _trimmed_range([_sample_total(item) for item in usable])
        if span is not None:
            basis_source = "totals"
            token_low, token_high = _scale_range(span[0], span[1], factor=shard_factor)
            if usable and not target:
                notes.append(f"没有给出目标文件数，区间按最近{sample_word}实测的总量给出。")
            else:
                notes.append(
                    f"历史运行没有记录文件数，区间按这些{sample_word}**实测的总量**给出，"
                    "未按目标文件数缩放。"
                )
        elif usable:
            notes.append("历史运行里没有一次上报过完整的 token，无法给出 token 区间。")
    if not usable:
        notes.append("还没有可参照的历史运行，无法估算 —— 先跑一次才有区间。")

    # 增量 + 没有增量样本：低端是线性折算（一定偏小），两端都要**锚回全量实测区间**上 ——
    # 低端抬到全量最省的一次，高端抬到全量最费的一次（最坏情况无非是它基本按全量跑了一遍）。
    #
    # 只抬低端是不够的：折算出来的高端同样远低于全量（12 个文件 × 单文件强度），于是
    # `high` 会被低端顶到同一个数上，区间塌成一个点 —— 那看起来像一个**精确**的估算，
    # 而它其实是我们最没有把握的那一处。理由要写进 notes，否则那两个端看起来像是算出来的。
    if wanted == MODE_INCREMENTAL and not same_mode and token_low is not None:
        span = _trimmed_range([_sample_total(item) for item in samples])
        if span is not None:
            if span[0] > token_low:
                token_low = int(span[0])
            if token_high is None or span[1] > token_high:
                token_high = int(span[1])
            notes.append(
                f"增量的两端已锚在**全量实测区间**上（{int(span[0]):,} ~ {int(span[1]):,} token）："
                "按文件数线性折算会明显偏小 —— 清单与逐轮上下文不随文件数线性缩小。"
            )

    # 时间：同一套口径（单文件毫秒 × 文件数）；没有文件数就用实测耗时区间。
    time_low: int | None = None
    time_high: int | None = None
    if target and time_intensity:
        span = _trimmed_range(time_intensity)
        assert span is not None
        time_low, time_high = _scale_range(
            span[0] * target, span[1] * target, factor=shard_factor
        )
    else:
        span = _trimmed_range([_int_or_none(item.get("duration_ms")) for item in usable])
        if span is not None:
            time_low, time_high = _scale_range(span[0], span[1], factor=shard_factor)

    if max_rounds or max_tool_requests:
        notes.append(
            "轮次上限 "
            + (str(max_rounds) if max_rounds else "未配置")
            + "、索取上限 "
            + (str(max_tool_requests) if max_tool_requests else "未配置")
            + "：撞哪一个上限都会改变实际用量，区间没有把它们算进去。"
        )

    # 三段 token 的拆分比例：取自样本实测（中位数）。
    cost = _cost_range(
        total_low=token_low,
        total_high=token_high,
        output_ratio=_median(
            [
                _ratio_of(
                    _int_or_none(item.get("tokens_output")),
                    _int_or_none(item.get("tokens_input")),
                )
                for item in usable
            ]
        ),
        cache_ratio=_median(
            [
                _ratio_of(
                    _int_or_none(item.get("cache_read")),
                    _int_or_none(item.get("tokens_input")),
                )
                for item in usable
            ]
        ),
        model=model or str((usable[0].get("model") if usable else "") or ""),
        table=price_table,
    )
    if token_low is not None and not cost["computable"]:
        notes.append(
            "价格表算不出费用，这里只给 token 与时间（理由："
            + (cost["reason"] or "未知")
            + "）。"
        )
    elif not cost["computable"] and not cost["reason"]:
        # 没有 token 区间可折时 `estimate_cost` 根本没被调用，`reason` 是空的。
        # 界面据此显示「为什么没有金额」——**空着会让它去猜**（一个「价格表不可用」
        # 的回落文案在「其实没跑过分析」时是句假话）。
        cost = {**cost, "reason": "还没有可参照的历史运行，估不出 token，也就估不出费用"}

    # 「最近一次实际值」：样本里最新的那一条。这里再排一次序，是为了让结果不依赖调用方
    # 传进来的顺序（Job 协议那边可能从 job 表拼样本，顺序不一定是时间序）。
    last = None
    if usable:
        newest = sorted(usable, key=lambda item: str(item.get("created_at") or ""))[-1]
        last = {
            "run_id": _int_or_none(newest.get("run_id")),
            "created_at": newest.get("created_at") or None,
            "files": _int_or_none(newest.get("files")),
            "tokens": _sample_total(newest),
            "duration_ms": _int_or_none(newest.get("duration_ms")),
            "rounds": _int_or_none(newest.get("rounds")),
            "shards": _int_or_none(newest.get("shards")),
            "cost": newest.get("cost") if _cost_ok(newest.get("cost")) else None,
        }

    if files and target and (target < min(files) or target > max(files)):
        notes.append(
            f"目标文件数（{target:,}）超出了样本的文件数区间（{min(files):,} ~ {max(files):,}），"
            "属于**外推**。"
        )

    return {
        "mode": wanted,
        "planned_files": target,
        "shard_count": shard_count,
        "baseline": {
            "reusable": baseline_reusable,
            "note": (
                "命中可复用基线：这次不需要新建付费运行，直接复用已有结论。"
                if baseline_reusable is True
                else (
                    "没有命中可复用基线，这次会真的跑一遍模型。"
                    if baseline_reusable is False
                    else "这次能不能复用基线还没有判定（取决于快照与依赖是否变化）。"
                )
            ),
        },
        "tokens": {"low": token_low, "high": token_high, "unit": "token"},
        "duration_ms": {"low": time_low, "high": time_high},
        "cost": cost,
        "last_actual": last,
        "basis": {
            "source": basis_source,
            "runs": len(usable),
            "same_mode_runs": len(same_mode),
            # **这一格是给界面用的**：为真时这个区间是**借来的**（用别的模式的运行折算），
            # 界面必须写出来，而且**不许**把它标成「预计增量代价」—— 一个偏保守但自称
            # 准确的数字，比一句「这个模式没有实测样本」更容易被当真、被拿去做决策。
            "mode_samples_missing": mode_samples_missing,
            "files": {
                "low": min(files) if files else None,
                "high": max(files) if files else None,
            },
            "shards": int(sample_shards) if sample_shards else None,
            "run_ids": [
                value for value in (_int_or_none(item.get("run_id")) for item in usable) if value
            ],
        },
        "notes": notes,
    }
