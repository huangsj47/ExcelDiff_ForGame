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
    for _sheet_name, sheet_data in sheets.items():
        if not isinstance(sheet_data, dict):
            continue
        rows = sheet_data.get("rows", [])
        if isinstance(rows, list) and len(rows) > 0:
            valid_sheets_count += 1
            total_rows += len(rows)

    if total_rows == 0:
        return False, f"所有工作表都没有差异数据 (共{len(sheets)}个工作表)"
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
    """Normalize cell values for HTML rendering."""
    if value is None or value == "null" or value == "None":
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""

    str_value = str(value).strip()
    if str_value.lower() in ["nan", "null", "undefined", ""]:
        return ""
    return str_value
