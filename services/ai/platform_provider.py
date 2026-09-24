"""真实取数：把模型的索取接到平台既有的取数链路上。

## 为什么 Excel 要特殊对待

`ContextProvider` 契约里，`""` 表示「确实没有内容」、`None` 表示「拿不到」。而平台上
几乎所有取数函数把两者混在一起（失败一律返回 `None`，`get_file_content_at_commit` 更
把异常返回成 `""`）。直接用会**撒谎**：模型把「我们没读到」当成「这里没有改动」，然后
基于这个前提给出一个看起来很确定的结论。

所以这一层做两件事：

1. **区分「拿不到」与「没有内容」**：先确认记录本身存在（`Commit` 行在不在），再看
   取数结果。行不在 → `None`；行在但内容为空 → `""`。
2. **把结构化 diff 渲染成模型读得懂的文本**。配表改动的主战场是 Excel，而 Excel 的
   diff 不是补丁文本。平台页面上渲染的那份结构化差异（`{'type':'excel','sheets':{…}}`）
   必须被翻译成「哪个表、哪一行、哪个字段从什么变成什么」——**绝不能把
   `二进制文件无法显示差异内容` 丢给模型**，那等于告诉它「这里没什么可看的」。

渲染部分（`render_diff_payload`）是纯函数：输入平台返回的 dict，输出文本。所以它可以
脱离数据库与仓库完整单测 —— 这也正是这一层里最容易出错、最值得测的部分。

## 为什么 diff 要读「平台已经算好的那一份」

取数还有第三种失败方式：**平台本地根本没有这份数据**。diff 原先一律现场重算（读本地
工作副本），而 platform/agent 模式下平台被显式禁止 clone，代码文件又没有单文件 diff
缓存 —— 于是代码仓库的 diff 永远取不到，而「取不到」被渲染成「取到了记录但没有补丁
内容」。修法是读平台**已经算好并落库**的周版本合并 diff（周版本页面读的同一个
payload），见 `_weekly_stored_diff`。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from services.ai.docx_view import is_docx, render_docx_text
from services.ai.frozen_repo import (
    FrozenRepository,
    FrozenTreeReader,
    ambiguous_path_text,
    build_read_scope_multi,
    exclusion_reason,
    not_tracked_text,
    reader_for_repository,
    reject_text,
    repository_label,
    resolve_frozen_repositories,
    resolve_repo_path,
)
from services.ai.reference_search import (
    entries_for,
    normalize_query,
    render_result,
)
from services.ai.repo_reference import search_frozen_repositories
from services.ai.scope import AnalysisScope, normalize_path
from services.ai.skill_loader import LoadedSkills
from services.ai.trace_evidence import failure_notice
from services.deployment_mode import is_agent_dispatch_mode
from services.excel_header_profiles import header_kwargs_for, resolve_for_file
from utils.content_window import (
    CONTENT_MAX_CHARS,
    DEFAULT_WINDOW_LINES,
    ContentPage,
    ContentWindow,
    page_lines,
    slice_lines,
)
from utils.logger import log_print
from utils.text_decoding import binary_content_notice, text_or_notice

# 每个工作表最多渲染多少行。**不设上限会把一个几千行的表整个塞进上下文**，而单条上限
# 会从中间截断，模型看到的是一张中间缺一块的表。这里按行裁剪并如实记账。
DEFAULT_MAX_ROWS_PER_SHEET = 120
# 「有记录、但没有补丁正文」时给模型的那句话。
#
# **不能写成「没有改动」** —— 这是这一层存在的理由（见本模块文档）。平台已经把
# 「读不到内容」与「两版一样」在取数那一步分开了（`get_unified_diff_data` 的闸门：
# 当前版本读不出来时直接返回 error 载荷），但真走到渲染这一步就分不出来了，
# 所以两个可能都留着，并明确否掉那个会把「读取失败」读成「这里没问题」的读法。
_NO_PATCH = (
    "取到了记录，但补丁正文是空的（两版内容相同，或者内容没读到）。"
    "**这不等于「没有改动」。**"
)

# 单次 `file_content` 交付的**一页**上限（字符）。**没有别的依据时**用这个值（初始值）：
# 计划推导出来的那个额度经 `ContextTools` → `apply_tool_limits` 交进来，于是同一份
# 提示词预算在小批次与大批次上给出不同的页大小 —— 而不是所有项目都被 11,000 夹住。
#
# 超过一页的正文**不是被砍掉了**，而是可以继续要（`utils.content_window.page_lines`
# 的 `next_cursor`，回执写在抬头里）。这一条是 2026-09-24（工作包 D 的 P2）改的：
# 原先「一次 11,000 字，剩下的没了」在给模型的文本里看不出来，而它据此认为
# 「这份文件就这么大」。
DEFAULT_CONTENT_MAX_CHARS = CONTENT_MAX_CHARS

# Agent 取回**配表**正文时，平台会在正文**前面**补的那一行出处（见
# `_render_agent_file_content` 的 `kind == "excel"` 那一支）。
#
# **这一行也占单条上限的位置。** `_read_excel_sheets` 是按 `char_budget` 精确收敛的
# （见它的 docstring「它交给上层的文本永远不会超预算」，实测能恰好落在 11,000 整），
# 于是「正文顶满额度 + 平台再补 27 字」必然超上限，`ContextTools` 再砍一刀 ——
# 表现是同一份配表**单机部署不报「被截断」、多节点部署报**，而且真砍掉 27 个字
# （末尾那行「正文只展示了前 N 行」与渲染器自己的截断标记正是被砍的那个）。
#
# 所以配表的正文额度**两边都留出这一行的位置**：两端渲染出来的**正文逐字节相同**，
# 补上出处之后也仍然不超上限。
_AGENT_EXCEL_NOTE = "（配表正文由业务节点（Agent）上的工作副本取出）\n"



def render_diff_payload(
    diff_data: Any,
    *,
    path: str = "",
    max_rows_per_sheet: int = DEFAULT_MAX_ROWS_PER_SHEET,
) -> Optional[str]:
    """把平台的 diff 结构渲染成给模型读的文本。

    返回 `None` 表示「这个结构我们认不出来」。**不返回空字符串** —— 空字符串在这个
    契约里是「确实没有差异」，而认不出来的结构必须让调用方知道它没拿到东西。
    """
    if diff_data is None:
        return None
    if isinstance(diff_data, str):
        return diff_data
    if not isinstance(diff_data, Mapping):
        return None

    kind = str(diff_data.get("type") or "").strip()

    if kind == "excel":
        return _render_excel(diff_data, path=path, max_rows=max_rows_per_sheet)
    if kind == "code":
        return _render_code(diff_data, path=path)
    if kind == "text":
        return _render_text(diff_data, path=path)
    if kind == "segmented_diff":
        return _render_segmented(
            diff_data, path=path, max_rows_per_sheet=max_rows_per_sheet
        )
    if kind == "image":
        return _render_image(diff_data, path=path)
    if kind == "binary":
        return _render_binary(diff_data, path=path)
    if kind == "error":
        return _render_error(diff_data, path=path)

    # 有些分支不写 `type`（例如工作表级的结果）。有 sheets 就按 Excel 处理，
    # 有 patch 就按代码处理，否则放弃。
    if diff_data.get("sheets"):
        return _render_excel(diff_data, path=path, max_rows=max_rows_per_sheet)
    if diff_data.get("patch"):
        return _render_code(diff_data, path=path)
    return None


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------


def _delta_provenance(base_commit_id: str, commit: str) -> str:
    """增量那一段的出处说明。**必须有**：模型读到的是「增量」，而这一份只覆盖

    `上一轮 → 这一轮`。不写清楚，它会把「本版本更早的改动不在里面」读成「那些改动
    不存在」—— 与 `_batch_provenance` 是同一件事的两个方向：那个说明「这里比你以为的
    宽」，这个说明「这里比你以为的窄」。
    """
    return (
        f"（出处：**上一轮分析之后新增的那一段** —— {str(base_commit_id)[:8]} … "
        f"{str(commit)[:8]}。这个文件在本版本更早的改动**已经在上次分析里看过**，"
        "不在这份差异里；不要把「这里没有」说成「这周没改过」。）"
    )


def _render_excel(payload: Mapping[str, Any], *, path: str, max_rows: int) -> str:
    where = path or str(payload.get("file_path") or "")

    # 解析失败**明说**，不能渲染成一张空表。
    if payload.get("error"):
        message = str(payload.get("message") or payload.get("error") or "未知原因")
        return f"[配表差异解析失败] {where}：{message}。**这不等于「没有改动」。**"

    sheets = payload.get("sheets") or {}
    deleted_file = str(payload.get("operation") or "") == "deleted"

    # **整份表被删除时也要把工作表列出来。** 原先这里只回一句「该表已被删除」，
    # 而调用方（`git_excel_parser_helpers`）为了这一句之外还能给出「原来有哪几张表、
    # 每张表里是什么」，特地去读了一遍 `commit.parents[0]` —— 注释写着「读一次就能
    # 给出来」。页面读到了，只有 AI 这条路被这句话短路掉，于是它只能把「删掉的 5 行里
    # 有没有已经放出的编号」写成信息缺口。没带 sheets 时才退回那句话。
    if deleted_file and (not isinstance(sheets, Mapping) or not sheets):
        return (
            f"[配表] {where}：该表已被删除。"
            "删除整张表要确认是否有代码或存档仍在引用。"
        )

    if not isinstance(sheets, Mapping) or not sheets:
        return f"[配表] {where}：本次没有可展示的差异。"

    summary = payload.get("summary") or {}
    lines: list[str] = []
    if where:
        lines.append(f"配表差异：{where}")
    if deleted_file:
        # 与工作表级那句同一个口径（见 `_render_sheet`）：**删掉了什么才是内容**。
        # 平台自己给的那句话照旧带着（它是「整份文件」范围的，与工作表级那句不重复）。
        message = str(payload.get("message") or "该Excel文件已被删除").strip()
        lines.append(
            f"**{message}**；下面是它被删除前的内容（每一行都是删除行）。"
            "删除整份配表要确认是否有代码或存档仍在引用。"
        )
    if summary:
        lines.append(
            f"整体：新增 {summary.get('added', 0)} 行、删除 {summary.get('removed', 0)} 行、"
            f"修改 {summary.get('modified', 0)} 行。"
        )
    lines.append("")

    for name, sheet in sheets.items():
        lines.extend(_render_sheet(str(name), sheet, max_rows=max_rows))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# 配表的**读取与装配**搬到 `services/ai/excel_source.py`、**渲染叶子**搬到
# `services/ai/excel_view.py`（各自的 docstring 里写了为什么）。原处回导：本模块的
# `render_diff_payload` / `_render_excel` / `PlatformContextProvider` 仍按旧名字调它们，
# 外部（`services/agent_file_content_reader.py`）与测试也一直直接 import 这些私有名。
from services.ai.excel_source import (  # noqa: E402,F401 —— 本模块与测试仍在按旧名字用
    _is_openpyxl_workbook,
    _read_excel_sheets,
    parse_sheet_window,
)
from services.ai.excel_view import (  # noqa: E402,F401 —— `_render_excel` 与测试仍在按旧名字用
    _cell,
    _render_header_block,
    _render_row,
    _render_sheet,
)

# --------------------------------------------------------------------------
# 代码 / 二进制 / 出错
# --------------------------------------------------------------------------


def _render_code(payload: Mapping[str, Any], *, path: str) -> str:
    where = path or str(payload.get("file_path") or "")
    patch = str(payload.get("patch") or "")
    if not patch.strip():
        return f"[代码] {where}：{_NO_PATCH}"
    return f"代码差异：{where}\n\n{patch}"


def _render_text(payload: Mapping[str, Any], *, path: str) -> str:
    """纯文本 / 代码文件的差异（`.lua`、`.py`、`.md`…）。

    `DiffService._process_text_diff` 交出来的形状是
    `{'type': 'text', 'raw_diff': …, 'hunks': …}` —— 补丁在 **`raw_diff`**，
    而不是 `patch`（`patch` 是 git 侧 `get_commit_range_diff` 的键名）。

    这里曾经根本没有 `text` 分支，于是所有纯文本文件一路落到 `return None`；
    而按 `ContextTools` 的契约，`None` 的含义是「取不到内容」，上层就把它报成
    「取数失败」。后果是**代码仓库的 AI 分析整个看不到 diff** —— 清单里有文件、
    diff 却永远取不到，模型只能写「代码侧几乎无 diff 可判」。平台的 diff 其实
    早就算好了，只是没人把它渲染出来。
    """
    where = path or str(payload.get("file_path") or "")
    patch = str(payload.get("raw_diff") or "")
    if not patch.strip():
        # `raw_diff` 正常都在；hunks 是它的结构化等价物（@@ 头 + 逐行 raw），
        # 作为兜底而不是主路径。
        pieces = []
        for hunk in payload.get("hunks") or []:
            if not isinstance(hunk, Mapping):
                continue
            pieces.append(str(hunk.get("header") or ""))
            for line in hunk.get("lines") or []:
                if isinstance(line, Mapping) and line.get("raw") is not None:
                    pieces.append(str(line["raw"]))
        patch = "\n".join(pieces)
    if not patch.strip():
        return f"[文本] {where}：{_NO_PATCH}"
    return f"文件差异：{where}\n\n{patch}"


def _fit_numbered_content(
    *,
    limit: int,
    window_of: Callable[[int], ContentWindow],
    build: Callable[[ContentWindow], str],
) -> str:
    """渲染「抬头 + 带行号的正文」，并保证**整串**不超 `limit`。

    ## 为什么不能只切正文、剩下的交给预算层

    `limit` 是**整条上下文**的额度，而抬头、每行的 `数字│` 前缀、末尾那句补救指路
    都在这份额度里。取数侧按 `limit` 切正文之后，整串必然比 `limit` 长（实测：
    300 行 × 35 字 → 正文 10,799 字、整串 11,939 字），于是预算层再砍一刀尾巴 ——
    而抬头里的「第 1–300 行」是取数侧写死的、不会跟着改，末尾那句补救指路也一起没了。

    线上能看到的形态（`utils/content_window` 的模块注释里那段「必须与预算层同一个数」
    的推理只覆盖了一半）：抬头是一句格式完整的中文权威句「共 300 行；下面是第 1–300 行」
    —— 连「不是全文」都没有 —— 而正文只到第 276 行，同屏唯一的截断信号是末尾那句英文
    `... [truncated by local tool]`。模型完全有理由按抬头引用第 290 行，而它从没拿到过。

    ## 收敛：**二分出「装得下的最大正文额度」**（2026-09-24 改，工作包 D 的 P2）

    原先这里是「量出开销 → 按 `limit - 开销` 重切」的两轮法。它在额度大（11,000）时够用，
    但在**页小**的时候会把页切得远小于额度：第一轮量的是**整份文件**那一版的渲染开销
    （每行 `数字│` 前缀都算），而重切之后的页只有几行，真实开销比它小一个数量级 ——
    于是「按上一轮的开销算出来的额度」把页按到了十分之一。实测（`content_max_chars=2,000`、
    400 行 × 52 字的文件）：**每页只给了 5 行 / 517 字**，而额度是 2,000 —— 这正是 P2 要
    拆的那种「额度在，却拿不到」。

    现在改成：先把「整段直接装得下」这一档短路返回（模型点名的那一段本来就装得下时
    一个字都不许少），否则在 `[1, limit]` 上**二分**出「渲染结果仍不超上限」的最大正文额度。
    `build` 的长度对额度单调不减，所以二分到的就是最大的那一档；迭代次数是对数级
    （`limit ≤ 32,000` → 至多 15 次），每次只做一次按行切分。

    最后那道 while 是**保证**：金额度太小（抬头自己就顶满）时二分可能一个可行解都没有，
    这时**从尾部整行地裁正文**（不裁整串 —— 那会砍掉末尾那句补救指路，而它是唯一的重来
    路径），抬头与正文永远由同一个页对象生成，两者始终自洽。
    """
    window = window_of(0)
    if limit <= 0:
        return build(window)

    rendered = build(window)
    if len(rendered) <= limit:
        # 模型点名的那一段本来就装得下 —— 一个字都不动（`tests/test_content_window.py`
        # 的 `test_a_named_window_is_not_shrunk_when_it_already_fits` 钉着这件事）。
        return rendered

    # 二分的下界：整份内容那一版的**开销**（抬头 + 行号前缀 + 指路）是最终这一页开销的
    # 上界（行只可能变少），所以「最优正文额度 ≥ limit - 开销」。这样起点比 1 更靠近答案，
    # 但下界永远不小于 1。
    overhead = max(0, len(rendered) - len(window.content))
    low = max(1, limit - overhead)
    high = max(1, limit)
    best: tuple | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = window_of(middle)
        candidate_text = build(candidate)
        if len(candidate_text) <= limit:
            best = (candidate, candidate_text)
            low = middle + 1
        else:
            high = middle - 1
    if best is not None:
        return best[1]

    while len(rendered) > limit and window.content:
        cut = window.content.rfind("\n")
        content = window.content[:cut] if cut > 0 else ""
        end_line = max(window.start_line, window.start_line + content.count("\n"))
        # `_replace` 而不是重建一个 `ContentWindow`：**页对象上多出来的那几个回执字段
        # （`total_chars` / `returned_chars` / `next_cursor`）必须跟着走**。重建会把它们
        # 悄悄丢掉，于是「被字符上限再裁了一刀」这件事在回执里还是「否」——
        # 而回执正是这一层要修的那种静默。
        window = _renumber(window, content=content, end_line=end_line, truncated=True)
        rendered = build(window)
    return rendered


def _renumber(window, *, content: str, end_line: int, truncated: bool):
    """把一段收窄后的正文写回**同一类**页对象，并把回执字段一起更新。

    `returned_chars` / `next_cursor` 是「这一页还剩多少、下一页怎么要」——
    它们跟着正文走，不跟着请求走：正文被再裁一刀之后，`next_cursor` 必须从**新的**
    末端继续，否则模型照它再要一次会拿到一段重叠的正文（而它不会知道自己看重复了）。
    """
    updated = window._replace(
        content=content, end_line=end_line, truncated=truncated
    )
    if hasattr(updated, "returned_chars"):
        total_lines = int(getattr(updated, "total_lines", 0) or 0)
        cursor = ""
        if total_lines and end_line < total_lines:
            width = max(1, int(getattr(updated, "end_line", end_line)) - window.start_line + 1)
            cursor = f"{end_line + 1}-{min(total_lines, end_line + width)}"
        updated = updated._replace(returned_chars=len(content), next_cursor=cursor)
    return updated


def _page_receipt(page) -> str:
    """一条**可以直接核对的回执**：这一页多少字、是不是全部、下一页的 `lines` 写什么。

    ## 为什么把回执塞进抬头那一行，而不是另起一行

    抬头的行号区间与正文的行号是**同一套坐标**（模型写进结论里的定位靠它），所以这一行
    必须自带「给的是哪一段」。而回执说的也正是这件事的另外几个面（整份多少字、这一页
    是不是被字符上限砍过、下一页怎么要）。放在同一行还有一个副作用是好的：
    `_fit_numbered_content` 把抬头算作开销，回执因此**永远不会**被裁掉 ——
    它要是单独一行，尾截断第一刀就可能砍到它（`render_result` 里那段「覆盖率那句必须
    排在最前面」是同一个教训）。

    `next_cursor` 用引号包起来：那是一段可以直接抄进 `lines` 的字符串（`"1181-1260"`），
    模型不必自己做行号算术。没有更多内容时它是 `无`（**不是空**）——
    「没给」与「没有了」是两件事。
    """
    page_truncated = "是" if bool(getattr(page, "truncated", False)) else "否"
    cursor = str(getattr(page, "next_cursor", "") or "")
    total_chars = int(getattr(page, "total_chars", 0) or 0)
    returned = int(getattr(page, "returned_chars", len(getattr(page, "content", "") or "")) or 0)
    return (
        f"｜回执：total_chars={total_chars} "
        f"returned_range={page.start_line}-{page.end_line} "
        f"returned_chars={returned} truncated={page_truncated} "
        f'next_cursor={chr(34) + cursor + chr(34) if cursor else "无"}'
    )


def _render_text_content(
    text: str,
    *,
    path: str,
    lines: str = "",
    auto: bool = False,
    limit: int | None = None,
    origin: str = "",
) -> str:
    """文本/代码正文：**带行号的一段窗口** + 「这是哪一段」的抬头 + **一页回执**。

    为什么带行号：模型写进结论里的定位（「第 1180 行那个判断」）必须能被人复核，而补丁里的
    `@@ -1180,7 +1180,9 @@` 也是行号 —— 两边用同一套坐标，模型才能把正文与改动对上。
    行号只占 `数字│` 这几列（比「第 N 行：」省得多），几千行也只多几千字符。

    `auto=True` 表示这一段是**平台按改动位置挑的**（模型没点名）。要说出来：一段从第 1700 行
    开始的正文，不说来源就像随机截的，模型会以为这就是文件的开头。

    `origin` 是「这一份是从哪读的」那一句（冻结版本的读取会带上它）。同一个路径可能在本次
    分析里被读到过两次（本批次那条提交一份、冻结 tip 一份），不写清版本，模型会把两份
    内容混着用 —— 而它接下来正是要拿它们做「调用方改了没有」的对比。

    额度的口径见 `_fit_numbered_content`：**抬头与行号前缀算在这份额度里**，不是切完
    正文再让预算层砍一刀 —— 那样抬头说的行号与正文实际给到的行对不上。

    `limit` 缺省时用**取数侧的页大小初值**（`DEFAULT_CONTENT_MAX_CHARS`）。一次真实分析里
    它由计划推导（见 `PlatformContextProvider.apply_tool_limits`），于是「用户把预算调高、
    正文还是只有 11,000 字」这件事在结构上不再成立 —— 这是工作包 D 的 P2。
    """
    where = path or ""
    cap = DEFAULT_CONTENT_MAX_CHARS if limit is None else max(1, int(limit))

    def _build(page) -> str:
        if page.total_lines == 0:
            return f"[{where}] 这个文件在当前版本里是空的（0 行）。"
        head = (
            f"文件正文：{where}（共 {page.total_lines} 行；"
            f"下面是第 {page.start_line}–{page.end_line} 行"
        )
        if page.is_partial():
            head += "，**不是全文**"
        head += "）"
        if origin:
            head += origin
        head += _page_receipt(page)
        if auto:
            head += "；这一段是按本次改动的位置自动选的（要看别处请指定 lines）"
        numbered = [
            f"{number}│{line}"
            for number, line in enumerate(
                page.content.split("\n"), start=page.start_line
            )
        ]
        body = "\n".join(numbered)
        if page.truncated:
            # 末尾这句是**唯一的重来路径**（抬头说「不是全文」而不说怎么继续时，模型知道
            # 自己没看全、却不知道怎么要剩下的 —— 配表那边先前就是这么变成死路的）。
            #
            # 例子里填的是**回执算出来的 `next_cursor`**，不是取数侧自己编一个
            # `end+1～end+120`：后者与「模型实际点名的那一段有多宽」无关，
            # 而按实际宽度给的下一页让「一次要点几次」是可预期的。
            cursor = str(getattr(page, "next_cursor", "") or "")
            follow = f'，例如 "{cursor}"' if cursor else ""
            body += (
                f"\n（这段在第 {page.end_line} 行被字符上限截断；需要更多请指定 lines{follow}）"
            )
        # 没有被字符上限砍、但窗口本来就没到文件末尾时**不加正文行**：这种情况下续页的
        # 坐标已经在上面的回执里（`next_cursor="61-71"`）。多加一行会挤掉一行正文 ——
        # 模型点名要的那一段本来就装得下时，一个字都不该少（`tests/test_content_window.py`
        # 的 `test_a_named_window_is_not_shrunk_when_it_already_fits` 钉着这件事）。
        return f"{head}\n{body}"

    return _fit_numbered_content(
        limit=cap,
        window_of=lambda budget: page_lines(text, lines, max_chars=budget),
        build=_build,
    )


def _render_agent_file_content(
    outcome: Mapping[str, Any], *, path: str, auto: bool = False, limit: int | None = None
) -> str:
    """业务节点取回来的正文（它自带「哪一段 / 共多少行」，行号由这里补上）。

    **配表例外**：`kind == "excel"` 时 Agent 回的已经是渲染好的工作表文本（抬头 + 整表统计 +
    工作表的各行），与平台本地那条路是**同一个渲染函数**。那条路不按行切、也没有「第 a–b 行」
    这个概念，所以这里必须原样返回 —— 再包一层行号会变成「行号套行号」，模型会把每一行
    的工作表正文当成文件的行号去引用。抬头也由渲染函数自己写（它才说得清「共几张表 /
    给了哪几张 / 别的怎么要」），这里只补一句出处。

    代价（照旧）：Agent 端取回时的 `max_chars` 与平台本地的 `_content_max_chars` 必须相等
    （都是 `CONTENT_MAX_CHARS`），否则同一次索取在单机与多节点下会给出不同的文本。

    **二进制例外**（`kind == "binary"`）：Agent 读到的是真正的二进制（魔数 / NUL，
    见 `utils.text_decoding.looks_binary`），它回的就是那句话本身
    （`utils.text_decoding.binary_content_notice`，与平台本地同一个模板）。
    这里**原样返回、不加出处、也不套行号**：那句话说的是「这份东西没法用文本核对」，
    不是一个文件的正文 —— 加一行「（内容由业务节点取出）」会让同一次索取在单机与
    多节点下给出两段不同的文本，而这一层的整个理由是两端逐字一致。
    """
    where = path or str(outcome.get("file_path") or "")
    kind = str(outcome.get("kind") or "")
    if kind == "binary":
        content = str(outcome.get("content") or "")
        # Agent 没给那句话时自己补一句（同模板）—— 不能回空串：空串在这个契约里是
        # 「确实没有内容」，那是另一件事。
        return content or binary_content_notice(where)
    if kind == "excel":
        content = str(outcome.get("content") or "")
        if not content:
            return f"[配表解析失败] {where}：内容无法解析成文本表格。**这不等于「没有内容」**。"
        return _AGENT_EXCEL_NOTE + content

    raw = str(outcome.get("content") or "")
    total = int(outcome.get("total_lines") or 0)
    start = int(outcome.get("start_line") or 1)
    if total == 0:
        return f"[{where}] 这个文件在当前版本里是空的（0 行）。"
    # Agent 侧已经按 `CONTENT_MAX_CHARS` 切过一次，但那量的是**正文**；抬头与每行的
    # `数字│` 前缀由这里加，所以整串仍可能超上限（见 `_fit_numbered_content`）。
    # Agent 回来的正文就是它切好的那一段，这里只能**从尾部整行地让**。
    from_agent_truncated = bool(outcome.get("truncated"))

    def _build(window: ContentWindow) -> str:
        head = (
            f"文件正文：{where}（共 {total} 行；"
            f"下面是第 {window.start_line}–{window.end_line} 行"
        )
        if window.is_partial():
            head += "，**不是全文**"
        head += "；内容由业务节点（Agent）上的工作副本取出"
        if auto:
            head += "，这一段是按本次改动的位置自动选的（要看别处请指定 lines）"
        head += "）"
        head += _page_receipt(window)
        numbered = [
            f"{number}│{line}"
            for number, line in enumerate(
                window.content.split("\n"), start=window.start_line
            )
        ]
        body = "\n".join(numbered)
        if window.truncated:
            cursor = str(getattr(window, "next_cursor", "") or "")
            follow = f'，例如 "{cursor}"' if cursor else ""
            body += (
                f"\n（这段在第 {window.end_line} 行被截断；需要更多请指定 lines{follow}）"
            )
        return f"{head}\n{body}"

    def _window_of(budget: int) -> ContentWindow:
        """把 Agent 回来的正文从尾部整行地收到 `budget` 之内。

        ## 回执里的 `total_chars` 说的是**Agent 回传的这一份**，不是整份文件

        平台这一层拿不到整份文件的字节数（正文在业务节点上），能如实说的是「我手里这份
        有多少字」。抬头里的「共 N 行」是 Agent 报的文件总行数（那个数它是知道的），
        所以「是不是全文」由行数判断，而 `total_chars` 只作为「这一份有多大」的量度。
        把不知道的东西写成 0 或假的是一个更糟的错（同 `round_events` 的「未上报 ≠ 0」）。
        """
        content = raw
        truncated = from_agent_truncated
        if budget > 0 and len(content) > budget:
            head = content[:budget]
            cut = head.rfind("\n")
            content = head[:cut] if cut > 0 else head
            truncated = True
        end_line = start + (content.count("\n") if content else 0)
        cursor = ""
        if total and end_line < total:
            width = max(1, end_line - start + 1)
            cursor = f"{end_line + 1}-{min(total, end_line + width)}"
        return ContentPage(
            content=content,
            start_line=start,
            end_line=end_line,
            total_lines=total,
            total_chars=len(raw),
            returned_chars=len(content),
            truncated=truncated,
            next_cursor=cursor,
        )

    return _fit_numbered_content(
        limit=DEFAULT_CONTENT_MAX_CHARS if limit is None else max(1, int(limit)),
        window_of=_window_of,
        build=_build,
    )


def _render_agent_file_diff(
    outcome: Mapping[str, Any], *, path: str, commit: str
) -> str:
    """业务节点算回来的「**这一条提交**改了这个文件的什么」。

    正文不用再加工：Agent 侧算完就地渲染（`render_diff_payload` 的同一份实现），这里
    只补一行出处。**出处必须说**——模型的索取形状是「提交 X 改了什么」，而这条路上给它的
    只是那一条提交与前一次提交之间的差异：周版本分析平时拿的是**整个窗口的合并差异**
    （`_weekly_stored_diff`），不说清它会以为这就是本窗口该文件的全部改动。

    截断由 Agent 侧做（`FILE_DIFF_MAX_BYTES`，末尾带截断标记），这里不重复。
    """
    where = path or str(outcome.get("file_path") or "")
    content = str(outcome.get("content") or "")
    if not content:
        return (
            f"[读不到差异] {where}：业务节点回传的差异是空的。"
            "**这不等于「没有改动」**，需要这个文件的差异时请写成信息缺口。"
        )
    provenance = (
        f"（出处：业务节点（Agent）在它的工作副本上按**提交 {str(commit or '')[:8]} 与"
        "前一次提交**现算的差异 —— 只含这一条提交对这个文件的改动。）"
    )
    # **Agent 那一刀要如实转述。** `agent_file_diff_reader` 是按 `FILE_DIFF_MAX_BYTES`
    # 先把渲染好的差异砍到上限的，而平台随后拿到的段号是在**砍过之后**的文本上数出来的
    # （`context_tools` 的 `render_window`）—— 于是抬头会写「共 3 段」而原文本该是 8 段，
    # 模型读到一份**看起来完整**的段清单，被砍掉的那几段没有任何坐标能点回来。
    #
    # `outcome` 里本来就带着 `truncated` 与 `original_chars`（Agent 侧如实填的），原先这里
    # 只取 `content`，两个键直接丢掉 —— 模型与面板都无从知道这份差异是被截过的。
    if outcome.get("truncated"):
        original = int(outcome.get("original_chars") or 0)
        provenance += (
            f"\n**这份差异在业务节点上就被截掉了**（原文约 {original:,} 字，只回传了 "
            f"{len(content):,} 字）—— 下面的段号**只覆盖收到的那部分**，"
            "没收到的那几段没有任何坐标能点回来。请把它当成信息缺口，"
            "**不要据此下「这个文件只改了这些」的结论**。"
        )
    return f"{provenance}\n{content}"


# 补丁里的块头：`@@ -1180,7 +1180,9 @@`。取的是**新版本侧**的行号 —— `file_content`
# 给的就是当前版本的内容，两边必须是同一套坐标才谈得上「改动附近」。
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", re.M)

def _changed_line_starts(diff_text: str) -> list:
    """渲染好的 diff 里所有块头的新版本起始行号（没有则空列表）。"""
    return [int(match.group(1)) for match in _HUNK_HEADER_RE.finditer(diff_text or "")]


def _window_around_changes(hunk_starts: list, *, span: int) -> str:
    """把默认窗口放在**改动附近**，而不是文件开头（返回 `"a-b"`，挑不出来就空串）。

    为什么这比「开头 400 行」值：改动可能在第 2200 行，而开头那 400 行与它毫无关系 ——
    模型拿到一段无关的代码，要么白花一次索取额度再问一次，要么据此得出错误结论。
    一屏上下文里最值钱的位置就是改动周围。

    改动比一个窗口还宽时只给第一段（抬头会写明「不是全文」），让模型自己决定要不要再要。
    上界不在这里夹（`slice_lines` 会按文件实际行数夹），因为**到这里还不知道文件有多长** ——
    正文是随后才读的，为了夹一个上界先把整份文件读进来是本末倒置。
    """
    if not hunk_starts:
        return ""
    first, last = min(hunk_starts), max(hunk_starts)
    # 改动之前留四分之一屏：函数的签名、局部变量初始化通常就在那几行里。
    start = max(1, first - span // 4)
    end = max(last, start + span - 1)
    if end - start + 1 > span:
        end = start + span - 1
    return f"{start}-{end}"


def _render_segmented(
    payload: Mapping[str, Any], *, path: str, max_rows_per_sheet: int
) -> Optional[str]:
    """分段合并 diff：同一个文件被窗口里若干次**不相邻**的提交改过。

    `merge_strategy == 'segmented'` 时平台交出来的是
    `{'type': 'segmented_diff', 'segments': [<每段一份完整载荷>], 'total_segments': N}`，
    每段自带 `segment_info`（这一段的 `current` / `previous` 与段号）。

    渲染层原先不认识这个类型（没有 `sheets`、也没有 `patch`）→ 一路落到 `return None`
    → 模型看到的是「取数失败」。而分段恰恰是**代码文件在周版本里最常见的形态之一**：
    一个文件在一周里被不相邻的几次提交改过就会变成它。配表侧早就有
    `extract_excel_diff_from_payload` 把分段合并起来，文本/代码侧没有对应的东西。

    每段单独渲染、各自标出「哪两条提交之间」：段与段之间是**不同时间段**的改动，
    拼成一整段连续补丁会让模型读成一次改动（`@@` 行号本来也不连续）。
    """
    segments = payload.get("segments")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        return None
    total = len(segments)
    pieces: list[str] = []
    for index, segment in enumerate(segments, start=1):
        if not isinstance(segment, Mapping):
            continue
        rendered = render_diff_payload(
            segment, path=path, max_rows_per_sheet=max_rows_per_sheet
        )
        if not rendered:
            continue
        info = segment.get("segment_info")
        between = ""
        if isinstance(info, Mapping):
            current = str(info.get("current") or "").strip()
            previous = str(info.get("previous") or "").strip()
            if current or previous:
                between = f"（{previous or '初始版本'} → {current or '未知'}）"
        pieces.append(f"--- 第 {index}/{total} 段{between} ---\n{rendered}")
    if not pieces:
        where = path or str(payload.get("file_path") or "")
        return f"[分段差异] {where}：{total} 段里没有一段带得出正文。"
    return "\n\n".join(pieces)


def _render_image(payload: Mapping[str, Any], *, path: str) -> str:
    """图片差异：只说变化，**绝不把 base64 倒给模型**。

    与 `_render_binary` 同一个道理 —— 写成「没有内容」会让模型当成「没改动」。
    `current_image` / `previous_image` 里是整张图的 base64，粘进上下文能瞬间
    吃掉整个预算，所以这里只取 `operation` / `is_same` 这类结论性字段。
    """
    where = path or str(payload.get("file_path") or "")
    operation = str(payload.get("operation") or "").strip()
    changed = payload.get("is_same") is False or operation in ("added", "modified")
    detail = "这张图确实变了" if changed else "这张图的内容没有变化"
    return (
        f"[图片] {where}：{detail}（{operation or '未知操作'}）。\n"
        "**图片的像素差异平台无法展示** —— **「无法展示」不等于「没有改动」**，"
        "需要确认画面本身时请说明「图片内容无法核对」。"
    )


def _render_binary(payload: Mapping[str, Any], *, path: str) -> str:
    where = path or str(payload.get("file_path") or "")
    message = str(payload.get("message") or "二进制文件无法显示差异内容")
    return (
        f"[无法展示的改动] {where}：{message}。\n"
        "**「无法展示」不等于「没有改动」** —— 这个文件确实变了，只是平台解析不出内容。"
        "需要判断时请说明「该文件内容无法核对」。"
    )


def _render_error(payload: Mapping[str, Any], *, path: str) -> str:
    where = path or str(payload.get("file_path") or "")
    detail = str(payload.get("detail") or payload.get("message") or "未知原因")
    return f"[取数失败] {where}：{detail}。**这不等于「没有改动」。**"
# 「平台已经算好的那份 diff」的取数搬到 `services/ai/stored_diff_source.py`
# （见那边的 docstring）。原处回导：`PlatformContextProvider` 仍按旧名字调它们。
from services.ai.stored_diff_source import (  # noqa: E402,F401 —— 类方法与测试仍在按旧名字用
    _batch_provenance,
    _is_failed_payload,
    _weekly_stored_diff,
)

# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


def _as_frozen_tuple(value: Any) -> tuple:
    """`frozen_repository` 参数 → 一组冻结对象（单个、`None`、序列都收）。

    允许一次给一组，是因为**本项目可能有多个仓库**（见 `_batch_repositories`）：显式
    指定版本的那条路（测试、将来由调用方在派发前定好版本）同样要能一次给全。给单个的
    老写法继续有效 —— 它是长度为 1 的一组。
    """
    if value is None:
        return ()
    if isinstance(value, FrozenRepository):
        return (value,)
    try:
        return tuple(item for item in value if item is not None)
    except TypeError:  # 不是可迭代的：当成单个（鸭子类型，取数层只认那三个属性）
        return (value,)


class PlatformContextProvider:
    """接到平台取数链路上的 `ContextProvider`。

    需要 app 上下文（会用 `db.session`）。**只读**：不写数据库、不触发 clone。
    """

    def __init__(
        self,
        *,
        loaded: LoadedSkills,
        max_rows_per_sheet: int = DEFAULT_MAX_ROWS_PER_SHEET,
        use_stored_batch_diff: bool = True,
        scope: AnalysisScope | None = None,
        manifest=None,
        delta_bases: Optional[Mapping[Any, str]] = None,
        cache_row_ids: Optional[Mapping[Any, int]] = None,
        frozen_repository: FrozenRepository | None = None,
        content_max_chars: int | None = None,
        repo_git_service: Any = None,
        repo_index_files: int | None = None,
    ):
        self._loaded = loaded
        self._max_rows = max_rows_per_sheet
        # 增量分析里**上一轮看到的那条提交**，键是 `(latest_commit_id, file_path)`。
        #
        # 有它的时候，这个文件的 diff 是 `上一轮的 latest → 这一轮的 latest` 两点对比，
        # 而不是缓存里那一份整窗口的合并差异（见 `_delta_diff`）。理由：增量说的是
        # 「上次分析之后新增的部分」，把整窗口再讲一遍既多付一遍 token，也把「这次变了
        # 什么」淹在「这周一共改了什么」里。没有它（新文件 / 这一轮没变的补偿项 /
        # 全量分析）就照旧给整窗口那一份 —— 那时整窗口**就是**它的全部改动。
        self._delta_bases = dict(delta_bases or {})
        # 每条 delta 来自哪一行 `WeeklyVersionDiffCache`（REV-AI-003），键
        # `(repository_id, latest_commit_id, file_path)` —— 与 `_delta_bases` 同形。
        #
        # 有它的时候，落库 diff 的来源**按行主键取**；没有时退回旧查询，但命中多行就
        # **拒绝猜测**（见 `stored_diff_source._weekly_stored_diff`）：两个周窗口的终点
        # 撞上同一条提交时，旧查询会取 id 最大的那一行，而那可能是**另一个窗口**的。
        self._cache_row_ids = dict(cache_row_ids or {})
        # 本批次的范围（哪些提交、各改了哪些文件）。只有 `find_references` 用它 —— 那个
        # 工具的问题是「这个标识符本批次里还有谁在用」，而「本批次」正是 scope 知道、
        # 别处都不知道的事（见 `ai/scope.py::batch_paths`）。不传时为 None：那几处
        # 调用方（测试、探测）本来也不会用到这个工具。
        self._scope = scope
        self._manifest = manifest
        # 「向 Agent 取正文」的会话内记忆（见 `_content_from_agent`）：同一个进程里同一份
        # 请求只等一次。放在实例上而不是模块级 —— 一次分析一个 provider，跨分析复用会把
        # 上一份分析的结果喂给下一次。
        self._agent_content_fetched: set = set()
        self._agent_content_cached: dict = {}
        self._agent_content_pending: dict = {}
        # 「向 Agent 要 diff」的同一套记忆（见 `_diff_from_agent`）：一条分析里同一个文件的
        # diff 常常被问两次（正文挑窗口时一次、模型自己再要一次），等待不该付两遍。
        self._agent_diff_fetched: set = set()
        self._agent_diff_cached: dict = {}
        self._agent_diff_pending: dict = {}
        # 「一次检索只做一次」的记忆（见 `_search_local`）：建索引是要真读文件的，
        # 模型对同一个词问两遍不该让节点把同一件事做两遍。
        self._search_cache: dict = {}
        # 快照索引（读一次整批、查询只查内存）。`_reference_index_key` 是建它时那份
        # 文件清单的指纹 —— 换了批次就重建，绝不把上一批的索引当成这一批的。
        #
        # **没有「检索额度」这种东西了**：`SearchBudget` 曾经假装是一个闸门（累加 used、
        # 比 remaining），但生产代码里没有任何地方读 `remaining` —— 唯一的效果是让
        # 第 2、3 次检索能搜多少文件取决于前几次「动过几个文件」，而两条部署路径
        # （本地 / 问 Agent）扣的数还不一样。真正管住成本的是索引的预热门槛
        # （`MAX_INDEX_FILES`）与「读一次、查询不再读」，不是那个计数器。
        self._reference_index = None
        self._reference_index_key = ""
        # 取数侧的一页上限。计划推导出来的值由 `ContextTools` 在构造时交进来
        # （`apply_tool_limits`）；没交时用初始值 —— 它是**页大小**，不是硬上限：
        # 超过一页的正文可以按 `next_cursor` 继续要。
        self._content_max_chars = int(content_max_chars or DEFAULT_CONTENT_MAX_CHARS)
        # 冻结仓库的只读范围（工作包 D 的 P1）。`frozen_repository` 显式给了就用它（测试、
        # 或者将来由调用方在派发前定好版本）；没给就**惰性**从本批次解析 ——
        # 理由见 `_resolved_readers` 的 docstring。
        #
        # 可以给**一个**，也可以给**一组**：本项目有多个仓库时逐一冻结（见
        # `_batch_repositories`），显式指定这条路同样按组收。
        self._frozen_declared = _as_frozen_tuple(frozen_repository)
        self._repo_git_service = repo_git_service
        # 仓库范围检索一次索引多少个文件（上限，不是目标）。给了就覆盖模块初值——
        # 调用方（或测试）据此把「一页多大」与运行预算对上。
        self._repo_index_files = repo_index_files
        self._frozen_reader: FrozenTreeReader | None = None
        # 本次冻结的**全部**仓库的读取器（本项目全部已接入仓库）。`_frozen_reader` 是旧的
        # 单仓读法（第一个），只留给「表头坐标」这类不需要多仓的调用点。
        self._frozen_readers: tuple[FrozenTreeReader, ...] = ()
        self._frozen_resolved = False
        self._frozen_reason = ""
        # 读侧对「冻结范围」的一次性结论（跟踪树清单）。同一个 provider 里复用，
        # 免得每轮索取都列一次树。
        self._repo_scope = None
        # 读平台已算好并落库的那一份（周版本合并 diff，页面同源），而不是现场重算。
        #
        # **默认开**：这条来源是「评审者看到的 diff」本身，而现场重算在
        # platform/agent 模式下必然读不到（平台被禁止 clone）。默认关掉它，
        # 就等于让「代码文件的 diff 取不到」这件事随时可以再发生一次。
        #
        # 关掉的只有一种场合：**单提交分析**。那时模型问的是「这一条提交改了什么」，
        # 而缓存里那一份覆盖的是一个窗口（可能好几条提交）—— 见 `_batch_provenance`。
        self._use_stored_batch_diff = bool(use_stored_batch_diff)

    # -- 文档 ---------------------------------------------------------------

    def read_reference(self, name: str) -> Optional[str]:
        """读 skill 文档。**只能读白名单里的** —— 白名单由 `AnalysisScope` 把着，
        这里再确认一次路径确实来自加载结果，而不是模型拼出来的相对路径。"""
        if str(name or "").strip() == "change-manifest" and self._manifest is not None:
            from services.ai.manifest import render_manifest_reference

            return render_manifest_reference(self._manifest)
        target = self._loaded.readable.get(str(name or ""))
        if target is None:
            return None
        path = Path(target)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            log_print(f"⚠️ AI 取数：读文档失败 {name}: {exc}")
            return None

    # -- 冻结仓库的只读范围（工作包 D 的 P1） -------------------------------

    def apply_tool_limits(self, limits: Mapping[str, int] | None) -> None:
        """把**本轮真正生效的单条上限**交给取数侧（页大小）。

        ## 为什么要有这个入口

        `file_content` 的正文在取数侧就被切（切在行边界上、抬头写着「这是第几段」），
        所以取数侧的上限**才是**给模型看到的那一页的大小。计划（`budget_plan`）已经按
        提示词预算推导出 `file_content` 该给多少，如果取数侧还按出厂常量 11,000 切，
        计划里那个数就是一句给不出来的话 —— 这正是工作包 D 点名的「隐藏截断」：
        用户把预算调高，正文仍然先被 11,000 夹住，而**界面上看不出来**。

        `ContextTools` 在构造时把 `limits` 交进来（它有那一份生效值）。
        非正数忽略：0 会让取数侧切出一段空正文，那是比不生效更糟的结果。
        """
        try:
            value = int((limits or {}).get("file_content") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            self._content_max_chars = value

    def _batch_repository(self):
        """本批次属于哪个仓库。**只给回执抬头用**（表头坐标、仓库归属那一行）。

        冻结范围不要再用它 —— 那是 `_batch_repositories()` 的事，见那里的说明。
        """
        ids: set = set()
        if self._scope is not None:
            for value in (self._scope.repository_ids_by_commit or {}).values():
                ids.update(int(item) for item in value or ())
        if ids:
            try:
                from models import Repository

                rows = (
                    Repository.query
                    .filter(Repository.id.in_(sorted(ids)))
                    .order_by(Repository.id.asc())
                    .all()
                )
                if rows:
                    return rows[0]
            except Exception as exc:  # noqa: BLE001 —— 查不动就退回下面那条
                log_print(f"⚠️ AI 取数：查本批次仓库失败：{exc}")
        if self._scope is not None:
            pairs = entries_for(self._scope.batch_paths(), self._scope.commit_of_path)
            return self._repository_of(pairs)
        return None

    def _batch_repositories(self):
        """**要冻结的全部仓库**：本批次各仓库所属项目下的全部已接入仓库。

        ## 为什么不是「本批次涉及的那几个」

        一次周版本分析覆盖的仓库可以不止一个（配置仓库 + 代码仓库同窗是常态），而窗口里
        被改动的往往只是其中一个 —— 核查「改了公共接口，调用方改没改」要读的恰恰是
        **没被改动**的那个仓库里的文件。只冻结本批次涉及的那几个，仍然会把「改的是配置表、
        要看代码仓库里的读取方」这类核查挡在门外。

        所以范围取「本项目全部已接入仓库的只读 Git 跟踪内容」——判据仍然是本项目，没有
        放宽到任意文件（路径形状与凭证排除两条判据在多仓之前照旧各判一次）。

        实测 run 57：本批次涉及 `qz_config`(1) 与 `qz_luaworkspace代码`(2)，而
        `_batch_repository()` 按 id 升序只冻结了 1，于是 4 条 `code/qz_*` 的文件全被
        判成「不在本次冻结版本的 Git 跟踪文件里」。

        查不到项目（老 payload、手工构造的 scope、单提交模式）时**退回本批次那几个仓库**，
        与加这一层之前的行为一致。
        """
        try:
            from models import Repository

            batch_ids: set = set()
            if self._scope is not None:
                for value in (self._scope.repository_ids_by_commit or {}).values():
                    batch_ids.update(int(item) for item in value or ())
            rows = ()
            if batch_ids:
                rows = (
                    Repository.query
                    .filter(Repository.id.in_(sorted(batch_ids)))
                    .order_by(Repository.id.asc())
                    .all()
                )
            project_ids = {getattr(row, "project_id", None) for row in rows}
            project_ids.discard(None)
            if project_ids:
                return tuple(
                    Repository.query
                    .filter(Repository.project_id.in_(sorted(project_ids)))
                    .order_by(Repository.id.asc())
                    .all()
                )
            if rows:
                return tuple(rows)
        except Exception as exc:  # noqa: BLE001 —— 查不动就退回下面那条
            log_print(f"⚠️ AI 取数：查本项目仓库失败：{exc}")
        single = self._batch_repository()
        return (single,) if single is not None else ()

    def _resolved_readers(self) -> tuple[tuple[FrozenTreeReader, ...], str]:
        """本次可用的**冻结读取器（一组）**。第二个返回值是拿不到的原因。

        为什么懒解析：provider 的构造点在 `services/ai_analysis_service.py`，那里拿不到
        仓库对象；而「本批次属于哪些仓库」这一层本来就知道。第一次真正需要仓库范围时解析
        一次，之后连同结论（包括「不可用」）一起记住 —— 每轮索取都去问一遍 git 是白花时间。
        """
        if self._frozen_resolved:
            return self._frozen_readers, self._frozen_reason
        self._frozen_resolved = True
        frozen = self._frozen_declared
        if frozen:
            self._frozen_readers = tuple(FrozenTreeReader(item) for item in frozen)
            self._frozen_reason = ""
            return self._frozen_readers, ""
        commits: tuple = ()
        if self._scope is not None:
            commits = tuple(self._scope.commits)
        frozens, note = resolve_frozen_repositories(
            self._batch_repositories(),
            scope_commits=commits,
            git_service_for=(
                (lambda repository: self._repo_git_service)
                if self._repo_git_service is not None
                else None
            ),
        )
        if not frozens:
            self._frozen_reason = note or "本批次里没有解析到仓库"
            log_print(
                f"🔍 AI 取数：本次没有冻结仓库的只读范围（{self._frozen_reason}）——"
                "引用检索退回「只搜本批次改动的文件」",
                "AI",
            )
            return (), self._frozen_reason
        self._frozen_readers = tuple(FrozenTreeReader(item) for item in frozens)
        self._frozen_reason = ""
        if note:
            # 部分仓库没冻结成功**照样可读剩下的那些** —— 但要说出来，否则「这个文件读不到」
            # 会被读成「它不存在」。
            log_print(f"⚠️ AI 取数：有仓库没能冻结（{note}），其余仓库照常可读", "AI")
        return self._frozen_readers, ""

    def _resolved_reader(self) -> tuple[FrozenTreeReader | None, str]:
        """第一个冻结读取器（旧读法）。**多仓判据一律用 `_resolved_readers()`。**"""
        readers, reason = self._resolved_readers()
        return (readers[0] if readers else None), reason

    def repo_read_scope(self):
        """本次分析可读的冻结范围（`frozen_repo.RepoReadScope`）；拿不到返回 `None`。

        协议层（`sanitize_requests`）拿它的 `paths` 判「这个路径能不能读」，而真正读的
        那一层仍然自己再判一次（**绝不因为协议层放行就信任**）。
        """
        if self._repo_scope is None:
            readers, _reason = self._resolved_readers()
            self._repo_scope = build_read_scope_multi(
                tuple(reader.frozen for reader in readers)
            )
        return self._repo_scope

    def repo_tracked_paths(self):
        """给协议层用的跟踪路径集合（**全部已冻结仓库的并集**）；不可用时返回 `None`。

        「空集合」会让协议层把每一条仓库范围的请求都判成越权（理由还会说「不在跟踪树
        里」—— 一句假话）；`None` 让它退回本批次那一套判据。
        """
        scope = self.repo_read_scope()
        if scope is None or not scope.available:
            return None
        return scope.paths

    def _content_outside_batch(self, path: str, lines: str) -> Optional[str]:
        """读**不在本批次**的那个文件（`file_content` 的冻结版本分支）。

        判定顺序是刻意的（与 `frozen_repo` 的分工一致）：先纯路径判据 → 再凭证排除 →
        再问「在不在某个冻结仓库的跟踪树里」→ 最后才读。**每一步的拒绝都要给理由**，
        否则模型会把它读成「平台取数失败」并写进信息缺口。

        ## 同一条相对路径在多个仓库里都有时**不猜**

        `config/item.xlsx` 这种路径在两个仓库里同时存在是可能的，而两个版本的内容与含义
        都可能不同 —— 猜一个等于把另一个仓库的内容当成这个仓库的交给模型。所以这时给一份
        **可操作**的拒绝：列出候选仓库，并说明怎么指定（`repository_id`，或给一条改过它的
        提交）。

        没有冻结范围时返回 `None` —— 与「本批次里没有这个路径」在此之前的语义**逐字
        相同**（那是这条路加进来之前的全部行为）。
        """
        readers, reason = self._resolved_readers()
        if not readers:
            return None
        normalized, reject = resolve_repo_path(path)
        if reject:
            return reject_text(str(path or ""), reject)
        blocked = exclusion_reason(normalized)
        if blocked:
            return reject_text(normalized, blocked)
        scope = self.repo_read_scope()
        candidates = scope.candidates(normalized)
        if len(candidates) > 1:
            return ambiguous_path_text(scope, normalized)
        reader = reader_for_repository(
            readers, candidates[0] if candidates else None
        ) or readers[0]
        tip = str(getattr(reader.frozen, "tip", "") or "")
        tracked = reader.is_tracked(normalized)
        if tracked is None:
            return (
                f"[无法检索] `{normalized}`：列不出本次冻结版本（{tip[:12]}）的跟踪文件 ——"
                "对象库里读不到它。**这不等于「这个文件不存在」**，"
                "需要它时请在报告里写成信息缺口。"
            )
        if not tracked:
            return not_tracked_text(scope, normalized)
        data, why = reader.read_within_limit(normalized)
        if data is None:
            return (
                f"[读不到正文] `{normalized}`@{tip[:12]}：{why}。"
                "**这不等于「没有内容」，也不等于「没有改动」**。"
            )
        return self._render_frozen_content(
            data, path=normalized, lines=lines, tip=tip, reader=reader
        )

    def _repository_row(self, repository_id: Any):
        """按 id 取仓库行（取不到返回 `None`）。给「这一份是从哪个仓库读的」用。"""
        if repository_id is None:
            return None
        try:
            from models import Repository

            return Repository.query.filter_by(id=repository_id).first()
        except Exception as exc:  # noqa: BLE001 —— 查不到只是少一个表头坐标来源
            log_print(f"⚠️ AI 取数：查仓库 {repository_id} 失败：{exc}")
            return None

    def _describe_repository(self, repository_id: Any) -> str:
        """候选仓库给人看的名字（`编号（名字）`）。名字取冻结对象自带的那一份 —— 不再查库
        （见 `frozen_repo.repository_label`）。"""
        reader = reader_for_repository(self._frozen_readers, repository_id)
        frozen = getattr(reader, "frozen", None) if reader is not None else None
        return repository_label(repository_id, getattr(frozen, "name", ""))

    def _render_frozen_content(
        self,
        data: bytes,
        *,
        path: str,
        lines: str,
        tip: str,
        reader: FrozenTreeReader | None = None,
    ) -> str:
        """把冻结版本上读到的字节渲染成给模型看的正文（各类型走**与本地同一条**渲染）。

        出处那一行**必须有**：同一个路径在本批次里也可能被读到过（那条走的是该文件
        自己那条提交的内容），不说清「这一份读的是哪个版本」，模型会把两处的内容
        混成一份 —— 而它接下来正是要拿这两份做「调用方改了没有」的对比。

        多仓之后出处还要写**哪个仓库**：同一条相对路径在两个仓库里都存在时，只写
        `路径@tip` 仍然分不清是哪一份。

        `reader` 是**读到这一份的那个**仓库的读取器（配表的表头坐标要按它那个仓库的
        配置算）；不传时退回第一个仓库 —— 只在单仓调用点发生。
        """
        frozen = getattr(reader, "frozen", None)
        repository = None
        if frozen is not None:
            repository = self._repository_row(getattr(frozen, "repository_id", None))
        if repository is None:
            repository = self._batch_repository()
        name = str(getattr(frozen, "name", "") or "")
        where = f"{path}@{tip[:12]}"
        origin = f"冻结版本 {tip[:12]} 上的内容" + (f"，仓库 {name}" if name else "")
        if _is_openpyxl_workbook(path):
            rendered = _read_excel_sheets(
                data,
                max_rows=self._max_rows,
                path=path,
                window=lines,
                char_budget=max(1, self._content_max_chars - len(_AGENT_EXCEL_NOTE)),
                **header_kwargs_for(
                    repository, path, raw=data, only=("header_rows", "header_name_row")
                ),
            )
            if rendered is None:
                return (
                    f"[配表解析失败] {where}：内容无法解析成文本表格。**这不等于「没有内容」**。"
                )
            return f"（出处：{origin}，不是工作副本）\n{rendered}"
        if is_docx(path):
            rendered = render_docx_text(data, path=path)
            if rendered is None:
                return (
                    f"[文档解析失败] {where}：内容无法解析成文本（不是可读的 OOXML 文档）。"
                    "**这不等于「没有内容」**。"
                )
            return self._render_text_page(
                rendered, path=where, lines=lines, auto=False, frozen_tip=tip
            )
        text = text_or_notice(data)
        if text is None:
            return binary_content_notice(where)
        return self._render_text_page(
            text, path=where, lines=lines, auto=False, frozen_tip=tip
        )

    def _render_text_page(
        self,
        text: str,
        *,
        path: str,
        lines: str = "",
        auto: bool = False,
        frozen_tip: str = "",
    ) -> str:
        """文本/代码正文的一页：**按本轮生效的页大小切**，并把出处写进抬头。

        这是 `_render_text_content` 的取数侧入口：唯一的差别是额度来自
        `self._content_max_chars`（计划推导出来的那个数，见 `apply_tool_limits`），
        于是「计划里写 30,333、正文只给 11,000」这一类对不上的账在结构上不可能出现。
        """
        origin = ""
        if frozen_tip:
            origin = (
                f"；内容来自**冻结版本 {frozen_tip[:12]}**"
                "（不是工作副本，也不是本批次那条提交上的内容）"
            )
        return _render_text_content(
            text,
            path=path,
            lines=lines,
            auto=auto,
            limit=self._content_max_chars,
            origin=origin,
        )

    # -- 提交 ---------------------------------------------------------------

    def commit_detail(self, commit: str) -> Optional[str]:
        """提交信息 + 改动文件清单。

        直接读平台的 `commits_log`，**不碰 GitService**：那边要先有本地检出，可能触发
        clone，而这里需要的每个字段库里都有。
        """
        rows = self._commit_rows(commit)
        if not rows:
            return None

        head = rows[0]
        lines = [
            f"提交 {commit}",
            f"- 提交信息：{(head.message or '').strip() or '（无）'}",
            f"- 作者：{head.author or '（未知）'}",
            f"- 时间：{head.commit_time.isoformat() if head.commit_time else '（未知）'}",
            f"- 改动文件（{len(rows)} 个）：",
        ]
        for row in rows:
            lines.append(f"  - [{row.operation or 'M'}] {row.path}")
        return "\n".join(lines) + "\n"

    def file_diff(self, commit: str, path: str, *, ask_agent: bool = True) -> Optional[str]:
        """某个文件在某个提交上的 diff（Excel 走结构化差异）。

        来源按顺序三条，前一条给不出**真差异**才走后面一条：

        1. **平台已经算好并落库的那一份**（周版本合并 diff，就是周版本页面读的同一个
           payload，见 `_weekly_stored_diff`）；单提交分析刻意关掉它（`use_stored_batch_diff=False`）
           —— 那时模型问的是「这一条提交改了什么」，窗口合并的那份会张冠李戴；
        2. **现场重算**（`resolve_previous_commit` + `get_unified_diff_data`）—— 需要平台本地
           有这个仓库的工作副本；
        3. **业务节点上的 Agent 现算**（`_diff_from_agent`，platform/agent 模式）。

        第 3 条是 2026-09-19 补上的：前两条在 platform/agent 模式下**都不可能**（平台被禁止
        clone、代码文件又没有单文件缓存），于是 `file_diff` 交出去的是一句「取数失败」——
        模型据此写「未读到任何代码 diff」，而真正算得出来的那台机器根本没被问过。
        前两条都不成立时**不再把失败说明当成答案**，去问 Agent；问到就问到了，问不到才
        带着原因如实回报（同 `file_content` 那条路）。

        `ask_agent=False` 只给「顺手看一眼本地那份补丁」的调用方用（`_default_window` 挑
        窗口位置）：它不要那句十几秒的等待，也不该为了挑个坐标派一个取数任务出去。
        """
        row = self._commit_row(commit, path)
        if row is None:
            return None

        text, failed = self._local_file_diff(row, commit, path)
        if not failed or not ask_agent:
            return text

        # 本地这一份是失败说明。只有多节点部署才去问 Agent：单机模式下本地就是**唯一**
        # 的取数点，那里算不出来的原因（这个提交里没有这个路径、仓库类型不支持…）
        # 换台机器算也一样。
        repository = getattr(row, "repository", None)
        if repository is None or not is_agent_dispatch_mode():
            return text
        return self._diff_from_agent(repository, commit, path, fallback=text or "")

    def _local_file_diff(self, row, commit: str, path: str):
        """本地两条来源给出的文本，以及**它是不是失败说明**（返回 `(文本, 失败)`）。

        为什么要有那个布尔值：失败说明是一句完整的话（`[取数失败] … 这不等于「没有改动」`），
        与真差异在调用方眼里长得一样 —— 没有它，「本地算不出来」就只能是终点。

        顺序：**先看增量基线**（有它才是「上一轮之后新增的那一段」），再退回平台已落库的
        整窗口合并 diff，最后才现场重算。取数失败时逐级放宽并各自带上出处说明 ——
        「退回了更宽的那一份」必须写在给模型的文本里，否则它会把更早的改动说成这次的。
        """
        delta = self._delta_diff(row, commit, path)
        if delta is not None:
            return delta

        if self._use_stored_batch_diff:
            stored, provenance = _weekly_stored_diff(
                getattr(row, "repository_id", None),
                path,
                commit,
                row_id=self._cache_row_for(row, commit, path),
            )
            if stored is not None and not _is_failed_payload(stored):
                rendered = render_diff_payload(
                    stored, path=path, max_rows_per_sheet=self._max_rows
                )
                if rendered:
                    # 出处说明只在这个窗口合并了多条提交时才有内容（见 `_batch_provenance`）
                    # —— 模型拿到的索取形状是「提交 X 改了什么」，不说明它会张冠李戴。
                    return (f"{provenance}\n{rendered}" if provenance else rendered), False
                # 落回实时计算：这份缓存载荷渲染不出来（例如旧口径的分段结构），
                # 而实时那条路算出来的东西至少是能读的。
                log_print(f"⚠️ AI 取数：已落库的 diff 渲染不出来，改用实时计算 {path}")
            elif stored is not None:
                # 落库的那一份本身就是失败（同步时就没算出来）。它渲染出来是一句
                # 「取数失败」，当成答案交给模型等于告诉它「这里没什么可看的」。
                log_print(f"⚠️ AI 取数：已落库的 diff 是失败载荷，改用实时计算 {path}")

        try:
            from services.commit_diff_logic import resolve_previous_commit
            from services.vcs_content_service import get_unified_diff_data

            previous = resolve_previous_commit(row)
            payload = get_unified_diff_data(row, previous)
        except Exception as exc:  # noqa: BLE001 —— 取数失败只该让这一条降级
            log_print(f"⚠️ AI 取数：diff 失败 {commit[:12]} {path}: {type(exc).__name__}: {exc}")
            return None, True

        rendered = render_diff_payload(payload, path=path, max_rows_per_sheet=self._max_rows)
        # 渲染不出来（认不出的结构）与失败载荷（读不到内容）都算「没拿到真差异」——
        # 两者都要让调用方去问 Agent，而`rendered` 仍然返回：问不到时它就是如实的原因。
        return rendered, rendered is None or _is_failed_payload(payload)

    def _delta_diff(self, row, commit: str, path: str):
        """增量的那一段：`上一轮的 latest → 这一轮的 latest`。

        返回 `None` 表示「这一轮不走这条路」（没有基线 —— 新文件 / 补偿项 / 全量分析），
        调用方照旧给整窗口那一份。**取数失败也返回 `None`**：那是「退回了更宽的那一份」，
        而更宽那一份自带 `_batch_provenance` 的出处说明，读的人知道它覆盖了整个批次。
        这里绝不返回一句「没有改动」——那份载荷是空的，不等于这个文件没变。
        """
        key = (getattr(row, "repository_id", None), str(commit), str(path))
        base_commit_id = self._delta_bases.get(key)
        if not base_commit_id and key[0] is not None:
            # 仅兼容没有 repository_id 的旧快照/手工构造载荷。生产快照始终带仓库 ID；
            # 两个真实仓库因此不会退化为只按 SVN 修订号和路径匹配。
            base_commit_id = self._delta_bases.get((None, key[1], key[2]))
        if not base_commit_id:
            return None
        try:
            from types import SimpleNamespace

            from services.vcs_content_service import get_unified_diff_data

            # 虚拟基线：`get_unified_diff_data` 只从它身上读 `commit_id`
            # （同 `commit_diff_logic` 里那处假基线，属性集由
            # `tests/test_commit_diff_virtual_baseline.py` 钉着）。
            previous = SimpleNamespace(
                commit_id=str(base_commit_id), path=path,
                repository_id=getattr(row, "repository_id", None),
            )
            payload = get_unified_diff_data(row, previous)
        except Exception as exc:  # noqa: BLE001 —— 取数失败只该让这一条降级
            log_print(
                f"⚠️ AI 取数：增量区间 diff 失败 {commit[:12]} {path}: "
                f"{type(exc).__name__}: {exc}（退回整窗口那一份）"
            )
            return None

        rendered = render_diff_payload(payload, path=path, max_rows_per_sheet=self._max_rows)
        if not rendered or _is_failed_payload(payload):
            return None
        return (f"{_delta_provenance(base_commit_id, commit)}\n{rendered}", False)

    def file_content(self, commit: str, path: str, lines: str = "") -> Optional[str]:
        """某个文件在这个提交上的正文（默认只给**一段窗口**，见下）。

        ## 正文从哪来：按部署模式两条来源

        * **平台本地有工作副本**（单机模式）→ `get_file_content_from_git`（沿用既有路径）；
        * **platform/agent 模式** → 平台被禁止 clone，正文只有业务节点上的 Agent 取得回来
          （`services/agent_file_content_dispatch.py`）。**读得到就用，读不到就说清为什么**
          （绑没绑 Agent、在不在线、是不是还在路上）。

        ## 为什么按窗口给，而不是整份文件

        模型问正文是为了看**改动周围**长什么样（改动本身在 `file_diff` 里）。整份几千行的
        lua 塞进上下文，付的是三份代价：跨节点传输、库里留一份、以及最贵的 —— 提示词里挤进
        几千行与本次判断无关的代码（单条上限 11,000 字符，而从中间截断的正文等于没有上下文）。

        **没点名时窗口放在改动附近**（`_default_window`：从已渲染的补丁里读块头的新版本侧
        行号），而不是文件开头那 400 行 —— 改动在第 2200 行时，开头那段与它毫无关系，模型
        要么白花一次索取额度再问一次，要么据此得出错误结论。挑不到改动位置（补丁拿不到、
        认不出块头）就退回文件开头，并在抬头里说明这一段是怎么来的：**不说来源等于给了一个
        看不出对错的坐标**。

        `lines` 对**所有**文件都有意义，只是单位不同：文本/代码是行号，**配表是「第几张
        工作表」**（`"2"` / `"2-3"`）。配表按张而不是按行，是因为它的正文是「每张表各自的
        统计 + 前若干行」，根本没有「整个文件的第几行」这个坐标（Agent 端那条路还显式标了
        `kind: "excel"` 并注明不要再按行号包一层）。不点名时配表从第 1 张开始尽量多给，
        给不下的在抬头里逐张列出并写明怎么要 —— 配表原先**没有**补救路径（`lines` 被完全
        忽略），于是它成了唯一一个「被截断了也问不回来」的内容形态。
        """
        row = self._commit_row(commit, path)
        if row is None:
            # 不在本批次里。**这不再是终点**（工作包 D 的 P1）：模型可能在别处看到过这个
            # 路径（例如它在引用检索的命中里读到了一个调用方），而「读不到」会让它把
            # 「调用方改了没有」写成信息缺口。这里改走**冻结版本**的只读范围。
            return self._content_outside_batch(path, lines)
        repository = getattr(row, "repository", None)
        if repository is None:
            return None

        # `auto` 只在**真的按改动位置挑到了窗口**时为真：挑不到就退回文件开头，那时不能说
        # 「这一段是按改动位置选的」—— 抬头里的每句话模型都会当成事实用。
        #
        # 配表不进这里：它们的「窗口」是**第几张工作表**，与行号不是一回事，为它多读一次
        # diff 去挑行号是白读（挑出来的行号对配表没有落点）。
        auto = False
        if not str(lines or "").strip() and not _is_openpyxl_workbook(path) and not is_docx(path):
            lines = self._default_window(commit, path)
            auto = bool(lines)

        # 配表的正文额度要**先扣掉平台自己会补的那一行出处**（见 `_AGENT_EXCEL_NOTE`）。
        # 单机与多节点用同一个值，两端渲染出来的正文才逐字节相同；而多节点那边补上
        # 出处之后仍然不超上限，`ContextTools` 就不会再砍一刀、也不会误报「被截断」。
        # 非配表路径不扣：那条路给 Agent 的 `max_chars` 量的是**正文**，抬头与每行的
        # `数字│` 前缀由平台这边加，而那边已经有 `_fit_numbered_content` 按整串重算。
        content_budget = self._content_max_chars
        if _is_openpyxl_workbook(path):
            content_budget = max(1, content_budget - len(_AGENT_EXCEL_NOTE))

        try:
            from services.vcs_content_service import get_file_content_from_git

            raw = get_file_content_from_git(repository, commit, path)
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：内容失败 {commit[:12]} {path}: {type(exc).__name__}: {exc}")
            raw = None

        if raw is None:
            # 平台本地读不到 —— platform/agent 模式下这是**常态**（平台被禁止 clone），
            # 所以不要就此放弃：正文在业务节点上，让 Agent 取回来。
            return self._content_from_agent(
                repository, commit, path, lines, auto=auto, max_chars=content_budget
            )

        if isinstance(raw, str):
            return self._render_text_page(raw, path=path, lines=lines, auto=auto)
        if not raw:
            # 取到了、长度为零：这是「确实没有内容」，按契约返回空串。
            return ""

        # 判定用 openpyxl 的实际能力，而不是平台的「配表」清单：见 _WORKBOOK_EXTENSIONS
        # 上方注释（`.csv`/`.tsv` 走文本分支才能把完整内容交给模型，`.xls`/`.xlsb`
        # openpyxl 也读不了）。这里原先 import 的 `services.excel_cache_service` 并不存在，
        # 且 import 在 try 之外 → 任何非空内容都会抛 ModuleNotFoundError。
        if _is_openpyxl_workbook(path):
            rendered = _read_excel_sheets(
                raw,
                max_rows=self._max_rows,
                path=path,
                window=lines,
                # 与发给 Agent 的是**同一个值**（见上面 `content_budget` 那段）：
                # 两端渲染出来的正文必须逐字节相同。
                char_budget=content_budget,
                # 表头坐标从**仓库配置**来（与 diff 引擎同一套口径、同一个收口函数）：
                # 列名取哪一行、表头块占几行，决定了正文里哪些行算数据。
                #
                # `only` 只要两个坐标：`marker_column`（标记列）的语义是「diff 时不算
                # 数据变更」，而这里是**给模型看内容** —— 把备注列从正文里抹掉只会让
                # 它少看到东西。所以 AI 正文这一侧不消费标记列。
                **header_kwargs_for(repository, path, raw=raw,
                                    only=("header_rows", "header_name_row")),
            )
            if rendered is None:
                return (
                    f"[配表解析失败] {path}：内容无法解析成文本表格。"
                    "**这不等于「没有内容」**，需要核对时请说明该表无法读取。"
                )
            return rendered
        # `.docx`：与配表同理先分流（它是 ZIP，下面那句 `text_or_notice` 会把它判成二进制）。
        # 渲染成文本行之后走**同一条**文本路径（`_render_text_content`）—— 窗口、行号、
        # 截断提示与其他文本文件一模一样，模型不必学第二套坐标。
        if is_docx(path):
            rendered = render_docx_text(raw, path=path)
            if rendered is None:
                return (
                    f"[文档解析失败] {path}：内容无法解析成文本（不是可读的 OOXML 文档，"
                    "或文件已损坏）。**这不等于「没有内容」**。"
                )
            return self._render_text_page(rendered, path=path, lines=lines, auto=auto)

        # 文本/代码：解码走 `utils.text_decoding`（**两端唯一一份实现**）。
        #
        # 原先这里是 `raw.decode("utf-8")` 严格解码、失败就回「[无法展示的内容] …不是文本…」
        # —— 那是一条**假的信息缺口**：GBK 的 lua（本仓库最常见的形态之一）读得到，
        # 模型却被告知读不到，于是把「读得到但没解码」写成「这里没有内容」。
        # 而 Agent 侧同一串字节走的是 utf-8 + `errors='replace'`（乱码），
        # 于是同一次索取在单机与多节点下给出**两份不同的文本**。
        #
        # 现在两端都调 `text_or_notice`：能解出文本就给文本（五级编码兜底），
        # 只有**真正的二进制**（魔数 / NUL，见 `looks_binary`）才发那句话 ——
        # 那句话的模板也在 `utils.text_decoding`，两端的措辞逐字相同。
        text = text_or_notice(raw)
        if text is None:
            return binary_content_notice(path)
        return self._render_text_page(text, path=path, lines=lines, auto=auto)

    def _default_window(self, commit: str, path: str) -> str:
        """模型没点名时，把窗口放在这个文件**本次改动**的位置上（返回 `"a-b"`）。

        行号取自这个文件在这个提交里的补丁块头 —— 走的是 `file_diff` 那条路，在周版本分析里
        它就是平台已落库的那一份（一次库读），补丁里的 `+1180` 也正是**当前版本**的行号，
        与 `file_content` 给的正文是同一套坐标。

        拿不到补丁、或补丁里没有块头（新增/删除整个文件、二进制、认不出的载荷）就返回空串：
        调用方会退回「文件开头那一段」，并在抬头里说明这一段是怎么来的 —— **不说来源，
        模型就无法判断这个坐标对不对**。

        **这条路上不向 Agent 要 diff**（`ask_agent=False`）：这只是挑一个窗口位置，而向业务
        节点要一次要等十几秒（`AGENT_FETCH_WAIT_SECONDS`）—— 为一次启发式多等一轮往返，
        代价远大于它买到的准确度。挑不到就退回文件开头，抬头会写明这一段是怎么来的。
        """
        try:
            diff_text = self.file_diff(commit, path, ask_agent=False)
        except Exception as exc:  # noqa: BLE001 —— 挑窗口失败不该影响取正文
            log_print(
                f"⚠️ AI 取数：定位改动位置失败 {commit[:12]} {path}: {type(exc).__name__}: {exc}"
            )
            return ""
        return _window_around_changes(
            _changed_line_starts(diff_text or ""), span=DEFAULT_WINDOW_LINES
        )

    def _content_from_agent(
        self,
        repository,
        commit: str,
        path: str,
        lines: str,
        *,
        auto: bool = False,
        max_chars: Optional[int] = None,
    ) -> Optional[str]:
        """向业务节点（Agent）要一份正文。**同一个进程里同一份请求只等一次。**

        为什么要在实例上记「已经取过」：模型常对同一个文件问两次（后面那次是为了引用），
        而等待是有代价的（`FILE_CONTENT_WAIT_SECONDS`）。记下来之后第二次直接命中；
        已经派出去、这次没等到的也不再重复等 —— 答案还是同一句「还在路上」。
        """
        from services.agent_file_content_dispatch import (
            header_config_fingerprint,
            request_file_content,
        )

        # 表头坐标也进这把 key：它决定配表的列名取哪一行，坐标变了就是另一份正文
        # （与派发层 `_matches` 的判据同一口径，见 `header_config_fingerprint`）。
        # 不带上它的话，一次分析中途改了仓库配置，第二次索取会拿到按旧坐标渲染的正文 ——
        # 而报告里看不出任何异样。非配表路径这两个值恒为未配置，指纹也就是个常数。
        #
        # 坐标**按这个文件**解析（与渲染那处同一个收口函数）—— 取仓库标量的话，
        # 指纹反映的就不是实际渲染用的那一组坐标，「坐标变了指纹没变」于是变成
        # 一次静默的缓存命中。
        #
        # 这里**不传 probe**（这一步拿不到文件字节）：所以「表头特征」规则不进指纹。
        # 影响面很小 —— 靠判据选中的方案只在**文件内容**变化时才可能翻转，而那种情况下
        # `commit` 已经变了、key 本来就不命中。
        header_profile = resolve_for_file(repository, path)
        header_config = header_config_fingerprint(
            header_profile.header_rows, header_profile.header_name_row
        )
        key = (getattr(repository, "id", None), commit, path, lines, header_config)
        if key in self._agent_content_fetched:
            cached = self._agent_content_cached.get(key)
            if cached is not None:
                return cached
            return self._agent_content_pending.get(key)
        self._agent_content_fetched.add(key)

        try:
            outcome = request_file_content(
                repository,
                commit_id=commit,
                file_path=path,
                lines=lines,
                max_chars=self._content_max_chars if max_chars is None else max_chars,
            )
        except Exception as exc:  # noqa: BLE001 —— 取一次正文失败只该让这一条降级
            log_print(f"⚠️ AI 取数：向 Agent 取正文失败 {path}: {type(exc).__name__}: {exc}")
            outcome = {"status": "unavailable", "message": f"向 Agent 取数异常：{exc}"}

        status = str(outcome.get("status") or "")
        if status == "ready":
            rendered = _render_agent_file_content(
                outcome, path=path, auto=auto, limit=self._content_max_chars
            )
            self._agent_content_cached[key] = rendered
            return rendered

        # `pending`（还在路上）与 `unavailable`（取不了）都要如实说，且都要**明确否掉**
        # 「读不到 = 没有改动」这个读法（本模块存在的理由）。
        reason = str(outcome.get("message") or "原因未知")
        if status == "pending":
            text = (
                f"[{path}] 正文还没取回来：{reason}。**这不等于「没有内容」**；"
                "这一轮请基于 diff 与其它证据判断。"
            )
        else:
            text = (
                f"[读不到正文] {path}：{reason}。**这不等于「没有内容」，也不等于「没有改动」**"
                "—— 需要这个文件的正文时，请在报告里写成信息缺口。"
            )
        self._agent_content_pending[key] = text
        return text

    def _diff_from_agent(self, repository, commit: str, path: str, *, fallback: str) -> str:
        """向业务节点（Agent）要「**这一条提交**改了这个文件的什么」。

        与 `_content_from_agent` 同一套路数（会话内同一份请求只等一次、`pending` 与
        `unavailable` 都要如实说），两处措辞不同是因为**模型接下来该做什么不一样**：
        正文取不到时它还能靠 diff 判断；差异取不到时它手里就只剩提交清单了 —— 那正是
        那句「未读到任何代码 diff」的来处，所以这句话必须明确指向「写成信息缺口」。

        `fallback` 是本地两条来源给出的失败说明（平台读不到内容、这个提交里没有这个路径…）：
        问不到 Agent 时原样带着它回给模型 —— **两个原因都要在**，只留一个会把另一半
        藏起来（例如「平台读不到」会让人去查工作副本，而真正该做的是给项目绑一个 Agent）。
        """
        from services.agent_file_content_dispatch import request_file_diff

        key = (getattr(repository, "id", None), commit, path)
        if key in self._agent_diff_fetched:
            cached = self._agent_diff_cached.get(key)
            return cached if cached is not None else self._agent_diff_pending.get(key, fallback)
        self._agent_diff_fetched.add(key)

        try:
            outcome = request_file_diff(repository, commit_id=commit, file_path=path)
        except Exception as exc:  # noqa: BLE001 —— 取一次差异失败只该让这一条降级
            log_print(f"⚠️ AI 取数：向 Agent 取差异失败 {path}: {type(exc).__name__}: {exc}")
            outcome = {"status": "unavailable", "message": f"向 Agent 取数异常：{exc}"}

        status = str(outcome.get("status") or "")
        if status == "ready":
            rendered = _render_agent_file_diff(outcome, path=path, commit=commit)
            self._agent_diff_cached[key] = rendered
            return rendered

        reason = str(outcome.get("message") or "原因未知")
        what = "差异还没取回来" if status == "pending" else "读不到差异"
        note = (
            f"[{what}] {path}：{reason}。**这不等于「没有改动」。**"
            "这一轮请只依据提交清单与其它证据判断，并在报告里把它写成信息缺口。"
        )
        text = f"{fallback}\n{note}" if fallback else note
        self._agent_diff_pending[key] = text
        return text

    # -- 内部 ---------------------------------------------------------------

    # -- 检索 ---------------------------------------------------------------

    def find_references(self, query: str, path: str = "") -> Optional[str]:
        """找这个标识符还有哪些出现位置。

        ## 范围（工作包 D 的 P1，2026-09-24 起）

        默认搜 **本次仓库冻结 tip 的 Git 跟踪文件**（`frozen_repo` + `repo_reference`），
        不再是「只搜本批次改动过的文件」。理由：小 diff 恰好改了公共接口时，只搜本批次
        会让「调用方改了没有」**既证实不了也证伪不了**，而 run 45/46 正是把这条写成了
        信息缺口 —— 继续加 `file_diff` 次数解决不了它。

        冻结范围拿不到时（没有本地工作副本、platform/agent 模式、不是 git 仓库）退回原来
        那条路（本批次 / 问 Agent），并**在结果里显式写明范围只有本批次** ——
        「搜不到」与「没搜那么宽」在模型那里必须分得开。

        ## 前缀只影响 `search()`，不影响索引

        给出的 `path` 是**查询范围内**的前缀（`batch_paths()` 按它过滤过的那份列表曾经
        直接进了索引 —— 那个 bug 见 `_search_local`）。现在传给索引的是**未过滤的整批**，
        `prefix` 交给 `search()`：于是「先问 `scripts/`、再问全局」的第二次查询仍然看得见
        整批，覆盖率的分母与命中都不会被上一次查询的范围污染。
        """
        search = normalize_query(query)
        if not search:
            return None

        repo_text = self._search_frozen_repo(search, normalize_path(path))
        if repo_text is not None:
            return repo_text

        if self._scope is None:
            return (
                "[检索不可用] 这次分析没有把「本批次改动了哪些文件」传给取数层，"
                "读不到范围。请在报告里把这条写成信息缺口，不要据此下结论。"
            )

        prefix = normalize_path(path)
        # **整批**（未过滤）：索引与前缀无关，前缀只在查询那一刻生效。
        pairs = entries_for(self._scope.batch_paths(), self._scope.commit_of_path)
        # 覆盖率的分母：本批次里**落在这个前缀范围内**的文件数（与本地那条路
        # `index.search(prefix=...)` 算出来的 `files_total` 是同一个数）。
        in_scope = sum(1 for item, _commit in pairs if not prefix or item.startswith(prefix))
        local = self._search_local(pairs, search, prefix=prefix)
        if local is not None:
            return self._with_narrow_scope_note(local)
        return self._with_narrow_scope_note(
            self._search_from_agent(pairs, search, prefix=prefix, total_files=in_scope)
        )

    def _search_frozen_repo(self, query: str, prefix: str) -> Optional[str]:
        """冻结版本的仓库范围检索（**每个仓库各搜一次**，见 `search_frozen_repositories`）。

        **不可用时返回 `None`** —— 调用方退回「只搜本批次」那条路，并补一句范围声明
        （「搜不到」与「没搜那么宽」在模型那里必须分得开）。
        """
        readers, _reason = self._resolved_readers()
        if not readers:
            return None
        extra = (
            {"max_files": int(self._repo_index_files)}
            if self._repo_index_files
            else {}
        )
        return search_frozen_repositories(readers, query, prefix=prefix, **extra)

    def _narrow_scope_note(self) -> str:
        """退回「只搜本批次」时补在结果前面的一句**范围声明**（拿得到范围时为空串）。

        没有它，模型会把「本批次里没有命中」读成「仓库里没有引用」—— 而这两句话的证据
        强度差着一个数量级，报告里的结论也因此分叉。
        """
        _reader, reason = self._resolved_reader()
        if _reader is not None:
            return ""
        return (
            f"[范围说明] 本次**没有**仓库冻结范围可用（{reason}），"
            "所以下面这次检索**只覆盖本批次改动过的文件**，"
            "未改动过的文件（例如别的模块里的调用方）不在里面。\n"
            "**「没有命中」只代表本批次里没有**，不能说成「仓库里没有引用」。\n\n"
        )

    def _with_narrow_scope_note(self, text: str) -> str:
        """给结果补上范围声明。**失败说明原样返回**。

        ## 为什么失败时不能补

        `[检索不到]` / `[检索还没回来]` 这几句是**取数层的失败说明**，而记账那一层
        （`trace_evidence.failure_notice` 与 `context_tools` 的 `failed` 计数）按**开头**
        认它们：在它们前面插一段话，这条失败就会被记成「成功取到内容」——
        同一个面板上「失败 0 条」与明细里列出的失败条数于是对不上，而后者被当成
        「取数都很顺」。

        代价是这种情况下范围声明缺席 —— 可以接受：那几句失败说明自己就带着
        「**这不等于「没有其它引用」**，请写成信息缺口」，而「一条都没搜成」比
        「只搜了本批次」是更强的限定。
        """
        note = self._narrow_scope_note()
        if not note:
            return text
        if failure_notice(text):
            return text
        return note + text

    def _search_local(
        self,
        pairs: Sequence[tuple[str, str]],
        query: str,
        *,
        prefix: str,
    ) -> Optional[str]:
        """平台本地的工作副本。**单机模式**走这条；多节点模式返回 `None`（改问 Agent）。

        与 `_content_from_agent` 同一套记忆：同一个进程里同一份检索只做一次
        （模型可能对同一个词问两遍，而建索引是要真读文件的）。

        ## 索引按**快照**缓存，按**前缀**过滤

        索引只建一次（一次分析里批次是固定的），但**不能用带前缀的那份文件列表建** ——
        前缀是**这次查询**的范围，不是批次的范围。拿它建索引会永久污染：
        「先问 `path='scripts/'`，再问全局」时第二次查询只能看见 `scripts/` 下的文件，
        而它报出来的 `files_total` / `scanned` / 命中全都是那个子集的，
        **模型看不出这是上次查询的残留**（它只会读到一句「覆盖了 12/12 个文件」）。

        所以：`pairs` 永远是整批，索引建在整批上，前缀进 `search()` 时再过滤；
        缓存键里放**快照指纹**做校验（同一批 = 同一个索引，换了批次就重建）。
        """
        from services.vcs_content_service import get_file_content_from_git

        key = (query, prefix)
        if key in self._search_cache:
            return self._search_cache[key]
        # 一次就够的判断：没有本地工作副本时 `get_file_content_from_git` 会返回 None，
        # 于是每个文件都算「读不到」——那会把整批白读一遍，还给出一句
        # 「N 个都读不到」的假话（真相是平台本地根本没有这个仓库）。
        repository = self._repository_of(pairs)
        if repository is None:
            return None
        if is_agent_dispatch_mode():
            # 多节点模式下平台被禁止 clone：本地读不到是**确定**的，别去读一遍。
            return None

        def reader(path: str, commit: str):
            return get_file_content_from_git(repository, commit, path)

        from services.ai.reference_index import (
            MAX_INDEX_FILES,
            SnapshotReferenceIndex,
            snapshot_digest,
        )

        digest = snapshot_digest(pairs)
        if self._reference_index is None or self._reference_index_key != digest:
            # 预热门槛（`MAX_INDEX_FILES`）：整批顺序读 blob 会把首次查询从 240 个文件拖到
            # 767 个，而 Agent 侧等检索只有 40 秒 —— 门槛之外的那些**如实算成缺口**
            # （`search()` 报 `unindexed`，抬头写「文件数到了上限就停了，剩下的没搜」）。
            self._reference_index = SnapshotReferenceIndex.build(
                pairs, reader=reader, max_files=MAX_INDEX_FILES
            )
            self._reference_index_key = digest
        result = self._reference_index.search(query, prefix=prefix)
        text = render_result(
            result,
            scope_note=(
                f"索引版本 `{result.index_version}`，快照 `{result.snapshot_digest[:12]}`；"
                f"索引覆盖本批次的前 {self._reference_index.indexed_files} 个文件，"
                "后续查询不重复读取 blob。"
            ),
        )
        self._search_cache[key] = text
        return text

    def _search_from_agent(
        self,
        pairs: Sequence[tuple[str, str]],
        query: str,
        *,
        prefix: str,
        total_files: int,
    ) -> Optional[str]:
        """业务节点上的 Agent 拿它自己的工作副本搜（platform/agent 模式的唯一取数点）。"""
        from services.agent_file_content_dispatch import request_references

        repository = self._repository_of(pairs)
        if repository is None:
            return (
                "[检索不到] 本批次的改动文件里找不到对应的仓库，无法确定去哪个节点上搜。"
            )
        try:
            outcome = request_references(
                repository,
                query=query,
                # **整批**：Agent 也按快照建一次索引，前缀交给它自己的 `search()`。
                # 只发前缀命中的那一批会让 Agent 的索引缓存按前缀分叉 —— 同一个 bug
                # 换个进程再犯一次（第二次查询看到的还是第一个前缀的范围）。
                entries=pairs,
                prefix=prefix,
                # 覆盖率的分母：**本批次里落在这个前缀范围内的文件数**（与本地那条路
                # `index.search(prefix=...)` 算出来的 `files_total` 一致）。两条路对分母的
                # 口径必须一样，否则同一个周版本在单机与多节点下印出互相矛盾的覆盖率，
                # 而模型正是拿这个数决定能不能说「没有其它引用」。
                total_files=total_files,
            )
        except Exception as exc:  # noqa: BLE001 —— 取一次检索失败只该让这一条降级
            log_print(f"⚠️ AI 取数：向 Agent 检索失败 {query}: {type(exc).__name__}: {exc}")
            return f"[检索不到] `{query}`：向 Agent 检索时出错（{exc}）。"

        status = str(outcome.get("status") or "")
        if status == "ready":
            rendered = str(outcome.get("text") or "")
            if rendered:
                return rendered
        reason = str(outcome.get("message") or "原因未知")
        # 两句的尾巴逐字相同 —— 用常量而不是抄两遍：`trace_evidence` 按**开头**认这几句，
        # 而两条分支各写一份尾巴的那天起，「还没回来」与「读不到」的原因就会开始漂移。
        tail = (
            "。**这不等于「没有其它引用」。**"
            "这一轮请只依据已取得的证据判断，并在报告里把它写成信息缺口。"
        )
        if status == "pending":
            return f"[检索还没回来] `{query}`：{reason}" + tail
        return f"[检索不到] `{query}`：{reason}" + tail

    def _repository_of(self, pairs: Sequence[tuple[str, str]]):
        """这批路径属于哪个仓库。取第一条能查出提交行的路径的仓库。

        跨仓库的批次（一个周版本关联多个仓库）只有第一个仓库会被检索 —— 这是**已知的
        局限**，写在这里而不是装作没有：结果文本里的「范围内共 N 个文件」是按这个仓库
        算的，不会把别的仓库的文件算进去。<!-- 后续可按仓库分组各派一次任务 -->
        """
        for path, commit in pairs:
            row = self._commit_row(commit, path)
            repository = getattr(row, "repository", None)
            if repository is not None:
                return repository
        return None

    def _cache_row_for(self, row, commit: str, path: str) -> Optional[int]:
        """这条 delta 来自哪一行周版本缓存（REV-AI-003）。取不到就返回 `None`。

        键与 `_delta_bases` 同形（带仓库优先，缺仓库时退回不带仓库的旧键）。
        """
        repository_id = getattr(row, "repository_id", None)
        found = self._cache_row_ids.get((repository_id, str(commit), str(path)))
        if found is None and repository_id is not None:
            found = self._cache_row_ids.get((None, str(commit), str(path)))
        return found

    def _repositories_for(self, commit: str) -> tuple[int, ...]:
        """本批次里带这个提交号的仓库。空元组 = **不知道**，调用方不收窄（REV-AI-001）。

        `commits_log` 是**跨仓库共用**的一张表，而 SVN 修订号只在单个仓库内唯一 ——
        不带仓库维度查它，会按 `id` 取到另一个仓库（甚至另一个项目）那一行。
        """
        if self._scope is None:
            return ()
        return tuple(self._scope.repository_ids_by_commit.get(str(commit), ()))

    def _commit_row(self, commit: str, path: str):
        from models import Commit, db

        try:
            query = Commit.query.filter_by(commit_id=commit, path=path)
            repositories = self._repositories_for(commit)
            if repositories:
                query = query.filter(Commit.repository_id.in_(repositories))
            return query.order_by(Commit.id.desc()).first()
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：查提交行失败 {commit[:12]}: {exc}")
            _ = db
            return None

    def _commit_rows(self, commit: str):
        from models import Commit

        try:
            query = Commit.query.filter_by(commit_id=commit)
            repositories = self._repositories_for(commit)
            if repositories:
                query = query.filter(Commit.repository_id.in_(repositories))
            return query.order_by(Commit.id.asc()).all()
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：查提交失败 {commit[:12]}: {exc}")
            return []
