# -*- coding: utf-8 -*-
"""逐请求指纹与分叉原因（工作包 F 的 A 包）：**答出「与上一次在哪个消息开始分叉」**。

## 这一组守的是什么

平台原先有逐轮用量与时长，但那两类数答不出「**这次请求与上一次在哪个消息开始分叉**」。
缺了它，一次缓存未命中就分不清是：

* **平台自己重写了前缀**（那要修，落在 `other` 这一档）；
* **压缩 / 提示词或知识包更新 / 换了快照 / 服务端过期**（那不该修 —— 为了复用缓存去
  隐藏真实变更，会直接损害报告的正确性）。

所以这一组按**行为**验四件事：

1. 「相同前缀追加」与「内部断点挪动但实际请求不变」真的被认成 `append`，而不是分叉；
2. 真分叉时**原因分得对**（首条动态消息变化 / 压缩 / 提示词版本 / 快照 / 其他）；
3. **口径**：没算出来（或上游没报）是 `None`，**不是 0**；
4. **隐私**：指纹里只有哈希与计数 —— 不存提示词正文，不含 API key，端点已去凭据。

外加两条接线（工作包 F 的另外两条要求）：

* `reasoning_tokens` 接通了上游的 `completion_tokens_details`（在此之前它恒为 `None`）；
* 三类耗时（模型调用 / 本地取数 / 建索引）在逐轮账上是分开的。
"""
from __future__ import annotations

import json

import pytest

from services.ai import request_fingerprint as fp
from services.ai.llm_client import ChatResult, LLMClient
from services.ai.request_fingerprint import (
    DIVERGENCE_APPEND,
    DIVERGENCE_COMPACTION,
    DIVERGENCE_INITIAL,
    DIVERGENCE_OTHER,
    DIVERGENCE_PROMPT_CHANGE,
    DIVERGENCE_SNAPSHOT_CHANGE,
    RequestFingerprint,
    RequestFingerprinter,
    canonical_message,
    common_prefix,
    event_columns,
    provider_snapshot_id,
    request_digest,
    stable_prefix_digest,
)
from tests.test_ai_engine import COMMIT, ScriptedClient, _final, _requests, _run
from tests.test_ai_llm_client import SECRET, FakeResponse, _client, _patch


def _messages(*pairs) -> list[dict[str, str]]:
    return [{"role": role, "content": text} for role, text in pairs]


def _system(text: str = "你是配表评审专家。") -> dict[str, str]:
    return {"role": "system", "content": text}


def _seed(number: int = 1, manifest: str = "本次变更共 1 个提交。") -> dict[str, str]:
    """稳定前缀的第二条：含变更清单的那一条 user 消息（见 `STABLE_PREFIX_MESSAGES`）。"""
    return {"role": "user", "content": f"{manifest}\n（第 {number} 份清单）"}


# ==========================================================================
#  一、纯函数：规范化、哈希、公共前缀
# ==========================================================================


def test_the_canonical_encoding_does_not_depend_on_key_order():
    """**同一条消息的两种键序必须算出同一个哈希。**

    消息字典在引擎里会被复制、被 `_mark_current` 挪断点，键的插入顺序不保证一致。
    按插入顺序编码的话，「实际发出去的东西一模一样」会被算成两次不同的请求 ——
    那正是这个诊断最不该出的错（把一个不存在的分叉报给人）。
    """
    one = {"role": "user", "content": "改了道具表"}
    two = {"content": "改了道具表", "role": "user"}

    assert canonical_message(one) == canonical_message(two)
    assert request_digest([one]) == request_digest([two])


def test_the_request_digest_ignores_the_internal_cache_breakpoint():
    """**内部断点变化但实际请求不变** —— 整请求哈希必须一字不变。

    断点是平台自己的调度信息（`prompt_cache` 把它从消息上摘掉之后才发出去），
    «把断点从这里挪到那里» 不该被算成一次分叉。
    """
    from services.ai.prompt_cache import strip_cache_breakpoints

    plain = _messages(("system", "规则"), ("user", "清单"))
    marked = [dict(plain[0]), dict(plain[1], _cache_breakpoint=True)]

    assert request_digest(strip_cache_breakpoints(marked)) == request_digest(plain)


