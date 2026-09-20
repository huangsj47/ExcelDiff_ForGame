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
from dataclasses import replace
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
    RoundProgress,
    run_analysis,
)
from services.ai.llm_client import ChatResult
from services.ai.prompt_cache import (
    CACHE_BREAKPOINT_KEY,
    MAX_CACHE_BREAKPOINTS,
    strip_cache_breakpoints,
)
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

    def file_content(self, commit, path, lines=""):
        # `lines` 是 `file_content` 的可选行窗口（协议的一部分，见 services/ai/protocol.py）。
        # 这个桩忽略它（不关心给的是哪一段），但**形参不能少** —— 少了就会 TypeError，
        # 而取数失败是被接住的：表现不是报错，是「这一轮什么都没拿到」，测试静默退化。
        return self._lookup(("file_content", commit, path), f"{path} 的完整内容")

    def read_reference(self, name):
        return self._lookup(("read_reference", name), f"{name} 的正文")

    def find_references(self, query, path=""):
        # 协议里的第五个门（`find_references`）。桩忽略范围，但**形参不能少**：少了就是
        # TypeError，而取数失败是被接住的 —— 表现不是报错，是「这一轮什么都没拿到」。
        return self._lookup(("find_references", query), f"{query} 命中 1 处：a.lua:12")


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
    assert "+ 一行改动" in client.calls[1][-1]["content"], "要来的 diff 没有回灌"
    assert TABLE in client.calls[1][-1]["content"]
    # 第二轮**不再重发**变更清单，而是给一段指向它的说明。清单本身仍在这次请求里
    # （就是第 1 轮那条消息），所以模型并没有失去它 —— 见下一条用例。
    assert "本次变更共 1 个提交" not in client.calls[1][-1]["content"], (
        "变更清单又被重发了一遍 —— 它是最大的一段，每轮重发既挤窗口又按未命中价计费"
    )
    assert "已经在第 1 轮" in client.calls[1][-1]["content"], "也没有告诉模型清单在哪"


def test_the_change_summary_is_sent_once_and_still_in_context_afterwards():
    """变更清单**只发一次**，但它一直在模型的上下文里，并且后续轮次必须被提醒回去看它。

    ## 这条用例原来的样子与为什么改

    原来它叫 `test_the_change_summary_is_present_in_every_round`，断言每一轮的 user
    消息里都重新出现整份清单，理由是「长对话里模型对最早那条的注意力最弱，而它正是判断
    的基准」。那个顾虑是真的，但付的代价太大：清单顶到上限时约 87,000 字，8 轮下来单这
    一段就 700,000 字 —— 比整个提示词预算还大，而窗口是硬约束，撑爆的结果是整次分析被
    上游拒绝（`budget.compact_history` 与引擎里的收尾路径就是为它准备的）。而且它是每轮
    **新追加**的内容，落在缓存断点之后，每一轮都按**未命中**价计费。

    改成「发一次 + 后续轮次给指针」之后，两件事都必须成立，所以这里两条都钉：
    清单仍然在请求里（第 1 轮那条消息没有被压掉），以及后续轮次说了「它在哪、它仍然有效」。
    """
    client = ScriptedClient(
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _final(_anomaly()),
    )

    _run(client)

    # 第一条消息就是第 1 轮的 user 消息：清单**始终**在这次请求里，只是不再重复。
    for call in client.calls:
        assert "本次变更共 1 个提交" in call[1]["content"], "变更清单从上下文里消失了"
    assert "本次变更共 1 个提交" in client.calls[0][-1]["content"], "第一轮没有给清单"
    for call in client.calls[1:]:
        assert "本次变更共 1 个提交" not in call[-1]["content"]
        assert "仍然是你的判断基准" in call[-1]["content"], (
            "没有提醒模型「清单仍然有效」—— 那正是原来那条用例担心的事"
        )


