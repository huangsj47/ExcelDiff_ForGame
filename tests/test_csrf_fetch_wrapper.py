# -*- coding: utf-8 -*-
"""base.html 里的 CSRF 请求包装器：写请求必须**真的**带上 token。

## 为什么值得单独钉住

平台所有非 GET 请求都要过 CSRF 校验（`services/app_security_bootstrap_service.py::enforce_csrf`），
而前端**只有一处**负责自动补 token：`templates/base.html` 里的那个 IIFE ——
它包装 `window.fetch` 与 `jQuery.ajaxSetup`，并给所有 `<form>` 塞隐藏字段。

这件事的失败方式非常安静：包装器没覆盖到的调用**不会报错**，只是请求少了
`X-CSRF-Token`，服务端照常返回 400「CSRF token invalid or missing.」。
线上就是这么报出来的：用户在页面上点「创建周版本」，弹窗写
`创建失败：CSRF token invalid or missing.`，刷新一下又好了 ——
看代码完全看不出问题，必须真的跑一遍那段 JS 才知道 token 到底有没有被加上。

## 这个测试断言什么（每条都是一个真实会踩的形态）

| 场景 | 期望 |
|---|---|
| `fetch(url, {method:'POST'})` | 补上 token |
| `fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}})` | 补上 token，且**不覆盖**既有请求头 |
| `fetch(new Request(url, {method:'POST'}))` | 补上 token（方法写在 `Request` 对象里） |
| `fetch(url)`（GET） | **不**补（GET 本来就不校验，补了反而多一个头） |
| `fetch('http://别的域/…', {method:'POST'})` | **不**补（跨域请求不该泄露 token） |
| 已显式带 `X-CSRF-Token` | 保留调用方给的值，不被覆盖 |
| `$.ajax({type:'POST'})` | `beforeSend` 里补上 token |
| `<form method="post">` 原生提交 | 追加隐藏字段 `_csrf_token` |

## 为什么要真的跑 JS

断言源码里存在 `headers.set('X-CSRF-Token'` 是没有意义的：
方法名大小写、`Request` 对象、跨域分支、`init` 与 `input` 谁优先 ——
每一条都只在**运行时**才决定结果。所以这里用 Node 直接执行 base.html 里
那段真实源码（配极简 DOM 桩），把每个场景最终落到请求上的头**逐条读出来**。

没有 Node 的环境自动跳过（仓库是纯 pytest 项目，不该为此新增运行时依赖）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_TEMPLATE = REPO_ROOT / "templates" / "base.html"

_IIFE_RE = re.compile(r"\n    \(function\(\) \{\n(.*?)\n    \}\)\(\);\n", re.S)

_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const token = process.argv[3];
const PLATFORM = 'http://platform.example';

const forms = [];
const handlers = {};
function makeForm(method) {
    const children = [];
    return {
        method: method,
        children: children,
        querySelector: function () { return null; },
        appendChild: function (el) { children.push(el); },
    };
}

globalThis.document = {
    querySelector: function (sel) {
        return sel === 'meta[name="csrf-token"]' ? { getAttribute: function () { return token; } } : null;
    },
    querySelectorAll: function () { return forms; },
    createElement: function () { return { type: '', name: '', value: '' }; },
    addEventListener: function (ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); },
};

const fetchCalls = [];
globalThis.window = {
    location: { origin: PLATFORM },
    fetch: function (input, init) { fetchCalls.push({ input: input, init: init }); return Promise.resolve({}); },
};

let ajaxSetupOptions = null;
globalThis.jQuery = { ajaxSetup: function (opts) { ajaxSetupOptions = opts; } };
globalThis.window.jQuery = globalThis.jQuery;

globalThis.location = globalThis.window.location;

vm.runInThisContext(source);

function headersOf(headers) {
    if (!headers) { return null; }
    if (typeof headers.entries === 'function') {
        return Object.fromEntries(Array.from(headers.entries()).map(function (e) { return [e[0].toLowerCase(), e[1]]; }));
    }
    const out = {};
    Object.keys(headers).forEach(function (k) { out[k.toLowerCase()] = headers[k]; });
    return out;
}

function runFetch(label, fn) {
    const before = fetchCalls.length;
    fn();
    const call = fetchCalls[before];
    let method = '';
    if (call.init && call.init.method) { method = call.init.method; }
    else if (call.input && typeof call.input === 'object' && call.input.method) { method = call.input.method; }
    return { label: label, method: method, headers: headersOf(call.init && call.init.headers) };
}

const results = {};

results.fetch_plain_post = runFetch('plain', function () {
    globalThis.window.fetch(PLATFORM + '/api/x', { method: 'POST' });
});

results.fetch_json_post = runFetch('json', function () {
    globalThis.window.fetch(PLATFORM + '/api/x', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
    });
});

results.fetch_request_object = runFetch('request-object', function () {
    globalThis.window.fetch(new Request(PLATFORM + '/api/x', { method: 'POST' }));
});

results.fetch_get = runFetch('get', function () {
    globalThis.window.fetch(PLATFORM + '/api/x');
});

results.fetch_cross_origin = runFetch('cross-origin', function () {
    globalThis.window.fetch('http://evil.example/api/x', { method: 'POST' });
});

results.fetch_explicit_token = runFetch('explicit', function () {
    globalThis.window.fetch(PLATFORM + '/api/x', {
        method: 'POST',
        headers: { 'X-CSRF-Token': 'caller-supplied' },
    });
});

(function () {
    const seen = {};
    const xhr = { setRequestHeader: function (k, v) { seen[k.toLowerCase()] = v; } };
    if (ajaxSetupOptions && typeof ajaxSetupOptions.beforeSend === 'function') {
        ajaxSetupOptions.beforeSend(xhr, { type: 'POST', url: PLATFORM + '/api/x' });
    }
    results.jquery_post = { label: 'jquery', headers: seen, hasSetup: !!ajaxSetupOptions };
})();

(function () {
    forms.length = 0;
    const form = makeForm('post');
    forms.push(form);
    (handlers['DOMContentLoaded'] || []).forEach(function (fn) { fn(); });
    const input = form.children[0] || null;
    results.form_native_post = {
        label: 'form',
        input: input ? { name: input.name, value: input.value, type: input.type } : null,
    };
})();

process.stdout.write(JSON.stringify(results));
"""


