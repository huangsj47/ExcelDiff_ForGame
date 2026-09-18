#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整页计算样式的**逐元素**前后对比（无头 Chrome）。

改样式最怕的不是「没改到」，而是「改到了别处」。所以这里把每一节里每个元素的计算后
字号/字重/行高/上下边距都算出来，按 **DOM 路径**（而不是文本）对齐，打印变了哪些。

为什么必须用真浏览器：同一段文字在不同分节里由不同选择器命中（`.step-content p` vs
裸 `<p>`），还叠着 Bootstrap / FontAwesome 与浏览器默认值，读 CSS 猜不出来。

**自检**：同一份 HTML 连渲染两次必须逐元素相同。不验这一条的话，CDN 上的字体能不能
加载、字体回退成哪一个都会让 line-height 漂移，diff 里全是噪声，真正改动的那几个
反而看不见。

用法：python scripts/diff_help_typography.py
"""
import json
import re
import subprocess
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

DOC = "templates/help.html"

PROBE = """
() => {
  const path = (node) => {
    const parts = [];
    let cur = node;
    while (cur && cur.tagName && cur.tagName !== 'BODY') {
      const parent = cur.parentElement;
      const idx = parent ? Array.prototype.indexOf.call(parent.children, cur) : 0;
      parts.unshift(cur.tagName + ':' + idx);
      cur = parent;
    }
    return parts.join('>');
  };
  const out = {};
  document.querySelectorAll('section.help-section').forEach((sec) => {
    sec.querySelectorAll('*').forEach((node) => {
      if (['SCRIPT', 'STYLE'].includes(node.tagName)) return;
      const cs = getComputedStyle(node);
      out[sec.id + '|' + path(node)] = {
        tag: node.tagName,
        cls: node.className || '',
        text: node.textContent.trim().slice(0, 24),
        size: cs.fontSize,
        weight: cs.fontWeight,
        line: cs.lineHeight,
        mt: cs.marginTop,
        mb: cs.marginBottom
      };
    });
  });
  return out;
}
"""


def render(html: str, tmp: Path, tag: str):
    page_path = tmp / f"help_{tag}.html"
    page_path.write_text(html, encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(page_path.as_uri(), wait_until="networkidle")
        page.evaluate("() => document.fonts.ready")
        page.wait_for_timeout(400)
        rows = page.evaluate(PROBE)
        browser.close()
    return rows


def _stable(html: str, tmp: Path, tag: str) -> dict:
    """同一份 HTML 渲染两次；不一致就说明这个对比工具本身不可信。"""
    first = render(html, tmp, tag + "1")
    second = render(html, tmp, tag + "2")
    noise = [k for k in first if first[k] != second.get(k)]
    if noise:
        print(f"⚠️ 自检失败：同一份 HTML 两次渲染有 {len(noise)} 处不同，例如 {noise[:3]}")
        for k in noise[:3]:
            print("   ", k, first[k], "!=", second.get(k))
    return second


def _style_block(html: str) -> str:
    """取**本页自己**那段 `<style>`。

    渲染出来的整页里有 2 段（`base.html` 还有一段），而 `re.search` 拿的是第一段 ——
    挑错那一段的话，替换之后两边的差别其实在别处，diff 会「一片绿」地骗人（实测过：
    报 0 处变化）。所以按本页独有的一条规则认领。
    """
    hits = [b for b in re.findall(r"<style>.*?</style>", html, re.S) if ".step-content p {" in b]
    assert len(hits) == 1, f"认不出本页的样式块（命中 {len(hits)} 段）"
    return hits[0]


def main() -> int:
    flask_app.config["TESTING"] = True
    after_html = flask_app.test_client().get("/help").get_data(as_text=True)
    head_raw = subprocess.run(
        ["git", "show", f"HEAD:{DOC}"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout

    # 「改前」= 把**这次改的那段 CSS** 换回 HEAD 那一版。
    # 不直接渲染 HEAD 的模板源：`help.html` 里有 Jinja 条件，原文渲染出来的 DOM 与
    # 真正跑起来的不一样，按 DOM 路径对齐就会全都对不上（实测 577/577 报「新增」）。
    # 这段改动整个在 `<style>` 里，DOM 逐字相同，所以只换样式块是精确的。
    before_html = after_html.replace(_style_block(after_html), _style_block(head_raw))
    assert before_html != after_html, "样式块没有差别，这个对比是空的"
    assert before_html.count("<section") == after_html.count("<section")

    tmp = ROOT / ".pytest_tmp" / "typo_diff"
    tmp.mkdir(parents=True, exist_ok=True)
    print("== 自检：同一份 HTML 两次渲染是否一致 ==")
    before = _stable(before_html, tmp, "before")
    after = _stable(after_html, tmp, "after")

    changed = []
    for k, a in after.items():
        b = before.get(k)
        if b is None:
            changed.append(("新增", k, {}, a))
            continue
        delta = {
            f: (b[f], a[f]) for f in ("size", "weight", "line", "mt", "mb") if b[f] != a[f]
        }
        if delta:
            changed.append(("变化", k, delta, a))

    by_section: dict[str, int] = {}
    for _, k, _, _ in changed:
        sec = k.split("|")[0]
        by_section[sec] = by_section.get(sec, 0) + 1
    print(f"\n元素数 before={len(before)} after={len(after)}；变化的 {len(changed)} 个")
    print("按分节：", json.dumps(by_section, ensure_ascii=False))
    for _, k, delta, a in changed:
        print(f"\n[{k.split('|')[0]}] <{a['tag']} class=\"{a['cls']}\"> {a['text']!r}")
        for field, (old, new) in delta.items():
            print(f"    {field}: {old} -> {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
