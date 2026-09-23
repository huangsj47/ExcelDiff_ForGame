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

from services.ai.context_tools import DEFAULT_TOOL_LIMITS
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


TRUNCATED_REPORT = "# 变更理解\n\n改了道具表。\n\n# 影响面分析\n\n只影响道具系统。\n"


def _truncated_json(report: str = TRUNCATED_REPORT) -> str:
    """一份**被截断的** JSON：写到 `report_markdown` 中途就断了。

    形状与实测一致（单次输出撞上网关上限）：开头是完整的
    `{"status": "final", "report_markdown": "…`，正文里的换行是 JSON 转义，末尾既没有
    闭合引号也没有 `}`。截到 120 字是刻意的：正文里那两个 `\\n# …` 标题都还在，这样
    它同时满足「像一份报告」，用来钉住「抢正文必须排在数标题之前」。
    """
    escaped = json.dumps(report, ensure_ascii=False)
    return '{"status": "final", "report_markdown": ' + escaped[:120]


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
    """**多轮的核心断言**：模型要的内容必须真的到达它。

    只断言「调用过两次模型」是不够的：那样一个「要了但没回灌」的实现也能通过，
    而它会让模型永远在盲猜。

    ## 2026-09-24（工作包 D 的 P3）：到达的轮次变了，判据没变

    平台现在会**预取**（`evidence_prefetch`）——本批次这几个文件的差异在第 1 轮就附上了，
    所以模型第 1 轮要的这份内容在第 2 轮给的是**指针**（「见上文那一节」，正文不重发，
    见 `context_tools` 第 4 条）。断言因此拆成两半，合起来仍然证明「内容到达了模型」：

    * 正文在**第 1 轮**的消息里（预取那份，带 `evidence_id`）；
    * 第 2 轮明确指出它在哪一节 —— 不重发正文是刻意的（重复一次按未命中价计费）。
    """
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )

    outcome = _run(client)

    assert outcome.rounds_used == 2
    assert outcome.requests_used == 1
    assert "+ 一行改动" in client.calls[0][-1]["content"], "预取来的 diff 没有进第 1 轮"
    assert TABLE in client.calls[0][-1]["content"]
    assert "[已在上文给出]" in client.calls[1][-1]["content"], (
        "重复索要同一份内容时没有给出指针 —— 模型会以为没拿到而反复要"
    )
    assert "### [file_diff]" in client.calls[1][-1]["content"], "指针要指到那一节的标题"
    # 第二轮**不再重发**变更清单，而是给一段指向它的说明。清单本身仍在这次请求里
    # （就是第 1 轮那条消息），所以模型并没有失去它 —— 见下一条用例。
    assert "本次变更共 1 个提交" not in client.calls[1][-1]["content"], (
        "变更清单又被重发了一遍 —— 它是最大的一段，每轮重发既挤窗口又按未命中价计费"
    )
    assert "已经在第 1 轮" in client.calls[1][-1]["content"], "也没有告诉模型清单在哪"


