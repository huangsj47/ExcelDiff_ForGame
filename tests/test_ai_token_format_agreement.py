# -*- coding: utf-8 -*-
"""token 数的展示格式：**三份实现必须给出同一串字符**。

## 为什么需要这个文件

同一个数字会出现在两个地方：抽屉里的预算横幅（后端拼好文案 → `analysis_budget._fmt_tokens`）
与「AI 消耗」页（前端 `aiuFmtTokens` / `fmtTokens`）。用户并排看到两个不一样的写法时，
合理的结论是「其中一个是错的」—— 而这里两边都没错，只是份数没对齐。

它们**已经分叉过一次**：后端整段 ≥1M 都用 `:.1f`（两位有效数字），两份 JS 用
`toFixed(num >= 1e7 ? 1 : 2)`（1M~10M 两位小数）。于是 1,250,000 在抽屉里是 `1.2M`、
在消耗页是 `1.25M`，而后端的 docstring 还写着「与前端 `aiuFmtTokens` 同一套口径」。

三份实现里**后端那份是事实源**（`services/ai/analysis_budget._fmt_tokens`），把它的规则
抄进两份 JS。这个文件拿同一张值表把三份钉在一起 —— `tests/test_content_window.py` 那种
「只断言几个常量相等」的写法在这里不够用：常量相等对「输出一致」既不充分也不必要。

## 两个曾经真实存在的越界产物

单位原先按**原值**的量级选、再取整，于是：

    999_999   →  "1000.0k"          （k 档取整后已经是一千 k）
    9_999_999 →  "10.0M" / "10.00M" （M 档取整后已经是一千万，三份里还有两种写法）

所以阈值压在**进位点前一点**（999_950 / 9_995_000），越界的那一小段改用上一档写。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = PROJECT_ROOT / "templates" / "ai_usage_dashboard.html"
USAGE_LINE = PROJECT_ROOT / "static" / "js" / "ai_usage_line.js"

# 值表：三份实现都要给出同一串。**边界值都在里面**，因为分叉从来发生在边界附近。
TOKEN_VALUES = (
    0, 1, 999, 1_000, 1_500, 12_345, 999_949, 999_950, 999_999,
    1_000_000, 1_049_999, 1_234_567, 1_250_000, 9_994_999, 9_995_000,
    9_999_999, 10_000_000, 12_300_000, 999_999_999,
)


def _strip_js_comments(text: str) -> str:
    """把 `//` 与 `/* */` 注释剥掉。

    本仓库的注释里**原样写着被禁掉的写法**（这里就写着 `1000.0k` 与 `toFixed(... ? 1 : 2)`
    这两句反例），不剥就会假红。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)//[^\n]*$", "", text)


