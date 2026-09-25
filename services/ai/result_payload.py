"""AI 分析的**结论载荷**：风险等级怎么定、结果字典长什么样。

## 为什么单独一个文件

这几个函数此前住在 `services/ai_analysis_service.py` 里。那个文件已经贴着仓库的长度
闸门（`scripts/check_file_length.py --strict` 在 2000 行报错），而它们**与文件的其余部分
几乎没有耦合**：输入是一个 `EngineOutcome` 加一份 payload 摘要，输出是一个纯字典，
不碰数据库、不碰 Flask。搬家是最便宜的腾地方方式（在原地加功能已经加不动了）。

## 一条口径（与「失败」有关的那一条）

**没有结论时按变更规模定级，并且明说那不是模型结论。** 反过来做（没结论就报「低」）
正是这套机制最该避免的事：用户分不出「模型看完说没问题」与「模型压根没答上来」，
而这两件事的处理方式完全相反。`failed_result` 同样只估算等级并写明原因 ——
不伪装成模型结论。
"""
from __future__ import annotations

from typing import List, Mapping, Tuple

from services.ai.engine import EngineOutcome
from services.ai.rules import anomaly_fingerprint
from services.ai.subagent import WEEKLY_MODE
from services.ai.usage import usage_from_outcome
from services.ai.verdict import retracted_fingerprints, render_review_skipped, ruling_rows

# 「额度用尽没轮到的」最多往结果里放几条。界面只展示前几条、其余归到「等 N 条」，
# 所以多放没有用；而额度用尽那一轮模型可能一口气要几十个请求，整份列表会白白撑大
# 落库的 response_payload。**计数不受它影响**（见 `request_budget.refused`）。
_REFUSED_ITEMS_MAX = 20


def coverage_notice_text(coverage) -> str:
    """覆盖账本 → **抽屉里那一段**「本次覆盖与缺口」（markdown 片段；没得说就空串）。

    ## 为什么是「一段文本进 payload」，而不是「塞进报告正文」

    审计要求（AI-P1-03）是「报告必须显示证据覆盖与缺口」，而**抽屉才是用户第一眼看到
    的地方**：导出那份 `.md` 早就有了（`routes/ai_analysis_routes.ai_run_report_md` 传了
    账本），可抽屉读的是落库的 `response_payload.report_markdown` —— 真机验证时那份里
    「本次覆盖与缺口」那一整段的关键词（覆盖那几行、缺口那几条）**一个都没有**。

    写进 `report_markdown` 有两条具体的风险，所以这里刻意不那样做：

    * `services/ai/family_ledger.reconcile_candidates` 拿报告文本做**字符串判据**
      （候选编号有没有被带回、候选的 `file_path` 有没有被点名），而覆盖段里恰好会列出
      **取数失败的文件路径**（例如 `config/奖励模式表_CfgRewardMode.xlsx`）。它有一条
      明文取舍 ——「正文里提到过**不算**采纳」，把平台自己列的缺口路径混进那段文本，
      就是往这个判据里塞平台自己的字；
    * 报告正文还会被别的环节读回去（下一轮的基线摘要、**当初那个**把裁决从正文里解析
      回来的 `verdict.read_ruling` —— 它已随 AI-P0-05 删除，见下面的追记），
      平台追加的段落会跟着一起进去。

    **2026-09-21 追记**：第二条里的裁决块已经没了（AI-P0-05：裁决走
    `EngineOutcome.verdict`，不再从正文反向解析），第一条里的字符串判据也整套删了
    （AI-P0-06：对账只按 `source_candidate_ids` 的 ID 集合，不比文件路径、不比标题）。
    也就是说，**当初这两条理由现在都不成立了**。

    但结论不变，而且现在多了一条更硬的理由：**报告正文只能有一份给人看的规范结论**
    （AI-P1-01）。覆盖段是「这一次看全了没有」的账，它是**平台补充**，与结论本身并列
    显示（`static/js/ai_context_notice.js` 贴在结论后面），而不是结论正文的一部分。
    混进正文会让「正文」这个字段同时承担两种语义，导出、抽屉、下一轮基线都读它。

    放进 payload 里当**独立一个键**（`coverage_notice`），报告正文因此**逐字不变** ——
    上面两条判据连碰都碰不到它。屏幕侧由 `static/js/ai_context_notice.js` 贴到结论后面
    （与「平台补充」那类提示同一个位置、同一条路径）。

    ## 措辞从哪来

    行与缺口都取自 `coverage_ledger`（`report_document.coverage_table_rows` /
    `coverage_gap_lines` 是导出文档用的同一对读法）：同一份数据在两处显示时**必须逐字
    一致**，各写一份迟早会说不到一起（这一行按文件去重、那一行按提交去重）。
    """
    if not isinstance(coverage, dict) or not coverage:
        return ""
    from services.ai import report_document

    rows = report_document.coverage_table_rows(coverage)
    gaps = report_document.coverage_gap_lines(coverage)
    if not rows and not gaps:
        return ""
    lines = [f"**{report_document.COVERAGE_TITLE}（平台补充）**", ""]
    # 行在前、缺口在后：与导出文档同一个顺序（先「看了多少」，再「没看的是哪些、为什么」）。
    lines.extend(f"- {name}：{value}" for name, value in rows)
    lines.extend(f"- {one}" for one in gaps)
    return "\n".join(lines)


