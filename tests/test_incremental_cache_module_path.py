"""`incremental_cache_system` 必须以 `services.` 包路径导入。

## 为什么值得单独一条守卫

原来它在仓库根目录，所以生产代码写的是裸模块名：

    from incremental_cache_system import IncrementalCacheManager

现在它在 `services/incremental_cache_system.py`，裸模块名会 `ModuleNotFoundError`。
但这个失败**不会有人发现**：

* 那行 import 位于 `try:` 内，而 `except REPOSITORY_UPDATE_FORM_FORCE_SYNC_ERRORS`
  的元组里**含 `ImportError`**（`ModuleNotFoundError` 是它的子类），
* 兜住之后只记一句「❌ 全量同步异常: No module named 'incremental_cache_system'」，
* 而这段代码跑在 `async_refilter` 后台线程里，日志进了 runlog 没人看。

净效果：管理员改仓库设置后，增量全量同步永远不跑，功能静默失效。

**行为测试挡不住它** —— 现有那条 `test_..._refilter_logs_force_sync_exception`
断言的是「日志里有『全量同步异常』」，import 挂掉时同样满足。所以这里从源码层面
直接禁止这种写法，并让它与那条行为断言互为补充（见该测试里对桩文本的断言）。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 裸模块名的导入形态。包路径（services.incremental_cache_system）不匹配。
_BARE_IMPORT_RES = (
    re.compile(r"^\s*import\s+incremental_cache_system\b", re.M),
    re.compile(r"^\s*from\s+incremental_cache_system\b", re.M),
)


def _python_files() -> list[str]:
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("不是 git 工作区，无法枚举被跟踪的源码")
    proc = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        pytest.skip(f"git ls-files 失败：{proc.stderr.strip()}")
    return [name for name in proc.stdout.split("\0") if name]


def test_the_module_itself_is_where_it_is_claimed_to_be():
    """先确认前提：文件真的在 services/ 下。

    否则这条守卫就成了空转 —— 文件若被搬回根目录，裸模块名反而又是对的。
    """
    assert (REPO_ROOT / "services" / "incremental_cache_system.py").is_file()
    assert not (REPO_ROOT / "incremental_cache_system.py").exists(), (
        "根目录又出现了 incremental_cache_system.py —— 要么它被搬回去了，"
        "要么留下了副本（副本会遮蔽包内模块，import 结果取决于 sys.path 顺序）"
    )


def test_no_bare_module_name_imports():
    offenders = []
    for rel in _python_files():
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for pattern in _BARE_IMPORT_RES:
            for match in pattern.finditer(source):
                line = source[: match.start()].count("\n") + 1
                offenders.append(f"{rel}:{line} {match.group(0).strip()}")
    assert not offenders, (
        "以下位置用裸模块名导入了 incremental_cache_system（它在 services/ 下，"
        "裸名会 ModuleNotFoundError，且会被 except ImportError 静默吞掉）：\n  "
        + "\n  ".join(offenders)
        + "\n改成 from services.incremental_cache_system import ..."
    )


def test_the_module_actually_imports_through_the_package_path():
    """行为侧确认：包路径真的能导入，且根目录裸名已经不行。"""
    import importlib

    module = importlib.import_module("services.incremental_cache_system")
    assert hasattr(module, "IncrementalCacheManager")

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("incremental_cache_system")
