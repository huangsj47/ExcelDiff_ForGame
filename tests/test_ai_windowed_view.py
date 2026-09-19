# -*- coding: utf-8 -*-
"""超长内容的分段渲染：**每一段都拿得到，而且模型知道自己拿到的是第几段**。

## 这个文件守的四条性质

1. **中间那段拿得到。** 原先 `file_diff` 走「保留首尾」的截断，中间永远读不到 ——
   而「就改了一行关键配置」很可能正落在中间。现在按改动块切段，模型点名就能要。
2. **抬头说清四件事**：共几段、这是第几段、**其余几段分别是什么**、怎么要。
   少了第三样，模型只能一段段试，而额度按**次数**计。
3. **「截断」这个词只给平台自己收掉的内容。** 模型点名要第 3-4 段、我们如实给了 3-4 段，
   那不叫截断（否则消耗面板上会有一堆「截断」，而真相是它按段读的）；要了 3-9 却只装得下
   3-5，那才是。
4. **切不开时不编段号。** 一份没有块头的内容仍然按老办法截断（`file_diff` 保留首尾），
   不假称「共 1 段，这里是第 1 段」—— 那会让模型以为还有第 2 段。
"""
from __future__ import annotations

import pytest

from services.ai.windowed_view import parse_window, render_window, split_segments

DIFF = """代码差异：scripts/x.lua

@@ -10,6 +10,8 @@ function a()
   local x = 1
+  local y = 2
@@ -50,3 +52,4 @@ function b()
   end
+  print('b')
@@ -90,2 +93,3 @@ function c()
   return 1
@@ -200,4 +204,5 @@ function d()
   local z = 3
"""

DOC = """# 一号规则

正文一

## 二号规则

正文二

### 三号细则

正文三

## 四号规则

正文四
"""

COMMIT = """提交 abc123
- 提交信息：修了个 bug
- 作者：张三
- 时间：2026-09-19T10:00:00
- 改动文件（4 个）：
  - [M] a.lua
  - [A] b.lua
  - [D] c.lua
  - [M] d.lua
"""


def _render(kind, text, *, window="", limit=11_000, label="L"):
    return render_window(kind=kind, label=label, text=text, window=window, limit=limit)


def _big_diff(hunks: int = 30, lines_per_hunk: int = 12) -> str:
    """够大的一份 diff（约 25,000 字），用来触发真实的分段与截断。

    上限是 11,000 字（`DEFAULT_TOOL_LIMITS`），而 `render_window` 给正文留的额度有一个
    1,000 字的**下限**（免得小额度的配置把内容压成一句空话），所以「装不下」这件事要用
    真实量级的内容来演，不能用 limit=120 这种测试专用的小数字。
    """
    parts = ["代码差异：scripts/big.lua\n"]
    for index in range(hunks):
        parts.append(f"\n@@ -{index * 100},6 +{index * 100},8 @@ function f{index}()\n")
        parts.extend(
            f"   line {line} of hunk {index} —— 一段用来撑长度的改动上下文文字\n"
            for line in range(lines_per_hunk)
        )
    return "".join(parts)


BIG = _big_diff()


def _body(text: str) -> str:
    """取渲染结果里的**正文**（抬头之后的那一半）。

    抬头里有「其余 N 段是」那段目录，它按设计会把没给出的段头也列出来 —— 断言「这一段
    在不在」时必须看正文，否则目录会让每个断言都为真，测试就变成一句空话。
    """
    return text.split("\n\n", 1)[1]


# ==========================================================================
# 窗口解析
# ==========================================================================


