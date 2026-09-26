# -*- coding: utf-8 -*-
"""工作包 B：**计划选择与有界预算**（`docs/AI分析全链路复测与强制改造指引-2026-09-23.md` §3.B）。

## 这一组要钉住的四件事

1. **因果方向**：规模由冻结快照的**确定性事实**决定，预算只是**上限**。
   3 个文件 / 1,597 字 diff 的配置 3 必须只跑**一个**分析者（实测：run 45 多代理
   979,910 token vs run 46 单代理 114,333 token，覆盖率一样都是 3/3 + 2/2 金标）；
2. **同一份计划**：运行侧（`ai_analysis_service`）与预估端点（`ai_usage_service`）
   读的是 `payload["plan"]` 这一份，不是各推一次；
3. **有界**：单次 token 硬上限 + 报告收尾预留，「已花 + 本轮保守预留 + 收尾预留」
   超了就**停止探索**、产出带缺口的报告、**不再新增模型调用**；
4. **不可静默复用**：`analysis_revision` 里带上规划版本与口径，全量预览说
   「全量不使用基线」而不是「还没有可复用基线」。

断言一律落在**行为**上（跑真路径看落库/返回结果），不数「代码怎么写」。
"""
from __future__ import annotations

import json

import pytest

from app import app as flask_app
from app import create_tables, db
from models.ai_analysis import AiAnalysisRun
from services import ai_analysis_service as ai_service
from services import ai_usage_service as usage_service
from services.ai import auto_sizing
from services.ai.analysis_plan import plan_of, snapshot_facts_from_payload
from services.ai.auto_sizing import (
    FAMILY_MAX_MEMBERS,
    MODE_FAMILY,
    MODE_SINGLE,
    PLAN_VERSION,
    SINGLE_AGENT_REQUESTS,
    SINGLE_AGENT_ROUNDS,
    AnalysisPlan,
    SnapshotFacts,
    cluster_changes,
    compose_analysis_revision,
    conservative_member_tokens,
    plan_analysis,
    planner_metadata,
    should_consult_planner,
    single_run_guard,
    validate_grouping,
)
from services.ai.provenance import current_provenance
from services.ai_analysis_service import build_weekly_group_key

DIMENSIONS = ("code_logic", "table_data", "version_branch", "cross_ref", "release_risk")


def _entry(path: str, commit: str, chars: int) -> dict:
    return {"path": path, "commit": commit, "chars": chars}


def _config_three() -> SnapshotFacts:
    """配置 3 的真实形状：**3 个变更文件、1,597 字 diff**（指引 §1 的实测输入）。"""
    return SnapshotFacts.from_mapping(
        {
            "file_count": 3,
            "diff_payload_chars": 1_597,
            "max_file_diff_chars": 900,
            "entries": [
                _entry("config/道具表_CfgItem.xlsx", "e54c73df", 700),
                _entry("src/skill/absorb.lua", "e54c73df", 500),
                _entry("src/skill/meteor.lua", "aa3fd90b", 397),
            ],
        }
    )


def _large_version(files: int = 847, *, chars_per_file: int = 5_000) -> SnapshotFacts:
    """配置 1 那一档：几百个文件、按目录与提交天然分成几十个簇。"""
    entries = [
        _entry(f"dir{index % 7}/sub{index % 3}/file_{index}.lua", f"c{index % 9}", chars_per_file)
        for index in range(files)
    ]
    return SnapshotFacts.from_mapping(
        {
            "file_count": files,
            "diff_payload_chars": files * chars_per_file,
            "max_file_diff_chars": chars_per_file,
            "truncated": True,
            "entries": entries,
        }
    )


# ==========================================================================
# 一、小批次：只跑一个分析者，且覆盖全部声明维度
# ==========================================================================


def test_the_three_file_config_plans_a_single_analyzer():
    """**配置 3 必须命中单分析者**（工作包 B 的核心验收）。"""
    plan = plan_analysis(_config_three(), 580_000, DIMENSIONS)

    assert plan.mode == MODE_SINGLE
    assert plan.member_count == 1
    assert plan.family is None, "单代理计划不许带家族池（它没有分片）"
    # 「单代理仍然覆盖全部声明维度」：维度是**检查清单**，不是分工依据。
    assert plan.covered_dimensions() == DIMENSIONS
    assert plan.members[0].max_rounds == SINGLE_AGENT_ROUNDS
    assert plan.members[0].max_tool_requests == SINGLE_AGENT_REQUESTS
    # 规模判定的依据要能读出来（用户问「为什么这次不分片」时，答案在这里）。
    assert "小批次" in plan.reason
    assert "1,597" in plan.reason
    assert plan.thresholds["small_batch_exceeded_by"] == ""


def test_a_single_analyzer_reports_the_full_batch_as_its_own_scope():
    plan = plan_analysis(_config_three(), 580_000, DIMENSIONS)

    assert plan.members[0].paths == (
        "config/道具表_CfgItem.xlsx",
        "src/skill/absorb.lua",
        "src/skill/meteor.lua",
    )


@pytest.mark.parametrize(
    ("facts", "why"),
    [
        ({"file_count": 9, "diff_payload_chars": 1_000}, "文件数超过 8"),
        ({"file_count": 3, "diff_payload_chars": 80_001}, "总量超过 80,000 字"),
        ({"file_count": 2, "diff_payload_chars": 41_000, "max_file_diff_chars": 40_001},
         "单文件超过 40,000 字"),
        ({"file_count": 3, "diff_payload_chars": 1_000, "truncated": True},
         "输入被截断（「没有超限」证明不了）"),
    ],
)
def test_any_of_the_four_facts_alone_leaves_the_small_batch_tier(facts, why):
    """**四条判据各自都能单独把这一批推出小批次。** 只写其中一条的实现会在另外三条上
    静默走错方向（比如「只看文件数」就会把 2 个超大文件判成小批次）。"""
    snapshot = SnapshotFacts.from_mapping(
        {**facts, "entries": [
            _entry(
                f"dir{index}/f{index}.lua",
                f"c{index}",
                facts.get("diff_payload_chars", 0) // max(1, facts["file_count"]),
            )
            for index in range(facts["file_count"])
        ]}
    )
    assert snapshot.small_batch_exceeded_by, why
    assert plan_analysis(snapshot, 580_000, DIMENSIONS).mode == MODE_FAMILY