def test_the_common_prefix_counts_messages_and_characters():
    """最长公共前缀给两个数：**条数**与这些消息的**字符数**（分叉点就在那里）。"""
    previous = _messages(("system", "规则"), ("user", "清单"), ("assistant", "好"))
    current = _messages(("system", "规则"), ("user", "清单"), ("user", "新的"))

    assert common_prefix(previous, current) == (2, len("规则") + len("清单"))


def test_the_stable_prefix_hash_is_comparable_across_runs():
    """稳定前缀哈希只覆盖前两条（system + 含变更清单的那一条）——
    **跨运行比的就是它**（同一次运行内的成员之间也是这一条）。"""
    first = [_system(), _seed(), {"role": "user", "content": "任务书 A"}]
    second = [_system(), _seed(), {"role": "user", "content": "完全不同的任务书 B"}]

    assert stable_prefix_digest(first) == stable_prefix_digest(second)
    assert request_digest(first) != request_digest(second)
    # 换了系统提示词（或换了知识包、换了变更清单）→ 前缀哈希必然变。
    assert stable_prefix_digest([_system("换了一套规则"), _seed()]) != stable_prefix_digest(
        [_system(), _seed()]
    )


def test_every_divergence_reason_is_on_the_whitelist():
    """六档判据各自可达，值域是一份白名单（界面按它给中文说法）。"""
    assert set(fp.DIVERGENCE_REASONS) == {
        DIVERGENCE_INITIAL,
        DIVERGENCE_APPEND,
        DIVERGENCE_COMPACTION,
        DIVERGENCE_PROMPT_CHANGE,
        DIVERGENCE_SNAPSHOT_CHANGE,
        DIVERGENCE_OTHER,
    }
    # 没有上一个请求 → initial（**不是 other**：这两句话的处置完全不同）。
    assert fp.deviation_reason(
        has_previous=False, stable_prefix_same=False, is_extension=False,
        compacted=False, prompt_changed=False, snapshot_changed=False,
    ) == DIVERGENCE_INITIAL
    # 稳定前缀没变 + 严格延长 → append。
    assert fp.deviation_reason(
        has_previous=True, stable_prefix_same=True, is_extension=True,
        compacted=False, prompt_changed=False, snapshot_changed=False,
    ) == DIVERGENCE_APPEND
    # 稳定前缀没变、不是延长、但压过历史 → compaction。
    assert fp.deviation_reason(
        has_previous=True, stable_prefix_same=True, is_extension=False,
        compacted=True, prompt_changed=False, snapshot_changed=False,
    ) == DIVERGENCE_COMPACTION
    # 稳定前缀变了 → 按两把版本号归因；**快照优先**（换了快照时提示词版本通常没变，
    # 先判提示词会把它写成一句错话）。
    assert fp.deviation_reason(
        has_previous=True, stable_prefix_same=False, is_extension=False,
        compacted=False, prompt_changed=False, snapshot_changed=True,
    ) == DIVERGENCE_SNAPSHOT_CHANGE
    assert fp.deviation_reason(
        has_previous=True, stable_prefix_same=False, is_extension=False,
        compacted=False, prompt_changed=True, snapshot_changed=False,
    ) == DIVERGENCE_PROMPT_CHANGE
    # 两把都变也报 snapshot_change：other 要留给**真的没人解释得了**的那一种。
    assert fp.deviation_reason(
        has_previous=True, stable_prefix_same=False, is_extension=False,
        compacted=False, prompt_changed=True, snapshot_changed=True,
    ) == DIVERGENCE_SNAPSHOT_CHANGE
    # 前缀变了而两把版本号都没变 → other（**这一类才是要去看代码的**）。
    assert fp.deviation_reason(
        has_previous=True, stable_prefix_same=False, is_extension=False,
        compacted=False, prompt_changed=False, snapshot_changed=False,
    ) == DIVERGENCE_OTHER
    # 前缀没变、不是延长、也没压过 → other。
    assert fp.deviation_reason(
        has_previous=True, stable_prefix_same=True, is_extension=False,
        compacted=False, prompt_changed=False, snapshot_changed=False,
    ) == DIVERGENCE_OTHER


# ==========================================================================
#  二、指纹器：逐次调用的分叉原因
# ==========================================================================


