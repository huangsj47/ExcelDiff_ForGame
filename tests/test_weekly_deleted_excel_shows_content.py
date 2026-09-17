# -*- coding: utf-8 -*-
"""周版本里被删掉的 Excel，必须把**删除前的内容**渲染出来，而不是只给一句「已删除」。

## 缺陷形态（线上实测，config_id=1 的第 17 个文件）

`services/weekly_version_logic.py::generate_weekly_excel_merged_diff_html` 一判定
「这个文件在本周被删了」就直接短路成提示条：

    <h5 class='alert-heading'>Excel文件已删除</h5>
    <p class='mb-0'>奖励模式表_CfgRewardMode.xlsx 在该周版本中已被删除。</p>
    可以查看 上一个版本 (c2b53031) 来查看删除前的Excel内容。

页面正文没有任何内容。同一个周版本里同时冒出了两个内容几乎相同的新文件
（`config/奖励模式.xlsx`、`config/奖励模式_CfgRewardMode.xlsx`）—— 逐个单元格比对，
被删的那份与 `config/奖励模式.xlsx` **逐格 0 差异**（连表名都没改内容，只是文件改名 +
工作表改名）。评审者看到的是「一张表被删了 + 两张表全新增」，看不到被删的到底是什么，
无法判断这是一次改名还是真的删掉了一张配表。

同一件事在另外两条路上早就是这个口径了：

* 非 Excel 的删除：`render_deleted_file_content` 会列出被删掉的行（还有「显示删除内容」按钮）；
* 提交页的删除提交：`get_deleted_file_diff_data` 把删除前那张表的每一行都渲染成删除行。

## 这些测试各钉什么

* `TestDeletedPayloadFromBaseline`：载荷侧。基线字节在 ⇒ 每张表每行都是删除行；
  字节拿不到（或根本不是 Excel）⇒ 返回 None，绝不编一份「没有任何内容的删除」。
* `TestDeletedExcelContentHtml`：渲染侧。提示条 + 可被前端解析的 JSON 载荷；
  表名/文件名来自被审核的仓库，必须转义与 urlencode。
* `TestWeeklyLogicRendersDeletedContent`：接线侧。删除态真的走「渲染内容」这条分支，
  且**先进 HTML 缓存**（后台任务用同一个键写；删除文件也要能被缓存命中，
  否则每次打开页面都重读整份基线 Excel）；取不到基线时退回提示条。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from html.parser import HTMLParser
from types import SimpleNamespace

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.diff_service import DiffService  # noqa: E402
from services.weekly_deleted_excel_helpers import (  # noqa: E402
    build_deleted_excel_payload,
    render_deleted_excel_content,
)

SHEET_NAME = "奖励模式"
DELETED_PATH = "config/奖励模式表_CfgRewardMode.xlsx"


def _xlsx_bytes(sheets: dict) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
    return buffer.getvalue()


BASELINE_BYTES = _xlsx_bytes({
    SHEET_NAME: pd.DataFrame({
        "id": ["TYPE", "DEFAULT", "测试id"],
        "字段": ["int", None, "1"],
        "每日获得次数上限": ["int", None, "1000"],
    }),
})


def _payload_node(markup: str) -> str:
    match = re.search(
        r'<script type="application/json"[^>]*data-weekly-excel-diff="1"[^>]*>(.*?)</script>',
        markup,
        re.S,
    )
    assert match, f"没有找到周版本页要读的 JSON 数据载荷节点:\n{markup[:600]}"
    return match.group(1)


class _TagCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend(name for name, _value in attrs)


def _parse(markup: str) -> _TagCollector:
    parser = _TagCollector()
    parser.feed(markup)
    return parser


class _FakeQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return None


class _Field:
    """列替身：`commit_id.like(...)` 与 `== 值` 都要能用（后者用于过滤条件拼装）。"""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return (self.name, "eq", other)

    def like(self, pattern):
        return (self.name, "like", pattern)


class _FakeCommitModel:
    """最小 Commit 模型替身：删除渲染要用它构造「上一个版本」链接的查询。"""

    repository_id = _Field("repository_id")
    path = _Field("path")
    commit_id = _Field("commit_id")
    query = _FakeQuery()


class TestDeletedPayloadFromBaseline:
    def test_every_row_of_the_baseline_becomes_a_removed_row(self):
        payload = build_deleted_excel_payload(
            file_path=DELETED_PATH, previous_content=BASELINE_BYTES, diff_service=DiffService()
        )

        assert payload and payload["sheets"], "删除前的内容没有生成任何工作表"
        sheet = payload["sheets"][SHEET_NAME]
        assert len(sheet["rows"]) == 3, (
            f"只留下 {len(sheet['rows'])} 行 —— 评审者看不到被删掉的是什么（线上就是只剩一句提示）"
        )
        assert all(row["status"] == "removed" for row in sheet["rows"])
        assert payload["summary"]["removed"] == 3
        assert sheet["rows"][0]["data"]["id"] == "TYPE", "行内容必须是基线版本的真实值"
        assert sheet["rows"][0]["row_number"] == 2, "行号是 Excel 真实行号（表头占第 1 行）"

    def test_missing_baseline_returns_none_instead_of_faking_a_deletion(self):
        """取不到基线字节（仓库没同步 / 路径对不上）时必须返回 None，
        让上层显示「已删除」提示 —— 渲染成「全表删除」等于让评审者把一次读取失败
        当成一次真实的删除确认掉（同 `DiffService.process_deleted_file` 的口径）。"""
        assert build_deleted_excel_payload(
            file_path=DELETED_PATH, previous_content=None, diff_service=DiffService()
        ) is None
        assert build_deleted_excel_payload(
            file_path=DELETED_PATH, previous_content=b"", diff_service=DiffService()
        ) is None

    def test_unreadable_bytes_return_none(self):
        assert build_deleted_excel_payload(
            file_path="config/坏文件.xlsx",
            previous_content=b"this is not an excel workbook",
            diff_service=DiffService(),
        ) is None


class TestDeletedExcelContentHtml:
    @staticmethod
    def _render(payload=None, file_path=DELETED_PATH, previous_commit_id="c2b53031"):
        from services.diff_render_helpers import render_excel_diff_html

        payload = payload or build_deleted_excel_payload(
            file_path=file_path, previous_content=BASELINE_BYTES, diff_service=DiffService()
        )
        return render_deleted_excel_content(
            commit_model=_FakeCommitModel,
            url_for=lambda *_a, **_k: "/should-not-be-used",
            config=SimpleNamespace(id=3, repository_id=7, project=SimpleNamespace(code="G119")),
            file_path=file_path,
            previous_commit_id=previous_commit_id,
            payload=payload,
            render_excel_html=render_excel_diff_html,
        )

    def test_banner_says_what_is_shown_and_links_the_previous_version(self):
        html = self._render()

        assert "Excel文件已删除" in html, "删除提示条不见了"
        assert "共 1 张工作表、3 行" in html, "提示条没有说明下面展示的是什么内容"
        assert "上一个版本 (c2b53031)" in html, "少了「上一个版本」的对照入口"
        assert "file-previous-version" in html and "commit_id=c2b53031" in html
        assert "删除前（c2b53031）" in html, "没有说明下面这份内容是删除前哪个版本的"

    def test_payload_is_what_the_weekly_page_parses_and_renders(self):
        """周版本页从 `[data-weekly-excel-diff]` 的 textContent 里 JSON.parse，
        再用 row.status / row.row_number / row.data[表头] 造行 —— 载荷必须齐。"""
        node = _payload_node(self._render())
        parsed = json.loads(node)

        sheet = parsed["sheets"][SHEET_NAME]
        assert sheet["headers"][:2] == ["id", "字段"]
        for row in sheet["rows"]:
            assert row["status"] == "removed", "被删的行状态不是 removed，渲染出来不会走删除行样式"
            assert isinstance(row["row_number"], int)
            assert set(sheet["headers"]) <= set(row["data"]), "行数据缺列，单元格会渲染成空白"

    def test_sheet_name_from_the_repo_cannot_break_out_of_the_payload(self):
        """表名来自被审核的 Excel（不可信）：`</script>` 不能提前闭合载荷标签。"""
        payload = {
            "type": "excel",
            "sheets": {
                "</script><img src=q onerror=window.__qa=1>": {
                    "has_changes": True,
                    "headers": ["id"],
                    "rows": [{"row_number": 2, "status": "removed", "data": {"id": "1"}}],
                }
            },
            "summary": {"added": 0, "removed": 1, "modified": 0, "total": 1},
        }
        html = self._render(payload=payload)

        parsed = _parse(html)
        assert "img" not in parsed.tags, f"表名里的标签被渲染成了真标签：{parsed.tags}"
        assert "onerror" not in parsed.attrs, f"表名顶出了事件属性：{parsed.attrs}"
        assert json.loads(_payload_node(html)) == payload, "载荷没有原样往返（页面会解析失败）"

    def test_previous_version_url_encodes_the_path_and_escapes_the_file_name(self):
        """文件名是仓库里的路径（不可信）：进 query 要 urlencode，进正文要转义。"""
        html = self._render(file_path='config/a&commit_id=evil"<b>.xlsx')

        parsed = _parse(html)
        assert "b" not in parsed.tags, f"文件名里的标签被渲染成了真标签：{parsed.tags}"
        href = re.search(r"href='([^']*file-previous-version[^']*)'", html)
        assert href, f"没有渲染出「上一个版本」链接:\n{html[:400]}"
        url = href.group(1)
        assert "&commit_id=evil" not in url and "&amp;commit_id=evil" not in url, (
            f"路径里的 `&` 逃进了 query，可以改写参数：{url}"
        )
        assert "%26commit_id%3Devil" in url, f"路径没有被 urlencode：{url}"


class TestReaderSelection:
    """基线读取器按仓库类型选：svn 仓库不能拿 git 的读法去读（读回来是 None）。"""

    def test_git_repository_uses_the_git_reader(self):
        from services.weekly_deleted_excel_helpers import read_deleted_excel_baseline

        seen = []
        content = read_deleted_excel_baseline(
            repository=SimpleNamespace(type="git"),
            file_path=DELETED_PATH,
            previous_commit_id="c2b53031",
            readers={"git": lambda *_a: seen.append("git") or BASELINE_BYTES,
                     "svn": lambda *_a: seen.append("svn") or b"svn"},
        )

        assert content == BASELINE_BYTES and seen == ["git"]

    def test_svn_repository_uses_the_svn_reader(self):
        from services.weekly_deleted_excel_helpers import read_deleted_excel_baseline

        seen = []
        read_deleted_excel_baseline(
            repository=SimpleNamespace(type="SVN"),
            file_path=DELETED_PATH,
            previous_commit_id="c2b53031",
            readers={"git": lambda *_a: seen.append("git") or BASELINE_BYTES,
                     "svn": lambda *_a: seen.append("svn") or b"svn"},
        )

        assert seen == ["svn"], "svn 仓库用了 git 的读法"

    def test_reader_failure_is_reported_but_does_not_raise(self):
        """VCS 读失败不能把整页变成异常页：返回 None，由上层给提示条。"""
        from services.weekly_deleted_excel_helpers import read_deleted_excel_baseline

        def _boom(*_args):
            raise RuntimeError("git 超时")

        assert read_deleted_excel_baseline(
            repository=SimpleNamespace(type="git"),
            file_path=DELETED_PATH,
            previous_commit_id="c2b53031",
            readers={"git": _boom},
        ) is None

    def test_no_previous_commit_means_no_read(self):
        from services.weekly_deleted_excel_helpers import read_deleted_excel_baseline

        called = []
        assert read_deleted_excel_baseline(
            repository=SimpleNamespace(type="git"),
            file_path=DELETED_PATH,
            previous_commit_id=None,
            readers={"git": lambda *_a: called.append(1)},
        ) is None
        assert called == []


class TestOrchestrator:
    """`render_weekly_deleted_excel`：两条分支的选择与注入。"""

    @staticmethod
    def _kwargs(**overrides):
        kwargs = dict(
            commit_model=_FakeCommitModel,
            url_for=lambda *_a, **_k: "/u",
            config=SimpleNamespace(id=3, repository_id=7, project=SimpleNamespace(code="G119")),
            file_path=DELETED_PATH,
            previous_commit_id="c2b53031",
            repository=SimpleNamespace(type="git"),
            readers={"git": lambda *_a: BASELINE_BYTES},
            diff_service=DiffService(),
            render_excel_html=lambda payload, path: "<div id='payload'></div>",
        )
        kwargs.update(overrides)
        return kwargs

    def test_content_branch_is_used_when_the_baseline_is_readable(self):
        from services.weekly_deleted_excel_helpers import render_weekly_deleted_excel

        html = render_weekly_deleted_excel(**self._kwargs())

        assert "Excel文件已删除" in html and "<div id='payload'></div>" in html
        assert "共 1 张工作表、3 行" in html

    def test_notice_branch_is_used_when_the_baseline_is_unavailable(self):
        from services.weekly_deleted_excel_helpers import render_weekly_deleted_excel

        notices = []
        html = render_weekly_deleted_excel(**self._kwargs(
            readers={"git": lambda *_a: None},
            render_notice=lambda **kw: notices.append(kw) or "<div id='notice'></div>",
        ))

        assert html == "<div id='notice'></div>", "取不到基线字节却没有退回提示条"
        assert notices and notices[0]["previous_commit_id"] == "c2b53031", (
            "退回提示条时没把「上一个版本」的版本号传下去 —— 评审者失去唯一的线索"
        )


class TestWeeklyLogicRendersDeletedContent:
    """接线侧：只把「删除判定 / 缓存 / VCS 读字节」换成假件，引擎与渲染都走真的。"""

    @staticmethod
    def _config():
        repository = SimpleNamespace(
            id=7, type="git", name="qz_config", project=SimpleNamespace(code="G119")
        )
        return SimpleNamespace(
            id=3, repository=repository, repository_id=7, project=SimpleNamespace(code="G119")
        )

    @pytest.fixture()
    def wired(self, monkeypatch):
        import services.weekly_version_logic as logic

        state = {"bytes": BASELINE_BYTES, "cached_html": None, "read_calls": []}

        class _FakeWeeklyCache:
            def get_cached_html(self, config_id, file_path, base_commit_id, latest_commit_id):
                state["cached_html_args"] = (config_id, file_path, base_commit_id, latest_commit_id)
                if state["cached_html"] is None:
                    return None
                return {"html_content": state["cached_html"]}

        def _reader(repository, commit_id, file_path):
            state["read_calls"].append((commit_id, file_path))
            return state["bytes"]

        monkeypatch.setattr(logic, "_resolve_weekly_deleted_excel_state", lambda *_a, **_k: (True, "b" * 8))
        monkeypatch.setattr(logic, "_weekly_excel_cache_service", _FakeWeeklyCache())
        monkeypatch.setattr(logic, "_get_file_content_from_git", _reader)
        monkeypatch.setattr(logic, "Commit", _FakeCommitModel)
        return state

    @staticmethod
    def _diff_cache():
        return SimpleNamespace(base_commit_id=None, latest_commit_id="a" * 40, merged_diff_data=None)

    def _run(self, path=DELETED_PATH):
        import services.weekly_version_logic as logic

        return logic.generate_weekly_excel_merged_diff_html(self._config(), self._diff_cache(), path)

    def test_deleted_file_renders_the_removed_rows(self, wired):
        html = self._run()

        assert "Excel文件已删除" in html, "删除提示条不见了"
        parsed = json.loads(_payload_node(html))
        rows = parsed["sheets"][SHEET_NAME]["rows"]
        assert len(rows) == 3 and all(row["status"] == "removed" for row in rows), (
            "删除态文件没有渲染出被删的内容 —— 页面又只剩一句「Excel文件已删除」"
        )
        assert wired["read_calls"] == [("b" * 8, DELETED_PATH)], (
            "没有按「删除它的那条提交之前的那一版」读基线字节"
        )

    def test_falls_back_to_the_notice_when_the_baseline_is_unreadable(self, wired):
        wired["bytes"] = None
        html = self._run()

        assert "Excel文件已删除" in html
        assert "data-weekly-excel-diff" not in html, (
            "取不到基线字节却渲染了一个空内容容器 —— 评审者会以为「删除但什么都没有」"
        )

    def test_cache_is_consulted_for_deleted_files_too(self, wired):
        """删除态文件同样进周版本 HTML 缓存（后台任务用同一个键写）。

        读缓存必须排在删除判定**之前**：否则每次打开页面都要把整份基线 Excel 重读重算，
        而后台任务写下的那一行永远没人读。
        """
        wired["cached_html"] = "<div id='from-cache'></div>"

        assert self._run() == "<div id='from-cache'></div>", (
            "删除态文件没有走 HTML 缓存 —— 每次访问都重算，且后台任务的缓存永远读不到"
        )
        assert wired["read_calls"] == [], "命中缓存却还是读了基线字节"
        assert wired["cached_html_args"][2] == "", (
            "查缓存时基线没有被规范成 ''（删除态文件没有 base_commit_id）"
        )
