# -*- coding: utf-8 -*-
"""配表单元格只改了「类型」时也必须可见（WP18，客户端 P0 漏报）。

## 缺陷形态

`services/diff_excel_reader.py` 的读取口径是 `dtype=str` + `keep_default_na=False`。
它保住的是**字面量**（`'00123'` 的前导零、`'1.10'` 的尾零、文本 `'TRUE'`），
代价是把「文件里这一格是什么类型」抹掉了：

    文件里是数值 100  ──┐
                        ├─  pandas 都读成 '100'  →  `_values_equal` 判等 → 「未变」
    文件里是文本 "100" ─┘

于是「策划把 100 改成了 '100'」这种提交：

* 页面：这一次提交显示「没有变更」；
* AI：`excel_view` 只渲染 `cell_changes` 的 `old_value` / `new_value`，
  而这条记录根本不存在（值相等，不会进 `cell_changes`）；
* 客户端：数值列变成文本列，`tonumber` / `int.Parse` 从「正常」变成
  「抛异常 / 取到 0」—— 配表缺陷里最难从取值上看出来的一类。

## 现在的口径

* 读取层额外产出**原始单元格类型**（`WorkbookData.cell_types`，openpyxl 原始扫描，
  与 `dtype=str` 的字面量互不影响）；
* `diff_excel_compare` 对「值字面量相同、原始类型不同」补一条 `cell_changes` 记录
  （`old_value == new_value`），并给值也变了的格子补 `old_type` / `new_type`
  （只在两侧都非空且类型不同时填）；
* `excel_view` 在那一格后追加 `（类型：数值→文本）`；
* **没有类型信息时输出与改动前逐字相同**（CSV / 老格式 / 手工构造的载荷都走这条路）。

## 本文件钉住的三件事

1. `100`（数值）→ `"100"`（文本）：`cell_changes` 带 `old_type` / `new_type`，
   渲染文本含「类型」；
2. 值真变化（`100 → 200`，两侧都是数值）：`cell_changes` 与渲染都与改动前一致，
   不额外加类型说明；
3. 无类型信息：`_render_row` / `_render_sheet` / CSV 全链路输出逐字不变。
"""
from __future__ import annotations

import datetime
import io
import os
import sys

import openpyxl
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.ai.excel_view import _render_row, _render_sheet  # noqa: E402
from services.ai.platform_provider import render_diff_payload  # noqa: E402
from services.diff_excel_reader import WorkbookData  # noqa: E402
from services.diff_service import DiffService  # noqa: E402

TABLE = "config/[30]道具表_CfgItem.xlsx"
HEADER = ["id", "name", "level", "val"]
# 关键列固定成第 1 列：这些样本只有 4 列，值一改相似度就可能掉到阈值以下，
# 会被行匹配报成「删一行 + 加一行」。本文件要钉的是 cell_changes，不是行配对。
KEY_COLUMNS = "1"


def _xlsx(rows, sheet="S"):
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _payload(previous_rows, current_rows, *, path=TABLE, sheet="S", **kwargs):
    payload = DiffService().process_diff(
        path, _xlsx(current_rows, sheet), _xlsx(previous_rows, sheet), **kwargs)
    assert payload.get("type") == "excel", payload
    return payload


def _sheet(previous_rows, current_rows, **kwargs):
    kwargs.setdefault("key_columns", KEY_COLUMNS)
    return _payload(previous_rows, current_rows, **kwargs)["sheets"]["S"]


def _render(sheet, name="S"):
    return "\n".join(_render_sheet(name, sheet, max_rows=20))


# ---------------------------------------------------------------------------
#  读取层：原始类型与字面量是两份事实，互不改写
# ---------------------------------------------------------------------------


class TestReaderKeepsRawCellTypes:
    """`_read_excel_data` 的返回值多一份类型表，但「按文本原样读」一个字都不动。"""

    def test_each_cell_type_is_recorded(self):
        content = _xlsx([
            ["id", "name", "flag", "day"],
            [1, "a", True, datetime.datetime(2024, 1, 2)],
            ["00123", "", None, 0],
        ])

        data = DiffService()._read_excel_data(content, "config/x.xlsx")

        assert isinstance(data, WorkbookData)
        # n=数值、t=文本、b=布尔、d=日期、e=空（编码见 diff_excel_reader）
        assert data.cell_types["S"] == ["ntbd", "teen"]

    def test_literal_semantics_are_not_changed(self):
        content = _xlsx([
            ["id", "name", "flag", "day"],
            [1, "a", True, datetime.datetime(2024, 1, 2)],
            ["00123", "", None, 0],
        ])

        frame = DiffService()._read_excel_data(content, "config/x.xlsx")["S"]

        # 与改动前的 dtype=str 读数逐字一致：前导零、布尔、日期、空值都没被改写
        assert frame.iloc[0].tolist() == ["1", "a", "True", "2024-01-02 00:00:00"]
        assert frame.iloc[1].tolist() == ["00123", "", "", "0"]


# ---------------------------------------------------------------------------
#  值没变、类型变了：这一条原先根本不存在
# ---------------------------------------------------------------------------


