#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent release package publish/load helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[1])


def get_agent_source_dir() -> str:
    return os.path.join(_repo_root(), "agent")


def get_agent_releases_root() -> str:
    raw = (os.environ.get("AGENT_RELEASES_DIR") or "").strip()
    if raw:
        return os.path.abspath(raw)
    return os.path.join(_repo_root(), "instance", "agent_releases")


def _releases_dir() -> str:
    return os.path.join(get_agent_releases_root(), "releases")


def _latest_manifest_path() -> str:
    return os.path.join(get_agent_releases_root(), "latest.json")


def _release_dir(version: str) -> str:
    return os.path.join(_releases_dir(), version)


def _release_manifest_path(version: str) -> str:
    return os.path.join(_release_dir(version), "manifest.json")


def _release_package_name(version: str) -> str:
    return f"agent_release_{version}.zip"


def _release_package_path(version: str) -> str:
    return os.path.join(_release_dir(version), _release_package_name(version))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_version(version: str) -> str:
    text = str(version or "").strip()
    if not text or not _VERSION_RE.match(text):
        raise ValueError(f"invalid release version: {version}")
    return text


def _atomic_write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _is_skip_file(rel_path: str) -> bool:
    rel_norm = rel_path.replace("\\", "/").strip("/")
    rel_lower = rel_norm.lower()
    parts = [p for p in rel_lower.split("/") if p]
    base_name = os.path.basename(rel_lower)

    if not rel_norm:
        return True
    if any(p in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"} for p in parts):
        return True
    if any(p in {"venv", ".venv", "agent_repos", "logs"} for p in parts):
        return True
    if rel_lower.endswith((".pyc", ".pyo", ".swp", ".tmp")):
        return True
    if rel_lower == ".env":
        return True
    if rel_lower.startswith("agent_package_") and rel_lower.endswith(".zip"):
        return True
    if rel_lower.startswith("agent_release_") and rel_lower.endswith(".zip"):
        return True
    if rel_lower in {
        ".agent_release_state.json",
        ".agent_update.lock",
    }:
        return True
    if base_name in {
        "打包agent.bat",
        "build_zip.py",
        "agent.log",
    }:
        return True
    return False


def _collect_agent_files(source_dir: str) -> list[dict]:
    base = os.path.abspath(source_dir)
    rows: list[dict] = []
    for root, _, files in os.walk(base):
        for name in files:
            abs_path = os.path.join(root, name)
            rel_path = os.path.relpath(abs_path, base).replace("\\", "/")
            if _is_skip_file(rel_path):
                continue
            file_size = os.path.getsize(abs_path)
            rows.append(
                {
                    "path": rel_path,
                    "size": file_size,
                    "sha256": _sha256_file(abs_path),
                }
            )
    rows.sort(key=lambda item: item["path"])
    return rows


def detect_git_commit_id(repo_root: str | None = None) -> str:
    base = os.path.abspath(repo_root or _repo_root())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=base,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return str(result.stdout or "").strip()


def generate_default_version(commit_id: str | None = None) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    commit = str(commit_id or "").strip()
    if commit:
        return f"{ts}-{commit[:8]}"
    return ts


def publish_agent_release(
    *,
    version: str | None = None,
    commit_id: str | None = None,
    notes: str | None = None,
    source_dir: str | None = None,
    force: bool = False,
) -> dict:
    source = os.path.abspath(source_dir or get_agent_source_dir())
    if not os.path.isdir(source):
        raise FileNotFoundError(f"agent source dir not found: {source}")

    resolved_commit = str(commit_id or "").strip() or detect_git_commit_id(_repo_root())
    resolved_version = _safe_version(version or generate_default_version(resolved_commit))

    release_dir = _release_dir(resolved_version)
    package_path = _release_package_path(resolved_version)
    manifest_path = _release_manifest_path(resolved_version)

    if os.path.exists(release_dir):
        if not force:
            raise FileExistsError(f"release version already exists: {resolved_version}")
        shutil.rmtree(release_dir, ignore_errors=True)

    os.makedirs(release_dir, exist_ok=True)
    file_rows = _collect_agent_files(source)
    if not file_rows:
        raise RuntimeError("agent source has no files to package")

    with ZipFile(package_path, "w", ZIP_DEFLATED) as zf:
        for item in file_rows:
            abs_path = os.path.join(source, item["path"].replace("/", os.sep))
            zf.write(abs_path, arcname=item["path"])

    package_sha256 = _sha256_file(package_path)
    package_size = os.path.getsize(package_path)
    manifest = {
        "version": resolved_version,
        "commit_id": resolved_commit,
        "created_at": _utc_now_iso(),
        "notes": str(notes or "").strip(),
        "package_file": os.path.basename(package_path),
        "package_size": package_size,
        "package_sha256": package_sha256,
        "managed_files": [item["path"] for item in file_rows],
        "files": file_rows,
    }
    _atomic_write_json(manifest_path, manifest)
    _atomic_write_json(_latest_manifest_path(), manifest)
    return manifest


def load_latest_release_manifest() -> dict | None:
    path = _latest_manifest_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "").strip()
    if not version:
        return None
    try:
        _safe_version(version)
    except Exception:
        return None
    return data


