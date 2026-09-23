# -*- coding: utf-8 -*-
"""子代理模式的**端到端接线**：配置打开之后，那一次周版本分析真的走了分片。

## 为什么要有这一组（而不是只测 `subagent.py`）

`subagent.py` 的测试都直接调它，所以「服务层有没有把它接上」在那些测试里是看不见的 ——
而这里最容易出的错恰恰是接线：`_run_engine_and_persist` 把同一份参数字典喂给两条路
（单代理 / 子代理），只要有一个键名对不上就是 `TypeError`，而它发生在**一次真跑里**。

所以这组测试走的是真实的那条链：`run_weekly_analysis_background` → `_execute_analysis`
→ `_run_engine_and_persist` → 引擎 / 编排 → 落库，只有 HTTP 是假的
（`build_endpoint_client` 换成假 client，与 `tests/test_ai_run_budget_warning.py` 同一套手法）。
"""
from __future__ import annotations

import json

import pytest

from app import app as flask_app
from app import create_tables, db
from dataclasses import replace
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from services import ai_analysis_service as ai_service
from models.ai_analysis import AiProjectAnalysisConfig
from services.ai_analysis_service import build_weekly_group_key
from tests.test_ai_analysis_service import COMMIT_SHA, TABLE_PATH, _FakeClient
from tests.test_ai_run_budget_warning import _prepare_weekly_run


def _enable_subagents(project_id: int) -> None:
    """打开子代理模式。

    2026-09-23 配置面收敛后 `subagent_count` 不可再提交（服务端收到即 400），分片数由
    `auto_sizing` 推导（默认 5 片）；**要控制片数用 `_force_shard_count`**（改推导
    结果），不是改配置行。
    """
    ai_service.update_project_analysis_config(
        project_id, {"subagent_enabled": True}
    )
    db.session.commit()


def _force_shard_count(monkeypatch, count: int) -> None:
    """把推导出的分片数钉在 `count` 片。

    收敛后这是**唯一**能控制片数的入口：运行侧从 `ai_service.derive_family_sizing`
    读推导值，库里的 subagent_count 列已被忽略。直接改配置行的老写法会写出
    「存了但没生效」—— 正是 RETIRED_FIELDS 要消灭的那种状态。
    """
    real = ai_service.derive_family_sizing

    def fake(**kwargs):
        sizing = real(**kwargs)
        return replace(
            sizing,
            shard_count=count,
            family_requests_pool=count * sizing.requests_per_shard,
            family_rounds_pool=count * sizing.rounds_per_shard,
        )

    monkeypatch.setattr(ai_service, "derive_family_sizing", fake)


def _disable_subagents(project_id: int) -> None:
    """**显式关掉**子代理模式。

    默认值现在是「开」（`DEFAULT_SUBAGENT_ENABLED`），所以「没开子代理时怎样怎样」
    这类用例必须自己关掉它 —— 靠默认值等于把用例绑在默认值上，改一次默认值就红一片，
    而它要测的行为（关掉之后逐字节回到单代理）一点没变。
    """
    ai_service.update_project_analysis_config(
        project_id, {"subagent_enabled": False}
    )
    db.session.commit()


def _run(monkeypatch, *, client=None):
    monkeypatch.setattr(
        ai_service, "build_endpoint_client", lambda *a, **k: (client or _FakeClient(), [])
    )
    return client or None


