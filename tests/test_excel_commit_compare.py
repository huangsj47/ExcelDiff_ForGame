# -*- coding: utf-8 -*-
"""「Excel 版本对比」：候选提交列表 + 按需对比（后端口径、越权、方向、三态）。

## 这个功能原先是个空壳，所以要钉住的是「链路真的接上了」

`templates/commit_diff.html` 的 `#diffCompareModal` 里，`#targetCommitSelect`
全仓只有一个声明处、没有任何 JS 往里面塞过 option，`executeDiffCompare()` 的实现
是一句 `alert('版本对比功能开发中...')`。用户看到的就是
「Excel版本对比 里面的选择对比的提交版本: 没有任何可选内容」。

所以本文件的断言分三类，缺一类这个缺陷都能原样回来：

1. **候选列表的查询口径**（后端）：同仓库 + 同文件路径、排除当前提交、按时间倒序、
   标出「页面当前正在对比的那一条」。口径一旦与页面正文不一致，用户在下拉里选了
   页头写着的那一条却看到另一份差异。
2. **越权**：对比接口收一个「另一侧提交」的 id。只按 id 取那条提交的话，任何登录
   用户都能拿别的仓库 / 别的文件的 commit id 去读它的差异内容。
3. **前端真的填了下拉**：把模板里那段真实内联脚本抠出来，在 Node 里配最小 DOM 桩
   跑一遍 —— 「有没有数据来源」这件事只在运行时才看得出来。

## 关于测试库

测试库是**会话级共用**的（conftest 只建一次，没有逐用例重置），所以这里所有断言
都只针对本用例自己造的数据（仓库 id / 路径都是唯一的），不做任何全局 count 断言。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import jsonify

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import app, create_tables, db  # noqa: E402
from models import Commit, Project, Repository  # noqa: E402
from services.commit_compare_api_service import (  # noqa: E402
    CANDIDATE_LIMIT,
    DIRECTION_CURRENT_TO_TARGET,
    DIRECTION_TARGET_TO_CURRENT,
    handle_get_commit_compare_candidates,
    handle_get_commit_compare_diff,
)
from services.commit_diff_logic import resolve_previous_commit  # noqa: E402

TEMPLATE = "templates/commit_diff.html"
ROUTES = "routes/commit_diff_routes.py"
APP = "app.py"

CANDIDATES_PATH = "/commits/<int:commit_id>/compare-candidates"
DIFF_PATH = "/commits/<int:commit_id>/compare-diff"

BASE_TIME = datetime(2026, 5, 1, 2, 0, 0, tzinfo=timezone.utc)


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), encoding="utf-8") as handle:
        return handle.read()


def _strip_css_comments(text):
    """剥掉 /* ... */ 注释（保留换行，行号仍对得上）。"""
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)


def _noop_log(*_args, **_kwargs):
    return None


def _uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class _Graph:
    """一个项目 + 两个仓库（同名文件的两条路径）+ 若干提交。"""

    def __init__(self, namespace):
        self.__dict__.update(vars(namespace))


@pytest.fixture
def graph():
    """造一份最小图：两条仓库、同一个文件路径各自的历史。

    `repo_a` 上的 `path_a` 有 4 条提交（时间递增），`path_b` 上 1 条，
    `repo_b` 上同样路径 1 条 —— 后两者都是「不该出现在候选里」的对照组。
    """
    with app.app_context():
        create_tables()
        tag = uuid.uuid4().hex[:8]
        project = Project(code=f"XC{tag}", name="版本对比", department="QA")
        db.session.add(project)
        db.session.commit()

        repo_a = Repository(project_id=project.id, name=_uid("xa"), type="git",
                            url="https://example.invalid/a.git", resource_type="table")
        repo_b = Repository(project_id=project.id, name=_uid("xb"), type="git",
                            url="https://example.invalid/b.git", resource_type="table")
        db.session.add_all([repo_a, repo_b])
        db.session.commit()

        path_a = f"config/{tag}.xlsx"
        path_b = f"other/{tag}.xlsx"

        def _add(repo, path, index, *, message=None, author="alice", commit_id=None):
            commit = Commit(
                repository_id=repo.id,
                path=path,
                commit_id=commit_id or f"{tag}{index:0<32}"[:40],
                version=f"v1.0.{index}",
                operation="M",
                author=author,
                commit_time=BASE_TIME + timedelta(hours=index),
                message=message if message is not None else f"第 {index} 次改动",
                status="pending",
            )
            db.session.add(commit)
            db.session.commit()
            return commit

        # 时间递增：c1 最旧、c4 最新（也是「当前提交」）
        c1 = _add(repo_a, path_a, 1)
        c2 = _add(repo_a, path_a, 2)
        c3 = _add(repo_a, path_a, 3)
        c4 = _add(repo_a, path_a, 4)
        other_path = _add(repo_a, path_b, 5)
        other_repo = _add(repo_b, path_a, 6)

        yield _Graph(SimpleNamespace(
            project_id=project.id,
            repo_a=repo_a.id, repo_b=repo_b.id,
            path_a=path_a, path_b=path_b,
            c1=c1.id, c2=c2.id, c3=c3.id, c4=c4.id,
            other_path=other_path.id, other_repo=other_repo.id,
        ))


def _load(commit_id):
    return Commit.query.get(commit_id)


def _stub_access(commit):
    """替身：把「能拿到这条提交」直接当成有权限（权限本身另有断言钉住）。"""
    repository = commit.repository
    return repository, repository.project


def _candidates(commit_id, *, resolve=None):
    with app.test_request_context(f"/commits/{commit_id}/compare-candidates"):
        response = handle_get_commit_compare_candidates(
            commit_id=commit_id,
            jsonify=jsonify,
            Commit=Commit,
            ensure_commit_access_or_403=_stub_access,
            resolve_previous_commit=resolve or resolve_previous_commit,
            attach_author_display=lambda commits: [setattr(c, "author_display", f"显示名-{c.author}")
                                                   for c in commits],
            log_print=_noop_log,
        )
    payload, status = _json(response)
    assert payload is not None, "候选接口没有返回 JSON"
    assert status == 200
    return payload


def _compare_diff(commit_id, *, target_id, direction=None, diff_result=None, query=None):
    """直接调 handler；`diff_result` 用来替换真实的差异计算。"""
    calls = []

    def _fake_unified(new_commit, baseline_commit):
        calls.append((new_commit.id, baseline_commit.id if baseline_commit else None))
        return diff_result

    query_string = query if query is not None else (
        f"target_commit_id={target_id}" + (f"&direction={direction}" if direction else "")
    )
    with app.test_request_context(f"/commits/{commit_id}/compare-diff?{query_string}"):
        response = handle_get_commit_compare_diff(
            commit_id=commit_id,
            request=__import__("flask").request,
            jsonify=jsonify,
            Commit=Commit,
            ensure_commit_access_or_403=_stub_access,
            get_unified_diff_data=_fake_unified,
            attach_author_display=lambda commits: None,
            log_print=_noop_log,
        )
    return response, calls


def _json(response):
    """(payload, status)：handler 有时返回裸 jsonify、有时返回 (jsonify, status)。"""
    if isinstance(response, tuple):
        return response[0].get_json(), response[1]
    return response.get_json(), 200


# ==========================================================================
# 一、候选列表的查询口径
# ==========================================================================


class TestCandidateQueryScope:
    def test_only_same_repository_and_same_path(self, graph):
        """候选必须同时受 repository_id 与 path 约束。

        只按 path 过滤会带进别的仓库的同名文件（多仓库共用一套配表目录时很常见），
        只按 repository_id 过滤会带进这个仓库里别的文件 —— 两种都会让用户选到一个
        「根本不存在这个文件」的版本，然后拿到一份垃圾差异。
        """
        payload = _candidates(graph.c4)
        ids = {item["id"] for item in payload["candidates"]}

        assert ids == {graph.c1, graph.c2, graph.c3}, (
            f"候选集合不对：期望同一仓库同一路径的三条历史提交，实际 {sorted(ids)}"
        )
        assert graph.other_path not in ids, "候选里混进了同一个仓库里**别的文件**的提交"
        assert graph.other_repo not in ids, "候选里混进了**别的仓库**的提交"

    def test_excludes_the_current_commit(self, graph):
        payload = _candidates(graph.c4)
        assert graph.c4 not in {item["id"] for item in payload["candidates"]}, (
            "当前提交出现在候选里 —— 选中它就是「自己和自己比」，只会得到一份空差异"
        )

    def test_ordered_by_commit_time_desc(self, graph):
        payload = _candidates(graph.c4)
        times = [item["commit_time"] for item in payload["candidates"]]
        assert times == sorted(times, reverse=True), f"候选没有按提交时间倒序：{times}"
        assert [item["id"] for item in payload["candidates"]] == [graph.c3, graph.c2, graph.c1]

    def test_marks_the_version_the_page_is_comparing_against(self, graph):
        """「当前正在对比的那一条」必须被标出来。

        页面正文比的是 `c4` 对 `c3`（同文件同仓库里 c4 的前一条）。前端据此默认
        选中它 —— 不标的话默认选中的是「最近的一条」，用户直接点「开始对比」
        得到的差异与页面正文不是同一份。
        """
        payload = _candidates(graph.c4)
        flagged = [item["id"] for item in payload["candidates"] if item["is_page_baseline"]]
        assert flagged == [graph.c3], f"应且只应标出 c3，实际 {flagged}"
        assert payload["baseline_commit_id"] == _load(graph.c3).commit_id

    def test_marks_nothing_when_baseline_is_not_a_db_row(self, graph):
        """基线是「虚拟提交」（VCS 回退）时不该误标任何一条。"""
        virtual = SimpleNamespace(commit_id="ffffffff" + "0" * 32)
        payload = _candidates(graph.c4, resolve=lambda commit, file_commits=None: virtual)
        assert [item for item in payload["candidates"] if item["is_page_baseline"]] == []

    def test_each_candidate_carries_what_the_dropdown_needs(self, graph):
        item = _candidates(graph.c4)["candidates"][0]
        # 类型（不是具体值）：缺任何一个，下拉里那一项就会显示成一段没有信息的文本
        assert isinstance(item["short_id"], str) and len(item["short_id"]) == 8
        assert item["commit_id"].startswith(item["short_id"])
        assert item["version"] and item["commit_time"] and item["author"] and item["message"]
        assert item["author"] == "显示名-alice", "作者显示名没有走 attach_author_display"

    def test_multi_line_message_is_reduced_to_its_first_line(self, graph):
        """提交信息只取首行：不截断的话换行会把下拉项撑成两行。"""
        # 刻意**不**另开 app_context：`graph` 夹具的上下文还开着，另开一个会拿到
        # 另一个 session，外面这个 session 的身份映射里仍是改之前的那一行
        # （SQLAlchemy 不会用新行覆盖已在身份映射里的对象上已加载的属性）。
        commit = _load(graph.c3)
        commit.message = "标题行\n\n详细说明\n还有更多"
        db.session.commit()
        item = _candidates(graph.c4)["candidates"][0]
        assert item["message"] == "标题行"

    def test_truncation_keeps_the_newest_versions(self, graph, monkeypatch):
        """超过上限时截断的是**最旧的**那一端。

        写成 `[:LIMIT]`（在排序之后切片）与先切片再排序是两回事：后者会把
        「最近的提交」丢掉、留下最旧的一批，而下拉里用户要找的通常是近期版本。
        """
        monkeypatch.setattr("services.commit_compare_api_service.CANDIDATE_LIMIT", 2)
        payload = _candidates(graph.c4)
        assert payload["truncated"] is True
        assert [item["id"] for item in payload["candidates"]] == [graph.c3, graph.c2]
        assert payload["total"] == 3, "被截断时 total 必须是全量条数，否则界面上说不清有多少"

    def test_no_path_yields_an_empty_list_not_an_error(self, graph):
        with app.app_context():
            commit = _load(graph.c4)
            commit.path = None
            db.session.commit()
            payload = _candidates(graph.c4)
        assert payload["success"] is True
        assert payload["candidates"] == []


# ==========================================================================
# 二、越权与参数校验
# ==========================================================================


class TestTargetCommitScope:
    def test_target_from_another_repository_is_rejected(self, graph):
        response, calls = _compare_diff(graph.c4, target_id=graph.other_repo,
                                        diff_result={"type": "excel", "sheets": {"S": {}}})
        payload, status = _json(response)
        assert status == 404, f"别的仓库的提交必须被拒（实际 {status}）"
        assert payload["error_type"] == "target_commit_out_of_scope"
        assert calls == [], "越权的目标提交竟然走到了差异计算"

    def test_target_from_another_path_is_rejected(self, graph):
        response, calls = _compare_diff(graph.c4, target_id=graph.other_path,
                                        diff_result={"type": "excel", "sheets": {"S": {}}})
        payload, status = _json(response)
        assert status == 404, "同一个仓库里**别的文件**的提交也必须被拒"
        assert payload["error_type"] == "target_commit_out_of_scope"
        assert calls == []

    def test_target_equal_to_current_is_rejected(self, graph):
        payload, status = _json(_compare_diff(graph.c4, target_id=graph.c4,
                                              diff_result={"type": "excel", "sheets": {"S": {}}})[0])
        assert status == 400
        assert payload["error_type"] == "invalid_target_commit"

    def test_missing_target_parameter_is_rejected(self, graph):
        payload, status = _json(_compare_diff(graph.c4, target_id=None, query="")[0])
        assert status == 400
        assert payload["error_type"] == "missing_target_commit"

    def test_non_numeric_target_is_rejected(self, graph):
        payload, status = _json(_compare_diff(graph.c4, target_id="c3", query="target_commit_id=c3")[0])
        assert status == 400
        assert payload["error_type"] == "invalid_target_commit"

    def test_unknown_target_id_is_not_found(self, graph):
        payload, status = _json(_compare_diff(graph.c4, target_id=10 ** 9,
                                              diff_result={"type": "excel", "sheets": {"S": {}}})[0])
        assert status == 404
        assert payload["error_type"] == "target_commit_out_of_scope"

    def test_rejection_happens_before_any_diff_work(self, graph):
        """口径上「先校验再算」：算完再判会把别人的文件内容读进内存。"""
        source = _read("services/commit_compare_api_service.py")
        body = source[source.index("def handle_get_commit_compare_diff("):]
        assert body.index("resolve_target_commit(") < body.index("get_unified_diff_data("), (
            "目标提交的作用域校验必须排在差异计算之前"
        )


# ==========================================================================
# 三、方向与三态
# ==========================================================================


class TestDirectionSwapsTheTwoSides:
    def test_default_direction_puts_the_selected_commit_on_the_old_side(self, graph):
        """默认方向 = 「选择的提交（旧）→ 当前提交（新）」。

        这与页头「对比版本」的口径一致：选中当前正在对比的那一条时，结果与页面
        正文是同一份，用户不会觉得平台算了两套。
        """
        _, calls = _compare_diff(graph.c4, target_id=graph.c3,
                                 diff_result={"type": "excel", "sheets": {"S": {}}})
        assert calls == [(graph.c4, graph.c3)], (
            "默认方向下应当以「选择的提交」为基线（第二个参数）、当前提交为新侧"
        )

    @pytest.mark.parametrize(
        "direction, expected",
        [
            (DIRECTION_TARGET_TO_CURRENT, "new_is_current"),
            (DIRECTION_CURRENT_TO_TARGET, "new_is_target"),
        ],
    )
    def test_direction_really_swaps_new_and_baseline(self, graph, direction, expected):
        """方向必须真的换两侧。

        只把方向回显在响应里、而差异仍按同一侧算，是这个功能最容易出现的假实现：
        界面上「A → B」的头换了，表体里的新增/删除行却一点没变。
        """
        _, calls = _compare_diff(graph.c4, target_id=graph.c3, direction=direction,
                                 diff_result={"type": "excel", "sheets": {"S": {}}})
        new_id, baseline_id = calls[0]
        if expected == "new_is_current":
            assert (new_id, baseline_id) == (graph.c4, graph.c3)
        else:
            assert (new_id, baseline_id) == (graph.c3, graph.c4)

    def test_response_swaps_from_and_to_too(self, graph):
        """响应里的 A（旧）/ B（新）也要跟着方向换，否则页头与表体对不上。"""
        response, _ = _compare_diff(graph.c4, target_id=graph.c3,
                                    direction=DIRECTION_TARGET_TO_CURRENT,
                                    diff_result={"type": "excel", "sheets": {"S": {}}})
        payload, _ = _json(response)
        assert payload["from_commit"]["id"] == graph.c3
        assert payload["to_commit"]["id"] == graph.c4

        response, _ = _compare_diff(graph.c4, target_id=graph.c3,
                                    direction=DIRECTION_CURRENT_TO_TARGET,
                                    diff_result={"type": "excel", "sheets": {"S": {}}})
        payload, _ = _json(response)
        assert payload["from_commit"]["id"] == graph.c4
        assert payload["to_commit"]["id"] == graph.c3

    def test_unknown_direction_falls_back_to_the_default(self, graph):
        _, calls = _compare_diff(graph.c4, target_id=graph.c3, direction="sideways",
                                 diff_result={"type": "excel", "sheets": {"S": {}}})
        assert calls == [(graph.c4, graph.c3)]


class TestEmptyAndErrorStates:
    def test_no_sheets_is_an_empty_state_not_a_500(self, graph):
        """「没有可比较的工作表」是一条空态，不是服务端错误。

        反过来说：把它当 500 处理，前端只能显示「加载失败 + 重试」，而重试永远
        不会成功 —— 用户会以为平台坏了。
        """
        payload, status = _json(_compare_diff(graph.c4, target_id=graph.c3,
                                              diff_result={"type": "excel", "sheets": {}})[0])
        assert status == 200
        assert payload["success"] is True
        assert payload["empty"] is True
        assert payload["diff_data"] is None
        assert payload["empty_reason"]

    def test_sheets_with_no_changes_is_not_reported_as_empty(self, graph):
        """「两边内容一样」不是空态：载荷有工作表，只是每张都没变更。

        后端把它当空态的话，前端就没有机会把「逐表比对后确实没差异」这句话说出来，
        只能显示「没有可比较的工作表」—— 两件不同的事被说成一件。
        """
        diff_data = {"type": "excel", "sheets": {"Sheet1": {"headers": ["id"], "rows": []}}}
        payload, status = _json(_compare_diff(graph.c4, target_id=graph.c3, diff_result=diff_data)[0])
        assert status == 200
        assert payload["empty"] is False
        assert payload["diff_data"] == diff_data

    def test_none_from_the_diff_service_is_an_error(self, graph):
        payload, status = _json(_compare_diff(graph.c4, target_id=graph.c3, diff_result=None)[0])
        assert status == 200
        assert payload["empty"] is True, "空载荷要走空态，而不是把 None 直接丢给前端渲染"

    def test_error_payload_from_the_diff_service_is_surfaced(self, graph):
        payload, status = _json(_compare_diff(
            graph.c4, target_id=graph.c3,
            diff_result={"type": "error", "message": "仓库内容读取失败"})[0])
        assert status == 200
        assert payload["empty"] is True
        assert "仓库内容读取失败" in payload["empty_reason"], (
            "差异服务给出的原因必须透出来（只说「没有可比较的工作表」会掩盖真实故障）"
        )

    def test_diff_service_exception_becomes_a_retryable_error(self, graph):
        def _boom(_new, _baseline):
            raise RuntimeError("VCS 超时")

        with app.test_request_context(f"/commits/{graph.c4}/compare-diff?target_commit_id={graph.c3}"):
            from flask import request as flask_request
            response = handle_get_commit_compare_diff(
                commit_id=graph.c4,
                request=flask_request,
                jsonify=jsonify,
                Commit=Commit,
                ensure_commit_access_or_403=_stub_access,
                get_unified_diff_data=_boom,
                attach_author_display=lambda commits: None,
                log_print=_noop_log,
            )
        payload, status = _json(response)
        assert status == 500
        assert payload["error_type"] == "compare_diff_failed"
        assert "VCS 超时" in payload["message"]


# ==========================================================================
# 四、鉴权口径（静态 + 真请求）
# ==========================================================================


class TestAuthWiring:
    def test_service_handlers_check_commit_access(self):
        source = _read("services/commit_compare_api_service.py")
        assert source.count("ensure_commit_access_or_403(commit)") == 2, (
            "两条 handler 都必须先判这条提交所在仓库/项目的访问权限"
        )

    def test_scoped_handlers_validate_path_params(self):
        """带 project_code / repository_name 的那一份必须过作用域校验。

        不过这一关的话，`/别的项目代号/别的仓库名/commits/<我的 id>/compare-diff`
        就能读到不属于那条路径的内容。
        """
        source = _read(APP)
        for name in ("get_commit_compare_candidates_with_path", "get_commit_compare_diff_with_path"):
            body = source[source.index(f"def {name}("):]
            body = body[:body.index("\ndef ", 1)]
            assert "dispatch_commit_route_with_scope(" in body
            assert "ensure_commit_route_scope_or_404_func=_ensure_commit_route_scope_or_404" in body

    def test_routes_are_registered_with_both_shapes(self):
        source = _read(ROUTES)
        assert CANDIDATES_PATH in source
        assert DIFF_PATH in source
        assert "/<project_code>/<repository_name>/commits/<int:commit_id>/compare-candidates" in source
        assert "/<project_code>/<repository_name>/commits/<int:commit_id>/compare-diff" in source

    def test_anonymous_request_is_refused(self, graph):
        """真发一次请求：没有项目权限的人拿不到候选列表。

        这条是整条链的回归闸门 —— 静态断言只保证「代码里调了鉴权函数」，
        真请求才能证明它在这次访问里确实生效。允许两种拒绝形态：重定向到登录页
        （302，平台未登录时的默认行为）或硬拒绝（401/403）。不允许的是
        「200 并且带着候选数据」。
        """
        with app.test_client() as client:
            response = client.get(f"/commits/{graph.c4}/compare-candidates")
        assert response.status_code in (301, 302, 401, 403), (
            f"匿名访问拿到了 {response.status_code} —— 候选列表（含作者、提交信息）泄露了"
        )
        assert b"is_page_baseline" not in response.data, "拒绝响应里仍然带着候选数据"

    def test_anonymous_request_to_the_scoped_route_is_refused(self, graph):
        with app.app_context():
            repository = _load(graph.c4).repository
            project_code = repository.project.code
            repo_name = repository.name
        with app.test_client() as client:
            response = client.get(
                f"/{project_code}/{repo_name}/commits/{graph.c4}/compare-candidates"
            )
        assert response.status_code in (301, 302, 401, 403, 404)
        assert b"is_page_baseline" not in response.data


# ==========================================================================
# 五、前端：下拉真的会被填上 option
# ==========================================================================


def _compare_script():
    """从模板里抠出「Excel 版本对比」那段真实内联脚本（只取 is_excel 那一支）。"""
    source = _read(TEMPLATE)
    start = source.index("/* Excel版本对比功能")
    end = source.index("{% else %}", start)
    block = source[start:end]
    block = block.replace("{% if is_excel %}", "")
    # Jinja 的 url_for 在 Node 里没有，替换成占位常量（值不影响被测逻辑）
    block = re.sub(r"var COMPARE_CANDIDATES_URL = \{\{.*?\}\};",
                   "var COMPARE_CANDIDATES_URL = '/compare-candidates';", block, flags=re.S)
    block = re.sub(r"var COMPARE_DIFF_URL = \{\{.*?\}\};",
                   "var COMPARE_DIFF_URL = '/compare-diff';", block, flags=re.S)
    assert "url_for" not in block, "还有没替换掉的 Jinja 表达式，Node 会直接语法错误"
    return block


_NODE_STUB = r"""
// ---- 最小 DOM 桩：只实现被测函数用到的那一小撮 API ----
class StubOption {
  constructor(text, value) { this.text = String(text); this.value = String(value); }
}
class StubElement {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this._textContent = '';
    this.innerHTML = '';
    this.tabIndex = 0;
    this.attributes = {};
  }
  // textContent 在真实 DOM 里会把子节点一起清掉；桩必须照做 ——
  // renderCompareOptions 就是靠 `select.textContent = ''` 清空旧 option 的，
  // 桩不清的话每渲染一次就多留一批旧项，而断言的正是「有没有多出来」。
  get textContent() { return this._textContent; }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  appendChild(node) { this.children.push(node); node.parentNode = this; return node; }
  setAttribute(name, value) { this.attributes[String(name)] = String(value); }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  addEventListener() {}
  querySelectorAll() { return []; }
  querySelector() { return null; }
  focus() {}
}
const byId = {};
function defineEl(id) { byId[id] = new StubElement('div'); return byId[id]; }
globalThis.document = {
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new StubElement(tag),
  addEventListener: () => {},
  querySelector: () => null,
  querySelectorAll: () => [],
};
globalThis.Option = StubOption;
globalThis.window = globalThis;
"""

_NODE_DRIVER = r"""
const select = defineEl('targetCommitSelect');
const filter = defineEl('compareCommitFilter');
defineEl('compareCandidateHint');
defineEl('diffCompareFootNote');
defineEl('diffCompareSubmitBtn');
const report = {};

