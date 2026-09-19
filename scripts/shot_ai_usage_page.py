#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `/ai-analysis/usage` 这一页在本机真渲染出来截图（无头 Chrome，真 CSS/JS + 真接口响应）。

**为什么要有这个脚本**：这一页的表现问题（密度、对齐、层级、窄屏、空态）全是**画出来
才看得见**的，而线上那台要登录、还要 `DIFF_AUDIT_TOKEN`（token 不进仓库、也不该由脚本
代管）。所以改成「在临时库里造一份有代表性的数据 → 用真路由取真响应 → 浏览器里渲染」：

  * 页面 HTML 走 `GET /ai-analysis/usage`（真登录态、真模板）；
  * 四个接口的响应**不是手写的**，而是从同一个测试客户端真取回来的
    （`/usage/overview`、`/platform-budget`、`/projects/<id>/config`、`/projects/<id>/budget`），
    所以键名与取值形态不会与后端漂移；
  * 静态资源按仓库里的真实文件回给浏览器，CDN 上的 Bootstrap / FontAwesome 走网络。

用法：
    python scripts/shot_ai_usage_page.py [输出前缀]

产物写在 `.pytest_tmp/`（不进仓库）。数据全是造的，与线上无关。
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 临时库：文件名里带 `pytest` 才会被 utils/db_safety 认成测试库（见那个模块的判据）。
SCRATCH_DB = ROOT / ".pytest_tmp" / "db" / "pytest_aiu_shot.db"
SCRATCH_DB.parent.mkdir(parents=True, exist_ok=True)
# 每次从空库开始：这个库是复用的，不清的话项目 id 会一直涨，
# 「下钻项目 id=1」这种预期就对不上（页面里选中的项目会变）。
for suffix in ("", "-wal", "-shm"):
    Path(str(SCRATCH_DB) + suffix).unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{SCRATCH_DB}"
os.environ.setdefault("RUN_SCHEDULER", "0")

from app import app as flask_app  # noqa: E402
from app import create_tables  # noqa: E402
from auth.services import register_user  # noqa: E402
from models import AiAnalysisRun, Project, db  # noqa: E402
from models.ai_analysis import AiAnalysisTrace, AiProjectAnalysisConfig  # noqa: E402
from services.ai.platform_budget import set_platform_budget  # noqa: E402
from services.ai.trace_evidence import encode_evidence  # noqa: E402
from services.ai.usage import encode_tools  # noqa: E402
from services.ai.usage_statistics import set_usage_baseline, usage_baseline  # noqa: E402

PASSWORD = "pw-123456"
STAMP = uuid.uuid4().hex[:6]

# 造数据用的项目：（名字，近 30 天的运行数，最后一条距今多少天，超预算？）
PROJECTS = (
    ("新手引导配表", 14, 0, True),
    ("战斗数值表", 9, 1, False),
    ("活动配置", 4, 6, False),
    ("UI 资源表", 0, None, False),          # 空态：一条运行都没有
)

MODEL = "claude-sonnet-5"

# 按工具类型的取数账（单次运行弹层里那一块要用真数据渲染，含「失败」那一列）。
TOOL_STATS = {
    "file_diff": {"calls": 6, "executions": 6, "cache_hits": 1, "failed": 2,
                  "refused_by_budget": 1, "truncated": 0,
                  "source_chars": 41_200, "produced_chars": 38_600},
    "file_content": {"calls": 3, "executions": 3, "cache_hits": 0, "failed": 1,
                     "refused_by_budget": 0, "truncated": 1,
                     "source_chars": 33_000, "produced_chars": 11_000},
}

PRICE_TABLE = json.dumps({
    "version": "2026-09-01",
    "currency": "CNY",
    "models": {
        MODEL: {"input": "21.00", "output": "105.00", "cache_read": "2.10",
                "cache_write": "26.25", "currency": "CNY"},
        "claude-haiku-4-5-20251001": {"input": "7.00", "output": "35.00",
                                      "cache_read": "0.70", "currency": "CNY"},
    },
}, ensure_ascii=False)


