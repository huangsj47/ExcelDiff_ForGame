# -*- coding: utf-8 -*-
"""DIFF-B03：Excel 工作表名（来自被审核文件，**不可信**）不得进入内联脚本。

## 缺陷形态

工作表名取自被审核的 .xlsx，与提交一起进仓库，审核者只负责"打开 diff 看一眼"。
它出现在两个地方，两处都会把数据当成**代码**：

1. `templates/diff_partials/excel_diff.html` 的
   `onclick="showExcelSheet('{{ sheet_name }}')"`。
   Jinja 的 HTML 自动转义会把 `'` 写成 `&#39;`，但**属性值是先被 HTML 解码、
   再交给 JS 引擎编译的** —— `&#39;` 解码回来就是一个真正的单引号，
   于是 `a');window.__qa=1;//` 直接闭合字符串，点击标签即执行任意脚本。
2. `static/js/diff-handlers.js` 的 `generateExcelTabs()` /
   `showExcelSheetInContainer()`：`onclick="switchExcelSheet('${sheet.name}')"`
   拼进字符串再 `innerHTML = ...`。innerHTML 里的 `onclick=` 会被编译成事件处理器，
   效果与 (1) 相同；`data-sheet="${sheet.name}"` 还允许用 `"` 直接逃出属性。

## 为什么断言"生成的 DOM 里没有 on* 属性"

这比断言"注入串没出现在 HTML 里"更接近根因：问题的本质不是某个字符串长得可疑，
而是**不可信数据被放进了代码位置**。同一份数据放在文本节点或 `data-*` 里是安全的
（属性值仍会被 HTML 解码，但解码后只参与取值，不参与编译），所以下面同时断言
"表名原样出现在文本/属性里"——防止有人用"把表名整个删掉"的方式让测试变绿。

## 两层各自防什么

* Python 侧渲染真实的 Jinja 模板 —— 覆盖服务端渲染路径（commit_diff_new 首屏）。
* Node 侧把 `static/js/diff-handlers.js` 的**真实源码**放进隔离 vm 跑
  （只伪造最小的 DOM 骨架，不启浏览器、不联网）—— 覆盖 JS 重建标签的路径。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html import unescape as _html_unescape
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = PROJECT_ROOT / "templates"
PARTIAL = "diff_partials/excel_diff.html"
DIFF_HANDLERS = PROJECT_ROOT / "static" / "js" / "diff-handlers.js"

# 报告复现用的标记（无害：只写一个变量，不触碰任何真实数据）
INJECTION = "a');window.__qa=1;//"
# 另一种形态：HTML 标签注入（属性内的 `"` 逃逸 / innerHTML 直接插标签）
TAG_INJECTION = '"><img src=x onerror="window.__qa=1">'


def _excel_column_letter(index: int) -> str:
    result = ""
    while index >= 0:
        result = chr(65 + (index % 26)) + result
        index = index // 26 - 1
    return result


def _jinja_env() -> Environment:
    """直接渲染 partial：它只用 excel_column_letter / format_cell_value 两个过滤器，
    没有 url_for / current_user 之类的依赖，不需要拉起整个 Flask 应用。"""
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    env.filters["excel_column_letter"] = _excel_column_letter
    env.filters["format_cell_value"] = lambda value: "" if value is None else str(value)
    return env


def _sheet_payload(sheet_name: str, headers=None):
    headers = headers if headers is not None else ["id", "name"]
    return {
        "type": "excel",
        "sheets": {
            sheet_name: {
                "has_changes": True,
                "status": "modified",
                "headers": headers,
                "rows": [
                    {
                        "row_number": 2,
                        "status": "modified",
                        "cells": [
                            {"value": "1", "status": "unchanged"},
                            {"value": "new", "old_value": "old", "status": "changed"},
                        ],
                    }
                ],
            }
        },
    }


def _multi_sheet_payload(names):
    return {"type": "excel", "sheets": {name: _sheet_payload(name)["sheets"][name] for name in names}}


def _render_partial(payload: dict) -> str:
    template = _jinja_env().get_template(PARTIAL)
    return template.render(diff_data=payload, excel_data=payload)


# --- 浏览器口径的标签/属性解析 -------------------------------------------
# 关键在"属性值由引号界定"：引号**内**的 `"` 不结束属性值，只有裸的 `"` 才结束。
# 这正是 `data-sheet="{{ name }}"` 被 `"` 逃逸的判定依据，也是内联 `&#39;`
# 能在 JS 引擎里还原成真引号的原因（属性值先解码、再编译）。
_TAG_RE = re.compile(r'<([a-zA-Z][^\s/>]*)((?:"[^"]*"|\'[^\']*\'|[^>"\'])*)>')
_ATTR_RE = re.compile(r'([^\s=/>]+)(?:\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]*))?')


def _parse_tags(html: str):
    """返回 [(tag_name, {attr_name: 解码后的值})]，属性名统一小写。"""
    tags = []
    for match in _TAG_RE.finditer(html):
        attrs = {}
        for attr in _ATTR_RE.finditer(match.group(2)):
            name = attr.group(1).lower()
            raw = attr.group(2)
            if raw is None:
                raw = ""
            else:
                raw = raw[1:-1] if raw[:1] in ("\"", "'") and raw[-1:] == raw[:1] else raw
            attrs[name] = _html_unescape(raw)
        tags.append((match.group(1).lower(), attrs))
    return tags


def _inline_handler_attrs(html: str):
    """所有 `on*` 属性 —— 出现任何一个就意味着"数据被放进了代码位置"。"""
    return [
        (tag, name, value)
        for tag, attrs in _parse_tags(html)
        for name, value in attrs.items()
        if name.startswith("on")
    ]


def _element_by_attr(html: str, attr: str, value: str):
    for tag, attrs in _parse_tags(html):
        if attrs.get(attr) == value:
            return tag, attrs
    return None, {}


def _text_content(html: str) -> str:
    return _html_unescape(_TAG_RE.sub(" ", html))


def _has_class(attrs, name: str) -> bool:
    """按 class 词元精确匹配 —— `excel-sheet-tab` 不能匹配上 `excel-sheet-tabs`。"""
    return name in (attrs.get("class") or "").split()


# ==========================================================================
# 1. Python 侧：服务端渲染的模板
# ==========================================================================


def test_partial_has_no_inline_handler_carrying_the_sheet_name():
    """渲染出的标签不得带内联 onclick —— 表名不能被放进 JS 字符串字面量。"""
    html = _render_partial(_sheet_payload(INJECTION))

    found = _inline_handler_attrs(html)
    assert found == [], (
        "模板仍然用内联 onclick 传表名。Jinja 的 HTML 转义在这里救不了："
        "属性值先被 HTML 解码（&#39; → '）、再交给 JS 引擎编译，注入串照样执行。"
        f"发现的内联处理器：{found}\n渲染结果片段：\n{html[:600]}"
    )


def test_partial_keeps_the_sheet_name_as_data_and_text():
    """移除内联脚本后，表名必须**原样**留在 data-sheet 与文本里 —— 交互要靠它工作，
    不能靠"把可疑名字删掉"来通过上面的断言。"""
    html = _render_partial(_sheet_payload(INJECTION))

    tag, attrs = _element_by_attr(html, "data-sheet", INJECTION)
    assert tag is not None, (
        f"没有 data-sheet 原样等于表名的元素，切换逻辑会失效：\n{html[:600]}"
    )
    assert attrs.get("aria-current") in ("true", "false"), "aria-current 语义丢了"
    assert INJECTION in _text_content(html), "表名没有作为文本渲染出来，标签会变成空白"


def test_partial_escapes_a_name_that_tries_to_close_the_attribute():
    """带 `"` 的表名不能逃出属性（否则可以凭空造出 onerror= 之类的新属性）。"""
    html = _render_partial(_sheet_payload(TAG_INJECTION))

    assert [t for t, _ in _parse_tags(html) if t == "img"] == [], (
        f"表名里的 `<img ...>` 被解析成了真标签：\n{html[:600]}"
    )
    assert _inline_handler_attrs(html) == [], (
        f"表名把内联处理器顶成了新属性：{_inline_handler_attrs(html)}"
    )
    _, attrs = _element_by_attr(html, "data-sheet", TAG_INJECTION)
    assert attrs, "带引号的表名没有原样留在 data-sheet 上"


def test_partial_keeps_the_active_and_aria_current_semantics():
    """交互语义不能顺手改掉：首个标签 active + aria-current=true，其余 false。"""
    html = _render_partial(_multi_sheet_payload(["甲", "乙"]))

    buttons = [attrs for _, attrs in _parse_tags(html) if _has_class(attrs, "excel-sheet-tab")]
    assert len(buttons) == 2, f"标签数量不对：{buttons}"
    assert "active" in buttons[0].get("class", ""), "首个标签没有 active"
    assert "active" not in buttons[1].get("class", ""), "第二个标签不该是 active"
    assert buttons[0].get("aria-current") == "true"
    assert buttons[1].get("aria-current") == "false"


# ==========================================================================
# 2. Node 侧：真实的 static/js/diff-handlers.js
# ==========================================================================

_NODE_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');

// ---------- 最小 DOM 骨架 ----------
// 只实现被测函数用到的那一小撮 API。故意不做完整 HTML 解析器：
// 内联处理器能不能执行，取决于"标签里有没有 on* 属性"，
// 而属性是按浏览器规则（引号内的 `"` 才结束属性值）切出来的。
const NAMED = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };

function decodeEntities(s) {
  return String(s).replace(/&(#[xX]?[0-9a-fA-F]+|[a-zA-Z]+);/g, (m, ent) => {
    if (ent[0] === '#') {
      const hex = ent[1] === 'x' || ent[1] === 'X';
      const code = parseInt(hex ? ent.slice(2) : ent.slice(1), hex ? 16 : 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return Object.prototype.hasOwnProperty.call(NAMED, ent) ? NAMED[ent] : m;
  });
}

const ALL_ELEMENTS = [];

function makeClassList(el) {
  const list = {
    add(name) {
      const set = new Set(el._classes());
      set.add(name);
      el._setClasses([...set]);
    },
    remove(name) {
      const set = new Set(el._classes());
      set.delete(name);
      el._setClasses([...set]);
    },
    contains(name) {
      return el._classes().indexOf(name) !== -1;
    },
  };
  return list;
}

class Element {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};     // 名 -> 解码后的值
    this.rawAttributes = {};  // 名 -> 原始值
    this.listeners = {};
    this._textContent = '';
    this._innerHTML = '';
    this._ownClasses = [];
    ALL_ELEMENTS.push(this);
  }

  _classes() {
    return this._ownClasses.slice();
  }

  _setClasses(list) {
    this._ownClasses = list;
    this.attributes['class'] = list.join(' ');
    this.rawAttributes['class'] = list.join(' ');
  }

  // dataset 直接落在 data-* 属性上，这样"有没有 on* 属性"的断言能看到它
  get dataset() {
    const el = this;
    return new Proxy({}, {
      get(_t, key) {
        return el.attributes['data-' + String(key)];
      },
      set(_t, key, value) {
        el.attributes['data-' + String(key)] = String(value);
        el.rawAttributes['data-' + String(key)] = String(value);
        return true;
      },
      has(_t, key) {
        return ('data-' + String(key)) in el.attributes;
      },
    });
  }

  get classList() {
    if (!this._classList) this._classList = makeClassList(this);
    return this._classList;
  }

  get className() {
    return this.attributes['class'] || '';
  }

  set className(value) {
    this._ownClasses = String(value).split(/\s+/).filter(Boolean);
    this.attributes['class'] = String(value);
    this.rawAttributes['class'] = String(value);
  }

  get id() {
    return this.attributes['id'] || '';
  }

  get textContent() {
    if (this.childNodes.length) {
      return this.childNodes.map((c) => c.textContent).join('');
    }
    return this._textContent;
  }

  set textContent(value) {
    this.childNodes = [];
    this._textContent = String(value);
    this._innerHTML = '';
  }

  get innerHTML() {
    if (this._innerHTML) return this._innerHTML;
    const text = this.textContent;
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  set innerHTML(value) {
    const html = String(value);
    this._innerHTML = html;
    this._textContent = '';
    this.childNodes = parseHtmlFragment(html);
    this.childNodes.forEach((child) => {
      child.parentNode = this;
    });
  }

  setAttribute(name, value) {
    this.attributes[String(name)] = String(value);
    this.rawAttributes[String(name)] = String(value);
    if (String(name) === 'class') this._ownClasses = String(value).split(/\s+/).filter(Boolean);
  }

  getAttribute(name) {
    const key = String(name);
    return key in this.attributes ? this.attributes[key] : null;
  }

  hasAttribute(name) {
    return String(name) in this.attributes;
  }

  appendChild(node) {
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  insertBefore(node, ref) {
    const index = this.childNodes.indexOf(ref);
    node.parentNode = this;
    if (index === -1) this.childNodes.push(node);
    else this.childNodes.splice(index, 0, node);
    return node;
  }

  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }

  removeEventListener(type, handler) {
    const list = this.listeners[type] || [];
    const i = list.indexOf(handler);
    if (i !== -1) list.splice(i, 1);
  }

  dispatch(type) {
    const handlers = (this.listeners[type] || []).slice();
    handlers.forEach((fn) => fn.call(this, { type, currentTarget: this, target: this }));
    return handlers.length;
  }

  // 骨架只认 `.class` 选择器；别的形态一律抛 SyntaxError。
  // 这是刻意的：真实浏览器里 `.tab[data-sheet="a\"><img ..."]` 也会抛 SyntaxError，
  // 把表名拼进选择器的写法必须在这里就失败，而不是"静默不激活"。
  querySelectorAll(selector) {
    const sel = String(selector).trim();
    if (!/^\.[A-Za-z0-9_-]+$/.test(sel)) {
      throw new SyntaxError("stub: unsupported selector " + sel);
    }
    return querySelectorAllByClass(sel.slice(1));
  }

  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length ? all[0] : null;
  }
}

function querySelectorAllByClass(cls) {
  return ALL_ELEMENTS.filter((el) => el.classList.contains(cls));
}

// 极简 HTML 片段解析：只认识"开始标签 + 文本"。
// 属性切开规则与浏览器一致：引号内的 `"` 才结束属性值。
function parseHtmlFragment(html) {
  const nodes = [];
  const tagRe = /<([a-zA-Z][^\s/>]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g;
  let match;
  while ((match = tagRe.exec(html)) !== null) {
    const el = new Element(match[1]);
    const attrBlob = match[2];
    const attrRe = /([^\s=/>]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>]*))?/g;
    let attr;
    while ((attr = attrRe.exec(attrBlob)) !== null) {
      const name = attr[1];
      let raw = attr[2];
      if (raw === undefined) raw = '';
      else raw = raw.replace(/^["']|["']$/g, '');
      el.attributes[name] = decodeEntities(raw);
      el.rawAttributes[name] = raw;
      if (name.toLowerCase() === 'class') {
        el._ownClasses = el.attributes[name].split(/\s+/).filter(Boolean);
      }
    }
    nodes.push(el);
  }
  return nodes;
}

const byId = {};
const document = {
  createElement(tag) {
    return new Element(tag);
  },
  createTextNode(text) {
    const el = new Element('#text');
    el.textContent = String(text);
    return el;
  },
  getElementById(id) {
    return byId[id] || null;
  },
  querySelectorAll(selector) {
    const sel = String(selector).trim();
    if (!/^\.[A-Za-z0-9_-]+$/.test(sel)) {
      throw new SyntaxError("stub: unsupported selector " + sel);
    }
    return querySelectorAllByClass(sel.slice(1));
  },
  querySelector(selector) {
    const all = document.querySelectorAll(selector);
    return all.length ? all[0] : null;
  },
};

const sandbox = {
  document,
  console: { log() {}, warn() {}, error() {} },
  window: {},
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'diff-handlers.js' });

// ---------- 被测数据的构造 ----------
const INJECTION = "a');window.__qa=1;//";
const TAG_INJECTION = '"><img src=x onerror="window.__qa=1">';

function sheetWithChanges() {
  return { headers: ['id'], rows: [{ row_number: 2, status: 'added', data: { id: '1' } }] };
}
function sheetWithoutChanges() {
  return { headers: ['id'], rows: [{ row_number: 2, status: 'unchanged', data: { id: '1' } }] };
}

function resetDom() {
  ALL_ELEMENTS.length = 0;
  Object.keys(byId).forEach((k) => delete byId[k]);
  const tabs = document.createElement('div');
  tabs.setAttribute('id', 'excel-sheet-tabs');
  byId['excel-sheet-tabs'] = tabs;
  return tabs;
}

function tabReport(tabsContainer) {
  const tabs = ALL_ELEMENTS.filter((el) => el.classList.contains('excel-sheet-tab'));
  return tabs.map((el) => ({
    tag: el.tagName,
    classes: el._classes(),
    attrs: el.attributes,
    text: el.textContent,
    listenerCount: (el.listeners['click'] || []).length,
    parentIsTabs: el.parentNode === tabsContainer,
  }));
}

function collectHandlerAttrs(elements) {
  const found = [];
  elements.forEach((el) => {
    Object.keys(el.attributes).forEach((name) => {
      if (/^on/i.test(name)) {
        found.push({ tag: el.tagName, name: name, value: el.attributes[name] });
      }
    });
  });
  return found;
}

const report = {};

// --- A. generateExcelTabs：恶意表名 ---
{
  const tabs = resetDom();
  const sheets = {};
  sheets[INJECTION] = sheetWithChanges();
  sheets[TAG_INJECTION] = sheetWithoutChanges();
  sheets['正常表'] = sheetWithChanges();
  sandbox.generateExcelTabs(sheets);
  const tabsInDom = ALL_ELEMENTS.filter((el) => el.classList.contains('excel-sheet-tab'));
  report.generateTabs = {
    tabs: tabReport(tabs),
    handlerAttrs: collectHandlerAttrs(tabsInDom),
    rawInnerHtml: tabs._innerHTML,
    containerHtmlLeakedScript: /window\.__qa|<img/i.test(tabs._innerHTML),
  };
}

// --- B. 行为保持：点击 → switchExcelSheet 用**原样的**表名 ---
{
  resetDom();
  const sheets = {};
  sheets['有变更'] = sheetWithChanges();
  sheets['无变更'] = sheetWithoutChanges();
  const content = document.createElement('div');
  content.setAttribute('id', 'sheet-content-有变更');
  byId['sheet-content-有变更'] = content;
  const content2 = document.createElement('div');
  content2.setAttribute('id', 'sheet-content-无变更');
  byId['sheet-content-无变更'] = content2;
  sandbox.generateExcelTabs(sheets);
  const tabsInDom = ALL_ELEMENTS.filter((el) => el.classList.contains('excel-sheet-tab'));
  const byName = {};
  tabsInDom.forEach((el) => {
    byName[el.getAttribute('data-sheet')] = el;
  });
  const changedTab = byName['有变更'];
  const unchangedTab = byName['无变更'];
  const dispatched = changedTab ? changedTab.dispatch('click') : -1;
  report.behaviour = {
    order: tabsInDom.map((el) => el.getAttribute('data-sheet')),
    changedTabActiveAfterClick: changedTab ? changedTab.classList.contains('active') : null,
    changedContentActive: content.classList.contains('active'),
    unchangedTabActive: unchangedTab ? unchangedTab.classList.contains('active') : null,
    unchangedHasDisabledClass: unchangedTab ? unchangedTab.classList.contains('excel-tab-disabled') : null,
    unchangedHasNoHandler: unchangedTab ? (unchangedTab.listeners['click'] || []).length === 0 : null,
    firstTabActiveOnRender: tabsInDom.length ? tabsInDom[0].classList.contains('active') : null,
    dispatchCount: dispatched,
    dataSheetRoundTrip: tabsInDom.map((el) => el.getAttribute('data-sheet')),
  };
}

// --- C. 表名里的引号不能让 switchExcelSheet 抛异常（选择器注入） ---
{
  resetDom();
  const sheets = {};
  sheets[INJECTION] = sheetWithChanges();
  sandbox.generateExcelTabs(sheets);
  let threw = null;
  let activated = null;
  const content = document.createElement('div');
  content.setAttribute('id', 'sheet-content-' + INJECTION);
  byId['sheet-content-' + INJECTION] = content;
  try {
    sandbox.switchExcelSheet(INJECTION);
    const tabsInDom = ALL_ELEMENTS.filter((el) => el.classList.contains('excel-sheet-tab'));
    activated = tabsInDom.length ? tabsInDom[0].classList.contains('active') : null;
  } catch (err) {
    threw = String(err && err.message ? err.message : err);
  }
  report.quotedSwitch = { threw: threw, activated: activated };
}

// --- D. showExcelSheetInContainer：同样的 onclick / innerHTML 拼接 ---
{
  resetDom();
  const container = document.createElement('div');
  container.setAttribute('id', 'excel-container-1');
  byId['excel-container-1'] = container;
  const sheets = {};
  sheets[INJECTION] = sheetWithChanges();
  sheets[TAG_INJECTION] = sheetWithChanges();
  sandbox.showExcelSheetInContainer({ sheets: sheets }, 'excel-container-1');
  const parsed = container.childNodes || [];
  const descendants = [];
  const walk = (nodes) => {
    nodes.forEach((n) => {
      descendants.push(n);
      if (n.childNodes) walk(n.childNodes);
    });
  };
  walk(parsed);
  report.container = {
    handlerAttrs: collectHandlerAttrs(descendants),
    htmlLeakedScript: /<img/i.test(container._innerHTML),
    childCount: descendants.length,
  };
}

// --- E. 表头（同样来自 Excel 文件）进入 title= 与文本 ---
{
  resetDom();
  const content = document.createElement('div');
  content.setAttribute('id', 'excel-content');
  byId['excel-content'] = content;
  const sheets = {};
  sheets['S'] = {
    headers: [TAG_INJECTION],
    rows: [{ row_number: 2, status: 'added', data: {} }],
  };
  sandbox.generateExcelContent(sheets);
  const descendants = [];
  const walk = (nodes) => {
    nodes.forEach((n) => {
      descendants.push(n);
      if (n.childNodes) walk(n.childNodes);
    });
  };
  walk(content.childNodes || []);
  report.headers = {
    handlerAttrs: collectHandlerAttrs(descendants),
    htmlLeakedTag: /<img/i.test(content._innerHTML),
  };
}

// --- F. 单元格值（同样是 Excel 内容）里的 {key,value} 参数对 ---
{
  resetDom();
  const content = document.createElement('div');
  content.setAttribute('id', 'excel-content');
  byId['excel-content'] = content;
  const CELL_INJECTION = '{key,<img src=x onerror="window.__qa=1">}';
  const sheets = {};
  sheets['S'] = {
    headers: ['id'],
    rows: [{
      row_number: 2,
      status: 'modified',
      data: { id: 'x' },
      cell_changes: [
        { column: 'id', old_value: '{key,1}', new_value: CELL_INJECTION },
      ],
    }],
  };
  sandbox.generateExcelContent(sheets);
  const descendants = [];
  const walk = (nodes) => {
    nodes.forEach((n) => {
      descendants.push(n);
      if (n.childNodes) walk(n.childNodes);
    });
  };
  walk(content.childNodes || []);
  report.cellValues = {
    handlerAttrs: collectHandlerAttrs(descendants),
    htmlLeakedTag: /<img/i.test(content._innerHTML),
  };
}

process.stdout.write(JSON.stringify(report));
"""


