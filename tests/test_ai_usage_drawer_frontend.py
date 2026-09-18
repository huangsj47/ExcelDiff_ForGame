# -*- coding: utf-8 -*-
"""抽屉里那一行「本次消耗」的文案口径 —— 用 node 真跑 `static/js/ai_usage_line.js`。

## 为什么必须真跑

这一行的全部风险都在**文案与数值的映射**上，而它偏偏是「看一眼觉得对」的那种代码：

* `null`（上游没上报）如果被 `Number(null)` 变成 0，界面就显示「命中缓存 0.0%」——
  一个用户会当真、而事实相反的结论。「没上报」与「一次都没命中」的含义正好相反。
* 功能上线前的历史运行没有用量，如果显示「本次消耗 0 tokens」，用户会以为那次分析
  不要钱，而不是「没采集」。

静态断言挡不住这类错误（`Number(null)` 是个合法的字符串写法），所以这里按仓库既有的
做法：把文件读进 node 真跑，断言**输出字符串**。

## 反向自检

除了正例，还断言「未上报」与「0」的输出**互不相同** —— 否则一旦有人把两支合并，
正例可能因为两句文案恰好都不为空而全绿。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_usage_line.js"

# 三份模板必须都引这个脚本（DOM 片段由 test_ai_usage_fragment_sync.py 逐字比对）。
TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)


def _run(cases: list) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的文案断言")
    driver = f"""
const fs = require('fs');
const vm = require('vm');
const sandbox = {{ window: {{}}, document: {{ getElementById: () => null, readyState: 'complete',
    addEventListener: () => {{}} }} }};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(SCRIPT))}, 'utf8'), sandbox);
