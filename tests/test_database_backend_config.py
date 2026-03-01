import os
from pathlib import Path

import pytest

from utils.db_config import (
    apply_database_settings,
    build_database_settings,
    get_database_backend_from_config,
    get_sqlite_path_from_uri,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestDatabaseBackendConfig:
    def test_default_backend_is_sqlite(self):
        settings = build_database_settings({})
        assert settings["backend"] == "sqlite"
        assert settings["database_uri"].startswith("sqlite:///")
        assert settings["sqlite_db_path"].endswith(os.path.join("instance", "diff_platform.db"))

    def test_mysql_backend_uri_and_engine_options(self):
        settings = build_database_settings(
            {
                "DB_BACKEND": "mysql",
                "DB_HOST": "127.0.0.1",
                "DB_PORT": "3307",
                "DB_USER": "user@dev",
                "DB_PASSWORD": "p@ ss",
                "DB_NAME": "diff_platform",
                "DB_CHARSET": "utf8mb4",
                "DB_POOL_RECYCLE": "1200",
                "DB_POOL_PRE_PING": "true",
                "DB_POOL_SIZE": "8",
                "DB_MAX_OVERFLOW": "16",
                "DB_POOL_TIMEOUT": "40",
            }
        )

        assert settings["backend"] == "mysql"
        assert settings["database_uri"].startswith("mysql+pymysql://user%40dev:p%40+ss@127.0.0.1:3307/diff_platform")
        assert "charset=utf8mb4" in settings["database_uri"]
        assert settings["engine_options"]["pool_pre_ping"] is True
        assert settings["engine_options"]["pool_recycle"] == 1200
        assert settings["engine_options"]["pool_size"] == 8
        assert settings["engine_options"]["max_overflow"] == 16
        assert settings["engine_options"]["pool_timeout"] == 40
        assert "***@" in settings["display_uri"]

    def test_mysql_backend_requires_host_user_and_db(self):
        with pytest.raises(ValueError):
            build_database_settings({"DB_BACKEND": "mysql"})

    def test_database_url_overrides_backend_switch(self):
        settings = build_database_settings(
            {
                "DB_BACKEND": "sqlite",
                "DATABASE_URL": "mysql+pymysql://demo:pwd@localhost:3306/demo_db?charset=utf8mb4",
            }
        )
        assert settings["backend"] == "mysql"
        assert settings["database_uri"].startswith("mysql+pymysql://demo:pwd@localhost:3306/demo_db")

    def test_apply_database_settings_updates_config(self):
        app_config = {}
        settings = apply_database_settings(
            app_config,
            env={
                "DB_BACKEND": "sqlite",
                "SQLITE_DB_PATH": "instance/custom_diff.db",
            },
        )

        assert settings["backend"] == "sqlite"
        assert app_config["DB_BACKEND"] == "sqlite"
        assert app_config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///")
        assert app_config["SQLALCHEMY_TRACK_MODIFICATIONS"] is False
        assert app_config["SQLITE_DB_PATH"].endswith(os.path.join("instance", "custom_diff.db"))
        assert "SQLALCHEMY_ENGINE_OPTIONS" not in app_config

    def test_get_backend_and_sqlite_path_from_uri(self):
        config = {"SQLALCHEMY_DATABASE_URI": "sqlite:///instance/diff_platform.db"}
        assert get_database_backend_from_config(config) == "sqlite"
        sqlite_path = get_sqlite_path_from_uri(config["SQLALCHEMY_DATABASE_URI"])
        assert sqlite_path is not None
        assert sqlite_path.endswith(os.path.join("instance", "diff_platform.db"))


class TestAppDatabaseWiringStaticChecks:
    def test_app_uses_database_settings_module(self):
        app_content = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
        assert "from utils.db_config import (" in app_content
        assert "db_runtime_settings = apply_database_settings(app.config)" in app_content
        assert "get_database_backend_from_config(app.config)" in app_content
        assert "inspect(db.engine).get_table_names()" in app_content
        assert "app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///" not in app_content
