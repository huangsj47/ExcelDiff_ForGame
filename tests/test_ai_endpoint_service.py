"""连接配置：字段校验、来源判定、模型列表与连接探测。

这几件事的共同特点是**它们全都面向一个可能在任意状态的端点上**：地址可能填错、可能
不支持模型列表、Token 可能过期、模型名可能不存在。所以本文件守的是一组「出错时怎么办」
的性质：

* **越界要报错，不静默夹。** 原来那版是 `_clamp_int`：用户填 5000、界面回填 1000，
  中间没有任何提示 —— 用户以为存进去的是 5000。要改的是「告诉他范围」，不是悄悄改他的值。
* **「不支持模型列表」不是错误。** 实测能用的模型名可能压根不在 `/models` 里，所以取不到
  列表时必须降级成「请手动填写」，而不是把保存/使用流程卡住。
* **预设地址与自定义地址要能区分**，否则界面回显不出用户选的是哪一个。
"""

from __future__ import annotations

import pytest

from services.ai.endpoint_service import (
    FIELD_DEFAULTS,
    FIELD_RULES,
    OPENAI_BASE_URL,
    SOURCE_CUSTOM,
    SOURCE_OPENAI,
    ConfigValidationError,
    FieldError,
    build_probe_client,
    probe_connection,
    probe_models,
    source_of,
    validate_endpoint_ready,
    validate_field,
    validate_payload,
)
from services.ai.llm_client import (
    ChatResult,
    LLMConfigError,
    LLMResponseError,
    LLMTransportError,
)


class FakeClient:
    """可控的假客户端。`list_models` / `complete` 各自可以设成抛异常。"""

    def __init__(
        self,
        *,
        models=None,
        models_error: Exception | None = None,
        chat_text: str = "收到",
        chat_model: str = "test-model",
        chat_error: Exception | None = None,
    ):
        self._models = models
        self._models_error = models_error
        self._chat_text = chat_text
        self._chat_model = chat_model
        self._chat_error = chat_error
        self.model = chat_model
        self.calls: list[tuple] = []

    def list_models(self):
        self.calls.append(("list_models",))
        if self._models_error is not None:
            raise self._models_error
        return list(self._models or [])

    def complete(self, messages, temperature=None):
        self.calls.append(("complete", temperature))
        if self._chat_error is not None:
            raise self._chat_error
        return ChatResult(
            text=self._chat_text, model=self._chat_model, prompt_tokens=5, completion_tokens=2
        )


# ==========================================================================
# 字段校验
# ==========================================================================


def test_an_in_range_integer_passes():
    assert validate_field("max_analysis_rounds", "5") == 5
    assert validate_field("max_analysis_rounds", 5) == 5


def test_an_out_of_range_integer_is_rejected_not_clamped():
    """**这条是刻意的，也是与原实现的区别所在。**

    `_clamp_int` 会把 999 悄悄变成 30，用户看到「保存成功」却不知道自己填的值被改了。
    """
    with pytest.raises(FieldError) as excinfo:
        validate_field("max_analysis_rounds", "999")

    assert "30" in str(excinfo.value)
    assert "999" in str(excinfo.value), "要把用户填的值也说出来"


def test_a_below_minimum_integer_is_rejected():
    with pytest.raises(FieldError):
        validate_field("max_tool_requests", "-1")


def test_a_non_integer_is_rejected_with_the_offending_text():
    with pytest.raises(FieldError) as excinfo:
        validate_field("max_files_per_run", "abc")
    assert "abc" in str(excinfo.value)


def test_an_empty_integer_is_rejected():
    for raw in ("", None, "   "):
        with pytest.raises(FieldError):
            validate_field("max_files_per_run", raw)


def test_a_boolean_field_accepts_the_usual_spellings():
    for raw in (True, "true", "1", "on", "是", "启用"):
        assert validate_field("auto_weekly_enabled", raw) is True
    for raw in (False, "false", "0", "off", "否"):
        assert validate_field("auto_weekly_enabled", raw) is False


def test_a_choice_field_is_case_insensitive_on_the_way_in():
    assert validate_field("min_severity", " CRITICAL ") == "critical"


def test_an_unknown_choice_is_rejected_and_lists_the_options():
    with pytest.raises(FieldError) as excinfo:
        validate_field("min_severity", "medium")

    message = str(excinfo.value)
    assert "high" in message and "critical" in message


