# -*- coding: utf-8 -*-
"""WP17：**单次输出上限**（`max_tokens`）—— 默认封顶、请求体真的带、端点拒绝只回退一次。

## 为什么这一组要存在

实测 Run22/Run23 的输出 token 占费用 **71%~79%**，而请求体此前**不带** `max_tokens`
（`EngineLimits.max_output_tokens` 是 `None`）：一次调用最多能写多长由网关那个看不见的
默认值决定，成本不可控也不可解释。这一组钉三件事：

1. `EngineLimits` 的**默认值**是一个真实的上限（≥4096，低于它会把结论 JSON 截成半份），
   并且这个上限**真的进了请求体**（不是只在计划里写着）；
2. 计划（`thresholds["output_tokens"]`）与请求体**同一个数**：计划说 8k、端点按 64k
   收钱这种账没人对得出来；
3. 端点**明确拒绝**这个字段时（HTTP 400 且响应体点名 `max_tokens`）只回退**一次**并
   记进进程内黑名单 —— 封顶绝不能把一次分析弄挂，也不能每轮都先吃一个 400。

`None` 仍然是**显式**逃生口（不传这个字段、用端点默认值），所以「默认封顶」不能靠
`complete_kwargs` 里再塞一个兜底数来实现 —— 那会让「显式关掉」变成做不到的事。
"""
from __future__ import annotations

import json

import pytest

from services.ai import llm_client as llm
from services.ai.auto_sizing import DEFAULT_OUTPUT_TOKENS, plan_analysis
from services.ai.engine import EngineLimits
from services.ai.llm_client import (
    LLMClient,
    LLMConfigError,
    LLMValueLimitError,
    max_tokens_rejection_reason,
    reset_max_tokens_rejections,
)
from services.ai.model_call import complete_kwargs, output_limit_hit

MESSAGES = [{"role": "user", "content": "hi"}]
BASE_URL = "https://gateway.internal/v1"
_MODEL = "gpt-4o-mini"
#: 端点拒绝这个字段时最常见的那种错误体（各家措辞不同，但都会出现字段名）。
_REJECT_BODY = '{"error": {"message": "Unsupported parameter: max_tokens"}}'


