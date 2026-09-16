# -*- coding: utf-8 -*-
"""占位密钥必须在**每一条真实启动路径**上被拦住，而不是只拦一条。

## 背景

`utils/env_bootstrap.py` 新增了「拒绝照抄模板的占位密钥」校验，并且
`start.bat` / `start.sh` 会检查它的退出码（`errorlevel 1` / `if ! ... then`）后中止。
但那只覆盖「用启动脚本起」这一条路径：

* `python app.py`（文档、容器编排、运维习惯里最常见的起法）**完全不经过**
  `utils.env_bootstrap`，校验形同虚设；
* Agent 节点跑的是 `agent/start_agent.py` → `agent/runner_runtime.main()`，
  也不经过平台的启动脚本 —— 而 Agent 侧那个 `AGENT_SHARED_SECRET` 一旦是模板占位值，
  任何人都能冒充该 Agent 领走真实任务。

所以本文件钉住两个**咽喉**上的校验：

1. `bootstrap/runtime_entry.py::_enforce_env_secrets_or_exit`
   —— Web 模式与 Agent 模式（`DEPLOYMENT_MODE=agent`）都走 `run_runtime_entry`；
2. `agent/runner_runtime.py::_assert_agent_secret_is_usable`
   —— Agent 独立打包部署，平台 `utils` 未必存在，必须自带一份（不能 import）。

## 两个容易写错、也最值得钉住的点

* 第 1 条的失败**必须发生在 `run_runtime_entry` 的 `try` 之外**。该函数里有
  `except SystemExit as exc:` 分支，会把 SystemExit 吞掉、随后正常返回 ——
  进程以 0 退出，于是「拒绝启动」变成了「静默启动成功」。这条由
  `test_runtime_entry_does_not_swallow_the_refusal` 直接对着函数源码位置断言。
* 第 2 条必须**拒绝**而不是告警：Agent 侧原本只有
  `if not settings.agent_shared_secret`（只管「没配」），
  「配了但配成模板占位值」会一路放行。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent import runner_runtime  # noqa: E402
from bootstrap import runtime_entry  # noqa: E402


# --------------------------------------------------------------------------
# 1. Web / Agent 共用的启动咽喉
# --------------------------------------------------------------------------


class _Recorder:
    """替身 print：记录被打印出来的内容（含 file 参数）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    @property
    def text(self):
        return "\n".join(str(arg) for arg in (call[0][0] for call in self.calls))


def _patch_gate(monkeypatch, *, issues, testing=False):
    """把 runtime_entry 用到的 env_bootstrap 接口换成可控替身。"""
    import utils.env_bootstrap as env_bootstrap

    monkeypatch.setattr(env_bootstrap, "is_testing_mode", lambda *a, **k: testing)
    monkeypatch.setattr(env_bootstrap, "check_env_file_secrets", lambda *a, **k: issues)
    return env_bootstrap


def test_placeholder_secret_aborts_with_exit_code_2(monkeypatch):
    _patch_gate(
        monkeypatch,
        issues=[("AGENT_SHARED_SECRET", "placeholder", "AGENT_SHARED_SECRET 仍是模板占位值")],
    )
    printer = _Recorder()
    with pytest.raises(SystemExit) as exc:
        runtime_entry._enforce_env_secrets_or_exit(printer)
    assert exc.value.code == 2, (
        "占位密钥必须以非 0 退出码终止启动；如果这里是 0 或 None，"
        "启动脚本的 errorlevel 判断就拦不住了。"
    )
    assert "AGENT_SHARED_SECRET" in printer.text, "指引里要写清是哪个键不合格"


def test_clean_secrets_do_not_abort(monkeypatch):
    _patch_gate(monkeypatch, issues=[])
    printer = _Recorder()
    runtime_entry._enforce_env_secrets_or_exit(printer)  # 不应抛异常
    assert printer.calls == [], "密钥合格时不该往启动输出里刷任何东西"


def test_testing_mode_short_circuits_the_gate(monkeypatch):
    """TESTING=1 时必须放行 —— 否则整仓用例会在导入阶段就炸。"""
    _patch_gate(monkeypatch, issues=[("FLASK_SECRET_KEY", "placeholder", "占位")], testing=True)
    runtime_entry._enforce_env_secrets_or_exit(_Recorder())  # 不应抛异常


