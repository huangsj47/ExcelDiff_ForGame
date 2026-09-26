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

from services.ai import round_events, trace_evidence
from services.ai.baseline import DEFAULT_BASELINE_CHARS, digest_fingerprints
from services.ai.auto_sizing import conservative_tokens_for
from services.ai.budget import (
    DEFAULT_MAX_ITEMS,
    DEFAULT_TOTAL_CHARS,
    MIN_KEEP_TURNS,
    ContextItem,
    TurnMemo,
    compact_history,
    enforce_budget,
    estimate_chars,
    looks_like_context_overflow,
)
from services.ai.context_tools import (
    DEFAULT_MAX_TOOL_REQUESTS,
    DEFAULT_TOOL_LIMITS,
    ContextProvider,
    ContextTools,
)
from services.ai.evidence_prefetch import (
    PrefetchResult,
    forget_unfitted,
    prefetch_evidence,
)
from services.ai.frozen_repo import provider_repo_paths
from services.ai.round_message import (
    MIN_ITEM_BUDGET,
)
from services.ai.round_message import (
    fit_items as _fit_items,
)
from services.ai.round_message import (
    prepare_round as _prepare_round,
)
from services.ai.round_observability import (
    CORRECTION_BASELINE_COVERAGE,
    CORRECTION_CHANNEL_MISMATCH,
    CORRECTION_TRUNCATED_OUTPUT,
    CORRECTION_UNPARSABLE,
    replay_chars as _replay_chars,
    round_usage_fields,
    truncation_reason as _truncation_reason,
    with_observability as _merge_observability,
)
from services.ai.prompt import (
    build_system_prompt,
    build_user_message,
    change_block,
    render_context_items,
)
from services.ai.prompt_cache import (
    mark_cache_breakpoint,
    mark_current as _mark_current,  # noqa: F401
)
from services.ai.protocol import (
    TRUNCATED_OUTPUT_HINT,
    build_baseline_coverage_hint,
    AnalysisPayload,
    Anomaly,
    DroppedItem,
    ProtocolError,
    build_channel_mismatch_hint,
    build_correction_hint,
    build_markdown_reemit_hint,
    ground_payload,
    looks_like_markdown_report,
    looks_like_tool_call_envelope,
    looks_like_truncated_json,
    missing_baseline_statuses,
    parse_payload,
    repair_dsml_tool_calls_payload,
    repair_split_string_payload,
    repair_unescaped_quotes_payload,
    salvage_report_markdown,
    sanitize_requests,
)
from services.ai.budget_gate import SingleRunBudget
# 一次调用周边的三件小事（请求参数 / 截断判据 / 这一笔账怎么算）也从引擎里搬走了:
# 它们与「一轮怎么编排」无关，而本文件贴着 2000 行的 ERROR 闸门。
from services.ai.model_call import (
    complete_kwargs,
    output_limit_hit,
    tokens_for_budget,
    usage_of as _usage_of,  # noqa: F401
)
from services.ai.request_fingerprint import RoundDiagnostics
# 引擎侧的中文措辞（trace 备注 + 收尾提示词）：纯文本拼装，不碰引擎状态，搬出去让本文件
# 离 2000 行 ERROR 闸门远一点。**仍按原名回导**，于是下面几十个调用点一个字都不用改。
#
# `# noqa: F401` 必须写在**每一行别名上**：写在 `from ... import (` 那一行时 ruff 认为
# 整块是未使用导入，`ruff --fix` 会把这些刻意保留的回导直接删掉（真发生过）。
from services.ai.wrap_up import build_wrap_up_entry, should_request_final_answer
from services.ai.round_notes import (  # noqa: F401
    batch_notes as _batch_notes,  # noqa: F401
    combine_notes as _combine_notes,  # noqa: F401
    join_recap as _join_recap,  # noqa: F401
    rejected_note as _rejected_note,  # noqa: F401
    request_labels,  # noqa: F401
    salvage_user_message as _salvage_user_message,  # noqa: F401
)
from services.ai.rules import RuleThresholds, normalize_anomalies
from services.ai.scope import AnalysisScope
from services.ai.skill_contract import DIMENSION_IDS, DimensionSpec, dimension_ids_of
from services.ai.skill_loader import LoadedSkills
from models.ai_analysis.project_config import DEFAULT_MAX_ANOMALIES_PER_SUBAGENT
from utils.logger import log_print

# 退化（degradation）的取值域与中文标签：常量与文案，没有任何依赖，单独一层。
# 搬到 `degradation.py` 是为了让本文件离 2000 行 ERROR 闸门远一点（本文件顶着那个闸门）。
#
# **仍按原名回导**：`subagent.py`、`result_payload` 与十几处测试都是按
# `engine.DEGRADE_*` 这个**属性**取用的，直接搬走就等于把它们全部打断。
from services.ai.degradation import (  # noqa: F401 —— 读侧与测试按属性名取用
    DEGRADATION_LABELS,
    DEGRADE_BUDGET,
    DEGRADE_CONTEXT,
    DEGRADE_MARKDOWN,
    DEGRADE_NONE,
    DEGRADE_PROTOCOL,
    DEGRADE_REQUESTS,
    DEGRADE_ROUNDS,
    DEGRADE_SUBAGENT,
    DEGRADE_VERIFY,
)

# 结果状态。`degraded` 是「有产出，但流程没走完」——必须与 `succeeded` 分开，
# 否则用户分不出「模型看完说没问题」和「模型没答上来我们拿旧内容凑了一份」。
STATUS_SUCCEEDED = "succeeded"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"



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


