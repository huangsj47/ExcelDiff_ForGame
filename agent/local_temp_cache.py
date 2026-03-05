#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent local temporary cache helpers."""

from __future__ import annotations

import hashlib
import json
import os
import time


def _cache_root(settings) -> str:
    base_dir = os.path.abspath(str(getattr(settings, "repos_base_dir", "agent_repos") or "agent_repos"))
    target = os.path.join(base_dir, "_temp_cache")
    os.makedirs(target, exist_ok=True)
    return target


def _cache_file_path(cache_root: str, cache_key: str) -> str:
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return os.path.join(cache_root, f"{digest}.json")


def _write_json_atomic(path: str, data: dict):
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temp_path, path)


def save_local_temp_cache(
    settings,
    *,
    cache_key: str,
    payload_json: str,
    payload_hash: str,
    payload_size: int,
    task_type: str | None = None,
    cache_kind: str | None = None,
    project_id: int | None = None,
    repository_id: int | None = None,
    commit_id: str | None = None,
    file_path: str | None = None,
    expire_seconds: int | None = None,
) -> str:
    key = str(cache_key or "").strip()
    if not key:
        raise ValueError("cache_key is required")
    payload_text = payload_json if isinstance(payload_json, str) else json.dumps(payload_json, ensure_ascii=False)
    now_ts = int(time.time())
    ttl_seconds = int(expire_seconds or int(getattr(settings, "temp_cache_expire_days", 90)) * 24 * 3600)
    ttl_seconds = max(60, ttl_seconds)

    row = {
        "cache_key": key,
        "task_type": task_type,
        "cache_kind": cache_kind or "task_result_payload",
        "project_id": project_id,
        "repository_id": repository_id,
        "commit_id": commit_id,
        "file_path": file_path,
        "payload_json": payload_text,
        "payload_hash": str(payload_hash or "").strip(),
        "payload_size": int(payload_size or len(payload_text.encode("utf-8"))),
        "created_at_ts": now_ts,
        "expire_at_ts": now_ts + ttl_seconds,
    }
    cache_root = _cache_root(settings)
    path = _cache_file_path(cache_root, key)
    _write_json_atomic(path, row)
    return path


def load_local_temp_cache(settings, cache_key: str, expected_hash: str | None = None):
    key = str(cache_key or "").strip()
    if not key:
        return None
    path = _cache_file_path(_cache_root(settings), key)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as handle:
            row = json.load(handle) or {}
    except Exception:
        return None

    if str(row.get("cache_key") or "").strip() != key:
        return None

    now_ts = int(time.time())
    expire_at_ts = int(row.get("expire_at_ts") or 0)
    if expire_at_ts and expire_at_ts <= now_ts:
        try:
            os.remove(path)
        except Exception:
            pass
        return None

    payload_hash = str(row.get("payload_hash") or "").strip()
    expected = str(expected_hash or "").strip()
    if expected and payload_hash and expected != payload_hash:
        return None

    payload_json = row.get("payload_json")
    if payload_json is None:
        return None
    if not isinstance(payload_json, str):
        payload_json = json.dumps(payload_json, ensure_ascii=False)

    ttl_seconds = max(60, expire_at_ts - now_ts) if expire_at_ts else max(
        60,
        int(getattr(settings, "temp_cache_expire_days", 90)) * 24 * 3600,
    )
    return {
        "cache_key": key,
        "task_type": row.get("task_type"),
        "cache_kind": row.get("cache_kind"),
        "project_id": row.get("project_id"),
        "repository_id": row.get("repository_id"),
        "commit_id": row.get("commit_id"),
        "file_path": row.get("file_path"),
        "payload_json": payload_json,
        "payload_hash": payload_hash,
        "payload_size": int(row.get("payload_size") or len(payload_json.encode("utf-8"))),
        "expire_seconds": ttl_seconds,
    }
