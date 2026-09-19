"""正在跑的分析的**进度快照**：进程内的一张表，给界面轮询用。

## 为什么需要它（以及为什么不是 SSE）

分析入口是「一个生成器里跑一次阻塞的分析调用」：`stream_commit_analysis` 在
`_execute_analysis` 返回之前**一个事件都 yield 不出去**（见 services/ai_analysis_service.py）。
所以多轮分析跑几分钟的过程中，界面只能显示「分析中」，既看不到第几轮，也看不到
「这次已经花了多少」。要改成边跑边推，就得把引擎挪到后台线程 + 队列 —— 那会带来
Flask 上下文与数据库会话的跨线程问题，代价远大于收益。

所以选的是**轮询**：引擎每跑完一轮把累计用量写进这里的快照（`on_round` 回调，
见 `services/ai/engine.py` 的 `RoundProgress`），界面每隔几秒读一次。

## 三条纪律

1. **这是显示用的缓存，不是账。** 落库的账在 `ai_analysis_run` / `ai_analysis_trace`，
   权威口径是 `services/ai/usage.py`。这里只回答「现在跑到哪了、已经花了多少」，
   而且**允许查不到**（进程重启、多进程部署、跑在别的 worker 里）—— 查不到时返回
   `None`，界面显示「进度不可用」，**不显示 0**。
2. **跑完就清。** 快照的存在时间不该超过一次分析：`clear()` 在 `_execute_analysis`
   的出口（含异常路径）调用；另外每条快照带 `updated_at`，超过 `MAX_AGE_SECONDS`
   没有更新就视为过期（进程里留下的残影不该被当成「正在跑」）。
3. **不能因为进度而出错。** 写快照失败、读快照失败一律吞掉：它是给界面看的一眼，
   而它服务的是一条要花钱的分析路径 —— 为了显示进度把分析弄挂，是本末倒置。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from utils.logger import log_print

# 超过这么久没有更新就当作过期（分析已经不在跑了，或者上一轮卡住了）。
#
# 取 120 秒：单次请求的超时可配（默认 300 秒），所以一轮跑到一半时快照可能是「几分钟前」
# 更新的，那**仍然是在跑**。过期的判据不是「多久没更新」而是「界面还该不该显示它在跑」——
# 而这件事真正的答案是 `_execute_analysis` 出口的 `clear()`；这个时限只兜住
# 「进程里留下了残影」这一种情况（例如线程被强杀）。
MAX_AGE_SECONDS = 900

# 快照数量上限。一个进程同时跑的分析不会多（每周/每次提交各一条，且都有闸门），
# 这个上限只是防呆：万一清理路径出问题，也不该无限增长。
MAX_ENTRIES = 200


@dataclass(frozen=True)
class ProgressSnapshot:
    """一眼进度。字段与 `RoundProgress` 同名同义（累计值、未上报是 `None`）。"""

    run_id: int
    project_id: int
    index: int
    max_rounds: int
    status: str
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: Optional[int]
    cache_write_tokens: Optional[int]
    requests_used: int
    requests_remaining: int
    items_chars: int
    elapsed_ms: int
    updated_at: float
    # 子代理模式（services/ai/subagent.py）：这一轮是哪个分片在跑、它是第几个/共几个。
    # `agent` 为空且 `agent_index > 0` = 那是**汇总**那一次（它就是这个分析的主代理）。
    # 三个都排在最后且带默认值：没开子代理时，构造一个快照与这一层之前**完全一样**
    # （`updated_at` 保持必填 —— 给它一个默认值等于埋一个「忘了传就是永远过期」的坑）。
    agent: str = ""
    agent_index: int = 0
    agent_total: int = 0

    @property
    def live_tokens(self) -> int:
        """本次运行**已消耗**的 token（输入 + 输出）。

        它是**下界**（只含上游已上报的部分），与预算那一档的口径一致 —— 拿它去和上限比
        时，「下界已经超了」是确定的结论，「下界没超」不能反过来说没超。文案里必须写清。
        """
        return int(self.prompt_tokens or 0) + int(self.completion_tokens or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "round": self.index,
            "max_rounds": self.max_rounds,
            "status": self.status,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "requests_used": self.requests_used,
            "requests_remaining": self.requests_remaining,
            "items_chars": self.items_chars,
            "elapsed_ms": self.elapsed_ms,
            "agent": self.agent,
            "agent_index": self.agent_index,
            "agent_total": self.agent_total,
            "live_tokens": self.live_tokens,
            "age_seconds": max(0, int(time.monotonic() - self.updated_at)),
        }


_lock = threading.Lock()
_snapshots: dict[int, ProgressSnapshot] = {}


def _prune_locked(now: float) -> None:
    """清掉过期的与超量的（调用方必须已持锁）。"""
    stale = [
        run_id
        for run_id, item in _snapshots.items()
        if now - item.updated_at > MAX_AGE_SECONDS
    ]
    for run_id in stale:
        _snapshots.pop(run_id, None)
    if len(_snapshots) > MAX_ENTRIES:
        ordered = sorted(_snapshots.items(), key=lambda pair: pair[1].updated_at)
        for run_id, _item in ordered[: len(_snapshots) - MAX_ENTRIES]:
            _snapshots.pop(run_id, None)


def publish(run_id: int, project_id: int, progress: Any) -> None:
    """把一轮的进度写进快照。**任何异常都吞掉**（见模块 docstring 第 3 条）。"""
    try:
        snapshot = ProgressSnapshot(
            run_id=int(run_id),
            project_id=int(project_id),
            index=int(getattr(progress, "index", 0) or 0),
            max_rounds=int(getattr(progress, "max_rounds", 0) or 0),
            status=str(getattr(progress, "status", "") or ""),
            prompt_tokens=int(getattr(progress, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(progress, "completion_tokens", 0) or 0),
            cache_read_tokens=getattr(progress, "cache_read_tokens", None),
            cache_write_tokens=getattr(progress, "cache_write_tokens", None),
            requests_used=int(getattr(progress, "requests_used", 0) or 0),
            requests_remaining=int(getattr(progress, "requests_remaining", 0) or 0),
            items_chars=int(getattr(progress, "items_chars", 0) or 0),
            elapsed_ms=int(getattr(progress, "elapsed_ms", 0) or 0),
            # 与其它字段同样用 `getattr`：这一层要能容忍任何「长得像进度」的对象
            # （老调用方、测试里的替身），缺字段就是没有分片信息，不是错误。
            agent=str(getattr(progress, "agent", "") or ""),
            agent_index=int(getattr(progress, "agent_index", 0) or 0),
            agent_total=int(getattr(progress, "agent_total", 0) or 0),
            updated_at=time.monotonic(),
        )
        now = time.monotonic()
        with _lock:
            _snapshots[snapshot.run_id] = snapshot
            _prune_locked(now)
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ 写分析进度快照失败（不影响分析）: run={run_id} {exc}", "AI", force=True)


def snapshot(run_id: int) -> Optional[ProgressSnapshot]:
    """读一眼进度。读不到（没在跑 / 已过期 / 别的进程在跑）返回 `None`，**不是 0**。"""
    try:
        now = time.monotonic()
        with _lock:
            _prune_locked(now)
            item = _snapshots.get(int(run_id))
        if item is None:
            return None
        if now - item.updated_at > MAX_AGE_SECONDS:
            return None
        return item
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ 读分析进度快照失败: run={run_id} {exc}", "AI", force=True)
        return None


def clear(run_id: int) -> None:
    """分析结束（成功或失败）时清掉。**幂等**，清不存在的键不报错。"""
    try:
        with _lock:
            _snapshots.pop(int(run_id), None)
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ 清分析进度快照失败（不影响分析）: run={run_id} {exc}", "AI", force=True)


def reset_for_tests() -> None:
    """测试用：清空整张表（测试库是会话级共用的，跨用例的残影会让断言飘）。"""
    with _lock:
        _snapshots.clear()
