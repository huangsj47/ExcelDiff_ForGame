"""Helpers for loading and merging weekly Excel diff payloads."""

from __future__ import annotations

import json

from utils.diff_data_utils import header_rows_have_changes

# 合并后的表头块里，「有改动」的三种状态（`unchanged` 是常驻行，见 header_rows_have_changes）。
_CHANGED_STATUSES = ("added", "removed", "modified")


def _merge_header_block(merged_sheet, source_rows, segment_info):
    """把某一段的表头块并进合并结果（**按行号去重**）。

    表头块不像数据行那样按行区间切分：每个分段都是同一个文件的一次完整 diff，
    各自带着**同一份**表头块。直接 append 会把表头行按分段数复制好几遍，
    所以这里按行号归并，并且**有改动的那一段赢** —— 表头行的改动才是要看的东西，
    而「第 2 段说没改」只是那一对提交之间没改。
    """
    block = merged_sheet.get("header_rows") or []
    by_row = {
        row["row_number"]: row
        for row in block
        if isinstance(row, dict) and "row_number" in row
    }
    for row in source_rows or []:
        if not isinstance(row, dict) or "row_number" not in row:
            continue
        incoming = dict(row)
        incoming.setdefault("segment_info", segment_info)
        existing = by_row.get(row["row_number"])
        if existing is None or (
            existing.get("status") not in _CHANGED_STATUSES
            and incoming.get("status") in _CHANGED_STATUSES
        ):
            by_row[row["row_number"]] = incoming
    if by_row:
        merged_sheet["header_rows"] = [by_row[key] for key in sorted(by_row)]


def _merge_header_changes(merged_sheet, source_changes):
    """列名变更是 `header_changes` 那份清单，同样按分段去重。

    修前这里整份丢掉：合并后的周版本视图里，「列名变更」的提示永远是空的 ——
    与「只改表头看不见」是同一类问题（`DiffService._build_header_rows` 的说明）。
    """
    seen = {
        (item.get("change"), item.get("column_index"), item.get("column"),
         item.get("old_name"), item.get("new_name"))
        for item in merged_sheet.get("header_changes") or []
        if isinstance(item, dict)
    }
    for item in source_changes or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("change"), item.get("column_index"), item.get("column"),
               item.get("old_name"), item.get("new_name"))
        if key in seen:
            continue
        seen.add(key)
        merged_sheet.setdefault("header_changes", []).append(item)


def _finalize_header_block(merged_sheet):
    """重算合并后的 `header_stats`，并按它决定这张表算不算有变更。"""
    if not merged_sheet.get("header_rows"):
        return
    block = merged_sheet["header_rows"]
    counts = {status: 0 for status in ("added", "removed", "modified")}
    for row in block:
        status = row.get("status") if isinstance(row, dict) else None
        if status in counts:
            counts[status] += 1
    # 合并之后「前后各多少行表头」已经分不出来了（每段都是同一份表头），
    # 两个总数都用块的行数，界面只用 added/removed/modified 三项。
    merged_sheet["header_stats"] = dict(
        counts, total_rows_current=len(block), total_rows_previous=len(block))


