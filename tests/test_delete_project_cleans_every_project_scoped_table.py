# -*- coding: utf-8 -*-
"""删除项目时必须把**每一张挂在项目下的表**都显式清掉。

## 这条为什么值得单独守

`delete_project` 是一张**手写的清单**。漏一张的后果不是「留了点垃圾数据」，而是
**项目永远删不掉**：

* `models/ai_analysis/*` 与 `models/agent.py` 里的关系是用 `backref` 建在 `Project`
  上的（`ai_analysis_config` / `ai_analysis_runs` / `ai_weekly_states` / `ai_api_key`），
  而默认 cascade 里没有 delete —— 所以 `db.session.delete(project)` 会先把子行的
  `project_id` 置 NULL。这几列全是 `nullable=False`，于是抛
  `IntegrityError`，整个事务回滚。
* 报错信息里**只有第一张撞上的表**（线上逐字是
  `NOT NULL constraint failed: ai_project_analysis_config.project_id`），后面几张要
  删一次、看一次日志才能试出来 —— 用户看到的是「删不掉」，看不到原因。

同一个坑 2026-09 已经踩过一次（`weekly_version_config.repository_id`，见
`tests/test_repository_delete_agent_task_fk.py`），那次是「删仓库」，这次是「删项目」。
两次都是**清单漂移**，所以除了补上今天缺的几张，还要有一条**跟着 schema 走**的护栏：
新加一张带项目外键的表时，这条测试必须先红。
"""

import inspect
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import services.repository_admin_handlers as repository_admin_handlers  # noqa: E402
from app import app, create_tables, db  # noqa: E402
from models import (  # noqa: E402
    AgentTempCache,
    Project,
    Repository,
    WeeklyVersionConfig,
)
from models.ai_analysis import (  # noqa: E402
    AiAnalysisAnomaly,
    AiAnalysisJob,
    AiAnalysisRun,
    AiAnalysisTrace,
    AiProjectAnalysisConfig,
    AiProjectApiKey,
    AiWeeklyAnalysisState,
)
from models.ai_analysis.job import (  # noqa: E402
    MODE_FULL,
    SOURCE_MANUAL,
    STATE_WAITING_SNAPSHOT,
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _strip_comments(source: str) -> str:
    """静态断言前先剥注释。

    这个仓库的注释里会**原样引用**要禁掉的写法/要提到的类名，不剥就会假通过
    （注释里写过 `AiAnalysisTrace`，而代码里其实没删它）。
    """
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"'''(?:.|\n)*?'''", "", source)
    return re.sub(r"(?m)#.*$", "", source)


def _model_by_table_name() -> dict:
    """`表名 -> 模型类`。用 mapper 反查，避免手写第二份清单。"""
    mapping = {}
    for mapper in db.Model.registry.mappers:
        table = mapper.local_table
        if table is not None:
            mapping.setdefault(table.name, mapper.class_)
    return mapping


def _project_scoped_models() -> list:
    """所有「跟着项目一起死」的表对应的模型。

    判据只有一条：**这一列有没有一个指向 `project.id` 的非空外键**。非空是关键 ——
    可空的那几种（`auth_project_create_requests.created_project_id`、
    `auth_user_functions.project_id`、`operation_log.repository_id`）语义是「引用」，
    项目没了要把引用置空、不是把整行删掉，`delete_project` 对它们是
    `_safe_nullify_created_project`。所以这里也按可空性分开，不混为一谈。

    再算一层间接：`ai_analysis_trace.run_id -> ai_analysis_run.id` 也是非空外键，
    删 run 的那一步同样会撞上 NOT NULL。
    """
    models_by_table = _model_by_table_name()
    project_children = set()
    for table in db.metadata.tables.values():
        for column in table.columns:
            if column.nullable:
                continue
            if any(fk.target_fullname == "project.id" for fk in column.foreign_keys):
                project_children.add(table.name)
                break

    owned = set(project_children)
    for table in db.metadata.tables.values():
        for column in table.columns:
            if column.nullable:
                continue
            if any(
                fk.target_fullname.split(".")[0] in project_children
                for fk in column.foreign_keys
            ):
                owned.add(table.name)
                break

    return sorted(
        {models_by_table[name].__name__ for name in owned if name in models_by_table}
    )


def test_every_project_scoped_table_is_named_in_delete_project():
    """护栏：新加一张带项目外键的表时，这条必须先红。

    它查的是 `delete_project` 的**源码**（已剥注释），而不是「跑一遍看有没有报错」——
    因为漏一张表的症状取决于那张表里**当时有没有数据**：本地库是空的就一路绿，
    线上有数据才炸。清单与 schema 对齐这件事只能静态查。
    """
    body = _strip_comments(inspect.getsource(repository_admin_handlers.delete_project))
    expected = _project_scoped_models()
    assert expected, "没扫到任何项目级表，说明这条测试的判据本身坏了"

    missing = [name for name in expected if name not in body]
    assert not missing, (
        "delete_project 没有清这几张表，漏一张项目就永远删不掉（NOT NULL constraint "
        f"failed: <表>.project_id）：{missing}"
    )


