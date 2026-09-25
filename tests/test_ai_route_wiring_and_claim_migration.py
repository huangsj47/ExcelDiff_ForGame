# -*- coding: utf-8 -*-
"""三处「必须由别人接线」的收尾：**报告导出接覆盖账本 / 跑中预算读 job 级累计 /
启动钩子接上认领列迁移**。

这三处有同一个形状：**新东西已经写好了（`coverage_for_run` / `snap.job_tokens` /
`migrations.ai_run_claim_columns.apply`），但没人把它们接上**。而三者的失败都很安静：

* 不接覆盖账本 → 报告照样导得出来，只是里面没有「覆盖与缺口」那一组 ——
  「分析范围：全量」继续被读成「整个版本都看过了」；
* 不接 job 级 token → 跑中预算**低估**：子代理模式下换个成员，那个数当场回退，
  「这次其实已经超了」的提示跟着消失；
* 不接认领迁移 → 老开发库上没有 UNIQUE 索引 → 撞不出 `IntegrityError` →
  并发幂等**静默失效**，行为退回改动前，而且没有任何报错。

前两条走真路由（`test_client` + 真登录），第三条在一张**临时 sqlite 库**上跑
启动期的 `apply_schema_migrations`（不动 `instance/diff_platform.db`）。
"""
from __future__ import annotations

import ast
import inspect as pyinspect
import json
import textwrap
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import app, create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace, AiPlatformBudget
from services import db_migration_service
from services.ai import report_document as doc
from services.ai import run_progress, verdict
from services.ai_analysis_service import update_project_analysis_config

# 一个全 40 位的提交号：白名单里存完整号，逐轮明细的标签里只有 12 位前缀。
LATEST = "f844faa6f59c3f1b571226e92ac03dbd22a6b790"

# 项目预算的上限。**刻意取在两帧之间**：换成员前 493,531（没超）、换成员后 528,491
# （超了）—— 这样「读的是 job 级还是当前成员」在界面上就是两个不同的结论。
TOKEN_LIMIT = 500_000


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _login(client, *, admin: bool = True):
    with client.session_transaction() as session:
        session["is_admin"] = admin
        session["admin_user"] = "route-wiring"
        session["_csrf_token"] = _uid("csrf")


@pytest.fixture(autouse=True)
def _clean_progress():
    """进度表是**进程内**的全局状态：不清干净，前一个用例的快照会让这一个的断言飘。"""
    run_progress.reset_for_tests()
    yield
    run_progress.reset_for_tests()


def _project() -> Project:
    project = Project(code=_uid("P"), name=_uid("route-wiring"))
    db.session.add(project)
    db.session.commit()
    return project


# ---------------------------------------------------------------------------
#  一、报告导出：接上覆盖账本
# ---------------------------------------------------------------------------
def _weekly_request_payload(*, files, window):
    """照 `build_weekly_payload` 落库的那种形状（只留账本要读的那几项）。"""
    entries = [{"file_path": path, "latest_commit_id": commit} for path, commit in files]
    return {
        "mode": "weekly",
        "scope": "full",
        "summary": {
            "total_files": window,
            "delta_files": len(entries),
            "batch_files": len(entries),
            "window_files": window,
        },
        "delta_files": entries,
        "list_files": [{"file_path": path} for path, _ in files],
        "policy": {"truncated": False, "truncation_reason": None},
        "focus": {"key": "all", "label": ""},
    }


def _run(
    project_id: int,
    *,
    response_text: str = "# 变更理解\n正文。\n",
    status: str = "succeeded",
    request_payload: dict | None = None,
    model: str = "fake-model",
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    created_at: datetime | None = None,
) -> AiAnalysisRun:
    stamp = created_at or datetime.now(timezone.utc)
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        status=status,
        scope="full",
        trigger_source="manual",
        started_at=stamp,
        # 还在跑的那条不写 `finished_at`（写了就不是「跑中」那条形状了）
        finished_at=None if status == "running" else stamp,
        created_at=stamp,
        response_text=response_text,
        response_payload=json.dumps({"risk_level": "high", "risk_reasons": ["x"]}),
        request_payload=(
            None
            if request_payload is None
            else json.dumps(request_payload, ensure_ascii=False)
        ),
        model=model,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
    )
    db.session.add(run)
    db.session.commit()
    return run


