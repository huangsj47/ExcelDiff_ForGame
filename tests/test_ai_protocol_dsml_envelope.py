# -*- coding: utf-8 -*-
"""输出通道错位：模型把取数请求写成「工具调用信封」时的保守修复。

## 样本是真实原文，而且不从本文复制

三段原文取自 `ai_analysis_trace.response_text`（run 44 的 S2/S4、run 38 的 V1），
由 `tests/fixtures/dsml_tool_envelopes/_dump_from_trace.py` 连着只读 DB **二进制导出**
到同一个目录，用例从文件读。

为什么绕这一道：那段文本本身就是**被测的错误输出**，它一旦被复制/转发就会被再次当成
「调用工具的指令」解析（实测踩过），既是噪声也会把样本改形。**样本只走数据通道。**
下面的合成样本同理 —— 信封标记由**码位拼**出来，源码里不留一份可被误读的字面量。

## 判据为什么必须保守（本文件一半的用例在钉这一条）

从自由文本里认「模型想要什么」是猜；猜错的代价是平台**替模型编了一份请求** —— 它会进
白名单、会被执行、会在报告里留下没有证据的读音。所以：
  * 只吃两种形状（体是协议请求 JSON / 空体但属性里带 20 位十六进制地址）；
  * 抽不到返回 None，交回原有的纠正提示；
  * 抽出来的东西**照样**过 `sanitize_requests`，白名单一步都不少；
  * **绝不伪造 final**，**绝不**回一个空 `requests` 的 `need_more_context`
    （那会撞「need_more_context 必须给出非空 requests」，反而把纠正提示搞得更差）。

反向样本与真实样本一样多：普通 JSON、普通 markdown、未知工具名、散文体、位数不对的
地址，一律不许被误吃。
"""
from __future__ import annotations

import json
import os

import pytest

from services.ai.protocol import (
    STATUS_NEED_MORE_CONTEXT,
    ProtocolError,
    build_channel_mismatch_hint,
    build_correction_hint,
    looks_like_tool_call_envelope,
    parse_payload,
    repair_dsml_tool_calls_payload,
    sanitize_requests,
)
from services.ai.scope import AnalysisScope

FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "dsml_tool_envelopes"
)
# 四个**真实**样本（逐字，未加工）：
#  * S4：4 条 invoke，3 条体是 `file_diff`、1 条是 `evidence`（地址只有 16 位）；
#  * V1：1 条 `evidence`，地址写在属性里；
#  * S2：整段只有一个 `think`，**没有任何请求**；
#  * 汇总：6 条自闭合 `evidence`（形态最复杂的一段，用来钉「下一条的属性不被当成正文」）。
S4_SAMPLE = "run44_s4_file_diff_and_evidence.txt"
RUN38_V1_SAMPLE = "run38_v1_evidence.txt"
S2_SAMPLE = "run44_s2_think_only.txt"
SUMMARY_SAMPLE = "run45_summary_evidence_only.txt"
# 第五个真实样本（run 44 的 S5 第 1 轮）：**当前判据不覆盖**的形态 —— 参数写成
# `invoke` 的子标签（而不是体、也不是属性）。留一条用例把它钉成「已知未覆盖」，
# 免得将来有人以为信封这一形态全都救得回来。
S5_UNCOVERED_SAMPLE = "run44_s5_reference_parameters.txt"

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
PATH_A = "config/30_goods/item.xlsx"
PATH_B = "scripts/export.py"

# 信封标记里的竖线是**全角** U+FF5C（本机抓到的四个真实样本逐字如此）。
# 用码位拼，不把那段标记抄进源码 —— 抄一次就等于在仓库里又放一份「像指令的文本」。
_BAR = chr(0xFF5C)
_TAG = f"<{_BAR}{_BAR}DSML{_BAR}{_BAR}"
_TAG_END = f"</{_BAR}{_BAR}DSML{_BAR}{_BAR}"


def _raw(name: str) -> str:
    """读一段**真实原文**。二进制读，字节原样。"""
    with open(os.path.join(FIXTURE_DIR, name), "rb") as handle:
        return handle.read().decode("utf-8")


def _envelope(*bodies: str, tool: str = "x") -> str:
    """合成一段信封（**只在反向样本里用**，形状与真实样本一致）。"""
    invokes = "".join(
        f'{_TAG} invoke name="{tool}">{body}{_TAG_END} parameter>\n' for body in bodies
    )
    return f"{_TAG} calls>\n{invokes}{_TAG_END} calls>"


def _scope() -> AnalysisScope:
    return AnalysisScope.from_iterables(
        commits=(COMMIT_A, COMMIT_B),
        paths_by_commit={COMMIT_A: (PATH_A,), COMMIT_B: (PATH_B,)},
        readable_references=("incident-checklist.md",),
    )