def _wrapper_source() -> str:
    """取出 base.html 里那段 CSRF 包装器 IIFE（文件里第一段 IIFE）。"""
    source = BASE_TEMPLATE.read_text(encoding="utf-8")
    match = _IIFE_RE.search(source)
    assert match, (
        "templates/base.html 里找不到 CSRF 请求包装器（第一段 IIFE）——"
        "实现被改写或删除了，本测试的前提出错。"
    )
    body = match.group(1)
    assert "X-CSRF-Token" in body, "取到的 IIFE 不是 CSRF 包装器"
    return "(function() {\n" + body + "\n})();\n"


@pytest.fixture(scope="module")
def report() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实 JS 断言（源码断言仍在其它用例里）")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "wrapper.js"
        script.write_text(_wrapper_source(), encoding="utf-8")
        driver = Path(tmp) / "driver.js"
        driver.write_text(_DRIVER, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(driver), str(script), "unit-test-token"],
            capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, f"Node 执行包装器失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


class TestFetchWrapperAddsTheToken:
    def test_plain_post_gets_the_token(self, report):
        headers = report["fetch_plain_post"]["headers"]
        assert headers and headers.get("x-csrf-token") == "unit-test-token", (
            f"裸 fetch POST 没有补上 X-CSRF-Token：{headers}\n"
            "服务端会对它回 400「CSRF token invalid or missing.」。"
        )

    def test_json_post_gets_the_token_without_losing_content_type(self, report):
        headers = report["fetch_json_post"]["headers"]
        assert headers.get("x-csrf-token") == "unit-test-token", (
            f"带 Content-Type 的 fetch POST 没有补上 token：{headers}"
        )
        assert headers.get("content-type") == "application/json", (
            f"包装器把调用方自己的请求头弄丢了：{headers}"
        )

    def test_request_object_post_gets_the_token(self, report):
        """方法写在 `Request` 对象里时也必须补上。

        `fetch(new Request(url, {method:'POST'}))` 是合法且常见的写法。
        包装器若只看第二个参数（`init`）的方法，这里会被当成 GET →
        不补 token → 写请求 400。这与「包装器没生效」是同一类故障。
        """
        headers = report["fetch_request_object"]["headers"]
        assert headers and headers.get("x-csrf-token") == "unit-test-token", (
            f"fetch(new Request(url, {{method: 'POST'}})) 没有补上 X-CSRF-Token：{headers}\n"
            "方法写在 Request 对象里，包装器必须也从那里读；"
            "漏掉它时请求会被服务端回 400「CSRF token invalid or missing.」。"
        )

    def test_get_is_left_alone(self, report):
        headers = report["fetch_get"]["headers"]
        assert not headers or "x-csrf-token" not in headers, (
            "GET 请求被加上了 CSRF 头 —— GET 本来就不校验，多余的头发给第三方没有意义"
        )

    def test_cross_origin_post_is_not_given_the_token(self, report):
        headers = report["fetch_cross_origin"]["headers"]
        assert not headers or "x-csrf-token" not in headers, (
            f"跨域请求被塞了 CSRF token：{headers} —— token 会泄露给第三方站点"
        )

    def test_caller_supplied_token_wins(self, report):
        headers = report["fetch_explicit_token"]["headers"]
        assert headers.get("x-csrf-token") == "caller-supplied", (
            f"包装器覆盖了调用方显式给出的 token：{headers}"
        )


class TestJQueryAndFormPaths:
    def test_jquery_ajax_post_gets_the_token(self, report):
        entry = report["jquery_post"]
        assert entry["hasSetup"], "$.ajaxSetup 没有被调用 —— jQuery 分支失效"
        assert entry["headers"].get("x-csrf-token") == "unit-test-token", (
            f"$.ajax POST 没有补上 X-CSRF-Token：{entry['headers']}"
        )

    def test_native_form_post_gets_a_hidden_field(self, report):
        entry = report["form_native_post"]
        assert entry["input"], "原生表单提交没有被追加 _csrf_token 隐藏字段"
        assert entry["input"]["name"] == "_csrf_token"
        assert entry["input"]["value"] == "unit-test-token"
        assert entry["input"]["type"] == "hidden"
