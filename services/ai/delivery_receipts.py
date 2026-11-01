"""Exact evidence delivery receipts, separate from fetching and UI preview limits."""
from __future__ import annotations


def delivered_evidence(items):
    from services.ai.context_tools import _request_of_key
    from services.ai.trace_evidence import summarize_executed

    items = tuple(item for item in items if not item.meta.get("repeat_pointer"))
    rows = summarize_executed(items, limit=len(items))
    for row, item in zip(rows, items):
        meta = item.meta
        row["evidence_id"] = str(meta.get("evidence_id") or "")
        row.update(delivery_version=1, segments=meta.get("segments"), shown=meta.get("shown"),
                   complete_segments=meta.get("complete_segments"),
                   omitted_for_budget=bool(meta.get("omitted_for_budget")),
                   shrink_level=meta.get("shrink_level"))
        if meta.get("cache_key"):
            row["request_key"] = tuple(meta["cache_key"])
            request = _request_of_key(meta["cache_key"])
            # 地址不是 UI 预览；不可按 200 字截掉长路径或丢掉仓库身份。
            row.update(commit=request.commit, path=request.path, lines=request.lines,
                       repository_id=request.repository_id)
    return tuple(rows)


def coverage_evidence(detail):
    """Explicit [] means nothing was delivered; absent/None is a legacy trace."""
    delivered = detail.get("delivered")
    return detail.get("executed", []) if delivered is None else delivered