def _anomaly_entry(item, row: dict | None) -> dict:
    """一条异常的落库/下发形态（与原先逐字一致），外加它收到的那条复核裁决。

    裁决字段**按指纹从裁决块里对回来**（不按下标：撤销之后清单已经短了，按下标会整体
    错位，而错位的后果是「这条的裁决跑到另一条上」）。取不到裁决时给空值：界面据此显示
    「未复核」，而不是把「没复核」显示成「没问题」。

    2026-09-21 补的四个键（`body_label` / `verify_note` / `verify_evidence_unlocatable` /
    `evidence_capped`）都是**同一条口径**：裁决对这条结论做了什么，读侧不必回去解析报告
    正文就能看到。少了它们，界面只能显示「裁决是 X」而显示不出「平台为什么还压了它的
    置信度」—— 后者是「降了不写」那一类缺陷的落点。

    `source_candidate_ids`（同日补）是同一件事的另一半：这条结论**来源于哪几条分片候选**。
    它必须落库 —— 候选对账（`family_ledger.reconcile_candidates`）在**落库之后**还要按它
    复核「这条候选的去向」，而下一轮的基线、冻结结论的重放也都读这一份。不存的话，血缘
    只活在这一次运行的内存里，任何一次回看都只能再去猜。
    """
    return {
        "fingerprint": anomaly_fingerprint(item),
        "title": item.title,
        "category": item.category,
        "severity": item.severity,
        "confidence": item.confidence,
        "evidence": list(item.evidence),
        "commit_ref": item.commit or "",
        "file_path": item.file_path or "",
        "impact": item.impact or "",
        "suggestion": item.suggestion or "",
        "finding_id": str((row or {}).get("finding_id") or ""),
        # 这条结论来源于哪几条分片候选（`S1-3` 这样的平台编号）。单代理路径为空。
        "source_candidate_ids": [str(text) for text in item.source_candidate_ids or ()],
        # 模型在报告正文里自己编的那个编号（`R3`）；空 = 正文里没有能对上的那一条。
        "body_label": str((row or {}).get("body_label") or ""),
        "verify_verdict": str((row or {}).get("verdict") or ""),
        "verify_verdict_label": str((row or {}).get("verdict_label") or ""),
        "verify_reason": str((row or {}).get("reason") or ""),
        "verify_note": str((row or {}).get("note") or ""),
        "verify_evidence": [str(text) for text in (row or {}).get("evidence_refs") or []],
        # 不成形（照它定位不到东西）的那几条依据。原字符串在 `verify_evidence` 里一字不少，
        # 这里单列出来是让读侧知道**哪几条不能当证据用**。
        "verify_evidence_unlocatable": [
            str(text) for text in (row or {}).get("unlocatable_refs") or []
        ],
        # 这条的置信度是不是被平台按证据缺口压下来的（口径 ③④）。
        "evidence_capped": bool((row or {}).get("evidence_capped")),
        "original_severity": str((row or {}).get("original_severity") or ""),
        "original_confidence": str((row or {}).get("original_confidence") or ""),
        # **逐条原子断言**与各自的裁决（P0-01）。异常面板与导出都读这一份：一条结论
        # 「凭什么算核实过了」这个问题，只有它答得了 —— 标题是模型的概括，而这里
        # 逐条写着哪一条已证实、哪一条待核查、查过的范围是什么。
        "claims": [dict(item) for item in (row or {}).get("claims") or []],
        "pending_claims": int((row or {}).get("pending_claims") or 0),
        "original_title": str((row or {}).get("original_title") or ""),
        # 这一轮复核是**独立取证**还是**原证据复读**（由平台按实际执行过的取数类型判定）。
        "verify_basis": str((row or {}).get("verify_basis") or ""),
        "verify_basis_label": str((row or {}).get("verify_basis_label") or ""),
    }


