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


def _run_node(
    script: str, probe_source: str, out_source: str, cases: dict | None = None
) -> dict:
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
            json.dumps(cases or {"noteCases": _NOTE_CASES}, ensure_ascii=False),
            encoding="utf-8",
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
            "当前筛选范围内有 1 个新任务运行中（当前任务未实时统计，实时用量尚未计入）；"
            "以下统计截至运行 #14，"
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
        """「N / M 次」里的两个数都是**已完成**的运行，而且 M 是这一屏的运行总数。

        这条原先钉的是 `renderKpis` 里的一句字面量；现在那句话搬进了纯函数
        `aiuKpiCards`（口径没变，只是搬了家 —— 搬家的理由是那样能被 node 真跑，
        见下面 `TestTheKpiCardsKeepTheKnownNumbersVisible`）。
        """
        script = _dashboard_script()
        body = _function_body(script, "aiuKpiCards")
        coverage = _function_body(script, "aiuCoverageText")
        render = _function_body(script, "renderKpis")

        assert "次已完成运行" in coverage, "「N / M 次运行」会被读成「一共跑了 M 次」"
        assert "次运行中，未计入" in body, "有在途运行时没有说一句「另有 N 次没算进来」"
        # 渲染这一层**不做任何口径判断**：卡片内容全部来自那个纯函数。
        assert "aiuKpiCards(" in render, "renderKpis 又自己算起口径来了"

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

    def test_the_project_and_weekly_rows_also_keep_the_known_numbers(self):
        """行内的命中率与费用**也要**先读已上报样本，不能只有顶部 KPI 修了。

        验收标准写的是「部分历史缺字段时，已知费用与缓存命中仍然可见」—— 只修 KPI
        的话，用户点进项目看到的仍然是一整格「未上报」，而下面那行还写着
        「20 次运行 · 15 次采集到用量」。
        """
        script = _dashboard_script()
        for name in ("renderProjects", "renderWeekly"):
            body = _function_body(script, name)
            assert "reported_samples" in body, f"{name} 还在只读那一份全体齐全的值"
            assert "known_value" in body, name

    def test_the_weekly_row_states_its_coverage(self):
        """周版本行要把覆盖率写在**行上**：行里的数字是已上报样本的值。"""
        body = _function_body(_dashboard_script(), "renderWeekly")

        assert "collected_runs" in body and "次采集到用量" in body

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


# ==========================================================================
#  四、KPI 卡片读的是「已上报样本 + 覆盖度」（AI-P1-02）
# ==========================================================================
# 库里的真实形状：20 次已完成运行里 5 次（失败在半路）token 三列全 NULL。
# 修之前，`tokens.total` / `cache.hit_rate` / `cost` 三者同时是 `null`，于是整屏
# 「未上报」——**一个历史缺失值抹掉了另外 15 次的有效统计**。
#
# 修法不是放宽旧的三个字段（它们仍然是「全体齐全时的精确值」），而是让页面优先读
# 服务端并列下发的 `reported_samples.*.known_value`，并把覆盖率写在副标题里。
# 这里用 **node 真跑** `aiuKpiCards`：静态断言只看得到字符串，看不见「到底显示了哪个数」。


def _totals(**overrides) -> dict:
    """一份 `completed_totals`（形状与 `aggregate_runs` 的产物一致）。"""
    totals = {
        "runs": 20,
        "collected_runs": 20,
        "tokens": {"input": 15000, "output": 3000, "total": None,
                   "cache_read": 6000, "cache_write": None},
        "cache": {"hit_rate": None, "read": 6000, "missing_runs": 5},
        "missing_runs": {"input": 5, "output": 5, "cache_read": 5, "cache_write": 20,
                         "context_chars": 0, "duration_ms": 0},
        "cost": None,
        "reported_samples": {
            "total_runs": 20,
            "tokens": {"total_runs": 20, "reported_runs": 15, "unknown_runs": 5,
                       "known_value": 18000, "known_input": 15000, "known_output": 3000},
            "cache": {"total_runs": 20, "reported_runs": 15, "unknown_runs": 5,
                      "known_value": 0.4, "known_input": 15000, "known_cache_read": 6000},
            "cost": {"total_runs": 20, "reported_runs": 15, "unknown_runs": 5,
                     "known_value": {"amount": "0.04", "amount_exact": "0.0432",
                                     "currency": "CNY", "price_version": "dual-1",
                                     "notes": []},
                     "reason": ""},
        },
    }
    totals.update(overrides)
    return totals


