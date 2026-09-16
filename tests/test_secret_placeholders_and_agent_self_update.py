#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""占位密钥启动校验 + Agent 自更新链路加固的回归测试。

覆盖两个已核实的问题：

1. `.env.simple` 里的 `FLASK_SECRET_KEY` / `AGENT_SHARED_SECRET` 是占位值，
   照抄部署就等于没有密钥（任何人可冒充 Agent 领任务、可伪造管理员 session）。
   `utils/env_bootstrap.py` 里还硬编码着与 `.env.simple` 逐字节相同的默认值。

2. `agent/self_update.py` 的自更新链路三个缺陷：
   - `version` 直接拼进 `shutil.rmtree(...)` → 路径穿越；
   - `download_path` 允许绝对 URL 且下载请求带 Agent 凭据 → 凭据外发；
   - `package_sha256` 可选（`if expect_sha256:`）→ 完整性校验 fail-open。

每条用例的 docstring 都写明「为什么需要它」；断言失败信息里写明「变红意味着什么」。
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from agent import self_update
from utils import env_bootstrap

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_SIMPLE_PATH = REPO_ROOT / ".env.simple"

WEAK_FLASK_SECRET = "请替换为一个随机字符串"
WEAK_AGENT_SECRET = "please-change-me"


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _write_env_file(path: Path, *, flask_secret: str, agent_secret: str) -> Path:
    path.write_text(
        "HOST=0.0.0.0\n"
        f"FLASK_SECRET_KEY={flask_secret}\n"
        f"AGENT_SHARED_SECRET={agent_secret}\n",
        encoding="utf-8",
    )
    return path


def _capture_startup_output(monkeypatch):
    """捕获启动校验打印到 stdout/stderr 的内容。

    为什么不用 pytest 的 capsys：`tests/conftest.py` 把 `sys.stdout/stderr`
    换成了直通真实终端的代理流（绕开 pytest 的捕获），整段中文指引会被刷进
    测试输出。这里显式替换成 StringIO，既让用例能断言「运维确实看得到指引」，
    也保持测试输出干净。
    """
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out, err


def _clear_secret_env(monkeypatch) -> None:
    """清掉环境里的密钥，让断言只看**传进去的那个 .env 文件**。

    为什么必须清：`app.py` 顶层是 `load_dotenv(override=False)`，只要仓库里存在
    真 `.env`（任何部署过的机器都有），**一次 `import app` 就会把
    FLASK_SECRET_KEY / AGENT_SHARED_SECRET 灌进 `os.environ`**；
    而 `utils/env_bootstrap.effective_env_values` 让真实环境变量**优先于文件**。
    于是「文件里写的是占位值」会被环境里的强值掩盖，`check_env_file_secrets`
    返回空列表，前置断言直接失败。

    触发条件只是「本次会话里有没有别的用例先 import 过 app」—— 谁先 import 取决于
    pytest 的收集顺序。实测：全量跑必红，单跑本文件绿。CI 上没有 `.env`
    所以一直没暴露。

    键名从 `_SECRET_MIN_LENGTHS` 取，避免以后新增密钥时这里悄悄漏掉。
    """
    for key in env_bootstrap._SECRET_MIN_LENGTHS:
        monkeypatch.delenv(key, raising=False)


def _no_testing_env(monkeypatch) -> None:
    """把进程切到「非 TESTING」模式，让启动校验真正生效。

    tests/conftest.py 全局设置了 `TESTING=1`，不删掉的话所有用例都会被
    当成测试环境放行，专测就永远看不到拒绝分支。
    """
    _clear_secret_env(monkeypatch)
    monkeypatch.delenv("TESTING", raising=False)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _build_release_zip(zip_path: Path) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        zf.writestr("start_agent.py", "from runner_runtime import main\nmain()\n")
        zf.writestr("runner_runtime.py", "def main():\n    return 0\n")
        zf.writestr("new_module.py", "VALUE = 2\n")
    return zip_path


def _release_payload(**overrides) -> dict:
    release = {
        "version": "new-v2",
        "commit_id": "abc12345",
        "download_path": "/api/agents/releases/new-v2/package",
    }
    release.update(overrides)
    return release


