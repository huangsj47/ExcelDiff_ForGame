# -*- coding: utf-8 -*-
"""多轮编排。

这个模块是「配置界面」与「真结论」之间唯一的连接件，所以测试的重点不是「函数返回了
什么」，而是**几条只有在真实多轮里才会出问题的性质**：

1. 模型索要的上下文真被执行、真回灌到下一轮（否则「多轮」是假的）；
2. 越权/不存在的路径要在执行前被挡掉（`sanitize_requests`），且**不能因此作废整轮**；
3. 轮次/额度耗尽、模型不吐 JSON —— 三种退化都必须**留下痕迹**，不能悄悄返回一个
   「成功」；
4. 提示词预算按「除条目之外还剩多少」给条目，而不是拿总预算当条目额度。
"""
from __future__ import annotations

import json
from pathlib import Path

from services.ai.engine import (
    DEGRADE_MARKDOWN,
    DEGRADE_NONE,
    DEGRADE_PROTOCOL,
    DEGRADE_REQUESTS,
    DEGRADE_ROUNDS,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineLimits,
    run_analysis,
)
from services.ai.llm_client import ChatResult
from services.ai.rules import RuleThresholds
from services.ai.scope import AnalysisScope
from services.ai.skill_loader import LoadedSkills, SkillDocument

COMMIT = "a" * 40
COMMIT_B = "b" * 40
TABLE = "config/[30]道具表_CfgItem.xlsx"
LUA = "build/lua/CfgItem.lua"


# --------------------------------------------------------------------------
# 假件：一个可按脚本回答的模型，一个会记账的取数口
# --------------------------------------------------------------------------


class ScriptedClient:
    """按脚本回答。脚本用完后一直重复最后一条。

    记录每一次收到的 messages，测试据此断言「上下文有没有真的回灌」。
    """

    def __init__(self, *replies: str, raise_on: Exception | None = None):
        self._replies = list(replies)
        self._raise_on = raise_on
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, *, temperature=None):
        self.calls.append([dict(item) for item in messages])
        if self._raise_on is not None:
            raise self._raise_on
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return ChatResult(text=self._replies[index], model="fake", prompt_tokens=10, completion_tokens=5)

    @property
    def last_messages(self) -> list[dict[str, str]]:
        return self.calls[-1]

    @property
    def last_user(self) -> str:
        return self.last_messages[-1]["content"]

    def all_user_text(self) -> str:
        return "\n".join(
            item["content"] for call in self.calls for item in call if item["role"] == "user"
        )


class FakeProvider:
    """取数口。默认所有路径都有内容，用 `contents` 精确控制某一条。"""

    def __init__(self, contents: dict | None = None, *, failing: bool = False):
        self.contents = dict(contents or {})
        self.failing = failing
        self.seen: list[tuple] = []

    def _lookup(self, key, default):
        self.seen.append(key)
        if self.failing:
            raise RuntimeError("git 超时")
        return self.contents.get(key, default)

    def commit_detail(self, commit):
        return self._lookup(("commit_detail", commit), f"提交 {commit} 的详情：改了道具表")

    def file_diff(self, commit, path):
        return self._lookup(("file_diff", commit, path), f"diff of {path} at {commit}\n+ 一行改动")

    def file_content(self, commit, path):
        return self._lookup(("file_content", commit, path), f"{path} 的完整内容")

    def read_reference(self, name):
        return self._lookup(("read_reference", name), f"{name} 的正文")


def _loaded() -> LoadedSkills:
    def doc(name: str, text: str, description: str = "说明") -> SkillDocument:
        return SkillDocument(
            name=name,
            description=description,
            path=Path("/tmp") / name,
            text=text,
            content_hash="hash-" + name,
        )

    return LoadedSkills(
        platform_skill=doc("version-diff-review", "# 角色与方法\n\n你是配表评审专家。\n"),
        platform_references=(doc("incident-checklist.md", "事故清单"),),
        project_manifest=doc("g119-knowledge", "# G119\n"),
        project_references=(doc("config-table-spec.md", "配表规范"),),
        project_skills=(),
        readable={"incident-checklist.md": Path("/tmp/a.md"), "config-table-spec.md": Path("/tmp/b.md")},
        project_slug="g119",
        revision="rev-1",
    )


def _scope(*, commits=(COMMIT,), paths=None) -> AnalysisScope:
    mapping = paths if paths is not None else {COMMIT: frozenset({TABLE, LUA})}
    return AnalysisScope(
        commits=tuple(commits),
        paths_by_commit={key: frozenset(value) for key, value in mapping.items()},
        readable_references=frozenset({"incident-checklist.md", "config-table-spec.md"}),
    )


