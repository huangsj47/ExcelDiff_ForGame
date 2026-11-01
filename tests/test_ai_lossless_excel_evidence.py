"""Source facts must survive BEFORE prompt pagination and completeness receipts."""
from services.ai.platform_provider import render_diff_payload


def test_every_changed_row_survives_source_rendering_before_pagination():
    rows = [{"status": "modified", "row_number": i,
             "cell_changes": [{"column": "price", "old_value": i, "new_value": i + 1}]}
            for i in range(1, 263)]
    rendered = render_diff_payload({"type": "excel", "sheets": {"Sheet1": {"rows": rows}}})
    assert "第 262 行" in rendered
    assert "未列出" not in rendered


def test_long_cell_keeps_value_tail_and_deleted_column_changes_are_not_lost():
    value = "x" * 300 + "IMPORTANT_TAIL"
    rendered = render_diff_payload({"type": "excel", "sheets": {"Sheet1": {"rows": [
        {"status": "modified", "row_number": 15, "cell_changes": [
            {"column": "value", "old_value": "old", "new_value": value}]}
    ]}}})
    assert value in rendered


def test_precise_rectangle_can_read_physical_row_after_default_preview_and_preserve_type():
    import io

    from openpyxl import Workbook

    from services.ai.excel_source import _read_excel_sheets
    book = Workbook()
    sheet = book.active
    sheet.title = "常量表"
    sheet.append(["name", "value"])
    for i in range(2, 263):
        sheet.append([f"variable_{i}", i])
    raw = io.BytesIO()
    book.save(raw)
    text = _read_excel_sheets(raw.getvalue(), max_rows=120, path="constants.xlsx",
                              window="常量表!A262:B262", char_budget=11000)
    assert "variable_262" in text
    assert "A262" in text and "B262" in text
    assert "variable_120" not in text
    assert "int" in text or "number" in text


def test_exact_cells_distinguish_true_blank_from_formula_without_cached_result():
    import io

    from openpyxl import Workbook

    from services.ai.excel_range import render_range
    book = Workbook()
    book.active["A262"] = 17
    book.active["C262"] = "=100/2"
    stream = io.BytesIO()
    book.save(stream)
    text = render_range(stream.getvalue(), "#1!A262:C262")
    assert '"cell": "B262"' in text
    assert '"type": "blank"' in text
    assert '"formula": "=100/2"' in text
    assert '"cached_status": "unknown"' in text


def test_exact_merged_follower_is_not_reported_as_an_unfilled_value():
    import io

    from openpyxl import Workbook

    from services.ai.excel_range import render_range
    book = Workbook()
    book.active.merge_cells("A1:B2")
    book.active["A1"] = "shared"
    stream = io.BytesIO()
    book.save(stream)
    text = render_range(stream.getvalue(), "#1!B2:B2")
    assert '"merged_anchor": "A1"' in text
    assert '"type": "merged_follower"' in text


def test_long_exact_cell_pages_reconstruct_complete_json_facts_without_losing_middle():
    import io
    import re

    from openpyxl import Workbook

    from services.ai.excel_range import render_range

    book = Workbook()
    original = "start" + "x" * 12000 + "MIDDLE_FACT" + "y" * 12000 + "end"
    book.active["B262"] = original
    raw = io.BytesIO()
    book.save(raw)
    pages, offset = [], 0
    while True:
        page = render_range(raw.getvalue(), f"#1!B262:B262@{offset}", char_budget=2000)
        assert len(page) <= 2000
        match = re.search(r"chars=(\d+)-(\d+)/(\d+)\n", page)
        assert match, page
        start, end, total = map(int, match.groups())
        assert start == offset and end > start
        pages.append(page[match.end():].rsplit("\n[矩形", 1)[0])
        if end == total:
            break
        assert f"@{end}" in page
        offset = end
    assert original in "".join(pages)
    assert len(pages) > 3


def test_wrong_sheet_name_returns_real_names_and_ordinal_for_immediate_correction():
    import io

    from openpyxl import Workbook

    from services.ai.excel_range import render_range

    book = Workbook()
    book.active.title = "Main"
    stream = io.BytesIO()
    book.save(stream)
    response = render_range(stream.getvalue(), "Sheet1!A1:B2")
    assert response.startswith("[取数失败]")
    assert "#1='Main'" in response
