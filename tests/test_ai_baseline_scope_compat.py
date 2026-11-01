# -*- coding: utf-8 -*-
"""分析范围（profile / focus / 仓库集）变了，旧结论**不得**当本轮基线（P1，2026-11-01）。

## 病灶（审查报告 P1，已核实）

`baseline_source.previous_run` 原先只按 `target_type/target_key` 取最近一条有结论的
运行；`ai_analysis_service._run_engine_and_persist` 随后 `previous_anomaly_rows` +
`reconcile_incremental_result` 直接继承。实测：把 `profile=all` 下代码仓库的 critical
当基线，再以 `profile=tables` 跑，那条 critical 仍被带进新报告，风险等级也被它抬高。

范围一改，上一轮那批结论回答的就是**另一批文件**的问题。「同一分组」不等于「同一
范围」—— 这个文件把这条边界钉在三个层次上：

* 签名本身（`analysis_scope_filter.scope_signature`，纯函数，不碰库）；
* 读侧（`previous_run` / `baseline_findings` / `previous_anomaly_rows` 只认**链上**
  同范围的那条运行；最近一条候选不同范围 / 旧 payload 判不了 → **链断，不继承**，
  不往回找更早的结论）；
* 写侧接线（`build_weekly_payload` 在指针那一轮不兼容时给 `payload["baseline"]` 落
  `scope_mismatch`，报告据此说明「历史结论未继承」）。

**为什么是链式、不是「找最近一条同范围的」**：A（tables）报了 X → B（all）改了那张表、
把 X 修好 → C（tables）只改另一张表。跳过 B 取 A，A 的 X 会原样回到 C 的报告里 ——
B 的修复被 A 的旧结论覆盖，而 B 正好夹在中间（只有它知道 X 已经不成立）。

**测试库是会话级共用的**（没有逐用例重置），所以每个用例自己造行、用唯一的分组键
隔离，互不打扰。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from services.ai.analysis_scope_filter import scope_signature, scope_signature_for_facts
from services.ai.baseline_source import (
    baseline_digest,
    baseline_findings,
    previous_anomaly_rows,
    previous_run,
)
from services.ai.incremental_baseline import reconcile_result

# 「这条运行没有 request_payload」与「request_payload 是 None」是两件事：
# 前者是 2026-11 之前的老行（判不了范围），后者是写坏了的行 —— 在签名这件事上
# 两者都判不了，但造行时得能区分，所以用一个哨兵。
_MISSING = object()


@pytest.fixture
def ctx():
    with flask_app.app_context():
        create_tables()
        yield


def _payload(profile: str = "tables", focus: str = "all", repository_ids=(1,)) -> dict:
    """一份 payload 里与范围有关的三样事实（其余键与签名无关）。"""
    return {
        "analysis_profile": profile,
        "focus": {"key": focus},
        "repositories": [{"repository_id": item} for item in repository_ids],
    }


def _project() -> Project:
    project = Project(code=f"P{uuid.uuid4().hex[:8]}", name=f"范围{uuid.uuid4().hex[:6]}")
    db.session.add(project)
    db.session.flush()
    return project


def _key() -> str:
    return f"grp-{uuid.uuid4().hex[:8]}"


def _run(
    project_id: int,
    key: str,
    *,
    when: datetime,
    payload=_MISSING,
    findings=(),
    status: str = "succeeded",
    structured: bool = True,
) -> AiAnalysisRun:
    """造一条运行（默认可当基线：跑完了、有结构化结论），可带若干条异常行。

    `payload` 缺省 = 不写 `request_payload`（2026-11 之前的老行）；`payload=None` =
    写了一列空值（同样判不了范围）。`status` / `structured` 用来造「还在跑的当前轮」
    与「只有 markdown 的降级」——这两档都**不在候选序列里**。
    """
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_key=key,
        status=status,
        scope="incremental",
        conclusion_structured=structured,
        created_at=when,
    )
    if payload is not _MISSING:
        run.request_payload = (
            json.dumps(payload, ensure_ascii=False) if payload is not None else None
        )
    db.session.add(run)
    db.session.flush()
    for fingerprint, severity, path in findings:
        db.session.add(
            AiAnalysisAnomaly(
                run_id=run.id,
                project_id=project_id,
                fingerprint=fingerprint,
                title=f"{fingerprint} 的问题",
                severity=severity,
                category="code_logic" if path.startswith("code/") else "config_value",
                file_path=path,
                evidence=json.dumps(["diff: x"], ensure_ascii=False),
                claims="[]",
            )
        )
    db.session.commit()
    return run


def _result(risk: str = "low") -> dict:
    return {
        "report_markdown": "# 本轮\n",
        "anomalies": [],
        "final_findings": [],
        "risk_level": risk,
        "risk_reasons": [],
    }


def test_previous_anomaly_rows_restores_scope_marks_from_the_saved_conclusion(ctx):
    project = _project()
    run = _run(
        project.id,
        _key(),
        when=datetime.now(timezone.utc),
        payload=_payload(),
        findings=(
            ("table-critical", "critical", "tables/a.xlsx"),
            ("background-critical", "critical", "code/unrelated.lua"),
        ),
    )
    run.response_payload = json.dumps(
        {"anomalies": [
            {"fingerprint": "table-critical", "out_of_scope": False},
            {"fingerprint": "background-critical", "out_of_scope": True},
        ]}
    )
    db.session.commit()

    rows = previous_anomaly_rows(run.id, scope_signature=scope_signature(_payload()))
    assert {row["fingerprint"]: row["out_of_scope"] for row in rows} == {
        "table-critical": False,
        "background-critical": True,
    }

    findings = baseline_findings("weekly", run.target_key, scope_signature=scope_signature(_payload()))
    assert {item.fingerprint: item.background for item in findings} == {
        "table-critical": False,
        "background-critical": True,
    }
    digest = baseline_digest(
        "weekly", run.target_key,
        change=SimpleNamespace(paths=()),
        scope_signature=scope_signature(_payload()),
    )
    assert "[背景发现：不计入风险等级]" in digest
    assert "背景发现不要列为当前风险" in digest


# ==========================================================================
# 一、签名本身：纯函数，缺键的默认值与「生效值以 payload 为准」
# ==========================================================================


def test_scope_signature_defaults_missing_keys_to_all():
    """缺键按 `all` —— 但这只是**签名函数的默认值**，读侧不会拿它去判旧 payload。

    `previous_run` 之所以能区分「老 payload」与「真的是 all 范围」，靠的是
    `baseline_source.payload_scope_signature` 对缺键显式回 `None`；签名函数本身只是
    把「范围」写成可比较的三元组。
    """
    assert scope_signature(None) == ("all", "all", ())
    assert scope_signature({}) == ("all", "all", ())
    assert scope_signature({"analysis_profile": "", "focus": {}, "repositories": []}) == (
        "all",
        "all",
        (),
    )


def test_scope_signature_keeps_the_effective_profile_and_sorts_repositories():
    """签名取 payload 里**生效的** profile（`tables` 回退后的 `all` 也照它写），
    仓库集去重排序 —— 同一份范围不能因为列表顺序不同被判成另一份。"""
    payload = _payload("tables", "table", (3, 1, 3, 2))
    assert scope_signature(payload) == ("tables", "table", (1, 2, 3))
    assert scope_signature(_payload("ALL", "Table", (2,))) == ("all", "table", (2,))
    # 缺 `focus` 键按 all，脏值（不是映射）也按 all
    assert scope_signature({"analysis_profile": "all"})[:2] == ("all", "all")
    assert scope_signature({"analysis_profile": "all", "focus": "table"})[:2] == ("all", "all")


def test_scope_signature_for_facts_matches_the_payload_form():
    """payload 还没组装出来时（`_concluded_run` 那一步）算的必须是**同一份**签名。

    两处口径一旦不同，补偿集会拿一个永远不相等的签名去比 —— 表现是「基线明明同范围，
    补偿却再也不补了」，而没有任何地方报错。
    """
    payload = _payload("tables", "code", (9, 7))
    assert scope_signature_for_facts("tables", "code", [7, 9]) == scope_signature(payload)


# ==========================================================================
# 二、读侧：范围不同 → 不继承（P1 的主断言）
# ==========================================================================


def test_switching_from_all_to_tables_does_not_inherit_the_code_repo_critical(ctx):
    """**P1 的原始形状**：`profile=all` 下代码仓库的 critical → 再以 `profile=tables` 跑。

    旧行为：`previous_run` 只按分组键取最近一条，critical 被 `reconcile_result` 原样
    继承，风险等级被抬到 high。现在：签名不同 → 这条运行连基线都不是，行也读不出来，
    对账拿到空表 → 风险等级只由本轮结论决定。
    """
    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    all_run = _run(
        project.id,
        key,
        when=now - timedelta(hours=1),
        payload=_payload("all", "all", (1, 2)),
        findings=(("fp-code-critical", "critical", "code/mod.lua"),),
    )
    tables_scope = scope_signature(_payload("tables", "all", (1,)))

    assert previous_run("weekly", key, scope_signature=tables_scope) is None, (
        "all 范围的那条运行被当成 tables 的基线了 —— P1 就是这个形状"
    )
    assert previous_anomaly_rows(all_run.id, scope_signature=tables_scope) == [], (
        "传进来的运行与本次范围不一致时，行照旧读得出来 —— 对账器会把 critical 继承进去"
    )
    assert baseline_findings("weekly", key, scope_signature=tables_scope) == [], (
        "范围不同的旧结论进了 baseline_findings —— 它们会以「已知问题」的身份回到输入里"
    )

    # 真实链路（`_run_engine_and_persist`）：previous 为 None、行为空、只剩一句说明。
    previous = previous_run("weekly", key, scope_signature=tables_scope)
    merged = reconcile_result(
        _result("low"),
        [],
        changed_paths={"code/mod.lua"},
        previous_run_id=None,
        scope_mismatch=True,
    )
    assert merged["anomalies"] == [], "范围变了还是继承了旧结论"
    assert merged["risk_level"] == "low", (
        "旧范围的 critical 把本报告的风险等级抬高了 —— 它根本不属于这次的范围"
    )
    assert "历史结论未继承" in merged["report_markdown"], "报告没说清历史为什么没进来"
    assert "仅作独立参考" in merged["report_markdown"]
    assert previous is None


def test_the_same_scope_still_inherits_normally(ctx):
    """**防过头**：profile / focus / 仓库集都一样时，继承规则逐字不变。

    这条同时钉住「critical 仍会抬等级」这个既有语义 —— 否则上面那条「等级没被抬高」
    可能只是因为它压根没参与定级（比如 risk_level 根本没被改写的路径）。
    """
    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    previous = _run(
        project.id,
        key,
        when=now - timedelta(hours=1),
        payload=_payload("tables", "table", (7,)),
        findings=(("fp-keep", "critical", "tables/reward.xlsx"),),
    )
    scope = scope_signature(_payload("tables", "table", (7,)))

    picked = previous_run("weekly", key, scope_signature=scope)
    assert picked is not None and picked.id == previous.id, "同范围的基线被范围签名挡住了"

    findings = baseline_findings("weekly", key, scope_signature=scope)
    assert [item.fingerprint for item in findings] == ["fp-keep"]

    rows = previous_anomaly_rows(previous.id, scope_signature=scope)
    merged = reconcile_result(
        _result("mid"), rows, changed_paths=set(), previous_run_id=previous.id
    )
    assert [row["fingerprint"] for row in merged["anomalies"]] == ["fp-keep"]
    assert merged["risk_level"] == "high", (
        "同范围的 critical 不再参与定级 —— 这条边界被修过头了（它只是不跨范围）"
    )


def test_a_nearer_incompatible_run_breaks_the_chain(ctx):
    """**链式判定的主断言**：紧挨着的那一条范围不同 → 本轮**一条旧结论都不继承**。

    审查报告的形状：A（tables）报了问题 X → B（all）改了那张表、X 修好了 →
    C（tables）只改了另一张表。C 开始分析时（C 自己还在 running，不是候选），候选序列
    是 B、A。第一版修法「签名不同就跳过、继续往回找」会跳过 B 取 A —— A 的 X 原样回到
    C 的报告里，**B 的修复被 A 的旧结论覆盖**。范围链断在 B 这里，说明「B 之后」没有
    一条可比的完整问题全集，宁可这轮不继承。
    """
    from services.ai.change_set import ChangeSet
    from services.ai.scope import AnalysisScope

    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    older_tables = _run(
        project.id,
        key,
        when=now - timedelta(hours=3),
        payload=_payload("tables", "all", (1,)),
        findings=(("fp-old-x", "critical", "tables/a.xlsx"),),
    )
    fixing_all = _run(
        project.id,
        key,
        when=now - timedelta(hours=2),
        payload=_payload("all", "all", (1, 2)),
        # B 的结论里 X 已经不在了（它改的就是那张表）—— 跳过 B 丢掉的正是这个事实。
        findings=(("fp-b-other", "low", "code/other.lua"),),
    )
    current = _run(
        project.id,
        key,
        when=now - timedelta(hours=1),
        payload=_payload("tables", "all", (1,)),
        status="running",
        structured=False,
    )
    scope = scope_signature(_payload("tables", "all", (1,)))

    assert previous_run("weekly", key, scope_signature=scope) is None, (
        f"基线退到了更早的 A（run {older_tables.id}）—— B（run {fixing_all.id}）的修复"
        "被 A 的旧结论覆盖了"
    )
    assert baseline_findings("weekly", key, scope_signature=scope) == [], (
        "A 的旧结论进了 baseline_findings —— 模型会把它当既成事实复述"
    )
    change = ChangeSet(summary="", scope=AnalysisScope(), paths=())
    assert baseline_digest("weekly", key, change, scope_signature=scope) == "", (
        "链断了摘要还在给 A 的旧结论"
    )
    # 对账输入也一样：从 B（不兼容的候选）或 C（还在跑的当前轮）入口都不能退到 A。
    assert previous_anomaly_rows(fixing_all.id, scope_signature=scope) == []
    assert previous_anomaly_rows(current.id, scope_signature=scope) == []

    merged = reconcile_result(
        _result("low"),
        [],
        changed_paths=set(),
        previous_run_id=None,
        scope_mismatch=True,
    )
    assert merged["anomalies"] == [], "旧范围的 A 还是被继承了"
    assert merged["risk_level"] == "low", "A 的 critical 参与了本报告的定级"


def test_consecutive_same_scope_runs_keep_inheriting(ctx):
    """**防过头**：A（tables）→ C（tables），中间没换范围 → 最近那条就是基线。

    链式判定不是「整组放弃」：同范围连续跑多少次，继承的都只是**最近一条**的行
    （不是把历次并起来）。
    """
    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    _run(
        project.id,
        key,
        when=now - timedelta(hours=2),
        payload=_payload("tables", "all", (1,)),
        findings=(("fp-a-old", "critical", "tables/a.xlsx"),),
    )
    newer = _run(
        project.id,
        key,
        when=now - timedelta(hours=1),
        payload=_payload("tables", "all", (1,)),
        findings=(("fp-c-now", "critical", "tables/c.xlsx"),),
    )
    scope = scope_signature(_payload("tables", "all", (1,)))

    picked = previous_run("weekly", key, scope_signature=scope)
    assert picked is not None and picked.id == newer.id, "同范围的最近一条不是基线"

    findings = baseline_findings("weekly", key, scope_signature=scope)
    assert [item.fingerprint for item in findings] == ["fp-c-now"], (
        "把历次结论并起来了（A 的旧条目被翻出来），或者整组放弃了"
    )
    rows = previous_anomaly_rows(newer.id, scope_signature=scope)
    assert [row["fingerprint"] for row in rows] == ["fp-c-now"]

    merged = reconcile_result(
        _result("mid"), rows, changed_paths=set(), previous_run_id=newer.id
    )
    assert [row["fingerprint"] for row in merged["anomalies"]] == ["fp-c-now"]
    assert merged["risk_level"] == "high", "同范围的 critical 不再参与定级（修过头了）"


def test_a_run_without_a_structured_conclusion_does_not_break_the_chain(ctx):
    """**链只对候选序列判**：中间那次没留下结构化结论（或失败）不影响继承。

    真实形状：A（tables，有结论）→ B（degraded，只有 markdown）→ F（failed）→
    当前轮 C（tables）。B / F 都**不是候选**（它们没留下「上一轮的问题全集」），
    越过它们不算换范围 —— 基线仍是 A。这也正是 `_concluded_run_before` 存在的理由：
    没有它的话，A 与 C 之间夹一次降级就把历史整段丢掉。

    摘要还要如实说一句「中间有分析没给出可比对的结论」（`skipped_unstructured_runs`），
    否则用户只会看到「基线是更早那次」而不知道为什么。
    """
    from services.ai.change_set import ChangeSet
    from services.ai.scope import AnalysisScope

    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    older = _run(
        project.id,
        key,
        when=now - timedelta(hours=4),
        payload=_payload("tables", "all", (1,)),
        findings=(("fp-a-keep", "high", "tables/a.xlsx"),),
    )
    _run(
        project.id,
        key,
        when=now - timedelta(hours=3),
        payload=_payload("tables", "all", (1,)),
        status="degraded",
        structured=False,
    )
    _run(
        project.id,
        key,
        when=now - timedelta(hours=2),
        payload=_payload("tables", "all", (1,)),
        status="failed",
        structured=False,
    )
    current = _run(
        project.id,
        key,
        when=now - timedelta(hours=1),
        payload=_payload("tables", "all", (1,)),
        status="running",
        structured=False,
    )
    scope = scope_signature(_payload("tables", "all", (1,)))

    picked = previous_run("weekly", key, scope_signature=scope)
    assert picked is not None and picked.id == older.id, (
        "中间夹着「没留下结构化结论」的运行就整段放弃了 —— 越过它们本来是该做的"
    )
    rows = previous_anomaly_rows(current.id, scope_signature=scope)
    assert [row["fingerprint"] for row in rows] == ["fp-a-keep"], (
        "回退链没越过降级 / 失败去取那条同范围的基线"
    )

    change = ChangeSet(summary="", scope=AnalysisScope(), paths=())
    digest = baseline_digest("weekly", key, change, scope_signature=scope)
    assert "fp-a-keep" in digest, digest
    assert "没有给出可比对的结论" in digest, (
        f"越过了一次没有结论的运行，却没在摘要里说明（用户不知道中间那次白跑了）：{digest!r}"
    )


def test_a_legacy_payload_in_the_middle_also_breaks_the_chain(ctx):
    """中间那次是**旧 payload**（判不了范围）→ 同样算链断，不退回更早那条。

    旧 payload 判不出范围，和本轮比永远是「不知道」。按「不知道就跳过」的旧写法会继续
    退到 A；而它同样可能是「改了那张表」的那一轮 —— 判不了就不继承（与模块抬头的取舍
    一致：宁可少带一次历史，不把另一范围的结论当本轮基线）。
    """
    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    older = _run(
        project.id,
        key,
        when=now - timedelta(hours=2),
        payload=_payload("tables", "all", (1,)),
        findings=(("fp-older", "high", "tables/a.xlsx"),),
    )
    legacy = _run(
        project.id,
        key,
        when=now - timedelta(hours=1),
        findings=(("fp-legacy", "critical", "code/legacy.lua"),),
    )
    scope = scope_signature(_payload("tables", "all", (1,)))

    assert previous_run("weekly", key, scope_signature=scope) is None, (
        f"判不了范围的中间那条被跳过、退到了更早的 run {older.id}"
    )
    assert previous_anomaly_rows(legacy.id, scope_signature=scope) == []
    assert baseline_findings("weekly", key, scope_signature=scope) == []


def test_the_fallback_chain_stops_at_the_first_incompatible_run(ctx):
    """`previous_anomaly_rows` 的回退链**不越过**范围不兼容的运行（A/B 两行就够）。

    A（tables，有行）→ B（all，有行）。从 B 这个入口读行时 B 不是可用基线，旧写法会
    继续退到 A —— A 的旧问题就这样被继承。现在 B 是**范围不兼容的候选** → 链断，回空表。

    D 那一臂再把「无结构化结论的运行可以越过」钉住：D（tables，只有 markdown）不是
    候选，越过它不算换范围；但越过之后紧挨着的 B 不兼容 → 仍然停在 B，不取 A。
    """
    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    older_tables = _run(
        project.id,
        key,
        when=now - timedelta(hours=3),
        payload=_payload("tables", "all", (1,)),
        findings=(("fp-a-old", "critical", "tables/a.xlsx"),),
    )
    fixing_all = _run(
        project.id,
        key,
        when=now - timedelta(hours=2),
        payload=_payload("all", "all", (1, 2)),
        findings=(("fp-b-other", "high", "code/other.lua"),),
    )
    markdown_only = _run(
        project.id,
        key,
        when=now - timedelta(hours=1),
        payload=_payload("tables", "all", (1,)),
        status="degraded",
        structured=False,
    )
    scope = scope_signature(_payload("tables", "all", (1,)))

    rows = previous_anomaly_rows(markdown_only.id, scope_signature=scope)
    assert rows == [], (
        f"回退链越过了 all 范围的 B（run {fixing_all.id}），取了更早 A 的行：{rows!r}"
    )
    rows = previous_anomaly_rows(fixing_all.id, scope_signature=scope)
    assert rows == [], (
        f"不兼容的 B 自己当不了基线，却退到了更早 A 的行：{rows!r}"
        f"（A 的 run id = {older_tables.id}）"
    )
    # 反向：不传签名时这条回退链照旧越过 D（没有结构化结论），取最近一条有结论的
    # 运行 B —— 老调用方的行为一字未变。
    assert [row["fingerprint"] for row in previous_anomaly_rows(markdown_only.id)] == [
        "fp-b-other"
    ]


def test_a_run_without_a_scope_payload_is_not_used_as_the_baseline(ctx):
    """**旧 payload 一律视为链断**（2026-11 之前的运行没有这两个键）。

    取舍：宁可这一轮少继承，也不把另一范围的结论当本轮基线。代价是第一轮看不到历史；
    但方向错了（把不在范围内的 critical 抬进报告）比少带一次历史贵得多。
    不给签名时行为与这条边界之前逐字相同（老调用方/老测试不受影响）。
    """
    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    legacy = _run(
        project.id,
        key,
        when=now - timedelta(hours=1),
        findings=(("fp-legacy", "critical", "code/legacy.lua"),),
    )
    scope = scope_signature(_payload("all", "all", (1,)))

    assert previous_run("weekly", key) is not None, "不传签名时行为变了（老调用方会被打断）"
    assert previous_run("weekly", key, scope_signature=scope) is None, (
        "判不了范围的老 payload 被当成了兼容基线 —— 默认值（all/all/空仓库）不是证据"
    )
    assert baseline_findings("weekly", key, scope_signature=scope) == []
    assert previous_anomaly_rows(legacy.id, scope_signature=scope) == []


# ==========================================================================
# 三、报告说明：范围变了要留一句（否则读者以为历史被逐条处理掉了）
# ==========================================================================


def test_the_digest_keeps_old_scope_findings_out_of_the_prompt(ctx):
    """范围不一致时旧结论**一个字都不进模型输入**（与用户点全量同一档）。

    对账不继承只是最后一层；如果摘要还把它们列成「已知问题」，模型会照抄一遍，
    风险等级照样被抬起来 —— 只是绕了一条路。
    """
    from services.ai.change_set import ChangeSet
    from services.ai.scope import AnalysisScope

    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    _run(
        project.id,
        key,
        when=now - timedelta(hours=1),
        payload=_payload("tables", "all", (1,)),
        findings=(("fp-keep", "high", "tables/a.xlsx"),),
    )
    change = ChangeSet(summary="", scope=AnalysisScope(), paths=())
    scope = scope_signature(_payload("tables", "all", (1,)))

    baseline = baseline_digest("weekly", key, change, scope_signature=scope)
    assert "fp-keep" in baseline, "前提不成立：这份历史本来会被注入（否则下面的「空」是假绿）"

    silent = baseline_digest(
        "weekly",
        key,
        change,
        baseline_account={"kind": "snapshot", "scope_mismatch": True},
        scope_signature=scope,
    )
    assert silent == "", f"范围变了摘要还在给旧结论：{silent!r}"


# ==========================================================================
# 四、写侧接线：换 profile 时 `payload["baseline"]["scope_mismatch"]` 要落下来
# ==========================================================================


def test_switching_profile_marks_the_baseline_scope_mismatch_on_the_payload(ctx):
    """走**真入口** `build_weekly_payload`：先 `all` 跑一轮，再切 `tables`。

    为什么要这一条：`_concluded_run` / `previous_run` 的签名判据各自单测绿了，而写侧
    **没把 `scope_mismatch` 写进 payload** 时，报告里不会出现那句说明 —— 判据对了、接线
    没换，报告静默，没有任何单测会红（本仓库已经踩过同型的坑）。
    """
    from services import ai_analysis_service as ai_service
    from tests.test_ai_scope_policy import _build_and_settle, _change, _make_batch, _paths

    group = _make_batch(code_files=2, table_files=2)
    project_id = group["project"].id
    config_id = group["cfg_by_kind"]["table"].id

    ok, message, errors = ai_service.update_project_analysis_config(
        project_id, {"analysis_profile": "all"}, updated_by="tester"
    )
    assert ok, f"{message} {errors}"
    db.session.commit()

    first = _build_and_settle(group, config_id)
    assert first["payload"]["analysis_profile"] == "all", "前提不成立：上一轮不是 all 范围"
    assert {item["repository_id"] for item in first["payload"]["repositories"]} == {
        group["cfg_by_kind"]["code"].repository_id,
        group["cfg_by_kind"]["table"].repository_id,
    }

    ok, message, errors = ai_service.update_project_analysis_config(
        project_id, {"analysis_profile": "tables"}, updated_by="tester"
    )
    assert ok, f"{message} {errors}"
    db.session.commit()
    _change(group, _paths(group))

    payload, _state, skip = ai_service.build_weekly_payload(config_id)
    assert skip is None, f"这一轮没建出 payload：{skip}"
    assert payload["analysis_profile"] == "tables"
    assert {item["repository_id"] for item in payload["repositories"]} == {
        group["cfg_by_kind"]["table"].repository_id
    }, "tables 的输入集没有收窄 —— 那范围其实没变，这条用例测不到东西"
    assert payload["baseline"]["scope_mismatch"] is True, (
        "指针那一轮与本次范围不同，payload 上没有留痕 —— 报告不会说明历史未继承"
    )

    scope = scope_signature(payload)
    assert (
        ai_service._previous_run("weekly", payload["group"]["key"], scope_signature=scope)
        is None
    ), "旧范围那一轮还是被当成了基线"


def test_a_run_without_a_scope_payload_still_feeds_the_unread_cursor(ctx):
    """`_concluded_run` 对**判不了范围**的老 payload 照旧给补偿集。

    为什么不与 `previous_run` 同一条严格口径：补偿集只决定「哪些文件进输入」，不会把
    结论继承进报告；而未读游标（`base_run_id`）正靠它接上。把老行一律判死会让升级之后
    的每一轮都从 streak=1 重新开始。
    """
    from services import ai_analysis_service as ai_service

    project = _project()
    key = _key()
    now = datetime.now(timezone.utc)
    legacy = _run(project.id, key, when=now - timedelta(hours=1))

    class _State:
        last_concluded_run_id = legacy.id

    scope = scope_signature(_payload("all", "all", (1,)))
    assert ai_service._concluded_run(_State(), scope) is not None, (
        "老 payload 被判成「范围不兼容」—— 补偿集与未读游标会一起断掉"
    )
    assert ai_service._concluded_run(_State(), None) is not None
