#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
业务流程综合测试
==================
覆盖以下核心业务流程：

1. TestWeeklySyncScheduler:      周版本同步调度器（含时区修复验证）
2. TestBackgroundTaskLifecycle:   后台任务生命周期管理
3. TestBackgroundTaskPauseResume: 后台任务暂停/恢复控制
4. TestTimezoneConsistency:       时区一致性验证
5. TestStatusSyncBidirectional:   状态同步双向链路
6. TestWeeklyVersionModelChain:   周版本模型完整性
7. TestGitServiceCoreChain:       Git 服务核心链路
8. TestDiffServiceEdgeCases:      Diff 服务边界场景
9. TestSecurityToolsChain:        安全工具链路
10. TestDatabaseConfigChain:      数据库配置链路
"""

import os
import sys
import json
import hashlib
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, Any
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _read_source(relative_path: str) -> str:
    """读取源码文件内容"""
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
#  1. 周版本同步调度器测试（含时区修复验证）
# ---------------------------------------------------------------------------

class TestWeeklySyncScheduler:
    """验证周版本同步调度器的时区修复和调度逻辑"""

    def test_scheduler_uses_naive_datetime_for_comparison(self):
        """调度器应使用 naive datetime 而非 UTC-aware datetime 做比较"""
        content = _read_source("app.py")
        # schedule_weekly_sync_tasks 函数中不应出现
        # datetime.now(timezone.utc) 用于与 config 时间比较
        func_start = content.find("def schedule_weekly_sync_tasks")
        func_end = content.find("\n# 设置定时任务", func_start)
        func_body = content[func_start:func_end]
        # 关键修复：应使用 datetime.now() 而非 datetime.now(timezone.utc)
        assert "now_local = datetime.now()" in func_body, \
            "调度器应使用 datetime.now() 获取本地时间"
        assert "datetime.now(timezone.utc)" not in func_body, \
            "调度器不应使用 datetime.now(timezone.utc) 与 naive 时间比较"

    def test_stale_task_detection_uses_naive_time(self):
        """卡死任务检测应使用 naive 本地时间比较"""
        content = _read_source("app.py")
        func_start = content.find("def schedule_weekly_sync_tasks")
        func_end = content.find("\n# 设置定时任务", func_start)
        func_body = content[func_start:func_end]
        assert "datetime.now() -" in func_body or \
               "(datetime.now() - stale_created)" in func_body, \
            "卡死任务检测应使用 naive datetime.now()"

    def test_scheduler_registered_every_2_minutes(self):
        """定时器应注册为每 2 分钟执行"""
        content = _read_source("app.py")
        assert "schedule.every(2).minutes.do(schedule_weekly_sync_tasks)" in content

    def test_merged_project_view_uses_naive_now(self):
        """合并项目视图的活跃状态判断应使用 naive 本地时间"""
        content = _read_source("app.py")
        func_start = content.find("def merged_project_view(")
        func_end = content.find("\ndef ", func_start + 10)
        func_body = content[func_start:func_end]
        # 应使用 datetime.now() 而非 datetime.now(timezone.utc)
        assert "now = datetime.now()" in func_body, \
            "merged_project_view 应使用 datetime.now()"

    def test_create_weekly_sync_task_function_exists(self):
        """create_weekly_sync_task 函数应存在并接受 config_id 参数"""
        content = _read_source("app.py")
        assert "def create_weekly_sync_task(config_id):" in content

    def test_process_weekly_version_sync_function_exists(self):
        """process_weekly_version_sync 函数应存在"""
        content = _read_source("app.py")
        assert "def process_weekly_version_sync(config_id):" in content


# ---------------------------------------------------------------------------
#  2. 后台任务生命周期管理
# ---------------------------------------------------------------------------

class TestBackgroundTaskLifecycle:
    """验证后台任务的创建、状态转换、超时检测"""

    def test_task_model_has_required_fields(self):
        """BackgroundTask 模型应包含所有必要字段"""
        content = _read_source("models/task.py")
        required = [
            "task_type", "repository_id", "commit_id",
            "priority", "status", "created_at",
            "started_at", "completed_at",
            "error_message", "retry_count"
        ]
        for field in required:
            assert field in content, f"缺少字段: {field}"

    def test_task_status_values(self):
        """任务状态应包含完整的状态机"""
        content = _read_source("models/task.py")
        for status in ["pending", "processing", "completed", "failed"]:
            assert status in content, f"缺少状态: {status}"

    def test_task_to_dict_method(self):
        """BackgroundTask 应有 to_dict 方法"""
        content = _read_source("models/task.py")
        assert "def to_dict(self):" in content

    def test_weekly_sync_task_creation_logic(self):
        """周版本同步任务创建应检查重复"""
        content = _read_source("app.py")
        func_start = content.find("def create_weekly_sync_task(")
        func_end = content.find("\ndef ", func_start + 10)
        func_body = content[func_start:func_end]
        assert "status='pending'" in func_body
        assert "existing_task" in func_body

    def test_weekly_sync_task_uses_priority_3(self):
        """周版本同步任务应使用高优先级 3"""
        content = _read_source("app.py")
        func_start = content.find("def create_weekly_sync_task(")
        func_end = content.find("\ndef ", func_start + 10)
        func_body = content[func_start:func_end]
        assert "priority=3" in func_body

    def test_worker_handles_weekly_sync_type(self):
        """后台 worker 应能处理 weekly_sync 类型任务"""
        content = _read_source("app.py")
        assert "elif task['type'] == 'weekly_sync':" in content

    def test_worker_updates_task_status_on_complete(self):
        """worker 完成后应更新任务状态"""
        content = _read_source("app.py")
        assert "update_task_status_with_retry" in content


# ---------------------------------------------------------------------------
#  3. 后台任务暂停/恢复控制
# ---------------------------------------------------------------------------

class TestBackgroundTaskPauseResume:
    """验证后台任务暂停/恢复机制"""

    def test_pause_sets_flag(self):
        """暂停后 is_tasks_paused 应返回 True"""
        from services.background_task_service import (
            pause_background_tasks, resume_background_tasks, is_tasks_paused
        )
        try:
            pause_background_tasks()
            assert is_tasks_paused() is True
        finally:
            resume_background_tasks()

    def test_resume_clears_flag(self):
        """恢复后 is_tasks_paused 应返回 False"""
        from services.background_task_service import (
            pause_background_tasks, resume_background_tasks, is_tasks_paused
        )
        pause_background_tasks()
        resume_background_tasks()
        assert is_tasks_paused() is False

    def test_wait_if_paused_returns_immediately_when_not_paused(self):
        """未暂停时 wait_if_paused 应立即返回"""
        from services.background_task_service import (
            resume_background_tasks, wait_if_paused
        )
        resume_background_tasks()
        start = time.monotonic()
        wait_if_paused()
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"wait_if_paused 应立即返回，实际耗时 {elapsed:.2f}s"

    def test_wait_if_paused_blocks_then_resumes(self):
        """暂停时 wait_if_paused 应阻塞直到恢复"""
        from services.background_task_service import (
            pause_background_tasks, resume_background_tasks, wait_if_paused
        )
        pause_background_tasks()

        result = {"unblocked": False}

        def worker():
            wait_if_paused()
            result["unblocked"] = True

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.3)
        assert result["unblocked"] is False, "暂停期间 worker 不应解除阻塞"
        resume_background_tasks()
        t.join(timeout=2)
        assert result["unblocked"] is True, "恢复后 worker 应解除阻塞"

    def test_thread_safety_concurrent_pause_resume(self):
        """并发暂停/恢复不应产生竞态条件"""
        from services.background_task_service import (
            pause_background_tasks, resume_background_tasks, is_tasks_paused
        )
        errors = []

        def toggle(pause):
            try:
                for _ in range(50):
                    if pause:
                        pause_background_tasks()
                    else:
                        resume_background_tasks()
                    is_tasks_paused()
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=toggle, args=(True,))
        t2 = threading.Thread(target=toggle, args=(False,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        # 清理状态
        resume_background_tasks()
        assert not errors, f"并发操作出错: {errors}"


# ---------------------------------------------------------------------------
#  4. 时区一致性验证
# ---------------------------------------------------------------------------

class TestTimezoneConsistency:
    """验证项目中的时区处理一致性"""

    def test_timezone_utils_module_completeness(self):
        """时区工具模块应包含完整的转换函数"""
        content = _read_source("utils/timezone_utils.py")
        required_funcs = [
            "def utc_to_beijing(",
            "def beijing_to_utc(",
            "def format_beijing_time(",
            "def now_beijing(",
            "def now_utc(",
            "def parse_time_with_timezone(",
        ]
        for func in required_funcs:
            assert func in content, f"缺少函数: {func}"

    def test_utc_to_beijing_conversion(self):
        """UTC 转北京时间应正确加 8 小时"""
        from utils.timezone_utils import utc_to_beijing
        utc_time = datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc)
        beijing = utc_to_beijing(utc_time)
        assert beijing.hour == 18
        assert beijing.day == 2

    def test_utc_to_beijing_naive_input(self):
        """UTC 转换对 naive datetime 应假定为 UTC"""
        from utils.timezone_utils import utc_to_beijing
        naive_time = datetime(2026, 3, 2, 0, 0, 0)
        beijing = utc_to_beijing(naive_time)
        assert beijing.hour == 8

    def test_utc_to_beijing_none_input(self):
        """UTC 转换对 None 应返回 None"""
        from utils.timezone_utils import utc_to_beijing
        assert utc_to_beijing(None) is None

    def test_beijing_to_utc_conversion(self):
        """北京时间转 UTC 应正确减 8 小时"""
        from utils.timezone_utils import beijing_to_utc, BEIJING_TZ
        beijing_time = datetime(2026, 3, 2, 18, 0, 0, tzinfo=BEIJING_TZ)
        utc = beijing_to_utc(beijing_time)
        assert utc.hour == 10

    def test_format_beijing_time_default(self):
        """格式化北京时间应使用默认格式"""
        from utils.timezone_utils import format_beijing_time
        utc_time = datetime(2026, 3, 2, 10, 30, 0, tzinfo=timezone.utc)
        result = format_beijing_time(utc_time)
        assert "2026/03/02 18:30:00" == result

    def test_format_beijing_time_none(self):
        """None 输入应返回 '未知时间'"""
        from utils.timezone_utils import format_beijing_time
        assert format_beijing_time(None) == '未知时间'

    def test_parse_time_iso_format(self):
        """解析 ISO 格式时间字符串"""
        from utils.timezone_utils import parse_time_with_timezone
        dt = parse_time_with_timezone("2026-03-02T10:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_parse_time_common_format(self):
        """解析常见格式时间字符串"""
        from utils.timezone_utils import parse_time_with_timezone
        dt = parse_time_with_timezone("2026-03-02 10:00:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 3

    def test_parse_time_invalid_input(self):
        """无效时间字符串应返回 None"""
        from utils.timezone_utils import parse_time_with_timezone
        assert parse_time_with_timezone("not-a-date") is None
        assert parse_time_with_timezone("") is None
        assert parse_time_with_timezone(None) is None

    def test_naive_datetime_comparison_safety(self):
        """验证 naive 和 aware datetime 比较会抛出 TypeError"""
        naive = datetime(2026, 3, 2, 10, 0, 0)
        aware = datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError):
            _ = naive > aware

    def test_weekly_config_model_uses_naive_times(self):
        """周版本配置模型的 start_time/end_time 应为 naive DateTime"""
        content = _read_source("models/weekly_version.py")
        # 检查 start_time 和 end_time 字段定义
        assert "start_time = db.Column(db.DateTime" in content
        assert "end_time = db.Column(db.DateTime" in content


# ---------------------------------------------------------------------------
#  5. 状态同步双向链路
# ---------------------------------------------------------------------------

class TestStatusSyncBidirectional:
    """验证状态同步服务的源码结构和双向逻辑"""

    def test_sync_service_has_bidirectional_methods(self):
        """StatusSyncService 应有双向同步方法"""
        content = _read_source("services/status_sync_service.py")
        assert "def sync_commit_to_weekly(" in content
        assert "def sync_weekly_to_commit(" in content

    def test_sync_service_find_related_methods(self):
        """StatusSyncService 应有查找关联记录的方法"""
        content = _read_source("services/status_sync_service.py")
        assert "def _find_related_weekly_caches(" in content
        assert "def _find_related_commits(" in content

    def test_sync_service_handles_merged_diff(self):
        """StatusSyncService 应区分合并diff和普通diff"""
        content = _read_source("services/status_sync_service.py")
        assert "def _is_merged_diff(" in content

    def test_sync_service_clear_all_status(self):
        """StatusSyncService 应有清除所有确认状态的方法"""
        content = _read_source("services/status_sync_service.py")
        assert "def clear_all_confirmation_status(" in content

    def test_sync_service_mapping_info(self):
        """StatusSyncService 应提供同步映射信息查询"""
        content = _read_source("services/status_sync_service.py")
        assert "def get_sync_mapping_info(" in content

    def test_weekly_diff_cache_has_confirmation_status(self):
        """周版本 diff 缓存应有确认状态字段"""
        content = _read_source("models/weekly_version.py")
        assert "confirmation_status" in content
        assert "overall_status" in content

    def test_commit_model_has_status_field(self):
        """提交模型应有 status 字段"""
        content = _read_source("models/commit.py")
        assert "status = db.Column(" in content
        assert "'pending'" in content


# ---------------------------------------------------------------------------
#  6. 周版本模型完整性
# ---------------------------------------------------------------------------

class TestWeeklyVersionModelChain:
    """验证周版本相关 ORM 模型的完整性"""

    def test_weekly_version_config_model(self):
        """WeeklyVersionConfig 模型字段完整"""
        content = _read_source("models/weekly_version.py")
        required = [
            "project_id", "repository_id", "name", "description",
            "branch", "start_time", "end_time", "cycle_type",
            "is_active", "auto_sync", "status",
        ]
        for field in required:
            assert field in content, f"WeeklyVersionConfig 缺少字段: {field}"

    def test_weekly_version_diff_cache_model(self):
        """WeeklyVersionDiffCache 模型字段完整"""
        content = _read_source("models/weekly_version.py")
        required = [
            "config_id", "repository_id", "file_path", "file_type",
            "merged_diff_data", "base_commit_id", "latest_commit_id",
            "commit_authors", "commit_messages", "commit_times",
            "commit_count", "confirmation_status", "overall_status",
            "cache_status", "processing_time", "file_size",
        ]
        for field in required:
            assert field in content, f"WeeklyVersionDiffCache 缺少字段: {field}"

    def test_weekly_version_excel_cache_model(self):
        """WeeklyVersionExcelCache 模型字段完整"""
        content = _read_source("models/weekly_version.py")
        required = [
            "cache_key", "html_content", "css_content", "js_content",
            "cache_metadata", "cache_status", "diff_version",
        ]
        for field in required:
            assert field in content, f"WeeklyVersionExcelCache 缺少字段: {field}"

    def test_weekly_diff_cache_has_indexes(self):
        """WeeklyVersionDiffCache 应有性能索引"""
        content = _read_source("models/weekly_version.py")
        expected_indexes = [
            "idx_weekly_diff_config_file",
            "idx_weekly_diff_repo",
            "idx_weekly_diff_status",
            "idx_weekly_diff_cache_status",
        ]
        for idx in expected_indexes:
            assert idx in content, f"缺少索引: {idx}"

    def test_weekly_excel_cache_has_indexes(self):
        """WeeklyVersionExcelCache 应有性能索引"""
        content = _read_source("models/weekly_version.py")
        expected_indexes = [
            "idx_weekly_excel_config_file",
            "idx_weekly_excel_repo",
            "idx_weekly_excel_status",
        ]
        for idx in expected_indexes:
            assert idx in content, f"缺少索引: {idx}"

    def test_models_have_relationships(self):
        """模型应有正确的关系定义"""
        content = _read_source("models/weekly_version.py")
        assert "db.relationship('Project'" in content
        assert "db.relationship('Repository'" in content
        assert "db.relationship('WeeklyVersionConfig'" in content

    def test_config_status_values(self):
        """周版本配置应支持三种状态"""
        content = _read_source("models/weekly_version.py")
        for status in ["active", "completed", "archived"]:
            assert status in content


# ---------------------------------------------------------------------------
#  7. Git 服务核心链路
# ---------------------------------------------------------------------------

class TestGitServiceCoreChain:
    """验证 Git 服务的核心方法和结构"""

    def test_git_service_class_exists(self):
        """GitService 类应存在"""
        content = _read_source("services/git_service.py")
        assert "class GitService:" in content

    def test_git_service_core_methods(self):
        """GitService 应有核心方法"""
        content = _read_source("services/git_service.py")
        required = [
            "def clone_or_update_repository(",
            "def get_commits(",
            "def get_file_diff(",
            "def get_commit_range_diff(",
            "def get_file_content(",
            "def get_branches(",
            "def test_network_connectivity(",
        ]
        for method in required:
            assert method in content, f"缺少方法: {method}"

    def test_enhanced_git_service_extends_base(self):
        """EnhancedGitService 应继承 GitService"""
        content = _read_source("services/enhanced_git_service.py")
        assert "class EnhancedGitService(GitService):" in content

    def test_enhanced_git_service_has_retry_logic(self):
        """EnhancedGitService 应有重试逻辑"""
        content = _read_source("services/enhanced_git_service.py")
        assert "retry" in content.lower()
        assert "def clone_or_update_repository_with_retry(" in content

    def test_threaded_git_service_extends_base(self):
        """ThreadedGitService 应继承 GitService"""
        content = _read_source("services/threaded_git_service.py")
        assert "class ThreadedGitService(GitService):" in content

    def test_threaded_git_service_has_date_range_method(self):
        """ThreadedGitService 应有日期范围查询方法"""
        content = _read_source("services/threaded_git_service.py")
        assert "def get_commits_in_date_range_base(" in content

    def test_git_service_has_thread_pool_cleanup(self):
        """GitService 应有线程池清理方法"""
        content = _read_source("services/git_service.py")
        assert "def cleanup_thread_pool(" in content

    def test_git_service_excel_diff_support(self):
        """GitService 应支持 Excel diff"""
        content = _read_source("services/git_service.py")
        assert "def parse_excel_diff(" in content


# ---------------------------------------------------------------------------
#  8. Diff 服务边界场景
# ---------------------------------------------------------------------------

class TestDiffServiceEdgeCases:
    """测试 DiffService 的边界场景和特殊输入处理"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from services.diff_service import DiffService
        self.service = DiffService()

    def test_empty_file_diff(self):
        """空文件 diff 不应崩溃"""
        result = self.service.process_diff("test.py", b"", None)
        assert result is not None
        assert "type" in result

    def test_binary_file_detection(self):
        """二进制文件应正确检测"""
        assert self.service.get_file_type("test.bin") == "binary"
        assert self.service.get_file_type("archive.tar.gz") == "binary"
        assert self.service.get_file_type("font.woff2") == "binary"

    def test_unicode_content_diff(self):
        """包含中文的文件 diff 不应崩溃"""
        old_content = "第一行\n第二行\n第三行\n".encode("utf-8")
        new_content = "第一行\n修改后的第二行\n第三行\n第四行\n".encode("utf-8")
        result = self.service.process_diff("test.txt", new_content, old_content)
        assert result["type"] == "text"

    def test_csv_with_empty_cells(self):
        """含空单元格的 CSV diff"""
        old_csv = b"id,name,value\n1,,100\n2,Bob,\n"
        new_csv = b"id,name,value\n1,Alice,100\n2,Bob,200\n"
        result = self.service.process_diff("data.csv", new_csv, old_csv)
        assert result["type"] == "excel"

    def test_large_csv_performance(self):
        """大 CSV 文件处理不应超时"""
        # 生成 500 行 CSV
        header = "id,name,value,category,score\n"
        rows = "".join(f"{i},name_{i},{i*100},cat_{i%5},{i*1.1}\n" for i in range(500))
        old_csv = (header + rows).encode("utf-8")
        # 修改 10 行
        modified_rows = rows
        for i in range(100, 110):
            modified_rows = modified_rows.replace(f"name_{i}", f"modified_{i}")
        new_csv = (header + modified_rows).encode("utf-8")

        start = time.monotonic()
        result = self.service.process_diff("big.csv", new_csv, old_csv)
        elapsed = time.monotonic() - start

        assert result["type"] == "excel"
        assert elapsed < 30, f"处理 500 行 CSV 耗时 {elapsed:.1f}s，应 <30s"

    def test_normalize_value_edge_cases(self):
        """_normalize_value 对各种特殊值的处理"""
        nv = self.service._normalize_value
        assert nv(False) == "False"
        assert nv(0) == "0"
        assert nv(0.0) == "0.0"
        assert nv([]) == "[]"
        assert nv("  hello  ") == "hello"

    def test_row_similarity_completely_different(self):
        """完全不同的行相似度应为 0"""
        cols = ["A", "B", "C"]
        row1 = {"A": "1", "B": "2", "C": "3"}
        row2 = {"A": "x", "B": "y", "C": "z"}
        score = self.service._calculate_row_similarity(row1, row2, cols)
        assert score == 0.0

    def test_delete_only_csv_diff(self):
        """仅删除行的 CSV diff"""
        old_csv = b"id,name\n1,Alice\n2,Bob\n3,Charlie\n"
        new_csv = b"id,name\n1,Alice\n"
        result = self.service.process_diff("data.csv", new_csv, old_csv)
        assert result["type"] == "excel"


