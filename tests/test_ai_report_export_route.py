# -*- coding: utf-8 -*-
"""`GET /ai-analysis/runs/<id>/report.md`：**把这一次运行的结论交给浏览器下载**。

这个端点只做三件事：判权、取元信息、把文件发出去。三件都容易做错，而且错法都很安静：

* **判权按这条运行自己的 project_id**（与 `/runs/<id>/usage` 同一条口径），不接受调用方
  用参数指定项目 —— 报告正文里是模型看过的变更细节，换个号就能拿到别人的是不行的；
* **没有结论时回 JSON，不回半个文件**：一个「下载成功但内容是空的」文件会被当成一份
  「分析过、没问题」的报告存档，比一句错误提示危险得多；
* **文件名**：中文要原样到浏览器（RFC 5987），扩展名要是 `.md`。

用真登录 + 真路由（`_has_project_access` 要过），403 那条用 monkeypatch ——
与 `tests/test_ai_usage_capture.py` 同一套办法（那一条要在**没有项目权限**的会话下才走
得到，而造一个「已登录但没这个项目」的会话比换掉判定函数贵得多）。
"""
from __future__ import annotations

import json
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import routes.ai_analysis_routes as ai_routes
import services.ai.provenance as provenance
import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Commit, Project, Repository, WeeklyVersionConfig
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from services.ai import report_document as doc
from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV, project_pack_slug
from tests.test_ai_history_survives_restart import REPORT_TEXT, _setup_config


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _login(client, *, admin: bool = True):
    with client.session_transaction() as session:
        session["is_admin"] = admin
        session["admin_user"] = "export-contract"
        session["_csrf_token"] = _uid("csrf")


def _run(
    *,
    project_id: int,
    status: str = "succeeded",
    response_text: str = REPORT_TEXT,
    payload: dict | None = None,
    target_type: str = "weekly",
    target_id: int | None = None,
    target_key: str | None = None,
    model: str = "",
    scope: str = "full",
    trigger_source: str = "manual",
    request_payload: str | None = None,
    error_message: str | None = None,
    created_at: datetime | None = None,
) -> AiAnalysisRun:
    now = created_at or datetime.now(timezone.utc)
    # `current_provenance` 里本来就带 model（它是缓存判等的一部分），所以模型名要**并进
    # 那一份**，不能再作为独立关键字传一次。
    fingerprint = provenance.current_provenance(project_id)
    if model:
        fingerprint["model"] = model
    run = AiAnalysisRun(
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target_key=target_key,
        status=status,
        scope=scope,
        trigger_source=trigger_source,
        started_at=now,
        finished_at=now + timedelta(seconds=30),
        created_at=now,
        response_text=response_text,
        response_payload=json.dumps(
            payload if payload is not None else {"risk_level": "high", "risk_reasons": ["模型报出 1 条"]},
            ensure_ascii=False,
        ),
        request_payload=request_payload,
        error_message=error_message,
        **fingerprint,
    )
    db.session.add(run)
    db.session.commit()
    return run


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


# ---------------------------------------------------------------------------
#  成功那一路
# ---------------------------------------------------------------------------
def test_a_weekly_report_downloads_as_a_markdown_attachment():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        assert response.status_code == 200
        assert response.mimetype == "text/markdown"
        disposition = response.headers["Content-Disposition"]
        assert "attachment" in disposition
        assert ".md" in disposition
        # 中文文件名按 RFC 5987 编码，浏览器拿到的就是这一份中文名
        assert "filename*=UTF-8''" in disposition
        assert urllib.parse.quote(project.name) in disposition
        assert "AI%E5%88%86%E6%9E%90%E6%8A%A5%E5%91%8A" in disposition  # 「AI分析报告」

        body = response.get_data(as_text=True)
        assert REPORT_TEXT in body, "报告原文必须逐字出现在下载的文件里"
        assert "| 项目 |" in body and project.name in body
        assert "周版本" in body and cfg.name.split(" - ")[0] in body


