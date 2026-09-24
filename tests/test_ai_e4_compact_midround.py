# -*- coding: utf-8 -*-
"""E4：紧凑中间协议 + 输出截断可观测。

## 这一份盯的是三件事

**1. 中间轮只留四个字段。** `need_more_context` 那一轮存在的意义只有一个：把「我还需要
什么」交回来并**开始下一轮**。可解析层原先照样解析 `report_markdown` / `anomalies` /
`dimensions` / `candidate_dispositions` —— 模型在中间轮顺手写一整份报告，平台**不拦不丢**
（那些字段对中间轮从来没有读者），只白花输出 token。现在它们被丢弃并**逐条记账**：
`dropped` 里出现一条 `mid_round_field`，读 trace 的人看得到「它这一轮本来想说什么」。

判据是「非空才算」。`"anomalies": []` 是模型的正当写法（这一轮没发现），把它记成
「写了报告被丢弃」是假账。

**2. 输出被截断时，**上游说的那句话**比我们自己猜准。** `finish_reason == "length"` 是
上游直接告诉我们「这次输出撞到上限了」。原先它只被**存储**（`RoundRecord` → trace），
从来没人读：截断检测靠 `looks_like_truncated_json`（括号配平）。于是有一种残结果会被
当成正常结果收下 —— 断点恰好落在**一个仍然合法的 JSON** 上（`requests` 数组已闭合、
后面的字段整段没写），`json.loads` 成功、括号也配平，而它其实只有半句话。这一份测试
把这条漏洞钉住：同一个可解析的中间轮回答，`finish_reason="length"` 时必须**先走截断
纠正**（要求压短后重发），而不是照单执行它那半份 `requests`。

**3. `reason` 有硬上限，`reason_code` 是它该有的形态。** `reason` 原先是一个**没有长度
约束**的自由文本字段，而平台自己在 SKILL.md 与提示词里反复要求模型「把判断写进
`reason`」—— 长 `reason` 是平台自己要来的。收紧的办法是**换字段**（写 `reason_code`，
`reason` 只留一行可选补充）而不是硬砍：砍掉的正是它让你说的那句话。真的超了上限时
**截断并记账**，不静默截断。

## 不在这份里的

E4 验收的另一半是「critical recall 与证据可定位率不下降」，而金标不存在
（`run22.json` 的 `issues` 是 `[]`、`labeled_by` 是 `""`，`metrics._ratio` 分母为 0 时
返回 `None`）—— 那一半目前**算不出来**，既有测试与这一份都不声称它达成了。
"""
from __future__ import annotations

import json
from pathlib import Path

from services.ai.engine import (
    EngineLimits,
    RoundRecord,
    run_analysis,
)
from services.ai.llm_client import ChatResult
from services.ai.protocol import (
    EVIDENCE_REQUEST_TYPE,
    REASON_MAX_CHARS,
    TRUNCATED_OUTPUT_HINT,
    ContextRequest,
    DroppedItem,
    ProtocolError,
    parse_payload,
    sanitize_requests,
)
from services.ai.rules import RuleThresholds
from services.ai.scope import AnalysisScope
from services.ai.skill_loader import LoadedSkills, SkillDocument

COMMIT = "a" * 40
TABLE = "config/[30]道具表_CfgItem.xlsx"


# --------------------------------------------------------------------------
# 1. 中间轮只留四个字段
# --------------------------------------------------------------------------


def _mid_round_payload(**extra) -> str:
    body = {
        "status": "need_more_context",
        "reason": "先看道具表的改动",
        "requests": [{"type": "file_diff", "commit": COMMIT, "path": TABLE}],
    }
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


def test_midround_keeps_only_status_reason_reason_code_and_requests():
    """中间轮顺手写的报告整份丢弃，并逐条记账 —— 不静默消失。"""
    payload = parse_payload(
        _mid_round_payload(
            report_markdown="# 变更理解\n\n这是一整份报告，但这一轮不该写它。",
            anomalies=[
                {
                    "title": "越界的取值",
                    "category": "value_sanity",
                    "severity": "high",
                    "confidence": "high",
                    "evidence": ["道具表第 12 行"],
                }
            ],
            dimensions=[{"id": "value_sanity", "hit": True, "note": "写了"}],
            candidate_dispositions=[{"candidate_id": "S1-1", "status": "adopted"}],
        )
    )

    assert payload.status == "need_more_context"
    assert payload.reason == "先看道具表的改动"
    assert len(payload.requests) == 1
    # 中间轮的这四个字段**没有读者**，所以一个都不留。
    assert payload.report_markdown == ""
    assert payload.anomalies == ()
    assert payload.dimensions == ()
    assert payload.candidate_dispositions == ()

    kinds = [item.kind for item in payload.dropped]
    assert kinds.count("mid_round_field") == 4, payload.dropped
    reasons = " ".join(item.reason for item in payload.dropped)
    for name in ("report_markdown", "anomalies", "dimensions", "candidate_dispositions"):
        assert name in reasons, name