def _add_trace(run, executed: list[dict]) -> None:
    db.session.add(
        AiAnalysisTrace(
            run_id=run.id,
            round_index=1,
            executed_json=json.dumps({"items": len(executed), "details": executed}, ensure_ascii=False),
        )
    )
    db.session.commit()


def _fetched(kind: str, commit: str, path: str) -> dict:
    return {
        "kind": kind,
        "label": f"{kind} {commit[:12]} {path}",
        "chars": 1200,
        "failed": False,
        "empty": False,
    }


def test_the_exported_report_carries_the_coverage_ledger():
    """导出的 md 里要看得到「覆盖与缺口」——**这一行不接，报告里就一个字都没有**。

    构造：这个版本改过 10 个文件、其中 2 个进了本次输入、只有 1 个真的取到过证据。
    于是报告里必须同时出现「装了多少」「取到多少」「还缺什么」三件事 —— 只写覆盖率
    或者只写「分析范围：全量」，读者会把后者读成「整个版本都看过了」。
    """
    with app.app_context():
        create_tables()
        project = _project()
        run = _run(
            project.id,
            request_payload=_weekly_request_payload(
                files=[("a.lua", LATEST), ("b.lua", LATEST)], window=10
            ),
        )
        _add_trace(run, [_fetched("file_diff", LATEST, "a.lua")])

        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    # 元信息表里那一组覆盖行（措辞与数字都由账本给）
    assert "| 本次覆盖 |" in body, "账本没接上：报告里没有任何覆盖行"
    assert "版本 10 个文件 → 输入 2 → 取证 1" in body
    assert "| 覆盖（分层） |" in body, "分层覆盖那两个数也要摆出来"
    # 「覆盖与缺口」那一段（只在传了账本时才出现，标题本身也是判据）
    assert f"**{doc.COVERAGE_TITLE}**" in body, "缺口那一段没出现"
    assert "**没有取到证据**" in body, "有 1 个文件这次没看过，报告里却没说"


def test_the_exported_report_drops_the_machine_readable_ruling_block():
    """**历史数据**那一行引擎注释不许出现在下载的文件里（AI-P0-05）。

    新运行不再写这个块（裁决走 `EngineOutcome.verdict`），但库里已有的那几条会一直被
    读到 —— 导出这条路径仍然要摘掉它（`verdict.strip_ruling_block`，与清理脚本同一份
    正则）。正文一个字都不许因此少掉。
    """
    raw = "第一行结论。\n\n<!-- ai-verify-ruling: {\"changed\": 1, \"rows\": []} -->"
    assert verdict.RULING_BLOCK_MARKER in raw, "构造的正文里没有那个块，这条用例什么都没测"

    with app.app_context():
        create_tables()
        project = _project()
        run = _run(project.id, response_text=raw)

        with app.test_client() as client:
            _login(client)
            response = client.get(f"/ai-analysis/runs/{run.id}/report.md")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "第一行结论。" in body, "摘块把报告正文一起摘掉了"
    assert verdict.RULING_BLOCK_MARKER not in body, "下载的 md 里还留着那一行机器注释"


