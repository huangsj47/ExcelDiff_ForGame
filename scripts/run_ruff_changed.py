#!/usr/bin/env python3
"""Run ruff on changed Python files and gate only changed lines."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _run_git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git command failed")
    return proc.stdout


def _normalize_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip()


def _extract_changed_lines_from_patch(patch_text: str) -> dict[str, set[int]]:
    changed_lines: dict[str, set[int]] = {}
    current_file: str | None = None
    new_line_no: int | None = None

    for raw_line in str(patch_text or "").splitlines():
        header_match = _DIFF_HEADER_RE.match(raw_line)
        if header_match:
            current_file = _normalize_path(header_match.group(2))
            new_line_no = None
            if current_file.endswith(".py"):
                changed_lines.setdefault(current_file, set())
            continue

        if not current_file or not current_file.endswith(".py"):
            continue

        hunk_match = _HUNK_RE.match(raw_line)
        if hunk_match:
            new_line_no = int(hunk_match.group(1))
            continue

        if new_line_no is None:
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            changed_lines[current_file].add(new_line_no)
            new_line_no += 1
            continue

        if raw_line.startswith("-") and not raw_line.startswith("---"):
            continue

        if raw_line.startswith("\\ No newline at end of file"):
            continue

        new_line_no += 1

    return changed_lines


def _list_untracked_files() -> list[str]:
    try:
        raw = _run_git(["ls-files", "--others", "--exclude-standard"])
    except RuntimeError:
        return []
    return [_normalize_path(line) for line in raw.splitlines() if line.strip()]


def _list_changed_files(base_ref: str | None) -> list[str]:
    candidates = []
    if base_ref:
        candidates.append(["diff", "--name-only", "--diff-filter=ACMRTUXB", f"{base_ref}...HEAD"])
    candidates.extend(
        [
            ["diff", "--name-only", "--cached", "--diff-filter=ACMRTUXB"],
            ["diff", "--name-only", "--diff-filter=ACMRTUXB"],
        ]
    )
    for command in candidates:
        try:
            raw = _run_git(command)
        except RuntimeError:
            continue
        files = [_normalize_path(line) for line in raw.splitlines() if line.strip()]
        if files:
            return files
    return []


def _filter_python_files(paths: list[str]) -> list[str]:
    result = []
    for path in paths:
        normalized = _normalize_path(path)
        if not normalized.endswith(".py"):
            continue
        if not Path(normalized).exists():
            continue
        result.append(normalized)
    return sorted(set(result))


def _list_changed_line_map(base_ref: str | None) -> dict[str, set[int] | None]:
    changed_line_map: dict[str, set[int] | None] = {}
    patch_commands = []
    if base_ref:
        patch_commands.append(["diff", "--unified=0", "--diff-filter=ACMRTUXB", f"{base_ref}...HEAD"])
    else:
        patch_commands.extend(
            [
                ["diff", "--unified=0", "--cached", "--diff-filter=ACMRTUXB"],
                ["diff", "--unified=0", "--diff-filter=ACMRTUXB"],
            ]
        )

    for command in patch_commands:
        try:
            patch_text = _run_git(command)
        except RuntimeError:
            continue
        parsed = _extract_changed_lines_from_patch(patch_text)
        for file_path, line_set in parsed.items():
            existing = changed_line_map.get(file_path)
            if existing is None and file_path in changed_line_map:
                continue
            if existing is None:
                changed_line_map[file_path] = set(line_set)
            else:
                existing.update(line_set)

    for item in _list_untracked_files():
        if item.endswith(".py") and Path(item).exists():
            # New file: treat every lint issue as newly introduced.
            changed_line_map[item] = None

    return changed_line_map


def _run_ruff_json(py_files: list[str]) -> tuple[int, list[dict[str, Any]]]:
    cmd = [sys.executable, "-m", "ruff", "check", "--output-format", "json", *py_files]
    print(f"▶ {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    raw_output = str(proc.stdout or "").strip()
    if not raw_output:
        return proc.returncode, []
    try:
        parsed = json.loads(raw_output)
    except json.JSONDecodeError:
        # Fall back to raw output for unexpected runtime errors.
        print(raw_output)
        return proc.returncode, []
    if not isinstance(parsed, list):
        return proc.returncode, []
    return proc.returncode, parsed


def _filter_ruff_issues_by_changed_lines(
    issues: list[dict[str, Any]],
    changed_lines: dict[str, set[int] | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    for issue in issues:
        filename = _normalize_path(issue.get("filename") or "")
        if not filename or filename not in changed_lines:
            kept.append(issue)
            continue

        allowed_lines = changed_lines.get(filename)
        if allowed_lines is None:
            kept.append(issue)
            continue

        location = issue.get("location") or {}
        try:
            row = int(location.get("row") or 0)
        except (TypeError, ValueError):
            kept.append(issue)
            continue

        if row in allowed_lines:
            kept.append(issue)
        else:
            ignored.append(issue)

    return kept, ignored


def _print_issues(issues: list[dict[str, Any]]) -> None:
    for issue in issues:
        filename = _normalize_path(issue.get("filename") or "")
        location = issue.get("location") or {}
        row = location.get("row") or 0
        col = location.get("column") or 0
        code = issue.get("code") or "UNKNOWN"
        message = str(issue.get("message") or "").strip()
        print(f"{filename}:{row}:{col}: {code} {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ruff on changed Python files.")
    parser.add_argument(
        "--base-ref",
        default=os.environ.get("RUFF_BASE_REF", ""),
        help="Git base ref for PR/CI diff; omit for local staged/working-tree changes",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run on full repository (ruff check .)",
    )
    args = parser.parse_args()

    try:
        _run_git(["rev-parse", "--is-inside-work-tree"])
    except RuntimeError as exc:
        print(f"❌ not a git repository: {exc}")
        return 2

    if args.all:
        cmd = [sys.executable, "-m", "ruff", "check", "."]
        print(f"▶ {' '.join(cmd)}")
        return subprocess.run(cmd, check=False).returncode

    base_ref = str(args.base_ref or "").strip() or None
    changed = _list_changed_files(base_ref)
    py_files = _filter_python_files(changed + _list_untracked_files())
    if not py_files:
        print("✅ no changed Python files, skip ruff")
        return 0

    print(f"ℹ️ ruff target files ({len(py_files)}):")
    for item in py_files:
        print(f"  - {item}")

    changed_line_map = _list_changed_line_map(base_ref)
    for item in py_files:
        changed_line_map.setdefault(item, None)

    return_code, issues = _run_ruff_json(py_files)
    if return_code not in {0, 1}:
        return return_code

    if not issues:
        print("✅ ruff clean on changed files")
        return 0

    kept, ignored = _filter_ruff_issues_by_changed_lines(issues, changed_line_map)
    if ignored:
        print(f"ℹ️ ignored existing diagnostics outside changed lines: {len(ignored)}")

    if not kept:
        print("✅ no new lint diagnostics on changed lines")
        return 0

    _print_issues(kept)
    print(f"❌ lint diagnostics on changed lines: {len(kept)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
