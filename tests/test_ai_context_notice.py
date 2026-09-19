# -*- coding: utf-8 -*-
"""抽屉里那几行「这次不是正常跑完的」提示 —— 用 node 真跑 `static/js/ai_context_notice.js`。

## 为什么必须真跑

这一段的全部风险在**「有」与「没有」的判定**上，而它偏偏是「看一眼觉得对」的那种代码：

* 一次**正常跑完**的分析如果也追加一句「一切正常」，用户会以为平台替他检查过了 ——
  而那句话是没有依据的（引擎没说「没问题」，它只是没降级）。所以「没事」的那一支必须
  返回空串。
* `dropped_turns = 0`（压过 0 轮）与 `dropped_turns = 2`（压过 2 轮）如果走同一支，
  界面会在没压过的时候说「已压掉 0 轮历史」—— 一个凭空出现的说法。
* `overflow_recovered` 与 `context_overflow` 是两件不同的事：前者是「上游拒了、我们救回来了」，
  后者是「收尾产出的结论」，都要说，而且说法不同。

静态断言挡不住这类错误（`num(0)` 拼进字符串是合法的写法），所以按仓库既有的做法
（见 `tests/test_ai_usage_drawer_frontend.py`）：读进 node 真跑，断言**输出字符串**。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "static" / "js" / "ai_context_notice.js"

TEMPLATES = (
    "templates/commit_diff_new.html",
    "templates/weekly_version_diff.html",
    "templates/merged_project_view.html",
)


def _read(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


# ==========================================================================
# 一、真跑：文案与判定的映射
# ==========================================================================


def _run(cases: list) -> list:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的文案断言")
    driver = f"""
const fs = require('fs');
const vm = require('vm');
const sandbox = {{ window: {{}} }};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(SCRIPT))}, 'utf8'), sandbox);
const api = sandbox.AiContextNotice;
const cases = {json.dumps(cases, ensure_ascii=False)};
process.stdout.write(JSON.stringify(cases.map(function (item) {{
    return {{ name: item.name, notice: api.contextNotice(item.payload) }};
}})));
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "driver.js"
        path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def results() -> dict:
    return {
        item["name"]: item["notice"]
        for item in _run(
            [
                {"name": "正常跑完", "payload": {
                    "status": "succeeded", "degradation": "", "degradation_label": "",
                    "context": {"budget_note": "", "compaction": {
                        "events": 0, "dropped_turns": 0, "dropped_chars": 0,
                        "overflow_recovered": False,
                    }},
                }},
                {"name": "轮次用尽", "payload": {
                    "status": "degraded", "degradation": "rounds_exhausted",
                    "degradation_label": "轮次用尽，基于已有证据出结论",
                    "context": {},
                }},
                {"name": "上游拒绝过", "payload": {
                    "status": "degraded", "degradation": "context_overflow",
                    "degradation_label": "提示词超出模型上下文窗口，已压掉历史后收尾出结论",
                    "context": {"compaction": {
                        "events": 0, "dropped_turns": 0, "dropped_chars": 0,
                        "overflow_recovered": True,
                    }},
                }},
                {"name": "压过两轮", "payload": {
                    "status": "succeeded", "degradation": "", "degradation_label": "",
                    "context": {"compaction": {
                        "events": 1, "dropped_turns": 2, "dropped_chars": 11824,
                        "overflow_recovered": False,
                    }},
                }},
                {"name": "窗口按默认值", "payload": {
                    "status": "succeeded",
                    "context": {"budget_note": "上下文窗口 1,000,000 token（端点未声明窗口，按默认值）。"},
                }},
                {"name": "缺字段", "payload": {"status": "succeeded"}},
                {"name": "空载荷", "payload": None},
            ]
        )
    }


def test_a_clean_run_says_nothing_at_all(results):
    """**不许凭空产出「一切正常」。** 引擎没降级不等于模型说了没问题。

    多加一句「本次分析正常完成」，用户就会把它读成「平台确认过没有风险」——
    而平台确认的只是「流程跑完了」。
    """
    assert results["正常跑完"] == ""


def test_a_degradation_is_named_and_explained(results):
    notice = results["轮次用尽"]
    assert "未完整跑完" in notice
    assert "轮次用尽，基于已有证据出结论" in notice, "服务端给的标签必须原样出现"
    assert "证据" in notice.split("：", 1)[1], "只说「降级了」不够，要说清这意味着什么"


def test_a_rejected_request_says_the_report_was_written_from_trimmed_context(results):
    """上游拒过这件事要单独说：它影响的是**这份报告该怎么读**，不只是「出过一点小状况」。"""
    notice = results["上游拒绝过"]
    assert "超长" in notice and "拒绝" in notice
    assert "信息缺口" in notice, "要提醒用户去看报告里的信息缺口"


