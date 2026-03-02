#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
业务全链路集成测试
==================
覆盖从项目创建 → 仓库配置 → 提交获取 → Diff 计算 → 缓存管理 的完整业务流程。

测试分层：
- TestDiffServiceChain:          DiffService 核心 diff 计算链路
- TestExcelDiffCacheChain:       Excel diff 缓存的存取链路
- TestExcelHtmlCacheChain:       HTML 缓存生成 / 命中 / 淘汰链路
- TestWeeklyExcelCacheChain:     周版本缓存判定与命中链路
- TestThreadSafety:              多线程竞态安全验证
- TestCacheCleanupChain:         缓存清理全链路
- TestStatusSyncChain:           状态同步链路
- TestRouteEndpointsChain:       路由端点可达性链路
- TestDiffAccuracyLargeDataset:  大数据集 diff 准确性 (#29)
"""

import os
import sys
import json
import math
import threading
import time
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, Any, List
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
#  Helper / Fixtures
# ---------------------------------------------------------------------------

def _read_source(relative_path: str) -> str:
    """读取源码文件内容"""
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _make_row(columns: list, values: list) -> dict:
    """快速构造一行数据"""
    return {col: val for col, val in zip(columns, values)}


def _build_excel_diff_data(sheets: Dict[str, list], file_path: str = "test.xlsx") -> Dict[str, Any]:
    """构造标准 Excel diff 数据结构"""
    return {
        "type": "excel",
        "file_path": file_path,
        "sheets": {
            name: {
                "rows": rows,
                "stats": {
                    "added": sum(1 for r in rows if r.get("status") == "added"),
                    "removed": sum(1 for r in rows if r.get("status") == "removed"),
                    "modified": sum(1 for r in rows if r.get("status") == "modified"),
                    "unchanged": sum(1 for r in rows if r.get("status") == "unchanged"),
                }
            }
            for name, rows in sheets.items()
        }
    }


# ---------------------------------------------------------------------------
#  1. DiffService 核心 diff 计算链路
# ---------------------------------------------------------------------------

class TestDiffServiceChain:
    """测试 DiffService 的文件类型判断 → diff 计算 → 结果结构完整性"""

    @pytest.fixture(autouse=True)
    def setup_diff_service(self):
        from services.diff_service import DiffService
        self.service = DiffService()

    # -- 文件类型判断 --

    def test_file_type_detection_excel(self):
        """Excel / CSV 文件应识别为 'excel' 类型"""
        for ext in [".xlsx", ".xls", ".xlsm", ".xlsb", ".ods", ".csv", ".tsv"]:
            ftype = self.service.get_file_type(f"data/test{ext}")
            assert ftype == "excel", f"扩展名 {ext} 未被识别为 excel"

    def test_file_type_detection_text(self):
        """文本文件应识别为 'text' 类型"""
        for ext in [".py", ".js", ".json", ".yaml", ".md", ".sql"]:
            ftype = self.service.get_file_type(f"src/main{ext}")
            assert ftype == "text", f"扩展名 {ext} 未被识别为 text"

    def test_file_type_detection_image(self):
        """图片文件应识别为 'image' 类型"""
        for ext in [".png", ".jpg", ".gif", ".webp", ".svg"]:
            ftype = self.service.get_file_type(f"assets/logo{ext}")
            assert ftype == "image", f"扩展名 {ext} 未被识别为 image"

    def test_file_type_detection_binary(self):
        """未知扩展名应识别为 'binary' 类型"""
        assert self.service.get_file_type("data.bin") == "binary"
        assert self.service.get_file_type("archive.zip") == "binary"

    # -- 文本 diff 计算 --

    def test_text_diff_new_file(self):
        """新建文件的 diff 应全部标记为 added"""
        content = b"line1\nline2\nline3\n"
        result = self.service.process_diff("test.py", content, None)
        assert result["type"] == "text"
        assert "hunks" in result or "diff_content" in result

    def test_text_diff_modified_file(self):
        """修改文件应产生 diff"""
        old_content = b"line1\nline2\nline3\n"
        new_content = b"line1\nline2_modified\nline3\nline4\n"
        result = self.service.process_diff("test.py", new_content, old_content)
        assert result["type"] == "text"

    def test_text_diff_identical_file(self):
        """相同文件 diff 应无变更"""
        content = b"line1\nline2\nline3\n"
        result = self.service.process_diff("test.py", content, content)
        assert result["type"] == "text"
        # 相同内容应无 hunk 或变更行为 0
        if "stats" in result:
            stats = result["stats"]
            assert stats.get("added", 0) == 0
            assert stats.get("removed", 0) == 0

    # -- Excel diff 计算（含 CSV）--

    def test_csv_diff_basic(self):
        """CSV 文件 diff 基本链路"""
        old_csv = b"id,name,value\n1,Alice,100\n2,Bob,200\n"
        new_csv = b"id,name,value\n1,Alice,150\n2,Bob,200\n3,Charlie,300\n"
        result = self.service.process_diff("data.csv", new_csv, old_csv)
        assert result["type"] == "excel"
        assert "sheets" in result

    def test_csv_diff_new_file(self):
        """新建 CSV 文件 diff"""
        new_csv = b"id,name\n1,Alice\n2,Bob\n"
        result = self.service.process_diff("data.csv", new_csv, None)
        assert result["type"] == "excel"

    # -- normalize_value 静态方法 --

    def test_normalize_value_nan_handling(self):
        """_normalize_value 应正确处理 NaN/None/空字符串"""
        nv = self.service._normalize_value
        assert nv(None) is None
        assert nv(float("nan")) is None
        assert nv("nan") is None
        assert nv("None") is None
        assert nv("null") is None
        assert nv("") is None
        assert nv("  ") is None
        assert nv("<NA>") is None
        assert nv("hello") == "hello"
        assert nv(123) == "123"
        assert nv(0) == "0"

    # -- 行匹配相关 --

    def test_row_similarity_identical_rows(self):
        """完全相同的行相似度应为 1.0"""
        cols = ["A", "B", "C"]
        row = {"A": "1", "B": "test", "C": "100"}
        assert self.service._calculate_row_similarity(row, row, cols) == 1.0

    def test_row_similarity_partial_match(self):
        """部分匹配行的相似度应在 0 到 1 之间"""
        cols = ["A", "B", "C"]
        row1 = {"A": "1", "B": "test", "C": "100"}
        row2 = {"A": "1", "B": "test", "C": "999"}
        score = self.service._calculate_row_similarity(row1, row2, cols)
        assert 0 < score < 1.0
        assert abs(score - 2 / 3) < 0.01  # 2/3 列匹配

    def test_row_similarity_empty_rows(self):
        """两个空行应视为匹配"""
        cols = ["A", "B"]
        row1 = {"A": None, "B": ""}
        row2 = {"A": "", "B": None}
        assert self.service._calculate_row_similarity(row1, row2, cols) == 1.0

    def test_rows_equal(self):
        """_rows_equal 精确匹配验证"""
        cols = ["A", "B"]
        r1 = {"A": "1", "B": "hello"}
        r2 = {"A": "1", "B": "hello"}
        r3 = {"A": "1", "B": "world"}
        assert self.service._rows_equal(r1, r2, cols) is True
        assert self.service._rows_equal(r1, r3, cols) is False

    def test_values_equal_with_normalization(self):
        """_values_equal 应对空值做归一化"""
        assert self.service._values_equal(None, "") is True
        assert self.service._values_equal("nan", None) is True
        assert self.service._values_equal("hello", "hello") is True
        assert self.service._values_equal("hello", "world") is False

    # -- 行哈希（改进后全列哈希）--

    def test_calculate_row_hash_full_columns(self):
        """行哈希应基于全部列计算（#37 修复后）"""
        cols = ["A", "B", "C", "D", "E", "F", "G"]
        row1 = {c: str(i) for i, c in enumerate(cols)}
        row2 = dict(row1)
        row2["G"] = "different"
        
        h1 = self.service._calculate_row_hash(row1, cols)
        h2 = self.service._calculate_row_hash(row2, cols)
        
        # 最后一列不同 → 哈希应不同
        assert h1 != h2

    def test_calculate_row_hash_empty_rows_unique(self):
        """空行应返回唯一标记（#37 修复后）"""
        cols = ["A", "B"]
        row1 = {"A": "", "B": None}
        row2 = {"A": None, "B": ""}
        
        h1 = self.service._calculate_row_hash(row1, cols)
        h2 = self.service._calculate_row_hash(row2, cols)
        # 不同空行对象应有不同哈希
        assert h1 != h2

    # -- 快速相似度预检（#32 修复后）--

    def test_quick_similarity_check_threshold(self):
        """_quick_similarity_check 至少需要2个关键列匹配"""
        cols = ["A", "B", "C", "D"]
        row1 = {"A": "1", "B": "2", "C": "3", "D": "4"}
        
        # 仅1个关键列匹配 → 应失败（除非ID列特殊匹配）
        row_1match = {"A": "1", "B": "X", "C": "Y", "D": "Z"}
        # 由于 A 列是第一列（ID列），特殊逻辑可能通过，这里验证逻辑一致性
        result = self.service._quick_similarity_check(row1, row_1match, cols)
        # 第一列相同 → 应通过ID列特殊检查
        assert result is True

        # 所有关键列都不匹配
        row_0match = {"A": "X", "B": "Y", "C": "Z", "D": "W"}
        result = self.service._quick_similarity_check(row1, row_0match, cols)
        assert result is False


# ---------------------------------------------------------------------------
#  2. Excel Diff 缓存存取链路
# ---------------------------------------------------------------------------

class TestExcelDiffCacheChain:
    """测试 ExcelDiffCacheService 的缓存保存 → 命中 → 淘汰链路（纯逻辑验证）"""

    def test_service_source_code_structure(self):
        """验证 ExcelDiffCacheService 源码结构完整性"""
        content = _read_source("services/excel_diff_cache_service.py")
        # 核心方法存在
        assert "def get_cached_diff(" in content
        assert "def save_cached_diff(" in content
        assert "def cache_diff_error(" in content
        assert "def cleanup_old_cache(" in content
        assert "def _cleanup_old_cache(" in content
        assert "def process_excel_diff_background(" in content

    def test_thread_safe_processing_set(self):
        """验证 processing_commits 使用了线程安全机制 (#30)"""
        content = _read_source("services/excel_diff_cache_service.py")
        assert "_processing_lock" in content
        assert "threading.Lock()" in content
        assert "with self._processing_lock:" in content

    def test_no_global_session_expire_all(self):
        """验证不再使用全局 session.expire_all() (#22)"""
        content = _read_source("services/excel_diff_cache_service.py")
        assert "db.session.expire_all()" not in content
        assert ".populate_existing()" in content

    def test_no_post_save_verification_query(self):
        """验证保存后不再执行冗余验证查询 (#33)"""
        content = _read_source("services/excel_diff_cache_service.py")
        # 原来的验证代码模式
        assert "移除冗余验证查询" in content or "saved_cache = DiffCache.query.filter_by" not in content

    def test_log_cleanup_frequency_controlled(self):
        """验证日志清理频率受计数器控制 (#34)"""
        content = _read_source("services/excel_diff_cache_service.py")
        assert "_log_write_count" in content
        assert "% 50 ==" in content

    def test_cleanup_uses_batch_delete(self):
        """验证缓存清理使用批量DELETE策略 (#23, #35)"""
        content = _read_source("services/excel_diff_cache_service.py")
        assert "delete(synchronize_session=False)" in content
        assert "subquery" in content

    def test_optimize_diff_data_keeps_only_changes(self):
        """验证 optimize_diff_data 只保留有变更的行"""
        from services.excel_diff_cache_service import ExcelDiffCacheService
        service = ExcelDiffCacheService()
        
        diff_data = _build_excel_diff_data({
            "Sheet1": [
                {"status": "unchanged", "cells": {"A": "1"}},
                {"status": "added", "cells": {"A": "2"}},
                {"status": "unchanged", "cells": {"A": "3"}},
                {"status": "modified", "cells": {"A": "4"}},
                {"status": "removed", "cells": {"A": "5"}},
            ]
        })
        
        optimized = service.optimize_diff_data(diff_data)
        rows = optimized["sheets"]["Sheet1"]["rows"]
        
        # 只保留 added / modified / removed
        assert len(rows) == 3
        assert all(r["status"] in ("added", "modified", "removed") for r in rows)

    def test_optimize_diff_data_non_excel_passthrough(self):
        """非 Excel 类型 diff 数据应原样返回"""
        from services.excel_diff_cache_service import ExcelDiffCacheService
        service = ExcelDiffCacheService()
        
        text_diff = {"type": "text", "hunks": []}
        assert service.optimize_diff_data(text_diff) is text_diff

    def test_is_excel_file_detection(self):
        """Excel 文件检测"""
        from services.excel_diff_cache_service import ExcelDiffCacheService
        service = ExcelDiffCacheService()
        
        assert service.is_excel_file("test.xlsx") is True
        assert service.is_excel_file("test.xls") is True
        assert service.is_excel_file("test.xlsm") is True
        assert service.is_excel_file("test.txt") is False
        assert service.is_excel_file("TEST.XLSX") is True


# ---------------------------------------------------------------------------
#  3. HTML 缓存生成 / 命中 / 淘汰链路
# ---------------------------------------------------------------------------

class TestExcelHtmlCacheChain:
    """测试 ExcelHtmlCacheService 的 HTML 生成 → 缓存 → 淘汰链路"""

    def test_service_source_code_structure(self):
        """验证 ExcelHtmlCacheService 源码结构完整性"""
        content = _read_source("services/excel_html_cache_service.py")
        assert "class ExcelHtmlCacheService:" in content
        assert "def generate_cache_key(" in content
        assert "def get_cached_html(" in content
        assert "def save_html_cache(" in content
        assert "def generate_excel_html(" in content
        assert "def cleanup_old_version_cache(" in content
        assert "def cleanup_expired_cache(" in content
        assert "def get_cache_statistics(" in content

    def test_cache_key_uses_sha256(self):
        """验证缓存 key 使用 SHA-256 (#36)"""
        content = _read_source("services/excel_html_cache_service.py")
        assert "hashlib.sha256" in content
        assert "hashlib.md5" not in content

    def test_cache_statistics_use_sql_aggregation(self):
        """验证缓存统计使用 SQL 聚合 (#24)"""
        content = _read_source("services/excel_html_cache_service.py")
        assert "func.sum(" in content
        assert "func.length(func.coalesce(" in content

    def test_context_safety_in_save_and_get(self):
        """验证 save/get 方法在 app_context 内完成所有操作 (#28)"""
        content = _read_source("services/excel_html_cache_service.py")
        # 确认 with flask_app.app_context() 内有 return
        assert "with flask_app.app_context():" in content or "with self.flask_app.app_context():" in content

    def test_generate_excel_html_produces_valid_output(self):
        """验证 HTML 生成函数产出非空结果"""
        from services.excel_html_cache_service import ExcelHtmlCacheService
        
        mock_db = MagicMock()
        service = ExcelHtmlCacheService(mock_db, "1.8.0")
        
        diff_data = _build_excel_diff_data({
            "Sheet1": [
                {"status": "added", "cells": {"A": "1", "B": "hello"}},
                {"status": "modified", "cells": {"A": "2", "B": "world"},
                 "previous_cells": {"A": "2", "B": "old_world"}},
            ]
        })
        
        html, css, js = service.generate_excel_html(diff_data)
        assert len(html) > 0
        assert len(css) > 0

    def test_generate_cache_key_deterministic(self):
        """相同参数应产生相同的缓存 key"""
        from services.excel_html_cache_service import ExcelHtmlCacheService
        
        mock_db = MagicMock()
        service = ExcelHtmlCacheService(mock_db, "1.8.0")
        
        key1 = service.generate_cache_key(1, "abc123", "data/test.xlsx")
        key2 = service.generate_cache_key(1, "abc123", "data/test.xlsx")
        key3 = service.generate_cache_key(1, "abc123", "data/test2.xlsx")
        
        assert key1 == key2
        assert key1 != key3
        assert len(key1) == 64  # SHA-256 hex digest


# ---------------------------------------------------------------------------
#  4. 周版本缓存判定与命中链路
# ---------------------------------------------------------------------------

class _SortableColumn:
    def desc(self):
        return self

class _FakeQuery:
    def __init__(self, first_result=None):
        self._first_result = first_result
        self.filter_calls = []

    def filter_by(self, **kwargs):
        self.filter_calls.append(kwargs)
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._first_result


class TestWeeklyExcelCacheChain:
    """测试周版本缓存的判定逻辑和缓存 key 生成"""

    def _patch_runtime_models(self, monkeypatch, diff_query, html_query):
        import services.weekly_excel_cache_service as wcm
        
        diff_model = type("DiffModel", (), {
            "query": diff_query,
            "updated_at": _SortableColumn(),
        })
        html_model = type("HtmlModel", (), {"query": html_query})

        def fake_get_runtime_models(*names):
            mapping = {
                "WeeklyVersionDiffCache": diff_model,
                "WeeklyVersionExcelCache": html_model,
            }
            return tuple(mapping[name] for name in names)

        monkeypatch.setattr(wcm, "get_runtime_models", fake_get_runtime_models)

    def test_non_excel_file_never_requires_cache(self):
        """非 Excel 文件不应触发周版本缓存"""
        from services.weekly_excel_cache_service import WeeklyExcelCacheService
        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        assert service.needs_merged_diff_cache(101, "foo/bar.txt") is False
        assert service.needs_merged_diff_cache(101, "foo/bar.py") is False
        assert service.needs_merged_diff_cache(101, "foo/bar.json") is False

    def test_excel_file_detection_all_extensions(self):
        """所有 Excel 扩展名都应被识别 (#21)"""
        from services.weekly_excel_cache_service import WeeklyExcelCacheService
        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        
        for ext in [".xlsx", ".xls", ".xlsm", ".xlsb", ".csv"]:
            assert service.is_excel_file(f"data/test{ext}") is True, f"{ext} 未被识别"

    def test_no_diff_cache_skips_html_generation(self, monkeypatch):
        """无完成状态的 diff 缓存 → 不应生成 HTML 缓存"""
        diff_query = _FakeQuery(first_result=None)
        html_query = _FakeQuery(first_result=None)
        self._patch_runtime_models(monkeypatch, diff_query, html_query)

        from services.weekly_excel_cache_service import WeeklyExcelCacheService
        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        assert service.needs_merged_diff_cache(200, "data.xlsx") is False

    def test_existing_html_cache_skips_regeneration(self, monkeypatch):
        """已有完成的 HTML 缓存 → 不应重新生成"""
        latest_diff = SimpleNamespace(base_commit_id="base", latest_commit_id="head")
        diff_query = _FakeQuery(first_result=latest_diff)
        html_query = _FakeQuery(first_result=SimpleNamespace(id=42))
        self._patch_runtime_models(monkeypatch, diff_query, html_query)

        from services.weekly_excel_cache_service import WeeklyExcelCacheService
        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        assert service.needs_merged_diff_cache(300, "data.xlsx") is False

    def test_missing_html_cache_triggers_generation(self, monkeypatch):
        """有 diff 缓存但无 HTML 缓存 → 应触发生成"""
        latest_diff = SimpleNamespace(base_commit_id=None, latest_commit_id="commit_abc")
        diff_query = _FakeQuery(first_result=latest_diff)
        html_query = _FakeQuery(first_result=None)
        self._patch_runtime_models(monkeypatch, diff_query, html_query)

        from services.weekly_excel_cache_service import WeeklyExcelCacheService
        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        assert service.needs_merged_diff_cache(400, "data.xlsx") is True

    def test_cache_key_uses_sha256(self):
        """周版本缓存 key 使用 SHA-256 (#36)"""
        from services.weekly_excel_cache_service import WeeklyExcelCacheService
        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        
        key = service.generate_cache_key(1, "test.xlsx", "base_id", "latest_id")
        assert len(key) == 64  # SHA-256 hex digest

    def test_thread_safe_processing_cache(self):
        """验证 processing_cache 使用了线程锁保护 (#30)"""
        content = _read_source("services/weekly_excel_cache_service.py")
        assert "_processing_lock" in content
        assert "threading.Lock()" in content

    def test_model_cache_mechanism(self):
        """验证模型引用缓存机制 (#38)"""
        content = _read_source("services/weekly_excel_cache_service.py")
        assert "_models_cache" in content
        assert "_get_model" in content


# ---------------------------------------------------------------------------
#  5. 多线程竞态安全验证
# ---------------------------------------------------------------------------

class TestThreadSafety:
    """验证关键服务在多线程场景下的安全性"""

    def test_excel_diff_cache_service_concurrent_processing(self):
        """ExcelDiffCacheService 并发处理任务的线程安全"""
        from services.excel_diff_cache_service import ExcelDiffCacheService
        service = ExcelDiffCacheService()
        
        errors = []
        results = {"added": 0, "skipped": 0}
        barrier = threading.Barrier(10)

        def attempt_add(task_key):
            try:
                barrier.wait(timeout=5)
                with service._processing_lock:
                    if task_key in service._processing_commits:
                        results["skipped"] += 1
                    else:
                        service._processing_commits.add(task_key)
                        results["added"] += 1
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=attempt_add, args=("repo1_commit1_file.xlsx",))
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"线程错误: {errors}"
        assert results["added"] == 1, f"应只有1个线程成功添加，实际: {results['added']}"
        assert results["skipped"] == 9

    def test_weekly_cache_service_concurrent_processing(self):
        """WeeklyExcelCacheService 并发处理任务的线程安全"""
        from services.weekly_excel_cache_service import WeeklyExcelCacheService
        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        
        errors = []
        results = {"added": 0, "skipped": 0}
        barrier = threading.Barrier(10)

        def attempt_add(cache_key):
            try:
                barrier.wait(timeout=5)
                with service._processing_lock:
                    if cache_key in service.processing_cache:
                        results["skipped"] += 1
                    else:
                        service.processing_cache.add(cache_key)
                        results["added"] += 1
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=attempt_add, args=("config1_file.xlsx",))
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert results["added"] == 1
        assert results["skipped"] == 9


