# -*- coding: utf-8 -*-
"""「项目事实」的接线：关键路径与生成物前缀都不许再是写死在平台里的常量。

四件事各自守一条**曾经一声不响**的失效路径：

1. `CRITICAL_PATH_PATTERNS` 的每条模式都要求前导斜杠（`/config/`），而 git 的改动路径是
   相对路径（`config/x.xlsx`）—— 判据恒为 False，「命中关键路径就升级为全量分析」
   这条通道从来没触发过，不报错、不留痕。
2. 界面上那一栏「重点表名」（`Repository.important_tables`）**全仓只写不读**：
   管理员填的重点从未影响过任何判定。
3. `build_bundles(generated_prefixes=...)` 这个形参没有任何生产调用方传过，
   不叫 `CfgXxx` 的项目里配对恒为 0 组，而提示词里一个字都不提。
4. 配对说明把「平台猜到的」写成了「平台确认的」（「表与其生成物，必须一起看」）。

测试库是**会话级共用**的（没有逐用例重置），所以这里的断言一律只用本用例自己建出来的
行，不做任何全局 count()。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from services.ai.bundles import build_bundles
from services.ai.change_set import from_weekly_payload
from services.ai.project_facts import (
    DEFAULT_CRITICAL_PATH_FACTS,
    DEFAULT_CRITICAL_PATH_PATTERNS,
    SOURCE_DEFAULT,
    SOURCE_PROJECT,
    SOURCE_PROJECT_EMPTY,
    critical_path_facts,
    declared_important_tables,
    generated_prefixes,
    important_table_hit,
)
from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV


def _uid(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ==========================================================================
# 一、关键路径的判据（第 1 条）
# ==========================================================================


def test_a_relative_config_path_is_a_critical_path():
    """**这一条就是那个从来没触发过的通道。**

    git 的 `Commit.path` 是**不带前导斜杠**的相对路径。老写法 `/config/` 对
    `config/x.xlsx` 一次都命中不了，于是「关键路径 → 全量分析」这条通道形同虚设。
    """
    assert ai_service._is_critical_path("config/x.xlsx")
    assert ai_service._is_critical_path("config/60_skill/角色属性表.xlsx")
    assert ai_service._is_critical_path("config/[30]道具表_CfgItem.xlsx")


def test_a_directory_that_merely_ends_with_config_is_not_a_critical_path():
    """`myconfig/x` 与 `deconfig/x` 跟 `config/` 这个目录没有关系，不许命中。

    这就是「路径分量起点」写法（`(?:^|/)config/`）与「只要包含 config/」的区别。
    """
    assert not ai_service._is_critical_path("myconfig/x")
    assert not ai_service._is_critical_path("deconfig/x")
    assert not ai_service._is_critical_path("src/notconfig/a.py")


def test_svn_style_absolute_paths_still_match():
    """修 bug 不能把原来就命中的写法弄丢：SVN 风格带前导斜杠的路径照旧命中。"""
    assert ai_service._is_critical_path("/config/demo.xlsx")
    assert ai_service._is_critical_path("db/migrations/001.sql")
    assert ai_service._is_critical_path("a.sql")
    assert not ai_service._is_critical_path("code/qz_pub/cfg/a.lua")


def test_every_default_pattern_is_anchored_at_a_component_start():
    """静态守一遍：默认清单不许再回到平台模块里，且每条都锚在路径分量起点上。

    清单搬去 `project_facts` 是这次改动的一部分：留在分析服务里就等于留了第二份
    事实源，而它当年正是「写错了也没人知道」的那一份。
    """
    assert not hasattr(ai_service, "CRITICAL_PATH_PATTERNS"), "关键路径清单又回到了平台模块里"

    for pattern in DEFAULT_CRITICAL_PATH_PATTERNS:
        if pattern.startswith("\\."):
            continue
        assert pattern.startswith("(?:^|/)"), f"{pattern} 没有锚在路径分量起点上"


# ==========================================================================
# 二、「重点表名」真的生效（第 3 条）
# ==========================================================================


def test_a_declared_important_table_matches_by_file_name():
    """字段语义是「表名」，所以要按文件名（而不是整条路径）比。

    三种写法都算命中：带目录前缀的路径、basename、去扩展名的 stem；大小写不敏感。
    """
    declared = declared_important_tables("道具表, RoleAttr")
    assert declared == ("道具表", "RoleAttr")

    # 管理员填的是人读的表名，文件名里带着 `[30]` 段位前缀与 `_CfgItem` 后缀。
    assert important_table_hit("config/[30]道具表_CfgItem.xlsx", declared) == "道具表"
    assert important_table_hit("config/RoleAttr.xlsx", ("roleattr",)) == "roleattr"
    # 完整路径写法也应该命中。
    assert important_table_hit("config/[30]道具表_CfgItem.xlsx", ("config/[30]道具表_CfgItem.xlsx",))
    # 名字分量的起点：`MyItemTable` 里的 `Item` 前面是字母，不算「Item 表」。
    assert important_table_hit("code/MyItemTable.lua", ("Item",)) == ""


def test_an_empty_declaration_is_not_a_declaration():
    assert declared_important_tables(None) == ()
    assert declared_important_tables("") == ()
    assert declared_important_tables("  ,  ；\n ") == ()


@pytest.fixture()
def declared_repo():
    """两个仓库、两条周版本配置：A 声明了重点表，B 什么都没声明。

    两个仓库里放**同一条路径**（`tables/[30]道具表_CfgItem.xlsx`，刻意不带 `config/`
    前缀，免得平台默认模式替它命中），这样两边的差别只可能来自那一栏声明。
    """
    with app.app_context():
        create_tables()
        project = Project(code=_uid("P"), name=_uid("facts-project"))
        db.session.add(project)
        db.session.flush()

        now = datetime.now(timezone.utc)
        last_analyzed_at = now - timedelta(hours=1)
        result = {"project": project, "last_analyzed_at": last_analyzed_at}

        for key, important in (("declared", "道具表、角色属性表"), ("silent", None)):
            repo = Repository(
                project_id=project.id,
                name=_uid(f"qz_{key}"),
                type="git",
                url=f"https://example.com/{key}.git",
                branch="main",
                resource_type="table",
                clone_status="completed",
                important_tables=important,
            )
            db.session.add(repo)
            db.session.flush()
            cfg = WeeklyVersionConfig(
                project_id=project.id,
                repository_id=repo.id,
                name=f"{key} - {repo.name}",
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
            # 9 个旧文件 + 1 个这次改的：delta/total = 0.1，低于两条数量阈值，
            # 于是范围判定**只可能**被「关键路径」这一条影响。
            for index in range(9):
                _seed(cfg, repo, f"tables/old_{index}.xlsx", updated_at=now - timedelta(hours=9))
            _seed(cfg, repo, "tables/[30]道具表_CfgItem.xlsx", updated_at=now - timedelta(minutes=5))
            result[key] = cfg
        db.session.commit()
        yield result


def _seed(cfg, repo, path: str, *, updated_at: datetime) -> None:
    db.session.add(
        WeeklyVersionDiffCache(
            config_id=cfg.id,
            repository_id=repo.id,
            file_path=path,
            file_type="excel",
            latest_commit_id="c" * 40,
            commit_count=1,
            updated_at=updated_at,
        )
    )


def test_the_declared_important_table_changes_the_scope_decision(declared_repo):
    """**这一条证明那一栏不再是死的。**

    同一个路径、同一批数据，只差仓库上那一栏「重点表名」：声明了的那个仓库把本次分析
    升级为全量，没声明的照旧走增量。在这之前两边的结果**永远一样**（因为它从来没被读过）。
    """
    with app.app_context():
        last = declared_repo["last_analyzed_at"]

        declared_summary, _details, skip = ai_service._summarize_weekly_files(
            [declared_repo["declared"]], last
        )
        assert skip is None
        assert declared_summary["critical_paths"] is True, "填了重点表却什么都没变"
        assert "道具表" in "".join(declared_summary["critical_path_hits"])
        assert ai_service._decide_scope(declared_summary, last) == (
            "full",
            "critical_path_detected",
        )

        silent_summary, _details, _skip = ai_service._summarize_weekly_files(
            [declared_repo["silent"]], last
        )
        assert silent_summary["critical_paths"] is False
        # 没命中时也要说得出「模式是从哪来的」，否则「声明了『没有』」与「什么都没声明」
        # 在结果里分不开（这里没知识包，所以是平台默认）。
        assert silent_summary["critical_path_source"] == SOURCE_DEFAULT
        assert ai_service._decide_scope(silent_summary, last) == ("incremental", "delta_small")


def test_the_control_repo_is_the_same_shape_as_the_declared_one(declared_repo):
    """守卫：上面的对照必须只差「那一栏声明」，否则那条对照什么都证明不了。"""
    with app.app_context():
        declared = declared_repo["declared"].repository
        silent = declared_repo["silent"].repository
        assert declared.important_tables, "对照组自己都没填，这条对照是空的"
        assert not silent.important_tables
        assert declared.resource_type == silent.resource_type


# ==========================================================================
# 三、生成物前缀：项目可声明，且「0 组」不再无声（第 2 条）
# ==========================================================================


MOD_TABLE = "config/[30]道具表_ModItem.xlsx"
MOD_LUA = "build/lua/ModItem.lua"


def test_a_declared_prefix_is_actually_used_by_build_bundles():
    """形参不再是空头承诺：项目声明 `Mod` 之后，`ModItem` 这一对才配得起来。

    同一个输入、只差前缀，结果必须不同 —— 否则那个形参依然是没人用的摆设。
    """
    paths = [MOD_TABLE, MOD_LUA]

    declared = build_bundles(paths, generated_prefixes=("Mod",))
    assert len(declared) == 1
    assert declared[0].members == (MOD_TABLE, MOD_LUA)

    default = build_bundles(paths)
    assert not any(bundle.is_multi for bundle in default), (
        "默认前缀配出了这一对 —— 那这条用例就证明不了前缀形参生效"
    )


def _write_pack(projects_root: Path, slug: str, frontmatter: str) -> None:
    pack = projects_root / slug / "references"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "project-facts.md").write_text(f"---\n{frontmatter}\n---\n\n正文。\n", encoding="utf-8")


def test_the_prefix_declared_in_the_knowledge_pack_reaches_the_prompt(tmp_path, monkeypatch):
    """端到端：知识包声明 → 读出来 → 传进 `build_bundles` → 变更清单里出现那一行。"""
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
    _write_pack(tmp_path, "p1", "name: p1\ndescription: p1 事实\ngenerated_prefixes: Mod")

    declaration = generated_prefixes("P1")
    assert declaration.source == SOURCE_PROJECT
    assert declaration.prefixes == ("Mod",)

    change = from_weekly_payload(
        {
            "mode": "weekly",
            "list_files": [
                {"file_path": MOD_TABLE, "latest_commit_id": "c1"},
                {"file_path": MOD_LUA, "latest_commit_id": "c1"},
            ],
        },
        prefixes=declaration,
    )
    assert change.bundle_lines, "项目声明了前缀，清单里却没有配对"
    assert "ModItem" in change.bundle_lines[0]
    assert "1 组" in change.bundle_note
    assert "Mod" in change.bundle_note


def test_declaring_none_is_not_the_same_as_declaring_nothing(tmp_path, monkeypatch):
    """**「用默认值」与「项目声明为空」必须分得开。**

    两者都会让本轮配出 0 组，但原因完全不同：前者是「平台不知道这个项目怎么配」，
    后者是「这个项目本来就没有可配对的产物」。`source` 与 `bundle_note` 都要说出来。
    """
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
    _write_pack(tmp_path, "p1", "name: p1\ndescription: p1 事实\ngenerated_prefixes: none")

    declared_empty = generated_prefixes("P1")
    assert declared_empty.source == SOURCE_PROJECT_EMPTY
    assert declared_empty.prefixes == ()
    # 空就是空 —— 但它是**项目表过态**的空，不是一个「没读到」的空（见下面的 source 断言）。
    assert declared_empty.is_empty is True

    undeclared = generated_prefixes("P2")
    assert undeclared.source == SOURCE_DEFAULT
    assert undeclared.prefixes == ("Cfg",)

    change = from_weekly_payload(
        {
            "mode": "weekly",
            "list_files": [
                {"file_path": "config/[30]道具表_CfgItem.xlsx", "latest_commit_id": "c1"},
                {"file_path": "build/lua/CfgItem.lua", "latest_commit_id": "c1"},
            ],
        },
        prefixes=declared_empty,
    )
    assert change.bundle_lines == ()
    assert "项目声明「没有可配对的生成物前缀」" in change.bundle_note, (
        "「0 组」没有说清原因，又回到了「本来没得配」与「不会配」分不开的状态"
    )


def test_a_broken_declaration_falls_back_to_the_default_and_says_so(tmp_path, monkeypatch):
    """声明坏了 → 用默认值，但**必须告警**：坏掉的声明不能变成一次静默降级。"""
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
    # frontmatter 里出现 `": "` 会被 YAML 截断，契约直接判非法（见 skill_contract）。
    _write_pack(tmp_path, "p1", "name: p1\ndescription: p1 事实\ngenerated_prefixes: Cfg: 坏的")

    declaration = generated_prefixes("P1")
    assert declaration.prefixes == ("Cfg",)
    assert declaration.source == SOURCE_DEFAULT
    assert declaration.warning, "声明坏了却一声不响"


def test_the_g119_pack_declares_its_own_prefix():
    """真实项目的声明必须真的被读到 —— 这条同时守着「文件被删掉/写坏」的情况。"""
    declaration = generated_prefixes("G119")
    assert declaration.source == SOURCE_PROJECT, "G119 的声明没被读到"
    assert declaration.prefixes == ("Cfg",)
    assert not declaration.warning


# ==========================================================================
# 四、关键路径的模式也可以由项目声明（第 1 条的另一半）
# ==========================================================================


def test_a_project_can_declare_its_own_critical_path_patterns(tmp_path, monkeypatch):
    """配表项目的危险路径（`cfg/`）不在平台默认清单里 —— 那是项目事实，由项目自己声明。"""
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
    _write_pack(
        tmp_path,
        "p1",
        "name: p1\ndescription: p1 事实\ncritical_path_patterns: (?:^|/)cfg/",
    )

    facts = critical_path_facts("P1")
    assert facts.patterns_source == SOURCE_PROJECT
    assert facts.why("code/qz_pub/cfg/a.lua")
    # 项目自己的模式生效之后，命中的是它，不是平台默认那几条。
    assert facts.why("config/x.xlsx") == ""


def test_declaring_no_patterns_is_honoured(tmp_path, monkeypatch):
    """项目声明「没有路径模式」时不许回落默认清单 —— 那是它明确表过态的。"""
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
    _write_pack(tmp_path, "p1", "name: p1\ndescription: p1 事实\ncritical_path_patterns: none")

    facts = critical_path_facts("P1")
    assert facts.patterns_source == SOURCE_PROJECT_EMPTY
    assert facts.patterns == ()
    assert facts.why("config/x.xlsx") == ""
    # 但「重点表名」这条通道不受影响：两者语义不重合，命中任一即关键路径。
    assert facts.why("tables/道具表.xlsx", ("道具表",))


def test_a_bad_pattern_is_dropped_and_reported(tmp_path, monkeypatch):
    """一个非法正则不许把整份声明废掉，也不许悄悄丢掉。"""
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
    _write_pack(
        tmp_path,
        "p1",
        "name: p1\ndescription: p1 事实\ncritical_path_patterns: (?:^|/)cfg/, (未闭合",
    )

    facts = critical_path_facts("P1")
    assert facts.patterns == ("(?:^|/)cfg/",)
    assert facts.why("code/cfg/a.lua")
    assert "非法正则" in facts.warning


def test_no_project_declaration_means_platform_defaults(tmp_path, monkeypatch):
    """绝大多数项目没声明过 —— 那时照旧用平台默认值，不报错、也不能变成「没有」。"""
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))

    facts = critical_path_facts("NOT_A_REAL_PROJECT")
    assert facts.patterns_source == SOURCE_DEFAULT
    assert facts.patterns == DEFAULT_CRITICAL_PATH_PATTERNS
    assert facts.why("config/x.xlsx")
    assert DEFAULT_CRITICAL_PATH_FACTS.why("db/migrations/001.sql")


# ==========================================================================
# 五、文案不许把「猜到的」说成「确认的」（第 4 条）
# ==========================================================================


_UNVERIFIED_CLAIMS = ("必须一起看", "表与其生成物", "这些改动是一件事", "配表改动 ")


def test_the_prompt_does_not_assert_what_the_platform_never_verified():
    """平台判「这是一组」的唯一依据是文件名里有同一个记号，它**不知道**这两个文件
    之间是什么关系（`Item.csv` 与脚本 `Item.py` 在别的项目里也会凑成一对）。"""
    change = from_weekly_payload(
        {
            "mode": "weekly",
            "list_files": [
                {"file_path": "config/[30]道具表_CfgItem.xlsx", "latest_commit_id": "c1"},
                {"file_path": "build/lua/CfgItem.lua", "latest_commit_id": "c1"},
            ],
        }
    )

    assert change.bundle_lines, "这一对没配上，下面的断言就是空转"
    for claim in _UNVERIFIED_CLAIMS:
        assert claim not in change.summary, f"提示词里出现了未经核实的断言：{claim}"
        assert claim not in change.bundle_lines[0]
    # 说清了「依据是什么」与「要自己确认」。
    assert "未经核实" in change.summary
    assert "待确认" in change.summary