def _requests(*types: str) -> str:
    """构造一轮「我要上下文」的回答。"""
    payload = {
        "status": "need_more_context",
        "requests": [dict(item) for item in types],
    }
    return json.dumps(payload, ensure_ascii=False)


def _markdown(body: str = "改了道具表。") -> str:
    """一份「像报告」的纯文本回答。

    判据是命中的约定章节标题数（至少 2 个，见 `protocol.REPORT_HEALTH_MIN_SECTIONS`），
    所以这里必须写够两个标题 —— 只写一个的话它不算报告，引擎会走重问而不是降级。
    """
    return f"# 变更理解\n\n{body}\n\n# 影响面分析\n\n只影响道具系统。\n"


def _final(*anomalies, report="# 变更理解\n\n改了道具表。\n") -> str:
    payload = {
        "status": "final",
        "report_markdown": report,
        "anomalies": [dict(item) for item in anomalies],
        "dimensions": [{"id": "config_id", "hit": bool(anomalies), "note": ""}],
    }
    return json.dumps(payload, ensure_ascii=False)


def _anomaly(**overrides) -> dict:
    base = {
        "title": "【道具】ID 被删除但生成文件仍在",
        "category": "config_id",
        "severity": "critical",
        "confidence": "high",
        "evidence": [f"{TABLE} 删除了 ID 1001", f"{LUA} 里 1001 还在"],
        "commit": COMMIT,
        "file_path": TABLE,
        "impact": "老存档引用的道具失效",
        "suggestion": "确认是否有意下线，并同步生成文件",
    }
    base.update(overrides)
    return base


def _run(client, provider=None, **overrides):
    kwargs = {
        "client": client,
        "provider": provider or FakeProvider(),
        "loaded": _loaded(),
        "scope": _scope(),
        "change_summary": "本次变更共 1 个提交、2 个文件。\n",
        "thresholds": RuleThresholds(),
    }
    kwargs.update(overrides)
    return run_analysis(**kwargs)


# ==========================================================================
# 正常路径
# ==========================================================================


def test_a_single_round_final_returns_the_report_and_anomalies():
    client = ScriptedClient(_final(_anomaly()))

    outcome = _run(client)

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.degradation == DEGRADE_NONE
    assert outcome.report_markdown.startswith("# 变更理解")
    assert [item.title for item in outcome.anomalies] == ["【道具】ID 被删除但生成文件仍在"]
    assert outcome.rounds_used == 1
    assert outcome.prompt_tokens == 10 and outcome.completion_tokens == 5


def test_the_model_can_ask_for_context_and_gets_it_back():
    """**多轮的核心断言**：第二轮的消息里必须有第一轮要来的内容。

    只断言「调用过两次模型」是不够的：那样一个「要了但没回灌」的实现也能通过，
    而它会让模型永远在盲猜。
    """
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )

    outcome = _run(client)

    assert outcome.rounds_used == 2
    assert outcome.requests_used == 1
    assert "本次变更共 1 个提交" in client.calls[1][-1]["content"], "第二轮没带变更清单"
    assert "+ 一行改动" in client.calls[1][-1]["content"], "要来的 diff 没有回灌"
    assert TABLE in client.calls[1][-1]["content"]


def test_the_change_summary_is_present_in_every_round():
    """多轮的对话历史里第一轮就带过摘要，但每轮的 user 消息仍要重新给 —— 长对话里
    模型对最早那条的注意力最弱，而它正是判断的基准。"""
    client = ScriptedClient(
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _final(_anomaly()),
    )

    _run(client)

    for call in client.calls:
        assert "本次变更共 1 个提交" in call[-1]["content"]


def test_the_baseline_is_carried_into_the_first_round():
    """增量分析的接线处：基线要进提示词，否则模型会把上一轮报过的问题再报一遍。"""
    client = ScriptedClient(_final())
    digest = "# 这个版本截至上次分析已经报过的问题\n共 1 条：仍待处理 1。\n- [high] 旧问题 #deadbeef\n"

    _run(client, baseline_digest=digest)

    assert "旧问题" in client.calls[0][-1]["content"]


# ==========================================================================
# 请求白名单
# ==========================================================================


def test_a_request_for_a_path_outside_the_change_set_is_not_executed():
    """模型要一个本次没改、也不可读的文件时，**不能去执行**。

    执行了就等于绕过白名单：模型可以顺着仓库结构把无关内容读进来，而报告里会出现
    「基于某文件」的结论，用户却不知道那份文件根本不该被看。
    """
    provider = FakeProvider()
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": "src/secret/keys.lua"}),
        _final(),
    )

    outcome = _run(client, provider)

    assert outcome.requests_used == 0, "越权路径被执行了"
    assert all(key[0] != "file_diff" for key in provider.seen)


