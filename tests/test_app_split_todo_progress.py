from pathlib import Path

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
