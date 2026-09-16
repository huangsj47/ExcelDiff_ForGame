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

    def describe(self) -> str:
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
    """被丢弃的条目及原因。会进 trace，便于解释「为什么少了几条」。"""

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
            )
        )
    return tuple(requests)


def _coerce_anomalies(value: Any) -> tuple[tuple[Anomaly, ...], tuple[DroppedItem, ...]]:
    if value is None:
        return (), ()
    if not isinstance(value, list):
        raise ProtocolError("anomalies 必须是数组")

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

        category = _as_str(entry.get("category"))
        if category not in DIMENSION_IDS:
            dropped.append(
                DroppedItem("anomaly", index, "category 不在允许集合内", category)
            )
            continue

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


def _coerce_dimensions(value: Any) -> tuple[tuple[DimensionReview, ...], tuple[DroppedItem, ...]]:
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
        if identifier not in DIMENSION_IDS:
            dropped.append(DroppedItem("dimension", index, "id 不在允许集合内", identifier))
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


def parse_payload(text: str) -> AnalysisPayload:
    """把模型回答解析成 `AnalysisPayload`。

    结构性错误抛 `ProtocolError`（编排层据此重问），条目级问题只丢弃并记账。
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
    anomalies, dropped_anomalies = _coerce_anomalies(raw.get("anomalies"))
    dimensions, dropped_dimensions = _coerce_dimensions(raw.get("dimensions"))

    if status == STATUS_FINAL:
        if not report_markdown:
            raise ProtocolError("status 为 final 时必须给出非空的 report_markdown")
        if not dimensions:
            # dimensions 是「六个维度都过了一遍」的证据。允许为空等于允许模型只挑
            # 好说的说，这正是它要防的事。
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


def sanitize_requests(
    requests: Iterable[ContextRequest], scope: AnalysisScope
) -> tuple[tuple[ContextRequest, ...], tuple[DroppedItem, ...]]:
    """工具白名单：把越权的上下文请求丢掉。

    这是「模型不能诱导服务端读任意文件」的落点。四重校验：类型在集合内、需要的字段
    齐全、commit 能解析到本批次、path 属于该 commit 改动过的文件。任一不满足就丢弃
    （不报错——模型偶尔写错一个字段不该作废整轮），并记账。
    """
    allowed: list[ContextRequest] = []
    dropped: list[DroppedItem] = []
    seen: set[tuple[str, str, str, str]] = set()

    for index, request in enumerate(requests):
        request_type = str(request.type or "").strip()
        if request_type not in REQUEST_TYPES:
            dropped.append(DroppedItem("request", index, "type 不在白名单内", request_type))
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

        resolved = scope.resolve_commit(request.commit)
        if resolved is None:
            dropped.append(
                DroppedItem("request", index, "commit 不属于本批次", request.commit)
            )
            continue

        if request_type in REQUEST_TYPES_NEEDING_PATH:
            if not scope.path_allowed(resolved, request.path):
                dropped.append(
                    DroppedItem("request", index, "path 不属于该 commit 改动的文件", request.path)
                )
                continue
            path = normalize_path(request.path)
        else:
            path = ""

        key = (request_type, resolved, path, "")
        if key in seen:
            continue  # 同一轮内重复索要不重复执行
        seen.add(key)
        allowed.append(ContextRequest(type=request_type, commit=resolved, path=path))

    return tuple(allowed), tuple(dropped)


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


def build_correction_hint(error: Exception) -> str:
    """把协议错误转成下一轮要说给模型听的话。"""
    return (
        f"你上一轮的返回不符合协议：{error}。\n"
        "请严格修正后重新返回：\n"
        "1. 只返回一个可被 json.loads 解析的 JSON 对象；\n"
        "2. 不要输出 <think> 块、不要用代码围栏包住 JSON、不要写 JSON 之外的说明文字；\n"
        "3. status 只能是 need_more_context 或 final；\n"
        "4. final 必须同时给出非空的 report_markdown 和 dimensions（六个维度都要写，"
        "未命中的写 hit 为 false 并说明理由）；\n"
        "5. 所有自然语言内容使用中文。"
    )


def build_budget_exhausted_hint() -> str:
    """预算耗尽时注入的收敛指令。

    **不报错退出**——模型手上已有的证据通常够写一份报告了，强制它收敛比作废整轮好。
    """
    return (
        "补充上下文的预算已耗尽。请基于当前已有的证据直接输出 final，"
        "禁止继续请求上下文。对于证据不足的维度，在 dimensions 里写 hit 为 false "
        "并在 note 里说明「信息不足」，同时在报告里标注信息缺口。"
    )
