"""模型单价表与费用估算。

## 这一组守的三件事

1. **默认表是空的，不预置任何单价。** 编出来的单价会被当成真钱看 —— 面板上显示
   「¥3.42」时没人会去想这个数是怎么来的。所以有一条测试专门钉住「出厂状态算不出费用，
   且理由是人话」。
2. **分三档计价。** 命中缓存的输入有独立单价，这正是 prompt cache 省钱的地方；把命中
   按未命中价算，「命中率 90%」在费用上就完全体现不出来。有一条反向自检证明这条测试
   不是碰巧对上了一个常数。
3. **算不出就 `None`，绝不返回 0 或部分金额。** 「这次没花钱」与「算不出来」是两件事。

另外，价格表里的键名写错（`input` 打成 `inpu`）会让那一档静默按 0 计价、算出来的费用
看着完全正常，所以解析层把多余键名一律当错误。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from services.ai import pricing

VALID_TABLE = json.dumps(
    {
        "version": "2026-09-18",
        "currency": "CNY",
        "models": {"cheap-model": {"input": 2.0, "output": 8.0, "cache_read": 0.2}},
    }
)


def _table(raw: str = VALID_TABLE) -> pricing.PriceTable:
    table, errors = pricing.parse_price_table(raw)
    assert errors == (), f"这份表本该是合法的：{errors}"
    assert table is not None
    return table


# --------------------------------------------------------------------------
# 一、出厂状态：没有单价，且理由能给人看
# --------------------------------------------------------------------------


def test_the_shipped_default_table_has_no_prices():
    """预置一份「看起来差不多」的单价，比不预置危险得多。"""
    assert pricing.default_price_table().models == {}


def test_an_unconfigured_table_yields_a_human_reason_not_a_zero():
    estimate = pricing.estimate_cost(
        "any-model", tokens_input=1200, tokens_output=300, table=pricing.default_price_table()
    )
    assert estimate.amount is None
    assert estimate.computable is False
    assert "价格表" in estimate.reason
    assert estimate.lines == ()


def test_an_empty_override_falls_back_to_the_default_table_not_to_nothing():
    """项目没填 → 用平台默认表。两条「没配」的路径汇到同一个出口。"""
    table, errors = pricing.load_price_table("")
    assert errors == ()
    assert table is not None
    assert table.source == "default"

    table2, errors2 = pricing.load_price_table(None)
    assert errors2 == ()
    assert table2.source == "default"


# --------------------------------------------------------------------------
# 二、分档计价
# --------------------------------------------------------------------------


def test_cache_hits_are_priced_separately_from_misses():
    estimate = pricing.estimate_cost(
        "cheap-model",
        tokens_input=1_000_000,
        tokens_output=0,
        cache_read=900_000,
        table=_table(),
    )
    # 未命中 100k × 2.0 = 0.2；命中 900k × 0.2 = 0.18 —— 合计 0.38
    assert estimate.amount == Decimal("0.38")
    assert estimate.currency == "CNY"
    assert [line.label for line in estimate.lines] == ["输入（未命中缓存）", "输入（命中缓存）", "输出"]


def test_the_amount_would_differ_if_hits_were_billed_at_the_miss_price():
    """反向自检：证明上一条测的是「分档」，不是碰巧对上了一个常数。

    把命中那 900k 按未命中价算，金额应当是 2.0 而不是 0.38。两个数不同，说明分档真的
    在起作用；若哪天有人把 `cache_read_per_million` 忽略掉，上一条会红而这条会先红。
    """
    all_miss = pricing.estimate_cost(
        "cheap-model", tokens_input=1_000_000, tokens_output=0, cache_read=None, table=_table()
    )
    with_hits = pricing.estimate_cost(
        "cheap-model", tokens_input=1_000_000, tokens_output=0, cache_read=900_000, table=_table()
    )
    assert all_miss.amount == Decimal("2")
    assert all_miss.amount != with_hits.amount


def test_a_zero_token_run_costs_zero_and_is_not_unknown():
    """这次确确实实没花到钱 —— 与「算不出来」必须分开。"""
    estimate = pricing.estimate_cost(
        "cheap-model", tokens_input=0, tokens_output=0, cache_read=0, table=_table()
    )
    assert estimate.amount == Decimal("0")
    assert estimate.computable is True


def test_a_missing_cache_price_falls_back_to_the_miss_price_with_a_note():
    """宁可给一个偏保守的上界，也不要让整块费用消失 —— 但必须写明它是保守的。"""
    raw = json.dumps(
        {"version": "v1", "models": {"m": {"input": 1.0, "output": 1.0}}}
    )
    estimate = pricing.estimate_cost(
        "m", tokens_input=1_000_000, tokens_output=0, cache_read=500_000, table=_table(raw)
    )
    assert estimate.amount == Decimal("1")
    assert any("偏保守" in note for note in estimate.notes)


def test_a_cache_read_larger_than_the_input_total_is_clamped_and_disclosed():
    """上游字段自相矛盾时按全部命中算，但要说出来 —— 悄悄 clamp 会让人以为上游是干净的。"""
    estimate = pricing.estimate_cost(
        "cheap-model", tokens_input=1000, tokens_output=0, cache_read=5000, table=_table()
    )
    assert estimate.amount is not None
    assert any("大于输入总数" in note for note in estimate.notes)
    assert all(line.tokens >= 0 for line in estimate.lines)


def test_an_unreported_cache_reading_is_disclosed_not_silently_assumed():
    estimate = pricing.estimate_cost(
        "cheap-model", tokens_input=1000, tokens_output=10, cache_read=None, table=_table()
    )
    assert any("未上报" in note for note in estimate.notes)


def test_unknown_tokens_make_it_uncomputable():
    """上游没给 token 数时不许按 0 算出一个「费用 0 元」的假结论。"""
    for input_tokens, output_tokens in ((None, 10), (10, None), (None, None)):
        estimate = pricing.estimate_cost(
            "cheap-model", tokens_input=input_tokens, tokens_output=output_tokens, table=_table()
        )
        assert estimate.amount is None
        assert "token" in estimate.reason


# --------------------------------------------------------------------------
# 三、模型名匹配
# --------------------------------------------------------------------------


def test_exact_match_wins():
    price, pattern = pricing.resolve_price("cheap-model", _table())
    assert pattern == "cheap-model"
    assert price is not None


def test_matching_is_case_insensitive_and_ignores_padding():
    _price, pattern = pricing.resolve_price("  Cheap-Model ", _table())
    assert pattern == "cheap-model"


def test_the_longest_prefix_wins():
    """网关给模型名加后缀是常态（`deepseek-v4-flash-inhouse-yd` 这类实测存在）。"""
    raw = json.dumps(
        {
            "version": "v1",
            "models": {
                "vendor-*": {"input": 9.0, "output": 9.0},
                "vendor-flash-*": {"input": 1.0, "output": 2.0},
            },
        }
    )
    price, pattern = pricing.resolve_price("vendor-flash-inhouse-yd", _table(raw))
    assert pattern == "vendor-flash-*"
    assert price is not None and price.input_per_million == Decimal("1.0")


def test_contains_matching_is_not_used():
    """`gpt-4` 不许命中 `gpt-4o` —— 价格差一个量级，而且完全没有提示。"""
    raw = json.dumps({"version": "v1", "models": {"gpt-4": {"input": 1.0, "output": 1.0}}})
    price, pattern = pricing.resolve_price("gpt-4o", _table(raw))
    assert price is None
    assert pattern == ""


def test_an_unknown_model_is_uncomputable_with_a_readable_reason():
    estimate = pricing.estimate_cost(
        "not-in-the-table", tokens_input=1000, tokens_output=10, table=_table()
    )
    assert estimate.amount is None
    assert "not-in-the-table" in estimate.reason


# --------------------------------------------------------------------------
# 四、解析层的拒绝面
# --------------------------------------------------------------------------


def test_the_parsed_table_carries_its_version_for_traceability():
    table = _table()
    assert table.version == "2026-09-18"
    assert table.source == "project"
    estimate = pricing.estimate_cost("cheap-model", tokens_input=1, tokens_output=1, table=table)
    assert estimate.price_version == "2026-09-18"


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("不是 JSON", "{不是 json"),
        ("最外层是数组", "[]"),
        ("没有 models", json.dumps({"version": "v1"})),
        ("models 是空对象", json.dumps({"version": "v1", "models": {}})),
        ("没有 version", json.dumps({"models": {"m": {"input": 1, "output": 1}}})),
        ("单价是负数", json.dumps({"version": "v1", "models": {"m": {"input": -1, "output": 1}}})),
        ("少了 output", json.dumps({"version": "v1", "models": {"m": {"input": 1}}})),
        # 键名打错是最危险的一类：那一档会静默按 0 计价，算出来的费用看着完全正常
        ("键名打错", json.dumps({"version": "v1", "models": {"m": {"inpu": 1, "output": 1}}})),
        (
            "缓存价高于未命中价",
            json.dumps({"version": "v1", "models": {"m": {"input": 1, "output": 1, "cache_read": 9}}}),
        ),
        ("模型条目不是对象", json.dumps({"version": "v1", "models": {"m": 3}})),
        ("表级多余键", json.dumps({"version": "v1", "models": {}, "typo": 1})),
    ],
)
def test_bad_tables_are_rejected_with_readable_errors(label, raw):
    table, errors = pricing.parse_price_table(raw)
    assert table is None, f"{label} 本该被拒"
    assert errors, f"{label} 被拒了但没说原因"


def test_a_broken_project_table_does_not_silently_fall_back_to_the_default():
    """静默回落 = 「用户以为改了价格、实际还是旧的」，是最难查的一类问题。"""
    table, errors = pricing.load_price_table("{坏掉的 json")
    assert table is None
    assert errors


def test_amounts_are_serialized_as_strings_so_the_frontend_never_does_float_math():
    estimate = pricing.estimate_cost(
        "cheap-model", tokens_input=1_000_000, tokens_output=1_000_000, cache_read=0, table=_table()
    )
    payload = estimate.to_dict()
    assert payload["amount"] == "10.00"  # 2.0 + 8.0，定点 2 位小数
    assert isinstance(payload["amount"], str)
    for line in payload["lines"]:
        assert isinstance(line["amount"], str)


def test_the_documented_shape_is_actually_parseable():
    """配置界面要展示的示例必须真的能用 —— 否则用户照着抄一遍还是错的。"""
    shape = pricing.price_table_doc_shape()
    table, errors = pricing.parse_price_table(shape)
    assert errors == ()
    assert table is not None
    assert table.models, "示例里至少要有一个模型条目"


# ==========================================================================
# 金额展示：全平台统一 2 位小数
# ==========================================================================
# 在这之前是「量化到 6 位再去掉尾随 0」，同一个面板上会同时出现 ¥86.40、¥0.001980、
# ¥2 三种形态 —— 费用是拿来横向比的一列，读的人不该先去数小数点后几位。


def test_money_is_always_two_decimal_places():
    cases = {
        Decimal("86.4"): "86.40",
        Decimal("86.400000"): "86.40",
        Decimal("2"): "2.00",
        Decimal("0"): "0.00",
        Decimal("1234567.891"): "1234567.89",   # 不进科学计数法
        Decimal("1.005"): "1.01",               # 四舍五入（HALF_UP），不是截断
        Decimal("1.004"): "1.00",
    }
    for value, expected in cases.items():
        assert pricing.money(value) == expected, f"{value} → {pricing.money(value)}"


def test_money_never_uses_scientific_notation():
    """`normalize()` 会把 10.00 变成 1E+1，前端拿到的就是「1E+1 元」。"""
    for value in (Decimal("10"), Decimal("1000000"), Decimal("0.5"), Decimal("1E+3")):
        text = pricing.money(value)
        assert "E" not in text.upper(), f"{value} → {text}"


def test_a_non_zero_amount_below_one_cent_is_not_shown_as_zero():
    """**不足一分钱不等于没花钱。**

    显示 `0.00` 会让读的人以为这次运行免费 —— 而它确实花了钱。本模块从头到尾守着
    「0 与算不出是两件事」这条口径，展示层不能反过来把「很小」说成「没有」。反向自检：
    真正的 0 必须仍然显示 0.00，否则这条规矩就变成了「所有小数字都不显示」。
    """
    assert pricing.money(Decimal("0.00198")) == pricing.MONEY_BELOW_ONE_CENT
    assert pricing.money(Decimal("0.0049")) == pricing.MONEY_BELOW_ONE_CENT
    assert pricing.money(Decimal("0")) == "0.00"
    assert pricing.money(Decimal("0.005")) == "0.01"   # 四舍五入到一分，不算「不足」


def test_money_returns_none_for_junk_instead_of_rendering_it():
    """脏数据不能被当成金额渲染出来。"""
    assert pricing.money(None) is None
    assert pricing.money(Decimal("NaN")) is None
    assert pricing.money(Decimal("Infinity")) is None
    assert pricing.money("不是数") is None


def test_the_price_change_signature_keeps_full_precision():
    """判等**不能**跟着展示精度走。

    展示是 2 位小数，判等是 6 位（`_exact_text`，与改动前的 `money()` 同一精度）。
    若 `_model_signature` 改用展示值判等，`2.0` → `2.000001` 这种改价就再也看不出，
    于是「改单价必须改 version」被静默绕过 —— 而那条规矩守的是「历史费用按哪版单价
    算的」能不能追溯。
    """
    bumped = VALID_TABLE.replace('"input": 2.0', '"input": 2.000001')
    assert pricing.money(Decimal("2.0")) == pricing.money(Decimal("2.000001")) == "2.00"
    assert pricing._model_signature(_table()) != pricing._model_signature(_table(bumped))
    reason = pricing.price_change_requires_version_bump(VALID_TABLE, bumped)
    assert reason, "改了单价却没改 version，必须被拦下"
