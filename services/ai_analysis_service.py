#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis service（真实执行器）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
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
from services.ai.analysis_budget import budget_gate_reason, early_stop_guard
from services.ai.baseline import (
    DISPOSITION_PENDING,
    BaselineFinding,
    build_baseline_digest,
    classify,
    suppressed_fingerprints,
)
from services.ai.budget import effective_prompt_budget
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
from services.ai.llm_client import LLMError
from services.ai.platform_provider import PlatformContextProvider
from services.ai.weekly_sync_gate import group_config_ids, weekly_sync_in_flight
from services.ai.pricing import (
    PriceTable,
    load_price_table,
    price_change_requires_version_bump,
    price_table_doc_shape,
)
from services.ai.prompt import prompt_version
from services.ai.result_payload import (
    failed_result,
    result_payload,
)
from services.ai.rules import RuleThresholds, rules_version
from services.ai.run_progress import clear as clear_run_progress
from services.ai.run_progress import publish as publish_run_progress
from services.ai.skill_loader import load_skills, skill_revision
from services.ai.subagent import plan_family, run_family_with_seed, subagent_mode_of
from services.ai.trace_evidence import encode_evidence
from services.ai.usage import encode_tools
from utils.dpapi_utils import DPAPI_PREFIX, decrypt_dpapi
from utils.logger import log_print
from utils.security_utils import decrypt_credential, encrypt_credential
from utils.timezone_utils import format_beijing_time

MAX_FILES_DEFAULT = DEFAULT_MAX_FILES_PER_RUN

# 变更清单「全列」的字符上限。一行约 60 字符（`  - [M] <path>`），所以 60,000 字符
# 约合 1,000 个文件。实测线上那一轮 767 个文件的全量清单是 45,777 字符 —— 装得下，
# 所以取样退化成兜底而不是常态。取值不更大的原因：清单每轮都要进提示词，而它每涨
# 一万字符就直接挤掉一条 diff（单条上限 11,000 字符）。
MAX_LIST_CHARS = 60_000

# 「分析范围」的取值之一：不筛。其余取值见 `_filter_delta_files_by_focus`。
FOCUS_ALL = "all"

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
        # 单价表的格式示例。由这里下发而不是写死在模板里：格式只有 pricing 模块那一份，
        # 抄进模板后改格式就会漏改，而用户照着过期示例填会存不进去。
        "price_table_doc": price_table_doc_shape(),
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

    # 改单价必须同时改 version（见 pricing.price_change_requires_version_bump）。
    # 判定要拿**库里现在这一份**去比，不能用界面加载时那一份 —— 两个人同时开着配置
    # 界面时，后者手上的旧快照会让这条规矩形同虚设。放在 setattr 之前：
    # 校验失败就一个字段都不落库。
    if "model_price_table" in normalized:
        version_error = price_change_requires_version_bump(
            row.model_price_table, normalized.get("model_price_table")
        )
        if version_error:
            db.session.rollback()
            errors = [FieldError("model_price_table", "模型单价表（JSON）", version_error)]
            return False, version_error, [item.as_dict() for item in errors]

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
            # 缓存标记的两个开关来自**项目配置**（读不出来时是「不发标记」的保守默认值，
            # 见 models/ai_analysis/project_config.py）。探测路径（测试连接 / 拉模型列表）
            # 不传这两个参数，于是也走默认值 —— 两条路径的差异只在这里，不在行为里。
            prompt_cache_mode=str(config.get("prompt_cache_mode") or ""),
            prompt_cache_format=str(config.get("prompt_cache_format") or ""),
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


def project_price_table(project_id: int) -> tuple[PriceTable | None, tuple[str, ...]]:
    """这个项目该用哪张价格表：项目配置里填了就用它，没填用平台默认表。

    端点侧（读取）与落库侧（记 `pricing_version`）都用这一个函数 —— 两边各读一份配置
    会让「记录时的版本」与「算费用时的版本」不是同一份，而这两者必须能对上，
    否则版本号这个字段就白记了。
    """
    config = get_project_analysis_config(project_id)
    return _price_table_from_config(config)


def _price_table_from_config(config: Optional[dict]) -> tuple[PriceTable | None, tuple[str, ...]]:
    return load_price_table((config or {}).get("model_price_table") or "")


