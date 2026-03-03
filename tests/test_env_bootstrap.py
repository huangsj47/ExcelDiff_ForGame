from pathlib import Path

from utils.env_bootstrap import (
    ensure_env_file,
    is_escaped_newline_malformed_env,
    render_env_text,
)


def test_render_env_text_uses_real_newlines():
    text = render_env_text(["A=1", "B=2", "C=3"])
    assert text == "A=1\nB=2\nC=3\n"
    assert "\\n" not in text


def test_detect_and_repair_malformed_env_with_literal_backslash_n(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\\nB=2\\nC=3\\n", encoding="utf-8")
    assert is_escaped_newline_malformed_env(env_path.read_text(encoding="utf-8")) is True

    action, _ = ensure_env_file(env_path)
    assert action == "repaired"
    assert env_path.read_text(encoding="utf-8") == "A=1\nB=2\nC=3\n"


def test_ensure_env_file_generates_defaults_when_missing(tmp_path):
    env_path = tmp_path / ".env"
    assert not env_path.exists()

    action, creds = ensure_env_file(env_path)
    content = env_path.read_text(encoding="utf-8")

    assert action == "generated"
    assert "ADMIN_PASSWORD" in creds
    assert "ADMIN_API_TOKEN" in creds
    assert content.startswith("# Auto-generated .env for Diff Platform\n")
    assert "\\n" not in content
    assert "HOST=0.0.0.0\n" in content
    assert "DB_BACKEND=sqlite\n" in content
    assert "BRANCH_REFRESH_COOLDOWN_SECONDS=120\n" in content


def test_start_bat_uses_bootstrap_module():
    content = Path("start.bat").read_text(encoding="utf-8")
    assert "-m utils.env_bootstrap" in content
