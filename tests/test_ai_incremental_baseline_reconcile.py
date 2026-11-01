from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from models.ai_analysis.anomaly import AiAnalysisIgnoredFingerprint
from services.ai.anomaly_disposition import persist_suppressed_previous, set_disposition
from services.ai.baseline import classify, digest_fingerprints, suppressed_fingerprints
from services.ai.baseline_closures import (
    DECLARED_FIXED,
    DECLARED_STANDING,
    BaselineClosure,
)
from services.ai.baseline_source import (
    baseline_digest,
    baseline_findings,
    previous_anomaly_rows,
)
from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome
from services.ai.incremental_baseline import reconcile_result
from services.ai.protocol import AnalysisPayload, Anomaly, Claim, DimensionReview
from services.ai.result_payload import result_payload


def _old(fingerprint, path, *, disposition="pending", severity="high"):
    return {
        "fingerprint": fingerprint,
        "title": f"old-{fingerprint}",
        "category": "code_logic",
        "severity": severity,
        "confidence": "high",
        "evidence": '["old evidence"]',
        "commit_ref": "a" * 40,
        "file_path": path,
        "impact": "old impact",
        "suggestion": "old suggestion",
        "disposition": disposition,
    }


def _current(fingerprint, path):
    return {
        "fingerprint": fingerprint,
        "title": f"new-{fingerprint}",
        "category": "code_logic",
        "severity": "high",
        "confidence": "very_high",
        "evidence": ["new evidence"],
        "commit_ref": "b" * 40,
        "file_path": path,
        "impact": "new impact",
        "suggestion": "new suggestion",
    }


def test_unchanged_in_scope_critical_remains_a_risk_after_an_unrelated_delta():
    old = _old("table-critical", "tables/a.xlsx", severity="critical")
    old["out_of_scope"] = False
    current = {
        "report_markdown": "", "anomalies": [], "final_findings": [],
        "risk_level": "low", "risk_reasons": [],
    }

    merged = reconcile_result(
        current, [old], changed_paths={"tables/b.xlsx"},
        previous_run_id=42, allowed_paths=None,
    )

    assert merged["anomalies"][0]["out_of_scope"] is False
    assert merged["risk_level"] == "high"


def test_historical_background_critical_stays_background_on_an_unrelated_delta():
    old = _old("background-critical", "code/unrelated.lua", severity="critical")
    old["out_of_scope"] = True
    current = {
        "report_markdown": "", "anomalies": [], "final_findings": [],
        "risk_level": "low", "risk_reasons": [],
    }

    merged = reconcile_result(
        current, [old], changed_paths={"tables/b.xlsx"},
        previous_run_id=42, allowed_paths=None,
    )

    assert merged["anomalies"][0]["out_of_scope"] is True
    assert merged["risk_level"] == "low"


def test_incremental_result_carries_forward_and_marks_changed_history_for_recheck():
    result = {
        "report_markdown": "# Current report\n",
        "anomalies": [_current("same", "changed.lua"), _current("fresh", "fresh.lua")],
        "final_findings": [
            {"fingerprint": "same", "active": True},
            {"fingerprint": "fresh", "active": True},
        ],
        "risk_level": "mid_high",
        "risk_reasons": ["current"],
    }
    previous = [
        _old("untouched", "stable.lua", disposition="confirmed", severity="critical"),
        _old("changed-missing", "changed-too.lua"),
        _old("same", "changed.lua"),
        _old("ignored", "ignored.lua", disposition="ignored"),
    ]

    merged = reconcile_result(
        result,
        previous,
        changed_paths={"changed.lua", "changed-too.lua"},
        previous_run_id=22,
    )

    by_id = {item["fingerprint"]: item for item in merged["anomalies"]}
    assert set(by_id) == {"fresh", "same", "untouched", "changed-missing"}
    assert by_id["same"]["baseline_state"] == "reconfirmed"
    assert by_id["untouched"]["baseline_state"] == "carried_forward"
    assert by_id["untouched"]["disposition"] == "confirmed"
    assert by_id["changed-missing"]["baseline_state"] == "needs_recheck"
    assert by_id["changed-missing"]["disposition"] == "pending"
    # WP28：`untouched` 是**继承来的背景发现**（文件不在本轮 changed 里、也没有指向
    # changed 的断言）。它照旧列在清单里，但不许把等级抬到 high —— 审查复现的正是
    # 「上一轮的背景发现经继承后变成正式风险」。
    assert merged["risk_level"] == "mid_high"
    assert merged["baseline_reconciliation"] == {
        "previous_run_id": 22,
        "previous_total": 4,
        "reconfirmed": 1,
        "carried_forward": 1,
        "needs_recheck": 1,
        # 这一轮没有任何收口声明 —— 三个新键都是 0，行为与这条通道存在之前逐字相同。
        "declared_standing": 0,
        "declared_fixed": 0,
        "declared_overturned": 0,
        "declarations_ignored": 0,
        "suppressed": 1,
        # `untouched`（继承的背景发现 critical）+ `fresh`（本轮报的、不在 changed 里也
        # 没有关联断言）—— 两条被单列，都没有参与等级判定。
        "out_of_scope": 2,
    }
    assert "历史结论延续（平台）" in merged["report_markdown"]
    assert "不能因为模型没有逐条回应就视为已修复" in merged["report_markdown"]
    assert len([row for row in merged["final_findings"] if row.get("active")]) == 4


def test_the_section_is_a_count_not_a_list():
    """这一节只报数（2026-09-25 产品决定，用户读 run 70 导出件后）。

    2026-09-24（run 63）它曾是**确定性清单**（「本节为准」），因为模型自己在正文里写的
    「历史结论状态」与它并排、读者看不出信哪份。2026-09-25 起正文的「风险评估」承担了
    「当前仍成立的全部问题」，于是这一节再当清单就是同一批标题的第二份 —— 正是 run 63
    那个毛病换个方向再来一遍。它当时 **682 字 / 13 行**，其中 8 行是逐条清单。
    """
    result = {
        "report_markdown": "# Current report\n",
        "anomalies": [],
        "final_findings": [],
    }
    previous = [
        _old("untouched", "stable.lua", severity="critical"),
        _old("also-untouched", "stable2.lua", severity="high"),
        _old("changed-missing", "changed.lua"),
    ]

    merged = reconcile_result(
        result, previous, changed_paths={"changed.lua"}, previous_run_id=22
    )
    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]

    assert "共 3 条" in section, "开头没有给出这一节的规模"
    assert "2 条仍成立" in section and "1 条本轮无结论" in section, section
    # **这一组不许叫「需要重新确认」**：那是提问期的说法（给模型看的那份基线用），
    # 写进报告就变成平台替这几条下结论 —— 而它知道的只有「文件改了、本轮结论里没它」。
    # 实测 run 73 撞过车：模型在正文里写了「已修复」，本节同时写着「需要重新确认」，
    # 同一份报告对同一批条目给出两种说法。
    assert "需要重新确认" not in section, (
        "这一组的名字又回到「需要重新确认」了 —— 它读起来像平台的结论，会与正文撞车"
    )
    # 一份只报数的节必须说清它不是全量清单，否则读者会以为「没列出来 = 没有了」；
    # 另一半是这一节存在的理由本身。
    assert "逐条清单以正文的「风险评估」为准" in section, (
        "没有说清它与正文那一节的分工 —— 两份并排时读者不知道信哪份"
    )
    assert "不能因为模型没有逐条回应就视为已修复" in section, (
        "「没提到 = 已修好」正是这一节要防的误读"
    )
    assert "只有结构化反证才能关闭" not in section, (
        "平台没有任何结构化通道能让模型关闭一条旧结论 —— 这句话承诺了一个不存在的东西"
    )
    # **一条标题都不再列**（含「需要重新确认」那一类）：条目本身在异常面板与导出附录里
    # 都看得到，认得出的东西不在这里再抄一遍。
    for title in ("old-untouched", "old-also-untouched", "old-changed-missing"):
        assert title not in section, f"逐条清单又回来了：{title}"
    assert "### " not in section, "这一节不该再有二级小节"