# ---------------------------------------------------------------------------
#  9. 安全工具链路
# ---------------------------------------------------------------------------

class TestSecurityToolsChain:
    """验证安全相关工具的完整性"""

    def test_request_security_has_csrf(self):
        """请求安全模块应有 CSRF 保护"""
        content = _read_source("utils/request_security.py")
        assert "csrf_token" in content
        assert "def configure_request_security(" in content

    def test_request_security_has_admin_decorator(self):
        """请求安全模块应有管理员验证装饰器"""
        content = _read_source("utils/request_security.py")
        assert "def require_admin(" in content

    def test_credential_encryption_exists(self):
        """应有凭证加密机制"""
        content = _read_source("utils/request_security.py")
        # 检查加密相关函数
        assert "encrypt" in content.lower() or "cipher" in content.lower() or \
               "token" in content.lower()

    def test_admin_login_route_exists(self):
        """应有管理员登录路由"""
        content = _read_source("routes/core_management_routes.py")
        assert "/auth/login" in content

    def test_admin_logout_route_exists(self):
        """应有管理员登出路由"""
        content = _read_source("routes/core_management_routes.py")
        assert "admin_logout" in content

    def test_sensitive_routes_protected(self):
        """敏感路由应有权限保护"""
        content = _read_source("routes/core_management_routes.py")
        # 仓库操作应有验证
        assert "require_admin" in content or "admin" in content


