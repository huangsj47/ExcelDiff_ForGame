# -*- coding: utf-8 -*-
"""消耗面板：「活动任务」那句提示的文案与渲染（KPI 上方那一条）。

## 这个文件守的四条性质

1. **KPI 读的是完成统计。** 页面必须拿 `completed_totals` 去渲染 KPI 与总览 ——
   拿回 `body.totals` 里那份「全部运行」的数字，就等于把修好的东西原样改回去。
2. **提示在 KPI 上方，且没有在途任务时整条不渲染。** 「现在没有任务在跑」不是一条
   需要常驻的信息；常驻的恒真提示只会让人以后不再读它。
3. **那句话说全了三件事**：有几个在跑、这些数字截至哪一次、以及临时值**不计入**统计。
   少任何一件，读的人都会把「还没结账」当成「已经花了这么多」。
4. **运行列表里那一条照旧诚实显示「未上报」。** 在途的运行不许为了「好看」被补成 0。

静态断言只看得见字符串，所以这里同时用 **node 真跑**模板里那段脚本（仓库既有做法）：
`aiuActiveNoteText` 是纯函数，喂进去什么就断言输出什么。

**注释必须先剥掉**：这段脚本的注释里原样写着要禁掉的写法（「不是 0」「不计入」这些
句子本身就在注释里），不剥就会假红/假绿（见 `tests/test_ai_usage_budget_ui.py` 的说明）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = "templates/ai_usage_dashboard.html"


def _read(rel: str) -> str:
    return (PROJECT_ROOT / rel).read_text(encoding="utf-8")


def _strip_js_comments(code: str) -> str:
    """剥掉 JS 的行注释与块注释，**字符串字面量里的 `//` 不动**。

    与 `tests/test_ai_usage_budget_ui.py` 同一套（那边还带 CSS 版）：`'…//…'` 里
    的斜杠不能被当成注释起点，否则后面的断言会搜不到东西。
    """
    out: list[str] = []
    index, length = 0, len(code)
    quote = ""
    while index < length:
        char = code[index]
        if quote:
            out.append(char)
            if char == "\\" and quote != "`":
                if index + 1 < length:
                    out.append(code[index + 1])
                    index += 2
                    continue
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'`":
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and code[index + 1] == "/":
            while index < length and code[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and code[index + 1] == "*":
            index += 2
            while index + 1 < length and not (code[index] == "*" and code[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _dashboard_script() -> str:
    """模板里那段内联 `<script>`，**注释已经剥掉**。"""
    text = _read(DASHBOARD)
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", text, re.S)
    assert len(blocks) == 1, f"消耗面板应该只有一段内联脚本，实际 {len(blocks)} 段"
    return _strip_js_comments(blocks[0])


def _function_body(script: str, name: str) -> str:
    """取出 `function <name>(...) { … }` 的函数体（按花括号配平）。"""
    start = script.index(f"function {name}(")
    brace = script.index("{", start)
    depth, index = 0, brace
    while index < len(script):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[brace: index + 1]
        index += 1
    raise AssertionError(f"找不到 {name} 的函数体")


def _function_source(script: str, name: str) -> str:
    """`function <name>(…) { … }` 的完整源码（含参数表）—— 能直接喂给 node 求值。"""
    start = script.index(f"function {name}(")
    head = script[start: script.index("{", start)]
    return head + _function_body(script, name)


# ---------------------------------------------------------------------------
#  用 node 真跑模板里那段脚本
# ---------------------------------------------------------------------------

_DOM_STUB = """
function makeElement(tag) {
    const el = {
        tagName: tag, hidden: false, textContent: '', innerHTML: '', className: '',
        value: '', disabled: false, title: '', type: '', colSpan: 0, tabIndex: -1,
        style: {}, dataset: {}, options: [], children: [], parentNode: null,
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, removeEventListener() {}, setAttribute() {},
        getAttribute() { return null; }, removeAttribute() {},
        appendChild(child) { if (child) { child.parentNode = el; el.children.push(child); } return child; },
        removeChild() {}, querySelector() { return null; }, querySelectorAll() { return []; },
        focus() {}, closest() { return null; }, insertBefore() {}, scrollIntoView() {}
    };
    return el;
}
"""

# `aiuActiveNoteText(active, completed)` 的用例：`[活动任务那一块, 完成统计那一块]`。
_NOTE_CASES = [
    # 0：没有在途任务 → 整句是 `null`（页面据它把整条隐藏）。
    [None, None],
    [{"count": 0, "latest_run_id": None, "live": {"tokens": None}}, {"runs": 5}],
    # 2：一条在跑、有一条已完成、进度快照里还没有数字。
    [{"count": 1, "latest_run_id": 15, "live": {"tokens": None, "reported_runs": 0}},
     {"latest_run_id": 14}],
    # 3：多条并行 + 进度快照里已经有临时值。
    [{"count": 2, "latest_run_id": 20, "live": {"tokens": 4500, "reported_runs": 1}},
     {"latest_run_id": 14}],
    # 4：一次都没完成过（新项目的第一次分析还在跑）。
    [{"count": 1, "latest_run_id": 7, "live": {"tokens": 0, "reported_runs": 1}},
     {"latest_run_id": None}],
    # 5：老响应 / 缺字段（没有 live、没有 completed）→ 不许抛，仍要说出「还没有已完成的运行」。
    [{"count": 1}, None],
    # 6：临时值不足 1k 时不带单位换算。
    [{"count": 1, "latest_run_id": 9, "live": {"tokens": 999, "reported_runs": 1}},
     {"latest_run_id": 8}],
    # 7：**任务跑完之后**（同一屏再次刷新）：整条要回到隐藏，文本也要清空 ——
    #    留着一句「当前筛选范围内有 1 个新任务运行中」就是一句过期的话。
    [{"count": 0, "latest_run_id": None, "live": {"tokens": None}},
     {"latest_run_id": 9}],
]


_NOTE_OUT = """function (A, sandbox, makeElement) {
    return {
        typeofs: {
            aiuActiveNoteText: typeof sandbox.aiuActiveNoteText,
            renderActiveNote: typeof sandbox.__probeRenderActiveNote
        },
        texts: A.noteCases.map(function (pair) {
            return sandbox.aiuActiveNoteText(pair[0], pair[1]);
        }),
        // 真调一次 `renderActiveNote`，把 `hidden` 与文本读回来：整句为空时必须**整条不渲染**。
        rendered: A.noteCases.map(function (pair) {
            sandbox.__probeRenderActiveNote('aiuActiveNote', pair[0], pair[1]);
            const host = sandbox.document.getElementById('aiuActiveNote');
            return {hidden: !!host.hidden, text: host.textContent};
        })
    };
}"""


def _run_node(script: str, probe_source: str, out_source: str) -> dict:
    """把模板里那段脚本读进 node 跑起来，按 `out_source` 把要断言的东西取回来。"""
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的纯函数断言")
    driver = _DOM_STUB + """
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const cache = {};
const sandbox = {
    console: console, URLSearchParams: URLSearchParams, setTimeout: setTimeout,
    fetch: function () { return Promise.reject(new Error('offline')); },
    document: {
        getElementById(id) { return cache[id] || (cache[id] = makeElement('div')); },
        createElement(tag) { return makeElement(tag); },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener() {},
        readyState: 'complete'
    },
    window: {
        location: { search: '', pathname: '/ai-analysis/usage', hash: '' },
        history: { replaceState() {} },
        addEventListener() {}
    },
    bootstrap: { Modal: function () { return { show() {}, hide() {} }; } }
};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'ai_usage_dashboard.js' });
const PROBE = __PROBE__;
if (PROBE) vm.runInContext(PROBE, sandbox, { filename: 'probe.js' });

