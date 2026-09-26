# -*- coding: utf-8 -*-
"""引擎层：「输出通道错位」的修复、独立纠正额度、以及**受循环上界约束**。

三件事分开钉：

1. **能抽的那一种不花轮次** —— 真实样本（run 44 的 S4）里本来就带着四条合法请求，
   平台抽出来就地执行，那一轮照常进 trace 的 `requests` 档，**不重发、不降级**。
2. **抽不出的那一种换一本账** —— 真实样本（run 44 的 S2，纯思考）仍要重问，但那一次
   从**独立的**额度里扣：一个已经撞过两次截断的分片不该因为这一种病整片阵亡。
3. **换账不等于无限重试** —— 重问照样占一轮，循环上界原样管着它。

样本取自 `tests/fixtures/dsml_tool_envelopes/`
（`_dump_from_trace.py` 从 `ai_analysis_trace` 二进制导出，见那份脚本的说明）；
本文**不粘贴**那段原文。
"""
from __future__ import annotations

import os

from services.ai.engine import (
    DEGRADE_NONE,
    DEGRADE_PROTOCOL,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineLimits,
)
from services.ai.protocol import parse_payload, repair_dsml_tool_calls_payload
from services.ai.scope import AnalysisScope
from tests.test_ai_engine import (
    TABLE,
    FakeProvider,
    ScriptedClient,
    _final,
    _markdown,
    _requests,
    _run,
)

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "dsml_tool_envelopes"
)
S4_SAMPLE = "run44_s4_file_diff_and_evidence.txt"
S2_SAMPLE = "run44_s2_think_only.txt"


def _sample(name: str) -> str:
    """真实原文，二进制读（理由见模块文档）。"""
    with open(os.path.join(FIXTURE_DIR, name), "rb") as handle:
        return handle.read().decode("utf-8")


def _scope_covering(name: str) -> AnalysisScope:
    """按样本里**真实出现**的 commit/path 建一个能放行它们的 scope。

    为什么不把那些 id 写死在用例里：那份 fixture 是逐字导出的原文，写死 id 等于在测试
    里再存一份它的内容 —— 两边一旦不同步，用例会静默退化成「全都因为批次外被丢掉」，
    而断言照样绿。这里从修复结果里现取，用的是**被测函数之外**的那一份事实（原文）。
    """
    payload = parse_payload(repair_dsml_tool_calls_payload(_sample(name)))
    paths_by_commit: dict[str, list[str]] = {}
    for request in payload.requests:
        if request.commit:
            paths_by_commit.setdefault(request.commit, []).append(request.path)
    return AnalysisScope.from_iterables(
        commits=tuple(paths_by_commit),
        paths_by_commit=paths_by_commit,
        readable_references=(),
    )


# ==========================================================================
# 一、能抽出来的：就地修复，不重发
# ==========================================================================


def test_a_real_envelope_with_requests_is_recovered_without_a_resend():
    """run 44 的 S4：那一轮本来就带着三条能执行的 `file_diff`，平台抽出来执行。

    旧行为是「返回无法解析」→ 重问（1 轮 + 1 次纠正额度），而重问回来的内容通常更短。
    现在这一轮直接进 `requests` 档，`note` 里写明是平台从信封里抽的。
    """
    client = ScriptedClient(_sample(S4_SAMPLE), _final())

    outcome = _run(client, scope=_scope_covering(S4_SAMPLE))

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.degradation == DEGRADE_NONE, "修好的是输出形态，不是跑完的流程"
    first = outcome.rounds[0]
    assert first.status == "requests", "这一轮该照常索取上下文，而不是被判无法解析"
    assert first.request_count == 3, "三条批次内的 file_diff 真的执行了"
    assert "工具调用信封抽取" in first.note, "修复必须留痕（读 trace 的人要能看见）"
    assert "未重发" in first.note, "留痕要能读成「这一轮没花重发的钱」"
    assert len(client.calls) == 2, "只多了一次正常的下一轮，不是重发"
    # 同一轮**只**记一条账：修复那一支不许自己 `_emit`，要落回循环体尾部（同一轮发两份
    # 会撞 `uq_ai_trace_run_round`），所以轮号必须是 1、2，不重不漏。
    assert [record.index for record in outcome.rounds] == [1, 2]


def test_the_recovered_round_still_records_what_the_whitelist_rejected():
    """抽样不许绕过白名单：那条只有 16 位地址的 `evidence` 被丢并**逐条记账**。"""
    client = ScriptedClient(_sample(S4_SAMPLE), _final())

    outcome = _run(client, scope=_scope_covering(S4_SAMPLE))

    rejected = [item for item in outcome.dropped if item.kind == "request"]
    assert rejected, "被白名单拒掉的请求必须留下记录，否则「为什么少看了一份」无从追溯"
    assert any("合法地址" in item.reason for item in rejected)


# ==========================================================================
# 二、抽不出来的：一次**独立**的纠正，且不吃协议那本账
# ==========================================================================


