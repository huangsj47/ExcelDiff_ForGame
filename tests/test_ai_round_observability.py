# -*- coding: utf-8 -*-
"""工作包 D 的 P5：逐轮**可观测性**字段（可见正文 / 推理 token / 角色 / 重放 / 原因）。

## 这一组钉的是哪一句话

run 45/46 的性能归因里有一句没被证过的话：`completion_tokens` 里有**多少是可见正文**。
`response_text` 是按 `TRACE_RESPONSE_MAX_CHARS` 截断后的原文，量不出「模型可见正文多长」
（里面裹着 `think` 与 JSON 转义），于是「输出 token 涨了 3 倍」既可能是写得更长、
也可能是想得更久 —— 而这两种情况该调的东西完全相反。

## 口径只有一条：**未上报存 `None`，不存 0**

0 是一个确定的观测值（「确实没有」），`None` 是「上游没报 / 读不出来」。把它们合并，
界面上就会出现一个确定的「输入 0 tokens」，而真相是一次调用根本没跑成。
"""
from __future__ import annotations

import json
from pathlib import Path

from services.ai.engine import (
    CORRECTION_TRUNCATED_OUTPUT,
    CORRECTION_UNPARSABLE,
    RoundRecord,
    _replay_chars,
    _truncation_reason,
    _visible_response_chars,
    _with_observability,
    run_analysis,
)
from services.ai.rules import RuleThresholds
from services.ai.request_fingerprint import DIVERGENCE_REASONS
from services.ai.scope import AnalysisScope
from services.ai.skill_loader import LoadedSkills, SkillDocument

COMMIT = "a" * 40
LUA = "scripts/net/proto.lua"


# ---------------------------------------------------------------------------
#  纯函数
# ---------------------------------------------------------------------------


def test_visible_chars_strips_the_think_block():
    """`think` 块**不产生可见正文**，所以它不算进这一格。"""
    text = "<think>先想 400 个字……</think>结论：没有问题。"

    assert _visible_response_chars(text) == len("结论：没有问题。")


def test_visible_chars_is_none_when_there_is_no_response():
    """没有响应文本 → `None`（**不是 0**）：一次没跑成的调用与一次没写东西的调用不同。"""
    assert _visible_response_chars("") is None
    assert _visible_response_chars(None) is None


def test_only_a_think_block_means_zero_visible_chars():
    """整段只有思考块 → 可见正文确实是 **0**（这是一个观测值，不是「未上报」）。"""
    assert _visible_response_chars("<think>全是思考</think>") == 0


def test_the_truncation_reason_names_the_constraint():
    """截断原因取条目上的 `truncated_by`（**是哪条约束砍的**，不是「被砍过」）。"""
    from services.ai.budget import ContextItem

    batch = type(
        "Batch",
        (),
        {
            "items": (
                ContextItem(kind="file_content", label="a", text="x",
                            meta={"truncated": True, "truncated_by": "tool_limit_file_content"}),
                ContextItem(kind="file_content", label="b", text="x",
                            meta={"truncated": True, "truncated_by": "tool_limit_file_content"}),
                ContextItem(kind="file_diff", label="c", text="x",
                            meta={"truncated": True, "truncated_by": "item_shrink_level_1"}),
                ContextItem(kind="file_diff", label="d", text="x", meta={}),
            )
        },
    )()

    assert _truncation_reason(batch) == "tool_limit_file_content、item_shrink_level_1"


def test_no_truncation_is_an_empty_reason_not_a_placeholder():
    """没被截断时是**空串**（不是「未知」之类的话）：这一格进的是「原因」列。"""
    batch = type("Batch", (), {"items": ()})()

    assert _truncation_reason(batch) == ""


def test_replay_chars_reads_the_cross_member_counter():
    """重放字符只取 `cross_member_replayed_chars`（**跨成员**那一项）。"""
    class Tools:
        stats = {
            "file_diff": {"cross_member_replayed_chars": 300, "produced_chars": 9_000},
            "file_content": {"cross_member_replayed_chars": 120, "produced_chars": 500},
        }

    assert _replay_chars(Tools()) == 420