def test_reconcile_is_a_noop_without_a_previous_baseline():
    result = {"report_markdown": "ok", "anomalies": [], "final_findings": []}
    assert reconcile_result(result, [], changed_paths=set(), previous_run_id=None) == result


def test_title_drift_on_the_same_file_reconfirms_instead_of_duplicating():
    old = _old("old-fingerprint", "config/reward.xlsx")
    old["title"] = "奖励模式 100004–100006 每日上限留空，服务端导出列缺值"
    current = _current("new-fingerprint", "config/reward.xlsx")
    current["title"] = "奖励模式 100004/100005/100006 每日上限留空且为服务端导出列"
    result = {
        "report_markdown": "current",
        "anomalies": [current],
        "final_findings": [{"fingerprint": "new-fingerprint", "active": True}],
    }

    merged = reconcile_result(
        result,
        [old],
        changed_paths={"config/reward.xlsx"},
        previous_run_id=22,
    )

    assert len(merged["anomalies"]) == 1
    assert merged["anomalies"][0]["baseline_state"] == "reconfirmed"
    assert merged["anomalies"][0]["baseline_previous_fingerprint"] == "old-fingerprint"
    assert merged["baseline_reconciliation"]["reconfirmed"] == 1


def _seed_a_previous_run_with_claims():
    """落一条上一轮结论，**带着断言清单**（+ 一条没有断言的，作为对照）。"""
    project = Project(code=f"P{uuid.uuid4().hex[:8]}", name=f"继承{uuid.uuid4().hex[:6]}")
    db.session.add(project)
    db.session.flush()
    run = AiAnalysisRun(
        project_id=project.id,
        target_type="weekly",
        target_key=f"grp-{uuid.uuid4().hex[:8]}",
        status="succeeded",
        scope="incremental",
        conclusion_structured=True,
    )
    db.session.add(run)
    db.session.flush()
    claims = [
        {
            "claim_id": "C1",
            "kind": "fact",
            "statement": "这张表删掉了列「是否移动中击退」",
            "status": "verified",
            "checked_scope": "code/qz_pub/const/BattleConst.lua@5bce80f",
        },
        {
            "claim_id": "C2",
            "kind": "negative_scope",
            "statement": "全项目没有任何地方还在读这一列",
            "status": "unverified",
            "checked_scope": "只扫了本批 120 个文件（窗口 1403 个）",
        },
    ]
    with_claims = AiAnalysisAnomaly(
        run_id=run.id,
        project_id=project.id,
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        title="【战斗配置】爆炸表整列删除「是否移动中击退」，读取方是否同步待确认（另有 1 条断言待核查）",
        severity="high",
        category="config_value",
        file_path="code/qz_pub/const/BattleConst.lua",
        evidence=json.dumps(["diff: -是否移动中击退"], ensure_ascii=False),
        claims=json.dumps(claims, ensure_ascii=False),
    )
    without_claims = AiAnalysisAnomaly(
        run_id=run.id,
        project_id=project.id,
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        title="【配表】另一条没有断言的结论",
        severity="high",
        category="config_value",
        file_path="config/other.xlsx",
        evidence=json.dumps(["diff: x"], ensure_ascii=False),
        claims="[]",
    )
    db.session.add_all([with_claims, without_claims])
    db.session.commit()
    return run, with_claims, without_claims


def test_a_carried_forward_row_keeps_its_claims():
    """继承项必须把**断言清单**带回来（真机实测，2026-09-24）。

    实测：run 62（增量）的 12 条继承项 `claims` 全是 `[]`，其中 3 条在上一轮（run 61）
    明明带着 3264 / 2210 / 3927 字节的断言。根因不在 `incremental_baseline` —— 它照
    `row.get("claims")` 读，键不在就回空数组；根因在**喂给它的那份行形状**：
    `ai_analysis_service` 调用处手抄的字典字面量少了 `claims` 这个键。

    所以这条用例从**真入口**走一遍（库里的行 → `previous_anomaly_rows` → 合并），
    而不是自己手写那份字典 —— 手写的那份键当然齐，正好会把这个缺陷放过去。
    """
    with flask_app.app_context():
        create_tables()
        run, with_claims, without_claims = _seed_a_previous_run_with_claims()

        rows = previous_anomaly_rows(run.id)

        assert len(rows) == 2
        assert all("claims" in row for row in rows), (
            "行形状里没有 claims 这个键 —— `_historical_anomaly` 的 row.get('claims') "
            "只会回空数组，继承项的断言清单静默消失"
        )

        merged = reconcile_result(
            {"report_markdown": "本轮", "anomalies": [], "final_findings": []},
            rows,
            changed_paths=set(),
            previous_run_id=run.id,
        )
        carried = {row["fingerprint"]: row for row in merged["anomalies"]}
        kept = carried[with_claims.fingerprint]
        assert kept["baseline_state"] == "carried_forward"
        assert [c["claim_id"] for c in kept["claims"]] == ["C1", "C2"], (
            f"继承项带的断言是 {kept['claims']!r}，上一轮存的那两条没带回来"
        )
        assert carried[without_claims.fingerprint]["claims"] == [], (
            "上一轮本来就没有断言的结论，这一轮凭空多出断言"
        )


# ==========================================================================
# 收口声明（`baseline_closures`）：模型说「这一条修好了」，平台收下这句话
# ==========================================================================
#
# 为什么要这条通道：`reconcile_result` 的硬规则是「模型没有重复输出 ≠ 已修复」，
# 而它**没有另一半** —— 平台收不到「已经修好了」这句话。实测 run 73 → run 74：
# 上一轮的模型在正文里写了「已修复」，那是一段文字、没有任何结构；于是下一轮做
# 基线时那条照样当在挂条目喂回去，报告把它写成「上次遗留，仍成立」。同一个事实
# 在两轮报告里翻转了一次，读者无从判断哪次是对的。


