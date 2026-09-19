# -*- coding: utf-8 -*-
"""「导出 md」那个链接的状态机 —— 用 node 真跑 `static/js/ai_report_export.js`。

## 为什么要真跑

这段代码只做一件事：**决定那个链接指向哪一次运行、或者干脆别显示**。它坏掉的方式全都
「看着像成功」：

* 链接指着**上一次**的结论（用户以为导的是屏幕上这份）；
* 没有可导出的结论时链接还在（点下去换来一个 409 的 JSON，用户看到的是「下载了一个
  报错」）；
* 撤下去的时候只加了 `hidden` 而没撤 `href`（右键「复制链接」拿到的是上一次的地址）。

静态断言只能证明这些字符串出现了，证明不了状态机走得对。所以按仓库既有做法
（`tests/test_ai_drawer_tabs_frontend.py`）把真函数放进带假 DOM 的 node 沙箱里跑。

## 假 DOM

只实现这段代码用到的那几样：`getElementById` / `setAttribute` / `getAttribute` /
`removeAttribute` / `hidden`。`href` 就是 `setAttribute('href', ...)` 写进 `_attrs`，
所以断言「链接指向哪一次」看的是 `_attrs.href`（与我给模板写的不写 `download` 那条
一起，构成「交给浏览器下载」的整套约定）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_report_export.js"
TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

// 三份模板里那两个 id —— 硬写在这里是**故意的**：它们是模板与模块的共同契约，
// 改名就该让这个测试红。
var COMMIT_LINK = 'aiExportMdLink';
var WEEKLY_LINK = 'weeklyAiExportMdLink';

var els = {};

function makeEl(id) {
    return {
        id: id, hidden: false, _attrs: {},
        setAttribute: function (k, v) { this._attrs[k] = String(v); },
        getAttribute: function (k) {
            return this._attrs[k] === undefined ? null : this._attrs[k];
        },
        removeAttribute: function (k) { delete this._attrs[k]; }
    };
}

function seedEls() {
    els = {};
    els[COMMIT_LINK] = makeEl(COMMIT_LINK);
    els[WEEKLY_LINK] = makeEl(WEEKLY_LINK);
}
seedEls();

var sandbox = { window: {}, console: console };
sandbox.window = sandbox;
sandbox.document = {
    // 与真 DOM 一样：没有的 id 返回 null（**不按需造**）——模块要是把 id 认错了，
    // 这里会安静地什么都不画（那正是要断言的情形），而不是对着空气出错。
    getElementById: function (id) { return els[id] || null; }
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
var api = sandbox.AiReportExport;

function link() { return els[api.state().linkId]; }

var OP = {
    initCommit: function () { api.init({ linkId: COMMIT_LINK }); },
    initWeekly: function () { api.init({ linkId: WEEKLY_LINK }); },
    settled: function (id) { api.track({ runId: id, status: 'succeeded' }); },
    failed: function (id) { api.track({ runId: id, status: 'failed' }); },
    running: function (id) { api.track({ runId: id, status: 'running' }); },
    none: function () { api.track({ runId: null }); },
    noStatus: function (id) { api.track({ runId: id }); }
};

function snap() {
    var node = link() || {};
    return {
        hidden: !!node.hidden,
        href: node.getAttribute ? node.getAttribute('href') : null,
        runId: api.currentRunId(),
        state: api.state()
    };
}

var cases = __CASES__;
var results = cases.map(function (item) {
    // 每个场景从干净的模块状态开始：重新加载一遍脚本（模块内部有状态）。
    seedEls();
    vm.runInContext(fs.readFileSync(__SCRIPT__, 'utf8'), sandbox);
    api = sandbox.AiReportExport;
    var snaps = item.ops.map(function (op) {
        var parts = op.split(':');
        OP[parts[0]](parts.length > 1 ? Number(parts[1]) : undefined);
        return snap();
    });
    return {
        name: item.name,
        snaps: snaps,
        // 模板里那个 id 是不是真的存在于这个模块的语义里（`state().linkId`）。
        linkId: api.state().linkId
    };
});

var pure = [];
__PURE__.forEach(function (item) {
    pure.push({name: item.name, canExport: api.canExport(item.status), href: api.hrefFor(item.runId)});
});

process.stdout.write(JSON.stringify({cases: results, pure: pure}));
"""