def test_the_baseline_is_carried_into_the_first_round():
    """增量分析的接线处：基线要进提示词，否则模型会把上一轮报过的问题再报一遍。"""
    client = ScriptedClient(_final())
    digest = "# 这个版本截至上次分析已经报过的问题\n共 1 条：仍待处理 1。\n- [high] 旧问题 #deadbeef\n"

    _run(client, baseline_digest=digest)

    assert "旧问题" in client.calls[0][-1]["content"]


# ==========================================================================
# 提示词缓存：请求必须是 append-extension，断点必须落在稳定前缀上
# ==========================================================================


def _sent(messages) -> list[dict]:
    """**真正会发出去**的那份消息。

    引擎在消息字典上留了内部断点标记（见 `prompt_cache.CACHE_BREAKPOINT_KEY`），而那个
    标记会在轮次之间**挪位置**（第 3 轮时它从第 2 轮的 user 消息挪到第 3 轮的）。
    标记不影响发出去的字节（客户端会摘掉），所以比较「请求是不是 append-extension」
    必须比这份摘干净的版本 —— 直接比引擎手里的列表会被标记的挪动误判成「前缀变了」。
    """
    return strip_cache_breakpoints(messages)


def test_every_request_is_an_append_extension_of_its_predecessor():
    """**多轮命中 prompt cache 的全部前提**，也是本文件里最该被钉住的一条。

    上游按**逐字节前缀**匹配缓存：只要第 N+1 轮的请求以第 N 轮的请求为前缀，第 N 轮
    写过的那段缓存就能被读到。这不是「顺手写成这样」—— 重新排序、重建 system、
    把上下文插回中间，任何一处都会让第 2 轮及以后的命中归零，而**归零是静默的**：
    分析照样跑完，只是每一轮都按未命中价计费。

    deepseek-harness 的 `request-reconstruction.spec.ts` 用同一条断言把这个纪律钉在
    编排层（它那边叫 `expectPrefixExtension`）。
    """
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _final(_anomaly()),
    )

    _run(client)

    assert len(client.calls) == 3
    for previous, current in zip(client.calls, client.calls[1:]):
        earlier, later = _sent(previous), _sent(current)
        assert len(later) > len(earlier), "每一轮都要**追加**消息"
        assert later[: len(earlier)] == earlier, (
            "第 N+1 轮的请求不是第 N 轮的前缀 —— 缓存从这一刻起再也命中不了"
        )


def test_the_cache_breakpoints_sit_on_the_stable_prefixes():
    """断点只落在**跨轮次不变**的那几段末尾。

    三个断点：系统消息末尾（平台 skill + 项目知识，跨运行逐字节相同）、第一轮 user
    消息末尾（变更清单 + 基线 + 首轮指引，一批变更里恒定）、当前这一轮 user 消息末尾
    （让下一轮把整份历史当作已有前缀读命中）。

    **总数必须 ≤ 4**：Anthropic 的硬上限是 4，超了直接 400 —— 而 400 会触发
    「去掉标记重试」并把整个功能关掉（见 `llm_client.complete` 的兜底）。
    """
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _requests({"type": "file_diff", "commit": COMMIT, "path": LUA}),
        _final(_anomaly()),
    )

    _run(client)

    last = client.calls[-1]
    marked = [index for index, item in enumerate(last) if item.get(CACHE_BREAKPOINT_KEY)]
    assert marked == [0, 1, 5], (
        f"断点位置变了：{marked}。应当是「系统消息」「第一轮 user」「最新一轮 user」"
    )
    assert len(marked) <= MAX_CACHE_BREAKPOINTS