def test_the_first_call_says_initial_and_reports_no_common_prefix():
    """第一次调用：`initial`，公共前缀两格都是 `None`（**不是 0**）。"""
    fingerprinter = RequestFingerprinter(prompt_version="p1", snapshot_id="s1")

    result = fingerprinter.before_call([_system(), _seed()])

    assert result.prefix_divergence_reason == DIVERGENCE_INITIAL
    assert result.prefix_common_messages is None, "没有上一个请求时不许写 0"
    assert result.prefix_common_chars is None
    assert result.message_count == 2
    assert result.request_chars == len("你是配表评审专家。") + len("本次变更共 1 个提交。\n（第 1 份清单）")


def test_appending_a_turn_is_append_extension():
    """append-only 的那条路（多轮分析的主路径）：第二轮必须是 `append`。"""
    fingerprinter = RequestFingerprinter(prompt_version="p1", snapshot_id="s1")
    first = [_system(), _seed()]
    fingerprinter.before_call(list(first))
    second = [
        *first,
        {"role": "assistant", "content": "我要看道具表"},
        {"role": "user", "content": "这是正文"},
    ]

    result = fingerprinter.before_call(list(second))

    assert result.prefix_divergence_reason == DIVERGENCE_APPEND
    assert result.prefix_common_messages == len(first), "上一次那 2 条一条不差地是前缀"
    assert result.prefix_common_chars == sum(len(item["content"]) for item in first)
    assert result.message_count == 4


def test_moving_the_breakpoint_between_two_calls_is_not_a_divergence():
    """**内部断点变化但实际请求不变** —— 两处都必须看不出来。

    断点③每轮都会挪到最新的那条 user 消息上（`_mark_current`）。挪断点若被算成
    分叉，这个诊断在正常路径上就会满屏假信号，读的人从此不再看它。
    """
    fingerprinter = RequestFingerprinter(prompt_version="p1", snapshot_id="s1")
    first = fingerprinter.before_call(
        [_system(), _seed(), {"role": "user", "content": "第一轮", "_cache_breakpoint": True}]
    )
    second = fingerprinter.before_call(
        [_system(), _seed(), {"role": "user", "content": "第一轮"}]
    )

    assert second.request_fingerprint == first.request_fingerprint
    assert second.prefix_divergence_reason == DIVERGENCE_APPEND


def test_a_rewritten_prefix_without_a_version_change_is_other():
    """**稳定前缀被改写、而提示词版本与快照都没变** → `other`。

    这一档才是「平台自己重写了前缀」的证据（要去看代码）。压缩、换提示词、换快照
    各自有名字，不会落到这里。
    """
    fingerprinter = RequestFingerprinter(prompt_version="p1", snapshot_id="s1")
    fingerprinter.before_call([_system(), _seed(), {"role": "user", "content": "第一轮"}])

    result = fingerprinter.before_call(
        [_system(), _seed(manifest="换了另一份清单"), {"role": "user", "content": "第一轮"}]
    )

    assert result.prefix_divergence_reason == DIVERGENCE_OTHER
    assert result.prefix_common_messages == 1, "系统提示词那一条仍然是公共前缀"


def test_a_prompt_version_bump_names_itself():
    """换了提示词 → `prompt_change`（允许的自然失效，不该被当成 bug 去修）。"""
    fingerprinter = RequestFingerprinter(prompt_version="p1", snapshot_id="s1")
    fingerprinter.before_call([_system(), _seed()])
    # 下一次调用时提示词版本变了（同一段消息序列也被改写）。
    fingerprinter.prompt_version = "p2"

    result = fingerprinter.before_call([_system("新的规则"), _seed()])

    assert result.prefix_divergence_reason == DIVERGENCE_PROMPT_CHANGE
    assert result.prompt_version == "p2"


def test_a_new_snapshot_names_itself_even_if_the_prompt_also_changed():
    """换了快照 → `snapshot_change`（快照优先：换了快照时提示词版本通常没变）。"""
    fingerprinter = RequestFingerprinter(prompt_version="p1", snapshot_id="s1")
    fingerprinter.before_call([_system(), _seed()])
    fingerprinter.prompt_version = "p2"
    fingerprinter.snapshot_id = "s2"

    result = fingerprinter.before_call([_system("新的规则"), _seed(manifest="新快照的清单")])

    assert result.prefix_divergence_reason == DIVERGENCE_SNAPSHOT_CHANGE
    assert result.snapshot_id == "s2"


