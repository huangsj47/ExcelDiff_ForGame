from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_weekly_diff_frontend_notifies_stats_recalculation_after_status_updates():
    content = _read("templates/weekly_version_diff.html")
    assert "const WEEKLY_STATS_SYNC_EVENT_KEY = 'weeklyVersionStatsChanged';" in content
    assert "function notifyWeeklyVersionStatsChanged(currentConfigId)" in content
    assert "localStorage.setItem(" in content
    assert "notifyWeeklyVersionStatsChanged(configId);" in content
