# -*- coding: utf-8 -*-
"""根目录 `conftest.py` 的守卫：**在 `tests/` 之外跑 pytest 也不许碰线上库**。

## 它钉的那个事故（2026-09-21，当天发生三次）

`tests/conftest.py` 的隔离是强制的，但它只在 pytest 加载到**它自己**的时候生效。pytest 给一个
测试文件加载 conftest 的范围是「rootdir → 该文件所在目录」，所以：

    在 `tests/` 里跑    → 加载 `tests/conftest.py` → 库被指到临时 sqlite  ✅
    在 `tests/` 之外跑  → **它根本不会被加载** → 应用回落到
                        `instance/diff_platform.db`（**线上库**）  ❌

而那一次的形态恰好最阴：`.pytest_tmp/p007/test_counts_probe.py` 被**显式点名**跑
（`pytest.ini` 的 `norecursedirs` 含 `.pytest_tmp`，所以只有点名才跑得到），它留下的字节码是
`test_counts_probe.cpython-313-**pytest-9.1.1**.pyc` —— 测试形态的行直接写进了线上库：
一个 `is_active=1, auto_sync=1` 的假周版本配置 + 假仓库 + 快照。**而 active+auto_sync 会被
调度器排 `weekly_sync`**，于是对不存在的仓库反复同步失败、刷日志、**占着单线程 worker**。

## 这个测试怎么保证自己不去碰真的线上库

**它对着一个"诱饵库"跑**（`.pytest_tmp/` 下的一个 sqlite 文件），把
`DATABASE_URL` 指着诱饵，然后在子进程里跑一个 `tests/` 之外的测试文件：

* 守卫**正常**：子进程里的 `DATABASE_URL` 被改写成 `.pytest_tmp/db/diff_platform_rootguard_*.db`，
  诱饵库**一个字节都不变**；
* 守卫**坏了**：子进程就会去写诱饵库 —— 断言发现它变了，测试红。

**用诱饵而不是真的 `instance/diff_platform.db`，是因为这条测试一旦失效就必须是"没拦住"，
而不是"把用户的真实数据写坏了"。** 测试自己的失效模式也必须是安全的。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DECOY_DIR = ROOT / ".pytest_tmp" / "guardcheck"


def _write_probe_test(dir_path: Path) -> Path:
    """在 `tests/` **之外**造一个测试文件（正是事故形态：脱离 tests/conftest.py）。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / f"test_probe_{uuid.uuid4().hex[:8]}.py"
    target.write_text(
        "import os\n"
        "def test_report_the_db_it_resolved():\n"
        "    print('RESOLVED_URI=' + os.environ.get('DATABASE_URL', ''))\n"
        "    assert True\n",
        encoding="utf-8",
    )
    return target