def test_a_rejected_request_is_explained_to_the_model():
    """**被拒的索取必须把原因交回给模型**，不能只留给自己人看。

    ## 这条是实测里最贵的那个坑

    模型在 178 个提交的批次里反复把 `(commit, path)` 配错，平台把请求丢掉，而模型那一轮
    只收到一句「（本轮没有附带任何上下文。）」（`prompt.render_context_items` 在没有条目
    时的那句话）。于是它把「我配错了」写成**「平台取数失败」**，还郑重写进报告的
    **信息缺口**——读者据此去找一个不存在的平台故障。实测那一轮：28 条索取被拒、
    占索取总数的 23%，报告里因此多了一条错误的信息缺口。

    原因一直是算好了的（`sanitize_requests` 的返回值），只是一直只进 trace 给人看。
    """
    other = "b" * 40
    # `commits` 与 `paths` 都要给：`commit_of_path` 只在 `commits` 里找「改过这个文件的
    # 那条提交」——只给 paths 的话它找不到，理由就退化成「不在本批次的任何文件里」，
    # 而模型需要的正是那条可换的提交。
    scope = _scope(
        commits=(COMMIT, other), paths={COMMIT: frozenset({TABLE}), other: frozenset({LUA})}
    )
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": LUA}),
        _final(_anomaly()),
    )

    _run(client, scope=scope)

    second = client.calls[1][-1]["content"]
    assert "没有被执行" in second, "被拒的索取没有告诉模型，它会当成平台取数失败"
    assert other[:12] in second, "没告诉它该换哪条提交 —— 下一轮还会照原样再要一次"
    assert "不等于「那里没有内容」" in second, (
        "没有说清「被拒 ≠ 那里没有内容」；实测里模型正是据此写出一条错误的信息缺口"
    )
    assert "diff of" not in second, "被拒的请求居然被执行了"


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

    ## 为什么断言「配表可以点名工作表」，而不是「配表拿不回来」

    这句话原先写的是「**配表的正文拿不回来**」，那是**半错的**，而且半错的那一半正好把
    模型劝退：配表在**工作表**这一级本来就拿得回来 —— `platform_provider.parse_sheet_window`
    把 `lines` 解释成「第几张工作表」，渲染的抬头自己就写着 `"lines": "<第几张表>"`
    （`tests/test_ai_platform_provider.py` 里那条测试正钉这件事）。而这句话却告诉模型
    「整类配表都没救」，它就不再去要那几张本来点个名就能拿到的表了。

    真正不可续的只是**同一张工作表内部被砍掉的行**（配表没有行坐标，`_read_excel_sheets`
    的 `window` 形参是「第几张表」而不是行区间），所以它必须**降格**说成表内行这一级的
    结论 —— 但**不能删**：不说，模型就会对着一张被砍过的表下结论。三种坐标（行窗口 /
    段号 / 工作表序号）都要在，且各自说清是给哪类内容用的。
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
    # 三种坐标各说各的适用对象：文本 / 代码的行窗口、文档与差异的段号、配表的工作表序号。
    # 这里断言的是 `lines="2"` 而不是 `"lines"`：配表那条给的是**取值**（第几张表），
    # 抬头里那种 `"lines": "<第几张表>"` 的字段写法在引擎这句话里并不出现。
    assert 'lines="2"' in note and "工作表" in note, (
        f"没有说清配表可以按工作表点名（工作表这一级本来拿得回来，"
        f"不说模型就不去要了）：{note}"
    )
    assert 'lines="1200-1600"' in note or "行窗口" in note, f"没有给行窗口那条路：{note}"
    assert 'lines="4-6"' in note or "点名段" in note, f"没有给段号那条路：{note}"
    # 「配表整类拿不回来」这句半错的话会把模型劝退，不许再出现。
    assert "配表的正文拿不回来" not in note, (
        f"又把整类配表判死了（工作表这一级本来点个名就能拿到）：{note}"
    )
    # 但表内被砍的行确实不可续 —— 这件事必须留着说给模型听。
    assert "被砍掉的行" in note and "file_diff" in note, (
        f"没有说清表内被砍掉的行没有坐标、要核对得走 file_diff：{note}"
    )
    # 「只砍了尾巴 / 后面的内容你没看到」是事实，且提醒模型它没看全。
    assert "只砍了尾巴" in note and "没看到" in note, f"没有说清砍的是哪一截：{note}"
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
    """`items_chars` 是**真正进了提示词**的字符数（不是工具取回的原始量）。

    2026-09-24（P3）之后第一轮就有条目了（平台预取），而且**同一份正文只进一次**：
    模型第 2 轮再要时给的是指针 —— 于是第 2 轮那个数**远小于**正文长度，这正是
    「进了提示词的量 ≠ 取回的量」那条判据在本轮的样子。
    """
    provider = FakeProvider({("file_diff", COMMIT, TABLE): "差异" * 500})
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(_anomaly()),
    )
    seen: list[RoundProgress] = []

    _run(client, provider=provider, on_round=seen.append)

    body = len("差异" * 500)
    # 条目文本 = 标题 + 正文（标题那几十个字也算进了提示词，所以取一个区间而不是等号）。
    assert body <= seen[0].items_chars < body + 500, "第 1 轮带的是预取来的正文，一字不少"
    assert "差异" * 10 in client.calls[0][-1]["content"]
    assert 0 < seen[1].items_chars < body, (
        "第 2 轮该是那段指针（很小），不是又一次 1,000 字的正文"
    )
    assert "差异" * 10 not in client.calls[1][-1]["content"], "同一份正文重发了一遍"


