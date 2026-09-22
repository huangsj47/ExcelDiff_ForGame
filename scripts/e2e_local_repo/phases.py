# -*- coding: utf-8 -*-
"""第二、三阶段：分片上下文超预算、增量分析。**无人值守**跑完并把过程写进日志。

    py phases.py overbudget     # 隔离窗口 → 降 prompt_char_budget 到下限 → 全量一次
    py phases.py incremental    # 推第二轮提交 → 同步 → 周版本同步 → 增量一次

为什么先隔离窗口：`build_weekly_payload` 取的是「同项目 + 同 start/end」的**全部**配置，
不隔离的话输入集是三个仓库的并集（1117 个文件），既贵又看不出增量给的是哪一段。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DB = "file:" + (ROOT / "instance" / "diff_platform.db").as_posix() + "?mode=ro"

CONFIG_ID = 3
WINDOW = ("2026-09-15 00:00:00", "2026-09-26 23:59:59")   # 只有 config 3 用这一段
#                                                    ^ 必须在**未来**：
# 窗口一过，调度器就把配置置 completed 并 continue（task_worker_service.py:1447），
# 之后再也不产生同步任务 —— 实测 00:06 就踩到了（end 写的 09-22 23:59）。
BUDGET_DEFAULT = 560000
BUDGET_TIGHT = 10000

sys.path.insert(0, str(HERE))
import drive  # noqa: E402


def q(sql, args=()):
    con = sqlite3.connect(DB, uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_job(job_id, deadline=3600):
    """等一条 job 走到终态，期间每 60 秒报一次进度（progress_json 是唯一的过程视图）。"""
    end = time.time() + deadline
    last = ""
    while time.time() < end:
        row = q("select state, run_id from ai_analysis_job where id=?", (job_id,))[0]
        state, run_id = row
        if state not in ("running", "queued", "waiting_snapshot"):
            log(f"job {job_id} 终态: {state} run={run_id}")
            return run_id
        snap = q("select progress_json from ai_analysis_job where id=?", (job_id,))[0][0]
        if snap:
            try:
                d = json.loads(snap)
                cur = (f"agent={d.get('agent')} {d.get('agent_index')}/{d.get('agent_total')}"
                       f" round={d.get('round')} req={d.get('requests_used')}"
                       f" ctx={d.get('items_chars')} tokens={d.get('job_tokens')}")
                if cur != last:
                    log(f"  job {job_id} 进度: {cur}")
                    last = cur
            except Exception:
                pass
        time.sleep(30)
    log(f"job {job_id} 超时未结束")
    return None


def max_weekly_sync_id():
    """触发**之前**先记下水位。PUT 请求自己就会入队一条 weekly_sync，取晚了
    （在触发之后取）就会把那条算成「已有的」，然后等一个永远不会来的 id。"""
    return q("select coalesce(max(id),0) from background_tasks where task_type='weekly_sync'")[0][0]


def wait_weekly_sync(before, deadline=150):
    """等一次新的 weekly_sync 跑完。

    **窗口没变时 PUT 根本不会入队**（更新分支只在 `time_changed` 里调
    `_create_weekly_sync_task`，`weekly_version_logic.py:502-509`），所以这里等不到就
    直接过去 —— 真正的判据是下面那张缓存表，不是任务有没有新增。等太久只是白等。
    """
    end = time.time() + deadline
    while time.time() < end:
        rows = q("select id, status from background_tasks where task_type='weekly_sync' and id>?",
                 (before,))
        if rows and all(r[1] in ("completed", "failed") for r in rows):
            log(f"weekly_sync 结束: {rows}")
            return rows
        time.sleep(10)
    log("本轮没有新的 weekly_sync 任务（窗口没变就不会入队），直接按现有缓存继续")
    return []


def trigger_full_or_incr(mode):
    status, body = drive.call("POST", f"/ai-analysis/weekly/{CONFIG_ID}/jobs",
                              payload={"analysis_mode": mode, "focus": "all"})
    job = json.loads(body)
    return job["job_id"]


def isolate_window():
    log(f"隔离窗口 → {WINDOW}")
    # PUT 更新分支自己会 _create_weekly_sync_task（weekly_version_logic.py:509），
    # 所以这一步同时也是「周版本同步」的触发器。
    before = max_weekly_sync_id()
    status, body = drive.call("PUT", f"/projects/{drive.PROJECT_ID}/weekly-version-config/api/{CONFIG_ID}",
                              payload={"start_time": WINDOW[0], "end_time": WINDOW[1]})
    log(f"  PUT {status} {body[:160]}")
    wait_weekly_sync(before)
    for r in q("select file_path, latest_commit_id, commit_count, cache_status,"
               " length(merged_diff_data) from weekly_version_diff_cache where config_id=?",
               (CONFIG_ID,)):
        log(f"  缓存: {r}")


def set_budget(value):
    status, body = drive.call("POST", f"/ai-analysis/projects/{drive.PROJECT_ID}/config",
                              payload={"prompt_char_budget": int(value)})
    log(f"  prompt_char_budget={value} → HTTP {status} {body[:120]}")
    assert status == 200 and json.loads(body).get("success"), body


def stage_overbudget():
    isolate_window()
    try:
        set_budget(BUDGET_TIGHT)
        job = trigger_full_or_incr("full")
        log(f"已触发超预算全量分析 job={job}")
        run = wait_job(job)
    finally:
        set_budget(BUDGET_DEFAULT)
    if run:
        subprocess.run([sys.executable, str(HERE / "report.py"), str(run)])


def wait_weekly_cache_moved(before_tips, deadline=1200):
    """等周版本缓存被重算到新版本。

    **不能靠 PUT 触发**：更新分支只在 `time_changed` 里建同步任务，而同值 PUT 不算变化；
    改窗口又会 `delete()` 掉全部缓存并换掉 group_key（基线跟着没了，增量会退化成首跑）。
    所以这里等的是**调度器**那条（`schedule_weekly_sync_tasks`，每 15 分钟一轮）。
    """
    end = time.time() + deadline
    while time.time() < end:
        now = dict(q("select file_path, latest_commit_id from weekly_version_diff_cache"
                     " where config_id=?", (CONFIG_ID,)))
        moved = {k: v for k, v in now.items() if before_tips.get(k) != v}
        if moved:
            log(f"周版本缓存已推进: {moved}")
            return moved
        time.sleep(20)
    log("⚠️ 超时：周版本缓存没有推进")
    return {}


def move_window_into_the_future():
    """把窗口整体推到未来并重建缓存。

    改 start/end 会让更新分支 `delete()` 掉全部缓存并重建（weekly_version_logic.py:502），
    同时也换掉了 group_key —— 这个组的基线要重新建立，所以下一步那次分析必然是首跑全量。
    """
    before = max_weekly_sync_id()
    status, body = drive.call("PUT", f"/projects/{drive.PROJECT_ID}/weekly-version-config/api/{CONFIG_ID}",
                              payload={"start_time": WINDOW[0], "end_time": WINDOW[1]})
    log(f"PUT 窗口 {WINDOW} → HTTP {status} {body[:120]}")
    wait_weekly_sync(before)
    for r in q("select file_path, latest_commit_id, commit_count from weekly_version_diff_cache"
               " where config_id=?", (CONFIG_ID,)):
        log(f"  缓存: {r[0]} {r[1][:8]} commits={r[2]}")


def analyze_and_report(mode, label):
    job = trigger_full_or_incr(mode)
    log(f"已触发{label} job={job}")
    run = wait_job(job)
    if run:
        subprocess.run([sys.executable, str(HERE / "report.py"), str(run)])
    return run


def stage_incremental():
    log("=== 第一步：窗口推到未来，用**已含第二轮提交**的缓存建立基线 ===")
    move_window_into_the_future()
    analyze_and_report("incremental", "基线（预期 first_run → 全量）")

    log("=== 第二步：只改一个文件（铁剑价格），再看增量做差给了什么 ===")
    print(subprocess.run([sys.executable, str(HERE / "make_fixture.py"), "round3"],
                         capture_output=True, text=True).stdout)
    tip = subprocess.run(["git", "rev-parse", "master"], cwd=str(HERE / "gitsrc"),
                         capture_output=True, text=True).stdout.strip()
    log(f"本地 tip={tip[:8]}，等后台采进来…")
    end = time.time() + 900
    while time.time() < end:
        if q("select last_synced_tip from repository where id=3")[0][0] == tip:
            log(f"已同步: {tip[:8]}")
            break
        time.sleep(20)
    else:
        log("⚠️ 超时未同步")

    before = dict(q("select file_path, latest_commit_id from weekly_version_diff_cache"
                    " where config_id=?", (CONFIG_ID,)))
    log("等周版本缓存推进（调度器每 15 分钟一轮）…")
    wait_weekly_cache_moved(before)
    analyze_and_report("incremental", "增量")


if __name__ == "__main__":
    {"overbudget": stage_overbudget, "incremental": stage_incremental}[sys.argv[1]]()