def test_a_think_only_envelope_gets_its_own_correction_even_with_no_protocol_budget():
    """纯思考的信封（run 44 的 S2）在 `max_corrections=0` 时**仍然**能重问一次。

    这正是「独立额度」的行为断言：若它与协议那 2 次共用，这条用例会直接死在第一轮
    （`protocol_corrections_exhausted`），而真实故障形态是「撞过两次截断的分片再遇一次
    信封就整片阵亡」。
    """
    client = ScriptedClient(_sample(S2_SAMPLE), _final())

    outcome = _run(client, limits=EngineLimits(max_corrections=0, max_channel_corrections=1))

    assert outcome.status == STATUS_SUCCEEDED, outcome.error_message
    assert len(client.calls) == 2, "初答 + 一次通道纠正"
    first = outcome.rounds[0]
    assert first.status == "unparsable"
    assert "输出通道错位" in first.note, "trace 要能读出「这一轮是被当成通道错位纠正的」"


def test_the_protocol_budget_is_spent_on_prose_not_on_the_envelope():
    """两本账分开：一句普通散文烧掉协议额度之后，信封仍拿得到它自己那一次。

    若两本账是一本，这句散文已经把唯一的额度花掉，信封那一轮会立刻阵亡；分开则两轮
    纠正各走各的。
    """
    client = ScriptedClient("我觉得这个改动还行吧，没什么大问题。", _sample(S2_SAMPLE), _final())

    outcome = _run(client, limits=EngineLimits(max_corrections=1, max_channel_corrections=1))

    assert outcome.status == STATUS_SUCCEEDED, outcome.error_message
    assert len(client.calls) == 3, "散文一轮纠正 + 信封一轮纠正，各一次"
    assert "不符合协议" in outcome.rounds[0].correction_hint, "第一轮是普通协议纠正"
    assert "输出通道错位" in outcome.rounds[1].note, "第二轮才是通道纠正"


def test_the_channel_correction_is_bounded_by_the_round_limit():
    """换本账**不解除循环上界**：一直吐信封的模型最多花掉「初答 + 额度」那么多轮。

    这条是那句注释的守卫 ——「重问也要占一轮，否则一个不肯说 JSON 的模型能把循环变成
    无限次重试」。额度换了一本账，这条纪律一个字都没松。
    """
    client = ScriptedClient(_sample(S2_SAMPLE))

    outcome = _run(client, limits=EngineLimits(max_rounds=8, max_channel_corrections=1))

    assert outcome.status == STATUS_FAILED
    assert outcome.degradation == DEGRADE_PROTOCOL
    assert outcome.rounds_used == 2, "初答 + 一次通道纠正，用完就收"
    assert len(client.calls) == 2


def test_the_channel_correction_never_exceeds_the_round_limit():
    """轮次上限是**硬**上界：只给 2 轮时，通道纠正最多用掉这 2 轮，一次都不多。

    **探索**轮次的那条上界不变（2 轮用完就停，纠正额度再大也换不出第三轮）。这条用例
    现在要多认一件事：额度用尽、手上又有取证时，失败出口会补**一次**「现在出结论」
    （`services/ai/wrap_up.py`）—— 那一次走的是兜底，不是通道纠正借来的轮次。
    判据因此分成两半，而不是把上界从 2 放宽到 3（那样这条守卫就废了）。
    """
    client = ScriptedClient(_sample(S2_SAMPLE))

    outcome = _run(client, limits=EngineLimits(max_rounds=2, max_channel_corrections=5))

    assert "补发" in outcome.rounds[-1].note, outcome.rounds[-1].note
    exploration = outcome.rounds[:-1]
    assert len(exploration) <= 2, [record.index for record in exploration]
    assert len(client.calls) == len(outcome.rounds)


# ==========================================================================
# 三、纠正提示：正面事实，不回显那段信封
# ==========================================================================


def test_the_retry_is_told_the_fact_and_not_shown_the_wrong_writing_again():
    """重问那一轮发出去的话里：有事实、没有那段信封。

    `correction_hint` 是**原样**发进提示词的（`prompt.build_user_message` 的
    「## 上一轮的问题」一节），所以断言它就等于断言模型收到的东西。
    """
    client = ScriptedClient(_sample(S2_SAMPLE), _final())

    outcome = _run(client, limits=EngineLimits(max_channel_corrections=1))

    hint = outcome.rounds[0].correction_hint
    assert "没有可以直接调用的 API 工具" in hint
    assert "requests" in hint
    assert "DSML" not in hint, "又把那段信封抄回提示词了"
    # 再核一遍「真的发出去了」：它落在用户消息里那一节。
    sent = client.calls[1][-1]["content"]
    assert "## 上一轮的问题" in sent
    assert "DSML" not in sent.split("## 上一轮的问题", 1)[1]


