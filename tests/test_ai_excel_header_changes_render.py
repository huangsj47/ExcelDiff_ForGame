# -*- coding: utf-8 -*-
"""配表列级变更（`header_changes`）必须进入 AI 文本，删列不能变成「整列逐行被清空」。

## 缺陷形态（方案 §4.7 客户端第 1 条）

`diff_excel_compare.py:321-375` 早就算出了 `sheet["header_changes"]`
（`renamed` / `added` / `removed`），但 `services/ai/excel_view.py` 的 `_render_sheet`
只渲染 `rows` + `header_rows`，列变更在这条路上**整段丢掉**。后果比「少说一句」严重：

* **改名**被 `_pair_columns` 的 `mapped` 抹平（上一版列名先改写成新名再逐格比），
  两侧值一样 ⇒ `rows` 是空的 ⇒ 模型读到「这次提交什么都没改」，而列改名很可能
  让导表/代码取值对不上；
* **删列**被 `reindex(fill_value='')` 补空 ⇒ 每一行都多一条「旧值 →（空）」⇒
  模型读到「有人把整列数据逐行清空了」，结论方向直接反了。

## 现在的口径

* 列变更独立成段（`列变更（N 处）` + `列改名` / `新增列` / `删除列` 明细），
  排在统计之后、表头块与数据行之前；
* 删列连带的「旧值 →（空）」在行渲染里跳过（该列的值以「取值示例」留在删除列那一行，
  不至于连「这一列原来有数据」都看不到）；
* `header_changes` 缺失/为空时，输出与改动前**逐字相同**（本文件钉住一份改动前的
  实际输出，见 `LEGACY_RENDERED`）。
"""
from __future__ import annotations

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
from services.diff_service import DiffService  # noqa: E402

TABLE = "config/[30]道具表_CfgItem.xlsx"


def _render(sheet, *, name="S", max_rows=20):
    return "\n".join(_render_sheet(name, sheet, max_rows=max_rows))


def _xlsx(header_row, data_rows, sheet="S"):
    """直接写原始行（第 1 行是表头）—— 列身份就是第 1 行，改列名必须能构造出来。"""
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(list(header_row))
    for row in data_rows:
        worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _sheet(previous_bytes, current_bytes, name="S"):
    payload = DiffService().process_diff(TABLE, current_bytes, previous_bytes)
    assert payload.get("type") == "excel", payload
    return payload["sheets"][name]


# ---------------------------------------------------------------------------
#  「列变更存在时」的行为
# ---------------------------------------------------------------------------


class TestRenamedColumnIsNotSilent:
    """列改名原先表现为「零差异」—— 必须明说这是列级变更，且新旧名都在。"""

    SHEET = {
        "rows": [],
        "stats": {"added": 0, "removed": 0, "modified": 0},
        "headers": ["id", "新名"],
        "header_changes": [
            {"change": "renamed", "column_index": 2, "column": "新名",
             "old_name": "旧名", "new_name": "新名"},
        ],
    }

    def test_the_rename_is_rendered_as_a_column_level_change(self):
        text = _render(self.SHEET)

        assert "列变更（1 处）" in text, text
        assert "列改名" in text, text
        assert "第 2 列" in text, text
        assert "「旧名」→「新名」" in text, (
            f"改名的旧名/新名没有成对出现，模型分不清是哪两列换了名字：{text}"
        )

    def test_a_rename_only_submission_no_longer_reads_as_no_change(self):
        text = _render(self.SHEET)

        assert "没有差异行" not in text, (
            f"只改列名的提交仍被渲染成「没有差异行」—— 报告会写成本次无改动：{text}"
        )

    def test_the_real_compare_payload_carries_the_same_information(self):
        """真实 `DiffService` 的产出（rows 为空）也要同样渲染 —— 钉住形状不漂。"""
        previous = _xlsx(["id", "保险箱模态房比例"], [["1", "all"], ["2", "all"]])
        current = _xlsx(["id", "双人风轮ID"], [["1", "all"], ["2", "all"]])
        sheet = _sheet(previous, current)
        assert sheet["rows"] == [], f"fixture 不再是「只改名」的形状：{sheet['rows']}"

        text = _render(sheet)

        assert "列改名" in text, text
        assert "「保险箱模态房比例」→「双人风轮ID」" in text, text
        assert "没有差异行" not in text, text


