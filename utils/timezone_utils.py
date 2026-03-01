#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时区处理工具 - 统一处理UTC时间转换为北京时间
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, Union

# 北京时区 (UTC+8)
BEIJING_TZ = timezone(timedelta(hours=8))

def utc_to_beijing(utc_time: Optional[datetime]) -> Optional[datetime]:
    """
    将UTC时间转换为北京时间
    
    Args:
        utc_time: UTC时间，可以是带时区信息的datetime或naive datetime
        
    Returns:
        北京时间的datetime对象，如果输入为None则返回None
    """
    if utc_time is None:
        return None
    
    # 如果是naive datetime，假设为UTC时间
    if utc_time.tzinfo is None:
        utc_time = utc_time.replace(tzinfo=timezone.utc)
    
    # 转换为北京时间
    beijing_time = utc_time.astimezone(BEIJING_TZ)
    return beijing_time

def beijing_to_utc(beijing_time: Optional[datetime]) -> Optional[datetime]:
    """
    将北京时间转换为UTC时间
    
    Args:
        beijing_time: 北京时间，可以是带时区信息的datetime或naive datetime
        
    Returns:
        UTC时间的datetime对象，如果输入为None则返回None
    """
    if beijing_time is None:
        return None
    
    # 如果是naive datetime，假设为北京时间
    if beijing_time.tzinfo is None:
        beijing_time = beijing_time.replace(tzinfo=BEIJING_TZ)
    
    # 转换为UTC时间
    utc_time = beijing_time.astimezone(timezone.utc)
    return utc_time

def format_beijing_time(utc_time: Optional[datetime], format_str: str = '%Y/%m/%d %H:%M:%S') -> str:
    """
    将UTC时间格式化为北京时间字符串
    
    Args:
        utc_time: UTC时间
        format_str: 格式化字符串，默认为 '%Y/%m/%d %H:%M:%S'
        
    Returns:
        格式化后的北京时间字符串，如果输入为None则返回'未知时间'
    """
    if utc_time is None:
        return '未知时间'
    
    beijing_time = utc_to_beijing(utc_time)
    if beijing_time is None:
        return '未知时间'
    
    return beijing_time.strftime(format_str)

def now_beijing() -> datetime:
    """
    获取当前北京时间
    
    Returns:
        当前北京时间的datetime对象
    """
    return datetime.now(BEIJING_TZ)

def now_utc() -> datetime:
    """
    获取当前UTC时间
    
    Returns:
        当前UTC时间的datetime对象
    """
    return datetime.now(timezone.utc)

def parse_time_with_timezone(time_str: str, assume_timezone: str = 'utc') -> Optional[datetime]:
    """
    解析时间字符串，支持多种格式
    
    Args:
        time_str: 时间字符串
        assume_timezone: 如果时间字符串没有时区信息，假设的时区 ('utc' 或 'beijing')
        
    Returns:
        解析后的datetime对象，如果解析失败则返回None
    """
    if not time_str:
        return None
    
    try:
        # 尝试解析ISO格式
        if 'T' in time_str:
            # 处理各种ISO格式
            time_str = time_str.replace('Z', '+00:00')
            dt = datetime.fromisoformat(time_str)
        else:
            # 尝试解析其他常见格式
            formats = [
                '%Y-%m-%d %H:%M:%S',
                '%Y/%m/%d %H:%M:%S',
                '%Y-%m-%d',
                '%Y/%m/%d'
            ]
            
            dt = None
            for fmt in formats:
                try:
                    dt = datetime.strptime(time_str, fmt)
                    break
                except ValueError:
                    continue
            
            if dt is None:
                return None
        
        # 如果没有时区信息，根据assume_timezone参数添加时区
        if dt.tzinfo is None:
            if assume_timezone.lower() == 'beijing':
                dt = dt.replace(tzinfo=BEIJING_TZ)
            else:  # 默认为UTC
                dt = dt.replace(tzinfo=timezone.utc)
        
        return dt
        
    except Exception:
        return None

def get_timezone_info() -> dict:
    """
    获取时区信息
    
    Returns:
        包含时区信息的字典
    """
    return {
        'beijing_tz': BEIJING_TZ,
        'utc_tz': timezone.utc,
        'beijing_offset': '+08:00',
        'utc_offset': '+00:00'
    }