def _node_binary() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过 JS 层验证")
    return node


def _run_harness() -> dict:
    """把真实 diff-handlers.js 放进隔离 vm 跑，拿回结构化结果。"""
    proc = subprocess.run(
        [_node_binary(), "-", str(DIFF_HANDLERS)],
        input=_NODE_HARNESS,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, (
        "Node 侧 harness 执行失败（通常意味着被测函数抛异常了）：\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert proc.stdout.strip(), f"harness 没有输出：\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def js_report() -> dict:
    return _run_harness()


def test_js_tabs_have_no_inline_event_handler_attributes(js_report):
    """生成的标签里不能有 on* 属性 —— innerHTML 会把它们编译成可执行处理器。"""
    data = js_report["generateTabs"]
    assert data["handlerAttrs"] == [], (
        "generateExcelTabs 把表名拼进了内联事件处理器，点击标签即执行注入的脚本："
        f"{data['handlerAttrs']}"
    )
    assert not data["containerHtmlLeakedScript"], (
        f"生成的 HTML 里出现了注入载荷：{data['rawInnerHtml'][:400]}"
    )


def test_js_tabs_keep_the_sheet_name_verbatim(js_report):
    """去掉内联处理器之后，表名必须原样落在 data-sheet / 文本上，切换才能工作。"""
    data = js_report["generateTabs"]
    names = [t["attrs"].get("data-sheet") for t in data["tabs"]]
    assert INJECTION in names, f"data-sheet 丢了恶意表名（不能靠删数据通过）：{names}"
    assert TAG_INJECTION in names, f"data-sheet 丢了带引号的表名：{names}"
    texts = [t["text"] for t in data["tabs"]]
    assert INJECTION in texts, f"标签文本丢了表名：{texts}"
    assert TAG_INJECTION in texts, f"标签文本丢了带引号的表名：{texts}"


def test_js_tabs_preserve_ordering_active_and_disabled_semantics(js_report):
    """排序（有变更在前，同组按名称）、首个 active、无变更 disabled + 不可点击。"""
    behaviour = js_report["behaviour"]
    assert behaviour["order"] == ["有变更", "无变更"], (
        f"排序逻辑被改动了：{behaviour['order']}"
    )
    assert behaviour["firstTabActiveOnRender"] is True, "首个标签没有 active"
    assert behaviour["unchangedHasDisabledClass"] is True, "无变更的标签没有 excel-tab-disabled"
    assert behaviour["unchangedHasNoHandler"] is True, "无变更的标签仍绑定了点击处理器"
    assert behaviour["dataSheetRoundTrip"] == ["有变更", "无变更"]


def test_js_clicking_a_tab_still_switches_sheets(js_report):
    """交互保持：点击有变更的标签 → active 转移 + 对应内容显示。"""
    behaviour = js_report["behaviour"]
    assert behaviour["dispatchCount"] == 1, "有变更的标签没有可用的点击处理器"
    assert behaviour["changedTabActiveAfterClick"] is True, "点击后标签没有变成 active"
    assert behaviour["changedContentActive"] is True, "点击后对应的工作表内容没有显示"


def test_js_switch_with_a_quoted_name_does_not_break(js_report):
    """表名里的引号不能把 querySelector 拼坏（原来会抛 SyntaxError，整段切换失效）。"""
    data = js_report["quotedSwitch"]
    assert data["threw"] is None, f"带引号的表名让切换抛异常：{data['threw']}"
    assert data["activated"] is True, "带引号的表名没能激活对应标签"


def test_js_container_tabs_have_no_inline_handler(js_report):
    """showExcelSheetInContainer 是同一处缺陷的第二个出口（合并 diff / 周版本页）。"""
    data = js_report["container"]
    assert data["handlerAttrs"] == [], (
        f"showExcelSheetInContainer 仍在拼内联 onclick：{data['handlerAttrs']}"
    )
    assert not data["htmlLeakedScript"], "容器 HTML 里出现了注入标签"


def test_js_table_headers_do_not_break_out_of_attributes(js_report):
    """表头（Excel 第一行）同样不可信：不能逃出 title="..." 或插成新标签。"""
    data = js_report["headers"]
    assert data["handlerAttrs"] == [], (
        f"表头把 on* 顶成了新属性：{data['handlerAttrs']}"
    )
    assert not data["htmlLeakedTag"], "表头里的 `<img ...>` 被当成标签插进了表格"


def test_js_cell_values_in_parameter_lists_are_escaped(js_report):
    """单元格值里的 `{key,<img onerror>}` 走的是"参数对高亮"分支 ——
    该分支原先明确写着"不转义"，把键值直接拼进 innerHTML。"""
    data = js_report["cellValues"]
    assert data["handlerAttrs"] == [], (
        f"单元格值把 on* 顶成了新属性：{data['handlerAttrs']}"
    )
    assert not data["htmlLeakedTag"], "单元格值里的 `<img ...>` 被当成标签插进了表格"


# ==========================================================================
# 3. 同类模式全仓扫描：表名不得再出现在任何内联事件处理器里
# ==========================================================================

# 「内联处理器 + 表名插值」的形态：
#   onclick="f('${sheet.name}')"        （JS 模板字符串）
#   onclick="f('{{ sheet_name }}')"     （Jinja）
# 先按引号切出属性值（引号内的另一种引号不算结束），再在值里找表名插值 ——
# 这样既不会跨属性误报，也不会因为值里带 `'` 而漏报。
_INLINE_HANDLER_RE = re.compile(
    r"""on[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)'|`([^`]*)`)""", re.I
)
_SHEET_REF_RE = re.compile(
    r"""\$\{[^}]*\b(?:sheet\.name|sheetName|sheet_name)\b[^}]*\}
      |\{\{[^}]*\b(?:sheet_name|sheet\.name)\b[^}]*\}\}""",
    re.I | re.X,
)


def _sheet_name_inline_handlers(text: str):
    hits = []
    for match in _INLINE_HANDLER_RE.finditer(text):
        value = next(group for group in match.groups() if group is not None)
        if _SHEET_REF_RE.search(value):
            hits.append(match.group(0))
    return hits


# 上面那条按"插值形态"匹配，抓不到**字符串拼接**的写法：
#   var clickHandler = isClickable ? 'onclick="switchExcelSheet(\'' + sheetName + '\')"' : '';
# 这种写法同样把表名放进了代码位置，所以再补一条行级兜底规则：
# 同一行里既出现内联处理器属性、又出现表名标识符，就算命中。
_INLINE_HANDLER_TOKEN_RE = re.compile(r"\bon[a-z]+\s*=", re.I)
_SHEET_NAME_IDENT_RE = re.compile(r"\b(?:sheet\.name|sheetName|sheet_name)\b")


def _sheet_name_near_inline_handler(text: str):
    """行级兜底扫描（先剥掉注释，避免"注释里提到 onclick"这种误报）。"""
    hits = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"//.*$", "", raw)
        line = re.sub(r"/\*.*?\*/", "", line)
        if _INLINE_HANDLER_TOKEN_RE.search(line) and _SHEET_NAME_IDENT_RE.search(line):
            hits.append((line_no, raw.strip()))
    return hits


def _scanned_sources():
    files = sorted((PROJECT_ROOT / "templates").rglob("*.html"))
    files += sorted((PROJECT_ROOT / "static" / "js").rglob("*.js"))
    return files


def test_the_scanner_can_actually_fail():
    """扫描本身要能命中：用旧写法当样本，扫描器在它身上必须报出来。

    没有这条，正则写歪了（比如引号处理错了）会让下面那条在整仓上"通过" ——
    一个什么都没测的绿。
    """
    old_style = [
        """onclick="showExcelSheet('{{ sheet_name }}')" """,
        """const c = sheet.hasChanges ? `onclick="switchExcelSheet('${sheet.name}')"` : '';""",
        """const c = sheet.hasChanges ? `onclick="switchMergedExcelSheet('${sheet.name}')"` : '';""",
        """html += `<div onclick="switchExcelSheetInContainer('${sheetName}', '${containerId}')">`;""",
        """onclick="window.switchWeeklyExcelSheet('${sheet.name}')" """,
    ]
    for sample in old_style:
        assert _sheet_name_inline_handlers(sample), (
            f"扫描器漏掉了这种旧写法，说明它守不住：{sample}"
        )

    # 反向：正常的（表名只进 data-* / textContent）不该被误报
    fine = [
        """tab.setAttribute('data-sheet', sheet.name);""",
        """const active = `<div class="t" data-sheet="${escapeHtml(sheet.name)}">`;""",
        """tabs[i].dataset.sheet === sheetName""",
    ]
    for sample in fine:
        assert _sheet_name_inline_handlers(sample) == [], f"误报：{sample}"


def test_no_source_puts_a_sheet_name_into_an_inline_handler():
    """全仓（templates/ + static/js/）不得再把表名拼进内联事件处理器。

    表名的来源只有一个：被审核的 Excel 文件。它能进
    `onclick="f('${sheet.name}')"` 就一定能逃出字符串 ——
    属性值是先被 HTML 解码、再交给 JS 引擎编译的。
    """
    hits = []
    for path in _scanned_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(PROJECT_ROOT)
        for found in _sheet_name_inline_handlers(text):
            hits.append(f"{rel}: {found[:110]}")
        for line_no, line in _sheet_name_near_inline_handler(text):
            hits.append(f"{rel}:{line_no}: {line[:110]}")

    assert hits == [], "仍有文件把表名拼进内联事件处理器：\n" + "\n".join(hits)


def test_weekly_full_diff_tabs_bind_listeners_instead_of_inline_onclick():
    """周版本完整 diff 页的 Excel 标签 —— 同一处缺陷的第四个出口。

    它是 `render_excel_diff_html()` 产出的合并 Excel diff 的宿主页面，
    标签同样由表名生成。契约：data-sheet 里转义、文本转义、点击走 addEventListener。
    """
    source = (
        PROJECT_ROOT / "templates" / "weekly_version_full_diff.html"
    ).read_text(encoding="utf-8")

    assert "onclick=\"window.switchWeeklyExcelSheet" not in source, (
        "表名仍然被拼进 switchWeeklyExcelSheet 的内联 onclick"
    )
    assert 'data-sheet="${escapeHtml(sheet.name)}"' in source, (
        "data-sheet 没有转义表名（`\"` 能逃出属性）"
    )
    assert "${escapeHtml(sheet.name)}" in source, "标签文本没有转义表名"
    assert "addEventListener('click'" in source, "标签点击没有改成事件绑定"
    assert "tab.getAttribute('data-sheet')" in source, (
        "点击回调没有从 data-sheet 读表名 —— 交互会失效"
    )
    # 无变更的标签仍不可点击
    assert "excel-tab-disabled" in source