class TestAddedColumnIsNamed:
    SHEET = {
        "rows": [
            {"row_number": 2, "status": "modified",
             "cell_changes": [{"column": "新列", "old_value": "", "new_value": "N1"}]},
        ],
        "stats": {"added": 0, "removed": 0, "modified": 1},
        "headers": ["id", "a", "新列", "b"],
        "header_changes": [
            {"change": "added", "column_index": 3, "column": "新列",
             "old_name": "", "new_name": "新列"},
        ],
    }

    def test_the_added_column_is_announced_with_its_position(self):
        text = _render(self.SHEET)

        assert "列变更（1 处）" in text, text
        assert "新增列" in text, text
        assert "第 3 列「新列」" in text, text

    def test_the_inserted_values_are_still_listed_per_row(self):
        """新增列的逐行「（空）→ 值」**保留**：它同时说明哪几行没填（漏配的一种）。"""
        text = _render(self.SHEET)

        assert "新列: （空） → N1" in text, text


class TestRemovedColumnDoesNotFloodEveryRow:
    """删列原先被渲染成「整列每一行的 旧值 →（空）」—— 一整片假变更。"""

    def _sheet(self, rows):
        return {
            "rows": rows,
            "stats": {"added": 0, "removed": 0, "modified": len(rows)},
            "headers": ["id", "a", "备注"],
            "header_changes": [
                {"change": "removed", "column_index": 3, "column": "备注",
                 "old_name": "备注", "new_name": ""},
            ],
        }

    def _rows(self):
        return [
            {"row_number": number, "status": "modified",
             "cell_changes": [{"column": "备注", "old_value": value, "new_value": ""}]}
            for number, value in ((2, "old1"), (3, "old2"), (4, "old3"))
        ]

    def test_the_deleted_column_is_named_as_a_column_level_change(self):
        text = _render(self._sheet(self._rows()))

        assert "列变更（1 处）" in text, text
        assert "删除列" in text, text
        assert "上一版第 3 列「备注」" in text, (
            f"删列的序号是上一版的，必须标明是哪一版：{text}"
        )

    def test_the_column_is_not_repeated_as_per_row_cell_changes(self):
        text = _render(self._sheet(self._rows()))

        for value in ("old1", "old2", "old3"):
            assert f"备注: {value} → （空）" not in text, (
                f"删列仍被渲染成整列逐行的假变更（{value}）：{text}"
            )
        assert "[修改]" not in text, (
            f"只因为删列才变成「修改」的行仍被当作行改动列出来：{text}"
        )

    def test_the_old_values_are_not_thrown_away(self):
        """跳过假变更 ≠ 丢掉证据：那一列上一版填过什么，仍要能读到。"""
        text = _render(self._sheet(self._rows()))

        assert "取值示例" in text, text
        for value in ("old1", "old2", "old3"):
            assert value in text, f"删掉的那一列原来的取值没有了：{text}"

    def test_the_note_explains_why_no_rows_are_listed(self):
        text = _render(self._sheet(self._rows()))

        assert "不再重复算作行数据改动" in text, text

    def test_a_real_change_in_another_column_survives_the_skip(self):
        rows = [
            {"row_number": 3, "status": "modified", "cell_changes": [
                {"column": "备注", "old_value": "old", "new_value": ""},
                {"column": "a", "old_value": "x", "new_value": "X"},
            ]},
        ]

        text = _render(self._sheet(rows))

        assert "a: x → X" in text, f"真实的行改动被删列过滤一起吞掉了：{text}"
        assert "备注: old" not in text, text
        assert "[修改] 第 3 行：a: x → X" in text, text

    def test_the_header_block_does_not_repeat_it_either(self):
        """多行表头的仓库里，表头块会为同一列再报一遍「旧值 →（空）」—— 同一份副本。"""
        previous = _xlsx(["id", "a", "备注"], [["编号", "甲", "备注名"], ["1", "x", "old1"]])
        current = _xlsx(["id", "a"], [["编号", "甲"], ["1", "x"]])
        payload = DiffService().process_diff(TABLE, current, previous, header_rows=2)
        sheet = payload["sheets"]["S"]
        assert sheet["header_changes"], f"fixture 没有列变更：{sheet}"

        text = _render(sheet)

        assert "备注: 备注名 → （空）" not in text, text
        assert "处改动" not in text, f"表头块只因为删列才「有改动」：{text}"
        assert "删除列" in text and "old1" in text, text

    def test_the_real_compare_payload_has_the_same_shape(self):
        previous = _xlsx(["id", "a", "备注"], [["1", "x", "old1"], ["2", "y", "old2"]])
        current = _xlsx(["id", "a"], [["1", "x"], ["2", "y"]])
        sheet = _sheet(previous, current)
        assert [c["change"] for c in sheet["header_changes"]] == ["removed"]

        text = _render(sheet)

        assert "删除列" in text, text
        assert "备注: old1 → （空）" not in text, text
        assert "old1" in text, f"删列的上一版取值被丢掉了：{text}"


