#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库根目录的 conftest：**保证任何一次 pytest 都不会写到线上库**。

## 为什么需要它（2026-09-21 的事故根因）

`tests/conftest.py` 的隔离是**正确且强制**的，但它只在 pytest 加载到**它自己**的时候生效。
pytest 给一个测试文件加载 conftest 的范围是「rootdir → 该文件所在目录」，所以：

    在 `tests/` 里跑      → 加载 `tests/conftest.py` → DATABASE_URL 被指到临时库 ✅
    在 `tests/` 之外跑    → **那个 conftest 根本不会被加载** → 应用回落到
                          `instance/diff_platform.db`（**线上库**）❌

而那一次的形态恰好最阴：`.pytest_tmp/p007/test_counts_probe.py` 被**显式点名**跑
（`norecursedirs` 含 `.pytest_tmp`，所以只有点名才跑得到），它留下的字节码是
`test_counts_probe.cpython-313-**pytest-9.1.1**.pyc` —— 它拿不到那三行隔离环境变量，
于是「测试形态的行」直接写进了线上库，同一个形态当天发生过三次。

根目录这个 conftest 会被**任何** pytest 运行加载（包括收集 `tests/` 之外的文件的那些），
所以它在 `pytest_configure` 里兜住这一档。

## 它做什么

1. `DATABASE_URL` 没设 → 指到 `.pytest_tmp/db/` 下的临时库；
2. 设了、但**指向 `instance/`**（或其它非临时库）→ **改写**成临时库，并在终端打一条
   响亮的警告（不静默改，也不中止 —— 中止会让「跑个临时脚本」变成一件要 debug 的事，
   而写线上库比「安静地换个库」危险得多）；
3. `ALLOW_DESTRUCTIVE_DB_OPS=false`，与 `tests/conftest.py` 同一口径。

它与 `tests/conftest.py` 不冲突：后者随后会把自己的 session 级临时库再覆盖上去
（两个都指向临时目录），且它的 `_assert_test_db_isolation()` 依旧通过。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# ★ 必须是**绝对路径**，锚在本文件所在目录上 —— **不能靠 cwd**。
#
# 这个坑我踩了两次，是同一族：
#   1. `_targets_outside_tests` 曾经拿 `os.getcwd()` 当仓库根，从别的目录点名跑
#      `tests/xxx.py` 时判成「伸到外面了」→ 无条件改写 → 正常测试全断（已改成
#      `config.rootpath`）；
#   2. 这里 `os.path.abspath(".pytest_tmp/db")` 仍然是 **cwd 相对**的，于是
#      `_is_temp_sqlite` 在别的 cwd 下会把**我们自己刚造出来的临时库**判成非临时库。
#
# CI（Linux）就是这么红的：
#     test_the_temp_db_we_create_is_recognised_by_the_platform_safety_layer
#     assert root_conftest._is_temp_sqlite(uri) is True  →  assert False is True
# 本地复现：`cd <别的目录> && pytest <仓库>/tests/test_pytest_db_isolation_guard.py`
# 症状和「守卫坏了」一模一样，但根因是**判据的基准选错了**。
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_TEMP_DB_DIR = os.path.join(_REPO_ROOT, ".pytest_tmp", "db")


def _is_temp_sqlite(url: str) -> bool:
    """是不是「我们自己造的临时库」。**判据要精确。**

    第一版写的是 `".pytest_tmp/" in path or "tmp" in path` —— `"tmp" in path` 太松了：
    任何路径里带 `tmp` 的都算数（`.../instance/tmp_backup/diff_platform.db` 也会被判成临时），
    而这条判据的用途是「要不要警告」，判松了就等于**该警告时不警告**。
    改成两个精确去处：本仓库的 `.pytest_tmp/` 底下，或者系统临时目录底下。
    """
    lowered = url.lower()
    if not lowered.startswith("sqlite:///"):
        return False
    raw = lowered[len("sqlite:///"):]
    try:
        # 去掉可能的 `?mode=ro` 之类的查询串，再规范化。
        raw = raw.split("?", 1)[0]
        candidate = os.path.realpath(os.path.abspath(raw))
        allowed = (
            os.path.realpath(os.path.abspath(_TEMP_DB_DIR)),
            os.path.realpath(os.path.abspath(tempfile.gettempdir())),
        )
    except (OSError, ValueError):
        return False
    for base in allowed:
        if candidate == base or candidate.startswith(base + os.sep):
            return True
    return False