# ---------------------------------------------------------------------------
#  二、跑中预算：读 **job 级**累计（不是当前成员的局部量）
# ---------------------------------------------------------------------------
class _Frame:
    """`RoundProgress` 的最小替身（字段与 `services/ai/engine.py` 的一致）。"""

    def __init__(self, **kwargs):
        self.agent = kwargs.get("agent", "")
        self.agent_index = kwargs.get("agent_index", 0)
        self.agent_total = kwargs.get("agent_total", 0)
        self.index = kwargs.get("index", 1)
        self.max_rounds = kwargs.get("max_rounds", 8)
        self.status = kwargs.get("status", "requests")
        self.prompt_tokens = kwargs.get("prompt_tokens", 100)
        self.completion_tokens = kwargs.get("completion_tokens", 20)
        self.cache_read_tokens = kwargs.get("cache_read_tokens", None)
        self.cache_write_tokens = kwargs.get("cache_write_tokens", None)
        self.requests_used = kwargs.get("requests_used", 3)
        self.requests_remaining = kwargs.get("requests_remaining", 17)
        self.items_chars = kwargs.get("items_chars", 4000)
        self.elapsed_ms = kwargs.get("elapsed_ms", 1200)


def test_the_progress_endpoint_reads_the_job_level_tokens_not_the_current_member():
    """跑中预算那一档取的是**跨成员累计**，不是「当前成员」的局部量。

    这是「轮询头上那句『已超』在换成员后消失」的成因：子代理模式下每个分片各起一个
    引擎，`live_tokens` 换成员就从 0 重新数，而**预算判的是整次分析**。

    构造：分片 S1 报 493,531（没超 500,000）→ 换到汇总时它只报 34,960。
    读 job 级 → 528,491，**超了**；读局部量 → 34,960，提示消失。落库那一档是空的
    （这条运行还在跑），所以两个读法在界面上就是两个完全不同的结论。
    """
    with app.app_context():
        create_tables()
        # 平台档没有项目过滤（它算的是所有项目之和），会话共用的测试库里可能留着别的
        # 用例设过的上限 —— 那会让 `over` 因为平台档而成立，这条用例就测不到项目档了。
        for row in AiPlatformBudget.query.all():
            db.session.delete(row)
        db.session.commit()

        project = _project()
        ok, message, errors = update_project_analysis_config(
            project.id,
            {"budget_period": "all_time", "budget_token_limit": TOKEN_LIMIT},
            updated_by="route-wiring",
        )
        assert ok, (message, errors)
        run = _run(project.id, status="running")

        # 分片 S1 跑完：局部 493,531 = job 493,531（还没换过成员，两个量一样）
        run_progress.publish(
            run.id,
            project.id,
            _Frame(agent="S1", agent_index=1, agent_total=2, index=1,
                   prompt_tokens=473_531, completion_tokens=20_000),
        )
        with app.test_client() as client:
            _login(client)
            before = client.get(f"/ai-analysis/runs/{run.id}/progress").get_json()

        # 换到汇总（`agent` 为空 + `agent_index` 变了）：局部量从 0 重新开始
        run_progress.publish(
            run.id,
            project.id,
            _Frame(agent="", agent_index=2, agent_total=2, index=1,
                   prompt_tokens=30_000, completion_tokens=4_960),
        )
        with app.test_client() as client:
            _login(client)
            after = client.get(f"/ai-analysis/runs/{run.id}/progress").get_json()

    assert before["budget"]["used"]["tokens"] == 493_531
    assert before["budget"]["over"] is False, before["budget"]["reason"]

    # 快照照旧给「当前成员」的局部量：两个字段不许互相顶替（界面按各自的含义读）
    assert after["progress"]["live_tokens"] == 34_960, after["progress"]["live_tokens"]
    assert after["progress"]["job_tokens"] == 528_491

    assert after["budget"]["used"]["tokens"] == 528_491, (
        "跑中预算读的是当前成员的局部量 —— 换成员后这个数会当场回退"
    )
    assert after["budget"]["over"] is True, "整次分析已经超了，界面上却什么都没说"
    assert "tokens" in after["budget"]["over_limits"]
    assert "含本次运行" in after["budget"]["reason"], after["budget"]["reason"]


