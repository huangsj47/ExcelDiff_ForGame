# -*- coding: utf-8 -*-
"""AI 取数：纯文本/代码文件的 diff 必须真的能交到模型手上。

## 缺陷形态（线上可见，且是必然发生）

周版本配置里同时挂了配表仓库（qx_config，Excel）和代码仓库（qz_luaworkspace，lua）。
AI 报告却写「代码侧几乎无 diff 可判」，并列出 6 个「取数失败」的 lua 文件。

根因是一条形状不匹配的链，**对每一个非 Excel 文件都必然失败**：

    DiffService.get_file_type('.lua')        -> 'text'      （TEXT_EXTENSIONS 含 .lua）
    DiffService._process_text_diff(...)      -> {'type': 'text', 'raw_diff': …, 'hunks': …}
    platform_provider.render_diff_payload    -> 只认 excel/code/binary/error，
                                                没有 text 分支 -> return None
    ContextTools 的契约                       -> None 等于「取不到内容」-> 「[取数失败]」

平台其实已经算好并交出了完整 diff，只是没人渲染它。本次线上 767 个文件里 748 个是
`.lua`，所以代码侧**一个文件都看不到**；配表侧因为 `type == "excel"` 恰好在白名单里
而完全正常 —— 这正好解释了「配表讲得很细、代码只有空话」的观感。

## 顺带修掉的第二个缺陷

`raw_diff` 原本是 `''.join(diff_lines)`。`unified_diff(lineterm="")` 下两种行混在一起：
正文行来自 `splitlines(keepends=True)` 自带换行，而 `---`/`+++`/`@@` 三行是 difflib
合成的、不带换行。于是三个头行会和紧随的第一行正文粘成一整行。而改成 `'\n'.join`
又会给正文行多加空行、整份 diff 变双倍行距 —— 两种写法都不对，必须逐行剥掉行尾。
"""
from __future__ import annotations

from services.ai.platform_provider import render_diff_payload
from services.diff_service import DiffService

LUA_OLD = b"function F:getRank(roleId)\n    return self.rankMap[roleId]\nend\n"
LUA_NEW = (
    b"function F:getRank(roleId)\n"
    b"    -- \xe4\xbf\xae\xe5\xa4\x8d\xef\xbc\x9a\xe6\x9c\xaa\xe5\x88\x9d\xe5\xa7\x8b\xe5\x8c\x96\xe6\x97\xb6\xe8\xbf\x94\xe5\x9b\x9e nil\n"
    b"    local rank = self.rankMap[roleId]\n"
    b"    return rank or 0\n"
    b"end\n"
)
LUA_PATH = "code/qz_pub/cfg/SeasonRankCfgMod.lua"


def _real_lua_payload():
    """走真实的 DiffService，不构造假载荷 —— 这个缺陷就是「真实形状没人认领」。"""
    return DiffService().process_diff(LUA_PATH, LUA_NEW, LUA_OLD, key_columns=None)


def test_a_lua_file_is_classified_as_text():
    """先钉住前提：`.lua` 是 text 类型。

    哪天 `TEXT_EXTENSIONS` 变了，下面那条端到端断言失败的原因会变得不明显。
    """
    service = DiffService()
    assert service.get_file_type(LUA_PATH) == "text"
    assert _real_lua_payload()["type"] == "text"


def test_a_real_text_diff_is_rendered_instead_of_dropped():
    """核心回归：真实 text 载荷必须渲染出内容，不能返回 None。

    返回 None 会被上层按契约报成「取数失败」，模型于是以为平台取不到这个文件的
    diff —— 而平台其实算好了。
    """
    payload = _real_lua_payload()
    rendered = render_diff_payload(payload, path=LUA_PATH)
    assert rendered is not None, (
        'text 类型的 diff 又被丢成 None 了：代码仓库的 AI 分析会整个看不到 diff'
    )
    assert LUA_PATH in rendered, "渲染结果里没有文件路径，模型分不清这是哪个文件"
    assert "return self.rankMap[roleId]" in rendered, "删掉的那一行没进渲染结果"
    assert "return rank or 0" in rendered, "新增的那一行没进渲染结果"


