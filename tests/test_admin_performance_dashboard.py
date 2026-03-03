from pathlib import Path

from services.performance_metrics_service import PerformanceMetricsService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_performance_dashboard_routes_exist():
    content = _read("routes/cache_management_routes.py")
    assert '"/admin/performance"' in content
    assert '"/admin/performance/stats"' in content
    assert '"/admin/performance/reset"' in content
    assert 'render_template("admin_performance_dashboard.html")' in content


def test_performance_dashboard_template_wires_stats_api():
    content = _read("templates/admin_performance_dashboard.html")
    assert "performance/stats" in content
    assert "performance/reset" in content
    assert "pipelineTableBody" in content
    assert "timelineContainer" in content
    assert "recentEventsBody" in content
    assert "recentPageSizeSelect" in content
    assert "recentPrevBtn" in content
    assert "recentNextBtn" in content
    assert "PIPELINE_LABELS" in content
    assert "state.recentLimit" in content


def test_performance_metrics_service_snapshot_aggregates_core_fields():
    service = PerformanceMetricsService(max_events=20)
    service.record(
        "api_excel_diff",
        success=True,
        metrics={"total_ms": 120, "render_ms": 50},
        tags={"source": "realtime"},
    )
    service.record(
        "api_excel_diff",
        success=False,
        metrics={"total_ms": 200},
        tags={"source": "exception"},
    )
    service.record(
        "weekly_excel_cache",
        success=True,
        metrics={"total_ms": 320, "save_ms": 80},
        tags={"source": "generated"},
    )

    snapshot = service.snapshot(window_minutes=60, recent_limit=20)

    assert snapshot["total_events"] == 3
    assert snapshot["success_count"] == 2
    assert snapshot["failed_count"] == 1
    assert snapshot["avg_total_ms"] > 0
    assert snapshot["p95_total_ms"] >= snapshot["avg_total_ms"]
    assert len(snapshot["pipeline_stats"]) >= 2
    assert len(snapshot["recent_events"]) == 3


def test_performance_metrics_service_recent_limit_clamps_to_500():
    service = PerformanceMetricsService(max_events=1200)
    for idx in range(900):
        service.record(
            "background_excel_cache",
            success=(idx % 5 != 0),
            metrics={"total_ms": 10 + idx},
            tags={"source": "background_excel", "index": idx},
        )

    snapshot = service.snapshot(window_minutes=60 * 24, recent_limit=9999)

    assert len(snapshot["recent_events"]) == 500
