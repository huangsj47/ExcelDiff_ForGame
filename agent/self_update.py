#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent self-update via platform release package."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
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
    # 节点的仓库工作副本目录：`repos` 是现在的默认值（见 agent/repo_paths.py），
    # `agent_repos` 是历史默认值/显式覆盖值。两者都要保护 —— 发布包不该覆盖或
    # 删除节点上的工作副本（否则每次自更新都要把每个仓库重新 clone 一遍）。
    "repos",
    "agent_repos",
    "logs",
    "__pycache__",
    _TEMP_UPDATE_DIR_NAME,
}

# 版本号只允许「字母数字开头 + 字母数字._-」。这一条就排除了：
# `../../..`、`/etc/cron.d`、`C:\Windows`、`..\..\` 等一切穿越/绝对路径写法。
_SAFE_RELEASE_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_RELEASE_VERSION_LENGTH = 128

# 发布包摘要必须是完整的 sha256 十六进制串。
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _agent_root() -> str:
    return str(Path(__file__).resolve().parent)


def _safe_join_within(base_dir: str, *parts: str) -> str:
    """在 `base_dir` 内安全拼接路径，越界即抛 RuntimeError。

    为什么 Agent 侧要用**自带**实现而不是 import 平台的 utils.path_security：
    与 `agent/handlers/auto_sync.py` 的 `_redact_secrets` 同一个原因 —— Agent 是
    独立部署的（`agent/` 可单独打包运行），平台 `utils` 未必存在；而
    `utils/path_security.py` 也**没有**通用的安全拼接函数（只有仓库路径专用
    的 `build_repository_local_path`）。安全校验绝不能因为「模块不存在」而
    静默失效，所以这里自包含实现。

    为什么不用「`..` 黑名单」而用 realpath + commonpath：黑名单漏判的形式太多
    （`....//`、URL 编码、Windows 盘符、UNC、软链接）。包含性校验把判断权交给
    文件系统本身，判定的是**最终落点**而不是字面量。
    """
    base_abs = os.path.realpath(os.path.abspath(str(base_dir)))
    candidate = os.path.realpath(os.path.join(base_abs, *[str(part) for part in parts]))
    if candidate != base_abs:
        try:
            common = os.path.commonpath([base_abs, candidate])
        except ValueError:
            # 不同盘符 / 不同驱动器（Windows）—— 一定越界。
            raise RuntimeError(f"path escapes base directory: {candidate!r} not under {base_abs!r}")
        if common != base_abs:
            raise RuntimeError(f"path escapes base directory: {candidate!r} not under {base_abs!r}")
    return candidate


def _is_safe_release_version(version: str) -> bool:
    """版本号必须是单一安全路径段（防目录穿越 / 绝对路径）。"""
    text = str(version or "").strip()
    if not text or len(text) > _MAX_RELEASE_VERSION_LENGTH:
        return False
    if not _SAFE_RELEASE_VERSION_PATTERN.match(text):
        return False
    # 正则已排除 `.` / `..`（首字符必须是字母数字），这里再挡一次以防正则被放宽。
    return text not in {".", ".."}


def _resolve_origin(url: str):
    """把 URL 归一成 `(scheme, host, port)`；非法或非 http(s) 返回 None。

    port 显式补全默认值，避免 `https://h` 与 `https://h:443` 被判定为不同源，
    也避免省略端口时绕过同源比较。
    """
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
    except Exception:
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        # 端口不是合法数字（例如 `http://host:abc/`）。
        return None
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    return scheme, host, port


def _is_same_origin(base_url: str, target_url: str) -> bool:
    """下载地址必须与平台 base URL 同源（scheme + host + port 全等）。

    自更新的下载请求会带上 `X-Agent-Token` / Agent 共享凭据。如果下载地址由
    服务端返回的 `download_path` 决定、且允许绝对 URL，那么一个被篡改的发布清单
    就能把集群凭据发给任意主机 —— 那是比「装到坏包」更严重的凭据外泄。
    """
    base_origin = _resolve_origin(base_url)
    target_origin = _resolve_origin(target_url)
    if base_origin is None or target_origin is None:
        return False
    return base_origin == target_origin


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
    if not _is_safe_release_version(version):
        # fail-closed：版本号直接参与 temp 目录拼接，绝不放行可疑值。
        log_func(f"拒绝自更新：发布版本号不合法 version={version!r}")
        return False, f"拒绝更新：发布版本号不合法 version={version!r}（只允许字母数字与 . _ -）"
    download_path = str(release.get("download_path") or "").strip()
    if not download_path:
        return False, "invalid download path"

    download_url = _join_platform_url(settings.platform_base_url, download_path)
    if not _is_same_origin(settings.platform_base_url, download_url):
        # 下载会带上 Agent 凭据，只允许发回平台自身。
        log_func(
            f"拒绝自更新：下载地址与平台不同源 download_url={download_url!r} "
            f"platform_base_url={settings.platform_base_url!r}"
        )
        return (
            False,
            f"拒绝更新：下载地址 {download_url!r} 与平台 {settings.platform_base_url!r} 不同源"
            "（自更新请求会携带 Agent 凭据，禁止发往第三方主机）",
        )

    try:
        temp_root = _safe_join_within(_agent_root(), _TEMP_UPDATE_DIR_NAME, version)
    except RuntimeError as path_exc:
        log_func(f"拒绝自更新：临时目录越界 version={version!r} error={path_exc}")
        return False, f"拒绝更新：版本号导致临时目录越界（{path_exc}）"

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

    # 完整性校验 fail-closed：旧实现写作 `if expect_sha256:`，清单里不写摘要就
    # 完全不校验 —— 一个能篡改发布清单的攻击者只要删掉这个字段即可投递任意包。
    expect_sha256 = str(release.get("package_sha256") or "").strip().lower()
    if not _SHA256_PATTERN.match(expect_sha256):
        log_func(f"拒绝自更新：发布清单缺少合法 package_sha256 value={expect_sha256!r}")
        return (
            False,
            "拒绝更新：发布清单缺少合法的 package_sha256（不能跳过完整性校验）",
        )
    real_sha256 = _sha256_file(package_path).lower()
    if real_sha256 != expect_sha256:
        log_func(f"拒绝自更新：安装包 sha256 校验失败 expected={expect_sha256} got={real_sha256}")
        return (
            False,
            f"拒绝更新：安装包 sha256 校验失败 expected={expect_sha256} got={real_sha256}",
        )

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