def _closure(fingerprint, *, status=DECLARED_FIXED, reason="兜底常量加回来了，服务端又校验了一次上限"):
    return {"fingerprint": fingerprint, "status": status, "reason": reason}


def test_a_declared_fix_leaves_the_ledger_but_stays_on_the_record():
    """收下的定义是**从在挂清单里挪出去**，而不是删掉。

    `anomalies` 是「仍然成立的问题」那一列（落库、面板、下一轮基线读的都是它），
    所以声明生效的**唯一**动作就是在它里面不出现；与此同时条目要在审计轨迹里带着
    模型的理由原文留着 —— 平台没有独立复核过，「谁说的、凭什么」必须看得到。
    """
    declared = "aaaa1111bbbb2222"
    result = {
        "report_markdown": "# Current report\n",
        "anomalies": [],
        "final_findings": [],
        "baseline_updates": [_closure(declared)],
    }
    previous = [_old(declared, "code/team.lua"), _old("cccc3333dddd4444", "code/other.lua")]

    merged = reconcile_result(result, previous, changed_paths=set(), previous_run_id=22)

    assert [item["fingerprint"] for item in merged["anomalies"]] == ["cccc3333dddd4444"], (
        "声明收口的那条还留在在挂清单里 —— 下一轮基线读的就是这一列，等于声明没生效"
    )
    assert merged["baseline_reconciliation"]["declared_fixed"] == 1
    assert merged["baseline_reconciliation"]["carried_forward"] == 1
    assert merged["baseline_reconciliation"]["declarations_ignored"] == 0

    trail = {row["fingerprint"]: row for row in merged["retracted_findings"]}
    row = trail[declared]
    assert row["active"] is False
    assert row["reason"] == _closure(declared)["reason"], "模型给的理由没有原样留下"
    assert row["baseline_state"] == "declared_fixed"
    # 同一个事实只写一遍：结论载荷里那一行与审计轨迹里的是同一行（面板与导出读它们）。
    assert {r["fingerprint"]: r for r in merged["final_findings"]}[declared] == row

    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]
    assert "1 条本轮声明已修复" in section, section
    # 措辞里必须有「声明」二字：平台没有独立复核过它，写成「已修复 N 条」就是把模型的
    # 话当成平台的结论 —— 这条通道最危险的一种读法。
    assert "条已修复" not in section, f"报告里出现了不带「声明」的「已修复」：{section!r}"


def test_a_declaration_for_a_fingerprint_that_is_not_in_the_ledger_does_nothing():
    """指纹对不上清单的声明**一条都不生效**，但必须记账、必须写进报告。

    「说了等于没说」是这条通道最坏的失效形态：模型以为它关掉了一条，平台什么都没做，
    下一轮那条又出现在清单里 —— 双方都以为对方处理了。所以对不上的那些要报出来，
    还要指路（指纹抄错就重抄一遍）。
    """
    result = {
        "report_markdown": "# Current report\n",
        "anomalies": [],
        "final_findings": [],
        "baseline_updates": [_closure("9999999999999999")],
    }
    previous = [_old("aaaa1111bbbb2222", "code/changed.lua")]

    merged = reconcile_result(
        result, previous, changed_paths={"code/changed.lua"}, previous_run_id=22
    )

    assert [item["fingerprint"] for item in merged["anomalies"]] == ["aaaa1111bbbb2222"], (
        "对不上的声明把一条在挂结论关掉了 —— 它凭什么关的是**这一条**？"
    )
    assert merged["anomalies"][0]["baseline_state"] == "needs_recheck"
    assert merged["baseline_reconciliation"]["declarations_ignored"] == 1
    assert merged["retracted_findings"] == []

    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]
    assert "1 条收口声明**没有生效**" in section, section


def test_a_declaration_does_not_close_a_finding_the_same_round_still_reports():
    """同一条既被声明收口、又被本轮重新报成结论时，**以重新报的为准**（保守方向）。

    两种可能：模型自己前后矛盾（正文里说修好了、清单里又留着它），或它的指纹指的是
    另一条。两种情况下「关掉」都是错的那一边，唯一不会误伤的判据是「本轮还在报 =
    还在挂」。代价是这次声明没生效 —— 那一笔记在 `declarations_ignored` 里。
    """
    fingerprint = "aaaa1111bbbb2222"
    result = {
        "report_markdown": "# Current report\n",
        "anomalies": [_current(fingerprint, "code/team.lua")],
        "final_findings": [{"fingerprint": fingerprint, "active": True}],
        "baseline_updates": [_closure(fingerprint)],
    }

    merged = reconcile_result(
        result, [_old(fingerprint, "code/team.lua")], changed_paths=set(), previous_run_id=22
    )

    assert merged["anomalies"][0]["baseline_state"] == "reconfirmed"
    assert merged["baseline_reconciliation"]["declared_fixed"] == 0
    assert merged["baseline_reconciliation"]["declarations_ignored"] == 1
    assert merged["retracted_findings"] == []


def test_the_section_says_out_loud_when_no_close_out_arrived():
    """清单里还有没定论的条目、而这一轮**一条结构化收口都没收到** —— 平台要把这件事说出来。

    为什么非说不可（2026-09-25 真机，两次复现）：模型会在正文里写「某几条已修复」
    （run 76 甚至用上了这条通道自己的词：「声明已由本次提交 fc3f45b5d402 修复」），
    而那句话平台读不到、也不认。读者于是看到两份说法：正文说修好了，本节说「仍成立 /
    本轮无结论」—— 两份都没错，缺的是中间那一格事实：**平台收到了什么**。

    平台在这里不判谁对（它没有独立复核过那几条），只陈述自己收到了什么。
    """
    merged = reconcile_result(
        {"report_markdown": "本轮", "anomalies": [], "final_findings": []},
        [_old("aaaa1111bbbb2222", "code/stable.lua")],
        changed_paths=set(),
        previous_run_id=22,
    )
    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]

    assert "没有收到 `baseline_updates`" in section, section
    assert "在平台的账里它们仍在挂" in section


def test_the_section_stops_saying_it_once_a_close_out_really_arrived():
    """收口收到之后这句就不许再出现 —— 它陈述的是「平台收到了什么」，收到就不是那样了。

    这一条同时防住「把这句话写成常驻口号」：它是**状态句**，不是免责声明。所以清单里
    必须**同时**有一条已收口、另一条还没定论 —— 只放一条已收口的话，
    「只要还有没定论的就说」这个改坏法也能让这条用例绿（判据落在「有没有声明」还是
    「有没有没定论的条目」上，就分不出来了）。
    """
    declared = "aaaa1111bbbb2222"
    merged = reconcile_result(
        {
            "report_markdown": "本轮",
            "anomalies": [],
            "final_findings": [],
            "baseline_updates": [_closure(declared)],
        },
        [_old(declared, "code/stable.lua"), _old("cccc3333dddd4444", "code/other.lua")],
        changed_paths=set(),
        previous_run_id=22,
    )
    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]

    assert merged["baseline_reconciliation"]["carried_forward"] == 1, "另一条没在清单里"
    assert "没有收到 `baseline_updates`" not in section, section
    assert "1 条本轮声明已修复" in section


