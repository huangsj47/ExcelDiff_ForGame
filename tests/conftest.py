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
import uuid
import shutil
import atexit
from pathlib import Path

import pytest
import tempfile
import _pytest.pathlib as _pytest_pathlib
import _pytest.tmpdir as _pytest_tmpdir

from utils.db_config import get_sqlite_path_from_uri
from utils.db_safety import is_temp_sqlite_path

# Guard pytest cleanup against permission quirks on this host.
_orig_cleanup_dead_symlinks = _pytest_pathlib.cleanup_dead_symlinks


def _safe_cleanup_dead_symlinks(root):
    try:
        return _orig_cleanup_dead_symlinks(root)
    except PermissionError:
        return None


_pytest_pathlib.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks
_pytest_tmpdir.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks

# Force temp dirs into workspace to avoid permission issues.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_pytest_tmp_parent = os.path.join(PROJECT_ROOT, ".pytest_tmp")
_pytest_basetemp_root = os.path.join(_pytest_tmp_parent, f"run_{uuid.uuid4().hex}")
_pytest_work_root = os.path.join(_pytest_tmp_parent, "work")
_pytest_db_root = os.path.join(_pytest_tmp_parent, "db")
os.makedirs(_pytest_basetemp_root, exist_ok=True)
os.makedirs(_pytest_work_root, exist_ok=True)
os.makedirs(_pytest_db_root, exist_ok=True)
os.environ.setdefault("PYTEST_TMPDIR", _pytest_basetemp_root)
os.environ.setdefault("TMPDIR", _pytest_work_root)
os.environ.setdefault("TEMP", _pytest_work_root)
os.environ.setdefault("TMP", _pytest_work_root)
tempfile.tempdir = _pytest_work_root


def _safe_mkdtemp(suffix=None, prefix=None, dir=None):
    suffix = "" if suffix is None else str(suffix)
    prefix = "tmp" if prefix is None else str(prefix)
    base = dir or _pytest_work_root
    os.makedirs(base, exist_ok=True)
    while True:
        name = f"{prefix}{uuid.uuid4().hex}{suffix}"
        path = os.path.join(base, name)
        try:
            os.makedirs(path, exist_ok=False)
            return path
        except FileExistsError:
            continue


class _SafeTemporaryDirectory:
    def __init__(self, suffix=None, prefix=None, dir=None):
        self.name = _safe_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)

    def __enter__(self):
        return self.name

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()

    def cleanup(self):
        shutil.rmtree(self.name, ignore_errors=True)


# Override tempfile helpers to avoid restricted temp folders on this host.
tempfile.mkdtemp = _safe_mkdtemp
tempfile.TemporaryDirectory = _SafeTemporaryDirectory

# Ensure app.py sees TESTING before any module import.
os.environ.setdefault("TESTING", "1")

# ── 在任何测试 import app 之前，保存原始 I/O 对象 ──
_real_stdout = sys.stdout
_real_stderr = sys.stderr
_real_print = builtins.print
_created_test_db_path = None
_cleanup_test_db_on_exit = True

# Ignore pytest tmp folders with restricted permissions.
collect_ignore = ["_pytest_tmp_run"]


class _NonClosingStream:
    """Proxy stream that ignores close() to keep pytest terminal output alive."""
    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        try:
            return self._stream.write(data)
        except ValueError:
            # Ignore writes to closed stream to keep pytest alive.
            return 0

    def flush(self):
        try:
            return self._stream.flush()
        except ValueError:
            return None

    def close(self):
        # Intentionally ignore close to avoid losing terminal output.
        return None

    @property
    def closed(self):
        return False

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _wrap_stream(stream):
    if stream is None:
        return None
    if isinstance(stream, _NonClosingStream):
        return stream
    return _NonClosingStream(stream)


# Wrap streams early to prevent accidental close during tests.
sys.stdout = _wrap_stream(sys.stdout)
sys.stderr = _wrap_stream(sys.stderr)


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
        tmp_file = tempfile.NamedTemporaryFile(
            prefix="diff_platform_test_",
            suffix=".db",
            delete=False,
            dir=_pytest_db_root,
        )
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
    def _pick_stream(primary, fallback):
        for stream in (primary, fallback):
            if stream is None:
                continue
            if getattr(stream, "closed", False):
                continue
            return stream
        return primary or fallback

    # 恢复 stdout
    try:
        desired_stdout = _pick_stream(_real_stdout, getattr(sys, "__stdout__", None))
        if sys.stdout is not desired_stdout:
            sys.stdout = _wrap_stream(desired_stdout)
    except Exception:
        sys.stdout = _wrap_stream(_pick_stream(_real_stdout, getattr(sys, "__stdout__", None)))

    # 恢复 stderr
    try:
        desired_stderr = _pick_stream(_real_stderr, getattr(sys, "__stderr__", None))
        if sys.stderr is not desired_stderr:
            sys.stderr = _wrap_stream(desired_stderr)
    except Exception:
        sys.stderr = _wrap_stream(_pick_stream(_real_stderr, getattr(sys, "__stderr__", None)))

    # 恢复 print
    if builtins.print is not _real_print:
        builtins.print = _real_print


def pytest_configure(config):
    """pytest 初始化最早阶段，设置环境变量阻止 app.py 的 IO 副作用"""
    if getattr(config.option, "basetemp", None) is None:
        config.option.basetemp = _pytest_basetemp_root
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


@pytest.fixture
def tmp_path():
    """Custom tmp_path to avoid pytest basetemp permission issues on this host."""
    path = Path(_safe_mkdtemp(prefix="pytest-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    """会话结束前，确保终端输出可写，避免 pytest 退出时 IO 关闭。"""
    _guard_io()