def test_the_moving_breakpoint_leaves_the_previous_round(monkeypatch):
    """断点跟着最新一轮走：上一轮的 user 消息上不再留标记。

    留着的代价是每轮重写一遍缓存（那一轮的内容之后不会再变，但断点在旧位置等于让上游
    去缓存一段「已经缓存过」的前缀），而且会更快撞上 4 个断点的上限。
    """
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _requests({"type": "file_diff", "commit": COMMIT, "path": LUA}),
        _final(_anomaly()),
    )

    _run(client)

    # 第 3 轮的对话里：u2 是第 4 条（索引 3），它上面的断点应当已经挪到 u3（索引 5）上。
    third = client.calls[-1]
    assert not third[3].get(CACHE_BREAKPOINT_KEY), "断点没有从上一轮挪走"
    assert third[5].get(CACHE_BREAKPOINT_KEY)


def test_a_single_round_run_only_marks_the_two_stable_blocks():
    """只跑一轮时不该凭空多出一个断点（那时「最新一轮」就是第一轮）。"""
    client = ScriptedClient(_final(_anomaly()))

    _run(client)

    marked = [index for index, item in enumerate(client.calls[0]) if item.get(CACHE_BREAKPOINT_KEY)]
    assert marked == [0, 1]


def test_the_breakpoint_marks_never_change_the_message_content():
    """挪断点不改内容：标记是**消息字典上的一个内部键**，不是内容的一部分。

    内容一旦被断点改动（例如给消息加一段「缓存提示」），append-extension 就断了 ——
    这条用例把那件事钉死。
    """
    client = ScriptedClient(_final(_anomaly()))

    _run(client, on_round=None)

    system = client.calls[0][0]
    assert set(system) == {"role", "content", CACHE_BREAKPOINT_KEY}
    assert system["content"].startswith("本协议由平台强制注入")


# ==========================================================================
# 逐轮进度回调（on_round）
# ==========================================================================


def test_a_truncated_item_is_named_and_the_way_back_is_spelled_out():
    """「有 1 条被截断」**必须点名是哪一条，并说清怎么把剩下的拿回来**。

    线上的一次真实核对逼出来的：面板上写着「有 1 条上下文因长度上限被截断」，而那一轮
    要了两样东西（一份规格文档 + 一张配表）—— 模型和人**都不知道是哪一条被砍了**，
    也不知道下一步做什么。旁边那两条说明（取数失败 / 预算省略）都是既点名又给动作的，
    只有这一条两个都没有，而它说的事情（你看的内容少了一截）比那两条更需要行动。

    两句话都要在：**文本 / 代码可以点名行窗口拿回来，配表拿不回来**（它按行渲染、
    没有行窗口）。这段文本看不到内容形态（配表与代码在引擎这一层长得一样），所以
    两种都给 —— 猜错的方向很坏：把一份规格文档说成「配表，拿不回来」，模型就不再要了，
    而它本来带个 `lines` 就能拿到。
    """
    long_text = "行" * 20_000  # 超过单条上限（11,000 字）
    provider = FakeProvider({("file_content", COMMIT, TABLE): long_text})
    client = ScriptedClient(
        _requests({"type": "file_content", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )

    outcome = _run(client, provider=provider)

    notes = [note for record in outcome.rounds for note in record.budget_notes]
    note = next((item for item in notes if "被截断" in item), "")
    assert note, f"被截断了却没有说明：{notes}"
    assert TABLE in note, f"没有点名是哪一条被截断：{note}"
    assert "重新索取" in note or "点名" in note, f"没有说怎么拿回来：{note}"
    assert "配表" in note and "拿不回来" in note, (
        f"没有说清配表那一条补不回来（而它正是会诱使模型白白再要一次的那条）：{note}"
    )
    assert "11,000" in note or "上限" in note, f"没有说明是哪道上限：{note}"


def test_the_progress_callback_fires_once_per_round_in_order():
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )
    seen: list[RoundProgress] = []

    outcome = _run(client, on_round=seen.append)

    assert [item.index for item in seen] == [1, 2]
    assert [item.status for item in seen] == ["requests", "final"]
    assert all(item.max_rounds == EngineLimits().max_rounds for item in seen)
    # 用**默认额度**算剩余，不写死数字：额度是个会被调整的配置（见
    # `models.ai_analysis.project_config.DEFAULT_MAX_TOOL_REQUESTS` 的注释），
    # 写死之后每调一次默认值都要来改一次这个与额度无关的用例。
    assert seen[0].requests_used == 1
    assert seen[0].requests_remaining == EngineLimits().max_tool_requests - 1
    assert seen[1].requests_used == 1
    # token 是**本次运行的累计值**（RoundRecord 里的才是本轮值）。
    assert [item.prompt_tokens for item in seen] == [10, 20]
    assert [item.completion_tokens for item in seen] == [5, 10]
    # 时间只增不减。
    assert seen[0].elapsed_ms <= seen[1].elapsed_ms
    assert outcome.rounds_used == 2


def test_the_progress_callback_reports_the_context_that_went_into_the_prompt():
    """`items_chars` 是**真正进了提示词**的字符数（不是工具取回的原始量）。"""
    provider = FakeProvider({("file_diff", COMMIT, TABLE): "差异" * 500})
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )
    seen: list[RoundProgress] = []

    _run(client, provider=provider, on_round=seen.append)

    assert seen[0].items_chars == 0, "第一轮还没有任何上下文"
    assert seen[1].items_chars == len("差异" * 500)
    assert "差异" * 10 in client.calls[1][-1]["content"]


