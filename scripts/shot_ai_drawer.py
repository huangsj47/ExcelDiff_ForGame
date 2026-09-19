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
from datetime import datetime, timezone
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
from models.ai_analysis import AiProjectAnalysisConfig  # noqa: E402
from services.ai.trace_evidence import encode_evidence, live_round_entry  # noqa: E402
from services.ai_analysis_service import _current_provenance  # noqa: E402

PASSWORD = "pw-123456"
STAMP = uuid.uuid4().hex[:6]
MODEL = "claude-sonnet-5"

REPORT = """# 变更理解

本次把「无敌帧」的判定从 `BattleMgr.update` 挪到了 `BattleMgr.onHit`，判定顺序因此
发生了变化：先算无敌、再算扣血。

# 影响面分析

- 战斗系统：命中结算链路 **必测**
- 技能系统：`PsSkSpc` 的特殊被动类型 7/27/28 已删除，同批仍有代码在改

# 风险评估

| 风险等级 | 高 |
|---|---|
"""


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
        provenance = _current_provenance(project.id)
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
        db.session.commit()
        return {"_admin": admin_name, "commit_id": commit.id,
                "project_id": project.id, "run_id": run.id}


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

    responses = {
        f"/ai-analysis/commit/{ids['commit_id']}/latest":
            client.get(f"/ai-analysis/commit/{ids['commit_id']}/latest").get_json(),
        f"/ai-analysis/runs/{ids['run_id']}/usage":
            client.get(f"/ai-analysis/runs/{ids['run_id']}/usage").get_json(),
    }
    assert responses[f"/ai-analysis/runs/{ids['run_id']}/usage"]["rounds"], (
        "逐轮明细是空的，「跑完之后看得到本次逐轮过程」就复核不到"
    )
    return html, responses


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
    return {
        think: pick('aiDrawerPanelThink'),
        report: pick('aiAnalysisOutput'),
        thinkSelected: sel('aiDrawerTabThink'),
        reportSelected: sel('aiDrawerTabReport'),
        note: (document.getElementById('aiThinkNote') || {}).textContent || '',
        meta: (document.getElementById('aiAnalysisMeta') || {}).textContent || '',
        cards: cards,
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
        shooter("settled_report")
        _report("跑完之后：默认落在完整结论", page.evaluate(_MEASURE_JS))

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
                const meta = document.getElementById('aiAnalysisMeta');
                if (meta) meta.textContent = AiStreamStatus.progressText(payload.progress, 'running');
            }""",
            {"progress": live},
        )
        page.wait_for_timeout(400)
        shooter("running_think")
        _report("跑动中：实时思考过程（一帧两轮）", page.evaluate(_MEASURE_JS))

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
