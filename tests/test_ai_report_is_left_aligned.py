# -*- coding: utf-8 -*-
"""AI 报告必须**靠左**排版，不能整篇居中。

## 缺陷形态（线上可见，用户截图）

抽屉里的报告连标题带正文全部居中：一级标题居中，列表项被收缩成内容宽度、
每行都居中，整块比抽屉窄一大截。

成因不是 `text-align` 写错了，而是**空态样式没被摘掉**：

    /* 空态 / 进行中：内容是提示语而不是报告，居中显示 */
    .ai-analysis-output.is-empty,
    .ai-analysis-output.is-busy {
        display: flex; flex-direction: column;
        align-items: center; justify-content: center;
        text-align: center;
    }

而容器在服务端渲染时**天生就带 `is-empty`**：

    <div class="ai-analysis-output is-empty" id="weeklyAiOutput">暂无分析结果。</div>

清掉这两个类的地方只有 `append*Line`（**逐行推送**那条路径）。报告并不一定逐行推
过来 —— 打开抽屉时若已经有结果，走的是 `set*Report` → `render*`，**一个 chunk 都
没有**，于是 `is-empty` 从初始 class 一路留到最后，整篇报告就用上了居中 flex 列。
流式那条路径反而是好的，所以这个缺陷只在「已有结果」时出现 —— 恰好是最常看到的那条。

修法是把摘类挪到 `render*()`（报告渲染的唯一出口），而不是留在 `append*Line` 里：
两处各管一半正是缺陷的来源。

## 为什么用 node 跑真函数而不是断言源码里有那两行

「源码里出现了 `classList.remove`」证明不了它**在正确的位置**（这正是刚才那个缺陷的
形状：两行都在，只是不在报告那条路径上）。所以这里抠出真实的三个函数、配一个最小的
DOM 替身，从「带 `is-empty` 的初始状态」出发调 `set*Report`，看类到底掉没掉。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RENDERER = PROJECT_ROOT / "static" / "js" / "ai-report-markdown.js"

# 三个模板的函数名不同，逐个点名而不是靠猜。
TEMPLATES = {
    "templates/weekly_version_diff.html": {
        "output_id": "weeklyAiOutput",
        "buffer": "weeklyAiBuffer",
        "queued": "weeklyAiRenderQueued",
        "set": "setWeeklyAiReport",
        "render": "renderWeeklyAi",
        "append": "appendWeeklyAiLine",
        "schedule": "scheduleWeeklyAi",
    },
    "templates/merged_project_view.html": {
        "output_id": "weeklyAiOutput",
        "buffer": "weeklyAiBuffer",
        "queued": "weeklyAiRenderQueued",
        "set": "setWeeklyAiReport",
        "render": "renderWeeklyAi",
        "append": "appendWeeklyAiLine",
        "schedule": "scheduleWeeklyAi",
    },
    "templates/commit_diff_new.html": {
        "output_id": "aiAnalysisOutput",
        "buffer": "aiBuffer",
        "queued": "aiRenderQueued",
        "set": "setAiReport",
        "render": "renderAi",
        "append": "appendAiLine",
        "schedule": "scheduleAi",
    },
}


def _read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """抠函数体前先剥注释：注释里的花括号会打乱配对（本仓库注释里就有 `{`）。"""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


def _function_source(src: str, name: str) -> str:
    """按大括号配对抠出一个函数声明。"""
    cleaned = _strip_js_comments(src)
    start = cleaned.find(f"function {name}(")
    assert start != -1, f"模板里找不到 function {name}("
    depth = 0
    for index in range(cleaned.index("{", start), len(cleaned)):
        if cleaned[index] == "{":
            depth += 1
        elif cleaned[index] == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start:index + 1]
    raise AssertionError(f"function {name}( 的大括号没有闭合")


def _build_driver(template: str, names: dict) -> str:
    """把真实的 render/set/append 函数抠出来，配一个最小 DOM 替身跑。"""
    src = _read(template)
    parts = [
        # 真渲染器，不用替身 —— 替身会把转义行为一起掩盖掉
        RENDERER.read_text(encoding="utf-8"),
        f"var {names['buffer']} = '';",
        f"var {names['queued']} = false;",
        # 合帧只为省性能，这里同步执行，断言不必等一帧
        "var requestAnimationFrame = function (fn) { fn(); };",
        _function_source(src, names["set"]),
        _function_source(src, names["render"]),
        _function_source(src, names["append"]),
        _function_source(src, names["schedule"]),
        f"""
var OUTPUT_ID = {json.dumps(names['output_id'])};
var elements = {{}};
function makeElement() {{
    var classes = [];
    return {{
        innerHTML: '', textContent: '', scrollTop: 0, scrollHeight: 0,
        classList: {{
            add: function (c) {{ if (classes.indexOf(c) < 0) classes.push(c); }},
            remove: function (c) {{ var i = classes.indexOf(c); if (i >= 0) classes.splice(i, 1); }},
            toggle: function (c, on) {{ on ? this.add(c) : this.remove(c); }},
            contains: function (c) {{ return classes.indexOf(c) >= 0; }},
        }},
        dump: function () {{ return classes.slice(); }},
    }};
}}
globalThis.document = {{
    getElementById: function (id) {{
        if (!elements[id]) elements[id] = makeElement();
        return elements[id];
    }},
}};