def test_the_error_message_uses_the_chinese_label():
    """报错里出现 `max_analysis_rounds` 这种列名，用户不知道指的是界面上哪一栏。"""
    with pytest.raises(FieldError) as excinfo:
        validate_field("max_analysis_rounds", "999")
    assert excinfo.value.label == "最大分析轮次"
    assert "max_analysis_rounds" not in str(excinfo.value)


def test_an_unknown_field_is_rejected():
    with pytest.raises(FieldError):
        validate_field("not_a_field", "x")


# ==========================================================================
# 地址归一化
# ==========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1/", "https://api.openai.com/v1"),
        ("http://127.0.0.1:15721/v1/", "http://127.0.0.1:15721/v1"),
        # 用户常把完整路径一起粘进来，归一化后仍要能用。
        ("https://host/v1/chat/completions", "https://host/v1"),
        ("https://host/v1/models", "https://host/v1"),
        ("  https://host/v1  ", "https://host/v1"),
    ],
)
def test_base_url_is_normalized(raw, expected):
    assert validate_field("api_base_url", raw) == expected


def test_an_empty_base_url_is_allowed_at_save_time():
    """允许先存别的配置、稍后再填地址（分步配置是常态）；缺什么在「跑分析前」再拦。"""
    assert validate_field("api_base_url", "") == ""


@pytest.mark.parametrize("raw", ["ftp://host/v1", "host/v1", "file:///etc/passwd"])
def test_a_non_http_url_is_rejected(raw):
    with pytest.raises(FieldError):
        validate_field("api_base_url", raw)


# ==========================================================================
# 整体提交体
# ==========================================================================


def test_only_the_submitted_fields_are_returned():
    """界面可能只改一栏。要求它把全部字段带上，在并发编辑时还会互相覆盖。"""
    normalized = validate_payload({"api_model": "deepseek-v4-flash"})
    assert normalized == {"api_model": "deepseek-v4-flash"}


def test_unknown_keys_are_ignored_rather_than_rejected():
    """前端可能带上别的 state（比如 csrf、tab 名），不该因此整单失败。"""
    normalized = validate_payload({"api_model": "m", "some_ui_state": "x"})
    assert normalized == {"api_model": "m"}


def test_all_errors_are_collected_in_one_pass():
    """一次把所有问题标红，用户改一轮就好；逐个报会让他改五遍。"""
    with pytest.raises(ConfigValidationError) as excinfo:
        validate_payload(
            {
                "max_analysis_rounds": "999",
                "min_severity": "medium",
                "api_base_url": "ftp://x",
                "api_model": "x" * 300,
            }
        )

    fields = {error.field for error in excinfo.value.errors}
    assert fields == {"max_analysis_rounds", "min_severity", "api_base_url", "api_model"}


def test_a_valid_payload_produces_no_errors():
    """反向自检：合格输入不能被误判。"""
    normalized = validate_payload(
        {
            "api_base_url": "http://127.0.0.1:15721/v1",
            "api_model": "deepseek-v4-flash",
            "max_analysis_rounds": "4",
            "min_severity": "critical",
            "auto_weekly_enabled": False,
            "prompt_template": "",
        }
    )
    assert normalized["max_analysis_rounds"] == 4
    assert normalized["auto_weekly_enabled"] is False


def test_an_empty_payload_is_valid():
    assert validate_payload({}) == {}
    assert validate_payload(None) == {}


# ==========================================================================
# 来源判定
# ==========================================================================


def test_the_openai_preset_is_recognised():
    assert source_of(OPENAI_BASE_URL) == SOURCE_OPENAI
    assert source_of(OPENAI_BASE_URL + "/") == SOURCE_OPENAI


def test_a_custom_endpoint_is_recognised():
    assert source_of("http://127.0.0.1:15721/v1") == SOURCE_CUSTOM


def test_an_empty_url_reads_as_the_preset():
    """还没配过的时候界面选中「预设」比选中「自定义」（配着空白字段）更合理。"""
    assert source_of("") == SOURCE_OPENAI


# ==========================================================================
# 跑分析之前的必要条件
# ==========================================================================


def test_missing_endpoint_pieces_are_reported_one_by_one():
    errors = validate_endpoint_ready({"api_base_url": "", "api_model": ""}, has_key=False)
    fields = {error.field for error in errors}
    assert fields == {"api_base_url", "api_model", "api_key"}


def test_a_ready_configuration_reports_nothing():
    errors = validate_endpoint_ready(
        {"api_base_url": "https://host/v1", "api_model": "m"}, has_key=True
    )
    assert errors == []


# ==========================================================================
# 模型列表探测
# ==========================================================================


