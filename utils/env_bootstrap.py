#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Environment file bootstrap helpers for startup scripts.

Goals:
1. Generate `.env` with real line breaks (never literal `\\n` sequences).
2. Repair previously malformed one-line `.env` files containing escaped newlines.
"""

from __future__ import annotations

import argparse
import pathlib
import secrets
from typing import Dict, Tuple


def build_default_env_lines() -> Tuple[list[str], Dict[str, str]]:
    """Build default `.env` lines and generated credentials."""
    flask_secret = secrets.token_urlsafe(48)
    admin_password = secrets.token_urlsafe(16)
    admin_token = secrets.token_urlsafe(32)
    lines = [
        "# Auto-generated .env for Diff Platform",
        "HOST=0.0.0.0",
        "PORT=8002",
        f"FLASK_SECRET_KEY={flask_secret}",
        "ADMIN_USERNAME=admin",
        f"ADMIN_PASSWORD={admin_password}",
        f"ADMIN_API_TOKEN={admin_token}",
        "ENABLE_ADMIN_SECURITY=true",
        "AUTH_DEBUG_MODE=false",
        "DB_BACKEND=sqlite",
        "DEBUG_LOG=false",
        "BRANCH_REFRESH_COOLDOWN_SECONDS=120",
    ]
    creds = {
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": admin_password,
        "ADMIN_API_TOKEN": admin_token,
    }
    return lines, creds


def render_env_text(lines: list[str]) -> str:
    """Render `.env` text using real newlines."""
    return "\n".join(lines) + "\n"


def is_escaped_newline_malformed_env(text: str) -> bool:
    """Detect malformed `.env` content written as a single line with `\\n` literals."""
    if "\\n" not in text:
        return False
    # Correct env files contain many real line breaks; malformed historical output usually has <= 1.
    return text.count("\n") <= 1


def repair_escaped_newline_env(env_path: pathlib.Path) -> bool:
    """Repair malformed `.env` content in-place. Returns True when file changed."""
    if not env_path.exists():
        return False
    text = env_path.read_text(encoding="utf-8")
    if not is_escaped_newline_malformed_env(text):
        return False
    fixed = text.replace("\\n", "\n")
    if not fixed.endswith("\n"):
        fixed += "\n"
    env_path.write_text(fixed, encoding="utf-8")
    return True


def ensure_env_file(env_path: pathlib.Path) -> Tuple[str, Dict[str, str]]:
    """Ensure `.env` exists and is valid.

    Returns:
    - action: one of `generated`, `repaired`, `ok`
    - creds: generated credentials only when action is `generated`, otherwise empty dict
    """
    if not env_path.exists():
        lines, creds = build_default_env_lines()
        env_path.write_text(render_env_text(lines), encoding="utf-8")
        return "generated", creds

    repaired = repair_escaped_newline_env(env_path)
    if repaired:
        return "repaired", {}
    return "ok", {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure .env file exists and is valid.")
    parser.add_argument("--env-path", default=".env", help="Path to env file (default: .env)")
    args = parser.parse_args()

    env_path = pathlib.Path(args.env_path)
    action, creds = ensure_env_file(env_path)

    if action == "generated":
        print("[INFO] .env generated successfully.")
        print(f"  ADMIN_USERNAME={creds['ADMIN_USERNAME']}")
        print(f"  ADMIN_PASSWORD={creds['ADMIN_PASSWORD']}")
        print(f"  ADMIN_API_TOKEN={creds['ADMIN_API_TOKEN']}")
    elif action == "repaired":
        print("[INFO] Detected malformed .env with escaped newline literals; repaired in-place.")
    else:
        print("[INFO] .env already exists and format is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
