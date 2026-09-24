#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁决这一层的**取值域、文案与数据结构** —— 从 `verdict.py` 搬出来的（2026-09-25）。

## 为什么单独一层

`verdict.py` 顶着仓库的 2000 行 ERROR 闸门（`scripts/check_file_length.py`），而**渲染那一半**
（`verdict_render.py`）要在运行时读这些取值域常量。常量若留在 `verdict.py` 里，两个模块就会互相
导入 —— 那时谁先被导入都会炸（`from X import name` 打在半初始化的模块上）。放到底层就没有这个
问题：依赖方向单一，`verdict_types` ← `verdict` ← `verdict_render`，任何导入顺序都成立。

## 这里放什么、不放什么

* **放**：裁决码与中文标签、节标题、几条判参常量，以及四个数据类
  （`Finding` / `VerifyVerdict` / `FindingRow` / `Reduction`）。
* **不放**：需要判断的逻辑。唯一的函数是 `severity_step_down` —— 它是查表
  （`SEVERITY_STEP_DOWN`）的薄封装，与那张表是同一件事，拆开只会让人两头找。

## 搬了文件不等于改了名字

读侧与测试此前按 `services.ai.verdict.X` 取用，`verdict.py` **逐个回导**保住那些说法
（见那个文件底部的 `/ 回导` 注释块）。**不要**因为「东西在这个模块里」就去改调用方的导入点。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from services.ai.claims import (
    CLAIM_REFUTED,
    CLAIM_UNREADABLE,
    CLAIM_UNVERIFIED,
    CLAIM_VERIFIED,
    VERIFY_BASIS_LABELS,
    VERIFY_BASIS_NONE,
    ClaimReview,
    ClaimVerdict,
)
from services.ai.protocol import Anomaly, DroppedItem
from services.ai.rules import anomaly_fingerprint

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

# --------------------------------------------------------------------------
# 逐条断言的裁决（P0-01）
# --------------------------------------------------------------------------
#
# 判据与措辞都在 `services/ai/claims.py`：那里是纯函数，三个渲染点（裁决节 / 异常面板 /
# 导出文档）读同一份。本模块只负责把它算出来的结果**落到行上**（`_apply_claims`）与渲染。
#
# 一条结论常常是**复合断言**（「取档失败路径改为断言中断进程」里至少含两个可分别证实的
# 事实），而裁决原先只能落在整条上。实测 run 58 的 F3 因此出现了最坏的那种组合：复核轮在
# 理由里**自己写着**「`assert(false)` 是中断整个进程还是仅中断本次登录请求，未能核实」，
# 整条却仍是 `critical` + `confirmed`。根因不是复核不诚实，是**粒度**。
# 现在每条结论带 `claims[]`（`protocol.Claim`），复核轮逐条回答，平台逐条记账。

# 一条发现的来源：主结论 还是 对账轮新发现。
SOURCE_SYNTHESIS = "synthesis"
SOURCE_VERIFY = "verify"

# 对账轮新发现的编号前缀。**必须与 `family_ledger.VERIFY_LABEL` 一致**（对账轮在面板上
# 的标签就是它，有测试钉着这两个值相等）：报告里两个地方用两个叫法，读的人会以为是两件事。
NEW_FINDING_PREFIX = "V1"

# 平台记账用的 `DroppedItem.kind`。与 `anomaly` / `anomaly_cap` / `subagent` / `unclassified`
# 并列：**这些条目的去向是「被复核裁掉或被平台校验拒收」**，与「模型没报」不是一回事。
KIND_VERIFY = "verify"

# 报告里那一节的标题，以及**历史数据**里那行机器可读块的标记（见 `strip_ruling_block`：
# 新运行不再写它，标记只用来把老行的残留认出来）。
# 2026-09-23：节名从「复核裁决（平台）」改为「复核标注（平台）」—— 正文主体回归模型写的
# 整体汇总报告（AI-P1-01 的呈现层反转），这一节降为跟在草稿后的标注（`subagent.aggregate_outcomes`）。
RULING_TITLE = "## 复核标注（平台）"
# 报告**开篇**那一节的标题（2026-09-24，run 63）。它只放三句话：核过几条、有几条没核、
# 被核的那几条各自什么下场；逐条明细仍在 `RULING_TITLE` 那一节，**排在正文之后**。
#
# 为什么要分开（run 57 的修法是对的，但做过头了）：run 57 的病是「正文写着 20 条结论、
# 读到尾部才知道只裁了 3 条」，所以把这一节整个前置了。run 63 实测的代价：前置的是
# **整节明细**（21 行，含逐条断言子列表），一份 141 行的报告要滚过 15% 的平台记账才读
# 到「这次改了什么」。第一眼要看见的是**那几个数**，不是逐条的对账记录。
RULING_SUMMARY_NAME = "复核摘要（平台）"
RULING_SUMMARY_TITLE = "## " + RULING_SUMMARY_NAME
RULING_BLOCK_MARKER = "ai-verify-ruling"

