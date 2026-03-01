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


class TestModelLoader:
    def test_prefers_models_when_models_exports_app_models(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace(USING_APP_MODELS=True, Commit="models_commit")
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

    def test_falls_back_to_app_when_models_not_app_bound(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace(USING_APP_MODELS=False, Commit="local_commit")
        app_module = SimpleNamespace(Commit="app_commit")

        def fake_import_module(name):
            if name == "models":
                return models_module
            if name == "app":
                return app_module
            raise ImportError(name)

        monkeypatch.setattr(model_loader.importlib, "import_module", fake_import_module)
        assert model_loader.get_runtime_model("Commit") == "app_commit"
        model_loader.clear_model_loader_cache()

    def test_falls_back_to_local_models_when_app_unavailable(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace(USING_APP_MODELS=False, Commit="local_commit")

        def fake_import_module(name):
            if name == "models":
                return models_module
            raise ImportError(name)

        monkeypatch.setattr(model_loader.importlib, "import_module", fake_import_module)
        assert model_loader.get_runtime_model("Commit") == "local_commit"
        model_loader.clear_model_loader_cache()

    def test_raises_when_model_missing(self, monkeypatch):
        model_loader.clear_model_loader_cache()
        models_module = SimpleNamespace(USING_APP_MODELS=False)
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
