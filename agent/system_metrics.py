#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 运行节点资源信息采集。"""

from __future__ import annotations

import os
import platform
import shutil

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

_PROC_HANDLE = None
_PROC_CPU_PRIMED = False


def _get_proc_handle():
    global _PROC_HANDLE
    if psutil is None:
        return None
    if _PROC_HANDLE is None:
        try:
            _PROC_HANDLE = psutil.Process(os.getpid())
        except Exception:
            _PROC_HANDLE = None
    return _PROC_HANDLE


def _collect_disk_free_bytes(base_dir: str):
    candidate = os.path.abspath(base_dir or ".")
    visited = set()
    targets = []

    current = candidate
    while current and current not in visited:
        visited.add(current)
        if os.path.exists(current):
            targets.append(current)
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent

    cwd_target = os.path.abspath(".")
    if cwd_target not in targets:
        targets.append(cwd_target)

    for target in targets:
        try:
            return int(shutil.disk_usage(target).free)
        except Exception:
            continue
    return None


def collect_agent_metrics(base_dir: str):
    global _PROC_CPU_PRIMED
    cpu_cores = os.cpu_count() or 1
    cpu_usage_percent = None
    agent_cpu_usage_percent = None
    memory_total_bytes = None
    memory_available_bytes = None
    agent_memory_rss_bytes = None
    disk_free_bytes = None

    if psutil is not None:
        try:
            cpu_usage_percent = float(psutil.cpu_percent(interval=None))
        except Exception:
            cpu_usage_percent = None

        proc = _get_proc_handle()
        if proc is not None:
            try:
                # First sampling with interval=None is undefined; prime for later cycles.
                raw_proc_cpu = float(proc.cpu_percent(interval=None))
                if not _PROC_CPU_PRIMED:
                    _PROC_CPU_PRIMED = True
                    agent_cpu_usage_percent = 0.0
                else:
                    cores = float(cpu_cores or 1)
                    agent_cpu_usage_percent = max(0.0, min(100.0, raw_proc_cpu / cores))
            except Exception:
                agent_cpu_usage_percent = None

        try:
            vm = psutil.virtual_memory()
            memory_total_bytes = int(vm.total)
            memory_available_bytes = int(vm.available)
        except Exception:
            memory_total_bytes = None
            memory_available_bytes = None

        if proc is not None:
            try:
                agent_memory_rss_bytes = int(proc.memory_info().rss)
            except Exception:
                agent_memory_rss_bytes = None

    disk_free_bytes = _collect_disk_free_bytes(base_dir)

    return {
        "cpu_cores": int(cpu_cores),
        "cpu_usage_percent": cpu_usage_percent,
        "agent_cpu_usage_percent": agent_cpu_usage_percent,
        "memory_total_bytes": memory_total_bytes,
        "memory_available_bytes": memory_available_bytes,
        "agent_memory_rss_bytes": agent_memory_rss_bytes,
        "disk_free_bytes": disk_free_bytes,
        "os_name": platform.system() or "",
        "os_version": platform.version() or "",
        "os_platform": platform.platform() or "",
    }
