"""任务优先级的**单一来源**，以及「等太久就提一档」的 aging。

## 语义（一句话，别猜）

**数值越小越优先。** `TaskWrapper.__lt__`（`services/task_worker_service.py`）就是按
`priority` 比的，`queue.PriorityQueue` 每次取出最小的那一个。本模块是**唯一**定义这些数字
的地方：别处一律 `from services.task_worker_priority import WEEKLY_SYNC`，
不要再写字面量。

## 为什么必须集中（实测出来的后果）

这些数字原先散落在十几个文件里，谁也不知道「把周版本同步调成 3」会越过谁。实测
（2026-09-21 复测）的后果是：

* `weekly_sync=3`、`auto_sync=5`、`weekly_excel_cache=5`、`weekly_ai_analysis=6`
  —— **AI 排在两种同步之后**：同步结束之后又连续跑了 3 个 `auto_sync` 才开始 AI；
* 用户点「重新分析」到模型真正开始，隔了 **13 分 53 秒**；
* `weekly_ai_analysis` 库里的定时任务**一条都没真正跑过**。

所以这里同时定两件事：**数值本身**，以及**手工 AI 不许被同步饿死**的那条规则。

## 手工 AI 为什么是 2

一次手工分析是**用户点出来的动作**，与「点开某个提交等 diff」同级；它排在
「保持仓库新鲜」的后台同步之后没有任何道理。而它**不会**让同步停摆：手工点击是低频事件，
且同一分组按 `group_key` 去重（`create_weekly_ai_analysis_task`），不会攒出一串。

调度器排的分析仍是 6：它确实比「同步跑完」低一档，靠下面这条 aging 兜底，不必抬价。

## aging：等得越久越优先（有界）

只抬价不改 aging 是不够的 —— 实测那条队列**永不空**（同步每 15 分钟补一条、一条要跑
4~5 分钟，队列里永远有优先级 3 可取），于是低优先级任务无限「排队中」。aging 让**老**任务
压过**新**任务，队列才会被消化而不是被不断插队。

两条硬边界，缺一不可：

* **有界**（`AGING_MAX_STEPS`）：最多提 3 档、15 分钟到顶。没有上限的话，
  一条挂了两小时的 `cleanup_cache`（20）会爬到用户动作前面去；
* **有底**（`AGING_FLOOR` = `WEEKLY_SYNC`）：aging 只用来**打平**同步，不许越过它。
  越过它就变成「有一批积压 → 同步永远排不上 → 同步停摆」，而同步停摆是比饿死更难查的
  故障（连日志都写着「正常让路」）。

aging 的三个入口（都在本模块，作用于**执行路径**而不是数据库行）：

1. `reweigh_queue()`：worker 每轮取任务前按真实等待时长重算整个堆 —— 这是 aging 唯一
   真正生效的地方（只在入队时算一次等于没算：那时等待时长恒为 0）；
2. `backdate_wrapper()`：从库里恢复任务（`load_pending_tasks`）时把「入队时刻」往前拨，
   让重启后捞回来的老任务按它真实的年龄参与 aging；
3. `retune_queued_task()`：去重命中、把手上升级成手工时，把**已经在队列里**的那一份也改掉
   —— 否则库里的 `priority` 变了、内存里那份还是旧的，日志与行为对不上。
"""

from __future__ import annotations

import heapq
import time

# ---------------------------------------------------------------------------
#  数值（越小越优先）
# ---------------------------------------------------------------------------

# 页面驱动的 excel_diff：用户正等着看，必须最先跑。
EXCEL_DIFF_PAGE = 1

# 用户点出来的 AI 分析（含「等同步跑完就自动开始」转交的那一次）。
WEEKLY_AI_MANUAL = 2

# Agent 派发的 commit_diff / 强制重试的 auto_sync。
AGENT_COMMIT_DIFF = 2
AUTO_SYNC_FORCE_RETRY = 2

# 周版本同步 / 普通 commit_diff。
WEEKLY_SYNC = 3
COMMIT_DIFF = 3

# 给 AI 打工的正文读取与引用检索（Agent 侧）。
AGENT_FILE_CONTENT = 4

# 后台自动同步 / 周版本 Excel 缓存重建。
AUTO_SYNC = 5
WEEKLY_EXCEL_CACHE = 5

# 调度器排的 AI 分析。
WEEKLY_AI_ANALYSIS = 6

# 「等同步跑完就自动开始」的意图行。**它不执行**（`load_pending_tasks` 明确跳过），
# 数值只影响它在任务列表里排得多靠前，不参与队列裁决。
WAITING_INTENT = 6

# 分支刷新 / excel_diff 的默认值 / 维护重建 / 每日清理。
BRANCH_REFRESH = 8
EXCEL_DIFF_DEFAULT = 10
MAINTENANCE_REBUILD = 15
CLEANUP_CACHE = 20

# 数据库里没写 priority（老行 / 外部写入）时的兜底。
DEFAULT_TASK_PRIORITY = EXCEL_DIFF_DEFAULT

# ---------------------------------------------------------------------------
#  aging
# ---------------------------------------------------------------------------

# 每等这么久提一档。
AGING_STEP_SECONDS = 300

# 最多提几档（15 分钟到顶）。
AGING_MAX_STEPS = 3

