# -*- coding: utf-8 -*-
"""WP27：只有与本次配表差异**有可核实关联**的代码风险参与定级。

## 这条口径要挡的是哪类错

WP20 把 `tables` 的 `allowed_paths` 放开成 `None`（读取不限制）之后，
`result_payload._out_of_scope` 在 `allowed is None` 时恒 False —— 模型在代码仓库、
未改动的引用方表上发现的一切都进了风险等级。后果是「读得越全，报告看起来越严重」，
而其中大部分只是背景观察，不是这次配表要处理的问题。

WP27 的判据：`allowed is None` 时，参与定级要求结论与**本次配表差异**有可核实关联：

* 结论指认的文件本身在 `delta_files` 里；
* 或它的某条原子断言（`Anomaly.claims[].evidence_refs`）指向 `delta_files` 里的文件
  —— 代码消费方 / 未改动引用方表对本次配表差异的结论，读者能照着那条依据去核。

其余的是**背景发现**：照常进 `anomalies`（带 `out_of_scope=True`，报告里看得见），
但不参与风险等级与「模型报出 N 条」的计数。

`allowed_paths` 非 None 时行为**逐字不变**（老 payload / 人工收窄仍按越界判）。
"""
from __future__ import annotations

import pytest

from services.ai import anomaly_scope, report_document
from services.ai.engine import EngineOutcome
from services.ai.protocol import Anomaly, Claim
from services.ai.result_payload import (
    _change_paths_of,
    _claims_touch,
    _out_of_scope,
    _ref_path,
    result_payload,
)

TABLE = "config/[30]道具表_CfgItem.xlsx"
UNCHANGED_TABLE = "config/[31]奖励表_CfgReward.xlsx"
CODE = "code/qz_logic/item_use.lua"
BACKGROUND_CODE = "code/qz_server/chat_service.lua"
COMMIT = "a" * 40


# --------------------------------------------------------------------------
# 构造：一份 `tables` 周版本 payload + 若干条结论
# --------------------------------------------------------------------------


def _payload(*, allowed_paths=None, delta_paths=(TABLE,)) -> dict:
    """手写一份周版本 payload。

    `delta_paths=None` = 老 payload（**没有** `delta_files` 键）；`allowed_paths` 显式
    传列表时是「人工收窄 / 升级前落库」的那一档。
    """
    payload: dict = {"mode": "weekly", "summary": {}, "allowed_paths": allowed_paths}
    if delta_paths is not None:
        payload["delta_files"] = [
            {"file_path": path, "latest_commit_id": COMMIT, "file_type": "table"}
            for path in delta_paths
        ]
    return payload


def _claim(*refs: str, statement: str = "改过的表会被这处代码读到") -> Claim:
    return Claim(
        claim_id="C1",
        kind="fact",
        statement=statement,
        evidence_refs=tuple(refs),
    )


def _anomaly(
    *,
    title: str,
    file_path: str,
    severity: str = "high",
    claims: tuple = (),
    category: str = "code_logic",
) -> Anomaly:
    return Anomaly(
        title=title,
        category=category,
        severity=severity,
        confidence="high",
        evidence=(f"{file_path} 第 3 行" if file_path else "整体流程",),
        commit=COMMIT,
        file_path=file_path,
        claims=claims,
    )


def _outcome(*anomalies: Anomaly) -> EngineOutcome:
    return EngineOutcome(
        status="succeeded",
        anomalies=tuple(anomalies),
        report_markdown="# 风险评估\n\n模型写的正文。\n",
    )


def _by_title(payload: dict) -> dict:
    return {entry["title"]: entry for entry in payload["anomalies"]}


# ==========================================================================
# 一、三档：配表差异本身 / 有可核实关联 / 无关联的背景发现
# ==========================================================================


