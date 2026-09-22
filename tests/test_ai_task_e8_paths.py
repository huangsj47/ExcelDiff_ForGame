# -*- coding: utf-8 -*-
"""任务 E3 / E8 的收尾：**截断可归因** + **五条周版本路径走 HTTP**。

## 为什么单独一个文件

`tests/test_ai_job_protocol.py` 已经 1000+ 行（它守的是「发起 / 订阅」两条协议），
E8 这五条路径是**用户点下去之后平台做什么**，判据完全不同。堆进去会让那个文件里
两套断言的失败原因混在一起。

## 这个文件守的两件事

1. **每次截断都说得出命中了哪个约束**（E3 验收原文）。原先 trace 的 details 里只有
   `truncated: true/false`，没有「是哪一条约束砍的」也没有「砍到多少」—— 事后翻账
   只能看到「这一条被截了」，看不出该去调哪个配置。
2. **五条路径各有一次端到端**（E8 验收原文）：普通增量、版本不匹配增量、全量、
   已有任务附着、无变化复用。前四条在 `POST /ai-analysis/weekly/<id>/jobs` 上就能
   看到完整的裁决（`effective_mode` / `upgrade_reason` / `base_run_id` / `attached`）；
   第五条要真的跑一次生产执行入口才看得见（结论被复用、**没有新建付费运行**）。

**测试库是会话级共用的**：断言一律按本用例自己造的项目 / 分组过滤，绝不数全表。
"""
from __future__ import annotations

# ruff: noqa: I001 —— 与 `tests/test_ai_job_protocol.py` 同一条理由：这几个模块互为
# 环形依赖，`services.task_worker_service` 必须**先**加载，否则任务处理器只加载了一半
# 就被别人取名字 → ImportError。isort 要的字母序恰好相反，整文件放行 I001。

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import services.ai_analysis_service as ai_service
import services.task_worker_service as worker_service  # noqa: F401 —— 环形依赖的定序，见上
import services.task_worker_task_handlers as task_handlers
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from models.ai_analysis import AiAnalysisRun, AiWeeklyAnalysisState
from services.ai import job_service
from services.ai.provenance import current_provenance

_CREATED: dict = {"projects": [], "groups": []}

_PATH = "tables/e8_table_{index}.xlsx"


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


@pytest.fixture(autouse=True)
def _cleanup_rows_this_file_creates():
    """造出来的东西自己收干净（会话级共用的库，没有逐用例重置）。"""
    yield
    from models import BackgroundTask
    from models.ai_analysis import AiAnalysisJob

    with app.app_context():
        for group_key in _CREATED["groups"]:
            AiAnalysisRun.query.filter(AiAnalysisRun.target_key == group_key).delete(
                synchronize_session=False
            )
            AiWeeklyAnalysisState.query.filter(
                AiWeeklyAnalysisState.group_key == group_key
            ).delete(synchronize_session=False)
            AiAnalysisJob.query.filter(AiAnalysisJob.target_key == group_key).delete(
                synchronize_session=False
            )
            BackgroundTask.query.filter(BackgroundTask.file_path == group_key).delete(
                synchronize_session=False
            )
        for project_id in _CREATED["projects"]:
            AiAnalysisJob.query.filter(AiAnalysisJob.project_id == project_id).delete(
                synchronize_session=False
            )
        db.session.commit()
        _CREATED["projects"] = []
        _CREATED["groups"] = []


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ===========================================================================
#  一、截断可归因（E3）
# ===========================================================================


