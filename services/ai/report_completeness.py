"""已接地高风险必须进入业务正文；不把双份模型输出的一致性当成保证。"""
from services.ai.report_items import sort_report_items, stable_report_id
from services.ai.rules import anomaly_fingerprint


def ensure_anomaly_cards(items, anomalies, *, dimension_labels=None):
    from services.ai.report_document import dimension_label

    result = list(items)
    represented = {item.get("finding_fingerprint") for item in result}
    for anomaly in anomalies:
        fingerprint = anomaly_fingerprint(anomaly)
        if fingerprint in represented:
            continue
        # 使用模型已经给出的事实与动作，不为缺失业务卡片再编一份证据或需求。
        item = {
            "finding_fingerprint": fingerprint, "finding_ref": anomaly.finding_ref,
            "severity": anomaly.severity, "business_area": dimension_label(anomaly.category, dimension_labels),
            "title": anomaly.title, "change_fact": anomaly.title,
            "change_fact_refs": [anomaly.file_path] if anomaly.file_path else [],
            "impact_hypothesis": anomaly.impact or "影响尚待验证",
            "evidence_status": "unverified", "decision_question": "",
            "test_actions": [{"conditions": "在对应变更与业务入口下复核",
                              "steps": [anomaly.suggestion or "按已有异常依据核对触发条件与实际结果"],
                              "expected": "先核对需求口径，再确认异常是否成立"}],
            "evidence_refs": [], "evidence_gaps": [
                "原异常未绑定到可识别的业务卡片，本条由平台保全；可能与未关联条目重叠，需按原异常依据核对"],
            "evidence_notes": list(anomaly.evidence),
            "source": "anomaly_preservation",
        }
        item["id"] = stable_report_id(item)
        result.append(item)
        represented.add(fingerprint)
    return sort_report_items(result)
