# -*- coding: utf-8 -*-
"""同一个 `latest_commit_id` 出现在两个周窗口里时，AI 必须读**触发它的那一份**缓存
（REV-AI-003）。

## 缺陷

`WeeklyVersionDiffCache` 是**按 `config_id` 写的**（一个周版本一行，
`weekly_version_logic.generate_weekly_merged_diff` 用 `filter_by(config_id, file_path)`
更新/新建）。而 AI 读取侧原先只按

    (repository_id, file_path, latest_commit_id) + order_by(id.desc()).first()

查 —— **没有 config_id**。两个周窗口的终点撞上同一条提交时（回填日期的提交、同一时刻
的两条提交都会造成这个），它就是在猜，而且会猜中**后建的那个窗口**。

这不是理论问题：`docs/AI分析使用说明.md` 明确承诺「周版本分析优先读平台已经算好并落库的
合并差异（**就是周版本页面上那一份**）」，而页面走的是 `filter_by(config_id, file_path)`。
同窗同提交时，**用户看到的和 AI 读到的就是两份东西**。

## 修法

写侧（`scope_sampling._summarize_weekly_files`）把来源**行主键**一并写进每条 delta，
取数侧按主键取；没带主键（老 payload）时退回旧查询，但**命中多行就拒绝猜测**。
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from app import app, create_tables, db
from models import Commit, Project, Repository
from models.weekly_version import WeeklyVersionConfig, WeeklyVersionDiffCache
from services.ai.platform_provider import PlatformContextProvider
from services.ai.stored_diff_source import _weekly_stored_diff

LUA = "code/qz_pub/battle/BattleMgr.lua"
SHARED_SHA = uuid.uuid4().hex + uuid.uuid4().hex[:8]
EXPECTED = "EXPECTED_WINDOW_A"
WRONG = "WRONG_WINDOW_C"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _cache_payload(marker: str) -> str:
    inner = {"type": "code", "file_path": LUA, "patch": f"@@ -1 +1 @@\n-{marker}\n", "hunks": []}
    return json.dumps({
        "file_path": LUA, "file_type": "text", "base_commit": "",
        "latest_commit": SHARED_SHA, "commits_count": 1, "commit_ids": [SHARED_SHA],
        "operations": ["M"], "merge_strategy": "single",
        "diff_data": inner, "merged_diff": inner,
    })


def _seed():
    """同仓同文件的**两个周窗口**，`latest_commit_id` 相同，缓存行内容不同。

    **wanted 那行先建、另一窗口那行后建** —— 后建的自增 id 更大，正是
    `order_by(id.desc())` 会猜中它的形状。
    """
    now = datetime.now(timezone.utc)
    project = Project(code=_uid("P"), name="周窗口身份")
    db.session.add(project)
    db.session.flush()
    repository = Repository(
        project_id=project.id, name=_uid("repo"), type="git",
        url="https://example.invalid/x.git", branch="main",
        resource_type="code", clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()
    db.session.add(Commit(repository_id=repository.id, commit_id=SHARED_SHA, path=LUA,
                          operation="M", author="a", commit_time=now, message="改动"))

    wanted = WeeklyVersionConfig(
        project_id=project.id, repository_id=repository.id, name=_uid("wanted-week"),
        branch="main", start_time=now - timedelta(days=7), end_time=now,
    )
    other = WeeklyVersionConfig(
        project_id=project.id, repository_id=repository.id, name=_uid("other-week"),
        branch="main", start_time=now - timedelta(days=14), end_time=now,
    )
    db.session.add_all([wanted, other])
    db.session.flush()

    row_wanted = WeeklyVersionDiffCache(
        config_id=wanted.id, repository_id=repository.id, file_path=LUA,
        file_type="code", latest_commit_id=SHARED_SHA,
        merged_diff_data=_cache_payload(EXPECTED), cache_status="completed",
    )
    row_other = WeeklyVersionDiffCache(
        config_id=other.id, repository_id=repository.id, file_path=LUA,
        file_type="code", latest_commit_id=SHARED_SHA,
        merged_diff_data=_cache_payload(WRONG), cache_status="completed",
    )
    db.session.add_all([row_wanted, row_other])
    db.session.commit()
    return repository.id, row_wanted.id, row_other.id


def _provider(*, cache_row_ids) -> PlatformContextProvider:
    return PlatformContextProvider(
        loaded=type("L", (), {"readable": {}, "skills": {}, "dimensions": ()})(),
        use_stored_batch_diff=True,
        cache_row_ids=cache_row_ids,
    )


def test_the_wanted_window_row_is_the_one_read():
    with app.app_context():
        create_tables()
        repository_id, wanted_row_id, _ = _seed()

        text = _provider(
            cache_row_ids={(repository_id, SHARED_SHA, LUA): wanted_row_id}
        ).file_diff(SHARED_SHA, LUA)

        assert text is not None, "本窗口确实有缓存行，不该读不到"
        assert EXPECTED in text
        assert WRONG not in text, (
            "读到了另一个周窗口那一份 —— 用户页面按 config_id 取，AI 按 (仓库,路径,提交)"
            "取 id 最大的那行，两者同窗同提交时不是同一份东西"
        )


def test_the_other_window_row_is_the_one_read_when_it_is_the_wanted_one():
    """反方向：换一条来源行，读到的必须是换过去的那一份。"""
    with app.app_context():
        create_tables()
        repository_id, _, other_row_id = _seed()

        text = _provider(
            cache_row_ids={(repository_id, SHARED_SHA, LUA): other_row_id}
        ).file_diff(SHARED_SHA, LUA)

        assert text is not None and WRONG in text and EXPECTED not in text


def test_without_a_row_id_an_ambiguous_hit_is_refused_instead_of_guessed():
    """老 payload（没带来源行）遇到多行时**拒绝猜测**，不许悄悄给另一窗口那一份。"""
    with app.app_context():
        create_tables()
        repository_id, _, _ = _seed()

        payload, provenance = _weekly_stored_diff(repository_id, LUA, SHARED_SHA)

        assert payload is None and provenance == "", (
            "两条缓存同键时旧查询取 id 最大的那行 —— 那是在猜，猜中的窗口看起来完全正常"
        )


def test_an_unambiguous_hit_without_a_row_id_still_works():
    """反方向：只有一条时旧路径照常给内容（老 payload 不许整成取不到）。"""
    with app.app_context():
        create_tables()
        repository_id, wanted_row_id, other_row_id = _seed()
        db.session.delete(db.session.get(WeeklyVersionDiffCache, other_row_id))
        db.session.commit()

        payload, _ = _weekly_stored_diff(repository_id, LUA, SHARED_SHA)

        assert payload is not None and payload.get("patch", "").find(EXPECTED) >= 0