class TestEveryTruncationNamesTheConstraintThatBit:
    """E3 验收：「每次截断都能说明命中了哪个**独立约束**」。

    `limit` 回答「砍到多少」，`truncated_by` 回答「是哪一条约束砍的」。两个都要有：
    只有数字时，用户看到「已按 11000 字上限截断」，但不知道那是单条上限、总预算、
    还是窗口水位 —— 而这三件事要调的地方完全不同。
    """

    def test_the_shrink_step_names_its_own_constraint(self):
        """逐级压缩（`budget.shrink_item`）砍的那一刀要写清是哪一级。"""
        from services.ai.budget import ContextItem, shrink_item

        item = ContextItem(
            kind="file_diff",
            label="file_diff a.lua",
            text="x" * 9_000,
            meta={"original_chars": 9_000},
        )

        shrunk = shrink_item(item, 1)

        assert shrunk.meta["truncated"] is True
        assert shrunk.meta["limit"] > 0
        assert shrunk.meta["truncated_by"] == "item_shrink_level_1", shrunk.meta
        # 未触发压缩的那一条**不许**凭空多一个约束名。
        untouched = ContextItem(kind="file_diff", label="b.lua", text="short", meta={})
        assert "truncated_by" not in shrink_item(untouched, 1).meta

    def test_the_trace_details_carry_the_limit_and_the_constraint(self):
        """trace 里那一条明细要能回答「砍到多少 / 被哪条约束砍的」。

        原先 details 里只有 `truncated`（布尔），于是「这次为什么没看全」在事后翻账时
        只能靠猜 —— 这与本模块存在的理由（`trace_evidence` 的模块 docstring）是同一条。
        """
        from services.ai.budget import ContextItem
        from services.ai.trace_evidence import summarize_executed

        item = ContextItem(
            kind="file_diff",
            label="file_diff a.lua",
            text="x" * 11_000,
            meta={"truncated": True, "limit": 11_000, "truncated_by": "tool_limit_file_diff"},
        )

        entry = summarize_executed([item])[0]

        assert entry["truncated"] is True
        assert entry["limit"] == 11_000, entry
        assert entry["truncated_by"] == "tool_limit_file_diff", entry

    def test_the_fetch_side_cut_names_the_per_item_cap(self):
        """**取数侧那一刀**（`context_tools._render`）也要写出约束名。

        这条与上面那条的分工：`budget.shrink_item` 管的是「整份提示词超预算时逐级压缩」，
        而这里管的是「单条正文超过本工具的条数上限」—— 两条约束调的地方完全不同
        （前者是总预算，后者是 `tool_limits`），所以 `truncated_by` 必须是两个不同的名字。

        此前取数侧只写 `meta["limit"]`（一个数字），于是线上「27 次 diff 单条截断」这件事
        事后翻账只能看到「被砍到 11,000」，看不出该去调哪个旋钮。
        """
        from services.ai.context_tools import DEFAULT_TOOL_LIMITS, ContextTools
        from services.ai.protocol import ContextRequest
        from services.ai.trace_evidence import summarize_executed

        class _Provider:
            def file_diff(self, commit, path):
                return "代码差异：a.lua\n@@ -1 +1 @@\n" + ("+ 一行很长的改动\n" * 4_000)

            def find_references(self, query, path=""):
                return "\n".join(f"config/a.lua:{i}: local target_id = {i}" for i in range(1, 900))

        tools = ContextTools(_Provider())
        batch = tools.execute(
            [
                ContextRequest(type="file_diff", commit="a" * 40, path="config/a.lua"),
                ContextRequest(type="find_references", query="target_id"),
            ]
        )

        by_kind = {item.kind: item for item in batch.items}
        # `file_diff` 走「分段 + 点名」那一支（它在 `_WINDOWED_KINDS` 里）。
        assert by_kind["file_diff"].meta["truncated"] is True
        assert by_kind["file_diff"].meta["truncated_by"] == "tool_limit_file_diff"
        assert by_kind["file_diff"].meta["limit"] == DEFAULT_TOOL_LIMITS["file_diff"]
        # `find_references` **不在** `_WINDOWED_KINDS` 里（`windowed_kinds()` 只有
        # `commit_detail` / `file_diff` / `read_reference`），所以它走纯文本「只砍尾巴」
        # 那一支 —— **两条分支各有一个写入点**，只测一条会漏掉另一条。
        #
        # 这条一开始写的是 `commit_detail`，而它**是** windowed 的，于是两个断言其实都在
        # 测同一条分支：把纯文本那一支的写入点删掉，用例照绿（变异验出来的）。
        assert by_kind["find_references"].meta["truncated"] is True
        assert by_kind["find_references"].meta["truncated_by"] == "tool_limit_find_references"
        assert by_kind["find_references"].meta["limit"] == DEFAULT_TOOL_LIMITS["find_references"]

        # 一路走到 trace 明细：写进 meta 而没进 trace，等于没写。
        details = {row["kind"]: row for row in summarize_executed(batch.items)}
        assert details["file_diff"]["truncated_by"] == "tool_limit_file_diff", details
        assert details["find_references"]["truncated_by"] == "tool_limit_find_references", details

    def test_an_untruncated_item_reports_no_constraint(self):
        """没被截断的那一条 `limit` / `truncated_by` 必须是**空**，不是 0 或一句「未知」。

        写成 `0` 会被界面读成「上限是 0 字」（一个确定的、错误的事实）。
        """
        from services.ai.budget import ContextItem
        from services.ai.trace_evidence import summarize_executed

        item = ContextItem(kind="file_diff", label="a.lua", text="short", meta={})

        entry = summarize_executed([item])[0]

        assert entry["truncated"] is False
        assert entry["limit"] is None, entry
        assert entry["truncated_by"] == "", entry


