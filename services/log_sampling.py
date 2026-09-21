#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热路径日志采样与汇总 —— 把「逐文件一行」压成「一段一条 + 一行汇总」。

## 为什么有这个模块

复测文档 7.1 实测：一次同步的日志写到 3,826 行时，其中
**1,003 + 1,005 = 2,008 行**只是「检查本地路径 / 路径是否存在」——占全文 52.5%。
这两行在 `services/vcs_content_service.py::get_file_content_from_git` 里
**按文件**打印，条数与仓库文件数成正比；而 `utils.logger.log_print` 每次都要
open/append/close 一次 `logs/runlog.log`，所以它同时也是同步 I/O 和 fsync 抖动。

## 做法是「采样 + 汇总」，不是删日志

`log_sampled(...)` 在同一个 group 上只打**第一条**，中间只累计；作用域结束时补
**最后一条 + 一行汇总**。三件事必须同时成立，缺一条这次改动就是退步：

| 要保住的能力 | 靠什么 |
|---|---|
| 不再刷屏 | 中间样本不打 |
| 「扫了多少文件、多少不存在」 | 汇总行的 `处理 N 次，其中 M 次命中/X 次未命中` |
| 「最后卡在哪个文件」 | 保留的最后一条样本 |
| 「按 job/run/stage 查耗时/失败/截断/重试」 | `log_stage_event(...)` 的结构化字段 + 采样行尾的上下文后缀 |

计数不是副产品，它就是**诊断能力本身**：把日志删干净同样能让「不再刷屏」变绿，
但那样「这次扫了 1003 个文件、其中 3 个不存在」就没人回答得了了。

## 开关

* `LOG_SAMPLE_MODE=aggregate`（默认）：首条 + 末条 + 汇总；
* `LOG_SAMPLE_MODE=all`：逐条全打（排查用），**汇总照旧**；
* `LOG_SAMPLE_MODE=off`：只留汇总；
* `LOG_SAMPLE_FLUSH_EVERY=<int>`（默认 200）：没有显式作用域时，每 N 条记录
  自动打一次汇总。周版本同步那条链路上没有人会去开作用域，没有这个下限，
  计数会一直憋在内存里直到进程结束——那就等于把日志删了。

汇总行固定走 `HOT` 类目（默认开，`LOG_HOT=false` 可关），首条/末条明细仍走它原来
那个类目（`GIT`/`SVN`/`DIFF`...，`LOG_GIT=false` 照旧关得掉）。这样默认配置下每个
group 只剩「一条明细 + 一行汇总」，而关掉某个业务类目时**不会**把「扫了多少」那行
一起关掉——那行现在是答案本身。
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional, Tuple

from utils.logger import log_print, log_structured_event

# ---------------------------------------------------------------------------
#  环境开关
# ---------------------------------------------------------------------------

LOG_SAMPLE_MODE_ENV = 'LOG_SAMPLE_MODE'
LOG_SAMPLE_FLUSH_EVERY_ENV = 'LOG_SAMPLE_FLUSH_EVERY'

MODE_AGGREGATE = 'aggregate'
MODE_ALL = 'all'
MODE_OFF = 'off'
_MODES = (MODE_AGGREGATE, MODE_ALL, MODE_OFF)

DEFAULT_FLUSH_EVERY = 200

# 命中 / 未命中 —— 汇总行里那两个数字的口径。没有这两语义的类目就只报总数。
OUTCOME_HIT = 'hit'
OUTCOME_MISS = 'miss'
OUTCOME_HIT_LABEL = '命中'
OUTCOME_MISS_LABEL = '未命中'
# 另外两种有明确中文说法的结局。再往下的自定义结局按原样打（`3 次timeout` 这种
# 生造标签比一个诚实的英文词更糟，所以**只**给这几个配标签）。
OUTCOME_FAIL = 'fail'
OUTCOME_TIMEOUT = 'timeout'
OUTCOME_FAIL_LABEL = '失败'
OUTCOME_TIMEOUT_LABEL = '超时'
_LABELLED_OUTCOMES = (
    (OUTCOME_HIT, OUTCOME_HIT_LABEL),
    (OUTCOME_MISS, OUTCOME_MISS_LABEL),
    (OUTCOME_FAIL, OUTCOME_FAIL_LABEL),
    (OUTCOME_TIMEOUT, OUTCOME_TIMEOUT_LABEL),
)
_KNOWN_OUTCOMES = frozenset(outcome for outcome, _label in _LABELLED_OUTCOMES)

