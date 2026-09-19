# -*- coding: utf-8 -*-
"""列表接口的 `?limit=` 不该因为填错而 500。

## 缺陷形态

`services/agent_management_handlers.py` 里两处写的是

    limit = max(1, min(200, int(request.args.get("limit") or 50)))

`request.args.get` 拿到的是**用户填的字符串**，`?limit=abc` 直接 `ValueError`。
翻页条数是最随便的一个参数（用户手改地址栏、脚本拼 URL、旧书签都能带上它），
填错时的正确表现是「回到默认条数」，而不是整个列表接口报 500 ——
页面上的表现是「Agent 任务列表打不开」，与用户填的那个数字看不出关系。

## 口径

脏值 / 空 / 小于 1 → 默认 50；超过上限 → 夹到 200（**不报错**：
要 1000 条就给 200 条，这与「填错了」不是一回事）。
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import app, create_tables  # noqa: E402

ADMIN_TOKEN = "limit-probe-admin-token"


def _uid(prefix: str) -> str:
    return f"{prefix}-{os.urandom(6).hex()}"


def _client(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    client = app.test_client()
    return client


def _get(client, path: str):
    return client.get(path, headers={"X-Admin-Token": ADMIN_TOKEN})


def test_a_junk_limit_does_not_break_the_agent_task_list(monkeypatch):
    with app.app_context():
        create_tables()
        with _client(monkeypatch) as client:
            for raw in ("abc", "2.5", "", "一", "-", "1e9", "9999999999999999999999"):
                resp = _get(client, f"/api/agents/tasks?limit={raw}")
                assert resp.status_code == 200, (raw, resp.status_code)
                assert (resp.get_json() or {}).get("success") is not False, raw


def test_a_junk_limit_does_not_break_the_incident_list(monkeypatch):
    with app.app_context():
        create_tables()
        with _client(monkeypatch) as client:
            # agent_code 不存在 → 404 是**业务的**「这个 Agent 不存在」，
            # 与参数解析无关；这里要钉的是它不因 limit 变成 500。
            for raw in ("abc", "2.5", "一"):
                resp = _get(client, f"/api/agents/{_uid('nope')}/incidents?limit={raw}")
                assert resp.status_code == 404, (raw, resp.status_code)
                assert "不存在" in (resp.get_json() or {}).get("message", ""), raw


def test_a_bad_limit_falls_back_to_the_default_instead_of_a_500():
    """直接钉判定函数：脏值 → 默认，超限 → 夹住，合法值 → 原样。"""
    from services.agent_management_handlers import _paged_limit

    cases = [
        ("abc", 50),
        ("", 50),
        ("0", 50),
        ("-3", 50),
        ("2.5", 50),
        ("7", 7),
        ("200", 200),
        ("1000", 200),
    ]
    for raw, expected in cases:
        with app.test_request_context(f"/api/agents/tasks?limit={raw}"):
            assert _paged_limit() == expected, raw
    with app.test_request_context("/api/agents/tasks"):
        assert _paged_limit() == 50