@pytest.mark.parametrize(
    "window,total,expected",
    [
        ("4-6", 9, (4, 6)),
        ("5", 9, (5, 5)),
        ("", 9, None),
        ("abc", 9, None),
        ("0-2", 9, None),
        ("6-4", 9, None),
        ("99", 9, None),          # 起点超界 → 按「没给窗口」处理，不是空结果
        ("8-99", 9, (8, 9)),      # 终点超界 → 夹到总数
    ],
)
def test_the_window_parser_is_forgiving_but_never_guesses(window, total, expected):
    """窗口写坏的代价只能是「拿到默认那一段」，不能是「丢掉整条请求」或「返回空」。
    `99` 这一条尤其要紧：它按「没给窗口」处理，而**不是**返回一个越界的区间。"""
    assert parse_window(window, total) == expected


# ==========================================================================
# 切段
# ==========================================================================


def test_a_code_diff_splits_by_hunk_and_keeps_the_header_with_the_first():
    segments, note = split_segments(DIFF, kind="file_diff")

    assert len(segments) == 4, "4 个 `@@` 块"
    assert segments[0].startswith("代码差异：scripts/x.lua"), "抬头并进第一段"
    assert segments[1].startswith("@@ -50,3 +52,4 @@")
    assert "改动块" in note


def test_a_document_splits_by_its_own_headings():
    segments, note = split_segments(DOC, kind="read_reference")

    assert [segment.splitlines()[0] for segment in segments] == [
        "# 一号规则",
        "## 二号规则",
        "### 三号细则",
        "## 四号规则",
    ]
    assert "小节" in note


def test_a_document_without_headings_still_splits_by_size():
    """没有小标题的文档若不切，就永远只读得到前半份（这正是要修的那件事）。"""
    segments, note = split_segments("x" * 9_000, kind="read_reference")

    assert len(segments) == 3
    assert "没有小标题" in note, "切法要如实说，否则模型以为那是文档自己的小节"


def test_a_commit_detail_splits_by_file_and_keeps_the_commit_info_out():
    segments, _ = split_segments(COMMIT, kind="commit_detail")

    assert segments == [
        "  - [M] a.lua\n",
        "  - [A] b.lua\n",
        "  - [D] c.lua\n",
        "  - [M] d.lua\n",
    ], "提交信息那几行不是「段」—— 它们每次都给"


# ==========================================================================
# 抬头
# ==========================================================================


def test_the_header_says_how_many_segments_there_are():
    text, meta = _render("file_diff", BIG)

    assert meta["segments"] == 30
    assert "共 30 段" in text
    assert "这里是第" in text
    assert "要看别的段" in text, "不说怎么要，模型只能猜"


def test_the_header_lists_what_was_left_out():
    """**抬头第三样**：只写「还有 N 段」的话，模型不知道那几段里有没有它要的。"""
    text, meta = _render("file_diff", BIG)

    assert meta["truncated"] is True, "25,000 字装不进 11,000"
    tail = text.split("其余")[-1]
    assert "@@ -1400,6 +1400,8 @@ function f14()" in tail, "没给出的段要按顺序列出来"
    assert "其余" in text


def test_the_hint_gives_a_real_segment_number():
    """例子里的段号必须真实存在 —— 给一个越界的例子等于教模型写坏请求。"""
    import re

    text, meta = _render("file_diff", BIG)
    match = re.search(r"（例如 \"(\d+)(?:-(\d+))?\"）", text)

    assert match, f"抬头里要有一个可照抄的例子：{text[:200]}"
    for group in match.groups():
        if group:
            assert 1 <= int(group) <= meta["segments"]


# ==========================================================================
# 点名要别的段
# ==========================================================================


def test_the_model_can_ask_for_the_middle_segment():
    """**这是这个模块存在的理由**：中间那一段原先永远拿不到。

    默认那一份只装得下前十几二十段，后面的段要靠点名 —— 这条用例走一遍那条路：
    挑一个**默认没给到**的段（按第一次的 `shown` 算出来，不写死段号），点名要它。
    """
    whole, meta = _render("file_diff", BIG)
    last_shown = int(meta["shown"].split("-")[-1])
    index = min(last_shown + 3, meta["segments"])       # 1-based
    marker = f"@@ -{(index - 1) * 100},6 +{(index - 1) * 100},8 @@ function f{index - 1}()"

    assert marker not in _body(whole), f"默认那一份不该已经包含第 {index} 段"
    later, later_meta = _render("file_diff", BIG, window=str(index))

    assert marker in _body(later), "点名之后就该拿到"
    assert later_meta["shown"] == str(index)
    assert later_meta["truncated"] is False, "点名要一段、给到了，不算截断"