def _price_version_for(config: Optional[dict]) -> str:
    """落库用的价格表版本。**不为它单独查一次库**：调用方手上已经有配置了。

    配置里的价格表解析不了（JSON 坏了）时返回空串并说一句 —— 空串在库里是 NULL，
    读取侧按「当时没有可用价格表」解释，与事实一致。
    """
    table, errors = _price_table_from_config(config)
    if errors and (config or {}).get("model_price_table"):
        log_print("⚠️ AI 分析：项目价格表不可用（" + "；".join(errors) + "），本次运行不记价格版本", "AI")
    return table.version if table else ""


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

    返回清理条数（int，按 **run** 计）；**失败返回 None**。
    失败不返回 0：0 表示「本来就没东西可清」，两者在调用方的日志/界面上无法区分
    （同 services/excel_diff_cache_service.py::cleanup_old_cache 的说明）。

    ## 为什么必须显式删子表（这条保留策略此前等于没跑）

    `AiAnalysisTrace` / `AiAnalysisAnomaly` 的 `run_id` 外键**没有** `ON DELETE CASCADE`
    （全仓 models/ 里没有一处 ondelete），而 SQLite 的 `PRAGMA foreign_keys=ON` 在
    `utils/sqlite_config.py` 里是真的开着的（app.py:251 导入那个监听器）。于是删父行
    会直接抛 `FOREIGN KEY constraint failed`：整条 DELETE 回滚，**一条都删不掉** ——
    不是「留下孤儿行」，而是「保留策略整体失效」，run/trace 表只涨不减。

    实测（给一条过期 run 挂一行 trace）：`cleanup_expired_analysis_runs()` 返回 None
    并打印「清理AI分析缓存失败: (sqlite3.IntegrityError) FOREIGN KEY constraint failed」，
    过期 run / trace / anomaly 一行都没少。

    **先子后父**，同一个事务里做完。两张子表都要删：trace 是逐轮明细，anomaly 是异常
    条目（含人工处置状态）—— 它们都只挂在 run 上，run 一删就再也读不到
    （`AiAnalysisAnomaly.queue` 只按 run_id 查），留着就是谁都取不到的死行。
    """
    cutoff = _utcnow() - timedelta(days=retention_days)
    try:
        expired_run_ids = AiAnalysisRun.query.with_entities(AiAnalysisRun.id).filter(
            AiAnalysisRun.created_at.isnot(None)
        ).filter(AiAnalysisRun.created_at < cutoff)

        children = (
            AiAnalysisTrace.query.filter(
                AiAnalysisTrace.run_id.in_(expired_run_ids)
            ).delete(synchronize_session=False),
            AiAnalysisAnomaly.query.filter(
                AiAnalysisAnomaly.run_id.in_(expired_run_ids)
            ).delete(synchronize_session=False),
        )
        deleted = (
            AiAnalysisRun.query.filter(AiAnalysisRun.created_at.isnot(None))
            .filter(AiAnalysisRun.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.session.commit()
        if any(children):
            log_print(
                f"🧹 随过期分析记录一并清理: {children[0] or 0} 条轮次明细，"
                f"{children[1] or 0} 条异常",
                "AI",
            )
        return int(deleted or 0)
    except Exception as exc:
        db.session.rollback()
        log_print(f"清理AI分析缓存失败: {exc}", "AI", force=True)
        return None


def fail_orphaned_analysis_runs() -> int:
    """把平台重启后遗留的 `running` 记录判为失败，返回处理条数。

    **重启是「这些 run 已经死了」的确定性证据**：持有它们的进程已经不在了，它们永远
    不会再被写完成。留在库里就是一条两头骗人的幽灵记录：

    * 读侧 `_is_run_fresh` 要求 `status == "succeeded"`，所以 `/latest` 看不见它 ——
      界面以为「这个版本从没分析过」，或者悄悄退回更早的那次成功记录；
    * 界面拿到「没有结果」就会去自动开跑一次，于是用户每次重启后点一下「AI分析」
      都会莫名跑起一次分析，而且页面上一直是「进行中」。

    为什么不等 `AiAnalysisRun.effective_status` 那 1 小时超时：那 1 小时里界面会一直
    误判，而重启已经把答案给出来了。`effective_status` 那条兜底留给另一种情况 ——
    进程活着、但某次分析真的卡死了。
    """
    try:
        orphans = AiAnalysisRun.query.filter_by(status="running").all()
        if not orphans:
            return 0
        now = _utcnow()
        for run in orphans:
            run.status = "failed"
            run.finished_at = now
            # 与 `_persist_outcome` 的失败语义一致：失败的 run 不留结论字段。
            # running 记录本来也没有结论，这里是防御性的。
            run.response_payload = None
            run.response_text = ""
            run.error_message = "平台重启，本次分析被中断（未跑完，可以重新分析）。"
        db.session.commit()
        log_print(f"重置被重启中断的 AI 分析记录: {len(orphans)} 条", "AI", force=True)
        return len(orphans)
    except Exception as exc:
        db.session.rollback()
        log_print(f"重置中断的 AI 分析记录失败: {exc}", "AI", force=True)
        return 0


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


def _select_listed_files(
    delta_files: List[dict], max_files: int
) -> tuple[List[dict], bool]:
    """决定变更清单里**列哪些文件**，返回 (清单, 是否截断)。

    ## 为什么默认全列

    清单里一行就是一个路径（`  - [M] code/qz_pub/xxx/SeasonRankCfgMod.lua`，约 60 字符），
    **便宜**；而它换来的是模型能自己判断「该看什么」。实测线上那一轮 767 个文件的
    全量清单是 45,777 字符，只占提示词预算的一小部分 —— 也就是说绝大多数版本都会走到
    「全列」这一支，取样只是兜底。

    以前 200 这个上限同时管着「列多少」和「能读多少」，代价是没列出来的 567 个文件
    连 `file_diff` 都被拒。现在两者分开了：白名单给全部改动文件，**这里只影响名字列不列**。
    所以即使退化到取样，模型仍然能靠 `commit_detail` 查出提交的完整文件清单再点名索取
    （`render_change_summary` 的截断说明里写清了这条路）。
    """
    total_chars = sum(len(str(item.get("file_path") or "")) + 10 for item in delta_files)
    if total_chars <= MAX_LIST_CHARS:
        return list(delta_files), False
    sampled = _sample_with_repo_fairness(delta_files, max_files)
    return sampled, len(sampled) < len(delta_files)


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


def _resource_type_of(repo) -> str:
    return str(getattr(repo, "resource_type", "") or "").lower()


def _filter_delta_files_by_focus(
    delta_files: List[dict], focus: Optional[str], configs: List[WeeklyVersionConfig]
) -> Tuple[List[dict], str]:
    """按用户选的「分析范围」筛文件，返回 (筛选后的清单, 给人看的范围名)。

    ## 为什么要有这个

    一个 767 个文件的版本，**人比任何自动策略都清楚这周该看哪一半**：这周改的是配表数值，
    下几周才轮到代码。让用户先选范围，比在服务端猜「哪 200 个更重要」准得多，而且成本是
    线性的（筛选之后清单短了、额度也集中了）。

    取值：`all`（或空）不筛；`table` / `code` 按仓库的 `resource_type` 筛；数字按
    仓库 id 筛。**认不出来的一律不筛**（`focus` 是 URL 参数，不能让它把分析变成空跑）。
    """
    text = str(focus or "").strip().lower()
    if not text or text == FOCUS_ALL:
        return list(delta_files), ""

    repo_by_id = {cfg.repository_id: cfg.repository for cfg in configs}

    if text in ("table", "code"):
        kept = [
            item for item in delta_files
            if _resource_type_of(repo_by_id.get(item.get("repository_id"))) == text
        ]
        label = "仅配表仓库" if text == "table" else "仅代码仓库"
        return kept, label

    try:
        repo_id = int(text)
    except (TypeError, ValueError):
        return list(delta_files), ""

    repo = repo_by_id.get(repo_id)
    if repo is None:
        return list(delta_files), ""
    kept = [item for item in delta_files if item.get("repository_id") == repo_id]
    return kept, f"仅仓库「{repo.name}」"


def build_weekly_payload(
    config_id: int,
    *,
    force_full: bool = False,
    focus: Optional[str] = None,
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
    focus_label = ""
    if focus:
        delta_files, focus_label = _filter_delta_files_by_focus(delta_files, focus, configs)
        if not delta_files:
            return None, state, "focus_empty"
        # **计数要跟着筛选走。** 不跟着改的话，提示词会告诉模型「本次变更共 767 个文件」
        # 而它只看得到 19 个 —— 那正是「把清单当全量」的镜像错误：这次是把全量说大了。
        summary = {
            **summary,
            "total_files": len(delta_files),
            "delta_files": len(delta_files),
        }
    list_files, list_truncated = _select_listed_files(delta_files, max_files)

    truncated = list_truncated
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
        # 用户选的「分析范围」。`label` 会被渲染进提示词（见 change_set._scope_note），
        # 所以报告与界面都能说清「这次只看了一半」。
        "focus": {"key": str(focus or FOCUS_ALL), "label": focus_label},
        "group": {
            "key": group_key,
            "base_name": base_name,
            "project_id": config.project_id,
            "start_time": config.start_time.isoformat() if config.start_time else None,
            "end_time": config.end_time.isoformat() if config.end_time else None,
        },
        "summary": summary,
        "repositories": repo_details,
        # 白名单：本批次**全部**改动过的文件。模型能读的 diff 就是这个集合。
        "delta_files": delta_files,
        # 提示词里**列出来**的那部分。绝大多数版本与 `delta_files` 相同（见下面的说明）。
        "list_files": list_files,
        "delta_truncated": truncated,
    }
    payload["policy"]["truncated"] = truncated
    if truncated:
        payload["policy"]["truncation_reason"] = "token_budget"
    return payload, state, None


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


def _apply_model_window(
    client: object, project_config: dict, limits: EngineLimits
) -> Tuple[EngineLimits, str]:
    """按模型窗口的水位压提示词预算。返回 `(额度, 说明)`。

    窗口**尽量向端点问**（`/v1/models` 里有时会声明）：端点不支持列模型、没声明窗口、
    或者报了个不合理的值，就按 `budget.DEFAULT_CONTEXT_TOKENS`（1M）这个**口径值**处理，
    并在说明里写明「按默认值处理」——**不能让人以为平台问到了**，那是假设不是事实。

    压到「窗口 × 60%」，理由与取值见 `budget.effective_prompt_budget`：以前的规则是
    「只在按 1:1 算也超窗时才压」，而窗口问不到时它什么都不做 —— 于是一个把预算配到
    2M 字的项目会拿一份 2M 字符的提示词去撞模型，必然被拒，整次分析连结论一起作废。

    说明为空表示没压（默认 360,000 字的预算在所有窗口下都不触发水位）。
    """
    model = str(project_config.get("api_model") or "").strip()
    window: object = None
    if model:
        contexts = getattr(client, "model_contexts", None)
        if contexts is not None:
            try:
                window = contexts().get(model)
            except LLMError as exc:
                # 问不到窗口不是失败点：下面按默认窗口继续（见 docstring）。
                log_print(f"AI 分析：取模型上下文窗口失败（按默认窗口处理）: {exc}")

    budget, note = effective_prompt_budget(limits.prompt_char_budget, window)
    if not note:
        return limits, ""
    return replace(limits, prompt_char_budget=budget), note


def _engine_limits(project_config: dict) -> EngineLimits:
    """项目配置 → 引擎额度。

    用 `or` 而不是 `dict.get(key, 默认)`：配置行是懒创建的，没填过的列读出来是 `None`，
    而 `None` 会让 `range(1, None + 1)` 在半夜的自动轮询里炸掉。
    """
    defaults = EngineLimits()
    requests = int(project_config.get("max_tool_requests") or defaults.max_tool_requests)
    return EngineLimits(
        max_rounds=int(project_config.get("max_analysis_rounds") or defaults.max_rounds),
        max_tool_requests=requests,
        # 条数上限**不得小于**索取次数：小于就会出现「付了 N 次索取、只带走 max_items 条」
        # —— 取回来的上下文被 `enforce_budget` 按条数静默裁掉，白花额度（见
        # `context_tools.DEFAULT_MAX_TOOL_REQUESTS` 的说明）。索取次数是用户可配的
        # （取值上限 100），所以这个下限必须跟着**配置**走，只在两个默认值上成立是不够的：
        # 用户把索取上限调到 40 的那一刻，20 条的条数上限就会开始丢他的东西。
        max_items=max(defaults.max_items, requests),
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


def _persist_outcome(
    run: AiAnalysisRun,
    outcome: EngineOutcome,
    result: dict,
    *,
    pricing_version: str = "",
) -> None:
    """把引擎结果落库：run 上写状态与账目，逐轮写 trace，逐条写异常。

    `error_message` 这一列此前**从来没有被写入过**，于是失败的分析在界面上永远是
    「分析中」，用户无从判断。这是这次要修的一部分。

    ## 为什么这里要把「账」写全

    这批列（`tool_requests_used` / `context_chars` / `anomalies_found` / `dropped_count`
    与新的用量列）此前**存在但从来没被写过**，一直是 NULL —— 于是「这次花了多少、
    上下文塞了多少、丢了几条」在库里根本没有记录，面板再怎么做也没有数据可读。
    写全它们才算把采集侧接上。

    `pricing_version` 由调用方传（它在建引擎时已经读过项目配置，见 `_execute_analysis`），
    这里不为了一个版本号再查一次库。
    """
    run.status = "failed" if outcome.status == STATUS_FAILED else "succeeded"
    run.finished_at = _utcnow()
    if run.status == "failed":
        # 失败**不写结论字段**。以前这里照样写 response_payload / response_text，
        # 于是库里那条失败记录长得和成功记录一样：有「结论」、有风险等级、有范围，
        # 前端只判「有没有结果」就把徽章显示成「已有结果」并附上「风险等级 high」。
        # 而那个 high 根本不是模型给的 —— 是 services/ai/result_payload.py 的 determine_risk_level 按变更规模
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
    # 用量：`None`（上游没报）原样落 NULL，**不许写成 0** —— 读取侧就是靠 NULL 与 0
    # 的差别来区分「未上报」与「确实没命中」的（见 services/ai/usage.py）。
    run.cache_read_tokens = outcome.cache_read_tokens
    run.cache_write_tokens = outcome.cache_write_tokens
    run.cache_source = outcome.cache_source or None
    run.duration_ms = outcome.duration_ms
    run.tool_requests_used = outcome.requests_used
    run.context_chars = outcome.context_chars
    run.tool_stats_json = encode_tools(outcome.tool_stats)
    run.anomalies_found = len(result.get("anomalies") or [])
    run.dropped_count = len(outcome.dropped)
    run.pricing_version = pricing_version or None
    run.error_message = outcome.error_message or None
    # 子代理模式：这次是按分片跑的，就如实记下来（没开时两列是 NULL = 老行为）。
    # 落在这里而不是 `result` 里：它是**这一次运行怎么跑的**，与「结论是什么」无关。
    mode = subagent_mode_of(outcome)
    run.subagent_mode = mode[0] if mode else None
    run.subagent_count = mode[1] if mode else None

    for record in outcome.rounds:
        db.session.add(
            AiAnalysisTrace(
                run_id=run.id,
                round_index=record.index,
                # 这一轮是哪个分片代理跑的（NULL = 主代理自己那几轮，也是所有老行的情形）。
                # `round_index` 是家族内全局序号，成员内部的轮次另存在 `agent_round` 里。
                agent=record.agent or None,
                agent_round=record.agent_round or None,
                outcome=record.status,
                parsed_ok=record.status != "unparsable",
                # 这一轮要了什么、拿到了什么、丢了什么、模型原样返回了什么 —— 编码只在
                # `trace_evidence` 一处（写库侧与读库侧共用），这里不拼 JSON：
                # 原先只写计数，于是「取数失败」与「真的读了一份 diff」在面板上长得一样。
                **encode_evidence(record),
                error=record.note or None,
                # 逐轮用量。这几列同样一直是 NULL：没有它们，「钱花在第几轮」答不上来，
                # 而提示词每轮都把上一轮的上下文重发一遍，后几轮才是贵的那些。
                tokens_input=record.prompt_tokens,
                tokens_output=record.completion_tokens,
                cache_read_tokens=record.cache_read_tokens,
                cache_write_tokens=record.cache_write_tokens,
                request_chars=record.prompt_chars,
                context_chars=record.context_chars,
                duration_ms=record.duration_ms,
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

    这层是**薄壳**：真正的活在 `_run_engine_and_persist` 里，这里只负责「跑完之后一定
    要把进度快照清掉」。清不掉的后果是界面一直显示「正在跑」—— 那条进程内的表没有
    别的清理时机（`run_progress` 的过期时限只是兜底）。

    ## 「不抛异常」这句承诺是**这一层**的责任，`_run_engine_and_persist` 不保证它

    上面那句承诺此前只有 `try/finally` 兜着，一个 `except` 都没有 —— 也就是
    「不抛异常」靠的是「下游一处都不会抛」这个假设。而下游做的事包括：读配置、
    解 JSON、连模型、写库。任何一处没想到的异常都会**带着「运行号早就发给界面了」**
    穿出生成器：SSE 在浏览器那头表现为连接静默断开，界面只剩一句
    「AI 分析失败或连接中断」，而真正的原因只留在服务端的日志里，
    库里那条 run 还停在 `running`（一小时后才被 `effective_status` 翻成失败）。

    用户报的正是这一幕。所以凡是**已经有运行号**的失败，都必须变成带原因的结论：
    在这里兜住，把原因写进 `error_message` 落库，界面就能照常渲染出「失败 + 原因」。
    """
    try:
        return _run_engine_and_persist(
            run,
            project_id=project_id,
            payload=payload,
            project_config=project_config,
            target_type=target_type,
            target_key=target_key,
        )
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：这一层的承诺就是这一句
        return _record_unexpected_failure(run, payload, project_config, exc)
    finally:
        clear_run_progress(run.id)


