# -*- coding: utf-8 -*-
"""抽屉里那份报告也要显示「证据覆盖与缺口」（审计 P1-03）—— 而**报告正文一个字不动**。

## 缺陷形态（真机验证）

导出那份 `.md` 里「本次覆盖与缺口」「覆盖（版本清单）」「取到证据」齐全、数字也诚实
（997 个文件、只列 200 个名字、取数失败 10 次、截断 30 条），而**写库那份**
（`response_payload.report_markdown` —— 抽屉与 `/latest` 读的就是它）里这些关键词
**一个都没有**。审计要求是「报告必须显示证据覆盖与缺口」，而抽屉才是用户第一眼看到的地方。

## 这一组守什么

1. **覆盖段真的到得了抽屉**：落库时挂进 payload（`coverage_notice`），并且前端那段渲染
   真的会把它贴到屏幕上（用 node 真跑 `static/js/ai_context_notice.js`）；
2. **报告正文逐字不变**：覆盖段**不许**进 `report_markdown` / `response_text`；
3. **验收核心**：覆盖段里列着**取数失败的文件路径**（例如
   `config/奖励模式表_CfgRewardMode.xlsx`），而这条路径**不能**让一条针对该文件、
   本该判「没有进入最终结论清单」的候选变成「已采纳」。对账（`family_ledger`）拿报告文本
   做字符串判据（候选编号、结论清单里点名了哪个文件），方向是**静默**的：判成「已采纳」
   那条真缺口就从平台自己的账上消失了 —— 正是本函数最不该出的那种错。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import app as flask_app
from app import create_tables, db
from models import Project
from models.ai_analysis import AiAnalysisRun, AiAnalysisTrace
from services.ai import coverage_ledger
from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome, RoundRecord
from services.ai.family_ledger import Candidate, reconcile_candidates
from services.ai.protocol import Anomaly
from services.ai.result_payload import result_payload
from services.ai_analysis_service import _persist_outcome

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOTICE_SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_context_notice.js"

# 全 40 位的提交号。白名单里存完整号，逐轮明细的标签里只有 12 位前缀。
LATEST = "f844faa6f59c3f1b571226e92ac03dbd22a6b790"
# **取数失败的那张表**：真机上覆盖段列的就是这种路径，而它也是那条候选的文件。
FAILED_PATH = "config/奖励模式表_CfgRewardMode.xlsx"
# 另一份真的取到了内容的文件（让覆盖率不是 0/2，避免用例在「全是失败」这个退化形态上
# 也能通过 —— 那样它证明不了「正常值也在」）。
FETCHED_PATH = "code/ConfMod.lua"
# 模型写的报告正文：刻意**不提**那张失败的表（真机那次它写在缺口的正文里，这里不需要）。
REPORT_BODY = "# 变更理解\n\n本轮改动集中在配表与战斗逻辑。\n"


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _weekly_payload(project_id: int, group_key: str) -> dict:
    """一次周版本分析的 `request_payload`（形状照 `build_weekly_payload` 落库的那一份）。"""
    files = [(FAILED_PATH, LATEST), (FETCHED_PATH, LATEST)]
    return {
        "mode": "weekly",
        "scope": "full",
        "focus": {"key": "all", "label": ""},
        "group": {
            "key": group_key, "base_name": "W", "project_id": project_id,
            "config_ids": [1], "start_time": None, "end_time": None,
        },
        "summary": {
            "total_files": 4, "delta_files": 2, "batch_files": 2, "window_files": 4,
        },
        "delta_files": [
            {"file_path": path, "latest_commit_id": LATEST} for path, _ in files
        ],
        "list_files": [{"file_path": path} for path, _ in files],
        "policy": {"truncated": False, "truncation_reason": None},
    }


def _fetched(kind: str, path: str, text: str) -> SimpleNamespace:
    """逐轮明细里的一条（`trace_evidence.summarize_executed` 按属性读它）。"""
    return SimpleNamespace(
        kind=kind,
        label=f"{kind} {LATEST[:12]} {path}",
        text=text,
        meta={},
    )


def _outcome() -> EngineOutcome:
    """这一次分析：一份取到了内容、一份**取数失败**（覆盖段会把失败那个路径列出来）。"""
    return EngineOutcome(
        status=STATUS_SUCCEEDED,
        report_markdown=REPORT_BODY,
        rounds=(
            RoundRecord(
                index=1,
                status="requests",
                item_count=2,
                executed=(
                    _fetched("file_content", FETCHED_PATH, "function ConfMod:run() end"),
                    _fetched(
                        "file_diff",
                        FAILED_PATH,
                        "[取数失败] 平台读不到这份差异：这一份没有可展示的改动",
                    ),
                ),
            ),
        ),
        # 按工具计数（与消耗面板同源）：失败一次 —— 覆盖段的「取数失败 N 次」读它。
        tool_stats={"file_diff": {"failed": 1, "truncated": 0, "refused_by_budget": 0}},
        requests_used=2,
    )


def _persisted_payload() -> dict:
    """**走真实的落库那一段**：建 run → 跑一次 `_persist_outcome` → 读回 payload。

    只读回内存里的 `run.response_payload` 不算数：抽屉读的是**写进库再读出来**的那一份
    （`/latest` 走 `response_payload` 列），序列化这一环如果丢了新键，屏幕上就还是看不见。
    """
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("CV"), name="覆盖落库用例")
        db.session.add(project)
        db.session.flush()

        group_key = f"g-{uuid.uuid4().hex[:8]}"
        payload = _weekly_payload(project.id, group_key)
        run = AiAnalysisRun(
            project_id=project.id,
            target_type="weekly",
            target_id=1,
            target_key=group_key,
            status="running",
            scope="full",
            trigger_source="manual",
            request_payload=json.dumps(payload, ensure_ascii=False),
        )
        db.session.add(run)
        db.session.flush()

        outcome = _outcome()
        result = result_payload(outcome, payload)
        _persist_outcome(run, outcome, result)
        db.session.refresh(run)
        stored = json.loads(run.response_payload)
        return {
            "run_id": run.id,
            "payload": stored,
            # **同一个对象**：`_persist_outcome` 是原地改它，而调用方拿它当 SSE 的
            # `result` 事件下发 —— 于是「刚跑完」那一次屏幕上也有这一段，不必等
            # `/latest` 回来。
            "in_memory": result,
            "response_text": run.response_text,
        }


def _cleanup(seeded: dict) -> None:
    """删掉这条 run 与它的明细（测试库是会话级共用的，留着会污染别的用例）。"""
    with flask_app.app_context():
        AiAnalysisTrace.query.filter_by(run_id=seeded["run_id"]).delete()
        AiAnalysisRun.query.filter_by(id=seeded["run_id"]).delete()
        db.session.commit()


# ==========================================================================
#  一、覆盖段真的到得了抽屉
# ==========================================================================


def test_the_stored_payload_carries_the_coverage_section():
    """**核心断言（正面）**：写进库的 payload 里有「本次覆盖与缺口」，且数字诚实。"""
    seeded = _persisted_payload()
    try:
        notice = seeded["payload"].get("coverage_notice") or ""
        assert "本次覆盖与缺口" in notice, f"落库的 payload 里没有覆盖段：{list(seeded['payload'])}"
        assert "覆盖（版本清单）" in notice, notice
        assert "覆盖（取到证据）" in notice, notice
        # 数字来自账本，不是另算一份：这一批 2 个文件、取到 1 个、失败 1 次。
        assert "1 / 2" in notice, notice
        assert "取数失败 1 次" in notice, notice
        # **失败的路径要在覆盖段里** —— 下面那条验收核心就是在最坏情况（路径出现在
        # 平台自己写的那段字里）下成立的。
        assert FAILED_PATH in notice, notice
        # 落库那一次**同时**把这一段交给了调用方（SSE 的 `result` 事件就是它）：
        # 少了这一半，「刚跑完」那一次要等 `/latest` 回来才看得见。
        assert seeded["in_memory"].get("coverage_notice") == notice, (
            "覆盖段只写进了库、没留给调用方 —— 刚跑完那一次屏幕上还是没有"
        )
    finally:
        _cleanup(seeded)


def test_the_drawer_renders_the_coverage_section_on_screen():
    """前端那一段**真跑一遍**：`withContextNotice(正文, payload)` 里要有覆盖段。

    与 `tests/test_ai_context_notice.py` 同一个做法（node 真跑那份 js）：静态断言看不出
    「这个键被读了吗」，而漏读的表现是**静默**的 —— 库里啥都有，屏幕上什么都没有。
    """
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的渲染断言")
    seeded = _persisted_payload()
    try:
        driver = f"""
