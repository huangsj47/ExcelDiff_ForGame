#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""In-memory performance metrics collector for admin dashboard."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List


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


def _to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class PerformanceMetricsService:
    """Thread-safe in-memory metrics service."""

    def __init__(self, max_events: int = 8000):
        self.max_events = max(1000, int(max_events))
        self._events = deque(maxlen=self.max_events)
        self._lock = threading.Lock()

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

        event = {
            "ts": _now_ts(),
            "pipeline": str(pipeline),
            "success": bool(success),
            "metrics": clean_metrics,
            "tags": clean_tags,
        }
        with self._lock:
            self._events.append(event)

    def clear(self) -> int:
        with self._lock:
            count = len(self._events)
            self._events.clear()
        return count

    def snapshot(self, *, window_minutes: int = 60, recent_limit: int = 30) -> Dict[str, Any]:
        window_minutes = max(5, min(int(window_minutes), 24 * 60))
        recent_limit = max(10, min(int(recent_limit), 500))
        now = _now_ts()
        cutoff = now - window_minutes * 60

        with self._lock:
            recent_window_events = [event for event in self._events if event["ts"] >= cutoff]
            all_events = list(self._events)

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
                },
            )
            info["count"] += 1
            if event["success"]:
                info["success_count"] += 1
                total_success += 1
            else:
                info["failed_count"] += 1
            info["last_ts"] = max(info["last_ts"], float(event["ts"]))

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
                    "last_event_at": _to_iso(info["last_ts"]) if info["last_ts"] else None,
                    "avg_metrics": avg_metrics,
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
                    "minute": datetime.fromtimestamp(minute_bucket, tz=timezone.utc).strftime("%H:%M"),
                    "count": int(bucket["count"]),
                    "success_rate": _round2((bucket["success_count"] / bucket["count"]) * 100.0) if bucket["count"] else 0.0,
                    "avg_total_ms": _round2(avg_total_ms),
                }
            )

        recent_events = []
        for event in sorted(recent_window_events, key=lambda item: item["ts"], reverse=True)[:recent_limit]:
            recent_events.append(
                {
                    "time": _to_iso(float(event["ts"])),
                    "pipeline": event["pipeline"],
                    "success": bool(event["success"]),
                    "metrics": event.get("metrics", {}),
                    "tags": event.get("tags", {}),
                }
            )

        total_events = len(recent_window_events)
        failed_events = total_events - total_success

        return {
            "generated_at": _to_iso(now),
            "window_minutes": window_minutes,
            "capacity": self.max_events,
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
        }


_GLOBAL_PERF_METRICS_SERVICE = PerformanceMetricsService()


def get_perf_metrics_service() -> PerformanceMetricsService:
    return _GLOBAL_PERF_METRICS_SERVICE