# 报告里每条理由/依据占的字符上限。裁决是模型写的，长度不受控 —— 一段几千字的「理由」
# 会把报告正文挤掉，而它要说的其实一句话就够。
_REASON_MAX_CHARS = 400
_REFS_MAX_ITEMS = 5
# 「查过什么」那一栏的上限。它在报告里是**一行**里的一个分句，写成长段落会把这一节
# 顶成散文 —— 而这一节的用途是让人一眼扫出「哪几条还没被证实」。
_SCOPE_MAX_CHARS = 240

# 「证据不足」时等级降一档的阶梯（口径 ①）。
#
# **为什么必须动等级**：实测 run 15 的 `F3` 裁决逐字写着「原 critical / very_high →
# 证据不足（待人工核验）」，而落库那行的 `severity` / `original_severity` 都还是 `critical`
# —— 清单里于是同时存在「critical」与「证据不足」，自相矛盾；而且这条会作为下一轮的基线
# （「上一次为止仍然成立的问题全集」）继续传下去。
#
# 阶梯比平台的严重度枚举**宽一档**：`skill_contract.SEVERITIES` 只有 `critical` / `high`，
# 所以 `high → medium` 是**平台赋值**的等级（模型报不出它，它只在「证据不足」这个处置上
# 出现）。`low` 是阶梯底，保持不动 —— 再降就成了「没有等级」。
SEVERITY_STEP_DOWN = {
    "critical": "high",
    "high": "medium",
    "medium": "low",
    "low": "low",
}

# 证据有已知缺口（口径 ③④）时置信度的上限。**不许维持 `very_high`**。
CONFIDENCE_CEILING_WITH_GAP = "high"

# 正文里模型自己编的编号（口径 ②）：`R1`…`R13`。
#
# 三条边界都是必需的：前面不能是字母/数字/下划线（`RF1`、`SV1` 不是它），后面不能紧跟数字
# （`R13` 不许被读成 `R1`），长度最多 3 位（正文编号不可能上千）。
_BODY_LABEL_RE = re.compile(r"(?<![A-Za-z0-9_])R(\d{1,3})(?![0-9])")
# 一个正文编号的上下文窗口 = 它所在的那一行 + 紧跟的一行（列表项常把「位置」写在下
# 一行），再按字符数封顶。**不做「整段」或「整篇」**：窗口一大，每条结论都能在里面找到
# 自己的文件路径，映射就从「判据」退化成「猜」。
_BODY_WINDOW_CHARS = 400
_BODY_WINDOW_LINES = 2

# 「索取额度用尽」这个降级码的取值。**必须与 `engine.DEGRADE_REQUESTS` 逐字一致**
# （有测试钉着；`subagent` 侧用的是那个常量，这里是读 `outcome.degradation` 时的比对值）。
# 不 import 它：本模块是纯函数层，engine 是执行层，反向依赖会把执行栈拖进来。
QUOTA_EXHAUSTED_CODE = "requests_exhausted"

# 依据的形状校验（口径 ③）搬到了 `services/ai/ref_shapes.py`：它的第二个读者是
# `claims.claim_review_of`（一条原子断言的依据能不能核），两处必须用同一份判据。
# `is_locatable_ref` 在这里重新导出 —— 既有导入点与测试都按本模块的名字用它。

# 依据/缺口两份文本比对时的**最短可比对长度**。低于它的词（`ID`、`a.lua`）在任何一份证据
# 里都出现得太多，拿它当「这条结论引用了那个文件」的判据必然误报 —— 宁可不匹配。
_MIN_MATCH_CHARS = 4

# 被截断的文件路径从**交付条目的标签**里读（`context_tools.describe_request` 的形态：
# `file_diff <commit12> <path>`）。只认这两种类型：`read_reference` 的标签是参考文档名、
# `commit_detail` 只有一个提交号，它们都不是仓库里的文件路径。
_FILE_LABEL_KINDS = ("file_diff", "file_content")


