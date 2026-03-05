#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent self-update via platform release package."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

try:
    from .http_client import download_file, post_json
except ImportError:
    from http_client import download_file, post_json


_STATE_FILE_NAME = ".agent_release_state.json"
_TEMP_UPDATE_DIR_NAME = ".agent_update_tmp"
_PROTECTED_FILES = {
    ".env",
    _STATE_FILE_NAME,
}
_PROTECTED_DIRS = {
    "venv",
    ".venv",
    "agent_repos",
    "logs",
    "__pycache__",
    _TEMP_UPDATE_DIR_NAME,
}


def _agent_root() -> str:
    return str(Path(__file__).resolve().parent)


def _state_path() -> str:
    return os.path.join(_agent_root(), _STATE_FILE_NAME)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data: dict):
    path = _state_path()
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def get_local_release_version() -> str:
    state = _read_state()
    return str(state.get("version") or "").strip() or "unknown"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _join_platform_url(platform_base_url: str, path: str) -> str:
    base = str(platform_base_url or "").strip().rstrip("/")
    rel = str(path or "").strip()
    if not rel:
        return base
    if rel.startswith("http://") or rel.startswith("https://"):
        return rel
    if not rel.startswith("/"):
        rel = "/" + rel
    return f"{base}{rel}"


def _is_protected_relpath(rel_path: str) -> bool:
    rel_norm = str(rel_path or "").replace("\\", "/").strip("/")
    if not rel_norm:
        return True
    if rel_norm in _PROTECTED_FILES:
        return True
    parts = rel_norm.split("/")
    if any(part in _PROTECTED_DIRS for part in parts):
        return True
    return False


def _safe_extract(zip_path: str, extracted_root: str):
    with ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            name = str(member.filename or "").replace("\\", "/").strip("/")
            if not name:
                continue
            if ".." in name.split("/"):
                raise RuntimeError(f"invalid zip entry: {name}")
            target = os.path.abspath(os.path.join(extracted_root, name))
            if not target.startswith(os.path.abspath(extracted_root) + os.sep):
                raise RuntimeError(f"invalid zip target: {name}")
            if member.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _collect_files(root_dir: str) -> set[str]:
    rows: set[str] = set()
    base = os.path.abspath(root_dir)
    for root, _, files in os.walk(base):
        for name in files:
            abs_path = os.path.join(root, name)
            rel_path = os.path.relpath(abs_path, base).replace("\\", "/")
            if _is_protected_relpath(rel_path):
                continue
            rows.add(rel_path)
    return rows


def _install_requirements_if_needed(settings, extracted_root: str):
    if not bool(getattr(settings, "auto_update_install_deps", True)):
        return
    req_path = os.path.join(extracted_root, "requirements.txt")
    if not os.path.exists(req_path):
        return
    timeout = int(getattr(settings, "auto_update_pip_timeout_seconds", 900) or 900)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--prefer-binary", "-r", req_path],
        cwd=_agent_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(60, timeout),
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(f"pip install failed: {stderr or stdout or result.returncode}")


def _apply_files(extracted_root: str, new_files: set[str], old_managed_files: set[str]):
    agent_root = _agent_root()
    backup_root = os.path.join(agent_root, _TEMP_UPDATE_DIR_NAME, "backup")
    os.makedirs(backup_root, exist_ok=True)

    backup_map: dict[str, str | None] = {}
    try:
        for rel in sorted(new_files):
            if _is_protected_relpath(rel):
                continue
            src = os.path.join(extracted_root, rel.replace("/", os.sep))
            dst = os.path.join(agent_root, rel.replace("/", os.sep))
            old_backup = None
            if os.path.isfile(dst):
                old_backup = os.path.join(backup_root, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(old_backup), exist_ok=True)
                shutil.copy2(dst, old_backup)
            backup_map[rel] = old_backup
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

        stale_files = {item for item in old_managed_files if item not in new_files}
        for rel in sorted(stale_files):
            if _is_protected_relpath(rel):
                continue
            dst = os.path.join(agent_root, rel.replace("/", os.sep))
            if not os.path.isfile(dst):
                continue
            old_backup = os.path.join(backup_root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(old_backup), exist_ok=True)
            shutil.copy2(dst, old_backup)
            backup_map[rel] = old_backup
            os.remove(dst)
    except Exception:
        for rel, old_backup in backup_map.items():
            dst = os.path.join(agent_root, rel.replace("/", os.sep))
            if old_backup and os.path.isfile(old_backup):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(old_backup, dst)
            elif os.path.exists(dst):
                try:
                    os.remove(dst)
                except Exception:
                    pass
        raise