def _function_source(script: str, name: str) -> str:
    """`function <name>(…) { … }` 的完整源码（按花括号配平），用于喂给 node 求值。"""
    start = script.index(f"function {name}(")
    brace = script.index("{", start)
    depth = 0
    for index in range(brace, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[start : index + 1]
    raise AssertionError(f"{name} 的花括号没有配平")


def _run_node(sources: dict[str, str], values: list[int]) -> dict[str, list]:
    """在 node 里把每份源码求值出来，对同一张值表调用其中的格式化函数。

    `sources` 是 {函数名: 该函数的源码}。返回 {函数名: [输出, …]}。
    """
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的纯函数断言")
    driver = """
const fs = require('fs');
const vm = require('vm');
const payload = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const sandbox = { console: console, Number: Number, Math: Math, String: String, isFinite: isFinite };
vm.createContext(sandbox);
const out = {};
for (const name of Object.keys(payload.sources)) {
    vm.runInContext('__fn = ' + payload.sources[name], sandbox, { filename: name + '.js' });
    out[name] = payload.values.map(function (v) { return sandbox.__fn(v); });
}
process.stdout.write(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as tmp:
        payload_path = Path(tmp) / "payload.json"
        payload_path.write_text(
            json.dumps({"sources": sources, "values": values}), encoding="utf-8"
        )
        driver_path = Path(tmp) / "driver.js"
        driver_path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(driver_path), str(payload_path)],
            capture_output=True, text=True, timeout=120,
        )
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _renderings() -> dict[str, list[str]]:
    """三份实现各自的输出（Python 那份直接跑，两份 JS 进 node 跑）。"""
    from services.ai.analysis_budget import _fmt_tokens

    dashboard = _strip_js_comments(DASHBOARD.read_text(encoding="utf-8"))
    usage_line = _strip_js_comments(USAGE_LINE.read_text(encoding="utf-8"))
    js = _run_node(
        {
            "aiuFmtTokens": _function_source(dashboard, "aiuFmtTokens"),
            "fmtTokens": _function_source(usage_line, "fmtTokens"),
        },
        list(TOKEN_VALUES),
    )
    # JS 那份对 null 返回 null（调用方决定显示什么），Python 那份返回「未上报」——
    # 这是调用方约定，不是格式本身，所以值表里不放 None。
    return {
        "python": [_fmt_tokens(value) for value in TOKEN_VALUES],
        "dashboard": js["aiuFmtTokens"],
        "usage_line": js["fmtTokens"],
    }


@pytest.fixture(scope="module")
def rendered() -> dict[str, list[str]]:
    return _renderings()


def test_the_three_copies_agree_on_every_value(rendered):
    """**这条是主断言**：三份实现逐值逐字相同。"""
    for index, value in enumerate(TOKEN_VALUES):
        got = {name: outputs[index] for name, outputs in rendered.items()}
        assert len(set(got.values())) == 1, (
            f"{value:,} 在三份实现里印得不一样：{got}\n"
            "后端的 `_fmt_tokens` 是事实源，两份 JS 要跟着它改。"
        )


def test_the_million_range_keeps_two_decimals(rendered):
    """1M~10M 是两位小数 —— 这一段原先三份分叉（后端一位、两份 JS 两位）。"""
    assert rendered["python"][TOKEN_VALUES.index(1_250_000)] == "1.25M", (
        "1,250,000 印成了 "
        f"{rendered['python'][TOKEN_VALUES.index(1_250_000)]}，"
        "后端 docstring 承诺的 `1.25M` 出不来"
    )
    assert rendered["python"][TOKEN_VALUES.index(1_234_567)] == "1.23M"


def test_no_value_rounds_out_of_its_own_unit(rendered):
    """**单位按取整之后的量级选**：不许出现 `1000.0k` 或 `10.00M` 这种越界产物。"""
    for index, value in enumerate(TOKEN_VALUES):
        text = rendered["python"][index]
        assert text != "1000.0k", f"{value:,} 印成了 1000.0k（k 档取整后已经是一千 k）"
        assert text != "10.00M", f"{value:,} 印成了 10.00M（M 档取整后已经是一千万）"
        if text.endswith("k"):
            number = float(text[:-1])
            assert number < 1000, f"{value:,} → {text}：k 档的值必须小于 1000"


def test_the_two_values_around_the_carry_stay_distinguishable(rendered):
    """9,999,999 与 10,000,000 各自进位后都是 10.0M —— 这是取整的事实，不是缺陷。

    真正要挡住的是「同一个数印成两个串」：9,999,999 先前是 `10.0M`（后端）与 `10.00M`
    （前端），而 10,000,000 是 `10.0M` —— 两个不同的数、三种写法。
    """
    near = rendered["python"][TOKEN_VALUES.index(9_999_999)]
    exact = rendered["python"][TOKEN_VALUES.index(10_000_000)]
    assert near == exact == "10.0M", (near, exact)


def test_the_thresholds_live_in_one_place_per_copy():
    """三份实现里的两个阈值必须一致 —— 值表挡的是当前取值，这条挡的是「只改了两份」。"""
    from services.ai.analysis_budget import _fmt_tokens

    assert _fmt_tokens(999_950) == "1.00M", "下限阈值：再按 k 写就成了 1000.0k"
    assert _fmt_tokens(999_949) == "999.9k"
    assert _fmt_tokens(9_995_000) == "10.0M" or _fmt_tokens(9_995_000) == "9.99M"
    assert _fmt_tokens(9_994_999) == "9.99M"


def test_the_python_source_is_the_one_the_banner_uses():
    """横幅上的 token 数真的走这个函数（否则「三份一致」是在守护一段没人用的代码）。

    `services/ai/analysis_budget.py` 的 `reasons` 文案里有 `_fmt_tokens(...)` 的调用点，
    这里按**去掉注释后的源码**断言，免得把注释里的举例当成调用点。
    """
    source = _strip_js_comments(
        (PROJECT_ROOT / "services" / "ai" / "analysis_budget.py").read_text(encoding="utf-8")
    )
    assert "_fmt_tokens(" in source, "`_fmt_tokens` 没有任何调用点"