def _run_probe(probe: Path, *, database_url: str) -> str:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env.pop("PYTEST_SQLITE_DB_PATH", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q", "-s",
         "-p", "no:cacheprovider", "-p", "no:warnings", "-p", "no:logging"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    return proc.stdout + proc.stderr


def _decoy_url(decoy: Path) -> str:
    return "sqlite:///" + decoy.as_posix()


def test_a_probe_outside_tests_gets_an_isolated_db():
    """`tests/` 之外的测试文件必须落到 `.pytest_tmp/` 下的临时库。"""
    decoy = DECOY_DIR / "decoy_untouched.db"
    DECOY_DIR.mkdir(parents=True, exist_ok=True)
    decoy.write_bytes(b"")          # 空文件：守卫若失效，它会被写进表结构
    before = decoy.stat().st_size

    probe = _write_probe_test(DECOY_DIR)
    out = _run_probe(probe, database_url=_decoy_url(decoy))

    resolved = [
        line.split("RESOLVED_URI=", 1)[1].strip()
        for line in out.splitlines()
        if "RESOLVED_URI=" in line
    ]
    assert resolved, f"子进程没有报出它解析到的库地址，输出：\n{out[-2000:]}"
    assert "instance/diff_platform.db" not in resolved[0], (
        "❌ 守卫失效：`tests/` 之外的 pytest 解析到了线上库 —— 这正是那个事故。"
        f"解析结果={resolved[0]!r}"
    )
    assert ".pytest_tmp" in resolved[0], f"没有落到临时库：{resolved[0]!r}"

    # 诱饵库必须一个字节都没变 —— 这是「真的没写进去」的行为证据，
    # 比只看环境变量字符串强（环境变量对了而应用没读到，是另一种失效）。
    assert decoy.stat().st_size == before, (
        "诱饵库被写了 —— 子进程真的把测试数据写进了 DATABASE_URL 指的那个库。"
    )


def test_an_explicitly_exported_unsafe_url_is_rewritten_and_reported():
    """有人显式 export 一个非临时库时：**改写 + 响亮地报出来**，不许静默。

    诱饵必须放在**既不在 `.pytest_tmp/` 下、也不在系统临时目录下**的地方 ——
    否则守卫判它是「自己造的临时库」、不该警告，这条用例就测不到警告那一支。
    用 `tests/_pytest_tmp_run/`：它在 `.gitignore` 里（不会进仓库），
    而路径里没有 `.pytest_tmp/` 这一段。
    """
    decoy_dir = ROOT / "tests" / "_pytest_tmp_run"
    decoy_dir.mkdir(parents=True, exist_ok=True)
    decoy = decoy_dir / "decoy_exported.db"
    decoy.write_bytes(b"")
    before = decoy.stat().st_size

    probe = _write_probe_test(DECOY_DIR)
    out = _run_probe(probe, database_url="sqlite:///" + decoy.as_posix())

    assert "非临时库" in out, (
        "守卫改写了库却没有告诉跑的人 —— 静默改写会让人以为自己在测别的东西。"
        f"输出：\n{out[-2000:]}"
    )
    assert decoy.stat().st_size == before, "诱饵库被写了，守卫没拦住"


def test_the_normal_tests_directory_run_is_left_alone():
    """**反向守卫（行为级）**：参数都在 `tests/` 里时，根 conftest 一个字节都不许碰。

    第一版守卫无条件设了环境变量，结果把 5600 条测试打红 **262 条** ——
    `tests/conftest.py` 的 `_assert_test_db_isolation()` 要求运行期库**等于它自己那个
    session 级临时库**，被 root 这一层抢先占了之后，每条用例在 setup/teardown 都撞
    「数据库不是临时 sqlite」。

    ## 为什么这条必须跑子进程、不能只测那个判据函数

    我第一版这条是直接调 `_targets_outside_tests(...)` 断言 True/False 的。变异验证把它
    证伪了：把 `pytest_configure` 里那个 `if not _targets_outside_tests(config): return`
    改成 `if False:`（= 退回坏的那一版），**三条用例全绿** —— 因为判据函数本身没变，
    而另外两条只验证「外面跑时被隔离」，坏那版照样隔离。

    真正要钉的是**行为**：跑 `tests/` 里的文件时，应用最终连的必须是 `tests/conftest.py`
    造的那个 `diff_platform_test_*` 库，**不能**是 root 守卫的 `diff_platform_rootguard_*`。
    应用启动时会打一行 `数据库后端: sqlite | URI: ...`，就断它。
    """
    # 探针放在 `tests/` **里面**（`tests/_pytest_tmp_run/` 在 .gitignore 里）——
    # 这才是那次 262 条红的形态：参数落在 tests/ 里，`tests/conftest.py` 会加载并
    # 在**导入期**把库设成它的 `diff_platform_test_*`；root 的 `pytest_configure`
    # 跑在所有 conftest 导入**之后**，无条件那版会把那个值覆盖掉。（顺序正是关键。）
    probe_dir = ROOT / "tests" / "_pytest_tmp_run"
    probe = _write_probe_test(probe_dir)
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.pop("PYTEST_SQLITE_DB_PATH", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q", "-s",
         "-p", "no:cacheprovider", "-p", "no:warnings", "-p", "no:logging"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    resolved = [
        line.split("RESOLVED_URI=", 1)[1].strip()
        for line in out.splitlines()
        if "RESOLVED_URI=" in line
    ]
    assert resolved, f"子进程没有报出它解析到的库地址，输出：\n{out[-2500:]}"
    assert "rootguard" not in resolved[0], (
        "❌ root conftest 抢了 `tests/` 跑的库 —— 正是那次 262 条红的形态"
        "（`tests/conftest.py` 的 `_assert_test_db_isolation()` 会全线炸）。"
        f"解析结果={resolved[0]!r}"
    )
    assert "diff_platform_test_" in resolved[0], (
        "没看到 tests/conftest.py 那个 session 级临时库的 URI —— 它没生效，"
        f"或者环境里有人预先占了 DATABASE_URL。解析结果={resolved[0]!r}"
    )
    assert "instance/diff_platform.db" not in resolved[0], (
        f"落到线上库了：{resolved[0]!r}"
    )


def test_a_tests_file_named_from_another_cwd_is_still_left_alone():
    """**反向守卫之二**：从**别的目录**用**绝对路径**点名跑 `tests/` 里的文件。

    第一版 `_targets_outside_tests` 拿 `os.getcwd()` 当基准，于是 cwd 不是仓库根时，
    `tests_dir` 被算成 `<cwd>/tests`、参数当然不在它底下 → 判成「伸到外面了」→ 无条件
    改写 → 把 `tests/conftest.py` 的临时库覆盖掉 → **正常的一轮测试全断**。形态与
    「在 `tests/` 之外跑」一模一样，只看 cwd 就分不开这两种情况。基准必须是 **rootdir**。

    这条用真子进程验，不调判据函数（上一节的教训：钉判据函数是空的）。
    """
    probe_dir = ROOT / "tests" / "_pytest_tmp_run"
    probe = _write_probe_test(probe_dir)
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.pop("PYTEST_SQLITE_DB_PATH", None)
    proc = subprocess.run(
        # 参数是**绝对路径**，cwd 是仓库根**之外**（用系统临时目录，一定不在仓库里）。
        [sys.executable, "-m", "pytest", str(probe.resolve()), "-q", "-s",
         "-p", "no:cacheprovider", "-p", "no:warnings", "-p", "no:logging"],
        cwd=str(Path(tempfile.gettempdir())), env=env,
        capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    resolved = [
        line.split("RESOLVED_URI=", 1)[1].strip()
        for line in out.splitlines()
        if "RESOLVED_URI=" in line
    ]
    assert resolved, f"子进程没有报出它解析到的库地址，输出：\n{out[-2500:]}"
    assert "rootguard" not in resolved[0], (
        "从别的 cwd 点名跑 tests/ 里的文件时，root conftest 误判成「伸到外面了」并抢了库 —— "
        f"正常的一轮测试会全断。解析结果={resolved[0]!r}"
    )
    assert "diff_platform_test_" in resolved[0], (
        f"没落到 tests/conftest.py 的临时库：{resolved[0]!r}"
    )


def test_the_outside_tests_predicate_itself():
    """判据函数的单元级守卫（快，覆盖各条输入形态）。"""
    sys.path.insert(0, str(ROOT))
    import conftest as root_conftest

    class _Cfg:
        def __init__(self, args, rootpath=ROOT):
            self.args = args
            self.rootpath = rootpath

    assert root_conftest._targets_outside_tests(_Cfg([str(ROOT / "tests")])) is False
    assert root_conftest._targets_outside_tests(
        _Cfg([str(ROOT / "tests" / "test_ai_job_protocol.py")])
    ) is False
    # 没有位置参数 = 走 pytest.ini 的 testpaths（tests/），也不该管。
    assert root_conftest._targets_outside_tests(_Cfg([])) is False
    # 伸到 tests/ 外面就必须管。
    assert root_conftest._targets_outside_tests(_Cfg([str(DECOY_DIR)])) is True
    assert root_conftest._targets_outside_tests(
        _Cfg([str(ROOT / "tests"), str(DECOY_DIR)])
    ) is True
    # ★ 基准是 rootpath 而不是 cwd：rootpath 说这是仓库、参数在它 tests/ 底下 → 不管。
    # （这条在 cwd 版下是 True —— 就是那个 bug。）
    assert root_conftest._targets_outside_tests(
        _Cfg([str(ROOT / "tests" / "test_ai_job_protocol.py")], rootpath=ROOT)
    ) is False
    # rootpath 缺失时退回 cwd，不能抛。
    class _NoRoot:
        args = [str(ROOT / "tests")]

    root_conftest._targets_outside_tests(_NoRoot())   # 不抛就算过


def test_the_temp_db_predicate_is_not_too_loose():
    """`_is_temp_sqlite` 必须精确。

    第一版是 `".pytest_tmp/" in path or "tmp" in path` —— `"tmp" in path` 太松：
    任何路径里带 `tmp` 都算数（`.../instance/tmp_backup/diff_platform.db` 也会被判成
    临时库），而这条判据决定「要不要警告」，判松了就是**该警告时不警告**。
    """
    sys.path.insert(0, str(ROOT))
    import conftest as root_conftest

    assert root_conftest._is_temp_sqlite(
        "sqlite:///" + (ROOT / ".pytest_tmp" / "db" / "x.db").as_posix()
    ) is True
    assert root_conftest._is_temp_sqlite(
        "sqlite:///" + (ROOT / "instance" / "diff_platform.db").as_posix()
    ) is False
    # 这个才是第一版会放过去的那个：路径里有 tmp，但不是临时库。
    assert root_conftest._is_temp_sqlite(
        "sqlite:///" + (ROOT / "instance" / "tmp_backup" / "diff_platform.db").as_posix()
    ) is False
    assert root_conftest._is_temp_sqlite("mysql://user@host/db") is False


def test_the_temp_db_predicate_keeps_the_path_case(monkeypatch):
    """**大小写敏感的文件系统上必须也对**：path 段按原样保留，不许整串 `url.lower()`。

    2026-09-23 CI（ubuntu-latest）红的就是这一族：

        FAILED test_the_temp_db_predicate_is_not_too_loose
          assert _is_temp_sqlite('sqlite:///…/ExcelDiff_ForGame/…/.pytest_tmp/db/x.db') is True
        FAILED test_the_temp_db_we_create_is_recognised_by_the_platform_safety_layer
          assert _is_temp_sqlite(uri) is True

    当时实现的第一句是 `lowered = url.lower()`，再从 `lowered` 里切出路径去和
    `_TEMP_DB_DIR` 比 `startswith`。本仓库的目录名 `ExcelDiff_ForGame` **带大写**，于是：

        被 lower 掉的候选   …/excelldiff_forgame/…/.pytest_tmp/db/x.db
        基准（从 __file__） …/ExcelDiff_ForGame/…/.pytest_tmp/db
        str.startswith     → False      ← 一侧 lower 了、另一侧没有，POSIX 区分大小写

    Windows 上之所以是绿的，纯粹因为 `os.path.realpath` 会把**已存在**的路径段还原成磁盘上
    的真实大小写，把被 lower 掉的那段又「修」了回来 —— 这份绿是**碰巧**的，不是设计出来的。

    ## 为什么必须把 realpath 换掉才钉得住

    「造一个大小写变体看返回什么」这种断言**天然是平台相关的**：大小写不敏感的文件系统上
    `…/ExcelDiff_ForGame/…` 和 `…/excelldiff_forgame/…` 是同一个目录（该 True），
    大小写敏感的系统上是两个目录（该 False）。钉不住。

    要钉的是「**不依赖 realpath 修大小写**」这个性质本身。所以这里把 realpath 换成 Linux
    语义的实现（只规范化符号链接与 `.`/`..`，不动大小写），再喂 CI 上那条真实输入 ——
    这条断言在 Windows 和 Linux 上都会红，也正是 CI 上红的那一条。
    """
    sys.path.insert(0, str(ROOT))
    import conftest as root_conftest

    # Linux 的 realpath：没有符号链接时 == normpath(abspath(x))，**不动大小写**。
    monkeypatch.setattr(
        os.path, "realpath", lambda p: os.path.normpath(os.path.abspath(p))
    )

    assert root_conftest._is_temp_sqlite(
        "sqlite:///" + (ROOT / ".pytest_tmp" / "db" / "x.db").as_posix()
    ) is True, (
        "判据依赖了「realpath 会把大小写修回来」—— 大小写敏感的文件系统上它不修，"
        "于是我们自己造的临时库被判成非临时库（CI 就是这么红的）。"
    )
    assert root_conftest._is_temp_sqlite(
        "sqlite:///"
        + (ROOT / ".pytest_tmp" / "db" / "diff_platform_test_rootguard_ab12.db").as_posix()
    ) is True
    # 反向：修掉大小写那一处**不许**顺手把判据放松。
    assert root_conftest._is_temp_sqlite(
        "sqlite:///" + (ROOT / "instance" / "diff_platform.db").as_posix()
    ) is False
    assert root_conftest._is_temp_sqlite(
        "sqlite:///" + (ROOT / "instance" / "tmp_backup" / "diff_platform.db").as_posix()
    ) is False

    # 反向之二：**不许**为了「兼容大小写」把比较改成不区分大小写（`candidate.lower() ==
    # base.lower()`）。在大小写敏感的文件系统上 `…/ExcelDiff_ForGame/…` 和
    # `…/excelldiff_forgame/…` 是**两个不同的目录**，把后者认成前者正是把判据放松。
    # 变异验证：把比较改成 `.lower()` 那种写法，仓库里**只有这一条**会红（其余 14 条全绿）。
    lowered_repo = ROOT.as_posix().lower()
    if lowered_repo != ROOT.as_posix():
        # 整个绝对路径本来就全小写时（比如克隆到 /tmp/repo）这条不成立，跳过即可。
        assert root_conftest._is_temp_sqlite(
            "sqlite:///" + lowered_repo + "/.pytest_tmp/db/x.db"
        ) is False, (
            "判据被改成大小写不敏感了 —— 那是放松：大小写敏感的文件系统上这是两个目录。"
        )


def test_the_temp_db_we_create_is_recognised_by_the_platform_safety_layer():
    """**本守卫自己造的库，必须被平台的安全层认成临时库。**

    这是另一个代理查出来的一条真问题：守卫原本把文件叫 `diff_platform_rootguard_<hex>.db`，
    而 `utils/db_safety.is_temp_sqlite_path()` 认的是 basename 里含
    `tmp` / `pytest` / `diff_platform_test` / `codex_test`，以及**系统临时目录**。
    `.pytest_tmp/db/` 不在系统临时目录下，`diff_platform_rootguard` 那四个又一个都不含
    —— 于是**守卫自己造出来的库被判成「非临时库」**。

    后果不是理论上的：`pytest tests/ 某个外部目录/`（参数两边都沾）时 `tests/conftest.py`
    也会加载，它的 `_assert_test_db_isolation()` 会按设计中止整轮测试 —— 也就是
    **守卫干活的那一刻反而把正常测试打断**。所以这条钉的是名字本身。
    """
    sys.path.insert(0, str(ROOT))
    import conftest as root_conftest
    from utils.db_safety import is_temp_sqlite_path

    _path, uri = root_conftest._temp_sqlite_uri()
    created = uri[len("sqlite:///"):]
    try:
        assert is_temp_sqlite_path(created) is True, (
            f"守卫造出来的库被平台判成非临时库：{created} —— "
            "名字里必须带 `diff_platform_test`（或落进系统临时目录）。"
        )
        assert root_conftest._is_temp_sqlite(uri) is True
    finally:
        try:
            os.remove(created)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _cleanup_probe_files():
    yield
    # 探针造在两个地方：`tests/` 外的诱饵目录，和 `tests/` 内的反向探针目录。
    for probe_dir in (DECOY_DIR, ROOT / "tests" / "_pytest_tmp_run"):
        for path in probe_dir.glob("test_probe_*.py"):
            try:
                path.unlink()
            except OSError:
                pass
        pycache = probe_dir / "__pycache__"
        if pycache.is_dir():
            for path in pycache.glob("test_probe_*.pyc"):
                try:
                    path.unlink()
                except OSError:
                    pass
