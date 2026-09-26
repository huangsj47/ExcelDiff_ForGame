# -*- coding: utf-8 -*-
"""会写库的路由不得允许 GET —— GET 直接绕过 CSRF 保护。

## 为什么需要这个测试

`services/app_security_bootstrap_service.py::enforce_csrf` 的第一条就是：

    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None

设计上没错（GET 应当是安全的），但仓库里有三个接口**在 GET 里写库**，
于是它们完全落在 CSRF 保护之外：

* `/api/excel-html-cache/clear?force_all=true`
    → `DELETE FROM excel_html_cache`（全平台清空），且原先没有 `@require_admin`，
      任意已登录用户都能调。浏览器对顶层导航会带上会话 Cookie，
      攻击者页面 `location.href = '.../clear?force_all=true'` 即可在受害者
      登录态下清空全平台 HTML diff 缓存。
* `/api/excel-html-cache/regenerate`
    → `db.session.delete(ExcelHtmlCache 行)` + `commit()`，再对查询串里
      任意 `repository_id` 触发 `get_excel_diff_data()` 重算（可被拿来刷 CPU）。
* `/update_commit_fields`
    → 遍历并改写 commits_log 里所有 `version`/`operation` 为空的记录
      （`<img src>` 即可在管理员会话下触发全表改写）。

三者都已改为 POST。本文件把「方法集合」和「非管理员会被拒」两件事都钉住。
"""
from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(scope="module")
def real_app():
    """真实应用（conftest 已把测试库隔离到 .pytest_tmp 下）。"""
    from app import app as flask_app

    return flask_app


def _methods_for_path(app, path):
    methods = set()
    for rule in app.url_map.iter_rules():
        if str(rule) == path:
            methods |= set(rule.methods)
    return methods


def _status_of(resp):
    if isinstance(resp, tuple):
        return resp[1]
    return getattr(resp, "status_code", None)


class TestCacheWriteRoutesAreNotGet:
    WRITE_PATHS = (
        "/api/excel-html-cache/clear",
        "/api/excel-html-cache/regenerate",
        # 删一条 AI 历次结论（连带它的报告正文、逐轮轨迹与结论清单，不可逆）。
        "/ai-analysis/runs/<int:run_id>/delete",
    )

    def test_paths_are_post_only(self, real_app):
        for path in self.WRITE_PATHS:
            methods = _methods_for_path(real_app, path)
            assert methods, f"{path} 没有任何 rule"
            assert "GET" not in methods, (
                f"{path} 仍然允许 GET，而 `enforce_csrf` 对 GET 直接放行 —— "
                f"这个接口会 DELETE/重算缓存，等于完全无 CSRF 保护。"
            )
            assert "POST" in methods, f"{path} 必须允许 POST"

    def test_clear_view_rejects_non_admin(self, real_app, monkeypatch):
        """`/api/excel-html-cache/clear` 必须带 @require_admin。

        原先它没有 —— 而同文件其余 7 个清理/重置接口全都有。
        """
        from utils import request_security

        monkeypatch.setattr(request_security, "ENABLE_ADMIN_SECURITY", True)
        monkeypatch.setattr(request_security, "_has_admin_access", lambda: False)

        view = real_app.view_functions["cache_management.clear_excel_html_cache"]
        with real_app.test_request_context(
            "/api/excel-html-cache/clear?force_all=true",
            method="POST",
            headers={"Accept": "application/json"},
        ):
            resp = view()

        assert _status_of(resp) in (401, 403), (
            f"非平台管理员调用 clear?force_all=true 应被拒绝，实际返回 {resp!r}。"
            f"注意 force_all=true 会执行 `DELETE FROM excel_html_cache`（全平台清空）。"
        )

    def test_regenerate_view_rejects_non_admin(self, real_app, monkeypatch):
        from utils import request_security

        monkeypatch.setattr(request_security, "ENABLE_ADMIN_SECURITY", True)
        monkeypatch.setattr(request_security, "_has_admin_access", lambda: False)

        view = real_app.view_functions["cache_management.regenerate_excel_html_cache"]
        with real_app.test_request_context(
            "/api/excel-html-cache/regenerate",
            method="POST",
            headers={"Accept": "application/json"},
        ):
            resp = view()

        assert _status_of(resp) in (401, 403), (
            f"非平台管理员调用 regenerate 应被拒绝，实际 {resp!r} —— "
            f"它会删缓存行并对任意 repository_id 触发重算。"
        )

    def test_update_commit_fields_route_is_post_only(self, real_app):
        """`/update_commit_fields` 会批量改写 commits_log，不能是 GET。"""
        methods = _methods_for_path(real_app, "/update_commit_fields")
        assert methods, "/update_commit_fields 没有任何 rule"
        assert "GET" not in methods, (
            "该接口遍历并改写所有 version/operation 为空的提交记录，"
            "GET 会绕过 CSRF（`<img src>` 即可在管理员会话下触发全表改写）。"
        )


class TestFrontendCallersUsePost:
    """改方法后前端调用方必须跟着改，否则界面按钮会静默失效。"""

    TEMPLATE = "templates/excel_cache_management.html"

    def test_clear_calls_use_post(self):
        path = os.path.join(PROJECT_ROOT, self.TEMPLATE)
        source = open(path, encoding="utf-8").read()
        assert "/api/excel-html-cache/clear?force_all=false'" in source
        for flag in ("force_all=false", "force_all=true"):
            snippet_at = source.index(f"/api/excel-html-cache/clear?{flag}")
            call = source[max(0, snippet_at - 120) : snippet_at + 90]
            assert "method: 'POST'" in call, (
                f"{self.TEMPLATE} 里 `{flag}` 的调用没有改成 POST —— "
                f"该路由现在只接受 POST，按钮会静默失败。"
            )