def merge_segmented_excel_diff_payload(segment_payloads):
    """Merge segmented excel payload list into a single excel payload."""
    if not isinstance(segment_payloads, list) or not segment_payloads:
        return None

    total_segments = len(segment_payloads)
    merged_result = {
        "type": "excel",
        "sheets": {},
        "has_changes": False,
        "is_merged": True,
        "merge_strategy": "segmented",
        "total_segments": total_segments,
    }

    for segment_index, segment_payload in enumerate(segment_payloads, start=1):
        excel_payload = extract_excel_diff_from_payload(segment_payload)
        if not excel_payload:
            continue

        sheets = excel_payload.get("sheets") if isinstance(excel_payload, dict) else None
        if not isinstance(sheets, dict):
            continue

        if excel_payload.get("has_changes"):
            merged_result["has_changes"] = True

        for sheet_name, sheet_data in sheets.items():
            if not isinstance(sheet_data, dict):
                continue
            merged_sheet = merged_result["sheets"].setdefault(
                sheet_name,
                {
                    "status": sheet_data.get("status", "modified"),
                    "has_changes": False,
                    "rows": [],
                    "stats": {"added": 0, "removed": 0, "modified": 0},
                },
            )

            rows = sheet_data.get("rows") or []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        row_copy = dict(row)
                        row_copy.setdefault(
                            "segment_info",
                            {"segment_index": segment_index, "total_segments": total_segments},
                        )
                        merged_sheet["rows"].append(row_copy)
                    else:
                        merged_sheet["rows"].append(row)

            merged_sheet["has_changes"] = (
                merged_sheet.get("has_changes", False)
                or bool(sheet_data.get("has_changes"))
                or bool(rows)
                or header_rows_have_changes(sheet_data)
            )

            _merge_header_block(
                merged_sheet,
                sheet_data.get("header_rows"),
                {"segment_index": segment_index, "total_segments": total_segments},
            )
            _merge_header_changes(merged_sheet, sheet_data.get("header_changes"))

            source_stats = sheet_data.get("stats")
            if isinstance(source_stats, dict):
                for stat_key in ("added", "removed", "modified"):
                    try:
                        merged_sheet["stats"][stat_key] += int(source_stats.get(stat_key, 0) or 0)
                    except (TypeError, ValueError):
                        continue
            elif isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    row_status = row.get("status")
                    if row_status in merged_sheet["stats"]:
                        merged_sheet["stats"][row_status] += 1

            if merged_sheet["rows"] or merged_sheet.get("header_changes") \
                    or header_rows_have_changes(merged_sheet):
                merged_sheet["status"] = "modified"

    for merged_sheet in merged_result["sheets"].values():
        _finalize_header_block(merged_sheet)

    if not merged_result["sheets"]:
        return None
    return merged_result


def extract_excel_diff_from_payload(payload):
    """Extract excel diff payload from merged/diff_data/segmented wrappers."""
    if payload is None:
        return None

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None

    if not isinstance(payload, dict):
        return None

    payload_type = payload.get("type")
    sheets = payload.get("sheets")
    if payload_type == "excel" and isinstance(sheets, dict):
        return payload

    for nested_key in ("diff_data", "merged_diff"):
        nested_payload = payload.get(nested_key)
        nested_excel = extract_excel_diff_from_payload(nested_payload)
        if nested_excel:
            return nested_excel

    segments = payload.get("segments")
    if payload_type == "segmented_diff" and isinstance(segments, list):
        return merge_segmented_excel_diff_payload(segments)

    return None


def load_weekly_excel_diff_from_cache(
    *,
    repository,
    diff_cache,
    file_path,
    commit_model,
    log_print,
    commit_sort_key,
    generate_merged_diff_data,
):
    """Prefer merged cache payload, fallback to recomputation by cached commit ids."""
    merged_payload = None
    if diff_cache.merged_diff_data:
        try:
            merged_payload = json.loads(diff_cache.merged_diff_data)
        except Exception as parse_err:
            log_print(
                f"周版本 merged_diff_data 解析失败，回退实时计算: {file_path}, 错误: {parse_err}",
                "WEEKLY",
                force=True,
            )

    cached_excel_diff = extract_excel_diff_from_payload(merged_payload)
    if cached_excel_diff:
        return cached_excel_diff

    commit_ids = []
    if isinstance(merged_payload, dict):
        raw_commit_ids = merged_payload.get("commit_ids")
        if isinstance(raw_commit_ids, list):
            commit_ids = [cid for cid in raw_commit_ids if isinstance(cid, str) and cid]

    if not commit_ids:
        return None

    commit_rows = commit_model.query.filter(
        commit_model.repository_id == repository.id,
        commit_model.path == file_path,
        commit_model.commit_id.in_(commit_ids),
    ).all()
    if not commit_rows:
        return None

    commit_map = {item.commit_id: item for item in commit_rows}
    ordered_commits = [commit_map[cid] for cid in commit_ids if cid in commit_map]
    if not ordered_commits:
        ordered_commits = sorted(commit_rows, key=commit_sort_key)

    base_commit = None
    if diff_cache.base_commit_id:
        base_commit = commit_model.query.filter(
            commit_model.repository_id == repository.id,
            commit_model.path == file_path,
            commit_model.commit_id == diff_cache.base_commit_id,
        ).first()

    recomputed = generate_merged_diff_data(
        repository=repository,
        file_path=file_path,
        base_commit=base_commit,
        latest_commit=ordered_commits[-1],
        commits=ordered_commits,
    )
    return extract_excel_diff_from_payload(recomputed)