# 汇总行默认类目：默认开（LOG_ALL=false 或 LOG_HOT=false 可关）。
SUMMARY_LOG_TYPE = 'HOT'
# 明细（首条/末条）默认类目：默认关。
DETAIL_LOG_TYPE = 'DETAIL'

# 会写进采样行后缀与结构化事件的诊断字段，顺序即展示顺序。
DIAGNOSTIC_FIELDS: Tuple[str, ...] = ('job_id', 'run_id', 'stage', 'task_id')


def _current_mode() -> str:
    raw = (os.environ.get(LOG_SAMPLE_MODE_ENV) or '').strip().lower()
    return raw if raw in _MODES else MODE_AGGREGATE


def _flush_every_default() -> int:
    raw = (os.environ.get(LOG_SAMPLE_FLUSH_EVERY_ENV) or '').strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FLUSH_EVERY
    return value if value > 0 else DEFAULT_FLUSH_EVERY


# ---------------------------------------------------------------------------
#  诊断上下文（job_id / run_id / stage / task_id）
# ---------------------------------------------------------------------------

_context_state = threading.local()


def _context_stack() -> List[Dict[str, object]]:
    stack = getattr(_context_state, 'stack', None)
    if stack is None:
        stack = []
        _context_state.stack = stack
    return stack


def current_diagnostics() -> Dict[str, object]:
    """当前的诊断字段（没有绑定时为空 dict）。"""
    stack = _context_stack()
    return dict(stack[-1]) if stack else {}


@contextmanager
def bind_diagnostics(**fields):
    """在 `with` 块内给日志行与结构化事件挂上 `job_id/run_id/stage/...`。

    任务 A 把 job 协议落地后，由 job/task 的入口处调用一次；在那之前，
    `stage` 由本模块的调用点自己给（例如 `repo_sync` / `weekly_diff`）。

    注意：作用域是**线程本地**的，新起的 worker 线程不会继承——需要的地方要
    自己再 `bind_diagnostics` 一次。这是刻意的：隐式跨线程继承会让「这条日志
    属于哪个 job」变成一个猜谜游戏。
    """
    stack = _context_stack()
    merged = dict(stack[-1]) if stack else {}
    merged.update({key: value for key, value in fields.items() if value is not None})
    stack.append(merged)
    try:
        yield merged
    finally:
        if stack and stack[-1] is merged:
            stack.pop()
        else:  # pragma: no cover - 只可能在乱序退出时发生
            try:
                stack.remove(merged)
            except ValueError:
                pass


def _context_suffix() -> str:
    diagnostics = current_diagnostics()
    parts = [
        f'{key}={diagnostics[key]}'
        for key in DIAGNOSTIC_FIELDS
        if diagnostics.get(key) not in (None, '')
    ]
    return f" [{' '.join(parts)}]" if parts else ''


def log_stage_event(event: str, log_type: str = 'INFO', force: bool = False, **fields) -> None:
    """打一行结构化诊断事件，自动并入当前的 `job_id/run_id/stage/task_id`。

    「按 job_id/run_id/stage 查到耗时、失败、截断和重试」靠的就是这些字段：
    调用点把**事实**（`duration_ms` / `failed` / `truncated` / `retry_count`）交进来，
    上下文由这里补齐，调用点不必自己拼一遍。
    """
    payload = current_diagnostics()
    payload.update(fields)
    log_structured_event(event, log_type=log_type, force=force, **payload)


# ---------------------------------------------------------------------------
#  采样器
# ---------------------------------------------------------------------------

