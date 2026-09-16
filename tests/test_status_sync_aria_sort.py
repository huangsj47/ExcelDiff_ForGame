"""表头排序按钮的 aria-sort 同步（WCAG 4.1.2 名称/角色/值）。

## 缺陷形态

`templates/status_sync_management.html` 的映射表有三列可排序，表头里的按钮是
**纯图标**（`<i class="fas fa-sort">`）加一个 `aria-label="按…排序"`。
点击后只换了图标、重排了数据 —— `aria-sort` 从头到尾没有出现过。

结果是视力正常的用户看到箭头变了，读屏用户听到的仍然是「按文件路径排序 按钮」，
**无从知道当前按哪一列、是升序还是降序**。`aria-sort` 是表头排序状态唯一的
无障碍表达，`aria-label` 表达不了它。

## 为什么要真的跑一遍 JS

这里断言的是「点完之后属性变成什么」，光看源码里有 `setAttribute('aria-sort'`
是不够的：把 `ascending` / `descending` 写反、或者忘了把其余列复位成 `none`，
纯文本断言都可能漏掉。

所以本文件优先用 **Node 直接执行模板里那段真实的 `syncAriaSort`**（配一个极小的
DOM 桩），逐条核对三个表头最终的属性值。没有 Node 的环境（CI 上不装 Node）
自动跳过这一条 —— 仓库本身是纯 pytest 项目，不该为此新增运行时依赖，
其余几条源码断言仍然生效。
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
TEMPLATE = REPO_ROOT / "templates" / "status_sync_management.html"

_JS_FUNCTION_RE = re.compile(
    r"function\s+syncAriaSort\s*\(\s*columnIndex\s*,\s*isAscending\s*\)\s*\{(.*?)\n\}",
    re.S,
)
_TH_RE = re.compile(r"<th\b([^>]*data-sort-column[^>]*)>(.*?)</th>", re.S)
_SORT_BUTTON_RE = re.compile(r'data-sort="(\d+)"')


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _function_body() -> str:
    match = _JS_FUNCTION_RE.search(_source())
    assert match, "模板里找不到 syncAriaSort 函数 —— 排序状态同步的实现被改动或删除了"
    return match.group(1)


def _headers() -> list[tuple[str, str]]:
    """[(th 属性串, th 内部 HTML)]，按模板出现顺序。"""
    return [(attrs, inner) for attrs, inner in _TH_RE.findall(_source())]


def test_sortable_headers_declare_a_neutral_initial_state():
    """初始必须是 `none`：还没点过任何一列时，「当前按 X 升序」是假信息。"""
    headers = _headers()
    assert len(headers) == 3, f"应有 3 个可排序表头，实际 {len(headers)} 个"
    columns = []
    for attrs, inner in headers:
        match = re.search(r'data-sort-column="(\d+)"', attrs)
        assert match, f"表头缺少 data-sort-column：{attrs.strip()}"
        columns.append(match.group(1))
        assert re.search(r'aria-sort="none"', attrs), (
            f"可排序表头必须带 aria-sort=\"none\" 作为初始值：{attrs.strip()}"
        )
        # th 上的列号与该列排序按钮的 data-sort 必须指向同一列，
        # 否则同步会把状态写到隔壁列上。
        button = _SORT_BUTTON_RE.search(inner)
        assert button, f"表头里没有带 data-sort 的排序按钮：{inner.strip()[:60]}"
        assert button.group(1) == match.group(1), (
            f"表头 data-sort-column={match.group(1)} 与按钮 data-sort={button.group(1)} 不一致"
        )
    assert columns == ["0", "1", "2"], columns


def test_sync_runs_before_the_table_is_repainted():
    """同步必须发生在 renderTable() 之前，且真的被调用、真的传了两个参数。"""
    source = _source()
    call = re.search(r"syncAriaSort\(columnIndex,\s*isAscending\)", source)
    assert call, "排序处理里没有调用 syncAriaSort(columnIndex, isAscending)"
    render = source.index("renderTable();", call.end())
    assert call.start() < render, "syncAriaSort 必须在 renderTable() 之前调用"


def test_sync_resets_the_other_columns_to_none():
    """同时只能有一列是当前排序列 —— 其余列必须复位成 none。"""
    body = _function_body()
    assert "none" in body, "syncAriaSort 没有处理「其余列复位」的分支"
    assert re.search(r"isTarget\s*\?\s*\(?\s*isAscending\s*\?", body), (
        "syncAriaSort 里没有「是被点的那列就按 isAscending 取升降序」的分支"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 才能真的执行这段 JS")
def test_sync_sets_the_right_state_when_actually_executed():
    """把模板里那段真实 JS 拿出来跑，逐条核对三个表头的最终属性。"""
    harness = """
const state = {};
const ths = [0, 1, 2].map((n) => ({
  getAttribute: (name) => (name === 'data-sort-column' ? String(n) : null),
  setAttribute: (name, value) => { state[n] = state[n] || {}; state[n][name] = value; },
}));
global.document = { querySelectorAll: (sel) => (sel === '[data-sort-column]' ? ths : []) };
__FUNCTION__
const out = [];
const snap = () => [0, 1, 2].map((n) => (state[n] || {})['aria-sort'] || null);
syncAriaSort(1, true);  out.push(['点击第 2 列 → 升序', snap()]);
syncAriaSort(1, false); out.push(['再点一次第 2 列 → 降序', snap()]);
syncAriaSort(2, true);  out.push(['改点第 3 列 → 第 2 列必须复位', snap()]);
syncAriaSort(0, false); out.push(['改点第 1 列 → 降序', snap()]);
console.log(JSON.stringify(out));
"""
    script = harness.replace("__FUNCTION__", "function syncAriaSort(columnIndex, isAscending) {" + _function_body() + "\n}")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "check.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            ["node", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
    assert result.returncode == 0, f"Node 执行失败：{result.stderr}"
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    # 未点过的列不是「没设置」，而是被显式写成 none —— 与模板里 th 的初始值一致。
    expected = [
        ["点击第 2 列 → 升序", ["none", "ascending", "none"]],
        ["再点一次第 2 列 → 降序", ["none", "descending", "none"]],
        ["改点第 3 列 → 第 2 列必须复位", ["none", "none", "ascending"]],
        ["改点第 1 列 → 降序", ["descending", "none", "none"]],
    ]
    assert observed == expected, (
        "排序状态同步的实际行为与预期不符：\n  "
        + "\n  ".join(f"{note}：得到 {got}，应为 {want}" for (note, got), (_, want) in zip(observed, expected))
    )
