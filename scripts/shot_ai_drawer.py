#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AI 抽屉的两个标签真渲染出来截图（无头 Chrome，真模板/真 CSS/真模块）。

**为什么要有这个脚本**：这一刀改的全是**画出来才看得见**的东西 —— 标签条的位置、
两个面板的显隐、逐轮卡片的信息密度、抽屉里那点高度够不够。而其中一条是**只能真渲染
才能验的**：

    `.ai-drawer-panel[hidden] { display: none; }`

作者样式里的 `display: flex`（`.ai-analysis-output.is-empty` 是 (0,2,0)，模板那段内联
style 还排在 style.css 之后）会盖掉浏览器默认的 `[hidden]`。写漏了的表现是**两个面板
同时显示**（像一大堆重复内容），而任何静态断言都看不出来 —— 只有浏览器算出来的
`display` 才是答案。所以这里把两个面板的 `getComputedStyle().display` 打出来。

做法与 `scripts/shot_ai_usage_page.py` 同一套：临时库里造一份有代表性的数据 → 用真路由
取真响应 → 浏览器里渲染。

  * 页面 HTML 走 `GET /commits/<id>/diff/new`（真模板、真抽屉 DOM）；
  * `/ai-analysis/commit/<id>/latest` 与 `/ai-analysis/runs/<id>/usage` 两份响应**从真路由
    取回来**（不是手写的），所以键名与后端不会漂移；
  * 「跑动中」那一张用的是**真形状的进度载荷**：`RoundRecord` → `trace_evidence.live_round_entry`
    → `run_progress.ProgressSnapshot.to_dict()`，与 `/progress` 发出来的那一份逐字同源
    （脚本里跑不起一次真分析，只能在浏览器里用真模块把这帧喂进去）。

用法：
    python scripts/shot_ai_drawer.py [输出前缀]

产物写在 `.pytest_tmp/`（不进仓库）。数据全是造的，与线上无关。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 临时库：文件名里带 `pytest` 才会被 utils/db_safety 认成测试库。
SCRATCH_DB = ROOT / ".pytest_tmp" / "db" / "pytest_aidrawer_shot.db"
SCRATCH_DB.parent.mkdir(parents=True, exist_ok=True)
for suffix in ("", "-wal", "-shm"):
    Path(str(SCRATCH_DB) + suffix).unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{SCRATCH_DB}"
os.environ.setdefault("RUN_SCHEDULER", "0")

from app import app as flask_app  # noqa: E402
from app import create_tables  # noqa: E402
from auth.services import register_user  # noqa: E402
from models import AiAnalysisRun, Commit, Project, Repository, db  # noqa: E402
from models.ai_analysis import AiAnalysisAnomaly, AiProjectAnalysisConfig  # noqa: E402
from services.ai.provenance import current_provenance  # noqa: E402
from services.ai.trace_evidence import encode_evidence, live_round_entry  # noqa: E402

PASSWORD = "pw-123456"
STAMP = uuid.uuid4().hex[:6]
MODEL = "claude-sonnet-5"

_REPORT_SECTION = """# 变更理解

本次把「无敌帧」的判定从 `BattleMgr.update` 挪到了 `BattleMgr.onHit`，判定顺序因此
发生了变化：先算无敌、再算扣血。

# 影响面分析

- 战斗系统：命中结算链路 **必测**
- 技能系统：`PsSkSpc` 的特殊被动类型 7/27/28 已删除，同批仍有代码在改

# 风险评估

| 风险等级 | 高 |
|---|---|
"""

# 报告正文要**足够长**：这里有一条读数是「打开抽屉时滚动位置在不在顶部」，而短报告
# 撑不出滚动条、`scrollTop` 恒等于 0 —— 那条断言就退化成同义反复（假绿）。
# 六份约 1800px，900 高的视口里抽屉正文区只有约 700px，滚得动。
REPORT = "\n".join(
    f"## 第 {index} 份结论\n\n{_REPORT_SECTION}" for index in range(1, 7)
)


def _round_records() -> list:
    """造两条有代表性的逐轮记录（第一条含一条**取不到**的上下文）。"""
    from services.ai.budget import ContextItem
    from services.ai.engine import RoundRecord
    from services.ai.protocol import ContextRequest, DroppedItem

    return [
        RoundRecord(
            index=1, status="requests", request_count=2, item_count=2,
            requests=(
                ContextRequest("file_diff", "a1b2c3d4" * 5, "code/qz_pub/battle/BattleMgr.lua"),
                ContextRequest("file_content", "a1b2c3d4" * 5,
                               "config/60_skill/【64】特殊被动技能表.xlsx"),
            ),
            executed=(
                ContextItem("file_diff", "代码差异 code/qz_pub/battle/BattleMgr.lua",
                            "代码差异：code/qz_pub/battle/BattleMgr.lua\n"
                            "@@ -118,7 +118,9 @@\n+    if target.invincible then\n"),
                ContextItem("file_content", "正文 config/60_skill/【64】特殊被动技能表.xlsx",
                            "[取数失败] 项目没有绑定 Agent 节点，platform/agent 模式下平台本地"
                            "也没有这个仓库的工作副本，读不到文件正文。"),
            ),
            dropped=(DroppedItem("request", 2, "超出本次工具请求总预算（2 次），未执行",
                                 "file_diff code/qz_pub/skill/SkillMgr.lua"),),
            budget_notes=("有 1 个上下文请求因超出本次索取额度而未执行。",),
            response_text='{"status": "need_more_context", "reason": "先看战斗逻辑与被动技能表"}',
            prompt_tokens=180_000, completion_tokens=9_400,
            prompt_chars=210_000, context_chars=25_000, duration_ms=18_400,
        ),
        RoundRecord(
            index=2, status="final",
            response_text=REPORT,
            prompt_tokens=205_000, completion_tokens=12_800,
            prompt_chars=232_000, context_chars=25_000, duration_ms=21_300,
        ),
    ]