def test_a_model_list_is_returned_sorted_and_deduplicated():
    result = probe_models(FakeClient(models=["b", "a", "b", " c "]))

    assert result.ok
    assert result.supported
    assert result.models == ("a", "b", "c")
    assert "3" in result.message


@pytest.mark.parametrize(
    "error",
    [
        LLMConfigError("端点返回 404"),
        LLMTransportError("连接超时"),
        LLMResponseError("响应不是 OpenAI 形态"),
    ],
)
def test_an_unavailable_list_degrades_instead_of_failing(error):
    """**取不到列表不是错误，不能阻断任何操作。**

    实测存在「能正常对话但不在列表里」的模型，所以模型名本来就允许手填；这时候把
    流程卡住只会让用户没法配置。
    """
    result = probe_models(FakeClient(models_error=error))

    assert not result.ok
    assert not result.supported
    assert "手动填写" in result.message
    assert result.models == ()
    assert result.detail, "要把真实原因留在 detail 里，便于排查"


def test_an_empty_model_list_also_degrades():
    result = probe_models(FakeClient(models=[]))
    assert not result.ok
    assert "手动填写" in result.message


def test_the_model_list_is_capped():
    result = probe_models(FakeClient(models=[f"m{n}" for n in range(600)]), limit=10)
    assert len(result.models) == 10


def test_probe_models_never_raises_on_a_failing_client():
    """无论客户端怎么炸，这个函数都必须返回结果而不是抛异常。"""
    class Exploding:
        model = "m"

        def list_models(self):
            raise LLMResponseError("boom")

    assert probe_models(Exploding()).ok is False


# ==========================================================================
# 连接探测
# ==========================================================================


def test_a_working_endpoint_reports_success_and_latency():
    result = probe_connection(FakeClient(chat_text="收到", chat_model="deepseek-v4-flash"))

    assert result.ok
    assert "deepseek-v4-flash" in result.message
    assert result.latency_ms >= 0


def test_a_failing_connection_reports_the_reason():
    result = probe_connection(FakeClient(chat_error=LLMTransportError("连接被拒绝")))

    assert not result.ok
    assert "连接被拒绝" in result.message
    assert result.detail == "LLMTransportError"


def test_an_empty_response_is_treated_as_a_failure_with_a_hint():
    """端点通了但内容是空的 —— 最常见的原因是模型名不被支持。

    这种情况报「成功」会让用户带着一个错的模型名去跑几分钟的分析，然后失败。
    """
    result = probe_connection(FakeClient(chat_text="   "))

    assert not result.ok
    assert "空" in result.message
    assert "模型名" in result.message


def test_the_probe_prompt_is_cheap_and_deterministic():
    """探测要尽量便宜、尽量确定：温度 0，问题本身不需要推理。"""
    client = FakeClient()
    probe_connection(client)

    assert client.calls == [("complete", 0)]


def test_probe_client_is_built_from_the_given_pieces():
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    build_probe_client(
        base_url="http://127.0.0.1:15721/v1", api_key="k", model="m", factory=factory
    )

    assert captured["api_key"] == "k"
    assert captured["model"] == "m"
    assert captured["timeout_seconds"] > 0


def test_probe_connection_never_raises_on_a_failing_client():
    class Exploding:
        model = "m"

        def complete(self, messages, temperature=None):
            raise LLMResponseError("boom")

    assert probe_connection(Exploding()).ok is False


# ==========================================================================
# 规则表与默认值
# ==========================================================================


def test_every_rule_has_a_chinese_label():
    for name, rule in FIELD_RULES.items():
        assert rule.label and not rule.label.isascii(), f"{name} 的标签不是中文：{rule.label}"


def test_every_default_is_accepted_by_its_own_rule():
    """默认值自己必须能通过校验。否则「留空保存」会失败，而用户什么都没做错。"""
    for name, value in FIELD_DEFAULTS.items():
        assert name in FIELD_RULES, f"{name} 有默认值但没有规则"
        validate_field(name, value)


def test_field_defaults_agree_with_the_model_layer():
    """**防漂移**：界面显示的默认值与模型层读取时补的默认值必须一致。

    两处不一致时界面会说「默认 8 轮」而实际生效的是别的值，谁都不会发现。
    """
    from models.ai_analysis.project_config import AiProjectAnalysisConfig

    resolved = AiProjectAnalysisConfig(project_id=1).resolved()
    for name, value in FIELD_DEFAULTS.items():
        assert resolved[name] == value, f"{name} 的默认值不一致：界面 {value} / 模型 {resolved[name]}"