const api = sandbox.aiUsageInternals;
const cases = {json.dumps(cases, ensure_ascii=False)};
const out = cases.map(function (item) {{
    const usage = item.usage || {{}};
    const tokens = usage.tokens || {{}};
    const cache = usage.cache || {{}};
    return {{
        name: item.name,
        line: api.buildLine(item.usage),
        tokens: api.fmtTokens(tokens.total),
        rate: api.fmtRate(cache.hit_rate),
        duration: api.fmtDuration(usage.duration_ms),
        cost: api.fmtCost(item.cost)
    }};
}});
process.stdout.write(JSON.stringify(out));
"""
    with __import__("tempfile").TemporaryDirectory() as tmp:
        path = Path(tmp) / "driver.js"
        path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return {item["name"]: item for item in json.loads(proc.stdout)}


@pytest.fixture(scope="module")
def results() -> dict:
    return _run(
        [
            {"name": "完整上报", "usage": {
                "collected": True,
                "tokens": {"input": 1000, "output": 200, "total": 1200, "cache_read": 900},
                "cache": {"hit_rate": 0.9},
                "rounds": 3,
                "duration_ms": 2500,
            }},
            {"name": "命中为零", "usage": {
                "collected": True,
                "tokens": {"input": 1000, "output": 200, "total": 1200, "cache_read": 0},
                "cache": {"hit_rate": 0.0},
                "rounds": 1,
                "duration_ms": 400,
            }},
            {"name": "缓存未上报", "usage": {
                "collected": True,
                "tokens": {"input": 1000, "output": 200, "total": 1200, "cache_read": None},
                "cache": {"hit_rate": None},
                "rounds": 2,
                "duration_ms": 900,
            }},
            {"name": "未采集", "usage": {"collected": False, "tokens": {}, "cache": {}}},
            {"name": "没有用量", "usage": None},
        ]
    )


def test_a_full_report_reads_as_tokens_and_hit_rate(results):
    line = results["完整上报"]["line"]
    assert "1.2k tokens" in line, line
    assert "命中缓存 90.0%" in line, line
    assert "3 轮" in line, line


def test_a_reported_zero_is_shown_as_zero(results):
    """上游报了「确实一次都没命中」—— 这时**必须**显示 0%，不能显示「未上报」。

    这一条与下面那条是一对：两者要是输出一样，说明有一支写错了。
    """
    line = results["命中为零"]["line"]
    assert "命中缓存 0.0%" in line, line


def test_an_unreported_cache_field_is_not_rendered_as_zero(results):
    """**核心回归**：`None` = 没上报，不许变成 0%。"""
    line = results["缓存未上报"]["line"]
    assert "0.0%" not in line, f"把「未上报」渲染成了 0%：{line}"
    assert "缓存命中未上报" in line, line


def test_the_two_are_actually_different(results):
    """反向自检：两种情况的输出必须不同，否则上面两条断言没有区分力。"""
    assert results["命中为零"]["line"] != results["缓存未上报"]["line"]
    assert results["命中为零"]["rate"] == "0.0%"
    assert results["缓存未上报"]["rate"] is None


def test_a_run_without_captured_usage_says_so_instead_of_zero(results):
    """历史运行（功能上线前）显示「未采集」，**不是 0 tokens**。"""
    line = results["未采集"]["line"]
    assert "未采集" in line, line
    assert "0" not in line, f"把「没采集」渲染出了 0：{line}"


def test_no_usage_hides_the_line_entirely(results):
    """没有结果时整行隐藏（由调用方决定），文案函数返回 null。"""
    assert results["没有用量"]["line"] is None


def test_token_formatting_scales(results):
    assert results["完整上报"]["tokens"] == "1.2k"
    assert results["命中为零"]["tokens"] == "1.2k"


@pytest.mark.parametrize("name", ["完整上报", "命中为零", "缓存未上报"])
def test_unknown_values_never_become_numbers(results, name):
    """`null` 进、`null` 出：任何一格都不许把「未上报」变成数字。"""
    item = results[name]
    for key, value in (("tokens", item["tokens"]), ("rate", item["rate"]),
                       ("duration", item["duration"])):
        assert value is None or isinstance(value, str), f"{name}.{key} = {value!r}"


# ---------------------------------------------------------------------------
#  「不足一分钱」的符号位置
# ---------------------------------------------------------------------------
# 后端对**算得出来但不到一分钱**的费用给的是字符串 `"<0.01"`
# （`services/ai/pricing.py`），不是数字 —— 前端只补币种符号、不做算术。
#
# 风险全在拼接顺序上：「符号 + 数字」这种写法一遇到 `<0.01` 就变成 `¥<0.01`，
# 读起来像「币种后面跟了个比较符」。正确的读法是 `<¥0.01`（不到一分钱），
# 符号要插在 `<` **之后**。静态断言看不见这个区别（两种拼法都是合法字符串），
# 所以照旧用 node 真跑。
_COST_CASES = [
    {"name": "不足一分钱", "cost": {"amount": "<0.01", "currency": "CNY"}},
    {"name": "不足一分钱美元", "cost": {"amount": "<0.01", "currency": "USD"}},
    {"name": "不足一分钱且认不出币种", "cost": {"amount": "<0.01", "currency": "XYZ"}},
    {"name": "正常金额", "cost": {"amount": "1.23", "currency": "CNY"}},
    {"name": "认不出币种的正常金额", "cost": {"amount": "1.23", "currency": "XYZ"}},
    {"name": "没有金额", "cost": {"amount": None, "currency": "CNY"}},
    {"name": "没有费用对象", "cost": None},
]


@pytest.fixture(scope="module")
def cost_results() -> dict:
    return _run(_COST_CASES)


def test_a_sub_cent_amount_keeps_the_symbol_inside_the_less_than(cost_results):
    """`<0.01` 是一个整体：符号插在 `<` 之后 → `<¥0.01`。"""
    text = cost_results["不足一分钱"]["cost"]
    assert text == "<¥0.01", text
    # 反向自检：这一条才是缺陷本身。谁把拼接顺序改回去（`symbol + text`），
    # 上面那条 still 会红，但这条能直接说出错在哪。
    assert "¥<" not in text, f"符号排到了 `<` 前面：{text}"
    assert cost_results["不足一分钱美元"]["cost"] == "<$0.01"


def test_an_unknown_currency_still_does_not_get_a_symbol(cost_results):
    """认不出的币种**不猜符号** —— `<` 那一条支路也一样（不许拼成 `<¥0.01`）。"""
    assert cost_results["不足一分钱且认不出币种"]["cost"] == "<0.01 XYZ"


def test_a_normal_amount_is_untouched_by_that_rule(cost_results):
    """没有 `<` 的金额走原路：符号在前、数字原样。"""
    assert cost_results["正常金额"]["cost"] == "¥1.23"
    assert cost_results["认不出币种的正常金额"]["cost"] == "1.23 XYZ"


def test_a_missing_amount_stays_missing_instead_of_becoming_zero(cost_results):
    assert cost_results["没有金额"]["cost"] is None
    assert cost_results["没有费用对象"]["cost"] is None


def test_the_script_is_referenced_by_all_three_templates():
    """三份模板都要引它 —— 少引一份，那份抽屉就永远不显示这一行（且不会报错）。"""
    for name in TEMPLATES:
        source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert "js/ai_usage_line.js" in source, f"{name} 没有引用 ai_usage_line.js"


def test_each_template_calls_it_on_both_paths():
    """`/latest` 与 SSE 的 `result` 两条路都要喂它。

    只挂一条的后果很隐蔽：刷新页面看得见消耗、刚跑完的分析看不见（或反过来），
    而两处代码离得很远，不会有人同时看。
    """
    for name in TEMPLATES:
        source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert source.count("renderAiUsageLine(") >= 4, (
            f"{name} 的调用点只有 {source.count('renderAiUsageLine(')} 处："
            "「无结果/进行中」要隐藏、「有结果」要显示，两个路径各一次"
        )
        assert "payload.usage, payload.run_id" in source, (
            f"{name} 的 SSE 分支没有把 usage 与 run_id 一起传进去（明细按钮会消失）"
        )