def test_the_progress_callback_also_fires_on_the_degraded_rounds():
    """降级那几轮**最需要**进度：用户正等着「到底跑到哪了」。

    漏报这几条的后果不是报错，而是界面上一直停在上一轮 —— 看起来像卡死了。
    """
    seen: list[RoundProgress] = []
    outcome = _run(ScriptedClient(_markdown()), on_round=seen.append)

    assert outcome.degradation == DEGRADE_MARKDOWN
    assert [item.status for item in seen] == ["unparsable"]
    assert seen[0].index == 1


def test_a_broken_progress_callback_never_fails_the_analysis():
    """回调抛异常只记一条日志。调用方用它发 SSE 事件、查预算 —— 那些动作失败不该让
    一次已经跑了几分钟的分析白跑（与 `run_analysis` 不抛异常同一条理由）。
    """
    def explode(_progress):
        raise RuntimeError("SSE 通道断了")

    outcome = _run(ScriptedClient(_final(_anomaly())), on_round=explode)

    assert outcome.status == STATUS_SUCCEEDED
    assert [item.title for item in outcome.anomalies] == ["【道具】ID 被删除但生成文件仍在"]


def test_the_cache_totals_are_none_until_every_round_reports_them():
    """逐轮上报的缓存 token：**任一轮没上报，累计值就是 `None`**（不是 0）。

    与 `EngineOutcome.cache_read_tokens` 同一条口径（见 `_sum_optional`）：拿「手里有的
    那几轮」去加会得出一个偏高的命中率，而那个数字看起来完全正常、没人会去怀疑它。
    """
    class CacheReporting(ScriptedClient):
        """只报某些轮：`reported_rounds` 之外的那几轮不带缓存字段。"""

        def __init__(self, *replies, reported_rounds=(1, 2)):
            super().__init__(*replies)
            self._reported = set(reported_rounds)

        def complete(self, messages, *, temperature=None):
            result = super().complete(messages, temperature=temperature)
            if len(self.calls) not in self._reported:
                return result  # 上游这一轮没报缓存字段
            return replace(
                result, cache_read_tokens=900, cache_write_tokens=0, cache_source="x"
            )

    replies = (
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )

    partial: list[RoundProgress] = []
    _run(CacheReporting(*replies, reported_rounds=(2,)), on_round=partial.append)

    assert partial[0].cache_read_tokens is None
    assert partial[1].cache_read_tokens is None, "有一轮没上报，累计值就不能给数字"

    complete: list[RoundProgress] = []
    _run(CacheReporting(*replies), on_round=complete.append)

    assert complete[0].cache_read_tokens == 900
    assert complete[1].cache_read_tokens == 1800
    assert complete[1].cache_write_tokens == 0


