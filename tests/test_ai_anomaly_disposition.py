# -*- coding: utf-8 -*-
"""`AiAnalysisAnomaly.disposition` 的写路径。

## 这个文件在钉什么

这一列**曾经整棵树都没有写路径**：读侧是通的（`baseline_source.baseline_findings`
→ `baseline.classify` → `suppressed` → `result_payload` 把「已忽略且文件没再变」的
结论从下一轮清单里剔掉），但没有入口能把它设成非默认值，于是那条链路一次都没生效过。

所以这里要钉的不只是「接口能改这一列」，还有**改完之后那条链路真的会变**——
`test_ignoring_a_finding_removes_it_from_the_next_run` 就是干这个的。少了它，
「接口 200」什么都证明不了（这个仓库里已经有过好几个「写进去了但没人读」的例子）。

## 三个容易写成假绿的地方

1. **权限**。断言「403」时必须确认**真的有一个能访问该项目的用户**这条对照 ——
   否则一个「永远 403」的实现也能让权限用例全绿。所以下面每条权限用例都配一条
   同形但应当 200 的请求。
2. **`?disposition=` 认不出来时**。悄悄当成「全部」的实现会让「只看已忽略」列出全部
   条目，而用户不会发现 —— 所以这一条要断言 400，不是断言「返回了东西」。
3. **批量处置的 `ids` 跨运行**。不按 `run_id` 过滤的实现会改到另一次运行的行上，
   而返回的计数完全正常。这条要用**另一次运行的真实 id** 去打。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import routes.ai_analysis_routes as ai_routes
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from services.ai import baseline_source
from services.ai.anomaly_disposition import (
    DISPOSITION_NOTE_MAX_CHARS,
    NOTE_TRUNCATION_MARK,
    DispositionError,
    anomalies_of_run,
    normalize_disposition,
    normalize_note,
    set_disposition,
    set_many,
)
from services.ai.baseline import classify, suppressed_fingerprints
from services.ai.result_payload import result_payload
from tests.test_ai_history_survives_restart import _uid


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


def _login(client, *, admin: bool = True):
    with client.session_transaction() as session:
        session["is_admin"] = admin
        session["admin_user"] = "disposition-tester"
        session["_csrf_token"] = _uid("csrf")


def _csrf(client) -> str:
    with client.session_transaction() as session:
        return session["_csrf_token"]


def _project():
    project = Project(code=_uid("P"), name=_uid("disp-project"))
    db.session.add(project)
    db.session.flush()
    repo = Repository(
        project_id=project.id,
        name=_uid("code"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        resource_type="code",
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
        start_time=datetime(2026, 3, 1),
        end_time=datetime(2026, 3, 8),
        cycle_type="custom",
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.commit()
    return project, cfg


def _run(project_id, target_key, *, status="succeeded") -> AiAnalysisRun:
    now = datetime.now(timezone.utc)
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_id=None,
        target_key=target_key,
        status=status,
        scope="full",
        trigger_source="manual",
        started_at=now,
        finished_at=now,
        created_at=now,
        response_text="# 报告\n",
        response_payload=json.dumps({"anomalies": [], "report_markdown": "# 报告\n"}),
        # **必须给**：上一轮能不能当基线，看的是「它有没有结构化结论」这一个字段
        # （见 `baseline_source.previous_run`）。不给就是 NULL，那上一次运行会被整条
        # 跳过 —— 于是下面那条「忽略了就不再提」的用例会以一个空基线通过，
        # 而它本来要证明的东西一个字都没被证明。
        conclusion_structured=True if status == "succeeded" else None,
    )
    db.session.add(run)
    db.session.commit()
    return run


def _anomaly(run, *, title="【道具】ID 被删除但生成文件仍在", severity="high",
             fingerprint=None, file_path="config/道具表.xlsx") -> AiAnalysisAnomaly:
    row = AiAnalysisAnomaly(
        run_id=run.id,
        project_id=run.project_id,
        fingerprint=fingerprint or _uid("fp"),
        title=title,
        category="config_id",
        severity=severity,
        confidence="high",
        evidence=json.dumps(["config/道具表.xlsx 删除了 ID 1001"], ensure_ascii=False),
        commit_ref="a" * 40,
        file_path=file_path,
        impact="老存档引用的道具失效",
        suggestion="确认是否有意下线",
    )
    db.session.add(row)
    db.session.commit()
    return row


# ==========================================================================
# 一、服务层：取值与四个字段的口径
# ==========================================================================


class TestTheValueIsNeverGuessed:
    def test_an_unknown_disposition_is_rejected(self):
        """**不悄悄退回 pending**：那会让「我标了已忽略」变成一件没有痕迹的假事。"""
        with pytest.raises(DispositionError):
            normalize_disposition("done")

    def test_a_missing_disposition_is_rejected_not_defaulted(self):
        with pytest.raises(DispositionError):
            normalize_disposition(None)

    def test_known_values_pass_through_case_insensitively(self):
        assert normalize_disposition("IGNORED") == "ignored"


class TestTheFourFieldsAreOneUnit:
    def test_setting_a_disposition_records_who_and_when(self):
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            row = _anomaly(run)

            stamp = datetime(2026, 5, 1, tzinfo=timezone.utc)
            set_disposition(row, disposition="confirmed", note="已核对", username="张三", now=stamp)
            db.session.commit()

            assert row.disposition == "confirmed"
            assert row.disposition_by == "张三"
            # SQLite 的 `DateTime` 存回来是**不带时区**的（本仓库所有列都是这个口径，
            # 见 `created_at`），所以比到分钟、不比 tzinfo。
            assert row.disposition_at.replace(tzinfo=None) == stamp.replace(tzinfo=None)
            assert row.disposition_note == "已核对"

    def test_reverting_to_pending_clears_the_companions(self):
        """撤销时必须把备注一起清掉。

        留着上一轮的备注，「待确认」那条读起来就像已经有了结论 —— 而那正是最容易
        让人跳过它的一种错觉。
        """
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            row = _anomaly(run)
            set_disposition(row, disposition="ignored", note="误报", username="张三")
            db.session.commit()

            set_disposition(row, disposition="pending", username="李四")
            db.session.commit()

            assert row.disposition == "pending"
            assert row.disposition_by is None
            assert row.disposition_at is None
            assert row.disposition_note is None


class TestTheNoteIsCappedButNotSilently:
    def test_a_long_note_is_truncated_with_a_mark(self):
        text = normalize_note("凑" * (DISPOSITION_NOTE_MAX_CHARS + 50))

        assert text.endswith(NOTE_TRUNCATION_MARK)
        assert "凑" * DISPOSITION_NOTE_MAX_CHARS in text

    def test_normalizing_is_idempotent(self):
        """批量那条路径会对同一段文本调用两次 —— 不幂等就会一次次追加标记。

        第一版的标记里带了原长度（`原备注 N 字`），第二次调用把第一次的结果当成原文，
        于是长度一路涨。这一条就是那时候补的。
        """
        once = normalize_note("凑" * (DISPOSITION_NOTE_MAX_CHARS + 50))

        assert normalize_note(once) == once

    def test_a_short_note_is_left_alone(self):
        assert normalize_note("  已核对  ") == "已核对"


class TestAnomaliesOfRun:
    def test_it_sorts_by_severity_then_confidence(self):
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            _anomaly(run, title="低", severity="high")
            _anomaly(run, title="高", severity="critical")
            _anomaly(run, title="更高", severity="critical")

            rows = anomalies_of_run(run.id)

            assert rows[0].severity == "critical"
            assert rows[-1].title == "低"

    def test_it_can_filter_by_disposition(self):
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            a = _anomaly(run, title="甲")
            _anomaly(run, title="乙")
            set_disposition(a, disposition="ignored", username="t")
            db.session.commit()

            assert [row.title for row in anomalies_of_run(run.id, disposition="ignored")] == ["甲"]


class TestBatchCounting:
    def test_the_count_is_what_actually_changed(self):
        """按「提交了几条」报数，用户重复点一次会看到「已处置 3 条」而一条都没动。"""
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            rows = [_anomaly(run, title=f"第{i}条") for i in range(3)]

            assert set_many(rows, disposition="ignored", username="t") == 3
            assert set_many(rows, disposition="ignored", username="t") == 0

    def test_reverting_to_pending_counts_even_though_it_is_the_default(self):
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            rows = [_anomaly(run, title=f"第{i}条") for i in range(2)]
            set_many(rows, disposition="ignored", username="t")

            assert set_many(rows, disposition="pending", username="t") == 2


# ==========================================================================
# 二、写完之后那条链路真的会变
# ==========================================================================


class TestTheLoopActuallyCloses:
    def test_ignoring_a_finding_removes_it_from_the_next_run(self):
        """**这是这一列存在的全部理由**：忽略过的条目，下一轮不再提着。

        链路：写行的 `disposition` → `baseline_findings` 读出来 → `classify` 判成
        suppressed → `suppressed_fingerprints` → `result_payload` 剔掉。
        中间任何一环断了，「接口返回 200」都照样是绿的。

        **指纹必须用引擎那一份算法算出来**，不能随手给一个：抑制是**按指纹**匹配的，
        行上的指纹与下一轮算出来的对不上，整条链路会静默失效 —— 而这一条用例会用
        「行上的指纹是我自己编的」这种写法把一个真 bug 糊过去。
        """
        from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome
        from services.ai.protocol import Anomaly
        from services.ai.rules import anomaly_fingerprint

        with app.app_context():
            project, cfg = _project()
            target_key = _uid("k")
            previous = _run(project.id, target_key)
            anomaly = Anomaly(
                title="【道具】ID 被删除但生成文件仍在",
                category="config_id",
                severity="high",
                confidence="high",
                evidence=("config/道具表.xlsx 删除了 ID 1001",),
                commit="a" * 40,
                file_path="config/道具表.xlsx",
                impact="老存档引用的道具失效",
                suggestion="确认是否有意下线",
            )
            fingerprint = anomaly_fingerprint(anomaly)
            row = _anomaly(previous, fingerprint=fingerprint)
            set_disposition(row, disposition="ignored", username="张三")
            db.session.commit()

            findings = baseline_source.baseline_findings("weekly", target_key)
            assert [item.fingerprint for item in findings] == [fingerprint]
            assert findings[0].disposition == "ignored", (
                "写进去的处置没有被基线读侧看见 —— 界面上标了「已忽略」，下一轮照样再报"
            )

            suppressed = suppressed_fingerprints(classify(findings))
            assert fingerprint in suppressed

            outcome = EngineOutcome(status=STATUS_SUCCEEDED, anomalies=(anomaly,))
            payload = result_payload(outcome, {}, suppressed=suppressed)

            assert payload["anomalies"] == []
            assert payload["suppressed_count"] == 1, (
                "剔掉了却不说剔了几条 —— 用户会以为这条结论自己消失了"
            )

            assert payload["anomalies"] == []
            assert payload["suppressed_count"] == 1, (
                "剔掉了却不说剔了几条 —— 用户会以为这条结论自己消失了"
            )

    def test_a_changed_file_brings_an_ignored_finding_back(self):
        """忽略**不等于**永远看不见：那个文件又变了就要重新确认。

        没有这一条，「忽略」会变成一张可以把任何问题永久藏起来的封条。
        """
        with app.app_context():
            project, cfg = _project()
            target_key = _uid("k")
            previous = _run(project.id, target_key)
            row = _anomaly(previous, file_path="config/道具表.xlsx")
            set_disposition(row, disposition="ignored", username="张三")
            db.session.commit()

            findings = baseline_source.baseline_findings("weekly", target_key)

            assert suppressed_fingerprints(
                classify(findings, changed_paths=["config/道具表.xlsx"])
            ) == frozenset()


# ==========================================================================
# 三、路由
# ==========================================================================


class TestTheReadRoute:
    def test_it_returns_rows_counts_and_labels(self):
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            a = _anomaly(run, title="甲")
            _anomaly(run, title="乙")
            set_disposition(a, disposition="ignored", username="张三")
            db.session.commit()
            run_id, anomaly_id = run.id, a.id

            with app.test_client() as client:
                _login(client)
                body = client.get(f"/ai-analysis/runs/{run_id}/anomalies").get_json()

        assert body["success"] is True
        assert body["total"] == 2
        assert body["counts"] == {"pending": 1, "confirmed": 0, "ignored": 1}
        # 中文标签由服务端给（界面不自己映射），且三个状态一个都不少。
        assert [item["value"] for item in body["dispositions"]] == ["pending", "confirmed", "ignored"]
        assert {item["label"] for item in body["dispositions"]} == {"待确认", "已确认", "已忽略"}
        one = [item for item in body["anomalies"] if item["id"] == anomaly_id][0]
        assert one["disposition"] == "ignored"
        assert one["disposition_by"] == "张三"
        # 证据出成数组，不是库里那个 JSON 文本（否则界面得自己 parse）。
        assert one["evidence"] == ["config/道具表.xlsx 删除了 ID 1001"]

    def test_an_unknown_filter_is_a_400_not_a_silent_show_all(self):
        """悄悄当成「全部」的话，用户点「只看已忽略」会看到全部条目而毫无察觉。"""
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            _anomaly(run)
            run_id = run.id

            with app.test_client() as client:
                _login(client)
                good = client.get(f"/ai-analysis/runs/{run_id}/anomalies?disposition=ignored")
                bad = client.get(f"/ai-analysis/runs/{run_id}/anomalies?disposition=done")

        assert good.status_code == 200, "对照：合法取值必须 200"
        assert bad.status_code == 400
        assert "disposition" in bad.get_json()["message"] or "处置状态" in bad.get_json()["message"]

    def test_a_denied_and_an_allowed_user_differ(self):
        """判权的写法照抄 `test_ai_report_export_route`：真登录 + 真路由，
        只在**权限判定那一处**打桩。

        直接 `_login(admin=False)` 是**没有登录态**，会被登录闸门 302 掉 ——
        那样这条用例证明的是「没登录会被重定向」，而不是「有登录但没权限会被 403」。
        """
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            _anomaly(run)
            run_id = run.id

            with app.test_client() as client:
                _login(client)
                original = ai_routes._has_project_access
                ai_routes._has_project_access = lambda _pid: False
                try:
                    denied = client.get(f"/ai-analysis/runs/{run_id}/anomalies")
                finally:
                    ai_routes._has_project_access = original
                allowed = client.get(f"/ai-analysis/runs/{run_id}/anomalies")

        assert denied.status_code == 403
        assert allowed.status_code == 200, "对照：同一个 id 有权限时必须通"


class TestTheWriteRoute:
    def test_posting_a_disposition_persists_it(self):
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            row = _anomaly(run)
            anomaly_id = row.id

            with app.test_client() as client:
                _login(client)
                response = client.post(
                    f"/ai-analysis/anomalies/{anomaly_id}/disposition",
                    json={"disposition": "ignored", "note": "误报，已确认"},
                    headers={"X-CSRF-Token": _csrf(client)},
                )
                body = response.get_json()
                reread = client.get(f"/ai-analysis/runs/{run.id}/anomalies").get_json()

        assert response.status_code == 200, body
        assert body["anomaly"]["disposition"] == "ignored"
        # **读回来**才算数：只断言写接口的返回，一个「返回了但没落库」的实现也是绿的。
        stored = [item for item in reread["anomalies"] if item["id"] == anomaly_id][0]
        assert stored["disposition"] == "ignored"
        assert stored["disposition_note"] == "误报，已确认"
        assert stored["disposition_at"] is not None
        # 处置人**必须**落上。平台管理员（环境变量那种）没有数据库用户对象，
        # 自己 `getattr(user, "username", "")` 会得到空串 —— 而「谁把这条标成已忽略的」
        # 正是最需要留名的一处。
        assert stored["disposition_by"] == "disposition-tester"

    def test_an_invalid_value_is_a_400_and_nothing_changes(self):
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            row = _anomaly(run)
            anomaly_id = row.id

            with app.test_client() as client:
                _login(client)
                response = client.post(
                    f"/ai-analysis/anomalies/{anomaly_id}/disposition",
                    json={"disposition": "done"},
                    headers={"X-CSRF-Token": _csrf(client)},
                )

            db.session.expire_all()
            assert response.status_code == 400
            assert db.session.get(AiAnalysisAnomaly, anomaly_id).disposition == "pending"

    def test_a_denied_user_cannot_write(self):
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            row = _anomaly(run)
            anomaly_id = row.id

            with app.test_client() as client:
                _login(client)
                token = _csrf(client)
                original = ai_routes._has_project_access
                ai_routes._has_project_access = lambda _pid: False
                try:
                    denied = client.post(
                        f"/ai-analysis/anomalies/{anomaly_id}/disposition",
                        json={"disposition": "ignored"},
                        headers={"X-CSRF-Token": token},
                    )
                finally:
                    ai_routes._has_project_access = original
                allowed = client.post(
                    f"/ai-analysis/anomalies/{anomaly_id}/disposition",
                    json={"disposition": "ignored"},
                    headers={"X-CSRF-Token": token},
                )

            db.session.expire_all()
            assert denied.status_code == 403
            assert allowed.status_code == 200, "对照：有权限的人必须写得进去"
            assert db.session.get(AiAnalysisAnomaly, anomaly_id).disposition == "ignored"

    def test_an_unknown_anomaly_is_a_404(self):
        with app.app_context():
            with app.test_client() as client:
                _login(client)
                response = client.post(
                    "/ai-analysis/anomalies/99999999/disposition",
                    json={"disposition": "ignored"},
                    headers={"X-CSRF-Token": _csrf(client)},
                )
        assert response.status_code == 404


class TestTheBatchRoute:
    def test_it_changes_all_of_them(self):
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            ids = [_anomaly(run, title=f"第{i}条").id for i in range(3)]
            run_id = run.id

            with app.test_client() as client:
                _login(client)
                response = client.post(
                    f"/ai-analysis/runs/{run_id}/anomalies/disposition",
                    json={"ids": ids, "disposition": "ignored", "note": "本批均为误报"},
                    headers={"X-CSRF-Token": _csrf(client)},
                )
                body = response.get_json()

        assert response.status_code == 200, body
        assert body["changed"] == 3
        assert body["counts"]["ignored"] == 3
        assert all(item["disposition"] == "ignored" for item in body["anomalies"])

    def test_ids_from_another_run_are_rejected(self):
        """不按 `run_id` 过滤的实现会改到另一次运行的行上，而计数完全正常。"""
        with app.app_context():
            project, cfg = _project()
            run_a = _run(project.id, _uid("k"))
            run_b = _run(project.id, _uid("k"))
            outsider = _anomaly(run_b, title="另一次运行的结论")
            mine = _anomaly(run_a, title="这次的结论")

            with app.test_client() as client:
                _login(client)
                response = client.post(
                    f"/ai-analysis/runs/{run_a.id}/anomalies/disposition",
                    json={"ids": [mine.id, outsider.id], "disposition": "ignored"},
                    headers={"X-CSRF-Token": _csrf(client)},
                )

            db.session.expire_all()
            assert response.status_code == 400
            assert "不属于本次运行" in response.get_json()["message"]
            # **一条都不许改**：验完再改一半比整批拒掉更难解释。
            assert db.session.get(AiAnalysisAnomaly, outsider.id).disposition == "pending"
            assert db.session.get(AiAnalysisAnomaly, mine.id).disposition == "pending"

    def test_an_empty_id_list_is_rejected(self):
        """不给「不传 ids = 全部」这种默认：一次误点把整份报告标成已忽略，
        下一轮这些条目集体消失，而用户没有任何地方能看出是被误标的。"""
        with app.app_context():
            project, cfg = _project()
            run = _run(project.id, _uid("k"))
            _anomaly(run)
            run_id = run.id

            with app.test_client() as client:
                _login(client)
                response = client.post(
                    f"/ai-analysis/runs/{run_id}/anomalies/disposition",
                    json={"disposition": "ignored"},
                    headers={"X-CSRF-Token": _csrf(client)},
                )
                body = response.get_json()

        assert response.status_code == 400
        assert "ids" in body["message"]
