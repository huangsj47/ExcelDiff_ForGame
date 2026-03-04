"""
conftest.py — 在测试环境中保护 sys.stdout/stderr 和 builtins.print
防止 app.py 模块级副作用（stdout 重包装 / print 重载）导致
pytest teardown 阶段出现 "I/O operation on closed file" 错误。
"""

import builtins
import sys
import os
import io
import tempfile
import atexit

from utils.db_config import get_sqlite_path_from_uri
from utils.db_safety import is_temp_sqlite_path

# ── 在任何测试 import app 之前，保存原始 I/O 对象 ──
_real_stdout = sys.stdout
_real_stderr = sys.stderr
_real_print = builtins.print
_created_test_db_path = None
_cleanup_test_db_on_exit = True


def _ensure_test_sqlite_db_env():
    """Force tests to use an isolated sqlite file, never production DB config."""
    global _created_test_db_path, _cleanup_test_db_on_exit

    # Optional explicit path for debugging, still must be test-only.
    explicit_path = os.environ.get("PYTEST_SQLITE_DB_PATH")
    if explicit_path:
        _cleanup_test_db_on_exit = False
        db_path = os.path.abspath(explicit_path)
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(db_path):
            open(db_path, "a", encoding="utf-8").close()
        _created_test_db_path = db_path
    elif not _created_test_db_path:
        _cleanup_test_db_on_exit = True
        tmp_file = tempfile.NamedTemporaryFile(prefix="diff_platform_test_", suffix=".db", delete=False)
        tmp_file.close()
        _created_test_db_path = tmp_file.name

    assert _created_test_db_path is not None
    sqlite_uri = f"sqlite:///{os.path.abspath(_created_test_db_path).replace('\\', '/')}"
    os.environ["DB_BACKEND"] = "sqlite"
    os.environ["SQLITE_DB_PATH"] = _created_test_db_path
    # DATABASE_URL has higher priority in app.py, force it to temp sqlite.
    os.environ["DATABASE_URL"] = sqlite_uri
    # Never allow global bypass of destructive guard in tests.
    os.environ["ALLOW_DESTRUCTIVE_DB_OPS"] = "false"


def _assert_test_db_isolation():
    """Fail fast if runtime DB target is not a temp sqlite database."""
    database_url = str(os.environ.get("DATABASE_URL", "") or "")
    if database_url and not database_url.lower().startswith("sqlite:///"):
        raise RuntimeError(
            f"测试环境数据库不安全: DATABASE_URL={database_url}。"
            "测试必须使用临时 sqlite 数据库。"
        )

    app_module = sys.modules.get("app")
    if app_module is None:
        return

    # 某些单测会临时替换 sys.modules["app"] 为 SimpleNamespace，直接跳过即可。
    flask_app = getattr(app_module, "app", None)
    if flask_app is None or not hasattr(flask_app, "config"):
        return

    runtime_uri = str(flask_app.config.get("SQLALCHEMY_DATABASE_URI", "") or "")
    sqlite_path = get_sqlite_path_from_uri(runtime_uri)
    if not sqlite_path or not is_temp_sqlite_path(sqlite_path):
        raise RuntimeError(
            "检测到测试运行时数据库不是临时 sqlite，已中止测试以保护正式数据。"
            f" runtime_uri={runtime_uri} sqlite_path={sqlite_path}"
        )


def _cleanup_test_sqlite_db():
    """Best-effort cleanup of temp sqlite files created for tests."""
    if not _cleanup_test_db_on_exit:
        return
    if not _created_test_db_path:
        return
    for path in (_created_test_db_path, f"{_created_test_db_path}-wal", f"{_created_test_db_path}-shm"):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


atexit.register(_cleanup_test_sqlite_db)
# 在 conftest 导入阶段立即生效，确保先于 test module import app。
_ensure_test_sqlite_db_env()


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
    _ensure_test_sqlite_db_env()
    _assert_test_db_isolation()
    _guard_io()


def pytest_runtest_setup(item):
    """每个测试开始前，确保 stdout/stderr/print 为原始对象"""
    _assert_test_db_isolation()
    _guard_io()


def pytest_runtest_teardown(item, nextitem):
    """每个测试结束后，恢复 stdout/stderr/print"""
    _assert_test_db_isolation()
    _guard_io()


def pytest_runtest_call(item):
    """每个测试执行前，再次确保 IO 正常"""
    _assert_test_db_isolation()
    _guard_io()