def test_an_ordinary_prose_answer_is_not_treated_as_a_channel_mismatch():
    """反向：普通散文不走这条分支（否则每一轮都会去花那本通道账）。

    与 `test_channel_correction_is_bounded_by_the_round_limit` 对照着看：同样是
    「一直不给 JSON」，散文那条路花的是协议额度（初答 + 2 次 = 3 轮），信封那条花的是
    自己的（初答 + 1 次 = 2 轮）。
    """
    client = ScriptedClient("我觉得这个改动还行吧，没什么大问题。")

    outcome = _run(client, limits=EngineLimits(max_rounds=8, max_corrections=2))

    assert outcome.degradation == DEGRADE_PROTOCOL
    assert outcome.rounds_used == 3, "普通散文照旧用协议那 2 次额度"
    assert not any("通道错位" in (record.note or "") for record in outcome.rounds)


def test_a_markdown_report_still_takes_the_markdown_road():
    """反向：markdown 报告的分支优先级没有被改动（它仍走「原样转成 JSON」那一次）。"""
    client = ScriptedClient(_markdown("改了道具表。"), _final())

    outcome = _run(client, limits=EngineLimits(max_channel_corrections=1))

    assert outcome.status == STATUS_SUCCEEDED
    assert "不符合协议" not in outcome.rounds[0].correction_hint
    assert not any("通道错位" in (record.note or "") for record in outcome.rounds)


def test_an_ordinary_json_round_is_untouched():
    """反向：正常的 JSON 轮次与通道这条分支毫无关系（默认行为逐字不变）。"""
    client = ScriptedClient(
        '{"status": "need_more_context", "reason_code": "triage", '
        '"requests": [{"type": "commit_detail", "commit": "' + "a" * 40 + '"}]}',
        _final(),
    )

    outcome = _run(client, provider=FakeProvider())

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.rounds[0].status == "requests"
    assert outcome.rounds[0].request_count == 1
    assert outcome.rounds[0].note == "", "正常轮次不该多出任何修复/纠正的按语"


# ==========================================================================
# 四、协议只有一条路：JSON 里的 requests（没有原生工具）
# ==========================================================================


def test_the_request_body_registers_no_native_tools():
    """请求体里**不许**出现 `tools` / `tool_choice`。

    这是「模型为什么会在正文里编工具调用」那条链的起点，也是本次修复**刻意没动**的地方：
    端点/网关能不能正确传 `tool_calls` 未证实，加了就是**双协议**（两套请求同时执行、
    白名单与逐轮账本会被绕开一套）。推理见工作包 C 的说明。
    """
    from services.ai.llm_client import LLMClient

    client = LLMClient(base_url="https://example.invalid/v1", model="m")

    body = client._request_body(
        [{"role": "user", "content": "hi"}], temperature=0.0, marker=None, max_tokens=10
    )

    assert "tools" not in body
    assert "tool_choice" not in body
    assert set(body) <= {"model", "messages", "temperature", "stream", "max_tokens"}, (
        f"请求体里出现了协议之外的字段：{sorted(body)}"
    )


def test_five_read_types_run_in_one_round_and_the_unknown_ones_do_not():
    """一轮里把五种只读取数（外加 `evidence`）全要一遍：能执行的执行、越权的拒掉。

    这一条同时钉住三件验收要看的事实：
      * 五种类型**真的都能执行**（不是「文档里写了」）；
      * 未知类型（模型自造的 `get_file_diff`）与越权参数**不可执行**，且逐条记账；
      * 跨轮重复索取走缓存命中（`cache_hits`），而额度照扣（省的是耗时不是额度）。
    """
    commit = "a" * 40
    client = ScriptedClient(
        _requests(
            {"type": "commit_detail", "commit": commit},
            {"type": "file_diff", "commit": commit, "path": TABLE},
            {"type": "file_content", "commit": commit, "path": TABLE},
            {"type": "read_reference", "name": "incident-checklist.md"},
            {"type": "find_references", "query": "CfgRewardMode"},
            {"type": "evidence", "name": "b" * 20},
            # 下面两条**不许执行**：一个是模型自造的工具名，一个是批次外的路径。
            {"type": "get_file_diff", "commit": commit, "path": TABLE},
            {"type": "file_diff", "commit": commit, "path": "scripts/secret.env"},
        ),
        # 第二轮把第一轮要过的 `file_diff` 原样再要一次 —— 跨轮缓存命中。
        _requests({"type": "file_diff", "commit": commit, "path": TABLE}),
        _final(),
    )

    outcome = _run(client, provider=FakeProvider())

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.rounds[0].request_count == 6, "五种只读取数 + evidence 都要执行"
    assert outcome.rounds[1].request_count == 1
    assert outcome.cache_hits >= 1, "同一个请求第二轮再要，应当走缓存而不是重新取数"

    rejected = [item for item in outcome.dropped if item.kind == "request"]
    details = " ".join(f"{item.detail} {item.reason}" for item in rejected)
    assert "get_file_diff" in details, "模型自造的工具名必须被白名单拒掉并记账"
    assert "secret.env" in details, "批次外的路径必须被拒掉并记账"
    assert not any(
        "get_file_diff" in str(key) for key in outcome.tool_stats
    ), "未知类型一次都不许执行"
