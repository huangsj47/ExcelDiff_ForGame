"""Helpers for loading and merging weekly Excel diff payloads."""

from __future__ import annotations

import json


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
            )

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

            if merged_sheet["rows"]:
                merged_sheet["status"] = "modified"

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
