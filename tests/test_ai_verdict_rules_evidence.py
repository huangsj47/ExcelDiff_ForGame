# -*- coding: utf-8 -*-
"""复核裁决的四条口径（全部来自 run 15 的实测报告）。

**一、「证据不足」要降一档等级。** `F3` 的裁决逐字写着「原 critical / very_high → 证据不足
（待人工核验）」，而落库那行的 `severity` / `original_severity` 都还是 `critical` ——
清单里于是同时出现「critical」与「证据不足」这种自相矛盾的组合。口径由用户定死：一律降一档
（`critical` → `high` → `medium` → `low`），理由沿用复核给的理由。

**二、正文的 `R#` 与平台的 `F#` 要有映射。** 模型在正文里自己编 `R1`…`R13`，reducer 另发
`F1`…`F17`（实测条数也不等：13 对 17），读者没法把裁决落回正文那一条。映射只按**可复现的
判据**（同一 `file_path` / 标题词集）建立，**对不上时宁可不写**（写错比不写糟得多）。

**三、依据要有形状校验。** 完整的 unified diff hunk（文件路径 + old/new 行区间）可以在
固定 commit 上复现，应当算可定位证据；`diff@@` 残片、缺路径的 hunk 和散文仍必须拒绝。

**四、证据有缺口不得维持 `very_high`。** `F1` 是 critical/very_high 且裁决「维持」，而信息
缺口里写着它引用的 `ProtoCScs.lua` 的 diff 被长度上限截断过；`F3` 转人工的原因是
`find_references` 额度耗尽。规则必须**收在受影响的那些结论上**，不许做成「一律降」。
"""
from __future__ import annotations

import pytest

from services.ai.budget import ContextItem
from services.ai.engine import (
    DEGRADE_NONE,
    DEGRADE_REQUESTS,
    STATUS_SUCCEEDED,
    EngineOutcome,
    RoundRecord,
)
from services.ai.family_ledger import Candidate
from services.ai.protocol import Anomaly
from services.ai.skill_contract import SEVERITIES
from services.ai.subagent import MemberOutcome, MemberPlan, aggregate_outcomes
from services.ai.verdict import (
    CONFIDENCE_CEILING_WITH_GAP,
    QUOTA_EXHAUSTED_CODE,
    SEVERITY_STEP_DOWN,
    UNREVIEWED_LABEL,
    VERDICT_CONFIRMED,
    VERDICT_DOWNGRADED,
    VERDICT_NEEDS_MORE_EVIDENCE,
    VERDICT_RETRACTED,
    EvidenceGaps,
    VerifyVerdict,
    _label_file,
    _label_token,
    assign_body_labels,
    assign_findings,
    evidence_gaps_of,
    gap_reasons_of,
    is_locatable_ref,
    reduce_findings,
    render_ruling,
    severity_step_down,
)

COMMIT = "b" * 40
LUA = "code/qz_server/src/tms/module/TmsTeamMgrMod.lua"
PROTO = "code/qz_server/src/proto/ProtoCScs.lua"
TABLE = "config/[30]道具表_CfgItem.xlsx"
# Run 22 实测使用的标准 unified diff 定位符；旧版把它误判为不可定位并压低置信度。
HUNK_REF = f"{LUA} @@ -61,66 +66,26 @@"
LEGACY_HUNK_REF = f"{LUA}:diff@@ -61,66 +66,26 @@"


def _anomaly(**overrides) -> Anomaly:
    return Anomaly(
        title=overrides.pop("title", "队伍成员校验被删除，可能越权"),
        category=overrides.pop("category", "logic_change"),
        severity=overrides.pop("severity", "critical"),
        confidence=overrides.pop("confidence", "very_high"),
        evidence=tuple(overrides.pop("evidence", (f"{LUA} 第 61 行的校验被删",))),
        commit=overrides.pop("commit", COMMIT),
        file_path=overrides.pop("file_path", LUA),
        impact=overrides.pop("impact", "任何玩家都能改队伍归属"),
        suggestion=overrides.pop("suggestion", "确认是否已移到服务端校验"),
        # 候选血缘（AI-P0-06）：对账**只按编号**，用例要显式声明它 ——
        # 漏传会静默退化成「汇总没有交回编号」，而症状与「这条候选没被采纳」逐字相同。
        source_candidate_ids=tuple(overrides.pop("source_candidate_ids", ())),
    )


def _row(reduction, finding_id: str = "F1"):
    return next(row for row in reduction.rows if row.finding_id == finding_id)


# ==========================================================================
# 口径一：「证据不足」降一档等级
# ==========================================================================