def _seed() -> dict:
    """造一份有代表性的数据，返回 `{项目名: project_id}`。"""
    ids = {}
    with flask_app.app_context():
        create_tables()
        admin_name = f"shotadm{STAMP}"
        admin, error = register_user(admin_name, PASSWORD, role="platform_admin")
        assert admin is not None, error
        ids["_admin"] = admin_name

        over_since = datetime.now(timezone.utc) - timedelta(days=20)
        for name, runs, last_days, over_budget in PROJECTS:
            project = Project(code=f"SHOT{STAMP}{len(ids)}", name=f"{name}")
            db.session.add(project)
            db.session.flush()
            ids[name] = project.id

            config = AiProjectAnalysisConfig(
                project_id=project.id,
                api_model=MODEL,
                model_price_table=PRICE_TABLE,
                budget_period="monthly",
                budget_token_limit=(2_000_000 if over_budget else 50_000_000),
                budget_cost_limit=("20.00" if over_budget else "500.00"),
                updated_by=admin_name,
            )
            db.session.add(config)

            for index in range(runs):
                created = over_since + timedelta(days=index, hours=index % 7)
                failed = index % 5 == 4
                # 每 3 条里留一条「缓存未上报」（cache_read = None）——
                # 那与「命中 0」是两句话，界面上必须能分辨（本页最要紧的一条口径）。
                cache_read = None if index % 3 == 2 else 12_000 + index * 3_100
                run = AiAnalysisRun(
                    project_id=project.id,
                    target_type="commit",
                    target_id=1000 + index,
                    target_key=f"{STAMP}-{project.id}-{index}",
                    status="failed" if failed else "succeeded",
                    scope="full",
                    trigger_source="scheduled" if index % 4 == 0 else "manual",
                    model=MODEL,
                    rounds_used=1 + index % 3,
                    tokens_input=180_000 + index * 21_500,
                    tokens_output=9_400 + index * 640,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=None if cache_read is None else 8_000,
                    duration_ms=42_000 + index * 3_100,
                    context_chars=180_000 + index * 4_000,
                    anomalies_found=index % 4,
                    dropped_count=index % 3,
                    error_message="上游 400：上下文超长" if failed else None,
                    created_at=created,
                    started_at=created,
                    finished_at=created + timedelta(seconds=45),
                    tool_stats_json=encode_tools(TOOL_STATS),
                )
                db.session.add(run)
        db.session.commit()

        # 逐轮明细（含「取不到」的那一条）挂在**逐次运行列表的第一行**那一笔上：
        # 弹层是从那一行点开的，挂在别的运行上等于截不到（第一版就是按「第一条创建的
        # 运行」挂的，而列表按时间倒序，于是点出来的那一笔根本没有逐轮记录）。
        newest = AiAnalysisRun.query.order_by(AiAnalysisRun.created_at.desc()).first()
        assert newest is not None, "一笔运行都没造出来"
        # 那一笔同时也是「子代理模式跑出来的」：分片表、逐轮表的分片列都靠它才有东西可看
        # （`_round_records` 的轮次带着 agent/agent_round）。**分片里故意留一个「未运行」**
        # —— 那一行的红字是这一块最要紧的读数，没有它截图就看不出效果。
        newest.subagent_mode = "subagents"
        newest.subagent_count = 3
        for record in _round_records():
            db.session.add(AiAnalysisTrace(
                run_id=newest.id, round_index=record.index, outcome=record.status,
                parsed_ok=record.status != "unparsable",
                # 分片那两列（NULL = 主代理自己那几轮，也是所有老行的情形）。
                agent=record.agent or None, agent_round=record.agent_round or None,
                tokens_input=record.prompt_tokens,
                tokens_output=record.completion_tokens,
                request_chars=record.prompt_chars,
                context_chars=record.context_chars,
                duration_ms=record.duration_ms,
                **encode_evidence(record),
            ))
        newest.response_payload = json.dumps(
            {
                "subagents": [
                    {"label": "S1", "role": "subagent", "index": 1,
                     "dimensions": ["config_id", "config_value", "config_linkage"],
                     "status": "succeeded", "rounds": 2, "requests": 5,
                     "tokens_input": 385_000, "tokens_output": 22_200,
                     "cache_read_tokens": 240_000, "cache_write_tokens": 40_000,
                     "anomalies": 3, "report_chars": 1_820, "skipped_reason": "", "error": ""},
                    {"label": "S2", "role": "subagent", "index": 2,
                     "dimensions": ["config_data", "value_sanity"],
                     "status": "succeeded", "rounds": 1, "requests": 4,
                     "tokens_input": 190_000, "tokens_output": 11_400,
                     "cache_read_tokens": 178_000, "cache_write_tokens": 0,
                     "anomalies": 2, "report_chars": 940, "skipped_reason": "", "error": ""},
                    {"label": "S3", "role": "subagent", "index": 3,
                     "dimensions": ["module_coupling", "code_logic", "version_branch", "process"],
                     "status": "skipped", "rounds": 0, "requests": 0,
                     "tokens_input": 0, "tokens_output": 0,
                     "cache_read_tokens": None, "cache_write_tokens": None,
                     "anomalies": 0, "report_chars": 0,
                     "skipped_reason": "剩余预算不足（本月已用 2,880,000 / 3,000,000 tokens）",
                     "error": ""},
                    {"label": "汇总", "role": "synthesis", "index": 4,
                     "dimensions": [], "status": "succeeded", "rounds": 1, "requests": 2,
                     "tokens_input": 210_000, "tokens_output": 14_600,
                     "cache_read_tokens": 196_000, "cache_write_tokens": 0,
                     "anomalies": 4, "report_chars": 3_400, "skipped_reason": "", "error": ""},
                    # 对账轮（`subagent_verify`）：**没有负责的维度** —— 面板上那一格
                    # 要显示成「（对账轮：找反证）」而不是空着的「—」，靠这一行复核。
                    {"label": "V1", "role": "verify", "index": 5,
                     "dimensions": [], "status": "succeeded", "rounds": 1, "requests": 3,
                     "tokens_input": 168_000, "tokens_output": 6_200,
                     "cache_read_tokens": 160_000, "cache_write_tokens": 0,
                     "anomalies": 1, "report_chars": 780, "skipped_reason": "", "error": ""},
                ],
                "subagent_skipped": ["S3：剩余预算不足"],
            },
            ensure_ascii=False,
        )
        db.session.commit()

        # 平台档：故意配成一个**已经超了**的小额度，好让顶部横幅与卡片内提示都出现
        # （「同一次超预算会被朗读两遍」那条可访问性改动要靠它复核）。
        ok, message, _errors = set_platform_budget(
            {"budget_period": "monthly", "budget_token_limit": 3_000_000,
             "budget_cost_limit": "50.00"},
            updated_by=admin_name,
        )
        assert ok, message

        # 统计起点：设在 20 天前，于是每个项目都有一部分运行落在起点之前 ——
        # 「起点之前的运行完全不显示」与「这一屏排除了 N 条」这两句话才都看得见。
        # 顺带复核那条红线：平台档「已用」是**超了**的状态，它不该因为设了起点而变小。
        baseline = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=20)
        ok, message, _errors = set_usage_baseline(baseline, updated_by=admin_name)
        assert ok, message
        ids["_first_run"] = newest.id
    return ids


