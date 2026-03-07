#!/usr/bin/env python3
"""
Run ruff on changed Python files only.

Use cases:
  - CI: block newly introduced lint issues without forcing one-shot cleanup.
  - Local: quick feedback on current change set.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


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
    return proc.stdout.strip()


def _list_changed_files(base_ref: str | None) -> list[str]:
    candidates = []
    # CI / PR mode: compare against provided base ref.
    if base_ref:
        candidates.append(["diff", "--name-only", "--diff-filter=ACMRTUXB", f"{base_ref}...HEAD"])
    # Local mode: prefer staged + working tree changes.
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
        files = [line.strip() for line in raw.splitlines() if line.strip()]
        if files:
            return files
    return []


def _filter_python_files(paths: list[str]) -> list[str]:
    result = []
    for path in paths:
        normalized = path.replace("\\", "/")
        if not normalized.endswith(".py"):
            continue
        if not Path(normalized).exists():
            continue
        result.append(normalized)
    return sorted(set(result))


def _list_untracked_files() -> list[str]:
    try:
        raw = _run_git(["ls-files", "--others", "--exclude-standard"])
    except RuntimeError:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


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

    cmd = [sys.executable, "-m", "ruff", "check", *py_files]
    print(f"▶ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
