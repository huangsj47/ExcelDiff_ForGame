"""子代理家族的**数据模型**与**平台侧对账** —— 谁交回了什么、谁没交回、候选去哪儿了。

## 为什么单独一个文件

`services/ai/subagent.py` 一直顶在 `scripts/check_file_length.py --strict` 的 2000 行
门槛上（搬出这一块之前是 2007 行，已经报 ERROR）。这里装的是那个文件里**与「怎么跑」无关**
的另一半：分片计划怎么描述、成员交回什么、平台事后怎么清点。搬家只挪位置、不改行为，
`subagent.py` 原处保留了回导，`services.ai.subagent.X` 这个引用面一个字没变。

## 这一块在管什么

**一、数据模型。** `MemberPlan` / `MemberOutcome` / `Candidate` / `FamilyResult` 是
「计划 → 执行 → 交回 → 汇总」四个阶段之间传的东西。它们与引擎无关：`subagent.py` 把
它们喂给引擎、再从引擎的返回里填回来，两边都只认字段名。

**二、平台侧对账（`reconcile_candidates`）。** 一个成员报了 N 条候选，主代理汇总时
可能只采纳了一部分 —— **平台不看模型怎么说，自己去正文与结论清单里核对**：这条候选的
编号、文件、结论，有没有落到最终报告里。这是「保证不建立在模型听话上」的落点，
`subagent.py` 只负责把它算出来的缺口文字拼进报告。

**三、「谁没交回结论」的文字（`_gap_lines` / `_shard_gap_lines` / `_verify_gap_lines`）。**
跑失败、被预算跳过、跑完但没按协议交回结论 —— 三种都要在报告里写成信息缺口，
绝不许表现为「那个维度没问题」。它们与对账同属一本账，所以跟对账放在一起。

## 与其余部分为什么耦合低

搬走的这 27 个定义只依赖标准库与 `services.ai.protocol` 那几个数据类型，**不 import**
本包的引擎、提示词、预算模块；反过来 `subagent.py` 用到的每一个名字都由它 import 回去。
唯一的例外是 `reconcile_candidates` 要调 `_shard_gap_lines` —— 那两个函数也在这份
MOVE 里，所以是同一个文件内部的调用，不构成往返依赖。

角色常量（`ROLE_*`）与候选条数上限（`CANDIDATE_*`）一并搬过来：数据模型自己要用它们，
留在原文件反而要反向 import。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from services.ai.budget import truncate_text
from services.ai.engine import (
    DEGRADATION_LABELS,
    DEGRADE_MARKDOWN,
    DEGRADE_PROTOCOL,
    STATUS_FAILED,
    EngineOutcome,
)
from services.ai.protocol import Anomaly, DroppedItem
from services.ai.rules import KIND_ANOMALY_CAP
from services.ai.scope import normalize_path

ROLE_SUBAGENT = "subagent"
ROLE_SYNTHESIS = "synthesis"
# 对账轮（找反证）。它**不是分片**：它读的是汇总之后的报告，要干的事是反驳。
ROLE_VERIFY = "verify"
# 对账轮在 trace 与面板上的标签（与 `S1..S6` 同一套编号法）。
VERIFY_LABEL = "V1"
# 「跑完了，但结构化结论没交回来」的两种降级：模型没按协议出 JSON（按 markdown 降级保存），
# 或者纠正到上限仍然不合协议。两种情况下**它报的结论一条都没进清单**，所以也算覆盖缺口
# （见 `_shard_gap_lines`）。**不含**轮次/索取/上下文耗尽那几种：那些是「提前收工」，
# 交回来的结论照旧进清单，只是没看全。
NO_STRUCTURED_OUTCOME = frozenset({DEGRADE_MARKDOWN, DEGRADE_PROTOCOL})
# 任务书里列「别的成员负责什么」与候选结论时的字符上限。它们都进了提示词，而提示词的
# 每一段都要与上下文条目抢同一份额度。
CANDIDATE_BLOCK_MAX_CHARS = 24_000
CANDIDATE_MAX_ITEMS_PER_MEMBER = 30
MEMBER_MARKDOWN_EXCERPT_CHARS = 2_000
# 每条候选进任务书时的字段上限（证据、影响、建议都可能很长）。
CANDIDATE_TEXT_MAX_CHARS = 400
# 平台侧对账用的编号前缀。模型被要求在采纳某条候选时把编号带进它的 `evidence`。
CANDIDATE_ID_PREFIX = "S"
# 维度怎么分给 n 个成员：**按清单里的相邻顺序均分**，不是一张写死的表。
#
# 原先这里是一张手工表（2~6 组各写一行），它有两个问题，都在「维度集合一旦项目化」时
# 同时发作：
#
# 1. **表必然作废。** 它是照着平台出厂那九个维度写的；项目声明了自己的清单
#    （`LoadedSkills.dimensions`）之后，表里的 id 一个都对不上。
# 2. **查不到就静默退化。** 原实现在表里查不到档位时返回「一个成员全看完」——用户开了
#    6 个分片、花了 7 倍的钱，拿到的是单代理的效果，而没有任何提示。
#
# 现在按**顺序相邻**切（`skill_contract` 里写明了这个顺序是有意义的：相邻即相关），
# 于是任何一份清单都能分，且分法是确定的：`ceil/floor` 的余数摊给**前面**几组
# （9 个维度切 4 组 → 3,2,2,2）。三条性质都有测试钉着：每组非空、每组是一段连续的
# 维度、全部维度恰好出现一次。


@dataclass(frozen=True)
class MemberPlan:
    """一个成员（子代理或汇总那一次）要怎么跑。"""

    index: int
    label: str
    role: str
    dimensions: tuple[str, ...]

    @property
    def is_synthesis(self) -> bool:
        return self.role == ROLE_SYNTHESIS


@dataclass(frozen=True)
class MemberOutcome:
    """一个成员跑完之后的账。`outcome is None` 表示它压根没跑（被跳过 / 还没轮到）。"""

    plan: MemberPlan
    outcome: EngineOutcome | None = None
    error: str = ""
    skipped_reason: str = ""
    # 进任务书的那批候选（已封顶，见 `CANDIDATE_MAX_ITEMS_PER_MEMBER`）。
    candidates: tuple["Candidate", ...] = ()
    # **它一共报了多少条**（封顶之前的数）。封顶了却不说，读的人会以为它只报了这么多 ——
    # 而那正好是「这次怎么报得这么少」最容易走偏的方向。
    candidate_total: int = 0

    @property
    def ran(self) -> bool:
        return self.outcome is not None

    @property
    def failed(self) -> bool:
        return self.error != "" or (
            self.outcome is not None and self.outcome.status == STATUS_FAILED
        )


@dataclass(frozen=True)
class Candidate:
    """一个成员报出的一条候选结论。**编号由平台发出去**，模型采纳时把它带回来。"""

    member_label: str
    index: int
    anomaly: Anomaly

    @property
    def id(self) -> str:
        return f"{CANDIDATE_ID_PREFIX}{self.member_label.lstrip('S')}-{self.index}"

    def describe(self) -> str:
        item = self.anomaly
        parts = [
            f"[{self.id}] {item.title}",
            f"  维度：{item.category} · 严重度 {item.severity} · 置信度 {item.confidence}",
        ]
        if item.file_path:
            parts.append(f"  文件：{item.file_path}{(' @ ' + item.commit[:12]) if item.commit else ''}")
        if item.impact:
            parts.append(f"  影响：{truncate_text(item.impact, CANDIDATE_TEXT_MAX_CHARS)[0]}")
        for evidence in item.evidence[:3]:
            parts.append(f"  证据：{truncate_text(str(evidence), CANDIDATE_TEXT_MAX_CHARS)[0]}")
        return "\n".join(parts)


@dataclass(frozen=True)
class FamilyResult:
    """一家子跑完之后的全部产出。`outcome` 是**合成出来的那一个**，直接交给落库。"""

    outcome: EngineOutcome
    steps: tuple[MemberOutcome, ...] = ()
    candidates: tuple[Candidate, ...] = ()

    @property
    def members(self) -> tuple[MemberOutcome, ...]:
        return tuple(step for step in self.steps if not step.plan.is_synthesis)

    @property
    def skipped(self) -> tuple[MemberOutcome, ...]:
        return tuple(step for step in self.steps if not step.ran and step.skipped_reason)

    @property
    def gaps(self) -> tuple[str, ...]:
        """哪些成员没跑成、为什么。报告与面板都用它 —— 空表示这一家子跑齐了。"""
        return _gap_lines(self.steps)


def _markdown_excerpt(outcome: EngineOutcome) -> str:
    text = (outcome.report_markdown or "").strip()
    if not text:
        return ""
    clipped, truncated = truncate_text(text, MEMBER_MARKDOWN_EXCERPT_CHARS)
    return clipped + ("…（截断）" if truncated else "")


def _gap_lines(steps: Sequence[MemberOutcome]) -> tuple[str, ...]:
    """哪个成员没跑成、为什么。**报告与面板共用这一份措辞**（两处各写一遍必然对不上）。

    分片与对账轮**分成两组**（见下面两个函数）：它们没跑成的后果不是一回事，写在一起
    会让「有一块维度没人看过」与「结论没经过复核」混成同一句话 —— 而前者要按缺口处理，
    后者只是少了一道复核。
    """
    return (*_shard_gap_lines(steps), *_verify_gap_lines(steps))


def _shard_never_ran(steps: Sequence[MemberOutcome]) -> bool:
    """有分片压根没跑（被跳过 / 没跑成）—— 这一条决定要不要记 `subagent_gap` 降级。

    **与 `_shard_gap_lines` 的第三类（跑完了但结论没按协议交回）无关**，这是刻意的：
    那一种自己就带着一个更具体的降级原因（`markdown_report` /
    `protocol_corrections_exhausted`），再套一层「子代理模式：有分片没有跑成」只会把
    「模型没按协议出 JSON」这个能直接对症的原因盖掉。两种都是降级，读的人两种都会看到。
    """
    return any(
        step.plan.role != ROLE_VERIFY and bool(step.skipped_reason or step.failed)
        for step in steps
    )


def _shard_gap_lines(steps: Sequence[MemberOutcome]) -> tuple[str, ...]:
    """分片（含汇总）没跑成 —— 它负责的维度这一次**没有人看过**。

    ## 「跑完了但结论没交回来」也算缺口（2026-09-20 加）

    原来只认「未运行 / 没跑成」。实测那次（run 6）S3 跑满 5 轮、**最后一轮没按协议出
    JSON**，平台按 markdown 降级保存 —— 于是它那一路的**结构化结论是 0 条**，而报告末尾
    一个字的缺口都没有。它负责的三个维度（`code_logic`/`version_branch`/`process`）
    在清单里看起来是「看过、没问题」，实际是「结论没回来」。

    措辞刻意不写「没有人看过」：它看过了，只是没交回来；也不写「没有发现问题」——
    那正是这条要防的读法。降级保存的正文里可能有它的发现（汇总的任务书里有它的片段），
    所以最后一句是让人去核对，而不是宣布这一块空白。
    """
    lines: list[str] = []
    for step in steps:
        dimensions = "、".join(step.plan.dimensions)
        if step.plan.role == ROLE_VERIFY:
            continue
        if step.skipped_reason:
            lines.append(
                f"- {step.plan.label} **未运行**：{step.skipped_reason}；"
                f"它负责的维度（{dimensions}）本次**没有人看过**。"
            )
        elif step.failed:
            reason = step.error or (
                step.outcome.error_message if step.outcome else ""
            ) or "没有给出可用结论"
            lines.append(
                f"- {step.plan.label} **没有跑成**：{reason}；"
                f"它负责的维度（{dimensions}）本次**没有人看过**。"
            )
        elif step.outcome is not None and step.outcome.degradation in NO_STRUCTURED_OUTCOME:
            label = DEGRADATION_LABELS.get(step.outcome.degradation, step.outcome.degradation)
            lines.append(
                f"- {step.plan.label} **跑完了，但结论没有按协议交回**（{label}）；"
                f"它负责的维度（{dimensions}）这一次**没有结构化结论进入清单** —— "
                "降级保存的正文里可能有它的发现，请人工核对，不要当成「没有问题」。"
            )
    return tuple(lines)


def _verify_gap_lines(steps: Sequence[MemberOutcome]) -> tuple[str, ...]:
    """对账轮没跑成 —— 它**没有负责的维度**，没跑成的后果是「结论没经过复核」。

    所以这里绝不能套上面那句「它负责的维度（空）没有人看过」：那是一句**错的**话
    （会读成「有一块维度没人看过」），而这两件事该被怎么处置完全不同。
    """
    lines: list[str] = []
    for step in steps:
        if step.plan.role != ROLE_VERIFY:
            continue
        if not (step.skipped_reason or step.failed):
            continue
        reason = step.skipped_reason or step.error or (
            step.outcome.error_message if step.outcome else ""
        ) or "没有给出可用结论"
        lines.append(
            f"- {step.plan.label}：{reason}；报告的结论"
            "**没有经过「找反证」这一道**，读的时候按原样看。"
        )
    return tuple(lines)


def reconcile_candidates(
    candidates: Sequence[Candidate],
    synthesis: EngineOutcome,
    *,
    steps: Sequence[MemberOutcome] = (),
) -> tuple[str, tuple[DroppedItem, ...]]:
    """平台侧自己查一遍：哪些候选**没有进入最终报告**、哪些成员根本没跑成。

    ## 为什么这件事不能交给模型

    「主代理把某个分片的发现一声不响地丢掉了」是这条链路上**最危险的失真**：报告看起来
    完全正常，只是少了一条，而没有任何人会去数。所以平台自己查，并把它写进报告末尾的
    「信息缺口（平台补充）」——**这条保证不建立在模型听话上**。

    ## 怎么算「进了最终报告」

    **三手**，任一手成立就算采纳：

    1. 候选编号（`[S1-2]`，平台发出去、要求模型采纳时带回）出现在最终报告或异常清单里；
    2. 最终异常清单里有**同一个文件**的条目（模型经常复述内容而不带编号）；
    3. 最终异常清单里**某一条的标题/证据/影响面里点了这个文件的名** —— 也就是
       「同一个文件」那一手的放宽版：一条结论只带**一个** `file_path` 字段，而它讨论的
       往往不止一个文件。

    第 3 手是 2026-09-20 加上的，起因是一次真实的**误报**（线上跑出来的那次周版本分析）：
    分片 S1 报的候选是「`config/奖励模式_CfgRewardMode.xlsx` 新建表内仅含一条测试数据、
    与两份同域表并存」，而汇总把它并成了一条 `file_path = config/奖励模式表_CfgRewardMode.xlsx`
    的结论 —— **它自己的证据第三条逐字写着 `config/奖励模式_CfgRewardMode.xlsx`**。
    候选明明被采纳了，前两手却都看不见：编号没带回、`file_path` 又不是同一个。
    于是平台在报告末尾告诉用户「1 条找不到去向，需要人工看一眼」，而那条就在报告里。

    三手都不成立才算没进 —— 而写进报告的那句话会**如实说明查的是什么**，
    不是断言「模型丢了它」。

    ## 每一手都必须比「同一个东西」，而不是比字符串

    这三手各自都有一个**看起来很省事但会判错**的写法：

    * **编号不能当子串找**。`candidate.id` 形如 `S1-2`，而同一分片的编号可以到
      `S1-30`（`CANDIDATE_MAX_ITEMS_PER_MEMBER`）—— `"S1-2" in 报告` 会被报告里的
      `[S1-25]` 命中。这是一个**漏报**：一条真的没被汇总进去的候选从此不出现，
      而这正是本函数存在的唯一理由（见上面「为什么这件事不能交给模型」）。
    * **路径要比归一化后的形态**。逐字相等时，`./config/a.xlsx` 与
      `config\\a.xlsx` 是两个文件。这是一个**误报**：已经进了报告的候选被说成
      「找不到去向」，读的人只好再去核一遍，而那个计数会虚高。
    * **第 3 手的路径不能不管边界**。`a/b.xlsx` 是 `x/a/b.xlsx` 的后半段，
      裸子串匹配会让后者替前者「认领」这条候选。所以两侧都挡住路径的续接字符
      （与 `_candidate_id_mentioned` 挡住 `S1-25` 是同一件事）。

    ## 第 3 手为什么**只看结论**、不看报告正文

    `_report_text` 里还有报告正文，正文里出现一个文件名看起来也能算「提到了」。不采纳
    这条路：报告正文正是模型写「这一块我没查到」的地方（那次真实运行里，
    `奖励模式_CfgRewardMode.xlsx` 就出现在模型自己那节「信息缺口（需人工核对）」里）。
    拿「正文提到过」当采纳证据，会把**真的缺口**说成「已有去向」—— 而这个方向是静默的，
    正是本函数最不该出的那种错。

    前者静默、后者吵闹，所以前者更要紧；但两者都不该发生。

    ## 去向已经写明的候选，不进这份名单（2026-09-20 加）

    那天的实测里三条候选被报成「找不到去向」，而**三条的去向其实都写着**：

    * 两条**被条数上限截掉了** —— 它们进了结论清单、只是没有位置，而报告里另有一节
      （`build_cap_section`）逐条列着它们。同一个东西在两节里各出现一次，而这两节的语义
      是相反的（那一节是「看到了、没位置」，这一节是「没看到」）—— 读的人只会觉得平台的
      账自相矛盾。现在按文件名认出它们（`_cap_details`）并单独计数。
    * 一条被模型**有意写成了「待取证假设」**（逐字给了那次 diff、也写了缺什么证据）。
      这一条仍然记账 —— 正文不是采纳证据 —— 但 `_gap_line` 会照实说「正文里出现过这个
      文件，请确认是有意降级还是被漏掉」。读的人据此知道该去补证据还是该去追汇总。
    """
    adopted = _report_text(synthesis)
    findings_text = _findings_text(synthesis)
    # 正文**只用来给条目加一句说明**（「正文里出现过这个文件」），不当采纳证据 ——
    # 理由见上面「第 3 手为什么只看结论、不看报告正文」。
    body_text = synthesis.report_markdown or ""
    cap_texts = _cap_details(synthesis.dropped)
    final_paths = {
        normalize_path(item.file_path)
        for item in synthesis.anomalies
        if normalize_path(item.file_path)
    }
    lines: list[str] = []
    dropped: list[DroppedItem] = []
    capped_explained = 0
    for candidate in candidates:
        if _candidate_id_mentioned(candidate.id, adopted):
            continue
        path = normalize_path(candidate.anomaly.file_path)
        if path and path in final_paths:
            continue
        if path and _path_named_in_findings(path, findings_text):
            continue
        if path and any(_path_named_in_findings(path, text) for text in cap_texts):
            # 去向已知：**被条数上限截掉了**，报告里另有一节逐条列着它。不记进这一节 ——
            # 同一批条目在两节里各出现一次，读的人会以为是两回事：那一节说的是「看到了、
            # 只是没位置」，这一节说的是「没看到」。这正是 `build_cap_section` 的
            # docstring 里写明「两节不能合成一节」的那个区别，反过来也不能重复报。
            capped_explained += 1
            continue
        in_body = bool(path) and _basename_named_in_text(path, body_text)
        dropped.append(
            DroppedItem(
                kind="subagent",
                index=candidate.index,
                reason="汇总报告里找不到这条候选的去向",
                detail=f"[{candidate.id}] {candidate.anomaly.title}"[:300],
            )
        )
        lines.append(_gap_line(candidate, in_body=in_body))

    shard_gaps = _shard_gap_lines(steps)
    verify_gaps = _verify_gap_lines(steps)
    if not lines and not shard_gaps and not verify_gaps:
        return "", ()

    blocks: list[str] = ["## 信息缺口（平台补充）"]
    if shard_gaps:
        blocks.append(
            "本次分析启用了分片代理，以下几块**没能交回结论** —— 是「压根没跑」还是"
            "「跑了但结论没交回来」，每条自己写着（后者不代表没问题，只代表这一块没结论）："
            "\n\n" + "\n".join(shard_gaps)
        )
    if verify_gaps:
        # 与上面那段分开写：对账轮没有负责的维度，把它挂在「没能交回结论的分片」下面会读成
        # 「有一块维度没人看过」，而它真正的后果是「结论没经过复核」。
        blocks.append(
            "另外，本次开着**「对账轮（找反证）」**，而它没有跑成：\n\n"
            + "\n".join(verify_gaps)
        )
    if lines:
        blocks.append(
            f"平台的账上共有 {len(candidates)} 条来自分片代理的候选结论，"
            f"其中 {len(lines)} 条**没有进入最终结论清单**。平台只核对两处落点："
            "候选编号、以及结论清单里的文件与结论 —— **正文里提到过不算采纳**"
            "（正文正是模型写「这里我没查到」的地方）。它们不因此就不成立，"
            "只是需要人工看一眼：\n\n" + "\n".join(lines)
        )
    if capped_explained:
        blocks.append(
            f"另有 {capped_explained} 条候选的缺席**已经解释过了**：它们被本次的条数上限"
            "截掉（进了结论清单、只是**没有位置**），逐条列在上面那节「结论条数上限"
            "（平台补充）」里，不重复计入这一份名单。"
        )
    blocks.append(
        "以上是平台**按记录核对**出来的，不是模型的自我说明。"
    )
    return "\n\n".join(blocks), tuple(dropped)


def _cap_details(dropped: Iterable[DroppedItem]) -> tuple[str, ...]:
    """条数上限截掉的那几条的记账文本（`detail` 里带着那条结论的文件名）。

    用来把「被上限截掉的候选」与「汇总真的漏掉的候选」分开：前者的去向在报告里
    已经逐条写着（`build_cap_section` 那一节），再报一次「找不到去向」会让同一批条目
    在两节里各出现一次，而这两节的语义是相反的。

    **按文件名匹配、不解析那串文本**：`detail` 是给人看的（`severity 标题（路径）`），
    标题里自己就带括号（`【物品表（14外观）】`），从两头切括号必然切错。这里复用
    `_path_named_in_findings` 的边界规则 —— 与它挡 `x/a/b.xlsx` 冒充 `a/b.xlsx` 同一件事。
    """
    return tuple(
        str(getattr(item, "detail", "") or "")
        for item in dropped
        if getattr(item, "kind", "") == KIND_ANOMALY_CAP
    )


def _gap_line(candidate: Candidate, *, in_body: bool) -> str:
    """「这条候选没有进入结论清单」那一行 —— **只说平台真的核对过的那两处落点**。

    ## 为什么不能写「也没有任何一条结论提到这个文件」

    那句话读起来是「整份报告里都没有这个文件」，而平台核对的只是**结论清单**
    （`_findings_text`：标题 + 证据 + 影响面）。2026-09-20 那次实测里，三条被报
    「找不到去向」的候选，文件**都在模型自己写的正文里出现过**（其中一条被写进了
    「待取证假设」）—— 读的人随手一搜就能推翻这句话，于是整节核对结论都不再可信。

    ## 正文里出现过，要单独说，而且不能当成「已采纳」

    它仍然不是采纳证据（理由见 `reconcile_candidates`）。但「有意写成待取证 / 待确认」
    与「一声不响地丢了」对读的人是两件事：前者要人去补证据，后者要人去追汇总。
    所以这里只**照实说一句在哪出现过**，把判断留给读的人。
    """
    head = (
        f"- `[{candidate.id}]` {candidate.anomaly.title}"
        f"（{candidate.anomaly.category}·{candidate.anomaly.severity}"
        + (f"，{candidate.anomaly.file_path}" if candidate.anomaly.file_path else "")
        + "）："
    )
    if in_body:
        return (
            head
            + "结论清单里没有它，但**报告正文里出现过这个文件** —— 请确认它是被有意写成了"
            "「待取证 / 待确认」，还是被漏掉了。"
        )
    return head + "报告里既没有引用这个编号，正文与结论清单里也都没有提到这个文件。"


def _candidate_id_mentioned(candidate_id: str, text: str) -> bool:
    """最终报告里有没有引用这个候选编号。**整段匹配，不当子串。**

    `candidate.id` 形如 `S1-2`（`Candidate.id`），而同一分片的编号可以到 `S1-30`
    （`CANDIDATE_MAX_ITEMS_PER_MEMBER`）—— `"S1-2" in 报告` 会被报告里的 `[S1-25]`
    命中，于是**一条真的没被汇总进去的候选不报了**。方向刚好是最坏的那个：漏报是
    静默的（报告读起来完全正常，只是少一条），而本函数存在的唯一理由就是数这一条。

    两边都卡边界：左边不能接字母数字（`[S1-2]` 与裸写的 `S1-2` 都算引用到了 ——
    模型不一定带方括号，那不该被当成没引用），右边不能接数字（挡住 `S1-25`）。
    """
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(str(candidate_id))}(?![0-9])", text) is not None


def _findings_text(synthesis: EngineOutcome) -> str:
    """**只有异常清单**的散文（标题 + 证据 + 影响面），不含报告正文。

    第 3 手匹配用的就是这一份，理由见 `reconcile_candidates` 那段「为什么只看结论」：
    报告正文里有模型自己写的「这一块我没查到」，拿它当采纳证据会把真缺口说成有去向。
    """
    return "\n".join(
        f"{item.title} {' '.join(str(text) for text in item.evidence)} {item.impact}"
        for item in synthesis.anomalies
    )


def _report_text(synthesis: EngineOutcome) -> str:
    """最终报告 + 异常清单拼成的可检索文本（判断候选编号在不在里面）。"""
    return "\n".join([synthesis.report_markdown or "", _findings_text(synthesis)])


def _path_named_in_findings(path: str, findings_text: str) -> bool:
    """结论清单里**点了这个文件的名**没有。

    两侧都挡住路径的续接字符：不加边界的话，候选的 `a/b.xlsx` 会被结论里的
    `x/a/b.xlsx` 认领 —— 一条真的没进报告的候选从此不报（静默的漏报，本函数最不该
    出的那种错）。与 `_candidate_id_mentioned` 挡 `S1-25` 是同一件事。

    尾部挡的是「更长的路径以它开头」：`a/b.xlsx.bak` 不该被当成点名了 `a/b.xlsx`，
    所以 `.` 也要挡。**代价是句号收尾的英文句子会漏掉一次匹配**（`见 a/b.xlsx.`）——
    这个方向（宁可说「找不到去向」）比反过来（把没进报告的候选说成进了）轻，取它。
    而不挡中文标点：中文写作里路径后面常常直接跟 `」`、`、`、`）`。
    """
    if not path:
        return False
    pattern = rf"(?<![A-Za-z0-9_\-./]){re.escape(path)}(?![A-Za-z0-9_\-/.])"
    return re.search(pattern, findings_text) is not None


def _basename_named_in_text(path: str, text: str) -> bool:
    """正文里点到这个**文件名**没有（只看文件名，不看目录）。

    ## 为什么不能拿全路径去比

    模型在正文里习惯只写文件名（`PackDropObjSnapshotMod.lua`），写全路径的时候少。
    2026-09-20 那次实测就是这个情形：报告正文里明明写着它，平台却照旧说「正文里也
    没有提到这个文件」—— 而那句话读的人随手一搜就能推翻。

    ## 边界

    左边挡字母数字与点（`MyX.lua`、`x.X.lua` 都是别的文件），**放过 `/`** ——
    正文写全路径 `a/b/X.lua` 也要算点到名。右边挡得更严（含 `.` 与 `-`），
    否则 `X.lua.bak` 会替 `X.lua` 认领。

    **只给「这一行该怎么措辞」用，不参与采纳判定。** 代价是「另一个目录下的同名文件」
    会被认成它：这一处认错只是让人多看一眼，而漏认的代价是一句可以被随手证伪的话。

    采纳判定（三手）用 `_path_named_in_findings` 的全路径比对，那里的取舍相反 ——
    见 `reconcile_candidates` 的「每一手都必须比「同一个东西」」。
    """
    name = path.rsplit("/", 1)[-1]
    if not name:
        return False
    pattern = rf"(?<![A-Za-z0-9_.]){re.escape(name)}(?![A-Za-z0-9_\-/.])"
    return re.search(pattern, text) is not None
