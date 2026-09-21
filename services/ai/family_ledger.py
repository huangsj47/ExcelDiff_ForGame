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
可能只采纳了一部分 —— **平台不看模型怎么说，自己去正文、结论清单与复核裁决里核对**：
这条候选的编号、文件、结论，有没有落到最终报告里；没有进去的话，它的去向是什么（复核
撤销 / 降级 / 转人工核验 / 被条数上限截掉 / 真的没有落点）。这是「保证不建立在模型听话
上」的落点，`subagent.py` 只负责把它算出来的缺口文字拼进报告。

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

from dataclasses import dataclass
from typing import Sequence

from services.ai.budget import truncate_text
from services.ai.engine import (
    DEGRADATION_LABELS,
    DEGRADE_MARKDOWN,
    DEGRADE_PROTOCOL,
    STATUS_FAILED,
    EngineOutcome,
)
from services.ai.protocol import Anomaly, DroppedItem
from services.ai.verdict import (
    VERDICT_DOWNGRADED,
    VERDICT_NEEDS_MORE_EVIDENCE,
    VERDICT_RETRACTED,
    FindingRow,
    Reduction,
)

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


# 复核裁决里**改变了候选去向**的那三种。`confirmed` 不在其中：它维持原状，那条结论照旧
# 在清单里，与「已采纳」是同一件事，不需要另说一遍。
#
# 次序即优先级（见 `_ruling_fate`）：撤销 → 降级 → 待人工核验。
RULING_FATES = (VERDICT_RETRACTED, VERDICT_DOWNGRADED, VERDICT_NEEDS_MORE_EVIDENCE)


