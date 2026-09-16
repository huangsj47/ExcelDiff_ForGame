# -*- coding: utf-8 -*-
"""整份 Excel 被删除时，页面必须把**删除前的内容**展示出来。

## 缺陷形态（线上实测）

`services/commit_diff_view_service.py` 的删除分支直接短路：

    if is_deleted:
        return render_template(..., diff_data={"type": "deleted", "message": "该文件已被删除"}, ...)

页面于是只显示一句「该文件在此提交中被删除，无法显示差异内容」+ 一个「上一个版本」链接。
而那个链接指向的是**那条提交自己的变更**，不是被删掉的内容 —— 配表整份删除
（一次提交删掉整个目录、或者改名被建模成「删旧名 + 加新名」）时，评审者
只被告知「有东西没了」却看不到没的是什么，只能盲签。

同一个病在另外两个产出方也在：
* `services/git_service.py::_compare_sheet_data` —— `{'status': 'deleted', 'rows': []}`
* `services/git_excel_parser_helpers.py` —— `{"status": "deleted", "rows": []}`，
  以及整份文件删除时回一个**没有 sheets** 的提示

## 这些测试各钉什么

* `TestProcessDeletedFile`：引擎侧。删除文件 ⇒ 每张表都是「已删除工作表」+ 全部行；
  基线字节拿不到时给**诚实的空结果**，绝不编造一份「什么都没有的删除」。
* `TestGeneralPathIsNotReused`：钉住「删除」不能靠 `current_content=None` 推断 ——
  通用路径对空内容必须拒绝，否则一次 VCS 读取失败会被渲染成一次真实的删除。
* `TestDeletedFileDiffData`：编排层（真实字节 + 真实引擎，只把缓存与 VCS 换成假件）。
* `TestDeletedShapeOneProducers`：另外两个产出方的整表/整份删除分支也要带 rows。
* `TestDeletedCommitPageRendersContent`：视图层与模板接线。
"""
from __future__ import annotations

import io
import os
import sys

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402


def _xlsx_bytes(sheets: dict) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
    return buffer.getvalue()


BASELINE_BYTES = _xlsx_bytes({
    "奖励模式": pd.DataFrame({
        "id": ["TYPE", "DEFAULT", "测试id"],
        "字段": ["int", None, "1"],
        "每日获得次数上限": ["int", None, "1000"],
    }),
})


