#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 本地 temp_cache_fetch 任务处理。"""

from __future__ import annotations

try:
    from ..local_temp_cache import load_local_temp_cache
except ImportError:
    from local_temp_cache import load_local_temp_cache


def execute_temp_cache_fetch(task: dict, settings):
    payload = task.get("payload") or {}
    cache_key = str(payload.get("cache_key") or "").strip()
    expected_hash = str(payload.get("expected_hash") or "").strip() or None
    if not cache_key:
        raise ValueError("temp_cache_fetch payload 缺少 cache_key")

    row = load_local_temp_cache(settings, cache_key, expected_hash=expected_hash)
    if not row:
        summary = {
            "message": "temp_cache_fetch miss",
            "cache_key": cache_key,
        }
        return "completed", summary, None, {"cache_key": cache_key}

    summary = {
        "message": "temp_cache_fetch hit",
        "cache_key": cache_key,
        "payload_size": row.get("payload_size"),
    }
    return "completed", summary, None, row