class TestTypeOnlyChangeIsReported:
    PREVIOUS = [HEADER, [1, "a", 10, 100]]
    CURRENT = [HEADER, [1, "a", 10, "100"]]

    def test_the_cell_change_carries_both_types(self):
        sheet = _sheet(self.PREVIOUS, self.CURRENT)

        assert sheet["rows"], f"只改类型的提交仍被判成「没有变更」：{sheet}"
        assert sheet["rows"][0]["cell_changes"] == [{
            "column": "val",
            "old_value": "100",
            "new_value": "100",
            "old_type": "数值",
            "new_type": "文本",
        }]

    def test_the_row_is_no_longer_reported_as_unchanged(self):
        sheet = _sheet(self.PREVIOUS, self.CURRENT)

        assert [row["status"] for row in sheet["rows"]] == ["modified"]
        assert sheet["stats"]["modified"] == 1
        assert sheet["stats"]["added"] == 0 and sheet["stats"]["removed"] == 0

    def test_the_rendered_text_names_the_type_change(self):
        text = _render(_sheet(self.PREVIOUS, self.CURRENT))

        assert "类型：数值→文本" in text, text
        assert "val: 100 → 100（类型：数值→文本）" in text, text

    def test_the_platform_payload_carries_it_too(self):
        """`render_diff_payload` 是 AI 实际读到的那条路，不能只有 `_render_sheet` 认。"""
        payload = _payload(self.PREVIOUS, self.CURRENT, key_columns=KEY_COLUMNS)

        text = render_diff_payload(payload)

        assert "类型：数值→文本" in text, text


# ---------------------------------------------------------------------------
#  值真的变了、类型没变：输出必须与改动前逐字相同
# ---------------------------------------------------------------------------


class TestValueChangeWithoutTypeChange:
    PREVIOUS = [HEADER, [1, "a", 10, 100]]
    CURRENT = [HEADER, [1, "a", 10, 200]]

    def test_cell_change_has_no_type_fields(self):
        sheet = _sheet(self.PREVIOUS, self.CURRENT)

        assert sheet["rows"][0]["cell_changes"] == [
            {"column": "val", "old_value": "100", "new_value": "200"}
        ]

    def test_rendered_output_is_byte_identical_to_the_previous_version(self):
        assert _render_sheet("S", _sheet(self.PREVIOUS, self.CURRENT), max_rows=20) == [
            "### 工作表「S」",
            "- 本次：新增 0、删除 0、修改 1（表内共 1 行差异记录）。",
            "- [修改] 第 2 行：val: 100 → 200",
        ]

    def test_no_type_note_leaks_into_the_text(self):
        assert "类型" not in _render(_sheet(self.PREVIOUS, self.CURRENT))


class TestValueAndTypeChangeTogether:
    """值也变、类型也变：类型说明照样要给 —— 客户端仍会因类型变化而取值行为不同。"""

    def test_both_the_values_and_the_types_are_reported(self):
        sheet = _sheet([HEADER, [1, "a", 10, 100]], [HEADER, [1, "a", 10, "abc"]])
        change = sheet["rows"][0]["cell_changes"][0]

        assert (change["old_value"], change["new_value"]) == ("100", "abc")
        assert (change["old_type"], change["new_type"]) == ("数值", "文本")
        assert "val: 100 → abc（类型：数值→文本）" in _render(sheet)


# ---------------------------------------------------------------------------
#  没有类型信息：渲染层必须与改动前逐字相同
# ---------------------------------------------------------------------------


LEGACY_ROW = {
    "row_number": 3,
    "status": "modified",
    "cell_changes": [{"column": "备注", "old_value": "old1", "new_value": ""}],
}
LEGACY_ROW_RENDERED = "- [修改] 第 3 行：备注: old1 → （空）"

LEGACY_SHEET = {
    "rows": [
        {"row_number": 3, "status": "modified",
         "cell_changes": [{"column": "攻击", "old_value": "30", "new_value": "45"}]},
    ],
    "stats": {"added": 0, "removed": 0, "modified": 1},
    "headers": ["ID", "攻击"],
}
LEGACY_SHEET_RENDERED = [
    "### 工作表「道具表」",
    "- 本次：新增 0、删除 0、修改 1（表内共 1 行差异记录）。",
    "- [修改] 第 3 行：攻击: 30 → 45",
]


class TestWithoutTypeInformationOutputIsUnchanged:
    def test_render_row_is_exact_when_type_fields_are_absent(self):
        assert _render_row(LEGACY_ROW) == LEGACY_ROW_RENDERED

    @pytest.mark.parametrize("extra", [
        {"old_type": None, "new_type": None},
        {"old_type": "", "new_type": ""},
        {"old_type": "数值"},                                   # 只有一边
        {"old_type": "数值", "new_type": "数值"},                 # 两侧同类型
    ])
    def test_incomplete_or_equal_types_do_not_add_a_note(self, extra):
        row = dict(LEGACY_ROW, cell_changes=[dict(LEGACY_ROW["cell_changes"][0], **extra)])

        assert _render_row(row) == LEGACY_ROW_RENDERED

    def test_render_sheet_is_exact_without_type_information(self):
        assert _render_sheet("道具表", LEGACY_SHEET, max_rows=20) == LEGACY_SHEET_RENDERED

    def test_csv_has_no_cell_types_and_renders_like_before(self):
        """CSV 的每一格本来都是文本，没有「原始类型」这回事 —— 整条链路走老路。"""
        service = DiffService()
        data = service._read_excel_data(b"id,val\n1,100\n", "config/x.csv")
        assert data.cell_types == {}

        payload = service.process_diff(
            "config/x.csv", b"id,name,level,val\n1,a,10,200\n",
            b"id,name,level,val\n1,a,10,100\n", key_columns=KEY_COLUMNS)
        sheet = payload["sheets"]["Sheet1"]

        assert sheet["rows"][0]["cell_changes"] == [
            {"column": "val", "old_value": "100", "new_value": "200"}
        ]
        assert _render_sheet("Sheet1", sheet, max_rows=20) == [
            "### 工作表「Sheet1」",
            "- 本次：新增 0、删除 0、修改 1（表内共 1 行差异记录）。",
            "- [修改] 第 2 行：val: 100 → 200",
        ]