def test_a_compacted_request_says_compaction():
    """平台压过历史（或压小重发 / 收尾）→ `compaction`，**不是** `other`。

    这两句话的处置完全相反：前者是必要裁剪（不该修），后者说明平台在别处重写了前缀
    （要去看代码）。把压缩报成 `other`，等于每次压过历史的运行都多一条要人去查的线索。
    """
    fingerprinter = RequestFingerprinter(prompt_version="p1", snapshot_id="s1")
    fingerprinter.before_call([
        _system(), _seed(),
        {"role": "user", "content": "第一轮"}, {"role": "assistant", "content": "好"},
    ])

    result = fingerprinter.before_call(
        [_system(), _seed(), {"role": "user", "content": "（压过的历史摘要）"}],
        compacted=True,
    )

    assert result.prefix_divergence_reason == DIVERGENCE_COMPACTION
    assert result.compacted is True
    assert result.to_dict()["compacted"] is True


def test_the_stable_prefix_survives_compaction():
    """压历史**不动前两条**（`compact_history` 的 `protect_head`）——
    所以压缩那一轮的前缀哈希与前一轮相同，跨运行复用缓存的那一段没被改写。"""
    fingerprinter = RequestFingerprinter(prompt_version="p1", snapshot_id="s1")
    first = fingerprinter.before_call([_system(), _seed(), {"role": "user", "content": "一"}])
    second = fingerprinter.before_call(
        [_system(), _seed(), {"role": "user", "content": "（摘要）"}], compacted=True
    )

    assert second.stable_prefix_fingerprint == first.stable_prefix_fingerprint


# ==========================================================================
#  三、口径与隐私
# ==========================================================================


def test_a_fingerprint_that_was_not_computed_is_none_not_zero():
    """没算出来（老调用方、诊断失败）→ 每一列都是 `None`，**不是 0**。

    0 会让「这一次没算」在界面上长得像「公共前缀 0 条」—— 后者是一个观测值，
    而前者是「不知道」。
    """
    columns = event_columns(None)

    assert set(columns) == {
        "request_fingerprint",
        "stable_prefix_fingerprint",
        "prefix_common_messages",
        "prefix_common_chars",
        "prefix_divergence_reason",
        "fingerprint_json",
    }
    assert all(value is None for value in columns.values()), columns


def test_the_fingerprint_carries_no_prompt_text_and_no_api_key():
    """**绝不把提示词再落一份库，也绝不保存 API key。**

    这里拿一段可识别的「正文」与一个密钥串喂进去，断言它们**在指纹的任何一格上都
    不出现**（整请求哈希是单向的，反推不出来）。
    """
    secret_body = "PATIENT-ZERO-正文标记"
    client = LLMClient(
        base_url="https://user:pass@gateway.internal/v1/chat/completions",
        api_key=SECRET,
        model="m1",
    )
    fingerprinter = RequestFingerprinter(
        prompt_version="p1", snapshot_id="s1",
        model=client.model, endpoint=client.base_url,
    )

    result = fingerprinter.before_call([_system(secret_body), _seed()])
    dumped = json.dumps(result.to_dict(), ensure_ascii=False)
    columns = event_columns(result)

    assert secret_body not in dumped, "提示词正文进了诊断数据"
    assert SECRET not in dumped, "API key 进了诊断数据"
    assert secret_body not in json.dumps(columns, ensure_ascii=False)
    # 端点标识是**归一之后**的地址：URL 里的凭据必须已经被去掉。
    assert "pass" not in result.endpoint and "@" not in result.endpoint
    assert result.endpoint == "https://gateway.internal/v1"


