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

from services.ai.anomaly_scope import out_of_scope
from services.ai.baseline_closures import (
    DECLARED_FIXED,
    DECLARED_STANDING,
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

# 分析范围变了（profile / focus / 仓库集）时，报告里那一句。
#
# 为什么必须写：范围一改，上一轮那批结论回答的就是**另一批文件**的问题，平台不再拿它
# 当基线（见 `baseline_source` 模块抬头）。可「什么都不发生」在报告里长得和「历史结论
# 恰好为空」一模一样 —— 读者会以为旧问题都被逐条处理掉了，而后者的方向恰好相反。
SCOPE_MISMATCH_NOTE = (
    "上一轮分析范围与本次不同（profile/focus/仓库），历史结论未继承，仅作独立参考。"
)


def _scope_mismatch_report(result: dict) -> dict:
    """范围变了、又没有更早的兼容基线：一条都不继承，但要在报告里明说。

    早退本身是对的（`reconcile_result` 拿不到旧行时什么都做不了），静默才是错的。
    不加这一节的话，「历史结论延续（平台）」整节不出现，读者无从判断是「本轮没有历史」
    还是「历史被范围这道边界挡住了」。
    """
    merged = deepcopy(result)
    section = "\n".join(("## 历史结论未继承（平台）", "", SCOPE_MISMATCH_NOTE))
    report = str(merged.get("report_markdown") or "").rstrip()
    merged["report_markdown"] = (report + "\n\n" + section).lstrip() if report else section
    return merged


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


def _allowed_path_set(allowed_paths) -> frozenset[str] | None:
    """把调用方给的 `allowed_paths` 归一化成判据要的形态（`None` = 不限制）。

    与 `result_payload._allowed_paths_of` / `change_set._allowed_path_set` 同一口径：
    **空列表不是「没给」**（空集 = 一条都不许），路径要归一化（否则写法差异会变成假
    越界）。这里不 import 那两个函数：它们各自拖着一条依赖链，而合并器只需要一个集合。
    """
    if allowed_paths is None:
        return None
    return frozenset(
        normalize_path(str(path)) for path in allowed_paths if str(path or "").strip()
    )


def _recompute_scope_marks(
    anomalies: list[dict],
    *,
    allowed: frozenset[str] | None,
    change_paths: frozenset[str],
) -> int:
    """为缺少范围标记的结论补判 `out_of_scope`，返回被单列的条数。

    ## 两种来源：已有标记保留，缺标记补判（WP28，审查 P1 已复现）

    `out_of_scope` 是「它和**那一轮**的评估范围有没有关系」的冻结结果：上一轮标成
    背景发现的那条，继承时必须带着标记回来（`baseline_source._anomaly_rows` 从运行
    载荷补回，`_historical_anomaly` 保留）。不带的话它在下一轮就变回一条正式结论 ——
    审查复现的形状正是：范围外 critical 背景发现经增量继承后标记消失，风险 low→high。

    旧载荷（`out_of_scope` 缺失）才按本轮 `change_paths` + claims 补判；已有标记不覆盖
    （本轮条目是 `result_payload` 用模型原始 claims 算的，重算会反方向误判）。

    ## 为什么不覆盖**已经带标记**的条目

    `result["anomalies"]` 里的条目已经由 `result_payload` 用**模型原始的
    `Anomaly.claims`** 算过一次；而落进载荷的 `claims` 字段不一定还是那一份 —— 没有
    复核轮的运行里它取自复核行、取不到就是空数组（见 `result_payload._anomaly_entry`）。
    拿载荷再算一遍，会把一条「断言指向改过的表」的**本轮**结论误判成背景发现 —— 那是
    这次修复的另一个方向。所以：**缺标记的**（继承条目、老载荷）必须重算；**已有标记的**
    保持原值（它本来就是用更完整的那份 claims 算出来的）。

    ## 为什么「没有本轮差异」时保持原标记

    `allowed is None` 时判据是「与本次配表差异有没有可核实关联」（WP27）。本轮
    `change_paths` 为空时**根本不存在「本次差异」这个锚点**：重算会把全部有路径的继承项
    判成背景发现，那不是纠正，而是把上一轮的结论整批静默降级（等级掉到规模兜底，读者
    看不出为什么）。所以这一档保持原标记 —— 老调用方/老测试的行为也逐字不变。有差异时
    一律重算。
    """
    if allowed is None and not change_paths:
        return sum(1 for item in anomalies if item.get("out_of_scope"))
    marked = 0
    for item in anomalies:
        if "out_of_scope" not in item:
            item["out_of_scope"] = out_of_scope(
                str(item.get("file_path") or ""),
                allowed,
                # 判据里的 `change_paths` 必须是**归一化后**的那一份：`out_of_scope` 会把
                # `file_path` / 依据路径归一化再比，拿原始写法去比会把命中的关系判丢。
                change_paths=change_paths,
                claims=item.get("claims") or [],
            )
        if item["out_of_scope"]:
            marked += 1
    return marked


def _historical_anomaly(row: Mapping, state: str, previous_run_id: int) -> dict:
    disposition = str(row.get("disposition") or "pending")
    if state == STATE_RECHECK:
        disposition = "pending"
    item = {
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
    # 已有风险的范围标记来自上一轮冻结结论；无关 delta 不改变其身份。
    if isinstance(row.get("out_of_scope"), bool):
        item["out_of_scope"] = row["out_of_scope"]
    return item


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
) -> tuple[dict[str, BaselineClosure], dict[str, BaselineClosure], list[BaselineClosure]]:
    """模型的逐条交代分成三堆：`(要收口的, 说仍成立的, 对不上的)`。

    对不上的那批**一条都不生效**，但会被如实记账并写进报告那一节 —— 模型以为自己关掉了
    一条，而平台什么都没做，是这条通道最坏的失效形态（说了等于没说，双方都以为成了）。

    `standing`（说仍成立）单列一堆：它**不关闭任何东西**，作用只是「这一条模型答过了」。
    两处读它：一是报告那一节据此判断「平台这一轮到底有没有收到交代」（见 `_report_section`），
    二是这一条照旧走「文件变了没」那条判定 —— 一句 `standing` 不抵消「相关文件又变了、
    证据已过期」那个信号（模型未必重看了那个文件）。
    """
    closures, _problems = coerce_closures(merged.get("baseline_updates"))
    known = {str(row.get("fingerprint") or "") for row in old_rows}
    closing: dict[str, BaselineClosure] = {}
    standing: dict[str, BaselineClosure] = {}
    unmatched: list[BaselineClosure] = []
    for item in closures:
        if item.fingerprint not in known:
            unmatched.append(item)
            continue
        bucket = standing if item.status == DECLARED_STANDING else closing
        if item.fingerprint in closing or item.fingerprint in standing:
            unmatched.append(item)  # 同一条答了两遍，第二遍不算
            continue
        bucket[item.fingerprint] = item
    return closing, standing, unmatched


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


def _was_suppressed(row: Mapping) -> bool:
    """这条历史结论上一轮是不是被人工忽略过（`disposition == ignored`）。

    **为什么需要一个专门的判据**：`reconcile_result` 对「已忽略且相关文件没再变」的行
    只做记账（`suppressed` 计数），**不把它放进这一轮的清单**；所以一个人重新看到这条
    结论时，本轮的行上已经看不出「它曾被人判定过不用管」。这个事实必须由合并器带出来，
    否则复活的条目在面板与报告里长得和一条全新发现完全一样。
    """
    return str(row.get("disposition") or "pending") == "ignored"


def _mark_suppressed_previous(item: dict) -> None:
    """在条目上留一个「曾被人工忽略」的标记（`True`，键只在命中时出现）。

    只在**真的命中**时加键：读侧（面板/导出/报告）用的是「有没有这个键」，给每条都塞一个
    `False` 会让这个标记失去区分力，也会让结论载荷多出一个恒假字段。
    """
    item["suppressed_previous"] = True


def _report_section(
    groups: Mapping[str, list[dict]],
    previous_run_id: int,
    *,
    reconciliation: Mapping | None = None,
    scope_mismatch: bool = False,
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

    `scope_mismatch` 那一句是**范围边界**的说明（分析范围与上一轮不同，平台没有继承那
    一轮的结论）：它只在调用方明确给出这个事实时出现，不是常驻句。
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
    answered = (
        int((reconciliation or {}).get("declared_standing") or 0)
        + int((reconciliation or {}).get("declared_fixed") or 0)
        + int((reconciliation or {}).get("declared_overturned") or 0)
    )
    undetermined = len(groups[STATE_RECHECK]) + len(groups[STATE_CARRIED])
    # 「曾被人工忽略、这一轮又回到清单里」的条目数。**只数留在本轮结论里的那些**：
    # 仍被抑制的那批不在 `groups` 里（它们连 `anomalies` 都不进），数错了会把一句
    # 「忽略没生效」写成事实，而那正是本节最不该说的话（忽略本身是生效的）。
    resurfaced = sum(
        1 for group in groups.values() for item in group if item.get("suppressed_previous")
    )
    clauses: list[str] = []
    if resurfaced:
        # 读者最自然的误读是「平台的忽略没生效」—— 所以要说清它**为什么**回来，以及
        # 忽略不是删除（判据在 `baseline.classify`：相关文件又变过，证据就过期了）。
        clauses.append(
            f"其中 {resurfaced} 条**曾被人工忽略**（相关文件本轮又变了，或模型再次报了出来）"
            "—— 「忽略」不是删除，这类条目重新出现时请再处置一次。"
        )
    if ignored:
        # 一条声明「说了等于没说」是这条通道最坏的失效形态（模型以为关掉了，平台什么都没做），
        # 所以对不上的那些必须报出来，而且要指路：指纹抄错了就重抄一遍。
        clauses.append(
            f"另有 {ignored} 条收口声明**没有生效**（指纹对不上本次清单里的条目，"
            "或同一条又被重新报成了结论）。"
        )
    if not answered and undetermined:
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
        #
        # **2026-09-25 晚订正**：这一句在接地那一层修好之前（`ground_payload` 漏带
        # `baseline_closures`）也照样会说 —— 那时模型答了清单，平台收下又扔掉，报告里
        # 却是「一条都没有交代」。修好之后它说的才是事实。判据没变，变的是平台确实收得到了。
        clauses.append(
            "本轮**没有收到 `baseline_updates`**（清单上的条目一条都没有交代）—— "
            "正文里若写着某几条「已修复」，那只是模型的话（平台不复核、也读不到），"
            "在平台的账里它们仍在挂。"
        )
    suppressed_count = int((reconciliation or {}).get("suppressed") or 0)
    if not total and suppressed_count:
        # `total` 只数三组可见条目，被抑制的不进组 —— 于是「上一轮那批全被忽略」以及
        # P0 那个形状（上一轮 0 行、台账里还有忽略项）都会落到 `total == 0`，原来这里
        # 印的是「共 0 条：。」，读起来像这一节坏了。把「它们去哪了」说出来：
        # 不是没有，是用户自己判过「不用再报」。**只在 total 为 0 时说**：有可见条目时
        # 这一节已经有内容，再加一句关于忽略项的常驻说明是噪音（与 `clauses` 其余几句
        # 「只在有事时说」同一口径）。
        clauses.append(
            f"上一轮那批里有 {suppressed_count} 条是**人工已忽略**、相关文件本轮也没有再变，"
            "平台照旧没把它们放回清单（「忽略」不是删除，相关文件再变时会重新出现）。"
        )
    out_of_scope_count = int((reconciliation or {}).get("out_of_scope") or 0)
    if out_of_scope_count:
        # 等级只看 in-scope 的结论（WP27），继承来的背景发现也不许把它抬上去（WP28）。
        # 但「没计入等级」不等于「没事」：条目仍在清单/正文里，这一句是说清口径，
        # 否则读者看到正文里一条 critical、本节等级却不是 high，只会以为报告自相矛盾。
        clauses.append(f"另有 {out_of_scope_count} 条为背景发现/关联不足，未计入等级。")
    if scope_mismatch:
        # 有更早的兼容基线可继承、但紧挨着的那一轮范围不同：本节继承的是更早那次。
        # 不说这句话，读者会把 `Run {previous_run_id}` 当成「上一轮」，而它其实隔了几轮；
        # 更糟的是把更近那一轮的问题默认为「已经被处理掉了」。
        clauses.append(SCOPE_MISMATCH_NOTE)
    tail = "".join(clauses)
    lines = [
        "## 历史结论延续（平台）",
        "",
        f"上一轮（Run {previous_run_id}）报过的问题，由平台按指纹与本轮结果确定性合并，"
        f"共 {total} 条：{counts or '无'}。{tail}"
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
    scope_mismatch: bool = False,
    allowed_paths=None,
) -> dict:
    """Return a cumulative incremental result without treating model silence as resolution.

    ## 模型的收口声明从 `result["baseline_updates"]` 读，不从参数传

    它本来就是**本次运行交出来的东西**（`result_payload` 把它从模型的回答原样搬进载荷，
    也是落库的那一份），从参数再走一遍只会多一个可能与载荷不一致的来源。缺键（老运行、
    失败运行）＝这一轮没有任何声明，行为与这条通道存在之前**逐字相同**。

    ## `scope_mismatch`：范围变了时的报告说明（P1，2026-11-01）

    调用方（`baseline_source` 的读侧）在「state 指针那一轮与本次范围不同」时置真。
    这一轮的旧行可能为空（没有更早的兼容基线），此时本函数照旧早退，但**报告里要留
    一句话**（见 `_scope_mismatch_report`）；有更早的兼容基线时，则在那一节里补一句
    （见 `_report_section`）。缺省 `False` = 行为与这条边界之前逐字相同。

    ## `allowed_paths`：本轮的范围（WP28，2026-11-01）

    就是本次 payload 的 `allowed_paths`（`None` = 读取不限制，空列表 = 一条都不许）。
    合并之后用它把每条条目的 `out_of_scope` **重算一遍**，再参与等级判定 —— 不重算的话，
    上一轮的范围外 / 背景发现继承到这一轮就变回正式结论（审查已复现：low→high），因为
    库里没有这一列、`_historical_anomaly` 也就带不回来。缺省 `None` 时与
    `result_payload._allowed_paths_of` 同一口径（**不是**「没有范围」）；只有
    `changed_paths` 也为空（本轮没有差异可对）时才保持原标记，见 `_recompute_scope_marks`。
    """
    old_rows = [row for row in previous if str(row.get("fingerprint") or "")]
    if not old_rows or previous_run_id is None:
        return _scope_mismatch_report(result) if scope_mismatch else result

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
    declared, standing, unmatched = _split_declarations(merged, old_rows)
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
            if _was_suppressed(old):
                # 上一轮被人工忽略、这一轮又报了回来（通常因为所在文件变了，判据见
                # `baseline.classify`）。两件事一起做：
                #   * 打标记 —— 不标记的话它看起来是一条**全新发现**，处置的人看不出
                #     这是已经有人判过「不用管」的那条；
                #   * **处置退回 pending** —— 它既然重新报了出来，就该由人重新判一次。
                #     沿用 `ignored` 的后果是这一行下一轮又被当成「人工已忽略」抑制掉：
                #     刚刚复活的条目再次静默消失，而这一轮的报告里还写着它。
                item["disposition"] = "pending"
                _mark_suppressed_previous(item)
            else:
                item.setdefault("disposition", str(old.get("disposition") or "pending"))
            if matched_fingerprint in final_by_id:
                final_by_id[matched_fingerprint]["baseline_state"] = STATE_RECONFIRMED
                final_by_id[matched_fingerprint]["baseline_run_id"] = previous_run_id
                if matched_fingerprint != fingerprint:
                    final_by_id[matched_fingerprint]["baseline_previous_fingerprint"] = fingerprint
                if _was_suppressed(old):
                    _mark_suppressed_previous(final_by_id[matched_fingerprint])
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
        if _was_suppressed(old):
            # 相关文件又变了 → 它从「已忽略」被提回清单（`_historical_anomaly` 已把
            # disposition 退回 pending，等人重新处置）。标记留在行上，见 `_was_suppressed`。
            _mark_suppressed_previous(item)
        # 模型**明确答过**「这一条仍成立」（`standing`）：照旧挂在清单里，只是把这件事记在
        # 行上 —— 报告那一节据此知道「平台这一轮收到过交代」，不必再补那句「没有收到」。
        # 它**不抵消**上面那个 `file_changed` 判断：`standing` 只说「这条还在」，没有说
        # 「我重看过那个刚变过的文件」。
        if str(old.get("fingerprint") or "") in standing:
            item["declared_status"] = DECLARED_STANDING
            used.add(str(old.get("fingerprint") or ""))
        anomalies.append(item)
        final_row = _final_row(item)
        if item.get("suppressed_previous"):
            # 结论载荷那一行与 `anomalies` 里的必须是同一份事实（面板/导出读 `final_findings`）。
            _mark_suppressed_previous(final_row)
        final_findings.append(final_row)
        groups[state].append(item)

    merged["anomalies"] = anomalies
    merged["final_findings"] = final_findings
    # 收口声明一律留在审计轨迹里（`active=False` + 模型的理由原文）。面板与导出读的就是
    # 这一列 —— 它**不是**删除，只是不再算作「仍然成立的问题」。
    merged["retracted_findings"] = retracted
    # **先补齐范围标记，再定级**：本轮条目已有标记，继承条目从上一轮载荷恢复；
    # 只有旧载荷缺标记时才用本轮路径补判。这样历史背景发现不会升格，
    # 历史有效风险也不会因本轮只改了另一张表而降格。
    scope_marked = _recompute_scope_marks(
        anomalies,
        allowed=_allowed_path_set(allowed_paths),
        change_paths=frozenset(changed),
    )
    merged["baseline_reconciliation"] = {
        "previous_run_id": previous_run_id,
        "previous_total": len(old_rows),
        "reconfirmed": len(groups[STATE_RECONFIRMED]),
        "carried_forward": len(groups[STATE_CARRIED]),
        "needs_recheck": len(groups[STATE_RECHECK]),
        "declared_standing": len(standing),
        "declared_fixed": len(groups[STATE_DECLARED_FIXED]),
        "declared_overturned": len(groups[STATE_DECLARED_OVERTURNED]),
        # 对不上清单的 + 声明了却又把同一条重新报成结论的（后者以前者同样的方式记账）。
        "declarations_ignored": len(unmatched) + len(set(declared) - used),
        "suppressed": suppressed,
        # 本轮清单里**被单列**（背景发现 / 关联不足）的条数：它们照常列在报告里，但不
        # 参与等级与计数。与 `risk_level` 那一支读的是同一份标记，不会各算一套。
        "out_of_scope": scope_marked,
    }
    section = _report_section(
        groups,
        previous_run_id,
        reconciliation=merged["baseline_reconciliation"],
        scope_mismatch=scope_mismatch,
    )
    report = str(merged.get("report_markdown") or "").rstrip()
    merged["report_markdown"] = (report + "\n\n" + section).lstrip() if report else section
    # **只有 in-scope 的 critical 才抬等级**（WP28）：继承来的背景发现 critical 仍然
    # 列在报告里，但它不属于「这次要处理的问题」（WP27 的口径），不许把等级顶到 high。
    # 只对 `False/缺键` 判真：老调用方/测试里没有这个键时行为与之前逐字相同。
    if any(
        str(item.get("severity") or "") == "critical" and not item.get("out_of_scope")
        for item in anomalies
    ):
        merged["risk_level"] = "high"
    reasons = list(merged.get("risk_reasons") or [])
    reasons.append(
        f"累积基线：延续 {len(groups[STATE_CARRIED])} 条，"
        f"待复核 {len(groups[STATE_RECHECK])} 条，本轮重新确认 {len(groups[STATE_RECONFIRMED])} 条"
        f"，声明收口 {len(groups[STATE_DECLARED_FIXED]) + len(groups[STATE_DECLARED_OVERTURNED])} 条"
    )
    merged["risk_reasons"] = reasons
    return merged
