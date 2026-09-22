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

reducer 的产出由平台**结构化地带出去**（`EngineOutcome.verdict`，就是 `Reduction.as_dict()`
那一份）：`result_payload` 从那个字段把它取回来放进结论载荷 —— 于是 **markdown、落库的值、
读侧、导出全部从 `final_findings` 渲染**，模型写的正文不再是独立真相源。

## 裁决**不再**写进报告正文（AI-P0-05，2026-09-21）

原先 reducer 的产出还会被序列化成一行 HTML 注释追加在报告末尾（`ruling_block`），假设
「markdown 渲染看不见它」。那个假设是错的：本平台的安全渲染器是**先整体转义、再套白名单**
（`static/js/ai-report-markdown.js`），注释必然变成一段可见的乱码 —— 实测 run 20 的
`response_text` 里 35.3%（12,617 / 35,763 字符）就是那段机器 json，而用户在页面上真的看到了它。

现在机器裁决只走结构化的那一条路。报告正文里只留**给人看**的内容：本模块渲染的
「复核裁决（平台）」一节（`render_ruling`），以及「未归类 / 条数上限 / 信息缺口」那几节。

## 四条落到结论上的口径（2026-09-21，全部来自 run 15 的实测）

1. **「证据不足」降一档等级**（`severity_step_down`）。原先只降置信度、等级一动不动，清单里
   于是出现「critical + 证据不足」这种自相矛盾的组合：`F3` 的裁决逐字写着「原 critical /
   very_high → 证据不足（待人工核验）」，而落库那行的 `severity` / `original_severity` 仍是
   `critical`。降级理由**沿用复核给的理由**，平台只补一句「降到了哪一档、为什么」。
   **阶梯比模型的严重度闭集宽一档**（`critical` → `high` → `medium` → `low`），而
   `skill_contract.SEVERITIES` 只有 `critical` / `high` —— 这个不一致是**刻意保留**的，
   见下面「为什么不去扩 `SEVERITIES`」。
2. **正文编号与平台编号的映射**（`assign_body_labels`）。模型在正文里自己编 `R1`…`R13`，
   平台发的是 `F1`…`F17`（实测 13 ≠ 17），读者没法把「裁决」落回「正文那一条」。映射只按
   **可复现的判据**（同一个 `file_path` / 标题词集相似度）建立；一个正文号被两条结论认领、
   或者一条结论对上两个正文号时**都不写** —— 写错比不写糟得多（写错会把裁决的账记到别的
   发现头上）。
3. **依据的形状校验**（`is_locatable_ref`）。标准 unified diff hunk 同时带文件路径和
   old/new 行区间，固定 commit 后可以稳定复现，属于可定位证据；不完整的 `diff@@` 残片、
   `x.xlsx:第 3 个 sheet`、`见 a.lua 第 61 行` 等散文仍会被拦截。裸文件路径
   （`build/lua/CfgItem.lua`）**算可定位** —— 它是能去查的快照坐标。不成形的**照原样留着
   并标成「不可定位」**（模型说了什么不许篡改），但一条都定位不到时**不得维持 `very_high`**。
4. **证据缺口压置信度**（`EvidenceGaps`）。`F1` 是 `critical`/`very_high` 且裁决「反证不成立
   （维持）」，而**同一份报告的信息缺口里**写着它引用的 `ProtoCScs.lua` 的 diff 被长度上限
   截断过（「第 11/24 段之后的改动块未看到」）；`F3` 转人工的直接原因是「`find_references`
   额度耗尽，无法枚举调用点」，而这次运行整体就是 `requests_exhausted` 降级。两类信号都可
   判定，所以规则是确定性的：**结论引用的文件被截断过、或者这次运行因索取额度用尽而降级且
   它需要的那一块没轮到 → 不得维持 `very_high`**，并在裁决与落库的文案里写明理由。
   为什么不做成「一律降」见 `EvidenceGaps` 的说明。

## 为什么不去扩 `SEVERITIES`

`medium` / `low` 是**平台赋值**的等级（只在「证据不足」这条处置上出现），不是模型能选的
等级：`skill_contract.SEVERITIES = ("critical", "high")` 是**给模型的闭集**，那是一道纪律 ——
一旦模型可以自己选 `medium`，它会拿「这不算太严重」把本该报上来的东西降着报，而阈值与
「哪些必须人工跟进」的口径都建立在这个闭集上。

代价是**同一件事在两个地方有两种写法**，各自都有明确的归属：

* **给模型看的**（下一轮提示词里的已报问题清单）：折回闭集 ——
  `baseline._prompt_severity`，否则模型会照抄一个它自己不许写的等级，那一轮报出来的条目
  会被 `protocol` 按「severity 不在允许集合内」丢掉；
* **给人看的与落库的**（裁决节、`final_findings`、异常表、导出）：平台降出来的**真实等级**
  （`medium`）。折回只发生在注入侧那一个字段上，**不是**在掩盖降档 —— 这一点在
  `baseline._prompt_severity` 的注释里也写了一遍（两处读者不同，两处口径都写着出处）。
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
    DEFAULT_SIMILARITY_THRESHOLD,
    MIN_SIMILARITY_TOKENS,
    SEVERITY_RANK,
    anomaly_fingerprint,
    containment,
    is_probable_duplicate,
    rank_anomalies,
    tokenize,
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

# 报告里那一节的标题，以及**历史数据**里那行机器可读块的标记（见 `strip_ruling_block`：
# 新运行不再写它，标记只用来把老行的残留认出来）。
RULING_TITLE = "## 复核裁决（平台）"
RULING_BLOCK_MARKER = "ai-verify-ruling"