def test_the_fingerprint_never_takes_the_call_down():
    """诊断算不出来**不能影响分析**：调用点两侧都是付费的模型调用。

    拿一个根本不是消息序列的东西喂进去，只该返回 `None`（= 这一轮未上报），
    而不是抛出去 —— 引擎那边 `_call_model` 就指着这条性质。
    """
    fingerprinter = RequestFingerprinter()

    assert fingerprinter.before_call(None) is None  # type: ignore[arg-type]
    assert fingerprinter.before_call(12345) is None  # type: ignore[arg-type]
    assert fingerprinter.before_call([1, 2, 3]) is None, (
        "不是消息（没有 items()）时也算不出来 —— 如实记未上报，不许编"
    )
    # 上一次真算出来的指纹不受影响（脏输入不该把「上一个请求」弄丢）。
    assert fingerprinter.before_call([_system()]) is not None
    assert fingerprinter.before_call(None) is None  # type: ignore[arg-type]
    assert fingerprinter.previous is not None


def test_the_endpoint_and_the_model_are_recorded_per_request():
    """逐请求记模型与端点：换模型/换端点会让服务端缓存整段失效，而这件事在别处看不出来。"""
    fingerprinter = RequestFingerprinter(model="m1", endpoint="https://h/v1")

    result = fingerprinter.before_call([_system()])

    assert result.model == "m1"
    assert result.endpoint == "https://h/v1"


def test_the_snapshot_id_comes_from_the_provider_or_is_empty():
    """快照 id 从 provider 上读（鸭子类型，与 `provider_repo_paths` 同一套手法）。

    拿不到就是空串 —— 它的含义是「不知道这次是哪个快照」，判据那一侧据此退回
    `other`，**不是**断言「快照没变」。
    """
    class WithScope:
        def repo_read_scope(self):
            return type("Scope", (), {"frozen": type("Frozen", (), {"tip": "deadbeef" * 5})()})()

    class Broken:
        def repo_read_scope(self):
            raise RuntimeError("列不出跟踪树")

    class Bare:
        pass

    assert provider_snapshot_id(WithScope()) == "deadbeef" * 5
    assert provider_snapshot_id(Broken()) == ""
    assert provider_snapshot_id(Bare()) == ""


# ==========================================================================
#  四、推理 token 接通了上游（在此之前它恒为 None）
# ==========================================================================