def test_replay_chars_is_zero_when_the_stats_cannot_be_read():
    class Broken:
        @property
        def stats(self):
            raise RuntimeError("统计坏了")

    assert _replay_chars(Broken()) == 0


def test_the_observability_block_only_adds_keys():
    """合并进逐轮明细时**只加键**：共享口径那一份（`trace_evidence`）逐字不动。"""
    record = RoundRecord(
        index=3, status="requests", visible_response_chars=1_234, reasoning_tokens=None,
        tool_replay_chars=88, truncation_reason="tool_limit_file_content",
        correction_reason="",
    )

    entry = _with_observability(record)

    assert entry["visible_response_chars"] == 1_234
    assert entry["reasoning_tokens"] is None, "未上报存 None，不存 0"
    assert entry["tool_replay_chars"] == 88
    assert entry["truncation_reason"] == "tool_limit_file_content"
    assert entry["correction_reason"] == ""
    # 共享口径的键仍在（键名与 `ai_usage_service.run_usage()` 逐字对齐的那一份）。
    for key in ("round_index", "tokens_input", "tokens_output", "response_text"):
        assert key in entry, key


# ---------------------------------------------------------------------------
#  跑一次真引擎：逐轮字段真的被填上了
# ---------------------------------------------------------------------------


class _ScriptedClient:
    def __init__(self, *replies: str):
        from services.ai.llm_client import ChatResult

        self._replies = list(replies)
        self._result = ChatResult
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, temperature=None):
        self.calls.append([dict(item) for item in messages])
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return self._result(
            text=self._replies[index], model="fake", prompt_tokens=10, completion_tokens=5,
        )


def _loaded() -> LoadedSkills:
    def doc(name: str, text: str) -> SkillDocument:
        return SkillDocument(name=name, description="说明", path=Path("/tmp") / name,
                             text=text, content_hash="h-" + name)

    return LoadedSkills(
        platform_skill=doc("version-diff-review", "# 角色与方法\n\n你是配表评审专家。\n"),
        platform_references=(doc("incident-checklist.md", "事故清单"),),
        project_manifest=doc("g119-knowledge", "# G119\n"),
        project_references=(), project_skills=(),
        readable={"incident-checklist.md": Path("/tmp/a.md")},
        project_slug="g119", revision="rev-1",
    )


def _scope() -> AnalysisScope:
    return AnalysisScope(
        commits=(COMMIT,), paths_by_commit={COMMIT: frozenset({LUA})},
        readable_references=frozenset({"incident-checklist.md"}),
    )


def test_a_round_records_visible_chars_and_the_reason_it_was_retried():
    """跑两轮：第一轮输出被截断（要重问），第二轮交结论。

    断言逐轮账里有「可见正文长度」与「这一轮为什么被纠正」—— 它们合起来才能回答
    「这次的输出 token 花在哪、白花了哪一轮」。
    """
    truncated = '{"status": "final", "report_markdown": "写了一半就断了'
    final = json.dumps(
        {
            "status": "final",
            "report_markdown": "# 变更理解\n\n改了协议。\n",
            "anomalies": [],
            "dimensions": [{"id": "module_coupling", "hit": False, "note": ""}],
        },
        ensure_ascii=False,
    )
    client = _ScriptedClient(truncated, final)

    outcome = run_analysis(
        client=client,
        provider=_FakeProvider(),
        loaded=_loaded(),
        scope=_scope(),
        change_summary="本次变更共 1 个提交、1 个文件。\n",
        thresholds=RuleThresholds(),
    )

    first, second = outcome.rounds[0], outcome.rounds[1]
    assert first.status == "unparsable"
    assert first.correction_reason == CORRECTION_TRUNCATED_OUTPUT, (
        "这一轮为什么被重问：截断（不是「格式不对」）—— 两者的应对相反"
    )
    assert first.visible_response_chars == len(truncated)
    assert first.reasoning_tokens is None, "上游没提供就必须是 None（不是 0）"
    assert second.status == "final"
    assert second.correction_reason == "", "交结论那一轮没有被纠正"
    assert second.visible_response_chars == len(final)

    # 逐轮明细里也带着这几个字段（落库那一份 `entry_json` 就是 dump 它）。
    entry = _with_observability(second)
    assert entry["visible_response_chars"] == len(final)


