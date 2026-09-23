# -*- coding: utf-8 -*-
"""计划、预算展示与清单**说的是同一件事**（P2-1 / P2-3）。

## 这一组盯的是哪一种不一致

实测 run 53 的两个数对不上，而且都不会报错：

* **展示**写着「每片 8 轮 / 40 次」，**引擎实际拿到的是 10 轮 / 50 次** ——
  `single_member_limits` 把额度按计划抬上去了，紧挨着的 `build_budget_plan` 却还在读
  原始 `limits`；
* **清单**写着 `by_shard = {S1:1, S2:1, S3:1}`，而这一轮是**单分析者** ——
  `build_manifest` 在 `attach_weekly_plan` **之前**跑，分片数读的是配置里的
  `subagent_count`，不是这次真的要拆几片。

## 为什么用例要挑「大版本却只有一个簇」那一档

小批次（3 个文件）那一档的计划额度**就是出厂默认 8/40**，展示与实际的差异在那里等于 0 ——
拿它做 fixture，改不改代码都绿，属于「为第二条分支挑的 fixture 其实不在那条分支上」。
所以两条单分析者用例都用 `12 个文件 / 每个 ~8,000 字 / 同一个提交同一个目录`：
它不是小批次（`file_count > 8` 且总字数 > 80,000），确定性分簇只给出一个簇，
于是计划必须把一个分析者的额度抬到 10 轮 / 50 次 —— 差异才真的存在。
`test_the_fixture_really_lands_on_the_raised_tier` 单独把这件事钉住：
阈值哪天变了，是它先红，而不是这几条用例静默变成永远为真。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app import app, create_tables, db
from models.ai_analysis import AiAnalysisRun
from models.weekly_version import WeeklyVersionDiffCache
from services import ai_analysis_service as ai_service
from services.ai.analysis_plan import plan_of
from services.ai.auto_sizing import (
    MODE_FAMILY,
    MODE_SINGLE,
    SnapshotFacts,
    plan_analysis,
)
from services.ai.engine import STATUS_SUCCEEDED, EngineLimits, EngineOutcome
from tests.test_ai_analysis_plan import DIMENSIONS
from tests.test_ai_analysis_service import (
    _create_project,
    _create_repo,
    _create_weekly_config,
    _FakeClient,
    _uid,
)

# 这一档的判定：不是小批次（12 > SMALL_BATCH_MAX_FILES=8，总字数 > SMALL_BATCH_MAX_TOTAL_CHARS=80,000），
# 但确定性分簇只给一个簇 → 单分析者，额度按 ceiling 抬到 10/50。
SINGLE_FILES = 12
FAMILY_DIRS = 2
PER_FILE_CHARS = 8_000


def _blob(chars: int) -> str:
    """一条 `merged_diff_data`，解码后**正好** `chars` 个字符。

    `analysis_plan._decoded_chars` 量的是 `len(json.dumps(payload, ensure_ascii=False))`，
    所以外壳（`{"diff": "…"}` 那 12 个字符）要减掉 —— 不减就会把每个文件的体量多算 12 字，
    量出来的总字数与这一档的门槛就不再是同一件事。
    """
    return json.dumps({"diff": "x" * max(0, chars - 12)})


def _seed(cfg, repo, paths, *, commit: str, chars: int = PER_FILE_CHARS) -> None:
    for path in paths:
        db.session.add(
            WeeklyVersionDiffCache(
                config_id=cfg.id,
                repository_id=repo.id,
                file_path=path,
                file_type="code",
                latest_commit_id=commit,
                commit_count=1,
                updated_at=datetime.now(timezone.utc),
                merged_diff_data=_blob(chars),
            )
        )


def _single_input():
    """一个目录、一个提交、12 个文件 —— 分不出簇，但要读的东西远超小批次门槛。"""
    project = _create_project()
    repo = _create_repo(project.id, _uid("code"), "svn", "code")
    cfg = _create_weekly_config(
        project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
    )
    _seed(cfg, repo, [f"src/file_{i}.lua" for i in range(SINGLE_FILES)], commit="a" * 40)
    return project, repo, cfg


def _family_input():
    """两个目录、两个提交 —— 两个变更簇，够拆成两个成员。"""
    project = _create_project()
    repo = _create_repo(project.id, _uid("code"), "svn", "code")
    cfg = _create_weekly_config(
        project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
    )
    for index in range(FAMILY_DIRS):
        _seed(
            cfg,
            repo,
            [f"dir{index}/file_{i}.lua" for i in range(SINGLE_FILES)],
            commit=f"{index + 1}" * 40,
        )
    return project, repo, cfg


def _plan_of_payload(payload: dict):
    plan = plan_of(payload)
    assert plan is not None, "这份 payload 里没有计划"
    return plan


# ==========================================================================
# 一、fixture 自己要先证明「那一档是活的」
# ==========================================================================


def test_the_fixture_really_lands_on_the_raised_tier():
    """12 个文件 / 一个簇 → **单分析者、额度被抬到 10 轮 50 次**，且与出厂默认不同。

    这条是整个文件的地基：它红了说明下面几条用来分辨「展示跟着计划走没有」的那个差异
    已经不存在了（阈值被调过、分簇规则被改过），那时下面几条会在**什么都没测到**的情况下
    保持绿。
    """
    with app.app_context():
        create_tables()
        _project, _repo, cfg = _single_input()
        db.session.commit()

        payload, _state, skip_reason = ai_service.build_weekly_payload(cfg.id)
        assert skip_reason is None, skip_reason

    plan = _plan_of_payload(payload)
    member = plan.members[0]
    defaults = EngineLimits()

    assert plan.mode == MODE_SINGLE, plan.mode
    # 轮次这一档从 8 抬到 10（`_single_plan` 的 ceiling 支），这是「抬过」的直接证据；
    # 索取次数同样抬高，但具体值随证据体积推导，所以只断言它**不等于**出厂默认。
    assert member.max_rounds > defaults.max_rounds, (member.max_rounds, defaults.max_rounds)
    assert member.max_tool_requests != defaults.max_tool_requests, (
        member.max_tool_requests,
        defaults.max_tool_requests,
    )


# ==========================================================================
# 二、P2-1：预算展示必须等于**引擎实际拿到的那一份**
# ==========================================================================


def test_the_persisted_budget_plan_matches_the_limits_the_engine_got(monkeypatch):
    """落库的 `budget_plan` 与交给引擎的 `limits` **必须是同一份数**。

    判据分三层，缺一不可：

    1. 引擎**实际收到**的额度是计划抬上去的 10/50（不是出厂默认）；
    2. 落库的 `budget_plan` 报的也是 10/50；
    3. 两者相等 —— 这一条才是「展示与实际一致」本身。

    只断言 (1) 或只断言 (2) 都放过原来那个 bug：那时引擎拿 10/50、展示写 8/40。
    """
    handed: list = []

    def recording_engine(**kwargs):
        handed.append(kwargs["limits"])
        return EngineOutcome(status=STATUS_SUCCEEDED, report_markdown="# 变更理解\nx")

    with app.app_context():
        create_tables()
        project, _repo, cfg = _single_input()
        db.session.commit()

        ai_service.set_project_api_key(project.id, "k")
        ai_service.update_project_analysis_config(
            project.id,
            {
                "api_base_url": "http://127.0.0.1:15721/v1",
                "api_model": "deepseek-v4-flash",
                "subagent_enabled": False,
            },
        )
        db.session.commit()

        monkeypatch.setattr(ai_service, "build_endpoint_client", lambda *a, **k: (_FakeClient(), []))
        monkeypatch.setattr(ai_service, "run_analysis", recording_engine)

        outcome = ai_service.run_weekly_analysis_background(cfg.id)
        assert outcome["status"] == "succeeded", outcome

        run = db.session.get(AiAnalysisRun, outcome["run_id"])
        # `budget_plan` 落在**结论 payload** 上（`response_payload`），与 SSE 的 result
        # 事件同一份 —— 界面读的就是它。计划在 `request_payload` 上，两者是两份不同用途的账。
        budget_plan = json.loads(run.response_payload)["context"]["budget_plan"]

    assert handed, "引擎没有被调用"
    assert handed[0].max_rounds > EngineLimits().max_rounds, handed[0].max_rounds
    # 展示那一栏在 `per_role` 里（单分析者就是「一个角色」的那一份额度）。
    shown = budget_plan["per_role"]
    assert shown["max_rounds"] == handed[0].max_rounds, (
        "展示写着 %s 轮，引擎实际拿到 %s 轮" % (shown["max_rounds"], handed[0].max_rounds)
    )
    assert shown["max_tool_requests"] == handed[0].max_tool_requests, (
        "展示写着 %s 次，引擎实际拿到 %s 次"
        % (shown["max_tool_requests"], handed[0].max_tool_requests)
    )


# ==========================================================================
# 三、P2-3：清单的分片数按**计划**算，不按配置里的 subagent_count
# ==========================================================================


def test_a_single_analyzer_manifest_does_not_invent_shards():
    """单分析者这一档：清单必须写「1 片」，不能编出 S1/S2/S3。

    配置里的 `subagent_count` 与这一轮**没有关系**（子代理关着 / 分不出簇），
    拿它当分片数，模型读到的 `change-manifest` 就会说「3 个分片各分到几个文件」——
    而这一轮并没有分片，也没有那份任务书可以让它去找。
    """
    with app.app_context():
        create_tables()
        _project, _repo, cfg = _single_input()
        db.session.commit()

        payload, _state, skip_reason = ai_service.build_weekly_payload(cfg.id)
        assert skip_reason is None, skip_reason

    manifest = payload["manifest"]
    assert payload["plan"]["mode"] == MODE_SINGLE
    assert manifest["shard_count"] == 1, manifest["shard_count"]
    assert manifest["by_shard"] == {"S1": SINGLE_FILES}, manifest["by_shard"]
    assert manifest["assigned"] == SINGLE_FILES, "单分析者必须覆盖全部文件"


def test_a_family_manifest_counts_the_plans_members_not_the_config():
    """反向的一半：真拆片时，片数取**计划里的成员数**，不取配置里的 `subagent_count`。

    没有这一条，一个「永远写 1」的实现也能让上面那条全绿 —— 而那会把真分片的账说成
    「没有分片」，方向同样坏（模型以为自己该看全部文件，于是重复劳动）。
    """
    with app.app_context():
        create_tables()
        project, _repo, cfg = _family_input()
        db.session.commit()

        # **只传 `subagent_enabled`**：`subagent_count` 已经不再接受手动配置，带上它
        # 会让整次保存失败（那一层的口径是「校验失败就一个字段都不落库」），
        # 于是子代理还关着、这一条用例会静默退化成单分析者那一档。
        ok, message, _errors = ai_service.update_project_analysis_config(
            project.id, {"subagent_enabled": True}
        )
        assert ok, message
        db.session.commit()

        configured_shards = ai_service.get_project_analysis_config(project.id)["subagent_count"]
        payload, _state, skip_reason = ai_service.build_weekly_payload(cfg.id)
        assert skip_reason is None, skip_reason

    plan = payload["plan"]
    manifest = payload["manifest"]
    assert plan["mode"] == MODE_FAMILY, (plan["mode"], plan["facts"])
    # 落库的 plan dict 里没有 `member_count` 这个键（那是 `AnalysisPlan` 的属性），
    # 成员数就是 `members` 的长度。
    member_count = len(plan["members"])
    assert member_count >= 2, member_count
    assert manifest["shard_count"] == member_count, (
        "清单写着 %s 片，计划说 %s 片" % (manifest["shard_count"], member_count)
    )
    # 配置里那个数**不是**这一轮的片数（原来那个 bug 就是拿它当片数）。
    # 两者相同时这条用例分辨不出「按计划」与「按配置」—— 那时要换一份 fixture，不是删断言。
    assert manifest["shard_count"] != configured_shards, (
        "片数 %s 与配置里的 subagent_count 撞了，这条用例失去分辨力"
        % manifest["shard_count"]
    )
    assert set(manifest["by_shard"]) == {
        f"S{index}" for index in range(1, member_count + 1)
    }, manifest["by_shard"]
    assert manifest["assigned"] == 2 * SINGLE_FILES


# ==========================================================================
# 四、P2-2：这个数**自称是什么**，必须与它实际是什么一致
# ==========================================================================


def test_the_plan_says_payload_estimate_not_rendered_diff():
    """计划理由与门槛那句话里写的是**差异载荷估算**，不是「渲染后 diff」。

    这个数从来不等于「模型看到的 diff 有多长」—— 它是库里 JSON 载荷解码后的长度。
    原先的名字与文案让读的人（包括实测时对着计划理由核对报告的人）拿它去比一个
    比不出来的东西。**口径一个字没改，改的是它自称是什么。**
    """
    facts = SnapshotFacts.from_mapping(
        {"file_count": 3, "diff_payload_chars": 80_001, "max_file_diff_chars": 100}
    )
    plan = plan_analysis(facts, 580_000, DIMENSIONS)

    for text in (facts.small_batch_exceeded_by, plan.reason):
        assert "差异载荷估算" in text, text
        assert "渲染" not in text, text


def test_an_old_snapshot_still_reads_under_its_old_key():
    """旧键名（`rendered_diff_chars`）读侧仍然认 —— 库里的运行写的就是它。

    `snapshot_facts` 冻在 `request_payload` 里，已经在库的那些运行（含实测的
    run 45/46/52/53）不可能跟着改名。不认旧键，它们的事实会被读成 0，
    「当时凭什么这么分工」就成了一句空话。
    """
    facts = SnapshotFacts.from_mapping(
        {"file_count": 3, "rendered_diff_chars": 12_345, "max_file_diff_chars": 100}
    )

    assert facts.diff_payload_chars == 12_345


def test_the_small_batch_threshold_behaves_exactly_as_before():
    """改名**没有**动门槛：81,000 过线、79,000 不过线，两侧各钉一次。

    只测一侧的话，「阈值被顺手调大/调小」这种改动不会被发现 —— 而这一条正是这次
    改名承诺过「门槛判据不变」的那句话。
    """
    over = SnapshotFacts.from_mapping({"file_count": 3, "diff_payload_chars": 81_000})
    under = SnapshotFacts.from_mapping({"file_count": 3, "diff_payload_chars": 79_000})

    assert over.small_batch_exceeded_by, "过线那一侧没说超限"
    assert under.small_batch_exceeded_by == "", under.small_batch_exceeded_by