def test_the_family_runs_and_persists_as_one_run(monkeypatch):
    """配置打开 → 一家子（2 个分片 + 1 次汇总）跑完，落库是**一条**运行。"""
    client = _FakeClient()
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        _enable_subagents(project.id)
        _force_shard_count(monkeypatch, 2)
        _run(monkeypatch, client=client)

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "succeeded", outcome

        # 假 client 每次都答 final → 2 个成员 + 1 次汇总 = 3 次模型调用。
        assert len(client.calls) == 3, (
            f"分片没有真的跑起来（只调了 {len(client.calls)} 次模型）"
        )
        # 每个成员的第 1 轮请求前两条消息必须完全相同（省钱的依据）。
        shared = {json.dumps(call[:2], ensure_ascii=False) for call in client.calls}
        assert len(shared) == 1, "共享前缀在真实链路里不一致 —— 缓存会全部 miss"

        run = (
            AiAnalysisRun.query.filter_by(
                target_type="weekly", target_key=build_weekly_group_key(cfg)
            )
            .order_by(AiAnalysisRun.id.desc())
            .first()
        )
        assert run is not None
        assert run.subagent_mode == "subagents"
        assert run.subagent_count == 2

        traces = (
            AiAnalysisTrace.query.filter_by(run_id=run.id)
            .order_by(AiAnalysisTrace.round_index.asc())
            .all()
        )
        assert [row.agent for row in traces] == ["S1", "S2", None]
        assert [row.round_index for row in traces] == [1, 2, 3]

        payload = json.loads(run.response_payload)
        assert [item["label"] for item in payload["subagents"]] == ["S1", "S2", "汇总"]


def test_the_same_batch_stays_a_single_agent_when_the_flag_is_off(monkeypatch):
    """**关掉之后**，请求与这个功能上线之前完全一样（只有一次模型调用）。

    显式关掉而不是「不配置」：默认值已经改成「开」了，靠默认值来测「关」的行为，
    改一次默认值这条用例就红 —— 而它守的东西（关掉 = 单代理）没有变。
    """
    client = _FakeClient()
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        _disable_subagents(project.id)
        _run(monkeypatch, client=client)

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "succeeded", outcome
        assert len(client.calls) == 1, "没开子代理却跑了多次 —— 默认行为被改动了"

        run = (
            AiAnalysisRun.query.filter_by(
                target_type="weekly", target_key=build_weekly_group_key(cfg)
            )
            .order_by(AiAnalysisRun.id.desc())
            .first()
        )
        assert run is not None
        assert run.subagent_mode is None and run.subagent_count is None


def test_a_commit_analysis_never_splits_even_when_enabled(monkeypatch):
    """**只对周版本生效**：单个提交的分析不拆（那一次的规模本来就不需要分工）。

    直接驱动执行器并给它一个 `mode=commit` 的载荷 —— 这正是服务层读的那个字段，
    所以它同时钉住了「服务层把模式传对了」。
    """
    client = _FakeClient()
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        _enable_subagents(project.id)
        _run(monkeypatch, client=client)

        run = AiAnalysisRun(
            project_id=project.id,
            target_type="commit",
            target_id=1,
            target_key=None,
            status="running",
        )
        db.session.add(run)
        db.session.commit()

        result = ai_service._execute_analysis(
            run,
            project_id=project.id,
            payload={
                "mode": "commit",
                "commit": {
                    "commit_id": COMMIT_SHA,
                    "path": TABLE_PATH,
                    "message": "改了道具表",
                    "operation": "M",
                },
            },
            project_config=ai_service.get_project_analysis_config(project.id),
            target_type="commit",
            target_key=None,
        )

        assert result["status"] == "succeeded", result
        assert len(client.calls) == 1, "单提交分析被拆片了 —— 它只该有一条路径"
        assert run.subagent_mode is None


