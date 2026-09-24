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
两节（2026-09-24 起）：开篇的「## 复核摘要（平台）」（`render_ruling_summary`，只有覆盖数
与各条的下场）与正文之后的「## 复核标注（平台）」（`render_ruling`，逐条点名被改判的结论）。

reducer 的产出由平台**结构化地带出去**（`EngineOutcome.verdict`，就是 `Reduction.as_dict()`
那一份）：`result_payload` 从那个字段把它取回来放进结论载荷 —— 于是 **markdown、落库的值、
读侧、导出全部从 `final_findings` 渲染**，模型写的正文不再是独立真相源。

## 裁决**不再**写进报告正文（AI-P0-05，2026-09-21）

原先 reducer 的产出还会被序列化成一行 HTML 注释追加在报告末尾（`ruling_block`），假设
「markdown 渲染看不见它」。那个假设是错的：本平台的安全渲染器是**先整体转义、再套白名单**
（`static/js/ai-report-markdown.js`），注释必然变成一段可见的乱码 —— 实测 run 20 的
`response_text` 里 35.3%（12,617 / 35,763 字符）就是那段机器 json，而用户在页面上真的看到了它。

现在机器裁决只走结构化的那一条路。报告正文里只留**给人看**的内容：模型写的整体汇总报告
（正文主体，见 `subagent.aggregate_outcomes`），以及本模块渲染的两节
（`render_ruling_summary` / `render_ruling`）与「未归类 / 条数上限 / 信息缺口」那几节。

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

