#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 分析配置弹层：**保存前校验 → 保存 → 关闭**这条链路，在真浏览器里走一遍。

**为什么要有这个脚本**：这一刀改的全是**只有浏览器算得出来**的东西 ——

  * `type="number"` 的**输入中间态**（敲了一半的 `1e`、只有一个 `-`）会把 `value` 读成
    空串，而 `badInput` 是不是 true 只有真引擎说了算。判错的表现不是报错，是
    「留空 = 不限制」那两栏把一个用户从没表达过的语义**静默**存下去。
  * 「点了保存到底关没关弹层」是 Bootstrap 的 Modal 实例说了算；
  * 「跨字段那条错误有没有落到那一栏旁边」要看 `setAiFieldError` 找不找得到容器。

做法与 `scripts/shot_ai_drawer.py` / `shot_ai_usage_page.py` 同一套：临时库里造数据 →
用真路由取真响应 → 浏览器里渲染。**保存那一步的 POST 被拦下来数次数**，所以「本地校验
挡住时一个请求都没发出去」这条是可证的，而不是看界面猜的。

用法：
    python scripts/shot_ai_config_modal.py [输出前缀]

产物写在 `.pytest_tmp/`（不进仓库）。数据全是造的，与线上无关。
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 临时库：文件名里带 `pytest` 才会被 utils/db_safety 认成测试库。
SCRATCH_DB = ROOT / ".pytest_tmp" / "db" / "pytest_aicfg_shot.db"
SCRATCH_DB.parent.mkdir(parents=True, exist_ok=True)
for suffix in ("", "-wal", "-shm"):
    Path(str(SCRATCH_DB) + suffix).unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{SCRATCH_DB}"
os.environ.setdefault("RUN_SCHEDULER", "0")

from app import app as flask_app  # noqa: E402
from app import create_tables  # noqa: E402
from auth.services import register_user  # noqa: E402
from models import Project, db  # noqa: E402

PASSWORD = "pw-123456"
STAMP = uuid.uuid4().hex[:6]


def _seed() -> dict:
    with flask_app.app_context():
        create_tables()
        admin_name = f"aiconf{STAMP}"
        admin, error = register_user(admin_name, PASSWORD, role="platform_admin")
        assert admin is not None, error

        project = Project(code=f"AICONF{STAMP}", name="战斗数值表")
        db.session.add(project)
        db.session.flush()
        db.session.commit()
        return {"_admin": admin_name, "project_id": project.id}


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

    page = client.get(f"/projects/{ids['project_id']}/merged-view")
    assert page.status_code == 200, page.status_code
    html = page.get_data(as_text=True)
    for marker in ("aiConfigModal", "aiSubagentVerifyToggle", "aiSubagentVerifyToggleError"):
        assert marker in html, f"页面里没有 {marker}"

    # 配置读接口的响应**从真路由取回来**，键名与后端不会漂移。
    config = client.get(f"/ai-analysis/projects/{ids['project_id']}/config").get_json()
    assert config and config.get("success"), config
    assert "subagent_verify" in config.get("field_schema", {}), "字段表里没有对账轮"
    return html, config


# 读数一次取全：每一格都是「浏览器算出来的」，不是脚本自己推的。
_PROBE_JS = r"""
() => {
    const el = (id) => document.getElementById(id);
    const visible = (id) => {
        const node = el(id);
        if (!node) return null;
        return {
            text: (node.textContent || '').trim(),
            // `d-none` 是隐藏、`hidden` 属性也是隐藏 —— 两种都要算进去，
            // 只看其中一个会让「错误其实没显示出来」被判成显示。
            shown: !node.classList.contains('d-none') && !node.hidden
        };
    };
    const modal = el('aiConfigModal');
    return {
        subagentChecked: el('aiSubagentToggle') ? el('aiSubagentToggle').checked : null,
        verifyChecked: el('aiSubagentVerifyToggle') ? el('aiSubagentVerifyToggle').checked : null,
        verifyError: visible('aiSubagentVerifyToggleError'),
        budgetError: visible('aiPromptCharBudgetInputError'),
        summary: visible('aiConfigErrorSummary'),
        summaryItems: Array.from(document.querySelectorAll('#aiConfigErrorList a'))
            .map((a) => (a.textContent || '').trim()),
        // 弹层关没关：Bootstrap 是用 `display` 与 `show` 类表达的，两者都读。
        modalOpen: !!modal && modal.classList.contains('show')
            && getComputedStyle(modal).display !== 'none',
        cardStatus: (el('aiConnectionStatus') || {}).textContent || ''
    };
}
"""