def test_the_round_records_carry_the_per_round_cache_usage():
    """**逐轮**的账才是回答「缓存有没有用」的那个数（总量只有一个数字）。

    这一条同时是「同一个 run 里第 2 轮及以后应当命中」这条验收的落点：真实端点上
    `cache_read_tokens` 在第一次之后的每一次都必须 > 0（见
    `test_ai_live_endpoint.py` 里那条对着真模型跑的用例）。
    """
    class CacheReporting(ScriptedClient):
        def complete(self, messages, *, temperature=None):
            result = super().complete(messages, temperature=temperature)
            return replace(
                result,
                cache_read_tokens=100 * (len(self.calls) - 1),
                cache_write_tokens=7,
                cache_source="prompt_cache_hit_tokens",
            )

    client = CacheReporting(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )

    outcome = _run(client)

    assert [item.cache_read_tokens for item in outcome.rounds] == [0, 100]
    assert outcome.cache_read_tokens == 100
    assert outcome.cache_write_tokens == 14
    assert outcome.cache_source == "prompt_cache_hit_tokens"
    assert all(item.prompt_tokens == 10 for item in outcome.rounds), "逐轮记的是本轮值"


def test_the_round_records_carry_the_evidence_not_just_the_counts():
    """逐轮要留下**证据**：模型说了什么、要了什么、拿到的是内容还是「取不到」。

    只记计数的话，「这次为什么没读到 X」在面板上无从查起 —— 模型没要、取数失败、
    被预算拒了，三种可能在三列计数上长得一模一样（见 `services/ai/trace_evidence.py`）。
    """
    provider = FakeProvider(contents={("file_diff", COMMIT, LUA): None})
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )

    outcome = _run(client, provider=provider)

    first = outcome.rounds[0]
    assert first.status == "requests"
    # 它点了名
    assert [getattr(req, "path", "") for req in first.requests] == [TABLE]
    # 它拿到了什么 —— 以及拿到的那一份是不是内容
    assert len(first.executed) == 1
    assert first.executed[0].text, "取到的正文必须挂在这一轮上"
    # 模型原样返回了什么（协议解释不了时要靠它）
    assert "need_more_context" in first.response_text
    # 结论那一轮也不例外
    assert "final" in outcome.rounds[1].response_text
    assert outcome.rounds[1].executed == (), "结论轮没有再取数"


def test_a_round_that_never_reached_the_model_is_still_recorded():
    """这一轮**没跑成**（上游拒绝）也要留一行。

    不留的话，trace 里最后一行是上一轮，面板上「这次分析为什么失败」看起来就是
    「跑了两轮、什么都没说」—— 而上游拒了什么只有一个地方写着（`error_message`）。
    """
    client = ScriptedClient(raise_on=RuntimeError("connection reset"))

    outcome = _run(client)

    assert outcome.status == STATUS_FAILED
    assert [item.status for item in outcome.rounds] == ["transport_error"], outcome.rounds
    assert "connection reset" in outcome.rounds[0].note


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


def test_the_used_up_round_is_not_told_the_whole_run_had_zero_requests():
    """**线上真实发生过**：额度用光的那一轮读到的是「本次分析**总共**可索取 0 次
    上下文」，模型把这句原样抄进了报告的「信息缺口」，用户拿着它来问「平台为什么只给了
    0 次额度」—— 而那次运行的额度本来是够的，只是被这个分片花完了。

    所以每一轮的额度那句话必须给出**三个数**：总额 / 已用 / 还剩。只给剩余，等于把
    剩余说成总额。
    """
    client = ScriptedClient(
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _final(),
    )

    _run(client, limits=EngineLimits(max_tool_requests=1))

    first_user = client.calls[0][-1]["content"]
    last_user = client.calls[1][-1]["content"]
    # 第 1 轮：还没花，那句话与从前逐字相同（子代理模式的共享前缀靠它保持缓存命中）。
    assert "本次分析总共可索取 1 次上下文" in first_user
    # 第 2 轮：花掉了，必须说清「总共 1 次、已经用掉 1 次、还能再索取 0 次」。
    assert "总共可索取 1 次" in last_user
    assert "已经用掉 1 次" in last_user
    assert "还能再索取 0 次" in last_user
    assert "总共可索取 0 次" not in last_user, "这就是模型抄进报告的那句话"