def _round_records() -> list:
    """造几条有代表性的逐轮记录（含一条**取不到**、一条老行）。走的是生产同一套编码。"""
    from services.ai.budget import ContextItem
    from services.ai.engine import RoundRecord
    from services.ai.protocol import ContextRequest, DroppedItem

    return [
        RoundRecord(
            index=1, status="requests", request_count=2, item_count=2,
            requests=(
                ContextRequest("file_diff", "a1b2c3d4" * 5, "code/qz_pub/battle/BattleMgr.lua"),
                ContextRequest("file_content", "a1b2c3d4" * 5, "code/qz_pub/battle/BattleMgr.lua",
                               lines="1180-1260"),
            ),
            executed=(
                ContextItem("file_diff", "file_diff code/qz_pub/battle/BattleMgr.lua",
                            "代码差异：code/qz_pub/battle/BattleMgr.lua\n@@ -118,7 +118,9 @@\n+    if target.invincible then\n"),
                ContextItem("file_content", "file_content code/qz_pub/item/ItemMgr.lua",
                            "[取数失败] code/qz_pub/item/ItemMgr.lua：项目没有绑定 Agent 节点，"
                            "platform/agent 模式下平台本地也没有这个仓库的工作副本，读不到文件正文。"
                            "**这不等于「没有内容」，也不等于「没有改动」**。"),
            ),
            dropped=(DroppedItem("request", 2, "超出本次工具请求总预算（20 次），未执行",
                                 "file_diff code/qz_pub/shop/ShopMgr.lua"),),
            budget_notes=("有 1 个上下文请求因超出本次索取额度而未执行。",),
            response_text='{"status": "need_more_context", "reason": "先看战斗逻辑与道具表"}',
            prompt_tokens=180_000, completion_tokens=9_400,
            prompt_chars=210_000, context_chars=25_000, duration_ms=18_400,
            agent="S1", agent_round=1,
        ),
        RoundRecord(
            index=2, status="final",
            response_text="# 变更理解\n\n改了战斗无敌帧的判定顺序。\n\n# 影响面分析\n\n只影响战斗系统。\n",
            prompt_tokens=205_000, completion_tokens=12_800,
            prompt_chars=232_000, context_chars=25_000, duration_ms=21_300,
            agent="S1", agent_round=2,
        ),
    ]


