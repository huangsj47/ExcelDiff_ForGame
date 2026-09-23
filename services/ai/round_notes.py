# -*- coding: utf-8 -*-
"""引擎侧的中文措辞：trace 备注、给模型的说明、收尾提示词。

## 为什么单独一个模块

`services/ai/engine.py` 顶着仓库 2000 行的 ERROR 闸门
（`scripts/check_file_length.py --strict`），而这几个函数是**纯文本拼装**：
入参进、字符串出，不碰引擎状态、不看时钟、不查库。搬出来之后引擎那边只留回导，
调用点一个字都不用改。

## 它们为什么值得写得这么啰嗦

这几句话是**模型唯一能读到的平台解释**。实测里模型把「你的索取被拒了」读成
「平台取数失败」、把「配表的行拿不回来」读成「配表整类拿不回来」—— 于是它把一条
**不存在的平台故障**写进报告的信息缺口，或者干脆不再去要本来拿得到的内容。
所以每一句都带着「为什么这么写」的现场记录：改这些措辞之前先读那几段注释，
它们不是文字润色，是踩过的坑。
"""

from __future__ import annotations

from typing import Any, Sequence

from services.ai.budget import (
    ContextItem,
    build_continuation_summary,
    truncate_text,
)
from services.ai.context_tools import describe_request

# 收尾提示词里变更清单保留多少字符。**要小到任何窗口都装得下** —— 这一段是
# `salvage_user_message` 存在的全部理由（见那个函数的说明）。
SALVAGE_SUMMARY_CHARS = 1_500

__all__ = [
    "SALVAGE_SUMMARY_CHARS",
    "batch_notes",
    "combine_notes",
    "describe_item",
    "join_recap",
    "rejected_note",
    "request_labels",
    "salvage_user_message",
]


def combine_notes(notes: Sequence[str], extra: str = "") -> str:
    """把这一轮的补充说明与构造点自己那句拼成一条 trace 备注。"""
    parts = [str(item).strip() for item in notes if str(item).strip()]
    if str(extra).strip():
        parts.append(str(extra).strip())
    return "；".join(parts)


def join_recap(first: str, second: str) -> str:
    """把两段压缩记录接起来（一次分析可能压了不止一次）。空的那段不留空行。"""
    parts = [str(part).strip() for part in (first, second) if str(part or "").strip()]
    return "\n\n".join(parts)


def salvage_user_message(change_summary: str, seen_items: Sequence[ContextItem]) -> str:
    """上游反复拒绝之后的**收尾**提示词。

    ## 为什么值得单独写一段

    这时的局面是：前面几轮的钱已经花了，模型也确实看到过一些内容，但整份提示词塞不进
    它的窗口。丢掉这次分析 = 全部白花；而**一份「证据不足、缺口写清楚了」的报告**仍然
    是用户能用的东西（`rules`/`protocol` 那一层本来就会按证据强度压结论）。

    ## 它必须小到任何窗口都装得下

    所以正文一条都不带：只带**变更清单的开头**（够认出改的是哪一片）与**取到过什么的
    目录**（`build_continuation_summary`，只有标签）。这两段加起来是千字符级，
    128k 窗口的模型也装得下。
    """
    head, truncated = truncate_text(str(change_summary or ""), SALVAGE_SUMMARY_CHARS)
    blocks = [
        "# 本次变更（收尾请求）",
        "",
        "这次分析的提示词超出了模型的上下文窗口，平台已经把历史压过一轮，仍然装不下。"
        "所以这一轮只给你这些：变更清单的开头，以及你之前取到过什么的目录。",
        "",
        "## 变更清单（开头部分）",
        head + ("\n\n（清单在此处被截断，后面还有内容。）" if truncated else ""),
    ]
    if seen_items:
        blocks.extend(
            [
                "",
                "## 你已经取到过的内容",
                build_continuation_summary(seen_items, keep=8),
            ]
        )
    blocks.extend(
        [
            "",
            "## 现在要做的",
            "**立刻输出协议要求的 JSON**，用你已经看到过的内容作答：",
            "- 只报有证据支持的问题，每条的证据必须来自你确实看过的内容；",
            "- 你没能看完的部分写进报告的「信息缺口」，不要用猜测填补；",
            "- 不要再索取上下文 —— 这一轮之后本次分析就结束了。",
        ]
    )
    return "\n".join(blocks)


def rejected_note(rejected: Any) -> str:
    """把「你上一轮这些索取没有被执行、原因是这些」说给模型（一句话，见调用处注释）。

    **最后那句不是客套**：实测里模型把「被拒」读成了「平台取数失败」，并据此写进报告的
    信息缺口。所以这里要显式说清「这不等于那里没有内容」，并告诉它下一步该做什么。
    """
    items = list(rejected)
    shown = items[:4]
    parts = []
    for item in shown:
        subject = str(getattr(item, "detail", "") or "").strip()
        reason = str(getattr(item, "reason", "") or "").strip()
        parts.append(f"{subject}（{reason}）" if subject else reason)
    more = f"，另有 {len(items) - len(shown)} 条同类未逐条列出" if len(items) > len(shown) else ""
    return (
        f"你上一轮有 {len(items)} 条上下文索取**没有被执行**：{'；'.join(parts)}{more}。"
        "**这不等于「那里没有内容」**，也不是平台取数失败 —— 按上面的原因改对之后重新索取即可；"
        "照原样再要一次不会被执行。"
    )


