# -*- coding: utf-8 -*-
"""上下文过长时怎么办：压历史、以及被上游拒绝之后的收尾。

## 这个文件守的四条性质

1. **提示词不会无限长。** 以前只有「本轮取回的上下文」受预算约束，历史不受任何约束 ——
   每一轮都把上一轮整条消息再发一遍，于是提示词随轮次单调增长，最后撞上模型窗口。
   这里断言的是「每一次真正发出去的请求都在预算内」，而不是「某个函数返回了什么」。
2. **压缩绝不静默。** 压掉的轮次要留下可读的记录（第几轮、索取了什么、可以重新索取），
   而且必须写进 trace。少一条，模型就会以为自己没拿到过、于是重新要一遍 —— 额度花两遍。
3. **上游拒绝不能把一次分析作废。** 估算终究是估算（字符 ≠ token）。上游说装不下时按
   「丢得越少越先试」补救：先把本轮条目压到 1/4 重发（历史留着）→ 还不行就连历史一起丢
   再压一遍 → 都不行才换成一段必然装得下的「收尾」提示词。**前面几轮的钱已经花了，
   空手而归是最坏的结果。**
4. **不是超长的错不要当成超长。** 认错的代价是最多多花三次压缩后的调用，但把网络错误、
   鉴权错误当成超长会掩盖真正的原因 —— 那两类都必须按原样快速失败（包括在补救过程中
   撞上的时候，见 `test_a_non_overflow_failure_during_the_retry_...`）。

`tests/test_ai_engine.py` 的假件（`ScriptedClient` / `FakeProvider` / `_run`）直接复用：
这里要测的正是「真跑一次多轮」，而不是某个纯函数。
"""
from __future__ import annotations

import pytest

from services.ai.budget import (
    MIN_KEEP_TURNS,
    ContextItem,
    TurnMemo,
    compact_history,
    estimate_chars,
    looks_like_context_overflow,
)
from services.ai.engine import (
    DEGRADATION_LABELS,
    DEGRADE_CONTEXT,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineLimits,
)
from services.ai.llm_client import ChatResult, LLMResponseError
from services.ai.prompt_cache import CACHE_BREAKPOINT_KEY
from tests.test_ai_engine import (
    COMMIT,
    LUA,
    TABLE,
    FakeProvider,
    ScriptedClient,
    _anomaly,
    _final,
    _requests,
    _run,
)

# 上游拒绝的四种常见文案（OpenAI 系 / vLLM / 网关转译 / 中文网关）。
OVERFLOW_ERRORS = (
    "LLMResponseError: This model's maximum context length is 131072 tokens",
    "LLMResponseError: context_length_exceeded",
    "LLMResponseError: The input is too long for this model",
    "LLMResponseError: 上下文长度超出最大限制",
)


# ==========================================================================
# 一、looks_like_context_overflow：认得出，但别乱认
# ==========================================================================


@pytest.mark.parametrize("text", OVERFLOW_ERRORS)
def test_the_common_overflow_phrasings_are_recognised(text):
    assert looks_like_context_overflow(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "LLMResponseError: Incorrect API key provided",
        "LLMAuthError: 401 Unauthorized",
        "ConnectionError: HTTPSConnectionPool(host='x', port=443): Read timed out",
        "LLMResponseError: model 'gpt-x' not found",
        "",
        None,
    ],
)
def test_unrelated_errors_are_not_mistaken_for_overflow(text):
    """**认错比漏认更贵。**

    漏认的代价是这次分析作废（所以宁可放宽），但把「密钥错了」「模型名不存在」当成超长
    会去压缩、重试、收尾三连，最后报一个「上下文不足」—— 真正的原因被埋掉，而用户会去
    调预算、换模型，做什么都没用。
    """
    assert looks_like_context_overflow(text) is False


# ==========================================================================
# 二、compact_history：只动中间，压的是重复
# ==========================================================================


