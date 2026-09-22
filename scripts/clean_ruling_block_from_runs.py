#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把**历史运行**里那段机器裁决 JSON 从报告正文里清掉（AI-P0-05 的一次性数据清理）。

    # 先看会动哪些行（默认**只打印、不写库**）
    python scripts/clean_ruling_block_from_runs.py

    # 确认无误之后才写
    python scripts/clean_ruling_block_from_runs.py --apply

    # 换一个库（默认是仓库的 instance/ 那个）
    python scripts/clean_ruling_block_from_runs.py --db instance/diff_platform.db

## 清的是哪一段

`ai-analysis` 报告正文末尾曾经被追加过一行 HTML 注释：

    <!-- ai-verify-ruling: {"verdicts_seen": 1, "rows": [...]} -->

它是平台写给自己的（`verdict.ruling_block` 写、`result_payload.read_ruling` 读回来），
假设「markdown 渲染看不见它」。**那个假设是错的**：本平台的安全渲染器是先整体转义、
再套白名单（`static/js/ai-report-markdown.js`），于是它变成一段可见的乱码 —— 实测
run 20 的 `response_text` 共 35,763 字符，其中这段 json 占 **12,617（35.3%）**，
用户在页面上真的看到了它（run 15 是 9,429 / 22,409 = 42.1%）。全库 21 条 run 里只有
run 15 与 run 20 带这个块，`ai_analysis_trace` 里 0 条。

新代码不再写它（裁决走 `EngineOutcome.verdict` 这个结构化字段）。这个脚本负责把
**已经落库的那几条**擦干净 —— 它们会一直被读到（抽屉、历史列表、导出）。

## 清了会不会丢信息

不会。裁决的**结构化副本**在做这次运行的时候就已经落进 `response_payload` 了
（`final_findings` / `retracted_findings`，见 `services/ai/result_payload.py`），
正文里那一行只是同一份数据的第二个副本。清掉的是副本，不是数据。

## 三条约束

1. **默认不写库。** 生产库上可能正跑着真实分析（复测文档第 6 节）—— 一个"跑一下看看"
   就把别人的运行数据改了是不可接受的。`--apply` 是显式开关，而且写之前先把每一行的
   **改动前后摘要**打出来。
2. **幂等。** 摘块用的是 `verdict.strip_ruling_block`（同一个正则），没有块的行原样返回
   —— 跑第二次的结果是「0 条需要清理」，不是"越跑越少"。
3. **只动这两列**：`ai_analysis_run.response_text` 与 `ai_analysis_run.response_payload`
   里 JSON 的 `report_markdown`。其余列（`final_findings` / `retracted_findings` /
   `anomalies`）一个字节都不碰 —— 那是结论本体。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from services.ai.verdict import RULING_BLOCK_MARKER, strip_ruling_block  # noqa: E402
from utils.runtime_paths import resolve_runtime_path  # noqa: E402

# 预览里每段最多打多少字符。整段 json 有十几 KB —— 全打出来没人看得完，
# 而这一段的作用只是让人确认「删的确实是那一行注释」。
_PREVIEW_CHARS = 120