class _Bucket:
    """一个 group 的累计：首条、末条、总次数、各结局的次数。"""

    __slots__ = ('key', 'label', 'total', 'outcomes', 'first_message',
                 'last_message', 'log_type', 'detail_type', 'summary_type',
                 'reported_total')

    def __init__(self, key: str, label: str, log_type: str) -> None:
        self.key = key
        self.label = label
        self.total = 0
        self.outcomes: 'OrderedDict[str, int]' = OrderedDict()
        self.first_message: Optional[str] = None
        self.last_message: Optional[str] = None
        self.log_type = log_type
        # 明细保留原类目（`GIT`/`SVN`/`DIFF`...），汇总固定走 HOT：
        # 这样 `LOG_GIT=false` 仍然能关掉明细，而「扫了多少」那行不会被一起关掉。
        self.detail_type = log_type
        self.summary_type = SUMMARY_LOG_TYPE
        # 滚动汇总用：上一次打到日志里的总数（避免总数没变还重复打）。
        self.reported_total = 0

    def summary_line(self, *, partial: bool) -> str:
        parts = [f'{self.label}: 处理 {self.total} 次']
        counts = []
        for outcome, label in _LABELLED_OUTCOMES:
            if outcome in self.outcomes:
                counts.append(f'{self.outcomes[outcome]} 次{label}')
        # 其它结局按原样列出，不硬塞进「命中/未命中」。
        for outcome, count in self.outcomes.items():
            if outcome in _KNOWN_OUTCOMES:
                continue
            counts.append(f'{count} 次{outcome}')
        if counts:
            parts.append(f"其中 {'/'.join(counts)}")
        line = '，'.join(parts)
        return f'{line}（累计）' if partial else line


class LogSampler:
    """一次请求 / 一次任务的采样作用域。

    **可以跨线程共享**（`ThreadedGitService` 的 worker 把逐文件的记录交回父线程建的
    那个采样器，见那边的注释）：`record`/`flush` 都在锁里。这不是为了性能 —— 是为了
    「扫了多少」这个数字必须来自**一份**账，而不是 6 个 worker 各记一份。
    """

    def __init__(self, name: str = 'request', flush_every: Optional[int] = None) -> None:
        self.name = name
        self.flush_every = int(flush_every) if flush_every else _flush_every_default()
        self._buckets: 'OrderedDict[str, _Bucket]' = OrderedDict()
        self._pending = 0
        # 作用域内已经打过「首条」的 group —— 滚动汇总会把 bucket 清掉重建，
        # 没有这个集合，每重建一次就会再打一遍首条，白涨行数。
        self._started: set = set()
        # RLock 而不是 Lock：`record` 持锁时会触发滚动 `flush`，同一线程要能再进。
        self._lock = threading.RLock()

    # -- 记录 ---------------------------------------------------------------
    def record(self, key: str, label: str, message: str, *, outcome: Optional[str] = None,
               log_type: str = 'GIT') -> bool:
        """记一条热路径日志。返回它是否**当场**被打了出来。

        * 该 group 在本作用域的第一条：打（否则「从哪个文件开始」就没了）；
        * 中间的：只计数；
        * `LOG_SAMPLE_MODE=all` 时每条都打。
        """
        mode = _current_mode()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(key=key, label=label, log_type=log_type)
                self._buckets[key] = bucket

            bucket.total += 1
            bucket.last_message = message
            if bucket.first_message is None:
                bucket.first_message = message
            if outcome is not None:
                bucket.outcomes[outcome] = bucket.outcomes.get(outcome, 0) + 1

            emitted = False
            if mode == MODE_ALL or (mode == MODE_AGGREGATE and key not in self._started):
                self._emit(message, bucket.detail_type)
                emitted = True
            self._started.add(key)

            self._pending += 1
            if self.flush_every and self._pending >= self.flush_every:
                self.flush(partial=True)
            return emitted

    # -- 汇总 ---------------------------------------------------------------
    def flush(self, *, partial: bool = False) -> List[str]:
        """打「末条 + 汇总」，然后收口计数。返回打出来的行（供测试与复用）。

        `partial=True` 是**滚动**汇总（没有显式作用域时的自动触发）：只打累计总数、
        不重置计数、不打末条 —— 这样总数只会单调向前，不会「每 200 条从零开始」。
        `partial=False` 是收口汇总：末条 + 最终总数，之后清空。
        """
        emitted: List[str] = []

        with self._lock:
            if partial:
                for bucket in self._buckets.values():
                    if bucket.total == 0 or bucket.total == bucket.reported_total:
                        continue
                    bucket.reported_total = bucket.total
                    emitted.append(self._emit_summary(bucket, partial=True))
                self._pending = 0
                return emitted

            for bucket in list(self._buckets.values()):
                if bucket.total == 0:
                    continue
                # 补一条末条 —— 只在**聚合**模式下有意义：那条是被采样掉、需要补回来的
                # 那条。`all` 模式已经逐条打过了（补了就是重复），`off` 模式本就只要汇总。
                # 只出现一次时明细行也已经把这唯一的事实说完了。
                if (_current_mode() == MODE_AGGREGATE
                        and bucket.total > 1 and bucket.last_message
                        and bucket.last_message != bucket.first_message):
                    line = bucket.last_message
                    self._emit(line, bucket.detail_type)
                    emitted.append(line)
                emitted.append(self._emit_summary(bucket, partial=False))

            self._buckets.clear()
            self._pending = 0
            return emitted

    def _emit_summary(self, bucket: _Bucket, *, partial: bool) -> str:
        line = bucket.summary_line(partial=partial)
        self._emit(line, bucket.summary_type)
        return line

    @staticmethod
    def _emit(message: str, log_type: str) -> None:
        log_print(message + _context_suffix(), log_type)


