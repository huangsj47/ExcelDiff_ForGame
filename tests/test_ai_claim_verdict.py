# -*- coding: utf-8 -*-
"""原子断言的逐条裁决（P0-01）。

## 这一组钉住的真实错误

实测 run 58（job 27）的 F3：标题写「取档失败路径改为**断言中断进程**」，而对账轮在自己的
理由里逐字写着「`assert(false)` 是中断整个进程还是仅中断本次登录请求，**未能核实**」——
整条仍是 `critical` / `very_high` / `confirmed`。同一轮的 F2 写「未见旧数据迁移」，
复核只重读了已有依据、扫的范围是「本批 120 个文件」（窗口有 1343 个），也裁成 `confirmed`。

根因不是复核不诚实，是**粒度**：一条结论是一句复合断言，而裁决只能落在整条上，
「这句里有一半没核实」无处安放。所以：

* 结论带 `claims[]`（原子断言，各自标类型）；
* 复核逐条回答（`status` + 它**查过的范围** + 是独立取证还是复读）；
* **任一断言没被证实 ⇒ 整条不得 `confirmed`**，标题也只能由已证实的部分构成；
* 「只重看了已有依据」叫**原证据复读**，不许显示成「反证不成立（独立核过）」。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from services.ai.claims import Claim, parse_claims
from services.ai.engine import EngineOutcome
from services.ai.protocol import Anomaly
from services.ai.subagent import verify_did_independent_work
from services.ai.subagent_tasks import build_verify_task
from services.ai.verdict import (
    CLAIM_REFUTED,
    CLAIM_UNREADABLE,
    CLAIM_UNVERIFIED,
    CLAIM_VERIFIED,
    VERDICT_CONFIRMED,
    VERDICT_NEEDS_MORE_EVIDENCE,
    VERDICT_RETRACTED,
    VERIFY_BASIS_INDEPENDENT,
    VERIFY_BASIS_REPLAY,
    parse_verdicts,
    reduce_findings,
    render_ruling,
    verdict_instructions,
)

# 断言编号用平台发出去的那一套（`protocol._as_claims` 生成的 `C1`/`C2`…）。
DIFF_CLAIM = Claim(
    claim_id="C1",
    kind="fact",
    statement="调用从 return false 改成 assert(bResult)",
    evidence_refs=("code/x.lua:88",),
)
CONSEQUENCE_CLAIM = Claim(
    claim_id="C2",
    kind="inference",
    statement="断言会中断整个进程",
)


def _anomaly(*claims: Claim, title: str = "取档失败路径改为断言中断进程") -> Anomaly:
    return Anomaly(
        title=title,
        category="code_logic",
        severity="critical",
        confidence="very_high",
        evidence=("code/x.lua:88 的调用改了",),
        file_path="code/x.lua",
        claims=tuple(claims),
    )


def _reply(*blocks: str) -> str:
    """对账轮的正文 + 末尾那个 json 块。"""
    return "核对完毕。\n\n```json\n{\"verdicts\": [" + ",".join(blocks) + "]}\n```"


def _synthesis(*claims: Claim) -> EngineOutcome:
    """汇总那一份产出（任务书读它的 `anomalies`）。"""
    return EngineOutcome(status="succeeded", anomalies=(_anomaly(*claims),))


def _verdict(finding_id: str, claims: str, verdict: str = "confirmed", **extra) -> str:
    fields = "".join(f', "{key}": "{value}"' for key, value in extra.items())
    return (
        f'{{"finding_id": "{finding_id}", "verdict": "{verdict}", "claims": [{claims}]{fields}}}'
    )


def _claim_reply(
    claim_id: str,
    status: str,
    *,
    basis: str = "",
    scope: str = "",
    complete: bool | None = None,
    reason: str = "",
) -> str:
    parts = [f'"claim_id": "{claim_id}"', f'"status": "{status}"']
    if basis:
        parts.append(f'"basis": "{basis}"')
    if scope:
        parts.append(f'"checked_scope": {scope}')
    if complete is not None:
        parts.append(f'"scope_complete": {"true" if complete else "false"}')
    if reason:
        parts.append(f'"reason": "{reason}"')
    return "{" + ", ".join(parts) + "}"


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


class TestTheClaimListIsRead:
    def test_a_claim_without_a_number_gets_one_from_the_platform(self):
        """编号是平台随后要拿去问复核、再按编号收回来的锚点 —— 缺了必须补上。"""
        claims, dropped = parse_claims([{"statement": "改成了断言"}, {"statement": "会中断进程"}], 0)
        assert [claim.claim_id for claim in claims] == ["C1", "C2"]
        assert dropped == ()

    def test_the_model_may_write_the_keys_a_different_way(self):
        """`text`/`id`/`layer` 这些写法都收 —— 因为一个近义词而丢掉整条断言，代价更大。"""
        claims, _ = parse_claims(
            [
                {
                    "id": "c3",
                    "type": "negative_scope",
                    "text": "整个项目没有迁移",
                    "layer": "window_history",
                    "evidence": "config/item.xlsx:12",
                }
            ],
            0,
        )
        assert len(claims) == 1
        assert claims[0].claim_id == "C3"
        assert claims[0].kind == "negative_scope"
        assert claims[0].statement == "整个项目没有迁移"
        assert claims[0].source_layer == "window_history"
        assert claims[0].evidence_refs == ("config/item.xlsx:12",)

    def test_a_claim_without_a_statement_is_dropped_and_accounted(self):
        claims, dropped = parse_claims([{"kind": "fact"}, {"statement": "留下的一条"}], 2)
        assert [claim.statement for claim in claims] == ["留下的一条"]
        assert len(dropped) == 1
        assert "缺少正文" in dropped[0].reason

    def test_an_unknown_kind_is_recorded_but_kept(self):
        """认不出来的类型按正向事实处理（默认成「范围声明」会让每次复核都报范围不足）。"""
        claims, dropped = parse_claims([{"kind": "rumour", "statement": "一条断言"}], 0)
        assert claims[0].kind == "fact"
        assert "kind 不在清单内" in dropped[0].reason

    def test_a_missing_kind_is_not_accounted_as_a_problem(self):
        """一个字都没写 `kind` 是正常兜底，不该在 trace 里刷假账。"""
        _, dropped = parse_claims([{"statement": "没写 kind 的一条"}], 0)
        assert dropped == ()

    def test_a_duplicate_claim_number_is_renumbered_not_dropped(self):
        claims, _ = parse_claims(
            [{"claim_id": "C1", "statement": "第一条"}, {"claim_id": "C1", "statement": "第二条"}],
            0,
        )
        assert [claim.claim_id for claim in claims] == ["C1", "C1-2"]


# --------------------------------------------------------------------------
# 逐条裁决（P0-01 的主判据）
# --------------------------------------------------------------------------


class TestAFindingWithAnUnverifiedClaimCannotBeConfirmed:
    def test_the_run_58_case(self):
        """F3 的两条断言：`assert` 那条可确认，「中断整个进程」那条核不了。

        断言：整条**不得 confirmed**，标题里不许再出现那句没被证实的后果。
        """
        reply = _reply(
            _verdict(
                "F1",
                _claim_reply("C1", "verified", basis="replay", scope='{"paths": ["code/x.lua"]}')
                + ","
                + _claim_reply("C2", "unverified", reason="查不到框架的异常边界"),
            )
        )
        reduction = reduce_findings([_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)], verdicts=parse_verdicts(reply))

        row = reduction.rows[0]
        assert row.verdict == VERDICT_NEEDS_MORE_EVIDENCE, (
            "有一条断言没被证实，整条却仍是 confirmed —— 这正是 run 58 的那个错误"
        )
        assert "中断进程" not in row.anomaly.title, "标题里还留着没被证实的断言"
        assert "调用从 return false 改成 assert(bResult)" in row.anomaly.title
        assert row.origin.title == "取档失败路径改为断言中断进程", "原标题要原样留着（审计）"

        statuses = {review.claim.claim_id: review.status for review in row.claim_reviews}
        assert statuses == {"C1": CLAIM_VERIFIED, "C2": CLAIM_UNVERIFIED}
        assert [r.display for r in row.claim_reviews if r.status == CLAIM_UNVERIFIED] == [
            "待核查：断言会中断整个进程"
        ]

    def test_the_verifier_gets_told_which_claims_blocked_it(self):
        reply = _reply(
            _verdict(
                "F1",
                _claim_reply("C1", "verified") + "," + _claim_reply("C2", "unverified"),
            )
        )
        reduction = reduce_findings([_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)], verdicts=parse_verdicts(reply))
        note = reduction.rows[0].note
        assert "C2" in note and "没有被证实" in note
        assert "证据不足" in note

    def test_all_claims_verified_keeps_the_original_title_and_verdict(self):
        """反向自检：两条都证实了就不该动它 —— 否则上面那条判红的可能是别的原因。"""
        reply = _reply(
            _verdict(
                "F1",
                _claim_reply("C1", "verified", basis="independent")
                + ","
                + _claim_reply("C2", "verified", basis="independent"),
            )
        )
        reduction = reduce_findings([_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)], verdicts=parse_verdicts(reply))
        row = reduction.rows[0]
        assert row.verdict == VERDICT_CONFIRMED
        assert row.anomaly.title == row.origin.title
        assert row.pending_claims == ()

    def test_a_claim_the_verifier_never_answered_counts_as_unverified(self):
        """只回了一部分 = 没回答的那些**没被证实**，不是「默认成立」。"""
        reply = _reply(_verdict("F1", _claim_reply("C1", "verified")))
        reduction = reduce_findings([_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)], verdicts=parse_verdicts(reply))
        assert reduction.rows[0].verdict == VERDICT_NEEDS_MORE_EVIDENCE
        assert "中断进程" not in reduction.rows[0].anomaly.title

    def test_a_refuted_claim_blocks_confirmation_too(self):
        reply = _reply(
            _verdict(
                "F1",
                _claim_reply("C1", "verified")
                + ","
                + _claim_reply("C2", "refuted", basis="independent", reason="框架里另有兜底"),
            )
        )
        row = reduce_findings([_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)], verdicts=parse_verdicts(reply)).rows[0]
        assert row.verdict == VERDICT_NEEDS_MORE_EVIDENCE
        assert [r.status for r in row.claim_reviews] == [CLAIM_VERIFIED, CLAIM_REFUTED]
        assert "反证成立：断言会中断整个进程" in [r.display for r in row.claim_reviews]

    def test_a_finding_without_claims_keeps_the_old_behaviour(self):
        """旧形态（没带 `claims[]`）逐字不变：平台的协议偏差不该改这条结论的等级。"""
        reply = _reply(_verdict("F1", ""))
        row = reduce_findings([_anomaly()], verdicts=parse_verdicts(reply)).rows[0]
        assert row.verdict == VERDICT_CONFIRMED
        assert row.claim_reviews == ()


# --------------------------------------------------------------------------
# 被裁决过的结论不许从清单里消失（真机 run 60）
# --------------------------------------------------------------------------


class TestAnAdjudicatedFindingKeepsTheIdentityThePayloadLooksItUpBy:
    """真机 run 60 实测：被裁决过的 3 条**从 `final_findings` 里整条消失了**。

    指纹是 `commit + 文件 + 标题词集`（`rules.anomaly_fingerprint`），而 P0 的标题改写
    （「只拿已证实的断言当标题」）**会改变它** —— 于是同一行存在两个指纹：
    `FindingRow.fingerprint` 按**原**标题算，而载荷是拿**裁决后**的结论去对账的。
    两边对不上时 `result_payload` 里两处查找（裁决行 ↔ 结论、撤销过滤）一起落空，
    而落空的形状**恰好只落在被裁决过的那几条**上：

    * `final_findings` 少了它们（实测 16 → 13）—— 下一轮基线与导出文档都接不到；
    * 逐条断言、取证方式、原标题一个都没附上（面板显示「未复核」，而正文写着已裁决）；
    * 撤销（`retracted_fingerprints`）对它们同样失效（被撤销的结论会留在活动清单里）。

    这两条用例一条钉身份、一条钉端到端（载荷里那一条还在，且带着断言）。
    """

    def _reduction(self):
        reply = _reply(
            _verdict(
                "F1",
                _claim_reply("C1", "verified", basis="independent", scope='{"paths": ["code/x.lua"]}')
                + ","
                + _claim_reply("C2", "unverified", reason="查不到框架的异常边界"),
            )
        )
        return reduce_findings(
            [_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)], verdicts=parse_verdicts(reply)
        )

    def test_the_row_fingerprint_is_the_one_the_payload_computes(self):
        from services.ai.rules import anomaly_fingerprint

        row = self._reduction().rows[0]
        assert row.anomaly.title != row.origin.title, "前提：这一条的标题被平台改写过"
        assert row.fingerprint == anomaly_fingerprint(row.anomaly), (
            
            "裁决行的指纹按原标题算，而载荷按裁决后的结论找它 —— 对不上就整条消失"
        )

    def test_the_adjudicated_finding_is_still_in_the_final_list_with_its_claims(self):
        from services.ai.result_payload import result_payload

        reduction = self._reduction()
        outcome = EngineOutcome(
            status="succeeded",
            anomalies=reduction.active_anomalies(),
            verdict=reduction.as_dict(),
        )
        result = result_payload(outcome, {"mode": "weekly", "summary": {}})

        ids = [item["finding_id"] for item in result["final_findings"]]
        assert ids == ["F1"], f"被裁决过的那条从 final_findings 里掉了：{ids}"
        assert result["final_findings"][0]["claims"], "逐条断言的裁决没附上"
        assert result["final_findings"][0]["verify_basis_label"]
        assert result["anomalies"][0]["verify_verdict"] == VERDICT_NEEDS_MORE_EVIDENCE, (
            "落库那一份读的是裁决行 —— 对不上时它显示「未复核」"
        )
    def test_a_retracted_row_with_a_rewritten_title_is_still_filtered_out(self):
        """**撤销**也必须照样生效：被撤销的结论不许留在活动清单里。

        `result_payload` 按 `retracted_fingerprints` 再滤一道（那是「落库侧不必相信
        上游做对了」的闸门）。标题被改写之后这个闸门对**恰好被撤销的那几条**失效 ——
        它与上一条是同一处身份问题的两个面，所以分开钉：一条钉`还在`，一条钉`不在`。
        """
        from services.ai.result_payload import result_payload
        from services.ai.verdict import retracted_fingerprints

        reply = _reply(
            _verdict(
                "F1",
                _claim_reply("C1", "verified", basis="independent")
                + ","
                + _claim_reply("C2", "unverified"),
                verdict="retracted",
                # 撤销必须带理由：既没理由也没依据的「撤销」平台按证据不足处理
                # （`_apply_verdict_inner` 那条分支），那是另一件事，不是本用例要钉的。
                reason="框架里另有兜底，这条断言不成立",
            )
        )
        reduction = reduce_findings(
            [_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)], verdicts=parse_verdicts(reply)
        )
        row = reduction.rows[0]
        assert row.verdict == VERDICT_RETRACTED
        assert row.anomaly.title != row.origin.title, "前提：撤销的这条标题也被改写过"
        assert row.fingerprint in retracted_fingerprints(reduction.as_dict())

        # **故意把上游那条约定破掉**：把已被撤销的那条仍然塞进 `outcome.anomalies`。
        # `result_payload` 里面那道闸门存在的意义就是「落库侧不相信上游做对了」，
        # 而喂 `active_anomalies()`（已摘掉撤销项）会让它**无事可做地通过** —— 那样
        # 这条用例对指纹改不改都不敏感（变异验证时它就是这么假绿的）。
        outcome = EngineOutcome(
            status="succeeded",
            anomalies=(row.anomaly,),
            verdict=reduction.as_dict(),
        )
        result = result_payload(outcome, {"mode": "weekly", "summary": {}})
        assert result["anomalies"] == [], "被撤销的结论还留在活动清单里"
        trail = [item for item in result["final_findings"] if not item["active"]]
        assert [item["finding_id"] for item in trail] == ["F1"], "撤销的痕迹要从审计轨迹里找得到"

# --------------------------------------------------------------------------
# 否定性范围声明（run 58 的 F2）
# --------------------------------------------------------------------------


class TestANegativeClaimNeedsItsWholeScopeChecked:
    NEGATIVE = Claim(
        claim_id="C1",
        kind="negative_scope",
        statement="整个项目没有迁移兼容读取",
        evidence_refs=("config/item.xlsx:12",),
    )

    def test_scanning_a_part_of_the_project_is_not_evidence_for_the_whole_project(self):
        """F2 的形状：只扫了本批 120 个文件（窗口 1343 个），却声称整个项目没有。"""
        reply = _reply(
            _verdict(
                "F1",
                _claim_reply(
                    "C1",
                    "verified",
                    scope='{"files_checked": 120, "files_total": 1343}',
                    complete=False,
                ),
            )
        )
        row = reduce_findings([_anomaly(self.NEGATIVE)], verdicts=parse_verdicts(reply)).rows[0]
        assert row.verdict == VERDICT_NEEDS_MORE_EVIDENCE, "范围不足的负断言被当成了已证实"
        review = row.claim_reviews[0]
        assert review.status == CLAIM_UNVERIFIED
        assert review.narrowed is True
        assert review.display == "已检查范围内未发现：整个项目没有迁移兼容读取"
        assert review.checked_scope == "文件 120/1343", "查过的范围必须跟着断言一起留下"

    def test_a_scope_wide_enough_does_verify_it(self):
        """反向自检：真的把声称的范围查完了，这条负断言就可以证实。"""
        reply = _reply(
            _verdict(
                "F1",
                _claim_reply(
                    "C1",
                    "verified",
                    basis="independent",
                    scope='{"files_checked": 1343, "files_total": 1343}',
                    complete=True,
                ),
            )
        )
        row = reduce_findings([_anomaly(self.NEGATIVE)], verdicts=parse_verdicts(reply)).rows[0]
        assert row.verdict == VERDICT_CONFIRMED
        assert row.claim_reviews[0].status == CLAIM_VERIFIED
        assert row.claim_reviews[0].narrowed is False

    def test_claiming_completeness_without_saying_what_was_searched_is_not_enough(self):
        """只打个勾说「都查过了」不算声明 —— 否则范围外推只要写 `true` 就能洗白。"""
        reply = _reply(
            _verdict("F1", _claim_reply("C1", "verified", complete=True))
        )
        row = reduce_findings([_anomaly(self.NEGATIVE)], verdicts=parse_verdicts(reply)).rows[0]
        assert row.claim_reviews[0].status == CLAIM_UNVERIFIED
        assert row.claim_reviews[0].narrowed is False, "没说过查了哪儿，就不是「已检查范围内未发现」"

    def test_an_empty_scope_object_does_not_count_either(self):
        reply = _reply(
            _verdict("F1", _claim_reply("C1", "verified", scope="{}", complete=True))
        )
        row = reduce_findings([_anomaly(self.NEGATIVE)], verdicts=parse_verdicts(reply)).rows[0]
        assert row.claim_reviews[0].status == CLAIM_UNVERIFIED


class TestEvidenceThatCannotBeLocatedIsNotEvidence:
    def test_a_claim_whose_refs_are_all_unlocatable_is_unreadable(self):
        """依据是一条不落地的散文（「代码里某处」）⇒ 读不到，不是「已证实」。"""
        claim = Claim(
            claim_id="C1",
            kind="fact",
            statement="某处逻辑有问题",
            evidence_refs=("看起来有风险",),
        )
        reply = _reply(_verdict("F1", _claim_reply("C1", "verified")))
        row = reduce_findings([_anomaly(claim)], verdicts=parse_verdicts(reply)).rows[0]
        assert row.claim_reviews[0].status == CLAIM_UNREADABLE
        assert row.claim_reviews[0].display == "证据读不到：某处逻辑有问题"
        assert row.verdict == VERDICT_NEEDS_MORE_EVIDENCE

    def test_a_claim_with_one_locatable_ref_is_fine(self):
        """反向自检：只要有一条能照着去核，就不该判成读不到。"""
        claim = Claim(
            claim_id="C1",
            kind="fact",
            statement="某处逻辑有问题",
            evidence_refs=("看起来有风险", "code/x.lua:88"),
        )
        reply = _reply(_verdict("F1", _claim_reply("C1", "verified")))
        row = reduce_findings([_anomaly(claim)], verdicts=parse_verdicts(reply)).rows[0]
        assert row.claim_reviews[0].status == CLAIM_VERIFIED


# --------------------------------------------------------------------------
# 取证方式：独立取证 还是 原证据复读
# --------------------------------------------------------------------------


class TestHowTheVerificationWasDone:
    def test_replay_only_is_not_called_an_independent_counter_check(self):
        reply = _reply(_verdict("F1", _claim_reply("C1", "verified", basis="replay")))
        row = reduce_findings(
            [_anomaly(DIFF_CLAIM)], verdicts=parse_verdicts(reply), verify_independent=False
        ).rows[0]
        assert row.verify_basis == VERIFY_BASIS_REPLAY
        assert row.verify_basis_label == "原证据复读（未经独立反证）"

    def test_the_report_says_so_in_its_own_section(self):
        """两种「没找到反证」分成两节 —— 措辞本身是这一条缺陷的另一半。"""
        reply = _reply(_verdict("F1", _claim_reply("C1", "verified", basis="replay")))
        reduction = reduce_findings(
            [_anomaly(DIFF_CLAIM)], verdicts=parse_verdicts(reply), verify_independent=False
        )
        text = render_ruling(reduction, review_ran=True)
        assert "原证据复读" in text
        assert "未经独立反证" in text
        assert "反证不成立" not in text, (
            "只回放了已有依据，却写成「反证不成立（维持原结论）」—— 读的人会当成独立核过"
        )

    def test_independent_work_gets_the_other_section(self):
        reply = _reply(_verdict("F1", _claim_reply("C1", "verified", basis="independent")))
        reduction = reduce_findings(
            [_anomaly(DIFF_CLAIM)], verdicts=parse_verdicts(reply), verify_independent=True
        )
        row = reduction.rows[0]
        assert row.verify_basis == VERIFY_BASIS_INDEPENDENT
        text = render_ruling(reduction, review_ran=True)
        assert "独立取证后维持原结论" in text
        assert "原证据复读" not in text

    def test_the_platform_judges_it_by_what_the_verifier_actually_ran(self):
        """**不采信模型自述**：判据是它执行过的取数类型。"""
        def _step(stats):
            return SimpleNamespace(
                plan=SimpleNamespace(role="verify"),
                outcome=SimpleNamespace(status="succeeded", tool_stats=stats),
            )

        assert verify_did_independent_work(
            [_step({"evidence": {"calls": 6}})]
        ) is False, "按地址取回已有原文不算独立取证（run 58 的 6 次全是它）"
        assert verify_did_independent_work(
            [_step({"evidence": {"calls": 2}, "find_references": {"calls": 1}})]
        ) is True
        assert verify_did_independent_work([_step({})]) is False


# --------------------------------------------------------------------------
# 提示词与渲染
# --------------------------------------------------------------------------


class TestTheVerifierIsAskedPerClaim:
    def test_the_task_lists_every_claim_with_its_number(self):
        task = build_verify_task(
            SimpleNamespace(count=2, verify_items=3, synthesis_task="", dimensions=()),
            _synthesis(DIFF_CLAIM, CONSEQUENCE_CLAIM),
        )
        assert "`C1`" in task and "`C2`" in task
        assert "断言会中断整个进程" in task
        assert "逐条回答" in task

    def test_the_contract_requires_a_status_and_a_scope_per_claim(self):
        text = verdict_instructions()
        assert "claims" in text
        assert "`claim_id`" in text and "`basis`" in text and "`checked_scope`" in text
        assert "已检查范围内未发现" in text
        assert "independent" in text and "replay" in text

    def test_the_claim_list_reaches_the_report_line(self):
        reply = _reply(
            _verdict(
                "F1",
                _claim_reply("C1", "verified") + "," + _claim_reply("C2", "unverified"),
            )
        )
        reduction = reduce_findings([_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)], verdicts=parse_verdicts(reply))
        text = render_ruling(reduction, review_ran=True)
        assert "逐条断言" in text
        assert "**待核查**" in text
        assert "**已证实**" in text


@pytest.mark.parametrize("status", [CLAIM_VERIFIED, CLAIM_UNVERIFIED, CLAIM_REFUTED, CLAIM_UNREADABLE])
def test_every_claim_status_has_a_chinese_label(status):
    """四态各有中文名（服务端算，界面不自己映射 —— 与严重度那两栏同一条口径）。"""
    from services.ai.verdict import CLAIM_STATUS_LABELS

    assert CLAIM_STATUS_LABELS[status].strip()


# --------------------------------------------------------------------------
# 三个渲染点读同一份（裁决节 / 异常面板 / 导出文档）
# --------------------------------------------------------------------------


def _reviewed_row():
    reply = _reply(
        _verdict(
            "F1",
            _claim_reply("C1", "verified", basis="independent", scope='{"paths": ["code/x.lua"]}')
            + ","
            + _claim_reply("C2", "unverified", reason="查不到异常边界"),
        )
    )
    reduction = reduce_findings(
        [_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)],
        verdicts=parse_verdicts(reply),
        verify_independent=True,
    )
    return reduction.rows[0]


class TestTheExportReadsTheSameAdjudicatedClaims:
    def test_the_appendix_prints_the_claim_list(self):
        from services.ai.report_document import anomaly_rows

        payload_row = _reviewed_row().as_dict()
        item = {
            "title": payload_row["title"],
            "severity": payload_row["severity"],
            "confidence": payload_row["confidence"],
            "category": "code_logic",
            "file_path": payload_row["file_path"],
            "impact": "",
            "evidence": ["code/x.lua:88"],
            "suggestion": "",
            "fingerprint": payload_row["fingerprint"],
            "claims": payload_row["claims"],
        }
        rows = anomaly_rows([item])
        assert rows[0]["claims"], "导出附录没有带上断言清单"
        text = "；".join(rows[0]["claims"])
        assert "`C1`" in text and "已证实" in text
        assert "`C2`" in text and "待核查" in text
        assert "查过：路径 code/x.lua" in text, "查过的范围要跟着导出（读者据此判断能信到什么程度）"

    def test_the_exported_title_is_the_adjudicated_one(self):
        """导出那一栏写的是**裁决之后**的标题 —— 未证实的断言不许当结论印出去。"""
        payload_row = _reviewed_row().as_dict()
        assert "中断进程" not in payload_row["title"]
        assert payload_row["original_title"] == "取档失败路径改为断言中断进程"

    def test_an_anomaly_without_claims_is_unchanged(self):
        from services.ai.report_document import anomaly_rows

        rows = anomaly_rows([{"title": "老的一条", "severity": "high", "confidence": "high"}])
        assert rows[0]["claims"] == []


class TestTheStatusWordIsPrintedOnce:
    """三个渲染点印的都是服务端拼好的 `heading`：**状态词只说一次**（2026-09-24，run 63）。

    从前三处都是各自拼 `status_label + " —— " + display`，而 `display` 对非 `verified`
    的状态**本来就带状态前缀** —— 报告与面板上印出来的是

        证据读不到 —— 证据读不到：次数记账在批次交付之前执行，且注释声明次数一经发起不退还。

    同一行两个状态词。run 63 的报告里每一条断言都是这个样子。
    """

    def _unreadable_row(self):
        """一条「依据读不到」的断言：它同时走 status_label 与 display 两条路。

        （依据全不可定位时平台判 `unreadable`，见 `claims.claim_review_of`。）
        """
        claim = Claim(
            claim_id="C1",
            kind="fact",
            statement="次数记账在批次交付之前执行",
            evidence_refs=("代码里某处",),
        )
        reply = _reply(_verdict("F1", _claim_reply("C1", "verified")))
        return reduce_findings([_anomaly(claim)], verdicts=parse_verdicts(reply)).rows[0]

    def test_an_unreadable_claim_prints_the_word_once(self):
        row = self._unreadable_row()
        assert row.claim_reviews[0].status == CLAIM_UNREADABLE, "构造没生效"
        text = render_ruling(
            reduce_findings(
                [row.anomaly],
                verdicts=parse_verdicts(
                    _reply(_verdict("F1", _claim_reply("C1", "verified")))
                ),
            ),
            review_ran=True,
        )
        assert "`C1` **证据读不到**：次数记账在批次交付之前执行" in text
        assert "证据读不到 —— " not in text, "状态词印了两遍 —— 渲染点又在自己拼 display 前缀"

    def test_the_report_line_the_panel_and_the_export_all_read_the_heading(self):
        from services.ai.report_document import anomaly_rows

        row = _reviewed_row()  # C1 已证实 / C2 待核查
        payload_row = row.as_dict()
        claims = {item["claim_id"]: item for item in payload_row["claims"]}

        # 服务端那一份就是那一行（状态词 + 正文，各一次）。
        assert claims["C1"]["heading"] == "已证实：调用从 return false 改成 assert(bResult)"
        assert claims["C2"]["heading"] == "待核查：断言会中断整个进程"
        # `display` 仍是**标题安全**那一份：已证实的不带前缀（`compose_title` 拿它当标题）。
        assert claims["C1"]["display"] == "调用从 return false 改成 assert(bResult)"
        assert claims["C2"]["display"] == claims["C2"]["heading"]

        report = render_ruling(
            reduce_findings(
                [_anomaly(DIFF_CLAIM, CONSEQUENCE_CLAIM)],
                verdicts=parse_verdicts(
                    _reply(
                        _verdict(
                            "F1",
                            _claim_reply("C1", "verified")
                            + ","
                            + _claim_reply("C2", "unverified"),
                        )
                    )
                ),
            ),
            review_ran=True,
        )
        assert "`C1` **已证实**：调用从 return false 改成 assert(bResult)" in report
        assert "已证实 —— " not in report and "待核查 —— " not in report, (
            "报告那一行又印了两遍状态词"
        )

        exported = "；".join(
            anomaly_rows(
                [
                    {
                        "title": payload_row["title"],
                        "severity": payload_row["severity"],
                        "confidence": payload_row["confidence"],
                        "evidence": [],
                        "claims": payload_row["claims"],
                    }
                ]
            )[0]["claims"]
        )
        assert "`C2` 待核查：断言会中断整个进程" in exported, "导出没读 `heading`"
        assert "待核查 —— " not in exported

    def test_a_narrowed_claim_says_so_in_the_label_not_only_in_the_sentence(self):
        """范围不足的负断言：**状态词**就得是「已检查范围内未发现」而不是「待核查」。

        渲染点把状态词印在前面（`heading` 就是这么拼的），所以这一档必须落进
        `status_label` —— 落在 `display` 里的话，行首那个词与句子里的说法会互相打架。
        """
        reply = _reply(
            _verdict(
                "F1",
                _claim_reply(
                    "C1",
                    "verified",
                    scope='{"files_checked": 120, "files_total": 1343}',
                    complete=False,
                ),
            )
        )
        row = reduce_findings(
            [_anomaly(Claim(claim_id="C1", kind="negative_scope",
                            statement="整个项目没有迁移兼容读取"))],
            verdicts=parse_verdicts(reply),
        ).rows[0]
        review = row.claim_reviews[0]

        assert review.status == CLAIM_UNVERIFIED, "范围不足的负断言不许算已证实"
        assert review.status_label == "已检查范围内未发现"
        assert review.heading == "已检查范围内未发现：整个项目没有迁移兼容读取"
        assert review.display == review.heading, "它本来就是标题安全的那一句（带前缀）"


class TestTheClaimVerdictIsPersisted:
    def test_the_row_keeps_the_claims_and_the_basis(self):
        """落库那一行要能把断言读回来（下一轮基线、导出、面板都读它）。"""
        from app import app as flask_app
        from app import create_tables, db
        from models.ai_analysis import AiAnalysisAnomaly
        from models.ai_analysis.analysis_run import AiAnalysisRun
        from models.project import Project

        payload_row = _reviewed_row().as_dict()
        with flask_app.app_context():
            create_tables()
            project = Project(code=f"P{uuid.uuid4().hex[:8]}", name="断言落库")
            db.session.add(project)
            db.session.flush()
            run = AiAnalysisRun(
                project_id=project.id,
                target_type="weekly",
                target_key=f"g-{uuid.uuid4().hex[:8]}",
                status="succeeded",
            )
            db.session.add(run)
            db.session.flush()

            row = AiAnalysisAnomaly(
                run_id=run.id,
                project_id=project.id,
                fingerprint=payload_row["fingerprint"],
                title=payload_row["title"],
                severity=payload_row["severity"],
                confidence=payload_row["confidence"],
                claims=_json_dumps(payload_row["claims"]),
            )
            db.session.add(row)
            db.session.flush()

            out = row.to_dict()
            assert [one["claim_id"] for one in out["claims"]] == ["C1", "C2"]
            assert out["claims"][1]["status_label"] == "待核查"
            assert out["claims"][1]["display"] == "待核查：断言会中断整个进程"
            assert out["claims"][0]["basis_label"] == "独立取证"
            db.session.rollback()

    def test_a_broken_claims_blob_does_not_break_the_read(self):
        """读路径不许因为一条存坏的字段 500（与 `evidence` 同一条兜底）。"""
        from app import app as flask_app
        from app import create_tables, db
        from models.ai_analysis import AiAnalysisAnomaly
        from models.ai_analysis.analysis_run import AiAnalysisRun
        from models.project import Project

        with flask_app.app_context():
            create_tables()
            project = Project(code=f"P{uuid.uuid4().hex[:8]}", name="坏断言")
            db.session.add(project)
            db.session.flush()
            run = AiAnalysisRun(
                project_id=project.id,
                target_type="weekly",
                target_key=f"g-{uuid.uuid4().hex[:8]}",
                status="succeeded",
            )
            db.session.add(run)
            db.session.flush()
            row = AiAnalysisAnomaly(
                run_id=run.id, project_id=project.id, title="坏数据",
                severity="high", confidence="high", claims="{不是 json",
            )
            db.session.add(row)
            db.session.flush()
            assert row.to_dict()["claims"] == []
            db.session.rollback()


def _json_dumps(value) -> str:
    import json as _json

    return _json.dumps(value, ensure_ascii=False)


class TestTheVerifyRoundIsNotCappedLikeANormalRound:
    """对账轮的依据**不是「支撑结论的几个坐标」，是它核过的清单**（实测 run 58）。

    常规轮的 `max_evidence=3` 是防注水；照搬到对账轮上，削掉的恰恰是「这一处我也去看过」
    —— 报告里那句「已核」于是没有对应的记录可回看。所以对账轮单独放宽，**其余门槛
    （严重度 / 条数上限 / 近似去重）一个都不动**：同一批结论在两轮里必须按同一套标准过筛。
    """
    def test_only_the_evidence_cap_is_loosened(self):
        import ast
        import io as _io

        from services.ai.rules import DEFAULT_MAX_EVIDENCE, RuleThresholds
        from services.ai.subagent import _run_verify  # noqa: F401 —— 存在性
        from services.ai.subagent_tasks import VERIFY_MAX_EVIDENCE

        assert VERIFY_MAX_EVIDENCE > DEFAULT_MAX_EVIDENCE

        source = _io.open(_run_verify.__code__.co_filename, encoding="utf-8").read()
        tree = ast.parse(source)
        node = next(
            item for item in ast.walk(tree)
            if isinstance(item, ast.FunctionDef) and item.name == "_run_verify"
        )
        body = ast.get_source_segment(source, node)

        assert "max_evidence=VERIFY_MAX_EVIDENCE" in body
        assert "replace(thresholds, max_evidence=VERIFY_MAX_EVIDENCE)" in body, (
            "必须只替换这一项；整份换掉会把常规轮的门槛一起改掉"
        )
        # 其余门槛一项都没在这里被改写。
        for field in ("min_severity", "min_confidence", "similarity_threshold", "max_anomalies"):
            assert f"{field}=" not in body, f"{field} 不该在对账轮被另设一个值"

        # 而且它真的生效：同一批 5 条依据，常规轮削成 3 条、对账轮的阈值留下 5 条。
        from services.ai.protocol import Anomaly
        from services.ai.rules import normalize_anomalies

        def _one(thresholds):
            return normalize_anomalies(
                [
                    Anomaly(
                        title="t", category="c", severity="high", confidence="high",
                        evidence=tuple(f"e{i}" for i in range(5)),
                    )
                ],
                thresholds=thresholds,
            )

        normal = _one(RuleThresholds())
        verify = _one(RuleThresholds(max_evidence=VERIFY_MAX_EVIDENCE))

        assert len(normal.anomalies[0].evidence) == DEFAULT_MAX_EVIDENCE
        assert len(verify.anomalies[0].evidence) == 5, "对账轮的依据不该被削"
        assert any("仅保留前" in item.reason for item in normal.dropped)
        assert not [item for item in verify.dropped if "仅保留前" in item.reason]