def _run_update(
    monkeypatch,
    tmp_path,
    *,
    release: dict,
    platform_base_url: str = "http://127.0.0.1:8002",
    release_zip: Path | None = None,
):
    """驱动一次 `check_and_apply_update`，返回 (updated, message, download_calls)。"""
    agent_root = tmp_path / "agent_runtime"
    agent_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(self_update, "_agent_root", lambda: str(agent_root))
    monkeypatch.setattr(
        self_update,
        "_install_requirements_if_needed",
        lambda settings, extracted_root: None,
    )

    def _fake_post_json(url, payload, headers=None, timeout=10):
        return 200, {"success": True, "has_update": True, "release": release}

    download_calls: list[dict] = []

    def _fake_download_file(url, target_path, headers=None, timeout=30):
        download_calls.append({"url": url, "headers": dict(headers or {})})
        if release_zip is None:
            return 0, {"success": False, "message": "download must not be attempted"}
        Path(target_path).parent.mkdir(parents=True, exist_ok=True)
        Path(target_path).write_bytes(release_zip.read_bytes())
        return 200, {"success": True}

    monkeypatch.setattr(self_update, "post_json", _fake_post_json)
    monkeypatch.setattr(self_update, "download_file", _fake_download_file)

    settings = SimpleNamespace(
        platform_base_url=platform_base_url,
        agent_code="agent-a",
        auto_update_request_timeout_seconds=15,
        auto_update_download_timeout_seconds=120,
        auto_update_install_deps=False,
    )
    updated, message = self_update.check_and_apply_update(
        settings=settings,
        common_headers={"X-Agent-Secret": "shared-secret"},
        agent_token="token-a",
        log_func=lambda msg: None,
    )
    return updated, message, download_calls


# ==========================================================================
# 问题 1：占位密钥
# ==========================================================================
def test_env_simple_template_secrets_are_flagged_as_placeholders():
    """为什么需要：`.env.simple` 是全仓公开的部署模板，它的两个密钥占位值必须
    能被校验器识别出来 —— 否则「照抄即部署」的漏洞会重新出现。

    这条变红意味着：模板里又出现了可用的/无法识别的密钥占位值，
    部署文档 `cp .env.simple .env` 的用户会拿到一个可被冒充的平台。
    """
    values = env_bootstrap.read_env_file_values(ENV_SIMPLE_PATH)
    issues = env_bootstrap.find_insecure_env_secrets(values)
    flagged = {key for key, _reason, _detail in issues}

    assert flagged == {"FLASK_SECRET_KEY", "AGENT_SHARED_SECRET"}, (
        f"`.env.simple` 的占位密钥未被全部识别: flagged={flagged}, values={values}"
    )
    assert all(reason == "placeholder" for _key, reason, _detail in issues)


def test_env_simple_does_not_ship_a_usable_secret():
    """为什么需要：模板里绝不能出现真实可用（随机、够长）的密钥 —— 那等于把
    一个「看起来已经配好了」的钥匙发给所有拿到仓库的人。

    这条变红意味着：`.env.simple` 里的某个密钥已经是一个可用的随机值，
    所有照抄模板的部署会共用同一个公开密钥。
    """
    values = env_bootstrap.read_env_file_values(ENV_SIMPLE_PATH)
    for key in ("FLASK_SECRET_KEY", "AGENT_SHARED_SECRET"):
        raw = str(values.get(key) or "").strip()
        assert raw, f"`.env.simple` 缺少 {key}，模板必须显式暴露这个待替换项"
        assert env_bootstrap.is_placeholder_secret(raw), (
            f"`.env.simple` 的 {key} 不是可识别的占位值（长度 {len(raw)}）—— "
            "它可能是一个真实可用的密钥，必须换成 __REPLACE_ME_...__ 形式"
        )


def test_startup_gate_refuses_placeholder_secrets_non_testing(monkeypatch, tmp_path):
    """为什么需要：这是漏洞的**主要拦截点**。照抄模板部署必须拒绝启动，
    否则公开可猜的 AGENT_SHARED_SECRET 会让任何人注册假 Agent 领取任务。

    这条变红意味着：平台会带着公开可猜的共享密钥启动，
    `/api/agents/register` 可被任意人冒充调用。
    """
    _no_testing_env(monkeypatch)
    env_path = _write_env_file(
        tmp_path / ".env",
        flask_secret=WEAK_FLASK_SECRET,
        agent_secret=WEAK_AGENT_SECRET,
    )
    monkeypatch.setattr("sys.argv", ["env_bootstrap", "--env-path", str(env_path)])
    _out, err = _capture_startup_output(monkeypatch)

    assert env_bootstrap.main() == 2, "占位密钥必须让启动校验失败（退出码 2）"
    issues = env_bootstrap.check_env_file_secrets(env_path)
    assert {key for key, _r, _d in issues} == {"FLASK_SECRET_KEY", "AGENT_SHARED_SECRET"}

    printed = err.getvalue()
    assert "secrets.token_urlsafe" in printed, "运维必须能在终端看到可直接照做的生成命令"
    assert "启动被拒绝" in printed
    # 只报键名与原因，不回显密钥值本身 —— 即便是弱密钥也不该出现在终端/日志里。
    assert "AGENT_SHARED_SECRET" in printed and "FLASK_SECRET_KEY" in printed
    assert WEAK_AGENT_SECRET not in printed