# ---------------------------------------------------------------------------
#  作用域
# ---------------------------------------------------------------------------

_scope_state = threading.local()


def _scope_stack() -> List[LogSampler]:
    stack = getattr(_scope_state, 'stack', None)
    if stack is None:
        stack = []
        _scope_state.stack = stack
    return stack


def current_sampler() -> LogSampler:
    if _scope_stack():
        return _scope_stack()[-1]
    implicit = getattr(_scope_state, 'implicit', None)
    if implicit is None:
        implicit = LogSampler(name='implicit')
        _scope_state.implicit = implicit
    return implicit


@contextmanager
def log_sampling_scope(name: str = 'request', flush_every: Optional[int] = None):
    """开一个采样作用域，退出时自动汇总。

    周版本同步 / 一次 diff 请求 / 一次分析任务都应当在入口处包一层；
    没包也不会丢计数（见 `current_sampler` 的隐式作用域）。
    """
    sampler = LogSampler(name=name, flush_every=flush_every)
    _scope_stack().append(sampler)
    try:
        yield sampler
    finally:
        stack = _scope_stack()
        if stack and stack[-1] is sampler:
            stack.pop()
        else:  # pragma: no cover - 只可能在乱序退出时发生
            try:
                stack.remove(sampler)
            except ValueError:
                pass
        sampler.flush()


def log_sampled(key: str, label: str, message: str, *, outcome: Optional[str] = None,
                log_type: str = 'GIT') -> bool:
    """热路径写日志的唯一入口。见 `LogSampler.record`。"""
    return current_sampler().record(
        key, label, message, outcome=outcome, log_type=log_type,
    )


def flush_log_sampling() -> List[str]:
    """把当前作用域里pending的计数打出来（作用域结束时也会自动打）。"""
    return current_sampler().flush()


def reset_log_sampling() -> None:
    """清空当前线程的作用域与计数（测试用；生产不需要调用）。"""
    _scope_state.stack = []
    _scope_state.implicit = None
    _context_state.stack = []


def summarize_outcomes(outcomes: Iterable[str]) -> str:
    """把一串结局折成汇总行里的那半句（供需要自建汇总的调用点复用）。"""
    counts: 'OrderedDict[str, int]' = OrderedDict()
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return '/'.join(f'{count} 次{outcome}' for outcome, count in counts.items())