def _record_unexpected_failure(
    run: AiAnalysisRun, payload: dict, project_config: dict, exc: Exception
) -> dict:
    """把「跑的过程中冒出来的异常」写成一条失败结论（并落库）。

    **它自己也不许抛**：调用它的时候现场已经出过一次事故了，再抛一次就是
    「连失败都报不出来」。所以落库那一步单独兜一层，失败只写日志。
    """
    message = f"分析中断：{type(exc).__name__}: {exc}"
    log_print(f"❌ AI 分析异常中断 {message}（run={run.id}）", "AI", force=True)

    try:
        summary = payload.get("summary") or {}
    except Exception:  # noqa: BLE001 —— payload 形态异常时别把收尾也弄挂
        summary = {}

    try:
        # 异常很可能来自一次失败的写库（列超长、连接断了…），会话此时是脏的，
        # 不回滚的话下面这句 `_persist_outcome` 会直接 PendingRollbackError ——
        # 那就成了「失败的原因报不出来，只报得出回滚失败」。
        db.session.rollback()
    except Exception as rollback_exc:  # noqa: BLE001
        log_print(f"⚠️ AI 分析：回滚会话失败（{rollback_exc}），继续尝试落库失败结论", "AI")

    result = failed_result(summary, message)
    try:
        _persist_outcome(
            run, engine_failed(message), result,
            pricing_version=_price_version_for(project_config),
        )
    except Exception as persist_exc:  # noqa: BLE001
        log_print(
            f"❌ AI 分析：失败结论也没能落库（run={run.id} {type(persist_exc).__name__}: "
            f"{persist_exc}），库里这条运行会停在 running",
            "AI",
            force=True,
        )
    return result


