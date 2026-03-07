"""Repository compare handlers extracted from app.py."""

from __future__ import annotations

from datetime import datetime

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

from services.model_loader import get_runtime_model, get_runtime_models
from utils.request_security import _has_project_access
from utils.timezone_utils import format_beijing_time


def _ensure_repository_access_or_403(repository):
    project = getattr(repository, "project", None)
    if project is None:
        abort(404)
    if not _has_project_access(project.id):
        abort(403)
    return project


def _ensure_commit_access_or_403(commit):
    repository = getattr(commit, "repository", None)
    _ensure_repository_access_or_403(repository)
    return repository


def repository_compare():
    """Repository compare page."""
    Repository, Commit = get_runtime_models("Repository", "Commit")

    source_repo_id = request.args.get("source")
    target_repo_id = request.args.get("target")
    start_time = request.args.get("start_time")
    end_time = request.args.get("end_time")
    interval_minutes = int(request.args.get("interval", 5))

    if not source_repo_id or not target_repo_id:
        flash("请选择要对比的仓库", "error")
        return redirect(url_for("index"))

    source_repo = Repository.query.get_or_404(source_repo_id)
    target_repo = Repository.query.get_or_404(target_repo_id)
    _ensure_repository_access_or_403(source_repo)
    _ensure_repository_access_or_403(target_repo)

    try:
        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except Exception:
        flash("时间格式错误", "error")
        return redirect(url_for("index"))

    source_commits = (
        Commit.query.filter(
            Commit.repository_id == source_repo_id,
            Commit.commit_time >= start_dt,
            Commit.commit_time <= end_dt,
        )
        .order_by(Commit.commit_time.desc())
        .all()
    )
    target_commits = (
        Commit.query.filter(
            Commit.repository_id == target_repo_id,
            Commit.commit_time >= start_dt,
            Commit.commit_time <= end_dt,
        )
        .order_by(Commit.commit_time.desc())
        .all()
    )

    comparison_result = analyze_repository_differences(
        source_commits,
        target_commits,
        source_repo,
        target_repo,
        interval_minutes,
    )

    return render_template(
        "repository_compare.html",
        source_repo=source_repo,
        target_repo=target_repo,
        start_time=start_dt,
        end_time=end_dt,
        interval_minutes=interval_minutes,
        comparison_result=comparison_result,
    )


def analyze_repository_differences(source_commits, target_commits, source_repo, target_repo, interval_minutes):
    """Analyze differences between two repositories."""
    source_files = {}
    target_files = {}

    for commit in source_commits:
        source_files.setdefault(commit.path, []).append(commit)

    for commit in target_commits:
        target_files.setdefault(commit.path, []).append(commit)

    differences = []
    all_files = set(source_files.keys()) | set(target_files.keys())

    for file_path in all_files:
        source_file_commits = source_files.get(file_path, [])
        target_file_commits = target_files.get(file_path, [])

        if not source_file_commits and target_file_commits:
            differences.append(
                {
                    "type": "target_only",
                    "file_path": file_path,
                    "target_commits": target_file_commits,
                    "description": f"文件只在{target_repo.name}中存在",
                }
            )
            continue

        if source_file_commits and not target_file_commits:
            differences.append(
                {
                    "type": "source_only",
                    "file_path": file_path,
                    "source_commits": source_file_commits,
                    "description": f"文件只在{source_repo.name}中存在",
                }
            )
            continue

        source_latest = max(source_file_commits, key=lambda item: item.commit_time)
        target_latest = max(target_file_commits, key=lambda item: item.commit_time)
        time_diff = abs((source_latest.commit_time - target_latest.commit_time).total_seconds() / 60)

        if time_diff <= interval_minutes:
            continue

        if source_latest.commit_time > target_latest.commit_time:
            differences.append(
                {
                    "type": "source_newer",
                    "file_path": file_path,
                    "source_commit": source_latest,
                    "target_commit": target_latest,
                    "time_diff_minutes": int(time_diff),
                    "description": f"{source_repo.name}中的版本更新（相差{int(time_diff)}分钟）",
                }
            )
        else:
            differences.append(
                {
                    "type": "target_newer",
                    "file_path": file_path,
                    "source_commit": source_latest,
                    "target_commit": target_latest,
                    "time_diff_minutes": int(time_diff),
                    "description": f"{target_repo.name}中的版本更新（相差{int(time_diff)}分钟）",
                }
            )

    return {
        "total_differences": len(differences),
        "differences": differences,
        "source_files_count": len(source_files),
        "target_files_count": len(target_files),
        "common_files_count": len(set(source_files.keys()) & set(target_files.keys())),
    }


