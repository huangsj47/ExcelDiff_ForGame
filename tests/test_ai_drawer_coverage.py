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


def test_compensation_and_dependency_inputs_are_visible_in_scope_and_coverage_text():
    """补偿/依赖文件进入机器账后，也必须在人读报告中说明来源与数量。"""
    payload = _weekly_payload(1, "visible-extra-inputs")
    payload["summary"].update({"compensation_files": 2, "dependency_files": 1})
    payload["compensation_files"] = [
        {"file_path": "config/上轮未读A.xlsx", "reason": "previous_uncovered"},
        {"file_path": "code/上轮未读B.lua", "reason": "previous_failed"},
    ]
    payload["dependency_files"] = [
        {"file_path": "code/协议消费者.lua", "reason": "baseline_finding_reference"},
    ]

    from services.ai.change_set import _scope_note

    scope = _scope_note(payload)
    assert "2 个上轮未覆盖补偿项" in scope
    assert "1 个依赖核查项" in scope

    ledger = coverage_ledger.build_ledger(request_payload=payload, executed=[])
    assert ledger["counts"]["compensation_files"] == 2
    assert ledger["counts"]["dependency_files"] == 1
    rendered = "\n".join(f"{key}: {value}" for key, value in ledger["rows"])
    assert "上轮未覆盖补偿" in rendered
    assert "依赖核查" in rendered


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

    正文是「这一次分析的结论」。抽屉、导出、历史三条读路径拿它当报告给用户看，下一轮的
    基线摘要也从里面读（`services/ai/run_cache_source.py` 逐行 split `response_text`）。
    平台自己追加的覆盖段落混进去，等于把平台的记账字混进结论里。

    **2026-09-21 追记**：这段 docstring 原先还列了两条理由 —— `family_ledger.reconcile_candidates`
    拿正文核对候选编号与候选的文件名、`verdict.read_ruling` 从正文里取回裁决块 ——
    这两条现在都不成立了（P0-06 改成只按候选 ID 集合对账，P0-05 把裁决搬进
    `EngineOutcome.verdict` / 载荷的 `ruling` 字段）。两条理由消失、结论不变，
    所以这个测试保留：判据换成「正文是给用户看的结论，记账不是结论」。
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
    （`anomalies`）里没有它、报告正文也没提它。

    ## 2026-09-21 改写（AI-P0-06 之后）

    这条用例原先断言 `dropped` 非空、文案里出现「没有进入最终结论清单」。**那个断言在
    P0-06 之后不再成立，而且它本来就不该成立**：对账改成**只按候选编号**（候选清单 vs
    汇总回填的 `source_candidate_ids`）之后，这条 fixture 里汇总**一个编号都没交回** ——
    平台拿到的是「无法对账」，不是一个「这条被丢了」的证据。按旧口径报「没有进入最终
    结论清单」，是在**没有证据**的情况下替模型定罪：那条候选完全可能已经写进了正文，
    只是编号没回填。所以 W1（P0-06 的实现方）把它降级成一条**说清自己不知道**的话，
    并把这一档单列出来要求人工对照。

    这条用例因此改成钉**新的**那个口径（三条一起钉，缺一条都会漏掉一种坏改法）：
    ① 说清「无法按编号对账」，不假装对上了；② **不许**出现「没有进入最终结论清单」
    这种没有证据的定罪；③ `dropped` 为空 —— 不往平台的账上塞假缺口。
    """
    seeded = _persisted_payload()
    try:
        stored = seeded["payload"]
        notice = stored.get("coverage_notice") or ""
        assert FAILED_PATH in notice, (
            "覆盖段没有作为**独立的一段**交到读侧（`coverage_notice`）—— 它要么没落库，"
            "要么被写进了报告正文（正文是对账拿来做字符串判据的那份文本）"
        )

        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown=stored.get("report_markdown") or "",
            anomalies=(),
            dropped=(),
        )
        text, dropped = reconcile_candidates((_candidate(),), synthesis)

        assert "无法按编号对账" in text, (
            "汇总没有交回任何编号时，平台必须**明说自己对不了账**（并要求人工过一遍）—— "
            f"现在的文案是：{text!r}"
        )
        assert "没有进入最终结论清单" not in text, (
            "一个编号都没交回 ≠ 这条候选被丢掉了。把「对不了账」写成「没有进入最终结论"
            f"清单」是在没有证据的情况下定罪 —— 现在的文案是：{text!r}"
        )
        assert dropped == (), (
            f"对不了账的时候不该往账上塞缺口，实际塞了：{[d.detail for d in dropped]!r}"
        )
        # 覆盖段里出现了那条失败路径这件事，**全程不参与**对账判据。
        assert FAILED_PATH not in text
    finally:
        _cleanup(seeded)


def test_the_reconciliation_is_blind_to_the_report_body():
    """对账的结果**必须与报告正文无关** —— 同一批候选、同一个汇总，正文换个字，账不变。

    这是「正文里的路径不算采纳证据」那条保证的**最强形式**：不是去断言某个具体文案，
    而是断言这个函数**根本不看正文**。P0-06 之前它看（`family_ledger._report_text`
    拿正文做字符串判据：候选编号当子串、文件名出现过就算被提到），于是模型在正文里写一句
    「这一块我没查到，涉及 `config/xxx.xlsx`」就足以把一条**真缺口**洗成「已有去向」。

    差分形式比断言某个文案更难被绕过：只要有人把任何形式的正文匹配加回来，这两次调用的
    结果就会不同，这条用例立刻红。

    ## 为什么 fixture 里要**交回一条编号**（否则这条用例是假绿）

    只喂「一条编号都没交回」的话，`reconcile_candidates` 会走「无法按编号对账」那一档，
    而那一档把缺口清单**无条件清空** —— 于是就算有人把正文匹配加回来，两次调用结果照样
    相等，这条用例永远绿。所以这里让汇总交回 `S2-1`：`no_lineage` 为假、缺口判定真的
    跑起来；`S1-1` 既没被交回也没进最终清单，它是一条**真缺口**，两条臂都必须报出来。
    下面那条「先确认真的报了缺口」的断言就是防这个假绿的。
    """
    def _outcome(body: str) -> EngineOutcome:
        return EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown=body,
            anomalies=(_synth_anomaly(),),
            dropped=(),
        )

    # 正文里**点名**了那条候选的文件 + 它的分片编号，两种旧启发式都喂到嘴边。
    bait = (
        "# 变更理解\n\n"
        f"这一块我没查到，涉及 `{FAILED_PATH}`。\n"
        "分片 S1 的候选 1 值得再看一眼。\n"
    )
    candidates = (_candidate(), _other_candidate())
    with_bait = reconcile_candidates(candidates, _outcome(bait))
    without_bait = reconcile_candidates(candidates, _outcome("# 变更理解\n\n没别的了。\n"))

    # 先确认这一档**真的在报缺口** —— 否则下面的相等只是「两边都空」那种假绿。
    _, dropped = with_bait
    assert "S1-1" in " | ".join(item.detail for item in dropped), (
        "汇总交回了 S2-1 却没交回 S1-1，S1-1 就是一条真缺口 —— 平台必须报出来。"
        f"实际 dropped={[item.detail for item in dropped]!r}"
    )

    assert with_bait == without_bait, (
        "正文一变，对账结果就变了 —— 说明有人又在拿正文当判据了。\n"
        f"带诱饵：{with_bait!r}\n不带诱饵：{without_bait!r}"
    )


def _synth_anomaly() -> Anomaly:
    """汇总交回的那条结论：它**只**认领 `S2-1`（于是 `S1-1` 成了真缺口）。"""
    return Anomaly(
        title="【其它】汇总只认领了 S2-1",
        category="code_logic",
        severity="mid",
        confidence="mid",
        evidence=["提交 f844faa6f59c：改动"],
        source_candidate_ids=("S2-1",),
    )


def _other_candidate() -> Candidate:
    """第二条候选（`S2-1`）—— 它是被交回的那一条，用来让缺口判定真的跑起来。"""
    return Candidate(
        member_label="S2",
        index=1,
        anomaly=Anomaly(
            title="【其它】一条无关的候选",
            category="code_logic",
            severity="low",
            confidence="low",
            evidence=["提交 f844faa6f59c：改动"],
            file_path=FETCHED_PATH,
            impact="无",
            suggestion="无",
        ),
    )


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