def _conversation(turns: int, *, chars: int = 1_000):
    """造一段 [system, user1, assistant1, user2, assistant2, ...] 的对话。

    结构与引擎跑到一半时**逐字一致**（这正是压历史要处理的那种输入）：system、第一轮
    的 user 消息、然后每轮一对 user/assistant。每段内容带上能认出是哪一轮的标记。
    """
    messages = [
        {"role": "system", "content": "系统提示词"},
        {"role": "user", "content": "第一轮：变更清单" + "甲" * chars},
    ]
    for index in range(1, turns + 1):
        messages.append({"role": "assistant", "content": f"第{index}轮答复" + "乙" * chars})
        if index < turns:
            messages.append({"role": "user", "content": f"第{index + 1}轮提问" + "丙" * chars})
    return messages


def _texts(messages) -> str:
    return "\n".join(str(item.get("content") or "") for item in messages)


def test_the_head_and_the_recent_turns_are_kept_verbatim():
    messages = _conversation(5)

    result = compact_history(messages, target_chars=4_000, keep_recent_turns=2)

    assert result.compacted
    assert result.messages[0] == messages[0], "system 被动了 —— 它是跨运行的缓存前缀"
    assert result.messages[1] == messages[1], "第一轮（变更清单）被动了"
    assert "第5轮答复" in _texts(result.messages), "最近一轮没有原文保留"
    assert "第1轮答复" not in _texts(result.messages), "最早的中间轮次没有被压掉"


def test_it_drops_as_few_turns_as_possible():
    """**能少压就少压。** 压掉的每一轮都是有代价的（模型要多一次重新索取）。

    目标卡在边界上：把最早那条 assistant 去掉刚好装得下。用 `<` 而不是 `<=` 就会多压
    一轮，而多压的那一轮是白丢的 —— 边界正是这条用例要钉的东西。
    """
    messages = _conversation(5, chars=100)
    target = estimate_chars(messages) - len(messages[2]["content"])

    result = compact_history(messages, target_chars=target)

    assert result.dropped_turns == 1, f"压了 {result.dropped_turns} 轮，多压了"
    assert estimate_chars(result.messages) <= target


def test_it_never_drops_below_the_floor():
    """目标再小也不能把「最近几轮」也压掉：模型当下的推理是从最近一轮继续的。"""
    messages = _conversation(6)

    result = compact_history(messages, target_chars=1, keep_recent_turns=MIN_KEEP_TURNS)

    assert result.compacted
    # 剩下的 = head + 最近 MIN_KEEP_TURNS 轮
    assert len(result.messages) <= 2 + 2 * MIN_KEEP_TURNS
    assert "第6轮答复" in _texts(result.messages)


def test_nothing_is_touched_when_it_already_fits():
    messages = _conversation(4, chars=100)

    result = compact_history(messages, target_chars=10_000)

    assert result.compacted is False
    assert result.messages == tuple(messages)
    assert result.recap == "" and result.notes == ()


def test_a_short_conversation_has_nothing_to_compact():
    """只有 system + 第一轮时无从压起（那两段是刻意不动的），如实返回、不报错。"""
    messages = _conversation(1)[:2]

    result = compact_history(messages, target_chars=1)

    assert result.compacted is False
    assert result.messages == tuple(messages)


def test_the_recap_says_what_was_dropped_and_that_it_can_be_requested_again():
    """**这是本文件最重要的一条。** 被压掉的轮次不能消失得无影无踪。"""
    messages = _conversation(5)
    memos = [
        TurnMemo(
            index=1,
            status="requests",
            items=(ContextItem(kind="file_diff", label="file_diff aaaa 道具表", text="x"),),
        )
    ]

    result = compact_history(messages, target_chars=2_000, keep_recent_turns=1, memos=memos)

    assert result.compacted
    assert "已压缩的历史" in result.recap
    assert "第 1 轮" in result.recap
    assert "道具表" in result.recap, "被压掉的轮次取到了什么，必须留下来"
    assert "重新索取" in result.recap, "不告诉它可以重新要，它会以为内容丢了"
    assert "重新索取" in "".join(result.notes)
    # 摘要本身必须**小**：摘要要是也很大，压历史就白压了。
    assert len(result.recap) < 2_000, f"摘要太大：{len(result.recap)}"


def test_the_original_messages_are_left_alone():
    """引擎传进来的是它自己那份 `messages`（压完还要继续往里 append），所以这里
    只能深拷贝式地读，**绝不能就地改动**。"""
    messages = _conversation(5)
    before = [dict(item) for item in messages]

    compact_history(messages, target_chars=2_000, keep_recent_turns=1)

    assert messages == before, "入参被就地改掉了"


