"""模型输出的解析、校验与接地。

## 三层职责，失败代价逐层降低

1. **解析**（`parse_json_candidates` / `parse_payload`）：模型可能返回带思考块、
   代码围栏或前后解释文字的 JSON。这一层尽量把它救回来。
2. **协议校验**：结构性错误（status 不认识、`final` 却没有报告正文）→ 抛
   `ProtocolError`，由编排层把它作为「纠正提示」重问一轮。这类错误重问一次通常就
   好了，且重问比硬猜安全。
3. **接地校验**（`ground_payload`）：**逐条**校验，不合法的**丢弃并记账**，不让整单
   失败。区别在于：协议错误是「我没看懂」，接地失败是「这一条不可信」——后者丢掉
   那一条就够了，为它作废其余 9 条正确结论代价太大。

    模型编造 commit、或者引用一个该提交根本没改过的文件，都是常见的。不校验的话，
    前端会渲染出点不开的链接，跟进的人查不到东西。

## 但有一条**永远不许**被丢弃：`category` 不是「可不可信」的问题

「这条结论可不可信」可以逐条判，因为判据（commit 与路径是否真实）是平台手里的事实；
「这条结论属于哪个维度」不是 —— 维度清单是**项目可声明**的
（`LoadedSkills.dimensions`，见 `skill_contract.render_dimension_section`），这一层没有
它自己的来源。若按平台出厂值判一次，就正好成了「校验按 A、提示词按 B」：声明了自己清单的
项目里，模型按提示词写下的真实发现会被静默丢掉，而报告看起来完全正常（只是少了一条）。

所以这里的口径是：**category 只决定它归到哪一组，不决定它留不留下**。落不进清单的条目
按原样保留（原始 category 一个字不改），由知道清单的那一层归到「未归类」并单独列出来
（`unclassified_anomalies`、`subagent.build_unclassified_section`、
`report_document.dimension_label`）。少一条发现是静默的，多一个「未归类」是响亮的。

清单可以由调用方**传进来**（`parse_payload(dimension_ids=…)`，引擎把
`LoadedSkills.dimensions` 的 id 传下来），但传进来也只用于**记账**（说明这一条为什么
会显示成「未归类」），不参与任何一条发现的取舍 —— 传与不传，`anomalies` 都是同一份。

## 「像不像一份报告」这个判定为什么重要

多轮预算耗尽时，模型可能已经输出了完整的 Markdown 报告、只是没按 JSON 协议收尾。
这时直接判失败会让用户白等一场。所以最后一个兜底是：如果回答里有足够多的约定章节
标题，就把它当报告降级返回（异常清单为空）。这个判定复用 `REPORT_SECTIONS`——**格式
约束同时当健康检查用**，是笔很划算的买卖。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from services.ai.reference_search import MIN_QUERY_CHARS, normalize_query
from services.ai.scope import AnalysisScope, normalize_path
from services.ai.skill_contract import (
    CONFIDENCES,
    DIMENSION_IDS,
    REPORT_SECTIONS,
    REQUEST_TYPES,
    SEVERITIES,
)

STATUS_NEED_MORE_CONTEXT = "need_more_context"
STATUS_FINAL = "final"
STATUSES = (STATUS_NEED_MORE_CONTEXT, STATUS_FINAL)

# 工具类型、severity、confidence 的允许集合定义在 `skill_contract` 里 —— 它们是
# 契约的一部分（必须与 SKILL.md 里给模型看的枚举逐字一致），由校验器自动守住。
REQUEST_TYPES_NEEDING_COMMIT = ("commit_detail", "file_diff", "file_content")
REQUEST_TYPES_NEEDING_PATH = ("file_diff", "file_content")

# 认「窗口」(`lines`) 的工具：返回长内容的四个。单位各不相同（见 `ContextRequest.lines`），
# 但语法与规范化是同一条 —— 于是「哪一段」这件事在协议层只需要一处解析。
_WINDOW_TYPES = ("file_content", "file_diff", "read_reference", "commit_detail")

# 报告健康检查需要命中多少个章节标题才认为「这像一份报告」。
REPORT_HEALTH_MIN_SECTIONS = 2

# 去掉模型可能带上的推理块。
_THINK_BLOCK_RE = re.compile(r"<think\b.*?</think\s*>", re.S | re.I)
# 代码围栏（含语言标注）。
_FENCED_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*(.*?)```", re.S)
_FENCE_MARKER_RE = re.compile(r"```[a-zA-Z0-9_+-]*")


class ProtocolError(ValueError):
    """模型的返回不符合协议。会被转成「纠正提示」重问一轮。"""


@dataclass(frozen=True)
class ContextRequest:
    type: str
    commit: str = ""
    path: str = ""
    name: str = ""
    # 要**哪一段**。空表示「你替我挑一段」：
    # `file_content` 会给改动附近那一段（见 `ai/platform_provider._render_text_content`），
    # 其余三个从第一段开始、按字符上限装到装不下为止。
    #
    # 为什么要有这个字段：代码文件的正文动辄几千行，整份给既超预算又没用（从中间截断的
    # 正文等于没有上下文）。让它点名要哪一段，比让平台猜它想看哪里准得多，也省得多。
    #
    # **单位随工具变**（四个认它的工具各自在返回文本的抬头里写明），这正是这个字段
    # 不再只叫「行窗口」的原因：`file_content` 是行号 `"1180-1260"`、`file_diff` 是第几个
    # 改动块、`read_reference` 是第几小节、`commit_detail` 是第几个改动文件。
    lines: str = ""
    # `find_references` 的搜索词（一个标识符：字段名、协议名、函数名、配置 ID）。
    # 只有这一个类型用它；其余类型的请求里它一律被清空（见 `sanitize_requests`）。
    query: str = ""

    def describe(self) -> str:
        if self.lines:
            # 标签是回查内容的地址，所以「要了哪一段」必须写进去（同一个文件的两段
            # 若共用一行标签，正文里就会出现两个标题一样的 `###` 节）。
            head = (
                f"read_reference {self.name}"
                if self.type == "read_reference"
                else f"{self.type} {self.commit[:12]} {self.path}".strip()
            )
            return f"{head} lines={self.lines}"
        if self.type == "read_reference":
            return f"read_reference {self.name}"
        if self.type == "commit_detail":
            return f"commit_detail {self.commit[:12]}"
        return f"{self.type} {self.commit[:12]} {self.path}"


@dataclass(frozen=True)
class Anomaly:
    title: str
    category: str
    severity: str
    confidence: str
    evidence: tuple[str, ...]
    commit: str = ""
    file_path: str = ""
    impact: str = ""
    suggestion: str = ""


@dataclass(frozen=True)
class DimensionReview:
    id: str
    hit: bool
    note: str = ""


@dataclass(frozen=True)
class DroppedItem:
    """条目级记账：**被丢弃**的条目及原因，以及**被归到「未归类」**的条目。

    `kind` 的取值为 `anomaly` / `dimension` / `request` / `subagent` / `unclassified`。
    `unclassified` 那一条与其他几种**不是一回事**：那条发现**没有被丢掉**（它在
    `payload.anomalies` 里，报告里也列着），这里只是把「为什么它的维度显示成未归类」
    记下来。它借用这一个结构是因为 trace 是平台里唯一一条按条目把记录带到面板上的
    通道（`result_payload` 的 `dropped` → `trace_evidence.summarize_dropped`）。
    """

    kind: str
    index: int
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class AnalysisPayload:
    status: str
    reason: str = ""
    requests: tuple[ContextRequest, ...] = ()
    report_markdown: str = ""
    anomalies: tuple[Anomaly, ...] = ()
    dimensions: tuple[DimensionReview, ...] = ()
    dropped: tuple[DroppedItem, ...] = field(default_factory=tuple)

    @property
    def is_final(self) -> bool:
        return self.status == STATUS_FINAL


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def parse_json_candidates(text: str) -> list[Any]:
    """把模型回答里可能的 JSON 逐个试出来，返回成功解析的结果列表。

    候选按「可信度从高到低」生成：原文最先，其次是剥掉推理块/围栏的形态，最后是从
    首尾大括号切出来的片段。逐个 `json.loads`，能解析成功的都返回 —— 调用方取第一个
    符合协议的。
    """
    raw = str(text or "")
    if not raw.strip():
        return []

    candidates: list[str] = []

    def _add(value: str) -> None:
        value = value.strip()
        if value and value not in candidates:
            candidates.append(value)

    _add(raw)
    _add(_THINK_BLOCK_RE.sub("", raw))

    for block in _FENCED_BLOCK_RE.findall(raw):
        _add(block)
    _add(_FENCE_MARKER_RE.sub("", raw))

    # 首尾大括号之间切片：模型在 JSON 前后写了说明文字时靠这一步。
    for candidate in list(candidates):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            _add(candidate[start : end + 1])

    parsed: list[Any] = []
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        parsed.append(value)
    return parsed


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _as_evidence(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, list):
        items = tuple(
            item.strip() for item in (_as_str(entry) for entry in value) if item
        )
        return items
    return ()


def _coerce_requests(value: Any) -> tuple[ContextRequest, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProtocolError("requests 必须是数组")
    requests: list[ContextRequest] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        requests.append(
            ContextRequest(
                type=_as_str(entry.get("type")),
                commit=_as_str(entry.get("commit")),
                path=_as_str(entry.get("path")),
                name=_as_str(entry.get("name")),
                lines=_as_str(entry.get("lines")),
                # `find_references` 的搜索词。**漏掉这一个字段的后果不是「差一点」**：
                # `sanitize_requests` 会按「搜索词太短（至少 3 个字）」把每一条
                # `find_references` 都丢掉，于是这个工具端到端**从来没有执行过**，
                # 而丢掉的理由还把责任推给了模型（它明明按 SKILL.md 写了 query）。
                query=_as_str(entry.get("query")),
            )
        )
    return tuple(requests)


def _coerce_anomalies(
    value: Any, dimension_ids: Iterable[str] = DIMENSION_IDS
) -> tuple[tuple[Anomaly, ...], tuple[DroppedItem, ...]]:
    """把 `anomalies` 逐条读成 `Anomaly`。**category 不在清单内的条目不丢。**

    ## 为什么 category 不再被丢弃

    这里原先写的是 `if category not in DIMENSION_IDS: 丢弃`。于是模型给出一个不在
    清单里的 category（换一个项目、换一套维度时这是必然会发生的）时，那一条异常**从
    报告里彻底消失**：报告看起来完全正常，只是少了一条，而没有任何人会去数。异常清单
    里没有它、报告正文里没有它，只剩 trace 里一条谁都看不到的记录 —— 这正是这套结构
    最该避免的失真形态。

    现在的口径：**category 只决定它归到哪一组，不决定它留不留下。** 落不进「本次生效的
    维度清单」的条目按原样保留（原始 category 一个字不改），由知道清单的那一层归到
    「未归类」并单独列出来（`unclassified_anomalies` / `subagent` 的报告段 /
    `report_document` 的维度列）。

    ## `dimension_ids` 只用来**记账**，不参与任何取舍

    清单由调用方给（引擎手里那份 `LoadedSkills.dimensions`）。它在这里只有一个用途：
    给「category 不在清单内」的条目留一条 `unclassified` 记录 —— 否则界面/报告上明明
    写着「未归类（performance）」，而没有任何地方说明**为什么**它没被认领。

    判据仍然是「结构问题才丢」：缺标题、severity/confidence 不在两档内、证据为空。
    那些不是「归错组」，而是这一条根本立不住（没有证据的断言无法跟进），且都记账。

    **记账刻意排在全部结构校验之后**：那两条记录写的是「未丢弃，已归入未归类」，
    而一条随后因为缺证据被丢掉的条目会让这句话变成假的（trace 里同时出现「已归入
    未归类」与「evidence 为空」）。
    """
    if value is None:
        return (), ()
    if not isinstance(value, list):
        raise ProtocolError("anomalies 必须是数组")

    allowed = {str(item).strip() for item in dimension_ids if str(item or "").strip()}
    kept: list[Anomaly] = []
    dropped: list[DroppedItem] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            dropped.append(DroppedItem("anomaly", index, "条目不是对象"))
            continue

        title = _as_str(entry.get("title"))
        if not title:
            dropped.append(DroppedItem("anomaly", index, "缺少标题"))
            continue

        # category 缺失也保留：没有归属的**发现**仍然是发现（下面的 reason 记着这件事），
        # 中文名那一格会显示成「未归类（未标注）」。丢掉它等于平台替模型做了一个
        # 「这条不重要」的判断，而平台没有这个依据。
        category = _as_str(entry.get("category"))

        severity = _as_str(entry.get("severity")).lower()
        if severity not in SEVERITIES:
            dropped.append(DroppedItem("anomaly", index, "severity 不在允许集合内", severity))
            continue

        confidence = _as_str(entry.get("confidence")).lower()
        if confidence not in CONFIDENCES:
            # 达不到门槛的判断只该写进报告正文，不该进这份给人工跟进的清单。
            dropped.append(
                DroppedItem("anomaly", index, "confidence 未达门槛", confidence)
            )
            continue

        evidence = _as_evidence(entry.get("evidence"))
        if not evidence:
            # 没有证据的断言无法跟进，也是空泛表述的主要来源。
            dropped.append(DroppedItem("anomaly", index, "evidence 为空"))
            continue

        # 这一条**留下来了**，只是没有归属（或归属不在清单内）—— 记账在这里写，
        # 上面那个「未丢弃」的说法才不会是假的。
        if not category:
            dropped.append(
                DroppedItem("unclassified", index, "缺 category（未丢弃，已归入「未归类」）", "")
            )
        elif category not in allowed:
            dropped.append(
                DroppedItem(
                    "unclassified",
                    index,
                    "category 不在本次生效的维度清单内（未丢弃，已归入「未归类」）",
                    category,
                )
            )

        kept.append(
            Anomaly(
                title=title,
                category=category,
                severity=severity,
                confidence=confidence,
                evidence=evidence,
                commit=_as_str(entry.get("commit")),
                file_path=normalize_path(_as_str(entry.get("file_path"))),
                impact=_as_str(entry.get("impact")),
                suggestion=_as_str(entry.get("suggestion")),
            )
        )
    return tuple(kept), tuple(dropped)


def unclassified_anomalies(
    anomalies: Iterable[Anomaly], dimension_ids: Iterable[str]
) -> tuple[Anomaly, ...]:
    """落在**本次生效的维度清单**之外的那些条目（要归到「未归类」里）。

    这是「不丢掉发现」的最后一步：解析层保留它们、这一层把它们点名出来，所以报告里
    一定有一处写着「这条不属于本次清单里的任何维度」。`dimension_ids` 由调用方给
    （`LoadedSkills.dimensions` 的那一份，或多成员计划里汇总那一份）—— **平台里只有
    一个地方能回答「本次清单是什么」，就是它**。

    category 为空的条目也算在内：它同样没有被认领。
    """
    allowed = set(dimension_ids)
    return tuple(item for item in anomalies if item.category not in allowed)


def _coerce_dimensions(value: Any) -> tuple[tuple[DimensionReview, ...], tuple[DroppedItem, ...]]:
    """把 `dimensions` 逐条读成 `DimensionReview`。**id 不在清单内的条目不丢。**

    与 `_coerce_anomalies` 同一条口径（那里的注释解释了为什么这一层不判集合）：模型
    按**项目声明的清单**写了 `performance`，而这里若按平台出厂值判，那条留痕会消失 ——
    提示词要求它写的维度，在「逐一交代」表里反而看不到，报告读起来完全正常。
    """
    if value is None:
        return (), ()
    if not isinstance(value, list):
        raise ProtocolError("dimensions 必须是数组")

    kept: list[DimensionReview] = []
    dropped: list[DroppedItem] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            dropped.append(DroppedItem("dimension", index, "条目不是对象"))
            continue
        identifier = _as_str(entry.get("id"))
        if not identifier:
            # 空 id 是结构问题：既分不了组、也没法显示（这一条本来就没说它是什么维度）。
            dropped.append(DroppedItem("dimension", index, "缺少 id"))
            continue
        kept.append(
            DimensionReview(
                id=identifier,
                hit=bool(entry.get("hit")),
                note=_as_str(entry.get("note")),
            )
        )
    return tuple(kept), tuple(dropped)


def _select_payload_object(parsed: Iterable[Any]) -> dict:
    """从解析结果里挑出符合协议外壳的那个对象。

    优先取带 `status` 的；没有就取第一个字典（模型可能漏了 status，交给协议校验
    去报错，报出的信息比「不是 JSON」更准确）。
    """
    fallback: dict | None = None
    for value in parsed:
        if not isinstance(value, dict):
            continue
        if _as_str(value.get("status")):
            return value
        if fallback is None:
            fallback = value
    if fallback is not None:
        return fallback
    raise ProtocolError("回答里找不到可解析的 JSON 对象")


def parse_payload(
    text: str, *, dimension_ids: Iterable[str] = DIMENSION_IDS
) -> AnalysisPayload:
    """把模型回答解析成 `AnalysisPayload`。

    结构性错误抛 `ProtocolError`（编排层据此重问），条目级问题只丢弃并记账。

    `dimension_ids` 是**本次生效的**维度清单（`LoadedSkills.dimensions` 的 id 那一份），
    由引擎传进来；不传就是平台出厂那一份。它**只影响记账**（见 `_coerce_anomalies`），
    不影响任何一条发现的去留 —— 落不进清单的条目照样按原样保留。
    """
    parsed = parse_json_candidates(text)
    if not parsed:
        preview = str(text or "").strip()[:200]
        raise ProtocolError(f"回答里没有可解析的 JSON。原文开头：{preview}")

    raw = _select_payload_object(parsed)

    status = _as_str(raw.get("status")).lower()
    if status not in STATUSES:
        raise ProtocolError(f"status 必须是 {STATUSES} 之一，实际是 {status!r}")

    requests = _coerce_requests(raw.get("requests"))
    if status == STATUS_NEED_MORE_CONTEXT and not requests:
        raise ProtocolError("status 为 need_more_context 时必须给出非空的 requests")

    report_markdown = _as_str(raw.get("report_markdown")) or _as_str(raw.get("report"))
    anomalies, dropped_anomalies = _coerce_anomalies(
        raw.get("anomalies"), dimension_ids=dimension_ids
    )
    dimensions, dropped_dimensions = _coerce_dimensions(raw.get("dimensions"))

    if status == STATUS_FINAL:
        if not report_markdown:
            raise ProtocolError("status 为 final 时必须给出非空的 report_markdown")
        if not dimensions:
            # dimensions 是「每个维度都过了一遍」的证据（清单 = `DIMENSION_IDS`，也就是
            # SKILL.md 的「九个检查维度」）。允许为空等于允许模型只挑好说的说，这正是它
            # 要防的事。条数不写死在这里 —— 写死的那一版曾经写着「六个」，而过了一年没人
            # 发现（`tests/test_ai_dimension_count_stays_in_sync.py` 现在钉住这一条）。
            raise ProtocolError("status 为 final 时必须给出非空的 dimensions（未命中的也要写）")

    return AnalysisPayload(
        status=status,
        reason=_as_str(raw.get("reason")),
        requests=requests,
        report_markdown=report_markdown,
        anomalies=anomalies,
        dimensions=dimensions,
        dropped=dropped_anomalies + dropped_dimensions,
    )


# --------------------------------------------------------------------------
# 接地校验
# --------------------------------------------------------------------------


def ground_payload(payload: AnalysisPayload, scope: AnalysisScope) -> AnalysisPayload:
    """逐条校验异常条目的 commit / file_path 是否真实，丢弃不合法的并记账。

    **不抛异常**：一条越权的条目不该作废其余正确的结论。丢弃的记录进 `dropped`，
    最终写进 trace，这样「为什么这次只报了 3 条」是可追溯的。
    """
    kept: list[Anomaly] = []
    dropped = list(payload.dropped)

    for index, anomaly in enumerate(payload.anomalies):
        resolved = scope.resolve_commit(anomaly.commit)
        if resolved is None:
            dropped.append(
                DroppedItem(
                    "anomaly",
                    index,
                    "commit 不属于本批次（可能是模型编造的）",
                    anomaly.commit,
                )
            )
            continue

        file_path = anomaly.file_path
        if file_path and not scope.path_allowed(resolved, file_path):
            dropped.append(
                DroppedItem(
                    "anomaly",
                    index,
                    "file_path 不在该 commit 改动过的文件里",
                    file_path,
                )
            )
            continue

        kept.append(
            Anomaly(
                title=anomaly.title,
                category=anomaly.category,
                severity=anomaly.severity,
                confidence=anomaly.confidence,
                evidence=anomaly.evidence,
                # 回写成全哈希：短前缀在前端点不开，也没法用于去重。
                commit=resolved,
                file_path=file_path,
                impact=anomaly.impact,
                suggestion=anomaly.suggestion,
            )
        )

    return AnalysisPayload(
        status=payload.status,
        reason=payload.reason,
        requests=payload.requests,
        report_markdown=payload.report_markdown,
        anomalies=tuple(kept),
        dimensions=payload.dimensions,
        dropped=tuple(dropped),
    )


# 控制字符（`\n`、`\r`、`\t` 等 0x00-0x1f，以及 DEL）。**不含空格**：空格是正常路径里
# 会出现的字符，把它算进去会让「文件名里有空格」的请求整批被拒。
_CONTROL_CHARS = frozenset(chr(code) for code in range(0x20)) | {"\x7f"}

# 回显给模型/日志的字段长度上限。字段来自模型，可以任意长 —— 不做上限的话，一条畸形的
# 超长路径会把「上一轮被拒」那句话本身撑爆（那句话要进提示词）。
_ECHO_MAX_CHARS = 200


def _has_control_chars(value) -> bool:
    """字段里是否出现控制字符（含换行）。"""
    return any(char in _CONTROL_CHARS for char in str(value or ""))


def _safe_repr(value) -> str:
    """把可能有害的字段转义成**只含可见字符**的一段文本，用于记账与回显。

    用 `repr` 而不是原值：换行会变成两个可见字符 `\\n`，于是它进了提示词也只是「路径里
    有个奇怪的转义」，而不是一行新的指令。
    """
    text = repr(str(value or ""))
    if len(text) <= _ECHO_MAX_CHARS:
        return text
    return text[: _ECHO_MAX_CHARS - 1] + "…"


def sanitize_requests(
    requests: Iterable[ContextRequest], scope: AnalysisScope
) -> tuple[tuple[ContextRequest, ...], tuple[DroppedItem, ...]]:
    """工具白名单：把越权的上下文请求丢掉。

    这是「模型不能诱导服务端读任意文件」的落点。四重校验：类型在集合内、需要的字段
    齐全、commit 能解析到本批次、path 属于该 commit 改动过的文件。任一不满足就丢弃
    （不报错——模型偶尔写错一个字段不该作废整轮），并记账。

    ## 另加一道：字段里带控制字符的请求按畸形丢掉

    请求字段（`commit` / `path` / `name` / `query`）会被**原样拼进下一轮的提示词**：平台
    用它们写一句「你上一轮这些索取没有被执行：<detail>（<reason>）」（`engine._rejected_note`），
    那句话是**平台自己写的话**，不在任何数据封套里（封套见 `prompt._wrap_untrusted`）。
    一个带换行的路径因此能在提示词里伪造出一行新指令，而且不经过数据封套那条路。

    正常的请求用不到控制字符（`normalize_path` 本来就会 strip 掉首尾空白），所以判据取
    「出现即畸形」，理由用 `repr` 转义后记账 —— 不把原样的控制字符回显出去。
    """
    allowed: list[ContextRequest] = []
    dropped: list[DroppedItem] = []
    seen: set[tuple[str, str, str, str]] = set()

    for index, request in enumerate(requests):
        request_type = str(request.type or "").strip()
        if request_type not in REQUEST_TYPES:
            dropped.append(DroppedItem("request", index, "type 不在白名单内", request_type))
            continue

        unsafe = next(
            (
                value
                for value in (request.commit, request.path, request.name, request.query)
                if _has_control_chars(value)
            ),
            "",
        )
        if unsafe:
            dropped.append(
                DroppedItem(
                    "request",
                    index,
                    "字段里含控制字符（换行等），按畸形请求丢弃",
                    _safe_repr(unsafe),
                )
            )
            continue

        if request_type == "read_reference":
            if not scope.reference_allowed(request.name):
                dropped.append(
                    DroppedItem("request", index, "文档不在可读清单里", request.name)
                )
                continue
            key = (request_type, "", "", request.name)
            if key in seen:
                continue
            seen.add(key)
            allowed.append(ContextRequest(type=request_type, name=request.name))
            continue

        if request_type == "find_references":
            # 这个工具**不带 commit**：它搜的是整个批次改动的文件（每份用各自最后那次
            # 提交的内容），所以它没有「某一条提交」可以校验，取而代之的是两件事 ——
            # 关键词得写得够具体（"id" 这种词会把整批都搜出来），以及可选的 `path`
            # 前缀必须真的匹配到本批次的改动文件。
            query = normalize_query(request.query)
            if len(query) < MIN_QUERY_CHARS:
                dropped.append(
                    DroppedItem(
                        "request",
                        index,
                        f"搜索词太短（至少 {MIN_QUERY_CHARS} 个字），换个具体一点的标识符",
                        query,
                    )
                )
                continue
            prefix = normalize_path(request.path)
            if prefix and not scope.prefix_allowed(prefix):
                dropped.append(
                    DroppedItem(
                        "request",
                        index,
                        "path 前缀匹配不到本批次改动过的任何文件",
                        prefix,
                    )
                )
                continue
            key = (request_type, "", prefix, query)
            if key in seen:
                continue
            seen.add(key)
            allowed.append(
                ContextRequest(type=request_type, path=prefix, query=query)
            )
            continue

        resolved = scope.resolve_commit(request.commit)
        if resolved is None:
            dropped.append(
                DroppedItem("request", index, "commit 不属于本批次", request.commit)
            )
            continue

        if request_type in REQUEST_TYPES_NEEDING_PATH:
            if not normalize_path(request.path):
                # 没给 path、或给的 path 归一化之后是空的：这是**请求格式**的问题，
                # 不是「提交配错了」。两种理由必须分开说 —— 混成同一句会让模型跑去换提交，
                # 而它该做的是把 path 补上（下一轮照原样再要一次也不会被执行）。
                dropped.append(
                    DroppedItem(
                        "request",
                        index,
                        f"{request_type} 必须带 path（这次没给，或给了归一化后为空的路径）",
                        repr(request.path),
                    )
                )
                continue
            if not scope.path_allowed(resolved, request.path):
                # **原因里必须带上是哪条提交，能换的又是哪条。**
                #
                # 这条记录有两个读者，两边都因为「只说『不属于』」吃过亏：
                # * **模型**：它只收到「本轮没有附带任何上下文」，于是把「我把
                #   (commit, path) 配错了」写成「平台取数失败」，还写进报告的信息缺口 ——
                #   读者会去找一个不存在的平台故障。实测那一轮 28 条被拒（占索取数 23%）。
                # * **人**：`detail` 原先只记 path、不记 commit，事后根本判不出是谁配错了。
                #
                # 本批次里改过这个文件的是哪条提交，平台是知道的（`commit_of_path`，
                # `find_references` 用的也是它）—— 说出来，模型下一轮就能问对。
                suggested = scope.commit_of_path(request.path)
                if suggested:
                    reason = (
                        f"这个文件不在 commit {resolved[:12]} 的改动清单里；"
                        f"本批次里改过它的是 {suggested[:12]}，换那条提交再问"
                    )
                else:
                    reason = (
                        f"这个文件不在 commit {resolved[:12]} 的改动清单里，"
                        "也不在本批次改动过的任何文件里"
                    )
                dropped.append(
                    DroppedItem("request", index, reason, f"{request.path}（配的是 {resolved[:12]}）")
                )
                continue
            path = normalize_path(request.path)
        else:
            path = ""

        # 窗口（`lines`）：**不是只有 `file_content` 认它**。四个会返回长内容的工具都用这一套
        # 语法点名「要哪一段」，只是单位不同 —— `file_content` 是行号（`1180-1260`）、
        # `file_diff` 是改动块、`read_reference` 是文档小节、`commit_detail` 是第几个文件。
        # 工具会在返回文本的抬头里写明「共几段 / 这是第几段 / 怎么要别的段」。
        #
        # **不合法一律清空**，而不是丢掉整条请求：内容本身仍然有用，模型把窗口写坏的代价
        # 只能是「拿到的还是默认那一段」。但格式必须是规范形态（`1180-1260`），这样它进得了
        # 去重键、也进得了日志 —— 否则同一个文件的两段窗口会被去重成一条，模型要第二段时
        # 拿回第一段。
        lines = _normalize_line_window(request.lines) if request_type in _WINDOW_TYPES else ""

        key = (request_type, resolved, path, lines)
        if key in seen:
            continue  # 同一轮内重复索要不重复执行
        seen.add(key)
        allowed.append(
            ContextRequest(type=request_type, commit=resolved, path=path, lines=lines)
        )

    return tuple(allowed), tuple(dropped)


# 行窗口的规范形态：`1180-1260`（单行写成 `1180`）。上限只是防呆 —— 真正的夹紧在
# `utils/content_window.slice_lines` 里按实际行数做。
_LINE_WINDOW_RE = re.compile(r"(\d{1,7})\s*(?:[-~—到]\s*(\d{1,7}))?")


def _normalize_line_window(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = _LINE_WINDOW_RE.fullmatch(text)
    if match is None:
        return ""
    start = int(match.group(1))
    if start < 1:
        return ""
    if match.group(2):
        end = int(match.group(2))
        if end < start:
            return ""
        return f"{start}-{end}"
    return str(start)


# --------------------------------------------------------------------------
# 健康检查与纠正提示
# --------------------------------------------------------------------------


def looks_like_markdown_report(text: str) -> bool:
    """回答是否已经像一份报告（用于轮次耗尽时的降级）。

    判据是命中的约定章节标题数。这些标题在 SKILL.md 里被固定下来，**格式约束因此
    同时充当健康检查**——不需要额外让模型输出一个「我完成了」的标记。
    """
    content = _THINK_BLOCK_RE.sub("", str(text or ""))
    hits = sum(1 for section in REPORT_SECTIONS if f"# {section}" in content)
    return hits >= REPORT_HEALTH_MIN_SECTIONS


# 抢正文用：`report_markdown` 的键与开引号。**不能**用一个吃掉整个字符串值的正则 ——
# 实测模型会把长字符串切成好几段（`"第一段","第二段"`），吃整段的写法只能抢到第一段
# （run 7：957 字 / 全长 55k）。所以只匹配到开引号，之后按字符扫（见 `_read_json_string`）。
_REPORT_MARKDOWN_OPENING_RE = re.compile(r'"report_markdown"\s*:\s*"')
# 续写块：逗号 + 引号。
_CONTINUATION_RE = re.compile(r'\s*,\s*"')
# 续写块的最小长度。JSON 的键名（`"anomalies"`、`"dimensions"`）永远比它短 ——
# 没有这道闸，正常收尾的 `"report_markdown": "…", "anomalies": [` 会把键名吃进正文。
_CONTINUATION_CHUNK_MIN = 40

# 输出被截断时发给模型的纠正提示。与 `build_correction_hint` 分开写：那个说的是
# 「你没按协议」，这个说的是「你写太长了」——**模型的应对完全相反**
# （前者要它改格式，后者要它砍内容）。
TRUNCATED_OUTPUT_HINT = (
    "上一轮的回答**被截断了**（单次输出有长度上限，JSON 没有收尾，因此无法解析）。"
    "请**压缩篇幅后完整重发**：正文按后果排序、同类条目合并成一条写，只保留能改变"
    "结论的内容；`dimensions` 与 `anomalies` 必须完整，整份 JSON 必须能解析。"
)


def _decode_json_string_body(body: str) -> str | None:
    """把一段 JSON 字符串的**内容**（不含首尾引号）解码成真文本；解不开返回 None。"""
    try:
        return json.loads(f'"{body}"')
    except ValueError:
        # 转义序列本身被截断（例如末尾是 `\u12`）——这一段救不回来。
        return None


def _read_json_string(content: str, start: int) -> tuple[str | None, int]:
    """从 `start`（开引号**之后**的那一位）读一个 JSON 字符串。

    返回 `(值, 下一个位置)`。**没有闭合引号时读到文本末尾为止** —— 被截断的那种情况
    本来就没有闭合引号，按「就到这里」处理正是我们要的。
    """
    chars: list[str] = []
    index = start
    while index < len(content):
        char = content[index]
        if char == "\\":
            if index + 1 >= len(content):
                break
            chars.append(content[index:index + 2])
            index += 2
            continue
        if char == '"':
            return _decode_json_string_body("".join(chars)), index + 1
        chars.append(char)
        index += 1
    return _decode_json_string_body("".join(chars)), len(content)


def salvage_report_markdown(text: str) -> str | None:
    """从**被截断的**响应里把 `report_markdown` 的正文抢出来；抢不到返回 None。

    ## 为什么需要它

    汇总那一步的正文常常两万多 token（实测 run 5/6/7 分别是 25.9k / 20.6k / 28.2k），
    撞上网关的单次输出上限就断在半截，`json.loads` 必然失败。而整份响应里最值钱的就是
    这段正文：不抢的话，用户拿到的是一坨 **JSON 源码**。

    最糟的一点是它**还会被误判成「像一份报告」**——`looks_like_markdown_report` 是在
    整段文本里数章节标题，而 JSON 字符串里那些 `\\n# 变更理解` 照样能数到，于是走了
    markdown 降级那条路，把 JSON 原文当正文存了下来（2026-09-21 run 7 实测：55k 字的
    报告被包在 `{"status": "final", "report_markdown": "…"` 里面，界面与导出都是这样）。

    ## 还要接着吃「续写块」

    拿 run 7 的原文试过：**只读第一个字符串只能抢到 957 字（全长 55k）**。因为模型写
    长字符串时是这么断的 —— `"第一段","第二段","第三段…`：每一段都是合法字符串，但
    段与段之间只有逗号、没有键名，整份 JSON 因此不合法。这不是截断，是模型自己的切分
    习惯，两种形态都会走到这里，所以续写块也要接上。

    接的条件有两条，缺一不可：**必须是「逗号 + 引号」**的续写形状，且这一段**够长**
    （`_CONTINUATION_CHUNK_MIN`）。后者是防 `"report_markdown": "…", "anomalies": […]`
    这种正常收尾 —— 那里逗号后面也是一个字符串（键名），但键名永远很短。

    ## 只抢正文，不抢 `anomalies`

    截断点通常落在正文之后（它是最后、最大的一个字段），此时 `anomalies` 数组要么没
    开始、要么断在半截。**按半个数组解析出来的结论比没有更危险**：条目不全却看起来是
    一份完整清单。所以这里只抢正文，结构化结论该没有还是没有，降级标签照旧。
    """
    content = _THINK_BLOCK_RE.sub("", str(text or ""))
    opening = _REPORT_MARKDOWN_OPENING_RE.search(content)
    if opening is None:
        return None
    value, position = _read_json_string(content, opening.end())
    if value is None or not value.strip():
        return None
    parts = [value]
    while True:
        separator = _CONTINUATION_RE.match(content, position)
        if separator is None:
            break
        chunk, next_position = _read_json_string(content, separator.end())
        if chunk is None or len(chunk) < _CONTINUATION_CHUNK_MIN:
            break
        # 切点落在哪都是可能的，但**标题必须留在行首**：下游按 `^# ` 切章节，把
        # `# 变更内容摘要` 粘在上一段的句尾会让那一节整个丢掉。
        if chunk.startswith("#") and not parts[-1].endswith("\n"):
            parts.append("\n\n")
        parts.append(chunk)
        position = next_position
    return "".join(parts)


def looks_like_truncated_json(text: str) -> bool:
    """回答「这段文本是不是一份没收尾的 JSON 对象」。

    判据是**括号没配平**（跳过字符串内部）：以 `{` 开头、扫到文本结束深度仍大于 0。
    为什么不只看「结尾不是 `}`」：模型常把 JSON 写完后再补一句说明（`{…}\\n以上。`），
    那种回答是**完整**的、只是不合协议，该给的提示是「你没按协议」，而不是「你写太长了」
    —— 两者要模型做的事正好相反。

    这段文本是**被截断**还是**不完整**，配合 `salvage_report_markdown` 一起用：抢得到
    `report_markdown` 说明断点在正文之后，抢不到（例如它在要上下文的半截被切断）也照样
    是截断 —— 那时同样该叫它压短，而不是叫它改格式。
    """
    content = _THINK_BLOCK_RE.sub("", str(text or "")).strip()
    if not content.startswith("{"):
        return False
    depth = 0
    in_string = False
    escaped = False
    for char in content:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return depth > 0


def build_correction_hint(
    error: Exception, *, dimension_ids: Iterable[str] = DIMENSION_IDS
) -> str:
    """把协议错误转成下一轮要说给模型听的话。

    ## 为什么这句里不再有「N 个维度」

    这里写过 `len(DIMENSION_IDS)`，也就是**平台出厂**的维度数。可维度清单是项目可声明的
    （`LoadedSkills.dimensions`，见 `skill_contract.render_dimension_section`），而这一层
    拿不到它 —— 一个声明了 12 个维度的项目，模型从提示词里读到 12 个，纠正提示却说
    「9 个维度都要写」：**校验按 A、提示词按 B**，而它既不会报错、也没人会去数。

    所以这句话改成**指回模型手里的那份清单**（系统提示词里那一节），不写任何数字。
    模型看不到清单时（例如清单就是出厂默认那九个，正文里逐条展开了）它照样知道要写几个。

    ## `dimension_ids`：清单与出厂默认不同时，把 id 逐个列出来

    与出厂默认相同时**一个字节都不加** —— 那九个 id 已经在 SKILL.md 正文里逐条展开过，
    再抄一遍只是白花提示词预算（而默认行为逐字不变是这个仓库的硬要求）。

    与出厂默认不同时，模型手里那份清单在正文末尾的追加节里，而这一轮它是**被纠正**的
    一轮 —— 把 id 逐个点名写进这句话，它就不必回去翻那一节才知道自己该写哪几个
    （`report` 里已经出现过「模型按别的清单写」的真实故障形态）。**仍然只列 id、不写
    条数**：条数一旦写死就会与清单漂移，而漂移不会报错（见上面那段）。
    """
    ids = tuple(str(item).strip() for item in dimension_ids if str(item or "").strip())
    declared = ""
    if ids and ids != DIMENSION_IDS:
        declared = "\n本次生效的维度清单是：" + "、".join(f"`{item}`" for item in ids) + "。"
    return (
        f"你上一轮的返回不符合协议：{error}。\n"
        "请严格修正后重新返回：\n"
        "1. 只返回一个可被 json.loads 解析的 JSON 对象；\n"
        "2. 不要输出 <think> 块、不要用代码围栏包住 JSON、不要写 JSON 之外的说明文字；\n"
        "3. status 只能是 need_more_context 或 final；\n"
        "4. final 必须同时给出非空的 report_markdown 和 dimensions"
        "（系统提示词里那份**本项目适用的维度清单**上的每一个维度都要写，"
        "未命中的写 hit 为 false 并说明理由）；\n"
        "5. 所有自然语言内容使用中文。"
        f"{declared}"
    )


# 三份收敛指令（额度耗尽 / 没有额度 / 最后一轮）共用的尾巴，**必须逐字相同**：
# 它管的是**交回来的形态** —— 证据不足的维度要按协议写 hit=false，而不是整段省掉。
# 以前「预算耗尽」那段文案在 prompt 与 protocol 里各有一份、两句话不一样，下场是改一处
# 漏一处；所以这里只留一份常量。
_CONVERGE_TAIL = (
    "对于证据不足的维度，在 dimensions 里写 hit 为 false "
    "并在 note 里说明「信息不足」，同时在报告里标注信息缺口。"
)


def build_budget_exhausted_hint(*, requests_total: int | None = None) -> str:
    """**上下文索取额度**耗尽时注入的收敛指令。

    **不报错退出**——模型手上已有的证据通常够写一份报告了，强制它收敛比作废整轮好。

    ## `requests_total == 0` 要换一句话

    「**用完**了」与「**从来没有**」在模型那里会长成同一句话，而模型会把这句话原样转述
    进报告的信息缺口。项目把上限配成 0 时，它写出来的是「额度用完」，用户据此去查额度
    怎么会被用完 —— 查不到，因为那是配置。所以 0 这一支明说「没有配置额度」。

    **只管索取额度，不管轮次** —— 轮次先耗尽的那一种见 `build_final_round_hint`。
    """
    if requests_total == 0:
        opening = (
            "本次分析**没有配置上下文索取额度**（上限 0 次），读不到任何文件内容、"
            "也做不了跨文件检索。请基于当前已有的证据直接输出 final，禁止请求上下文。"
        )
    else:
        opening = (
            "补充上下文的预算已耗尽。请基于当前已有的证据直接输出 final，"
            "禁止继续请求上下文。"
        )
    return opening + _CONVERGE_TAIL


def build_markdown_reemit_hint() -> str:
    """把上一条 markdown 报告**原样**转成协议 JSON 的纠正提示。

    引擎原先遇到「模型给了 markdown 而不是 JSON」是直接收工的：正文留下来，结构化结论
    整份放弃 —— 哪怕还剩三轮、纠正额度一次没用（实测 run 10 的 S3 跑满 8 轮后写成
    markdown，它负责的 code_logic / version_branch / process 三个维度**一条结构化结论都
    没进清单**；run 6 也出过同一形态）。而「把上一条原样转成 JSON」比「重新写一份报告」
    容易得多：内容已经在对话里，模型只需要换一种包装。

    所以措辞的重心是**不许借机重写**（新增/省略/改写都会让已经写实的证据变形），以及
    只做格式转换、不要再索取上下文。
    """
    return (
        "你上一条回答是一份 **markdown 报告**，而协议要求的是 JSON。"
        "请把**上一条的内容原样**转成协议 JSON（`status` 为 `final`）：结论、证据、影响、"
        "建议都要**逐条搬过来**，不要新增、不要省略、不要趁这次重写或合并。"
        "只输出这个 JSON，不要再索取上下文。" + _CONVERGE_TAIL
    )


def build_final_round_hint(*, round_index: int, max_rounds: int) -> str:
    """**最后一轮**的收敛指令（轮次先耗尽的那一条路）。

    只按索取额度判「该收尾了」是不够的：额度没花完、轮次先到顶，模型就完全不知道
    这是最后一条消息 —— 实测 run 10 的 S3 跑满 8 轮（只用了 38/40 次索取）之后写了一段
    markdown 叙述，而不是协议 JSON：它负责的三个维度（`code_logic` / `version_branch` /
    `process`）**一条结构化结论都没有**，报告里只能按「跑完了但结论没交回」标出来
    （见 `family_ledger._shard_gap_lines`）。同一形态在 run 6 已经出过一次，那次是 5 轮。

    ## 为什么要把机制说给模型听

    模型对「还剩 2 次索取」是有判断力的：不说清，它就会把这 2 次花掉。而**最后一轮索取
    回来的内容要等下一轮才会送到它手上，下一轮不存在** —— 那些内容平台照样去取，只是
    永远到不了模型眼前。所以这里不是客套地让它「收尾」，而是告诉它这件事实：现在索取
    等于白花一轮，且这一轮之后没有任何机会再交结论。
    """
    return (
        f"**这是本次分析的最后一条消息（第 {round_index}/{max_rounds} 轮）。**"
        "你这一轮索取回来的内容要等**下一轮**才会送到你手上，而下一轮不存在 ——"
        "现在再索取等于把这一轮白白花掉：平台会去取，但你永远看不到。"
        "所以本轮必须直接输出 final（按协议给 JSON），不要写成 markdown 叙述。"
        + _CONVERGE_TAIL
    )