# ==========================================================================
# 一、真实样本：能救的救回来
# ==========================================================================


def test_the_real_s4_sample_yields_its_four_requests():
    """run 44 的 S4 第 1 轮：三个 `file_diff` + 一个 `evidence`，抽得出来。

    这一轮今天的下场是「返回无法解析」→ 重问一次（花 1 轮 + 1 次纠正额度），而它
    **本来就带着四条合法请求**。抽出来就地执行，这一轮不再白花。
    """
    payload = parse_payload(repair_dsml_tool_calls_payload(_raw(S4_SAMPLE)))

    assert payload.status == STATUS_NEED_MORE_CONTEXT
    assert [request.type for request in payload.requests] == ["file_diff"] * 3 + ["evidence"], (
        "四条请求的类型与顺序就是信封里的原始顺序"
    )
    assert all(request.commit for request in payload.requests[:3]), "file_diff 带着信封里的提交"


def test_the_real_s4_sample_still_goes_through_the_whitelist():
    """抽出来的请求**照样**过白名单 —— 修复不等于放行。

    这是安全边界的落点：`repair_*` 只把文本拼回一份 JSON，越权/畸形的请求一条都不会
    因为它来自信封就被执行。这里用一个**不含**信封里那些提交的 scope，所以四条应当
    全部被记账丢掉，且「执行 + 丢掉」必须等于四条 —— 不许有条目凭空消失。
    """
    payload = parse_payload(repair_dsml_tool_calls_payload(_raw(S4_SAMPLE)))

    allowed, dropped = sanitize_requests(payload.requests, _scope())

    assert len(allowed) + len(dropped) == 4, "四条请求必须条条有下落（执行或记账）"
    assert allowed == (), "批次外的提交一条都不许执行"
    assert any("合法地址" in item.reason for item in dropped), (
        "16 位的地址必须被按畸形丢掉，理由要说清是地址的问题"
    )


def test_the_real_s4_sample_keeps_its_in_batch_requests_when_the_scope_allows_them():
    """把 scope 换成「信封里那三个提交都在批次内」后，放行 3 条、丢 1 条。

    与上一条互补：上一条证明「不能绕过校验」，这一条证明「校验通过的三条真的能执行」。
    两个方向缺一个，就会留下「全都丢了」与「全都放了」两种假绿。
    """
    payload = parse_payload(repair_dsml_tool_calls_payload(_raw(S4_SAMPLE)))
    paths_by_commit: dict[str, list[str]] = {}
    for request in payload.requests:
        if request.commit:
            paths_by_commit.setdefault(request.commit, []).append(request.path)
    scope = AnalysisScope.from_iterables(
        commits=tuple(paths_by_commit),
        paths_by_commit=paths_by_commit,
        readable_references=(),
    )

    allowed, dropped = sanitize_requests(payload.requests, scope)

    assert [request.type for request in allowed] == ["file_diff"] * 3
    assert len(dropped) == 1, "只有一个地址位数不对的 evidence 被丢"
    assert "合法地址" in dropped[0].reason


def test_the_real_evidence_only_sample_is_repaired():
    """run 38 的 V1 第 1 轮：空体、地址写在属性里 —— 抽得出一条 `evidence`。

    信封里同一行有**两个** `name`（前一个是工具名，后一个才是地址），所以这条用例真正
    钉住的是「在全部属性值里找那个地址」，而不是「取第一个 `name`」。
    """
    payload = parse_payload(repair_dsml_tool_calls_payload(_raw(RUN38_V1_SAMPLE)))

    assert payload.status == STATUS_NEED_MORE_CONTEXT
    assert [request.type for request in payload.requests] == ["evidence"]
    address = payload.requests[0].name
    assert len(address) == 20, "抽出来的必须就是属性里那个 20 位地址"

    allowed, dropped = sanitize_requests(payload.requests, _scope())
    assert [request.type for request in allowed] == ["evidence"], (
        f"这个地址形状是对的，不该被丢：{dropped}"
    )


def test_the_real_multi_invoke_sample_is_repaired():
    """run 45 的汇总第 1 轮（真实原文）：一行一条、自闭合、六条 `evidence`。

    它是**形态最复杂**的一段（自闭合标签后面直接跟下一条 invoke 的开标签，中间没有闭
    标签），所以单独钉一条：少一条就说明「下一条的属性被当成了上一条的正文」。
    """
    payload = parse_payload(repair_dsml_tool_calls_payload(_raw(SUMMARY_SAMPLE)))

    assert len(payload.requests) == 6, "六条 invoke 要一条不少地抽出来"
    assert {request.type for request in payload.requests} == {"evidence"}


# ==========================================================================
# 二、真实样本：救不了的必须老实返回 None
# ==========================================================================


