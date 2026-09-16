# -*- coding: utf-8 -*-
"""运行期路径必须锚定在**仓库根目录**，与进程的当前工作目录（CWD）无关。

## 缺陷

多处运行期路径此前用 `os.path.abspath(相对路径)` 解析，而 `os.path.abspath`
是相对 **CWD** 的：

* `utils/db_config.py`：`DEFAULT_SQLITE_PATH = os.path.abspath("instance/diff_platform.db")`
* `config.py`：`DATABASE_CONFIG = {'db_path': os.path.abspath("instance/diff_platform.db")}`
* `utils/path_security.py::build_repository_local_path(base_dir="repos")`：
  `base_abs = os.path.abspath(base_dir)`

**实测（修复前）**：

    cd %TEMP% ; python <仓库根>/app.py
    → 运行期数据库路径变成 ...\\Temp\\instance\\diff_platform.db

后果按危害排序：

1. **看起来像数据丢失**：换个方式启动（Windows 服务、systemd 的
   `WorkingDirectory`、`cd /` 后跟绝对路径）就会新建一个空库，
   提交列表、确认状态、周版本配置全部「消失」，而旧库还躺在磁盘上。
2. **工作副本被整体重克隆**：`repos/` 同理 —— 新位置下所有仓库都被判定为
   「未克隆」，于是每个仓库重新 clone 一遍，旧的 `repos/` 变成孤儿目录。
3. **诊断信息说谎**：`/api/system/info` 报的路径与实际在用的库可能是两个
   不同文件。

## 本文件断言什么

1. 相对路径解析结果与 CWD 无关（用 `monkeypatch.chdir` 真的换目录，而不是
   只比较字符串前缀 —— 只比前缀的话，`abspath` 版本在 CWD == 仓库根时也会通过，
   测试就是空的）；
2. 绝对路径原样保留（显式指定要听显式指定的）；
3. `repos/` 工作副本目录同样锚定仓库根；
4. `config.DATABASE_CONFIG` 与 `utils/db_config.DEFAULT_SQLITE_PATH` 是**同一个值**
   —— 两处各写一份 abspath 正是这个 bug 的另一半：它们会随 CWD 一起漂移，
   但漂到同一个地方，于是「碰巧一致」，掩盖了问题。
"""
from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils import db_config, path_security  # noqa: E402
from utils.runtime_paths import repo_root, resolve_runtime_path  # noqa: E402


# 一个与仓库根不同的目录；chdir 过去就能暴露「相对 CWD」的实现。
_ELSEWHERE = os.path.dirname(PROJECT_ROOT)

# 跳过 fixture：pytest 的工作目录已经是仓库根，这里要一个**确定不同**的目录。
if os.path.normcase(_ELSEWHERE) == os.path.normcase(PROJECT_ROOT):  # pragma: no cover
    _ELSEWHERE = os.path.abspath(os.sep)


class TestRepoRoot:
    def test_repo_root_is_the_directory_containing_utils(self):
        assert os.path.isdir(os.path.join(repo_root(), "utils"))
        assert os.path.isfile(os.path.join(repo_root(), "app.py"))

    def test_repo_root_does_not_depend_on_cwd(self, monkeypatch, tmp_path):
        before = repo_root()
        monkeypatch.chdir(tmp_path)
        assert repo_root() == before, (
            "repo_root() 随 CWD 变化了 —— 它必须用 __file__ 推导，不能用 os.getcwd()"
        )


class TestResolveRuntimePath:
    def test_relative_path_is_anchored_to_repo_root(self):
        resolved = resolve_runtime_path(os.path.join("instance", "x.db"))
        assert resolved == os.path.join(repo_root(), "instance", "x.db")

    def test_relative_path_is_stable_across_cwd(self, monkeypatch, tmp_path):
        """核心断言：换到别的目录，解析结果必须一模一样。"""
        baseline = resolve_runtime_path(os.path.join("instance", "x.db"))
        monkeypatch.chdir(tmp_path)
        assert resolve_runtime_path(os.path.join("instance", "x.db")) == baseline, (
            "换 CWD 后相对路径解析结果变了 —— 这正是「换个目录启动就换了一个库」的成因"
        )

    def test_absolute_path_is_kept(self, tmp_path):
        absolute = os.path.join(str(tmp_path), "explicit.db")
        assert resolve_runtime_path(absolute) == absolute

    def test_empty_falls_back_to_default_relative(self):
        resolved = resolve_runtime_path("", default_relative=os.path.join("instance", "x.db"))
        assert resolved == os.path.join(repo_root(), "instance", "x.db")

    def test_separators_are_normalized(self):
        assert "\\/" not in resolve_runtime_path("instance/sub/x.db")
        assert os.path.isabs(resolve_runtime_path("instance/x.db"))