# 提价的下限：与 `WEEKLY_SYNC` 同级，靠 `counter`（先入先出）压过新来的同步。
# **不许再低**：低于它，aging 就会把同步饿死（见模块 docstring）。
AGING_FLOOR = WEEKLY_SYNC


def aging_steps(waited_seconds):
    """等了这么久该提几档（纯函数，有界）。"""
    try:
        waited = float(waited_seconds or 0.0)
    except (TypeError, ValueError):
        return 0
    if waited <= 0:
        return 0
    return min(int(waited // AGING_STEP_SECONDS), AGING_MAX_STEPS)


def effective_priority(base_priority, waited_seconds):
    """基础优先级 + 等待时长 → 现在真正用来排队的那一个数。

    `base_priority` 已经不低于下限时原样返回（提价只向上，不会把页面驱动的 excel_diff
    从 1 **降**到 3）。
    """
    try:
        base = int(base_priority)
    except (TypeError, ValueError):
        base = DEFAULT_TASK_PRIORITY
    if base <= AGING_FLOOR:
        return base
    return max(AGING_FLOOR, base - aging_steps(waited_seconds))


def backdate_wrapper(wrapper, waited_seconds):
    """把一个 `TaskWrapper` 的「入队时刻」往前拨 `waited_seconds`。

    从库里恢复任务时用：那些行可能已经等了几十分钟，按 `time.time()` 起算就等于
    刚入队（aging 恒为 0）。
    """
    try:
        wrapper.enqueued_at = time.time() - max(float(waited_seconds or 0.0), 0.0)
    except (AttributeError, TypeError, ValueError):
        # 假队列（测试里的记录器）拿到的是普通 dict —— 拨不动就算了，
        # 这是记账用的字段，不该把入队流程打断。
        pass
    return wrapper


def _queue_items(queue_obj):
    """队列内部的堆列表；拿不到就返回 None（假队列没有堆）。"""
    heap = getattr(queue_obj, "queue", None)
    return heap if isinstance(heap, list) else None


def reweigh_queue(queue_obj, *, now=None):
    """按等待时长重算队列里每个 `TaskWrapper` 的优先级，返回改动条数。

    **必须在 worker 取任务之前调用**：只在入队那一刻算 aging 等于没算（那时等待时长为 0），
    而本缺陷的形态恰恰是「任务在队列里躺着不动」。

    持有 `mutex` 再改，与 `PriorityQueue.put/get` 同一把锁，不会与入队/出队交错。
    改完 `heapify`：堆里键值变了之后不重建就没有堆序，取出来的可能不是最小的那个。
    """
    mutex = getattr(queue_obj, "mutex", None)
    if mutex is None:
        return 0
    current = time.time() if now is None else now
    with mutex:
        heap = _queue_items(queue_obj)
        if not heap:
            return 0
        changed = 0
        for item in heap:
            enqueued_at = getattr(item, "enqueued_at", None)
            if enqueued_at is None:
                continue
            base = getattr(item, "base_priority", getattr(item, "priority", None))
            target = effective_priority(base, current - enqueued_at)
            if target != getattr(item, "priority", None):
                item.priority = target
                changed += 1
        if changed:
            heapq.heapify(heap)
        return changed


def _same_task_id(left, right):
    """`task_data` 里的 task_id 可能是 int 也可能是 str（Agent 回传路径）。"""
    if left is None or right is None:
        return False
    return str(left) == str(right)


def retune_queued_task(queue_obj, task_id, *, priority=None, patch=None):
    """把队列里那条 `task_id` 的任务就地改掉：优先级 / 载荷里的字段。

    返回改到的条数。0 = 它不在内存队列里（已经出队 / 在别的进程里 / 还没入队）——
    调用方据此决定要不要去补一次入队。**不在这里补**：本函数只认「已经在队列里的那一份」。

    `patch` 用来同步载荷里那几个「只在入队那一刻放进去了」的字段（`trigger_source` /
    `requested_mode`）。去重命中一条更早创建的任务时，库里那一行会被升级成 manual，而内存
    队列里那一份还带着旧的 `scheduled` —— 它随载荷走到执行侧（Agent 模式还会原样回传），
    库里写着「手动」、跑出来的账却是「定时」，同一条链子两处说法。
    """
    mutex = getattr(queue_obj, "mutex", None)
    if mutex is None or task_id is None:
        return 0
    with mutex:
        heap = _queue_items(queue_obj)
        if not heap:
            return 0
        changed = 0
        priority_changed = False
        for item in heap:
            task_data = getattr(item, "task_data", None)
            if not isinstance(task_data, dict):
                continue
            if not _same_task_id(task_data.get("task_id"), task_id):
                continue
            touched = False
            if priority is not None and getattr(item, "base_priority", None) != priority:
                item.base_priority = priority
                item.priority = priority
                priority_changed = True
                touched = True
            for key, value in dict(patch or {}).items():
                if value is None or task_data.get(key) == value:
                    continue
                task_data[key] = value
                touched = True
            if touched:
                changed += 1
        if priority_changed:
            # 只改载荷里的字段不会动堆序（比较键没变）；改了优先级就必须重建。
            heapq.heapify(heap)
        return changed