def test_the_cache_breakpoint_on_the_head_survives():
    """断点②（第一轮那条消息）必须原样留在消息字典上。

    它换了位置就等于把「跨运行复用缓存」这件事丢掉了，而那正是断点存在的全部理由。
    """
    messages = _conversation(5)
    messages[0][CACHE_BREAKPOINT_KEY] = {"type": "ephemeral"}
    messages[1][CACHE_BREAKPOINT_KEY] = {"type": "ephemeral"}

    result = compact_history(messages, target_chars=2_000, keep_recent_turns=1)

    assert result.messages[0].get(CACHE_BREAKPOINT_KEY), "system 上的断点丢了"
    assert result.messages[1].get(CACHE_BREAKPOINT_KEY), "第一轮上的断点丢了"


# ==========================================================================
# 三、整份提示词受预算约束（多轮真跑）
# ==========================================================================

BUDGET = 35_000
# 变更清单占大头（线上真实形态：全量列出时约 87,000 字）。加上每轮 11,000 的 diff
# 与逐轮累积的历史，不压的话第 4 轮就会顶破 35,000。
_BIG_SUMMARY = "本次变更共 1 个提交、2 个文件。\n" + "文件清单行。\n" * 1_200
# 5 轮索取 + 1 轮结论 = 6 次调用，所以 max_rounds 要留出第 6 轮。
_CHATTY_ROUNDS = 4
# 每一轮要**不同的**东西：同一个文件第二次要只会拿到一段指针（那是刻意的设计，
# 见 context_tools 的「同一份正文不重发第二遍」），历史也就不涨了。
_ROTATION = (
    {"type": "file_diff", "commit": COMMIT, "path": TABLE},
    {"type": "file_diff", "commit": COMMIT, "path": LUA},
    {"type": "file_content", "commit": COMMIT, "path": TABLE},
    {"type": "file_content", "commit": COMMIT, "path": LUA},
    {"type": "commit_detail", "commit": COMMIT},
)


class FatProvider(FakeProvider):
    """每条上下文都给到上限附近（11,000 字）。

    `FakeProvider` 给的是一句话（几十字符），那样历史**涨不起来** —— 而这个文件要测的
    正是「历史涨到撑破预算时会发生什么」，所以内容必须是真的体积。
    """

    def file_diff(self, commit, path):
        self.seen.append(("file_diff", commit, path))
        return f"diff of {path} at {commit}\n" + "+ 一行改动\n" * 4_000

    def file_content(self, commit, path, lines=""):
        # 形参不能少（协议里有这个可选窗口）。少了它取数会抛 TypeError，而取数失败是被
        # 接住的 —— 表现不是报错，是这个文件里四条「历史涨到撑破预算」的用例全变成
        # 「历史根本没涨」（实测：本文件 4 条红，报的是「跑了这么多轮都没压过历史」）。
        self.seen.append(("file_content", commit, path))
        return f"{path} 的完整内容\n" + "行内容\n" * 4_000

    def commit_detail(self, commit):
        self.seen.append(("commit_detail", commit))
        return f"提交 {commit} 的详情\n" + "文件名\n" * 4_000


def _chatty_client(rounds: int = _CHATTY_ROUNDS, **kwargs):
    """要上下文要很多轮，最后才给结论。"""
    replies = [_requests(_ROTATION[index % len(_ROTATION)]) for index in range(rounds)]
    replies.append(_final(_anomaly()))
    return ScriptedClient(*replies, **kwargs)


def _run_big(client, **overrides):
    kwargs = {
        "limits": EngineLimits(prompt_char_budget=BUDGET, max_rounds=_CHATTY_ROUNDS + 2),
        "change_summary": _BIG_SUMMARY,
        "provider": FatProvider(),
    }
    kwargs.update(overrides)
    return _run(client, **kwargs)


