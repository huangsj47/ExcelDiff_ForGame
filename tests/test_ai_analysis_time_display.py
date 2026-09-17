# -*- coding: utf-8 -*-
"""「最近分析」的时间要按 UTC+8 显示，而不是裸 ISO。

## 缺陷形态

界面上显示的是 `最近分析：2026-09-17T12:53:28.872891` —— 带微秒、带 T 的裸 ISO，
而且那个 12:53 是 **UTC**，比北京时间早 8 小时（用户反馈「时间不对」）。

链条：`created_at` 存的是 naive-UTC 墙钟（SQLite 会丢 tzinfo），序列化时
`run.created_at.isoformat()` 直接给前端，前端原样贴进文本。

## 为什么必须在**服务端**格式化

ES 规范里「带时间但不带偏移」的 ISO 串按**浏览器本地时区**解析：

    new Date('2026-09-17T12:53:28.872891').toISOString() -> 2026-09-17T04:53:28.872Z

在 UTC+8 的机器上它被当成北京时间、再转一次就又多错 8 小时。所以这里不用前端
`Intl` 转换，而是服务端算好、前端只显示 —— 这类错就没有发生的余地。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from services.ai_analysis_service import _created_at_display


def test_a_naive_utc_timestamp_is_shown_as_beijing_time():
    """用户截图里的原值：库里 12:53:28（UTC）应显示为 20:53:28。"""
    run = SimpleNamespace(created_at=datetime(2026, 9, 17, 12, 53, 28, 872891))
    assert _created_at_display(run) == "2026-09-17 20:53:28"


def test_the_display_is_human_readable_not_iso():
    """不能再把裸 ISO（T 分隔、带微秒）给界面。"""
    run = SimpleNamespace(created_at=datetime(2026, 1, 2, 3, 4, 5, 678901))
    shown = _created_at_display(run)
    assert "T" not in shown, f"还在显示 ISO 的 T 分隔：{shown}"
    assert "." not in shown, f"还在显示微秒：{shown}"
    assert shown == "2026-01-02 11:04:05"


def test_a_missing_timestamp_returns_none_not_a_fake_string():
    """没有时间就给 None，让前端回落到 '-'，不要编一个「未知时间」混进去。"""
    assert _created_at_display(SimpleNamespace(created_at=None)) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        (datetime(2026, 9, 17, 16, 0, 0), "2026-09-18 00:00:00"),   # 跨日
        (datetime(2026, 12, 31, 20, 0, 0), "2027-01-01 04:00:00"),  # 跨年
    ],
)
def test_the_utc_to_beijing_shift_holds_across_day_and_year_boundaries(raw, expected):
    """+8 小时会跨日、跨年，不能被「当天 23:59 封顶」这类写法悄悄夹掉。"""
    assert _created_at_display(SimpleNamespace(created_at=raw)) == expected
