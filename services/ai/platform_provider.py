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
"""

from __future__ import annotations

import io
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
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
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

    for row in shown[:max_rows]:
        rendered = _render_row(row)
        if rendered:
            head.append(rendered)
    if len(shown) > max_rows:
        head.append(
            f"- （另有 {len(shown) - max_rows} 行差异未列出，需要时请针对具体行索取。）"
        )
    return head


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
        return f"[代码] {where}：取到了记录但没有补丁内容。"
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
        return f"[文本] {where}：取到了记录但没有补丁内容。"
    return f"文件差异：{where}\n\n{patch}"


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


def _read_excel_sheets(raw: bytes, *, max_rows: int) -> Optional[str]:
    """把 xlsx 的字节渲染成文本表格。

    Excel 的「完整内容」本质就是一张表；按二进制拒绝它会让模型完全看不到这张表长什么样。
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
            count = 0
            for row in sheet.iter_rows(values_only=True):
                if count >= max_rows:
                    lines.append(f"- （另有更多行未列出，本表只展示前 {max_rows} 行。）")
                    break
                if row is None or all(value is None for value in row):
                    continue
                lines.append("- " + " ｜ ".join(_cell(value) for value in row))
                count += 1
            lines.append("")
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines).rstrip() + "\n"


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
    ):
        self._loaded = loaded
        self._max_rows = max_rows_per_sheet

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
        """某个文件在某个提交上的 diff（Excel 走结构化差异）。"""
        row = self._commit_row(commit, path)
        if row is None:
            return None

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
