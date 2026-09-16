# -*- coding: utf-8 -*-
"""commits_log 索引补齐 + update_commit_fields 批量更新。

## 为什么需要这个测试

### 1. commits_log 的索引
`commits_log` 是全平台最大的表（提交列表、diff、周版本、缓存回查全都按
`repository_id` / `commit_id` / `commit_time` / `status` 过滤），但它在
`models/commit.py` 里**一个 `Index(...)` 都没有**，而同项目的
`AgentTask.__table_args__` 已经定义了 4 个。结果是这些查询全部退化成全表扫描。

更隐蔽的是：光在模型里补索引**只对新建的库生效** —— `db.create_all()` 用
`checkfirst=True`，表已存在就直接跳过，索引不会补建；而 SQLite 又不支持
`ALTER TABLE ... ADD CONSTRAINT`。所以 `services/db_migration_service.py`
必须自己发 `CREATE INDEX`，老库才拿得到索引。这两件事（模型层定义、迁移层补建）
本文件都要钉住。

### 2. update_commit_fields 的写法
原实现把 `version`/`operation` 为 NULL 的行**全部 SELECT 出来**，在 Python 里
for 循环逐行赋值（每行都被实例化成 ORM 对象，代价随行数线性增长）。
实测 20 万行：旧写法 4.88s / Python 堆峰值 578MB，新写法 0.13s / ≈0MB。
现在改成两条批量 `UPDATE`（表达式下推到数据库）。

## 变红意味着什么

* 索引用例变红 → 要么模型里的 `__table_args__` 被删/改错列，要么迁移层的
  `CREATE INDEX` 没真正执行 —— **已部署的老库会继续全表扫描**，是线上事故级别的回归。
* 幂等用例变红 → 每次启动都会报错（SQLite 重跑报错 / MySQL 1061 没被吞掉），
  启动链路会挂。
* 批量更新用例变红 → 数据被改错（覆盖了非 NULL 的值、空 commit_id 没写成
  'unknown'、返回条数语义变了），或者又退回了「把行拉进 Python」的老写法。
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, inspect, or_
from sqlalchemy import text as sa_text

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models import Commit  # noqa: E402
from services import db_migration_service  # noqa: E402

# 模型层与迁移层必须同时具备的索引：(索引名, 表名, 列元组)
REQUIRED_INDEXES = (
    ("idx_commits_log_repo_commit_time", "commits_log", ("repository_id", "commit_time")),
    ("idx_commits_log_repo_commit_id", "commits_log", ("repository_id", "commit_id")),
    ("idx_commits_log_repo_status", "commits_log", ("repository_id", "status")),
    ("idx_commits_log_commit_time", "commits_log", ("commit_time",)),
    ("idx_background_tasks_status_priority", "background_tasks", ("status", "priority", "created_at")),
    ("idx_background_tasks_type_repo_status", "background_tasks", ("task_type", "repository_id", "status")),
)

COMMITS_LOG_INDEXES = tuple(item for item in REQUIRED_INDEXES if item[1] == "commits_log")


def _noop_log(*_args, **_kwargs):
    return None


def _indexes_of(engine, table_name):
    """返回 {索引名: 列名元组}，后端无关（不写死 PRAGMA）。"""
    return {
        index["name"]: tuple(index.get("column_names") or ())
        for index in inspect(engine).get_indexes(table_name)
    }


@pytest.fixture()
def real_db():
    """真实应用 + conftest 隔离出来的临时 sqlite 库。"""
    import app as app_module

    with app_module.app.app_context():
        app_module.db.create_all()
        yield app_module.db


@pytest.fixture()
def seeded_repo():
    """建一个项目 + 仓库，供插入 commits_log 行使用。

    每次用唯一 code / 仓库名：conftest 的隔离库是整个会话共用的，
    固定值会撞 UNIQUE 约束。teardown 只删自己造的行。
    """
    import app as app_module
    from models import Project, Repository

    db = app_module.db
    tag = uuid.uuid4().hex[:8]

    with app_module.app.app_context():
        db.create_all()
        project = Project(code=f"IDX{tag}", name="索引测试项目", department="QA")
        db.session.add(project)
        db.session.commit()

        repo = Repository(
            project_id=project.id,
            name=f"idx_repo_{tag}",
            type="git",
            url="https://example.invalid/idx.git",
            resource_type="table",
        )
        db.session.add(repo)
        db.session.commit()

        try:
            yield {"project_id": project.id, "repository_id": repo.id}
        finally:
            try:
                Commit.query.filter_by(repository_id=repo.id).delete(synchronize_session=False)
                Repository.query.filter_by(id=repo.id).delete(synchronize_session=False)
                Project.query.filter_by(id=project.id).delete(synchronize_session=False)
                db.session.commit()
            except Exception:  # pragma: no cover - teardown 尽力而为
                db.session.rollback()


def _add_commits(db, repository_id, specs):
    """specs: [(commit_id, version, operation), ...]"""
    for commit_id, version, operation in specs:
        db.session.add(
            Commit(
                repository_id=repository_id,
                commit_id=commit_id,
                path="config/test.xlsx",
                version=version,
                operation=operation,
                commit_time=datetime(2026, 3, 1, 12, 0, 0),
                status="pending",
            )
        )
    db.session.commit()


def _call_update_commit_fields_route():
    """绕过 require_admin（权限另有测试覆盖），直接调被装饰的函数体。"""
    import app as app_module
    import services.commit_operation_handlers as handlers

    with app_module.app.test_request_context("/update_commit_fields", method="POST"):
        result = handlers.update_commit_fields_route.__wrapped__()

    if isinstance(result, tuple):
        _response, status_code = result
        return status_code, result[0].get_json()
    return result.status_code, result.get_json()


# ---------------------------------------------------------------------------
# 一、索引：模型层定义
# ---------------------------------------------------------------------------
class TestCommitIndexDefinitions:
    def test_commit_model_declares_required_indexes(self):
        """commits_log 的模型必须声明这 4 个索引。

        变红 = 有人删掉了 __table_args__ 或改错了列 —— 新建的库会重新变成无索引，
        列表查询从 ~0.3ms/a 退化成百毫秒级全表扫描。
        """
        declared = {
            index.name: tuple(col.name for col in index.columns)
            for index in Commit.__table__.indexes
        }
        for name, _table, columns in COMMITS_LOG_INDEXES:
            assert name in declared, f"模型缺少索引 {name}"
            assert declared[name] == columns, (
                f"索引 {name} 的列不对: 期望 {columns}, 实际 {declared[name]}"
            )

    def test_background_task_model_declares_required_indexes(self):
        """background_tasks 的队列表索引也必须存在（status+priority 是 worker 热路径）。"""
        from models import BackgroundTask

        declared = {
            index.name: tuple(col.name for col in index.columns)
            for index in BackgroundTask.__table__.indexes
        }
        for name, _table, columns in REQUIRED_INDEXES:
            if _table != "background_tasks":
                continue
            assert name in declared, f"模型缺少索引 {name}"
            assert declared[name] == columns, (
                f"索引 {name} 的列不对: 期望 {columns}, 实际 {declared[name]}"
            )

    def test_indexes_materialize_into_a_fresh_database(self, tmp_path):
        """在全新库里 create_all 必须真的把索引建出来（不只是元数据里声明）。

        变红 = __table_args__ 写法有问题（例如把 Index 加到了错误的类上），
        新部署的库拿不到索引。
        """
        import app as app_module

        fresh_path = tmp_path / "fresh_index_probe.db"
        engine = create_engine(f"sqlite:///{str(fresh_path).replace(os.sep, '/')}")
        try:
            app_module.db.metadata.create_all(engine)
            commit_indexes = _indexes_of(engine, "commits_log")
            for name, _table, columns in COMMITS_LOG_INDEXES:
                assert name in commit_indexes, f"全新库缺少索引 {name}: {sorted(commit_indexes)}"
                assert commit_indexes[name] == columns

            task_indexes = _indexes_of(engine, "background_tasks")
            assert "idx_background_tasks_status_priority" in task_indexes
            assert "idx_background_tasks_type_repo_status" in task_indexes
        finally:
            engine.dispose()

    def test_model_indexes_and_migration_constants_agree(self):
        """模型层清单与迁移层常量必须逐项一致。

        两边各管一半：模型管新库（create_all），迁移管老库（CREATE INDEX）。
        只改一边会让新库和老库的索引不一致，且这种漂移不会有任何报错。
        """
        declared = {
            (index.name, index.table.name, tuple(col.name for col in index.columns))
            for index in Commit.__table__.indexes
        }
        from models import BackgroundTask

        declared |= {
            (index.name, index.table.name, tuple(col.name for col in index.columns))
            for index in BackgroundTask.__table__.indexes
        }
        for expected in REQUIRED_INDEXES:
            assert expected in declared, f"模型层没有声明 {expected}"

        assert set(db_migration_service.REQUIRED_INDEXES) == set(REQUIRED_INDEXES), (
            "db_migration_service.REQUIRED_INDEXES 与模型层索引清单不一致"
        )


# ---------------------------------------------------------------------------
# 二、索引：迁移层（老库补建 + 幂等）
# ---------------------------------------------------------------------------
class TestIndexMigration:
    def test_migration_creates_missing_indexes_on_real_db(self, real_db):
        """真实库上跑一次迁移后，关键索引必须都在。

        变红 = 老库升级后拿不到索引（迁移没跑 / DDL 报错被吞了）。
        """
        db_migration_service.apply_index_migrations(real_db, _noop_log)
        existing = _indexes_of(real_db.engine, "commits_log")
        for name, _table, columns in COMMITS_LOG_INDEXES:
            assert name in existing, f"迁移后仍缺少索引 {name}: {sorted(existing)}"
            assert existing[name] == columns

    def test_migration_is_idempotent_and_survives_existing_data(self, real_db, seeded_repo):
        """在已有数据的库上，连跑两次迁移都不能报错，且索引最终都在。

        这是最关键的一条：老库升级时 commits_log 里已经有几十万行，
        迁移必须（a）能在有数据的表上建索引；（b）重复执行不炸
        —— SQLite 靠 CREATE INDEX IF NOT EXISTS，MySQL 靠吞掉 1061。
        变红 = 每次启动都会在迁移阶段抛异常。
        """
        repository_id = seeded_repo["repository_id"]
        # 先把索引全删掉，模拟「一个从没建过索引的老库」
        for name, table, _columns in REQUIRED_INDEXES:
            if table != "commits_log":
                continue
            real_db.session.execute(sa_text(f"DROP INDEX IF EXISTS {name}"))
        real_db.session.commit()
        remaining = _indexes_of(real_db.engine, "commits_log")
        for name, _table, _columns in COMMITS_LOG_INDEXES:
            assert name not in remaining, f"准备阶段没能删掉 {name}"

        _add_commits(
            real_db,
            repository_id,
            [(f"c{i:07d}{uuid.uuid4().hex[:8]}", None, None) for i in range(200)],
        )

        db_migration_service.apply_index_migrations(real_db, _noop_log)
        first_pass = _indexes_of(real_db.engine, "commits_log")

        # 第二次：幂等，不能抛异常，索引集合不能变
        db_migration_service.apply_index_migrations(real_db, _noop_log)
        second_pass = _indexes_of(real_db.engine, "commits_log")

        for name, _table, columns in COMMITS_LOG_INDEXES:
            assert name in first_pass, f"有数据的库上没建出索引 {name}"
            assert first_pass[name] == columns
        assert second_pass == first_pass, "第二次迁移改变了索引集合"

        # 数据本身不能被动到
        assert Commit.query.filter_by(repository_id=repository_id).count() == 200

    def test_ddl_uses_if_not_exists_on_sqlite_and_plain_ddl_on_mysql(self):
        """DDL 分支必须按后端选对写法。

        SQLite 支持 `CREATE INDEX IF NOT EXISTS`；MySQL（含 8.0）**不支持**，
        写了就是 1064 语法错误。变红 = MySQL 部署会因为语法错误起不来。
        """
        sqlite_ddl = db_migration_service._build_create_index_sql(
            "sqlite", "commits_log", "idx_x", ("repository_id", "commit_time")
        )
        assert sqlite_ddl == (
            "CREATE INDEX IF NOT EXISTS idx_x ON commits_log (repository_id, commit_time)"
        )

        mysql_ddl = db_migration_service._build_create_index_sql(
            "mysql", "commits_log", "idx_x", ("repository_id", "commit_time")
        )
        assert "IF NOT EXISTS" not in mysql_ddl
        assert mysql_ddl == "CREATE INDEX idx_x ON commits_log (repository_id, commit_time)"

    def test_mysql_duplicate_index_error_is_treated_as_success(self):
        """MySQL 1061（ER_DUP_KEYNAME）必须被当成「已存在」而不是失败。

        迁移里 MySQL 走的是「照建 + 吞 1061」。这里用一个假异常验证识别逻辑，
        顺带验证普通错误不会被误判成幂等路径。
        """
        from sqlalchemy.exc import OperationalError, ProgrammingError

        duplicate = ProgrammingError(
            "CREATE INDEX ...", {}, Exception(1061, "Duplicate key name 'idx_x'")
        )
        assert db_migration_service._is_index_already_exists_error(duplicate) is True

        by_message = OperationalError(
            "CREATE INDEX ...", {}, Exception(1061, "Duplicate key name 'idx_x'")
        )
        assert db_migration_service._is_index_already_exists_error(by_message) is True

        unrelated = OperationalError(
            "CREATE INDEX ...", {}, Exception(1146, "Table 'x.commits_log' doesn't exist")
        )
        assert db_migration_service._is_index_already_exists_error(unrelated) is False

    def test_apply_schema_migrations_also_runs_index_migration(self, real_db, monkeypatch):
        """入口函数 apply_schema_migrations 必须把索引迁移带上。

        变红 = 有人新增了一个 _migrate_* 但忘了在入口里调用索引迁移，
        老库永远不会补索引（这正是本次要修的缺陷本身）。
        """
        called = {"count": 0}
        original = db_migration_service.apply_index_migrations

        def _spy(db, log_print):
            called["count"] += 1
            return original(db, log_print)

        monkeypatch.setattr(db_migration_service, "apply_index_migrations", _spy)
        db_migration_service.apply_schema_migrations(real_db, _noop_log)
        assert called["count"] == 1


# ---------------------------------------------------------------------------
# 三、update_commit_fields 批量更新
# ---------------------------------------------------------------------------
class TestBulkUpdateCommitFields:
    def test_backfills_only_null_rows(self, real_db, seeded_repo):
        """NULL 的补上、非 NULL 的原封不动。

        变红 = 批量 UPDATE 的 WHERE 条件写错（例如漏了 IS NULL），
        把用户已经确认过的 version/operation 覆盖掉 —— 数据损坏。
        """
        repository_id = seeded_repo["repository_id"]
        _add_commits(
            real_db,
            repository_id,
            [
                ("abcdef1234567890", None, None),   # 两列都缺
                ("beefdead12345678", "v1", None),   # 只缺 operation
                ("cafebabe12345678", None, "A"),    # 只缺 version
                ("deadbeef12345678", "v2", "M"),    # 都不缺 → 必须原封不动
            ],
        )

        status_code, payload = _call_update_commit_fields_route()
        assert status_code == 200
        assert payload["success"] is True

        rows = {
            row.commit_id: (row.version, row.operation)
            for row in Commit.query.filter_by(repository_id=repository_id).all()
        }
        assert rows["abcdef1234567890"] == ("abcdef12", "M")
        assert rows["beefdead12345678"] == ("v1", "M")
        assert rows["cafebabe12345678"] == ("cafebabe", "A")
        assert rows["deadbeef12345678"] == ("v2", "M"), "非 NULL 的值被覆盖了"

    def test_empty_commit_id_becomes_unknown(self, real_db, seeded_repo):
        """commit_id 为空串时 version 必须写 'unknown'（保持与 Python 版切片一致）。

        原实现是 `commit.commit_id[:8] if commit.commit_id else 'unknown'`，
        空串是 falsy → 'unknown'。裸写 substr(commit_id, 1, 8) 会得到 ''，
        变红说明 CASE 分支没兜住空串。
        """
        repository_id = seeded_repo["repository_id"]
        _add_commits(real_db, repository_id, [("", None, "M")])

        status_code, _payload = _call_update_commit_fields_route()
        assert status_code == 200

        row = Commit.query.filter_by(repository_id=repository_id).one()
        assert row.version == "unknown"
        assert row.operation == "M"

    def test_returned_count_keeps_or_semantics(self, real_db, seeded_repo):
        """返回条数必须仍是「version IS NULL **或** operation IS NULL」的行数。

        原实现统计的是筛选出的行数（两列都缺也只算 1 条），不是两条 UPDATE
        rowcount 的简单相加（那样两列都缺的行会被算 2 次）。变红 = 前端看到的
        「成功更新 N 条」数字被改了，用户对不上账。
        """
        repository_id = seeded_repo["repository_id"]
        predicate = or_(Commit.version.is_(None), Commit.operation.is_(None))
        baseline = real_db.session.query(func.count(Commit.id)).filter(predicate).scalar() or 0

        specs = [
            ("1111111111111111", None, None),   # 两列都缺 → OR 计数算 1
            ("2222222222222222", None, None),   # 两列都缺 → OR 计数算 1
            ("3333333333333333", "v1", None),
            ("4444444444444444", "v2", "M"),
        ]
        _add_commits(real_db, repository_id, specs)

        expected = baseline + 3  # 两列都缺的那两条各自只算 1
        assert (
            real_db.session.query(func.count(Commit.id)).filter(predicate).scalar() or 0
        ) == expected

        status_code, payload = _call_update_commit_fields_route()
        assert status_code == 200
        assert payload["updated_count"] == expected, (
            f"返回条数应为 OR 语义的 {expected}, 实际 {payload['updated_count']}"
        )
        assert f"成功更新 {expected} 条提交记录" == payload["message"]

    def test_second_run_is_a_noop_and_reports_zero(self, real_db, seeded_repo):
        """再跑一次应该是 0 条（幂等），且没有任何行被改动。

        变红 = 批量 UPDATE 的过滤条件失效，会把整表反复重写。
        """
        repository_id = seeded_repo["repository_id"]
        _add_commits(
            real_db,
            repository_id,
            [("aaaa111122223333", None, None), ("bbbb111122223333", "v1", "M")],
        )

        status_code, _payload = _call_update_commit_fields_route()
        assert status_code == 200
        after_first = {
            row.commit_id: (row.version, row.operation)
            for row in Commit.query.filter_by(repository_id=repository_id).all()
        }

        _status_code, payload = _call_update_commit_fields_route()
        assert payload["updated_count"] == 0

        after_second = {
            row.commit_id: (row.version, row.operation)
            for row in Commit.query.filter_by(repository_id=repository_id).all()
        }
        assert after_second == after_first

    def test_bulk_update_does_not_load_rows_into_python(self, real_db, seeded_repo):
        """实现必须走批量 UPDATE，不能退回「SELECT 出行再 for 循环」。

        用 SQLAlchemy 的 before_cursor_execute 事件把这次调用真正发出去的 SQL
        全部录下来：应当恰好有 2 条 UPDATE，且**没有任何**把 commits_log 整行
        查出来的 ORM 实体 SELECT。
        变红 = 又有人把 ORM 逐行赋值写回来了（20 万行 ≈ 4.9s / Python 堆峰值 578MB）。
        """
        from sqlalchemy import event

        statements = []

        def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        engine = real_db.engine
        event.listen(engine, "before_cursor_execute", _capture)
        try:
            repository_id = seeded_repo["repository_id"]
            _add_commits(real_db, repository_id, [("cccc111122223333", None, None)])
            statements.clear()
            status_code, payload = _call_update_commit_fields_route()
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

        assert status_code == 200
        assert payload["updated_count"] >= 1

        updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE COMMITS_LOG")]
        assert len(updates) == 2, f"应当恰好 2 条批量 UPDATE，实际 {len(updates)} 条: {statements}"

        full_row_selects = [
            s for s in statements if "SELECT commits_log.id, commits_log.repository_id" in s
        ]
        assert not full_row_selects, (
            f"批量更新路径不允许把整行 ORM 对象拉进 Python: {full_row_selects}"
        )

        assert Commit.query.filter_by(repository_id=repository_id).one().version == "cccc1111"

    def test_substr_expression_is_1_based_on_both_backends(self, real_db):
        """substr(x, 1, 8) 在 SQLite / MySQL 上都是 1-based，与 Python 切片一致。

        MySQL 的 SUBSTR 就是 SUBSTRING 的同义函数，不需要额外别名 —— 这条把两个
        后端各自编译出来的 SQL 文本钉住，避免有人改成 0-based（会截错字符串）或
        改成 MySQL 不认的函数名。
        """
        expr = func.substr(Commit.commit_id, 1, 8)
        compiled = str(
            expr.compile(dialect=real_db.engine.dialect, compile_kwargs={"literal_binds": True})
        )
        assert compiled == "substr(commits_log.commit_id, 1, 8)"

        from sqlalchemy.dialects import mysql as mysql_dialect_module

        mysql_compiled = str(
            expr.compile(
                dialect=mysql_dialect_module.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert mysql_compiled == "substr(commits_log.commit_id, 1, 8)"


# ---------------------------------------------------------------------------
# 四、与「旧 Python 逐行赋值版」的等价性（矩阵式对照）
#
# 上面那些用例是「逐条列举」：每条钉一个具体取值。它们证明不了「对所有输入
# 两个实现一致」—— 而这次改写恰恰是**语义等价重写**，风险全在没被列举到的组合上
# （例如 commit_id 恰好等于 8 个字符、短于 8 个、恰好 7/9 个字符、
#  version 为空串而非 NULL、operation 为空串而非 NULL）。
#
# 所以这里换一种写法：把**旧的 Python 实现逐字重写**成 `_legacy_update_commit_fields`，
# 用同一批矩阵输入分别跑新旧两版（跑在两张互不干扰的数据集上），
# 逐行比对最终落库的 (version, operation) 与返回的条数。
# 这样新增一种奇怪取值只要加进矩阵，语义差异就会立刻暴露。
# ---------------------------------------------------------------------------

# 覆盖：NULL / 空串 / 恰好 8 字符 / 短于 8 / 长于 8 / 纯空白 / 非 ASCII。
# 每项是 (commit_id, version, operation)。
_COMMIT_FIELD_MATRIX = (
    ("abcdef1234567890", None, None),   # 两列都缺
    ("beefdead12345678", "v1", None),   # 只缺 operation
    ("cafebabe12345678", None, "A"),    # 只缺 version
    ("deadbeef12345678", "v2", "M"),    # 都不缺 → 必须原封不动
    ("", None, None),                   # 空串 commit_id（falsy）
    ("", "v3", None),                   # 空串 commit_id + 已有 version
    ("12345678", None, None),           # 恰好 8 字符
    ("1234567", None, None),            # 7 字符（短于 8）
    ("123456789", None, None),          # 9 字符（长于 8）
    ("   ", None, None),                # 纯空白（truthy → 原样切片）
    ("配置表版本_中文标识", None, None),  # 非 ASCII，切片按字符不按字节
    ("12345678", "", ""),               # 空串 version/operation：不是 NULL → 不动
    ("short", "", None),                # 空串 version 保留，operation 补 'M'
)


def _legacy_update_commit_fields(commit_rows):
    """**旧实现逐字重写**（改写前的生产代码），只把 ORM 查询换成传入的行列表。

    原代码：

        commits_to_update = Commit.query.filter(
            (Commit.version.is_(None)) | (Commit.operation.is_(None))
        ).all()
        updated_count = 0
        for commit in commits_to_update:
            if commit.version is None:
                commit.version = commit.commit_id[:8] if commit.commit_id else 'unknown'
            if commit.operation is None:
                commit.operation = 'M'
            updated_count += 1

    返回 (最终 (version, operation) 列表, updated_count)。
    """
    target = [row for row in commit_rows if row.version is None or row.operation is None]
    updated_count = 0
    for commit in target:
        if commit.version is None:
            commit.version = commit.commit_id[:8] if commit.commit_id else "unknown"
        if commit.operation is None:
            commit.operation = "M"
        updated_count += 1
    return updated_count


class _PlainRow:
    """`_legacy_update_commit_fields` 的输入行（不需要是 ORM 对象）。"""

    def __init__(self, commit_id, version, operation):
        self.commit_id = commit_id
        self.version = version
        self.operation = operation


def _snapshot(real_db, repository_id):
    return {
        row.commit_id: (row.version, row.operation)
        for row in Commit.query.filter_by(repository_id=repository_id).all()
    }


class TestBulkUpdateMatchesLegacyImplementation:
    def test_new_implementation_is_equivalent_to_the_old_python_loop(self, real_db, seeded_repo):
        """对整张矩阵，新实现必须与旧实现逐行等价（含返回条数）。"""
        repository_id = seeded_repo["repository_id"]

        # 旧实现的预期结果 —— 在纯 Python 里算，不碰数据库。
        legacy_rows = [_PlainRow(cid, ver, op) for cid, ver, op in _COMMIT_FIELD_MATRIX]
        legacy_count = _legacy_update_commit_fields(legacy_rows)
        legacy_expected = {
            row.commit_id: (row.version, row.operation) for row in legacy_rows
        }

        _add_commits(real_db, repository_id, list(_COMMIT_FIELD_MATRIX))
        status_code, payload = _call_update_commit_fields_route()
        assert status_code == 200

        actual = _snapshot(real_db, repository_id)

        assert payload["updated_count"] == legacy_count, (
            f"返回条数与旧实现不一致：新 {payload['updated_count']} vs 旧 {legacy_count}。"
            "旧实现统计的是「version IS NULL 或 operation IS NULL」的行数。"
        )
        assert actual == legacy_expected, (
            "落库结果与旧 Python 实现不一致。逐行差异：\n"
            + "\n".join(
                f"  commit_id={cid!r}: 新 {actual.get(cid)!r} vs 旧 {legacy_expected.get(cid)!r}"
                for cid in sorted(legacy_expected, key=repr)
                if actual.get(cid) != legacy_expected.get(cid)
            )
        )

    def test_whitespace_only_commit_id_keeps_slice_semantics(self, real_db, seeded_repo):
        """纯空白 commit_id 是 truthy → 旧实现原样切片，新实现不得改成 'unknown'。

        这一条单独拎出来是因为它最容易写错：`CASE WHEN commit_id IS NULL OR
        commit_id = '' THEN 'unknown'` 是对的，但只要有人「顺手」加上
        `TRIM(commit_id) = ''`，这里就会从 '   ' 变成 'unknown'。
        """
        repository_id = seeded_repo["repository_id"]
        _add_commits(real_db, repository_id, [("   ", None, None)])

        status_code, _payload = _call_update_commit_fields_route()
        assert status_code == 200

        row = Commit.query.filter_by(repository_id=repository_id).one()
        assert row.version == "   ", f"纯空白应当原样切片为 '   '，实际 {row.version!r}"
