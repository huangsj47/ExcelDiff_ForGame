"""配表整表统计：把一个工作表的规模、取值分布压成一小段可读的文字。

## 为什么单独一层

`value_sanity` 维度要求模型判断「这个值合不合理」，而判断必须有比较基准。配表动辄
几千行、上百列，**整表塞进提示词是不可能的**（`file_content` 单条上限 11,000 字符）；
只给改动的那一个单元格又等于让它拿孤零零一个数字猜。所以折中是「整表统计」这一段：
列数、（取值范围的）分位数、去重取值数与最常见的几个、空值率 —— 它排在正文**之前**
（正文按「只砍尾巴」截断，放在后面就会被砍掉）。

## 它是纯函数

输入是逐行的单元格文本，输出是一段文字。不碰文件、不碰数据库 —— 所以统计口径
（哪些行算数、哪些列进了上限、抽样到了没有）可以被完整单测，而这一层最容易出错的地方
恰恰是**口径**：统计说「不同取值 5000」时，真实值可能更大，模型会拿这个数当「允许集合」
的依据（见 `render` 里那几处如实标注的上限）。

从 `services/ai/platform_provider.py` 拆出来：那个文件已经越过仓库的 2000 行硬上限
（`scripts/check_file_length.py --strict`），而这一块与「向模型交付什么」的其余部分
没有耦合 —— 它只被 `_read_excel_sheets` 与 `_assemble_workbook` 用。

## 分组求和与权重和（WP12）

逐列的 min/中位/P90/max 回答不了「这一列的权重加起来是不是 1」—— 那要跨行求和；
也回答不了「按组求和后哪几组最大」—— 那要**同一行上**的分组列与数值列配对，
从逐列的边际分布反推不出来。所以这里按列名识别权重/分组列，额外给几行：权重和
（全表累加，不受采样上限影响）与 Top 5 分组和。识别不到这两类列时**一行都不加**，
输出与旧版逐字相同。

列名误判会凭空多出一行「证据」，比没有更糟，所以英文关键词按分词匹配：
`width` 里的 `id` 不算 ID 列，`generate` 里的 `rate` 不算权重列。
"""

from __future__ import annotations

import re
from typing import Sequence

# 整表统计的规模上限。这些数字决定「统计块」的字符数上界 —— 它要挤在
# `file_content` 的 11,000 字符额度里，而且必须排在正文**之前**（正文按「只砍尾巴」
# 截断，放在后面就会被砍掉）。
_STATS_COLUMNS_LIMIT = 24
_STATS_TOP_VALUES = 3
_STATS_MAX_SAMPLES = 20_000
# 每列最多记多少个**不同取值**。超了就不再收新取值（计数也不再加），见 `add_row`。
# 这个数必须与 `_STATS_MAX_SAMPLES` 一样**如实报出去**：统计说「不同取值 5000」时，
# 真实值可能远大于 5000，而模型会拿这个数当「允许集合有多大」的依据（见模块抬头）。
_STATS_MAX_DISTINCT_TEXTS = 5_000

# 分组求和的 Top N（契约 WP12 要求 N ≤ 5）。
_STATS_GROUP_TOP = 5
# 一张表最多看几个分组列、每个分组列最多对几个数值列求和。配表里「组/ID/group」
# 这类列可能并排好几列（大组、子组、难度ID），两两配对会让统计块按列数²膨胀 ——
# 而统计块要挤在 `file_content` 的单条额度里（见模块抬头），外加的下限就在这。
_STATS_GROUP_COLUMNS = 2
_STATS_GROUP_VALUE_COLUMNS = 2
# 权重/概率列同样限个数：`概率1..概率5` 这种并列列很常见，全报会把正文挤掉。
_STATS_WEIGHT_COLUMNS = 3
# 权重和的比较容差。0.1 + 0.2 这类十进制小数在二进制里不精确（0.30000000000000004），
# 用 `== 1` 判会把「配平了的表」报成偏离。
_WEIGHT_SUM_TOLERANCE = 1e-6

