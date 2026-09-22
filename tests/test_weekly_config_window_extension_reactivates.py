# -*- coding: utf-8 -*-
"""「把过期周版本的窗口顺延到未来」必须**同时**把 status 复位。

## 症状（2026-09-22 端到端实测踩到）

调度器的两个分支都要求 `status == 'active'`：`services/task_worker_service.py:1448`
先「窗口结束 → 置 completed → continue」，`:1453` 紧随其后的那个分支才去建同步任务。
窗口一过它就把这条置成 completed，而更新分支从前只改时间、不改 status —— 于是
「顺延一周」之后界面上 `end_time` 是未来、`is_active` 是 True，**同步与分析却再也
不会发生**。实测那次：配置建出来 6 分钟后就到点了（`end_time` 写的当天 23:59）。

## 两条反方向（防「一律复位」这种过头修法）

- 窗口**仍在过去**的（用户只是挪了一小时）不许复位 —— 那等于让早就结束的版本诈尸。
  判据必须与调度器同源：北京墙钟 <= `end_time`。
- 请求里**显式带了 `status`** 的以请求为准。复位分支是 `elif`，不许抢掉
  「手动标记完成」这个既有动作。
"""
import uuid
from datetime import timedelta

from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig
from utils.timezone_utils import now_beijing


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _stamp(delta_days: int) -> str:
    """北京墙钟的 `YYYY-MM-DDTHH:MM`（接口收的那个形状）。"""
    return (now_beijing() + timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M")


def _make_config(monkeypatch, *, end_delta_days: int, status: str) -> tuple[int, int, str]:
    """建一个窗口已经过期的配置，并把它置成调度器那个终态。"""
    token = _uid("admin-token")
    monkeypatch.setenv("ADMIN_API_TOKEN", token)
    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="weekly-window-extension")
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id,
            name=_uid("repo"),
            type="git",
            url=f"https://example.com/{_uid('r')}.git",
            branch="main",
            clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()
        config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repository.id,
            name=_uid("W"),
            branch="main",
            start_time=(now_beijing() + timedelta(days=end_delta_days - 1)).replace(tzinfo=None),
            end_time=(now_beijing() + timedelta(days=end_delta_days)).replace(tzinfo=None),
            cycle_type="custom",
            is_active=True,
            auto_sync=True,
            status=status,
        )
        db.session.add(config)
        db.session.commit()
        return project.id, config.id, token


def _put(project_id: int, config_id: int, token: str, body: dict) -> dict:
    with app.app_context():
        with app.test_client() as client:
            resp = client.put(
                f"/projects/{project_id}/weekly-version-config/api/{config_id}",
                json=body,
                headers={"X-Admin-Token": token},
            )
            assert resp.status_code == 200, resp.get_data(as_text=True)
            return resp.get_json() or {}


def _status_of(config_id: int) -> str:
    with app.app_context():
        return db.session.get(WeeklyVersionConfig, config_id).status


def test_extending_an_expired_window_reactivates_the_config(monkeypatch):
    project_id, config_id, token = _make_config(monkeypatch, end_delta_days=-3, status="completed")

    _put(project_id, config_id, token, {
        "start_time": _stamp(0), "end_time": _stamp(7),
    })

    assert _status_of(config_id) == "active", (
        "窗口顺延到未来了、status 却还是 completed —— 调度器那两个分支都不会再进，"
        "这个版本从此既不落缓存也不做分析"
    )


def test_moving_the_window_but_keeping_it_in_the_past_does_not_reactivate(monkeypatch):
    """反方向：只是把窗口在过去里挪了挪，不许诈尸。"""
    project_id, config_id, token = _make_config(monkeypatch, end_delta_days=-3, status="completed")

    _put(project_id, config_id, token, {
        "start_time": _stamp(-5), "end_time": _stamp(-1),
    })

    assert _status_of(config_id) == "completed"


def test_touching_other_fields_does_not_reactivate(monkeypatch):
    """反方向：只改名字（没动窗口）连 `time_changed` 都不该有。"""
    project_id, config_id, token = _make_config(monkeypatch, end_delta_days=-3, status="completed")

    payload = _put(project_id, config_id, token, {"name": "改个名字"})

    assert payload.get("time_changed") is False, "前提不成立：这次请求不该被判成改了窗口"
    assert _status_of(config_id) == "completed"


def test_an_explicit_status_in_the_request_wins(monkeypatch):
    """显式带 status 的以请求为准 —— 复位分支是 `elif`，不许抢。"""
    project_id, config_id, token = _make_config(monkeypatch, end_delta_days=7, status="active")

    _put(project_id, config_id, token, {
        "start_time": _stamp(0), "end_time": _stamp(9), "status": "completed",
    })

    assert _status_of(config_id) == "completed"