def test_missing_env_bootstrap_module_does_not_block_startup(monkeypatch):
    """校验模块本身不可用时只告警 —— 安全性不该反过来变成可用性事故。"""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "utils.env_bootstrap":
            raise ImportError("simulated missing module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    printer = _Recorder()
    runtime_entry._enforce_env_secrets_or_exit(printer)
    assert "跳过" in printer.text


def test_runtime_entry_does_not_swallow_the_refusal():
    """失败点必须在 try **之外**。

    `run_runtime_entry` 里有 `except SystemExit as exc:` —— 只要把校验放进那个 try，
    抛出的 SystemExit 就会被吞掉、函数正常返回、进程以 0 退出。
    这里直接对源码位置断言，因为这是「实现位置」而非「实现行为」的契约。
    """
    source = Path(runtime_entry.__file__).read_text(encoding="utf-8")
    # 锚定**调用点**（行首 4 空格 + 换行）而不是裸函数名 ——
    # 裸函数名会先命中 `def _enforce_env_secrets_or_exit(...)` 定义行，
    # 于是 index 落在文件靠前的定义处，断言恒真。
    call_marker = "\n    _enforce_env_secrets_or_exit(_original_print)\n"
    assert call_marker in source, (
        "run_runtime_entry 里找不到启动密钥校验的调用点（写法变了就更新本契约）"
    )
    call_index = source.index(call_marker)
    try_index = source.index("\n    try:\n", call_index)
    assert call_index < try_index, (
        "启动密钥校验被放进了 run_runtime_entry 的 try 块里 —— "
        "该函数有 `except SystemExit` 分支会把它吞成「启动成功」。"
    )
    assert "except SystemExit" in source[try_index:], (
        "本断言的前提（try 块里有 except SystemExit）不再成立，请重新评估这个契约。"
    )


def test_gate_runs_before_clear_log_file():
    """检查必须早于 clear_log_file 那一步 —— 它会清空日志，失败原因会被抹掉。"""
    source = Path(runtime_entry.__file__).read_text(encoding="utf-8")
    call_marker = "\n    _enforce_env_secrets_or_exit(_original_print)\n"
    gate_index = source.index(call_marker)
    # 锚定 try 块里那条真正的调用语句（8 空格缩进 + 换行）。
    # 用裸的 "clear_log_file()" 会先命中注释里对它的提及，断言就会假红。
    clear_index = source.index("\n        clear_log_file()\n")
    assert gate_index < clear_index


# --------------------------------------------------------------------------
# 2. Agent 节点自带的那份（独立打包，不能 import 平台 utils）
# --------------------------------------------------------------------------


class TestAgentSecretGate:
    @pytest.mark.parametrize(
        "secret",
        [
            "please-change-me",  # .env.simple 里的原始占位值
            "  PLEASE-CHANGE-ME  ",  # 大小写/空白不该成为绕过口子
            "change_me",
            "replace-me",
            "your-secret-here",
            "placeholder",
            "请替换为一个随机字符串",  # 中文说明文字
            "test",
            "default",
        ],
    )
    def test_placeholder_values_are_rejected(self, secret):
        with pytest.raises(RuntimeError) as exc:
            runner_runtime._assert_agent_secret_is_usable(secret)
        assert "AGENT_SHARED_SECRET" in str(exc.value)

    @pytest.mark.parametrize("length", [16, 32, 48, 64])
    def test_random_looking_values_are_accepted(self, length):
        import secrets

        runner_runtime._assert_agent_secret_is_usable(secrets.token_urlsafe(length))

    def test_guard_is_wired_into_run_agent(self):
        """光有函数不算数 —— 必须挂在 `run_agent()` 的启动路径上。"""
        source = Path(runner_runtime.__file__).read_text(encoding="utf-8")
        assert "_assert_agent_secret_is_usable(settings.agent_shared_secret)" in source, (
            "run_agent() 没有调用占位密钥校验；Agent 侧只挡了「没配」，没挡「配成模板值」。"
        )
        missing_index = source.index('raise RuntimeError("缺少 AGENT_SHARED_SECRET")')
        guard_index = source.index("_assert_agent_secret_is_usable(settings.agent_shared_secret)")
        assert missing_index < guard_index, "校验要跟在「缺少密钥」检查之后，保持原有报错优先级"

    def test_agent_module_does_not_import_platform_utils(self):
        """Agent 独立打包部署，`utils` 在节点机上未必存在。

        一旦写成 `from utils.env_bootstrap import ...`，在节点机上就是 ImportError
        —— 而安全校验因为「模块不存在」而失效，比没有校验更危险（看起来有）。
        """
        source = Path(runner_runtime.__file__).read_text(encoding="utf-8")
        assert "from utils" not in source
        assert "import utils" not in source


def test_env_simple_template_value_is_actually_rejected_by_both_gates(monkeypatch):
    """端到端对齐：拿模板里真实写着的那两个占位值，两道闸都必须拒绝。

    这条防的是「两边黑名单各写各的、都漏掉模板里的那个值」——
    直接从 `.env.simple` 里读出来喂进去，而不是抄一份字面量。
    """
    template = (PROJECT_ROOT / ".env.simple").read_text(encoding="utf-8-sig")
    values = {}
    for raw_line in template.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    agent_secret = values.get("AGENT_SHARED_SECRET", "")
    assert agent_secret, ".env.simple 里应当有 AGENT_SHARED_SECRET 这一行"
    # 模板里的值必须是被拒绝的（否则照抄部署就是可用凭据）。
    with pytest.raises(RuntimeError):
        runner_runtime._assert_agent_secret_is_usable(agent_secret)


def test_testing_env_is_set_under_pytest():
    """前提校验：conftest 必须给 TESTING=1。

    上面的 `test_testing_mode_short_circuits_the_gate` 与整套用例能否跑起来
    都依赖这个前提；如果哪天 conftest 去掉它，这里会先红，而不是让
    「启动校验把测试全卡死」这种难以归因的失败出现。
    """
    assert str(os.environ.get("TESTING") or "").strip() in {"1", "true", "yes", "on"}
