from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_commit_list_batch_buttons_require_two_or_more_selected():
    content = _read("templates/commit_list.html")
    assert "if (checkboxes.length >= 2)" in content
    assert "if (commitIds.length < 2)" in content


def test_commit_list_plain_click_can_deselect_single_selected_row():
    content = _read("templates/commit_list.html")
    assert "if (clickedIsSelected && selectedRows.length === 1)" in content
    assert "checkbox.checked = false;" in content


def test_repository_compare_modal_uses_repo_branch_label_and_no_interval_setting():
    content = _read("templates/commit_list.html")
    assert "({{ _repo_branch_label }}标签)" in content
    assert "data-repo-type=\"{{ repo.type|lower }}\"" in content
    assert "id=\"intervalMinutes\"" not in content
    assert "name=\"interval_minutes\"" not in content
    assert "&interval=${intervalMinutes}" not in content


def test_git_commit_range_diff_debug_logs_are_env_gated():
    content = _read("services/git_service.py")
    start = content.index("def get_commit_range_diff")
    end = content.index("def get_commit_info", start)
    method_body = content[start:end]

    assert "VERBOSE_GIT_DIFF_RANGE_LOGS" in method_body
    assert "debug_enabled = verbose_flag in {\"1\", \"true\", \"yes\", \"on\"}" in method_body
    assert "def debug_log(message):" in method_body
    assert re.search(r"(?m)^\s*print\(", method_body) is None