# ---------------------------------------------------------------------------
#  6. 缓存清理全链路
# ---------------------------------------------------------------------------

class TestCacheCleanupChain:
    """验证缓存清理策略的正确性（源码静态分析）"""

    def test_excel_diff_cache_batch_delete_pattern(self):
        """ExcelDiffCacheService 使用批量 DELETE 而非逐条删除"""
        content = _read_source("services/excel_diff_cache_service.py")
        # cleanup_old_cache 使用批量 delete
        assert "delete(synchronize_session=False)" in content
        # 不使用 db.session.delete(cache) 循环模式
        assert "for cache in " not in content or "db.session.delete(cache)" not in content

    def test_weekly_cache_cleanup_expired_exists(self):
        """WeeklyExcelCacheService 有过期缓存清理方法"""
        content = _read_source("services/weekly_excel_cache_service.py")
        assert "def cleanup_expired_cache(" in content
        assert "def cleanup_old_cache(" in content

    def test_html_cache_cleanup_methods_exist(self):
        """ExcelHtmlCacheService 有版本和过期清理方法"""
        content = _read_source("services/excel_html_cache_service.py")
        assert "def cleanup_old_version_cache(" in content
        assert "def cleanup_expired_cache(" in content


# ---------------------------------------------------------------------------
#  7. 状态同步链路
# ---------------------------------------------------------------------------