def test_the_commit_report_names_the_commit_and_its_subject():
    with app.app_context():
        project, repo, _cfg = _setup_config()
        commit = Commit(
            repository_id=repo.id,
            commit_id="a" * 40,
            path="code/a.lua",
            operation="M",
            author="tester",
            commit_time=datetime.now(timezone.utc),
            message="修复奖励发放顺序",
            status="pending",
        )
        db.session.add(commit)
        db.session.commit()
        run = _run(
            project_id=project.id, target_type="commit", target_id=commit.id
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        body = response.get_data(as_text=True)
        assert "提交 aaaaaaaaaaaa（修复奖励发放顺序）" in body


def test_the_anomalies_from_the_payload_reach_the_appendix():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
            payload={
                "risk_level": "high",
                "risk_reasons": ["模型报出 1 条达门槛的问题"],
                "anomalies": [
                    {
                        "severity": "critical",
                        "confidence": "very_high",
                        "category": "config_id",
                        "title": "7007 悬空",
                        "file_path": "config/a.xlsx",
                        "impact": "引用不到",
                        "evidence": ["a.xlsx 第 3 行"],
                        "suggestion": "补一条",
                    }
                ],
                "suppressed_count": 2,
            },
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        body = response.get_data(as_text=True)
        # 这一条 payload 里没有指纹、库里也没有对应的行 → 处置列写 `-`，
        # **不是**「待确认」：没找到记录不等于「还没人处理过」。
        assert "| 严重 | 很高 | 配置 ID | - | 7007 悬空 | config/a.xlsx | 引用不到 |" in body
        assert "另有 2 条此前已被人工标记忽略，不在此列。" in body
        assert "待确认" not in body


def _anomaly_row(run, *, fingerprint: str, disposition: str = "pending") -> AiAnalysisAnomaly:
    return AiAnalysisAnomaly(
        run_id=run.id,
        project_id=run.project_id,
        fingerprint=fingerprint,
        title="7007 悬空",
        category="config_id",
        severity="critical",
        confidence="very_high",
        evidence=json.dumps(["a.xlsx 第 3 行"], ensure_ascii=False),
        commit_ref="",
        file_path="config/a.xlsx",
        impact="引用不到",
        suggestion="补一条",
        disposition=disposition,
    )


def test_the_disposition_column_is_read_from_the_database_at_export_time():
    """附录的「处置」列取的是**导出这一刻库里的行**，不是 payload 快照。

    这根线很容易接错：`response_payload.anomalies` 是分析当时的快照（每个条目只有
    `fingerprint`，**没有**处置状态），照它渲染就永远只有一种值。所以路由必须按这一次
    运行现查 `AiAnalysisAnomaly`，并按指纹对上。这条端到端把它钉住：写进库、再导，
    导出来的就是那个状态；改了状态再导，这一列跟着变（而报告正文逐字不变）。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
            payload={
                "risk_level": "high",
                "risk_reasons": ["模型报出 1 条达门槛的问题"],
                "anomalies": [
                    {
                        "severity": "critical",
                        "confidence": "very_high",
                        "category": "config_id",
                        "title": "7007 悬空",
                        "file_path": "config/a.xlsx",
                        "impact": "引用不到",
                        "evidence": ["a.xlsx 第 3 行"],
                        "suggestion": "补一条",
                        "fingerprint": "fp-matched",
                    }
                ],
                "suppressed_count": 0,
            },
        )
        db.session.add(_anomaly_row(run, fingerprint="fp-matched", disposition="confirmed"))
        # 指纹对不上的一行**不许贴到这条上**（同名不同条是常事）
        db.session.add(_anomaly_row(run, fingerprint="fp-other", disposition="ignored"))
        db.session.commit()

        with app.test_client() as client:
            _login(client)
            before = client.get(f"/ai-analysis/runs/{run.id}/report.md").get_data(as_text=True)

        assert "| 严重 | 很高 | 配置 ID | 已确认 | 7007 悬空 | config/a.xlsx | 引用不到 |" in before
        # 只查那一格：附录开头那句说明里本来就有「已忽略」这个词
        assert "| 配置 ID | 已忽略 |" not in before, "指纹没对上，却把别人的处置贴了上来"

        row = AiAnalysisAnomaly.query.filter_by(run_id=run.id, fingerprint="fp-matched").first()
        row.disposition = "ignored"
        db.session.commit()

        with app.test_client() as client:
            _login(client)
            after = client.get(f"/ai-analysis/runs/{run.id}/report.md").get_data(as_text=True)

        assert "| 严重 | 很高 | 配置 ID | 已忽略 | 7007 悬空 | config/a.xlsx | 引用不到 |" in after
        # 报告原文逐字不变 —— 变的是人工处置的进度，不是模型说过的话
        assert REPORT_TEXT in after


def test_the_focus_label_comes_from_the_stored_request_payload():
    """「分析范围」那一行取的是**发起时写下的那个标签**，不在这里重算 ——
    重算等于多一份会漂移的实现（`_filter_delta_files_by_focus`）。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
            request_payload=json.dumps({"focus": {"key": "table", "label": "仅配表仓库"}}),
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        assert "| 分析焦点 | 仅配表仓库 |" in response.get_data(as_text=True)


def test_a_broken_request_payload_does_not_break_the_export():
    """老记录 / 手工改过的 JSON：读不出来就当没有，不能 500。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
            request_payload="{这不是 JSON",
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        assert response.status_code == 200
        assert "分析焦点" not in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
#  没有可导出的结论：回 JSON，不回空文件
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,response_text,expected",
    [
        ("failed", "", "分析失败"),
        ("failed", REPORT_TEXT, "分析失败"),
        ("running", REPORT_TEXT, "还在跑"),
        ("pending", "", "还在排队"),
        ("succeeded", "", "没有可导出的报告正文"),
        ("succeeded", "   ", "没有可导出的报告正文"),
    ],
)
def test_a_run_without_a_conclusion_gets_a_json_refusal(status, response_text, expected):
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            status=status,
            response_text=response_text,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
            error_message="模型没答上来",
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        assert response.status_code == 409
        assert response.is_json, "失败路径必须是 JSON —— 不能给一个空的 .md 文件"
        assert "Content-Disposition" not in response.headers
        payload = response.get_json()
        assert payload["success"] is False
        assert expected in payload["message"]


def test_a_failed_run_says_why_in_the_message():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            status="failed",
            response_text="",
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
            error_message="额度用完了",
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        assert "额度用完了" in response.get_json()["message"]


# ---------------------------------------------------------------------------
#  权限与边界
# ---------------------------------------------------------------------------
def test_an_unknown_run_is_404():
    with app.app_context():
        with app.test_client() as client:
            _login(client)
            response = client.get("/ai-analysis/runs/99999999/report.md")
        assert response.status_code == 404
        assert response.is_json


def test_a_run_of_another_project_is_403():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
        )
        with app.test_client() as client:
            _login(client)
            original = ai_routes._has_project_access
            ai_routes._has_project_access = lambda _pid: False
            try:
                response = client.get(f"/ai-analysis/runs/{run.id}/report.md")
            finally:
                ai_routes._has_project_access = original

        assert response.status_code == 403
        assert response.is_json


def test_the_permission_check_uses_the_runs_own_project_not_a_parameter():
    """带一个「我有权限的项目」当参数，也拿不到别人的报告。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            target_key=ai_service.build_weekly_group_key(cfg),
        )
        seen: list[int] = []
        with app.test_client() as client:
            _login(client)
            original = ai_routes._has_project_access

            def _record(pid):
                seen.append(pid)
                return False

            ai_routes._has_project_access = _record
            try:
                response = client.get(
                    f"/ai-analysis/runs/{run.id}/report.md?project_id=1"
                )
            finally:
                ai_routes._has_project_access = original

        assert response.status_code == 403
        assert seen == [project.id], "判权只看这条运行自己的项目"


