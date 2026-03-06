#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Application bootstrap lifecycle manager."""

from __future__ import annotations

import threading


class AppBootstrapManager:
    """Encapsulate app initialize/cleanup lifecycle steps."""

    def __init__(
        self,
        *,
        app,
        log_print,
        enable_local_worker: bool,
        create_tables_func,
        init_auth_default_data_func,
        start_background_task_worker_func,
        stop_background_task_worker_func,
        clear_version_mismatch_cache_func,
        cleanup_pending_deletions_func,
        cleanup_git_processes_func,
    ):
        self._app = app
        self._log_print = log_print
        self._enable_local_worker = bool(enable_local_worker)
        self._create_tables_func = create_tables_func
        self._init_auth_default_data_func = init_auth_default_data_func
        self._start_background_task_worker_func = start_background_task_worker_func
        self._stop_background_task_worker_func = stop_background_task_worker_func
        self._clear_version_mismatch_cache_func = clear_version_mismatch_cache_func
        self._cleanup_pending_deletions_func = cleanup_pending_deletions_func
        self._cleanup_git_processes_func = cleanup_git_processes_func
        self._initialized = False

    def initialize_app(self):
        """Initialize database, auth defaults, workers and cache cleanup."""
        if self._initialized:
            self._log_print("应用已经初始化过，跳过重复初始化", "APP")
            return

        try:
            self._create_tables_func()
            self._log_print("数据库表创建完成", "APP")
            try:
                self._init_auth_default_data_func()
                self._log_print("Auth: 默认数据初始化完成", "AUTH")
            except Exception as exc:
                self._log_print(f"Auth 默认数据初始化跳过: {exc}", "AUTH")

            if self._enable_local_worker:
                with self._app.app_context():
                    self._start_background_task_worker_func()

                def async_cache_cleanup():
                    try:
                        with self._app.app_context():
                            self._clear_version_mismatch_cache_func()
                    except Exception as exc:
                        self._log_print(f"异步缓存清理失败: {exc}", "APP", force=True)

                cleanup_thread = threading.Thread(target=async_cache_cleanup, daemon=True)
                cleanup_thread.start()
                self._log_print("异步缓存清理已启动", "APP")
                self._cleanup_pending_deletions_func()
            else:
                self._log_print("当前为 platform/agent 模式，跳过本地后台任务与缓存清理线程", "APP", force=True)

            self._log_print("应用初始化完成", "APP")
            self._initialized = True
        except Exception as exc:
            self._log_print(f"应用初始化失败: {exc}", "APP", force=True)
            raise

    def cleanup_app(self):
        """Cleanup worker threads and git child processes on exit."""
        try:
            if self._enable_local_worker:
                self._stop_background_task_worker_func()
            self._cleanup_git_processes_func()
        except Exception as exc:
            self._log_print(f"应用清理过程中出现错误: {exc}", "APP", force=True)