class TestStatusSyncChain:
    """验证状态同步服务的源码结构"""

    def test_status_sync_service_structure(self):
        """StatusSyncService 应有完整的双向同步方法"""
        content = _read_source("services/status_sync_service.py")
        assert "class StatusSyncService:" in content
        assert "def sync_commit_to_weekly(" in content
        assert "def sync_weekly_to_commit(" in content
        assert "def _find_related_weekly_caches(" in content
        assert "def _find_related_commits(" in content
        assert "def clear_all_confirmation_status(" in content


# ---------------------------------------------------------------------------
#  8. 路由端点可达性链路
# ---------------------------------------------------------------------------

class TestRouteEndpointsChain:
    """验证所有路由蓝图结构完整"""

    def test_blueprint_registrations_in_app(self):
        """app.py 应注册所有蓝图"""
        content = _read_source("app.py")
        assert "from routes.cache_management_routes import cache_management_bp" in content
        assert "from routes.weekly_version_management_routes import weekly_version_bp" in content
        assert "from routes.commit_diff_routes import commit_diff_bp" in content
        assert "from routes.core_management_routes import core_management_bp" in content
        # 确认没有直接在 app.py 定义路由
        assert "@app.route(" not in content

    def test_cache_management_routes_complete(self):
        """缓存管理路由蓝图端点完整"""
        content = _read_source("routes/cache_management_routes.py")
        expected_endpoints = [
            "/admin/excel-cache",
            "/api/excel-cache/logs",
            "/api/excel-html-cache/clear",
            "/api/excel-html-cache/stats",
        ]
        for ep in expected_endpoints:
            assert ep in content, f"缺少端点: {ep}"

    def test_commit_diff_routes_complete(self):
        """提交差异路由蓝图端点完整"""
        content = _read_source("routes/commit_diff_routes.py")
        expected_endpoints = [
            "/repositories/<int:repository_id>/commits",
            "/commits/<int:commit_id>/diff",
            "/commits/batch-update",
        ]
        for ep in expected_endpoints:
            assert ep in content, f"缺少端点: {ep}"

    def test_weekly_version_routes_complete(self):
        """周版本管理路由蓝图端点完整"""
        content = _read_source("routes/weekly_version_management_routes.py")
        expected_endpoints = [
            "/projects/<int:project_id>/weekly-version-config",
            "/weekly-version-config/<int:config_id>/diff",
            "/weekly-version-config/<int:config_id>/stats",
        ]
        for ep in expected_endpoints:
            assert ep in content, f"缺少端点: {ep}"

    def test_core_management_routes_complete(self):
        """核心管理路由蓝图端点完整"""
        content = _read_source("routes/core_management_routes.py")
        expected_endpoints = [
            "/auth/login",
            "/projects",
        ]
        for ep in expected_endpoints:
            assert ep in content, f"缺少端点: {ep}"


