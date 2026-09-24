#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把裁决**渲染成给人看的文本** —— 从 `verdict.py` 搬出来的（2026-09-25）。

这一半全是「`Reduction` → 报告里的字」：开篇那一节（`render_ruling_summary`）、正文之后逐条
点名的那一节（`render_ruling`）、「配置要了复核却没跑成」的开篇说明（`render_review_skipped`），
以及老报告里那个机器块的剥离与读回（`strip_ruling_block` / `retracted_fingerprints` /
`ruling_rows`）。

## 为什么单独一个文件

`verdict.py` 顶着 2000 行 ERROR 闸门；而**判**（哪些结论被改判、为什么）与**写**（这段中文怎么
组织）是两件事，改一处不必翻另一处的几千字注释。取值域与数据类在 `verdict_types`，本模块只按
名字取用。

## 兼容出口在 `verdict.py` 底部

读侧与测试此前按 `services.ai.verdict.render_ruling` 取用，那里**逐个回导**保住这些说法。
**新增一个给人看的渲染函数，要同时加到那一行** —— 漏了它，`verdict.render_xxx` 会 AttributeError
（响亮），但只有老读法会炸而新调用点全绿，最难查。
"""
from __future__ import annotations

import re

from services.ai.budget import truncate_text
from services.ai.claims import (
    VERIFY_BASIS_INDEPENDENT,
    VERIFY_BASIS_REPLAY,
    claim_lines,
)
from services.ai.verdict_types import (
    _REASON_MAX_CHARS,
    CONFIDENCE_CEILING_WITH_GAP,
    RULING_BLOCK_MARKER,
    RULING_SUMMARY_NAME,
    RULING_SUMMARY_TITLE,
    RULING_TITLE,
    SOURCE_VERIFY,
    VERDICT_CONFIRMED,
    VERDICT_DOWNGRADED,
    VERDICT_NEEDS_MORE_EVIDENCE,
    VERDICT_RETRACTED,
    FindingRow,
    Reduction,
)

# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

# 对账轮跑了、但一条可逐条应用的裁决都没给（它可能只报了新发现、也可能把裁决写成了正文
# 里的一段话）。2026-09-23 起正文主体是模型写的汇总报告，这一节只做一行说明 —— 不能再让
# 替身文案把整份草稿顶出正文（AI-P1-01 时期的旧形态，用户实测后明确不要）。
# 2026-09-24：它是**开篇那一节**（`RULING_SUMMARY_TITLE`），所以说的是「下面的汇总」。
_NO_CHANGE_SUMMARY = (
    RULING_SUMMARY_TITLE
    + "\n\n"
    + "本次对账轮**没有给出可逐条应用的裁决**（按任务书要求，裁决要在 `report_markdown` "
    "里单独给一个 json 代码块），下面的汇总按原样采信，读的时候各条按未复核看；"
    "对账轮原文存档在本次运行的结论载荷里（`verify_report_markdown`）。\n"
)


def render_review_skipped(why: str) -> str:
    """配置**要求**跑对账轮、而这一轮没跑成时，开篇那一行说明（`why` = 括号里那半句）。

    run 65 实测：配置 `subagent_verify=1`，而本次只有 1 个变更文件 ⇒ `plan_family` 判
    「分不出 2 片」返回 `None`、走单代理路径 —— 报告里**一个字都没提**复核没跑，而
    `help.html` 写的是「对账轮没跑成时会如实标成降级」。界面那条横幅只管面板，**报告才是
    被存档、被导出、被转发的那一份**。配置里压根没开复核时，调用方不许调这里。
    """
    return (
        RULING_SUMMARY_TITLE
        + "\n\n"
        + f"本次**没有跑「找反证」复核**（{why}）。下面正文里的结论都是模型一遍写出来的，"
        "等级与置信度是它自己填的 —— 读的时候按**未经复核的初稿**看。\n"
    )


def _fate_counts(reduction: Reduction) -> list[str]:
    """被复核的条目各是什么下场（`render_ruling_summary` 那一段用它）。

    **与逐条明细同一份数据**（`Reduction` 的同一组属性）：摘要说「转人工核验 2 条」而明细只
    列出 1 条，是这一节最不能出的错 —— 两次各算一遍迟早会分叉。`维持原结论` 那一档拆出
    「只是重看了已有依据」的条数（P0-01 的两种「没找到反证」），因为它的可信度不一样。
    """
    confirmed = [row for row in reduction.rows if row.verdict == VERDICT_CONFIRMED]
    replayed = [
        row for row in confirmed if row.verify_basis != VERIFY_BASIS_INDEPENDENT
    ]
    groups = (
        ("撤销", reduction.retracted),
        (
            "降级",
            tuple(row for row in reduction.rows if row.verdict == VERDICT_DOWNGRADED),
        ),
        (
            "转人工核验",
            tuple(
                row
                for row in reduction.rows
                if row.verdict == VERDICT_NEEDS_MORE_EVIDENCE
            ),
        ),
        ("维持原结论", tuple(confirmed)),
        ("复核新发现（已合入清单）", reduction.new_findings),
    )
    parts = []
    for label, rows in groups:
        if not rows:
            continue
        extra = (
            f"（其中 {len(replayed)} 条只是重看了已有依据、未经独立反证）"
            if label == "维持原结论" and replayed
            else ""
        )
        parts.append(f"**{label} {len(rows)} 条**{extra}")
    return parts


def render_ruling_summary(reduction: Reduction, *, review_ran: bool) -> str:
    """**开篇**那一节：核过几条、有几条没核、被核的那几条各自什么下场。

    ## 为什么在前（2026-09-24，run 57）

    原先它跟在正文后面，读者先读到的是**未经复核的断言**，读到尾部才知道「复核只覆盖
    3 条、其余待人工核验」—— 而正文里那几条高严重度陈述与尾部的状态说明是矛盾的。
    放在前面 + 明写覆盖数，这条矛盾在第一眼就能看见（模型原文一个字都不改）。

    ## 为什么拆成两节（2026-09-24，run 63）

    run 57 那一修把**整节明细**前置了：run 63 的报告开篇是 21 行平台记账，正文被推到第
    23 行 —— 要前置的是**那几个数**，不是对账记录；逐条明细归 `render_ruling`，排在正文
    之后。这一节**不许膨胀**（有测试钉着段数）：正文主体是模型写的报告，这里只报数。
    """
    if not review_ran:
        return ""
    if not reduction.changed:
        # 零裁决那一形态：整节就是这一行说明，没有明细可拆。
        return _NO_CHANGE_SUMMARY

    total_rows = len(reduction.rows)
    reviewed = total_rows - len(reduction.unreviewed)
    parts = _fate_counts(reduction)
    fates = (
        "被复核的这几条：" + "、".join(parts) + "。"
        if parts
        else "本次复核**没有改变任何一条结论**的去留或等级。"
    )
    lines = [
        RULING_SUMMARY_TITLE,
        "",
        # **覆盖数写在最前面**：它排在报告正文之前，读者第一眼要知道的是「下面那些结论里
        # 有多少条被核过」——run 57 的病正是正文写着 20 条结论、尾部才说「待人工核验」，
        # 而读者先看到、也更容易相信的是正文。
        f"本次复核**只覆盖 {reviewed} 条**（主结论共 {total_rows} 条），"
        f"**其余 {total_rows - reviewed} 条未经复核** —— 它们在下面的正文里按模型原话"
        "保留，等级与置信度都还是模型自己填的。",
        "",
        fates + "逐条的下落、理由与断言状态写在正文之后的「复核标注（平台）」一节；"
        # **这一句是读法约定，不能省**（2026-09-24，run 64 实测）：平台只改落库的结论清单
        # 与这两节，正文一个字不动 —— 于是被撤销的那条在「风险评估」里仍写着「缓解：把静默
        # 跳过改回至少一次告警」，而它恰恰是复核撤掉的那条。不在这里说清「正文没按复核改写」，
        # 读者会照着一份已被否证的待办去改代码，而唯一能对上的线索在 4000 字之后。
        "正文各章仍是模型原话、**没有按复核结果改写** —— 被撤销或降级的条目在正文里"
        "仍按原样写着，以「复核标注（平台）」为准。",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_ruling(reduction: Reduction, *, review_ran: bool) -> str:
    """**逐条明细**：每条被复核的结论原等级是什么、裁决成什么、为什么、断言逐条什么状态。

    从 `final_findings` 渲染，**不是模型写的正文**。它排在**正文之后**（2026-09-24，
    run 63）：开篇只留 `render_ruling_summary` 那几个数，明细按需查。两节读的是同一个
    `Reduction`，不存在「摘要说 2 条、明细只列 1 条」的分叉。

    `review_ran` 为假、或这一轮什么都没改变时返回空串（零裁决那一形态整节都在摘要里，
    见 `_NO_CHANGE_SUMMARY`）。
    """
    if not review_ran:
        return ""
    if not reduction.changed:
        return ""

    lines: list[str] = [
        RULING_TITLE,
        "",
        "对账轮（找反证）的裁决已经应用到落库的异常清单与最终结论上（保留 / 降级 / "
        "撤销 / 转人工核验）；这一节**只标注有变化的条目**，未点名的按原样采信。"
        f"覆盖几条、各条什么下场，见开篇的「{RULING_SUMMARY_NAME}」。",
        "",
    ]
    if not reduction.verdicts_seen:
        # 有影响但**一条裁决都没读到**：可能是它只报了新发现、也可能是它把裁决写成了
        # 正文里的一段话（那不是裁决）。这两种情况下「结论为什么没动」都得说明白，
        # 否则读的人会以为复核不生效是平台坏了。
        # 措辞是「**去留**按原样」而不是「按原样采信」：证据缺口那一步（口径 ③④）与
        # 复核有没有给裁决无关，它照样会压置信度 —— 说「一律按原样」就把平台自己刚做的
        # 事说成了没发生。
        notice = (
            "**注意**：本次复核没有回结构化裁决（正文里的话不构成裁决，平台只认 json 块），"
            "正文里的结论**去留**按原样采信，只有它新报出来的条目被合入了清单"
        )
        if reduction.evidence_capped:
            notice += (
                f"；平台另按本次运行的证据缺口压了 {reduction.evidence_capped} 条的置信度"
                "（见「证据缺口」一节），那不是复核的裁决，是平台自己的动作"
            )
        lines.append(notice + "。")
        lines.append("")

    retracted = reduction.retracted
    if retracted:
        lines.append(f"### 已撤销 {len(retracted)} 条（移出当前结论清单）")
        lines.append("")
        lines.append(
            "这几条**不进异常表、不进下一轮基线**（下一轮的基线语义是「上一次为止仍然成立"
            "的问题全集」）；原文与撤销理由留在下面 —— 撤销本身也是结论，不能没有痕迹。"
        )
        lines.append("")
        for row in retracted:
            lines.append(_row_line(row))
        lines.append("")

    downgraded = tuple(row for row in reduction.rows if row.verdict == VERDICT_DOWNGRADED)
    if downgraded:
        lines.append(f"### 已降级 {len(downgraded)} 条（按新等级采信）")
        lines.append("")
        for row in downgraded:
            lines.append(_row_line(row))
        lines.append("")

    pending = tuple(
        row for row in reduction.rows if row.verdict == VERDICT_NEEDS_MORE_EVIDENCE
    )
    if pending:
        lines.append(f"### 待人工核验 {len(pending)} 条（证据不足）")
        lines.append("")
        lines.append(
            "这几条**仍在清单里**，但平台按口径把它们**降了一档等级**（`critical` → `high`、"
            "`high` → `medium`），置信度也不再按 `very_high` 采信 —— 一条自己都说证据不足的"
            "结论不该同时挂着最高等级与最高置信度，请人工看一遍再决定处置。"
        )
        lines.append("")
        for row in pending:
            lines.append(_row_line(row))
        lines.append("")

    confirmed = tuple(row for row in reduction.rows if row.verdict == VERDICT_CONFIRMED)
    if confirmed:
        # **两种「没找到反证」分成两节**（P0-01）。它们在自己的句子里的可信度不一样：
        # 一条是「有人独立去搜过、没搜到」，另一条是「重看了一遍已有的材料、没看出问题」。
        # 实测 run 58 那三条**全部**是后者（6 次索取全指向已有证据地址，一次新检索都没有），
        # 而报告里它们的措辞是「反证不成立（维持原结论）」—— 读的人会把它当成前一种。
        independent = [
            row for row in confirmed if row.verify_basis == VERIFY_BASIS_INDEPENDENT
        ]
        replayed = [
            row for row in confirmed if row.verify_basis != VERIFY_BASIS_INDEPENDENT
        ]
        if independent:
            lines.append(f"### 反证不成立 {len(independent)} 条（独立取证后维持原结论）")
            lines.append("")
            lines.append("有人**自己去搜过**、没找到反证。搜了哪里写在每一条的理由与查过范围里。")
            lines.append("")
            for row in independent:
                lines.append(_row_line(row))
            lines.append("")
        if replayed:
            lines.append(f"### 原证据复读 {len(replayed)} 条（**未经独立反证**）")
            lines.append("")
            lines.append(
                "这几条复核**只重看了已有的依据**，没有做新的检索 —— 「没找到反证」在这里指的是"
                "「在原有材料里没看出问题」，**不等于**有人独立去搜过。它们按原等级采信，"
                "但读的时候要知道这一档的差别。"
            )
            lines.append("")
            for row in replayed:
                lines.append(_row_line(row))
            lines.append("")

    new_findings = reduction.new_findings
    if new_findings:
        lines.append(f"### 对账轮新发现 {len(new_findings)} 条（已合入清单）")
        lines.append("")
        lines.append(
            "这几条是对账轮在找反证的过程中新报出来的，经与主结论同一道校验（结构、重复、"
            "条数上限）后合入 —— 它们是这一轮的附带产出，不是「找反证」的结果。"
        )
        lines.append("")
        for row in new_findings:
            lines.append(_row_line(row))
        lines.append("")

    # 上面各节已经逐条列过的那些（下面那一节不重复列，理由见 `_gap_section`）。
    shown = {
        row.finding_id
        for row in (*retracted, *downgraded, *pending, *confirmed, *new_findings)
    }
    lines.extend(_gap_section(reduction, shown=shown))

    if reduction.rejected:
        lines.append(f"### 复核阶段记账：{len(reduction.rejected)} 条没有进入清单")
        lines.append("")
        lines.append(
            "这里**只记对账轮（V1）新增或改写结论时被平台拒绝的条目**；主分析阶段因条数上限"
            "淘汰的另列在「结论条数上限」，两组不是重复计数。"
        )
        lines.append("")
        for item in reduction.rejected:
            detail = item.detail or "（未记标题）"
            lines.append(f"- {detail}：{item.reason}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _gap_section(reduction: Reduction, *, shown: set[str]) -> list[str]:
    """证据缺口那一节（口径 ③④）。**按平台的动作分组，与上面按裁决分组是两把尺子。**

    没被压过的运行（`evidence_capped == 0`）一个字都不渲染。已经被上面某一节列过的行
    **不在这里重复** —— 它的「平台说明」就写在上面那一行里（两边逐字一致），而同一件事
    在报告里出现两遍正是本节开头那句「以本节为准」要收的口子。
    """
    capped = tuple(row for row in reduction.rows if row.evidence_capped)
    if not capped:
        return []
    lines = [
        f"### 证据缺口 {len(capped)} 条（平台压到 `{CONFIDENCE_CEILING_WITH_GAP}`）",
        "",
        "本次运行的证据里**有已知的缺口**（某个文件被长度上限截断、或者索取额度用尽导致"
        "某一块一次都没轮到）。受影响的这几条**不得维持 `very_high`** —— 证据不完整时还挂着"
        "最高置信度，等于把「没看到」写成了「看过了」。压的是**置信度，不是结论**：它们仍在"
        "清单里，理由写在各条的「平台说明」里。",
        "",
    ]
    lines.extend(_row_line(row) for row in capped if row.finding_id not in shown)
    if all(row.finding_id in shown for row in capped):
        # 一个都不剩：这几条在上面某一节里已经逐条列过（同一行的「平台说明」就是理由）。
        # 不写这一句的话，这一节看起来像「说有 N 条、一条都没列出来」—— 而那正是这一批
        # 缺陷要防的那种「说了没做」。
        lines.append(
            "这几条已经在上面按裁决分组的那几节里逐条列过，"
            "降级理由写在同一行的「平台说明」里；这里不再重复一遍。"
        )
    lines.append("")
    return lines


def _row_line(row: FindingRow) -> str:
    """一行的措辞。**原等级、裁决、处置三样都写出来** —— 只写裁决，读的人不知道
    「降级」是从哪一级降下来的。

    三个 2026-09-21 补上的东西，都是为了让人能**把这一行落回原处**：

    * 头部带上正文里那个编号（口径 ②，`（正文 R3）`）—— 模型没给正文编号时**什么都不写**
      （2026-09-24 起：从前写「（正文未编号）」，那是拿一句真话去填一个不存在的问题，
      而 run 63 的三条全是它 —— 读者看到的是「[F1]（正文未编号）」：一个编号加一句
      「这个编号在正文里找不到」。**头部不再印平台内部编号 `[F…]`**：产品里没有任何
      一处显示它（异常面板的字段里没有 `finding_id`），它只在平台自己的载荷与轨迹里
      成立，印在给人看的报告上只是个查不到的引用）；
    * 等级/置信度**只要动过就写出来**（口径 ①），包括「证据不足」那一档，措辞里带上
      「平台按证据不足降一档」这句出处；
    * 不成形的依据就地标成「（不可定位）」（口径 ③）—— 它照原样留着，但不构成证据。

    2026-09-24（P0-01）再加两样：

    * 头部用的是**裁决之后**的标题（`row.anomaly.title`）：有断言没被证实时它由
      `_compose_title` 只取已证实的部分重排 —— 未证实的肯定断言不许当标题出现
      （run 58 的 F3 标题写着「断言中断进程」，而复核自己承认那一点没核实）；
    * 紧跟一行**逐条断言的清单**（缩进成子列表）。每一条带自己的状态与措辞：
      `已证实` / `待核查：…` / `已检查范围内未发现：…` / `反证成立：…`。
      这一段是「这条结论凭什么算核过了」的唯一答案。
    """
    head = f"- **{_body_label_text(row)}{row.anomaly.title}**："
    original = f"原 `{row.origin.severity}` / `{row.origin.confidence}`"
    if row.verdict == VERDICT_RETRACTED:
        action = "**反证成立（撤销）**，已从当前结论清单移除"
    elif row.verdict == VERDICT_DOWNGRADED:
        action = (
            f"**反证部分成立（降级）**：`{row.origin.severity}` → `{row.anomaly.severity}`"
        )
        # 等级写在动作里了（`severity=False`），但**置信度**若另被证据缺口压过，
        # 还要单独说 —— 否则这一行会写着降了级、却看不出置信度也动了。
        if row.evidence_capped:
            action += _level_change_text(
                row, cause="平台按「证据有缺口」处理", severity=False
            )
    elif row.verdict == VERDICT_NEEDS_MORE_EVIDENCE:
        action = "**证据不足（待人工核验）**"
        action += _level_change_text(row, cause="平台按「证据不足降一档」处理")
    else:
        action = f"**{_action_label(row)}**"
        if row.evidence_capped:
            action += _level_change_text(row, cause="平台按「证据有缺口」处理")
    if row.source == SOURCE_VERIFY:
        action += "（对账轮新发现）"
    detail = [f"{original} → {action}"]
    if row.reason:
        detail.append(f"理由：{truncate_text(row.reason, _REASON_MAX_CHARS)[0]}")
    if row.evidence_refs:
        detail.append("依据：" + "、".join(_ref_text(row)))
    if row.note:
        detail.append(f"平台说明：{row.note}")
    # 复核方式排在最后：它是对**上面整句**的限定（「反证不成立」是重看了已有依据，
    # 还是自己去搜了一遍），不是另一个并列的事实。字段名从「取证方式」改成「复核方式」
    # （2026-09-24）：值本身叫「独立取证」，两个「取证」叠在一行里读着别扭。
    if row.verify_basis:
        detail.append(f"复核方式：**{row.verify_basis_label}**")
    line = head + "；".join(detail)
    claims = claim_lines(row.claim_reviews)
    return line + ("\n" + claims if claims else "")


def _action_label(row: FindingRow) -> str:
    """裁决那一格的中文。**`confirmed` 按取证方式分两种说法**（P0-01）。

    「反证不成立（维持原结论）」这句话的读法是「**有人去找过反证**、没找到」。而复核只
    重看了一遍已有依据时，它答的是另一个问题（「在原有材料里没看出问题」）—— 两句话
    都写在同一行里会自相矛盾（前面说「反证不成立」、后面说「未经独立反证」），
    所以这一格直接换名字，而不是靠后面那句限定去救。

    其他裁决码不带这个区分：降级 / 撤销 / 证据不足说的是**结论本身**怎么了，
    与复核是用哪种方式得出结论无关。
    """
    if row.verdict == VERDICT_CONFIRMED and row.verify_basis == VERIFY_BASIS_REPLAY:
        return "原证据复读（维持原结论）"
    return row.verdict_label


def _body_label_text(row: FindingRow) -> str:
    """头部那一小段「（正文 R3）」。

    对账轮新发现的条目**不写**（它们本来就不在模型写的正文里，`source` 那一栏说了它从
    哪来）。**模型没给正文编号时也什么都不写**（2026-09-24，run 63）：模型的正文不一定
    带 `R1`/`R2` 编号（run 63 用的是【致命】/【高】），那时从前写「（正文未编号）」——
    读者看到「[F1]（正文未编号）」：一个平台内部编号，加一句平台自己承认「它在正文里
    找不到」。要落回正文靠的是**标题**，它就在这一行里。
    """
    if row.source == SOURCE_VERIFY:
        return ""
    return f"（正文 {row.body_label}）" if row.body_label else ""


def _level_change_text(row: FindingRow, *, cause: str, severity: bool = True) -> str:
    """等级 / 置信度动过的话，把**新的那一档**写出来并注明出处（口径 ①）。

    ## 只写新的那一档（2026-09-24，run 63）

    从前写的是「等级 `critical` → `high`」—— 而这一行的开头已经印了
    「原 `critical` / `very_high` → …」，同一个起点在一行里出现两次（run 63 的一条明细
    里同一件事被说了三遍：动作、等级变化、置信度变化）。起点在本行开头，这里只需回答
    「降到了哪一档」。`severity=False` 给「降级」那一支用：那里的等级变化已写在动作里。
    """
    parts: list[str] = []
    if severity and row.anomaly.severity != row.origin.severity:
        parts.append(f"等级降到 `{row.anomaly.severity}`")
    if row.anomaly.confidence != row.origin.confidence:
        parts.append(f"置信度降到 `{row.anomaly.confidence}`")
    if not parts:
        return ""
    return f"，{'、'.join(parts)}（{cause}）"


def _ref_text(row: FindingRow) -> tuple[str, ...]:
    """依据那一行：不成形的那些就地标成「（不可定位）」。

    **不改写原字符串**（模型说了什么是一个事实），只在它后面加这三个字 —— 读的人据此
    知道哪几条能照着去核，哪几条核不了。判据与 `is_locatable_ref` 是同一个函数，
    不在这里另写一份（两处各判一次迟早会不一致）。
    """
    bad = set(row.unlocatable_refs)
    return tuple(
        f"{ref}（不可定位）" if ref in bad else ref for ref in row.evidence_refs
    )


# 历史数据里那行机器块的形状：`<!-- ai-verify-ruling: {...} -->`。它里面若含 `-->`，
# 写进去时被转义成 `-->`（见 git 历史里的 `ruling_block`），所以这个正则里的
# `-->` 一定是那条注释真正的收尾。
#
# **只服务于历史数据的剥离**（`strip_ruling_block`）：新的运行不再往正文里写机器块，
# 但清理脚本与导出路径还要能把**老行**里那一行认出来并摘掉。
_RULING_BLOCK_RE = re.compile(
    r"<!--\s*" + re.escape(RULING_BLOCK_MARKER) + r"\s*:\s*(\{.*?\})\s*-->", re.DOTALL
)


def strip_ruling_block(markdown: str) -> str:
    """去掉**历史数据**里那行机器可读块（`<!-- ai-verify-ruling: {...} -->`）。

    ## 它现在只服务两件事，都不是「新写入的兼容层」

    * **一次性数据清理**（`scripts/clean_ruling_block_from_runs.py`）：库里已有的那些行
      还带着它，清理脚本按同一个正则摘掉；
    * **导出路径**（`routes/ai_analysis_routes.py`）：用户下载的是原始 markdown，
      在旧行被清理之前（或者清理脚本没跑过的库上），那一行会原样出现在文件里。
      导出前摘一次，读的人只看到给人看的那几节。

    ## 为什么这条设计被废掉了（AI-P0-05）

    原先裁决的机器形态就写在报告正文末尾，理由是「HTML 注释在 markdown 渲染里看不见」。
    但本平台的安全渲染器是**先整体转义、再套白名单**（`static/js/ai-report-markdown.js`），
    注释必然变成一段可见的乱码 —— 实测 run 20 的正文里 35.3% 是那段 json。靠注释藏内部
    数据本身就不可靠，所以裁决改走结构化字段（`EngineOutcome.verdict`），正文里只剩给人
    看的内容。这个函数因此不再有「新写入」的一侧。
    """
    return _RULING_BLOCK_RE.sub("", markdown or "").strip()


def retracted_fingerprints(ruling: dict | None) -> frozenset:
    """被撤销的那些条目的指纹。

    `result_payload` 用它把「待落库集合」再滤一道：撤销在 `aggregate_outcomes` 里已经
    生效（`outcome.anomalies` 里已经没有它们），但那是一处**约定**而不是一道闸门，
    多这一步，落库那一侧不必相信上游做对了。
    """
    if not ruling:
        return frozenset()
    return frozenset(
        str(row.get("fingerprint") or "")
        for row in ruling.get("rows") or ()
        if isinstance(row, dict) and not row.get("active") and row.get("fingerprint")
    )


def ruling_rows(ruling: dict | None, *, active: bool | None = None) -> tuple[dict, ...]:
    """裁决里的行（`active=True/False` 过滤；不传则全给）。"""
    if not ruling:
        return ()
    rows = tuple(row for row in ruling.get("rows") or () if isinstance(row, dict))
    if active is None:
        return rows
    return tuple(row for row in rows if bool(row.get("active")) is active)