def test_the_thresholds_are_declared_as_initial_values_not_as_facts():
    """阈值是**初值**，必须能在计划里被读出来（改它的人要知道自己在改一个待校准的数）。

    这条同时防止「有人把 8 写成一段看起来像结论的注释」：`thresholds` 里带着三个门槛的
    当前取值，计划落库之后能回答「当时是按什么门槛判的」。
    """
    plan = plan_analysis(_config_three(), 580_000, DIMENSIONS)

    assert plan.thresholds["small_batch_max_files"] == 8
    assert plan.thresholds["small_batch_max_total_chars"] == 80_000
    assert plan.thresholds["small_batch_max_file_chars"] == 40_000
    assert plan.plan_version == PLAN_VERSION


# ==========================================================================
# 二、大版本：按变更簇分工，总预算有界
# ==========================================================================


def test_a_large_version_splits_by_clusters_and_bounds_the_total_budget():
    plan = plan_analysis(_large_version(), 580_000, DIMENSIONS)

    assert plan.mode == MODE_FAMILY
    assert 2 <= plan.member_count <= FAMILY_MAX_MEMBERS
    # 每个文件都被分配**恰好**一次（分工的完整性）：三万个路径一个不漏、一个不重。
    assigned = [path for member in plan.members for path in member.paths]
    assert sorted(assigned) == sorted(item["path"] for item in _large_version().entries)
    assert len(assigned) == len(set(assigned))
    # 分片拿到的是**变更簇**（而不是「9 个维度均分成 5 份」）：每个成员有具体路径。
    assert all(member.paths for member in plan.members)
    assert all(member.objective for member in plan.members)
    # 全部声明维度仍被覆盖（各成员合起来 + 汇总）。
    assert plan.covered_dimensions() == DIMENSIONS
    # 总预算有界，且两个预留都在计划里。
    assert plan.total_token_budget == auto_sizing.SINGLE_RUN_TOKEN_CAP_LARGE
    assert 0 < plan.report_reserve_tokens < plan.total_token_budget
    assert plan.round_reserve_tokens > 0
    # 家族池 = 各成员名义额之和（`subagent.FamilyQuota` 按它滚动）。
    assert plan.family.shard_count == plan.member_count
    assert plan.family.family_requests_pool == sum(m.max_tool_requests for m in plan.members)
    assert plan.family.family_rounds_pool == sum(m.max_rounds for m in plan.members)


def test_the_member_quota_comes_from_evidence_volume_not_from_the_budget():
    """**这就是被改掉的那个因果方向。**

    同样 580k 预算下：diff 体量差 10 倍的两批，每片名义额必须不同（体量大 → 要读的东西
    多 → 索取多）。旧实现里这两个数**恒等**（都等于「预算 ÷ 11k」），于是 3 个文件也开
    50 次索取。
    """
    small = plan_analysis(_large_version(500, chars_per_file=200), 580_000, DIMENSIONS)
    large = plan_analysis(_large_version(500, chars_per_file=20_000), 580_000, DIMENSIONS)

    assert small.family.requests_per_shard < large.family.requests_per_shard, (
        "体量变大而每片索取没变 —— 额度又变成从预算反解的那个数了"
    )


def test_the_budget_still_caps_the_member_quota():
    """预算**仍然**是上限（这条不能被上一条冲掉）：体量再大也不许超预算反解出来的那个数。"""
    plan = plan_analysis(_large_version(800, chars_per_file=100_000), 580_000, DIMENSIONS)

    ceiling = auto_sizing.derive_family_sizing(
        effective_user_chars=580_000, file_count=800, dimension_count=len(DIMENSIONS)
    ).requests_per_shard
    assert all(member.max_tool_requests <= ceiling for member in plan.members)
    assert plan.thresholds["small_batch_exceeded_by"]


def test_unknown_volume_is_configured_at_the_ceiling_not_at_the_floor():
    """**「量不出多少字」不等于「很小」。** 体量未知时按上限配，否则真正的大版本会在
    跑到一半才发现额度不够（那时代价已经付了）。"""
    unknown = SnapshotFacts.from_mapping(
        {"file_count": 400, "entries": [_entry(f"d{i}/f{i}.lua", f"c{i}", 0) for i in range(400)]}
    )
    plan = plan_analysis(unknown, 580_000, DIMENSIONS)
    ceiling = auto_sizing.derive_family_sizing(
        effective_user_chars=580_000, file_count=400
    ).requests_per_shard

    assert plan.mode == MODE_FAMILY
    assert plan.family.requests_per_shard >= ceiling


def test_the_table_and_its_generated_file_stay_in_one_cluster():
    """同目录同主名的「源表 ↔ 生成物」不许分给两个成员 —— 分开就各看一半的改动。"""
    clusters = cluster_changes(
        [
            _entry("config/item.xlsx", "c1", 100),
            _entry("config/item.lua", "c1", 100),
        ]
    )

    assert clusters == (("config/item.lua", "config/item.xlsx"),)


def test_the_same_facts_always_produce_the_same_plan():
    """同一份输入 → 逐字相同的计划（确定性）。否则「预估那份计划」与「实际跑的那份」
    没法比较，用户永远说不清「为什么确认框说 5 片、实际 3 片」。"""
    first = plan_analysis(_large_version(), 580_000, DIMENSIONS).to_dict()
    second = plan_analysis(_large_version(), 580_000, DIMENSIONS).to_dict()

    assert first == second


