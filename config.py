#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应用配置文件
"""

import os
import sys

# 设置控制台输出编码为UTF-8
#
# ⚠️ 只在**进程自己的原始流**上替换（`sys.stdout is sys.__stdout__`）。
# 本模块在 import 时无条件把 `sys.stdout` 换成自己包的 `TextIOWrapper`，会**丢掉
# 宿主已经安装好的流**：pytest 的捕获对象、IDE / 调试器的输出面板、gunicorn 等
# WSGI 宿主的日志流都会被替换掉，宿主的输出从此写进一个悬空对象。
#
# 实测（修复前）：在一个测试里 `from config import DATABASE_CONFIG`（此前进程尚未
# 导入过 config）之后，pytest 的收尾汇总**整段消失**——`python -m pytest` 只打印
# 11 个点就退出、退出码 1，看不到 "N passed / M failed"，CI 拿不到任何结果。
# 这类失败极难归因：坏的是「谁能打印」，而不是任何一条断言。
#
# 用 `is sys.__stdout__` 而不是 `isinstance(..., io.TextIOWrapper)` 判断：
# pytest 在 `--capture=fd` 下也会把 `sys.stdout` 换成 `TextIOWrapper`（指向临时
# 文件），isinstance 分辨不出来，而 `is sys.__stdout__` 能准确区分
# 「还是进程原本的标准输出」与「宿主已经换过了」。
if sys.platform == 'win32':
    import io

    def _wrap_utf8(stream, fallback):
        """把控制台流包成 UTF-8；不是进程原始流、或没有 buffer 时原样返回。"""
        if stream is not fallback:
            return stream
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            return stream
        return io.TextIOWrapper(buffer, encoding='utf-8', errors='replace')

    sys.stdout = _wrap_utf8(sys.stdout, sys.__stdout__)
    sys.stderr = _wrap_utf8(sys.stderr, sys.__stderr__)
    # 设置控制台代码页为UTF-8
    os.system('chcp 65001 >nul 2>&1')

# 设置窗口标题
from os import system
system("title SEOTool - diff-confirmation-platform")

# Diff逻辑版本号 - 当diff算法或逻辑发生变化时需要更新此版本号
#
# ⚠️ 本常量在仓库里有**两处**字面量：这里与 app.py。驱动缓存失效的是 app.py 那一份
# （它被用来构造 DiffCache / ExcelHtmlCache / WeeklyVersionExcelCache 的 diff_version，
# 并经运行时注册表供缓存管理页展示）。本文件这一份由 tasks/cache_cleanup.py 读取，
# 用来**删除版本不匹配的缓存**。两者必须一致 —— 只改一处会出现
# 「版本号升了但缓存没清」或「当前版本的缓存被当过期数据删掉」这类静默不一致，且不会报错。
# 一致性由 tests/test_diff_logic_version_single_source.py 锁定。
#
# 1.9.0：Excel/CSV 改为按文本原样读取（dtype=str + keep_default_na=False），并修掉
#        _normalize_value 把文本 null/none/nan/<na> 当空值、以及 strip 掉首尾空格的问题。
#        影响：升级后 diff 数量会比以前多（这是预期，见 README）。
# 1.10.0：行过滤（_has_valid_data / _filter_nan_rows）与 _normalize_value 统一口径 ——
#        整行都是 null/None/空白串的行不再被当成空行丢掉（这些行的改动原先全部漏报）；
#        .tsv 按制表符读取，不再必然报「Excel file format cannot be determined」。
DIFF_LOGIC_VERSION = "1.13.0"

# 日志级别配置
LOG_LEVEL = {
    'APP_VERBOSE': True,      # 应用主要日志
    'GIT_VERBOSE': True,     # Git操作详细日志
    'CACHE_VERBOSE': True,   # 缓存操作详细日志
    'DIFF_VERBOSE': True,    # Diff计算详细日志
    'SVN_VERBOSE': True,     # SVN操作详细日志
    'EXCEL_VERBOSE': True,   # Excel处理详细日志
    'LOGGING_VERBOSE': True   # 通用日志输出（重载print函数使用）
}

from utils.db_config import DEFAULT_SQLITE_PATH as _DEFAULT_SQLITE_PATH  # noqa: E402


# Flask配置
class Config:
    # ⚠️ 本类**没有**被 `app.config.from_object(Config)` 使用（全仓无该调用）。
    # 运行期真正生效的两个值来自：
    #   * SECRET_KEY            → bootstrap/app_factory.py::build_runtime_settings()
    #                             读 FLASK_SECRET_KEY / SECRET_KEY，缺省再生成运行期随机值
    #   * SQLALCHEMY_DATABASE_URI → utils/db_config.py::apply_database_settings()
    #                             读 DATABASE_URL / DB_* 
    # 这里原先写的是字面量占位值 'your-secret-key-here' 与一个 CWD 相关的
    # sqlite 路径。它们现在都改成**引用同一份来源**，而不是各留一份会在运行时
    # 被覆盖的假默认值 —— 那种「看起来配了、其实从来没生效」的值，会让人在排查
    # 会话丢失或数据库路径不对时白跑很久。
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY") or None
    SQLALCHEMY_DATABASE_URI = f'sqlite:///{_DEFAULT_SQLITE_PATH}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # 服务器配置
    HOST = '0.0.0.0'
    PORT = 8002
    DEBUG = False
    USE_RELOADER = False
    THREADED = True

# 数据库配置
#
# 这里只是**静态默认值**，运行时会被 utils/db_config.py::apply_database_settings()
# 按 DATABASE_URL / DB_BACKEND 覆盖（优先级：DATABASE_URL > DB_* > 本处）。
# 排查配置问题时以 app.config["SQLALCHEMY_DATABASE_URI"] 为准。
#
# 路径取自 utils/db_config.py 的 DEFAULT_SQLITE_PATH，不再自己 abspath ——
# 那份默认值锚定在**仓库根目录**，而 `os.path.abspath("instance/...")` 是相对
# 当前工作目录的：换个目录启动就会指向另一个库文件。详见 utils/runtime_paths.py。

DATABASE_CONFIG = {
    'db_path': _DEFAULT_SQLITE_PATH,
    'instance_dir': os.path.dirname(_DEFAULT_SQLITE_PATH),
}

# 后台任务配置
BACKGROUND_TASK_CONFIG = {
    'max_workers': 4,
    'queue_timeout': 30,
    'retry_limit': 3,
    'cleanup_interval': 3600,  # 1小时
}

# 缓存配置
CACHE_CONFIG = {
    'long_processing_threshold': 10.0,  # 秒
    'long_processing_expire_days': 90,  # 天
    'max_cache_entries': 1000,
    'cleanup_batch_size': 100,
}

# 定时任务配置
SCHEDULE_CONFIG = {
    'cleanup_time': "04:00",  # 每天4点清理
    'weekly_sync_interval': 2,  # 每2分钟检查周版本同步
    'repo_sync_interval': 10,  # 每10分钟自动同步仓库新提交
}
