# -*- coding: utf-8 -*-
"""给模型看的那一段正文怎么切（`utils/content_window.py`）。

## 这个文件存在的理由

窗口规则**两端在用**：平台自己读得到正文时切（`ai/platform_provider._render_text_content`）、
Agent 读工作副本时也切（`agent_file_content_reader.read_file_content_for_agent`）。两端各写一遍
必然漂移，而漂移的表现是**同一份请求在不同部署下报出不同的行号** —— 行号是模型写进结论
里的证据（「第 1180 行那个判断」），错一格就没人能复核。

所以这里逐条钉住：
1. 默认给一段（不是整份文件）；
2. 模型点名 `lines="1180-1260"` 时给的就是那一段，**行号从 1180 起算**；
3. 截断点落在整行边界上，且截断后的 `end_line` 与真正给出去的行数一致
   （否则抬头说的行数与正文对不上，等于给了个错的坐标）；
4. **认不出来的窗口一律回落**，绝不因为模型写错参数就让整次取数失败。
"""
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.content_window import (  # noqa: E402
    CONTENT_MAX_CHARS,
    DEFAULT_WINDOW_LINES,
    SINGLE_LINE_WINDOW_LINES,
    parse_line_window,
    slice_lines,
)


def _text(lines):
    return "\n".join(f"line {i}" for i in range(1, lines + 1))


class TestDefaultWindow:
    """没点名要哪一段时：给开头一段，并如实说这是片段。"""

    def test_short_file_is_given_whole(self):
        window = slice_lines(_text(10))
        assert window.content == _text(10)
        assert (window.start_line, window.end_line, window.total_lines) == (1, 10, 10)
        assert window.is_partial() is False, '一共 10 行的文件给全了，不该说「不是全文」'

    def test_long_file_is_cut_to_the_default_window(self):
        window = slice_lines(_text(5000))
        assert window.total_lines == 5000, '整份文件有多少行必须如实报出来'
        assert window.end_line - window.start_line + 1 == DEFAULT_WINDOW_LINES
        assert window.is_partial() is True
        assert 'line 5000' not in window.content, '整份几千行的正文不该全给模型'

    def test_empty_file_is_zero_lines(self):
        window = slice_lines('')
        assert (window.content, window.total_lines) == ('', 0)
        assert window.is_partial() is False


class TestNamedWindow:
    """模型点名了 `lines`：给的就是那一段，且行号是文件里的真实行号。"""

    def test_a_range_is_honoured(self):
        window = slice_lines(_text(3000), '1180-1260')
        assert (window.start_line, window.end_line) == (1180, 1260)
        assert window.content.split('\n')[0] == 'line 1180', '切错了一行，模型引用的行号就全错'
        assert window.content.split('\n')[-1] == 'line 1260'
        assert len(window.content.split('\n')) == 81

    def test_a_single_line_means_a_small_window_from_there(self):
        window = slice_lines(_text(3000), '1180')
        assert window.start_line == 1180
        assert window.end_line == 1180 + SINGLE_LINE_WINDOW_LINES - 1, (
            '只写一个行号时给多少行必须是固定口径，不能随实现变'
        )

    def test_range_is_clamped_to_the_file(self):
        window = slice_lines(_text(100), '90-500')
        assert (window.start_line, window.end_line) == (90, 100)
        assert window.content.split('\n')[-1] == 'line 100'


class TestMalformedWindowFallsBack:
    """写坏的窗口只该让「这一刀没切好」，不该让整次取数失败。"""

    @pytest.mark.parametrize('spec', ['', None, 'abc', '0', '-5', '1180-', '9000000', '1.5'])
    def test_unusable_specs_fall_back_to_the_default_window(self, spec):
        window = slice_lines(_text(1000), spec)
        assert window.start_line == 1, f'spec={spec!r} 没有回落到默认窗口'
        assert window.end_line == DEFAULT_WINDOW_LINES
        assert window.content.startswith('line 1')

    def test_parse_never_raises_and_always_returns_a_pair(self):
        for spec in ['abc', '0', '99999999', '  ', None, object()]:
            start, end = parse_line_window(spec, total_lines=100)
            assert 1 <= start <= 100 and end >= start, f'spec={spec!r} → {(start, end)}'


