#!/usr/bin/env python3
"""
文件长度守卫脚本 — 检查 Python 文件是否超出推荐行数上限。

用法:
    python scripts/check_file_length.py
    python scripts/check_file_length.py --strict
    python scripts/check_file_length.py --strict app.py services/task_worker_service.py

规则说明:
    - WARNING 阈值 (默认 1800 行): 提醒开发者考虑拆分
    - ERROR   阈值 (默认 2000 行): 强烈建议新功能写入新文件或已有的较短文件
    - LEGACY_ALLOWLIST: 历史遗留超长文件，仅告警不阻断（用于渐进治理）
"""

import argparse
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
WARNING_THRESHOLD = 1800   # 行数 >= 此值时输出警告
ERROR_THRESHOLD   = 2000   # 行数 >= 此值时输出错误

# 排除的目录（相对于项目根目录）
EXCLUDED_DIRS = {
    "tests",
    "migrations",
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    ".git",
    "instance",
}

# 排除的具体文件（相对于项目根目录）
EXCLUDED_FILES = {
    "app.py.bak",
}

# 历史遗留超长文件白名单（仅用于 --strict 时渐进治理，不阻断当前提交）
LEGACY_OVERSIZE_ALLOWLIST = {
    "app.py",
    "services/git_service.py",
    "services/weekly_version_logic.py",
    "services/agent_management_handlers.py",
}

# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

def count_lines(filepath: Path) -> int:
    """统计文件行数，忽略编码错误。"""
    try:
        return sum(1 for _ in filepath.open("r", encoding="utf-8", errors="replace"))
    except Exception:
        return 0


def find_python_files(root: Path):
    """递归查找所有 .py 文件，跳过排除目录和文件。"""
    for dirpath, dirnames, filenames in os.walk(root):
        # 排除不需要检查的目录
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fname), root)
            if rel in EXCLUDED_FILES:
                continue
            yield Path(dirpath) / fname, rel.replace("\\", "/")


def _is_excluded_relpath(relpath: str) -> bool:
    parts = Path(relpath).parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    return relpath in EXCLUDED_FILES


def iter_target_python_files(root: Path, explicit_paths: list[str]):
    """返回待检查文件列表。支持 pre-commit 传入的显式文件列表。"""
    if not explicit_paths:
        yield from find_python_files(root)
        return

    seen = set()
    for raw in explicit_paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (root / candidate).resolve()
        else:
            candidate = candidate.resolve()

        if not candidate.exists() or candidate.is_dir() or candidate.suffix != ".py":
            continue

        try:
            relpath = candidate.relative_to(root).as_posix()
        except ValueError:
            # 不在仓库根目录下
            continue

        if _is_excluded_relpath(relpath):
            continue
        if relpath in seen:
            continue
        seen.add(relpath)
        yield candidate, relpath


def main():
    parser = argparse.ArgumentParser(description="检查 Python 文件行数是否超限")
    parser.add_argument(
        "--strict", action="store_true",
        help="如果有文件超出 ERROR 阈值，以退出码 1 退出",
    )
    parser.add_argument(
        "--warn-threshold", type=int, default=WARNING_THRESHOLD,
        help=f"WARNING 阈值行数 (默认 {WARNING_THRESHOLD})",
    )
    parser.add_argument(
        "--error-threshold", type=int, default=ERROR_THRESHOLD,
        help=f"ERROR 阈值行数 (默认 {ERROR_THRESHOLD})",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="可选：仅检查指定文件（支持相对路径/绝对路径，适配 pre-commit 传参）",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    warnings_list = []
    errors_list = []
    allowlisted_errors = []

    for filepath, relpath in sorted(iter_target_python_files(project_root, args.paths), key=lambda x: x[1]):
        lines = count_lines(filepath)
        if lines >= args.error_threshold:
            if relpath in LEGACY_OVERSIZE_ALLOWLIST:
                allowlisted_errors.append((relpath, lines))
            else:
                errors_list.append((relpath, lines))
        elif lines >= args.warn_threshold:
            warnings_list.append((relpath, lines))

    # --------------- 输出 ---------------
    print(f"📏 文件长度检查 (WARNING >= {args.warn_threshold} 行, ERROR >= {args.error_threshold} 行)")
    print(f"   项目根目录: {project_root}")
    print()

    if args.paths:
        print(f"   检查范围: 显式文件 {len(args.paths)} 个")
    else:
        print("   检查范围: 全仓库 Python 文件")
    print()

    if not warnings_list and not errors_list and not allowlisted_errors:
        print("✅ 所有文件均在推荐行数范围内，无需处理。")
        return 0

    if warnings_list:
        print(f"⚠️  WARNING — 以下文件较长，建议考虑拆分:")
        for relpath, lines in warnings_list:
            print(f"   {relpath}: {lines} 行")
        print()

    if errors_list:
        print(f"❌ ERROR — 以下文件过长，新增功能应写入新文件或已有的较短文件:")
        for relpath, lines in errors_list:
            print(f"   {relpath}: {lines} 行")
        print()
        print("💡 建议: 将独立的业务逻辑、路由处理器、工具函数拆分到 services/ 或 routes/ 目录中。")

    if allowlisted_errors:
        print("⏭️ LEGACY_ALLOWLIST — 以下历史超长文件已记录，当前不阻断:")
        for relpath, lines in allowlisted_errors:
            print(f"   {relpath}: {lines} 行")
        print()
        print("💡 建议: 逐步拆分这些文件后，再从 LEGACY_ALLOWLIST 中移除。")

    if args.strict and errors_list:
        print("\n🚫 --strict 模式: 检测到超限文件，退出码 1")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
