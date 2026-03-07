from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


class TestWeeklyTodoFollowups:
    def test_weekly_excel_task_dedup_is_enabled(self):
        content = _read("services/weekly_version_logic.py")
        assert "def create_weekly_excel_cache_task(config_id, file_path):" in content
        assert "BackgroundTask.task_type == 'weekly_excel_cache'" in content
        assert "BackgroundTask.status.in_(['pending', 'processing'])" in content
        assert "跳过重复周版本Excel缓存任务" in content

    def test_weekly_excel_html_prefers_merged_cache_payload(self):
        content = _read("services/weekly_version_logic.py")
        assert "def _load_weekly_excel_diff_from_cache(repository, diff_cache, file_path):" in content
        assert "_extract_excel_diff_from_payload(merged_payload)" in content
        assert "复用周版本缓存中的合并Excel diff" in content
        assert "暂时使用第一个和最后一个提交进行对比" not in content

    def test_weekly_excel_lookup_composite_index_exists(self):
        content = _read("models/weekly_version.py")
        assert "idx_weekly_excel_lookup" in content
        assert "'base_commit_id'" in content
        assert "'latest_commit_id'" in content
        assert "'diff_version'" in content
        assert "'cache_status'" in content

    def test_excel_diff_service_recognizes_xlsb_and_csv(self):
        content = _read("services/excel_diff_cache_service.py")
        func_start = content.find("def is_excel_file(self, file_path):")
        assert func_start >= 0
        func_end = content.find("\n    def ", func_start + 1)
        func_body = content[func_start:func_end] if func_end > 0 else content[func_start:]
        assert ".xlsb" in func_body
        assert ".csv" in func_body

    def test_startup_version_mismatch_cleanup_uses_bulk_service_calls(self):
        app_content = _read("app.py")
        func_start = app_content.find("def clear_version_mismatch_cache():")
        assert func_start >= 0
        func_end = app_content.find("\n# ---------------------------------------------------------------------------", func_start)
        func_body = app_content[func_start:func_end] if func_end > 0 else app_content[func_start:]
        assert "clear_startup_version_mismatch_cache(" in func_body

        service_content = _read("services/app_bootstrap_db_service.py")
        assert "excel_cache_service.cleanup_version_mismatch_cache()" in service_content
        assert "excel_html_cache_service.cleanup_old_version_cache()" in service_content
        assert ".limit(batch_size).all()" not in service_content
        assert "time.sleep(0.1)" not in service_content

    def test_auth_bootstrap_logic_is_extracted_to_service(self):
        app_content = _read("app.py")
        assert "from services.auth_bootstrap_service import initialize_auth_subsystem" in app_content
        assert "initialize_auth_subsystem(app=app, db=db, log_print=log_print)" in app_content

        service_content = _read("services/auth_bootstrap_service.py")
        assert "def initialize_auth_subsystem(*, app, db, log_print):" in service_content
        assert "def register_qkit_fallback_endpoints(app_instance, log_print):" in service_content
        assert "def log_auth_route_diagnostics(app_instance, log_print):" in service_content