# ===========================================================================
#  二、五条路径（E8）
# ===========================================================================


def _make_group(*, file_count: int = 6) -> dict:
    """一个周版本分组：项目 + 配表仓库 + 配置 + `file_count` 行缓存。"""
    now = datetime.now(timezone.utc)
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()
    repo = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        resource_type="table",
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    cfg = WeeklyVersionConfig(
        project_id=project.id,
        repository_id=repo.id,
        name=_uid("weekly"),
        branch="main",
        start_time=now - timedelta(days=7),
        end_time=now,
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.flush()
    commits = {}
    for index in range(file_count):
        path = _PATH.format(index=index)
        commit = f"c{index:04d}"
        commits[path] = commit
        db.session.add(
            WeeklyVersionDiffCache(
                config_id=cfg.id,
                repository_id=repo.id,
                file_path=path,
                file_type="excel",
                merged_diff_data="{}",
                base_commit_id="b" * 40,
                latest_commit_id=commit,
                commit_count=1,
                cache_status="completed",
                diff_version="1.18.0",
            )
        )
    db.session.commit()
    group_key = ai_service.build_weekly_group_key(cfg)
    _CREATED["projects"].append(project.id)
    _CREATED["groups"].append(group_key)
    # **只带裸值出去**：ORM 实例出了 app context 就是 detached，而 `commit()` 会把
    # 实例上的属性标成过期 —— 再读 `.id` 会去刷新，于是抛 DetachedInstanceError。
    return {
        "project_id": project.id,
        "repo_id": repo.id,
        "cfg_id": cfg.id,
        "group_key": group_key,
    }


def _settle_and_advance(group, *, engine_status: str = "succeeded") -> int:
    """走一遍**生产路径**（建 payload → 建 run → 落终态 → 推进指针），不真的调模型。"""
    cfg_id = group["cfg_id"]
    payload, state, skip = ai_service.build_weekly_payload(cfg_id)
    assert skip is None, f"这次 payload 没建出来：{skip}"
    run = ai_service._create_run(
        project_id=group["project_id"],
        target_type="weekly",
        target_id=cfg_id,
        target_key=payload["group"]["key"],
        response_mode="blocking",
        scope=payload["scope"],
        trigger_source="manual",
        payload=payload,
    )
    run.status = engine_status
    run.conclusion_structured = True
    run.active_key = None
    run.response_text = "报告正文"
    run.response_payload = "{}"
    run.finished_at = datetime.now(timezone.utc)
    db.session.commit()
    ai_service._update_weekly_state(payload, run, state, engine_status=engine_status)
    run_id = run.id
    db.session.commit()
    # **只把 id 交出去**：ORM 实例出了 app context 就是 detached（见 `_make_group`）。
    return run_id


def _second_group(first: dict) -> dict:
    """同一个项目里**另一个窗口**的配置（另一个分组）—— 用来量「近似有没有说出来」。"""
    now = datetime.now(timezone.utc)
    cfg = WeeklyVersionConfig(
        project_id=first["project_id"],
        repository_id=first["repo_id"],
        name=_uid("weekly-2"),
        branch="main",
        # 换一个窗口 → 换一个 group_key（分组按 project_id + 时间窗划）。
        start_time=now - timedelta(days=30),
        end_time=now - timedelta(days=8),
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.flush()
    db.session.add(
        WeeklyVersionDiffCache(
            config_id=cfg.id,
            repository_id=first["repo_id"],
            file_path="tables/e8_other_0.xlsx",
            file_type="excel",
            merged_diff_data="{}",
            base_commit_id="b" * 40,
            latest_commit_id="c0000",
            commit_count=1,
            cache_status="completed",
            diff_version="1.18.0",
        )
    )
    db.session.commit()
    group_key = ai_service.build_weekly_group_key(cfg)
    _CREATED["groups"].append(group_key)
    return {"project_id": first["project_id"], "repo_id": first["repo_id"],
            "cfg_id": cfg.id, "group_key": group_key}


def _login(client) -> None:
    with client.session_transaction() as session:
        session["is_admin"] = True
        session["admin_user"] = "e8-tester"
        session["_csrf_token"] = _uid("csrf")


def _post_job(client, config_id, **body):
    with client.session_transaction() as session:
        token = session["_csrf_token"]
    return client.post(
        f"/ai-analysis/weekly/{config_id}/jobs",
        json=body,
        headers={"X-CSRF-Token": token},
    )


def _runs_for(group) -> int:
    """按**分组键**过滤 —— 测试库是会话级共用的，全表 count 会「全量绿、单跑红」。"""
    return AiAnalysisRun.query.filter_by(target_key=group["group_key"]).count()


class TestTheFiveWeeklyPathsOverHttp:
    def test_path_1_a_plain_incremental_run_keeps_the_incremental_mode(self):
        """**普通增量**：有可复用基线 → 增量就是增量，不被升级。

        这条路径原先在界面上「一个字都不弹」就开跑（报告 §5.5）。服务端这一侧要能
        给出预检要摆的那三样事实（增量文件数 / 补偿文件数 / 基线 run），判据是
        job 行上的 `base_run_id` 指向**结论基线**那一条。
        """
        with app.app_context():
            create_tables()
            group = _make_group()
            baseline_run_id = _settle_and_advance(group)

        with app.test_client() as client:
            _login(client)
            body = _post_job(
                client, group["cfg_id"],
                analysis_mode="incremental", idempotency_key=_uid("k"),
            ).get_json()

        assert body["success"] is True, body
        assert body["requested_mode"] == "incremental"
        assert body["effective_mode"] == "incremental", body
        assert body["upgrade_reason"] in (None, ""), body
        assert body["job"]["base_run_id"] == baseline_run_id, body["job"]

    def test_the_precheck_endpoint_reports_the_three_facts_over_http(self):
        """**预检要摆的那三样，走 HTTP 拿得到**（E8 的前半句）。

        这条用例是**补上一个真被漏掉的分支**：`analysis_estimate` 里「状态行存在、
        `last_concluded_run_id` 有值」那一段原先一条测试都没走到 —— 于是里面一句
        `db.session.get(...)`（模块里根本没 import `db`）在静态检查里才露出来，
        而功能测试全绿。判据必须**同时**是「三个字段有值」与「值正确」：
        只断言键存在的话，`None` 也能通过。
        """
        with app.app_context():
            create_tables()
            group = _make_group(file_count=6)
            baseline_run_id = _settle_and_advance(group)
            # 让其中 2 个文件有了新提交 —— 本次的增量就是这 2 个。
            for index in (0, 1):
                row = WeeklyVersionDiffCache.query.filter_by(
                    config_id=group["cfg_id"], file_path=_PATH.format(index=index)
                ).one()
                row.latest_commit_id = f"d{index:04d}"
            db.session.commit()

        with app.test_client() as client:
            _login(client)
            body = client.get(
                f"/ai-analysis/usage/estimate?project={group['project_id']}"
                f"&config={group['cfg_id']}&mode=incremental"
            ).get_json()

        assert body["success"] is True, body
        assert body["delta_files"] == 2, body
        assert body["compensation_files"] == 0, body
        assert body["baseline_run"]["run_id"] == baseline_run_id, body["baseline_run"]
        assert body["baseline_run"]["created_at"], body["baseline_run"]
        assert body["baseline_run"]["scope"], body["baseline_run"]
        assert body["upgrade_reason"] == "delta_ratio_high", body
        # 这三项**不参与**区间折算：带上这句话，读的人才不会拿它当折扣。
        assert any("不参与" in note for note in body["notes"]), body["notes"]

    def test_it_says_which_group_the_precheck_facts_came_from(self):
        """多个分组时，预检要**说出**那份事实取自哪个窗口（近似不许被当成事实）。

        `/usage/estimate` 的参数表里没有 config（取值只认 project / mode / files /
        baseline），所以服务端只能按「最近更新过的状态行」挑分组。挑错在界面上表现为
        「预检摆的是别的窗口的基线 run」—— 一句带窗口的说明能让它当场被认出来。
        **只有一个分组时不许加那句话**（无话找话的说明会让真正的警告贬值）。
        """
        with app.app_context():
            create_tables()
            first = _make_group()
            first_run_id = _settle_and_advance(first)
            second = _second_group(first)
            _settle_and_advance(second)

        with app.test_client() as client:
            _login(client)
            body = client.get(
                f"/ai-analysis/usage/estimate?project={first['project_id']}&mode=incremental"
            ).get_json()
            exact = client.get(
                f"/ai-analysis/usage/estimate?project={first['project_id']}"
                f"&config={first['cfg_id']}&mode=incremental"
            ).get_json()

        ambiguity = [note for note in body["notes"] if "个周版本分组" in note]
        assert ambiguity, body["notes"]
        assert "窗口" in ambiguity[0], ambiguity
        assert exact["baseline_run"]["run_id"] == first_run_id, exact
        assert not [note for note in exact["notes"] if "个周版本分组" in note], exact["notes"]

    def test_path_2_a_stale_baseline_still_runs_incremental_when_the_user_insists(self):
        """**版本不匹配增量**：基线是旧规则产出的，用户仍选增量 → 照跑增量。

        平台**不许**替用户改成全量（那是他自己按下了那个选项），但**必须**让这件事
        在调用模型之前看得见：`/latest` 要如实带 `stale` / `stale_reason`，界面据此
        弹「继续增量 / 升级全量」二选一（见 `test_ai_dual_mode_and_estimate_ui` 那一组）。
        """
        with app.app_context():
            create_tables()
            group = _make_group()
            baseline_run_id = _settle_and_advance(group)
            # 把这份基线的溯源改坏一处 —— 「旧规则产出的结论」就是这一种形态。
            expected = current_provenance(group["project_id"])
            assert expected, "溯源指纹是空的，这条用例就没有意义了"
            baseline_row = db.session.get(AiAnalysisRun, baseline_run_id)
            baseline_row.rules_version = "old-rules-version"
            db.session.commit()

        with app.test_client() as client:
            _login(client)
            latest = client.get(
                f"/ai-analysis/weekly/{group["cfg_id"]}/latest"
            ).get_json()
            body = _post_job(
                client, group["cfg_id"],
                analysis_mode="incremental", idempotency_key=_uid("k"),
            ).get_json()

        assert latest["result"] is not None, latest
        assert latest["result"]["stale"] is True, latest["result"]
        assert latest["result"]["stale_reason"] == "rules_changed", latest["result"]
        assert body["effective_mode"] == "incremental", body
        assert body["job"]["base_run_id"] == baseline_run_id, body["job"]

    def test_path_3_a_full_run_is_full_and_says_it_has_no_upgrade_reason(self):
        """**全量**：用户自己要的，`effective_mode=full`，而且**没有**「升级原因」。

        给它编一个 `upgrade_reason` 的话，界面会用 `weeklyAiUpgradeNotice` 说一句
        「平台把这次增量升成了全量」—— 那是一次无话找话的假通知。
        """
        with app.app_context():
            create_tables()
            group = _make_group()

        with app.test_client() as client:
            _login(client)
            body = _post_job(
                client, group["cfg_id"],
                analysis_mode="full", idempotency_key=_uid("k"),
            ).get_json()

        assert body["requested_mode"] == "full", body
        assert body["effective_mode"] == "full", body
        assert body["upgrade_reason"] in (None, ""), body

    def test_path_4_a_second_click_attaches_to_the_same_job(self):
        """**已有任务附着**：同一个目标 + 同一份输入点两次，只跑一次、不花第二次钱。

        判据是**行数不变**且第二次拿到同一个 `job_id`（`attached=True`）—— 只看
        `success` 的话，「每次点击都建一条新 job」照样绿。
        """
        with app.app_context():
            create_tables()
            group = _make_group()
            _settle_and_advance(group)

        with app.test_client() as client:
            _login(client)
            first = _post_job(
                client, group["cfg_id"],
                analysis_mode="incremental", idempotency_key=_uid("k"),
            ).get_json()
            with app.app_context():
                from models.ai_analysis import AiAnalysisJob

                after_first = AiAnalysisJob.query.filter_by(
                    target_key=group["group_key"]
                ).count()
            second = _post_job(
                client, group["cfg_id"],
                analysis_mode="incremental", idempotency_key=_uid("k"),
            ).get_json()

        assert second["job_id"] == first["job_id"], (first, second)
        assert second["attached"] is True, second
        with app.app_context():
            from models.ai_analysis import AiAnalysisJob

            assert AiAnalysisJob.query.filter_by(
                target_key=group["group_key"]
            ).count() == after_first == 1

    def test_path_5_no_change_reuses_the_conclusion_and_creates_no_run(self):
        """**无变化复用**：没有新变化时不新建付费运行，job 按 `reused` 收口。

        这条走的是生产执行入口（`run_weekly_analysis_background`）与生产收口
        （`settle_job_from_result`），只在最后一步用 HTTP 读回结果 —— 中间没有
        「测试自己拼一个结局」的环节。
        """
        with app.app_context():
            create_tables()
            group = _make_group()
            baseline_run_id = _settle_and_advance(group)
            before = _runs_for(group)

        with app.test_client() as client:
            _login(client)
            created = _post_job(
                client, group["cfg_id"],
                analysis_mode="incremental", idempotency_key=_uid("k"),
            ).get_json()
            job_id = created["job_id"]
            with app.app_context():
                task_id = job_service.get_job(job_id).task_id

            with app.app_context():
                outcome = ai_service.run_weekly_analysis_background(
                    group["cfg_id"], task_id=task_id, trigger_source="manual"
                )
                task_handlers.settle_job_from_result(outcome, task_id=task_id)

            job = client.get(f"/ai-analysis/jobs/{job_id}").get_json()["job"]

        assert outcome["status"] == "skipped", outcome
        assert outcome["reason"] == "no_change", outcome
        assert outcome["reused_run_id"] == baseline_run_id, outcome
        with app.app_context():
            assert _runs_for(group) == before, "没有新变化却建了一条 run —— 白花一次调用"
        assert job["state"] == "reused", job
        assert job["reused_run_id"] == baseline_run_id, job