class TestTheEvidenceShortfallDropsTheSeverityOneNotch:
    def test_a_critical_goes_to_high(self):
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_NEEDS_MORE_EVIDENCE,
                    reason="找不到服务端那道校验的代码",
                ),
            ),
        )

        row = _row(reduction)
        assert row.anomaly.severity == "high", "清单里不许同时出现「critical」与「证据不足」"
        assert row.origin.severity == "critical", "原等级留着（报告要写从哪一级降下来）"
        assert row.anomaly.confidence == "high", "置信度同样不许维持 very_high"

    def test_a_high_goes_to_medium(self):
        reduction = reduce_findings(
            [_anomaly(severity="high", confidence="very_high")],
            verdicts=(
                VerifyVerdict(finding_id="F1", verdict=VERDICT_NEEDS_MORE_EVIDENCE),
            ),
        )

        assert _row(reduction).anomaly.severity == "medium"

    def test_the_ladder_is_the_documented_one(self):
        assert SEVERITY_STEP_DOWN == {
            "critical": "high",
            "high": "medium",
            "medium": "low",
            "low": "low",
        }
        assert severity_step_down("LOW") == "low", "大小写归一化后仍认"
        assert severity_step_down("unknown") == "unknown", "不认识的等级不许凭空编一个更低的"

    def test_a_broken_downgrade_lands_on_the_same_ladder(self):
        """「说降级却不给新等级」按「证据不足」处理（既有口径），所以它也要降一档。"""
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1", verdict=VERDICT_DOWNGRADED, reason="反证部分成立"
                ),
            ),
        )

        row = _row(reduction)
        assert row.verdict == VERDICT_NEEDS_MORE_EVIDENCE
        assert row.anomaly.severity == "high"
        assert "没有给出比" in row.note

    def test_a_real_downgrade_still_uses_the_written_level(self):
        """**对照组**：复核给了合法的新等级时按它走，不套降一档的阶梯。"""
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_DOWNGRADED,
                    final_severity="high",
                    reason="服务端另有一道校验",
                ),
            ),
        )

        assert _row(reduction).anomaly.severity == "high"
        assert _row(reduction).verdict == VERDICT_DOWNGRADED

    def test_the_section_says_which_notch_and_why(self):
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_NEEDS_MORE_EVIDENCE,
                    reason="缺一份服务端校验的代码",
                ),
            ),
        )

        section = render_ruling(reduction, review_ran=True)
        assert "等级 `critical` → `high`" in section
        assert "证据不足降一档" in section, "要写出这一档是从哪来的（平台口径，不是模型说的）"
        assert "缺一份服务端校验的代码" in section, "降级理由沿用复核给的理由"
        assert "待人工核验" in section

    def test_the_payload_carries_both_ends(self):
        """落库那一行：`severity` 是新的、`original_severity` 是旧的 —— 两个都要在。"""
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(finding_id="F1", verdict=VERDICT_NEEDS_MORE_EVIDENCE),
            ),
        )
        # 2026-09-21（AI-P0-05）：裁决不再序列化成正文末尾那行 HTML 注释，落库的形态
        # 就是 `reduction.as_dict()`（`EngineOutcome.verdict` 携带的正是它）。
        # 这一条测的是裁决**结果**（新旧等级都在），与「从哪里读回来」无关。
        ruling = reduction.as_dict()

        active = [row for row in ruling["rows"] if row["active"]]
        assert [row["severity"] for row in active] == ["high"]
        assert [row["original_severity"] for row in active] == ["critical"]
        assert active[0]["verdict"] == VERDICT_NEEDS_MORE_EVIDENCE


# ==========================================================================
# 口径二：正文编号 → 平台编号
# ==========================================================================


class TestTheBodyLabelMapping:
    def test_it_maps_by_the_file_path(self):
        report = (
            "## 风险评估\n\n"
            f"R3. 队伍成员校验被删除（critical，仍成立）\n   位置：{LUA}\n\n"
            "R4. 另一条与本条无关的问题\n   位置：config/别的表.xlsx\n"
        )

        labels = assign_body_labels(report, assign_findings([_anomaly()]))

        assert labels == {"F1": "R3"}

    def test_it_falls_back_to_the_title_when_there_is_no_path(self):
        anomaly = _anomaly(file_path="", title="本次改动没有走配表评审流程")
        report = "## 风险评估\n\nR7. 本次改动没有走配表评审流程，建议补一次评审。\n"

        labels = assign_body_labels(report, assign_findings([anomaly]))

        assert labels == {"F1": "R7"}

    def test_two_findings_claiming_one_number_are_both_left_unmapped(self):
        report = f"## 风险评估\n\nR3. 队伍成员校验被删除\n   位置：{LUA}\n"
        anomalies = [_anomaly(), _anomaly(title="队伍成员校验被删除（另一个说法）")]

        labels = assign_body_labels(report, assign_findings(anomalies))

        assert labels == {}, "至少有一条会指错 —— 宁可不写"

    def test_one_finding_matching_two_numbers_is_not_mapped(self):
        report = (
            "## 风险评估\n\n"
            f"R3. 队伍成员校验被删除\n   位置：{LUA}\n\n"
            f"R5. 队伍成员校验被删除（同一件事的复述）\n   位置：{LUA}\n"
        )

        labels = assign_body_labels(report, assign_findings([_anomaly()]))

        assert labels == {}, "分不清对应哪一条，不写"

    def test_the_mapping_must_not_come_from_inside_another_word(self):
        """**对照组**：`RF3` 不是正文编号（模型写它是在指别的编号体系）。"""
        report = (
            "## 风险评估\n\n"
            f"RF3. 队伍成员校验被删除\n   位置：{LUA}\n\n"
            f"R9. 队伍成员校验被删除\n   位置：{LUA}\n"
        )

        labels = assign_body_labels(report, assign_findings([_anomaly()]))

        assert labels == {"F1": "R9"}

    def test_a_finding_without_a_counterpart_says_nothing(self):
        """对不上正文编号时**什么都不写**（2026-09-24，run 63）。

        从前写的是「（正文未编号）」：头部于是成了「[F1]（正文未编号）」—— 一个平台内部
        编号，加一句平台自己承认「它在正文里找不到」。读者要落回正文靠的是**标题**，
        它就在这一行里；那个不存在的编号只添乱。
        """
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(finding_id="F1", verdict=VERDICT_CONFIRMED, reason="没有反证"),
            ),
            body_text="## 风险评估\n\nR1. 一条与它无关的问题\n   位置：config/别的表.xlsx\n",
        )

        row = _row(reduction)
        assert row.body_label == ""
        section = render_ruling(reduction, review_ran=True)
        assert "正文未编号" not in section, "那句自我否定的括注又回来了"
        assert "[F1]" not in section, (
            "平台内部编号印进了给人看的报告 —— 产品里没有一处显示 finding_id"
        )
        assert section.count(f"- **{row.anomaly.title}**") == 1, "这一条没有落点"

    def test_the_report_carries_the_body_number(self):
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(finding_id="F1", verdict=VERDICT_CONFIRMED, reason="没找到反证"),
            ),
            body_text=f"## 风险评估\n\nR3. 队伍成员校验被删除\n   位置：{LUA}\n",
        )

        assert "（正文 R3）" in render_ruling(reduction, review_ran=True)
        ruling = reduction.as_dict()
        assert ruling["rows"][0]["body_label"] == "R3"

    def test_no_body_text_means_everything_is_numbered_by_nothing(self):
        reduction = reduce_findings([_anomaly()])

        assert _row(reduction).body_label == ""


