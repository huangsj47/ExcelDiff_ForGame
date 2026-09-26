"""连接配置：接口地址、模型名、凭据，以及「能不能连通」的实测。

## 为什么单独一个模块

这三件事原来散在 `ai_analysis_service.py` 里，而且都缺一半：新加的 `api_base_url` /
`api_model` 列**没有任何读写路径**，密钥用 Windows-only 的 DPAPI（Linux 部署直接抛异常，
而仓库其余部分早就换成跨平台的 Fernet 了）。

放在这里还有一层原因：范围校验、模型列表代理、连接测试都必须是**可注入依赖的纯逻辑**，
否则单测只能对着真实端点跑，而那种测试在 CI 上一定 skip，等于没测。

## 三条实测得到的事实（决定了本模块的写法）

1. **能用的模型名不一定出现在 `/models` 列表里。** 本机代理上 `deepseek-v4-flash` 可以
   正常对话，但 84 条模型列表里没有它（有 `deepseek-flash`、`deepseek-v4-flash-inhouse-yd`，
   就是没有这一个）。所以模型名必须**可自由填写**，列表只当建议 —— 做成只能选的
   `<select>` 会让用户选不到自己正在用的模型。
2. **端点不支持模型列表是常态**（403/404/405 或返回体不是那个形态）。这**不是错误**，
   不能阻断保存，要如实告诉用户「该端点不支持列表，请手动填写」。
3. **base_url 的写法五花八门**：可能填到 `/v1`，也可能连 `/chat/completions` 一起填进来。
   `llm_client.normalize_base_url` 负责归一化，这里不重复实现。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping

from models.ai_analysis.project_config import (
    BUDGET_COST_LIMIT_MAX,
    BUDGET_PERIOD_CHOICES,
    BUDGET_TOKEN_LIMIT_RANGE,
    CONFIDENCE_CHOICES,
    DEFAULT_BUDGET_COST_LIMIT,
    DEFAULT_BUDGET_PERIOD,
    DEFAULT_BUDGET_TOKEN_LIMIT,
    DEFAULT_MAX_ANALYSIS_ROUNDS,
    DEFAULT_AUTO_WEEKLY_ENABLED,
    DEFAULT_MAX_ANOMALIES_PER_RUN,
    DEFAULT_MAX_ANOMALIES_PER_SUBAGENT,
    DEFAULT_MAX_FILES_PER_RUN,
    DEFAULT_MAX_TOOL_REQUESTS,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_SEVERITY,
    DEFAULT_PROMPT_CACHE_FORMAT,
    DEFAULT_PROMPT_CACHE_MODE,
    DEFAULT_PROMPT_CHAR_BUDGET,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_SUBAGENT_COUNT,
    DEFAULT_SUBAGENT_ENABLED,
    DEFAULT_SUBAGENT_VERIFY,
    DEFAULT_WEEKLY_INTERVAL_MINUTES,
    PROMPT_CACHE_FORMAT_CHOICES,
    PROMPT_CACHE_MODE_CHOICES,
    PROMPT_CHAR_BUDGET_RANGE,
    SINGLE_RUN_TOKEN_LIMIT_RANGE,
    SEVERITY_CHOICES,
    WEEKLY_INTERVAL_RANGE,
)
from services.ai.pricing import parse_price_table
from services.ai.llm_client import (
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMResponseError,
    LLMTransportError,
    normalize_base_url,
)

# 服务来源的两个预设。`custom` 不预填任何东西 —— 预填一个假地址只会让人以为它是对的。
SOURCE_OPENAI = "openai"
SOURCE_CUSTOM = "custom"
SOURCES = (SOURCE_OPENAI, SOURCE_CUSTOM)
OPENAI_BASE_URL = "https://api.openai.com/v1"

# 模型列表与连接测试的独立超时。比分析用的超时短得多：用户在界面上等一个按钮，
# 30 秒已经很难受了，而分析本身跑几分钟是可以接受的。
PROBE_TIMEOUT_SECONDS = 30


# --------------------------------------------------------------------------
# 字段规则
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRule:
    """一个可配置字段的校验规则。

    `label` 是**给用户看的中文名**：报错信息里出现 `max_analysis_rounds` 这种列名，
    用户根本不知道说的是界面上哪一栏。
    """

    label: str
    # int / optional_int / optional_money / bool / text / url / choice / price_table
    # `optional_*` 两种是**可为空**的；解析成 None 后由读取侧决定使用安全默认或不限制。
    kind: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    max_length: int = 0


# 规则表同时被**服务端校验**与**前端提示文案**使用（前端通过配置接口读到它），
# 这样「界面上写着 1~30、后端却按别的范围夹」这类前后端不一致不会发生。
FIELD_RULES: Mapping[str, FieldRule] = {
    "api_base_url": FieldRule("接口地址", "url", max_length=500),
    "api_model": FieldRule("模型名字", "text", max_length=200),
    "auto_weekly_enabled": FieldRule("周版本自动分析", "bool"),
    "weekly_interval_minutes": FieldRule(
        "分析间隔（分钟）", "int", *WEEKLY_INTERVAL_RANGE
    ),
    # **这一栏不是「一次分析能花多少钱」** —— 它是**每轮请求装多少字**的水位
    # （超了才压历史）。2026-09-26 之前它顶着「预算」这个名字，实测就有用户
    # 按「钱」去调它。真正的单次预算是下面那一栏。
    "prompt_char_budget": FieldRule(
        "每轮提示词字符水位", "int", *PROMPT_CHAR_BUDGET_RANGE
    ),
    "single_run_token_limit": FieldRule(
        "单次分析预算（token）", "optional_int", *SINGLE_RUN_TOKEN_LIMIT_RANGE
    ),
    # 提示词缓存标记。这两栏**不猜端点**：`cache_control` 不是 OpenAI 协议的一部分，
    # 一个私有域名既可能是 Anthropic 兼容层，也可能是完全不认这个字段的转发器，
    # 而猜错的代价是一次 400。所以默认是「不发标记」（auto + none），要发的部署者
    # 声明他**知道**的那件事。见 services/ai/prompt_cache.py。
    "prompt_cache_mode": FieldRule(
        "提示词缓存断点", "choice", choices=PROMPT_CACHE_MODE_CHOICES
    ),
    "prompt_cache_format": FieldRule(
        "缓存标记约定", "choice", choices=PROMPT_CACHE_FORMAT_CHOICES
    ),
    # 子代理模式（2026-09，见 services/ai/subagent.py）。**默认关**：打开它会让一次分析
    # 的模型调用次数变成 (n+1) 倍，必须由人主动打开。只对**周版本**分析生效。
    # 分片数与每片的索取/轮次额度由平台按预算与本周规模自动推导（默认 5 片，
    # `services/ai/auto_sizing.py`），**不再可配**（见 RETIRED_FIELDS）。
    "subagent_enabled": FieldRule("子代理模式（仅周版本）", "bool"),
    # 对账轮：汇总之后再跑一次「找反证」。同样是**默认关**的额外一轮模型调用。
    "subagent_verify": FieldRule("对账轮（找反证，仅周版本）", "bool"),
    "min_severity": FieldRule("严重度门槛", "choice", choices=SEVERITY_CHOICES),
    "min_confidence": FieldRule("置信度门槛", "choice", choices=CONFIDENCE_CHOICES),
    # 这两栏是长文本，长度只做一个防呆上限。
    "prompt_template": FieldRule("项目补充指令", "text", max_length=20_000),
    # 单价表按 JSON 校验（kind="price_table"），见 `_coerce_price_table`：保存时就报错，
    # 而不是等到算费用时才发现——那时用户只看到「费用算不出来」，原因在几步之外。
    "model_price_table": FieldRule("模型单价表（JSON）", "price_table", max_length=20_000),
    "project_knowledge": FieldRule("项目补充知识", "text", max_length=20_000),
    # --- 预算闸门。字段允许清空，所以用 optional_* 两种 kind：它们收空串/NULL，
    # 收成一个 `None`；用 kind="int" 的话「留空」会被判成「请填写一个整数」，
    # token 空值在读取侧回落到 100M/月，费用空值表示不按金额限制。
    "budget_period": FieldRule("预算周期", "choice", choices=BUDGET_PERIOD_CHOICES),
    "budget_token_limit": FieldRule(
        "周期内 token 上限", "optional_int", *BUDGET_TOKEN_LIMIT_RANGE
    ),
    "budget_cost_limit": FieldRule(
        "周期内费用上限", "optional_money", minimum=0, maximum=BUDGET_COST_LIMIT_MAX
    ),
}

# 已收敛为平台自动推导的配置键：**收到即报字段级错误**，不静默忽略。
#
# 静默忽略会制造「存了但没生效」：老脚本或浏览器缓存的旧页面把这几个键发上来，
# 保存成功、用户以为改了参数 —— 而周版本路径已经不读它们（分片/索取/轮次由
# `services/ai/auto_sizing.py` 推导，超时/异常上限走平台常量）。报错而不是吞掉，
# 与 `_cross_field_errors` 的「不许静默」是同一条纪律。键名 → 给用户看的一句话。
RETIRED_FIELDS: Mapping[str, str] = {
    "max_files_per_run": "清单取样上限已改为平台按本周规模自动推导",
    "max_analysis_rounds": "最大分析轮次已改为平台按预算自动推导",
    "max_tool_requests": "上下文索取上限已改为平台按预算自动推导",
    "request_timeout_seconds": "单次请求超时已改为平台内置默认",
    "subagent_count": "分片数已改为平台按预算与维度自动推导（默认 5 片）",
    "max_anomalies_per_run": "单次异常上限已改为平台内置默认",
    "max_anomalies_per_subagent": "每个分片异常上限已改为平台自动推导",
}

# 界面上显示默认值时要用的值（与模型层的列默认值同源）。
#
# ★ 这里**必须引用常量**，不能再写一个字面量。`auto_weekly_enabled` 曾经在这份表里
# 写死 `True`，而模型层的常量是权威 —— 结果是「项目从没配过配置行」这条**最常见的路径**
# 走的是这份副本（`project_config_source.get_project_analysis_config` 在 `row is None`
# 时用 `dict(FIELD_DEFAULTS)`），于是改模型层的默认值对它**毫无影响**：
# 2026-09-22 把默认改成「关」之后，`tests/test_weekly_ai_auto_trigger_gate.py` 里
# 「没配过的项目」那条用例照样读出「开」——断言没红，因为真相在另一份副本里。
#
# ★ 注意 2026-09-23 配置面收敛之后，`RETIRED_FIELDS` 里的 7 个键**故意留在这里**：
# 模型层的列还在（单提交分析路径与 `resolved()` 继续读出厂默认），删掉这里的键会让
# 「无配置行」那条最常见路径返回的字典缺键 —— 消费方是 `project_config.get(key)`
# 还是 `project_config[key]` 没有逐一审过，缺键的 KeyError 比多一个不可配的默认值
# 危险得多。它们只是**不再出现在 FIELD_RULES**（界面不渲染、下发 schema 不含、
# 提交时被 `RETIRED_FIELDS` 拒掉），不再能被用户改到。
FIELD_DEFAULTS: Mapping[str, Any] = {
    "api_base_url": "",
    "api_model": "",
    "auto_weekly_enabled": DEFAULT_AUTO_WEEKLY_ENABLED,
    "weekly_interval_minutes": DEFAULT_WEEKLY_INTERVAL_MINUTES,
    "max_files_per_run": DEFAULT_MAX_FILES_PER_RUN,
    "max_analysis_rounds": DEFAULT_MAX_ANALYSIS_ROUNDS,
    "max_tool_requests": DEFAULT_MAX_TOOL_REQUESTS,
    "prompt_char_budget": DEFAULT_PROMPT_CHAR_BUDGET,
    "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT_SECONDS,
    "prompt_cache_mode": DEFAULT_PROMPT_CACHE_MODE,
    "prompt_cache_format": DEFAULT_PROMPT_CACHE_FORMAT,
    "min_severity": DEFAULT_MIN_SEVERITY,
    "min_confidence": DEFAULT_MIN_CONFIDENCE,
    "max_anomalies_per_run": DEFAULT_MAX_ANOMALIES_PER_RUN,
    "max_anomalies_per_subagent": DEFAULT_MAX_ANOMALIES_PER_SUBAGENT,
    "subagent_enabled": DEFAULT_SUBAGENT_ENABLED,
    "subagent_count": DEFAULT_SUBAGENT_COUNT,
    "subagent_verify": DEFAULT_SUBAGENT_VERIFY,
    "prompt_template": "",
    "project_knowledge": "",
    "model_price_table": "",
    # token 默认显示 100M/月安全额度；费用 None 表示不按金额限制。
    "budget_period": DEFAULT_BUDGET_PERIOD,
    "budget_token_limit": DEFAULT_BUDGET_TOKEN_LIMIT,
    "budget_cost_limit": DEFAULT_BUDGET_COST_LIMIT,
    # 单次分析预算：平台初值不是一个固定数（按模式取：单代理 3M / 家族 8M，见
    # `auto_sizing`），所以这里给 `None` = 「没配，用平台算出来的那个数」。
    "single_run_token_limit": None,
}


@dataclass
class FieldError(Exception):
    """单个字段的校验失败。

    **必须真的继承 Exception** —— 第一版把它写成了 frozen dataclass（只是个值对象），
    然后到处 `raise FieldError(...)`，运行期报的是 `TypeError: exceptions must derive
    from BaseException`，而且 `except FieldError` 也没法捕获。冒烟脚本第一次调用就炸了。

    另外注意 dataclass 生成的 `__init__` 会覆盖 `Exception.__init__`，于是 `args` 是空的、
    `str(exc)` 会返回空串。日志里需要可读信息，所以显式实现 `__str__`。
    """

    field: str
    label: str
    message: str

    def __str__(self) -> str:
        return f"{self.label}：{self.message}"

    def as_dict(self) -> dict:
        return {"field": self.field, "label": self.label, "message": self.message}


class ConfigValidationError(ValueError):
    """配置校验失败。带字段级明细，供界面逐项标红。"""

    def __init__(self, errors: Iterable[FieldError]):
        self.errors = tuple(errors)
        super().__init__("；".join(f"{item.label}：{item.message}" for item in self.errors))


def _coerce_int(field_name: str, rule: FieldRule, raw: Any) -> int:
    """读整数。**越界就报错，不静默夹到边界内**。

    原来那版是 `_clamp_int`：用户填 5000、界面回填 1000，中间没有任何提示，用户以为
    自己填的是 5000。要改的是「告诉他范围是多少」，不是悄悄改掉他的值。
    """
    if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
        raise FieldError(field_name, rule.label, "请填写一个整数")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise FieldError(field_name, rule.label, f"「{raw}」不是整数") from None
    if rule.minimum is not None and value < rule.minimum:
        raise FieldError(
            field_name, rule.label, f"不能小于 {rule.minimum}（当前填的是 {value}）"
        )
    if rule.maximum is not None and value > rule.maximum:
        raise FieldError(
            field_name, rule.label, f"不能大于 {rule.maximum}（当前填的是 {value}）"
        )
    return value


def _coerce_optional_int(field_name: str, rule: FieldRule, raw: Any) -> int | None:
    """可空整数上限。空值先存为 `None`，非空则照 `_coerce_int` 校验。

    项目 token 的读取侧会把 `None` 解析为 100M/月；平台 token 的读取侧仍可解释为不限制。
    刻意不把空串当成 0，因为 0 会把分析完全锁死。
    """
    if raw is None or (not isinstance(raw, bool) and str(raw).strip() == ""):
        return None
    return _coerce_int(field_name, rule, raw)


def _coerce_optional_money(field_name: str, rule: FieldRule, raw: Any) -> str | None:
    """可空金额上限。空 = 不限制；非空必须是**非负**的十进制数。

    金额存成字符串而不是 float：费用那一整套（`services/ai/pricing.py`）用 `Decimal`
    算，存 float 会在这里埋一个二进制浮点误差。负数直接报错 —— 「-1 元上限」在任何
    解释下都不成立，而闸门会把它当成「已经超了」，静默锁死分析。
    """
    if raw is None or (not isinstance(raw, bool) and str(raw).strip() == ""):
        return None
    if isinstance(raw, bool):
        raise FieldError(field_name, rule.label, "请填写一个数字")
    text = str(raw).strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise FieldError(field_name, rule.label, f"「{raw}」不是数字") from None
    if not number.is_finite():
        raise FieldError(field_name, rule.label, f"「{raw}」不是有效数字")
    if number <= 0:
        # 0 与负数一样挡掉：0 是一个「一毛钱都不许花」的上限，等价于把 AI 分析关掉，
        # 而用户想表达的通常是「不限制」—— 那应该是**留空**，不是填 0。
        raise FieldError(
            field_name, rule.label, f"必须大于 0（当前填的是 {text}）；留空表示不限制"
        )
    if rule.maximum is not None and number > Decimal(rule.maximum):
        raise FieldError(
            field_name, rule.label, f"不能大于 {rule.maximum}（当前填的是 {text}）"
        )
    # 去掉尾随 0 之外的任何改写都不做：用户填 12.50 就存 12.50，回显时看到的还是他填的。
    return format(number, "f")


def _coerce_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "是", "启用"}


def _coerce_text(field_name: str, rule: FieldRule, raw: Any) -> str:
    text = "" if raw is None else str(raw).strip()
    if rule.max_length and len(text) > rule.max_length:
        raise FieldError(
            field_name, rule.label, f"太长了（{len(text)} 字，上限 {rule.max_length}）"
        )
    return text


def _coerce_price_table(field_name: str, rule: FieldRule, raw: Any) -> str:
    """单价表：空串表示「用平台默认表」，非空则必须是**能解析**的 JSON 表。

    保存时就校验，而不是等到算费用时才发现。后者的症状是面板上「费用算不出来」，
    而真正的原因（JSON 里少了个逗号、键名打错）在几步之外，且没有任何提示指向配置。
    错误信息直接用解析器给的那几条 —— 它会指出**哪个键**有问题，比一句「格式不对」有用。

    校验通过后**原样存用户输入的文本**（不做 JSON 重排）：界面上回显的就是他写的样子，
    免得他以为自己填的东西被改过。
    """
    text = _coerce_text(field_name, rule, raw)
    if not text:
        return ""
    table, errors = parse_price_table(text)
    if table is None:
        raise FieldError(field_name, rule.label, "；".join(errors))
    return text


def _coerce_url(field_name: str, rule: FieldRule, raw: Any) -> str:
    text = _coerce_text(field_name, rule, raw)
    if not text:
        return ""
    try:
        return normalize_base_url(text)
    except LLMConfigError as exc:
        raise FieldError(field_name, rule.label, str(exc)) from None


def _coerce_choice(field_name: str, rule: FieldRule, raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text not in rule.choices:
        raise FieldError(
            field_name, rule.label, f"只能是 {' / '.join(rule.choices)} 之一"
        )
    return text


def validate_field(field_name: str, raw: Any) -> Any:
    """校验并规范化单个字段。"""
    rule = FIELD_RULES.get(field_name)
    if rule is None:
        raise FieldError(field_name, field_name, "不是可配置的字段")
    if rule.kind == "int":
        return _coerce_int(field_name, rule, raw)
    if rule.kind == "optional_int":
        return _coerce_optional_int(field_name, rule, raw)
    if rule.kind == "optional_money":
        return _coerce_optional_money(field_name, rule, raw)
    if rule.kind == "bool":
        return _coerce_bool(raw)
    if rule.kind == "url":
        return _coerce_url(field_name, rule, raw)
    if rule.kind == "choice":
        return _coerce_choice(field_name, rule, raw)
    if rule.kind == "price_table":
        return _coerce_price_table(field_name, rule, raw)
    return _coerce_text(field_name, rule, raw)


def validate_payload(payload: Mapping[str, Any]) -> dict:
    """校验整个提交体，返回「规范化后的、只含被提交字段」的字典。

    只处理提交里出现的键：界面可能只改一栏（例如只换模型名），要求它把全部字段都带上
    是没必要的负担，也容易在并发编辑时互相覆盖。

    **收集全部错误再抛**，而不是遇到第一个就返回 —— 一次把所有问题标红，用户改一轮
    就好；逐个报会让他改五遍。

    根节点不是对象时**也走同一条出口**（`ConfigValidationError` → 路由回 400）：服务层
    还会被脚本、后台任务直接调用，`dict(payload or {})` 在那里抛的是 TypeError，
    调用方拿到的是一个 500 而不是结构化错误。
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise ConfigValidationError(
            [FieldError("__body__", "请求体", "必须是 JSON 对象")]
        )

    errors: list[FieldError] = []
    normalized: dict[str, Any] = {}
    for field_name, raw in dict(payload).items():
        if field_name in RETIRED_FIELDS:
            # 收敛键：**收到即报错**。走「未知字段忽略」那支的话，老脚本把
            # `subagent_count` 发上来会保存成功，用户以为改了分片数 ——
            # 而它早就不被读了（见 RETIRED_FIELDS 注释）。
            errors.append(
                FieldError(
                    field_name,
                    field_name,
                    f"这一项已改为平台自动推导，不再接受手动配置（{RETIRED_FIELDS[field_name]}）",
                )
            )
            continue
        if field_name not in FIELD_RULES:
            continue  # 未知字段忽略，不报错（前端可能带上别的 state）
        try:
            normalized[field_name] = validate_field(field_name, raw)
        except FieldError as exc:
            errors.append(exc)
    if errors:
        raise ConfigValidationError(errors)
    return normalized