class TestDedupColumnNameStillMatchesTheRowData:
    """列变更里的重名列展示名（`X（同名列第 2 个）`）与行里的原始名（`X.1`）要能对上。

    对不上时过滤一条都命中不了，删列的整片假变更会原样继续出现 ——
    这正是 `diff_excel_compare._display_column_name` 与 `cell_changes` 两套写法的接缝。
    """

    SHEET = {
        "rows": [
            {"row_number": 3, "status": "modified",
             "cell_changes": [{"column": "From.79", "old_value": "v", "new_value": ""}]},
        ],
        "headers": ["From", "From.1"],
        "header_changes": [
            {"change": "removed", "column_index": 80,
             "column": "From（同名列第 80 个）",
             "old_name": "From（同名列第 80 个）", "new_name": ""},
        ],
    }

    def test_the_row_level_change_is_skipped(self):
        text = _render(self.SHEET)

        assert "From.79: v → （空）" not in text, text
        assert "取值示例：v" in text, text


class TestColumnChangesAreAnIndependentParagraph:
    """模型必须一眼看出「这是列结构变化」，且读在行数据之前。"""

    SHEET = {
        "rows": [
            {"row_number": 3, "status": "modified",
             "cell_changes": [{"column": "a", "old_value": "x", "new_value": "X"}]},
        ],
        "stats": {"added": 0, "removed": 0, "modified": 1},
        "headers": ["id", "a", "备注"],
        "header_changes": [
            {"change": "removed", "column_index": 3, "column": "备注",
             "old_name": "备注", "new_name": ""},
        ],
    }

    def test_it_comes_before_the_row_lines(self):
        text = _render(self.SHEET)

        assert text.index("列变更") < text.index("[修改]"), text

    def test_the_platform_render_path_carries_it_too(self):
        text = render_diff_payload({
            "type": "excel", "file_path": TABLE,
            "sheets": {"道具表": dict(self.SHEET, header_changes=[])},
        })
        assert "列变更" not in text, text

        text = render_diff_payload({
            "type": "excel", "file_path": TABLE,
            "sheets": {"道具表": self.SHEET},
        })
        assert "列变更（1 处）" in text and "删除列" in text, text


# ---------------------------------------------------------------------------
#  「列变更不存在时」的输出必须与改动前逐字相同
# ---------------------------------------------------------------------------

# 改动前 `_render_sheet("道具表", LEGACY_SHEET, max_rows=20)` 的实际输出
# （含 `headers`：这次改动不许因为读到 headers 而无条件多渲染一行）。
LEGACY_RENDERED = [
    "### 工作表「道具表」",
    "- 本次：新增 1、删除 0、修改 1（表内共 3 行差异记录）。",
    "- 表头 2 行中有 1 处改动。",
    "- [修改] 第 1 行：ID: id → ID",
    "- [新增] 第 2 行：{ID=1001, 攻击=30}",
    "- [修改] 第 3 行：攻击: 30 → 45",
]

LEGACY_SHEET = {
    "rows": [
        {"row_number": 2, "status": "added", "data": {"ID": "1001", "攻击": "30"}},
        {"row_number": 3, "status": "modified",
         "cell_changes": [{"column": "攻击", "old_value": "30", "new_value": "45"}]},
        {"row_number": 4, "status": "unchanged", "data": {"ID": "1"}},
    ],
    "header_rows": [
        {"row_number": 1, "status": "modified",
         "cell_changes": [{"column": "ID", "old_value": "id", "new_value": "ID"}]},
        {"row_number": 2, "status": "unchanged", "data": {"ID": "id"}},
    ],
    "stats": {"added": 1, "removed": 0, "modified": 1},
    "headers": ["ID", "攻击"],
}


class TestWithoutHeaderChangesNothingChanges:
    @pytest.mark.parametrize("extra", [
        {},
        {"header_changes": None},
        {"header_changes": []},
        {"header_changes": "形状不认识"},
    ])
    def test_output_is_byte_identical_to_the_previous_version(self, extra):
        assert _render_sheet("道具表", dict(LEGACY_SHEET, **extra), max_rows=20) == LEGACY_RENDERED

    def test_render_row_keeps_its_old_behaviour_by_default(self):
        """`_render_row` 是回导给 `platform_provider` 的旧名字：默认不跳过任何列。"""
        row = {"row_number": 3, "status": "modified",
               "cell_changes": [{"column": "备注", "old_value": "old1", "new_value": ""}]}

        assert _render_row(row) == "- [修改] 第 3 行：备注: old1 → （空）"
