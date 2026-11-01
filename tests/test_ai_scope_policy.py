# -*- coding: utf-8 -*-
"""范围判定（scope）只表示**输入口径**：建议整窗重跑 ≠ 升全量。

## 这一组守的是什么

`scope` 曾经把三件事混在一起：输入装的是整窗、平台建议整窗、以及「没有基线」。
「变化文件数多 / 比例高 / 命中关键路径」被直接翻译成 `scope="full"`，于是：

* 读侧把「分析范围」写成全量，而输入其实只有那十几条 delta（实测 run 63：真实变化
  11 条，判 full · `critical_path_detected`）；
* 预检与前端把 `full` 当成「必须整窗重跑」，用户点一次增量、最后真的整窗跑
  （实测 run 62：33 真实 + 20 补偿 + 1 依赖 = 54，补偿项把判据顶过了 50）；
* 默认关键路径清单里的 `config/` 让配表项目**每次动表**都命中（实测 4/6 次）。

2026-11-01 起：`scope == "full"` **只允许「没有基线」**（首跑 / 用户显式全量）；
平台建议整窗时 `scope` 仍是 `incremental`、基线不清空，建议落在
`policy.suggested_full` / `policy.decision` 上。下面的用例把这几条钉住，并回放
run 63 / run 62 的输入账。

第四节还钉了「分析范围」的另一半：`analysis_profile` 决定本次过哪一份**检查维度
清单**（`tables` → 5 个配表维度，`all` → 出厂 9 个），以及它只在不声明时生效。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from models.ai_analysis import AiAnalysisTrace
from models.ai_analysis.project_config import AiProjectAnalysisConfig
from services.ai import project_facts
from services.ai.scope_sampling import (
    FULL_ANALYSIS_FILE_THRESHOLD,
    ScopeDecision,
    _decide_scope,
    decide_scope,
)
from services.ai.skill_contract import (
    DEFAULT_DIMENSION_SPECS,
    TABLE_DIMENSION_IDS,
    TABLE_DIMENSION_SPECS,
)
from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV, load_skills
from services.ai.snapshot_store import BaselineSnapshot


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _baseline(item_count: int) -> BaselineSnapshot:
    """一份「上一轮分析过这么多文件」的基线（纯函数判定只需要条数）。"""
    return BaselineSnapshot(
        snapshot_id=1, content_digest="x" * 40, item_count=item_count, complete=False, items={}
    )


def _summary(
    *,
    real_delta: int,
    compensation: int = 0,
    dependency: int = 0,
    denominator: int = 200,
    window: int = 200,
    critical_hits=(),
) -> dict:
    """照 `_summarize_weekly_files` 的键写一份 summary（只留判定要读的那几项）。"""
    carried = compensation + dependency
    return {
        "total_files": window,
        "delta_files": real_delta + carried,
        "batch_files": real_delta + carried,
        "scoped_delta_files": real_delta,
        "carried_files": carried,
        "denominator": denominator,
        "delta_ratio": real_delta / max(denominator, 1),
        "compensation_files": compensation,
        "dependency_files": dependency,
        "window_files": window,
        "critical_paths": bool(critical_hits),
        "critical_path_hits": list(critical_hits),
        "critical_path_source": "default",
    }


# ===========================================================================
#  一、回放 run 63 / run 62：判定不再把「补偿/关键路径」翻译成 full
# ===========================================================================


def test_run_63_replay_is_incremental_and_only_suggests_a_full_rerun():
    """回放 run 63：真实变化 11 条 + 补偿 13 条 + 依赖 1 条（输入共 25 条）。

    旧裁决：命中默认清单里的 `config/` → `full · critical_path_detected`，于是
    「分析范围：全量」写进了报告、下一次做差也拿不到这条基线。新裁决：输入口径仍是
    增量，命中只把 `suggested_full` 置真 —— 要不要整窗重跑由用户决定。
    """
    summary = _summary(
        real_delta=11,
        compensation=13,
        dependency=1,
        window=25,
        critical_hits=[
            "config/10_role/角色等级表_CfgPlayerLevel.xlsx"
            "（命中关键路径模式 (?:^|/)config/）"
        ],
    )
    decision = decide_scope(summary, _baseline(200))

    assert decision.scope == "incremental"
    assert decision.reason == "critical_path_detected"
    assert decision.suggested_full is True
    assert (decision.real_delta, decision.compensation, decision.dependency) == (11, 13, 1)
    assert decision.ratio == pytest.approx(11 / 200)
    assert decision.critical_hits and decision.critical_source == "default"
    # 兼容 wrapper 的形状不变（job 结算与既有调用点按二元组取）。
    assert _decide_scope(summary, _baseline(200)) == ("incremental", "critical_path_detected")


def test_run_63_without_the_config_hit_is_a_plain_small_delta():
    """同一个输入、去掉那条 `config/` 命中（默认清单里已经没有它了）。

    这才是修复后的常态：11 条真实变化既不判 full、也不建议整窗 —— 修之前它 100%
    命中 `(?:^|/)config/`，因为配表项目的主体内容就在那个目录下。
    """
    decision = decide_scope(
        _summary(real_delta=11, compensation=13, dependency=1, window=25),
        _baseline(200),
    )

    assert (decision.scope, decision.reason) == ("incremental", "delta_small")
    assert decision.suggested_full is False


def test_run_62_replay_does_not_count_compensation_as_a_change():
    """回放 run 62：33 真实 + 20 补偿 + 1 依赖 = 54。

    旧判据拿 54 去比 `FULL_ANALYSIS_FILE_THRESHOLD`（50）→ `delta_count_high`。
    新判据只数真实的 33（`scoped_delta_files`），补偿与依赖进 `carried_files`：
    既不判 full，也**不会**因为补偿项而建议整窗。
    """
    decision = decide_scope(
        _summary(real_delta=33, compensation=20, dependency=1, window=54),
        _baseline(200),
    )

    assert decision.scope == "incremental"
    assert decision.real_delta == 33
    assert decision.reason != "delta_count_high", "补偿项又被算进了「变化了多少」"
    assert decision.reason == "delta_small"
    assert decision.suggested_full is False


def test_the_file_count_threshold_counts_only_the_real_delta():
    """阈值本身保留，只是分子换成真实变化：49 / 50 两侧各一条。

    run 62 的同形输入只要真实变化到 50，仍然会建议整窗 —— 修的是分子口径，
    不是把阈值关掉。
    """
    under = decide_scope(
        _summary(real_delta=49, compensation=20, dependency=1, denominator=1000),
        _baseline(1000),
    )
    assert (under.reason, under.suggested_full) == ("delta_small", False)

    at = decide_scope(
        _summary(real_delta=FULL_ANALYSIS_FILE_THRESHOLD, denominator=1000),
        _baseline(1000),
    )
    assert (at.scope, at.reason, at.suggested_full) == (
        "incremental",
        "delta_count_high",
        True,
    )


def test_a_high_ratio_only_suggests_and_uses_the_baseline_denominator():
    """比例判据同样只建议整窗；分母是**基线条数**（62），不是整窗缓存行数。"""
    decision = decide_scope(_summary(real_delta=20, denominator=62), _baseline(62))

    assert (decision.scope, decision.reason) == ("incremental", "delta_ratio_high")
    assert decision.suggested_full is True
    assert decision.ratio == pytest.approx(20 / 62)


# ===========================================================================
#  二、`scope == "full"` 只允许「没有基线」
# ===========================================================================


def test_no_baseline_is_the_only_automatic_full():
    """**没有基线 → full / first_run**（既有语义，任何改动都不许丢）。"""
    decision = decide_scope(_summary(real_delta=3, denominator=200), None)
    assert (decision.scope, decision.reason) == ("full", "first_run")
    assert decision.suggested_full is False

    # 窗口再大、命中再多，也不能在「没有基线」之外凭空造出一次全量。
    big = decide_scope(
        _summary(real_delta=500, denominator=500, critical_hits=["config/x.xlsx（命中）"]),
        None,
    )
    assert (big.scope, big.reason) == ("full", "first_run")


def test_a_watermark_baseline_still_counts_as_a_baseline():
    """过渡态（只剩时间水位线、还没有冻结快照）也算「有基线」：判增量，不判首跑。"""
    decision = decide_scope(
        _summary(real_delta=2, denominator=10), datetime.now(timezone.utc)
    )
    assert decision.scope == "incremental"


def test_the_decision_is_a_frozen_value_object():
    """裁决是一份**可落库的账**：字段名与契约一致，且不可变（防止调用方顺手改）。"""
    decision = decide_scope(_summary(real_delta=5), _baseline(200))
    assert isinstance(decision, ScopeDecision)
    for field in (
        "scope",
        "reason",
        "suggested_full",
        "real_delta",
        "compensation",
        "dependency",
        "denominator",
        "ratio",
        "critical_source",
        "critical_hits",
    ):
        assert hasattr(decision, field), f"ScopeDecision 少了 {field}"
    with pytest.raises(Exception):
        decision.scope = "full"  # type: ignore[misc]


# ===========================================================================
#  三、写侧接线：summary 新字段、focus 前置、建议整窗不清基线、config/ 不命中
# ===========================================================================


def _make_batch(*, code_files: int = 0, table_files: int = 0, table_dir: str = "tables") -> dict:
    """一个周版本批次：可选的代码仓库 + 配表仓库，缓存行都是「已完成」。

    **路径刻意避开默认关键路径模式**（`tables/`、`code/`），这样量的是计数与 focus，
    不会被关键路径那条判据搅在一起（`config/` 那一条见本文件最后一个用例）。
    """
    now = datetime.now(timezone.utc)
    project = Project(code=_uid("P"), name=_uid("scope-project"), department="QA")
    db.session.add(project)
    db.session.flush()
    group = {"project": project, "rows": {}, "commits": {}, "cfg_by_kind": {}}

    for kind, count in (("table", table_files), ("code", code_files)):
        if count <= 0:
            continue
        repo = Repository(
            project_id=project.id,
            name=_uid(f"repo_{kind}"),
            type="git",
            url=f"https://example.com/{_uid('r')}.git",
            branch="main",
            resource_type=kind,
            clone_status="completed",
        )
        db.session.add(repo)
        db.session.flush()
        cfg = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repo.id,
            # 同一个版本名 → 同一分组（判据见 `weekly_batch_configs`）。
            name=f"W-scope - {repo.name}",
            branch="main",
            start_time=now - timedelta(days=7),
            end_time=now,
            is_active=True,
            auto_sync=True,
            status="active",
        )
        db.session.add(cfg)
        db.session.flush()
        group["cfg_by_kind"][kind] = cfg
        for index in range(count):
            if kind == "table":
                path = f"{table_dir}/table_{index}.xlsx"
                file_type = "excel"
            else:
                path = f"code/mod_{index}.lua"
                file_type = "code"
            commit = f"c{kind}{index:04d}"
            row = WeeklyVersionDiffCache(
                config_id=cfg.id,
                repository_id=repo.id,
                file_path=path,
                file_type=file_type,
                merged_diff_data="{}",
                base_commit_id="b" * 40,
                latest_commit_id=commit,
                commit_count=1,
                cache_status="completed",
                diff_version="1.18.0",
            )
            db.session.add(row)
            group["rows"][path] = row
            group["commits"][path] = commit
    db.session.commit()
    return group


def _table_config_id(group) -> int:
    return group["cfg_by_kind"]["table"].id


def _paths(group, *, prefix: str = "tables/", indices=None) -> list:
    rows = [path for path in group["rows"] if path.startswith(prefix)]
    if indices is None:
        return rows
    return [_sorted_paths(group, prefix)[index] for index in indices]


def _sorted_paths(group, prefix: str) -> list:
    def _index(path: str) -> int:
        return int(path.rsplit("_", 1)[-1].split(".")[0])

    return sorted(
        (path for path in group["rows"] if path.startswith(prefix)), key=_index
    )


def _change(group, paths) -> None:
    """这些文件有了新提交 —— 变的是**内容身份**，不是 `updated_at`。"""
    for path in paths:
        row = group["rows"][path]
        row.latest_commit_id = f"d{row.latest_commit_id}"
    db.session.commit()


def _build_and_settle(group, config_id: int, *, status: str = "succeeded") -> dict:
    """走一遍生产路径：建 payload → 建 run（冻结目标快照）→ 落终态 → 推进指针。"""
    payload, state, skip = ai_service.build_weekly_payload(config_id)
    assert skip is None, f"这次 payload 没建出来：{skip}"
    run = ai_service._create_run(
        project_id=group["project"].id,
        target_type="weekly",
        target_id=config_id,
        target_key=payload["group"]["key"],
        response_mode="blocking",
        scope=payload["scope"],
        trigger_source="manual",
        payload=payload,
    )
    run.status = status
    run.conclusion_structured = True
    run.active_key = None
    run.response_text = "报告正文"
    run.response_payload = "{}"
    run.finished_at = datetime.now(timezone.utc)
    db.session.commit()
    ai_service._update_weekly_state(payload, run, state, engine_status=status)
    return {"run": run, "payload": payload}


def _record_evidence(run, paths, commits) -> None:
    """给这条 run 落一份逐轮明细：这些文件**取到过证据**（其余进补偿集）。"""
    details = [
        {
            "kind": "file_diff",
            "label": f"file_diff {str(commits.get(path, ''))[:12]} {path}",
            "failed": False,
            "empty": False,
        }
        for path in paths
    ]
    db.session.add(
        AiAnalysisTrace(
            run_id=run.id,
            round_index=1,
            executed_json=json.dumps({"items": len(details), "details": details}),
        )
    )
    db.session.commit()


def test_the_summary_separates_real_changes_from_carried_items():
    """`scoped_delta_files` 只数真实变化；`delta_files` / `batch_files` 语义不变。

    实测 run 62 的病根就在这两个数混用：`delta_files` 含补偿（≤20）与依赖（≤50），
    而依赖上限恰好等于文件数阈值 —— 单靠它们就能触发「变化很多」。
    """
    with app.app_context():
        create_tables()
        group = _make_batch(table_files=40)
        config_id = _table_config_id(group)

        first = _build_and_settle(group, config_id, status="degraded")
        assert first["payload"]["scope"] == "full", "首跑不是全量，后面的增量不成立"

        # 上一轮只对 30 个文件取到了证据 → 另外 10 个进补偿集。
        uncovered = set(_paths(group, indices=range(30, 40)))
        _record_evidence(
            first["run"],
            [path for path in group["rows"] if path not in uncovered],
            group["commits"],
        )
        _change(group, _paths(group, indices=range(5)))

        payload, _state, skip = ai_service.build_weekly_payload(config_id)
        assert skip is None
        summary = payload["summary"]

        assert summary["scoped_delta_files"] == 5, "真实变化被补偿/依赖灌水了"
        assert summary["compensation_files"] == 10
        assert summary["carried_files"] == 10
        assert summary["delta_files"] == 15, "白名单口径（含补偿/依赖）被改了"
        assert summary["batch_files"] == 15
        # 分母是**基线快照条数**（40），不是整窗缓存行数；两者这里恰好相等，
        # 但下一组用例会把它们分开。
        assert summary["denominator"] == 40
        assert summary["delta_ratio"] == pytest.approx(5 / 40)
        assert payload["policy"]["decision"]["real_delta"] == 5
        assert payload["policy"]["decision"]["compensation"] == 10
        assert payload["scope"] == "incremental"


def test_a_table_focus_counts_only_the_table_delta_before_deciding_scope():
    """**focus 前移**：100 个代码改动 + 20 张配表，选「仅配表」时分子只有 20。

    旧行为的判定在 focus **之前**：拿 120 个文件比阈值，于是 `delta_count_high`、
    用户选的「仅配表」对 `scope` 一点作用都没有（审查报告 :37 点名的那一条）。
    修好之后：分子 20、分母仍是基线条数 120（不能跟着 focus 缩到 20，否则 20/20
    会变成 100%、反而必中比例阈值）。
    """
    with app.app_context():
        create_tables()
        group = _make_batch(code_files=100, table_files=20)
        config_id = _table_config_id(group)
        # 本用例量的是**focus 前移**（混合批次 120 个文件里只看 20 张表）：前提是整批
        # 都进输入。`tables`（默认，WP14）会先按仓库收窄到配表那 20 张，focus 这一段
        # 就测不到了 —— 显式按 all 跑，把 profile 收窄与 focus 收窄当成两个变量。
        ok, message, errors = ai_service.update_project_analysis_config(
            group["project"].id, {"analysis_profile": "all"}, updated_by="tester"
        )
        assert ok, f"{message} {errors}"
        db.session.commit()

        # 首跑：整窗 120 条进输入，并冻结成下一轮的基线快照。
        first = _build_and_settle(group, config_id)
        assert len(first["payload"]["delta_files"]) == 120

        # 下一轮：120 个文件都变了。
        _change(group, list(group["rows"]))

        unfiltered, _state, skip = ai_service.build_weekly_payload(config_id)
        assert skip is None
        assert unfiltered["summary"]["scoped_delta_files"] == 120
        assert unfiltered["policy"]["reason"] == "delta_count_high"
        assert unfiltered["policy"]["suggested_full"] is True

        payload, _state, skip = ai_service.build_weekly_payload(config_id, focus="table")
        assert skip is None
        assert payload["scope"] == "incremental"
        assert payload["summary"]["scoped_delta_files"] == 20, "分子没跟着筛选走"
        assert payload["summary"]["delta_files"] == 20
        assert payload["summary"]["window_files"] == 120, "窗口数被 focus 改写了"
        assert payload["summary"]["denominator"] == 120, "分母跟着 focus 一起缩了"
        assert payload["policy"]["decision"]["real_delta"] == 20
        # 20 / 120 = 0.167 < 0.30，且 20 < 50 → 由配表那一半单独裁决成「小增量」。
        assert payload["policy"]["reason"] == "delta_small"
        assert payload["policy"]["suggested_full"] is False
        assert all(
            item["file_path"].startswith("tables/") for item in payload["delta_files"]
        )


def test_a_suggested_full_rerun_keeps_the_incremental_baseline():
    """平台建议整窗 ≠ 清空基线：`scope` 仍是增量、`baseline.kind` 仍是 snapshot。

    这是 QA 审查点名的那条断言。只要「建议」被写成 `scope=full`，预检与前端就会把它
    翻译成「必须整窗重跑」，下一次做差再也拿不到这条基线 —— 「首次全量后一直全量」
    的固化点。裁决账（`policy.decision`）同时要落库，否则事后没人答得出「为什么建议」。
    """
    with app.app_context():
        create_tables()
        group = _make_batch(table_files=60)
        config_id = _table_config_id(group)

        first = _build_and_settle(group, config_id)
        baseline_id = first["payload"]["snapshot"]["snapshot_id"]
        _change(group, list(group["rows"]))

        payload, _state, skip = ai_service.build_weekly_payload(config_id)
        assert skip is None
        assert payload["scope"] == "incremental"
        assert payload["policy"]["suggested_full"] is True
        assert payload["policy"]["reason"] == "delta_count_high"
        assert payload["policy"]["input_scope"] == "delta"
        assert payload["baseline"]["kind"] == "snapshot", "平台建议整窗时把基线清空了"
        assert payload["baseline"]["snapshot_id"] == baseline_id

        decision = payload["policy"]["decision"]
        assert decision["real_delta"] == 60
        assert decision["denominator"] == 60
        assert decision["ratio"] == pytest.approx(1.0)
        assert decision["thresholds"] == {"file_count": 50, "ratio": 0.30}
        assert decision["critical_hits"] == []
        # 建议整窗时 `delta_files` 仍是那 60 条 delta，不是整窗重算 —— 真全量要用户点。
        assert len(payload["delta_files"]) == 60


def test_a_config_table_no_longer_hits_the_default_critical_paths():
    """配表仓库改一张 `config/` 下的表 → 默认模式不再命中关键路径。

    实测 run 63 的 hits 原文就是这么来的：
    `config/10_role/角色等级表_CfgPlayerLevel.xlsx（命中关键路径模式 (?:^|/)config/）`。
    命中之后旧判据升 full；现在默认清单里没有 `config/`，而配表仓库的 `config/`
    一律按常规目录处理，于是「动一张表」不再等于「建议整窗」。
    """
    with app.app_context():
        create_tables()
        group = _make_batch(table_files=10, table_dir="config")
        config_id = _table_config_id(group)

        first = _build_and_settle(group, config_id)
        assert first["payload"]["scope"] == "full", "首跑不是全量，下面的对照不成立"

        _change(group, _paths(group, prefix="config/", indices=[0]))

        payload, _state, skip = ai_service.build_weekly_payload(config_id)
        assert skip is None
        assert payload["summary"]["critical_paths"] is False
        assert payload["summary"]["critical_path_hits"] == []
        assert payload["summary"]["critical_path_source"] == "default"
        assert (payload["scope"], payload["policy"]["reason"]) == (
            "incremental",
            "delta_small",
        )
        assert payload["policy"]["suggested_full"] is False


# ===========================================================================
#  四、analysis_profile：本次生效的是哪一份检查维度清单
# ===========================================================================
#
# 「只分析配表」不是给模型加一句措辞，而是**换一份合同**：出厂 9 个维度里有 4 个
# （`module_coupling` / `code_logic` / `version_branch` / `process`）在配表项目里拿不到
# 证据，却与配表维度抢同一份结论条数和同一批取证额度（方案 §3.2 L5 / §4.4）。
#
# 纯函数 `dimension_specs_for_profile` 的口径由 WP3 的 `test_ai_dimension_declaration`
# 守；这一节钉的是**接线**：`project_facts.dimensions` 与 `LoadedSkills.dimensions`。
# 规则：知识包里声明了 `dimensions:` → 以声明为准（不与 profile 取交集，否则
# `performance/protocol/resource` 这类清单会被交成空集，而协议要求它非空）；
# 没声明才按 `analysis_profile` 选。

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_dimension_pack(projects_root: Path, code: str, extra_frontmatter: str = "") -> None:
    """在临时项目根下写一份知识包（frontmatter 可选带 `dimensions:`）。"""
    slug = code.strip().lower()
    directory = projects_root / slug / "references"
    directory.mkdir(parents=True, exist_ok=True)
    frontmatter = f"name: {slug}\ndescription: {slug} 的项目事实"
    if extra_frontmatter:
        frontmatter += "\n" + extra_frontmatter
    (directory / "project-facts.md").write_text(
        f"---\n{frontmatter}\n---\n\n正文。\n", encoding="utf-8"
    )


def _make_profile_project(*, profile: str | None = None) -> Project:
    """一个落库的项目；`profile` 给定时显式写一行配置（`None` = 没有配置行）。"""
    project = Project(code=_uid("P"), name=_uid("profile-project"), department="QA")
    db.session.add(project)
    db.session.flush()
    if profile is not None:
        db.session.add(
            AiProjectAnalysisConfig(project_id=project.id, analysis_profile=profile)
        )
    db.session.commit()
    return project


def test_the_default_profile_limits_the_effective_list_to_the_five_table_ones(
    tmp_path, monkeypatch
):
    """没有配置行 → `resolved()` 的默认档 `tables` → 5 个配表维度。

    这条同时钉住「默认值必须生效」：配置面板还没做，但默认档不能停在字段里没人读。
    """
    with app.app_context():
        create_tables()
        project = _make_profile_project()
        monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
        _write_dimension_pack(tmp_path, project.code)

        declaration = project_facts.dimensions(project.code, project_id=project.id)
        assert declaration.dimensions == TABLE_DIMENSION_SPECS
        assert tuple(spec.id for spec in declaration.dimensions) == TABLE_DIMENSION_IDS
        assert "tables" in declaration.detail

        loaded = load_skills(REPO_ROOT, project_code=project.code)
        assert loaded.dimensions == TABLE_DIMENSION_SPECS, (
            "默认档没接到 LoadedSkills.dimensions 上 —— 提示词与分工仍按出厂 9 个跑"
        )


def test_the_all_profile_keeps_the_factory_nine(tmp_path, monkeypatch):
    """`analysis_profile=all` → 出厂那 9 个（同一个对象，不是副本），提示词不多一字节。"""
    with app.app_context():
        create_tables()
        project = _make_profile_project(profile="all")
        monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
        _write_dimension_pack(tmp_path, project.code)

        declaration = project_facts.dimensions(project.code, project_id=project.id)
        assert declaration.dimensions == DEFAULT_DIMENSION_SPECS

        loaded = load_skills(REPO_ROOT, project_code=project.code)
        assert loaded.dimensions == DEFAULT_DIMENSION_SPECS
        # 与出厂清单相同时不追加「本项目适用的维度清单」那一节（提示词逐字不变）。
        assert loaded.platform_skill.text == (
            REPO_ROOT / "skills" / "version-diff-review" / "SKILL.md"
        ).read_text(encoding="utf-8")


def test_a_declared_list_wins_over_the_profile(tmp_path, monkeypatch):
    """**声明优先，且不与 profile 取交集。**

    这个项目声明的是三个非配表维度；`tables` profile 与它交集为空 —— 取交集的后果是
    `final.dimensions` 为空，模型报出的每条异常都只能进「未归类」。声明是显式意图，
    profile 只补「没声明」那一档。
    """
    with app.app_context():
        create_tables()
        project = _make_profile_project(profile="tables")
        monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
        _write_dimension_pack(
            tmp_path,
            project.code,
            "dimensions: performance=性能与耗时, protocol=协议兼容性, resource=资源引用",
        )

        declaration = project_facts.dimensions(project.code, project_id=project.id)
        assert tuple(spec.id for spec in declaration.dimensions) == (
            "performance",
            "protocol",
            "resource",
        )
        assert declaration.source == project_facts.SOURCE_PROJECT

        loaded = load_skills(REPO_ROOT, project_code=project.code)
        assert tuple(spec.id for spec in loaded.dimensions) == (
            "performance",
            "protocol",
            "resource",
        )


def test_without_a_project_id_the_profile_is_not_consulted(tmp_path, monkeypatch):
    """只给项目代号时**不知道 profile** → 沿用旧行为（出厂 9 个）。

    `None` 是「这条路径没有库会话」的意思（`skill_revision`、知识包校验、脚本），
    它必须与「项目明确选了某档」分开：把未知当成 `tables` 会让这些路径上的结论与
    提示词悄悄换一份合同，而它们本来只回答「这个项目声明了什么」。
    """
    with app.app_context():
        create_tables()
        project = _make_profile_project(profile="tables")
        monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
        _write_dimension_pack(tmp_path, project.code)

        without_id = project_facts.dimensions(project.code, repo_root=tmp_path)
        assert without_id.dimensions == DEFAULT_DIMENSION_SPECS

        with_id = project_facts.dimensions(
            project.code, repo_root=tmp_path, project_id=project.id
        )
        assert with_id.dimensions == TABLE_DIMENSION_SPECS


def test_an_explicit_profile_wins_over_the_stored_config(tmp_path, monkeypatch):
    """调用方手上有**生效后**的 profile 时，以它为准（P2-1）。

    配置里是 `all`，但本次 payload 生效的是 `tables`（或反过来）—— 这不代表配置写错了，
    而是周版本回退 / 单提交分析这两条路的生效档本来就与配置不同。让 skill 加载回头去读
    配置，等于把「输入按 A、维度按 B」重新装回来。
    """
    with app.app_context():
        create_tables()
        project = _make_profile_project(profile="all")
        monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
        _write_dimension_pack(tmp_path, project.code)

        declaration = project_facts.dimensions(
            project.code, repo_root=tmp_path, project_id=project.id, profile="tables"
        )
        assert declaration.dimensions == TABLE_DIMENSION_SPECS

        loaded = load_skills(REPO_ROOT, project_code=project.code, profile="tables")
        assert loaded.dimensions == TABLE_DIMENSION_SPECS, (
            "profile 参数没传到 project_facts —— 仍在按配置里的 all 取 9 维"
        )


def test_a_declared_list_still_wins_over_an_explicit_profile(tmp_path, monkeypatch):
    """**声明优先**对显式 profile 同样成立：profile 只补「没声明」那一档。"""
    with app.app_context():
        create_tables()
        project = _make_profile_project(profile="tables")
        monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
        _write_dimension_pack(
            tmp_path,
            project.code,
            "dimensions: performance=性能与耗时, protocol=协议兼容性",
        )

        declaration = project_facts.dimensions(
            project.code, repo_root=tmp_path, project_id=project.id, profile="all"
        )
        assert tuple(spec.id for spec in declaration.dimensions) == (
            "performance",
            "protocol",
        )
        assert declaration.source == project_facts.SOURCE_PROJECT

        loaded = load_skills(REPO_ROOT, project_code=project.code, profile="all")
        assert tuple(spec.id for spec in loaded.dimensions) == (
            "performance",
            "protocol",
        )
