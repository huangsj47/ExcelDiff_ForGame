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
