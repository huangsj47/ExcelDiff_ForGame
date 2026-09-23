# -*- coding: utf-8 -*-
"""逐请求指纹与分叉原因：**回答「这次请求与上一次在哪个消息开始分叉」**。

## 平台缺的是哪一块数据

逐轮用量与时长早就有，但那两类数答不出这个问题：一次缓存未命中，既可能是**平台自己
重写了前缀**（那要修），也可能是**必要的压缩 / 提示词或知识包更新 / 服务端缓存过期**
（那不该修，改了反而损害报告质量）。两种情形在「命中率掉了」这个观测值上长得一模一样。

所以这一层在**每次模型调用之前**，对真正要发出去的那一份消息序列算出四个诊断值：
整请求哈希、稳定前缀哈希、与上一个请求的最长公共前缀（消息条数 + 字符数）、以及
分叉原因（`initial / append / compaction / prompt_change / snapshot_change / other`）。

## 口径四条（一条都不许省）

1. **哈希是本机诊断指标，不是命中率。** 提供商按 token / 自己的内部单元匹配缓存，我们
   比的是本机对消息序列算出的哈希：**哈希相同不保证缓存命中**（服务端可能已过期、可能
   按不同粒度切分），**哈希不同也不保证未命中**（我们这一侧多一个不参与匹配的字段就会
   让哈希变掉）。所以它在界面上与文档里的名字是「分叉原因」，**不许**被当成命中率用。
2. **绝不保存 API key，也不为这个诊断再落一份提示词。** 只存哈希与计数（见
   `RequestFingerprint.to_dict` 的字段清单）；消息正文一个字节都不进这个模块的输出。
   端点标识取 `llm_client.normalize_base_url` 归一后的地址 —— 它已经去掉了 URL 里的
   凭据。
3. **`None` 是「没有可比的上一个请求 / 上游没报」，不是 0。** `prefix_common_*` 在第一
   次调用上是 `None`（没有上一个请求），不是「公共前缀 0 条」——后者是一个观测值。
4. **压缩不是 bug，这一层只负责如实归因。** `compaction` 说明「这次请求的开头被压过
   历史改写」；真正要修的是「稳定前缀变了、而提示词版本与快照都没变」那一种
   （它落在 `other` 里，见 `divergence_reason`）。

## 与「上一个请求」比的是哪一次：**同一次运行、同一个成员内的上一次调用**

* 成员之间不比：分片之间本来就该分叉（任务书不同），拿它们互比只会得到满屏的
  `other`；
* **跨运行也不比**：那需要把上一次的完整消息留在什么地方，而口径 2 明确不许我们
  再落一份提示词。跨运行的那一半由 `stable_prefix_fingerprint` 承担 —— 它只覆盖
  「系统提示词 + 首轮 user（含同快照的变更清单）」那一段，两行记录哈希相同就说明
  那一段逐字节相同，可以在两个运行之间直接比（这正是「跨运行复用缓存」的依据）。
* 所以判定「平台自己重写了前缀」时必须连着看两件事：本行的分叉原因，以及本行与前一行
  的稳定前缀哈希是否相等。`snapshot_change` / `prompt_change` 是**允许的自然失效**。

## 为什么是一个独立模块

`services/ai/engine.py` 贴着 2000 行的 ERROR 闸门（`scripts/check_file_length.py`）。
这一层是纯函数 + 一个保存「上一个请求」的小对象，搬出来之后引擎那边只剩接线
（一个 `_call_model` 包装），而算法与理由都在这里能被单独读、单独测。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from services.ai.prompt import prompt_version
from services.ai.prompt_cache import strip_cache_breakpoints

# ---------------------------------------------------------------------------
#  分叉原因（判据的取值域是一份白名单）
# ---------------------------------------------------------------------------

# 这次调用没有可比的上一个请求（一次运行的第一次调用、进程重启后的第一次）。
DIVERGENCE_INITIAL = "initial"
# 这次的消息是上一次的**严格延长**（消息序列 append-only，缓存的理想形态）。
DIVERGENCE_APPEND = "append"
# 平台自己把历史压掉了（`budget.compact_history`，或上游拒绝后的压小重发 / 收尾）。
DIVERGENCE_COMPACTION = "compaction"
# 稳定前缀变了，且提示词版本变了 —— 换了提示词，属于**允许的自然失效**。
DIVERGENCE_PROMPT_CHANGE = "prompt_change"
# 稳定前缀变了，且快照变了 —— 换了快照，同样允许（复用旧前缀反而是错的）。
DIVERGENCE_SNAPSHOT_CHANGE = "snapshot_change"
# 以上都不是：稳定前缀变了而两把版本号都没变、消息变少了却没压过历史……
# **这一类才是要去看代码的那一类**（平台可能在别处重写了前缀）。
DIVERGENCE_OTHER = "other"

DIVERGENCE_REASONS = (
    DIVERGENCE_INITIAL,
    DIVERGENCE_APPEND,
    DIVERGENCE_COMPACTION,
    DIVERGENCE_PROMPT_CHANGE,
    DIVERGENCE_SNAPSHOT_CHANGE,
    DIVERGENCE_OTHER,
)

# 稳定前缀 = 请求最前面的几条消息。今天的形状在两条路上都是同一条：
#
# * 单代理：`[system, 第一轮 user（变更清单 + 基线 + 首轮指引）]`；
# * 子代理：`seed_messages` 就是这份共享前缀（见 `subagent.build_seed_messages`），
#   任务书排在第 3 条 —— 它按成员各不相同，**不属于**稳定前缀。
#
# 所以固定取 2：第一条是系统提示词（平台 skill + 项目知识），第二条含整份变更清单。
# 这个数与 `budget.compact_history` 的 `protect_head` 是同一件事的两面（那一边钉住
# 「不许压掉」的前 k 条），改这里之前先看那一边。
STABLE_PREFIX_MESSAGES = 2

# 哈希截到 16 个十六进制字符：它的用途是**本机对照**（两行相等 / 不等），不是密码学。
# 截短只为让它在库里、日志里、界面上都短到能一眼看完。
DIGEST_CHARS = 16


# ---------------------------------------------------------------------------
#  规范化与哈希（纯函数）
# ---------------------------------------------------------------------------


def _content_of(message: Any) -> str:
    """一条消息的正文（非字符串一律折成空串：诊断层不做内容判断，也不许抛）。"""
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def canonical_message(message: Any) -> str:
    """一条消息的规范编码（**固定 JSON 规则**：键排序、不转义非 ASCII、无多余空格）。

    `sort_keys=True` 是硬要求：字典的插入顺序在不同构造路径上不一样（同一条消息由
    `_mark_current` 拿到的副本可能与原件键序不同），按插入顺序编码会让**内容一模一样
    的两条消息算出两个哈希** —— 那正好是这个模块要诊断的那类假信号。

    非 JSON 可编码的值用 `default=str` 兜住：诊断层绝不能因为某个字段是对象就抛出去
    （调用点两侧都是付费的模型调用）。
    """
    if not isinstance(message, Mapping):
        return json.dumps(str(message), ensure_ascii=False)
    payload = {str(key): value for key, value in message.items()}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def digest(text: str) -> str:
    """一段文本的短哈希（见 `DIGEST_CHARS`）。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def request_digest(messages: Iterable[Any]) -> str:
    """整请求哈希：按 `canonical_message` 逐条编码后用换行拼起来再哈希。

    **在去掉内部缓存断点之后算**（调用方传进来的就该是那一份，见
    `RequestFingerprinter.before_call`）：断点标记是平台自己的调度信息，
    «挪一个断点» 不该改变这个哈希 —— 那正是「内部断点变化但实际请求不变」这条性质的
    落点（有测试钉着）。
    """
    return digest("\n".join(canonical_message(item) for item in messages))


