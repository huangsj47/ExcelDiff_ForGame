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
2. 客户端重建表体/标签的那条路：`onclick="switchExcelSheet('${sheet.name}')"`
   拼进字符串再 `innerHTML = ...`。innerHTML 里的 `onclick=` 会被编译成事件处理器，
   效果与 (1) 相同；`data-sheet="${sheet.name}"` 还允许用 `"` 直接逃出属性。
   这一段原先落在 `static/js/diff-handlers.js` 的 `generateExcelTabs()` /
   `showExcelSheetInContainer()` / `generateExcelContent()` 上，2026 全删了，
   但**删的理由不同**：后两个属于整条跑不到的客户端表体渲染链（它找的容器
   `#excel-content` 在唯一的入口页 `templates/commit_diff_new.html` 上不存在，
   那页的容器是 partial 渲染的 `#excel-content-area`）；`generateExcelTabs` 那一组
   则是**跑起来会坏事** —— 它把 partial 服务端渲染好的标签删掉重建，再按
   `sheet-content-${name}` 找内容（partial 用的是 `sheet-${name}`），于是那一页
   所有 `.excel-sheet-content` 的 `active` 被摘掉且没有加回来的，而它是
   `display: none`：整块 Excel 表体不显示。两处都在
   `static/js/diff-handlers.js` 的文件头写明了。
   现在标签由 partial 服务端渲染、表体只有一份实现 `static/js/excel_diff_table.js`，
   所以下面 A/B 两段跑 partial 的真实渲染结果 + 真实内联脚本，D/E/F 三段跑
   **那个模块**的真入口 —— 断言一条没减，防的还是同一件事。

## 为什么断言"生成的 DOM 里没有 on* 属性"

这比断言"注入串没出现在 HTML 里"更接近根因：问题的本质不是某个字符串长得可疑，
而是**不可信数据被放进了代码位置**。同一份数据放在文本节点或 `data-*` 里是安全的
（属性值仍会被 HTML 解码，但解码后只参与取值，不参与编译），所以下面同时断言
"表名原样出现在文本/属性里"——防止有人用"把表名整个删掉"的方式让测试变绿。

## 两层各自防什么

