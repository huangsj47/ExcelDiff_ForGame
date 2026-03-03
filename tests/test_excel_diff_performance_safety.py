from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _function_body(content: str, func_name: str) -> str:
    start = content.find(f"def {func_name}(")
    assert start >= 0, f"function not found: {func_name}"
    end = content.find("\ndef ", start + 1)
    if end < 0:
        end = len(content)
    return content[start:end]


def test_excel_task_dedup_includes_processing_status():
    content = _read("services/task_worker_service.py")
    single_body = _function_body(content, "add_excel_diff_task")
    batch_body = _function_body(content, "add_excel_diff_tasks_batch")

    assert "status.in_(['pending', 'processing'])" in single_body
    assert "status.in_(['pending', 'processing'])" in batch_body


def test_excel_task_enqueue_cooldown_exists_and_is_used():
    content = _read("services/task_worker_service.py")

    assert "EXCEL_TASK_ENQUEUE_COOLDOWN_SECONDS" in content
    assert "def _is_excel_task_cooling_down(" in content
    assert "def _mark_excel_task_cooldown(" in content

    single_body = _function_body(content, "add_excel_diff_task")
    batch_body = _function_body(content, "add_excel_diff_tasks_batch")

    assert "_is_excel_task_cooling_down(" in single_body
    assert "_mark_excel_task_cooldown(" in single_body
    assert "bypass_cooldown = priority <= 3" in single_body
    assert "if cooling_down and not bypass_cooldown:" in single_body
    assert "_db.session.flush()" in single_body
    assert "_is_excel_task_cooling_down(" in batch_body
    assert "incoming_seen_tasks = set()" in batch_body


def test_save_cached_diff_centralizes_optimize_diff_data():
    content = _read("services/excel_diff_cache_service.py")
    body = _function_body(content, "save_cached_diff")

    assert "normalized_diff_data = self.optimize_diff_data(diff_data)" in body
    assert "diff_json = json.dumps(normalized_diff_data)" in body


def test_background_excel_previous_commit_query_has_time_and_id_tiebreak():
    content = _read("services/excel_diff_cache_service.py")
    body = _function_body(content, "process_excel_diff_background")

    assert "Commit.commit_time == commit.commit_time" in body
    assert "Commit.id < commit.id" in body


def test_diff_service_uses_dataframe_bulk_conversion_path():
    content = _read("services/diff_service.py")

    assert "def _dataframe_rows_with_index(" in content
    smart_body = _function_body(content, "_smart_row_diff")
    assert "self._dataframe_rows_with_index(current_df)" in smart_body
    assert "self._dataframe_rows_with_index(previous_df)" in smart_body
    assert "rows_equal = False" in smart_body
    assert "if len(current_rows) == len(previous_rows):" in smart_body
    assert "self._rows_equal(" in smart_body


def test_position_matcher_removes_dead_offset_code():
    content = _read("services/diff_service.py")
    body = _function_body(content, "_find_position_based_matches")

    assert "offset_sum = 0" not in body
    assert "for m_item in []:" not in body
    assert "existing_offsets = []" not in body
