# -*- coding: utf-8 -*-
"""基线只能建立在**有结构化结论**的那次运行上。

## 这条为什么值得单独守

`baseline.py` 把上一次那批结论当成「这个版本当前仍成立的问题全集」，下一轮的提示词里
写着「这些是**已知问题，不要当作新发现重复报**」。这个语义只在结构化结论上成立。

而引擎有两条降级路径，落库时**都记成 `status="succeeded"`**（`_persist_outcome` 只把
`STATUS_FAILED` 映射成 "failed"）：

* 有 payload 的那种（轮次用尽 / 额度用尽 / 上下文被压 / 缺一个分片）——结论仍然是
  结构化的，只是浅。**这种可以当基线**，丢掉它反而会让上一批已知问题全部被当成新发现。
* 没有 payload 的那种（`DEGRADE_MARKDOWN`：模型没按协议给 JSON，只留下一份 markdown
  报告）——**一条结构化结论都没有**。

第二种被当成基线时，下一轮读到的基线是「共 0 条」，整段「已知问题」等于不存在：上次
报过的问题会被重新当成新发现报一遍，而**没有任何地方说得出这是为什么**。

## 为什么不能靠 `degradation` 那一列判

子代理模式下 `degradation` 是 `_worst()` 取最重的一档，而 `DEGRADE_MARKDOWN` 是 3、
`DEGRADE_SUBAGENT` 是 6 —— 汇总那一份只有 markdown、同时又缺了一个分片时，
`degradation` 报出来是 `subagent_gap`，**看不出结论其实只有 markdown**。周版本分析正是
子代理模式，所以这条路上必须有一个不会被盖掉的信号：`conclusion_structured`。

## 往回退，不是把历次并起来

被跳过的那次什么结论都没留下，退到上一条完整的「问题全集」正是本来该用的那份 ——
这与 `_baseline_findings` 里「不许把历次并起来」并不矛盾（那条禁的是翻出已修好的旧条目）。
跳过了就要说：摘要里会加一句说明，这里一并守。

**测试库是会话级共用的**（没有逐用例重置），所以每个用例自己造行、自己清。
"""
from __future__ import annotations

import uuid

import pytest

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from services.ai_analysis_service import (
    _baseline_digest,
    _baseline_findings,
    _previous_run,
    _skipped_unstructured_runs,
)


@pytest.fixture
def ctx():
    with flask_app.app_context():
        create_tables()
        yield


def _project() -> Project:
    project = Project(code=f"P{uuid.uuid4().hex[:8]}", name=f"基线{uuid.uuid4().hex[:6]}")
    db.session.add(project)
    db.session.flush()
    return project


def _run(project_id: int, target_key: str, *, structured, marker: str) -> AiAnalysisRun:
    """造一条运行。`structured=None` 表示失败/未完成（这一列是 NULL）。"""
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_key=target_key,
        status="succeeded" if structured is not None else "failed",
        conclusion_structured=structured,
        response_text=f"# 报告 {marker}\n",
    )
    db.session.add(run)
    db.session.flush()
    # 只有结构化那一支会落异常行（只有 markdown 的那种一条都没有）。
    if structured:
        db.session.add(
            AiAnalysisAnomaly(
                run_id=run.id,
                project_id=project_id,
                fingerprint=f"fp-{marker}",
                title=f"【配表】{marker} 的问题",
                severity="high",
                category="config_value",
                file_path="src/a.lua",
            )
        )
        db.session.flush()
    return run


def _cleanup(run_ids) -> None:
    for run_id in run_ids:
        AiAnalysisAnomaly.query.filter_by(run_id=run_id).delete()
        AiAnalysisRun.query.filter_by(id=run_id).delete()
    db.session.commit()


def test_a_markdown_only_run_is_not_used_as_the_baseline(ctx):
    """最近那次只有 markdown，基线必须退到更早那次真正有结论的。"""
    project = _project()
    key = f"g-{uuid.uuid4().hex[:8]}"
    older = _run(project.id, key, structured=True, marker="OLDER")
    newer = _run(project.id, key, structured=False, marker="MARKDOWN")
    # 时间上 newer 更晚：created_at 由 default 生成，同一秒内可能相同，
    # 所以显式错开，让「最近一条」这件事在测试里是确定的。
    newer.created_at = older.created_at.replace(year=older.created_at.year + 1)
    db.session.commit()
    try:
        picked = _previous_run("weekly", key)
        assert picked is not None
        assert picked.id == older.id, (
            "基线选到了只有 markdown 的那次运行 —— 它的结构化结论是空的，"
            "下一轮会把上次报过的问题全部当新发现重报。"
        )

        findings = _baseline_findings("weekly", key)
        assert [item.title for item in findings] == ["【配表】OLDER 的问题"]
    finally:
        _cleanup([older.id, newer.id])