def test_the_progress_endpoint_still_counts_the_live_cost():
    """换读 job 级 token **不许**把费用那一档少算 —— 那一档仍按本次运行已跑完的轮次算。

    快照里分得出输入 / 输出两个分量的只有「当前成员」这一份（job 级那一本账只记总数），
    所以费用取 `snap.prompt_tokens` / `completion_tokens`：那是这个成员**已跑完的轮次**
    的累计（与 token 那一档同一条口径）。

    两个构造上的讲究：

    * 库里**要有一条费用算得出来的已落库运行**：`with_live_usage` 的费用分支要先有一个
      可加的基数（`used.cost_exact`），一条都算不出时它按 `_cost_totals` 的口径整档不判
      —— 那是「部分算得出 = 一个偏小的假数字」那条纪律，不是这次改动引入的；
    * 那条**正在跑**的运行放在本周期窗口之外：它自己还没上报 token，落在窗口里会让整档
      变成「算不出」。
    """
    price_table = json.dumps(
        {"version": "t-1", "currency": "CNY", "models": {"fake-model": {"input": 2.0, "output": 8.0}}}
    )
    with app.app_context():
        create_tables()
        for row in AiPlatformBudget.query.all():
            db.session.delete(row)
        db.session.commit()

        project = _project()
        ok, message, errors = update_project_analysis_config(
            project.id,
            {
                "budget_period": "weekly",
                "budget_token_limit": 10_000_000,   # token 那一档不参与，只看费用
                "budget_cost_limit": "0.5",
                "model_price_table": price_table,
            },
            updated_by="route-wiring",
        )
        assert ok, (message, errors)
        # 本周跑完的那一次：本周期已落库的费用（0.0036 元），是这一档的基数
        _run(project.id, status="succeeded", tokens_input=1_000, tokens_output=200)
        run = _run(
            project.id,
            status="running",
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )

        run_progress.publish(
            run.id,
            project.id,
            _Frame(agent="S1", agent_index=1, agent_total=2, index=1,
                   prompt_tokens=473_531, completion_tokens=20_000),
        )
        with app.test_client() as client:
            _login(client)
            payload = client.get(f"/ai-analysis/runs/{run.id}/progress").get_json()

    budget = payload["budget"]
    assert "cost" in budget["over_limits"], budget["reason"]
    assert "含本次运行" in budget["reason"], budget["reason"]
    assert budget["used"]["cost_exact"] not in (None, "", "0"), budget["used"]


# ---------------------------------------------------------------------------
#  三、认领列迁移：接到启动钩子上（临时库上验）
# ---------------------------------------------------------------------------
_OLD_RUN_DDL = """
CREATE TABLE ai_analysis_run (
    id INTEGER NOT NULL PRIMARY KEY,
    project_id INTEGER NOT NULL,
    target_type VARCHAR(20),
    target_key VARCHAR(200),
    status VARCHAR(20),
    created_at DATETIME
)
"""


class _DbStub:
    """迁移只用 `db.engine` 与 `db.session`，所以给一个最小的替身。

    用替身而不是给 Flask-SQLAlchemy 的 `db` 换引擎：后者的引擎在创建时就绑死了，
    把测试指向真实库再删列会污染整个会话共用的隔离库。
    """

    def __init__(self, engine):
        self.engine = engine
        self.session = Session(engine)


def _sqlite_master(stub, *, kind: str) -> list[tuple]:
    with stub.engine.begin() as connection:
        return list(
            connection.exec_driver_sql(
                f"SELECT name, sql FROM sqlite_master WHERE type = '{kind}'"
                " AND tbl_name = 'ai_analysis_run'"
            ).fetchall()
        )