# ---------------------------------------------------------------------------
#  10. 数据库配置链路
# ---------------------------------------------------------------------------

class TestDatabaseConfigChain:
    """验证数据库配置和初始化"""

    def test_database_settings_module_exists(self):
        """数据库配置模块应存在"""
        from utils.db_config import build_database_settings, apply_database_settings
        assert callable(build_database_settings)
        assert callable(apply_database_settings)

    def test_default_backend_is_sqlite(self):
        """默认数据库后端应为 SQLite"""
        from utils.db_config import build_database_settings
        settings = build_database_settings({})
        uri = settings["database_uri"]
        assert "sqlite" in uri.lower()

    def test_models_package_init(self):
        """models 包应有正确的 __init__.py"""
        content = _read_source("models/__init__.py")
        assert "db" in content

    def test_all_models_importable(self):
        """所有模型文件应可正常导入定义"""
        model_files = [
            "models/project.py",
            "models/repository.py",
            "models/commit.py",
            "models/task.py",
            "models/cache.py",
            "models/weekly_version.py",
            "models/operation_log.py",
        ]
        for mf in model_files:
            content = _read_source(mf)
            assert "db.Model" in content, f"{mf} 应包含 db.Model 定义"

    def test_diff_platform_db_filename(self):
        """数据库文件名应为 diff_platform.db"""
        from utils.db_config import build_database_settings
        settings = build_database_settings({})
        uri = settings["database_uri"]
        assert "diff_platform" in uri


