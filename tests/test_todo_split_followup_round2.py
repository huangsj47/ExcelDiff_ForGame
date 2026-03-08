from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_git_service_excel_parser_logic_extracted():
    content = _read("services/git_service.py")
    assert "from services.git_excel_parser_helpers import (" in content
    assert "def parse_excel_diff(self, commit_id, file_path):" in content
    assert "return parse_excel_diff(self, commit_id, file_path)" in content
    assert "def _extract_excel_data(self, commit, file_path):" in content
    assert "return extract_excel_data(self, commit, file_path)" in content
    assert "def _generate_excel_diff_data(self, current_data, previous_data, file_path):" in content
    assert "return generate_excel_diff_data(self, current_data, previous_data, file_path)" in content


def test_weekly_excel_merge_helpers_extracted():
    content = _read("services/weekly_version_logic.py")
    assert "from services.weekly_excel_merge_helpers import (" in content
    assert "def _merge_segmented_excel_diff_payload(segment_payloads):" in content
    assert "return _merge_segmented_excel_diff_payload_helper(segment_payloads)" in content
    assert "def _extract_excel_diff_from_payload(payload):" in content
    assert "return _extract_excel_diff_from_payload_helper(payload)" in content
    assert "def _load_weekly_excel_diff_from_cache(repository, diff_cache, file_path):" in content
    assert "return _load_weekly_excel_diff_from_cache_helper(" in content


def test_git_and_weekly_service_file_length_budget_round2():
    git_lines = _read("services/git_service.py").splitlines()
    weekly_lines = _read("services/weekly_version_logic.py").splitlines()
    assert len(git_lines) < 1800
    assert len(weekly_lines) < 1800