# 列名识别。中文按「包含」；英文按分词后的词（理由见模块抬头：`width` 含 `id`、
# `generate` 含 `rate`，直接 `in` 会凭空造出一行权重/分组统计）。
_WEIGHT_LABEL_WORDS = ("权重", "概率")
_WEIGHT_LABEL_TOKENS = ("weight", "rate", "prob")
_GROUP_LABEL_WORDS = ("组",)
_GROUP_LABEL_IDS = ("id",)
_ASCII_WORD = re.compile(r"[a-z0-9]+")


def _label_tokens(label: str) -> set[str]:
    """列名里的英文/数字词。中文列名（如 `道具ID`）会得到 `{"id"}`。"""
    return set(_ASCII_WORD.findall(label.lower()))


def _is_weight_label(label: str) -> bool:
    """列名像「权重/概率」吗。英文按**词首**匹配，所以 `weights` / `probability` 也算。"""
    lowered = label.lower()
    if any(word in lowered for word in _WEIGHT_LABEL_WORDS):
        return True
    return any(
        token.startswith(prefix)
        for token in _label_tokens(lowered)
        for prefix in _WEIGHT_LABEL_TOKENS
    )


def _is_group_label(label: str) -> bool:
    """列名像「ID/分组」吗。`id` 只认独立词 —— `width`、`valid` 里的 `id` 不算。"""
    lowered = label.lower()
    if any(word in lowered for word in _GROUP_LABEL_WORDS):
        return True
    return any(
        token.startswith("group") or token in _GROUP_LABEL_IDS
        for token in _label_tokens(lowered)
    )


def _is_id_only_group_label(label: str) -> bool:
    """这个分组列是不是**只**靠 `id` 命中的（没有「组/group」这些显式分组词）。

    用来区分 `组` 列与 `id` 列：`组` 天然是分组键，值只出现一次也照求；而 `id` 在配表里
    多半是唯一主键，每行一个值时「分组和」只是把原值抄一遍（见 `_group_sum_lines`）。
    """
    lowered = label.lower()
    if any(word in lowered for word in _GROUP_LABEL_WORDS):
        return False
    return not any(token.startswith("group") for token in _label_tokens(lowered))


def _number_text(value: float) -> str:
    """数值的紧凑写法：整数不带 `.0`，浮点不留 `0.30000000000000004` 这种尾巴。"""
    if isinstance(value, bool):  # bool 是 int 的子类，配表里少见但要挡住
        return str(value)
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6g}"


