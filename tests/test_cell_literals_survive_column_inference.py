# -*- coding: utf-8 -*-
"""单元格的字面量不能被**列级类型推断**改写。

## 缺陷形态（线上 6684 与同批语料）

`_read_excel_data` 用 `dtype=str, keep_default_na=False` 读表，防住了「字面量被当成类型
改写」（`'00123'`→123、`'1.10'`→1.1、`'TRUE'`→True —— 见该函数的注释）。但**列级**的
类型推断发生在它之前：一列里只要混进一个布尔，同列的数字与布尔就互相转换，
`dtype=str` 只是把转换后的结果转成字符串：

| 原始单元格 | 平台显示 | 出处 |
|---|---|---|
| 数字 `0` | `False` | 6684 `硬直类型表.xlsx`「清理吸灵器吸住状态」列，16 个格子 |
| 布尔 `TRUE` | `1` | 同批语料 `【50】武器模板表.xlsx`、`模型表_CfgModel.xlsx` 等 |

后果有两层，第二层更致命：页面显示的取值不是文件里的取值；而且两版之间只要有一版
多了一个布尔，同一个格子就会一边显示 `0`、一边显示 `False` —— 报出一条**没人改过的
变更**。变更确认平台上，假变更与漏报一样会让评审者去核对一个不存在的东西。

## 口径

读取仍然是 pandas `dtype=str`（既有口径不变），随后 `_repair_inferred_cells` 用
**原始单元格类型**（openpyxl `data_only=True`，同一份字节）把这两种被改写过的形态
还原，其余格子一律不动。

## 这些测试各钉什么

* `TestLiteralsSurviveTheColumnInference`：混布尔列里的数字还是数字、布尔还是布尔。
* `TestEmptyAndTextCellsAreUntouched`：空单元格仍是空、文本 `TRUE`/`null` 仍是文本。
* `TestNoPhantomChangeAcrossVersions`：一版多一个布尔，不能让同一格子变成一条假变更。
* `TestRepairIsFailureProof`：拿不到原始类型时保持既有读数，不能让整份读取失败。
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


def _xlsx_bytes(frame: pd.DataFrame, sheet_name: str = "Sheet1") -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=sheet_name, index=False)
    return buffer.getvalue()


def _read(frame: pd.DataFrame, sheet_name: str = "Sheet1") -> pd.DataFrame:
    """走产品读取路径（pandas dtype=str + 原始类型还原）。"""
    return DiffService()._read_excel_data(
        _xlsx_bytes(frame, sheet_name), "config/x.xlsx"
    )[sheet_name]


MIXED_BOOL_NUMBER = pd.DataFrame({
    "名称": ["TYPE", "DEFAULT", "测试id", "另一个"],
    "开关": [False, 0, 1, True],          # 布尔与数字混在同一列（线上 6684 的形态）
})


class TestLiteralsSurviveTheColumnInference:
    def test_numbers_in_a_boolean_column_stay_numbers(self):
        df = _read(MIXED_BOOL_NUMBER)
        values = [str(v) for v in df["开关"].tolist()]
        assert values == ["False", "0", "1", "True"], (
            f"混布尔列里的数字被改写了：{values}\n"
            "（数字 0 显示成 False、布尔 TRUE 显示成 1 —— 页面取值不是文件里的取值）"
        )

    def test_boolean_cells_stay_booleans(self):
        df = _read(MIXED_BOOL_NUMBER)
        assert str(df["开关"].iloc[0]) == "False", "布尔 FALSE 应显示 False"
        assert str(df["开关"].iloc[3]) == "True", "布尔 TRUE 应显示 True"

    def test_all_boolean_column_is_unchanged(self):
        df = _read(pd.DataFrame({"开关": [True, False, True], "id": ["a", "b", "c"]}))
        assert [str(v) for v in df["开关"].tolist()] == ["True", "False", "True"]

    def test_all_number_column_is_unchanged(self):
        df = _read(pd.DataFrame({"数量": [0, 1, 1000], "id": ["a", "b", "c"]}))
        assert [str(v) for v in df["数量"].tolist()] == ["0", "1", "1000"]


class TestEmptyAndTextCellsAreUntouched:
    def test_empty_cells_stay_empty(self):
        df = _read(pd.DataFrame({"开关": [True, None, 0], "id": ["a", "b", "c"]}))
        # 空单元格：归一化后算空（'' 与 NaN 在 _normalize_value 里同义）
        assert DiffService()._normalize_value(df["开关"].iloc[1]) is None, (
            f"空单元格变成了取值：{df['开关'].iloc[1]!r}"
        )

    def test_text_literals_are_not_turned_into_types(self):
        df = _read(pd.DataFrame({
            "备注": ["TRUE", "null", "00123", "1.10"],
            "id": ["a", "b", "c", "d"],
        }))
        assert [str(v) for v in df["备注"].tolist()] == ["TRUE", "null", "00123", "1.10"], (
            "dtype=str 的口径被破坏了（文本被当成类型改写）"
        )

    def test_text_that_looks_numeric_next_to_a_boolean_is_kept(self):
        df = _read(pd.DataFrame({"混": [True, "1", "0"], "id": ["a", "b", "c"]}))
        assert [str(v) for v in df["混"].tolist()] == ["True", "1", "0"], (
            f"文本 '1'/'0' 被当成了布尔：{[str(v) for v in df['混'].tolist()]}"
        )


def _payload(current: pd.DataFrame, previous: pd.DataFrame):
    service = DiffService()
    return service._compare_excel_data(
        service._read_excel_data(_xlsx_bytes(current), "config/x.xlsx"),
        service._read_excel_data(_xlsx_bytes(previous), "config/x.xlsx"),
        "config/x.xlsx",
    )


class TestNoPhantomChangeAcrossVersions:
    """一版多了一个布尔 → 不能被报成「同一格子变了」。"""

    # 行匹配按相似度认「同一行」，所以除开关列外多给几列，让「改了一格」被认成修改行
    # （只有 2 列时相似度不足，引擎会把整行报成删除+新增，看不到 cell_changes）
    @staticmethod
    def _frame(ids, kinds, switches):
        return pd.DataFrame({
            "id": ids, "名称": [f"名字{i}" for i in range(len(ids))],
            "类型": kinds, "开关": switches,
        })

    def test_unchanged_numeric_cell_is_not_reported(self):
        previous = self._frame(["a", "b"], ["int", "int"], [0, 1])
        current = self._frame(["a", "b", "c"], ["int", "int", "bool"], [0, 1, True])
        payload = _payload(current, previous)

        sheet = payload["sheets"]["Sheet1"]
        reported = [
            (row["row_number"], change["column"], str(change["old_value"]), str(change["new_value"]))
            for row in sheet["rows"]
            for change in (row.get("cell_changes") or [])
        ]
        assert len(sheet["rows"]) >= 1, "载荷是空的 —— 这个用例没有真的比出东西来"
        assert not any(old == "0" and new == "False" for _r, _c, old, new in reported), (
            f"数字 0 被报成「0 → False」（没人改过这个格子）：{reported}"
        )

    def test_a_real_boolean_edit_is_still_reported(self):
        previous = self._frame(["a", "b"], ["bool", "bool"], [True, False])
        current = self._frame(["a", "b"], ["bool", "bool"], [True, True])
        payload = _payload(current, previous)

        sheet = payload["sheets"]["Sheet1"]
        pairs = [
            (str(change["old_value"]), str(change["new_value"]))
            for row in sheet["rows"]
            for change in (row.get("cell_changes") or [])
        ]
        assert pairs, f"整行真变更没有被报出来：{payload['summary']}"
        assert ("False", "True") in pairs, f"FALSE → TRUE 的取值没有出现在变更里：{pairs}"


class TestRepairIsFailureProof:
    def test_missing_raw_types_keep_the_pandas_reading(self):
        """拿不到原始单元格类型（openpyxl 打不开）时保持既有读数，不能整份读取失败。"""
        service = DiffService()
        frame = _frame = pd.DataFrame({"a": ["1"]})
        sheets = {"Sheet1": frame}
        assert service._repair_inferred_cells(b"not an excel workbook", sheets) is sheets

    def test_csv_path_has_no_sheet_to_repair(self):
        """CSV/TSV 没有布尔类型，走的是另一条分支（这里只钉住它不会被误伤）。"""
        service = DiffService()
        sheets = service._read_excel_data(b"id,name\n1,a\n", "config/x.csv")
        assert [str(v) for v in sheets["Sheet1"]["id"].tolist()] == ["1"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
