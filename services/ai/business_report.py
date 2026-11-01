"""Readable business report with short display labels and a lossless evidence index.

Display labels are local to a report. Stable IDs and exact evidence coordinates remain
in the index; neither display order nor shortened navigation changes stored findings.
"""
from __future__ import annotations

import re

from services.ai.report_items import sort_report_items


def next_action(item):
    question = item.get("decision_question")
    return "先确认：" + question if question else item["test_actions"][0]["steps"][0]


def evidence_label(item):
    if item.get("evidence_status") == "verified":
        return "事实有依据，影响待确认"
    if item.get("evidence_status") == "partial":
        return "来源不完整，影响待确认" if item.get("evidence_gaps") else "来源已取回，影响待确认"
    return "依据待补，影响待确认"


def render(items, sections, *, confirmed_blocker_ids=()):
    from services.ai.report_document import _business_text as text
    from services.ai.report_document import severity_label

    ordered = sort_report_items(items, confirmed_blocker_ids=confirmed_blocker_ids)
    labelled = [(f"R{index}", item) for index, item in enumerate(ordered, 1)]
    lines = ["# 变更理解", "",
             "本节为模型归纳，含待核实推断，不构成发布裁决；阻断与放行由负责人确认。", "",
             text(sections.get("overview")), ""]

    def chapter(title, key, empty, qualifier=""):
        lines.extend([f"# {title}", ""])
        if qualifier:
            lines.extend([qualifier, ""])
        lines.extend([f"- {text(value)}" for value in sections.get(key) or ()] or [empty])
        lines.append("")

    chapter("变更内容摘要", "changes", "未提供额外变更摘要。")
    chapter("影响面分析", "impacts", "影响面尚待结合条目逐项验证。",
            "以下列出影响路径与验证对象；实际后果须满足对应条目的推断前提并经验证。")
    lines.extend(["# 风险评估", ""])
    if not labelled:
        lines.extend(["本次未报告风险条目；未取证范围不能据此判断安全。", ""])
    else:
        lines.extend(["**风险速览**（等级表示关注优先级，业务影响仍需验证）", ""])
        for label, item in labelled:
            lines.append(f"- **{label} · {severity_label(item['severity'])} · {text(item['business_area'])}**："
                         f"关注标题：{text(item.get('title') or item['change_fact'])}")
        lines.append("")
    for label, item in labelled:
        lines.extend([f"## {label} · {severity_label(item['severity'])} · {text(item['business_area'])}"
                      f"：关注标题：{text(item.get('title') or item['change_fact'])}", "",
                      f"变化（待核实描述）：{text(item['change_fact'])}", "",
                      f"影响假设：在列明前提成立时，可能出现：{text(item['impact_hypothesis'])}" if item.get("impact_basis") == "supported"
                      else f"影响假设：仅在待确认前提成立时，可能出现：{text(item['impact_hypothesis'])}", "",
                      f"依据状态：{evidence_label(item)}；定位见取证索引 {label}。", ""])
        if item.get("impact_conditions"):
            lines.extend(["推断前提：" + "；".join(map(text, item["impact_conditions"])), ""])
        else:
            lines.extend(["推断前提：尚未确认触发条件、换算关系或消费逻辑，不能据此认定实际影响。", ""])
        if item.get("decision_question"):
            lines.extend([f"待确认：{text(item['decision_question'])}", ""])
        # 缺口不能随着技术坐标一起折叠；失败与不确定性仍在风险旁直说。
        if item.get("evidence_gaps"):
            lines.extend(["证据缺口：" + "；".join(map(text, item["evidence_gaps"])), ""])
        lines.extend([f"下一步：{text(next_action(item))}（完整条件与判据见「验证 {label}」）。", ""])
    lines.extend(["# 测试建议", ""])
    for label, item in labelled:
        for index, action in enumerate(item["test_actions"], 1):
            lines.extend([f"## 验证 {label} · {index} · {text(item['business_area'])}", ""])
            if item.get("decision_question"):
                lines.extend([f"先确认：{text(item['decision_question'])}", ""])
            known = action.get("expected_status") == "known" and not item.get("decision_question")
            expected = action["expected"] if known else "待确认需求后补充预期；按确认后的规则判断实际结果"
            lines.extend([f"条件：{text(action['conditions'])}", "",
                          "步骤：" + " → ".join(map(text, action["steps"])), "",
                          f"预期：{text(expected)}", ""])
            if known:
                lines.extend(["预期口径：模型已提供规则依据；来源取回不等于业务需求已获人工确认。", ""])
    if not labelled:
        lines.extend(["没有关联风险条目的专项动作；仍需完成发布基础验证。", ""])
    chapter("回归建议", "regressions", "未提供额外跨功能回归建议。")
    lines.extend(["# 上线与回滚关注点", "",
                  "先核实各项前提与需求规则，执行对应验证，再由负责人决定修复与放行；"
                  "上线顺序、观测与回滚措施按已确认规则落实。", ""])
    if sections.get("gaps"):
        lines.extend(["## 信息缺口与待确认", ""])
        lines.extend(f"- {text(value)}" for value in sections["gaps"])
        lines.append("")
    if labelled or sections.get("release_actions"):
        lines.extend(["## 取证索引（技术详情）", "",
                      "短编号仅用于本报告阅读与分工；跨轮追踪以稳定标识为准。", ""])
    for label, item in labelled:
        identifier = str(item["id"])
        safe_id = identifier if re.fullmatch(r"R-[0-9a-f]{12}", identifier) else text(identifier)
        lines.extend([f"### {label} · 稳定标识 {safe_id}", ""])
        if item.get("change_fact_refs"):
            lines.extend(["事实坐标：" + "；".join(map(text, item["change_fact_refs"])), ""])
        for index, action in enumerate(item["test_actions"], 1):
            if action.get("model_expected"):
                lines.extend([f"验证 {label} · {index} 模型原预期（审计原文，不自动作为验收判据）："
                              + text(action["model_expected"]), ""])
        refs = list(item.get("evidence_refs", ())) + list(item.get("impact_refs", ()))
        refs.extend(ref for action in item["test_actions"] for ref in action.get("expected_refs", ()))
        for reference in refs:
            coordinate = " / ".join(text(reference.get(key)) for key in (
                "evidence_id", "repository_id", "repository", "commit", "path", "lines",
            ) if reference.get(key))
            available = "（未取回或不可用）" if reference.get("available") is False else ""
            lines.extend([f"- 证据：{coordinate}{available}", ""])
        if not refs:
            lines.extend(["尚未绑定本轮取证回执；下列原异常坐标未重新核验。" if item.get("evidence_notes")
                          else "尚无可定位证据，须补取证。", ""])
        if item.get("evidence_notes"):
            lines.extend(["原异常依据（未重新核验）：", ""])
            lines.extend(f"- {text(value)}" for value in item["evidence_notes"])
            lines.append("")
    if sections.get("release_actions"):
        lines.extend(["### 模型原上线与回滚建议（审计原文，未授权为放行或阻断决策）", ""])
        lines.extend(f"- {text(value)}" for value in sections["release_actions"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
