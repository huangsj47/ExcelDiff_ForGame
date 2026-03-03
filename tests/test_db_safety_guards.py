import pytest

from utils.db_safety import (
    assert_destructive_db_allowed,
    is_temp_sqlite_path,
)


def test_is_temp_sqlite_path_detects_temp_patterns():
    assert is_temp_sqlite_path(r"C:\Users\foo\AppData\Local\Temp\diff_platform_test_abc.db")
    assert is_temp_sqlite_path("/tmp/pytest-of-user/pytest-1/test.db")
    assert not is_temp_sqlite_path(r"C:\work\project\instance\diff_platform.db")


def test_destructive_guard_blocks_default_instance_sqlite():
    with pytest.raises(RuntimeError) as exc_info:
        assert_destructive_db_allowed(
            database_uri="sqlite:///C:/repo/instance/diff_platform.db",
            action_name="unit_test_block_case",
            testing=False,
        )
    assert "拒绝执行破坏性数据库操作" in str(exc_info.value)


def test_destructive_guard_allows_testing_temp_sqlite():
    assert_destructive_db_allowed(
        database_uri="sqlite:///C:/Users/foo/AppData/Local/Temp/diff_platform_test_safe.db",
        action_name="unit_test_temp_case",
        testing=True,
    )


def test_destructive_guard_allows_explicit_env_override(monkeypatch):
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_DB_OPS", "true")
    assert_destructive_db_allowed(
        database_uri="sqlite:///C:/repo/instance/diff_platform.db",
        action_name="unit_test_env_override",
        testing=False,
    )
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_DB_OPS", raising=False)


def test_destructive_guard_blocks_non_sqlite_without_override(monkeypatch):
    monkeypatch.delenv("ALLOW_DESTRUCTIVE_DB_OPS", raising=False)
    with pytest.raises(RuntimeError):
        assert_destructive_db_allowed(
            database_uri="mysql+pymysql://root:pwd@127.0.0.1:3306/diff_platform",
            action_name="unit_test_mysql_block",
            testing=True,
        )


def test_recreate_db_script_has_safety_guard():
    content = open("recreate_db.py", "r", encoding="utf-8").read()
    assert "assert_destructive_db_allowed" in content
    assert "db.drop_all()" in content