def _final_findings_of(ruling: dict | None, kept: list, suppressed: frozenset) -> List[dict]:
    """**唯一的 `final_findings`**：活动清单（`kept`）+ 被复核撤销的那些（`active: False`）。

    有复核时活动部分**按 `kept` 逐条取**、裁决按指纹附上 —— `kept` 已经把人工忽略
    （`suppressed`）与复核撤销都滤掉了，于是 `final_findings` 与 `anomalies` 同源、同序、
    同长度，两份不会各说各话。审计轨迹（被撤销的那些）同样滤掉人工已忽略的条目：
    **分诊过的东西不许从另一个键里冒回来**（那正是「已忽略」这个处置会失效的路径）。

    没有复核时把结论投影成同一形状：键一个不少，值是空 —— 读侧不必为「这次没开复核」
    另写一个分支。
    """
    rows = ruling_rows(ruling)
    if rows:
        by_fingerprint = {str(row.get("fingerprint") or ""): row for row in rows}
        active = [
            dict(by_fingerprint[anomaly_fingerprint(item)])
            for item in kept
            if anomaly_fingerprint(item) in by_fingerprint
        ]
        trail = [
            dict(row)
            for row in rows
            if not row.get("active")
            and str(row.get("fingerprint") or "") not in suppressed
        ]
        return active + trail
    return [
        {
            "finding_id": "",
            "source": "synthesis",
            "verdict": "",
            "verdict_label": "",
            "active": True,
            "reason": "",
            "evidence_refs": [],
            "unlocatable_refs": [],
            "evidence_capped": False,
            "note": "",
            "body_label": "",
            "source_candidate_ids": list(item.source_candidate_ids or ()),
            "fingerprint": anomaly_fingerprint(item),
            "title": item.title,
            "category": item.category,
            "file_path": item.file_path or "",
            "commit_ref": item.commit or "",
            "severity": item.severity,
            "confidence": item.confidence,
            "original_severity": item.severity,
            "original_confidence": item.confidence,
            # 没有复核 = 没有裁决，**也没有逐条断言的裁决结果**。键照给、值为空：
            # 读侧不必为「这次没开复核」另写一个分支（同上面那条口径）。
            # 注意 `claims` 的**模型原文**仍然在 `anomalies[]` 里 —— 这里是裁决那一份。
            "claims": [],
            "pending_claims": 0,
            "original_title": item.title,
            "verify_basis": "",
            "verify_basis_label": "",
        }
        for item in kept
    ]


