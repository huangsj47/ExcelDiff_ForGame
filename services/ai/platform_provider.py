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
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from services.ai.skill_loader import LoadedSkills
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

    # 解析失败 / 文件被删除，都要**明说**，不能渲染成一张空表。
    if payload.get("error"):
        message = str(payload.get("message") or payload.get("error") or "未知原因")
        return f"[配表差异解析失败] {where}：{message}。**这不等于「没有改动」。**"
    if str(payload.get("operation") or "") == "deleted":
        return f"[配表] {where}：该表已被删除。删除整张表要确认是否有代码或存档仍在引用。"

    sheets = payload.get("sheets") or {}
    if not isinstance(sheets, Mapping) or not sheets:
        return f"[配表] {where}：本次没有可展示的差异。"

    summary = payload.get("summary") or {}
    lines: list[str] = []
    if where:
        lines.append(f"配表差异：{where}")
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
    if operation == "deleted":
        return head + ["- 该工作表已被删除。"]
    if operation == "added":
        head.append("- 该工作表是新增的。")
    if sheet.get("error"):
        return head + [f"- 解析失败：{sheet.get('message') or sheet.get('error')}"]

    rows = sheet.get("rows") or []
    header_lines = _render_header_block(sheet)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        if header_lines:
            # 「只改了表头」的提交：rows 是空的，但表头行真的变了 ——
            # 这里说「没有差异行」等于让 AI 告诉评审者这次提交什么都没改。
            return head + header_lines
        return head + ["- 没有差异行。"]

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
        return prefix + " ｜ ".join(_cell(cell) for cell in cells)

    return prefix + "（该行有变更，但没有可展示的字段明细）"


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


def _read_excel_sheets(raw: bytes, *, max_rows: int) -> Optional[str]:
    """把 xlsx 的字节渲染成文本表格。

    Excel 的「完整内容」本质就是一张表；按二进制拒绝它会让模型完全看不到这张表长什么样。

    每个工作表先给一段**整表统计**，再给前 `max_rows` 行正文。顺序是有意的：截断只砍
    尾巴（`budget.truncate_text`），统计放前面才不会被砍掉 —— 而它恰恰是「值合不合理」
    这类判断唯一拿得到的比较基准。
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

    lines: list[str] = []
    try:
        for name in book.sheetnames:
            sheet = book[name]
            lines.append(f"### 工作表「{name}」")
            stats = _SheetStats()
            labels: list[str] = []
            body: list[str] = []
            count = 0
            truncated_rows = False
            for row in sheet.iter_rows(values_only=True):
                if not labels and row is not None and any(
                    value is not None and str(value).strip() for value in row
                ):
                    # 首个非空行按**列名**处理：它只当标签，不进统计（表头文字当成取值会
                    # 让每个文本列都多出一个「只出现一次」的值）。
                    labels = ["" if value is None else str(value) for value in row]
                    continue
                stats.add_row(row)
                if count >= max_rows:
                    # 统计要覆盖整表，所以这里不能 break：只停止累积正文。
                    if row is not None and any(value is not None for value in row):
                        truncated_rows = True
                    continue
                if row is None or all(value is None for value in row):
                    continue
                body.append("- " + " ｜ ".join(_cell(value) for value in row))
                count += 1
            # **统计在正文之前**：`file_content` 单条上限 11,000 字符，而截断只砍尾巴
            # （`budget.truncate_text`）—— 放在正文后面，大表一截断就先丢基准，
            # 而基准正是这条路径存在的理由。
            lines.extend(stats.render(labels))
            lines.extend(body)
            if truncated_rows:
                lines.append(
                    f"- （正文只展示了前 {max_rows} 行；上面的整表统计覆盖整表。）"
                )
            lines.append("")
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines).rstrip() + "\n"


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
    ):
        self._loaded = loaded
        self._max_rows = max_rows_per_sheet
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

    def file_diff(self, commit: str, path: str) -> Optional[str]:
        """某个文件在某个提交上的 diff（Excel 走结构化差异）。

        **先读平台已经算好并落库的那一份**（周版本合并 diff，就是周版本页面读的同一个
        payload），读不到才退回实时计算。理由见 `_weekly_stored_diff`：实时计算需要
        平台本地有该仓库的工作副本，而 platform/agent 模式下平台明确不 clone ——
        那条路上「读不到内容」会退化成一句「取到了记录但没有补丁内容」，模型据此得出的
        结论不是「我读不到」，而是「这里没什么可看的」。
        """
        row = self._commit_row(commit, path)
        if row is None:
            return None

        if self._use_stored_batch_diff:
            stored, provenance = _weekly_stored_diff(
                getattr(row, "repository_id", None), path, commit
            )
            if stored is not None:
                rendered = render_diff_payload(
                    stored, path=path, max_rows_per_sheet=self._max_rows
                )
                if rendered:
                    # 出处说明只在这个窗口合并了多条提交时才有内容（见 `_batch_provenance`）
                    # —— 模型拿到的索取形状是「提交 X 改了什么」，不说明它会张冠李戴。
                    return f"{provenance}\n{rendered}" if provenance else rendered
                # 落回实时计算：这份缓存载荷渲染不出来（例如旧口径的分段结构），
                # 而实时那条路算出来的东西至少是能读的。
                log_print(f"⚠️ AI 取数：已落库的 diff 渲染不出来，改用实时计算 {path}")

        try:
            from services.commit_diff_logic import resolve_previous_commit
            from services.vcs_content_service import get_unified_diff_data

            previous = resolve_previous_commit(row)
            payload = get_unified_diff_data(row, previous)
        except Exception as exc:  # noqa: BLE001 —— 取数失败只该让这一条降级
            log_print(f"⚠️ AI 取数：diff 失败 {commit[:12]} {path}: {type(exc).__name__}: {exc}")
            return None

        return render_diff_payload(payload, path=path, max_rows_per_sheet=self._max_rows)

    def file_content(self, commit: str, path: str) -> Optional[str]:
        """某个文件在某个提交上的完整内容。"""
        row = self._commit_row(commit, path)
        if row is None:
            return None
        repository = getattr(row, "repository", None)
        if repository is None:
            return None

        try:
            from services.vcs_content_service import get_file_content_from_git

            raw = get_file_content_from_git(repository, commit, path)
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：内容失败 {commit[:12]} {path}: {type(exc).__name__}: {exc}")
            return None

        if raw is None:
            return None
        if isinstance(raw, str):
            return raw
        if not raw:
            # 取到了、长度为零：这是「确实没有内容」，按契约返回空串。
            return ""

        # 判定用 openpyxl 的实际能力，而不是平台的「配表」清单：见 _WORKBOOK_EXTENSIONS
        # 上方注释（`.csv`/`.tsv` 走文本分支才能把完整内容交给模型，`.xls`/`.xlsb`
        # openpyxl 也读不了）。这里原先 import 的 `services.excel_cache_service` 并不存在，
        # 且 import 在 try 之外 → 任何非空内容都会抛 ModuleNotFoundError。
        if _is_openpyxl_workbook(path):
            rendered = _read_excel_sheets(raw, max_rows=self._max_rows)
            if rendered is None:
                return (
                    f"[配表] {path}：内容无法解析成文本表格。"
                    "**这不等于「没有内容」**，需要核对时请说明该表无法读取。"
                )
            return rendered
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return (
                f"[无法展示的内容] {path}：不是文本也不是配表，无法以文本形式核对。"
                "**这不等于「没有内容」。**"
            )

    # -- 内部 ---------------------------------------------------------------

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
