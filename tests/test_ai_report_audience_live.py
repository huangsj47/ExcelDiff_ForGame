"""Live QA: resolve project context before producing planner-facing reward prose.

Run explicitly with: python -m pytest -m live tests/test_ai_report_audience_live.py -q
"""

import hashlib
import io
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from services.ai.engine import EngineLimits, run_analysis
from services.ai.excel_source import _read_excel_sheets
from services.ai.llm_client import LLMClient
from services.ai.result_payload import result_payload
from services.ai.scope import AnalysisScope
from services.ai.skill_loader import SkillDocument, load_skills
from tests.test_ai_live_endpoint import _is_reachable, _live_settings

pytestmark = pytest.mark.live


def test_reward_report_reads_background_and_keeps_plain_prose_grounded(tmp_path):
    root = Path(__file__).resolve().parents[1]
    endpoint = _live_settings()
    if not endpoint or not _is_reachable(endpoint[0]):
        pytest.skip("Live model endpoint unavailable")
    commit = "e" * 40
    references = {
        "world-guide.md": "项目「暮岛行动」是小队出航、搜集物资、回港结算的游戏。玩家在船上用星币购买补给，周榜按出航积分结算。星币与装扮/补给物品没有公布的通用兑换比。物品必须按(kind,id)复合主键识别，不同类型的同编号不是同一物品。",
        "reward-catalog.md": "同冻结版本资料：奖励模式501用于出航周榜第6名，502用于第1名；不是前两档，不能按行顺序或物品价值猜名次。物品(kind=7,id=200001)为星币，船上购买补给用；(kind=12,id=901)为荧火徽章，是永久装扮，不可出售；(kind=16,id=9)为越野补给箱，可打开获得出航消耗品，不可直接出售。两种新物品都没有货币售价，资料未给价值折算或应与原奖励等价的设计规则。规则由人工确认。",
    }

    def doc(name, body, description):
        return SkillDocument(
            name=name,
            description=description,
            path=root / ".pytest_tmp" / name,
            text=body,
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
        )

    loaded = load_skills(root, profile="tables")
    manifest = doc(
        "sample-project",
        "暮岛行动项目知识包。背景见world-guide.md；奖励模式的用途和物品双键名称见reward-catalog.md。请使用read_reference读取适用资料。",
        "项目知识索引",
    )
    project_docs = tuple(
        doc(name, body, "游戏背景与玩法" if "world" in name else "奖励模式用途、复合主键与物品名称目录")
        for name, body in references.items()
    )
    loaded = replace(
        loaded,
        project_manifest=manifest,
        project_references=project_docs,
        readable={**loaded.readable, **{d.name: d.path for d in project_docs}},
        project_slug="sample-project",
    )
    diffs = {
        "tables/Rewards.xlsx": "RewardMode physical rows3/4: ID501 fixedReward 7,200001,20000 -> 12,901,1; ID502 fixedReward 7,200001,50000 -> 16,9,1. Names and usage are not in diff; project knowledge pack indexes them.",
        "tables/Calendar.xlsx": "Event physical row2 id91 enabled1 Gregorian start date2027,2,28 -> 2027,2,30. Actual export entry runtime/export.py validates all enabled rows with datetime.datetime(year,month,day); unhandled ValueError stops export.",
    }
    bodies = {
        **diffs,
        "runtime/export.py": (
            "import datetime\n"
            "def export_calendar(event_rows):\n"
            "    # This entry exports ONLY Calendar. Rewards has an independent import.\n"
            "    # There is no shared batch transaction or dependency between the two.\n"
            "    result = []\n"
            "    for row in event_rows:\n"
            "        if row.enabled:\n"
            "            result.append(datetime.datetime(*row.start_date))\n"
            "    return result\n"
        ),
    }

    def workbook(sheet_name, rows):
        book = Workbook()
        book.active.title = sheet_name
        for row in rows:
            book.active.append(row)
        stream = io.BytesIO()
        book.save(stream)
        return stream.getvalue()

    workbooks = {
        "tables/Rewards.xlsx": workbook(
            "Main",
            [
                ["rewardModeId", "fixedReward", "usage"],
                [None, None, None],
                [501, "12,901,1", "见项目奖励目录的名次映射"],
                [502, "16,9,1", "见项目奖励目录的名次映射"],
            ],
        ),
        "tables/Calendar.xlsx": workbook(
            "Event",
            [
                ["id", "enabled", "start_date"],
                [91, 1, "2027,2,30"],
            ],
        ),
    }

    class Provider:
        def __init__(self):
            self.calls = []

        def file_diff(self, commit, path, repository_id=""):
            self.calls.append(("file_diff", path, ""))
            return diffs.get(path)

        def file_content(self, commit, path, lines="", repository_id=""):
            self.calls.append(("file_content", path, lines))
            if path in workbooks:
                return _read_excel_sheets(workbooks[path], max_rows=120, path=path, window=lines, char_budget=11000)
            return bodies.get(path)

        def find_references(self, query, path="", cursor="", max_output_chars=7800):
            self.calls.append(("find_references", query, cursor))
            return "Full fixture: tables/Rewards.xlsx, tables/Calendar.xlsx, runtime/export.py. Reward-mode consumers are described in project reference reward-catalog.md; no other fixture files exist."

        def commit_detail(self, commit, repository_id=""):
            return "\n".join(diffs)

        def read_reference(self, name):
            self.calls.append(("read_reference", name, ""))
            return references.get(name) or next((d.text for d in loaded.platform_references if d.name == name), None)

    scope = AnalysisScope.from_iterables(
        commits=[commit], paths_by_commit={commit: set(bodies)}, readable_references=loaded.readable
    )
    scope = replace(scope, input_paths=tuple(diffs))
    settings = _live_settings()
    assert settings and _is_reachable(settings[0])
    client = LLMClient(base_url=settings[0], api_key=settings[1], model=settings[2], timeout_seconds=180)
    provider = Provider()
    start = time.monotonic()

    def progress(p):
        print(
            json.dumps(
                dict(round=p.index, status=p.status, input=p.prompt_tokens, output=p.completion_tokens),
                ensure_ascii=False,
            ),
            flush=True,
        )

    outcome = run_analysis(
        client=client,
        provider=provider,
        loaded=loaded,
        scope=scope,
        change_summary="Two changed configuration tables. Frozen unchanged export implementation remains readable.\n"
        + "\n".join(f"{commit} [M] {p}" for p in diffs),
        coverage_files=tuple(SimpleNamespace(repository_id="", commit=commit, path=p) for p in diffs),
        limits=EngineLimits(max_rounds=5, max_tool_requests=20, prompt_char_budget=180000),
        on_round=progress,
    )
    result = result_payload(outcome, {})
    (tmp_path / "report.md").write_text(result["report_markdown"], encoding="utf-8")
    main = result["report_markdown"].split("## 取证索引（技术详情）", 1)[0]
    facts = dict(
        status=outcome.status,
        degradation=outcome.degradation,
        seconds=round(time.monotonic() - start, 2),
        input=outcome.prompt_tokens,
        output=outcome.completion_tokens,
        cache_read=outcome.cache_read_tokens,
        reader_summaries=[r.get("reader_summary", "") for r in result["report_items"]],
        tools=provider.calls,
        rounds=[
            dict(index=r.index, status=r.status, output=r.completion_tokens, finish=r.finish_reason)
            for r in outcome.rounds
        ],
    )
    (tmp_path / "result.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(facts, ensure_ascii=False), flush=True)
    assert outcome.status == "succeeded", facts
    assert any(t == "read_reference" and p == "reward-catalog.md" for t, p, w in provider.calls), facts
    assert "荧火徽章" in main and "越野补给箱" in main and "周榜" in main, main
    assert all(s and len(s) <= 100 and "…" not in s for s in facts["reader_summaries"]), facts
    assert "7,200001,20000" not in main and "12,901,1" not in main, main
    assert not any(bad in main for bad in ("下降一到两个数量级", "下降两个数量级", "收益降低万倍")), main

    # The catalog deliberately gives non-sequential ranks; the reader must use it,
    # rather than infer first/second places from row order or old currency amounts.
    assert "第6名" in main or "第 6 名" in main, main
    reward_cards = [r for r in result["report_items"] if "荧火徽章" in r.get("reader_change", "")]
    assert reward_cards, main
    assert all(
        word not in main
        for word in ("整批配置都无法", "Rewards 均无", "Rewards与Calendar均无", "奖励改动也一起落不了地")
    ), main