def test_midround_records_what_it_dropped_not_just_that_it_dropped():
    """记账里要有**内容体量**，否则读的人看不出「它本来想说什么」。"""
    payload = parse_payload(
        _mid_round_payload(report_markdown="x" * 500, anomalies=[{}, {}])
    )

    details = {item.reason: item.detail for item in payload.dropped}
    assert any("500" in detail for detail in details.values()), payload.dropped
    assert any("2" in detail for detail in details.values()), payload.dropped


def test_empty_midround_fields_are_not_recorded_as_dropped():
    """空数组不是「写了报告」—— 记成丢弃就是假账。"""
    payload = parse_payload(
        _mid_round_payload(anomalies=[], dimensions=[], report_markdown="   ")
    )

    assert [item for item in payload.dropped if item.kind == "mid_round_field"] == []


def test_a_clean_midround_records_nothing_at_all():
    payload = parse_payload(_mid_round_payload())

    assert payload.dropped == ()


def test_a_final_round_still_parses_all_of_them():
    """收窄**只管中间轮** —— 最终轮必须照旧拿到全部字段（差一点就是整份结论丢掉）。"""
    payload = parse_payload(
        json.dumps(
            {
                "status": "final",
                "reason": "看完了",
                "report_markdown": "# 变更理解\n\n正文",
                "dimensions": [{"id": "config_id", "hit": False, "note": "没命中"}],
                "anomalies": [
                    {
                        "title": "删除了已放出的 ID",
                        "category": "config_id",
                        "severity": "high",
                        "confidence": "high",
                        "evidence": ["道具表第 12 行"],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    assert payload.report_markdown
    assert len(payload.anomalies) == 1
    assert len(payload.dimensions) == 1
    assert [item for item in payload.dropped if item.kind == "mid_round_field"] == []


# --------------------------------------------------------------------------
# 2. reason_code 与 reason 的硬上限
# --------------------------------------------------------------------------


def test_reason_code_is_parsed_and_kept_short():
    payload = parse_payload(_mid_round_payload(reason_code="need_config_pair"))

    assert payload.reason_code == "need_config_pair"


def test_reason_code_defaults_to_empty():
    assert parse_payload(_mid_round_payload()).reason_code == ""


def test_reason_code_over_the_limit_is_truncated_and_recorded():
    payload = parse_payload(_mid_round_payload(reason_code="x" * 200))

    assert len(payload.reason_code) < 200
    assert [item.kind for item in payload.dropped if item.kind == "reason_code"]


def test_reason_within_the_limit_is_untouched():
    text = "x" * REASON_MAX_CHARS
    payload = parse_payload(_mid_round_payload(reason=text))

    assert payload.reason == text
    assert payload.dropped == ()


def test_overlong_reason_is_truncated_and_recorded():
    """超长 `reason` **截断 + 记账**，不静默截断（静默截断读起来像模型只写了这么点）。"""
    payload = parse_payload(_mid_round_payload(reason="判" * (REASON_MAX_CHARS + 300)))

    assert len(payload.reason) == REASON_MAX_CHARS
    dropped = [item for item in payload.dropped if item.kind == "reason"]
    assert len(dropped) == 1, payload.dropped
    assert str(REASON_MAX_CHARS + 300) in dropped[0].reason, dropped[0]


# --------------------------------------------------------------------------
# 3. 按 evidence_id 取证据的请求类型
# --------------------------------------------------------------------------

EVIDENCE_ID = "0123456789abcdef0123"


def _scope() -> AnalysisScope:
    return AnalysisScope(
        commits=(COMMIT,),
        paths_by_commit={COMMIT: frozenset({TABLE})},
        readable_references=frozenset(),
    )


def test_the_evidence_request_type_survives_sanitize():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="evidence", name=EVIDENCE_ID)], _scope()
    )

    assert dropped == ()
    assert len(allowed) == 1
    assert allowed[0].type == "evidence"
    assert allowed[0].name == EVIDENCE_ID


def test_a_malformed_evidence_id_is_rejected_with_a_reason():
    """id 的形状是平台的约定（sha256 前 20 位十六进制），不像它的按畸形丢掉。"""
    allowed, dropped = sanitize_requests(
        [
            ContextRequest(type="evidence", name="not-an-id"),
            ContextRequest(type="evidence", name=EVIDENCE_ID[:-1]),
            ContextRequest(type="evidence", name=EVIDENCE_ID.replace("0", "g")),
            ContextRequest(type="evidence", name=""),
        ],
        _scope(),
    )

    assert allowed == ()
    assert len(dropped) == 4
    assert all(item.kind == "request" for item in dropped)


def test_an_uppercase_evidence_id_is_normalised_not_rejected():
    """大小写不是「形状不对」：地址是十六进制，平台统一成小写再查。

    判成畸形会让模型因为「把 ID 全大写抄了一遍」而拿不到内容 —— 那不是它的错，
    而它拿到的理由（「形状不对」）也指不到真正的原因上。
    """
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="evidence", name=EVIDENCE_ID.upper())], _scope()
    )

    assert dropped == ()
    assert [item.name for item in allowed] == [EVIDENCE_ID]


