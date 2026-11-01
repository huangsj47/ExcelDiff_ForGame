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

import re
from collections import Counter
from typing import Any, Container, Mapping, Sequence

# 单元格值的展示上限，防止某个字段里塞了一整段文本把行撑爆。
_MAX_CELL_CHARS = 160

_ROW_LABELS = {"added": "新增", "removed": "删除", "modified": "修改", "unchanged": "未变"}

# 表头块里「有改动」的三种状态（`unchanged` 是常驻行，见 `_render_header_block`）。
# 与 `utils/diff_data_utils.header_rows_have_changes`、前端模块的 `CHANGED_ROW_STATUSES`
# 是同一个判据的三个副本（三种语言各一份，改动时一起改）。
_HEADER_CHANGED_STATUSES = ("added", "removed", "modified")
# 表头块最多列几行。表头行就那么几行，单独给一个小上限即可。
_MAX_HEADER_ROWS = 20

# 列级变更段的上限。列改名/增删列是评审者要核对的改动本身，所以逐条给出来；
# 但一次提交动几十列时也不能把真正的行数据挤出单条上限。
_MAX_COLUMN_CHANGES = 40
# 表头列名最多列几个：模型据此把「第 N 列」对上名字，超宽表不列全。
_MAX_COLUMN_NAMES = 40
# 删除列的「上一版取值」证据：只留去重后的前几个值，避免又铺成一整列。
_MAX_REMOVED_VALUE_SAMPLES = 6
_MAX_REMOVED_VALUE_CHARS = 40

# 列级变更的三种形态（`diff_excel_compare._compare_dataframes` 的产出）：
# `{'change': 'renamed'|'added'|'removed', 'column_index', 'column', 'old_name', 'new_name'}`。
_COLUMN_CHANGE_LABELS = {"renamed": "列改名", "added": "新增列", "removed": "删除列"}
# 摘要里的分类计数。**带「处」**：单独一个「删除 1」会被读成删了一行。
_COLUMN_CHANGE_COUNT_LABELS = {"renamed": "改名", "added": "新增列", "removed": "删除列"}
# `diff_excel_compare._display_column_name` 会把重名列的 `X.1` 写成
# `X（同名列第 2 个）`，而行数据里的 `cell_changes` 用的是**原始列名** ——
# 删列过滤要两边都对得上，所以这里能把它还原回去。
_DEDUP_COLUMN_NAME_RE = re.compile(r'^(.+)（同名列第 (\d+) 个）$')


def _render_sheet(name: str, sheet: Any, *, max_rows: int) -> list[str]:
    """一张工作表的正文。

    ## 为什么列级变更必须独立成段

    `sheet["header_changes"]` 是 `diff_excel_compare` 已经算好的列级事实，但原先这里
    只渲染 `rows` + `header_rows`，于是它在行数据这条路上被**结构性抹平**了：

    * **改名**：上一版的列名已按配对结果改写成新名再逐格比（`_pair_columns` 的
      `mapped`），两侧值一样 ⇒ `rows` 是空的，模型读到的是「这次提交什么都没改」；
    * **删列**：上一版多出来的列被 `reindex(fill_value='')` 补空 ⇒ 每一行都多出一条
      「旧值 →（空）」，模型读到的是「有人把整列数据逐行清空了」。

    两种读法都会把结论方向带偏（列被删/改名可能直接影响导表与代码取值），所以列变更
    独立成段；删列那部分在行渲染里**跳过**，不再重复算成行数据改动。
    """
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

    rows = _row_list(sheet.get("rows"))

    # 列级变更先算：它同时决定 rows / header_rows 里哪些 `cell_changes` 只是「列被删」
    # 的连带结果（多行表头的仓库里，表头块会为同一列再报一遍）。
    column_changes = _header_changes(sheet)
    removed_columns = _removed_column_names(column_changes)
    removed_evidence = _removed_column_evidence(rows, removed_columns)
    header_lines = _render_header_block(sheet, skip_columns=removed_columns)

    # 优先展示有变化的行：整表几千行时，未变的行是纯噪音。
    changed = [row for row in rows if _row_status(row) not in ("", "unchanged")]
    dropped_rows = 0
    if removed_columns:
        kept = []
        for row in changed:
            if _is_removed_column_only_row(row, removed_columns):
                dropped_rows += 1
            else:
                kept.append(row)
        changed = kept
    # 原先「没有一行带状态」时退回展示全部行；但「有变更行、只是全被删列过滤掉」不能
    # 退回展示全部 —— 那会把未变行也铺出来，等于凭空多出一批行改动。
    shown = changed or ([] if dropped_rows else list(rows))

    column_lines = _render_column_changes(
        sheet, column_changes, removed_evidence=removed_evidence, dropped_rows=dropped_rows)

    if not rows:
        if deleted:
            # 平台只说了「删了这张表」而没有行 —— 那句话本身照旧是全部信息。
            return head
        if header_lines or column_lines:
            # 「只改了表头 / 只改了列」的提交：rows 是空的，但表头行或列结构真的变了 ——
            # 这里说「没有差异行」等于让 AI 告诉评审者这次提交什么都没改。
            return head + column_lines + header_lines
        return head + ["- 没有差异行。"]
    if deleted:
        head[-1] = (
            f"- **该工作表已被删除**，下面是它被删除前的内容"
            f"（{len(rows)} 行，每一行都是删除行）。"
        )

    stats = sheet.get("stats") or {}
    if stats:
        head.append(
            f"- 本次：新增 {stats.get('added', 0)}、删除 {stats.get('removed', 0)}、"
            f"修改 {stats.get('modified', 0)}（表内共 {len(rows)} 行差异记录）。"
        )
    # 列变更紧跟统计、在表头块与数据行之前 —— 它说明的是「表结构」，不是某一行。
    head.extend(column_lines)
    # 表头行紧跟在统计之后、数据行之前 —— 与页面上的位置一致（表头块在最上面）。
    head.extend(header_lines)

    for row in shown[:max_rows]:
        rendered = _render_row(row, skip_columns=removed_columns)
        if rendered:
            head.append(rendered)
    if len(shown) > max_rows:
        head.append(
            f"- （另有 {len(shown) - max_rows} 行差异未列出，需要时请针对具体行索取。）"
        )
    return head