def test_a_config_delta_path_is_in_scope():
    """档一：结论指认的文件就是本次配表差异 —— 它当然参与定级。"""
    outcome = _outcome(_anomaly(title="配表 ID 冲突", file_path=TABLE, severity="medium"))

    payload = result_payload(outcome, _payload(allowed_paths=None))

    assert payload["anomalies"][0]["out_of_scope"] is False, (
        "配表 delta 自己的结论被标成背景发现 —— 等级会掉到规模兜底"
    )
    assert payload["risk_level"] == "mid_high"
    assert payload["risk_reasons"][0] == "模型报出 1 条达门槛的问题"
    assert not any("另有" in reason for reason in payload["risk_reasons"]), (
        "没有单列条目，却在定级依据里说了「另有 N 条」"
    )


@pytest.mark.parametrize(
    "ref",
    [
        f"{TABLE}:12",
        # 裸路径：没有定位符也是合法依据（`ref_shapes.is_locatable_ref` 的既有口径）。
        TABLE,
        # 单元格区间**自带一个冒号**：从右边切会得到一个不存在的路径 —— 这里钉住
        # 「解析复用 `ref_shapes._split_ref`」而不是自己切。
        f"{TABLE}:Sheet1!B2:C5",
        # 反斜杠写法：归一化之后必须还认得出是同一张表。
        TABLE.replace("/", "\\") + ":12",
    ],
)
def test_a_claim_referencing_the_changed_table_makes_a_code_finding_relevant(ref):
    """档二：代码消费方的结论，但它引用了本次改过的表 —— 有可核实关联，参与定级。

    这是本次审查建议里最关键的一档：读取放开（WP20）的价值就在于能看到「谁在消费这张
    表」，若这类结论被当成背景发现单列，真问题会跟着等级一起消失。
    """
    outcome = _outcome(
        _anomaly(
            title="道具使用分支没有处理新 ID",
            file_path=CODE,
            severity="critical",
            claims=(_claim(ref),),
        )
    )

    payload = result_payload(outcome, _payload(allowed_paths=None))

    assert payload["anomalies"][0]["out_of_scope"] is False, (
        f"依据 {ref!r} 指向本次改动的表，结论却被单列 —— 可核实关联没生效"
    )
    assert payload["risk_level"] == "high", "有可核实关联的 critical 没有参与定级"
    assert payload["risk_reasons"][0] == "模型报出 1 条达门槛的问题"


def test_a_background_finding_is_listed_but_does_not_set_the_level():
    """档三：纯背景代码结论照常列出（报告可见），但不抬等级、不计入「模型报出 N 条」。"""
    outcome = _outcome(
        _anomaly(title="配表 ID 冲突", file_path=TABLE, severity="medium"),
        _anomaly(
            title="聊天服务里的并发写",
            file_path=BACKGROUND_CODE,
            severity="critical",
            claims=(_claim(f"{BACKGROUND_CODE}:88"),),
        ),
    )

    payload = result_payload(outcome, _payload(allowed_paths=None))

    by_title = _by_title(payload)
    assert by_title["配表 ID 冲突"]["out_of_scope"] is False
    assert by_title["聊天服务里的并发写"]["out_of_scope"] is True, (
        "与本次配表差异无关的代码结论被算进了定级 —— 读得越全、报告越严重"
    )
    # **条目一个不少**：单列不是丢掉，报告里仍要看得见模型报过什么。
    assert len(payload["anomalies"]) == 2
    assert payload["risk_level"] == "mid_high", "背景里的 critical 把等级抬到了 high"
    assert payload["risk_reasons"][0] == "模型报出 1 条达门槛的问题"
    assert any(
        "另有 1 条" in reason and "关联不足" in reason and "未计入等级" in reason
        for reason in payload["risk_reasons"]
    ), "单列的条数没有交代 ——「平台不采纳」会被读成「模型觉得没问题」"

    text = report_document.build_report_markdown(
        anomalies=payload["anomalies"], report_text="# 风险评估\n\n模型写的正文。\n"
    )
    assert "聊天服务里的并发写" in text, "背景发现在报告里消失了 —— 它不是被丢掉，是被单列"