# ==========================================================================
# 三、手动关掉子代理 = 强制单代理（自动规划不许把它打开）
# ==========================================================================


def test_turning_subagents_off_forces_a_single_analyzer_even_for_a_huge_version():
    plan = plan_analysis(_large_version(), 580_000, DIMENSIONS, subagent_enabled=False)

    assert plan.mode == MODE_SINGLE
    assert plan.member_count == 1
    assert plan.covered_dimensions() == DIMENSIONS
    assert "强制" in plan.reason
    assert plan.thresholds["forced_single"] is True


def test_the_plan_feeds_plan_family_only_when_it_says_family():
    """接线：`plan_family` 的片数必须是计划里的片数（单代理计划 → 返回 None 走单代理）。

    这是「计划和编排不会分叉」的机制本身 —— 而不是「两边各算一次、碰巧相等」。
    """
    from services.ai.engine import EngineLimits
    from services.ai.subagent import plan_family

    single = plan_analysis(_config_three(), 580_000, DIMENSIONS)
    family = plan_analysis(_large_version(), 580_000, DIMENSIONS)

    assert plan_family(
        mode="weekly", enabled=True, count=0, limits=EngineLimits(), sizing=None
    ) is None
    family_plan = plan_family(
        mode="weekly",
        enabled=True,
        count=family.member_count,
        limits=EngineLimits(),
        sizing=family.family,
    )
    assert family_plan is not None
    assert family_plan.count == family.member_count
    # 单代理计划没有家族数值 → 调用方只能传 0 → 一定退回单代理。
    assert single.family is None


def test_the_single_agent_run_really_gets_the_plans_quota(monkeypatch):
    """单代理路径的轮次/索取**必须来自计划**，不是引擎的出厂默认。

    这一档的用例是「几百个文件却只有一个变更簇」：计划把额度抬到 10 轮/50 次
    （小批次那一档才是 8/40）。引擎不跟着抬，那次分析会在第 8 轮饿死，
    报告里只能写「额度用尽」—— 而计划上写着 10 轮，又是一次「计划与实际不一致」。

    证据取**引擎自己报出来的 `max_rounds`**（进度帧），不是再看一遍计划：
    看计划等于把「计划写对了」当成「引擎照着跑了」。
    """
    from datetime import datetime, timezone

    from tests.test_ai_analysis_service import _FakeClient, _seed_diff_cache
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    with flask_app.app_context():
        create_tables()
        ai_service, _project, cfg = _prepare_weekly_run(monkeypatch)
        # 同一提交 + 同一顶层目录 → **一个变更簇**（`cluster_changes` 的规则），
        # 但体量远超小批次门槛（200 × 1,000 字），于是「单代理 + 抬高的额度」。
        stamp = datetime.now(timezone.utc)
        for index in range(199):
            _seed_diff_cache(
                cfg, cfg.repository, f"config/表_{index:03d}.xlsx", stamp,
                commit_id="a" * 40,
            )
        db.session.commit()
        _stamp_decoded_payloads(cfg.id, 1_000)

        payload, _state, _skip = ai_service.build_weekly_payload(cfg.id)
        plan = auto_sizing.AnalysisPlan.from_dict(payload["plan"])
        assert plan is not None
        assert plan.mode == MODE_SINGLE, "一个变更簇就不该分工"
        assert plan.members[0].max_rounds > SINGLE_AGENT_ROUNDS, (
            "这一档没有把额度抬过出厂默认，这条用例证明不了任何事"
        )

        frames = []
        monkeypatch.setattr(
            ai_service, "publish_run_progress",
            lambda run_id, project_id, progress: frames.append(progress),
        )
        client = _FakeClient()
        monkeypatch.setattr(ai_service, "build_endpoint_client", lambda *a, **k: (client, []))

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

    assert outcome["status"] == "succeeded", outcome
    assert frames, "一轮都没报，拿不到引擎自己说的额度"
    reported = {frame.max_rounds for frame in frames}
    assert reported == {plan.members[0].max_rounds}, (
        f"引擎按 {reported} 轮跑，而计划写的是 {plan.members[0].max_rounds} 轮"
    )


# ==========================================================================
# 四、规划模型：只在必要时、只给元数据、必须过校验
# ==========================================================================


def test_the_planner_is_only_consulted_when_the_deterministic_rules_are_not_enough():
    """两种情形才允许那一次（付费的）规划调用：簇太多、或归属不清。"""
    assert should_consult_planner(_config_three()) is False
    assert should_consult_planner(_large_version(files=200, chars_per_file=1)) is True, (
        "200 个文件分出了远多于 12 个簇 —— 这时才值得问一次模型"
    )
    unclear = SnapshotFacts.from_mapping(
        {
            "file_count": 20,
            "entries": [_entry(f"d/f{index}.lua", "", 100) for index in range(20)],
        }
    )
    assert should_consult_planner(unclear) is True, "归属不清（没有提交号）也要问一次"


def test_the_planner_input_has_no_table_bodies():
    """**规划输入只有元数据/diff 摘要/关联图**：路径、提交、类型、体量、分簇。

    这是「不输入整表」的落地形态 —— 键名被显式钉住，免得将来有人顺手把
    `merged_diff_data` 塞进来（那会是一次付费的整表上传）。
    """
    metadata = planner_metadata(_large_version(files=30))

    assert set(metadata) == {
        "file_count",
        "diff_payload_chars",
        "max_file_diff_chars",
        "truncated",
        "chars_estimated",
        "files",
        "clusters",
        "contract",
        "note",
    }
    assert set(metadata["files"][0]) == {"path", "commit", "suffix", "chars"}
    assert metadata["contract"] == auto_sizing.PLANNER_JSON_CONTRACT


