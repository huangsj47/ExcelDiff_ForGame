#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给帮助页的某一节截图（分节默认 display:none，要先点对应的 tab）。"""
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover —— 开发工具，不是项目依赖
    raise SystemExit(
        "这个脚本要 playwright（pip install playwright && playwright install chrome）。"
        "它只在**改样式时手动跑**，不参与测试与 CI。"
    )

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import app as flask_app  # noqa: E402


def main(section_id: str, out_name: str) -> int:
    flask_app.config["TESTING"] = True
    html = flask_app.test_client().get("/help").get_data(as_text=True)
    page_path = ROOT / ".pytest_tmp" / "help_probe.html"
    page_path.parent.mkdir(exist_ok=True)
    page_path.write_text(html, encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(page_path.as_uri())
        page.wait_for_timeout(200)
        # 点开对应的 tab（tab 的 data 属性指向分节 id）
        page.evaluate(
            """(id) => {
                document.querySelectorAll('section.help-section').forEach((s) => {
                    s.style.display = s.id === id ? 'block' : 'none';
                });
                document.getElementById(id).scrollIntoView();
            }""",
            section_id,
        )
        page.wait_for_timeout(200)
        el = page.query_selector(f"#{section_id}")
        out = ROOT / ".pytest_tmp" / out_name
        el.screenshot(path=str(out))
        browser.close()
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
