#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一轮的「证据」：模型说了什么、要了什么、拿到的是内容还是**一句取不到**。

## 为什么要有这一层（2026-09-19）

一次周版本分析的报告里写着「未读到任何代码 diff」，而面板上那一轮看起来**一切正常**：
`ai_analysis_trace` 只记了计数（索取 6 次、执行 6 条、丢弃 0 条），取数回来的正文一个字
都没落库。于是「这次为什么没读到」只能靠猜 —— 是模型没要？是取数失败了？是被预算拒了？
三种可能在三列计数上长得一模一样。

更要紧的是：**工具「成功返回」与「取到了内容」不是一回事**。取不到时 provider 给的是一句
完整的话（`[取数失败] xxx：平台读不到…**这不等于「没有改动」**`），它看着像内容、字数也不
为 0，所以在「条数 + 字符数」的口径下，一次失败的索取与一次真的读了一份 diff **完全无法
区分**。这一层把那个区分显式记下来（`failed` / `reason`）。

## 为什么是纯函数 + 鸭子类型

输入只有引擎的 `RoundRecord` 与协议里的那几个 dataclass，输出是 JSON。不碰数据库、不碰
Flask，也不 import `protocol` / `budget`（只按属性名读）—— 这样它可以被完整单测，而
「写库侧」与「读库侧」共用同一份编码/解码，不会一边改了字段名一边没改。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional, Sequence

from services.ai.budget import truncate_text
from utils.logger import log_print

# 单轮模型原文的入库上限。**不是省空间，是省得看不出重点**：一轮的原文通常只有几百字
# （一个 JSON），但结论那一轮可能是一整份报告（几万字）—— 面板上展开几十份报告没有意义，
# 报告正文本来就在 `ai_analysis_run.response_text` 里。
TRACE_RESPONSE_MAX_CHARS = 4000
# 每条明细里那个自由文本字段（丢弃原因等）的上限。
TRACE_DETAIL_MAX_CHARS = 300
# 每轮最多记多少条明细。索取一次最多几十条，正常分析远低于这个数。
TRACE_LIST_MAX_ITEMS = 40

# ---------------------------------------------------------------------------
# 「正在跑的那一轮」的实时条目：**比落库那份小得多**，因为它每 3 秒随轮询整体重发一次
# （`static/js/ai_stream_status.js` 的 POLL_INTERVAL_MS），一次 30 轮的分析若按落库上限
# 带出去就是每帧上百 KB。上限在这里由**载荷**决定，与省空间无关。
# ---------------------------------------------------------------------------
# 一轮的模型原文：落库上限 4000，实时只带 800 —— 思考过程那一栏要的是「这轮它说了什么」
# 的轮廓，完整原文跑完之后在 `/runs/<id>/usage` 上照样看得到。
LIVE_RESPONSE_MAX_CHARS = 800
LIVE_LIST_MAX_ITEMS = 8

# provider **取不到**时给出的那几句话的开头（`services/ai/platform_provider.py` 与
# `services/ai/context_tools.py` 是仅有的两个发出方）。它们长得像内容，所以只能按开头认。
#
# 这不是「猜文案」：这几句就是那一层对外的失败契约（`ContextProvider` 返回 `None` 与
# 返回一句话的区别）。改文案就要改这里 —— `tests/test_ai_trace_evidence.py` 会拿
# provider 的真输出喂进来，并扫这两个模块的源码，改漏了会红。
#
# 同一批括号里还有几条**不是**失败说明，别顺手加进来：`[配表]`（该表已被删除 / 本次没有
# 可展示的差异，都是真结论）、`[文本]`/`[代码]`/`[图片]`/`[分段差异]`（正常内容的抬头）。
#
# `[配表解析失败]` 与 `[配表差异解析失败]` 是两句不同的话：前者的对象是**正文**
# （`file_content` 读一张读不了的表），后者是**差异**（`file_diff` 解不出一份载荷）。
# 两者都要在，且都**不能**退回成 `[配表]` —— 那个前缀被真结论共用，按它识别会把
# 「该表已被删除」当成取数失败。
FAILURE_NOTICE_PREFIXES = (
    "[取数失败]",
    "[读不到差异]",
    "[读不到正文]",
    "[配表解析失败]",
    "[配表差异解析失败]",
    "[无法展示的内容]",
    "[无法展示的改动]",
    # `find_references` 的四句（`platform_provider._search_*`）。它们同样是「没拿到内容」：
    # 认不出来的话，面板上那一次的「失败」列会是 0，而报告里那句「检索不到」反而看着像
    # 一条**结论**（「本批次没有别处引用」）—— 这两件事的区别正是本模块存在的理由。
    "[检索不到]",
    "[检索还没回来]",
    "[检索不可用]",
    # ★ `[检索额度用尽]` 已删除（2026-09-22）：E2 把 `SearchBudget` 那道**共享扫描额度**
    # 整个拆掉了（`reference_search` 现在是「建一次索引、按查询查内存」），于是**没有任何
    # 地方再发出这句话**。留在清单里的害处不是说错，而是**攒死代码**：`failure_notice()`
    # 永远匹配不到它，下一个人看到这行会以为「额度用尽」这条路径还在。
    # 钉住这一点的测试是 `tests/test_ai_trace_evidence.py::
    # test_the_prefixes_are_the_ones_the_provider_emits` —— 它**扫源码**核对每个前缀都还有
    # 发出方，所以这一行的删除不是审美，是被一条会红的用例逼出来的。
)


