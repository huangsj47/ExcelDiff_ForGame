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
from typing import Any, Callable, Mapping, MutableMapping, Protocol, Sequence

from services.ai import trace_evidence
from services.ai.baseline import DEFAULT_BASELINE_CHARS
from services.ai.budget import (
    DEFAULT_MAX_ITEMS,
    DEFAULT_TOTAL_CHARS,
    MIN_KEEP_TURNS,
    ContextItem,
    TurnMemo,
    build_continuation_summary,
    compact_history,
    enforce_budget,
    estimate_chars,
    looks_like_context_overflow,
    truncate_text,
)
from services.ai.context_tools import (
    DEFAULT_MAX_TOOL_REQUESTS,
    ContextProvider,
    ContextTools,
    describe_request,
)
from services.ai.llm_client import non_negative_int
from services.ai.prompt import build_system_prompt, build_user_message, change_block
from services.ai.prompt_cache import CACHE_BREAKPOINT_KEY, mark_cache_breakpoint
from services.ai.protocol import (
    AnalysisPayload,
    Anomaly,
    DroppedItem,
    ProtocolError,
    TRUNCATED_OUTPUT_HINT,
    build_correction_hint,
    ground_payload,
    looks_like_markdown_report,
    looks_like_truncated_json,
    parse_payload,
    salvage_report_markdown,
    sanitize_requests,
)
from services.ai.rules import RuleThresholds, normalize_anomalies
from services.ai.scope import AnalysisScope
from services.ai.skill_contract import DIMENSION_IDS, DimensionSpec, dimension_ids_of
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
# 上游以「上下文超长」拒绝了请求，平台收缩提示词后把结论收回来了。**它是一次退化**：
# 模型是在一份被压过的提示词上作答的，与正常跑完不是一回事，必须说出来。
DEGRADE_CONTEXT = "context_overflow"
# 子代理模式（`services/ai/subagent.py`）下「有成员没跑成」或「它报出的结论没有进入最终
# 报告」。**它比上面几条都重**：那几条说的是「这块看过了但看得不够」，这一条说的是
# 「这块**没有人看过**」—— 而它最容易被读成「这里没问题」。所以它必须出现在
# `degradation` 上（抽屉会显示 ⚠️ 与这段话），不能只写在报告正文里。
DEGRADE_SUBAGENT = "subagent_gap"
# 对账轮（`subagent_verify`）没跑成。它与上面那条的分别要读清楚：那条是「有一块维度
# 没人看过」，这条是**「结论没经过复核」**—— 报告本身是完整的，只是少了「找反证」这一步。
# 所以它比 `DEGRADE_SUBAGENT` 轻一档（见 subagent.py 的 `_DEGRADE_RANK`），但**仍然要说**：
# 用户打开对账轮，图的正是那一步，静默没了等于他以为自己买到了没买到的东西。
DEGRADE_VERIFY = "subagent_verify"

DEGRADATION_LABELS = {
    DEGRADE_ROUNDS: "轮次用尽，基于已有证据出结论",
    DEGRADE_REQUESTS: "上下文索取额度用尽，基于已有证据出结论",
    DEGRADE_MARKDOWN: "模型没有按协议输出 JSON，已按 markdown 报告降级保存",
    DEGRADE_PROTOCOL: "连续多轮无法解析出协议要求的 JSON",
    DEGRADE_CONTEXT: "提示词超出模型上下文窗口，已压掉历史后收尾出结论",
    DEGRADE_SUBAGENT: (
        "子代理模式：有分片没有跑成、或它报出的结论没有进入最终报告"
        "（见报告末尾的「信息缺口（平台补充）」）"
    ),
    DEGRADE_VERIFY: (
        "子代理模式：对账轮（找反证）没有跑成，报告里的结论**没有经过这道复核**"
    ),
}

# 给上下文条目留的最小额度。低于这个值就没什么可给的了，与其压到 0 不如如实记账。
_MIN_ITEM_BUDGET = 4_000

# 收尾提示词里保留多少变更清单（字符）。只要够模型认出「这次改的是哪一片」即可：
# 收尾请求的前提就是「装不下」，所以它必须小到任何窗口都装得下。
_SALVAGE_SUMMARY_CHARS = 1_500


def _live_round_entry(record: RoundRecord) -> dict | None:
    """这一轮的「思考过程」条目。**算不出来就不给**，绝不让它影响分析。

    它进的是每轮都往外报的那条进度（`run_progress.publish`），而那条进度的读者是界面。
    所以这里与 `_emit` 里那句「回调失败不作废分析」同一条纪律：为显示服务的东西坏了，
    代价只能是**这一次没有过程可看**，不能是一次跑了几分钟的分析白跑。
    """
    try:
        return trace_evidence.live_round_entry(record)
    except Exception as exc:  # noqa: BLE001 —— 见 docstring，显示层不许弄挂分析
        log_print(
            f"⚠️ AI 分析：整理本轮明细失败（{type(exc).__name__}：{exc}），"
            "本轮不进「思考过程」，分析继续。",
            "AI",
            force=True,
        )
        return None


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
    # 这一轮是**谁**跑的。空串 = 常规的单代理运行；子代理模式下是 `S1`/`S2`…
    # （`services/ai/subagent.py`）。`index` 是**整个家族内全局递增**的轮次号，
    # 而 `agent_round` 是这一轮在**那个成员内部**的序号 —— 界面要显示「S1 · 第 2/4 轮」，
    # 而两个成员各自都有「第 1 轮」。全局序号是为了让 `uq_ai_trace_run_round`
    # （run_id + round_index）一个字节都不用改。
    agent: str = ""
    agent_round: int = 0
    request_count: int = 0
    item_count: int = 0
    refused_by_budget: int = 0
    truncated: int = 0
    note: str = ""
    # 这一轮的用量。输入 token 是**累计值**：提示词每轮都把上一轮的上下文重发一遍，
    # 所以轮次越靠后这一轮越贵 —— 逐轮列出来才看得出钱花在第几轮。
    #
    # `None` = 这一轮上游没报（见 llm_client 的同名口径）。**不许用 0 代替**：
    # 界面上「输入 0 tokens」会把一次调用失败说成「这一轮没花钱」。
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # `None` = 上游没报这个字段（见 context_tools 与 llm_client 里的同名口径）。
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # 这一轮发出去的提示词字符数、这一轮模型调用的耗时、
    # 以及这一轮**真正进了提示词**的上下文条目字符数（同上，不是取回的原始量）。
    prompt_chars: int = 0
    context_chars: int = 0
    duration_ms: int = 0
    # 下面是「证据」——回答「它为什么没读到 X」必需的东西。原先 trace 只记计数，
    # 而「模型没要」「取数失败」「被预算拒了」三件事在计数上长得一模一样（见
    # `services/ai/trace_evidence.py` 的模块文档）。
    #
    # 这一轮模型**原样返回的内容**（落库时按 `TRACE_RESPONSE_MAX_CHARS` 截断）：
    # 协议解释不了它、结论写得浅、只报了一条，都要靠它。
    response_text: str = ""
    # 索取的 / 取到的 / 被丢掉的，各自一条摘要（由 `trace_evidence` 编码成 JSON）。
    requests: tuple = ()
    executed: tuple = ()
    dropped: tuple = ()
    # 预算层给出的说明（压了几级、省了多少、哪几条取数失败）——与 `note`（引擎自己写的
    # 那些「上游拒了、改用收尾提示词」）分开记，两者的读者不是同一批：前者是模型看的，
    # 后者是复核的人看的。
    budget_notes: tuple = ()
    # 协议不合规时给模型的纠正提示：那一轮为什么被重问。
    correction_hint: str = ""


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
    # 本次运行**累计**的用量，只含上游已上报的部分；**任一轮没上报就是 `None`**
    # （与 `EngineOutcome` 同一口径，见 `_sum_optional`）。界面据此决定要不要显示
    # 「本次已用 N tokens」——`None` 时只说轮次，不补一个 0。
    prompt_tokens: int | None
    completion_tokens: int | None
    # 同上，累计；**任一轮没上报就是 `None`**（与 `EngineOutcome` 同一口径，见 `_sum_optional`）。
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    requests_used: int
    requests_remaining: int
    # 本轮真正进了提示词的上下文字符数。
    items_chars: int
    # 本次运行从开始到现在已过去的毫秒数。
    elapsed_ms: int
    # 子代理模式下这一轮属于哪个成员（`S1`/`S2`…；汇总那一次是空串，表示主代理自己）。
    # 三个都有默认值：单代理运行（今天唯一的路径）不传它们，行为逐字节不变。
    # 界面显示的是「分片 S1 (1/3) · 第 2 轮」：`agent_index`/`agent_total` 是成员在家族里
    # 的位次，`index` 仍是**这个成员内部**的轮次（与 `RoundRecord.index` 的全局序号不同，
    # 后者是为了 trace 的唯一约束）。
    agent: str = ""
    agent_index: int = 0
    agent_total: int = 0
    # 这一轮**发生了什么**：要了什么、拿到了什么、哪条取不到、模型原样返回了什么。
    # 形状与 `ai_usage_service.run_usage()["rounds"][i]` 逐字相同（由
    # `trace_evidence.live_round_entry` 产出），所以「思考过程」那一栏跑的时候与跑完之后
    # 是同一个渲染器 —— 两套键名就是两种真相。
    # 默认 `None`：不传它的调用方（老代码、测试替身）行为与这一层之前完全一样。
    round_entry: dict | None = None