def test_the_progress_callback_also_fires_on_the_degraded_rounds():
    """降级那几轮**最需要**进度：用户正等着「到底跑到哪了」。

    漏报这几条的后果不是报错，而是界面上一直停在上一轮 —— 看起来像卡死了。
    """
    seen: list[RoundProgress] = []
    outcome = _run(ScriptedClient(_markdown()), on_round=seen.append)

    assert outcome.degradation == DEGRADE_MARKDOWN
    # 两轮：写成 markdown 的那一轮 + 要求「原样转成 JSON」的那一轮（见上面那条上限）。
    assert [item.status for item in seen] == ["unparsable", "unparsable"]
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
    # 判据落在**那个越权路径**上，不是「有没有执行过任何 file_diff」：P3 之后平台自己会
    # 预取（`evidence_prefetch`），而它只取 scope 里的路径。用后者当判据的话，
    # 预取一上线这条断言就失效（它量的是别的东西），而失效的方向恰好是「漏报越权」。
    assert not [key for key in provider.seen if key[2] == "src/secret/keys.lua"], provider.seen


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
    # P3 之后这份失败说明是**预取**带回来的，落在第 1 轮；第 2 轮给的是那条指针
    # （「见上文那一节」）。要钉的性质没变：取不到这件事**必须说清**，而且不能只说一次
    # 就消失 —— 所以两轮都断言。
    assert "取数失败" in client.calls[0][-1]["content"]
    # 这句「不等于没问题」来自 `context_tools._failure_text`（失败文本自带），而不是
    # `_batch_notes` 那句 `不要据此下结论` —— 后者只在**模型自己索取的那一轮**才拼进去，
    # 而 P3 之后这一次是预取取回来的（模型第 2 轮拿到的是指针）。要守的性质没变：
    # 「取不到」必须与「没问题」分开说，两轮都不能让它消失。
    assert "不代表它没有问题" in client.calls[0][-1]["content"], "没告诉模型取不到不等于没问题"
    assert "[已在上文给出]" in client.calls[1][-1]["content"], "第 2 轮要指回那份说明"


def test_an_empty_result_is_labelled_as_empty_not_as_success():
    """取数成功但没内容（新增文件没有旧版本）必须明说 —— 否则模型会把「空」读成
    「这里没问题」。"""
    provider = FakeProvider({("file_diff", COMMIT, TABLE): ""})
    client = ScriptedClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(),
    )

    _run(client, provider)

    # 同一条性质，落在第 1 轮（P3 起这份「空」是预取带回来的）。
    assert "无内容" in client.calls[0][-1]["content"]
    assert "不要把「无内容」等同于「没有风险」" in client.calls[0][-1]["content"]


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


def test_an_immediate_markdown_answer_burns_at_most_one_extra_call():
    """markdown 只要求转一次格式，**不跟着纠正额度走**。

    把上一条原样转成 JSON 是个确定性很高的动作：第一次没做、第三次更不会做，而每一次
    重问都要把整份提示词重发一遍。所以这条上限是「一次」，与 `max_corrections`（默认 2、
    给截断重发用的）无关 —— 后者放宽时不该顺带把这条也放宽。
    """
    client = ScriptedClient(_markdown())

    outcome = _run(client, limits=EngineLimits(max_corrections=5))

    assert len(client.calls) == 2, f"重问了不止一次：{len(client.calls)} 轮"
    assert outcome.degradation == DEGRADE_MARKDOWN, "两次都是 markdown，仍要按降级收工"


SPLIT_REPORT = (
    "# 变更理解\n\n本次改的是道具表的回收价，回收价从 100 提到 10000，等于商店卖出价，"
    "经济闭环被打破。\n\n# 影响面分析\n\n只影响道具系统，回收价与售价相等，玩家可以无限刷钱。\n"
)