def get_commits_by_file(repository_id):
    """Get all commit records for selected file path."""
    Commit, Repository = get_runtime_models("Commit", "Repository")

    file_path = request.args.get("path")
    if not file_path:
        return jsonify({"error": "文件路径不能为空"}), 400

    repository = Repository.query.get_or_404(repository_id)
    _ensure_repository_access_or_403(repository)

    commits = (
        Commit.query.filter(
            Commit.repository_id == repository_id,
            Commit.path == file_path,
        )
        .order_by(Commit.commit_time.desc())
        .all()
    )

    commits_data = []
    for commit in commits:
        commits_data.append(
            {
                "id": commit.id,
                "version": commit.version,
                "author": commit.author,
                "commit_time": format_beijing_time(commit.commit_time, "%Y-%m-%d %H:%M:%S") if commit.commit_time else "",
                "status": commit.status,
                "operation": commit.operation,
            }
        )

    return jsonify({"commits": commits_data})


def commits_compare():
    """Commit compare page."""
    (Commit,) = get_runtime_models("Commit")
    get_diff_data = get_runtime_model("get_diff_data")

    from_commit_id = request.args.get("from")
    to_commit_id = request.args.get("to")

    if not from_commit_id or not to_commit_id:
        flash("请指定要对比的提交", "error")
        return redirect(url_for("index"))

    from_commit = Commit.query.get_or_404(from_commit_id)
    to_commit = Commit.query.get_or_404(to_commit_id)
    from_repository = _ensure_commit_access_or_403(from_commit)
    to_repository = _ensure_commit_access_or_403(to_commit)

    if getattr(from_repository, "id", None) != getattr(to_repository, "id", None):
        flash("只能对比同一仓库的不同版本", "error")
        return redirect(url_for("commit_diff", commit_id=from_commit_id))

    if from_commit.path != to_commit.path:
        flash("只能对比同一文件的不同版本", "error")
        return redirect(url_for("commit_diff", commit_id=from_commit_id))

    from_diff_data = get_diff_data(from_commit)
    to_diff_data = get_diff_data(to_commit)
    compare_diff = generate_compare_diff(from_commit, to_commit, from_diff_data, to_diff_data)

    return render_template(
        "commits_compare.html",
        from_commit=from_commit,
        to_commit=to_commit,
        compare_diff=compare_diff,
        repository=from_commit.repository,
        project=from_commit.repository.project,
    )


def generate_compare_diff(from_commit, to_commit, from_diff_data, to_diff_data):
    """Generate compare diff between two commits."""
    log_print = get_runtime_model("log_print")
    active_git_processes = get_runtime_model("active_git_processes")

    try:
        repository = from_commit.repository
        if repository.type == "git":
            from services.threaded_git_service import ThreadedGitService

            service = ThreadedGitService(
                repository.url,
                repository.root_directory,
                repository.username,
                repository.token,
                repository,
                active_git_processes,
            )
            diff_data = service.get_commit_range_diff(
                from_commit.commit_id,
                to_commit.commit_id,
                from_commit.path,
            )
            if diff_data and diff_data.get("hunks"):
                diff_data["file_path"] = from_commit.path
                diff_data["from_commit"] = from_commit.version
                diff_data["to_commit"] = to_commit.version
                return diff_data

        return {
            "type": "code",
            "file_path": from_commit.path,
            "from_commit": from_commit.version,
            "to_commit": to_commit.version,
            "lines": [
                {
                    "type": "header",
                    "content": f"对比 {from_commit.version} 和 {to_commit.version}",
                    "old_line_number": None,
                    "new_line_number": None,
                },
                {
                    "type": "context",
                    "content": "无法获取详细diff信息",
                    "old_line_number": 1,
                    "new_line_number": 1,
                },
            ],
        }
    except Exception as exc:
        log_print(f"生成对比diff失败: {exc}")
        return {
            "type": "code",
            "file_path": from_commit.path,
            "from_commit": from_commit.version,
            "to_commit": to_commit.version,
            "lines": [
                {
                    "type": "header",
                    "content": f"对比 {from_commit.version} 和 {to_commit.version}",
                    "old_line_number": None,
                    "new_line_number": None,
                },
                {
                    "type": "context",
                    "content": f"diff生成失败: {exc}",
                    "old_line_number": 1,
                    "new_line_number": 1,
                },
            ],
        }

