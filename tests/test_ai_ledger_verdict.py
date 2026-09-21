# -*- coding: utf-8 -*-
"""对账要**先看复核裁决**再谈缺口 —— 撤销 / 降级 / 转人工核验的候选不是「遗漏」。

## 这一组盯的是什么

真机 run 13 的报告里，同一件事被写了两遍，而且两遍互相矛盾：

* 正文「历史结论状态」写「**已推翻（1 条）**：ProtoCTms joinTeamByRecruit 参数由 roleId
  改为 recruitId……客户端已同步，**该缺口关闭**」；
* 平台追加的「信息缺口（平台补充）」写「`[S1-9]` ……报告里既没有引用这个编号，正文与
  结论清单里也都没有提到这个文件 → **需要人工看一眼**」。

机制上的原因是对账只查两处落点（候选编号在不在报告里、候选的 `file_path` 在不在结论
清单里），**完全不看规范结论的状态**：一条被复核撤销的结论（`verdict.Reduction.retracted`）
已经不在这份清单里了，于是那条候选被算成「没有进入最终结论清单」—— 而它真正的去向是
「已撤销」。

所以这一组钉住四件事：

1. 撤销的候选**不是遗漏**，平台补充里如实说「已撤销」，并给出它对应的结论编号；
2. **分类要精确**：撤销（移出清单）与降级（还在清单里、等级变了）不能混成一类 ——
   用户对这两件事的处置不一样；
3. 对应到 `needs_more_evidence` / 已降级的候选，措辞与裁决一致，**不把同一件事说两遍**
   （一边「转人工核验」、一边「没有进入最终结论清单」）；
4. **不许放宽成「凡候选都能找到落点」** —— 真的没有落点的候选照旧报成缺口（反向对照）。

## 2026-09-21 换过一次判据（AI-P0-06）

上面第 1、2 条里那个「候选 ↔ 结论」的对应关系原先是用**标题、文件路径、证据文本**反推
出来的。那套启发式在真机上产生的全是假缺口（run 20 的 `S3-3`/`F5`：同名协议拆在两个
文件里、标题措辞也不同），所以对应关系改为**显式血缘**：汇总在每条结论的
`source_candidate_ids` 里列出它来源于哪几条候选，平台只比这一组编号。这一组用例因此
全部按编号组织 —— 与文件、标题无关。
"""
from __future__ import annotations

from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome
from services.ai.family_ledger import Candidate, reconcile_candidates
from services.ai.protocol import Anomaly
from services.ai.verdict import (
    VERDICT_DOWNGRADED,
    VERDICT_NEEDS_MORE_EVIDENCE,
    VERDICT_RETRACTED,
    VerifyVerdict,
    assign_findings,
    reduce_findings,
)

PROTO = "code/qz_pub/protocols/ProtoCTms.lua"
DROP = "code/qz_server/src/gas/module/drop/GasDropPickTxnMod.lua"
COMMIT = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _obj(
    title: str,
    *,
    file_path: str = "",
    severity: str = "high",
    confidence: str = "high",
    category: str = "code_logic",
    commit: str = COMMIT,
    evidence: tuple[str, ...] = ("提交 a1b2c3d4：改了这里",),
    source_candidate_ids: tuple[str, ...] = (),
) -> Anomaly:
    return Anomaly(
        title=title,
        category=category,
        severity=severity,
        confidence=confidence,
        evidence=evidence,
        commit=commit,
        file_path=file_path,
        impact="客户端与协议侧会不一致",
        suggestion="核对两端",
        source_candidate_ids=source_candidate_ids,
    )


def _candidate(label: str = "S1", index: int = 9, **overrides) -> Candidate:
    return Candidate(member_label=label, index=index, anomaly=_obj(**overrides))


def _synthesis(**overrides) -> EngineOutcome:
    """一份**裁决之后**的汇总：报告里不再有那条被撤销的结论。

    这就是真机那个形态 —— 撤销在落库那一侧已经生效（`outcome.anomalies` 里没有它），
    而报告正文（现在是平台那几节）里也不再提它。
    """
    kwargs = dict(
        status=STATUS_SUCCEEDED,
        report_markdown="# 变更理解\n\n本次改动集中在协议层。\n",
        anomalies=(),
    )
    kwargs.update(overrides)
    return EngineOutcome(**kwargs)


