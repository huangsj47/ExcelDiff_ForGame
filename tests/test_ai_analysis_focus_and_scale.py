# -*- coding: utf-8 -*-
"""文件量大时的取数口径：清单全列、白名单给全、范围可选。

## 这一组守的是什么

一个 767 个文件的周版本上，原来的做法是「按优先级取样 200 个」，而这 200 个**同时**
是提示词里列出的名字**和**模型能读的白名单。三件事一起坏：

1. **名字少**：模型只看到 200 个名字，另外 567 个连存在都不知道 —— 除非它猜路径。
2. **读不到**：那 567 个就算猜到了路径，`file_diff` 也会被 `sanitize_requests` 丢掉，
   因为白名单就是那 200 个。于是报告里那句「还有 567 个文件没看到」是**真的没办法**。
3. **额度分散**：12 次索取面对 767 个文件，本来就只够看个零头。

对应的三处改动：
* `list_files`（列出哪些名字）与 `delta_files`（白名单）拆开，后者给**全部**改动文件；
* 清单默认**全列**（767 个名字约 4.6 万字符，装得进预算），只有超 `MAX_LIST_CHARS` 才退化；
* 索取次数 12 → 20，并且**条数上限不得小于它**（否则取回来又被丢掉）。

再加一个「分析范围」：文件多的版本里，人比服务端的排序策略更清楚这周该看哪一半。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from services.ai.change_set import from_weekly_payload

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _uid(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _create_project():
    project = Project(code=_uid("P"), name=_uid("ai-project"))
    db.session.add(project)
    db.session.flush()
    return project


def _create_repo(project_id: int, name: str, resource_type: str) -> Repository:
    repo = Repository(
        project_id=project_id,
        name=name,
        type="git",
        url=f"https://example.com/{name}.git",
        branch="main",
        resource_type=resource_type,
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    return repo


def _create_weekly_config(project_id: int, repo: Repository, base_name: str):
    cfg = WeeklyVersionConfig(
        project_id=project_id,
        repository_id=repo.id,
        name=f"{base_name} - {repo.name}",
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
    db.session.flush()
    return cfg


def _seed(config: WeeklyVersionConfig, repo: Repository, path: str, *, age_hours: int = 2):
    db.session.add(
        WeeklyVersionDiffCache(
            config_id=config.id,
            repository_id=repo.id,
            file_path=path,
            file_type="lua" if path.endswith(".lua") else "excel",
            latest_commit_id="c" * 40,
            commit_count=1,
            updated_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        )
    )


@pytest.fixture()
def two_repos():
    """一个代码仓库（30 个 lua）+ 一个配表仓库（4 张表）的周版本。"""
    with app.app_context():
        create_tables()
        project = _create_project()
        code_repo = _create_repo(project.id, _uid("qz_luaworkspace"), "code")
        table_repo = _create_repo(project.id, _uid("qz_config"), "table")
        cfg_code = _create_weekly_config(project.id, code_repo, "W1")
        cfg_table = _create_weekly_config(project.id, table_repo, "W1")
        for i in range(30):
            _seed(cfg_code, code_repo, f"code/mod/Mod{i}.lua")
        for i in range(4):
            _seed(cfg_table, table_repo, f"config/{i}_奖励模式_CfgRewardMode{i}.xlsx")
        db.session.commit()
        yield {
            "project": project,
            "code_repo": code_repo,
            "table_repo": table_repo,
            "cfg_code": cfg_code,
            "cfg_table": cfg_table,
        }


# ==========================================================================
# 一、清单默认全列；白名单永远给全部
# ==========================================================================


def test_the_list_is_not_capped_at_max_files(monkeypatch, two_repos):
    """`max_files_per_run` 不再是「列多少」的硬上限。"""
    with app.app_context():
        monkeypatch.setattr(
            ai_service, "get_project_analysis_config",
            lambda *a, **k: {"max_files_per_run": 5},
        )
        payload, _state, skip = ai_service.build_weekly_payload(two_repos["cfg_code"].id)

        assert skip is None
        assert len(payload["list_files"]) == 34, (
            f"清单被 max_files_per_run 截断了：{len(payload['list_files'])}"
        )
        assert payload["delta_truncated"] is False


def test_the_whitelist_always_carries_every_changed_file(monkeypatch, two_repos):
    """清单可以少列，**白名单不能少给** —— 少给就是「读不到」。"""
    with app.app_context():
        monkeypatch.setattr(
            ai_service, "get_project_analysis_config",
            lambda *a, **k: {"max_files_per_run": 5},
        )
        monkeypatch.setattr(ai_service, "MAX_LIST_CHARS", 50)   # 逼出退化那一支
        payload, _state, _skip = ai_service.build_weekly_payload(two_repos["cfg_code"].id)

        assert len(payload["list_files"]) < 34, "没有退化，这条用例失去意义"
        assert len(payload["delta_files"]) == 34, (
            f"白名单跟着清单一起被砍了：{len(payload['delta_files'])}"
        )


# ==========================================================================
# 二、分析范围（focus）
# ==========================================================================


def test_focusing_on_table_repositories_keeps_only_them(two_repos):
    with app.app_context():
        payload, _state, skip = ai_service.build_weekly_payload(
            two_repos["cfg_code"].id, focus="table"
        )

        assert skip is None
        paths = [item["file_path"] for item in payload["delta_files"]]
        assert paths and all(path.startswith("config/") for path in paths), paths
        assert len(paths) == 4
        assert payload["focus"]["label"] == "仅配表仓库"
        # 计数必须跟着筛选走，否则提示词会说「共 34 个文件」而模型只看得到 4 个
        assert payload["summary"]["total_files"] == 4
        assert payload["summary"]["delta_files"] == 4


def test_focusing_on_code_repositories_keeps_only_them(two_repos):
    with app.app_context():
        payload, _state, _skip = ai_service.build_weekly_payload(
            two_repos["cfg_code"].id, focus="code"
        )

        paths = [item["file_path"] for item in payload["delta_files"]]
        assert len(paths) == 30 and all(path.endswith(".lua") for path in paths)
        assert payload["focus"]["label"] == "仅代码仓库"


def test_focusing_on_one_repository_uses_its_name(two_repos):
    with app.app_context():
        table_repo_id = two_repos["table_repo"].id
        payload, _state, _skip = ai_service.build_weekly_payload(
            two_repos["cfg_code"].id, focus=str(table_repo_id)
        )

        assert len(payload["delta_files"]) == 4
        assert two_repos["table_repo"].name in payload["focus"]["label"]


@pytest.mark.parametrize("focus", ["all", "", None, "不存在的范围", "999999", "table;drop"])
def test_an_unusable_focus_falls_back_to_no_filter(focus, two_repos):
    """`focus` 来自 URL，认不出来时必须退回「不筛」，不能把分析变成空跑或报错。"""
    with app.app_context():
        payload, _state, skip = ai_service.build_weekly_payload(
            two_repos["cfg_code"].id, focus=focus
        )

        assert skip is None
        assert len(payload["delta_files"]) == 34, f"focus={focus!r} 把清单筛没了"
        assert payload["focus"]["label"] == ""


def test_an_empty_focus_is_reported_instead_of_running_on_nothing(two_repos):
    """选中的范围里没有改动 → 明确返回 `focus_empty`，而不是跑一次空分析。

    注意分组是按 (project_id, start_time) 划的，所以「同一组的第三个仓库」要复用
    同一个 start_time 才会落进这个组 —— 这是周版本分组的既有口径。
    """
    with app.app_context():
        project = two_repos["project"]
        empty_repo = _create_repo(project.id, _uid("nochange"), "code")
        # 这个 config 对象本身用不到，但**必须建**：分组是按 (project_id, start_time) 划的，
        # 没有它，empty_repo 就不在这一组里，`focus` 也就筛不到任何东西。
        _create_weekly_config(project.id, empty_repo, "W1")
        db.session.commit()

        payload, _state, skip = ai_service.build_weekly_payload(
            two_repos["cfg_code"].id, focus=str(empty_repo.id)
        )

        assert payload is None
        assert skip == "focus_empty"
        # 不筛的话这个版本是有东西可分析的（34 个文件），所以要区分「没变化」与「筛没了」
        assert ai_service.build_weekly_payload(two_repos["cfg_code"].id)[2] is None


def test_the_prompt_says_which_range_was_analyzed(two_repos):
    """范围必须写进提示词：不写，模型会把「这个范围没问题」说成「本版本没问题」。"""
    with app.app_context():
        payload, _state, _skip = ai_service.build_weekly_payload(
            two_repos["cfg_code"].id, focus="table"
        )
        change = from_weekly_payload(payload, readable_references=())

        assert "仅配表仓库" in change.summary
        assert "不要把结论说成覆盖了整个版本" in change.summary


def test_an_unfiltered_run_says_nothing_about_a_range(two_repos):
    with app.app_context():
        payload, _state, _skip = ai_service.build_weekly_payload(two_repos["cfg_code"].id)
        change = from_weekly_payload(payload, readable_references=())

        assert "仅配表仓库" not in change.summary
        assert "只分析了" not in change.summary


# ==========================================================================
# 三、接线守卫（静态）
# ==========================================================================


def test_a_configured_request_cap_raises_the_item_cap_with_it():
    """索取次数调大时，条数上限必须跟着走。

    `enforce_budget` 按 `max_items` 裁掉多余的条目。两个默认值本来是齐的，但索取次数
    **用户可配**（取值上限 100）：用户在界面上把它调到超过默认值的那一刻，条数上限还停在
    默认值，于是「付了 N 次索取、只带走默认值那么多条」—— 取回来的东西被静默丢掉，
    而报告里看不出少了什么。这个下限必须跟着配置走，不能只在默认值上成立。
    """
    from services.ai.budget import DEFAULT_MAX_ITEMS
    from services.ai_analysis_service import _engine_limits

    over = DEFAULT_MAX_ITEMS + 20
    limits = _engine_limits({"max_tool_requests": over})
    assert limits.max_tool_requests == over
    assert limits.max_items >= over, f"条数上限没跟上：{limits.max_items}"

    # 调小时不反向跟着缩：默认的条数上限是有意的余量，不该被一个更小的索取次数削掉 ——
    # 模型一轮里可能一次要点好几份内容，条数上限管的是「一轮带得走多少」。
    smaller = _engine_limits({"max_tool_requests": 5})
    assert smaller.max_items == DEFAULT_MAX_ITEMS
    assert smaller.max_tool_requests == 5


def test_a_repo_without_a_resource_type_counts_as_code():
    """「只看代码仓库」必须与界面那一栏判的是同一件事。

    `models/repository.py` 写明 `resource_type` 取值是 `'table' / 'res' / 'code'`，
    而这一列**可空**（写入侧还有一条裸赋值会写进 NULL）。模板里的选项是
    `(cfg.repository.resource_type or 'code') == 'table'` —— **NULL 与 `'res'` 都算代码**。
    后端原先拿 `== "code"` 去比，`"" != "code"`，于是用户选「只看代码仓库」时那些
    `resource_type` 为空的仓库的改动**一条都不会进输入**，而报告上写着「仅代码仓库」，
    模型据此把一个缺口说成覆盖完整。
    """
    from types import SimpleNamespace

    from services.ai_analysis_service import _filter_delta_files_by_focus

    configs = [
        SimpleNamespace(repository_id=1, repository=SimpleNamespace(resource_type="code")),
        SimpleNamespace(repository_id=2, repository=SimpleNamespace(resource_type=None)),
        SimpleNamespace(repository_id=3, repository=SimpleNamespace(resource_type="res")),
        SimpleNamespace(repository_id=4, repository=SimpleNamespace(resource_type="table")),
    ]
    files = [{"file_path": f"f{i}.lua", "repository_id": i} for i in (1, 2, 3, 4)]

    code_kept, code_label = _filter_delta_files_by_focus(files, "code", configs)
    table_kept, table_label = _filter_delta_files_by_focus(files, "table", configs)

    assert [item["repository_id"] for item in code_kept] == [1, 2, 3], (
        "resource_type 为空 / 'res' 的仓库在界面上算代码仓库，后端也必须算"
    )
    assert [item["repository_id"] for item in table_kept] == [4]
    assert code_label == "仅代码仓库" and table_label == "仅配表仓库"


def test_a_zero_request_cap_stays_zero():
    """「上下文索取上限 = 0」是一条**明确的配置**，不是「没填」。

    取值范围就是 `0..100`，而 0 有专门的含义：平台为它写了两句话
    （`prompt._budget_line` 的「本次分析不允许索取上下文」与
    `protocol.build_budget_exhausted_hint` 的 0 分支）。这里原先写的是
    `int(project_config.get(...) or defaults)`，`0 or 40` 求值成 40 —— 那两句因此
    在生产路径上**永远执行不到**，模型照常点名读 40 份 diff 并计费。

    空与 0 要分开，两个方向都是：`None`（懒创建的行没填过）与空串回落到默认值，
    `0` 原样带上。
    """
    from services.ai_analysis_service import _engine_limits

    assert _engine_limits({"max_tool_requests": 0}).max_tool_requests == 0
    for missing in (None, "", "   "):
        assert _engine_limits({"max_tool_requests": missing}).max_tool_requests == 40
    assert _engine_limits({}).max_tool_requests == 40


def test_the_default_request_and_item_caps_agree():
    """两个默认值必须**相等**（不是「差不多」）。

    不等就会出现「付了 20 次索取、只带走 12 条」这类浪费 —— 拆开看每个数字都合理，
    合起来才是错的，所以只能这样钉。
    """
    from services.ai.budget import DEFAULT_MAX_ITEMS
    from services.ai.context_tools import DEFAULT_MAX_TOOL_REQUESTS

    assert DEFAULT_MAX_ITEMS == DEFAULT_MAX_TOOL_REQUESTS


def test_the_stream_route_reads_and_forwards_focus():
    """光有筛选不算数，URL 参数得真的传进去。"""
    source = open(
        os.path.join(PROJECT_ROOT, "routes", "ai_analysis_routes.py"), encoding="utf-8"
    ).read()
    assert 'request.args.get("focus"' in source, "路由没有读 focus 参数"
    assert "focus=focus" in source, "读到了 focus 却没有传给 stream_weekly_analysis"


def test_the_drawer_offers_a_range_selector_and_sends_it():
    """界面要能选范围，并且把它带进 stream 请求。"""
    source = open(
        os.path.join(PROJECT_ROOT, "templates", "weekly_version_diff.html"), encoding="utf-8"
    ).read()

    assert 'id="weeklyAiFocus"' in source, "抽屉里没有范围选择控件"
    assert "weeklyAiFocusValue()" in source, "没有读取所选范围的函数"
    assert "focus=${focus}" in source, "stream 请求里没有带上所选范围"
    # 「只影响下次分析」这件事要说清：否则用户会以为切换就能换掉已显示的结果
    assert "只影响下次分析" in source


def test_the_saved_result_shows_the_range_it_covers():
    """已保存结果的范围要显示出来 —— 它的结论只覆盖那一半。"""
    source = open(
        os.path.join(PROJECT_ROOT, "templates", "weekly_version_diff.html"), encoding="utf-8"
    ).read()
    assert "result.focus" in source, "界面没有读已保存结果的分析范围"


def test_the_read_path_reports_the_range_of_the_stored_run():
    """`/latest` 要把范围读出来给界面，读不到时按「全部」处理（老记录没有这个字段）。"""
    from types import SimpleNamespace

    run = SimpleNamespace(
        request_payload='{"focus": {"key": "table", "label": "仅配表仓库"}}'
    )
    assert ai_service._focus_from_run(run) == {"key": "table", "label": "仅配表仓库"}

    for raw in (None, "", "not json", "{}", '{"focus": 3}'):
        assert ai_service._focus_from_run(SimpleNamespace(request_payload=raw)) == {
            "key": "all", "label": "",
        }, f"request_payload={raw!r} 时没有退回「全部」"