def _run_engine_and_persist(
    run: AiAnalysisRun,
    *,
    project_id: int,
    payload: dict,
    project_config: dict,
    target_type: str,
    target_key: Optional[str],
) -> dict:
    summary = payload.get("summary") or {}

    # 正式分析用配置里的单次请求超时（默认 300 秒，界面可改），**不是**探测用的 30 秒。
    # 这两者混用会让分析必然超时，见 build_endpoint_client 的说明。
    configured_timeout = get_project_analysis_config(project_id).get("request_timeout_seconds")
    client, errors = build_endpoint_client(
        project_id, {}, timeout_seconds=_coerce_timeout(configured_timeout)
    )
    if client is None:
        message = "接口配置不完整：" + "；".join(item["message"] for item in errors)
        result = failed_result(summary, message)
        _persist_outcome(
            run, engine_failed(message), result,
            pricing_version=_price_version_for(project_config),
        )
        return result

    loaded = _load_project_skills(project_id)
    if loaded is None:
        message = "分析协议（skill）加载失败，未发起分析。"
        result = failed_result(summary, message)
        _persist_outcome(
            run, engine_failed(message), result,
            pricing_version=_price_version_for(project_config),
        )
        return result

    readable = sorted(getattr(loaded, "readable", {}) or {})
    change = (
        from_commit_payload(payload, readable_references=readable)
        if payload.get("mode") == "commit"
        else from_weekly_payload(payload, readable_references=readable)
    )

    limits, budget_note = _apply_model_window(client, project_config, _engine_limits(project_config))
    if budget_note:
        log_print(f"⚠️ AI 分析：{budget_note}", "AI", force=True)
    # 这一份参数**两条路共用**（单代理 / 子代理），键名就是 `engine.run_analysis` 的形参名。
    # 分成两处各写一遍的话，14 个参数里总会有一处漏掉，而漏掉的那个是静默的。
    engine_args = {
        "client": client,
        # 周版本批次分析读平台**已算好并落库**的那一份合并 diff（就是周版本页面读的
        # 同一个 payload），而不是现场用本地工作副本重算 —— 见
        # `platform_provider._weekly_stored_diff`。单提交分析要关掉它：那时模型问的是
        # 「这一条提交改了什么」，而缓存里那一份覆盖的是一个窗口（可能好几条提交）。
        # 子代理模式下**所有成员共用一个 provider**：它的记忆（agent 取回的正文与差异）
        # 因此也共享，N 个成员不会把同一份内容取 N 遍。
        "provider": PlatformContextProvider(
            # `scope` 只有 `find_references` 用：那个工具要知道「本批次改了哪些文件」，
            # 而这正是 scope 才知道的事（工具本身不带 commit，见 ai/reference_search.py）。
            loaded=loaded, scope=change.scope,
            use_stored_batch_diff=(payload.get("mode") != "commit"),
        ),
        "loaded": loaded,
        "scope": change.scope,
        "change_summary": change.summary,
        "limits": limits,
        "thresholds": RuleThresholds.from_config(project_config),
        "project_knowledge": project_config.get("project_knowledge") or "",
        "project_instructions": project_config.get("prompt_template") or "",
        "baseline_digest": _baseline_digest(target_type, target_key, change),
        # 每跑完一轮把累计用量写进进程内的进度快照（services/ai/run_progress.py，界面
        # 一边跑一边轮询它）。**它是给界面看的一眼，不是账** —— 账在 `_persist_outcome`。
        "on_round": lambda progress: publish_run_progress(run.id, project_id, progress),
    }
    # 子代理模式（services/ai/subagent.py）：默认关、只对周版本生效，不适用时返回 None
    # 走原来的单代理路径。`verify` 是对账轮，它依附在子代理上 —— 没开子代理时不生效。
    plan = plan_family(
        mode=payload.get("mode") or "",
        enabled=bool(project_config.get("subagent_enabled")),
        count=project_config.get("subagent_count") or 0,
        verify=bool(project_config.get("subagent_verify")),
        limits=limits,
    )
    outcome = (
        run_analysis(**engine_args)
        if plan is None
        else run_family_with_seed(
            plan=plan,
            # 每片开跑前看一眼预算（含本次已消耗的）：判据与起跑闸门同一个 `budget_status`，
            # 只是多算了这一家子已花掉的 token —— 否则前面几片的花费还没落库，每一片都
            # 看到「还没超」。被跳过的分片会进报告的信息缺口。
            should_skip=early_stop_guard(project_id, entry="subagent"),
            **engine_args,
        )
    )

    result = result_payload(
        outcome,
        payload,
        suppressed=_suppressed(target_type, target_key, change),
        context_budget_note=budget_note,
    )
    _persist_outcome(
        run, outcome, result, pricing_version=_price_version_for(project_config)
    )
    return result


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {_json_dumps(payload)}\n\n"


