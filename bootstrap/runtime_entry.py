#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime entrypoint split from app.py __main__ block."""

from __future__ import annotations

import os
import signal
import sys
import threading


def _resolve_original_print(app_module):
    candidate = getattr(app_module, "_original_print", None)
    if callable(candidate):
        return candidate

    try:
        from utils.logger import _original_print as logger_original_print

        if callable(logger_original_print):
            return logger_original_print
    except Exception:
        pass

    return print


def _configure_runtime_io(_original_print):
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    _original_print("[TRACE] about to reconfigure stdout")
    try:
        sys.stdout.reconfigure(line_buffering=True)
        _original_print("[TRACE] stdout reconfigured")
    except Exception as exc:
        _original_print(f"[TRACE] stdout reconfigure failed: {exc}")
    try:
        sys.stderr.reconfigure(line_buffering=True)
        _original_print("[TRACE] stderr reconfigured")
    except Exception as exc:
        _original_print(f"[TRACE] stderr reconfigure failed: {exc}")


def run_runtime_entry(app_module):
    """Run startup/shutdown flow using app module runtime objects."""
    _original_print = _resolve_original_print(app_module)
    log_print = getattr(app_module, "log_print")
    cleanup_app = getattr(app_module, "cleanup_app")
    initialize_app = getattr(app_module, "initialize_app")
    clear_log_file = getattr(app_module, "clear_log_file")
    app = getattr(app_module, "app")
    deployment_mode = str(getattr(app_module, "DEPLOYMENT_MODE") or "single").strip().lower()

    _original_print("[TRACE] entered __main__")
    _configure_runtime_io(_original_print)

    shutdown_flag = threading.Event()
    _original_print("[TRACE] about to call clear_log_file")

    def signal_handler(signum, frame):
        log_print("\n接收到中断信号，正在关闭应用...", "APP")
        shutdown_flag.set()
        cleanup_app()
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        if deployment_mode == "agent":
            log_print("以 Agent 模式启动（不启动 Flask Web 服务）", "APP", force=True)
            from agent.runner_runtime import run_agent

            run_agent()
            sys.exit(0)

        clear_log_file()
        initialize_app()
        log_print("正在启动服务器...", "APP")
        log_print("按 Ctrl+C 停止服务器", "APP")
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8002"))
        app.run(debug=False, host=host, port=port, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        log_print("\n接收到键盘中断，正在关闭应用...", "APP")
        cleanup_app()
    except SystemExit as exc:
        import traceback

        _original_print(f"[DEBUG] SystemExit caught: code={exc.code}")
        traceback.print_exc()
    except Exception as exc:
        import traceback

        _original_print(f"[DEBUG] Exception caught: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        log_print(f"应用运行异常: {exc}", "APP", force=True)
        cleanup_app()
        sys.exit(1)
    finally:
        if not shutdown_flag.is_set():
            cleanup_app()
