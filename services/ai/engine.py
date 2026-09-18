"""多轮编排：把「一次分析」从头跑到尾。

## 这个模块解决什么

在它之前，平台跑的是一条假路径：`_build_stub_result()` 用变更条数算出一个风险等级，
密钥只当布尔阀门用。「分析」从来没有真的问过模型，配置界面配得再好也拿不到真结论。

这里把前面那些纯逻辑件串成一次真实的分析：组装提示词 → 问模型 → 解析 → 执行它索要的
上下文 → 回灌 → 直到它给出 `final` 或者轮次/预算耗尽。

## 三个刻意的设计

**一、注入依赖，不碰数据库、不碰网络。**
`client` 只要有一个 `complete(messages, *, temperature)`；`provider` 只要能按
`ContextProvider` 的四个方法取数。于是整个多轮循环可以在 pytest 里用假 client 跑完 ——
包括「模型第一轮乱答、第二轮改正」这种真实会出现但难以复现的情形。

**二、降级要记在结果里，不能悄悄发生。**
轮次耗尽、索取额度耗尽、模型不吐 JSON 只给 markdown、连续几轮都解析不出——每一条都会
写进 `degradation` 与 `error_message`。只返回一个「成功」的结论，用户就分不出
「模型看完说没问题」和「模型压根没答上来」——而这两件事的处理方式完全相反。

**三、提示词预算在**组装前**就压，不是发出去以后才发现超了。**
上下文条目和系统提示词、变更摘要、基线摘要抢同一份预算。所以先测出「除条目之外的开销」，
再把剩下的额度给条目，而不是先塞满再截断。

## 与增量分析的关系

本模块不决定「要不要复用上次的结果」——那是 `baseline.py` 与调用方（服务层）的事。
它只负责**这一轮**：把 `baseline_digest` 带进提示词，并保证报出来的结论经过
门槛过滤与判重。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol, Sequence

from services.ai.baseline import DEFAULT_BASELINE_CHARS
from services.ai.budget import (
    DEFAULT_MAX_ITEMS,
    DEFAULT_TOTAL_CHARS,
    ContextItem,
    enforce_budget,
    estimate_chars,
)
from services.ai.context_tools import (
    DEFAULT_MAX_TOOL_REQUESTS,
    ContextProvider,
    ContextTools,
    describe_request,
)
from services.ai.prompt import build_system_prompt, build_user_message
from services.ai.prompt_cache import CACHE_BREAKPOINT_KEY, mark_cache_breakpoint
from services.ai.protocol import (
    AnalysisPayload,
    Anomaly,
    DroppedItem,
    ProtocolError,
    build_correction_hint,
    ground_payload,
    looks_like_markdown_report,
    parse_payload,
    sanitize_requests,
)
from services.ai.rules import RuleThresholds, normalize_anomalies
from services.ai.scope import AnalysisScope
from services.ai.skill_loader import LoadedSkills
from utils.logger import log_print

# 结果状态。`degraded` 是「有产出，但流程没走完」——必须与 `succeeded` 分开，
# 否则用户分不出「模型看完说没问题」和「模型没答上来我们拿旧内容凑了一份」。
STATUS_SUCCEEDED = "succeeded"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"

# 退化原因。空字符串表示没有退化。
DEGRADE_NONE = ""
DEGRADE_ROUNDS = "rounds_exhausted"
DEGRADE_REQUESTS = "requests_exhausted"
DEGRADE_MARKDOWN = "markdown_report"
DEGRADE_PROTOCOL = "protocol_corrections_exhausted"

DEGRADATION_LABELS = {
    DEGRADE_ROUNDS: "轮次用尽，基于已有证据出结论",
    DEGRADE_REQUESTS: "上下文索取额度用尽，基于已有证据出结论",
    DEGRADE_MARKDOWN: "模型没有按协议输出 JSON，已按 markdown 报告降级保存",
    DEGRADE_PROTOCOL: "连续多轮无法解析出协议要求的 JSON",
}

# 给上下文条目留的最小额度。低于这个值就没什么可给的了，与其压到 0 不如如实记账。
_MIN_ITEM_BUDGET = 4_000


class _ChatClient(Protocol):
    """本模块对客户端的最小要求。`llm_client.LLMClient` 满足它，测试里的假实现也容易满足。"""

    def complete(self, messages: list[dict[str, str]], *, temperature: float | None = ...) -> Any: ...


# --------------------------------------------------------------------------
# 参数与结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineLimits:
    """一次分析的额度。默认值来自各模块自己的常量，避免这里再写一遍数字。"""

    max_rounds: int = 8
    max_tool_requests: int = DEFAULT_MAX_TOOL_REQUESTS
    max_items: int = DEFAULT_MAX_ITEMS
    prompt_char_budget: int = DEFAULT_TOTAL_CHARS
    baseline_char_budget: int = DEFAULT_BASELINE_CHARS
    max_corrections: int = 2
    temperature: float = 0.0

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "EngineLimits":
        """从项目配置里取值。**只认已知的键**，缺的用默认值。"""
        source = dict(config or {})
        known = {item.name for item in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        payload = {key: source[key] for key in known if key in source and source[key] is not None}
        return cls(**payload)


@dataclass(frozen=True)
class RoundRecord:
    """一轮的记账。写进 trace，用来回答「为什么这次只报了两条」。"""

    index: int
    status: str  # requests / final / unparsable
    request_count: int = 0
    item_count: int = 0
    refused_by_budget: int = 0
    truncated: int = 0
    note: str = ""
    # 这一轮的用量。输入 token 是**累计值**：提示词每轮都把上一轮的上下文重发一遍，
    # 所以轮次越靠后这一轮越贵 —— 逐轮列出来才看得出钱花在第几轮。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # `None` = 上游没报这个字段（见 context_tools 与 llm_client 里的同名口径）。
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # 这一轮发出去的提示词字符数、这一轮模型调用的耗时、
    # 以及这一轮**真正进了提示词**的上下文条目字符数（同上，不是取回的原始量）。
    prompt_chars: int = 0
    context_chars: int = 0
    duration_ms: int = 0


@dataclass(frozen=True)
class RoundProgress:
    """一轮跑完之后，**实时**往外报的那几个数（`run_analysis(on_round=...)`）。

    与 `RoundRecord` 的区别是用途：`RoundRecord` 是落库的账（trace 里逐轮一行，
    事后回答「钱花在第几轮」），这里是**跑的过程中**给调用方看的一眼 ——
    界面要一边跑一边显示「第 2/8 轮，已用 45 秒」。

    所以这里的 token 是**本次运行的累计值**（跨轮相加），而 `RoundRecord` 里的是本轮值；
    `elapsed_ms` 同理，是整次运行已过去的毫秒数（本轮的耗时看 `RoundRecord.duration_ms`）。
    """

    index: int
    max_rounds: int
    status: str  # requests / final / unparsable
    # 本次运行**累计**的用量，只含上游已上报的部分。
    prompt_tokens: int
    completion_tokens: int
    # 同上，累计；**任一轮没上报就是 `None`**（与 `EngineOutcome` 同一口径，见 `_sum_optional`）。
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    requests_used: int
    requests_remaining: int
    # 本轮真正进了提示词的上下文字符数。
    items_chars: int
    # 本次运行从开始到现在已过去的毫秒数。
    elapsed_ms: int


@dataclass(frozen=True)
class EngineOutcome:
    """一次分析的全部产出。"""

    status: str
    payload: AnalysisPayload | None = None
    # 门槛过滤、去重、封顶之后的最终清单（已经是展示顺序）。
    anomalies: tuple[Anomaly, ...] = ()
    # 被丢弃的条目与原因，跨轮次累计。
    dropped: tuple[DroppedItem, ...] = ()
    report_markdown: str = ""
    rounds: tuple[RoundRecord, ...] = ()
    requests_used: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    degradation: str = DEGRADE_NONE
    error_message: str = ""
    # prompt cache 的账目。`None` = 上游没报（**不是「没命中」**，见 `_sum_optional`）。
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # 这两个数是从哪种字段形态读来的（各家命名不一，见 llm_client._extract_cache_usage）。
    # 留着它才能回答「为什么这个端点从来不上报缓存」——是端点不支持，还是形态没认出来。
    cache_source: str = ""
    # 按工具类型的记账，键见 `context_tools._STAT_COUNTERS`。
    tool_stats: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    # 取回并**真正放进提示词**的上下文字符数。与 tool_stats 里的 source_chars 不是一个
    # 口径：那个是工具取回的原始量，可能被截断/被预算砍掉之后才进提示词。
    context_chars: int = 0
    duration_ms: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_SUCCEEDED

    @property
    def usable(self) -> bool:
        """有没有可以给人看的东西。降级但有报告也算有。"""
        return bool(self.report_markdown.strip() or self.anomalies)

    @property
    def degradation_label(self) -> str:
        return DEGRADATION_LABELS.get(self.degradation, "")

    @property
    def rounds_used(self) -> int:
        return len(self.rounds)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "degradation": self.degradation,
            "degradation_label": self.degradation_label,
            "error_message": self.error_message,
            "rounds_used": self.rounds_used,
            "requests_used": self.requests_used,
            "cache_hits": self.cache_hits,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_source": self.cache_source,
            "context_chars": self.context_chars,
            "duration_ms": self.duration_ms,
            "tool_stats": {kind: dict(counters) for kind, counters in self.tool_stats.items()},
            "anomaly_count": len(self.anomalies),
            "dropped": [
                {"kind": item.kind, "index": item.index, "reason": item.reason, "detail": item.detail}
                for item in self.dropped
            ],
            "rounds": [
                {
                    "index": item.index,
                    "status": item.status,
                    "request_count": item.request_count,
                    "item_count": item.item_count,
                    "refused_by_budget": item.refused_by_budget,
                    "truncated": item.truncated,
                    "note": item.note,
                    "prompt_tokens": item.prompt_tokens,
                    "completion_tokens": item.completion_tokens,
                    "cache_read_tokens": item.cache_read_tokens,
                    "cache_write_tokens": item.cache_write_tokens,
                    "prompt_chars": item.prompt_chars,
                    "context_chars": item.context_chars,
                    "duration_ms": item.duration_ms,
                }
                for item in self.rounds
            ],
        }


# 便于测试与调用方构造一个「什么都没跑」的结果。
def failed(error_message: str) -> EngineOutcome:
    return EngineOutcome(status=STATUS_FAILED, error_message=str(error_message or "分析失败"))


def _sum_optional(values: Sequence[int | None]) -> int | None:
    """把各轮的值加起来；**只要有一轮没上报，整次就是 `None`**。

    不能拿「手里有的那几轮」去算：一轮报了 900/1000、另一轮没报，只把报了的加起来会得出
    一个偏高的命中率 —— 而那个数字看起来完全正常，没有人会去怀疑它。

    空列表（一次模型调用都没成功）也是 `None`：没跑成与「花了 0」是两件事。
    """
    if not values:
        return None
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values if value is not None)


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def run_analysis(
    *,
    client: _ChatClient,
    provider: ContextProvider,
    loaded: LoadedSkills,
    scope: AnalysisScope,
    change_summary: str,
    limits: EngineLimits | None = None,
    thresholds: RuleThresholds | None = None,
    project_knowledge: str = "",
    project_instructions: str = "",
    baseline_digest: str = "",
    on_round: Callable[[RoundProgress], None] | None = None,
) -> EngineOutcome:
    """跑完一次分析。**不抛异常**：任何失败都变成 `status="failed"` 的结果。

    不抛的理由是调用方在后台线程/SSE 里跑，抛出去只会变成一个没人看的堆栈；而
    「这次为什么没出结论」是用户要看到的信息，必须结构化地带回去。

    `on_round` 每轮调一次（在 `RoundRecord` 落进结果之后），用来把进度实时推给界面。
    它抛异常**只会被记一条日志**，绝不作废这次分析 —— 调用方会用它发 SSE 事件、查预算，
    那些动作失败不该让一次已经跑了几分钟的分析白跑。不传它时行为与以前完全一致。

    ## 提示词缓存：请求是 append-extension 的

    `messages` 从第一轮起就是 append-only 的：system 只构造一次，之后每轮只 append
    一条 user 和一条 assistant。这**不是顺手写成这样**，而是跨轮次命中 prompt cache 的
    全部前提 —— 上游按**逐字节前缀**匹配缓存，任何一处重建（重新排序、重写 system、
    把上下文插回去）都会让第 2 轮及以后的缓存命中归零。`cache_control` 断点也挂在这条
    性质上：断点只落在「跨轮次不变、跨运行也尽量不变」的那几段末尾。
    """
    limits = limits or EngineLimits()
    thresholds = thresholds or RuleThresholds()

    tools = ContextTools(provider=provider, max_tool_requests=limits.max_tool_requests)
    system_prompt = build_system_prompt(
        loaded,
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
    )
    # 断点 ①：系统消息的末尾。它是整次请求里最稳的一段 —— 同一份平台 skill + 同一份
    # 项目知识包，在同一项目上跨运行**逐字节相同**，所以它值得一个独立的缓存条目。
    messages: list[dict[str, Any]] = [
        mark_cache_breakpoint({"role": "system", "content": system_prompt})
    ]
    # 断点 ③（移动的那个）挂在哪条消息上。见循环里的说明。
    movable_breakpoint: dict[str, Any] | None = None

    rounds: list[RoundRecord] = []
    dropped: list[DroppedItem] = []
    pending_items: tuple[ContextItem, ...] = ()
    budget_notes: list[str] = []
    correction_hint = ""
    degradation = DEGRADE_NONE
    payload: AnalysisPayload | None = None
    markdown_fallback = ""
    prompt_tokens = 0
    completion_tokens = 0
    # 用量记账：跨轮累计的缓存 token、真正进了提示词的字符数、起始时刻。
    cache_reads: list[int | None] = []
    cache_writes: list[int | None] = []
    cache_sources: list[str] = []
    context_chars = 0
    started_at = time.monotonic()

    def _usage_fields() -> dict:
        """所有 `EngineOutcome` 构造点共用的用量字段。

        这次要给每一个 return 各加 5 个字段，而本函数有 4 个构造点 —— 漏掉哪一个，那次
        运行在面板上就是一行空白，且**不会报错**（字段有默认值）。集中一处 splat 就没法漏。
        """
        return {
            "cache_read_tokens": _sum_optional(cache_reads),
            "cache_write_tokens": _sum_optional(cache_writes),
            # 第一次真的读到缓存字段的那一轮用的是哪种形态。`next(..., "")` 而不是取最后一轮：
            # 上游偶尔只在某些轮上报，取不到的那轮不该把已经看出来的形态覆盖成空。
            "cache_source": next((item for item in cache_sources if item), ""),
            "tool_stats": tools.stats,
            "context_chars": context_chars,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        }

    def _emit(record: RoundRecord) -> None:
        """记一轮的账，并把它**实时**报给调用方。

        **每一轮的 `RoundRecord` 都必须从这一个出口出去**（本轮共有 5 个构造点：
        请求轮、final 轮、markdown 降级轮、纠正额度耗尽轮、以及要重问的那一轮）。
        漏掉任何一个，那条路径上的进度就永远不会报出去 —— 而这几种恰恰都是用户最需要
        看到进度的时刻（跑偏了、在重问、降级了）。

        回调抛异常只记一条日志：调用方用它发 SSE 事件、查预算，那些动作失败不该让一次
        已经跑了几分钟的分析白跑（与 `run_analysis` 不抛异常同一条理由）。
        """
        rounds.append(record)
        if on_round is None:
            return
        try:
            on_round(
                RoundProgress(
                    index=record.index,
                    max_rounds=limits.max_rounds,
                    status=record.status,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    # 整个列表一起看：**任一轮没上报就是 None**（口径见 _sum_optional）。
                    cache_read_tokens=_sum_optional(cache_reads),
                    cache_write_tokens=_sum_optional(cache_writes),
                    requests_used=tools.requests_seen,
                    requests_remaining=tools.requests_remaining,
                    items_chars=record.context_chars,
                    elapsed_ms=int((time.monotonic() - started_at) * 1000),
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 回调失败不作废分析，见 docstring
            log_print(
                f"⚠️ AI 分析：轮次进度回调失败（{type(exc).__name__}：{exc}），已忽略，"
                "分析继续。",
                "AI",
                force=True,
            )

    for round_index in range(1, limits.max_rounds + 1):
        exhausted = tools.requests_remaining <= 0
        items, item_notes = _fit_items(
            pending_items,
            messages=messages,
            change_summary=change_summary,
            limits=limits,
        )
        # 真正进了提示词的字符数（不是工具取回的原始量，见 EngineOutcome.context_chars）。
        # 逐轮也算一份：总账说明「这次分析塞了多少进去」，而逐轮才说明**是哪一轮塞的**
        # —— 后几轮重发前几轮的全部上下文，钱正是花在那里。
        round_context_chars = sum(len(item.text) for item in items)
        context_chars += round_context_chars

        user_message = build_user_message(
            change_summary=change_summary,
            round_index=round_index,
            max_rounds=limits.max_rounds,
            items=items,
            baseline_digest=baseline_digest,
            budget_notes=[*budget_notes, *item_notes],
            requests_remaining=tools.requests_remaining,
            correction_hint=correction_hint,
            budget_exhausted=exhausted,
        )
        # 断点 ②：**第一轮** user 消息的末尾。变更清单（全量列出，顶到上限时约 87,000
        # 字符）、历史结论基线、首轮指引这三段在一批变更里是恒定的，而且占了整份提示词的
        # 大头 —— 跨运行复用缓存主要靠它。它**只挂第一轮**：后续轮次的 user 消息里有本轮
        # 才取回的上下文（每轮都不同），挂上去等于每轮重写一遍缓存。
        #
        # 断点 ③：**当前这一轮** user 消息的末尾，且只保留最新的一条（下一轮开始时把上一条
        # 上的断点摘掉，`movable_breakpoint` 就是记着「上一条是谁」的那个变量）。它换来的是
        # 「下一轮的整份历史都是我这次请求的前缀」—— 第 N 轮的内容成了第 N+1 轮的前缀，
        # 多轮的缓存命中正是从这里来的。
        #
        # 两个断点都只加在**消息字典上的内部标记**上，不改内容：摘掉/挪动断点不会让任何
        # 一条消息的字节发生变化，所以「挪断点」与「append-extension」不冲突。
        entry: dict[str, Any] = {"role": "user", "content": user_message}
        if movable_breakpoint is not None:
            movable_breakpoint.pop(CACHE_BREAKPOINT_KEY, None)
        mark_cache_breakpoint(entry)
        if round_index > 1:
            movable_breakpoint = entry
        messages.append(entry)

        round_started = time.monotonic()
        try:
            result = client.complete(messages, temperature=limits.temperature)
        except Exception as exc:  # noqa: BLE001 —— 网络/鉴权/超时都归为「这次没跑成」
            return EngineOutcome(
                status=STATUS_FAILED,
                rounds=tuple(rounds),
                dropped=tuple(dropped),
                requests_used=tools.requests_seen,
                cache_hits=tools.cache_hits,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error_message=f"调用模型失败（{type(exc).__name__}）：{exc}",
                **_usage_fields(),
            )

        text = str(getattr(result, "text", "") or "")
        round_prompt = int(getattr(result, "prompt_tokens", 0) or 0)
        round_completion = int(getattr(result, "completion_tokens", 0) or 0)
        prompt_tokens += round_prompt
        completion_tokens += round_completion
        round_cache_read = getattr(result, "cache_read_tokens", None)
        round_cache_write = getattr(result, "cache_write_tokens", None)
        cache_reads.append(round_cache_read)
        cache_writes.append(round_cache_write)
        cache_sources.append(str(getattr(result, "cache_source", "") or ""))
        # 本轮用量挂到每一个 RoundRecord 上（下面有 5 个构造点）。同样用 splat：逐处复制
        # 字段一定会漏，而漏掉的那一轮在 trace 里看着就像「这一轮没花钱」。
        round_extra = {
            "prompt_tokens": round_prompt,
            "completion_tokens": round_completion,
            "cache_read_tokens": round_cache_read,
            "cache_write_tokens": round_cache_write,
            "prompt_chars": len(user_message),
            "context_chars": round_context_chars,
            "duration_ms": int((time.monotonic() - round_started) * 1000),
        }
        messages.append({"role": "assistant", "content": text})

        try:
            parsed = parse_payload(text)
        except ProtocolError as exc:
            if looks_like_markdown_report(text):
                # 模型给了一份像样的 markdown 报告。与其把它扔掉重问，不如留下来当降级产出：
                # 内容通常是有用的，用户至少能读到。
                _emit(RoundRecord(round_index, "unparsable", note="按 markdown 报告降级", **round_extra))
                payload = None
                markdown_fallback = text.strip()
                degradation = DEGRADE_MARKDOWN
                break
            if limits.max_corrections <= 0:
                _emit(RoundRecord(round_index, "unparsable", note=str(exc)[:200], **round_extra))
                degradation = DEGRADE_PROTOCOL
                break
            # 重问也要占一轮：否则一个不肯说 JSON 的模型能把循环变成无限次重试。
            limits = replace(limits, max_corrections=limits.max_corrections - 1)
            correction_hint = build_correction_hint(exc)
            _emit(RoundRecord(round_index, "unparsable", note=str(exc)[:200], **round_extra))
            pending_items = ()
            budget_notes = []
            continue

        correction_hint = ""
        if parsed.is_final:
            payload = parsed
            _emit(RoundRecord(round_index, "final", item_count=len(items), **round_extra))
            break

        # `sanitize_requests` 同时返回「通过白名单的」与「被丢掉的及原因」——两样都要：
        # 前者去执行，后者进 trace（否则「为什么这次少看了一个文件」无从追溯）。
        requests, request_dropped = sanitize_requests(parsed.requests, scope)
        dropped.extend(parsed.dropped)
        dropped.extend(request_dropped)
        batch = tools.execute(requests)
        dropped.extend(batch.dropped)
        pending_items = batch.items
        budget_notes = _batch_notes(batch)
        _emit(
            RoundRecord(
                round_index,
                "requests",
                request_count=len(requests),
                item_count=len(batch.items),
                refused_by_budget=batch.refused_by_budget,
                truncated=batch.truncated,
                **round_extra,
            )
        )
    else:
        degradation = DEGRADE_ROUNDS

    # 这里**不需要**再判断一次「最后一轮是不是一份报告」：每一轮解析失败时都已经查过
    # `looks_like_markdown_report`（最后一轮也不例外），像报告的在循环里就留下了。
    # 曾经在这里又写了一遍，是一段永远走不到的死代码 —— 它让「降级路径」看起来有两条，
    # 读代码的人会以为少了一条覆盖。
    if payload is None and not markdown_fallback:
        # 既没拿到 final，也没有可用的 markdown。给一句能对上号的原因。
        if degradation == DEGRADE_NONE:
            degradation = DEGRADE_ROUNDS
        return EngineOutcome(
            status=STATUS_FAILED,
            rounds=tuple(rounds),
            dropped=tuple(dropped),
            requests_used=tools.requests_seen,
            cache_hits=tools.cache_hits,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            degradation=degradation,
            error_message=(
                f"没有拿到可用的结论：{DEGRADATION_LABELS.get(degradation, degradation)}"
            ),
            **_usage_fields(),
        )

    if payload is None:
        return EngineOutcome(
            status=STATUS_DEGRADED,
            report_markdown=markdown_fallback,
            rounds=tuple(rounds),
            dropped=tuple(dropped),
            requests_used=tools.requests_seen,
            cache_hits=tools.cache_hits,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            degradation=degradation,
            error_message=DEGRADATION_LABELS.get(degradation, ""),
            **_usage_fields(),
        )

    grounded = ground_payload(payload, scope)
    normalized = normalize_anomalies(grounded.anomalies, thresholds)
    dropped.extend(grounded.dropped)
    dropped.extend(normalized.dropped)

    if tools.requests_remaining <= 0 and degradation == DEGRADE_NONE:
        degradation = DEGRADE_REQUESTS

    return EngineOutcome(
        status=STATUS_DEGRADED if degradation else STATUS_SUCCEEDED,
        payload=grounded,
        anomalies=normalized.anomalies,
        dropped=tuple(dropped),
        report_markdown=grounded.report_markdown,
        rounds=tuple(rounds),
        requests_used=tools.requests_seen,
        cache_hits=tools.cache_hits,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        degradation=degradation,
        error_message=DEGRADATION_LABELS.get(degradation, ""),
        **_usage_fields(),
    )


# --------------------------------------------------------------------------
# 预算
# --------------------------------------------------------------------------


def _fit_items(
    items: Sequence[ContextItem],
    *,
    messages: Sequence[Mapping[str, str]],
    change_summary: str,
    limits: EngineLimits,
) -> tuple[tuple[ContextItem, ...], list[str]]:
    """把上下文条目压进「除条目之外还剩多少」的额度里。

    **不能拿总预算当条目额度**：系统提示词、变更摘要、基线摘要、前几轮的问答都要从
    同一份预算里出。按总预算给条目，必然超；超了以后要么被服务端拒绝，要么被截断，
    而模型分不出「文件就这么大」和「预算不够」——正是 `budget.py` 要解决的那个问题。
    """
    notes: list[str] = []
    overhead = estimate_chars(messages) + len(change_summary) + limits.baseline_char_budget
    residual = limits.prompt_char_budget - overhead
    if residual < _MIN_ITEM_BUDGET:
        notes.append(
            f"提示词已用掉 {overhead:,} 字（上限 {limits.prompt_char_budget:,}），"
            f"留给上下文的额度被压到 {_MIN_ITEM_BUDGET:,} 字，本轮内容会大幅压缩。"
        )
        residual = _MIN_ITEM_BUDGET

    result = enforce_budget(items, max_items=limits.max_items, total_chars=residual)
    notes.extend(result.notes)
    return result.items, notes


def _batch_notes(batch: Any) -> list[str]:
    """把一次批量执行里的异常情况转成给模型看的一句话。"""
    notes: list[str] = []
    if batch.refused_by_budget:
        notes.append(
            f"有 {batch.refused_by_budget} 个上下文请求因超出本次索取额度而未执行。"
        )
    if batch.truncated:
        notes.append(f"有 {batch.truncated} 条上下文因长度上限被截断。")
    failed = [item for item in batch.items if item.meta.get("tool_failed")]
    if failed:
        notes.append(
            f"有 {len(failed)} 条上下文取数失败（{'、'.join(describe_item(item) for item in failed[:3])}）。"
            "**取不到不等于没有风险**，不要据此下结论。"
        )
    return notes


def describe_item(item: ContextItem) -> str:
    return str(item.label or item.kind)


def request_labels(payload: AnalysisPayload | None) -> list[str]:
    """给 trace 用：这次分析向模型要过哪些东西。"""
    if payload is None:
        return []
    return [describe_request(request) for request in payload.requests]


# 保留给调用方做「轮次摘要」用。
__all__ = [
    "DEGRADATION_LABELS",
    "DEGRADE_MARKDOWN",
    "DEGRADE_NONE",
    "DEGRADE_PROTOCOL",
    "DEGRADE_REQUESTS",
    "DEGRADE_ROUNDS",
    "EngineLimits",
    "EngineOutcome",
    "RoundProgress",
    "RoundRecord",
    "STATUS_DEGRADED",
    "STATUS_FAILED",
    "STATUS_SUCCEEDED",
    "failed",
    "request_labels",
    "run_analysis",
]