def determine_risk_level(summary: dict) -> str:
    total_files = int(summary.get("total_files") or 0)
    delta_files = int(summary.get("delta_files") or 0)
    critical = bool(summary.get("critical_paths"))
    if critical or total_files >= 120 or delta_files >= 60:
        return "high"
    if total_files >= 80 or delta_files >= 40:
        return "mid_high"
    if total_files >= 40 or delta_files >= 20:
        return "medium"
    if total_files >= 15 or delta_files >= 8:
        return "mid_low"
    return "low"


def risk_level_from_outcome(outcome: EngineOutcome, summary: dict) -> Tuple[str, List[str]]:
    """风险等级与依据。

    **有结论时按结论定级；没有结论时按变更规模定级，并明说那不是模型结论。**
    反过来做（没结论就报「低」）正是这套机制最该避免的事：用户分不出「模型看完说没问题」
    和「模型压根没答上来」，而这两件事的处理方式完全相反。
    """
    if outcome.anomalies:
        severities = {item.severity for item in outcome.anomalies}
        level = "high" if "critical" in severities else "mid_high"
        reasons = [f"模型报出 {len(outcome.anomalies)} 条达门槛的问题"]
        if "critical" in severities:
            reasons.append("其中含 critical")
        if outcome.degradation:
            reasons.append(outcome.degradation_label or outcome.degradation)
        return level, reasons

    level = determine_risk_level(summary)
    reasons = [
        f"变更规模：{summary.get('total_files', 0)} 个文件"
        f"（本次变化 {summary.get('delta_files', 0)} 个）"
    ]
    if summary.get("critical_paths"):
        reasons.append("含关键路径")
    if outcome.succeeded:
        reasons.append("模型未报出达门槛的问题")
    else:
        reasons.append(
            f"⚠️ 本次未取得完整结论（{outcome.degradation_label or outcome.degradation or '原因未知'}），"
            "该等级仅按变更规模估算，**不是**模型评估结果"
        )
    return level, reasons


def note_review_skipped(
    result: dict, *, project_config: Mapping, payload: Mapping
) -> dict:
    """配置**要求**跑对账轮、而这次只分得出一个分析者 ⇒ 报告开篇补一行说明。

    ## 判据（与 `subagent.plan_family` 同源，不许各写一套）

    周版本 + 子代理开着 + `subagent_verify` 为真，三条缺一不可。子代理关掉时那个开关
    本来就不生效（界面上也是这么写的），所以在那里沉默是对的；**要了却没跑**才要说话。

    ## 为什么这一行必须写（2026-09-24，run 65 实测）

    run 65：`subagent_enabled=1`、`subagent_verify=1`，而本次只有 1 个变更文件 ⇒
    `plan_family` 判「分不出 2 片」返回 `None`、走单代理路径。于是既没有对账轮，报告里
    也**一个字都没提**，而 `help.html` 写的是「对账轮没跑成时会如实标成降级」—— 界面那条
    横幅只管面板，**报告才是被存档、被导出、被转发的那一份**。

    「跑了但没跑成」由 `subagent.run_family` 自己写（它才知道成员账），两处判据互斥：
    这里只在单代理路径上被调用。返回新字典，不改调用方手上那一份。
    """
    if not (
        str(payload.get("mode") or "") == WEEKLY_MODE
        and project_config.get("subagent_enabled")
        and project_config.get("subagent_verify")
    ):
        return result
    note = render_review_skipped(
        "本次的改动只分得出一个分析者、没有分片 —— 它核对的正是各分片各自的结论"
    )
    return {
        **result,
        "report_markdown": note + "\n" + str(result.get("report_markdown") or ""),
    }