# 报告里每条理由/依据占的字符上限。裁决是模型写的，长度不受控 —— 一段几千字的「理由」
# 会把报告正文挤掉，而它要说的其实一句话就够。
_REASON_MAX_CHARS = 400
_REFS_MAX_ITEMS = 5

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

# 可定位依据的五种形态（口径 ③）。**逐条判据都在 `is_locatable_ref` 里**，这里只放形状：
#
# * 行号 / 行范围：`12`、`12-15`、`12 ~ 15`；
# * sheet 名：`Sheet1`（不许含空白、`!`、`:`、`@`）；
# * 单元格 / 区间：`Sheet1!A1`、`Sheet1!A1:C5`。
# * unified diff hunk：`a.lua @@ -12,3 +14,5 @@` 或兼容旧模型的
#   `a.lua:diff@@ -12,3 +14,5 @@`；必须同时有完整 old/new 坐标。
_REF_LINE_RE = re.compile(r"^\d{1,7}(?:\s*[-~]\s*\d{1,7})?$")
_REF_SHEET_RE = re.compile(r"^[^\s!:@]{1,64}$")
_REF_CELL_RE = re.compile(r"^[^\s!:@]{1,64}![A-Za-z]{1,3}\d{1,7}(?::[A-Za-z]{1,3}\d{1,7})?$")
_REF_HUNK_RE = re.compile(
    r"^(?P<path>\S+?)(?:\s+|:diff)"
    r"@@ -\d{1,7}(?:,\d{1,7})? \+\d{1,7}(?:,\d{1,7})? @@(?: .*)?$"
)
# 配表类扩展名。sheet / 单元格这两种定位符**只对它们成立**：`.lua` 文件没有 sheet，
# 认下来就等于把一句自由文本当成坐标。
_SHEET_SUFFIXES = (".xlsx", ".xlsm", ".xlsb", ".xls", ".csv")

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
            "title": self.origin.title,
            "category": self.origin.category,
            "file_path": self.origin.file_path,
            "commit_ref": self.origin.commit,
            "severity": self.anomaly.severity,
            "confidence": self.anomaly.confidence,
            "original_severity": self.origin.severity,
            "original_confidence": self.origin.confidence,
            # 候选血缘：读侧据此回看「这条结论是哪个分片报的」。
            "source_candidate_ids": list(self.source_candidate_ids),
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
# ② 正文编号 → 平台编号
# --------------------------------------------------------------------------


def assign_body_labels(report: str, findings: Sequence[Finding]) -> dict[str, str]:
    """`{F编号: 正文编号}`：把平台发的编号对回**模型在正文里自己编的那个 `R#`**。

    ## 为什么要有这一步

    两套编号是各编各的：模型在「风险评估 / 结论清单」里写 `R1`…`R13`，reducer 另发
    `F1`…`F17`（实测 run 15 是 13 对 17，条数也不等）。读者拿着裁决节里的 `[F3]`，
    落不回正文的任何一条 —— 裁决是给**人**看的，对不上就等于没写。

    ## 判据（可复现，且宁可不写）

    每个 `R#` 取它**所在那一行 + 紧跟一行**（列表项常把「位置」写在下一行）作为窗口，
    一条结论与它对应当且仅当下面**之一**成立：

    * 这条结论的 `file_path` 出现在窗口里（路径是位置，最强的判据）；或
    * 这条结论的标题词集被窗口覆盖到 `rules.DEFAULT_SIMILARITY_THRESHOLD` 以上
      （与近似去重同一套 `tokenize` / `containment`，不是这里另写一套相似度）。

    然后两道**唯一性**闸门，任一不满足就整条不写：

    * 一条结论对上了**两个不同的** `R#` —— 分不清它对应哪一条；
    * 一个 `R#` 被**两条结论**认领 —— 至少有一条会指错。

    对不上（或没对上的）一律在报告里如实写「正文未编号」。**这不是缺失，是事实**：
    正文本来就只有 13 条，而平台发得出 17 个编号。
    """
    entries = _body_label_entries(report)
    if not entries:
        return {}
    claims: dict[str, list[str]] = {}
    for finding in findings:
        matched = sorted(
            {
                label
                for label, window in entries
                if _same_finding_as_window(finding.anomaly, window)
            }
        )
        if len(matched) != 1:
            continue
        claims.setdefault(matched[0], []).append(finding.finding_id)
    return {
        finding_id: label
        for label, finding_ids in claims.items()
        if len(finding_ids) == 1
        for finding_id in finding_ids
    }


def _body_label_entries(report: str) -> list[tuple[str, str]]:
    """正文里每个 `R#` 与它的上下文窗口：`[("R3", "R3. …\\n位置：…")]`。"""
    text = str(report or "")
    lines = text.splitlines()
    # 每个字符落在哪一行。窗口要按**行**取，所以先把行首偏移量算出来（一次 O(n)），
    # 匹配时二分查找 —— 直接在每行上跑正则会漏掉跨行重复编号的去重。
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line) + 1
    entries: list[tuple[str, str]] = []
    for matched in _BODY_LABEL_RE.finditer(text):
        line_index = _line_index_of(starts, matched.start())
        window = "\n".join(lines[line_index : line_index + _BODY_WINDOW_LINES])
        entries.append((f"R{int(matched.group(1))}", window[:_BODY_WINDOW_CHARS]))
    return entries


def _line_index_of(starts: Sequence[int], position: int) -> int:
    """`position` 落在第几行（`starts` 是各行首的偏移量，递增）。"""
    low, high = 0, len(starts) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if starts[middle] <= position:
            low = middle
        else:
            high = middle - 1
    return low