@pytest.mark.parametrize("frozen", [1, 0])
def test_a_frozen_dirty_count_no_longer_changes_the_weekly_run(monkeypatch, frozen):
    """库里冻结的 `subagent_count` 脏值（1 或 0）**不再影响周版本运行**。

    收敛（2026-09-23）之前这一列还能把分片数压成 1（退化成单代理）；现在周版本的分片
    数一律由 `auto_sizing` 推导、这一列被忽略 —— 老行里的 0/1 只是一段死数据。
    这里同时守住「死数据不炸、也不生效」两个方向。
    """
    client = _FakeClient()
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        _enable_subagents(project.id)
        row = AiProjectAnalysisConfig.query.filter_by(project_id=project.id).first()
        row.subagent_count = frozen  # 直接改列：模拟收敛之前的老行
        db.session.commit()
        _run(monkeypatch, client=client)

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "succeeded", outcome
        # 推导目标 5 片 + 汇总 = 6 次模型调用 —— 脏列没有把分片压没。
        assert len(client.calls) == 6, (
            f"冻结的脏 subagent_count={frozen} 仍然在起作用（只调了 {len(client.calls)} 次）"
        )
        run = (
            AiAnalysisRun.query.filter_by(
                target_type="weekly", target_key=build_weekly_group_key(cfg)
            )
            .order_by(AiAnalysisRun.id.desc())
            .first()
        )
        assert run.subagent_count == 5, "落库记的应是推导值，不是库里的冻结值"


def _enable_verify(project_id: int, *, enabled: bool) -> None:
    ai_service.update_project_analysis_config(project_id, {"subagent_verify": enabled})
    db.session.commit()


def test_the_verify_round_runs_and_lands_on_the_same_run(monkeypatch):
    """开了对账轮：多跑**一次**模型调用（标签 V1），报告里多出「对账结果」那一节。

    这里量的是接线：配置项从库里读出来、传进 `plan_family`、编排跑完、再落进同一条运行
    （`uq_ai_trace_run_round` 靠家族内全局递增的 `round_index` 撑住 —— 对账轮是第 4 轮）。
    """
    client = _FakeClient()
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        _enable_subagents(project.id)
        _force_shard_count(monkeypatch, 2)
        _enable_verify(project.id, enabled=True)
        _run(monkeypatch, client=client)

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "succeeded", outcome
        # 2 个分片 + 1 次汇总 + 1 次对账
        assert len(client.calls) == 4, f"对账轮没有跑起来（只调了 {len(client.calls)} 次）"
        # 对账轮同样吃共享前缀（省钱的机制对它一样成立）。
        shared = {json.dumps(call[:2], ensure_ascii=False) for call in client.calls}
        assert len(shared) == 1, "对账轮没有用共享前缀 —— 缓存会 miss"

        run = (
            AiAnalysisRun.query.filter_by(
                target_type="weekly", target_key=build_weekly_group_key(cfg)
            )
            .order_by(AiAnalysisRun.id.desc())
            .first()
        )
        traces = (
            AiAnalysisTrace.query.filter_by(run_id=run.id)
            .order_by(AiAnalysisTrace.round_index.asc())
            .all()
        )
        assert [row.agent for row in traces] == ["S1", "S2", None, "V1"]
        assert [row.round_index for row in traces] == [1, 2, 3, 4]
        # 「对账轮落在**同一条** run 上」是这一条要保的不变量（`uq_ai_trace_run_round` 靠
        # 家族内全局递增的 `round_index` 撑住）—— 上面那个查询按 `run_id=run.id` 过滤，
        # 这里把它写成明文断言：对账轮不许另起一条运行。
        assert all(row.run_id == run.id for row in traces), "对账轮落到了另一条 run 上"

        payload = json.loads(run.response_payload)
        assert [item["label"] for item in payload["subagents"]] == ["S1", "S2", "汇总", "V1"]
        assert payload["subagents"][-1]["role"] == "verify"
        # AI-P1-01（2026-09-21）：对账轮的**原文不再进报告正文** —— 正文里只留平台那几节
        # 规范结论，读的人不必自己辨认哪一句已经被裁决改掉。但原文一个字节都不许少：
        # 它作为独立存档落进结论载荷的 `verify_report_markdown`（默认不渲染）。
        assert "## 对账结果（找反证）" not in run.response_text, (
            "对账轮原文又回到了报告正文里（AI-P1-01：正文只能有一份规范结论）"
        )
        archived = payload["verify_report_markdown"]
        assert archived.startswith("## 对账结果（找反证）"), (
            "对账轮的原文没有落进它自己的键 —— 那次复核说了什么就再也读不到了"
        )
        assert "道具表删了一行。" in archived, "存档要逐字保留模型交回的那段正文"


