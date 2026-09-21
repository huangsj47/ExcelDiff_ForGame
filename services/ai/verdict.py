"""复核裁决：把「找反证」的结果落到**结论本身**上，而不只是追加一段文字。

## 这一层修的是什么

对账轮（面板上的 `V1`）跑完之后的产出，此前**只被追加成报告末尾的一段文字**：

* 它在正文里写「反证成立…建议由 critical **降级**」「反证成立…建议**撤掉**」，而平台的
  结论清单一个字都没改 —— 实测那次（run 12）异常表里 `id=109/110` 仍旧是
  `critical` / `very_high` / `pending`，而**同一份报告**的正文风险清单里还写着
  「（critical，**仍成立**）」。矛盾在**同一个字段内部**，而且这几行还会作为下一轮的
  基线（skill 定死的语义：上一次那批 = 「这个版本当前仍成立的问题全集」）继续传下去。
* 它按任务书要求在 `anomalies` 里报出的**新发现被整条丢掉**（连「未归类」那一节都进不去），
  而它的 `dropped` 反而会被并进来 —— 「静默吃掉一条发现」正是这套结构最该避免的失真。

所以这里做两件事，都做成**纯函数**（不碰数据库、不碰 Flask、不看时间、不看随机数）：

1. **裁决 → 结论**。主结论里每条发现有一个稳定编号（`F1`…，见 `assign_findings`），
   对账轮交出**结构化裁决**（`confirmed` / `downgraded` / `retracted` /
   `needs_more_evidence`），本模块按编号逐条应用，产出唯一的 `final_findings`：
   保留（附复核证据）、降级（写明新等级）、撤销（移出当前清单、留进审计轨迹）、
   证据不足（不维持 `very_high`，转「待人工核验」）。
2. **新发现 → 结论**。对账轮新报的条目走与主结论同一道校验（结构、重复、条数上限）
   之后合入；**不满足的按「被拒」如实记账**，不许静默消失。

## 缺裁决时按原样采信，而且明说

对账轮没跑成、或者跑成了但没给出结构化裁决时，**一条结论都不改**，并在报告里写明
「本次复核没有给可逐条应用的裁决」。反过来做（没裁决就当反证成立）会把「复核没跑」
变成「结论被撤销」—— 与 `result_payload` 那句「没有结论时按变更规模定级，并明说不是
模型结论」是同一条口径：不能拿一个不存在的结论去动真实结论。

## 两条「不许静默」的判据

* **撤销要有代价**：`retracted` 必须给出 `reason` 或 `evidence_refs`，两者都没有时平台
  **不采信**（按 `needs_more_evidence` 处理并写明原因）。任务书里那句「反证必须落到具体
  位置」是这条判据的出处 —— 一个词的答复不足以让一条结论消失。
* **降级要有新等级**：`downgraded` 必须给出比原等级**更低**的合法等级，否则同样按
  `needs_more_evidence` 处理。**绝不许因为「裁决不完整」就退回原样采信** —— 那正是
  「V1 说降级、清单里仍是 critical」这条缺陷的复现路径。

## 裁决从哪儿读回来

对账轮按 `verdict_instructions` 的形状在 `report_markdown` 里给一个 json 代码块；平台用
**自己那套容错解析**读它（`protocol.parse_json_candidates`：剥推理块、剥围栏、首尾大括号
切片）—— 与读模型其它结构化输出是同一套口径。读回来的裁决还会被平台**重新渲染**成报告里
的「## 复核裁决（平台）」一节：那一节是与正文并列的最终口径。

reducer 的产出另外写回一个**机器可读块**（报告末尾的一行 HTML 注释，见 `ruling_block`），
`result_payload` 从那里把它取回来放进结论载荷 —— 于是 **markdown、落库的值、读侧、导出
全部从 `final_findings` 渲染**，模型写的正文不再是独立真相源（它仍然留在报告里，但被那一节
明确降级为「原文附在后面」）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence

from services.ai.budget import truncate_text
from services.ai.protocol import Anomaly, DroppedItem, parse_json_candidates
from services.ai.rules import (
    DEFAULT_MAX_ANOMALIES,
    SEVERITY_RANK,
    anomaly_fingerprint,
    is_probable_duplicate,
    rank_anomalies,
)

# --------------------------------------------------------------------------
# 裁决码与文案
# --------------------------------------------------------------------------

VERDICT_CONFIRMED = "confirmed"
VERDICT_DOWNGRADED = "downgraded"
VERDICT_RETRACTED = "retracted"
VERDICT_NEEDS_MORE_EVIDENCE = "needs_more_evidence"
VERDICTS = (
    VERDICT_CONFIRMED,
    VERDICT_DOWNGRADED,
    VERDICT_RETRACTED,
    VERDICT_NEEDS_MORE_EVIDENCE,
)
VERDICT_LABELS = {
    VERDICT_CONFIRMED: "反证不成立（维持原结论）",
    VERDICT_DOWNGRADED: "反证部分成立（降级）",
    VERDICT_RETRACTED: "反证成立（撤销）",
    VERDICT_NEEDS_MORE_EVIDENCE: "证据不足（待人工核验）",
}
# 模型偶尔会把裁决码写成中文/动词形态（提示词里给的是英文码）。**只认明确的那几个**：
# 多认一个模糊说法就等于多一个「猜」的分支，而猜错的代价是结论被误撤销。
_VERDICT_ALIASES = {
    "confirm": VERDICT_CONFIRMED,
    "确认": VERDICT_CONFIRMED,
    "成立": VERDICT_CONFIRMED,
    "downgrade": VERDICT_DOWNGRADED,
    "降级": VERDICT_DOWNGRADED,
    "retract": VERDICT_RETRACTED,
    "撤销": VERDICT_RETRACTED,
    "撤掉": VERDICT_RETRACTED,
    "more_evidence": VERDICT_NEEDS_MORE_EVIDENCE,
    "needs_evidence": VERDICT_NEEDS_MORE_EVIDENCE,
    "证据不足": VERDICT_NEEDS_MORE_EVIDENCE,
    "待人工核验": VERDICT_NEEDS_MORE_EVIDENCE,
}

# 没被复核覆盖到的条目（对账轮只核对最严重的几条）与它的中文说法。
UNREVIEWED = ""
UNREVIEWED_LABEL = "未复核"

# 一条发现的来源：主结论 还是 对账轮新发现。
SOURCE_SYNTHESIS = "synthesis"
SOURCE_VERIFY = "verify"

# 对账轮新发现的编号前缀。**必须与 `family_ledger.VERIFY_LABEL` 一致**（对账轮在面板上
# 的标签就是它，有测试钉着这两个值相等）：报告里两个地方用两个叫法，读的人会以为是两件事。
NEW_FINDING_PREFIX = "V1"

# 平台记账用的 `DroppedItem.kind`。与 `anomaly` / `anomaly_cap` / `subagent` / `unclassified`
# 并列：**这些条目的去向是「被复核裁掉或被平台校验拒收」**，与「模型没报」不是一回事。
KIND_VERIFY = "verify"

# 报告里那一节的标题与机器可读块的标记。
RULING_TITLE = "## 复核裁决（平台）"
RULING_BLOCK_MARKER = "ai-verify-ruling"

# 报告里每条理由/依据占的字符上限。裁决是模型写的，长度不受控 —— 一段几千字的「理由」
# 会把报告正文挤掉，而它要说的其实一句话就够。
_REASON_MAX_CHARS = 400
_REFS_MAX_ITEMS = 5


# --------------------------------------------------------------------------
# 数据
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """进对账轮任务书的一条待核对结论：**平台发的编号**，模型按它回答。"""

    finding_id: str
    position: int
    anomaly: Anomaly


@dataclass(frozen=True)
class VerifyVerdict:
    """对账轮给出的一条结构化裁决。"""

    finding_id: str
    verdict: str
    final_severity: str = ""
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class FindingRow:
    """`final_findings` 里的一行：原结论 + 裁决 + 裁决之后真正生效的那一条。"""

    finding_id: str
    # 裁决**之后**的结论：`severity` / `confidence` 就是落库与界面要用的值。
    anomaly: Anomaly
    # 裁决**之前**的原样（审计轨迹要能回答「原来报的是什么等级」）。
    origin: Anomaly
    source: str = SOURCE_SYNTHESIS
    verdict: str = UNREVIEWED
    reason: str = ""
    evidence_refs: tuple[str, ...] = ()
    # 平台自己写的一句（裁决不完整、被降格处理等原因）。与 `reason`（模型写的理由）分开：
    # 两者的作者不同，读的人需要分得清哪一句是模型说的。
    note: str = ""

    @property
    def active(self) -> bool:
        return self.verdict != VERDICT_RETRACTED

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, UNREVIEWED_LABEL)

    @property
    def fingerprint(self) -> str:
        return anomaly_fingerprint(self.origin)

    @property
    def level_changed(self) -> bool:
        return (
            self.anomaly.severity != self.origin.severity
            or self.anomaly.confidence != self.origin.confidence
        )

    def as_dict(self) -> dict:
        """机器可读形态（进报告那一行注释、也进结论载荷的 `final_findings`）。"""
        return {
            "finding_id": self.finding_id,
            "source": self.source,
            "verdict": self.verdict,
            "verdict_label": self.verdict_label,
            "active": self.active,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "note": self.note,
            "fingerprint": self.fingerprint,
            "title": self.origin.title,
            "category": self.origin.category,
            "file_path": self.origin.file_path,
            "commit_ref": self.origin.commit,
            "severity": self.anomaly.severity,
            "confidence": self.anomaly.confidence,
            "original_severity": self.origin.severity,
            "original_confidence": self.origin.confidence,
        }


@dataclass(frozen=True)
class Reduction:
    """一次复核的**全部**结果：活动的 `final_findings` + 审计轨迹 + 平台记账。"""

    rows: tuple[FindingRow, ...] = ()
    # 被拒的条目（缺证据的新发现、与已有结论重复的新发现、超出条数上限的、无法对应的裁决）。
    # 它们是**记账**，不是「没发生」——报告里逐条列出来。
    rejected: tuple[DroppedItem, ...] = ()
    # 收到几条结构化裁决。0 表示这一轮**没有可逐条应用的东西**（报告里要明说）。
    verdicts_seen: int = 0

    @property
    def active(self) -> tuple[FindingRow, ...]:
        return tuple(row for row in self.rows if row.active)

    @property
    def retracted(self) -> tuple[FindingRow, ...]:
        return tuple(row for row in self.rows if not row.active)

    @property
    def unreviewed(self) -> tuple[FindingRow, ...]:
        return tuple(row for row in self.rows if row.verdict == UNREVIEWED)

    @property
    def new_findings(self) -> tuple[FindingRow, ...]:
        """对账轮新发现、且已经合入清单的那几条。"""
        return tuple(row for row in self.rows if row.source == SOURCE_VERIFY)

    @property
    def changed(self) -> bool:
        """裁决或新发现有没有对结论产生任何影响。

        决定要不要往报告里加那一节：**没有影响时一个字都不加**（默认行为逐字不变），
        有影响时那一节就是「报告为什么与模型正文不一致」的出处。
        """
        return bool(self.verdicts_seen or self.new_findings or self.rejected)

    def active_anomalies(self) -> tuple[Anomaly, ...]:
        """落库与界面要用的那一份（`outcome.anomalies` 就取它）。"""
        return tuple(row.anomaly for row in self.active)

    def by_fingerprint(self) -> dict[str, FindingRow]:
        return {row.fingerprint: row for row in self.rows}

    def as_dict(self) -> dict:
        return {
            "verdicts_seen": int(self.verdicts_seen),
            "rows": [row.as_dict() for row in self.rows],
            "rejected": [
                {"reason": item.reason, "detail": item.detail} for item in self.rejected
            ],
        }


# --------------------------------------------------------------------------
# 编号
# --------------------------------------------------------------------------


def assign_findings(anomalies: Sequence[Anomaly]) -> tuple[Finding, ...]:
    """给主结论里每条发现发一个**稳定编号**（`F1`…`Fn`）。

    次序与报告里那份清单完全一致（`rules.rank_anomalies`：严重度 → 置信度，同档保持
    模型给的顺序）—— 任务书里列的「最严重的几条」用的也是这一套排序，两处各排一次
    迟早会对不上编号（对不上就意味着**裁决打到了另一条结论上**）。
    """
    ordered = rank_anomalies(list(enumerate(anomalies)))
    return tuple(
        Finding(finding_id=f"F{position}", position=index, anomaly=anomaly)
        for position, (index, anomaly) in enumerate(ordered, start=1)
    )


def _new_finding_id(index: int) -> str:
    return f"{NEW_FINDING_PREFIX}-{index}"


# --------------------------------------------------------------------------
# 读裁决
# --------------------------------------------------------------------------


def verdict_block_of(markdown: str) -> dict | None:
    """从一段文本里取出裁决块（`{"verdicts": [...]}`）。取不到返回 `None`。

    用平台自己的容错解析（`protocol.parse_json_candidates`）：模型可能把 json 包在围栏里、
    前面写一段说明、或者带上推理块，这些都已经在那一层处理过。**只认带 `verdicts` 数组的
    那个对象** —— 报告里可能还有别的 json（模型自己贴的配置片段），不能误读。
    """
    for value in parse_json_candidates(markdown or ""):
        if isinstance(value, dict) and isinstance(value.get("verdicts"), list):
            return value
    return None


def parse_verdicts(markdown: str) -> tuple[VerifyVerdict, ...]:
    """读出对账轮的结构化裁决（**读不出来就是没有**，不猜、不推断）。

    编号认两种写法：平台发的 `F2`，以及**裸写序号** `2`。后者是安全的：任务书里那几条
    的序号与 `F` 编号是同一个数（`assign_findings` 的次序就是任务书里列的次序），
    而平台发出的编号只有审查过的那几条 —— 裸数字指不到审查范围之外。
    """
    block = verdict_block_of(markdown)
    if block is None:
        return ()
    verdicts: list[VerifyVerdict] = []
    for entry in block.get("verdicts") or []:
        if not isinstance(entry, dict):
            continue
        finding_id = _as_finding_id(entry.get("finding_id") or entry.get("id"))
        verdict = _normalize_verdict(entry.get("verdict"))
        if not finding_id or not verdict:
            continue
        verdicts.append(
            VerifyVerdict(
                finding_id=finding_id,
                verdict=verdict,
                final_severity=_as_severity(entry.get("final_severity")),
                reason=_as_text(entry.get("reason")),
                evidence_refs=_as_refs(entry.get("evidence_refs")),
            )
        )
    return tuple(verdicts)


def _as_finding_id(value: Any) -> str:
    text = str(value or "").strip()
    if text.isdigit():
        return f"F{int(text)}"
    return text.upper()


_FENCED_BLOCK_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def strip_verdict_block(markdown: str) -> str:
    """把模型写的那一段裁决块从正文里去掉。

    内容已经由平台渲染成「复核裁决（平台）」那一节（而且是**按裁决结果**渲染的），
    原样留着只会让同一件事在报告里出现两遍，其中一遍还是未加工的 json。
    只删**认得出来的**那一个块（`verdict_block_of` 认得出才算），认不出来时正文一个字不动。

    两种形态都要能删：围栏里的（任务书要求的那种）与裸写在正文里的。**只删那一个块的
    字节区间** —— 按「第一个 `{` 到最后一个 `}`」切会把模型写在后面的正文一起删掉
    （它会引用配置片段、写花括号），那种损失是静默的。
    """
    raw = markdown or ""
    if verdict_block_of(raw) is None:
        return raw
    for matched in _FENCED_BLOCK_RE.finditer(raw):
        if _is_verdict_object(matched.group(1)):
            return (raw[: matched.start()] + raw[matched.end():]).strip()
    span = _bare_verdict_span(raw)
    if span is None:
        return raw.strip()
    return (raw[: span[0]] + raw[span[1]:]).strip()


def _is_verdict_object(text: str) -> bool:
    try:
        value = json.loads(str(text or "").strip())
    except ValueError:
        return False
    return isinstance(value, dict) and isinstance(value.get("verdicts"), list)


def _bare_verdict_span(raw: str) -> tuple[int, int] | None:
    """裸写的裁决对象在原文里的起止（找不到就是 `None`）。

    用 `json.JSONDecoder.raw_decode` 逐个 `{` 试 —— 试出**真能解析成对象、且带
    `verdicts` 数组**的那个，而不是「第一对花括号」。
    """
    decoder = json.JSONDecoder()
    for position, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(raw, position)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("verdicts"), list):
            return position, end
    return None


def _normalize_verdict(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in VERDICTS:
        return text
    return _VERDICT_ALIASES.get(text, "")


def _as_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in SEVERITY_RANK else ""


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item).strip() for item in value if str(item or "").strip())
    return str(value or "").strip()


def _as_refs(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return ()
    return tuple(
        text for text in (str(item).strip() for item in items[:_REFS_MAX_ITEMS]) if text
    )


# --------------------------------------------------------------------------
# reducer
# --------------------------------------------------------------------------


def reduce_findings(
    base: Sequence[Anomaly],
    *,
    new: Sequence[Anomaly] = (),
    verdicts: Sequence[VerifyVerdict] = (),
    limit: int = DEFAULT_MAX_ANOMALIES,
    source_label: str = NEW_FINDING_PREFIX,
) -> Reduction:
    """把「主结论 + 对账轮新发现 + 结构化裁决」压成唯一的一份结果。**确定性**。

    参数 `base` 是主结论清单（`synthesis.anomalies`），`new` 是对账轮自己报出来的
    （`outcome.anomalies`）。两者都已经过引擎那一侧的门槛与去重，这里再做的是**跨来源**
    的那几件事：重复、条数上限、以及按裁决决定去留。
    """
    rows = [_row_of(finding, verdicts) for finding in assign_findings(base)]
    rejected: list[DroppedItem] = []
    matched_ids: set[str] = set()
    known = {row.finding_id for row in rows}

    for verdict in verdicts:
        if verdict.finding_id in known:
            matched_ids.add(verdict.finding_id)
        else:
            # 一条裁决指向不存在的编号：不能悄悄丢掉（那会让「复核说了话」与「结论没动」
            # 同时成立却无人知道），如实记一笔。
            rejected.append(
                DroppedItem(
                    KIND_VERIFY,
                    0,
                    "复核裁决指向的编号找不到对应结论，平台未应用",
                    f"[{verdict.finding_id}] {VERDICT_LABELS.get(verdict.verdict, verdict.verdict)}",
                )
            )

    for index, anomaly in enumerate(new, start=1):
        finding_id = _new_finding_id(index)
        duplicate = _duplicate_of(anomaly, rows)
        if not anomaly.title.strip() or not anomaly.evidence:
            rejected.append(
                DroppedItem(
                    KIND_VERIFY,
                    index,
                    "对账轮新发现的条目缺标题或证据，平台不采信（与主结论同一道校验）",
                    f"[{finding_id}] {anomaly.title or '（无标题）'}",
                )
            )
            continue
        if duplicate is not None:
            reason = (
                f"与**刚被复核撤销**的 [{duplicate.finding_id}] 是同一个问题，"
                "需要人工判一下到底撤不撤"
                if not duplicate.active
                else f"与清单里已有的 [{duplicate.finding_id}] 重复"
            )
            rejected.append(
                DroppedItem(
                    KIND_VERIFY,
                    index,
                    reason,
                    f"[{finding_id}] {anomaly.title}",
                )
            )
            continue
        rows.append(
            FindingRow(
                finding_id=finding_id,
                anomaly=anomaly,
                origin=anomaly,
                source=SOURCE_VERIFY,
                note=f"由对账轮（{source_label}）在找反证的过程中报出",
            )
        )

    if any(row.source == SOURCE_VERIFY for row in rows):
        # 有合入时才重排：新发现可能比原有几条更严重，而清单的次序是「严重度优先」
        # （`rules.cap_anomalies` 的契约）。**没有任何合入时次序逐字不变** —— 那条路径
        # 处处都有测试钉着，改成「一律重排」会让默认行为产生看不出来的漂移。
        # `rank_anomalies` 收的是 (下标, 结论)、还回来的也是这两样，所以按下标取回行。
        rows = [
            rows[index] for index, _anomaly in rank_anomalies(
                list(enumerate(row.anomaly for row in rows))
            )
        ]

    return Reduction(
        rows=_apply_limit(rows, limit=limit, rejected=rejected),
        rejected=tuple(rejected),
        verdicts_seen=len(matched_ids),
    )


def _row_of(finding: Finding, verdicts: Sequence[VerifyVerdict]) -> FindingRow:
    """一条主结论 + 它收到的那条裁决（没收到就是「未复核」）。**取第一条，多余的记账。**"""
    verdict = next(
        (item for item in verdicts if item.finding_id == finding.finding_id), None
    )
    origin = finding.anomaly
    if verdict is None:
        return FindingRow(finding_id=finding.finding_id, anomaly=origin, origin=origin)
    return _apply_verdict(finding.finding_id, origin, verdict)


def _apply_verdict(finding_id: str, origin: Anomaly, verdict: VerifyVerdict) -> FindingRow:
    """把一条裁决落到结论上。**不完整或不可信的裁决一律降格为「证据不足」**。

    降格而不是「按原样采信」：后者正是这条缺陷的复现路径（模型说了降级/撤销，清单里
    仍是 `critical`）。降格之后平台至少不再按 `very_high` 采信它，并在报告里写明原因。
    """
    reason = verdict.reason
    refs = verdict.evidence_refs
    if verdict.verdict == VERDICT_CONFIRMED:
        return FindingRow(
            finding_id=finding_id,
            anomaly=origin,
            origin=origin,
            verdict=VERDICT_CONFIRMED,
            reason=reason,
            evidence_refs=refs,
        )

    if verdict.verdict == VERDICT_RETRACTED:
        if reason or refs:
            return FindingRow(
                finding_id=finding_id,
                anomaly=origin,
                origin=origin,
                verdict=VERDICT_RETRACTED,
                reason=reason,
                evidence_refs=refs,
            )
        return _needs_more(
            finding_id,
            origin,
            reason,
            refs,
            note="复核给了「撤销」，但既没写理由也没给依据（任务书要求反证落到具体位置）；"
            "平台按「证据不足」处理，请人工核验后再决定撤不撤",
        )

    if verdict.verdict == VERDICT_DOWNGRADED:
        target = verdict.final_severity
        lowered = target and SEVERITY_RANK.get(target, 0) < SEVERITY_RANK.get(origin.severity, 0)
        if lowered:
            return FindingRow(
                finding_id=finding_id,
                anomaly=replace(origin, severity=target),
                origin=origin,
                verdict=VERDICT_DOWNGRADED,
                reason=reason,
                evidence_refs=refs,
            )
        return _needs_more(
            finding_id,
            origin,
            reason,
            refs,
            note=(
                f"复核认为应降级，但没有给出比 `{origin.severity}` 更低的合法等级"
                f"（给的是 `{target or '空'}`）；平台按「证据不足」处理，"
                "**不再按原等级的原置信度采信**"
            ),
        )

    # `needs_more_evidence`：保留在清单里，但**不得维持 very_high**（置信度是它进清单的
    # 门槛之一，维持 very_high 等于这条裁决在数据上不留任何痕迹），并注明待人工核验。
    return _needs_more(finding_id, origin, reason, refs, note="")


def _needs_more(
    finding_id: str,
    origin: Anomaly,
    reason: str,
    refs: tuple[str, ...],
    *,
    note: str,
) -> FindingRow:
    capped = "high" if origin.confidence == "very_high" else origin.confidence
    return FindingRow(
        finding_id=finding_id,
        anomaly=replace(origin, confidence=capped),
        origin=origin,
        verdict=VERDICT_NEEDS_MORE_EVIDENCE,
        reason=reason,
        evidence_refs=refs,
        note=note,
    )


def _duplicate_of(anomaly: Anomaly, rows: Sequence[FindingRow]) -> FindingRow | None:
    """这条新发现是不是已经有了（先比指纹，再按 `rules` 的近似判重回落一次）。"""
    fingerprint = anomaly_fingerprint(anomaly)
    for row in rows:
        if row.fingerprint == fingerprint:
            return row
    for row in rows:
        if is_probable_duplicate(row.origin, anomaly):
            return row
    return None


def _apply_limit(
    rows: list[FindingRow], *, limit: int, rejected: list[DroppedItem]
) -> tuple[FindingRow, ...]:
    """按条数上限收口。**超出的按严重度从低到高砍，并逐条记账。**

    次序沿用与引擎封顶同一套（`rules.rank_anomalies`）：两处各排一次，迟早会出现
    「平台砍掉的这条比留下的那条更严重」。砍下来的是**合入之后**才超限的那几条
    （主结论自己超限的那部分在引擎那一侧已经记过账了，见 `build_cap_section`）。
    """
    size = max(0, int(limit))
    if len(rows) <= size:
        return tuple(rows)
    # 排序传的是**结论**而不是行（`rules.rank_anomalies` 只看 severity / confidence），
    # 拿回来的下标才是行在 `rows` 里的位置。
    ranked = rank_anomalies(list(enumerate(row.anomaly for row in rows)))
    keep = {index for index, _ in ranked[:size]}
    kept: list[FindingRow] = []
    for index, row in enumerate(rows):
        if index in keep:
            kept.append(row)
            continue
        rejected.append(
            DroppedItem(
                KIND_VERIFY,
                index,
                f"超出本次条数上限（{size} 条），已按严重度优先保留",
                f"[{row.finding_id}] {row.anomaly.severity} {row.anomaly.title}",
            )
        )
    return tuple(kept)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

_NOT_APPLIED_SECTION = (
    RULING_TITLE
    + "\n\n"
    + "本次开着「对账轮（找反证）」，但它**没有给出可逐条应用的裁决**（按任务书要求，"
    "裁决要在 `report_markdown` 里单独给一个 json 代码块）。因此**本次复核对下面的结论"
    "一条都没有生效** —— 清单里这些条按原样采信，读的时候按未复核看；它的原文附在"
    "下面那一节里。\n"
)


def render_ruling(reduction: Reduction, *, review_ran: bool) -> str:
    """把复核结果渲染成报告里那一节。**从 `final_findings` 渲染，不是模型写的正文。**

    `review_ran` 为假（没开对账轮 / 它没跑成）时一个字都不渲染：没有复核就没有裁决，
    报告不该为此多出一节。
    """
    if not review_ran:
        return ""
    if not reduction.changed:
        return _NOT_APPLIED_SECTION

    lines: list[str] = [
        RULING_TITLE,
        "",
        "本节是平台按对账轮（找反证）的结构化裁决渲染的**最终口径**：裁决已经应用到下面的"
        "结论清单上（保留 / 降级 / 撤销 / 转人工核验），落库的异常、下一轮的基线、导出报告"
        "读的都是这一份。**报告正文里凡与本节不一致的地方（例如正文仍写着「（critical，"
        "仍成立）」），以本节为准** —— 正文是裁决之前的原文，平台不改模型写的那份稿子。",
        "",
    ]
    reviewed = len(reduction.rows) - len(reduction.unreviewed)
    lines.append(
        f"本次复核覆盖：主结论 {len(reduction.rows)} 条里给出裁决 {reviewed} 条"
        f"（对账轮只核对最严重的几条，其余按原样采信）。"
    )
    lines.append("")
    if not reduction.verdicts_seen:
        # 有影响但**一条裁决都没读到**：可能是它只报了新发现、也可能是它把裁决写成了
        # 正文里的一段话（那不是裁决）。这两种情况下「结论为什么没动」都得说明白，
        # 否则读的人会以为复核不生效是平台坏了。
        lines.append(
            "**注意**：本次复核**没有回结构化裁决**（正文里的话不构成裁决，平台只认那个 "
            "json 块），所以下面的结论一律按原样采信；只有它新报出来的条目被合入了清单。"
        )
        lines.append("")

    retracted = reduction.retracted
    if retracted:
        lines.append(f"### 已撤销 {len(retracted)} 条（移出当前结论清单）")
        lines.append("")
        lines.append(
            "这几条**不进异常表、不进下一轮基线**（下一轮的基线语义是「上一次为止仍然成立的"
            "问题全集」）。原文与撤销理由保留在这一节里 —— 撤销本身也是结论，不能没有痕迹。"
        )
        lines.append("")
        for row in retracted:
            lines.append(_row_line(row))
        lines.append("")

    downgraded = tuple(row for row in reduction.rows if row.verdict == VERDICT_DOWNGRADED)
    if downgraded:
        lines.append(f"### 已降级 {len(downgraded)} 条（按新等级采信）")
        lines.append("")
        for row in downgraded:
            lines.append(_row_line(row))
        lines.append("")

    pending = tuple(
        row for row in reduction.rows if row.verdict == VERDICT_NEEDS_MORE_EVIDENCE
    )
    if pending:
        lines.append(f"### 待人工核验 {len(pending)} 条（证据不足）")
        lines.append("")
        lines.append(
            "这几条**仍在清单里**，但置信度不再按 `very_high` 采信 —— 请人工看一遍再决定"
            "处置。"
        )
        lines.append("")
        for row in pending:
            lines.append(_row_line(row))
        lines.append("")

    confirmed = tuple(row for row in reduction.rows if row.verdict == VERDICT_CONFIRMED)
    if confirmed:
        lines.append(f"### 反证不成立 {len(confirmed)} 条（维持原结论）")
        lines.append("")
        lines.append("有人去找过反证、没找到。找过哪里写在每一条的理由里。")
        lines.append("")
        for row in confirmed:
            lines.append(_row_line(row))
        lines.append("")

    new_findings = reduction.new_findings
    if new_findings:
        lines.append(f"### 对账轮新发现 {len(new_findings)} 条（已合入清单）")
        lines.append("")
        lines.append(
            "这几条是对账轮在找反证的过程中新报出来的，经与主结论同一道校验（结构、重复、"
            "条数上限）之后合入 —— 它们**不是**「找反证」的结果，是这一轮的附带产出。"
        )
        lines.append("")
        for row in new_findings:
            lines.append(_row_line(row))
        lines.append("")

    if reduction.rejected:
        lines.append(f"### 平台记账：{len(reduction.rejected)} 条没有进入清单")
        lines.append("")
        for item in reduction.rejected:
            detail = item.detail or "（未记标题）"
            lines.append(f"- {detail}：{item.reason}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _row_line(row: FindingRow) -> str:
    """一行的措辞。**原等级、裁决、处置三样都写出来** —— 只写裁决，读的人不知道
    「降级」是从哪一级降下来的。"""
    head = f"- **[{row.finding_id}] {row.origin.title}**："
    original = f"原 `{row.origin.severity}` / `{row.origin.confidence}`"
    if row.verdict == VERDICT_RETRACTED:
        action = "**反证成立（撤销）**，已从当前结论清单移除"
    elif row.verdict == VERDICT_DOWNGRADED:
        action = (
            f"**反证部分成立（降级）**：`{row.origin.severity}` → `{row.anomaly.severity}`"
        )
    elif row.verdict == VERDICT_NEEDS_MORE_EVIDENCE:
        action = "**证据不足（待人工核验）**"
        if row.level_changed:
            action += f"，置信度 `{row.origin.confidence}` → `{row.anomaly.confidence}`"
    else:
        action = f"**{row.verdict_label}**"
    if row.source == SOURCE_VERIFY:
        action += "（对账轮新发现）"
    detail = [f"{original} → {action}"]
    if row.reason:
        detail.append(f"理由：{truncate_text(row.reason, _REASON_MAX_CHARS)[0]}")
    if row.evidence_refs:
        detail.append("依据：" + "、".join(row.evidence_refs))
    if row.note:
        detail.append(f"平台说明：{row.note}")
    return head + "；".join(detail)


def ruling_block(reduction: Reduction) -> str:
    """机器可读块：报告末尾的一行 HTML 注释（markdown 渲染看不见，平台读得回）。

    为什么放在报告里而不是新加一个 `EngineOutcome` 字段：`EngineOutcome` 的字段是引擎
    那边的契约（本模块不拥有），而这一份**本来就是报告的一部分** —— 报告到哪儿它到哪儿，
    导出、历史回放、SSE 全都自动带上。`result_payload` 用 `read_ruling` 取回它。

    `-->` 会被转义：`reason` 是模型写的，里面出现一个 `-->` 就会把这个注释**提前关掉**，
    于是剩下的 json 变成正文里的一段乱码，而 `read_ruling` 读不回来。
    """
    if not reduction.changed:
        return ""
    payload = json.dumps(reduction.as_dict(), ensure_ascii=False, sort_keys=True)
    return f"<!-- {RULING_BLOCK_MARKER}: {payload.replace('-->', '--\\u003e')} -->"


_RULING_BLOCK_RE = re.compile(
    r"<!--\s*" + re.escape(RULING_BLOCK_MARKER) + r"\s*:\s*(\{.*?\})\s*-->", re.DOTALL
)


def strip_ruling_block(markdown: str) -> str:
    """去掉报告末尾那一行机器可读块（`ruling_block` 写进去的）。

    给**导出**用：那一行是给平台读的 HTML 注释（网页里渲染看不见），但导出的是原始
    markdown，读的人会看到一行 `<!-- ... -->`。导出那一侧只要在拼文档前调一次这个函数，
    报告里就只剩给人看的那几节。
    """
    return _RULING_BLOCK_RE.sub("", markdown or "").strip()


def read_ruling(markdown: str) -> dict | None:
    """从报告里取回机器可读的裁决（取不到返回 `None`）。

    取不到只可能是两件事：这次**根本没有复核**（单代理路径、没开对账轮），或者那次复核
    对结论**没有产生任何影响**。两种情况下读取侧都按「没有裁决」处理 —— 也就是今天的行为。
    """
    matched = _RULING_BLOCK_RE.search(markdown or "")
    if matched is None:
        return None
    try:
        value = json.loads(matched.group(1))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def retracted_fingerprints(ruling: dict | None) -> frozenset:
    """被撤销的那些条目的指纹。

    `result_payload` 用它把「待落库集合」再滤一道：撤销在 `aggregate_outcomes` 里已经
    生效（`outcome.anomalies` 里已经没有它们），但那是一处**约定**而不是一道闸门，
    多这一步，落库那一侧不必相信上游做对了。
    """
    if not ruling:
        return frozenset()
    return frozenset(
        str(row.get("fingerprint") or "")
        for row in ruling.get("rows") or ()
        if isinstance(row, dict) and not row.get("active") and row.get("fingerprint")
    )


def ruling_rows(ruling: dict | None, *, active: bool | None = None) -> tuple[dict, ...]:
    """裁决里的行（`active=True/False` 过滤；不传则全给）。"""
    if not ruling:
        return ()
    rows = tuple(row for row in ruling.get("rows") or () if isinstance(row, dict))
    if active is None:
        return rows
    return tuple(row for row in rows if bool(row.get("active")) is active)


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

_VERDICT_SCHEMA_SAMPLE = (
    '{"verdicts": [\n'
    '  {"finding_id": "F1", "verdict": "retracted", "reason": "<反证是什么：哪个文件哪一行>",\n'
    '   "evidence_refs": ["<文件:行>"]},\n'
    '  {"finding_id": "F2", "verdict": "downgraded", "final_severity": "high",\n'
    '   "reason": "<为什么不再是 critical>"},\n'
    '  {"finding_id": "F3", "verdict": "needs_more_evidence", "reason": "<还缺什么证据>"},\n'
    '  {"finding_id": "F4", "verdict": "confirmed", "reason": "<查了哪里，没找到反证>"}\n'
    "]}"
)


def verdict_instructions() -> str:
    """任务书里那一段「结构化裁决」（`subagent.build_verify_task` 拼进去）。

    **这一段是整条链路的前提**：平台只按这个 json 块决定某条结论是保留、降级还是撤销。
    所以它必须写清楚两件事：形状（照抄）、以及「没有这一块会发生什么」—— 后者不写，
    模型会以为正文那段话就够了（实测就是这么写的），而平台不会替它猜。
    """
    return (
        "## 结构化裁决（**必须给**，平台按它改结论）\n\n"
        "上面每一条都带一个编号（`F1`…）。除了写在正文里的那段话，**还要在 "
        "`report_markdown` 末尾单独给一个 json 代码块**，形如：\n\n"
        "```json\n" + _VERDICT_SCHEMA_SAMPLE + "\n```\n\n"
        "`verdict` 只能取这四个值：\n\n"
        "* `confirmed`：反证不成立，维持原结论；\n"
        "* `downgraded`：反证部分成立，**必须同时给 `final_severity`**（只允许 "
        "`critical` / `high`，且必须**低于**原等级）；\n"
        "* `retracted`：反证成立，这一条**从结论清单里撤掉**（不进异常表、不进下一轮基线）"
        "—— 必须给 `reason` 或 `evidence_refs`，两者都没有平台**不采信**"
        "（会按「证据不足」转人工核验）；\n"
        "* `needs_more_evidence`：证据不足，转「待人工核验」（平台不再按 `very_high` 采信）。\n\n"
        "**这一块是平台唯一的依据**：正文写得再明确，没有这一块，你这一轮复核对结论"
        "**一条都不生效**（平台不替你猜）。反过来，写了这一块它就会**直接改结论** ——"
        "所以只在反证真的成立时才写 `retracted` / `downgraded`。\n"
    )
