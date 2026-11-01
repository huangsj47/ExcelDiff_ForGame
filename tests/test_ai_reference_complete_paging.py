"""Reference results must distinguish complete matching from bounded delivery."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from services.ai.reference_index import SnapshotReferenceIndex
from services.ai.repo_reference import search_frozen_repositories, search_frozen_repository


class Reader:
    allowed_paths = None

    def __init__(self, files, *, tip="abc", repository_id=1):
        self.files = files
        self.frozen = SimpleNamespace(repository_id=repository_id, tip=tip)

    def tracked_paths(self):
        return tuple(self.files)

    def read_bytes(self, path):
        return self.files[path]


def workbook(case):
    book = Workbook()
    sheet = book.active
    sheet.append(["id", "text"])
    if case == "row201":
        sheet.cell(201, 1, "hiddenNeedle")
    elif case == "sheet4":
        for _ in range(3):
            book.create_sheet()
        book.worksheets[3].cell(1, 1, "hiddenNeedle")
    elif case == "cell161":
        sheet.cell(2, 1, "a" * 160 + "hiddenNeedle")
    elif case == "rightcol":
        for column in range(1, 16):
            sheet.cell(2, column, "a" * 160)
        sheet.cell(2, 16, "hiddenNeedle")
    elif case == "formula":
        sheet.cell(2, 1, "=hiddenNeedle()")
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


@pytest.mark.parametrize("case", ["row201", "sheet4", "cell161", "rightcol", "formula"])
def test_every_workbook_cell_is_matched_before_preview_is_shortened(case):
    result = search_frozen_repository(Reader({"a.xlsx": workbook(case)}), "hiddenNeedle", cache={})
    assert "a.xlsx:" in result.text
    assert "hiddenNeedle" in result.text.split("a.xlsx:", 1)[1]
    assert result.coverage_complete


@pytest.mark.parametrize("prefix", ["", "flat/"])
def test_real_cursor_reaches_files_after_a_full_flat_page(prefix):
    reader = Reader({f"{prefix}{i:03}.lua": b"hiddenNeedle" if i == 299 else b"normal"
                     for i in range(300)})
    first = search_frozen_repository(reader, "hiddenNeedle", prefix=prefix, cache={})
    assert first.next_cursor
    second = search_frozen_repository(reader, "hiddenNeedle", prefix=prefix,
                                      cursor=first.next_cursor, cache={})
    assert f"{prefix}299.lua:1:" in second.text
    assert not second.next_cursor
    assert second.scan_start == 240 and second.scan_end == 300
    assert not second.coverage_complete, "A tail page alone cannot prove a whole-tree negative"


def test_hit_pages_inside_one_file_are_reachable_without_changing_prefix():
    reader = Reader({"flat.lua": ("hiddenNeedle\n" * 100).encode()})
    first = search_frozen_repository(reader, "hiddenNeedle", max_hits=80, cache={})
    assert first.next_cursor
    second = search_frozen_repository(reader, "hiddenNeedle", max_hits=80,
                                      cursor=first.next_cursor, cache={})
    assert "flat.lua:100:" in second.text
    assert not second.next_cursor
    assert "flat.lua:1:" not in second.text


def test_cursor_is_bound_to_snapshot_scope_query_and_repository():
    files = {f"{i:03}.lua": b"normal" for i in range(300)}
    reader = Reader(files)
    cursor = search_frozen_repository(reader, "needle", cache={}).next_cursor
    assert cursor
    variants = [(Reader(files, tip="other"), "needle", ""),
                (Reader(files, repository_id=2), "needle", ""),
                (reader, "different", ""), (reader, "needle", "2")]
    for changed, query, prefix in variants:
        with pytest.raises(ValueError, match="cursor|游标"):
            search_frozen_repository(changed, query, prefix=prefix, cursor=cursor, cache={})


def test_concurrent_index_cache_reads_and_evictions_preserve_their_own_snapshot():
    cache = {}
    def search(number):
        result = search_frozen_repository(Reader({"a.lua": f"needle{number}".encode()},
                                                tip=str(number)), f"needle{number}", cache=cache)
        return f"a.lua:1: needle{number}" in result.text
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(search, range(40)))
    assert len(cache) <= 2


def test_index_memory_slice_leaves_remaining_files_reachable():
    entries = [(f"{i}.lua", "abc") for i in range(5)]
    index = SnapshotReferenceIndex.build(entries, reader=lambda *_: "x" * 40,
                                         max_total_chars=100)
    assert index.indexed_files == 2
    assert index.search("xxx").unindexed == 3


def test_output_budget_retains_a_real_cursor_for_every_unshown_hit():
    reader = Reader({"a.lua": (("x" * 300 + "hiddenNeedle\n") * 100).encode()})
    seen = set()
    cursor = ""
    for _ in range(50):
        result = search_frozen_repository(reader, "hiddenNeedle", cursor=cursor,
                                          max_output_chars=1800, cache={})
        assert len(result.text) <= 1800
        import re
        seen.update(int(number) for number in re.findall(r"a.lua:(\d+):", result.text))
        cursor = result.next_cursor
        if not cursor:
            break
    assert seen == set(range(1, 101))


def test_multi_repository_receipts_precede_hit_bodies_and_fit_the_tool_budget():
    readers = [Reader({"a.lua": ("hiddenNeedle " * 50 + "\n").encode() * 100}, repository_id=i)
               for i in range(2)]
    text = search_frozen_repositories(readers, "hiddenNeedle", cache={})
    assert len(text) <= 7800
    assert text.count("[reference_page]") == 2
    assert text.index("query_id=", text.index("query_id=") + 1) < text.index("a.lua:1:")


def test_expanded_workbook_limit_is_an_explicit_gap_not_a_false_zero(monkeypatch):
    from services.ai import reference_index as module
    monkeypatch.setattr(module, "XLSX_MAX_EXPANDED_BYTES", 10)
    result = search_frozen_repository(Reader({"a.xlsx": workbook("row201")}), "hiddenNeedle", cache={})
    assert result.skipped == 1 and not result.coverage_complete
    assert "覆盖不足" in result.text


def test_raw_blob_page_budget_stops_before_files_become_unreachable():
    entries = [(f"{i}.bin", "abc") for i in range(5)]
    index = SnapshotReferenceIndex.build(entries, reader=lambda *_: b"\0" * 40,
                                         max_total_bytes=100)
    assert index.indexed_files == 2
    assert index.search("xxx").unindexed == 3


def test_sparse_workbook_dimensions_cannot_trigger_unbounded_empty_cell_iteration(monkeypatch):
    from services.ai import reference_index as module
    monkeypatch.setattr(module, "XLSX_MAX_CELLS", 100)
    result = search_frozen_repository(Reader({"a.xlsx": workbook("row201")}), "hiddenNeedle", cache={})
    assert result.skipped == 1 and not result.coverage_complete


def test_expanded_workbook_bytes_are_also_bounded_across_the_entire_scan_page(monkeypatch):
    import zipfile

    from services.ai import reference_index as module
    raw = workbook("row201")
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        expanded = sum(info.file_size for info in archive.infolist())
    monkeypatch.setattr(module, "MAX_INDEX_TOTAL_EXPANDED_BYTES", expanded + 1)
    result = search_frozen_repository(Reader({"a.xlsx": raw, "b.xlsx": raw}), "hiddenNeedle", cache={})
    assert result.scan_end == 1 and result.next_cursor
    assert not result.coverage_complete


def test_many_repository_results_are_paginated_without_losing_the_first_hit():
    import re
    readers = [Reader({f"repo{i}.lua": b"hiddenNeedle " * 100}, repository_id=i) for i in range(20)]
    cursor, seen = "", set()
    for _ in range(24):
        text = search_frozen_repositories(readers, "hiddenNeedle", cursor=cursor, cache={})
        assert len(text) <= 7800
        seen.update(re.findall(r"repo\d+\.lua:1:", text))
        queued = re.search(r'\[reference_repository_page\].*next_cursor="([^"]*)"', text)
        cursor = queued.group(1) if queued else ""
        if not cursor:
            break
    assert seen == {f"repo{i}.lua:1:" for i in range(20)}


def test_formula_text_and_cached_computed_id_remain_searchable_together():
    import zipfile
    raw = workbook("formula")
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(raw)) as source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                content = content.replace(b"<v></v>", b"<v>89501</v>").replace(b"<v />", b"<v>89501</v>")
            target.writestr(info, content)
    index = SnapshotReferenceIndex.build([("formula.xlsx", "abc")], reader=lambda *_: output.getvalue())
    assert index.search("hiddenNeedle").hits
    assert index.search("89501").hits


def test_underreported_workbook_dimensions_do_not_hide_real_cells():
    import re
    import zipfile

    output = BytesIO()
    with zipfile.ZipFile(BytesIO(workbook("row201"))) as source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                content = re.sub(rb'<dimension ref="[^"]+"\s*/>', b'<dimension ref="A1:A1"/>', content)
            target.writestr(info, content)
    result = search_frozen_repository(Reader({"a.xlsx": output.getvalue()}), "hiddenNeedle", cache={})
    assert "a.xlsx:" in result.text
    assert "第201行" in result.text
    assert result.coverage_complete


def test_underreported_dimensions_cannot_bypass_the_actual_cell_budget(monkeypatch):
    import re
    import zipfile

    from services.ai import reference_index as module

    monkeypatch.setattr(module, "XLSX_MAX_CELLS", 100)
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(workbook("row201"))) as source, zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                content = re.sub(rb'<dimension ref="[^"]+"\s*/>', b'<dimension ref="A1:A1"/>', content)
            target.writestr(info, content)
    result = search_frozen_repository(Reader({"a.xlsx": output.getvalue()}), "hiddenNeedle", cache={})
    assert result.skipped == 1
    assert not result.coverage_complete