def test_a_partially_valid_batch_still_executes_the_valid_ones():
    """一条越权不该拖累其余合法请求 —— 那会让模型失去它本可以拿到的证据。"""
    provider = FakeProvider()
    client = ScriptedClient(
        _requests(
            {"type": "file_diff", "commit": COMMIT, "path": "src/secret/keys.lua"},
            {"type": "file_diff", "commit": COMMIT, "path": TABLE},
        ),
        _final(),
    )

    outcome = _run(client, provider)

    assert outcome.requests_used == 1
    assert ("file_diff", COMMIT, TABLE) in provider.seen


# ==========================================================================
# 取数失败
# ==========================================================================


def test_a_failing_tool_does_not_kill_the_round():
    """取数炸了（仓库没检出、git 超时）只该让那一条变成「内容不可用」。

    让整轮失败的话，一次网络抖动就会让整个版本分析没有结论。
    """
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )

    outcome = _run(client, FakeProvider(failing=True))

    assert outcome.status == STATUS_SUCCEEDED
    assert "取数失败" in client.calls[1][-1]["content"]
    assert "不要据此下结论" in client.calls[1][-1]["content"], "没告诉模型取不到不等于没问题"


def test_an_empty_result_is_labelled_as_empty_not_as_success():
    """取数成功但没内容（新增文件没有旧版本）必须明说 —— 否则模型会把「空」读成
    「这里没问题」。"""
    provider = FakeProvider({("file_diff", COMMIT, TABLE): ""})
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(),
    )

    _run(client, provider)

    assert "无内容" in client.calls[1][-1]["content"]


# ==========================================================================
# 退化：每一种都要留下痕迹
# ==========================================================================


def test_running_out_of_rounds_is_reported_rather_than_hidden():
    """模型一直要上下文要不够时，轮次会耗尽。

    这时**不能**返回一个干净的「成功」：用户会以为模型看完了说没问题，而实际上它
    从头到尾都在要文件。
    """
    client = ScriptedClient(_requests({"type": "commit_detail", "commit": COMMIT}))

    outcome = _run(client, limits=EngineLimits(max_rounds=3))

    assert outcome.status == STATUS_FAILED
    assert outcome.degradation == DEGRADE_ROUNDS
    assert "轮次用尽" in outcome.error_message
    assert outcome.rounds_used == 3


def test_exhausting_the_request_budget_marks_the_run_degraded():
    """额度用尽后模型仍给出了结论 —— 有产出，但它是「没看全」的结论。"""
    client = ScriptedClient(
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _final(_anomaly()),
    )

    outcome = _run(client, limits=EngineLimits(max_tool_requests=1))

    assert outcome.status == STATUS_DEGRADED
    assert outcome.degradation == DEGRADE_REQUESTS
    assert outcome.usable, "有报告就该留下"


def test_the_last_round_is_told_the_budget_is_gone():
    """额度没了要**明说**，而且要说到位：只说「请给结论」不够，还得禁止再索取 ——
    否则模型会把最后一轮花在又一次请求上，而那次请求会被直接拒绝。"""
    client = ScriptedClient(
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _final(),
    )

    _run(client, limits=EngineLimits(max_tool_requests=1))

    last_user = client.calls[1][-1]["content"]
    assert "预算已耗尽" in last_user
    assert "禁止继续请求上下文" in last_user


def test_a_markdown_report_is_kept_instead_of_thrown_away():
    """模型没按协议包 JSON、直接给了报告正文时，那份正文通常是有用的。

    丢掉它重问，既浪费一次调用，也可能再问一次还是这样。所以保留为降级产出，并
    明确标记「这不是协议输出」。
    """
    client = ScriptedClient(_markdown("改了道具表，风险中等。"))

    outcome = _run(client)

    assert outcome.status == STATUS_DEGRADED
    assert outcome.degradation == DEGRADE_MARKDOWN
    assert "改了道具表" in outcome.report_markdown
    assert outcome.anomalies == (), "降级路径不该凭空造出异常条目"


def test_an_immediate_markdown_answer_does_not_burn_another_call():
    client = ScriptedClient(_markdown())

    _run(client)

    assert len(client.calls) == 1