def test_the_section_stays_quiet_when_everything_was_reconfirmed():
    """清单里的条目本轮**都有定论**时也不必说 —— 那句话是给「还没定论」的条目配的。"""
    fingerprint = "aaaa1111bbbb2222"
    merged = reconcile_result(
        {
            "report_markdown": "本轮",
            "anomalies": [_current(fingerprint, "code/stable.lua")],
            "final_findings": [{"fingerprint": fingerprint, "active": True}],
        },
        [_old(fingerprint, "code/stable.lua")],
        changed_paths=set(),
        previous_run_id=22,
    )
    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]

    assert merged["baseline_reconciliation"]["reconfirmed"] == 1
    assert "没有收到 `baseline_updates`" not in section, section


def test_saying_standing_keeps_the_item_and_counts_as_an_answer():
    """模型逐条交代里的 `standing`（仍成立）**不关闭任何东西**，但它算「答过了」。

    为什么值得单独一条：这是 2026-09-25 晚把这条通道从「可选的两档」改成「逐条枚举」时
    带出来的第三档（见 `baseline_closures` 的模块 docstring）。它有两个作用，都必须钉住：

    * **清单里那一条照旧挂着**（它只是被确认了，不是被关掉）；
    * 报告那一节据此知道平台**收到过交代**，于是不补那句「没有收到 `baseline_updates`」
      —— 那句话是给「模型一个字都没答」的情形配的。
    """
    fingerprint = "aaaa1111bbbb2222"
    result = {
        "report_markdown": "本轮",
        "anomalies": [],
        "final_findings": [],
        "baseline_updates": [_closure(fingerprint, status=DECLARED_STANDING, reason="")],
    }

    merged = reconcile_result(
        result, [_old(fingerprint, "code/stable.lua")], changed_paths=set(), previous_run_id=22
    )
    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]

    assert [item["fingerprint"] for item in merged["anomalies"]] == [fingerprint], (
        "`standing` 把一条仍成立的结论关掉了 —— 它只表示「这条还在」"
    )
    assert merged["baseline_reconciliation"]["declared_standing"] == 1
    assert merged["baseline_reconciliation"]["carried_forward"] == 1
    assert "没有收到 `baseline_updates`" not in section, (
        "模型逐条答过了，却还在说「没有收到」—— 那句话会被读成模型什么都没说"
    )


def test_a_standing_answer_does_not_cancel_the_stale_evidence_signal():
    """`standing` **不抵消**「相关文件又变了、证据已过期」。

    那一格是**平台的**判断（文件变过、而这条结论的证据来自那次改动之前），模型说一句
    「仍成立」并不能替代「它重看过那个刚变过的文件」—— 后者才是这条状态要防的东西。
    """
    fingerprint = "aaaa1111bbbb2222"
    merged = reconcile_result(
        {
            "report_markdown": "本轮",
            "anomalies": [],
            "final_findings": [],
            "baseline_updates": [_closure(fingerprint, status=DECLARED_STANDING, reason="")],
        },
        [_old(fingerprint, "code/changed.lua")],
        changed_paths={"code/changed.lua"},
        previous_run_id=22,
    )

    assert merged["anomalies"][0]["baseline_state"] == "needs_recheck"


def test_a_declared_fix_does_not_come_back_as_an_in_flight_item():
    """整条链走一遍：声明 → 合并 → **落库** → 下一轮的基线里没有它。

    只断言「合并结果里没有它」是不够的 —— 中间还有一步：结论落库时写的是哪一份。
    `_persist_outcome` 逐条建 `AiAnalysisAnomaly` 行，读的是 `result["anomalies"]`
    （`ai_analysis_service` 里的 `for item in result.get("anomalies") or []`），而
    下一轮的基线读的就是这张表。合并与落库之间接错了来源（比如又去读 `outcome.anomalies`），
    这条通道就是死的 —— 而「报告里写着声明已修复」照样是绿的。
    """
    # 落库那一步只在真入口上有，为了收集期不把整个服务模块拖进来，这里就近导入。
    from services.ai_analysis_service import _persist_outcome

    with flask_app.app_context():
        create_tables()
        previous, fingerprint = _seed_a_previous_run_with_one_finding()
        run = AiAnalysisRun(
            project_id=previous.project_id,
            target_type=previous.target_type,
            target_key=previous.target_key,
            status="succeeded",
            scope="incremental",
            conclusion_structured=True,
        )
        db.session.add(run)
        db.session.commit()

        outcome = EngineOutcome(
            status=STATUS_SUCCEEDED,
            payload=AnalysisPayload(
                status="final",
                dimensions=(DimensionReview(id="config_id", hit=False, note="本批次没改配表"),),
                baseline_closures=(
                    BaselineClosure(
                        fingerprint=fingerprint,
                        status=DECLARED_FIXED,
                        reason="兜底常量加回来了",
                    ),
                ),
            ),
            anomalies=(),
            report_markdown="# 变更理解\n\n本轮没有新发现。\n",
        )
        result = reconcile_result(
            result_payload(outcome, {}),
            previous_anomaly_rows(previous.id),
            changed_paths=set(),
            previous_run_id=previous.id,
        )
        _persist_outcome(run, outcome, result)

        assert AiAnalysisAnomaly.query.filter_by(run_id=run.id).count() == 0, (
            "声明收口的条目还是被写进了异常表 —— 下一轮基线读的就是这张表"
        )
        assert [item.fingerprint for item in baseline_findings("weekly", run.target_key)] == [], (
            "下一轮的基线里还有那条已声明修复的结论 —— 那条声明等于没说"
        )
        payload = json.loads(run.response_payload)
        assert [row["fingerprint"] for row in payload["retracted_findings"]] == [fingerprint]
        assert payload["retracted_findings"][0]["reason"] == "兜底常量加回来了"


def _seed_a_previous_run_with_one_finding(path="code/qz_pub/team.lua"):
    """落一条上一轮结论（带指纹与文件路径），返回 `(运行, 指纹)`。"""
    project = Project(code=f"P{uuid.uuid4().hex[:8]}", name=f"收口{uuid.uuid4().hex[:6]}")
    db.session.add(project)
    db.session.flush()
    run = AiAnalysisRun(
        project_id=project.id,
        target_type="weekly",
        target_key=f"grp-{uuid.uuid4().hex[:8]}",
        status="succeeded",
        scope="incremental",
        conclusion_structured=True,
    )
    db.session.add(run)
    db.session.flush()
    fingerprint = uuid.uuid4().hex[:16]
    db.session.add(
        AiAnalysisAnomaly(
            run_id=run.id,
            project_id=project.id,
            fingerprint=fingerprint,
            title="【战斗】队伍人数上限只在客户端判，服务端不校验",
            severity="high",
            category="code_logic",
            file_path=path,
            evidence=json.dumps(["diff: -if #team.members >= limit then"], ensure_ascii=False),
            claims="[]",
        )
    )
    db.session.commit()
    return run, fingerprint


# ==========================================================================
# 「曾被人工忽略」的条目又回来了：结果里要留着这个标记
# ==========================================================================
#
# 产品审查（2026-11-01）第 5 条的第 2 点（**代码可证**的那一半）：复活判据是**本轮**
# 的 `change.paths`（`baseline.classify`）—— 一条被忽略的结论只要所在文件又变一次，
# 就一定会重新出现在清单里。这是刻意的（「忽略」不许变成「永远看不见」），但从行上看
# 它和一条全新发现一模一样：重新看到它的人不知道上一轮已经有人判定过「这条不用管」。
# 所以合并器要留下 `suppressed_previous`。


def test_a_previously_ignored_item_that_comes_back_is_marked():
    """两条复活路径都要带标记，且**没被忽略过的条目不许带**（否则标记失去区分力）。

    * 平台路径：文件又变了、模型本轮没提 → `needs_recheck` 重新进清单；
    * 模型路径：文件又变了、模型自己又报了一遍 → `reconfirmed`。

    第三种（已忽略**且**文件没再变）照旧被抑制：它连清单都不进，自然也不该被数进
    「曾被忽略又回来了」那一句 —— 否则报告会把它读成「忽略没生效」。
    """
    fingerprint = "aaaa1111bbbb2222"
    rereported = "cccc3333dddd4444"
    never_ignored = "eeee5555ffff6666"
    still_suppressed = "99990000aaaa1111"
    result = {
        "report_markdown": "本轮",
        "anomalies": [_current(rereported, "code/changed.lua")],
        "final_findings": [{"fingerprint": rereported, "active": True}],
    }
    previous = [
        _old(fingerprint, "code/changed.lua", disposition="ignored"),
        _old(rereported, "code/changed.lua", disposition="ignored"),
        _old(never_ignored, "code/changed.lua"),
        _old(still_suppressed, "code/stable.lua", disposition="ignored"),
    ]

    merged = reconcile_result(
        result, previous, changed_paths={"code/changed.lua"}, previous_run_id=22
    )

    by_id = {item["fingerprint"]: item for item in merged["anomalies"]}
    assert by_id[fingerprint]["baseline_state"] == "needs_recheck"
    assert by_id[fingerprint]["suppressed_previous"] is True, (
        "文件又变而被提回清单的条目没有「曾被忽略」标记 —— 它看起来是一条全新发现"
    )
    assert by_id[fingerprint]["disposition"] == "pending", "复活的条目要重新处置，不能沿用 ignored"
    assert by_id[rereported]["baseline_state"] == "reconfirmed"
    assert by_id[rereported]["suppressed_previous"] is True, (
        "模型重新报出来的条目丢了「曾被忽略」标记"
    )
    assert "suppressed_previous" not in by_id[never_ignored], (
        "从没被忽略过的条目也带了标记 —— 这个字段就没有区分力了"
    )
    assert still_suppressed not in by_id, "文件没再变的忽略项又被放回了在挂清单"
    assert merged["baseline_reconciliation"]["suppressed"] == 1

    # 结论载荷是面板与导出读的那一列：同一个事实必须也在那里。
    finals = {
        row["fingerprint"]: row for row in merged["final_findings"] if row.get("active")
    }
    assert finals[fingerprint]["suppressed_previous"] is True
    assert finals[rereported]["suppressed_previous"] is True
    assert "suppressed_previous" not in finals[never_ignored]

    # 报告那一节只报数，但这一句必须说：读者最容易把它读成「平台的忽略没生效」。
    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]
    assert "2 条**曾被人工忽略**" in section, section
    assert "「忽略」不是删除" in section, section


def test_the_ignored_marker_stays_silent_when_nothing_resurfaced():
    """没有复活条目时一个字都不许多 —— 那一句是**状态句**，不是常驻免责声明。"""
    merged = reconcile_result(
        {
            "report_markdown": "本轮",
            "anomalies": [_current("aaaa1111bbbb2222", "code/stable.lua")],
            "final_findings": [{"fingerprint": "aaaa1111bbbb2222", "active": True}],
        },
        [
            _old("aaaa1111bbbb2222", "code/stable.lua"),
            _old("cccc3333dddd4444", "code/stable.lua", disposition="ignored"),
        ],
        changed_paths=set(),
        previous_run_id=22,
    )

    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]
    assert "曾被人工忽略" not in section, section
    assert merged["anomalies"][0].get("suppressed_previous") is None
    assert merged["baseline_reconciliation"]["suppressed"] == 1


# ==========================================================================
# 三、人工忽略跨「结构化 0 行」的持久化（P0 真漏口，2026-11）
#
# 病灶与修法写在 `services/ai/baseline_source.py` 的模块抬头。这一组从**真入口**走：
# 库里的处置 → 台账 → `previous_anomaly_rows` / `baseline_findings` → 合并 → 落库。
# 手写上一轮行形状的写法证明不了台账真的被读进来了。
# ==========================================================================


def _ledger_context():
    """一个项目 + 一个周版本分组键（测试库会话级共用，键必须唯一）。"""
    project = Project(code=f"P{uuid.uuid4().hex[:8]}", name=f"台账{uuid.uuid4().hex[:6]}")
    db.session.add(project)
    db.session.flush()
    return project, f"grp-{uuid.uuid4().hex[:8]}"


def _run_at(project_id, target_key, when):
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_key=target_key,
        status="succeeded",
        scope="incremental",
        conclusion_structured=True,
        created_at=when,
    )
    db.session.add(run)
    db.session.flush()
    return run


def _seed_an_ignored_finding(run, *, title="【道具】ID 被删除但生成文件仍在"):
    """在 `run` 上落一条结论并**人工忽略**它（处置走统一写路径 → 台账一起落）。"""
    anomaly = AiAnalysisAnomaly(
        run_id=run.id,
        project_id=run.project_id,
        fingerprint=uuid.uuid4().hex[:16],
        title=title,
        category="config_id",
        severity="high",
        confidence="high",
        evidence=json.dumps(["config/道具表.xlsx 删除了 ID 1001"], ensure_ascii=False),
        commit_ref="a" * 40,
        file_path="config/道具表.xlsx",
        impact="老存档引用的道具失效",
        suggestion="确认是否有意下线",
    )
    db.session.add(anomaly)
    db.session.flush()
    set_disposition(anomaly, disposition="ignored", username="张三")
    db.session.commit()
    return anomaly


def _reported_anomaly(*, path="config/道具表.xlsx"):
    """模型报出来的那一条（指纹由引擎算法算 —— 台账按指纹匹配，不能手编）。"""
    return Anomaly(
        title="【道具】ID 被删除但生成文件仍在",
        category="config_id",
        severity="high",
        confidence="high",
        evidence=(f"{path} 删除了 ID 1001",),
        commit="b" * 40,
        file_path=path,
        impact="老存档引用的道具失效",
        suggestion="确认是否有意下线",
    )


def _seed_an_ignored_finding(run):
    """在 `run` 上落一条结论并**人工忽略**它（处置走统一写路径 → 台账一起落）。

    指纹用**引擎那一份算法**从 `_reported_anomaly()` 算出来：台账是按指纹匹配的，
    行上的指纹与下一轮模型算出来的对不上，整条链路会静默失效 —— 而这条用例会以
    「随便给一个指纹」的写法把这个真 bug 糊过去。
    """
    from services.ai.rules import anomaly_fingerprint

    reported = _reported_anomaly()
    anomaly = AiAnalysisAnomaly(
        run_id=run.id,
        project_id=run.project_id,
        fingerprint=anomaly_fingerprint(reported),
        title=reported.title,
        category=reported.category,
        severity=reported.severity,
        confidence=reported.confidence,
        evidence=json.dumps(list(reported.evidence), ensure_ascii=False),
        commit_ref=reported.commit,
        file_path=reported.file_path,
        impact=reported.impact,
        suggestion=reported.suggestion,
    )
    db.session.add(anomaly)
    db.session.flush()
    set_disposition(anomaly, disposition="ignored", username="张三")
    db.session.commit()
    return anomaly


def _drop_ledger_case(target_key, run_ids):
    AiAnalysisIgnoredFingerprint.query.filter_by(target_key=target_key).delete()
    for run_id in run_ids:
        AiAnalysisAnomaly.query.filter_by(run_id=run_id).delete()
        AiAnalysisRun.query.filter_by(id=run_id).delete()
    db.session.commit()


def test_a_zero_row_run_still_feeds_the_ignored_ledger_into_reconciliation():
    """**P0 的形状**：忽略 → 结构化 0 行 → 下一轮同指纹再报 → 仍被抑制。

    0 行那一次是基线；它的行是空的，但台账里那条「不用再报」还在。对账器拿到台账补
    进来的行之后，按「已忽略 + 文件没再变」把它挡掉（`suppressed` 计数），而不是让它
    以 `pending` 身份重新出现在清单里。
    """
    with flask_app.app_context():
        create_tables()
        project, key = _ledger_context()
        now = datetime.now(timezone.utc)
        first = _run_at(project.id, key, now - timedelta(hours=2))
        ignored = _seed_an_ignored_finding(first)
        fingerprint = ignored.fingerprint
        zero_row = _run_at(project.id, key, now - timedelta(hours=1))
        try:
            assert AiAnalysisAnomaly.query.filter_by(run_id=zero_row.id).count() == 0, "前提不成立"

            rows = previous_anomaly_rows(zero_row.id)
            assert [row["fingerprint"] for row in rows] == [fingerprint], (
                "0 行那次没有把台账补进对账输入 —— 对账器会直接早退，"
                "「曾被忽略」这个事实在这一轮彻底消失"
            )
            assert rows[0]["disposition"] == "ignored"
            # 形状要**完整**（不是从台账拼出来的残缺行）：对账器要把历史等级、证据、
            # 断言一路带进本轮结果，缺键的表现是面板上少一段内容而不是报错。
            assert rows[0]["evidence"] == ["config/道具表.xlsx 删除了 ID 1001"]
            assert rows[0]["title"] == ignored.title

            findings = baseline_findings("weekly", key)
            suppressed = suppressed_fingerprints(classify(findings))
            assert suppressed == frozenset({fingerprint})

            outcome = EngineOutcome(
                status=STATUS_SUCCEEDED,
                anomalies=(_reported_anomaly(),),
                report_markdown="# 本轮\n",
            )
            result = result_payload(outcome, {}, suppressed=suppressed)
            assert result["anomalies"] == [], "被忽略的条目又被当成新结论交付了"

            merged = reconcile_result(
                result,
                rows,
                changed_paths=set(),
                previous_run_id=zero_row.id,
            )
            assert merged["anomalies"] == []
            assert merged["baseline_reconciliation"]["suppressed"] == 1
            # 这一节以前会印「共 0 条：。」（读起来像坏了）：被抑制的条目不进任何一组，
            # 于是「它们去哪了」必须单独说一句。
            section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]
            assert "共 0 条" in section, section
            assert "人工已忽略" in section and "放回清单" in section, section
        finally:
            _drop_ledger_case(key, [first.id, zero_row.id])


def test_the_revival_after_a_zero_row_run_is_marked_and_persisted():
    """0 行缺口之后文件又变了 → 复活条目要带「曾被忽略」，而且要**落进库**。

    只把标记写在这一次的 `result` 字典里是不够的：面板、导出、下一轮基线读的都是
    `ai_analysis_anomaly.suppressed_previous` 那一列（刷新一次页面就消失的字段等于没有）。
    `_persist_outcome` 不在本工作包的文件所有权内，所以落库走
    `anomaly_disposition.persist_suppressed_previous` 这个辅助（一行接入，理由见它的
    docstring）；这里从真入口走一遍，证明那一列真的落得下去。
    """
    from services.ai_analysis_service import _persist_outcome

    with flask_app.app_context():
        create_tables()
        project, key = _ledger_context()
        now = datetime.now(timezone.utc)
        first = _run_at(project.id, key, now - timedelta(hours=2))
        ignored = _seed_an_ignored_finding(first)
        fingerprint = ignored.fingerprint
        zero_row = _run_at(project.id, key, now - timedelta(hours=1))
        current = _run_at(project.id, key, now)
        try:
            rows = previous_anomaly_rows(zero_row.id)
            assert [row["fingerprint"] for row in rows] == [fingerprint], "前提不成立"

            outcome = EngineOutcome(
                status=STATUS_SUCCEEDED,
                anomalies=(_reported_anomaly(),),
                report_markdown="# 本轮\n",
            )
            # 文件变了 → 抑制不再命中（`baseline.classify` 的复活判据），这一条会进清单。
            change_paths = {"config/道具表.xlsx"}
            suppressed = suppressed_fingerprints(
                classify(baseline_findings("weekly", key), changed_paths=change_paths)
            )
            assert suppressed == frozenset(), "前提不成立：文件变了还抑制，复活无从谈起"
            result = result_payload(outcome, {}, suppressed=suppressed)
            assert [row["fingerprint"] for row in result["anomalies"]] == [fingerprint]

            merged = reconcile_result(
                result,
                rows,
                changed_paths=change_paths,
                previous_run_id=zero_row.id,
            )
            revivals = [item for item in merged["anomalies"] if item["fingerprint"] == fingerprint]
            assert len(revivals) == 1
            assert revivals[0]["suppressed_previous"] is True, (
                "跨 0 行缺口复活的条目没有「曾被忽略」标记 —— 它看起来是一条全新发现"
            )
            assert revivals[0]["disposition"] == "pending", (
                "复活的条目沿用了 ignored —— 下一轮它又会被当成「人工已忽略」静默抹掉"
            )

            _persist_outcome(current, outcome, merged)
            # `_persist_outcome` 落库后**已经**调用过 `persist_suppressed_previous`
            # （WP15 接线），所以这里再调一次是幂等的、改 0 行；真正要钉的是下面
            # 读库那一条（列已置 True）。
            assert persist_suppressed_previous(current.id, merged["anomalies"]) == 0

            db.session.expire_all()
            stored = AiAnalysisAnomaly.query.filter_by(
                run_id=current.id, fingerprint=fingerprint
            ).one()
            assert stored.suppressed_previous is True, (
                "标记没有落进库 —— 面板/导出/下一轮基线读的是这一列，不是这一次的返回体"
            )
            assert stored.disposition == "pending"
            assert stored.to_dict()["suppressed_previous"] is True
            # 撤销处置（paging/confirming）不许把「来历」抹掉：那是两件事。
            assert stored.to_dict()["disposition"] == "pending"
        finally:
            _drop_ledger_case(key, [first.id, zero_row.id, current.id])


def test_a_closure_for_a_ledger_only_entry_is_not_counted_as_unmatched():
    """摘要里列出的台账条目，模型对它给交代时**不许**被记成「指纹对不上」。

    这是把台账并进**对账输入**（而不只是摘要）的理由：模型按协议必须对「清单上的每
    一条」给出 `baseline_updates`，而合并器的「认识哪些指纹」来自对账输入。两边不同源
    时，一条台账条目的合法交代会被记账成 `declarations_ignored`，并在报告里写成
    「有收口声明没有生效（指纹对不上本次清单）」—— 那句错误提示本身是假的，读者会去
    找一个不存在的抄写错误。

    **形状是「上一轮还有别的行」**（不是 0 行）：这一档正好证明台账的并入与上一轮报了
    几条无关。
    """
    from services.ai.change_set import ChangeSet
    from services.ai.scope import AnalysisScope

    with flask_app.app_context():
        create_tables()
        project, key = _ledger_context()
        now = datetime.now(timezone.utc)
        first = _run_at(project.id, key, now - timedelta(hours=2))
        ignored = _seed_an_ignored_finding(first)
        fingerprint = ignored.fingerprint
        # 上一轮不是 0 行：另有一条普通结论（它的文件本轮没变，会被 carried_forward）。
        later = _run_at(project.id, key, now - timedelta(hours=1))
        current_row = AiAnalysisAnomaly(
            run_id=later.id,
            project_id=project.id,
            fingerprint=uuid.uuid4().hex[:16],
            title="另一条仍然成立的结论",
            category="code_logic",
            severity="high",
            file_path="code/stable.lua",
        )
        db.session.add(current_row)
        db.session.commit()
        try:
            # 前提：摘要里确实有那条台账条目的指纹（否则模型根本没被告知它）。
            change = ChangeSet(summary="", scope=AnalysisScope(), paths=())
            digest = baseline_digest("weekly", key, change)
            assert fingerprint in digest_fingerprints(digest), digest

            rows = previous_anomaly_rows(later.id)
            assert {row["fingerprint"] for row in rows} == {
                fingerprint,
                current_row.fingerprint,
            }, "台账条目没有并进对账输入 —— 模型对它的交代会被当成「指纹对不上」"

            merged = reconcile_result(
                {
                    "report_markdown": "本轮",
                    "anomalies": [],
                    "final_findings": [],
                    "baseline_updates": [
                        _closure(fingerprint, status=DECLARED_STANDING, reason="")
                    ],
                },
                rows,
                changed_paths=set(),
                previous_run_id=later.id,
            )
            assert merged["baseline_reconciliation"]["declarations_ignored"] == 0, (
                "模型对台账条目的交代被记成了「指纹对不上本次清单」—— 那条提示是假的"
            )
            assert merged["baseline_reconciliation"]["declared_standing"] == 1
        finally:
            _drop_ledger_case(key, [first.id, later.id])


# ==========================================================================
# 五、继承条目的范围标记（WP28）：背景发现不许在下一轮变成正式风险
#
# 案情（审查 P1，已复现）：WP27 把与本次配表差异无关的结论标 `out_of_scope=True`、
# 不计本轮等级；可下一轮增量继承时，`_historical_anomaly` 不带这个键（库里也没有这一
# 列），`:567` 的 critical 升级又对所有继承条目生效 —— 一条范围外 critical 背景发现
# 经继承后标记消失，风险 low→high。
#
# 修法：合并之后按**本轮**的 `allowed_paths` / `changed_paths` / `claims` 重算，再定级。
# 这一节钉住三件事：标记不丢、等级不被背景发现抬高、本轮自己的 in-scope critical 照旧
# 抬等级（不许修过头）。
# ==========================================================================


DELTA_TABLE = "config/[30]道具表_CfgItem.xlsx"
BACKGROUND_CODE = "code/qz_server/chat_service.lua"


def _old_with_claims(fingerprint, path, refs, *, severity="critical"):
    """上一轮落库形状的一行：断言清单存的是 JSON 文本（`AiAnalysisAnomaly.claims`）。

    **故意不带 `out_of_scope` 键**：库里没有这一列，继承项拿到的就是这种行。标记必须
    由本轮重算得出；如果实现只是把上一轮的键抄过来，这条用例就会红。
    """
    return {
        **_old(fingerprint, path, severity=severity),
        "claims": json.dumps(
            [
                {
                    "claim_id": "C1",
                    "kind": "fact",
                    "statement": "这段代码会读到改过的表",
                    "evidence_refs": list(refs),
                }
            ],
            ensure_ascii=False,
        ),
    }


def _empty_result(risk="mid_high"):
    return {
        "report_markdown": "# 本轮\n",
        "anomalies": [],
        "final_findings": [],
        "risk_level": risk,
        "risk_reasons": [],
    }