_ALL_REPORTED = _totals(
    tokens={"input": 15000, "output": 3000, "total": 18000,
            "cache_read": 6000, "cache_write": None},
    cache={"hit_rate": 0.4, "read": 6000, "missing_runs": 0},
    missing_runs={"input": 0, "output": 0, "cache_read": 0, "cache_write": 20,
                  "context_chars": 0, "duration_ms": 0},
    cost={"amount": "0.04", "amount_exact": "0.0432", "currency": "CNY",
          "price_version": "dual-1", "notes": []},
    reported_samples={
        "total_runs": 20,
        "tokens": {"total_runs": 20, "reported_runs": 20, "unknown_runs": 0,
                   "known_value": 18000, "known_input": 15000, "known_output": 3000},
        "cache": {"total_runs": 20, "reported_runs": 20, "unknown_runs": 0,
                  "known_value": 0.4, "known_input": 15000, "known_cache_read": 6000},
        "cost": {"total_runs": 20, "reported_runs": 20, "unknown_runs": 0,
                 "known_value": {"amount": "0.04", "amount_exact": "0.0432",
                                 "currency": "CNY", "price_version": "dual-1",
                                 "notes": []},
                 "reason": ""},
    },
)

_NOTHING_DONE = _totals(
    runs=0, collected_runs=0,
    tokens={"input": None, "output": None, "total": None,
            "cache_read": None, "cache_write": None},
    cache={"hit_rate": None, "read": None, "missing_runs": 0},
    missing_runs={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                  "context_chars": 0, "duration_ms": 0},
    reported_samples={
        "total_runs": 0,
        "tokens": {"total_runs": 0, "reported_runs": 0, "unknown_runs": 0,
                   "known_value": None, "known_input": None, "known_output": None},
        "cache": {"total_runs": 0, "reported_runs": 0, "unknown_runs": 0,
                  "known_value": None, "known_input": None, "known_cache_read": None},
        "cost": {"total_runs": 0, "reported_runs": 0, "unknown_runs": 0,
                 "known_value": None, "reason": "还没有配置价格表"},
    },
)

# 老响应：没有 `reported_samples`（接口回落的形状）—— 退回旧字段渲染，不许抛。
_OLD_RESPONSE = {
    "runs": 3,
    "tokens": {"input": 3000, "output": 600, "total": 3600,
               "cache_read": 1200, "cache_write": None},
    "cache": {"hit_rate": 0.4}, "missing_runs": {}, "cost": None,
}

_KPI_CASES = [
    # 0：**这个文件存在的理由** —— 部分历史缺字段。三张卡片都必须给出已知值。
    [_totals(), 0, 0],
    # 1：全体齐全 → 样本值与旧的精确值同时存在，卡片按精确值显示（不带「已上报样本」后缀）。
    [_ALL_REPORTED, 0, 0],
    # 2：一次都没完成（新项目，第一条还在跑）→ 三张卡片「未上报」，但**不报错、不补 0**。
    [_NOTHING_DONE, 0, 0],
    # 3：有在途运行 + 有超预算项目 → 两句都要出现，且不能把在途那次的临时值算进来。
    [_totals(), 2, 3],
    # 4：老响应（没有 `reported_samples`）→ 退回旧字段渲染，**不许抛**。
    [_OLD_RESPONSE, 0, 0],
]

_KPI_OUT = """function (A, sandbox, makeElement) {
    return {
        cards: A.kpiCases.map(function (item) {
            return sandbox.aiuKpiCards(item[0], item[1], item[2]);
        }),
        coverage: [
            sandbox.aiuCoverageText(20, 5, '输入 token'),
            sandbox.aiuCoverageText(20, 0, '输入 token'),
            sandbox.aiuCoverageText(0, 0, '输入 token')
        ]
    };
}"""


@pytest.fixture(scope="module")
def kpi_js() -> dict:
    return _run_node(
        _dashboard_script(), "", _KPI_OUT, cases={"kpiCases": _KPI_CASES}
    )


