# -*- coding: utf-8 -*-
"""启动脚本挑解释器的判据必须落在「能不能真的跑起来」上，不能落在「PATH 里有没有这个名字」。

## 这一条钉的是哪一次真实故障

`bash start.sh` 在本机以 **49** 退出，而屏幕上没有任何一句指向真正的原因。Windows 的
「应用执行别名」在 PATH 上放了一个 `python3` 转发桩：`command -v python3` 命得中，可它
执行时只往 stderr 写一句「Python was not found; … Microsoft Store …」并以 49 退出 ——
**连 `--version` 都这样**。旧写法（`command -v` 判存在）于是把一个**跑不了的**解释器写进
`PYTHON_CMD`：版本号读成空串，失败点被推到十几行之后的「建虚拟环境」那一步。

`start.bat` 那份同病：`where python` 同样只判「PATH 里有没有这个名字」，命中的可能是同一个
转发桩。

## 判据为什么是「把桩放上去真的跑一遍」

静态断言（`assert "command -v" not in text`）证不了任何事 —— 它只说某一行怎么写，
不说脚本最后选中的是谁。所以这里：

* `start.sh`：把那个函数**从脚本里原样取出来**，在 PATH 最前面放一个与 Windows 别名
  **同形**的壳脚本（挡住真的 python3、打印同一句、exit 49），看它返回哪个名字；
* `start.bat`：**真的把整只 `start.bat` 跑一遍**（PATH 前面放一个叫 `python` 的、
  一跑就失败的可执行文件），看它是不是如实报错而不是继续往下走。

**每一条都同时跑一遍旧口径并断言两臂不同。** 只断言「新代码给出了正确答案」是不够的：
桩要是根本没落在那条分支上，这条用例证明的就只是「我写了新代码」，而不是「新判据起了作用」。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
START_SH = ROOT / "start.sh"
START_BAT = ROOT / "start.bat"

# Windows 上「应用执行别名」转发桩的可观察行为：被叫到就往 stderr 写这一句、以 49 退出。
# 桩只要做到这两件事就够 —— 判据看的是「跑不跑得起来」，不是那一句话本身。
_ALIAS_EXIT_CODE = 49
_ALIAS_MESSAGE = (
    "Python was not found; run without arguments to install from the Microsoft Store"
)


def _extract_pick_python() -> str:
    """把 `_pick_python` 的定义**从 start.sh 里原样取出来**（取不到就让用例红）。

    取不到时必须红，不能悄悄跳过：那意味着函数被改名或删了，而这条用例会变成一句
    永远为真的空断言 —— 那正是「假绿」。
    """
    text = START_SH.read_text(encoding="utf-8")
    marker = "_pick_python() {"
    assert marker in text, f"start.sh 里找不到 {marker} —— 函数被改名或删了？"
    start = text.index(marker)
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _fake_path_with(stub_dir: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
    return env


def _write_shell_stub(path: Path) -> None:
    """一个与 Windows 别名同形的壳脚本：挡住同名命令、打印那句话、按 49 退出。"""
    path.write_text(
        "#!/bin/sh\n"
        f"echo '{_ALIAS_MESSAGE}' >&2\n"
        f"exit {_ALIAS_EXIT_CODE}\n",
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(path, 0o755)


@pytest.mark.skipif(sys.platform == "win32" and not shutil.which("bash"), reason="需要 bash")
def test_start_sh_skips_an_unrunnable_python3(tmp_path):
    """`python3` 在 PATH 上但跑不起来时，选中的必须是那个**真的能跑**的解释器。

    机器上必须有另一个可用的解释器（`python`），否则这条用例的前提不成立 —— 那种情况下
    直接红，不静默跳过。
    """
    assert shutil.which("python"), "本机没有可用的 python，这条用例的前提不成立"

    _write_shell_stub(tmp_path / "python3")
    env = _fake_path_with(tmp_path)
    script = _extract_pick_python()

    picked = subprocess.run(
        ["bash", "-c", f"{script}\n_pick_python"],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )
    assert picked.stdout.strip() == "python", (
        f"选中了 {picked.stdout.strip()!r} —— 那个 python3 是跑不起来的转发桩"
    )

    # 同一条 PATH 上跑旧口径：它必须选到那个桩，否则这条用例证明不了「新判据在起作用」。
    old_rule = (
        'PYTHON_CMD=""; '
        "if command -v python3 &> /dev/null; then PYTHON_CMD=python3; "
        "elif command -v python &> /dev/null; then PYTHON_CMD=python; fi; "
        'echo "$PYTHON_CMD"'
    )
    legacy = subprocess.run(
        ["bash", "-c", old_rule], capture_output=True, text=True, env=env, cwd=str(ROOT),
    )
    assert legacy.stdout.strip() == "python3", (
        "旧口径在这条 PATH 下没有选到那个桩 —— 桩没落在那条分支上，"
        "上面那句断言就只是「我写了新判据」，证明不了它起了作用"
    )


def _write_windows_stub(stub_dir: Path) -> None:
    """放一个叫 `python.exe` 的、一跑就失败的可执行文件。

    **为什么是「拷一个真实 exe 过来」而不是自己造一个**：Windows 上没装 Python 时，
    `python` 命中的是「应用执行别名」那个 `.exe` 转发桩 —— 它是**可执行文件**，不是
    批处理。若拿 `.bat` 冒名顶替，`start.bat` 里那句 `python -c …` 会因为「批处理直接
    执行另一个批处理会夺走控制权」而根本不返回，测出来的是另一个故障。

    这里取 `where.exe`：它不认识 `-c`，必然非零退出 —— 与转发桩**可观察的行为**同形
    （叫 python、一跑就失败）。判据看的是这个，不是它打印了什么。
    """
    source = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe"
    assert source.exists(), f"找不到 {source}，没法造这个桩"
    shutil.copyfile(source, stub_dir / "python.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="start.bat 只在 Windows 上跑")
def test_start_bat_reports_an_unrunnable_python_instead_of_continuing(tmp_path):
    """`python` 在 PATH 上但跑不起来时，脚本必须**如实报错并停下**，不能继续往下走。

    判据是整只脚本真的被跑了一遍之后的结果：退出码非 0、且屏幕上出现那句错误提示。
    旧口径（`where python`）在同一条 PATH 上会命中那个桩、检查通过 —— 两臂不同，
    才说明改的是行为而不是写法。

    ## 为什么要在一个空的沙箱目录里跑

    这条用例**必须保证它自己不可能把应用启动起来**。第一次写的时候没这么做，变异验证
    当场把这一点戳出来了：把判据改回 `where python` 之后检查通过、脚本继续往下走，
    最终真的 `python app.py` 起了一个服务 —— 而它的标准输出被父进程的管道接住，
    于是**用例永远不返回**（子进程还活着，`communicate` 等不到 EOF）。判据错了应该红，
    不应该挂住。

    所以 cwd 换成临时目录：那里没有 `app.py`、没有 `venv\\Scripts\\python.exe`
    （脚本会优先用它）、也没有 `.env`。`start.bat` 自己不做 `cd`（已核），走的是相对
    路径 —— 沙箱里那些依赖全都找不到，脚本只能沿着错误分支走到头。
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    _write_windows_stub(tmp_path)
    env = _fake_path_with(tmp_path)

    found = subprocess.run(["cmd", "/c", "where python"], capture_output=True, text=True, env=env)
    assert str(tmp_path).lower() in found.stdout.lower(), (
        "桩没顶到 PATH 最前面，这条用例的前提不成立"
    )

    assert _run_bat(START_BAT, env=env, cwd=sandbox) != 0


def _run_bat(script: Path, *, env: dict, cwd: Path) -> int:
    """跑一遍脚本并**保证不会挂住**：超时就整棵进程树杀掉，然后让用例红。

    `subprocess.run(timeout=…)` 在这里不够用：超时它只杀直接子进程，孙进程还握着管道，
    `communicate` 依旧等不到 EOF。
    """
    proc = subprocess.Popen(
        ["cmd", "/c", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,  # 脚本末尾有 pause；不接标准输入会挂住
        text=True,
        env=env,
        cwd=str(cwd),
    )
    try:
        out, _ = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        proc.kill()
        out, _ = proc.communicate()
        raise AssertionError(f"start.bat 挂住了 —— 本该报错退出。输出结尾：{out[-500:]}")
    assert "not found or is not runnable" in out, out[-800:]
    return proc.returncode