def _retracted_reduction(**overrides):
    """一次真实的 reducer 产出：一条结论被复核撤销（不是手搓的 `Reduction`）。

    那条结论**带着候选血缘**（`source_candidate_ids=("S1-9",)`）—— 这正是它与候选对上
    的唯一依据。
    """
    base = _obj(
        "【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
        file_path=PROTO,
        source_candidate_ids=("S1-9",),
    )
    kwargs = dict(
        verdicts=(
            VerifyVerdict(
                finding_id="F1",
                verdict=VERDICT_RETRACTED,
                reason="客户端已同步该参数，缺口关闭",
                evidence_refs=("ProtoCTms.lua:120",),
            ),
        ),
    )
    kwargs.update(overrides)
    return base, reduce_findings([base], **kwargs)


class TestARetractedCandidateIsNotMissing:
    def _candidate(self) -> Candidate:
        """它对应的那条候选（编号 `S1-9`，与结论的血缘一致）。"""
        return _candidate(
            index=9,
            title="【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
            file_path=PROTO,
        )

    def test_it_is_not_reported_as_a_possible_omission(self):
        """run 13 那个形态：正文说「已推翻、缺口关闭」，平台补充不许说「可能遗漏」。"""
        _, reduction = _retracted_reduction()

        text, dropped = reconcile_candidates(
            (self._candidate(),), _synthesis(), reduction=reduction
        )

        assert dropped == (), "被复核撤销的候选不是「没进清单」，不该记进缺口账"
        assert "可能遗漏" not in text
        assert "既没有引用这个编号" not in text, (
            f"撤消的候选仍被按「找不到去向」写：\n{text}"
        )
        assert "没有进入最终结论清单" not in text

    def test_it_says_retracted_and_names_the_finding(self):
        """平台补充里要如实说「已撤销」，并写出它对应哪条结论 —— 读的人才能去核。"""
        _, reduction = _retracted_reduction()

        text, _ = reconcile_candidates(
            (self._candidate(),), _synthesis(), reduction=reduction
        )

        assert "[S1-9]" in text
        assert "已撤销" in text or "撤销" in text, text
        assert "[F1]" in text, f"没给出它对应哪条结论：\n{text}"
        assert "复核裁决（平台）" in text, "要说清撤销的理由在哪一节，读者才知道去哪看"

    def test_only_the_explained_candidates_still_produce_the_section(self):
        """这一节**不因为「没有真缺口」就整个消失**：账上有一条候选，就得交代它去哪了。

        与「被平台自己截掉」那一种的差别在这里：那一种的去向在报告里另有记账
        （「结论条数上限（平台补充）」那一节与运行轨迹），而复核撤销只列**结论**
        （`[F1]`），候选编号（`[S1-9]`）在报告里别处一个都不出现 —— 这一节是唯一能回答
        「`[S1-9]` 去哪儿了」的地方。
        """
        _, reduction = _retracted_reduction()

        text, _ = reconcile_candidates(
            (self._candidate(),), _synthesis(), reduction=reduction
        )

        assert "信息缺口（平台补充）" in text
        assert "撤销" in text

    def test_the_ruling_wins_over_the_raw_finding_still_being_in_the_list(self):
        """**裁决先看**：原结论还在汇总清单里（裁决之前的账），也不改变分类。

        落库那一侧看不出来这一条 —— 只有把 `reduction` 交给对账，平台才知道这条候选
        的去向是「撤销」而不是「采纳」。
        """
        base, reduction = _retracted_reduction()
        synthesis = _synthesis(anomalies=(base,))

        text, dropped = reconcile_candidates(
            (self._candidate(),), synthesis, reduction=reduction
        )

        assert dropped == (), "撤销不是缺口"
        assert "撤销" in text and "[S1-9]" in text


