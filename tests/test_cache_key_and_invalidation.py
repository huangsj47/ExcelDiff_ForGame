# -*- coding: utf-8 -*-
"""缓存键（基线）+ 失效（版本）+ 截断标记 的行为回归。

本文件锁住四件在界面上**都看不出错**、只能靠测试发现的事：

1. **基线必须进缓存键**。同一条 (仓库, 提交, 文件) 缓存，「和谁比」在仓库里至少有
   5 条互不相同的解析路径（后台任务按 (commit_time, id) 取前一条、提交页用
   resolve_previous_commit、刷新接口只按 commit_time <、区间/合并 diff 显式传
   previous、周版本按窗口起点）。缓存键不含 previous 时，后一条路径会直接命中
   前一条路径算出来的结果 —— 同样的表格、同样的格式，只是比错了对象，**看不出错**。
2. **周版本合并 diff 缓存要有版本列**。DiffCache / ExcelHtmlCache /
   WeeklyVersionExcelCache 都有 diff_version，升级 DIFF_LOGIC_VERSION 就失效；
   WeeklyVersionDiffCache 原先没有，于是比较口径变了它还在被复用。
3. **超大文件截断不能标成 completed**。截断后行明细被清空，模板对空 rows 的
   渲染是「工作表 X 没有数据或无变更」，用户会以为文件真的没改动。
4. **incremental_cache_system 不能按一个不存在的列过滤**。它曾同时过滤
   `diff_logic_version`（DiffCache 上没有这一列）与 `diff_version`，SQLAlchemy 抛
   InvalidRequestError 被 except 吞掉 → 函数永远返回空集合 → 每次增量同步都认为
   「一个文件都没缓存过」，把全部 Excel 重新排队做全量重算。

每个用例的 docstring 里都写了「为什么需要」与「变红意味着什么」。
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services import incremental_cache_system
from app import app, create_tables, db
from models import Commit, DiffCache, Project, Repository, WeeklyVersionDiffCache
from services.excel_diff_cache_service import ExcelDiffCacheService
from services.excel_html_cache_service import ExcelHtmlCacheService
from services.weekly_excel_cache_service import WeeklyExcelCacheService

PROJECT_PATH = "client_data/a.xlsx"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _make_repository() -> Repository:
    project = Project(code=_uid("P"), name=_uid("project"), department="QA")
    db.session.add(project)
    db.session.flush()
    repository = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url=f"https://example.com/{_uid('repo')}.git",
        server_url="https://example.com",
        branch="main",
        resource_type="table",
        clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()
    return repository


def _seed_file_commits(repository: Repository, path: str = PROJECT_PATH):
    """给同一个文件造 3 条提交，commit_time 严格递增（次序不依赖数据库返回顺序）。"""
    base = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    commits = []
    for index in range(3):
        commit = Commit(
            repository_id=repository.id,
            commit_id=_uid("commit"),
            path=path,
            author="dev",
            commit_time=base + timedelta(hours=index),
            message=f"commit-{index}",
        )
        db.session.add(commit)
        commits.append(commit)
    db.session.flush()
    return commits


def _excel_payload(marker: str, path: str = PROJECT_PATH) -> dict:
    """带 marker 的 Excel diff 负载：marker 用来分辨「这份缓存是谁的基线算的」。"""
    return {
        "type": "excel",
        "file_path": path,
        "marker": marker,
        "summary": {"added": 0, "removed": 0, "modified": 1},
        "sheets": {
            "Sheet1": {
                "headers": ["A"],
                "stats": {},
                "rows": [
                    {
                        "status": "modified",
                        "row_number": 1,
                        "cells": [{"value": marker, "old_value": "old"}],
                    }
                ],
            }
        },
    }


# ---------------------------------------------------------------------------
#  1. 基线进缓存键
# ---------------------------------------------------------------------------


def test_same_commit_different_baselines_do_not_share_cache():
    """同一个 commit_id、两个不同 previous → 必须各占一行，互不串用。

    为什么需要：缓存键原先是 (repository_id, commit_id, file_path, diff_version)，
    **不含**用来做对比的前一个提交。但「和谁比」在代码里有 5 条互不相同的解析路径
    （后台任务、提交页、刷新接口、区间/合并 diff 显式传入、周版本窗口起点）。
    只要有一条路径先写了缓存，其它路径就会命中**别人的基线算出来的结果** ——
    表格一样、样式一样、也不报错，只是内容比对错了对象。

    变红意味着：previous_commit_id 又没进缓存键（或写入端又按
    (repo, commit, file) 覆盖同一行），「比错对象」这个静默错误回来了。
    """
    with app.app_context():
        create_tables()
        repository = _make_repository()
        c1, c2, c3 = _seed_file_commits(repository)
        service = ExcelDiffCacheService()

        # 路径 A：后台任务口径 —— c3 的基线是紧邻的 c2
        assert service.save_cached_diff(
            repository.id, c3.commit_id, PROJECT_PATH,
            _excel_payload("vs-c2"), previous_commit_id=c2.commit_id,
        ) is True
        # 路径 B：区间/合并 diff 口径 —— 同一条提交，基线却是区间起点 c1
        assert service.save_cached_diff(
            repository.id, c3.commit_id, PROJECT_PATH,
            _excel_payload("vs-c1"), previous_commit_id=c1.commit_id,
        ) is True

        rows = DiffCache.query.filter_by(
            repository_id=repository.id, commit_id=c3.commit_id, file_path=PROJECT_PATH
        ).all()
        assert len(rows) == 2, "不同基线必须各占一行；只有一行说明后写的把先写的覆盖了"
        assert {row.previous_commit_id for row in rows} == {c1.commit_id, c2.commit_id}

        hit_c2 = service.get_cached_diff(
            repository.id, c3.commit_id, PROJECT_PATH, previous_commit_id=c2.commit_id
        )
        assert hit_c2 is not None
        assert json.loads(hit_c2.diff_data)["marker"] == "vs-c2"

        hit_c1 = service.get_cached_diff(
            repository.id, c3.commit_id, PROJECT_PATH, previous_commit_id=c1.commit_id
        )
        assert hit_c1 is not None
        assert json.loads(hit_c1.diff_data)["marker"] == "vs-c1"

        # 第三个基线从没写过 → 必须未命中（重算），绝不能复用上面任意一份
        assert service.get_cached_diff(
            repository.id, c3.commit_id, PROJECT_PATH, previous_commit_id="deadbeefdeadbeef"
        ) is None

        db.session.remove()


def test_anonymous_cache_read_validates_authoritative_baseline():
    """绝大多数调用方不传 previous：读缓存时必须自己解析权威基线再校验。

    为什么需要：真正会踩坑的不是「显式传不同基线」的调用方（它自己知道要比谁），
    而是**不传基线的老调用方** —— 它们只会得到「最新一行」缓存。只要区间/合并
    diff 那条路径先写了一份「用区间起点当基线」的结果，提交页就会把它当自己的
    结果展示出来，用户看到的 diff 与「这一条提交改了什么」根本不是一回事。

    变红意味着：匿名读又退化成「取最新一行」，基线校验形同虚设。
    """
    with app.app_context():
        create_tables()
        repository = _make_repository()
        c1, c2, c3 = _seed_file_commits(repository)
        service = ExcelDiffCacheService()

        # 只有「区间基线 c1」这一份缓存（权威基线 c2 那份还没算）
        service.save_cached_diff(
            repository.id, c3.commit_id, PROJECT_PATH,
            _excel_payload("vs-c1"), previous_commit_id=c1.commit_id,
        )

        # 不传 previous → 解析出权威基线 c2 → 与已存的 c1 不一致 → 未命中，重算
        assert service.get_cached_diff(repository.id, c3.commit_id, PROJECT_PATH) is None

        # 对照：把权威基线那份写进去之后，匿名读必须命中它（而不是 c1 那份）
        service.save_cached_diff(
            repository.id, c3.commit_id, PROJECT_PATH,
            _excel_payload("vs-c2"), previous_commit_id=c2.commit_id,
        )
        hit = service.get_cached_diff(repository.id, c3.commit_id, PROJECT_PATH)
        assert hit is not None
        assert json.loads(hit.diff_data)["marker"] == "vs-c2", (
            "匿名读命中的不是权威基线（紧邻前一条 c2）的结果 —— 基线校验没生效"
        )

        db.session.remove()


# ---------------------------------------------------------------------------
#  2. 超大文件：截断要单独标记，不能伪装成「无变更」
# ---------------------------------------------------------------------------


def test_oversized_payload_is_marked_truncated_not_completed():
    """超过大小上限的文件必须标成 truncated，并在 HTML 里明说「文件过大，未完整比对」。

    为什么需要：截断后行明细被清空，而 templates/diff_partials/excel_diff.html
    对空 rows 的渲染是「工作表 "X" 没有数据或无变更」。原先截断结果照样写
    cache_status='completed'，于是「太大没比完」和「真的没改动」在界面上完全同形，
    用户会据此认为这个文件本版本没有改动。

    变红意味着：截断又被当成 completed（或渲染层又落回空表格分支），
    「文件过大」重新变成一句看不出来的「无变更」。
    """
    with app.app_context():
        create_tables()
        repository = _make_repository()
        c1, c2, _c3 = _seed_file_commits(repository)
        service = ExcelDiffCacheService()

        # 不必真的生成 20MB 负载：把上限压到 1KB，行为完全一致（走的是同一条分支）
        service.MAX_DIFF_DATA_BYTES = 1024

        assert service.save_cached_diff(
            repository.id, c2.commit_id, PROJECT_PATH,
            _excel_payload("x" * 5000), previous_commit_id=c1.commit_id,
        ) is True

        row = DiffCache.query.filter_by(
            repository_id=repository.id, commit_id=c2.commit_id, file_path=PROJECT_PATH
        ).first()
        assert row is not None
        assert row.cache_status == "truncated", (
            f"截断缓存的 cache_status 应为 truncated，实际为 {row.cache_status!r}；"
            f"标成 completed 会让界面显示成「无变更」"
        )

        payload = json.loads(row.diff_data)
        assert payload["truncated"] is True
        assert payload.get("notice"), "截断负载必须自带可展示的提示语"
        assert ExcelDiffCacheService.is_truncated_cache(row) is True

        # 截断缓存仍然算「命中」（否则每次访问都要重算一份 >20MB 的 diff），
        # 但渲染层必须显示提示而不是空表格。
        assert service.get_cached_diff(
            repository.id, c2.commit_id, PROJECT_PATH, previous_commit_id=c1.commit_id
        ) is not None

        html_service = ExcelHtmlCacheService(db, "1.9.0")
        html, _css, _js = html_service.generate_excel_html(payload)
        assert "文件过大，未完整比对" in html
        assert "没有数据或无变更" not in html, (
            "截断负载被渲染成了「工作表没有数据或无变更」—— 用户会以为文件真的没改动"
        )

        db.session.remove()


def test_completed_payload_is_not_marked_truncated():
    """正常大小的负载不能被误标成截断（防止「一律标截断」这种偷懒修法）。

    为什么需要：上面那条用例只证明「超限会标截断」，如果实现改成无条件标记，
    所有正常缓存都会被当成「未完整比对」，界面反而更不可信。

    变红意味着：正常负载也被标成了 truncated（或 is_truncated_cache 恒为真）。
    """
    with app.app_context():
        create_tables()
        repository = _make_repository()
        c1, c2, _c3 = _seed_file_commits(repository)
        service = ExcelDiffCacheService()

        assert service.save_cached_diff(
            repository.id, c2.commit_id, PROJECT_PATH,
            _excel_payload("small"), previous_commit_id=c1.commit_id,
        ) is True

        row = DiffCache.query.filter_by(
            repository_id=repository.id, commit_id=c2.commit_id, file_path=PROJECT_PATH
        ).first()
        assert row.cache_status == "completed"
        assert json.loads(row.diff_data).get("truncated") is None
        assert ExcelDiffCacheService.is_truncated_cache(row) is False
        assert ExcelDiffCacheService.is_truncated_cache(None) is False

        db.session.remove()


def test_legacy_truncated_row_is_detected_by_payload_marker():
    """老库里的截断记录状态仍是 completed，只能靠负载里的 truncated 标记识别。

    为什么需要：这次改动之前写下的截断记录不会被回填状态，不认它们就等于
    「升级后仍然把已经存在的截断缓存当无变更显示」，等于没修。

    变红意味着：is_truncated_cache 只认 cache_status，历史截断数据重新变成「无变更」。
    """
    row = SimpleNamespace(
        cache_status="completed",
        diff_data=json.dumps({"type": "excel", "truncated": True, "notice": "文件过大，未完整比对"}),
    )
    assert ExcelDiffCacheService.is_truncated_cache(row) is True

    normal = SimpleNamespace(cache_status="completed", diff_data=json.dumps({"type": "excel", "sheets": {}}))
    assert ExcelDiffCacheService.is_truncated_cache(normal) is False


# ---------------------------------------------------------------------------
#  3. 周版本合并 diff 的版本校验
# ---------------------------------------------------------------------------


class _SortableColumn:
    def desc(self):
        return self


class _FilteringQuery:
    """按 filter_by 的 kwargs 真的做过滤的最小假查询。

    比「first 永远返回同一行」的假查询更强：它能让「版本不匹配时是否真的被排除」
    这件事被真正验证到，而不是只验证「函数被调用过」。
    """

    def __init__(self, rows):
        self._rows = list(rows)
        self._filters = {}

    def filter_by(self, **kwargs):
        self._filters.update(kwargs)
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        for row in self._rows:
            if all(getattr(row, key, None) == value for key, value in self._filters.items()):
                return row
        return None


def _patch_weekly_models(monkeypatch, diff_rows, html_rows):
    """把 WeeklyVersionDiffCache / WeeklyVersionExcelCache 换成上面的假查询。"""
    diff_model = type("DiffModel", (), {"query": _FilteringQuery(diff_rows), "updated_at": _SortableColumn()})
    html_model = type("HtmlModel", (), {"query": _FilteringQuery(html_rows)})

    def fake_get_runtime_models(*names):
        mapping = {
            "WeeklyVersionDiffCache": diff_model,
            "WeeklyVersionExcelCache": html_model,
        }
        return tuple(mapping[name] for name in names)

    monkeypatch.setattr(
        "services.weekly_excel_cache_service.get_runtime_models", fake_get_runtime_models
    )


def test_weekly_merged_diff_cache_has_version_column():
    """WeeklyVersionDiffCache 必须真的带 diff_version 列，否则版本校验无处可读。

    为什么需要：DiffCache / ExcelHtmlCache / WeeklyVersionExcelCache 都有
    diff_version，升级 DIFF_LOGIC_VERSION 就会失效；周版本合并 diff 缓存原先
    没有这一列，比较口径变了它还在被复用。

    变红意味着：列被删了/改名了 —— 那么「版本升级让周版本合并 diff 失效」
    这条逻辑就没有数据来源，读到的永远是 None。
    """
    assert "diff_version" in WeeklyVersionDiffCache.__table__.columns


def test_weekly_merged_diff_cache_version_mismatch_forces_regeneration(monkeypatch):
    """合并口径变了（缓存行带着旧版本号）→ 必须重算，不能复用旧合并结果。

    构造：WeeklyVersionDiffCache 行带着 1.8.0，且对应 (base, latest) 的
    WeeklyVersionExcelCache 已经存在（也就是「HTML 缓存看起来是有的」）。
    老逻辑（没有版本校验）会认为「已有缓存，跳过」，于是周版本页面继续展示
    用旧口径算出来的合并 diff。

    变红意味着：needs_merged_diff_cache 不再校验版本 → 升级 DIFF_LOGIC_VERSION
    后周版本合并 diff 不会失效。
    """
    stale_diff = SimpleNamespace(
        config_id=7001,
        file_path="data/week.xlsx",
        base_commit_id="base001",
        latest_commit_id="head001",
        diff_version="1.8.0",
        cache_status="completed",
    )
    existing_html = SimpleNamespace(
        config_id=7001,
        file_path="data/week.xlsx",
        base_commit_id="base001",
        latest_commit_id="head001",
        diff_version="1.9.0",
        cache_status="completed",
        id=1,
    )
    _patch_weekly_models(monkeypatch, [stale_diff], [existing_html])

    service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.9.0")
    assert service.needs_merged_diff_cache(7001, "data/week.xlsx") is True, (
        "缓存行的 diff_version=1.8.0 与当前 1.9.0 不一致，必须重算；"
        "返回 False 表示旧口径的合并结果又被复用了"
    )


def test_weekly_merged_diff_cache_same_version_reuses_html_cache(monkeypatch):
    """版本一致时行为不能变：已有 HTML 缓存 → 不重算（防止「一律重算」的过度修复）。

    为什么需要：上面那条用例只证明「版本不一致会重算」。如果实现改成恒返回 True，
    每次周版本同步都会重刷全部 Excel HTML 缓存，是另一种故障（性能）。

    变红意味着：版本一致也被判成不一致，周版本缓存失去复用能力。
    """
    current_diff = SimpleNamespace(
        config_id=7002,
        file_path="data/week.xlsx",
        base_commit_id="base001",
        latest_commit_id="head001",
        diff_version="1.9.0",
        cache_status="completed",
    )
    existing_html = SimpleNamespace(
        config_id=7002,
        file_path="data/week.xlsx",
        base_commit_id="base001",
        latest_commit_id="head001",
        diff_version="1.9.0",
        cache_status="completed",
        id=1,
    )
    _patch_weekly_models(monkeypatch, [current_diff], [existing_html])

    service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.9.0")
    assert service.needs_merged_diff_cache(7002, "data/week.xlsx") is False


def test_legacy_weekly_merged_diff_cache_without_version_is_tolerated(monkeypatch):
    """老库里 diff_version 为 NULL 的合并缓存按「可用」处理（历史行不误伤）。

    为什么需要：加列之后老数据的值是 NULL，无法区分「旧口径」和「当前口径」。
    判成不可用会让每次周版本同步都重刷全部 Excel HTML 缓存，而写入方不在本文件，
    这个开关在这里关不掉。这条用例把这个取舍固定下来，避免以后被「顺手改成严格」
    而引入没人注意的性能回归。

    变红意味着：NULL 被当成「版本不一致」，周版本同步每次都会全量重刷 HTML 缓存。
    """
    legacy_diff = SimpleNamespace(
        config_id=7003,
        file_path="data/week.xlsx",
        base_commit_id="base001",
        latest_commit_id="head001",
        diff_version=None,
        cache_status="completed",
    )
    existing_html = SimpleNamespace(
        config_id=7003,
        file_path="data/week.xlsx",
        base_commit_id="base001",
        latest_commit_id="head001",
        diff_version="1.9.0",
        cache_status="completed",
        id=1,
    )
    _patch_weekly_models(monkeypatch, [legacy_diff], [existing_html])

    service = WeeklyExcelCacheService(SimpleNamespace(session=None), "1.9.0")
    assert service.needs_merged_diff_cache(7003, "data/week.xlsx") is False


# ---------------------------------------------------------------------------
#  4. incremental_cache_system：不再因为不存在的列返回空集合
# ---------------------------------------------------------------------------


def test_diffcache_has_no_diff_logic_version_column():
    """把「那一列不存在」这个事实固定下来（解释老代码为什么必然返回空集合）。

    为什么需要：老代码按 `DiffCache.diff_logic_version` 过滤，SQLAlchemy 会抛
    InvalidRequestError，被 except 吞掉 → 函数永远返回空集合 → 每次增量同步都
    认为「一个文件都没缓存过」，把全部 Excel 重新排队做全量重算。

    变红意味着：要么列被真的加上了（那 incremental_cache_system 的过滤条件也要
    跟着改），要么有人在别处又按这个不存在的列名过滤。
    """
    assert not hasattr(DiffCache, "diff_logic_version")
    assert hasattr(DiffCache, "diff_version")


def test_incremental_cache_lists_existing_cached_files(monkeypatch):
    """真的写了缓存 → get_existing_cached_files 必须返回它（而不是空集合）。

    为什么需要：这个函数的返回值决定「哪些文件的 Excel 缓存任务不用再排」。它
    返回空集合时**不会报任何错**，只是每次同步都全量重排任务 —— 日志里只有一句
    含糊的「获取已有缓存失败」，完全看不出是列名写错了。

    变红意味着：过滤条件又一次命中不存在的列（或版本号被硬编码成与当前源不一致的
    值），缓存去重功能整体失效，增量同步退化为全量重算。
    """
    with app.app_context():
        create_tables()
        repository = _make_repository()
        c1, c2, _c3 = _seed_file_commits(repository)
        service = ExcelDiffCacheService()
        service.save_cached_diff(
            repository.id, c2.commit_id, PROJECT_PATH,
            _excel_payload("cached"), previous_commit_id=c1.commit_id,
        )

        manager = incremental_cache_system.IncrementalCacheManager()
        cached = manager.get_existing_cached_files(repository.id)
        assert cached == {f"{c2.commit_id}:{PROJECT_PATH}"}, (
            f"已写入缓存却没被列出来（实际 {cached!r}）—— 增量同步会把这份 Excel 重新排队重算"
        )

        db.session.remove()


def test_incremental_cache_version_comes_from_single_source():
    """版本号必须来自唯一版本源（app.py 的 DIFF_LOGIC_VERSION），不能硬编码。

    为什么需要：这里原先写死 "1.7.0"（注释还写着「从app.py获取当前版本」），与真实
    版本源早已脱节：拿去比 repository.cache_version 永远不相等，于是「版本不匹配 →
    全量同步」被误判成常态；拿去过滤缓存表则一行都命中不了。

    变红意味着：又出现了第二份版本字面量，且它与真正的版本源不一致 ——
    这正是本仓库已经踩过一次的坑（见 tests/test_diff_logic_version_single_source.py）。
    """
    import app as app_module

    manager = incremental_cache_system.IncrementalCacheManager()
    assert manager.diff_logic_version == app_module.DIFF_LOGIC_VERSION
    assert manager.diff_logic_version != "1.7.0"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