def test_no_request_ever_exceeds_the_budget():
    """**核心性质**：真正发出去的每一条请求都在预算内。

    以前这里必然红：历史不受约束，第 5 轮之后每一条都在预算之上（字节数上完全看不出来，
    只有把每一条都量一遍才知道）。所以这条断言是逐次量的，不是看最后一条。
    """
    client = _chatty_client()

    outcome = _run_big(client)

    assert outcome.compaction.happened, "跑了这么多轮都没压过历史，说明约束没生效"
    oversized = [
        (index, estimate_chars(call))
        for index, call in enumerate(client.calls)
        if estimate_chars(call) > BUDGET
    ]
    assert not oversized, f"有请求超出预算（第几次调用, 字符数）：{oversized}"


def test_the_history_is_really_what_pushes_it_over():
    """反向自检：**不许**通过「把条目压到 0」来让上面那条通过。

    如果实现是「反正塞不下就什么都不塞」，那提示词的体积确实不会超 —— 但模型也就什么
    都看不到了。所以这里要求：压过之后，最近一轮仍然拿到了它索要的正文。
    """
    client = _chatty_client()

    outcome = _run_big(client)

    assert outcome.compaction.dropped_chars > 0
    # 压过之后最近一轮仍然拿到了它索要的正文（渲染出来的小节标题 + 正文都在）。
    assert outcome.rounds[-1].item_count > 0, "最后一轮一条上下文都没有 —— 那是「什么都不塞」"
    assert "### [" in client.calls[-1][-1]["content"], "最近的上下文没有回灌"
    assert outcome.requests_used == _CHATTY_ROUNDS, "索取额度被别的东西吃掉了"


def test_the_compressed_rounds_are_recorded_in_the_trace():
    """压了哪几轮、省了多少，必须在 trace 里查得到 —— 否则「这次分析怎么浅了」无从解释。"""
    client = _chatty_client()

    outcome = _run_big(client)

    notes = " ".join(record.note for record in outcome.rounds)
    assert "为控制上下文体积" in notes, f"trace 里没有任何压缩的痕迹：{notes!r}"
    assert outcome.compaction.events >= 1
    assert outcome.compaction.dropped_turns >= 1
    assert outcome.compaction.to_dict()["dropped_chars"] > 0


def test_the_model_is_told_the_history_was_compressed():
    """模型也必须被告知：它一开始看到过、后来「不见了」的内容去哪了。

    不说的话，它会把「压缩掉了」当成「我没拿到过」，于是重新索取 —— 额度花两遍。

    而且要**说清被压掉的是哪几轮、那几轮取到了什么**。这一条同时守着 `memos` 与轮次的
    对齐（`compact_history` 靠位置对应，错位的话记录会挂到别的轮次上 —— 那比不记更糟：
    模型会以为「第 1 轮拿过 LUA」，于是不再去要它）。
    """
    client = _chatty_client()

    _run_big(client)

    notice = client.calls[-1][-1]["content"]
    assert "已压缩的历史" in notice
    assert "重新索取" in notice
    assert "第 1 轮：索取了上下文" in notice, "没有说清被压掉的是哪一轮"
    assert TABLE in notice, "被压掉的那一轮取到了什么，必须留在记录里"


def test_a_run_inside_the_budget_never_compacts():
    """预算够用时一个字都不许压 —— 压了就是白丢信息。

    （反向守卫：上面几条都在「预算不够」的前提下，如果没有这一条，一个「每轮都压一下」
    的实现也能全绿。）
    """
    client = _chatty_client(rounds=1)

    outcome = _run(
        client,
        limits=EngineLimits(prompt_char_budget=2_000_000, max_rounds=3),
        change_summary="本次变更共 1 个提交、2 个文件。\n",
    )

    assert outcome.compaction.happened is False
    assert outcome.compaction.to_dict() == {
        "events": 0,
        "dropped_turns": 0,
        "dropped_chars": 0,
        "overflow_recovered": False,
    }


# ==========================================================================
# 四、上游拒绝了：先压条目 → 再丢历史 → 最后收尾
# ==========================================================================


