"""作用域校验与协议解析 —— 模型输出进入系统前的两道闸门。

## 两条最要紧的性质

1. **接地校验逐条丢弃，不作废整单**。模型编一个 commit、或引用一个该提交没改过的
   文件，是常见错误。为这一条作废其余 9 条正确结论，代价太大；但不校验又会让前端
   渲染出点不开的链接、让跟进的人查不到东西。所以是「丢掉那一条 + 记账」。
2. **工具白名单默认拒绝**。这是「模型不能诱导服务端读任意文件」的落点 —— 没有它，
   把 `.env` 之类的内容拉进模型上下文只是改一个 `path` 字段的事。

下面每条守卫都配了反向用例，避免「全绿但没在测东西」。
"""

from __future__ import annotations

import re

import pytest

from services.ai.protocol import (
    AnalysisPayload,
    ContextRequest,
    ProtocolError,
    build_budget_exhausted_hint,
    build_correction_hint,
    ground_payload,
    looks_like_markdown_report,
    parse_json_candidates,
    parse_payload,
    sanitize_requests,
)
from services.ai.scope import AnalysisScope, normalize_path
from services.ai.skill_contract import DIMENSION_IDS

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
PATH_A = "config/30_goods/item.xlsx"
PATH_B = "scripts/export.py"


def _scope() -> AnalysisScope:
    return AnalysisScope.from_iterables(
        commits=(COMMIT_A, COMMIT_B),
        paths_by_commit={COMMIT_A: (PATH_A,), COMMIT_B: (PATH_B,)},
        readable_references=("incident-checklist.md",),
    )


def _final_payload(**overrides) -> dict:
    payload = {
        "status": "final",
        "report_markdown": "# 变更理解\n内容\n# 风险评估\n内容\n",
        "dimensions": [{"id": "config_id", "hit": False, "note": "本次未涉及配表"}],
        "anomalies": [],
    }
    payload.update(overrides)
    return payload


def _anomaly(**overrides) -> dict:
    anomaly = {
        "title": "【配表】道具 ID 被删除",
        "category": "config_id",
        "severity": "critical",
        "confidence": "high",
        "evidence": ["30_goods/item.xlsx 第 12 行 100012 被删除"],
        "commit": COMMIT_A,
        "file_path": PATH_A,
    }
    anomaly.update(overrides)
    return anomaly


# --------------------------------------------------------------------------
# 路径归一
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a/b.xlsx", "a/b.xlsx"),
        ("./a/b.xlsx", "a/b.xlsx"),
        ("././a/b.xlsx", "a/b.xlsx"),
        ("a\\b.xlsx", "a/b.xlsx"),
        ('  "a/b.xlsx"  ', "a/b.xlsx"),
        ("'a/b.xlsx'", "a/b.xlsx"),
        ("", ""),
    ],
)
def test_path_normalization(raw, expected):
    """模型常把分隔符写成反斜杠、加 `./` 前缀、或带引号。

    这些写法差异不改变它指的是哪个文件，所以归一化后再比对，而不是因为写法不同
    就判成越权（那会造成「模型明明指对了却被拒」的假失败）。
    """
    assert normalize_path(raw) == expected


# --------------------------------------------------------------------------
# commit 解析
# --------------------------------------------------------------------------


def test_full_hash_resolves():
    assert _scope().resolve_commit(COMMIT_A) == COMMIT_A


def test_case_insensitive_resolution():
    assert _scope().resolve_commit(COMMIT_A.upper()) == COMMIT_A


def test_unique_short_prefix_resolves():
    assert _scope().resolve_commit("aaaaaa") == COMMIT_A


def test_ambiguous_prefix_is_rejected():
    """前缀匹配到多个时返回 None，**不猜**。

    猜错的后果比丢弃更糟：异常条目会挂到另一个提交上，跟进的人按那个提交去查，
    查到的是完全不相关的改动。
    """
    scope = AnalysisScope.from_iterables(
        commits=("abc111" + "0" * 34, "abc222" + "0" * 34),
        paths_by_commit={},
        readable_references=(),
    )
    assert scope.resolve_commit("abc") is None
    assert scope.resolve_commit("abc1") == "abc111" + "0" * 34


@pytest.mark.parametrize("raw", ["", "   ", "zzz", "abc", COMMIT_A[:2]])
def test_unresolvable_commits_return_none(raw):
    """太短的前缀（<4 位）匹配面太大，没有意义，直接判不合法。"""
    assert _scope().resolve_commit(raw) is None


# --------------------------------------------------------------------------
# 路径作用域
# --------------------------------------------------------------------------


