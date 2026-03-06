#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 本地 auto_sync 任务处理。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, urlunparse

try:
    from utils.path_security import build_repository_local_path
except Exception:  # pragma: no cover - 独立Agent目录运行时可能无平台utils模块
    build_repository_local_path = None

_GIT_LOCK_FILES = (
    os.path.join(".git", "index.lock"),
    os.path.join(".git", "config.lock"),
    os.path.join(".git", "HEAD.lock"),
    os.path.join(".git", "packed-refs.lock"),
    os.path.join(".git", "shallow.lock"),
)
_SAFE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_SVN_LOCK_KEYWORDS = (
    "e155004",
    "working copy locked",
    "is locked",
    "run 'svn cleanup'",
    "cleanup",
)


def _sanitize_segment(segment: str, fallback: str) -> str:
    raw = str(segment or "").strip()
    if _SAFE_SEGMENT_PATTERN.match(raw):
        return raw
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned or fallback


def _build_repo_local_path_fallback(project_code: str, repository_name: str, repository_id: int, base_dir: str) -> str:
    safe_project = _sanitize_segment(project_code, "project")
    safe_repo = _sanitize_segment(repository_name, "repository")
    safe_id = int(repository_id)
    base_abs = os.path.abspath(base_dir)
    candidate = os.path.abspath(os.path.join(base_abs, f"{safe_project}_{safe_repo}_{safe_id}"))
    if not (candidate == base_abs or candidate.startswith(base_abs + os.sep)):
        raise ValueError("Repository path escapes base directory")
    return candidate


def _resolve_local_repo_dir(base_dir: str, repository_id: int, project_code: str, repository_name: str) -> str:
    legacy_repo_dir = os.path.join(base_dir, f"repo_{repository_id}")
    use_named_dir = bool(project_code and repository_name)
    if not use_named_dir:
        return legacy_repo_dir

    named_repo_dir = None
    if build_repository_local_path:
        try:
            named_repo_dir = build_repository_local_path(
                project_code,
                repository_name,
                repository_id,
                base_dir=base_dir,
                strict=False,
            )
        except Exception:
            named_repo_dir = None
    if not named_repo_dir:
        named_repo_dir = _build_repo_local_path_fallback(project_code, repository_name, repository_id, base_dir)

    if os.path.isdir(legacy_repo_dir) and not os.path.exists(named_repo_dir):
        os.makedirs(os.path.dirname(named_repo_dir), exist_ok=True)
        try:
            shutil.move(legacy_repo_dir, named_repo_dir)
        except Exception:
            return legacy_repo_dir

    if os.path.isdir(named_repo_dir):
        return named_repo_dir
    if os.path.isdir(legacy_repo_dir):
        return legacy_repo_dir
    return named_repo_dir


