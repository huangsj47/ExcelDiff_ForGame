#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""In-memory performance metrics collector for admin dashboard."""

from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List
from utils.timezone_utils import format_beijing_time

_EXCEL_EXTENSIONS = (
    ".xlsx",
    ".xls",
    ".xlsm",
    ".xlsb",
    ".csv",
)
_CODE_SOURCES = {
    "realtime_non_excel",
}
_EXCEL_SOURCES = {
    "html_cache",
    "data_cache",
    "background_excel",
    "generated",
    "cache_hit",
    "realtime",
    "realtime_excel",
    "realtime_html_render_failed",
    "realtime_excel_save_cache_failed",
    "realtime_diff_failed",
    "data_cache_html_render_failed",
    "background_diff_failed",
    "background_inner_exception",
    "background_outer_exception",
}
_REALTIME_SOURCES = {
    "realtime",
    "realtime_excel",
    "realtime_non_excel",
    "realtime_html_render_failed",
    "realtime_excel_save_cache_failed",
    "realtime_diff_failed",
    "data_cache_html_render_failed",
    "diff_data_empty",
}
_BACKGROUND_SOURCES = {
    "html_cache",
    "data_cache",
    "background_excel",
    "generated",
    "cache_hit",
    "background_diff_failed",
    "background_inner_exception",
    "background_outer_exception",
}


def _now_ts() -> float:
    return time.time()


def _safe_percentile(values: List[float], percentile: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = int(math.ceil((percentile / 100.0) * len(sorted_values))) - 1
    index = max(0, min(index, len(sorted_values) - 1))
    return float(sorted_values[index])


def _round2(value: float) -> float:
    return round(float(value), 2)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _to_display_time(ts: float, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return format_beijing_time(dt, fmt)


def _normalize_filter_value(value: Any, *, allowed: set[str], default: str = "all") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if normalized not in allowed:
        return default
    return normalized


def _is_excel_path(file_path: Any) -> bool:
    text = str(file_path or "").strip().lower()
    return bool(text) and text.endswith(_EXCEL_EXTENSIONS)


def _infer_diff_kind(pipeline: str, tags: Dict[str, str]) -> str:
    source = str(tags.get("source", "") or "").strip().lower()
    if source in _CODE_SOURCES:
        return "code"
    if source in _EXCEL_SOURCES:
        return "excel"

    file_path = str(tags.get("file_path", "") or "").strip()
    if _is_excel_path(file_path):
        return "excel"

    if pipeline in {"api_excel_diff", "background_excel_cache", "weekly_excel_cache"}:
        return "excel"
    return "code"


def _infer_mode_kind(pipeline: str, tags: Dict[str, str]) -> str:
    source = str(tags.get("source", "") or "").strip().lower()
    if source in _BACKGROUND_SOURCES:
        return "background_cache"
    if source in _REALTIME_SOURCES:
        return "realtime"
    if source.startswith("background_"):
        return "background_cache"
    if pipeline in {"background_excel_cache", "weekly_excel_cache"}:
        return "background_cache"
    return "realtime"


def _project_filter_matches(tags: Dict[str, str], project_filter: str) -> bool:
    if project_filter == "all":
        return True
    project_code = str(tags.get("project_code", "") or "").strip().lower()
    project_id = str(tags.get("project_id", "") or "").strip().lower()
    if not project_code and not project_id:
        return False
    candidates = {project_code, project_id}
    if project_id:
        candidates.add(f"id:{project_id}")
    return project_filter in {item for item in candidates if item}


def _summarize_kind(kind_counter: Dict[str, int]) -> str:
    if not kind_counter:
        return "unknown"
    active = [key for key, value in kind_counter.items() if int(value or 0) > 0]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        return "mixed"
    return "unknown"


def _safe_int_env(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, value))


def _safe_float_env(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, value))


