# -*- coding: utf-8 -*-
"""模型的收口声明：**读进来**这一层（`services/ai/baseline_closures.py`）。

## 这一层要守的两件事

**1. 丢弃必须有账。** 一条声明进不来有三种原因（形状不对 / 枚举不在集合里 / 没写理由），
而**「模型写了、平台没读出来」是最坏的一种静默**：模型以为它关掉了一条旧结论，下一轮
那条又出现在清单里，双方都以为对方处理了。所以这一层把每一条进不来的都连**原文**和
**原因**一起交回去，由调用方记进 `dropped`（`protocol.parse_payload` → 面板的运行轨迹）。

**2. `None` / 缺键在这一层不是错误。** 这一层按「模型给了什么」解析，不做任何要求 ——
没有清单的轮次（首跑 / 用户点了全量 / 上一轮没有结构化结论）模型不写它就对了，把它记成
一条「丢弃」是假账（读的人会去找一份不存在的声明）。

**「给了清单就要逐条交代」是引擎在解析之后查的**（`missing_baseline_statuses` → 当场补问
一次），不在这里抛错：在这里抛等于把「少填一个状态」升级成「整份结论作废」（重问额度用尽
后会降级成只有 markdown），那比今天的处境更糟。

为什么这一层不自己记进 `dropped`：它不知道账记在哪（协议层记进 `payload.dropped`，
而 `incremental_baseline` 只用解析结果、不写账），硬塞一个全局账本等于把这个决定
藏进一层与它无关的模块里。
"""
from __future__ import annotations

import json

from services.ai.baseline_closures import (
    DECLARED_FIXED,
    DECLARED_OVERTURNED,
    DECLARED_STANDING,
    MAX_CLOSURES,
    REASON_MAX_CHARS,
    coerce_closures,
)
from services.ai.protocol import missing_baseline_statuses, parse_payload

FP = "0123456789abcdef"
FP2 = "fedcba9876543210"


def _final(**extra) -> str:
    """一份最小的 `final` 回答（`dimensions` 必填，所以给它一条）。"""
    body = {
        "status": "final",
        "report_markdown": "# 变更理解\n\n正文",
        "dimensions": [{"id": "config_id", "hit": False, "note": "本批次没改配表"}],
    }
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


# --------------------------------------------------------------------------
# 一、解析：什么算一条声明
# --------------------------------------------------------------------------


def test_absent_declarations_are_not_a_problem():
    """可选字段不写就是没写 —— **不是错误**，也不许记成丢弃。"""
    payload = parse_payload(_final())

    assert payload.baseline_closures == ()
    assert [item for item in payload.dropped if item.kind == "baseline_update"] == []


def test_a_well_formed_declaration_survives_the_round_trip():
    payload = parse_payload(
        _final(
            baseline_updates=[
                {
                    "fingerprint": FP,
                    "status": "fixed",
                    "reason": "兜底常量加回来了，服务端又校验了队伍上限",
                }
            ]
        )
    )

    assert len(payload.baseline_closures) == 1
    closure = payload.baseline_closures[0]
    assert closure.fingerprint == FP
    assert closure.status == DECLARED_FIXED
    assert closure.reason == "兜底常量加回来了，服务端又校验了队伍上限"
    assert closure.label == "声明已修复", "给人看的说法里必须带「声明」二字"
    assert payload.dropped == ()


def test_the_uppercase_fingerprint_and_status_are_the_same_declaration():
    """指纹抄成大写、状态写 `Fixed` —— 形状对就收下，不该因为大小写丢掉一条声明。"""
    closures, problems = coerce_closures(
        [{"fingerprint": FP.upper(), "status": "OVERTURNED", "reason": "反证在另一处"}]
    )

    assert problems == ()
    assert [item.fingerprint for item in closures] == [FP]
    assert closures[0].status == DECLARED_OVERTURNED


def test_every_unusable_declaration_is_recorded_with_its_reason():
    """进不来的四种形态各记一条账，且**带上原文**（不然读的人不知道它写了什么）。"""
    closures, problems = coerce_closures(
        [
            "0123456789abcdef",  # 不是对象
            {"fingerprint": "short", "status": "fixed", "reason": "理由"},  # 指纹形状不对
            {"fingerprint": FP, "status": "resolved", "reason": "理由"},  # 枚举不在集合里
            {"fingerprint": FP2, "status": "fixed", "reason": "   "},  # 没写理由
            {"fingerprint": FP, "status": "fixed", "reason": "第一条"},  # 这条收下
            {"fingerprint": FP, "status": "overturned", "reason": "第二条"},  # 同一条写了两遍
        ]
    )

    assert [(item.fingerprint, item.reason) for item in closures] == [(FP, "第一条")]
    assert [reason for _, reason in problems] == [
        "这一条不是一个对象",
        "fingerprint 必须是清单里那条末尾的 16 位十六进制",
        f"status 只能是 {DECLARED_STANDING} | {DECLARED_FIXED} | {DECLARED_OVERTURNED}",
        "没有写理由 —— 空理由的声明不算收口",
        "同一条结论声明了两次，只留第一条",
    ]
    assert problems[0][0] == "0123456789abcdef", "账里没有原文，读的人看不出它写了什么"


def test_a_declaration_without_a_reason_is_not_a_closure():
    """理由是本轮唯一能解释「凭什么说它修好了」的东西，而它是给人看的。

    空理由的声明与没声明**在读者眼里是同一件事**（都只有一句「修好了」），而它会让
    目录里那条旧结论**真的**从在挂清单里消失 —— 收下的代价大于不收。
    """
    closures, problems = coerce_closures([{"fingerprint": FP, "status": "fixed"}])

    assert closures == ()
    assert len(problems) == 1