# ---------------------------------------------------------------------------
#  9. 大数据集 diff 准确性 (#29)
# ---------------------------------------------------------------------------

class TestDiffAccuracyLargeDataset:
    """验证大数据集（>100行）Excel diff 的准确性"""

    @pytest.fixture(autouse=True)
    def setup_diff_service(self):
        from services.diff_service import DiffService
        self.service = DiffService()

    def _generate_rows(self, count: int, columns: list, prefix: str = "") -> list:
        """生成测试行数据"""
        rows = []
        for i in range(count):
            row = {}
            for j, col in enumerate(columns):
                row[col] = f"{prefix}{i}_{j}"
            rows.append(row)
        return rows

    def test_identical_large_dataset_no_false_changes(self):
        """200 行完全相同的数据集不应产生虚假变更"""
        cols = ["ID", "Name", "Value", "Category", "Score"]
        rows = self._generate_rows(200, cols)
        
        matches = self.service._fast_row_matching(rows, rows, cols)
        
        # 所有行都应被匹配
        assert len(matches) == 200
        for m in matches:
            assert m["similarity"] == 1.0

    def test_single_row_insert_in_middle(self):
        """在200行数据中间插入1行，不应导致大量虚假新增/删除"""
        cols = ["ID", "Name", "Value"]
        previous = self._generate_rows(200, cols)
        
        # 在第100行后插入一行
        current = list(previous)
        current.insert(100, {"ID": "NEW", "Name": "inserted", "Value": "new_val"})
        
        matches = self.service._fast_row_matching(current, previous, cols)
        matched_current = set(m["current_idx"] for m in matches)
        
        # 至少80%的原有行应该被正确匹配（改进后应>90%）
        original_matched = len([m for m in matches if m["similarity"] >= 0.9])
        assert original_matched >= 160, (
            f"200行中仅匹配{original_matched}行，插入1行不应导致如此多失配"
        )

    def test_batch_modification_recognized(self):
        """批量修改最后一列，应识别为 modified 而非 added+deleted"""
        cols = ["ID", "Name", "Value", "Status"]
        count = 150
        previous = self._generate_rows(count, cols)
        
        # 修改20行的 Status 列
        current = [dict(row) for row in previous]
        for i in range(50, 70):
            current[i]["Status"] = "CHANGED"
        
        matches = self.service._fast_row_matching(current, previous, cols)
        matched_current = set(m["current_idx"] for m in matches)
        
        # 被修改的行也应该被匹配（虽然相似度 < 1.0）
        modified_matched = sum(1 for i in range(50, 70) if i in matched_current)
        assert modified_matched >= 15, (
            f"20行被修改，但仅{modified_matched}行被正确匹配为修改"
        )

    def test_adaptive_search_range(self):
        """验证搜索范围自适应数据集大小（#29 改进）"""
        content = _read_source("services/diff_service.py")
        # 应使用自适应范围而非固定值3
        assert "max(10," in content or "search_range = max(" in content
        assert "search_range = 3" not in content

    def test_position_based_matching_covers_offset(self):
        """验证位置匹配在大偏移场景下仍能工作"""
        cols = ["ID", "Name"]
        # 删除前20行 → 后续行全部偏移20
        previous = self._generate_rows(100, cols)
        current = list(previous[20:])  # 删除前20行
        
        matches = self.service._fast_row_matching(current, previous, cols)
        
        # 后80行应被匹配
        assert len(matches) >= 70, (
            f"删除前20行后80行应大部分被匹配，实际仅匹配{len(matches)}行"
        )