def _chat_payload(usage_extra: dict) -> dict:
    return {
        "model": "m1",
        "choices": [{"message": {"content": "好"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40, **usage_extra},
    }


def test_the_reasoning_tokens_are_read_from_the_completion_details(monkeypatch):
    """上游报了 `completion_tokens_details.reasoning_tokens` → 记下来。

    这一格是「能不能下调单次输出上限」的前置数据：输出撞上限时，「写得太长」与
    「想得太久」的处置完全相反。
    """
    _patch(monkeypatch, lambda *_: FakeResponse(
        payload=_chat_payload({"completion_tokens_details": {"reasoning_tokens": 31}})
    ))

    result = _client().complete([{"role": "user", "content": "hi"}])

    assert result.reasoning_tokens == 31
    assert result.reasoning_source == "completion_tokens_details.reasoning_tokens"


def test_an_upstream_that_does_not_report_reasoning_leaves_it_none(monkeypatch):
    """**上游没报就是 `None`，不是 0。** 0 是「确实没有推理」这个确定的观测值。"""
    _patch(monkeypatch, lambda *_: FakeResponse(payload=_chat_payload({})))

    result = _client().complete([{"role": "user", "content": "hi"}])

    assert result.reasoning_tokens is None
    assert result.reasoning_source == ""


def test_a_reported_zero_is_zero(monkeypatch):
    """报了 0（确实没有推理）→ 就是 `0`，不许被折成 `None`。"""
    _patch(monkeypatch, lambda *_: FakeResponse(
        payload=_chat_payload({"completion_tokens_details": {"reasoning_tokens": 0}})
    ))

    assert _client().complete([{"role": "user", "content": "hi"}]).reasoning_tokens == 0


def test_reasoning_must_be_a_subset_of_the_output(monkeypatch):
    """推理 **> 输出**只可能是字段读错了对象（推理是输出的子集）→ 不采信。

    与缓存那两个字段同一条纪律：拆错的分子分母比没有更糟（它会算出一个看起来很合理
    但错误的占比）。
    """
    _patch(monkeypatch, lambda *_: FakeResponse(
        payload={
            "choices": [{"message": {"content": "好"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "completion_tokens_details": {"reasoning_tokens": 900},
            },
        }
    ))

    result = _client().complete([{"role": "user", "content": "hi"}])

    assert result.reasoning_tokens is None
    assert result.reasoning_source == ""


def test_a_flat_reasoning_field_is_the_fallback(monkeypatch):
    """少数网关把同名字段平铺在 `usage` 上 —— 认它（来源标记要能分辨是哪一种形态）。"""
    _patch(monkeypatch, lambda *_: FakeResponse(
        payload=_chat_payload({"reasoning_tokens": 7})
    ))

    result = _client().complete([{"role": "user", "content": "hi"}])

    assert result.reasoning_tokens == 7
    assert result.reasoning_source == "reasoning_tokens"


# ==========================================================================
#  五、真跑一次引擎：逐轮账上真的有指纹与三类耗时
# ==========================================================================


class _ReasoningClient(ScriptedClient):
    """按脚本回答，并且**上游报了推理 token**。用来钉住那一格真的接到了帧上。"""

    def complete(self, messages, *, temperature=None):
        result = super().complete(messages, temperature=temperature)
        return ChatResult(
            text=result.text,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cache_read_tokens=600,
            cache_source="prompt_cache_hit_tokens",
            reasoning_tokens=17,
            reasoning_source="completion_tokens_details.reasoning_tokens",
        )


class _Frames:
    """把引擎每一轮报出来的那一帧收下来（`on_round`）。"""

    def __init__(self) -> None:
        self.items: list = []

    def __call__(self, progress) -> None:
        self.items.append(progress)

    def __len__(self) -> int:
        return len(self.items)


def test_each_round_carries_a_fingerprint_and_the_three_timings():
    """跑两轮（第一轮要上下文、第二轮交结论），**报出来的那一帧**上要有：

    * 分叉原因：第 1 轮 `initial`、第 2 轮 `append`（多轮分析的主路径就是这么走的）；
    * 公共前缀条数：第 2 轮 = 第 1 轮发出去的消息条数；
    * 三类耗时**分开**：模型调用、本地取数（模型要过上下文的那一轮才发生）、
      建索引（只记在**第 1 轮**上 —— 循环之前那一段准备只发生一次）；
    * 指纹算在**真正发出去的那一份消息**上（拿模型收到的那一份去对，不信「打算发的」）；
    * 推理 token 接上了上游（**不是 `None`**，也不是 0）。
    """
    client = _ReasoningClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": "a/表.xlsx"}), _final()
    )
    frames = _Frames()

    outcome = _run(client, on_round=frames)

    assert outcome.status == "succeeded"
    assert len(frames) == 2, "两轮应当报两帧（on_start 不传，所以没有第 0 帧）"
    first = frames.items[0].round_diagnostics
    second = frames.items[1].round_diagnostics
    assert first is not None and second is not None

    assert first["prefix_divergence_reason"] == DIVERGENCE_INITIAL
    assert first["prefix_common_messages"] is None, "没有上一个请求时不许写 0"

    # 指纹算的是**发出去的那一份**：拿模型真收到的那一份重算一遍哈希，必须逐字相同。
    sent = client.calls[0]
    from services.ai.prompt_cache import strip_cache_breakpoints

    assert first["request_fingerprint"] == request_digest(strip_cache_breakpoints(sent))
    payload = json.loads(first["fingerprint_json"])
    assert payload["message_count"] == len(sent)
    assert payload["request_chars"] == sum(len(item["content"]) for item in sent)

    assert second["prefix_divergence_reason"] == DIVERGENCE_APPEND
    assert second["prefix_common_messages"] == len(sent), (
        "第 1 轮发出去的那几条一条不差地是第 2 轮请求的前缀"
    )
    assert second["request_fingerprint"] != first["request_fingerprint"]

    # 三类耗时分开记。`model_call_ms` 每轮都有；取数发生在「模型要了上下文」的那一轮
    # （第 1 轮的记录是取完数才报的）；建索引那一段只在第 1 轮（其余轮是 `None`，
    # **不是 0**）。
    assert first["model_call_ms"] is not None and first["model_call_ms"] >= 0
    assert first["tool_fetch_ms"] is not None, "第 1 轮的模型要了上下文，取数发生在这一轮"
    assert first["index_build_ms"] is not None, "循环之前那一段本地准备要记在第 1 轮上"
    assert second["tool_fetch_ms"] is None, "交结论那一轮没有取数可量"
    assert second["index_build_ms"] is None, "那一段准备只发生一次，后续轮次是 None"

    # 推理 token 接上了上游（**不是 None**，也不是 0）。
    assert first["reasoning_tokens"] == 17
    assert first["reasoning_source"] == "completion_tokens_details.reasoning_tokens"
    assert first["usage_source"] == "prompt_cache_hit_tokens"
    assert outcome.cache_read_tokens == 1200, "两轮各命中 600"


def test_an_upstream_that_reports_nothing_leaves_the_rounds_unreported():
    """上游什么都不报（`ScriptedClient` 那种只给正文的桩）→ 那几格是 `None`，**不是 0**。

    这一条钉的是口径本身：把「没上报」写成 0，界面上就会出现一个确定的
    「这一轮推理 0 tokens」「这一轮没花时间」。
    """
    frames = _Frames()

    _run(ScriptedClient(_final()), on_round=frames)

    diagnostics = frames.items[0].round_diagnostics
    assert diagnostics["reasoning_tokens"] is None
    assert diagnostics["usage_source"] is None, (
        "没读到缓存字段时来源标记是 NULL（不是猜一个字段名，也不是空串）"
    )
    assert diagnostics["reasoning_source"] is None
    # 本地量的那两格仍然是数（它们不依赖上游），而 `tool_fetch_ms` 仍然是 None。
    assert diagnostics["model_call_ms"] is not None
    assert diagnostics["tool_fetch_ms"] is None


def test_a_failed_call_still_reports_the_request_that_failed():
    """调用失败那一轮仍然留一行（`transport_error`）**并且带着那一次请求的指纹**。

    「失败也要留痕」是既有的行为（trace 里最后一行不能是上一轮）；工作包 F 再补一层：
    失败的那一次请求发出去的是什么、花了多久，同样要看得到 —— 否则一次失败在账上
    只有一句错误文本，查不出它当时发的是哪一份提示词（只能看到哈希）。
    """
    client = ScriptedClient(_final(), raise_on=RuntimeError("网关 502"))
    frames = _Frames()

    outcome = _run(client, on_round=frames)

    assert outcome.status == "failed"
    assert [record.status for record in outcome.rounds] == ["transport_error"]
    diagnostics = frames.items[0].round_diagnostics
    assert diagnostics["request_fingerprint"] is not None, "失败的那次请求也要有指纹"
    assert diagnostics["prefix_divergence_reason"] == DIVERGENCE_INITIAL
    assert diagnostics["model_call_ms"] is not None, "失败的那次调用同样花了时间"
    assert diagnostics["reasoning_tokens"] is None, "没拿到响应，就没有上游用量"


# ==========================================================================
#  六、事件账本：帧 → 列（新数据落在工作包 E 的那张表上）
# ==========================================================================


def test_the_event_columns_carry_the_fingerprint_and_keep_none():
    """一帧进度 → 事件列：指纹落到它自己的那几列上，**未算出来的仍是 `None`**。

    只验映射（不碰数据库）：落库那一条由 `tests/test_ai_round_event_ledger.py` 的
    那一套行为用例覆盖（同一个写入口）。
    """
    result = RequestFingerprinter(prompt_version="p1", snapshot_id="s1").before_call(
        [_system(), _seed()]
    )
    columns = event_columns(result)

    assert columns["request_fingerprint"] == result.request_fingerprint
    assert columns["stable_prefix_fingerprint"] == result.stable_prefix_fingerprint
    assert columns["prefix_divergence_reason"] == DIVERGENCE_INITIAL
    assert columns["prefix_common_messages"] is None
    payload = json.loads(columns["fingerprint_json"])
    assert payload["prompt_version"] == "p1"
    assert payload["snapshot_id"] == "s1"
    assert payload["message_count"] == 2


@pytest.mark.parametrize("value", ["", None, "deadbeef"])
def test_the_hash_columns_never_store_an_empty_string(value):
    """空哈希写成 `NULL`（未上报），**不写空串**：空串在库里长得像一个值。"""
    columns = event_columns(RequestFingerprint(request_fingerprint=value or ""))

    assert columns["request_fingerprint"] == (value or None)