def test_a_compaction_reports_the_numbers(results):
    notice = results["压过两轮"]
    assert "2 轮" in notice and "11,824" in notice, notice


def test_each_warning_is_its_own_paragraph():
    """**这几句要分成三段，不能连成一句。**

    报告渲染器把段落内的单换行当软换行、用空格接起来（`ai-report-markdown.js` 的
    `flushPara`）—— 用单换行的话，「轮次用尽」+「压掉了 2 轮历史」+「窗口是按默认值算的」
    会连成一大句，而这是**互不相干的三件事**。一整块读起来像免责声明，用户一眼就跳过 ——
    而这一块的全部意义就是别被跳过。

    判据：两段之间恰好一个空行，不多不少（多一个空行渲染出来是双倍行距）。
    """
    blank = chr(10) * 2
    mixed = _run([{"name": "降级 + 压缩 + 默认窗口", "payload": {
        "status": "degraded", "degradation": "rounds_exhausted",
        "degradation_label": "轮次用尽，基于已有证据出结论",
        "context": {
            "budget_note": "上下文窗口 1,000,000 token（端点未声明窗口，按默认值）。",
            "compaction": {
                "events": 1, "dropped_turns": 2, "dropped_chars": 11824,
                "overflow_recovered": False,
            },
        },
    }}])[0]["notice"]

    assert mixed.count(blank) == 2, f"三段之间各要一个空行：{mixed!r}"
    assert blank * 2 not in mixed, "别多给空行（渲染出来是双倍行距）"
    assert mixed.count(chr(10)) == 4, f"只有两处空行，没有多余换行：{mixed!r}"

    # 反向：只有一句的时候不该出现空行（一个孤零零的段落分隔看着像漏了内容）。
    single = _run([{"name": "只降级", "payload": {
        "status": "degraded", "degradation": "rounds_exhausted",
        "degradation_label": "轮次用尽，基于已有证据出结论",
        "context": {},
    }}])[0]["notice"]

    assert blank not in single, f"只有一句也插了空行：{single!r}"


def test_zero_compaction_never_prints_a_zero(results):
    """**反向自检**：没压过的时候不许出现「压掉 0 轮」「省下 0 字」。

    一个 0 会让人以为「压过，但没省下什么」—— 而事实是「什么都没压」。
    """
    for name in ("正常跑完", "上游拒绝过"):
        assert "0 轮" not in results[name], f"{name} 里出现了凭空的数量"
        assert "0 字" not in results[name], f"{name} 里出现了凭空的数量"


def test_the_window_note_is_passed_through_verbatim(results):
    """窗口说明由服务端下发（含「窗口是按默认值算的」这种必须说清的事），前端只负责显示。"""
    assert "端点未声明窗口，按默认值" in results["窗口按默认值"]


@pytest.mark.parametrize("name", ["缺字段", "空载荷"])
def test_missing_fields_do_not_blow_up_or_invent_text(results, name):
    """老数据里没有 `context` 这个键（这次才加的），界面不能因此报错或乱说。"""
    assert results[name] == ""


# ==========================================================================
# 二、三份抽屉都接上了（静态守卫）
# ==========================================================================


@pytest.mark.parametrize("path", TEMPLATES)
def test_every_drawer_shows_the_notice(path):
    source = _read(path)

    assert "js/ai_context_notice.js" in source, f"{path} 没有引用共享的提示模块"
    assert "AiContextNotice.contextNotice(payload)" in source, (
        f"{path} 没有把结果交给共享模块 —— 降级了也不会有任何提示"
    )


@pytest.mark.parametrize("path", TEMPLATES)
def test_the_notice_text_has_a_single_source(path):
    """文案不许在模板里写死。三份模板各写一份，必然有一份漏改。"""
    source = _read(path)

    assert "未完整跑完" not in source, f"{path} 里写死了降级的文案"
    assert "已压成摘要" not in source, f"{path} 里写死了压缩的文案"


# ==========================================================================
# 三、提示必须跟着**报告**走，不能只活在流式缓冲区里
# ==========================================================================