def execute_auto_sync(task: dict, settings):
    payload = task.get("payload") or {}
    repo_cfg = payload.get("repository") or {}
    repository_id = payload.get("repository_id") or repo_cfg.get("repository_id")
    if not repository_id:
        raise ValueError("auto_sync payload 缺少 repository_id")

    repo_type = str(repo_cfg.get("type") or "").strip().lower()
    if repo_type not in {"git", "svn"}:
        raise ValueError(f"暂仅支持 git/svn 仓库, type={repo_type}")

    remote_url = str(repo_cfg.get("url") or "").strip()
    if not remote_url:
        raise ValueError("auto_sync payload 缺少 repository.url")

    username = repo_cfg.get("username")
    token = repo_cfg.get("token")
    branch = (repo_cfg.get("branch") or "").strip() or None
    path_regex = repo_cfg.get("path_regex")
    log_filter_regex = repo_cfg.get("log_filter_regex")
    commit_filter = repo_cfg.get("commit_filter")
    limit = int(payload.get("limit") or 300)
    limit = max(50, min(2000, limit))
    force_reclone = bool(payload.get("force_reclone"))
    force_repair_update = bool(payload.get("force_repair_update")) and not force_reclone

    base_dir = os.path.abspath(settings.repos_base_dir)
    os.makedirs(base_dir, exist_ok=True)
    project_code = str(
        repo_cfg.get("project_code")
        or payload.get("project_code")
        or ""
    ).strip()
    repository_name = str(
        repo_cfg.get("repository_name")
        or payload.get("repository_name")
        or ""
    ).strip()
    local_repo_dir = _resolve_local_repo_dir(
        base_dir=base_dir,
        repository_id=int(repository_id),
        project_code=project_code,
        repository_name=repository_name,
    )

    if repo_type == "git":
        auth_url = _build_auth_url(remote_url, username, token)
        if force_reclone:
            _remove_repo_dir_for_reclone(local_repo_dir)
        elif force_repair_update and os.path.isdir(os.path.join(local_repo_dir, ".git")):
            _self_heal_repo(local_repo_dir, branch)
        _sync_repo(local_repo_dir, auth_url, branch)
        commits = _collect_commits(
            repo_dir=local_repo_dir,
            branch=branch,
            limit=limit,
            path_regex=path_regex,
            log_filter_regex=log_filter_regex,
            commit_filter=commit_filter,
        )
    else:
        password = repo_cfg.get("password") or repo_cfg.get("token")
        current_version = repo_cfg.get("current_version")
        if force_reclone:
            _remove_repo_dir_for_reclone(local_repo_dir)
        elif force_repair_update and os.path.isdir(os.path.join(local_repo_dir, ".svn")):
            _self_heal_svn_repo(local_repo_dir, username, password)
        _sync_svn_repo(local_repo_dir, remote_url, username, password)
        commits = _collect_svn_commits(
            repo_dir=local_repo_dir,
            limit=limit,
            path_regex=path_regex,
            log_filter_regex=log_filter_regex,
            commit_filter=commit_filter,
            username=username,
            password=password,
            current_version=current_version,
        )

    summary = {
        "repository_id": repository_id,
        "repository_type": repo_type,
        "commit_count": len(commits),
        "message": "auto_sync executed by agent",
        "retry_strategy": "force_reclone" if force_reclone else ("force_repair_update" if force_repair_update else "default"),
    }
    result_payload = {
        "repository_id": repository_id,
        "commits": commits,
    }
    return "completed", summary, None, result_payload


def _remove_repo_dir_for_reclone(local_repo_dir: str):
    target = os.path.abspath(str(local_repo_dir or "").strip())
    if not target:
        return
    if not os.path.exists(target):
        return

    try:
        shutil.rmtree(target, ignore_errors=False)
    except Exception:
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "rmdir", "/s", "/q", target],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            shutil.rmtree(target, ignore_errors=True)

    if os.path.exists(target):
        raise RuntimeError(f"failed to remove local repo dir before reclone: {target}")


def _build_auth_url(url: str, username: str | None, token: str | None):
    parsed = urlparse(url)
    if not token or parsed.scheme not in ("http", "https"):
        return url

    user = username or "oauth2"
    netloc = f"{quote(str(user), safe='')}:{quote(str(token), safe='')}@{parsed.netloc}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _run_git(cmd, cwd=None, timeout=120):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git cmd failed: {' '.join(cmd)} | stderr={result.stderr.strip()}")
    return result.stdout


def _cleanup_git_lock_files(local_repo_dir: str):
    removed = []
    for relative in _GIT_LOCK_FILES:
        target = os.path.join(local_repo_dir, relative)
        if not os.path.exists(target):
            continue
        try:
            os.remove(target)
            removed.append(relative.replace("\\", "/"))
        except Exception:
            continue
    return removed


def _ensure_branch_checked_out(local_repo_dir: str, branch: str | None):
    if not branch:
        return
    try:
        _run_git(["git", "checkout", branch], cwd=local_repo_dir, timeout=90)
    except Exception:
        _run_git(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=local_repo_dir, timeout=90)


def _self_heal_repo(local_repo_dir: str, branch: str | None):
    _cleanup_git_lock_files(local_repo_dir)
    try:
        _run_git(["git", "reset", "--hard", "HEAD"], cwd=local_repo_dir, timeout=120)
    except Exception:
        pass
    try:
        _run_git(["git", "clean", "-fd"], cwd=local_repo_dir, timeout=120)
    except Exception:
        pass
    try:
        _run_git(["git", "gc", "--prune=now"], cwd=local_repo_dir, timeout=180)
    except Exception:
        pass
    try:
        _run_git(["git", "fetch", "--all", "--prune"], cwd=local_repo_dir, timeout=300)
    except Exception:
        pass
    _ensure_branch_checked_out(local_repo_dir, branch)


