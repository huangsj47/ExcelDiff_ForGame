#!/usr/bin/env python3
"""
文件长度守卫脚本 — 检查项目中的 Python 文件是否超出推荐行数上限。

用法:
    python scripts/check_file_length.py          # 检查所有 .py 文件
    python scripts/check_file_length.py --strict  # 超限时以非零退出码退出（可用于CI / pre-commit）

规则说明:
    - WARNING 阈值 (默认 1500 行): 提醒开发者考虑拆分
    - ERROR   阈值 (默认 2000 行): 强烈建议新功能写入新文件或已有的较短文件
    - 排除:  tests/ 目录、迁移脚本、第三方依赖
"""

import argparse
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
WARNING_THRESHOLD = 1500   # 行数 >= 此值时输出警告
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
            yield Path(dirpath) / fname, rel


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
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    warnings_list = []
    errors_list = []

    for filepath, relpath in sorted(find_python_files(project_root), key=lambda x: x[1]):
        lines = count_lines(filepath)
        if lines >= args.error_threshold:
            errors_list.append((relpath, lines))
        elif lines >= args.warn_threshold:
            warnings_list.append((relpath, lines))

    # --------------- 输出 ---------------
    print(f"📏 文件长度检查 (WARNING >= {args.warn_threshold} 行, ERROR >= {args.error_threshold} 行)")
    print(f"   项目根目录: {project_root}")
    print()

    if not warnings_list and not errors_list:
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

    if args.strict and errors_list:
        print("\n🚫 --strict 模式: 检测到超限文件，退出码 1")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