def test_the_export_path_is_get_only():
    rules = [
        rule
        for rule in app.url_map.iter_rules()
        if str(rule).endswith("/report.md")
    ]
    assert rules, "找不到导出路由"
    for rule in rules:
        assert rule.methods is not None
        assert "GET" in rule.methods
        assert "POST" not in rule.methods


def test_the_export_path_does_not_look_like_a_usage_endpoint():
    """`tests/test_ai_usage_capture.py` 会扫所有带 `/usage` 的路由并断言它们是「只回 JSON 的
    只读端点」—— 这个端点回的是文件，路径里就不能出现 `usage`。"""
    rules = [str(rule) for rule in app.url_map.iter_rules()]
    assert any(rule.endswith("/report.md") for rule in rules)
    for rule in rules:
        if rule.endswith("/report.md"):
            assert "usage" not in rule


def test_a_deleted_weekly_config_falls_back_to_a_plain_label():
    """目标行被删掉时不许编一个名字，也不许 500。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(project_id=project.id, target_type="weekly", target_id=cfg.id)
        db.session.delete(cfg)
        db.session.commit()
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        assert response.status_code == 200
        assert "| 目标 | 周版本 |" in response.get_data(as_text=True)


def test_an_unreadable_response_payload_still_exports_the_report_text():
    """`response_payload` 坏掉时：报告原文照样导出，元信息里少那几行。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(project_id=project.id, target_type="weekly", target_id=cfg.id)
        run.response_payload = "{坏掉的 JSON"
        db.session.commit()
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert REPORT_TEXT in body
        assert "| 风险等级 | - |" in body
        assert "是否降级" in body


