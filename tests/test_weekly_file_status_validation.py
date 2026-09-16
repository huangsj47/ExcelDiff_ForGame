# -*- coding: utf-8 -*-
"""周版本「文件状态」只接受 {pending, confirmed, rejected}。

## 为什么需要这个测试

`POST /weekly-version-config/<id>/file-status`（services/weekly_version_file_handlers.py
::weekly_version_file_status_api）只判了 `not status`（空/None），**没有判取值**：

    status = data.get("status")
    if not file_path or not status:
        return jsonify({"success": False, "message": "缺少必需参数"}), 400
    ...
    diff_cache.overall_status = status          # 原样落库
    ...
    sync_service.sync_weekly_to_commit(config_id, file_path, status)  # 再同步给 Commit

于是任何非空字符串（"banana"）都会：200 返回、写进
`WeeklyVersionDiffCache.overall_status` / `confirmation_status.dev`、并同步给
`Commit.status`。后果不是"多一个字段值"这么轻：

- 周版本列表的筛选只有 pending/confirmed/rejected 三项
  （templates/weekly_version_diff.html 的 statusFilter / statusNames），
  `weekly_version_stats_api` 也按这三个值分别 count —— 脏状态的文件从筛选和
  统计里**消失**（total 与三项之和对不上）。
- 同一函数里"操作人"的写入是 if/elif 分支：非三值状态下 `status_changed_by`
  保持旧值不变，出现"状态是 banana、操作人却是上一个确认者"的自相矛盾记录。
- Commit 侧同样落脏值，而提交列表/比较页只认 pending/reviewed/confirmed/rejected，
  界面上表现为"其他"（danger 徽章）且无法通过界面纠正回合法值组合。

## 取值域的证据（不是猜的）

- 模型注释：models/weekly_version.py:70 `overall_status = ... # 'pending', 'confirmed', 'rejected'`
  （Commit 侧 models/commit.py:27 同）。
- 前端调用点：templates/weekly_version_diff.html / weekly_version_full_diff.html
  的 updateFileStatusQuick/updateFileStatus 只传 'pending' / 'confirmed' / 'rejected'。
- 统计与筛选：weekly_version_stats_api 只 count 这三个值；周版本页筛选器
  也只有这三项。
- 提交状态域更大（含 'reviewed'，见 services/commit_status_api_service.py:49），
  但那是**提交**的状态，不属于周版本文件状态 —— 下面
  test_commit_reviewed_status_does_not_pollute_weekly_cache 覆盖这条路径。

## 这些测试变红意味着什么

- `test_rejects_*`：接口又开始接受任意字符串，脏值可以再次落库。
- `test_valid_*`：合法的三态被误拒（功能回退）。
- `test_service_layer_*`：服务层直调又能绕过校验写脏值。
- `test_no_half_written_state_*`：校验放到了写库之后，非法请求留下"改了一半"的记录。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import app, create_tables, db  # noqa: E402
from models import Commit, Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache  # noqa: E402

FILE_PATH = "src/hero.lua"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _seed(monkeypatch):
    """建一条 config + 一条 diff 缓存 + 一条相关提交，返回 (config_id, commit_db_id)。"""
    admin_token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", admin_token)

    project = Project(code=_uid("P"), name="weekly-file-status-validation")
    db.session.add(project)
    db.session.flush()

    repository = Repository(
        project_id=project.id,
        name=_uid("repo"),
        type="git",
        url="https://example.com/repo.git",
        branch="main",
        clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()

    config = WeeklyVersionConfig(
        project_id=project.id,
        repository_id=repository.id,
        name="W-status",
        branch="main",
        start_time=datetime(2026, 3, 2, 0, 0, 0),
        end_time=datetime(2026, 3, 9, 0, 0, 0),
        is_active=True,
        auto_sync=True,
    )
    db.session.add(config)
    db.session.flush()

    commit = Commit(
        repository_id=repository.id,
        commit_id=_uid("sha")[:12],
        path=FILE_PATH,
        status="pending",
        commit_time=datetime(2026, 3, 5, 0, 0, 0),
    )
    db.session.add(commit)
    db.session.flush()

    cache = WeeklyVersionDiffCache(
        config_id=config.id,
        repository_id=repository.id,
        file_path=FILE_PATH,
        commit_count=1,
        base_commit_id=commit.commit_id,
        latest_commit_id=commit.commit_id,
        overall_status="pending",
        confirmation_status=json.dumps({"dev": "pending"}),
    )
    db.session.add(cache)
    db.session.commit()

    return config.id, commit.id, admin_token


def _post_status(client, config_id, status_value, token):
    return client.post(
        f"/weekly-version-config/{config_id}/file-status",
        json={"file_path": FILE_PATH, "status": status_value},
        headers={"X-Admin-Token": token},
    )


@pytest.fixture
def seeded(monkeypatch):
    with app.app_context():
        create_tables()
        config_id, commit_db_id, token = _seed(monkeypatch)
        yield config_id, commit_db_id, token


def _read_back(config_id, commit_db_id):
    """从库里重新读，而不是看返回码 —— 返回 200 不代表没写脏值。"""
    cache = WeeklyVersionDiffCache.query.filter_by(config_id=config_id, file_path=FILE_PATH).first()
    commit = db.session.get(Commit, commit_db_id)
    assert cache is not None and commit is not None
    return (
        cache.overall_status,
        json.loads(cache.confirmation_status or "{}").get("dev"),
        commit.status,
    )


BOGUS_STATUSES = ["banana", "PENDING", "reviewed", "已确认", " done ", "1"]


@pytest.mark.parametrize("bogus", BOGUS_STATUSES)
def test_rejects_illegal_status_strings_and_writes_nothing(seeded, bogus):
    config_id, commit_db_id, token = seeded
    with app.test_client() as client:
        resp = _post_status(client, config_id, bogus, token)
        assert resp.status_code == 400, (
            f"status={bogus!r} 应被拒绝（4xx），实际 {resp.status_code}: {resp.get_data(as_text=True)}"
        )

    cache_status, cache_dev_status, commit_status = _read_back(config_id, commit_db_id)
    assert cache_status == "pending", f"非法值 {bogus!r} 落进了 overall_status：{cache_status!r}"
    assert cache_dev_status == "pending", f"非法值 {bogus!r} 落进了 confirmation_status.dev：{cache_dev_status!r}"
    assert commit_status == "pending", f"非法值 {bogus!r} 被同步到了 Commit.status：{commit_status!r}"


def test_rejects_non_string_status(seeded):
    """非字符串（数字/对象/布尔）同样不能落进 String 列。"""
    config_id, commit_db_id, token = seeded
    with app.test_client() as client:
        for bogus in (123, {"a": 1}, ["confirmed"], True):
            resp = _post_status(client, config_id, bogus, token)
            assert resp.status_code == 400, f"status={bogus!r} 应被拒绝，实际 {resp.status_code}"

    assert _read_back(config_id, commit_db_id) == ("pending", "pending", "pending")


def test_no_half_written_state_when_rejected(seeded):
    """非法请求不能留下「缓存改了、提交没改」的半成品。

    先在合法状态下确认一次，再发非法请求：两边都必须停在 confirmed。
    """
    config_id, commit_db_id, token = seeded
    with app.test_client() as client:
        ok = _post_status(client, config_id, "confirmed", token)
        assert ok.status_code == 200, ok.get_data(as_text=True)

    assert _read_back(config_id, commit_db_id) == ("confirmed", "confirmed", "confirmed")

    with app.test_client() as client:
        bad = _post_status(client, config_id, "banana", token)
        assert bad.status_code == 400

    cache_status, cache_dev_status, commit_status = _read_back(config_id, commit_db_id)
    assert (cache_status, cache_dev_status) == ("confirmed", "confirmed"), (
        f"缓存被非法请求改动了：overall_status={cache_status!r} dev={cache_dev_status!r}"
    )
    assert commit_status == "confirmed", (
        f"提交记录被非法请求改动了：commit.status={commit_status!r}"
    )


@pytest.mark.parametrize("status_value", ["pending", "confirmed", "rejected"])
def test_valid_statuses_still_accepted(seeded, status_value):
    """三态正常值必须继续可用（缓存 + 提交同步都到位）。"""
    config_id, commit_db_id, token = seeded
    with app.test_client() as client:
        resp = _post_status(client, config_id, status_value, token)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert (resp.get_json() or {}).get("success") is True

    cache_status, cache_dev_status, commit_status = _read_back(config_id, commit_db_id)
    assert cache_status == status_value
    assert cache_dev_status == status_value
    assert commit_status == status_value


def test_service_layer_rejects_illegal_status(seeded):
    """服务层直调也必须拒绝 —— 接口层校验不算修好。"""
    from services.status_sync_service import StatusSyncService

    config_id, commit_db_id, token = seeded
    before = _read_back(config_id, commit_db_id)

    result = StatusSyncService(db).sync_weekly_to_commit(config_id, FILE_PATH, "banana")

    assert not result.get("success"), f"服务层接受了非法状态: {result}"
    assert _read_back(config_id, commit_db_id) == before == ("pending", "pending", "pending")


def test_commit_reviewed_status_does_not_pollute_weekly_cache(seeded):
    """提交侧合法的 'reviewed' 不属于周版本文件状态，不能落到周版本缓存上。

    提交状态域含 'reviewed'（services/commit_status_api_service.py:49），
    但周版本界面/统计只认三态 —— 之前 commit→weekly 的同步会把它原样写进
    cache.overall_status，文件随即从筛选与统计里消失。
    """
    from services.status_sync_service import StatusSyncService

    config_id, commit_db_id, token = seeded
    commit = db.session.get(Commit, commit_db_id)
    commit.status = "reviewed"
    commit.status_changed_by = "someone"
    db.session.commit()

    StatusSyncService(db).sync_commit_to_weekly(commit_db_id, "reviewed")

    cache = WeeklyVersionDiffCache.query.filter_by(config_id=config_id, file_path=FILE_PATH).first()
    db.session.refresh(cache)
    assert cache.overall_status == "pending", (
        f"'reviewed' 又污染了周版本缓存：{cache.overall_status!r}"
    )