def _same_finding_as_window(anomaly: Anomaly, window: str) -> bool:
    """这条结论与这段正文窗口讲的是不是同一条（判据见 `assign_body_labels`）。"""
    path = _norm_text(anomaly.file_path)
    if len(path) >= _MIN_MATCH_CHARS and path in _norm_text(window):
        return True
    title_tokens = tokenize(anomaly.title)
    window_tokens = tokenize(window)
    if not title_tokens or not window_tokens:
        return False
    if min(len(title_tokens), len(window_tokens)) < MIN_SIMILARITY_TOKENS:
        return False
    return containment(title_tokens, window_tokens) >= DEFAULT_SIMILARITY_THRESHOLD


# --------------------------------------------------------------------------
# ③ 依据的形状校验
# --------------------------------------------------------------------------


def is_locatable_ref(ref: str) -> bool:
    """这条依据能不能**照着它定位到东西**（口径 ③）。

    认可三组形态，其余一律不成形：

    * **裸路径**（`build/lua/CfgItem.lua`）：**能去查**的快照坐标 —— 平台自己的 diff
      载荷就是这么标的（`file_diff <commit> <path>`），按提交取一份快照就能核。判据复用
      `_looks_like_path`：无空白 / 控制字符、无 `@@`，且**最后一段带扩展名**。
      刻意**没有**放宽到「有分隔符就算」（`code/qz_server/src/tms/module` 这种只有目录、
      没有文件名的写法）：本仓库里可定位的东西都带扩展名（`config/*.xlsx`、
      `code/**/*.lua`），一个到目录为止的写法更可能是被截断的路径或一句概述，
      而不是一个坐标；而这一侧的错法比另一侧贵（见下面「宁严勿宽」）；
    * **路径 + 定位符**，定位符只认下表四种（`文件:行` / `文件:行范围` / `文件:sheet` /
      `文件:sheet!单元格`）：

      | 定位符 | 形态 | 例 |
      |---|---|---|
      | 行 / 行范围 | `^\\d{1,7}([-~]\\d{1,7})?$` | `61`、`61-66` |
      | sheet | 无空白 / `!` / `:` / `@` 的名字 | `Sheet1` |
      | 单元格 / 区间 | `sheet!A1` / `sheet!A1:C5` | `Sheet1!B2` |

      后两种**只对配表类扩展名成立**：`.lua` 没有 sheet，`Sheet1` 挂在它后面是自由文本，
      不是坐标。
    * **路径 + 完整 unified diff hunk**：同时给出 old/new 起始行和可选长度，例如
      `a.lua @@ -61,66 +66,26 @@`。固定 commit 后它能直接定位对应改动块。兼容模型曾输出的
      `a.lua:diff@@ ... @@`，但不接受少一侧坐标、缺路径或混入 hunk 语法的散文。

    ## 两处宁严勿宽的取舍

    两侧的错法不对称：**判成不可定位**只影响「能否维持 `very_high`」（结论留着、报告里
    写明理由）；**判成可定位**却会把一句散文当成证据，而那正是这一条要拦下的东西。所以：

    * 定位符里带空白一律不成形。真实的工作表名极少带空格，而带空格的那些写法里绝大多数
      是散文（`x.xlsx:第 3 个 sheet`）；
    * 路径里带空白同样不成形（`见 a.lua 第 61 行` 是一句话，不是一个坐标）。

    反过来说，**只有「有个定位符、但定位符不成形」才判死**：`path` 后面什么都没有
    （裸路径）是合法形态，不是残缺形态 —— 把它一起判死会让一批正常结论无故压档，而压档
    是本层唯一会改结论的副作用。
    """
    text = _ref_body(ref)
    hunk = _REF_HUNK_RE.fullmatch(text)
    if hunk:
        return _looks_like_path(hunk.group("path"))
    path, locator = _split_ref(text)
    if not locator:
        # 裸路径（或整个字符串里就没有 `:`）。判据与切分那一侧同一个函数，不另写一份。
        return _looks_like_path(text)
    if not path:
        return False
    if "," in locator or "@" in locator:
        return False
    if locator.isdigit() or _REF_LINE_RE.match(locator):
        return True
    if path.lower().endswith(_SHEET_SUFFIXES):
        return bool(_REF_SHEET_RE.match(locator) or _REF_CELL_RE.match(locator))
    return False


def _ref_body(value: Any) -> str:
    """依据的裸文本：剥掉空白与模型常加的那几对包裹符号（反引号、引号、括号）。"""
    return str(value or "").strip().strip("`'\"“”‘’（）()[]<>").strip()


def _split_ref(ref: str) -> tuple[str, str]:
    """把一条依据拆成 `(路径, 定位符)`（拆不出就是两个空串）。

    切点**从左往右**找第一个「左边像个路径」的 `:`，而不是从右边切：

    * `C:/work/a.lua:61` —— 盘符那个 `:` 的左边是 `C`，最后一段没有点，跳过；
    * `config/x.xlsx:Sheet1!A1:C5` —— 单元格区间**自带一个 `:`**，从右边切会把路径切成
      `config/x.xlsx:Sheet1!A1`（一个不存在的文件），反过来切才对；
    标准 hunk 在进入这里前已经由 `is_locatable_ref` 单独识别；不完整的 `diff@@` 残片仍会
    保留成 `(路径, 定位符)`，并由形状校验拒绝。

    路径里含空白（模型写了一句「见 xxx.lua 第 61 行」）或含 `@@` 一律判不成形：那是散文，
    不是坐标。
    """
    text = _ref_body(ref)
    for position, char in enumerate(text):
        if char != ":":
            continue
        path, locator = text[:position].strip(), text[position + 1 :].strip()
        if _looks_like_path(path) and locator:
            return path, locator
    return "", ""


