# -*- coding: utf-8 -*-
"""测试仓库连接时，不得把仓库 URL 内嵌的凭据写进日志或回显给浏览器。

## 为什么需要这个测试

仓库 URL 常被整条粘贴成 `https://oauth2:<PAT>@git.example.com/x.git` —— 于是
**URL 本身就是凭据载体**。`services/repository_admin_handlers.py` 的「测试仓库」
入口把 URL 原样写进日志（`logs/runlog.log` 是持久化文件、保留多份备份），并把
底层 git 返回的错误文本（其中常带整条 remote URL）回显到页面上。

同仓库并非没有意识到这件事：`services/git_service.py` / `enhanced_git_service.py`
早已统一过 `utils/security_utils.sanitize_url()`，这里是漏网的一处。本文件同时
锁住三件事，避免将来有人把它改回去：

1. 日志里的 URL 已脱敏；
2. 回显给浏览器的成功/失败/异常三种文案都不含明文 token；
3. 脱敏**不是**「把整段文本删掉」——不含凭据的文本必须原样保留（否则测试会在
   实现被改成 `return ""` 时依然通过，等于空断言）。
"""
import os
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services import repository_admin_handlers as handlers  # noqa: E402

# 刻意带上 http 形态的 userinfo —— 这正是被泄漏的那一种。
RAW_TOKEN = "ghp_AAAbbbCCCddd"
RAW_URL = f"https://oauth2:{RAW_TOKEN}@git.example.com/game/config.git"
MASKED_HOST_PART = "git.example.com/game/config.git"


class _Recorder:
    """替代 log_print：记录每条日志文本，供断言检查。"""

    def __init__(self):
        self.messages = []

    def __call__(self, message, *args, **kwargs):
        self.messages.append(str(message))


def _fake_repository(**overrides):
    base = {
        "id": 7,
        "name": "config",
        "url": RAW_URL,
        "token": RAW_TOKEN,
        "type": "git",
        "branch": "main",
        "project_id": 3,
        "project": SimpleNamespace(id=3, code="G119"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeQuery:
    def __init__(self, repository):
        self._repository = repository

    def get_or_404(self, _repository_id):
        return self._repository


class _FakeService:
    def __init__(self, *, ssh_ok=True, result=(False, ""), raises=None):
        self.local_path = "C:/repos/G119_config_7"
        self._ssh_ok = ssh_ok
        self._result = result
        self._raises = raises

    def test_ssh_connection(self):
        return self._ssh_ok

    def clone_or_update_repository(self):
        if self._raises is not None:
            raise self._raises
        return self._result


def _call_view(monkeypatch, repository, service, *, accept_json=True):
    """在请求上下文里调用被 @require_admin 包裹的真实视图函数。"""
    from flask import Flask

    recorder = _Recorder()
    fake_model = SimpleNamespace(query=_FakeQuery(repository))

    monkeypatch.setattr(handlers, "_runtime", lambda *names: (fake_model, recorder))
    monkeypatch.setattr(
        handlers,
        "get_runtime_model",
        # 视图里是 `get_git_service(repository)` —— 这里要交回「能被调用」的东西。
        lambda name: (lambda *_a, **_k: service) if name == "get_git_service" else (lambda *_a, **_k: None),
    )
    monkeypatch.setattr(handlers, "is_agent_dispatch_mode", lambda: False)

    app = Flask(__name__)
    app.secret_key = "test-secret"
    with app.test_request_context(
        "/repositories/7/test-connection",
        headers={"Accept": "application/json"} if accept_json else {},
    ):
        from flask import session

        # 环境变量管理员会话（未绑定 auth_user_id）——require_admin 放行的既有路径。
        session["is_admin"] = True
        response = handlers.test_repository(7)

    payload = None
    status_code = None
    if isinstance(response, tuple):
        payload = response[0].get_json()
        status_code = response[1]
    return recorder, payload, status_code


def _assert_no_credentials(text, *, where):
    assert RAW_TOKEN not in text, f"{where} 泄漏了明文 token: {text}"
    assert f"oauth2:{RAW_TOKEN}" not in text, f"{where} 泄漏了 userinfo: {text}"


def _assert_url_still_diagnosable(text, *, where):
    """脱敏后仍要能看出是哪个仓库 —— 不是把整段删掉。"""
    assert MASKED_HOST_PART in text, f"{where} 把 URL 整段抹掉了，排障信息丢失: {text}"


class TestConnectionTestRedaction:
    def test_url_log_is_masked(self, monkeypatch):
        recorder, _, _ = _call_view(
            monkeypatch, _fake_repository(), _FakeService(result=(True, "ok"))
        )
        url_logs = [m for m in recorder.messages if m.startswith("仓库URL:")]
        assert url_logs, "没有记录仓库 URL 日志，用例前提失效"
        for message in url_logs:
            _assert_no_credentials(message, where="日志")
            _assert_url_still_diagnosable(message, where="日志")

    def test_failure_message_echoed_to_browser_is_masked(self, monkeypatch):
        # 底层 git 的报错常把整条 remote URL 拼进去
        git_error = f"fatal: could not read from '{RAW_URL}'"
        _, payload, status_code = _call_view(
            monkeypatch, _fake_repository(), _FakeService(result=(False, git_error))
        )
        assert status_code == 400
        _assert_no_credentials(payload["message"], where="失败回显")
        _assert_url_still_diagnosable(payload["message"], where="失败回显")

    def test_success_message_echoed_to_browser_is_masked(self, monkeypatch):
        _, payload, status_code = _call_view(
            monkeypatch,
            _fake_repository(),
            _FakeService(result=(True, f"仓库已同步: {RAW_URL}")),
        )
        assert status_code == 200
        _assert_no_credentials(payload["message"], where="成功回显")
        _assert_url_still_diagnosable(payload["message"], where="成功回显")

    def test_exception_text_is_masked_in_log_and_response(self, monkeypatch):
        exc = RuntimeError(f"git clone failed for {RAW_URL}")
        recorder, payload, status_code = _call_view(
            monkeypatch, _fake_repository(), _FakeService(raises=exc)
        )
        assert status_code == 500
        _assert_no_credentials(payload["message"], where="异常回显")
        # 栈文本也会被写进日志（stderr 常被重定向进文件），必须一起脱敏
        for message in recorder.messages:
            _assert_no_credentials(message, where="异常日志")
        assert any("Traceback" in m for m in recorder.messages), "异常栈没有落日志，用例前提失效"

    def test_repository_without_embedded_credentials_is_untouched(self, monkeypatch):
        """反向用例：没有凭据的 URL 必须原样保留，防止实现被改成「一律清空」。"""
        plain_url = "https://git.example.com/game/config.git"
        repository = _fake_repository(url=plain_url, token="")
        recorder, payload, _ = _call_view(
            monkeypatch, repository, _FakeService(result=(False, f"fatal: bad revision in {plain_url}"))
        )
        assert plain_url in payload["message"]
        assert any(plain_url in m for m in recorder.messages)

    def test_short_token_is_not_blindly_replaced(self, monkeypatch):
        """极短 token 不做文本替换，避免误伤正常文本（如把 abclog 里的 abc 换掉）。"""
        repository = _fake_repository(token="abc", url="https://git.example.com/abclog.git")
        recorder, payload, _ = _call_view(
            monkeypatch,
            repository,
            _FakeService(result=(True, "abclog 同步完成")),
        )
        assert "abclog" in payload["message"], "短 token 触发了误替换"
        assert any("abclog" in m for m in recorder.messages)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
