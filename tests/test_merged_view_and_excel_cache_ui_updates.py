from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_merged_view_backend_passes_today_date_to_template():
    content = _read("services/weekly_version_logic.py")
    assert 'today_date = now.strftime("%Y-%m-%d")' in content
    assert "today_date=today_date," in content


def test_excel_cache_template_hides_html_stats_and_moves_agent_card():
    content = _read("templates/excel_cache_management.html")
    assert "hidden-html-cache-card" in content
    assert ".hidden-html-cache-card {" in content
    assert "display: none !important;" in content
    assert ".agent-temp-cache-card { order: 4; }" in content
