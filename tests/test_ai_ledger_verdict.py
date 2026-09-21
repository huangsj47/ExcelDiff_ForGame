# -*- coding: utf-8 -*-
"""对账要**先看复核裁决**再谈缺口 —— 撤销 / 降级 / 转人工核验的候选不是「遗漏」。

## 这一组盯的是什么

真机 run 13 的报告里，同一件事被写了两遍，而且两遍互相矛盾：

* 正文「历史结论状态」写「**已推翻（1 条）**：ProtoCTms joinTeamByRecruit 参数由 roleId
  改为 recruitId……客户端已同步，**该缺口关闭**」；
* 平台追加的「信息缺口（平台补充）」写「`[S1-9]` ……报告里既没有引用这个编号，正文与
  结论清单里也都没有提到这个文件 → **需要人工看一眼**」。

一处说关闭、一处说可能遗漏。机制上的原因是对账只查两处落点（候选编号在不在报告里、
候选的 `file_path` 在不在结论清单里），**完全不看规范结论的状态**：一条被复核撤销的结论
（`verdict.Reduction.retracted`）已经不在这份清单里了，于是那条候选被算成「没有进入最终
结论清单」—— 而它真正的去向是「已撤销」。

所以这一组钉住四件事：

1. 撤销的候选**不是遗漏**，平台补充里如实说「已撤销」，并给出它对应的结论编号；
2. **分类要精确**：撤销（移出清单）与降级（还在清单里、等级变了）不能混成一类 ——
   用户对这两件事的处置不一样；
3. 对应到 `needs_more_evidence` / 已降级的候选，措辞与裁决一致，**不把同一件事说两遍**
   （一边「转人工核验」、一边「没有进入最终结论清单」）；
4. **不许放宽成「凡候选都能找到落点」** —— 真的没有落点的候选照旧报成缺口（反向对照）。
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
    )


def _candidate(label: str = "S1", index: int = 9, **overrides) -> Candidate:
    return Candidate(member_label=label, index=index, anomaly=_obj(**overrides))


def _synthesis(**overrides) -> EngineOutcome:
    """一份**裁决之后**的汇总：报告里不再有那条被撤销的结论。

    这就是真机那个形态 —— 撤销在落库那一侧已经生效（`outcome.anomalies` 里没有它），
    于是对账那两处落点一处都看不到它。
    """
    kwargs = dict(
        status=STATUS_SUCCEEDED,
        report_markdown="# 变更理解\n\n本次改动集中在协议层。\n",
        anomalies=(),
    )
    kwargs.update(overrides)
    return EngineOutcome(**kwargs)


def _retracted_reduction(**overrides):
    """一次真实的 reducer 产出：一条结论被复核撤销（不是手搓的 `Reduction`）。"""
    base = _obj(
        "【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
        file_path=PROTO,
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
    def test_it_is_not_reported_as_a_possible_omission(self):
        """run 13 那个形态：正文说「已推翻、缺口关闭」，平台补充不许说「可能遗漏」。"""
        _, reduction = _retracted_reduction()
        candidate = _candidate(
            title="【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
            file_path=PROTO,
        )

        text, dropped = reconcile_candidates((candidate,), _synthesis(), reduction=reduction)

        assert dropped == (), "被复核撤销的候选不是「没进清单」，不该记进缺口账"
        assert "可能遗漏" not in text
        assert "既没有引用这个编号" not in text, (
            f"撤消的候选仍被按「找不到去向」写：\n{text}"
        )

    def test_it_says_retracted_and_names_the_finding(self):
        """平台补充里要如实说「已撤销」，并写出它对应哪条结论 —— 读的人才能去核。"""
        _, reduction = _retracted_reduction()
        candidate = _candidate(
            title="【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
            file_path=PROTO,
        )

        text, _ = reconcile_candidates((candidate,), _synthesis(), reduction=reduction)

        assert "[S1-9]" in text
        assert "已撤销" in text or "撤销" in text, text
        assert "[F1]" in text, f"没给出它对应哪条结论：\n{text}"
        assert "复核裁决（平台）" in text, "要说清撤销的理由在哪一节，读者才知道去哪看"

    def test_only_the_explained_candidates_still_produce_the_section(self):
        """这一节**不因为「没有真缺口」就整个消失**：账上有一条候选，就得交代它去哪了。

        与「被条数上限截掉」那一条的差别在这里：截断的候选在报告里另有一节逐条列着
        （`build_cap_section`），而复核撤销只列**结论**（`[F1]`），候选编号（`[S1-9]`）
        在报告里别处一个都不出现 —— 这一节是唯一能回答「`[S1-9]` 去哪儿了」的地方。
        """
        _, reduction = _retracted_reduction()
        candidate = _candidate(
            title="【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
            file_path=PROTO,
        )

        text, _ = reconcile_candidates((candidate,), _synthesis(), reduction=reduction)

        assert "信息缺口（平台补充）" in text
        assert "已经解释过了" in text

    def test_the_ruling_wins_over_the_raw_finding_still_being_in_the_list(self):
        """**裁决先看**：原结论还在汇总清单里（裁决之前的账），也不改变分类。

        落库那一侧看不出来这一条 —— 只有把 `reduction` 交给对账，平台才知道这条候选
        的去向是「撤销」而不是「采纳」。
        """
        base, reduction = _retracted_reduction()
        candidate = _candidate(
            title="【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
            file_path=PROTO,
        )
        synthesis = _synthesis(anomalies=(base,))

        text, dropped = reconcile_candidates((candidate,), synthesis, reduction=reduction)

        assert dropped == (), "撤销不是缺口"
        assert "撤销" in text and "[S1-9]" in text


class TestDowngradedAndPendingAreNotLumpedTogether:
    def _candidate_for(self, path: str, title: str) -> Candidate:
        return _candidate(index=4, title=title, file_path=path)

    def test_a_downgraded_candidate_is_a_downgrade_not_a_retraction(self):
        """降级 = **还在清单里**、等级变了；撤销 = 移出清单。两件事不能混成一类。"""
        base = _obj("【掉落拾取】新增事务序号硬校验", file_path=DROP, severity="critical")
        reduction = reduce_findings(
            [base],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1", verdict=VERDICT_DOWNGRADED, final_severity="high"
                ),
            ),
        )
        candidate = self._candidate_for(DROP, "【掉落拾取】新增事务序号硬校验")

        text, dropped = reconcile_candidates((candidate,), _synthesis(), reduction=reduction)

        assert dropped == ()
        assert "[S1-4]" in text
        assert "降级" in text, text
        assert "已撤销" not in text, f"降级被写成撤销（两者处置不同）：\n{text}"
        assert "没有进入最终结论清单" not in text
        assert "critical" in text and "high" in text, "要说清降到哪一级"

    def test_a_needs_more_evidence_candidate_is_not_told_twice(self):
        """「转人工核验」与「没有进入最终结论清单」是同一件事 —— 说一遍就够。"""
        base = _obj("【协议】中部删除导致 id 前移", file_path=DROP)
        reduction = reduce_findings(
            [base],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1", verdict=VERDICT_NEEDS_MORE_EVIDENCE, reason="缺日志"
                ),
            ),
        )
        candidate = self._candidate_for(DROP, "【协议】中部删除导致 id 前移")

        text, dropped = reconcile_candidates((candidate,), _synthesis(), reduction=reduction)

        assert dropped == ()
        assert "[S1-4]" in text and "待人工核验" in text
        assert "没有进入最终结论清单" not in text, f"同一件事说了两遍：\n{text}"
        assert "需要人工看一眼" not in text, f"把「待人工核验」又说了一遍：\n{text}"

    def test_the_three_fates_get_three_different_blocks(self):
        """三条候选、三种去向 → 三段各自计数、各自措辞，不合并成一句「都不是遗漏」。"""
        base_retracted = _obj("【协议】A 被撤销", file_path=PROTO, severity="high")
        base_downgraded = _obj("【掉落】B 被降级", file_path=DROP, severity="critical")
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
        _, reduction = _retracted_reduction()
        landed = _candidate(
            title="【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
            file_path=PROTO,
        )
        orphan = _candidate(
            index=7,
            title="【掉落】备份不再深拷贝",
            file_path="code/qz_pub/core/scene/aoi/pack/PackDropObjSnapshotMod.lua",
        )

        text, dropped = reconcile_candidates((landed, orphan), _synthesis(), reduction=reduction)

        assert [item.index for item in dropped] == [7], (
            f"复核撤销把一条真的没有落点的候选也吞掉了：{[item.detail for item in dropped]}"
        )
        assert dropped[0].kind == "subagent"
        assert "[S1-7]" in text
        assert "报告里既没有引用这个编号" in text, "真缺口那一段措辞不该跟着改掉"

    def test_a_candidate_without_a_path_is_not_claimed_by_a_finding_without_one(self):
        """两侧都没有 `file_path` 时**不许**按「都没有」就认成同一条。

        认错的代价是静默的：一条真的被漏掉的候选从此不报，而这一节存在的唯一理由
        就是数它。
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

        text, dropped = reconcile_candidates((candidate,), _synthesis(), reduction=reduction)

        assert [item.index for item in dropped] == [2], f"空路径被认成了同一条：\n{text}"

    def test_a_finding_about_another_file_does_not_claim_the_candidate(self):
        _, reduction = _retracted_reduction()
        candidate = _candidate(
            index=8, title="【掉落】事务序号硬校验缺失", file_path=DROP
        )

        text, dropped = reconcile_candidates((candidate,), _synthesis(), reduction=reduction)

        assert [item.index for item in dropped] == [8]
        assert "[S1-8]" in text

    def test_without_a_reduction_nothing_changes(self):
        """不传 `reduction` 时逐字保持原行为（调用方没接上时不许静默改变结论）。"""
        candidate = _candidate(
            title="【协议】ProtoCTms joinTeamByRecruit 参数由 roleId 改为 recruitId",
            file_path=PROTO,
        )

        text, dropped = reconcile_candidates((candidate,), _synthesis())

        assert [item.index for item in dropped] == [9]
        assert "报告里既没有引用这个编号" in text


class TestTheSelfDescriptionFollowsTheCheck:
    def test_it_no_longer_claims_only_two_landing_spots(self):
        """「平台只核对两处落点」在加了复核裁决这一处之后已经不准确，必须跟着改。"""
        orphan = _candidate(index=7, title="【掉落】备份不再深拷贝", file_path=DROP)

        text, _ = reconcile_candidates((orphan,), _synthesis())

        assert "平台只核对两处落点" not in text
        assert "正文里提到过不算采纳" in text, "核对口径要写出来，否则读者不知道正文不算"
        assert "复核的裁决" in text, (
            f"核对落在哪几处要说全（复核裁决也是其中一处）：\n{text}"
        )
