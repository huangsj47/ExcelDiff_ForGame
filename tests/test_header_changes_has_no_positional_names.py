# -*- coding: utf-8 -*-
"""列变更提示里不能出现**位置型伪名**，也不能自相矛盾。

## 缺陷形态（线上 200 条抽查，真值仓库逐格复核）

`header_changes` 是「列级变更提示」的数据源（模板渲染成
`新增列 X；删除列 Y；列名 A → B`）。它的 added/removed 来自**未配对**的列，
而未配对在很多情况下只是**位置**造成的，不是有人改了列名：

| 记录 | 平台报出的列变更 | 真相 |
|---|---|---|
| 6685 `91_专项教学关.xlsx`「新手关卡」 | 13 条：`出生点id`、`完成进度增加`、`标题` 等既报新增又报删除 | 只插了一个「出生点id」列 |
| 6095 `BuffCfg_Buff配置.xlsx` | 95 条，其中 38 个列号同时 added+removed，`属性修改.1`、`定时生效.2` 这类名字成串 | 整段重复列名错位 |
| 6225 `主角状态定义以及规则表.xlsx` | `新增列 From.79 / From.80 / From.81` | 表里有 82 个 `From` 列，`.79` 是 pandas 的**出现次序**后缀，不是列名 |

第 3 类尤其误导：评审者会去表里找一个叫 `From.79` 的列，而那个名字根本不在文件里。

## 这些测试各钉什么

* `TestInsertedColumnIsTheOnlyChange`：插一列只报那一列（同名跨列位不算增删）。
* `TestPlaceholderPositionsAreNotNames`：空表头列的占位名不进提示。
* `TestDuplicateHeaderSuffixesAreShownAsOccurrences`：`X.N` 还原成「X（同名列第 N+1 个）」。
* `TestRealColumnChangesStillReported`：真新增/删除/改名一条都不能少。
* `TestEndToEnd`：走真实读取（pandas 的去重命名）跑一遍线上形态。
"""
from __future__ import annotations

import io
import os
import sys

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402