def test_a_duplicate_evidence_request_runs_once():
    allowed, _dropped = sanitize_requests(
        [ContextRequest(type="evidence", name=EVIDENCE_ID)] * 2, _scope()
    )

    assert len(allowed) == 1


def test_a_malformed_evidence_id_never_reaches_the_batch():
    """同一个形状的判据要在 `ContextRequest` 那一层就能看出来（`describe` 不炸）。"""
    assert "evidence" in ContextRequest(type="evidence", name=EVIDENCE_ID).describe()


def test_unknown_request_type_is_still_rejected():
    """白名单没有被这一次放宽冲掉。"""
    _allowed, dropped = sanitize_requests(
        [ContextRequest(type="read_anything", name="x")], _scope()
    )

    assert len(dropped) == 1
    assert dropped[0].kind == "request"


# --------------------------------------------------------------------------
# 4. finish_reason=length 优先，括号配平降为兜底（引擎端）
# --------------------------------------------------------------------------


class RecordingClient:
    """按脚本回答，并且**每一次都能控制 finish_reason**。"""

    def __init__(self, *replies: tuple[str, str]):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, temperature=None):
        self.calls.append([dict(item) for item in messages])
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        text, finish_reason = self._replies[index]
        return ChatResult(text=text, model="fake", finish_reason=finish_reason)

    @property
    def last_user(self) -> str:
        return str(self.calls[-1][-1]["content"])

    def user_text(self, call_index: int) -> str:
        return "\n".join(
            str(item["content"]) for item in self.calls[call_index] if item["role"] == "user"
        )


class StubProvider:
    def __init__(self):
        self.seen: list[tuple] = []

    def commit_detail(self, commit):
        self.seen.append(("commit_detail", commit))
        return "提交详情"

    def file_diff(self, commit, path):
        self.seen.append(("file_diff", commit, path))
        return f"diff of {path}"

    def file_content(self, commit, path, lines="", repository_id=""):
        self.seen.append(("file_content", commit, path, lines))
        return "正文"

    def read_reference(self, name):
        self.seen.append(("read_reference", name))
        return "参考"

    def find_references(self, query, path=""):
        self.seen.append(("find_references", query, path))
        return "命中"


def _loaded() -> LoadedSkills:
    def doc(name: str, text: str) -> SkillDocument:
        return SkillDocument(
            name=name,
            description="说明",
            path=Path("/tmp") / name,
            text=text,
            content_hash="hash-" + name,
        )

    return LoadedSkills(
        platform_skill=doc("version-diff-review", "# 角色与方法\n\n你是配表评审专家。\n"),
        platform_references=(),
        project_manifest=None,
        project_references=(),
        project_skills=(),
        readable={},
        project_slug="",
        revision="rev-1",
    )


FINAL_JSON = json.dumps(
    {
        "status": "final",
        "reason": "看完了",
        "report_markdown": "# 变更理解\n\n## 信息缺口\n\n没有缺口。",
        "dimensions": [{"id": "config_id", "hit": False, "note": "没命中"}],
        "anomalies": [],
    },
    ensure_ascii=False,
)


def _run(client, provider, **limit_overrides):
    limits = EngineLimits(max_rounds=4, max_tool_requests=10, **limit_overrides)
    return run_analysis(
        client=client,
        provider=provider,
        loaded=_loaded(),
        scope=_scope(),
        change_summary=f"本批次 1 个提交，改了 {TABLE}",
        limits=limits,
        thresholds=RuleThresholds(),
    )