def test_the_claim_migration_is_wired_into_startup():
    """光有 `migrations/ai_run_claim_columns.py` 不算数，必须挂在启动钩子上。

    漏接的后果不是报错而是**静默**：老库上不会有那一列与唯一索引，写入侧的
    `except IntegrityError` 永远撞不到东西 —— 并发幂等退回改动前，日志里一个字都没有。

    按**语法树**认「真的调了」而不是在源码里找那个名字：把那一行注释掉之后，字符串
    断言照样绿（注释里就有那个名字），而那正是漏接的样子。
    """
    source = textwrap.dedent(
        pyinspect.getsource(db_migration_service.apply_schema_migrations)
    )
    called = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_migrate_ai_run_claim_columns" in called, (
        "认领列迁移没有接进 apply_schema_migrations（注释掉不算接上）"
    )
    assert callable(db_migration_service.apply_ai_run_claim_columns)


def test_apply_schema_migrations_builds_the_claim_column_and_unique_index(tmp_path):
    """在一张**临时**的「迁移前」库上跑启动钩子：列与唯一索引真的建出来了。

    断言的是三件事：① 重复执行不报错（它每次启动都会跑一遍）；② `active_key` 上有
    **UNIQUE**（不是普通索引）—— 幂等靠的就是它；③ 同一个 `active_key` 插两次真的撞
    `IntegrityError`（这是「那道约束是幂等的地基」的实证，不是只看名字在不在）。
    """
    engine = create_engine(f"sqlite:///{(tmp_path / 'old_claim.db').as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(_OLD_RUN_DDL)

    stub = _DbStub(engine)
    messages: list[str] = []

    def _log(*args, **_kwargs):
        messages.append(" ".join(str(arg) for arg in args))

    try:
        db_migration_service.apply_schema_migrations(stub, _log)
        columns = {col["name"] for col in inspect(engine).get_columns("ai_analysis_run")}
        assert {"active_key", "degradation"}.issubset(columns), columns

        # 第二遍：启动时每次都会跑，重复执行必须无害
        messages.clear()
        db_migration_service.apply_schema_migrations(stub, _log)
        failures = [message for message in messages if "失败" in message]
        assert not failures, f"第二遍迁移报了失败：{failures}"

        indexes = {name: sql for name, sql in _sqlite_master(stub, kind="index")}
        assert "uq_ai_run_active_key" in indexes, indexes
        assert "UNIQUE" in (indexes["uq_ai_run_active_key"] or "").upper(), indexes

        # 那道 UNIQUE 真的拦得住并发认领（NULL 在 SQLite 里互不冲突，所以用同一个值）
        stub.session.execute(
            sa_text(
                "INSERT INTO ai_analysis_run (id, project_id, active_key)"
                " VALUES (1, 1, 'same-input')"
            )
        )
        stub.session.commit()
        with pytest.raises(IntegrityError):
            stub.session.execute(
                sa_text(
                    "INSERT INTO ai_analysis_run (id, project_id, active_key)"
                    " VALUES (2, 1, 'same-input')"
                )
            )
            stub.session.commit()
        stub.session.rollback()
    finally:
        stub.session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
#  四、一次性数据清理：把历史行里那段机器 json 擦掉（AI-P0-05）
# ---------------------------------------------------------------------------
# 库里已经落下的那几行（实测 run 15 / run 20）会一直被读到 —— 抽屉、历史列表、导出。
# 新代码不再写它，但**已经写进去的**得有人擦。脚本本身很简单，容易出错的是它周边的
# 三条约束：默认不写库、幂等、只动那两列。三条都在这里钉住。

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "clean_ruling_block_from_runs.py"
)
BLOCK = (
    '<!-- ai-verify-ruling: {"verdicts_seen": 1, "rows": [{"finding_id": "F1"}]} -->'
)