def _csrf(client) -> str:
    with client.session_transaction() as session:
        token = session.get("_csrf_token", "")
        if not token:
            token = uuid.uuid4().hex
            session["_csrf_token"] = token
    return token


def _capture(ids: dict) -> tuple:
    """真登录 → 取页面 HTML 与四个接口的响应。"""
    client = flask_app.test_client()
    token = _csrf(client)
    client.post("/auth/login",
                data={"username": ids["_admin"], "password": PASSWORD, "csrf_token": token},
                headers={"X-CSRFToken": token}, follow_redirects=False)

    page = client.get("/ai-analysis/usage")
    assert page.status_code == 200, page.status_code
    html = page.get_data(as_text=True)

    # 下钻卡只看第一个有数据的项目
    drill_id = ids["新手引导配表"]
    responses = {
        "/ai-analysis/usage/overview": client.get("/ai-analysis/usage/overview").get_json(),
        "/ai-analysis/usage/project/%d" % drill_id:
            client.get(f"/ai-analysis/usage/project/{drill_id}").get_json(),
        "/ai-analysis/platform-budget": client.get("/ai-analysis/platform-budget").get_json(),
        "/ai-analysis/projects/%d/budget" % drill_id:
            client.get(f"/ai-analysis/projects/{drill_id}/budget").get_json(),
        "/ai-analysis/projects/%d/config" % drill_id:
            client.get(f"/ai-analysis/projects/{drill_id}/config").get_json(),
        # 单次运行弹层（含逐轮明细）：**这一份也要走真路由**，否则「取不到」那一行
        # 是我手写进响应里的，页面上显示得再对也不说明后端给的就是它。
        "/ai-analysis/runs/%d/usage" % ids["_first_run"]:
            client.get(f"/ai-analysis/runs/{ids['_first_run']}/usage").get_json(),
    }

    # 「统计范围：全部历史」那一支也要有一份真响应：口径那一行有两句话，两句都得看过。
    # 清掉起点取一份、再把它放回去，页面拿到的仍是同一个起点状态。
    with flask_app.app_context():
        baseline = usage_baseline()
        assert baseline is not None, "种子数据没设上起点，「起点」那一支就截不到了"
        set_usage_baseline(None, updated_by=ids["_admin"])
        all_history = client.get("/ai-analysis/usage/overview").get_json()
        set_usage_baseline(baseline, updated_by=ids["_admin"])
    assert all_history["statistics"]["counted_since"] is None, "清掉起点后仍带着起点"
    assert responses["/ai-analysis/usage/overview"]["statistics"]["counted_since"], (
        "总览那份响应里没有起点 —— 页面会显示成「全部历史」，而库里有起点"
    )
    assert all_history["totals"]["runs"] > responses["/ai-analysis/usage/overview"]["totals"]["runs"], (
        "两份响应的合计一样多 —— 那「起点之前的运行完全不显示」这句话就没被复核到"
    )
    return html, responses, drill_id, all_history