def end_with_a_terminal_event(events: Iterable[str]) -> Iterable[str]:
    """把一串 SSE 事件包成「**一定以 `result` 或 `error` 收尾**」的一串。

    ## 为什么需要这一层（它拦的是哪一段）

    `_execute_analysis` 已经承诺不抛异常，但一次流式分析里**不止**那一件事会抛：
    建运行记录之前的 payload 构造、跑完之后推进周版本水位线、甚至把结论转成 JSON。
    任何一处抛出去，生成器就结束了（WSGI 只是把连接关掉）—— 浏览器那头是
    `EventSource` 收到一个**没有 `data` 的 `error` 事件**，而界面上没有 `data` 的
    `error` 只能说出那句含糊的「AI 分析失败或连接中断」，原因一个字都不在里面。
    用户报了这句话，这就是它的来源之一。

    所以这里把「兜底收尾」放在**流的最外层**：还活着就把原因发出去。它不需要知道
    运行号 —— 走到这里时运行记录要么还没建（那就没有任何要修正的库状态），
    要么结论早已落库（`_persist_outcome` 在 `result` 之前就写完了）。

    顺带钉住一件更容易被忽略的事：**正常跑完的流不许被这层改动**。它只在异常路径上
    追加事件，成功路径逐字透传（`return` 之后不能再 yield —— 那会给已经收尾的流
    补一个 `error`，把一次成功的分析显示成失败）。
    """
    try:
        yield from events
    except Exception as exc:  # noqa: BLE001 —— 这一层的存在意义就是兜住任何异常
        message = f"分析中断：{type(exc).__name__}: {exc}"
        log_print(f"❌ AI 分析的 SSE 流异常中断（{message}），已把原因发给界面", "AI", force=True)
        yield _sse_event("error", {"message": message})


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

    # 预算闸门（手动·单提交）。放在缓存复用之后：回放一份已有结论不花钱，不该被拦；
    # 也放在真正开跑之前：超预算就不发起任何请求。
    budget_reason = budget_gate_reason(project_id, entry="commit_manual")
    if budget_reason:
        yield _sse_event("error", {"message": budget_reason})
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
    # **开跑之前**先把运行号发出去：界面要按它轮询「跑到第几轮、现在超了没」
    # （`/ai-analysis/runs/<id>/progress`），而分析是阻塞跑的 —— 等结束再给号，
    # 那个提示就只能在跑完之后才可能出现，也就没有意义了。
    yield _sse_event("run", {"run_id": run.id})

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
    # 带上 run_id：抽屉里那一行「本次消耗」的「明细」按钮要按**运行记录**取数
    # （`/ai-analysis/runs/<id>/usage`），而 SSE 的 result 事件只带结论本身。
    # 只加在事件上，不进 `response_payload` —— 那是落库的结论，不该混入运行身份。
    yield _sse_event("result", {**result, "run_id": run.id})