def validate_endpoint_ready(config: Mapping[str, Any], *, has_key: bool) -> list[FieldError]:
    """跑分析之前必须具备的条件。

    与「保存配置」分开：允许先把模型名存下来、稍后再填 token（分步配置是常态），
    但要跑分析时必须给出**明确说清缺什么**的提示，而不是发一个必然失败的请求。
    """
    errors: list[FieldError] = []
    if not str(config.get("api_base_url") or "").strip():
        errors.append(FieldError("api_base_url", FIELD_RULES["api_base_url"].label, "还没有配置接口地址"))
    if not str(config.get("api_model") or "").strip():
        errors.append(FieldError("api_model", FIELD_RULES["api_model"].label, "还没有配置模型名字"))
    if not has_key:
        errors.append(FieldError("api_key", "API Token", "还没有配置 Token"))
    return errors


def describe_field_schema() -> dict:
    """把规则表序列化成前端可用的结构（标签、类型、范围、选项、默认值）。

    前端**从接口读它**来渲染 `min`/`max` 与提示文案，而不是在模板里写死 —— 否则就会
    出现「界面写着 1~30、后端按别的范围校验」这类前后端不一致，而这正是本次要修的东西。
    """
    return {
        name: {
            "label": rule.label,
            "kind": rule.kind,
            "min": rule.minimum,
            "max": rule.maximum,
            "choices": list(rule.choices),
            "max_length": rule.max_length,
            "default": FIELD_DEFAULTS.get(name),
        }
        for name, rule in FIELD_RULES.items()
    }


