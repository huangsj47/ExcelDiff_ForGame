#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""这批结论是由「哪套提示词 / skill / 规则 / 模型 / 门槛」产生的。

## 为什么单独一个模块

两件事把它与编排层分开：

1. **它要被读取侧独立用到** —— 判一份历史结论还能不能复用（`_is_run_fresh`）、
   `/latest` 的回放判等、以及无头 Chrome 的截图脚本都只关心这一份指纹，不关心
   分析怎么跑；
2. `services/ai_analysis_service.py` 贴着文件长度硬上限（2000 行），而这段是本模块里
   唯一**只依赖配置与版本号、不碰引擎**的一块，搬出来代价最小。

## 指纹里必须有门槛（`analysis_revision`）

`prompt_version()` / `skill_revision()` / `rules_version()` 三个都是**源码内容哈希**，
而 `rules_version()` 只哈希 `rules.py` 一个文件（`RULE_SOURCE_FILES`）—— 用户改
`min_severity` / `min_confidence` / `max_anomalies_per_run` 时它**逐字不变**。而那几项
直接决定「哪些结论会被报出来」：把门槛从 `high` 收到 `critical` 之后重看老提交，
拿到的会是旧门槛下归一化的结论，连「规则变了」那句提示都不出现。

这件事规则层早就写清楚了（`RuleThresholds.revision_component` 的 docstring：「门槛变了
就是另一个问题，**必须**重跑」），那个方法也一直在，只是**没有任何生产调用点** ——
`AiAnalysisRun.analysis_revision` 这一列从建出来起就没被写过。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from models import Project, db
from models.ai_analysis import AiAnalysisRun, AiProjectAnalysisConfig
from services.ai.prompt import prompt_version
from services.ai.rules import RulesConfigError, RuleThresholds, rules_version
from services.ai.skill_loader import skill_revision

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _project_config_row(project_id: int) -> Optional[AiProjectAnalysisConfig]:
    return AiProjectAnalysisConfig.query.filter_by(project_id=project_id).first()


def current_provenance(project_id: int) -> dict:
    """这批结论是由「哪套提示词 / skill / 规则 / 模型 / 门槛」产生的。

    **必须落库，也必须参与缓存判等**，理由有两条：

    1. 不记录就没法说明一份结论是怎么来的。改了 skill 之后重看同一份结论，无法判断
       它是新规则跑出来的还是旧的。
    2. 缓存只按「目标 + 时间」命中时，改了 skill、提示词、规则或换了模型之后，90 天内
       重看老提交拿到的仍然是**旧结论** —— 从用户角度看就是「我的改动没生效」。

    ## 配置不合法时不降级成默认值

    `from_config` 对不认识的取值抛 `RulesConfigError`（「不降级、不猜」）。读取侧从来不
    校验配置，所以库里真被手工改坏时不能让它把 `/latest` 变成 500 —— 给一个独有的取值
    即可：非法配置照样与任何历史结论都不相等，于是**照旧逼出一次重跑**，而重跑时会以
    正常路径报出配置错误。
    """
    project = db.session.get(Project, project_id)
    project_code = getattr(project, "code", None) if project else None
    row = _project_config_row(project_id)
    resolved: Mapping[str, Any] | None = row.resolved() if row is not None else None
    model = str((resolved or {}).get("api_model") or "")
    try:
        analysis_revision = RuleThresholds.from_config(resolved).revision_component()
    except RulesConfigError:
        analysis_revision = "invalid-config"
    return {
        "prompt_version": prompt_version(),
        "skill_version": skill_revision(_REPO_ROOT, project_code=project_code),
        "rules_version": rules_version(),
        "analysis_revision": analysis_revision,
        "model": model.strip(),
    }


def provenance_matches(run: AiAnalysisRun, expected: Optional[dict]) -> bool:
    """run 的溯源字段是否与「现在这套」一致。

    老库上的行这些列是 NULL —— 一律判为**不一致**（即不可复用）。代价是老提交会被
    重新分析一次，换来的是「绝不会把旧规则下的结论当成新规则下的结论」。
    """
    if not expected:
        return True
    for key, value in expected.items():
        if str(getattr(run, key, None) or "") != str(value or ""):
            return False
    return True