def _render_header_block(sheet: Mapping, *, skip_columns: Container[str] = ()) -> list:
    """表头块（物理第 2..N 行，见 `services/diff_service.py::_build_header_rows`）。

    与数据行同一个口径：**只列有改动的表头行** —— 块里含未改动的行（页面拿它们说明
    「表头共 3 行」），但那些进提示词只是噪音。

    必须列出来：表头行的改动**不在 `rows` 里**，不列的话「这次提交只改了表头」
    在 AI 眼里就是「没有差异行」，它会如实告诉评审者这次提交什么都没改。

    `skip_columns`（整列被删的名字）与数据行同一个理由：表头行里那一格也只会变成
    「旧值 →（空）」，那是列级变更的又一份副本；整行只因此才有改动的，连行都不要列。
    """
    rows = sheet.get("header_rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    changed = [row for row in rows if _row_status(row) in _HEADER_CHANGED_STATUSES]
    if skip_columns:
        changed = [row for row in changed if not _is_removed_column_only_row(row, skip_columns)]
    if not changed:
        return []
    lines = [f"- 表头 {len(rows)} 行中有 {len(changed)} 处改动。"]
    for row in changed[:_MAX_HEADER_ROWS]:
        rendered = _render_row(row, skip_columns=skip_columns)
        if rendered:
            lines.append(rendered)
    return lines


def _row_list(value: Any) -> list:
    """`rows` 的形状判据与原先一致：不是行序列就当没有行（字符串/字典都不是行列表）。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return list(value)


# --------------------------------------------------------------------------
# 列级变更（`sheet["header_changes"]`）
# --------------------------------------------------------------------------


def _header_changes(sheet: Mapping) -> list[Mapping]:
    """`sheet["header_changes"]` 里形状认得出来的列级变更条目。"""
    changes = sheet.get("header_changes")
    if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
        return []
    return [change for change in changes if isinstance(change, Mapping)]


def _column_change_kind(change: Mapping) -> str:
    return str(change.get("change") or "").strip().lower()


def _change_column_name(change: Mapping) -> str:
    """列变更里「这一列叫什么」。rename 的 `column` 已经是新名，added/removed 同名。"""
    for key in ("column", "new_name", "old_name"):
        text = _name_text(change.get(key))
        if text.strip():
            return text
    return ""


def _name_text(value: Any) -> str:
    return "" if value is None else str(value)


def _change_cell_column(change: Mapping) -> str:
    """行 `cell_changes` 里的列名。**这里是原始列名**（见 `_removed_column_names`）。"""
    column = change.get("column")
    return "" if column is None else str(column)


def _column_index_text(change: Mapping) -> str:
    """「第 N 列」，序号从 1 开始（与 Excel 列序一致）。"""
    try:
        number = int(change.get("column_index"))
    except (TypeError, ValueError):
        return ""
    return f"第 {number} 列"


def _column_name_aliases(display: str) -> set[str]:
    """列变更里的展示名 → 行数据里的原始列名。

    `diff_excel_compare._display_column_name` 把重名列 `X.1` 改写成
    `X（同名列第 2 个）`（名字里带的是出现次序，直接报 `From.79` 会让评审者去找一个
    表里根本不存在的列）。行里的 `cell_changes` 用的仍是原始列名 `X.1` ——
    删列过滤必须两种写法都认，否则一条都跳不过去、整列假变更照旧出现。
    """
    match = _DEDUP_COLUMN_NAME_RE.fullmatch(display)
    if not match:
        return set()
    occurrence = int(match.group(2))
    if occurrence < 2:
        return set()
    return {f"{match.group(1)}.{occurrence - 1}"}


def _removed_column_names(changes: Sequence[Mapping]) -> dict[str, str]:
    """被删掉的列：可匹配的列名 → 展示名（`cell_changes` 与列变更的两种写法都在里面）。"""
    names: dict[str, str] = {}
    for change in changes:
        if _column_change_kind(change) != "removed":
            continue
        display = _change_column_name(change)
        if not display:
            continue
        names.setdefault(display, display)
        for alias in _column_name_aliases(display):
            names.setdefault(alias, display)
    return names


def _removed_column_evidence(rows: Sequence, removed: Mapping[str, str]) -> dict[str, dict]:
    """被删掉的列在上一版填过哪些值。

    **为什么不能连值一起丢掉**：下面会把「旧值 →（空）」这种成片假变更从行里跳过，
    而被删掉那一列的值只存在于 `cell_changes` 里 —— 全丢掉之后，模型与评审者就再也
    看不到「那一列原来填了什么」，而删掉一列常常正是风险本身（枚举/ID 列被删，
    代码与老存档还在按它取值）。所以只留**去重后的前几个值**：既说明「这一列有数据」，
    又不构成一整列的逐行变更。
    """
    if not removed:
        return {}
    evidence: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        changes = row.get("cell_changes")
        if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
            continue
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            display = removed.get(_change_cell_column(change))
            if display is None:
                continue
            bucket = evidence.setdefault(display, {"rows": 0, "values": []})
            bucket["rows"] += 1
            value = _clip(_cell(change.get("old_value")), _MAX_REMOVED_VALUE_CHARS)
            if value not in bucket["values"] and len(bucket["values"]) < _MAX_REMOVED_VALUE_SAMPLES:
                bucket["values"].append(value)
    return evidence


def _is_removed_column_only_row(row: Any, removed: Mapping[str, str]) -> bool:
    """这一行被报成「修改」，是不是只因为某一列被删了。

    只有 `modified` 行才可能有这种假变更：`added` / `removed` 行本身就是行级事实
    （删列同时新增了一行时，新增行照样要列出来）。形状认不全时返回 False ——
    宁可不跳过，也不要吞掉真实改动。
    """
    if _row_status(row) != "modified":
        return False
    number = row.get("row_number")
    previous_number = row.get("previous_row_number")
    if previous_number is not None and str(previous_number) != str(number):
        # 行位置也变了：那是行级事实，不能连它一起吞掉。
        return False
    changes = row.get("cell_changes")
    if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)) or not changes:
        return False
    columns = []
    for change in changes:
        if not isinstance(change, Mapping):
            return False
        columns.append(_change_cell_column(change))
    return bool(columns) and all(column in removed for column in columns)


def _render_column_changes(
    sheet: Mapping,
    changes: Sequence[Mapping],
    *,
    removed_evidence: Mapping[str, Mapping] | None = None,
    dropped_rows: int = 0,
) -> list[str]:
    """列级变更段落。**必须独立成段**（理由见 `_render_sheet` 的 docstring）。"""
    if not changes:
        return []
    evidence = removed_evidence or {}
    counts = Counter(_column_change_kind(change) for change in changes)
    breakdown = "、".join(
        f"{label} {counts[kind]} 处"
        for kind, label in _COLUMN_CHANGE_COUNT_LABELS.items()
        if counts.get(kind)
    )
    first = f"- 列变更（{len(changes)} 处）"
    if breakdown:
        first += f"：{breakdown}"
    lines = [first + "。这是列结构变化，不是行数据改动。"]
    for change in changes[:_MAX_COLUMN_CHANGES]:
        lines.append(_column_change_line(change, evidence))
    if len(changes) > _MAX_COLUMN_CHANGES:
        lines.append(f"- （另有 {len(changes) - _MAX_COLUMN_CHANGES} 处列变更未列出。）")
    lines.extend(_render_sheet_columns(sheet, has_removed=bool(counts.get("removed"))))
    if dropped_rows:
        lines.append(
            f"- 上面「删除列」连带的 {dropped_rows} 行「旧值 →（空）」已按列级变更计，"
            "不再重复算作行数据改动。"
        )
    return lines


def _column_change_line(change: Mapping, evidence: Mapping[str, Mapping]) -> str:
    kind = _column_change_kind(change)
    label = _COLUMN_CHANGE_LABELS.get(kind)
    display = _change_column_name(change) or "（未命名列）"
    index = _column_index_text(change)
    if kind == "renamed":
        old_name = _name_text(change.get("old_name")) or "（空列名）"
        new_name = _name_text(change.get("new_name")) or display
        return f"- {label}：{index}「{old_name}」→「{new_name}」"
    if kind == "added":
        return f"- {label}：{index}「{display}」"
    if kind == "removed":
        # 删列的序号是**上一版的**：当前表里已经没有这一列，说「第 N 列」要标明是哪一版。
        evidence_text = _removed_evidence_text(evidence.get(display))
        return f"- {label}：上一版{index}「{display}」{evidence_text}"
    return f"- 列变更（{kind or '未知类型'}）：{index}「{display}」"


def _removed_evidence_text(info: Mapping | None) -> str:
    if not info or not info.get("rows"):
        return ""
    values = [str(value) for value in info.get("values") or []]
    if not values:
        return ""
    suffix = " 等" if int(info["rows"]) > len(values) else ""
    return f"（上一版这一列有值的 {info['rows']} 行，取值示例：{'、'.join(values)}{suffix}）"


def _render_sheet_columns(sheet: Mapping, *, has_removed: bool = False) -> list[str]:
    """表头列名（两版并集）。列变更里的「第 N 列」要能对上名字，模型才能定位。"""
    headers = sheet.get("headers")
    if not isinstance(headers, Sequence) or isinstance(headers, (str, bytes)) or not headers:
        headers = sheet.get("columns")
    if not isinstance(headers, Sequence) or isinstance(headers, (str, bytes)) or not headers:
        return []
    names = [str(name) for name in headers]
    text = "、".join(names[:_MAX_COLUMN_NAMES])
    if len(names) > _MAX_COLUMN_NAMES:
        text += f"…（另有 {len(names) - _MAX_COLUMN_NAMES} 列未列出）"
    # 顺序与 `diff_excel_compare` 的 `ordered_columns` 一致：当前列在前，被删掉的追加在后。
    suffix = "；按当前表顺序，已删除的列排在最后" if has_removed else "，按当前表顺序"
    return [f"- 列名（共 {len(names)} 列{suffix}）：{text}"]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _row_status(row: Any) -> str:
    if not isinstance(row, Mapping):
        return ""
    return str(row.get("status") or "").strip().lower()


def _render_row(row: Any, *, skip_columns: Container[str] = ()) -> str:
    """一行变更的文本。`skip_columns` 用来跳过「整列被删」连带的假变更。"""
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
            if skip_columns and _change_cell_column(change) in skip_columns:
                # 这一整列被删了（见 `_render_sheet`）：每一行的「旧值 →（空）」
                # 是同一个列级事实，留在这里会盖过真正改掉的那几格。
                continue
            parts.append(
                f"{column}: {_cell(change.get('old_value'))} → {_cell(change.get('new_value'))}"
                + _type_change_note(change)
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


def _type_change_note(change: Mapping) -> str:
    """单元格类型变化的说明；**没有类型信息时返回空串**。

    形状约定（`diff_excel_compare._annotate_cell_changes_with_types` /
    `_row_type_only_changes` 的产出）：只有两侧都非空且类型不同时才会带
    `old_type` / `new_type`。所以这里判空就够 —— 缺字段、只有一边、或两边相同
    都返回空串，渲染结果与改动前**逐字相同**（本功能不改变任何既有输出）。

    为什么值得单独说一句：客户端是按类型取值的（`tonumber` / `int.Parse`），
    `100`（数值）改成 `"100"`（文本）在页面与文本 diff 里都是「值没变」，
    但游戏运行时会从「正常」变成「抛异常 / 取到 0」—— 这是配表缺陷里
    最容易被漏掉、也最难从取值上看出来的一类。
    """
    old_type = change.get("old_type")
    new_type = change.get("new_type")
    old_text = "" if old_type is None else str(old_type).strip()
    new_text = "" if new_type is None else str(new_type).strip()
    if not old_text or not new_text or old_text == new_text:
        return ""
    return f"（类型：{old_text}→{new_text}）"


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