def stable_prefix_digest(
    messages: Sequence[Any], *, stable_messages: int = STABLE_PREFIX_MESSAGES
) -> str:
    """稳定前缀哈希：请求最前面 `stable_messages` 条消息（见 `STABLE_PREFIX_MESSAGES`）。

    条数不足时**按实际有的算**（空请求也能算出哈希）：这里不编「缺失的前缀」，
    否则两个都缺前缀的请求会显示成「前缀相同」，而它们其实是同一件「什么都没有」的事。
    """
    count = max(0, int(stable_messages))
    return digest("\n".join(canonical_message(item) for item in list(messages)[:count]))


def common_prefix(
    previous: Sequence[Any], current: Sequence[Any]
) -> tuple[int, int]:
    """与上一个请求的最长公共前缀：`(消息条数, 这些消息的字符数)`。

    逐条按规范编码比：**只有整条逐字节相同才继续**（前缀匹配的实际语义就是逐字节），
    在某一条上不同就停在那里 —— 那个位置就是「这次请求与上次在哪个消息开始分叉」。
    """
    count = 0
    chars = 0
    for old, new in zip(previous, current):
        if canonical_message(old) != canonical_message(new):
            break
        count += 1
        chars += len(_content_of(new))
    return count, chars


def request_chars(messages: Sequence[Any]) -> int:
    """这份请求的字符数（**与 `budget.estimate_chars` 同一口径**：正文长度求和）。"""
    return sum(len(_content_of(item)) for item in messages)


