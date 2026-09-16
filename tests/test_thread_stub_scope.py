"""测试替身的作用域：不许把全局 `threading` 模块整个换掉。

## 问题

有一类测试需要拦住「后台线程启动」，好让线程体同步执行（否则断言跑在线程之前，
或者真去 clone 仓库）。早先的做法是：

    monkeypatch.setattr("services.repository_creation_handlers.threading.Thread", _NoStartThread)

带点的路径是**逐级 getattr**：`services.repository_creation_handlers.threading`
拿到的是**全局 threading 模块对象本身**，再 `.Thread = 桩` 改的是它的属性 ——
于是 `threading.Thread` 在整个进程里被换掉了，不只是被测模块那一次引用。

后果：进程里任何别的地方都会拿到桩。gitpython 执行 git 命令时会用
`threading.Thread(...)` 起线程读子进程管道，然后 `.join()`；桩没有 `join()`，
报 AttributeError，而且栈指向跟被测代码毫无关系的地方。当时测试是绿的
（被测路径没触发真实子进程），但它给后续任何真实子进程调用埋了雷。

正确做法是把模块的 `threading` **名字**换成代理（见 tests/conftest.py 的
`stub_thread_in`），或者干脆在生产代码里留一个模块级接缝
（见 services/repository_update_form_service.py 的 `start_background_thread`）。

本文件就是这两条约束的守卫：一条静态扫描，一条真实行为断言。
"""

from __future__ import annotations

import re
import threading as real_threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

# 本文件自己会写出这些模式（就在下面几行），扫描时排除掉。
_SELF = Path(__file__).name

# 形如 "....threading.Thread" 的 monkeypatch 目标：以 threading.Thread 结尾的
# 带点路径。`setattr(threading, "Thread", ...)` 这种直接写法也一并覆盖。
_LEAK_PATTERNS = (
    re.compile(r"""setattr\(\s*["'][\w.]*threading\.Thread["']"""),
    re.compile(r"""setattr\(\s*threading\s*,\s*["']Thread["']"""),
)


def test_no_test_patches_the_global_threading_module():
    """静态扫描：测试里不许出现会改到全局 threading.Thread 的写法。"""
    offenders = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == _SELF:
            continue
        source = path.read_text(encoding="utf-8")
        for pattern in _LEAK_PATTERNS:
            for match in pattern.finditer(source):
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line} {match.group(0)}")
    assert not offenders, (
        "以下测试会把全局 threading.Thread 换掉（影响 pytest 自身与 gitpython 等）：\n  "
        + "\n  ".join(offenders)
        + "\n改用 tests/conftest.py 的 stub_thread_in，或在生产代码里留模块级接缝。"
    )


def test_stub_thread_in_only_touches_its_own_module(stub_thread_in):
    """行为断言：替换后，被测模块拿到桩，全局 threading.Thread 纹丝不动。"""
    import services.repository_creation_handlers as handlers

    real_thread = real_threading.Thread
    real_lock = real_threading.Lock

    class _StubThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    stub_thread_in("services.repository_creation_handlers", _StubThread)

    assert handlers.threading.Thread is _StubThread, "被测模块应拿到桩"
    # 这两条才是关键：换个写法（monkeypatch 带点路径打到 .Thread 上）就会红。
    assert real_threading.Thread is real_thread, "全局 threading.Thread 被换掉了"
    assert handlers.threading.Lock is real_lock, "代理应把其余属性透传给真的 threading"


def test_start_background_thread_is_a_replaceable_seam(monkeypatch):
    """repository_update_form_service 的后台线程启动必须能在**本模块内**替换掉。

    该模块的 `threading` 是函数内 import 的，没有模块级属性可打 ——
    所以才需要 `start_background_thread` 这个接缝。
    """
    import services.repository_update_form_service as service

    calls = []
    monkeypatch.setattr(
        service, "start_background_thread", lambda target, **kwargs: calls.append(target)
    )
    service.start_background_thread(lambda: None)
    assert len(calls) == 1

    # 撤掉替身，再调真的实现：必须真的起线程执行线程体，且不碰全局 threading
    monkeypatch.undo()
    real_thread = real_threading.Thread
    started = []
    thread = service.start_background_thread(lambda: started.append(True))
    thread.join(timeout=5)
    assert started == [True], "start_background_thread 没有真的执行线程体"
    assert thread.daemon is True, "后台线程应显式设成 daemon"
    assert real_threading.Thread is real_thread


def test_update_form_service_starts_threads_only_through_the_seam():
    """本模块必须**只**通过接缝起线程。

    光有 `start_background_thread` 这个函数不够：调用处如果仍旧自己
    `threading.Thread(...)`，接缝就是个没人走的摆设 —— 测试替换了它，
    真实线程照起，异步断言照样可能过（实测：那条变异是绿的）。
    """
    source = (REPO_ROOT / "services" / "repository_update_form_service.py").read_text(encoding="utf-8")
    seam = source.index("def start_background_thread(")
    next_def = source.index("\ndef ", seam + 1)
    body, outside = source[seam:next_def], source[:seam] + source[next_def:]

    assert "threading.Thread(" in body, "接缝函数本身应当真的创建线程"
    assert "threading.Thread(" not in outside, (
        "除 start_background_thread 外，本模块不应再直接创建线程 —— "
        "那样测试就没有模块内的下手点，只能去改全局 threading.Thread。"
    )
