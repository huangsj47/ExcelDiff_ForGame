# -*- coding: utf-8 -*-
r"""登录后的 `next` 跳转不许跳去站外。

## 为什么要单独钉一条

`_is_safe_redirect` 是登录页那道「跳回你原来想去的页面」（`?next=`）的守门人：
它放行了，浏览器就真的会跳过去，而**登录页是钓鱼最常借用的第一跳**
（链接看着是自家域名，落地页是别人的）。

它的判据是「`urlparse` 出来的 netloc 为空、或者是本站」—— 而 `urlparse` 与**浏览器**
对同一个串的理解并不总是一样：浏览器按 WHATWG 规则会把 `\\` 归一成 `/`，
于是 `/\evil.example.com` 在 `urlparse` 眼里是「站内路径」（netloc 为空），
在浏览器眼里却是 `//evil.example.com`（协议相对地址，跳到站外）。
制表符/换行同理 —— 浏览器会把它们**剥掉**，剥完可能露出 `//`。

所以这里钉两条：真站内的路径照旧放行（不能靠「一律拒绝」通过），
反斜杠/控制字符那几种形态一律拒绝。
"""
from __future__ import annotations

import pytest

from app import app
from utils.request_security import _is_safe_redirect

HOST_BASE = "http://10.226.98.33:8002"


def _check(target: str) -> bool:
    with app.test_request_context("/auth/login", base_url=HOST_BASE):
        return _is_safe_redirect(target)


def test_real_in_site_paths_are_still_allowed():
    """**不能靠把跳转关掉来通过**：站内路径必须照旧放行，否则登录后回不到原页面。"""
    for target in (
        "/commits/1",
        "/weekly-version-config/2/diff",
        "/index",
        "/merged-project/3?tab=ai",
        HOST_BASE + "/commits/1",          # 绝对地址但就是本站
    ):
        assert _check(target) is True, target


def test_other_hosts_are_refused():
    for target in (
        "//evil.example.com",
        "http://evil.example.com/x",
        "https://evil.example.com",
    ):
        assert _check(target) is False, target


@pytest.mark.parametrize(
    "target",
    [
        "/\\evil.example.com",       # 浏览器当成 //evil.example.com
        "\\/evil.example.com",
        "/\tevil.example.com",       # 制表符会被剥掉，露出 //
        "/\nevil.example.com",
        "//\tevil.example.com",
    ],
)
def test_backslash_and_control_characters_are_refused(target):
    """这几种串在 `urlparse` 眼里是站内、在浏览器眼里是站外 —— 必须拒绝。"""
    assert _check(target) is False, repr(target)


def test_an_empty_target_is_refused():
    for target in ("", None):
        assert _check(target) is False, repr(target)


def test_a_non_http_scheme_is_refused():
    """`javascript:` / `data:` 这类同样不能进 Location。"""
    for target in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd"):
        assert _check(target) is False, target