class RejectAt:
    """在**指定的第几次调用**上抛「上下文超长」，其余正常回答。

    用集合而不是「前 N 次」来指定，是因为要测的几种局面差别很大：第一次调用就被拒
    （提示词本身就太大）、跑到第 5 轮才被拒（历史攒大了）、以及**补救本身也被拒**
    （`reject_at=(3, 4)`：压小那次也没过）。后者才是「一直退到收尾」的情形。

    回答按**成功**的调用次数取，所以一次拒绝不会吃掉一条脚本回答。

    `error` 可以是一句话（所有被拒的调用都用它），也可以是 `{第几次调用: 那句话}` ——
    后者用来测「压小重发那一次撞上的是**别的**故障」。
    """

    def __init__(self, *replies, reject_at=(1,), error: object = None):
        self._replies = list(replies)
        self._reject_at = set(reject_at)
        self._error = OVERFLOW_ERRORS[0] if error is None else error
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, temperature=None):
        self.calls.append([dict(item) for item in messages])
        number = len(self.calls)
        if number in self._reject_at:
            text = (
                self._error.get(number, OVERFLOW_ERRORS[0])
                if isinstance(self._error, dict)
                else self._error
            )
            raise LLMResponseError(str(text))
        succeeded = sum(1 for index in range(1, number + 1) if index not in self._reject_at)
        index = min(succeeded - 1, len(self._replies) - 1)
        result = ChatResult(
            text=self._replies[index], model="fake", prompt_tokens=10, completion_tokens=5
        )
        return result

    def sizes(self) -> list[int]:
        return [estimate_chars(call) for call in self.calls]


def test_a_rejection_is_recovered_by_shrinking_the_items_first():
    """**这条是「分析不会因为超出上下文而作废」的证据。**

    上游拒绝之后按「丢得越少越先试」补救，第一步是**把本轮条目压到 1/4 重发，历史留着**。

    ## 为什么第一步不是压历史

    条目是按我们自己的额度渲染出来的**确定量**（每轮最多 40 条 × 11,000 字，而且每轮
    都要重发一遍），压到 1/4 是我们说了算的；压历史则是不可逆的 —— 那些轮次的内容模型
    再也看不到，只能重新索取，而索取是花额度的。所以先试代价小的那一步。

    第 2 轮被拒（而不是更晚）是有意的：那时条目还没被压到下限，第一步才**压得动**
    （贴着下限时第一步是空转，见 `..._that_cannot_reduce_anything_is_not_sent`）。
    """
    client = RejectAt(
        *([_requests(_ROTATION[i]) for i in range(2)] + [_final(_anomaly())]), reject_at=(2,)
    )

    outcome = _run_big(client)

    assert outcome.status == STATUS_DEGRADED, outcome.error_message
    assert outcome.degradation == DEGRADE_CONTEXT
    assert outcome.anomalies, "压小之后结论丢了"
    assert outcome.compaction.overflow_recovered is True
    assert len(client.calls) == 4, "第 2 次被拒 → 第 3 次是压小重发，不是收尾"
    rejected, retry = client.calls[1], client.calls[2]
    assert estimate_chars(retry) < estimate_chars(rejected), "重发的那一次没有变小"
    assert len(retry) == len(rejected), "这一步不该动历史 —— 历史还在"
    assert "压到 1/4" in " ".join(record.note for record in outcome.rounds)


def test_a_shrink_that_cannot_reduce_anything_is_not_sent():
    """**压不动的补救不许发。** 条目已经贴着额度下限时，「把额度砍到 1/4」压出来的是
    一模一样的体积（下限 4,000 字兜着，还多一行「额度被压到 4,000 字」的提示 ——
    实测比原来还长 59 个字符）。那不是补救，是白烧一次调用。

    所以这一步直接跳过，进下一步（丢历史）：历史才是那时的大头。
    """
    # 第 3 轮被拒：这一轮的条目在预算 35,000 下早已贴着下限。
    client = RejectAt(
        *([_requests(_ROTATION[i]) for i in range(2)] + [_final(_anomaly())]), reject_at=(3,)
    )

    outcome = _run_big(client)

    assert len(client.calls) == 4, "第 3 次被拒 → 第 4 次应当是丢历史，而不是原样重发"
    third, fourth = client.calls[2], client.calls[3]
    assert len(fourth) == 2, "跳过压不动的那一步，直接丢历史（只留 system + 本轮）"
    assert estimate_chars(fourth) < estimate_chars(third)
    assert "历史丢弃" in " ".join(record.note for record in outcome.rounds)
    assert outcome.anomalies, "丢历史之后结论丢了"