# ==========================================================================
# 口径三：依据的形状
# ==========================================================================


class TestTheEvidenceRefShape:
    @pytest.mark.parametrize(
        "ref",
        [
            HUNK_REF,
            LEGACY_HUNK_REF,
            f"{LUA} @@ -61 +66 @@ validate_member",
            f"`{LUA} @@ -61,0 +66,4 @@`",
        ],
    )
    def test_a_complete_unified_diff_hunk_is_locatable(self, ref):
        assert is_locatable_ref(ref) is True

    @pytest.mark.parametrize(
        "ref",
        [
            f"{LUA}:61",
            f"{LUA}:61-66",
            f"{LUA}:61 ~ 66",
            f"{TABLE}:Sheet1",
            f"{TABLE}:Sheet1!B2",
            f"{TABLE}:Sheet1!A1:C5",
            f"`{TABLE}:Sheet1!B2`",  # 模型常把依据用反引号包起来
            "C:/work/repo/a.lua:61",
            # **裸路径**（没有定位符）：平台自己的 diff 载荷就是这么标的，
            # 按提交取一份快照就能核 —— 判死它会把一批正常结论无故压档。
            LUA,
            TABLE,
            f"`{LUA}`",
            "build/lua/CfgItem.lua",
        ],
    )
    def test_the_accepted_shapes(self, ref):
        assert is_locatable_ref(ref) is True

    @pytest.mark.parametrize(
        "ref",
        [
            "",
            "   ",
            "没有冒号的一段话",
            f"{LUA}:diff@@ -61,66 + @@",  # new 坐标不完整
            f"{LUA} @@ -61,66 @@",  # 缜密语法要求同时给出 old/new 坐标
            "@@ -61,66 +66,26 @@",  # 没有文件路径
            f"{LUA}:第 61 行",  # 行号与散文混在一起
            f"{LUA}:61,66",  # 逗号分隔不是行范围
            f"{LUA}:Sheet1",  # `.lua` 没有 sheet —— 这是自由文本，不是坐标
            "见 config/x.xlsx 第 3 个 sheet",
            f"{TABLE}:第 3 个 sheet",  # 带空白的定位符一律不成形（散文远多于真表名）
            f"{TABLE}:Sheet1!",  # 单元格是空的
            "详见上文",
            "同上",
            "（无）",
            "config/[30]道具表.xlsx：第 3 张表",  # 全角冒号 + 散文：整串没有坐标
            "code/qz_server/src/tms/module",  # 只有目录、没有扩展名：不是快照坐标
        ],
    )
    def test_everything_else_is_not_locatable(self, ref):
        assert is_locatable_ref(ref) is False

    def test_a_bare_path_keeps_the_confidence(self):
        """**① 的直接后果**：裸路径算可定位，于是一条只有裸路径依据的结论不再被压档。

        这条是防「放宽过了头」的另一半：`test_everything_else_is_not_locatable` 守的是
        「散文仍然拦得住」，这条守的是「真坐标不许误伤」。
        """
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_CONFIRMED,
                    reason="查了调用点，没找到反证",
                    evidence_refs=(LUA,),
                ),
            ),
        )

        row = _row(reduction)
        assert row.anomaly.confidence == "very_high"
        assert row.unlocatable_refs == ()
        assert "（不可定位）" not in render_ruling(reduction, review_ran=True)

    def test_a_complete_hunk_keeps_very_high_confidence(self):
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_CONFIRMED,
                    reason="查了调用点，没找到反证",
                    evidence_refs=(HUNK_REF,),
                ),
            ),
        )

        row = _row(reduction)
        assert row.active
        assert row.anomaly.confidence == "very_high"
        assert row.unlocatable_refs == ()
        assert row.evidence_capped is False

    def test_all_refs_unlocatable_cannot_stay_very_high(self):
        bad_ref = f"{LUA}:diff@@ -61,66 + @@"
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_CONFIRMED,
                    reason="查了调用点，没找到反证",
                    evidence_refs=(bad_ref,),
                ),
            ),
        )

        row = _row(reduction)
        assert row.active, "「不可定位」不是撤销的理由 —— 它只是不能当证据用"
        assert row.anomaly.confidence == CONFIDENCE_CEILING_WITH_GAP
        assert row.unlocatable_refs == (bad_ref,)
        assert row.evidence_capped is True
        assert "没有一条能定位" in row.note

    def test_one_locatable_ref_is_enough_to_keep_it(self):
        good = f"{LUA}:61"
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_CONFIRMED,
                    reason="查了调用点，没找到反证",
                    evidence_refs=(good, HUNK_REF),
                ),
            ),
        )

        row = _row(reduction)
        assert row.anomaly.confidence == "very_high", "有一条能定位的依据撑着，不压"
        assert row.unlocatable_refs == ()
        assert row.evidence_capped is False
        assert row.note == ""

    def test_the_report_marks_it_without_rewriting_it(self):
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_CONFIRMED,
                    evidence_refs=(HUNK_REF,),
                ),
            ),
        )

        section = render_ruling(reduction, review_ran=True)
        assert HUNK_REF in section, "模型写了什么，一字不许改写"
        assert f"{HUNK_REF}（不可定位）" not in section
        ruling = reduction.as_dict()
        assert ruling["rows"][0]["evidence_refs"] == [HUNK_REF], "机器可读的原文一字不动"
        assert ruling["rows"][0]["unlocatable_refs"] == []

    def test_a_locatable_ref_alone_is_not_marked(self):
        reduction = reduce_findings(
            [_anomaly()],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_CONFIRMED,
                    evidence_refs=(f"{LUA}:61",),
                ),
            ),
        )

        assert "（不可定位）" not in render_ruling(reduction, review_ran=True)