function fresh() {{
    elements = {{}};
    var el = document.getElementById(OUTPUT_ID);
    // 服务端渲染出来的初始状态：占位语 + is-empty
    el.classList.add('is-empty');
    el.textContent = '暂无分析结果。';
    {names['buffer']} = '';
    return el;
}}

var results = {{}};

// 1) 核心回归：已有结果这条路径（没有任何 chunk），报告不许居中
var el = fresh();
{names['set']}('# 影响面分析\\n\\n- 吸灵器链路 **必测**');
results.cached_report = {{ classes: el.dump(), html: el.innerHTML }};

// 2) 流式那条路径（逐行推送）同样不许留空态
el = fresh();
{names['append']}('# 变更理解');
results.streamed_report = {{ classes: el.dump(), html: el.innerHTML }};

// 3) 进行中（is-empty + is-busy 都在）时报告到达
el = fresh();
el.classList.add('is-busy');
{names['set']}('## 风险评估');
results.report_after_busy = {{ classes: el.dump(), html: el.innerHTML }};

// 4) 空报告：没有正文可显示，空态样式要留着（否则空框看着像坏了）
el = fresh();
{names['set']}('');
results.empty_report = {{ classes: el.dump(), html: el.innerHTML }};

process.stdout.write(JSON.stringify(results));
""",
    ]
    return "\n".join(parts)


def _run(template: str, names: dict) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实渲染断言")
    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / "driver.js"
        driver.write_text(_build_driver(template, names), encoding="utf-8")
        proc = subprocess.run(
            ["node", str(driver)], capture_output=True, text=True, timeout=60,
        )
    assert proc.returncode == 0, f"Node 执行失败（{template}）：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def behaviours() -> dict:
    return {name: _run(name, names) for name, names in TEMPLATES.items()}


# --------------------------------------------------------------------------
# 让缺陷可能发生的那个前提
# --------------------------------------------------------------------------

@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_the_empty_state_is_a_centered_flex_column(template):
    """先钉住前提：空态样式确实是「居中 flex 列」，而且初始 class 里就有 `is-empty`。

    这两条缺一条，本文件守的缺陷就不成立（也就不会有人再踩）。哪天空态改成靠左了，
    这条会失败并提醒删掉这个文件 —— 而不是留下一个永远为真的空断言。
    """
    src = _read(template)
    rule = re.search(r"\.ai-analysis-output\.is-empty[^{]*\{([^}]*)\}", src)
    assert rule is not None, f"{template} 里没有 .ai-analysis-output.is-empty 规则"
    body = rule.group(1)
    assert re.search(r"text-align:\s*center", body), "空态不再居中了，本文件的守卫前提已变"
    assert re.search(r"align-items:\s*center", body), "空态不再是居中 flex 列"
    assert re.search(r"display:\s*flex", body)

    assert re.search(
        r'class="[^"]*ai-analysis-output[^"]*is-empty', src
    ), "输出容器不再从服务端带着 is-empty 出厂，触发条件已变"


# --------------------------------------------------------------------------
# 核心回归
# --------------------------------------------------------------------------

@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_a_report_from_cache_is_not_rendered_centered(behaviours, template):
    """**核心回归**：`set*Report`（已有结果这条路径）必须摘掉空态样式。

    这条路径一个 chunk 都没有，所以只把摘类写在 `append*Line` 里是不够的 ——
    那正是线上「打开抽屉就看到整篇居中」的成因。
    """
    result = behaviours[template]["cached_report"]
    assert "is-empty" not in result["classes"], (
        f"{template}：报告渲染后 is-empty 还在，整篇会被居中 flex 列排版"
    )
    assert result["html"].strip(), "报告没有渲染出内容"
    assert "<h1>" in result["html"] and "<li>" in result["html"], (
        f"渲染结构不对：{result['html']}"
    )


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_a_report_after_the_busy_state_is_not_centered(behaviours, template):
    """进行中（is-busy）之后到达的报告也要靠左。"""
    result = behaviours[template]["report_after_busy"]
    assert "is-busy" not in result["classes"], (
        f"{template}：is-busy 没摘掉，报告会带着「进行中」的居中排版"
    )
    assert "is-empty" not in result["classes"]


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_the_streaming_path_still_clears_the_state(behaviours, template):
    """逐行推送那条路径不能因为这次改动而退化。"""
    result = behaviours[template]["streamed_report"]
    assert "is-empty" not in result["classes"]
    assert "is-busy" not in result["classes"]


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_an_empty_report_keeps_the_centered_empty_state(behaviours, template):
    """没有正文时不许把空态样式摘掉。

    这是上面那条守卫的边界：`render*` 里的摘类必须带「有正文」这个条件。
    无条件摘掉的话，空报告会留下一个既没有提示语、也不再居中的空框。
    """
    result = behaviours[template]["empty_report"]
    assert "is-empty" in result["classes"], (
        f"{template}：空报告把空态样式也摘掉了，空框会失去居中的提示语样式"
    )
