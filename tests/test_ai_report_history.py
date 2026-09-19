# -*- coding: utf-8 -*-
"""「历次结论」的取数：**与 `/latest` 同一把尺子**、只列有结论的、摘要说得清。

这个列表的意义全在「它是同一份数据的另一个入口」上，所以错法都很隐蔽：

* 窗口与 `/latest` 不一样 → 列表里有、点进去说没有（或反过来）；
* 把 `running` / `pending` 也列进来 → 「历次结论」里点开一条没有结论的东西；
* 周版本按 `target_id`（config id）而不是 `target_key`（分组键）取 → **列表里一条都没有**，
  因为读侧从来不按 config id 存（`build_weekly_group_key` 才是身份）；
* 摘要把 `# 变更理解` 这种标题当成内容 → 一列全是「# 变更理解」，等于没有摘要。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models.ai_analysis import AiAnalysisRun
from services import ai_report_history_service as history
from tests.test_ai_history_survives_restart import REPORT_TEXT, _setup_config
from tests.test_ai_report_export_route import _run


@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


def _weekly_key(cfg) -> str:
    return ai_service.build_weekly_group_key(cfg)


def _weekly_run(project_id: int, cfg, **over):
    over.setdefault("target_type", "weekly")
    over.setdefault("target_id", cfg.id)
    over.setdefault("target_key", _weekly_key(cfg))
    return _run(project_id=project_id, **over)


# ---------------------------------------------------------------------------
#  列表：新的在前、只列有结论的
# ---------------------------------------------------------------------------
def test_the_list_is_newest_first_and_only_carries_conclusions():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        now = datetime.now(timezone.utc)
        older = _weekly_run(project.id, cfg, created_at=now - timedelta(days=2))
        newer = _weekly_run(project.id, cfg, created_at=now - timedelta(days=1))
        # 中间态不进这个列表
        _weekly_run(project.id, cfg, status="running", response_text="",
                    created_at=now - timedelta(hours=3))
        _weekly_run(project.id, cfg, status="pending", response_text="",
                    created_at=now - timedelta(hours=2))
        # 别的目标也不进
        _run(project_id=project.id, target_type="commit", target_id=999999)

        payload = history.list_target_runs(kind="weekly", target_key=_weekly_key(cfg))

        assert [row["run_id"] for row in payload["runs"]] == [newer.id, older.id]
        assert payload["total"] == 2
        assert payload["truncated"] is False


def test_a_run_outside_the_window_is_not_listed():
    """窗口与 `/latest` 同一把尺子（同一个保留天数）—— 否则会出现「列表里有、
    点进去说没有」这种没法解释的状态。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        now = datetime.now(timezone.utc)
        old = _weekly_run(project.id, cfg,
                          created_at=now - timedelta(days=history.ANALYSIS_CACHE_DAYS + 5))
        fresh = _weekly_run(project.id, cfg, created_at=now - timedelta(days=1))

        payload = history.list_target_runs(kind="weekly", target_key=_weekly_key(cfg))

        assert [row["run_id"] for row in payload["runs"]] == [fresh.id]
        assert old.id not in [row["run_id"] for row in payload["runs"]]
        assert payload["window_days"] == history.ANALYSIS_CACHE_DAYS


