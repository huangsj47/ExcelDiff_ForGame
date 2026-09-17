"""模型客户端：URL 归一、重试边界、密钥脱敏、SSE 解析。

## 为什么不用 responses / requests-mock

`requirements-dev.txt` 里没有它们，为了几个用例引入新依赖不划算。这里直接把
`requests.request` 换成一个受控的假实现，既能精确控制状态码、响应头与流式行，
又能断言「打了几次请求、每次睡了多久」——这些恰恰是本文件最需要验证的东西。

## 密钥脱敏是本文件的重点

错误信息会一路走到日志、数据库的 `error_message`、最后渲染到页面上。上游把 token
回显在错误体里、或者把凭据放在 URL 里都很常见，所以**每一个**抛异常的分支都要断言
密钥没漏出去，而不只是测一个代表。
"""

from __future__ import annotations

import json

import pytest
import requests

from services.ai import llm_client as llm
from services.ai.llm_client import (
    CONTEXT_WINDOW_KEYS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    MAX_MODELS,
    LLMClient,
    LLMConfigError,
    LLMResponseError,
    LLMTransportError,
    chat_completions_url,
    models_url,
    normalize_base_url,
    redact_secret,
)

SECRET = "sk-super-secret-value-1234"


class FakeResponse:
    """够用的假响应：只实现客户端真正用到的那几个成员。"""

    def __init__(
        self,
        *,
        status_code: int = 200,
        payload=None,
        body_text: str = "",
        headers: dict | None = None,
        lines: list[str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._body = body_text
        self.headers = dict(headers or {})
        self._lines = list(lines or [])
        self.closed = False

    @property
    def text(self) -> str:
        if self._body:
            return self._body
        return json.dumps(self._payload, ensure_ascii=False) if self._payload is not None else ""

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("No JSON")
        return self._payload

    def iter_lines(self, decode_unicode: bool = False):
        yield from self._lines

    def close(self) -> None:
        self.closed = True


def _patch(monkeypatch, handler):
    """把 requests.request 替换成受控实现，并记录调用。"""
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return handler(method, url, kwargs, len(calls))

    monkeypatch.setattr(llm.requests, "request", fake_request)
    return calls


def _client(**overrides) -> LLMClient:
    kwargs = {
        "base_url": "https://gateway.internal/v1",
        "api_key": SECRET,
        "model": "gpt-4o-mini",
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    return LLMClient(**kwargs)


# --------------------------------------------------------------------------
# URL 归一
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://gateway.internal/v1",
        "https://gateway.internal/v1/",
        "https://gateway.internal/v1/chat/completions",
        "https://gateway.internal/v1/models",
    ],
)
def test_base_url_forms_normalize_to_the_same_base(raw):
    """用户经常把**完整端点**粘进来。

    不归一就会拼出 `.../v1/chat/completions/models`，而它表现为「模型列表拿不到」，
    用户只会以为自己 token 配错了——排查成本极高。
    """
    assert normalize_base_url(raw) == "https://gateway.internal/v1"


def test_urls_are_built_from_the_normalized_base():
    assert chat_completions_url("https://h/v1/chat/completions") == "https://h/v1/chat/completions"
    assert models_url("https://h/v1/chat/completions") == "https://h/v1/models"


def test_base_url_without_a_path_is_supported():
    assert models_url("https://h") == "https://h/models"


@pytest.mark.parametrize("raw", ["", "   ", "ftp://h/v1", "gateway.internal/v1", "https://"])
def test_invalid_base_urls_are_rejected(raw):
    with pytest.raises(LLMConfigError):
        normalize_base_url(raw)


def test_credentials_embedded_in_the_url_are_stripped():
    """地址会进日志与错误信息，内嵌凭据不能跟着走。"""
    assert normalize_base_url("https://user:pw@h/v1") == "https://h/v1"


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------


def test_redact_secret_masks_the_explicit_key():
    assert SECRET not in redact_secret(f"upstream said {SECRET} is invalid", SECRET)
    assert "<redacted>" in redact_secret(f"upstream said {SECRET}", SECRET)


def test_redact_secret_masks_url_credentials_without_an_explicit_key():
    assert "pw" not in redact_secret("https://user:pw@h/v1/models")


# --------------------------------------------------------------------------
# 模型列表
# --------------------------------------------------------------------------


def _models_payload(*ids):
    return {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}


def test_list_models_parses_sorts_and_dedupes(monkeypatch):
    _patch(monkeypatch, lambda *_: FakeResponse(payload=_models_payload("b", "a", "b", "c")))
    assert _client().list_models() == ("a", "b", "c")


def test_list_models_caps_the_result(monkeypatch):
    """某个网关返回上千条时不能把页面和上下文一起撑坏。"""
    many = [f"model-{index:04d}" for index in range(MAX_MODELS + 50)]
    _patch(monkeypatch, lambda *_: FakeResponse(payload=_models_payload(*many)))
    assert len(_client().list_models()) == MAX_MODELS


def test_list_models_accepts_non_openai_shapes(monkeypatch):
    """有的网关把模型写成裸字符串，或者用 name 而不是 id。"""
    payload = {"data": ["plain-model", {"name": "named-model"}]}
    _patch(monkeypatch, lambda *_: FakeResponse(payload=payload))
    assert _client().list_models() == ("named-model", "plain-model")


@pytest.mark.parametrize(
    "payload",
    [{"models": []}, {"data": []}, {"data": "nope"}, ["not", "a", "dict"]],
)
def test_list_models_rejects_responses_it_cannot_use(monkeypatch, payload):
    """取不到列表要**明确报错**，让调用方能提示「请手动填写模型名」。

    不能返回空元组 —— 那会让前端显示一个空下拉框，用户不知道是端点不支持还是自己没配好。
    """
    _patch(monkeypatch, lambda *_: FakeResponse(payload=payload))
    with pytest.raises(LLMResponseError):
        _client().list_models()


def test_list_models_sends_the_key_as_a_bearer_header(monkeypatch):
    calls = _patch(monkeypatch, lambda *_: FakeResponse(payload=_models_payload("m")))
    _client().list_models()
    assert calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert calls[0]["url"] == "https://gateway.internal/v1/models"


def test_list_models_does_not_follow_redirects(monkeypatch):
    """密钥不能跟着 302 跑到别的主机上去。"""
    calls = _patch(monkeypatch, lambda *_: FakeResponse(payload=_models_payload("m")))
    _client().list_models()
    assert calls[0]["allow_redirects"] is False


# --------------------------------------------------------------------------
# 上下文窗口
#
# 只有一个用途：判断项目配的字符预算会不会**明显**超出模型窗口
# （`budget.clamp_to_model_window`）。所以这些用例守的是「宁可认不出来，也不要认错」。
# --------------------------------------------------------------------------


def _context_payload(*entries):
    return {"object": "list", "data": list(entries)}


@pytest.mark.parametrize(
    "key",
    ["context_length", "context_window", "max_context_length", "max_context_tokens",
     "max_model_len", "n_ctx"],
)
def test_the_declared_context_window_is_read_under_any_of_its_common_names(monkeypatch, key):
    """各家网关的字段名不一样（vLLM 用 `max_model_len`，有的用 `context_length`）。"""
    _patch(monkeypatch, lambda *_: FakeResponse(
        payload=_context_payload({"id": "m", key: 131072})
    ))
    assert _client().model_contexts() == {"m": 131072}


def test_a_numeric_string_window_is_accepted(monkeypatch):
    """同一个字段有的网关给数字、有的给字符串。"""
    _patch(monkeypatch, lambda *_: FakeResponse(
        payload=_context_payload({"id": "m", "context_length": "65536"})
    ))
    assert _client().model_contexts() == {"m": 65536}


def test_an_endpoint_that_declares_nothing_yields_nothing(monkeypatch):
    """**这是本功能最重要的性质**：端点没声明窗口时返回空 dict，调用方据此不压缩任何
    东西。返回一个猜出来的默认窗口会静默地砍掉分析质量。"""
    _patch(monkeypatch, lambda *_: FakeResponse(payload=_models_payload("m")))
    assert _client().model_contexts() == {}


@pytest.mark.parametrize(
    "value",
    [
        None, "abc", True, False, 0, -1, 512, 10 ** 9,
        # 最容易认错的一个：`max_tokens` 是「单次最多生成多少」，不是窗口。
        # 它落在合理区间外时会被区间挡掉；这条用例里的值本身就在区间外。
    ],
)
def test_implausible_windows_are_treated_as_undeclared(monkeypatch, value):
    """认不出来就当没有 —— **不猜**。把 `n_ctx: 0` 或一个荒谬的值当真，预算会被压到
    一个荒唐的大小，而用户只看到「这次分析特别浅」。"""
    _patch(monkeypatch, lambda *_: FakeResponse(
        payload=_context_payload({"id": "m", "context_length": value})
    ))
    assert _client().model_contexts() == {}


def test_the_declared_key_priority_decides_not_the_json_order(monkeypatch):
    """同一个模型声明了多个窗口字段时，按 `CONTEXT_WINDOW_KEYS` 的顺序取第一个认得的。

    与 JSON 里的键顺序无关 —— 否则同一个端点两次请求的解析结果会跟着上游的键顺序变。
    `context_length` 在优先级表里排在 `n_ctx` 前面，所以取前者。
    """
    assert CONTEXT_WINDOW_KEYS.index("context_length") < CONTEXT_WINDOW_KEYS.index("n_ctx")
    _patch(monkeypatch, lambda *_: FakeResponse(
        payload=_context_payload({"id": "m", "n_ctx": 8192, "context_length": 32768})
    ))
    assert _client().model_contexts() == {"m": 32768}


def test_models_without_a_window_are_simply_absent(monkeypatch):
    """一部分模型有、一部分没有时，只返回有的那些。"""
    _patch(monkeypatch, lambda *_: FakeResponse(
        payload=_context_payload(
            {"id": "with", "context_length": 200000},
            {"id": "without"},
            {"id": "with", "context_length": 999999},
        )
    ))
    contexts = _client().model_contexts()
    assert contexts == {"with": 200000}
    assert "without" not in contexts


def test_model_contexts_shares_the_models_response_parsing(monkeypatch):
    """两个接口读的是同一个 `/v1/models`，解析规则（含错误处理）不能各写一份。"""
    _patch(monkeypatch, lambda *_: FakeResponse(payload={"data": "nope"}))
    with pytest.raises(LLMResponseError):
        _client().model_contexts()


# --------------------------------------------------------------------------
# 重试边界
# --------------------------------------------------------------------------


def test_retries_a_retryable_status_then_succeeds(monkeypatch):
    slept: list[float] = []

    def handler(_method, _url, _kwargs, call_no):
        if call_no == 1:
            return FakeResponse(status_code=429, body_text="rate limited")
        return FakeResponse(payload=_models_payload("m"))

    calls = _patch(monkeypatch, handler)
    client = _client(sleep=slept.append)

    assert client.list_models() == ("m",)
    assert len(calls) == 2
    assert len(slept) == 1


def test_retry_after_header_wins_over_the_backoff_curve(monkeypatch):
    """上游明确说了多久之后再来，就按它说的来，比我们自己猜退避曲线准。"""
    slept: list[float] = []

    def handler(_method, _url, _kwargs, call_no):
        if call_no == 1:
            return FakeResponse(status_code=429, headers={"Retry-After": "7"}, body_text="slow down")
        return FakeResponse(payload=_models_payload("m"))

    _patch(monkeypatch, handler)
    _client(sleep=slept.append).list_models()
    assert slept == [7.0]


def test_retry_after_is_capped(monkeypatch):
    slept: list[float] = []

    def handler(_method, _url, _kwargs, call_no):
        if call_no == 1:
            return FakeResponse(status_code=429, headers={"Retry-After": "99999"}, body_text="x")
        return FakeResponse(payload=_models_payload("m"))

    _patch(monkeypatch, handler)
    _client(sleep=slept.append).list_models()
    assert slept and slept[0] <= llm.BACKOFF_MAX_SECONDS


def test_retry_budget_is_bounded(monkeypatch):
    """重试栈刻意做得很浅：只有传输层一层，且次数固定。

    要评审的那份外部工具叠了四层重试，单任务最坏 32 次调用且没有全局 deadline，
    一个坏任务就能拖垮队列。这条把「浅」固化下来。
    """
    slept: list[float] = []
    calls = _patch(
        monkeypatch,
        lambda *_: FakeResponse(status_code=503, headers={"Retry-After": "0"}, body_text="down"),
    )
    client = _client(sleep=slept.append)

    with pytest.raises(LLMTransportError):
        client.list_models()

    assert len(calls) == MAX_ATTEMPTS
    assert len(slept) == MAX_ATTEMPTS - 1


def test_auth_failure_is_not_retried(monkeypatch):
    """401/403 是配置问题，重试没有意义，而且会掩盖真实原因。

    必须让用户看到「密钥不对」，而不是「网络错误，请稍后重试」。
    """
    slept: list[float] = []
    calls = _patch(monkeypatch, lambda *_: FakeResponse(status_code=401, body_text="bad key"))
    client = _client(sleep=slept.append)

    with pytest.raises(LLMConfigError):
        client.list_models()

    assert len(calls) == 1, "配置类错误不该重试"
    assert slept == []


def test_redirect_is_reported_as_a_configuration_problem(monkeypatch):
    """3xx 必须被显式处理成失败。

    请求禁用了自动跟随，所以 3xx 会原样回来；若不显式判断，`status_code < 400`
    会把它当成成功，接着 `response.json()` 在登录页 HTML 上抛一个含义不明的解析
    错误 —— 用户看到「响应不是 OpenAI 形态」，而真实原因是端点把他重定向了。
    """
    _patch(
        monkeypatch,
        lambda *_: FakeResponse(status_code=302, headers={"Location": "https://elsewhere/login"}),
    )
    with pytest.raises(LLMConfigError) as excinfo:
        _client().list_models()
    assert "重定向" in str(excinfo.value)


def test_connection_errors_are_retried_then_reported(monkeypatch):
    calls = _patch(monkeypatch, lambda *_: (_ for _ in ()).throw(requests.exceptions.ConnectionError("boom")))
    with pytest.raises(LLMTransportError):
        _client().list_models()
    assert len(calls) == MAX_ATTEMPTS


# --------------------------------------------------------------------------
# 密钥绝不泄漏
# --------------------------------------------------------------------------


def test_key_never_appears_in_transport_error_messages(monkeypatch):
    """上游把 token 回显在错误体里是常见形态，错误信息又会进日志与数据库。"""
    _patch(
        monkeypatch,
        lambda *_: FakeResponse(
            status_code=400, body_text=f"invalid api key: {SECRET}"
        ),
    )
    with pytest.raises(LLMConfigError) as excinfo:
        _client().list_models()
    assert SECRET not in str(excinfo.value)
    assert "<redacted>" in str(excinfo.value)


def test_key_never_appears_in_exception_strings_from_the_transport(monkeypatch):
    """传输层异常自己也会带上整条请求信息（URL 里可能就有密钥）。"""

    def handler(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError(f"failed to reach https://u:{SECRET}@h/v1")

    _patch(monkeypatch, handler)
    with pytest.raises(LLMTransportError) as excinfo:
        _client().list_models()
    assert SECRET not in str(excinfo.value)


def test_key_never_appears_when_the_response_is_not_json(monkeypatch):
    _patch(monkeypatch, lambda *_: FakeResponse(status_code=200, body_text=f"<html>{SECRET}</html>"))
    with pytest.raises(LLMResponseError) as excinfo:
        _client().list_models()
    assert SECRET not in str(excinfo.value)


# --------------------------------------------------------------------------
# 补全
# --------------------------------------------------------------------------


def _completion_payload(content=None, *, text=None, usage=None, model="gpt-4o-mini"):
    choice: dict = {"finish_reason": "stop"}
    if content is not None:
        choice["message"] = {"role": "assistant", "content": content}
    if text is not None:
        choice["text"] = text
    return {"model": model, "choices": [choice], "usage": usage or {}}


def test_complete_extracts_plain_content(monkeypatch):
    _patch(monkeypatch, lambda *_: FakeResponse(payload=_completion_payload("你好")))
    result = _client().complete([{"role": "user", "content": "hi"}])
    assert result.text == "你好"
    assert result.model == "gpt-4o-mini"


def test_complete_extracts_list_form_content(monkeypatch):
    """有些网关把 content 拆成分片数组。"""
    payload = {
        "choices": [{"message": {"content": [{"type": "text", "text": "分段"}, {"type": "text", "text": "内容"}]}}]
    }
    _patch(monkeypatch, lambda *_: FakeResponse(payload=payload))
    assert _client().complete([{"role": "user", "content": "hi"}]).text == "分段内容"


def test_complete_extracts_legacy_text_field(monkeypatch):
    _patch(monkeypatch, lambda *_: FakeResponse(payload=_completion_payload(text="旧形态")))
    assert _client().complete([{"role": "user", "content": "hi"}]).text == "旧形态"


def test_complete_reads_token_usage(monkeypatch):
    payload = _completion_payload("x", usage={"prompt_tokens": 11, "completion_tokens": 7})
    _patch(monkeypatch, lambda *_: FakeResponse(payload=payload))
    result = _client().complete([{"role": "user", "content": "hi"}])
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (11, 7, 18)


def test_complete_rejects_a_response_with_no_usable_text(monkeypatch):
    """**不能返回空字符串**：空串会被上层当成「模型什么都没说」，从而去走协议纠错
    回路，把一个解析问题伪装成模型问题。"""
    _patch(monkeypatch, lambda *_: FakeResponse(payload={"choices": [{"finish_reason": "stop"}]}))
    with pytest.raises(LLMResponseError):
        _client().complete([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("payload", [{"choices": []}, {"nope": 1}, "just a string"])
def test_complete_rejects_malformed_payloads(monkeypatch, payload):
    _patch(monkeypatch, lambda *_: FakeResponse(payload=payload))
    with pytest.raises(LLMResponseError):
        _client().complete([{"role": "user", "content": "hi"}])


def test_complete_requires_a_model_name(monkeypatch):
    """缺模型名是配置问题，应该在发请求**之前**就报出来。"""
    calls = _patch(monkeypatch, lambda *_: FakeResponse(payload=_completion_payload("x")))
    with pytest.raises(LLMConfigError):
        _client(model="").complete([{"role": "user", "content": "hi"}])
    assert calls == []


def test_complete_requires_messages(monkeypatch):
    _patch(monkeypatch, lambda *_: FakeResponse(payload=_completion_payload("x")))
    with pytest.raises(LLMConfigError):
        _client().complete([])


# --------------------------------------------------------------------------
# 流式
# --------------------------------------------------------------------------


def _sse(*frames: str) -> list[str]:
    return list(frames)


def test_stream_concatenates_deltas(monkeypatch):
    lines = _sse(
        'data: {"choices":[{"delta":{"content":"你"}}]}',
        "",
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        "data: [DONE]",
    )
    _patch(monkeypatch, lambda *_: FakeResponse(lines=lines))
    assert "".join(_client().stream([{"role": "user", "content": "hi"}])) == "你好"


def test_stream_ignores_heartbeats_and_blank_lines(monkeypatch):
    """SSE 的注释帧（以 `:` 开头）是心跳，必须跳过而不是当成内容。"""
    lines = _sse(": keep-alive", "", "   ", 'data: {"choices":[{"delta":{"content":"A"}}]}', "data: [DONE]")
    _patch(monkeypatch, lambda *_: FakeResponse(lines=lines))
    assert "".join(_client().stream([{"role": "user", "content": "hi"}])) == "A"


def test_stream_tolerates_a_single_malformed_frame(monkeypatch):
    """单帧坏了不该中断整个流——后面的内容仍然有效。"""
    lines = _sse(
        'data: {"choices":[{"delta":{"content":"A"}}]}',
        "data: {这帧坏了",
        'data: {"choices":[{"delta":{"content":"B"}}]}',
        "data: [DONE]",
    )
    _patch(monkeypatch, lambda *_: FakeResponse(lines=lines))
    assert "".join(_client().stream([{"role": "user", "content": "hi"}])) == "AB"


def test_stream_skips_frames_without_delta_content(monkeypatch):
    lines = _sse(
        'data: {"choices":[{"delta":{}}]}',
        'data: {"choices":[]}',
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"content":"Z"}}]}',
        "data: [DONE]",
    )
    _patch(monkeypatch, lambda *_: FakeResponse(lines=lines))
    assert "".join(_client().stream([{"role": "user", "content": "hi"}])) == "Z"


def test_stream_sets_the_stream_flag_in_the_body(monkeypatch):
    calls = _patch(monkeypatch, lambda *_: FakeResponse(lines=_sse("data: [DONE]")))
    list(_client().stream([{"role": "user", "content": "hi"}]))
    assert calls[0]["json"]["stream"] is True
    assert calls[0]["stream"] is True


# --------------------------------------------------------------------------
# 超时
# --------------------------------------------------------------------------


def test_timeout_is_sent_on_every_request(monkeypatch):
    calls = _patch(monkeypatch, lambda *_: FakeResponse(payload=_models_payload("m")))
    _client().list_models()
    assert calls[0]["timeout"] == DEFAULT_TIMEOUT_SECONDS


def test_default_timeout_reads_the_env(monkeypatch):
    monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "42")
    assert llm.resolve_default_timeout_seconds() == 42

    monkeypatch.setenv("AI_REQUEST_TIMEOUT_SECONDS", "not-a-number")
    assert llm.resolve_default_timeout_seconds() == DEFAULT_TIMEOUT_SECONDS

    monkeypatch.delenv("AI_REQUEST_TIMEOUT_SECONDS", raising=False)
    assert llm.resolve_default_timeout_seconds() == DEFAULT_TIMEOUT_SECONDS
