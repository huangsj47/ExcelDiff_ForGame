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

import io
import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from services.ai.budget import truncate_text
from services.ai.reference_search import (
    MAX_SCAN_FILES,
    SearchBudget,
    entries_for,
    normalize_query,
    render_result,
    search_files,
)
from services.ai.scope import AnalysisScope, normalize_path
from services.ai.skill_loader import LoadedSkills
from services.deployment_mode import is_agent_dispatch_mode
from utils.content_window import CONTENT_MAX_CHARS, DEFAULT_WINDOW_LINES, slice_lines
from utils.logger import log_print

# 每个工作表最多渲染多少行。**不设上限会把一个几千行的表整个塞进上下文**，而单条上限
# 会从中间截断，模型看到的是一张中间缺一块的表。这里按行裁剪并如实记账。
DEFAULT_MAX_ROWS_PER_SHEET = 120
# 单元格值的展示上限，防止某个字段里塞了一整段文本把行撑爆。
_MAX_CELL_CHARS = 160

_ROW_LABELS = {"added": "新增", "removed": "删除", "modified": "修改", "unchanged": "未变"}

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

# 单次 `file_content` 的正文上限（字符）。**与预算层的单条上限是同一个数**
# （`utils.content_window.CONTENT_MAX_CHARS`，`ContextTools` 也引用它）：
#
# 取值比它小没有好处（额度就在那里，不用就浪费了），比它大则是**净损失** ——
# 取数侧按自己的上限切好、写好「第 a–b 行 / 共 N 行」的抬头之后，预算层还会再按
# 自己的上限砍一次尾巴：砍在半行中间，抬头说的行数也就成了假的。
DEFAULT_CONTENT_MAX_CHARS = CONTENT_MAX_CHARS

# 表头块里「有改动」的三种状态（`unchanged` 是常驻行，见 `_render_header_block`）。
# 与 `utils/diff_data_utils.header_rows_have_changes`、前端模块的 `CHANGED_ROW_STATUSES`
# 是同一个判据的三个副本（三种语言各一份，改动时一起改）。
_HEADER_CHANGED_STATUSES = ("added", "removed", "modified")
# 表头块最多列几行。表头行就那么几行，单独给一个小上限即可。
_MAX_HEADER_ROWS = 20


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


def _render_sheet(name: str, sheet: Any, *, max_rows: int) -> list[str]:
    if not isinstance(sheet, Mapping):
        return [f"### 工作表「{name}」", "- （结构无法识别）"]

    head = [f"### 工作表「{name}」"]
    operation = str(sheet.get("operation") or "").strip()
    # `status: deleted` 是 `git_service._deleted_sheet_diff` 的写法（它**带着 rows**），
    # `operation: deleted` 是 `diff_service` 的写法（也带着 rows）——两种都要认。
    deleted = operation == "deleted" or _row_status(sheet) == "deleted"
    if deleted:
        # **不能在这里就 return。** 删除工作表时「删掉了什么」是这条差异的全部内容：
        # 平台两边都把行留着（`_deleted_sheet_diff` 的注释：「这是评审者唯一能看到
        # 『到底删掉了什么』的地方，不能为了省体积只留一个计数」），页面上看得到，
        # 只有 AI 这条路原先把它丢了。下面按普通分支把 rows / stats 渲染出来。
        head.append("- **该工作表已被删除**。")
    elif operation == "added":
        head.append("- 该工作表是新增的。")
    if sheet.get("error"):
        return [head[0], f"- 解析失败：{sheet.get('message') or sheet.get('error')}"]

    rows = sheet.get("rows") or []
    header_lines = _render_header_block(sheet)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        if deleted:
            # 平台只说了「删了这张表」而没有行 —— 那句话本身照旧是全部信息。
            return head
        if header_lines:
            # 「只改了表头」的提交：rows 是空的，但表头行真的变了 ——
            # 这里说「没有差异行」等于让 AI 告诉评审者这次提交什么都没改。
            return head + header_lines
        return head + ["- 没有差异行。"]
    if deleted:
        head[-1] = (
            f"- **该工作表已被删除**，下面是它被删除前的内容"
            f"（{len(rows)} 行，每一行都是删除行）。"
        )

    # 优先展示有变化的行：整表几千行时，未变的行是纯噪音。
    changed = [row for row in rows if _row_status(row) not in ("", "unchanged")]
    shown = changed or list(rows)

    stats = sheet.get("stats") or {}
    if stats:
        head.append(
            f"- 本次：新增 {stats.get('added', 0)}、删除 {stats.get('removed', 0)}、"
            f"修改 {stats.get('modified', 0)}（表内共 {len(rows)} 行差异记录）。"
        )
    # 表头行紧跟在统计之后、数据行之前 —— 与页面上的位置一致（表头块在最上面）。
    head.extend(header_lines)

    for row in shown[:max_rows]:
        rendered = _render_row(row)
        if rendered:
            head.append(rendered)
    if len(shown) > max_rows:
        head.append(
            f"- （另有 {len(shown) - max_rows} 行差异未列出，需要时请针对具体行索取。）"
        )
    return head