class _Response:
    """够用的假响应：只实现客户端真正用到的那几个成员。"""

    def __init__(self, *, status_code: int = 200, payload=None, body: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self._body = body
        self.headers: dict = {}
        self.closed = False

    @property
    def text(self) -> str:
        if self._body:
            return self._body
        return json.dumps(self._payload or {}, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("No JSON")
        return self._payload

    def close(self) -> None:
        self.closed = True


def _patch(monkeypatch, handler):
    """把 `requests.request` 换成一个受控假实现，并记录调用（含请求体）。"""
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return handler(kwargs.get("json") or {}, len(calls))

    monkeypatch.setattr(llm.requests, "request", fake_request)
    return calls


def _client(**overrides) -> LLMClient:
    kwargs = {
        "base_url": BASE_URL,
        "api_key": "sk-test-value",
        "model": _MODEL,
        "sleep": lambda _seconds: None,
    }
    kwargs.update(overrides)
    return LLMClient(**kwargs)


def _completion_payload(text: str = "ok") -> dict:
    return {
        "model": _MODEL,
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }


@pytest.fixture(autouse=True)
def _clean_rejections():
    """每个用例从干净的黑名单开始（它是**进程级**状态，跨用例会互相影响）。"""
    reset_max_tokens_rejections()
    yield
    reset_max_tokens_rejections()


# --------------------------------------------------------------------------
# 一、默认值：真的非空、真的够大、真的进请求体
# --------------------------------------------------------------------------


def test_the_engine_caps_output_by_default():
    limits = EngineLimits()

    assert limits.max_output_tokens == DEFAULT_OUTPUT_TOKENS
    assert limits.max_output_tokens is not None
    assert DEFAULT_OUTPUT_TOKENS >= 4096, (
        "低于 4096 会把最终结论那份 JSON 截成半份 —— 比不封顶更糟"
    )


def test_the_default_cap_reaches_the_request_body(monkeypatch):
    calls = _patch(monkeypatch, lambda *_: _Response(payload=_completion_payload()))

    result = _client().complete(MESSAGES, **complete_kwargs(EngineLimits()))

    assert result.text == "ok"
    # **请求体**（不是 `EngineLimits` 的属性）里必须有这个字段，且与引擎默认值同源。
    assert calls[0]["json"]["max_tokens"] == DEFAULT_OUTPUT_TOKENS
    assert calls[0]["json"]["max_tokens"] == EngineLimits().max_output_tokens


def test_the_client_caps_output_when_the_caller_passes_nothing(monkeypatch):
    """**默认封顶的落点**：调用方不传这个参数时，客户端按平台默认值封顶。

    默认值放在客户端这一层，是为了让引擎默认轮不必多传一个与默认值相同的字段
    （否则每一个测试假 client 都要跟着改签名）；而**请求体里照样有** `max_tokens` ——
    那才是端点看见的东西。客户端用**哨兵**区分「没传」（→ 补默认值）与「显式
    `None`」（→ 不传字段），`complete_kwargs` 默认不带键是因为它会补齐，不是漏了。
    """
    calls = _patch(monkeypatch, lambda *_: _Response(payload=_completion_payload()))

    _client().complete(MESSAGES)

    assert calls[0]["json"]["max_tokens"] == DEFAULT_OUTPUT_TOKENS
    assert calls[0]["json"]["max_tokens"] == EngineLimits().max_output_tokens


def test_an_explicit_none_keeps_the_old_request_without_the_field(monkeypatch):
    """**显式** `None` = 不传这个字段（逃生口）；别把它与「默认封顶」混成一件事。"""
    kwargs = complete_kwargs(EngineLimits(max_output_tokens=None))
    assert kwargs.get("max_tokens", "missing") is None

    calls = _patch(monkeypatch, lambda *_: _Response(payload=_completion_payload()))
    _client().complete(MESSAGES, **kwargs)
    assert "max_tokens" not in calls[0]["json"]

    assert complete_kwargs(EngineLimits(max_output_tokens=4096))["max_tokens"] == 4096


def test_a_configured_value_reaches_the_request_body(monkeypatch):
    calls = _patch(monkeypatch, lambda *_: _Response(payload=_completion_payload()))

    _client().complete(MESSAGES, **complete_kwargs(EngineLimits(max_output_tokens=4096)))

    assert calls[0]["json"]["max_tokens"] == 4096


# --------------------------------------------------------------------------
# 二、计划与请求体是**同一个数**（计划上可查，端点按它收费）
# --------------------------------------------------------------------------


def _small_facts() -> dict:
    return {
        "file_count": 3,
        "diff_payload_chars": 3_000,
        "max_file_diff_chars": 1_000,
        "entries": [
            {"path": f"config/table_{index}.xlsx", "commit": f"c{index}", "chars": 1_000}
            for index in range(3)
        ],
    }


def test_the_plan_records_the_same_cap_as_the_request():
    plan = plan_analysis(_small_facts(), 100_000)

    assert plan.thresholds["output_tokens"] == DEFAULT_OUTPUT_TOKENS
    assert plan.estimate["output_tokens_per_member"] == DEFAULT_OUTPUT_TOKENS
    # 计划里的数、客户端封顶用的数、端点收到的数：**同一个常量**。
    # 两边各写一份的那天起，「计划说 8k、端点按 64k 收钱」这种账就没人对得出来了。
    from services.ai import llm_client as _llm

    assert _llm.DEFAULT_OUTPUT_TOKENS == plan.thresholds["output_tokens"]


def test_a_configured_cap_is_what_the_plan_records():
    plan = plan_analysis(
        _small_facts(), {"user_chars": 100_000, "output_tokens": 4_096}
    )

    assert plan.thresholds["output_tokens"] == 4_096
    assert plan.estimate["output_tokens_per_member"] == 4_096


# --------------------------------------------------------------------------
# 三、端点不支持时：**只回退一次**，然后记进黑名单
# --------------------------------------------------------------------------


def test_an_endpoint_that_rejects_max_tokens_is_retried_once_without_it(monkeypatch):
    def handler(body, _n):
        if "max_tokens" in body:
            return _Response(status_code=400, body=_REJECT_BODY)
        return _Response(payload=_completion_payload("回退之后照常"))

    calls = _patch(monkeypatch, handler)

    result = _client().complete(MESSAGES, max_tokens=DEFAULT_OUTPUT_TOKENS)

    assert result.text == "回退之后照常"
    assert len(calls) == 2, "只许回退一次：既不能直接失败，也不能反复重排"
    assert calls[0]["json"]["max_tokens"] == DEFAULT_OUTPUT_TOKENS
    assert "max_tokens" not in calls[1]["json"], "回退那一次仍然带着字段 = 没回退"
    assert max_tokens_rejection_reason(BASE_URL, _MODEL) is not None


def test_the_rejection_is_remembered_so_the_next_call_does_not_retry(monkeypatch):
    def handler(body, _n):
        if "max_tokens" in body:
            return _Response(status_code=400, body=_REJECT_BODY)
        return _Response(payload=_completion_payload())

    calls = _patch(monkeypatch, handler)
    client = _client()

    client.complete(MESSAGES, max_tokens=DEFAULT_OUTPUT_TOKENS)  # 2 次（含回退）
    calls_after_first = len(calls)
    client.complete(MESSAGES, max_tokens=DEFAULT_OUTPUT_TOKENS)  # 已知不支持 → 直接不带

    assert calls_after_first == 2
    assert len(calls) == 3, "第二次调用又先吃了一个 400 —— 黑名单没生效"
    assert "max_tokens" not in calls[-1]["json"]


def test_a_failed_retry_does_not_loop(monkeypatch):
    """回退那一次也失败时：**不许再回退**（否则每轮都重排，延迟翻倍）。"""
    calls = _patch(monkeypatch, lambda *_: _Response(status_code=400, body=_REJECT_BODY))

    with pytest.raises(LLMConfigError):
        _client().complete(MESSAGES, max_tokens=DEFAULT_OUTPUT_TOKENS)

    assert len(calls) == 2


def test_a_400_that_does_not_mention_max_tokens_is_not_retried(monkeypatch):
    """模型名写错 / messages 形状不对：换参数也不会变好，只发**一次**。"""
    calls = _patch(
        monkeypatch,
        lambda *_: _Response(status_code=400, body='{"error": {"message": "invalid model"}}'),
    )

    with pytest.raises(LLMConfigError):
        _client().complete(MESSAGES, max_tokens=DEFAULT_OUTPUT_TOKENS)

    assert len(calls) == 1
    assert max_tokens_rejection_reason(BASE_URL, _MODEL) is None, (
        "与 max_tokens 无关的 400 不该把端点拉黑"
    )


# --------------------------------------------------------------------------
# 三之二、400 说的是「值太大」而不是「不认字段」：**改小数值**，不撤掉上限
# --------------------------------------------------------------------------
#
# 审查报告 P2：此前任何点名 `max_tokens` 的 400 都被当成「不支持字段」，于是去掉字段
# 并把端点拉黑 —— 一次「max_tokens must be <= 4096」就让后续所有调用永久不再发送单次
# 输出上限。数值超限与字段不支持是两件事，处置必须分开。

_NUMERIC_LIMIT_BODY = '{"error": {"message": "max_tokens must be <= 4096"}}'


def test_a_numeric_limit_is_clamped_and_kept_for_later_calls(monkeypatch):
    """数值超限 → 第二次带 `min(请求值, limit)` 重试；不拉黑、后续调用照样带上限。"""

    def handler(body, _n):
        if body.get("max_tokens", 0) > 4096:
            return _Response(status_code=400, body=_NUMERIC_LIMIT_BODY)
        return _Response(payload=_completion_payload("降级之后照常"))

    calls = _patch(monkeypatch, handler)
    client = _client()

    result = client.complete(MESSAGES, max_tokens=8192)

    assert result.text == "降级之后照常"
    assert [call["json"].get("max_tokens") for call in calls] == [8192, 4096], (
        "第二次必须把数值**改小**到端点给的上限，而不是去掉字段"
    )
    assert max_tokens_rejection_reason(BASE_URL, _MODEL) is None, (
        "数值超限不是「不支持字段」；拉黑会把输出上限永久撤掉（P2）"
    )

    # 第三次调用仍然从调用方给的值开始（字段**在**），再被降到上限 —— 关键是它没有
    # 因为上一轮的黑名单而永久不带 `max_tokens`。
    calls.clear()
    client.complete(MESSAGES, max_tokens=8192)
    assert calls[0]["json"]["max_tokens"] == 8192, "上一轮的数值超限把上限永久撤掉了"
    assert calls[-1]["json"]["max_tokens"] == 4096


def test_a_limit_at_or_above_the_requested_value_is_not_retried(monkeypatch):
    """limit（16384）比请求值（8192）还大：改小后数值不变，别再发一次一模一样的请求。"""
    calls = _patch(
        monkeypatch,
        lambda *_: _Response(
            status_code=400,
            body='{"error": {"message": "max_tokens: maximum is 16384"}}',
        ),
    )

    with pytest.raises(LLMValueLimitError):
        _client().complete(MESSAGES, max_tokens=8192)

    assert len(calls) == 1, "max_tokens <= limit 时 min() 等于原值，重试只是白打一次"
    assert max_tokens_rejection_reason(BASE_URL, _MODEL) is None, (
        "解析出的上限比请求值大时不能误判成「不支持字段」"
    )


# --------------------------------------------------------------------------
# 三之三、WP30：`must not exceed` / 区间这类数值上限也必须走「改小数值」，不是撤字段
# --------------------------------------------------------------------------
#
# 审查报告 P2 的同一个洞：WP26 只认 `must be <=` / `at most` / `maximum is` 这几种
# 措辞，端点换成 `must not exceed 4096` / `valid range is [1, 4096]` 就解析不出上限，
# 于是落回「不认字段」分支 —— 去字段 + 拉黑。下面的用例把这两类措辞钉死。

_NOT_EXCEED_BODIES = [
    '{"error": {"message": "max_tokens must not exceed 4096"}}',
    '{"error": {"message": "valid range of max_tokens is [1, 4096]"}}',
]


@pytest.mark.parametrize("reject_body", _NOT_EXCEED_BODIES)
def test_a_not_exceed_or_range_limit_is_clamped_not_blacklisted(monkeypatch, reject_body):
    """`must not exceed 4096` / `[1, 4096]`：数值改小重试，黑名单必须是空的。"""

    def handler(body, _n):
        if body.get("max_tokens", 0) > 4096:
            return _Response(status_code=400, body=reject_body)
        return _Response(payload=_completion_payload("降级之后照常"))

    calls = _patch(monkeypatch, handler)

    result = _client().complete(MESSAGES, max_tokens=8192)

    assert result.text == "降级之后照常"
    assert [call["json"].get("max_tokens") for call in calls] == [8192, 4096], (
        "第二次必须把数值**改小**到 4096，而不是去掉字段"
    )
    assert max_tokens_rejection_reason(BASE_URL, _MODEL) is None, (
        "数值上限（含 not exceed / 区间形态）不是「不支持字段」；拉黑 = 永久撤掉封顶（P2）"
    )


@pytest.mark.parametrize(
    "reject_body",
    [
        '{"error": {"message": "Unknown parameter: max_tokens"}}',
        '{"error": {"message": "unsupported parameter max_tokens"}}',
    ],
)
def test_field_level_rejection_still_drops_the_field_and_blacklists(
    monkeypatch, reject_body
):
    """端点**明确说字段不认识**时，旧行为原样保留：去字段一次 + 拉黑。"""

    def handler(body, _n):
        if "max_tokens" in body:
            return _Response(status_code=400, body=reject_body)
        return _Response(payload=_completion_payload("去掉字段之后照常"))

    calls = _patch(monkeypatch, handler)

    result = _client().complete(MESSAGES, max_tokens=DEFAULT_OUTPUT_TOKENS)

    assert result.text == "去掉字段之后照常"
    assert len(calls) == 2
    assert "max_tokens" not in calls[1]["json"]
    assert max_tokens_rejection_reason(BASE_URL, _MODEL) is not None


def test_a_max_tokens_400_without_limit_or_field_word_is_not_retried(monkeypatch):
    """`max_tokens: invalid`：既没上限也没说字段不存在 → 原样失败，**不**去字段、**不**拉黑。

    这种 400 换参数也不会变好；去掉字段重试只会把端点的真实抱怨藏起来，还顺带永久
    撤掉单次输出上限 —— 那正是审查报告 P2 要求杜绝的方向。
    """
    calls = _patch(
        monkeypatch,
        lambda *_: _Response(
            status_code=400, body='{"error": {"message": "max_tokens: invalid"}}'
        ),
    )

    with pytest.raises(LLMConfigError) as excinfo:
        _client().complete(MESSAGES, max_tokens=8192)

    assert not isinstance(excinfo.value, LLMValueLimitError)
    assert len(calls) == 1, "去掉字段重试一次也不会变好，只是多打一次上游"
    assert calls[0]["json"]["max_tokens"] == 8192
    assert max_tokens_rejection_reason(BASE_URL, _MODEL) is None, (
        "没说「不认识这个字段」就不许拉黑"
    )


# --------------------------------------------------------------------------
# 四、封顶不改变「撞上上限」的识别方式（降级逻辑读的还是 finish_reason）
# --------------------------------------------------------------------------


def test_a_length_finish_reason_is_still_the_truncation_signal():
    assert output_limit_hit({"finish_reason": "length"}) is True
    assert output_limit_hit({"finish_reason": "stop"}) is False
    # 上游没报 finish_reason 时不能被当成「撞上限」（那条由括号配平兜底）。
    assert output_limit_hit({}) is False
