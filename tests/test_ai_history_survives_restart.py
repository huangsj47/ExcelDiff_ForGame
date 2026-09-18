# -*- coding: utf-8 -*-
"""分析过一次，历史结论就不该消失。

## 缺陷形态（用户报的）

用户对本周版本手动跑过一次 AI 分析（他设了「不自动分析」，之后也没有人重跑），
重启平台后页面上变成「版本AI分析：未分析」。结论**一直躺在库里**，界面却说从没分析过。

读侧原来只看**最新那一条 run**，并且要求它通过 `_is_run_fresh`。这两件事各自都会把
一份好端端的结论变成「没有结果」：

1. **溯源不一致。** `_is_run_fresh` 里的 `_provenance_matches` 要求
   `prompt_version` / `skill_version` / `rules_version` / `model` 与**现在这套**逐字相同。
   改了提示词、改了评审规程 `SKILL.md`、换了模型，老 run 一律判为不可复用。
   问题在于「可复用」是给**跳过重跑**用的（`stream_*` 里的缓存命中，省一次调用），
   不该拿来判断「这个版本有没有历史结论可看」—— 后者的代价是用户以为自己的分析白做了。
   用户这次踩的正是这条：改 SKILL.md / prompt.py 当天重启，全部历史结论一起消失。
2. **更新的那条不是结论。** 重启时 `fail_orphaned_analysis_runs` 把遗留的 `running`
   判成 `failed`（这是对的，那些 run 永远不会被写完成）；但失败的 run 也占着「最新」
   这个位置，于是更早那条**成功**的结论被它挡住，读侧返回 None。

## 修成的口径

三步，每一步都要能回答「用户接下来该做什么」：

* 最新那条真的可用（成功 + 有内容 + 未过期 + 溯源一致）→ 直接给；
* 最新那条正在跑 → 如实报「进行中」（这条没变，见
  `tests/test_weekly_ai_auto_trigger_gate.py`）；
* 否则**退回到最近一条真有结论的成功记录**，并带上 `stale` / `stale_reason` /
  `stale_note` 如实说明它是旧的 —— 「结论是旧的」与「没有结论」是两件事，
  界面必须分得开，否则用户没有任何办法知道自己的分析还在不在；
* 一条结论都没有时，才轮到「最近一次失败」（给原因、不给结论）。这条路以前走不到，
  界面里那个「上次分析失败：<原因>」的分支因此是死的。

## 为什么用 `prompt_version` 造「规则变了」

`_current_provenance` 的四个字段里，`prompt_version` / `skill_version` / `rules_version`
都是**源码内容哈希**。测试里改文件造这件事既不安全也没必要（会污染其它用例），
写一个与当前哈希不同的值就是同一个效果 —— 判等只比较字符串。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig
from models.ai_analysis import AiAnalysisRun

# 一条「有结论」的最小载荷：读侧只要求 response_text 或 response_payload 非空。
REPORT_TEXT = "# 变更理解\n把奖励发放从先扣后发改成先发后扣。\n"
REPORT_PAYLOAD = '{"risk_level": "high", "anomaly_count": 1}'


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _setup_config():
    """一个可跑的项目 + 周版本配置（与 `test_weekly_ai_auto_trigger_gate` 同一套口径）。"""
    project = Project(code=_uid("P"), name=_uid("ai-project"))
    db.session.add(project)
    db.session.flush()
    repo = Repository(
        project_id=project.id,
        name=_uid("code"),
        type="git",
        url=f"https://example.com/{_uid('r')}.git",
        branch="main",
        resource_type="code",
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    cfg = WeeklyVersionConfig(
        project_id=project.id,
        repository_id=repo.id,
        name=f"W1 - {repo.name}",
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
    db.session.commit()
    return project, repo, cfg


def _add_run(
    project_id: int,
    cfg,
    *,
    status: str,
    age_seconds: int = 0,
    concluded: bool = True,
    prompt_version: str | None = None,
    error_message: str = "",
    target_key: str | None = None,
) -> AiAnalysisRun:
    """造一条 run。

    `concluded=False` 造「没有结论」的记录（失败/被中断的那些就是这样）；
    `prompt_version` 不传就用**当前**的哈希（= 溯源一致），传别的值就模拟「规则变了」。
    """
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    provenance = ai_service._current_provenance(project_id)
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_id=cfg.id,
        target_key=target_key or ai_service.build_weekly_group_key(cfg),
        status=status,
        scope="full",
        trigger_source="manual",
        started_at=ts,
        created_at=ts,
        finished_at=ts if status != "running" else None,
        response_text=REPORT_TEXT if concluded else "",
        response_payload=REPORT_PAYLOAD if concluded else None,
        error_message=error_message,
        prompt_version=prompt_version if prompt_version is not None else provenance["prompt_version"],
        skill_version=provenance["skill_version"],
        rules_version=provenance["rules_version"],
        model=provenance["model"],
    )
    db.session.add(run)
    db.session.commit()
    return run


# ==========================================================================
# 一、规则变了：结论是旧的，但**还在**
# ==========================================================================


def test_a_conclusion_produced_under_older_rules_is_still_shown():
    """**核心回归**：改了提示词 / 评审规程 / 模型之后，历史结论不许变成「未分析」。

    这是用户报的那一条。溯源不一致只意味着「这份结论是旧规则下的」，界面照样要
    把它显示出来（并说明是旧的），而不是让用户以为分析白做了。
    """
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        run = _add_run(project.id, cfg, status="succeeded", prompt_version="prompt-obsolete")

        result = ai_service.get_latest_weekly_result(cfg.id)

        assert result is not None, "改了评审规程之后，历史结论被报成了「从没分析过」"
        assert result["run_id"] == run.id
        assert result["response_text"] == REPORT_TEXT, "结论正文丢了"
        assert result["result"] and result["result"]["risk_level"] == "high"
        assert result["stale"] is True, "旧规则的结论必须自报为「旧」，不能冒充当前结论"
        assert result["stale_reason"] == ai_service.STALE_REASON_RULES_CHANGED
        assert result["stale_note"], "旧结论要有一句给人看的说明"
        assert "仅供参考" in result["stale_note"]


def test_a_current_conclusion_is_not_marked_stale():
    """反向自检：溯源一致时不许打 stale —— 否则上面那条用一个恒真的字段也能过。"""
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        run = _add_run(project.id, cfg, status="succeeded")

        result = ai_service.get_latest_weekly_result(cfg.id)

        assert result["run_id"] == run.id
        assert "stale" not in result, f"当前规则下的结论被打了旧标记：{result.get('stale_note')}"


# ==========================================================================
# 二、更新的那次没跑完：不许挡住更早那条结论
# ==========================================================================


def test_an_interrupted_newer_run_does_not_hide_the_previous_conclusion():
    """重启把「最新那条」判成 failed 之后，更早那条成功结论必须顶上来。

    `fail_orphaned_analysis_runs` 把遗留的 running 判成 failed 是对的（那些 run 再也
    不会被写完成）；错的是读侧让这条失败记录独占「最新」的位置 —— 于是用户重启一次，
    上一次好好的结论就看不见了。
    """
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        older = _add_run(project.id, cfg, status="succeeded", age_seconds=3600)
        newer = _add_run(
            project.id,
            cfg,
            status="failed",
            concluded=False,
            error_message="平台重启，本次分析被中断（未跑完，可以重新分析）。",
        )

        result = ai_service.get_latest_weekly_result(cfg.id)

        assert result is not None, "更新的那条失败记录把上一次的结论挡住了"
        assert result["run_id"] == older.id, "退回的不是那条有结论的记录"
        assert result["response_text"] == REPORT_TEXT
        assert result["stale"] is True
        assert result["stale_reason"] == ai_service.STALE_REASON_INTERRUPTED
        assert result["newest_run"]["run_id"] == newer.id, "要说清挡住它的那一次是哪一条"
        assert result["newest_run"]["status"] == "failed"
        assert "未完成" in result["stale_note"]


def test_the_latest_conclusion_wins_when_several_exist():
    """有多条结论时退回**最近**那条成功记录，不是最早那条。"""
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        _add_run(project.id, cfg, status="succeeded", age_seconds=7200, prompt_version="prompt-old")
        middle = _add_run(project.id, cfg, status="succeeded", age_seconds=3600)
        _add_run(project.id, cfg, status="failed", concluded=False, error_message="超时")

        result = ai_service.get_latest_weekly_result(cfg.id)

        assert result["run_id"] == middle.id, f"退回的不是最近那条结论：{result['run_id']}"


# ==========================================================================
# 三、真的没有结论时不许凭空造一个
# ==========================================================================


def test_a_failed_run_is_reported_as_failed_not_as_no_result():
    """一条结论都没有、最近那次是失败 → 要报「失败」并给出原因。

    这是界面里那个「上次分析失败：<原因>」分支的数据来源。以前读侧一律折叠成 None，
    那条分支根本走不到，用户看到的是「暂无分析结果」，而实际上刚刚那次是失败退出的。
    """
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        run = _add_run(
            project.id, cfg, status="failed", concluded=False, error_message="接口地址连不上"
        )

        result = ai_service.get_latest_weekly_result(cfg.id)

        assert result is not None, "失败被报成了「暂无分析结果」"
        assert result["run_id"] == run.id
        assert result["status"] == "failed"
        assert result["error_message"] == "接口地址连不上"
        assert not result["response_text"], "失败的记录不该带出任何结论文本"
        assert not result.get("stale"), "没有结论时不该说「以下是旧结论」"


def test_nothing_analyzed_yet_is_still_none():
    """真的一条记录都没有 → None（不许把「没分析过」美化成一个空结论）。"""
    with app.app_context():
        create_tables()
        _project, _repo, cfg = _setup_config()

        assert ai_service.get_latest_weekly_result(cfg.id) is None


def test_an_expired_conclusion_is_not_resurrected():
    """超过保留期的结论不许被退回 —— 退回逻辑只放宽**溯源**这一条，不放宽时间窗。

    保留期是「这份结论还值不值得看」的现有口径（`ANALYSIS_CACHE_DAYS`，默认 90 天）：
    一个周版本的结论过期到保留期之外，已经不能代表这个版本了。放宽它等于让「退回旧
    结论」变成「把任何考古发现都当成结论」。
    """
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        days = ai_service.ANALYSIS_CACHE_DAYS + 1
        _add_run(project.id, cfg, status="succeeded", age_seconds=days * 86400)

        assert ai_service.get_latest_weekly_result(cfg.id) is None, (
            "过期结论被退回了，保留期形同虚设"
        )


def test_an_expired_conclusion_does_not_mask_a_failed_attempt():
    """过期结论 + 更新的失败记录：如实报失败，**不许**把过期结论顶上来。"""
    with app.app_context():
        create_tables()
        project, _repo, cfg = _setup_config()
        days = ai_service.ANALYSIS_CACHE_DAYS + 1
        _add_run(project.id, cfg, status="succeeded", age_seconds=days * 86400)
        _add_run(project.id, cfg, status="failed", concluded=False, error_message="超时")

        result = ai_service.get_latest_weekly_result(cfg.id)

        assert result is not None
        assert result["status"] == "failed", "最近一次失败没被报出来"
        assert not result["response_text"], "过期结论被当成结论顶了上来"


# ==========================================================================
# 四、单提交那条读侧是同一套口径
# ==========================================================================


def test_the_commit_read_path_uses_the_same_rule():
    """单提交页与周版本页共用一份读侧实现 —— 否则同一个缺陷要在两处各修一遍。"""
    with app.app_context():
        create_tables()
        project, repo, _cfg = _setup_config()
        from models import Commit

        commit = Commit(
            repository_id=repo.id,
            commit_id=_uid("c")[:10],
            message="fix",
            author="tester",
            commit_time=datetime(2026, 3, 2),
            path="config/sheet.xlsx",
        )
        db.session.add(commit)
        db.session.commit()

        # 测试库是**会话级共用**的（没有逐用例重置），而单提交的 run 是按
        # `target_id`（一个普通整数列，没有外键）归组的 —— 别的测试文件完全可能
        # 留下同样 `target_id` 的 run，而它是「更新的一条」，会把我的那条挤掉。
        # 症状正是本仓库那条老坑：**单跑绿、全量红**。所以这里先清干净这个键。
        AiAnalysisRun.query.filter_by(target_type="commit", target_id=commit.id).delete()
        db.session.commit()

        ts = datetime.now(timezone.utc) - timedelta(hours=1)
        run = AiAnalysisRun(
            project_id=project.id,
            target_type="commit",
            target_id=commit.id,
            target_key=None,
            status="succeeded",
            scope="full",
            trigger_source="manual",
            started_at=ts,
            created_at=ts,
            finished_at=ts,
            response_text=REPORT_TEXT,
            response_payload=REPORT_PAYLOAD,
            prompt_version="prompt-obsolete",
        )
        db.session.add(run)
        db.session.commit()

        result = ai_service.get_latest_commit_result(commit.id)

        assert result is not None, "单提交页也把旧规则的结论报成了「没有分析」"
        assert result["run_id"] == run.id
        assert result["stale_reason"] == ai_service.STALE_REASON_RULES_CHANGED
        # 周版本专有的 focus 不该出现在单提交的返回体里
        assert "focus" not in result


# ==========================================================================
# 五、前端：那句话要真的显示出来
# ==========================================================================

TEMPLATES = (
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
    "templates/commit_diff_new.html",
)


def _strip_js_comments(src: str) -> str:
    """先剥注释再断言：本仓库会把要禁掉的写法原样写进注释里（踩过坑）。"""
    import re

    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def _template_source(name: str) -> str:
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, name), encoding="utf-8") as handle:
        return handle.read()


def test_every_drawer_shows_the_stale_note():
    """三份抽屉都要把 `stale_note` 显示出来。

    只改后端不改前端的话，用户看到的还是「最近分析：<时间> | 风险等级 X」——
    一句看起来完全正常的话，而他无从知道这是旧规则下的结论。三份模板是逐字复制的，
    所以要逐份钉住：漏一份，那一页就会把旧结论当新结论显示。
    """
    for name in TEMPLATES:
        body = _strip_js_comments(_template_source(name))
        assert "stale_note" in body, f"{name} 没有把「这份结论是旧的」显示出来"
        # 不能只在注释里提一句：必须真的拼进 meta 那一行。
        assert "meta.textContent" in body, f"{name} 里连 meta 行都没了？"


def test_the_project_card_badge_marks_a_stale_level():
    """项目卡片上的风险标签也要标出「旧」。

    那张卡片是给人**一眼看**的地方：一个来自旧规则的等级与刚跑出来的一模一样。
    只在抽屉里说明不够 —— 用户不点开抽屉就看不到。
    """
    body = _strip_js_comments(_template_source("templates/merged_project_view.html"))
    assert "（旧）" in body, "风险标签没有标出「旧结论」"
    # 标签态要由数据驱动：读取侧给的 `stale` 必须真的传到渲染函数里，
    # 否则上面那句只是一段永远不会执行的字符串。
    assert "result.stale" in body, "`stale` 没被传进标签渲染，标签永远只会显示「未分析/等级」"
