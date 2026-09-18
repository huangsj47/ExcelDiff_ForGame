# -*- coding: utf-8 -*-
"""提示词预算 vs 模型上下文窗口。

## 这一组守的是什么

`prompt_char_budget` 是按**字符**配的，而模型窗口是按 **token** 计的，两者的换算比例
取决于提示词里中文占多少 —— **平台无从知道**。于是这里的规则是：

1. 窗口**尽量向端点问**（`/v1/models` 里有时会声明）。问不到就按 `DEFAULT_CONTEXT_TOKENS`
   （1M）这个**口径值**处理，并且**必须写明是按默认值来的** —— 「按 1M 压的」与
   「端点声明了 1M」是两件事，界面不能让人分不清。
2. 生效预算 = min(配置预算, 窗口 × 60%)。字符按 1:1 当 token 算（`CHARS_PER_TOKEN`），
   这个方向是**保守**的，所以压出来的提示词在 token 意义上一定装得下。
3. **不维护「模型名 → 窗口」对照表**：那是猜出来的数字，模型一迭代就过期。

## 这条规则改过一次，原来是什么样

原来是 `clamp_to_model_window`：只在「预算字符数 > 窗口 token 数」时才压，其余一律不动。
顾虑（没有依据就不该砍分析质量）是对的，但它留下一个更糟的口子 —— **窗口问不到时它
什么都不做**。一个把预算配到 2,000,000 字的项目会拿着一份 2M 字符的提示词去撞模型，
必然被拒，而拒绝的后果是整次分析连结论一起作废（引擎里那套压缩重试 + 收尾就是补它的）。

现在按水位压：默认预算 360,000 字在任何窗口下都不触发（< 600,000），所以**已有项目的
行为不变**；只有把预算配得很大的项目才会吃到这层水位。
"""
from __future__ import annotations

import inspect
import os
import re

import pytest