def _card(cards: list, label: str) -> dict:
    matched = [card for card in cards if card["label"].startswith(label)]
    assert len(matched) == 1, [card["label"] for card in cards]
    return matched[0]


class TestTheKpiCardsKeepTheKnownNumbersVisible:
    def test_the_functions_are_really_callable(self, kpi_js):
        assert len(kpi_js["cards"]) == len(_KPI_CASES)

    def test_one_missing_history_row_does_not_blank_the_screen(self, kpi_js):
        """15 次有数、5 次没有 → 三张卡片全部显示**已上报样本**的值，覆盖率写在副标题里。

        这一条就是整件事的验收：修之前这三张卡片一起写「未上报」。
        """
        cards = kpi_js["cards"][0]

        assert "未上报" not in [card["value"] for card in cards], cards
        assert _card(cards, "合计 token")["value"] == "18.0k"
        assert _card(cards, "合计 token")["unknown"] is False
        assert "已上报样本 15 / 20 次已完成运行" in _card(cards, "合计 token")["sub"]
        assert "已知值，不是总额" in _card(cards, "合计 token")["sub"]

    def test_the_cache_rate_and_cost_stay_visible_and_say_what_they_are(self, kpi_js):
        cards = kpi_js["cards"][0]
        rate = _card(cards, "缓存命中率")
        cost = _card(cards, "费用估算")

        assert rate["value"] == "40.0%", rate
        assert rate["unknown"] is False, "命中率整格「未上报」—— 那正是这次要修的症状"
        assert "已上报样本" in rate["sub"]

        assert cost["value"] == "¥0.04", cost
        assert "已知最低费用" in cost["sub"], "费用的口径没写出来 —— 读的人会当成总额"
        assert "已上报样本" in cost["sub"], cost["sub"]

    def test_a_fully_reported_history_keeps_using_the_exact_values(self, kpi_js):
        """全体齐全时**不**给卡片加「（已上报样本）」后缀 —— 那时两组数本来就相同。"""
        cards = kpi_js["cards"][1]

        assert _card(cards, "合计 token")["label"] == "合计 token"
        assert _card(cards, "合计 token")["value"] == "18.0k"
        assert _card(cards, "缓存命中率")["label"] == "缓存命中率"
        assert "已上报样本" not in _card(cards, "缓存命中率")["sub"]

    def test_nothing_completed_means_unknown_not_zero(self, kpi_js):
        """一次都没完成 → 「未上报」，**不是 0**：0 是「跑了但没花」，与「还没跑」正相反。"""
        cards = kpi_js["cards"][2]

        assert _card(cards, "合计 token")["value"] == "未上报"
        assert _card(cards, "合计 token")["unknown"] is True
        assert _card(cards, "缓存命中率")["value"] == "未上报"

    def test_an_unconfigured_price_table_gives_the_reason_not_unknown(self, kpi_js):
        """价格表算不出费用时给**理由**，不写「未上报」—— 那是 token 的口径。"""
        cost = _card(kpi_js["cards"][2], "费用估算")

        assert cost["value"] == "还没有配置价格表", cost
        assert cost["unknown"] is True

    def test_active_runs_are_named_in_the_subtitle_not_counted_in(self, kpi_js):
        cards = kpi_js["cards"][3]

        assert "另有 3 次运行中，未计入" in _card(cards, "合计 token")["sub"]
        assert _card(cards, "合计 token")["value"] == "18.0k", (
            "在途那几次的临时值混进了合计"
        )
        assert _card(cards, "已超预算的项目")["value"] == "2 个"

    def test_an_old_response_without_samples_still_renders(self, kpi_js):
        """老响应（没有 `reported_samples`）→ 退回旧字段，不抛、不白屏。"""
        cards = kpi_js["cards"][4]

        assert _card(cards, "合计 token")["value"] == "3.6k"
        assert _card(cards, "缓存命中率")["value"] == "40.0%"
        assert len([card for card in cards if card["label"] == "费用估算"]) == 0

    def test_the_coverage_sentence_has_three_shapes(self, kpi_js):
        """覆盖率那句在三种情况下都要说得通：部分 / 全部 / 一次都没有。"""
        partial, full, none = kpi_js["coverage"]

        assert partial == "已上报样本 15 / 20 次已完成运行，另有 5 次未上报输入 token"
        assert full == "全部 20 次已完成运行都已上报"
        assert none == "还没有已完成的运行"