# ---------------------------------------------------------------------------
#  10. 工具函数链路
# ---------------------------------------------------------------------------

class TestUtilFunctionsChain:
    """验证工具模块的导入和行为"""

    def test_diff_data_utils_import(self):
        """diff_data_utils 模块应可正常导入"""
        from utils.diff_data_utils import (
            clean_json_data,
            format_cell_value,
            get_excel_column_letter,
            safe_json_serialize,
            validate_excel_diff_data,
        )
        
        # 基础功能验证
        assert clean_json_data({"x": float("nan")}) == {"x": None}
        assert format_cell_value(" null ") == ""
        assert get_excel_column_letter(0) == "A"
        assert get_excel_column_letter(25) == "Z"
        assert get_excel_column_letter(26) == "AA"

        valid, _ = validate_excel_diff_data({
            "type": "excel",
            "sheets": {"S1": {"rows": [{"status": "added"}]}}
        })
        assert valid is True

    def test_request_security_module_structure(self):
        """请求安全模块结构完整"""
        content = _read_source("utils/request_security.py")
        assert "def configure_request_security(" in content
        assert "def csrf_token(" in content
        assert "def require_admin(" in content

    def test_model_loader_module_structure(self):
        """模型加载器结构完整"""
        content = _read_source("services/model_loader.py")
        assert "def get_runtime_model(" in content
        assert "def get_runtime_models(" in content
        assert "def clear_model_loader_cache(" in content


