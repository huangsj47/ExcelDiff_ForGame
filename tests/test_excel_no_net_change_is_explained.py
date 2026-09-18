# -*- coding: utf-8 -*-
"""表在这个区间里没有净变更时，页面必须**说出来**，而不是渲染一张空表。

## 缺陷形态（线上真实报障）

`http://10.226.98.33:8002/weekly-version-config/1/file-full-diff?file_path=config%2F90_tutorial%2F90_新手关卡流程.xlsx`
打开后「没有任何修改 diff」，看上去像平台坏了。

真实原因是**这张表在这个版本区间里确实没有净变更**：它在区间内被提交了 3 次
（`94b31e17` → `c1e029a9` → `49251c62`），而最后一次把内容改回了原样 ——
用真值仓库独立核对过，区间首尾的单元格指纹相同（128 行全等），载荷里
`stats` 三项全 0、`total_rows_current` 与 `total_rows_previous` 都是 126。
所以 diff **算得没错**。

错的是渲染：载荷里 `rows` 是**空数组**，而空数组在 JS 里是**真值**，
`if (!sheetData.rows)` 这个「无数据」分支拦不住它；headers 又非空，
第二个「没有表头也没有数据行」的分支也拦不住。于是落到「渲染表格」那一步，
产出一张**只有表头、一行都没有**的表 —— 用户看到的就是一片空白。

## 这一组守什么

1. `noNetChangeHtml` 对三种载荷给三种说法，**不合并成一句**：统计说有变更却一行都
   拿不出来，是平台自己的问题，说成「没有变更」等于把 bug 藏起来。
2. 两个页面（周版本完整 diff / 多版本合并）都要在渲染表格**之前**拦这一手。
3. 拦截条件必须带上 `!noticesHtml`：只有表级/列级变更（整表增删、改列名）时，
   提示行本身就是内容，不能被「没有净变更」顶掉。
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

# 两个页面各自有一份同形的实现（本仓库既有约定：这几段 Excel 渲染逻辑本来就是
# 每个模板一份，改动要一起改）。
PAGES = [
    "templates/weekly_version_full_diff.html",
    "templates/merge_diff.html",
]
# 调用点所在的函数（周版本页 / 合并页）
CALLERS = {
    "templates/weekly_version_full_diff.html": "showWeeklyExcelSheet",
    "templates/merge_diff.html": "showMergedExcelSheet",
}

_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function escapeHtml(text) {
    return String(text === null || text === undefined ? '' : text)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
// 把函数声明变成函数表达式求值，拿到这个函数本身
const noNetChangeHtml = eval('(' + src.replace('function noNetChangeHtml', 'function') + ')');
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
process.stdout.write(JSON.stringify(
    cases.map(c => ({name: c.name, html: noNetChangeHtml(c.sheet)}))
));
"""

# 用户报的那张表的真实载荷（值取自线上 /file-full-diff-data 返回的 JSON）
REPORTED = {
    "rows": [],
    "headers": ["CfgTutorialFlow", "Unnamed: 1", "步骤id", "流程id", "注释", "操作类型", "操作参数", "描述"],
    "stats": {
        "total_rows_current": 126,
        "total_rows_previous": 126,
        "added": 0,
        "removed": 0,
        "modified": 0,
    },
}


