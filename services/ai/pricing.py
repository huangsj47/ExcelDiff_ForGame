"""模型单价与费用估算。

## 为什么单独一个模块

费用**只能自己算**：OpenAI 兼容的 `/chat/completions` 响应里没有金额，只有 token 数
（`services/ai/llm_client.py` 的 `_extract_usage` / `_extract_cache_usage`）。所以「这个
版本花了多少钱」这件事，依赖一张我们自己维护的单价表。

## 三条纪律

1. **默认表是空的，不预置任何单价。** 编出来的单价会被当成真钱看 —— 面板上显示
   「¥3.42」时没人会去想这个数是怎么来的。宁可先显示「还没有配置价格表」，也不要显示
   一个我无法核实的数字。要算费用就在项目的 AI 分析配置里填（见 `_DOC_SHAPE`），或改
   这里的 `DEFAULT_PRICE_TABLE` 给全平台一份默认值。
2. **分三档计价：未命中输入的、命中缓存的、输出的。** 这正是 prompt cache 省钱的地方；
   把命中输入按未命中价算会让「缓存命中率 90%」这个数字在费用上完全体现不出来。
3. **算不出就返回 `None` + 一句人话理由**，绝不返回 0 或部分金额。`0` 是「这次没花钱」
   这个确定的结论，和「算不出来」是两件事，面板上必须能分开。

## 单价单位

**每 100 万 token**，与各家价目表的写法一致。金额用 `Decimal` 计算（float 累加会在
合计里出现 `0.30000000000000004` 那类误差），序列化成字符串交给前端展示，前端不做算术。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping

# 默认表的版本号。**改 DEFAULT_PRICE_TABLE 必须同步改这里** —— 它会随每次运行落进
# `ai_analysis_run.pricing_version`，用来回答「这条费用是按哪版单价算的」。
DEFAULT_PRICE_TABLE_VERSION = "unset"

# 金额与单价的默认币种。只做展示，不换算。
DEFAULT_CURRENCY = "CNY"

# 每百万 token 的单价。键是**模型名模式**，见 resolve_price() 的匹配规则。
#
# 刻意留空（见模块 docstring 第 1 条）。填的时候照这个形状：
#   "deepseek-chat": {"input": 2.0, "output": 8.0, "cache_read": 0.2},
#   "my-gateway-model-*": {"input": 1.0, "output": 2.0, "note": "内部价，2026-09 核"},
# 缓存命中价（cache_read）不填时：命中那部分按未命中价计入，并在结果里注明「偏保守」。
DEFAULT_PRICE_TABLE: dict[str, Any] = {
    "currency": DEFAULT_CURRENCY,
    "models": {},
}

_DOC_SHAPE = """{
  "version": "2026-09-18",
  "currency": "CNY",
  "models": {
    "deepseek-chat":  {"input": 2.0, "output": 8.0, "cache_read": 0.2},
    "gateway-model-*": {"input": 1.0, "output": 2.0}
  }
}"""

# 每个模型条目允许出现的键。**写错键名要报错**，不能忽略 —— 把 "input" 打成 "inpu"
# 会让那一档静默按 0 计价，算出来的费用看起来完全正常。
_ALLOWED_MODEL_KEYS = {"input", "output", "cache_read", "cache_write", "currency", "note"}
_ALLOWED_TABLE_KEYS = {"version", "currency", "models"}


@dataclass(frozen=True)
class ModelPrice:
    """一个模型的三档单价（每 100 万 token）。"""

    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None
    currency: str = DEFAULT_CURRENCY
    note: str = ""


@dataclass(frozen=True)
class PriceTable:
    """一份可用的单价表，外加它的来源。"""

    models: Mapping[str, ModelPrice]
    version: str = DEFAULT_PRICE_TABLE_VERSION
    currency: str = DEFAULT_CURRENCY
    source: str = "default"  # default | project

    def entries(self) -> tuple[dict[str, Any], ...]:
        """给界面展示用（不含 Decimal，数字转成字符串）。"""
        return tuple(
            {
                "pattern": pattern,
                "input": money(price.input_per_million),
                "output": money(price.output_per_million),
                "cache_read": money(price.cache_read_per_million),
                "cache_write": money(price.cache_write_per_million),
                "currency": price.currency,
                "note": price.note,
            }
            for pattern, price in sorted(self.models.items())
        )


@dataclass(frozen=True)
class CostLine:
    """费用的一项：哪一档、多少 token、单价多少、算出来多少。"""

    label: str
    tokens: int
    unit_price: Decimal | None
    amount: Decimal | None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "tokens": self.tokens,
            "unit_price": money(self.unit_price),
            "amount": money(self.amount),
            "note": self.note,
        }


@dataclass(frozen=True)
class CostEstimate:
    """一次费用估算的结果。

    `amount is None` 表示**算不出**，此时 `reason` 一定是一句可以直接显示给用户的中文。
    `lines` 即使算得出也可能带 `note`（例如「缓存命中价未配置，按未命中价计入，偏保守」）。
    """

    amount: Decimal | None = None
    currency: str = ""
    reason: str = ""
    price_version: str = ""
    matched_pattern: str = ""
    source: str = ""
    lines: tuple[CostLine, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def computable(self) -> bool:
        return self.amount is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount": money(self.amount),
            "currency": self.currency,
            "reason": self.reason,
            "price_version": self.price_version,
            "matched_pattern": self.matched_pattern,
            "source": self.source,
            "lines": [line.to_dict() for line in self.lines],
            "notes": list(self.notes),
        }


# 金额的展示精度。**2 位小数，全平台统一。**
#
# 在这之前是「量化到 6 位再去掉尾随 0」，于是同一个面板上会同时出现 `¥86.40`、
# `¥0.001980`、`¥2` 三种形态：读的人得先数小数点后几位才知道这个数有多大，而费用恰恰是
# 拿来横向比的一列。统一到分之后，「¥86.40 / ¥0.00 / ¥2.00」一眼可比。
#
# 金额的**计算**不受这里影响：算的是未舍入的 Decimal，只有展示这一步量化
# （见 `estimate_cost` / `usage.aggregate_runs`）。所以「每条都显示 0.00、合计却是 3.00」
# 这种正常的四舍五入是可能的，而它比「显示 6 位小数」更符合钱的读法。
MONEY_QUANTUM = Decimal("0.01")
# 非零但不足一分钱的金额怎么显示。
#
# **不显示 `0.00`**：那是「这次没花钱」这个确定的结论，而本模块从头到尾都在守
# 「0 与算不出是两件事」这条口径（见模块 docstring 第 3 条）。一次几厘钱的运行显示成
# `0.00`，读的人会以为它免费 —— 而它确实花了钱，只是不足一分。所以给一个明确的
# 「小于一分」，既保住了 2 位小数的统一读法，又不撒谎。
MONEY_BELOW_ONE_CENT = "<0.01"


def money(value: Decimal | None) -> str | None:
    """金额 → 字符串（**定点 2 位小数**）。前端只展示、不做算术，所以走字符串。

    三条口径：

    * **不用 `normalize()`**：它会把 `10.00` 变成 `1E+1`，前端拿到的就是「1E+1 元」。
    * **不用科学计数法**：一律 `format(..., "f")`。
    * **非零但不足一分** → `"<0.01"`（见 `MONEY_BELOW_ONE_CENT`）。`0` 进 `0` 出。

    读不出数字（脏数据）时返回 `None`，与「金额是 `None`」同一个出口：界面会把
    `None` 显示成「未配置 / 算不出」，而一个 `NaN` 或 `1E+1` 会直接被当成金额渲染。
    """
    if value is None:
        return None
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None

    rounded = amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    if rounded == 0 and amount != 0:
        return MONEY_BELOW_ONE_CENT
    return format(rounded, "f")


def _exact_text(value: Decimal | None) -> str | None:
    """**不做展示舍入**的金额文本，只给「两个单价表是不是同一份」这类判等用。

    展示走 `money()`（2 位小数），但判等不能跟着它走：`2.0000001` 与 `2.0000002` 在
    展示上都是 `2.00`，而 `price_change_requires_version_bump` 正是靠逐档比对这个签名
    来抓「改了单价却没改 version」。用展示值判等等于给那条规矩开了一个静默的后门 ——
    改价改到小数点后第三位就不再要求升版本了。
    """
    if value is None:
        return None
    if not value.is_finite():
        return None
    text = format(value.quantize(Decimal("0.000001")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _decimal(value: Any) -> Decimal | None:
    """JSON 里的数 → Decimal。拒绝 bool（`True` 不是 1）与负数。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def parse_price_table(raw: str | None) -> tuple[PriceTable | None, tuple[str, ...]]:
    """把项目配置里那串 JSON 解析成单价表。

    **解析失败返回 `(None, 错误)`，不静默回落到默认表** —— 静默回落等于「用户以为改了价
    格、实际还是旧的」，那种错误没有任何提示，只会让人对着一个不对的费用数字找原因。
    """
    text = (raw or "").strip()
    if not text:
        return None, ("价格表为空",)

    try:
        payload = json.loads(text)
    except ValueError as exc:
        return None, (f"不是合法 JSON：{exc}",)

    if not isinstance(payload, dict):
        return None, ("价格表的最外层必须是一个对象",)

    unknown_table_keys = sorted(set(payload) - _ALLOWED_TABLE_KEYS)
    if unknown_table_keys:
        return None, (f"表里有多余的键：{'、'.join(unknown_table_keys)}",)

    raw_models = payload.get("models")
    if not isinstance(raw_models, dict):
        return None, ("缺少 models 对象（格式示例见配置界面的说明）",)
    if not raw_models:
        return None, ("models 是空的，没有任何单价可算",)

    table_currency = str(payload.get("currency") or DEFAULT_CURRENCY).strip() or DEFAULT_CURRENCY
    version = str(payload.get("version") or "").strip()
    if not version:
        return None, ("缺少 version（改价格时必须改它，否则无法追溯历史费用是按哪版算的）",)

    models: dict[str, ModelPrice] = {}
    errors: list[str] = []
    for pattern, entry in raw_models.items():
        name = str(pattern).strip()
        if not name:
            errors.append("有一个空白的模型名模式")
            continue
        if not isinstance(entry, dict):
            errors.append(f"`{name}` 的取值必须是一个对象")
            continue
        unknown = sorted(set(entry) - _ALLOWED_MODEL_KEYS)
        if unknown:
            errors.append(f"`{name}` 里有多余的键：{'、'.join(unknown)}")
            continue

        input_price = _decimal(entry.get("input"))
        output_price = _decimal(entry.get("output"))
        if input_price is None or output_price is None:
            errors.append(f"`{name}` 必须给出非负的 input 与 output 单价")
            continue

        cache_read = _decimal(entry.get("cache_read"))
        cache_write = _decimal(entry.get("cache_write"))
        currency = str(entry.get("currency") or table_currency).strip() or table_currency
        if currency != table_currency:
            errors.append(f"`{name}` 的币种（{currency}）与表头的币种（{table_currency}）不一致")
            continue
        # 命中缓存的单价高于未命中，几乎一定是填反了。这种错误算出来的费用看起来
        # 「有零有整」，不会有人怀疑，所以挡在这里。
        if cache_read is not None and cache_read > input_price:
            errors.append(f"`{name}` 的 cache_read 单价高于 input，疑似填反了")
            continue

        models[name] = ModelPrice(
            input_per_million=input_price,
            output_per_million=output_price,
            cache_read_per_million=cache_read,
            cache_write_per_million=cache_write,
            currency=currency,
            note=str(entry.get("note") or "").strip(),
        )

    if errors:
        return None, tuple(errors)
    return PriceTable(models=models, version=version, currency=table_currency, source="project"), ()


