# -*- coding: utf-8 -*-
"""子代理模式的**省钱依据**：共享前缀必须逐字节相同。

## 这条为什么必须逐字节断言

prompt cache 是按**逐字节前缀**匹配的。共享消息里只要有一个字符随成员变化（最常见的是
「本次可索取 N 次」那个数字），第二个成员起就全部 miss —— 而**这件事不会报错**，只表现
为账单贵了 n 倍、缓存命中率那一列是 0。靠「应该会命中」是查不出来的，所以这里把**真正
发出去的请求体**拿出来逐字节比。

## 断言的三件事

1. 同一批里所有成员（含汇总那一次）的**前两条消息完全相同**；
2. 系统消息与**任何**一次运行（含单代理）相同，而「变更清单」那一条要与**同一份额度**
   的单代理运行相同 —— 额度那一句写在共享消息里（「可索取 N 次」/「第 1/N 轮」），
   所以分摊之后它与项目配置那份额度**不同**，这一点下面用两条测试分开钉住，
   免得把「同额度才相同」误读成「永远相同」；
3. 缓存断点在任何一轮都恰好是 `[system, 共享消息, 当前这一轮]` 三个。子代理模式下第 1 轮
   就有三个断点，所以任务书上那个**必须能挪走**；不挪的话第 2 轮变成 4 个、第 3 轮起
   第 5 个被静默丢掉（丢的恰好是当前轮最有价值的那个）。
"""
from __future__ import annotations

import json

from services.ai.engine import EngineLimits, run_analysis
from services.ai.llm_client import ChatResult
from services.ai.prompt_cache import CACHE_BREAKPOINT_KEY, strip_cache_breakpoints
from services.ai.subagent import attach_seed, plan_family, run_family
from tests.test_ai_engine import (
    COMMIT,
    FakeProvider,
    _final,
    _loaded,
    _requests,
    _scope,
)

LIMITS = EngineLimits(max_rounds=8, max_tool_requests=20)
CHANGE_SUMMARY = "本次变更共 1 个提交、2 个文件。\n"


class QueueClient:
    """按队列回答，队列用完就一直重复最后一条。**逐次记录收到的 messages。**"""

    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, temperature=None):
        self.calls.append([dict(item) for item in messages])
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return ChatResult(
            text=self._replies[index], model="fake", prompt_tokens=10, completion_tokens=5
        )


def _sent(call: list[dict]) -> str:
    """把一次调用的消息拍成**真正会发出去的字节**（内部标记摘掉后序列化）。"""
    return json.dumps(strip_cache_breakpoints(call), ensure_ascii=False, sort_keys=True)


def _marked(call: list[dict]) -> list[int]:
    return [index for index, item in enumerate(call) if item.get(CACHE_BREAKPOINT_KEY)]


def _plan(count: int = 3, *, seed: bool = True):
    plan = plan_family(mode="weekly", enabled=True, count=count, limits=LIMITS)
    assert plan is not None
    if not seed:
        return plan
    return attach_seed(plan, loaded=_loaded(), change_summary=CHANGE_SUMMARY)


def _family_args():
    return {
        "loaded": _loaded(),
        "scope": _scope(),
        "change_summary": CHANGE_SUMMARY,
    }


class TestTheSharedPrefixIsByteIdentical:
    def test_all_members_send_the_same_first_two_messages(self):
        client = QueueClient(_final())
        plan = _plan(3)

        run_family(client=client, provider=FakeProvider(), plan=plan, **_family_args())

        # 3 个成员 + 1 次汇总，每个都只跑一轮 → 4 次调用。
        assert len(client.calls) == 4, [len(call) for call in client.calls]
        firsts = {_sent(call[:2]) for call in client.calls}
        assert len(firsts) == 1, (
            "成员之间的共享前缀不一致 —— 缓存会全部 miss，而且不会有任何报错。"
            f"各成员的前两条消息长度：{[len(_sent(call[:2])) for call in client.calls]}"
        )

    def test_the_members_own_messages_differ(self):
        """共享前缀之后的**任务书**必须各不相同（否则分工没生效）。"""
        client = QueueClient(_final())
        plan = _plan(3)

        run_family(client=client, provider=FakeProvider(), plan=plan, **_family_args())

        tasks = [call[2]["content"] for call in client.calls]
        assert len(set(tasks)) == len(tasks), "有成员的私有段重复了"
        assert "第 1/3 个分片代理" in tasks[0]
        assert "第 2/3 个分片代理" in tasks[1]
        assert "主代理" in tasks[-1], "最后一次是汇总"

    def test_the_system_message_is_shared_with_every_other_run(self):
        """断点①那一段跨运行恒定：同一份 skill + 同一份项目知识包，与额度无关。"""
        single = QueueClient(_final())
        run_analysis(
            client=single, provider=FakeProvider(), limits=LIMITS, **_family_args()
        )
        family = QueueClient(_final())
        run_family(
            client=family, provider=FakeProvider(), plan=_plan(3), **_family_args()
        )

        assert _sent(single.calls[0][:1]) == _sent(family.calls[0][:1]), (
            "系统消息不一致 —— 那是跨运行复用的最基本的一段"
        )

    def test_at_the_same_limits_the_change_list_prefix_is_identical_too(self):
        """**额度相同**时，单代理的第一轮与子代理的共享消息是同一段字节。

        额度不同就不相同，而且**应该**不同：那一句要如实告诉模型它能花多少（见下一组
        测试）。所以这里钉的是「同额度 ⇒ 同字节」，不是「永远同字节」。
        """
        plan = _plan(3)
        single = QueueClient(_final())
        run_analysis(
            client=single,
            provider=FakeProvider(),
            limits=plan.limits,
            thresholds=None,
            **_family_args(),
        )
        family = QueueClient(_final())
        run_family(client=family, provider=FakeProvider(), plan=plan, **_family_args())

        assert _sent(single.calls[0][:2]) == _sent(family.calls[0][:2])

    def test_the_quota_number_is_a_family_constant(self):
        """额度那一句在共享消息里，所以分摊后每个成员看到的数字必须一样。

        它同时也是「自己该花多少」的唯一依据 —— 分摊后的数字比整次分析的额度小，
        所以成员不会以为自己能花掉全部额度。
        """
        plan = _plan(3)
        call = QueueClient(_final())
        run_family(
            client=call, provider=FakeProvider(), plan=plan, **_family_args()
        )
        shared = call.calls[0][1]["content"]

        assert f"可索取 {plan.limits.max_tool_requests} 次" in shared, shared[-400:]


