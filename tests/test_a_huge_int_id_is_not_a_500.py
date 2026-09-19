# -*- coding: utf-8 -*-
"""路径段 / 查询参数里的**超大整数**不该打出 500。

## 缺陷形态

Werkzeug 的 `<int:…>` 转换器是 `/\d+/` + 裸 `int()`，**没有上界**。于是

    GET /ai-analysis/runs/99999999999999999999/usage

能正常匹配，handler 拿到一个 10^20 的整数，第一句 `db.session.get(Model, id)` 就把
这个值绑给 pysqlite，而 pysqlite 抛的是

    OverflowError: Python int too large to convert to SQLite INTEGER

**它不是 `SQLAlchemyError`**（`auth/providers.py` 只 `except SQLAlchemyError` 捕不住它），
仓库里也没有兜它的错误处理器 —— 于是任何已登录用户拿一条 URL 就能打出 500，
而这样的路由有几十条（`/commits/<id>/…`、`/projects/<id>/…`、`/ai-analysis/runs/<id>/…`）。

查询参数那一半是同一个病：`/ai-analysis/usage/overview?project=<大整数>` 走 `_parse_int`，
它只判 `> 0`，那个数原样进了 `filter(project_id == …)`。

## 口径

超出 64 位有符号范围（SQLite INTEGER 的上界）的 id **不可能存在**，所以：

* 路径段：按「这条规则不匹配」处理 → **404**；
* 查询参数：按「这个筛选值认不出来」处理 → **回落默认**，界面如实记一条 note
  （`parse_usage_filters` 的 docstring 承诺「任何非法值都回落默认，绝不抛异常」）。

## 两条曾经写错的地方（本文件的用例就是为它们存在的）

1. **必须真的登录**。第一版用了一个不存在的账号去 `POST /auth/login`，登录失败，
   于是请求被 302 到登录页 —— 断言写成 `status_code in (404, 302, 401, 403)` 就**恒真**，
   把 `bound_integer_url_converter` 整个删掉它照样绿。现在用真账号登录，并且断的是
   **确切的 404**。
2. **转换器装的时机**。`Rule.get_converter` 是在 `Rule.compile()`（由 `Map.add` 触发、
   即注册每一条路由的那一刻）里读 `self.map.converters` 的，所以换晚了已经注册的规则
   不会跟着变。实测两个更晚的位置都不行：只在 `configure_app_routing_bootstrap` 里装，
   蓝图那条规则编译出来的仍是 `IntegerConverter`（只有它后面补加的短名别名规则是新的），
   而匹配到的是蓝图那条，照样 500；放进 `configure_app_blueprints` 则漏掉注册得更早的
   `auth` 那批。**唯一正确的落点是 app 工厂**，`create_app()` 里那一句。
"""
from __future__ import annotations

import os
import re
import sys

import pytest
from werkzeug.routing import ValidationError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import app, create_tables  # noqa: E402
from auth.services import register_user  # noqa: E402
from services.app_routing_bootstrap_service import (  # noqa: E402
    MAX_INTEGER_ID,
    BoundedIntegerConverter,
)

PASSWORD = "HugeInt!2026"

# 刚好超过 64 位有符号数上界 —— 会炸的就是这个区间，而它在现实里也真的会出现
# （地址栏里多按一位、旧书签、脚本拼错）。
TOO_BIG = str(MAX_INTEGER_ID + 1)

# 路由文本里声明成整数的那些变量（`<int:name>`）。


_INT_VAR_RE = re.compile(r"<int:([A-Za-z_][A-Za-z0-9_]*)>")