# ==========================================================================
#  五、执行前的预估卡片（AI-P1-03）
# ==========================================================================
# 同一段脚本里再跑一个纯函数：`aiuEstimateCards`。三条硬要求各有用例 ——
# **只给区间不给单点**、**价格没配置时只给 token 与时间**、**基线单独一张卡**。

_ESTIMATE_CASES = [
    # 0：价格表没配（`DEFAULT_PRICE_TABLE` 是空表，这是常态）→ 只给 token 与时间。
    [{
        "tokens": {"low": 1519646, "high": 3424708, "unit": "token"},
        "duration_ms": {"low": 1214893, "high": 2165817},
        "shard_count": 3,
        "baseline": {"reusable": False, "note": "没有命中可复用基线，这次会真的跑一遍模型。"},
        "cost": {"low": None, "high": None, "computable": False,
                 "reason": "还没有配置价格表", "currency": ""},
        "last_actual": {"run_id": 20, "files": 1009, "tokens": 1999833,
                        "duration_ms": 1595548, "shards": 3},
        "basis": {"source": "files", "runs": 15, "same_mode_runs": 15,
                  "files": {"low": 847, "high": 1009}, "shards": None, "run_ids": [1]},
        "notes": [],
    }],
    # 1：配了价格表 → 给区间；两端相同就写一个数（不写 `2.0M ~ 2.0M`）。
    [{
        "tokens": {"low": 2000000, "high": 2000000, "unit": "token"},
        "duration_ms": {"low": 1500000, "high": 1500000},
        "shard_count": 1,
        "baseline": {"reusable": True,
                     "note": "命中可复用基线：这次不需要新建付费运行，直接复用已有结论。"},
        "cost": {"low": {"amount": "4.00", "price_version": "v2", "currency": "CNY"},
                 "high": {"amount": "4.00", "price_version": "v2", "currency": "CNY"},
                 "computable": True, "reason": "", "currency": "CNY"},
        "last_actual": None,
        "basis": {"source": "totals", "runs": 2, "same_mode_runs": 0,
                  "files": {"low": None, "high": None}, "shards": None, "run_ids": []},
        "notes": ["没有「incremental」的历史运行，区间按最近 2 次运行估算；"],
    }],
    # 1.5：**借来的区间**：增量没有实测样本，服务端给的是全量运行的区间
    #      （`basis.mode_samples_missing`）—— 必须照实标出来，不许写成「预计增量代价」。
    [{
        "mode": "incremental",
        "tokens": {"low": 1275659, "high": 2874854, "unit": "token"},
        "duration_ms": {"low": 900000, "high": 1600000},
        "shard_count": 3,
        "baseline": {"reusable": True, "note": "命中可复用基线：这次不需要新建付费运行。"},
        "cost": {"low": None, "high": None, "computable": False,
                 "reason": "还没有配置价格表", "currency": ""},
        "last_actual": None,
        "basis": {"source": "files", "runs": 15, "same_mode_runs": 0,
                  "mode_samples_missing": True,
                  "files": {"low": 847, "high": 1009}, "shards": 3, "run_ids": [1]},
        "notes": ["**incremental 没有实测样本**：这里参照的是全量运行的区间（最近 15 次），"
                  "不是这个模式的实测值 —— 换了模式之后文件数与轮次都会变，实际可能明显偏离。"],
    }],
    # 2：一条可参照的运行都没有 → 三格「未上报」，不许抛、不许编数字。
    [{"basis": {"source": "none", "runs": 0, "same_mode_runs": 0,
                "files": {"low": None, "high": None}, "shards": None, "run_ids": []},
      "tokens": {"low": None, "high": None, "unit": "token"},
      "duration_ms": {"low": None, "high": None},
      "baseline": {"reusable": None, "note": "这次能不能复用基线还没有判定。"},
      "cost": {"low": None, "high": None, "computable": False,
               "reason": "还没有配置价格表", "currency": ""},
      "last_actual": None,
      "notes": ["还没有可参照的历史运行，无法估算 —— 先跑一次才有区间。"]}],
]

