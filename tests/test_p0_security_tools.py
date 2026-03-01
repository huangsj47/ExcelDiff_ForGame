import os
import re
from pathlib import Path

import pytest

from utils.path_security import build_repository_local_path, validate_segment
from utils.security_utils import (
    decrypt_credential,
    encrypt_credential,
    sanitize_text,
    sanitize_url,
    validate_repository_name,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="_csrf_token"\s+value="([^"]+)"', html)
    assert match, "页面中未找到 _csrf_token 隐藏字段"
    return match.group(1)


class TestSecurityUtils:
    def test_encrypt_decrypt_roundtrip(self):
        secret = "my_token_123"
        encrypted = encrypt_credential(secret)
        assert encrypted is not None
        assert encrypted.startswith("enc::")
        assert decrypt_credential(encrypted) == secret

    def test_decrypt_plaintext_backward_compatible(self):
        assert decrypt_credential("plain_password") == "plain_password"

    def test_sanitize_url_and_text(self):
        url = "https://oauth2:super-secret@git.example.com/group/repo.git"
        safe_url = sanitize_url(url)
        assert "super-secret" not in safe_url
        assert "***" in safe_url

        text = "clone https://oauth2:super-secret@git.example.com/repo.git now"
        safe_text = sanitize_text(text)
        assert "super-secret" not in safe_text
        assert "***" in safe_text

    def test_validate_repository_name(self):
        assert validate_repository_name("repo_01.test-name")
        assert not validate_repository_name("../evil")
        assert not validate_repository_name("name with space")
        assert not validate_repository_name("")


class TestPathSecurity:
    def test_build_repository_local_path(self):
        path = build_repository_local_path("PROJ", "repo_1", 12)
        repos_root = os.path.abspath("repos")
        assert os.path.abspath(path).startswith(repos_root + os.sep)
        assert path.endswith(f"PROJ_repo_1_{12}")

    def test_build_repository_local_path_sanitizes_non_strict(self):
        path = build_repository_local_path("PROJ", "../bad/name", 99, strict=False)
        assert ".." not in path
        assert "bad_name" in path

    def test_build_repository_local_path_rejects_invalid_in_strict_mode(self):
        with pytest.raises(ValueError):
            build_repository_local_path("PROJ", "../bad", 1, strict=True)

    def test_validate_segment(self):
        assert validate_segment("ok_repo-1")
        assert not validate_segment("../bad")


class TestAppSecurityBehavior:
    @pytest.fixture(scope="class")
    def app_module(self):
        try:
            import app as app_module
        except Exception as exc:
            pytest.skip(f"app 模块导入失败，跳过应用级安全测试: {exc}")

        app_module.app.config["TESTING"] = True
        return app_module

    @pytest.fixture
    def client(self, app_module):
        return app_module.app.test_client()

    def test_admin_endpoint_requires_auth(self, client):
        response = client.get(
            "/admin/excel-cache/strategy-info",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 401
        payload = response.get_json() or {}
        assert payload.get("success") is False

    def test_admin_token_can_pass_auth_gate(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_API_TOKEN", "p0-test-admin-token")
        response = client.get(
            "/admin/excel-cache/strategy-info",
            headers={
                "Accept": "application/json",
                "X-Admin-Token": "p0-test-admin-token",
            },
        )
        assert response.status_code != 401

    def test_post_without_csrf_rejected(self, client):
        response = client.post(
            "/auth/logout",
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 400
        payload = response.get_json() or {}
        assert "CSRF" in str(payload.get("message", ""))

    def test_admin_login_with_csrf(self, client, monkeypatch):
        monkeypatch.setenv("ADMIN_USERNAME", "admin")
        monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")

        get_page = client.get("/auth/login")
        assert get_page.status_code == 200
        csrf = _extract_csrf_token(get_page.get_data(as_text=True))

        login_resp = client.post(
            "/auth/login",
            data={
                "username": "admin",
                "password": "test-admin-password",
                "_csrf_token": csrf,
            },
            follow_redirects=False,
        )
        assert login_resp.status_code in (302, 303)

        with client.session_transaction() as sess:
            assert sess.get("is_admin") is True


class TestTemplateSecurity:
    def test_git_template_not_prefill_token(self):
        content = (PROJECT_ROOT / "templates" / "add_git_repository.html").read_text(encoding="utf-8")
        assert "{{ repository.token }}" not in content
        assert "留空表示不修改当前 token" in content

    def test_svn_template_not_prefill_password(self):
        content = (PROJECT_ROOT / "templates" / "add_svn_repository.html").read_text(encoding="utf-8")
        assert "{{ repository.password }}" not in content
        assert "留空表示不修改当前密码" in content
