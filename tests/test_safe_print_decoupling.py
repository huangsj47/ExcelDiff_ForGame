import io
import sys
from pathlib import Path
from types import SimpleNamespace

from utils import safe_print as safe_print_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_safe_print_module_has_no_direct_app_import():
    content = _read("utils/safe_print.py")
    assert "from app import" not in content


def test_get_app_log_print_returns_none_when_app_not_loaded(monkeypatch):
    monkeypatch.delitem(sys.modules, "app", raising=False)
    monkeypatch.delitem(sys.modules, "utils.logger", raising=False)
    app_log_print, log_level = safe_print_module._get_logger_log_print()
    assert app_log_print is None
    assert log_level == {}


def test_safe_print_delegates_to_loaded_app_log_print(monkeypatch):
    calls = []

    def fake_log_print(message, log_type, force):
        calls.append((message, log_type, force))

    fake_app = SimpleNamespace(log_print=fake_log_print, LOG_LEVEL={"INFO_VERBOSE": True})
    monkeypatch.setitem(sys.modules, "app", fake_app)

    safe_print_module.safe_print("hello", "INFO", True)
    assert calls == [("hello", "INFO", True)]


def test_safe_print_fallback_writes_stdout_when_app_missing(monkeypatch):
    monkeypatch.delitem(sys.modules, "app", raising=False)
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)

    safe_print_module.safe_print("fallback-message", "INFO", True)
    assert "fallback-message" in buffer.getvalue()