_ESTIMATE_OUT = """function (A, sandbox, makeElement) {
    return {
        type: typeof sandbox.aiuEstimateCards,
        cards: A.estimateCases.map(function (item) {
            return sandbox.aiuEstimateCards(item[0]);
        })
    };
}"""


@pytest.fixture(scope="module")
def estimate_js() -> dict:
    return _run_node(
        _dashboard_script(), "", _ESTIMATE_OUT,
        cases={"estimateCases": _ESTIMATE_CASES},
    )


class TestTheEstimateCardsNeverFakeACost:
    def test_the_function_is_really_callable(self, estimate_js):
        assert estimate_js["type"] == "function"
        assert len(estimate_js["cards"]) == len(_ESTIMATE_CASES)

    def test_without_a_price_table_tokens_and_time_still_come_through(self, estimate_js):
        """**价格没配置时显示 token 与时间，不伪造费用**（AI-P1-03 的硬要求）。"""
        cards = estimate_js["cards"][0]

        assert _card(cards, "预计 token")["value"] == "1.52M ~ 3.42M"
        assert _card(cards, "预计时间")["value"] == "20.2 min ~ 36.1 min"
        assert _card(cards, "预计费用")["value"] == "算不出", cards
        assert _card(cards, "预计费用")["unknown"] is True
        assert "还没有配置价格表" in _card(cards, "预计费用")["sub"]
        assert "0" not in _card(cards, "预计费用")["value"], "把「算不出」写成了 0"

    def test_the_last_actual_value_and_the_baseline_are_shown(self, estimate_js):
        """确认框要的四样这里都有：预计 token / 预计时间 / 最近一次实际值 / 是否命中基线。"""
        cards = estimate_js["cards"][0]

        last = _card(cards, "最近一次实际值")
        assert "2.00M token" in last["value"], last
        assert "运行 #20" in last["sub"] and "1009 个文件" in last["sub"]
        assert _card(cards, "可复用基线")["value"] == "没有命中"
        assert "命中可复用基线" in _card(cards, "可复用基线")["sub"]

    def test_a_single_ended_range_is_not_written_twice(self, estimate_js):
        """区间两端相同时写一个数（`2.0M ~ 2.0M` 会被读成「范围很大」）。"""
        cards = estimate_js["cards"][1]

        assert _card(cards, "预计 token")["value"] == "2.00M"
        assert _card(cards, "预计时间")["value"] == "25.0 min"
        assert _card(cards, "可复用基线")["value"] == "命中"
        assert _card(cards, "预计费用")["value"] == "¥4.00", cards

    def test_a_borrowed_range_says_it_is_borrowed(self, estimate_js):
        """增量没有实测样本 → 区间**照给**，但必须写明它是从全量借来的。

        这一条是裁定过的口径：库里全是全量运行，那就不要假装知道增量要花多少。
        界面**不许**把这个数标成「预计增量代价」—— 一个偏保守但自称准确的数字，
        比一句「这个模式没有实测样本」更容易被当真、更容易被拿去做决策。
        """
        cards = estimate_js["cards"][2]

        token = _card(cards, "预计 token")
        assert "增量：参照全量" in token["label"], token["label"]
        assert "全量" in token["sub"] and "不是增量的实测值" in token["sub"], token["sub"]
        assert "同类运行" not in token["sub"], (
            "借来的区间却说是「同类运行」折算的 —— 那是两件事"
        )
        assert "增量：参照全量" in _card(cards, "预计时间")["label"]

    def test_a_same_mode_range_is_not_labelled_as_borrowed(self, estimate_js):
        """有本模式实测样本时**不许**挂那个后缀（挂上去会把真估算说成借来的）。"""
        cards = estimate_js["cards"][0]

        assert "参照全量" not in _card(cards, "预计 token")["label"]
        assert "同类运行" in _card(cards, "预计 token")["sub"]

    def test_no_history_is_unknown_and_says_so(self, estimate_js):
        cards = estimate_js["cards"][3]

        assert _card(cards, "预计 token")["value"] == "未上报"
        assert "先跑一次才有区间" in _card(cards, "预计 token")["sub"]
        assert _card(cards, "预计费用")["value"] == "算不出"