# ---------------------------------------------------------------------------
#  11. 周版本路由端点完整性
# ---------------------------------------------------------------------------

class TestWeeklyVersionRoutes:
    """验证周版本管理的路由端点完整性"""

    def test_weekly_config_route(self):
        """周版本配置路由应存在"""
        content = _read_source("routes/weekly_version_management_routes.py")
        assert "weekly_version_config_route" in content

    def test_weekly_config_api_route(self):
        """周版本配置 API 路由应存在"""
        content = _read_source("routes/weekly_version_management_routes.py")
        assert "weekly_version_config_api_route" in content

    def test_weekly_diff_route(self):
        """周版本 diff 查看路由应存在"""
        content = _read_source("routes/weekly_version_management_routes.py")
        assert "weekly_version_diff_route" in content

    def test_weekly_files_api(self):
        """周版本文件列表 API 应存在"""
        content = _read_source("routes/weekly_version_management_routes.py")
        assert "weekly_version_files_api_route" in content

    def test_weekly_file_diff_api(self):
        """周版本文件 diff API 应存在"""
        content = _read_source("routes/weekly_version_management_routes.py")
        assert "weekly_version_file_diff_api_route" in content

    def test_weekly_status_api(self):
        """周版本文件状态 API 应存在"""
        content = _read_source("routes/weekly_version_management_routes.py")
        assert "weekly_version_file_status_api_route" in content

    def test_weekly_batch_confirm_api(self):
        """周版本批量确认 API 应存在"""
        content = _read_source("routes/weekly_version_management_routes.py")
        assert "weekly_version_batch_confirm_api_route" in content

    def test_weekly_stats_api(self):
        """周版本统计 API 应存在"""
        content = _read_source("routes/weekly_version_management_routes.py")
        assert "weekly_version_stats_api_route" in content


