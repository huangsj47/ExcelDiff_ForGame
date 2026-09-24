# -*- coding: utf-8 -*-
"""覆盖账本（AI-P1-03）：**这次分析看了多少、没看多少、缺口是什么**。

## 这一组钉的是什么

在这之前，一次分析关于「覆盖」只有一个词：报告里那行「分析范围：全量」。于是读者会把
提示词里列出的 200 个名字当成「它看完了这个版本」—— 而实测是 995 个改动文件里只有
57 个取到过证据。这一组用例守四件事，每一件坏掉的方式都很安静：

1. **三种覆盖各算各的**（输入的装了多少 / 名字列了多少 / 真的看过多少），少一种就有一
   个问题没人回答；
2. **「取到证据」的两种去重口径必须都算出来**：只写一个，同一份数据能算出两个覆盖率而
   没人知道差在哪；
3. **缺数就是缺数**：老运行没有逐轮明细时，证据覆盖是「未知」不是 0 —— 写成 0 等于替
   用户断言「它一个文件都没看」；
4. **报告里真的显示它**，而且「全量」不再是一个会被读成「全读」的词。

真实数字（可复现）：run 12 = 57 / 995（保守）、71 段；run 13 = 60 / 996、69 段。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone

import pytest

from app import app, create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from services.ai import coverage_ledger as ledger_mod
from services.ai import report_document as doc

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 一个全 40 位的提交号。白名单里存的是完整号，逐轮明细的标签里只有 12 位前缀。
LATEST = "f844faa6f59c3f1b571226e92ac03dbd22a6b790"
OLDER = "322ece563731088a334a1d71870016078d76bbc8"


def _weekly_payload(*, files, listed=None, window=None, truncated=False, focus="", repositories=None):
    """周版本模式的 `request_payload`（照 `build_weekly_payload` 落库的那种形状）。

    `repositories` 是「第几条属于哪个仓库」（下标 → 仓库 id），不传就不带这个键
    （老 payload 的形状）。
    """
    entries = [{"file_path": path, "latest_commit_id": commit} for path, commit in files]
    for index, repository_id in (repositories or {}).items():
        entries[index]["repository_id"] = repository_id
    summary = {
        "total_files": window if window is not None else len(entries),
        "delta_files": len(entries),
        "batch_files": len(entries),
        "window_files": window if window is not None else len(entries),
    }
    names = [path for path, _ in files] if listed is None else listed
    return {
        "mode": "weekly",
        "scope": "full",
        "summary": summary,
        "delta_files": entries,
        "list_files": [{"file_path": name} for name in names],
        "policy": {"truncated": bool(truncated), "truncation_reason": "token_budget" if truncated else None},
        "focus": {"key": "all", "label": focus},
    }


def _fetched(kind, commit, path, *, lines="", failed=False, empty=False, repository_id=None):
    """逐轮明细里的一条（照 `trace_evidence.summarize_executed` 的形状）。

    `repository_id` 是 P1a 加进明细的一项（`context_tools` 把请求上的仓库写进 `meta`）：
    同一条 `(提交, 路径)` 在两个仓库里是两份不同的内容，只按路径记账会把两者的覆盖混成
    一个数。老行没有这一项 ⇒ 留空 = 不知道。
    """
    label = f"{kind} {commit[:12]} {path}" + (f" lines={lines}" if lines else "")
    row = {"kind": kind, "label": label, "chars": 1200, "failed": failed, "empty": empty}
    if repository_id is not None:
        row["repository_id"] = str(repository_id)
    return row


def _stats(**by_kind):
    """`run.tool_stats_json` 的形状：工具类型 → 那一组计数。"""
    return by_kind


# ---------------------------------------------------------------------------
#  一、三种覆盖
# ---------------------------------------------------------------------------
def test_the_three_coverages_answer_three_different_questions():
    """输入装了多少 / 名字列了多少 / 真的看过多少 —— 三个问题三个数，不能互相替代。"""
    payload = _weekly_payload(
        files=[("a.xlsx", LATEST), ("b.lua", LATEST), ("c.lua", LATEST), ("d.lua", LATEST)],
        listed=["a.xlsx", "b.lua"],          # 清单长到列不下，只列了 2 个名字
        window=10,                            # 这个版本一共改过 10 个文件
        truncated=True,
    )
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "a.xlsx")],
        tool_stats=_stats(),
    )

    assert ledger["inventory_coverage"] == {"covered": 4, "total": 10, "ratio": 0.4}
    assert ledger["listed_coverage"] == {"covered": 2, "total": 4, "ratio": 0.5}
    assert ledger["evidence_coverage"]["by_pair"] == {"covered": 1, "total": 4, "ratio": 0.25}
    assert ledger["counts"]["pending_files"] == 3


def test_two_dedup_conventions_give_two_different_numbers():
    """**两种去重口径要算出两个数**，而且要说清哪个是报告用的那一份。

    构造：白名单 3 个文件，本批次的提交都是 LATEST；证据里 a 取的是**本批次那条提交**
    （算）、b 取的是**更早**那条提交的版本（宽松口径算、保守口径不算）、c 也是本批次的
    那条（算）。于是保守 = 2、宽松 = 3。
    """
    payload = _weekly_payload(
        files=[("a.xlsx", LATEST), ("b.lua", LATEST), ("c.lua", LATEST)]
    )
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[
            _fetched("file_diff", LATEST, "a.xlsx"),
            _fetched("file_diff", OLDER, "b.lua"),   # 更早那条提交：不是白名单里那一条
            _fetched("file_content", LATEST, "c.lua"),
        ],
        tool_stats=_stats(),
    )

    evidence = ledger["evidence_coverage"]
    assert evidence["dedup"] == "pair", "报告默认读的那一份必须是保守口径"
    assert evidence["by_pair"]["covered"] == 2
    assert evidence["by_path"]["covered"] == 3
    assert evidence["by_pair"]["ratio"] != evidence["by_path"]["ratio"]
    # 两个数都写进了给报告的那几行，而且各自标了口径
    rows = dict(ledger["rows"])
    assert "2 / 3" in rows["覆盖（取到证据）"]
    assert "按「(本批次最新提交, 文件)」去重" in rows["覆盖（取到证据）"]
    assert "3 / 3" in rows["覆盖（同上·另一种去重）"]
    assert "按「文件」去重" in rows["覆盖（同上·另一种去重）"]


def test_a_line_window_suffix_must_not_count_as_another_file():
    """`lines=` 是**同一个文件的另一段**，不是另一个文件 —— 不摘掉它，覆盖率会虚高。

    实测 run 12：标签里带着行窗口去重是 71，摘掉行窗口按文件去重是 57。57 才是
    「有多少个文件被看过」；71 是「取回了多少段」，两个数都留着（后者不是覆盖率）。
    """
    payload = _weekly_payload(files=[("conf.lua", LATEST), ("x.lua", LATEST)])
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[
            _fetched("file_content", LATEST, "conf.lua", lines="30"),
            _fetched("file_content", LATEST, "conf.lua", lines="30-31"),
            _fetched("file_diff", LATEST, "conf.lua"),
        ],
        tool_stats=_stats(),
    )

    assert ledger["counts"]["evidence_files_by_path"] == 1, "同一个文件的三段只能算一个文件"
    assert ledger["counts"]["evidence_segments"] == 3, "但它确实取回了三段"
    assert ledger_mod.parse_evidence_label(
        f"file_content {LATEST[:12]} code/conf.lua lines=30-31"
    ) == (LATEST[:12], "code/conf.lua", "30-31")


def test_a_failed_or_empty_fetch_is_not_evidence():
    """取不到（`failed`）与「工具明确回了没有内容」（`empty`）都**不算看过**。

    把这两者算成证据，等于把「没有证据」写成「这里没问题」—— 本仓库的底线。
    """
    payload = _weekly_payload(files=[("a.xlsx", LATEST), ("b.xlsx", LATEST)])
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[
            _fetched("file_diff", LATEST, "a.xlsx", failed=True),
            _fetched("file_diff", LATEST, "b.xlsx", empty=True),
        ],
        tool_stats=_stats(),
    )

    assert ledger["counts"]["evidence_files_by_pair"] == 0
    assert ledger["evidence_coverage"]["failed_labels"], "失败的条目要留痕（报告里会说哪些没取到）"


def test_only_file_tools_count_as_evidence():
    """只有 `file_diff` / `file_content` 算「这个文件被看过」。

    `commit_detail` 给的是文件名清单、`find_references` 只回行号、`read_reference` 读的是
    平台规程 —— 都不是「读了某个文件的改动」，算进来就是虚报覆盖率。
    """
    payload = _weekly_payload(files=[("a.lua", LATEST), ("b.lua", LATEST)])
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[
            _fetched("commit_detail", LATEST, "a.lua"),
            _fetched("find_references", LATEST, "b.lua"),
            _fetched("read_reference", LATEST, "c.md"),
        ],
        tool_stats=_stats(),
    )

    assert ledger["counts"]["evidence_files_by_pair"] == 0
    assert ledger["counts"]["evidence_files_by_path"] == 0


def test_a_file_outside_the_whitelist_is_not_counted():
    """越权点名的文件平台根本不会去读；万一漏了一条到明细里，也不能算进覆盖率。"""
    payload = _weekly_payload(files=[("a.lua", LATEST)])
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "../../etc/passwd")],
        tool_stats=_stats(),
    )

    assert ledger["counts"]["evidence_files_by_pair"] == 0


# ---------------------------------------------------------------------------
#  二、缺数就是缺数
# ---------------------------------------------------------------------------
def test_an_old_run_without_details_says_unknown_instead_of_zero():
    """没有逐轮明细（老运行）时，证据覆盖是**未知**，不是 0。

    与消耗面板「未上报 ≠ 0」同一条口径：写成 0 等于替用户断言「它一个文件都没看」。
    """
    payload = _weekly_payload(files=[("a.lua", LATEST)])
    ledger = ledger_mod.build_ledger(request_payload=payload, executed=None, tool_stats=None)

    evidence = ledger["evidence_coverage"]
    assert evidence["collected"] is False
    assert ledger["counts"]["evidence_files_by_pair"] is None
    assert evidence["by_pair"]["ratio"] is None
    rows = dict(ledger["rows"])
    assert "未知" in rows["覆盖（取到证据）"]
    assert "0" not in rows["覆盖（取到证据）"].replace("不是 0", "")
    # 缺口里要说明白：平台说不出「看过哪些文件」
    assert any("没有留下取数明细" in one for one in ledger["gaps"])


def test_a_payload_without_the_newer_keys_does_not_invent_numbers():
    """老 payload 没有 `batch_files` / `window_files` 时退回清单条数，**不编一个版本总数**。"""
    payload = {
        "mode": "weekly",
        "scope": "full",
        "summary": {"total_files": 3, "delta_files": 3},
        "delta_files": [
            {"file_path": "a.lua", "latest_commit_id": LATEST},
            {"file_path": "b.lua", "latest_commit_id": LATEST},
            {"file_path": "c.lua", "latest_commit_id": LATEST},
        ],
        "list_files": ["a.lua", "b.lua", "c.lua"],
    }
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "a.lua")],
        tool_stats=_stats(file_diff={"failed": 0, "refused_by_budget": 0, "truncated": 0}),
    )

    assert ledger["counts"]["batch_files"] == 3
    assert ledger["counts"]["evidence_files_by_pair"] == 1
    # `total_files` 只是兜底：读成「版本总数」会把「只看了一半」说成「版本就这么大」，
    # 所以列表那两行的措辞不能拿它当版本总数来宣称。
    assert ledger["counts"]["window_files"] == 3


def test_the_gap_counters_are_the_ones_the_usage_panel_shows():
    """缺口里那三个数**必须与消耗面板「取数（按工具类型）」那张表对得上**。

    面板上的「失败」列是所有工具类型的合计（`find_references` 检索不到也算）。缺口里少算
    一类，同一个事实就有两个数，用户会以为其中一个错了 —— 而这两处说的是同一件事。
    """
    payload = _weekly_payload(files=[("a.lua", LATEST)])
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "a.lua")],
        tool_stats=_stats(
            file_diff={"failed": 0, "refused_by_budget": 1, "truncated": 2},
            file_content={"failed": 1, "refused_by_budget": 0, "truncated": 0},
            find_references={"failed": 6, "refused_by_budget": 0, "truncated": 0},
        ),
    )

    assert ledger["counts"]["failed_requests"] == 7
    assert ledger["counts"]["refused_by_budget"] == 1
    assert ledger["counts"]["truncated_items"] == 2


def test_a_broken_or_empty_payload_does_not_crash():
    """坏数据（`None` / 字符串 / 列表）不该让导出整份失败 —— 但也**不许编数**。"""
    for payload in (None, "", "{不是 json", [], {"delta_files": "不是列表"}):
        ledger = ledger_mod.build_ledger(request_payload=payload, executed=[], tool_stats=None)
        assert ledger["counts"]["batch_files"] is None
        assert ledger["inventory_coverage"]["ratio"] is None
        assert ledger["rows"], "表里至少要有一行（写「未记录」），不能空着"


def test_a_single_commit_run_does_not_talk_about_a_version():
    """单提交模式：分析对象是那一条提交改的那一个文件。

    「本版本改动过的 996 个文件」这套措辞在这里会让人去找一个不存在的版本 ——
    所以那一行单独一套说法（数字口径不变：1 个文件，看没看到）。
    """
    payload = {
        "mode": "commit",
        "scope": "full",
        "commit": {"commit_id": LATEST, "path": "code/qz_server/src/Mod.lua", "message": "修"},
    }
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "code/qz_server/src/Mod.lua")],
        tool_stats=_stats(file_diff={"failed": 0, "refused_by_budget": 0, "truncated": 0}),
    )

    rows = dict(ledger["rows"])
    assert rows["覆盖（本次提交）"] == "这条提交改了 1 个文件，都在本次输入里"
    assert "覆盖（列出的名字）" not in rows, "单提交模式没有第二份清单，别摆一行空的"
    assert "1 / 1" in rows["覆盖（取到证据）"]
    assert ledger["limited"] is False, "那一个文件取到了证据，这一轮没有缺口"

    # 取不到时同样是缺口（不是「没问题」）
    missed = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "code/qz_server/src/Mod.lua", failed=True)],
        tool_stats=_stats(file_diff={"failed": 1, "refused_by_budget": 0, "truncated": 0}),
    )
    assert missed["limited"] is True
    assert any("没有取到证据" in one for one in missed["gaps"])


# ---------------------------------------------------------------------------
#  三、缺口：没看的是哪些、为什么
# ---------------------------------------------------------------------------
def test_every_gap_kind_shows_up():
    """四种缺口各说各的：清单截断 / 没有证据 / 取数失败 / 被拒与截断。

    合成一句话（「有 94% 没看」）读者就没法决定下一步做什么：改配置、查 Agent、还是
    缩小分析范围。
    """
    payload = _weekly_payload(
        files=[("a.xlsx", LATEST), ("b.xlsx", LATEST), ("c.xlsx", LATEST)],
        listed=["a.xlsx"],
        truncated=True,
    )
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "a.xlsx", failed=True)],
        tool_stats=_stats(file_diff={"failed": 1, "refused_by_budget": 4, "truncated": 2}),
    )
    gaps = "\n".join(ledger["gaps"])

    assert "变更清单长到列不下" in gaps
    assert "没有取到证据" in gaps and "还有 3 个" in gaps
    assert "取数失败 1 次" in gaps
    assert "有 4 次索取因为超出本次的上下文额度没有执行" in gaps
    assert "有 2 条取数在交给模型之前被截断" in gaps


def test_a_fully_covered_run_does_not_get_a_hedge_it_did_not_earn():
    """**真的全看过时不许加限定语** —— 那时「全量」就是「全都看过」。

    这条是反向自检：限定语加错了方向（对一次干净的全量分析说「不等于整个版本都看过」）
    同样是在印假信息，只是这次印的是悲观的那一面。
    """
    payload = _weekly_payload(files=[("a.lua", LATEST), ("b.lua", LATEST)])
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "a.lua"), _fetched("file_diff", LATEST, "b.lua")],
        tool_stats=_stats(file_diff={"failed": 0, "refused_by_budget": 0, "truncated": 0}),
    )

    assert ledger["limited"] is False
    assert ledger["scope_note"] == ""
    assert ledger["gaps"] == []


def test_the_scope_note_appears_whenever_something_is_missing():
    """只要有缺口，「分析范围」那一格就必须带上限定语（它是这一整份报告的前提）。"""
    payload = _weekly_payload(files=[("a.lua", LATEST)], window=500, truncated=True)
    ledger = ledger_mod.build_ledger(
        request_payload=payload, executed=[], tool_stats=_stats()
    )
    assert ledger["limited"] is True
    assert ledger["scope_note"] == ledger_mod.SCOPE_LIMITED_NOTE


def test_a_picked_scope_is_a_gap_of_its_own():
    """用户选了「只看配表仓库」时，其它仓库的改动不在输入里 —— 那也是一处缺口。"""
    payload = _weekly_payload(files=[("a.xlsx", LATEST)], focus="仅配表仓库")
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "a.xlsx")],
        tool_stats=_stats(),
    )
    assert ledger["limited"] is True
    assert any("仅配表仓库" in one for one in ledger["gaps"])


def test_the_real_shape_of_a_finished_run():
    """照实测形状（run 12）：995 个改动文件、列出 200 个名字、57 个文件取到证据、71 段。

    这个形状是**这个功能存在的理由**：报告里写着「全量」，而实际上只有 5.7%。
    """
    files = [(f"config/table_{index}.xlsx", LATEST) for index in range(995)]
    executed = []
    for index in range(57):
        executed.append(_fetched("file_diff", LATEST, f"config/table_{index}.xlsx"))
    # 其中 14 个文件被分段读第二遍（71 段 = 57 + 14）
    for index in range(14):
        executed.append(
            _fetched("file_content", LATEST, f"config/table_{index}.xlsx", lines="2")
        )

    ledger = ledger_mod.build_ledger(
        request_payload=_weekly_payload(files=files, listed=[path for path, _ in files][:200], truncated=True),
        executed=executed,
        tool_stats=_stats(file_diff={"failed": 0, "refused_by_budget": 0, "truncated": 18}),
    )

    counts = ledger["counts"]
    assert (counts["window_files"], counts["batch_files"], counts["listed_files"]) == (995, 995, 200)
    assert counts["evidence_files_by_pair"] == 57
    assert counts["evidence_files_by_path"] == 57
    assert counts["evidence_segments"] == 71
    assert counts["pending_files"] == 938
    rows = dict(ledger["rows"])
    assert "57 / 995（5.7%）" in rows["覆盖（取到证据）"]
    assert "71 段证据" in rows["覆盖（同上·另一种去重）"]


# ---------------------------------------------------------------------------
#  四、报告里真的显示它
# ---------------------------------------------------------------------------
def _report(**overrides):
    kwargs = dict(
        project_label="配表平台",
        target_label="周版本 第42周版本",
        run_id=12,
        created_at_display="2026-09-21 10:00:00",
        risk_level="high",
        scope="full",
        trigger_source="manual",
        model="qwen3-max",
        report_text="# 变更理解\n\n正文",
        anomalies=[],
    )
    kwargs.update(overrides)
    return doc.build_report_markdown(**kwargs)


def test_the_report_shows_the_ledger_and_never_says_full_means_everything_read():
    """报告里要**真的出现**覆盖与缺口，而且「分析范围」那一格不再是一个会被读成
    「全读」的词。"""
    payload = _weekly_payload(
        files=[(f"f{index}.lua", LATEST) for index in range(995)],
        listed=[f"f{index}.lua" for index in range(200)],
        truncated=True,
    )
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, f"f{index}.lua") for index in range(57)],
        tool_stats=_stats(file_diff={"failed": 0, "refused_by_budget": 0, "truncated": 18}),
    )
    text = _report(coverage=ledger)

    assert "| 覆盖（取到证据） |" in text
    assert "57 / 995" in text
    assert doc.COVERAGE_TITLE in text
    assert text.index(doc.COVERAGE_TITLE) < text.index("# 变更理解"), "缺口要在正文之前说"
    # 被读成「全读」的那一行没了：范围那一格现在带限定语
    assert "| 分析范围 | 全量 |" not in text
    assert "不等于「整个版本都看过了」" in text
    assert "看完了" not in text and "全部读完" not in text
    # 三段式没被改坏：正文两侧仍然各一条分隔线
    assert text.count("\n---\n") == 2


def test_a_report_without_a_ledger_is_byte_for_byte_what_it_was():
    """不传账本时逐字与以前相同 —— 覆盖是「额外的诚实」，不是让老调用方跟着改的理由。"""
    text = _report()

    assert "| 分析范围 | 全量 |" in text
    assert "覆盖（" not in text
    assert doc.COVERAGE_TITLE not in text


def test_the_coverage_rows_keep_the_meta_table_shape():
    """覆盖那几行也是元信息表的行：两格、竖线转义，一行的值里带 `|` 不许把表切成两行。"""
    ledger = ledger_mod.build_ledger(
        request_payload=_weekly_payload(files=[("a.lua", LATEST)], listed=["a | b.lua"]),
        executed=[],
        tool_stats=_stats(),
    )
    text = _report(coverage=ledger)
    lines = text.splitlines()
    separator = lines.index("| --- | --- |")
    for line in lines[3:separator]:
        assert line.count("|") == 3, line


def test_the_report_never_invents_coverage_from_junk():
    """账本形状不对（不是字典 / 行不是两元组）时**宁可什么都不显示**，不印半行。"""
    text = _report(coverage={"rows": ["坏行", ("只有一项",), None, ("好行", "值")], "gaps": ["一句话"]})

    assert "| 好行 | 值 |" in text
    assert "坏行" not in text
    assert "只有一项" not in text


# ---------------------------------------------------------------------------
#  五、降级那一档的显示
# ---------------------------------------------------------------------------
def test_a_degraded_run_has_a_word_of_its_own():
    """`degraded` 原先在 `STATUS_LABELS` 里没有，历次结论里显示的是原始英文码。

    而它恰恰是最需要被读出来的一档：跑完了、有报告，但流程没走完。
    """
    assert doc.status_label("degraded") == "降级完成"
    assert doc.status_label("succeeded") == "已有结论"
    assert doc.status_label("failed") == "分析失败"


def test_the_degraded_word_matches_the_usage_dashboard():
    """同一件事在消耗面板上已经有一个词了，两处只能有一个说法。

    （`templates/ai_usage_dashboard.html` 的 `{ succeeded: '完成', degraded: '降级完成', … }`）
    """
    dashboard = os.path.join(PROJECT_ROOT, "templates", "ai_usage_dashboard.html")
    with open(dashboard, encoding="utf-8") as handle:
        content = handle.read()
    assert re.search(r"degraded:\s*'([^']+)'", content), "消耗面板上的状态词不见了"
    assert re.search(r"degraded:\s*'([^']+)'", content).group(1) == doc.status_label("degraded")


# ---------------------------------------------------------------------------
#  六、读取侧：一条运行行 → 账本
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _tables():
    with app.app_context():
        create_tables()


def _run_with_trace(*, executed_json, tool_stats=None, request_payload=None) -> AiAnalysisRun:
    now = datetime.now(timezone.utc)
    project = Project(code=f"cov_{uuid.uuid4().hex[:8]}", name="覆盖率用例")
    db.session.add(project)
    db.session.commit()
    run = AiAnalysisRun(
        project_id=project.id,
        target_type="weekly",
        status="succeeded",
        scope="full",
        trigger_source="manual",
        created_at=now,
        request_payload=json.dumps(
            request_payload
            if request_payload is not None
            else _weekly_payload(files=[("a.lua", LATEST), ("b.lua", LATEST)]),
            ensure_ascii=False,
        ),
        tool_stats_json=json.dumps(tool_stats or _stats(), ensure_ascii=False),
    )
    db.session.add(run)
    db.session.commit()
    db.session.add(
        AiAnalysisTrace(
            run_id=run.id,
            round_index=1,
            executed_json=executed_json,
            requests_json=json.dumps({"count": 1, "items": []}, ensure_ascii=False),
            dropped_json=json.dumps(
                {"refused_by_budget": 0, "truncated": 0, "details": []}, ensure_ascii=False
            ),
        )
    )
    db.session.commit()
    return run


def test_ledger_from_run_reads_the_payload_and_the_round_records():
    with app.app_context():
        detail = json.dumps(
            {"items": 1, "details": [_fetched("file_diff", LATEST, "a.lua")]}, ensure_ascii=False
        )
        run = _run_with_trace(
            executed_json=detail,
            tool_stats=_stats(file_diff={"failed": 2, "refused_by_budget": 0, "truncated": 1}),
        )
        ledger = ledger_mod.ledger_from_run(run)

        assert ledger["counts"]["batch_files"] == 2
        assert ledger["counts"]["evidence_files_by_pair"] == 1
        assert ledger["counts"]["failed_requests"] == 2, "缺口那三个数取运行级的按工具计数"
        assert ledger["counts"]["truncated_items"] == 1
        assert ledger["limited"] is True


def test_ledger_from_run_says_unknown_when_the_round_records_were_never_captured():
    """那一列是 NULL 的老运行：**未知**，不是 0（`decode_evidence` 会把两者都读成空列表）。"""
    with app.app_context():
        run = _run_with_trace(executed_json=None)
        ledger = ledger_mod.ledger_from_run(run)

        assert ledger["evidence_coverage"]["collected"] is False
        assert ledger["counts"]["evidence_files_by_pair"] is None


def test_the_report_module_gives_the_route_a_one_line_entry():
    """导出路由只需要一行：`coverage=report_document.coverage_for_run(run)`。

    它必须与账本自己那条路径给出**同一份**结果（两个入口算两遍，迟早会不一样）。
    """
    with app.app_context():
        detail = json.dumps(
            {"items": 1, "details": [_fetched("file_diff", LATEST, "a.lua")]}, ensure_ascii=False
        )
        run = _run_with_trace(executed_json=detail, tool_stats=_stats())

        assert doc.coverage_for_run(run) == ledger_mod.ledger_from_run(run)
        assert doc.coverage_for_run(run)["counts"]["evidence_files_by_pair"] == 1


# ---------------------------------------------------------------------------
#  七、文档口径：两份说明与代码同源
# ---------------------------------------------------------------------------
def _read(relative_path: str) -> str:
    with open(os.path.join(PROJECT_ROOT, relative_path), encoding="utf-8") as handle:
        return handle.read()


def test_the_docs_no_longer_promise_that_every_file_was_read():
    """「它翻完了」「让平台读完整个版本的变更」这类说法会被读成「几百个都看过了」。

    那是这个功能**当初**的定位（`docs/AI分析使用说明.md` 里那句话引入于 18a95d9，
    `templates/help.html` 有同款），而实测是 996 个改动文件里 60 个取到过证据。平台从不
    承诺「每个文件都读一遍」，两份说明也不能替它承诺 —— 它们要说的是准确的那三件事：
    **枚举完整清单、对全部改动做确定性预检、对风险候选取证**，并如实写出覆盖与缺口。
    """
    doc_text = _read("docs/AI分析使用说明.md")
    help_text = _read("templates/help.html")

    assert "把全部变更清单读完的助手" not in doc_text, "说明文档还在说「它把清单读完了」"
    assert "它翻完了" not in doc_text, "说明文档还在说「它翻完了」"
    assert "让平台读完整个版本的变更" not in help_text, "帮助页还在说「它读完整个版本」"

    for label, text in (("说明文档", doc_text), ("帮助页", help_text)):
        assert "覆盖与缺口" in text, f"{label}没有写「覆盖与缺口」"
        assert "不等于「整个版本都看过了」" in text, (
            f"{label}没有写清「分析范围：全量」是分析口径、不等于整个版本都看过了"
        )
        assert "枚举" in text and "取证" in text, f"{label}没有说清它到底是怎么看的"


def test_the_docs_spell_out_both_dedup_conventions_and_where_the_numbers_show():
    """**两条去重口径都要写清**（只写一个，同一份数据就有两个覆盖率），并且要说清它们
    出现在哪儿 —— 报告与导出文档里那几行。"""
    doc_text = _read("docs/AI分析使用说明.md")
    help_text = _read("templates/help.html")

    assert "按「(本批次最新提交, 文件)」去重" in doc_text
    assert "按「文件」去重" in doc_text
    for label, text in (("说明文档", doc_text), ("帮助页", help_text)):
        assert "保守" in text and "宽松" in text, f"{label}没有说清哪一份才是报告用的口径"
        assert "去重" in text, f"{label}没有写「去重口径」这四个字"

    # 文档里写的那些口径，与账本真正输出的行是同一批字（少了这一段，读者对不上号）
    rows = dict(
        ledger_mod.build_ledger(
            request_payload=_weekly_payload(files=[("a.lua", LATEST), ("b.lua", LATEST)]),
            executed=[_fetched("file_diff", LATEST, "a.lua")],
            tool_stats=_stats(),
        )["rows"]
    )
    for name in ("覆盖（版本清单）", "覆盖（列出的名字）", "覆盖（取到证据）"):
        assert name in rows, f"账本没有输出「{name}」这一行"
        assert name in doc_text, f"说明文档没有解释「{name}」这一行是什么意思"



# ---------------------------------------------------------------------------
#  五、提交数的两个口径：**由程序给出来，摆在报告里**
# ---------------------------------------------------------------------------


def _commits_ledger(*, window_ids, files, listed=None, window=None):
    payload = _weekly_payload(files=files, listed=listed, window=window)
    if window_ids is not None:
        payload["window_commit_ids"] = list(window_ids)
    return ledger_mod.build_ledger(
        request_payload=payload, executed=[], tool_stats=_stats()
    )


def test_the_two_commit_counts_are_rendered_into_the_report():
    """实测 run 54：冻结窗口有 **4** 条提交，报告开篇写成了 **2** 条。

    三个数（本窗口实际提交数 / 文件最新提交数 / 按文件累加的合并提交数）本来就在提示词里
    各说各的，问题是**正文仍能挑一个错的**。所以把前两个交给程序渲染、摆进报告。

    判据落在「与输入账一致」上，而不是落在某句措辞上：`window_commit_ids` 是写侧冻结的
    窗口清单，`delta_files` 的 `latest_commit_id` 是本次输入。两个数各自等于它们的去重个数。
    """
    window_ids = ["w1" * 20, "w2" * 20, "w3" * 20, "w4" * 20]
    files = [("a.xlsx", LATEST), ("b.lua", LATEST), ("c.lua", OLDER)]

    ledger = _commits_ledger(window_ids=window_ids, files=files)
    rows = dict(ledger["rows"])

    assert "4" in rows["提交（本窗口）"], rows["提交（本窗口）"]
    assert "2" in rows["提交（本次输入）"], rows["提交（本次输入）"]
    assert ledger["counts"]["window_commits"] == len(set(window_ids))
    assert ledger["counts"]["input_commits"] == len({c for _, c in files})


def test_the_same_two_numbers_reach_both_readers_verbatim():
    """报告里那两行与导出文档里那两行**是同一批字** —— 读者不该看到两个版本。"""
    ledger = _commits_ledger(window_ids=["w1" * 20, "w2" * 20], files=[("a.lua", LATEST)])

    doc_text = doc.build_report_markdown(
        project_label="P", report_text="正文", coverage=ledger
    )
    from services.ai.result_payload import coverage_notice_text

    notice = coverage_notice_text(ledger)

    for name in ("提交（本窗口）", "提交（本次输入）"):
        for value in (dict(ledger["rows"])[name],):
            assert name in doc_text and value in doc_text, f"导出文档里缺「{name}」这一行"
            assert name in notice and value in notice, f"抽屉那份里缺「{name}」这一行"


def test_a_payload_without_the_window_list_says_unknown_not_zero():
    """老 payload 没有 `window_commit_ids` 时写「未记录」，**不许写 0**。

    写成 0 就是替用户断言「这个窗口一条提交都没有」—— 与 `cache_read_tokens` 那几个
    字段同一条口径（`None` ≠ `0`）。
    """
    rows = dict(_commits_ledger(window_ids=None, files=[("a.lua", LATEST)])["rows"])

    assert rows["提交（本窗口）"] == ledger_mod.UNKNOWN
    assert "0" not in rows["提交（本窗口）"]
    # 本次输入那个数是**算得出来的**（`delta_files` 就在 payload 里），所以它照常给出。
    assert "1" in rows["提交（本次输入）"]


def test_a_single_commit_run_does_not_get_window_rows():
    """单提交模式没有「窗口 vs 本次输入」这回事，写这两行只会让人去找第二份清单。"""
    payload = {
        "mode": "commit",
        "scope": "full",
        "commit": {"commit_id": LATEST, "path": "a.lua", "message": "m", "author": "x"},
    }
    rows = dict(ledger_mod.build_ledger(request_payload=payload, executed=[], tool_stats={})["rows"])

    assert "提交（本窗口）" not in rows
    assert "提交（本次输入）" not in rows


def test_the_same_path_in_two_repositories_is_two_coverages():
    """**同一个路径在两个仓库里各有一条时，两条都要算**（P1a）。

    原先白名单收成 `{路径: 提交}`，后写的那条顶掉先写的：读的是 A 仓库那一版，却拿 B
    仓库那条提交做匹配，匹配不上就被当成「没看过」—— 覆盖账于是少报，而报告里那个数
    正是「这次到底看了多少」的唯一出口。
    """
    payload = _weekly_payload(
        files=[("config/[30]道具表.xlsx", LATEST), ("config/[30]道具表.xlsx", LATEST)],
        repositories={0: 1, 1: 2},
    )
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[
            _fetched("file_diff", LATEST, "config/[30]道具表.xlsx", repository_id=1),
            _fetched("file_diff", LATEST, "config/[30]道具表.xlsx", repository_id=2),
        ],
        tool_stats=_stats(),
    )

    evidence = ledger["evidence_coverage"]
    assert evidence["by_pair"]["covered"] == 2, "两个仓库各算一条覆盖"
    assert evidence["by_path"]["covered"] == 1, "宽松口径按路径去重，还是一个文件"
    assert ledger["counts"]["pending_files"] == 0


def test_reading_one_repository_does_not_cover_the_other():
    """反方向：只读了 A 仓库那一份，B 仓库那条**不算看过**（这正是「两个仓库各一条」的意义）。"""
    payload = _weekly_payload(
        files=[("config/[30]道具表.xlsx", LATEST), ("config/[30]道具表.xlsx", LATEST)],
        repositories={0: 1, 1: 2},
    )
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[
            _fetched("file_diff", LATEST, "config/[30]道具表.xlsx", repository_id=1),
        ],
        tool_stats=_stats(),
    )

    assert ledger["evidence_coverage"]["by_pair"]["covered"] == 1
    assert ledger["counts"]["pending_files"] == 1


def test_a_row_without_a_repository_still_counts():
    """老行没有 `repository_id` ⇒ **不许**因此判成「没看过」（那会让覆盖率永远差一截）。"""
    payload = _weekly_payload(
        files=[("config/[30]道具表.xlsx", LATEST)], repositories={0: 1},
    )
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "config/[30]道具表.xlsx")],
        tool_stats=_stats(),
    )

    assert ledger["evidence_coverage"]["by_pair"]["covered"] == 1


def test_the_wrong_commit_is_still_not_covered():
    """提交对不上仍然是「没看过这一版」—— 放宽到只看路径会让覆盖率虚报。"""
    payload = _weekly_payload(files=[("a.xlsx", LATEST)], repositories={0: 1})
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", OLDER, "a.xlsx", repository_id=1)],
        tool_stats=_stats(),
    )

    assert ledger["evidence_coverage"]["by_pair"]["covered"] == 0
    assert ledger["evidence_coverage"]["by_path"]["covered"] == 1


# ---------------------------------------------------------------------------
#  六、一眼账与结论账（P1b-UI）：**把两个比值和两个条数摆在同一行**
# ---------------------------------------------------------------------------


def _rows_of(ledger):
    return dict(ledger["rows"])


def test_the_headline_row_carries_both_ratios():
    """「输入 / 窗口」与「取证 / 输入」**在同一条里**。

    实测里读者（和模型）拿到的是十几行各自正确的数，却拼不出这两个比值 —— 于是把
    「分析范围：全量」（那是**范围**口径）读成了「都看过了」。分子分母必须挨着写。
    """
    payload = _weekly_payload(
        files=[("a.xlsx", LATEST), ("b.lua", LATEST), ("c.lua", LATEST)],
        window=1343,
    )
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "a.xlsx")],
        tool_stats=_stats(),
    )
    headline = _rows_of(ledger)["本次覆盖"]
    assert "输入 3 / 窗口 1343" in headline, headline
    assert "取证 1 / 输入 3" in headline, headline
    # 两个比值**都不是 100%**，而这一行不许把它们说成一个数
    assert "别把它们读成一个" in headline


def test_the_headline_says_unknown_instead_of_zero_without_evidence():
    """没有逐轮明细 ⇒ 取证那一半是**未记录**，不是 0（与整份账本同一条纪律）。"""
    ledger = ledger_mod.build_ledger(
        request_payload=_weekly_payload(files=[("a.xlsx", LATEST)], window=10),
        executed=None,
        tool_stats=_stats(),
    )
    headline = _rows_of(ledger)["本次覆盖"]
    assert "取证未记录 / 输入 1" in headline, headline
    assert "取证 0" not in headline


def test_a_single_commit_run_does_not_pretend_there_is_a_window():
    """单提交模式没有「窗口」这一层，写了会让读者去找一份不存在的版本清单。"""
    payload = _weekly_payload(files=[("a.xlsx", LATEST)], window=10)
    payload["mode"] = "commit"
    ledger = ledger_mod.build_ledger(
        request_payload=payload, executed=[], tool_stats=_stats()
    )
    assert "窗口" not in _rows_of(ledger)["本次覆盖"]


def test_the_findings_row_splits_this_round_from_the_inherited_ones():
    """run 58：正文写「主结论共 13 条」，而载荷里是 50 条（13 本轮 + 37 继承）。

    两个数各自都对，但它们不是同一个集合 —— 不拆开写，读者只能猜是哪个错了。
    """
    payload = _weekly_payload(files=[("a.xlsx", LATEST)], window=10)
    response = {
        "final_findings": (
            [{"fingerprint": f"s{index}", "source": "synthesis", "active": True} for index in range(13)]
            + [{"fingerprint": f"b{index}", "source": "baseline", "active": True} for index in range(37)]
            + [{"fingerprint": "r1", "source": "synthesis", "active": False}]
        )
    }
    ledger = ledger_mod.build_ledger(
        request_payload=payload, executed=[], tool_stats=_stats(), response_payload=response
    )
    row = _rows_of(ledger)["结论（本轮 / 继承）"]
    assert "本轮新增 13 条 + 基线继承 37 条 = 在挂 50 条" in row, row
    assert "报告正文只写本轮那几条" in row
    assert "另有 1 条已按复核裁决撤销" in row


def test_without_the_findings_account_the_row_is_absent_not_zero():
    """老运行 / 失败运行取不到这份数据 ⇒ **一个字都不说**（写成 0 条比不说更糟）。"""
    ledger = ledger_mod.build_ledger(
        request_payload=_weekly_payload(files=[("a.xlsx", LATEST)], window=10),
        executed=[],
        tool_stats=_stats(),
    )
    assert "结论（本轮 / 继承）" not in _rows_of(ledger)
    assert ledger["findings"] == {"recorded": False}


def test_both_rows_reach_the_drawer_text():
    """这两行是**服务端**给的：抽屉那一段（`coverage_notice`）必须逐字带着它们 ——
    否则屏幕上还是那十几个拼不出比值的数。"""
    from services.ai.result_payload import coverage_notice_text

    payload = _weekly_payload(
        files=[("a.xlsx", LATEST), ("b.lua", LATEST)], window=1343
    )
    ledger = ledger_mod.build_ledger(
        request_payload=payload,
        executed=[_fetched("file_diff", LATEST, "a.xlsx")],
        tool_stats=_stats(),
        response_payload={
            "final_findings": [
                {"fingerprint": "s1", "source": "synthesis", "active": True},
                {"fingerprint": "b1", "source": "baseline", "active": True},
            ]
        },
    )
    text = coverage_notice_text(ledger)
    assert "本次覆盖" in text and "输入 2 / 窗口 1343" in text, text
    assert "取证 1 / 输入 2" in text, text
    assert "本轮新增 1 条 + 基线继承 1 条" in text, text
    # 覆盖段仍然**只是** payload 里的一段文本：报告正文那份一个字都不动（另有用例钉着）。
    assert doc.coverage_table_rows(ledger), "导出那条路读的是同一份 rows"