def _looks_like_path(value: str) -> bool:
    """这一段像不像一个**文件路径**（不是「有斜杠」就行，见 `_split_ref`）。"""
    if not value or any(char.isspace() for char in value) or "@@" in value:
        return False
    return "." in value.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------
# ④ 证据缺口
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceGaps:
    """本次运行**已知的、可判定的**证据缺口。

    ## 两类信号，都只看账本、不看措辞

    * `truncated_files`：**交付**时被长度上限截断过的文件（`RoundRecord.executed` 里
      `meta["truncated"]` 为真的那几条，路径从它自己的标签里读）。`tool_stats` 里也有
      `truncated` 计数，但那是**按工具类型**记的合计、点不出文件名 —— 拿它当判据只能
      「这次有截断 → 所有结论一律降」，正是下面说的那种一刀切，所以不用它。
    * `quota_refusals` + `requests_exhausted`：这次运行整体因**索取额度用尽**而降级，且
      被拒掉的那几次索取点名到了某个文件 / 某个检索词。两者必须同时成立。

    ## 为什么不做成「一律降」

    `requests_exhausted` 是**全运行级**的信号（任何一片额度用尽，整家都标这个降级码）。
    直接拿它把所有结论的置信度打下去，等于用「某一片少看了两个文件」去否定一条与那些
    文件毫无关系的结论 —— 那是拿一个与本案无关的事实去动真实结论，与「不能拿一个不存在
    的复核去动真实结论」是同一条口径。

    所以范围收在**这一条结论自己受影响的证据**上：它引用的文件被截断过，或者它需要的
    那一块（点名到它的文件 / 它的检索词）一次都没轮到。代价如实说：被拒的请求**没有点名**
    到这条结论时（模型要的是别的东西），这条不降 —— 那时平台的账本里确实没有任何证据
    说明它受了影响，宁可不降，也不编一个理由。
    """

    truncated_files: tuple[str, ...] = ()
    quota_refusals: tuple[str, ...] = ()
    requests_exhausted: bool = False

    @property
    def known(self) -> bool:
        """有没有**可能**影响某条结论的缺口（只看账本，不针对具体某条）。"""
        return bool(self.truncated_files or (self.requests_exhausted and self.quota_refusals))


def evidence_gaps_of(outcomes: Iterable[Any]) -> EvidenceGaps:
    """从各成员的 `EngineOutcome` 里读出本次运行的证据缺口（鸭子类型，纯函数）。

    `outcomes` 里可以有 `None`（没跑成的成员）—— 跳过，而不是当成「它没截断任何东西」：
    跳过只是不贡献信号，把它当成一条反证会凭空减少缺口。
    """
    truncated: list[str] = []
    refusals: list[str] = []
    exhausted = False
    for outcome in outcomes:
        if outcome is None:
            continue
        if str(getattr(outcome, "degradation", "") or "") == QUOTA_EXHAUSTED_CODE:
            exhausted = True
        refusals.extend(
            str(label) for label in (getattr(outcome, "refused_requests", ()) or ()) if label
        )
        for record in getattr(outcome, "rounds", ()) or ():
            for item in getattr(record, "executed", ()) or ():
                meta = getattr(item, "meta", None) or {}
                if not meta.get("truncated"):
                    continue
                path = _label_file(getattr(item, "label", ""))
                if path:
                    truncated.append(path)
    # `dict.fromkeys` 去重且**保序**：报告里那几句话的顺序每次都要一样（同一个输入
    # 两次运行得出不同的措辞，读的人会以为是两件事）。
    return EvidenceGaps(
        truncated_files=tuple(dict.fromkeys(truncated)),
        quota_refusals=tuple(dict.fromkeys(refusals)),
        requests_exhausted=exhausted,
    )


def _label_file(label: Any) -> str:
    """交付条目的标签 → 它读的是哪个文件（认不出来就是空串）。

    标签形态由 `context_tools.describe_request` 定：`file_diff <commit12> <path>`，窗口
    请求再加一段 `lines=…`。`=` 那一段先摘掉（不然 `parts[-1]` 拿到的是窗口而不是路径），
    提交号可能是空串（于是只剩两段），所以从**末尾**取路径而不是按下标取。
    """
    parts = [part for part in str(label or "").strip().split() if "=" not in part]
    if len(parts) < 2 or parts[0] not in _FILE_LABEL_KINDS:
        return ""
    return _norm_path(parts[-1])