# ==========================================================================
# 口径四：证据缺口
# ==========================================================================


def _truncated_outcome(*paths: str, refused=(), degradation=DEGRADE_NONE) -> EngineOutcome:
    """一份「这些文件被截断过 / 这几次索取没轮到」的成员账（走真实的数据结构）。"""
    items = tuple(
        ContextItem(
            kind="file_diff",
            label=f"file_diff {'a' * 12} {path} lines=11-24",
            text="…",
            meta={"original_chars": 9000, "truncated": True, "limit": 2000},
        )
        for path in paths
    )
    return EngineOutcome(
        status=STATUS_SUCCEEDED,
        rounds=(RoundRecord(index=1, status="requests", executed=items, truncated=len(items)),),
        refused_requests=tuple(refused),
        degradation=degradation,
    )


class TestTheEvidenceGapsAreExtractedFromTheLedger:
    def test_the_quota_code_matches_the_engine(self):
        assert QUOTA_EXHAUSTED_CODE == DEGRADE_REQUESTS, (
            "读 `outcome.degradation` 的比对值必须与引擎写进去的那个码逐字一致"
        )

    def test_it_reads_the_truncated_file_names(self):
        gaps = evidence_gaps_of([_truncated_outcome(PROTO), _truncated_outcome(PROTO, LUA)])

        assert gaps.truncated_files == (PROTO, LUA), "去重且保序（同一个输入两次运行要同一句话）"
        assert gaps.requests_exhausted is False

    def test_it_reads_the_refused_labels_and_the_degradation_code(self):
        gaps = evidence_gaps_of(
            [
                _truncated_outcome(
                    refused=("引用扫描 TmsTeamMgrMod", f"{LUA}（61-66 行）"),
                    degradation=DEGRADE_REQUESTS,
                )
            ]
        )

        assert gaps.requests_exhausted is True
        assert gaps.quota_refusals == ("引用扫描 TmsTeamMgrMod", f"{LUA}（61-66 行）")

    def test_a_member_that_never_ran_contributes_nothing(self):
        assert evidence_gaps_of([None]) == EvidenceGaps()

    def test_a_run_without_gaps_knows_nothing(self):
        assert evidence_gaps_of([_truncated_outcome()]).known is False

    def test_the_label_format_is_the_real_one(self):
        """**这两条是防假绿的关键**：上面那些 fixture 里的标签是本文件自己写的。

        标签格式的真源在 `context_tools`（`describe_request` / `_human_request_label`），
        那里一改，我这份 fixture 不会跟着变、测试照样绿 —— 而线上读不到文件名，
        「引用的文件被截断过」这条信号就静默失效了（降级理由变成永不触发，谁也不报错）。
        所以这里拿**真的生成函数**喂进来，而不是再抄一遍格式。
        """
        from services.ai.context_tools import _human_request_label, describe_request
        from services.ai.protocol import ContextRequest

        request = ContextRequest(type="file_diff", commit="a" * 40, path=LUA)
        assert _label_file(describe_request(request)) == LUA

        windowed = ContextRequest(type="file_content", commit="a" * 40, path=LUA, lines="11-24")
        assert _label_file(describe_request(windowed)) == LUA, "带窗口后缀时也要读得出路径"

        assert _label_token(
            _human_request_label(ContextRequest(type="find_references", query="TmsTeamMgrMod"))
        ) == "TmsTeamMgrMod"
        assert _label_token(
            _human_request_label(ContextRequest(type="file_content", commit="a" * 40, path=LUA,
                                               lines="61-66"))
        ) == LUA