class TestSqliteDefaultPathIsAnchored:
    def test_default_sqlite_path_is_under_repo_root(self):
        assert db_config.DEFAULT_SQLITE_PATH == os.path.join(
            repo_root(), "instance", "diff_platform.db"
        )

    def test_build_sqlite_uri_is_stable_across_cwd(self, monkeypatch, tmp_path):
        """换到别的目录解析 .env，SQLite 路径必须不变。

        修复前：`SQLITE_DB_PATH=instance/diff_platform.db`（.env.simple 里就是这么写的）
        从 %TEMP% 启动会得到 %TEMP%\\instance\\diff_platform.db。
        """
        env = {"SQLITE_DB_PATH": "instance/diff_platform.db"}
        baseline_uri, baseline_path = db_config.build_sqlite_uri(env)

        monkeypatch.chdir(tmp_path)
        uri, db_path = db_config.build_sqlite_uri(env)

        assert db_path == baseline_path, (
            f"CWD 变化后 SQLite 路径变了：{baseline_path!r} → {db_path!r}"
        )
        assert uri == baseline_uri
        assert db_path == os.path.join(repo_root(), "instance", "diff_platform.db")

    def test_absolute_env_path_wins(self, tmp_path):
        absolute = os.path.join(str(tmp_path), "opt", "prod.db")
        _uri, db_path = db_config.build_sqlite_uri({"SQLITE_DB_PATH": absolute})
        assert db_path == absolute, "显式绝对路径必须原样生效"


class TestConfigDatabaseConfigAgreesWithDbConfig:
    def test_database_config_uses_the_single_source_of_truth(self):
        """两份 abspath 会各自漂移但「碰巧一致」，必须只剩一个来源。"""
        from config import DATABASE_CONFIG

        assert DATABASE_CONFIG["db_path"] == db_config.DEFAULT_SQLITE_PATH, (
            "config.DATABASE_CONFIG['db_path'] 与 utils/db_config.DEFAULT_SQLITE_PATH "
            "不是同一个值 —— 两处各写一份路径解析正是 CWD 依赖 bug 的另一半。"
        )
        assert DATABASE_CONFIG["instance_dir"] == os.path.dirname(
            db_config.DEFAULT_SQLITE_PATH
        )


