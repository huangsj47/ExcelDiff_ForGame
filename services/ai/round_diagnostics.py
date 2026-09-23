# -*- coding: utf-8 -*-
"""逐轮**诊断值**的读取侧：四格并排的用量 + 那一轮为什么没复用上缓存（工作包 F）。

## 这一组回答什么

用量页此前只有「输入 token / 其中命中缓存 / 输出 token」三格，而**「未命中输入」**
（真正按未命中价计费的那一档）只藏在副标题里、**推理 token** 在逐轮账上根本没有数。
四格并排之后，「这次花了多少钱、花在哪一档」才一眼看得出；逐轮那一层再补上
**分叉原因**（这次请求与上一次在哪个消息开始分叉）、三类耗时与推理 token。

数据本身是**跑的时候**就逐轮写进事件账本的（`models/ai_analysis/round_event.py`，
工作包 E 的那一张；指纹由 `services/ai/request_fingerprint.py` 在每次模型调用之前算）。
这里只做**读取与摆形状**，不重算任何东西：同一件事只该有一个来源。

## 口径（三条，别踩）

1. **未命中 = 总输入 − 缓存输入，只减这一次**。上游的 `prompt_tokens` **含**缓存命中，
   平台存的 `tokens_input` 就是它（与上游同口径）；对面那套（Harness 的 `inputTokens`）
   已经把命中减掉了，拿它的数当总输入就会减第二遍，得出一个偏小的未命中数。
2. **`None`（上游没报）与 `0`（报了且确实是 0）在每一格上都是两个值**。把它们合并，
   界面上就会出现一个确定的「未命中 0 tokens」，而真相是「不知道」。
3. **哈希是本机诊断指标，不是命中率**。提供商按 token / 自己的内部单元匹配缓存：
   哈希相同**不保证**命中（服务端可能已过期），哈希不同也**不保证**未命中（我们这一侧
   多一个不参与匹配的字段就会让哈希变掉）。所以字段名与文案都说「分叉原因」，
   不说「命中/未命中」，界面上那句话也必须跟着这些数字一起出现。

## 隐私

只读哈希与计数：**没有提示词正文**（正文不在这里，也不为诊断再落一份），
**没有 API key**（端点标识是 `normalize_base_url` 归一之后的地址，URL 里的凭据已被去掉）。

## 为什么诊断值摆在 `rounds` **旁边**，不并进每一行

`rounds[i]` 的键必须与「跑的过程中」那一份（`trace_evidence.live_round_entry`）
**逐字相同** —— 同一个「思考过程」面板有两条来路，键不一样就会有两种真相，那条契约有
测试钉着（`tests/test_ai_live_thinking_snapshot.py`），而 `live_round_entry` 不在本工作包
的文件范围内。所以诊断值按 `round_index` 摆成独立的一块（`round_diagnostics`），
逐轮那一层要读就查这一块（模板里的 `aiuRoundDiag`）。

## 为什么是一个独立模块

`services/ai_usage_service.py` 贴着 2000 行的 ERROR 闸门（`scripts/check_file_length.py`，
**实测**：加这一组之前 1988 行，直接加进去是 2216 行）。这一组是**纯函数 + 两处读取**，
搬出来之后那个文件只留两行调用。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from services.ai import round_events
from services.ai.usage import usage_from_run
from utils.logger import log_print

__all__ = ["collect", "diagnostics_table", "input_breakdown", "json_object"]


def _optional_int(value: Any) -> Optional[int]:
    """读一个可空的整数。**`None` 原样带出去**（未上报），不许兜成 0。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def json_object(raw: Any) -> dict[str, Any]:
    """把库里那列 JSON 文本读成 dict。读不出来给 `{}`（老行/坏行不该让整页炸掉）。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def input_breakdown(
    *,
    tokens_input: Optional[int],
    cache_read: Optional[int],
    tokens_output: Optional[int],
    reasoning: Optional[int] = None,
    reasoning_reported_rounds: int = 0,
    reasoning_rounds: int = 0,
) -> dict[str, Any]:
    """**四格并排**的那一份：总输入 / 未命中输入 / 缓存输入 / 输出（含推理）。

    口径见模块 docstring 的前两条（只减一次；`None` 与 `0` 是两件事）。

    `reasoning` 是**输出的一部分**（上游把它放在 `completion_tokens_details` 里），
    所以输出那一格的文案是「输出（含推理）」，这一格是它的明细。
    """
    total = _optional_int(tokens_input)
    cached = _optional_int(cache_read)
    output = _optional_int(tokens_output)
    # 两个加数都得有才能相减。夹到 0 是给「上游把命中报得比总输入大」这种脏数据留的
    # 余地（与面板上那句 `Math.max(0, input - hit)` 同一手法）——夹到 0 表示「按这个数
    # 算出来是 0」，而**不是**「未命中未知」。
    uncached = None if (total is None or cached is None) else max(0, total - cached)
    return {
        "total_input": total,
        "uncached_input": uncached,
        "cached_input": cached,
        "output": output,
        "reasoning": _optional_int(reasoning),
        # 推理那一格是「这一格是下界还是精确值」的依据：上游只在部分轮次上报它，
        # 全部轮次上报才算精确（与 `reported_samples` 同一套说法）。
        "reasoning_reported_rounds": int(reasoning_reported_rounds or 0),
        "reasoning_rounds": int(reasoning_rounds or 0),
    }


def diagnostics_table(run_id: Any) -> dict[int, dict[str, Any]]:
    """逐轮事件账本上的**诊断值**，键是 `ai_analysis_trace.round_index`。

    值是这一轮的上游用量来源、推理 token、三类耗时与请求指纹
    （`services/ai/request_fingerprint.py` 算出来的那几个数）。

    ## 为什么位置可以作为键（以及什么时候不行）

    两个来源的轮次口径不同：事件账本按「成员内轮次」记（运行中每个成员只知道自己的
    轮次），trace 按**家族内全局递增**的 `round_index` 记。而两者**遍历顺序相同**
    （按成员位次、成员内轮次升序）—— 这正是 `round_events.reconcile_interrupted_run`
    能按同一个顺序把事件补成 trace 行的原因。所以位置号可以直接当键，
    读取侧再按 `(分片标签, 成员内轮次)` 复核一次（见 `_diagnostics_for`）。
    """
    try:
        rows = round_events.events_for_run(int(run_id))
    except Exception as exc:  # noqa: BLE001 —— 读不到诊断值不该让整页 500
        log_print(f"⚠️ 读逐轮诊断值失败: run={run_id} {exc}", "AI", force=True)
        return {}
    items: dict[int, dict[str, Any]] = {}
    for position, row in enumerate(rows, start=1):
        items[position] = {
            "member": str(getattr(row, "member", "") or ""),
            "member_round": int(getattr(row, "round", 0) or 0),
            "reasoning_tokens": _optional_int(getattr(row, "reasoning_tokens", None)),
            "usage_source": str(getattr(row, "usage_source", "") or ""),
            "reasoning_source": str(getattr(row, "reasoning_source", "") or ""),
            "model_call_ms": _optional_int(getattr(row, "model_call_ms", None)),
            "tool_fetch_ms": _optional_int(getattr(row, "tool_fetch_ms", None)),
            "index_build_ms": _optional_int(getattr(row, "index_build_ms", None)),
            "request_fingerprint": _decode_fingerprint(row),
        }
    return items


def _decode_fingerprint(row: Any) -> Optional[dict[str, Any]]:
    """事件行上的指纹 JSON。

    **没算出来就是 `None`**（未上报），不是空 dict —— 界面上「这一轮没算指纹」与
    「算出来是一份空指纹」要说成两句话。指纹里只有哈希与计数（不含提示词正文，
    见 `request_fingerprint` 的口径 2）。
    """
    payload = json_object(getattr(row, "fingerprint_json", None))
    if not payload:
        return None
    # 几个会被界面单独读的列照原样并进来（它们是同一份数据的列化形态，见
    # `request_fingerprint.event_columns`）—— 只有 JSON 里缺了才补，避免两份值打架。
    for name in (
        "request_fingerprint",
        "stable_prefix_fingerprint",
        "prefix_common_messages",
        "prefix_common_chars",
        "prefix_divergence_reason",
    ):
        if payload.get(name) is None:
            payload[name] = getattr(row, name, None)
    return payload


def _diagnostics_for(row: Any, items: Mapping[int, dict[str, Any]]) -> dict[str, Any]:
    """一条 trace 行对应的逐轮诊断。**对不上就给空 dict**（宁可空着，不许错位）。

    位置之外再核一次身份：**分片运行**里事件行带着分片标签与成员内轮次
    （`agent` / `agent_round` 在 trace 上也是那两列），两处对不上说明事件账本缺行
    （写失败、被保留期清过），位置号从那一刻起就整体错位了 —— 那时把 S1 第 3 轮的
    指纹安到 S4 第 1 轮头上，比这一格空着难查得多。
    """
    item = items.get(int(getattr(row, "round_index", 0) or 0))
    if not item:
        return {}
    member = str(item.get("member") or "")
    agent = str(getattr(row, "agent", "") or "")
    if member or agent:
        if member != agent:
            return {}
        if int(item.get("member_round") or 0) != int(getattr(row, "agent_round", 0) or 0):
            return {}
    return item


# 逐轮诊断值里有几个字段是**事件账本内部用的**（身份与解码中间物），不进界面那一份。
_DIAGNOSTIC_INTERNAL_KEYS = ("member", "member_round")


def _round_diagnostic_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    """逐轮诊断 → 界面读的那几个键。**缺了就给 `None`，不是 0**（见 `input_breakdown`）。

    产物进 `run_usage()["round_diagnostics"][round_index]`（**不并进 `rounds[i]`**，
    理由见本模块 docstring）。

    与 `_diagnostics_for` 分开是因为两者的读者不同：那一个决定「这条事件行是不是属于
    这一轮」（判据），这一个决定「给界面摆成什么形状」（呈现）。合成一个的话，
    以后想多给界面一个键就得动判据。
    """
    source = dict(item or {})
    for key in _DIAGNOSTIC_INTERNAL_KEYS:
        source.pop(key, None)
    return {
        "reasoning_tokens": source.get("reasoning_tokens"),
        "usage_source": source.get("usage_source") or "",
        "reasoning_source": source.get("reasoning_source") or "",
        "model_call_ms": source.get("model_call_ms"),
        "tool_fetch_ms": source.get("tool_fetch_ms"),
        "index_build_ms": source.get("index_build_ms"),
        "request_fingerprint": source.get("request_fingerprint"),
    }


def collect(
    run: Any, rounds: Sequence[Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """一次读全：`(逐轮诊断块, 运行级四格用量)`。**一次数据库读**，不是两处各读一遍。

    * 逐轮块按 `round_index` 摆（**字符串键**：它要原样进 JSON，那边的键就是字符串），
      每一行 trace 都有一条 —— 老运行（那时还没有这些列）里面全是 `None` = 未上报，
      这样「这一轮有没有诊断」不用去猜键在不在。
    * 四格那一份的加数取 `usage_from_run`（**不传价格表**：这里只要 token，算费用是
      调用方的事）。
    * 推理 token 的**运行级**合计只在报过的那些轮次之间求和（`None` = 一轮都没报）；
      分母用**事件账本里的全部轮次**，不是被 `MAX_ROUNDS` 截过的 trace 行 ——
      否则「12/60 轮上报」会随读取上限变化。
    """
    usage = usage_from_run(run)
    items = diagnostics_table(getattr(run, "id", 0))
    reasoning_values = [
        item["reasoning_tokens"]
        for item in items.values()
        if item.get("reasoning_tokens") is not None
    ]
    block = {
        str(row.round_index): {
            **_round_diagnostic_fields(_diagnostics_for(row, items)),
            # 逐轮的**未命中输入**（= 总输入 − 缓存命中，**只减一次**）。两个加数任一
            # 没有就是 `None`（未上报）—— 面板上「未命中 0」与「不知道」不是一回事。
            # 它按那条形状契约也不能落在 `rounds[i]` 上，所以与诊断值同路。
            "uncached_input": input_breakdown(
                tokens_input=row.tokens_input,
                cache_read=row.cache_read_tokens,
                tokens_output=row.tokens_output,
            )["uncached_input"],
        }
        for row in rounds
    }
    breakdown = input_breakdown(
        tokens_input=usage["tokens"]["input"],
        cache_read=usage["tokens"]["cache_read"],
        tokens_output=usage["tokens"]["output"],
        reasoning=sum(reasoning_values) if reasoning_values else None,
        reasoning_reported_rounds=len(reasoning_values),
        reasoning_rounds=len(items),
    )
    return block, breakdown