def test_a_report_shaped_last_round_is_salvaged_after_the_rounds_run_out():
    """跑完轮次、最后一轮给的是报告正文而不是 JSON —— 模型显然想交付结论，留下它。"""
    client = ScriptedClient(
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _markdown("回归范围建议覆盖背包。"),
    )

    outcome = _run(client, limits=EngineLimits(max_rounds=2))

    assert outcome.status == STATUS_DEGRADED
    assert outcome.degradation == DEGRADE_MARKDOWN
    assert "回归范围" in outcome.report_markdown


def test_repeatedly_unparsable_answers_give_up_and_say_so():
    """一直不吐 JSON 时不能无限重问。

    重问要**占一轮**：否则一个坚决不配合的模型会把循环变成无限次调用，账单和等待都
    没有上限。
    """
    client = ScriptedClient("我觉得这个改动还行吧，没什么大问题。")

    outcome = _run(client, limits=EngineLimits(max_rounds=8, max_corrections=2))

    assert outcome.status == STATUS_FAILED
    assert outcome.degradation == DEGRADE_PROTOCOL
    assert outcome.rounds_used <= 8
    assert outcome.rounds_used == 3, "初答 + 两次重问，第三次就给不出机会了"


def test_a_correction_hint_is_sent_on_the_retry():
    client = ScriptedClient("这不是 JSON", _final())

    _run(client)

    assert "上一轮的问题" in client.calls[1][-1]["content"]


def test_a_transport_failure_becomes_a_failed_result_not_an_exception():
    """调用模型失败（超时、鉴权、连接断）必须变成结果，不能往上抛。

    调用方在后台线程或 SSE 生成器里跑，抛出去只会变成一个没人看的堆栈，而用户看到的是
    「分析中」永远转下去。
    """
    client = ScriptedClient(_final(), raise_on=RuntimeError("connection reset"))

    outcome = _run(client)

    assert outcome.status == STATUS_FAILED
    assert "connection reset" in outcome.error_message
    assert "RuntimeError" in outcome.error_message


# ==========================================================================
# 门槛与归一化
# ==========================================================================


def test_an_anomaly_below_the_confidence_bar_never_reaches_the_list():
    """**这是精度优先的落点**：模型报了一条 `medium` 置信度的东西，无论它写得多像，
    都不该出现在给人工跟进的清单里。"""
    client = ScriptedClient(
        _final(_anomaly(confidence="medium"), _anomaly(title="【道具】另一条", confidence="high"))
    )

    outcome = _run(client)

    assert [item.title for item in outcome.anomalies] == ["【道具】另一条"]
    assert any(item.reason for item in outcome.dropped), "丢掉的原因要记账"


def test_an_anomaly_pointing_at_an_unknown_commit_is_dropped_and_recorded():
    """模型编一个不存在的提交号时，条目要被丢掉**并且记账** —— 「为什么这次少了一条」
    必须能追溯。"""
    client = ScriptedClient(_final(_anomaly(commit="f" * 40)))

    outcome = _run(client)

    assert outcome.anomalies == ()
    assert outcome.dropped


def test_the_cap_keeps_the_most_severe_ones():
    """超限时不能按顺序砍：模型输出顺序没有保证，砍掉后 N 条完全可能把唯一的
    `critical` 砍掉，只留一堆 `high`。"""
    anomalies = [_anomaly(title=f"【道具】high {index}", severity="high") for index in range(12)]
    anomalies.append(_anomaly(title="【道具】唯一的 critical", severity="critical"))
    client = ScriptedClient(_final(*anomalies))

    outcome = _run(client, thresholds=RuleThresholds(max_anomalies=3))

    assert len(outcome.anomalies) == 3
    assert outcome.anomalies[0].title == "【道具】唯一的 critical"


# ==========================================================================
# 预算
# ==========================================================================


