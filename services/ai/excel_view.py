#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配表差异的**渲染叶子**：把一张工作表的结构翻译成模型读得懂的文本行。

## 为什么单独一个文件

`services/ai/platform_provider.py` 贴着仓库的长度闸门（`scripts/check_file_length.py --strict`
在 2000 行报错）。这一组函数是那一层里**最纯**的部分：入参是平台已经算好的结构化 dict
（`{'type': 'excel', 'sheets': {…}}` 里的单个 sheet），出参是 `list[str]`，不碰文件、
不碰数据库、不认识额度与契约。搬出来之后它既能被完整单测，也让「取数」与「排版」两件事
各自有个说得清的边界。

## 谁在用

* `platform_provider._render_excel`（表格级抬头与整体统计留在那边）调 `_render_sheet`；
* `services/ai/excel_source.py` 的 `_read_excel_sheets` 调 `_cell` —— 因此这个模块
  **不许** import `excel_source`（否则成环）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# 单元格值的展示上限，防止某个字段里塞了一整段文本把行撑爆。
_MAX_CELL_CHARS = 160

_ROW_LABELS = {"added": "新增", "removed": "删除", "modified": "修改", "unchanged": "未变"}

# 表头块里「有改动」的三种状态（`unchanged` 是常驻行，见 `_render_header_block`）。
# 与 `utils/diff_data_utils.header_rows_have_changes`、前端模块的 `CHANGED_ROW_STATUSES`
# 是同一个判据的三个副本（三种语言各一份，改动时一起改）。
_HEADER_CHANGED_STATUSES = ("added", "removed", "modified")
# 表头块最多列几行。表头行就那么几行，单独给一个小上限即可。
_MAX_HEADER_ROWS = 20


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
