"""AI 用量面板的数据组装：跨项目总览、单项目（含周版本）下钻、单次运行明细。

## 三个端点共用这里的口径

金额与命中率的算法全在 `services/ai/usage.py` 与 `services/ai/pricing.py`，
本模块只做「查哪些行、怎么分组、给界面摆成什么形状」。这样界面上的三个层级
（平台 → 项目 → 周版本 → 单次运行）用的是同一套数，不会出现「总览的数字加起来
不等于下钻的数字」。

## 每个项目的价格表是各自的

单价表存在**项目配置**里（`ai_project_analysis_config.model_price_table`），所以
跨项目总览的合计金额只有在「每个项目都算得出、且币种一致」时才给 —— 否则给 `None`
加一句理由。混着几种币种加出来的数字比没有数字更糟。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from models import Project, WeeklyVersionConfig
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from services.ai.pricing import money
from services.ai.usage import aggregate_runs, usage_from_run
from services.ai_analysis_service import project_price_table

# 单次运行明细里最多回多少轮。轮次上限由配置决定（默认 8，最大 30），这个数字只是
# 「别再多了」的兜底，避免一条异常数据把界面撑爆。
MAX_ROUNDS = 60

# 下钻页每次最多列多少条运行记录。汇总数字仍然按**全部**运行算 —— 只截断列表，
# 不截断统计，否则页面上的合计会随着「看多少行」变化。
MAX_RUNS = 200


def _price_block(table, errors: Sequence[str]) -> dict[str, Any]:
    """这一层用的价格表状态。界面据此决定「显示费用」还是「提示去配置」。"""
    return {
        "version": table.version if table else "",
        "currency": table.currency if table else "",
        "source": table.source if table else "",
        "configured": bool(table and table.models),
        "errors": list(errors or ()),
    }


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _runs_query(project_ids: Optional[Iterable[int]]):
    query = AiAnalysisRun.query
    if project_ids is not None:
        ids = list(project_ids)
        if not ids:
            return None
        query = query.filter(AiAnalysisRun.project_id.in_(ids))
    return query


def _totals_cost(entries: Sequence[dict]) -> Optional[dict[str, Any]]:
    """跨项目的费用合计。

    **只有每个项目都算得出、且币种一致时才给数字。** 少算一个项目、或把两种币种直接
    相加，得到的都是一个看起来正常、实际错的金额 —— 这比「算不出」危险得多。
    """
    costs = [entry.get("cost") for entry in entries]
    if not costs or any(not item or item.get("amount") is None for item in costs):
        return None
    currencies = {str(item.get("currency") or "") for item in costs}
    if len(currencies) != 1:
        return None
    total = sum((Decimal(str(item["amount"])) for item in costs), Decimal(0))
    return {
        "amount": money(total),
        "currency": currencies.pop(),
        "reason": "",
        "notes": ["按各项目自己的价格表估算后相加"],
        "lines": [],
    }


def usage_overview(accessible_project_ids: Optional[Iterable[int]]) -> dict[str, Any]:
    """跨项目总览。`accessible_project_ids=None` 表示全部（平台管理员）。

    权限过滤在这里做**查询级**过滤（`project_id IN (...)`），而不是查完再筛：
    后者一旦哪天有人漏了那一步，就是「无权用户看到别人的消耗」。
    """
    query = _runs_query(accessible_project_ids)
    entries: list[dict[str, Any]] = []
    all_runs: list[AiAnalysisRun] = []

    if query is not None:
        all_runs = query.order_by(AiAnalysisRun.created_at.desc()).all()
        grouped: dict[int, list[AiAnalysisRun]] = {}
        for run in all_runs:
            grouped.setdefault(run.project_id, []).append(run)

        names = {
            project.id: (project.name or project.code or f"项目 {project.id}")
            for project in Project.query.filter(Project.id.in_(list(grouped))).all()
        }
        for project_id, runs in grouped.items():
            table, errors = project_price_table(project_id)
            stats = aggregate_runs(runs, price_table=table)
            entries.append(
                {
                    "project_id": project_id,
                    "name": names.get(project_id, f"项目 {project_id}"),
                    "runs": stats["runs"],
                    "collected_runs": stats["collected_runs"],
                    "tokens": stats["tokens"],
                    "cache": stats["cache"],
                    "tools": stats["tools"],
                    "missing_runs": stats["missing_runs"],
                    "cost": stats["cost"],
                    "pricing": _price_block(table, errors),
                    "last_run_at": _iso(
                        max((run.created_at for run in runs if run.created_at), default=None)
                    ),
                }
            )

    entries.sort(key=lambda item: item["runs"], reverse=True)

    totals = {"runs": 0, "tokens": {}, "cache": {}, "tools": {}, "cost": None}
    if all_runs:
        # 合计不带价格表算（各项目的表不同），费用另算 —— 见 _totals_cost。
        stats = aggregate_runs(all_runs, price_table=None)
        totals = {
            "runs": stats["runs"],
            "collected_runs": stats["collected_runs"],
            "tokens": stats["tokens"],
            "cache": stats["cache"],
            "tools": stats["tools"],
            "missing_runs": stats["missing_runs"],
            "cost": _totals_cost(entries),
        }

    return {
        "success": True,
        "projects": entries,
        "totals": totals,
        "generated_at": _iso(datetime.now(timezone.utc)),
    }


def project_usage(project_id: int) -> dict[str, Any]:
    """单项目下钻：周版本维度 + 逐次运行 + 按工具类型。

    「当前周版本」取**最近分析过**的那个 group_key（`is_latest=True`）—— 界面上写的是
    「最近分析的周版本」，不叫「当前周版本」：平台无法从分析记录反推出用户心里那个
    「当前」，用词必须与事实一致。
    """
    table, errors = project_price_table(project_id)
    runs = (
        AiAnalysisRun.query.filter_by(project_id=project_id)
        .order_by(AiAnalysisRun.created_at.desc())
        .all()
    )
    stats = aggregate_runs(runs, price_table=table)
    pricing = _price_block(table, errors)

    weekly_groups: dict[str, list[AiAnalysisRun]] = {}
    for run in runs:
        if run.target_type == "weekly" and run.target_key:
            weekly_groups.setdefault(run.target_key, []).append(run)

    config_ids = {
        group[0].target_id for group in weekly_groups.values() if group and group[0].target_id
    }
    config_names: dict[int, str] = {}
    if config_ids:
        config_names = {
            config.id: (config.name or f"周版本配置 {config.id}")
            for config in WeeklyVersionConfig.query.filter(
                WeeklyVersionConfig.id.in_(list(config_ids))
            ).all()
        }

    weekly_versions = []
    for group_key, group in weekly_groups.items():
        group_stats = aggregate_runs(group, price_table=table)
        config_id = group[0].target_id
        weekly_versions.append(
            {
                "group_key": group_key,
                "config_id": config_id,
                "config_name": config_names.get(config_id, "") if config_id else "",
                "runs": group_stats["runs"],
                "collected_runs": group_stats["collected_runs"],
                "tokens": group_stats["tokens"],
                "cache": group_stats["cache"],
                "tools": group_stats["tools"],
                "missing_runs": group_stats["missing_runs"],
                "cost": group_stats["cost"],
                "last_run_at": _iso(
                    max((run.created_at for run in group if run.created_at), default=None)
                ),
                "is_latest": False,  # 排序后按时间重新标记，见下面几行
            }
        )
    # 按「最近一次分析时间」倒序，「最近分析过的周版本」排在最前面并被标成 is_latest。
    weekly_versions.sort(key=lambda item: item["last_run_at"] or "", reverse=True)
    for index, item in enumerate(weekly_versions):
        item["is_latest"] = index == 0

    return {
        "success": True,
        "project_id": project_id,
        "totals": {
            "runs": stats["runs"],
            "collected_runs": stats["collected_runs"],
            "tokens": stats["tokens"],
            "cache": stats["cache"],
            "tools": stats["tools"],
            "missing_runs": stats["missing_runs"],
            "cost": stats["cost"],
        },
        "weekly_versions": weekly_versions,
        "runs": [_run_row(run, table) for run in runs[:MAX_RUNS]],
        "runs_truncated": len(runs) > MAX_RUNS,
        "pricing": pricing,
        "generated_at": _iso(datetime.now(timezone.utc)),
    }


def _run_row(run: AiAnalysisRun, table) -> dict[str, Any]:
    usage = usage_from_run(run, price_table=table)
    return {
        "run_id": run.id,
        "target_type": run.target_type,
        "target_id": run.target_id,
        "target_key": run.target_key,
        "status": run.effective_status,
        "scope": run.scope,
        "trigger_source": run.trigger_source,
        "model": run.model or "",
        "created_at": _iso(run.created_at),
        "finished_at": _iso(run.finished_at),
        "usage": usage,
    }


def run_usage(run_id: int) -> Optional[dict[str, Any]]:
    """单次运行的明细：token 三段、命中率、费用、逐轮、按工具类型。

    权限判定在路由层按**这条运行自己的 `project_id`** 做 —— 不能只信 URL 里的项目号，
    否则换个项目号就能读到别人的运行明细。
    """
    run = AiAnalysisRun.query.filter_by(id=run_id).first()
    if run is None:
        return None

    table, errors = project_price_table(run.project_id)
    rounds = (
        AiAnalysisTrace.query.filter_by(run_id=run.id)
        .order_by(AiAnalysisTrace.round_index.asc())
        .limit(MAX_ROUNDS)
        .all()
    )
    return {
        "success": True,
        "run": {
            **_run_row(run, table),
            "rounds_used": run.rounds_used,
            "tool_requests_used": run.tool_requests_used,
            "anomalies_found": run.anomalies_found,
            "dropped_count": run.dropped_count,
            "error_message": run.error_message or "",
            "prompt_version": run.prompt_version or "",
            "skill_version": run.skill_version or "",
            "rules_version": run.rules_version or "",
        },
        # 逐轮：为什么后几轮更贵（提示词每轮重发上一轮的上下文），只有逐轮列出来才看得出。
        "rounds": [
            {
                "round_index": row.round_index,
                "outcome": row.outcome or "",
                "parsed_ok": bool(row.parsed_ok),
                "tokens_input": row.tokens_input,
                "tokens_output": row.tokens_output,
                "cache_read_tokens": row.cache_read_tokens,
                "cache_write_tokens": row.cache_write_tokens,
                "request_chars": row.request_chars,
                "context_chars": row.context_chars,
                "duration_ms": row.duration_ms,
                "error": row.error or "",
            }
            for row in rounds
        ],
        "rounds_truncated": len(rounds) >= MAX_ROUNDS,
        "pricing": _price_block(table, errors),
    }