def _sync_existing_repo(local_repo_dir: str, remote_url: str, branch: str | None):
    attempt_error = None
    for attempt in range(2):
        try:
            _cleanup_git_lock_files(local_repo_dir)
            _run_git(["git", "remote", "set-url", "origin", remote_url], cwd=local_repo_dir, timeout=60)
            _run_git(["git", "fetch", "--all", "--prune"], cwd=local_repo_dir, timeout=300)
            _ensure_branch_checked_out(local_repo_dir, branch)
            if branch:
                _run_git(["git", "pull", "--no-rebase", "origin", branch], cwd=local_repo_dir, timeout=300)
            else:
                _run_git(["git", "pull", "--no-rebase", "origin"], cwd=local_repo_dir, timeout=300)
            return
        except Exception as exc:
            attempt_error = exc
            if attempt == 0:
                _self_heal_repo(local_repo_dir, branch)
                continue
            raise
    if attempt_error:
        raise attempt_error


def _sync_repo(local_repo_dir: str, remote_url: str, branch: str | None):
    git_dir = os.path.join(local_repo_dir, ".git")
    if not os.path.isdir(git_dir):
        os.makedirs(os.path.dirname(local_repo_dir), exist_ok=True)
        clone_cmd = ["git", "clone"]
        if branch:
            clone_cmd.extend(["--branch", branch])
        clone_cmd.extend([remote_url, local_repo_dir])
        _run_git(clone_cmd, timeout=600)
        return

    _sync_existing_repo(local_repo_dir, remote_url, branch)


def _collect_commits(*, repo_dir, branch, limit, path_regex, log_filter_regex, commit_filter):
    cmd = [
        "git",
        "log",
        f"-n{limit}",
        "--date=iso-strict",
        "--name-status",
        "--pretty=format:@@@%H|%an|%ae|%cI|%s",
    ]
    if branch:
        cmd.append(branch)

    raw = _run_git(cmd, cwd=repo_dir, timeout=300)
    lines = raw.splitlines()

    path_re = re.compile(path_regex) if path_regex else None
    msg_re = re.compile(log_filter_regex) if log_filter_regex else None
    blocked_emails = {
        item.strip().lower()
        for item in str(commit_filter or "").split(",")
        if item.strip()
    }

    commits = []
    current = None
    skip_current = False

    for line in lines:
        if line.startswith("@@@"):
            header = line[3:]
            parts = header.split("|", 4)
            if len(parts) < 5:
                current = None
                skip_current = True
                continue
            commit_id, author, author_email, commit_time, message = parts
            current = {
                "commit_id": commit_id.strip(),
                "author": author.strip(),
                "author_email": author_email.strip(),
                "commit_time": commit_time.strip(),
                "message": message.strip(),
            }
            skip_current = False
            if blocked_emails and current["author_email"].lower() in blocked_emails:
                skip_current = True
            if msg_re and msg_re.search(current["message"]):
                skip_current = True
            continue

        if not current or skip_current:
            continue
        if not line.strip():
            continue

        parts = line.split("\t")
        if len(parts) < 2:
            continue

        status_code = (parts[0] or "").strip().upper()
        file_path = parts[-1].strip()
        if not file_path:
            continue

        if path_re and not path_re.search(file_path):
            continue

        if status_code.startswith("A"):
            op = "A"
        elif status_code.startswith("D"):
            op = "D"
        else:
            op = "M"

        commits.append(
            {
                "commit_id": current["commit_id"],
                "version": current["commit_id"][:8],
                "path": file_path,
                "operation": op,
                "author": current["author"],
                "author_email": current["author_email"],
                "commit_time": current["commit_time"],
                "message": current["message"],
            }
        )

    return commits


def _decode_output(byte_output):
    if not byte_output:
        return ""
    for encoding in ("utf-8", "gbk", "cp936", "latin1"):
        try:
            return byte_output.decode(encoding)
        except Exception:
            continue
    return byte_output.decode("utf-8", errors="ignore")


def _build_svn_auth_args(username: str | None, password: str | None):
    args = []
    if username:
        args.extend(["--username", str(username)])
    if password:
        args.extend(["--password", str(password)])
    args.extend(["--non-interactive", "--trust-server-cert"])
    return args


def _run_svn(cmd, cwd=None, timeout=180):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=False,
        timeout=timeout,
    )
    stdout = _decode_output(result.stdout)
    stderr = _decode_output(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"svn cmd failed: {' '.join(cmd[:3])} ... | stderr={stderr.strip()}")
    return stdout


def _is_svn_lock_error(error_text: str):
    lowered = str(error_text or "").lower()
    return any(keyword in lowered for keyword in _SVN_LOCK_KEYWORDS)