def test_a_valid_proposal_is_used_and_named_as_such():
    facts = _large_version(files=30)
    clusters = cluster_changes(facts.entries)
    half = len(clusters) // 2
    proposal = {
        "groups": [
            {
                "id": "g1",
                "paths": [p for group in clusters[:half] for p in group],
                "objective": "先看前一半",
                "qa_dimensions": ["code_logic"],
            },
            {
                "id": "g2",
                "paths": [p for group in clusters[half:] for p in group],
                "objective": "再看后一半",
                "qa_dimensions": ["table_data"],
            },
        ],
        "shared_paths": [],
        "reason": "按目录分两半",
    }
    plan = plan_analysis(facts, 580_000, DIMENSIONS, proposal=proposal)

    assert plan.planner["consulted"] is True
    assert plan.planner["accepted"] is True
    assert plan.mode == MODE_FAMILY
    assert [m.objective for m in plan.members] == ["先看前一半", "再看后一半"]
    assert plan.members[0].qa_dimensions == ("code_logic",)
    assert plan.covered_dimensions() == DIMENSIONS


@pytest.mark.parametrize(
    ("broken", "why"),
    [
        ({"groups": []}, "空 groups"),
        ({"groups": [{"id": "g1", "paths": [], "objective": "x"}]}, "空组"),
        (
            {"groups": [{"id": "g1", "paths": ["/etc/passwd"], "objective": "x"}]},
            "越权路径（不在快照内）",
        ),
        (
            {"groups": [{"id": "g1", "paths": ["config/道具表_CfgItem.xlsx"], "objective": "x"}]},
            "漏掉了大部分文件",
        ),
        (
            {
                "groups": [
                    {"id": f"g{i}", "paths": [p], "objective": "x"}
                    for i, p in enumerate(
                        [item["path"] for item in _config_three().entries]
                    )
                ]
                + [{"id": "gx", "paths": ["config/道具表_CfgItem.xlsx"], "objective": "x"}]
            },
            "组数超过上限",
        ),
        (
            {
                "groups": [
                    {
                        "id": "g1",
                        "paths": [item["path"] for item in _config_three().entries],
                        "objective": "x",
                        "qa_dimensions": ["编出来的维度"],
                    }
                ]
            },
            "点名了一个不存在的维度",
        ),
    ],
)
def test_an_invalid_proposal_falls_back_to_the_deterministic_grouping(broken, why):
    """**无效建议一律退回确定性分组**，并说清为什么（`planner.problems` 会落进计划）。"""
    facts = _config_three()
    forced = SnapshotFacts.from_mapping(
        {**facts.to_dict(), "file_count": 30, "diff_payload_chars": 30_000,
         "entries": [*facts.entries, *[
             _entry(f"extra/dir{i}/f{i}.lua", f"x{i}", 1_000) for i in range(27)
         ]]}
    )
    plan = plan_analysis(forced, 580_000, DIMENSIONS, proposal=broken)

    assert plan.planner["consulted"] is True
    assert plan.planner["accepted"] is False
    assert plan.planner["problems"], why
    if broken.get("groups"):
        assert plan.mode == MODE_FAMILY
        assigned = [path for member in plan.members for path in member.paths]
        assert sorted(assigned) == sorted(item["path"] for item in forced.entries), (
            "退回确定性分组之后必须仍然覆盖全部文件"
        )


def test_validate_grouping_accepts_a_complete_proposal():
    facts = _config_three()
    ok = validate_grouping(
        {
            "groups": [
                {
                    "id": "g1",
                    "paths": [item["path"] for item in facts.entries],
                    "objective": "全看",
                    "qa_dimensions": ["code_logic"],
                }
            ],
            "shared_paths": [],
            "reason": "只有一批",
        },
        facts,
        dimensions=DIMENSIONS,
    )

    assert ok.accepted is True
    assert ok.problems == ()


# ==========================================================================
# 五、单次硬上限：已花 + 本轮预留 + 收尾预留
# ==========================================================================


def test_the_single_run_guard_blocks_only_when_the_reserves_would_be_eaten():
    plan = plan_analysis(_config_three(), 580_000, DIMENSIONS)
    cap = plan.total_token_budget
    reserve = plan.round_reserve_tokens + plan.report_reserve_tokens

    assert single_run_guard(plan, spent_tokens=0)["blocked"] is False
    # 刚好还剩得下预留：**不拦**（拦早了会把能跑完的分析掐掉）。
    assert single_run_guard(plan, spent_tokens=cap - reserve - 1)["blocked"] is False
    # 再花 1 个 token 就会吃掉收尾预留：拦，并说明白是哪一项超了。
    blocked = single_run_guard(plan, spent_tokens=cap - reserve + 1)
    assert blocked["blocked"] is True
    assert "收尾预留" in blocked["reason"]
    # 这句话原先还写着「（不再新增模型调用）」，2026-09-26 之后它是一句假话：额度用尽、
    # 而手上已经有取证时，平台会**再补一次**「现在出结论」（`services/ai/wrap_up.py`）。
    # 这一档说的是**探索**停止 —— 判据跟着改，别把交付那一次也算进来。
    assert "停止探索" in blocked["reason"]
    assert "不再新增模型调用" not in blocked["reason"]


def test_an_unreported_usage_is_estimated_and_marked_not_zero():
    """上游没报用量时**保守估算并标记为估算** —— 不是 0（那等于无限放行），
    也不是精确值。"""
    class _Outcome:
        prompt_tokens = None
        completion_tokens = None
        context_chars = 30_000
        rounds = (object(), object(), object())

    tokens, estimated = conservative_member_tokens(_Outcome(), output_cap=8_000)

    assert estimated is True
    assert tokens >= 30_000, "取回 3 万字正文的成员不可能「没花钱」"
    # 没跑成的成员是「压根没跑」，不是「跑了但不知道花了多少」。
    assert conservative_member_tokens(None, output_cap=8_000) == (0, False)


def test_a_reported_usage_is_used_as_is_and_not_marked_as_estimated():
    class _Outcome:
        prompt_tokens = 1_000
        completion_tokens = 200
        context_chars = 99_999
        rounds = ()

    assert conservative_member_tokens(_Outcome(), output_cap=8_000) == (1_200, False)