def source_of(base_url: str) -> str:
    """判断当前地址属于哪个来源，用于界面回显选中哪个单选项。"""
    normalized = normalize_base_url(base_url) if base_url else ""
    if not normalized:
        return SOURCE_OPENAI
    return SOURCE_OPENAI if normalized == normalize_base_url(OPENAI_BASE_URL) else SOURCE_CUSTOM


# --------------------------------------------------------------------------
# 模型列表与连接测试
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """一次探测的结果。

    `supported=False` 与 `ok=False` 是**两件不同的事**：前者是「这个端点本来就没有
    列表接口」（正常，不是错误，引导手填），后者是「真的失败了」（网络、鉴权）。
    把它们混成一个 ok 位，界面就只能说「获取失败」，而用户不知道是该重试还是该手填。
    """

    ok: bool
    supported: bool = True
    message: str = ""
    models: tuple[str, ...] = ()
    detail: str = ""
    latency_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "supported": self.supported,
            "message": self.message,
            "models": list(self.models),
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


def probe_models(client: LLMClient, *, limit: int = 500) -> ProbeResult:
    """取可用模型列表。

    失败一律**降级成「不支持列表 + 手填指引」**，不抛出：取不到列表不该阻断任何操作，
    因为模型名本来就允许手填（而且实测存在「能用但不在列表里」的模型）。
    """
    started = time.monotonic()
    try:
        models = client.list_models()
    except LLMConfigError as exc:
        # 地址/鉴权配置本身有问题 —— 这类要说全，用户改地址就能解决。
        return ProbeResult(
            ok=False,
            supported=False,
            message="该端点不支持获取模型列表，请手动填写模型名字。",
            detail=str(exc),
            latency_ms=_elapsed_ms(started),
        )
    except (LLMTransportError, LLMResponseError, LLMError) as exc:
        return ProbeResult(
            ok=False,
            supported=False,
            message="无法获取模型列表（端点未响应或返回格式不同），请手动填写模型名字。",
            detail=str(exc),
            latency_ms=_elapsed_ms(started),
        )

    # 先 strip 再收集：端点返回 `" c "` 这种带空格的名字时，直接塞进 datalist 会让用户
    # 选中一个带空格的名字，随后请求里的 model 就是 `" c "`，模型匹配不上。
    # 第一版写成 `{str(item) for item in models if str(item).strip()}` —— 判空用了 strip、
    # 取值却没 strip，带空格的名字原样留了下来。
    cleaned = {str(item).strip() for item in models}
    ordered = tuple(sorted(name for name in cleaned if name)[:limit])
    if not ordered:
        return ProbeResult(
            ok=False,
            supported=False,
            message="端点返回了空的模型列表，请手动填写模型名字。",
            latency_ms=_elapsed_ms(started),
        )
    return ProbeResult(
        ok=True,
        supported=True,
        message=f"已获取 {len(ordered)} 个可用模型。",
        models=ordered,
        latency_ms=_elapsed_ms(started),
    )