def _run(cases: list, pure: list) -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的导出链接状态机断言")
    driver = (
        DRIVER.replace("__SCRIPT__", json.dumps(str(SCRIPT)))
        .replace("__CASES__", json.dumps(cases, ensure_ascii=False))
        .replace("__PURE__", json.dumps(pure, ensure_ascii=False))
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "driver.js"
        path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def run() -> dict:
    return _run(
        [
            # 1. 页面刚打开：还没有结论 → 链接是收起来的（初始 hidden 在模板里）。
            {"name": "初始", "ops": ["initCommit"]},
            # 2. 读到一条有结论的运行 → 显示，并指向**那一次**。
            {"name": "有结论", "ops": ["initCommit", "settled:41"]},
            # 3. 失败的运行：没有正文 → 不显示一个点下去必然 409 的按钮。
            {"name": "失败", "ops": ["initCommit", "settled:41", "failed:42"]},
            # 4. 正在跑：结论还没出来。
            {"name": "进行中", "ops": ["initCommit", "settled:41", "running:42"]},
            # 5. 新一次开跑（还没拿到运行号）→ 收起来，**且 href 一并撤掉**。
            {"name": "开跑", "ops": ["initCommit", "settled:41", "none"]},
            # 6. 换目标（另一个运行号）→ 链接跟着换。
            {"name": "换一次运行", "ops": ["initCommit", "settled:41", "settled:42"]},
            # 7. 同一条重复 track（点「刷新结果」）→ 结果不变。
            {"name": "重复 track", "ops": ["initCommit", "settled:41", "settled:41"]},
            # 8. 另一份模板的那个 id（周版本 / 合并视图共用）。
            {"name": "周版本那份", "ops": ["initWeekly", "settled:7"]},
            # 9. 状态缺失（老接口没给 status）→ 当作不可导出，不显示。
            {"name": "没有状态", "ops": ["initCommit", "noStatus:41"]},
            # 10. 失败之后再跑成 → 又能导了。
            {"name": "失败后重跑成", "ops": ["initCommit", "failed:42", "settled:43"]},
        ],
        [
            {"name": "成功", "status": "succeeded", "runId": 9},
            {"name": "失败", "status": "failed", "runId": 9},
            {"name": "进行中", "status": "running", "runId": 9},
            {"name": "排队中", "status": "pending", "runId": 9},
            {"name": "降级", "status": "degraded", "runId": 9},
            {"name": "空", "status": "", "runId": 9},
            {"name": "缺", "status": None, "runId": 9},
        ],
    )


def _by_name(run: dict) -> dict:
    return {item["name"]: item for item in run["cases"]}


# --------------------------------------------------------------------------
#  链接指向哪一次
# --------------------------------------------------------------------------
def test_a_concluded_run_makes_the_link_visible_and_pointing_at_that_run(run):
    last = _by_name(run)["有结论"]["snaps"][-1]

    assert last["hidden"] is False
    assert last["href"] == "/ai-analysis/runs/41/report.md"
    assert last["runId"] == 41


def test_the_link_follows_the_run_that_is_on_screen(run):
    """换一条运行（点「刷新结果」拿到新的一次）→ 链接跟着换，不是留在上一次。"""
    snaps = _by_name(run)["换一次运行"]["snaps"]

    assert snaps[1]["href"].endswith("/runs/41/report.md")
    assert snaps[2]["href"].endswith("/runs/42/report.md")


def test_tracking_the_same_run_twice_changes_nothing(run):
    snaps = _by_name(run)["重复 track"]["snaps"]

    assert snaps[1] == snaps[2]


# --------------------------------------------------------------------------
#  什么时候它不许出现
# --------------------------------------------------------------------------
def test_a_failed_run_hides_the_link(run):
    """失败没有报告正文 —— 显示一个点下去必然 409 的按钮是在骗人。"""
    last = _by_name(run)["失败"]["snaps"][-1]

    assert last["hidden"] is True
    assert last["runId"] is None


def test_a_running_run_hides_the_link(run):
    last = _by_name(run)["进行中"]["snaps"][-1]
    assert last["hidden"] is True


def test_a_new_run_removes_the_href_not_just_the_visibility(run):
    """**`href` 必须一并撤掉**：留着一个上一次的地址，右键「复制链接」拿到的是别人的结论。"""
    last = _by_name(run)["开跑"]["snaps"][-1]

    assert last["hidden"] is True
    assert last["href"] is None


def test_a_missing_status_counts_as_not_exportable(run):
    """接口没给状态（老记录 / 载荷变形）→ 不显示。宁可不给，也不给一个会 409 的链接。"""
    last = _by_name(run)["没有状态"]["snaps"][-1]
    assert last["hidden"] is True


def test_it_comes_back_after_a_later_run_succeeds(run):
    last = _by_name(run)["失败后重跑成"]["snaps"][-1]

    assert last["hidden"] is False
    assert last["href"].endswith("/runs/43/report.md")


# --------------------------------------------------------------------------
#  两份模板的那个 id
# --------------------------------------------------------------------------
def test_the_module_keeps_the_link_id_it_was_initialised_with(run):
    assert _by_name(run)["有结论"]["linkId"] == "aiExportMdLink"
    assert _by_name(run)["周版本那份"]["linkId"] == "weeklyAiExportMdLink"


def test_the_weekly_link_works_the_same_way(run):
    last = _by_name(run)["周版本那份"]["snaps"][-1]
    assert last["hidden"] is False
    assert last["href"] == "/ai-analysis/runs/7/report.md"


# --------------------------------------------------------------------------
#  纯函数：谁算「有结论」
# --------------------------------------------------------------------------
def test_only_a_succeeded_run_is_exportable(run):
    """**这是界面这一层的判定**，最终裁决在服务端（`report_document.is_exportable`）——
    它还要看有没有报告正文，而那是界面拿不到的信息（只有 `/latest` 里的 `status`）。
    两边只要有一边说不，就不会给一个 409 的链接。"""
    pure = {item["name"]: item for item in run["pure"]}

    assert pure["成功"]["canExport"] is True
    for name in ("失败", "进行中", "排队中", "降级", "空", "缺"):
        assert pure[name]["canExport"] is False, f"{name} 不该被当成可导出"


def test_the_href_is_the_export_endpoint_of_that_run(run):
    pure = {item["name"]: item for item in run["pure"]}
    assert pure["成功"]["href"] == "/ai-analysis/runs/9/report.md"


# --------------------------------------------------------------------------
#  模板那一侧：链接本身
# --------------------------------------------------------------------------
def _template(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def test_every_drawer_has_an_export_link():
    ids = {
        "templates/commit_diff_new.html": 'id="aiExportMdLink"',
        "templates/weekly_version_diff.html": 'id="weeklyAiExportMdLink"',
        "templates/merged_project_view.html": 'id="weeklyAiExportMdLink"',
    }
    for name, marker in ids.items():
        assert marker in _template(name), f"{name} 的 footer 里没有导出链接"


def test_the_link_does_not_carry_a_download_attribute():
    """**空的 `download` 会让浏览器按 URL 末段命名**（`report.md`），把服务端拼好的
    `AI分析报告-<项目>-<目标>-20260912.md` 盖掉。文件名归 `Content-Disposition`。"""
    for name in TEMPLATES:
        source = _template(name)
        anchor = [
            line for line in source.splitlines() if "ExportMdLink" in line and "<a " in line
        ]
        assert len(anchor) == 1, f"{name} 里的导出链接不止一个/找不到"
        assert "download" not in anchor[0], f"{name} 的导出链接带了 download 属性"


def test_the_link_starts_hidden_and_has_a_text_label():
    """初始 hidden（`paint()` 之前不许露出来）；图标是 aria-hidden 的，所以必须有一个
    文字标签 —— 否则读屏里这是个没有名字的链接。"""
    for name in TEMPLATES:
        source = _template(name)
        anchor = next(
            line for line in source.splitlines() if "ExportMdLink" in line and "<a " in line
        )
        assert "hidden>" in anchor, f"{name} 的导出链接不是初始隐藏的"
        assert "导出 md" in source
        assert '<i class="fas fa-file-arrow-down" aria-hidden="true"></i>' in source


def test_every_drawer_loads_the_module():
    for name in TEMPLATES:
        assert "js/ai_report_export.js" in _template(name), f"{name} 没有引导出那个模块"


def test_every_place_that_decides_the_displayed_run_also_tracks_the_export():
    """**漏一处就是「下载下来的是上一次的结论」**（看着像成功的那种错）。

    三份模板的分支结构不同（合并视图那份还要在 SSE 的 `result` 里补一次 —— 它跑完不再
    回读 `/latest`），所以这里只钉「每个分支都提到过」这件事。
    """
    for name in TEMPLATES:
        source = _template(name)
        assert source.count("AiReportExport.track(") >= 5, f"{name} 的 track 调用太少"
        assert "status: 'failed'" in source, f"{name} 没有在失败分支里收起链接"
        assert "status: 'running'" in source, f"{name} 没有在进行中分支里收起链接"
        assert "runId: null" in source, f"{name} 没有在「没有结论」时收起链接"
        assert "AiReportExport.init(" in source, f"{name} 没有初始化导出链接"