def test_startup_gate_accepts_strong_random_secrets(monkeypatch, tmp_path):
    """为什么需要：修复不能误伤正常部署 —— 随机且够长的密钥必须放行，
    否则运维会以为平台坏了。

    这条变红意味着：正常的随机密钥被拒，平台无法启动（误报）。
    """
    _no_testing_env(monkeypatch)
    env_path = _write_env_file(
        tmp_path / ".env",
        flask_secret="A" * 48,
        agent_secret="B" * 48,
    )
    monkeypatch.setattr("sys.argv", ["env_bootstrap", "--env-path", str(env_path)])
    _capture_startup_output(monkeypatch)

    assert env_bootstrap.find_insecure_env_secrets(env_bootstrap.read_env_file_values(env_path)) == []
    assert env_bootstrap.main() == 0


def test_startup_gate_is_lenient_under_testing(monkeypatch, tmp_path):
    """为什么需要：项目规则要求测试环境不能被这条校验卡住；conftest 全局设了
    `TESTING=1`，这里必须能复现「测试环境即使有占位值也放行」。

    这条变红意味着：pytest 套件会因为占位值而起不来（或该豁免被删掉）。
    """
    # 这条要保留 TESTING=1，所以不能直接用 _no_testing_env；但**必须**清掉环境里的
    # 密钥 —— 否则有真 .env 的机器上，import app 灌进来的强密钥会盖掉这里写的占位值，
    # 前置断言在「别的用例先 import 过 app」时失败（本文件其它用例都调了
    # _no_testing_env，只有这条漏了）。
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("TESTING", "1")
    env_path = _write_env_file(
        tmp_path / ".env",
        flask_secret=WEAK_FLASK_SECRET,
        agent_secret=WEAK_AGENT_SECRET,
    )
    monkeypatch.setattr("sys.argv", ["env_bootstrap", "--env-path", str(env_path)])
    _capture_startup_output(monkeypatch)

    assert env_bootstrap.check_env_file_secrets(env_path), "前置条件：该文件确实含占位值"
    assert env_bootstrap.main() == 0, "TESTING=1 时必须放行"


def test_generated_env_passes_its_own_startup_gate(tmp_path):
    """为什么需要：全新安装（`.env` 不存在）由 env_bootstrap 生成配置，生成结果
    必须能直接通过校验 —— 否则「首次启动」会被自己拦住，等于修死。

    这条变红意味着：fresh install 的自动生成 `.env` 含占位/过短密钥，
    首次启动即失败；或生成逻辑又写回了 `please-change-me`。
    """
    env_path = tmp_path / ".env"
    action, creds = env_bootstrap.ensure_env_file(env_path)

    assert action == "generated"
    issues = env_bootstrap.check_env_file_secrets(env_path)
    assert issues == [], f"自动生成的 .env 未通过校验: {issues}"
    assert creds.get("AGENT_SHARED_SECRET") != WEAK_AGENT_SECRET


def test_short_secret_is_rejected(monkeypatch, tmp_path):
    """为什么需要：占位值只是弱密钥的一种。短密钥（熵不足）同样是可爆破的，
    必须一并拦下，否则「改成短字符串」就成了绕过口子。

    这条变红意味着：长度不足的密钥被放行，弱密钥可通过缩短来绕过校验。
    """
    _no_testing_env(monkeypatch)
    env_path = _write_env_file(tmp_path / ".env", flask_secret="abc", agent_secret="xyz")
    issues = env_bootstrap.check_env_file_secrets(env_path)

    reasons = {key: reason for key, reason, _detail in issues}
    assert reasons == {"FLASK_SECRET_KEY": "too_short", "AGENT_SHARED_SECRET": "too_short"}


