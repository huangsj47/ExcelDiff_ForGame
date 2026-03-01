#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应用工具函数
"""

import math
import json
from datetime import datetime, timezone


def clean_json_data(data):
    """
    清理数据中的不可JSON序列化的值（如nan, inf等）
    
    Args:
        data: 待清理的数据
        
    Returns:
        清理后的数据
    """
    if isinstance(data, dict):
        return {k: clean_json_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [clean_json_data(item) for item in data]
    elif isinstance(data, float):
        if math.isnan(data) or math.isinf(data):
            return None
        return data
    else:
        return data


def validate_excel_diff_data(diff_data):
    """
    验证Excel差异数据的完整性
    
    Args:
        diff_data: 待验证的差异数据
        
    Returns:
        tuple: (is_valid, message)
    """
    if not diff_data:
        return False, "diff_data为空"
    
    try:
        # 尝试解析JSON
        if isinstance(diff_data, str):
            data = json.loads(diff_data)
        else:
            data = diff_data
        
        # 检查必需的字段
        required_fields = ['type', 'file_path']
        for field in required_fields:
            if field not in data:
                return False, f"缺少必需字段: {field}"
        
        return True, "数据有效"
    
    except json.JSONDecodeError as e:
        return False, f"JSON解析错误: {str(e)}"
    except Exception as e:
        return False, f"验证错误: {str(e)}"


def get_excel_column_letter(index):
    """将数字索引转换为Excel列字母 (0->A, 1->B, ..., 25->Z, 26->AA)"""
    result = ""
    while index >= 0:
        result = chr(65 + (index % 26)) + result
        index = index // 26 - 1
        if index < 0:
            break
    return result


def format_file_size(size_bytes):
    """格式化文件大小"""
    if size_bytes == 0:
        return "0 B"
    
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_names[i]}"


def format_duration(seconds):
    """格式化持续时间"""
    if seconds < 60:
        return f"{seconds:.1f}秒"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}分钟"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}小时"


def safe_json_loads(json_str, default=None):
    """安全的JSON解析"""
    if not json_str:
        return default
    
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return default


def safe_json_dumps(data, default=None):
    """安全的JSON序列化"""
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return default


def get_client_ip(request):
    """获取客户端IP地址"""
    if request.environ.get('HTTP_X_FORWARDED_FOR'):
        return request.environ['HTTP_X_FORWARDED_FOR'].split(',')[0].strip()
    elif request.environ.get('HTTP_X_REAL_IP'):
        return request.environ['HTTP_X_REAL_IP']
    else:
        return request.environ.get('REMOTE_ADDR', 'unknown')