def test_the_verify_flag_does_nothing_on_its_own(monkeypatch):
    """**对账轮依附在子代理模式上**：没开子代理时它一个字都不生效。

    否则「开了对账轮但没开子代理」会变成一次**没有任何分片**的独立复核 —— 那既不是
    用户想要的，也没有可核对的「几个分片各自的结论」。
    """
    client = _FakeClient()
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        _disable_subagents(project.id)          # 前提：子代理模式是关的
        _enable_verify(project.id, enabled=True)
        _run(monkeypatch, client=client)

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "succeeded", outcome
        assert len(client.calls) == 1, "没开子代理却多跑了对账轮"
        assert "对账结果" not in (outcome.get("report_markdown") or "")


def test_the_verify_default_is_off(monkeypatch):
    """老库上这一列是 NULL → 读成「关」。**它是唯一安全的默认值**（一次额外调用）。"""
    with flask_app.app_context():
        create_tables()
        _ai_service, project, _cfg = _prepare_weekly_run(monkeypatch)

        cfg_data = ai_service.get_project_analysis_config(project.id)

        assert cfg_data["subagent_verify"] is False


def test_the_main_agent_announces_itself_before_it_starts(monkeypatch):
    """**「主代理在分析了」这件事必须报出去，而不是继续挂着上一个分片的名字。**

    `on_round` 只在每一轮**跑完**之后才发，所以汇总那一次开始跑时，快照里留的还是
    最后一个分片 —— 界面写着「分片 S2 (2/3) · 第 1 轮」，而主代理其实已经在跑了，
    这一跑可能是几分钟。用户看到的现象就是「思考过程不动了，状态还停在分片」。

    修法是让每一次 `run_analysis` 在第一次模型调用之前报一帧（引擎的 `on_start`），
    归属由 `_call_engine` 的那个 `report` 贴上去 —— 汇总那一次的 `agent` 是空串、
    `agent_index == agent_total`，界面据此念「汇总」。

    判据用「帧的序列」而不是「某一帧存在」：只断言存在的话，把这些帧排错顺序
    （比如汇总的开始帧发在它自己那一轮之后）照样能绿。
    """
    published: list = []
    client = _FakeClient()
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        _enable_subagents(project.id)
        _force_shard_count(monkeypatch, 2)
        _run(monkeypatch, client=client)
        monkeypatch.setattr(
            ai_service, "publish_run_progress",
            lambda _run_id, _project_id, progress: published.append(progress),
        )

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "succeeded", outcome

    # 每个成员：一帧「开始」+ 一帧「第 1 轮」。2 个分片 + 1 次汇总 = 6 帧。
    starts = [p for p in published if p.index == 0]
    assert len(starts) == 3, (
        f"开始帧的数量不对（{[(p.agent, p.agent_index) for p in starts]}）—— "
        "少了就等于某个成员在跑的时候界面还挂着上一个的名字"
    )

    # 汇总那一次的开始帧：`agent` 空、位次等于总数 —— 与 subagent/ai_stream_status
    # 两处共同的判定口径一致（界面据此念「汇总」）。
    synthesis_start = starts[-1]
    assert synthesis_start.agent == "", f"汇总的开始帧带了分片名：{synthesis_start.agent}"
    assert synthesis_start.agent_index == synthesis_start.agent_total == 3

    # 而且它必须在**汇总那一次的第一个模型调用之前**发 —— 排在分片的帧后面。
    labels = [(p.agent, p.index) for p in published]
    assert labels.index(("", 0)) > labels.index(("S2", 1)), (
        f"汇总的开始帧没有排在最后一个分片之后：{labels}"
    )
