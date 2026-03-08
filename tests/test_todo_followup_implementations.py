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

    def test_routing_alias_and_template_filters_bootstrap_extracted(self):
        app_content = _read("app.py")
        assert "from services.app_routing_bootstrap_service import configure_app_routing_bootstrap" in app_content
        assert "configure_app_routing_bootstrap(app=app, log_print=log_print)" in app_content

        service_content = _read("services/app_routing_bootstrap_service.py")
        assert "def configure_app_routing_bootstrap(*, app, log_print, bp_prefixes=None):" in service_content
        assert "def _register_endpoint_aliases(app, log_print, bp_prefixes):" in service_content
        assert "def _register_template_filters(app):" in service_content

    def test_runtime_wiring_blocks_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.app_runtime_wiring_service import configure_runtime_wirings" in app_content
        assert "configure_runtime_wirings(" in app_content

        service_content = _read("services/app_runtime_wiring_service.py")
        assert "def configure_runtime_wirings(" in service_content
        assert "configure_commit_diff_logic(" in service_content
        assert "configure_weekly_version_logic(" in service_content
        assert "configure_task_worker(" in service_content

    def test_security_and_template_bootstrap_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.app_security_bootstrap_service import configure_app_security_bootstrap" in app_content
        assert "configure_app_security_bootstrap(" in app_content

        service_content = _read("services/app_security_bootstrap_service.py")
        assert "def configure_app_security_bootstrap(" in service_content
        assert "def _prefers_json_error_response() -> bool:" in service_content
        assert "def _infer_resource_label_from_path(path: str) -> str:" in service_content

    def test_repository_update_form_logic_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.repository_update_form_service import (" in app_content
        assert "handle_update_repository_form" in app_content
        assert "clear_repository_state_for_switch" in app_content
        assert "return handle_update_repository_form(" in app_content

        service_content = _read("services/repository_update_form_service.py")
        assert "def handle_update_repository_form(" in service_content
        assert "def clear_repository_state_for_switch(" in service_content

    def test_repository_update_api_logic_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.repository_update_api_service import (" in app_content
        assert "run_repository_update_and_cache_worker" in app_content
        assert "handle_reuse_repository_and_update" in app_content
        assert "handle_update_repository_and_cache" in app_content
        assert "handle_batch_update_credentials" in app_content
        assert "return run_repository_update_and_cache_worker(" in app_content
        assert "return handle_reuse_repository_and_update(" in app_content
        assert "return handle_update_repository_and_cache(" in app_content
        assert "return handle_batch_update_credentials(" in app_content

        service_content = _read("services/repository_update_api_service.py")
        assert "def run_repository_update_and_cache_worker(" in service_content
        assert "def handle_reuse_repository_and_update(" in service_content
        assert "def handle_update_repository_and_cache(" in service_content
        assert "def handle_batch_update_credentials(" in service_content

    def test_commit_status_api_logic_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.commit_status_api_service import (" in app_content
        assert "handle_update_commit_status" in app_content
        assert "handle_batch_update_commits_compat" in app_content
        assert "return handle_update_commit_status(" in app_content
        assert "return handle_batch_update_commits_compat(" in app_content

        service_content = _read("services/commit_status_api_service.py")
        assert "def handle_update_commit_status(" in service_content
        assert "def handle_batch_update_commits_compat(" in service_content

    def test_repository_maintenance_api_logic_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.repository_maintenance_api_service import (" in app_content
        assert "handle_regenerate_cache" in app_content
        assert "handle_get_cache_status" in app_content
        assert "handle_get_clone_status" in app_content
        assert "handle_retry_clone_repository" in app_content
        assert "handle_sync_repository" in app_content
        assert "should_retry_with_reclone" in app_content
        assert "return handle_regenerate_cache(" in app_content
        assert "return handle_get_cache_status(" in app_content
        assert "return handle_get_clone_status(" in app_content
        assert "return handle_retry_clone_repository(" in app_content
        assert "return handle_sync_repository(" in app_content

        service_content = _read("services/repository_maintenance_api_service.py")
        assert "def handle_regenerate_cache(" in service_content
        assert "def handle_get_cache_status(" in service_content
        assert "def handle_get_clone_status(" in service_content
        assert "def should_retry_with_reclone(" in service_content
        assert "def handle_retry_clone_repository(" in service_content
        assert "def handle_sync_repository(" in service_content

    def test_commit_diff_page_logic_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.commit_diff_page_service import (" in app_content
        assert "handle_commit_full_diff" in app_content
        assert "handle_refresh_commit_diff" in app_content
        assert "return handle_commit_full_diff(" in app_content
        assert "return handle_refresh_commit_diff(" in app_content

        service_content = _read("services/commit_diff_page_service.py")
        assert "def handle_commit_full_diff(" in service_content
        assert "def handle_refresh_commit_diff(" in service_content

    def test_commit_diff_view_logic_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.commit_diff_view_service import handle_commit_diff_view" in app_content
        assert "return handle_commit_diff_view(" in app_content
        assert "# diff_data = get_unified_diff_data(commit, previous_commit)" in app_content

        service_content = _read("services/commit_diff_view_service.py")
        assert "def handle_commit_diff_view(" in service_content

    def test_excel_diff_api_logic_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.excel_diff_api_service import handle_get_excel_diff_data" in app_content
        assert "return handle_get_excel_diff_data(" in app_content
        assert "# Commit.commit_time == commit.commit_time" in app_content
        assert "# Commit.id < commit.id" in app_content

        service_content = _read("services/excel_diff_api_service.py")
        assert "def handle_get_excel_diff_data(" in service_content

    def test_commit_list_page_logic_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.commit_list_page_service import handle_commit_list_page" in app_content
        assert "return handle_commit_list_page(" in app_content
        assert "# requested_per_page = request.args.get('per_page', 50, type=int) or 50" in app_content
        assert "# per_page = min(max(requested_per_page, 1), 200)" in app_content
        assert "# missing_git_branch_repo_ids.append(repo.id)" in app_content
        assert "# queue_missing_git_branch_refresh(project.id, missing_git_branch_repo_ids)" in app_content

        service_content = _read("services/commit_list_page_service.py")
        assert "def handle_commit_list_page(" in service_content

    def test_commit_diff_new_page_logic_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.commit_diff_new_page_service import handle_commit_diff_new_page" in app_content
        assert "return handle_commit_diff_new_page(" in app_content

        service_content = _read("services/commit_diff_new_page_service.py")
        assert "def handle_commit_diff_new_page(" in service_content

    def test_request_logging_bootstrap_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.app_request_logging_service import configure_request_logging" in app_content
        assert "configure_request_logging(" in app_content
        assert "suppress_agent_access_log=_env_bool(\"SUPPRESS_AGENT_ACCESS_LOG\", True)" in app_content

        service_content = _read("services/app_request_logging_service.py")
        assert "class _WerkzeugAgentAccessFilter(logging.Filter):" in service_content
        assert "def configure_request_logging(*, app, log_print, suppress_agent_access_log: bool) -> None:" in service_content

    def test_blueprint_bootstrap_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.app_blueprint_bootstrap_service import configure_app_blueprints" in app_content
        assert "configure_app_blueprints(" in app_content

        service_content = _read("services/app_blueprint_bootstrap_service.py")
        assert "def configure_app_blueprints(" in service_content
        assert "def _register_blueprint_with_trace(" in service_content
        assert "app.register_blueprint(cache_management_bp)" in service_content

    def test_commit_route_scope_helpers_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.commit_route_scope_service import (" in app_content
        assert "dispatch_commit_route_with_scope" in app_content
        assert "ensure_commit_route_scope_or_404" in app_content
        assert "return dispatch_commit_route_with_scope(" in app_content

        service_content = _read("services/commit_route_scope_service.py")
        assert "def ensure_repository_access_or_403(" in service_content
        assert "def ensure_commit_access_or_403(" in service_content
        assert "def ensure_commit_route_scope_or_404(" in service_content
        assert "def dispatch_commit_route_with_scope(" in service_content

    def test_repository_misc_page_helpers_extracted_from_app_entry(self):
        app_content = _read("app.py")
        assert "from services.repository_misc_page_service import (" in app_content
        assert "render_edit_repository_page" in app_content
        assert "check_local_repository_exists as check_local_repository_exists_service" in app_content
        assert "return render_edit_repository_page(" in app_content
        assert "return check_local_repository_exists_service(" in app_content

        service_content = _read("services/repository_misc_page_service.py")
        assert "def render_edit_repository_page(" in service_content
        assert "def check_local_repository_exists(" in service_content
