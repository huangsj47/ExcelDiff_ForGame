"""`scripts/` 下的运维脚本必须能按常规方式直接运行。

## 为什么需要这条

`python scripts/init_database.py` 这类调用，`sys.path[0]` 是**脚本所在目录**
（`scripts/`），**不是仓库根**。而这些脚本要 `from services.model_loader import ...`、
`from utils.db_config import ...` —— 全都需要仓库根在 `sys.path` 上。

它们原先躺在仓库根，靠的正是「脚本所在目录恰好就是根」这个巧合；
搬进 `scripts/` 之后，如果忘了补 `sys.path` 引导，**语法检查、ruff、import 静态分析
统统发现不了**，只有真正执行时才 ModuleNotFoundError。所以这里真的把它们跑起来。

（`init_html_cache.py` 里原本就有一行 `sys.path.insert`，但它写在
 `from services...` **之后** —— 对本次导入已经来不及，是句死代码。静态看
 「有自举」其实是假的，这也是必须真跑的理由。）

## 全程不碰生产库

环境变量按 tests/conftest.py 的隔离配方指向临时 sqlite
（`DATABASE_URL` / `DB_BACKEND` / `SQLITE_DB_PATH`），并断言跑完之后
`instance/diff_platform.db` **没有被创建**。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
INSTANCE_DB = REPO_ROOT / "instance" / "diff_platform.db"

# 判「生产库是自己变的还是脚本改的」时，空跑多久算数。取 15 秒：真机服务的后台调度按分钟
# 级 tick，太短会漏掉一次写、把假红放过去；太长则每次真回归都要白等。
_CONTROL_WINDOW_SECONDS = 15

# 需要仓库根在 sys.path 上、因此必须自带引导的脚本。
# extract_diff_lines.py 只用 stdlib + requests，不需要引导，故不在此列。
BOOTSTRAPPED_SCRIPTS = ("init_database.py", "init_html_cache.py", "recreate_db.py", "check_db.py")


def _isolated_env(tmp_dir: Path) -> dict[str, str]:
    """照 tests/conftest.py 的配方，把运行期数据库钉到临时 sqlite。"""
    db = tmp_dir / "smoke.db"
    uri = f"sqlite:///{db.as_posix()}"
    env = dict(os.environ)
    env.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DB_PATH": str(db),
            "DATABASE_URL": uri,
            # 与测试环境一致：不允许绕过破坏性操作守卫
            "ALLOW_DESTRUCTIVE_DB_OPS": "false",
        }
    )
    # 关键：必须摘掉 PYTHONPATH / PYTHONSAFEPATH。
    # 开发者或 CI 常把仓库根放进 PYTHONPATH，那样 `python scripts/X.py` **即使完全
    # 没有 sys.path 引导**也能 import 到 services —— 子进程冒烟测试会全绿，
    # 而自举缺失被掩盖（本机 PYTHONPATH 恰好为空，所以这是个潜伏的洞）。
    for name in ("PYTHONPATH", "PYTHONSAFEPATH"):
        env.pop(name, None)
    return env


def _run(script: str, tmp_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script)],
        cwd=REPO_ROOT,
        env=_isolated_env(tmp_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


@pytest.mark.parametrize("script", BOOTSTRAPPED_SCRIPTS)
def test_script_does_not_fail_on_imports(script, tmp_path):
    """核心断言：以 `python scripts/X.py` 执行时不会因为导入而失败。"""
    result = _run(script, tmp_path)
    combined = f"{result.stdout}\n{result.stderr}"
    for marker in ("ModuleNotFoundError", "ImportError", "No module named"):
        assert marker not in combined, (
            f"{script} 以 `python scripts/{script}` 执行时导入失败 —— "
            f"缺少 sys.path 引导。真实输出：\n{combined}"
        )


def test_recreate_db_reaches_its_destructive_guard(tmp_path):
    """recreate_db 必须一路跑到守卫那一步才被拦下。

    这条比「没有 ImportError」更强：它证明脚本真的执行到了业务代码
    （`assert_destructive_db_allowed` 已经拿到 db.engine.url 并做出判断），
    而不是在导入阶段就悄悄退出。
    """
    result = _run("recreate_db.py", tmp_path)
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, f"未设 ALLOW_DESTRUCTIVE_DB_OPS 时不该成功：\n{combined}"
    assert "assert_destructive_db_allowed" in combined or "ALLOW_DESTRUCTIVE_DB_OPS" in combined, (
        f"没有看到破坏性操作守卫的拒绝信息，说明没跑到那一步：\n{combined}"
    )


def test_check_db_refuses_to_create_an_empty_database(tmp_path):
    """check_db 的用途是「查看已有库」，不能凭空建一个空库再报『没有表』。

    必须显式传一个**确定不存在**的路径：不传参数时它看的是仓库默认库
    （`instance/diff_platform.db`），而那个文件在开发机上通常已经存在，
    断言就变成看环境脸色 —— 之前那版正是这么写的，CI 绿、本机红。
    """
    missing = tmp_path / "definitely-not-here.db"
    assert not missing.exists()
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_db.py"), str(missing)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, combined
    assert "数据库文件不存在" in combined, combined
    assert not missing.exists(), "被拒之后不该留下一个空库文件"


def test_init_scripts_actually_create_tables_in_the_temp_db(tmp_path):
    """跑通即证明导入链路完好；顺带确认表真的建在临时库上。"""
    for script in ("init_database.py", "init_html_cache.py"):
        result = _run(script, tmp_path)
        combined = f"{result.stdout}\n{result.stderr}"
        assert result.returncode == 0, f"{script} 失败：\n{combined}"
        assert "ModuleNotFoundError" not in combined


def _app_server_is_listening() -> bool:
    """本机有没有在跑真机服务（`python app.py`）。

    端口与 `bootstrap/runtime_entry.py` 的 `os.environ.get("PORT", "8002")` 同源：
    同样先看 `PORT`，默认同样是 8002。**只探「有没有人在这个端口上监听」**，不发请求 ——
    这条判据要的是「服务很可能开着」，不是「服务答得对」。
    """
    try:
        port = int(os.environ.get("PORT", "8002"))
    except (TypeError, ValueError):
        return False
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def test_smoke_runs_never_touch_the_production_db(tmp_path):
    """整组冒烟跑完后，生产库文件不应被创建或改动。

    `instance/` 目录本身可能出现（凭据加密密钥会自动落盘，见
    utils/security_utils._auto_generated_key_material），所以这里只钉数据库文件。
    连 `-wal` / `-shm` 一起比 —— WAL 模式下数据可能还没并回主文件，
    只比主文件会漏掉「真的写了、只是没 checkpoint」。

    ## 这条判据会被环境证伪，所以要能分清「谁改的」（2026-09-25）

    真机服务（`python app.py`）自己开着生产库，后台调度每跑一次就往 WAL 里落一笔。
    **实测**：什么都不跑的 20 秒里，`-wal` 的内容也变了（大小不变、内容变），而四个冒烟
    脚本**各自单独跑完，生产库一个字都没变** —— 也就是说这条断言红的时候，多半不是脚本
    写的。这个假红与真回归长得一模一样（同一条断言、同一句话），照它排查只会一路怀疑
    隔离配方：实测全量跑会红、单独重跑又绿。

    所以按「跑前跑后不相等」之后**再分一次谁改的**：本机有服务在监听 ⇒ 跳过（理由写在
    跳过信息里，`-rs` 看得见）；没有服务、再空跑一个同样长的窗口它**还在变** ⇒ 也是别人
    在写，同样跳过。两条都不成立（安静环境里它只在我们跑脚本时变）才判红。
    """
    def snapshot():
        return {
            suffix: (INSTANCE_DB.with_name(INSTANCE_DB.name + suffix).read_bytes()
                      if INSTANCE_DB.with_name(INSTANCE_DB.name + suffix).exists() else None)
            for suffix in ("", "-wal", "-shm")
        }

    before = snapshot()
    for script in BOOTSTRAPPED_SCRIPTS:
        _run(script, tmp_path)
    after = snapshot()
    if before == after:
        return

    # 变了 —— 先分清是谁改的，判据见 docstring。
    if INSTANCE_DB.exists() and _app_server_is_listening():
        pytest.skip(
            "本机有真机服务在监听（它自己在写生产库 WAL）—— 这次量测不成立，不是回归"
        )
    settled = snapshot()
    time.sleep(_CONTROL_WINDOW_SECONDS)
    if snapshot() != settled:
        pytest.skip(
            f"没人跑脚本的这 {_CONTROL_WINDOW_SECONDS} 秒里生产库自己还在变 —— "
            "另有进程在写它，这次量测不成立，不是回归"
        )
    assert before == after, (
        f"{INSTANCE_DB} 在冒烟测试期间被创建或改动了 —— "
        "隔离只覆盖了 SQLite 后端，检查是否有脚本绕开 DATABASE_URL 直接连文件"
        f"（已排除环境因素：本机没有服务监听，且空跑 {_CONTROL_WINDOW_SECONDS} 秒它稳着不动）"
    )


@pytest.mark.parametrize("script", BOOTSTRAPPED_SCRIPTS)
def test_each_script_bootstraps_sys_path_before_importing_first_party(script):
    """静态兜底：`sys.path` 引导必须在第一个 services/utils 导入**之前**。

    子进程冒烟测试是真正的判据，但它有一个前提：子进程里 PYTHONPATH 是干净的
    （上面 `_isolated_env` 已保证）。万一将来有人又把它放回去，冒烟测试会被
    静默废掉而这条仍然有效 —— 用 AST 看顺序，不受运行环境影响。

    `scripts/init_html_cache.py` 的旧版本正是「有 insert 但写在 import 之后」，
    静态看像有引导、实际是死代码，所以这里比的是**位置**而不是「有没有」。
    """
    import ast

    tree = ast.parse((SCRIPTS / script).read_text(encoding="utf-8"))
    bootstrap_line, first_party_line = None, None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"insert", "append"} and "path" in ast.unparse(node.func.value):
                if bootstrap_line is None:
                    bootstrap_line = node.lineno
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {"services", "utils"}:
            if first_party_line is None:
                first_party_line = node.lineno

    assert bootstrap_line is not None, f"{script} 里没有 sys.path 引导"
    assert first_party_line is not None, f"{script} 里没有 services/utils 导入（守卫前提变了）"
    assert bootstrap_line < first_party_line, (
        f"{script} 的 sys.path 引导在第 {bootstrap_line} 行，却早在第 {first_party_line} 行"
        f"就导入了 first-party 模块 —— 这个顺序下引导对本次导入来不及"
    )