class PerformanceMetricsService:
    """Thread-safe in-memory metrics service."""

    def __init__(
        self,
        max_events: int = 8000,
        *,
        max_scope_share: float = 0.35,
        min_scope_events: int = 300,
    ):
        self.max_events = max(1000, int(max_events))
        self.max_scope_share = max(0.05, min(1.0, float(max_scope_share)))
        self.min_scope_events = max(50, int(min_scope_events))
        self._events = deque()
        self._scope_counts: Dict[str, int] = {}
        self._evicted_events = 0
        self._scope_rebalance_evictions = 0
        self._lock = threading.Lock()

    @staticmethod
    def _build_scope_key(pipeline: str, tags: Dict[str, str]) -> str:
        source = str(tags.get("source", "") or "")
        repository_id = str(tags.get("repository_id", "") or "")
        config_id = str(tags.get("config_id", "") or "")
        scope_id = "global"
        if repository_id:
            scope_id = f"repo:{repository_id}"
        elif config_id:
            scope_id = f"cfg:{config_id}"
        return f"{pipeline}|{source}|{scope_id}"

    def _add_scope_count(self, scope_key: str) -> None:
        self._scope_counts[scope_key] = self._scope_counts.get(scope_key, 0) + 1

    def _remove_scope_count(self, scope_key: str | None) -> None:
        if not scope_key:
            return
        current = self._scope_counts.get(scope_key, 0)
        if current <= 1:
            self._scope_counts.pop(scope_key, None)
            return
        self._scope_counts[scope_key] = current - 1

    def _current_scope_capacity(self, incoming_scope: str | None = None) -> int:
        active_scopes = len(self._scope_counts)
        if incoming_scope and incoming_scope not in self._scope_counts:
            active_scopes += 1
        active_scopes = max(1, active_scopes)
        equal_share = max(1, self.max_events // active_scopes)
        max_share_limit = max(1, int(self.max_events * self.max_scope_share))
        scope_capacity = min(equal_share, max_share_limit)
        scope_capacity = max(self.min_scope_events, scope_capacity)
        return max(1, min(self.max_events, scope_capacity))

    def _pick_overrepresented_scope(self, scope_capacity: int) -> str | None:
        candidate_key = None
        candidate_count = scope_capacity
        for scope_key, count in self._scope_counts.items():
            if count > candidate_count:
                candidate_key = scope_key
                candidate_count = count
        return candidate_key

    def _pop_oldest_from_scope(self, scope_key: str) -> Dict[str, Any] | None:
        for event in self._events:
            if event.get("_scope_key") != scope_key:
                continue
            self._events.remove(event)
            return event
        return None

    def _evict_if_needed(self, incoming_scope: str) -> None:
        if len(self._events) < self.max_events:
            return
        scope_capacity = self._current_scope_capacity(incoming_scope)
        over_scope = self._pick_overrepresented_scope(scope_capacity)
        removed = None
        if over_scope:
            removed = self._pop_oldest_from_scope(over_scope)
        if removed is None and self._events:
            removed = self._events.popleft()
        if removed is None:
            return
        self._remove_scope_count(removed.get("_scope_key"))
        self._evicted_events += 1
        if over_scope:
            self._scope_rebalance_evictions += 1

    def record(self, pipeline: str, *, success: bool = True, metrics: Dict[str, Any] | None = None, tags: Dict[str, Any] | None = None) -> None:
        if not pipeline:
            return
        clean_metrics: Dict[str, float] = {}
        clean_tags: Dict[str, str] = {}

        for key, value in (metrics or {}).items():
            if _is_number(value):
                clean_metrics[str(key)] = float(value)

        for key, value in (tags or {}).items():
            if value is None:
                continue
            clean_tags[str(key)] = str(value)

        scope_key = self._build_scope_key(str(pipeline), clean_tags)
        event = {
            "ts": _now_ts(),
            "pipeline": str(pipeline),
            "success": bool(success),
            "metrics": clean_metrics,
            "tags": clean_tags,
            "_scope_key": scope_key,
        }
        with self._lock:
            self._evict_if_needed(scope_key)
            self._events.append(event)
            self._add_scope_count(scope_key)

    def clear(self) -> int:
        with self._lock:
            count = len(self._events)
            self._events.clear()
            self._scope_counts.clear()
            self._evicted_events = 0
            self._scope_rebalance_evictions = 0
        return count

    def snapshot(
        self,
        *,
        window_minutes: int = 60,
        recent_limit: int = 30,
        diff_kind: str = "all",
        mode_kind: str = "all",
        project_filter: str = "all",
    ) -> Dict[str, Any]:
        # Dashboard supports minute/hour/day windows up to 30 days.
        window_minutes = max(5, min(int(window_minutes), 30 * 24 * 60))
        recent_limit = max(10, min(int(recent_limit), 500))
        diff_kind = _normalize_filter_value(diff_kind, allowed={"all", "code", "excel"}, default="all")
        mode_kind = _normalize_filter_value(mode_kind, allowed={"all", "realtime", "background_cache"}, default="all")
        project_filter = str(project_filter or "all").strip().lower() or "all"
        now = _now_ts()
        cutoff = now - window_minutes * 60

        with self._lock:
            all_events = list(self._events)
            base_window_events = [event for event in all_events if event["ts"] >= cutoff]
            active_scope_count = len(self._scope_counts)
            soft_scope_capacity = self._current_scope_capacity()
            evicted_event_count = int(self._evicted_events)
            scope_rebalance_evictions = int(self._scope_rebalance_evictions)

        project_options_map: Dict[str, Dict[str, str]] = {}
        recent_window_events = []
        for event in base_window_events:
            tags = event.get("tags", {}) or {}
            event_diff_kind = _infer_diff_kind(str(event.get("pipeline", "")), tags)
            event_mode_kind = _infer_mode_kind(str(event.get("pipeline", "")), tags)

            project_code_raw = str(tags.get("project_code", "") or "").strip()
            project_id_raw = str(tags.get("project_id", "") or "").strip()
            if project_code_raw or project_id_raw:
                value = project_code_raw or project_id_raw
                label = project_code_raw or f"项目ID:{project_id_raw}"
                if project_code_raw and project_id_raw:
                    label = f"{project_code_raw} (ID:{project_id_raw})"
                project_options_map[value.lower()] = {"value": value, "label": label}

            if diff_kind != "all" and event_diff_kind != diff_kind:
                continue
            if mode_kind != "all" and event_mode_kind != mode_kind:
                continue
            if not _project_filter_matches(tags, project_filter):
                continue

            enriched_event = dict(event)
            enriched_event["_diff_kind"] = event_diff_kind
            enriched_event["_mode_kind"] = event_mode_kind
            recent_window_events.append(enriched_event)

        pipeline_map: Dict[str, Dict[str, Any]] = {}
        total_success = 0
        total_total_ms_values: List[float] = []

        minute_map: Dict[int, Dict[str, float]] = {}
        for event in recent_window_events:
            pipeline = event["pipeline"]
            info = pipeline_map.setdefault(
                pipeline,
                {
                    "pipeline": pipeline,
                    "count": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "last_ts": 0.0,
                    "total_ms_values": [],
                    "metric_sums": {},
                    "metric_counts": {},
                    "diff_kind_counts": {},
                    "mode_kind_counts": {},
                },
            )
            info["count"] += 1
            if event["success"]:
                info["success_count"] += 1
                total_success += 1
            else:
                info["failed_count"] += 1
            info["last_ts"] = max(info["last_ts"], float(event["ts"]))
            event_diff_kind = str(event.get("_diff_kind") or _infer_diff_kind(pipeline, event.get("tags", {}) or {}))
            event_mode_kind = str(event.get("_mode_kind") or _infer_mode_kind(pipeline, event.get("tags", {}) or {}))
            info["diff_kind_counts"][event_diff_kind] = int(info["diff_kind_counts"].get(event_diff_kind, 0)) + 1
            info["mode_kind_counts"][event_mode_kind] = int(info["mode_kind_counts"].get(event_mode_kind, 0)) + 1

            metrics = event.get("metrics", {})
            total_ms = metrics.get("total_ms")
            if _is_number(total_ms):
                total_ms_value = float(total_ms)
                info["total_ms_values"].append(total_ms_value)
                total_total_ms_values.append(total_ms_value)

            for key, value in metrics.items():
                if not _is_number(value):
                    continue
                info["metric_sums"][key] = info["metric_sums"].get(key, 0.0) + float(value)
                info["metric_counts"][key] = info["metric_counts"].get(key, 0) + 1

            minute_bucket = int(event["ts"] // 60) * 60
            bucket = minute_map.setdefault(
                minute_bucket,
                {"count": 0, "success_count": 0, "total_ms_sum": 0.0, "total_ms_count": 0},
            )
            bucket["count"] += 1
            if event["success"]:
                bucket["success_count"] += 1
            if _is_number(total_ms):
                bucket["total_ms_sum"] += float(total_ms)
                bucket["total_ms_count"] += 1

        pipeline_stats = []
        for pipeline, info in pipeline_map.items():
            count = info["count"]
            success_count = info["success_count"]
            failed_count = info["failed_count"]
            total_ms_values = info["total_ms_values"]

            avg_metrics = {}
            for key, value_sum in info["metric_sums"].items():
                metric_count = info["metric_counts"].get(key, 0)
                if metric_count <= 0:
                    continue
                avg_metrics[f"avg_{key}"] = _round2(value_sum / metric_count)

            pipeline_stats.append(
                {
                    "pipeline": pipeline,
                    "count": count,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "success_rate": _round2((success_count / count) * 100.0) if count else 0.0,
                    "avg_total_ms": _round2(sum(total_ms_values) / len(total_ms_values)) if total_ms_values else 0.0,
                    "p95_total_ms": _round2(_safe_percentile(total_ms_values, 95)) if total_ms_values else 0.0,
                    "max_total_ms": _round2(max(total_ms_values)) if total_ms_values else 0.0,
                    "events_per_min": _round2(count / max(window_minutes, 1)),
                    "last_event_at": _to_display_time(info["last_ts"]) if info["last_ts"] else None,
                    "avg_metrics": avg_metrics,
                    "diff_kind": _summarize_kind(info["diff_kind_counts"]),
                    "mode_kind": _summarize_kind(info["mode_kind_counts"]),
                }
            )

        pipeline_stats.sort(key=lambda item: item["count"], reverse=True)

        timeline = []
        for minute_bucket in sorted(minute_map.keys()):
            bucket = minute_map[minute_bucket]
            avg_total_ms = (
                bucket["total_ms_sum"] / bucket["total_ms_count"]
                if bucket["total_ms_count"] > 0
                else 0.0
            )
            timeline.append(
                {
                    "minute": _to_display_time(float(minute_bucket), "%H:%M"),
                    "count": int(bucket["count"]),
                    "success_rate": _round2((bucket["success_count"] / bucket["count"]) * 100.0) if bucket["count"] else 0.0,
                    "avg_total_ms": _round2(avg_total_ms),
                }
            )

        recent_events = []
        for event in sorted(recent_window_events, key=lambda item: item["ts"], reverse=True)[:recent_limit]:
            recent_events.append(
                {
                    "time": _to_display_time(float(event["ts"])),
                    "pipeline": event["pipeline"],
                    "success": bool(event["success"]),
                    "metrics": event.get("metrics", {}),
                    "tags": event.get("tags", {}),
                    "diff_kind": str(event.get("_diff_kind") or "unknown"),
                    "mode_kind": str(event.get("_mode_kind") or "unknown"),
                }
            )

        total_events = len(recent_window_events)
        failed_events = total_events - total_success

        return {
            "generated_at": _to_display_time(now),
            "timezone": "UTC+8",
            "window_minutes": window_minutes,
            "capacity": self.max_events,
            "active_scope_count": active_scope_count,
            "soft_scope_capacity": soft_scope_capacity,
            "scope_balance_enabled": True,
            "evicted_event_count": evicted_event_count,
            "scope_rebalance_evictions": scope_rebalance_evictions,
            "total_events": total_events,
            "success_count": total_success,
            "failed_count": failed_events,
            "success_rate": _round2((total_success / total_events) * 100.0) if total_events else 0.0,
            "avg_total_ms": _round2(sum(total_total_ms_values) / len(total_total_ms_values)) if total_total_ms_values else 0.0,
            "p95_total_ms": _round2(_safe_percentile(total_total_ms_values, 95)) if total_total_ms_values else 0.0,
            "max_total_ms": _round2(max(total_total_ms_values)) if total_total_ms_values else 0.0,
            "pipeline_stats": pipeline_stats,
            "timeline": timeline,
            "recent_events": recent_events,
            "stored_event_count": len(all_events),
            "available_projects": sorted(project_options_map.values(), key=lambda item: item["label"].lower()),
            "applied_filters": {
                "diff_kind": diff_kind,
                "mode_kind": mode_kind,
                "project_filter": project_filter,
            },
        }


_GLOBAL_PERF_METRICS_SERVICE = PerformanceMetricsService(
    max_events=_safe_int_env("PERF_METRICS_MAX_EVENTS", 8000, min_value=1000, max_value=500000),
    max_scope_share=_safe_float_env("PERF_METRICS_MAX_SCOPE_SHARE", 0.35, min_value=0.05, max_value=1.0),
    min_scope_events=_safe_int_env("PERF_METRICS_MIN_SCOPE_EVENTS", 300, min_value=50, max_value=20000),
)


def get_perf_metrics_service() -> PerformanceMetricsService:
    return _GLOBAL_PERF_METRICS_SERVICE