def test_path_belonging_to_the_commit_is_allowed():
    assert _scope().path_allowed(COMMIT_A, PATH_A)


def test_path_from_another_commit_is_rejected():
    """跨提交引用要拒：那说明模型把两个提交的改动记混了。"""
    assert not _scope().path_allowed(COMMIT_A, PATH_B)


def test_unknown_path_is_rejected():
    assert not _scope().path_allowed(COMMIT_A, "../../.env")


def test_empty_path_is_rejected():
    assert not _scope().path_allowed(COMMIT_A, "")


# --------------------------------------------------------------------------
# JSON 容错解析
# --------------------------------------------------------------------------


def test_parses_plain_json():
    assert parse_json_candidates('{"a": 1}') == [{"a": 1}]


def test_parses_json_wrapped_in_think_block():
    """模型经常把推理过程一起输出。协议里禁止了它，但解析层仍要兜住 —— 两层都保留。"""
    text = '<think>先看配表…</think>\n{"status": "final"}'
    assert {"status": "final"} in parse_json_candidates(text)


def test_parses_json_inside_a_code_fence():
    text = '```json\n{"status": "final"}\n```'
    assert {"status": "final"} in parse_json_candidates(text)


def test_parses_json_surrounded_by_prose():
    text = '好的，下面是结果：\n{"status": "final"}\n以上。'
    assert {"status": "final"} in parse_json_candidates(text)


def test_parses_json_with_nested_braces():
    """从首尾大括号切片时不能把嵌套的内层当成整体。"""
    text = '前面的话 {"a": {"b": [1, 2]}, "c": "}"} 后面的话'
    assert {"a": {"b": [1, 2]}, "c": "}"} in parse_json_candidates(text)


@pytest.mark.parametrize("text", ["", "   ", "完全不是 JSON 的一段话"])
def test_no_candidates_for_unusable_text(text):
    assert parse_json_candidates(text) == []


# --------------------------------------------------------------------------
# 协议校验
# --------------------------------------------------------------------------


def test_parses_a_valid_need_more_context():
    payload = parse_payload(
        '{"status": "need_more_context", "reason": "需要看具体改动",'
        ' "requests": [{"type": "file_diff", "commit": "abc", "path": "a.xlsx"}]}'
    )
    assert payload.status == "need_more_context"
    assert payload.requests[0].type == "file_diff"
    assert not payload.is_final


def test_parses_a_valid_final():
    """注意用 `json.dumps` 构造，而不是手写带 `\\n` 的字符串字面量。

    手写时 Python 会先把 `\n` 变成真换行，于是 JSON 字符串里出现裸控制字符、
    整体不再合法 —— 那样测的是「解析器能不能救回非法 JSON」，而不是本用例要测的
    「合法 final 能被解析」。第一版就踩了这个。
    """
    import json

    payload = parse_payload(json.dumps(_final_payload(), ensure_ascii=False))

    assert payload.is_final
    assert payload.report_markdown.startswith("# 变更理解")
    assert payload.dimensions[0].id == "config_id"


def test_unknown_status_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        parse_payload('{"status": "maybe"}')


def test_missing_status_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        parse_payload('{"report_markdown": "x"}')


def test_need_more_context_without_requests_is_a_protocol_error():
    """要上下文却不说要什么，这一轮就是空转。"""
    with pytest.raises(ProtocolError):
        parse_payload('{"status": "need_more_context", "requests": []}')


def test_final_without_a_report_is_a_protocol_error():
    with pytest.raises(ProtocolError):
        parse_payload('{"status":"final","dimensions":[{"id":"config_id","hit":false}]}')


def test_final_without_dimensions_is_a_protocol_error():
    """dimensions 是「每个维度都过了一遍」的证据。

    允许它为空等于允许模型只挑好说的说 —— 而这正是这个字段要防的事。
    """
    with pytest.raises(ProtocolError):
        parse_payload('{"status":"final","report_markdown":"# 变更理解\nx"}')


def test_the_correction_hint_never_disagrees_with_the_dimension_list():
    """纠正提示那句「维度都要写」**不许出现任何字面条数**。

    它写死过「六个」（SKILL 与运行期契约都是九个），而且**不会有人发现**：服务端照样
    接受九个，只是模型被提示「写六个就算齐了」——少写的那三个维度在报告里永远没有留痕，
    而所有用例都是绿的。

    后来它改成 `len(DIMENSION_IDS)`（平台**出厂**的维度数），仍然会在项目声明了自己的
    清单时对不上：模型从提示词里读到 12 个维度，纠正提示却说「9 个维度都要写」——
    **校验按 A、提示词按 B**，同样不会报错。所以现在这句话**指回模型手里那份清单**
    （系统提示词里的那两节），一个数字都不写。
    """
    hint = build_correction_hint(ProtocolError("dimensions 缺了"))

    assert "维度清单" in hint, hint
    for number in ("六个维度", "九个维度", "9 个维度", "12 个维度"):
        assert number not in hint, f"维度条数又写死了：{hint}"
    assert not re.search(r"\d+\s*个维度", hint), f"纠正提示里出现了字面条数：{hint}"


