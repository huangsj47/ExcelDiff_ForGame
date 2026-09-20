# -*- coding: utf-8 -*-
"""`_SheetStats` 的两把抽样刀都必须**如实报出去**。

## 这条为什么值得单独守

统计块是模型判断「这个值在不在允许集合里」的依据。它同时受两个上限约束：

* 数值列只采 `_STATS_MAX_SAMPLES` 个样本；
* 文本列只记 `_STATS_MAX_DISTINCT_TEXTS` 个不同取值。

两条都会让统计里的数字**小于真实值**，而模型看到的只是一个光秃秃的数。`sheet_stats.py`
的模块抬头把这件事写得很清楚：「统计说『不同取值 5000』时，真实值可能更大，模型会拿这个
数当『允许集合』」。所以撞了上限就必须说话。

原先只有数值那一支说话，文本那一支**默默丢掉新取值**——同一份渲染里两个上限，一个报
一个不报，读的人（和模型）没有任何线索去分辨。

## 顺带守住「标签跟着常量走」

原先那句是写死的 `"（抽样上限 20000）"`。常量和文案分处两地，改了一个忘了另一个，
报出去的数就是假的——而它假得很安静。这里断言标签里必须出现**常量本身**的值。

## 断言为什么盯「上限」这两个字，以及为什么用正则抓那个数字

第一版这里断言的是「渲染结果里出现 `5000`」。它是**假通过**：撞了上限时每列会打印
`不同取值 5000`，而这个 `5000` 本来就在，跟有没有那句上限说明毫无关系——把整个上限说明
删掉，断言照样绿（实测如此）。所以判据换成标签里的**措辞**：只有真的被砍过，
`_NUMERIC_CAP_MARK` / `_TEXT_CAP_MARK` 才会出现。

第二版改成了「结果里出现 `str(常量)`」，**还是假通过**：把常量改成 `20`、文案写死成旧值
`20000`（正是「改了常量忘了改文案」那个 bug），而 `"20" in "（抽样上限 20000）"` 成立。
所以数字必须**贴着措辞抓**：用正则把紧跟在「抽样上限」后面的那个数字取出来，再跟常量比。
这样抓到的才是这一句真正报出去的数。
"""

import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.ai.sheet_stats import (  # noqa: E402
    _STATS_MAX_DISTINCT_TEXTS,
    _STATS_MAX_SAMPLES,
    _SheetStats,
)

# 一列列名，够渲染出统计块。
_LABELS = ["品质"]

# 措辞：只有真的撞了上限才会出现。用它判「说没说」。
_NUMERIC_CAP_MARK = "抽样上限"
_TEXT_CAP_MARK = "取值上限"

# 措辞后面紧跟的那个数字：用它判「说的是不是真数」。
_NUMERIC_CAP_NUMBER = re.compile(r"抽样上限\s*(\d+)")
_TEXT_CAP_NUMBER = re.compile(r"取值上限\s*(\d+)")


def _render(rows) -> str:
    stats = _SheetStats()
    for row in rows:
        stats.add_row(row)
    return "\n".join(stats.render(_LABELS))


def _reported_number(pattern: re.Pattern, text: str, what: str) -> str:
    match = pattern.search(text)
    assert match is not None, f"没有找到{what}的说明。\n实际输出：\n{text}"
    return match.group(1)


def test_text_bucket_cap_is_disclosed_when_it_bites():
    """不同取值超过上限时，渲染出来必须说明「实际不止这些」，并报出真正的上限。"""
    rows = [[f"值{index}"] for index in range(_STATS_MAX_DISTINCT_TEXTS + 5)]
    text = _render(rows)

    assert "不同取值" in text, text
    reported = _reported_number(_TEXT_CAP_NUMBER, text, "文本取值上限")
    assert reported == str(_STATS_MAX_DISTINCT_TEXTS), (
        f"文本取值撞了上限（{_STATS_MAX_DISTINCT_TEXTS}）却没有如实说明，"
        f"模型会把「不同取值 {_STATS_MAX_DISTINCT_TEXTS}」读成真实取值个数。\n实际输出：\n{text}"
    )


def test_text_bucket_cap_is_silent_when_it_does_not_bite():
    """没撞上限就不能出现上限字样——否则这句话就不再意味着「这里被砍过」。"""
    rows = [[f"值{index}"] for index in range(10)]
    text = _render(rows)

    assert "不同取值" in text, text
    assert _TEXT_CAP_MARK not in text, (
        f"只出现了 10 个取值，却报了取值上限——这句话必须只在真的被砍过时才出现。\n"
        f"实际输出：\n{text}"
    )