def _compose(cases: list) -> list:
    """真跑 `withContextNotice`：它决定「正文 + 提示」怎么拼。"""
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的拼接断言")
    driver = f"""
const fs = require('fs');
const vm = require('vm');
const sandbox = {{ window: {{}} }};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(SCRIPT))}, 'utf8'), sandbox);
const api = sandbox.AiContextNotice;
const cases = {json.dumps(cases, ensure_ascii=False)};
process.stdout.write(JSON.stringify(cases.map(function (item) {{
    return {{ name: item.name, text: api.withContextNotice(item.text, item.payload) }};
}})));
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "driver.js"
        path.write_text(driver, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, "Node 执行失败：" + proc.stdout + proc.stderr
    return {item["name"]: item["text"] for item in json.loads(proc.stdout)}


DEGRADED = {
    "status": "degraded",
    "degradation": "rounds_exhausted",
    "degradation_label": "轮次用尽，基于已有证据出结论",
    "context": {},
}
CLEAN = {
    "status": "succeeded", "degradation": "", "degradation_label": "",
    "context": {"budget_note": "", "compaction": {
        "events": 0, "dropped_turns": 0, "dropped_chars": 0, "overflow_recovered": False,
    }},
}


@pytest.fixture(scope="module")
def composed() -> dict:
    return _compose(
        [
            {"name": "降级 + 正文", "text": "## 变更理解\n\n道具表删了一行。", "payload": DEGRADED},
            {"name": "降级 + 空正文", "text": "", "payload": DEGRADED},
            {"name": "正常 + 正文", "text": "## 变更理解", "payload": CLEAN},
            {"name": "正常 + 空正文", "text": "", "payload": CLEAN},
            {"name": "正文不是字符串", "text": None, "payload": DEGRADED},
        ]
    )


def test_the_report_body_is_kept_and_the_notice_follows_it(composed):
    """这一条是 `/latest` 重渲染那条路要的东西：**正文一个字不少，提示跟在后面**。

    以前提示只存在于 SSE 的流式缓冲区里，而同一个事件紧接着会拉一次 `/latest`、
    用落库正文**整体替换**缓冲区 —— 那几行刚画上去就没了；打开页面/刷新时更是
    一个字都不出现。而它说的正是「这份报告该不该当真」。
    """
    text = composed["降级 + 正文"]

    assert text.startswith("## 变更理解"), "正文必须在前、且一字不改"
    assert "道具表删了一行。" in text
    assert "未完整跑完" in text
    assert text.index("道具表删了一行。") < text.index("未完整跑完"), "提示要跟在正文后面"


def test_a_clean_run_composes_to_the_body_itself(composed):
    """正常跑完的那一次：拼出来的东西与落库正文**逐字相同**（不许多一个换行）。

    多一个尾部换行不算错，但「拼一次变一点」意味着这个函数不能用在「正文没变就不重渲染」
    的判断上 —— 那种地方一旦误判就会反复重画。
    """
    assert composed["正常 + 正文"] == "## 变更理解"
    assert composed["正常 + 空正文"] == ""


def test_an_empty_body_still_carries_the_notice(composed):
    """降级的那次可能没有正文（模型没吐 JSON，服务端也没留下东西）——

    这时提示**更不能丢**：屏幕上只有那句「暂无分析结果」，而真相是「这次没跑成」。
    """
    assert composed["降级 + 空正文"].strip().startswith("⚠️")


def test_a_non_string_body_does_not_leak_null(composed):
    """`response_text` 可能是 `null`（失败的那次），不能拼出 "null
⚠️ …"。"""
    assert "null" not in composed["正文不是字符串"]
    assert "未完整跑完" in composed["正文不是字符串"]


@pytest.mark.parametrize("path", TEMPLATES)
def test_every_drawer_renders_the_notice_along_with_the_persisted_report(path):
    """**每个「把落库正文画到屏幕上」的地方都要走这个拼接。**

    以前只有 SSE 那条路接了提示，而三份抽屉在同一个事件里都会再去拉一次 `/latest`、
    用 `response_text` 整体重渲染 —— 于是提示的存活时间就是从 `result` 事件到那次
    fetch 回来之间。断言写成「出现过 `contextNotice(payload)`」是挡不住的（它一直都在），
    所以要**正面要求**这个合并调用，并**反面禁止**裸渲染。
    """
    import re

    source = _read(path)
    body = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    body = re.sub(r"//[^\n]*", " ", body)

    assert "AiContextNotice.withContextNotice(result.response_text" in body, (
        f"{path} 直接渲染了落库正文，没带上降级/压缩提示 —— "
        "刷新页面后那次分析是「降级跑完的」这件事就消失了"
    )
    for setter in ("setAiReport", "setWeeklyAiReport"):
        assert f"{setter}(result.response_text)" not in body, (
            f"{path} 里还有一处裸渲染 {setter}(result.response_text)"
        )


def test_the_single_source_still_knows_every_degradation_reason():
    """反查：引擎里定义的每种降级原因，共享模块都要有一句「这意味着什么」。

    漏一种的后果是那一支只显示服务端的标签（「协议纠错次数用尽」这类），
    用户不知道这对他手上的报告意味着什么。
    """
    from services.ai.engine import DEGRADATION_LABELS

    source = _read("static/js/ai_context_notice.js")
    for reason in DEGRADATION_LABELS:
        assert f"{reason}:" in source, f"共享模块没有解释 {reason!r}"
