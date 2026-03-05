#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent HTTP 客户端（标准库实现）。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
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


def get_json(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 10):
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    request_url = str(url or "").strip()
    if params:
        query = urllib.parse.urlencode(params)
        delimiter = "&" if "?" in request_url else "?"
        request_url = f"{request_url}{delimiter}{query}"

    req = urllib.request.Request(url=request_url, headers=req_headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, _safe_json_loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, _safe_json_loads(body)
    except Exception as exc:
        return 0, {"success": False, "message": str(exc)}


def download_file(url: str, target_path: str, headers: dict | None = None, timeout: int = 30):
    req_headers = {}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url=str(url or "").strip(), headers=req_headers, method="GET")

    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(target_path, "wb") as fp:
                while True:
                    chunk = resp.read(1024 * 64)
                    if not chunk:
                        break
                    fp.write(chunk)
            return resp.status, {"success": True, "size": os.path.getsize(target_path)}
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
