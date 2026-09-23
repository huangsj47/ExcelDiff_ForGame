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
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy import text as sa_text

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from models import (  # noqa: E402
    Commit,
    Project,
    Repository,
    WeeklyVersionConfig,
    WeeklyVersionDiffCache,
)
from services import db_migration_service  # noqa: E402
from services.repository_creation_handlers import allocate_repository_id  # noqa: E402
from services.weekly_version_logic import _current_diff_logic_version  # noqa: E402

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


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _StubExcelCacheService:
    """Excel HTML 缓存服务的替身：这一组不考它，让它永远说「不欠缓存」。

    不桩的话它会去读磁盘上的 Excel —— 与 `diff_version` 这件事毫无关系，
    却会让用例因为环境里没有那个文件而红。
    """

    @staticmethod
    def needs_merged_diff_cache(_config_id, _file_path):
        return False

    @staticmethod
    def log_cache_operation(*_args, **_kwargs):
        return None


class _WeeklySyncEnv:
    """一个能真跑 `process_weekly_version_sync` 的最小环境。

    打桩的只有**外部边界**（合并 diff 的生成、Excel 缓存服务、git 拓扑序）——
    写入那一段（含 `diff_version` 的赋值）跑的是真代码，这一组要考的正是它。
    把写入也换成桩就等于「测了个替身」。
    """

    FILE_PATH = "code/pz/const/weekly_version_probe.lua"

    def __init__(self, db, monkeypatch):
        import services.weekly_version_logic as weekly_logic

        self.db = db
        self.monkeypatch = monkeypatch
        self.logic = weekly_logic
        self.payload = {"diff": "第一次"}

        now = datetime.now(timezone.utc)
        project = Project(code=_uid("WV"), name=_uid("proj"), department="QA")
        db.session.add(project)
        db.session.flush()

        self.repository = Repository(
            project_id=project.id,
            name=_uid("repo"),
            type="git",
            url="https://example.com/probe/repo.git",
            branch="main",
            clone_status="completed",
        )
        db.session.add(self.repository)
        db.session.flush()

        self.config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=self.repository.id,
            name=_uid("weekly"),
            branch="main",
            # 窗口必须盖住下面造的那些提交，否则 `process_weekly_version_sync`
            # 一个文件都发现不了 —— 那时用例会以「没写行」失败，而不是以「没打版本」失败。
            start_time=now - timedelta(days=1),
            end_time=now + timedelta(days=1),
            is_active=True,
            auto_sync=True,
            status="active",
        )
        db.session.add(self.config)
        db.session.flush()

        # 窗口**开始之前**的最后一个提交 = 基准。有它就不会去问 VCS 要真实基准
        # （那条路要真的 clone，这一组不考它）。
        db.session.add(Commit(
            repository_id=self.repository.id,
            commit_id=_uid("base"),
            path=self.FILE_PATH,
            version="v0000000",
            operation="M",
            author="base_user",
            commit_time=now - timedelta(days=2),
            message="base",
            status="pending",
        ))
        db.session.commit()

        monkeypatch.setattr(
            weekly_logic, "_generate_merged_diff_data", lambda *_a, **_k: self.payload
        )
        monkeypatch.setattr(weekly_logic, "_weekly_excel_cache_service", _StubExcelCacheService())

    def _add_commit(self, tag: str, message: str) -> None:
        now = datetime.now(timezone.utc)
        self.db.session.add(Commit(
            repository_id=self.repository.id,
            commit_id=_uid(tag),
            path=self.FILE_PATH,
            version=f"v{len(message):07d}",
            operation="M",
            author="dev_a",
            commit_time=now - timedelta(minutes=len(self.db.session.new) + 1),
            message=message,
            status="pending",
        ))
        self.db.session.commit()

    def sync(self, payload: dict) -> WeeklyVersionDiffCache:
        """改一次输入、同步一次，返回库里那一行。"""
        self.payload = payload
        self._add_commit("tip", f"change-{payload.get('diff', '')}")
        self.logic.process_weekly_version_sync(self.config.id)
        cache = WeeklyVersionDiffCache.query.filter_by(
            config_id=self.config.id, file_path=self.FILE_PATH
        ).first()
        assert cache is not None, (
            "同步之后库里没有这一行 —— 用例没跑到写入那一段，先查窗口与提交造得对不对"
        )
        return cache