class TestDowngradedAndPendingAreNotLumpedTogether:
    def test_a_downgraded_candidate_is_a_downgrade_not_a_retraction(self):
        """降级 = **还在清单里**、等级变了；撤销 = 移出清单。两件事不能混成一类。

        降级之后 `anomaly` 是新等级，但**血缘跟着走**（`origin` 的那一份一路 `replace`
        过来）—— 不跟的话这条候选会掉进「找不到去向」，而那正是降级这次处置的反面。
        """
        base = _obj(
            "【掉落拾取】新增事务序号硬校验",
            file_path=DROP,
            severity="critical",
            source_candidate_ids=("S1-4",),
        )
        reduction = reduce_findings(
            [base],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1", verdict=VERDICT_DOWNGRADED, final_severity="high"
                ),
            ),
        )
        candidate = _candidate(index=4, title="【掉落拾取】新增事务序号硬校验", file_path=DROP)

        text, dropped = reconcile_candidates((candidate,), _synthesis(), reduction=reduction)

        assert dropped == ()
        assert "[S1-4]" in text
        assert "降级" in text, text
        assert "已撤销" not in text, f"降级被写成撤销（两者处置不同）：\n{text}"
        assert "没有进入最终结论清单" not in text
        assert "critical" in text and "high" in text, "要说清降到哪一级"

    def test_a_needs_more_evidence_candidate_is_not_told_twice(self):
        """「转人工核验」与「没有进入最终结论清单」是同一件事 —— 说一遍就够。"""
        base = _obj(
            "【协议】中部删除导致 id 前移",
            file_path=DROP,
            source_candidate_ids=("S1-4",),
        )
        reduction = reduce_findings(
            [base],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1", verdict=VERDICT_NEEDS_MORE_EVIDENCE, reason="缺日志"
                ),
            ),
        )
        candidate = _candidate(index=4, title="【协议】中部删除导致 id 前移", file_path=DROP)

        text, dropped = reconcile_candidates((candidate,), _synthesis(), reduction=reduction)

        assert dropped == ()
        assert "[S1-4]" in text and "待人工核验" in text
        assert "没有进入最终结论清单" not in text, f"同一件事说了两遍：\n{text}"
        assert "需要人工看一眼" not in text, f"把「待人工核验」又说了一遍：\n{text}"

    def test_the_three_fates_get_three_different_blocks(self):
        """三条候选、三种去向 → 三段各自计数、各自措辞，不合并成一句「都不是遗漏」。"""
        base_retracted = _obj(
            "【协议】A 被撤销",
            file_path=PROTO,
            severity="high",
            source_candidate_ids=("S1-1",),
        )
        base_downgraded = _obj(
            "【掉落】B 被降级",
            file_path=DROP,
            severity="critical",
            source_candidate_ids=("S1-2",),
        )
        # 编号由 `assign_findings` 按严重度发（`critical` 那条是 F1），测试里按标题取回来
        # —— 写死 F1/F2 会让这条测试在排序规则变一下时变成「裁决打到了另一条结论上」。
        ids = {
            finding.anomaly.title: finding.finding_id
            for finding in assign_findings([base_retracted, base_downgraded])
        }
        assert ids["【协议】A 被撤销"] != ids["【掉落】B 被降级"]
        reduction = reduce_findings(
            [base_retracted, base_downgraded],
            verdicts=(
                VerifyVerdict(
                    finding_id=ids["【协议】A 被撤销"],
                    verdict=VERDICT_RETRACTED,
                    reason="反证成立",
                    evidence_refs=("ProtoCTms.lua:9",),
                ),
                VerifyVerdict(
                    finding_id=ids["【掉落】B 被降级"],
                    verdict=VERDICT_DOWNGRADED,
                    final_severity="medium",
                ),
            ),
        )
        retracted = _candidate(index=1, title="【协议】A 被撤销", file_path=PROTO)
        downgraded = _candidate(index=2, title="【掉落】B 被降级", file_path=DROP)
        pending = _candidate(index=3, title="【别的】C 没落点", file_path="config/C.xlsx")

        text, dropped = reconcile_candidates(
            (retracted, downgraded, pending), _synthesis(), reduction=reduction
        )

        assert [item.index for item in dropped] == [3], (
            f"只有真的没有落点的那条该记账：{[item.detail for item in dropped]}"
        )
        assert "[S1-1]" in text and "[S1-2]" in text and "[S1-3]" in text
        assert "已撤销" in text and "降级" in text
        assert "没有进入最终结论清单" in text, "那条真缺口仍要报"