def default_price_table() -> PriceTable:
    """平台默认表（可能是空的 —— 空的表示「还没配」，不是「免费」）。"""
    raw = dict(DEFAULT_PRICE_TABLE)
    raw.setdefault("version", DEFAULT_PRICE_TABLE_VERSION)
    models: dict[str, ModelPrice] = {}
    for pattern, entry in (raw.get("models") or {}).items():
        price = _decimal(entry.get("input"))
        output = _decimal(entry.get("output"))
        if price is None or output is None:
            continue
        models[str(pattern)] = ModelPrice(
            input_per_million=price,
            output_per_million=output,
            cache_read_per_million=_decimal(entry.get("cache_read")),
            cache_write_per_million=_decimal(entry.get("cache_write")),
            currency=str(entry.get("currency") or raw.get("currency") or DEFAULT_CURRENCY),
            note=str(entry.get("note") or ""),
        )
    return PriceTable(
        models=models,
        version=str(raw.get("version") or DEFAULT_PRICE_TABLE_VERSION),
        currency=str(raw.get("currency") or DEFAULT_CURRENCY),
        source="default",
    )


def load_price_table(override_json: str | None) -> tuple[PriceTable | None, tuple[str, ...]]:
    """项目覆盖优先，空则用平台默认表。返回 `(表, 错误)`；`table is None` 表示不可用。"""
    if (override_json or "").strip():
        return parse_price_table(override_json)
    table = default_price_table()
    return table, ()