def _deleted_commit():
    """一个「文件在此提交里被删除」的提交（只用到这几个字段）。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        commit_id="a" * 40,
        path="config/奖励模式_CfgRewardMode.xlsx",
        commit_time=None,
        operation="D",
        repository=SimpleNamespace(id=7, type="git", project_id=None, project=None),
    )


@pytest.fixture()
def patched(monkeypatch):
    """真实引擎 + 真实字节，只把缓存与 VCS 读内容换成假件。"""
    import services.excel_diff_cache_service as cache_module
    import services.vcs_content_service as vcs_module

    saved = {}

    class _FakeCacheService:
        def __init__(self):
            self.saved_rows = []
            self.cached_json = None  # 设为 JSON 文本即模拟「缓存命中」

        def get_cached_diff(self, *_args, **_kwargs):
            if self.cached_json is None:
                return None
            from types import SimpleNamespace
            return SimpleNamespace(diff_data=self.cached_json)

        def save_cached_diff(self, **kwargs):
            self.saved_rows.append(kwargs)
            return True

    fake_cache = _FakeCacheService()
    monkeypatch.setattr(cache_module, "ExcelDiffCacheService", lambda: fake_cache)
    monkeypatch.setattr(
        vcs_module, "get_file_content_from_git",
        lambda repository, commit_id, path: BASELINE_BYTES if commit_id == "b" * 40 else None,
    )
    saved["cache"] = fake_cache
    return saved


class TestProcessDeletedFile:
    def test_every_sheet_is_marked_deleted_with_all_rows(self):
        result = DiffService().process_deleted_file("config/x.xlsx", BASELINE_BYTES)
        assert result["type"] == "excel"
        sheet = result["sheets"]["奖励模式"]
        assert sheet["operation"] == "deleted", (
            "删除文件时工作表没有标成 deleted —— 渲染层就不认它是「整表删除」"
        )
        assert len(sheet["rows"]) == 3, (
            f"被删除的文件只留下 {len(sheet['rows'])} 行 —— "
            "评审者看不到删掉了什么（这正是线上「文件已删除」空页面的成因）"
        )
        assert all(row["status"] == "removed" for row in sheet["rows"])
        assert result["summary"]["removed"] == 3
        assert sheet["rows"][0]["data"]["id"] == "TYPE", "行内容必须是基线版本的真实值"

    def test_missing_baseline_stays_empty_instead_of_faking_a_deletion(self):
        """基线字节拿不到（仓库没同步 / 路径对不上）时，必须给空结果让上层显示
        「无法获取差异」，不能凭空造一份「删除但没有任何内容」的载荷。"""
        result = DiffService().process_deleted_file("config/x.xlsx", None)
        assert result["sheets"] == {}
        assert result["summary"]["total"] == 0


class TestGeneralPathIsNotReused:
    """`current_content=None` 有歧义，通用路径必须继续拒绝它。

    「文件在这个提交里不存在」（真删除）与「从 VCS 读内容失败」在
    `process_diff(path, None, previous)` 这个调用形状上**完全一样**。若为了省一个
    方法就让通用路径把它当成删除，一次读取失败就会被渲染成一次真实的删除 ——
    评审者会把「没读到」确认成「已删除」。所以删除只能由调用方显式声明。
    """

    def test_process_diff_does_not_silently_produce_an_all_deleted_payload(self):
        result = DiffService().process_diff("config/x.xlsx", None, BASELINE_BYTES)
        assert not result.get("sheets"), (
            "通用路径把空当前内容当成了「整份删除」——"
            "VCS 读取失败会被渲染成一次真实的删除"
        )


class TestDeletedFileDiffData:
    """编排层：把缓存与 VCS 换成假件，引擎与真实字节都走真的。"""

    @staticmethod
    def _commit():
        return _deleted_commit()

    def test_builds_payload_from_the_baseline_and_caches_it(self, patched):
        from services.vcs_content_service import get_deleted_file_diff_data

        previous = type("P", (), {"commit_id": "b" * 40})()
        result = get_deleted_file_diff_data(self._commit(), previous)

        assert result and result["sheets"], "删除文件没有生成任何内容"
        assert len(result["sheets"]["奖励模式"]["rows"]) == 3
        assert patched["cache"].saved_rows, "删除文件的差异没有写缓存（每次访问都要重算）"
        assert patched["cache"].saved_rows[0]["previous_commit_id"] == "b" * 40, (
            "写缓存时没有按真实基线落库 —— 缓存键里的基线就失去意义了"
        )

    def test_returns_none_when_baseline_bytes_are_unavailable(self, patched):
        from services.vcs_content_service import get_deleted_file_diff_data

        previous = type("P", (), {"commit_id": "c" * 40})()  # 取不到内容的版本
        assert get_deleted_file_diff_data(self._commit(), previous) is None

    def test_returns_none_without_a_previous_commit(self, patched):
        from services.vcs_content_service import get_deleted_file_diff_data

        assert get_deleted_file_diff_data(self._commit(), None) is None

    def test_cached_payload_without_sheets_is_ignored_and_recomputed(self, patched):
        """缓存里那份「没有工作表的 excel」是通用路径写下的（历史缺陷）：对删除提交
        来说它等于什么都没有。必须当作未命中重算并覆盖 —— 否则**先被接口或后台任务
        碰过**的那条删除提交，页面上被删内容就永远不显示（线上实测：同一批删除提交里
        先被接口访问过的页面全空，没被访问过的正常渲染出全部被删行）。
        """
        import json

        from services.vcs_content_service import get_deleted_file_diff_data

        patched["cache"].cached_json = json.dumps({"type": "excel", "sheets": {}})
        previous = type("P", (), {"commit_id": "b" * 40})()

        result = get_deleted_file_diff_data(self._commit(), previous)

        assert result and result["sheets"], "缓存里的空载荷被当成结果返回了 —— 页面又会只剩一句「文件已删除」"
        assert len(result["sheets"]["奖励模式"]["rows"]) == 3
        assert patched["cache"].saved_rows, "重算之后没有覆盖那份空载荷，下次还是读到它"

    def test_cached_payload_with_sheets_is_still_served(self, patched):
        """有内容的缓存仍要命中（这是缓存存在的意义）。"""
        import json

        from services.vcs_content_service import get_deleted_file_diff_data

        cached = {"type": "excel", "sheets": {"奖励模式": {"operation": "deleted", "rows": [{"row_number": 1}]}}}
        patched["cache"].cached_json = json.dumps(cached)
        previous = type("P", (), {"commit_id": "b" * 40})()

        result = get_deleted_file_diff_data(self._commit(), previous)

        assert result == cached
        assert not patched["cache"].saved_rows, "命中缓存却又重算了一遍"


class TestUnifiedDiffRoutesDeletions:
    """`get_unified_diff_data` 是接口与后台任务共用的入口。

    它不知道「这个文件已经没了」：拿不到当前内容就只算出一个**没有工作表的 excel
    载荷**，接口把它回成「没有找到Excel工作表数据」、后台任务把它写进缓存。
    删除提交必须在这里就转到删除自己的路径。
    """

    def test_deleted_commit_returns_the_deleted_payload(self, patched):
        from services.vcs_content_service import get_unified_diff_data

        previous = type("P", (), {"commit_id": "b" * 40})()
        payload = get_unified_diff_data(_deleted_commit(), previous)

        assert payload and payload.get("sheets"), (
            "删除提交在统一入口上没有走删除路径 —— 接口会回「没有找到Excel工作表数据」，"
            "并把这份空载荷写进缓存供页面读取"
        )
        assert len(payload["sheets"]["奖励模式"]["rows"]) == 3
        assert payload["sheets"]["奖励模式"]["operation"] == "deleted"


class TestDeletedShapeOneProducers:
    """另一套产出方（git_service / git_excel_parser_helpers，行内是 cells）也不能丢内容。"""

    @staticmethod
    def _previous_sheet():
        return [{"A": "id", "B": "奖励模式编号"}, {"A": "TYPE", "B": "define"}]

    @staticmethod
    def _service():
        """真实 GitService 实例（跳过 __init__：这些方法只依赖 self 上的纯逻辑）。"""
        from services.git_service import GitService

        service = GitService.__new__(GitService)
        service.performance_stats = {"excel_processing_time": 0.0}
        return service

    def test_deleted_sheet_diff_keeps_rows_and_cells(self):
        sheet = self._service()._deleted_sheet_diff(self._previous_sheet())

        assert sheet["status"] == "deleted", "模板与前端都靠这个字段显示「工作表已被删除」"
        assert sheet["headers"] == ["A", "B"]
        assert len(sheet["rows"]) == 2
        assert sheet["rows"][0]["row_number"] == 1
        assert sheet["rows"][1]["cells"][0] == {"value": "TYPE", "status": "removed"}
        assert sheet["has_changes"] is True, "没有 has_changes 的话标签页不会标「变更」"

    def test_compare_sheet_data_uses_it_for_deleted_sheets(self):
        sheet = self._service()._compare_sheet_data([], self._previous_sheet())
        assert sheet["status"] == "deleted"
        assert len(sheet["rows"]) == 2, (
            "整表删除时 _compare_sheet_data 又只回计数了 —— 正文会再次空白"
        )

    def test_empty_previous_sheet_stays_empty(self):
        sheet = self._service()._deleted_sheet_diff([])
        assert sheet["rows"] == [] and sheet["has_changes"] is False

    def test_generate_excel_diff_data_keeps_deleted_sheet_rows(self):
        from services import git_excel_parser_helpers as helpers

        service = self._service()
        result = helpers.generate_excel_diff_data(
            service,
            {"保留表": [{"A": "1"}]},
            {"保留表": [{"A": "1"}], "删掉的表": self._previous_sheet()},
            "config/x.xlsx",
        )
        deleted = result["sheets"]["删掉的表"]
        assert deleted["status"] == "deleted"
        assert len(deleted["rows"]) == 2, "整表删除的行又被丢掉了"
        assert deleted["rows"][1]["cells"][0]["value"] == "TYPE"


class TestDeletedCommitPageRendersContent:
    """视图层 + 模板接线。"""

    def test_view_passes_deleted_content_and_keeps_deleted_flag(self):
        from types import SimpleNamespace

        import services.commit_diff_view_service as view

        commit = SimpleNamespace(id=5, commit_id="f" * 40, operation="D", repository_id=1,
                                 path="config/x.xlsx", commit_time="2026-09-01")
        previous = SimpleNamespace(id=4, commit_id="e" * 40, version="v4", commit_time="2026-08-31")
        payload = {"type": "excel", "sheets": {"奖励模式": {"operation": "deleted", "rows": [{"row_number": 1}]}},
                   "summary": {"added": 0, "removed": 1, "modified": 0, "total": 1}}
        seen = {}

        def _build(**kwargs):
            seen.update(kwargs)
            return kwargs

        context = view.handle_commit_diff_view(
            commit_id=5,
            time_module=SimpleNamespace(time=lambda: 1.0),
            Commit=_CommitModel(commit),
            db=SimpleNamespace(session=SimpleNamespace(delete=lambda *_a, **_k: None,
                                                       commit=lambda: None, rollback=lambda: None)),
            excel_cache_service=SimpleNamespace(is_excel_file=lambda _p: True),
            add_excel_diff_task=lambda *_a, **_k: None,
            threaded_git_service_cls=lambda *_a, **_k: SimpleNamespace(),
            active_git_processes={},
            get_commit_diff_mode_strategy=lambda: SimpleNamespace(async_agent_diff=False),
            resolve_previous_commit=lambda *_a, **_k: previous,
            attach_author_display=lambda *_a, **_k: None,
            get_unified_diff_data=lambda *_a, **_k: None,
            get_deleted_file_diff_data=lambda *_a, **_k: payload,
            get_diff_data=lambda *_a, **_k: None,
            validate_excel_diff_data=lambda *_a, **_k: (True, "ok"),
            clean_json_data=lambda data: data,
            build_commit_diff_template_context=_build,
            performance_metrics_service=SimpleNamespace(record=lambda *_a, **_k: None),
            ensure_commit_access_or_403=lambda _c: (SimpleNamespace(id=1, name="r", type="git"), None),
            render_template=lambda _t, **ctx: ctx,
            log_print=lambda *_a, **_k: None,
        )

        assert context["is_deleted"] is True
        assert context["diff_data"] is payload, (
            "删除提交没有把「被删内容」传给模板，页面又只剩一句「无法显示差异内容」"
        )
        assert seen["diff_data"] is payload

    def test_view_falls_back_to_notice_when_content_is_unavailable(self):
        from types import SimpleNamespace

        import services.commit_diff_view_service as view

        commit = SimpleNamespace(id=6, commit_id="a" * 40, operation="D", repository_id=1,
                                 path="config/x.xlsx", commit_time="2026-09-01")
        previous = SimpleNamespace(id=5, commit_id="b" * 40, version="v5",
                                   commit_time="2026-08-31")

        context = view.handle_commit_diff_view(
            commit_id=6,
            time_module=SimpleNamespace(time=lambda: 1.0),
            Commit=_CommitModel(commit),
            db=SimpleNamespace(session=SimpleNamespace(delete=lambda *_a, **_k: None,
                                                       commit=lambda: None, rollback=lambda: None)),
            excel_cache_service=SimpleNamespace(is_excel_file=lambda _p: True),
            add_excel_diff_task=lambda *_a, **_k: None,
            threaded_git_service_cls=lambda *_a, **_k: SimpleNamespace(),
            active_git_processes={},
            get_commit_diff_mode_strategy=lambda: SimpleNamespace(async_agent_diff=False),
            resolve_previous_commit=lambda *_a, **_k: previous,
            attach_author_display=lambda *_a, **_k: None,
            get_unified_diff_data=lambda *_a, **_k: None,
            get_deleted_file_diff_data=lambda *_a, **_k: None,   # 取不到基线字节
            get_diff_data=lambda *_a, **_k: None,
            validate_excel_diff_data=lambda *_a, **_k: (True, "ok"),
            clean_json_data=lambda data: data,
            build_commit_diff_template_context=lambda **kwargs: kwargs,
            performance_metrics_service=SimpleNamespace(record=lambda *_a, **_k: None),
            ensure_commit_access_or_403=lambda _c: (SimpleNamespace(id=1, name="r", type="git"), None),
            render_template=lambda _t, **ctx: ctx,
            log_print=lambda *_a, **_k: None,
        )

        assert context["diff_data"] == {"type": "deleted", "message": "该文件已被删除"}
        assert not context["diff_data"].get("sheets")

    def test_loader_partial_provides_the_container_the_renderer_needs(self):
        """删除分支要把 #excel-diff-container 渲染出来，前端 renderer 才有地方写内容
        （JS 会在拿到 sheets 时直接写这个容器，容器不存在就在 null 上抛异常）。"""
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader(os.path.join(PROJECT_ROOT, "templates")))
        html = env.get_template("diff_partials/excel_diff_loader.html").render()

        assert 'id="excel-diff-container"' in html
        assert 'id="excel-loading-indicator"' in html
        assert 'id="loading-progress-bar"' in html

    def test_commit_diff_template_renders_loader_in_deleted_branch(self):
        """结构性断言：删除分支（is_deleted）在有被删内容时必须带上 loader。

        这个模板 extends base.html，整页渲染需要一整套全局变量，所以这里只钉住接线
        本身：删除分支里出现了 loader 的 include，且它的条件是「有 sheets」。
        """
        source = open(os.path.join(PROJECT_ROOT, "templates", "commit_diff.html"),
                      encoding="utf-8").read()
        deleted_branch = source.split("{% if is_deleted %}", 1)[1].split("{% elif is_excel %}", 1)[0]
        assert "diff_partials/excel_diff_loader.html" in deleted_branch, (
            "删除分支没有渲染 Excel 容器 —— 被删内容没有地方显示"
        )
        assert "diff_data.sheets" in deleted_branch, (
            "删除分支渲染容器的条件里没有 diff_data.sheets —— 拿不到内容时会渲染出一个空容器"
        )


class _Field:
    """列替身：既要做 `== 值`、又要支持 .desc()（order_by 用）。"""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return (self.name, "eq", other)

    def desc(self):
        return (self.name, "desc")


class _CommitModel:
    """最小可用的 Commit 模型替身。

    视图服务会用它构造查询（`Commit.repository_id == ...` 等），所以类上要有这些
    属性；这里不接真数据库，比较与排序都只是 Python 层面的字符串操作。
    """

    repository_id = _Field("repository_id")
    path = _Field("path")
    commit_time = _Field("commit_time")
    id = _Field("id")

    def __init__(self, commit):
        self.query = _CommitQuery(commit)


class _CommitQuery:
    """最小可用的查询替身（只用到 get_or_404 与 filter/order_by/all）。"""

    def __init__(self, commit):
        self._commit = commit

    def get_or_404(self, _commit_id):
        return self._commit

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return [self._commit]