class TestAGapOnlyCapsTheFindingsItTouches:
    def test_a_truncated_cited_file_cannot_stay_very_high(self):
        gaps = EvidenceGaps(truncated_files=(PROTO,))
        reduction = reduce_findings(
            [_anomaly(file_path=PROTO, evidence=(f"{PROTO} 的协议字段被改名",))],
            verdicts=(
                VerifyVerdict(finding_id="F1", verdict=VERDICT_CONFIRMED, reason="没有反证"),
            ),
            gaps=gaps,
        )

        row = _row(reduction)
        assert row.anomaly.confidence == "high"
        assert row.evidence_capped is True
        assert PROTO in row.note and "截断" in row.note

    def test_truncation_does_not_touch_unrelated_findings(self):
        """**取舍**：只有它引用的那个文件被截断时才压，别的一律不动。"""
        gaps = EvidenceGaps(truncated_files=(PROTO,))
        reduction = reduce_findings(
            [_anomaly(file_path=LUA, evidence=(f"{LUA} 第 61 行的校验被删",))],
            verdicts=(
                VerifyVerdict(finding_id="F1", verdict=VERDICT_CONFIRMED, reason="没有反证"),
            ),
            gaps=gaps,
        )

        row = _row(reduction)
        assert row.anomaly.confidence == "very_high"
        assert row.evidence_capped is False

    def test_a_global_quota_exhaustion_alone_does_not_cap_everything(self):
        """`requests_exhausted` 是全运行级的信号 —— 被拒的那一块与这条无关时不许压它。"""
        gaps = EvidenceGaps(
            quota_refusals=("引用扫描 CfgRewardMode", "config/别的表.xlsx"),
            requests_exhausted=True,
        )
        reduction = reduce_findings([_anomaly()], gaps=gaps)

        row = _row(reduction)
        assert row.anomaly.confidence == "very_high", "拿与本案无关的事实去动真实结论"
        assert row.evidence_capped is False

    def test_a_quota_refusal_that_names_it_does_cap_it(self):
        gaps = EvidenceGaps(
            quota_refusals=("引用扫描 TmsTeamMgrMod", "config/别的表.xlsx"),
            requests_exhausted=True,
        )
        reduction = reduce_findings([_anomaly()], gaps=gaps)

        row = _row(reduction)
        assert row.anomaly.confidence == "high"
        assert row.evidence_capped is True
        assert "额度用尽" in row.note and "引用扫描 TmsTeamMgrMod" in row.note
    def test_a_refused_file_request_that_names_it_also_caps_it(self):
        gaps = EvidenceGaps(quota_refusals=(f"{LUA}（61-66 行）",), requests_exhausted=True)

        row = _row(reduce_findings([_anomaly()], gaps=gaps))

        assert row.anomaly.confidence == "high"
        assert LUA in row.note

    def test_an_unreviewed_finding_is_capped_too(self):
        """复核只核对最严重的几条 —— 没被复核到的那几条照样受缺口影响。"""
        reduction = reduce_findings(
            [_anomaly(file_path=PROTO)], gaps=EvidenceGaps(truncated_files=(PROTO,))
        )

        row = _row(reduction)
        assert row.verdict == UNREVIEWED_LABEL or row.verdict == ""
        assert row.anomaly.confidence == "high"
        assert reduction.changed is True, "压了置信度就必须渲染那一节来说明"

    def test_a_retracted_finding_is_left_alone(self):
        gaps = EvidenceGaps(truncated_files=(PROTO,))
        reduction = reduce_findings(
            [_anomaly(file_path=PROTO)],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1",
                    verdict=VERDICT_RETRACTED,
                    reason="同一提交里已经删掉了",
                    evidence_refs=(f"{PROTO}:12",),
                ),
            ),
            gaps=gaps,
        )

        assert reduction.retracted[0].evidence_capped is False

    def test_the_gap_section_explains_it(self):
        reduction = reduce_findings(
            [_anomaly(file_path=PROTO)],
            verdicts=(
                VerifyVerdict(finding_id="F1", verdict=VERDICT_CONFIRMED, reason="没有反证"),
            ),
            gaps=EvidenceGaps(truncated_files=(PROTO,)),
        )

        section = render_ruling(reduction, review_ran=True)
        assert "证据缺口" in section
        assert "不得维持 `very_high`" in section
        assert PROTO in section

    def test_the_gap_section_is_not_rendered_when_nothing_was_capped(self):
        reduction = reduce_findings([_anomaly()], gaps=EvidenceGaps())

        assert "证据缺口" not in render_ruling(reduction, review_ran=True)
        assert reduction.evidence_capped == 0

    def test_a_row_that_no_other_section_lists_is_listed_here(self):
        """复核只核对最严重的几条 —— 没被核到的那几条只能在这里被逐条列出来。"""
        reduction = reduce_findings(
            [_anomaly(file_path=PROTO)], gaps=EvidenceGaps(truncated_files=(PROTO,))
        )

        section = render_ruling(reduction, review_ran=True)
        assert f"- **{_anomaly(file_path=PROTO).title}**" in section
        assert "证据有**已知缺口**" in section
        assert "已经在上面" not in section, "它没在上面任何一节里出现过"
        assert "那不是复核的裁决" in section, (
            "一条裁决都没有时，那句话必须说清「压置信度是平台自己的动作」"
        )

    def test_a_row_already_listed_is_not_listed_twice(self):
        reduction = reduce_findings(
            [_anomaly(file_path=PROTO)],
            verdicts=(
                VerifyVerdict(finding_id="F1", verdict=VERDICT_CONFIRMED, reason="没有反证"),
            ),
            gaps=EvidenceGaps(truncated_files=(PROTO,)),
        )

        section = render_ruling(reduction, review_ran=True)
        assert section.count("### 证据缺口") == 1
        assert "已经在上面" in section
        assert section.count(f"- **{_anomaly(file_path=PROTO).title}**") == 1, (
            "同一件事在报告里出现两遍"
        )

    def test_the_unknown_authors_are_kept_apart(self):
        """模型写的理由与平台写的理由**分开**：读的人要分得清哪句是谁说的。"""
        reduction = reduce_findings(
            [_anomaly(file_path=PROTO)],
            verdicts=(
                VerifyVerdict(
                    finding_id="F1", verdict=VERDICT_CONFIRMED, reason="查了调用点，没有反证"
                ),
            ),
            gaps=EvidenceGaps(truncated_files=(PROTO,)),
        )

        row = _row(reduction)
        assert row.reason == "查了调用点，没有反证"
        assert row.note.startswith("证据有**已知缺口**")

    def test_gap_reasons_only_read_the_ledger(self):
        """判据是账本，不是报告里的自由文本（改一个字就失效的判据不能用）。"""
        gaps = EvidenceGaps(truncated_files=(PROTO,))

        assert gap_reasons_of(_anomaly(file_path=PROTO), gaps) != []
        assert gap_reasons_of(_anomaly(file_path=LUA), gaps) == []
        assert gap_reasons_of(_anomaly(file_path=PROTO), EvidenceGaps()) == []