def _preview(text: str) -> str:
    """把一段文本压成一行预览（换行折成 `⏎`，超长截断）。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= _PREVIEW_CHARS:
        return flat
    return flat[:_PREVIEW_CHARS] + "…"


def _strip_payload(raw: str) -> tuple[str, bool]:
    """`response_payload` 那一列：只摘 `report_markdown` 里的块。

    返回 `(新的 json 文本, 有没有改动)`。**认不出来的形状原样返回**（不是 JSON、不是
    对象、没有 `report_markdown`、以及 `report_markdown` 里没有块）—— 这一列的键很多，
    解析不动的时候宁可不动它，也不要把它写坏（那会让这一行彻底读不出结论）。
    """
    if not raw or RULING_BLOCK_MARKER not in raw:
        return raw, False
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw, False
    if not isinstance(payload, dict):
        return raw, False
    report = payload.get("report_markdown")
    if not isinstance(report, str) or RULING_BLOCK_MARKER not in report:
        return raw, False
    cleaned = strip_ruling_block(report)
    if cleaned == report:
        return raw, False
    payload["report_markdown"] = cleaned
    return json.dumps(payload, ensure_ascii=False), True


def _rows_needing_cleanup(cursor: sqlite3.Cursor) -> list[tuple[int, str, str]]:
    """所有正文里带那个标记的行：`(run_id, response_text, response_payload)`。

    筛选放在 SQL 里（`LIKE`）而不是把整表拉进内存：`response_text` 是几十 KB 的正文，
    全库拉一遍就是几十 MB —— 而这一步在**默认的只打印模式**下也要跑。

    `LIKE` 匹配的是标记字面量（`ai-verify-ruling`），它只出现在那行注释里。
    """
    cursor.execute(
        "SELECT id, response_text, response_payload FROM ai_analysis_run "
        "WHERE response_text LIKE ? OR response_payload LIKE ? ORDER BY id",
        (f"%{RULING_BLOCK_MARKER}%", f"%{RULING_BLOCK_MARKER}%"),
    )
    return [(row[0], row[1] or "", row[2] or "") for row in cursor.fetchall()]


def _count_in_trace(cursor: sqlite3.Cursor) -> int:
    """`ai_analysis_trace` 里带标记的行数（只报数，本脚本**不动它**）。

    审计里这一列是 0 条。留着这一行是因为「这里也有一份」是个很容易漏掉的假设 ——
    真出现了就得单独决策（那一列是按轮次的明细，与报告正文不是一回事）。
    """
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM ai_analysis_trace WHERE response_text LIKE ?",
            (f"%{RULING_BLOCK_MARKER}%",),
        )
        return int(cursor.fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="清掉历史运行报告正文里的机器裁决块（默认只打印）"
    )
    parser.add_argument(
        "--db",
        default="",
        help="SQLite 库文件路径（默认 instance/diff_platform.db）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的写库。不加这个开关时只打印会动哪些行。",
    )
    args = parser.parse_args(argv)

    db_path = args.db or resolve_runtime_path("instance/diff_platform.db")
    if not os.path.exists(db_path):
        # sqlite3.connect 对不存在的路径是「创建」而不是报错：一个只读清理脚本
        # 顺手建出一个空库，比什么都不做更糟。
        print(f"数据库文件不存在：{db_path}")
        return 2

    print(f"数据库：{db_path}")
    print(f"模式：{'写库（--apply）' if args.apply else '只打印（加 --apply 才会写）'}")

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        rows = _rows_needing_cleanup(cursor)
        trace_count = _count_in_trace(cursor)
        print(f"正文里带机器块的运行：{len(rows)} 条")
        print(f"（ai_analysis_trace 里带机器块的行：{trace_count} 条 —— 本脚本不动那一列）")
        if not rows:
            print("没有需要清理的行（这个脚本是幂等的，重复跑就是这句话）。")
            return 0

        changed_text = 0
        changed_payload = 0
        for run_id, text, raw_payload in rows:
            new_text = strip_ruling_block(text)
            new_payload, payload_changed = _strip_payload(raw_payload)
            text_changed = new_text != text
            changed_text += int(text_changed)
            changed_payload += int(payload_changed)
            print(
                f"- run {run_id}: response_text {'要清 ' + str(len(text) - len(new_text)) + ' 字符' if text_changed else '无需改动'}"
                f"；response_payload {'要清' if payload_changed else '无需改动'}"
            )
            print(f"    清理前：…{_preview(text[-_PREVIEW_CHARS * 2:])}")
            print(f"    清理后：…{_preview(new_text[-_PREVIEW_CHARS:])}")
            if args.apply:
                cursor.execute(
                    "UPDATE ai_analysis_run SET response_text = ?, response_payload = ? WHERE id = ?",
                    (
                        new_text,
                        new_payload if payload_changed else raw_payload,
                        run_id,
                    ),
                )

        if args.apply:
            conn.commit()
            print(
                f"已写库：response_text {changed_text} 条、"
                f"response_payload.report_markdown {changed_payload} 条。"
            )
            # 再查一遍证明幂等（也是「写成功了没有」的当场证据）。
            print(f"复查：仍带机器块的行 {len(_rows_needing_cleanup(cursor))} 条。")
        else:
            print(
                f"**没有写库**（打印出来的是将要发生的改动）："
                f"response_text 会改 {changed_text} 条、"
                f"response_payload.report_markdown 会改 {changed_payload} 条。"
            )
            print("确认无误后加 --apply 重跑。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