class TestRepositoryWorkingCopyIsAnchored:
    def test_repos_base_dir_is_stable_across_cwd(self, monkeypatch, tmp_path):
        """工作副本目录不能随 CWD 漂移。

        修复前：从别处启动会在新目录下新建一个空 `repos/`，所有仓库被判为
        「未克隆」而重新 clone，旧的 `repos/` 变成孤儿。
        """
        baseline = path_security.build_repository_local_path("proj", "repo", 7)
        monkeypatch.chdir(tmp_path)
        assert path_security.build_repository_local_path("proj", "repo", 7) == baseline, (
            "换 CWD 后工作副本路径变了 —— 会触发「全部仓库重新克隆」"
        )

    def test_explicit_base_dir_still_works(self, monkeypatch, tmp_path):
        """显式传 base_dir 时以调用方为准（Agent 侧就是这么传的）。"""
        explicit = str(tmp_path)
        got = path_security.build_repository_local_path("proj", "repo", 7, base_dir=explicit)
        assert got == os.path.join(explicit, "proj_repo_7")

    def test_relative_base_dir_is_anchored_to_repo_root(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        got = path_security.build_repository_local_path("p", "r", 1, base_dir="repos")
        assert got == os.path.join(repo_root(), "repos", "p_r_1")

    @pytest.mark.parametrize("bad", ["../escape", "..", "a/../../b"])
    def test_escape_attempts_are_still_rejected(self, bad):
        """锚定改动不能削弱原有的越界校验。

        注意断言的**不是**「结果里不含 `..` 子串」：`_sanitize_segment` 会把
        `..` 保留下来当**字面文件名**（`repos/.._.._1`），`a/../../b` 里的 `/`
        被换成 `_` 变成单段名 `a_.._.._b` —— 两者都老老实实待在 `base_dir` 里，
        含 `..` 子串并不代表穿越。真正要保证的是「落点仍在 base_dir 内」。
        """
        got = path_security.build_repository_local_path(bad, bad, 1, base_dir="repos")
        base_abs = os.path.join(repo_root(), "repos")

        assert got.startswith(base_abs + os.sep), f"{got} 逃出了 {base_abs}"
        # 落点必须是 base_dir 的直接子项（只多一层），且父目录就是 base_abs。
        assert os.path.dirname(got) == base_abs, (
            f"{got} 的父目录不是 {base_abs} —— 说明名字里的分隔符没被消掉"
        )
        # 名字里不允许残留路径分隔符（否则 join 之后还能再往下钻）。
        assert os.sep not in os.path.basename(got)
        assert "/" not in os.path.basename(got)


class TestConfigDoesNotHijackHostStreams:
    """`import config` 不得替换宿主已经安装好的 stdout / stderr。

    ## 缺陷

    `config.py` 在 win32 上无条件执行：

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', ...)

    它假定 `sys.stdout` 就是进程原本的控制台流。但只要有宿主先装过自己的流
    （pytest 的捕获对象、IDE/调试器的输出面板、gunicorn 等 WSGI 宿主），
    这一行就会把**宿主的流换掉**，宿主此后的输出写进一个悬空对象。

    **实测（修复前）**：在一个测试里 `from config import DATABASE_CONFIG`
    （该进程此前没导入过 config）之后，pytest 的收尾汇总整段消失 ——
    `python -m pytest` 只打印一串点就退出、退出码 1，
    看不到 `N passed / M failed`，CI 拿不到任何结果。失败极难归因：
    坏的是「谁能打印」，而不是任何一条断言。

    ## 本文件断言什么

    1. 宿主换过 `sys.stdout` 时，`import config` 不会把它替换掉；
    2. 但**进程原本的标准输出**仍会被包成 UTF-8（Windows 控制台里中文不炸的
       那个原始目的不能被修坏）。
    """

    def test_host_stream_is_preserved(self, monkeypatch):
        """在独立的子进程里验证，避免污染当前进程的 sys.stdout。"""
        import subprocess
        import textwrap

        script = textwrap.dedent(
            """
            import io, sys
            # 模拟宿主（pytest / IDE / WSGI 服务器）先安装了自己的流。
            #
            # 关键：必须**带 `buffer` 属性**。修复前的写法是
            # `io.TextIOWrapper(sys.stdout.buffer, ...)`，只要宿主流的 `buffer`
            # 取得到就会照换不误 —— 用一个没有 buffer 的替身来测，等于在测
            # 「没有 buffer 时会走哪条分支」，修复前后都会通过（验证过）。
            class HostStream(io.TextIOBase):
                def __init__(self):
                    self.written = []
                    self.buffer = io.BytesIO()
                def write(self, s):
                    self.written.append(s)
                    return len(s)
                def flush(self):
                    pass

            host_out, host_err = HostStream(), HostStream()
            real_out = sys.__stdout__
            sys.stdout, sys.stderr = host_out, host_err
            import config  # noqa: F401
            # 哨兵必须写到**真实** stdout：宿主流是我们自己装的，写进去就看不到了。
            assert sys.stdout is host_out, "config 把宿主的 stdout 换掉了"
            assert sys.stderr is host_err, "config 把宿主的 stderr 换掉了"
            real_out.write("PRESERVED")
            real_out.flush()
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert "config 把宿主的" not in (result.stderr or ""), (
            f"import config 替换了宿主已安装的流：\n{result.stderr}"
        )
        assert "PRESERVED" in (result.stdout or "") + (result.stderr or ""), (
            f"子进程没有跑到断言之后：stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_原本的标准输出仍然被包成_utf8(self):
        """原来的目的（Windows 控制台中文不炸）不能被修坏。"""
        import subprocess

        script = (
            "import sys, io;"
            "before = sys.stdout;"
            "import config;"
            "after = sys.stdout;"
            "print('REWRAPPED' if (after is not before and "
            "getattr(after, 'encoding', '').lower().replace('-', '') == 'utf8') else "
            "'NOT_REWRAPPED:' + repr(after))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        combined = (result.stdout or "") + (result.stderr or "")
        if sys.platform == "win32":
            assert "REWRAPPED" in combined, (
                f"Windows 下 config 不再把标准输出包成 UTF-8 —— 控制台中文会炸：{combined!r}"
            )
        else:
            # 非 Windows 不做替换是设计如此（见 config.py 的 `if sys.platform == 'win32'`）。
            assert "NOT_REWRAPPED" in combined or "REWRAPPED" in combined