def test_a_parseable_midround_cut_by_the_output_limit_is_not_executed_as_is():
    """**这是本次最直接的收益点**：残 JSON 解析得通，但它只有半句话。

    同一个回答，如果只看括号配平（它配平）就会被照单收下；上游那句
    `finish_reason="length"` 才是识破它的信号。
    """
    client = RecordingClient(
        (_mid_round_payload(), "length"),
        (FINAL_JSON, "stop"),
    )
    provider = StubProvider()

    outcome = _run(client, provider)

    # 它的 `requests` **没有被执行** —— 平台要求它压短后重发。
    #
    # 判据是**模型的索取额度**（`requests_used`），不是「provider 有没有被调用过」：
    # P3 之后平台自己会预取本批次的文件（那条路不计入模型的额度，也不经
    # `sanitize_requests` 之外的任何入口）。用后者当判据，这条用例就会因为「预取调了
    # provider」而红 —— 而它真正要守的「残 JSON 的请求没有被执行」反而没人看了。
    assert outcome.requests_used == 0, provider.seen
    assert TRUNCATED_OUTPUT_HINT in client.user_text(1)
    assert outcome.rounds[0].correction_hint == TRUNCATED_OUTPUT_HINT


def test_the_same_midround_without_the_limit_is_executed_normally():
    """差分对照：把 `finish_reason` 换成 `stop`，这一轮就照常执行。

    没有这一条，上面那条测试在「引擎永远不执行 requests」的实现下也是绿的。
    """
    client = RecordingClient(
        (_mid_round_payload(), "stop"),
        (FINAL_JSON, "stop"),
    )
    provider = StubProvider()

    outcome = _run(client, provider)

    assert provider.seen, "finish_reason 不是 length 时必须照常执行"
    assert outcome.rounds[0].correction_hint == ""


def test_bracket_imbalance_still_catches_truncation_when_finish_reason_is_silent():
    """括号配平降为**兜底**，不是删掉：端点上没有 finish_reason 时仍然要认出来。"""
    client = RecordingClient(
        ('{"status": "final", "report_markdown": "断在半截', ""),
        (FINAL_JSON, "stop"),
    )
    provider = StubProvider()

    outcome = _run(client, provider)

    assert outcome.rounds[0].correction_hint == TRUNCATED_OUTPUT_HINT


def test_output_budget_hit_is_recorded_on_the_round():
    """截断**可观测**：落在 RoundRecord 上，落库/面板那条路读得到同一个事实。"""
    client = RecordingClient((FINAL_JSON, "length"))
    provider = StubProvider()

    outcome = _run(client, provider, max_corrections=0)

    assert outcome.rounds[-1].output_budget_hit is True


def test_output_budget_hit_is_false_on_a_normal_round():
    """反向对照：正常收尾的那一轮不许被打上这个标记（否则这个数没有信息量）。"""
    client = RecordingClient((FINAL_JSON, "stop"))
    provider = StubProvider()

    outcome = _run(client, provider)

    assert outcome.rounds[-1].output_budget_hit is False


def test_a_parseable_final_is_still_accepted_when_corrections_are_gone():
    """纠正额度用完时**不许把一份能解析的最终报告丢掉**（丢它比收下它糟得多）。"""
    client = RecordingClient((FINAL_JSON, "length"))
    provider = StubProvider()

    outcome = _run(client, provider, max_corrections=0)

    assert outcome.payload is not None
    assert outcome.payload.is_final


# --------------------------------------------------------------------------
# 5. max_tokens 透传
# --------------------------------------------------------------------------


def test_engine_limits_carries_max_output_tokens_and_defaults_to_none():
    assert EngineLimits().max_output_tokens is None
    assert EngineLimits(max_output_tokens=4096).max_output_tokens == 4096
    assert EngineLimits.from_config({"max_output_tokens": 2048}).max_output_tokens == 2048


def test_max_output_tokens_reaches_the_client_when_configured():
    seen: list[dict] = []

    class SpyClient:
        def complete(self, messages, *, temperature=None, **kwargs):
            seen.append(kwargs)
            return ChatResult(text=FINAL_JSON, model="fake")

    _run(SpyClient(), StubProvider(), max_output_tokens=4096)

    assert seen and all(item.get("max_tokens") == 4096 for item in seen), seen


def test_no_max_tokens_is_sent_when_it_is_not_configured():
    """不配就不发 —— 「默认行为逐字节不变」这条性质必须留着。"""
    seen: list[dict] = []

    class SpyClient:
        def complete(self, messages, *, temperature=None, **kwargs):
            seen.append(kwargs)
            return ChatResult(text=FINAL_JSON, model="fake")

    _run(SpyClient(), StubProvider())

    assert seen and all("max_tokens" not in item for item in seen), seen