def test_a_non_list_value_is_one_problem_not_a_crash():
    closures, problems = coerce_closures({"fingerprint": FP})

    assert closures == ()
    assert problems == (("", "baseline_updates 必须是数组"),)


def test_the_reason_is_clipped_instead_of_dropped():
    """理由超长**截断收下**，不整条丢 —— 理由短一点仍然是有用的，丢掉就什么都没了。"""
    closures, problems = coerce_closures(
        [{"fingerprint": FP, "status": "fixed", "reason": "长" * (REASON_MAX_CHARS + 50)}]
    )

    assert problems == ()
    assert len(closures[0].reason) == REASON_MAX_CHARS


def test_the_declaration_count_is_capped_and_the_overflow_is_accounted():
    """上限是**上限而不是目标**：超出的那句要记进账，不能悄悄收下第 51 条。"""
    entries = [
        {"fingerprint": f"{index:016x}", "status": "fixed", "reason": "理由"}
        for index in range(MAX_CLOSURES + 3)
    ]

    closures, problems = coerce_closures(entries)

    assert len(closures) == MAX_CLOSURES
    assert len(problems) == 3
    assert all(f"一次最多声明 {MAX_CLOSURES} 条" in reason for _, reason in problems)


# --------------------------------------------------------------------------
# 三、逐条枚举：清单里有几条就要几条状态
# --------------------------------------------------------------------------


def test_the_list_from_the_prompt_must_be_answered_item_by_item():
    """给了清单就要求逐条交代 —— 缺的**逐条记账**（引擎据此当场要一次）。

    这一层只负责把「缺了哪几条」说清楚，别的一概不管：**催不催、催几次、催不动怎么办
    是引擎的事**（`engine` 的 `CORRECTION_BASELINE_COVERAGE` 那一支）。

    为什么要有这条规矩：「模型多半会写」不是可以依赖的东西 —— `dimensions` 一次没漏过，
    正因为它必填、缺了会被打回。判据落在**记账**上：少一条就是 `dropped` 里的一条
    `baseline_coverage_missing`，引擎按它决定补问什么。
    """
    payload = parse_payload(
        _final(
            baseline_updates=[
                {"fingerprint": FP, "status": "standing"},
                {"fingerprint": FP2, "status": "fixed", "reason": "兜底加回来了"},
            ]
        ),
        baseline_fingerprints=(FP, FP2, "1111222233334444"),
    )

    missing = missing_baseline_statuses(payload)
    assert missing == ("1111222233334444",), missing
    dropped = [item for item in payload.dropped if item.kind == "baseline_coverage_missing"]
    assert [item.detail for item in dropped] == ["1111222233334444"]
    # 记账归记账，**结论照收**：少填一个状态不该让整份结论作废。
    assert payload.is_final and payload.report_markdown


def test_standing_needs_no_reason_but_a_close_out_does():
    """`standing` 是「这条还在」，它没有新话要说 —— 要求它也写理由等于逼模型编一段。

    `reason` 是给人看的「凭什么说它修好了」，而 `standing` 的主张是「什么都没变」，
    没有可核的东西。
    """
    closures, problems = coerce_closures(
        [
            {"fingerprint": FP, "status": "standing"},
            {"fingerprint": FP2, "status": "overturned"},
        ]
    )

    assert problems == (("fedcba9876543210 · overturned", "没有写理由 —— 空理由的声明不算收口"),)
    assert [(item.fingerprint, item.status, item.reason) for item in closures] == [
        (FP, DECLARED_STANDING, ""),
    ]


def test_no_list_means_nothing_to_answer():
    """没有清单（首跑 / 全量 / 上一轮没结论）时一个字都不要求。"""
    payload = parse_payload(_final(), baseline_fingerprints=())

    assert missing_baseline_statuses(payload) == ()



# --------------------------------------------------------------------------
# 二、协议层：进不来的要落进 `dropped`
# --------------------------------------------------------------------------


def test_the_malformed_declarations_land_in_the_dropped_ledger():
    """**这一层存在的理由**：模型写了、平台没读出来时，账上要看得见。

    只判「`baseline_closures` 是空的」是不够的 —— 那与「模型压根没写」在读数的人眼里
    长得一模一样，而两者的下一步动作完全相反（一个去改协议、一个什么都不用做）。
    """
    payload = parse_payload(
        _final(
            baseline_updates=[
                {"fingerprint": "nope", "status": "fixed", "reason": "理由"},
                {"fingerprint": FP, "status": "fixed", "reason": "这条是好的"},
            ]
        )
    )

    assert [item.fingerprint for item in payload.baseline_closures] == [FP]
    dropped = [item for item in payload.dropped if item.kind == "baseline_update"]
    assert len(dropped) == 1
    assert "nope" in dropped[0].detail and "16 位十六进制" in dropped[0].reason


def test_a_mid_round_declaration_is_dropped_like_the_other_final_only_fields():
    """中间轮没有收口这件事要办 —— 那一轮存在的意义只有「我还需要什么」。"""
    payload = parse_payload(
        json.dumps(
            {
                "status": "need_more_context",
                "reason": "先看道具表",
                "requests": [
                    {"type": "file_diff", "commit": "a" * 40, "path": "config/道具表.xlsx"}
                ],
                "baseline_updates": [
                    {"fingerprint": FP, "status": "fixed", "reason": "顺手写的"}
                ],
            },
            ensure_ascii=False,
        )
    )

    assert payload.baseline_closures == ()
    dropped = [item for item in payload.dropped if item.kind == "mid_round_field"]
    assert [item.detail for item in dropped] == ["baseline_updates 给了 1 条"], payload.dropped
