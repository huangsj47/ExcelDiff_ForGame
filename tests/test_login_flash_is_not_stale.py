# -*- coding: utf-8 -*-
"""登录成功之后不该再冒出「请先登录。」。

## 缺陷形态（实测报障）

重启平台之后，浏览器里那个旧页面点任何操作都会被要求登录。登录页正常显示了
「请先登录。」，登录也成功了 —— 可是**登录后的第一个页面顶上又写着「请先登录。」**，
看起来像刚登录就掉登录。

## 根因

「未登录 → flash 一句提示 + 302 到登录页」这条路只对**页面导航**成立。
页面里的脚本（轮询任务状态、拉用量、异步加载 diff）落到同一条分支上时：

1. 浏览器拿到 302 + 登录页 HTML（脚本多半只当成「请求失败了」），**更重要的是**；
2. 那个响应带回来一个**新的 session cookie**，里面装着这条 flash。登录页渲染时
   已经把 flash 消费掉了，而这条晚到的 cookie 又把它写了回去 —— 于是它会在
   用户登录成功后的第一个页面顶上显示出来。

平台是 flask 的 cookie session，flash 就存在 cookie 里，所以「谁最后写的 cookie
谁说了算」，这类竞态只能从**源头**堵：脚本发的请求不要走 flash 那条路。

## 两条守卫（各测一半）

* `_is_document_navigation()`：带 Fetch Metadata（`Sec-Fetch-Mode`）的脚本请求按
  「非导航」处理 → 返回 401 JSON，不写 flash；**没有这个头的请求按导航处理**，
  行为与改前逐字一致（curl / 老浏览器 / 测试客户端都不发这个头）。
* `discard_pending_flashes()`：登录 POST 成功时把会话里残留的提示丢掉，
  登录页那条 `已登录 → 直接跳 next` 的分支也丢一次 —— 那是它最后的清理时机。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
from flask import session as flask_session

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app import app, create_tables, db  # noqa: E402
from auth.services import register_user  # noqa: E402
from utils.request_security import _is_document_navigation  # noqa: E402

PASSWORD = "pw-123456"
# 需要登录才能打开的页面（未登录 → 走「flash + 跳登录页」那条分支）
PROTECTED = "/projects"


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token", "")
        if not token:
            token = uuid.uuid4().hex
            sess["_csrf_token"] = token
    return token


def _pending_flashes(client):
    with client.session_transaction() as sess:
        return list(sess.get("_flashes") or [])


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    with app.app_context():
        create_tables()
    with app.test_client() as test_client:
        yield test_client


class TestFetchMetadataDistinguishesNavigationFromScripts:
    """判据本身：只有明确说了「不是导航」才算脚本请求。"""

    @pytest.mark.parametrize("mode,expected", [
        ("navigate", True),
        ("cors", False),
        ("no-cors", False),
        ("same-origin", False),
        ("NAVIGATE", True),
    ])
    def test_mode_header_is_honoured(self, client, mode, expected):
        with app.test_request_context("/", headers={"Sec-Fetch-Mode": mode}):
            assert _is_document_navigation() is expected

    def test_missing_header_is_treated_as_a_navigation(self, client):
        """没有 Fetch Metadata 的客户端（curl / 老浏览器 / 测试）行为不许变。"""
        with app.test_request_context("/"):
            assert _is_document_navigation() is True


class TestScriptRequestsDoNotWriteAStaleFlash:
    def test_an_xhr_is_answered_with_401_json_and_no_flash(self, client):
        response = client.get(PROTECTED, headers={"Sec-Fetch-Mode": "cors"})

        assert response.status_code == 401, (
            f"脚本发的请求应该拿到 401 JSON，实际是 {response.status_code}"
        )
        assert response.get_json().get("success") is False
        assert _pending_flashes(client) == [], (
            "脚本发的请求往 session 里写了一条 flash —— 这条 flash 会在用户登录成功"
            "之后才显示出来（晚到的 session cookie 把它写了回去）"
        )

    def test_a_page_navigation_still_redirects_to_the_login_page(self, client):
        """反向自检：真正的页面导航该给的提示一个字都不能少。"""
        response = client.get(PROTECTED, headers={"Sec-Fetch-Mode": "navigate"})

        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]
        messages = [message for _category, message in _pending_flashes(client)]
        assert messages == ["请先登录。"], messages

    def test_a_client_without_fetch_metadata_keeps_the_old_behaviour(self, client):
        """curl / 测试客户端不发这个头 —— 它们必须照旧拿到跳转与提示。"""
        response = client.get(PROTECTED)

        assert response.status_code == 302
        assert [message for _c, message in _pending_flashes(client)] == ["请先登录。"]


class TestSuccessfulLoginClearsAStaleFlash:
    def _register(self, username: str):
        with app.app_context():
            register_user(username, PASSWORD, role="normal")

    def test_the_stale_message_is_not_shown_after_logging_in(self, client):
        username = _uid("flashuser")
        self._register(username)

        # 复现：登录页之前留下了一条「请先登录。」（它还没被消费掉）
        with client.session_transaction() as sess:
            sess["_flashes"] = [("error", "请先登录。")]

        token = _csrf(client)
        response = client.post(
            "/auth/login",
            data={"username": username, "password": PASSWORD, "csrf_token": token},
            headers={"X-CSRFToken": token},
            follow_redirects=False,
        )
        assert response.status_code == 302, response.status_code

        messages = [message for _c, message in _pending_flashes(client)]
        assert "请先登录。" not in messages, (
            f"登录成功后会话里还留着旧的「请先登录。」：{messages} —— "
            f"它会在下一个页面顶上显示出来"
        )
        assert "登录成功。" in messages, messages

    def test_visiting_the_login_page_while_logged_in_also_clears_it(self, client):
        username = _uid("flashuser2")
        self._register(username)
        token = _csrf(client)
        client.post(
            "/auth/login",
            data={"username": username, "password": PASSWORD, "csrf_token": token},
            headers={"X-CSRFToken": token},
        )

        with client.session_transaction() as sess:
            sess["_flashes"] = [("error", "请先登录。")]

        # 已登录时访问登录页 = 「直接跳 next」，这条分支也不该把旧提示带到下个页面
        response = client.get("/auth/login")
        assert response.status_code == 302
        assert _pending_flashes(client) == [], _pending_flashes(client)