def test_when_the_shrunk_retry_is_rejected_too_the_history_goes():
    """第二步才丢历史：条目压到 1/4 还装不下，说明大头在**历史**那边（水位估得不准）。

    这一步同样保留「最近这一批证据文本」，丢的只是早先那几轮的对话 —— 顺序是
    「丢得越少的越先试」，不是「一次丢光」。
    """
    client = RejectAt(
        *([_requests(_ROTATION[i]) for i in range(2)] + [_final(_anomaly())]), reject_at=(2, 3)
    )

    outcome = _run_big(client)

    assert len(client.calls) == 5, "第 2 次被拒 → 压条目（第 3 次）也被拒 → 第 4 次丢历史"
    rejected, first_try, second_try = client.calls[1], client.calls[2], client.calls[3]
    assert len(first_try) == len(rejected), "第一步之后、第二步之前，历史还在"
    assert len(second_try) == 2, "第二步只留 system + 本轮，历史被丢掉"
    assert estimate_chars(second_try) < estimate_chars(first_try)
    assert outcome.status == STATUS_DEGRADED, outcome.error_message
    assert outcome.degradation == DEGRADE_CONTEXT
    assert outcome.anomalies, "丢历史之后结论丢了"
    assert "历史丢弃" in " ".join(record.note for record in outcome.rounds)


def test_the_last_resort_is_still_a_prompt_that_fits_any_window():
    """三级补救全被拒之后，才用那段**必然装得下**的收尾提示词。

    收尾请求必须小到任何窗口都装得下：它存在的唯一理由就是「上面那些装不下」。
    只带变更清单的开头与已取内容的目录，一条正文都不带 —— 正文正是装不下的那部分。
    """
    client = RejectAt(_requests(_ROTATION[0]), _final(_anomaly()), reject_at=(2, 3, 4))

    outcome = _run_big(client)

    assert len(client.calls) == 5, "压条目、丢历史都被拒 → 第 5 次才是收尾"
    salvage = client.calls[-1]
    assert len(salvage) == 2, "收尾请求只该有 system 与一条 user 消息"
    assert estimate_chars(salvage) < 8_000
    assert "信息缺口" in salvage[-1]["content"], "收尾时必须要求它标出没看完的部分"
    assert "不要再索取上下文" in salvage[-1]["content"], (
        "这一轮之后分析就结束了，还让它索取等于白烧一次调用"
    )
    assert "上下文" in DEGRADATION_LABELS[DEGRADE_CONTEXT]
    assert outcome.status == STATUS_DEGRADED


def test_a_recovered_overflow_still_marks_the_whole_run_as_degraded():
    """第 3 轮撞了窗口、第 4 轮才交结论 —— 这份结论仍然是「在被裁剪过的提示词上得到的」。

    判据必须是**整次运行**的，不能只看交结论那一轮：只按当前轮判就会把这次降级记成一次
    干净的运行，而用户正是靠这个标签判断「这份报告我该信几分」。
    """
    replies = [_requests(_ROTATION[index % len(_ROTATION)]) for index in range(3)]
    client = RejectAt(*replies, _final(_anomaly()), reject_at=(3,))

    outcome = _run_big(client)

    assert outcome.requests_used == 3
    assert outcome.status == STATUS_DEGRADED, outcome.error_message
    assert outcome.degradation == DEGRADE_CONTEXT, "撞过窗口这件事随交结论的轮次一起丢了"


