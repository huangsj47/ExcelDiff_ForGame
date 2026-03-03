#!/usr/bin/env python3
"""Extract weekly version logic from app.py into services/weekly_version_logic.py"""

import re

# Read app.py
with open("app.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

# Lines 586-1957 (0-indexed: 585-1956)
START = 585  # 0-indexed, line 586
END = 1957   # 0-indexed exclusive, line 1957

block_lines = lines[START:END]
block_code = "".join(block_lines)

# Build the new module
header = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周版本（Weekly Version）业务逻辑
====================================
从 app.py 中拆分出的周版本相关路由处理函数和辅助方法。

函数列表:
  - weekly_version_config(project_id)
  - weekly_version_config_api(project_id)
  - weekly_version_config_detail_api(project_id, config_id)
  - weekly_version_list(project_id)
  - merged_project_view(project_id)
  - weekly_version_diff(config_id)
  - weekly_version_config_info_api(config_id)
  - weekly_version_files_api(config_id)
  - weekly_version_file_diff_api(config_id)
  - weekly_version_file_full_diff(config_id)
  - weekly_version_file_full_diff_data(config_id)
  - generate_weekly_git_diff_html(...)
  - _merge_segmented_excel_diff_payload(...)
  - _extract_excel_diff_from_payload(...)
  - _load_weekly_excel_diff_from_cache(...)
  - generate_weekly_excel_merged_diff_html(...)
  - get_status_text(status)
  - get_status_badge_class(status)
  - process_weekly_version_sync(config_id)
  - generate_weekly_merged_diff(config, file_path, commits)
  - process_weekly_excel_cache(config_id, file_path)
  - create_weekly_excel_cache_task(config_id, file_path)
  - get_real_base_commit_from_vcs(config, file_path)
"""

import json
import math
import os
import traceback
from datetime import datetime, timedelta, timezone

from flask import render_template, request, jsonify, url_for

from models import (
    db,
    Project,
    Repository,
    Commit,
    BackgroundTask,
    WeeklyVersionConfig,
    WeeklyVersionDiffCache,
    WeeklyVersionExcelCache,
    DiffCache,
    ExcelHtmlCache,
)
from services.diff_service import DiffService
from utils.logger import log_print

# ---------------------------------------------------------------------------
#  运行时依赖 — 由 configure_weekly_version_logic() 注入
# ---------------------------------------------------------------------------
_excel_cache_service = None
_weekly_excel_cache_service = None
_excel_html_cache_service = None

# 延迟导入的函数引用
_create_weekly_sync_task = None
_get_unified_diff_data = None
_get_git_service = None
_get_svn_service = None
_get_file_content_from_git = None
_get_file_content_from_svn = None
_generate_merged_diff_data = None


def configure_weekly_version_logic(
    *,
    excel_cache_service,
    weekly_excel_cache_service,
    excel_html_cache_service,
    create_weekly_sync_task_func,
    get_unified_diff_data_func,
    get_git_service_func,
    get_svn_service_func,
    get_file_content_from_git_func,
    get_file_content_from_svn_func,
    generate_merged_diff_data_func,
):
    """注入运行时依赖。由 app.py 在初始化阶段调用。"""
    global _excel_cache_service, _weekly_excel_cache_service, _excel_html_cache_service
    global _create_weekly_sync_task, _get_unified_diff_data
    global _get_git_service, _get_svn_service
    global _get_file_content_from_git, _get_file_content_from_svn
    global _generate_merged_diff_data

    _excel_cache_service = excel_cache_service
    _weekly_excel_cache_service = weekly_excel_cache_service
    _excel_html_cache_service = excel_html_cache_service
    _create_weekly_sync_task = create_weekly_sync_task_func
    _get_unified_diff_data = get_unified_diff_data_func
    _get_git_service = get_git_service_func
    _get_svn_service = get_svn_service_func
    _get_file_content_from_git = get_file_content_from_git_func
    _get_file_content_from_svn = get_file_content_from_svn_func
    _generate_merged_diff_data = generate_merged_diff_data_func


# ---------------------------------------------------------------------------
#  以下为从 app.py 拆分出来的周版本业务逻辑
# ---------------------------------------------------------------------------

'''

# Replace direct references to injected dependencies in the block
replacements = [
    # Service objects
    ("excel_cache_service.", "_excel_cache_service."),
    ("weekly_excel_cache_service.", "_weekly_excel_cache_service."),
    ("excel_html_cache_service.", "_excel_html_cache_service."),
    # Functions (be careful with word boundaries)
]

modified_block = block_code

# Rename service references - but only standalone identifiers, not inside strings
# For service objects, only replace when they are used as objects (not in strings or comments)
modified_block = re.sub(r'\bexcel_cache_service\.', '_excel_cache_service.', modified_block)
modified_block = re.sub(r'\bweekly_excel_cache_service\.', '_weekly_excel_cache_service.', modified_block)
modified_block = re.sub(r'\bexcel_html_cache_service\.', '_excel_html_cache_service.', modified_block)

# Function calls - replace with module-level references
modified_block = re.sub(r'\bcreate_weekly_sync_task\(', '_create_weekly_sync_task(', modified_block)
modified_block = re.sub(r'\bget_unified_diff_data\(', '_get_unified_diff_data(', modified_block)
modified_block = re.sub(r'\bget_git_service\(', '_get_git_service(', modified_block)
modified_block = re.sub(r'\bget_svn_service\(', '_get_svn_service(', modified_block)
modified_block = re.sub(r'\bget_file_content_from_git\(', '_get_file_content_from_git(', modified_block)
modified_block = re.sub(r'\bget_file_content_from_svn\(', '_get_file_content_from_svn(', modified_block)
modified_block = re.sub(r'\bgenerate_merged_diff_data\(', '_generate_merged_diff_data(', modified_block)

# Remove duplicate imports that are already in the header
# Remove "from datetime import datetime, timezone" style inline imports
modified_block = re.sub(r'\n\s*from datetime import datetime, timezone\n', '\n', modified_block)
modified_block = re.sub(r'\n\s*from datetime import datetime\n', '\n', modified_block)
modified_block = re.sub(r'\n\s*from utils\.timezone_utils import now_beijing\n', '\n', modified_block)
# But we need now_beijing, add it to header
header = header.replace(
    "from utils.logger import log_print",
    "from utils.logger import log_print\nfrom utils.timezone_utils import now_beijing"
)

# Write the new module
with open("services/weekly_version_logic.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(modified_block)

print(f"Created services/weekly_version_logic.py with {len(modified_block.splitlines())} lines of logic code")

# Now create the replacement block for app.py
# List of function names to re-export
func_names = []
for line in block_lines:
    m = re.match(r'^def (\w+)\(', line)
    if m:
        func_names.append(m.group(1))

# Remove internal/helper functions that start with _
public_funcs = [f for f in func_names if not f.startswith('_')]
private_funcs = [f for f in func_names if f.startswith('_')]

replacement = '''# ---------------------------------------------------------------------------
#  周版本业务逻辑 — 已拆分至 services/weekly_version_logic.py
# ---------------------------------------------------------------------------
from services.weekly_version_logic import (
    configure_weekly_version_logic,
'''
for fn in public_funcs:
    replacement += f"    {fn},\n"
for fn in private_funcs:
    replacement += f"    {fn},\n"
replacement += ")\n"

# Replace lines in app.py
new_lines = lines[:START] + [replacement] + lines[END:]

with open("app.py", "w", encoding="utf-8") as f:
    f.writelines(new_lines)

new_count = len(new_lines)
print(f"Updated app.py: {len(lines)} -> {new_count} lines (removed {len(lines) - new_count} lines)")
print(f"\nPublic functions exported: {public_funcs}")
print(f"Private functions exported: {private_funcs}")
print(f"\nDon't forget to add configure_weekly_version_logic() call in app.py!")
