# -*- coding: utf-8 -*-
"""一次性脚本：把 trace 里的真实「工具调用信封」原文导出成 fixture 文件。

## 为什么要有这个脚本

样本必须逐字真实（本仓吃过「为第二条分支挑的 fixture 其实不在那条分支上」的亏），
而这三段原文里带着的 HTML 形态，一旦经过任何「人/模型的手」转发就会被当成工具调用，
既烧轮次又会把样本改形。所以：**连着只读 DB，二进制写出**，原文的字节从头到尾只走
数据通道。测试从文件读，不再粘贴。

只读查询（`mode=ro`），不写库、不碰线上实例。
"""
from __future__ import annotations

import os
import sqlite3

REPO = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
DB = os.path.join(REPO, "instance", "diff_platform.db")
OUT = os.path.dirname(os.path.abspath(__file__))

# (fixture 文件名, trace.id, 期望来源) —— id 是 `ai_analysis_trace` 的主键，
# 一行就是一轮的原文（unparsable 轮按 UNPARSABLE_RESPONSE_MAX_CHARS 存，没截）。
SAMPLES = (
    ("run44_s4_file_diff_and_evidence.txt", 1017, "run 44 / S4 / 第 1 轮"),
    ("run38_v1_evidence.txt", 849, "run 38 / V1 / 第 1 轮"),
    ("run44_s2_think_only.txt", 1006, "run 44 / S2 / 第 1 轮"),
    ("run45_summary_evidence_only.txt", 1061, "run 45 / 汇总 / 第 1 轮"),
    # 第五段：**当前判据不覆盖**的形态（参数写成子标签而不是体或属性）。
    # 留着它是为了让「哪些形态救了、哪些没救」在 fixture 里就是一条可读的事实，
    # 而不是将来有人以为「信封都覆盖了」。用例见 `test_ai_protocol_dsml_envelope.py`。
    ("run44_s5_reference_parameters.txt", 1022, "run 44 / S5 / 第 1 轮（未覆盖形态）"),
)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        cursor = connection.cursor()
        for name, trace_id, source in SAMPLES:
            cursor.execute(
                "select run_id, round_index, agent, outcome, response_text "
                "from ai_analysis_trace where id=?",
                (trace_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise SystemExit(f"trace id {trace_id} 不在了（{source}）")
            run_id, round_index, agent, outcome, text = row
            if not text:
                raise SystemExit(f"trace id {trace_id} 的原文是空的（{source}）")
            # **二进制写**：字节原样落盘，不经过任何换行/编码转换。
            with open(os.path.join(OUT, name), "wb") as handle:
                handle.write(text.encode("utf-8"))
            print(
                f"{name}: run={run_id} round={round_index} agent={agent} "
                f"outcome={outcome} chars={len(text)}"
            )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