def test_delete_project_removes_the_whole_ai_analysis_family(monkeypatch):
    """今天就踩到的那个坑：项目下只要有一份 AI 分析配置，删除就整个失败。

    fixture 刻意把**每一张** ai_* 表和 `AgentTempCache` 都塞上数据 —— 只塞一张的话，
    修了 `ai_project_analysis_config` 之后这条会绿，而 `ai_analysis_trace` 那张
    （间接外键，删 run 时才炸）依旧没人管。
    """
    monkeypatch.setattr(
        repository_admin_handlers,
        "delete_local_repository_directory",
        lambda *_args, **_kwargs: None,
    )

    with app.app_context():
        create_tables()

        project = Project(code=_uid("P"), name=_uid("proj"))
        db.session.add(project)
        db.session.flush()
        project_id = project.id

        repo = Repository(
            project_id=project_id,
            name=_uid("repo"),
            type="git",
            url="ssh://git@example.com/group/repo.git",
            branch="master",
            clone_status="failed",
        )
        db.session.add(repo)
        db.session.flush()

        config = AiProjectAnalysisConfig(project_id=project_id)
        db.session.add(config)
        db.session.add(AiProjectApiKey(project_id=project_id, encrypted_key="enc"))
        db.session.add(AiWeeklyAnalysisState(project_id=project_id, group_key=_uid("grp")))
        db.session.add(
            AgentTempCache(
                cache_key=_uid("cache"),
                project_id=project_id,
                repository_id=repo.id,
            )
        )
        # 任务身份表（`models/ai_analysis/job.py`，2026-09-21 新加）。
        #
        # 它的 `project_id` 是**非空外键**，所以它与这一族里其它表是同一个坑：不显式清，
        # 删项目时 ORM 把子行 `project_id` 置 NULL 撞 NOT NULL，**整个项目删不掉**。
        # 刻意用 `waiting_snapshot`（最常见的非终态）而不是终态：终态的 job 也照样要清，
        # 但非终态更能代表「用户点了按钮、还没跑完就删项目」这个真实形态。
        db.session.add(
            AiAnalysisJob(
                project_id=project_id,
                target_type="weekly",
                target_key=_uid("grp"),
                requested_mode=MODE_FULL,
                state=STATE_WAITING_SNAPSHOT,
                trigger_source=SOURCE_MANUAL,
            )
        )
        db.session.flush()

        run = AiAnalysisRun(project_id=project_id, target_type="weekly", target_id=1)
        db.session.add(run)
        db.session.flush()
        db.session.add(
            AiAnalysisTrace(run_id=run.id, round_index=1, agent="S1", agent_round=1)
        )
        db.session.add(
            AiAnalysisAnomaly(run_id=run.id, project_id=project_id, title="一条异常")
        )
        db.session.commit()
        run_id = run.id

        with app.test_request_context(f"/projects/{project_id}/delete", method="POST"):
            response = repository_admin_handlers.delete_project.__wrapped__(project_id)
            # 删除失败时它也会 302（只是 flash 一句错误），所以下面必须直接查库。
            assert response.status_code == 302

        assert db.session.get(Project, project_id) is None, "项目本身没删掉"
        assert AiProjectAnalysisConfig.query.filter_by(project_id=project_id).count() == 0
        assert AiProjectApiKey.query.filter_by(project_id=project_id).count() == 0
        assert AiWeeklyAnalysisState.query.filter_by(project_id=project_id).count() == 0
        assert AgentTempCache.query.filter_by(project_id=project_id).count() == 0
        assert AiAnalysisRun.query.filter_by(project_id=project_id).count() == 0
        assert AiAnalysisTrace.query.filter_by(run_id=run_id).count() == 0
        assert AiAnalysisAnomaly.query.filter_by(project_id=project_id).count() == 0
        assert AiAnalysisJob.query.filter_by(project_id=project_id).count() == 0, (
            "任务身份表没清干净 —— 它的 project_id 是非空外键，留着就是下一条删不掉的项目"
        )


def test_delete_project_leaves_no_weekly_config_behind(monkeypatch):
    """周版本配置挂在项目上（不是仓库上），同一个坑的另一半。"""
    monkeypatch.setattr(
        repository_admin_handlers,
        "delete_local_repository_directory",
        lambda *_args, **_kwargs: None,
    )

    with app.app_context():
        create_tables()

        project = Project(code=_uid("P"), name=_uid("proj"))
        db.session.add(project)
        db.session.flush()
        project_id = project.id

        repo = Repository(
            project_id=project_id,
            name=_uid("repo"),
            type="git",
            url="ssh://git@example.com/group/repo.git",
            branch="master",
            clone_status="failed",
        )
        db.session.add(repo)
        db.session.flush()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.add(
            WeeklyVersionConfig(
                project_id=project_id,
                repository_id=repo.id,
                name=_uid("周版本"),
                branch="master",
                start_time=now,
                end_time=now + timedelta(days=7),
            )
        )
        db.session.commit()

        with app.test_request_context(f"/projects/{project_id}/delete", method="POST"):
            assert repository_admin_handlers.delete_project.__wrapped__(project_id).status_code == 302

        assert db.session.get(Project, project_id) is None
        assert WeeklyVersionConfig.query.filter_by(project_id=project_id).count() == 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