def test_the_weekly_list_uses_the_group_key_not_the_config_id():
    """周版本的身份是**分组键**，不是 config id（读侧从不按 config id 存）。

    传 config id 时必须**报错**，而不是退回「`target_key IS NULL`」那一桶：
    后者会安静地列出「一批没有分组键的周版本运行」，看起来像这个周版本的历史，
    其实谁都不是（而且那个断言还会随测试库里别的行时红时绿）。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _weekly_run(project.id, cfg)

        with pytest.raises(ValueError):
            history.list_target_runs(kind="weekly", target_id=cfg.id)
        with pytest.raises(ValueError):
            history.list_target_runs(kind="weekly")

        rows = history.list_target_runs(kind="weekly", target_key=_weekly_key(cfg))["runs"]
        assert rows and rows[0]["run_id"] == run.id


def test_the_limit_and_the_truncated_flag_tell_the_truth():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        now = datetime.now(timezone.utc)
        for index in range(4):
            _weekly_run(project.id, cfg, created_at=now - timedelta(minutes=index))

        payload = history.list_target_runs(
            kind="weekly", target_key=_weekly_key(cfg), limit=2
        )

        assert len(payload["runs"]) == 2
        assert payload["total"] == 4
        assert payload["truncated"] is True
        assert payload["limit"] == 2


def test_a_silly_limit_is_clamped():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        _weekly_run(project.id, cfg)

        assert history.list_target_runs(
            kind="weekly", target_key=_weekly_key(cfg), limit=0
        )["limit"] == history.DEFAULT_HISTORY_LIMIT
        assert history.list_target_runs(
            kind="weekly", target_key=_weekly_key(cfg), limit=100000
        )["limit"] == history.MAX_HISTORY_LIMIT


def test_an_unknown_kind_is_refused_loudly():
    with app.app_context():
        with pytest.raises(ValueError):
            history.list_target_runs(kind="everything")


def test_a_target_that_is_running_says_so_even_with_an_empty_list():
    """「一次都没跑过」与「正在跑、还没跑完第一次」是两件事。

    这个弹层最常被打开的时刻正是「正在跑、想看看上一次」—— 那时列表可能是空的，
    界面若只说「这个目标还没有跑过分析」，用户会以为平台把他的分析弄丢了。
    """
    with app.app_context():
        project, _repo, cfg = _setup_config()
        key = _weekly_key(cfg)
        _weekly_run(project.id, cfg, status="running", response_text="")

        payload = history.list_target_runs(kind="weekly", target_key=key)

        assert payload["runs"] == [], "中间态不进列表"
        assert payload["in_progress"] is True
        assert payload["total"] == 0


def test_a_target_with_nothing_at_all_is_not_reported_as_running():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        payload = history.list_target_runs(kind="weekly", target_key=_weekly_key(cfg))

        assert payload["runs"] == []
        assert payload["in_progress"] is False


# ---------------------------------------------------------------------------
#  每一行：给人看的字都在服务端拼好
# ---------------------------------------------------------------------------
def test_a_successful_row_carries_the_words_the_ui_shows():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _weekly_run(
            project.id, cfg,
            payload={
                "risk_level": "mid_high",
                "risk_reasons": ["模型报出 2 条达门槛的问题"],
                "anomalies": [{"title": "a"}, {"title": "b"}],
            },
            scope="incremental",
            trigger_source="scheduled",
            request_payload='{"focus": {"key": "table", "label": "仅配表仓库"}}',
        )

        row = history.list_target_runs(
            kind="weekly", target_key=_weekly_key(cfg)
        )["runs"][0]

        assert row["run_id"] == run.id
        assert row["status"] == "succeeded"
        assert row["status_label"] == "已有结论"
        assert row["risk_label"] == "中高"
        assert row["scope_label"].startswith("增量")
        assert row["trigger_label"] == "定时"
        assert row["focus_label"] == "仅配表仓库"
        assert row["anomaly_count"] == 2
        assert row["exportable"] is True
        assert row["created_at_display"], "没有时间就没法认出是哪一次"


def test_the_summary_is_the_first_thing_the_report_actually_says():
    """**不是** `# 变更理解` 那一行 —— 那是结构不是内容。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        _weekly_run(project.id, cfg)

        row = history.list_target_runs(
            kind="weekly", target_key=_weekly_key(cfg)
        )["runs"][0]

        assert row["summary"] == "把奖励发放从先扣后发改成先发后扣。"