class _SheetStats:
    """一个工作表的整表统计。

    存在的理由：`value_sanity` 维度要求「判断这个值合不合理」，而判断必须有比较基准。
    配表动辄几千行、上百列，**整表塞进提示词是不可能的**（`file_content` 单条上限
    11,000 字符）；只给改动的那一个单元格又等于让模型拿孤零零一个数字猜 ——
    「这个值看起来很大」正是这个平台反复要挡掉的那种「证据」。

    于是这里做一件平台**做得到而模型做不到**的事：把整表读一遍，只把分布交出去。
    **统计是按截断之前的全部行算的**，所以哪怕正文只剩前 200 行，基准依然是完整的全表。
    """

    def __init__(self) -> None:
        self.rows = 0
        self.width = 0
        self._numbers: list[list[float]] = []
        self._texts: list[dict[str, int]] = []
        self._nonempty: list[int] = []
        # 数值型取值的**总个数**，不受 `_STATS_MAX_SAMPLES` 影响。判断「这一列是不是数值列」
        # 必须用它，不能用 `len(self._numbers)` —— 后者是**采样后**的长度（见 `render`）。
        self._numeric_seen: list[int] = []
        self._numeric_capped: list[bool] = []
        self._text_capped: list[bool] = []
        # 权重/概率列的运行总和。**按全部行累加**，不经过 `_STATS_MAX_SAMPLES` 采样 ——
        # 一个 float 加法器不占地方，而「权重和是不是 1」只有算全表才有意义；
        # 采样到一半停下会把一张配平的表报成「和小于 1」。
        self._numeric_sums: list[float] = []
        # 分组求和的原料：逐行的原始取值，只留前 `_STATS_MAX_SAMPLES` 行。
        # 为什么必须留下逐行数据：分组求和要的是「组」列与数值列在**同一行**上的配对，
        # 而上面的分位数/取值分布是逐列独立的边际统计，从它们反推不出这个配对。
        # 上限沿用同一个采样上限，超了在渲染时如实标注（见 `_group_sum_lines`）。
        self._sample_rows: list[tuple] = []
        self._rows_capped = False

    def _slot(self, index: int) -> int:
        while len(self._numbers) <= index:
            self._numbers.append([])
            self._texts.append({})
            self._nonempty.append(0)
            self._numeric_seen.append(0)
            self._numeric_capped.append(False)
            self._text_capped.append(False)
            self._numeric_sums.append(0.0)
        return index

    def add_row(self, row) -> None:
        """把一行计入统计。**首行由调用方按列名处理，不进这里** —— 表头本身不是数据：

        把 `品质` 这一列表头文字 `品质` 当成一个取值，会让每个文本列都多出一个
        「只出现一次的取值」，模型据此判断「这个值不在允许集合里」时会先撞上它。
        """
        values = list(row)
        if not any(value is not None and str(value).strip() for value in values):
            return  # 整行为空：与正文渲染一致，不计入
        self.rows += 1
        self.width = max(self.width, len(values))
        # 只留前 `_STATS_MAX_SAMPLES` 行给分组求和：更长的表按同一口径采样（`render` 会
        # 标注这是采样）。每格文本只留 20 字 —— 分组键在渲染时本来就截到 20 字，而整格
        # 留下来会让一张宽文本表多占几十 MB，这一段恰恰是「不能把整表留在内存里」的由来。
        sampled: list = [None] * min(len(values), _STATS_COLUMNS_LIMIT)
        for index, value in enumerate(values):
            if index >= _STATS_COLUMNS_LIMIT:
                break
            if value is None or not str(value).strip():
                continue
            slot = self._slot(index)
            self._nonempty[slot] += 1
            text = str(value).strip()
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._numeric_seen[slot] += 1
                # 总和在这里累加：与 `_numbers` 的采样无关，见 `__init__` 里的说明。
                self._numeric_sums[slot] += float(value)
                if len(self._numbers[slot]) < _STATS_MAX_SAMPLES:
                    self._numbers[slot].append(float(value))
                else:
                    self._numeric_capped[slot] = True
                sampled[index] = value
            else:
                sampled[index] = text[:20]
            if len(self._texts[slot]) < _STATS_MAX_DISTINCT_TEXTS:
                self._texts[slot][text[:40]] = self._texts[slot].get(text[:40], 0) + 1
            elif text in self._texts[slot]:
                self._texts[slot][text[:40]] += 1
            else:
                # 已经在取值上限上，又是一个**没见过**的取值：它进不了统计。记下来
                # 只为在渲染时如实说一句「这一列的不同取值不止这些」——不声不响地丢掉，
                # 模型会把「不同取值 5000」读成真实取值个数。
                self._text_capped[slot] = True
        if len(self._sample_rows) < _STATS_MAX_SAMPLES:
            self._sample_rows.append(tuple(sampled))
        else:
            self._rows_capped = True

    def render(self, column_labels: Sequence[str], *, header_count: int = 1,
               name_row: int = 1) -> list[str]:
        """渲染成给模型看的几行。`column_labels` 是列名行的值（配表通常就是列名）。

        `header_count` / `name_row` 只在仓库配了表头坐标时才有意义
        （见 `_read_excel_sheets`）：那时抬头必须**如实说清**列名取自第几行、有几行被
        当作表头没进统计 —— 否则「表头 3 行」的表在这里看起来就是「凭空少了两行」。
        未配置（默认值）时这句话与今天**逐字相同**（既有断言按「首行按列名」认统计块）。
        """
        if self.rows == 0:
            return []
        # 仓库配了表头坐标（表头块 > 1 行，或列名不在第 1 行）时，抬头必须说清列名取自
        # 第几行、有几行没进统计；否则「列名取自第 2 行」的表在这里看起来就是「凭空少一行」。
        configured = header_count > 1 or name_row > 1
        if configured:
            head = [
                f"- 整表统计（列名取自第 {name_row} 行，表头共 {header_count} 行不计入统计与"
                f"正文；其余 {self.rows} 行参与统计；与下面只展示前若干行无关。"
                "判断某个值是否合理时用它做比较基准）："
            ]
        else:
            head = [
                f"- 整表统计（首行按列名，其余 {self.rows} 行参与统计；与下面只展示前若干行无关。"
                "判断某个值是否合理时用它做比较基准）："
            ]
        lines: list[str] = []
        shown = min(self.width, _STATS_COLUMNS_LIMIT)
        # 列名取自哪一行决定这一句怎么写：没配表头坐标时它就是首行（今天的文案，逐字不变）；
        # 配了名称行之后写「首行」是错的（列名来自第 2 行），而模型正是靠这句话把统计里的
        # 「第 N 列」与表里的字段对上。
        label_word = "列名" if configured else "首行"
        for index in range(shown):
            label = column_labels[index] if index < len(column_labels) else ""
            label = str(label or "").strip()[:20]
            name = f"第 {index + 1} 列" + (f"（{label_word}「{label}」）" if label else "")
            non_empty = self._nonempty[index] if index < len(self._nonempty) else 0
            if non_empty == 0:
                continue
            numbers = self._numbers[index] if index < len(self._numbers) else []
            numeric_seen = self._numeric_seen[index] if index < len(self._numeric_seen) else 0
            # 半数以上是数值就按数值列报分布；否则按取值分布报（品质、类型、状态这类
            # 枚举列要看到「有哪些取值、各占多少」，那正是「不在允许集合里」的依据）。
            # 判据收敛在 `_numeric_column`：分组求和与这里必须用**同一个**「数值列」口径。
            if self._numeric_column(index):
                ordered = sorted(numbers)
                cap = (
                    f"（抽样上限 {_STATS_MAX_SAMPLES}）"
                    if self._numeric_capped[index]
                    else ""
                )
                lines.append(
                    f"  - {name}：非空 {non_empty}｜数值 {len(numbers)}{cap}｜"
                    f"最小 {_number_text(ordered[0])}｜中位 {_number_text(_percentile(ordered, 0.5))}｜"
                    f"P90 {_number_text(_percentile(ordered, 0.9))}｜最大 {_number_text(ordered[-1])}"
                )
            else:
                buckets = self._texts[index] if index < len(self._texts) else {}
                top = sorted(buckets.items(), key=lambda item: (-item[1], item[0]))
                top_text = "、".join(f"{value}({count})" for value, count in top[:_STATS_TOP_VALUES])
                # 与数值那一支同一条口径：撞了上限就**如实说**，并且说清这不是真实取值数。
                capped = self._text_capped[index] if index < len(self._text_capped) else False
                cap = (
                    f"（取值上限 {_STATS_MAX_DISTINCT_TEXTS}，实际不止这些）"
                    if capped
                    else ""
                )
                lines.append(
                    f"  - {name}：非空 {non_empty}｜不同取值 {len(buckets)}{cap}｜"
                    + (f"最多：{top_text}" if top_text else "（无文本取值）")
                    # 这里同样报**总个数**：`len(numbers)` 是采样后的，一列四万多行时会少报。
                    # 走到这一支就意味着数值不到半数（`numeric_seen == 0` 或 `*2 < non_empty`），
                    # 所以只要 `numeric_seen` 非零就该说一句。
                    + (f"｜其中数值 {numeric_seen}" if numeric_seen else "")
                )
        if self.width > shown:
            lines.append(f"  - （另有 {self.width - shown} 列未做统计。）")
        # 分组/权重统计**追加在现有统计段之后**：命中不了关键词时一行都不加，
        # 输出与旧版逐字相同（既有断言是按旧文案写的）。
        lines.extend(self._group_stats_lines(column_labels))
        return head + lines if lines else []

    def _numeric_column(self, index: int) -> bool:
        """这一列算不算「数值列」。

        判据是**数值型取值的总个数**，不是采样后的 `len(self._numbers)`。后者有上限：
        一列四万多行时采样停在上限，`len(numbers) * 2 >= non_empty` 就不成立，整列改走
        文本分支 —— 报出来的是「不同取值 5000」，最小/中位/P90/最大一个都不给。而这一支
        给出的分布正是 `value_sanity` 维度要的比较基准（模块抬头说要避免的正是「拿孤零零
        一个数字去猜」）。实测 40,001 行即触发。
        """
        non_empty = self._nonempty[index] if index < len(self._nonempty) else 0
        numeric_seen = self._numeric_seen[index] if index < len(self._numeric_seen) else 0
        return bool(non_empty and numeric_seen and numeric_seen * 2 >= non_empty)

    def _group_stats_lines(self, column_labels: Sequence[str]) -> list[str]:
        """权重和 + 按分组列求和。识别不到这两类列时返回空列表（旧输出不变）。"""
        shown = min(self.width, _STATS_COLUMNS_LIMIT)
        labels = [
            str(column_labels[index] or "").strip() if index < len(column_labels) else ""
            for index in range(shown)
        ]
        return self._weight_lines(labels) + self._group_sum_lines(labels)

    def _weight_lines(self, labels: Sequence[str]) -> list[str]:
        """疑似权重/概率列的**全表**总和，以及与 1（或 100）是否一致。

        为什么必须报「和」：概率和/权重和是确定性口径（`skills/version-diff-review/
        SKILL.md` 写了「权重总和」），而逐列分位数看不出「少配一条、多配一条」。
        """
        indexes = [
            index
            for index in range(len(labels))
            if _is_weight_label(labels[index]) and self._numeric_column(index)
        ]
        lines: list[str] = []
        for index in indexes[:_STATS_WEIGHT_COLUMNS]:
            total = self._numeric_sums[index] if index < len(self._numeric_sums) else 0.0
            # 容差比较：0.1 + 0.2 = 0.30000000000000004，`== 1` 会把配平的表报成偏离。
            if abs(total - 1.0) <= _WEIGHT_SUM_TOLERANCE:
                verdict = "等于 1，符合权重和口径"
            elif abs(total - 100.0) <= _WEIGHT_SUM_TOLERANCE:
                verdict = "等于 100，按百分比权重看符合"
            else:
                verdict = "不等于 1，也不等于 100，检查是否漏配/多配"
            # 非数值取值（如「-」「无」）被跳过了：不说的话，报出来的和看起来像全表全量。
            non_empty = self._nonempty[index] if index < len(self._nonempty) else 0
            numeric_seen = self._numeric_seen[index] if index < len(self._numeric_seen) else 0
            skipped = (
                f"｜另有 {non_empty - numeric_seen} 个非数值取值未计入"
                if non_empty > numeric_seen
                else ""
            )
            lines.append(
                f"  - 权重列{self._column_name(index, labels[index])}："
                f"总和 {_number_text(total)}（{verdict}）{skipped}"
            )
        if len(indexes) > _STATS_WEIGHT_COLUMNS:
            lines.append(
                f"  - （另有 {len(indexes) - _STATS_WEIGHT_COLUMNS} 个疑似权重列未统计总和。）"
            )
        return lines

    def _group_sum_lines(self, labels: Sequence[str]) -> list[str]:
        """按分组列对每个数值列求 Top N 分组和。

        分组列与数值列必须在**同一行**上配对，所以只能用 `_sample_rows`（逐行原料）；
        超过采样上限时分组和只覆盖前 N 行，必须如实标注 —— 否则模型会把「前 20000 行
        的组和」当成全表组和（模块抬头那条「统计数小于真实值要说话」的同一条纪律）。

        **ID 列每行一个取值时不输出**：那只是把该行原值换个写法抄一遍（Top 5 就是
        最大的 5 个值，逐列统计里已经有 max），不提供任何聚合信息。配表几乎每张都有
        `id` 这种唯一主键，不挡掉的话统计块会被这种噪音占满 —— 而这一段的额度是从
        正文里让出来的。显式分组列（`组` / `group`）不受这条限制：它本来就是分组键。
        """
        group_indexes = [
            index for index in range(len(labels)) if _is_group_label(labels[index])
        ][:_STATS_GROUP_COLUMNS]
        if not group_indexes:
            return []
        # 分组列本身若也是数值列（ID 列），不参与求和：ID 之和没有业务含义。
        value_indexes = [
            index
            for index in range(len(labels))
            if index not in group_indexes and self._numeric_column(index)
        ][:_STATS_GROUP_VALUE_COLUMNS]
        if not value_indexes:
            return []
        multiple_values = len(value_indexes) > 1
        lines: list[str] = []
        for group_index in group_indexes:
            skip_unique_keys = _is_id_only_group_label(labels[group_index])
            for value_index in value_indexes:
                totals, paired_rows = self._sum_by_group(group_index, value_index)
                if not totals or (skip_unique_keys and len(totals) == paired_rows):
                    continue
                top = sorted(totals.items(), key=lambda item: (-item[1], item[0]))[
                    :_STATS_GROUP_TOP
                ]
                body = "、".join(f"{key}={_number_text(value)}" for key, value in top)
                # 只说 Top 5 而不说一共几组，读的人会以为是全部（同「另有 N 列」那条纪律）。
                qualifier = f"Top {_STATS_GROUP_TOP}，共 {len(totals)} 组"
                if multiple_values:
                    qualifier += f"，值列{self._column_name(value_index, labels[value_index])}"
                if self._rows_capped:
                    qualifier += f"，采样仅前 {_STATS_MAX_SAMPLES} 行"
                lines.append(
                    f"  - 按{self._column_name(group_index, labels[group_index])}"
                    f"分组求和（{qualifier}）：{body}"
                )
        return lines

    def _sum_by_group(self, group_index: int, value_index: int) -> tuple[dict[str, float], int]:
        """分组列取值 → 该组内数值列之和，以及实际配上的行数。

        配上的行数用来判「分组键是不是每行都不一样」（见 `_group_sum_lines`）：那种表
        的分组和没有聚合信息。文本/空值按契约跳过。
        """
        totals: dict[str, float] = {}
        paired_rows = 0
        for row in self._sample_rows:
            if group_index >= len(row) or value_index >= len(row):
                continue
            key_value = row[group_index]
            if key_value is None or not str(key_value).strip():
                continue
            number = row[value_index]
            if not isinstance(number, (int, float)) or isinstance(number, bool):
                continue
            key = str(key_value).strip()[:20]
            totals[key] = totals.get(key, 0.0) + float(number)
            paired_rows += 1
        return totals, paired_rows

    @staticmethod
    def _column_name(index: int, label: str) -> str:
        """`「组」` 或（没有列名时）`第 2 列` —— 与逐列统计里同一种叫法。"""
        label = str(label or "").strip()[:20]
        return f"「{label}」" if label else f"第 {index + 1} 列"


def _percentile(ordered: Sequence[float], ratio: float) -> float:
    """已排序列表的分位数（线性插值）。"""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = ratio * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight
