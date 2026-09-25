from __future__ import annotations

import json
import uuid

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from services.ai.baseline_closures import DECLARED_FIXED, BaselineClosure
from services.ai.baseline_source import baseline_findings, previous_anomaly_rows
from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome
from services.ai.incremental_baseline import reconcile_result
from services.ai.protocol import AnalysisPayload, DimensionReview
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
    assert merged["risk_level"] == "high"
    assert merged["baseline_reconciliation"] == {
        "previous_run_id": 22,
        "previous_total": 4,
        "reconfirmed": 1,
        "carried_forward": 1,
        "needs_recheck": 1,
        # 这一轮没有任何收口声明 —— 三个新键都是 0，行为与这条通道存在之前逐字相同。
        "declared_fixed": 0,
        "declared_overturned": 0,
        "declarations_ignored": 0,
        "suppressed": 1,
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