def test_a_run_whose_only_findings_are_background_falls_back_to_the_scale():
    """只报了背景发现的那次会落到规模兜底 —— 背景里的 critical 绝不许顶格。

    这一条同时钉住「不准」与「说清楚」：等级不能是 high，定级依据必须写明模型报了但
    平台没有采纳（否则「平台不采纳」会被读成「模型觉得没问题」）。
    """
    outcome = _outcome(
        _anomaly(
            title="聊天服务里的并发写",
            file_path=BACKGROUND_CODE,
            severity="critical",
            claims=(_claim(f"{BACKGROUND_CODE}:88"),),
        )
    )

    payload = result_payload(outcome, _payload(allowed_paths=None))

    assert payload["risk_level"] != "high", "背景里的 critical 把等级顶到了 high"
    assert any("模型未报出达门槛的问题" in reason for reason in payload["risk_reasons"])
    assert any("关联不足" in reason for reason in payload["risk_reasons"]), (
        "只报了背景发现时没有交代 ——「平台不采纳」会被读成「模型觉得没问题」"
    )


def test_a_claim_on_an_unchanged_table_is_not_a_relation():
    """引用了**另一张没改的表**不算关联：本次配表差异里没有它，没什么可照着核的。"""
    outcome = _outcome(
        _anomaly(
            title="奖励表读取路径有遗漏",
            file_path=CODE,
            severity="critical",
            claims=(_claim(f"{UNCHANGED_TABLE}:7"),),
        )
    )

    payload = result_payload(outcome, _payload(allowed_paths=None))

    assert payload["anomalies"][0]["out_of_scope"] is True
    assert payload["risk_level"] != "high"


def test_a_finding_without_claims_is_conservatively_background():
    """`claims` 形状缺失 / 为空 = **读不出关联**，按背景发现单列（保守方向）。

    这是本机制已知的代价：旧形态的结论（模型没按协议给 `claims`）只要指认的文件不在
    delta 里，就会从「参与定级」退到「单列」。宁可少算，也不许背景发现抬等级 ——
    读得出关联的那一档由上面那条用例钉住。
    """
    outcome = _outcome(
        _anomaly(title="没有原子断言的代码结论", file_path=CODE, severity="critical")
    )

    payload = result_payload(outcome, _payload(allowed_paths=None))

    assert payload["anomalies"][0]["out_of_scope"] is True
    assert payload["risk_level"] != "high"


def test_a_process_finding_without_a_file_path_still_counts():
    """没有文件路径的跨文件 / 流程结论仍放行（旧口径，两种判据下都成立）。"""
    outcome = _outcome(_anomaly(title="整体流程缺回滚", file_path="", severity="high"))

    payload = result_payload(outcome, _payload(allowed_paths=None))

    assert payload["anomalies"][0]["out_of_scope"] is False, (
        "无路径的流程结论被单列 —— 它被静默降级，用户看不出有一条关键结论被丢掉"
    )
    assert payload["risk_level"] == "mid_high"
    assert payload["risk_reasons"][0] == "模型报出 1 条达门槛的问题"


# ==========================================================================
# 二、`allowed_paths` 非 None：旧行为**逐字不变**
# ==========================================================================


def test_an_explicit_allow_list_still_wins_over_the_claims():
    """显式白名单是「读取范围」的声明：文件不在里面就是越界，关联证据不能翻案。

    这一档服务老 payload 与将来的人工收窄 —— 判据必须还是加 WP27 之前那一份。
    """
    outcome = _outcome(
        _anomaly(title="配表 ID 冲突", file_path=TABLE, severity="medium"),
        _anomaly(
            title="消费方漏改判断",
            file_path=CODE,
            severity="critical",
            # 依据指向白名单里的表：在 `None` 档会救回这条结论，在显式列表档**不**。
            claims=(_claim(f"{TABLE}:12"),),
        ),
    )

    payload = result_payload(outcome, _payload(allowed_paths=[TABLE]))

    by_title = _by_title(payload)
    assert by_title["配表 ID 冲突"]["out_of_scope"] is False
    assert by_title["消费方漏改判断"]["out_of_scope"] is True, (
        "显式白名单被 claims 里的关联证据翻案了 —— 读取范围的声明失效"
    )
    assert payload["risk_level"] == "mid_high"
    assert any("范围外" in reason for reason in payload["risk_reasons"]), (
        "旧口径的措辞变了 —— 升级前落库的 payload 重新渲染会读到另一套话"
    )