def result_payload(
    outcome: EngineOutcome,
    payload: dict,
    *,
    suppressed: frozenset = frozenset(),
    context_budget_note: str = "",
    budget_plan: dict | None = None,
) -> dict:
    """给前端与后续读取用的结果。

    保留既有的 `risk_level`（界面在读它），其余键是真实产出。被人工忽略的结论不进
    `anomalies` —— 尊重分诊结果，而不是每轮再问一次。**被复核撤销的结论同样不进**：
    它已经不在 `outcome.anomalies` 里了（`subagent.aggregate_outcomes` 按 `final_findings`
    取的活动清单），这里再按裁决块里的指纹滤一道 —— 那是一条约定，而这里是一道闸门。

    `context_budget_note` 是「这次分析的提示词预算被窗口压过」的说明（`_apply_model_window`）。
    以前它只写进日志 —— 于是「这次分析浅了」在界面上完全看不出原因，而它正是最需要
    被看见的一类降级。
    """
    summary = payload.get("summary") or {}
    risk_level, risk_reasons = risk_level_from_outcome(outcome, summary)
    # 复核裁决：`EngineOutcome.verdict` 那一份**结构化**的结果（`verdict.Reduction.as_dict()`）。
    #
    # 2026-09-21 之前这一行是 `read_ruling(outcome.report_markdown)` —— 平台把机器状态
    # 序列化成报告正文末尾的一行 HTML 注释，再从 markdown 里反向解析回来。两头都错：
    # 写进去的那一段在安全 Markdown 渲染器下会变成可见的乱码（实测占 run 20 正文的
    # 35.3%），而「从给人看的文本里恢复机器状态」本来就不该是一条契约。
    #
    # 取不到 = 这次没有复核（单代理路径 / 没开对账轮），或者复核对结论没有产生任何影响
    # —— 下面两个额外条件都退化成「什么都不做」，单代理那条路逐字不变。
    ruling = outcome.verdict if isinstance(outcome.verdict, dict) and outcome.verdict else None
    retracted = retracted_fingerprints(ruling)
    kept = [
        item
        for item in outcome.anomalies
        if anomaly_fingerprint(item) not in suppressed
        and anomaly_fingerprint(item) not in retracted
    ]
    # 逐条裁决按**指纹**与结论对起来（不按下标：撤销之后的清单已经短了，按下标会整体错位）。
    rows = {str(row.get("fingerprint") or ""): row for row in ruling_rows(ruling)}

    return {
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        # **给人看的那一份报告正文**（= `ai_analysis_run.response_text`）。
        # 正文主体是模型写的整体汇总报告；开篇是复核摘要，正文之后是复核标注明细与平台
        # 那几节（未归类 / 条数上限 / 信息缺口）；对账轮原文不在这里（见下面
        # `verify_report_markdown`）。
        #
        # **一个字节的机器 JSON 都不许有**：裁决走 `outcome.verdict`，正文只给人读
        # （AI-P0-05）。从前这里还会带一行 `<!-- ai-verify-ruling: {...} -->`。
        "report_markdown": outcome.report_markdown,
        # **模型自己写的那份汇总草稿**：只有有复核结论时才另存这一份（没开对账轮的运行里
        # 它就是上面那份正文的开头，再存一份等于同一段字节在载荷里出现两次）。它是给外部
        # 读侧保留的**存档**（模型原始草稿、不含平台拼接），默认不渲染 —— 屏幕与导出读的
        # 都是上面那份正文。
        "draft_markdown": outcome.draft_markdown,
        # 对账轮的整份原文（同样只作为存档；它的结论已经由平台按裁决**结果**渲染进了
        # 开篇的「复核摘要（平台）」与正文之后的「复核标注（平台）」两节，原文里那个
        # json 块也已被摘掉）。
        "verify_report_markdown": outcome.verify_report_markdown,
        "status": outcome.status,
        "degradation": outcome.degradation,
        "degradation_label": outcome.degradation_label,
        "error_message": outcome.error_message,
        # 上下文预算与压缩的账。放在这里（而不是只写日志）的理由与 usage 一样：
        # SSE 的 result 事件与 /latest 自动都有，界面不必再拉一次接口。
        "context": {
            "budget_note": str(context_budget_note or ""),
            "budget_plan": dict(budget_plan or {}),
            "compaction": outcome.compaction.to_dict(),
            # 「上下文索取额度」这本账。原先只有一句 `degradation_label`
            # （「上下文索取额度用尽，基于已有证据出结论」），用户看完只知道**出事了**，
            # 不知道三件该知道的事：缺的是哪几块、占多少、该调什么。
            #
            # * `used` / `refused` 是同一个账本的两侧：模型这次一共索取
            #   `used + refused` 次，其中 `refused` 一次都没轮到；
            # * `refused_items` 是那几块的标签（点名到文件 / 查询），界面据此把
            #   「还有文件没看」展开成具体清单。
            #
            # `used` 在**子代理模式下是全家合计**（见 `subagent._final_outcome`），
            # 所以界面不能拿它跟配置里的上限直接比 —— 那个上限是**每个成员**的
            # （每个成员各拿一整份，见 `subagent.MEMBER_BUDGET_PERCENT`）。
            # 文案里因此只说比例，不说「已用 N/M」。
            "request_budget": {
                "used": int(outcome.requests_used or 0),
                "refused": len(outcome.refused_requests),
                # 列表**封顶**（界面只展示前几条，剩下的是「等 N 条」），但 `refused`
                # 那个计数始终是全额 —— 拿截断后的列表长度当计数，会把「缺了 40 块」
                # 说成「缺了 20 块」。
                "refused_items": list(outcome.refused_requests[:_REFUSED_ITEMS_MAX]),
            },
        },
        "anomalies": [_anomaly_entry(item, rows.get(anomaly_fingerprint(item))) for item in kept],
        # **复核裁决的最终形态**（`services/ai/verdict.py`）。三件事在这一份里同时成立：
        #
        # * `anomalies` 是它的**活动投影**（落库、面板、导出读的都是那一列）—— 被撤销的
        #   条目不在其中，于是异常表、下一轮基线都不再把它当成「仍然成立的问题」；
        # * 被撤销的条目连同**原等级与撤销理由**留在审计轨迹里（`retracted_findings`），
        #   报告正文里也有那一节 —— 撤销本身也是结论，不能没有痕迹；
        # * 每条都带 `finding_id` / `verify_verdict` / `verify_reason` / `verify_evidence`，
        #   读侧不必回去解析报告正文（**不再让模型写的那段文字当独立真相源**）。
        #
        # 没有复核时（单代理、没开对账轮、或复核没给出裁决）这里退化成「活动清单的原样投影」，
        # 形状不变 —— 读侧只写一处，不必为「没复核」多一个分支。
        "final_findings": _final_findings_of(ruling, kept, suppressed),
        "retracted_findings": [
            dict(row)
            for row in ruling_rows(ruling, active=False)
            if str(row.get("fingerprint") or "") not in suppressed
        ],
        "suppressed_count": len(outcome.anomalies) - len(kept),
        # 九个维度**逐一**的交代（`DimensionReview`：命中与否 + 未命中的理由）。
        #
        # 这一份原先在落库时被丢掉了：解析（`protocol._coerce_dimensions`，final 必须非空）
        # 与引擎持有（`EngineOutcome.payload`）两段都在，只有这里不产出这个键 ——
        # 于是「九个维度都过了一遍」这句保证**只活在提示词里**，报告读完之后谁也核不了。
        # 剩下的唯一线索是异常的 `category`（它落在同一集合里），而**未命中维度的理由
        # 全部消失** —— 那正是「这一块为什么不需要看」的唯一出处。
        #
        # 放在这里与 `subagents` 同一个理由：SSE 的 `result` 事件、`/latest`、
        # 结论回放读的都是这同一份字典。
        "dimensions": [
            {"id": item.id, "hit": bool(item.hit), "note": item.note or ""}
            for item in (outcome.payload.dimensions if outcome.payload else ())
        ],
        # **本次分析当时生效的**检查维度清单（id + 中文名），来自
        # `LoadedSkills.dimensions`（见 `engine.run_analysis`）。
        #
        # 它与上面那个 `dimensions` **不是一回事**：上面是模型逐维度的交代（命中与否 +
        # 理由），这里是平台当时用的那份清单本身。它必须随结果落库，因为导出文档要把
        # 异常的 `category` 翻成中文名（`report_document.dimension_label`），而导出发生在
        # **很久之后**：那时项目可能已经改过声明（`references/project-facts.md`）。
        #
        # 现查项目当前声明是**篡改历史**：一条当时归在 `performance` 下的发现，会在新
        # 清单里被显示成「未归类（performance）」——报告读起来完全正常，只是把结论按
        # 今天的口径重新贴了标签。所以清单在分析当时就存进来，导出只读这一份。
        "dimension_specs": [
            {"id": spec.id, "label": spec.label} for spec in outcome.dimension_specs
        ],
        "rounds_used": outcome.rounds_used,
        "requests_used": outcome.requests_used,
        # 本次的用量。放在这里有两个原因：SSE 的 `result` 事件与 `/latest`（读的是落库的
        # response_payload）**自动都有**，抽屉那一行「本次消耗 N tokens」不必为「正在
        # 分析中」再拉一次接口；而且它随结论一起被缓存复用 —— 同一份结论回放两次，
        # 显示的消耗也是当初那次的，不会变成 0。
        #
        # 口径见 services/ai/usage.py。费用**不在这里算**（这一层拿不到价格表），
        # 由 `/ai-analysis/runs/<id>/usage` 在读取侧按当前价格表算。
        "usage": usage_from_outcome(outcome),
        "dropped": [
            {"kind": item.kind, "reason": item.reason, "detail": item.detail}
            for item in outcome.dropped
        ],
        # 子代理模式（services/ai/subagent.py）：这次分了几片、每一片跑了什么、谁没跑成。
        #
        # 放在这里是因为**面板与抽屉读的都是这一份**（落库的 response_payload）：不写进来，
        # 「谁没跑成」就只存在于报告正文的一段文字里，而那段文字是最容易被跳过的部分。
        # 没开子代理时是空列表 —— 读取侧据此区分「没开」与「一个成员都没跑」。
        "subagents": [dict(item) for item in outcome.subagents],
        "subagent_skipped": list(outcome.subagent_skipped),
    }


