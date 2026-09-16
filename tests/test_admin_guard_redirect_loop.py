"""已登录但权限不足时，必须给 403，**不能**跳登录页。

## 缺陷（线上实测复现）

`utils/request_security.py::_unauthorized_admin_response` 原先对「未登录」和
「已登录但权限不足」返回同一种响应：flash 一句「请先使用管理员账号登录。」+
跳 `auth_bp.login?next=<当前URL>`。

而 `auth/routes.py::login` 开头是「**已登录则直接跳 next**」。两者一撞就成环：

    /admin/excel-cache
      → 302 /auth/login?next=/admin/excel-cache    （守卫：不是管理员）
      → 302 /admin/excel-cache                     （登录页：已登录，跳 next）
      → 302 /auth/login?next=/admin/excel-cache    （守卫：还不是管理员）
      → …

浏览器报「将您重定向的次数过多」（用户报障原文），而且**每绕一圈塞一条 flash**：
实测普通用户访问一次 `/admin/excel-cache` 就累积 5 条
「请先使用管理员账号登录。」—— 用户看到的那一屏重复提示就是这个。

语义上也本就该分开：跳登录解决的是「你是谁」，而这个用户身份已经确定、
只是权限不够，重登一万次也没用 —— 那是 403。仓库里 `@app.errorhandler(Forbidden)`
早就写好了权限不足页（HTML）与 403 JSON，原实现只是从没用上。
"""

from __future__ import annotations

import re
import uuid

import pytest
from flask import session as flask_session

from app import app, create_tables
from auth.services import register_user

ADMIN_PAGE = "/admin/excel-cache"
ADMIN_API = "/admin/performance/stats"
ADMIN_FLASH = "请先使用管理员账号登录。"


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


@pytest.fixture
def accounts():
    """建一个平台管理员和一个普通用户（用户名唯一，避免与其它用例串）。"""
    admin_name, normal_name = _uid("adm"), _uid("usr")
    with app.app_context():
        create_tables()
        admin, err = register_user(admin_name, "pw-123456", role="platform_admin")
        assert admin is not None, err
        normal, err = register_user(normal_name, "pw-123456", role="normal")
        assert normal is not None, err
    return {"admin": (admin_name, "pw-123456"), "normal": (normal_name, "pw-123456")}


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token", "")
        if not token:
            token = uuid.uuid4().hex
            sess["_csrf_token"] = token
    return token


def _login(client, credentials) -> None:
    username, password = credentials
    token = _csrf(client)
    client.post(
        "/auth/login",
        data={"username": username, "password": password, "csrf_token": token},
        headers={"X-CSRFToken": token},
        follow_redirects=False,
    )


def _flashes(client) -> list[str]:
    with client.session_transaction() as sess:
        return [message for _category, message in sess.get("_flashes", [])]


def _walk(client, url: str, hops: int = 6) -> list[tuple[str, int]]:
    """跟着 302 走，返回 [(路径, 状态码), ...]。"""
    trail = []
    for _ in range(hops):
        response = client.get(url, follow_redirects=False)
        trail.append((url.split("?")[0], response.status_code))
        if response.status_code != 302:
            break
        location = response.headers.get("Location") or ""
        url = location.split("://", 1)[-1]
        url = "/" + url.split("/", 1)[1] if "/" in url and "://" in location else location
    return trail


def test_authenticated_non_admin_gets_403_and_no_redirect_loop(accounts):
    client = app.test_client()
    _login(client, accounts["normal"])

    response = client.get(ADMIN_PAGE, follow_redirects=False)

    assert response.status_code == 403, (
        f"已登录但非管理员访问 {ADMIN_PAGE} 应得 403，实际 {response.status_code} "
        f"Location={response.headers.get('Location')}"
    )
    assert "/auth/login" not in (response.headers.get("Location") or ""), (
        "不该把已登录的用户再推去登录页 —— 那正是重定向环的来源"
    )


def test_no_loop_even_if_the_client_follows_redirects(accounts):
    """把重定向链走完，确认它不会在 /admin 与 /auth/login 之间打转。"""
    client = app.test_client()
    _login(client, accounts["normal"])

    trail = _walk(client, ADMIN_PAGE)

    assert trail[0] == (ADMIN_PAGE, 403), trail
    assert all("/auth/login" not in path for path, _ in trail), (
        f"跳转链里出现了登录页，说明环还在：{trail}"
    )


def test_repeated_guard_hits_do_not_pile_up_flash_messages(accounts):
    """用户报障：「请先使用管理员账号登录。」弹了很多条，正常只该提示一次。

    环每绕一圈塞一条 flash，所以「一次浏览累积 N 条」正是环的指纹。
    修好后这条路径根本不 flash（403 页面自己会说明原因）。
    """
    client = app.test_client()
    _login(client, accounts["normal"])

    for _ in range(5):
        client.get(ADMIN_PAGE, follow_redirects=False)

    repeats = [m for m in _flashes(client) if m == ADMIN_FLASH]
    assert repeats == [], f"权限不足不该再累积「{ADMIN_FLASH}」，实际 {len(repeats)} 条"


def test_platform_admin_still_reaches_the_page(accounts):
    """别为了消环把管理员也挡掉。"""
    client = app.test_client()
    _login(client, accounts["admin"])

    assert client.get(ADMIN_PAGE, follow_redirects=False).status_code == 200
    assert client.get(ADMIN_API, follow_redirects=False).status_code == 200
    assert _flashes(client) == []


def test_api_call_from_a_non_admin_is_403_not_401(accounts):
    """API 侧同理：401 会诱导客户端去重新登录，而问题不是登录。"""
    client = app.test_client()
    _login(client, accounts["normal"])

    response = client.get(ADMIN_API, headers={"Accept": "application/json"})

    assert response.status_code == 403, response.status_code
    assert (response.get_json() or {}).get("message") == "权限不足"


def test_anonymous_visitor_still_goes_to_login_with_one_flash(accounts):
    """未登录是另一回事：仍然跳登录页，且只提示一次。

    注意这里出现的是「请先登录。」而不是「请先使用管理员账号登录。」——
    `enforce_admin_access` 先判 `is_logged_in()`，所以匿名访客走的是
    `_unauthorized_login_response`。管理员那句只在「已登录但不是管理员」时才出现，
    也就是本次修的那条路径。
    """
    client = app.test_client()

    response = client.get(ADMIN_PAGE, follow_redirects=False)

    assert response.status_code == 302
    assert "/auth/login" in (response.headers.get("Location") or "")
    messages = _flashes(client)
    assert messages.count("请先登录。") == 1
    assert ADMIN_FLASH not in messages


def test_anonymous_api_call_still_gets_401(accounts):
    client = app.test_client()

    response = client.get(ADMIN_API, headers={"Accept": "application/json"})

    assert response.status_code == 401, response.status_code
