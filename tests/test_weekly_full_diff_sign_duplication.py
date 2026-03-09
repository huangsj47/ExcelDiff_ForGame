from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_github_diff_renderer_disables_global_before_sign_duplication():
    content = _read("services/diff_render_helpers.py")
    assert ".diff-container .diff-line-content::before" in content
    assert "content: none !important;" in content