def probe_connection(client: LLMClient) -> ProbeResult:
    """发一次最小对话，验证「地址 + Token + 模型名」这一组能不能真的用。

    这是「自定义端点」能不能落地的关键：用户填完必须能立刻知道结果，而不是等到跑一次
    完整分析（几分钟、几千 token）才发现模型名写错了。

    用 `max_tokens` 之外不传任何多余参数，温度 0，问一个不需要思考的问题 —— 探测本身
    要尽量便宜、尽量确定。
    """
    started = time.monotonic()
    try:
        result = client.complete(
            [{"role": "user", "content": "只回复两个字：收到"}],
            temperature=0,
        )
    except LLMError as exc:
        return ProbeResult(
            ok=False,
            message=f"连接失败：{exc}",
            detail=type(exc).__name__,
            latency_ms=_elapsed_ms(started),
        )

    if not str(result.text or "").strip():
        return ProbeResult(
            ok=False,
            message="端点有响应，但返回内容是空的（可能是模型名不被支持，或该模型只返回推理内容）。",
            latency_ms=_elapsed_ms(started),
        )
    return ProbeResult(
        ok=True,
        message=f"连接成功，模型 {result.model or client.model} 响应正常。",
        models=(str(result.model or client.model),),
        latency_ms=_elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def build_probe_client(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    prompt_cache_mode: str = DEFAULT_PROMPT_CACHE_MODE,
    prompt_cache_format: str = DEFAULT_PROMPT_CACHE_FORMAT,
    factory: Callable[..., LLMClient] = LLMClient,
) -> LLMClient:
    """构造探测用的客户端。工厂可注入，单测因此不需要真网络。

    `prompt_cache_mode` / `prompt_cache_format` 从这里一路传进 `LLMClient`。默认值是
    「不发标记」，而**探测请求本来就不会带标记**：标记是挂在消息上的（见
    `prompt_cache.mark_cache_breakpoint`），只有编排层知道断点该放哪，探测只发一两句
    短消息。所以这两个参数defaulted 与否不影响探测的行为，留着是为了让「测试连接」
    与「正式分析」走**同一段构造代码** —— 两条路径的差异只该在传入的值里。
    """
    return factory(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        prompt_cache_mode=prompt_cache_mode,
        prompt_cache_format=prompt_cache_format,
    )