def _round_counts(record: RoundRecord) -> dict | None:
    """这一轮的**计数**（要了几次 / 执行几条 / 几条取不到 / 几条被截断 / 报了几条候选）。

    **必须从这一层出去**：进「思考过程」的明细是截断过的（`LIVE_LIST_MAX_ITEMS = 8`，
    而且 `executed` 只留取不到的），拿它数数会把「索取 19 次、执行 3 次」数成「8 次、0 次」。
    逐轮事件账本的计数列（`models/ai_analysis/round_event.py`）读的就是这一份。

    与 `_live_round_entry` 同一条纪律：算不出来就是 `None`，绝不作废一次付费分析。
    实现放在 `services/ai/round_events.py`（这个文件贴着行数闸门，只留薄钩子）。
    """
    try:
        return round_events.round_counts(record)
    except Exception as exc:  # noqa: BLE001 —— 见 docstring，显示层不许弄挂分析
        log_print(
            f"⚠️ AI 分析：整理本轮计数失败（{type(exc).__name__}：{exc}），"
            "这一轮的计数按未上报处理，分析继续。",
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
    # 每个**分片**最多报几条结论（仅子代理模式）。与 `max_tool_requests` 一样是**额度**：
    # 它会写进分片任务书，模型照它写，所以省的是输出 token。
    #
    # 单代理那条路用不到它（那时 `max_anomalies_per_run` 就是全部），所以留着默认值即可。
    max_anomalies_per_subagent: int = DEFAULT_MAX_ANOMALIES_PER_SUBAGENT
    prompt_char_budget: int = DEFAULT_TOTAL_CHARS
    baseline_char_budget: int = DEFAULT_BASELINE_CHARS
    max_corrections: int = 2
    # 「输出通道错位」（模型把取数请求写成工具调用信封）**单独一本账**，默认 1 次：
    # 那 2 次 `max_corrections` 是给截断 / JSON 结构错误的，共用会让「撞过两次截断的
    # 分片再遇一次信封」整片阵亡。**重问仍占一轮**（见循环体里那句注释）——换本账
    # 不解除循环上界。判据与理由见 `protocol.repair_dsml_tool_calls_payload`。
    max_channel_corrections: int = 1
    temperature: float = 0.0
    # **单次输出上限**（token）。`None` = 不传这个字段，用端点的默认值（**默认行为逐字节
    # 不变**，见 `llm_client._request_body`）。
    #
    # 它管的是「一次模型调用最多能写多长」，与分析逻辑无关：配了它，撞上上限与否就由项目
    # 说了算，而不是由网关那个看不见的默认值说了算。**它不掐任何一轮的内容** ——
    # 谁该看多少上下文、该报几条结论，与这个数字无关（`EngineLimits` 里其余那些才管
    # 那些事）。撞上上限时的处理见 `run_analysis` 里 `finish_reason == "length"` 那一支。
    max_output_tokens: int | None = None
    tool_limits: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_TOOL_LIMITS))

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
    # 上游停止原因（stop / length / content_filter…）。`length` 是输出被硬截断的直接证据。
    finish_reason: str = ""
    # **这一轮的输出撞上了单次输出上限**。
    #
    # 判据是两个信号的并集：上游明说的 `finish_reason == "length"`，以及我们自己看出来的
    # 括号不配平（`protocol.looks_like_truncated_json`）。前者更直接（它不依赖形状 ——
    # 断点恰好落在一个仍然合法的 JSON 上时，括号是配平的），后者是端点上没有
    # `finish_reason` 时的兜底。
    #
    # 单独一个 bool 而不是让读的人自己去比 `finish_reason`：**「截断」这件事有两个来源**，
    # 而在 trace 上「这一轮被截断过」必须是一个能直接读到的结论 —— 让每个读者各写一遍
    # 那个并集判据，迟早会有人只判 `finish_reason`（于是漏掉端点上不报的那一半）。
    output_budget_hit: bool = False
    # ---- 逐轮可观测性（P5）--------------------------------------------------
    # 回答「这一轮的钱花在哪、为什么」。口径：**未上报存 `None`，不存 0**（0 是确定的
    # 观测值，「上游没报」不是）。每条字段的判据与理由见 `round_observability`。
    # 这一轮模型**可见正文**的字符数（剥掉 `think` 块之后）。
    # `None` = 读不出来（没有响应文本）。
    visible_response_chars: int | None = None
    # 上游若提供推理 token 就记在这里（由 `llm_client._extract_reasoning_usage` 读）。
    # **`None` = 上游没提供，不是 0** —— 这一格是「能不能下调单次输出上限」的前置数据。
    reasoning_tokens: int | None = None
    # 三类耗时与逐请求指纹**不在这张账上**：它们是诊断值，直接由 `RoundDiagnostics`
    # 落进逐轮事件账本（见 `services/ai/request_fingerprint.py`，那边有全部口径）。
    # 这一轮交给模型的**工具正文**字符数里，有多少是**跨成员重放**的
    # （同一个证据在别的分片里已经取过、这一轮又把全文发了一遍）。
    # 子代理模式下这是「多角色串行」的主要成本来源之一，而它原先只在按类型的统计里
    # 能看出来、在逐轮账上完全没有。
    tool_replay_chars: int = 0
    # 这一轮**为什么被截断**（如 `tool_limit_file_content` / `item_shrink_level_1`）。
    # 空串 = 这一轮没有交付截断。截断不可归因正是任务 E3 点名的毛病。
    truncation_reason: str = ""
    # 这一轮**为什么被纠正**（如 `unparsable` / `truncated_output` / `channel_mismatch`）。
    # 空串 = 没纠正。纠正的**成本**就是它占掉的那一轮（`index` 与 `max_rounds` 的对比）。
    correction_reason: str = ""
    # 这一轮**报出了几条候选结论**（模型这一轮交回的 `anomalies` 条数）。
    #
    # 只有交结论的那一轮（`status == "final"`）才有值，其余轮次是 `None` —— 而 `None`
    # 与 `0` 是两件事：前者是「这一轮不是交结论的那一轮」，后者是「交了一份结论，
    # 一条候选都没有」。逐轮事件账本按这个口径落列（`round_events.event_fields`），
    # 界面按「每个成员报了几条候选」读它。
    candidates: int | None = None


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
    # 这一轮的**计数**（要了几次 / 执行几条 / 几条取不到 / 几条被截断 / 几条被拒 /
    # 报了几条候选），由 `round_events.round_counts` 从**未截断**的 `RoundRecord` 上算。
    #
    # 与 `round_entry` 分开是必要的：那一份是**给人看的明细**，条数有上限（8 条）而且
    # 「取不到」是筛过一遍的，拿它数数会把 19 次索取数成 8 次。逐轮事件账本
    # （`models/ai_analysis/round_event.py`）的计数列读的就是这一份。
    # 默认 `None`：老调用方、测试替身不传它，行为与这一层之前完全一样。
    round_counts: dict | None = None
    # 这一轮的**诊断值**（推理 token / 来源标记 / 三类耗时 / 请求指纹的哈希与计数），
    # 形状 = 逐轮事件账本的那几列（`RoundDiagnostics.event_fields`）。与 token 那几格
    # 同一口径：**没上报就是 `None`**。默认 `None`：老调用方、测试替身不传，
    # 行为与这一层之前完全一样。
    round_diagnostics: dict | None = None


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
    # **报告正文** —— 给人看的那一份。单代理路径下它就是模型写的报告；子代理路径下正文
    # 主体也是模型写的汇总报告，复核摘要（开篇）与复核标注明细 / 平台那几节（未归类 /
    # 条数上限 / 信息缺口）分列前后；模型那份草稿在有复核结论时另存一份在 `draft_markdown`
    # 里（给外部读侧的存档，2026-09-23 起正文不再把草稿顶出去）。
    #
    # 它同时是 `ai_analysis_run.response_text`（落库）与结论载荷的 `report_markdown`，
    # **一个字节都不许有机器 JSON**（AI-P0-05，见 `verdict` 那段说明）。
    report_markdown: str = ""
    # 复核裁决的**结构化**那一份（`verdict.Reduction.as_dict()` 的产物，见
    # `services/ai/verdict.py`）。承载它的不再是报告正文末尾那行 HTML 注释 ——
    # 那个做法在安全 Markdown 渲染器下会变成一段可见的乱码（实测 run 20 的
    # `response_text` 里 35.3% 是那段 json，见 AI-P0-05），而且「从 Markdown 反向解析
    # 机器状态」本身就是个坏契约。`result_payload` 只认这一个字段。
    #
    # `None` = 这次没有复核（单代理路径、没开对账轮），或者复核对结论没有产生任何影响
    # —— 两种情形在读取侧是同一件事：按「没有裁决」处理，什么都不改。
    verdict: Mapping[str, Any] | None = None
    # **模型自己写的那份汇总草稿**，以及**对账轮的整份原文**。两者都是「存档」：正文主体
    # 是模型草稿，这两份留在这里供调试与追溯（被改判结论的规范值在落库异常表与
    # `final_findings` 里，呈现层不重复造第二份清单）。
    #
    # 草稿**只有有复核标注时才另存这一份** —— 没开对账轮时它同时是正文的开头，再存一份
    # 就是同一段字节在载荷里出现两次，而「同一份东西重复持久化」正是这一批缺陷的形态。
    draft_markdown: str = ""
    verify_report_markdown: str = ""
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
    single_run_budget: SingleRunBudget | None = None,
    mandatory_files: Sequence[Any] = (),
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
      别的成员取过的同一份内容直接给全文，不再取第二次（见 `context_tools` 第 5 条）；
    * `mandatory_files` —— **本成员分到的必读清单**（`(仓库, 提交, 路径)` 三元组，
      P1b）。它只被用来**每轮算一次进度**（「还有几条没取到证据」，见
      `mandatory_progress`），既不改工具白名单、也不改取数行为；不传时那一段恒为空，
      提示词与从前**逐字节相同**。

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

    ## 单次 token 硬上限（`single_run_budget`）

    不传它时本函数的行为与它不存在时**逐字节相同**（老调用点一个都不用改）。传了就在
    每一轮开头问一次「还付得起下一轮吗」，付不起就带着已经拿到的证据停住 —— 判据与
    保证的边界写在 `services/ai/budget_gate.py`，那**唯一的**公式在
    `auto_sizing.single_run_guard`。停住的原因进 `degradation`（`DEGRADE_BUDGET`），
    与「轮次用尽」「索取额度用尽」并列成三件不同的事：那两条是额度，这条是钱。
    """
    limits = limits or EngineLimits()
    thresholds = thresholds or RuleThresholds()

    # 逐请求指纹（诊断「缓存为什么没命中」）+ 三类耗时。**一次运行（一个成员）一个**：
    # 它保存「上一次请求」用来算公共前缀。算法、口径与理由全在 `RoundDiagnostics`。
    diagnostics = RoundDiagnostics.for_run(client, provider=provider)
    diagnostics.start()

    # 本次分析生效的维度清单（id 那一份）。**来源只有一个**：`LoadedSkills.dimensions`。
    # 解析层的记账（`parse_payload(dimension_ids=…)`）、纠正提示、第一轮开场指令、
    # 以及报告末尾「未归类」那一节读的都是它 —— 四处各取一次平台出厂值，正好就是
    # 「校验按 A、提示词按 B」的来源，而那种错不会报错。
    dimension_ids = dimension_ids_of(loaded.dimensions)

    # 这一轮提示词里那份历史清单的指纹（给「逐条交代状态」那条规矩用）。
    # 从**渲染好的摘要**里读回来，理由见 `baseline.digest_fingerprints`：这条规矩的
    # 适用范围就是模型看到的那份清单，而只有那段文本知道 `_fit_groups` 丢掉了哪几条。
    baseline_fingerprints = digest_fingerprints(baseline_digest)

    tools = ContextTools(
        provider=provider,
        max_tool_requests=limits.max_tool_requests,
        limits=limits.tool_limits,
        body_cache=body_cache,
        mandatory_files=tuple(mandatory_files),
    )
    # 冻结仓库的只读范围（工作包 D 的 P1）。**一次解析、整次分析复用**：
    # provider 自己知道仓库与 tip（它一直在按提交行取数），引擎不该再问一遍 ——
    # 两处各推一次必然漂移，而漂移的表现是「协议层放行的路径取数层读不了」。
    repo_paths = provider_repo_paths(provider)
    # P3：**把最有用的证据提前到第 1 轮**（有界、可追、可复现 —— 三条纪律与上界都在
    # `evidence_prefetch` 的模块 docstring 里）。位置就在这里：`tools` 已建好，而循环
    # 还没开始 —— 第 1 轮的消息正是由下面 `pending_items` 拼出来的，晚一步就白做。
    # `repo_paths` 一并传下去：预取里的「邻近窗口」会落在**未改动**的文件上（金标形态：
    # 改了定义、调用方没改），它在协议层要靠冻结 tip 的跟踪集合放行。
    # **只在单代理那条路上跑预取**。子代理模式的第 1 轮是**任务书**（`task_message`），
    # 那时本函数不拼 `pending_items`（`_prepare_round` 直接返回任务书）—— 预取出来的
    # 东西只能带到第 2 轮，而**模型的第 1 轮索取已经执行过了**：那些请求会命中预取写下的
    # 缓存，拿回一句「见上文那一节」，而那一节根本还没出现。这不是概率问题，是稳定复现的
    # 假话，所以宁可不预取（分片的「证据前置」由 `subagent._render_candidates` 那份候选
    # 地址负责，那是另一条路）。
    prefetch = (
        PrefetchResult()
        if task_message.strip()
        else prefetch_evidence(tools, scope=scope, limits=limits, repo_paths=repo_paths)
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
    pending_items: tuple[ContextItem, ...] = prefetch.items
    budget_notes: list[str] = list(prefetch.notes)
    correction_hint = ""
    degradation = DEGRADE_NONE
    payload: AnalysisPayload | None = None
    markdown_fallback = ""
    # 「已经为 markdown 重问过一次了吗」。**只给一次机会**，不跟着 `max_corrections`
    # 走：把上一条原样转成 JSON 是个确定性很高的动作，第一次没做、第三次更不会做，
    # 而每一次重问都要把整份提示词重发一遍（钱与时间都按轮次走）。
    markdown_retried = False
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

    def _note_usage(usage: dict[str, Any]) -> None:
        """一次调用的用量进本次运行的总账（**唯一一处**）。

        常规的某一轮与「一条结论都没交回来」时补发的那一次收尾调用都走它 ——
        兜底那次要是不记账，平台自己报的数就是错的（见 `wrap_up`）。

        逐次记下来、出口处由 `_totals()` 合成（与两个缓存字段同一条口径）。这里
        **不能**写 `+= usage[...]`：读不到的那一次会被按 0 加进去，「上游没报」
        就变成了「确实没花」。诊断账（推理 token 的来源、三类耗时、请求指纹）也在
        这里扣一次，见 `RoundDiagnostics`。
        """
        prompt_reads.append(usage["prompt_tokens"])
        completion_reads.append(usage["completion_tokens"])
        cache_reads.append(usage["cache_read_tokens"])
        cache_writes.append(usage["cache_write_tokens"])
        cache_sources.append(usage["cache_source"])
        diagnostics.note_usage(usage)
    started_at = time.monotonic()
    # 上下文压缩的记账（见 CompactionReport）。这里只累加，出口在 _usage_fields。
    compaction_events = 0
    compaction_turns = 0
    compaction_chars = 0
    overflow_recovered = False
    # 这次运行**撞过上下文窗口**（三种补救里任何一种发生过都算）。它**不能按轮记**：
    # 第 3 轮撞了窗口、第 5 轮才交结论时，结论仍然是「在一份被裁剪过的提示词上得到的」，
    # 只看当前轮就会把这次降级记成一次干净的运行 —— 而用户正是靠这个标签判断
    # 「这份报告我该信几分」。
    context_overflow_recovered = False
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
                    # **报出来的上限不许小于这一轮的序号**：有两处会超出
                    # `limits.max_rounds` —— 借来转格式的那一轮（`format_retry_granted`）
                    # 与额度用尽后补发的那一次收尾（`wrap_up`）。照抄配置值的话，
                    # 面板上会出现「第 9/8 轮」这种读不通的进度。
                    max_rounds=max(limits.max_rounds, record.index),
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
                    round_entry=_with_observability(record),
                    # 逐轮的**计数**（同一处算、同一条纪律）。它与上面那一份明细是两件事：
                    # 明细有上限、且只留「取不到」的条目，数不出真数（见 `_round_counts`）。
                    round_counts=_round_counts(record),
                    # 逐轮的诊断值（推理 token / 来源标记 / 三类耗时 / 请求指纹），同样
                    # 从这一个出口出去：逐轮事件账本按这一份落列（列名与模型逐字对齐）。
                    round_diagnostics=diagnostics.event_fields(record.index),
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

    # 轮次上限。**格式转换可以借用一轮**（`format_retry_granted`，见下面 markdown 那一支）：
    # 它一次上下文都不取，只是把上一条原样换成协议 JSON。轮次刚好用尽时不给这一轮，
    # 那一片的结论就整份丢掉 —— 实测就是这么丢的（一个分片 8 轮的最后一条是 markdown，
    # 它负责的三个维度一条结构化结论都没交回来）。
    format_retry_granted = False
    # 上一轮**开始时**的账（用来量「上一轮真的花了多少」，见下面那个前瞻）。`None` =
    # 还没有跑过任何一轮。
    round_start_spent: int | None = None
    for round_index in range(1, limits.max_rounds + 2):
        if round_index > limits.max_rounds and not format_retry_granted:
            # 没借到那一轮 —— 与 `for ... else`（轮次跑完都没 break）同一件事，只是现在
            # 循环多跑一次判断，所以那句兜底要在这里补上。**只在还没定过原因时才写**
            # （理由见循环末尾那段）。
            if degradation == DEGRADE_NONE:
                degradation = DEGRADE_ROUNDS
            break
        # **单次 token 硬上限：唯一一道闸，在每一轮开头判一次。**
        #
        # 放在这里（而不是塞进 `client.complete` 前或包一层异常）有三个理由：一轮里最多
        # 三次调用（主调用、压小重发、收尾补救）全在这一道之后，**每轮的保守预留**本来就
        # 覆盖了它们（`round_reserve_tokens` 的语义就是「本轮最坏情况」）；三个调用点外面
        # 各有一圈 `except Exception`，在那里抛异常会被它们咽掉、变成「传输失败」这种
        # 与钱无关的说法；而按轮判不需要用异常做控流。
        #
        # 这一停**不是失败**，是「再花就超上限了」：只要手上已经有取证（跑过轮次或取到过
        # 内容），失败出口会补一次「现在出结论」把它兑现成一份报告（`services/ai/wrap_up.py`）。
        # —— 这句话原先写的是「下面的兜底会带着已有的轮次出结论」，而当时**没有任何一处**
        # 会真的去要那份结论：它只在模型自己已经写了报告时才成立（run 3 就是这么空的）。
        # 已经有了更具体的原因（例如上一轮刚写了 markdown 报告、正等它转成 JSON）时不覆盖
        # —— 与上面「轮次上限」那一段同一条规矩。
        if single_run_budget is not None and not single_run_budget.affordable():
            if degradation == DEGRADE_NONE:
                degradation = DEGRADE_BUDGET
            break
        # 这一轮对外报的轮次上限：借来的那一轮要报成「第 N/N 轮」，不能出现「第 9/8 轮」。
        reported_max_rounds = (
            round_index if format_retry_granted else limits.max_rounds
        )
        exhausted = tools.requests_remaining <= 0
        # 这一轮的诊断状态重置（耗时与指纹都只属于这一轮，见 `RoundDiagnostics`）。
        diagnostics.begin_round()
        # 这一轮要写进 trace 的补充说明：压过历史、被上游拒过、走了收尾 —— 都是「这次分析
        # 不是正常跑完的」的证据，只留在日志里等于没说。
        round_notes: list[str] = []
        # **钱这条额度也要提前说一声**（2026-09-26 真机 run 3）。
        #
        # 上面那道闸判的是「这一轮付得起吗」—— 它只在**钱已经花掉之后**才说话，而在那之前
        # 模型收不到任何信号：轮次有 `build_final_round_hint`、索取额度有
        # `build_budget_exhausted_hint`，钱这条原先一句话都没有。run 3 因此跑了 9 轮、
        # 9 轮全在索取上下文、**一次结论都没写**，第 10 轮开头被拦下，整次运行以
        # 「没有拿到可用的结论」收场。这里把**同一道闸门向前推一轮**：按「上一轮真的花了
        # 多少」估出这一轮结束后的账，付不起下一轮就在**这一轮**把话说给模型听。
        #
        # 判据在 `SingleRunBudget.next_round_affordable`（与 `affordable()` 同一条公式，算术在
        # `auto_sizing.single_run_lookahead`）；这里只负责把「上一轮花了多少」量出来 ——
        # 两次轮首的账相减，不去回读 `RoundRecord`（那会多出一条读法）。
        token_budget_low = False
        if single_run_budget is not None:
            last_round_tokens = (
                None
                if round_start_spent is None
                else max(0, single_run_budget.spent_tokens - round_start_spent)
            )
            token_budget_low = not single_run_budget.next_round_affordable(last_round_tokens)
            round_start_spent = single_run_budget.spent_tokens
            if token_budget_low:
                # 只留在日志里等于没说：事后要能回答「模型为什么这一轮突然收尾」。
                round_notes.append("预算将尽：本轮已提示模型收尾（不再索取上下文）")
        # 必读清单进度（P1b）：**每轮都重报一次**，因为它每轮都在变（模型正在读）。
        # 走 `budget_notes` 这条路是刻意的 —— 它是**每轮都发给模型**的（任务书只发一次），
        # 而这一段随成员/轮次变化，本来就只该出现在成员私有消息里（共享前缀逐字节相同
        # 是 prompt cache 的全部依据，见 `subagent.build_seed_messages`）。
        #
        # **拼进 brief 的那一份元组，而不是往 `budget_notes` 这个列表里 append**：
        # 那个列表的生命周期是「本轮执行完之后由 `_batch_notes(batch)` 整体替换」，
        # 往里塞一条会在任何一条「本轮提前 continue」的路径上留下来，下一轮再塞一次
        # 就成了重复的一条。
        mandatory_note = tools.mandatory_note()
        brief = _RoundBrief(
            round_index=round_index,
            max_rounds=reported_max_rounds,
            change_summary=change_summary,
            baseline_digest=baseline_digest,
            correction_hint=correction_hint,
            budget_exhausted=exhausted,
            token_budget_low=token_budget_low,
            budget_notes=(
                (*budget_notes, mandatory_note) if mandatory_note else tuple(budget_notes)
            ),
            pending_items=tuple(pending_items),
            recap=recap_text,
            requests_remaining=tools.requests_remaining,
            requests_total=limits.max_tool_requests,
            limits=limits,
            task_message=task_message,
            dimension_ids=dimension_ids,
        )
        items, user_message = _prepare_round(brief, messages)
        if prefetch.items:
            # 预取写进了缓存，所以「取到了」必须真的等于「进了提示词」—— 被预算裁掉的
            # 要用 `forget_unfitted` 摘掉，否则后面的「见上文那一节」是一句假话。
            forget_unfitted(prefetch.items, items, tools)

        # **整份提示词**（系统提示词 + 变更清单 + 历史 + 本轮上下文）超出预算时压历史。
        #
        # 这一步补的是一个真窟窿：以前只有「本轮取回的上下文」受预算约束（`_fit_items`
        # 按剩余额度压条目），**历史不受任何约束** —— 每一轮都把上一轮的整条消息再发一遍，
        # 于是提示词随轮次单调增长，最后撞上模型窗口被上游拒绝，一次已经跑了几轮的分析
        # 连结论一起作废。压掉的是重复，不是信息：被压的轮次会留下一条记录（见
        # `budget.compact_history`），而且可以重新索取。
        if estimate_chars(messages) + len(user_message) > limits.prompt_char_budget:
            # 目标里**先扣掉本轮消息自己的位置**：只按总预算压历史的话，压到刚好等于预算、
            # 再把本轮消息加上去就又超了。而且条目的额度有下限（`MIN_ITEM_BUDGET`）——
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
                diagnostics.mark_compacted()
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
        try:
            result = diagnostics.model_call(
                [*messages, entry], client.complete, complete_kwargs(limits)
            )
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
            # 补救按「丢得越少越先试」的顺序来：
            #   ① 历史留着，把**本轮的条目**按更小的额度重压一遍再发；
            #   ② 历史丢掉，条目仍按紧缩后的额度重压（保住最近这一批证据文本）；
            #   ③ 都不行才用「收尾」提示词 —— 它只带变更清单开头与已取内容**目录**，
            #      正文一条不带，必然装得下，但模型只能靠记得的东西作答。
            # 每一步都**要先算出它确实压小了**才发（见下面 `original_chars` 那段）。
            #
            # 原先只有 ③，理由是「上游拒了说明水位估得不准，压历史也补不上」。那句话对
            # **历史**成立，对**条目**不成立：条目是按我们自己的额度渲染出来的，压到
            # 1/4 是我们说了算的确定量（每次最多 40 条 × 11,000 字符，是提示词里最大的
            # 一块，而且每轮都要重发一遍）。所以先把确定能压掉的那块压掉，再谈放弃。
            log_print(
                f"⚠️ AI 分析：上游拒绝了本次请求（{error_text[:300]}）。"
                "按「提示词超出上下文窗口」处理。",
                "AI",
                force=True,
            )
            shrunk_result = None
            transport_error: Exception | None = None
            # 「这一压到底有没有用」**发之前**就能算出来（`_prepare_round` 是确定性的，
            # 同一份输入必然组装出同一份消息）。这一步不能省：条目额度有个下限
            # （`MIN_ITEM_BUDGET` = 4,000），条目**已经贴着下限**时把额度再砍到 1/4 也还是
            # 4,000 字，重发出去的那一条与刚被拒的那条一样的体积 —— 实测里它甚至因为多了
            # 一行「额度被压到 4,000 字」的提示而**更长**。那不是补救，是白烧一次调用。
            # 压不出东西就直接进下一步（丢历史），三步都压不动才走收尾。
            original_chars = estimate_chars([*messages, {"role": "user", "content": user_message}])
            if items:
                for keep_history, factor in ((True, 4), (False, 4)):
                    shrunk_limits = replace(
                        limits,
                        prompt_char_budget=max(
                            MIN_ITEM_BUDGET, limits.prompt_char_budget // factor
                        ),
                    )
                    base = list(messages) if keep_history else list(messages[:1])
                    fitted, shrunk_text = _prepare_round(
                        replace(brief, limits=shrunk_limits), base
                    )
                    shrunk_entry = {"role": "user", "content": shrunk_text}
                    if estimate_chars([*base, shrunk_entry]) >= original_chars:
                        continue
                    # **标记要在「确定要发」之后才打**：`_mark_current` 会顺手把上一条上的
                    # 缓存断点摘掉，而放弃的那一次不该动它 —— 摘了却没有新消息接手，
                    # 这个可挪动的断点就凭空消失了（下一轮的缓存命中会跟着掉）。
                    shrunk_entry = _mark_current(shrunk_entry, movable_breakpoint)
                    try:
                        shrunk_result = diagnostics.model_call(
                            [*base, shrunk_entry], client.complete, complete_kwargs(limits)
                        )
                    except Exception as shrink_exc:  # noqa: BLE001
                        if looks_like_context_overflow(shrink_exc):
                            continue
                        transport_error = shrink_exc
                        break
                    # **`entry` 必须换成真发出去的那一条。** 出口处那句
                    # `messages.append(entry)` 追加的是「这一轮发给模型的消息」—— 不同步过来，
                    # 进历史的会是**刚被上游拒掉的那条超大消息**，它比发出去的这条大一倍，
                    # 于是下一轮再被拒一次、再补救一次，越补越大（实测：补救后那一轮
                    # 21,845 → 之后每轮 27,000+）。补救的意义就是别让它留在上下文里。
                    messages[:] = base
                    entry = shrunk_entry
                    # 与正常路径逐字同一条规矩（见上面 `if round_index > 1 or seed_messages`）：
                    # 不记下来的话，断点会一轮一轮往上堆 —— 上一轮那条没被摘掉，下一轮又加
                    # 一个，超过 `MAX_CACHE_BREAKPOINTS` 之后被**静默丢掉**的恰好是最新的那条。
                    # 这条路径只可能在 `round_index >= 2` 上走（第 1 轮没有条目可压），
                    # 所以那个条件在这里恒真。
                    movable_breakpoint = shrunk_entry
                    user_message = shrunk_text
                    items = fitted
                    context_overflow_recovered = True
                    overflow_recovered = True
                    diagnostics.mark_compacted()
                    # **把这次观测到的上限记下来，供本次运行剩下的轮次用。**
                    # 上游刚说了「这么大装不下」，而那份提示词正是 `original_chars` 这么大
                    # —— 这是本次运行里唯一一个**实测**的上限（水位那些数是估出来的，估错
                    # 了才会走到这里）。不记的话，下一轮照原样组装、再被拒一次、再补救一次。
                    # 落成 3/4 是给「字符 → token 的换算误差」留的余量。
                    #
                    # 不需要另加机制：`compact_history` 与 `_fit_items` 都按
                    # `limits.prompt_char_budget` 干活，调小它，下一轮自己就会压。
                    limits = replace(
                        limits,
                        prompt_char_budget=max(MIN_ITEM_BUDGET, int(original_chars * 3 / 4)),
                    )
                    round_notes.append(
                        f"上游以「上下文超长」拒绝了这次请求（累计已用 {len(rounds)} 轮、"
                        f"{tools.requests_seen} 次索取），已把上下文压到 1/{factor}"
                        f"{'（历史保留）' if keep_history else '（历史丢弃）'}后重发；"
                        f"本次运行后续轮次的提示词预算按这次实测的上限收到 "
                        f"{limits.prompt_char_budget:,} 字。"
                    )
                    break
            if transport_error is not None:
                # 重试过程中撞上的是**别的**故障（502 之类），不是「装不下」：如实报，
                # 别拿它去触发收尾提示词 —— 那会把一次传输故障记成「窗口不够」。
                _emit(RoundRecord(
                    round_index, "transport_error",
                    note=f"压小上下文重发时失败（{type(transport_error).__name__}: "
                         f"{transport_error}）"[:400],
                ))
                return EngineOutcome(
                    status=STATUS_FAILED,
                    rounds=tuple(rounds),
                    dropped=tuple(dropped),
                    requests_used=tools.requests_seen,
                    cache_hits=tools.cache_hits,
                    **_totals(),
                    error_message=f"调用模型失败（{type(transport_error).__name__}）：{transport_error}",
                    **_usage_fields(),
                )
            if shrunk_result is not None:
                result = shrunk_result
            else:
                # 连压到 1/4 都装不下（或这一轮本来就没有条目可压）→ 收尾提示词。
                # 它是千字符级的，任何窗口都装得下。
                # 上游**确实**以超长拒过 —— 这件事本身就要记下来：它意味着这次的结论是在
                # 一份被大幅裁剪的提示词上得到的。彻底失败的那条路径读的是 `error_message`。
                context_overflow_recovered = True
                overflow_recovered = True
                # 同上面那条：把实测到的上限记进本次运行剩下的轮次（收尾之后模型还可能
                # 再要一轮上下文，那一轮不该再按原来那个已经被拒过的预算组装）。
                limits = replace(
                    limits,
                    prompt_char_budget=max(MIN_ITEM_BUDGET, int(original_chars * 3 / 4)),
                )
                messages[:] = messages[:1]
                items = ()
                user_message = _salvage_user_message(change_summary, seen_items)
                diagnostics.mark_compacted()
                entry = _mark_current({"role": "user", "content": user_message}, movable_breakpoint)
                movable_breakpoint = entry
                round_notes.append(
                    "上游以「上下文超长」拒绝了这次请求（累计已用 "
                    f"{len(rounds)} 轮、{tools.requests_seen} 次索取），已改用「收尾」提示词"
                    "（只带变更清单开头与已取内容目录）；本次运行后续轮次的提示词预算按"
                    f"这次实测的上限收到 {limits.prompt_char_budget:,} 字。"
                )
                try:
                    result = diagnostics.model_call(
                        [*messages, entry], client.complete, complete_kwargs(limits)
                    )
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
        _note_usage(usage)
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
        # 合成器在 `round_observability`（**兜底那一次收尾调用也走它** —— 它同样是
        # 平台花掉的一笔，见 `wrap_up`）。
        round_extra = round_usage_fields(
            usage,
            prompt_chars=len(user_message),
            context_chars=round_context_chars,
            duration_ms=int((time.monotonic() - round_started) * 1000),
            text=text,
        )
        if single_run_budget is not None:
            # 单次上限的账也在这里扣（**按次**；上面那道闸是**按轮**判的，因为一轮里可能
            # 有两次调用，第二次用的是压小后的提示词）。上游没报用量时的口径、以及
            # 「按单价折算成等效 token」那件事都在 `model_call.tokens_for_budget`。
            spent, estimated = tokens_for_budget(
                usage, len(user_message), estimate_chars(messages), text, limits,
                weights=single_run_budget.weights,
            )
            single_run_budget.note_usage(spent, estimated=estimated)
        messages.append(entry)
        messages.append({"role": "assistant", "content": text})

        # **上游说「你写超了」，这句话比我们自己猜形状准。**
        #
        # 判据放在 `parse_payload` **之前**，因为这一支要处理的正是「解析得通、内容残缺」
        # 那一种：断点恰好落在一个仍然合法的 JSON 上（`requests` 数组已闭合、后面的字段
        # 整段没写）时，`json.loads` 成功、括号也配平 —— 只看形状的话，这份**半句话**会
        # 被当成正常结果收下，而它是残缺的（该要的上下文没要全、该写的字段一个没写）。
        # 原先 `finish_reason` 只被**存储**（→ trace），从来没有人读它。
        #
        # 只在「还有下一轮可借」时才花这次纠正（`round_index < limits.max_rounds`）：
        # 最后一轮要求它重发是白花一次纠正额度 —— 那句话永远发不出去，而这份额度本该留给
        # 别处。额度用完/没有下一轮时下面的解析照常进行（**一份能解析的结论不许被丢掉**：
        # 收下它并让 `output_budget_hit` 如实记着这件事，比丢掉它强得多）。
        if (
            output_limit_hit(usage)
            and limits.max_corrections > 0
            and round_index < limits.max_rounds
        ):
            limits = replace(limits, max_corrections=limits.max_corrections - 1)
            correction_hint = TRUNCATED_OUTPUT_HINT
            salvaged_report = salvage_report_markdown(text)
            if salvaged_report is not None:
                # 与下面那一支同一套兜底：重问再失败时至少还有一份正文，而不是 JSON 源码。
                markdown_fallback = salvaged_report
            _emit(RoundRecord(
                round_index, "unparsable",
                correction_hint=correction_hint,
                correction_reason=CORRECTION_TRUNCATED_OUTPUT,
                note=_combine_notes(
                    round_notes,
                    "上游报告输出撞上单次输出上限（finish_reason=length），"
                    "已要求压短后重发（不再按括号配平判断）",
                ),
                **round_extra,
            ))
            pending_items = ()
            budget_notes = []
            round_memos.append(TurnMemo(index=round_index, status="unparsable"))
            continue

        try:
            parsed = parse_payload(
                text,
                dimension_ids=dimension_ids,
                baseline_fingerprints=baseline_fingerprints,
            )
        except ProtocolError as exc:
            # 先看是不是**被截断的 JSON**（单次输出有上限，汇总那一步最容易撞上）。
            # 这个判断必须排在 `looks_like_markdown_report` **之前**：JSON 字符串里的
            # `\n# 变更理解` 同样能数到章节标题，晚一步就会把 JSON 原文当成正文存下来
            # （实测 run 7：55k 字的报告被包在 `{"status": "final", …` 里面）。
            salvaged_report = salvage_report_markdown(text)
            # **括号配平降为兜底**（E4）：上面那一支已经在 `finish_reason` 说明「写超了」
            # 时先走过一遍，这里供端点上**不报** `finish_reason` 的情形用。
            #
            # **只看形状**（括号配平），不要求抢得到正文：run 8 有一轮是在**要上下文的
            # 半截**被切掉的，那种回答里没有 `report_markdown`，但它同样是「写太长了」，
            # 给的提示也该是压短，而不是改格式。
            truncated = looks_like_truncated_json(text)
            if truncated and limits.max_corrections > 0:
                # 它不是「不肯说 JSON」，是**写太长了**。重问一次并要求压短，比直接降级
                # 强：降级只留下正文，结构化结论（人工跟进清单）会整份丢掉。
                limits = replace(limits, max_corrections=limits.max_corrections - 1)
                correction_hint = TRUNCATED_OUTPUT_HINT
                if salvaged_report is not None:
                    # 抢到了正文就留着当兜底：重问再失败时至少还有一份正文，而不是 JSON 源码。
                    markdown_fallback = salvaged_report
                _emit(RoundRecord(
                    round_index, "unparsable",
                    correction_hint=correction_hint,
                    correction_reason=CORRECTION_TRUNCATED_OUTPUT,
                    note=_combine_notes(round_notes, "输出被截断，已要求压短后重发"),
                    **round_extra,
                ))
                pending_items = ()
                budget_notes = []
                round_memos.append(TurnMemo(index=round_index, status="unparsable"))
                continue
            if truncated and salvaged_report is not None:
                # 纠正额度也用完了：正文抢得出来，结构化那一半确实没了 —— 留下正文。
                _emit(RoundRecord(
                    round_index, "unparsable",
                    note=_combine_notes(round_notes, "输出被截断，已抢出正文降级"), **round_extra,
                ))
                payload = None
                markdown_fallback = salvaged_report
                degradation = DEGRADE_MARKDOWN
                break
            # **先试平台自己修**（实测 run 38/40/41 的三个形态）：模型把两万 token 的
            # report_markdown 写坏，但括号配平、`finish_reason=stop` —— 三道判据全数绕过，
            # 整轮被当成 markdown 报告重发。重发的代价不只是 token：按「原样转成 JSON」
            # 交回的正文**普遍更短**（run 38：16.5k→6.7k），内容先丢了一截。而
            # `anomalies`/`dimensions` 写在正文之后、本来就是好的 —— 修好正文就地解析，
            # 不重发也不降级。两种病、同一副骨架：
            #  * 续写块：正文被切成 `"第一段","第二段"`（run 38 的 S2 第 7 轮）；
            #  * 裸引号：正文原样引用了 `require("…")` 这类带双引号的代码、没转义
            #    （run 41 的汇总第 4 轮，trace 存了完整原文确诊）。
            # 都修不好就按原分支处理 —— 这条修复不堵任何原有的路，失败也留痕。
            #
            # 第三项（2026-09-23）：**输出通道错位**（模型把取数请求写成工具调用信封，
            # trace 里 run 38/44/45 共 5 轮实测）。它同样是「有请求可救、救回来就不用
            # 重发」，所以挂在同一个循环里；抽不到就返回 None，落到下面通道错位那一支。
            # 三条 note 各自一份：note 是给读 trace 的人看的，「把 report_markdown 写坏」
            # 对信封那种病是一句假话（正文没坏）。
            repaired_parsed = None
            repair_note = ""
            for repair_name, repair_text, repaired_note in (
                (
                    "续写块拼接",
                    repair_split_string_payload(text),
                    "模型把 report_markdown 写坏（续写块拼接），平台已自动修复，未重发",
                ),
                (
                    "正文裸引号转义",
                    repair_unescaped_quotes_payload(text),
                    "模型把 report_markdown 写坏（正文裸引号转义），平台已自动修复，未重发",
                ),
                (
                    "工具调用信封抽取",
                    repair_dsml_tool_calls_payload(text),
                    "模型把取数请求写成了工具调用信封（工具调用信封抽取），"
                    "已按平台取数类型抽出请求并执行，未重发",
                ),
            ):
                if repair_text is None:
                    continue
                try:
                    repaired_parsed = parse_payload(
                        repair_text,
                        dimension_ids=dimension_ids,
                        baseline_fingerprints=baseline_fingerprints,
                    )
                except ProtocolError as exc:
                    # **失败也要留痕**：「识别出这种病、但修完仍解析失败」与「根本不是
                    # 这种病」在 trace 上必须分得开 —— 不留这句话，下一轮实测又要靠猜。
                    round_notes.append(
                        f"已尝试{repair_name}，但整份 payload 仍解析失败（"
                        + str(exc)[:120]
                        + "），按原分支处理"
                    )
                    repaired_parsed = None
                    continue
                repair_note = repaired_note
                break
            if repaired_parsed is not None:
                parsed = repaired_parsed
                round_notes.append(repair_note)
                # **不 continue、不 break、也不再发这一轮的记录**：except 块到此结束，
                # 落回循环体尾部的常规处理（与「这一轮本来就解析成功」同一条路），
                # 由那里统一发这一轮的 `final`/`requests` 记录 —— 同一轮发两份会撞
                # `uq_ai_trace_run_round`。
            elif looks_like_markdown_report(text):
                # 模型给了一份像样的 markdown 报告。**先留下来当降级产出**：内容通常是有用的，
                # 用户至少能读到。然后才决定要不要再问一次结构。
                payload = None
                markdown_fallback = text.strip()
                degradation = DEGRADE_MARKDOWN
                if limits.max_corrections > 0 and not markdown_retried:
                    # 还有纠正额度、且没为 markdown 重问过 → 问一次：**只要求把上一条
                    # 原样转成 JSON**。
                    #
                    # 原先这里是不看额度直接 `break` 的 —— 哪怕还剩三轮，结构化结论也整份
                    # 放弃。实测 run 10 的 S3 就是这么丢掉 code_logic / version_branch /
                    # process 三个维度的：它跑满 8 轮后写了一段 markdown，平台留下了正文、
                    # 结构没了，报告里只能按「跑完了但结论没有按协议交回」把它标出来。
                    #
                    # 重问的代价是**一轮**，而且**不会丢东西**：正文已经存在 `markdown_fallback`
                    # 里，重问失败也只是回到今天这个结局。转格式比重新写一份报告容易得多 ——
                    # 内容已经在对话里，模型只需换一种包装。
                    if round_index >= limits.max_rounds:
                        # **轮次正好用尽**：借一轮给这次格式转换（只借一次，见循环头部）。
                        # 不借的话，下面的 `continue` 就等于「循环到此结束」—— 那句重问
                        # 永远发不出去。而这一支（最后一条消息写成 markdown）恰恰最常见：
                        # 模型被明确告知「这是最后一条消息」之后，最容易改写成叙述。
                        # 实测 run 11 的两个分片都是这样：一个余一轮、一个正好到顶。
                        format_retry_granted = True
                    markdown_retried = True
                    correction_hint = build_markdown_reemit_hint()
                    _emit(RoundRecord(
                        round_index, "unparsable",
                        correction_hint=correction_hint,
                        correction_reason=CORRECTION_UNPARSABLE,
                        note=_combine_notes(round_notes, "按 markdown 报告降级，已要求原样转成 JSON"),
                        **round_extra,
                    ))
                    pending_items = ()
                    budget_notes = []
                    round_memos.append(TurnMemo(index=round_index, status="unparsable"))
                    continue
                _emit(RoundRecord(
                    round_index, "unparsable",
                    note=_combine_notes(round_notes, "按 markdown 报告降级"), **round_extra,
                ))
                break
            elif looks_like_tool_call_envelope(text):
                # **输出通道错位**：模型把取数请求写成工具调用信封，而不是协议 JSON。
                # 能就地抽出来的走不到这里（上面第三项修复已接管），到这里的是抽不出请求
                # 的信封（trace 实测：整段只有一个思考块）。排在 markdown 判据**之后**
                # （不动既有两个分支的先后）、排在 `max_corrections` 那两支**之前** ——
                # 这一种病不从那本账里扣（理由见 `EngineLimits.max_channel_corrections`）。
                if limits.max_channel_corrections > 0:
                    limits = replace(
                        limits, max_channel_corrections=limits.max_channel_corrections - 1
                    )
                    correction_hint = build_channel_mismatch_hint()
                    _emit(RoundRecord(
                        round_index, "unparsable",
                        # 留痕分三档：修复（上面那一支的 note）/ 纠正（这一句）/
                        # 失败（下面那一支的 note）。
                        correction_hint=correction_hint,
                        correction_reason=CORRECTION_CHANNEL_MISMATCH,
                        note=_combine_notes(round_notes, "输出通道错位（工具调用信封），已重问一次"),
                        **round_extra,
                    ))
                    pending_items = ()
                    budget_notes = []
                    round_memos.append(TurnMemo(index=round_index, status="unparsable"))
                    continue
                _emit(RoundRecord(
                    round_index, "unparsable",
                    note=_combine_notes(
                        round_notes, "输出通道错位（工具调用信封），独立纠正额度已用完"
                    ),
                    **round_extra,
                ))
                degradation = DEGRADE_PROTOCOL
                break
            elif limits.max_corrections <= 0:
                _emit(RoundRecord(
                    round_index, "unparsable",
                    note=_combine_notes(round_notes, str(exc)[:200]), **round_extra,
                ))
                degradation = DEGRADE_PROTOCOL
                break
            else:
                # 重问也要占一轮：否则一个不肯说 JSON 的模型能把循环变成无限次重试。
                limits = replace(limits, max_corrections=limits.max_corrections - 1)
                correction_hint = build_correction_hint(exc, dimension_ids=dimension_ids)
                _emit(RoundRecord(
                    round_index, "unparsable",
                    # 那一轮为什么被重问：`note` 里是协议错误本身（给人看），
                    # `correction_hint` 是随后发给模型的那段纠正提示（原样记下来）。
                    correction_hint=correction_hint,
                    correction_reason=CORRECTION_UNPARSABLE,
                    note=_combine_notes(round_notes, str(exc)[:200]), **round_extra,
                ))
                pending_items = ()
                budget_notes = []
                round_memos.append(TurnMemo(index=round_index, status="unparsable"))
                continue

        correction_hint = ""
        if parsed.is_final:
            # **历史清单没有逐条交代时，当场要一次。**
            #
            # 「模型不填这个字段」这个判断是**错的**（2026-09-25 晚订正）：当时拿的是
            # run 75 / 76 的 `baseline_updates` 是 `[]`，而那个 `[]` 由平台自己造 ——
            # `ground_payload` 漏带了 `baseline_closures`，写好的也被扔掉。反证在下面
            # 这一支本身：清单里 5 / 6 条一条状态都没有时它会补问，而 run 77 / 78
            # **一次都没补问**，只可能是收下了。
            #
            # 那这一支还要不要？要 —— 理由不依赖上面那个误判：这条规矩必须**平台说了算**，
            # 而不是「模型多半会写」。`dimensions` 一次没漏过，正因为它必填、缺了会被打回。
            #
            # 与上面三种纠正不同的是：这一份结论**解析成功、照收不误**（缺状态不毁结论）。
            # 所以只在「还有重问额度」且「还有下一轮」时才花这一次 —— 最后一轮要求它重发是
            # 白花（那句话永远发不出去），那份额度本该留给别处。
            # **这一轮的结局是 `final`，不是 `unparsable`**：那份 JSON 解析成功了，模型
            # 确实给出了一份结论 —— 只是清单没交代完，平台把它退回去补一块。写成
            # `unparsable` 会让运行轨迹里出现「输出无法解析」（`budget._ROUND_STATUS_LABELS`）
            # 与 `parsed_ok=False`，而那两句都是假的。它被退回去这件事由 `correction_reason`
            # 与 `correction_hint` 记着，不必借一个不成立的状态值来说。
            missing_statuses = missing_baseline_statuses(parsed)
            if (
                missing_statuses
                and limits.max_corrections > 0
                and round_index < limits.max_rounds
            ):
                limits = replace(limits, max_corrections=limits.max_corrections - 1)
                correction_hint = build_baseline_coverage_hint(missing_statuses)
                _emit(RoundRecord(
                    round_index, "final",
                    item_count=len(items),
                    correction_hint=correction_hint,
                    correction_reason=CORRECTION_BASELINE_COVERAGE,
                    note=_combine_notes(
                        round_notes,
                        f"结论已解析，但历史清单里有 {len(missing_statuses)} 条没有给出状态，"
                        "已要求补齐（结论照收，不因此作废）",
                    ),
                    **round_extra,
                ))
                pending_items = ()
                budget_notes = []
                round_memos.append(TurnMemo(index=round_index, status="final"))
                continue
            payload = parsed
            if context_overflow_recovered:
                # 这份结论是在**被裁剪过的提示词**上得出的，与正常跑完不是一回事。
                # 判据是**整次运行**的，不是当前这一轮（见 `context_overflow_recovered`）。
                degradation = DEGRADE_CONTEXT
            elif degradation == DEGRADE_MARKDOWN:
                # 前一轮写成了 markdown、这一轮按要求原样转成了 JSON —— **结构化结论到手了**。
                # 不抹掉这个原因的话，`family_ledger` 会按它写一句「跑完了但结论没有按协议
                # 交回」，而那正是它这一轮刚刚交回来的东西（一句假话）。
                degradation = DEGRADE_NONE
            _emit(RoundRecord(
                round_index, "final",
                item_count=len(items), note=_combine_notes(round_notes),
                # 这一轮报了几条候选（给逐轮事件账本与「每个成员报了几条」那一栏）。
                candidates=len(parsed.anomalies),
                **round_extra,
            ))
            round_memos.append(TurnMemo(index=round_index, status="final", items=tuple(items)))
            break

        # `sanitize_requests` 同时返回「通过白名单的」与「被丢掉的及原因」——两样都要：
        # 前者去执行，后者进 trace（否则「为什么这次少看了一个文件」无从追溯）。
        #
        # `repo_paths` 是**本次冻结 tip 上 Git 跟踪文件的路径集合**（P1）：有它时
        # `file_content` 的判据从「本批次改动过的文件」放宽到「这一版里存在的文件」，
        # 于是「改公共接口、读调用方」能在同一轮完成；没有它时判据与从前逐字相同。
        # 集合从 `provider_repo_paths` 来：**不猜**，拿不到就给 `None`。
        dropped_before = len(dropped)
        requests, request_dropped = sanitize_requests(
            parsed.requests, scope, repo_paths=repo_paths
        )
        dropped.extend(parsed.dropped)
        dropped.extend(request_dropped)
        # P5：这一轮的工具重放字符 = 执行前后「跨成员重放」累计值的差。
        # 用**差**而不是累计值：累计值在第 3 轮会把第 1 轮的重放再算一遍，而
        # 「这一轮为什么贵」问的正是本轮那一段。记账本身在 `context_tools`（唯一口径）。
        replay_before = _replay_chars(tools)
        # 本地取数耗时（含 provider 内部建索引的那一段，见 `RoundDiagnostics`）。
        batch = diagnostics.tool_fetch(tools.execute, requests)
        replay_chars = max(0, _replay_chars(tools) - replay_before)
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
                # P5：这一轮交给模型的工具正文里有多少是跨成员重放的（子代理模式下
                # 「多角色串行」的主要成本之一），以及**这一轮的交付为什么被截断**。
                tool_replay_chars=replay_chars,
                truncation_reason=_truncation_reason(batch),
                **round_extra,
            )
        )
        # 这一轮索取了什么要留下来：压历史时它就是被压掉的那几轮的「记录」（见
        # budget.compact_history）。不留的话，压完历史模型就只知道自己拿过东西、
        # 不知道拿过什么，于是重新要一遍 —— 额度花两遍，还是没看到内容。
        round_memos.append(TurnMemo(index=round_index, status="requests", items=tuple(batch.items)))
    else:
        # `for` 的 else：轮次跑完都没 `break` —— 也就是「一直没交结论」。
        #
        # **只在还没定过原因时才兜底**：模型的 markdown 那一条路（`DEGRADE_MARKDOWN`）
        # 现在会 `continue`（重问一次格式），于是最后一轮走完之后会落到这里 ——
        # 无条件覆盖的话，「模型写了报告但没按协议交」会被改写成「轮次用尽」，
        # 而这两句话对应的处置完全不同（前者要人工去读那份正文，后者是「没看完」）。
        if degradation == DEGRADE_NONE:
            degradation = DEGRADE_ROUNDS

    # 这里**不需要**再判断一次「最后一轮是不是一份报告」：每一轮解析失败时都已经查过
    # `looks_like_markdown_report`（最后一轮也不例外），像报告的在循环里就留下了。
    # 曾经在这里又写了一遍，是一段永远走不到的死代码 —— 它让「降级路径」看起来有两条，
    # 读代码的人会以为少了一条覆盖。
    if payload is None and not markdown_fallback:
        # 既没拿到 final，也没有可用的 markdown。
        if degradation == DEGRADE_NONE:
            degradation = DEGRADE_ROUNDS
        # **认输之前补一次「现在出结论」**（2026-09-26 项目 1 run 3 的直接教训）。
        #
        # 三种额度用尽时循环在轮首就停了，而原先这里**直接**返回失败 —— 中间一次模型
        # 调用都没有：模型手上那几轮取证全部作废，`report_reserve_tokens` 那笔报告预留
        # 也从来没被花过。什么时候发、为什么带完整对话、为什么只发一次，全在
        # `services/ai/wrap_up.py` 的模块 docstring 里。
        wrap_error = ""
        if should_request_final_answer(
            degradation=degradation, rounds=rounds, seen_items=seen_items
        ):
            wrap_started = time.monotonic()
            entry = _mark_current(build_wrap_up_entry(), movable_breakpoint)
            wrap_extra: dict[str, Any] = {}
            try:
                result = diagnostics.model_call(
                    [*messages, entry], client.complete, complete_kwargs(limits)
                )
            except Exception as exc:  # noqa: BLE001 —— 兜底那次没发成不改结论本身
                wrap_error = f"{type(exc).__name__}: {exc}"
            else:
                usage = _usage_of(result)
                text = usage["text"]
                _note_usage(usage)
                if single_run_budget is not None:
                    # 这一笔照样按次扣账：不记的话平台自己报的数就是错的（见 wrap_up）。
                    # 于是 `headroom` 可能微负 —— 分界写在 `budget_gate` 的 docstring 里：
                    # **闸门管探索调用，交付那一次在它的定义域之外**。
                    spent, estimated = tokens_for_budget(
                        usage, len(entry["content"]), estimate_chars(messages), text,
                        limits, weights=single_run_budget.weights,
                    )
                    single_run_budget.note_usage(spent, estimated=estimated)
                # 那一次调用与它的回答也进对话：它是这次运行的**最后一条**消息，
                # 事后要能在 trace 里读到它是怎么被要求的（正文见 `round_hints`）。
                messages.append(entry)
                messages.append({"role": "assistant", "content": text})
                wrap_extra = round_usage_fields(
                    usage,
                    prompt_chars=len(entry["content"]),
                    context_chars=0,  # 这一次没有带任何上下文条目
                    duration_ms=int((time.monotonic() - wrap_started) * 1000),
                    text=text,
                )
                try:
                    wrap_payload = parse_payload(
                        text,
                        dimension_ids=dimension_ids,
                        baseline_fingerprints=baseline_fingerprints,
                    )
                except ProtocolError:
                    wrap_payload = None
                # **只认 `final`**：模型若又回一句「我还想看 X」，那是它没听明白 ——
                # 收下它等于把一份一条结论都没有的空壳当成报告交出去，比失败更糟
                # （用户会看到一份很干净的报告，而里面没有任何结论）。
                if wrap_payload is not None and wrap_payload.is_final:
                    payload = wrap_payload
                elif looks_like_markdown_report(text):
                    # 正文也是交付：与循环里那条路同一条口径（留下正文，这一轮按
                    # `unparsable` 记）。`degradation` 仍是闸门写的那一个 —— 不覆盖。
                    markdown_fallback = text.strip()
                else:
                    wrap_error = "模型没有按协议交回 JSON"
            # 轮序号按**已有的条数 + 1** 取，不许照抄循环变量：它在「借一轮做格式
            # 转换」那条路上会走到 `max_rounds + 1`，照抄就会与上一条撞号 ——
            # `ai_analysis_trace` 上有 `uq_ai_trace_run_round`（run_id + 轮序号）。
            _emit(RoundRecord(
                len(rounds) + 1,
                "final" if payload is not None else "unparsable",
                note=(
                    "额度用尽后补发了一次「现在出结论」，拿到了结论"
                    if not wrap_error
                    else f"额度用尽后补发了一次「现在出结论」，仍未拿到结论（{wrap_error}）"
                ),
                **wrap_extra,
            ))
        if payload is None and not markdown_fallback:
            # 兜底也试过了（或压根没资格发）→ 按原来的失败收，但说清兜底做了什么。
            suffix = (
                f"；已补发一次收尾调用，仍未拿到结论（{wrap_error}）" if wrap_error else ""
            )
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
                    f"{suffix}"
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
    # 「钱只够这一轮了」——`SingleRunBudget.next_round_affordable` 的估算结果（见 `round_hints
    # .build_token_budget_final_hint`）。与 `budget_exhausted`（索取**次数**额度）是两条
    # 不同的额度：那一条是硬事实，这一条是估算，所以 `prompt.build_user_message` 里把它
    # 排在最后 —— 硬事实成立时它不说话。
    #
    # 带默认值所以放在这一组（dataclass 的非默认字段必须排在前面）。
    token_budget_low: bool = False



def _with_observability(record: RoundRecord) -> dict | None:
    """把 P5 的逐轮观测字段**并进**这一轮的明细（实现与理由见 `round_observability`）。

    薄钩子留在这里只为一件事：`_live_round_entry` 的兜底在这一层（明细算不出来就返回
    None，**不编一份只有 P5 字段的半份明细** —— 那会让读的人以为其余明细本来就是空的）。
    """
    return _merge_observability(record, _live_round_entry(record))




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