def failure_notice(text: Any) -> str:
    """这段内容是不是「取不到」的说明（是则返回那句话，否则空串）。

    返回**整句**而不是布尔：报告与面板要的是「为什么取不到」，而原因就在这句话里
    （平台读不到 / 已向 Agent 索取但超时 / 项目没绑 Agent 节点）。
    """
    content = str(text or "").lstrip()
    for prefix in FAILURE_NOTICE_PREFIXES:
        if content.startswith(prefix):
            return content.splitlines()[0][:TRACE_DETAIL_MAX_CHARS]
    return ""


def _clip(value: Any, limit: int = TRACE_DETAIL_MAX_CHARS) -> str:
    return str(value or "")[:limit]


def _int_or_none(value: Any) -> Optional[int]:
    """数字字段 → int / None。**认不出来就是 `None`（「不知道」），不是 0。**

    `0` 是一个确定的结论（「上限是 0 字」），而「这一条没有上限可报」（没被截断）
    与它是两件事 —— 见 `summarize_executed` 里 `limit` 的说明。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _head(items: Any, limit: int = TRACE_LIST_MAX_ITEMS) -> list:
    if not isinstance(items, (list, tuple)):
        return []
    return list(items)[:limit]


def summarize_requests(requests: Any, *, limit: int = TRACE_LIST_MAX_ITEMS) -> list[dict]:
    """模型这一轮点名要了什么（它自己写的请求，越权的那些不在这里 —— 那些在 dropped）。"""
    out = []
    for request in _head(requests, limit):
        describe = getattr(request, "describe", None)
        out.append({
            "type": _clip(getattr(request, "type", ""), 40),
            "commit": _clip(getattr(request, "commit", ""), 64),
            "path": _clip(getattr(request, "path", "")),
            "name": _clip(getattr(request, "name", "")),
            "lines": _clip(getattr(request, "lines", ""), 40),
            "query": _clip(getattr(request, "query", ""), 200),
            # 人读的那一行（面板直接显示它）。`describe()` 是协议自带的写法，不在这里另写一套。
            "text": _clip(describe() if callable(describe) else "", 200),
        })
    return out


def summarize_executed(items: Any, *, limit: int = TRACE_LIST_MAX_ITEMS) -> list[dict]:
    """这一轮**真正进了提示词**的东西：多少字、是不是一句失败说明。

    `failed` 与 `empty` 必须分开：前者是「取不到」（有原因可说），后者是工具明确回了一句
    「确实没有内容」。把两者合并，等于把「没有证据」写成「这里没问题」—— 那正是本模块
    这一段要防的事。

    ## `truncated` 的口径是**交付**，与汇总计数是同一个（不是两种算法）

    这里写的是 `item.meta["truncated"]`：**这**一条交给模型时是不是截断的正文。跨成员共享
    缓存命中的条目给出去的就是那份截断过的原件（`context_tools` 模块 docstring 第 6 条），
    所以它也在这里算一条 —— 那正是该条分支记账侧原先漏掉的一笔，症状是
    `Σ details[].truncated`（25）比 `dropped_json.truncated`（18）多。

    两边现在读的是同一份事实，等式（各自求和后）必须成立：

        Σ_轮 dropped_json.truncated == Σ_轮 Σ_details details[].truncated

    这条等式是 `tests/test_ai_truncation_ledger.py` 逐字锁住的。**不要把这里的 `truncated`
    去掉去凑相等** —— 那会把「这一家看到的内容是残缺的」从账上抹掉。
    """
    out = []
    for item in _head(items, limit):
        meta = dict(getattr(item, "meta", None) or {})
        text = str(getattr(item, "text", "") or "")
        notice = failure_notice(text)
        out.append({
            "kind": _clip(getattr(item, "kind", ""), 40),
            "label": _clip(getattr(item, "label", ""), 200),
            "chars": len(text),
            "failed": bool(notice or meta.get("tool_failed")),
            "empty": bool(meta.get("tool_empty")),
            "reason": notice or _clip(meta.get("reason", "")),
            "truncated": bool(meta.get("truncated")),
            # **截断要可归因**（E3 验收）。原先只有 `truncated` 这个布尔，于是事后
            # 翻账只能看到「这一条被砍了」，看不出该去调哪个配置 —— 而单条上限、
            # 总预算、窗口水位、逐级压缩这四条约束要调的地方完全不同。
            #
            # 没人置位时是 `None` / 空串：`0` 会被读成「上限是 0 字」这个确定的错误事实。
            "limit": _int_or_none(meta.get("limit")),
            "truncated_by": _clip(meta.get("truncated_by", ""), 60),
        })
    return out


def summarize_dropped(dropped: Any, *, limit: int = TRACE_LIST_MAX_ITEMS) -> list[dict]:
    """这一轮被丢掉的东西**与原因**（越权、超预算、取数异常…）。

    只记条数的话，「这次少看了一个文件」永远查不出是哪一步丢的。
    """
    out = []
    for item in _head(dropped, limit):
        out.append({
            "kind": _clip(getattr(item, "kind", ""), 40),
            "reason": _clip(getattr(item, "reason", "")),
            "detail": _clip(getattr(item, "detail", "")),
        })
    return out


def _dump(payload: dict) -> Optional[str]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return text if text != "{}" else None


def encode_evidence(record: Any) -> dict:
    """一轮的记账 → `AiAnalysisTrace` 的那几列（**写库侧唯一入口**）。

    三个 `*_json` 里**既保留原来的计数、也带上明细**：计数是既有的读法（面板上的
    「索取 N 次」），明细是新加的。少一个，某一侧的读法就会看到 0。

    这一层存在的直接理由见本模块文档：计数分不出「读到了」与「取不到」。
    """
    return {
        "requests_json": _dump({
            "count": int(getattr(record, "request_count", 0) or 0),
            "items": summarize_requests(getattr(record, "requests", ())),
        }),
        "executed_json": _dump({
            "items": int(getattr(record, "item_count", 0) or 0),
            "details": summarize_executed(getattr(record, "executed", ())),
        }),
        "dropped_json": _dump({
            "refused_by_budget": int(getattr(record, "refused_by_budget", 0) or 0),
            # 与 `executed_json.details[].truncated` **是同一份口径**（交付，含跨成员复用
            # 同一条被截断的正文）：两个数各自求和后必须相等，见 `summarize_executed`。
            "truncated": int(getattr(record, "truncated", 0) or 0),
            "details": summarize_dropped(getattr(record, "dropped", ())),
        }),
        "response_text": _clip_response(getattr(record, "response_text", "")),
        "budget_notes": "\n".join(
            str(note) for note in (getattr(record, "budget_notes", ()) or ()) if str(note).strip()
        ) or None,
        "correction_hint": _clip(getattr(record, "correction_hint", "")) or None,
    }


def _clip_response(text: Any) -> Optional[str]:
    content = str(text or "")
    if not content:
        return None
    try:
        return truncate_text(content, TRACE_RESPONSE_MAX_CHARS)[0]
    except ValueError:  # pragma: no cover —— 常量必然为正，留着只是不让它炸
        log_print(f"⚠️ AI trace：单轮原文截断失败，改为不记（{TRACE_RESPONSE_MAX_CHARS}）", "AI")
        return None


def _load(raw: Any) -> dict:
    try:
        payload = json.loads(raw or "null")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _list_field(payload: dict, key: str) -> list:
    value = payload.get(key)
    return list(value) if isinstance(value, list) else []


def decode_evidence(row: Any) -> dict:
    """`AiAnalysisTrace` 的几列 → 给界面读的明细（**读库侧唯一入口**）。

    老行（这一层之前写下的）只有计数没有明细：那时 `items`/`details` 不存在，
    这里如实给空列表 —— 界面上是「没有明细」，不是「没有索取」。
    """
    requests = _load(getattr(row, "requests_json", None))
    executed = _load(getattr(row, "executed_json", None))
    dropped = _load(getattr(row, "dropped_json", None))
    return {
        "requests": _list_field(requests, "items"),
        "executed": _list_field(executed, "details"),
        "dropped": _list_field(dropped, "details"),
        "response_text": str(getattr(row, "response_text", None) or ""),
        "budget_notes": str(getattr(row, "budget_notes", None) or ""),
        "correction_hint": str(getattr(row, "correction_hint", None) or ""),
    }


def failed_labels(executed: Sequence[dict] | Iterable[dict]) -> list[str]:
    """这一轮里「取不到」的那些条目的标签（给一句话摘要用）。"""
    out = []
    for item in executed or ():
        if isinstance(item, dict) and item.get("failed"):
            out.append(str(item.get("label") or item.get("kind") or ""))
    return out


def _optional_int(value) -> int | None:
    """上游给的用量数：读得出来就是非负整数，读不出来（含 `None`）就是 `None`。

    **不把「没上报」写成 0** —— 与 `cache_read_tokens` 那两个字段同一条口径。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def live_round_entry(record: Any) -> dict:
    """一轮的记账 → **正在跑的界面上**那一条（思考过程标签页用）。

    ## 为什么它的键必须与 `ai_usage_service.run_usage()["rounds"][i]` 一样

    同一个界面有两条来路：跑的过程中读进程内的进度快照（本函数），跑完之后读
    `/ai-analysis/runs/<id>/usage`（那一份由 `decode_evidence` + trace 的列拼出来）。
    两处若各有一套键名，同一个面板就会同时存在两种真相 —— 而它们描述的是同一件事。
    所以这里逐字对齐那一份的键，只有取值上限更紧（见 `LIVE_*` 常量）。

    ## 截断更紧，但**不换说法**

    `executed` 在实时这一份里**只留 `failed` 或 `empty` 的条目**（思考过程那一栏要看的正是
    「这一轮哪条没拿到」），并且截到 `LIVE_LIST_MAX_ITEMS`。读侧那一份是全量的 ——
    渲染器只画 `failed`/`empty` 的那些，两边显示出来就一致。

    ## 它不碰分片标签

    `agent` / `agent_round` 在引擎这一层是空的（引擎每次只跑一个成员，标签由
    `subagent._call_engine` 在回调外层贴上去，见 `services/ai/subagent.py`）。所以这里
    照实录 `record` 上的值，**不猜**：实时那一路由 `run_progress.publish` 用进度对象上的
    `agent` 补齐，落库那一路本来就是对的。
    """
    # **先筛再截**：`summarize_executed` 内部是按前 N 条截的（默认 8），而一轮执行 9~20 条
    # 是常态 —— 先截再筛的话，「取不到」的条目只要排在第 9 位之后就一条都不显示，
    # 而落库那份是全量、渲染出来是有的。同一个过程两个来源画出两样东西，正是这个模块
    # 一开始就要避免的那件事（模块 docstring 与 ai_think_log.js 都明写「两边显示一致」）。
    executed = [
        item
        for item in summarize_executed(
            getattr(record, "executed", ()), limit=TRACE_LIST_MAX_ITEMS
        )
        if item.get("failed") or item.get("empty")
    ][:LIVE_LIST_MAX_ITEMS]
    return {
        "round_index": int(getattr(record, "index", 0) or 0),
        "agent": _clip(getattr(record, "agent", ""), 40),
        "agent_round": int(getattr(record, "agent_round", 0) or 0),
        "outcome": _clip(getattr(record, "status", ""), 40),
        "parsed_ok": str(getattr(record, "status", "")) != "unparsable",
        # `None` 是「上游没上报」，一路原样带出去 —— 渲染成 0 就是把「不知道」说成「没有」。
        # 这一条对**四个**用量字段是同一句话：输入 / 输出 / 缓存读 / 缓存写。
        # 以前只有缓存那两个字段守住了，输入输出被 `or 0` 兜成 0，于是「这一轮调用失败、
        # 上游什么都没报」在面板上写成「输入 0 tokens」—— 一次没跑成的调用被说成没花钱。
        "tokens_input": _optional_int(getattr(record, "prompt_tokens", None)),
        "tokens_output": _optional_int(getattr(record, "completion_tokens", None)),
        "cache_read_tokens": getattr(record, "cache_read_tokens", None),
        "cache_write_tokens": getattr(record, "cache_write_tokens", None),
        "request_chars": int(getattr(record, "prompt_chars", 0) or 0),
        "context_chars": int(getattr(record, "context_chars", 0) or 0),
        "duration_ms": int(getattr(record, "duration_ms", 0) or 0),
        "error": _clip(getattr(record, "note", "")),
        "requests": summarize_requests(
            getattr(record, "requests", ()), limit=LIVE_LIST_MAX_ITEMS
        ),
        # 上面已经筛过、也截过了（顺序见那段注释：**先筛再截**）。
        "executed": executed,
        "dropped": summarize_dropped(
            getattr(record, "dropped", ()), limit=LIVE_LIST_MAX_ITEMS
        ),
        "response_text": _clip_live_response(getattr(record, "response_text", "")),
        "budget_notes": "\n".join(
            str(note) for note in (getattr(record, "budget_notes", ()) or ()) if str(note).strip()
        )[:LIVE_RESPONSE_MAX_CHARS],
        "correction_hint": _clip(getattr(record, "correction_hint", "")),
        "finish_reason": _clip(getattr(record, "finish_reason", ""), 40),
    }


def _clip_live_response(text: Any) -> str:
    content = str(text or "")
    if not content:
        return ""
    try:
        return truncate_text(content, LIVE_RESPONSE_MAX_CHARS)[0]
    except ValueError:  # pragma: no cover —— 同上，常量必然为正
        return content[:LIVE_RESPONSE_MAX_CHARS]