def _seed() -> dict:
    with flask_app.app_context():
        create_tables()
        admin_name = f"drawer{STAMP}"
        admin, error = register_user(admin_name, PASSWORD, role="platform_admin")
        assert admin is not None, error

        project = Project(code=f"DRAWER{STAMP}", name="战斗数值表")
        db.session.add(project)
        db.session.flush()

        repository = Repository(
            project_id=project.id, name="qz_config", type="git",
            url=f"https://example.invalid/{STAMP}.git", resource_type="code",
        )
        db.session.add(repository)
        db.session.flush()

        commit = Commit(
            repository_id=repository.id, commit_id="c" * 40,
            path="code/qz_pub/battle/BattleMgr.lua", operation="M",
            author="zhangsan", commit_time=datetime.now(timezone.utc),
            message="战斗：无敌帧判定顺序调整", status="pending",
        )
        db.session.add(commit)
        db.session.flush()

        # 项目配置行：模型名要从这里取，否则溯源里的 model 是空串，与这条运行对不上，
        # 页面会给结论挂一条「旧结论」徽章（另一条路径的观感，会干扰这一刀要看的东西）。
        db.session.add(AiProjectAnalysisConfig(project_id=project.id, api_model=MODEL))

        # 溯源三件套照**当前源码**写：不写的话这份结论会被判成「旧版规程产出」，
        # 抽屉上挂一条「旧结论」徽章 —— 那是另一条路径的观感，会干扰这一刀要看的东西。
        provenance = current_provenance(project.id)
        run = AiAnalysisRun(
            project_id=project.id, target_type="commit", target_id=commit.id,
            status="succeeded", scope="full", trigger_source="manual",
            response_text=REPORT, model=provenance["model"] or MODEL,
            prompt_version=provenance["prompt_version"],
            skill_version=provenance["skill_version"],
            rules_version=provenance["rules_version"],
            rounds_used=2, tool_requests_used=2,
            tokens_input=385_000, tokens_output=22_200,
            cache_read_tokens=240_000, cache_write_tokens=40_000,
            context_chars=50_000, duration_ms=39_700, anomalies_found=2,
            created_at=datetime.now(timezone.utc),
            response_payload=json.dumps(
                {"risk_level": "高", "risk_reasons": ["按变更规模估算"], "anomalies": []},
                ensure_ascii=False,
            ),
        )
        db.session.add(run)
        db.session.flush()

        for record in _round_records():
            from models.ai_analysis import AiAnalysisTrace

            db.session.add(AiAnalysisTrace(
                run_id=run.id, round_index=record.index, outcome=record.status,
                parsed_ok=record.status != "unparsable",
                tokens_input=record.prompt_tokens,
                tokens_output=record.completion_tokens,
                request_chars=record.prompt_chars,
                context_chars=record.context_chars,
                duration_ms=record.duration_ms,
                **encode_evidence(record),
            ))
        db.session.flush()

        # 上一次「失败」的运行：历次结论那个列表要有**不止一条**才看得出「翻看历史」
        # 是什么样子（一条是「只有一次结论」的空态，两条才有对比）。
        older = AiAnalysisRun(
            project_id=project.id, target_type="commit", target_id=commit.id,
            status="failed", scope="full", trigger_source="scheduled",
            response_text="", error_message="额度用完了：本月项目预算已超上限",
            model=provenance["model"] or MODEL,
            prompt_version=provenance["prompt_version"],
            skill_version=provenance["skill_version"],
            rules_version=provenance["rules_version"],
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
            finished_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
        db.session.add(older)
        db.session.flush()

        # **再补几条**（2026-09-25）：历次结论改成两栏之后，有一条只有真浏览器算得出来的
        # 读数 —— 「左列自己滚、右栏自己滚」。两条运行的列表撑不出滚动条，那条断言就会
        # 退化成同义反复（`scrollHeight == clientHeight`）。这几条都排在「现在」与那次
        # 失败之间，所以**最后一条仍然是那次失败的**（探针靠这条翻历史）。
        extra = []
        for index in range(8):
            # **变量名不能叫 `run`**：下面 `_seed` 的返回值与结构化的结论行都指着
            # 上面那一次运行。循环里覆盖了它，探针就会拿一份**没有逐轮明细**的运行去
            # 问 `/usage`，而报错的地方离这里很远（`assert ...["rounds"]`）。
            extra_run = AiAnalysisRun(
                project_id=project.id, target_type="commit", target_id=commit.id,
                status="succeeded", scope="full" if index % 2 else "incremental",
                trigger_source="manual", response_text=REPORT,
                model=provenance["model"] or MODEL,
                prompt_version=provenance["prompt_version"],
                skill_version=provenance["skill_version"],
                rules_version=provenance["rules_version"],
                rounds_used=3, tool_requests_used=5,
                tokens_input=180_000 + index, tokens_output=9_000 + index,
                anomalies_found=index % 3, duration_ms=30_000,
                created_at=datetime.now(timezone.utc) - timedelta(hours=index + 1),
                finished_at=datetime.now(timezone.utc) - timedelta(hours=index + 1),
                response_payload=json.dumps(
                    {"risk_level": "高", "anomalies": []}, ensure_ascii=False),
            )
            db.session.add(extra_run)
            db.session.flush()
            extra.append(extra_run.id)

        # 报告下面那块「结构化结论 + 处置」要有真行才复核得到：一条还没处置、一条已经
        # 被处置过（处置人/时间/备注都得显示出来）。**失败的运行一条都不建** ——
        # 翻到那一次时看到的正是「这一次没有落库的结构化结论条目」那个空态。
        db.session.add(AiAnalysisAnomaly(
            run_id=run.id, project_id=project.id, fingerprint="a1b2c3d4",
            title="【道具】ID 被删除但生成文件仍在", category="config_id",
            severity="critical", confidence="very_high",
            evidence=json.dumps(
                ["config/道具表.xlsx 删除了 ID 1001", "build/lua/CfgItem.lua 里 1001 还在"],
                ensure_ascii=False),
            commit_ref="c" * 40, file_path="config/道具表.xlsx",
            impact="老存档引用的道具失效", suggestion="确认是否有意下线",
        ))
        db.session.add(AiAnalysisAnomaly(
            run_id=run.id, project_id=project.id, fingerprint="d4e5f6a7",
            title="提交信息与改动不符", category="commit_msg",
            severity="high", confidence="high",
            evidence=json.dumps(["提交说「修数值」，实际改的是技能表"], ensure_ascii=False),
            commit_ref="c" * 40, file_path="config/技能表.xlsx",
            disposition="ignored", disposition_by="zhangsan",
            disposition_at=datetime.now(timezone.utc),
            disposition_note="误报，已核对",
        ))
        db.session.commit()
        return {"_admin": admin_name, "commit_id": commit.id,
                "project_id": project.id, "run_id": run.id, "older_run_id": older.id,
                # 历次结论里会出现的那几条（新的在前、那次失败的仍在最后）
                "history_run_ids": [run.id, *extra, older.id]}


def _csrf(client) -> str:
    with client.session_transaction() as session:
        token = session.get("_csrf_token", "")
        if not token:
            token = uuid.uuid4().hex
            session["_csrf_token"] = token
    return token


def _capture(ids: dict) -> tuple:
    client = flask_app.test_client()
    token = _csrf(client)
    client.post("/auth/login",
                data={"username": ids["_admin"], "password": PASSWORD, "csrf_token": token},
                headers={"X-CSRFToken": token}, follow_redirects=False)

    page = client.get(f"/commits/{ids['commit_id']}/diff/new")
    assert page.status_code == 200, page.status_code
    html = page.get_data(as_text=True)
    assert "aiDrawerTabThink" in html, "页面里没有标签那一段 DOM"
    assert "aiExportMdLink" in html, "页面里没有「导出 md」那个链接"

    responses = {
        f"/ai-analysis/commit/{ids['commit_id']}/latest":
            client.get(f"/ai-analysis/commit/{ids['commit_id']}/latest").get_json(),
        f"/ai-analysis/runs/{ids['run_id']}/usage":
            client.get(f"/ai-analysis/runs/{ids['run_id']}/usage").get_json(),
        # 历次结论：列表与其中每一条的报告，都从真路由取（键名与后端不会漂移）。
        f"/ai-analysis/commit/{ids['commit_id']}/history":
            client.get(f"/ai-analysis/commit/{ids['commit_id']}/history").get_json(),
    }
    for run_id in ids["history_run_ids"]:
        responses[f"/ai-analysis/runs/{run_id}/report"] = client.get(
            f"/ai-analysis/runs/{run_id}/report"
        ).get_json()
        # 报告下面那块「结构化结论 + 处置」自己会去取这一次的结论行；不预置这条，
        # 请求会落到真网络上（页面不是从真服务起的）→ 面板上挂一条「读不到结构化结论」。
        responses[f"/ai-analysis/runs/{run_id}/anomalies"] = client.get(
            f"/ai-analysis/runs/{run_id}/anomalies"
        ).get_json()
    assert len(responses[f"/ai-analysis/commit/{ids['commit_id']}/history"]["runs"]) >= 2, (
        "历次结论只有一条，「翻历史」的样子就复核不到"
    )
    assert responses[f"/ai-analysis/runs/{ids['run_id']}/anomalies"]["total"] >= 2, (
        "结论行是空的，报告下面那块处置面板就复核不到"
    )
    assert responses[f"/ai-analysis/runs/{ids['run_id']}/usage"]["rounds"], (
        "逐轮明细是空的，「跑完之后看得到本次逐轮过程」就复核不到"
    )
    _check_the_export(client, ids)
    return html, responses


def _check_the_export(client, ids: dict) -> None:
    """**导出这条路的端到端读一遍**：真路由、真响应头、真文件开头几行。

    浏览器里只能验到「链接指向哪一次、显示不显示」；「点下去拿到的是什么」只有真发一次
    请求才看得见 —— 尤其是那个中文文件名（它由 Werkzeug 按 RFC 5987 编码，
    写错的表现是文件名变成一串 `%E2%80%A6`）。
    """
    response = client.get(f"/ai-analysis/runs/{ids['run_id']}/report.md")
    print("\n=== 导出 md（真路由、真响应头）===")
    print(f"  HTTP {response.status_code}  {response.headers.get('Content-Type')}")
    print(f"  Content-Disposition: {response.headers.get('Content-Disposition')}")
    assert response.status_code == 200, response.get_data(as_text=True)[:200]
    assert response.mimetype == "text/markdown"
    assert "attachment" in response.headers.get("Content-Disposition", "")
    text = response.get_data(as_text=True)
    for line in text.splitlines()[:8]:
        print(f"  | {line}")
    print(f"  …（共 {len(text)} 字符）")
    assert REPORT.splitlines()[0] in text, "下载下来的文件里没有报告原文"


def _live_progress(ids: dict) -> dict:
    """跑动中的那一帧：与 `/progress` 发出来的那一份**同源同形**。"""
    from services.ai.run_progress import ProgressSnapshot

    records = _round_records()
    return ProgressSnapshot(
        run_id=ids["run_id"], project_id=ids["project_id"],
        index=1, max_rounds=8, status="requests",
        prompt_tokens=180_000, completion_tokens=9_400,
        cache_read_tokens=120_000, cache_write_tokens=None,
        requests_used=2, requests_remaining=0, items_chars=25_000, elapsed_ms=18_400,
        rounds=tuple(live_round_entry(record) for record in records),
        rounds_seen=len(records), rounds_truncated=False,
        updated_at=time.monotonic(),
    ).to_dict()


# 只量这一件事：两个面板**必须**互为显隐，而 `[hidden]` 的胜出只能靠浏览器算。
_MEASURE_JS = r"""
() => {
    const pick = (id) => {
        const el = document.getElementById(id);
        if (!el) return null;
        return {
            id: id,
            hidden: !!el.hidden,
            display: getComputedStyle(el).display,
            height: Math.round(el.getBoundingClientRect().height)
        };
    };
    const sel = (id) => {
        const el = document.getElementById(id);
        return el ? el.getAttribute('aria-selected') : null;
    };
    const cards = Array.from(document.querySelectorAll('.ai-think-round')).map((el) => ({
        head: (el.querySelector('.ai-think-round-head') || {}).textContent || '',
        lines: el.querySelectorAll('p, li').length,
        height: Math.round(el.getBoundingClientRect().height)
    }));
    const exportLink = document.getElementById('aiExportMdLink');
    // 抽屉宽度与正文框的滚动位置：两条都只有真浏览器算得出来（宽度是 clamp 的
    // 解算结果、滚动位置取决于内容与盒子的真实高度）。
    const box = (id) => {
        const el = document.getElementById(id);
        if (!el) return null;
        return {
            width: Math.round(el.getBoundingClientRect().width),
            scrollTop: Math.round(el.scrollTop),
            scrollHeight: Math.round(el.scrollHeight),
            clientHeight: Math.round(el.clientHeight)
        };
    };
    return {
        drawer: box('aiDrawer'),
        output: box('aiAnalysisOutput'),
        think: pick('aiDrawerPanelThink'),
        report: pick('aiAnalysisOutput'),
        thinkSelected: sel('aiDrawerTabThink'),
        reportSelected: sel('aiDrawerTabReport'),
        note: (document.getElementById('aiThinkNote') || {}).textContent || '',
        meta: (document.getElementById('aiAnalysisMeta') || {}).textContent || '',
        cards: cards,
        // 「导出 md」：链接在不在、指哪儿（`href` 的末段就是运行号）。
        exportLink: exportLink ? {
            hidden: !!exportLink.hidden,
            display: getComputedStyle(exportLink).display,
            href: exportLink.getAttribute('href'),
            text: (exportLink.textContent || '').trim()
        } : null,
        // 正文框里到底画了没有（报告渲染的唯一出口是 render*）
        reportHtmlChars: (document.getElementById('aiAnalysisOutput') || {}).innerHTML.length || 0
    };
}
"""


# **这一条是这一刀最需要真浏览器的读数**：服务端渲染出来的正文框天生带 `is-empty`
# （那一条是 `display:flex`），而模板那段内联 style 排在 style.css 之后。作者样式会盖掉
# 浏览器默认的 `[hidden]{display:none}`，所以必须有一条更高优先级的选择器把它压回去
# （`.ai-drawer-body .ai-analysis-output[hidden]`）。写漏了的表现是**两个面板同时显示**。
_HIDDEN_PROBE_JS = r"""
() => {
    const el = document.getElementById('aiAnalysisOutput');
    const before = el.className;
    el.className = 'ai-analysis-output is-empty';   // 初始 class：带 display:flex
    el.hidden = true;                                // 完整结论标签没被选中
    const hiddenDisplay = getComputedStyle(el).display;
    el.hidden = false;
    const shownDisplay = getComputedStyle(el).display;
    el.className = before;
    return { hiddenDisplay: hiddenDisplay, shownDisplay: shownDisplay };
}
"""


def _check_the_hidden_rule(page) -> None:
    data = page.evaluate(_HIDDEN_PROBE_JS)
    print("\n=== `[hidden]` 压不压得住 `display:flex` ===")
    print(f"  空态 + hidden  → display={data['hiddenDisplay']}")
    print(f"  空态 + 显示    → display={data['shownDisplay']}")
    assert data["hiddenDisplay"] == "none", (
        "带 is-empty 的正文框在 hidden 时仍然显示 —— `.ai-drawer-body .ai-analysis-output"
        "[hidden]` 那条没生效，用户会看到两个面板同时显示"
    )
    assert data["shownDisplay"] == "flex", "空态那一支本来就该是居中的 flex 列"
    # 「导出 md」那个 `<a class="btn">` 是同一个坑的第二处：Bootstrap 的 `.btn` 是
    # `display:inline-block`，而 `.ai-drawer-footer a.btn` 自己那条 `inline-flex` 更具体
    # —— 两条都盖得掉浏览器默认的 `[hidden]{display:none}`。
    link = page.evaluate(_EXPORT_HIDDEN_PROBE_JS)
    print("\n=== 「导出 md」那个链接的 `[hidden]`（同一个坑的第二处）===")
    print(f"  hidden → display={link['hiddenDisplay']}")
    print(f"  显示   → display={link['shownDisplay']}")
    assert link["hiddenDisplay"] == "none", (
        "「导出 md」在 hidden 时仍然显示 —— `.ai-drawer-footer a.btn[hidden]` 那条没生效，"
        "没有可导出的结论时用户会看到一个点下去换来 409 的按钮"
    )
    assert link["shownDisplay"] != "none", "有结论时它得看得见"
    # 同一个坑的第三处：「完整结论」标签上那个未读点。它只有 6×6 像素，写漏了的表现
    # 不是「两个面板同时显示」这种一眼可见的坏，而是**那个点从页面打开起就常亮** ——
    # 看着像个装饰性小圆点，于是「结论更新了」这个提示等于没有。
    dot = page.evaluate(_DOT_HIDDEN_PROBE_JS)
    print("\n=== 未读点的 `[hidden]`（同一个坑的第三处）===")
    print(f"  hidden → display={dot['hiddenDisplay']}")
    print(f"  显示   → display={dot['shownDisplay']}")
    assert dot["hiddenDisplay"] == "none", (
        "未读点在 hidden 时仍然占位 —— `.ai-drawer-tab-dot[hidden]` 那条没生效，"
        "它会一直亮着，用户以为结论每次都刚更新过"
    )
    assert dot["shownDisplay"] == "inline-block", "该显示时它要显示出来"


_DOT_HIDDEN_PROBE_JS = r"""
() => {
    const el = document.getElementById('aiDrawerTabReportDot');
    if (!el) return {hiddenDisplay: 'missing', shownDisplay: 'missing'};
    const before = el.hidden;
    el.hidden = true;
    const hiddenDisplay = getComputedStyle(el).display;
    el.hidden = false;
    const shownDisplay = getComputedStyle(el).display;
    el.hidden = before;
    return {hiddenDisplay: hiddenDisplay, shownDisplay: shownDisplay};
}
"""


_EXPORT_HIDDEN_PROBE_JS = r"""
() => {
    const el = document.getElementById('aiExportMdLink');
    const before = el.hidden;
    el.hidden = true;
    const hiddenDisplay = getComputedStyle(el).display;
    el.hidden = false;
    const shownDisplay = getComputedStyle(el).display;
    el.hidden = before;
    return {hiddenDisplay: hiddenDisplay, shownDisplay: shownDisplay};
}
"""


# 历次结论弹层的读数（真 DOM）。
_HISTORY_PROBE_JS = r"""
() => {
    const modal = document.getElementById('aiReportHistoryModal');
    const body = document.getElementById('aiReportHistoryBody');
    const items = Array.from(document.querySelectorAll('.ai-history-item'));
    const findExport = (node) => {
        const link = node.querySelector('#aiHistoryExportMdLink');
        return link ? link.getAttribute('href') : null;
    };
    const rect = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return {
            top: Math.round(r.top), left: Math.round(r.left),
            right: Math.round(r.right), bottom: Math.round(r.bottom),
            width: Math.round(r.width), height: Math.round(r.height)
        };
    };
    const scroll = (el) => el ? {
        scrollHeight: Math.round(el.scrollHeight),
        clientHeight: Math.round(el.clientHeight),
        scrollTop: Math.round(el.scrollTop)
    } : null;
    const listPane = document.querySelector('.ai-history-list-pane');
    const detailPane = document.querySelector('.ai-history-detail-pane');
    const rowOf = (el) => ({
        when: (el.querySelector('.ai-history-when') || {}).textContent || '',
        badge: (el.querySelector('.badge') || {}).textContent || '',
        risk: (el.querySelector('.ai-history-risk') || {}).textContent || '',
        currentTag: (el.querySelector('.ai-history-current-tag') || {}).textContent || '',
        summary: (el.querySelector('.ai-history-summary') || {}).textContent || '',
        meta: (el.querySelector('.ai-history-row-meta') || {}).textContent || '',
        selected: el.classList.contains('is-selected'),
        tabindex: el.getAttribute('tabindex'),
        ariaSelected: el.getAttribute('aria-selected'),
        height: Math.round(el.getBoundingClientRect().height)
    });
    return {
        visible: !!modal && modal.classList.contains('show'),
        rows: items.length,
        rows_text: items.map((el) => (el.textContent || '').trim().slice(0, 80)),
        rowShapes: items.map(rowOf),
        times: Array.from(document.querySelectorAll('.ai-history-when')).map(
            (node) => node.textContent),
        note: (document.querySelector('.ai-history-meta') || {}).textContent || '',
        mark: (document.querySelector('.ai-history-mark') || {}).textContent || '',
        report: (document.getElementById('aiHistoryReportBody') || {}).textContent || '',
        exportHref: findExport(body),
        currentRows: document.querySelectorAll('.ai-history-item.is-current').length,
        selectedRows: document.querySelectorAll('.ai-history-item.is-selected').length,
        // 两栏的几何：**只有真浏览器算得出来**（网格解算 + 内容驱动的高度）
        listPane: rect(listPane),
        detailPane: rect(detailPane),
        listScroll: scroll(listPane),
        detailScroll: scroll(detailPane),
        reportRect: rect(document.getElementById('aiHistoryReportBody')),
        viewport: {width: window.innerWidth, height: window.innerHeight},
        // 报告下面那块「结构化结论 + 处置」：清单、每条三个动作、批量按钮的可否。
        // 逐条读（不是 `querySelector` 取第一个）—— 「哪一条的处置人显示出来了」
        // 只有逐条看才知道：第一个匹配到 `.ai-anomaly-item-by` 的节点未必是第一条。
        panelItems: Array.from(
            document.querySelectorAll('#aiAnomalyPanel .ai-anomaly-item')
        ).map((item) => ({
            title: (item.querySelector('.ai-anomaly-item-title') || {}).textContent || '',
            state: (item.querySelector('.ai-anomaly-item-state') || {}).textContent || '',
            by: (item.querySelector('.ai-anomaly-item-by') || {}).textContent || '',
            when: (item.querySelector('.ai-anomaly-item-at') || {}).textContent || '',
            note: (item.querySelector('.ai-anomaly-item-note') || {}).textContent || '',
            severity: (item.querySelector('.ai-anomaly-item-severity') || {}).textContent || '',
            evidence: item.querySelectorAll('.ai-anomaly-evidence li').length,
            actions: Array.from(item.querySelectorAll('.ai-anomaly-act')).map(
                (b) => b.textContent),
        })),
        panelCounts: (document.querySelector('#aiAnomalyPanel .ai-anomaly-counts')
                      || {}).textContent || '',
        panelPicked: (document.querySelector('#aiAnomalyPanel .ai-anomaly-picked')
                      || {}).textContent || '',
        panelBatchDisabled: Array.from(
            document.querySelectorAll('#aiAnomalyPanel .ai-anomaly-batch')
        ).map((b) => b.disabled),
        panelError: (document.querySelector('#aiAnomalyPanel .ai-anomaly-error')
                     || {}).textContent || '',
        panelMeta: (document.querySelector('#aiAnomalyPanel .ai-anomaly-meta')
                    || {}).textContent || ''
    };
}
"""

# 键盘那一档：焦点**真的**落在刚选中的那一条上，并且那一圈焦点环真的画出来了。
# `:focus-visible` 的胜出只有浏览器算得出来（假 DOM 里根本没有这个概念）。
_FOCUS_PROBE_JS = r"""
() => {
    const active = document.activeElement;
    if (!active) return {tag: null};
    const style = getComputedStyle(active);
    return {
        className: active.className,
        isSelected: active.classList.contains('is-selected'),
        role: active.getAttribute('role'),
        outlineWidth: style.outlineStyle === 'none' ? '0px' : style.outlineWidth,
        outlineColor: style.outlineColor
    };
}
"""


def _check_the_history_layout(page, history: dict) -> None:
    """2026-09-25 改版的三条读数 —— **全是只有真浏览器算得出来的**。

    用户报的原话：「选择某个结论后的跳转文本显示也不方便查看，**没有跳到正文**」。
    改版把「一张宽表 + 报告接在表下面」换成「左列 + 右栏」。这三条各管一件事：

    1. **两栏并排**（左栏在左、右栏在右、上沿齐平）—— 网格解算的结果；
    2. **报告在视野里**（选中之后 `#aiHistoryReportBody` 的顶边落在视口内）——
       这一条正是用户那句抱怨的判据；改版前报告顶边在 1000px 开外；
    3. **两栏各自滚**（各自的 `scrollHeight > clientHeight`）—— 顺带证明第 2 条不是
       「内容太短所以刚好看得见」那种假绿。
    """
    list_pane, detail_pane = history["listPane"], history["detailPane"]
    viewport = history["viewport"]
    print("\n=== 历次结论：两栏几何（真浏览器算的）===")
    print(f"  视口 {viewport['width']}×{viewport['height']}")
    print(f"  左栏 {list_pane}  滚动={history['listScroll']}")
    print(f"  右栏 {detail_pane}  滚动={history['detailScroll']}")
    print(f"  报告正文 rect={history['reportRect']}")
    print(f"  选中项={history['selectedRows']}  当前项={history['currentRows']}")
    for row in history["rowShapes"][:3]:
        print(f"  | {row['when']} [{row['badge']}] {row['risk']} {row['currentTag']}"
              f" / {row['summary'][:40]} / {row['meta']} / 高 {row['height']}px")

    assert list_pane and detail_pane, "两栏有一个没画出来"
    assert list_pane["right"] <= detail_pane["left"] + 1, (
        f"两栏不是并排的（左栏右沿 {list_pane['right']} > 右栏左沿 {detail_pane['left']}）"
        " —— 报告又回到了列表下面"
    )
    assert abs(list_pane["top"] - detail_pane["top"]) <= 1, "两栏上沿没对齐"
    assert list_pane["width"] >= 300, f"左栏只有 {list_pane['width']}px，一条摘要读不了"

    # 两栏都得真的撑出滚动条，否则下面那条「报告在视野里」是「内容太短」的假绿
    assert history["listScroll"]["scrollHeight"] > history["listScroll"]["clientHeight"] + 20, (
        f"左栏没撑出滚动条（{history['listScroll']}）—— 「两栏各自滚」这条断言会退化成恒真；"
        "先把造数里的历史条数加上去"
    )
    assert (history["detailScroll"]["scrollHeight"]
            > history["detailScroll"]["clientHeight"] + 20), (
        f"右栏没撑出滚动条（{history['detailScroll']}）—— 报告太短就验不出「报告自己在滚」"
    )

    # **用户那句抱怨的判据**：选中之后报告正文在视野里，不用滚。
    report = history["reportRect"]
    assert report["top"] >= 0 and report["top"] < viewport["height"], (
        f"选中之后报告正文的顶边在 {report['top']}px —— 不在视野里"
        f"（视口高 {viewport['height']}），用户看到的就是「点了没反应」"
    )
    assert report["bottom"] > report["top"], "报告正文是零高度"
    # 当前那一份的标记：**文字标签**，不是又一条颜色
    current = [row for row in history["rowShapes"] if row["currentTag"]]
    assert len(current) == 1 and current[0]["currentTag"] == "当前显示", (
        f"「当前显示」那一条没有文字标签：{history['rowShapes']}"
    )
    assert current[0]["tabindex"] == "0", "当前那条 Tab 进不来"


def _check_the_history_keyboard(page, history: dict, out_prefix: str) -> None:
    """方向键：焦点走到哪一条，看的就是哪一条（右栏跟着换）。

    这一档只有真浏览器验得了：`:focus-visible` 的胜出、`document.activeElement`
    跟不跟得上，假 DOM 里都没有这些概念。

    **先按 Tab 走进列表**：方向键的处理函数挂在清单容器上，而弹层刚打开时焦点在关闭
    按钮上 —— 键盘用户得先 Tab 进来（roving tabindex：整张清单只有选中那条能 Tab 到）。
    探针把这一步也真按出来，顺带证明**这条路走得通**（Tab 走不到列表项的话，方向键
    这套快捷键对键盘用户等于不存在）。
    """
    reached = False
    for _ in range(6):
        page.keyboard.press("Tab")
        probe = page.evaluate(_FOCUS_PROBE_JS)
        if probe.get("role") == "option":
            reached = True
            break
    assert reached, "按 Tab 走不到列表项 —— 方向键那套快捷键键盘用户够不着"
    print("\n=== 历次结论：键盘 ===")
    print(f"  Tab 之后焦点: {page.evaluate(_FOCUS_PROBE_JS)}")

    before = history["rowShapes"].index(
        next(row for row in history["rowShapes"] if row["selected"])
    )
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(500)
    after = page.evaluate(_HISTORY_PROBE_JS)
    moved = after["rowShapes"].index(
        next(row for row in after["rowShapes"] if row["selected"])
    )
    print(f"  选中从第 {before + 1} 条走到第 {moved + 1} 条")
    focus = page.evaluate(_FOCUS_PROBE_JS)
    print(f"  document.activeElement: {focus}")
    print(f"  标记: {after['mark']}")
    assert moved == before + 1, "方向键没有换选中"
    assert focus["isSelected"], "焦点没落到刚选中的那一条上"
    assert focus["role"] == "option", f"焦点那个节点的角色不对：{focus['role']}"
    assert focus["outlineWidth"] not in ("0px", ""), (
        f"焦点环没画出来（outlineWidth={focus['outlineWidth']}）—— 键盘用户看不出焦点在哪"
    )
    assert "的结论（历史）" in (after["mark"] or "") or "当前显示的结论" in (after["mark"] or ""), (
        after["mark"]
    )
    # 右栏确实跟着换了：报告正文在视野里（同一条判据，键盘路径也要成立）
    report = after["reportRect"]
    assert report["top"] < after["viewport"]["height"], "键盘换选中之后报告跑到视野外了"
    page.locator("#aiReportHistoryModal .modal-content").screenshot(
        path=str(ROOT / ".pytest_tmp" / f"{out_prefix}_history_keyboard.png"))
    page.keyboard.press("ArrowUp")
    page.wait_for_timeout(400)
    back = page.evaluate(_HISTORY_PROBE_JS)
    assert back["rowShapes"].index(
        next(row for row in back["rowShapes"] if row["selected"])
    ) == before, "ArrowUp 没回到原来那一条"


def _check_the_history_narrow(page, history: dict, out_prefix: str) -> None:
    """窄屏（900px）：两栏退回上下两段，**报告的顶边仍要在第一屏里**。

    退回上下之后，如果列表不封顶，报告又会被推到折叠线以下 —— 那就是改版前的毛病
    换个宽度又犯一次。所以这一条断的是「详情栏的顶边落在视口内」（`max-height: 40vh`
    那条规则在起作用）。
    """
    page.set_viewport_size({"width": 900, "height": 900})
    page.wait_for_timeout(400)
    narrow = page.evaluate(_HISTORY_PROBE_JS)
    print("\n=== 历次结论：窄屏 900px（上下两段）===")
    print(f"  左栏 {narrow['listPane']}")
    print(f"  右栏 {narrow['detailPane']}  报告 rect={narrow['reportRect']}")
    assert narrow["listPane"]["bottom"] <= narrow["detailPane"]["top"] + 1, (
        "窄屏没有退回上下两段"
    )
    assert narrow["listPane"]["height"] <= 900 * 0.4 + 2, (
        f"窄屏的左栏没有封顶（高 {narrow['listPane']['height']}px），报告会被推到折叠线以下"
    )
    assert narrow["reportRect"]["top"] < 900, (
        f"窄屏下报告正文的顶边在 {narrow['reportRect']['top']}px（视口 900）—— 看不见"
    )
    page.locator("#aiReportHistoryModal .modal-content").screenshot(
        path=str(ROOT / ".pytest_tmp" / f"{out_prefix}_history_narrow.png"))
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(300)


def _report(label: str, data: dict) -> None:
    print(f"\n=== {label} ===")
    for key in ("think", "report"):
        item = data[key]
        print(f"  {key:6} hidden={item['hidden']!s:5} display={item['display']:8} "
              f"height={item['height']}")
    assert bool(data["think"]["hidden"]) != bool(data["report"]["hidden"]), (
        "两个面板的显隐相同 —— `[hidden]` 没压住 `display:flex`（或者反过来）"
    )
    print(f"  aria-selected: 思考过程={data['thinkSelected']} 完整结论={data['reportSelected']}")
    print(f"  说明: {data['note']}")
    print(f"  状态行: {data['meta']}")
    print(f"  正文框 HTML: {data['reportHtmlChars']} 字符")
    link = data.get("exportLink")
    if link:
        print(f"  导出 md: hidden={link['hidden']} display={link['display']} "
              f"href={link['href']} 文字={link['text']!r}")
    for card in data["cards"]:
        print(f"  轮次卡: {card['head'][:60]} ({card['lines']} 行, {card['height']}px)")


def _main(out_prefix: str) -> int:
    from playwright.sync_api import sync_playwright

    ids = _seed()
    html, responses = _capture(ids)
    live = _live_progress(ids)

    out_dir = ROOT / ".pytest_tmp"
    html_path = out_dir / f"{out_prefix}.html"
    html = html.replace("<head>", f'<head><base href="{(ROOT / "x").as_uri()}">', 1)
    html_path.write_text(html, encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        def _path_of(url: str) -> str:
            path = url.split("?")[0].split("#")[0]
            match = re.match(r"^[a-z]+://[^/]*(/.*)$", path, re.I)
            path = match.group(1) if match else path
            return re.sub(r"^/[A-Za-z]:", "", path).rstrip("/")

        def _serve(route):
            url = _path_of(route.request.url)
            for prefix, payload in responses.items():
                if url == prefix:
                    return route.fulfill(status=200, content_type="application/json",
                                         body=json.dumps(payload, ensure_ascii=False))
            if "/static/" in url:
                target = ROOT / "static" / url.split("/static/", 1)[1]
                if target.exists():
                    content_type = {
                        ".css": "text/css", ".js": "application/javascript",
                        ".svg": "image/svg+xml",
                    }.get(target.suffix, "application/octet-stream")
                    return route.fulfill(status=200, content_type=content_type,
                                         body=target.read_bytes())
            return route.continue_()

        page.route("**/*", _serve)
        page.goto(html_path.as_uri(), wait_until="networkidle")
        page.wait_for_timeout(500)

        # 打开抽屉：点的是页面上那个真按钮（它会跑 loadLatestResult）。
        page.click("#aiDrawerOpenBtn")
        page.wait_for_timeout(900)
        shooter = lambda name: page.locator("#aiDrawer").screenshot(  # noqa: E731
            path=str(out_dir / f"{out_prefix}_{name}.png"))
        settled = page.evaluate(_MEASURE_JS)
        _report("跑完之后：默认落在完整结论", settled)
        # 抽屉宽度：三档在 720 那一版上整体 ×1.4（用户要求「默认宽度增大 40%」）。
        # 视口 1440 → 87vw 超过上限，取的就是 1008。
        print(f"  抽屉宽度: {settled['drawer']['width']}px"
              f"  正文框滚动: top={settled['output']['scrollTop']}"
              f" 内容高={settled['output']['scrollHeight']}"
              f"/可见高={settled['output']['clientHeight']}")
        assert 1000 <= settled["drawer"]["width"] <= 1010, (
            f"抽屉宽度是 {settled['drawer']['width']}px，不是加宽后的 1008px"
            "（加宽前是 720px）"
        )
        # **先证明这一档是活的**：撑不出滚动条时 `scrollTop` 恒为 0，下面那条断言就
        # 变成同义反复 —— 本仓库在「假绿的守卫」上踩过不止一次。
        assert settled["output"]["scrollHeight"] > settled["output"]["clientHeight"] + 20, (
            "正文框没撑出滚动条，「打开抽屉停在顶部」这条断言会退化成恒真；"
            "先把 REPORT 加长再验"
        )
        assert settled["output"]["scrollTop"] == 0, (
            f"打开抽屉时正文停在 {settled['output']['scrollTop']}px —— "
            "用户一进来看到的是报告最后一屏（反馈「显示的是最底部的结论文本」）"
        )
        # 反方向：**流式 chunk 推进来时必须跟到末尾**（跑动中用户看着的就是那一屏）。
        # 少了这一条，「不粘末尾」写成死值 `scrollTop = 0` 也照样骗过上面那条断言，
        # 而跑动中新推来的输出会停在第一屏不动。探完把正文还原回去，后面的截图不受影响。
        page.evaluate("() => { window.__probeBuffer = aiBuffer; }")
        page.evaluate("() => appendAiLine('\\n流式追加的一行')")
        page.wait_for_timeout(400)
        tail = page.evaluate(_MEASURE_JS)
        print(f"  逐行追加之后: top={tail['output']['scrollTop']}"
              f"/最大可滚={tail['output']['scrollHeight'] - tail['output']['clientHeight']}")
        assert tail["output"]["scrollTop"] > 0, (
            "逐行追加之后仍停在顶部 —— 跑动中新推来的输出看不见"
        )
        page.evaluate(
            "() => { setAiReport(window.__probeBuffer); delete window.__probeBuffer; }"
        )
        page.wait_for_timeout(300)
        # 有结论 → 「导出 md」可见，且指向**这一次**的运行。
        assert settled["exportLink"] and settled["exportLink"]["hidden"] is False, (
            "有结论时「导出 md」仍然藏着 —— 用户没法把结论交出去"
        )
        assert settled["exportLink"]["href"].endswith(
            f"/ai-analysis/runs/{ids['run_id']}/report.md"
        ), settled["exportLink"]["href"]
        shooter("settled_report")
        # footer 单独一张：三个动作的排布（刷新 / 重新分析 / 导出 md）。
        page.locator("#aiDrawer .ai-drawer-footer").screenshot(
            path=str(out_dir / f"{out_prefix}_footer.png"))

        # 用户去看这一次的逐轮过程：切过去时**懒加载**落库的明细（`/usage`）。
        page.click("#aiDrawerTabThink")
        page.wait_for_timeout(700)
        shooter("settled_think")
        _report("跑完之后：思考过程（落库的逐轮）", page.evaluate(_MEASURE_JS))

        # 跑动中的那一帧：用真模块喂一份真形状的进度载荷（见 `_live_progress`）。
        page.evaluate(
            """(payload) => {
                AiDrawerTabs.reset();
                AiDrawerTabs.markRunning();
                AiThinkLog.watch(payload.progress.run_id);
                AiThinkLog.applyProgress(payload.progress);
                // 与 startAnalysis 里那一次一致：屏幕上的结论刚被换掉，导出也就没有对象了。
                AiReportExport.track({ runId: null });
                const meta = document.getElementById('aiAnalysisMeta');
                if (meta) meta.textContent = AiStreamStatus.progressText(payload.progress, 'running');
            }""",
            {"progress": live},
        )
        page.wait_for_timeout(400)
        running = page.evaluate(_MEASURE_JS)
        _report("跑动中：实时思考过程（一帧两轮）", running)
        assert running["exportLink"]["hidden"] is True, (
            "跑动中「导出 md」还看得见 —— 那时候屏幕上没有结论可导"
        )
        shooter("running_think")

        # 历次结论：**跑动中也照样能点**，这里在跑动那一帧之后打开它。
        page.click("#aiHistoryBtn")
        page.wait_for_timeout(900)
        history = page.evaluate(_HISTORY_PROBE_JS)
        print("\n=== 历次结论（真弹层、真列表、真报告）===")
        print(f"  弹层可见: {history['visible']}  行数: {history['rows']}")
        print(f"  说明: {history['note']}")
        for row in history["rows_text"]:
            print(f"  | {row}")
        print(f"  标记: {history['mark']}")
        print(f"  弹层里的导出链接: {history['exportHref']}")
        print(f"  列表里的时间: {history['times']}")
        assert history["visible"], "历次结论弹层没打开（按钮点了没反应）"
        assert history["rows"] >= 2, "列表少于两条，「翻历史」就复核不到"
        assert "当前显示的结论" in (history["mark"] or ""), "没有标出「你看的是哪一份」"
        assert (history["exportHref"] or "").endswith(
            f"/ai-analysis/runs/{ids['run_id']}/report.md"
        ), history["exportHref"]
        # 报告下面那块：清单得有内容、每条三个动作、没勾选时批量按钮不可用。
        print("\n=== 结构化结论 + 处置（报告下面那块）===")
        print(f"  计数行: {history['panelCounts']}  勾选: {history['panelPicked']}")
        for row in history["panelItems"]:
            print(f"  | [{row['severity']}] {row['title']} —— {row['state']}"
                  f" / {row['by']} / {row['when']} / {row['note']}"
                  f" / 证据 {row['evidence']} 条 / 动作 {row['actions']}")
        print(f"  批量按钮 disabled={history['panelBatchDisabled']}"
              f"  错误条: {history['panelError']!r}")
        assert history["panelError"] == "", (
            f"处置面板挂着一条错误：{history['panelError']}"
        )
        assert len(history["panelItems"]) >= 2, "结论清单一条都没画出来"
        for row in history["panelItems"]:
            assert row["actions"] == ["已确认", "已忽略", "撤销"], row["actions"]
            assert row["evidence"] >= 1, f"这条结论一条证据都没显示：{row['title']}"
            # 严重度印的是**服务端算好的中文名**（真路由 → 真字段 → 真 DOM 走一遍）。
            assert row["severity"] in ("严重", "高"), (
                f"严重度显示的不是中文名：{row['severity']!r}"
            )
        disposed = [row for row in history["panelItems"] if row["state"] == "已忽略"]
        assert len(disposed) == 1, history["panelItems"]
        assert "误报，已核对" in disposed[0]["note"], disposed[0]
        assert "zhangsan" in disposed[0]["by"], disposed[0]
        # 时间用服务端那个北京时间的显示串，**不是**库里那个裸 ISO（带 T、带微秒）。
        assert disposed[0]["when"].startswith("时间：2026-"), disposed[0]["when"]
        assert "T" not in disposed[0]["when"], (
            f"时间印的是裸 ISO（UTC），不是服务端算好的北京时间：{disposed[0]['when']!r}"
        )
        # 还没处置的那一条不该凭空多出处置人/备注（假的「已经有人看过了」）。
        fresh = [row for row in history["panelItems"] if row["state"] == "待确认"]
        assert fresh and not fresh[0]["by"] and not fresh[0]["note"], fresh
        assert all(history["panelBatchDisabled"]), (
            "一条都没勾时批量按钮却是可用的 —— 点下去只会换来一句 400"
        )
        page.locator("#aiReportHistoryModal .modal-content").screenshot(
            path=str(out_dir / f"{out_prefix}_history.png"))
        # 2026-09-25 改版的三条读数（两栏几何 / 报告在不在视野里 / 两栏各自滚）
        _check_the_history_layout(page, history)
        _check_the_history_keyboard(page, history, out_prefix)
        _check_the_history_narrow(page, history, out_prefix)
        # 面板在报告**下面**：滚到底单独拍一张，看清清单与处置动作长什么样。
        page.evaluate(
            "() => { const el = document.getElementById('aiAnomalyPanel');"
            " el.scrollIntoView({block: 'end'}); }"
        )
        page.wait_for_timeout(300)
        page.locator("#aiAnomalyPanel").screenshot(
            path=str(out_dir / f"{out_prefix}_disposition.png"))
        # 翻到上一次（失败的那次）：标记变「历史」，失败那条没有正文也没有导出。
        # **点整行**（不是行里某个按钮）—— 整行可点是这一版的行为。
        page.evaluate(
            "() => { const rows = document.querySelectorAll('.ai-history-item');"
            " rows[rows.length - 1].click(); }"
        )
        page.wait_for_timeout(700)
        older = page.evaluate(_HISTORY_PROBE_JS)
        print("\n=== 翻到上一次（失败的那一次）===")
        print(f"  标记: {older['mark']}")
        print(f"  报告区: {(older['report'] or '')[:60]}")
        print(f"  弹层里的导出链接: {older['exportHref']}")
        assert "（历史）" in (older["mark"] or ""), older["mark"]
        assert older["exportHref"] is None, "失败的那一次没有正文，不该给一个导出链接"
        # 失败的那一次一条结论行都没有：面板要说实话，不是留一片空白。
        print(f"  处置面板: 条数={len(older['panelItems'])}  说明={older['panelMeta']!r}")
        assert not older["panelItems"], "失败的那一次不该有结论行"
        assert "没有落库的结构化结论" in (older["panelMeta"] or ""), (
            f"没有结论行时面板什么都没说：{older['panelMeta']!r}"
        )
        page.locator("#aiReportHistoryModal .modal-content").screenshot(
            path=str(out_dir / f"{out_prefix}_history_older.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # 只有两个标签，方向键也要能换（键盘用户不看标签条也得能过去）。
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(250)
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(250)
        _report("键盘换标签之后", page.evaluate(_MEASURE_JS))

        _check_the_hidden_rule(page)

        print(f"\n图在 {out_dir}（{out_prefix}_*.png）")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1] if len(sys.argv) > 1 else "ai_drawer"))