from services.ai.claims import (
    # 下面这几个名字在本模块**重新导出**（既有的导入点与测试按 `verdict` 的名字用它们：
    # `subagent` 取 `INDEPENDENT_REQUEST_TYPES`，测试取状态常量与中文名表）。
    #
    # 2026-09-25 拆分后只剩下这些：原先还有几个是**为本模块自己的逻辑**导入的
    # （`claim_lines` / `ClaimReview` / `parse_claim_verdicts` …），那些随逻辑搬去了
    # `verdict_types` / `verdict_render`。从本模块取用它们从来不是契约 —— 需要它们就
    # 去那一层拿。判据不是「谁还记得」，是**全仓扫 `from services.ai.verdict import …`**
    # （`tests/test_ai_verify_verdict.py` 里那条守卫每跑一次就重扫一遍）。
    CLAIM_REFUTED,  # noqa: F401
    CLAIM_STATUS_LABELS,  # noqa: F401
    CLAIM_STATUSES,  # noqa: F401
    CLAIM_UNREADABLE,  # noqa: F401
    CLAIM_UNVERIFIED,  # noqa: F401
    CLAIM_VERIFIED,  # noqa: F401
    INDEPENDENT_REQUEST_TYPES,  # noqa: F401
    VERIFY_BASIS_INDEPENDENT,  # noqa: F401
    VERIFY_BASIS_LABELS,  # noqa: F401
    VERIFY_BASIS_REPLAY,  # noqa: F401
    # 下面这三个是**本层逻辑自己要用**的（裁决解析与渲染的两个入口），不是回导。
    claim_outcome,
    claims_instructions,
    parse_claim_verdicts,
)
from services.ai.protocol import Anomaly, DroppedItem, parse_json_candidates
from services.ai.ref_shapes import is_locatable_ref
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
# 取值域、文案与数据类搬去了 `verdict_types`（本文件贴着 2000 行 ERROR 闸门）。
# **逐个回导，不用 `import *`**：读侧与测试按 `verdict.X` 取用这些名字（`family_ledger`
# 取裁决码与节名、`subagent` 取 `VERIFY_BASIS_*` 的邻居、多个测试取中文名表），
# 回导让那些说法一个字都不用改。**新增常量要加进这一份**，否则老读法 ImportError。
# --------------------------------------------------------------------------
from services.ai.verdict_types import (  # noqa: F401 —— 搬家的兼容出口，本文件未必每个都用
    _BODY_LABEL_RE,
    _BODY_WINDOW_CHARS,
    _BODY_WINDOW_LINES,
    _FILE_LABEL_KINDS,
    _MIN_MATCH_CHARS,
    _REASON_MAX_CHARS,
    _REFS_MAX_ITEMS,
    _SCOPE_MAX_CHARS,
    _VERDICT_ALIASES,
    CONFIDENCE_CEILING_WITH_GAP,
    KIND_VERIFY,
    NEW_FINDING_PREFIX,
    QUOTA_EXHAUSTED_CODE,
    RULING_BLOCK_MARKER,
    RULING_SUMMARY_NAME,
    RULING_SUMMARY_TITLE,
    RULING_TITLE,
    SEVERITY_STEP_DOWN,
    SOURCE_SYNTHESIS,
    SOURCE_VERIFY,
    UNREVIEWED,
    UNREVIEWED_LABEL,
    VERDICT_CONFIRMED,
    VERDICT_DOWNGRADED,
    VERDICT_LABELS,
    VERDICT_NEEDS_MORE_EVIDENCE,
    VERDICT_RETRACTED,
    VERDICTS,
    Finding,
    FindingRow,
    Reduction,
    VerifyVerdict,
    severity_step_down,
)

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
# ③ 依据的形状校验 —— **已搬到 `services/ai/ref_shapes.py`**
# --------------------------------------------------------------------------
#
# 它的第二个读者是 `claims.claim_review_of`（一条原子断言的依据能不能核），两处必须用
# 同一份判据。`is_locatable_ref` 在本模块**重新导出**（顶部那份 import）—— 既有的导入点
# 与测试都按本模块的名字用它。




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
                claims=parse_claim_verdicts(entry),
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

    内容已经由平台渲染成「复核标注（平台）」那一节（而且是**按裁决结果**渲染的），
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
    verify_independent: bool = False,
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

    `verify_independent`（P0-01）由调用方按**对账轮实际执行过的取数类型**判定
    （`subagent.verify_did_independent_work`），**不采信模型自述**：它决定每一行上的
    「独立取证 / 原证据复读」。默认 `False` = 按复读记 —— 这是**保守**的那一侧：
    平台没看到新检索时，不该把「重看了一遍已有依据」说成独立反证核验。
    """
    findings = assign_findings(base)
    labels = assign_body_labels(body_text, findings)
    rows = [
        replace(
            _row_of(finding, verdicts, verify_independent=verify_independent),
            body_label=labels.get(finding.finding_id, ""),
        )
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
                    verdict.finding_id,
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
                    anomaly.title or "（无标题）",
                )
            )
            continue
        if duplicate is not None:
            reason = (
                f"与**刚被复核撤销**的「{duplicate.anomaly.title}」是同一个问题，需要人工判一下到底撤不撤"
                if not duplicate.active
                else f"与清单里已有的「{duplicate.anomaly.title}」重复"
            )
            rejected.append(
                DroppedItem(
                    KIND_VERIFY,
                    index,
                    reason,
                    anomaly.title,
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


def _row_of(
    finding: Finding, verdicts: Sequence[VerifyVerdict], *, verify_independent: bool = False
) -> FindingRow:
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
    return _apply_verdict(
        finding.finding_id, origin, verdict, verify_independent=verify_independent
    )


def _apply_verdict(
    finding_id: str,
    origin: Anomaly,
    verdict: VerifyVerdict,
    *,
    verify_independent: bool = False,
) -> FindingRow:
    """把一条裁决落到结论上。**不完整或不可信的裁决一律降格为「证据不足」**。

    降格而不是「按原样采信」：后者正是这条缺陷的复现路径（模型说了降级/撤销，清单里
    仍是 `critical`）。降格之后平台至少不再按 `very_high` 采信它，并在报告里写明原因。

    出口统一过一道 `_with_ref_shapes`（口径 ③）：`evidence_refs` 是模型写的自由文本，
    形状校验与「有没有收到裁决」无关 —— 每条路径都得出这一道。

    **候选血缘也在这里补一道**（不逐分支写）：下面每个分支都是拿 `origin` 建行的，
    血缘在 `origin` 里；逐分支各写一次迟早会漏掉一个分支，而漏掉的后果是「这条候选的
    去向查不到」——正是 AI-P0-06 要修的那一类假缺口。

    **断言裁决在整条裁决之后**（`_apply_claims`）：它只做两件既有逻辑做不到的事 ——
    「有断言没被证实 ⇒ 不得 confirmed」（向上单向），以及把断言状态与取证方式挂到行上。
    顺序是先 `_apply_claims` 再 `_with_ref_shapes`：前者可能用 `_needs_more` **重建**一行，
    而形状校验必须盖到最终那一行上。
    """
    row = _apply_claims(
        _apply_verdict_inner(finding_id, origin, verdict),
        verdict,
        verify_independent=verify_independent,
    )
    row = _with_ref_shapes(row)
    if row.source_candidate_ids == origin.source_candidate_ids:
        return row
    return replace(row, source_candidate_ids=origin.source_candidate_ids)


def _apply_claims(
    row: FindingRow, verdict: VerifyVerdict, *, verify_independent: bool
) -> FindingRow:
    """逐条断言裁决的落点（P0-01）。判据本身在 `services/ai/claims.py`（纯函数）。

    ## 为什么「有一个断言没证实」就不许 `confirmed`

    这是这一批缺陷的**唯一一条硬规则**：`confirmed` 在报告里的意思是「有人去找过反证、
    没找到」，而一条含未证实断言的结论**没有资格**这么说（run 58 的 F3：标题断言
    「断言中断整个进程」，复核在理由里承认这一点没核实，整条仍是 `confirmed`）。

    不能 `confirmed` 时降为「证据不足」而不是「撤销」：没证实 ≠ 是错的。等级仍按既有
    口径降一档、置信度不再维持 `very_high`（`_needs_more`），把「这条结论的全部或一部分
    还没立住」体现在清单本身上，而不是只写在说明里。

    ## 反证成立也一样挡住 `confirmed`，但同样**不自动撤销**

    断言被证伪说明这条结论至少有一部分是错的。平台**不替人做「撤掉整条」这个决定**
    （撤销会让它不进异常表、不进下一轮基线），而是降为待人工核验并在那一行里点名是哪条
    断言被证伪了。模型自己给 `retracted` 时照旧撤销（那条路在上面）。

    ## 没有断言的结论（旧形态）

    它没带 `claims[]`，平台无从逐项裁决 —— 于是它**不得算已核实**，但也**不因此改等级**：
    那是模型的协议偏差，不是这条结论本身的问题。改等级要有一个说得出的理由。
    **取证方式照写**：那一栏说的是「这一轮复核是怎么做的」，与有没有断言无关。
    """
    outcome = claim_outcome(
        row.origin.claims,
        verdict.claims,
        origin_title=row.origin.title,
        verify_independent=verify_independent,
    )
    if not row.origin.claims:
        return replace(row, verify_basis=outcome.basis)

    attached = {
        "anomaly": replace(row.anomaly, title=outcome.title),
        "claim_reviews": outcome.reviews,
        "verify_basis": outcome.basis,
    }
    if not outcome.blocked or row.verdict != VERDICT_CONFIRMED:
        return replace(row, **attached)

    downgraded = _needs_more(
        row.finding_id,
        row.origin,
        row.reason,
        row.evidence_refs,
        note=outcome.note,
    )
    return replace(
        downgraded,
        anomaly=replace(downgraded.anomaly, title=outcome.title),
        claim_reviews=outcome.reviews,
        verify_basis=outcome.basis,
    )


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
                f"{row.anomaly.severity} {row.anomaly.title}",
            )
        )
    return tuple(kept)


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

_VERDICT_SCHEMA_SAMPLE = (
    '{"verdicts": [\n'
    '  {"finding_id": "F1", "verdict": "retracted", "reason": "<反证是什么：哪个文件哪一行>",\n'
    '   "evidence_refs": ["<文件:行>"],\n'
    '   "claims": [{"claim_id": "C1", "status": "refuted", "basis": "independent",\n'
    '     "checked_scope": {"paths": ["<哪个文件>"]}, "reason": "<那一行说明了什么>"}]},\n'
    '  {"finding_id": "F2", "verdict": "downgraded", "final_severity": "high",\n'
    '   "reason": "<为什么不再是 critical>",\n'
    '   "claims": [{"claim_id": "C2", "status": "verified", "basis": "independent",\n'
    '     "checked_scope": {"paths": ["<哪个文件>"], "symbols": ["<查过的标识符>"]}}]},\n'
    '  {"finding_id": "F3", "verdict": "needs_more_evidence",\n'
    '   "reason": "<还缺什么证据>",\n'
    '   "claims": [{"claim_id": "C3", "status": "verified", "basis": "independent"},\n'
    '     {"claim_id": "C4", "status": "unverified", "reason": "<为什么核不了>"}]},\n'
    '  {"finding_id": "F4", "verdict": "confirmed", "reason": "<查了哪里，没找到反证>",\n'
    '   "claims": [{"claim_id": "C5", "status": "verified", "basis": "replay"}]}\n'
    "]}"
)


def verdict_instructions() -> str:
    """任务书里那一段「结构化裁决」（`subagent.build_verify_task` 拼进去）。

    **这一段是整条链路的前提**：平台只按这个 json 块决定某条结论是保留、降级还是撤销。
    所以它必须写清楚两件事：形状（照抄）、以及「没有这一块会发生什么」—— 后者不写，
    模型会以为正文那段话就够了（实测就是这么写的），而平台不会替它猜。

    ## 逐条断言那一段（P0-01）

    上面每一条结论给了断言清单（编号 `C1`…）。**逐条回答**是这次协议的硬要求，理由写在
    `_CLAIM_STATUSES_HELP` 里：一条结论可以由几条**互不相干**的事实组成，整条回答会让
    「其中一条没核实」变成「整条已核实」—— 实测 run 58 的 F3 就是这么被标成 confirmed 的。
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
        + claims_instructions()
        + "**这一块是平台唯一的依据**：正文写得再明确，没有这一块，你这一轮复核对结论"
        "**一条都不生效**（平台不替你猜）。反过来，写了这一块它就会**直接改结论** ——"
        "所以只在反证真的成立时才写 `retracted` / `downgraded`。\n"
    )


# --------------------------------------------------------------------------
# 回导：渲染那一半搬去了 `verdict_render`（本文件贴着 2000 行 ERROR 闸门）。
# 读侧与测试按 `verdict.render_ruling` 等取用（`subagent` / `result_payload` / 多个测试），
# 这里**逐个回导**保住那些说法。**必须放在文件底部**：`verdict_render` 顶部要 import
# `verdict_types`，而本文件顶部若反过来 import 它，两者就成环（谁先被导入都炸）。
# **新增给人看的渲染函数要加进这一行。**
# --------------------------------------------------------------------------
from services.ai.verdict_render import (  # noqa: E402, F401, I001 —— 见上面那段
    render_review_skipped,
    render_ruling,
    render_ruling_summary,
    retracted_fingerprints,
    ruling_rows,
    strip_ruling_block,
)