def deviation_reason(
    *,
    has_previous: bool,
    stable_prefix_same: bool,
    is_extension: bool,
    compacted: bool,
    prompt_changed: bool,
    snapshot_changed: bool,
) -> str:
    """分叉原因（判据集中在这一个纯函数里，取值见 `DIVERGENCE_REASONS` 的白名单）。

    判据的**顺序**是刻意的，改顺序就会改结论：

    1. 没有上一个请求 → `initial`；
    2. 稳定前缀变了 → 先看两把版本号：**快照优先**（换了快照意味着变更清单整段换了，
       这时候提示词版本通常没变；先判提示词会把它写成 `prompt_change` —— 一句错话，
       会把查的人引去翻 `prompt.py`）。两把都变了也报 `snapshot_change`：它至少是
       一个能对上号的解释，而 `other` 是「没人解释得了」，那一档要留给真的没人解释的
       情形（两把版本号都没变、前缀却变了 = 平台在别处重写了前缀，那才是要去看代码的）；
    3. 稳定前缀没变而这次是上一次的严格延长 → `append`；
    4. 不然若这一轮压过历史 → `compaction`；
    5. 都不成立 → `other`。
    """
    if not has_previous:
        return DIVERGENCE_INITIAL
    if not stable_prefix_same:
        if snapshot_changed:
            return DIVERGENCE_SNAPSHOT_CHANGE
        if prompt_changed:
            return DIVERGENCE_PROMPT_CHANGE
        return DIVERGENCE_OTHER
    if is_extension:
        return DIVERGENCE_APPEND
    if compacted:
        return DIVERGENCE_COMPACTION
    return DIVERGENCE_OTHER


# ---------------------------------------------------------------------------
#  一次请求的指纹
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestFingerprint:
    """一次请求的诊断指纹。**只有哈希与计数，没有正文**（口径 2）。

    字段分三类：

    * **诊断值**：`request_fingerprint` / `stable_prefix_fingerprint` /
      `prefix_common_messages` / `prefix_common_chars` / `prefix_divergence_reason`；
    * **形状**：`message_count` / `request_chars` / `stable_messages`；
    * **本次运行的固定事实**：`prompt_version` / `snapshot_id` / `compacted` /
      `model` / `endpoint`（`endpoint` 是归一化之后的地址，**不含凭据**）。

    「角色」（主代理 / 分片 / 汇总 / 对账）**不在这里**：它由事件行自己的
    `member` / `member_index` / `member_total` 三列推出来
    （`round_events.member_role`，唯一一份判据）。这个模块在引擎里跑，那时分片标签
    还没贴上去（`subagent._call_engine` 在回调外层贴），自己再猜一个必然分叉。
    """

    request_fingerprint: str = ""
    stable_prefix_fingerprint: str = ""
    # `None` = 没有可比的上一个请求（口径 3）。
    prefix_common_messages: Optional[int] = None
    prefix_common_chars: Optional[int] = None
    prefix_divergence_reason: str = DIVERGENCE_INITIAL
    message_count: int = 0
    request_chars: int = 0
    stable_messages: int = STABLE_PREFIX_MESSAGES
    prompt_version: str = ""
    snapshot_id: str = ""
    # 这一次请求的开头是不是被平台自己压过（历史压缩、压小重发、收尾提示词）。
    compacted: bool = False
    model: str = ""
    endpoint: str = ""

    def to_dict(self) -> dict:
        """给落库用的字典（进事件行的 `fingerprint_json`）。**哈希与计数，无正文。**"""
        return {
            "request_fingerprint": self.request_fingerprint,
            "stable_prefix_fingerprint": self.stable_prefix_fingerprint,
            "prefix_common_messages": self.prefix_common_messages,
            "prefix_common_chars": self.prefix_common_chars,
            "prefix_divergence_reason": self.prefix_divergence_reason,
            "message_count": self.message_count,
            "request_chars": self.request_chars,
            "stable_messages": self.stable_messages,
            "prompt_version": self.prompt_version,
            "snapshot_id": self.snapshot_id,
            "compacted": bool(self.compacted),
            "model": self.model,
            "endpoint": self.endpoint,
        }