def stream_weekly_analysis(
    config_id: int, trigger_source: str = "manual", focus: Optional[str] = None
) -> Iterable[str]:
    payload, state, skip_reason = build_weekly_payload(config_id, focus=focus)
    if skip_reason == "no_change":
        cached = get_latest_weekly_result(config_id)
        if cached and cached.get("run_id"):
            run = db.session.get(AiAnalysisRun, cached["run_id"])
            if run and _is_run_fresh(run):
                yield from _stream_cached_run(run)
                return
        payload, state, skip_reason = build_weekly_payload(config_id, force_full=True, focus=focus)
    if skip_reason == "focus_empty":
        yield _sse_event(
            "error",
            {"message": "选定的分析范围里没有改动过的文件，换一个范围再试。"},
        )
        return
    if payload is None:
        yield _sse_event("error", {"message": "Weekly analysis payload not ready."})
        return

    project_id = payload["group"]["project_id"]

    # 预算闸门（手动·周版本）。与 commit 那条同理：缓存复用之后、真正开跑之前。
    budget_reason = budget_gate_reason(project_id, entry="weekly_manual")
    if budget_reason:
        yield _sse_event("error", {"message": budget_reason})
        return

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
    # 与单提交那条同理：运行号必须在**开跑之前**给出去，界面才能一边跑一边问进度。
    yield _sse_event("run", {"run_id": run.id})

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
    # 带上 run_id：抽屉里那一行「本次消耗」的「明细」按钮要按**运行记录**取数
    # （`/ai-analysis/runs/<id>/usage`），而 SSE 的 result 事件只带结论本身。
    # 只加在事件上，不进 `response_payload` —— 那是落库的结论，不该混入运行身份。
    yield _sse_event("result", {**result, "run_id": run.id})