def _admin_client():
    """**真登录**的客户端。

    第一版这里用的是不存在的账号，登录失败 → 请求被 302 到登录页 → 断言恒真。
    现在账号真建、密码真填，并断言登录确实成功了。
    """
    with app.app_context():
        create_tables()
        name = f"hugeint{os.urandom(4).hex()}"
        admin, error = register_user(name, PASSWORD, role="platform_admin")
        assert admin is not None, error

    client = app.test_client()
    with client.session_transaction() as session:
        session["_csrf_token"] = "probe"
    resp = client.post(
        "/auth/login",
        data={"username": name, "password": PASSWORD, "csrf_token": "probe"},
        headers={"X-CSRFToken": "probe"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302), f"登录没成功（{resp.status_code}），下面的断言会变成空转"
    return client


# 只列**任何登录用户都能打到**的那一批（不需要项目权限，正是这条最容易被利用的原因）。
HUGE_INT_PATHS = (
    "/ai-analysis/runs/{id}/usage",
    "/ai-analysis/runs/{id}/report",
    "/ai-analysis/runs/{id}/report.md",
    "/ai-analysis/runs/{id}/progress",
    "/ai-analysis/commit/{id}/history",
    "/ai-analysis/commit/{id}/latest",
    "/ai-analysis/weekly/{id}/latest",
    "/commits/{id}/diff/new",
)


@pytest.mark.parametrize("template", HUGE_INT_PATHS)
def test_a_huge_id_in_the_path_is_a_404_not_a_500(template):
    client = _admin_client()
    resp = client.get(template.format(id=TOO_BIG))
    assert resp.status_code == 404, (
        f"{template} 遇到超大整数 {TOO_BIG} 返回了 {resp.status_code}，期望 404 —— "
        "`<int:…>` 没有上界时那个数会一路进到数据库查询里抛 OverflowError"
    )


def test_a_huge_id_in_the_usage_query_is_not_a_500():
    """查询参数那一半走的是另一条路（`_parse_int`），转换器管不到它。"""
    client = _admin_client()
    resp = client.get(f"/ai-analysis/usage/overview?project={TOO_BIG}")
    assert resp.status_code == 200, resp.status_code


def test_the_converter_bounds_exactly_at_the_sqlite_integer_limit():
    """上界的取值要有依据（SQLite 的 INTEGER 是 64 位有符号数），且**不能收过头**。

    收过头的后果与不收一样糟：正常 id 也进不去 handler，路由变成永远 404。
    所以这一条同时钉住「上界本身能用、上界+1 用不了、正常值照常」。
    """
    converter = BoundedIntegerConverter(app.url_map)

    assert MAX_INTEGER_ID == 2**63 - 1
    assert converter.to_python(str(MAX_INTEGER_ID)) == MAX_INTEGER_ID
    assert converter.to_python("1") == 1
    with pytest.raises(ValidationError):
        converter.to_python(TOO_BIG)


def test_every_registered_int_route_compiled_with_the_bounded_converter():
    """**注册时机**：换转换器必须早于**每一条** `<int:…>` 路由的注册。

    这条是上一版真正漏掉的东西。`Rule.get_converter` 是在 `Rule.compile()`（由
    `Map.add` 触发，也就是注册每一条路由的那一刻）里读 `self.map.converters` 的，
    换晚了已经注册的规则不会跟着变。实测两个更早的位置都不行：

    * 只在 `configure_app_routing_bootstrap` 里装 → 蓝图那条规则仍是 `IntegerConverter`
      （只有它后面补加的短名别名规则是新的），而匹配到的是蓝图那条，照样 500；
    * 在 `configure_app_blueprints` 里装 → AI 那批好了，但注册得更早的 `auth` 那批
      （`/auth/api/users/<int:user_id>/…` 等）仍是旧的。

    所以唯一的落点是 app 工厂。这条用例就是钉住「不许再挪回去」。

    **只查 `<int:…>` 声明过的变量**：同一条规则里还有 `<path:>` / 字符串段，
    那些本来就不该是整数转换器（第一版不分青红皂白地把它们全报了出来）。
    """
    unbounded = []
    for rule in app.url_map.iter_rules():
        declared = set(_INT_VAR_RE.findall(rule.rule))
        converters = getattr(rule, "_converters", {}) or {}
        for name in declared:
            converter = converters.get(name)
            if not isinstance(converter, BoundedIntegerConverter):
                kind = type(converter).__name__ if converter is not None else "（没有转换器）"
                unbounded.append(f"{rule.rule} ({name}) → {kind}")
    assert not unbounded, (
        "这些 `<int:…>` 路由编译时用的不是带上界的转换器（换转换器的时机太晚了）：\n  "
        + "\n  ".join(sorted(set(unbounded))[:10])
    )