def resolve_price(model: str, table: PriceTable) -> tuple[ModelPrice | None, str]:
    """按模型名找单价，返回 `(单价, 命中的模式)`；找不到返回 `(None, "")`。

    规则（顺序固定）：
    1. 归一化（去空白 + 小写）后**精确匹配**；
    2. 否则**最长前缀匹配**：只有以 `*` 结尾的模式参与（`deepseek-v4-*` 命中
       `deepseek-v4-flash-inhouse-yd` —— 网关给模型名加后缀是常态）；
    3. **没有「包含」匹配**：那会让 `gpt-4` 命中 `gpt-4o`，价格差一个量级却毫无提示。
    """
    wanted = str(model or "").strip().lower()
    if not wanted:
        return None, ""

    exact = {str(key).strip().lower(): key for key in table.models}
    if wanted in exact:
        return table.models[exact[wanted]], exact[wanted]

    best_pattern = ""
    best_price: ModelPrice | None = None
    for key, price in table.models.items():
        normalized = str(key).strip().lower()
        if not normalized.endswith("*"):
            continue
        prefix = normalized[:-1]
        if prefix and wanted.startswith(prefix) and len(prefix) > len(best_pattern):
            best_pattern, best_price = prefix, price
    if best_price is not None:
        return best_price, f"{best_pattern}*"
    return None, ""


