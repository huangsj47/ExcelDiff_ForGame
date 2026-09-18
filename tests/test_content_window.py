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
        assert window.end_line == window.start_line + len(window.content.split('\n')) - 1, (
            f'抬头会写「第 {window.start_line}–{window.end_line} 行」，'
            f'但正文只有 {len(window.content.split("\n"))} 行'
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
