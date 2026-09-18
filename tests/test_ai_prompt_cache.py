"""提示词缓存标记：能力是声明出来的，标记绝不能拖垮一次分析。

这个文件守两条性质，两条都是「出错的代价远大于收益」那一类：

1. **默认不发。** `auto` + `none`（出厂默认）下，请求体必须与功能上线前**逐字节相同** ——
   `cache_control` 不是 OpenAI 协议的一部分，一个私有网关可能完全不认它，而猜错
   （按主机名/模型名猜「像不像 Anthropic」）的代价是一次 400。
2. **带了标记失败不能拖垮分析。** 网关拒了未知字段时，去掉标记重试一次；重试成功就
   照常出结论，并把该端点记进进程内的黑名单。这一条是本文件最重要的一条 ——
   用「省钱」换掉「这次分析跑不出来」，怎么算都是亏的。

消息上的断点标记（`_cache_breakpoint`）是**内部**的：它在任何情况下都不许出现在请求体里。
它不是 Anthropic 的字段，漏出去就是一个未知字段，而未知字段正是会被 400 掉的东西。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.ai import prompt_cache
from services.ai.llm_client import LLMClient, LLMConfigError, LLMTransportError
from services.ai.prompt_cache import (
    CACHE_BREAKPOINT_KEY,
    CACHE_FORMAT_ANTHROPIC,
    CACHE_FORMAT_NONE,
    CACHE_MODE_AUTO,
    CACHE_MODE_EXPLICIT,
    CACHE_MODE_OFF,
    MAX_CACHE_BREAKPOINTS,
    apply_cache_breakpoints,
    mark_cache_breakpoint,
    resolve_cache_marker,
    strip_cache_breakpoints,
)

SECRET = "sk-super-secret-value-1234"


@pytest.fixture(autouse=True)
def _clean_rejections():
    """黑名单是**进程级**状态（见 `prompt_cache` 模块注释），用例之间必须互不影响。"""
    prompt_cache.reset_cache_marker_rejections()
    yield
    prompt_cache.reset_cache_marker_rejections()


# --------------------------------------------------------------------------
# 假件：受控的 HTTP 层（与 test_ai_llm_client 同一套打法，不打真网络）
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, body_text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self._body = body_text
        self.headers = dict(headers or {})
        self.closed = False

    @property
    def text(self) -> str:
        if self._body:
            return self._body
        return json.dumps(self._payload, ensure_ascii=False) if self._payload is not None else ""

    def content(self) -> bytes:  # pragma: no cover - 本文件用不到
        return self.text.encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("No JSON")
        return self._payload

    def close(self) -> None:
        self.closed = True


def _ok_payload(text: str = "收到") -> dict:
    return {
        "model": "fake-model",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5},
    }


def _patch(monkeypatch, handler):
    """替换 `requests.request` 并记录每次请求体。"""
    bodies: list[dict] = []

    def fake_request(method, url, **kwargs):
        bodies.append(kwargs.get("json"))
        return handler(method, url, kwargs, len(bodies))

    monkeypatch.setattr("services.ai.llm_client.requests.request", fake_request)
    return bodies


def _client(**overrides) -> LLMClient:
    kwargs = {
        "base_url": "https://gateway.internal/v1",
        "api_key": SECRET,
        "model": "gateway-model",
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    return LLMClient(**kwargs)


def _declared_client(**overrides) -> LLMClient:
    """一个**声明过**接受缓存标记的客户端（`explicit` = 用户明确声称端点接受）。"""
    return _client(prompt_cache_mode=CACHE_MODE_EXPLICIT, **overrides)


def _messages() -> list[dict]:
    """一份带断点的消息：system 与第一条 user（与引擎挂的位置同形）。"""
    return [
        mark_cache_breakpoint({"role": "system", "content": "你是配表评审专家。" * 20}),
        mark_cache_breakpoint({"role": "user", "content": "第 1 轮：先要 diff。" * 10}),
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "第 2 轮：继续。" * 10},
    ]


def _has_cache_control(body: dict) -> bool:
    return "cache_control" in json.dumps(body, ensure_ascii=False)


# --------------------------------------------------------------------------
# 模式 × 形态 → 发不发
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,cache_format,expected",
    [
        # 默认：什么都不声明 → 不发。这是最重要的一格。
        (CACHE_MODE_AUTO, CACHE_FORMAT_NONE, False),
        # 端点声明了约定 → 发。
        (CACHE_MODE_AUTO, CACHE_FORMAT_ANTHROPIC, True),
        # 用户明确声称端点接受 → 发（哪怕没声明形态）。
        (CACHE_MODE_EXPLICIT, CACHE_FORMAT_NONE, True),
        (CACHE_MODE_EXPLICIT, CACHE_FORMAT_ANTHROPIC, True),
        # 明确关掉 → 永不发，哪怕声明了约定。
        (CACHE_MODE_OFF, CACHE_FORMAT_ANTHROPIC, False),
        (CACHE_MODE_OFF, CACHE_FORMAT_NONE, False),
        # 垃圾值 → 走默认（auto + none → 不发），**不落回 off**：那是另一个意思。
        ("", "", False),
        ("ON", "Anthropic-ish", False),
    ],
)
def test_the_switch_combination_decides_whether_markers_are_sent(mode, cache_format, expected):
    marker = resolve_cache_marker(mode, cache_format)
    assert (marker is not None) is expected


def test_the_marker_is_the_one_convention_we_implement():
    """只有一种约定：`{"type": "ephemeral"}`。写错一个字母就是一个未知字段。"""
    assert resolve_cache_marker(CACHE_MODE_EXPLICIT, CACHE_FORMAT_NONE) == {"type": "ephemeral"}


def test_nothing_about_the_endpoint_is_guessed():
    """**端点「像不像 Anthropic」不参与任何决策。**

    这条用例守的是一个**已经删掉**的东西：按主机名/模型名猜端点风格的那个启发式。
    它看起来是个好主意，但对内网网关天然失效（私有域名既可能是 Anthropic 兼容层，
    也可能是完全不认这个字段的转发器），而猜错的代价是一次 400 —— 而且是**悄悄**发生的：
    用户只会看到分析失败，不会知道是我们猜的。

    所以 `prompt_cache` 里不许再出现按 URL/模型名判断的逻辑，只留「声明」这一条路。

    断言前先剥掉注释与文档字符串（本模块的注释里就会引用 `claude` / `anthropic`
    这几个词来讲这件事），剥法见 `test_ai_prompt._code_without_comments_or_docstrings`。
    """
    from tests.test_ai_prompt import _code_without_comments_or_docstrings

    code = _code_without_comments_or_docstrings(
        Path(prompt_cache.__file__).read_text(encoding="utf-8")
    )
    for forbidden in ("claude", "host", "urlparse", "netloc", "http://", "https://"):
        assert forbidden not in code, (
            f"`prompt_cache.py` 的代码里出现了 `{forbidden}`：端点能力只能由配置声明，"
            "不许从地址或模型名推断"
        )


# --------------------------------------------------------------------------
# 消息上的断点标记
# --------------------------------------------------------------------------


def test_strip_removes_the_internal_marker_and_nothing_else():
    messages = _messages()
    clean = strip_cache_breakpoints(messages)

    assert all(CACHE_BREAKPOINT_KEY not in item for item in clean)
    assert clean == [{k: v for k, v in item.items() if k != CACHE_BREAKPOINT_KEY} for item in messages]


def test_apply_converts_only_the_marked_messages_into_content_blocks():
    messages = _messages()
    body_messages = apply_cache_breakpoints(messages, {"type": "ephemeral"})

    assert isinstance(body_messages[0]["content"], list)
    assert body_messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert body_messages[0]["content"][0]["text"] == messages[0]["content"]
    # 没挂断点的消息一个字都不改（还是字符串）。
    assert body_messages[2] == messages[2]
    assert isinstance(body_messages[3]["content"], str)


def test_apply_never_emits_more_breakpoints_than_upstream_allows():
    """Anthropic 的硬上限是 4，超了直接 400 —— 而 400 会把整个功能关掉（黑名单）。

    所以编排层只挂 3 个，这里再兜一道：多出来的标记一律不发。
    """
    messages = [mark_cache_breakpoint({"role": "user", "content": f"第 {i} 轮"}) for i in range(10)]
    body_messages = apply_cache_breakpoints(messages, {"type": "ephemeral"})

    marked = [item for item in body_messages if isinstance(item["content"], list)]
    assert len(marked) == MAX_CACHE_BREAKPOINTS


def test_apply_with_no_marker_is_exactly_a_strip():
    messages = _messages()
    assert apply_cache_breakpoints(messages, None) == strip_cache_breakpoints(messages)


# --------------------------------------------------------------------------
# 请求体：默认路径必须与功能上线前逐字节相同
# --------------------------------------------------------------------------


def test_the_default_configuration_sends_the_body_unchanged(monkeypatch):
    """**默认不发标记。** 出厂配置（auto + none）下请求体必须与以前一模一样。

    这条不只是「没多出字段」：连消息上的内部标记都不许漏出去（它是一个未知字段，
    而未知字段正是会被网关 400 掉的东西）。
    """
    bodies = _patch(monkeypatch, lambda *args: FakeResponse(payload=_ok_payload()))
    client = _client()  # 出厂默认：auto + none

    client.complete(_messages())

    body = bodies[0]
    assert body["messages"] == [
        {"role": "system", "content": "你是配表评审专家。" * 20},
        {"role": "user", "content": "第 1 轮：先要 diff。" * 10},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "第 2 轮：继续。" * 10},
    ]
    assert "cache_control" not in json.dumps(body, ensure_ascii=False)
    assert client.prompt_cache_mode == CACHE_MODE_AUTO


def test_a_declared_endpoint_gets_the_marker_on_the_marked_messages(monkeypatch):
    bodies = _patch(monkeypatch, lambda *args: FakeResponse(payload=_ok_payload()))
    client = _declared_client()

    result = client.complete(_messages())

    assert result.text == "收到"
    body = bodies[0]
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # 没挂断点的消息保持字符串形态。
    assert body["messages"][3]["content"] == "第 2 轮：继续。" * 10


def test_the_mode_and_format_are_normalized_once_at_construction():
    client = _client(prompt_cache_mode="EXPLICIT", prompt_cache_format="  Anthropic ")
    assert client.prompt_cache_mode == CACHE_MODE_EXPLICIT
    assert client.prompt_cache_format == CACHE_FORMAT_ANTHROPIC


def test_a_streaming_request_never_leaks_the_internal_marker(monkeypatch):
    """流式那条路径不挂标记，但消息仍然要过一遍断点处理（否则内部键会漏进 JSON）。"""
    bodies = _patch(
        monkeypatch,
        lambda *args: FakeResponse(payload=_ok_payload(), body_text="data: [DONE]\n"),
    )
    client = _declared_client()

    # iter_lines 在 FakeResponse 上没有实现，这里只需要它走到发请求那一步。
    with pytest.raises(AttributeError):
        list(client.stream(_messages()))

    assert CACHE_BREAKPOINT_KEY not in json.dumps(bodies[0], ensure_ascii=False)
    assert "cache_control" not in json.dumps(bodies[0], ensure_ascii=False)


# --------------------------------------------------------------------------
# 安全阀：网关拒了缓存标记 → 去掉标记重试，分析照常
# --------------------------------------------------------------------------


def test_a_gateway_that_rejects_the_marker_is_retried_without_it(monkeypatch):
    """**本文件最要紧的一条。**

    未知字段被 4xx 拒绝是最常见的失败形态，而它的后果是**整个分析跑不起来**。
    用一个省钱的优化换掉一次分析，这笔账怎么算都是亏的 —— 所以必须自动退回。
    """
    logged: list[str] = []

    def handler(method, url, kwargs, index):
        if index == 1:
            return FakeResponse(
                status_code=400,
                body_text='{"error":{"message":"unknown field: cache_control"}}',
            )
        return FakeResponse(payload=_ok_payload())

    bodies = _patch(monkeypatch, handler)
    monkeypatch.setattr(
        "services.ai.llm_client.log_print", lambda message, *a, **k: logged.append(str(message))
    )
    client = _declared_client()

    result = client.complete(_messages())

    assert result.text == "收到", "去掉标记重试成功之后，这次分析要照常完成"
    assert len(bodies) == 2, "只该重试一次"
    assert _has_cache_control(bodies[0]), "第一次必须真的带了标记（否则这条用例测了个空"
    assert not _has_cache_control(bodies[1]), "重试必须真的不带标记"
    # 内部标记不许漏进任何一次请求体。
    assert all(CACHE_BREAKPOINT_KEY not in json.dumps(body, ensure_ascii=False) for body in bodies)
    # 记下来了，并且记的是**端点**（不是这一次调用）。
    reason = prompt_cache.cache_marker_rejection_reason(client.base_url, client.model)
    assert reason and "unknown field" in reason
    # 而且说清楚了为什么。
    assert logged and "缓存标记" in logged[0]


def test_a_rejected_endpoint_stops_getting_markers_in_the_same_process(monkeypatch):
    """记住之后，**后续请求一开始就不带标记** —— 不必每次都先撞一次 400。

    这一条省下的是「每次分析都白跑一个请求」，而且那一次失败会在日志里留下一行
    看起来像故障的 400。
    """
    def handler(method, url, kwargs, index):
        if index == 1:
            return FakeResponse(status_code=422, body_text="unprocessable: cache_control")
        return FakeResponse(payload=_ok_payload())

    bodies = _patch(monkeypatch, handler)
    monkeypatch.setattr("services.ai.llm_client.log_print", lambda *a, **k: None)
    client = _declared_client()

    client.complete(_messages())
    assert len(bodies) == 2

    # 不清空列表：`index` 是按**打过的请求总数**算的（清空会让它从头开始，
    # 于是第二次调用又拿到「第一次」那个 422）。
    mark = len(bodies)
    client.complete(_messages())

    assert len(bodies) - mark == 1, "第二次请求不该再撞一次 400"
    assert not _has_cache_control(bodies[-1])
    assert CACHE_MODE_EXPLICIT == client.prompt_cache_mode, "配置没有被改掉，只是这个端点被记住了"


def test_a_second_client_on_the_same_endpoint_also_skips_the_marker(monkeypatch):
    """黑名单是**进程级**的：一次分析创建一个客户端，记在实例上等于没记。"""
    _patch(monkeypatch, lambda *args: FakeResponse(payload=_ok_payload()))
    monkeypatch.setattr("services.ai.llm_client.log_print", lambda *a, **k: None)
    first = _declared_client()
    prompt_cache.mark_cache_marker_rejected(first.base_url, first.model, "400")

    bodies = _patch(monkeypatch, lambda *args: FakeResponse(payload=_ok_payload()))
    second = _declared_client()
    second.complete(_messages())

    assert not _has_cache_control(bodies[0])


def test_a_failure_that_is_not_about_the_marker_does_not_blacklist_the_endpoint(monkeypatch):
    """**反向守卫**：去掉标记**仍然**失败说明与标记无关（密钥、网络、网关故障）。

    这时候把端点拉黑，等于把一个其实没问题的功能永久关掉 —— 而且没有任何人会发现，
    因为「不发标记」与「端点不支持」在日志里长得一样。
    """
    bodies = _patch(
        monkeypatch,
        lambda *args: FakeResponse(status_code=401, body_text='{"error":"bad key"}'),
    )
    monkeypatch.setattr("services.ai.llm_client.log_print", lambda *a, **k: None)
    client = _declared_client()

    with pytest.raises(LLMConfigError):
        client.complete(_messages())

    assert len(bodies) == 2, "带标记失败 → 去掉标记再试一次"
    assert prompt_cache.cache_marker_rejections() == {}, "与标记无关的失败不许拉黑端点"
    # 而且下一次请求**仍然带标记**（功能没被关掉）。
    with pytest.raises(LLMConfigError):
        client.complete(_messages())
    assert _has_cache_control(bodies[2])


def test_the_safety_valve_does_not_double_the_retry_ladder_on_transport_errors(monkeypatch):
    """去掉标记那一次如果还是传输层故障，传播的是**那一次**的错误。

    这里只断言「没有把端点拉黑」与「异常类型没变」：`_request` 自己那层重试（3 次）
    与这一层（1 次）是两件事，加起来最坏 4 次，仍然是可接受的量级。
    """
    _patch(
        monkeypatch,
        lambda *args: FakeResponse(status_code=503, body_text="upstream busy"),
    )
    monkeypatch.setattr("services.ai.llm_client.log_print", lambda *a, **k: None)
    client = _declared_client()

    with pytest.raises(LLMTransportError):
        client.complete(_messages())

    assert prompt_cache.cache_marker_rejections() == {}


def test_a_client_without_the_feature_never_retries(monkeypatch):
    """没开标记时不许出现「多打一次请求」：默认路径的行为必须与以前完全一致。"""
    bodies = _patch(
        monkeypatch,
        lambda *args: FakeResponse(status_code=400, body_text="bad request"),
    )
    client = _client()  # auto + none → 不带标记

    with pytest.raises(LLMConfigError):
        client.complete(_messages())

    assert len(bodies) == 1


def test_the_rejection_reason_is_redacted(monkeypatch):
    """黑名单里的原因会进日志，所以它也要过一遍脱敏（与其它错误信息同一条纪律）。"""
    def handler(method, url, kwargs, index):
        if index == 1:
            return FakeResponse(status_code=400, body_text=f"bad request: token={SECRET}")
        return FakeResponse(payload=_ok_payload())

    _patch(monkeypatch, handler)
    logged: list[str] = []
    monkeypatch.setattr(
        "services.ai.llm_client.log_print", lambda message, *a, **k: logged.append(str(message))
    )
    client = _declared_client()

    client.complete(_messages())

    reason = prompt_cache.cache_marker_rejection_reason(client.base_url, client.model) or ""
    assert reason, "这条用例要验的是「原因被脱敏」，不是「没记原因」"
    assert SECRET not in reason
    assert SECRET not in " ".join(logged)
