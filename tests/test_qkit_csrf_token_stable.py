# -*- coding: utf-8 -*-
"""qkit 会话的 CSRF token 必须是**确定值**，且登录时就要钉死。

## 钉住的问题（线上报障形态）

用户报障：「创建周版本 → `创建失败：CSRF token invalid or missing.`」，
而且**刷新一下就好了**、`qkit` 登录态看起来一直是好的。

成因链条（`utils/request_security.py::csrf_token` + `enforce_csrf` +
`qkit_auth/providers.py::_derive_csrf_token_from_qkitjwt`）：

1. CSRF token 是**会话级**的，页面里那份来自渲染那一刻的会话
   （`templates/base.html` 的 `<meta name="csrf-token">`）；
2. 会话里没有 token 时，`csrf_token()` 会**随机**生成一个 R；
3. 会话 cookie 一旦被重建（进程重启、`FLASK_SECRET_KEY` 变更、浏览器丢 cookie、
   多进程密钥不一致），服务端会话变空，qkit 恢复路径派生出的是**确定值 D**；
4. 于是「页面拿着 R、服务端只有 D」—— 该页面上**所有**写操作都报
   「CSRF token invalid or missing.」，**刷新（重新渲染，拿到 D）即恢复**。

因为 qkitjwt cookie 还在，用户**始终是登录态**（恢复路径会把他重新登录回来），
所以报障听起来像权限或网络问题，而不是「会话丢了」。

## 修法

登录完成时（`qkit_auth/routes.py::_set_user_session`）无条件调用
`fix_session_csrf_token_from_qkitjwt`，把 token 钉成 D。此后：

* 页面渲染的就是 D（`csrf_token()` 只在缺失时才生成，已有值原样返回）；
* 会话丢了 → 恢复路径派生的还是同一个 D → **旧页面依旧能提交**。

## 这些测试各钉什么

* `test_login_pins_token_to_deterministic_value`：登录后会话里的 token == D；
* `test_page_token_survives_session_loss`：**核心**。渲染出的页面 token 在会话丢失、
  经恢复路径重建之后仍然相等（修复前：随机 R ≠ D，必红）；
* `test_login_overwrites_pre_login_random_token`：钉住「无条件覆盖」这一决定 ——
  若有人把调用换成只在缺失时才写的 `_ensure_session_csrf_token`，本测试会红，
  而上面那条也会跟着红（那正是修复前的形态）；
* `test_real_app_accepts_the_session_token`：走真实 app 的 `enforce_csrf`
  与真实 `/auth/login` 路由，确认「会话里的 token + 请求里带同一个」确实被放行 ——
  防止上面几条只在单元层面自洽。
"""
from __future__ import annotations

import os
import sys

import pytest
from flask import Flask, session

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from qkit_auth.models import QkitAuthUser  # noqa: E402
from qkit_auth.providers import (  # noqa: E402
    _derive_csrf_token_from_qkitjwt,
    _ensure_session_csrf_token,
)
from qkit_auth.routes import _set_user_session  # noqa: E402
from utils.request_security import csrf_token  # noqa: E402

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJ1aWQiOiJ0ZXN0dXNlciJ9.c2lnbmF0dXJl"


def _build_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "unit-test-secret"
    return app


def _qkit_user() -> QkitAuthUser:
    """不需要落库的临时用户对象（`_set_user_session` 只读它的字段/属性）。"""
    return QkitAuthUser(
        username="testuser",
        display_name="测试用户",
        email="testuser@corp.netease.com",
        role="normal",
        is_active=True,
    )


