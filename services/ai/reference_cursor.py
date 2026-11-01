"""Opaque continuation addresses bound to a frozen reference search scope."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets

_configured_key = (os.environ.get("AI_REFERENCE_CURSOR_KEY") or os.environ.get("FLASK_SECRET_KEY")
                   or os.environ.get("SECRET_KEY"))
_KEY = hashlib.sha256(_configured_key.encode()).digest() if _configured_key else secrets.token_bytes(32)


def encode_cursor(scope: str, repository_id, start: int, hit: int, *, after=None, group="", remote="") -> str:
    value = dict(v=1, scope=scope, repo=str(repository_id), start=start, hit=hit)
    if after is not None:
        value.update(after=after, group=group)
    if remote:
        value["remote"] = remote
    body = json.dumps(value,
                      separators=(",", ":")).encode()
    signature = hmac.new(_KEY, body, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(signature + body).decode().rstrip("=")


def decode_cursor(cursor: str, *, scope: str = "") -> dict:
    try:
        if len(cursor) > 1024:
            raise ValueError
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        signature, body = raw[:16], raw[16:]
        expected = hmac.new(_KEY, body, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        value = json.loads(body)
        if (value.get("v") != 1 or (scope and value.get("scope") != scope)
                or type(value.get("start")) is not int or value["start"] < 0
                or type(value.get("hit")) is not int or value["hit"] < 0):
            raise ValueError
        if "after" in value and (type(value["after"]) is not int or value["after"] < 0):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("检索游标 cursor 无效或不属于当前快照、范围、关键词；请清空 cursor 重新检索") from exc