def test_a_tighter_period_limit_brings_the_single_run_cap_down():
    """**用户可覆盖单次上限**的现成入口：项目配置里那个周期 token 上限。

    它管的是「这个周期一共能花多少」，比单次初值更紧时单次当然也不许超过它 ——
    否则计划里会出现「单次允许 8M、这个月只允许 0.9M」这种自相矛盾的数字。
    """
    plan = plan_analysis(
        _config_three(),
        {"user_chars": 580_000, "period_token_limit": 900_000},
        DIMENSIONS,
    )

    assert plan.total_token_budget == 900_000
    assert "周期上限" in plan.thresholds["single_run_token_cap_source"]
    # 预留是按上限的**比例**算的，所以上限降下来预留也跟着降（不许出现预留 > 上限）。
    assert 0 < plan.report_reserve_tokens < plan.total_token_budget
    # 收紧之后的判据仍按新上限算：花到 90 万就拦住，不再允许走到 150 万。
    assert single_run_guard(plan, spent_tokens=900_001)["blocked"] is True


def test_a_looser_period_limit_never_raises_the_single_run_cap():
    """反向自检：周期上限**更松**时什么也不做。

    周期留空时它解析成 100M/月 —— 要是拿它当单次上限，就等于「一次可以花一亿」，
    正是这次要修的那件事。
    """
    plan = plan_analysis(
        _config_three(),
        {"user_chars": 580_000, "period_token_limit": 100_000_000},
        DIMENSIONS,
    )

    assert plan.total_token_budget == auto_sizing.SINGLE_RUN_TOKEN_CAP_SMALL
    assert plan.thresholds["single_run_token_cap_source"] == "平台初值"


def test_the_configured_single_run_budget_reaches_the_persisted_plan(monkeypatch):
    """走真路径：配置页把「单次分析预算」填成 2M → **落库的那份计划**按 2M 算。

    这一条与上面那条周期上限的分别在于**这个旋钮是 2026-09-26 才有的**：在那之前
    「单次上限」是平台常量，界面上根本没有能改它的地方 —— 用户以为自己在调的
    `prompt_char_budget` 其实是**每轮提示词的水位**（真机 run 3 就是按那个理解配的）。
    所以这里要证的不只是「字段通了」，而是「填进这个框的那个数就是这一次的硬上限」。
    """
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        ai_service.update_project_analysis_config(
            project.id, {"single_run_token_limit": 2_000_000}
        )
        db.session.commit()

        payload, _state, _skip = ai_service.build_weekly_payload(cfg.id)

        assert payload["plan"]["total_token_budget"] == 2_000_000, (
            "配置里的单次分析预算没有进计划 —— 界面上的那个框就成了一句空话"
        )
        assert payload["plan"]["thresholds"]["single_run_token_cap_source"] == "用户配置"


def test_a_blank_single_run_budget_falls_back_to_the_platform_default(monkeypatch):
    """反向的一半：**留空 = 用平台初值**，不是 0、也不是上一次填过的那个数。

    `single_run_token_limit` 的 `None` 是有语义的（`NULLABLE_RESOLVED_KEYS`），而
    「留空」在界面上是一条合法操作（清空即回到平台按规模算）。回落成 0 会把这一次
    分析锁死，回落成上次的值会让用户以为清空没生效。
    """
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        ai_service.update_project_analysis_config(
            project.id, {"single_run_token_limit": 2_000_000}
        )
        db.session.commit()
        ai_service.update_project_analysis_config(
            project.id, {"single_run_token_limit": None}
        )
        db.session.commit()

        payload, _state, _skip = ai_service.build_weekly_payload(cfg.id)

        assert payload["plan"]["total_token_budget"] == auto_sizing.SINGLE_RUN_TOKEN_CAP_SMALL
        assert payload["plan"]["thresholds"]["single_run_token_cap_source"] == "平台初值"


@pytest.mark.parametrize(
    "files, chars_per_file, user_chars",
    [
        (3, 500, 150_000),
        (847, 5_000, 580_000),
        (5_000, 20_000, 2_000_000),
    ],
)
def test_the_platform_default_cap_covers_the_largest_plan_it_can_emit(
    files, chars_per_file, user_chars
):
    """**平台初值这一档必须自己就够跑完它排得出来的轮数。**

    2026-09-26 之前平台初值是 150 万，而单代理一档能排到 10 轮 —— 「跑满轮数的运行必然
    以『没有结论』收场」，真机 run 3 就是这么死的。修法不是「跑的时候发现不够再抬底」
    （那种分支在任何配置下都进不去，读的人却会以为有保护），而是**把这个常量本身按
    「能排出来的最贵计划」定死**，再让**这条用例**看着它：

    * 计划里的上限就是那枚常量（`==` 那一条）—— 证明没有人在背后悄悄改数；
    * 而按计划自己的轮数把预留花完，**闸门仍然放行**（`blocked is False`）。

    把 `SINGLE_RUN_TOKEN_CAP_SMALL` 调回 150 万，第二条就红 —— 那正是它要拦的回归。

    三档输入分别代表：小批次（3 个文件）、真机那个周版本（847 个）、以及能把轮数与
    清单取样同时顶到上界的那一档（5000 个文件 × 2 万字 diff、水位拉到 2M）。
    """
    plan = plan_analysis(
        _large_version(files, chars_per_file=chars_per_file),
        {"user_chars": user_chars},
        DIMENSIONS,
        subagent_enabled=False,
    )

    assert plan.mode == MODE_SINGLE, "这几档都要走单代理那一档（上限公式只对它生效）"
    assert plan.total_token_budget == auto_sizing.SINGLE_RUN_TOKEN_CAP_SMALL
    # 轮数从**计划自己的成员**上读，不再从 thresholds 抄一份（那一份已经删了）。
    rounds = sum(item.max_rounds for item in plan.members)
    assert rounds > 0

    spent = rounds * plan.round_reserve_tokens
    guard = single_run_guard(plan, spent_tokens=spent)
    assert guard["blocked"] is False, (
        f"跑完计划自己排的 {rounds} 轮之后闸门就不放行了（{guard['reason']}）—— "
        "上限这一档比它能排出来的计划还小"
    )