def _temp_sqlite_uri() -> tuple[str, str]:
    db_dir = os.path.abspath(_TEMP_DB_DIR)
    os.makedirs(db_dir, exist_ok=True)
    # ★ 文件名必须含 `diff_platform_test`，**不能**叫 `diff_platform_rootguard_*`。
    # 判据在 `utils/db_safety.is_temp_sqlite_path()`：它认 basename 里含
    # `tmp` / `pytest` / `diff_platform_test` / `codex_test`，以及系统临时目录。
    # 而 `.pytest_tmp/db/` 既不在系统临时目录下、`diff_platform_rootguard` 这四个
    # 也一个都不含 —— 于是**本守卫自己造出来的库**会被平台判成「非临时库」。
    # 后果不是理论上的：`pytest tests/ 某目录/`（两边都沾）时 `tests/conftest.py`
    # 也会加载，它的 `_assert_test_db_isolation()` 会按设计中止整轮测试 ——
    # 也就是「守卫干活时反而把正常测试打断」。名字里带上 `diff_platform_test` 就一致了。
    handle = tempfile.NamedTemporaryFile(
        prefix="diff_platform_test_rootguard_", suffix=".db", delete=False, dir=db_dir
    )
    handle.close()
    return handle.name, "sqlite:///" + Path(os.path.abspath(handle.name)).as_posix()


def _root_of(config) -> str:
    """pytest 认定的 **rootdir**，不是 cwd。

    第一版这里写的是 `os.path.abspath(os.getcwd())` —— 那是个真 bug，实测能复现：
    从**别的目录**用**绝对路径**点名跑仓库里的 `tests/xxx.py` 时，cwd 不是仓库根，
    于是 `tests_dir` 算成了 `<cwd>/tests`，参数当然不在它底下 → 判成「伸到外面了」
    → 无条件改写，把 `tests/conftest.py` 的临时库覆盖掉 → 正常的一轮测试全断。
    形态与「在 `tests/` 之外跑」一模一样，所以不看 rootdir 就分不开这两种情况。
    """
    for attr in ("rootpath", "rootdir"):
        value = getattr(config, attr, None)
        if value:
            try:
                return os.path.abspath(str(value))
            except (OSError, ValueError):
                continue
    return os.path.abspath(os.getcwd())


def _targets_outside_tests(config) -> bool:
    """这次 pytest 会不会收集 `tests/` 之外的文件？

    **这个判断是整条守卫的关键。** 第一版我无条件设了环境变量，结果把 5600 条测试
    打红了 262 条 —— `tests/conftest.py` 的 `_assert_test_db_isolation()` 比它看起来
    严格得多：它要求运行期库**等于它自己那个 session 级临时库**，而我在 root 这一层
    先把 DATABASE_URL 占了，应用就解析到**我的**文件上，于是每条用例在 setup/teardown
    都撞「数据库不是临时 sqlite」。

    所以：只要参数都落在 `tests/` 里，`tests/conftest.py` 一定会被加载并做好隔离，
    **这里一个字节都不许碰**。只有参数伸到 `tests/` 外面时才轮到我。
    """
    root = _root_of(config)
    tests_dir = os.path.join(root, "tests")
    args = [a for a in getattr(config, "args", ()) if a and not a.startswith("-")]
    if not args:
        # 没有位置参数 = 走 pytest.ini 的 testpaths（tests/），conftest 会加载。
        return False
    for arg in args:
        try:
            path = os.path.abspath(arg)
        except (OSError, ValueError):
            continue
        try:
            if os.path.commonpath([path, tests_dir]) != tests_dir:
                return True
        except ValueError:
            # 不同盘符 —— 那必然不在 tests/ 里。
            return True
    return False


def pytest_configure(config):  # noqa: ARG001 —— pytest 的钩子签名
    if not _targets_outside_tests(config):
        return
    url = str(os.environ.get("DATABASE_URL", "") or "")
    path, temp_uri = _temp_sqlite_uri()

    if url and not _is_temp_sqlite(url):
        # 这一条就是事故本身：测试被指着线上库跑。
        sys_stderr_write = getattr(__import__("sys"), "stderr").write
        sys_stderr_write(
            "\n" + "=" * 78 + "\n"
            "⚠️  这次 pytest 收集了 `tests/` 之外的文件，而 DATABASE_URL 指着**非临时库**；\n"
            "    已强制改写到临时库：\n"
            f"      原来: {url}\n"
            f"      现在: {temp_uri}\n"
            "    原因：在 `tests/` 之外跑 pytest 不会加载 tests/conftest.py，\n"
            "    应用会回落到 instance/diff_platform.db（线上库）。\n"
            + "=" * 78 + "\n"
        )

    os.environ["DATABASE_URL"] = temp_uri
    os.environ["SQLITE_DB_PATH"] = path
    os.environ["DB_BACKEND"] = "sqlite"
    # 与 tests/conftest.py 同一口径：测试里不许绕过破坏性操作的守卫。
    os.environ["ALLOW_DESTRUCTIVE_DB_OPS"] = "false"
    config._rootguard_temp_path = path


def pytest_unconfigure(config):
    """只清理**没人用过**的那个临时文件（DATABASE_URL 已经不是它了）。"""
    path = getattr(config, "_rootguard_temp_path", None)
    if not path:
        return
    current = str(os.environ.get("DATABASE_URL", "") or "")
    if Path(path).as_posix() in current:
        # 还在用它 —— 这是「在 tests/ 之外跑」的那条路，留着（可能要看它）。
        return
    try:
        os.remove(path)
    except OSError:
        pass