def test_empty_secret_is_only_a_warning_not_fatal(monkeypatch, tmp_path):
    """为什么需要：文档里承诺「留空 = 未配置，保留既有降级行为」（运行期随机
    FLASK_SECRET_KEY + Agent 接口 503）。这条锁定该承诺，防止校验过严把
    现有单机部署全部拒之门外。

    这条变红意味着：留空密钥的既有部署被拒绝启动，属于破坏性行为变更。
    """
    _no_testing_env(monkeypatch)
    env_path = _write_env_file(tmp_path / ".env", flask_secret="", agent_secret="")

    assert env_bootstrap.check_env_file_secrets(env_path) == []
    monkeypatch.setattr("sys.argv", ["env_bootstrap", "--env-path", str(env_path)])
    _capture_startup_output(monkeypatch)
    assert env_bootstrap.main() == 0


def test_real_environment_overrides_env_file(monkeypatch, tmp_path):
    """为什么需要：`app.py` 用 `load_dotenv(override=False)`，真实环境变量优先。
    校验器必须用同一优先级，否则「环境变量配了强密钥、.env 里留占位值」的
    部署会被误判为不安全而无法启动。

    这条变红意味着：校验优先级与运行时不一致，会产生无法启动的误报。
    """
    _no_testing_env(monkeypatch)
    env_path = _write_env_file(
        tmp_path / ".env",
        flask_secret=WEAK_FLASK_SECRET,
        agent_secret=WEAK_AGENT_SECRET,
    )
    monkeypatch.setenv("FLASK_SECRET_KEY", "C" * 48)
    monkeypatch.setenv("AGENT_SHARED_SECRET", "D" * 48)

    assert env_bootstrap.check_env_file_secrets(env_path) == []


def test_assert_env_secrets_secure_raises_for_placeholders():
    """为什么需要：给其他启动链路（例如未来的 runtime_entry）提供一个
    fail-closed 的断言入口，异常信息必须是可直接照做的中文指引。

    这条变红意味着：断言入口不抛异常或信息缺少修复步骤，其他调用方无法 fail-closed。
    """
    with pytest.raises(env_bootstrap.InsecureEnvSecretError) as exc_info:
        env_bootstrap.assert_env_secrets_secure(
            {"FLASK_SECRET_KEY": WEAK_FLASK_SECRET, "AGENT_SHARED_SECRET": WEAK_AGENT_SECRET}
        )

    text = str(exc_info.value)
    assert "secrets.token_urlsafe" in text
    assert "AGENT_SHARED_SECRET" in text
    assert "FLASK_SECRET_KEY" in text


# ==========================================================================
# 问题 2-1：version 路径穿越
# ==========================================================================
@pytest.mark.parametrize(
    "bad_version",
    [
        "../../..",
        "../../victim",
        "/etc/passwd",
        "..\\..\\victim",
        r"C:\Windows\System32",
        "..",
        "./..",
        "foo/../../bar",
        "foo/bar",
        "",
        "a" * 200,
    ],
)
def test_unsafe_release_version_is_rejected(bad_version):
    """为什么需要：`version` 直接进入 `os.path.join(root, ".agent_update_tmp", version)`
    并交给 `shutil.rmtree`。放行任意字符串等于给平台一个「删除 Agent 主机任意目录」的原语。

    这条变红意味着：自更新可以接受穿越/绝对/含分隔符的版本号，
    `version="../../.."` 会删掉 Agent 目录之外的文件。
    """
    assert self_update._is_safe_release_version(bad_version) is False


@pytest.mark.parametrize("good_version", ["v1", "1.2.3", "new-v2", "release_2026.09.16", "v1.0.0-beta1"])
def test_safe_release_version_is_accepted(good_version):
    """为什么需要：防穿越不能把正常版本号一起挡掉，否则自更新彻底不可用。

    这条变红意味着：正常发布的版本号被拒，Agent 无法自更新。
    """
    assert self_update._is_safe_release_version(good_version) is True


def test_safe_join_within_rejects_escapes(tmp_path):
    """为什么需要：包含性校验是版本号正则之外的第二道防线（纵深防御）。
    即使版本号校验被放宽或被绕过，落点仍必须留在 base 目录内。

    这条变红意味着：路径拼接可逃出 base 目录，`shutil.rmtree` 会作用到外部路径。
    """
    base = tmp_path / "agent_root"
    base.mkdir()

    assert Path(self_update._safe_join_within(str(base), "sub", "file.txt")).is_relative_to(base)

    for escape in ("..", "../..", "../../victim", str(tmp_path / "victim")):
        with pytest.raises(RuntimeError):
            self_update._safe_join_within(str(base), escape)


