# -*- coding: utf-8 -*-
"""驱动**正在运行的那个平台实例**（127.0.0.1:8002）跑一遍全量/增量分析。

为什么走 HTTP 而不是进程内直调：周版本同步任务与 AI 分析任务都是**排进运行中那个进程
的内存队列**的（`_enqueue_pending_row` → `ai_task_queue`），另起一个进程 enqueue 根本
送不到它的 worker（`load_pending_tasks` 只在启动时读一次库）。所以驱动必须从外面打进去。

token 从 `.env` 的 `ADMIN_API_TOKEN` 读，**只进请求头，不落盘、不回显**。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8002"
PROJECT_ID = 2
SRC_REPO = (Path(__file__).resolve().parent / "origin.git").as_posix()


def _token() -> str:
    text = (ROOT / ".env").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^ADMIN_API_TOKEN=(.+)$", text, re.M)
    if not m:
        raise SystemExit("没有 ADMIN_API_TOKEN")
    return m.group(1).strip().strip('"')


def call(method, path, *, form=None, payload=None, timeout=120):
    url = BASE + path
    data, headers = None, {"X-Admin-Token": _token()}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _show(status, body, limit=600):
    print(f"  HTTP {status}")
    print("  " + body[:limit].replace("\n", "\n  "))


# ---------------------------------------------------------------------------

def create_repo():
    # 六个必填：name/url/server_url/token/branch/resource_type
    # （services/repository_creation_handlers.py:155）。resource_type 用 code 是为了
    # 绕开「table 还要求 header_rows」那条；url 必须是**裸库**，理由见 make_fixture。
    # 不传 current_date → start_date 为 NULL → 首次同步走全量采集。
    status, body = call("POST", "/repositories/git", form={
        "project_id": PROJECT_ID,
        "name": "e2e_synctest",
        "category": "e2e",
        "url": SRC_REPO,
        "server_url": SRC_REPO,
        "token": "local",
        "branch": "master",
        "resource_type": "code",
    })
    _show(status, body)


def manual_sync(repo_id):
    status, body = call("POST", f"/repositories/{repo_id}/sync", timeout=600)
    _show(status, body)


def create_config(repo_id):
    status, body = call("POST", f"/projects/{PROJECT_ID}/weekly-version-config/api", payload={
        "name": "e2e 全量/增量验证",
        "description": "本地造的少量 git 测试数据（见 .pytest_tmp/e2e/make_fixture.py）",
        "repository_id": repo_id,
        "branch": "master",
        "start_time": "2026-09-14 00:00:00",
        "end_time": "2026-09-28 23:59:59",
        "cycle_type": "custom",
        "auto_sync": True,
    })
    _show(status, body)


def config_info(config_id):
    status, body = call("GET", f"/weekly-version-config/{config_id}/info")
    _show(status, body)


def analyze(config_id, mode):
    status, body = call("POST", f"/ai-analysis/weekly/{config_id}/jobs",
                        payload={"analysis_mode": mode, "focus": "all"})
    _show(status, body)


def set_window(config_id, start, end):
    """把这条配置的窗口改成**只有它自己用**的一段。

    `build_weekly_payload` 取的是「同项目 + 同 start/end」的全部配置，所以只要还有
    别的配置共用一个窗口，输入集就是那几个仓库的并集 —— 造数时这一点最容易踩。
    """
    status, body = call("PUT", f"/projects/{PROJECT_ID}/weekly-version-config/api/{config_id}",
                        payload={"start_time": start, "end_time": end})
    _show(status, body)


def set_budget(value):
    status, body = call("POST", f"/ai-analysis/projects/{PROJECT_ID}/config",
                        payload={"prompt_char_budget": int(value)})
    _show(status, body)


def restore_config():
    snap = json.loads((Path(__file__).resolve().parent / "ai_config_snapshot.json")
                      .read_text(encoding="utf-8"))
    status, body = call("POST", f"/ai-analysis/projects/{PROJECT_ID}/config", payload=snap)
    _show(status, body)


def list_configs():
    status, body = call("GET", f"/projects/{PROJECT_ID}/weekly-version-config/api")
    _show(status, body, 2000)


if __name__ == "__main__":
    # 用法：py drive.py create-repo | list | info <id> | sync <repo_id>
    #       | config <repo_id> | full <config_id> | incr <config_id>
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "create-repo":
        create_repo()
    elif cmd == "list":
        list_configs()
    elif cmd == "info":
        config_info(int(rest[0]))
    elif cmd == "sync":
        manual_sync(int(rest[0]))
    elif cmd == "config":
        create_config(int(rest[0]))
    elif cmd == "full":
        analyze(int(rest[0]), "full")
    elif cmd == "incr":
        analyze(int(rest[0]), "incremental")
    elif cmd == "window":
        set_window(int(rest[0]), rest[1], rest[2])
    elif cmd == "budget":
        set_budget(rest[0])
    elif cmd == "restore-config":
        restore_config()
    else:
        raise SystemExit(f"未知命令 {cmd}")
