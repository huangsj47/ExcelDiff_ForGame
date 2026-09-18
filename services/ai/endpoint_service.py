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
    DEFAULT_MAX_ANOMALIES_PER_RUN,
    DEFAULT_MAX_FILES_PER_RUN,
    DEFAULT_MAX_TOOL_REQUESTS,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_SEVERITY,
    DEFAULT_PROMPT_CHAR_BUDGET,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_WEEKLY_INTERVAL_MINUTES,
    MAX_ANALYSIS_ROUNDS_RANGE,
    MAX_ANOMALIES_PER_RUN_RANGE,
    MAX_FILES_PER_RUN_RANGE,
    MAX_TOOL_REQUESTS_RANGE,
    PROMPT_CHAR_BUDGET_RANGE,
    REQUEST_TIMEOUT_RANGE,
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
    # `optional_*` 两种是**可为空**的：空 = 不限制（预算那一栏），见 _coerce_optional_*。
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
    # 标签改过：这个值**不再**限制「一次分析能处理多少个文件」（那由变更清单与
    # `MAX_LIST_CHARS` 决定），也不再限制「模型能读多少」（白名单给本批次全部改动文件）。
    # 它现在只在**清单长到列不下**的时候决定取样多少个 —— 旧的「单次最大文件数」
    # 会让用户以为调大它就能多看文件，而实际上绝大多数版本根本走不到这一支。
    "max_files_per_run": FieldRule("清单过长时的取样上限", "int", *MAX_FILES_PER_RUN_RANGE),
    "max_analysis_rounds": FieldRule("最大分析轮次", "int", *MAX_ANALYSIS_ROUNDS_RANGE),
    "max_tool_requests": FieldRule("上下文索取上限", "int", *MAX_TOOL_REQUESTS_RANGE),
    "prompt_char_budget": FieldRule("提示词字符预算", "int", *PROMPT_CHAR_BUDGET_RANGE),
    "request_timeout_seconds": FieldRule("单次请求超时（秒）", "int", *REQUEST_TIMEOUT_RANGE),
    "min_severity": FieldRule("严重度门槛", "choice", choices=SEVERITY_CHOICES),
    "min_confidence": FieldRule("置信度门槛", "choice", choices=CONFIDENCE_CHOICES),
    "max_anomalies_per_run": FieldRule(
        "单次异常上限", "int", *MAX_ANOMALIES_PER_RUN_RANGE
    ),
    # 这两栏是长文本，长度只做一个防呆上限。
    "prompt_template": FieldRule("项目补充指令", "text", max_length=20_000),
    # 单价表按 JSON 校验（kind="price_table"），见 `_coerce_price_table`：保存时就报错，
    # 而不是等到算费用时才发现——那时用户只看到「费用算不出来」，原因在几步之外。
    "model_price_table": FieldRule("模型单价表（JSON）", "price_table", max_length=20_000),
    "project_knowledge": FieldRule("项目补充知识", "text", max_length=20_000),
    # --- 预算闸门。**空 = 不限制**，所以用 optional_* 两种 kind：它们收空串/NULL，
    # 收成一个 `None`；用 kind="int" 的话「留空」会被判成「请填写一个整数」，
    # 用户就没法表达「不限制」这个意思了。
    "budget_period": FieldRule("预算周期", "choice", choices=BUDGET_PERIOD_CHOICES),
    "budget_token_limit": FieldRule(
        "周期内 token 上限", "optional_int", *BUDGET_TOKEN_LIMIT_RANGE
    ),
    "budget_cost_limit": FieldRule(
        "周期内费用上限", "optional_money", minimum=0, maximum=BUDGET_COST_LIMIT_MAX
    ),
}

# 界面上显示默认值时要用的值（与模型层的列默认值同源）。
FIELD_DEFAULTS: Mapping[str, Any] = {
    "api_base_url": "",
    "api_model": "",
    "auto_weekly_enabled": True,
    "weekly_interval_minutes": DEFAULT_WEEKLY_INTERVAL_MINUTES,
    "max_files_per_run": DEFAULT_MAX_FILES_PER_RUN,
    "max_analysis_rounds": DEFAULT_MAX_ANALYSIS_ROUNDS,
    "max_tool_requests": DEFAULT_MAX_TOOL_REQUESTS,
    "prompt_char_budget": DEFAULT_PROMPT_CHAR_BUDGET,
    "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT_SECONDS,
    "min_severity": DEFAULT_MIN_SEVERITY,
    "min_confidence": DEFAULT_MIN_CONFIDENCE,
    "max_anomalies_per_run": DEFAULT_MAX_ANOMALIES_PER_RUN,
    "prompt_template": "",
    "project_knowledge": "",
    "model_price_table": "",
    # 预算：`None` 是**有意义的取值**（不限制），不是「还没填」。界面据此留空输入框，
    # 而不是显示一个 0。
    "budget_period": DEFAULT_BUDGET_PERIOD,
    "budget_token_limit": DEFAULT_BUDGET_TOKEN_LIMIT,
    "budget_cost_limit": DEFAULT_BUDGET_COST_LIMIT,
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
    """可空整数上限。**空 = 不限制（`None`）**，非空则照 `_coerce_int` 那套范围校验。

    刻意不把空串当成 0：0 会被预算闸门读成「一个 token 都不许花」，于是「没配预算」
    与「不许花钱」变成同一件事 —— 而用户只是没填这一栏。
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
    factory: Callable[..., LLMClient] = LLMClient,
) -> LLMClient:
    """构造探测用的客户端。工厂可注入，单测因此不需要真网络。"""
    return factory(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
    )