def reconcile_candidates(
    candidates: Sequence[Candidate],
    synthesis: EngineOutcome,
    *,
    steps: Sequence[MemberOutcome] = (),
    reduction: Reduction | None = None,
) -> tuple[str, tuple[DroppedItem, ...]]:
    """平台侧自己查一遍：哪些候选**没有进入最终报告**、哪些成员根本没跑成。

    ## 为什么这件事不能交给模型

    「主代理把某个分片的发现一声不响地丢掉了」是这条链路上**最危险的失真**：报告看起来
    完全正常，只是少了一条，而没有任何人会去数。所以平台自己查，并把它写进报告末尾的
    「信息缺口（平台补充）」——**这条保证不建立在模型听话上**。

    ## 判据只有一条：**候选编号**（AI-P0-06，2026-09-21 换的）

    每个候选有平台发出去的不可变编号（`Candidate.id`，形如 `S1-3`），而汇总那一次的任务书
    要求每条结论把它的来源编号写进 `source_candidate_ids`（一对多、多对一都允许，
    见 `build_synthesis_task`）。平台做的是**两个集合的比对**：

    * 编号落在**最终清单**（活动的那些结论）上 → 已采纳；
    * 编号落在**本次复核裁决**改变过的那几条上 → 去向是「已撤销 / 已降级 / 转人工核验」；
    * 编号在汇总交回的那一组里、但没有落到最终清单上（被条数上限截掉、未达门槛、与已有
      条目判为同一问题）→ 去向在平台自己那几本账里，本节不重复计入；
    * 编号**一条都没出现在汇总交回的结论里** → 真缺口，逐条报，并记进 `dropped`
      （它会触发 `subagent_gap` 降级）。

    ## 换掉的那三手（以及它们为什么必须走）

    原先这里从自然语言反推血缘，三手任一手成立就算采纳：候选编号出现在报告正文里 /
    最终清单里有一条同文件的结论 / 结论的标题·证据·影响面里点了候选的路径名。三手在
    真机上产生的全是**假缺口**：run 20 的 `S3-3`（`ProtoScsGas.lua`）与最终结论 `F5`
    （`ProtoCGas.lua`，**同名协议拆在两个文件里**）讨论的是同一个问题，标题措辞也不同，
    `F5` 的 `evidence_refs` 还是空的 —— 三手一条都没挂上，于是 4 条候选被报成「找不到
    去向」，而那 4 条假缺口又是那一次 `subagent_gap` 降级的**唯一**触发源。

    反推的另一面同样致命：它会把**真的缺口**说成「已有去向」（同文件即算采纳），
    而那个方向是静默的。显式血缘两头都堵上。

    ## 一条编号都没交回时：说一次，不报 N 条

    模型没按 schema 交回 `source_candidate_ids` 时，**不逐条报假缺口**（run 20 的成因），
    而是如实说一句「本次汇总没有交回候选血缘，无法按编号对账」——**一次**，并且不产生
    `dropped` 记录（否则整次运行仍会被判成 `subagent_gap` 降级）。

    ## 去向已经写明的候选，不进这份名单

    被复核撤销 / 降级 / 转人工核验的那几条由 `_ruling_line` 各成一段如实说（措辞与
    「复核裁决（平台）」那一节逐字一致）；被平台自己截掉或合并的那几条另有一句汇总
    （它们的记账在「结论条数上限（平台补充）」那一节与运行轨迹里）。同一件事在报告里
    出现两遍、而两遍说法不同，正是这一批缺陷的形态。

    `reduction` 由调用方（`subagent.aggregate_outcomes`，它已经算过这一份）传进来 ——
    **同一个对象，不在这里重算一遍**：两处各算一次，迟早会出现「对账说撤销、落库说还在」。
    """
    # 汇总交回了哪些编号、其中哪些落到了最终清单上。
    submitted = _submitted_candidate_ids(synthesis, reduction)
    landed = _landed_candidate_ids(synthesis, reduction)
    lines: list[str] = []
    dropped: list[DroppedItem] = []
    settled = 0
    # 复核裁决那三种去向各攒一段（`_ruling_blocks`）。撤销与降级**分开攒**：前者是移出
    # 清单、后者是还在清单里，读的人对这两件事的处置不一样。
    ruling_lines: dict[str, list[str]] = {verdict: [] for verdict in RULING_FATES}
    for candidate in candidates:
        fate = _ruling_fate(candidate, reduction)
        if fate is not None:
            verdict, row = fate
            ruling_lines[verdict].append(_ruling_line(candidate, verdict, row))
            continue
        if candidate.id in landed:
            continue
        if candidate.id in submitted:
            # 汇总交过这个编号，但它没有留在最终清单里 —— 平台在这一侧把它处置掉了
            # （超出条数上限 / 未达门槛 / 与已有条目判为同一问题）。那几本账各自逐条记着，
            # 这里只汇总一句，不重复计入这份名单。
            settled += 1
            continue
        dropped.append(
            DroppedItem(
                kind="subagent",
                index=candidate.index,
                reason="汇总交回的结论里没有一条声明来源于这条候选",
                detail=f"[{candidate.id}] {candidate.anomaly.title}"[:300],
            )
        )
        lines.append(_gap_line(candidate))

    # 一条编号都没交回：**说一次，不报 N 条**（见 docstring 最后那两节）。
    no_lineage = bool(candidates) and not submitted
    shard_gaps = _shard_gap_lines(steps)
    verify_gaps = _verify_gap_lines(steps)
    explained = tuple(
        (verdict, tuple(items)) for verdict, items in ruling_lines.items() if items
    )
    if no_lineage:
        lines = []
        dropped = []
    # 有真缺口、有分片/对账轮的缺口、或者有**去向我们得说一句**的候选时才出这一节。
    # 「被平台自己截掉/合并」那一种不单独成段（汇总结算的那一句已经说了）：它们的去向在
    # 报告里另有记账（`build_cap_section` 与运行轨迹），重复报会让同一批条目在两节里
    # 各出现一次、而两节的说法还不一样。
    if (
        not lines
        and not no_lineage
        and not shard_gaps
        and not verify_gaps
        and not explained
        and not settled
    ):
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
    if no_lineage:
        blocks.append(
            f"平台的账上共有 {len(candidates)} 条来自分片代理的候选结论，而**本次汇总"
            "没有交回候选血缘**（每条结论都该在 `source_candidate_ids` 里写上它来源于"
            "哪几条候选）。没有这一组编号，平台**无法按编号对账** —— 所以这里既不报"
            "「找不到去向」，也不把它们算成遗漏：**请人工对照各分片的候选清单过一遍**。"
        )
    if lines:
        blocks.append(
            f"平台的账上共有 {len(candidates)} 条来自分片代理的候选结论，"
            f"其中 {len(lines)} 条**没有进入最终结论清单**。平台的核对**只按候选编号**："
            "汇总交回的每条结论都带着它的来源编号（`source_candidate_ids`），平台拿候选"
            "清单去比这一组编号 —— 对不上的就是下面这几条。它们不因此就不成立，"
            "只是需要人工看一眼：\n\n" + "\n".join(lines)
        )
    for verdict, items in explained:
        blocks.append(_ruling_block(verdict, items))
    if settled:
        blocks.append(
            f"另有 {settled} 条候选的编号**汇总交回过、但没有留在最终清单里** ——"
            "平台在这一侧把它们处置掉了（超出本次条数上限 / 未达告警门槛 / 与已有条目"
            "判为同一问题）。这几条的记账在「结论条数上限（平台补充）」那一节与本次"
            "运行轨迹里，不重复计入这一份名单。"
        )
    blocks.append(
        "以上是平台**按记录核对**出来的，不是模型的自我说明。"
    )
    return "\n\n".join(blocks), tuple(dropped)