def test_structural_errors_on_a_list_field_are_protocol_errors():
    with pytest.raises(ProtocolError):
        parse_payload('{"status":"final","report_markdown":"x","dimensions":{},"anomalies":[]}')


# --------------------------------------------------------------------------
# 条目级丢弃（不是整单失败）
# --------------------------------------------------------------------------


def _single_anomaly_payload(anomaly: dict) -> AnalysisPayload:
    import json

    return parse_payload(json.dumps(_final_payload(anomalies=[anomaly]), ensure_ascii=False))


@pytest.mark.parametrize(
    ("label", "override"),
    [
        ("缺标题", {"title": ""}),
        ("severity 不在两档内", {"severity": "medium"}),
        ("confidence 未达门槛", {"confidence": "low"}),
        ("evidence 为空列表", {"evidence": []}),
        ("evidence 为空字符串", {"evidence": "   "}),
    ],
)
def test_items_that_fail_the_schema_are_dropped_not_fatal(label, override):
    """条目级问题只丢那一条，**协议本身仍然成立**。

    这些是最容易被写成「抛异常」的地方，而那样做的代价是：一条缺证据的条目会让
    整轮 10 条结论一起作废，还要多花一轮重问。

    **`category 不认识` 刻意不在这个参数表里**：它已经不属于「该丢的那几条」了 ——
    见下面 `test_an_unknown_category_is_kept_not_dropped`。它曾经在这里，而那正是
    「换一个项目、真实的发现被静默丢掉」的入口。
    """
    payload = _single_anomaly_payload(_anomaly(**override))

    assert payload.anomalies == ()
    assert payload.dropped, f"{label} 应当被记账，否则「为什么少了一条」无法解释"
    assert any(item.kind == "anomaly" for item in payload.dropped)


def test_a_fully_valid_anomaly_is_kept():
    """反向自检：上面的丢弃不能是无差别丢弃。"""
    payload = _single_anomaly_payload(_anomaly())

    assert len(payload.anomalies) == 1
    assert payload.dropped == ()
    assert payload.anomalies[0].evidence


def test_evidence_accepts_both_string_and_list():
    as_list = _single_anomaly_payload(_anomaly(evidence=["一", "二"]))
    as_string = _single_anomaly_payload(_anomaly(evidence="只有一条"))

    assert as_list.anomalies[0].evidence == ("一", "二")
    assert as_string.anomalies[0].evidence == ("只有一条",)


def test_dimension_with_unknown_id_is_kept_not_dropped():
    """`dimensions[]` 里认不出来的 id **不丢** —— 项目声明了自己的清单时那是它该写的 id。

    原先这里断言的是「丢掉 made_up」。那在「维度是平台写死的九个」时看着无害，
    可维度清单是项目可声明的（`LoadedSkills.dimensions`）：一个声明了 `performance` 的
    项目，模型按提示词写了 `performance`，而这一层按平台出厂值把它丢掉 ——
    提示词要求它交代的维度，在「逐一交代」表里反而看不到，报告读起来完全正常。
    """
    import json

    payload = parse_payload(
        json.dumps(
            _final_payload(
                dimensions=[
                    {"id": "config_id", "hit": True, "note": "命中"},
                    {"id": "made_up", "hit": True, "note": "编的"},
                ]
            ),
            ensure_ascii=False,
        )
    )
    assert [item.id for item in payload.dimensions] == ["config_id", "made_up"]
    assert payload.dropped == ()


def test_a_dimension_entry_without_an_id_is_still_dropped():
    """空 id 不是「归错组」，而是这一条根本没说它是什么维度（结构问题）—— 丢掉并记账。"""
    import json

    payload = parse_payload(
        json.dumps(
            _final_payload(dimensions=[{"id": "", "hit": True}, {"id": "process", "hit": False}]),
            ensure_ascii=False,
        )
    )
    assert [item.id for item in payload.dimensions] == ["process"]
    assert any(item.kind == "dimension" for item in payload.dropped)


