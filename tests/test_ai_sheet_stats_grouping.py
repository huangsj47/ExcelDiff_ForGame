# -*- coding: utf-8 -*-
"""`_SheetStats` 的权重和 / 分组求和（WP12）。

## 为什么这两行值得单独守

`value_sanity` 的口径（`skills/version-diff-review/SKILL.md:221-222`）要求看「权重总和」，
而 `sheet_stats.py` 原先逐列只给 min/中位/P90/max：一列权重配成 `0.5 + 0.37` 时，分布
看起来完全正常，少配的那 0.13 只有**跨行求和**才看得见。同理，「按组求和后哪几组花掉了
大部分预算」要的是分组列与数值列在**同一行**上的配对，逐列的边际分位数反推不出来。

## 这里守的三件事

1. 权重和算的是**全表**（不受 20,000 行采样上限影响），偏离 1 / 100 时必须说「不等于」；
2. 分组和只取 Top 5，但必须同时说出**一共几组** —— 只说「Top 5」会被读成全部；
3. 识别不到权重/分组列时，渲染结果与旧版**逐字相同**（下面用固定字符串对比）。
   这一条尤其重要：列名误判会凭空多出一行「证据」，模型会把它当作真实口径去用，
   比没有这行更糟。

`width` / `generate` 这两个假列名是专门放的：英文关键词若按子串匹配，
`width` 会被当成 `id` 列、`generate` 会被当成 `rate` 列 —— 一张普通表凭空多出
分组求和与权重和。
"""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.ai.sheet_stats import _SheetStats  # noqa: E402


def _render(labels, rows, **kwargs) -> str:
    stats = _SheetStats()
    for row in rows:
        stats.add_row(row)
    return "\n".join(stats.render(labels, **kwargs))


class TestWeightSum:
    """权重/概率列的**总和**，以及「和是不是 1（或 100）」。"""

    def test_a_weight_column_that_does_not_add_up_is_reported(self):
        """0.5 + 0.37：逐列分布看不出问题，只有求和能看见少的 0.13。"""
        text = _render(["组", "权重"], [["A", 0.5], ["B", 0.37]])

        assert "  - 权重列「权重」：总和 0.87（不等于 1，也不等于 100，检查是否漏配/多配）" in text, text
        # 这两条是 WP12 的验收原话，单独拎出来，免得将来改写文案时被顺手删掉。
        assert "总和 0.87" in text, text
        assert "不等于 1" in text, text

    def test_a_balanced_weight_column_is_not_reported_as_deviation(self):
        """配平了的表不能被报成偏离 —— 否则模型每周都要去查一个不存在的漏配。"""
        text = _render(["权重"], [[0.5], [0.5]])

        assert "总和 1（等于 1" in text, text
        assert "不等于" not in text, text

    def test_percentage_weights_are_accepted_as_100(self):
        """概率/权重也有 0~100 的口径（万分比、百分比），100 不是偏离。"""
        text = _render(["概率"], [[60], [40]])

        assert "总和 100（等于 100" in text, text
        assert "不等于" not in text, text

    def test_text_values_in_a_weight_column_do_not_silently_shrink_the_sum(self):
        """非数值被跳过时必须说话：否则「总和 0.7」看起来像全表全量。"""
        text = _render(["权重"], [[0.5], ["无"], [0.2]])

        assert "总和 0.7" in text, text
        assert "另有 1 个非数值取值未计入" in text, text

    def test_english_weight_labels_are_recognised(self):
        """英文列名按词匹配（`drop_prob` 里的 `prob`）。"""
        text = _render(["drop_prob"], [[0.2], [0.2]])

        assert "权重列「drop_prob」：总和 0.4" in text, text

    def test_the_sum_is_full_table_even_when_samples_are_capped(self, monkeypatch):
        """权重和**不能**跟着采样上限走：一张四万行的表会被报成「和只有一半」。

        `_numeric_sums` 在 `add_row` 里按全部行累加（见 `__init__` 的注释），这里把
        采样上限压到 2 行来钉住这件事。
        """
        import services.ai.sheet_stats as sheet_stats

        monkeypatch.setattr(sheet_stats, "_STATS_MAX_SAMPLES", 2)
        text = _render(["权重"], [[0.25], [0.25], [0.25], [0.25]])

        assert "总和 1（等于 1" in text, text
        assert "抽样上限 2" in text, text  # 分位数那一支仍如实标注采样

    def test_an_all_text_weight_column_is_not_reported_as_a_sum(self):
        """列名像权重、值全是文本时不算权重列：报「总和 0」会是无中生有。"""
        text = _render(["权重"], [["高"], ["低"]])

        assert "权重列" not in text, text


