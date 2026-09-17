#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Utilities for diff payload cleanup and Excel value formatting."""

import math


def clean_json_data(data):
    """Recursively replace NaN/Inf values with None for JSON safety."""
    if isinstance(data, dict):
        return {key: clean_json_data(value) for key, value in data.items()}
    if isinstance(data, list):
        return [clean_json_data(item) for item in data]
    if isinstance(data, float):
        if math.isnan(data) or math.isinf(data):
            return None
        return data
    return data


def validate_excel_diff_data(diff_data):
    """Validate basic Excel diff payload structure."""
    if not diff_data:
        return False, "diff_data为空"

    if not isinstance(diff_data, dict):
        return False, f"diff_data不是字典类型: {type(diff_data)}"

    required_fields = ["type", "sheets"]
    for field in required_fields:
        if field not in diff_data:
            return False, f"缺少必需字段: {field}"

    if diff_data.get("type") != "excel":
        return False, f"type字段不正确: {diff_data.get('type')}"

    sheets = diff_data.get("sheets")
    if not isinstance(sheets, dict):
        return False, f"sheets字段不是字典类型: {type(sheets)}"

    valid_sheets_count = 0
    total_rows = 0
    header_changes = 0
    for _sheet_name, sheet_data in sheets.items():
        if not isinstance(sheet_data, dict):
            continue
        rows = sheet_data.get("rows", [])
        if isinstance(rows, list) and len(rows) > 0:
            valid_sheets_count += 1
            total_rows += len(rows)
        # 列级变更（列名改了 / 加了列 / 删了列）也是内容。
        # 只按 rows 判的话，「一次提交只改了列名」这种载荷会被判成
        # 「所有工作表都没有差异数据」，页面每次访问都要重算一遍。
        sheet_header_changes = sheet_data.get("header_changes")
        if isinstance(sheet_header_changes, list):
            header_changes += len(sheet_header_changes)

    if total_rows == 0 and header_changes == 0:
        return False, f"所有工作表都没有差异数据 (共{len(sheets)}个工作表)"
    if total_rows == 0:
        return True, f"验证通过: {header_changes}处列变更, 0行差异"
    return True, f"验证通过: {valid_sheets_count}个有效工作表, 共{total_rows}行差异"


def safe_json_serialize(obj):
    """Recursively sanitize data before JSON serialization."""

    def _clean_value(value):
        if isinstance(value, dict):
            return {key: _clean_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_clean_value(item) for item in value]
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None
            return value
        return value

    return _clean_value(obj)


def get_excel_column_letter(index):
    """Convert column index to Excel column letter (0 -> A, 27 -> AB)."""
    result = ""
    while index >= 0:
        result = chr(65 + (index % 26)) + result
        index = index // 26 - 1
    return result


def format_cell_value(value):
    """把单元格值渲染成展示文本：**只**把真正的空值显示为空。

    这是 `_normalize_value`（services/diff_service.py）在展示层的对应物，
    两者口径必须一致 —— 见下方「为什么」。

    ⚠️ 历史行为（已修，DIFF_LOGIC_VERSION 1.9.0）：这里曾把**文本**
    `'null'` / `'None'` / `'nan'` / `'undefined'` 也渲染成空串，并对结果 `strip()`。
    两个后果：

    1. 配表里真实的取值 `null`（表示「无掉落 / 无引用」很常见）在界面上**显示为空**，
       与真正的空单元格长得一模一样；
    2. 更严重的是**与比较层不一致**：比较层现在认为 `'null'` ≠ 空（会报变更），
       若展示层把两边都渲染成空，审核者会看到一行「空 → 空」的变更行，
       **完全无法判断到底改了什么** —— 这比不报变更更难排查。

    因此本函数的契约是：**表里怎么写就怎么显示**，不做任何改写、不 strip。
    首尾空格与连续空格由 CSS 的 `white-space: pre-wrap` 原样呈现
    （见 static/css/excel-diff-new.css 的 `.excel-cell`）。
    """
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value)