def _render_header_block(sheet: Mapping) -> list:
    """表头块（物理第 2..N 行，见 `services/diff_service.py::_build_header_rows`）。

    与数据行同一个口径：**只列有改动的表头行** —— 块里含未改动的行（页面拿它们说明
    「表头共 3 行」），但那些进提示词只是噪音。

    必须列出来：表头行的改动**不在 `rows` 里**，不列的话「这次提交只改了表头」
    在 AI 眼里就是「没有差异行」，它会如实告诉评审者这次提交什么都没改。
    """
    rows = sheet.get("header_rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    changed = [row for row in rows if _row_status(row) in _HEADER_CHANGED_STATUSES]
    if not changed:
        return []
    lines = [f"- 表头 {len(rows)} 行中有 {len(changed)} 处改动。"]
    for row in changed[:_MAX_HEADER_ROWS]:
        rendered = _render_row(row)
        if rendered:
            lines.append(rendered)
    return lines


def _row_status(row: Any) -> str:
    if not isinstance(row, Mapping):
        return ""
    return str(row.get("status") or "").strip().lower()


def _render_row(row: Any) -> str:
    if not isinstance(row, Mapping):
        return ""
    status = _ROW_LABELS.get(_row_status(row), _row_status(row) or "变更")
    number = row.get("row_number")
    prefix = f"- [{status}]" + (f" 第 {number} 行：" if number is not None else " ")
    # 修改行还要说清它在**上一版本**里是第几行：插/删一行之后两版行号会不同
    # （实测 12 ↔ 11），只说一个数会让模型（和读报告的人）按错的行号去定位。
    previous_number = row.get("previous_row_number")
    if previous_number is not None and str(previous_number) != str(number):
        prefix = f"- [{status}] 第 {number} 行（上一版第 {previous_number} 行）："

    cell_changes = row.get("cell_changes") or []
    if cell_changes:
        parts = []
        for change in cell_changes:
            if not isinstance(change, Mapping):
                continue
            column = change.get("column")
            parts.append(
                f"{column}: {_cell(change.get('old_value'))} → {_cell(change.get('new_value'))}"
            )
        if parts:
            return prefix + "；".join(parts)

    data = row.get("data")
    if isinstance(data, Mapping):
        return prefix + "{" + ", ".join(f"{key}={_cell(value)}" for key, value in data.items()) + "}"

    cells = row.get("cells")
    if isinstance(cells, Sequence) and not isinstance(cells, (str, bytes)):
        return prefix + " ｜ ".join(_cell_entry(cell) for cell in cells)

    return prefix + "（该行有变更，但没有可展示的字段明细）"


def _cell_entry(cell: Any) -> str:
    """`cells` 数组里的一项。**这一列有两种形状，都是平台真实产出的。**

    * **裸值**：早期写法，也还有分支在用；
    * `{'value', 'status'}` / `{'value', 'old_value', 'new_value', 'status'}`：
      `services/git_service.py` 里三个产出点（新增工作表、**删除工作表**、修改行）
      用的都是这个形状，它的 docstring 也写明「模板与前端读的就是这个形状」。

    第二种原先落到 `_cell()` 上、被 `str()` 成一段 Python 字典字面量
    （`{'value': 100001, 'status': 'removed'}`）—— 模型读到的是「一个字典」，
    而不是那个格子里的一千零一。删除工作表那条路上，这几行是**判断「删掉的编号
    有没有已经放出去」的唯一依据**，读成字典字面量就等于没读到。
    """
    if not isinstance(cell, Mapping):
        return _cell(cell)
    if "old_value" in cell or "new_value" in cell:
        return f"{_cell(cell.get('old_value'))} → {_cell(cell.get('new_value'))}"
    return _cell(cell.get("value"))


def _cell(value: Any) -> str:
    """单元格值的展示形式。

    `None` 与空串**都写成 `（空）`**，但要与「字段不存在」区分开：这里只处理前两者，
    所以不能把 `None` 渲染成 `null` 之外的东西——`None` 在配表里通常就是「这个格子
    是空的」，而空格子被改成有值是**一类真实的缺陷**（漏配、错行）。
    """
    if value is None:
        return "（空）"
    text = str(value)
    if not text.strip():
        return "（空）"
    text = text.replace("\n", " ").strip()
    if len(text) > _MAX_CELL_CHARS:
        return text[:_MAX_CELL_CHARS] + "…"
    return text


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


def _render_text_content(text: str, *, path: str, lines: str = "", auto: bool = False) -> str:
    """文本/代码正文：**带行号的一段窗口** + 「这是哪一段」的抬头。

    为什么带行号：模型写进结论里的定位（「第 1180 行那个判断」）必须能被人复核，而补丁里的
    `@@ -1180,7 +1180,9 @@` 也是行号 —— 两边用同一套坐标，模型才能把正文与改动对上。
    行号只占 `数字│` 这几列（比「第 N 行：」省得多），几千行也只多几千字符。

    `auto=True` 表示这一段是**平台按改动位置挑的**（模型没点名）。要说出来：一段从第 1700 行
    开始的正文，不说来源就像随机截的，模型会以为这就是文件的开头。
    """
    window = slice_lines(text, lines, max_chars=DEFAULT_CONTENT_MAX_CHARS)
    where = path or ""
    if window.total_lines == 0:
        return f"[{where}] 这个文件在当前版本里是空的（0 行）。"
    head = f"文件正文：{where}（共 {window.total_lines} 行；下面是第 {window.start_line}–{window.end_line} 行"
    if window.is_partial():
        head += "，**不是全文**"
    head += "）"
    if auto:
        head += "；这一段是按本次改动的位置自动选的（要看别处请指定 lines）"
    numbered = [
        f"{number}│{line}"
        for number, line in enumerate(
            window.content.split("\n"), start=window.start_line
        )
    ]
    body = "\n".join(numbered)
    if window.truncated:
        body += f"\n（这段在第 {window.end_line} 行被截断；需要更多请指定 lines，例如 \"{window.end_line + 1}-{window.end_line + 120}\"）"
    return f"{head}\n{body}"


def _render_agent_file_content(
    outcome: Mapping[str, Any], *, path: str, auto: bool = False
) -> str:
    """业务节点取回来的正文（它自带「哪一段 / 共多少行」，行号由这里补上）。

    **配表例外**：`kind == "excel"` 时 Agent 回的已经是渲染好的工作表文本（抬头 + 整表统计 +
    工作表的各行），与平台本地那条路是**同一个渲染函数**。那条路不按行切、也没有「第 a–b 行」
    这个概念，所以这里必须原样返回 —— 再包一层行号会变成「行号套行号」，模型会把每一行
    的工作表正文当成文件的行号去引用。抬头也由渲染函数自己写（它才说得清「共几张表 /
    给了哪几张 / 别的怎么要」），这里只补一句出处。

    代价（照旧）：Agent 端取回时的 `max_chars` 与平台本地的 `_content_max_chars` 必须相等
    （都是 `CONTENT_MAX_CHARS`），否则同一次索取在单机与多节点下会给出不同的文本。
    """
    where = path or str(outcome.get("file_path") or "")
    if str(outcome.get("kind") or "") == "excel":
        content = str(outcome.get("content") or "")
        if not content:
            return f"[配表] {where}：内容无法解析成文本表格。**这不等于「没有内容」**。"
        return f"（配表正文由业务节点（Agent）上的工作副本取出）\n{content}"

    content = str(outcome.get("content") or "")
    total = int(outcome.get("total_lines") or 0)
    start = int(outcome.get("start_line") or 1)
    end = int(outcome.get("end_line") or start)
    if total == 0:
        return f"[{where}] 这个文件在当前版本里是空的（0 行）。"
    head = f"文件正文：{where}（共 {total} 行；下面是第 {start}–{end} 行"
    if bool(outcome.get("truncated")) or start > 1 or end < total:
        head += "，**不是全文**"
    head += "；内容由业务节点（Agent）上的工作副本取出"
    if auto:
        head += "，这一段是按本次改动的位置自动选的（要看别处请指定 lines）"
    head += "）"
    numbered = [
        f"{number}│{line}" for number, line in enumerate(content.split("\n"), start=start)
    ]
    body = "\n".join(numbered)
    if bool(outcome.get("truncated")):
        body += f"\n（这段在第 {end} 行被截断；需要更多请指定 lines，例如 \"{end + 1}-{end + 120}\"）"
    return f"{head}\n{body}"


def _render_agent_file_diff(
    outcome: Mapping[str, Any], *, path: str, commit: str
) -> str:
    """业务节点算回来的「**这一条提交**改了这个文件的什么」。

    正文不用再加工：Agent 侧算完就地渲染（`render_diff_payload` 的同一份实现），这里
    只补一行出处。**出处必须说**——模型的索取形状是「提交 X 改了什么」，而这条路上给它的
    只是那一条提交与前一次提交之间的差异：周版本分析平时拿的是**整个窗口的合并差异**
    （`_weekly_stored_diff`），不说清它会以为这就是本窗口该文件的全部改动。

    截断由 Agent 侧做（`FILE_DIFF_MAX_CHARS`，末尾带截断标记），这里不重复。
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


# --------------------------------------------------------------------------
# 纯文本内容（Excel 渲染成表）
# --------------------------------------------------------------------------

# 能进 `_read_excel_sheets` 的扩展名：判定依据是**解析器的实际能力**，不是平台的配表清单。
#
# `_read_excel_sheets` 用 openpyxl，只认 OOXML 工作簿。平台的配表清单
# （`ExcelDiffCacheService.is_excel_file` = `.xlsx/.xls/.xlsm/.xlsb/.csv`）比它宽，
# 直接拿来会踩两个坑：
#   * `.csv` / `.tsv` 本来是纯文本，文本分支能把**完整内容**交给模型；若按「配表」进
#     表格分支，`load_workbook` 解析失败 → 模型只收到「内容无法解析成文本表格」，
#     等于把读得到的内容说成读不了；
#   * `.xls` / `.xlsb` 是 OLE/二进制，openpyxl 同样不支持，进文本分支会得到显式的
#     「不是文本也不是配表」说明（不会抛异常）。
# 所以这里只放 openpyxl 真正能打开的那几种。
_WORKBOOK_EXTENSIONS = ('.xlsx', '.xlsm', '.xltx', '.xltm')


def _is_openpyxl_workbook(path: str) -> bool:
    """这个路径该按工作簿解析吗（= openpyxl 读得动吗）。"""
    return Path(str(path or '')).suffix.lower() in _WORKBOOK_EXTENSIONS


# 整表统计的规模上限。这些数字决定「统计块」的字符数上界 —— 它要挤在
# `file_content` 的 11,000 字符额度里，而且必须排在正文**之前**（正文按「只砍尾巴」
# 截断，放在后面就会被砍掉）。
_STATS_COLUMNS_LIMIT = 24
_STATS_TOP_VALUES = 3
_STATS_MAX_SAMPLES = 20_000


def _number_text(value: float) -> str:
    """数值的紧凑写法：整数不带 `.0`，浮点不留 `0.30000000000000004` 这种尾巴。"""
    if isinstance(value, bool):  # bool 是 int 的子类，配表里少见但要挡住
        return str(value)
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6g}"


class _SheetStats:
    """一个工作表的整表统计。

    存在的理由：`value_sanity` 维度要求「判断这个值合不合理」，而判断必须有比较基准。
    配表动辄几千行、上百列，**整表塞进提示词是不可能的**（`file_content` 单条上限
    11,000 字符）；只给改动的那一个单元格又等于让模型拿孤零零一个数字猜 ——
    「这个值看起来很大」正是这个平台反复要挡掉的那种「证据」。

    于是这里做一件平台**做得到而模型做不到**的事：把整表读一遍，只把分布交出去。
    **统计是按截断之前的全部行算的**，所以哪怕正文只剩前 200 行，基准依然是完整的全表。
    """

    def __init__(self) -> None:
        self.rows = 0
        self.width = 0
        self._numbers: list[list[float]] = []
        self._texts: list[dict[str, int]] = []
        self._nonempty: list[int] = []
        self._numeric_capped: list[bool] = []

    def _slot(self, index: int) -> int:
        while len(self._numbers) <= index:
            self._numbers.append([])
            self._texts.append({})
            self._nonempty.append(0)
            self._numeric_capped.append(False)
        return index

    def add_row(self, row) -> None:
        """把一行计入统计。**首行由调用方按列名处理，不进这里** —— 表头本身不是数据：

        把 `品质` 这一列表头文字 `品质` 当成一个取值，会让每个文本列都多出一个
        「只出现一次的取值」，模型据此判断「这个值不在允许集合里」时会先撞上它。
        """
        values = list(row)
        if not any(value is not None and str(value).strip() for value in values):
            return  # 整行为空：与正文渲染一致，不计入
        self.rows += 1
        self.width = max(self.width, len(values))
        for index, value in enumerate(values):
            if index >= _STATS_COLUMNS_LIMIT:
                break
            if value is None or not str(value).strip():
                continue
            slot = self._slot(index)
            self._nonempty[slot] += 1
            text = str(value).strip()
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if len(self._numbers[slot]) < _STATS_MAX_SAMPLES:
                    self._numbers[slot].append(float(value))
                else:
                    self._numeric_capped[slot] = True
            if len(self._texts[slot]) < 5_000:
                self._texts[slot][text[:40]] = self._texts[slot].get(text[:40], 0) + 1
            elif text in self._texts[slot]:
                self._texts[slot][text[:40]] += 1

    def render(self, column_labels: Sequence[str]) -> list[str]:
        """渲染成给模型看的几行。`column_labels` 是首行的值（配表通常就是列名）。"""
        if self.rows == 0:
            return []
        head = [
            f"- 整表统计（首行按列名，其余 {self.rows} 行参与统计；与下面只展示前若干行无关。"
            "判断某个值是否合理时用它做比较基准）："
        ]
        lines: list[str] = []
        shown = min(self.width, _STATS_COLUMNS_LIMIT)
        for index in range(shown):
            label = column_labels[index] if index < len(column_labels) else ""
            label = str(label or "").strip()[:20]
            name = f"第 {index + 1} 列" + (f"（首行「{label}」）" if label else "")
            non_empty = self._nonempty[index] if index < len(self._nonempty) else 0
            if non_empty == 0:
                continue
            numbers = self._numbers[index] if index < len(self._numbers) else []
            # 半数以上是数值就按数值列报分布；否则按取值分布报（品质、类型、状态这类
            # 枚举列要看到「有哪些取值、各占多少」，那正是「不在允许集合里」的依据）。
            if numbers and len(numbers) * 2 >= non_empty:
                ordered = sorted(numbers)
                cap = "（抽样上限 20000）" if self._numeric_capped[index] else ""
                lines.append(
                    f"  - {name}：非空 {non_empty}｜数值 {len(numbers)}{cap}｜"
                    f"最小 {_number_text(ordered[0])}｜中位 {_number_text(_percentile(ordered, 0.5))}｜"
                    f"P90 {_number_text(_percentile(ordered, 0.9))}｜最大 {_number_text(ordered[-1])}"
                )
            else:
                buckets = self._texts[index] if index < len(self._texts) else {}
                top = sorted(buckets.items(), key=lambda item: (-item[1], item[0]))
                top_text = "、".join(f"{value}({count})" for value, count in top[:_STATS_TOP_VALUES])
                lines.append(
                    f"  - {name}：非空 {non_empty}｜不同取值 {len(buckets)}｜"
                    + (f"最多：{top_text}" if top_text else "（无文本取值）")
                    + (f"｜其中数值 {len(numbers)}" if numbers and len(numbers) * 2 < non_empty else "")
                )
        if self.width > shown:
            lines.append(f"  - （另有 {self.width - shown} 列未做统计。）")
        return head + lines if lines else []


def _percentile(ordered: Sequence[float], ratio: float) -> float:
    """已排序列表的分位数（线性插值）。"""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = ratio * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def parse_sheet_window(window: str) -> Optional[tuple]:
    """`lines` 在**配表**上表示「第几张工作表」（1 起算的闭区间）。认不出来返回 `None`。

    ## 为什么配表按「张」而不是按「行」

    文本 / 代码的 `lines` 是行号，因为行号是模型在代码里定位的坐标（`@@ -1180,7` 与正文
    行号是同一套）。而配表的正文是**每张表各自的统计 + 前若干行**，根本没有「整个文件的
    第几行」这个概念 —— Agent 端那条路甚至显式标了 `kind: "excel"` 并注明「不要再按行号
    包一层」。所以这里换一个模型说得清、也对得上的坐标：**第几张表**。

    `protocol._normalize_line_window` 已经把 `lines` 规整成 `N` / `N-M` / `""`，这里只做
    「认不认得出」：**认不出就当没给**，与 `utils/content_window.parse_line_window` 同一条
    口径 —— 窗口写坏的代价只能是拿到默认那一段，不能是丢掉整条请求。
    """
    text = str(window or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,7})(?:-(\d{1,7}))?", text)
    if match is None:
        return None
    start = int(match.group(1))
    if start < 1:
        return None
    end = int(match.group(2)) if match.group(2) else start
    if end < start:
        return None
    return start, end


def _read_excel_sheets(
    raw: bytes,
    *,
    max_rows: int,
    path: str = "",
    window: str = "",
    char_budget: int = 0,
) -> Optional[str]:
    """把 xlsx 的字节渲染成文本表格。**自报家门、自我收敛、按工作表可点名。**

    Excel 的「完整内容」本质就是一张表；按二进制拒绝它会让模型完全看不到这张表长什么样。

    ## 排版：所有工作表的**统计**先发，所有**正文**后发

    截断只砍尾巴（`budget.truncate_text`），所以「先统计后正文」是这个函数的立意 ——
    统计是「值合不合理」这类判断唯一拿得到的比较基准，被砍掉就等于这条路径白跑了。

    但这件事故**不能按工作表各排各的**。原先的排版是「表1 统计 → 表1 正文 → 表2 统计 → …」：
    表 1 的正文一多，后面每张表的统计就全在截断线之外了。线上真实的一本书就是这样 ——
    0_常规属性 3,053 行、整份渲染 25,261 字，截到 11,000 之后「2_M scs属性」的统计一个字
    都没到，而「属性叠加方式=4 是否合理」正需要拿它的统计当基准。

    ## 自我收敛：**不把「砍掉一半」留给预算层**

    上面那个修法解决了统计，但正文仍然可能被整段尾截断 —— 而配表**没有补救路径**：
    `file_content` 的 `lines` 对代码是行窗口、对配表原先被完全忽略，所以模型被告知
    「有 1 条上下文因长度上限被截断」，然后什么都做不了。同轮里另外三个会返回长内容的
    工具（`file_diff` / `read_reference` / `commit_detail`）都有「分段 + 点名」。

    所以这个函数**自己按 `char_budget` 收敛**，并把「一共有几张表 / 这次给了哪张 /
    别的怎么要」写在最前面。它交给上层的文本**永远不会超预算** —— 于是配表不再出现
    「因长度上限被截断」，取而代之的是一句模型能照做的指路：要看第 2 张，写 `"lines": "2"`。

    ## 参数

    * `max_rows`：每张表的正文最多给多少行（统计不受它影响，永远覆盖整表）。
    * `path`：写进抬头（与 `_render_text_content` 同一个约定）。
    * `window`：`""` = 从第 1 张开始、尽量多给；`"2"` / `"2-3"` = 点名要第几张。
    * `char_budget`：0 = 不限（小工具与既有单测用）；生产路径传单条上限。
    """
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover —— openpyxl 是平台的既有依赖
        return None

    try:
        book = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 —— 解析失败要变成可读说明，不能炸掉整轮
        log_print(f"⚠️ AI 取数：Excel 内容解析失败 {type(exc).__name__}: {exc}")
        return None

    # 逐表收集：`(表名, 统计行, 正文行, 数据总行数)`。正文只收 `max_rows` 行 —— 统计已经在
    # `stats.add_row` 里过了全部行，正文收多了没用（行窗口不存在），却要为此把几千行
    # 字符串留在内存里。
    collected: list[tuple[str, list[str], list[str], int]] = []
    try:
        for name in book.sheetnames:
            sheet = book[name]
            stats = _SheetStats()
            labels: list[str] = []
            body: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                if not labels and row is not None and any(
                    value is not None and str(value).strip() for value in row
                ):
                    # 首个非空行按**列名**处理：它只当标签，不进统计（表头文字当成取值会
                    # 让每个文本列都多出一个「只出现一次」的值）。
                    labels = ["" if value is None else str(value) for value in row]
                    continue
                stats.add_row(row)
                if len(body) >= max_rows:
                    # 统计要覆盖整表，所以这里不能 break：只停止累积正文。
                    continue
                if row is None or all(value is None for value in row):
                    continue
                body.append("- " + " ｜ ".join(_cell(value) for value in row))
            collected.append((str(name), stats.render(labels), body, stats.rows))
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass

    return _assemble_workbook(
        collected, path=path, window=window, char_budget=char_budget, max_rows=max_rows
    )


# 抬头与「没给的那几张表怎么要」那两行必须留下来的预留：前者是「这是哪一段」的唯一出处，
# 后者是模型唯一能照做的补救动作（见 `_read_excel_sheets`）。
_EXCEL_HEADER_RESERVE = 700
# 统计那一档最多吃掉预算的多大比例。它是**基准**，比正文值钱，所以先给它一份保底额度；
# 但也不能全给它 —— 一张 24 列的宽表统计就要 2,000 字上下，几张表就能把预算吃光。
_EXCEL_STATS_SHARE = 0.5
# 一张表在正文段里**至少**要给到几行，否则这一块不值得占额度（统计本来就覆盖整表了）。
# 低于它就不给这一张的正文，改为在抬头里点名 —— 模型写一句 `"lines": "3"` 就能单独拿到，
# 而「每张表各给一行」既占额度又几乎不含信息。
_EXCEL_MIN_BODY_ROWS = 20


def _block_chars(lines: Sequence[str]) -> int:
    """一组行的字符数（含换行）—— 排版的额度判断只用它。"""
    return sum(len(line) + 1 for line in lines)


def _assemble_workbook(
    collected: Sequence[tuple],
    *,
    path: str,
    window: str,
    char_budget: int,
    max_rows: int,
) -> str:
    """把逐表收集到的内容排成「统计 → 正文」两段，并在预算内**说清缺了什么**。

    单独提出来是因为它是这个渲染里唯一会算错的地方（谁进得去、谁进不去、进去了几张），
    而它只依赖收集结果与一个字符数 —— 可以直接喂数进来验，不必造真 xlsx。
    """
    total_sheets = len(collected)
    limit = max(0, int(char_budget or 0))
    # 统计档的额度：预算的一半（至少留 2,000 —— 低于它连一段真实统计都放不下，那这一档
    # 就没有存在的意义了），并且**扣掉抬头那 700 的预留**：统计把预算吃光、抬头被截掉，
    # 模型就既不知道这是哪份文件、也不知道怎么要剩下的。
    stats_room = 0
    if limit:
        stats_room = max(
            0, min(limit - _EXCEL_HEADER_RESERVE, max(2_000, int(limit * _EXCEL_STATS_SHARE)))
        )

    stats_lines: list[str] = []
    stats_skipped: list[str] = []
    for index, (name, stats, _body, _rows) in enumerate(collected, start=1):
        # 空表（一行数据都没有）`render` 返回空列表 —— 不冒出一段「按 0 行算出」的统计。
        if not stats:
            continue
        block = [f"#### 工作表「{name}」", *stats]
        # 第一张的统计永远给（`stats_lines` 为空时不拦）：一张统计都没有的话，这一档就白设了。
        if limit and stats_lines and _block_chars(stats_lines) + _block_chars(block) > stats_room:
            stats_skipped.append(f"第 {index} 张「{name}」")
            continue
        stats_lines.extend(block)

    lines: list[str] = []
    if stats_lines:
        lines.append(
            "### 整表统计（覆盖每一张工作表的**全部**行；与下面只展示一部分正文无关。"
            "判断某个值是否合理时用它做比较基准）"
        )
        lines.extend(stats_lines)
        if stats_skipped:
            lines.append(
                f"- （另有 {len(stats_skipped)} 张表的统计因篇幅没列：{'、'.join(stats_skipped)}。"
                "它们的正文可以按下面的方法单独索取。）"
            )
        lines.append("")

    # 正文档：点名了就给点名的那些，没点名就从第 1 张开始、尽量多给。
    span = parse_sheet_window(window)
    asked = span is not None
    wanted = (
        list(range(span[0], min(span[1], total_sheets) + 1))
        if span is not None
        else list(range(1, total_sheets + 1))
    )

    # 正文段：每张表一个块。`built` 只装**进得去**的那些，兜底时从尾部整块退。
    #
    # 额度预算里先扣掉 `_EXCEL_HEADER_RESERVE`（抬头 + 那句指路的预留），于是「装不下」
    # 只可能发生在正文段 —— 抬头与指路是整个函数唯一能自我补救的地方，它们必须留下。
    used = _block_chars(lines) + (_EXCEL_HEADER_RESERVE if limit else 0)
    built: list[tuple[int, list[str]]] = []
    for index in wanted:
        name, _stats, rows, total_rows = collected[index - 1]

        def _body_block(take: int, name: str = name, rows: list = rows, total_rows: int = total_rows):
            """这个工作表在「只给前 `take` 行」时的正文块。"""
            chunk = [f"### 工作表「{name}」（数据第 1–{take} 行，共 {total_rows} 行）"]
            chunk.extend(rows[:take])
            if not take:
                chunk.append("- （这张表没有数据行。）")
            elif take < total_rows:
                chunk.append(
                    f"- （正文只展示了前 {take} 行，共 {total_rows} 行；"
                    "上面的整表统计覆盖整表。要看**改动的行**请用 `file_diff`，它按改动行给。）"
                )
            return chunk

        # 这张表的正文**给多少行**由额度说了算，`max_rows` 只是上限之一。
        #
        # 这一步不能省：一行宽表（24 列、每列几十字）就要 200 字上下，`max_rows=120` 的
        # 一块就是两万多字 —— 超过了整条上限。只做「整块进不进得去」的判断，结果是第一块
        # 就把预算吃穿，然后退回 `truncate_text` 砍尾巴 —— 正是这次要消掉的那条死路。
        #
        # 逐次减行而不是一次算出「能放几行」：块里还有抬头与那句「只展示了前 N 行」的说明，
        # 它们随 `take` 变，一次算不准；这里按四分之一收敛，几十行以内必然落到位。
        take = len(rows)
        floor = min(len(rows), _EXCEL_MIN_BODY_ROWS)
        block = _body_block(take)
        while limit and take > floor and used + _block_chars(block) > limit:
            take = max(floor, take - max(1, take // 4))
            block = _body_block(take)
        # 第一块永远给（哪怕只是一行）——「一张表都没给」比「给了一张、其余指路」糟得多。
        # 后面的表如果连 `_EXCEL_MIN_BODY_ROWS` 行都放不下，就不占这份额度：它已经被抬头
        # 点名了，模型写一句 `"lines": "3"` 就能单独拿到它。
        if limit and built and used + _block_chars(block) > limit:
            break
        built.append((index, block))
        used += _block_chars(block) + 1  # +1 是块之间那个空行

    def _render(blocks: Sequence[tuple[int, list[str]]]) -> str:
        """把「抬头 + 统计档 + 正文档」拼成最终文本。抬头里的那张清单由 `blocks` 决定 ——
        兜底退块之后它必须跟着改，否则抬头会说「给了第 3 张」而正文里没有。"""
        shown = [index for index, _chunk in blocks]
        head = [f"配表正文：{path}" if path else "配表正文"]
        head[0] += f"（共 {total_sheets} 张工作表）"
        if shown:
            head[0] += f"；本次给了第 {'、'.join(str(item) for item in shown)} 张的正文"
        elif asked:
            head[0] += (
                f"；你要的第 {span[0]}-{span[1]} 张不存在（这份工作簿一共只有 {total_sheets} 张）"
            )
        else:
            head[0] += "；本次没有给任何一张表的正文"
        missing = [index for index in range(1, total_sheets + 1) if index not in shown]
        if missing:
            example = f"{missing[0]}-{missing[0] + 1}" if len(missing) > 1 else str(missing[0])
            head.append(
                f'要看别的表，在请求里写 "lines": "<第几张表>"（例如 "{example}"）。'
                f"另有 {len(missing)} 张表的正文没给："
                + "、".join(f"第 {index} 张「{collected[index - 1][0]}」" for index in missing)
            )
        elif asked:
            head[0] += "（你点名要的那几张）"
        chunks = [*head, "", *lines]
        if blocks:
            chunks.append(f"### 工作表正文（每张表最多展示前 {max_rows} 行）")
            for _index, block in blocks:
                chunks.extend(block)
                chunks.append("")
        return "\n".join(chunks).rstrip() + "\n"

    text = _render(built)
    # 兜底：预估偏小时**整块往回退**，而不是整段 `truncate_text` —— 后者会砍掉最后那句
    # 「要看别的表怎么写」，那正是配表唯一的重来路径（也正是这次改动要解决的事）。
    while limit and len(text) > limit and len(built) > 1:
        built = built[:-1]
        text = _render(built)
    if limit and len(text) > limit:
        # 只剩一块了还是超（一张表本身就有几万字）：这时只能收它的尾巴，抬头照旧。
        text = truncate_text(text, limit)[0]
    return text


# --------------------------------------------------------------------------
# 平台已经算好的 diff
# --------------------------------------------------------------------------


def _weekly_stored_diff(repository_id: Optional[int], path: str, commit: str):
    """这个文件在这个提交上的、**平台已经算好并落库**的周版本合并 diff。

    返回 `(载荷, 出处说明)`：载荷是渲染层能直接吃的
    `{'type': 'code'|'text'|'excel'|'segmented_diff', …}`，没有就是 `(None, "")`；
    出处说明是给模型看的一行「这是谁的 diff」，只在合并了多条提交时才有内容
    （见下面的「不许张冠李戴」）。

    ## 为什么 AI 要读这一份，而不是自己再算一遍

    1. **它就是评审者在页面上看的那一份。** `WeeklyVersionDiffCache.merged_diff_data`
       是周版本同步（`weekly_version_logic.generate_weekly_merged_diff`）算出来落库的，
       周版本页面的取数（`weekly_version_file_diff_api` → `generate_weekly_git_diff_html`
       / `generate_weekly_excel_merged_diff_html`）读的就是它。让模型读别的来源，
       它给出的结论就没法与人看到的页面对账。

    2. **自己算的那一份是「单提交 vs 前一提交」，在周版本里只是其中一段。**
       把这一份给它，等于把一个文件一周里的改动只看最后一次。

    3. **平台本地往往根本没有工作副本。** platform/agent 模式下平台被显式禁止 clone
       （`get_file_content_from_git` 直接返回 None），而**代码文件的 diff 没有单文件缓存**
       （`DiffCache` / `ExcelDiffCacheService` 都是配表专用的），于是实时路径必然读不到
       任何内容 —— 那时旧渲染层给出的「取到了记录但没有补丁内容」是一句假话，
       模型读到的是「这里没什么可看的」。配表之所以没这个问题，正是因为它的 diff 有缓存。

    ## 为什么按 (仓库, 路径, latest_commit_id) 精确匹配

    变更清单里给模型的每个文件都带着自己的 `latest_commit_id`（就是这一列），模型
    索取时原样传回来 —— 于是「它问的那个提交」与「这条缓存行」是同一个东西，
    不需要再猜「哪一次周版本」。

    ## 不许张冠李戴

    这条缓存覆盖的是**一个窗口**（可能好几条提交），而模型的索取长成
    `file_diff(commit=X, path=p)` —— 一个问「提交 X 改了什么」的形状。窗口里不止一条
    提交时，模型会把整段窗口的改动都算到 X 头上（而它没有任何办法发现）。所以合并了
    多条提交时，出处说明会明写「这是覆盖 N 条提交的合并差异」。

    `diff_version` **不在这里校验**：口径版本决定的是「周版本同步要不要重算」
    （`needs_merged_diff_cache` → `is_merged_diff_cache_current`），读取侧
    （含页面的 Excel 分支 `load_weekly_excel_diff_from_cache`）本来就不看它。
    """
    if not repository_id or not path or not commit:
        return None, ""
    from models.weekly_version import WeeklyVersionDiffCache

    try:
        row = (
            WeeklyVersionDiffCache.query.filter_by(
                repository_id=repository_id, file_path=path, latest_commit_id=commit
            )
            .order_by(WeeklyVersionDiffCache.id.desc())
            .first()
        )
    except Exception as exc:  # noqa: BLE001 —— 取不到只是少一条来源，不该让整次索取失败
        log_print(f"⚠️ AI 取数：查周版本合并 diff 失败 {path}: {type(exc).__name__}: {exc}")
        return None, ""
    if row is None or not row.merged_diff_data:
        return None, ""
    try:
        envelope = json.loads(row.merged_diff_data)
    except (TypeError, ValueError) as exc:
        log_print(f"⚠️ AI 取数：周版本合并 diff 解析失败 {path}: {exc}")
        return None, ""
    if not isinstance(envelope, Mapping):
        return None, ""
    # 外壳是 `generate_merged_diff_data` 的元数据（commit_ids / authors / merge_strategy…），
    # 真正的载荷在 `diff_data` 与 `merged_diff` 里（同一个对象，历史原因各留了一份）。
    for key in ("diff_data", "merged_diff"):
        candidate = envelope.get(key)
        if isinstance(candidate, Mapping) and candidate:
            return candidate, _batch_provenance(envelope)
    return None, ""


def _is_failed_payload(payload: Any) -> bool:
    """这份载荷是「取不到」还是「真的差异」。

    `type == 'error'`（`get_unified_diff_data` 读不到内容时给的）与带 `error` 键的载荷
    （配表侧的读取失败）都是**失败说明**，渲染出来是一句完整的话 —— 它长得像内容，
    所以调用方必须能分辨它：否则「本地算不出来」会悄悄变成「给模型一句取数失败」就结束了，
    而真正能算出来的那台机器（业务节点的 Agent）根本没被问过。
    """
    if not isinstance(payload, Mapping):
        return True
    return bool(str(payload.get("type") or "").strip() == "error" or payload.get("error"))


def _batch_provenance(envelope: Mapping[str, Any]) -> str:
    """这份合并 diff 覆盖了哪些提交 —— 只在**不止一条**时给说明。

    一条提交时它逐字等于「这条提交的差异」，说明只是噪音；两条以上时不说清楚，
    模型会把整段窗口算到它问的那一条提交头上。
    """
    commit_ids = envelope.get("commit_ids")
    ids = [str(item) for item in commit_ids if item] if isinstance(commit_ids, Sequence) \
        and not isinstance(commit_ids, (str, bytes)) else []
    count = len(ids) or int(envelope.get("commits_count") or 0)
    if count <= 1:
        return ""
    if ids:
        scope = f"（{ids[0][:8]} … {ids[-1][:8]}）"
    else:
        scope = ""
    return (
        f"（出处：平台已落库的**合并差异**，覆盖本批次的 {count} 条提交{scope} ——"
        "它是这个文件在本批次里的全部改动，**不是单独某一条提交的改动**。）"
    )


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


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
    ):
        self._loaded = loaded
        self._max_rows = max_rows_per_sheet
        # 本批次的范围（哪些提交、各改了哪些文件）。只有 `find_references` 用它 —— 那个
        # 工具的问题是「这个标识符本批次里还有谁在用」，而「本批次」正是 scope 知道、
        # 别处都不知道的事（见 `ai/scope.py::batch_paths`）。不传时为 None：那几处
        # 调用方（测试、探测）本来也不会用到这个工具。
        self._scope = scope
        # 「检索扫过多少文件」的额度（见 `reference_search.SearchBudget`）。挂在实例上，
        # 所以子代理模式下 N 个成员**共用一份**（它们共用一个 provider）。
        self._search_budget = SearchBudget()
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
        # 「一次检索只做一次」的记忆（见 `_search_local`）：240 个文件是真读的，
        # 模型对同一个词问两遍不该让节点把同一件事做两遍。
        self._search_cache: dict = {}
        self._content_max_chars = DEFAULT_CONTENT_MAX_CHARS
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
        target = self._loaded.readable.get(str(name or ""))
        if target is None:
            return None
        path = Path(target)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            log_print(f"⚠️ AI 取数：读文档失败 {name}: {exc}")
            return None

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
        """
        if self._use_stored_batch_diff:
            stored, provenance = _weekly_stored_diff(
                getattr(row, "repository_id", None), path, commit
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
            return None
        repository = getattr(row, "repository", None)
        if repository is None:
            return None

        # `auto` 只在**真的按改动位置挑到了窗口**时为真：挑不到就退回文件开头，那时不能说
        # 「这一段是按改动位置选的」—— 抬头里的每句话模型都会当成事实用。
        #
        # 配表不进这里：它们的「窗口」是**第几张工作表**，与行号不是一回事，为它多读一次
        # diff 去挑行号是白读（挑出来的行号对配表没有落点）。
        auto = False
        if not str(lines or "").strip() and not _is_openpyxl_workbook(path):
            lines = self._default_window(commit, path)
            auto = bool(lines)

        try:
            from services.vcs_content_service import get_file_content_from_git

            raw = get_file_content_from_git(repository, commit, path)
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：内容失败 {commit[:12]} {path}: {type(exc).__name__}: {exc}")
            raw = None

        if raw is None:
            # 平台本地读不到 —— platform/agent 模式下这是**常态**（平台被禁止 clone），
            # 所以不要就此放弃：正文在业务节点上，让 Agent 取回来。
            return self._content_from_agent(repository, commit, path, lines, auto=auto)

        if isinstance(raw, str):
            return _render_text_content(raw, path=path, lines=lines, auto=auto)
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
                char_budget=self._content_max_chars,
            )
            if rendered is None:
                return (
                    f"[配表] {path}：内容无法解析成文本表格。"
                    "**这不等于「没有内容」**，需要核对时请说明该表无法读取。"
                )
            return rendered
        try:
            return _render_text_content(raw.decode("utf-8"), path=path, lines=lines, auto=auto)
        except UnicodeDecodeError:
            return (
                f"[无法展示的内容] {path}：不是文本也不是配表，无法以文本形式核对。"
                "**这不等于「没有内容」。**"
            )

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
        self, repository, commit: str, path: str, lines: str, *, auto: bool = False
    ) -> Optional[str]:
        """向业务节点（Agent）要一份正文。**同一个进程里同一份请求只等一次。**

        为什么要在实例上记「已经取过」：模型常对同一个文件问两次（后面那次是为了引用），
        而等待是有代价的（`FILE_CONTENT_WAIT_SECONDS`）。记下来之后第二次直接命中；
        已经派出去、这次没等到的也不再重复等 —— 答案还是同一句「还在路上」。
        """
        from services.agent_file_content_dispatch import request_file_content

        key = (getattr(repository, "id", None), commit, path, lines)
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
                max_chars=self._content_max_chars,
            )
        except Exception as exc:  # noqa: BLE001 —— 取一次正文失败只该让这一条降级
            log_print(f"⚠️ AI 取数：向 Agent 取正文失败 {path}: {type(exc).__name__}: {exc}")
            outcome = {"status": "unavailable", "message": f"向 Agent 取数异常：{exc}"}

        status = str(outcome.get("status") or "")
        if status == "ready":
            rendered = _render_agent_file_content(outcome, path=path, auto=auto)
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
        """在本批次改动的文件里找这个标识符的其它出现位置（`ai/reference_search.py`）。

        与 `file_content` / `file_diff` 同一条路数：**平台本地能读就本地读，读不了就问
        Agent**。区别在于它一次要读很多文件，所以两条来源都加了额度与上限，并且**读了
        几个、跳过了几个、有没有被上限截断**都要写进给模型的文本里 ——

        「没搜到」与「没搜完」在模型那里必须分得开：前者可以写进结论，后者只能写成
        信息缺口。少了这几个数，它会用一句「没有其它引用」把一次只扫了 240 个文件的
        搜索说成结论，而线上一个周版本有 767 个文件。
        """
        search = normalize_query(query)
        if not search:
            return None
        if self._scope is None:
            return (
                "[检索不可用] 这次分析没有把「本批次改动了哪些文件」传给取数层，"
                "读不到范围。请在报告里把这条写成信息缺口，不要据此下结论。"
            )

        prefix = normalize_path(path)
        pairs = entries_for(
            [item for item in self._scope.batch_paths() if not prefix or item.startswith(prefix)],
            self._scope.commit_of_path,
        )
        allowance = min(MAX_SCAN_FILES, self._search_budget.remaining)
        if allowance <= 0:
            return (
                "[检索额度用尽] 本次分析里 `find_references` 能扫的文件数已经用完，"
                "这一次没有搜。请改用已知的路径直接索取 diff 或正文。"
            )

        local = self._search_local(pairs, search, prefix=prefix, allowance=allowance)
        if local is not None:
            return local
        return self._search_from_agent(pairs, search, prefix=prefix, allowance=allowance)

    def _search_local(
        self,
        pairs: Sequence[tuple[str, str]],
        query: str,
        *,
        prefix: str,
        allowance: int,
    ) -> Optional[str]:
        """平台本地的工作副本。**单机模式**走这条；多节点模式返回 `None`（改问 Agent）。

        与 `_content_from_agent` 同一套记忆：同一个进程里同一份检索只做一次
        （模型可能对同一个词问两遍，而 240 个文件是要真读的）。
        """
        from services.vcs_content_service import get_file_content_from_git

        key = (query, prefix)
        if key in self._search_cache:
            return self._search_cache[key]
        # 一次就够的判断：没有本地工作副本时 `get_file_content_from_git` 会返回 None，
        # 于是每个文件都算「读不到」——那会把 240 个文件白读一遍，还给出一句
        # 「240 个都读不到」的假话（真相是平台本地根本没有这个仓库）。
        repository = self._repository_of(pairs)
        if repository is None:
            return None
        if is_agent_dispatch_mode():
            # 多节点模式下平台被禁止 clone：本地读不到是**确定**的，别去读 240 次。
            return None

        def reader(path: str, commit: str):
            return get_file_content_from_git(repository, commit, path)

        result = search_files(
            pairs, query, reader=reader, max_files=allowance, prefix=prefix
        )
        self._search_budget.consume(result.scanned + result.binary + result.missing)
        text = render_result(result)
        self._search_cache[key] = text
        return text

    def _search_from_agent(
        self,
        pairs: Sequence[tuple[str, str]],
        query: str,
        *,
        prefix: str,
        allowance: int,
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
                entries=pairs[:allowance],
                prefix=prefix,
                # **本批次的总数，不是 `entries` 的长度**：`entries` 已经截到额度上限，
                # 而覆盖率的分母必须是「这次一共改了多少个文件」。带错了它就会在报告里
                # 写「240/240 全覆盖」，把「没搜到」当成结论 —— 与本地那条路（分母是完整
                # 列表）会给出互相矛盾的两个覆盖率。
                total_files=len(pairs),
            )
        except Exception as exc:  # noqa: BLE001 —— 取一次检索失败只该让这一条降级
            log_print(f"⚠️ AI 取数：向 Agent 检索失败 {query}: {type(exc).__name__}: {exc}")
            return f"[检索不到] `{query}`：向 Agent 检索时出错（{exc}）。"

        status = str(outcome.get("status") or "")
        if status == "ready":
            rendered = str(outcome.get("text") or "")
            if rendered:
                self._search_budget.consume(int(outcome.get("scanned") or 0))
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

    def _commit_row(self, commit: str, path: str):
        from models import Commit, db

        try:
            return (
                Commit.query.filter_by(commit_id=commit, path=path)
                .order_by(Commit.id.desc())
                .first()
            )
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：查提交行失败 {commit[:12]}: {exc}")
            _ = db
            return None

    def _commit_rows(self, commit: str):
        from models import Commit

        try:
            return (
                Commit.query.filter_by(commit_id=commit).order_by(Commit.id.asc()).all()
            )
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：查提交失败 {commit[:12]}: {exc}")
            return []