class TestRealGapsAreStillReported:
    """反向对照：**不许**放宽成「凡候选都能找到落点」。"""

    def test_a_candidate_with_no_landing_is_still_a_gap(self):
        base, reduction = _retracted_reduction()
        landed = _candidate(
            index=9,
            title="【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
            file_path=PROTO,
        )
        orphan = _candidate(
            index=7,
            title="【掉落】备份不再深拷贝",
            file_path="code/qz_pub/core/scene/aoi/pack/PackDropObjSnapshotMod.lua",
        )

        text, dropped = reconcile_candidates(
            (landed, orphan), _synthesis(), reduction=reduction
        )

        assert [item.index for item in dropped] == [7], (
            f"复核撤销把一条真的没有落点的候选也吞掉了：{[item.detail for item in dropped]}"
        )
        assert dropped[0].kind == "subagent"
        assert "[S1-7]" in text
        assert "没有任何一条声明来源于这个编号" in text, "真缺口那一段措辞不该跟着改掉"

    def test_a_finding_with_no_lineage_does_not_claim_any_candidate(self):
        """**标题与文件都一样，也不认**：判据是编号，不是这两样。

        这条与「同文件就算采纳」那手启发式刚好相反，是有意的取舍：那手在真机上产生的是
        假缺口（run 20 的 `S3-3`/`F5`），而「同文件」既会误报（同名协议拆在两个文件里）
        又会漏报（把真的缺口说成有去向 —— 静默的那一侧）。所以平台不再替汇总猜。
        """
        base = _obj("【流程】本次改动没走配表评审流程", file_path="")
        reduction = reduce_findings(
            [base],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_RETRACTED,
                    reason="反证成立",
                    evidence_refs=("docs/流程.md:3",),
                ),
            ),
        )
        candidate = _candidate(index=2, title="【道具】ID 被删除但生成文件仍在", file_path="")
        other = _candidate(index=8, title="【掉落】事务序号硬校验缺失", file_path=DROP)

        # 汇总这一侧**有**血缘（另一条结论带着 `S1-1`），所以它不是「一条编号都没交回」，
        # 这两条候选就是真缺口。
        adopted = _obj("另一条结论", source_candidate_ids=("S1-1",))

        text, dropped = reconcile_candidates(
            (candidate, other), _synthesis(anomalies=(adopted,)), reduction=reduction
        )

        assert [item.index for item in dropped] == [2, 8], (
            f"没有血缘的候选被别的结论认领了：\n{text}"
        )

    def test_a_finding_about_another_file_does_not_claim_the_candidate(self):
        """别的结论带着别的编号 —— 这一条照旧是真缺口（反向对照的第二手）。"""
        stranded = _obj(
            "【协议】另一条结论",
            file_path=PROTO,
            source_candidate_ids=("S1-1",),
        )
        reduction = reduce_findings([])  # 裁决里没有它，也不影响这条用例的判据
        assert reduction.rows == ()
        candidate = _candidate(index=8, title="【掉落】事务序号硬校验缺失", file_path=DROP)

        text, dropped = reconcile_candidates(
            (candidate,), _synthesis(anomalies=(stranded,)), reduction=reduction
        )

        assert [item.index for item in dropped] == [8]
        assert "[S1-8]" in text

    def test_without_a_reduction_the_lineage_still_decides(self):
        """不传 `reduction` 时按汇总自己那份清单的血缘判（逐字保持调用方没接上时的行为）。"""
        candidate = _candidate(
            index=9,
            title="【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
            file_path=PROTO,
        )
        adopted = _obj(
            "换了个说法的同一条", file_path="别的/文件.lua", source_candidate_ids=("S1-9",)
        )

        landed_text, landed = reconcile_candidates(
            (candidate,), _synthesis(anomalies=(adopted,))
        )
        # 两份汇总都**交回了编号**（各自带着别的候选），所以走的是逐条比对那条路。
        gap_text, gap = reconcile_candidates(
            (candidate,),
            _synthesis(anomalies=(_obj("别的", source_candidate_ids=("S1-1",)),)),
        )

        assert (landed_text, landed) == ("", ()), "血缘对上了却还报缺口"
        assert [item.index for item in gap] == [9]
        assert "[S1-9]" in gap_text


class TestTheSelfDescriptionFollowsTheCheck:
    def test_it_says_what_the_platform_actually_compares(self):
        """自称的核对口径必须与真正做的那件事一致 —— 多写一手就是没做过的保证。"""
        orphan = _candidate(index=7, title="【掉落】备份不再深拷贝", file_path=DROP)
        synthesis = _synthesis(
            anomalies=(_obj("别的", source_candidate_ids=("S1-1",)),)
        )

        text, _ = reconcile_candidates((orphan,), synthesis)

        assert "平台的核对**只按候选编号**" in text, (
            f"核对落在哪一处要说全（现在只有编号这一处）：\n{text}"
        )
        assert "正文里提到过不算采纳" not in text, (
            "「正文里提到过」已经不再是判据了 —— 这句承诺现在指向一件不存在的事"
        )
        assert "平台只核对两处落点" not in text
        assert "不是模型的自我说明" in text