def estimate_cost(
    model: str,
    *,
    tokens_input: int | None,
    tokens_output: int | None,
    cache_read: int | None = None,
    cache_write: int | None = None,
    table: PriceTable | None,
) -> CostEstimate:
    """按单价表算一次运行的费用。算不出时 `amount is None` 且 `reason` 可直接显示。

    `tokens_input` 是**输入总数**（含命中缓存的部分） —— OpenAI 与 DeepSeek 的
    `prompt_tokens` 都是这个口径，所以「未命中」那一档要减掉 `cache_read`。
    """
    def _uncomputable(reason: str) -> CostEstimate:
        return CostEstimate(
            reason=reason,
            price_version=(table.version if table else ""),
            source=(table.source if table else ""),
        )

    if table is None:
        return _uncomputable("还没有配置价格表")
    if not table.models:
        return _uncomputable("还没有配置价格表")
    if tokens_input is None or tokens_output is None:
        return _uncomputable("上游没有返回 token 数，无法估算")

    price, pattern = resolve_price(model, table)
    if price is None:
        return _uncomputable(f"价格表里没有匹配 `{model or '（未配置模型）'}` 的条目")

    notes: list[str] = []
    hit = cache_read if cache_read is not None else 0
    miss = tokens_input
    if cache_read is None:
        if price.cache_read_per_million is not None:
            notes.append("上游未上报缓存命中数，输入全部按未命中价计算")
    else:
        if cache_read > tokens_input:
            # 上游字段自相矛盾。按「全部命中」算，但要说出来 —— 悄悄 clamp 会让人
            # 以为上游数据是干净的。
            notes.append("上游给的缓存命中数大于输入总数，已按全部命中计算")
            hit = tokens_input
        miss = tokens_input - hit

    miss_price = price.input_per_million
    hit_price = price.cache_read_per_million
    if hit > 0 and hit_price is None:
        hit_price = miss_price
        notes.append("价格表没给缓存命中价，命中部分按未命中价计入（费用偏保守）")

    lines: list[CostLine] = [
        CostLine(
            label="输入（未命中缓存）",
            tokens=miss,
            unit_price=miss_price,
            amount=miss_price * miss / Decimal(1_000_000),
        )
    ]
    if hit > 0:
        lines.append(
            CostLine(
                label="输入（命中缓存）",
                tokens=hit,
                unit_price=hit_price,
                amount=(hit_price or Decimal(0)) * hit / Decimal(1_000_000),
            )
        )
    if cache_write:
        write_price = price.cache_write_per_million
        if write_price is None:
            notes.append("上游上报了缓存写入 token，但价格表没有对应单价，未计入")
        else:
            lines.append(
                CostLine(
                    label="输入（写入缓存）",
                    tokens=cache_write,
                    unit_price=write_price,
                    amount=write_price * cache_write / Decimal(1_000_000),
                )
            )
    lines.append(
        CostLine(
            label="输出",
            tokens=tokens_output,
            unit_price=price.output_per_million,
            amount=price.output_per_million * tokens_output / Decimal(1_000_000),
        )
    )

    total = sum((line.amount or Decimal(0) for line in lines), Decimal(0))
    return CostEstimate(
        amount=total,
        currency=price.currency,
        price_version=table.version,
        matched_pattern=pattern,
        source=table.source,
        lines=tuple(lines),
        notes=tuple(notes),
    )


