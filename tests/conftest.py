"""
conftest.py — 在测试环境中保护 sys.stdout/stderr 和 builtins.print
防止 app.py 模块级副作用（stdout 重包装 / print 重载）导致
pytest teardown 阶段出现 "I/O operation on closed file" 错误。
"""

import builtins
import sys
import os
import io

# ── 在任何测试 import app 之前，保存原始 I/O 对象 ──
_real_stdout = sys.stdout
_real_stderr = sys.stderr
_real_print = builtins.print


def _guard_io():
    """确保 sys.stdout/stderr/builtins.print 为健康状态"""
    # 恢复 stdout
    try:
        if sys.stdout is not _real_stdout:
            # 如果当前的 stdout 已经关闭，必须恢复
            if hasattr(sys.stdout, 'closed') and sys.stdout.closed:
                sys.stdout = _real_stdout
            else:
                sys.stdout = _real_stdout
    except Exception:
        sys.stdout = _real_stdout

    # 恢复 stderr
    try:
        if sys.stderr is not _real_stderr:
            if hasattr(sys.stderr, 'closed') and sys.stderr.closed:
                sys.stderr = _real_stderr
            else:
                sys.stderr = _real_stderr
    except Exception:
        sys.stderr = _real_stderr

    # 恢复 print
    if builtins.print is not _real_print:
        builtins.print = _real_print


def pytest_configure(config):
    """pytest 初始化最早阶段，设置环境变量阻止 app.py 的 IO 副作用"""
    # 告诉 app.py 不要修改 stdout（如果 app.py 支持的话）
    os.environ.setdefault("TESTING", "1")
    _guard_io()


def pytest_runtest_setup(item):
    """每个测试开始前，确保 stdout/stderr/print 为原始对象"""
    _guard_io()


def pytest_runtest_teardown(item, nextitem):
    """每个测试结束后，恢复 stdout/stderr/print"""
    _guard_io()


def pytest_runtest_call(item):
    """每个测试执行前，再次确保 IO 正常"""
    _guard_io()