def _sync_once(db, monkeypatch, *, payload: dict, again: bool = False):
    """跑一次同步并返回缓存行。

    `again=True` 表示「这个用例已经跑过一次」，复用上一轮留下的环境（同一个 config），
    这样第二次会走**更新**分支而不是又新建一行。环境存在会话级的 `_ENVS` 里 ——
    它要跨两次调用存活，而 `real_db` 夹具每个用例都会重新进入应用上下文。
    """
    key = id(db)
    env = _ENVS.get(key)
    if env is None or not again:
        env = _WeeklySyncEnv(db, monkeypatch)
        _ENVS[key] = env
    return env.sync(payload)


_ENVS: dict = {}


class TestWritePathStampsTheVersion:
    """写入方必须真的赋值；只在模型/迁移上做工作等于「已接线但没通电」。

    ## 为什么这里不再用「grep 源码找赋值语句」

    原先这一组的第一条是**数源码里的赋值出现次数**：

        hits = re.findall(r"\\.?diff_version\\s*=\\s*_current_diff_logic_version\\(\\)", source)
        assert len(hits) >= 2

    它的判据是**写法**（两个分支里各自内联调用 helper），不是**行为**。
    8770854 把那次调用提成了一个局部变量 `diff_version = _current_diff_logic_version()`
    （因为同一个值现在还要喂给 `weekly_cache_inputs_unchanged` / `weekly_cache_is_unchanged`
    两个判据）—— 两条分支照旧把版本写进列，**行为一个字都没变**，而 grep 只剩 1 处命中，
    这条断言从那天起一直红着。它红得没有价值：真正的失效模式（「列加了、迁移写了、
    写入方不赋值 → 恒为 NULL」）它其实也证明不了 —— 一个把赋值写在死分支里的实现照样过。

    所以现在改成**跑真的写入路径、看库里的那一行**：两条分支各跑一次，断言列上就是
    当前的运行期版本号。这正是本文件 docstring 里写的那个缺口（「没有任何用例去写一条
    缓存再检查它的 diff_version」）。
    """

    def test_a_brand_new_row_is_stamped(self, real_db, monkeypatch):
        """新建分支：库里有这个文件的行之前，同步一次。"""
        cache = _sync_once(real_db, monkeypatch, payload={"diff": "第一次"})

        version = _current_diff_logic_version()
        assert version, "取不到运行期版本号，无法验证写入路径"
        assert cache.diff_version == version, (
            f"新建的缓存行 diff_version 是 {cache.diff_version!r}，应当是 {version!r}"
            "（不赋值 → 该列恒为 NULL → 升级口径后合并 diff 不会失效）"
        )

    def test_a_row_written_before_the_column_existed_gets_stamped_on_rewrite(
        self, real_db, monkeypatch
    ):
        """更新分支：**列是后加的**，所以线上真正存在的是「已有一行、这一列为 NULL」。

        再同步一次时它会走「更新现有缓存」那条分支 —— 那次必须把版本补上，否则
        加列之前写下的行**永远**停在 NULL 上（读侧只能一直当历史行宽容处理）。
        库里的行是同一行（`id` 不变），所以这确实考的是更新分支而不是又新建了一行。
        """
        cache = _sync_once(real_db, monkeypatch, payload={"diff": "第一次"})
        row_id = cache.id
        cache.diff_version = None  # 模拟加列之前写入的行
        real_db.session.commit()

        updated = _sync_once(real_db, monkeypatch, payload={"diff": "改了内容"}, again=True)

        version = _current_diff_logic_version()
        assert updated.id == row_id, "这是又新建了一行 —— 那就没考到更新分支"
        assert updated.diff_version == version, (
            f"更新分支没有补上版本号，列里是 {updated.diff_version!r}"
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