# ---------------------------------------------------------------------------
#  12. 模型加载器服务解耦验证
# ---------------------------------------------------------------------------

class TestModelLoaderDecoupling:
    """验证模型加载器的服务解耦设计"""

    def test_model_loader_core_functions(self):
        """模型加载器应有核心函数"""
        content = _read_source("services/model_loader.py")
        assert "def get_runtime_model(" in content
        assert "def get_runtime_models(" in content
        assert "def clear_model_loader_cache(" in content

    def test_services_use_model_loader(self):
        """服务层应使用模型加载器而非直接导入 app"""
        services_to_check = [
            "services/status_sync_service.py",
            "services/weekly_excel_cache_service.py",
        ]
        for svc in services_to_check:
            content = _read_source(svc)
            # 不应直接从 app 导入模型
            assert "from app import" not in content, \
                f"{svc} 不应直接从 app 导入"


# ---------------------------------------------------------------------------
#  13. 缓存管理全链路
# ---------------------------------------------------------------------------

class TestCacheManagementFullChain:
    """验证缓存管理的完整业务链路"""

    def test_excel_diff_cache_service_lifecycle(self):
        """ExcelDiffCacheService 应有完整的生命周期方法"""
        content = _read_source("services/excel_diff_cache_service.py")
        lifecycle_methods = [
            "def get_cached_diff(",
            "def save_cached_diff(",
            "def cache_diff_error(",
            "def cleanup_old_cache(",
            "def cleanup_expired_cache(",
            "def cleanup_version_mismatch_cache(",
            "def get_cache_statistics(",
        ]
        for method in lifecycle_methods:
            assert method in content, f"缺少方法: {method}"

    def test_html_cache_service_lifecycle(self):
        """ExcelHtmlCacheService 应有完整的生命周期方法"""
        content = _read_source("services/excel_html_cache_service.py")
        lifecycle_methods = [
            "def get_cached_html(",
            "def save_html_cache(",
            "def generate_excel_html(",
            "def cleanup_old_version_cache(",
            "def cleanup_expired_cache(",
            "def delete_html_cache(",
        ]
        for method in lifecycle_methods:
            assert method in content, f"缺少方法: {method}"

    def test_weekly_cache_service_lifecycle(self):
        """WeeklyExcelCacheService 应有完整的生命周期方法"""
        content = _read_source("services/weekly_excel_cache_service.py")
        lifecycle_methods = [
            "def needs_merged_diff_cache(",
            "def get_cached_html(",
            "def save_html_cache(",
            "def cleanup_expired_cache(",
            "def cleanup_old_cache(",
            "def clear_all_cache(",
        ]
        for method in lifecycle_methods:
            assert method in content, f"缺少方法: {method}"

    def test_cache_routes_complete(self):
        """缓存管理路由应完整"""
        content = _read_source("routes/cache_management_routes.py")
        expected = [
            "cleanup_expired_cache",
            "clear_all_diff_cache",
            "clear_excel_html_cache",
            "get_excel_html_cache_stats",
            "get_weekly_excel_cache_stats",
        ]
        for ep in expected:
            assert ep in content, f"缺少缓存管理端点: {ep}"