def _submitted_candidate_ids(
    synthesis: EngineOutcome, reduction: Reduction | None
) -> frozenset[str]:
    """汇总**交回了哪些候选编号**（它自己写下的那份血缘）。

    两批都要看：

    * `synthesis.payload.anomalies` 是模型原始那一批（`_run_one` 也是从这里抽候选的，
      见那里的「抽的是过门槛之前的」）；
    * `synthesis.anomalies` 是过了门槛/去重/封顶之后的那一批。

    只看后者的话，**被条数上限截掉**的那几条会被误报成「找不到去向」（它们的编号在
    前者里，而结论对象已经被平台丢掉了 —— 上限记账只有一行文字，没有编号）。
    `reduction.rows` 也要并进来：裁决之后的活动清单与原始那批不一定逐条对应。
    """
    ids: set[str] = set()
    for item in synthesis.anomalies:
        ids.update(item.source_candidate_ids)
    if synthesis.payload is not None:
        for item in synthesis.payload.anomalies:
            ids.update(item.source_candidate_ids)
    if reduction is not None:
        ids.update(reduction.claimed_candidate_ids)
    return frozenset(ids)


def _landed_candidate_ids(
    synthesis: EngineOutcome, reduction: Reduction | None
) -> frozenset[str]:
    """**进了最终结论清单**（活动的那些结论）的候选编号。

    有 `reduction` 时以它为准 —— 那才是落库与界面读的清单（撤销的条目已经被移出）。
    没有 `reduction` 时退化成汇总那一份清单，逐字保持调用方没接上时的行为。
    """
    if reduction is not None and reduction.rows:
        return reduction.landed_candidate_ids
    return frozenset(
        candidate_id
        for item in synthesis.anomalies
        for candidate_id in item.source_candidate_ids
    )


def _ruling_fate(
    candidate: Candidate, reduction: Reduction | None
) -> tuple[str, FindingRow] | None:
    """这条候选在**本次复核裁决**里的去向（对不上任何一条就是 `None`）。

    判据是**编号**（`FindingRow.source_candidate_ids` 里有没有这条候选的 ID）—— 不是
    标题、不是文件、不是证据文本：那三手正是 AI-P0-06 换掉的东西，它们的误判会让一条
    真的被漏掉的候选被宣布成「已撤销」（静默），也会让一条真的有去向的候选被报成缺口。

    优先级写成 `RULING_FATES` 的次序（撤销 → 降级 → 待人工核验）：一条候选同时沾上两条
    裁决时（少见，但可能），取**处置更重**的那一条 —— 撤销是「移出清单」，说成降级会让读的
    人以为它还在，而那正是这条缺陷的形态。
    """
    if reduction is None:
        return None
    for verdict in RULING_FATES:
        for row in reduction.rows:
            if row.verdict == verdict and candidate.id in row.source_candidate_ids:
                return verdict, row
    return None


