# -*- coding: utf-8 -*-
"""每个**分片**报几条结论，是一道**写给模型看的额度**。

## 它与「单次异常上限」不是同一件事

`max_anomalies_per_run` 是**事后闸门**：模型答完之后按严重度截，不省任何一次调用
（`models/ai_analysis/project_config.py` 的注释里写着这一点）。分片各自报一两万字、
最后只留 20 条，那笔输出 token 是白花的 —— 实测一次分析里分片的输出占七成以上。

`max_anomalies_per_subagent` 走的是另一条路：这个数字**进分片任务书**，模型从一开始
就按它写。所以这一组用例钉的是「那个数字真的到了任务书里、而且跟着配置走」，不是
「截断函数能截对」—— 后者由 `tests/test_ai_cap_is_disclosed.py` 管。

## 两条边界

- 分片额度**不超过全次上限**：项目把单次上限调小、却留下分片额度的话，会变成
  「每个分片照报 10 条、汇总再砍一半」，白写的 token 正是这一栏要省的。
- 下限是 1 不是 0：0 会让分片一个字都不许写结论，那是锁不是额度。
"""
import pytest

from models.ai_analysis.project_config import (
    DEFAULT_MAX_ANOMALIES_PER_SUBAGENT,
    AiProjectAnalysisConfig,
)
from services.ai.endpoint_service import (
    FIELD_DEFAULTS,
    FIELD_RULES,
    ConfigValidationError,
    FieldError,
    validate_field,
    validate_payload,
)
from services.ai.engine import EngineLimits
from services.ai.subagent import build_member_task, build_synthesis_task, plan_family
from services.ai_analysis_service import _engine_limits


def _member_task(*, cap: int, count: int = 3) -> str:
    plan = plan_family(
        mode="weekly",
        enabled=True,
        count=count,
        limits=EngineLimits(max_anomalies_per_subagent=cap),
    )
    assert plan is not None, "前提不成立：这个规模本该走子代理那条路"
    return build_member_task(plan.members[0], plan)


def test_the_default_matches_between_the_model_and_the_service_layer():
    """默认值有两份副本（模型层 / 服务层），漂移过一次就够受了。

    2026-09-23 收敛后这一栏**不再可配**（不在 FIELD_RULES、提交即报错），但出厂默认
    仍有两处副本：模型层常量与「无配置行」路径的 FIELD_DEFAULTS —— 单提交路径与
    `resolved()` 继续读它们，两边必须一致。
    """
    assert DEFAULT_MAX_ANOMALIES_PER_SUBAGENT == 10
    assert _engine_limits({}).max_anomalies_per_subagent == DEFAULT_MAX_ANOMALIES_PER_SUBAGENT
    assert FIELD_DEFAULTS["max_anomalies_per_subagent"] == DEFAULT_MAX_ANOMALIES_PER_SUBAGENT
    # 收敛键不再有校验规则（界面不渲染、schema 不下发）。
    assert "max_anomalies_per_subagent" not in FIELD_RULES


def test_the_member_task_carries_the_cap():
    text = _member_task(cap=7)
    assert "最多 7 条" in text, "额度没写进分片任务书 —— 模型不知道，省不到 token"


def test_the_cap_follows_the_configuration():
    """两个不同的值必须给出两份不同的任务书：钉死一个数字会被写死常量蒙混过去。"""
    assert "最多 3 条" in _member_task(cap=3)
    assert "最多 12 条" in _member_task(cap=12)
    assert "最多 3 条" not in _member_task(cap=12)


def test_the_cap_reaches_the_task_book_from_the_project_config():
    """从配置字典一路到任务书，中间那几层都要真的传。"""
    limits = _engine_limits({"max_anomalies_per_subagent": 4, "subagent_count": 3})
    plan = plan_family(mode="weekly", enabled=True, count=3, limits=limits)
    assert "最多 4 条" in build_member_task(plan.members[0], plan)


def test_a_shard_is_never_allowed_more_than_the_whole_run():
    limits = _engine_limits({"max_anomalies_per_subagent": 10, "max_anomalies_per_run": 3})
    assert limits.max_anomalies_per_subagent == 3


@pytest.mark.parametrize("run_cap", [0, 1])
def test_a_non_positive_run_cap_does_not_lock_the_shards_out(run_cap):
    """全次上限是 0 时额度按 1 算 —— 0 条不是一个额度，而且会让任务书里出现「最多 0 条」。"""
    limits = _engine_limits(
        {"max_anomalies_per_subagent": 10, "max_anomalies_per_run": run_cap}
    )
    assert limits.max_anomalies_per_subagent == 1


def test_a_null_column_reads_as_the_default():
    """老库上加的列是 NULL（迁移不带 DEFAULT 子句），必须读成默认值而不是 0。"""
    assert _engine_limits({"max_anomalies_per_subagent": None}).max_anomalies_per_subagent == 10
    assert AiProjectAnalysisConfig().resolved()["max_anomalies_per_subagent"] == 10


def test_the_synthesis_is_told_the_candidates_are_a_capped_sample():
    """汇总不该把「候选只有这些」读成「这个版本只有这些」。"""
    plan = plan_family(
        mode="weekly", enabled=True, count=3, limits=EngineLimits(max_anomalies_per_subagent=6)
    )
    text = build_synthesis_task(plan, steps=())
    assert "最多 6 " in text
    assert "抽样" in text


def test_the_field_is_no_longer_writable_through_the_endpoint_layer():
    """2026-09-23 收敛后这一栏**收到即报错**：周版本路径的每片额度由平台推导
    （`auto_sizing.anomalies_per_subagent`），不再接受手动配置 —— 静默存进去的
    话，用户以为改了每片额度，而它根本不被读。

    （「老库加列读成 NULL 也不炸」由 `tests/test_ai_models_and_migration.py` 的
    `CONFIG_NEW_COLUMNS` 那份清单验 —— 那一组是拿真库跑一遍迁移的。）
    """
    # 单字段入口：不再是可配置字段。
    with pytest.raises(FieldError):
        validate_field("max_anomalies_per_subagent", "8")
    # 整体提交入口：报的是「已改为平台自动推导」那条，而不是「不认识这个字段」。
    with pytest.raises(ConfigValidationError) as excinfo:
        validate_payload({"max_anomalies_per_subagent": "8"})
    errors = excinfo.value.errors
    assert len(errors) == 1
    assert "平台自动推导" in errors[0].message


def test_the_run_cap_still_wins_at_the_aggregation_step():
    """分片额度是**额度**不是承诺：项目把全次上限调大时，分片额度仍然管着自己那一段。"""
    limits = _engine_limits({"max_anomalies_per_subagent": 4, "max_anomalies_per_run": 50})
    assert limits.max_anomalies_per_subagent == 4