def test_the_downloaded_body_is_exactly_what_the_document_builder_produces():
    """路由只负责取元信息 —— 拼文档的逻辑一行都不许长在这里。

    否则「抽屉里显示的报告」与「下载下来的报告」会各有一套排版，而且没人会发现。
    """
    with app.app_context():
        project, repo, _cfg = _setup_config()
        commit = Commit(
            repository_id=repo.id,
            commit_id="b" * 40,
            path="code/b.lua",
            operation="M",
            author="tester",
            commit_time=datetime.now(timezone.utc),
            message="改了一行",
            status="pending",
        )
        db.session.add(commit)
        db.session.commit()
        payload = {"risk_level": "medium", "risk_reasons": ["变更规模：3 个文件"]}
        run = _run(
            project_id=project.id,
            target_type="commit",
            target_id=commit.id,
            payload=payload,
            scope="incremental",
            trigger_source="scheduled",
            request_payload=None,
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        expected = doc.build_report_markdown(
            project_label=project.name,
            target_label=doc.commit_target_label(commit.commit_id, commit.message),
            run_id=run.id,
            created_at_display=doc.beijing_display(run.created_at),
            risk_level="medium",
            risk_reasons=["变更规模：3 个文件"],
            scope="incremental",
            trigger_source="scheduled",
            model=run.model,
            degradation_label="",
            focus_label="",
            report_text=REPORT_TEXT,
            anomalies=[],
            suppressed_count=0,
        )
        assert response.get_data(as_text=True) == expected


def test_the_downloaded_file_has_a_readable_chinese_name():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(project_id=project.id, target_type="weekly", target_id=cfg.id)
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        name = doc.report_filename(
            project_name=project.name,
            target_label=doc.weekly_target_label(cfg.name, cfg.start_time, cfg.end_time),
            when=run.created_at,
        )
        assert urllib.parse.quote(name, safe="") in response.headers["Content-Disposition"]


# ---------------------------------------------------------------------------
#  维度那一列：按**结果里存的那份清单**翻中文名
# ---------------------------------------------------------------------------
def _declare_dimensions(projects_root, project_code: str, declaration: str) -> None:
    """给项目写一份 `references/project-facts.md`（**项目当前的声明**）。"""
    pack = projects_root / project_pack_slug(project_code) / "references"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "project-facts.md").write_text(
        "---\n"
        f"name: {project_pack_slug(project_code)}\n"
        f"description: {project_code} 的项目事实\n"
        f"dimensions: {declaration}\n"
        "---\n\n正文。\n",
        encoding="utf-8",
    )


def test_the_dimension_column_uses_the_list_stored_with_the_result(tmp_path, monkeypatch):
    """导出按**结果里存的那份清单**渲染，不是按项目今天的声明。

    这条用例的两个关键点缺一不可：

    * 结果里存的是 A（`performance` = 性能与耗时）；
    * 项目**今天**的声明是 B（同一个 id、另一个中文名）。

    一个「导出时现查项目当前声明」的实现会把 B 打进去 —— 那不是「显示得不准」，而是
    按新清单**改写历史**：项目改了声明之后，历史结论会被重新贴标签（当时合法的维度
    变成「未归类」，或者同一个 id 换了个名字），而报告读起来完全正常。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        projects_root = tmp_path / "projects"
        monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(projects_root))
        _declare_dimensions(projects_root, project.code, "performance=今天的叫法")

        run = _run(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            payload={
                "risk_level": "high",
                "risk_reasons": ["模型报出 1 条"],
                # 这次分析**当时**生效的清单：平台自己留存的那一份。
                "dimension_specs": [{"id": "performance", "label": "性能与耗时"}],
                "anomalies": [
                    {
                        "title": "性能回归",
                        "category": "performance",
                        "severity": "high",
                        "confidence": "high",
                        "evidence": ["耗时从 12ms 涨到 400ms"],
                    }
                ],
            },
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "性能与耗时" in body, "必须按结果里存的那份清单翻中文名"
        assert "今天的叫法" not in body, (
            "导出时现查了项目当前声明 —— 那是按新清单改写历史结论"
        )
        assert "未归类（performance）" not in body, (
            "项目自己声明的维度被显示成了「未归类」"
        )
        assert "平台没有丢弃它们" not in body, "没有未归类条目时不该多那句说明"


def test_the_dimension_column_falls_back_when_the_result_has_no_list(tmp_path, monkeypatch):
    """结果里**没有**这份清单时回落到平台出厂清单（字段缺失的兜底，不是兼容旧数据）。

    认不出的 category 仍然要**响亮**：显示成「未归类（<原始 id>）」并加一句说明，
    而不是把原始 id 当做一个正常的维度名。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _run(
            project_id=project.id,
            target_type="weekly",
            target_id=cfg.id,
            payload={
                "risk_level": "high",
                "risk_reasons": ["模型报出 1 条"],
                "anomalies": [
                    {
                        "title": "性能回归",
                        "category": "performance",
                        "severity": "high",
                        "confidence": "high",
                        "evidence": ["e"],
                    }
                ],
            },
        )
        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

        body = response.get_data(as_text=True)
        assert "未归类（performance）" in body
        assert "平台没有丢弃它们" in body
