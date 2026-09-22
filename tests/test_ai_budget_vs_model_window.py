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

现在按水位压：下面这些用例用的都是**显式传入**的 360,000 字，它在 1M 的窗口下不触发；
所以**已有项目的
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
from services.ai.budget_plan import derive_tool_limits
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

    传进来的 360,000 字小于任何「像样的窗口」的 60%（1M 的 60% 是 600,000），
    所以这次改动对没调过预算的项目是透明的 —— 改动不该顺手改掉所有分析的行为。
    """
    for window in (1_000_000, None, 0, "junk"):
        client = _FakeClient({"m": window})
        limits, note = _apply_model_window(
            client, {"api_model": "m"}, EngineLimits(prompt_char_budget=360_000)
        )
        assert (limits.prompt_char_budget, note) == (360_000, ""), window


def test_a_clamped_budget_still_carries_the_platform_prompt():
    """被水位压过的那个数交给引擎时**必须是整份提示词的额度**，不能再扣一次平台那段。

    引擎比的是**整份提示词**：`engine.estimate_chars(messages)`，而 `messages` 的第一条
    就是系统提示词（`subagent.build_seed_messages` 的「system + 含整份变更清单的 user」）。
    所以 `limits.prompt_char_budget` 是「平台内置提示词 + 用户内容」的**总数**。

    这里原先放的是 `总数 − platform_chars`，于是平台那段被**扣了两次**：一次在
    `effective_prompt_budget` 的水位里，一次在这个减法里。重度压缩的项目因此白少用
    `platform_chars` 那么多额度。

    **为什么此前没被发现**：上面那条 `test_other_fields_of_the_limits_survive_the_clamp`
    用的是默认的 `platform_chars=0`，两种写法都得 60,000 —— 于是这个
    `platform_chars != 0` 的形状**一条用例都没有**。这条补的就是那个空档。
    """
    client = _FakeClient({"m": 100_000})  # 水位 = 100,000 × 60% = 60,000 字

    limits, note = _apply_model_window(
        client,
        {"api_model": "m"},
        EngineLimits(prompt_char_budget=500_000, max_tool_requests=40),
        platform_chars=20_000,
    )

    assert "水位" in note, note
    # 引擎拿到的总数 = 水位本身，**不是** 60,000 − 20,000。
    assert limits.prompt_char_budget == 60_000, limits.prompt_char_budget
    # 而派生单条上限用的是**用户内容**的额度（平台那段不能吃掉用户的额度）：
    # 这一半必须留着，否则上一条会退化成「干脆别减」。
    assert limits.tool_limits["file_diff"] == derive_tool_limits(
        prompt_char_budget=40_000, max_tool_requests=40
    )["file_diff"], limits.tool_limits


def test_the_clamped_budget_does_not_spend_the_window_on_the_platform_prompt():
    """反向自检：把平台那段的减法去掉，总数就会**超过水位** —— 那正是要防的事。

    没有这一条，上面那条只能证明「等于 60,000」，证明不了「60,000 是水位而不是别的数」。
    """
    client = _FakeClient({"m": 100_000})

    limits, _ = _apply_model_window(
        client,
        {"api_model": "m"},
        EngineLimits(prompt_char_budget=500_000),
        platform_chars=20_000,
    )

    # 水位是窗口的 60%，不是窗口本身 —— 上限不能被平台提示词顶到水位以上。
    assert limits.prompt_char_budget <= context_watermark_chars(100_000)
    assert limits.prompt_char_budget < 500_000, "没压住用户配的 500,000"


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

    assert "_apply_model_window(" in code, "分析路径没有调用窗口压缩"
    assert "client, project_config, _engine_limits(project_config" in code, (
        "窗口压缩没有接在额度构造上（它必须压的就是那份要交给引擎的额度）"
    )
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


# ==========================================================================
# 配置里那一栏「提示词字符预算」算谁的内容
# ==========================================================================


def test_the_builtin_prompt_does_not_eat_the_users_budget():
    """**内置提示词由平台出，加在用户额度之上。**

    不这样切的话，用户改的是一个数、影响的是另一个数：他配 100k，实际能用的只有
    100k 减去内置提示词那十几 k —— 而内置提示词的字数随平台版本变化，他在配置页上
    完全看不出这件事。这一条锁的是「加回去」这个动作（`_engine_limits` 的
    `platform_chars`）。
    """
    from services.ai_analysis_service import _engine_limits

    plain = _engine_limits({"prompt_char_budget": 100_000})
    with_builtin = _engine_limits({"prompt_char_budget": 100_000}, platform_chars=16_000)

    assert plain.prompt_char_budget == 100_000
    assert with_builtin.prompt_char_budget == 116_000
    # 其余几项不受影响（这个参数只管预算那一栏）。
    assert plain.max_rounds == with_builtin.max_rounds
    assert plain.max_tool_requests == with_builtin.max_tool_requests


def test_the_platform_chars_cover_the_builtin_sections_only():
    """内置那一段**只**含平台自己的部分：项目知识包、补充指令、子 skill 索引都不算。

    它们仍然从用户额度里扣 —— 那是用户自己要带的内容。混进去的后果是「项目知识包写得
    越多，用户以为还剩的额度越少」，而真正吃额度的其实是系统提示词里那一段（组装侧照旧
    把它算进 overhead），两处口径一对不上，用户就会以为平台算错了。
    """
    from services.ai.prompt import build_system_prompt, platform_prompt_chars
    from tests.test_ai_prompt import _loaded

    loaded = _loaded()
    with_project = platform_prompt_chars(loaded)
    without_project = platform_prompt_chars(_loaded(with_project=False))

    assert with_project == without_project, "内置那一段的字数不该随项目知识包变化"
    assert 0 < with_project < len(build_system_prompt(loaded)), (
        "内置那一段应当是系统提示词的一部分（不是全部，也不是 0）"
    )


def test_a_missing_skill_loader_result_is_zero_not_an_exception():
    """skill 加载失败时 `loaded` 是 `None` —— 那里取字数要回 0，不能抛。

    加载失败本身不阻断分析（见 `_load_project_skills`），而算预算是每一条路径都会走的
    一步：在这里抛异常等于把「知识包缺失」升级成「分析跑不起来」。
    """
    from services.ai.prompt import platform_prompt_chars

    assert platform_prompt_chars(None) == 0


# ==========================================================================
#  四、运行计划（E3）：额度必须**真的**改变截断点
#
#  上面那些用例守的是「水位怎么算」与「谁调用谁」—— 全是形状与接线。形状对、接线对，
#  但额度算完被绕过、或者 `ContextTools` 读的是另一个键，两种实现都能让它们全绿。
#  这一节拿真的一份 40,000 字 diff 走一遍取数，看它到底被砍在哪里。
# ==========================================================================


def test_the_planned_tool_limits_really_move_the_truncation_point():
    """**计划里的单条上限必须真的改变取数的截断点。**

    期望值取自 `derive_tool_limits(prompt_char_budget=560_000, max_tool_requests=40)`：
    `share = int(560000 * 0.65 / 12) = 30,333`。同一份 40,000 字的 diff：

    * 默认额度（11,000）→ 砍到 11,000；
    * 计划额度（30,333）→ 砍到 30,333。

    两份都截断了（内容本身超过 30,333），所以差别**只**能来自额度 —— 这正是这条用例
    要证明的那一件事。
    """
    from services.ai.budget_plan import derive_tool_limits
    from services.ai.context_tools import DEFAULT_TOOL_LIMITS, ContextTools
    from services.ai.protocol import ContextRequest

    body = "x" * 40_000

    class Provider:
        def file_diff(self, commit, path):
            # 切不开的一整块（没有 `@@` 块头）→ 走 `truncate_text_middle`，
            # 它的截断点就是 `limits` 里给的那个数。
            return body

    request = ContextRequest(type="file_diff", commit="a" * 40, path="a.lua")

    def delivered(limits) -> object:
        tools = ContextTools(Provider(), max_tool_requests=40, limits=limits)
        return tools.execute([request]).items[0]

    planned = derive_tool_limits(prompt_char_budget=560_000, max_tool_requests=40)
    assert planned["file_diff"] == 30_333, planned

    default_item = delivered(dict(DEFAULT_TOOL_LIMITS))
    planned_item = delivered(planned)

    assert default_item.meta["limit"] == 11_000, default_item.meta
    assert planned_item.meta["limit"] == 30_333, planned_item.meta
    assert len(default_item.text) <= 11_000
    assert 30_333 - 100 <= len(planned_item.text) <= 30_333, len(planned_item.text)
    assert len(planned_item.text) > len(default_item.text) + 15_000


def test_a_planned_file_content_cap_is_capped_by_what_the_provider_delivers():
    """`file_content` **不许**在计划里报一个取数侧根本给不出的数。

    正文在取数侧就按 `CONTENT_MAX_CHARS`（11,000）切好了（`platform_provider` /
    Agent 两侧都读这个常量）。计划里若写 30,333，那条抬头说的行数与实际给出的正文
    就对不上 —— 而那个行号是模型写进结论里的坐标。

    `file_diff` / `read_reference` 不受这个夹：它们的正文由平台自己拼，取数侧的
    「11,000」只是**旧的默认额度**，不是硬上限。
    """
    from services.ai.budget_plan import derive_tool_limits
    from utils.content_window import CONTENT_MAX_CHARS

    planned = derive_tool_limits(prompt_char_budget=560_000, max_tool_requests=40)

    assert planned["file_diff"] == 30_333, "diff 的额度没有被抬起来"
    assert planned["file_content_provider_max_chars"] == CONTENT_MAX_CHARS
    assert planned["file_content_provider_max_chars"] <= planned["file_content"]


def test_the_remaining_budget_form_of_the_limits():
    """按**剩余**预算算的那一档：剩余少了，单条上限跟着降；不给就走总预算口径。

    两种口径并存不是冗余：运行到一半时该看剩余（前面几轮已经花掉的不该再被算一遍），
    而起跑前只有总预算。判据必须是「给没给」，不是「剩余是不是 0」—— 剩余真的为 0
    时按 0 算出来的额度由下限兜住，而不是悄悄退回总预算。
    """
    from services.ai.budget_plan import derive_tool_limits

    full = derive_tool_limits(prompt_char_budget=560_000, max_tool_requests=40)
    same = derive_tool_limits(
        prompt_char_budget=560_000,
        max_tool_requests=40,
        remaining_chars=560_000,
        remaining_requests=40,
    )
    assert same == full, "给了与总预算相同的剩余时结果应当逐字相同"

    shrunk = derive_tool_limits(
        prompt_char_budget=560_000,
        max_tool_requests=40,
        remaining_chars=120_000,
        remaining_requests=4,
    )
    assert shrunk["file_diff"] < full["file_diff"], shrunk
    # 下限仍在：剩余再少也不会给一个小于 11,000 的 diff 额度（那是既有默认值，
    # 降到它之下等于让「预算还有余」的项目比默认更差）。
    assert shrunk["file_diff"] >= 11_000, shrunk


def test_the_plan_carries_the_window_source_and_the_reserved_output_space():
    """E3 要求的另外两项事实：**窗口来源**与**保留输出空间**。

    它们必须在计划里、而且必须能被界面渲染出来：窗口来源是
    「600,000 这个水位是怎么来的」的唯一说明（按默认值压的 ≠ 端点声明的）；
    保留输出空间是「为什么水位只到 60%」的那个数。
    """
    from services.ai.budget import context_reserved_chars
    from services.ai.budget_plan import build_budget_plan

    plan = build_budget_plan(
        configured_prompt_chars=2_000_000,
        effective_prompt_chars=580_000,
        platform_chars=20_000,
        max_rounds=8,
        max_tool_requests=40,
        window_source="not_probed_default",
        reserved_output_chars=context_reserved_chars(1_000_000),
        window_note="端点未声明窗口，按默认值",
    )

    assert plan["prompt_chars"]["window_source"] == "not_probed_default"
    assert plan["prompt_chars"]["reason"] == "端点未声明窗口，按默认值"
    assert plan["reserved_output"]["chars"] == 400_000, plan["reserved_output"]
    # 默认不给时是 0 / 空串，不是 None（界面按「有没有」判，None 会让它去猜）。
    bare = build_budget_plan(
        configured_prompt_chars=560_000,
        effective_prompt_chars=560_000,
        platform_chars=0,
        max_rounds=8,
        max_tool_requests=40,
    )
    assert bare["prompt_chars"]["window_source"] == ""
    assert bare["reserved_output"]["chars"] == 0