* Python 侧渲染真实的 Jinja 模板 —— 覆盖服务端渲染路径（commit_diff_new 首屏）。
* Node 侧把 `static/js/diff-handlers.js`、`static/js/excel_diff_table.js` 与
  **partial 真实渲染出来的标签 HTML + 它自带的真实内联脚本**分别放进隔离 vm 跑
  （只伪造最小的 DOM 骨架，不启浏览器、不联网）—— 覆盖「点击标签切换工作表」
  与「JS 渲染表体」这两条只有跑起来才看得见的路径。
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
# 表体渲染的唯一实现（表头 / 行 / 高亮 / 转义都在这里）
EXCEL_TABLE_MODULE = PROJECT_ROOT / "static" / "js" / "excel_diff_table.js"

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
//
// 骨架不建文本节点，但「表名作为**文本**渲染出来了」也是一条要断言的事
// （表名只留在属性里、标签显示成空白，同样是坏的），所以每个元素额外记一份
// `textAfter`：这个开始标签与下一个标签之间的文本（按浏览器规则解码后）。
function parseHtmlFragment(html) {
  const nodes = [];
  const tagRe = /<([a-zA-Z][^\s/>]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g;
  const matches = [];
  let match;
  while ((match = tagRe.exec(html)) !== null) {
    matches.push({ start: match.index, end: tagRe.lastIndex, tag: match[1], attrBlob: match[2] });
  }
  matches.forEach((found, index) => {
    const el = new Element(found.tag);
    const attrBlob = found.attrBlob;
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
    const stop = index + 1 < matches.length ? matches[index + 1].start : html.length;
    el.textAfter = decodeEntities(html.slice(found.end, stop));
    nodes.push(el);
  });
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

// ---------- 表体渲染的唯一实现 ----------
// D/E/F 三段原先跑的是 diff-handlers.js 的 showExcelSheetInContainer /
// generateExcelContent。那两个函数属于该文件里**整条跑不到**的客户端 Excel 表体
// 渲染链（generateExcelContent 找的 `#excel-content` 在唯一的调用页
// templates/commit_diff_new.html 上不存在，那页的容器是 partial 渲染的
// `#excel-content-area`），2026 已删除。表体现在只有一份实现：
// static/js/excel_diff_table.js。于是这三段改成加载那个模块、跑它的真入口 ——
// 断言（没有 on* 属性、没有真标签被插进 DOM）一条没减，防的还是同一件事。
const tableSource = fs.readFileSync(process.argv[3], 'utf8');
const tableSandbox = {
  document,
  console: { log() {}, warn() {}, error() {} },
  window: {},
};
tableSandbox.window = tableSandbox;
// 展示口径由页面提供，模块自己不实现它（见该模块文件头的第 1 条契约）。
tableSandbox.window.formatCellValue = function (value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number' && isNaN(value)) return '';
  return String(value);
};
vm.createContext(tableSandbox);
vm.runInContext(tableSource, tableSandbox, { filename: 'excel_diff_table.js' });
const ExcelDiffTable = tableSandbox.window.ExcelDiffTable;

// ---------- 被测数据的构造 ----------
const INJECTION = "a');window.__qa=1;//";
const TAG_INJECTION = '"><img src=x onerror="window.__qa=1">';

// partial（diff_partials/excel_diff.html）**真实渲染出来的** HTML 与它自带的
// 那段内联脚本。两者都由 Python 侧渲染真实 Jinja 模板后传进来 —— 这一段跑的是
// 页面上真正会跑的那份标签逻辑，不是复刻。
const PARTIAL_HTML = __PARTIAL_HTML__;
const PARTIAL_SCRIPT = __PARTIAL_SCRIPT__;

function resetDom() {
  ALL_ELEMENTS.length = 0;
  Object.keys(byId).forEach((k) => delete byId[k]);
}

// 从一棵（极简解析出来的）DOM 子树里收下所有元素。
function collectDescendants(root) {
  const out = [];
  const walk = (nodes) => {
    nodes.forEach((n) => {
      out.push(n);
      if (n.childNodes) walk(n.childNodes);
    });
  };
  walk((root && root.childNodes) || []);
  return out;
}

// 用 partial 的真实渲染结果建 DOM。innerHTML 的解析规则与浏览器一致
// （引号内的 `"` 不结束属性值），属性值也按浏览器规则解码 —— 所以下面看到的
// 就是浏览器会看到的东西。带 id 的元素要登记进 byId：骨架里的 getElementById
// 只认那张表。
function mountPartialHtml() {
  resetDom();
  const page = document.createElement('div');
  page.innerHTML = PARTIAL_HTML;
  byId['__page'] = page;
  collectDescendants(page).concat([page]).forEach((el) => {
    const id = el.getAttribute && el.getAttribute('id');
    if (id) byId[id] = el;
  });
  return page;
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

// --- A. partial 渲染出的标签：没有 on* 属性 / 表名原样落地 ---
// 这一段原先跑 diff-handlers.js 的 generateExcelTabs（那段把服务端渲染好的标签
// **删掉再重建**的客户端逻辑）。那一组函数连同这个缺陷一起删除了（见
// static/js/diff-handlers.js 文件头）：现在标签由 diff_partials/excel_diff.html
// 服务端渲染，切换由它自带的内联脚本绑定。断言没减，落点换成唯一在渲染标签的这份。
mountPartialHtml();
const partialSandbox = {
  document,
  console: { log() {}, warn() {}, error() {} },
  window: {},
};
partialSandbox.window = partialSandbox;
vm.createContext(partialSandbox);
vm.runInContext(PARTIAL_SCRIPT, partialSandbox, { filename: 'excel_diff_partial_inline.js' });

const partialTabs = ALL_ELEMENTS.filter((el) => el.classList.contains('excel-sheet-tab'));
report.tabs = {
  tabCount: partialTabs.length,
  tags: partialTabs.map((el) => el.tagName),
  names: partialTabs.map((el) => el.getAttribute('data-sheet')),
  // 标签的可见文本 = 开始标签与它第一个子标签之间的那段（见 parseHtmlFragment）
  texts: partialTabs.map((el) => el.textAfter || ''),
  classes: partialTabs.map((el) => el._classes()),
  listenerCounts: partialTabs.map((el) => (el.listeners['click'] || []).length),
  handlerAttrs: collectHandlerAttrs(ALL_ELEMENTS),
};

// --- B. 点击标签：用**原样的**表名切换，且表名里的引号不能把切换弄坏 ---
// 覆盖两种不可信表名：带 `'` 的（内联处理器时代能闭合字符串）与带 `"` 的
// （能逃出属性）。两者都必须能正确切换 —— 不能靠「把可疑名字删掉」通过。
function clickReport(clickedName) {
  const tabs = ALL_ELEMENTS.filter((el) => el.classList.contains('excel-sheet-tab'));
  const target = tabs.filter((el) => el.getAttribute('data-sheet') === clickedName)[0];
  let threw = null;
  try {
    if (target) target.dispatch('click');
  } catch (err) {
    threw = String(err && err.message ? err.message : err);
  }
  const contents = ALL_ELEMENTS.filter((el) => el.classList.contains('excel-sheet-content'));
  const activeContents = contents.filter((el) => el.classList.contains('active'));
  return {
    threw: threw,
    targetFound: !!target,
    expectedContentId: 'sheet-' + clickedName,
    targetActive: target ? target.classList.contains('active') : null,
    targetAriaCurrent: target ? target.getAttribute('aria-current') : null,
    activeContentCount: activeContents.length,
    activeContentId: activeContents.length ? activeContents[0].getAttribute('id') : null,
    othersStillActive: tabs.filter((el) => el !== target && el.classList.contains('active')).length,
  };
}

mountPartialHtml();
const clickSandbox = {
  document,
  console: { log() {}, warn() {}, error() {} },
  window: {},
};
clickSandbox.window = clickSandbox;
vm.createContext(clickSandbox);
vm.runInContext(PARTIAL_SCRIPT, clickSandbox, { filename: 'excel_diff_partial_inline.js' });
report.tabClick = {
  quote: clickReport(INJECTION),
  angle: clickReport(TAG_INJECTION),
};

// --- D. 容器路径：合并页 showExcelSheetInContainer 真正走的那一句 ---
// 这一句在 templates/merge_diff.html 里（归一化行格式之后调共享实现），
// 它是"同一处缺陷的第二个出口"，只是实现换成了共享模块。
{
  resetDom();
  const container = document.createElement('div');
  container.setAttribute('id', 'excel-container-1');
  byId['excel-container-1'] = container;
  ExcelDiffTable.mountSheetTable(container, {
    name: INJECTION,
    headers: ['id', TAG_INJECTION],
    rows: [{
      row_number: 2,
      status: 'added',
      data: { id: '1', [TAG_INJECTION]: TAG_INJECTION },
    }],
  }, { sheetName: INJECTION });
  const descendants = collectDescendants(container);
  report.container = {
    handlerAttrs: collectHandlerAttrs(descendants),
    htmlLeakedScript: /<img/i.test(container._innerHTML),
    childCount: descendants.length,
    // 非空转保险：表头行真的渲染出来了，上面两条断言才算数
    headerRowRendered: container._innerHTML.indexOf('excel-column-header') !== -1,
  };
}

// --- E. 表头（同样来自 Excel 文件）进入 title= 与文本 ---
{
  resetDom();
  const container = document.createElement('div');
  container.setAttribute('id', 'excel-headers');
  byId['excel-headers'] = container;
  const sheetData = {
    headers: [TAG_INJECTION],
    // 这一格必须有**内容**：默认开着「隐藏本页空列」，整列全空的话这一列根本不会
    // 渲染出来，下面那两条断言就变成空转（不是「没漏」，是「没渲染」）。
    rows: [{ row_number: 2, status: 'added', data: { [TAG_INJECTION]: TAG_INJECTION } }],
  };
  ExcelDiffTable.mountSheetTable(container, sheetData, {});
  const descendants = collectDescendants(container);
  report.headers = {
    handlerAttrs: collectHandlerAttrs(descendants),
    htmlLeakedTag: /<img/i.test(container._innerHTML),
    headerRowRendered: container._innerHTML.indexOf('excel-column-header') !== -1,
  };
}

// --- F. 单元格值（同样是 Excel 内容）里的 {key,value} 参数对 ---
{
  resetDom();
  const container = document.createElement('div');
  container.setAttribute('id', 'excel-cells');
  byId['excel-cells'] = container;
  const CELL_INJECTION = '{key,<img src=x onerror="window.__qa=1">}';
  const sheetData = {
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
  ExcelDiffTable.mountSheetTable(container, sheetData, {});
  const descendants = collectDescendants(container);
  report.cellValues = {
    handlerAttrs: collectHandlerAttrs(descendants),
    htmlLeakedTag: /<img/i.test(container._innerHTML),
    // 同样是非空转保险：这一格真的进了表体（改前/改后两行都在）
    modifiedCellsRendered: /excel-modified-(old|new)/.test(container._innerHTML),
  };
}

process.stdout.write(JSON.stringify(report));
"""


def _node_binary() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过 JS 层验证")
    return node


def _partial_script(html: str) -> str:
    """抠出 partial 里那段内联 <script> 的内容。

    标签的点击绑定就在那段脚本里（`bindExcelSheetTabs` 的 IIFE），跑它才是跑真实现。
    """
    blocks = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I)
    assert blocks, f"{PARTIAL} 里没有内联脚本 —— 标签的点击绑定去哪了？"
    return "\n".join(blocks)


# 喂给 harness 的多工作表载荷：两种不可信表名各一张，外加一张正常表
# （正常表用来确认「不是只认得恶意名字」）。
HARNESS_SHEET_NAMES = ("正常表", INJECTION, TAG_INJECTION)


def _run_harness() -> dict:
    """把真实 diff-handlers.js、真实 excel_diff_table.js 与 partial 的**真实渲染
    结果 + 真实内联脚本**放进隔离 vm 跑，拿回结构化结果。"""
    partial_html = _render_partial(_multi_sheet_payload(list(HARNESS_SHEET_NAMES)))
    script = (_NODE_HARNESS
              .replace("__PARTIAL_HTML__", json.dumps(partial_html))
              .replace("__PARTIAL_SCRIPT__", json.dumps(_partial_script(partial_html))))
    proc = subprocess.run(
        [_node_binary(), "-", str(DIFF_HANDLERS), str(EXCEL_TABLE_MODULE)],
        input=script,
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
    """渲染出来的标签里不能有 on* 属性 —— innerHTML 会把它们编译成可执行处理器。

    落点原先是被测的那段 JS 建出来的标签；那段 JS（diff-handlers.js 的
    `generateExcelTabs`）因「把服务端标签删掉重建、并让表体整块不显示」已删除，
    现在标签由 `diff_partials/excel_diff.html` 服务端渲染，本断言改钉它。
    """
    data = js_report["tabs"]
    assert data["tabCount"] > 0, (
        f"partial 一个标签都没渲染出来，下面两条断言会空转：{data}"
    )
    assert data["handlerAttrs"] == [], (
        "标签被拼进了内联事件处理器，点击即执行注入的脚本："
        f"{data['handlerAttrs']}"
    )


def test_js_tabs_keep_the_sheet_name_verbatim(js_report):
    """表名必须原样落在 data-sheet / 文本上，切换才能工作（不能靠删数据通过）。

    文本这一半按**子串**判：标签的可见文本除了表名还可能有「变更」徽章的文字。
    """
    data = js_report["tabs"]
    assert INJECTION in data["names"], (
        f"data-sheet 丢了恶意表名（不能靠删数据通过）：{data['names']}"
    )
    assert TAG_INJECTION in data["names"], f"data-sheet 丢了带引号的表名：{data['names']}"
    texts = data["texts"]
    assert any(INJECTION in t for t in texts), f"标签文本丢了表名：{texts}"
    assert any(TAG_INJECTION in t for t in texts), f"标签文本丢了带引号的表名：{texts}"


def test_js_tabs_all_carry_a_click_handler(js_report):
    """标签是真 `<button>`、每个都绑了点击处理器，且不带 `excel-tab-disabled`。

    这条原先钉的是「无变更的标签带 excel-tab-disabled、且不绑处理器」——
    那套「按变更状态排序 + 禁用」的规则是已删除的客户端 `generateExcelTabs`
    的产物，partial 的服务端渲染没有它（有没有变更是用「变更」徽章表达的，
    标签一律可点）。于是断言换成这份实现的等价契约：**标签无一遗漏地可点**，
    漏一个就有一张工作表点不开，而那正是原来那套规则的另一半要防的事。
    顺带钉住 `<button>`：客户端重建标签的写法把服务端的 `<button>` 换成了 `<div>`，
    键盘/读屏语义一起丢了 —— 这条拦住「再有人用 JS 重建一遍」。
    """
    data = js_report["tabs"]
    assert data["classes"], f"没有标签，断言会空转：{data}"
    for tag, classes, count in zip(data["tags"], data["classes"], data["listenerCounts"]):
        assert tag == "BUTTON", (
            f"标签不再是服务端渲染的 <button>，而是 <{tag}>（键盘/读屏语义会丢）：{classes}"
        )
        assert "excel-tab-disabled" not in classes, (
            f"标签带上了 excel-tab-disabled（这一页的标签应当一律可点）：{classes}"
        )
        assert count == 1, (
            f"标签的点击处理器数量是 {count}，应为 1 —— 少了就点不开：{classes}"
        )


def test_js_clicking_a_tab_switches_sheets(js_report):
    """交互保持：点击标签 → active 转移 + 对应的工作表内容显示出来。

    两种不可信表名各点一次：带 `'` 的（内联处理器时代能闭合字符串）与带 `"`
    的（能逃出属性）。两者都必须能正确切换出**它自己**那张表。
    """
    for label, data in (("单引号", js_report["tabClick"]["quote"]),
                        ("双引号", js_report["tabClick"]["angle"])):
        assert data["targetFound"], f"{label}表名的标签没渲染出来，这一段会空转：{data}"
        assert data["threw"] is None, (
            f"{label}表名让切换抛异常：{data['threw']}"
        )
        assert data["targetActive"] is True, (
            f"{label}表名：点击后标签没有变成 active"
        )
        assert data["othersStillActive"] == 0, (
            f"{label}表名：点击后其它标签仍然是 active：{data}"
        )
        assert data["activeContentCount"] == 1, (
            f"{label}表名：点击后应当正好有一块工作表内容显示，实际 "
            f"{data['activeContentCount']} 块（表体是 display:none，一块都没有 = 整块不显示）"
        )
        assert data["activeContentId"] == data["expectedContentId"], (
            f"{label}表名：显示出来的不是它自己那张工作表 —— "
            f"拿到的是 {data['activeContentId']!r}，应当是 {data['expectedContentId']!r}"
        )
        assert data["targetAriaCurrent"] == "true", (
            f"{label}表名：aria-current 语义丢了：{data}"
        )


def test_js_container_tabs_have_no_inline_handler(js_report):
    """容器路径是同一处缺陷的第二个出口（合并 diff / 周版本页）。

    这一段原先跑 diff-handlers.js 的 `showExcelSheetInContainer`；那个函数随整条
    跑不到的客户端表体渲染链删除了，现在跑的是合并页真正调的那一句
    （`ExcelDiffTable.mountSheetTable`）。断言没变：**渲染出来的 DOM 里不许有 on*
    属性、不许有注入标签**。
    """
    data = js_report["container"]
    assert data["childCount"] > 0 and data["headerRowRendered"], (
        f"容器路径什么都没渲染出来，下面两条断言会变成空转：{data}"
    )
    assert data["handlerAttrs"] == [], (
        f"容器渲染把不可信文本拼成了内联处理器：{data['handlerAttrs']}"
    )
    assert not data["htmlLeakedScript"], "容器 HTML 里出现了注入标签"


def test_js_table_headers_do_not_break_out_of_attributes(js_report):
    """表头（Excel 第一行）同样不可信：不能逃出 title="..." 或插成新标签。

    这一段原先跑 diff-handlers.js 的 `generateExcelContent`（已随那条跑不到的渲染链
    删除），现在跑表体渲染的唯一实现 `static/js/excel_diff_table.js`；
    `title="` 那一处转义就在它的 `tableHeadRowHtml` 里。
    """
    data = js_report["headers"]
    assert data["headerRowRendered"], (
        f"表头行没渲染出来（多半是被「隐藏本页空列」整列去掉了），断言会空转：{data}"
    )
    assert data["handlerAttrs"] == [], (
        f"表头把 on* 顶成了新属性：{data['handlerAttrs']}"
    )
    assert not data["htmlLeakedTag"], "表头里的 `<img ...>` 被当成标签插进了表格"


def test_js_cell_values_in_parameter_lists_are_escaped(js_report):
    """单元格值里的 `{key,<img onerror>}` 走的是"参数对高亮"分支 ——
    该分支原先明确写着"不转义"，把键值直接拼进 innerHTML。

    落点同上换成了 `static/js/excel_diff_table.js`（高亮只有那一份实现）。
    """
    data = js_report["cellValues"]
    assert data["modifiedCellsRendered"], (
        f"修改行的改前/改后两格没渲染出来，下面两条断言会空转：{data}"
    )
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