@dataclass(frozen=True)
class CompactionReport:
    """这次分析压过几次历史、压掉了多少。

    存在的理由与 `degradation` 一样：**压缩不能悄悄发生**。一次「看起来正常、其实是在被
    压过的提示词上作答」的分析，与一次正常跑完的分析，读报告的人必须能分辨 —— 否则
    「这次结论怎么这么浅」永远查不出原因。
    """

    # 压了几次（每次是一条 `BudgetsOverflow` 或上游拒绝）。
    events: int = 0
    dropped_turns: int = 0
    dropped_chars: int = 0
    # 上游已经拒过一次、靠压缩/收尾救回来了。
    overflow_recovered: bool = False

    @property
    def happened(self) -> bool:
        return self.events > 0 or self.overflow_recovered

    def to_dict(self) -> dict:
        return {
            "events": self.events,
            "dropped_turns": self.dropped_turns,
            "dropped_chars": self.dropped_chars,
            "overflow_recovered": self.overflow_recovered,
        }


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
    # 因「上下文索取上限」用尽而**没有执行**的那些请求（`describe_request` 的一行标签，
    # 跨轮累计）。与 `requests_used` 是同一个账本的两侧：模型这次一共索取
    # `requests_used + len(refused_requests)` 次，其中后者一次都没轮到。
    #
    # 它必须落到结果里：光有一句「额度用尽，还有文件没看」，读的人既不知道**缺的是哪几块**
    # （于是无法判断这次的结论能不能用），也不知道**该调哪个参数**。抽屉据此把
    # 「还有文件没看」展开成「没轮到的是这几个」。
    refused_requests: tuple[str, ...] = ()
    # **逐轮累计、任一轮没上报就是 `None`**（见 `_sum_optional` 与 `_totals`）。
    # `None` 会一路走到 `run.tokens_input`（可空列）与费用估算那里 —— 那正是它该去的地方：
    # 费用会如实说「上游没有返回 token 数，无法估算」，而不是算出一个确定的 ¥0.00。
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
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
    # 上下文压缩的记账（见 CompactionReport）。
    compaction: CompactionReport = field(default_factory=CompactionReport)
    # 子代理模式（`services/ai/subagent.py`）下**每个成员**的账。空元组 = 没开子代理。
    #
    # 它必须落在结果里，理由与 `CompactionReport` 一样：一个成员跑失败了、或者因为预算
    # 不足被跳过，**绝不能表现为「那个维度没问题」**。落库的账是人能查的，所以要能回答
    # 「这次是谁跑的、谁没跑成、为什么」。里面是纯数据（dict / str），`to_dict` 保持 JSON 安全。
    subagents: tuple[Mapping[str, Any], ...] = ()
    # 被子代理模式**跳过**的成员（预算不足时）与它们的去向说明。与 `subagents` 分开：
    # 前者是「跑了但没跑成」，这里是「压根没跑」——两件事在报告里都要写成信息缺口，
    # 但读的人需要分得清。
    subagent_skipped: tuple[str, ...] = ()
    # **本次分析生效的检查维度清单**（id + 报告里给人看的中文名），来自
    # `LoadedSkills.dimensions`。它随结果一起落库（`result_payload`），理由只有一条：
    # 导出文档要把异常的 `category` 翻成中文名，而那时的运行记录里**必须**留着
    # 「这次分析当时生效的是哪一份」。
    #
    # **不许在导出时现查项目当前声明**：项目改了声明之后，历史结论会被按新清单重新贴
    # 标签（一条当时合法、归在 `performance` 下的发现，会在新清单里变成「未归类」）——
    # 那是篡改历史，而它看起来完全正常。
    dimension_specs: tuple[DimensionSpec, ...] = ()

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
            "compaction": self.compaction.to_dict(),
            "tool_stats": {kind: dict(counters) for kind, counters in self.tool_stats.items()},
            "anomaly_count": len(self.anomalies),
            "subagents": [dict(item) for item in self.subagents],
            "subagent_skipped": list(self.subagent_skipped),
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
    on_start: Callable[[RoundProgress], None] | None = None,
    seed_messages: Sequence[Mapping[str, Any]] = (),
    task_message: str = "",
    body_cache: MutableMapping[Any, ContextItem] | None = None,
) -> EngineOutcome:
    """跑完一次分析。**不抛异常**：任何失败都变成 `status="failed"` 的结果。

    不抛的理由是调用方在后台线程/SSE 里跑，抛出去只会变成一个没人看的堆栈；而
    「这次为什么没出结论」是用户要看到的信息，必须结构化地带回去。

    `on_round` 每轮调一次（在 `RoundRecord` 落进结果之后），用来把进度实时推给界面。
    它抛异常**只会被记一条日志**，绝不作废这次分析 —— 调用方会用它发 SSE 事件、查预算，
    那些动作失败不该让一次已经跑了几分钟的分析白跑。不传它时行为与以前完全一致。

    `on_start` 在**第一次模型调用之前**调一次（`index=0`，还没有任何一轮跑完）。
    它存在的理由很具体：`on_round` 只在 `client.complete` **返回之后**才发，所以从
    「开始跑」到「第一轮跑完」之间（读配置、装 skill、拼上下文，再叠上整整一次模型
    调用 —— 可以是几分钟）界面上一帧进度都没有。那段时间里抽屉写着「分析中：进度
    不可用」，结论面板一直挂着「AI 分析进行中...」—— 看起来像卡住了，实际它正在干活。
    有了这一帧，界面才分得清「读不到进度」与「正在跑第一轮」这两件**含义相反**的事
    （前者是我们看不见，后者是它在正常干活）。

    子代理模式下它同时解决第二个问题：汇总（主代理）开始跑时，快照里还留着**上一个
    分片**的归属，界面于是写着「分片 S3 · 第 2 轮」，而实际上主代理已经在跑了。
    归属由调用方贴（见 `subagent._call_engine` 的 `report`），引擎这一层只知道「开始了」。

    ## 子代理模式：`seed_messages` + `task_message` + `body_cache`

    这三个参数是**唯一**为子代理开的门（编排在 `services/ai/subagent.py`），不传时本函数
    的行为与它们不存在时**逐字节相同**（有一条回归测试钉着这件事）：

    * `seed_messages` —— 已经拼好的**共享前缀**（system + 含整份变更清单的第一条 user 消息，
      断点①②都挂好了）。传了它就**不重建** system，也不重发变更清单；
    * `task_message` —— 第 1 轮的 user 消息**原文**（「你负责哪几个维度」）；
    * `body_cache` —— N 个成员共享的正文缓存。**它只影响取数，不影响提示词的形状**：
      别的成员取过的同一份内容直接给全文，不再取第二次（见 `context_tools` 第 5 条）。

    于是子代理的请求长成 `[system, 共享消息, 任务书, assistant, …]`：前两条在**同一批的
    所有成员之间逐字节相同**，所以第一个成员写下的 prompt cache，后面每个成员（含汇总那
    一次）都能命中；成员私有的差异**全部**排在共享前缀之后。这条性质是子代理模式省钱的
    全部依据，改动这里之前先看 `subagent.build_seed_messages` 的说明与
    `tests/test_ai_subagent_cache.py`。

    ## 提示词缓存：请求是 append-extension 的

    `messages` 从第一轮起就是 append-only 的：system 只构造一次，之后每轮只 append
    一条 user 和一条 assistant。这**不是顺手写成这样**，而是跨轮次命中 prompt cache 的
    全部前提 —— 上游按**逐字节前缀**匹配缓存，任何一处重建（重新排序、重写 system、
    把上下文插回去）都会让第 2 轮及以后的缓存命中归零。`cache_control` 断点也挂在这条
    性质上：断点只落在「跨轮次不变、跨运行也尽量不变」的那几段末尾。
    """
    limits = limits or EngineLimits()
    thresholds = thresholds or RuleThresholds()

    # 本次分析生效的维度清单（id 那一份）。**来源只有一个**：`LoadedSkills.dimensions`。
    # 解析层的记账（`parse_payload(dimension_ids=…)`）、纠正提示、第一轮开场指令、
    # 以及报告末尾「未归类」那一节读的都是它 —— 四处各取一次平台出厂值，正好就是
    # 「校验按 A、提示词按 B」的来源，而那种错不会报错。
    dimension_ids = dimension_ids_of(loaded.dimensions)

    tools = ContextTools(
        provider=provider,
        max_tool_requests=limits.max_tool_requests,
        body_cache=body_cache,
    )
    if seed_messages:
        # 共享前缀整段照用（**拷贝**，别让下面的断点挪动改到调用方那份 —— 它正要被
        # 同一批的其它成员再用一次）。断点①②已经在里面了，system 也**不再重建**：
        # 重建出来的字符串即使内容一样，也要赌它逐字节相同，而赌输的代价是缓存全不命中。
        messages: list[dict[str, Any]] = [dict(item) for item in seed_messages]
    else:
        system_prompt = build_system_prompt(
            loaded,
            project_knowledge=project_knowledge,
            project_instructions=project_instructions,
        )
        # 断点 ①：系统消息的末尾。它是整次请求里最稳的一段 —— 同一份平台 skill + 同一份
        # 项目知识包，在同一项目上跨运行**逐字节相同**，所以它值得一个独立的缓存条目。
        messages = [mark_cache_breakpoint({"role": "system", "content": system_prompt})]
    # 断点 ③（移动的那个）挂在哪条消息上。见循环里的说明。
    movable_breakpoint: dict[str, Any] | None = None
    # 子代理模式下任务书是 `messages[2]`，压历史必须把它一起钉住（见 `compact_history`）。
    protect_head = 3 if seed_messages else 2

    rounds: list[RoundRecord] = []
    dropped: list[DroppedItem] = []
    # 因额度用尽而没执行的请求**分别是哪几个**（跨轮累计）。只有计数说明不了「缺的是
    # 哪几块」，而用户看到「额度用尽，还有文件没看」时第二个问题一定是「哪些」。
    refused_items: list[str] = []
    pending_items: tuple[ContextItem, ...] = ()
    budget_notes: list[str] = []
    correction_hint = ""
    degradation = DEGRADE_NONE
    payload: AnalysisPayload | None = None
    markdown_fallback = ""
    # 跨轮累计的输入 / 输出 token，与下面两个缓存字段**同一套口径**：逐轮记下来，
    # 出口处用 `_sum_optional` 合成 —— **只要有一轮没上报，整次就是 `None`**。
    # 以前这里是 `prompt_tokens = 0` 然后在循环里 `+=`，读不到的那一轮直接按 0 加进去，
    # 于是「上游没报」与「确实没花钱」变成同一个数（见 `_sum_optional` 的 docstring）。
    prompt_reads: list[int | None] = []
    completion_reads: list[int | None] = []
    # 用量记账：跨轮累计的缓存 token、真正进了提示词的字符数、起始时刻。
    cache_reads: list[int | None] = []
    cache_writes: list[int | None] = []
    cache_sources: list[str] = []
    context_chars = 0
    started_at = time.monotonic()
    # 上下文压缩的记账（见 CompactionReport）。这里只累加，出口在 _usage_fields。
    compaction_events = 0
    compaction_turns = 0
    compaction_chars = 0
    overflow_recovered = False
    # 每一轮留一句「索取了什么」，压历史时用它生成摘要（见 budget.compact_history）。
    round_memos: list[TurnMemo] = []
    # 这次分析**取到过**的全部上下文。收尾请求（上游拒绝之后）只带它们的目录。
    seen_items: list[ContextItem] = []
    # 压过历史之后补进本轮消息的那段记录。一旦压过就每轮都带着 —— 它描述的那些轮次
    # 已经不在对话里了，只带一次的话下一轮模型就再也看不到。
    recap_text = ""

    def _compaction_report() -> CompactionReport:
        return CompactionReport(
            events=compaction_events,
            dropped_turns=compaction_turns,
            dropped_chars=compaction_chars,
            overflow_recovered=overflow_recovered,
        )

    def _totals() -> dict:
        """跨轮累计的输入 / 输出 token。

        与 `_usage_fields` 里那两个缓存字段**同一套口径**（都在出口处过 `_sum_optional`）：
        只要有一轮没上报，整次就是 `None`。以前这里是 `+= 0`，把「上游没报」记成了
        「确实没花」，而费用那一栏会据此算出一个确定的 `¥0.00`。
        """
        return {
            "prompt_tokens": _sum_optional(prompt_reads),
            "completion_tokens": _sum_optional(completion_reads),
        }

    def _usage_fields() -> dict:
        """所有 `EngineOutcome` 构造点共用的字段。

        这次要给每一个 return 各加 5 个字段，而本函数有 4 个构造点 —— 漏掉哪一个，那次
        运行在面板上就是一行空白，且**不会报错**（字段有默认值）。集中一处 splat 就没法漏。

        它**不只有用量**：凡是「每个构造点都必须带上、漏了会静默出错」的字段都放这里。
        `dimension_specs` 就是这样一个 —— 它落进结果、导出文档按它把 category 翻成中文名，
        而漏掉的那条路径（失败 / 降级）恰好是最需要解释清「这一条为什么是未归类」的。
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
            "compaction": _compaction_report(),
            # 与 `_usage_fields` 同一个理由：本函数有 5 个 `EngineOutcome` 构造点，
            # 逐个加字段一定会漏，而漏掉的那条路径恰好是「失败 / 降级」——
            # 也就是最需要说清「缺了哪几块」的那几条。读出时取当前值（闭包），
            # 所以在循环里就构造的那两个出口拿到的也是这一轮为止的累计。
            "refused_requests": tuple(refused_items),
            "dimension_specs": loaded.dimensions,
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
                    **_totals(),
                    # 整个列表一起看：**任一轮没上报就是 None**（口径见 _sum_optional）。
                    cache_read_tokens=_sum_optional(cache_reads),
                    cache_write_tokens=_sum_optional(cache_writes),
                    requests_used=tools.requests_seen,
                    requests_remaining=tools.requests_remaining,
                    items_chars=record.context_chars,
                    elapsed_ms=int((time.monotonic() - started_at) * 1000),
                    # 逐轮明细（思考过程标签页）。放在**这个唯一出口**里算，5 条轮次路径
                    # 自动全覆盖；算它不许影响分析 —— 所以整段包在 try 里，失败了就只是
                    # 这一次没有过程可看（下面那条 except 已经在兜回调本身）。
                    round_entry=_live_round_entry(record),
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 回调失败不作废分析，见 docstring
            log_print(
                f"⚠️ AI 分析：轮次进度回调失败（{type(exc).__name__}：{exc}），已忽略，"
                "分析继续。",
                "AI",
                force=True,
            )

    # **开始跑了**：在第一次 `client.complete` 之前报一帧（见 docstring 的 `on_start`）。
    # 报的是「还没有任何一轮跑完」这个事实本身 —— 它的价值全在于**及时**：晚一帧就等于
    # 让界面在整整一次模型调用的时间里显示「进度不可用」。
    #
    # 与 `on_round` 同一条纪律：回调抛异常只记一条日志，绝不作废这次分析。
    if on_start is not None:
        try:
            on_start(
                RoundProgress(
                    index=0,
                    max_rounds=limits.max_rounds,
                    status="starting",
                    # token 是 `None` 而不是 0：**一次调用都还没发生**，报 0 会把它
                    # 说成「这一轮没花钱」（同 `live_tokens` 的口径）。
                    prompt_tokens=None,
                    completion_tokens=None,
                    cache_read_tokens=None,
                    cache_write_tokens=None,
                    requests_used=tools.requests_seen,
                    requests_remaining=tools.requests_remaining,
                    items_chars=0,
                    elapsed_ms=int((time.monotonic() - started_at) * 1000),
                    round_entry=None,
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 回调失败不作废分析，见 docstring
            log_print(
                f"⚠️ AI 分析：开始进度回调失败（{type(exc).__name__}：{exc}），已忽略，"
                "分析继续。",
                "AI",
                force=True,
            )

    for round_index in range(1, limits.max_rounds + 1):
        exhausted = tools.requests_remaining <= 0
        # 这一轮要写进 trace 的补充说明：压过历史、被上游拒过、走了收尾 —— 都是「这次分析
        # 不是正常跑完的」的证据，只留在日志里等于没说。
        round_notes: list[str] = []
        brief = _RoundBrief(
            round_index=round_index,
            max_rounds=limits.max_rounds,
            change_summary=change_summary,
            baseline_digest=baseline_digest,
            correction_hint=correction_hint,
            budget_exhausted=exhausted,
            budget_notes=tuple(budget_notes),
            pending_items=tuple(pending_items),
            recap=recap_text,
            requests_remaining=tools.requests_remaining,
            requests_total=limits.max_tool_requests,
            limits=limits,
            task_message=task_message,
            dimension_ids=dimension_ids,
        )
        items, user_message = _prepare_round(brief, messages)

        # **整份提示词**（系统提示词 + 变更清单 + 历史 + 本轮上下文）超出预算时压历史。
        #
        # 这一步补的是一个真窟窿：以前只有「本轮取回的上下文」受预算约束（`_fit_items`
        # 按剩余额度压条目），**历史不受任何约束** —— 每一轮都把上一轮的整条消息再发一遍，
        # 于是提示词随轮次单调增长，最后撞上模型窗口被上游拒绝，一次已经跑了几轮的分析
        # 连结论一起作废。压掉的是重复，不是信息：被压的轮次会留下一条记录（见
        # `budget.compact_history`），而且可以重新索取。
        if estimate_chars(messages) + len(user_message) > limits.prompt_char_budget:
            # 目标里**先扣掉本轮消息自己的位置**：只按总预算压历史的话，压到刚好等于预算、
            # 再把本轮消息加上去就又超了。而且条目的额度有下限（`_MIN_ITEM_BUDGET`）——
            # 压到负数它也会给 4,000 字，那 4,000 字必须有地方放。
            compacted = compact_history(
                messages,
                target_chars=max(0, limits.prompt_char_budget - len(user_message)),
                keep_recent_turns=MIN_KEEP_TURNS,
                memos=round_memos,
                # 子代理的**任务书**在第 3 条，必须一起钉住：被压掉之后它会忘了自己的分工
                # 而开始自由发挥，且不报任何错（见 `compact_history` 的说明）。
                protect_head=protect_head,
            )
            if compacted.compacted:
                messages[:] = list(compacted.messages)
                recap_text = _join_recap(recap_text, compacted.recap)
                compaction_events += 1
                compaction_turns += compacted.dropped_turns
                compaction_chars += compacted.dropped_chars
                round_notes.extend(compacted.notes)
                log_print(f"ℹ️ AI 分析：{compacted.notes[0]}", "AI", force=True)
                # 重算一遍：条目额度取决于「除条目之外占了多少」，压历史正是为了把它腾出来。
                # 少算这一步，压出来的空间就白压了（条目仍按旧额度被裁）。
                brief = replace(brief, recap=recap_text)
                items, user_message = _prepare_round(brief, messages)

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
        #
        # 子代理模式（`seed_messages`）下**第 1 轮也要记下来**：那时任务书上已经有一个断点，
        # 不记的话它会一直挂着，第 2 轮变成 4 个、第 3 轮起第 5 个被 `MAX_CACHE_BREAKPOINTS`
        # **静默丢掉** —— 丢的恰好是当前轮那条最有价值的。单代理路径第 1 轮不记（那时
        # 断点②就挂在第 1 轮的消息上，它本来就不该被挪走），行为与以前逐字节相同。
        entry = _mark_current({"role": "user", "content": user_message}, movable_breakpoint)
        if round_index > 1 or seed_messages:
            movable_breakpoint = entry

        round_started = time.monotonic()
        salvaged = False
        try:
            result = client.complete([*messages, entry], temperature=limits.temperature)
        except Exception as exc:  # noqa: BLE001 —— 网络/鉴权/超时都归为「这次没跑成」
            error_text = f"{type(exc).__name__}: {exc}"
            if not looks_like_context_overflow(error_text):
                # 这一轮**没跑成**也要留一行：不留的话，trace 里最后一行是上一轮，
                # 「这次分析为什么失败」在面板上看起来就是「跑了两轮、什么都没说」。
                _emit(RoundRecord(
                    round_index, "transport_error",
                    note=f"调用模型失败（{type(exc).__name__}）：{exc}"[:400],
                ))
                return EngineOutcome(
                    status=STATUS_FAILED,
                    rounds=tuple(rounds),
                    dropped=tuple(dropped),
                    requests_used=tools.requests_seen,
                    cache_hits=tools.cache_hits,
                    **_totals(),
                    error_message=f"调用模型失败（{type(exc).__name__}）：{exc}",
                    **_usage_fields(),
                )

            # 上游说装不下。**不能认输**：前面几轮的钱已经花了，空手而归是最坏的结果。
            #
            # 补救只有一步：换成一段**必然装得下**的「收尾」提示词，把结论要回来。
            #
            # 为什么不「压掉历史再试一次」：上游拒了，说明我们按字符估的那个水位在这台
            # 模型上不准（水位本身只有窗口的 60%，还被拒就意味着真实可用的量远小于声明
            # 的窗口）。那种偏差不是「少发一轮历史」能补上的 —— 压完还是超，只是多等一次
            # 超时、多留一条失败日志。而收尾提示词是千字符级的，任何窗口都装得下。
            log_print(
                f"⚠️ AI 分析：上游拒绝了本次请求（{error_text[:300]}）。"
                "按「提示词超出上下文窗口」处理，改用收尾提示词。",
                "AI",
                force=True,
            )
            # 上游**确实**以超长拒过 —— 这件事本身就要记下来：它意味着这次的结论是在一份
            # 被大幅裁剪的提示词上得到的。彻底失败的那条路径读的是 `error_message`。
            overflow_recovered = True
            salvaged = True
            messages[:] = messages[:1]
            items = ()
            user_message = _salvage_user_message(change_summary, seen_items)
            entry = _mark_current({"role": "user", "content": user_message}, movable_breakpoint)
            movable_breakpoint = entry
            round_notes.append(
                "上游以「上下文超长」拒绝了这次请求（累计已用 "
                f"{len(rounds)} 轮、{tools.requests_seen} 次索取），已改用「收尾」提示词"
                "（只带变更清单开头与已取内容目录）。"
            )
            try:
                result = client.complete([*messages, entry], temperature=limits.temperature)
            except Exception as final_exc:  # noqa: BLE001
                # 同上面那条：这一轮连着两次都没发出去，也要留痕（`transport_error`），
                # 否则「上游到底拒了什么」在 trace 上无从查起。
                _emit(RoundRecord(
                    round_index, "transport_error",
                    note=(
                        f"上游以「上下文超长」拒绝（{error_text[:200]}），收尾请求也被拒绝"
                        f"（{type(final_exc).__name__}: {final_exc}）"
                    )[:400],
                ))
                return EngineOutcome(
                    status=STATUS_FAILED,
                    rounds=tuple(rounds),
                    dropped=tuple(dropped),
                    requests_used=tools.requests_seen,
                    cache_hits=tools.cache_hits,
                    **_totals(),
                    error_message=(
                        "模型上下文不足：收尾请求也被拒绝"
                        f"（{type(final_exc).__name__}: {final_exc}）。"
                        "建议缩小本次分析的范围（按单个提交或指定文件分析），"
                        "或改用上下文窗口更大的模型。"
                    ),
                    **_usage_fields(),
                )

        usage = _usage_of(result)
        text = usage["text"]
        # 逐轮记下来，出口处再由 `_totals()` 合成（与下面两个缓存字段同一条口径）。
        # 这里**不能**写 `+= usage[...]`：读不到的那一轮会被按 0 加进去，
        # 「上游没报」就变成了「确实没花」。
        prompt_reads.append(usage["prompt_tokens"])
        completion_reads.append(usage["completion_tokens"])
        cache_reads.append(usage["cache_read_tokens"])
        cache_writes.append(usage["cache_write_tokens"])
        cache_sources.append(usage["cache_source"])
        # 真正进了提示词的字符数（不是工具取回的原始量，见 EngineOutcome.context_chars）。
        # 逐轮也算一份：总账说明「这次分析塞了多少进去」，而逐轮才说明**是哪一轮塞的**
        # —— 后几轮重发前几轮的全部上下文，钱正是花在那里。
        #
        # 记的是**最后真的发出去的那一份**：重试过一次的话，第一次那些条目根本没到模型手上，
        # 算进去会让「这一轮塞了多少」虚高。
        round_context_chars = sum(len(item.text) for item in items)
        context_chars += round_context_chars
        # 本轮用量挂到每一个 RoundRecord 上（下面有 5 个构造点）。同样用 splat：逐处复制
        # 字段一定会漏，而漏掉的那一轮在 trace 里看着就像「这一轮没花钱」。
        round_extra = {
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "cache_read_tokens": usage["cache_read_tokens"],
            "cache_write_tokens": usage["cache_write_tokens"],
            "prompt_chars": len(user_message),
            "context_chars": round_context_chars,
            "duration_ms": int((time.monotonic() - round_started) * 1000),
            # 模型这一轮的原文也挂在**每一个**构造点上（同一条 splat 的理由）：解析失败
            # 的那几轮恰恰是最需要原文的（它到底返回了什么，才没被认成 JSON）。
            "response_text": text,
        }
        messages.append(entry)
        messages.append({"role": "assistant", "content": text})

        try:
            parsed = parse_payload(text, dimension_ids=dimension_ids)
        except ProtocolError as exc:
            # 先看是不是**被截断的 JSON**（单次输出有上限，汇总那一步最容易撞上）。
            # 这个判断必须排在 `looks_like_markdown_report` **之前**：JSON 字符串里的
            # `\n# 变更理解` 同样能数到章节标题，晚一步就会把 JSON 原文当成正文存下来
            # （实测 run 7：55k 字的报告被包在 `{"status": "final", …` 里面）。
            salvaged_report = salvage_report_markdown(text)
            truncated = salvaged_report is not None and looks_like_truncated_json(text)
            if truncated and limits.max_corrections > 0:
                # 它不是「不肯说 JSON」，是**写太长了**。重问一次并要求压短，比直接降级
                # 强：降级只留下正文，结构化结论（人工跟进清单）会整份丢掉。
                limits = replace(limits, max_corrections=limits.max_corrections - 1)
                correction_hint = TRUNCATED_OUTPUT_HINT
                markdown_fallback = salvaged_report
                _emit(RoundRecord(
                    round_index, "unparsable",
                    correction_hint=correction_hint,
                    note=_combine_notes(round_notes, "输出被截断，已要求压短后重发"),
                    **round_extra,
                ))
                pending_items = ()
                budget_notes = []
                round_memos.append(TurnMemo(index=round_index, status="unparsable"))
                continue
            if truncated:
                # 纠正额度也用完了：正文抢得出来，结构化那一半确实没了 —— 留下正文。
                _emit(RoundRecord(
                    round_index, "unparsable",
                    note=_combine_notes(round_notes, "输出被截断，已抢出正文降级"), **round_extra,
                ))
                payload = None
                markdown_fallback = salvaged_report
                degradation = DEGRADE_MARKDOWN
                break
            if looks_like_markdown_report(text):
                # 模型给了一份像样的 markdown 报告。与其把它扔掉重问，不如留下来当降级产出：
                # 内容通常是有用的，用户至少能读到。
                _emit(RoundRecord(
                    round_index, "unparsable",
                    note=_combine_notes(round_notes, "按 markdown 报告降级"), **round_extra,
                ))
                payload = None
                markdown_fallback = text.strip()
                degradation = DEGRADE_MARKDOWN
                break
            if limits.max_corrections <= 0:
                _emit(RoundRecord(
                    round_index, "unparsable",
                    note=_combine_notes(round_notes, str(exc)[:200]), **round_extra,
                ))
                degradation = DEGRADE_PROTOCOL
                break
            # 重问也要占一轮：否则一个不肯说 JSON 的模型能把循环变成无限次重试。
            limits = replace(limits, max_corrections=limits.max_corrections - 1)
            correction_hint = build_correction_hint(exc, dimension_ids=dimension_ids)
            _emit(RoundRecord(
                round_index, "unparsable",
                # 那一轮为什么被重问：`note` 里是协议错误本身（给人看），
                # `correction_hint` 是随后发给模型的那段纠正提示（原样记下来）。
                correction_hint=correction_hint,
                note=_combine_notes(round_notes, str(exc)[:200]), **round_extra,
            ))
            pending_items = ()
            budget_notes = []
            round_memos.append(TurnMemo(index=round_index, status="unparsable"))
            continue

        correction_hint = ""
        if parsed.is_final:
            payload = parsed
            if salvaged:
                # 这份结论是在**被压过的提示词**上得出的，与正常跑完不是一回事。
                degradation = DEGRADE_CONTEXT
            _emit(RoundRecord(
                round_index, "final",
                item_count=len(items), note=_combine_notes(round_notes), **round_extra,
            ))
            round_memos.append(TurnMemo(index=round_index, status="final", items=tuple(items)))
            break

        # `sanitize_requests` 同时返回「通过白名单的」与「被丢掉的及原因」——两样都要：
        # 前者去执行，后者进 trace（否则「为什么这次少看了一个文件」无从追溯）。
        dropped_before = len(dropped)
        requests, request_dropped = sanitize_requests(parsed.requests, scope)
        dropped.extend(parsed.dropped)
        dropped.extend(request_dropped)
        batch = tools.execute(requests)
        dropped.extend(batch.dropped)
        refused_items.extend(batch.refused_items)
        pending_items = batch.items
        # **取到什么就记什么**，而不是「发出什么才记什么」：收尾提示词要用这份目录回答
        # 「你已经取到过哪些内容」，而它恰恰可能发生在「这一轮取到了、但这一轮的消息被
        # 上游拒了」之后 —— 那时按「发出去的」记就是空的，模型会以为自己什么都没看过。
        seen_items.extend(batch.items)
        budget_notes = _batch_notes(batch)
        # **被拒的索取必须把原因交回给模型。**
        #
        # 不给原因，模型那一轮只看到一句「（本轮没有附带任何上下文。）」—— 于是它把
        # 「我把 (commit, path) 配错了」写成「平台取数失败」，还郑重写进报告的信息缺口，
        # 读者据此去找一个**不存在的平台故障**。实测那一轮：28 条索取被拒（占索取总数
        # 23%），报告里因此多了一条错误的信息缺口，而模型自述是「两次点名取证均未拿到内容」。
        #
        # 原因早就算好了（`sanitize_requests` 的返回值），只是一直只进 trace 给人看。
        # 这里把**被拒的**交回去；额度类说明（refused_by_budget 等）已经由
        # `_batch_notes` 说过一遍，不要重复。
        rejected = (*parsed.dropped, *request_dropped)
        if rejected:
            budget_notes.append(_rejected_note(rejected))
        _emit(
            RoundRecord(
                round_index,
                "requests",
                request_count=len(requests),
                item_count=len(batch.items),
                refused_by_budget=batch.refused_by_budget,
                truncated=batch.truncated,
                # 这一轮的三份明细（要了什么、拿到的是内容还是「取不到」、丢了什么及原因）。
                # `dropped` 是跨轮累计的，所以按本轮的起点切片 —— 记成全部的话，第 8 轮会
                # 把第 1 轮丢的东西也列一遍，读的人以为这一轮丢了 20 条。
                requests=tuple(requests),
                executed=tuple(batch.items),
                dropped=tuple(dropped[dropped_before:]),
                budget_notes=tuple(budget_notes),
                note=_combine_notes(round_notes),
                **round_extra,
            )
        )
        # 这一轮索取了什么要留下来：压历史时它就是被压掉的那几轮的「记录」（见
        # budget.compact_history）。不留的话，压完历史模型就只知道自己拿过东西、
        # 不知道拿过什么，于是重新要一遍 —— 额度花两遍，还是没看到内容。
        round_memos.append(TurnMemo(index=round_index, status="requests", items=tuple(batch.items)))
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
            **_totals(),
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
            **_totals(),
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

    report_markdown = grounded.report_markdown
    if not seed_messages:
        # 「未归类」那一节：**单代理路径的报告正文里也必须有它**（与子代理路径同一份
        # 渲染函数 —— 两份实现迟早会长出两种措辞，而这里说的是一件很要紧的事）。
        #
        # 子代理路径由 `subagent.aggregate_outcomes` 在汇总之后追加，所以这里只在
        # **这一份报告就是最终报告**时追加。`seed_messages` 非空就是「我在替一家子里的
        # 某个成员跑」：那一份是分片交上去的素材，最终报告由汇总那一次产出 —— 在这里
        # 也追加一遍，最终报告里就会出现两节「未归类」。
        #
        # 渲染函数从 `subagent` **函数内导入**：它在模块级 import 本模块（要用
        # `run_analysis` 跑每个成员），顶层互相导入会成环。
        from services.ai.subagent import build_cap_section, build_unclassified_section

        section = build_unclassified_section(normalized.anomalies, dimension_ids)
        if section:
            report_markdown = (report_markdown.rstrip() + "\n\n" + section).strip() + "\n"
        # 「结论条数上限」那一节：**同一条理由、同一个位置**。上限生效时清单是静默变短的，
        # 报告正文读起来完全正常 —— 用户看到「这次报了 10 条」，看不出还有几条被截掉了。
        cap_section = build_cap_section(normalized.dropped)
        if cap_section:
            report_markdown = (report_markdown.rstrip() + "\n\n" + cap_section).strip() + "\n"

    return EngineOutcome(
        status=STATUS_DEGRADED if degradation else STATUS_SUCCEEDED,
        payload=grounded,
        anomalies=normalized.anomalies,
        dropped=tuple(dropped),
        report_markdown=report_markdown,
        rounds=tuple(rounds),
        requests_used=tools.requests_seen,
        cache_hits=tools.cache_hits,
        **_totals(),
        degradation=degradation,
        error_message=DEGRADATION_LABELS.get(degradation, ""),
        **_usage_fields(),
    )


# --------------------------------------------------------------------------
# 预算
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 一轮消息的组装
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _RoundBrief:
    """一轮里「与历史无关」的那些输入。

    攒成一包是因为**同一轮可能要组装两次**：压掉历史之后条目额度会变大，必须重算一遍
    （见 `_prepare_round`）。散着传十几个参数，两次调用之间漏传一个就是一条静默的错
    —— 而它表现为「分析结果说不清哪里不对」。
    """

    round_index: int
    max_rounds: int
    change_summary: str
    baseline_digest: str
    correction_hint: str
    budget_exhausted: bool
    budget_notes: tuple[str, ...]
    pending_items: tuple[ContextItem, ...]
    recap: str
    requests_remaining: int
    # 本次运行的总额度（`limits.max_tool_requests`）。**必须与 `requests_remaining` 一起
    # 给模型**：只报剩余数时，第 2 轮起那句「总共可索取 N 次」会把剩余说成总额 ——
    # 模型会把它原样抄进报告的信息缺口（见 `prompt._budget_line` 的 docstring）。
    requests_total: int
    limits: EngineLimits
    # 子代理模式下的**任务书**（第 1 轮的 user 消息原文，见 `run_analysis`）。空串 = 常规路径。
    task_message: str = ""
    # 本次生效的维度清单（id 那一份）。只给第一轮的开场指令用：清单里没有
    # `config_data` / `value_sanity` 的项目不该读到「按配表数值看」那两步
    # （见 `prompt._first_round_hint`）。默认是平台出厂那一份 —— 与这段改动之前
    # 逐字相同（`run_analysis` 每次都会按 `loaded.dimensions` 传真的那份进来）。
    dimension_ids: tuple[str, ...] = DIMENSION_IDS


def _prepare_round(
    brief: _RoundBrief, messages: Sequence[Mapping[str, Any]]
) -> tuple[tuple[ContextItem, ...], str]:
    """组装本轮要发的 user 消息，返回 `(真正进得去的上下文, 消息文本)`。

    **预算与消息必须用同一份变更清单文本**（`prompt.change_block`）：按清单全文算预算、
    消息里只放指针，会让平台白白少用几十万字符的额度；反过来的组合则是超预算。

    子代理模式的第 1 轮走上面那条 `task_message` 分支：变更清单已经在**共享消息**里
    （`seed_messages`）且已经按 `estimate_chars(messages)` 计入预算，这里再拼一遍会把它
    算两次 —— 于是平台会白白少给自己的成员几万字符的上下文额度。
    """
    if brief.round_index <= 1 and brief.task_message.strip():
        # 第 1 轮没有待发条目（`pending_items` 要等第一次索取之后才有），所以不必过 `_fit_items`。
        return (), brief.task_message
    block = change_block(brief.change_summary, round_index=brief.round_index)
    items, item_notes = _fit_items(
        brief.pending_items,
        messages=messages,
        change_summary=block,
        limits=brief.limits,
    )
    message = build_user_message(
        change_summary=brief.change_summary,
        round_index=brief.round_index,
        max_rounds=brief.max_rounds,
        items=items,
        baseline_digest=brief.baseline_digest,
        budget_notes=[*brief.budget_notes, *item_notes],
        requests_remaining=brief.requests_remaining,
        requests_total=brief.requests_total,
        correction_hint=brief.correction_hint,
        budget_exhausted=brief.budget_exhausted,
        history_recap=brief.recap,
        dimension_ids=brief.dimension_ids,
    )
    return items, message


def _mark_current(
    entry: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any]:
    """把「可挪动的缓存断点」挪到这一条消息上（见 `run_analysis` 里断点 ③ 的说明）。

    返回的就是 `entry` 本身（原地打了标记），返回值是为了让调用处写成
    `entry = _mark_current(entry, movable_breakpoint)` —— 少一层「忘了把新断点记下来」。
    """
    if previous is not None:
        # 上一条可能已经不在 messages 里了（压历史把它丢掉了）：从一个不再发送的字典上
        # 摘标记是空操作，不会出错，也不需要额外判断。
        previous.pop(CACHE_BREAKPOINT_KEY, None)
    mark_cache_breakpoint(entry)
    return entry


def _usage_of(result: Any) -> dict[str, Any]:
    """把一次模型调用的用量读出来（字段口径见 `llm_client.ChatResult`）。

    集中一处是为了让「重试之后那一次调用」与正常路径用同一套读法 —— 分散在三处读
    `getattr(result, ...)`，漏掉哪一处都是「这一轮看着没花钱」。
    """
    return {
        "text": str(getattr(result, "text", "") or ""),
        # 上游没报就是 `None`（不是 0）—— 口径与缓存那两个字段一致，见 `_sum_optional`。
        "prompt_tokens": non_negative_int(getattr(result, "prompt_tokens", None)),
        "completion_tokens": non_negative_int(getattr(result, "completion_tokens", None)),
        "cache_read_tokens": getattr(result, "cache_read_tokens", None),
        "cache_write_tokens": getattr(result, "cache_write_tokens", None),
        "cache_source": str(getattr(result, "cache_source", "") or ""),
    }


def _combine_notes(notes: Sequence[str], extra: str = "") -> str:
    """把这一轮的补充说明与构造点自己那句拼成一条 trace 备注。"""
    parts = [str(item).strip() for item in notes if str(item).strip()]
    if str(extra).strip():
        parts.append(str(extra).strip())
    return "；".join(parts)


def _join_recap(first: str, second: str) -> str:
    """把两段压缩记录接起来（一次分析可能压了不止一次）。空的那段不留空行。"""
    parts = [str(part).strip() for part in (first, second) if str(part or "").strip()]
    return "\n\n".join(parts)


def _salvage_user_message(change_summary: str, seen_items: Sequence[ContextItem]) -> str:
    """上游反复拒绝之后的**收尾**提示词。

    ## 为什么值得单独写一段

    这时的局面是：前面几轮的钱已经花了，模型也确实看到过一些内容，但整份提示词塞不进
    它的窗口。丢掉这次分析 = 全部白花；而**一份「证据不足、缺口写清楚了」的报告**仍然
    是用户能用的东西（`rules`/`protocol` 那一层本来就会按证据强度压结论）。

    ## 它必须小到任何窗口都装得下

    所以正文一条都不带：只带**变更清单的开头**（够认出改的是哪一片）与**取到过什么的
    目录**（`build_continuation_summary`，只有标签）。这两段加起来是千字符级，
    128k 窗口的模型也装得下。
    """
    head, truncated = truncate_text(str(change_summary or ""), _SALVAGE_SUMMARY_CHARS)
    blocks = [
        "# 本次变更（收尾请求）",
        "",
        "这次分析的提示词超出了模型的上下文窗口，平台已经把历史压过一轮，仍然装不下。"
        "所以这一轮只给你这些：变更清单的开头，以及你之前取到过什么的目录。",
        "",
        "## 变更清单（开头部分）",
        head + ("\n\n（清单在此处被截断，后面还有内容。）" if truncated else ""),
    ]
    if seen_items:
        blocks.extend(
            [
                "",
                "## 你已经取到过的内容",
                build_continuation_summary(seen_items, keep=8),
            ]
        )
    blocks.extend(
        [
            "",
            "## 现在要做的",
            "**立刻输出协议要求的 JSON**，用你已经看到过的内容作答：",
            "- 只报有证据支持的问题，每条的证据必须来自你确实看过的内容；",
            "- 你没能看完的部分写进报告的「信息缺口」，不要用猜测填补；",
            "- 不要再索取上下文 —— 这一轮之后本次分析就结束了。",
        ]
    )
    return "\n".join(blocks)


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


def _rejected_note(rejected: Any) -> str:
    """把「你上一轮这些索取没有被执行、原因是这些」说给模型（一句话，见调用处注释）。

    **最后那句不是客套**：实测里模型把「被拒」读成了「平台取数失败」，并据此写进报告的
    信息缺口。所以这里要显式说清「这不等于那里没有内容」，并告诉它下一步该做什么。
    """
    items = list(rejected)
    shown = items[:4]
    parts = []
    for item in shown:
        subject = str(getattr(item, "detail", "") or "").strip()
        reason = str(getattr(item, "reason", "") or "").strip()
        parts.append(f"{subject}（{reason}）" if subject else reason)
    more = f"，另有 {len(items) - len(shown)} 条同类未逐条列出" if len(items) > len(shown) else ""
    return (
        f"你上一轮有 {len(items)} 条上下文索取**没有被执行**：{'；'.join(parts)}{more}。"
        "**这不等于「那里没有内容」**，也不是平台取数失败 —— 按上面的原因改对之后重新索取即可；"
        "照原样再要一次不会被执行。"
    )


def _batch_notes(batch: Any) -> list[str]:
    """把一次批量执行里的异常情况转成给模型看的一句话。"""
    notes: list[str] = []
    if batch.refused_by_budget:
        notes.append(
            f"有 {batch.refused_by_budget} 个上下文请求因超出本次索取额度而未执行。"
        )
    cut = [item for item in batch.items if item.meta.get("truncated")]
    if cut:
        notes.append(_truncation_note(cut))
    failed = [item for item in batch.items if item.meta.get("tool_failed")]
    if failed:
        notes.append(
            f"有 {len(failed)} 条上下文取数失败（{'、'.join(describe_item(item) for item in failed[:3])}）。"
            "**取不到不等于没有风险**，不要据此下结论。"
        )
    return notes


def _truncation_note(cut: Sequence[ContextItem]) -> str:
    """截断那句话必须**点名是哪一条**，并说清**怎么把剩下的拿回来**。

    线上的一次真实核对逼出了这两件事：面板上写着「有 1 条上下文因长度上限被截断」，
    而那一轮要了两样东西（一份规格文档 + 一张配表）—— 模型（和人）都不知道是哪一条被砍的，
    更不知道下一步该做什么。

    旁边那两条说明都是既点名又给动作的：取数失败那条列出条目并说「取不到不等于没有风险」，
    预算省略那条（`budget._omission_note`）说「如果结论依赖被省略的部分，请重新索取」。
    只有这一条两个都没有，而它说的事情（**你看的内容少了一截**）比那两条更需要行动。

    ## 三种坐标，各自说清给哪类内容用

    「怎么拿回来」按内容形态分三种，而这个函数**看不到形态** —— 它拿到的只是一段渲染好的
    文本（配表的渲染与代码的渲染在这里长得一样，虽然配表的抬头自己写着怎么点名）。
    所以三种都给，并各自点明**是哪类内容用的** —— 模型自己知道它刚才要的是什么。
    按文本抬头去猜形态是可行的，但猜错的方向很坏：把一份规格文档说成「配表，拿不回来」，
    模型就不再去要了，而它本来只要带个 `lines` 就能拿到。

    ## 配表：**工作表**这一级拿得回来，**表内被砍掉的行**拿不回来

    原先这里写的是「配表的正文拿不回来」，那是**半错的**，而且半错的那一半正好把模型劝退：
    模型读到「配表拿不回来」，就不再去要本来拿得到的那几张表了。

    * **工作表这一级是可点名的**：`platform_provider.parse_sheet_window` 就把 `lines` 解释成
      「第几张工作表」，渲染出来的抬头自己写着 `"lines": "<第几张表>"` 并逐张点名缺了谁
      （`_assemble_workbook._render`）。所以这里要求模型「点名工作表」。
    * **表内被砍掉的行不可续**：配表没有行坐标，`_read_excel_sheets` 的 `window` 形参是
      「第几张表」而不是行区间；`_assemble_workbook` 的 `take` 降到 `_EXCEL_MIN_BODY_ROWS`
      之后仍装不下就落到 `truncate_text`（只砍尾巴）—— 重问同一张表得到**逐字节相同**的
      结果，那一段永久不可达。

    后面这半句仍然要说给模型听：不说，它就会对着一张被砍过的表下结论（那正是这条说明存在
    的理由）。但它是**表内行**这一级的结论，不能升格成「整类配表拿不回来」。
    """
    labels = "、".join(describe_item(item) for item in cut[:3])
    more = f"等 {len(cut)} 条" if len(cut) > 3 else ""
    return (
        f"有 {len(cut)} 条上下文因**单条长度上限**被截断（{labels}{more}），"
        "**只砍了尾巴**，后面的内容你没看到。要拿回来："
        "文本 / 代码类重新索取时点名行窗口（`lines=\"1200-1600\"`）；"
        "文档、差异与提交清单类点名段（`lines=\"4-6\"`）；"
        "**配表点名工作表**（`lines=\"2\"` 就是第 2 张表）。"
        "**同一张工作表里被砍掉的行没有坐标**（配表按行渲染、没有行窗口），"
        "要核对那些行请用 `file_diff` —— 它按改动行给。"
    )


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
    "DEGRADE_CONTEXT",
    "DEGRADE_MARKDOWN",
    "DEGRADE_NONE",
    "DEGRADE_PROTOCOL",
    "DEGRADE_REQUESTS",
    "DEGRADE_ROUNDS",
    "CompactionReport",
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