from services.ai.budget import (
    CHARS_PER_TOKEN,
    COMPACT_AT_RATIO,
    DEFAULT_CONTEXT_TOKENS,
    context_watermark_chars,
    effective_prompt_budget,
    resolve_context_window,
)
from services.ai.engine import EngineLimits
from services.ai.llm_client import LLMError
from services.ai_analysis_service import _apply_model_window

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(relative: str) -> str:
    with open(os.path.join(PROJECT_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


class _FakeClient:
    """只实现 `model_contexts`。分析路径拿到的真客户端也只有这一个接口被用到。"""

    def __init__(self, contexts=None, *, error=None):
        self._contexts = contexts or {}
        self._error = error
        self.calls = 0

    def model_contexts(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._contexts


# ==========================================================================
# 一、判定：水位、默认窗口、以及「默认值必须说出来」
# ==========================================================================


def test_a_window_that_makes_the_budget_clearly_overflow_is_applied():
    client = _FakeClient({"m": 200_000})

    limits, note = _apply_model_window(
        client, {"api_model": "m"}, EngineLimits(prompt_char_budget=360_000)
    )

    assert limits.prompt_char_budget == 120_000, "200,000 token 的 60% 水位"
    assert "200,000" in note and "120,000" in note


def test_a_window_the_budget_fits_inside_leaves_it_alone():
    """预算 120,000 字、窗口 200,000 token —— 在水位内，**一个字都不压**。

    水位的作用是「留出余量」，而不是「把窗口占满」：压掉的是重复（历史），不是信息，
    所以没超水位时压它只会在没有依据的情况下砍分析质量。
    """
    client = _FakeClient({"m": 200_000})

    limits, note = _apply_model_window(
        client, {"api_model": "m"}, EngineLimits(prompt_char_budget=120_000)
    )

    assert limits.prompt_char_budget == 120_000
    assert note == ""


def test_the_default_budget_does_not_trigger_any_watermark():
    """**已有项目的行为一个字都不变。**

    默认预算 360,000 字小于任何「像样的窗口」的 60%（1M 的 60% 是 600,000），
    所以这次改动对没调过预算的项目是透明的 —— 改动不该顺手改掉所有分析的行为。
    """
    for window in (1_000_000, None, 0, "junk"):
        client = _FakeClient({"m": window})
        limits, note = _apply_model_window(
            client, {"api_model": "m"}, EngineLimits(prompt_char_budget=360_000)
        )
        assert (limits.prompt_char_budget, note) == (360_000, ""), window


def test_an_unknown_window_falls_back_to_a_stated_default():
    """窗口问不到时按 1M 处理，**但必须说出来这是默认值**。

    配一个超过 600,000 字的预算才会看到这条 —— 而那正是「不问窗口就必然撞窗」的配置。
    说明里写「默认」两个字是这个用例的重点：读的人必须能分清假设与事实。
    """
    client = _FakeClient({})

    limits, note = _apply_model_window(
        client, {"api_model": "m"}, EngineLimits(prompt_char_budget=2_000_000)
    )

    assert limits.prompt_char_budget == context_watermark_chars(DEFAULT_CONTEXT_TOKENS)
    assert "默认值" in note, f"没有写明窗口是按默认值来的：{note}"


def test_a_declared_window_is_not_described_as_a_default():
    """反向自检：真的问到了窗口时，说明里**不许**出现「默认值」。

    没有这一条，上面那条只能证明「文案里有『默认值』三个字」，证明不了它跟事实一致。
    """
    client = _FakeClient({"m": 128_000})

    _limits, note = _apply_model_window(
        client, {"api_model": "m"}, EngineLimits(prompt_char_budget=2_000_000)
    )

    assert "默认值" not in note, note
    assert "128,000" in note


@pytest.mark.parametrize("tokens", [0, -1, None, "abc", float("nan")])
def test_a_silly_window_falls_back_instead_of_crashing(tokens):
    """端点报了个荒唐的值（0 / 负数 / 非数字）时按默认窗口处理，**不抛异常**。

    额外的保护不该成为新的失败点：一次分析不能因为「窗口字段解析不出来」而失败。
    """
    window, defaulted = resolve_context_window(tokens)

    assert (window, defaulted) == (DEFAULT_CONTEXT_TOKENS, True)


def test_the_watermark_is_a_conservative_bound_not_a_hopeful_one():
    """水位是按**字符**算的，而窗口是按 token —— 这个换算方向必须是保守的那一边。

    `CHARS_PER_TOKEN` 取 1.0 意味着「一个字符至少一个 token」（中文约 1:1、英文约 4:1），
    于是「字符数 ≤ 窗口 × 60%」能推出「token 数 ≤ 窗口 × 60%」。取值变小（例如英文习惯的
    0.25）会让这个推论失效：那等于赌提示词里没有中文，而本平台的分析内容全是中文配表。
    """
    assert CHARS_PER_TOKEN >= 1.0, "换算比例取到 1 以下就不再是保守估计了"
    assert 0 < COMPACT_AT_RATIO < 1, "水位必须在 0 与 1 之间"
    assert context_watermark_chars(100_000) == int(100_000 * COMPACT_AT_RATIO * CHARS_PER_TOKEN)


def test_other_fields_of_the_limits_survive_the_clamp():
    """只改预算。`EngineLimits` 是 frozen 的，改错字段会静默丢掉别的额度。"""
    client = _FakeClient({"m": 100_000})

    limits, _ = _apply_model_window(
        client,
        {"api_model": "m"},
        EngineLimits(prompt_char_budget=500_000, max_rounds=3, max_tool_requests=7),
    )

    assert limits.prompt_char_budget == 60_000
    assert (limits.max_rounds, limits.max_tool_requests) == (3, 7)


@pytest.mark.parametrize(
    "configured,window",
    [(360_000, 1_000_000), (100_000, 200_000), (100, 1_000_000)],
)
def test_a_budget_inside_the_watermark_is_returned_with_no_note(configured, window):
    """没压的时候**说明必须是空的** —— 空说明是「这次没动过」的唯一信号，
    调用方据此决定要不要在界面上提示「本次分析被压缩过」。"""
    assert effective_prompt_budget(configured, window) == (configured, "")


# ==========================================================================
# 二、问不到窗口时什么都不做，也不报错
# ==========================================================================


def test_an_endpoint_that_declares_no_window_changes_nothing():
    client = _FakeClient({})

    limits, note = _apply_model_window(
        client, {"api_model": "m"}, EngineLimits(prompt_char_budget=360_000)
    )

    assert limits.prompt_char_budget == 360_000
    assert note == ""


def test_a_model_missing_from_the_listing_changes_nothing():
    """列表里有别的模型、没有当前这个 —— 与「没声明」同等对待，不拿别人的窗口来用。"""
    client = _FakeClient({"other": 8_000})

    limits, _ = _apply_model_window(
        client, {"api_model": "m"}, EngineLimits(prompt_char_budget=360_000)
    )

    assert limits.prompt_char_budget == 360_000


def test_a_failing_probe_does_not_fail_the_analysis():
    """**额外的保护不该成为新的失败点。**

    端点不支持列模型是常态（`list_models` 的文档里就写着），一次分析不能因为
    「问不到窗口」而失败。
    """
    client = _FakeClient(error=LLMError("端点不支持获取模型列表"))

    limits, note = _apply_model_window(
        client, {"api_model": "m"}, EngineLimits(prompt_char_budget=360_000)
    )

    assert limits.prompt_char_budget == 360_000
    assert note == ""


def test_a_client_without_the_probe_interface_is_tolerated():
    """不假设调用方一定给的是 `LLMClient`（测试替身、未来的别的实现都可能没有这个方法）。"""

    class _Bare:
        pass

    limits, _ = _apply_model_window(
        _Bare(), {"api_model": "m"}, EngineLimits(prompt_char_budget=360_000)
    )

    assert limits.prompt_char_budget == 360_000


def test_no_model_configured_means_no_probe_at_all():
    """模型名都没填时**不去问** —— 问来的窗口也没有用武之地，白搭一次出网请求。"""
    client = _FakeClient({"m": 1_000})

    limits, _ = _apply_model_window(
        client, {"api_model": "  "}, EngineLimits(prompt_char_budget=360_000)
    )

    assert limits.prompt_char_budget == 360_000
    assert client.calls == 0


# ==========================================================================
# 三、接线守卫（静态 + 签名）
# ==========================================================================


def test_the_default_window_is_a_named_constant_not_a_literal():
    """默认窗口必须是**一个有名字、有说明**的常量。

    这条用例的前身是「窗口必须是必传参数、不许有默认值」，理由是「给它一个默认值就等于
    让平台替模型猜窗口」。用户侧后来明确要求「问不到就默认 1M」，所以规则改成：可以有
    默认值，但 (1) 它必须是一个说得清来源的常量，(2) 用到它时必须在说明里写明这一点
    （见 `test_an_unknown_window_falls_back_to_a_stated_default`）。

    仍然不许的，是散落在代码里的字面量 —— 那种东西没人知道它从哪来、什么时候该改。
    """
    source = _read("services/ai/budget.py")
    assert "DEFAULT_CONTEXT_TOKENS = 1_000_000" in source, "默认窗口不再是有名常量"

    window, defaulted = resolve_context_window(None)
    assert (window, defaulted) == (DEFAULT_CONTEXT_TOKENS, True)
    # 传进来的值不会被改写：问到了就用问到的。
    assert resolve_context_window(131_072) == (131_072, False)


def test_the_configured_budget_is_never_raised_only_lowered():
    """水位只能把预算**压小**，永不放宽。

    反过来（把 100,000 字的预算提到 600,000）会让用户在配置界面填的那个数字失去意义，
    而且用户是按「填多少就最多花多少」理解的 —— 提示词预算同时是**成本**约束。
    """
    for configured in (0, 100, 100_000, 360_000, 10_000_000):
        budget, _note = effective_prompt_budget(configured, 1_000_000)
        assert budget <= configured


def test_the_clamped_limits_are_what_actually_runs():
    """算出来的额度必须真的传给引擎。

    只加函数不接线是个很容易漏的错：`_apply_model_window` 会被算一遍然后原样丢掉，
    测试全绿、线上无效。
    """
    code = re.sub(r"#[^\n]*", "", _read("services/ai_analysis_service.py"))

    assert "_apply_model_window(client, project_config" in code, "分析路径没有调用窗口压缩"
    assert "limits=limits," in code, "压缩后的额度没有传给 run_analysis"
    assert "limits=_engine_limits(project_config)," not in code, (
        "仍然把未压缩的额度传给了引擎"
    )


def test_the_analysis_path_is_the_only_place_that_builds_limits():
    """额度只有一处构造点。

    多一处就会有第二条路径绕过窗口压缩 —— 表现是「有的分析压了、有的没压」，
    排查时完全看不出规律。
    """
    calls = [
        line
        for line in _read("services/ai_analysis_service.py").splitlines()
        if "_engine_limits(" in line and not line.strip().startswith("#")
    ]
    definitions = [line for line in calls if line.lstrip().startswith("def ")]
    assert len(definitions) == 1, f"定义有 {len(definitions)} 处"
    assert len(calls) == 2, f"构造点不止一处（含定义共 {len(calls)} 行）: {calls}"


@pytest.mark.parametrize("name", ["services/ai/budget.py", "services/ai/llm_client.py"])
def test_no_model_name_appears_in_the_budget_layer(name):
    """预算层里**不能出现具体模型名**。

    出现模型名通常意味着有人加了一张「模型名 → 窗口」对照表。那是从别处抄来的数字，
    模型一迭代就过期，而过期之后没有任何东西会提醒你 —— 它只会一直偏低。
    """
    source = _read(name)
    for model in ("gpt-4", "claude-", "deepseek", "qwen", "glm-"):
        assert model not in source.lower(), f"{name} 里出现了模型名 {model!r}"