class TestTokenIsDeterministic:
    def test_login_pins_token_to_deterministic_value(self):
        app = _build_app()
        with app.test_request_context("/qkit_auth/after_login"):
            _set_user_session(_qkit_user(), token=_JWT)
            expected = _derive_csrf_token_from_qkitjwt(_JWT)
            assert expected, "派生值不该为空：这是整套机制的前提"
            assert session.get("_csrf_token") == expected, (
                "登录后会话里的 CSRF token 不是「由 qkitjwt 派生的确定值」。\n"
                "若它是随机的，会话一旦被重建，恢复路径派生的 D 就与页面里的对不上，"
                "用户在那个页面上点任何写操作都会收到「CSRF token invalid or missing.」。"
            )

    def test_login_overwrites_pre_login_random_token(self):
        """登录时**无条件**覆盖（不是「没有才写」）。

        `_set_user_session` 若改用只在缺失时才写的 `_ensure_session_csrf_token`，
        登录前那次匿名页面渲染留下的随机值会活下来 —— 那正是修复前的形态。
        """
        app = _build_app()
        with app.test_request_context("/qkit_auth/after_login"):
            session["_csrf_token"] = "pre-login-random-value"
            _set_user_session(_qkit_user(), token=_JWT)
            assert session["_csrf_token"] == _derive_csrf_token_from_qkitjwt(_JWT), (
                "登录没有覆盖登录前留下的随机 token。"
            )

    def test_derived_value_is_stable_across_calls(self):
        app = _build_app()
        with app.test_request_context("/"):
            first = _derive_csrf_token_from_qkitjwt(_JWT)
            second = _derive_csrf_token_from_qkitjwt(_JWT)
            assert first == second and first
            assert _derive_csrf_token_from_qkitjwt("") == "", "空 token 不该派生出值"
            assert first != _derive_csrf_token_from_qkitjwt(_JWT + "x"), (
                "不同 qkitjwt 必须派生出不同 token（否则换个账号也能复用旧 token）"
            )


class TestPageTokenSurvivesSessionLoss:
    def test_page_token_survives_session_loss(self):
        """核心回归：会话丢了、从 qkitjwt 恢复之后，**页面里那份 token 依然有效**。

        修复前这里是红的：页面拿到随机 R，恢复路径派生 D，R != D。
        """
        app = _build_app()
        with app.test_request_context("/qkit_auth/after_login"):
            _set_user_session(_qkit_user(), token=_JWT)
            page_token = csrf_token()  # 页面 meta 里会渲染出去的值

        with app.test_request_context("/api/something"):
            session.clear()  # 模拟会话 cookie 被重建（重启/换密钥/丢 cookie）
            _ensure_session_csrf_token(_JWT)  # 恢复路径把会话重建起来
            assert session.get("_csrf_token") == page_token, (
                "会话重建后派生出的 token 与页面里那份不一致："
                "用户手上那个页面会以「CSRF token invalid or missing.」告终，"
                "而他刷新一下又好了 —— 极难归因。"
            )


def _flashes(client) -> list[str]:
    """读尚未消费的 flash 文案（CSRF 失败是「302 + flash」，不是 400）。"""
    with client.session_transaction() as sess:
        return [str(message) for _category, message in sess.get("_flashes", [])]


class TestRealAppAcceptsTheSessionToken:
    """走真实 app 的 `enforce_csrf`，确认这不只是单元层面自洽。

    选 `POST /auth/login` 作靶子：它在 `AUTH_EXEMPT_PATHS/ENDPOINTS` 里，
    所以能**绕开登录门禁、单独打到 CSRF 校验**（`enforce_admin_access` 注册在它前面，
    换成需要登录的接口就只会看到 401，测不到 CSRF）。

    非 API 请求的 CSRF 失败是「302 + flash」，所以断言的是 flash 而不是响应体；
    并且带一个**不匹配 token** 的对照组 —— 否则「没看到报错」可能只是
    「这个接口根本没做 CSRF 校验」。
    """

    @pytest.fixture()
    def client(self):
        from app import app as real_app

        real_app.config["TESTING"] = True
        with real_app.test_client() as test_client:
            yield test_client

    def test_real_app_accepts_the_session_token(self, client):
        with client.session_transaction() as sess:
            sess["_csrf_token"] = "pinned-token-value"

        client.post("/auth/login", data={"next": "/", "_csrf_token": "pinned-token-value"})
        assert not [m for m in _flashes(client) if "CSRF token" in m], (
            "会话里的 token 与请求里带的一致，却被 CSRF 拒了。"
        )

        # 对照组：同一个会话、换一个对不上的 token，必须被拒。
        client.post("/auth/login", data={"next": "/", "_csrf_token": "some-other-value"})
        assert [m for m in _flashes(client) if "CSRF token" in m], (
            "对不上的 token 竟然没有被拒 —— 说明上面那条「没报错」毫无意义"
            "（CSRF 校验根本没执行）。"
        )