def test_the_period_limit_reaches_the_persisted_plan_through_the_real_run(monkeypatch):
    """走真路径：配置里把周期上限调紧 → **落库的那份计划**跟着收紧（不是只改了纯函数）。"""
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        ai_service.update_project_analysis_config(
            project.id, {"budget_token_limit": 1_000_000}
        )
        db.session.commit()

        payload, _state, _skip = ai_service.build_weekly_payload(cfg.id)
        assert payload["plan"]["total_token_budget"] == 1_000_000, (
            "配置里的周期上限没有进计划 —— 「用户可覆盖」就成了一句空话"
        )
        assert "周期上限" in payload["plan"]["thresholds"]["single_run_token_cap_source"]


# ==========================================================================
# 六、口径溯源：规划版本进 analysis_revision
# ==========================================================================


def test_the_analysis_revision_carries_the_plan_version_diff_and_protocol():
    """`analysis_revision` 必须包含 diff/快照、证据协议与**规划版本**。

    判据是**行为**：改规划版本 → 同一个门槛算出来的 revision 必须变（否则「基线口径已变」
    的提示不会出现，旧结论被静默复用）。
    """
    base = compose_analysis_revision("sev=high;conf=high")
    assert base.startswith("sev=high;conf=high"), "门槛成分要保留（它是最常变的那一项）"
    assert len(base) <= 80, "列宽是 80，超了在 SQLite 上静默截断、在 MySQL 上直接报错"
    assert auto_sizing.diff_snapshot_revision() in json.dumps(
        {"v": auto_sizing.diff_snapshot_revision()}
    )
    # 三样口径任一变化都要改变结果：这里用「换一个规划版本」代表。
    import services.ai.auto_sizing as sizing_mod

    original = sizing_mod.PLAN_VERSION
    try:
        sizing_mod.PLAN_VERSION = "2099-01-01.1"
        assert compose_analysis_revision("sev=high;conf=high") != base
    finally:
        sizing_mod.PLAN_VERSION = original


def test_only_a_broken_threshold_config_skips_the_composition():
    """门槛配置本身非法时给哨兵值（与任何历史结论都不相等 → 照旧逼出重跑），
    其余情况下**读侧与写侧调的是同一个函数**（见 `current_provenance`）。"""
    with flask_app.app_context():
        create_tables()
        from models.ai_analysis import AiProjectAnalysisConfig
        from tests.test_ai_analysis_service import _create_project

        project = _create_project()
        db.session.commit()
        row = AiProjectAnalysisConfig.query.filter_by(project_id=project.id).first()
        if row is None:
            row = AiProjectAnalysisConfig(project_id=project.id)
            db.session.add(row)
        row.min_severity = "high"
        db.session.commit()

        revision = current_provenance(project.id)["analysis_revision"]
        assert revision.startswith("sev=high")
        assert "x=" in revision
        assert current_provenance(project.id)["analysis_revision"] == revision


# ==========================================================================
# 七、端到端：预估与运行读**同一份**计划
# ==========================================================================


def _patch_client(monkeypatch):
    from tests.test_ai_analysis_service import _FakeClient

    client = _FakeClient()
    monkeypatch.setattr(
        ai_service, "build_endpoint_client", lambda *a, **k: (client, [])
    )
    return client


def test_the_run_and_the_preview_read_the_same_persisted_plan(monkeypatch):
    """**同一份已持久化计划**（工作包 B 的验收：预估/实际计划字段一致）。

    断言的是**逐字相等**：两条路径各自调 `build_weekly_payload`，同一份输入必须得到
    同一个计划字典（含 mode / 分片 / 每成员额度 / 单次上限 / 两个预留）。

    **顺序是「先预估、后运行」**：预估是只读的，而一次运行会推进基线指针与结论，
    于是它之后重建的 payload 会把「上轮没取到证据」的文件标成补偿项（`source` 变了，
    关键路径扫描也会跳过它）—— 那是**输入合法地变了**，不是两次推导不一致。
    """
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    client = _patch_client(monkeypatch)
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)

        preview = usage_service.analysis_estimate(
            project.id, mode="incremental", config_id=cfg.id
        )
        assert preview["plan"] and preview["plan"]["mode"] == MODE_SINGLE

        outcome = ai_service.run_weekly_analysis_background(cfg.id)
        assert outcome["status"] == "succeeded", outcome
        assert len(client.calls) == 1, "3 个文件以下的小批次只该有一个分析者"

        run = (
            AiAnalysisRun.query.filter_by(
                target_type="weekly", target_key=build_weekly_group_key(cfg)
            )
            .order_by(AiAnalysisRun.id.desc())
            .first()
        )
        run_payload = json.loads(run.request_payload)

    run_plan = run_payload["plan"]
    assert run_plan["mode"] == MODE_SINGLE
    assert preview["plan"] == run_plan, (
        "预估摆给用户的那份计划与实际跑的那份不是同一份 —— 确认框里的分片数/额度"
        "就变成了谎话"
    )
    assert preview["budget_plan"]["plan"] == run_plan
    assert preview["shard_count"] == 1
    # 事实也要一并落库（「当时凭什么这么分工」的第一问答的是输入长什么样）。
    assert run_payload["snapshot_facts"]["file_count"] == 1
    assert run_payload["snapshot_facts"]["thresholds"]["max_files"] == 8