def test_numeric_sample_cap_is_disclosed_when_it_bites():
    """数值那一支原本就报，这里把它钉住，免得将来被顺手改掉。"""
    rows = [[index] for index in range(_STATS_MAX_SAMPLES + 5)]
    text = _render(rows)

    reported = _reported_number(_NUMERIC_CAP_NUMBER, text, "数值样本上限")
    assert reported == str(_STATS_MAX_SAMPLES), (
        f"数值样本撞了上限（{_STATS_MAX_SAMPLES}），报出去的却是 {reported}。\n实际输出：\n{text}"
    )


def test_numeric_sample_cap_is_silent_when_it_does_not_bite():
    rows = [[index] for index in range(10)]
    text = _render(rows)

    assert "数值" in text, text
    assert _NUMERIC_CAP_MARK not in text, text


def test_cap_labels_track_their_constants_not_a_hardcoded_number():
    """改了常量没改文案时，报出去的就是假数——这一条专门抓那个。

    分开写而不是并进上面几条：上面几条的断言里，常量与文案**同时**来自当前代码，
    只有当有人把数字写死时两者才分家；这里把「写死」这件事本身做成可复现的判据。
    """
    import services.ai.sheet_stats as sheet_stats

    original = sheet_stats._STATS_MAX_SAMPLES
    try:
        # 行数取 10 而不是 20：采样上限一改小，判「是不是数值列」的那个比值就可能不成立，
        # 这一列会**整列改走文本分支**（报「不同取值」而不是数值分布），数值那支的上限说明
        # 于是根本不出现 —— 那样这条测试测的就不是「标签跟不跟常量走」，而是另一个口径问题了。
        # 10 行 / 上限 7 能保证它仍被判为数值列。
        sheet_stats._STATS_MAX_SAMPLES = 7
        text = _render([[index] for index in range(10)])
        reported = _reported_number(_NUMERIC_CAP_NUMBER, text, "数值样本上限")
        assert reported == "7", (
            f"把常量改成 7 之后，统计里报的仍是 {reported} —— 说明这个数字是写死的，"
            f"不是从常量来的。\n实际输出：\n{text}"
        )
    finally:
        sheet_stats._STATS_MAX_SAMPLES = original


def test_a_numeric_column_with_more_rows_than_twice_the_sample_cap_is_still_numeric():
    """采样上限不该改变「这一列算数值列还是文本列」的判定。

    实测：一列 40,001 行时采样停在上限 20,000，若拿 `len(numbers)` 去判，
    `20000 * 2 >= 40001` 不成立，整列改走文本分支 —— 报出来是「不同取值 5000」，
    最小/中位/P90/最大**一个都不给**，而这一支给出的分位数正是 `value_sanity`
    维度要的比较基准（模块抬头说要避免的正是「拿孤零零一个数字去猜」）。
    """
    rows = [[index] for index in range(_STATS_MAX_SAMPLES * 2 + 1)]
    text = _render(rows)

    assert "中位" in text, (
        f"一列 {len(rows)} 行全是数值，却报了取值分布而不是数值分布——"
        f"分位数全丢了。\n实际输出：\n{text}"
    )


def test_a_minority_numeric_column_is_still_reported_as_text():
    """反向：数值只占少数时仍按取值分布报，别把上面那条修过头。"""
    rows = [[index if index < 3 else f"文本{index}"] for index in range(10)]
    text = _render(rows)

    assert "不同取值" in text, text
    assert "中位" not in text, text
    assert "其中数值 3" in text, f"数值个数没如实报出来：\n{text}"


def test_a_new_value_above_the_cap_does_not_silently_change_existing_counts():
    """撞上限之后，**已有**取值的计数仍要继续累加（不能因为满了就整列停摆）。

    这一条是上面那些断言的前提：如果满了之后连已有取值都不再计数，那「不同取值」这个数
    虽然小了，各取值的占比也全错了——两种失真叠在一起，报出来的「最多：X(N)」也不再可信。
    """
    rows = [[f"值{index}"] for index in range(_STATS_MAX_DISTINCT_TEXTS)]
    rows.append(["值0"])  # 已存在，应继续计数
    rows.append(["从没见过的值"])  # 新取值，会被丢掉
    text = _render(rows)

    assert "值0(2)" in text, f"已有取值在撞上限后没有再计数：\n{text}"