def test_an_unknown_category_is_kept_not_dropped():
    """**任何情况下都不许静默丢掉一条发现。**

    模型给了一个落不进本次生效清单的 category（换项目、换维度时必然发生）时，
    原先那一条异常**从报告里彻底消失**：报告看起来完全正常，只是少了一条，
    异常清单、报告正文里都没有它，只剩 trace 里一条谁都看不到的记录。

    现在的口径：category 只决定它归到哪一组，不决定它留不留下 —— 按原样保留
    （原始 category 一个字不改），由知道清单的那一层归到「未归类」并单独列出来。
    """
    payload = _single_anomaly_payload(_anomaly(category="performance"))

    assert len(payload.anomalies) == 1
    assert payload.anomalies[0].category == "performance", "原始 category 必须原样保留"
    assert payload.anomalies[0].evidence, "它自己的证据也要留着（没有证据的发现无法跟进）"


def test_an_anomaly_without_a_category_is_kept_and_recorded():
    """连 category 都没写也不丢：没有归属的**发现**仍然是发现。

    账里要留下一条记录（说明它在报告里会显示成「未归类」），否则「这一条为什么不属于
    任何维度」无从解释。
    """
    payload = _single_anomaly_payload(_anomaly(category=""))

    assert len(payload.anomalies) == 1
    assert payload.anomalies[0].category == ""
    assert any(item.kind == "unclassified" for item in payload.dropped)


def test_dropped_records_carry_the_offending_value():
    """记下被丢弃的值，才能回答「模型到底写了什么」。

    category 那一格**不再是被丢弃的值**（它现在留在异常自己身上），所以这里断的是
    「被保留的那一条仍然带着模型写的原始值」—— 与上面那条一致性判据同一件事：
    读报告的人要能看出模型写了什么，而不是看到一个被平台改写过的值。
    """
    payload = _single_anomaly_payload(_anomaly(category="bogus"))

    assert payload.anomalies[0].category == "bogus"


# --------------------------------------------------------------------------
# 接地校验
# --------------------------------------------------------------------------


def test_fabricated_commit_is_dropped():
    payload = _single_anomaly_payload(_anomaly(commit="d" * 40))
    grounded = ground_payload(payload, _scope())

    assert grounded.anomalies == ()
    assert any("commit" in item.reason for item in grounded.dropped)


def test_grounding_rewrites_a_short_prefix_to_the_full_hash():
    """短前缀在前端点不开，也没法用于去重，所以命中后要回写成全哈希。"""
    payload = _single_anomaly_payload(_anomaly(commit="aaaaaa"))
    grounded = ground_payload(payload, _scope())

    assert grounded.anomalies[0].commit == COMMIT_A


def test_file_path_not_changed_by_that_commit_is_dropped():
    payload = _single_anomaly_payload(_anomaly(file_path=PATH_B))
    grounded = ground_payload(payload, _scope())

    assert grounded.anomalies == ()
    assert any("file_path" in item.reason for item in grounded.dropped)


def test_anomaly_without_a_file_path_is_kept():
    """有些异常是全局性的（例如「本次改动没走配表评审流程」），没有具体文件。"""
    payload = _single_anomaly_payload(_anomaly(file_path=""))
    grounded = ground_payload(payload, _scope())

    assert len(grounded.anomalies) == 1


def test_grounding_keeps_the_valid_items_when_others_are_dropped():
    """**这条是接地校验的核心性质**：不因为一条坏条目作废其余结论。"""
    import json

    payload = parse_payload(
        json.dumps(
            _final_payload(
                anomalies=[
                    _anomaly(),
                    _anomaly(commit="d" * 40),
                    _anomaly(title="第二条有效", file_path=PATH_A),
                ]
            ),
            ensure_ascii=False,
        )
    )
    grounded = ground_payload(payload, _scope())

    assert [item.title for item in grounded.anomalies] == [
        "【配表】道具 ID 被删除",
        "第二条有效",
    ]
    assert len(grounded.dropped) == 1


def test_grounding_preserves_the_earlier_dropped_records():
    """接地阶段不该把解析阶段已经记下的丢弃原因冲掉。"""
    payload = _single_anomaly_payload(_anomaly(confidence="low"))
    grounded = ground_payload(payload, _scope())
    assert grounded.dropped == payload.dropped


# --------------------------------------------------------------------------
# 工具白名单
# --------------------------------------------------------------------------


def test_sanitize_allows_an_in_scope_request():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_diff", commit="aaaaaa", path=PATH_A)], _scope()
    )
    assert len(allowed) == 1
    assert allowed[0].commit == COMMIT_A
    assert dropped == ()


def test_sanitize_rejects_an_unknown_type():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="read_secret_file", commit=COMMIT_A, path=PATH_A)], _scope()
    )
    assert allowed == ()
    assert "type" in dropped[0].reason


