#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表头探测器：把一个工作簿的前若干行读成「某列的逐行文本」。

`services/excel_header_profiles.py` 里的判据求值是纯函数（`probe(column, within)`
回调），真实的读表在这里 —— 分开是为了让匹配逻辑可测，也让「读文件」这件事
只有一个入口（要改读取方式时不会漏掉某一处）。

## 只读**第一张工作表**

表头格式是**文件级**的特征：一个配表文件里的各张工作表用同一套表头（实测
`常量表.xlsx` 的 10 张 sheet 就是同一套）。所以判据只在第一张表上求值 ——
「任一 sheet 满足即算命中」会让判据松一大截，而它本来是用来收紧的。

## 上限与惰性

* 一次最多看前 `MAX_PROBE_ROWS` 行（配表不会把表头特征放到更后面；`anywhere`
  判据也受这个上限约束，文档里写明）。
* **整个工作簿在一个 probe 实例里只打开一次** —— 一个文件上可能配了好几条判据，
  每条都重新 `load_workbook` 是没必要的重复解析。
* **只有真的配了 `header_detect` 规则时才会被调用**（见 `resolve_for_file` 的
  分支顺序）—— 没配的仓库在 diff 主路径上一次文件都不读。
"""

from __future__ import annotations

import io
from typing import Callable, Dict, List, Optional

from services.excel_header_profiles import column_index
from utils.logger import log_print

# 判据最多看前多少行。表头特征都在开头几行；给一个上限是为了让 `anywhere` 判据
# 在大表上不至于把整本书读进内存。
MAX_PROBE_ROWS = 2000


def make_probe(raw: Optional[bytes], *, sheet_name: str = "") -> Callable[[str, Optional[int]], List[str]]:
    """造一个探测器；`raw` 是工作簿的原始字节。

    `raw` 为空（调用方没有字节，例如只拿到了 diff 结果）时返回一个**永远读不到东西**
    的探测器 —— 判据于是判不成立，退回仓库默认坐标。这比抛错好：拿不到字节是
    「这条判据没法用」，不是「这次 diff 失败」。
    """
    cache: Dict[str, List[str]] = {}

    def probe(column: str, within: Optional[int]) -> List[str]:
        index = column_index(column)
        if index is None:
            return []
        limit = MAX_PROBE_ROWS if not within else min(int(within), MAX_PROBE_ROWS)
        key = f"{column}!{limit}"
        if key in cache:
            return cache[key]
        values = _read_column(raw, index, limit, sheet_name)
        cache[key] = values
        return values

    return probe


def _read_column(raw: Optional[bytes], index: int, limit: int, sheet_name: str) -> List[str]:
    """某列前 `limit` 行的文本，**按行对齐、含空串**（第 i 个 = 第 i+1 行）。

    按行对齐是 `evaluate_detection` 的硬要求：`r2:A == id` 问的是第 2 行那一格，
    把空行滤掉会让它去看第 3 行，而结果照样是个确定的真/假 —— 错得没有痕迹。
    """
    if not raw:
        return []
    try:
        from openpyxl import load_workbook
    except ImportError:  # pragma: no cover —— openpyxl 是平台的既有依赖
        return []
    book = None
    try:
        # `read_only=True` + `data_only=True`：与 AI 侧读正文（`services/ai/excel_source.py`）
        # 同一套开关。`read_only` 让 iter_rows 流式产出，下面的 break 才是真的「早停」。
        book = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
        if sheet_name and sheet_name in book.sheetnames:
            sheet = book[sheet_name]
        else:
            sheet = book[book.sheetnames[0]]
        values: List[str] = []
        for row in sheet.iter_rows(min_col=index + 1, max_col=index + 1, values_only=True):
            if len(values) >= limit:
                break
            cell = row[0] if row else None
            values.append("" if cell is None else str(cell))
        return values
    except Exception as exc:  # noqa: BLE001 —— 坏工作簿/加密文件都可能有
        # 只记日志不抛：探测失败的方向是「判据不成立 → 退回仓库默认坐标」，
        # 而抛出去会让整次 diff 失败 —— 为一个可选功能付那个代价不划算。
        log_print(
            f"⚠️ 表头探测读不出这张表（{type(exc).__name__}: {exc}），按未命中处理",
            "EXCEL",
        )
        return []
    finally:
        if book is not None:
            try:
                book.close()
            except Exception:  # noqa: BLE001 —— 关不掉不影响结果
                pass