# ==========================================================================
# 端到端：一家子跑完之后的落点
# ==========================================================================


class TestTheFamilyPathAppliesTheGaps:
    def _steps(self, *, verify_outcome, member_outcome):
        subagent = MemberPlan(index=1, label="S1", role="subagent", dimensions=("logic_change",))
        verify = MemberPlan(index=3, label="V1", role="verify", dimensions=())
        return [
            MemberOutcome(plan=subagent, outcome=member_outcome),
            MemberOutcome(plan=verify, outcome=verify_outcome),
        ]

    def _synthesis(self):
        return EngineOutcome(
            status=STATUS_SUCCEEDED,
            anomalies=(_anomaly(file_path=PROTO, evidence=(f"{PROTO} 的协议字段被改名",)),),
            report_markdown=f"## 风险评估\n\nR1. 队伍成员校验被删除\n   位置：{PROTO}\n",
        )

    def _verify(self):
        return EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown=(
                "# 对账\n\n第 1 条 ××：未找到反证。\n\n```json\n"
                '{"verdicts": [{"finding_id": "F1", "verdict": "confirmed", '
                '"reason": "查了调用点，没找到反证"}]}\n```\n'
            ),
        )

    def test_the_truncation_in_a_shard_reaches_the_synthesis_conclusion(self):
        merged = aggregate_outcomes(
            synthesis=self._synthesis(),
            steps=self._steps(
                verify_outcome=self._verify(),
                member_outcome=_truncated_outcome(PROTO),
            ),
        )

        assert [item.confidence for item in merged.anomalies] == ["high"], (
            "汇总那条的引用文件被分片截断过，置信度不许维持 very_high"
        )
        assert "证据缺口" in merged.report_markdown
        assert "（正文 R1）" in merged.report_markdown, "这一行要能落回正文"

    def test_the_review_running_is_what_makes_it_renderable(self):
        """复核没跑成时**不压**：那一节是这些降级唯一会被说明的地方。

        压了却无处说明，就是一次静默改动 —— 而静默正是这批缺陷的共同形态。
        """
        merged = aggregate_outcomes(
            synthesis=self._synthesis(),
            steps=self._steps(
                verify_outcome=None,
                member_outcome=_truncated_outcome(PROTO),
            ),
        )

        assert [item.confidence for item in merged.anomalies] == ["very_high"]
        assert "证据缺口" not in merged.report_markdown


# ==========================================================================
# 接线：`aggregate_outcomes` → `reconcile_candidates(reduction=…)`
# ==========================================================================