const A = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = (__OUT__)(A, sandbox, makeElement);
process.stdout.write(JSON.stringify(out));
"""
    driver = driver.replace("__PROBE__", json.dumps(probe_source))
    driver = driver.replace("__OUT__", out_source)
    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "ai_usage_dashboard.js"
        script_path.write_text(script, encoding="utf-8")
        cases_path = Path(tmp) / "cases.json"
        cases_path.write_text(
            json.dumps({"noteCases": _NOTE_CASES}, ensure_ascii=False), encoding="utf-8"
        )
        driver_path = Path(tmp) / "driver.js"
        driver_path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(driver_path), str(script_path), str(cases_path)],
            capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def js() -> dict:
    script = _dashboard_script()
    # `renderActiveNote` 在 IIFE 里，外面拿不到 —— 把模板里**逐字那段源码**求值进同一个
    # 上下文，于是能真调它、把 `hidden` 与文本读回来。
    #
    # 求值进全局作用域时它看不见 IIFE 里的 `$`（那是 IIFE 自己的局部函数），所以先按
    # 模板里那一行的写法补一个同名的：**补的是查找方式，不是被测量的行为** ——
    # 这一条测的是「整句为空时 `hidden` 回到真、文本清空」。
    probe = (
        "function $(id) { return document.getElementById(id); };\n"
        "__probeRenderActiveNote = " + _function_source(script, "renderActiveNote") + ";"
    )
    return _run_node(script, probe, _NOTE_OUT)


# ==========================================================================
#  一、整句话（真跑）
# ==========================================================================


class TestTheNoteSentence:
    def test_the_function_is_really_callable(self, js):
        assert js["typeofs"]["aiuActiveNoteText"] == "function"
        assert js["typeofs"]["renderActiveNote"] == "function", "renderActiveNote 没有探测到"

    def test_without_an_active_run_there_is_no_sentence(self, js):
        """没有在途任务 → `null`。这不是「空字符串」：调用方靠它决定整条渲染不渲染。"""
        assert js["texts"][0] is None
        assert js["texts"][1] is None

    def test_the_sentence_says_all_three_things(self, js):
        """有几个在跑 / 这屏算到哪一次 / 临时值不计入 —— 一条都不能少。"""
        text = js["texts"][2]

        assert text == (
            "当前筛选范围内有 1 个新任务运行中，实时用量尚未计入；以下统计截至运行 #14，"
            "任务完成后自动更新。这次运行还没有上报任何用量。"
        ), text

    def test_the_temporary_value_is_labelled_as_partial(self, js):
        text = js["texts"][3]

        assert "当前筛选范围内有 2 个新任务运行中" in text, text
        assert "截至运行 #14" in text, text
        assert "临时值 4.5k token" in text, text
        assert "不计入上面的统计" in text, text
        # 「只含已上报的部分」—— 它是下界，不是结账值。
        assert "只含已上报的部分" in text, text

    def test_a_run_with_no_completed_history_says_so(self, js):
        text = js["texts"][4]

        assert "截至还没有已完成的运行" in text, text
        # `0` token 是「报了且确实是 0」，照样要给数字（不是「未上报」）。
        assert "临时值 0 token" in text, text

    def test_missing_fields_do_not_throw(self, js):
        """老响应 / 缺 `live` / 缺 `completed`：句子仍要说得通，不许抛。"""
        text = js["texts"][5]

        assert "当前筛选范围内有 1 个新任务运行中" in text, text
        assert "截至还没有已完成的运行" in text, text
        assert "还没有上报任何用量" in text, text

    def test_small_values_keep_their_unit(self, js):
        assert "临时值 999 token" in js["texts"][6], js["texts"][6]


# ==========================================================================
#  二、渲染：没有在途任务时整条不渲染
# ==========================================================================


class TestTheNoteIsRenderedOnlyWhenThereIsSomethingToSay:
    def test_it_gets_hidden_again_when_the_task_finishes(self, js):
        """上一条任务结账之后再刷一屏，提示必须**回到隐藏**、文本清空。

        留着一句「当前有 1 个新任务运行中」就是一句过期的话 —— 而它看上去完全正常。
        这一条与上面那条一前一后：`rendered[7]` 是从「正显示着」变回「隐藏」的那一步。
        """
        assert js["rendered"][1]["hidden"] is True
        assert js["rendered"][1]["text"] == ""

        finished = js["rendered"][7]
        assert finished["hidden"] is True, "任务跑完之后提示还挂在页面上"
        assert finished["text"] == "", "隐藏了但文本还留着，读屏/复制都会读到旧内容"

    def test_it_shows_the_sentence_while_something_is_running(self, js):
        shown = js["rendered"][2]

        assert shown["hidden"] is False
        assert shown["text"] == js["texts"][2]


# ==========================================================================
#  三、静态：位置、接线、以及「不许补 0」
# ==========================================================================


class TestTheWiring:
    def _html(self) -> str:
        return _read(DASHBOARD)

    def test_the_note_sits_above_the_kpis(self):
        """位置本身就是口径的一部分：那句话解释的正是**下面**那些数字。"""
        html = self._html()
        note = html.index('id="aiuActiveNote"')

        assert note < html.index('id="aiuKpis"'), "活动任务提示跑到 KPI 下面去了"
        assert note < html.index('id="aiuStatsCard"'), "提示跑到「统计口径」卡片下面去了"
        assert 'role="status"' in html[note - 200: note + 200], "提示条缺 role=status"

    def test_the_kpis_read_the_completed_totals(self):
        """KPI 与总览读 `completed_totals` —— 读回 `body.totals` 就把问题改回去了。

        （两者在服务端**逐字等价**，所以这条断言守的是「读的是哪一个字段」这件事本身：
        哪天有人把「全部运行」重新塞回 `totals`，这里要能红。）
        """
        script = _dashboard_script()
        load = _function_body(script, "loadOverview")

        assert "body.completed_totals" in load, "KPI 没有读完成统计"
        assert load.index("renderActiveNote(") < load.index("renderKpis("), (
            "提示必须在 KPI 之前渲染/摆放：先给数字再给解释，读的人会先被那个偏小的数字误导"
        )

    def test_the_kpi_subtitle_says_the_runs_are_completed(self):
        body = _function_body(_dashboard_script(), "renderKpis")

        assert "次已完成运行采集到用量" in body, "「N / M 次运行」会被读成「一共跑了 M 次」"
        assert "次运行中，未计入" in body, "有在途运行时没有说一句「另有 N 次没算进来」"

    def test_the_running_row_is_never_padded_with_a_zero(self):
        """运行列表里在途那一条：token 读不出来就是「未上报」，不许 `|| 0`。"""
        body = _function_body(_dashboard_script(), "renderRuns")

        assert "tokens.input === null" in body, "运行的 token 那两格缺了 null 守卫"
        assert "numCell(pair)" in body, "「输入 / 命中」那一格不再是 null 感知的 numCell"
        assert "|| 0" not in body, "运行列表里出现了补 0"

    def test_the_drill_uses_the_same_split(self):
        script = _dashboard_script()
        load = _function_body(script, "loadDrill")

        assert "body.completed_totals" in load, "下钻的合计没有读完成统计"
        assert "aiuDrillActiveNote" in load, "下钻没有那条活动任务提示"

    def test_the_project_row_says_how_many_are_still_running(self):
        body = _function_body(_dashboard_script(), "renderProjects")

        assert "running_runs" in body, (
            "项目行只写「N 次运行」而合计只算已完成的那些 —— 不说清就会出现"
            "「这行写 3 次、点开列了 4 条」"
        )

    def test_no_markdown_asterisks_leak_into_the_page(self):
        """这段脚本里**不许出现 `**`**：注释全部剥掉之后还剩下的星号，只可能在字符串里。

        这一页的文案里到处都是 markdown 味的 `**加粗**`（注释里也是），而它们一旦落进
        用户看得见的字符串，屏幕上就是**两个星号**（`textContent` 不认 markdown）。
        实测踩过一次：`'上面的合计按全部**已完成**的运行计算。'` 被原样显示成了
        「全部**已完成**的运行」。这条断言是**剥注释之后**做的，所以注释里的那些
        加粗不会被误伤（见文件头那条纪律）。
        """
        script = _dashboard_script()

        offenders = [line.strip() for line in script.splitlines() if "**" in line]
        assert offenders == [], f"用户可见的文案里混进了 markdown 星号：{offenders}"

    def test_the_run_table_caption_marks_the_completed_only_scope(self):
        """表头说明也不许写 markdown —— 那是 HTML 文本，要用 `<strong>`。"""
        html = self._html()

        assert "汇总数字按全部<strong>已完成</strong>的运行计算" in html
        assert "汇总数字按全部**已完成**的运行计算" not in html


# ==========================================================================
#  四、那句话承诺的「自动更新」必须真的做到
# ==========================================================================
# 提示里写着「任务完成后自动更新」，而这一页原先**不会**自己刷新 —— 那就是一句做不到的
# 承诺（用户盯着一个不含刚跑完那次的数字，还以为页面卡了）。这几条守的是那件事真的接上了，
# 以及「跟新的那一拍不许把整页闪成骨架屏」。


class TestTheAutoRefreshKeepsThatPromise:
    def test_the_timer_only_exists_while_something_is_running(self):
        body = _function_body(_dashboard_script(), "scheduleActiveRefresh")

        assert "armActiveRefresh(surface)" in body
        assert "clearTimeout" in body, "重排时没有清掉上一拍，两个定时器会同时在跑"

    def test_the_two_surfaces_keep_their_own_timer(self):
        """总览与下钻**各排各的**：两者的在途条数本来就可能不是同一个数。

        共用一个槽位的话，总览取回「0 个在跑」会把下钻刚排上的那一拍清掉 ——
        而下钻还挂着一句「有 1 个运行中」，那句话就再也不会自己更新了。
        """
        script = _dashboard_script()
        arm = _function_body(script, "armActiveRefresh")

        assert "activeRefresh[surface" in arm, "定时器只有一份（或按错了键）"
        assert "'overview'" in _function_body(script, "loadOverview")
        assert "'drill'" in _function_body(script, "loadDrill")

    def test_each_tick_reloads_quietly(self):
        """跟新走 `loadOverview(true)`：不闪骨架屏、不弹错误、不动筛选表单。"""
        body = _function_body(_dashboard_script(), "runActiveRefresh")

        assert "loadOverview(true)" in body, "后台跟新必须走 quiet 那条路"
        assert "loadDrill(" in body, "下钻开着时也要一起跟新（它上面有同一句话）"
        assert "document.hidden" in body, "后台标签页里不该继续打点"

    def test_the_quiet_path_never_flashes_the_skeleton(self):
        body = _function_body(_dashboard_script(), "loadOverview")

        assert "if (!quiet)" in body
        settle = body.index("if (!quiet)")
        assert settle < body.index("setLoading(true)"), (
            "安静那一拍也会把整页藏进骨架屏 —— 每 15 秒闪一次白，比不更新更糟"
        )
        # 下一拍排不排，取决于这一拍取回来的在途条数（跑完就自然停）。
        assert "scheduleActiveRefresh('overview', activeRuns.count || 0)" in body

    def test_the_interval_is_named_and_documented(self):
        script = _dashboard_script()
        body = _function_body(script, "armActiveRefresh")

        assert "AIU_ACTIVE_REFRESH_MS" in script
        assert "setTimeout" in body and "AIU_ACTIVE_REFRESH_MS" in body
        # 链式 `setTimeout`（一次只有一个请求在飞），不是 `setInterval`（慢网络下会堆积）。
        assert "setInterval" not in body

    def test_the_drill_does_not_wink_on_a_quiet_refresh(self):
        body = _function_body(_dashboard_script(), "loadDrill")

        assert "opts.quiet" in body
        assert body.index("if (!opts.quiet)") < body.index("加载中..."), (
            "安静那一拍也写「加载中...」，会把眼前的表格抹掉一瞬"
        )