class TestGroupSums:
    """按「ID/分组」列对数值列求 Top 5 分组和。"""

    def test_top_five_group_sums_are_reported_with_the_total_group_count(self):
        """A 组 3 行共 1200：逐行值 400 看起来一样，只有分组和能看出这一组占了预算。"""
        rows = [
            ["A", 400], ["A", 400], ["A", 400],
            ["B", 300], ["C", 250], ["D", 200], ["E", 150], ["F", 100],
        ]
        text = _render(["组", "数量"], rows)

        assert (
            "  - 按「组」分组求和（Top 5，共 6 组）：A=1200、B=300、C=250、D=200、E=150"
        ) in text, text
        assert "F=100" not in text, text
        # 追加在逐列统计之后（模型先看到逐列基准，再看汇总）。
        assert text.index("按「组」分组求和") > text.index("第 2 列"), text

    def test_an_id_column_with_repeated_ids_is_grouped(self):
        """`ID` 也是契约点名的分组列：重复 ID（一行一个子项）求和才是有信息的。"""
        text = _render(["ID", "数量"], [[1001, 10], [1001, 20], [1002, 5]])

        assert "  - 按「ID」分组求和（Top 5，共 2 组）：1001=30、1002=5" in text, text

    def test_a_unique_id_column_is_not_grouped(self):
        """唯一主键的分组和只是把每行原值抄一遍，Top 5 就是最大值 —— 不该占统计额度。

        配表几乎每张都带 `id`，不挡掉的话「权重和/Top 分组」这两行在真实提示词里
        会被这种没有聚合信息的行淹没。
        """
        text = _render(["id", "价值"], [[1, 10], [2, 20], [3, 30]])

        assert "分组求和" not in text, text

    def test_an_explicit_group_column_is_reported_even_if_each_group_appears_once(self):
        """`组` / `group` 是显式分组键，不受上面那条「唯一 ID」限制。"""
        text = _render(["组", "数量"], [["A", 10], ["B", 20]])

        assert "  - 按「组」分组求和（Top 5，共 2 组）：B=20、A=10" in text, text

    def test_text_and_empty_values_are_skipped_not_counted_as_zero(self):
        """文本/空值按契约跳过；跳过不是「按 0 计」，也不是把没有数值的组渲染出来。"""
        text = _render(
            ["组", "数量"],
            [["A", 100], ["A", 50], ["A", "无"], [None, 999], ["B", 30], ["C", "无"]],
        )

        assert "  - 按「组」分组求和（Top 5，共 2 组）：A=150、B=30" in text, text
        assert "C=" not in text, "只有文本取值的组没有数值可求和，不该出现在分组和里"

    def test_a_group_column_without_a_numeric_value_column_stays_silent(self):
        """只有分组列、没有可求和的数值列时不加行（否则「求和」是空的）。"""
        text = _render(["组", "说明"], [["A", "x"], ["B", "y"]])

        assert "分组求和" not in text, text

    def test_the_sample_cap_is_disclosed(self, monkeypatch):
        """分组和只覆盖采样到的行时必须说清，否则「共 N 组」会被读成全表。"""
        import services.ai.sheet_stats as sheet_stats

        monkeypatch.setattr(sheet_stats, "_STATS_MAX_SAMPLES", 2)
        text = _render(["组", "数量"], [["A", 10], ["A", 20], ["B", 30]])

        assert "采样仅前 2 行" in text, text
        assert "共 1 组" in text, text


class TestLabelsAreNotMisread:
    """英文关键词按词匹配：`width` 不是 ID 列、`generate` 不是权重列。"""

    def test_width_is_not_an_id_column_and_generate_is_not_a_weight_column(self):
        text = _render(
            ["width", "generate", "value"],
            [[1.1, "x", 2], [1.2, "y", 3]],
        )

        assert "权重列" not in text, text
        assert "分组求和" not in text, text


class TestDefaultOutputIsUnchanged:
    """没有权重/分组列时，渲染结果与旧版**逐字相同**（固定输入对比）。"""

    def test_a_table_without_weight_or_group_columns_renders_exactly_as_before(self):
        text = _render(["品质", "攻击"], [["普通", 10], ["稀有", 20], ["普通", 30]])

        assert text == (
            "- 整表统计（首行按列名，其余 3 行参与统计；与下面只展示前若干行无关。"
            "判断某个值是否合理时用它做比较基准）：\n"
            "  - 第 1 列（首行「品质」）：非空 3｜不同取值 2｜最多：普通(2)、稀有(1)\n"
            "  - 第 2 列（首行「攻击」）：非空 3｜数值 3｜最小 10｜中位 20｜P90 28｜最大 30"
        )