def severity_step_down(severity: str) -> str:
    """等级降一档（`critical` → `high` → `medium` → `low`）。

    **不认识的等级原样返回**：凭空编一个更低的等级，比「没降」更糟 —— 后者在报告里看得
    出来（写着证据不足、等级却没动），前者是一条查不出出处的假事实。
    """
    text = str(severity or "").strip().lower()
    return SEVERITY_STEP_DOWN.get(text, text)


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
    #: 这条结论的**逐条断言**裁决（`claims[]`）。空 = 复核轮没逐条回答，那时按
    #: 「一条都没证实」处理（见 `_claim_reviews_of`）。
    claims: tuple[ClaimVerdict, ...] = ()


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
    # 平台自己写的一句（裁决不完整、证据有缺口等原因）。与 `reason`（模型写的理由）分开：
    # 两者的作者不同，读的人需要分得清哪一句是模型说的。**多个原因用「；」连起来**放在
    # 这一个字段里 —— 报告里它就是一行「平台说明：…」，分成几个字段只会让读者自己拼。
    note: str = ""
    # 正文里模型自己编的那个编号（`R3`）。空串 = 正文里找不到能对上的那一条，报告里如实写
    # 「正文未编号」（见 `assign_body_labels`：对不上时宁可不写）。
    body_label: str = ""
    # `evidence_refs` 里**不成形**的那些（`is_locatable_ref` 不认）。原样留着（模型说了什么
    # 不许篡改），但在报告里标成「不可定位」——它们不构成「可定位快照证据」。
    unlocatable_refs: tuple[str, ...] = ()
    # 这条的置信度是不是被平台**按证据缺口**压下来的（口径 ③④）。报告据此单列一节说明
    # 理由：降置信度而不说为什么，与缺陷本身是同一类问题。
    evidence_capped: bool = False
    # 这条结论**来源于哪几条分片候选**（`protocol.Anomaly.source_candidate_ids` 原样带来）。
    #
    # 它在这里的作用是让**候选对账**（`family_ledger.reconcile_candidates`）能按编号问
    # 「这条候选的去向是哪条结论」——在这之前那一层是靠标题/文件/证据文本反推的，而反推
    # 产生的假缺口正是 AI-P0-06 要修的那一类。撤销与降级都保持这个字段（`origin` 的那一份
    # 一路 `replace` 过来），所以「候选 → 结论」的对应关系不会因为裁决而断掉。
    source_candidate_ids: tuple[str, ...] = ()
    #: 这条结论的**原子断言**各自的裁决结果（P0-01）。空 = 这条结论没带断言（旧形态 /
    #: 模型没按协议写），那时它**不得算已核实**（见 `_apply_claims`）。
    claim_reviews: tuple[ClaimReview, ...] = ()
    #: 这一轮复核**是怎么**得出结论的（独立取证 / 原证据复读 / 未取证）。由平台按对账轮
    #: **实际执行过**的取数类型判定 —— 不采信模型自述（run 58 那三条的措辞是「反证不成立」，
    #: 而它 6 次索取全是按地址取回已有原文，一次新检索都没有）。
    verify_basis: str = VERIFY_BASIS_NONE

    @property
    def active(self) -> bool:
        return self.verdict != VERDICT_RETRACTED

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, UNREVIEWED_LABEL)

    @property
    def verify_basis_label(self) -> str:
        return VERIFY_BASIS_LABELS.get(
            self.verify_basis, VERIFY_BASIS_LABELS[VERIFY_BASIS_NONE]
        )

    @property
    def pending_claims(self) -> tuple[ClaimReview, ...]:
        """还没有被证实的那些断言（待核查 / 读不到）。**渲染与判定都读它。**"""
        return tuple(
            review
            for review in self.claim_reviews
            if review.status in (CLAIM_UNVERIFIED, CLAIM_UNREADABLE)
        )

    @property
    def refuted_claims(self) -> tuple[ClaimReview, ...]:
        return tuple(r for r in self.claim_reviews if r.status == CLAIM_REFUTED)

    @property
    def verified_claims(self) -> tuple[ClaimReview, ...]:
        return tuple(r for r in self.claim_reviews if r.status == CLAIM_VERIFIED)

    @property
    def fingerprint(self) -> str:
        """这条结论的身份指纹。**按裁决之后的那一份算**（`self.anomaly`，不是 `origin`）。

        P0 的标题改写（「只拿已证实的断言当标题」，`claims.compose_title`）会改变指纹 ——
        `anomaly_fingerprint` 收的是 `commit + 文件 + 标题词集`。而**载荷、落库、下一轮基线
        都按裁决后的那一条算指纹**（`result_payload._anomaly_entry` 写的就是它），所以这一
        格必须与它们同源：按 `origin` 算等于给同一行留了两个身份，两边对不上时落空的**恰好
        只有被裁决过的那几条** —— 真机 run 60 实测：`final_findings` 少了 3 条（16 → 13）、
        逐条断言与取证方式一个都没附上、撤销过滤对它们失效，而正文里写着已裁决。
        """
        return anomaly_fingerprint(self.anomaly)

    @property
    def level_changed(self) -> bool:
        return (
            self.anomaly.severity != self.origin.severity
            or self.anomaly.confidence != self.origin.confidence
        )

    def as_dict(self) -> dict:
        """机器可读形态（进结论载荷的 `final_findings` / `retracted_findings`）。"""
        return {
            "finding_id": self.finding_id,
            "source": self.source,
            "verdict": self.verdict,
            "verdict_label": self.verdict_label,
            "active": self.active,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "unlocatable_refs": list(self.unlocatable_refs),
            "evidence_capped": bool(self.evidence_capped),
            "note": self.note,
            "body_label": self.body_label,
            "fingerprint": self.fingerprint,
            # `title` 是**裁决之后**那一份：有未证实断言时它由平台按已证实的断言重排
            # （`_compose_title`），原来的标题原样留在 `original_title` 里。
            # 「标题里不许保留正文承认未核实的肯定断言」这条要求落在这一格上。
            "title": self.anomaly.title,
            "original_title": self.origin.title,
            "category": self.origin.category,
            "file_path": self.origin.file_path,
            "commit_ref": self.origin.commit,
            "severity": self.anomaly.severity,
            "confidence": self.anomaly.confidence,
            "original_severity": self.origin.severity,
            "original_confidence": self.origin.confidence,
            # 候选血缘：读侧据此回看「这条结论是哪个分片报的」。
            "source_candidate_ids": list(self.source_candidate_ids),
            # 断言与取证方式：读侧据此回答「这条结论凭什么算核实过了」。
            "claims": [review.as_dict() for review in self.claim_reviews],
            "pending_claims": len(self.pending_claims),
            "verify_basis": self.verify_basis,
            "verify_basis_label": self.verify_basis_label,
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
    # 有**几条结论的置信度是被平台按证据缺口压下来的**（口径 ③④）。它必须计进 `changed`：
    # 一次「复核没给裁决、但平台按截断/额度缺口压了两条」的运行同样要渲染那一节 ——
    # 降了置信度却不解释，与这条缺陷本身是同一类问题。
    evidence_capped: int = 0

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

        `evidence_capped` 也算影响：把置信度从 `very_high` 压到 `high` 是落到结论上的
        改动，报告必须解释它（否则读侧只看到「库里是 high、正文写着 very_high」）。
        """
        return bool(
            self.verdicts_seen
            or self.new_findings
            or self.rejected
            or self.evidence_capped
        )

    def active_anomalies(self) -> tuple[Anomaly, ...]:
        """落库与界面要用的那一份（`outcome.anomalies` 就取它）。"""
        return tuple(row.anomaly for row in self.active)

    @property
    def claimed_candidate_ids(self) -> frozenset[str]:
        """这份结果里**出现过的候选编号**（`FindingRow.source_candidate_ids` 的并集）。

        给候选对账用（`family_ledger.reconcile_candidates`）：汇总把哪些编号交回来了。
        **含被撤销的那几行** —— 撤销本身也是一个去向（「已撤销」），把它排除会让那条候选
        又变成「找不到去向」，而那正是 AI-P0-06 要修的那一类假缺口。
        """
        return frozenset(
            candidate_id for row in self.rows for candidate_id in row.source_candidate_ids
        )

    @property
    def landed_candidate_ids(self) -> frozenset[str]:
        """**进了最终结论清单**（活动行）的那些候选编号。

        与 `claimed_candidate_ids` 的差别是「汇总写过它」还是「它真的还在清单里」：
        对账时前者解释「这条候选有去向」，后者才叫「已采纳」。
        """
        return frozenset(
            candidate_id for row in self.active for candidate_id in row.source_candidate_ids
        )

    def by_fingerprint(self) -> dict[str, FindingRow]:
        return {row.fingerprint: row for row in self.rows}

    def as_dict(self) -> dict:
        return {
            "verdicts_seen": int(self.verdicts_seen),
            "evidence_capped": int(self.evidence_capped),
            "rows": [row.as_dict() for row in self.rows],
            "rejected": [
                {"reason": item.reason, "detail": item.detail} for item in self.rejected
            ],
        }