def run_weekly_analysis_background(config_id: int, task_id: Optional[int] = None) -> dict:
    # **执行前再查一次开关。** `schedule_weekly_ai_analysis_tasks` 在建任务时查过，
    # 但任务一旦入队就独立于开关了：关掉自动分析**不会**取消已经排队的任务，而重启时
    # `load_pending_tasks` 还会把上次残留的 `processing` 任务改回 `pending` 重新入队
    # （`task_worker_service.py:1273-1279`）。于是「关掉开关 + 重启」必定跑一次 ——
    # 这正是用户报的「停止了自动分析后仍然自动触发」。
    #
    # 闸设在这里是安全的：本函数是**后台路径的唯一入口**（两个调用点都在
    # `task_worker_service`），手动分析走的是 `stream_weekly_analysis`，不受影响。
    config = db.session.get(WeeklyVersionConfig, config_id)
    if config is not None:
        project_cfg = get_project_analysis_config(config.project_id)
        if not project_cfg.get("auto_weekly_enabled", True):
            log_print(
                f"周版本自动分析已关闭，跳过已排队的任务: config_id={config_id}", "AI", force=True,
            )
            return {"status": "skipped", "reason": "auto_weekly_disabled"}

        # 预算闸门（后台·周版本）。与开关那道闸同理放在这里：本函数是后台路径的
        # 唯一入口，闸设在这里既覆盖调度器，也覆盖「开关被关掉之前就已经排好队」的
        # 残留任务。**记成 skipped 并写日志**，不静默 —— 静默跳过的表现是
        # 「自动分析不跑了，但没有任何地方说为什么」。
        budget_reason = budget_gate_reason(config.project_id, entry="weekly_background")
        if budget_reason:
            log_print(
                f"周版本自动分析被预算拦截，跳过已排队的任务: config_id={config_id} "
                f"project={config.project_id} —— {budget_reason}",
                "AI",
                force=True,
            )
            return {"status": "skipped", "reason": "over_budget", "message": budget_reason}

        # 同步闸门（后台·周版本）。与上面两道同理放在这里：调度器那道拦的是「还没排队」，
        # 这道拦的是「已经排好队了」—— 重启时 `load_pending_tasks` 会把上次残留的
        # `processing` 任务改回 pending 再跑一次，那正好撞上启动期的同步。
        # **跳过且不推进水位线**：`last_analyzed_at` 不变，下一个调度周期会重新排队。
        sync_reason = weekly_sync_in_flight(group_config_ids(config))
        if sync_reason:
            log_print(
                f"周版本自动分析推迟（{sync_reason}）: config_id={config_id}",
                "AI",
                force=True,
            )
            return {"status": "skipped", "reason": "sync_in_flight", "message": sync_reason}

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


def _focus_from_run(run: AiAnalysisRun) -> dict:
    """这次 run 用的「分析范围」。存在它自己的 request_payload 里。

    为什么要读出来给界面：结果是按范围筛过的，界面若只说「最近分析」而不说范围，
    用户会把「只看配表仓库」那份结论当成对全版本的结论 —— 与「把清单当全量」是同一类
    错误。老记录没有这个字段，读不到就按「全部」处理。
    """
    stored = _parse_response_payload(run.request_payload)
    focus = (stored or {}).get("focus") if isinstance(stored, dict) else None
    if isinstance(focus, dict):
        return {
            "key": str(focus.get("key") or FOCUS_ALL),
            "label": str(focus.get("label") or ""),
        }
    return {"key": FOCUS_ALL, "label": ""}


def _in_progress_result(run: AiAnalysisRun) -> dict:
    """「正在进行中」的读侧形态：给得出身份，给不出结论。"""
    return {
        "run_id": run.id,
        "status": "running",
        "in_progress": True,
        "scope": run.scope,
        "trigger_source": run.trigger_source,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "created_at_display": _created_at_display(run),
        "response_text": "",
        "result": None,
    }


def _conclusion_payload(run: AiAnalysisRun) -> dict:
    """一条**有结论的** run 的读侧形态。"""
    return {
        "run_id": run.id,
        "status": run.status,
        "scope": run.scope,
        "trigger_source": run.trigger_source,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "created_at_display": _created_at_display(run),
        "response_text": run.response_text,
        "result": _parse_response_payload(run.response_payload),
    }


def _last_attempt_failed_result(run: AiAnalysisRun) -> dict:
    """「最近一次失败」的读侧形态：给得出失败原因，给不出结论。

    这条路径以前根本走不到 —— 读侧只在「有新结论」时返回值，失败一律折叠成 None，
    于是界面里那个 `status === 'failed'` 分支（「上次分析失败：<原因>」）是死的，
    用户看到的是「暂无分析结果」。失败与「没分析过」是两件事，不能混。
    """
    return {
        "run_id": run.id,
        "status": "failed",
        "in_progress": False,
        "scope": run.scope,
        "trigger_source": run.trigger_source,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "created_at_display": _created_at_display(run),
        "error_message": run.error_message or "",
        "response_text": "",
        "result": None,
    }


# 「这份结论不是最新那一次」的两种成因。它们要分开说，因为用户该做的事不一样：
# 前者要重新分析才有新规则下的结论，后者只要等/重跑上一次没跑完的那次。
STALE_REASON_RULES_CHANGED = "rules_changed"
STALE_REASON_INTERRUPTED = "interrupted"


