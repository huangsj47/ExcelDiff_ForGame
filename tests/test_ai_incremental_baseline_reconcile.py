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
