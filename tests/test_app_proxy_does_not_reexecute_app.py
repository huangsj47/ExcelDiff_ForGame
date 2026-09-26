# -*- coding: utf-8 -*-
"""`python app.py` 起服务时，`weekly_version_logic` 那两个代理不许把 app.py 再执行一遍。

## 缺陷形态（真机日志，2026-09-26）

那两个代理原先查 `sys.modules['app']`，查不到就 `import app`。而 `python app.py` 启动时
app.py 在 `sys.modules` 里的名字是 **`__main__`** —— 于是兜底那一支**把整份 app.py 重新
执行了一遍**：蓝图重新注册、`services initialized` / `task_worker configured` 再跑一次、
`create_tables()` 再来一遍，日志里多出完整一段启动输出（`reached if __name__ check,
__name__='app'` —— 这一行只可能来自第二次执行；只有线程与调度器那几行有守卫，所以它们
各只出现一次，其余全部翻倍）。

修法不是「再认一个名字」，而是**取消这一跳**：这两个函数的本体在
`services.commit_diff_logic`，`app` 只是把它们转出来（这一跳是 `e223378 才分app.py文件`
留下的，那时它们还住在 app.py 里）。所以现在的判据是**根本不需要 app**：把 `app` 从
`sys.modules` 里拿掉、再把 `import app` 这条路拦死，两个代理照样取得到本体。
"""
from __future__ import annotations

import sys

import services.commit_diff_logic as commit_diff_logic
from services import weekly_version_logic


class _RefuseImportingApp:
    """拦住 `import app` —— 它在真机上的表现就是「把整份 app.py 再执行一遍」。

    真机下 `sys.modules` 里根本没有 `app` 这个名字（它叫 `__main__`），所以旧写法
    **一定**走这一支。这里让它当场失败，测试才能把「又与导入了一份 app.py」和
    「取到了本体」分开 —— 后者在两种写法下都成立，正是它让这个 bug 一直没被发现。
    """

    def find_spec(self, name, path=None, target=None):
        if name == "app":
            raise ImportError("`python app.py` 下没有 `app` 这个模块，这里也不许重新导入")
        return None


def _as_if_started_by_python_app_py(monkeypatch):
    """把进程摆成 `python app.py` 的样子：`sys.modules` 里没有 `app`，且不许再导入。"""
    monkeypatch.delitem(sys.modules, "app", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_RefuseImportingApp(), *sys.meta_path])


def test_the_merge_proxy_needs_no_app_module(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        commit_diff_logic, "get_real_diff_data_for_merge", lambda commit: sentinel
    )
    _as_if_started_by_python_app_py(monkeypatch)

    assert weekly_version_logic.get_real_diff_data_for_merge(object()) is sentinel


def test_the_pair_diff_proxy_needs_no_app_module(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        commit_diff_logic, "get_commit_pair_diff_internal", lambda current, previous: sentinel
    )
    _as_if_started_by_python_app_py(monkeypatch)

    assert weekly_version_logic.get_commit_pair_diff_internal(object(), object()) is sentinel
