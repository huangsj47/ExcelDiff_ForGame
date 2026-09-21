# -*- coding: utf-8 -*-
"""降级运行（`status="degraded"`）在**读侧**的十处接线。

## 背景：写入侧已经原生区分了三种终态

`_persist_outcome` 现在把引擎的 succeeded / degraded / failed **逐字**落成同名 status
（此前 degraded 被写进 `"succeeded"`）。库里的现实是：13 条完成运行里 **12 条**是降级 ——
也就是说读侧原来「只认 succeeded」的每一处，实际都在过滤掉绝大多数真实结论。

## 统一语义

`run.status` 回答的是「这次**交付**是什么形态」，**不是**「模型读全了没有」：

* `succeeded` / `degraded` —— **都有结论**（降级是「有结论但浅」：有报告正文、有结构化
  结论形态）。这两者在本文件覆盖的每一条读路径上必须**同等对待**：可展示、可回放、
  可导出、可当基线、可在用量面板里筛出来。
* `failed` —— **没有结论**。它在这条路径上仍然必须被拒：本文件每一条都配了反向对照，
  防止把改动做成「一律放行」式的假绿。

「模型读全了没有」只能看引擎状态（`outcome.status` / `result["status"]`），**不许**从
`run.status` 反推 —— 那件事由 `tests/test_ai_run_lifecycle_degraded_status.py` 从写侧守。

## 为什么用真库真行

这些判定全是 SQL 条件（`.in_(...)`）与几个纯函数，用桩替换掉就等于把要验的东西换掉了。
测试库是**会话级共用**的（没有逐用例重置），所以每条用例自己造行、自己清，且用 uuid
生成互不相撞的 target_key / commit_id。
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import services.ai.provenance as provenance
import services.ai_analysis_service as ai_service
import services.ai_report_history_service as history
from app import app as flask_app
from app import create_tables, db
from models import Commit
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from services import ai_usage_service as usage
from services.ai import baseline_source, run_cache_source
from services.ai import report_document as doc
from tests.test_ai_history_survives_restart import REPORT_TEXT, _setup_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 有结论的两种交付形态。它们在每一条读路径上必须同进同出。
CONCLUDED = ("succeeded", "degraded")


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with flask_app.app_context():
        create_tables()


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _run(
    *,
    project_id: int,
    status: str,
    target_type: str = "weekly",
    target_id: int | None = None,
    target_key: str | None = None,
    response_text: str = REPORT_TEXT,
    response_payload: str | None = '{"risk_level": "high", "anomalies": []}',
    conclusion_structured: bool | None = True,
    error_message: str | None = None,
    created_at: datetime | None = None,
    provenance_ok: bool = True,
) -> AiAnalysisRun:
    """造一条运行。默认除 status 以外**完全够格**被当结论（溯源一致、有正文、未过期）。"""
    now = created_at or datetime.now(timezone.utc)
    fingerprint = provenance.current_provenance(project_id)
    if not provenance_ok:
        fingerprint["prompt_version"] = "prompt-变了"
    run = AiAnalysisRun(
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target_key=target_key,
        status=status,
        scope="full",
        trigger_source="manual",
        started_at=now,
        finished_at=now + timedelta(seconds=30),
        created_at=now,
        response_text=response_text,
        response_payload=response_payload,
        conclusion_structured=conclusion_structured,
        error_message=error_message,
        **fingerprint,
    )
    db.session.add(run)
    db.session.flush()
    return run


def _anomaly(run: AiAnalysisRun, marker: str) -> None:
    db.session.add(AiAnalysisAnomaly(
        run_id=run.id, project_id=run.project_id, fingerprint=f"fp-{marker}",
        title=f"【配表】{marker} 的问题", severity="high", category="config_value",
        file_path="src/a.lua",
    ))


def _cleanup(*runs: AiAnalysisRun) -> None:
    for run in runs:
        AiAnalysisAnomaly.query.filter_by(run_id=run.id).delete()
        AiAnalysisRun.query.filter_by(id=run.id).delete()
    db.session.commit()


def _commit(repo_id: int) -> Commit:
    commit = Commit(
        repository_id=repo_id, commit_id=_uid("c")[:10], path="config/a.xlsx",
        message="fix", author="tester", commit_time=datetime(2026, 3, 2),
    )
    db.session.add(commit)
    db.session.flush()
    return commit


def _sse_events(text: str) -> list:
    """SSE 文本 → `[(event, payload), …]`。`data` 是 JSON，必须解析后再搜中文。"""
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name, payload = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[len("data:"):].strip())
        events.append((name, payload))
    return events


# ==========================================================================
#  一、缓存复用与 SSE 回放（`services/ai/run_cache_source.py:57`）
# ==========================================================================


def test_a_degraded_run_is_reusable_as_a_cached_result():
    """**再点一次不该重新花钱。**

    `_is_run_fresh` 是 `/latest` 第 1 步、`stream_*` 的缓存命中、以及「跳过重跑」三处
    共用的判据。它原来是 `run.status != "succeeded": return False` —— 降级运行一律不是
    「现成的」，于是用户点第二次会再跑一整轮模型调用。
    """
    with flask_app.app_context():
        create_tables()
        project, _repo, _cfg = _setup_config()
        degraded = _run(project_id=project.id, status="degraded")
        failed = _run(
            project_id=project.id, status="failed",
            response_payload=None, response_text="调用模型失败：Read timed out.",
            conclusion_structured=None, error_message="上游断了",
        )
        try:
            assert run_cache_source._is_run_fresh(degraded) is True, (
                "降级运行不算「现成的结论」—— 用户再点一次分析会重新花钱"
            )
            # 反向对照：失败运行**没有结论**，永远不许当缓存回放（它的 response_text
            # 是错误文本，回放出来就是「上次的失败」）。
            assert run_cache_source._is_run_fresh(failed) is False, (
                "失败的运行被判成可复用 —— 会被当成缓存直接回放"
            )
        finally:
            _cleanup(degraded, failed)


def test_a_degraded_run_replays_through_the_sse_entry():
    """回放本身：`stream_commit_analysis` 命中缓存后**不再发起任何模型调用**。

    判据是第一个事件就是 `cached`（`_stream_cached_run` 的签名形态），而不是
    「Project API key not configured.」—— 后者说明它走到了真正开跑那一段，
    也就是**要花钱的那一段**。
    """
    with flask_app.app_context():
        create_tables()
        project, repo, _cfg = _setup_config()
        cached_commit = _commit(repo.id)
        other_commit = _commit(repo.id)
        degraded = _run(
            project_id=project.id, status="degraded",
            target_type="commit", target_id=cached_commit.id,
        )
        failed = _run(
            project_id=project.id, status="failed",
            target_type="commit", target_id=other_commit.id,
            response_payload=None, response_text="调用模型失败", conclusion_structured=None,
            error_message="上游断了",
        )
        try:
            events = _sse_events("".join(ai_service.stream_commit_analysis(cached_commit.id)))
            assert events[0][0] == "cached", (
                f"降级结论没有被回放 —— 用户点一次就再花一次钱：{events[:2]}"
            )
            body = "".join(payload.get("text", "") for name, payload in events if name == "chunk")
            assert "变更理解" in body, "回放出来的不是那份报告正文"

            # 反向对照：失败的 run 不构成缓存命中，这条路会走到真正开跑那一段
            # （没有配 key 时就是那句 error）。
            failed_events = _sse_events("".join(ai_service.stream_commit_analysis(other_commit.id)))
            assert failed_events[0][0] != "cached", (
                f"失败的运行被当成缓存回放了：{failed_events[:2]}"
            )
        finally:
            AiAnalysisRun.query.filter_by(target_type="commit").filter(
                AiAnalysisRun.id.in_([degraded.id, failed.id])
            ).delete(synchronize_session=False)
            Commit.query.filter(Commit.id.in_([cached_commit.id, other_commit.id])).delete(
                synchronize_session=False
            )
            db.session.commit()


# ==========================================================================
#  二、`/latest`：降级读得到，而且**不许**被说成「旧版评审规程产出的」
# ==========================================================================


def test_the_latest_endpoint_reads_a_degraded_run():
    """降级运行必须能从 `/latest` 读到，且**不带 `stale`**。

    这一条同时钉住「不许只改一半」：如果只放宽 `_latest_concluded_run` 而不放宽
    `_is_run_fresh`，第 1 步会判不可用、第 3 步又命中**同一条**记录，
    于是 `concluded.id == run.id` 成立 → `stale_reason="rules_changed"` →
    界面对用户说「该结论由旧版评审规程产出（提示词/规则/模型已更新）」。
    那是一句**假话**：提示词一个字都没改，变的是我们把 degraded 写进了 status 列。
    """
    with flask_app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id, status="degraded",
            target_key=ai_service.build_weekly_group_key(cfg), target_id=cfg.id,
        )
        try:
            payload = ai_service.get_latest_weekly_result(cfg.id)
            assert payload is not None, "降级运行从 `/latest` 上消失了（界面会去自动重跑一次）"
            assert payload["run_id"] == run.id, payload
            assert payload["status"] == "degraded", payload
            assert payload.get("stale_reason") != ai_service.STALE_REASON_RULES_CHANGED, (
                "对自己这条新鲜结论说「由旧版评审规程产出」—— 提示词一个字都没改过"
            )
            assert not payload.get("stale"), f"新鲜的降级结论被判成了旧的：{payload}"
            # 「已报过的问题」与「结论」用的是同一批输入，基线这一半也要有它
            assert ai_service._previous_run("weekly", run.target_key) is not None
        finally:
            _cleanup(run)


def test_a_failed_run_is_still_never_a_conclusion():
    """反向对照：失败**没有结论**，`/latest` 给的是「上次失败」那个形态。

    不许把「放宽到 degraded」做成「凡是跑过的都算结论」。
    """
    with flask_app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id, status="failed",
            target_key=ai_service.build_weekly_group_key(cfg), target_id=cfg.id,
            response_text="", response_payload=None, conclusion_structured=None,
            error_message="额度用完了",
        )
        try:
            payload = ai_service.get_latest_weekly_result(cfg.id)
            assert payload is not None, payload
            assert payload["status"] == "failed", payload
            assert not (payload.get("response_text") or "").strip(), (
                "失败的运行被当成了「有结论」"
            )
            assert "额度用完了" in payload.get("error_message", ""), payload
            assert ai_service._previous_run("weekly", run.target_key) is None, (
                "失败的运行被当成了基线"
            )
        finally:
            _cleanup(run)


def test_a_degraded_run_with_changed_provenance_is_honestly_called_stale():
    """溯源**真的**变了时，那句「旧版评审规程」照旧要说出来（别把上一条修过头）。"""
    with flask_app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id, status="degraded",
            target_key=ai_service.build_weekly_group_key(cfg), target_id=cfg.id,
            provenance_ok=False,
        )
        try:
            payload = ai_service.get_latest_weekly_result(cfg.id)
            assert payload is not None, payload
            assert payload.get("stale_reason") == ai_service.STALE_REASON_RULES_CHANGED, payload
        finally:
            _cleanup(run)


# ==========================================================================
#  三、基线（`services/ai/baseline_source.py:59, 81`）
# ==========================================================================


def test_a_degraded_run_is_used_as_the_baseline():
    """有结构化结论的降级运行**就是基线**。

    写入侧刚把 `conclusion_structured` 从「status 是不是 succeeded」改成「有没有结构化
    payload」，`tests/test_ai_baseline_needs_structured_conclusion.py` 的 docstring 写的
    正是「有 payload 的降级**应当**能当基线」。读侧这条 `.filter(status == "succeeded")`
    不改，两者就自相矛盾：下一轮读到的基线是「共 0 条」，上一批已知问题全部被当新发现重报。
    """
    with flask_app.app_context():
        create_tables()
        project, _repo, _cfg = _setup_config()
        key = f"g-{uuid.uuid4().hex[:8]}"
        older = _run(project_id=project.id, status="succeeded", target_key=key)
        newer = _run(
            project_id=project.id, status="degraded", target_key=key,
            created_at=older.created_at + timedelta(hours=1),
        )
        _anomaly(newer, "DEGRADED_BUT_OK")
        db.session.commit()
        try:
            picked = baseline_source.previous_run("weekly", key)
            assert picked is not None and picked.id == newer.id, (
                "有结构化结论的降级运行被基线跳过了 —— 与写入侧放宽的 conclusion_structured 矛盾"
            )
            titles = [item.title for item in baseline_source.baseline_findings("weekly", key)]
            assert "【配表】DEGRADED_BUT_OK 的问题" in titles, titles
            assert baseline_source.skipped_unstructured_runs("weekly", key, picked) == 0, (
                "降级运行被记成了「中间有一次没给出可比对的结论」"
            )
        finally:
            _cleanup(older, newer)


def test_a_degraded_run_without_a_payload_is_still_not_a_baseline():
    """反向对照：只有 markdown 的那种降级（`conclusion_structured=False`）**仍然不能**
    当基线 —— 它的「问题全集」是空的，拿它当基线等于把上一批已知问题全部当新发现重报。

    同时：那条被跳过的运行要在摘要里**说得出来**（`skipped_unstructured_runs` 也得认
    degraded，否则「中间有一次分析没给出可比对的结论」这句话对降级运行永远是静默的）。
    """
    with flask_app.app_context():
        create_tables()
        project, _repo, _cfg = _setup_config()
        key = f"g-{uuid.uuid4().hex[:8]}"
        older = _run(project_id=project.id, status="succeeded", target_key=key)
        markdown_only = _run(
            project_id=project.id, status="degraded", target_key=key,
            conclusion_structured=False, response_payload=None,
            created_at=older.created_at + timedelta(hours=1),
        )
        failed = _run(
            project_id=project.id, status="failed", target_key=key,
            conclusion_structured=None, response_text="", response_payload=None,
            created_at=older.created_at + timedelta(hours=2),
        )
        try:
            picked = baseline_source.previous_run("weekly", key)
            assert picked is not None and picked.id == older.id, (
                "拿不准的降级（只有 markdown）或失败运行被当成了基线"
            )
            assert baseline_source.skipped_unstructured_runs("weekly", key, picked) == 1, (
                "中间那次没给出可比对结论的降级运行没有被计入 —— 摘要里就不会说这句话"
            )
        finally:
            _cleanup(older, markdown_only, failed)


# ==========================================================================
#  四、历次结论（`services/ai_report_history_service.py:210`）
# ==========================================================================


def test_a_degraded_run_shows_up_in_the_history_list():
    """降级运行不许从「历次结论」整条消失 —— 它有报告正文，用户要能翻到。"""
    with flask_app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        key = ai_service.build_weekly_group_key(cfg)
        run = _run(project_id=project.id, status="degraded", target_key=key, target_id=cfg.id)
        try:
            payload = history.list_target_runs(kind="weekly", target_key=key)
            rows = {row["run_id"]: row for row in payload["runs"]}
            assert run.id in rows, "降级运行从「历次结论」里消失了"
            assert rows[run.id]["status"] == "degraded", rows[run.id]
            assert rows[run.id]["status_label"] == "降级完成", rows[run.id]
            assert rows[run.id]["exportable"] is True, "降级那次的导出按钮没打开（它有报告正文）"
        finally:
            _cleanup(run)


def test_the_history_list_still_hides_the_intermediate_states():
    """反向对照：中间态不进列表；失败进列表但**不可导出**。"""
    with flask_app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        key = ai_service.build_weekly_group_key(cfg)
        running = _run(
            project_id=project.id, status="running", target_key=key, target_id=cfg.id,
            response_text="", response_payload=None,
        )
        failed = _run(
            project_id=project.id, status="failed", target_key=key, target_id=cfg.id,
            response_payload=None, conclusion_structured=None, error_message="额度用完了",
        )
        try:
            payload = history.list_target_runs(kind="weekly", target_key=key)
            rows = {row["run_id"]: row for row in payload["runs"]}
            assert running.id not in rows, "中间态进了「历次结论」"
            assert failed.id in rows, "失败运行要在「为什么失败」那一档里翻得到"
            assert rows[failed.id]["exportable"] is False, "失败运行被判成可导出"
            assert payload["in_progress"] is True, "有一条正在跑，界面要说得出"
        finally:
            _cleanup(running, failed)


# ==========================================================================
#  五、导出（`services/ai/report_document.py` 的 EXPORTABLE_STATUSES）
# ==========================================================================


def test_a_degraded_run_is_exportable_but_a_failed_one_is_not():
    """降级有真实分析，今天它们存成 succeeded 本来就是可导出的；改成原生 degraded
    之后反而导出不了是**倒退**。失败没有正文，仍然不许导出。"""
    assert doc.is_exportable(status="degraded", report_text="# 变更理解\n...") is True
    for status in ("failed", "running", "pending", ""):
        assert doc.is_exportable(status=status, report_text="# 半份") is False, status
    # 有状态没正文照样不行（第二条判据与 status 无关，一并确认没被放宽）
    assert doc.is_exportable(status="degraded", report_text="") is False


def test_the_export_endpoint_downloads_a_degraded_report():
    """端到端：降级那一份能下载下来，失败那一份换回一句 JSON 而不是半个文件。"""
    with flask_app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        key = ai_service.build_weekly_group_key(cfg)
        degraded = _run(project_id=project.id, status="degraded", target_key=key, target_id=cfg.id)
        failed = _run(
            project_id=project.id, status="failed", target_key=key, target_id=cfg.id,
            response_text="", response_payload=None, conclusion_structured=None,
            error_message="额度用完了",
        )
        try:
            with flask_app.test_client() as client:
                _login(client)
                ok = client.get(f"/ai-analysis/runs/{degraded.id}/report.md")
                bad = client.get(f"/ai-analysis/runs/{failed.id}/report.md")

            assert ok.status_code == 200, ok.get_data(as_text=True)
            assert "变更理解" in ok.get_data(as_text=True)
            assert bad.status_code == 409, (
                f"失败运行也给了文件（下载成功但内容残缺比报错更危险）：{bad.status_code}"
            )
        finally:
            _cleanup(degraded, failed)


def _login(client) -> None:
    with client.session_transaction() as session:
        session["is_admin"] = True
        session["admin_user"] = "degraded-readside"
        session["_csrf_token"] = _uid("csrf")


# ==========================================================================
#  六、用量面板（`services/ai_usage_service.py:122-123` + 模板那一档）
# ==========================================================================


def test_the_usage_panel_offers_a_degraded_filter():
    """面板要能按「降级」筛。`RUN_STATUSES` 已含 degraded，筛选口径得跟上。"""
    assert "degraded" in usage.STATUS_CHOICES, usage.STATUS_CHOICES
    assert usage.STATUS_LABELS["degraded"] == "降级", usage.STATUS_LABELS
    assert usage.parse_usage_filters({"status": "degraded"}).status == "degraded", (
        "地址栏里写了 degraded 却被当成未知值回落成「全部状态」"
    )
    options = [item["value"] for item in usage.filter_options()["statuses"]]
    assert "degraded" in options, options
    # 认不出来的值照旧回落「全部状态」并留一句话（别把这条回归掉）
    unknown = usage.parse_usage_filters({"status": "成功"})
    assert unknown.status == usage.STATUS_ALL and unknown.notes, unknown


def test_the_status_filter_picks_out_degraded_runs_and_not_failed_ones():
    with flask_app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        key = ai_service.build_weekly_group_key(cfg)
        degraded = _run(project_id=project.id, status="degraded", target_key=key, target_id=cfg.id)
        failed = _run(
            project_id=project.id, status="failed", target_key=key, target_id=cfg.id,
            response_payload=None, conclusion_structured=None, error_message="额度用完了",
        )
        try:
            filters = usage.UsageFilters(status="degraded")
            assert usage.run_matches(degraded, filters, (None, None)) is True, "降级运行筛不出来"
            assert usage.run_matches(failed, filters, (None, None)) is False, (
                "「降级」这一档把失败也算了进来"
            )
        finally:
            _cleanup(degraded, failed)


_STATUS_TEXT_CASES = ("succeeded", "degraded", "failed", "running", "weird")


def test_the_dashboard_badge_says_degraded_in_chinese():
    """徽章上不许露出英文码 `degraded`（那一格是给人看的）。"""
    values = dict(zip(_STATUS_TEXT_CASES, _aiu_run_status_text()))

    assert values["degraded"] == "降级完成", values
    assert values["succeeded"] == "成功" and values["failed"] == "失败", values
    # 认不出来的状态照旧原样带出来（不编一个中文名）
    assert values["weird"] == "weird", values


def _aiu_run_status_text() -> list:
    """把面板模板里那个状态文案函数取出来，用 node 真跑一遍。

    抄一份进测试就失去意义了（改了模板测试还是绿的），所以按仓库既有做法
    （`tests/test_ai_drawer_stream_state.py::_function_source`）从模板源码里取。
    """
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真跑")
    source = (PROJECT_ROOT / "templates" / "ai_usage_dashboard.html").read_text(encoding="utf-8")
    body = _function_source(source, "aiuRunStatusText")
    driver = (
        "var fn = " + body + ";\n"
        "process.stdout.write(JSON.stringify("
        + json.dumps(list(_STATUS_TEXT_CASES)) + ".map(fn)));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "status_text.js"
        path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _function_source(source: str, name: str) -> str:
    """`function <name>(…) { … }` 的完整源码（按花括号配平）。"""
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth, index = 0, brace
    while index < len(source):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start: index + 1]
        index += 1
    raise AssertionError(f"找不到 {name} 的函数体")


# ==========================================================================
#  七、前端：抽屉的状态机与历次结论的徽章颜色档
# ==========================================================================

STREAM_MODULE = "static/js/ai_stream_status.js"
HISTORY_MODULE = "static/js/ai_report_history.js"

_STATUS_WORLD = ["succeeded", "degraded", "failed", "running", "pending", ""]

_STREAM_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const sandbox = { console: console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox, { filename: 'ai_stream_status.js' });
const S = sandbox.AiStreamStatus;

function fakeFetch(frames) {
    const queue = frames.slice();
    return function () {
        const frame = queue.length > 1 ? queue.shift() : queue[0];
        return Promise.resolve({ json: function () { return Promise.resolve(frame); } });
    };
}

// 一次轮询的完整生命周期：第一帧 → 逐帧驱动 → `stop()`。
// 每一帧之后都记下**那一行字现在是什么**，这样「跑完了还留着分析中」会直接落进断言。
async function runWatch(spec) {
    const el = { textContent: '初始文案' };
    const ticks = [];
    const cleared = [];
    const finished = [];
    const steps = [];
    const watch = S.watchRun(spec.runId, {
        metaEl: el,
        onFinished: function (status) { finished.push(status); },
        fetchImpl: fakeFetch(spec.frames),
        setInterval: function (fn) { ticks.push(fn); return ticks.length; },
        clearInterval: function (id) { cleared.push(id); }
    });
    await watch.first;
    steps.push(el.textContent);
    for (let index = 0; index < ticks.length; index += 1) {
        ticks[index]();
        await new Promise(function (resolve) { setTimeout(resolve, 0); });
        steps.push(el.textContent);
    }
    watch.stop();
    return {steps: steps, afterStop: el.textContent, finished: finished, cleared: cleared.length};
}

(async function () {
    const statuses = JSON.parse(process.argv[4]);
    const out = {
        statuses: statuses,
        isTerminal: statuses.map(S.isTerminal),
        isRunning: statuses.map(S.isRunning),
        // 连接层断了之后回头问到的状态：degraded 是**终态**
        interrupt: statuses.map(function (s) { return S.interruptOutcome(s, true); }),
        progressDegraded: S.progressText({round: 2, max_rounds: 8, live_tokens: 100}, 'degraded'),
        watches: []
    };
    for (const spec of JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))) {
        out.watches.push(await runWatch(spec));
    }
    process.stdout.write(JSON.stringify(out));
})().catch(function (err) {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
});
"""