def event_columns(fingerprint: Any) -> dict:
    """指纹 → 事件表的列（**唯一一份映射**）。

    `None`（没算出来 / 老调用方）→ 每一列都是 `None`（未上报），**不编一个 0**：
    「这一次没算指纹」与「公共前缀 0 条」是两件事。

    只有三个字段单独占列（它们是会被「按原因筛」「按前缀比」读的），其余进
    `fingerprint_json`（形状与版本号那一组，给人看与给脚本读）。
    """
    if fingerprint is None:
        return {
            "request_fingerprint": None,
            "stable_prefix_fingerprint": None,
            "prefix_common_messages": None,
            "prefix_common_chars": None,
            "prefix_divergence_reason": None,
            "fingerprint_json": None,
        }
    try:
        payload = fingerprint.to_dict()
    except AttributeError:
        # 长得像指纹但不是（测试替身给了个 dict）：按 dict 读，读不出来就是未上报。
        payload = dict(fingerprint) if isinstance(fingerprint, Mapping) else {}
    if not payload:
        return event_columns(None)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        encoded = None
    return {
        "request_fingerprint": str(payload.get("request_fingerprint") or "")[:DIGEST_CHARS] or None,
        "stable_prefix_fingerprint": str(payload.get("stable_prefix_fingerprint") or "")[:DIGEST_CHARS] or None,
        "prefix_common_messages": _optional_int(payload.get("prefix_common_messages")),
        "prefix_common_chars": _optional_int(payload.get("prefix_common_chars")),
        "prefix_divergence_reason": str(payload.get("prefix_divergence_reason") or "")[:30] or None,
        "fingerprint_json": encoded,
    }


