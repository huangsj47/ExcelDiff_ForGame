import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


class TestNodeCompatibilityRemoval:
    def test_node_legacy_files_removed(self):
        removed_files = [
            "server.js",
            "package.json",
            "routes/commits.js",
            "routes/projects.js",
            "routes/repositories.js",
            "views/layout.ejs",
        ]
        for rel in removed_files:
            assert not (PROJECT_ROOT / rel).exists(), f"应已移除: {rel}"

    def test_readme_no_node_compat_section(self):
        content = _read("README.md")
        assert "Node 兼容模块说明" not in content
        assert "Blueprint/Express" not in content

    def test_root_one_off_test_script_removed(self):
        assert not (PROJECT_ROOT / "test.py").exists()


class TestApiContractAndPaginationOptimization:
    def test_main_js_uses_aligned_batch_endpoints(self):
        content = _read("static/js/main.js")
        assert "/commits/batch-approve" in content
        assert "/commits/batch-reject" in content
        assert "/commits/batch-update" not in content

    def test_main_js_quick_update_uses_status_field(self):
        content = _read("static/js/main.js")
        assert "status: targetStatus" in content
        assert "action: status" not in content

    def test_app_has_batch_update_compat_route(self):
        content = _read("routes/commit_diff_routes.py")
        assert "/commits/batch-update" in content
        assert "endpoint=\"batch_update_commits_compat\"" in content
        app_content = _read("app.py")
        assert "def batch_update_commits_compat():" in app_content

    def test_commit_list_per_page_is_limited(self):
        content = _read("app.py")
        assert "requested_per_page = request.args.get('per_page', 50, type=int) or 50" in content
        assert "per_page = min(max(requested_per_page, 1), 200)" in content

    def test_weekly_version_per_page_is_limited(self):
        content = _read("services/weekly_version_logic.py")
        assert "requested_per_page = request.args.get('per_page', 20, type=int) or 20" in content
        assert "per_page = min(max(requested_per_page, 1), 200)" in content

    def test_redundant_query_count_debug_removed(self):
        content = _read("app.py")
        removed_markers = [
            "添加作者筛选后: {query.count()}",
            "添加路径筛选后: {query.count()}",
            "添加版本筛选后: {query.count()}",
            "添加操作筛选后: {query.count()}",
            "添加多状态筛选后: {query.count()}",
            "添加状态列表筛选后: {query.count()}",
            "添加单状态筛选后: {query.count()}",
            "Total commits in query: {query.count()}",
        ]
        for marker in removed_markers:
            assert marker not in content

    def test_thread_pool_workers_config_is_consistent(self):
        git_service = _read("services/git_service.py")
        threaded_service = _read("services/threaded_git_service.py")
        assert "max_workers=None" in git_service
        assert "max_workers=None" in threaded_service
        assert "max_workers=max_workers" in threaded_service
        assert "max_workers=6" not in threaded_service

    def test_git_service_thread_pool_is_lazy_initialized(self):
        git_service = _read("services/git_service.py")
        assert "self.thread_pool = None" in git_service
        assert "def _get_thread_pool(self):" in git_service
        assert "self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)" in git_service
        assert "with ThreadPoolExecutor(max_workers=min(len(sheet_tasks), self.max_workers)) as executor:" not in git_service
        assert "with ThreadPoolExecutor(max_workers=min(len(batches), self.max_workers)) as executor:" not in git_service

    def test_legacy_excel_diff_status_endpoint_deprecated(self):
        content = _read("routes/cache_management_routes.py")
        assert "@cache_management_bp.route(\"/api/excel-diff-status/<cache_key>\")" in content
        assert "\"status\": \"deprecated\"" in content
        assert "410" in content

    def test_async_repository_update_uses_id_based_worker(self):
        content = _read("app.py")
        assert "def run_repository_update_and_cache(repository_id):" in content
        assert "with app.app_context():" in content
        assert "db.session.get(Repository, repository_id)" in content
        assert "target=run_repository_update_and_cache" in content

    def test_sensitive_endpoints_cover_async_update_routes(self):
        content = _read("app.py")
        assert "'reuse_repository_and_update'" in content
        assert "'update_repository_and_cache'" in content

    def test_project_cache_stats_endpoint_uses_aggregated_queries(self):
        content = _read("routes/cache_management_routes.py")
        assert "func.sum(case((DiffCache.cache_status == \"completed\", 1), else_=0))" in content
        assert "func.sum(case((ExcelHtmlCache.cache_status == \"completed\", 1), else_=0))" in content
        assert "func.sum(case((WeeklyVersionExcelCache.cache_status == \"completed\", 1), else_=0))" in content

    def test_clear_all_diff_cache_uses_bulk_delete(self):
        content = _read("routes/cache_management_routes.py")
        assert "DiffCache.query.delete(synchronize_session=False)" in content
        assert "BackgroundTask.query.filter_by(task_type=\"excel_diff\").delete(synchronize_session=False)" in content
        assert "DELETE FROM diff_cache WHERE id IN" not in content
        assert "time.sleep(0.01)" not in content

    def test_excel_cache_logs_uses_db_pagination_not_all(self):
        content = _read("routes/cache_management_routes.py")
        assert "logs_query = OperationLog.query.filter_by(source=\"excel_cache\")" in content
        assert "total_logs_raw = logs_query.count()" in content
        assert ".offset(offset)" in content
        assert ".limit(fetch_size)" in content
        assert "OperationLog.query.order_by(OperationLog.created_at.desc()).all()" not in content
