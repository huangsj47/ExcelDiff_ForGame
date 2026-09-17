# -*- coding: utf-8 -*-
"""整行增删之后，行的配对与「改前值」的归属。

## 背景（线上 200 条抽查，本地重跑引擎逐条复核）

引擎的行配对是「完全相同的行当锚点 → 段内 DP 对齐」。整行增删之后，行号会整体平移，
配对结果分三种情况：

1. **同一逻辑行、只是位置变了**（线上 6311：一行下移 3 位，前 3 列 3/3 相同）——
   配对是对的，只是载荷里报的是**当前版**的行号，基线里那一行的行号不同；
2. **插入 + 后续行改号**（线上 6405/6402「90_新手关卡流程.xlsx」）——引擎按内容把
   改号后的行认回原行（`步骤id 5003→5004`），配对同样是对的；
3. **一大段近似重复行插入 + 改号**（线上 6094「【990】截图资源表」）——段内 DP 在
   近似重复里选出的是**整体最优**的对齐，个别行会与「内容最像的那一行」差几行
   （该记录 43 行修改里有 11 行的配对严格劣于最优）。

三种情况都**没有编造内容**：200 条记录 1046 处变更里，`new_value` 100% 落在当前版
报出的那一行那一列；503 处 `old_value` 100% 能在基线里找到（只是其中 78 处落在别的
行号上）。这条不变量是本文件的重点。

## 这些测试各钉什么

* `TestPairingInvariants`：无论哪种形态，**new 值必须落在报出的那一格、old 值必须是
  基线里真实存在的值、行号必须是 Excel 真实行号、未变更的行不出现** —— 这几条是
  「评审者照着改前/改后核对」的地基，任何配对算法改动都不许破坏。
* `TestInsertionDoesNotCascade`：纯插入（其余行内容不变）只报那一行新增，不产生级联
  「修改」。
* `TestRenumberAfterInsertion`：插入 + 后续改号时的**当前口径**（含行号归属漂移），
  配对算法若改动，这里要同步更新并说明为什么新口径更好。
* `TestNearDuplicateBlockInsertion`：近似重复段落里只钉不变量，**不钉具体配对** ——
  这一段的配对在数据上本就是歧义的，钉死会把一个歧义当成契约。
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
        frame.to_excel(writer, sheet_name="S", index=False)
    return buffer.getvalue()


def _sheet_rows(frame: pd.DataFrame):
    """Excel 原始网格（第 1 行是表头），用于按位置核对。"""
    header = [str(c) for c in frame.columns]
    body = [[("" if v is None else str(v)) for v in row] for row in frame.itertuples(index=False)]
    return [header] + body


def _compare(current: pd.DataFrame, previous: pd.DataFrame):
    service = DiffService()
    return service._compare_excel_data(
        service._read_excel_data(_xlsx_bytes(current), "config/x.xlsx"),
        service._read_excel_data(_xlsx_bytes(previous), "config/x.xlsx"),
        "config/x.xlsx",
    )


def _changes(payload, sheet="S"):
    out = []
    for row in payload["sheets"][sheet]["rows"]:
        for change in row.get("cell_changes") or []:
            out.append({
                "row_number": row["row_number"],
                "status": row["status"],
                "column": change["column"],
                "old_value": str(change.get("old_value") or ""),
                "new_value": str(change.get("new_value") or ""),
            })
    return out


def _column_index(rows, name):
    return rows[0].index(name) if name in rows[0] else None


def _assert_invariants(payload, current, previous, sheet="S"):
    cur_rows = _sheet_rows(current)
    prev_rows = _sheet_rows(previous)
    summary = payload["summary"]
    assert summary["total"] > 0, "这个用例没有比出任何变更 —— 用例本身失效了"

    def _live(rows, rn, column):
        ci = _column_index(rows, column)
        if ci is None or rn - 1 >= len(rows):
            return None
        row = rows[rn - 1]
        return row[ci] if ci < len(row) else None

    for row in payload["sheets"][sheet]["rows"]:
        rn = row["row_number"]
        assert rn >= 2, f"行号必须是 Excel 真实行号（≥2），拿到 {rn}"
        if row["status"] == "added":
            for column, value in (row.get("data") or {}).items():
                if not str(value):
                    continue
                assert _live(cur_rows, rn, column) == str(value), (
                    f"新增行 r{rn} 的 {column!r} 报成 {value!r}，当前版同一格是 "
                    f"{_live(cur_rows, rn, column)!r}"
                )
        elif row["status"] == "removed":
            for column, value in (row.get("data") or {}).items():
                if not str(value):
                    continue
                assert _live(prev_rows, rn, column) == str(value), (
                    f"删除行 r{rn} 的 {column!r} 报成 {value!r}，基线同一格是 "
                    f"{_live(prev_rows, rn, column)!r}"
                )
        for change in row.get("cell_changes") or []:
            column = change["column"]
            new_value = str(change.get("new_value") or "")
            old_value = str(change.get("old_value") or "")
            if new_value:
                live = _live(cur_rows, rn, column)
                assert live == new_value, (
                    f"报出的 new 值 {new_value!r} 不在当前版 r{rn} 列 {column!r}（实际 {live!r}）"
                    " —— 配对错了：评审者会照着一个不存在的「改后值」去核对"
                )
            if old_value:
                ci_prev = _column_index(prev_rows, column)
                column_values = ([r[ci_prev] for r in prev_rows[1:] if ci_prev < len(r)]
                                 if ci_prev is not None else [v for r in prev_rows[1:] for v in r])
                assert old_value in column_values, (
                    f"报出的 old 值 {old_value!r} 在基线里根本不存在"
                    " —— 配对把别的表/别的列的值当成了「改前值」"
                )


# 线上 6405「90_新手关卡流程.xlsx」的形态：中间插一行，后面整体改号
RENUMBER_PREVIOUS = pd.DataFrame({
    "表": ["T"] * 4,
    "步骤id": [5001, 5002, 5003, 5004],
    "操作类型": ["A", "B", "SHOW_HIGHLIGHT", "WAIT_EVENT"],
    "参数": ["p1", "p2", "tgt=Panel", "key=ROLE"],
    "描述": ["d1", "d2", "d3", "d4"],
})
RENUMBER_CURRENT = pd.DataFrame({
    "表": ["T"] * 5,
    "步骤id": [5001, 5002, 5003, 5004, 5005],
    "操作类型": ["A", "B", "SHOW_PROMPT", "SHOW_HIGHLIGHT", "WAIT_EVENT"],
    "参数": ["p1", "p2", "tgt=Panel2", "tgt=Panel", "key=ROLE"],
    "描述": ["d1", "d2", "d3", "d3", "d4"],
})


def _near_duplicate_block(count=6):
    """一大段近似重复行（只有 id 与少量字段不同），用来复现 6094 的形态。"""
    prev = pd.DataFrame({
        "表": ["T"] * count,
        "ID": list(range(1, count + 1)),
        "类型": [3] * count,
        "资源ID": [100 + i for i in range(count)],
        "命名": ["存钱罐" if i % 2 else "钱袋" for i in range(count)],
        "帧数": [8] * count,
    })
    ins = pd.DataFrame({
        "表": ["T"] * 4,
        "ID": list(range(100, 104)),
        "类型": [3] * 4,
        "资源ID": [900 + i for i in range(4)],
        "命名": ["大型容器"] * 4,
        "帧数": [8] * 4,
    })
    cur = pd.concat([prev, ins], ignore_index=True)
    cur.loc[cur.index[:count], "ID"] = range(200, 200 + count)   # 原有行整体改号
    return cur, prev


SHAPES = {
    "纯插入": (pd.DataFrame({"表": ["T"] * 3, "ID": [1, 2, 3], "值": ["a", "b", "c"]}),
             pd.DataFrame({"表": ["T"] * 2, "ID": [1, 2], "值": ["a", "b"]})),
    "纯删除": (pd.DataFrame({"表": ["T"] * 2, "ID": [1, 2], "值": ["a", "b"]}),
             pd.DataFrame({"表": ["T"] * 3, "ID": [1, 2, 3], "值": ["a", "b", "c"]})),
    "插入+改号": (RENUMBER_CURRENT, RENUMBER_PREVIOUS),
    "近似重复段插入": _near_duplicate_block(),
    "改一格": (pd.DataFrame({"表": ["T"] * 3, "ID": [1, 2, 3], "值": ["a", "X", "c"]}),
             pd.DataFrame({"表": ["T"] * 3, "ID": [1, 2, 3], "值": ["a", "b", "c"]})),
    "整行替换": (pd.DataFrame({"表": ["T"] * 3, "ID": [1, 2, 3], "值": ["a", "X", "Y"]}),
             pd.DataFrame({"表": ["T"] * 3, "ID": [1, 2, 3], "值": ["a", "b", "c"]})),
}


class TestPairingInvariants:
    @pytest.mark.parametrize("name", sorted(SHAPES))
    def test_new_value_always_holds_and_old_value_always_exists(self, name):
        current, previous = SHAPES[name]
        _assert_invariants(_compare(current, previous), current, previous)

    def test_identical_rows_produce_no_output(self):
        frame = pd.DataFrame({"表": ["T"] * 3, "ID": [1, 2, 3], "值": ["a", "b", "c"]})
        payload = _compare(frame, frame)
        assert payload["summary"] == {"added": 0, "removed": 0, "modified": 0, "total": 0}, (
            f"完全相同的两版报出了变更：{payload['summary']}"
        )

    def test_pure_row_reorder_produces_no_output(self):
        """只换了行序、内容没动 → 不报变更。

        这是本引擎的既有口径（同内容的行是锚点，不分位置）。配表里行序有时也有语义
        （例如流程表的步骤表顺序），这一条如果哪天要改，属于口径变更、不是修 bug：
        需要先想清楚「整表重排」要不要逐行报「删一行 + 加一行」。
        """
        current = pd.DataFrame({"表": ["T"] * 3, "ID": [2, 1, 3], "值": ["b", "a", "c"]})
        previous = pd.DataFrame({"表": ["T"] * 3, "ID": [1, 2, 3], "值": ["a", "b", "c"]})
        payload = _compare(current, previous)
        assert payload["summary"] == {"added": 0, "removed": 0, "modified": 0, "total": 0}

    def test_the_invariant_helper_actually_fails_on_a_tampered_payload(self):
        """不变量助手自己也要能被证伪：改坏一处 new 值、改坏一处 old 值都必须被抓到。"""
        current, previous = SHAPES["改一格"]
        payload = _compare(current, previous)
        _assert_invariants(payload, current, previous)          # 原样通过

        import copy
        tampered = copy.deepcopy(payload)
        tampered["sheets"]["S"]["rows"][0]["cell_changes"][0]["new_value"] = "不存在的值"
        with pytest.raises(AssertionError):
            _assert_invariants(tampered, current, previous)

        tampered = copy.deepcopy(payload)
        tampered["sheets"]["S"]["rows"][0]["cell_changes"][0]["old_value"] = "任意编造的值"
        with pytest.raises(AssertionError):
            _assert_invariants(tampered, current, previous)


class TestInsertionDoesNotCascade:
    def test_only_the_inserted_row_is_reported(self):
        previous = pd.DataFrame({"表": ["T"] * 3, "ID": [1, 2, 3], "值": ["a", "b", "c"]})
        current = pd.DataFrame({"表": ["T"] * 4, "ID": [1, 9, 2, 3], "值": ["a", "新", "b", "c"]})
        payload = _compare(current, previous)

        assert payload["summary"] == {"added": 1, "removed": 0, "modified": 0, "total": 1}, (
            f"纯插入产生了级联变更：{payload['summary']}\n"
            "（线上 6685 那种「插一列/一行就一串修改」就是这一类的旧形态）"
        )
        _assert_invariants(payload, current, previous)


class TestRenumberAfterInsertion:
    """插入 + 后续改号的**当前口径**：按内容把改号后的行认回原行。

    因此载荷里会出现 `步骤id 5003→5004` 这样的「修改」，而它的 old 值在基线里的行号
    比报出的行号**小 1**（基线里那一行在插入点之前）。页面显示的是当前版行号，
    这一点载荷没说明 —— 评审者拿行号回基线文件核对时会对不上。
    配对算法若改动，这里要同步更新，并在提交信息里说明为什么新口径更好。
    """

    def test_current_behaviour_is_content_based_pairing_with_row_number_drift(self):
        payload = _compare(RENUMBER_CURRENT, RENUMBER_PREVIOUS)
        assert payload["summary"] == {"added": 1, "removed": 0, "modified": 2, "total": 2 + 1}
        assert [c["column"] for c in _changes(payload)] == ["步骤id", "步骤id"]
        assert ["%s→%s" % (c["old_value"], c["new_value"]) for c in _changes(payload)] == [
            "5003→5004", "5004→5005"
        ], "改号后的行没有被认回原行（口径变了就得重新评估）"
        _assert_invariants(payload, RENUMBER_CURRENT, RENUMBER_PREVIOUS)

    def test_old_value_row_number_is_smaller_than_the_reported_one(self):
        payload = _compare(RENUMBER_CURRENT, RENUMBER_PREVIOUS)
        prev_rows = _sheet_rows(RENUMBER_PREVIOUS)
        for change in _changes(payload):
            column = prev_rows[0].index(change["column"])
            where = [i + 1 for i, row in enumerate(prev_rows[1:], start=1)
                     if column < len(row) and row[column] == change["old_value"]]
            assert where == [change["row_number"] - 1], (
                f"改号行的 old 值在基线第 {where} 行，报出的行号是 {change['row_number']}"
                " —— 这段的口径变了，请同步更新本用例与说明"
            )


class TestNearDuplicateBlockInsertion:
    """近似重复段落里只钉不变量：这一段的配对在数据上本就是歧义的。"""

    def test_block_insertion_keeps_the_invariants(self):
        current, previous = _near_duplicate_block()
        payload = _compare(current, previous)
        _assert_invariants(payload, current, previous)
        assert payload["summary"]["added"] >= 4, (
            f"插入的 4 行没有被报成新增：{payload['summary']}"
        )

    def test_pairing_may_be_offset_within_near_duplicates(self):
        """**这是当前口径的已知限制**，不是契约：段内 DP 求的是整段最优，

        在近似重复行里个别行会与「内容最像的那一行」差几行（线上 6094：43 行修改里
        11 行的配对严格劣于最优）。这里只钉住「差几行也仍在同一段里」——
        一旦配对算法改动，请用 `pair_audit*` 那套离线对账重新评估，再更新本用例。
        """
        current, previous = _near_duplicate_block()
        payload = _compare(current, previous)
        prev_rows = _sheet_rows(previous)
        offsets = []
        for change in _changes(payload):
            column = prev_rows[0].index(change["column"])
            where = [i + 1 for i, row in enumerate(prev_rows[1:], start=1)
                     if column < len(row) and row[column] == change["old_value"]]
            if where:
                offsets.append(change["row_number"] - where[0])
        assert offsets, "没有可核对 old 值归属的变更 —— 用例失效了"
        assert max(abs(o) for o in offsets) <= len(current), (
            f"old 值跑到了离谱的行上：位移 {sorted(offsets)[:5]}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