def test_the_skip_is_disclosed_in_the_digest(ctx):
    """退到更早那次时，摘要里必须说一句「中间有分析没给出可比对的结论」。"""
    project = _project()
    key = f"g-{uuid.uuid4().hex[:8]}"
    older = _run(project.id, key, structured=True, marker="OLDER")
    newer = _run(project.id, key, structured=False, marker="MARKDOWN")
    newer.created_at = older.created_at.replace(year=older.created_at.year + 1)
    db.session.commit()
    try:
        assert _skipped_unstructured_runs("weekly", key, _previous_run("weekly", key)) == 1

        from services.ai.change_set import ChangeSet
        from services.ai.scope import AnalysisScope

        # 变更集在这一条里不重要：`_baseline_digest` 只用它的 paths 去判「哪条结论的
        # 证据过期了」，空集就是「这次没有文件变动」。
        change = ChangeSet(summary="", scope=AnalysisScope(), paths=())
        digest = _baseline_digest("weekly", key, change)
        assert "OLDER 的问题" in digest, digest
        assert "没有给出可比对的结论" in digest, (
            f"退到更早那次却没有说明，用户不知道中间那次白跑了。\n实际输出：\n{digest}"
        )
    finally:
        _cleanup([older.id, newer.id])


def test_a_structured_degraded_run_is_still_used_as_the_baseline(ctx):
    """反向：**有结构化结论**的降级（轮次/额度/分片缺口）照旧当基线。

    别把这条修过头：`_worst()` 会把 `DEGRADE_SUBAGENT` 排在 `DEGRADE_MARKDOWN` 之前，
    子代理模式下一次很普通的降级也带着一个吓人的标签 —— 但它那批结论是可用的，
    丢掉它反而让上一批已知问题全部被当成新发现。
    """
    project = _project()
    key = f"g-{uuid.uuid4().hex[:8]}"
    older = _run(project.id, key, structured=True, marker="OLDER")
    newer = _run(project.id, key, structured=True, marker="DEGRADED_BUT_OK")
    newer.created_at = older.created_at.replace(year=older.created_at.year + 1)
    db.session.commit()
    try:
        picked = _previous_run("weekly", key)
        assert picked is not None and picked.id == newer.id, (
            "有结构化结论的降级运行被跳过了 —— 它的结论是可用的。"
        )
        assert _skipped_unstructured_runs("weekly", key, picked) == 0
    finally:
        _cleanup([older.id, newer.id])


def test_a_null_conclusion_structured_is_not_used_as_the_baseline(ctx):
    """这一列是 NULL 的行（失败/未完成，以及加列之前的历史行）一律不当基线。"""
    project = _project()
    key = f"g-{uuid.uuid4().hex[:8]}"
    older = _run(project.id, key, structured=True, marker="OLDER")
    nulled = _run(project.id, key, structured=None, marker="NULL")
    nulled.status = "succeeded"  # 故意造出「succeeded 但形态未知」这一种
    nulled.created_at = older.created_at.replace(year=older.created_at.year + 1)
    db.session.commit()
    try:
        picked = _previous_run("weekly", key)
        assert picked is not None and picked.id == older.id, (
            "拿不准（NULL）的行被当成了基线 —— 拿不准就不该用。"
        )
    finally:
        _cleanup([older.id, nulled.id])


def test_the_read_side_still_shows_a_markdown_only_run(ctx):
    """**「能不能当基线」与「能不能看」是两把尺子。**

    只有 markdown 的那次照样有报告给用户看。`_latest_concluded_run` 是读侧，它**不许**
    跟着 `_previous_run` 一起把它排除掉 —— 那等于把用户的一次分析藏起来。
    """
    from services.ai_analysis_service import _latest_concluded_run

    project = _project()
    key = f"g-{uuid.uuid4().hex[:8]}"
    markdown_only = _run(project.id, key, structured=False, marker="MARKDOWN")
    markdown_only.finished_at = markdown_only.created_at
    db.session.commit()
    try:
        shown = _latest_concluded_run(
            [AiAnalysisRun.target_type == "weekly", AiAnalysisRun.target_key == key]
        )
        assert shown is not None and shown.id == markdown_only.id, (
            "读侧把只有 markdown 的那次也挡掉了 —— 用户的那份报告就看不见了。"
        )
    finally:
        _cleanup([markdown_only.id])


# ==========================================================================
#  用户**显式**点的全量：旧结论一个字都不进模型输入
#
#  实测的由来（2026-09-24）：全量运行仍然带着上一轮的清单，报告就把两条**从未存在过**
#  的物品 ID 写成「本期删除」的高风险 —— 直接读那个仓库的三个提交，每一版都只有
#  1001–1005，1006/1007 从来没有出现过。旧结论被注入之后，模型把它当成了既成事实。
#
#  **判据是运行账上的 `reason`，不是 `scope`**：平台把增量升格成全量时（`delta_ratio_high`
#  等）做差基线仍在同一快照上、结论可比，那时必须照常注入。这一组把两个方向都钉住。
# ==========================================================================


def _empty_change():
    from services.ai.change_set import ChangeSet
    from services.ai.scope import AnalysisScope

    # 变更集在这一组里不重要：`_baseline_digest` 只用它的 paths 判「哪条结论的证据过期了」，
    # 空集就是「这次没有文件变动」。
    return ChangeSet(summary="", scope=AnalysisScope(), paths=())