def test_sanitize_rejects_a_path_outside_the_commit():
    """这是「模型不能诱导服务端读任意文件」的那一步。"""
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit=COMMIT_A, path="../../.env")], _scope()
    )
    assert allowed == ()
    assert "path" in dropped[0].reason


def test_sanitize_rejects_a_commit_outside_the_batch():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="commit_detail", commit="d" * 40)], _scope()
    )
    assert allowed == ()
    assert "commit" in dropped[0].reason


def test_sanitize_rejects_an_unreadable_reference():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="read_reference", name="../../etc/passwd")], _scope()
    )
    assert allowed == ()
    assert "可读清单" in dropped[0].reason


def test_sanitize_allows_a_readable_reference():
    allowed, _dropped = sanitize_requests(
        [ContextRequest(type="read_reference", name="incident-checklist.md")], _scope()
    )
    assert [item.name for item in allowed] == ["incident-checklist.md"]


def test_sanitize_dedupes_within_one_round():
    """同一轮里重复索要同一个文件只执行一次，避免白白吃掉预算。"""
    request = ContextRequest(type="file_diff", commit=COMMIT_A, path=PATH_A)
    allowed, dropped = sanitize_requests([request, request, request], _scope())
    assert len(allowed) == 1
    assert dropped == ()


def test_sanitize_accepts_a_commit_detail_without_a_path():
    allowed, _dropped = sanitize_requests(
        [ContextRequest(type="commit_detail", commit=COMMIT_A)], _scope()
    )
    assert [item.type for item in allowed] == ["commit_detail"]


def test_sanitize_drops_a_file_request_without_a_path():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_diff", commit=COMMIT_A, path="")], _scope()
    )
    assert allowed == ()
    assert "path" in dropped[0].reason


def test_sanitize_keeps_good_requests_when_others_are_bad():
    """与接地校验同一性质：坏请求不该作废整轮。"""
    allowed, dropped = sanitize_requests(
        [
            ContextRequest(type="file_diff", commit=COMMIT_A, path=PATH_A),
            ContextRequest(type="file_diff", commit=COMMIT_A, path="../../.env"),
        ],
        _scope(),
    )
    assert len(allowed) == 1
    assert len(dropped) == 1


# --------------------------------------------------------------------------
# 健康检查与提示语
# --------------------------------------------------------------------------


def test_looks_like_a_report_when_enough_sections_are_present():
    assert looks_like_markdown_report("# 变更理解\nx\n# 风险评估\ny\n")


def test_one_section_is_not_enough():
    assert not looks_like_markdown_report("# 变更理解\nx\n")


@pytest.mark.parametrize("text", ["", "   ", "就一句话"])
def test_obviously_non_report_text_is_rejected(text):
    assert not looks_like_markdown_report(text)


def test_section_headings_inside_a_fence_still_count():
    """围栏里的章节标题**也算**，这是刻意的。

    这个判定只在「解析失败」时兜底。而模型最可能的失败形态恰恰是把整份 JSON 包在
    代码围栏里没闭合、或者只在末尾被截断——那种情况下 `report_markdown` 字段里的
    标题是**真实存在的一份报告**，把它判成「不像报告」会让用户白等一场。
    """
    assert looks_like_markdown_report("```json\n{\"report_markdown\": \"# 变更理解\\n# 风险评估\"}\n")


def test_correction_hint_restates_the_protocol():
    hint = build_correction_hint(ProtocolError("status 不认识"))
    assert "status 不认识" in hint
    assert "json.loads" in hint
    assert "<think>" in hint
    assert "dimensions" in hint


def test_budget_hint_forces_convergence_without_failing():
    """预算耗尽时注入收敛指令，而不是让整轮失败 —— 模型手上的证据通常够写报告了。"""
    hint = build_budget_exhausted_hint()
    assert "final" in hint
    assert "禁止继续请求上下文" in hint


def test_dimension_ids_used_in_tests_are_the_real_ones():
    """防止本文件里的示例用了不存在的维度 id 而让上面几条变成空转。

    这个数量断言是**有意的 tripwire**：加维度时它会红，逼你回来确认本文件的示例
    （`"id": "config_id"`、`category="..."`）是不是也该覆盖新维度。数字本身不是重点，
    「停下来看一眼」才是。9 个维度是 2026-09-18 加入 `value_sanity` 之后的数
    （此前 8 个是同日加入 `config_data`、7 个是加入 `module_coupling` 之后的数）。
    """
    assert len(DIMENSION_IDS) == 9
    assert "config_id" in DIMENSION_IDS
    assert "config_data" in DIMENSION_IDS
    assert "value_sanity" in DIMENSION_IDS