class TestTheReconcileCallIsWiredToTheSameReduction:
    """对账侧（`family_ledger.reconcile_candidates`）把 `reduction` 做成 keyword-only。

    也就是说**不传也不会报错** —— 但它会掉进另一个分支。所以「接线」这件事只有在这里
    钉得住：不钉，它会在某次重构里静默掉线，而掉线的症状（同一件事在报告里说两遍、
    两遍互相矛盾）没人会当成回归。

    ## 接没接线的判据（AI-P0-06 之后：去向只按编号认）

    这条候选的编号由**汇总**声明（`_merged` 里的 `source_candidate_ids=("S1-1",)`），
    而它的处置来自**对账轮**的裁决。两个分支各有一句独有的措辞：

    * **接线了**：`_ruling_fate` 在对账轮的裁决行里按编号认出它 → 「已撤销」那一段按候选
      逐条写（`RULING_CLAIM_PHRASE`），结算那一段不出现；
    * **没接线**（`reduction=None`）：认领无从谈起，它落到「编号交回过、但没有留在最终
      清单里」那一段（`SETTLED_PHRASE`），逐条写的那一段不出现。

    两个措辞各只属于一个分支，所以这一对断言能分辨接线与否（而不是靠「这一节永远不
    出现」造成的假绿）。
    """

    # 「已撤销」那一段的独有措辞（`family_ledger._RULING_BLOCK_TEXT[VERDICT_RETRACTED]`）。
    RULING_CLAIM_PHRASE = "撤销本身也是结论，不是遗漏"
    # 「编号交回过、但没留在最终清单里」那一段的独有措辞（`reconcile_candidates` 尾部的
    # 结算块）—— 只有没接到 `reduction` 时这条候选才会落到那里。
    SETTLED_PHRASE = "没有留在最终清单里"
    # 「找不到去向」那一段的独有措辞。
    GAP_PHRASE = "需要人工看一眼"

    def _candidate(self):
        """一条**只能**被裁决按编号认领的候选（`file_path` 留空，用例尽量小）。

        汇总声明了它的编号（`S1-1`），处置则在裁决行上 —— 所以它到底落在「已撤销」还是
        「结算」那一段，完全取决于 `reduction` 有没有传进对账。
        """
        return Candidate(
            member_label="S1", index=1, anomaly=_anomaly(file_path="", title=_anomaly().title)
        )

    def _merged(self, *, candidates):
        retract = (
            "# 对账\n\n第 1 条 ××：反证成立 —— 服务端那道校验还在。\n\n```json\n"
            '{"verdicts": [{"finding_id": "F1", "verdict": "retracted", '
            '"reason": "服务端另有一道校验会挡住", "evidence_refs": ["code/a.lua:61"]}]}\n```\n'
        )
        return aggregate_outcomes(
            synthesis=EngineOutcome(
                status=STATUS_SUCCEEDED,
                # 汇总声明这条结论来源于 `S1-1` —— 对账据此把候选与裁决行对上。
                anomalies=(_anomaly(source_candidate_ids=("S1-1",)),),
                report_markdown="# 报告\n",
            ),
            steps=[
                MemberOutcome(
                    plan=MemberPlan(index=1, label="S1", role="subagent", dimensions=("logic_change",)),
                    outcome=_truncated_outcome(),
                ),
                MemberOutcome(
                    plan=MemberPlan(index=3, label="V1", role="verify", dimensions=()),
                    outcome=EngineOutcome(status=STATUS_SUCCEEDED, report_markdown=retract),
                ),
            ],
            candidates=candidates,
        )

    def test_a_retracted_candidate_is_not_reported_as_a_missing_one(self):
        merged = self._merged(candidates=(self._candidate(),))

        assert "[S1-1]" in merged.report_markdown
        assert "已撤销" in merged.report_markdown, "去向要如实说「已撤销」"
        assert self.RULING_CLAIM_PHRASE in merged.report_markdown, (
            "按编号认领那一段没出现 —— 接线没生效（`reduction=` 没传进对账，"
            "候选掉进了结算那一支）"
        )
        assert self.SETTLED_PHRASE not in merged.report_markdown, (
            "被复核撤销的候选被写进了「编号交回过但没留在清单里」那一支"
        )
        assert self.GAP_PHRASE not in merged.report_markdown, (
            "被复核撤销的候选仍被写进「信息缺口」"
        )

    def test_a_candidate_no_ruling_matches_is_still_reported(self):
        """**对照组**：上面那句断言不是「这一节永远不出现」造成的假绿。"""
        orphan = Candidate(
            member_label="S2",
            index=7,
            anomaly=_anomaly(file_path="", title="一条谁都没提过的问题"),
        )

        merged = self._merged(candidates=(self._candidate(), orphan))

        assert self.GAP_PHRASE in merged.report_markdown
        assert "[S2-7]" in merged.report_markdown
        assert "已撤销" in merged.report_markdown, "对照组与撤销那一条共存"
        assert self.RULING_CLAIM_PHRASE in merged.report_markdown, "撤销那一段仍在"


# ==========================================================================
# 两条边界（与既有口径的一致性）
# ==========================================================================


def test_the_gap_section_never_adds_a_top_level_heading():
    reduction = reduce_findings(
        [_anomaly(file_path=PROTO)], gaps=EvidenceGaps(truncated_files=(PROTO,))
    )

    section = render_ruling(reduction, review_ran=True)
    assert section.strip()
    assert not [line for line in section.split("\n") if line.startswith("# ")]