def _read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def _strip_js_comments(source: str) -> str:
    """剥掉 `//` 行注释与 `/* */` 块注释。

    本仓库的注释里经常**引用**被断言的那些字符串（这一组的注释就写着
    `if (!sheetData.rows)`、`rows` 等等），不剥注释的静态断言会被自己的注释喂饱 ——
    这个坑在本仓库已经踩过三次。
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)//[^\n]*$", "", source)


def _extract_function(source: str, name: str) -> str:
    """按大括号配对抠出一个函数的源码（模板里混着 Jinja 与 HTML，没法 import）。

    两种写法都要认：`function f(...) {` 与 `window.f = function(...) {`
    —— 周版本页用的是后者，只认前者会在这里得到一个 `substring not found`。
    """
    start = -1
    for marker in (f"function {name}(", f"{name} = function("):
        found = source.find(marker)
        if found != -1:
            start = found
            break
    if start == -1:
        raise AssertionError(f"模板里找不到 {name}")

    depth = 0
    for index in range(source.index("{", start), len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{name} 的大括号没有配对 —— 抠出来的源码是半截的")


@pytest.fixture(scope="module")
def rendered() -> dict:
    """把两个模板里的 `noNetChangeHtml` 源码放进 node 跑，拿到真实输出。"""
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实渲染断言")

    cases = [
        {"name": "reported", "sheet": REPORTED},
        {
            "name": "inconsistent",
            "sheet": {"rows": [], "headers": ["a"], "stats": {"added": 5, "removed": 0, "modified": 0}},
        },
        {"name": "no_stats", "sheet": {"rows": [], "headers": ["a"]}},
        {
            "name": "no_row_counts",
            "sheet": {"rows": [], "headers": ["a"], "stats": {"added": 0, "removed": 0, "modified": 0}},
        },
        {
            "name": "counts_disagree",
            "sheet": {
                "rows": [],
                "headers": ["a"],
                "stats": {"total_rows_current": 130, "total_rows_previous": 126,
                          "added": 0, "removed": 0, "modified": 0},
            },
        },
    ]

    out: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "driver.js").write_text(_DRIVER, encoding="utf-8")
        (tmpdir / "cases.json").write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

        for relative in PAGES:
            source = _read(relative)
            (tmpdir / "fn.js").write_text(
                _extract_function(source, "noNetChangeHtml"), encoding="utf-8"
            )
            proc = subprocess.run(
                ["node", str(tmpdir / "driver.js"), str(tmpdir / "fn.js"), str(tmpdir / "cases.json")],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert proc.returncode == 0, f"{relative} 的函数跑不起来：\n{proc.stdout}\n{proc.stderr}"
            out[relative] = {item["name"]: item["html"] for item in json.loads(proc.stdout)}
    return out


@pytest.mark.parametrize("page", PAGES)
def test_the_reported_file_gets_an_explanation(rendered, page):
    """用户报的那张表：要说清「没有净变更」，并且把行数报出来。

    只写一句「没有变更」是不够的 —— 用户已经看到文件出现在改动清单里，
    不解释「为什么它在清单里却没有内容」，他只会以为平台漏了。
    """
    html = rendered[page]["reported"]

    assert "没有净变更" in html
    assert "126" in html, "没有给出前后行数，用户无法判断是「真没变」还是「平台没读到」"
    assert "新增、删除、变更均为 0" in html


@pytest.mark.parametrize("page", PAGES)
def test_an_inconsistent_payload_is_reported_as_a_platform_problem(rendered, page):
    """统计说改了 5 处、却一行都拿不出来 —— 这是平台自己的问题，**不许说成没有变更**。

    这两个分支合并成一句的后果：真正的差异计算 bug 会被「没有变更」这句话盖住，
    而「没有变更」正是评审者最不会去追问的一句话。
    """
    html = rendered[page]["inconsistent"]

    assert "5 处变更" in html
    assert "没有净变更" not in html, "统计与内容不一致时不许说「没有净变更」"
    assert "反馈给维护者" in html


@pytest.mark.parametrize("page", PAGES)
def test_a_payload_without_stats_makes_no_claim(rendered, page):
    """老缓存里没有 `stats`：无从判断，就不要断言「没有变更」。"""
    html = rendered[page]["no_stats"]

    assert "没有变更行" in html
    assert "没有净变更" not in html
    assert "均为 0" not in html


@pytest.mark.parametrize("page", PAGES)
def test_missing_row_counts_still_explains_without_them(rendered, page):
    """有 stats 但没有前后行数时，退化成不带行数的说法，而不是渲染出 `undefined`。"""
    html = rendered[page]["no_row_counts"]

    assert "没有净变更" in html
    assert "undefined" not in html
    assert "NaN" not in html


@pytest.mark.parametrize("page", PAGES)
def test_two_numbers_are_both_reported_when_they_disagree(rendered, page):
    """两边行数不同却一个变更计数都没有 —— 把两个数都念出来。

    写成「前后都是 N 行」会把差异抹平（拿 `total_rows_current` 覆盖掉），
    而这条恰恰是唯一能暴露「统计与内容不一致」的线索。
    """
    html = rendered[page]["counts_disagree"]

    assert "126" in html and "130" in html, "行数对不上时只报了一个数"
    assert "前后都是" not in html


# --------------------------------------------------------------------------
# 接线：两个页面都必须在渲染表格之前拦这一手
# --------------------------------------------------------------------------


# 每个页面「把表格渲染出去」的那一行。2026 结构重构后表体由共享模块渲染，
# 所以锚点从各自拼 `tableHtml` 换成了对共享实现的调用 —— 换的是**锚点的写法**，
# 不是断言本身：「拦截必须在渲染之前」这条要求照旧。
TABLE_RENDER_ANCHORS = {
    "templates/weekly_version_full_diff.html": "ExcelDiffTable.mountSheetTable",
    "templates/merge_diff.html": "ExcelDiffTable.mountSheetTable",
}


@pytest.mark.parametrize("page", PAGES)
def test_the_page_intercepts_before_rendering_the_table(page):
    """光有函数不算数 —— 它得在渲染表格**之前**被调用并 return。

    不拦的下场就是线上那个样子：一张只有表头、一行都没有的表。所以这里同时断言
    「调用点在渲染之前」与「调用后直接 return」，只断言函数存在是不够的。
    """
    source = _strip_js_comments(_read(page))
    caller = _extract_function(source, CALLERS[page])

    anchor = TABLE_RENDER_ANCHORS[page]
    assert anchor in caller, (
        f"{page}：渲染入口里找不到「把表格渲染出去」的那一步（{anchor}）—— "
        f"用例失去了参照点，先确认这一页还在渲染表格"
    )
    guard_at = caller.index("noNetChangeHtml(sheetData)")
    table_at = caller.index(anchor)
    assert guard_at < table_at, "拦截写在渲染表格后面，等于没拦"

    between = caller[guard_at:table_at]
    assert "return" in between, "拦截之后没有 return，还会继续往下渲染空表"


@pytest.mark.parametrize("page", PAGES)
def test_the_intercept_requires_having_no_other_notices(page):
    """条件必须带上 `!noticesHtml`。

    只有表级变更（整张表新增/删除）或列级变更（改列名）时，行数本来就是 0 ——
    而那两件事**就是这次改动的全部内容**，提示行必须照常显示。漏掉这个条件的后果是
    把「整张表被删了」显示成「没有净变更」，正好说反。
    """
    source = _strip_js_comments(_read(page))
    caller = _extract_function(source, CALLERS[page])

    assert "!(sheetData.rows || []).length && !noticesHtml" in caller, (
        "拦截条件漏了 !noticesHtml：表级/列级变更会被误报成「没有净变更」"
    )
