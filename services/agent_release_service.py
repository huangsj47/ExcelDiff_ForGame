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

from utils.logger import log_print

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_JSON_PARSE_ERRORS = (json.JSONDecodeError, OSError, ValueError)
_SUBPROCESS_DETECT_ERRORS = (OSError, ValueError, subprocess.SubprocessError)


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


def _safe_join_within(base_dir: str, *parts: str) -> str:
    """在 `base_dir` **之内**安全拼接路径，越界（含「就是 base 本身」）即抛 `ValueError`。

    用 realpath + commonpath 判定**最终落点**，而不是「黑名单里有没有 `..`」——
    黑名单漏判的形式太多（`....//`、URL 编码、Windows 盘符、UNC、软链接）。

    判据比版本号正则更靠得住，所以它同时兜住两件事：版本号将来被放宽、
    以及**清单文件里的 `package_file` 字段**（那是从磁盘读进来的数据，
    换过一份清单就可能带出 `../` 之类的东西）。

    【为什么连 base 本身也拒】Agent 侧那个同名 helper 是允许 `candidate == base` 的
    （它靠版本号正则挡「`.`」）。这里**更严一档**：本函数的每个调用方都至少传一个
    part，落到 base 本身只可能是 `"."` / 空串这类退化输入 —— 而 `_release_dir`
    返回 `releases/` 本身意味着 `rmtree` 会删掉**所有**已发布的版本。
    严的方向是安全的，不依赖上游那一层是否记得挡。
    """
    base_abs = os.path.realpath(os.path.abspath(str(base_dir)))
    candidate = os.path.realpath(os.path.join(base_abs, *[str(part) for part in parts]))
    if candidate == base_abs:
        raise ValueError(f"path resolves to the base directory itself: {candidate!r}")
    try:
        common = os.path.commonpath([base_abs, candidate])
    except ValueError:
        # 不同盘符 / 不同驱动器（Windows）—— 一定越界。
        raise ValueError(
            f"path escapes base directory: {candidate!r} not under {base_abs!r}"
        )
    if common != base_abs:
        raise ValueError(f"path escapes base directory: {candidate!r} not under {base_abs!r}")
    return candidate


def _release_dir(version: str) -> str:
    return _safe_join_within(_releases_dir(), version)


def _release_manifest_path(version: str) -> str:
    return _safe_join_within(_release_dir(version), "manifest.json")


def _release_package_name(version: str) -> str:
    return f"agent_release_{version}.zip"


def _release_package_path(version: str) -> str:
    return _safe_join_within(_release_dir(version), _release_package_name(version))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_version(version: str) -> str:
    """版本号 = **单一安全路径段**。

    这里是平台侧的最后一道口子：版本号会直接进 `os.path.join(releases, version)`，
    而 `publish_agent_release` 里紧跟着就是 `os.makedirs(...)` 与
    `shutil.rmtree(_release_dir(version))`（`force=True` 时）。

    原先是 `^[A-Za-z0-9._-]{1,64}$` —— 这个类**放行 `.` 与 `..`**（它们的字符全在
    类里），于是 `publish_agent_release(version="..")` 把 `manifest.json` /
    `latest.json` / 那个 zip 写到 `releases/` **之外**；带 `force=True` 时更狠：
    `rmtree(releases/..)` 删的是这个发布根目录的**父目录**。

    口径与 Agent 侧 `agent/self_update.py::_is_safe_release_version` **对齐**
    （那边一直是「首字符必须是字母数字」+ 显式挡 `.`/`..`）。平台是这套协议的服务端，
    判据不该比自己的客户端还松。
    """
    text = str(version or "").strip()
    if not text or not _VERSION_RE.match(text):
        raise ValueError(f"invalid release version: {version}")
    # 正则已经排除了 `.` / `..`（首字符必须是字母数字），这里再挡一次：
    # 与 Agent 侧同一句注释、同一个理由 —— 防的是**将来有人把正则放宽**。
    if text in {".", ".."}:
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
            except OSError:
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
    # `repos` 是节点仓库工作副本的当前默认目录名（见 agent/repo_paths.py），
    # `agent_repos` 是历史默认值/显式覆盖值 —— 工作副本绝不能打进发布包。
    if any(p in {"venv", ".venv", "agent_repos", "repos", "logs"} for p in parts):
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
    except _SUBPROCESS_DETECT_ERRORS:
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
    except _JSON_PARSE_ERRORS:
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "").strip()
    if not version:
        return None
    try:
        _safe_version(version)
    except ValueError:
        return None
    return data


def load_release_manifest(version: str) -> dict | None:
    try:
        resolved = _safe_version(version)
        # `_release_manifest_path` 里还有一层包含性校验（`_safe_join_within`）。
        # 它对**合法版本号**不会触发，但 `list_release_manifests` 是拿
        # `os.listdir(releases/)` 的目录名逐个来调的 —— 那个目录里如果有一个
        # **软链接**指向外部，realpath 之后就越界了。这时应当「这个版本读不了」，
        # 而不是让整张发布列表 500。
        path = _release_manifest_path(resolved)
    except ValueError:
        return None
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except _JSON_PARSE_ERRORS:
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("version") or "").strip() != resolved:
        return None
    return data


def get_release_package_path(version: str) -> str | None:
    """某个版本的 zip 包落在哪 —— 返回值一定是该版本目录**之内**的一个文件。

    `package_file` 是**从清单文件读进来的数据**，不是这里算出来的：清单与 zip 一起
    躺在磁盘上，换过一份清单就可能带出 `../../x.zip` 或绝对路径。所以拼完之后必须
    再走一次包含性校验（`_safe_join_within`），越界就当这个版本没有包 ——
    这里的调用方（`send_file`）拿到什么就会发什么，不能只靠版本号那一层。
    """
    manifest = load_release_manifest(version)
    if not manifest:
        return None
    package_name = str(manifest.get("package_file") or "").strip()
    if not package_name:
        return None
    try:
        resolved_version = _safe_version(version)
    except ValueError:
        return None
    try:
        package_path = _safe_join_within(_release_dir(resolved_version), package_name)
    except ValueError:
        log_print(
            f"⚠️ 发布清单里的 package_file 越界，忽略该版本: {version} | {package_name!r}",
            "AGENT",
            force=True,
        )
        return None
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
    except ValueError:
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
