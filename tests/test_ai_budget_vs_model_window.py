# -*- coding: utf-8 -*-
"""提示词预算 vs 模型上下文窗口。

## 这一组守的是什么

`prompt_char_budget` 是按**字符**配的，而模型窗口是按 **token** 计的，两者的换算比例
取决于提示词里中文占多少 —— **平台无从知道**。于是这里只有两条规则：

1. 窗口**只能向端点问**（`/v1/models` 里有时会声明）。问不到就不动任何东西 ——
   **不维护「模型名 → 窗口」对照表**：那是猜出来的数字，模型一迭代就过期，而按过期的
   窗口压预算会静默地砍掉分析质量。
2. 只有「按最乐观的 1 字 1 token 也算得超窗」这一档才真的压（见
   `budget.clamp_to_model_window`）。其余情况一律不动。

在线上的 G119 上，这两条的实际含义是：项目的预算是 200,000 字、模型窗口若是
200,000 token，**相等 → 不压**（没有依据）；而新默认值 360,000 字配同一个窗口就会被
压回 200,000。所以这层保护只在「换了更大的默认值、模型窗口却没跟上」时生效。
"""
from __future__ import annotations

import inspect
import os
import re

import pytest

from services.ai.budget import clamp_to_model_window
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
# 一、判定
# ==========================================================================


def test_a_window_that_makes_the_budget_clearly_overflow_is_applied():
    client = _FakeClient({"m": 200_000})

    limits, note = _apply_model_window(
        client, {"api_model": "m"}, EngineLimits(prompt_char_budget=360_000)
    )

    assert limits.prompt_char_budget == 200_000
    assert "360,000" in note


def test_a_window_the_budget_fits_inside_leaves_it_alone():
    """预算 200,000 字、窗口 200,000 token —— **相等也不压**。

    这正是 G119 线上的真实组合：字与 token 最乐观按 1:1 算刚好装得下，再往下压就是
    在没有依据的情况下砍分析质量，而且用户看不出发生了什么。
    """
    client = _FakeClient({"m": 200_000})

    limits, note = _apply_model_window(
        client, {"api_model": "m"}, EngineLimits(prompt_char_budget=200_000)
    )

    assert limits.prompt_char_budget == 200_000
    assert note == ""


def test_other_fields_of_the_limits_survive_the_clamp():
    """只改预算。`EngineLimits` 是 frozen 的，改错字段会静默丢掉别的额度。"""
    client = _FakeClient({"m": 100_000})

    limits, _ = _apply_model_window(
        client,
        {"api_model": "m"},
        EngineLimits(prompt_char_budget=500_000, max_rounds=3, max_tool_requests=7),
    )

    assert limits.prompt_char_budget == 100_000
    assert (limits.max_rounds, limits.max_tool_requests) == (3, 7)


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


def test_the_window_can_only_come_from_the_endpoint():
    """窗口必须是**必传参数**，没有默认值。

    给它一个默认值（哪怕是 200,000 这种「常见值」）就等于让平台替模型猜窗口，
    而猜错的方向是静默砍预算。这条用例拦的正是那次「顺手加个默认值」的改动。
    """
    parameter = inspect.signature(clamp_to_model_window).parameters["context_tokens"]
    assert parameter.default is inspect.Parameter.empty, "窗口有了默认值 = 平台开始猜窗口"


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
