"""条数上限生效时，报告里必须说得出「还有几条没列进来」。

## 为什么单独一个文件

上限（`RuleThresholds.max_anomalies`，默认 10）生效时，结论清单是**静默变短**的：
截断在归一化那一层发生，报告正文读起来完全正常。用户看到「这次报了 10 条」，
看不出「恰好有 10 条」与「有 13 条、后 3 条被截掉了」的区别 —— 而后者只要把上限调大
重跑一次就能拿到。这正是本仓库反复出的那一类问题：**不声不响**。

## 两个容易写成假绿的地方（都踩过）

1. **按 `kind == "anomaly"` 过滤**。`"anomaly"` 这个 kind 早就被协议层占着
   （`protocol._coerce_anomalies` 用它记「severity 不在允许集合内」「evidence 为空」
   这类模型自己写坏了的条目）。上限那一种现在用 `KIND_ANOMALY_CAP`。写错的话，
   模型写坏一条就会在报告里多出一节「超出了上限」，而真正被截掉的反而可能不在里面。
   下面 `test_a_malformed_anomaly_is_not_reported_as_a_cap_drop` 钉的就是这个。
2. **从当前配置读上限**。这一节说的是「**当时那次**运行的上限」，用户改了配置之后
   翻看旧报告，两者不是同一个数。所以只从记账里取（`_cap_limit_of`）。
"""

from __future__ import annotations

import json

from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome
from services.ai.protocol import Anomaly, DroppedItem, parse_payload
from services.ai.rules import KIND_ANOMALY_CAP, RuleThresholds, normalize_anomalies
from services.ai.subagent import CAP_TITLE, Candidate, aggregate_outcomes, build_cap_section
from tests.test_ai_engine import TABLE, _anomaly, _final, _run


def _cap_records(result) -> list[DroppedItem]:
    return [item for item in result.dropped if item.kind == KIND_ANOMALY_CAP]


def _obj(**overrides) -> Anomaly:
    """`_anomaly()` 给的是 dict（那是模型的输出形态），归一化要的是 `Anomaly`。"""
    data = _anomaly(**overrides)
    return Anomaly(
        title=data["title"], category=data["category"], severity=data["severity"],
        confidence=data["confidence"], evidence=tuple(data["evidence"]),
        commit=data["commit"], file_path=data["file_path"], impact=data["impact"],
        suggestion=data["suggestion"],
    )


def _objs(*overrides_list) -> list[Anomaly]:
    return [_obj(**overrides) for overrides in overrides_list]


# ==========================================================================
# 记账本身
# ==========================================================================


class TestTheCapIsRecordedAsItsOwnKind:
    def test_the_cap_drop_has_its_own_kind(self):
        """**不能复用 `"anomaly"`** —— 那一个已经被协议层占着了。"""
        objs = _objs(
            {"title": "道具ID被删除", "severity": "high", "file_path": "config/a.xlsx"},
            {"title": "等级上限配错", "severity": "high", "file_path": "config/b.xlsx"},
            {"title": "登录重连超时", "severity": "high", "file_path": "config/c.xlsx"},
        )

        result = normalize_anomalies(objs, RuleThresholds(max_anomalies=2))

        assert result.capped == 1
        cap_records = _cap_records(result)
        assert len(cap_records) == 1
        assert cap_records[0].kind != "anomaly", (
            "复用了协议层的 kind：模型写坏一条就会被报告说成「被上限截掉了」"
        )

    def test_the_record_carries_the_file_path(self):
        """只记标题的话，读的人没法回看是哪份表 —— 而这一节就是要给他线索。"""
        objs = _objs(
            {"title": "道具ID被删除", "severity": "high"},
            {"title": "等级上限配错", "severity": "high", "file_path": "config/等级表.xlsx"},
        )

        result = normalize_anomalies(objs, RuleThresholds(max_anomalies=1))

        record = _cap_records(result)[0]
        assert "config/等级表.xlsx" in record.detail


