from pathlib import Path
from types import SimpleNamespace

import pytest

from services import model_loader
from services import svn_service


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


class TestServiceAppCoupling:
    def test_status_sync_service_no_direct_app_imports(self):
        content = _read("services/status_sync_service.py")
        assert "from app import" not in content
        assert "get_runtime_models(" in content

    def test_svn_service_no_direct_app_imports(self):
        content = _read("services/svn_service.py")
        assert "from app import" not in content
        assert "get_runtime_models(" in content

    def test_excel_html_cache_service_no_direct_app_imports(self):
        content = _read("services/excel_html_cache_service.py")
        assert "from app import" not in content
        assert "import_module('app')" not in content
        assert "get_runtime_models(" in content

    def test_weekly_excel_cache_service_no_direct_app_imports(self):
        content = _read("services/weekly_excel_cache_service.py")
        assert "from app import" not in content
        assert "import_module('app')" not in content
        assert "get_runtime_models(" in content

    def test_incremental_cache_system_no_direct_app_imports(self):
        content = _read("incremental_cache_system.py")
        assert "from app import" not in content
        assert "get_runtime_models(" in content

    def test_db_retry_no_direct_app_import(self):
        content = _read("utils/db_retry.py")
        assert "from app import" not in content
        assert "get_runtime_models(" in content

    def test_init_scripts_use_model_loader(self):
        for path in ["init_database.py", "init_html_cache.py", "recreate_db.py"]:
            content = _read(path)
            assert "from app import" not in content
            assert "get_runtime_models(" in content


class TestModelLoader:
    def test_model_lookup_does_not_touch_app_when_models_hit(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace(Commit="models_commit")
        import_calls = {"models": 0, "app": 0}

        def fake_import_module(name):
            if name == "models":
                import_calls["models"] += 1
                return models_module
            if name == "app":
                import_calls["app"] += 1
                raise AssertionError("model lookup should not import app when models hit")
            raise ImportError(name)

        monkeypatch.setattr(model_loader.importlib, "import_module", fake_import_module)
        assert model_loader.get_runtime_model("Commit") == "models_commit"
        assert import_calls["models"] == 1
        assert import_calls["app"] == 0
        model_loader.clear_model_loader_cache()

    def test_prefers_models_for_model_objects(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace(Commit="models_commit")
        app_module = SimpleNamespace(Commit="app_commit")

        def fake_import_module(name):
            if name == "models":
                return models_module
            if name == "app":
                return app_module
            raise ImportError(name)

        monkeypatch.setattr(model_loader.importlib, "import_module", fake_import_module)
        assert model_loader.get_runtime_model("Commit") == "models_commit"
        model_loader.clear_model_loader_cache()

    def test_model_objects_do_not_fall_back_to_app_when_models_has_symbol(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace(Commit="local_commit")
        app_module = SimpleNamespace(Commit="app_commit")

        def fake_import_module(name):
            if name == "models":
                return models_module
            if name == "app":
                return app_module
            raise ImportError(name)

        monkeypatch.setattr(model_loader.importlib, "import_module", fake_import_module)
        assert model_loader.get_runtime_model("Commit") == "local_commit"
        model_loader.clear_model_loader_cache()

    def test_non_model_object_falls_back_to_app(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace()
        app_module = SimpleNamespace(log_print="log_fn")

        def fake_import_module(name):
            if name == "models":
                return models_module
            if name == "app":
                return app_module
            raise ImportError(name)

        monkeypatch.setattr(model_loader.importlib, "import_module", fake_import_module)
        assert model_loader.get_runtime_model("log_print") == "log_fn"
        model_loader.clear_model_loader_cache()

    def test_falls_back_to_local_models_when_app_unavailable(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace(Commit="local_commit")

        def fake_import_module(name):
            if name == "models":
                return models_module
            raise ImportError(name)

        monkeypatch.setattr(model_loader.importlib, "import_module", fake_import_module)
        assert model_loader.get_runtime_model("Commit") == "local_commit"
        model_loader.clear_model_loader_cache()

    def test_raises_when_model_missing(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace()
        app_module = SimpleNamespace()

        def fake_import_module(name):
            if name == "models":
                return models_module
            if name == "app":
                return app_module
            raise ImportError(name)

        monkeypatch.setattr(model_loader.importlib, "import_module", fake_import_module)
        with pytest.raises(RuntimeError):
            model_loader.get_runtime_model("MissingModel")
        model_loader.clear_model_loader_cache()


class TestSvnModelAdapter:
    def test_get_db_models_uses_runtime_loader(self, monkeypatch):
        monkeypatch.setattr(
            svn_service,
            "get_runtime_models",
            lambda *_names: ("db_obj", "commit_obj"),
        )
        db_obj, commit_obj = svn_service.get_db_models()
        assert db_obj == "db_obj"
        assert commit_obj == "commit_obj"

    def test_get_db_models_returns_none_on_loader_failure(self, monkeypatch):
        def boom(*_names):
            raise RuntimeError("boom")

        monkeypatch.setattr(svn_service, "get_runtime_models", boom)
        db_obj, commit_obj = svn_service.get_db_models()
        assert db_obj is None
        assert commit_obj is None
