#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis framework service (stubbed executor).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError

from models import (
    Commit,
    Repository,
    WeeklyVersionConfig,
    WeeklyVersionDiffCache,
    db,
)
from models.ai_analysis import (
    AiAnalysisRun,
    AiProjectAnalysisConfig,
    AiProjectApiKey,
    AiWeeklyAnalysisState,
)
from models.ai_analysis.project_config import DEFAULT_MAX_FILES_PER_RUN
from services.ai.endpoint_service import (
    FIELD_DEFAULTS,
    OPENAI_BASE_URL,
    PROBE_TIMEOUT_SECONDS,
    ConfigValidationError,
    FieldError,
    build_probe_client,
    describe_field_schema,
    source_of,
    validate_endpoint_ready,
    validate_field,
    validate_payload,
)
from utils.dpapi_utils import DPAPI_PREFIX, decrypt_dpapi
from utils.logger import log_print
from utils.security_utils import decrypt_credential, encrypt_credential

MAX_FILES_DEFAULT = DEFAULT_MAX_FILES_PER_RUN
FULL_ANALYSIS_FILE_THRESHOLD = 50
FULL_ANALYSIS_RATIO_THRESHOLD = 0.30
EXECUTION_VERSION = "latest"
ANALYSIS_CACHE_DAYS = int(os.environ.get("AI_ANALYSIS_CACHE_DAYS", "90"))
DEFAULT_PROMPT_TEMPLATE = """以下内容用于补充平台内置的分析协议。

内置协议（检查维度、输出格式、证据与置信度门槛、反误报条款）由平台强制提供，
请**不要在这里重复，也不要试图放宽**它们。这里只写本项目特有的事实与偏好。

请按下面几项填写；没有把握的**留空即可，不要编造** —— 模型会把缺失当作
「信息缺口」标出来，而编造出来的事实会被当成真的。

1. 技术栈与工程结构
   （例：Unity + C# + Lua；配表放在 config/ 下，生成物是 CfgXxx.lua）
2. 配表规范要点
   （例：ID 为六位制、前两位表示类型段；能分表则分表）
3. 重点模块（这些模块的改动需要压测或完整回归）
   （例：登录、充值、匹配、战斗、邮件、排行榜、全服推送）
4. 本项目的红线与历史高频事故
5. 输出偏好
   （例：风险点请附复现步骤与影响范围；报告控制在 800 字内）
"""
CRITICAL_PATH_PATTERNS = (
    r"/config/",
    r"/configs/",
    r"/sql/",
    r"/schema/",
    r"/migrations/",
    r"/auth/",
    r"/permission/",
    r"/payment/",
    r"/billing/",
    r"\.sql$",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _analysis_cache_cutoff() -> datetime:
    return _utcnow() - timedelta(days=ANALYSIS_CACHE_DAYS)


def _get_project_config_row(project_id: int) -> Optional[AiProjectAnalysisConfig]:
    return AiProjectAnalysisConfig.query.filter_by(project_id=project_id).first()


def get_project_analysis_config(project_id: int) -> dict:
    """读项目维度的 AI 配置。

    返回体里额外带上 `field_schema` / `field_defaults`，让界面**从同一个事实源**渲染
    标签、范围与默认值。范围写死在模板里就会出现「界面写着 1~30、后端按别的范围校验」
    这类前后端不一致，而那正是这次要修掉的东西之一。

    **API Key 只回状态，绝不回显**（连掩码都不给）：掩码会让用户误以为能对出来。
    """
    row = _get_project_config_row(project_id)
    if row is None:
        values = dict(FIELD_DEFAULTS)
        meta = {"configured": False, "updated_by": None, "updated_at": None}
    else:
        values = row.resolved()
        meta = {
            "configured": True,
            "updated_by": row.updated_by,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    key_state = get_project_api_key_status(project_id)
    return {
        **meta,
        **values,
        "source": source_of(values.get("api_base_url", "")),
        "openai_base_url": OPENAI_BASE_URL,
        "api_key": key_state,
        "field_schema": describe_field_schema(),
        "endpoint_ready": not validate_endpoint_ready(values, has_key=bool(key_state.get("configured"))),
    }


def update_project_analysis_config(
    project_id: int, payload: dict, updated_by: str = ""
) -> Tuple[bool, str, list]:
    """保存项目配置。返回 `(成功, 提示语, 字段级错误列表)`。

    **校验失败不改库、不回填、不夹取。** 旧实现用 `_clamp_int` 把越界值悄悄改成边界值：
    用户填 5000、界面回填 1000，中间没有任何提示，他以为存进去的是 5000。现在的做法是
    把「范围是多少、你填的是多少」原样告诉他，让他自己改。
    """
    row = _get_project_config_row(project_id)
    if row is None:
        row = AiProjectAnalysisConfig(project_id=project_id)
        db.session.add(row)

    try:
        normalized = validate_payload(payload or {})
    except ConfigValidationError as exc:
        db.session.rollback()
        return False, str(exc), [item.as_dict() for item in exc.errors]

    for field_name, value in normalized.items():
        setattr(row, field_name, value)
    if normalized:
        row.updated_by = (updated_by or "").strip()
        row.updated_at = _utcnow()
    db.session.commit()
    return True, "AI 分析配置已保存。", []


def build_endpoint_client(
    project_id: int, override: Optional[dict] = None
) -> Tuple[Optional[object], list]:
    """按「请求体优先、已保存配置回退」构造探测用的客户端。

    返回 `(client, errors)`；errors 是字段级列表，**格式与保存配置时的 400 一致** ——
    前端因此可以复用同一套「哪一栏错了」的渲染，不必为测试连接再写一份。

    「未保存也能测」是刻意的：用户填完地址/Token/模型名之后，第一件想做的事就是
    确认这组配置能不能用。要求他先保存一个可能错的配置再测，是很别扭的顺序。
    输入框留空表示「沿用已保存的值」，而不是「清空」。
    """
    config = get_project_analysis_config(project_id)
    payload = dict(override or {})

    base_url = str(payload.get("api_base_url") or config.get("api_base_url") or "").strip()
    model = str(payload.get("api_model") or config.get("api_model") or "").strip()
    typed_key = str(payload.get("api_key") or "").strip()
    api_key = typed_key or (_get_project_api_key(project_id) or "")

    problems: list = []
    if not base_url:
        problems.append(FieldError("api_base_url", "接口地址", "请先填写接口地址"))
    else:
        try:
            base_url = validate_field("api_base_url", base_url)
        except FieldError as exc:
            problems.append(exc)
    if not model:
        problems.append(FieldError("api_model", "模型名字", "请先填写模型名字"))
    if not api_key:
        problems.append(FieldError("api_key", "API Token", "请先填写 API Token"))

    if problems:
        return None, [item.as_dict() for item in problems]

    return (
        build_probe_client(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
        ),
        [],
    )


def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _resolve_base_name(config: WeeklyVersionConfig) -> str:
    name = str(config.name or "").strip()
    if " - " in name:
        return name.split(" - ", 1)[0]
    return name


def build_weekly_group_key(config: WeeklyVersionConfig) -> str:
    base_name = _resolve_base_name(config)
    start_key = config.start_time.strftime("%Y%m%d%H%M") if config.start_time else "unknown"
    end_key = config.end_time.strftime("%Y%m%d%H%M") if config.end_time else "unknown"
    safe_base = base_name.replace("|", "_")
    return f"{config.project_id}|{start_key}|{end_key}|{safe_base}"


def _is_run_fresh(run: Optional[AiAnalysisRun]) -> bool:
    if not run:
        return False
    if not (run.response_text or run.response_payload):
        return False
    ts = run.finished_at or run.created_at
    if not ts:
        return False
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= _analysis_cache_cutoff()


def _parse_response_payload(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _stream_cached_run(run: AiAnalysisRun) -> Iterable[str]:
    yield _sse_event(
        "cached",
        {
            "run_id": run.id,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "scope": run.scope,
        },
    )
    if run.response_text:
        for line in run.response_text.splitlines():
            yield _sse_event("chunk", {"text": line})
    payload = _parse_response_payload(run.response_payload)
    if payload:
        yield _sse_event("result", payload)


def cleanup_expired_analysis_runs(retention_days: int = ANALYSIS_CACHE_DAYS):
    """清理过期的 AI 分析记录。

    返回清理条数（int）；**失败返回 None**。
    失败不返回 0：0 表示「本来就没东西可清」，两者在调用方的日志/界面上无法区分
    （同 services/excel_diff_cache_service.py::cleanup_old_cache 的说明）。
    """
    cutoff = _utcnow() - timedelta(days=retention_days)
    try:
        deleted = (
            AiAnalysisRun.query.filter(AiAnalysisRun.created_at.isnot(None))
            .filter(AiAnalysisRun.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        return int(deleted or 0)
    except Exception as exc:
        db.session.rollback()
        log_print(f"清理AI分析缓存失败: {exc}", "AI", force=True)
        return None


def _repo_priority(repo: Repository) -> int:
    resource_type = str(getattr(repo, "resource_type", "") or "").lower()
    repo_type = str(getattr(repo, "type", "") or "").lower()
    if resource_type == "code" or repo_type == "git":
        return 2
    return 1


def _is_critical_path(path: str) -> bool:
    if not path:
        return False
    normalized = path.replace("\\", "/").lower()
    for pattern in CRITICAL_PATH_PATTERNS:
        if re.search(pattern, normalized):
            return True
    return False


def set_project_api_key(project_id: int, api_key: str, updated_by: str = "") -> Tuple[bool, str]:
    if not api_key or not str(api_key).strip():
        return False, "API key is empty."
    encrypted = encrypt_credential(api_key)
    if not encrypted:
        return False, "API key encryption failed."
    record = AiProjectApiKey.query.filter_by(project_id=project_id).first()
    if record:
        record.encrypted_key = encrypted
        record.updated_by = (updated_by or "").strip()
        record.updated_at = _utcnow()
    else:
        record = AiProjectApiKey(
            project_id=project_id,
            encrypted_key=encrypted,
            updated_by=(updated_by or "").strip(),
        )
        db.session.add(record)
    db.session.commit()
    return True, "API key updated."


def get_project_api_key_status(project_id: int) -> Dict[str, Optional[str]]:
    record = AiProjectApiKey.query.filter_by(project_id=project_id).first()
    if not record:
        return {"configured": False, "updated_at": None, "format": None}
    return {
        "configured": True,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        # 让界面能区分「已配置」与「已配置但需要重新保存一次」（旧密文在当前平台解不开）。
        "format": "dpapi" if str(record.encrypted_key or "").startswith(DPAPI_PREFIX) else "fernet",
    }


def _get_project_api_key(project_id: int) -> Optional[str]:
    """取明文 Token。

    兼容读旧的 `dpapi::` 密文 —— 否则已经配过密钥的项目会突然全部失效。读到旧格式时：
    Windows 上解密成功就**顺手以新格式重写一遍**（懒迁移，以后再换平台就不用解 DPAPI）；
    非 Windows 上解不开，返回 None 让调用方给出可读指引，而不是抛一个
    `RuntimeError: DPAPI is only available on Windows.` 让用户完全摸不着头脑。
    """
    record = AiProjectApiKey.query.filter_by(project_id=project_id).first()
    if not record:
        return None

    stored = record.encrypted_key or ""
    if not stored.startswith(DPAPI_PREFIX):
        return decrypt_credential(stored)

    legacy = decrypt_dpapi(stored)
    if not legacy:
        log_print(
            "该项目保存的 API Key 是 Windows DPAPI 加密的，当前平台无法解密。"
            "请到本项目的 AI 配置里重新保存一次 Token。",
            "AI",
            force=True,
        )
        return None

    try:
        migrated = encrypt_credential(legacy)
        if migrated:
            record.encrypted_key = migrated
            db.session.commit()
    except SQLAlchemyError:
        # 懒迁移失败不该影响本次分析 —— 明文已经拿到了。
        db.session.rollback()
    return legacy


def _limit_items(items: List[dict], max_items: int) -> List[dict]:
    if max_items <= 0:
        return items
    return items[:max_items]


def _summarize_weekly_files(
    configs: List[WeeklyVersionConfig],
    last_analyzed_at: Optional[datetime],
) -> Tuple[dict, dict, Optional[str]]:
    config_ids = [cfg.id for cfg in configs]
    total_query = WeeklyVersionDiffCache.query.filter(
        WeeklyVersionDiffCache.config_id.in_(config_ids)
    )
    total_files = total_query.count()

    if last_analyzed_at:
        delta_query = total_query.filter(WeeklyVersionDiffCache.updated_at > last_analyzed_at)
    else:
        delta_query = total_query

    delta_entries = delta_query.all()
    delta_count = len(delta_entries)

    if last_analyzed_at and delta_count == 0:
        return {}, {}, "no_change"

    repo_lookup = {cfg.repository_id: cfg.repository for cfg in configs}
    repo_summaries: Dict[int, dict] = {}
    delta_files: List[dict] = []
    total_files_by_repo: Dict[int, int] = {}
    critical_hit = False

    for entry in delta_entries:
        repo = repo_lookup.get(entry.repository_id)
        repo_name = repo.name if repo else f"repo-{entry.repository_id}"
        repo_priority = _repo_priority(repo) if repo else 1
        total_files_by_repo[entry.repository_id] = total_files_by_repo.get(entry.repository_id, 0) + 1
        if _is_critical_path(entry.file_path):
            critical_hit = True

        delta_files.append(
            {
                "repository_id": entry.repository_id,
                "repository_name": repo_name,
                "priority": repo_priority,
                "file_path": entry.file_path,
                "file_type": entry.file_type,
                "latest_commit_id": entry.latest_commit_id,
                "commit_count": entry.commit_count,
                "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
            }
        )

    for cfg in configs:
        repo = cfg.repository
        total_count = WeeklyVersionDiffCache.query.filter_by(config_id=cfg.id).count()
        repo_summaries[cfg.repository_id] = {
            "repository_id": cfg.repository_id,
            "repository_name": repo.name,
            "resource_type": getattr(repo, "resource_type", None),
            "priority": _repo_priority(repo),
            "total_files": total_count,
            "delta_files": total_files_by_repo.get(cfg.repository_id, 0),
        }

    delta_files.sort(
        key=lambda item: (item.get("priority", 1), item.get("commit_count", 0), item.get("file_path", "")),
        reverse=True,
    )

    summary = {
        "total_files": total_files,
        "delta_files": delta_count,
        "critical_paths": critical_hit,
    }
    return summary, {
        "repos": list(repo_summaries.values()),
        "delta_files": delta_files,
    }, None


def has_weekly_changes(config_ids: List[int], last_analyzed_at: Optional[datetime]) -> bool:
    if not config_ids:
        return False
    query = WeeklyVersionDiffCache.query.filter(WeeklyVersionDiffCache.config_id.in_(config_ids))
    if last_analyzed_at:
        query = query.filter(WeeklyVersionDiffCache.updated_at > last_analyzed_at)
    return query.first() is not None


def _decide_scope(summary: dict, last_analyzed_at: Optional[datetime]) -> Tuple[str, str]:
    if not last_analyzed_at:
        return "full", "first_run"
    delta_count = int(summary.get("delta_files") or 0)
    total_count = int(summary.get("total_files") or 0)
    if total_count <= 0:
        return "full", "empty_total"
    ratio = delta_count / max(total_count, 1)
    critical_hit = bool(summary.get("critical_paths"))
    if delta_count >= FULL_ANALYSIS_FILE_THRESHOLD:
        return "full", "delta_count_high"
    if ratio >= FULL_ANALYSIS_RATIO_THRESHOLD:
        return "full", "delta_ratio_high"
    if critical_hit:
        return "full", "critical_path_detected"
    return "incremental", "delta_small"


def build_commit_payload(commit_id: int) -> dict:
    commit = Commit.query.get_or_404(commit_id)
    repo = db.session.get(Repository, commit.repository_id)
    project_id = repo.project_id if repo else None
    config = get_project_analysis_config(project_id) if project_id else None
    payload = {
        "mode": "commit",
        "scope": "full",
        "execution": {
            "version": EXECUTION_VERSION,
            "streaming_preferred": True,
        },
        "prompt": config["prompt_template"] if config else DEFAULT_PROMPT_TEMPLATE,
        "commit": {
            "id": commit.id,
            "commit_id": commit.commit_id,
            "path": commit.path,
            "operation": commit.operation,
            "author": commit.author,
            "message": commit.message,
            "commit_time": commit.commit_time.isoformat() if commit.commit_time else None,
        },
        "repository": {
            "id": repo.id if repo else None,
            "name": repo.name if repo else None,
            "resource_type": getattr(repo, "resource_type", None) if repo else None,
        },
    }
    return payload


def build_weekly_payload(
    config_id: int,
    *,
    force_full: bool = False,
) -> Tuple[Optional[dict], Optional[AiWeeklyAnalysisState], Optional[str]]:
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    configs = WeeklyVersionConfig.query.filter(
        WeeklyVersionConfig.project_id == config.project_id,
        WeeklyVersionConfig.start_time == config.start_time,
        WeeklyVersionConfig.end_time == config.end_time,
    ).order_by(WeeklyVersionConfig.repository_id.asc()).all()
    if not configs:
        return None, None, "no_configs"

    group_key = build_weekly_group_key(config)
    base_name = _resolve_base_name(config)
    project_config = get_project_analysis_config(config.project_id)
    state = AiWeeklyAnalysisState.query.filter_by(group_key=group_key).first()
    last_analyzed_at = None if force_full else (state.last_analyzed_at if state else None)

    summary, details, skip_reason = _summarize_weekly_files(configs, last_analyzed_at)
    if skip_reason and not force_full:
        return None, state, skip_reason

    scope, policy = _decide_scope(summary, last_analyzed_at)
    max_files = int(project_config.get("max_files_per_run") or MAX_FILES_DEFAULT)

    repo_details = details.get("repos", [])
    repo_details.sort(key=lambda item: (item.get("priority", 1), item.get("repository_name", "")), reverse=True)

    delta_files = details.get("delta_files", [])
    limited_delta_files = _limit_items(delta_files, max_files)

    truncated = len(delta_files) > len(limited_delta_files)
    payload = {
        "mode": "weekly",
        "scope": scope,
        "execution": {
            "version": EXECUTION_VERSION,
            "streaming_preferred": True,
        },
        "prompt": project_config.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE,
        "policy": {
            "reason": policy,
            "max_files": max_files,
            "sample_strategy": "priority_then_commit_count",
            "allow_cross_file": True,
        },
        "group": {
            "key": group_key,
            "base_name": base_name,
            "project_id": config.project_id,
            "start_time": config.start_time.isoformat() if config.start_time else None,
            "end_time": config.end_time.isoformat() if config.end_time else None,
        },
        "summary": summary,
        "repositories": repo_details,
        "delta_files": limited_delta_files,
        "delta_truncated": truncated,
    }
    payload["policy"]["truncated"] = truncated
    if truncated:
        payload["policy"]["truncation_reason"] = "token_budget"
    return payload, state, None


def _determine_risk_level(summary: dict) -> str:
    total_files = int(summary.get("total_files") or 0)
    delta_files = int(summary.get("delta_files") or 0)
    critical = bool(summary.get("critical_paths"))
    if critical or total_files >= 120 or delta_files >= 60:
        return "high"
    if total_files >= 80 or delta_files >= 40:
        return "mid_high"
    if total_files >= 40 or delta_files >= 20:
        return "medium"
    if total_files >= 15 or delta_files >= 8:
        return "mid_low"
    return "low"


def _build_stub_result(payload: dict) -> Tuple[dict, str]:
    summary = payload.get("summary") or {}
    risk_level = _determine_risk_level(summary)
    mode = payload.get("mode")
    scope = payload.get("scope")
    total_files = summary.get("total_files", 0)
    delta_files = summary.get("delta_files", 0)

    risk_reasons = [
        f"Scope: {scope}",
        f"Total files: {total_files}",
        f"Delta files: {delta_files}",
    ]
    if summary.get("critical_paths"):
        risk_reasons.append("Critical paths detected.")

    test_suggestions = [
        "Run smoke tests on core user flows.",
        "Verify configuration loading and permissions.",
        "Validate data migration or schema changes if applicable.",
    ]
    smoke_list = [
        "Login / auth flow",
        "Primary business flow",
        "Error handling / rollback",
    ]
    unknowns = [
        "Exact runtime impact depends on downstream integrations.",
    ]

    result = {
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "impact_scope": [mode, scope],
        "test_suggestions": test_suggestions,
        "smoke_list": smoke_list,
        "unknowns": unknowns,
    }

    text_lines = [
        f"AI Analysis ({mode})",
        f"Risk Level: {risk_level}",
        "Reasons:",
        *[f"- {item}" for item in risk_reasons],
        "Test Suggestions:",
        *[f"- {item}" for item in test_suggestions],
    ]
    return result, "\n".join(text_lines)


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {_json_dumps(payload)}\n\n"


def stream_commit_analysis(commit_id: int, user_label: str = "") -> Iterable[str]:
    commit = Commit.query.get_or_404(commit_id)
    repo = db.session.get(Repository, commit.repository_id)
    project_id = repo.project_id if repo else None
    if not project_id:
        yield _sse_event("error", {"message": "Project not found."})
        return

    cached_run = (
        AiAnalysisRun.query.filter_by(target_type="commit", target_id=commit_id)
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )
    if _is_run_fresh(cached_run):
        yield from _stream_cached_run(cached_run)
        return
    if not _get_project_api_key(project_id):
        yield _sse_event("error", {"message": "Project API key not configured."})
        return

    payload = build_commit_payload(commit_id)
    trace_id = f"commit-{commit_id}-{int(_utcnow().timestamp())}"

    run = AiAnalysisRun(
        project_id=project_id,
        target_type="commit",
        target_id=commit_id,
        target_key=None,
        status="running",
        response_mode="streaming",
        scope="full",
        trigger_source="manual",
        trace_id=trace_id,
        request_payload=_json_dumps(payload),
        started_at=_utcnow(),
    )
    db.session.add(run)
    db.session.commit()

    result, text = _build_stub_result(payload)
    for line in text.splitlines():
        yield _sse_event("chunk", {"text": line})

    run.status = "succeeded"
    run.finished_at = _utcnow()
    run.response_payload = _json_dumps(result)
    run.response_text = text
    db.session.commit()
    yield _sse_event("result", result)


def stream_weekly_analysis(config_id: int, trigger_source: str = "manual") -> Iterable[str]:
    payload, state, skip_reason = build_weekly_payload(config_id)
    if skip_reason == "no_change":
        cached = get_latest_weekly_result(config_id)
        if cached and cached.get("run_id"):
            run = db.session.get(AiAnalysisRun, cached["run_id"])
            if run and _is_run_fresh(run):
                yield from _stream_cached_run(run)
                return
        payload, state, skip_reason = build_weekly_payload(config_id, force_full=True)
    if payload is None:
        yield _sse_event("error", {"message": "Weekly analysis payload not ready."})
        return

    project_id = payload["group"]["project_id"]
    if not _get_project_api_key(project_id):
        yield _sse_event("error", {"message": "Project API key not configured."})
        return

    trace_id = f"weekly-{config_id}-{int(_utcnow().timestamp())}"
    group_key = payload["group"]["key"]
    scope = payload.get("scope", "full")
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_id=config_id,
        target_key=group_key,
        status="running",
        response_mode="streaming",
        scope=scope,
        trigger_source=trigger_source,
        trace_id=trace_id,
        request_payload=_json_dumps(payload),
        delta_summary=_json_dumps({
            "delta_files": payload.get("summary", {}).get("delta_files", 0),
            "total_files": payload.get("summary", {}).get("total_files", 0),
            "scope": scope,
        }),
        started_at=_utcnow(),
    )
    db.session.add(run)
    db.session.commit()

    result, text = _build_stub_result(payload)
    for line in text.splitlines():
        yield _sse_event("chunk", {"text": line})

    run.status = "succeeded"
    run.finished_at = _utcnow()
    run.response_payload = _json_dumps(result)
    run.response_text = text
    db.session.commit()

    _update_weekly_state(payload, run, state)
    yield _sse_event("result", result)


def run_weekly_analysis_background(config_id: int, task_id: Optional[int] = None) -> dict:
    payload, state, skip_reason = build_weekly_payload(config_id)
    if skip_reason == "no_change":
        cached = get_latest_weekly_result(config_id)
        if cached and cached.get("run_id"):
            run = db.session.get(AiAnalysisRun, cached["run_id"])
            if run and _is_run_fresh(run):
                return {"status": "skipped", "reason": "no_change"}
        payload, state, skip_reason = build_weekly_payload(config_id, force_full=True)
    if payload is None:
        return {"status": "skipped", "reason": "payload_empty"}

    project_id = payload["group"]["project_id"]
    if not _get_project_api_key(project_id):
        return {"status": "skipped", "reason": "missing_api_key"}

    group_key = payload["group"]["key"]
    scope = payload.get("scope", "full")
    run = AiAnalysisRun(
        project_id=project_id,
        target_type="weekly",
        target_id=config_id,
        target_key=group_key,
        status="running",
        response_mode="blocking",
        scope=scope,
        trigger_source="scheduled",
        trace_id=f"weekly-{config_id}-{int(_utcnow().timestamp())}",
        request_payload=_json_dumps(payload),
        delta_summary=_json_dumps({
            "delta_files": payload.get("summary", {}).get("delta_files", 0),
            "total_files": payload.get("summary", {}).get("total_files", 0),
            "scope": scope,
        }),
        started_at=_utcnow(),
    )
    db.session.add(run)
    db.session.commit()

    result, text = _build_stub_result(payload)
    run.status = "succeeded"
    run.finished_at = _utcnow()
    run.response_payload = _json_dumps(result)
    run.response_text = text
    db.session.commit()

    _update_weekly_state(payload, run, state)
    return {"status": "succeeded", "run_id": run.id}


def _update_weekly_state(payload: dict, run: AiAnalysisRun, state: Optional[AiWeeklyAnalysisState]) -> None:
    group = payload.get("group") or {}
    summary = payload.get("summary") or {}
    if not group:
        return
    if not state:
        state = AiWeeklyAnalysisState(
            project_id=group.get("project_id"),
            group_key=group.get("key"),
            base_name=group.get("base_name"),
            start_time=_parse_iso_datetime(group.get("start_time")),
            end_time=_parse_iso_datetime(group.get("end_time")),
        )
        db.session.add(state)

    state.last_analyzed_at = _utcnow()
    state.last_analysis_run_id = run.id
    state.last_scope = run.scope
    state.last_summary = _json_dumps(summary)
    state.last_triggered_at = run.started_at or _utcnow()
    state.updated_at = _utcnow()
    db.session.commit()


def _parse_iso_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def get_latest_weekly_result(config_id: int) -> Optional[dict]:
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    group_key = build_weekly_group_key(config)
    run = (
        AiAnalysisRun.query.filter_by(target_type="weekly", target_key=group_key)
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )
    if not run or not _is_run_fresh(run):
        return None
    payload = _parse_response_payload(run.response_payload)
    return {
        "run_id": run.id,
        "status": run.status,
        "scope": run.scope,
        "trigger_source": run.trigger_source,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "response_text": run.response_text,
        "result": payload,
    }


def get_latest_commit_result(commit_id: int) -> Optional[dict]:
    run = (
        AiAnalysisRun.query.filter_by(target_type="commit", target_id=commit_id)
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )
    if not run or not _is_run_fresh(run):
        return None
    payload = _parse_response_payload(run.response_payload)
    return {
        "run_id": run.id,
        "status": run.status,
        "scope": run.scope,
        "trigger_source": run.trigger_source,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "response_text": run.response_text,
        "result": payload,
    }


def select_primary_weekly_config(configs: List[WeeklyVersionConfig]) -> WeeklyVersionConfig:
    def _score(cfg: WeeklyVersionConfig) -> Tuple[int, int]:
        return (_repo_priority(cfg.repository), -cfg.id)

    return sorted(configs, key=_score, reverse=True)[0]