def test_a_project_configured_with_no_request_budget_says_so_instead_of_claiming_exhaustion():
    """上限配成 0 是**配置**，不是「用完了」：报告里要能看出该去改配置，而不是去查
    额度怎么被花掉的（那是查不出来的）。"""
    client = ScriptedClient(_final())

    _run(client, limits=EngineLimits(max_tool_requests=0))

    first_user = client.calls[0][-1]["content"]
    assert "不允许索取上下文" in first_user
    assert "上限是 0 次" in first_user
    assert "预算已耗尽" not in first_user, "0 次额度不是「耗尽」，它从来没被给过"


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


class HalfReporting(ScriptedClient):
    """前几轮报 usage，之后不再报（真实网关会这样：某些轮次就是不带 usage）。"""

    def __init__(self, *replies, report_rounds: int = 1):
        super().__init__(*replies)
        self._report_rounds = report_rounds

    def complete(self, messages, *, temperature=None):
        result = super().complete(messages, temperature=temperature)
        if len(self.calls) > self._report_rounds:
            return replace(result, prompt_tokens=None, completion_tokens=None)
        return result


def test_one_round_that_does_not_report_makes_the_whole_total_unknown():
    """**只要有一轮没上报，整次就是 `None`** —— 不许只把报了的那几轮加起来。

    这条以前是 `prompt_tokens += usage[...]`，读不到的那一轮直接按 0 加进去，
    于是「上游没报」变成「确实没花」。而那个偏小的数看起来完全正常，没有人会去怀疑它 ——
    代价是费用那一栏算出一个确定的 `¥0.00`（见 `pricing.estimate_cost`：它只为 `None`
    留了「无法估算」这条路）。
    """
    client = HalfReporting(_requests(), _final(_anomaly()), report_rounds=1)

    outcome = _run(client)

    assert len(outcome.rounds) >= 2, "前提：这次跑了不止一轮，才有「有一轮没报」可言"
    assert [item.prompt_tokens for item in outcome.rounds][0] == 10, "第一轮报了"
    assert [item.prompt_tokens for item in outcome.rounds][1] is None, "第二轮没报"
    assert outcome.prompt_tokens is None, (
        "有一轮没上报，总账却给了一个数 —— 那个数偏小却看起来正常"
    )
    assert outcome.completion_tokens is None


def test_all_rounds_reporting_still_adds_up():
    """**前提**：每一轮都报的时候，总账照样是相加的结果（别把口径改成一律 None）。"""
    client = ScriptedClient(_requests(), _final(_anomaly()))

    outcome = _run(client)

    assert len(outcome.rounds) == 2
    assert outcome.prompt_tokens == 20, "两轮各 10，总账必须是 20"
    assert outcome.completion_tokens == 10


def test_the_requests_that_never_got_a_turn_are_named():
    """额度用尽时，**没轮到的那几个请求要能被点出来**。

    原先只留了一个计数（`refused_by_budget`），而它只说明「有几条没轮到」，
    说明不了「缺的是哪几块」—— 用户看到「还有文件没看」时的第二个问题一定是「哪些」。
    这个清单一路走到结果里（`context.request_budget.refused_items`），抽屉据此展开。

    这里要的是**引擎侧的累计**：被拒的请求发生在哪一轮，就要从哪一轮记上，
    且不能把「执行成功的」也算进来。
    """
    client = ScriptedClient(
        _requests(
            {"type": "file_content", "commit": COMMIT, "path": TABLE},
            {"type": "file_content", "commit": COMMIT, "path": LUA},
            {"type": "find_references", "query": "CfgRewardMode"},
        ),
        _final(_anomaly()),
    )

    outcome = _run(client, limits=EngineLimits(max_tool_requests=1))

    assert outcome.degradation == DEGRADE_REQUESTS
    assert outcome.refused_requests == (LUA, "引用扫描 CfgRewardMode"), (
        "没轮到的请求没被点名，或者把执行成功的那一个也算进来了"
    )