class TestTheEstimateWiring:
    """估算那一格的接线：卡片在、端点对、数字来自接口而不是模板里写死的常数。"""

    def test_the_card_exists_and_sits_with_the_kpi_block(self):
        html = _read(DASHBOARD)

        for anchor in ("aiuEstimateCard", "aiuEstimateBody", "aiuEstimateProject",
                       "aiuEstimateMode", "aiuEstimateFiles"):
            assert f'id="{anchor}"' in html, anchor
        # 它在 KPI 下面（「已经花了多少」→「下一次大概花多少」），在项目表上面。
        assert html.index('id="aiuKpis"') < html.index('id="aiuEstimateCard"')
        assert html.index('id="aiuEstimateCard"') < html.index("各项目消耗")

    def test_it_calls_the_read_only_endpoint(self):
        script = _dashboard_script()

        assert "/ai-analysis/usage/estimate" in script, "估算没有走服务端的端点"
        body = _function_body(script, "loadEstimate")
        assert "params.set('project'" in body
        assert "params.set('mode'" in body, "模式没传，服务端只能按全量估"
        # 失败**不覆盖**上面那些已经取回来的数字（那是这一页的正文）。
        assert "renderEstimate({})" in body

    def test_the_numbers_come_from_the_payload_not_from_the_template(self):
        script = _dashboard_script()
        body = _function_body(script, "aiuEstimateCards")

        for key in ("tokens", "duration_ms", "last_actual", "baseline", "cost", "basis"):
            assert key in body, f"估算卡片没有读 {key}"

    def test_a_failed_estimate_does_not_blank_the_grid(self):
        body = _function_body(_dashboard_script(), "renderEstimate")

        assert "payload.basis" in body, (
            "没有估算结果时也摆一排「未上报」—— 那是「还没问」，不是「算不出」"
        )

    def test_the_notes_are_rendered_on_the_page_not_only_returned(self):
        """**前提必须落在页面上**，不能只躺在接口字段里。

        `notes` 里写着「分片是串行的，按 N 个分片折算」「目标文件数超出样本区间属于外推」
        「增量参照的是全量区间」这些前提 —— 区间一旦离开前提，读起来就是一个承诺。
        所以 `notes` 只回给接口 = 丢了前提，这一条钉的就是「它真的被写进页面」。
        """
        script = _dashboard_script()
        body = _function_body(script, "renderEstimate")

        assert "aiuEstimateNotes" in body, "notes 没有写进页面上那个节点"
        assert "payload.notes" in body
        assert "notes.hidden = !lines.length" in body, "没有 notes 时那一行要收起来"
        # 接口给的前提里有 markdown 味的 `**加粗**`（后端文案自带），front-end 必须抹掉 ——
        # `textContent` 不认 markdown，原样落下去用户看到的是两个星号。
        assert "replace(/\*\*/g" in body, "notes 里的 ** 会原样显示给用户"
        # 页面里那个承载节点必须是可见的普通段落（不是 hidden 的容器）。
        html = _read(DASHBOARD)
        assert 'id="aiuEstimateNotes"' in html

    def test_the_shard_precondition_is_in_the_notes_not_just_in_the_payload(self):
        """分片折算那句前提必须**同时在**接口字段与页面可见处 —— 服务端 `notes` 里
        有一句「按 N 个分片折算…分片是串行的」，页面照单渲染（上一条钉渲染）。

        这里再钉一次**文案本身**还在：它是「token 按分片数折算」这个做法的唯一说明，
        删掉它，那个数字就从「有依据的粗估」变成「来路不明的精确值」。
        """
        from services.ai.usage import estimate_analysis

        result = estimate_analysis(
            planned_files=100, mode="full", shard_count=4,
            recent_runs=[{
                "run_id": 1, "created_at": "2026-09-01", "scope": "full",
                "tokens_input": 1_000_000, "tokens_output": 200_000,
                "cache_read": 800_000, "duration_ms": 600_000,
                "files": 100, "shards": 2, "model": "m", "cost": None,
            }],
        )

        shard_notes = [note for note in result["notes"] if "分片" in note]
        assert shard_notes, result["notes"]
        assert any("串行" in note for note in shard_notes), shard_notes
