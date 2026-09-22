# -*- coding: utf-8 -*-
"""读一次运行的结果：分片有没有真跑、模型调用了几次、增量给了什么基准。

用法：py report.py <run_id> [<run_id> ...]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = "file:" + (Path(__file__).resolve().parents[2] / "instance" / "diff_platform.db").as_posix() + "?mode=ro"


def q(sql, args=()):
    con = sqlite3.connect(DB, uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def report(run_id):
    print(f"================ run {run_id} ================")
    row = q("select status, scope, rounds_used, tokens_input, tokens_output, cache_read_tokens,"
            " tool_requests_used, anomalies_found, subagent_mode, subagent_count, duration_ms,"
            " substr(response_text, 1, 300) from ai_analysis_run where id=?", (run_id,))
    if not row:
        print("  没有这条 run")
        return
    (status, scope, rounds, ti, to, cr, tools, anom, smode, scount, dur, text) = row[0]
    deg = q("select coalesce(degradation,''), substr(coalesce(error_message,''),1,200)"
            " from ai_analysis_run where id=?", (run_id,))[0]
    print(f"  status={status} scope={scope} rounds={rounds} tools={tools} anomalies={anom}")
    print(f"  degradation={deg[0]!r} error={deg[1]!r}")
    print(f"  tokens in/out={ti}/{to} cache_read={cr} duration={dur}ms")
    print(f"  subagent_mode={smode} subagent_count={scount}")

    print("  --- trace（每条 = 一轮 = 一次模型调用）---")
    for agent, n, ti_, to_ in q(
        "select coalesce(agent,'(主/汇总)'), count(*), sum(tokens_input), sum(tokens_output)"
        " from ai_analysis_trace where run_id=? group by agent order by 1", (run_id,)
    ):
        print(f"    {agent:>10}: {n} 轮  in={ti_} out={to_}")

    print("  --- 分片 ---")
    payload = q("select response_payload from ai_analysis_run where id=?", (run_id,))[0][0]
    try:
        p = json.loads(payload) if payload else {}
    except Exception as exc:
        print("    payload 解析失败:", exc)
        p = {}
    for s in (p.get("subagents") or []):
        an = s.get("anomalies")
        an = an if isinstance(an, int) else len(an or [])
        rq = s.get("requests")
        rq = rq if isinstance(rq, int) else len(rq or [])
        print(f"    {s.get('agent') or s.get('id')}: status={s.get('status')}"
              f" rounds={s.get('rounds')} requests={rq} anomalies={an}"
              f" skipped={s.get('skipped_reason')!r}")
    if p.get("subagent_skipped"):
        print("    subagent_skipped:", p.get("subagent_skipped"))
    print("    usage:", json.dumps(p.get("usage") or {}, ensure_ascii=False)[:300])

    print("  --- 结论条目 ---")
    for a in q("select severity, confidence, title, file_path from ai_analysis_anomaly"
               " where run_id=? order by id", (run_id,)):
        print("    ", a)

    req = q("select request_payload from ai_analysis_run where id=?", (run_id,))[0][0]
    try:
        rp = json.loads(req) if req else {}
    except Exception:
        rp = {}
    dl = rp.get("delta_files") or []
    print(f"  --- 输入账 --- scope={rp.get('scope')} delta_files={len(dl)}"
          f" manifest_total={(rp.get('manifest') or {}).get('total')}")
    based = [d for d in dl if d.get("diff_base_commit_id")]
    print(f"    带增量基准（diff_base_commit_id）的：{len(based)}/{len(dl)}")
    for d in dl[:6]:
        print("      ", d.get("file_path"), "base=", (d.get("diff_base_commit_id") or "")[:8],
              "latest=", (d.get("latest_commit_id") or "")[:8])
    print("  response_text:", (text or "").replace("\n", " ")[:240])


if __name__ == "__main__":
    for rid in [int(a) for a in sys.argv[1:]]:
        report(rid)