def test_llm_client_puts_max_tokens_in_the_request_body():
    from services.ai.llm_client import LLMClient

    client = LLMClient(base_url="https://example.invalid/v1", model="m")

    body = client._request_body(
        [{"role": "user", "content": "hi"}], temperature=None, marker=None, max_tokens=777
    )
    assert body["max_tokens"] == 777

    without = client._request_body(
        [{"role": "user", "content": "hi"}], temperature=None, marker=None
    )
    assert "max_tokens" not in without


def test_llm_client_complete_forwards_max_tokens_to_the_wire(monkeypatch):
    from services.ai.llm_client import LLMClient

    client = LLMClient(base_url="https://example.invalid/v1", model="m")
    captured: list[dict] = []

    def fake_once(messages, *, temperature, marker, max_tokens=None):
        captured.append({"temperature": temperature, "max_tokens": max_tokens})
        return ChatResult(text="{}", model="m")

    monkeypatch.setattr(client, "_complete_once", fake_once)
    client.complete([{"role": "user", "content": "hi"}], max_tokens=123)

    assert captured == [{"temperature": None, "max_tokens": 123}]


# --------------------------------------------------------------------------
# 6. 文案：SKILL.md 与提示词里那五处
# --------------------------------------------------------------------------


def test_the_first_round_hint_no_longer_asks_for_a_long_reason():
    """平台自己在提示词里要求「把判断写进 reason」，正是长 `reason` 的来源。

    只改协议不改文案，会被提示词顶回去。
    """
    from services.ai.prompt import _first_round_hint

    text = _first_round_hint()
    assert "reason_code" in text
    assert "写进 `reason`" not in text


def test_skill_md_documents_the_evidence_lookup():
    """按地址取回原文这件事，SKILL.md 里必须写着。

    没有这一句，任务书里那个 `@evidence_id=…（原文 N 字，按需索取）` 对模型就只是个装饰。

    ## 它曾经只能写在**正文**里，2026-09-22 收口了

    `skill_contract.extract_request_types` 会把 SKILL.md 里每一个 `"type": "X"` 抓出来与
    `skill_contract.REQUEST_TYPES` **逐字比对**。这条改动的第一版因此没敢把
    `{"type": "evidence", …}` 写进 JSON 示例（写了 `test_shipped_skills_satisfy_the_contract`
    就红），只留了正文那一句，**而契约校验看不见这个类型** —— 协议侧靠
    `ALLOWED_REQUEST_TYPES = (*REQUEST_TYPES, …)` 自己拼一份绕过去。

    收口之后：`evidence` 同时进了 `skill_contract.REQUEST_TYPES` 与 SKILL.md 的 JSON 示例，
    `protocol.ALLOWED_REQUEST_TYPES` 就是 `REQUEST_TYPES` 本身（不再拼第二份）。
    **下面两条断言都留着**：正文那句讲「什么时候、怎么用」，JSON 示例才是契约提取器认的地方，
    少任何一边都会让模型拿不到完整的形状。
    """
    from pathlib import Path

    body = Path("skills/version-diff-review/SKILL.md").read_text(encoding="utf-8")

    assert "按地址取回原文" in body
    sentence = body.split("按地址取回原文")[1].split("\n")[0]
    assert "`type`" in sentence and "`evidence`" in sentence, sentence
    # 与协议同口径：白名单在 `protocol.ALLOWED_REQUEST_TYPES` 里，这里只是它的文档。
    assert EVIDENCE_REQUEST_TYPE == "evidence"


def test_skill_md_mid_round_form_is_narrowed():
    """SKILL.md 进系统提示词 —— 中间轮的形态定义在文档里，必须与协议同口径。

    **剥注释再断言是对源码说的；SKILL.md 是给模型看的正文**，所以这里断言的是它逐字
    写下的东西。判据取**形态一那个 JSON 代码块**：它是给模型照抄的规范形状，不能出现
    中间轮不认的那四个字段；正文里则必须**明说**这四个字段属于 `final`。
    """
    from pathlib import Path

    body = Path("skills/version-diff-review/SKILL.md").read_text(encoding="utf-8")
    form_one = body.split("### 形态一")[1].split("### 形态二")[0]
    example = form_one.split("```json")[1].split("```")[0]

    assert "reason_code" in example
    for banned in ("report_markdown", "anomalies", "dimensions", "candidate_dispositions"):
        assert banned not in example, banned
        assert banned in form_one, f"正文里必须明说 {banned} 属于 final"
    assert "reason_code" in body.split("## 渐进式披露")[1]