function optionTexts() { return select.children.map((o) => String(o.text)); }
function optionValues() { return select.children.map((o) => String(o.value)); }

const CANDIDATES = __CANDIDATES__;
diffCompareState.candidates = CANDIDATES;
diffCompareState.total = CANDIDATES.length;
diffCompareState.truncated = false;

// 1) 列表填充：每一项一个 option，值是提交记录 id
renderCompareOptions();
report.filledCount = select.children.length;
report.values = optionValues();
report.texts = optionTexts();
report.selected = select.value;
report.hint = document.getElementById('compareCandidateHint').textContent;
report.disabled = select.disabled;

// 2) 默认选中「页面当前正在对比的那一条」
report.baselineSelected = String(select.value) === '3';

// 3) 过滤：只重建 option，不改变控件语义
filter.value = '2026-05-01 02:00';
renderCompareOptions();
report.filteredCount = select.children.length;
report.filteredTexts = optionTexts();
report.filteredDisabled = select.disabled;
report.filteredHint = document.getElementById('compareCandidateHint').textContent;

// 4) 过滤到一个都不剩：给一条说明性 option 并禁用提交
filter.value = '这个关键词不存在';
renderCompareOptions();
report.noMatchCount = select.children.length;
report.noMatchText = optionTexts()[0];
report.noMatchDisabled = select.disabled;

