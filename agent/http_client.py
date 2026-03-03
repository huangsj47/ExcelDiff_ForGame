#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent HTTP 客户端（标准库实现）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 10):
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url=url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, _safe_json_loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, _safe_json_loads(body)
    except Exception as exc:
        return 0, {"success": False, "message": str(exc)}


def _safe_json_loads(raw: str):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {"success": False, "message": raw}