def _label_token(label: Any) -> str:
    """被拒的索取标签 → 它点名的那一块（文件 / 检索词 / 提交号）。

    标签形态由 `context_tools._human_request_label` 定：`引用扫描 <query>`、
    `config/x.xlsx（12-15 行）`、`提交 abcd1234 的改动详情`、`参考文档 xxx`。
    这里只做「把前缀和括号摘掉」这一件事，**不猜**：摘不出东西就返回空串，
    调用方据此不匹配。
    """
    text = str(label or "").strip()
    for prefix in ("引用扫描", "参考文档", "提交"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    text = text.split("（")[0].strip()
    if text.endswith("的改动详情"):
        text = text[: -len("的改动详情")].strip()
    return text


def _norm_path(value: Any) -> str:
    """路径的显示形态：反斜杠转正斜杠、去掉开头的 `./`。**大小写原样保留** ——
    报告里那一句要点名到真实存在的文件，`protocscs.lua` 是个不存在的路径。"""
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _norm_text(value: Any) -> str:
    """比对用的归一化：在 `_norm_path` 之上再折叠大小写。"""
    return _norm_path(value).lower()


def _refers_to(text: str, needle: str) -> bool:
    """`needle` 有没有出现在这段文本里（口径 ④ 的比对，两侧都归一化）。

    长度低于 `_MIN_MATCH_CHARS` 的比对对象直接判否：`ID`、`a.lua` 这类词在任何一段证据里
    都能找到，用它当「这条结论引用了它」的判据必然误报。
    """
    token = _norm_text(needle)
    return len(token) >= _MIN_MATCH_CHARS and token in _norm_text(text)


def _cited_text(anomaly: Anomaly) -> str:
    """一条结论**自己引用过的东西**（缺口比对只在这份文本上做）。

    取标题 / 文件路径 / 提交 / 证据四项。**不含 `impact` / `suggestion`**：那两段是建议
    （「建议回归 xx 模块」），把它们算进来会让一条只是*提到*某个文件的结论也被判成受缺口
    影响 —— 缺口判据要的是「这条结论的证据依赖它」，不是「这段话里出现过它」。
    """
    return "\n".join(
        [anomaly.title or "", anomaly.file_path or "", anomaly.commit or "", *anomaly.evidence]
    )


def gap_reasons_of(anomaly: Anomaly, gaps: EvidenceGaps) -> list[str]:
    """这条结论身上**已知**的证据缺口（一条一句人话；没有就是空列表）。

    只读账本里的可判定信号（文件被截断 / 额度用尽且它需要的那块没轮到），不看报告里的
    自由文本 —— 「模型在信息缺口那一节里提过它」这种判据改一个字就静默失效。
    """
    if not gaps.known:
        return []
    cited = _cited_text(anomaly)
    reasons: list[str] = []
    for path in gaps.truncated_files:
        if _refers_to(cited, path):
            reasons.append(
                f"它引用的 `{path}` 在这次运行里被长度上限**截断**过（后面的改动块没看到），"
                "证据是残缺的"
            )
    if gaps.requests_exhausted:
        for label in gaps.quota_refusals:
            if _refers_to(cited, _label_token(label)):
                reasons.append(
                    f"这次运行的**索取额度用尽**，它需要的「{label}」一次都没轮到"
                )
    return reasons


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
    body_text: str = "",
    gaps: EvidenceGaps = EvidenceGaps(),
) -> Reduction:
    """把「主结论 + 对账轮新发现 + 结构化裁决」压成唯一的一份结果。**确定性**。

    参数 `base` 是主结论清单（`synthesis.anomalies`），`new` 是对账轮自己报出来的
    （`outcome.anomalies`）。两者都已经过引擎那一侧的门槛与去重，这里再做的是**跨来源**
    的那几件事：重复、条数上限、以及按裁决决定去留。

    另外两个参数是 2026-09-21 加的，都不改上面那条主路：

    * `body_text`：模型写的正文（`synthesis.report_markdown`），用来把 `F#` 对回正文里
      它自己编的 `R#`（`assign_body_labels`）。空串 = 没有正文可对，报告里如实写「正文未编号」。
    * `gaps`：本次运行**已知的证据缺口**（`EvidenceGaps`）。**默认空 = 这一步什么都不做**
      —— 单代理路径、测试里的直接调用都走这个默认值，行为逐字不变。
    """
    findings = assign_findings(base)
    labels = assign_body_labels(body_text, findings)
    rows = [
        replace(_row_of(finding, verdicts), body_label=labels.get(finding.finding_id, ""))
        for finding in findings
    ]
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

    # 证据缺口（口径 ③④）压在**封顶之后**：它只改「留在清单里」的那些条，而次序由严重度
    # 主导、置信度只影响同档内的先后 —— 放在封顶之后，这一步就不可能改变谁被截掉。
    kept = _with_evidence_gaps(_apply_limit(rows, limit=limit, rejected=rejected), gaps)
    return Reduction(
        rows=kept,
        rejected=tuple(rejected),
        verdicts_seen=len(matched_ids),
        evidence_capped=sum(1 for row in kept if row.evidence_capped),
    )


def _row_of(finding: Finding, verdicts: Sequence[VerifyVerdict]) -> FindingRow:
    """一条主结论 + 它收到的那条裁决（没收到就是「未复核」）。**取第一条，多余的记账。**"""
    verdict = next(
        (item for item in verdicts if item.finding_id == finding.finding_id), None
    )
    origin = finding.anomaly
    # 候选血缘**在两条路上都带上**：`_apply_verdict` 各分支都是用 `origin` 建行的
    # （保留在 `anomaly` 里），而这里要保证「没收到裁决」那条路也一样不缺。
    if verdict is None:
        return FindingRow(
            finding_id=finding.finding_id,
            anomaly=origin,
            origin=origin,
            source_candidate_ids=origin.source_candidate_ids,
        )
    return _apply_verdict(finding.finding_id, origin, verdict)