def test_the_real_think_only_sample_is_not_repaired():
    """run 44 的 S2 第 1 轮：整段只有一个 `think`，壳里**没有请求**。

    这一条是「保守」的核心：它必须仍返回 None，走今天那条重问的老路 ——
    **行为逐字不变**。若哪天有人为了「降低 unparsable 轮数」去伪造一条请求，
    这条用例会红。
    """
    text = _raw(S2_SAMPLE)

    assert looks_like_tool_call_envelope(text) is True, "它确实是信封（引擎据此换纠正角度）"
    assert repair_dsml_tool_calls_payload(text) is None
    with pytest.raises(ProtocolError):
        parse_payload(text)


def test_the_real_s5_sample_is_a_known_uncovered_shape():
    """run 44 的 S5 第 1 轮：**这一形态救不了**，如实返回 None（已知边界，不是漏测）。

    它把参数写成了 `invoke` 的**子标签**（`parameter name="…"`），既不在体里、也不在
    `invoke` 的属性里 —— 而本函数的判据只吃「体是协议请求 JSON」与「属性里带 20 位地址」
    两种形状（见工作包 C 的验收口径）。所以这一轮仍然走重问，与其他两张救不回来的样本
    是同一条路。

    为什么**不**顺手支持它：从子标签里重建一条请求，就等于平台自己决定「哪个 `name`
    是类型、哪个是参数」，而这一段的字段名本来就不是协议字段（那是模型按自己的工具格式
    编的）。这一条记在这里，是为了让「覆盖到哪」在 fixture 与用例里都是一句可核的话。
    """
    text = _raw(S5_UNCOVERED_SAMPLE)

    assert looks_like_tool_call_envelope(text) is True, "它确实是信封（引擎会换纠正角度）"
    assert repair_dsml_tool_calls_payload(text) is None


def test_a_repaired_payload_never_claims_final_and_never_has_empty_requests():
    """抽到的结果**只**是 need_more_context，且一定带非空 requests。

    两件都不许发生：伪造 `final`（那会凭空造出一份结论）、回一个空 `requests` 的
    `need_more_context`（会撞协议校验，纠正提示反而更差）。
    """
    for name in (S4_SAMPLE, RUN38_V1_SAMPLE, SUMMARY_SAMPLE):
        raw = repair_dsml_tool_calls_payload(_raw(name))
        assert raw is not None, name
        body = json.loads(raw)
        assert body["status"] == STATUS_NEED_MORE_CONTEXT, name
        assert body["requests"], f"{name}：不许给空 requests"


# ==========================================================================
# 三、反向样本：不是这种病的一律不许被误吃
# ==========================================================================


def test_an_ordinary_json_answer_is_not_touched():
    """普通协议 JSON 不是信封 —— 修复函数不许碰它，也不许被认成信封。"""
    text = json.dumps(
        {
            "status": "need_more_context",
            "requests": [{"type": "file_diff", "commit": COMMIT_A, "path": PATH_A}],
        },
        ensure_ascii=False,
    )

    assert repair_dsml_tool_calls_payload(text) is None
    assert looks_like_tool_call_envelope(text) is False


def test_an_ordinary_markdown_report_is_not_touched():
    """普通 markdown 报告不是信封（它该走既有那条 markdown 路）。"""
    text = "# 变更理解\n\n改了道具表。\n\n# 影响面分析\n\n只影响道具系统。\n"

    assert repair_dsml_tool_calls_payload(text) is None
    assert looks_like_tool_call_envelope(text) is False


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("体里没有 type（不是平台协议请求）", '{"path": "a/b.xlsx"}'),
        ("体是散文", "我需要先看战斗逻辑的 diff"),
        ("体是数组", '[{"type": "file_diff", "commit": "x", "path": "y"}]'),
        ("体是空串", ""),
        ("体是数字", "42"),
    ],
)
def test_an_unrecognised_invoke_body_is_not_guessed(label, body):
    """未知工具名 / 非协议体一律不猜 —— 返回 None，交回纠正提示。

    这一组是「绝不伪造请求」的样例集：只要体里判不出「平台认得的那一条请求」，
    平台就**没有**依据说模型想要哪条数据。
    """
    text = _envelope(body, tool="get_file_diff")

    assert repair_dsml_tool_calls_payload(text) is None, label


def test_an_empty_body_without_a_real_address_is_not_guessed():
    """空体只在「属性里有一个 20 位十六进制地址」时才认 —— 其它属性一律不猜。

    地址位数不对的那种（16 位）在真实样本里出现过，它**能被抽出来但会被白名单丢掉**；
    而这里这条是压根不成形状的（比如工具名后面什么都没有），必须返回 None。
    """
    assert repair_dsml_tool_calls_payload(_envelope("", tool="evidence")) is None
    assert repair_dsml_tool_calls_payload(_envelope("", tool="get_evidence")) is None