def _load_cleanup_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("clean_ruling_block", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _temp_run_db(tmp_path) -> str:
    """一个只带 `ai_analysis_run` 那三列的小库（不碰 instance/ 那个真库）。"""
    import sqlite3

    path = tmp_path / "runs.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ai_analysis_run (id INTEGER PRIMARY KEY, response_text TEXT,"
        " response_payload TEXT)"
    )
    payload = json.dumps(
        {
            "risk_level": "high",
            "report_markdown": f"# 报告\n\n第一行结论。\n\n{BLOCK}",
            "final_findings": [{"finding_id": "F1", "active": False}],
        },
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT INTO ai_analysis_run (id, response_text, response_payload) VALUES (?, ?, ?)",
        (1, f"# 报告\n\n第一行结论。\n\n{BLOCK}", payload),
    )
    # 一条干净的运行：脚本不许碰它（幂等的另一半）。
    clean_payload = json.dumps(
        {"risk_level": "low", "report_markdown": "# 报告\n\n没问题。\n"}, ensure_ascii=False
    )
    conn.execute(
        "INSERT INTO ai_analysis_run (id, response_text, response_payload) VALUES (?, ?, ?)",
        (2, "# 报告\n\n没问题。\n", clean_payload),
    )
    conn.commit()
    conn.close()
    return str(path)


def _read_rows(db_path: str) -> dict:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT id, response_text, response_payload FROM ai_analysis_run"
            )
        }
    finally:
        conn.close()


def test_the_cleanup_script_does_not_touch_the_db_without_apply(tmp_path):
    """**默认只打印。** 生产库上可能正跑着真实分析 —— 「跑一下看看」不能改别人的数据。

    这里断言的是**库没变**而不是打了什么字：本项目 `pytest.ini` 配了 `-s`（capture
    关着），stdout 断言在别的运行方式下会变成永远为真的空检查。要读「还剩几条」就调
    脚本自己的查询函数（`_rows_needing_cleanup`）——那是它真正的判据。
    """
    module = _load_cleanup_script()
    db_path = _temp_run_db(tmp_path)
    before = _read_rows(db_path)

    assert module.main(["--db", db_path]) == 0

    assert _read_rows(db_path) == before, "没加 --apply 却写库了"
    assert len(_rows_with_block(module, db_path)) == 1, "预览模式该识别出这一条要清"


def test_the_cleanup_script_strips_both_columns_and_is_idempotent(tmp_path):
    module = _load_cleanup_script()
    db_path = _temp_run_db(tmp_path)

    assert module.main(["--db", db_path, "--apply"]) == 0
    text, payload = _read_rows(db_path)[1]
    assert "ai-verify-ruling" not in text
    assert "第一行结论。" in text, "摘块把正文一起摘掉了"
    assert "ai-verify-ruling" not in payload
    # 摘块顺带把正文末尾的空白去掉（`strip_ruling_block` 的既有行为，导出也用它）。
    assert json.loads(payload)["report_markdown"] == "# 报告\n\n第一行结论。"
    # **结论本体一个字节都不许动**：清的是副本，不是数据。
    assert json.loads(payload)["final_findings"] == [{"finding_id": "F1", "active": False}]
    # 干净的那一行原样不动。
    assert _read_rows(db_path)[2] == (
        "# 报告\n\n没问题。\n",
        json.dumps(
            {"risk_level": "low", "report_markdown": "# 报告\n\n没问题。\n"},
            ensure_ascii=False,
        ),
    )

    # 幂等：第二次跑之后「需要清理的行」还是 0 条，而不是越跑越少。
    after_first = _read_rows(db_path)
    assert module.main(["--db", db_path, "--apply"]) == 0
    assert _rows_with_block(module, db_path) == []
    assert _read_rows(db_path) == after_first, "第二次跑动了不该动的行"


def test_the_cleanup_script_refuses_a_missing_database(tmp_path):
    """库文件不存在时报错退出 —— `sqlite3.connect` 会顺手建一个空库，那比什么都不做更糟。"""
    module = _load_cleanup_script()
    missing = tmp_path / "nope.db"

    assert module.main(["--db", str(missing)]) == 2
    assert not missing.exists(), "脚本把一个不存在的库「建」了出来"


def _rows_with_block(module, db_path: str) -> list:
    """脚本自己的「还有哪些行要清」查询（用它判幂等，不解析 stdout）。"""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return module._rows_needing_cleanup(conn.cursor())
    finally:
        conn.close()
