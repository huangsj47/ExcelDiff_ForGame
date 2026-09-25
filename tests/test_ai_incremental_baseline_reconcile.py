from __future__ import annotations

import json
import uuid

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from services.ai.baseline_source import previous_anomaly_rows
from services.ai.incremental_baseline import reconcile_result


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
        "suppressed": 1,
    }
    assert "历史结论延续（平台）" in merged["report_markdown"]
    assert "不能因为模型没有重复输出就视为已修复" in merged["report_markdown"]
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
    assert "2 条仍成立" in section and "1 条需要重新确认" in section, section
    # 一份只报数的节必须说清它不是全量清单，否则读者会以为「没列出来 = 没有了」；
    # 另一半是这一节存在的理由本身。
    assert "逐条清单以正文的「风险评估」为准" in section, (
        "没有说清它与正文那一节的分工 —— 两份并排时读者不知道信哪份"
    )
    assert "不能因为模型没有重复输出就视为已修复" in section, (
        "「没提到 = 已修好」正是这一节要防的误读"
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
