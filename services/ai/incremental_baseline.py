"""Deterministically merge the previous cumulative findings into an incremental result.

The model receives the baseline as guidance, but omission is not evidence that an old
problem was fixed.  This module keeps that safety rule in platform code: untouched old
findings remain active, and findings whose files changed remain active as needing review
until a structured result explicitly settles them.
"""

from __future__ import annotations

import json
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence

from services.ai.baseline_closures import (
    DECLARED_FIXED,
    BaselineClosure,
    coerce_closures,
)
from services.ai.scope import normalize_path

STATE_RECONFIRMED = "reconfirmed"
STATE_CARRIED = "carried_forward"
STATE_RECHECK = "needs_recheck"
# 本轮由模型**声明**收口的两种（见 `baseline_closures`）。名字里带 `declared_` 是刻意的：
# 平台没有独立复核过它，读到这个值的人第一眼就该知道这件事。
STATE_DECLARED_FIXED = "declared_fixed"
STATE_DECLARED_OVERTURNED = "declared_overturned"


def _evidence(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return [value] if value.strip() else []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _historical_anomaly(row: Mapping, state: str, previous_run_id: int) -> dict:
    disposition = str(row.get("disposition") or "pending")
    if state == STATE_RECHECK:
        disposition = "pending"
    return {
        "fingerprint": str(row.get("fingerprint") or ""),
        "title": str(row.get("title") or "（无标题）"),
        "category": str(row.get("category") or ""),
        "severity": str(row.get("severity") or "high"),
        "confidence": str(row.get("confidence") or "high"),
        "evidence": _evidence(row.get("evidence")),
        "commit_ref": str(row.get("commit_ref") or ""),
        "file_path": str(row.get("file_path") or ""),
        "impact": str(row.get("impact") or ""),
        "suggestion": str(row.get("suggestion") or ""),
        "disposition": disposition,
        "baseline_state": state,
        "baseline_run_id": previous_run_id,
        "finding_id": "",
        "source_candidate_ids": [],
        "body_label": "",
        "verify_verdict": "",
        "verify_verdict_label": "",
        "verify_reason": "",
        "verify_note": "",
        "verify_evidence": [],
        "verify_evidence_unlocatable": [],
        "evidence_capped": False,
        "original_severity": str(row.get("severity") or "high"),
        "original_confidence": str(row.get("confidence") or "high"),
        # 历史结论的断言清单：上一轮存下来的那一份**原样带回**（它在库里就是 JSON 文本）。
        # 不带的话，这条结论的 `claims` 会在「这一轮没有被模型重新报到」之后消失 ——
        # 而它恰恰是「这条结论当时凭什么算核实过了」的唯一记录。
        # 逐条断言的**裁决**不带：那是上一轮的复核结论，这一轮没核过它（`verify_basis`
        # 保持空 = 未取证），拿上一轮的状态冒充这一轮的核实结果是另一回事。
        "claims": _claims(row.get("claims")),
        "pending_claims": 0,
        "original_title": str(row.get("title") or "（无标题）"),
        "verify_basis": "",
        "verify_basis_label": "",
    }


def _claims(raw: Any) -> list:
    """历史行里的 `claims`（JSON 文本）读成数组。坏数据回空数组，不抛。"""
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _final_row(
    item: Mapping,
    *,
    active: bool = True,
    reason: str = "",
) -> dict:
    """一条旧结论在结论载荷里的行。

    `active=False` 只用于**本轮被声明修好 / 推翻**的那几条（`_declared_row`）：它们照样
    留在 `final_findings` 与 `retracted_findings` 里（面板、导出、审计都要看得到），
    只是不再算作「仍然成立的问题」—— 下一轮基线正是按这个把它们放下的。
    """
    return {
        "finding_id": "",
        "source": "baseline",
        "verdict": "",
        "verdict_label": "",
        "active": active,
        "reason": reason,
        "evidence_refs": [],
        "unlocatable_refs": [],
        "evidence_capped": False,
        "note": "",
        "body_label": "",
        "source_candidate_ids": [],
        "fingerprint": item["fingerprint"],
        "title": item["title"],
        "category": item["category"],
        "file_path": item["file_path"],
        "commit_ref": item["commit_ref"],
        "severity": item["severity"],
        "confidence": item["confidence"],
        "original_severity": item["severity"],
        "original_confidence": item["confidence"],
        # 与 `_historical_anomaly` 同一份（键一个不少，读侧不必分支）。
        "claims": item.get("claims") or [],
        "pending_claims": 0,
        "original_title": item.get("title") or "",
        "verify_basis": "",
        "verify_basis_label": "",
        "baseline_state": item["baseline_state"],
        "baseline_run_id": item["baseline_run_id"],
    }


def _declared_state(closure: BaselineClosure) -> str:
    return (
        STATE_DECLARED_FIXED
        if closure.status == DECLARED_FIXED
        else STATE_DECLARED_OVERTURNED
    )


def _split_declarations(
    merged: Mapping, old_rows: Sequence[Mapping]
) -> tuple[dict[str, BaselineClosure], list[BaselineClosure]]:
    """模型的收口声明分成「对得上清单的」与「对不上的」。

    对不上的那批**一条都不生效**，但会被如实记账并写进报告那一节 —— 模型以为自己关掉了
    一条，而平台什么都没做，是这条通道最坏的失效形态（说了等于没说，双方都以为成了）。
    """
    closures, _problems = coerce_closures(merged.get("baseline_updates"))
    known = {str(row.get("fingerprint") or "") for row in old_rows}
    matched: dict[str, BaselineClosure] = {}
    unmatched: list[BaselineClosure] = []
    for item in closures:
        if item.fingerprint in known and item.fingerprint not in matched:
            matched[item.fingerprint] = item
        else:
            unmatched.append(item)
    return matched, unmatched


def _declared_row(
    old: Mapping, closure: BaselineClosure, previous_run_id: int
) -> tuple[dict, dict]:
    """本轮被模型**声明**收口的一条旧结论：`(审计轨迹行, 结论载荷行)`。

    两行都由上面两个现成的工厂造出来（`_historical_anomaly` / `_final_row`）——
    **不许在这里手抄一份行形状**：手抄的那一份少一个键时，表现是「面板上少一段内容」
    而不是异常，没人会去数（这一课在 `previous_anomaly_rows` 那里已经付过一次）。

    `active=False` 是这条通道的全部作用：它把条目从「仍然成立的问题」里挪出去。**不是
    删除** —— 它仍在 `retracted_findings` 与报告那一节里，并带着模型的理由原文。
    """
    state = (
        STATE_DECLARED_FIXED
        if closure.status == DECLARED_FIXED
        else STATE_DECLARED_OVERTURNED
    )
    item = _historical_anomaly(old, state, previous_run_id)
    item["declared_reason"] = closure.reason
    item["declared_status"] = closure.status
    return item, _final_row(item, active=False, reason=closure.reason)


def _report_section(
    groups: Mapping[str, list[dict]],
    previous_run_id: int,
    *,
    reconciliation: Mapping | None = None,
) -> str:
    """「历史结论延续（平台）」这一节 —— **2026-09-25 起它只是一行账**。

    ## 角色反转过一次（run 63 → 2026-09-25）

    2026-09-24（run 63）这一节被做成**确定性清单**，理由是：模型自己在正文里写一节
    「历史结论状态」、平台再列一遍同一批 15 条标题，读者看不出哪一份算数。那时的分工是
    「本节为准」。

    2026-09-25 产品要求**正文的「风险评估」承担当前仍成立的全部问题**（本轮新增 + 上次
    遗留仍然成立的），于是这里再当一次清单就成了同一批标题的第二份 —— 正是 run 63 那个
    毛病换个方向再来一遍。它退回「账」：**只报总数与分组计数**。用户读 run 70 的导出件时
    的原话是「用户只需要关心完整报告，这些杂项可以简要说明即可」——这一节当时 682 字 / 13 行，
    其中 8 行是逐条清单。

    ## 留下来的两句（都不是计数）

    * **「逐条清单以正文的『风险评估』为准，本节只记数」** —— 一份只报数的节必须说清它
      不是全量清单，否则读者会以为「没列出来 = 没有了」；
    * **「旧结论不能因为模型没有重复输出就视为已修复」** —— 这一节存在的理由本身。

    「需要重新确认」那几条**不再逐条点名**（2026-09-25 产品决定）：它们是「相关文件又变了、
    尚无充分反证」的那一类，条目本身在异常面板与导出附录里都能看到，本节再抄一遍标题
    是第二份清单。要人工去看的那几条，正文的「风险评估」里也在。

    ## 第三组的名字在报告里不叫「需要重新确认」（2026-09-25 再改一次）

    那个名字**读起来像平台对这几条下了结论**，而平台在这里恰恰下不了结论 —— 它算出来的
    只有「这条所在的文件本轮改过、而本轮的结论里没有它」。这个名字与正文撞过车：实测
    run 73 的模型在正文里明确写了「已修复（不再列入清单）」（它读了新代码，看到兜底常量
    加回来了），而本节同时写着「2 条需要重新确认」—— 同一份报告对同一批条目给出两种说法，
    正是 run 63 那个毛病换个方向再来一遍。

    所以报告里这一组改叫**「本轮无结论」**：只陈述平台真正知道的那件事。给模型看的那份
    基线（`baseline.py`）仍叫「需要重新确认」—— 那是**提问**（这条你本轮得看一眼），
    与这里的**记账**不是一回事，两组名字从此分开。
    """
    labels = (
        (STATE_RECONFIRMED, "本轮重新确认"),
        (STATE_CARRIED, "仍成立"),
        # **不许改回「需要重新确认」**：那是提问期的说法（`baseline.py` 给模型看的那份
        # 基线用），在这里读起来像平台下了结论，而它知道的只有「文件改了、本轮结论里没它」。
        # 与正文撞车的实测见模块 docstring。
        (STATE_RECHECK, "本轮无结论"),
        # 这两组**必须带「声明」二字**：平台没有独立复核过，措辞里不许出现「已修复」这种
        # 断言式说法（`baseline_closures` 的模块 docstring 写着这条通道的边界）。
        (STATE_DECLARED_FIXED, "本轮声明已修复"),
        (STATE_DECLARED_OVERTURNED, "本轮声明已被推翻"),
    )
    total = sum(len(groups[state]) for state, _ in labels)
    counts = "、".join(
        f"{len(groups[state])} 条{label}" for state, label in labels if groups[state]
    )
    ignored = int((reconciliation or {}).get("declarations_ignored") or 0)
    declared = int((reconciliation or {}).get("declared_fixed") or 0) + int(
        (reconciliation or {}).get("declared_overturned") or 0
    )
    undetermined = len(groups[STATE_RECHECK]) + len(groups[STATE_CARRIED])
    clauses: list[str] = []
    if ignored:
        # 一条声明「说了等于没说」是这条通道最坏的失效形态（模型以为关掉了，平台什么都没做），
        # 所以对不上的那些必须报出来，而且要指路：指纹抄错了就重抄一遍。
        clauses.append(
            f"另有 {ignored} 条收口声明**没有生效**（指纹对不上本次清单里的条目，"
            "或同一条又被重新报成了结论）。"
        )
    if not declared and undetermined:
        # **平台这一轮一条结构化收口都没收到**，而清单里还有没定论的条目 —— 把这件事说出来。
        #
        # 为什么非说不可（2026-09-25 真机，两次复现）：模型会在正文里写「某几条已修复」
        # （run 76 甚至用上了这条通道自己的词：「声明已由本次提交 fc3f45b5d402 修复」），
        # 而那句话平台**读不到**、也不认（`baseline_closures` 的模块 docstring 写着为什么
        # 不能认）。读者于是看到两份说法：正文说修好了，本节说「仍成立 / 本轮无结论」——
        # 而两份都没有错，缺的是**中间那一格事实**：平台收到了什么。
        #
        # 平台在这里**不判谁对**（它没有独立复核过那几条），只陈述自己收到了什么。这一句
        # 会出现在多数报告里（多数轮次本来就没人声明收口）—— 那不是噪音，那是这条通道
        # 当前的真实状态：正文里那句话与平台的账是**两件事**，读者必须知道。
        clauses.append(
            "本轮**没有收到任何结构化收口声明** —— 正文里若写着某几条「已修复」，"
            "那只是模型的话（平台不复核、也读不到），在平台的账里它们仍在挂。"
        )
    tail = "".join(clauses)
    lines = [
        "## 历史结论延续（平台）",
        "",
        f"上一轮（Run {previous_run_id}）报过的问题，由平台按指纹与本轮结果确定性合并，"
        f"共 {total} 条：{counts}。{tail}"
        "**逐条清单以正文的「风险评估」为准**，本节只记数 —— 旧结论不能因为模型没有"
        "逐条回应就视为已修复：这几条本轮到底怎么样了，正文里逐条写着"
        "（仍成立 / 已修复 / 已被推翻；「声明」那几组是模型给的收口，平台未独立复核）。",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _semantic_match(old: Mapping, current: Mapping) -> float:
    """Conservative fallback for fingerprints that drift when wording changes."""
    old_path = normalize_path(str(old.get("file_path") or ""))
    current_path = normalize_path(str(current.get("file_path") or ""))
    if not old_path or old_path != current_path:
        return 0.0
    old_category = str(old.get("category") or "")
    current_category = str(current.get("category") or "")
    if old_category and current_category and old_category != current_category:
        return 0.0
    old_title = str(old.get("title") or "").strip()
    current_title = str(current.get("title") or "").strip()
    if not old_title or not current_title:
        return 0.0
    return SequenceMatcher(None, old_title, current_title).ratio()


def reconcile_result(
    result: dict,
    previous: Iterable[Mapping],
    *,
    changed_paths: Iterable[str],
    previous_run_id: int | None,
) -> dict:
    """Return a cumulative incremental result without treating model silence as resolution.

    ## 模型的收口声明从 `result["baseline_updates"]` 读，不从参数传

    它本来就是**本次运行交出来的东西**（`result_payload` 把它从模型的回答原样搬进载荷，
    也是落库的那一份），从参数再走一遍只会多一个可能与载荷不一致的来源。缺键（老运行、
    失败运行）＝这一轮没有任何声明，行为与这条通道存在之前**逐字相同**。
    """
    old_rows = [row for row in previous if str(row.get("fingerprint") or "")]
    if not old_rows or previous_run_id is None:
        return result

    merged = deepcopy(result)
    anomalies = list(merged.get("anomalies") or [])
    final_findings = list(merged.get("final_findings") or [])
    retracted = list(merged.get("retracted_findings") or [])
    current = {str(item.get("fingerprint") or ""): item for item in anomalies}
    final_by_id = {
        str(item.get("fingerprint") or ""): item
        for item in final_findings
        if item.get("active") is not False
    }
    changed = {normalize_path(path) for path in changed_paths if str(path or "").strip()}
    groups = {
        STATE_RECONFIRMED: [],
        STATE_CARRIED: [],
        STATE_RECHECK: [],
        STATE_DECLARED_FIXED: [],
        STATE_DECLARED_OVERTURNED: [],
    }
    suppressed = 0
    matched_current: set[str] = set()
    # 声明只对**本次清单里真的有**的指纹生效。对不上清单的（模型的指纹写错、或对的是更早
    # 那一轮的条目）一条都不生效，但**要记账**：否则模型以为自己关掉了一条，而平台当没
    # 看见 —— 那正是这条通道要消灭的那种「说了等于没说」。
    declared, unmatched = _split_declarations(merged, old_rows)
    used: set[str] = set()

    for old in old_rows:
        fingerprint = str(old.get("fingerprint") or "")
        path = normalize_path(str(old.get("file_path") or ""))
        file_changed = bool(path and path in changed)
        matched_fingerprint = fingerprint if fingerprint in current else ""
        if not matched_fingerprint:
            scored = sorted(
                (
                    (_semantic_match(old, item), current_id)
                    for current_id, item in current.items()
                    if current_id not in matched_current
                ),
                reverse=True,
            )
            if scored and scored[0][0] >= 0.72:
                matched_fingerprint = scored[0][1]
        if matched_fingerprint:
            item = current[matched_fingerprint]
            item["baseline_state"] = STATE_RECONFIRMED
            item["baseline_run_id"] = previous_run_id
            if matched_fingerprint != fingerprint:
                item["baseline_previous_fingerprint"] = fingerprint
            item.setdefault("disposition", str(old.get("disposition") or "pending"))
            if matched_fingerprint in final_by_id:
                final_by_id[matched_fingerprint]["baseline_state"] = STATE_RECONFIRMED
                final_by_id[matched_fingerprint]["baseline_run_id"] = previous_run_id
                if matched_fingerprint != fingerprint:
                    final_by_id[matched_fingerprint]["baseline_previous_fingerprint"] = fingerprint
            matched_current.add(matched_fingerprint)
            groups[STATE_RECONFIRMED].append(item)
            continue
        closure = declared.get(fingerprint)
        if closure is not None:
            # 声明生效：**不进 `anomalies`**（那是「仍然成立的问题」的列，落库、面板、
            # 下一轮基线读的都是它），只进审计轨迹与结论载荷 —— 于是下一轮它不再被当成
            # 在挂条目喂回去，而这一轮的读者仍然看得到「模型说它修好了、理由是什么」。
            used.add(fingerprint)
            item, row = _declared_row(old, closure, previous_run_id)
            final_findings.append(row)
            retracted.append(row)
            groups[_declared_state(closure)].append(item)
            continue
        if str(old.get("disposition") or "pending") == "ignored" and not file_changed:
            suppressed += 1
            continue
        state = STATE_RECHECK if file_changed else STATE_CARRIED
        item = _historical_anomaly(old, state, previous_run_id)
        anomalies.append(item)
        final_findings.append(_final_row(item))
        groups[state].append(item)

    merged["anomalies"] = anomalies
    merged["final_findings"] = final_findings
    # 收口声明一律留在审计轨迹里（`active=False` + 模型的理由原文）。面板与导出读的就是
    # 这一列 —— 它**不是**删除，只是不再算作「仍然成立的问题」。
    merged["retracted_findings"] = retracted
    merged["baseline_reconciliation"] = {
        "previous_run_id": previous_run_id,
        "previous_total": len(old_rows),
        "reconfirmed": len(groups[STATE_RECONFIRMED]),
        "carried_forward": len(groups[STATE_CARRIED]),
        "needs_recheck": len(groups[STATE_RECHECK]),
        "declared_fixed": len(groups[STATE_DECLARED_FIXED]),
        "declared_overturned": len(groups[STATE_DECLARED_OVERTURNED]),
        # 对不上清单的 + 声明了却又把同一条重新报成结论的（后者以前者同样的方式记账）。
        "declarations_ignored": len(unmatched) + len(set(declared) - used),
        "suppressed": suppressed,
    }
    section = _report_section(groups, previous_run_id, reconciliation=merged["baseline_reconciliation"])
    report = str(merged.get("report_markdown") or "").rstrip()
    merged["report_markdown"] = (report + "\n\n" + section).lstrip() if report else section
    if any(str(item.get("severity") or "") == "critical" for item in anomalies):
        merged["risk_level"] = "high"
    reasons = list(merged.get("risk_reasons") or [])
    reasons.append(
        f"累积基线：延续 {len(groups[STATE_CARRIED])} 条，"
        f"待复核 {len(groups[STATE_RECHECK])} 条，本轮重新确认 {len(groups[STATE_RECONFIRMED])} 条"
        f"，声明收口 {len(groups[STATE_DECLARED_FIXED]) + len(groups[STATE_DECLARED_OVERTURNED])} 条"
    )
    merged["risk_reasons"] = reasons
    return merged
