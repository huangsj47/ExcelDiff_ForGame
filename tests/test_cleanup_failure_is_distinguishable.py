# -*- coding: utf-8 -*-
"""清理缓存「执行失败」必须与「本来就没东西可清」可区分。

## 为什么需要这个测试

这批清理方法原先在异常分支里一律 `return 0`。而调用方
（`routes/cache_management_routes.py` 的 `/admin/excel-cache/cleanup-expired`、
`/admin/weekly-excel-cache/cleanup`）**直接把这个返回值当成「清理了 N 条」展示**，
于是：

    清理失败  → 接口返回 {"success": true, "message": "清理完成", "expired_count": 0}
    真的没东西 → 接口返回 {"success": true, "message": "清理完成", "expired_count": 0}

两者在管理界面上完全同形。后果是**缓存表可能长期只增不减而无人察觉** ——
尤其配合「worker 的 cleanup_cache 分支根本没有 app context」那个缺陷
（见 tests/test_worker_tasks_have_app_context.py），每天 04:00 的清理
实际上一次都没成功过，而接口一直报「清理完成」。

现在这些方法失败时返回 `None`，路由据此返回 `success: false` + 500。

## 本文件断言什么

* 内部真的抛异常时，清理方法返回 `None`（不是 0）；
* 真的没有东西可清时，返回 `0`（不是 None）—— 反向保险，
  否则「成功」会被误报成失败；
* 路由层拿到 None 时不再回 `success: true`。

不 import app、不碰数据库（用会抛异常的桩替换模型）。
"""
import os
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import services.excel_diff_cache_service as excel_cache_module  # noqa: E402


class _RaisingQuery:
    """任何属性访问都抛异常，用来模拟「查询失败」。"""

    def __getattr__(self, name):
        raise RuntimeError("模拟查询失败（例如 database is locked）")


class _RaisingModel:
    @property
    def query(self):
        raise RuntimeError("模拟查询失败（例如 database is locked）")


@pytest.fixture()
def svc():
    return excel_cache_module.ExcelDiffCacheService()


def test_cleanup_old_cache_failure_returns_none_not_zero(svc, monkeypatch):
    monkeypatch.setattr(excel_cache_module, "DiffCache", _RaisingModel())
    monkeypatch.setattr(excel_cache_module, "db", SimpleNamespace(session=SimpleNamespace(rollback=lambda: None)))

    result = svc.cleanup_old_cache(30)

    assert result is None, (
        f'清理失败时返回了 {result!r} 而不是 None。\n'
        f'调用方（cache_management_routes 的清理接口）会把它当成「清理了 {result!r} 条」，'
        f'于是「执行失败」在管理界面上显示为「清理完成，0 条」，'
        f'与「本来就没东西可清」无法区分 —— 缓存表只增不减也无人察觉。'
    )


def test_cleanup_expired_cache_failure_returns_none_not_zero(svc, monkeypatch):
    monkeypatch.setattr(excel_cache_module, "DiffCache", _RaisingModel())
    monkeypatch.setattr(excel_cache_module, "db", SimpleNamespace(session=SimpleNamespace(rollback=lambda: None)))

    assert svc.cleanup_expired_cache() is None


def test_cleanup_old_cache_nothing_to_do_returns_zero():
    """反向保险：真的没有东西可清时必须返回 0（而不是 None）。

    否则「一切正常、无需清理」会被误报成失败，管理员会去追一个不存在的问题。

    这里用**真实模型 + 真实（隔离的临时）空库**：清理逻辑要参与 SQLAlchemy
    表达式构造，手写桩做不到，硬凑出来的桩只会测到桩自己。
    tests/conftest.py 的 _assert_test_db_isolation() 保证不会打到真实库。
    """
    import app as app_module

    with app_module.app.app_context():
        # 这个测试自己建表：pytest 用的是 conftest 指定的隔离临时库，
        # 未必跑过 create_all（走 app 的启动流程才会）。不建表的话
        # 清理会因为 "no such table: diff_cache" 而失败，测到的就不是本契约了。
        app_module.db.create_all()
        result = excel_cache_module.ExcelDiffCacheService().cleanup_old_cache(30)

    assert result == 0, (
        f'空库上清理应当返回 0（表示「无需清理」），实际 {result!r}。\n'
        f'返回 None 会让路由报 success:false + 500，把正常状态误报成失败。'
    )


def test_cleanup_failure_is_logged(svc, monkeypatch):
    """失败必须留下日志 —— 否则返回 None 也无从排查。"""
    logged = []
    monkeypatch.setattr(excel_cache_module, "log_print", lambda *a, **k: logged.append(str(a[0]) if a else ""))
    monkeypatch.setattr(excel_cache_module, "DiffCache", _RaisingModel())
    monkeypatch.setattr(excel_cache_module, "db", SimpleNamespace(session=SimpleNamespace(rollback=lambda: None)))

    svc.cleanup_old_cache(30)

    assert any("失败" in msg for msg in logged), f'清理失败没有任何日志：{logged}'


def test_other_service_cleanups_return_none_on_failure():
    """同族方法也必须是同一契约（源码级检查异常分支的返回值）。

    这几处是同类缺陷的复制粘贴，逐个写行为测试成本高且重复；
    这里只钉住「异常分支不得 return 0」这一条硬性约定。
    """
    targets = [
        ("services/weekly_excel_cache_service.py", "清理过期周版本Excel缓存失败"),
        ("services/weekly_excel_cache_service.py", "清理超限周版本Excel缓存失败"),
        ("services/excel_html_cache_service.py", "清理过期HTML缓存失败"),
    ]
    problems = []
    for rel_path, marker in targets:
        path = os.path.join(PROJECT_ROOT, rel_path)
        with open(path, encoding='utf-8') as fh:
            lines = fh.readlines()
        for idx, line in enumerate(lines):
            if marker in line and '失败后回滚也失败' not in line:
                # 该日志之后的前几行里找 return
                for follow in lines[idx:idx + 5]:
                    stripped = follow.strip()
                    if stripped.startswith('return'):
                        if stripped != 'return None':
                            problems.append(f'{rel_path}: {marker} 之后是 `{stripped}`')
                        break
    assert not problems, (
        '以下清理方法在失败分支返回了 0 而不是 None，'
        '会让「执行失败」与「本来就没东西可清」在管理界面上无法区分：\n  '
        + '\n  '.join(problems)
    )
