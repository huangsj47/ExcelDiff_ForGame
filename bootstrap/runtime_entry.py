#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runtime entrypoint split from app.py __main__ block."""

from __future__ import annotations

import os
import pathlib
import signal
import sys
import threading

# 仓根（app.py 所在目录）。`.env` 的定位必须与 app.py 顶部
# `os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")` 完全一致 ——
# 否则从别的 cwd 启动时这里查的是另一个文件，校验就落空了。
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


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


def _enforce_env_secrets_or_exit(_original_print):
    """启动前拒绝「照抄模板」的占位密钥；不合格直接以退出码 2 终止。

    为什么需要这一层（`start.bat` / `start.sh` 已经跑过 `utils.env_bootstrap`）：
    那两个脚本会检查 `utils.env_bootstrap` 的退出码并中止，但**直接 `python app.py`
    会整条绕过它** —— 而文档、容器编排、以及不少运维习惯正是这么起的。
    这里补在 Web 模式与 Agent 模式**共用的唯一咽喉** `run_runtime_entry` 上，
    让「看起来配好了、其实用的是公开常量」的部署无法启动。

    失败要放在 `run_runtime_entry` 的 `try` **之外**：该函数里有
    `except SystemExit as exc:` 分支，会把异常吞掉并让进程以 0 退出 —— 那就成了
    「拒绝启动」却报成功。

    校验失败抛 `SystemExit` 而非返回布尔值，是为了让任何调用方都无法忽略它。
    """
    try:
        from utils.env_bootstrap import (
            check_env_file_secrets,
            format_insecure_secret_guidance,
            is_testing_mode,
        )
    except Exception as exc:  # pragma: no cover - 模块缺失不应阻断启动
        _original_print(f"[WARN] .env 密钥校验不可用，已跳过: {exc}")
        return

    if is_testing_mode():
        return

    env_path = _REPO_ROOT / ".env"
    issues = check_env_file_secrets(env_path)
    if not issues:
        return

    _original_print(
        format_insecure_secret_guidance(issues, env_path),
        file=sys.stderr,
    )
    raise SystemExit(2)


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

    # 必须早于 clear_log_file 那一步（它会清空日志文件，失败原因会被抹掉），
    # 也必须早于 signal/initialize_app —— 密钥不合格时不该碰任何运行时状态。
    _enforce_env_secrets_or_exit(_original_print)

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