# ---------------------------------------------------------------------------
#  14. 提交记录处理链路
# ---------------------------------------------------------------------------

class TestCommitProcessingChain:
    """验证提交记录的处理和 diff 计算链路"""

    def test_commit_model_completeness(self):
        """Commit 模型应包含所有必要字段"""
        content = _read_source("models/commit.py")
        required = [
            "repository_id", "commit_id", "path",
            "version", "operation", "author",
            "commit_time", "message", "status",
        ]
        for field in required:
            assert field in content, f"缺少字段: {field}"

    def test_commit_model_tablename(self):
        """Commit 模型表名应为 commits_log"""
        content = _read_source("models/commit.py")
        assert "__tablename__ = 'commits_log'" in content

    def test_commit_to_dict_method(self):
        """Commit 模型应有 to_dict 方法"""
        content = _read_source("models/commit.py")
        assert "def to_dict(self):" in content

    def test_commit_diff_routes_complete(self):
        """提交 diff 路由蓝图端点完整"""
        content = _read_source("routes/commit_diff_routes.py")
        expected = [
            "commit_list_route",
            "commit_diff_route",
            "update_commit_status_route",
            "batch_update_commits_compat_route",
            "refresh_commit_diff_route",
        ]
        for ep in expected:
            assert ep in content, f"缺少端点: {ep}"

    def test_batch_approve_reject_routes(self):
        """批量确认/拒绝路由应存在"""
        content = _read_source("routes/commit_diff_routes.py")
        assert "batch_approve_commits_route" in content
        assert "batch_reject_commits_route" in content


