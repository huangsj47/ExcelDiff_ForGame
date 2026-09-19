# -*- coding: utf-8 -*-
"""「思考过程」的数据来源：**跑的时候**读进度快照，**跑完之后**读落库的 trace。

## 这一组守的是什么

同一个面板有两条来路，它们描述的是同一件事：

* 跑动中 → `run_progress.ProgressSnapshot.rounds`（进程内的快照，每 3 秒随轮询重发）；
* 跑完后 → `ai_usage_service.run_usage()["rounds"]`（落库的 `ai_analysis_trace` 逐行）。

所以 `trace_evidence.live_round_entry(record)` 的键**必须与 `run_usage` 那一行逐字相同** ——
键名一分叉，同一个界面就会同时存在两种真相：跑的时候显示「取不到 2 条」，跑完之后同一轮
显示成空白，而两边都没报错。这个文件里最要紧的一条就是拿**真的落库行**去比键。

另外三条：截断上限（每帧的载荷）、`None` 不许变成 0（上游没上报 ≠ 没命中）、
以及**累加与「一共几轮」的账**（截断之后列表会变短，不能反过来说只跑了 8 轮）。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from services.ai import run_progress
from services.ai.budget import ContextItem
from services.ai.engine import RoundRecord
from services.ai.protocol import ContextRequest, DroppedItem
from services.ai.trace_evidence import (
    LIVE_LIST_MAX_ITEMS,
    LIVE_RESPONSE_MAX_CHARS,
    encode_evidence,
    live_round_entry,
)
from services.ai_usage_service import run_usage

# 路径与提交号都从引擎测试里借：**必须是 `_scope()` 白名单里的那一份**，否则第一轮
# 索要的上下文会被 `sanitize_requests` 当成越权丢掉，断言就变成在量一个空的索取。
from tests.test_ai_engine import COMMIT, LUA  # noqa: E402


def _record(**overrides) -> RoundRecord:
    base = dict(
        index=2,
        status="requests",
        request_count=2,
        item_count=2,
        refused_by_budget=1,
        truncated=1,
        note="上游拒了一次",
        prompt_tokens=180_000,
        completion_tokens=9_400,
        cache_read_tokens=120_000,
        cache_write_tokens=0,
        prompt_chars=210_000,
        context_chars=25_000,
        duration_ms=18_400,
        response_text='{"status": "need_more_context", "reason": "先看战斗逻辑"}',
        requests=(
            ContextRequest("file_diff", COMMIT, LUA),
            ContextRequest("file_content", COMMIT, LUA, lines="1180-1260"),
        ),
        executed=(
            ContextItem("file_diff", f"file_diff {LUA}", "代码差异：…\n+ 一行\n"),
            ContextItem(
                "file_content", f"file_content {LUA}", "正文若干", meta={"truncated": True}
            ),
        ),
        dropped=(
            DroppedItem("request", 1, "超出本次工具请求总预算（20 次），未执行", "file_diff x"),
        ),
        budget_notes=("有 1 个上下文请求因超出本次索取额度而未执行。",),
        correction_hint="你上一轮的返回不符合协议：…",
    )
    base.update(overrides)
    return RoundRecord(**base)


class TestTheShapeIsTheSameOnBothSides:
    """**这一组是这个文件存在的理由**：实时那一份与落库那一份必须同形。"""

    def test_the_live_entry_has_exactly_the_keys_of_a_stored_round(self):
        record = _record()
        with flask_app.app_context():
            create_tables()
            project = Project(code=f"P_{uuid.uuid4().hex[:8]}", name="思考过程")
            db.session.add(project)
            db.session.flush()
            run = AiAnalysisRun(
                project_id=project.id,
                target_type="commit",
                target_id=1,
                status="succeeded",
                scope="full",
                trigger_source="manual",
                response_text="结论",
                model="fake-model",
                created_at=datetime.now(timezone.utc),
            )
            db.session.add(run)
            db.session.flush()
            # **真的落库一行**（编码走的是写库侧那一处），再按读库侧读回来比。
            db.session.add(
                AiAnalysisTrace(
                    run_id=run.id,
                    round_index=record.index,
                    outcome=record.status,
                    parsed_ok=record.status != "unparsable",
                    error=record.note or None,
                    tokens_input=record.prompt_tokens,
                    tokens_output=record.completion_tokens,
                    cache_read_tokens=record.cache_read_tokens,
                    cache_write_tokens=record.cache_write_tokens,
                    request_chars=record.prompt_chars,
                    context_chars=record.context_chars,
                    duration_ms=record.duration_ms,
                    **encode_evidence(record),
                )
            )
            db.session.commit()

            stored = run_usage(run.id)["rounds"][0]
            live = live_round_entry(record)

        assert set(live) == set(stored), (
            "实时那一份与落库那一份的键不一样 —— 同一个「思考过程」面板会有两种真相"
        )
        # 取值也要对得上（键相同但语义错位同样是两种真相）。
        assert live["round_index"] == stored["round_index"] == 2
        assert live["outcome"] == stored["outcome"] == "requests"
        assert live["parsed_ok"] is True
        assert live["tokens_input"] == stored["tokens_input"] == 180_000
        assert live["requests"][0]["path"] == stored["requests"][0]["path"] == LUA
        assert live["dropped"][0]["reason"] == stored["dropped"][0]["reason"]

    def test_the_engine_fills_it_through_the_only_round_exit(self):
        """`_emit` 是每一轮的唯一出口 —— 5 条轮次路径都要带上明细。

        直接量它：跑一次真引擎（假 client 先要上下文、再给结论），每一轮报出来的进度
        都必须带 `round_entry`。漏一条路径，那条路径上的「思考过程」就是空的。
        """
        from services.ai.engine import EngineLimits, run_analysis
        from tests.test_ai_engine import FakeProvider, _final, _loaded, _requests, _scope

        class ScriptedClient:
            def __init__(self, *replies: str):
                self._replies = list(replies)
                self.calls = 0

            def complete(self, messages, *, temperature=None):
                from services.ai.llm_client import ChatResult

                index = min(self.calls, len(self._replies) - 1)
                self.calls += 1
                return ChatResult(
                    text=self._replies[index], model="fake",
                    prompt_tokens=10, completion_tokens=5,
                )

        seen: list = []
        client = ScriptedClient(_requests({"type": "file_diff", "commit": COMMIT, "path": LUA}),
                                _final())
        run_analysis(
            client=client,
            provider=FakeProvider(),
            loaded=_loaded(),
            scope=_scope(),
            change_summary="本次变更共 1 个提交。\n",
            limits=EngineLimits(max_rounds=4, max_tool_requests=6),
            on_round=seen.append,
        )

        assert seen, "一轮都没报出来"
        for progress in seen:
            assert progress.round_entry, f"第 {progress.index} 轮没有带明细"
            assert progress.round_entry["round_index"] == progress.index
        assert seen[0].round_entry["requests"], "第一轮要了上下文，明细里应当有它"


class TestTheCapsAreTighterThanTheStoredOnes:
    """实时那一份每 3 秒重发一次，**上限由载荷决定**。"""

    def test_a_long_model_reply_is_clipped(self):
        entry = live_round_entry(_record(response_text="x" * 5000))

        assert len(entry["response_text"]) == LIVE_RESPONSE_MAX_CHARS

    def test_the_lists_are_capped(self):
        record = _record(
            requests=tuple(ContextRequest("file_diff", COMMIT, f"a/{i}.lua") for i in range(40)),
            dropped=tuple(DroppedItem("request", i, "超出额度") for i in range(40)),
        )

        entry = live_round_entry(record)

        assert len(entry["requests"]) == LIVE_LIST_MAX_ITEMS
        assert len(entry["dropped"]) == LIVE_LIST_MAX_ITEMS

    def test_only_the_unreadable_items_ride_along(self):
        """`executed` 只留「取不到」与「确实没有内容」—— 那一栏要看的正是这个。

        读侧那一份是全量的；渲染器两边都只画这两类，所以显示出来一致。
        """
        record = _record(
            executed=(
                ContextItem("file_diff", "读到了", "代码差异：…\n+ 一行\n"),
                ContextItem("file_diff", "没读到", "[取数失败] 平台读不到 X 的内容"),
                ContextItem("file_content", "空的", "", meta={"tool_empty": True}),
            )
        )

        entry = live_round_entry(record)

        assert [item["label"] for item in entry["executed"]] == ["没读到", "空的"]

    def test_an_unreported_cache_field_stays_none(self):
        """**`None` 不是 `0`**：上游没报 ≠ 没命中（渲染成 0% 就是凭空造一个结论）。"""
        entry = live_round_entry(_record(cache_read_tokens=None))

        assert entry["cache_read_tokens"] is None

    def test_junk_in_never_raises(self):
        """明细算不出来时**不许抛** —— 它服务的是显示，代价不能是一次分析白跑。"""
        entry = live_round_entry(object())

        assert entry["round_index"] == 0 and entry["requests"] == []


class TestTheSnapshotAccumulates:
    """界面可能在第 5 轮才打开抽屉 —— 前面几轮必须还在。"""

    def setup_method(self):
        run_progress.reset_for_tests()

    def _progress(self, index: int, *, status: str = "requests", **overrides):
        from types import SimpleNamespace

        base = dict(
            index=index, max_rounds=8, status=status, prompt_tokens=10, completion_tokens=5,
            cache_read_tokens=None, cache_write_tokens=None, requests_used=index,
            requests_remaining=8 - index, items_chars=100, elapsed_ms=1000,
            agent="", agent_index=0, agent_total=0,
            # 明细里的 `outcome` 与进度上的 `status` 是同一个事实，别只在一边改。
            round_entry=live_round_entry(_record(index=index, status=status)),
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_two_rounds_accumulate(self):
        run_progress.publish(7, 1, self._progress(1))
        run_progress.publish(7, 1, self._progress(2))

        snap = run_progress.snapshot(7)

        assert [item["round_index"] for item in snap.rounds] == [1, 2]
        assert snap.rounds_seen == 2 and snap.rounds_truncated is False

    def test_the_same_round_reported_twice_replaces_instead_of_appending(self):
        """一次重问/重试可能让同一个轮次号出现两次 —— 界面上该是「一轮一行」。"""
        run_progress.publish(7, 1, self._progress(1))
        run_progress.publish(7, 1, self._progress(1, status="final"))

        snap = run_progress.snapshot(7)

        assert len(snap.rounds) == 1
        assert snap.rounds[0]["outcome"] == "final"
        assert snap.rounds_seen == 1, "同一轮报两次不该让「一共几轮」变成 2"

    def test_the_next_shard_starting_over_at_round_one_does_not_eat_the_previous_one(self):
        """**子代理模式下少了这一条，实时面板会少一轮。**

        每个分片的引擎都从第 1 轮开始编号（引擎不知道自己在第几个分片里），所以
        「S1 的第 2 轮」之后紧跟的是「S2 的第 1 轮」。判重若只看 `round_index`，
        这一条会被当成「上一轮的重报」而**替换掉 S1 的最后一轮** —— 面板上那轮就没了，
        而落库那份（重编号成家族全局序号）两轮都在：同一个过程，两个来源画出两样东西。
        """
        run_progress.publish(7, 1, self._progress(1, agent="S1", agent_index=1, agent_total=2))
        run_progress.publish(7, 1, self._progress(2, agent="S1", agent_index=1, agent_total=2))
        run_progress.publish(7, 1, self._progress(1, agent="S2", agent_index=2, agent_total=2))

        snap = run_progress.snapshot(7)

        assert [(item["agent"], item["round_index"]) for item in snap.rounds] == [
            ("S1", 1), ("S1", 2), ("S2", 1),
        ], "换分片了：新分片的第 1 轮不能顶掉上一个分片的最后一轮"
        assert snap.rounds_seen == 3

    def test_a_long_run_keeps_the_last_rounds_and_says_it_truncated(self):
        for index in range(1, 21):
            run_progress.publish(7, 1, self._progress(index))

        snap = run_progress.snapshot(7)

        assert len(snap.rounds) == run_progress.MAX_LIVE_ROUNDS
        assert [item["round_index"] for item in snap.rounds][0] == 20 - run_progress.MAX_LIVE_ROUNDS + 1
        assert snap.rounds_truncated is True
        # 「一共跑过 20 轮」不能因为只留了 8 条就说成 8。
        assert snap.rounds_seen == 20

    def test_the_shard_label_is_filled_from_the_progress(self):
        """引擎不知道自己在哪个分片里（标签由 subagent 在回调外层贴），进度对象知道。"""
        run_progress.publish(7, 1, self._progress(2, agent="S1", agent_index=1, agent_total=3))

        entry = run_progress.snapshot(7).rounds[0]

        assert entry["agent"] == "S1", "实时那一份缺分片标签，与落库那份对不上"

    def test_a_junk_progress_still_publishes_the_numbers(self):
        """老调用方/测试替身没有 `round_entry`：数字照报，只是没有过程可看。"""
        class _Bare:
            index = 1
            max_rounds = 8
            status = "requests"

        run_progress.publish(7, 1, _Bare())

        snap = run_progress.snapshot(7)

        assert snap.rounds == () and snap.rounds_seen == 0
        assert snap.index == 1

    def test_the_payload_carries_the_rounds(self):
        run_progress.publish(7, 1, self._progress(1))

        payload = run_progress.snapshot(7).to_dict()

        assert payload["rounds"][0]["round_index"] == 1
        assert payload["rounds_seen"] == 1 and payload["rounds_truncated"] is False

    def test_it_is_a_plain_json_payload(self):
        """整条快照要能被 jsonify —— 里面混进一个 dataclass 就是接口 500。"""
        run_progress.publish(7, 1, self._progress(1))

        text = json.dumps(run_progress.snapshot(7).to_dict(), ensure_ascii=False)

        assert "round_index" in text
