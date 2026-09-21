# -*- coding: utf-8 -*-
"""把 `.docx` 的正文渲染成文本，给模型看。

## 为什么需要它

`.docx` 是 ZIP，`looks_binary` 认得出（魔数 `PK\\x03\\x04`），于是 `file_content` 对它
一律回一句「[无法展示的内容] …不是文本」—— 而项目里真正有信息量的东西常常正是这类文件：
策划工作台上的访谈记录、需求文档、评审纪要。模型看不到它们，只能靠文件名猜。

## 为什么不用 python-docx

`.docx` 就是一个 zip，正文在 `word/document.xml` 里，段落是 `w:p`、表格是 `w:tbl`、
文本是 `w:t` —— 用标准库的 `zipfile` + `ElementTree` 就够，不必为它加一个依赖
（平台的部署形态是「业务节点上跑一份」，多一个依赖就多一处要装的东西）。
**删掉的文字天然不会被读进来**：Word 把删除线内容放在 `w:delText` 里，而这里只读 `w:t`。

## 与配表（`.xlsx`）的分工

配表是**结构化**的（表 × 行 × 列，列名有坐标），所以它有自己那条路（`excel_view`）。
docx 是**叙述性**的，没有行列坐标可言，所以这里渲染成纯文本行，交给
`utils.content_window.slice_lines` 按行切窗口 —— 与代码/文本那条路**同一套窗口规则与
抬头格式**：模型拿到的都是「共 N 行、下面是第 a–b 行」，它已经会读了。

渲染出来的行号是**文本的行号**（不是 Word 里的行），这一点由调用方的抬头说清（它本来
就写着「共 N 行」，N 是渲染后的行数）。

## 渲染口径

* 段落按文档顺序给，空段落丢掉（它们在 Word 里占多数、对模型没有信息量）；
* 标题按 `pStyle` 的 `HeadingN` 映射成 `#`×N；`Title` 映射成 `#`；
* 带编号/项目符号的段落（`numPr`）前缀 `- `；
* 表格逐行渲染成 `| 单元格 | … |`，前面加一行 `（表：X 行 × Y 列）`——
  **不假装它是 markdown 表**：没有列名与表头行的信息，冒充分隔线会让模型以为首行是列名。
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Optional

# OOXML 的命名空间前缀。Word 的正文元素都带它。
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 能按这条路读的扩展名。`.doc`（OLE 老格式）不在这里：它不是 zip，读不了，
# 让它照旧走「无法展示的内容」——**那句提示是对的**，不能假装读得到。
DOCX_EXTENSIONS = (".docx", ".docm")

_HEADING_RE = re.compile(r"^Heading([1-6])$")


def is_docx(path: str) -> bool:
    """按扩展名判。**不看内容**：`.docx` 与别的 OOXML 都是 zip，魔数分不出来，
    而扩展名是平台上下唯一稳定的判据（`.xlsx` 那条路也是这么分的）。"""
    lowered = str(path or "").strip().lower()
    return lowered.endswith(DOCX_EXTENSIONS)


def render_docx_text(raw: Any, *, path: str = "") -> Optional[str]:
    """渲染正文。**读不出来返回 `None`**（调用方据此说「解析失败」，而不是给一句空正文）。

    返回 `None` 与返回空串是两件事：「解析不了」和「这份文档是空的」——
    前者要人去看那个文件，后者只是没有内容。
    """
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw or b"")
    if not data:
        return ""
    try:
        with io.BytesIO(data) as buffer, zipfile.ZipFile(buffer) as archive:
            document = archive.read("word/document.xml")
    except Exception:  # noqa: BLE001 —— BadZipFile/KeyError/OSError 都归为「读不了」
        return None
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        return None
    body = root.find(f"{_W}body")
    if body is None:
        return None

    lines: list[str] = []
    for block in body:
        if block.tag == f"{_W}p":
            text = _paragraph_text(block)
            if text:
                lines.append(_decorate(block, text))
        elif block.tag == f"{_W}tbl":
            lines.extend(_table_lines(block))
    # 末尾补一个换行：与别的正文来源一致（`slice_lines` 按行切，末尾有没有换行
    # 决定最后一行算不算一行 —— 让它算）。
    return "\n".join(lines) + "\n" if lines else ""


def _paragraph_text(paragraph: ET.Element) -> str:
    """一段的纯文本。`w:tab` 给制表符，`w:br` 给换行，其余取 `w:t` 的文字。

    **只读 `w:t`**：Word 把「修订后删掉的文字」放在 `w:delText` 里，读进来会把已经不
    存在的内容当成正文 —— 那比读不到更糟（模型会据此写结论）。
    """
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag == f"{_W}br":
            parts.append("\n")
    return "".join(parts).strip()


def _decorate(paragraph: ET.Element, text: str) -> str:
    """按段落样式加前缀（标题 / 列表项）。认不出来就原样返回。"""
    properties = paragraph.find(f"{_W}pPr")
    if properties is None:
        return text
    style = properties.find(f"{_W}pStyle")
    if style is not None:
        value = str(style.get(f"{_W}val") or "")
        if value == "Title":
            return f"# {text}"
        matched = _HEADING_RE.match(value)
        if matched:
            return f"{'#' * int(matched.group(1))} {text}"
    if properties.find(f"{_W}numPr") is not None:
        return f"- {text}"
    return text


def _table_lines(table: ET.Element) -> list[str]:
    """一张表渲染成若干行。**不写 markdown 分隔线**（见模块抬头那段）。"""
    rows: list[list[str]] = []
    for row in table.findall(f"{_W}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{_W}tc"):
            text = " ".join(
                part for part in (_paragraph_text(p) for p in cell.findall(f"{_W}p")) if part
            )
            cells.append(text.replace("|", "\\|"))
        rows.append(cells)
    if not rows:
        return []
    width = max(len(row) for row in rows)
    head = f"（表：{len(rows)} 行 × {width} 列）"
    body = ["| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in rows]
    return [head, *body]