def test_the_protocol_correction_reason_is_named():
    """连续两轮不吐 JSON → 纠正原因是 `unparsable_json`，而不是一句自由文本。"""
    client = _ScriptedClient("我觉得这个改动没什么问题。", "还是这句话。")
    final = json.dumps(
        {
            "status": "final",
            "report_markdown": "# 变更理解\n\n没问题。\n",
            "anomalies": [],
            "dimensions": [],
        },
        ensure_ascii=False,
    )
    client._replies = ["我觉得这个改动没什么问题。", final]

    outcome = run_analysis(
        client=client, provider=_FakeProvider(), loaded=_loaded(), scope=_scope(),
        change_summary="本次变更共 1 个提交、1 个文件。\n", thresholds=RuleThresholds(),
    )

    assert outcome.rounds[0].correction_reason == CORRECTION_UNPARSABLE
    assert outcome.rounds[0].correction_hint, "纠正提示也要留着（那份才是发给模型的）"


class _FakeProvider:
    """取数口：一律给一句内容（这一组测的是用量记账，不是取数）。"""

    def commit_detail(self, commit):
        return f"提交 {commit} 的详情"

    def file_diff(self, commit, path):
        return f"diff of {path}"

    def file_content(self, commit, path, lines=""):
        return f"{path} 的正文"

    def read_reference(self, name):
        return f"{name} 的正文"

    def find_references(self, query, path=""):
        return f"{query} 命中 1 处"


# ---------------------------------------------------------------------------
#  诊断值真的走到了进度帧上（缓存诊断的接线证明）
#
#  这一组补的是「**接线了不等于被用了**」那个坑：`RoundDiagnostics` 在引擎里有 9 个
#  调用点，任何一处漏调都不会报错 —— 字段有默认值，那一轮在界面上只是永远空着。
#  所以要跑一次真引擎、从进度回调里把帧捞出来看。
#
#  诊断值挂在 `RoundProgress`（**进度帧**）而不是返回值的 `RoundRecord` 上，这是有意的：
#  逐轮事件账本读的就是进度帧（`round_events.record` ← `run_progress.publish`），
#  所以拿 `outcome.rounds[i]` 去断言这一组会 AttributeError —— 那不是 bug，是走错了门。
# ---------------------------------------------------------------------------


def _diagnostics_frames(client):
    """跑一次两轮的分析，返回每个进度帧上的诊断字典。"""
    truncated = '{"status": "final", "report_markdown": "写了一半就断了'
    final = json.dumps(
        {
            "status": "final",
            "report_markdown": "# 变更理解\n\n改了协议。\n",
            "anomalies": [],
            "dimensions": [{"id": "module_coupling", "hit": False, "note": ""}],
        },
        ensure_ascii=False,
    )
    client._replies = [truncated, final]
    frames: list = []
    run_analysis(
        client=client, provider=_FakeProvider(), loaded=_loaded(), scope=_scope(),
        change_summary="本次变更共 1 个提交、1 个文件。\n", thresholds=RuleThresholds(),
        on_round=frames.append,
    )
    assert len(frames) == 2, "这一组要的是两轮：第一轮截断、第二轮交结论"
    return frames, client