def _optional_int(value: Any) -> Optional[int]:
    """读一个整数；读不出来（含 `None`）就是 `None`。**不许兜成 0**（口径 3）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
#  调用侧：算一次、记住它，下一次拿它当「上一个请求」
# ---------------------------------------------------------------------------


@dataclass
class RequestFingerprinter:
    """一次运行（一个成员）内，逐次调用算指纹并记住上一次。

    `endpoint` / `model` / `prompt_version` / `snapshot_id` 是**本次运行的固定事实**，
    构造时给一次：它们逐轮再读一遍也不会变，而「每轮重算一次快照 id」会真的去问一遍
    provider（那是一次列跟踪树的开销）。

    **这个对象不是线程安全的**，也不该是：它描述的是「同一个成员的消息序列」，而那
    序列本来就是单线程顺序长出来的（引擎每轮一次调用）。
    """

    prompt_version: str = ""
    snapshot_id: str = ""
    model: str = ""
    endpoint: str = ""
    stable_messages: int = STABLE_PREFIX_MESSAGES
    # 上一次调用发出的（去掉断点后的）消息序列与它的指纹。
    _previous_messages: list = field(default_factory=list, repr=False)
    _previous: Optional[RequestFingerprint] = field(default=None, repr=False)

    @property
    def previous(self) -> Optional[RequestFingerprint]:
        """上一次调用的指纹（`None` = 还没调过）。给测试与日志用。"""
        return self._previous

    def before_call(
        self, messages: Sequence[Any], *, compacted: bool = False
    ) -> Optional[RequestFingerprint]:
        """**发送之前**调一次：返回这一次请求的指纹，失败返回 `None`。

        ## 为什么调用点必须是「真正要发的那一份消息」

        `messages` 必须是**原样交给 `client.complete` 的那个列表**：指纹的意义就是
        「实际发出去的东西与上次差在哪」，按「打算发的」算会漏掉最后一步的改写。
        内部缓存断点由本函数自己摘掉（`prompt_cache.strip_cache_breakpoints`）——
        摘的是标记，**内容一个字节都不动**。

        ## 任何异常都吞掉

        调用点的两侧都是付费的模型调用：让一个诊断把一次分析弄挂，是本末倒置。
        算不出来就返回 `None`，事件行上那几列记 `None`（未上报）。
        """
        try:
            clean = strip_cache_breakpoints(messages)
        except Exception:  # noqa: BLE001 —— 见 docstring：诊断不许弄挂分析
            return None
        try:
            fingerprint = self._compute(clean, compacted=compacted)
        except Exception:  # noqa: BLE001 —— 同上
            return None
        self._previous_messages = clean
        self._previous = fingerprint
        return fingerprint

    # -- 内部 ---------------------------------------------------------------

    def _compute(self, clean: Sequence[Any], *, compacted: bool) -> RequestFingerprint:
        previous = self._previous_messages
        has_previous = self._previous is not None
        counts = common_prefix(previous, clean) if has_previous else None
        stable_same = True
        if has_previous and counts is not None:
            # 「稳定前缀那一段没变」= **两边都有的那部分**逐条相同（取两者条数与
            # `stable_messages` 的最小值）。不拿两个 `stable_prefix_digest` 直接比：
            # 上一次请求比这一次短时（例如上一次只有 system），两个哈希必然不同，
            # 而那句话不是「前缀被改写」，只是「上一次还没有那一段」—— 直接比会把它
            # 报成 `prompt_change` / `other`（一条假线索）。
            shared = min(len(previous), len(clean), self.stable_messages)
            stable_same = counts[0] >= shared
        # 「这一次是上一次的严格延长」：上一条一条不差地是这一次的前缀，且这次更长。
        is_extension = bool(
            has_previous
            and len(clean) >= len(previous)
            and counts is not None
            and counts[0] == len(previous)
        )
        prompt_changed = bool(
            has_previous
            and self._previous is not None
            and self._previous.prompt_version != self.prompt_version
        )
        snapshot_changed = bool(
            has_previous
            and self._previous is not None
            and self._previous.snapshot_id != self.snapshot_id
        )
        return RequestFingerprint(
            request_fingerprint=request_digest(clean),
            stable_prefix_fingerprint=stable_prefix_digest(
                clean, stable_messages=self.stable_messages
            ),
            # 没有上一个请求时是 `None`（口径 3），不是 0。
            prefix_common_messages=None if counts is None else counts[0],
            prefix_common_chars=None if counts is None else counts[1],
            prefix_divergence_reason=deviation_reason(
                has_previous=has_previous,
                stable_prefix_same=stable_same,
                is_extension=is_extension,
                compacted=bool(compacted),
                prompt_changed=prompt_changed,
                snapshot_changed=snapshot_changed,
            ),
            message_count=len(clean),
            request_chars=request_chars(clean),
            stable_messages=self.stable_messages,
            prompt_version=self.prompt_version,
            snapshot_id=self.snapshot_id,
            compacted=bool(compacted),
            model=self.model,
            endpoint=self.endpoint,
        )


# ---------------------------------------------------------------------------
#  一次运行的诊断账：逐请求指纹 + 三类耗时
# ---------------------------------------------------------------------------


def _millis_since(started: float) -> int:
    """从某个 `time.monotonic()` 起点到现在的毫秒数。"""
    return int((time.monotonic() - started) * 1000)


class RoundDiagnostics:
    """一次运行（一个成员）的诊断账：逐请求指纹、三类耗时、当前这一轮的状态。

    ## 为什么整块搬出引擎

    `services/ai/engine.py` 贴着 2000 行的 ERROR 闸门（`scripts/check_file_length.py`），
    而这一块是**记账**、不是编排：引擎只在三个时刻告诉它发生了什么
    （建账 / `begin_round` / `model_call`），轮到写账时取一次 `event_fields()`。
    它同时解掉另一件更容易出错的事 —— 逐处复制的字段一定会漏（五个 `RoundRecord`
    构造点，漏掉的那个在界面上就是「这一轮没算指纹」，而字段有默认值、不会报错）。

    ## 三类耗时**分开**是刻意的，但优先级是次要的

    指引实测本样本 99.7% 的时间在模型调用链上，所以这三样是观测值、不是优化的靶子：
    `model_call_ms` 量的是真的模型调用（含压小重发与收尾那几次），`tool_fetch_ms` 量的是
    这一轮真的去 Git / Excel 取数的那一段（provider 内部建索引也在里面 —— 这一层看不到
    更细的切分，**如实记成一把**，不猜一个拆出来的数），`index_build_ms` 量的是循环之前
    那一段本地准备（拼系统提示词 / 证据预取 / 首轮组装），它只发生一次、只记在第 1 轮上。

    `None` 一律表示「这一轮没有这一类」或「没算出来」，**不是 0**；`model_call_ms` 是
    本地计时（不依赖上游），所以它是个整数（没有调用过的轮次是 0 —— 那种轮次今天不存在）。
    """

    def __init__(
        self,
        *,
        prompt_version: str = "",
        snapshot_id: str = "",
        model: str = "",
        endpoint: str = "",
        stable_messages: int = STABLE_PREFIX_MESSAGES,
    ) -> None:
        self.fingerprints = RequestFingerprinter(
            prompt_version=prompt_version,
            snapshot_id=snapshot_id,
            model=model,
            endpoint=endpoint,
            stable_messages=stable_messages,
        )
        self._started: Optional[float] = None
        # 循环之前那一段本地准备的耗时（第一次模型调用之前量一次，只量一次）。
        self.index_build_ms: Optional[int] = None
        # 这一轮的耗时与指纹。`model_call_ms` 是累计值（一轮里可能有 2~3 次调用）。
        self.model_call_ms: int = 0
        self.tool_fetch_ms: Optional[int] = None
        # 这一轮的上游用量来源与推理 token（由 `note_usage` 在调用之后填）。
        self.reasoning_tokens: Optional[int] = None
        self.usage_source: str = ""
        self.reasoning_source: str = ""
        # 这一轮的请求开头是不是被平台自己压过（历史压缩 / 压小重发 / 收尾）。
        self.compacted: bool = False
        # 这一轮**最后一次**发出去的请求的指纹（这一轮就是用它的回答做的决定）。
        self.fingerprint: Optional[RequestFingerprint] = None

    @classmethod
    def for_run(cls, client: Any, *, provider: Any = None) -> "RoundDiagnostics":
        """按一次真实运行建账：三个固定事实只读一次。

        提示词版本（源码内容哈希）、快照 id（`provider_snapshot_id`；拿不到是空串 ——
        那表示「不知道这次是哪个快照」，判据据此退回 `other`，**不是**断言「快照没变」）、
        以及模型与端点（**不含凭据**：`base_url` 在客户端构造时已经归一）。

        取不到就给空串：这些值来自调用方的对象（测试替身、探针都可能没有那两属性），
        拿 `getattr` 而不是要求它们 —— 诊断层不该给别的层加接口要求。
        """
        return cls(
            prompt_version=prompt_version(),
            snapshot_id=provider_snapshot_id(provider),
            model=str(getattr(client, "model", "") or ""),
            endpoint=str(getattr(client, "base_url", "") or ""),
        )

    def start(self) -> None:
        """循环开始之前调一次：本地准备的计时起点。"""
        self._started = time.monotonic()

    def begin_round(self) -> None:
        """每一轮开头调一次：把这一轮的状态清零（上一轮的指纹不许留到这一轮）。"""
        self.model_call_ms = 0
        self.tool_fetch_ms = None
        self.reasoning_tokens = None
        self.usage_source = ""
        self.reasoning_source = ""
        self.compacted = False
        self.fingerprint = None

    def mark_compacted(self) -> None:
        """记下「这一轮的请求开头被平台自己压过」（压缩 / 压小重发 / 收尾）。"""
        self.compacted = True

    def model_call(self, messages: Sequence[Any], complete: Callable[..., Any], kwargs: dict) -> Any:
        """发一次模型调用：**先**算这一次请求的指纹，再计时跑 `complete(messages, **kwargs)`。

        计时放在 `finally` 里：抛异常的那一次调用同样是花了时间的（而且往往最贵），
        漏掉它会让「补救过的轮次」在账上看着比没补救的还便宜。
        """
        self._before_call(messages)
        started = time.monotonic()
        try:
            return complete(messages, **kwargs)
        finally:
            self.model_call_ms += _millis_since(started)

    def tool_fetch(self, execute: Callable[..., Any], requests: Any) -> Any:
        """本地取数的耗时（这一轮真的去取数的那一段）。"""
        started = time.monotonic()
        try:
            return execute(requests)
        finally:
            self.tool_fetch_ms = _millis_since(started)

    def note_usage(self, usage: Mapping[str, Any]) -> None:
        """记下上游这一次报了什么（在调用**之后**调一次）。

        `reasoning_tokens` / `usage_source` / `reasoning_source` 都来自 `engine._usage_of`
        读出来的那一份（**读法只有一处**，三处 `getattr(result, ...)` 会漏掉「重试之后
        那一次调用」那一路）。上游没报就是 `None` / `""`，**不是 0**。
        """
        self.reasoning_tokens = _optional_int(usage.get("reasoning_tokens"))
        self.usage_source = str(usage.get("cache_source") or "")
        self.reasoning_source = str(usage.get("reasoning_source") or "")

    def event_fields(self, round_index: int) -> dict:
        """给逐轮事件账本的那几列（**唯一一份映射**：列名与模型逐字对齐）。"""
        fields = {
            "reasoning_tokens": self.reasoning_tokens,
            "usage_source": self.usage_source or None,
            "reasoning_source": self.reasoning_source or None,
            "model_call_ms": int(self.model_call_ms),
            "tool_fetch_ms": self.tool_fetch_ms,
            # 那一整段只发生一次：记在第 1 轮上，其余轮是 `None`（不是 0）。
            "index_build_ms": self.index_build_ms if int(round_index) == 1 else None,
        }
        fields.update(event_columns(self.fingerprint))
        return fields

    # -- 内部 ---------------------------------------------------------------

    def _before_call(self, messages: Sequence[Any]) -> None:
        if self.index_build_ms is None and self._started is not None:
            # 第一次模型调用之前的那一刻 = 本地准备那一段的终点。
            self.index_build_ms = _millis_since(self._started)
        self.fingerprint = self.fingerprints.before_call(
            messages, compacted=self.compacted
        )


# ---------------------------------------------------------------------------
#  运行侧的固定事实：快照 id
# ---------------------------------------------------------------------------


def provider_snapshot_id(provider: Any) -> str:
    """本次分析冻结在哪个快照上（一个能区分「换了快照」的短标识）。拿不到就是空串。

    ## 为什么用鸭子类型，而不是往 `ContextProvider` 协议上加方法

    与 `frozen_repo.provider_repo_paths` 同一条理由：`ContextProvider` 是有意做窄的
    协议，而它的实现有一大堆（真实 provider、假 provider、测试桩），往协议上加一项
    等于要求每一个都跟上 —— 而它们大多根本不懂「冻结快照」这回事。

    读的是 `repo_read_scope().frozen.tip`（`platform_provider` 那一层已经解析过一次，
    结果被缓存，不会每轮去问 git）。**拿不到给空串**，它的含义是「这次不知道快照是
    哪一个」—— 判据那一侧据此退回 `other`（见 `deviation_reason`），而不是断言
    「快照没变」。
    """
    getter = getattr(provider, "repo_read_scope", None)
    if not callable(getter):
        return ""
    try:
        scope = getter()
    except Exception:  # noqa: BLE001 —— 拿不到标识不该影响这次运行
        return ""
    frozen = getattr(scope, "frozen", None)
    return str(getattr(frozen, "tip", "") or "")[:40]


__all__ = [
    "DIGEST_CHARS",
    "DIVERGENCE_APPEND",
    "DIVERGENCE_COMPACTION",
    "DIVERGENCE_INITIAL",
    "DIVERGENCE_OTHER",
    "DIVERGENCE_PROMPT_CHANGE",
    "DIVERGENCE_REASONS",
    "DIVERGENCE_SNAPSHOT_CHANGE",
    "STABLE_PREFIX_MESSAGES",
    "RequestFingerprint",
    "RequestFingerprinter",
    "RoundDiagnostics",
    "canonical_message",
    "common_prefix",
    "deviation_reason",
    "digest",
    "event_columns",
    "provider_snapshot_id",
    "request_chars",
    "request_digest",
    "stable_prefix_digest",
]