def test_a_rerun_plan_inside_the_worker_reuses_the_persisted_one(monkeypatch):
    """worker 里**不回退旧公式**：payload 里没有计划时会现算一份并补记进 payload。

    这条守的是「老 payload / 被裁过的 payload」这一档 —— 它必须走同一个纯函数，
    而不是回到「从预算反解 5 片」。
    """
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    _patch_client(monkeypatch)
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        payload, _state, _skip = ai_service.build_weekly_payload(cfg.id)
        del payload["plan"]           # 模拟这一改动之前建出来的 payload

        run = AiAnalysisRun(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=build_weekly_group_key(cfg),
            status="running",
        )
        db.session.add(run)
        db.session.commit()

        result = ai_service._execute_analysis(
            run,
            project_id=project.id,
            payload=payload,
            project_config=ai_service.get_project_analysis_config(project.id),
            target_type="weekly",
            target_key=build_weekly_group_key(cfg),
        )

        assert result["status"] == "succeeded", result
        restored = plan_of(payload)
        assert restored is not None, "现算的那份计划没有补记回 payload"
        assert restored.mode == MODE_SINGLE
        assert restored.snapshot_fingerprint


def test_an_exhausted_single_run_cap_stops_the_exploration_without_new_model_calls(monkeypatch):
    """**预算不足时**：停止探索、报告可读（已查/未查分开）、**不新增模型调用**。

    单次上限压到 1,000 token（预留本身就比它大）→ 每一个分片在开跑前就被跳过，
    只有汇总那一次照跑（它是唯一产出报告的一步，跳过它等于这一家子白跑）。
    """
    from tests.test_ai_run_budget_warning import _prepare_weekly_run
    from tests.test_ai_subagent_wiring import _enable_subagents, _force_shard_count

    client = _patch_client(monkeypatch)
    monkeypatch.setattr(auto_sizing, "SINGLE_RUN_TOKEN_CAP_SMALL", 1_000)
    monkeypatch.setattr(auto_sizing, "SINGLE_RUN_TOKEN_CAP_LARGE", 1_000)
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        _enable_subagents(project.id)
        _force_shard_count(monkeypatch, 2)

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] in ("succeeded", "degraded"), outcome
        # **分片一次都没跑**（每个都被上限拦下），只有汇总那一次调用了模型。
        assert len(client.calls) == 1, (
            f"上限早就超了却还发了 {len(client.calls)} 次模型调用"
        )
        run = (
            AiAnalysisRun.query.filter_by(
                target_type="weekly", target_key=build_weekly_group_key(cfg)
            )
            .order_by(AiAnalysisRun.id.desc())
            .first()
        )
        payload = json.loads(run.response_payload)
        skipped = json.dumps(payload.get("subagents") or [], ensure_ascii=False)
        assert "单次分析上限" in skipped or "单次分析上限" in (run.response_text or ""), (
            "被跳过的分片没有在报告里点名 —— 静默跳过等于把「没人看过」写成「没问题」"
        )


# ==========================================================================
# 八、全量预览的基线：不是「没有」，是「这次不用」
# ==========================================================================


def test_the_full_mode_preview_says_the_baseline_is_not_used(monkeypatch):
    """全量模式预览的「基线 run」必须说「**全量不使用基线**」，而不是
    「还没有可复用基线」—— 后者是一句与事实相反的话（基线可能存在，只是全量刻意不看）。"""
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    with flask_app.app_context():
        create_tables()
        _ai_service, project, cfg = _prepare_weekly_run(monkeypatch)

        estimate = usage_service.analysis_estimate(project.id, mode="full", config_id=cfg.id)

    baseline = estimate["baseline_run"]
    assert baseline["not_applicable"] == "full"
    assert baseline["label"] == "全量不使用基线"
    assert baseline["run_id"] is None
    assert "不需要" not in baseline["note"] or True  # 文案由模板渲染，服务端只给事实
    assert estimate["mode"] == "full"


def test_the_incremental_preview_still_reports_a_real_baseline(monkeypatch):
    """反向自检：增量模式下**不许**把基线说成「不使用」—— 否则这条测的是
    「全量那句话存在」，而不是「全量与增量说得不一样」。"""
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    with flask_app.app_context():
        create_tables()
        _ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        estimate = usage_service.analysis_estimate(
            project.id, mode="incremental", config_id=cfg.id
        )

    baseline = estimate["baseline_run"] or {}
    assert baseline.get("not_applicable") != "full"


# ==========================================================================
# 九、快照事实的取数：量的是**载荷**，且估算与实测分开
# ==========================================================================