def price_table_doc_shape() -> str:
    """配置界面要展示的格式示例。"""
    return _DOC_SHAPE


def _model_signature(table: PriceTable) -> dict[str, tuple[str, str, str, str]]:
    """单价表里**真正决定金额**的那部分：模式名 → 三（四）档单价。

    只取单价，不取 `note`：给一条模型加一句备注不会让历史费用对不上，而改一个数字会。

    **用 `_exact_text` 而不是 `money()`**：展示精度是 2 位小数，而判等要能分辨
    `2.0000001` 与 `2.0000002`。理由见 `_exact_text` 的 docstring。
    """
    return {
        str(pattern): (
            _exact_text(price.input_per_million) or "",
            _exact_text(price.output_per_million) or "",
            _exact_text(price.cache_read_per_million) or "",
            _exact_text(price.cache_write_per_million) or "",
        )
        for pattern, price in table.models.items()
    }


def price_change_requires_version_bump(
    previous_json: str | None, next_json: str | None
) -> str | None:
    """改了单价却没改 `version` → 返回一句可以直接显示的中文；没问题返回 `None`。

    ## 为什么这条规矩要写成代码

    每次运行会把当时的 `version` 落进 `ai_analysis_run.pricing_version`，用来回答
    「这条历史费用是按哪版单价算的」（见 `services/ai/usage.py::usage_from_run`）。
    价格改了、版本没改的后果不是报错，而是**安静地把历史解释弄错**：库里前后两批
    运行记着同一个版本号，金额却不一样，而且没有任何办法分辨哪条是改价前算的。

    ## 什么时候**不**报错

    * 新的表是空的（= 移除项目价格表，回落到平台默认表）；
    * 新的表解析不了（那是 `parse_price_table` 的错，由它去报，这里不抢）；
    * 之前没有可用的表（第一次配，没有可比对的旧版本）；
    * 只有 `version` 变了、单价没变（重新标版本是允许的）。
    """
    next_text = (next_json or "").strip()
    if not next_text:
        return None

    next_table, _next_errors = parse_price_table(next_text)
    if next_table is None:
        return None

    prev_text = (previous_json or "").strip()
    if not prev_text:
        return None
    prev_table, _prev_errors = parse_price_table(prev_text)
    if prev_table is None:
        return None

    if _model_signature(prev_table) == _model_signature(next_table):
        return None
    if str(prev_table.version) != str(next_table.version):
        return None
    return (
        f"单价改了，但 version 还是「{next_table.version}」。改单价必须同时改 version —— "
        "每次运行会把当时的版本号记下来，用来解释历史费用是按哪版算的；"
        "版本不变的话，改价前后的费用在库里分不开。"
    )
