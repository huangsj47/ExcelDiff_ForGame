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
        content = _read("app.py")
        assert "@app.route('/commits/batch-update', methods=['POST'])" in content
        assert "def batch_update_commits_compat():" in content

    def test_commit_list_per_page_is_limited(self):
        content = _read("app.py")
        assert "requested_per_page = request.args.get('per_page', 50, type=int) or 50" in content
        assert "per_page = min(max(requested_per_page, 1), 200)" in content

    def test_weekly_version_per_page_is_limited(self):
        content = _read("app.py")
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
        assert "max_workers=max_workers" in threaded_service
