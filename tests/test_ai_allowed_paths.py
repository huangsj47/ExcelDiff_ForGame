# -*- coding: utf-8 -*-
"""`allowed_paths`：读取范围的显式声明 + 输出侧「范围外 / 关联不足」（契约 1.8 / WP14 / WP20 / WP27）。

## 这条链要挡的是哪类错

`allowed_paths` 是**读取范围**的声明：非空时只许读集合内路径，`None` = 不限制，空列表
= 一条都不许。WP7/WP14/WP17 曾把它当成 `profile=tables` 的收窄点；**WP20 起
`profile=tables` 不再产出集合**（输入收窄的是 `configs` / `delta_files`，不是读取范围），
所以这一份机制现在只服务两类输入：升级前写入的老 payload（里面带着集合）与将来的人工
收窄。机制本身一个字不改（`None` → 全放行，`[]` → 全拒绝）。

## 2026-11 WP14：`tables` 是「仓库」，不是「后缀」

最初那版把 `tables` 实现成「只保留 xlsx/csv 后缀」。真实项目里配表的加载器、导表产物、
协议文件常常和 xlsx 在**同一个仓库**里（`config/CfgItem.lua`、`config/item.py`、
`config/item.json`），它们变了的风险正是配表维度要回答的；按后缀挡掉等于让模型看不到
自己刚被交办的输入。判据因此落在 `Repository.resource_type` 上：

* 配表仓库里的**全部改动文件**（含 `.lua/.py/.json/.proto`）进 `delta_files`；
* 代码/资源仓库（`code` / `res` / NULL）的文件不进输入；
* 项目没有配表仓库 → 回退 `all`（否则一个文件都分析不了），并记
  `policy.decision.profile_fallback = "no_table_repository"`。

## 2026-11 WP20：**输入收窄 ≠ 读取收窄**

配置表不是孤立的：表与表之间、配表与代码仓库之间都有关联。`tables` 若继续把
`allowed_paths` 收成 delta 路径，未改动的引用方表 / 生成物 / 导表工具与代码仓库的
消费方代码（枚举、协议、常量）全都读不到，跨表与配表-代码关联取证直接退化。所以：

* `allowed_paths_for_profile("tables", ...) is None` —— 读取不限制（全部跟踪文件）；
* `delta_files` / 清单 / 快照 / 增量分母仍是**配表仓库的改动**（不变，问题 B 的收敛）；
* 代价（WP27 收回）：`allowed_paths=None` 时不再「全部放行」。WP20 当时接受「代码仓库
  上的结论全部进风险等级」，WP27 改成按**可核实关联**判 —— 配表 delta 本身、以及断言
  依据指向配表 delta 的结论参与定级；纯背景发现单列（`out_of_scope=True`，报告仍可见，
  不进等级、不计计数）。判据见 `result_payload._out_of_scope` 与
  `tests/test_ai_related_evidence_scope.py`。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from services.ai import project_config_source, report_document
from services.ai.analysis_scope_filter import (
    PROFILE_FALLBACK_NO_TABLE_REPOSITORY,
    allowed_paths_by_repository,
    allowed_paths_for_profile,
    filter_configs_for_profile,
)
from services.ai.change_set import from_weekly_payload
from services.ai.engine import EngineOutcome
from services.ai.protocol import Anomaly, Claim
from services.ai.result_payload import result_payload
from services.ai.skill_contract import DIMENSION_IDS, TABLE_DIMENSION_IDS
from services.ai_analysis_service import build_weekly_payload
from services.ai_usage_service import analysis_estimate

TABLE = "config/[30]道具表_CfgItem.xlsx"
CSV = "config/reward.csv"
# **配表仓库里的代码/产物**：它们不是「代码仓库的改动」，而是配表风险的证据。
TABLE_LUA = "config/loader/CfgItemLoader.lua"
TABLE_PY = "config/export/item_export.py"
TABLE_JSON = "config/proto/item_enum.json"
CODE = "code/a.lua"
CODE_EXTRA = "code/compensated.lua"
TABLE_COMMIT = "a" * 40
CSV_COMMIT = "b" * 40
CODE_COMMIT = "c" * 40
WINDOW_ONLY_COMMIT = "e" * 40

_MISSING = object()



def _payload(allowed_paths=_MISSING) -> dict:
    """手写一份周版本 payload：`_MISSING` = 老 payload（根本没有这个键）。"""
    payload = {
        "mode": "weekly",
        "scope": "incremental",
        "delta_files": [
            {
                "file_path": TABLE,
                "latest_commit_id": TABLE_COMMIT,
                "file_type": "table",
                "repository_id": 11,
            },
            {
                "file_path": CODE,
                "latest_commit_id": CODE_COMMIT,
                "file_type": "code",
                "repository_id": 22,
            },
        ],
    }
    if allowed_paths is not _MISSING:
        payload["allowed_paths"] = allowed_paths
    return payload


def _anomaly(*, title: str, file_path: str, severity: str, claims: tuple = ()) -> Anomaly:
    return Anomaly(
        title=title,
        category="config_id",
        severity=severity,
        confidence="high",
        evidence=(f"{file_path} 第 3 行",),
        commit="d" * 40,
        file_path=file_path,
        claims=claims,
    )


def _claim(ref: str) -> Claim:
    return Claim(
        claim_id="C1",
        kind="fact",
        statement="这处代码消费的是本次改过的配表",
        evidence_refs=(ref,),
    )


# ==========================================================================
# 一、显式集合的机制仍在（老 payload / 人工收窄；`profile=tables` 已不再产出）
# ==========================================================================


def test_an_explicit_allow_list_still_narrows_an_old_payload():
    """`allowed_paths` 非空 ⇒ 代码文件连「进清单」的机会都没有（老 payload 兼容）。

    WP20 之后写侧的 `tables` 不再产出集合（见下面「写侧接线」一节），但升级前已经
    落库的请求 payload 里还带着它 —— 这些运行必须按当时的声明执行，不能因为读了新代码
    就悄悄放大范围。
    """
    change = from_weekly_payload(_payload([TABLE]))

    assert change.scope.path_allowed(TABLE_COMMIT, TABLE) is True
    assert change.scope.path_allowed(CODE_COMMIT, CODE) is False, (
        "代码文件仍在授权表里 —— 模型能读到它的 diff，报告层再过滤已经晚了"
    )
    assert CODE_COMMIT not in change.scope.commits, "越界路径所属的提交也不该进白名单"
    assert [commit.commit for commit in change.commits] == [TABLE_COMMIT]
    assert CODE not in change.summary, "越界路径的名字仍进了给模型看的变更清单"
    assert all(entry.path != CODE for entry in change.scope.entries), (
        "`entries`（三元组归属）里还有代码路径 —— 三处授权数据必须收窄同一批"
    )


def test_path_spellings_are_normalized_before_membership():
    """白名单里的写法差异不许变成「假拒绝」（反斜杠 / `./` 前缀）。"""
    change = from_weekly_payload(_payload(["./" + TABLE.replace("/", "\\")]))

    assert change.scope.path_allowed(TABLE_COMMIT, TABLE) is True
    assert change.scope.path_allowed(CODE_COMMIT, CODE) is False


def test_all_profile_and_legacy_payloads_keep_the_old_behavior():
    """`None`（profile=all）与「缺键」（老 payload）都必须逐字回到旧行为。"""
    for payload in (_payload(None), _payload()):
        change = from_weekly_payload(payload)

        assert change.scope.path_allowed(CODE_COMMIT, CODE) is True
        assert change.scope.path_allowed(TABLE_COMMIT, TABLE) is True
        assert [commit.commit for commit in change.commits] == [TABLE_COMMIT, CODE_COMMIT]


def test_an_empty_allow_list_refuses_every_path():
    """空列表 = 「一条都不许」（2026-11-01 WP9 修正），与缺键**不是**同义词。

    原先空列表与缺键同义（都按不限制）是一处安全口子：老写侧在「一张配表都没有」的
    批次里给的就是空列表，模型会因此读得到全部代码 diff —— 与平台声明正好相反。
    WP20 之后 `tables` 不再产出空列表（没有 delta 也给 `None`），但**显式**空列表仍必须
    拒绝：将来若有人手工声明「什么都不许读」，这一层不能把它当成「没给」。
    缺键仍是「不知道，照旧全放」，见上面那条兼容用例。
    """
    change = from_weekly_payload(_payload([]))

    assert change.scope.path_allowed(CODE_COMMIT, CODE) is False
    assert change.scope.path_allowed(TABLE_COMMIT, TABLE) is False
    assert change.is_empty


def test_window_commit_files_are_narrowed_too():
    """窗口提交账是**第二种授权来源**：只筛 delta 等于留一道侧门。

    `build()` 会把「窗口提交自己改过的路径」补进 `paths_by_commit`（单提交 `file_diff`
    靠它）。显式集合不收窄这一份的话，一个代码提交只要不在收窄后的白名单键上，它改过的
    代码文件照样读得到 —— 集合就只在「提交号恰好相同」时才生效。
    """
    payload = _payload([TABLE])
    payload["window_commit_ids"] = [CODE_COMMIT, WINDOW_ONLY_COMMIT]
    payload["window_commit_files"] = {
        CODE_COMMIT: {
            "paths": [CODE],
            "repository_id": 22,
            "repositories": {"22": [CODE]},
        },
        # 这条提交**不在 delta 里**（只出现在窗口账上）：它最能说明「提交本身不许被删」。
        WINDOW_ONLY_COMMIT: {
            "paths": [CODE_EXTRA],
            "repository_id": 22,
            "repositories": {"22": [CODE_EXTRA]},
        },
    }

    change = from_weekly_payload(payload)
    assert change.scope.path_allowed(CODE_COMMIT, CODE) is False, (
        "窗口账把越界代码重新加进了授权表 —— 模型照样读得到代码 diff"
    )
    assert change.scope.path_allowed(WINDOW_ONLY_COMMIT, CODE_EXTRA) is False
    assert change.scope.path_allowed(TABLE_COMMIT, TABLE) is True
    assert all(entry.path != CODE for entry in change.scope.entries)
    assert all(entry.path != CODE_EXTRA for entry in change.scope.entries)
    assert change.scope.resolve_commit(CODE_COMMIT) == CODE_COMMIT, (
        "提交本身被删了 —— 模型问「这个提交改了什么」会得到「不属于本批次」"
    )
    assert change.scope.resolve_commit(WINDOW_ONLY_COMMIT) == WINDOW_ONLY_COMMIT, (
        "只在窗口账上的提交被删了 —— `commit_detail` 会退化成「不属于本批次」"
    )
    assert not change.scope.changed_paths(CODE_COMMIT), "那条提交的路径集应该已经空了"
    assert not change.scope.changed_paths(WINDOW_ONLY_COMMIT)

    legacy = from_weekly_payload({**payload, "allowed_paths": None})
    assert legacy.scope.path_allowed(CODE_COMMIT, CODE) is True, (
        "`None` 时窗口账被收窄了 —— 旧行为必须逐字不变"
    )
    assert legacy.scope.path_allowed(WINDOW_ONLY_COMMIT, CODE_EXTRA) is True


def test_out_of_scope_compensation_items_are_not_advertised():
    """补偿/依赖清单是**给模型看的待核查项**：列一个它读不到的文件只会变成假信息缺口。"""
    payload = _payload([TABLE])
    payload["delta_files"].append(
        {
            "file_path": CODE_EXTRA,
            "latest_commit_id": CODE_COMMIT,
            "source": "compensation",
        }
    )

    narrowed = from_weekly_payload(payload)
    assert CODE_EXTRA not in narrowed.summary

    legacy = from_weekly_payload({**payload, "allowed_paths": None})
    assert CODE_EXTRA in legacy.summary, "`None` 时补偿项清单也被收窄了 —— 旧行为被改掉"


# ==========================================================================
# 二、输出侧：`tables` 下只有**有可核实关联**的结论参与定级（WP20 + WP27）
#
# 「显式集合仍把越界条目标成范围外、不计入定级」的机制用例在
# `tests/test_ai_result_payload.py` 里（`allowed_paths=["config/道具表.xlsx"]`）。
# 本节钉的是 `allowed_paths=None`（写侧 `tables` 的默认）那一档：WP20 曾让代码仓库上
# 的结论**全部**进等级；WP27 改成按「与本次配表差异有没有可核实关联」分三档
# （配表 / 有关联 / 无关联），完整判据见 `tests/test_ai_related_evidence_scope.py`。
# ==========================================================================


def _outcome(*anomalies: Anomaly) -> EngineOutcome:
    return EngineOutcome(
        status="succeeded",
        anomalies=tuple(anomalies),
        report_markdown="# 风险评估\n\n共两条。\n",
    )


def test_under_tables_only_related_conclusions_reach_the_level():
    """WP27：`allowed_paths=None` 时按「可核实关联」分三档 —— 配表 / 有关联 / 无关联。

    WP20 的写法是「代码仓库上的结论照常进等级」，等于把读取放开的代价整个转嫁给定级：
    模型读得越全，报告看起来越严重。WP27 把判据收回到**本次配表差异**上：

    * 配表 delta 自己的结论 → 参与定级；
    * 代码消费方 / 未改动引用方表的结论，但断言依据指向配表 delta → 参与定级（可核实）；
    * 纯背景的代码结论 → 照常列出（`out_of_scope=True`），不抬等级、不计入条数。
    """
    outcome = _outcome(
        _anomaly(title="配表 ID 冲突", file_path=TABLE, severity="medium"),
        _anomaly(
            title="消费方漏改判断",
            file_path=CODE,
            severity="critical",
            claims=(_claim(f"{TABLE}:12"),),
        ),
        _anomaly(
            title="无关模块的并发问题",
            file_path="code/b.lua",
            severity="critical",
            claims=(_claim("code/b.lua:88"),),
        ),
    )

    payload = result_payload(
        outcome,
        {
            "summary": {},
            "allowed_paths": None,
            "delta_files": [{"file_path": TABLE, "latest_commit_id": TABLE_COMMIT}],
        },
    )

    by_title = {entry["title"]: entry for entry in payload["anomalies"]}
    assert by_title["配表 ID 冲突"]["out_of_scope"] is False, (
        "配表 delta 自己的结论被单列了 —— 等级会掉到规模兜底"
    )
    assert by_title["消费方漏改判断"]["out_of_scope"] is False, (
        "依据指向配表 delta 的代码结论被单列 —— 可核实关联没生效"
    )
    assert by_title["无关模块的并发问题"]["out_of_scope"] is True, (
        "纯背景的代码结论进了定级 —— 读得越全、报告越严重"
    )
    # **条目一个不少**：单列不是丢掉，报告里仍看得见模型报过什么。
    assert len(payload["anomalies"]) == 3
    assert payload["risk_level"] == "high", "有可核实关联的 critical 没有参与定级"
    assert payload["risk_reasons"][0] == "模型报出 2 条达门槛的问题"
    assert any("另有 1 条" in reason for reason in payload["risk_reasons"]), (
        "单列的条数没有交代 ——「平台不采纳」会被读成「模型觉得没问题」"
    )
    # 报告正文/导出仍照常渲染三条：范围内那两条是这次要处理的，背景那条单列但可见。
    text = report_document.build_report_markdown(
        anomalies=payload["anomalies"], report_text="# 风险评估\n\n共三条。\n"
    )
    assert "配表 ID 冲突" in text and "消费方漏改判断" in text
    assert "无关模块的并发问题" in text, "背景发现在报告里消失了 —— 它不是被丢掉，是被单列"


def test_anomalies_without_a_file_path_are_not_out_of_scope():
    """跨文件 / 流程类结论没有指认任何文件，按越界吞掉会静默降级。"""
    outcome = _outcome(_anomaly(title="整体流程缺回滚", file_path="", severity="high"))

    payload = result_payload(outcome, {"summary": {}, "allowed_paths": [TABLE]})

    assert payload["anomalies"][0]["out_of_scope"] is False
    assert payload["risk_level"] == "mid_high", "没有文件路径的结论也必须参与定级"


def test_without_delta_files_a_code_conclusion_is_conservatively_listed():
    """老 payload（缺 `delta_files`）没有「本次差异」可对：有路径的结论一律单列。

    `None` 与缺键仍是同一种「读取不限制」，但**定级**不再无条件放行代码结论（WP27）：
    关联锚点缺失时宁可少算，也不许背景发现把等级抬上去。无路径的流程结论仍放行 ——
    「什么都不许读」不等于「什么都不许说」。
    """
    outcome = _outcome(
        _anomaly(title="代码分支条件写错", file_path=CODE, severity="critical"),
        _anomaly(title="整体流程缺回滚", file_path="", severity="high"),
    )

    legacy = result_payload(outcome, {"summary": {}})
    unlimited = result_payload(outcome, {"summary": {}, "allowed_paths": None})

    assert legacy["anomalies"] == unlimited["anomalies"], "缺键与 `None` 不再是同一口径"
    by_title = {entry["title"]: entry for entry in legacy["anomalies"]}
    assert by_title["代码分支条件写错"]["out_of_scope"] is True, (
        "缺 `delta_files` 时代码结论仍进等级 —— 关联锚点缺失没有按保守方向判"
    )
    assert by_title["整体流程缺回滚"]["out_of_scope"] is False
    assert legacy["risk_level"] == unlimited["risk_level"] == "mid_high"
    assert legacy["risk_reasons"] == unlimited["risk_reasons"]
    assert any("另有 1 条" in reason for reason in legacy["risk_reasons"])


# ==========================================================================
# 三、写侧接线：输入按「仓库」收窄，读取**不**收窄（WP14 + WP20）
# ==========================================================================


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _seed_cache(config, repo, path: str, commit: str, file_type: str | None) -> None:
    db.session.add(
        WeeklyVersionDiffCache(
            config_id=config.id,
            repository_id=repo.id,
            file_path=path,
            # 生产数据里这一列可能是 NULL（写侧只写缓存内容）—— 按仓库判据下它不参与决策，
            # 但真实数据长这样，用例也得长这样。
            file_type=file_type,
            latest_commit_id=commit,
            commit_count=1,
        )
    )


def _make_project(*, table_files=(), code_files=()) -> dict:
    """一个项目：可选的配表仓库 + 代码仓库，各自带同批次的周版本配置与缓存行。

    `files` 是 `(路径, file_type)` 序列。两个仓库共用「W1 - <repo>」的版本名，所以
    `weekly_batch_configs` 认为它们是**同一批**（这正是本文件要收窄的那个集合）。
    """
    project = Project(code=_uid("P"), name=_uid("ai-project"))
    db.session.add(project)
    db.session.flush()
    made: dict[str, dict] = {}
    start_time = datetime(2026, 3, 1, 0, 0)
    end_time = datetime(2026, 3, 8, 0, 0)
    for kind, files in (("table", table_files), ("code", code_files)):
        if not files:
            continue
        repo = Repository(
            project_id=project.id,
            name=_uid(kind),
            type="git",
            url=f"https://example.invalid/{_uid(kind)}.git",
            branch="main",
            resource_type=kind,
            clone_status="completed",
        )
        db.session.add(repo)
        db.session.flush()
        cfg = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repo.id,
            name=f"W1 - {repo.name}",
            description="",
            branch="main",
            start_time=start_time,
            end_time=end_time,
            cycle_type="custom",
            is_active=True,
            auto_sync=True,
            status="active",
        )
        db.session.add(cfg)
        db.session.flush()
        for index, (path, file_type) in enumerate(files):
            _seed_cache(cfg, repo, path, f"{kind[0]}{index:039d}", file_type)
        made[kind] = {"repo": repo, "cfg": cfg}
    db.session.commit()
    return {"project": project, "repos": made}


def _commit_by_path(payload: dict) -> dict[str, str]:
    return {item["file_path"]: item["latest_commit_id"] for item in payload["delta_files"]}


def test_tables_narrows_the_input_but_not_the_read_scope():
    """**WP14 + WP20 的核心断言**：输入只装配表仓库的改动，读取**不**设路径上限。

    两个口径必须同时成立，缺一个都会退化：
    * 输入收窄（WP14 / 问题 B）：代码仓库的文件不进 `delta_files`、不进快照/分母；
    * 读取放宽（WP20）：`allowed_paths is None`，配表仓库里**未改动**的引用方表、生成物、
      导表工具，以及代码仓库里谁消费这张表，都能按需读（冻结仓库一层见
      `tests/test_ai_frozen_repo_allowed_paths.py`）。
    """
    with app.app_context():
        create_tables()
        made = _make_project(
            table_files=[
                (TABLE, None),
                (CSV, "table"),
                (TABLE_LUA, "code"),
                (TABLE_PY, None),
                (TABLE_JSON, "json"),
            ],
            code_files=[(CODE, "code")],
        )
        table_repo_id = made["repos"]["table"]["repo"].id
        table_cfg_id = made["repos"]["table"]["cfg"].id

        payload, _state, skip = build_weekly_payload(table_cfg_id)
        assert skip is None

        expected = {TABLE, CSV, TABLE_LUA, TABLE_PY, TABLE_JSON}
        assert payload["analysis_profile"] == "tables"
        assert {item["file_path"] for item in payload["delta_files"]} == expected, (
            "配表仓库里的代码/产物被后缀过滤掉了 —— 模型读不到刚被交办的输入"
        )
        assert {item["repository_id"] for item in payload["delta_files"]} == {
            table_repo_id
        }, "代码仓库的文件进了输入 —— 输入收窄（问题 B）被退回去了"
        assert payload["group"]["config_ids"] == [table_cfg_id], (
            "代码仓库的配置仍在本批次里 —— 快照与分组会把它一起冻进去"
        )
        # **读取范围**（WP20）：不限制 = 允许读全部跟踪文件。
        assert payload["allowed_paths"] is None, (
            "tables 又把读取范围收成 delta 路径 —— 未改动的引用方表与代码仓库的消费方读不到"
        )
        assert payload["policy"]["decision"]["read_scope"] == "all_tracked"
        assert payload["policy"]["decision"]["allowed_files"] is None
        assert "profile_fallback" not in payload["policy"]["decision"], (
            "有配表仓库却记了回退 —— 读侧会以为这次跑的是 all"
        )

        change = from_weekly_payload(payload)
        commits = _commit_by_path(payload)
        for path in (TABLE, CSV, TABLE_LUA, TABLE_PY, TABLE_JSON):
            assert change.scope.path_allowed(commits[path], path) is True, (
                f"配表仓库内的 {path} 读不到 —— allowed_paths 又按后缀筛了"
            )


def test_the_all_profile_still_reads_every_repository():
    """`all` 是与「没有 profile 这个概念」时逐字相同的那一档。"""
    with app.app_context():
        create_tables()
        made = _make_project(table_files=[(TABLE, "table")], code_files=[(CODE, "code")])
        ok, message, errors = project_config_source.update_project_analysis_config(
            made["project"].id, {"analysis_profile": "all"}, updated_by="tester"
        )
        assert ok, f"{message} {errors}"

        payload, _state, skip = build_weekly_payload(made["repos"]["table"]["cfg"].id)
        assert skip is None
        assert payload["analysis_profile"] == "all"
        assert payload["allowed_paths"] is None, "profile=all 仍写了白名单 —— 旧行为被改掉了"
        assert {item["file_path"] for item in payload["delta_files"]} == {TABLE, CODE}
        assert "profile_fallback" not in payload["policy"]["decision"]

        change = from_weekly_payload(payload)
        commits = _commit_by_path(payload)
        assert change.scope.path_allowed(commits[TABLE], TABLE) is True
        assert change.scope.path_allowed(commits[CODE], CODE) is True


def test_a_project_without_a_table_repository_falls_back_to_all():
    """没有配表仓库 → 回退 `all` 并留痕；否则这个项目一个文件都分析不了。"""
    with app.app_context():
        create_tables()
        made = _make_project(code_files=[(CODE, "code")])

        payload, _state, skip = build_weekly_payload(made["repos"]["code"]["cfg"].id)
        assert skip is None, "没有配表仓库的项目被拦成了空跑 —— 用户点了分析却没有输入"
        assert payload["analysis_profile"] == "all", "生效值没跟着回退走"
        assert payload["allowed_paths"] is None, "回退 all 后仍写了白名单"
        assert payload["policy"]["decision"]["profile_fallback"] == (
            PROFILE_FALLBACK_NO_TABLE_REPOSITORY
        )
        assert {item["file_path"] for item in payload["delta_files"]} == {CODE}

        change = from_weekly_payload(payload)
        commits = _commit_by_path(payload)
        assert change.scope.path_allowed(commits[CODE], CODE) is True


def test_the_weekly_plan_uses_the_effective_profiles_dimensions():
    """周版本 `tables` → 计划里是 5 个配表维度（P2-1 的接线）。

    维度清单决定子代理分工与报告分组；`build_weekly_payload` 里算计划时传的 profile
    必须与 payload 的生效值同源 —— 传错档的表现是「输入是配表、清单却是 9 维」。
    """
    with app.app_context():
        create_tables()
        made = _make_project(table_files=[(TABLE, "table")], code_files=[(CODE, "code")])

        payload, _state, skip = build_weekly_payload(made["repos"]["table"]["cfg"].id)

        assert skip is None
        assert payload["analysis_profile"] == "tables"
        assert payload["plan"]["dimensions"] == list(TABLE_DIMENSION_IDS), (
            f"tables 生效时计划用的是另一份维度清单：{payload['plan']['dimensions']}"
        )
        assert len(payload["plan"]["dimensions"]) == 5


def test_a_project_without_a_table_repository_uses_the_nine_dimensions():
    """没有配表仓库 → payload 回退 `all`，维度清单也必须跟着回退到 9 个（P2-1）。

    这是「输入回退」与「维度清单」的一次跨层对齐：payload 说这次跑的是 all，加载 skill
    时若还按项目配置的 `tables` 取，模型会只拿到 5 个配表维度 —— 而代码改动恰好不在
    这 5 个里，报告会以「覆盖完整」的样子漏掉它们。
    """
    with app.app_context():
        create_tables()
        made = _make_project(code_files=[(CODE, "code")])

        payload, _state, skip = build_weekly_payload(made["repos"]["code"]["cfg"].id)

        assert skip is None
        assert payload["analysis_profile"] == "all", "生效值没跟着回退走"
        assert payload["plan"]["dimensions"] == list(DIMENSION_IDS), (
            f"回退 all 之后计划仍是配表维度：{payload['plan']['dimensions']}"
        )
        assert len(payload["plan"]["dimensions"]) == 9


def test_the_config_filter_reuses_the_repository_resource_type():
    """判据与 `focus_scope.resource_type_of` 同一份：`res` 与 NULL 都算代码仓库。"""
    configs = [
        SimpleNamespace(repository=SimpleNamespace(resource_type="table")),
        SimpleNamespace(repository=SimpleNamespace(resource_type="code")),
        SimpleNamespace(repository=SimpleNamespace(resource_type="res")),
        SimpleNamespace(repository=None),
    ]

    kept, fallback = filter_configs_for_profile(configs, "tables")
    assert kept == [configs[0]]
    assert fallback == ""

    assert filter_configs_for_profile(configs, "all") == (configs, "")

    only_code, fallback = filter_configs_for_profile(configs[1:], "tables")
    assert only_code == configs[1:], "没有配表仓库时应原样返回（调用方据此回退 all）"
    assert fallback == PROFILE_FALLBACK_NO_TABLE_REPOSITORY


def test_the_allow_list_is_no_longer_derived_from_the_delta():
    """WP20：`tables` 与 `all` 都返回 `None` —— 读取范围不再由 delta 路径拼出来。

    这条是本节的口径锚点：白名单若还是 delta 的路径集合，未改动的引用方表、生成物与
    代码仓库里的消费方就都读不到；反过来空 delta 也不能被当成「一条都不许读」（`[]`），
    否则「这一批没有配表改动」会被误读成「什么都不能看」。
    """
    delta = [
        {"file_path": TABLE_LUA},
        {"file_path": TABLE},
        {"file_path": ""},
        {"file_path": TABLE_LUA},
    ]
    assert allowed_paths_for_profile("tables", delta) is None
    assert allowed_paths_for_profile("all", delta) is None
    # 空 delta 同样是 `None`（不限制），**不是** `[]`（一条都不许）。
    assert allowed_paths_for_profile("tables", []) is None
    # 认不出来的脏值按 `tables` 归一：结果仍是 `None`，而不是静默退回某个旧分支。
    assert allowed_paths_for_profile("", delta) is None


# ==========================================================================
# 分仓白名单（P1-3）：扁平集合不含仓库身份，同名路径会跨仓库借道
# ==========================================================================


def test_allowed_paths_are_grouped_by_repository():
    """按 `repository_id` 分组、保序去重；缺 id 或空路径的条目跳过（不猜归属）。

    顺序有意义：调用方把这份映射交给 `FrozenTreeReader` 之后，拒绝理由里列的是
    「这个仓库允许哪些路径」，而那是给人（与模型）读的账。
    """
    delta = [
        {"file_path": TABLE, "repository_id": 11},
        {"file_path": CSV, "repository_id": 11},
        {"file_path": CODE, "repository_id": 22},
        {"file_path": TABLE, "repository_id": 11},  # 重复：只留第一次
        {"file_path": TABLE_LUA},  # 缺 id：无法归属，跳过
        {"file_path": "", "repository_id": 11},  # 空路径：跳过
    ]

    assert allowed_paths_by_repository(delta) == {
        11: [TABLE, CSV],
        22: [CODE],
    }
    assert allowed_paths_by_repository([]) == {}


def test_the_payload_carries_the_per_repository_whitelist(monkeypatch):
    """`build_weekly_payload` 必须把白名单的**分仓形态**一起写进 payload。

    WP20 的生产口径是 `allowed_paths=None`（读取放开），所以这里用 monkeypatch 模拟
    「未来 strict / 老 payload 带集合」那一档 —— 不看形状的话，接线漏了也测不出来：
    provider 收到 `None` 会退回扁平集合，跨仓同名路径照样穿过去。
    """
    with app.app_context():
        create_tables()
        made = _make_project(table_files=[(TABLE, "table")], code_files=[(CODE, "code")])
        table_repo_id = made["repos"]["table"]["repo"].id
        cfg_id = made["repos"]["table"]["cfg"].id

        # 形状锚点：没有白名单时也写这个键（`None` = 老 payload 兼容）。
        payload, _state, skip = build_weekly_payload(cfg_id)
        assert skip is None
        assert "allowed_paths_by_repo" in payload
        assert payload["allowed_paths_by_repo"] is None

        # 显式白名单：按 delta 的仓库分组，代码仓库不在映射里。
        monkeypatch.setattr(
            ai_service, "allowed_paths_for_profile", lambda *_a, **_k: [TABLE]
        )
        payload, _state, skip = build_weekly_payload(cfg_id)
        assert skip is None
        assert payload["allowed_paths"] == [TABLE]
        assert payload["allowed_paths_by_repo"] == {table_repo_id: [TABLE]}, (
            "白名单没有按仓库拆 —— 代码仓库里的同名路径会借道"
        )


# ==========================================================================
# 提示词里的范围说明：`tables` 收窄的是输入，不是读取（WP16 + WP20）
# ==========================================================================


def test_the_scope_note_separates_the_input_from_the_read_scope():
    """`profile=tables` 时那句范围说明要**同时**交代输入收窄与读取放开。

    `tables` 在**输入侧**就把代码 / 资源仓库整批筛掉了，而范围说明前面那几句说的仍是
    「本版本改动过的 N 个文件」—— 模型会把它读成整个版本，于是「配表仓库里没问题」
    被写成「本版本没问题」（实施记录 §15 第 5 条）。WP20 之后读取又放开了（代码仓库的
    消费方可以读），所以旧措辞「其它仓库不在输入里」也不够用了：它会被读成「代码读不到」，
    模型于是不去核对跨表 / 跨仓库引用。这一句必须把两面都写清楚。
    """
    from services.ai.change_set import _scope_note

    tables = _scope_note({"scope": "full", "analysis_profile": "tables", "summary": {}})
    assert "本次**分析输入**只含配置表仓库的改动" in tables
    assert "**读取范围**包含配置表仓库全部文件与代码仓库" in tables
    assert "结论优先落在配表改动上" in tables
    assert "其它仓库不在输入里" not in tables, "旧措辞会让模型以为代码仓库读不到"

    # 老 payload（升级前落库）带着显式集合：那时读取确实被收窄，措辞必须照旧 ——
    # 说「读取范围包含代码仓库」对它是假话。
    legacy_narrowed = _scope_note(
        {
            "scope": "full",
            "analysis_profile": "tables",
            "allowed_paths": [TABLE],
            "summary": {},
        }
    )
    assert "其它仓库不在输入里" in legacy_narrowed
    assert "**读取范围**包含" not in legacy_narrowed

    # **`all` / 缺键逐字不变**：那是「没有收窄」，不是「收窄了但没记」——
    # 老 payload 渲染出来的提示词必须与这一轮之前完全一致。
    assert (
        _scope_note({"scope": "full", "analysis_profile": "all", "summary": {}})
        == "本次分析范围：全量。"
    )
    assert _scope_note({"scope": "full", "summary": {}}) == "本次分析范围：全量。"

    # 已有的「只覆盖了一部分」那句逻辑不动：两句同时在。
    partial = _scope_note(
        {
            "scope": "full",
            "analysis_profile": "tables",
            "summary": {"window_files": 10, "batch_files": 4},
        }
    )
    assert "本版本改动过的 10 个文件里的 4 个" in partial
    assert "本次**分析输入**只含配置表仓库的改动" in partial


def test_the_precheck_mode_decision_reports_the_profile_fallback():
    """没有配表仓库 → 预检的 `mode_decision.profile_fallback` 要说出回退原因。

    写侧已经把它记进 `policy.decision.profile_fallback`，但那一行只在**跑完之后**的
    payload 里；预检发生在点「开始」之前 —— 不说出来，用户看到的是「本次只分析配置表
    仓库」的说明，而真正跑的是全部仓库（实施记录 §16.3 第 2 条那个「静默退成 all」）。
    """
    with app.app_context():
        create_tables()
        fallback_group = _make_project(code_files=[(CODE, "code")])
        fallback = analysis_estimate(
            fallback_group["project"].id,
            mode="incremental",
            config_id=fallback_group["repos"]["code"]["cfg"].id,
        )
        ok_group = _make_project(table_files=[(TABLE, "table")])
        normal = analysis_estimate(
            ok_group["project"].id,
            mode="incremental",
            config_id=ok_group["repos"]["table"]["cfg"].id,
        )

    assert (
        fallback["mode_decision"]["profile_fallback"]
        == PROFILE_FALLBACK_NO_TABLE_REPOSITORY
    ), fallback["mode_decision"]
    # **空串不写**：有配表仓库时形状与加这个键之前一致（界面按缺键 = 没回退处理）。
    assert "profile_fallback" not in normal["mode_decision"], normal["mode_decision"]
    # 预检回的还是「增量」：回退只改输入范围，不改模式（契约 1.5）。
    assert fallback["mode_decision"]["effective"] == "incremental"