def test_the_rendered_diff_keeps_line_structure():
    """diff 的每一行必须各占一行，`+`/`-` 落在行首。

    这一条守的是 `raw_diff` 的换行处理：`''.join` 会把 `---`/`+++`/`@@` 和第一行
    正文粘成一整行；`'\\n'.join` 又会让正文行双倍行距。两种都会让模型读不出结构。
    """
    rendered = render_diff_payload(_real_lua_payload(), path=LUA_PATH)
    lines = rendered.splitlines()
    body = lines[2:]  # 前两行是「文件差异：<path>」和一个空行
    assert body[0].startswith("--- a/"), f"diff 头行被粘住了：{body[0]!r}"
    assert body[1].startswith("+++ b/"), f"diff 头行被粘住了：{body[1]!r}"
    assert body[2].startswith("@@"), f"hunk 头被粘住了：{body[2]!r}"
    assert "" not in body, f"diff 里出现了空行（双倍行距）：{body}"
    for line in body[3:]:
        assert line[:1] in (" ", "+", "-"), f"这一行没有 diff 前缀：{line!r}"


def test_a_text_payload_without_a_body_says_so_instead_of_going_silent():
    """有记录但没内容时要说明白，不能返回 None。"""
    rendered = render_diff_payload({"type": "text", "file_path": "a/b.lua"})
    assert rendered is not None
    assert "a/b.lua" in rendered


def test_the_hunks_fallback_rebuilds_the_patch():
    """`raw_diff` 缺失时用 hunks 兜底重建（结构化等价物）。"""
    payload = {
        "type": "text",
        "file_path": "a/b.lua",
        "hunks": [{
            "header": "@@ -1,2 +1,2 @@",
            "lines": [
                {"type": "context", "content": " ctx", "raw": " ctx"},
                {"type": "removed", "content": "old", "raw": "-old"},
                {"type": "added", "content": "new", "raw": "+new"},
            ],
        }],
    }
    rendered = render_diff_payload(payload, path="a/b.lua")
    assert "@@ -1,2 +1,2 @@" in rendered
    assert "-old" in rendered and "+new" in rendered


def test_an_image_diff_reports_the_change_without_dumping_base64():
    """图片：说清「变了/没变」，但**绝不能把 base64 倒进上下文**。

    一张图的 base64 能瞬间吃掉整个 prompt 预算；而写成「没有内容」又会让模型
    把它当成「没改动」（与 `_render_binary` 同一个坑）。
    """
    payload = {
        "type": "image",
        "file_path": "assets/ui/icon.png",
        "operation": "modified",
        "is_same": False,
        "current_image": {"base64": "iVBORw0KGgoAAAANSUhEUg" * 500, "info": {}},
        "previous_image": {"base64": "iVBORw0KGgoAAAANSUhEUg" * 500, "info": {}},
    }
    rendered = render_diff_payload(payload, path="assets/ui/icon.png")
    assert rendered is not None
    assert "assets/ui/icon.png" in rendered
    assert "iVBORw0KGgo" not in rendered, "把图片的 base64 倒进上下文了，会吃光预算"
    assert "不等于" in rendered, "没说清「无法展示」不等于「没有改动」"


def test_an_unrecognised_structure_still_returns_none():
    """`text` 分支不能顺带把「真认不出来」也变成有内容。

    认不出来返回 None 是既定契约（空串会被当成「确实没有差异」，是另一种撒谎），
    这里守住新增分支没有把那条契约放宽。
    """
    assert render_diff_payload({"不相干的键": 1}) is None
    assert render_diff_payload({"type": "某种新类型"}) is None
