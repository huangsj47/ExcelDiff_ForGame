"""Shared runtime model loader to reduce hard coupling with app.py."""

from __future__ import annotations

import importlib
from functools import lru_cache
from typing import Any, Tuple


@lru_cache(maxsize=1)
def _load_models_module():
    try:
        return importlib.import_module("models")
    except Exception:
        return None


@lru_cache(maxsize=1)
def _load_app_module():
    try:
        return importlib.import_module("app")
    except Exception:
        return None


def _resolve_runtime_object(name: str) -> Any:
    """Resolve object from models first, then app fallback, then local models."""
    models_module = _load_models_module()
    app_module = _load_app_module()

    if models_module and getattr(models_module, "USING_APP_MODELS", False) and hasattr(models_module, name):
        return getattr(models_module, name)

    if app_module and hasattr(app_module, name):
        return getattr(app_module, name)

    if models_module and hasattr(models_module, name):
        return getattr(models_module, name)

    raise RuntimeError(f"无法解析运行时对象: {name}")


def get_runtime_model(name: str) -> Any:
    return _resolve_runtime_object(name)


def get_runtime_models(*names: str) -> Tuple[Any, ...]:
    return tuple(_resolve_runtime_object(name) for name in names)


def clear_model_loader_cache() -> None:
    _load_models_module.cache_clear()
    _load_app_module.cache_clear()