# 三种去向各一段（`_ruling_block`）。措辞与既有的那两段同一口气（「已经解释过了」），
# 但**三段分开**：撤销、降级、待人工核验对读的人是三件事，合成一句会让处置说不清。
_RULING_BLOCK_TEXT = {
    VERDICT_RETRACTED: (
        "另有 {count} 条候选的缺席**已经解释过了**：与它们同一条的结论在本次复核里"
        "**已撤销**（移出当前结论清单、不进下一轮基线）—— 撤销本身也是结论，不是遗漏，"
        "所以不用再去人工找一遍。撤销的理由与原文列在前面那节「复核裁决（平台）」里，"
        "这里只记候选与结论的对应关系，免得两边的账对不上：\n\n{items}"
    ),
    VERDICT_DOWNGRADED: (
        "另有 {count} 条候选**进了结论清单，只是等级被本次复核降了**：它们不是遗漏 —— "
        "结论还在清单里，按复核**之后**的等级采信（降到哪一级、为什么，写在"
        "「复核裁决（平台）」那一节里）。这里只记候选与结论的对应关系，"
        "免得两边的账对不上：\n\n{items}"
    ),
    VERDICT_NEEDS_MORE_EVIDENCE: (
        "另有 {count} 条候选**进了结论清单，但本次复核把它们转成了「待人工核验」**"
        "（证据不足，置信度不再按 `very_high` 采信）：它们不是遗漏，处置以"
        "「复核裁决（平台）」那一节为准，这里**不另说一遍**，只记候选与结论的对应关系："
        "\n\n{items}"
    ),
}


def _ruling_block(verdict: str, items: Sequence[str]) -> str:
    """「这几条候选的去向由复核裁决写着」那一段（自己带计数）。"""
    return _RULING_BLOCK_TEXT[verdict].format(count=len(items), items="\n".join(items))


def _ruling_line(candidate: Candidate, verdict: str, row: FindingRow) -> str:
    """「这条候选对应到哪条复核结论」那一行。

    裁决的说法直接用 `row.verdict_label`（`verdict.VERDICT_LABELS` 那一份）—— 报告里两处
    （「复核裁决（平台）」那一节、这一节）说同一条裁决必须**逐字一致**，各写一份措辞迟早
    会说成两件事。
    """
    detail = (
        f"对应本次复核的 `[{row.finding_id}]`「{row.origin.title}」，"
        f"裁决为 **{row.verdict_label}**"
    )
    if verdict == VERDICT_DOWNGRADED:
        detail += f"（`{row.origin.severity}` → `{row.anomaly.severity}`）"
    return f"{_candidate_head(candidate)}{detail}。"


def _candidate_head(candidate: Candidate) -> str:
    """一条候选在名单里的开头（编号、标题、维度/严重度、文件）。

    `_gap_line` 与 `_ruling_line` 共用它：同一批候选在「没有进入清单」与「去向由裁决写着」
    两段里出现时，读的人要靠这半个句子认出是同一条。
    """
    return (
        f"- `[{candidate.id}]` {candidate.anomaly.title}"
        f"（{candidate.anomaly.category}·{candidate.anomaly.severity}"
        + (f"，{candidate.anomaly.file_path}" if candidate.anomaly.file_path else "")
        + "）："
    )


def _gap_line(candidate: Candidate) -> str:
    """「这条候选没有进入结论清单」那一行 —— **只说这一条候选自己的去向**。

    ## 它只声称平台真正做过的那一件事

    平台核对的唯一判据是**候选编号**（`source_candidate_ids`，见 `reconcile_candidates`）。
    所以这一行只能说「汇总交回的结论里没有一条声明来源于这个编号」。

    在这之前它写的是「报告里既没有引用这个编号，正文与结论清单里也都没有提到这个文件」
    —— 那句话声称的是**文件名的匹配**，而那条判据（连同同文件、同标题那几手）正是
    AI-P0-06 删掉的东西。一并删掉的还有一句更早的话：「也没有任何一条结论提到这个文件」
    （读起来像「整份报告里都没有这个文件」，而平台核对的只是结论清单；2026-09-20 那次
    实测报出的三条「找不到去向」，文件全都在模型自己写的正文里出现过，读的人随手一搜
    就能推翻这句话）。**多写一手就是平台没做过的保证。**
    """
    return (
        _candidate_head(candidate)
        + "汇总交回的结论里**没有任何一条声明来源于这个编号**"
        "（`source_candidate_ids`）—— 是真的没被采纳，还是采纳了却忘了带编号，"
        "需要人工看一眼。"
    )


# 注：`_anomaly_text`（把一条结论拍成散文、供字符串比对用）与它服务的那几手启发式
# （`_same_finding` / `_path_named_in_findings` / `_basename_named_in_text`）一并删掉了
# （AI-P0-06）：对账改按编号之后没有任何调用方。
