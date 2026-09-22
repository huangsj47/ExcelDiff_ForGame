#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目级 AI 配置与接口凭据：读、写，以及按它构造端点客户端。

## 为什么单独一个文件

`services/ai_analysis_service.py` 是仓库里改动最频繁的文件（近 200 个提交里被改过 20 次），
而它同时贴着长度闸门（`scripts/check_file_length.py --strict` 在 2000 行报错）。这一块是
那个文件里**最独立的一层**：进来的是表单字典与 project_id，出去的是配置字典 / 端点客户端；
它不认识引擎、不认识报告、也不认识分析范围。搬出来之后，「Token 存在哪、什么时候掩码」
「保存时校验哪几栏」这类问题只需要看这一个文件。命名与 `baseline_source.py` 同构。

## 与模型层的分工

`models/ai_analysis/project_config.py` 定义列、默认值与取值范围（**唯一事实源**），
这里负责「按业务规则读写它们」：合并、校验、掩码、加密存储。

## `_utcnow` 为什么在这里

它原先定义在 `ai_analysis_service.py`，但**三个模块**都要用它（这里、`run_cache_source`
和原文件的落库路径）。放在最底层的这个模块，另外两处从它 import —— 只有一份定义。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Mapping, Optional, Tuple

from sqlalchemy.exc import SQLAlchemyError

from models import WeeklyVersionConfig, db
from models.ai_analysis import AiProjectAnalysisConfig, AiProjectApiKey
from models.ai_analysis.project_config import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_RANGE,
)
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
from services.ai.pricing import (
    PriceTable,
    load_price_table,
    price_change_requires_version_bump,
    price_table_doc_shape,
)
from utils.dpapi_utils import DPAPI_PREFIX, decrypt_dpapi
from utils.logger import log_print
from utils.security_utils import decrypt_credential, encrypt_credential


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

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

def _cross_field_errors(row, normalized: dict) -> list:
    """跨字段规则。**单字段校验器看不到另一栏**，所以只能放在这一层。

    只有「两栏的最终值都能定下来」时才判：提交里没有的键回落到**这一行实际生效的值**
    （`row.resolved()`，NULL 也按默认值补齐）。界面可能只改一栏，那时拿不到的那一栏
    必须按现值算 —— 否则一个本来就合法的组合会被误判成冲突，用户改个模型名都存不进去。

    为什么「对账轮 + 子代理关闭」必须**报错**而不是静默改成关：那正是本仓库反复要挡的
    那类缺陷 —— 界面显示 A、存下去/跑起来是 B。用户勾着「对账轮」，报告里却没有
    「对账结果」，他找不到任何地方说明为什么。`plan_family` 那边（`services/ai/subagent.py`）
    已经写明「没开子代理就没有对账轮」，这里做的是把同一句话**提前到保存那一刻**说出来。

    也**不做**「越界就夹到边界内」那种兜底：与 `_coerce_int` 同一条纪律 ——
    要改的是告诉他为什么不行，不是悄悄改掉他的值。
    """
    current = row.resolved() if hasattr(row, "resolved") else {}

    def final(field_name: str):
        if field_name in normalized:
            return normalized[field_name]
        return current.get(field_name, FIELD_DEFAULTS.get(field_name))

    verify = bool(final("subagent_verify"))
    enabled = bool(final("subagent_enabled"))
    if verify and not enabled:
        return [
            FieldError(
                "subagent_verify",
                "对账轮（找反证，仅周版本）",
                "需要先打开「子代理模式」：对账轮核对的是各分片各自的结论，"
                "单代理那条路没有东西可核对，这一轮不会执行。"
                "要么打开上面的开关，要么把这一栏关掉。",
            )
        ]
    return []

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

    # 跨字段（对账轮 ⇐ 子代理模式）。与逐字段校验**同一套出口**，界面不必为它再写一份渲染。
    cross_errors = _cross_field_errors(row, normalized)
    if cross_errors:
        db.session.rollback()
        return (
            False,
            "；".join(str(item) for item in cross_errors),
            [item.as_dict() for item in cross_errors],
        )

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

def _resolve_base_name(config: WeeklyVersionConfig) -> str:
    name = str(config.name or "").strip()
    if " - " in name:
        return name.split(" - ", 1)[0]
    return name

def weekly_batch_configs(config: WeeklyVersionConfig) -> list:
    """一个周版本批次里的全部配置 —— **同项目 + 同窗口 + 同版本名**。

    三件事必须一起看，否则会把两个版本当成一个：

    * 「同项目 + 同窗口」是最初的判据，但它**不够**：同一窗口下可以有另一个名字的
      周版本配置（界面上完全建得出来，`weekly_version_config` 上没有任何唯一约束）。
    * 分组键 `build_weekly_group_key` 含**版本名** —— 平台自己就把「同窗口不同名」当成
      **两个版本**（两条 `AiWeeklyAnalysisState`、两次独立分析）。
    * 于是判据分了家：AI 的输入集按「项目 + 窗口」取，把两个版本的文件混进同一份清单，
      而报告标题写的是触发的那一个版本名。症状是报告写着 release-candidate，
      `delta_files` 里却有 unrelated-experiment 的文件（REV-AI-002）。

    **多仓库的批次不受影响**：它们由 `weekly_version_logic` 建成
    `f"{name} - {repository.name}"`，取版本名时按第一个 `" - "` 切开，所以
    「W1 - repoA / W1 - repoB」仍然在同一批里。

    调用方：`ai_analysis_service.build_weekly_payload`（输入集）、
    `weekly_sync_gate.group_config_ids`（同步闸门）、
    `weekly_version_logic.weekly_version_diff`（页面上的仓库标签页）—— 三处必须是
    同一份判据，各写一遍就会再次分家。
    """
    if config is None:
        return []
    base_name = _resolve_base_name(config)
    rows = (
        WeeklyVersionConfig.query.filter(
            WeeklyVersionConfig.project_id == config.project_id,
            WeeklyVersionConfig.start_time == config.start_time,
            WeeklyVersionConfig.end_time == config.end_time,
        )
        .order_by(WeeklyVersionConfig.repository_id.asc())
        .all()
    )
    return [row for row in rows if _resolve_base_name(row) == base_name]

def build_weekly_group_key(config: WeeklyVersionConfig) -> str:
    base_name = _resolve_base_name(config)
    start_key = config.start_time.strftime("%Y%m%d%H%M") if config.start_time else "unknown"
    end_key = config.end_time.strftime("%Y%m%d%H%M") if config.end_time else "unknown"
    safe_base = base_name.replace("|", "_")
    return f"{config.project_id}|{start_key}|{end_key}|{safe_base}"

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
