#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis service（真实执行器）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError

from models import (
    Commit,
    Project,
    Repository,
    WeeklyVersionConfig,
    WeeklyVersionDiffCache,
    db,
)
from models.ai_analysis import (
    AiAnalysisAnomaly,
    AiAnalysisRun,
    AiAnalysisTrace,
    AiProjectAnalysisConfig,
    AiProjectApiKey,
    AiWeeklyAnalysisState,
)
from models.ai_analysis.project_config import (
    DEFAULT_MAX_FILES_PER_RUN,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_RANGE,
)
from services.ai.baseline import (
    DISPOSITION_PENDING,
    BaselineFinding,
    build_baseline_digest,
    classify,
    suppressed_fingerprints,
)
from services.ai.change_set import ChangeSet, from_commit_payload, from_weekly_payload
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
from services.ai.engine import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineLimits,
    EngineOutcome,
    run_analysis,
)
from services.ai.engine import (
    failed as engine_failed,
)
from services.ai.platform_provider import PlatformContextProvider
from services.ai.prompt import prompt_version
from services.ai.rules import RuleThresholds, anomaly_fingerprint, rules_version
from services.ai.skill_loader import load_skills, skill_revision
from utils.dpapi_utils import DPAPI_PREFIX, decrypt_dpapi
from utils.logger import log_print
from utils.security_utils import decrypt_credential, encrypt_credential
from utils.timezone_utils import format_beijing_time

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