def test_the_summary_skips_headings_tables_and_list_markers():
    assert history._first_line_of_report(
        "# 变更理解\n\n| 表 | 说明 |\n|---|---|\n| a | b |\n\n- 第一条实质内容\n"
    ) == "第一条实质内容"
    assert history._first_line_of_report("```\ncode\n```\n\n正文在这里") == "正文在这里"
    assert history._first_line_of_report("# 只有标题\n\n## 还是标题\n") == ""


def test_a_long_summary_is_cut_with_an_ellipsis():
    long_line = "这" * 200
    assert history._first_line_of_report("# 一\n\n" + long_line).endswith("…")
    assert len(history._first_line_of_report("# 一\n\n" + long_line)) <= history.SUMMARY_MAX_CHARS + 1


def test_a_failed_row_says_why_and_is_not_exportable():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        _weekly_run(project.id, cfg, status="failed", response_text="",
                    error_message="额度用完了")

        row = history.list_target_runs(
            kind="weekly", target_key=_weekly_key(cfg)
        )["runs"][0]

        assert row["status_label"] == "分析失败"
        assert row["summary"] == "失败：额度用完了"
        assert row["risk_label"] == "", "失败没有结论，不该摆一个风险等级"
        assert row["exportable"] is False


def test_a_failed_row_without_a_reason_does_not_invent_one():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        _weekly_run(project.id, cfg, status="failed", response_text="", error_message="")

        row = history.list_target_runs(
            kind="weekly", target_key=_weekly_key(cfg)
        )["runs"][0]

        assert row["summary"] == "失败：平台上没有留下原因"


def test_a_degraded_row_says_so_up_front():
    """降级是「这份结论的可信度」的一部分，混在一句话后面会被漏读。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        _weekly_run(
            project.id, cfg,
            payload={"risk_level": "high", "degradation_label": "轮次耗尽，按已有证据出报告"},
        )

        row = history.list_target_runs(
            kind="weekly", target_key=_weekly_key(cfg)
        )["runs"][0]

        assert row["summary"].startswith("降级（轮次耗尽，按已有证据出报告）：")


def test_a_succeeded_run_without_a_report_is_not_exportable():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        _weekly_run(project.id, cfg, response_text="")

        row = history.list_target_runs(
            kind="weekly", target_key=_weekly_key(cfg)
        )["runs"][0]

        assert row["exportable"] is False, "导出一份只有元信息的文件比不给更糟"
        assert row["summary"] == "这次没有留下可读的报告正文"


# ---------------------------------------------------------------------------
#  取某一次：形状与 `/latest` 一致
# ---------------------------------------------------------------------------
def test_getting_one_run_gives_the_same_shape_as_latest():
    """一个渲染器通吃的前提：**形状逐字相同**。"""
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _weekly_run(project.id, cfg)

        one = history.get_run_report(run.id)
        latest = ai_service.get_latest_weekly_result(cfg.id)

        assert one["run_id"] == latest["run_id"] == run.id
        assert set(one) == set(latest), f"形状不一致：{set(one) ^ set(latest)}"
        assert one["response_text"] == REPORT_TEXT
        assert one["result"]["risk_level"] == latest["result"]["risk_level"]


def test_getting_a_failed_run_gives_the_failure_shape():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _weekly_run(project.id, cfg, status="failed", response_text="",
                          error_message="模型没答上来")

        payload = history.get_run_report(run.id)

        assert payload["status"] == "failed"
        assert payload["error_message"] == "模型没答上来"
        assert payload["result"] is None
        assert payload["response_text"] == ""


def test_getting_a_running_run_says_so_instead_of_pretending():
    with app.app_context():
        project, _repo, cfg = _setup_config()
        run = _weekly_run(project.id, cfg, status="running", response_text="")

        payload = history.get_run_report(run.id)

        assert payload["status"] == "running"
        assert payload["result"] is None


def test_getting_a_missing_run_is_none():
    with app.app_context():
        assert history.get_run_report(99999999) is None
        assert history.get_run_report(0) is None