def batch_notes(batch: Any) -> list[str]:
    """把一次批量执行里的异常情况转成给模型看的一句话。"""
    notes: list[str] = []
    if batch.refused_by_budget:
        notes.append(
            f"有 {batch.refused_by_budget} 个上下文请求因超出本次索取额度而未执行。"
        )
    cut = [item for item in batch.items if item.meta.get("truncated")]
    if cut:
        notes.append(truncation_note(cut))
    failed = [item for item in batch.items if item.meta.get("tool_failed")]
    if failed:
        notes.append(
            f"有 {len(failed)} 条上下文取数失败（{'、'.join(describe_item(item) for item in failed[:3])}）。"
            "**取不到不等于没有风险**，不要据此下结论。"
        )
    return notes


def truncation_note(cut: Sequence[ContextItem]) -> str:
    """截断那句话必须**点名是哪一条**，并说清**怎么把剩下的拿回来**。

    线上的一次真实核对逼出了这两件事：面板上写着「有 1 条上下文因长度上限被截断」，
    而那一轮要了两样东西（一份规格文档 + 一张配表）—— 模型（和人）都不知道是哪一条被砍的，
    更不知道下一步该做什么。

    旁边那两条说明都是既点名又给动作的：取数失败那条列出条目并说「取不到不等于没有风险」，
    预算省略那条（`budget._omission_note`）说「如果结论依赖被省略的部分，请重新索取」。
    只有这一条两个都没有，而它说的事情（**你看的内容少了一截**）比那两条更需要行动。

    ## 三种坐标，各自说清给哪类内容用

    「怎么拿回来」按内容形态分三种，而这个函数**看不到形态** —— 它拿到的只是一段渲染好的
    文本（配表的渲染与代码的渲染在这里长得一样，虽然配表的抬头自己写着怎么点名）。
    所以三种都给，并各自点明**是哪类内容用的** —— 模型自己知道它刚才要的是什么。
    按文本抬头去猜形态是可行的，但猜错的方向很坏：把一份规格文档说成「配表，拿不回来」，
    模型就不再去要了，而它本来只要带个 `lines` 就能拿到。

    ## 配表：**工作表**这一级拿得回来，**表内被砍掉的行**拿不回来

    原先这里写的是「配表的正文拿不回来」，那是**半错的**，而且半错的那一半正好把模型劝退：
    模型读到「配表拿不回来」，就不再去要本来拿得到的那几张表了。

    * **工作表这一级是可点名的**：`platform_provider.parse_sheet_window` 就把 `lines` 解释成
      「第几张工作表」，渲染出来的抬头自己写着 `"lines": "<第几张表>"` 并逐张点名缺了谁
      （`_assemble_workbook._render`）。所以这里要求模型「点名工作表」。
    * **表内被砍掉的行不可续**：配表没有行坐标，`_read_excel_sheets` 的 `window` 形参是
      「第几张表」而不是行区间；`_assemble_workbook` 的 `take` 降到 `_EXCEL_MIN_BODY_ROWS`
      之后仍装不下就落到 `truncate_text`（只砍尾巴）—— 重问同一张表得到**逐字节相同**的
      结果，那一段永久不可达。

    后面这半句仍然要说给模型听：不说，它就会对着一张被砍过的表下结论（那正是这条说明存在
    的理由）。但它是**表内行**这一级的结论，不能升格成「整类配表拿不回来」。
    """
    labels = "、".join(describe_item(item) for item in cut[:3])
    more = f"等 {len(cut)} 条" if len(cut) > 3 else ""
    return (
        f"有 {len(cut)} 条上下文因**单条长度上限**被截断（{labels}{more}），"
        "**只砍了尾巴**，后面的内容你没看到。要拿回来："
        "文本 / 代码类重新索取时点名行窗口（`lines=\"1200-1600\"`）；"
        "文档、差异与提交清单类点名段（`lines=\"4-6\"`）；"
        "**配表点名工作表**（`lines=\"2\"` 就是第 2 张表）。"
        "**同一张工作表里被砍掉的行没有坐标**（配表按行渲染、没有行窗口），"
        "要核对那些行请用 `file_diff` —— 它按改动行给。"
    )


def describe_item(item: ContextItem) -> str:
    return str(item.label or item.kind)


def request_labels(payload: Any) -> list[str]:
    """给 trace 用：这次分析向模型要过哪些东西。"""
    if payload is None:
        return []
    return [describe_request(request) for request in payload.requests]