class TestTruncation:
    """按字符上限截断时，行号必须跟着走 —— 抬头与正文说的是同一件事。"""

    def test_cut_lands_on_a_line_boundary(self):
        window = slice_lines(_text(1000), '1-400', max_chars=60)
        assert window.truncated is True
        lines = window.content.split('\n')
        assert all(line.startswith('line ') for line in lines), (
            f'切在半行中间了（模型会读到一段看起来像代码、实际不存在的内容）：{lines[-1]!r}'
        )
        assert len(window.content) <= 60

    def test_end_line_matches_what_was_actually_given(self):
        window = slice_lines(_text(1000), '1-400', max_chars=60)
        given = len(window.content.split('\n'))
        assert window.end_line == window.start_line + given - 1, (
            # 这个 `given` 不是为了少写一个变量：f-string 的**表达式里**放反斜杠是
            # Python 3.12+ 才允许的写法（PEP 701），本仓库的 CI 跑 3.11，会直接 SyntaxError
            # （`test_python311_syntax_compat.py` 就是钉这件事的）。
            f'抬头会写「第 {window.start_line}–{window.end_line} 行」，但正文只有 {given} 行'
        )
        assert window.end_line < 400

    def test_no_truncation_when_under_the_limit(self):
        window = slice_lines(_text(10), max_chars=10_000)
        assert window.truncated is False
        assert slice_lines(_text(10), max_chars=0).truncated is False, (
            '0 表示不设上限（平台侧不传时就是这个），不能当成「截成空」'
        )


class TestIsPartial:
    """「这不是全文」这句话要有依据，不能一律写上去。"""

    def test_whole_short_file_is_not_partial(self):
        assert slice_lines(_text(5)).is_partial() is False

    def test_default_window_of_a_long_file_is_partial(self):
        assert slice_lines(_text(5000)).is_partial() is True

    def test_named_window_in_the_middle_is_partial(self):
        assert slice_lines(_text(5000), '2000-2100').is_partial() is True

    def test_window_ending_before_the_last_line_is_partial(self):
        assert slice_lines(_text(100), '1-50').is_partial() is True


class TestTheCapIsOneNumber:
    """正文上限只有一个数 —— 四份副本必须逐字相等。

    切正文的地方（Agent 侧、平台侧）与花额度的地方（预算层）各有一个上限。它们不一致的
    后果不是「少给一点」，而是**抬头说的行数与正文对不上**：取数侧先按自己的上限切好、
    写好「下面是第 a–b 行」，预算层再按自己的上限砍一刀尾巴 —— 砍在行中间，行号就成了假的，
    而模型正是拿那个行号写进结论当证据的。

    所以这条不是「顺手对齐」，是把一个必须成立的等式钉住。
    """

    def test_all_four_copies_are_the_same_number(self):
        from services.agent_file_content_dispatch import FILE_CONTENT_MAX_CHARS
        from services.ai.context_tools import DEFAULT_TOOL_LIMITS
        from services.ai.platform_provider import DEFAULT_CONTENT_MAX_CHARS

        assert DEFAULT_TOOL_LIMITS['file_content'] == CONTENT_MAX_CHARS, (
            '预算层的单条上限与切正文用的字符上限不是同一个数'
        )
        assert DEFAULT_CONTENT_MAX_CHARS == CONTENT_MAX_CHARS
        assert FILE_CONTENT_MAX_CHARS == CONTENT_MAX_CHARS

    def test_the_cap_is_a_real_cap_but_not_a_tiny_one(self):
        """太小会让模型一次看不完一个函数，太大就是把预算全花在一个文件上。"""
        assert 4_000 <= CONTENT_MAX_CHARS <= 20_000