def test_an_empty_body_with_a_short_address_is_not_guessed():
    """空体 + **位数不对**的地址 → 不抽（真样本里那个 16 位的地址走的是另一条路）。

    这条钉的是判据的**严格**：`evidence` 的地址是「整条属性值 = 20 位十六进制」。
    放宽成「里面有 20 位」就会把 `commit="<40 位提交号>"` 的前 20 位当成地址，
    凭空造出一条**形状合法**的 `evidence` 请求 —— 它能过白名单，执行层只会回一句
    「取不到」，而报告读起来像模型自己要过这一份。
    """
    text = f'{_TAG} calls>\n{_TAG} invoke name="evidence" name="095950227e0a5051" />\n{_TAG_END} calls>'

    assert repair_dsml_tool_calls_payload(text) is None


def test_a_commit_id_in_an_attribute_is_not_turned_into_an_evidence_request():
    """`commit="<40 位提交号>"` 不是地址 —— 不许取它前 20 位当地址。

    这是「绝不替模型编请求」最容易被破的一处：提交号本身就是十六进制，取前 20 位
    得到的地址**形状完全合法**，白名单拦不住它。
    """
    text = (
        f"{_TAG} calls>\n"
        f'{_TAG} invoke name="commit_detail" commit="{COMMIT_A}" />\n'
        f"{_TAG_END} calls>"
    )

    assert repair_dsml_tool_calls_payload(text) is None


def test_a_final_shaped_body_is_not_read_as_a_request():
    """体是一份完整 payload（`status` 在里面而不是 `type`）时不算「协议请求」。

    真实样本里出现过这种形态（run 45 的汇总第 30 轮，`invoke` 名是 `request`），
    而那一轮**本来就解析成功了** —— 首尾大括号切片就能取出里面那份 JSON。所以这里
    不是缺口，用不着（也不该）为它扩判据：多认一种形状就是多一个猜错的地方。
    """
    text = _envelope(
        json.dumps(
            {
                "status": "need_more_context",
                "requests": [{"type": "evidence", "name": "a" * 20}],
            },
            ensure_ascii=False,
        ),
        tool="request",
    )

    assert repair_dsml_tool_calls_payload(text) is None


# ==========================================================================
# 四、纠正提示不再回显那段信封
# ==========================================================================


def test_the_correction_hint_does_not_echo_the_envelope():
    """错误原文里带着信封时，纠正提示换成**一句正面事实**，不回显原文。

    旧行为是把模型的错误信封原样抄回去（`ai_analysis_trace.correction_hint` 里存着逐字
    的原文，run 44 的三轮都如此）—— 对这种病等于**又示范了一遍**。所以这里两个方向都钉：
    信封那几个字不许出现，正面事实必须在场。
    """
    # 引擎抛出的正是这句话（`parse_payload` 里那一句），原文照抄它的形状。
    error = ProtocolError(f"回答里没有可解析的 JSON。原文开头：{_raw(S2_SAMPLE)[:200]}")

    hint = build_correction_hint(error)

    assert "DSML" not in hint, "那段信封又被抄回提示词里了"
    assert "没有可以直接调用的 API 工具" in hint
    assert "requests" in hint, "正面事实必须说清「这些名字写在 requests 里」"


def test_a_normal_error_still_carries_its_own_text():
    """反向：普通协议错误照旧把它自己那句话带上（不许把有用信息一起抹掉）。"""
    hint = build_correction_hint(ProtocolError("status 必须是 ('need_more_context', 'final') 之一"))

    assert "status 必须是" in hint


def test_the_channel_hint_says_the_fact_without_naming_the_wrong_writing():
    """通道错位的提示：只讲事实，不复述那种写法。

    一个具体的坑：旧的 `build_correction_hint` 里写着「不要输出 `<think>` 块」，而 `think`
    恰恰是实测出现过的 `invoke` 名（run 44 的 S2）—— **写「禁止 X」会把 X 的字面量送进
    上下文**。这条用例把同一个坑钉在提示词这一侧。
    """
    hint = build_channel_mismatch_hint()

    assert "没有可以直接调用的 API 工具" in hint
    for literal in ("DSML", "invoke", "think", "get_file_diff"):
        assert literal not in hint, f"提示里出现了 {literal} —— 等于又示范了一遍"


def test_the_envelope_predicate_only_fires_on_the_envelope():
    """`looks_like_tool_call_envelope` 是引擎换账本/换措辞的判据，必须窄。"""
    for name in (S2_SAMPLE, S4_SAMPLE, RUN38_V1_SAMPLE, SUMMARY_SAMPLE, S5_UNCOVERED_SAMPLE):
        assert looks_like_tool_call_envelope(_raw(name)) is True, name

    for text in ("", "   ", "{" , "报告正文", "# 变更理解\n\nx\n\n# 影响面分析\n\ny\n"):
        assert looks_like_tool_call_envelope(text) is False, repr(text)
