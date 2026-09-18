#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一个文件的正文切成「给模型看的那一段」：行窗口 + 字符上限。

## 为什么按窗口给，而不是整份文件

代码评审要的是**改动周围**长什么样（改动本身已经在 diff 里）。一份几千行的 lua 整份
塞进上下文，付的是三份代价：跨节点传输、库里留一份、以及最贵的 —— 提示词里挤进几千行
与本次判断无关的代码（单条内容上限就是为这件事设的，而被从中截断的正文等于没有上下文）。

所以窗口是**默认行为**，不是优化选项；模型可以点名要哪一段（`lines="1180-1260"`）。

## 这一条规则有两端在用，所以只有一份实现

* Agent 侧读工作副本时切（`services/agent_file_content_reader.read_file_content_for_agent`）；
* 平台侧自己读得到时切（单机模式，`services/ai/platform_provider.file_content`）。

两端各写一遍「`lines` 怎么解析、截断了算到哪一行」必然漂移，而漂移的表现是同一份请求
在不同部署下给出不同的行号 —— 行号是模型写进结论里的证据，错一格就没人能复核。
"""

from __future__ import annotations

import re
from typing import NamedTuple

# 没给窗口时给多少行。够看清一个函数与它的调用点，又不至于把提示词预算吃掉。
DEFAULT_WINDOW_LINES = 400
# 请求里只写了一个行号（`lines="1180"`）时，从那一行往后给多少行。
SINGLE_LINE_WINDOW_LINES = 80

# 一次给模型看的正文**字符**上限。**必须与 `services.ai.context_tools` 里
# `file_content` 那条单条上限是同一个数**（那里现在直接引用本常量），理由：
#
# 这里按行边界切好之后，预算层还会再按字符截一次（`budget.truncate_text`，只砍尾巴）。
# 两个数不一致的后果是**抬头与正文对不上** —— 抬头写着「下面是第 1180–1260 行」，
# 而正文被砍到 1290 行之前就断了，尾行还断在半行中间。行号是模型写进结论里的证据
# （「第 1203 行那个判断」），对不上就没人能复核。
#
# 为什么是 11,000：那是与预算对账出来的单条额度（见 `context_tools.DEFAULT_TOOL_LIMITS`
# 上方那段算式）。在这里就切到上限，比让预算层事后砍一刀好在两点：切在行边界上、
# 且余下的行数会被如实写进抬头（模型据此知道还能再要哪一段）。
CONTENT_MAX_CHARS = 11_000

_WINDOW_RE = re.compile(r"(\d+)\s*(?:[-~—到]\s*(\d+))?")


class ContentWindow(NamedTuple):
    """切好的正文 + 它是文件的哪一段（**都要给模型看**）。"""

    content: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool

    def is_partial(self) -> bool:
        return self.truncated or self.start_line > 1 or self.end_line < self.total_lines


def parse_line_window(spec, *, total_lines: int) -> tuple:
    """把 `lines` 解析成 1 起算的 `(start, end)`；认不出来就当没给。

    **认不出来一律回落**：一个写错的窗口不该让整次取数失败 —— 模型写错参数的代价只能
    是「没拿到更好的一刀」，不能是「什么都没拿到」。
    """
    total = max(0, int(total_lines or 0))
    fallback_end = min(total, DEFAULT_WINDOW_LINES) if total else 0
    text = str(spec or "").strip()
    if not text:
        return 1, fallback_end
    match = _WINDOW_RE.fullmatch(text)
    if not match:
        return 1, fallback_end
    # 行号从 1 起算：`0` 与负数都是写错的（`-5` 连正则都过不了）。写错就当作没给，
    # 别把 `0` 悄悄解释成「第 1 行起 80 行」—— 那会让模型以为自己点的那一段拿到了。
    if int(match.group(1)) < 1:
        return 1, fallback_end
    start = max(1, int(match.group(1)))
    if total and start > total:
        return 1, fallback_end
    if match.group(2):
        end = int(match.group(2))
    else:
        end = start + SINGLE_LINE_WINDOW_LINES - 1
    end = max(start, end)
    if total:
        end = min(end, total)
    return start, end


def slice_lines(text, spec=None, *, max_chars: int = 0) -> ContentWindow:
    """按窗口切正文，再按字符上限截断（截断后**行号要跟着改**）。"""
    body = text if isinstance(text, str) else ""
    lines = body.splitlines()
    total = len(lines)
    start, end = parse_line_window(spec, total_lines=total)
    window = lines[start - 1:end] if total else []
    content = "\n".join(window)
    truncated = False
    limit = int(max_chars or 0)
    if limit and len(content) > limit:
        truncated = True
        # 截断点必须落在整行边界上：切在半行中间会让模型读到一段看起来像代码、
        # 实际不存在的语句（`return a` 与 `return ab` 是两回事）。
        head = content[:limit]
        cut = head.rfind("\n")
        content = head[:cut] if cut > 0 else head
        end = start + (content.count("\n") if content else 0)
    return ContentWindow(
        content=content,
        start_line=start,
        end_line=max(start, min(end, total) if total else end),
        total_lines=total,
        truncated=truncated,
    )