def _coerce_timeout(value) -> int:
    """把配置里的单次请求超时收成合法整数秒。

    配置是从表单来的，可能是字符串、可能是脏值、也可能是 None。这里按
    `REQUEST_TIMEOUT_RANGE`（同一份给界面渲染范围的事实源）夹紧并回落默认值 ——
    超时值直接决定「分析能不能跑完」，不能因为一个脏值就退回到 30 秒那种必然超时的值。
    """
    low, high = REQUEST_TIMEOUT_RANGE
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_REQUEST_TIMEOUT_SECONDS
    return max(low, min(high, seconds))


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

    非 dict 的 payload（例如请求体根节点是 `[1]`）由 `validate_payload` 转成
    字段级错误 —— 在这里 `dict()` 会直接抛 TypeError，那是 500 而不是 400。
    """
    row = _get_project_config_row(project_id)
    if row is None:
        row = AiProjectAnalysisConfig(project_id=project_id)
        db.session.add(row)

    try:
        normalized = validate_payload(payload)
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
    project_id: int, override: Optional[dict] = None, *, timeout_seconds: Optional[int] = None
) -> Tuple[Optional[object], list]:
    """按「请求体优先、已保存配置回退」构造客户端。

    `timeout_seconds` 要按用途区分：探测（测试连接 / 拉模型列表）用默认的
    `PROBE_TIMEOUT_SECONDS`，**正式分析必须传配置里的 `request_timeout_seconds`**。
    这里曾把两者混为一谈，于是正式分析也拿到 30 秒 —— 而它是**非流式**请求
    （`LLMClient._request(..., stream=False)`），requests 的 timeout 对非流式响应
    等价于「整个响应体要在 30 秒内到齐」。网关得先吃下几百 KB 的 prompt 再生成完整
    JSON 报告，30 秒根本不够；症状就是「测试连接 1.4 秒成功、正式分析必然 Read
    timed out」——用户会以为配置有问题，其实是超时值用错了。

    返回 `(client, errors)`；errors 是字段级列表，**格式与保存配置时的 400 一致** ——
    前端因此可以复用同一套「哪一栏错了」的渲染，不必为测试连接再写一份。

    「未保存也能测」是刻意的：用户填完地址/Token/模型名之后，第一件想做的事就是
    确认这组配置能不能用。要求他先保存一个可能错的配置再测，是很别扭的顺序。
    输入框留空表示「沿用已保存的值」，而不是「清空」。

    `override` 不是 dict（例如请求体根节点是 `[1]`）时返回**字段级错误**而不是抛
    TypeError：这一层也会被后台任务直接调用，且路由靠 `client is None` 回 400。
    """
    if override is not None and not isinstance(override, Mapping):
        return None, [FieldError("__body__", "请求体", "必须是 JSON 对象").as_dict()]

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
            timeout_seconds=(
                timeout_seconds if timeout_seconds else PROBE_TIMEOUT_SECONDS
            ),
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


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _current_provenance(project_id: int) -> dict:
    """这批结论是由「哪套提示词 / skill / 规则 / 模型」产生的。

    **必须落库，也必须参与缓存判等**，理由有两条：

    1. 不记录就没法说明一份结论是怎么来的。改了 skill 之后重看同一份结论，无法判断
       它是新规则跑出来的还是旧的。
    2. 缓存目前只按「目标 + 时间」命中。于是改了 skill、提示词、规则或换了模型之后，
       90 天内重看老提交拿到的仍然是**旧结论** —— 从用户角度看就是「我的改动没生效」。
       三个版本号都是源码内容哈希，改了文件就变，缓存自然失效。
    """
    project = db.session.get(Project, project_id)
    project_code = getattr(project, "code", None) if project else None
    row = _get_project_config_row(project_id)
    model = ""
    if row is not None:
        model = str(row.resolved().get("api_model") or "")
    return {
        "prompt_version": prompt_version(),
        "skill_version": skill_revision(_REPO_ROOT, project_code=project_code),
        "rules_version": rules_version(),
        "model": model.strip(),
    }


def _provenance_matches(run: AiAnalysisRun, expected: Optional[dict]) -> bool:
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


def _is_run_fresh(run: Optional[AiAnalysisRun], *, expected: Optional[dict] = None) -> bool:
    """这份历史结论现在还能不能直接复用。

    除了时间窗，还要求产生它的那套 prompt/skill/rules/model 与现在一致（见
    `_current_provenance`）。不传 `expected` 时按 run 自己的项目现算。

    **失败的 run 一律不可复用。** 这一条以前漏了，后果比「显示错了」更严重：
    失败的 run 也写了 `response_text`（内容是错误文本）与 `finished_at`，溯源也在
    建 run 时就写全了，于是 `_is_run_fresh` 判它可用 → 失败记录被当成「已有结果」，
    还会被 `stream_*` 当缓存**直接回放**：用户再点一次分析，拿到的是上次的失败，
    而不是重新跑。读侧必须自己判 status，不能指望写入侧不写。
    （`_previous_run()` 一直只取 `status == "succeeded"`，说明这是本来的设计意图。）
    """
    if not run:
        return False
    if run.status != "succeeded":
        return False
    if not (run.response_text or run.response_payload):
        return False
    ts = run.finished_at or run.created_at
    if not ts:
        return False
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts < _analysis_cache_cutoff():
        return False
    if expected is None and run.project_id:
        expected = _current_provenance(run.project_id)
    return _provenance_matches(run, expected)


def _parse_response_payload(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# 界面上的「最近分析」时间一律走北京时间（UTC+8）。
#
# 库里存的是 naive-UTC 墙钟（SQLite 会丢掉 tzinfo，口径见 utils/timezone_utils），
# 而 created_at 直接 isoformat() 出来是「带时间、不带偏移、还带微秒」的串，界面上
# 就长成 2026-09-17T12:53:28.872891 —— 既不是北京时间，也不是人看的格式。
#
# 为什么在**服务端**格式化而不是交给前端：ES 规范里「带时间但不带偏移」的 ISO 串
# 按**浏览器本地时区**解析，非 UTC+8 的机器上再转换一次就又多错 8 小时。在服务端
# 算好、前端只负责显示，这类错就没有发生的余地。
def _created_at_display(run: AiAnalysisRun) -> Optional[str]:
    created_at = getattr(run, "created_at", None)
    if created_at is None:
        return None
    # format_beijing_time 把 naive 入参当 UTC 解释，与库里的 naive-UTC 口径一致
    return format_beijing_time(created_at, "%Y-%m-%d %H:%M:%S")


def _stream_cached_run(run: AiAnalysisRun) -> Iterable[str]:
    yield _sse_event(
        "cached",
        {
            "run_id": run.id,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "created_at_display": _created_at_display(run),
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
    """取样时仓库的先手顺序：代码仓库优先于配表仓库。

    **判据只能是 `resource_type`，不能加上 `type == "git"`。** 线上两个仓库的
    `type` 都是 `git`，那一句会让「代码仓库」和「配表仓库」一起返回 2 —— 优先级
    形同虚设，`policy.sample_strategy` 写着 `priority_then_commit_count` 而实际
    只按 `commit_count` 排。

    注意这个值**只用于取样的发牌顺序**，不再被 `select_primary_weekly_config`
    复用（那里关心的是分组身份，不是取样偏好）。
    """
    resource_type = str(getattr(repo, "resource_type", "") or "").lower()
    return 2 if resource_type == "code" else 1


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


def _sample_with_repo_fairness(
    items: List[dict], max_items: int, *, repo_key: str = "repository_id"
) -> List[dict]:
    """按「各仓库轮流发牌」取样，保证没有仓库会被整个挤出清单。

    **不能只按全局排序截断。** 线上一次周版本有 767 个文件：748 个 lua（代码仓库）
    加 19 个配表，取前 200 时配表**一个都进不去** —— 而配表改的正是数值、ID、奖励
    这些评审最关心的东西。（当时的实际排序按 `commit_count` 降序，19 张配表因为
    改动次数少而排在后面。）

    做法是轮转发牌：按各仓库**优先级最高的那条**决定发牌顺序，然后每轮给每个仓库
    各发一条，直到取满或全部发完。文件少的仓库很快发完，剩下的名额自然全归文件多的
    仓库 —— 上例里配表 19 条全进，代码仓库拿走其余 181 条。

    `items` 必须**已按全局优先级降序排好**：桶内顺序、以及返回值的展示顺序都依赖它。
    """
    if max_items <= 0 or len(items) <= max_items:
        return list(items)

    buckets: Dict[object, List[int]] = {}
    for position, item in enumerate(items):
        buckets.setdefault(item.get(repo_key), []).append(position)

    # 每个仓库的第一条就是它优先级最高的那条（items 已全局排序），
    # 用它代表这个仓库的先手顺序。sorted 是稳定的，同优先级时保持首次出现的顺序。
    deal_order = sorted(
        buckets.values(),
        key=lambda positions: -int(items[positions[0]].get("priority") or 0),
    )

    chosen: List[int] = []
    round_index = 0
    while len(chosen) < max_items:
        dealt = False
        for positions in deal_order:
            if round_index >= len(positions):
                continue
            chosen.append(positions[round_index])
            dealt = True
            if len(chosen) >= max_items:
                break
        if not dealt:      # 所有仓库都发完了
            break
        round_index += 1

    chosen.sort()          # 回到全局优先级顺序，展示口径与改动前一致
    return [items[position] for position in chosen]


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
    limited_delta_files = _sample_with_repo_fairness(delta_files, max_files)

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


def _load_project_skills(project_id: int):
    """加载平台 skill 与项目知识包。**加载失败不阻断分析**：skill 是提示词的一部分，
    提示词装不上不该让用户连一次分析都跑不了。"""
    project = db.session.get(Project, project_id)
    code = getattr(project, "code", None) if project else None
    try:
        return load_skills(_REPO_ROOT, project_code=code)
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ AI 分析：skill 加载失败（project={project_id}）: {exc}")
        return None


def _engine_limits(project_config: dict) -> EngineLimits:
    """项目配置 → 引擎额度。

    用 `or` 而不是 `dict.get(key, 默认)`：配置行是懒创建的，没填过的列读出来是 `None`，
    而 `None` 会让 `range(1, None + 1)` 在半夜的自动轮询里炸掉。
    """
    defaults = EngineLimits()
    return EngineLimits(
        max_rounds=int(project_config.get("max_analysis_rounds") or defaults.max_rounds),
        max_tool_requests=int(project_config.get("max_tool_requests") or defaults.max_tool_requests),
        prompt_char_budget=int(
            project_config.get("prompt_char_budget") or defaults.prompt_char_budget
        ),
    )


def _previous_run(target_type: str, target_key: Optional[str]) -> Optional[AiAnalysisRun]:
    if not target_key:
        return None
    return (
        AiAnalysisRun.query.filter_by(target_type=target_type, target_key=target_key)
        .filter(AiAnalysisRun.status == "succeeded")
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )


def _baseline_findings(target_type: str, target_key: Optional[str]) -> List[BaselineFinding]:
    """上一次成功运行报出的那批结论。

    **取「上一次运行的那批」而不是把历次运行并起来**：每次运行产出的本来就是「这个版本
    当前仍成立的问题全集」（skill 里定死了这个语义），所以上一次那批就是当前基线。
    并起来反而会把已经修好的旧条目重新翻出来。
    """
    previous = _previous_run(target_type, target_key)
    if previous is None:
        return []
    return [
        BaselineFinding(
            fingerprint=row.fingerprint or "",
            title=row.title or "",
            severity=row.severity or "high",
            category=row.category or "",
            file_path=row.file_path or "",
            commit_ref=row.commit_ref or "",
            disposition=row.disposition or DISPOSITION_PENDING,
        )
        for row in AiAnalysisAnomaly.query.filter_by(run_id=previous.id).all()
        if row.fingerprint
    ]


def _baseline_digest(target_type: str, target_key: Optional[str], change: ChangeSet) -> str:
    """给模型看的「已经报过的问题」。

    `changed_paths` 传「上次报过、这次又变了」的文件：那类结论的证据已经过期，要重新
    确认 —— 包括人工标过「已忽略」的。这是「忽略」不会变成「永远看不见」的保证。

    **先 `classify` 再渲染，两件事必须分开做**：`build_baseline_digest` 刻意不收
    `changed_paths`，因为它再判一遍状态会把刚判成「需要重新确认」的结论判回「已忽略」
    并从摘要里抹掉 —— 而且是静默的（报告里只是少一条）。
    """
    findings = _baseline_findings(target_type, target_key)
    if not findings:
        return ""
    return build_baseline_digest(classify(findings, changed_paths=change.paths))


def _suppressed(target_type: str, target_key: Optional[str], change: ChangeSet) -> frozenset:
    """人工已忽略、且相关文件没有再变的指纹。这些不再进清单。"""
    findings = _baseline_findings(target_type, target_key)
    if not findings:
        return frozenset()
    return suppressed_fingerprints(classify(findings, changed_paths=change.paths))


def _risk_level_from_outcome(outcome: EngineOutcome, summary: dict) -> Tuple[str, List[str]]:
    """风险等级与依据。

    **有结论时按结论定级；没有结论时按变更规模定级，并明说那不是模型结论。**
    反过来做（没结论就报「低」）正是这套机制最该避免的事：用户分不出「模型看完说没问题」
    和「模型压根没答上来」，而这两件事的处理方式完全相反。
    """
    if outcome.anomalies:
        severities = {item.severity for item in outcome.anomalies}
        level = "high" if "critical" in severities else "mid_high"
        reasons = [f"模型报出 {len(outcome.anomalies)} 条达门槛的问题"]
        if "critical" in severities:
            reasons.append("其中含 critical")
        if outcome.degradation:
            reasons.append(outcome.degradation_label or outcome.degradation)
        return level, reasons

    level = _determine_risk_level(summary)
    reasons = [
        f"变更规模：{summary.get('total_files', 0)} 个文件"
        f"（本次变化 {summary.get('delta_files', 0)} 个）"
    ]
    if summary.get("critical_paths"):
        reasons.append("含关键路径")
    if outcome.succeeded:
        reasons.append("模型未报出达门槛的问题")
    else:
        reasons.append(
            f"⚠️ 本次未取得完整结论（{outcome.degradation_label or outcome.degradation or '原因未知'}），"
            "该等级仅按变更规模估算，**不是**模型评估结果"
        )
    return level, reasons


def _result_payload(
    outcome: EngineOutcome,
    payload: dict,
    *,
    suppressed: frozenset = frozenset(),
) -> dict:
    """给前端与后续读取用的结果。

    保留既有的 `risk_level`（界面在读它），其余键是真实产出。被人工忽略的结论不进
    `anomalies` —— 尊重分诊结果，而不是每轮再问一次。
    """
    summary = payload.get("summary") or {}
    risk_level, risk_reasons = _risk_level_from_outcome(outcome, summary)
    kept = [item for item in outcome.anomalies if anomaly_fingerprint(item) not in suppressed]

    return {
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "report_markdown": outcome.report_markdown,
        "status": outcome.status,
        "degradation": outcome.degradation,
        "degradation_label": outcome.degradation_label,
        "error_message": outcome.error_message,
        "anomalies": [
            {
                "fingerprint": anomaly_fingerprint(item),
                "title": item.title,
                "category": item.category,
                "severity": item.severity,
                "confidence": item.confidence,
                "evidence": list(item.evidence),
                "commit_ref": item.commit or "",
                "file_path": item.file_path or "",
                "impact": item.impact or "",
                "suggestion": item.suggestion or "",
            }
            for item in kept
        ],
        "suppressed_count": len(outcome.anomalies) - len(kept),
        "rounds_used": outcome.rounds_used,
        "requests_used": outcome.requests_used,
        "dropped": [
            {"kind": item.kind, "reason": item.reason, "detail": item.detail}
            for item in outcome.dropped
        ],
    }


def _persist_outcome(run: AiAnalysisRun, outcome: EngineOutcome, result: dict) -> None:
    """把引擎结果落库：run 上写状态与账目，逐轮写 trace，逐条写异常。

    `error_message` 这一列此前**从来没有被写入过**，于是失败的分析在界面上永远是
    「分析中」，用户无从判断。这是这次要修的一部分。
    """
    run.status = "failed" if outcome.status == STATUS_FAILED else "succeeded"
    run.finished_at = _utcnow()
    if run.status == "failed":
        # 失败**不写结论字段**。以前这里照样写 response_payload / response_text，
        # 于是库里那条失败记录长得和成功记录一样：有「结论」、有风险等级、有范围，
        # 前端只判「有没有结果」就把徽章显示成「已有结果」并附上「风险等级 high」。
        # 而那个 high 根本不是模型给的 —— 是 _determine_risk_level 按变更规模
        # （total_files >= 120）估出来的兜底值，模型压根没答上来。
        # 失败应当只留错误，让「有没有成功结论」这件事在数据层就无歧义。
        run.response_payload = None
        run.response_text = ""
    else:
        run.response_payload = _json_dumps(result)
        run.response_text = outcome.report_markdown or outcome.error_message or ""
    run.rounds_used = outcome.rounds_used
    run.tokens_input = outcome.prompt_tokens
    run.tokens_output = outcome.completion_tokens
    run.error_message = outcome.error_message or None

    for record in outcome.rounds:
        db.session.add(
            AiAnalysisTrace(
                run_id=run.id,
                round_index=record.index,
                outcome=record.status,
                parsed_ok=record.status != "unparsable",
                requests_json=_json_dumps({"count": record.request_count}),
                executed_json=_json_dumps({"items": record.item_count}),
                dropped_json=_json_dumps(
                    {"refused_by_budget": record.refused_by_budget, "truncated": record.truncated}
                ),
                error=record.note or None,
            )
        )

    for item in result.get("anomalies") or []:
        db.session.add(
            AiAnalysisAnomaly(
                run_id=run.id,
                project_id=run.project_id,
                fingerprint=item["fingerprint"],
                title=item["title"] or "（无标题）",
                category=item["category"],
                severity=item["severity"],
                confidence=item["confidence"],
                evidence=_json_dumps(item["evidence"]),
                commit_ref=item["commit_ref"],
                file_path=item["file_path"],
                impact=item["impact"],
                suggestion=item["suggestion"],
            )
        )

    db.session.commit()


def _failed_result(summary: dict, message: str) -> dict:
    """没发起分析时的结果。**等级按规模估算并写明原因** —— 不伪装成模型结论。"""
    return {
        "risk_level": _determine_risk_level(summary),
        "risk_reasons": [message, "该等级仅按变更规模估算，**不是**模型评估结果"],
        "report_markdown": "",
        "status": "failed",
        "degradation": "not_started",
        "degradation_label": message,
        "error_message": message,
        "anomalies": [],
        "suppressed_count": 0,
        "rounds_used": 0,
        "requests_used": 0,
        "dropped": [],
    }


def _create_run(
    *,
    project_id: int,
    target_type: str,
    target_id: int,
    target_key: Optional[str],
    response_mode: str,
    scope: str,
    trigger_source: str,
    payload: dict,
) -> AiAnalysisRun:
    """建一条 running 的 run 记录。

    **先落库再跑**：分析要花几十秒到几分钟，这期间界面上必须能看到「正在分析」。等跑完
    才写库的话，用户在这段时间里看到的是一片空白，看起来像按钮没生效。

    溯源四个值（prompt / skill / rules / model）在这里一次写全：它们同时是缓存键，
    缺一个就会出现「改了 skill 却还在复用旧结论」——那个坑已经踩过一次。
    """
    summary = payload.get("summary") or {}
    run = AiAnalysisRun(
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target_key=target_key,
        status="running",
        response_mode=response_mode,
        scope=scope,
        trigger_source=trigger_source,
        trace_id=f"{target_type}-{target_id}-{int(_utcnow().timestamp())}",
        **_current_provenance(project_id),
        request_payload=_json_dumps(payload),
        delta_summary=_json_dumps(
            {
                "delta_files": summary.get("delta_files", 0),
                "total_files": summary.get("total_files", 0),
                "scope": scope,
            }
        ),
        started_at=_utcnow(),
    )
    db.session.add(run)
    db.session.commit()
    return run


def _execute_analysis(
    run: AiAnalysisRun,
    *,
    project_id: int,
    payload: dict,
    project_config: dict,
    target_type: str,
    target_key: Optional[str],
) -> dict:
    """跑一次真实分析并把结果落库。**不抛异常** —— 失败要变成带原因的结论。

    调用方在后台线程或 SSE 生成器里跑，抛出去只会变成一个没人看的堆栈，而用户那边
    是「分析中」永远转下去。
    """
    summary = payload.get("summary") or {}

    # 正式分析用配置里的单次请求超时（默认 300 秒，界面可改），**不是**探测用的 30 秒。
    # 这两者混用会让分析必然超时，见 build_endpoint_client 的说明。
    configured_timeout = get_project_analysis_config(project_id).get("request_timeout_seconds")
    client, errors = build_endpoint_client(
        project_id, {}, timeout_seconds=_coerce_timeout(configured_timeout)
    )
    if client is None:
        message = "接口配置不完整：" + "；".join(item["message"] for item in errors)
        result = _failed_result(summary, message)
        _persist_outcome(run, engine_failed(message), result)
        return result

    loaded = _load_project_skills(project_id)
    if loaded is None:
        message = "分析协议（skill）加载失败，未发起分析。"
        result = _failed_result(summary, message)
        _persist_outcome(run, engine_failed(message), result)
        return result

    readable = sorted(getattr(loaded, "readable", {}) or {})
    change = (
        from_commit_payload(payload, readable_references=readable)
        if payload.get("mode") == "commit"
        else from_weekly_payload(payload, readable_references=readable)
    )

    outcome = run_analysis(
        client=client,
        provider=PlatformContextProvider(loaded=loaded),
        loaded=loaded,
        scope=change.scope,
        change_summary=change.summary,
        limits=_engine_limits(project_config),
        thresholds=RuleThresholds.from_config(project_config),
        project_knowledge=project_config.get("project_knowledge") or "",
        project_instructions=project_config.get("prompt_template") or "",
        baseline_digest=_baseline_digest(target_type, target_key, change),
    )

    result = _result_payload(
        outcome, payload, suppressed=_suppressed(target_type, target_key, change)
    )
    _persist_outcome(run, outcome, result)
    return result


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
    run = _create_run(
        project_id=project_id,
        target_type="commit",
        target_id=commit_id,
        target_key=None,
        response_mode="streaming",
        scope="full",
        trigger_source="manual",
        payload=payload,
    )

    result = _execute_analysis(
        run,
        project_id=project_id,
        payload=payload,
        project_config=get_project_analysis_config(project_id),
        target_type="commit",
        target_key=None,
    )
    for line in (result.get("report_markdown") or "").splitlines():
        yield _sse_event("chunk", {"text": line})
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

    group_key = payload["group"]["key"]
    run = _create_run(
        project_id=project_id,
        target_type="weekly",
        target_id=config_id,
        target_key=group_key,
        response_mode="streaming",
        scope=payload.get("scope", "full"),
        trigger_source=trigger_source,
        payload=payload,
    )

    result = _execute_analysis(
        run,
        project_id=project_id,
        payload=payload,
        project_config=get_project_analysis_config(project_id),
        target_type="weekly",
        target_key=group_key,
    )
    for line in (result.get("report_markdown") or "").splitlines():
        yield _sse_event("chunk", {"text": line})

    _update_weekly_state(payload, run, state, engine_status=result.get("status"))
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
    run = _create_run(
        project_id=project_id,
        target_type="weekly",
        target_id=config_id,
        target_key=group_key,
        response_mode="blocking",
        scope=payload.get("scope", "full"),
        trigger_source="scheduled",
        payload=payload,
    )

    result = _execute_analysis(
        run,
        project_id=project_id,
        payload=payload,
        project_config=get_project_analysis_config(project_id),
        target_type="weekly",
        target_key=group_key,
    )

    _update_weekly_state(payload, run, state, engine_status=result.get("status"))
    return {
        "status": "succeeded" if result.get("status") != "failed" else "failed",
        "run_id": run.id,
        "error_message": result.get("error_message") or None,
    }


def _update_weekly_state(
    payload: dict,
    run: AiAnalysisRun,
    state: Optional[AiWeeklyAnalysisState],
    *,
    engine_status: Optional[str],
) -> None:
    """推进这个周版本分组的「分析水位线」。

    **只有真正跑完整了的 run 才推进。** `last_analyzed_at` 是增量分析的水位线
    （`_summarize_weekly_files` 用它筛 `updated_at > last_analyzed_at` 的文件），
    一次没跑完的分析如果把它推到当前时刻，那批变更就被整体判成「已看过」——
    下一次分析直接返回 no_change 静默跳过，用户再点多少次都跑不动。

    这里刻意**不看 `run.status`**：`_persist_outcome` 把 `degraded`（降级但有报告，
    例如「上下文索取额度用尽，基于已有证据出结论」）也存成 `"succeeded"`，所以
    `run.status` 分不出「跑完了」与「没跑完就出结论」。而 degraded 恰恰是水位线
    最不该推进的那一类：线上有一次 767 个文件里有 748 个 `.lua` 的 diff 根本没读到，
    却被标成「已分析」，增量从此只看得到水位线之后的新文件。

    判据用引擎侧的 `outcome.status`（`STATUS_SUCCEEDED` 才推进），
    所以这是个**必填的关键字参数** —— 将来新增调用点时，忘了传会直接报错，
    而不是悄悄退回一个分不出 degraded 的判据。
    """
    if engine_status != STATUS_SUCCEEDED:
        return
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
            "created_at_display": _created_at_display(run),
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
            "created_at_display": _created_at_display(run),
        "response_text": run.response_text,
        "result": payload,
    }


def select_primary_weekly_config(configs: List[WeeklyVersionConfig]) -> WeeklyVersionConfig:
    """挑一个 config，用来给**新建分组**命名（`base_name`）并定窗口。

    **刻意不跟随取样的仓库优先级。** 这个值决定 `AiWeeklyAnalysisState.base_name`，
    而 base_name 参与 `group_key` 的计算 —— 换个仓库当 primary 就是换了分组身份，
    已有分组的增量水位线会对不上。所以这里保持既有口径：**取 id 最小的那个**
    （部署上就是先建的配表仓库那一侧）。

    以前这里写的是 `sorted(configs, key=(_repo_priority, -cfg.id), reverse=True)[0]`。
    当时 `_repo_priority` 对两个仓库都返回 2（`type == "git"` 那一句），于是实际
    行为就是「id 最小」；把它显式写出来，是为了在 `_repo_priority` 修好之后不再
    阴差阳错地改成「代码仓库当主」。
    """
    return min(configs, key=lambda cfg: cfg.id)