def failed_result(summary: dict, message: str) -> dict:
    """没发起分析时的结果。**等级按规模估算并写明原因** —— 不伪装成模型结论。"""
    return {
        "risk_level": determine_risk_level(summary),
        "risk_reasons": [message, "该等级仅按变更规模估算，**不是**模型评估结果"],
        "report_markdown": "",
        # 形状与 `result_payload` 一致（读侧只写一处 `payload.get("report_markdown")`）。
        # 没发起分析当然没有草稿与对账轮原文，但**键必须在** —— 少一个键与空串在界面上
        # 的区别是「这一块永远空着」与「这次没有这两份存档」。
        "draft_markdown": "",
        "verify_report_markdown": "",
        "status": "failed",
        "degradation": "not_started",
        "degradation_label": message,
        "error_message": message,
        "anomalies": [],
        # 形状与 `result_payload` 一致（读侧只写一处 `payload.get("final_findings")`）：
        # 没发起分析当然没有任何结论，但**键必须在** —— 少一个键与空列表在界面上的区别是
        # 「这一块永远空着、也没有任何报错」与「这次没有结论」。
        "final_findings": [],
        "retracted_findings": [],
        # 形状与 `result_payload` 保持一致：读取侧只写一处 `payload.get("dimensions")`，
        # 不必为「没跑起来的那次」多加一个分支（少一个键与空列表在界面上的区别是
        # 「九个维度一个都没交代」与「这次根本没跑」，而后者已经由 status 说了）。
        "dimensions": [],
        # 同上面那条：形状一致，读取侧只写一处 `payload.get("dimension_specs")`。
        # 空列表 = 「这次没有清单可查」，导出按平台出厂清单回落（这是缺字段的兜底，
        # 不是为旧数据写的兼容分支）。
        "dimension_specs": [],
        "suppressed_count": 0,
        "rounds_used": 0,
        "requests_used": 0,
        "dropped": [],
    }