def test_traversal_version_does_not_delete_outside_directory(monkeypatch, tmp_path):
    """为什么需要：这是漏洞的**端到端**证明 —— 不只是「函数返回 False」，
    而是「目录真的还在」。构造 `version="../../victim"`，旧代码会把
    `tmp_path/victim` 整个 rmtree 掉。

    这条变红意味着：路径穿越真实可达，攻击者可通过发布清单删除 Agent 主机任意目录。
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("do not delete\n", encoding="utf-8")

    updated, message, download_calls = _run_update(
        monkeypatch,
        tmp_path,
        release=_release_payload(version="../../victim"),
    )

    assert updated is False, f"穿越版本号必须被拒绝，实际 message={message!r}"
    assert "不合法" in message
    assert victim.is_dir() and (victim / "important.txt").exists(), (
        "目录被删除了 —— 说明 version 仍能穿越出 Agent 目录"
    )
    assert download_calls == [], "版本号不合法时不应发起下载"


# ==========================================================================
# 问题 2-2：非同源下载 URL + 凭据外发
# ==========================================================================
@pytest.mark.parametrize(
    "bad_url",
    [
        "http://evil.example.com/pkg.zip",
        "https://evil.example.com/pkg.zip",
        "http://127.0.0.1:9999/pkg.zip",  # 同主机不同端口
        "https://127.0.0.1:8002/pkg.zip",  # 同主机同端口不同 scheme
        "http://127.0.0.1:8002@evil.example.com/pkg.zip",  # userinfo 混淆
    ],
)
def test_cross_origin_download_url_is_rejected(monkeypatch, tmp_path, bad_url):
    """为什么需要：下载请求带 `X-Agent-Token` 与共享凭据。如果 `download_path`
    可以是任意绝对 URL，一个被篡改的发布清单就能把整个机群的凭据发给攻击者主机 ——
    这比装个坏包严重得多。

    这条变红意味着：Agent 会把机群凭据发送到第三方主机（凭据外泄）。
    """
    release_zip = _build_release_zip(tmp_path / "src" / "release.zip")
    updated, message, download_calls = _run_update(
        monkeypatch,
        tmp_path,
        release=_release_payload(
            download_path=bad_url,
            package_sha256=_sha256_file(release_zip),
        ),
        release_zip=release_zip,
    )

    assert updated is False, f"非同源地址必须被拒绝，实际 message={message!r}"
    assert "不同源" in message
    assert download_calls == [], "非同源地址绝不能被请求（凭据外泄）"


def test_same_origin_absolute_url_is_allowed(monkeypatch, tmp_path):
    """为什么需要：同源校验不能过严 —— 平台返回绝对 URL（同源）是合法形态，
    必须放行，否则自更新彻底失效。

    这条变红意味着：合法的同源绝对 URL 被拒，自更新不可用。
    """
    release_zip = _build_release_zip(tmp_path / "src" / "release.zip")
    updated, message, download_calls = _run_update(
        monkeypatch,
        tmp_path,
        release=_release_payload(
            download_path="http://127.0.0.1:8002/api/agents/releases/new-v2/package",
            package_sha256=_sha256_file(release_zip),
        ),
        release_zip=release_zip,
    )

    assert updated is True, f"同源绝对 URL 应放行，实际 message={message!r}"
    assert len(download_calls) == 1


def test_default_port_normalization_does_not_bypass_origin_check():
    """为什么需要：同源比较必须归一化默认端口，否则 `https://h` 与
    `https://h:443` 会被当成不同源（误报），或省略端口时绕过比较（漏报）。

    这条变红意味着：同源判断在省略端口时不可靠。
    """
    assert self_update._is_same_origin("https://diff.example.com", "https://diff.example.com:443/pkg")
    assert self_update._is_same_origin("http://diff.example.com", "http://diff.example.com:80/pkg")
    assert not self_update._is_same_origin("https://diff.example.com", "https://diff.example.com:8443/pkg")


def test_non_http_scheme_is_never_same_origin():
    """为什么需要：`file://`、`ftp://`、`data:` 等 scheme 若被判为「同源」，
    就等于允许 Agent 从本地文件系统或任意协议处理器取「安装包」。

    这条变红意味着：非 http(s) 的下载地址可通过同源检查。
    """
    for scheme in ("file:///etc/passwd", "ftp://diff.example.com/pkg.zip", "data:text/plain,x", "//diff.example.com/pkg"):
        assert self_update._is_same_origin("https://diff.example.com", scheme) is False


def test_uppercase_sha256_is_normalized_and_accepted(monkeypatch, tmp_path):
    """为什么需要：清单里的摘要大小写不应影响判定 —— 大小写不敏感地归一化后比较，
    避免「摘要其实对得上但因为大小写被拒」的误报。

    这条变红意味着：大写摘要被误判为格式非法，正常发布包无法安装。
    """
    release_zip = _build_release_zip(tmp_path / "src" / "release.zip")
    updated, message, _download_calls = _run_update(
        monkeypatch,
        tmp_path,
        release=_release_payload(package_sha256=_sha256_file(release_zip).upper()),
        release_zip=release_zip,
    )

    assert updated is True, f"大写摘要应被归一化后接受，实际 message={message!r}"


# ==========================================================================
# 问题 2-3：package_sha256 必填（fail-closed）
# ==========================================================================
@pytest.mark.parametrize(
    "bad_sha",
    [None, "", "   ", "not-a-sha", "abc123", "0" * 63, "g" * 64, "0" * 64 + "0"],
)
def test_missing_or_malformed_sha256_refuses_install(monkeypatch, tmp_path, bad_sha):
    """为什么需要：旧实现是 `if expect_sha256:` —— 清单里不写摘要就**完全不校验**。
    能篡改发布清单的攻击者只要删掉这个字段，就能投递任意安装包。这是典型的
    fail-open，必须变成 fail-closed。

    这条变红意味着：缺少/非法摘要时仍会安装，完整性保护可被一个字段删除绕过。
    """
    release_zip = _build_release_zip(tmp_path / "src" / "release.zip")
    release = _release_payload()
    if bad_sha is not None:
        release["package_sha256"] = bad_sha

    updated, message, _download_calls = _run_update(
        monkeypatch,
        tmp_path,
        release=release,
        release_zip=release_zip,
    )

    assert updated is False, f"缺少合法摘要必须拒绝安装，实际 message={message!r}"
    assert "package_sha256" in message
    assert not (tmp_path / "agent_runtime" / "start_agent.py").exists(), "拒绝后不应落盘任何文件"
    assert not (tmp_path / "agent_runtime" / ".agent_release_state.json").exists()


def test_sha256_mismatch_refuses_install(monkeypatch, tmp_path):
    """为什么需要：摘要存在但内容不符 = 包被替换/损坏，必须拒绝安装。
    旧代码这里抛 RuntimeError（拒绝安装），新实现改为「拒绝并把原因写进返回消息」，
    让平台侧能看到明确原因，而不是一个泛化异常。

    这条变红意味着：摘要不匹配的包仍被安装（供应链投毒可达）。
    """
    release_zip = _build_release_zip(tmp_path / "src" / "release.zip")
    updated, message, download_calls = _run_update(
        monkeypatch,
        tmp_path,
        release=_release_payload(package_sha256="f" * 64),
        release_zip=release_zip,
    )

    assert updated is False, f"摘要不匹配必须拒绝安装，实际 message={message!r}"
    assert "sha256" in message and "校验失败" in message
    assert len(download_calls) == 1
    assert not (tmp_path / "agent_runtime" / "start_agent.py").exists()
    assert not (tmp_path / "agent_runtime" / ".agent_release_state.json").exists()


def test_valid_release_with_all_guards_satisfied_still_installs(monkeypatch, tmp_path):
    """为什么需要：三个加固点都不能把正常发布流程打死 —— 同源地址 + 合法摘要 +
    合法版本号必须能正常安装并写入状态文件。

    这条变红意味着：正常自更新被安全校验误伤，Agent 无法升级。
    """
    release_zip = _build_release_zip(tmp_path / "src" / "release.zip")
    updated, message, _download_calls = _run_update(
        monkeypatch,
        tmp_path,
        release=_release_payload(
            package_sha256=_sha256_file(release_zip),
            package_size=release_zip.stat().st_size,
        ),
        release_zip=release_zip,
    )

    agent_root = tmp_path / "agent_runtime"
    assert updated is True, f"合法发布包应安装成功，实际 message={message!r}"
    assert message == "updated to new-v2"
    assert (agent_root / "start_agent.py").exists()
    assert (agent_root / "new_module.py").exists()
    state = json.loads((agent_root / ".agent_release_state.json").read_text(encoding="utf-8"))
    assert state.get("version") == "new-v2"