def test_a_user_requested_full_run_gets_no_history_at_all(ctx):
    """用户点了全量 → 摘要整段不出现（不是「共 0 条」，是**一个字都没有**）。

    「共 0 条」与「没有这一节」在提示词里是两件事：前者会被模型读成「历史清单是空的，
    所以这些问题是新发现」，后者才是「这次从零重判」。
    """
    from services.ai.baseline import FORCE_FULL_REASON

    project = _project()
    key = f"g-{uuid.uuid4().hex[:8]}"
    older = _run(project.id, key, structured=True, marker="OLDER")
    db.session.commit()
    try:
        change = _empty_change()
        # 先确认这一份历史本来是会被注入的 —— 否则下面那条断言可能因为「压根没取到」
        # 而假绿（这一族用例最容易踩的坑）。
        assert "OLDER 的问题" in _baseline_digest("weekly", key, change)

        digest = _baseline_digest(
            "weekly",
            key,
            change,
            baseline_account={"kind": "none", "reason": FORCE_FULL_REASON, "complete": False},
        )
        assert digest == "", f"用户点的全量仍然带着旧结论：{digest!r}"
    finally:
        _cleanup([older.id])


def test_a_platform_upgraded_full_still_carries_the_history(ctx):
    """**防过头**：平台把增量升格成全量时，旧结论必须照常注入。

    那一档的做差基线还在同一快照上（`kind == "snapshot"`），结论可比；关掉它等于让
    「1 小时前分析过、现在只多了 1 个 commit」这件事重新变成从零开始 —— 那正是增量评审
    用不下去的根本原因。
    """
    project = _project()
    key = f"g-{uuid.uuid4().hex[:8]}"
    older = _run(project.id, key, structured=True, marker="OLDER")
    db.session.commit()
    try:
        digest = _baseline_digest(
            "weekly",
            key,
            _empty_change(),
            baseline_account={"kind": "snapshot", "complete": True},
        )
        assert "OLDER 的问题" in digest, (
            "平台升格的全量被当成「用户点的全量」关掉了历史 —— 过头了"
        )
    finally:
        _cleanup([older.id])


def test_human_suppression_survives_a_user_requested_full(ctx):
    """**人工忽略跨全量保留**（产品决定，不是漏改）。

    摘要是**给模型的输入**，忽略是**用户的分类账**。用户已经判定「这条不用再报」，
    重跑一次不该把他的话作废；而被抑制的条目是用户自己要求抹掉的，不是平台静默抹掉的
    —— 所以这里不违反 `baseline_source` 那条「两边同源」的原则（它的理由是防「报告里
    被抹掉、摘要里也没说」这种两头不靠；全量档里摘要整段不出现，不存在「没说」）。
    """
    from services.ai.baseline import FORCE_FULL_REASON
    from services.ai_analysis_service import _suppressed

    project = _project()
    key = f"g-{uuid.uuid4().hex[:8]}"
    older = _run(project.id, key, structured=True, marker="IGNORED")
    AiAnalysisAnomaly.query.filter_by(run_id=older.id).update({"disposition": "ignored"})
    db.session.commit()
    try:
        change = _empty_change()
        assert _suppressed("weekly", key, change) == frozenset({"fp-IGNORED"})
        # 摘要关掉了，抑制照旧 —— 两者用的是**不同**的判据，这是刻意的。
        assert (
            _baseline_digest(
                "weekly",
                key,
                change,
                baseline_account={"kind": "none", "reason": FORCE_FULL_REASON},
            )
            == ""
        )
        assert _suppressed("weekly", key, change) == frozenset({"fp-IGNORED"}), (
            "全量把用户的人工忽略一起作废了 —— 那是他的分类账，不是模型的输入"
        )
    finally:
        _cleanup([older.id])


@pytest.mark.parametrize(
    "account,expected",
    [
        (None, False),
        ({}, False),
        ({"kind": "none"}, False),  # 真首跑：没有 reason
        ({"kind": "none", "reason": "force_full"}, True),
        ({"kind": "snapshot", "reason": "force_full"}, True),  # reason 说了算
        ({"kind": "watermark", "reason": "watermark"}, False),
        ({"kind": "none", "reason": "Force_Full"}, False),  # 大小写不宽容
        ({"kind": "none", "reason": None}, False),
        ("force_full", False),  # 脏值（不是映射）不许当成命中
    ],
    ids=[
        "没有账",
        "空账",
        "真首跑无 reason",
        "用户点全量",
        "快照但 reason 说全量",
        "老分组水位线",
        "大小写不同",
        "reason 是 None",
        "脏值不是映射",
    ],
)
def test_the_predicate_only_fires_on_an_explicit_full(account, expected):
    """判据只认一个值，其余一律**不关**。

    保守的方向是明确的：宁可多带一次旧结论，也不要因为账上一个字段没读到、
    或者读出一个没见过的值，就静默改变一次全量评审的输入。
    """
    from services.ai.baseline_source import run_ignores_history

    assert run_ignores_history(account) is expected