def test_an_empty_allow_list_refuses_every_located_finding_even_with_claims():
    """空列表 = 一条都不许读：有路径的结论全部越界，依据指向 delta 也不放行。"""
    outcome = _outcome(
        _anomaly(
            title="消费方漏改判断",
            file_path=CODE,
            severity="critical",
            claims=(_claim(f"{TABLE}:12"),),
        ),
        _anomaly(title="整体流程缺回滚", file_path="", severity="high"),
    )

    payload = result_payload(outcome, _payload(allowed_paths=[]))

    by_title = _by_title(payload)
    assert by_title["消费方漏改判断"]["out_of_scope"] is True
    assert by_title["整体流程缺回滚"]["out_of_scope"] is False
    assert payload["risk_level"] == "mid_high", "越界的 critical 参与了定级"
    assert any("范围外" in reason for reason in payload["risk_reasons"])


# ==========================================================================
# 三、老 payload（没有 `delta_files`）：关联锚点缺失时按保守方向判
# ==========================================================================


def test_a_legacy_payload_without_delta_files_is_conservative():
    """没有 `delta_files` = 没有「本次差异」可对：有路径的结论一律单列，无路径的放行。

    这是**保守方向**：锚点缺失时宁可少算等级，也不许把整个仓库的背景观察当成这次配表
    的风险。旧 payload（升级前落库）会因此被重新渲染成更低的等级 —— 这是本轮明确接受
    的代价，而不是漏判：条目仍列在报告里，定级依据也写明了「另有 N 条」。
    """
    outcome = _outcome(
        _anomaly(
            title="代码分支条件写错",
            file_path=CODE,
            severity="critical",
            claims=(_claim(f"{TABLE}:12"),),
        ),
        _anomaly(title="整体流程缺回滚", file_path="", severity="high"),
    )

    payload = result_payload(outcome, _payload(delta_paths=None))

    by_title = _by_title(payload)
    assert by_title["代码分支条件写错"]["out_of_scope"] is True
    assert by_title["整体流程缺回滚"]["out_of_scope"] is False
    assert payload["risk_level"] == "mid_high"
    assert payload["risk_reasons"][0] == "模型报出 1 条达门槛的问题"
    assert any("另有 1 条" in reason for reason in payload["risk_reasons"])


def test_the_change_paths_are_normalized_and_junk_is_skipped():
    """`_change_paths_of` 与 `_allowed_paths_of` 同一套归一化：写法差异不许变成假拒绝。"""
    assert _change_paths_of({}) == frozenset(), "缺键不是「有一条空路径」"
    assert _change_paths_of({"delta_files": None}) == frozenset()
    assert _change_paths_of(
        {
            "delta_files": [
                {"file_path": "./" + TABLE.replace("/", "\\")},
                {"file_path": ""},
                {"file_path": None},
                "junk",
                {"no_path": 1},
                {"file_path": "  " + TABLE + "  "},
            ]
        }
    ) == frozenset({TABLE}), "归一化 / 去重 / 跳过脏形状有一条没做到"
    # 单提交：`delta_files` 缺键时退回 `commit.path`（只有这一个锚点，同样要归一化）。
    assert _change_paths_of(
        {"mode": "commit", "commit": {"path": "./" + CODE.replace("/", "\\")}}
    ) == frozenset({CODE})
    # `commit` 形状不对 / 没有 path：仍是空集（不猜）。
    assert _change_paths_of({"mode": "commit", "commit": {}}) == frozenset()
    assert _change_paths_of({"mode": "commit", "commit": "junk"}) == frozenset()
    # 周版本（无 `delta_files`、无 `commit`）仍是空集 —— 「老 payload 保守」那条的前提。
    assert _change_paths_of({"mode": "weekly", "summary": {}}) == frozenset()


