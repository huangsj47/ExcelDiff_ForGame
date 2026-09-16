# -*- coding: utf-8 -*-
"""周版本合并 diff 缓存的**版本列**必须「加了列 + 写得进 + 老库能迁移」三件齐全。

## 背景

`DiffCache` / `ExcelHtmlCache` / `WeeklyVersionExcelCache` 都有 `diff_version`
（比较口径版本），升级 `DIFF_LOGIC_VERSION` 就会让旧缓存失效。但
`WeeklyVersionDiffCache` 原先**没有**这一列，于是合并口径变了（例如 1.9.0 改成按
文本原样读取 Excel）周版本合并 diff 还在用旧结果 —— 而合并 diff 的输入是「窗口内
多条提交」，比单文件缓存更难靠人工发现，界面上新旧口径**完全同形**。

## 为什么这个文件必须存在：三件事分属三个文件

补这个能力需要三处同时到位，缺任何一处都是「静默无效果」：

1. `models/weekly_version.py`：加列（**只对新建的库生效**）
2. `services/db_migration_service.py`：`ALTER TABLE ... ADD COLUMN`（老库靠这条，
   少了它读侧会直接抛 `no such column: diff_version`）
3. `services/weekly_version_logic.py`：写入时**打上**版本号

第 3 条最容易漏：列加了、迁移也写了，但写入方不赋值 → 该列恒为 NULL → 读侧
（`is_merged_diff_cache_current`）只能把 NULL 当「历史行」宽容处理（一律判失效会让
每次同步重刷全部 Excel HTML 缓存），于是功能「已接线但没通电」：不报错，也不生效。

**实测过这个坑**：三处齐全之前，全量测试也是全绿的 —— 因为没有任何用例去写一条
缓存再检查它的 `diff_version`。本文件就是补这个缺口。
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy import text as sa_text

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models import WeeklyVersionDiffCache, db  # noqa: E402
from services import db_migration_service  # noqa: E402
from services.repository_creation_handlers import allocate_repository_id  # noqa: E402

WEEKLY_CACHE_TABLE = "weekly_version_diff_cache"
VERSION_COLUMN = "diff_version"


@pytest.fixture()
def real_db():
    """带应用上下文的真实库（沿用 conftest 隔离出来的临时 sqlite）。"""
    import app as app_module

    app_module.create_tables()
    with app_module.app.app_context():
        yield app_module.db


class TestModelDeclaresTheColumn:
    def test_column_exists_on_the_model(self):
        assert VERSION_COLUMN in WeeklyVersionDiffCache.__table__.columns, (
            f"WeeklyVersionDiffCache 缺 {VERSION_COLUMN} 列 —— 升级 DIFF_LOGIC_VERSION "
            "后合并 diff 缓存不会失效"
        )

    def test_column_is_indexed(self):
        """清理与失效查询都会按它过滤，没索引会退化成全表扫描。"""
        names = {index.name for index in WeeklyVersionDiffCache.__table__.indexes}
        assert "idx_weekly_diff_version" in names, f"缺索引，现有：{sorted(names)}"


class _EngineBackedSession:
    """把 `db.session.execute/commit/rollback` 转发到指定引擎的最小替身。

    `_migrate_table_columns` 用的是 `inspect(db.engine)` + `db.session.execute(DDL)`
    —— 所以「老库」必须真的在这个引擎上被 ALTER 出来。用假的空 session 会让
    迁移什么都没做，用例就变成空断言（实测：用空替身时报「重复执行加了多列」，
    因为那一列从头到尾就没被加上）。
    """

    def __init__(self, engine):
        self._engine = engine
        self._conn = None

    def _connection(self):
        if self._conn is None:
            self._conn = self._engine.connect()
        return self._conn

    def execute(self, statement, *args, **kwargs):
        return self._connection().execute(statement, *args, **kwargs)

    def commit(self):
        if self._conn is not None:
            self._conn.commit()

    def rollback(self):
        if self._conn is not None:
            self._conn.rollback()

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class _FakeDb:
    def __init__(self, engine):
        self.engine = engine
        self.session = _EngineBackedSession(engine)


class TestMigrationAddsTheColumnToExistingDatabases:
    """老库（建表时还没有这一列）必须能靠迁移补上。"""

    def _make_legacy_table(self, engine):
        """造一张**没有 diff_version** 的 weekly_version_diff_cache。

        不用 `db.create_all()`：那会用当前模型建表、列是齐的，
        测不出「老库缺列」这个真实场景。
        """
        with engine.begin() as conn:
            conn.execute(
                sa_text(
                    f"""
                    CREATE TABLE {WEEKLY_CACHE_TABLE} (
                        id INTEGER PRIMARY KEY,
                        config_id INTEGER,
                        repository_id INTEGER,
                        file_path VARCHAR(500),
                        cache_status VARCHAR(20),
                        overall_status VARCHAR(20),
                        last_sync_time DATETIME
                    )
                    """
                )
            )
            conn.execute(
                sa_text(
                    f"INSERT INTO {WEEKLY_CACHE_TABLE} "
                    "(id, config_id, repository_id, file_path, cache_status) "
                    "VALUES (1, 1, 1, 'Config/A.xlsx', 'completed')"
                )
            )

    def test_migration_adds_the_column_without_losing_rows(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
        self._make_legacy_table(engine)
        assert VERSION_COLUMN not in {
            c["name"] for c in inspect(engine).get_columns(WEEKLY_CACHE_TABLE)
        }

        messages = []
        db_migration_service._migrate_weekly_version_diff_cache_columns(
            _FakeDb(engine), lambda msg, *a, **k: messages.append(str(msg))
        )

        columns = {c["name"] for c in inspect(engine).get_columns(WEEKLY_CACHE_TABLE)}
        assert VERSION_COLUMN in columns, (
            f"迁移没有给老库补上 {VERSION_COLUMN}（现有列 {sorted(columns)}）—— "
            "老库读这一列时会直接抛 no such column"
        )

        # 已有数据必须原样保留（ALTER TABLE ADD COLUMN 不该动数据）。
        with engine.connect() as conn:
            row = conn.execute(
                sa_text(f"SELECT id, file_path, {VERSION_COLUMN} FROM {WEEKLY_CACHE_TABLE}")
            ).fetchone()
        assert row[0] == 1 and row[1] == "Config/A.xlsx"
        assert row[2] is None, "新增列对历史行应当是 NULL（读侧按历史行宽容处理）"

    def test_migration_is_idempotent(self, tmp_path):
        """重复执行不该报错（每次启动都会跑）。"""
        engine = create_engine(f"sqlite:///{tmp_path / 'legacy2.db'}")
        self._make_legacy_table(engine)
        fake_db = _FakeDb(engine)
        for _ in range(3):
            db_migration_service._migrate_weekly_version_diff_cache_columns(
                fake_db, lambda *a, **k: None
            )
        fake_db.session.close()
        columns = [c["name"] for c in inspect(engine).get_columns(WEEKLY_CACHE_TABLE)]
        assert columns.count(VERSION_COLUMN) == 1, f"重复执行加了多列：{columns}"


class TestFreshDatabaseHasTheColumn:
    def test_create_all_produces_the_column(self, real_db):
        columns = {c["name"] for c in inspect(real_db.engine).get_columns(WEEKLY_CACHE_TABLE)}
        assert VERSION_COLUMN in columns, (
            f"新建库里 {WEEKLY_CACHE_TABLE} 没有 {VERSION_COLUMN} —— "
            "模型上的列定义没生效"
        )


class TestVersionSourceIsTheLiveOne:
    def test_current_version_helper_matches_the_runtime_constant(self, real_db):
        """`_current_diff_logic_version()` 必须返回**真正生效**的那个版本号。

        仓库里有两份字面量（app.py 那份驱动缓存失效、config.py 那份只做界面展示），
        写缓存时必须用前者，否则缓存会带着一个不对的版本号出生。
        """
        import app as app_module
        from services.weekly_version_logic import _current_diff_logic_version

        resolved = _current_diff_logic_version()
        # 版本号不该硬编码进断言（会随升级而过期），改成与运行期常量比对。
        assert resolved == str(app_module.DIFF_LOGIC_VERSION), (
            f"_current_diff_logic_version() 返回 {resolved!r}，"
            f"而运行时 DIFF_LOGIC_VERSION 是 {app_module.DIFF_LOGIC_VERSION!r}"
        )

    def test_helper_does_not_fall_back_to_a_hardcoded_version(self):
        """解析不出时应返回 None，而不是猜一个版本号。

        猜出来的版本号会让缓存静默失效（或静默不错效），比不标注更难查。
        """
        import services.weekly_version_logic as weekly_logic

        source = open(weekly_logic.__file__, encoding="utf-8").read()
        start = source.index("def _current_diff_logic_version()")
        body = source[start : source.index("\ndef ", start + 10)]
        assert "return None" in body, "取不到版本时应当返回 None"

        import re

        literal_versions = re.findall(r"return\s+[\"'](\d+\.\d+\.\d+)[\"']", body)
        assert not literal_versions, f"helper 里出现了硬编码版本号：{literal_versions}"


class TestWritePathStampsTheVersion:
    """写入方必须真的赋值；只在模型/迁移上做工作等于「已接线但没通电」。"""

    def test_both_write_branches_stamp_the_version(self):
        import re

        import services.weekly_version_logic as weekly_logic

        source = open(weekly_logic.__file__, encoding="utf-8").read()
        # 允许 `diff_version=_current_diff_logic_version()` 与
        # `existing_cache.diff_version = _current_diff_logic_version()` 两种写法
        # （等号两侧可能有空格、可能有属性前缀）—— 断言的是「赋值」而不是空格。
        hits = re.findall(r"\.?diff_version\s*=\s*_current_diff_logic_version\(\)", source)
        assert len(hits) >= 2, (
            f"只找到 {len(hits)} 处 diff_version 赋值。周版本合并 diff 缓存有两条写入分支"
            "（更新已有 / 新建），两处都要打上 —— 实测只加列+迁移、不赋值时该列恒为 NULL，"
            "版本失效功能完全不生效，而且全量测试仍全绿。"
        )

    def test_stamped_row_survives_a_round_trip(self, real_db):
        """端到端：写一条带版本的缓存行，读回来版本号还在且被判定为「当前口径」。"""
        import app as app_module
        from models import Project, Repository, WeeklyVersionConfig
        from services.weekly_version_logic import _current_diff_logic_version

        version = _current_diff_logic_version()
        assert version, "取不到运行期版本号，无法验证写入路径"

        # 必须建真实的父行：SQLite 开了 PRAGMA foreign_keys=ON，凭空填 FK 会
        # IntegrityError。用唯一名字，避免与并行用例撞唯一约束。
        uid = uuid.uuid4().hex[:10]
        project = Project(code=f"WVER{uid}"[:12], name=f"wver_proj_{uid}")
        real_db.session.add(project)
        real_db.session.commit()
        repository = Repository(
            id=allocate_repository_id(),
            project_id=project.id,
            name=f"wver_repo_{uid}",
            type="git",
            url="https://example.invalid/wver.git",
            resource_type="table",
        )
        real_db.session.add(repository)
        real_db.session.commit()
        config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repository.id,
            name=f"wver_cfg_{uid}",
            branch="main",  # NOT NULL：不填会 IntegrityError
            start_time=datetime(2026, 3, 1, 0, 0, 0),
            end_time=datetime(2026, 3, 7, 23, 59, 59),
        )
        real_db.session.add(config)
        real_db.session.commit()

        row = WeeklyVersionDiffCache(
            config_id=config.id,
            repository_id=repository.id,
            file_path="Config/version_round_trip.xlsx",
            merged_diff_data="{}",
            latest_commit_id="deadbeef",
            cache_status="completed",
            overall_status="pending",
            diff_version=version,
            last_sync_time=datetime.now(timezone.utc),
        )
        real_db.session.add(row)
        real_db.session.commit()
        row_id = row.id

        try:
            real_db.session.expire_all()
            fetched = real_db.session.get(WeeklyVersionDiffCache, row_id)
            assert fetched is not None
            assert fetched.diff_version == version, (
                f"版本号没落库：写 {version!r}、读回 {fetched.diff_version!r}"
            )

            service = app_module.weekly_excel_cache_service
            assert service.is_merged_diff_cache_current(fetched) is True, (
                "同一版本号的行应当被判为「当前口径」（可复用）"
            )
            fetched.diff_version = "0.0.1-not-the-live-one"
            assert service.is_merged_diff_cache_current(fetched) is False, (
                "版本号不一致时必须判为不可用 —— 否则升级 DIFF_LOGIC_VERSION 后"
                "周版本页面继续展示旧口径算出来的合并结果"
            )
        finally:
            real_db.session.query(WeeklyVersionDiffCache).filter_by(id=row_id).delete()
            real_db.session.query(WeeklyVersionConfig).filter_by(id=config.id).delete()
            real_db.session.query(Repository).filter_by(id=repository.id).delete()
            real_db.session.query(Project).filter_by(id=project.id).delete()
            real_db.session.commit()


class TestTruncatedExcelRendering:
    """「文件过大、只存了摘要」绝不能渲染成「没有改动」。

    ## 缺陷

    `save_cached_diff` 在负载超过 20MB 时把 `sheets[*].rows` 清空、只留统计，
    原先仍写 `cache_status='completed'`。渲染层看到空 rows，就落进
    「工作表 "X" 没有数据或无变更」那个分支 —— 界面上与「这个文件本版本真的没改」
    **完全同形**。评审者据此确认提交，等于确认了没看过的内容。

    缓存侧已改为 `cache_status='truncated'` 并在负载里带 `truncated/notice` 标记，
    这里钉住**渲染侧**也认得这个标记：
      * 截断负载 → 出现「文件过大，未完整比对」，且**不出现**「无变更」字样；
      * 非截断的空负载 → 仍是原来的「无变更」分支（不能把正常的空结果也改口）。
    """

    @staticmethod
    def _truncated_payload():
        return {
            "type": "excel",
            "truncated": True,
            "truncation_reason": "payload_too_large",
            "notice": "文件过大，未完整比对",
            "original_size_mb": 31.4,
            "max_size_mb": 20,
            "error": "数据过大(31.4MB)，已截断。请在线查看差异。",
            "sheets": {"Sheet1": {"stats": {"added": 3}, "rows": [], "headers": ["A"]}},
        }

    def test_render_helper_shows_the_truncation_notice(self):
        from services.diff_render_helpers import render_excel_diff_html

        rendered = render_excel_diff_html(self._truncated_payload(), "Config/Big.xlsx")

        assert "文件过大，未完整比对" in rendered, (
            "截断负载没有渲染出提示 —— 会落进「Excel文件无变更数据」分支"
        )
        assert "无变更" not in rendered, (
            f"截断负载里出现了「无变更」字样，界面会与真的没改动混淆：\n{rendered[:400]}"
        )
        assert 'data-truncated="true"' in rendered, (
            "缺少 data-truncated 标记，页面/脚本无法再区分这条是不是截断结果"
        )
        assert "31.4MB" in rendered, "应当给出原始大小，让用户判断要不要在线重算"

    def test_render_helper_accepts_a_json_string_payload(self):
        """缓存行的 `diff_data` 是 Text 列，会以 JSON 字符串形态传进来。"""
        import json as _json

        from services.diff_render_helpers import render_excel_diff_html

        rendered = render_excel_diff_html(
            _json.dumps(self._truncated_payload()), "Config/Big.xlsx"
        )
        assert "文件过大，未完整比对" in rendered

    def test_normal_empty_payload_still_says_no_change(self):
        """非截断的空结果仍走原分支 —— 不能把正常的「没有改动」也改口。"""
        from services.diff_render_helpers import render_excel_diff_html

        assert "Excel文件无变更数据" in render_excel_diff_html(None, "x.xlsx")
        assert "Excel文件无变更数据" in render_excel_diff_html({"type": "excel"}, "x.xlsx")

    def test_partial_template_distinguishes_truncated_from_empty_rows(self):
        """模板里那段空 rows 分支必须带截断判定（纯文本契约，无法用渲染断言替代）。"""
        from pathlib import Path

        template = (
            Path(PROJECT_ROOT) / "templates" / "diff_partials" / "excel_diff.html"
        ).read_text(encoding="utf-8")
        assert "excel_data.truncated" in template, (
            "模板的空 rows 分支没有判 excel_data.truncated —— 截断结果会被显示成"
            "「工作表 X 没有数据或无变更」"
        )
        assert "excel-truncated-notice" in template