class TestTheHeaderAndTheBodyAlwaysAgree:
    """**「四个常量相等」不是这条的防线，这条才是。**

    `TestTheCapIsOneNumber` 只钉住「四个数逐字相等」。常量相等对「整串 ≤ 上限」既不充分
    也不必要 —— 抬头、每行的 `数字│` 前缀、末尾那句补救指路**都算在这份额度里**，
    而它们是任何常量都对齐不了的开销。

    实测（修之前）：300 行 × 35 字的文件，正文 10,799 字、整串 11,939 字。取数侧量的是
    正文，预算层量的是整串，于是必然再砍一刀尾巴；而抬头里的「第 1–300 行」是取数侧
    写死的、不跟着改，连「不是全文」都没标。同屏唯一的截断信号是末尾那句英文
    `... [truncated by local tool]` —— 模型完全有理由按抬头引用第 290 行，而它从没拿到过。

    所以这里断言的是**两件事**，缺一件都不够：
    1. 整串不超上限（超了就会被预算层砍，而抬头不会跟着改）；
    2. 抬头声明的区间末尾 == 正文里最后一个 `数字│` 的行号。
    """

    # (行数, 每行字符数)：前两组是实测出来会触发两次截断的形态，第三组是宽行。
    SHAPES = ((300, 35), (500, 35), (400, 200), (5_000, 40))

    @staticmethod
    def _assert_agrees(rendered: str, where: str) -> None:
        assert len(rendered) <= CONTENT_MAX_CHARS, (
            f"{where}：整串 {len(rendered)} 字，超了上限 {CONTENT_MAX_CHARS} —— "
            "预算层会再砍一刀尾巴，而抬头里的行号不会跟着改"
        )
        head = rendered.split("\n")[0]
        claimed = re.search(r"第 (\d+)–(\d+) 行", head)
        assert claimed is not None, f"{where}：抬头里没有行号区间：{head!r}"
        numbers = [int(m.group(1)) for m in re.finditer(r"(?m)^(\d+)│", rendered)]
        assert numbers, f"{where}：正文一行都没给"
        assert numbers[-1] == int(claimed.group(2)), (
            f"{where}：抬头说给到第 {claimed.group(2)} 行，正文实际只到第 {numbers[-1]} 行"
        )
        assert numbers[0] == int(claimed.group(1)), (
            f"{where}：抬头说从第 {claimed.group(1)} 行起，正文第一行是第 {numbers[0]} 行"
        )

    @pytest.mark.parametrize("rows,width", SHAPES)
    def test_the_platform_local_path_agrees(self, rows, width):
        from services.ai.platform_provider import _render_text_content

        text = "\n".join("x" * width for _ in range(rows))
        self._assert_agrees(_render_text_content(text, path="code/a.lua"), f"本地 {rows}×{width}")

    @pytest.mark.parametrize("rows,width", SHAPES)
    def test_the_agent_path_agrees(self, rows, width):
        """Agent 那条路把**同一个开销**加在 Agent 切好的正文上，所以同样会超。"""
        from services.ai.platform_provider import _render_agent_file_content

        text = "\n".join("x" * width for _ in range(rows))
        rendered = _render_agent_file_content(
            {
                "file_path": "code/a.lua",
                "content": text,
                "start_line": 1,
                "end_line": rows,
                "total_lines": rows,
                "truncated": False,
            },
            path="code/a.lua",
        )
        self._assert_agrees(rendered, f"Agent {rows}×{width}")

    @pytest.mark.parametrize("rows,width", SHAPES)
    def test_the_way_back_survives_the_fitting(self, rows, width):
        """末尾那句「需要更多请指定 lines」**必须活下来**。

        它是这条路上唯一的重来路径：抬头写「不是全文」而正文末尾没有那一句时，模型知道
        自己没看全，却不知道能再要一次（配表那边先前就是这么变成死路的）。
        """
        from services.ai.platform_provider import _render_text_content

        text = "\n".join("x" * width for _ in range(rows))
        rendered = _render_text_content(text, path="code/a.lua")

        assert "不是全文" in rendered, "抬头没有标出这是片段"
        assert "指定 lines" in rendered, f"{rows}×{width}：末端的补救指路被裁掉了"

    def test_a_named_window_is_not_shrunk_when_it_already_fits(self):
        """点名的窗口本来就装得下时，一个字都不该少 —— 别为了「保险」把模型要的段裁掉。"""
        from services.ai.platform_provider import _render_text_content

        text = "\n".join(f"line {index}" for index in range(1, 201))
        rendered = _render_text_content(text, path="code/a.lua", lines="50-60")

        assert rendered.count("\n") == 11, f"窗口被多裁了：{rendered!r}"  # 抬头 + 11 行正文
        assert "第 50–60 行" in rendered
        assert "不是全文" in rendered, "点了中间一段，抬头要说这不是全文"

