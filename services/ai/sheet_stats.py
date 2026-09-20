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
"""

from __future__ import annotations

from typing import Sequence

# 整表统计的规模上限。这些数字决定「统计块」的字符数上界 —— 它要挤在
# `file_content` 的 11,000 字符额度里，而且必须排在正文**之前**（正文按「只砍尾巴」
# 截断，放在后面就会被砍掉）。
_STATS_COLUMNS_LIMIT = 24
_STATS_TOP_VALUES = 3
_STATS_MAX_SAMPLES = 20_000


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
        self._numeric_capped: list[bool] = []

    def _slot(self, index: int) -> int:
        while len(self._numbers) <= index:
            self._numbers.append([])
            self._texts.append({})
            self._nonempty.append(0)
            self._numeric_capped.append(False)
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
        for index, value in enumerate(values):
            if index >= _STATS_COLUMNS_LIMIT:
                break
            if value is None or not str(value).strip():
                continue
            slot = self._slot(index)
            self._nonempty[slot] += 1
            text = str(value).strip()
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if len(self._numbers[slot]) < _STATS_MAX_SAMPLES:
                    self._numbers[slot].append(float(value))
                else:
                    self._numeric_capped[slot] = True
            if len(self._texts[slot]) < 5_000:
                self._texts[slot][text[:40]] = self._texts[slot].get(text[:40], 0) + 1
            elif text in self._texts[slot]:
                self._texts[slot][text[:40]] += 1

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
            # 半数以上是数值就按数值列报分布；否则按取值分布报（品质、类型、状态这类
            # 枚举列要看到「有哪些取值、各占多少」，那正是「不在允许集合里」的依据）。
            if numbers and len(numbers) * 2 >= non_empty:
                ordered = sorted(numbers)
                cap = "（抽样上限 20000）" if self._numeric_capped[index] else ""
                lines.append(
                    f"  - {name}：非空 {non_empty}｜数值 {len(numbers)}{cap}｜"
                    f"最小 {_number_text(ordered[0])}｜中位 {_number_text(_percentile(ordered, 0.5))}｜"
                    f"P90 {_number_text(_percentile(ordered, 0.9))}｜最大 {_number_text(ordered[-1])}"
                )
            else:
                buckets = self._texts[index] if index < len(self._texts) else {}
                top = sorted(buckets.items(), key=lambda item: (-item[1], item[0]))
                top_text = "、".join(f"{value}({count})" for value, count in top[:_STATS_TOP_VALUES])
                lines.append(
                    f"  - {name}：非空 {non_empty}｜不同取值 {len(buckets)}｜"
                    + (f"最多：{top_text}" if top_text else "（无文本取值）")
                    + (f"｜其中数值 {len(numbers)}" if numbers and len(numbers) * 2 < non_empty else "")
                )
        if self.width > shown:
            lines.append(f"  - （另有 {self.width - shown} 列未做统计。）")
        return head + lines if lines else []


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
