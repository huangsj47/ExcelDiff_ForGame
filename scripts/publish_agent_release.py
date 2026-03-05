#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-command publish for agent release package."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[1])


def _append_repo_root_to_syspath():
    root = _repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)


def _git_is_dirty(repo_root: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    return bool((result.stdout or "").strip())


def main():
    parser = argparse.ArgumentParser(description="Publish an agent release package and update latest manifest.")
    parser.add_argument("--version", default="", help="release version (default: utc timestamp + commit short hash)")
    parser.add_argument("--notes", default="", help="release notes")
    parser.add_argument("--source-dir", default="", help="agent source directory (default: <repo>/agent)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing version")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="rollback latest release instead of publishing a new package",
    )
    parser.add_argument(
        "--rollback-target-version",
        default="",
        help="target release version for rollback (optional)",
    )
    parser.add_argument(
        "--rollback-steps",
        type=int,
        default=1,
        help="rollback N steps to older release when target version is not specified (default: 1)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow publishing when git working tree is dirty",
    )
    args = parser.parse_args()

    repo_root = _repo_root()
    if not args.allow_dirty and _git_is_dirty(repo_root):
        print("ERROR: git working tree is dirty. Commit first or use --allow-dirty.")
        return 2

    _append_repo_root_to_syspath()
    from services.agent_release_service import publish_agent_release, rollback_latest_release

    try:
        if args.rollback:
            result = rollback_latest_release(
                target_version=args.rollback_target_version or None,
                steps=max(1, int(args.rollback_steps or 1)),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(
                f"\nRollback release: from={result.get('from_version')} -> to={result.get('to_version')}, "
                f"changed={result.get('changed')}"
            )
            return 0

        manifest = publish_agent_release(
            version=args.version or None,
            notes=args.notes or None,
            source_dir=args.source_dir or None,
            force=bool(args.force),
        )
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    releases_root = os.environ.get("AGENT_RELEASES_DIR") or os.path.join(repo_root, "instance", "agent_releases")
    print(f"\nPublished release: version={manifest.get('version')}")
    print(f"Releases root: {os.path.abspath(releases_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