def test_an_inherited_background_critical_keeps_out_of_scope_and_does_not_raise_the_level():
    """审查复现的形状：claims 只引代码路径 ⇒ 背景发现；继承后标记还在、等级不升高。

    上一轮它就是**范围外**的（与配表 delta 无关），这一轮更不会因为「被继承」而变成
    正式风险。若没有这次重算，`out_of_scope` 在继承行上根本不存在，下面那句 critical
    升级会把它当成正式 critical → `risk_level` 被顶到 high。
    """
    previous = [
        _old_with_claims("bg-critical", BACKGROUND_CODE, [f"{BACKGROUND_CODE}:88"])
    ]

    merged = reconcile_result(
        _empty_result(),
        previous,
        changed_paths={DELTA_TABLE},
        previous_run_id=22,
        allowed_paths=None,
    )

    item = merged["anomalies"][0]
    assert item["baseline_state"] == "carried_forward"
    assert item["out_of_scope"] is True, (
        "继承来的背景发现没有重算标记 —— 下一轮它会以正式结论身份参与定级"
    )
    assert merged["risk_level"] == "mid_high", (
        "继承来的背景发现 critical 把等级抬到了 high（审查复现的 low→high）"
    )
    assert merged["baseline_reconciliation"]["out_of_scope"] == 1
    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]
    assert "另有 1 条为背景发现/关联不足，未计入等级" in section, (
        "被单列的条数没有交代 —— 正文里一条 critical、等级却不是 high，读者会以为报告自相矛盾"
    )


def test_an_inherited_critical_whose_claims_touch_the_delta_still_raises_the_level():
    """防过头：上一轮 critical 的断言命中**本轮**配表 delta ⇒ 有关联，照常抬 high。

    这条同时证明重算不是「把所有继承项一律判成背景发现」—— 那会把真问题一起按下
    去，而它恰好是 WP27 要保住的那一档（代码消费方对改过的表的结论）。
    """
    previous = [_old_with_claims("related-critical", BACKGROUND_CODE, [f"{DELTA_TABLE}:12"])]

    merged = reconcile_result(
        _empty_result(),
        previous,
        changed_paths={DELTA_TABLE},
        previous_run_id=22,
        allowed_paths=None,
    )

    item = merged["anomalies"][0]
    assert item["out_of_scope"] is False, "断言指向本轮改过的表，却被判成背景发现"
    assert merged["risk_level"] == "high", "有可核实关联的继承 critical 没有参与定级"
    assert merged["baseline_reconciliation"]["out_of_scope"] == 0
    section = merged["report_markdown"].split("## 历史结论延续（平台）", 1)[1]
    assert "另有 0 条" not in section, "没有单列条目，却报告了「另有 N 条」"


def test_an_in_scope_critical_this_round_keeps_the_level_high():
    """本轮自己的 in-scope critical 照旧抬 high —— 背景发现不许抬，正常的不许被压。"""
    critical = _current("current-critical", DELTA_TABLE)
    critical["severity"] = "critical"
    result = {
        **_empty_result("high"),
        "anomalies": [critical],
        "final_findings": [{"fingerprint": "current-critical", "active": True}],
    }
    previous = [
        _old_with_claims("bg-critical", BACKGROUND_CODE, [f"{BACKGROUND_CODE}:88"])
    ]

    merged = reconcile_result(
        result,
        previous,
        changed_paths={DELTA_TABLE},
        previous_run_id=22,
        allowed_paths=None,
    )

    by_id = {item["fingerprint"]: item for item in merged["anomalies"]}
    assert by_id["current-critical"]["out_of_scope"] is False
    assert by_id["bg-critical"]["out_of_scope"] is True
    assert merged["risk_level"] == "high", (
        "本轮 in-scope critical 的 high 被背景发现的过滤一起压掉了"
    )
    assert merged["baseline_reconciliation"]["out_of_scope"] == 1


def test_a_current_finding_whose_claims_point_at_the_delta_stays_in_scope():
    """本轮条目的标记**不许被载荷里那份 claims 覆盖**（重算的边界）。

    没有复核轮的运行里，`result_payload._anomaly_entry` 的 `claims` 取自复核行，取不到
    就是空数组；而它的 `out_of_scope` 是用模型原始 `Anomaly.claims` 算的（那份才带着
    `evidence_refs`）。合并器若拿载荷里的 claims 再算一遍，一条「断言指向改过的表」的
    本轮 critical 会在继承合并这一步被误判成背景发现 —— 等级与报告口径一起漂。
    """
    outcome = EngineOutcome(
        status="succeeded",
        anomalies=(
            Anomaly(
                title="消费方没有处理新 ID",
                category="code_logic",
                severity="critical",
                confidence="high",
                evidence=(f"{BACKGROUND_CODE}:3",),
                commit="a" * 40,
                file_path=BACKGROUND_CODE,
                claims=(
                    Claim(
                        claim_id="C1",
                        kind="fact",
                        statement="这段代码会读到改过的表",
                        evidence_refs=(f"{DELTA_TABLE}:12",),
                    ),
                ),
            ),
        ),
        report_markdown="# 本轮\n",
    )
    payload = {
        "mode": "weekly",
        "summary": {},
        "allowed_paths": None,
        "delta_files": [{"file_path": DELTA_TABLE, "latest_commit_id": "a" * 40}],
    }
    result = result_payload(outcome, payload)
    # 前提：载荷里这条**已经是 in-scope**（result_payload 用的是模型原始 claims）。
    assert result["risk_level"] == "high"
    assert result["anomalies"][0]["out_of_scope"] is False
    assert result["anomalies"][0]["claims"] == [], (
        "前提不成立：这条用例要的是「载荷里的 claims 已经丢了证据」的那个形状"
    )

    merged = reconcile_result(
        result,
        [_old("old-background", "code/other.lua")],
        changed_paths={DELTA_TABLE},
        previous_run_id=7,
        allowed_paths=None,
    )

    current = next(
        item for item in merged["anomalies"] if item["title"] == "消费方没有处理新 ID"
    )
    assert current["out_of_scope"] is False, (
        "本轮的有可核实关联结论被合并器拿载荷里的空 claims 重算成了背景发现"
    )
    assert merged["risk_level"] == "high"
    assert merged["baseline_reconciliation"]["out_of_scope"] == 1, (
        "被单列的只有那条继承来的背景发现；本轮 in-scope 的那条被算进去了"
    )


def test_an_explicit_empty_allow_list_marks_inherited_rows_out_of_scope():
    """显式 `allowed_paths=[]`（一条都不许读）对**继承条目**同样生效。

    这一档走的是旧口径：文件不在白名单里就是越界，断言指向 delta 也不能翻案。继承
    条目原先完全绕过这条判据（它只看 `risk_level`），等于旧范围的声明对历史结论失效。
    """
    previous = [
        _old_with_claims("inherited-located", BACKGROUND_CODE, [f"{DELTA_TABLE}:12"])
    ]

    merged = reconcile_result(
        _empty_result(),
        previous,
        changed_paths={DELTA_TABLE},
        previous_run_id=22,
        allowed_paths=[],
    )

    item = merged["anomalies"][0]
    assert item["out_of_scope"] is True, (
        "显式空白名单没有作用到继承条目上 —— 读取范围的声明对历史结论失效"
    )
    assert merged["risk_level"] == "mid_high"
    assert merged["baseline_reconciliation"]["out_of_scope"] == 1