# 量值的判据（都在 SKILL「Quick Reference」里）：正文 < 12px 是缺陷；页面横向滚动是
# 缺陷；卡片高度参差、说明文字比数据还高，是「看起来不专业」的常见来源。
_MEASURE_JS = r"""
() => {
    const px = (value) => Math.round(parseFloat(value) || 0);
    const cards = Array.from(document.querySelectorAll('.aiu-card')).map((el) => ({
        title: (el.querySelector('h2, h3, .aiu-card__title') || {}).textContent?.trim() || '',
        height: Math.round(el.getBoundingClientRect().height)
    }));
    const tiny = Array.from(document.querySelectorAll('.aiu-page *')).filter((el) => {
        if (!el.textContent || !el.textContent.trim()) return false;
        if (el.children.length) return false;
        return px(getComputedStyle(el).fontSize) < 12;
    }).map((el) => ({tag: el.tagName.toLowerCase(), cls: el.className || '',
                     size: px(getComputedStyle(el).fontSize),
                     chars: el.textContent.trim().length}));
    const wide = Array.from(document.querySelectorAll('.aiu-page *')).filter(
        (el) => el.scrollWidth > el.clientWidth + 1 && el.clientWidth > 0
    ).map((el) => ({tag: el.tagName.toLowerCase(), cls: String(el.className || ''),
                    scroll: el.scrollWidth, client: el.clientWidth})).slice(0, 8);
    const cols = Array.from(document.querySelectorAll('#aiuProjectRows tr:first-child td'))
        .map((td) => Math.round(td.getBoundingClientRect().width));
    // 字号分布：SKILL 的 Type Scale 是 12/14/16/18/24/32，随机字号是缺陷。
    const scale = {};
    Array.from(document.querySelectorAll('.aiu-page *')).forEach((el) => {
        if (!el.textContent || !el.textContent.trim() || el.children.length) return;
        const size = px(getComputedStyle(el).fontSize);
        scale[size] = (scale[size] || 0) + 1;
    });
    // 一段文本折成几行：Range.getClientRects().length 才是行数（高度量不出来）。
    const prose = Array.from(document.querySelectorAll('.aiu-page p, .aiu-page li'))
        .filter((el) => el.textContent.trim().length > 40)
        .map((el) => {
            const range = document.createRange();
            range.selectNodeContents(el);
            const rect = el.getBoundingClientRect();
            return {
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                lines: range.getClientRects().length,
                chars: el.textContent.trim().length,
                cls: String(el.className || ''),
                id: el.id || (el.closest('[id]') || {}).id || '',
                head: el.textContent.trim().slice(0, 24)
            };
        }).sort((a, b) => b.height - a.height).slice(0, 6);
    return {
        typeScale: scale,
        prose: prose,
        pageHeight: Math.round(document.querySelector('.aiu-page').getBoundingClientRect().height),
        docHeight: Math.round(document.documentElement.scrollHeight),
        horizontalScroll: document.documentElement.scrollWidth > window.innerWidth + 1,
        cards: cards,
        tinyCount: tiny.length,
        tinyChars: tiny.reduce((sum, item) => sum + item.chars, 0),
        tinySample: tiny.slice(0, 6),
        overflowing: wide,
        projectRowColumns: cols
    };
}
"""


def _measure(page, label: str) -> dict:
    data = page.evaluate(_MEASURE_JS)
    print(f"--- {label} ---")
    print(f"页面高 {data['pageHeight']}px（文档 {data['docHeight']}px）"
          f"  横向滚动={data['horizontalScroll']}")
    print("卡片高度: " + "  ".join(f"{c['title'][:10]}={c['height']}" for c in data["cards"]))
    print(f"小于 12px 的文本节点 {data['tinyCount']} 个、共 {data['tinyChars']} 字")
    for item in data["tinySample"]:
        print(f"    {item['size']}px {item['cls'][:40]} … {item['chars']} 字")
    if data["overflowing"]:
        print("横向溢出（容器比内容窄）:")
        for item in data["overflowing"]:
            print(f"    {item['cls'][:40]} {item['scroll']} > {item['client']}")
    print("字号分布: " + "  ".join(f"{k}px×{v}" for k, v in sorted(data["typeScale"].items(), key=lambda kv: -kv[1])))
    print("最长的几段说明文字（行数按 Range 量）:")
    for item in data["prose"]:
        print(f"    宽 {item['width']}px / {item['lines']} 行 / {item['chars']} 字"
              f"  [{item['cls'][:24]}] {item['id'][:20]} 「{item['head']}…」")
    if data["projectRowColumns"]:
        print(f"项目表列宽合计 {sum(data['projectRowColumns'])}: {data['projectRowColumns']}")
    return data


