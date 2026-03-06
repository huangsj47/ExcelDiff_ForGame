#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 agent 目录为 zip。"""

from __future__ import annotations

import os
import zipfile
from datetime import datetime


def should_skip_rel(rel_path: str) -> bool:
    lower = rel_path.replace("\\", "/").lower()
    name = os.path.basename(lower)

    if "/__pycache__/" in f"/{lower}/":
        return True
    if name.endswith(".pyc"):
        return True
    if name.endswith(".swp"):
        return True
    if name == ".env":
        return True
    if name == "打包agent.bat":
        return True
    if name.startswith("agent_package_") and name.endswith(".zip"):
        return True
    return False


def build():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    out_name = f"agent_package_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    out_path = os.path.join(base_dir, out_name)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d.lower() != "venv"]
            for name in files:
                abs_path = os.path.join(root, name)
                rel_path = os.path.relpath(abs_path, base_dir)
                if should_skip_rel(rel_path):
                    continue
                zf.write(abs_path, arcname=rel_path)

    print(out_path)


if __name__ == "__main__":
    build()
