"""Read exact physical spreadsheet coordinates, with repeatable character paging.

file_content.lines: Sheet1!A262:H262 or #1!A262:H262. @N continues the
same rectangle's serialized cell facts at character N. No whole-sheet statistics
or first-N-row preview is substituted for explicitly requested coordinates.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass

MAX_SELECTED_CELLS = 10000
MAX_SELECTED_CHARS = 8 * 1024 * 1024


@dataclass(frozen=True)
class ExcelSelection:
    sheet: str
    area: str
    offset: int = 0

    def address(self, offset=0):
        return f"{self.sheet}!{self.area}" + (f"@{offset}" if offset else "")


def parse_selection(value):
    from openpyxl.utils.cell import range_boundaries

    match = re.fullmatch(r"(.{1,128})!([A-Za-z]{1,3}[1-9]\d{0,6}(?::[A-Za-z]{1,3}[1-9]\d{0,6})?)(?:@(\d{1,10}))?", str(value or ""))
    if not match:
        raise ValueError("Excel 坐标须为 Sheet1!A262:H262 或 #1!A262:H262，可用 @字符偏移续读")
    sheet, area, offset = match.groups()
    if any(ord(c) < 32 for c in sheet):
        raise ValueError("工作表名称不得含控制字符")
    left, top, right, bottom = range_boundaries(area.upper())
    if left > right or top > bottom or right > 16384 or bottom > 1048576:
        raise ValueError("Excel 物理坐标超出范围或逆序")
    if (right - left + 1) * (bottom - top + 1) > MAX_SELECTED_CELLS:
        raise ValueError("单次矩形最多10000个单元格，请缩小范围")
    return ExcelSelection(sheet, area.upper(), int(offset or 0))


def render_range(raw, selection, *, path="", char_budget=11000, header_rows=1, header_name_row=1):
    from openpyxl import load_workbook
    from openpyxl.utils.cell import get_column_letter, range_boundaries

    try:
        selected = parse_selection(selection)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 64 * 1024 * 1024:
                raise ValueError("工作簿解压尺寸超过精确读取上限，请提供更小的源表")
        book = load_workbook(io.BytesIO(raw), data_only=False, read_only=True)
        cached = None
        try:
            sheet_name = selected.sheet
            if sheet_name.startswith("#") and sheet_name[1:].isdigit():
                index = int(sheet_name[1:])
                if not 1 <= index <= len(book.sheetnames):
                    raise ValueError("点名的工作表序号不存在")
                sheet_name = book.sheetnames[index - 1]
            elif sheet_name.startswith("'") and sheet_name.endswith("'"):
                sheet_name = sheet_name[1:-1].replace("''", "'")
            if sheet_name not in book.sheetnames:
                choices = "; ".join(f"#{i}={name!r}" for i, name in enumerate(book.sheetnames[:20], 1))
                raise ValueError(f"点名的工作表不存在；可读目录（最多列20张）：{choices}；可用 #序号!坐标重试")
            sheet = book[sheet_name]
            cached = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
            cached_sheet = cached[sheet_name]
            left, top, right, bottom = range_boundaries(selected.area)
            facts, size = [], 0

            count = max(1, int(header_rows or 1))
            if count * (right - left + 1) > MAX_SELECTED_CELLS:
                raise ValueError("配置表头过大，请缩小列范围")
            merge_areas = _merged_areas(raw, sheet)

            def collect(start, end, role):
                nonlocal size
                bounds = dict(min_row=start, max_row=end, min_col=left, max_col=right)
                source_rows = sheet.iter_rows(**bounds)
                cached_rows = cached_sheet.iter_rows(**bounds)
                for row_number, (row, values) in enumerate(zip(source_rows, cached_rows), start=start):
                    for column, (cell, cached_cell) in enumerate(zip(row, values), start=left):
                        address = f"{get_column_letter(column)}{row_number}"
                        value = cell.value
                        anchor = next((a for x1, y1, x2, y2, a in merge_areas
                                       if x1 <= column <= x2 and y1 <= row_number <= y2), None)
                        entry = {"role": role, "cell": address,
                                 "type": "blank" if value is None else type(value).__name__, "value": value}
                        if anchor:
                            entry["merged_anchor"] = anchor
                            if address != anchor:
                                entry["type"] = "merged_follower"
                        if getattr(cell, "data_type", "") == "f":
                            entry.update(type="formula", formula=value, value=cached_cell.value,
                                         cached_status="unknown" if cached_cell.value is None else "available")
                        line = json.dumps(entry, ensure_ascii=False, default=str)
                        size += len(line)
                        if size > MAX_SELECTED_CHARS:
                            raise ValueError("请求矩形内容过大，请缩小到需要核实的行列")
                        facts.append(line)

            if top > count:
                collect(1, count, "header")
            collect(top, bottom, "data")
            body = "\n".join(facts)
        finally:
            book.close()
            if cached is not None:
                cached.close()
        if selected.offset > len(body):
            raise ValueError("续读偏移超过本矩形正文长度")
        head = f"[Excel精确取证] {path} sheet={sheet_name!r} range={selected.area}；物理坐标，保留单元格类型。\n"
        # Reserve the exact continuation receipt, rather than silently tail-truncating cell facts.
        limit = max(1, int(char_budget or len(body) + 1000))
        room = max(1, limit - len(head) - len(selected.address()) - 250)
        end = min(len(body), selected.offset + room)
        tail = (f'\n[矩形未完整交付] 下一页 file_content lines="{selected.address(end)}"；拼接各页后才是完整单元格事实。'
                if end < len(body) else "\n[矩形完整交付]" if selected.offset == 0 else "\n[矩形末页] 请与此前同矩形分页合并阅读。")
        return head + f"chars={selected.offset}-{end}/{len(body)}\n" + body[selected.offset:end] + tail
    except Exception as exc:
        return f"[取数失败] Excel精确坐标 {selection!r}：{type(exc).__name__}: {exc}"


def _merged_areas(raw, sheet):
    from xml.etree.ElementTree import iterparse

    from openpyxl.utils.cell import range_boundaries

    path = str(getattr(sheet, "_worksheet_path", "")).lstrip("/")
    if not path:
        raise ValueError("无法确认工作表合并元信息，未将空格当成缺值")
    result = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        with archive.open(path) as stream:
            for _event, element in iterparse(stream, events=("end",)):
                if element.tag.rsplit("}", 1)[-1] == "mergeCell":
                    area = element.attrib.get("ref", "")
                    if area:
                        result.append((*range_boundaries(area), area.split(":")[0]))
                element.clear()
    return result
