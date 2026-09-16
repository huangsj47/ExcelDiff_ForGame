"""独立于执行线程的任务租约心跳，不依赖平台运行环境。"""
from threading import Event, Thread

try:
    from .http_client import post_json
except ImportError:
    from http_client import post_json


def renew_task_lease(settings, task, agent_token, headers):
    return post_json(
        f"{settings.platform_base_url}/api/agents/heartbeat",
        {"agent_code": settings.agent_code, "agent_token": agent_token,
         "task_id": task["id"], "attempt": task.get("attempt", 0), "status": "online",
         "lease_seconds": max(120, min(600, settings.heartbeat_interval_seconds * 3))},
        headers=headers, timeout=10,
    )


class TaskHeartbeat:
    def __init__(self, settings, task, agent_token, headers):
        self.settings, self.task = settings, task
        self.agent_token, self.headers = agent_token, headers
        self.stop = Event()
        self.thread = Thread(target=self._run, name="agent-task-heartbeat", daemon=True)

    def _run(self):
        interval = min(30, max(0.01, self.settings.heartbeat_interval_seconds))
        while not self.stop.wait(interval):
            try:
                status, _ = renew_task_lease(self.settings, self.task, self.agent_token, self.headers)
                if status in (401, 403, 404, 409):
                    return
            except (OSError, ValueError, RuntimeError):
                # 短暂断网不终止续租；最终回传仍由平台批次栅栏裁决。
                continue

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=11)
