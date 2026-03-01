from pathlib import Path
from types import SimpleNamespace

import services.weekly_excel_cache_service as weekly_cache_module
from services.weekly_excel_cache_service import WeeklyExcelCacheService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


class _SortableColumn:
    def desc(self):
        return self


class _FakeQuery:
    def __init__(self, first_result=None):
        self._first_result = first_result
        self.filter_calls = []
        self.order_calls = []

    def filter_by(self, **kwargs):
        self.filter_calls.append(kwargs)
        return self

    def order_by(self, *args, **kwargs):
        self.order_calls.append((args, kwargs))
        return self

    def first(self):
        return self._first_result


class TestExcelAndCacheStaticChecks:
    def test_excel_cache_logs_filters_source(self):
        content = _read("routes/cache_management_routes.py")
        assert "logs_query = OperationLog.query.filter_by(source=\"excel_cache\")" in content
        assert "total_logs_raw = logs_query.count()" in content
        assert "logs_query\n                .order_by(OperationLog.created_at.desc())" in content

    def test_weekly_cache_status_reset_uses_previous_latest_commit(self):
        content = _read("app.py")
        assert "previous_latest_commit_id = existing_cache.latest_commit_id" in content
        assert "if previous_latest_commit_id != latest_commit.commit_id:" in content

    def test_weekly_needs_cache_no_longer_always_true(self):
        content = _read("services/weekly_excel_cache_service.py")
        assert "Debug: Returning True for" not in content
        assert "WeeklyVersionDiffCache" in content
        assert "existing_cache is None" in content

    def test_excel_diff_cache_avoids_global_session_expire_all(self):
        content = _read("services/excel_diff_cache_service.py")
        assert "db.session.expire_all()" not in content
        assert ".populate_existing()" in content

    def test_excel_html_cache_stats_use_sql_aggregation(self):
        content = _read("services/excel_html_cache_service.py")
        assert "func.sum(" in content
        assert "func.length(func.coalesce(ExcelHtmlCache.html_content, ''))" in content
        assert "func.length(func.coalesce(ExcelHtmlCache.css_content, ''))" in content
        assert "func.length(func.coalesce(ExcelHtmlCache.js_content, ''))" in content
        assert "for cache in query.filter(ExcelHtmlCache.cache_status == 'completed').all():" not in content


class TestWeeklyExcelCacheServiceNeedsCache:
    def _patch_runtime_models(self, monkeypatch, diff_query, html_query):
        diff_model = type(
            "DiffModel",
            (),
            {
                "query": diff_query,
                "updated_at": _SortableColumn(),
            },
        )
        html_model = type("HtmlModel", (), {"query": html_query})

        def fake_get_runtime_models(*names):
            mapping = {
                "WeeklyVersionDiffCache": diff_model,
                "WeeklyVersionExcelCache": html_model,
            }
            return tuple(mapping[name] for name in names)

        monkeypatch.setattr(weekly_cache_module, "get_runtime_models", fake_get_runtime_models)

    def test_non_excel_file_never_requires_weekly_excel_cache(self):
        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        assert service.needs_merged_diff_cache(101, "foo/bar.txt") is False

    def test_weekly_excel_detection_covers_xlsm_and_xlsb(self):
        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        assert service.is_excel_file("foo/test.xlsm") is True
        assert service.is_excel_file("foo/test.xlsb") is True

    def test_no_completed_diff_cache_skips_weekly_excel_cache_task(self, monkeypatch):
        diff_query = _FakeQuery(first_result=None)
        html_query = _FakeQuery(first_result=None)
        self._patch_runtime_models(monkeypatch, diff_query, html_query)

        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        assert service.needs_merged_diff_cache(202, "foo/bar.xlsx") is False
        assert diff_query.filter_calls[0]["config_id"] == 202
        assert diff_query.filter_calls[0]["file_path"] == "foo/bar.xlsx"
        assert diff_query.filter_calls[0]["cache_status"] == "completed"
        assert len(html_query.filter_calls) == 0

    def test_missing_html_cache_requires_generation(self, monkeypatch):
        latest_diff = SimpleNamespace(base_commit_id=None, latest_commit_id="abc123")
        diff_query = _FakeQuery(first_result=latest_diff)
        html_query = _FakeQuery(first_result=None)
        self._patch_runtime_models(monkeypatch, diff_query, html_query)

        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        assert service.needs_merged_diff_cache(303, "foo/bar.xlsm") is True
        assert html_query.filter_calls[0]["base_commit_id"] == ""
        assert html_query.filter_calls[0]["latest_commit_id"] == "abc123"
        assert html_query.filter_calls[0]["diff_version"] == "1.8.0"
        assert html_query.filter_calls[0]["cache_status"] == "completed"

    def test_existing_html_cache_skips_regeneration(self, monkeypatch):
        latest_diff = SimpleNamespace(base_commit_id="base001", latest_commit_id="head001")
        diff_query = _FakeQuery(first_result=latest_diff)
        html_query = _FakeQuery(first_result=SimpleNamespace(id=1))
        self._patch_runtime_models(monkeypatch, diff_query, html_query)

        service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
        assert service.needs_merged_diff_cache(404, "foo/bar.csv") is False
