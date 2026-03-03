from types import SimpleNamespace

import services.excel_html_cache_service as html_cache_module
import services.weekly_excel_cache_service as weekly_cache_module
from services.excel_html_cache_service import ExcelHtmlCacheService
from services.weekly_excel_cache_service import WeeklyExcelCacheService


class _DummyWeeklyModel:
    query = None


class _DummyHtmlModel:
    query = None


def test_weekly_get_model_single_name_unwraps_tuple(monkeypatch):
    def fake_get_runtime_models(*names):
        assert names == ("WeeklyVersionExcelCache",)
        return (_DummyWeeklyModel,)

    monkeypatch.setattr(weekly_cache_module, "get_runtime_models", fake_get_runtime_models)
    service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")

    model = service._get_model("WeeklyVersionExcelCache")

    assert model is _DummyWeeklyModel
    assert not isinstance(model, tuple)


def test_excel_html_get_model_single_name_unwraps_tuple(monkeypatch):
    def fake_get_runtime_models(*names):
        assert names == ("ExcelHtmlCache",)
        return (_DummyHtmlModel,)

    monkeypatch.setattr(html_cache_module, "get_runtime_models", fake_get_runtime_models)
    service = ExcelHtmlCacheService(SimpleNamespace(session=None), "1.8.0")

    model = service._get_model("ExcelHtmlCache")

    assert model is _DummyHtmlModel
    assert not isinstance(model, tuple)


def test_weekly_cache_stats_logs_real_exception(monkeypatch):
    service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.8.0")
    captured = []

    def fake_get_model(*_names):
        raise RuntimeError("weekly stats boom")

    monkeypatch.setattr(service, "_get_model", fake_get_model)
    monkeypatch.setattr(
        service,
        "_log_exception",
        lambda context, exc, category="WEEKLY": captured.append((context, str(exc), category)),
    )

    stats = service.get_cache_stats()

    assert stats["total_count"] == 0
    assert captured
    assert "weekly stats boom" in captured[0][1]


def test_html_cache_stats_logs_real_exception(monkeypatch):
    service = ExcelHtmlCacheService(SimpleNamespace(session=None), "1.8.0")
    captured = []

    def fake_get_model(*_names):
        raise RuntimeError("html stats boom")

    monkeypatch.setattr(service, "_get_model", fake_get_model)
    monkeypatch.setattr(
        service,
        "_log_exception",
        lambda context, exc, category="CACHE": captured.append((context, str(exc), category)),
    )

    stats = service.get_cache_statistics()

    assert stats["total_count"] == 0
    assert captured
    assert "html stats boom" in captured[0][1]