_RUNNING_FRAME = {"success": True, "status": "running",
                  "progress": {"round": 2, "max_rounds": 8, "live_tokens": 92329}}


def _run_stream_module() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的状态机断言")
    watches = [
        # 降级运行：轮询自己发现它已经结束 —— 必须停表、收字、发 onFinished。
        # 不停表的表现就是「抽屉对降级运行永远转圈、onFinished 不触发」。
        {"runId": 7, "frames": [_RUNNING_FRAME, {"success": True, "status": "degraded"}]},
        # 反向对照：还在跑就**不许**收摊。
        {"runId": 7, "frames": [_RUNNING_FRAME]},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "watch.js"
        frames = Path(tmp) / "frames.json"
        path.write_text(_STREAM_DRIVER, encoding="utf-8")
        frames.write_text(json.dumps(watches), encoding="utf-8")
        proc = subprocess.run(
            ["node", str(path), str(PROJECT_ROOT / STREAM_MODULE), str(frames),
             json.dumps(_STATUS_WORLD)],
            capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_a_degraded_run_is_a_terminal_state():
    """**最严重的一处**：`TERMINAL` 里没有 degraded，于是 `watchRun` 读不到终态 ——
    抽屉对降级运行永远转圈，`onFinished` 永远不触发（实测库里 13 条完成运行 **12 条**
    是 degraded，也就是说绝大多数运行都停在这一幕上）。"""
    result = _run_stream_module()
    world = result["statuses"]
    terminal = dict(zip(world, result["isTerminal"]))
    running = dict(zip(world, result["isRunning"]))

    assert terminal["degraded"] is True, "degraded 不是终态 —— 抽屉会一直转圈"
    for status in CONCLUDED + ("failed",):
        assert terminal[status] is True, status
    for status in ("running", "pending", ""):
        assert terminal[status] is False, status
    # 降级**不是**「还在跑」：它跑完了（只是浅）。
    assert running["degraded"] is False, "降级被判成「还在跑」"


def test_a_degraded_run_stops_the_watch_and_settles_the_drawer():
    result = _run_stream_module()
    watch, still_running = result["watches"]

    assert watch["finished"] == ["degraded"], (
        f"轮询没把降级识别成终态（`onFinished` 不触发，抽屉永远不收摊）：{watch}"
    )
    assert watch["cleared"] >= 1, watch
    assert not watch["steps"][-1] and "分析中" not in watch["afterStop"], watch
    # 第一帧照旧写着「分析中」（这不是「什么都不显示」的假绿）
    assert "分析中" in watch["steps"][0], watch
    # 反向对照：还在跑的时候不许收摊收字（那会让用户以为这次已经结束了）。
    assert still_running["finished"] == [], still_running
    assert "分析中" in still_running["steps"][-1], still_running


def test_a_dropped_connection_on_a_degraded_run_reads_the_result_back():
    """断线之后回头问到的状态是 degraded：**那是终态** —— 该去取落库的结论，
    不许报成「读不到状态」继续干等（`interruptOutcome` 那条兜底分支原来的落点）。"""
    result = _run_stream_module()
    world = result["statuses"]
    outcomes = dict(zip(world, result["interrupt"]))
    degraded = outcomes["degraded"]

    assert degraded["refresh"] is True, (
        f"降级运行被当成「状态读不到」—— 界面会一直等一个已经结束的运行：{degraded}"
    )
    assert degraded["keepPolling"] is False, degraded
    assert degraded["badge"] != "失败", "降级不是一个失败"
    # 与 succeeded 走**同一个**分支（两者都是「有结论」）
    assert degraded == outcomes["succeeded"], (degraded, outcomes["succeeded"])
    # 到了终态，那行进度字必须让位
    assert result["progressDegraded"] is None, result["progressDegraded"]


_HISTORY_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const sandbox = { console: console };
sandbox.window = sandbox;
sandbox.document = { getElementById: function () { return null; } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox, { filename: 'ai_report_history.js' });
const api = sandbox.AiReportHistory;
const statuses = JSON.parse(process.argv[3]);
process.stdout.write(JSON.stringify({
    tones: statuses.map(api.statusTone),
    concluded: statuses.map(api.hasConclusion)
}));
"""


def _run_history_module(statuses: list) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真跑")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "history.js"
        path.write_text(_HISTORY_DRIVER, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(path), str(PROJECT_ROOT / HISTORY_MODULE), json.dumps(statuses)],
            capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_the_history_badge_tones_degraded_as_a_warning():
    """降级既不是成功也不是失败 —— 它那一档要看得出来（warning）。"""
    world = ["succeeded", "degraded", "failed", "running"]
    result = _run_history_module(world)
    tones = dict(zip(world, result["tones"]))

    assert tones["degraded"] == "warning", tones
    assert tones["succeeded"] == "success" and tones["failed"] == "danger", tones
    assert tones["running"] == "secondary", tones


def test_the_history_viewer_knows_a_degraded_run_has_a_conclusion():
    """「历次结论」里点开一条降级运行：**它那份报告要渲染出来**。

    判据原来是「不是 succeeded 就算没有结论」。`DEGRADE_MARKDOWN` 那种降级
    （模型没按协议给 JSON）的 `result` 恰好就是 `null`，而它的 `response_text` 是一份
    完整的 markdown 报告 —— 按旧判据会被显示成「这一次还没有结论。」，与列表里那一行
    摘要（正是从 `response_text` 里取的）自相矛盾。
    """
    world = ["succeeded", "degraded", "failed", "running", "pending", ""]
    concluded = dict(zip(world, _run_history_module(world)["concluded"]))

    assert concluded["degraded"] is True, "降级被判成「没有结论」—— 报告正文不渲染"
    for status in CONCLUDED:
        assert concluded[status] is True, status
    # 反向对照：这三个是真的没有结论（各自的形态由服务端给：失败给原因、跑动中给身份）。
    for status in ("failed", "running", "pending", ""):
        assert concluded[status] is False, status


# ==========================================================================
#  八、静态守卫：写入侧放宽了，读侧还有没有别处只看 succeeded
# ==========================================================================

READ_SIDE_FILES = (
    "services/ai/run_cache_source.py",
    "services/ai/baseline_source.py",
    "services/ai_report_history_service.py",
    "services/ai/report_document.py",
    "services/ai_analysis_service.py",
)


def _succeeded_only_guards(source: str) -> list:
    """源码里所有「只看 `status == succeeded`」的判据 → `[(行号, 那一行), …]`。

    **走语法树而不是文本匹配**，因为文本匹配在这种题上两头都会错：

    * 本仓库的注释/docstring 里**原样引用着被禁掉的写法**（`_is_run_fresh` 的抬头就写着
      `_previous_run() 一直只取 status == "succeeded"`），不剥注释会假红；而手写剥注释
      又会漏掉 docstring（它们不是 `#` 注释）；
    * 反过来，把字符串整段抹掉又会把**真的判据**（`!= "succeeded"` 里的那个字面量）
      一起抹掉 —— 于是断言恒真，成了假绿。

    看 `ast.Compare` / `.in_(...)` 就没有这两个问题：注释与 docstring 根本不在树里。
    """
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            if not any(_is_status_ref(item) for item in operands):
                continue
            others = [item for item in operands if not _is_status_ref(item)]
            if any(_mentions(item, "succeeded") for item in others) and not any(
                _mentions(item, "degraded") for item in others
            ):
                offenders.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("in_", "notin_")
            and _is_status_ref(node.func.value)
        ):
            for item in node.args:
                if _mentions(item, "succeeded") and not _mentions(item, "degraded"):
                    offenders.append(node.lineno)
    return offenders


def _is_status_ref(node) -> bool:
    """`run.status` / `AiAnalysisRun.status` / 光秃秃的 `status`。"""
    if isinstance(node, ast.Attribute):
        return node.attr == "status"
    return isinstance(node, ast.Name) and node.id == "status"


def _mentions(node, value: str) -> bool:
    """这一棵子树里有没有那个字符串字面量（含元组 / 列表 / 关键字参数里的）。"""
    return any(
        isinstance(item, ast.Constant) and item.value == value for item in ast.walk(node)
    )


def test_no_read_side_file_still_asks_for_succeeded_alone():
    """**只认 succeeded 的判据在读侧已经一处不剩。**

    这一条守的是「漏改一处」：十处接线散在五个文件里，而漏掉任何一处的症状都不同
    （抽屉转圈 / 不再回放 / 假 stale / 基线清空 / 历史消失 / 导出倒退 / 筛不出来），
    没有一条会自己报错。
    """
    offenders = []
    for name in READ_SIDE_FILES:
        source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        lines = source.splitlines()
        for number in _succeeded_only_guards(source):
            line = lines[number - 1].strip() if number <= len(lines) else ""
            offenders.append(f"{name}:{number} {line}")
    assert not offenders, "读侧还在只认 succeeded：\n" + "\n".join(offenders)