def _report(label: str, data: dict) -> None:
    print(f"\n=== {label} ===")
    print(f"  子代理={data['subagentChecked']} 对账轮={data['verifyChecked']}")
    err = data["verifyError"] or {}
    print(f"  对账轮内联错误: shown={err.get('shown')} text={err.get('text')!r}")
    budget = data["budgetError"] or {}
    print(f"  字符预算内联错误: shown={budget.get('shown')} text={budget.get('text')!r}")
    summary = data["summary"] or {}
    print(f"  顶部摘要: shown={summary.get('shown')}")
    for item in data["summaryItems"]:
        print(f"    | {item}")
    print(f"  弹层开着={data['modalOpen']}  卡片状态行={data['cardStatus']!r}")


def _main(out_prefix: str) -> int:
    from playwright.sync_api import sync_playwright

    ids = _seed()
    html, config = _capture(ids)

    out_dir = ROOT / ".pytest_tmp"
    html_path = out_dir / f"{out_prefix}.html"
    # 静态资源按相对路径找 —— 与 shot_ai_drawer 同一个手法。
    html = html.replace("<head>", f'<head><base href="{(ROOT / "x").as_uri()}">', 1)
    html_path.write_text(html, encoding="utf-8")

    posts: list = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1500, "height": 950})

        def _path_of(url: str) -> str:
            path = url.split("?")[0].split("#")[0]
            match = re.match(r"^[a-z]+://[^/]*(/.*)$", path, re.I)
            path = match.group(1) if match else path
            return re.sub(r"^/[A-Za-z]:", "", path).rstrip("/")

        def _serve(route):
            request = route.request
            url = _path_of(request.url)
            if url.endswith("/config"):
                if request.method == "POST":
                    # 保存那一步拦在这里：**只数次数**，不真发出去（这是页面不是服务端）。
                    posts.append(json.loads(request.post_data or "{}"))
                    return route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({"success": True, "message": "AI 分析配置已保存。"},
                                        ensure_ascii=False),
                    )
                return route.fulfill(status=200, content_type="application/json",
                                     body=json.dumps(config, ensure_ascii=False))
            if "/static/" in url:
                target = ROOT / "static" / url.split("/static/", 1)[1]
                if target.exists():
                    content_type = {".css": "text/css", ".js": "application/javascript",
                                    ".svg": "image/svg+xml"}.get(
                        target.suffix, "application/octet-stream")
                    return route.fulfill(status=200, content_type=content_type,
                                         body=target.read_bytes())
            return route.continue_()

        page.route("**/*", _serve)
        page.goto(html_path.as_uri(), wait_until="networkidle")
        page.wait_for_timeout(400)

        page.click("#aiConfigOpenBtn")
        page.wait_for_timeout(900)
        assert page.evaluate("() => document.getElementById('aiConfigModal').classList.contains('show')"), (
            "弹层没打开，后面的读数都不成立"
        )

        # --- 1. 关掉子代理、勾上对账轮 → 内联错误要**当场**出现 ---
        page.evaluate("""() => {
            const sub = document.getElementById('aiSubagentToggle');
            sub.checked = false;
            sub.dispatchEvent(new Event('change', {bubbles: true}));
            const ver = document.getElementById('aiSubagentVerifyToggle');
            ver.checked = true;
            ver.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        page.wait_for_timeout(250)
        bad = page.evaluate(_PROBE_JS)
        _report("失效组合：子代理关 + 对账轮开", bad)
        assert bad["verifyError"] and bad["verifyError"]["shown"], (
            "跨字段那条错误没有落到对账轮那一栏旁边"
        )
        assert "子代理模式" in bad["verifyError"]["text"], bad["verifyError"]["text"]

        # --- 2. 这种状态下点保存 → **一个请求都不该发出去** ---
        page.click("#aiConfigSaveBtn")
        page.wait_for_timeout(700)
        blocked = page.evaluate(_PROBE_JS)
        _report("本地校验挡住的那一次保存", blocked)
        assert posts == [], f"本地校验没挡住，配置已经提交出去了：{posts}"
        assert blocked["summary"]["shown"], "没有顶部摘要，用户不知道哪儿错了"
        assert any("对账轮" in item for item in blocked["summaryItems"]), blocked["summaryItems"]
        assert blocked["modalOpen"] is True, "被挡下来时弹层不该关"

        # --- 3. 数字框里的**中间态**（`1e`）不能被当成「留空」 ---
        page.evaluate("""() => {
            const sub = document.getElementById('aiSubagentToggle');
            sub.checked = true;                       // 先把跨字段那一条解开
            sub.dispatchEvent(new Event('change', {bubbles: true}));
            const ver = document.getElementById('aiSubagentVerifyToggle');
            ver.checked = false;
            ver.dispatchEvent(new Event('input', {bubbles: true}));
            document.getElementById('aiPromptCharBudgetInput').value = '';
        }""")
        page.click("#aiPromptCharBudgetInput")
        page.keyboard.type("1e", delay=60)
        page.wait_for_timeout(150)
        typed = page.evaluate(
            "() => { const el = document.getElementById('aiPromptCharBudgetInput');"
            " return {value: el.value, badInput: !!(el.validity && el.validity.badInput)}; }"
        )
        print("\n=== 字符预算框里敲 `1e` ===")
        print(f"  value={typed['value']!r} badInput={typed['badInput']}")
        assert typed["value"] == "" and typed["badInput"] is True, (
            "这一版 Chrome 的行为变了（value 不再是空串 / badInput 不再为真），"
            "`aiFieldProblem` 里那条判据的前提没了 —— 要么换判据，要么改这条断言"
        )
        page.click("#aiConfigSaveBtn")
        page.wait_for_timeout(700)
        mid = page.evaluate(_PROBE_JS)
        _report("输入中间态下的保存", mid)
        assert posts == [], f"「敲了一半」被当成合法值提交了：{posts}"
        assert mid["budgetError"] and mid["budgetError"]["shown"], (
            "字符预算那一栏没有报「请填写数字」—— 它被当成「留空 = 不限制」放过去了"
        )

        # --- 4. 都合法了 → 保存 → **弹层要关**，且卡片上要说「已保存」 ---
        page.evaluate("""() => {
            document.getElementById('aiPromptCharBudgetInput').value = '200000';
        }""")
        page.click("#aiConfigSaveBtn")
        page.wait_for_timeout(1200)
        ok = page.evaluate(_PROBE_JS)
        _report("校验通过后的保存", ok)
        assert len(posts) == 1, f"保存请求发了 {len(posts)} 次（应当是 1 次）：{posts}"
        assert posts[0].get("subagent_enabled") is True
        assert posts[0].get("subagent_verify") is False
        assert ok["modalOpen"] is False, "保存成功了弹层却没关"
        assert ok["cardStatus"].startswith("AI 分析配置已保存。"), (
            f"关掉弹层之后没有任何「已保存」的说法：{ok['cardStatus']!r}"
        )
        page.screenshot(path=str(out_dir / f"{out_prefix}_saved.png"))

        print(f"\n图在 {out_dir}（{out_prefix}_*.png）")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1] if len(sys.argv) > 1 else "ai_config_modal"))
