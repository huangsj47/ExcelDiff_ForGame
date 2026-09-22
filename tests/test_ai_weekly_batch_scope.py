# -*- coding: utf-8 -*-
"""AI 的**输入集**必须和**分组键**用同一个判据（REV-AI-002）。

## 缺陷

「同项目 + 同起止时间」的周版本配置不一定是同一个版本 —— 界面上完全建得出两条同窗口
但**不同名**的配置。平台自己也是这么认的：分组键 `build_weekly_group_key` 是
`{project_id}|{start}|{end}|{版本名}`，**含名字**，于是「同窗不同名」= 两个分组、
两条 `AiWeeklyAnalysisState`、两次独立分析。

但输入集只按「项目 + 窗口」取（`WeeklyVersionConfig.query.filter(project_id, start, end)`），
于是**两个版本的文件混进同一份清单**：报告标题写的是触发的那一个版本名
（`report_document.weekly_target_label` 取 `run.target_id` 那条配置），`delta_files` 里却
有另一个版本的文件。报告读起来完全自洽，只有对着配置列表才看得出不对。

## 修法

判据收进 `services/ai/project_config_source.weekly_batch_configs`，三处同调：
AI 的输入集、同步闸门（`weekly_sync_gate.group_config_ids`）、页面的仓库标签页
（`weekly_version_logic.weekly_version_diff`）。

**多仓库批次不受影响**：那批配置的名字是 `f"{版本名} - {仓库名}"`，取版本名时按第一个
`" - "` 切开，所以「W1 - repoA / W1 - repoB」仍在同一批 —— 那正是本文件的第一条用例。
"""
import uuid
from datetime import datetime, timedelta, timezone

from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig
from models.weekly_version import WeeklyVersionDiffCache
from services import ai_analysis_service as ai_service

WINDOW_START = datetime(2026, 3, 1)
WINDOW_END = datetime(2026, 3, 8)
THIS_VERSION = "config/物品表.xlsx"
OTHER_VERSION = "src/not-this-release.lua"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _repository(project_id: int, name: str) -> Repository:
    repository = Repository(
        project_id=project_id, name=name, type="git",
        url=f"https://example.com/{name}.git", branch="main",
        resource_type="table", clone_status="completed",
    )
    db.session.add(repository)
    db.session.flush()
    return repository


def _config(project_id: int, repository: Repository, name: str) -> WeeklyVersionConfig:
    config = WeeklyVersionConfig(
        project_id=project_id, repository_id=repository.id, name=name,
        description="", branch="main",
        start_time=WINDOW_START, end_time=WINDOW_END, cycle_type="custom",
        is_active=True, auto_sync=True, status="active",
    )
    db.session.add(config)
    db.session.flush()
    return config


def _cache_row(config: WeeklyVersionConfig, repository: Repository, path: str) -> None:
    db.session.add(WeeklyVersionDiffCache(
        config_id=config.id, repository_id=repository.id, file_path=path,
        file_type="excel" if path.endswith(".xlsx") else "lua",
        latest_commit_id=(config.id.to_bytes(4, "big").hex() + "0" * 32)[:40],
        commit_count=1, updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
    ))


def _seed_two_versions():
    """同项目、同窗口、**两个不同的版本名**，各有自己的仓库与文件。"""
    project = Project(code=_uid("P"), name="同窗不同版本")
    db.session.add(project)
    db.session.flush()

    wanted_repo = _repository(project.id, _uid("wanted_repo"))
    other_repo = _repository(project.id, _uid("other_repo"))
    wanted = _config(project.id, wanted_repo, _uid("release-candidate"))
    other = _config(project.id, other_repo, _uid("unrelated-experiment"))
    _cache_row(wanted, wanted_repo, THIS_VERSION)
    _cache_row(other, other_repo, OTHER_VERSION)
    db.session.commit()
    return wanted.id, other.id


def test_the_payload_does_not_include_another_same_window_version():
    with app.app_context():
        create_tables()
        wanted_id, other_id = _seed_two_versions()

        payload, _state, skip = ai_service.build_weekly_payload(wanted_id)

        assert skip is None
        paths = {item["file_path"] for item in payload["delta_files"]}
        assert THIS_VERSION in paths, "本版本自己的文件都没进来，用例前提不成立"
        assert OTHER_VERSION not in paths, (
            "同窗口另一个版本的文混进了本版本的输入集 —— 报告标题写着一个版本，"
            "清单里却有另一个版本的文件"
        )
        assert payload["group"]["config_ids"] == [wanted_id], payload["group"]["config_ids"]
        assert other_id not in payload["group"]["config_ids"]


def test_the_other_version_gets_its_own_input_set():
    """反方向：从另一条配置触发时，进来的是它自己的文件。"""
    with app.app_context():
        create_tables()
        wanted_id, other_id = _seed_two_versions()

        payload, _state, skip = ai_service.build_weekly_payload(other_id)

        assert skip is None
        paths = {item["file_path"] for item in payload["delta_files"]}
        assert OTHER_VERSION in paths and THIS_VERSION not in paths
        assert payload["group"]["config_ids"] == [other_id]


def test_a_multi_repository_batch_still_shares_one_input_set():
    """反方向：多仓库批次（`版本名 - 仓库名`）**必须**仍是一批。

    收窄判据时最容易误伤的就是这一条：它们的名字逐字不同，只有版本名部分相同。
    """
    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name="多仓库批次")
        db.session.add(project)
        db.session.flush()
        base_name = _uid("W1")
        first_repo = _repository(project.id, _uid("repoA"))
        second_repo = _repository(project.id, _uid("repoB"))
        first = _config(project.id, first_repo, f"{base_name} - {first_repo.name}")
        second = _config(project.id, second_repo, f"{base_name} - {second_repo.name}")
        _cache_row(first, first_repo, THIS_VERSION)
        _cache_row(second, second_repo, OTHER_VERSION)
        db.session.commit()

        payload, _state, skip = ai_service.build_weekly_payload(first.id)

        assert skip is None
        paths = {item["file_path"] for item in payload["delta_files"]}
        assert paths == {THIS_VERSION, OTHER_VERSION}, paths
        assert set(payload["group"]["config_ids"]) == {first.id, second.id}