def test_claims_in_dict_form_are_read_too():
    """`_out_of_scope` 同时认 `Claim` 与字典形态（`_anomaly_entry` 那一侧的读法）。"""
    assert _out_of_scope(
        CODE,
        None,
        change_paths=frozenset({TABLE}),
        claims=({"evidence_refs": [f"{TABLE}:12"]},),
    ) is False
    assert _out_of_scope(
        CODE,
        None,
        change_paths=frozenset({TABLE}),
        claims=({"evidence_refs": [f"{BACKGROUND_CODE}:9"]},),
    ) is True
    # 证据是裸字符串（不是列表）：`claims._as_evidence` 的既有口径也认这一种。
    assert _out_of_scope(
        CODE, None, change_paths=frozenset({TABLE}), claims=({"evidence_refs": f"{TABLE}:12"},)
    ) is False


# ==========================================================================
# 四、单提交模式：锚点是它自己那条提交（`commit.path`）
#
# `build_commit_payload` 没有 `delta_files`（单提交的输入就是那一条提交）。没有这一节
# 的 fallback，单提交运行里所有有路径的结论都会被当成背景发现、等级退化成规模兜底 ——
# 那是 WP27 不该波及的另一条输入形态。周版本（无 delta、无 commit）仍是空集，保守口径
# 不变（见上一节）。
# ==========================================================================


def _commit_payload(path: str = CODE) -> dict:
    """`build_commit_payload` 的形状：没有 `delta_files`，只有一条 commit。"""
    return {
        "mode": "commit",
        "scope": "full",
        "summary": {},
        "commit": {"commit_id": COMMIT, "path": path, "operation": "modify"},
    }


def test_a_single_commit_payload_uses_the_commit_path_as_the_anchor():
    """单提交里指着**该提交改动文件**的结论照常参与定级（锚点是 `commit.path`）。"""
    outcome = _outcome(
        _anomaly(title="单提交里的条件写错", file_path=CODE, severity="critical")
    )

    payload = result_payload(outcome, _commit_payload())

    assert payload["anomalies"][0]["out_of_scope"] is False, (
        "单提交自己的改动被当成背景发现 —— 等级会掉到规模兜底"
    )
    assert payload["risk_level"] == "high"
    assert payload["risk_reasons"][0] == "模型报出 1 条达门槛的问题"
    assert not any("另有" in reason for reason in payload["risk_reasons"]), (
        "单提交里没有背景发现，却在定级依据里说了「另有 N 条」"
    )


def test_a_single_commit_payload_still_lists_an_unrelated_path():
    """`commit.path` 只给它自己那一个文件兜底：别的文件上的结论仍是背景发现。"""
    outcome = _outcome(
        _anomaly(title="无关模块的并发写", file_path=BACKGROUND_CODE, severity="critical")
    )

    payload = result_payload(outcome, _commit_payload())

    assert payload["anomalies"][0]["out_of_scope"] is True, (
        "单提交的锚点把整个仓库都算成了「本次改动」—— 背景发现又进了等级"
    )
    assert payload["risk_level"] != "high"
    assert any("关联不足" in reason for reason in payload["risk_reasons"])


# ==========================================================================
# 五、判据只有一个出处（WP28）：`result_payload` 的旧私有名是别名，不是第二份实现
# ==========================================================================


def test_the_scope_rules_have_a_single_implementation_behind_the_old_names():
    """这四个名字必须**指向同一份实现**，不是各自留了一份拷贝。

    WP28 把判据搬进 `services.ai.anomaly_scope`（增量基线也要用它重算继承条目）。
    若 `result_payload` 不是 import 而是把函数再抄一份，两个调用方就会在边界上慢慢分叉：
    同一类结论在「本轮定级」与「继承后定级」里一个算 in-scope、一个算背景发现，而表现
    只是两轮报告的等级对不上 —— 没有任何异常可查。
    """
    assert _change_paths_of is anomaly_scope.change_paths_of
    assert _ref_path is anomaly_scope.ref_path
    assert _claims_touch is anomaly_scope.claims_touch
    assert _out_of_scope is anomaly_scope.out_of_scope