def _latest_concluded_run(conditions) -> Optional[AiAnalysisRun]:
    """该目标最近一条**真的有结论**的成功记录。**刻意不看溯源。**

    溯源自（提示词 / skill / 规则 / 模型的版本号）只该决定「这份结论能不能拿来跳过
    重跑」—— 那是省一次调用的事；不该决定「这份结论还看不看得到」—— 那是用户以为
    自己的分析白做了的事。两者混用一把尺子的后果已经出现过：改过 `prompt.py` 或
    `SKILL.md` 之后，所有历史结论在界面上一起变成「未分析」，而结论一直躺在库里。
    """
    run = (
        AiAnalysisRun.query.filter(*conditions)
        .filter(AiAnalysisRun.status == "succeeded")
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )
    if run is None or not (run.response_text or run.response_payload):
        return None
    ts = run.finished_at or run.created_at
    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts < _analysis_cache_cutoff():
        return None
    return run


def _stale_note(reason: str, *, concluded: AiAnalysisRun, newest: AiAnalysisRun) -> str:
    """把「这份结论为什么不是最新那一次」写成给人看的一句话。

    在服务端写死一句话，而不是让三份抽屉模板各自拼：那三份是逐字复制的，让它们
    各自拼文案就是三处会各自演化 —— 而这个仓库里「同一句话在多处各自演化」正是
    反复出问题的地方（见 `static/js/ai_usage_line.js` 的存在理由）。
    """
    at = _created_at_display(concluded)
    if reason == STALE_REASON_RULES_CHANGED:
        return f"该结论由旧版评审规程产出（提示词/规则/模型已更新），以下是 {at} 的结论，仅供参考"
    return (
        f"最近一次分析未完成（{_created_at_display(newest)}，{newest.status}），"
        f"以下是 {at} 的结论"
    )


def _read_latest_result(conditions, *, project_id: Optional[int] = None) -> Optional[dict]:
    """读侧的统一口径：**库里只要有结论，就必须看得见。**

    判定顺序（每一步都要能回答「用户接下来该做什么」）：

    1. 最新那条可用（`_is_run_fresh`：成功 + 有内容 + 未过期 + 溯源一致）→ 直接给；
    2. 最新那条正在跑（且不是僵尸）→ 如实报「进行中」，不能报成「没有结果」。
       报成没有结果的后果不只是少显示一条：界面拿到「没有结果」会去自动开跑一次，
       于是同一次分析在用户眼里变成两次、页面上永远挂着「进行中」——用户报过这个。
       进程已死的幽灵记录由启动时的 `fail_orphaned_analysis_runs` 清掉，所以这里
       剩下的 running 是真的在跑；
    3. 否则**退回到最近一条真有结论的成功记录**，并带上 `stale` / `stale_reason` /
       `stale_note` 如实说明它是旧的。这是用户报的那条：「我进行过 AI 分析，重启
       服务后变成未分析」—— 结论还在，只是最新那次不是它（规则变了、或最新那次被
       重启打断成 failed），而旧口径把「最新那条不可用」直接等同于「没有结论」；
    4. 一条结论都没有时，才轮到「最近一次失败」这条形态（有原因、没结论）；
       连失败都没有就返回 None —— 那才是真的没分析过。

    `project_id` 只用于溯源现算（见 `_is_run_fresh` 的 `expected` 参数）。
    """
    run = (
        AiAnalysisRun.query.filter(*conditions)
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )
    if run is None:
        return None
    expected = _current_provenance(project_id) if project_id else None
    if _is_run_fresh(run, expected=expected):
        return _conclusion_payload(run)
    if run.status == "running" and not run.is_stale_running:
        return _in_progress_result(run)

    concluded = _latest_concluded_run(conditions)
    if concluded is None:
        return _last_attempt_failed_result(run) if run.status == "failed" else None
    if concluded.id == run.id:
        # 唯一那条成功记录就是最新这条，却没过 `_is_run_fresh` —— 只可能是溯源变了
        # （时间窗与 status 在 `_latest_concluded_run` 里已经判过一遍）。
        reason = STALE_REASON_RULES_CHANGED
    else:
        reason = STALE_REASON_INTERRUPTED
    payload = _conclusion_payload(concluded)
    payload["stale"] = True
    payload["stale_reason"] = reason
    payload["stale_note"] = _stale_note(reason, concluded=concluded, newest=run)
    # 最新那条的身份也带上：界面要能说清「挡住它的那一次是什么状态」。
    payload["newest_run"] = {
        "run_id": run.id,
        "status": run.status,
        "created_at_display": _created_at_display(run),
    }
    return payload


def get_latest_weekly_result(config_id: int) -> Optional[dict]:
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    group_key = build_weekly_group_key(config)
    payload = _read_latest_result(
        (AiAnalysisRun.target_type == "weekly", AiAnalysisRun.target_key == group_key),
        project_id=config.project_id,
    )
    if payload and payload.get("run_id"):
        # `focus` 只有周版本有（单提交不分范围），所以只在周版本这条路径上补。
        run = db.session.get(AiAnalysisRun, payload["run_id"])
        payload["focus"] = _focus_from_run(run) if run else {"key": FOCUS_ALL, "label": ""}
    return payload


def get_latest_commit_result(commit_id: int) -> Optional[dict]:
    commit = db.session.get(Commit, commit_id)
    repo = db.session.get(Repository, commit.repository_id) if commit else None
    return _read_latest_result(
        (AiAnalysisRun.target_type == "commit", AiAnalysisRun.target_id == commit_id),
        project_id=repo.project_id if repo else None,
    )


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