# ==========================================================================
# 那一节本身
# ==========================================================================


class TestTheSection:
    def test_it_names_every_cut_item_and_the_limit(self):
        objs = _objs(
            {"title": "道具ID被删除", "severity": "high", "file_path": "config/a.xlsx"},
            {"title": "等级上限配错", "severity": "high", "file_path": "config/b.xlsx"},
            {"title": "登录重连超时", "severity": "high", "file_path": "config/c.xlsx"},
        )
        result = normalize_anomalies(objs, RuleThresholds(max_anomalies=1))

        section = build_cap_section(result.dropped)

        assert section.startswith(CAP_TITLE)
        for title in ("等级上限配错", "登录重连超时"):
            assert title in section, "被截掉的那一条没有列出来"
        assert "道具ID被删除" not in section, "保留下来的那一条不该出现在「被截掉」里"
        assert "上限是 1 条" in section
        assert "另有 2 条" in section

    def test_it_says_these_are_not_findings_that_were_ruled_out(self):
        """「被截掉」不等于「没查到」，也不等于「不成立」—— 说反了会让人白补一次分析。"""
        result = normalize_anomalies(
            _objs({"title": "普通甲"}, {"title": "普通乙", "severity": "high"}),
            RuleThresholds(max_anomalies=1),
        )

        section = build_cap_section(result.dropped)

        assert "不是「没查到」" in section
        assert "不成立" in section

    def test_no_cap_no_section(self):
        result = normalize_anomalies(_objs({}), RuleThresholds(max_anomalies=10))

        assert build_cap_section(result.dropped) == ""

    def test_a_malformed_anomaly_is_not_reported_as_a_cap_drop(self):
        """**反自检**：协议层丢的条目（模型自己写坏了）不是「被上限截掉」。

        写错 kind 的实现会把这一条也列进「结论条数上限」那一节，而它真正的去向是
        「模型这一轮返回的内容不符合协议」—— 两件事的处置完全不同。
        """
        payload = parse_payload(
            json.dumps(
                {
                    "status": "final",
                    "report_markdown": "# 正文",
                    "dimensions": [{"id": "config_id", "hit": False, "note": ""}],
                    "anomalies": [
                        {
                            "title": "severity 写错了",
                            "category": "config_id",
                            "severity": "非常严重",
                            "confidence": "high",
                            "evidence": ["ev"],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

        assert [item.kind for item in payload.dropped] == ["anomaly"], (
            "前提变了：协议层丢的条目现在不是 `anomaly` 了，这条反自检要跟着改"
        )
        assert build_cap_section(payload.dropped) == "", (
            "协议层丢的条目被当成了「被上限截掉」"
        )

    def test_the_limit_comes_from_the_record_not_from_todays_config(self):
        """翻看旧报告时，`thresholds` 是**今天**的值，记账里才是当时那个值。"""
        stale = (
            DroppedItem(
                KIND_ANOMALY_CAP,
                3,
                "超出本次上限（7 条），已按严重度优先保留",
                "high 某条（config/a.xlsx）",
            ),
        )

        section = build_cap_section(stale)

        assert "上限是 7 条" in section, "上限值不是从记账里取的"

    def test_an_unparsable_record_says_so_instead_of_guessing(self):
        """记账里没有可解析的上限时**不许编一个数**出来。"""
        odd = (DroppedItem(KIND_ANOMALY_CAP, 1, "原因措辞变了", "high 某条"),)

        section = build_cap_section(odd)

        assert "上限是 未记录 条" in section

    def test_a_record_without_a_title_still_takes_a_line(self):
        """标题为空也要占一行并说明 —— 少一行会让「另有 N 条」与下面的条数对不上。"""
        odd = (DroppedItem(KIND_ANOMALY_CAP, 1, "超出本次上限（1 条），已按严重度优先保留", ""),)

        section = build_cap_section(odd)

        assert "平台没有记下标题" in section


# ==========================================================================
# 落到报告里
# ==========================================================================


class TestItReachesTheReport:
    def test_the_single_agent_report_gains_the_section(self):
        """**端到端**：模型报了 3 条、上限是 1，报告正文里必须看得到这件事。"""
        client = _ScriptedClient(
            _final(
                _anomaly(title="道具ID被删除", severity="high"),
                _anomaly(title="等级上限配错", severity="high"),
                _anomaly(title="登录重连超时", severity="high"),
            )
        )

        outcome = _run(client, thresholds=RuleThresholds(max_anomalies=1))

        assert CAP_TITLE in outcome.report_markdown, (
            "上限生效了，报告正文里却一个字都没提 —— 用户看不出清单是短的"
        )
        assert len(outcome.anomalies) == 1
        # 保留的那一条仍在正文之外的结构化清单里，这一节只解释「少了什么」。
        assert outcome.anomalies[0].title == "道具ID被删除"

    def test_a_report_without_a_cap_has_no_section(self):
        client = _ScriptedClient(_final(_anomaly()))

        outcome = _run(client)

        assert CAP_TITLE not in outcome.report_markdown


class TestTheFamilyReportGainsItToo:
    """子代理路径的最终报告是**汇总那一次**产出的，那一次的 `dropped` 才带着上限记账。

    单代理那条路已经在 `TestItReachesTheReport` 里端到端跑过了；这里钉的是
    `aggregate_outcomes` 那一处调用点 —— 少了它，开了子代理的项目永远看不到这一节。
    """

    def test_aggregate_appends_the_section(self):
        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 变更理解\n\n一切正常。\n",
            anomalies=(),
            dropped=(
                DroppedItem(
                    KIND_ANOMALY_CAP,
                    11,
                    "超出本次上限（10 条），已按严重度优先保留",
                    "high 道具ID被删除（config/[30]道具表_CfgItem.xlsx）",
                ),
            ),
        )

        outcome = aggregate_outcomes(synthesis=synthesis, steps=(), candidates=())

        assert CAP_TITLE in outcome.report_markdown
        assert "config/[30]道具表_CfgItem.xlsx" in outcome.report_markdown

    def test_the_cap_section_comes_before_the_gap_section(self):
        """信息缺口永远收尾：它是「哪些东西没看到」，读的人靠它在末尾一眼找到。

        上限那一节说的是「结论清单少了什么」，属于**结论的一部分**，排在它前面。
        """
        candidate = Candidate(
            member_label="S1",
            index=1,
            anomaly=_obj(title="没进报告的那一条", file_path="config/别的表.xlsx"),
        )
        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 变更理解\n\n一切正常。\n",
            anomalies=(),
            dropped=(
                DroppedItem(
                    KIND_ANOMALY_CAP,
                    11,
                    "超出本次上限（10 条），已按严重度优先保留",
                    "high 道具ID被删除",
                ),
            ),
        )

        outcome = aggregate_outcomes(
            synthesis=synthesis, steps=(), candidates=(candidate,)
        )

        assert CAP_TITLE in outcome.report_markdown
        assert "信息缺口（平台补充）" in outcome.report_markdown
        assert outcome.report_markdown.index(CAP_TITLE) < outcome.report_markdown.index(
            "信息缺口（平台补充）"
        ), "上限那一节排到了信息缺口后面 —— 末尾那一节必须永远是「没看到什么」"


class _ScriptedClient:
    """按顺序回答的桩（与 `tests/test_ai_engine.py` 的同名桩同形，只留这一条用得到的）。"""

    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, temperature=None):
        from services.ai.llm_client import ChatResult

        self.calls.append([dict(item) for item in messages])
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return ChatResult(
            text=self._replies[index], model="fake", prompt_tokens=10, completion_tokens=5
        )


def test_the_table_path_helper_is_the_one_we_think_it_is():
    """守住本文件里 `TABLE` 的用法：它不是随便一个字符串，是那两条证据里的表。"""
    assert TABLE in _anomaly()["evidence"][0]