def test_a_big_baseline_squeezes_the_context_items():
    """**「条目按剩余额度给」的直接后果**：基线摘要变长，能给的上下文就变少。

    两个数字在同一份预算里，一个涨另一个就得让。所以这里用「同一批请求、只改基线预算」
    对照着测：宽松时两份 diff 都在，紧张时至少有一份进不来（或被压掉）。
    把这条钉住，是为了让「把基线预算调大」这个动作的代价可见 —— 否则调完只会看到
    「模型看的东西变少了」，却不知道为什么。

    注意不能只断言「消息里有『截断』字样」：单条上限（`DEFAULT_TOOL_LIMITS`）自己就会
    截断长内容并留下这个字样，于是断言在任何预算逻辑下都成立 —— 第一版就是这么写的，
    变异测试里完全测不出把 `residual` 换成总预算这个改动。
    """
    contents = {
        ("file_diff", COMMIT, TABLE): "甲" * 30_000,
        ("file_diff", COMMIT, LUA): "乙" * 30_000,
    }
    reply = _requests(
        {"type": "file_diff", "commit": COMMIT, "path": TABLE},
        {"type": "file_diff", "commit": COMMIT, "path": LUA},
    )

    roomy_client = ScriptedClient(reply, _final())
    tight_client = ScriptedClient(reply, _final())

    roomy = _run(
        roomy_client,
        FakeProvider(contents),
        limits=EngineLimits(prompt_char_budget=200_000, baseline_char_budget=0),
    )
    _run(
        tight_client,
        FakeProvider(contents),
        limits=EngineLimits(prompt_char_budget=200_000, baseline_char_budget=196_000),
    )

    assert roomy.status == STATUS_SUCCEEDED
    roomy_user = roomy_client.calls[1][-1]["content"]
    tight_user = tight_client.calls[1][-1]["content"]

    assert "甲" in roomy_user and "乙" in roomy_user, "宽松时两份 diff 都该在"
    assert len(tight_user) < len(roomy_user), (
        "基线占掉 196,000 字后，条目该被压下来；两条消息一样长说明基线没算进额度里"
    )
    # 压缩记账只在预算真的卡住时才出现 —— 这是区分「算了基线的额度」与「按总预算给」
    # 的那条信号（后者 22,000 字的条目在 200,000 里根本不会触发压缩）。
    assert "为控制预算，部分上下文已被压缩" in tight_user
    assert "为控制预算，部分上下文已被压缩" not in roomy_user


def test_a_tiny_budget_is_disclosed_rather_than_silently_applied():
    big = "x" * 60_000
    provider = FakeProvider({("file_diff", COMMIT, TABLE): big})
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(),
    )

    _run(client, provider, limits=EngineLimits(prompt_char_budget=8_000, baseline_char_budget=4_000))

    assert "留给上下文的额度" in client.calls[1][-1]["content"]


def test_the_cache_short_circuit_still_counts_against_the_budget():
    """重复要同一个文件省下的是我们的耗时，不是模型的额度。计入才能逼它收敛。"""
    provider = FakeProvider()
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(),
    )

    outcome = _run(client, provider)

    assert outcome.requests_used == 2, "重复索要没有计入额度"
    assert outcome.cache_hits == 1, "第二次应当命中缓存"
    assert len([key for key in provider.seen if key[0] == "file_diff" and key[2] == TABLE]) == 1


# ==========================================================================
# 结果结构与配置
# ==========================================================================


def test_to_dict_is_json_safe():
    outcome = _run(ScriptedClient(_final(_anomaly())))
    json.dumps(outcome.to_dict(), ensure_ascii=False)


def test_the_round_trace_explains_where_the_rounds_went():
    client = ScriptedClient(
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _final(_anomaly()),
    )

    payload = _run(client).to_dict()

    assert [item["status"] for item in payload["rounds"]] == ["requests", "final"]
    assert payload["rounds"][0]["request_count"] == 1


def test_engine_limits_take_the_values_the_project_configured():
    limits = EngineLimits.from_config({"max_rounds": 3, "max_tool_requests": 5})

    assert limits.max_rounds == 3
    assert limits.max_tool_requests == 5
    # 没配的仍旧是默认值，不会因为「配置里出现了别的键」被清零
    assert limits.prompt_char_budget == EngineLimits().prompt_char_budget


def test_engine_limits_ignore_unknown_keys_and_none_values():
    """未知键与 None 都要忽略。

    None 尤其要忽略：配置行是懒创建的，没填过的列读出来就是 None，直接塞进
    `EngineLimits(**payload)` 会把 `max_rounds=None` 变成 `None`，然后在
    `range(1, None + 1)` 上炸 —— 而且是在深夜里、自动轮询的时候炸。
    """
    limits = EngineLimits.from_config({"max_rounds": None, "编造的字段": 99})

    assert limits.max_rounds == EngineLimits().max_rounds
    assert limits == EngineLimits()


def test_anomaly_thresholds_are_a_separate_knob_from_the_engine_limits():
    """**额度与门槛是两套配置**，别混在一处。

    额度（几轮、要几次、多少字符）是「这次能花多少」，门槛（严重度、置信度、最多报
    几条）是「什么样的问题才配上榜」。它们的默认值分别来自 `engine` 与 `rules`，
    改一处不该动另一处。
    """
    thresholds = RuleThresholds.from_config(None)
    limits = EngineLimits()

    assert not hasattr(limits, "max_anomalies"), (
        "门槛不该出现在 EngineLimits 里：那会让「调大额度」顺带改变上榜标准"
    )
    assert thresholds.max_anomalies > 0