class TestTheBreakpointsStayAtThree:
    def test_the_first_round_marks_system_shared_and_task(self):
        client = QueueClient(_final())
        run_family(client=client, provider=FakeProvider(), plan=_plan(3), **_family_args())

        for call in client.calls:
            # 第 1 轮：system + 共享 + 任务书
            assert _marked(call) == [0, 1, 2], (
                f"第 1 轮的断点不在预期位置：{_marked(call)}（共 3 条消息）"
            )

    def test_the_task_marker_moves_off_on_the_next_round(self):
        """任务书上那个断点必须能挪走，否则轮次一多就会撞上断点上限被静默丢掉。"""
        client = QueueClient(_requests({"type": "commit_detail", "commit": COMMIT}), _final())
        run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_family_args())

        # S1 跑了两轮：第 2 次调用是 [system, 共享, 任务书, assistant, 本轮]
        second = client.calls[1]
        assert len(second) == 5, [len(call) for call in client.calls]
        assert _marked(second) == [0, 1, 4], (
            f"第 2 轮的断点应当是「system + 共享 + 当前这一轮」，实际 {_marked(second)}"
        )
        assert second[0] == client.calls[0][0], "系统消息不许被重建"
        assert second[1] == client.calls[0][1], "共享消息不许被改动"

    def test_the_history_is_append_only(self):
        """第 2 轮的请求必须把第 1 轮的整段当作前缀（append-only）。"""
        client = QueueClient(_requests({"type": "commit_detail", "commit": COMMIT}), _final())
        run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_family_args())

        first, second = client.calls[0], client.calls[1]
        assert _sent(second[: len(first)]) == _sent(first), (
            "第 2 轮没有以第 1 轮的整段为前缀 —— 跨轮次的缓存命中就没了"
        )


class TestTheUnseededPathIsUnchanged:
    def test_a_plain_run_still_builds_its_own_system_message(self):
        """不传 `seed_messages` / `task_message` 时，行为与这个功能之前一样。"""
        client = QueueClient(_final())
        run_analysis(
            client=client, provider=FakeProvider(), limits=LIMITS, **_family_args()
        )

        call = client.calls[0]
        assert call[0]["role"] == "system"
        assert _marked(call) == [0, 1], f"单代理第 1 轮应当只有两个断点：{_marked(call)}"
        # 第 1 轮的 user 消息就是「变更清单 + 首轮指引」那一段（不是任务书）。
        assert "本次变更" in call[1]["content"]

    def test_a_plain_run_keeps_the_change_list_pointer_from_round_two(self):
        client = QueueClient(_requests({"type": "commit_detail", "commit": COMMIT}), _final())
        run_analysis(
            client=client, provider=FakeProvider(), limits=LIMITS, **_family_args()
        )

        assert len(client.calls) == 2
        # 第 2 轮的断点在「当前这一轮」上（第 1 轮那条的断点②不动）。
        assert _marked(client.calls[1]) == [0, 1, 3], _marked(client.calls[1])


class TestTheTaskMessageReplacesTheFirstUserMessage:
    def test_the_first_round_body_is_the_task_verbatim(self):
        plan = _plan(2)
        client = QueueClient(_final())

        run_family(client=client, provider=FakeProvider(), plan=plan, **_family_args())

        task = client.calls[0][2]["content"]
        assert "分片代理" in task
        # 第 1 轮**不再重发**变更清单（它在共享消息里，重发既挤窗口又按未命中价计费）。
        assert "本次变更共 1 个提交" not in task, task[:400]

    def test_the_change_list_is_still_there_for_later_rounds(self):
        """后续轮次的「清单指针」还在，模型不会以为自己看不到清单了。"""
        client = QueueClient(_requests({"type": "commit_detail", "commit": COMMIT}), _final())
        run_family(client=client, provider=FakeProvider(), plan=_plan(2), **_family_args())

        # 2 个成员 + 1 次汇总；S1 跑了两轮 → 4 次调用。
        assert len(client.calls) == 4
        second_round = client.calls[1][-1]["content"]
        assert "变更清单" in second_round, second_round[:400]