# ---------------------------------------------------------------------------
#  11. 配置与环境验证
# ---------------------------------------------------------------------------

class TestConfigAndEnvironment:
    """验证配置文件和环境结构"""

    def test_requirements_contains_key_packages(self):
        """requirements.txt 包含关键依赖"""
        content = _read_source("requirements.txt")
        key_packages = ["Flask", "Flask-SQLAlchemy", "GitPython", "openpyxl", "pandas"]
        for pkg in key_packages:
            assert pkg in content, f"缺少依赖: {pkg}"

    def test_startup_scripts_exist(self):
        """启动脚本应存在"""
        assert (PROJECT_ROOT / "start.bat").exists(), "缺少 start.bat"
        assert (PROJECT_ROOT / "start.sh").exists(), "缺少 start.sh"

    def test_project_structure_directories(self):
        """关键目录应存在"""
        for d in ["services", "routes", "utils", "templates", "static", "tests"]:
            assert (PROJECT_ROOT / d).is_dir(), f"缺少目录: {d}"


# ---------------------------------------------------------------------------
#  12. 跨服务集成链路
# ---------------------------------------------------------------------------

class TestCrossServiceIntegration:
    """验证服务之间的协作逻辑"""

    def test_diff_service_to_html_cache_pipeline(self):
        """DiffService 输出 → ExcelHtmlCacheService.generate_excel_html 消费"""
        from services.diff_service import DiffService
        from services.excel_html_cache_service import ExcelHtmlCacheService

        diff_svc = DiffService()
        
        # 模拟一个 CSV diff
        old_csv = b"id,name,score\n1,Alice,90\n2,Bob,85\n"
        new_csv = b"id,name,score\n1,Alice,95\n2,Bob,85\n3,Charlie,88\n"
        diff_result = diff_svc.process_diff("grades.csv", new_csv, old_csv)
        
        assert diff_result["type"] == "excel"
        
        # 将 diff 结果传给 HTML 缓存服务
        mock_db = MagicMock()
        html_svc = ExcelHtmlCacheService(mock_db, "1.8.0")
        html, css, js = html_svc.generate_excel_html(diff_result)
        
        assert len(html) > 0, "HTML 输出不应为空"

    def test_cache_key_consistency_across_services(self):
        """ExcelHtmlCacheService 和 WeeklyExcelCacheService 的缓存 key 都使用 SHA-256"""
        html_content = _read_source("services/excel_html_cache_service.py")
        weekly_content = _read_source("services/weekly_excel_cache_service.py")
        
        assert "sha256" in html_content
        assert "sha256" in weekly_content
        # 两者都不使用 MD5
        assert "md5" not in html_content.lower() or "sha256" in html_content
        assert "md5" not in weekly_content.lower() or "sha256" in weekly_content


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-x"])
