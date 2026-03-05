#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rollback agent latest release manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[1])


def _append_repo_root_to_syspath():
    root = _repo_root()
    if root not in sys.path:
        sys.path.insert(0, root)


def main():
    parser = argparse.ArgumentParser(description="Rollback latest agent release.")
    parser.add_argument("--target-version", default="", help="rollback to this version explicitly")
    parser.add_argument("--steps", type=int, default=1, help="rollback N steps when target version is not specified")
    args = parser.parse_args()

    _append_repo_root_to_syspath()
    from services.agent_release_service import rollback_latest_release

    try:
        result = rollback_latest_release(
            target_version=args.target_version or None,
            steps=max(1, int(args.steps or 1)),
        )
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        f"\nRollback release: from={result.get('from_version')} -> to={result.get('to_version')}, "
        f"changed={result.get('changed')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
