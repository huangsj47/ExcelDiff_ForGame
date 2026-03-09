from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_merged_view_template_contains_pass_rate_slot_and_logic():
    content = _read("templates/merged_project_view.html")
    assert 'id="passRate-{{ config.id }}"' in content
    assert 'id="passRate-${config.id}"' in content
    assert "function calculatePassRate(totalFiles, confirmedCount)" in content
    assert "function updatePassRateDisplay(configId, passRateValue)" in content
    assert "通过率：" in content


def test_merged_view_template_updates_pass_rate_when_stats_loaded():
    content = _read("templates/merged_project_view.html")
    assert "const passRateValue = calculatePassRate(totalFiles, confirmedCount);" in content
    assert "updatePassRateDisplay(configId, passRateValue);" in content
    assert "pass-rate-high" in content
    assert "pass-rate-mid" in content
    assert "pass-rate-low" in content


def test_merged_view_template_highlights_time_range_and_shows_today_date():
    content = _read("templates/merged_project_view.html")
    assert "version-time-range-highlight" in content
    assert "周版本时间范围：" in content
    assert "version-today-badge" in content
    assert "今天：<strong>{{ today_date }}</strong>" in content
    assert "const todayDateLabel = {{ today_date | tojson }};" in content