def _self_heal_svn_repo(local_repo_dir: str, username: str | None, password: str | None):
    cleanup_cmd = ["svn", "cleanup", local_repo_dir] + _build_svn_auth_args(username, password)
    revert_cmd = ["svn", "revert", "-R", local_repo_dir] + _build_svn_auth_args(username, password)
    try:
        _run_svn(cleanup_cmd, timeout=180)
    except Exception:
        pass
    try:
        _run_svn(revert_cmd, timeout=240)
    except Exception:
        pass


def _sync_existing_svn_repo(local_repo_dir: str, username: str | None, password: str | None):
    last_error = None
    for attempt in range(2):
        try:
            cleanup_cmd = ["svn", "cleanup", local_repo_dir] + _build_svn_auth_args(username, password)
            try:
                _run_svn(cleanup_cmd, timeout=120)
            except Exception:
                pass
            update_cmd = ["svn", "update", local_repo_dir] + _build_svn_auth_args(username, password)
            _run_svn(update_cmd, cwd=local_repo_dir, timeout=600)
            return
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                if _is_svn_lock_error(str(exc)):
                    _self_heal_svn_repo(local_repo_dir, username, password)
                    continue
                _self_heal_svn_repo(local_repo_dir, username, password)
                continue
            raise
    if last_error:
        raise last_error


def _sync_svn_repo(local_repo_dir: str, remote_url: str, username: str | None, password: str | None):
    svn_dir = os.path.join(local_repo_dir, ".svn")
    if not os.path.isdir(svn_dir):
        if os.path.isdir(local_repo_dir):
            _remove_repo_dir_for_reclone(local_repo_dir)
        os.makedirs(os.path.dirname(local_repo_dir), exist_ok=True)
        checkout_cmd = ["svn", "checkout", remote_url, local_repo_dir] + _build_svn_auth_args(username, password)
        _run_svn(checkout_cmd, timeout=900)
        return
    _sync_existing_svn_repo(local_repo_dir, username, password)


def _normalize_svn_revision(raw_revision):
    text = str(raw_revision or "").strip()
    if not text:
        return None
    if text.lower().startswith("r"):
        text = text[1:]
    if text.isdigit():
        return text
    return None


def _normalize_iso_datetime(date_text: str):
    text = str(date_text or "").strip()
    if not text:
        return ""
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(date_text or "").strip()


def _collect_svn_commits(
    *,
    repo_dir,
    limit,
    path_regex,
    log_filter_regex,
    commit_filter,
    username,
    password,
    current_version,
):
    cmd = ["svn", "log", "--xml", "-v", f"-l{limit}", repo_dir]
    normalized_revision = _normalize_svn_revision(current_version)
    if normalized_revision:
        cmd.extend(["-r", f"{normalized_revision}:HEAD"])
    cmd.extend(_build_svn_auth_args(username, password))

    xml_text = _run_svn(cmd, cwd=repo_dir, timeout=600)
    root = ET.fromstring(xml_text)

    path_re = re.compile(path_regex) if path_regex else None
    msg_re = re.compile(log_filter_regex) if log_filter_regex else None
    blocked_authors = {
        item.strip().lower()
        for item in str(commit_filter or "").split(",")
        if item.strip()
    }

    commits = []
    for logentry in root.findall("logentry"):
        revision = str(logentry.get("revision") or "").strip()
        if not revision:
            continue
        author = (logentry.findtext("author") or "").strip()
        message = (logentry.findtext("msg") or "").strip()
        commit_time = _normalize_iso_datetime(logentry.findtext("date") or "")

        if blocked_authors and author.lower() in blocked_authors:
            continue
        if msg_re and msg_re.search(message):
            continue

        paths_node = logentry.find("paths")
        if paths_node is None:
            continue

        for path_node in paths_node.findall("path"):
            file_path = str(path_node.text or "").strip()
            if not file_path:
                continue
            if path_re and not path_re.search(file_path):
                continue

            action = str(path_node.get("action") or "M").strip().upper()
            if action.startswith("A"):
                operation = "A"
            elif action.startswith("D"):
                operation = "D"
            else:
                operation = "M"

            commits.append(
                {
                    "commit_id": f"r{revision}",
                    "version": revision,
                    "path": file_path,
                    "operation": operation,
                    "author": author,
                    "author_email": "",
                    "commit_time": commit_time,
                    "message": message,
                }
            )
    return commits