def test_a_recovered_overflow_tightens_the_budget_for_the_rest_of_the_run():
    """实测到的上限要**用在本次运行剩下的轮次**上。

    否则下一轮照原来的预算组装、再被拒一次、再补救一次 —— 每一轮都多烧一到三次调用。
    上限是从「刚被拒的那份提示词有多大」来的：它是这次运行里唯一一个实测值（水位是按
    窗口估的，估错了才会走到这里）。

    **它是一个「下限兜着」的软上限。** 系统提示词与第一轮（变更清单）钉住不压，最近几轮
    原文也要留 —— 那几块合起来本身就可能比上限大，那时压不到上限（这是既有设计，
    不是这次补救能改的），能保证的是**不再往上涨回被拒的那条**。
    """
    replies = [_requests(_ROTATION[index % len(_ROTATION)]) for index in range(3)]
    client = RejectAt(*replies, _final(_anomaly()), reject_at=(2,))

    outcome = _run_big(client)

    assert outcome.status == STATUS_DEGRADED
    rejected_chars = estimate_chars(client.calls[1])
    cap = int(rejected_chars * 3 / 4)
    assert cap < BUDGET, "这一组数据没测到收紧（上限还比原预算大）"
    notes = " ".join(record.note for record in outcome.rounds)
    assert f"收到 {cap:,} 字" in notes, f"没有把实测到的上限写进记录：{notes!r}"
    later = [estimate_chars(call) for call in client.calls[3:]]
    assert len(later) >= 2, "补救之后应当还有不止一轮"
    assert max(later) <= rejected_chars, (
        f"补救之后又涨回超过被拒的那一条：{later}（被拒的那条 {rejected_chars}）"
    )


def test_the_message_that_was_rejected_does_not_land_in_the_history():
    """补救成功之后，进历史的必须是**真发出去的那一条**。

    出口处那句 `messages.append(entry)` 追加的是「这一轮发给模型的消息」。收缩之后忘了把
    `entry` 换掉的话，进历史的是**刚被上游拒掉的超大消息** —— 于是下一轮更大、再被拒一次、
    再补救一次（实测：补救后的下一轮 21,845 → 27,136，越补越大），而补救的意义恰恰是
    别让它留在上下文里。
    """
    client = RejectAt(
        *([_requests(_ROTATION[i]) for i in range(2)] + [_final(_anomaly())]), reject_at=(2,)
    )

    outcome = _run_big(client)

    rejected_chars = estimate_chars(client.calls[1])
    assert outcome.status == STATUS_DEGRADED, outcome.error_message
    assert len(client.calls) == 4
    after = estimate_chars(client.calls[3])
    assert after < rejected_chars, (
        f"补救之后那一轮比被拒的那条还大（{after} >= {rejected_chars}）——"
        "被拒的消息进了历史"
    )


def test_a_non_overflow_failure_during_the_retry_is_reported_as_itself():
    """压小重发时撞上**别的**故障（502 之类）要如实报。

    **不许**拿它去触发收尾提示词：那会把一次传输故障记成「窗口不够」，而用户会照着
    「上下文不足」的建议去调预算、换模型 —— 做什么都没用（与 `looks_like_context_overflow`
    那条取舍同一个理由）。
    """
    client = RejectAt(
        *([_requests(_ROTATION[i]) for i in range(2)] + [_final(_anomaly())]),
        reject_at=(3, 4),
        error={3: OVERFLOW_ERRORS[0], 4: "LLMResponseError: 502 Bad Gateway"},
    )

    outcome = _run_big(client)

    assert outcome.status == STATUS_FAILED
    assert len(client.calls) == 4, "第 4 次是传输故障 → 就此打住，不再收尾"
    assert "502" in outcome.error_message, outcome.error_message
    assert "上下文不足" not in outcome.error_message, "传输故障被说成了窗口不够"


def test_every_rejection_is_recorded_in_the_trace():
    """被拒这件事必须留痕，而且要写清**它发生在跑到第几轮、已经花了几次索取之后**。

    只写一句「出错了」没用：读 trace 的人要判断的是「这次结论是在什么基础上得到的」，
    所以「已经取过 4 份上下文才被拒」与「第一轮就被拒」是两件完全不同的事。
    """
    client = RejectAt(
        *([_requests(_ROTATION[index % len(_ROTATION)]) for index in range(_CHATTY_ROUNDS)]),
        _final(_anomaly()),
        reject_at=(5,),
    )

    outcome = _run_big(client)

    notes = " ".join(record.note for record in outcome.rounds)
    assert "超长" in notes, notes
    assert "压到 1/4" in notes, f"没写清补救做了什么：{notes!r}"
    assert "累计已用" in notes, f"没有写清是在什么基础上被拒的：{notes!r}"
    assert outcome.compaction.overflow_recovered is True