def test_asking_for_a_range_gives_exactly_that_range():
    text, meta = _render("read_reference", DOC, window="2-3", limit=11_000)

    assert meta["shown"] == "2-3"
    body = text.split("\n\n", 1)[1].split("其余")[0]
    assert "## 二号规则" in body and "### 三号细则" in body
    assert "# 一号规则" not in body and "## 四号规则" not in body


def test_a_commit_detail_paging_always_keeps_the_commit_info():
    """翻页翻的是文件清单；「这是哪条提交」每次都要在 —— 丢了它那些路径没有意义。"""
    text, meta = _render("commit_detail", COMMIT, window="3-4", limit=6_000)

    assert meta["shown"] == "3-4"
    assert "提交信息：修了个 bug" in text and "作者：张三" in text
    assert "  - [D] c.lua" in text and "  - [M] d.lua" in text
    assert "  - [M] a.lua" not in text, "点名了第 3-4 个文件就不该给第 1 个"


def test_every_segment_is_reachable_by_walking_the_windows():
    """从头到尾一段段走，**30 段全都能拿到**（这正是「永远读不到中间」的回归用例）。"""
    bodies = [
        _render("file_diff", BIG, window=str(index))[0]
        for index in range(1, 31)
    ]

    for index in range(30):
        marker = f"@@ -{index * 100},6 +{index * 100},8 @@ function f{index}()"
        # 抬头里也会出现这个块头的**缩进副本**（「其余 N 段是」那段目录），所以按
        # 「以它开头的行」数 —— 正文里的块头是顶格的，目录里的带缩进与段号。
        hits = sum(line.startswith(marker) for text in bodies for line in text.splitlines())
        assert hits == 1, f"第 {index + 1} 段应该在且只在一次里出现（实际 {hits} 次）"


# ==========================================================================
# 「截断」这个词的口径
# ==========================================================================


def test_asking_for_a_window_is_not_a_truncation():
    """模型点名要的窗口**不算截断** —— 它拿到了它要的全部。

    算成截断的话，消耗面板上的「截断」列会把「按段深挖」这件事说成「平台在砍内容」。
    """
    _, meta = _render("file_diff", DIFF, window="1-2", limit=11_000)
    assert meta["truncated"] is False

    _, all_of_it = _render("file_diff", DIFF, limit=11_000)
    assert all_of_it["truncated"] is False


def test_when_the_content_does_not_fit_it_says_so():
    """没给窗口、而装不下 → 这是**平台**在收内容，如实记成截断。"""
    _, meta = _render("file_diff", BIG)
    assert meta["truncated"] is True


def test_a_window_that_does_not_fit_is_a_truncation():
    """要了 1-30 却只装得下前十几段 —— 那是平台没给全，必须记成截断。"""
    text, meta = _render("file_diff", BIG, window="1-30")

    assert meta["truncated"] is True
    assert meta["shown"].split("-")[-1] != "30", f"没装上就不该说给了 30 段：{meta}"


def test_content_that_cannot_be_split_keeps_the_old_truncation():
    """切不开时**不编段号**：仍旧按老办法截断（`file_diff` 保留首尾）。

    假称「共 1 段，这里是第 1 段」会让模型以为还有第 2 段 —— 一次为不存在的段花的额度，
    和没说一样糟。
    """
    text, meta = _render("file_diff", "开头-" + "中" * 30_000 + "-结尾", limit=2_000)

    assert meta == {"segments": 1, "split": meta["split"], "truncated": True}
    assert text.startswith("开头-") and text.endswith("-结尾"), "保留首尾"
    assert "共 1 段" not in text and "这里是第" not in text