// 5) 用户已选的项在重新过滤后要被保住（否则每敲一个字选择就跳走）
filter.value = 'v1.0.3';
renderCompareOptions();
select.value = optionValues()[0];
const chosen = String(select.value);
filter.value = '';
renderCompareOptions();
report.keptSelection = String(select.value) === chosen;

// 6) 空候选：不是空白页，而是一条说明 + 禁用
diffCompareState.candidates = [];
diffCompareState.total = 0;
renderCompareOptions();
report.emptyCount = select.children.length;
report.emptyText = optionTexts()[0];
report.emptyDisabled = select.disabled;
report.emptyHint = document.getElementById('compareCandidateHint').textContent;

process.stdout.write(JSON.stringify(report));
"""


def _run_compare_js(candidates):
    """在 Node 里跑模板里的真实脚本。

    DOM 桩、被测脚本、驱动脚本**都在同一个 vm 上下文里**执行：桩定义的是
    context 的全局（`document` / `Option` / `window`），在外层脚本里定义的话
    被测脚本根本看不见（第一版就栽在这里，报 ReferenceError）。
    三段都用 JSON 编码嵌进外层脚本，避免模板里的引号/换行破坏外层语法。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过前端填充逻辑验证")

    driver = _NODE_DRIVER.replace("__CANDIDATES__", json.dumps(candidates))
    script = (
        "const vm = require('vm');\n"
        "const sandbox = {console: {log() {}, warn() {}, error() {}}, process: process};\n"
        "vm.createContext(sandbox);\n"
        f"vm.runInContext({json.dumps(_NODE_STUB)}, sandbox, {{filename: 'dom_stub.js'}});\n"
        f"vm.runInContext({json.dumps(_compare_script())}, sandbox, {{filename: 'commit_diff_compare.js'}});\n"
        f"vm.runInContext({json.dumps(driver)}, sandbox, {{filename: 'driver.js'}});\n"
    )
    proc = subprocess.run([node, "-"], input=script, capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, (
        "跑版本对比的前端填充逻辑时 Node 报错：\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


def _candidate_fixture(count=3):
    out = []
    for index in range(1, count + 1):
        out.append({
            "id": index,
            "commit_id": f"abcdef{index:02d}" + "0" * 30,
            "short_id": f"abcdef{index:02d}",
            "version": f"v1.0.{index}",
            "operation": "M",
            "commit_time": f"2026-05-01 0{index % 10}:00:00",
            "author": "张三",
            "message": f"第 {index} 次改动",
            "is_page_baseline": index == 3,
        })
    return out


class TestDropdownIsReallyFilled:
    """这一组直接跑模板里的真实脚本 —— 「有没有数据来源」只有跑起来才看得见。"""

    def test_every_candidate_becomes_an_option(self):
        report = _run_compare_js(_candidate_fixture())
        assert report["filledCount"] == 3, (
            "下拉没有被填满 —— 这正是线上「没有任何可选内容」的形态"
        )
        assert report["values"] == ["1", "2", "3"], "option 的 value 必须是提交记录 id"

    def test_option_text_carries_id_version_time_author_and_message(self):
        report = _run_compare_js(_candidate_fixture())
        text = report["texts"][0]
        for needle in ("abcdef01", "v1.0.1", "2026-05-01 01:00:00", "张三", "第 1 次改动"):
            assert needle in text, f"选项文本里缺少「{needle}」：{text}"

    def test_the_baseline_candidate_is_preselected(self):
        report = _run_compare_js(_candidate_fixture())
        assert report["baselineSelected"] is True
        assert "[当前对比版本]" in report["texts"][2]

    def test_filter_narrows_without_losing_keyboard_semantics(self):
        report = _run_compare_js(_candidate_fixture())
        assert report["filteredCount"] == 1, "过滤没有生效"
        assert report["filteredDisabled"] is False, "过滤后仍有命中，下拉不该被禁用"
        assert "匹配" in report["filteredHint"]

    def test_no_match_explains_instead_of_showing_an_empty_box(self):
        report = _run_compare_js(_candidate_fixture())
        assert report["noMatchCount"] == 1
        assert report["noMatchText"], "无命中时下拉是空的 —— 用户看到的是一个没有内容的控件"
        assert report["noMatchDisabled"] is True, "无命中时不能提交"

    def test_selection_survives_a_refilter(self):
        report = _run_compare_js(_candidate_fixture())
        assert report["keptSelection"] is True

    def test_empty_candidate_list_says_why(self):
        report = _run_compare_js([])
        assert report["emptyCount"] == 1
        assert report["emptyText"]
        assert report["emptyDisabled"] is True
        assert report["emptyHint"], "没有可选版本时也要说清原因"


class TestTemplateContract:
    def test_candidates_are_fetched_lazily_when_the_modal_opens(self):
        """候选列表只在弹窗第一次打开时拉。

        写在页面加载里的话，每个打开 diff 页的人都要为「可能不会点开的下拉」
        付一次「同仓库同路径全部历史提交」的查询。
        """
        source = _read(TEMPLATE)
        assert "addEventListener('show.bs.modal'" in source
        body = source[source.index("document.addEventListener('DOMContentLoaded'"):]
        body = body[:body.index("window.executeDiffCompare")]
        assert "loadDiffCompareCandidates(false)" in body, "弹窗打开时没有触发候选加载"
        assert "COMPARE_CANDIDATES_URL" in body

    def test_select_is_only_ever_filled_through_the_shared_renderer(self):
        """下拉的 option 只由 renderCompareOptions 产生。

        历史上这里是「模板里写一个占位 option、没有任何代码填它」——所以这条断言
        盯的是「option 的插入点存在且有唯一来源」，而不是某个字符串在不在。
        """
        source = _read(TEMPLATE)
        assert source.count("targetCommitSelect") >= 2, "下拉只出现在模板声明里，没有任何消费方"
        candidates = _compare_script()
        assert "renderCompareOptions" in candidates
        assert "new Option(" in candidates

    def test_compare_result_uses_the_single_table_implementation(self):
        """表体必须走 `ExcelDiffTable.mountSheetTable`，且本页不许自己拼单元格。

        本页历史上自己拼过一份单元格（表头两行、转义口径各不相同），2026 收进了
        `static/js/excel_diff_table.js`；在对比区里再拼一份等于把那次重构撤销。
        （`.excel-cell` 的真正闸门在 tests/test_excel_cell_max_width.py，这里只补
        「对比区这一条新路径」也必须走共享实现。）
        """
        script = _compare_script()
        assert "ExcelDiffTable.mountSheetTable(" in script
        assert "<td" not in script, "对比区自己在拼单元格"
        assert "<table" not in script, "对比区自己在拼表格"

    def test_sheet_tabs_do_not_carry_sheet_names_into_code_positions(self):
        """工作表名来自被审核的 Excel：只作为文本与数字下标出现，不进选择器/onclick。"""
        script = _compare_script()
        assert "data-sheet-index=" in script
        assert "escapeHtml(name)" in script
        assert "querySelector('.dc-sheet-tab[data-sheet" not in script

    def test_result_has_an_explicit_headline_and_a_way_back(self):
        """对比态必须说清「A（时间）→ B（时间）」，并且有返回当前提交对比的出口。"""
        source = _read(TEMPLATE)
        assert "dc-compare__route" in source
        assert "A · 旧" in source and "B · 新" in source
        assert "exitDiffCompare" in source
        assert "返回当前提交对比" in source

    def test_all_three_states_exist(self):
        source = _read(TEMPLATE)
        for element_id in ("diffCompareLoading", "diffCompareEmpty", "diffCompareResultError"):
            assert element_id in source, f"缺少 {element_id}（加载/空/错误三态）"
        assert "retryDiffCompare" in source, "错误态没有重试入口"
        assert "这两个版本之间没有差异" in source, "两边内容一样时没有说清「无差异」"

    def test_entering_compare_mode_hides_the_current_diff_card(self):
        script = _compare_script()
        assert "currentCommitDiffCard" in script
        assert "card.hidden = true" in script
        source = _read(TEMPLATE)
        assert 'id="currentCommitDiffCard"' in source


class TestVisualContract:
    """用户点名的那句「UI没有设计感」——把当初的硬约束钉在这里。"""

    def test_no_bare_hex_colours_in_the_new_styles(self):
        """新增样式里的颜色必须走令牌，不许写裸 hex。

        先剥注释再扫：本文件顶部的说明里原样引用了几个 hex（「实测对比度」那几行），
        不剥的话会把说明文字当成违规声明 —— 这是本仓库反复踩过的坑
        （静态断言必须先剥注释，注释里会原样引用要禁掉的写法）。
        """
        source = _read(TEMPLATE)
        style = source[source.index("<style>"):source.index("</style>")]
        # 只看本页新增的那段（旧的 diff 样式里有历史裸 hex，不在本次范围内）。
        # 标记本身写在注释里，所以要从那条注释的**结尾**开始切，再剥注释 ——
        # 从标记处直接切的话，那段注释的 `/*` 落在切片之外，剥不掉。
        marker_at = style.index("Excel 版本对比（弹窗 + 结果区）")
        added = _strip_css_comments(style[style.index("*/", marker_at) + 2:])
        offenders = [
            line.strip()
            for line in added.splitlines()
            if re.search(r"#[0-9a-fA-F]{3,8}\b", line) and "var(--" not in line
        ]
        assert offenders == [], f"新样式里出现了裸 hex：{offenders}"

    def test_icons_come_from_the_icon_font_the_page_already_loads(self):
        """本页只引了 Bootstrap Icons；base.html 只引 FontAwesome。"""
        source = _read(TEMPLATE)
        assert "bootstrap-icons" in source, "用了 bi bi-* 却没引 bootstrap-icons，图标会是隐形的"
        block = source[source.index('id="diffCompareResult"'):source.index("<!-- Excel版本对比模态框")]
        assert "bi bi-" in block

    def test_touch_targets_focus_ring_and_reduced_motion_are_declared(self):
        source = _read(TEMPLATE)
        style = source[source.index("<style>"):source.index("</style>")]
        added = style[style.index("Excel 版本对比（弹窗 + 结果区）"):]
        assert "min-height: 44px" in added, "触控目标没有达到 44px"
        assert ":focus-visible" in added, "新控件没有焦点环"
        assert "outline: 3px solid" in added, "焦点环不在 2-4px 区间"
        assert "prefers-reduced-motion: reduce" in added
        assert "tabular-nums" in added, "时间/提交号这类数据列没有用等宽数字"

    def test_mobile_breakpoint_avoids_horizontal_scroll(self):
        added = _read(TEMPLATE)
        added = added[added.index("Excel 版本对比（弹窗 + 结果区）"):]
        assert "@media (max-width: 575.98px)" in added
        assert "flex-wrap: wrap" in added
