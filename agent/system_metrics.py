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


def collect_agent_metrics(base_dir: str):
    cpu_cores = os.cpu_count() or 1
    cpu_usage_percent = None
    memory_total_bytes = None
    memory_available_bytes = None
    disk_free_bytes = None

    if psutil is not None:
        try:
            cpu_usage_percent = float(psutil.cpu_percent(interval=None))
        except Exception:
            cpu_usage_percent = None

        try:
            vm = psutil.virtual_memory()
            memory_total_bytes = int(vm.total)
            memory_available_bytes = int(vm.available)
        except Exception:
            memory_total_bytes = None
            memory_available_bytes = None

    try:
        disk_target = os.path.abspath(base_dir or ".")
        disk_free_bytes = int(shutil.disk_usage(disk_target).free)
    except Exception:
        disk_free_bytes = None

    return {
        "cpu_cores": int(cpu_cores),
        "cpu_usage_percent": cpu_usage_percent,
        "memory_total_bytes": memory_total_bytes,
        "memory_available_bytes": memory_available_bytes,
        "disk_free_bytes": disk_free_bytes,
        "os_name": platform.system() or "",
        "os_version": platform.version() or "",
        "os_platform": platform.platform() or "",
    }