def load_release_manifest(version: str) -> dict | None:
    try:
        resolved = _safe_version(version)
    except Exception:
        return None
    path = _release_manifest_path(resolved)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("version") or "").strip() != resolved:
        return None
    return data


def get_release_package_path(version: str) -> str | None:
    manifest = load_release_manifest(version)
    if not manifest:
        return None
    package_name = str(manifest.get("package_file") or "").strip()
    if not package_name:
        return None
    try:
        resolved_version = _safe_version(version)
    except Exception:
        return None
    package_path = os.path.join(_release_dir(resolved_version), package_name)
    if not os.path.exists(package_path):
        return None
    return package_path


def _parse_created_at_for_sort(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def list_release_manifests() -> list[dict]:
    releases_root = _releases_dir()
    if not os.path.isdir(releases_root):
        return []

    rows: list[dict] = []
    for name in os.listdir(releases_root):
        release_dir = os.path.join(releases_root, name)
        if not os.path.isdir(release_dir):
            continue
        manifest = load_release_manifest(name)
        if not manifest:
            continue
        rows.append(manifest)

    rows.sort(
        key=lambda item: (
            _parse_created_at_for_sort(item.get("created_at") or ""),
            str(item.get("version") or ""),
        ),
        reverse=True,
    )
    return rows


def rollback_latest_release(*, target_version: str | None = None, steps: int = 1) -> dict:
    current_latest = load_latest_release_manifest()
    if not current_latest:
        raise RuntimeError("no latest release to rollback")

    current_version = str(current_latest.get("version") or "").strip()
    if not current_version:
        raise RuntimeError("invalid latest release manifest")

    releases = list_release_manifests()
    if not releases:
        raise RuntimeError("no releases found")

    if target_version:
        resolved_target = _safe_version(target_version)
        target = load_release_manifest(resolved_target)
        if not target:
            raise RuntimeError(f"target release not found: {resolved_target}")
    else:
        step_count = max(1, int(steps or 1))
        current_idx = None
        for idx, item in enumerate(releases):
            if str(item.get("version") or "").strip() == current_version:
                current_idx = idx
                break
        if current_idx is None:
            raise RuntimeError(f"latest version not found in release list: {current_version}")
        target_idx = current_idx + step_count
        if target_idx >= len(releases):
            raise RuntimeError("no older release to rollback to")
        target = releases[target_idx]

    target_version_resolved = str(target.get("version") or "").strip()
    if not target_version_resolved:
        raise RuntimeError("invalid target release manifest")
    if target_version_resolved == current_version:
        return {
            "changed": False,
            "from_version": current_version,
            "to_version": target_version_resolved,
            "latest": target,
        }

    _atomic_write_json(_latest_manifest_path(), target)
    return {
        "changed": True,
        "from_version": current_version,
        "to_version": target_version_resolved,
        "latest": target,
    }