def _apply_verdict(finding_id: str, origin: Anomaly, verdict: VerifyVerdict) -> FindingRow:
    """把一条裁决落到结论上。**不完整或不可信的裁决一律降格为「证据不足」**。

    降格而不是「按原样采信」：后者正是这条缺陷的复现路径（模型说了降级/撤销，清单里
    仍是 `critical`）。降格之后平台至少不再按 `very_high` 采信它，并在报告里写明原因。

    出口统一过一道 `_with_ref_shapes`（口径 ③）：`evidence_refs` 是模型写的自由文本，
    形状校验与「有没有收到裁决」无关 —— 每条路径都得出这一道。

    **候选血缘也在这里补一道**（不逐分支写）：下面每个分支都是拿 `origin` 建行的，
    血缘在 `origin` 里；逐分支各写一次迟早会漏掉一个分支，而漏掉的后果是「这条候选的
    去向查不到」——正是 AI-P0-06 要修的那一类假缺口。
    """
    row = _with_ref_shapes(_apply_verdict_inner(finding_id, origin, verdict))
    if row.source_candidate_ids == origin.source_candidate_ids:
        return row
    return replace(row, source_candidate_ids=origin.source_candidate_ids)


def _apply_verdict_inner(
    finding_id: str, origin: Anomaly, verdict: VerifyVerdict
) -> FindingRow:
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

    # `needs_more_evidence`：保留在清单里，但**等级降一档、且不得维持 very_high**
    # （置信度是它进清单的门槛之一，维持 very_high 等于这条裁决在数据上不留任何痕迹），
    # 并注明待人工核验。
    return _needs_more(finding_id, origin, reason, refs, note="")


def _needs_more(
    finding_id: str,
    origin: Anomaly,
    reason: str,
    refs: tuple[str, ...],
    *,
    note: str,
) -> FindingRow:
    """「证据不足」的处置：**等级降一档 + 置信度不再维持 `very_high`**（口径 ①）。

    两样都动，是因为它们答的是两个问题：等级是「这条问题现在算多严重」，置信度是「我们对
    它有多确定」。实测那次只动了后者，清单里于是同时出现 `critical` 与「证据不足」——
    自相矛盾的那个组合说的正是等级这一侧。
    """
    capped = CONFIDENCE_CEILING_WITH_GAP if origin.confidence == "very_high" else origin.confidence
    return FindingRow(
        finding_id=finding_id,
        anomaly=replace(
            origin, severity=severity_step_down(origin.severity), confidence=capped
        ),
        origin=origin,
        verdict=VERDICT_NEEDS_MORE_EVIDENCE,
        reason=reason,
        evidence_refs=refs,
        note=note,
    )


def _with_ref_shapes(row: FindingRow) -> FindingRow:
    """口径 ③：给 `evidence_refs` 做形状校验，不成形的标出来。

    两件事，分开做：

    * **标注**：不成形的那些进 `unlocatable_refs`，报告里在依据后面写「（不可定位）」。
      原字符串**不进改动** —— 模型说了什么是一个事实，平台可以标注它、不能改写它。
    * **据此不维持 `very_high`**：一条依据都定位不到时（`evidence_refs` 非空、可定位的
      一条都没有），这条结论手里其实没有可定位快照证据，而审计要求 high/critical 必须有。
      此时把置信度压到 `high`（`_cap_very_high`），理由写进 `note`。

    只要还有**一条**能定位的依据，就不压：那时 `very_high` 是那条依据在撑着，不成形的那条
    只是多余的话 —— 把多余的话当成缺陷去动真实结论，比留着它更糟。
    """
    bad = tuple(ref for ref in row.evidence_refs if not is_locatable_ref(ref))
    if not bad:
        return row
    row = replace(row, unlocatable_refs=bad)
    if len(bad) < len(row.evidence_refs):
        return row
    return _cap_very_high(
        row,
        f"复核给的 {len(bad)} 条依据**没有一条能定位**到「文件:行 / 文件:sheet/单元格 / "
        "完整 diff hunk」，够不上「可定位快照证据」",
    )


def _cap_very_high(row: FindingRow, reason: str) -> FindingRow:
    """把一条结论的置信度从 `very_high` 压到 `high`，并把理由写进平台说明。

    **只在真的是 `very_high` 时才是「一次降级」**：置信度已经在 `high` 或更低时原样返回
    （理由仍然成立，但那条已经被别的原因压过了 —— 再记一遍只会让报告里同一句话出现两次，
    而报告那一节是按「平台真做了什么」列的）。
    """
    if row.anomaly.confidence != "very_high":
        return row
    return replace(
        row,
        anomaly=replace(row.anomaly, confidence=CONFIDENCE_CEILING_WITH_GAP),
        evidence_capped=True,
        note=_join_notes(
            row.note,
            f"证据有**已知缺口**，不得维持 `very_high`（平台压到 `{CONFIDENCE_CEILING_WITH_GAP}`）：{reason}",
        ),
    )


def _join_notes(*parts: str) -> str:
    """把几句平台说明连成一句（`；` 分隔，空的丢掉）。"""
    return "；".join(part.strip() for part in parts if str(part or "").strip())


