#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 本地 auto_sync 任务处理。"""

from __future__ import annotations

import os
import re
import subprocess
from urllib.parse import quote, urlparse, urlunparse

_GIT_LOCK_FILES = (
    os.path.join(".git", "index.lock"),
    os.path.join(".git", "config.lock"),
    os.path.join(".git", "HEAD.lock"),
    os.path.join(".git", "packed-refs.lock"),
    os.path.join(".git", "shallow.lock"),
)


def execute_auto_sync(task: dict, settings):
    payload = task.get("payload") or {}
    repo_cfg = payload.get("repository") or {}
    repository_id = payload.get("repository_id") or repo_cfg.get("repository_id")
    if not repository_id:
        raise ValueError("auto_sync payload 缺少 repository_id")

    repo_type = str(repo_cfg.get("type") or "").strip().lower()
    if repo_type != "git":
        raise ValueError(f"暂仅支持 git 仓库, type={repo_type}")

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

    base_dir = os.path.abspath(settings.repos_base_dir)
    os.makedirs(base_dir, exist_ok=True)
    local_repo_dir = os.path.join(base_dir, f"repo_{repository_id}")

    auth_url = _build_auth_url(remote_url, username, token)
    _sync_repo(local_repo_dir, auth_url, branch)
    commits = _collect_commits(
        repo_dir=local_repo_dir,
        branch=branch,
        limit=limit,
        path_regex=path_regex,
        log_filter_regex=log_filter_regex,
        commit_filter=commit_filter,
    )

    summary = {
        "repository_id": repository_id,
        "commit_count": len(commits),
        "message": "auto_sync executed by agent",
    }
    result_payload = {
        "repository_id": repository_id,
        "commits": commits,
    }
    return "completed", summary, None, result_payload


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
