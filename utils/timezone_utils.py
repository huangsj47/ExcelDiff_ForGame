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

# ---------------------------------------------------------------------------
#  naive 墙钟之间的换算（数据库列专用）
#
#  【背景：本平台的 DB 时间列有两套「墙钟」，必须显式换算才能比较】
#
#  库里的 DateTime 列**全部是 naive**（SQLite 方言在绑定参数时会静默丢弃 tzinfo，
#  即使模型写了 timezone=True 也一样）。问题在于同一个库里存在两种墙钟：
#
#    1. naive-UTC —— ORM 默认值与 VCS 解析走的都是这条：
#         datetime.now(timezone.utc)                     → 存 UTC 墙钟
#         datetime.fromtimestamp(ts, tz=timezone.utc)     → 存 UTC 墙钟（Commit.commit_time）
#
#    2. 北京墙钟 —— 用户在浏览器 <input type="datetime-local"> 里填的、以及纯文本
#       日期字段里填的，原样 fromisoformat 入库：
#         WeeklyVersionConfig.start_time / end_time
#         Repository.start_date
#
#  直接拿第 2 种和第 1 种比大小，结果会**整体偏移 8 小时**，而且不报错。
#  实测后果（修复前）：周版本窗口「3/2 00:00 ~ 3/9 00:00」实际生效为
#  「3/2 08:00 ~ 3/9 08:00」（真实北京时间），于是每周窗口**前 8 小时的提交
#  被静默丢弃**，基准版本也被选错，窗口首日变更整体不可见。
#
#  所以：凡是「用户填的墙钟」要与「提交时间列」比较，必须先过下面的函数。
#  这里刻意**不改变存储格式**（模板有 8 处在用这些值，改成 UTC 会牵动整条展示链），
#  只在比较边界统一换算。
# ---------------------------------------------------------------------------

def beijing_wallclock_to_utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    """把「用户填写的北京墙钟」转成「库里统一的 naive-UTC 墙钟」。

    用于：拿 WeeklyVersionConfig.start_time/end_time（北京墙钟）或
    Repository.start_date（北京墙钟）去和 Commit.commit_time（naive-UTC）比较之前。

    naive 入参**按北京时间解释**（这正是它的来源），aware 入参按其自身时区处理。
    返回 naive（去掉 tzinfo），以便直接与库里的列比较。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(timezone.utc).replace(tzinfo=None)

def utc_naive_to_beijing_wallclock(value: Optional[datetime]) -> Optional[datetime]:
    """`beijing_wallclock_to_utc_naive` 的反向：naive-UTC 墙钟 → naive 北京墙钟。

    用于：需要拿「当前时间」去和**北京墙钟**的配置值比较时
    （例如判断某个周版本配置是否处于 active 窗口内）。

    不要用 `datetime.now()`：那取的是**宿主机**本地时间，在 UTC 容器里会差 8 小时，
    而在开发机（UTC+8）上「碰巧正确」—— 属于最难发现的一类缺陷。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)

def beijing_window_to_utc_naive(start_time: Optional[datetime],
                               end_time: Optional[datetime]):
    """一次换算一个「北京墙钟」窗口的两端，返回 (start_utc, end_utc)。

    两个都返回 naive-UTC，可直接用于 `Commit.commit_time >= start_utc` 这类比较。
    任一为 None 时对应位置返回 None —— 调用方自行决定「没有窗口」的语义。
    """
    return (
        beijing_wallclock_to_utc_naive(start_time),
        beijing_wallclock_to_utc_naive(end_time),
    )

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