def _split_final(*anomalies, report: str = SPLIT_REPORT) -> str:
    """一份**被切成多段字符串**的 final payload（实测 run 38 的 S2 第 7 轮）。

    正文被写成 `"第一段","第二段"`：段与段之间只有逗号、没有键名，整份 JSON 因此
    不合法 —— 但括号配平、`finish_reason=stop`，`anomalies`/`dimensions` 写在正文
    之后、本来就是好的。每一段都长于 40 字（`_CONTINUATION_CHUNK_MIN`）：短段会被
    当成键名，这个形态就切不起来了。
    """
    escaped = json.dumps(report, ensure_ascii=False)[1:-1]
    head, tail = escaped[: len(escaped) // 2], escaped[len(escaped) // 2 :]
    return (
        '{"status": "final", "report_markdown":'
        '"' + head + '","' + tail + '", '
        '"anomalies": ' + json.dumps([dict(item) for item in anomalies], ensure_ascii=False) + ', '
        '"dimensions": [{"id": "config_id", "hit": ' + ("true" if anomalies else "false")
        + ', "note": ""}]}'
    )


def test_a_split_string_final_is_repaired_in_place_without_a_resend():
    """续写块形态的 final 不再整轮重发：平台把段拼回一个字符串就地解析。

    实测 run 38 的 S2 第 7 轮：这种回答以前被 `looks_like_markdown_report` 判成
    markdown 报告（JSON 字符串里的 `\\n# 变更理解` 照样能数到章节标题），整轮重发
    再要一次「原样转成 JSON」—— 重发回来的正文从 16.5k token 压到 6.7k，
    **内容先丢了一截**。而 anomalies/dimensions 就写在正文后面、本来就是好的。
    """
    client = ScriptedClient(_split_final(_anomaly()))

    outcome = _run(client)

    assert len(client.calls) == 1, "拼接修复不花一轮重发"
    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.degradation == DEGRADE_NONE, "修好的是格式，不是跑完的流程，不许降级"
    assert outcome.rounds_used == 1
    assert "经济闭环被打破" in outcome.report_markdown
    assert "玩家可以无限刷钱" in outcome.report_markdown, "后半段正文必须一字不少"
    assert [item.title for item in outcome.anomalies] == ["【道具】ID 被删除但生成文件仍在"], (
        "正文之后的结构化结论必须原样保留"
    )
    assert "续写块拼接" in outcome.rounds[0].note, "修复必须留痕：读 trace 的人得知道发生过什么"


def test_an_unrepairable_split_still_takes_the_markdown_road():
    """续写块后面的字段也坏了时，修复让路：不堵 markdown 降级那条原有的路。

    差分的关键：同一形态、但 `anomalies` 给了非法类型 —— 拼回后 `json.loads` 通了、
    `_coerce_anomalies` 拒收，修复这条路走不通，必须落回「按 markdown 报告降级重问」
    的既有行为，而不是把这份回答丢掉。
    """
    text = _split_final().replace('"anomalies": []', '"anomalies": 5')
    client = ScriptedClient(text)

    outcome = _run(client)

    assert outcome.degradation == DEGRADE_MARKDOWN, "修复不了时必须回到原有的降级路"
    assert len(client.calls) == 2, "markdown 分支只重问一次格式"


def test_a_failed_repair_leaves_a_note_in_the_round_record():
    """「识别出续写块、拼回后仍失败」必须留痕：与「没识别出续写块」在 trace 上分得开。

    run 40 的 S1 第 6 轮正是这么丢的确诊线索 —— 不留这句话，下次只能再猜一遍。
    """
    text = _split_final().replace('"anomalies": []', '"anomalies": 5')
    client = ScriptedClient(text)

    outcome = _run(client)

    assert any("已尝试续写块拼接" in (round_.note or "") for round_ in outcome.rounds), (
        "失败的修复尝试必须写进那一轮的 note"
    )


RAW_QUOTE_FINAL = (
    '{"status": "final", "report_markdown": '
    '"# 变更理解\\n\\n起服链路里 `require(\\"headcode/LaunchArgs\\").apply()` 未包 pcall。'
    '\\n\\n# 影响面分析\\n\\n只影响起服。", '
    '"anomalies": [], "dimensions": [{"id": "config_id", "hit": false, "note": ""}]}'
)


def test_raw_quotes_in_the_report_are_escaped_in_place_without_a_resend():
    """正文原样引用了带双引号的代码（没做 JSON 转义）→ 平台转义修复，就地解析。

    实测形态（run 41 的汇总第 4 轮，trace 存档了完整原文）：`json.loads` 在体内那个
    引号处认定字符串提前闭合 —— 括号配平、finish_reason=stop，与续写块是**同一个
    漏斗的另一种病**，旧实现同样掉进 markdown 分支整轮重发。而真实闭合引号可以靠
    「正文之后的第一个顶级键」定位，转义后整份 payload 直接可解析。
    """
    broken = RAW_QUOTE_FINAL.replace('require(\\"headcode/LaunchArgs\\").apply()',
                                     'require("headcode/LaunchArgs").apply()')
    assert broken != RAW_QUOTE_FINAL, "fixture 没真的把引号弄坏，这条用例是空转"
    client = ScriptedClient(broken)

    outcome = _run(client)

    assert len(client.calls) == 1, "转义修复不花一轮重发"
    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.degradation == DEGRADE_NONE
    assert 'require("headcode/LaunchArgs").apply()' in outcome.report_markdown, (
        "带引号的代码必须原样保留在正文里"
    )
    assert "裸引号" in outcome.rounds[0].note, "修复必须留痕"


def test_a_truncated_json_is_asked_to_shorten_instead_of_being_dropped():
    """单次输出撞上限时，第一次要**要求压短重发**，而不是就地降级。

    降级只留下正文，结构化结论（人工跟进清单）会整份丢掉 —— 而那份清单正是人工要用的
    东西。所以撞上限要花一轮重问，并把原因说清楚：它以为的「格式错」和真实原因
    「你写太长了」应对方式完全相反，不说清下一轮它还会写这么长。
    """
    client = ScriptedClient(_truncated_json(), _final())

    outcome = _run(client)

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.degradation == DEGRADE_NONE
    sent = [msg["content"] for call in client.calls[1:] for msg in call]
    assert any("被截断" in text and "压缩篇幅" in text for text in sent), (
        "重问那一轮必须把「被截断」与「压短」都说给模型"
    )


def test_a_truncated_answer_without_a_report_still_gets_the_shorten_hint():
    """要上下文的半截回答里没有 `report_markdown`，但它同样是「写太长了」。

    判据如果挂在「抢得到正文」上，这种回答就会被当成「没按协议」，模型收到的是
    「你格式不对」—— 于是它下一轮照样写这么长，再被切一次。实测 run 8 有一轮正是如此。
    """
    client = ScriptedClient(
        '{"status": "need_more_context", "reason": "本轮我在核对 ProtoCGas 的注册顺序',
        _final(),
    )

    outcome = _run(client)

    assert outcome.status == STATUS_SUCCEEDED
    sent = [msg["content"] for call in client.calls[1:] for msg in call]
    assert any("被截断" in text for text in sent), "要按「截断」纠正，而不是按「协议不对」纠正"
    assert not any("不符合协议" in text for text in sent)


def test_a_truncated_json_without_corrections_left_keeps_the_report_not_the_json():
    """纠正额度用完时，留下的是**正文**，不是那坨 JSON 源码。

    实测过（2026-09-21 run 7）：JSON 字符串里的 `\\n# 变更理解` 同样能数到章节标题，
    于是走了「像一份报告」那条路，把 `{"status": "final", …` 原样当成了报告 —— 界面与
    导出拿到的都是 JSON 源码，55k 字的正文全被包在里面。
    """
    client = ScriptedClient(_truncated_json())

    outcome = _run(client, limits=EngineLimits(max_corrections=0))

    assert outcome.status == STATUS_DEGRADED
    assert outcome.degradation == DEGRADE_MARKDOWN
    assert outcome.report_markdown.startswith("# 变更理解"), outcome.report_markdown[:80]
    assert '"status"' not in outcome.report_markdown
    assert outcome.anomalies == (), "截断的 JSON 里那半个数组不许当结论"


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

#: 本组用例把**单条上限**固定成计划推导出来的那一档（`budget_plan.derive_tool_limits`
#: 在 560k 预算下给 30,333）。理由：这两条测的是「条目额度 vs 基线」，而 P3 的预取
#: 有一条**硬份额**判据（`evidence_prefetch`：份额装不下一条就一条都不取）——
#: 默认的 11,000 单条上限会让 200,000 × 12% = 24,000 的份额装得下两条，于是预取
#: 先把这两份 diff 取走，第 2 轮就变成指针而不是正文，测的就不是额度这件事了。
#: 固定成 30,333 之后份额（24,000）装不下一条，预取自动跳过，这条用例回到它本来的问题。
_FAT_TOOL_LIMITS = {**DEFAULT_TOOL_LIMITS, "file_diff": 30_333, "file_content": 30_333}


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
        limits=EngineLimits(
            prompt_char_budget=200_000, baseline_char_budget=0, tool_limits=_FAT_TOOL_LIMITS
        ),
    )
    _run(
        tight_client,
        FakeProvider(contents),
        limits=EngineLimits(
            prompt_char_budget=200_000, baseline_char_budget=196_000, tool_limits=_FAT_TOOL_LIMITS
        ),
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
    # 「同一份内容不重复取数」这条性质现在要在**两本账**上读（P3）：预取取一次（平台
    # 发起，不占模型的额度），模型自己那两次里第二次命中缓存 —— 所以同一个文件一共
    # 只执行 2 次，而**模型侧只执行了 1 次**。只数 `provider.seen` 里 TABLE 的条数
    # 会把平台那次也算进去，断言里的「1」就变成了一句空话（数的不是同一件事）。
    table_runs = [key for key in provider.seen if key[0] == "file_diff" and key[2] == TABLE]
    assert len(table_runs) == 2, "预取一次 + 模型第一次索取一次；第二次必须走缓存"


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


def test_the_last_round_is_told_it_is_the_last_round():
    """**轮次先耗尽的那条路**也要有人明说「这是最后一条消息」。

    只按索取额度判「该收尾了」是不够的：额度还剩着、轮次到顶时，模型完全不知道这是最后
    一次机会 —— 实测 run 10 的 S3 跑满 8 轮（只用了 38/40 次索取）后写了一段 markdown
    叙述而不是协议 JSON，它负责的三个维度（code_logic / version_branch / process）
    **一条结构化结论都没交回来**，报告里只能按「跑完了但结论没交回」标出来。
    """
    client = ScriptedClient(
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _final(),
    )

    _run(client, limits=EngineLimits(max_rounds=2))

    last_user = client.calls[1][-1]["content"]
    assert "最后一条消息" in last_user
    assert "下一轮不存在" in last_user, "没有说清「这一轮索取的东西送不到你手上」这个机制"
    assert "不要写成 markdown 叙述" in last_user
    assert "预算已耗尽" not in last_user, "轮次到顶不是「额度用完」，两者不能混成一句话"


def test_a_middle_round_is_not_told_to_stop_asking():
    """**反自检**：中间几轮不许出现这句 —— 那等于让模型提前放弃索取。

    少要一次上下文，报告就少一块证据，而这种损伤在报告里看不出来（它只会写得更自信）。
    """
    client = ScriptedClient(
        _requests({"type": "commit_detail", "commit": COMMIT}),
        _requests({"type": "file_diff", "path": LUA}),
        _final(),
    )

    _run(client, limits=EngineLimits(max_rounds=3))

    middle_user = client.calls[1][-1]["content"]
    assert "最后一条消息" not in middle_user, "第 2 轮（共 3 轮）就被要求收尾了"
    assert "最后一条消息" in client.calls[2][-1]["content"], "真正的最后一轮反而没说"


def test_a_markdown_answer_is_asked_once_more_for_the_same_content_as_json():
    """模型写成 markdown 时，**先要一次结构，再谈降级**。

    原先这条路是不看额度的直接收工：正文留下了，结构化结论整份放弃 —— 哪怕还剩三轮、
    纠正额度一次没用。实测 run 10 的 S3 就是这么丢掉 code_logic / version_branch /
    process 三个维度的（跑满 8 轮后写成 markdown，那一块维度**一条结构化结论都没进清单**）。

    这里钉住三件事：重问一次、重问的是**同一条回答**（原样转格式）、以及成功之后
    **不再挂着** `markdown_report` 这个降级原因 —— 后者会让 `family_ledger` 写出一句
    「跑完了但结论没有按协议交回」，而结论这一轮刚刚交回来了。
    """
    client = ScriptedClient(_markdown(), _final(_anomaly()))

    outcome = _run(client, limits=EngineLimits(max_rounds=4))

    assert outcome.status == STATUS_SUCCEEDED, outcome.error_message
    assert outcome.degradation == DEGRADE_NONE, (
        f"接到 JSON 之后还挂着「没有结构化结论」的降级原因：{outcome.degradation}"
    )
    assert outcome.payload is not None and outcome.payload.anomalies
    retry_user = client.calls[1][-1]["content"]
    assert "markdown 报告" in retry_user
    assert "原样" in retry_user, "没要求「原样转」，模型会借机重写并丢掉已写实的证据"
    assert "不要新增、不要省略" in retry_user


def test_a_markdown_answer_that_never_becomes_json_keeps_the_fallback_report():
    """**反自检**：重问也没转成 JSON 时，正文必须还在，且降级原因仍是 markdown。

    少了这一条，一个「把 markdown 重问一遍就扔掉」的实现能让上面那条全绿 —— 而那等于
    用一次额外的调用换来一份空报告。
    """
    client = ScriptedClient(_markdown("改了道具表，值被删了。"), _markdown("还是 markdown。"))

    outcome = _run(client, limits=EngineLimits(max_rounds=4))

    assert outcome.status == STATUS_DEGRADED, outcome.status
    assert outcome.degradation == DEGRADE_MARKDOWN, outcome.degradation
    # 留**最后一次**的正文：那是模型最近一次、也是最完整的一次作答。
    assert "还是 markdown" in (outcome.report_markdown or ""), "正文被丢掉了"


def test_the_markdown_retry_is_capped_by_the_correction_budget():
    """重问要占**纠正额度**，否则一个只肯写 markdown 的模型能把轮次全耗在重问上。

    额度用完之后必须落到「留下正文 + 降级」，而不是继续追问到轮次耗尽。
    """
    client = ScriptedClient(_markdown())

    outcome = _run(client, limits=EngineLimits(max_rounds=8, max_corrections=0))

    assert outcome.degradation == DEGRADE_MARKDOWN
    # 纠正额度关掉时连那一次重问都不该发生：1 轮就收工
    assert len(client.calls) == 1, f"纠正额度为 0 却仍然重问了：{len(client.calls)} 轮"


def test_a_markdown_answer_in_the_last_round_still_gets_its_one_retry():
    """**最后一条消息写成 markdown 时，那一次重问必须真的发出去。**

    重问走的是「下一轮」，而轮次正好用尽时 `continue` 就等于「循环到此结束」—— 重问的
    那句话永远发不出去，那一片的结论整份丢掉。实测就是这个形态：一个分片跑满 8 轮、
    最后一条是 markdown，它负责的三个维度一条结构化结论都没交回来。

    所以格式转换可以**借用一轮**（只借一次）：它不取任何上下文，一次调用的事，
    而它换回来的是整片维度能不能进清单。
    """
    client = ScriptedClient(_markdown(), _final(_anomaly()))

    outcome = _run(client, limits=EngineLimits(max_rounds=1))

    assert len(client.calls) == 2, "轮次用尽就放弃重问了 —— 那一次转换调用没发出去"
    assert outcome.degradation == DEGRADE_NONE, outcome.degradation
    assert [item.title for item in outcome.anomalies], "转成 JSON 之后结论应当留下"


def test_the_borrowed_round_is_reported_as_the_last_round():
    """借来的那一轮要对模型报成「第 N/N 轮」，不能出现「第 9/8 轮」。

    提示词里的轮次是给模型看的（它据此判断还有没有机会），一个自相矛盾的编号会让它
    以为后面还有轮次 —— 而那一轮之后分析就结束了。
    """
    client = ScriptedClient(_markdown(), _final(_anomaly()))

    _run(client, limits=EngineLimits(max_rounds=1))

    retry_user = client.calls[1][-1]["content"]
    assert "第 2/2 轮" in retry_user, f"轮次编号自相矛盾：{retry_user[:200]!r}"
    assert "9/8" not in retry_user


def test_a_second_markdown_answer_does_not_borrow_another_round():
    """借轮**只借一次**：再问一次还是 markdown 就收工，不能无限借下去。"""
    client = ScriptedClient(_markdown(), _markdown())

    outcome = _run(client, limits=EngineLimits(max_rounds=1))

    assert len(client.calls) == 2, f"借轮没有收敛：发了 {len(client.calls)} 次"
    assert outcome.degradation == DEGRADE_MARKDOWN
    assert outcome.report_markdown, "正文被丢掉了"
