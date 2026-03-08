from pathlib import Path
from types import SimpleNamespace

from services.api_response_service import (
    build_error_payload,
    build_success_payload,
    json_error,
    json_success,
)
from services.git_diff_helpers import generate_basic_diff, parse_unified_diff
from services.weekly_deleted_excel_helpers import (
    is_deleted_operation,
    resolve_weekly_deleted_excel_state,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_api_response_error_payload_contract_fields():
    payload = build_error_payload(
        message="bad request",
        error_type="invalid_request",
        retry_after_seconds=30,
    )
    assert payload["status"] == "error"
    assert payload["message"] == "bad request"
    assert payload["error_type"] == "invalid_request"
    assert payload["retry_after_seconds"] == 30
    assert payload["success"] is False


def test_api_response_success_payload_contract_fields():
    payload = build_success_payload(
        message="ok",
        status="ready",
        retry_after_seconds=10,
        pending=True,
    )
    assert payload["status"] == "ready"
    assert payload["message"] == "ok"
    assert payload["retry_after_seconds"] == 10
    assert payload["success"] is True
    assert payload["pending"] is True


def test_json_error_and_success_helpers_return_tuple():
    ok_resp, ok_code = json_success(
        jsonify=lambda data: data,
        message="done",
        status="ready",
        http_status=202,
        task_id=9,
    )
    assert ok_code == 202
    assert ok_resp["status"] == "ready"
    assert ok_resp["task_id"] == 9

    err_resp, err_code = json_error(
        jsonify=lambda data: data,
        message="boom",
        error_type="runtime_error",
        http_status=500,
    )
    assert err_code == 500
    assert err_resp["status"] == "error"
    assert err_resp["error_type"] == "runtime_error"


def test_git_diff_helpers_basic_parse_and_generate():
    patch = "\n".join(
        [
            "@@ -1,2 +1,2 @@",
            "-old_line",
            "+new_line",
            " keep",
        ]
    )
    hunks = parse_unified_diff(patch)
    assert len(hunks) == 1
    assert hunks[0]["old_start"] == 1
    assert hunks[0]["new_start"] == 1
    assert [line["type"] for line in hunks[0]["lines"]] == ["removed", "added", "context"]

    diff_data = generate_basic_diff("a\nb\n", "a\nc\n", "demo.txt")
    assert diff_data is not None
    assert diff_data["type"] == "code"
    assert diff_data["file_path"] == "demo.txt"
    assert isinstance(diff_data.get("hunks"), list)


def test_weekly_deleted_excel_state_fallback_from_cached_operations():
    fake_commit_model = SimpleNamespace(query=None)
    config = SimpleNamespace(repository_id=None, repository=SimpleNamespace(id=None))
    diff_cache = SimpleNamespace(
        latest_commit_id=None,
        base_commit_id="base-001",
        merged_diff_data='{"operations":["M","D"],"commit_ids":["c1","c2"]}',
    )
    is_deleted, previous_commit_id = resolve_weekly_deleted_excel_state(
        commit_model=fake_commit_model,
        config=config,
        diff_cache=diff_cache,
        file_path="a.xlsx",
    )
    assert is_deleted is True
    assert previous_commit_id == "c1"
    assert is_deleted_operation("deleted") is True


def test_services_use_unified_response_contract_and_split_helpers():
    excel_content = _read("services/excel_diff_api_service.py")
    commit_page_content = _read("services/commit_diff_page_service.py")
    commit_ops_content = _read("services/commit_operation_handlers.py")
    git_content = _read("services/git_service.py")
    weekly_content = _read("services/weekly_version_logic.py")

    assert "from services.api_response_service import json_error, json_success" in excel_content
    assert "from services.api_response_service import json_error, json_success" in commit_page_content
    assert "from services.api_response_service import json_error, json_success" in commit_ops_content
    assert "error_type" in excel_content
    assert "retry_after_seconds" in excel_content

    assert "from services.git_diff_helpers import (" in git_content
    assert "return parse_unified_diff(patch_text)" in git_content
    assert "return generate_basic_diff(previous_content, current_content, file_path)" in git_content

    assert "from services.weekly_deleted_excel_helpers import (" in weekly_content
    assert "return _resolve_weekly_deleted_excel_state_helper(" in weekly_content
    assert "return _render_weekly_deleted_excel_notice_helper(" in weekly_content