def _xlsx_bytes(frame: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Sheet1", index=False)
    return buffer.getvalue()


def _frame(columns, rows):
    """表头行 + 数据行 → 用 header=0 读回来时列名就是 columns。"""
    return pd.DataFrame(rows, columns=columns)


def _changes(current_columns, current_rows, previous_columns, previous_rows):
    service = DiffService()
    current = _frame(current_columns, current_rows)
    previous = _frame(previous_columns, previous_rows)
    result = service._detailed_dataframe_comparison(current, previous)
    return result.get("header_changes") or []


def _labels(changes):
    return [(c["change"], c["column"]) for c in changes]


class TestInsertedColumnIsTheOnlyChange:
    """前面插一列 → 后面整体后移；同名跨列位不算「新增+删除」。"""

    def test_shifted_columns_are_not_reported_as_add_and_remove(self):
        changes = _changes(
            ["ID", "出生点id", "完成进度增加", "标题"],
            [["1", "p1", "10", "t"]],
            ["ID", "完成进度增加", "标题"],
            [["1", "10", "t"]],
        )
        assert _labels(changes) == [("added", "出生点id")], (
            f"插一列却报了 {len(changes)} 条列变更：{[c['column'] for c in changes]}\n"
            "（线上 6685 就是插一个「出生点id」报了 13 条，其中「完成进度增加」「标题」"
            "既报新增又报删除）"
        )

    def test_no_column_index_is_reported_both_ways(self):
        changes = _changes(
            ["ID", "新", "B", "C"],
            [["1", "x", "2", "3"]],
            ["ID", "B", "C"],
            [["1", "2", "3"]],
        )
        both = {}
        for change in changes:
            both.setdefault(change["column_index"], set()).add(change["change"])
        conflicting = {i: k for i, k in both.items() if {"added", "removed"} <= k}
        assert not conflicting, f"同一个列号同时被报新增与删除：{conflicting}"


class TestPlaceholderPositionsAreNotNames:
    """空表头的列：占位名带的是位置，不是身份。"""

    def test_placeholder_names_do_not_enter_the_hint(self):
        # 上一版第 2 列表头为空（Unnamed: 1），当前版插了一列，空表头列整体后移
        changes = _changes(
            ["ID", "新列", "Unnamed: 2", "尾巴"],
            [["1", "x", "", "z"]],
            ["ID", "Unnamed: 1", "尾巴"],
            [["1", "", "z"]],
        )
        assert _labels(changes) == [("added", "新列")], (
            f"空表头列的占位名进了提示：{[c['column'] for c in changes]}"
        )

    def test_literal_text_that_looks_like_a_placeholder_is_still_suppressed(self):
        """表里真的写着 `Unnamed: 8` 这种文本时（6685 就是）也不报：

        它与 pandas 的占位名在数据层无法区分，而报出来的效果一样糟
        （评审者会去找一个「Unnamed: 8」列）。列的值照常在表格里显示。
        """
        changes = _changes(
            ["ID", "Unnamed: 8", "Unnamed: 9"],
            [["1", "", ""]],
            ["ID", "Unnamed: 8"],
            [["1", ""]],
        )
        assert [c["column"] for c in changes] == [], (
            f"占位形态的列名被报成列变更：{[c['column'] for c in changes]}"
        )


class TestDuplicateHeaderSuffixesAreShownAsOccurrences:
    """`X.N` 是 pandas 的重复列名后缀（出现次序），不是列名。"""

    def test_suffix_is_rendered_as_an_occurrence(self):
        # 表头里有 3 个同名 `From`，第 2 个起被 pandas 改名成 `From.1` / `From.2`
        current = ["From", "From.1", "From.2", "From.3"]
        previous = ["From", "From.1", "From.2"]
        changes = _changes(
            current,
            [["a", "b", "c", "d"]],
            previous,
            [["a", "b", "c"]],
        )
        assert _labels(changes) == [("added", "From（同名列第 4 个）")], (
            f"重复列名的出现次序没有还原：{[c['column'] for c in changes]}"
        )

    def test_a_real_column_literally_named_x_dot_n_is_kept(self):
        """表里真有一列就叫 `X.1`（而没有 `X`）时不许改写成「同名列」——那是它的真名。"""
        changes = _changes(
            ["id", "Type.1"],
            [["1", "v"]],
            ["id"],
            [["1"]],
        )
        assert _labels(changes) == [("added", "Type.1")], (
            f"真名字被当成去重后缀改写了：{[c['column'] for c in changes]}"
        )

    def test_renamed_pairs_use_the_same_rendering(self):
        changes = _changes(
            ["id", "From", "From.1", "新名字"],
            [["1", "a", "b", "c"]],
            ["id", "From", "From.1", "旧名字"],
            [["1", "a", "b", "c"]],
        )
        assert ("renamed", "新名字") in _labels(changes)
        assert all("From" not in str(c.get("old_name")) for c in changes if c["change"] == "renamed")


class TestRealColumnChangesStillReported:
    """提示要短，但不能把真变更一起吞掉。"""

    def test_genuinely_added_and_removed_columns_are_reported(self):
        # 增删数量不等（一段 2 列 ↔ 一段 1 列）：这时才按新增/删除报
        # （数量相等时是「改名」，见 TestRealColumnChangesStillReported 的改名用例）
        changes = _changes(
            ["ID", "保留", "新增的列", "还有一个新列"],
            [["1", "a", "x", "y"]],
            ["ID", "保留", "要删的列"],
            [["1", "a", "z"]],
        )
        assert ("added", "新增的列") in _labels(changes), f"真新增列被吞了：{_labels(changes)}"
        assert ("added", "还有一个新列") in _labels(changes)
        assert ("removed", "要删的列") in _labels(changes)

    def test_renamed_column_is_reported(self):
        changes = _changes(
            ["ID", "保险箱模态房比例"],
            [["1", "0.5"]],
            ["ID", "双人风轮ID"],
            [["1", "0.5"]],
        )
        assert any(c["change"] == "renamed" and c["old_name"] == "双人风轮ID"
                   and c["new_name"] == "保险箱模态房比例" for c in changes), (
            f"列改名没有被报出来：{changes}"
        )

    def test_column_index_is_one_based_and_matches_excel_order(self):
        changes = _changes(
            ["ID", "保留", "新增的列"],
            [["1", "a", "x"]],
            ["ID", "保留"],
            [["1", "a"]],
        )
        assert changes[0]["column_index"] == 3, "列号必须是从 1 开始的 Excel 列序"


class TestEndToEnd:
    """走真实读取：pandas 的重复列名后缀由它自己生成（不靠测试构造）。"""

    def test_duplicate_from_columns_are_reported_as_occurrences(self):
        base = _xlsx_bytes(_frame(["id", "From", "From"], [["1", "a", "b"]]))
        current = _xlsx_bytes(_frame(["id", "From", "From", "From"], [["1", "a", "b", "c"]]))
        payload = DiffService()._compare_excel_data(
            DiffService()._read_excel_data(current, "config/x.xlsx"),
            DiffService()._read_excel_data(base, "config/x.xlsx"),
            "config/x.xlsx",
        )
        changes = payload["sheets"]["Sheet1"]["header_changes"]
        assert [c["column"] for c in changes] == ["From（同名列第 3 个）"], (
            f"线上 6225 的形态没有还原：{[c['column'] for c in changes]}"
        )

    def test_inserted_column_with_shifted_duplicates_reports_one_change(self):
        base = _xlsx_bytes(_frame(["id", "B", "C"], [["1", "b", "c"]]))
        current = _xlsx_bytes(_frame(["id", "新", "B", "C"], [["1", "n", "b", "c"]]))
        payload = DiffService()._compare_excel_data(
            DiffService()._read_excel_data(current, "config/x.xlsx"),
            DiffService()._read_excel_data(base, "config/x.xlsx"),
            "config/x.xlsx",
        )
        changes = payload["sheets"]["Sheet1"]["header_changes"]
        assert [c["column"] for c in changes] == ["新"], (
            f"插一列报了多条：{[(c['change'], c['column']) for c in changes]}"
        )

    def test_the_6685_shape_reports_only_the_inserted_column(self):
        """线上 6685 的完整形态：插一列 + 表里写着 `Unnamed: 8` 这类文本表头。

        修前平台报 13 条，其中「完成进度增加」「标题」既报新增又报删除、
        `Unnamed: 10` 在同一个列号上两头都报。
        """
        previous_columns = ["CfgTutorialChapter", "ID", "类型", "新手",
                            "完成进度增加", "标题", "Unnamed: 8", "Unnamed: 9"]
        current_columns = ["CfgTutorialChapter", "ID", "类型", "新手", "出生点id",
                           "完成进度增加", "标题", "Unnamed: 10", "Unnamed: 11",
                           "Unnamed: 8", "Unnamed: 9"]
        rows_prev = [[str(i) for i in range(len(previous_columns))]]
        rows_cur = [[str(i) for i in range(len(current_columns))]]
        base = _xlsx_bytes(_frame(previous_columns, rows_prev))
        current = _xlsx_bytes(_frame(current_columns, rows_cur))

        payload = DiffService()._compare_excel_data(
            DiffService()._read_excel_data(current, "config/x.xlsx"),
            DiffService()._read_excel_data(base, "config/x.xlsx"),
            "config/x.xlsx",
        )
        changes = payload["sheets"]["Sheet1"]["header_changes"]

        assert [(c["change"], c["column"]) for c in changes] == [("added", "出生点id")], (
            f"6685 的形态没有被收敛成一条：{[(c['change'], c['column']) for c in changes]}"
        )
        both = {}
        for change in changes:
            both.setdefault(change["column_index"], set()).add(change["change"])
        assert not {i: k for i, k in both.items() if {"added", "removed"} <= k}, (
            "同一个列号又同时被报新增与删除"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