def test_the_first_round_reports_initial_and_no_previous_request():
    """第一次调用**没有可比的上一请求** → 公共前缀是 `None`，不是 `0`。

    「公共前缀 0 条」是一个真实观测值（上一次请求与这一次第一条就不同），而第一次调用
    根本没有上一次 —— 两者在界面上都显示成 0 的话，「前缀从第一条就被改写」这个信号
    就永远查不出来（它会被满屏的第一次调用淹没）。
    """
    client = _ScriptedClient()
    frames, _ = _diagnostics_frames(client)
    first = frames[0].round_diagnostics

    assert first["prefix_divergence_reason"] == "initial"
    assert first["prefix_common_messages"] is None
    assert first["prefix_common_chars"] is None
    # 16 个十六进制字符的短哈希（用途是本机对照，不是密码学，见 DIGEST_CHARS）。
    assert len(first["request_fingerprint"]) == 16
    assert all(c in "0123456789abcdef" for c in first["request_fingerprint"])


def test_the_second_round_is_an_append_over_the_same_stable_prefix():
    """第二轮是上一轮的**严格延长** → `append`，且稳定前缀哈希**一个字符都没变**。

    后半句才是重点：跨运行复用缓存靠的就是「稳定前缀哈希相等」这一条
    （`RequestFingerprint` 的 docstring 里写明了它与 `prefix_common_*` 分工不同）。
    如果纠正轮重拼了系统提示词，这个哈希就会变 —— 而那正是「平台自己重写了前缀」
    这一类要去看代码的情形。
    """
    client = _ScriptedClient()
    frames, client = _diagnostics_frames(client)
    first, second = frames[0].round_diagnostics, frames[1].round_diagnostics

    assert second["prefix_divergence_reason"] == "append"
    assert second["prefix_common_messages"] == len(client.calls[0]), (
        "公共前缀的条数应当等于上一次请求的条数（这一次把它整个接了下去）"
    )
    assert second["prefix_common_chars"] > 0
    assert second["stable_prefix_fingerprint"] == first["stable_prefix_fingerprint"], (
        "稳定前缀（系统提示词 + 首轮变更清单）跨轮必须逐字节不变"
    )
    assert second["request_fingerprint"] != first["request_fingerprint"], (
        "整请求哈希当然要变 —— 它变而稳定前缀不变，正是 append 的定义"
    )


def test_the_local_preparation_time_is_recorded_once_on_the_first_round():
    """「拼系统提示词 / 预取」那一段只发生一次，所以只记在第 1 轮。

    其余轮记 `None`（**不是 0**）：0 会让逐轮图看起来像「每一轮都花了 0 秒准备」，
    而真相是那一段根本不属于后面的轮次。
    """
    client = _ScriptedClient()
    frames, _ = _diagnostics_frames(client)

    assert frames[0].round_diagnostics["index_build_ms"] is not None
    assert frames[1].round_diagnostics["index_build_ms"] is None


def test_an_unreported_reasoning_count_stays_none_on_the_progress_frame():
    """上游没报推理 token → 帧上是 `None`，**不是 0**。

    `_ScriptedClient` 就是一个不报这个字段的上游（它只给 prompt/completion 两个数）。
    写成 0 的话，用量面板会显示一个确定的「推理 0 tokens」—— 而真相是这次调用压根
    没测到，两者对「能不能下调单次输出上限」这个决定的意义完全相反。
    """
    client = _ScriptedClient()
    frames, _ = _diagnostics_frames(client)

    for frame in frames:
        diagnostics = frame.round_diagnostics
        assert diagnostics["reasoning_tokens"] is None
        assert diagnostics["usage_source"] is None
        assert diagnostics["reasoning_source"] is None


def test_every_recorded_divergence_reason_is_on_the_whitelist():
    """落库的分叉原因必须取自 `DIVERGENCE_REASONS` 那份白名单。

    这一列在界面上是给人拿去**筛**的（「哪几次是 `other`」就是要去看代码的那一类），
    出现一个白名单外的自由文本，那个筛选框就会静默地少掉几行。
    """
    client = _ScriptedClient()
    frames, _ = _diagnostics_frames(client)

    for frame in frames:
        assert frame.round_diagnostics["prefix_divergence_reason"] in DIVERGENCE_REASONS