def test_a_run_without_any_gap_behaves_exactly_as_before():
    """默认值那条路逐字不变：没有缺口时一个字都不加、一个值都不动。

    这条判据以前落在「那行机器块是不是空串」上（`ruling_block(reduction) == ""`）。
    块没了（AI-P0-05），判据改成**同一个判据在新结构上的形态**：一条裁决都没被逐条应用
    （`verdicts_seen == 0`）、没有一条置信度被压（`evidence_capped == 0`）、没有拒收的条目、
    每一行都还是「未复核」且平台一个字都没写。这正是 `result_payload` 判定
    `EngineOutcome.verdict = None`（载荷里没有裁决）与 `render_ruling` 返回空串的依据
    —— 不是换个函数名让它恒真。
    """
    reduction = reduce_findings([_anomaly()])

    assert reduction.changed is False
    assert reduction.active_anomalies() == (_anomaly(),)
    assert render_ruling(reduction, review_ran=False) == "", "没缺口时那一节不该出现"
    ruling = reduction.as_dict()
    assert ruling["verdicts_seen"] == 0
    assert ruling["evidence_capped"] == 0
    assert ruling["rejected"] == []
    rows = ruling["rows"]
    assert rows, "结论清单本身就是这份裁决的内容（行必须在）"
    assert {row["verdict_label"] for row in rows} == {UNREVIEWED_LABEL}
    assert all(not row["note"] and not row["reason"] for row in rows), (
        "平台没话说的时候不许往行里塞说明"
    )
    assert all(row["source_candidate_ids"] == [] for row in rows)


# ==========================================================================
# 口径①的下游：注入侧把平台赋值的等级折回模型的闭集
# ==========================================================================


class TestThePromptSeverityIsFoldedBackToTheModelClosedSet:
    """`medium` / `low` 是平台赋值的等级，模型**不许写**（`skill_contract.SEVERITIES`）。

    但基线摘要（下一轮提示词里的「已报过的问题」）会把它送到模型眼前，模型会照抄 ——
    照抄回来的那条会被 `protocol` 按「severity 不在允许集合内」丢掉。折回**只发生在
    这一个字段**上：给人看的那几处（裁决节、`final_findings`、异常表、导出）仍然是平台
    降出来的真实等级。
    """

    def test_every_platform_assigned_level_is_folded(self):
        """防漂移：`verdict` 那架梯子能产出的等级，折回表必须都覆盖得住。

        梯子加一档而这里没跟着加，症状是那一档的结论在下一轮**静默消失** ——
        所以这条不写成「列表相等」而是「梯子的像集 ⊆ 折回表的键」。
        """
        from services.ai.baseline import _PROMPT_SEVERITY_FOLD

        produced = set(SEVERITY_STEP_DOWN.values()) - set(SEVERITIES)
        assert produced <= set(_PROMPT_SEVERITY_FOLD), (
            f"梯子能产出 {sorted(produced)}，折回表只认 {sorted(_PROMPT_SEVERITY_FOLD)}"
        )
        assert set(_PROMPT_SEVERITY_FOLD.values()) <= set(SEVERITIES), (
            "折回的目标必须是模型能写的等级，否则折了个寂寞"
        )

    def test_the_contract_set_itself_passes_through(self):
        from services.ai.baseline import _prompt_severity

        assert _prompt_severity("critical") == "critical"
        assert _prompt_severity("high") == "high"

    def test_an_unknown_level_is_not_invented(self):
        """这一层不认识的值**原样返回**：编一个等级在提示词里看不出任何异常。"""
        from services.ai.baseline import _prompt_severity

        assert _prompt_severity("medium") == "high"
        assert _prompt_severity("low") == "high"
        assert _prompt_severity("weird") == "weird"
        assert _prompt_severity("") == ""

    def test_the_digest_shows_the_model_a_level_it_may_write(self):
        """走一遍真正的渲染（不是只看那个小函数）：提示词里不许出现 `[medium]`。"""
        from services.ai.baseline import (
            STATE_OPEN,
            BaselineFinding,
            build_baseline_digest,
        )

        finding = BaselineFinding(
            fingerprint="f" * 16,
            title="队伍成员校验被删除，可能越权",
            severity="medium",
            file_path=LUA,
            state=STATE_OPEN,
        )
        text = build_baseline_digest((finding,), note="")

        assert "队伍成员校验被删除" in text, "这条没进摘要的话，下面那句断言是空过的"
        assert "[medium]" not in text
        assert "[high]" in text

    def test_the_level_the_model_sees_is_recorded_at_the_real_one(self):
        """**另一半**：折回只动提示词那一个字段 —— 落库/给人看的仍然是真实的降档等级。

        不钉这一条，后来的人会以为「折回」是在掩盖降档。
        """
        reduction = reduce_findings(
            [_anomaly(severity="high")],
            verdicts=(
                VerifyVerdict(finding_id="F1", verdict=VERDICT_NEEDS_MORE_EVIDENCE),
            ),
        )

        assert reduction.active[0].anomaly.severity == "medium", "落库的值是真实的降档等级"
        assert "medium" in render_ruling(reduction, review_ran=True)
