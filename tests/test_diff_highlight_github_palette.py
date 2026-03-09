from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_weekly_full_diff_inline_highlight_uses_github_like_contrast():
    content = _read("templates/weekly_version_full_diff.html")
    assert "background: #abf2bc;" in content
    assert "background: #ffb8bd;" in content
    assert "color: #24292f;" in content
    assert "box-shadow: inset 0 0 0 1px rgba(27, 31, 36, 0.08);" in content


def test_shared_text_diff_styles_use_github_like_palette():
    content = _read("static/css/diff-styles.css")
    assert "background: #abf2bc;" in content
    assert "background: #ffb8bd;" in content
    assert "background: #dafbe1;" in content
    assert "background: #ffebe9;" in content
    assert "background: #ccf2d4;" in content
    assert "background: #ffd7d5;" in content


def test_git_diff_renderer_inline_palette_matches_github_like_contrast():
    content = _read("services/diff_render_helpers.py")
    assert "background: #abf2bc;" in content
    assert "background: #ffb8bd;" in content
    assert "color: #24292f;" in content