def check_and_apply_update(settings, common_headers: dict, agent_token: str, log_func):
    current_version = get_local_release_version()
    latest_url = f"{settings.platform_base_url}/api/agents/releases/latest"
    payload = {
        "agent_code": settings.agent_code,
        "agent_token": agent_token,
        "current_version": current_version,
    }
    status, data = post_json(
        latest_url,
        payload,
        headers=common_headers,
        timeout=int(getattr(settings, "auto_update_request_timeout_seconds", 15) or 15),
    )
    if status != 200 or not data.get("success"):
        return False, f"check update failed: status={status}, body={data}"
    if not data.get("has_update"):
        return False, "no update"

    release = data.get("release") if isinstance(data.get("release"), dict) else {}
    version = str(release.get("version") or "").strip()
    if not version:
        return False, "invalid release version"
    download_path = str(release.get("download_path") or "").strip()
    if not download_path:
        return False, "invalid download path"

    download_url = _join_platform_url(settings.platform_base_url, download_path)
    temp_root = os.path.join(_agent_root(), _TEMP_UPDATE_DIR_NAME, version)
    if os.path.exists(temp_root):
        shutil.rmtree(temp_root, ignore_errors=True)
    os.makedirs(temp_root, exist_ok=True)

    package_path = os.path.join(temp_root, "release.zip")
    download_headers = dict(common_headers or {})
    download_headers["X-Agent-Code"] = settings.agent_code
    download_headers["X-Agent-Token"] = agent_token
    dl_status, dl_data = download_file(
        download_url,
        package_path,
        headers=download_headers,
        timeout=int(getattr(settings, "auto_update_download_timeout_seconds", 120) or 120),
    )
    if dl_status != 200:
        return False, f"download update failed: status={dl_status}, body={dl_data}"

    expect_sha256 = str(release.get("package_sha256") or "").strip().lower()
    if expect_sha256:
        real_sha256 = _sha256_file(package_path).lower()
        if real_sha256 != expect_sha256:
            raise RuntimeError(f"package sha256 mismatch: expected={expect_sha256}, got={real_sha256}")

    expect_size = int(release.get("package_size") or 0)
    if expect_size > 0:
        real_size = os.path.getsize(package_path)
        if real_size != expect_size:
            raise RuntimeError(f"package size mismatch: expected={expect_size}, got={real_size}")

    extracted_root = os.path.join(temp_root, "extracted")
    os.makedirs(extracted_root, exist_ok=True)
    _safe_extract(package_path, extracted_root)

    _install_requirements_if_needed(settings, extracted_root)

    new_files = _collect_files(extracted_root)
    if "start_agent.py" not in new_files or "runner_runtime.py" not in new_files:
        raise RuntimeError("release package missing required entry files")

    old_state = _read_state()
    old_managed_files = {
        item for item in (old_state.get("managed_files") or [])
        if isinstance(item, str) and item.strip()
    }
    _apply_files(extracted_root, new_files, old_managed_files)
    _write_state(
        {
            "version": version,
            "commit_id": str(release.get("commit_id") or "").strip(),
            "installed_at": _now_text(),
            "managed_files": sorted(new_files),
        }
    )
    try:
        shutil.rmtree(temp_root, ignore_errors=True)
    except Exception:
        pass
    log_func(f"检测到新版本并已应用: {current_version} -> {version}")
    return True, f"updated to {version}"
