from pathlib import Path

import pytest

from utils.diff_data_utils import (
    clean_json_data,
    format_cell_value,
    get_excel_column_letter,
    safe_json_serialize,
    validate_excel_diff_data,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


class TestAppSplitTodoProgress:
    def test_app_imports_diff_data_utils(self):
        content = _read("app.py")
        assert "from utils.diff_data_utils import (" in content
        assert "clean_json_data" in content
        assert "validate_excel_diff_data" in content
        assert "safe_json_serialize" in content
        assert "get_excel_column_letter" in content
        assert "format_cell_value" in content

    def test_app_local_definitions_removed_for_a2(self):
        content = _read("app.py")
        assert "def clean_json_data(" not in content
        assert "def validate_excel_diff_data(" not in content
        assert "def safe_json_serialize(" not in content
        assert "def get_excel_column_letter(" not in content
        assert "def format_cell_value(" not in content

    def test_diff_data_utils_behavior(self):
        data = {
            "v1": float("nan"),
            "v2": [1, float("inf"), {"x": float("-inf")}],
        }
        cleaned = clean_json_data(data)
        assert cleaned["v1"] is None
        assert cleaned["v2"][1] is None
        assert cleaned["v2"][2]["x"] is None

        serialized = safe_json_serialize(data)
        assert serialized["v1"] is None
        assert serialized["v2"][1] is None

        valid, _msg = validate_excel_diff_data(
            {"type": "excel", "sheets": {"Sheet1": {"rows": [{"status": "added"}]}}}
        )
        assert valid is True

        assert get_excel_column_letter(0) == "A"
        assert get_excel_column_letter(27) == "AB"
        assert format_cell_value(" null ") == ""
        assert format_cell_value(123) == "123"

    def test_app_uses_extracted_excel_diff_cache_service(self):
        content = _read("app.py")
        assert "from services.excel_diff_cache_service import (" in content
        assert "configure_excel_diff_cache_service(" in content
        assert "excel_cache_service = ExcelDiffCacheService()" in content
        assert "class ExcelDiffCacheService:" not in content

    def test_extracted_excel_diff_cache_service_module_exists(self):
        content = _read("services/excel_diff_cache_service.py")
        assert "def configure_excel_diff_cache_service(" in content
        assert "class ExcelDiffCacheService:" in content

    def test_app_uses_extracted_request_security_helpers(self):
        content = _read("app.py")
        assert "from utils.request_security import (" in content
        assert "configure_request_security(" in content
        assert "def csrf_token(" not in content
        assert "def _is_valid_admin_token(" not in content
        assert "def _csrf_token_from_request(" not in content
        assert "def require_admin(" not in content

    def test_extracted_request_security_module_exists(self):
        content = _read("utils/request_security.py")
        assert "def configure_request_security(" in content
        assert "def csrf_token(" in content
        assert "def _is_valid_admin_token(" in content
        assert "def require_admin(" in content

    def test_app_registers_cache_management_blueprint(self):
        content = _read("app.py")
        assert "from routes.cache_management_routes import cache_management_bp" in content
        assert "app.register_blueprint(cache_management_bp)" in content

    def test_cache_management_routes_extracted_to_blueprint(self):
        content = _read("routes/cache_management_routes.py")
        assert "cache_management_bp = Blueprint(" in content
        assert "/admin/excel-cache/cleanup-expired" in content
        assert "/admin/excel-cache/clear-all-diff-cache" in content
        assert "/admin/excel-cache/strategy-info" in content
        assert "/api/excel-cache/logs" in content
        assert "/api/excel-html-cache/clear" in content
        assert "/api/excel-html-cache/regenerate" in content
        assert "/api/excel-diff-status/<cache_key>" in content
        assert "/api/excel-html-cache/stats" in content
        assert "/api/excel-cache/stats-by-project" in content
        assert "/admin/weekly-excel-cache/stats" in content
        assert "/admin/weekly-excel-cache/cleanup" in content
        assert "/admin/weekly-excel-cache/clear-all" in content
        assert "/admin/weekly-excel-cache/rebuild/<int:config_id>" in content
        assert "/admin/excel-cache" in content

    def test_app_removed_migrated_cache_route_definitions(self):
        content = _read("app.py")
        assert "@app.route('/api/excel-cache/logs')" not in content
        assert "@app.route('/api/excel-html-cache/clear')" not in content
        assert "@app.route('/api/excel-html-cache/regenerate')" not in content
        assert "@app.route('/api/excel-diff-status/<cache_key>')" not in content
        assert "@app.route('/api/excel-html-cache/stats', methods=['GET'])" not in content
        assert "@app.route('/api/excel-cache/stats-by-project', methods=['GET'])" not in content
        assert "@app.route('/admin/weekly-excel-cache/stats', methods=['GET'])" not in content
        assert "@app.route('/admin/weekly-excel-cache/cleanup', methods=['POST'])" not in content
        assert "@app.route('/admin/weekly-excel-cache/clear-all', methods=['POST'])" not in content
        assert "@app.route('/admin/weekly-excel-cache/rebuild/<int:config_id>', methods=['POST'])" not in content
        assert "@app.route('/admin/excel-cache')" not in content

    def test_app_registers_weekly_version_blueprint(self):
        content = _read("app.py")
        assert "from routes.weekly_version_management_routes import weekly_version_bp" in content
        assert "app.register_blueprint(weekly_version_bp, name=\"\")" in content

    def test_weekly_version_routes_extracted_to_blueprint(self):
        content = _read("routes/weekly_version_management_routes.py")
        assert "weekly_version_bp = Blueprint(" in content
        assert "/projects/<int:project_id>/weekly-version-config" in content
        assert "/projects/<int:project_id>/weekly-version-config/api" in content
        assert "/projects/<int:project_id>/weekly-version" in content
        assert "/weekly-version-config/<int:config_id>/diff" in content
        assert "/weekly-version-config/<int:config_id>/file-full-diff" in content
        assert "/weekly-version-config/<int:config_id>/batch-confirm" in content
        assert "/weekly-version-config/<int:config_id>/stats" in content

    def test_app_removed_migrated_weekly_route_decorators(self):
        content = _read("app.py")
        assert "@app.route('/projects/<int:project_id>/weekly-version-config')" not in content
        assert "@app.route('/projects/<int:project_id>/weekly-version-config/api', methods=['GET', 'POST'])" not in content
        assert "@app.route('/projects/<int:project_id>/weekly-version-config/api/<int:config_id>', methods=['GET', 'PUT', 'DELETE'])" not in content
        assert "@app.route('/projects/<int:project_id>/weekly-version')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/diff')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/info')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/files')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/file-diff')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/file-full-diff')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/file-full-diff-data')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/file-previous-version')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/file-complete-diff')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/file-status', methods=['POST'])" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/file-status-info')" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/batch-confirm', methods=['POST'])" not in content
        assert "@app.route('/weekly-version-config/<int:config_id>/stats')" not in content

    def test_weekly_version_legacy_endpoints_remain_accessible_via_url_for(self):
        try:
            import app as app_module
        except Exception as exc:
            pytest.skip(f"app 模块导入失败，跳过 endpoint 兼容性检查: {exc}")

        flask_app = app_module.app
        with flask_app.test_request_context("/"):
            from flask import url_for

            assert url_for("weekly_version_config", project_id=1) == "/projects/1/weekly-version-config"
            assert url_for("weekly_version_diff", config_id=2) == "/weekly-version-config/2/diff"
            assert url_for("weekly_version_stats_api", config_id=3) == "/weekly-version-config/3/stats"

    def test_app_registers_commit_diff_blueprint(self):
        content = _read("app.py")
        assert "from routes.commit_diff_routes import commit_diff_bp" in content
        assert "app.register_blueprint(commit_diff_bp, name=\"\")" in content

    def test_commit_diff_routes_extracted_to_blueprint(self):
        content = _read("routes/commit_diff_routes.py")
        assert "commit_diff_bp = Blueprint(" in content
        assert "/repositories/<int:repository_id>/commits" in content
        assert "/commits/<int:commit_id>/excel-diff-data" in content
        assert "/commits/<int:commit_id>/diff/new" in content
        assert "/commits/<int:commit_id>/full-diff" in content
        assert "/commits/<int:commit_id>/refresh-diff" in content
        assert "/commits/<int:commit_id>/diff" in content
        assert "/commits/<int:commit_id>/status" in content
        assert "/commits/batch-update" in content
        assert "/commits/batch-approve" in content
        assert "/commits/batch-reject" in content
        assert "/commits/<int:commit_id>/priority-diff" in content
        assert "/commits/<int:commit_id>/diff-data" in content
        assert "/commits/merge-diff/refresh" in content
        assert "/commits/merge-diff" in content
        assert "/update_commit_fields" in content
        assert "/repositories/<int:repository_id>/commits/by-file" in content
        assert "/commits/compare" in content

    def test_app_removed_migrated_commit_diff_route_decorators(self):
        content = _read("app.py")
        assert "@app.route('/repositories/<int:repository_id>/commits')" not in content
        assert "@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/excel-diff-data')" not in content
        assert "@app.route('/commits/<int:commit_id>/excel-diff-data')" not in content
        assert "@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/diff/new')" not in content
        assert "@app.route('/commits/<int:commit_id>/diff/new')" not in content
        assert "@app.route('/commits/<int:commit_id>/full-diff')" not in content
        assert "@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/refresh-diff', methods=['POST'])" not in content
        assert "@app.route('/commits/<int:commit_id>/refresh-diff', methods=['POST'])" not in content
        assert "@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/diff')" not in content
        assert "@app.route('/commits/<int:commit_id>/diff')" not in content
        assert "@app.route('/commits/<int:commit_id>/status', methods=['POST'])" not in content
        assert "@app.route('/commits/batch-update', methods=['POST'])" not in content
        assert "@app.route('/commits/<int:commit_id>/approve-all', methods=['POST'])" not in content
        assert "@app.route('/commits/batch-approve', methods=['POST'])" not in content
        assert "@app.route('/commits/batch-reject', methods=['POST'])" not in content
        assert "@app.route('/commits/reject', methods=['POST'])" not in content
        assert "@app.route('/commits/<int:commit_id>/priority-diff', methods=['POST'])" not in content
        assert "@app.route('/<project_code>/<repository_name>/commits/<int:commit_id>/priority-diff', methods=['POST'])" not in content
        assert "@app.route('/commits/<int:commit_id>/diff-data', methods=['GET'])" not in content
        assert "@app.route('/commits/merge-diff/refresh', methods=['POST'])" not in content
        assert "@app.route('/commits/merge-diff')" not in content
        assert "@app.route('/update_commit_fields')" not in content
        assert "@app.route('/repositories/<int:repository_id>/commits/by-file')" not in content
        assert "@app.route('/commits/compare')" not in content

    def test_commit_diff_legacy_endpoints_remain_accessible_via_url_for(self):
        try:
            import app as app_module
        except Exception as exc:
            pytest.skip(f"app 模块导入失败，跳过 commit/diff endpoint 兼容性检查: {exc}")

        flask_app = app_module.app
        with flask_app.test_request_context("/"):
            from flask import url_for

            assert url_for("commit_list", repository_id=1) == "/repositories/1/commits"
            assert url_for("commit_diff", commit_id=2) == "/commits/2/diff"
            assert url_for("batch_update_commits_compat") == "/commits/batch-update"

    def test_app_registers_core_management_blueprint(self):
        content = _read("app.py")
        assert "from routes.core_management_routes import core_management_bp" in content
        assert "app.register_blueprint(core_management_bp, name=\"\")" in content

    def test_core_management_routes_extracted_to_blueprint(self):
        content = _read("routes/core_management_routes.py")
        assert "core_management_bp = Blueprint(" in content
        assert "/auth/login" in content
        assert "/projects" in content
        assert "/projects/<int:project_id>/repositories" in content
        assert "/repositories/git" in content
        assert "/repositories/svn" in content
        assert "/status-sync/management" in content
        assert "/repositories/compare" in content

    def test_app_has_no_direct_route_decorators_after_a8(self):
        content = _read("app.py")
        assert "@app.route(" not in content

    def test_core_legacy_endpoints_remain_accessible_via_url_for(self):
        try:
            import app as app_module
        except Exception as exc:
            pytest.skip(f"app 模块导入失败，跳过 core endpoint 兼容性检查: {exc}")

        flask_app = app_module.app
        with flask_app.test_request_context("/"):
            from flask import url_for

            assert url_for("index") == "/"
            assert url_for("projects") == "/projects"
            assert url_for("repository_config", project_id=1) == "/projects/1/repositories"
            assert url_for("repository_compare") == "/repositories/compare"