def _shot(out_prefix: str) -> int:
    from playwright.sync_api import sync_playwright

    ids = _seed()
    html, responses, drill_id, all_history = _capture(ids)

    out_dir = ROOT / ".pytest_tmp"
    html_path = out_dir / f"{out_prefix}.html"
    # `<base>` 指到仓库根：页面里的 `/static/...` 与 `url_for` 出来的绝对路径都能落地；
    # 接口请求由下面的 route 拦掉，不会真的去打网络。
    html = html.replace("<head>", f'<head><base href="{(ROOT / "x").as_uri()}">', 1)
    html_path.write_text(html, encoding="utf-8")

    shots = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        def _path_of(url: str) -> str:
            """`file:///ai-analysis/usage/overview` → `/ai-analysis/usage/overview`。

            页面里那些 fetch 用的是**绝对路径**，而 `page.goto` 走的是 `file://` ——
            路径会落到 `file:///C:/ai-analysis/...`（盘符成了路径的第一段）。按整串比
            永远对不上，表现是每个接口都「Failed to fetch」、静态资源全 404。
            """
            path = url.split("?")[0].split("#")[0]
            match = re.match(r"^[a-z]+://[^/]*(/.*)$", path, re.I)
            path = match.group(1) if match else path
            # Windows 上 `file:///` 后面还跟着盘符：`file:///C:/static/x.js`。
            # 不剥掉它，静态资源与接口就全部匹配不上（表现是整个页面像没加载完）。
            return re.sub(r"^/[A-Za-z]:", "", path).rstrip("/")

        unmatched = []

        def _serve(route):
            url = _path_of(route.request.url)
            for prefix, payload in responses.items():
                if url == prefix:
                    return route.fulfill(status=200, content_type="application/json",
                                         body=json.dumps(payload, ensure_ascii=False))
            if "/static/" in url:
                # 拼回 `static/` 这一段：仓库里的文件在 `static/` 下，
                # 而 URL 里那一段是路由前缀（只按 split 取会在仓库根上找，永远找不到）。
                target = ROOT / "static" / url.split("/static/", 1)[1]
                if target.exists():
                    content_type = {
                        ".css": "text/css", ".js": "application/javascript",
                        ".png": "image/png", ".svg": "image/svg+xml",
                        ".woff2": "font/woff2", ".ico": "image/x-icon",
                    }.get(target.suffix, "application/octet-stream")
                    return route.fulfill(status=200, content_type=content_type,
                                         body=target.read_bytes())
            # 落不到这里说明拦截规则与页面的请求形态对不上了（表现是每个接口都
            # 「Failed to fetch」，看起来像后端挂了）。把 URL 打出来，别猜。
            if route.request.url.startswith("file:"):
                unmatched.append(route.request.url)
            return route.continue_()

        page.route("**/*", _serve)
        page.goto(html_path.as_uri(), wait_until="networkidle")
        page.wait_for_timeout(600)

        for label, size in (("wide", {"width": 1440, "height": 1100}),
                            ("mid", {"width": 1024, "height": 1100}),
                            ("narrow", {"width": 420, "height": 1100})):
            page.set_viewport_size(size)
            page.wait_for_timeout(250)
            path = out_dir / f"{out_prefix}_{label}.png"
            page.screenshot(path=str(path), full_page=True)
            shots.append(path)
            _measure(page, label)

        # 项目明细（下钻卡）单独一张：它默认是折叠的，点第一行才出来
        page.set_viewport_size({"width": 1440, "height": 1100})
        try:
            page.click("#aiuProjectRows .aiu-name-btn", timeout=4000)
            page.wait_for_timeout(900)
            path = out_dir / f"{out_prefix}_drill.png"
            page.screenshot(path=str(path), full_page=True)
            shots.append(path)
        except Exception as exc:  # noqa: BLE001 —— 截图工具，点不开就算了
            print(f"（下钻卡没截到：{exc}）")

        # 统计口径那两个弹层。它们在客户端合成（`openStatsModal` / `openResetModal`），
        # 所以「点了有没有反应、确认词有没有真把按钮锁住」只能真点一次才知道。
        print("口径行（有起点）：" + page.inner_text("#aiuStatsLine"))
        try:
            page.click("#aiuStatsBaselineBtn", timeout=4000)
            page.wait_for_timeout(500)
            path = out_dir / f"{out_prefix}_stats_modal.png"
            page.screenshot(path=str(path))
            shots.append(path)
            print("起点弹层预填：" + page.input_value("#aiuStatsSinceInput")
                  + "（上限 " + page.get_attribute("#aiuStatsSinceInput", "max") + "）")
            page.click("#aiuStatsModal .btn-close")
            page.wait_for_timeout(400)
        except Exception as exc:  # noqa: BLE001
            print(f"（起点弹层没截到：{exc}）")
        try:
            page.click("#aiuStatsResetBtn", timeout=4000)
            page.wait_for_timeout(500)
            locked = page.is_disabled("#aiuResetConfirmBtn")
            page.screenshot(path=str(out_dir / f"{out_prefix}_reset_modal_locked.png"))
            shots.append(out_dir / f"{out_prefix}_reset_modal_locked.png")
            word = page.inner_text("#aiuStatsConfirmWord").strip()
            page.fill("#aiuStatsConfirmInput", word)
            page.wait_for_timeout(300)
            unlocked = not page.is_disabled("#aiuResetConfirmBtn")
            page.screenshot(path=str(out_dir / f"{out_prefix}_reset_modal_ready.png"))
            shots.append(out_dir / f"{out_prefix}_reset_modal_ready.png")
            print(f"确认词「{word}」：没输入时确认按钮 disabled={locked}，"
                  f"输入之后 disabled={not unlocked}")
            assert locked and unlocked, "确认词没有锁住 / 解锁确认按钮"
            page.click("#aiuResetModal .btn-close")
            page.wait_for_timeout(400)
        except Exception as exc:  # noqa: BLE001
            print(f"（重置弹层没截到：{exc}）")

        # 单次运行弹层 + 逐轮明细：这一块是 2026-09-19 新加的，而「取不到的那一条在
        # 界面上分不分得出来」只有真渲染才看得见（它红不红、原因有没有显示出来）。
        try:
            page.click("#aiuRunRows .aiu-btn.secondary", timeout=4000)
            page.wait_for_timeout(700)
            page.screenshot(path=str(out_dir / f"{out_prefix}_run_modal.png"))
            shots.append(out_dir / f"{out_prefix}_run_modal.png")
            # 逐轮表最后一列的「明细」按钮：点第一轮，展开它下面的证据块。
            page.click("#aiuRunModalBody .aiu-round-actions .aiu-btn", timeout=4000)
            page.wait_for_timeout(500)
            page.screenshot(path=str(out_dir / f"{out_prefix}_round_detail.png"), full_page=False)
            shots.append(out_dir / f"{out_prefix}_round_detail.png")
            print("逐轮明细里「取不到」的行数："
                  + str(page.locator("#aiuRunModalBody .aiu-round-failed").count()))
            print("工具表的失败列合计："
                  + page.inner_text("#aiuRunModalBody .aiu-table tbody tr:last-child"))
            # 「分片代理」那张表单独截一次并把每行念一遍：它排在弹层靠下的位置，整屏截图
            # 截不全，而**对账轮那一格显示成什么**（它没有负责的维度）正是这个功能加进来
            # 之后唯一需要眼睛确认的地方 —— 看不了就等于没复核。
            table = page.locator("#aiuRunModalBody table:has(#aiuSubagentTableCaption)")
            if table.count():
                table.scroll_into_view_if_needed()
                page.wait_for_timeout(200)
                table.screenshot(path=str(out_dir / f"{out_prefix}_subagent_table.png"))
                shots.append(out_dir / f"{out_prefix}_subagent_table.png")
                for row in table.locator("tbody tr").all():
                    print("分片行：" + " | ".join(row.locator("td").all_inner_texts()))
            else:
                print("（没找到「分片代理」表：这一份数据里没有 subagents 块）")
            _measure(page, "run-modal")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception as exc:  # noqa: BLE001 —— 截图工具，点不开就算了
            print(f"（单次运行弹层没截到：{exc}）")

        # 「统计范围：全部历史」那一支：换一份真响应重载一次页面。
        responses["/ai-analysis/usage/overview"] = all_history
        page.goto(html_path.as_uri(), wait_until="networkidle")
        page.wait_for_timeout(600)
        print("口径行（无起点）：" + page.inner_text("#aiuStatsLine"))
        page.screenshot(path=str(out_dir / f"{out_prefix}_all_history.png"), full_page=True)
        shots.append(out_dir / f"{out_prefix}_all_history.png")
        _measure(page, "all-history")
        browser.close()

    for path in shots:
        print(path)
    if unmatched:
        print("（这些请求没被拦下，页面里的接口调用会失败）：")
        for item in dict.fromkeys(unmatched):
            print("   ", item)
    # 页面上的量值：字数/行数/横向溢出，先看数再看图
    print(f"（项目 {len(ids) - 1} 个，下钻项目 id={drill_id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_shot(sys.argv[1] if len(sys.argv) > 1 else "aiu_page"))