def test_a_run_inside_its_budget_names_nothing():
    """没超预算时是空元组 —— 抽屉据此决定要不要展开那段「没轮到的包括…」。"""
    client = ScriptedClient(
        _requests({"type": "file_content", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )

    outcome = _run(client, limits=EngineLimits(max_tool_requests=8))

    assert outcome.refused_requests == ()


def test_the_start_callback_fires_before_the_first_model_call():
    """**「开始了」这一帧必须在第一次模型调用之前发出去。**

    `on_round` 只在 `client.complete` 返回之后才发，所以从开跑到第一轮跑完之间
    （读配置、拼上下文，再叠上整整一次模型调用 —— 可以是几分钟）界面一帧进度都收不到，
    只能显示「分析中：进度不可用」，看起来像卡住了。这一帧就是给那段空白用的。

    「在最前面」这件事**只能这样验**：让回调在被调用时去读 client 的调用记录 —— 那时
    必须还是 0 次。只看「回调被调过」的写法对它是不是第一件事毫无约束。
    """
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )
    calls_when_started: list[int] = []

    _run(client, on_start=lambda _progress: calls_when_started.append(len(client.calls)))

    assert calls_when_started == [0], (
        f"开始那一帧不是在第一次模型调用之前发的（当时已有 {calls_when_started} 次调用）"
    )


def test_the_start_frame_does_not_invent_a_round_or_a_zero():
    """开始那一帧说的是「还没有任何一轮跑完」，不是「第 0 轮、花了 0 个 token」。

    0 是一个结论（一次都没跑、一个 token 都没花），而这里的事实是**还不知道** ——
    同 `live_tokens` 的口径（`None` 而不是 0）。
    """
    seen: list[RoundProgress] = []

    _run(ScriptedClient(_final(_anomaly())), on_start=seen.append)

    assert len(seen) == 1
    start = seen[0]
    assert start.index == 0, "开始帧不该占用轮次编号（轮次从 1 开始数）"
    assert start.prompt_tokens is None and start.completion_tokens is None, (
        "还没调用过模型却报了一个 token 数"
    )
    assert start.requests_used == 0
    assert start.requests_remaining == EngineLimits().max_tool_requests
    assert start.round_entry is None, "开始帧不该带一条逐轮明细（那一轮还没发生）"


def test_the_start_frame_is_not_a_round():
    """它**不是一轮**：不许进 `outcome.rounds`，也不许把 `requests_used` 算进去。

    进 rounds 的后果不是报错，而是 trace 里凭空多出一轮「什么都没干」的记录 ——
    读 trace 的人会以为模型跑了一轮却没要任何上下文。
    """
    seen: list[RoundProgress] = []

    outcome = _run(
        ScriptedClient(
            _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
            _final(_anomaly()),
        ),
        on_start=seen.append,
    )

    assert len(seen) == 1, "开始帧被当成一轮重复报了"
    assert [item.index for item in outcome.rounds] == [1, 2], (
        "开始帧混进了逐轮记录里"
    )


def test_a_failing_start_callback_does_not_kill_the_analysis():
    """与 `on_round` 同一条纪律：**给界面看的东西坏了，不能作废一次要花钱的分析。**"""
    def explode(_progress):
        raise RuntimeError("SSE 通道断了")

    outcome = _run(ScriptedClient(_final(_anomaly())), on_start=explode)

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.usable