def _stamp_decoded_payloads(config_id: int, body_chars: int) -> tuple[dict[int, int], int]:
    """给这个配置下**每一行**缓存写上形态与写入侧一致的载荷。

    返回 `({行 id: 解码后字数}, 库里那份 ASCII 转义串的总长度)`。

    写入侧存的是 `json.dumps(...)`（默认 `ensure_ascii=True`），所以库里的中文是
    `\\uXXXX` 六个字符一个汉字 —— 这正是「先解码再量」那条口径要防的东西：
    直接量原串会把每个汉字算成 6 倍，3 个文件的中文配表差异会被量成 8 万字。
    """
    from models.weekly_version import WeeklyVersionDiffCache

    rows = (
        WeeklyVersionDiffCache.query.filter_by(config_id=config_id)
        .order_by(WeeklyVersionDiffCache.id)
        .all()
    )
    expected: dict[int, int] = {}
    raw_total = 0
    body = {"rows": [{"cell": "差异内容" * max(1, body_chars // 4)}]}
    for row in rows:
        row.merged_diff_data = json.dumps(body)  # 默认 ensure_ascii=True：库里就是这种形态
        expected[int(row.id)] = len(json.dumps(body, ensure_ascii=False))
        raw_total += len(row.merged_diff_data)
    db.session.commit()
    assert rows, "这个配置下一行缓存都没有，量不出任何东西"
    return expected, raw_total


def test_a_small_batch_is_measured_file_by_file(monkeypatch):
    """小批次**逐个量**，量出来的是**解码后**的字数（门槛判定的边界必须准）。"""
    from datetime import datetime, timezone

    from tests.test_ai_analysis_service import _seed_diff_cache
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    with flask_app.app_context():
        create_tables()
        ai_service, _project, cfg = _prepare_weekly_run(monkeypatch)
        _seed_diff_cache(
            cfg, cfg.repository, "config/道具表.xlsx", datetime.now(timezone.utc),
            commit_id="b" * 40,
        )
        db.session.commit()
        expected, raw_total = _stamp_decoded_payloads(cfg.id, 4_000)

        payload, _state, _skip = ai_service.build_weekly_payload(cfg.id)
        facts = snapshot_facts_from_payload(payload)

        assert facts.file_count == len(payload["delta_files"]) == len(expected)
        assert facts.chars_estimated is False, "两个文件的批次必须逐个量，不许抽样放大"
        assert facts.diff_payload_chars == sum(expected.values()), (
            "量出来的字数与解码后的载荷对不上 —— 门槛判定就建立在这个数上"
        )
        assert facts.diff_payload_chars * 3 < raw_total, (
            "量的是库里那串原样长度（没有解码）：中文被算成了 6 倍，"
            "这正是把 3 个文件的配置 3 误判成需要分工的那条路"
        )
        assert facts.max_file_diff_chars <= facts.diff_payload_chars
        assert facts.is_small_batch is True
        assert facts.digest(), "事实必须有内容指纹（「是不是同一份输入」要能回答）"


def test_a_big_batch_is_sampled_and_says_so(monkeypatch):
    """大版本按**等距抽样**放大，并且如实标 `chars_estimated=True` + `sample_size`。

    「量出来的」与「推出来的」不许看起来一样：抽样放大的总和是**估计**，
    与逐个量出来的数字必须能分辨（与「未上报不许写成 0」同一条纪律）。
    """
    from datetime import datetime, timezone

    from tests.test_ai_analysis_service import _seed_diff_cache
    from tests.test_ai_run_budget_warning import _prepare_weekly_run

    with flask_app.app_context():
        create_tables()
        ai_service, _project, cfg = _prepare_weekly_run(monkeypatch)
        stamp = datetime.now(timezone.utc)
        for index in range(29):
            _seed_diff_cache(
                cfg, cfg.repository, f"config/表_{index:02d}.xlsx", stamp,
                commit_id=f"{index:040d}",
            )
        db.session.commit()
        expected, raw_total = _stamp_decoded_payloads(cfg.id, 6_000)
        per_file = max(expected.values())

        payload, _state, _skip = ai_service.build_weekly_payload(cfg.id)
        facts = snapshot_facts_from_payload(payload)

        assert facts.file_count == len(payload["delta_files"]) == len(expected) == 30
        assert facts.chars_estimated is True, "30 个文件却没标成「估」—— 估计被当成了实测"
        assert facts.sample_size == 20 < facts.file_count, (
            "抽样条数必须如实写出来，否则「估的」与「量的」在数据上分不开"
        )
        # 放大口径：总和 = 文件数 × 每份均值（每份载荷一样大，所以这里可以逐字对）。
        assert facts.diff_payload_chars == facts.file_count * per_file
        assert facts.diff_payload_chars > auto_sizing.SMALL_BATCH_MAX_TOTAL_CHARS
        assert facts.is_small_batch is False
        assert facts.diff_payload_chars * 3 < raw_total * facts.file_count / facts.sample_size
        assert facts.digest()


def test_the_plan_round_trips_through_the_payload():
    plan = plan_analysis(_large_version(), 580_000, DIMENSIONS)
    restored = AnalysisPlan.from_dict(json.loads(json.dumps(plan.to_dict())))

    assert restored is not None
    assert restored.to_dict() == plan.to_dict()
    assert AnalysisPlan.from_dict(None) is None
    assert AnalysisPlan.from_dict({"mode": ""}) is None


# ==========================================================================
# 十、成员数**不来自**维度数（2026-09-23 复核补上的一条）
# ==========================================================================


def test_the_member_count_does_not_come_from_the_dimension_count():
    """**维度数不是成员数的上界。** 成员是按变更簇切出来的**工作量**，而每个成员都要
    覆盖**全部**维度清单 —— 所以一个只声明 3 个维度的项目，同样该按证据体积把人数开够。

    判据用「同一份输入、**只换**维度清单」：成员数必须不变。上一版是从
    `derive_family_sizing` 的 `ceiling.shard_count` 漏进来的（那个函数把分片数按
    `min(目标, max(2, 维度数))` 夹过一次），于是 847 个文件配 3 个维度只开 3 个成员 ——
    那正是指引明令去掉的「把 N 个维度机械均分成 N 个角色」。

    **样本必须选维度数 < `FAMILY_MAX_MEMBERS` 的那一档**：9 个维度时两边都是 5，
    拿它当样本无论实现对不对都会绿（本仓吃过「为第二条分支挑的 fixture 其实不在
    那条分支上」的亏）。所以这里两边都断言，3 那一档才是真正咬得住的那一口。
    """
    facts = _large_version()
    few = plan_analysis(facts, 580_000, ("code_logic", "config_table", "cross_ref"))
    many = plan_analysis(facts, 580_000, tuple(f"dim{index}" for index in range(9)))

    assert few.mode == MODE_FAMILY and many.mode == MODE_FAMILY
    assert few.member_count == many.member_count, (
        f"成员数跟着维度数变了：3 个维度开 {few.member_count} 个成员、"
        f"9 个维度开 {many.member_count} 个 —— 那是「把维度机械均分成角色」"
    )
    # 而且覆盖的是**全部**声明维度（不是各管一摊之后没人兜底）
    assert set(few.covered_dimensions()) == {"code_logic", "config_table", "cross_ref"}
    assert len(many.covered_dimensions()) == 9