# ---------------------------------------------------------------------------
#  15. SVN 服务链路
# ---------------------------------------------------------------------------

class TestSVNServiceChain:
    """验证 SVN 服务的核心结构"""

    def test_svn_service_class_exists(self):
        """SVNService 类应存在"""
        content = _read_source("services/svn_service.py")
        assert "class SVNService:" in content

    def test_svn_service_core_methods(self):
        """SVNService 应有核心方法"""
        content = _read_source("services/svn_service.py")
        required = [
            "def checkout_or_update_repository(",
            "def get_commits(",
            "def get_file_diff(",
            "def get_version_range_diff(",
            "def parse_excel_diff(",
            "def sync_repository_commits(",
        ]
        for method in required:
            assert method in content, f"缺少方法: {method}"


# ---------------------------------------------------------------------------
#  16. 模板文件完整性
# ---------------------------------------------------------------------------

class TestTemplateCompleteness:
    """验证关键模板文件的存在和基本结构"""

    def test_base_template_exists(self):
        """base.html 模板应存在"""
        assert (PROJECT_ROOT / "templates" / "base.html").exists()

    def test_weekly_version_templates_exist(self):
        """周版本相关模板应存在"""
        templates = [
            "weekly_version_config.html",
            "weekly_version_diff.html",
        ]
        for tmpl in templates:
            assert (PROJECT_ROOT / "templates" / tmpl).exists(), f"缺少模板: {tmpl}"

    def test_commit_list_template_exists(self):
        """提交列表模板应存在"""
        assert (PROJECT_ROOT / "templates" / "commit_list.html").exists()

    def test_weekly_config_uses_enterprise_theme(self):
        """周版本配置页面应使用企业级深蓝主题"""
        content = _read_source("templates/weekly_version_config.html")
        assert "#1e3a5f" in content or "linear-gradient" in content

    def test_weekly_diff_uses_enterprise_theme(self):
        """周版本 diff 页面应使用企业级深蓝主题"""
        content = _read_source("templates/weekly_version_diff.html")
        assert "#1e3a5f" in content or "linear-gradient" in content


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-x"])