def _with_evidence_gaps(
    rows: tuple[FindingRow, ...], gaps: EvidenceGaps
) -> tuple[FindingRow, ...]:
    """口径 ④：本次运行有证据缺口时，受影响的那些条不得维持 `very_high`。

    只处理**活动的**行：被撤销的那些已经移出清单，给它们压置信度没有任何读者。
    """
    if not gaps.known:
        return rows
    out: list[FindingRow] = []
    for row in rows:
        reasons = gap_reasons_of(row.origin, gaps) if row.active else []
        out.append(_cap_very_high(row, "；".join(reasons)) if reasons else row)
    return tuple(out)


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
    "一条都没有生效** —— 清单里这些条按原样采信，读的时候按未复核看。"
    # 2026-09-21：原文**不再进报告正文**（AI-P1-01：报告只有一份规范结论），所以那句话
    # 不能再写「附在下面那一节里」—— 它现在是一份独立存档（结论载荷的
    # `verify_report_markdown`），报告里没有它。
    "它的原文留档在本次运行的结论载荷里（`verify_report_markdown`），不再附进报告。\n"
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
        "读的都是这一份。**报告里没有第二份结论清单** —— 模型写的那份汇总草稿不再进报告"
        "正文（它是一份独立存档，见这次运行的结论载荷 `draft_markdown`），所以读的人"
        "不必再去辨认哪一句已经被本节改掉。",
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
        # 措辞是「**去留**按原样」而不是「按原样采信」：证据缺口那一步（口径 ③④）与
        # 复核有没有给裁决无关，它照样会压置信度 —— 说「一律按原样」就把平台自己刚做的
        # 事说成了没发生。
        notice = (
            "**注意**：本次复核**没有回结构化裁决**（正文里的话不构成裁决，平台只认那个 "
            "json 块），所以下面的结论**去留**一律按原样采信；只有它新报出来的条目被合入了"
            "清单。"
        )
        if reduction.evidence_capped:
            notice += (
                f"另外，平台按本次运行的**证据缺口**压了 {reduction.evidence_capped} 条的"
                "置信度（见下面「证据缺口」那一节）—— 那不是复核的裁决，是平台自己的动作。"
            )
        lines.append(notice)
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
            "这几条**仍在清单里**，但平台按口径把它们**降了一档等级**（`critical` → `high`、"
            "`high` → `medium`），置信度也不再按 `very_high` 采信 —— 请人工看一遍再决定处置。"
            "为什么降：裁决给的是「证据不足」，而一条自己都说证据不足的结论不该同时挂着"
            "最高等级与最高置信度。"
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

    # 上面各节已经逐条列过的那些（下面那一节不重复列，理由见 `_gap_section`）。
    shown = {
        row.finding_id
        for row in (*retracted, *downgraded, *pending, *confirmed, *new_findings)
    }
    lines.extend(_gap_section(reduction, shown=shown))

    if reduction.rejected:
        lines.append(f"### 复核阶段记账：{len(reduction.rejected)} 条没有进入清单")
        lines.append("")
        lines.append(
            "这里**只记录对账轮（V1）新增或改写结论时被平台拒绝的条目**；主分析阶段因"
            "结论条数上限淘汰的条目会单独列在后面的「结论条数上限」中，两组不是重复计数。"
        )
        lines.append("")
        for item in reduction.rejected:
            detail = item.detail or "（未记标题）"
            lines.append(f"- {detail}：{item.reason}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _gap_section(reduction: Reduction, *, shown: set[str]) -> list[str]:
    """证据缺口那一节（口径 ③④）。**按平台的动作分组，与上面按裁决分组是两把尺子。**

    没被压过的运行（`evidence_capped == 0`）一个字都不渲染。已经被上面某一节列过的行
    **不在这里重复** —— 它的「平台说明」就写在上面那一行里（两边逐字一致），而同一件事
    在报告里出现两遍正是本节开头那句「以本节为准」要收的口子。
    """
    capped = tuple(row for row in reduction.rows if row.evidence_capped)
    if not capped:
        return []
    lines = [
        f"### 证据缺口 {len(capped)} 条（平台压到 `{CONFIDENCE_CEILING_WITH_GAP}`）",
        "",
        "本次运行的证据里**有已知的缺口**（某个文件被长度上限截断、或者索取额度用尽导致"
        "某一块一次都没轮到）。受影响的这几条**不得维持 `very_high`** —— 证据不完整时还挂着"
        "最高置信度，等于把「没看到」写成了「看过了」。压的是**置信度，不是结论**：它们仍在"
        "清单里，理由写在各条的「平台说明」里。",
        "",
    ]
    lines.extend(_row_line(row) for row in capped if row.finding_id not in shown)
    if all(row.finding_id in shown for row in capped):
        # 一个都不剩：这几条在上面某一节里已经逐条列过（同一行的「平台说明」就是理由）。
        # 不写这一句的话，这一节看起来像「说有 N 条、一条都没列出来」—— 而那正是这一批
        # 缺陷要防的那种「说了没做」。
        lines.append(
            "这几条已经在上面「已降级 / 待人工核验 / 反证不成立」的那一节里逐条列过，"
            "降级理由写在同一行的「平台说明」里；这里不再重复一遍。"
        )
    lines.append("")
    return lines


def _row_line(row: FindingRow) -> str:
    """一行的措辞。**原等级、裁决、处置三样都写出来** —— 只写裁决，读的人不知道
    「降级」是从哪一级降下来的。

    三个 2026-09-21 补上的东西，都是为了让人能**把这一行落回原处**：

    * 头部带上正文里那个编号（口径 ②，`[F3]（正文 R3）`）—— 没有就如实写「正文未编号」；
    * 等级/置信度**只要动过就写出来**（口径 ①），包括「证据不足」那一档，措辞里带上
      「平台按证据不足降一档」这句出处；
    * 不成形的依据就地标成「（不可定位）」（口径 ③）—— 它照原样留着，但不构成证据。
    """
    head = f"- **[{row.finding_id}]{_body_label_text(row)} {row.origin.title}**："
    original = f"原 `{row.origin.severity}` / `{row.origin.confidence}`"
    if row.verdict == VERDICT_RETRACTED:
        action = "**反证成立（撤销）**，已从当前结论清单移除"
    elif row.verdict == VERDICT_DOWNGRADED:
        action = (
            f"**反证部分成立（降级）**：`{row.origin.severity}` → `{row.anomaly.severity}`"
        )
        # 等级写在动作里了（`severity=False`），但**置信度**若另被证据缺口压过，
        # 还要单独说 —— 否则这一行会写着降了级、却看不出置信度也动了。
        if row.evidence_capped:
            action += _level_change_text(
                row, cause="平台按「证据有缺口」处理", severity=False
            )
    elif row.verdict == VERDICT_NEEDS_MORE_EVIDENCE:
        action = "**证据不足（待人工核验）**"
        action += _level_change_text(row, cause="平台按「证据不足降一档」处理")
    else:
        action = f"**{row.verdict_label}**"
        if row.evidence_capped:
            action += _level_change_text(row, cause="平台按「证据有缺口」处理")
    if row.source == SOURCE_VERIFY:
        action += "（对账轮新发现）"
    detail = [f"{original} → {action}"]
    if row.reason:
        detail.append(f"理由：{truncate_text(row.reason, _REASON_MAX_CHARS)[0]}")
    if row.evidence_refs:
        detail.append("依据：" + "、".join(_ref_text(row)))
    if row.note:
        detail.append(f"平台说明：{row.note}")
    return head + "；".join(detail)


def _body_label_text(row: FindingRow) -> str:
    """头部那一小段「（正文 R3）」。

    对账轮新发现的条目**不写**：它们本来就不在模型写的那份正文里（`source` 那一栏已经
    说了它从哪来），给它写一句「正文未编号」是拿一句真话去填一个不存在的问题。
    """
    if row.source == SOURCE_VERIFY:
        return ""
    return f"（正文 {row.body_label}）" if row.body_label else "（正文未编号）"


def _level_change_text(row: FindingRow, *, cause: str, severity: bool = True) -> str:
    """等级 / 置信度动过的话，把两处变化写出来并注明出处（口径 ① 要求的「降到了哪一档」）。

    `severity=False` 给「降级」那一支用：那里的等级变化已经写在动作里了，再写一遍就是
    同一件事在同一行里出现两次。
    """
    parts: list[str] = []
    if severity and row.anomaly.severity != row.origin.severity:
        parts.append(f"等级 `{row.origin.severity}` → `{row.anomaly.severity}`")
    if row.anomaly.confidence != row.origin.confidence:
        parts.append(f"置信度 `{row.origin.confidence}` → `{row.anomaly.confidence}`")
    if not parts:
        return ""
    return f"，{'、'.join(parts)}（{cause}）"


def _ref_text(row: FindingRow) -> tuple[str, ...]:
    """依据那一行：不成形的那些就地标成「（不可定位）」。

    **不改写原字符串**（模型说了什么是一个事实），只在它后面加这三个字 —— 读的人据此
    知道哪几条能照着去核，哪几条核不了。判据与 `is_locatable_ref` 是同一个函数，
    不在这里另写一份（两处各判一次迟早会不一致）。
    """
    bad = set(row.unlocatable_refs)
    return tuple(
        f"{ref}（不可定位）" if ref in bad else ref for ref in row.evidence_refs
    )


# 历史数据里那行机器块的形状：`<!-- ai-verify-ruling: {...} -->`。它里面若含 `-->`，
# 写进去时被转义成 `-->`（见 git 历史里的 `ruling_block`），所以这个正则里的
# `-->` 一定是那条注释真正的收尾。
#
# **只服务于历史数据的剥离**（`strip_ruling_block`）：新的运行不再往正文里写机器块，
# 但清理脚本与导出路径还要能把**老行**里那一行认出来并摘掉。
_RULING_BLOCK_RE = re.compile(
    r"<!--\s*" + re.escape(RULING_BLOCK_MARKER) + r"\s*:\s*(\{.*?\})\s*-->", re.DOTALL
)


def strip_ruling_block(markdown: str) -> str:
    """去掉**历史数据**里那行机器可读块（`<!-- ai-verify-ruling: {...} -->`）。

    ## 它现在只服务两件事，都不是「新写入的兼容层」

    * **一次性数据清理**（`scripts/clean_ruling_block_from_runs.py`）：库里已有的那些行
      还带着它，清理脚本按同一个正则摘掉；
    * **导出路径**（`routes/ai_analysis_routes.py`）：用户下载的是原始 markdown，
      在旧行被清理之前（或者清理脚本没跑过的库上），那一行会原样出现在文件里。
      导出前摘一次，读的人只看到给人看的那几节。

    ## 为什么这条设计被废掉了（AI-P0-05）

    原先裁决的机器形态就写在报告正文末尾，理由是「HTML 注释在 markdown 渲染里看不见」。
    但本平台的安全渲染器是**先整体转义、再套白名单**（`static/js/ai-report-markdown.js`），
    注释必然变成一段可见的乱码 —— 实测 run 20 的正文里 35.3% 是那段 json。靠注释藏内部
    数据本身就不可靠，所以裁决改走结构化字段（`EngineOutcome.verdict`），正文里只剩给人
    看的内容。这个函数因此不再有「新写入」的一侧。
    """
    return _RULING_BLOCK_RE.sub("", markdown or "").strip()


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
        "* `needs_more_evidence`：证据不足，转「待人工核验」。**这一档有代价**：平台会把这条"
        "的等级**降一档**（`critical` → `high`、`high` → `medium`），置信度也不再按 "
        "`very_high` 采信 —— 所以只在真的缺证据时才用它。\n\n"
        "**这一块是平台唯一的依据**：正文写得再明确，没有这一块，你这一轮复核对结论"
        "**一条都不生效**（平台不替你猜）。反过来，写了这一块它就会**直接改结论** ——"
        "所以只在反证真的成立时才写 `retracted` / `downgraded`。\n"
    )