const fs = require('fs');
const vm = require('vm');
const sandbox = {{ window: {{}} }};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(NOTICE_SCRIPT))}, 'utf8'), sandbox);
const payload = {json.dumps(seeded["payload"], ensure_ascii=False)};
const body = {json.dumps(seeded["response_text"], ensure_ascii=False)};
process.stdout.write(JSON.stringify({{
    coverage: sandbox.AiContextNotice.coverageNotice(payload),
    composed: sandbox.AiContextNotice.withContextNotice(body, payload)
}}));
"""
        proc = subprocess.run(
            ["node", "-e", driver], capture_output=True, text=True,
            encoding="utf-8", cwd=str(PROJECT_ROOT), timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)

        assert "本次覆盖与缺口" in out["coverage"], out["coverage"][:200]
        assert FAILED_PATH in out["coverage"], out["coverage"][:200]
        # 贴到屏幕上的是「正文 + 覆盖段」：正文一个字节不少，覆盖段跟在后面。
        assert out["composed"].startswith(seeded["response_text"].strip()[:10]), out["composed"][:200]
        assert "本次覆盖与缺口" in out["composed"], out["composed"][:200]
    finally:
        _cleanup(seeded)


# ==========================================================================
#  二、报告正文一个字不动（否则会污染拿它做判据的那几处）
# ==========================================================================


def test_the_coverage_section_never_enters_the_report_text():
    """覆盖段落**不许**进 `report_markdown` / `response_text`。

    它们不是「给人看的文本」那么简单：`family_ledger.reconcile_candidates` 拿它核对候选
    编号与候选的文件名、`verdict.read_ruling` 从里面取回复核裁决块、下一轮的基线摘要也读
    它。平台自己追加的段落混进去，等于往这些判据里塞平台自己的字。
    """
    seeded = _persisted_payload()
    try:
        report = seeded["payload"].get("report_markdown") or ""
        assert report == REPORT_BODY, "报告正文被改写了（覆盖段不该进正文）"
        for marker in ("本次覆盖与缺口", "覆盖（取到证据）", FAILED_PATH, "平台补充"):
            assert marker not in report, f"覆盖段的内容混进了报告正文：{marker}"
        assert marker not in (seeded["response_text"] or "")
    finally:
        _cleanup(seeded)


# ==========================================================================
#  三、验收核心：覆盖段里的失败路径**不能**变成「已采纳」的证据
# ==========================================================================


def _candidate() -> Candidate:
    """一条针对**取数失败那张表**的候选（这正是真机那次的情形）。"""
    return Candidate(
        member_label="S1",
        index=1,
        anomaly=Anomaly(
            title="【奖励模式表】新建表内仅含一条测试数据，且与两份同域表并存",
            category="code_logic",
            severity="mid",
            confidence="mid",
            evidence=["提交 f844faa6f59c：新增工作表"],
            commit=LATEST,
            file_path=FAILED_PATH,
            impact="三表并存时读取会拿到空表",
            suggestion="确认是否需要保留新建的那张表",
        ),
    )


def test_the_failed_path_in_the_coverage_section_is_not_adoption_evidence():
    """**本任务的验收核心**：覆盖段里出现某个文件的路径，**不会**让一条针对该文件、
    本该判「没有进入最终结论清单」的候选变成「已采纳」。

    构造的是最坏情况：那条候选的文件正是**覆盖段里点名列出的取数失败路径**，而结论清单
    （`anomalies`）里没有它、报告正文也没提它。对账必须仍然报「没有进入最终结论清单」——
    说成「已采纳」是**静默**的（平台自己那条缺口就此消失，报告读起来完全正常）。
    """
    seeded = _persisted_payload()
    try:
        stored = seeded["payload"]
        notice = stored.get("coverage_notice") or ""
        assert FAILED_PATH in notice, (
            "覆盖段没有作为**独立的一段**交到读侧（`coverage_notice`）—— 它要么没落库，"
            "要么被写进了报告正文（正文是对账拿来做字符串判据的那份文本）"
        )

        # 对账读的是落库的报告正文 + 结论清单（`family_ledger._report_text` 那两份）。
        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown=stored.get("report_markdown") or "",
            anomalies=(),
            dropped=(),
        )
        text, dropped = reconcile_candidates((_candidate(),), synthesis)

        assert dropped, (
            "覆盖段里出现了候选的文件路径，对账就把它判成「已采纳」了 —— "
            "那条真缺口会从平台的账上静默消失"
        )
        assert "[S1-1]" in dropped[0].detail, dropped[0].detail
        assert "没有进入最终结论清单" in text, text
        # 正文里没提它 → 措辞也不该说「正文里出现过这个文件」。
        assert "正文里出现过" not in text, text
    finally:
        _cleanup(seeded)


def test_a_failed_path_names_the_file_but_never_adopts_it():
    """把这份覆盖段**塞进正文**（错误实现会这么做）也仍然不算采纳。

    这一条钉的是那条取舍：对账的第 3 手**只看结论清单**，不看报告正文 —— 正文正是模型写
    「这一块我没查到」的地方，拿它当采纳证据会把**真缺口**说成「已有去向」。所以本任务的
    保证有两层：这里是行为层（正文里的路径不算数），上面那条是结构层（覆盖段压根不进正文）。
    """
    covered_text = "# 变更理解\n\n" + coverage_section_with_failed_path()
    synthesis = EngineOutcome(
        status=STATUS_SUCCEEDED, report_markdown=covered_text, anomalies=(), dropped=(),
    )

    _, dropped = reconcile_candidates((_candidate(),), synthesis)

    assert dropped, "正文里的路径被当成了采纳证据"


def coverage_section_with_failed_path() -> str:
    """照落库那一段的形状，手工拼一份覆盖段（给上面那条用例当「错误实现」的输入）。"""
    ledger = coverage_ledger.build_ledger(
        request_payload={
            "mode": "weekly",
            "summary": {"batch_files": 2, "window_files": 4},
            "delta_files": [
                {"file_path": path, "latest_commit_id": LATEST}
                for path in (FAILED_PATH, FETCHED_PATH)
            ],
            "list_files": [{"file_path": FAILED_PATH}, {"file_path": FETCHED_PATH}],
        },
        executed=[
            {
                "kind": "file_diff", "label": FAILED_PATH, "chars": 0,
                "failed": True, "empty": False,
            }
        ],
        tool_stats={"file_diff": {"failed": 1}},
    )
    from services.ai.result_payload import coverage_notice_text

    return coverage_notice_text(ledger)