def test_when_even_the_salvage_is_rejected_the_failure_explains_what_to_do():
    """彻底失败也要给一句**能照着做**的话，而不是一个异常名。"""
    client = RejectAt(_final(_anomaly()), reject_at=(1, 2))

    outcome = _run_big(client)

    assert outcome.status == STATUS_FAILED
    assert len(client.calls) == 2, "第一轮压不动 → 直接收尾 → 失败，共两次"
    assert "上下文" in outcome.error_message
    assert "范围" in outcome.error_message or "模型" in outcome.error_message


def test_a_non_overflow_error_still_fails_fast():
    """**别把别的错当成超长。** 网络/鉴权错误必须一次就失败，不去压缩重试。

    认错在这里的代价不只是多花一次调用：它会掩盖真正的原因，而用户会照着「上下文不足」
    的建议去调预算、换模型 —— 做什么都没用。
    """

    class Broken:
        def __init__(self):
            self.calls = []

        def complete(self, messages, *, temperature=None):
            self.calls.append([dict(item) for item in messages])
            raise LLMResponseError("Incorrect API key provided")

    client = Broken()

    outcome = _run_big(client)

    assert outcome.status == STATUS_FAILED
    assert len(client.calls) == 1, "非超长的错误不该触发补救流程"
    assert "Incorrect API key" in outcome.error_message
    assert outcome.compaction.happened is False


def test_the_salvage_keeps_the_report_quality_gates():
    """收尾请求拿回来的结论**仍然走门槛与去重**，不是「降级了就照单全收」。

    走的是同一个 `ground_payload` / `normalize_anomalies` 出口 —— 这条用例守的是
    「收尾路径另开了一条不做校验的捷径」这个容易犯的错。
    """
    low = _anomaly(severity="low", confidence="low", title="【道具】低价值提示")
    client = RejectAt(_final(_anomaly(), low), reject_at=(1,))

    outcome = _run_big(client)

    assert outcome.degradation == DEGRADE_CONTEXT
    titles = [item.title for item in outcome.anomalies]
    assert "【道具】低价值提示" not in titles, "收尾路径绕过了门槛过滤"
    assert titles, "收尾拿回来的结论一条都没留下"


def test_the_salvage_lists_what_was_already_fetched():
    """收尾提示词里必须有「你取到过什么」的目录。

    否则模型会以为自己什么都没看过 —— 而它其实看过好几个文件的 diff，只是那些正文
    已经不在上下文里了。不说的话，它要么不敢下结论，要么凭猜测下结论。
    """
    # 第 1 轮正常索取（真的取到了一份 diff），第 2 轮的消息被拒 → 后面两次补救也没过 → 收尾。
    client = RejectAt(_requests(_ROTATION[0]), _final(_anomaly()), reject_at=(2, 3, 4))

    _run_big(client)

    assert len(client.calls) == 5, "第 2 次被拒、后两次补救也没过 → 第 5 次才是收尾"
    salvage_text = client.calls[-1][-1]["content"]
    assert "你已经取到过的内容" in salvage_text
    assert TABLE in salvage_text, "取过的文件没有列进收尾提示词"


def test_the_salvage_carries_the_head_of_the_change_list():
    """收尾提示词必须还认得出「这次改的是哪一片」：变更清单的开头要带上。

    而且它必须**带截断标记** —— 只给开头不说被截断了，模型会把那一段当成全部，
    然后对着一份残缺的清单下结论。
    """
    client = RejectAt(_final(_anomaly()), reject_at=(1,))

    _run_big(client)

    salvage_text = client.calls[-1][-1]["content"]
    assert "本次变更共 1 个提交" in salvage_text
    assert "截断" in salvage_text, "截断了却没说，模型会以为那就是全部"


def test_the_recovered_run_still_reports_its_usage():
    """补救路径上的调用**也是要花钱的**，用量不能丢。

    被拒的那些上游不报 usage（拿不到就是拿不到），但成功那一次的必须记账。
    """
    client = RejectAt(_final(_anomaly()), reject_at=(1,))

    outcome = _run_big(client)

    assert outcome.prompt_tokens == 10 and outcome.completion_tokens == 5
    assert outcome.duration_ms >= 0
