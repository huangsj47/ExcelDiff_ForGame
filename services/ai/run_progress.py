"""正在跑的分析的**进度快照**：进程内的一张表，给界面轮询用。

## 为什么需要它（以及为什么不是 SSE）

分析入口是「一个生成器里跑一次阻塞的分析调用」：`stream_commit_analysis` 在
`_execute_analysis` 返回之前**一个事件都 yield 不出去**（见 services/ai_analysis_service.py）。
所以多轮分析跑几分钟的过程中，界面只能显示「分析中」，既看不到第几轮，也看不到
「这次已经花了多少」。要改成边跑边推，就得把引擎挪到后台线程 + 队列 —— 那会带来
Flask 上下文与数据库会话的跨线程问题，代价远大于收益。

所以选的是**轮询**：引擎每跑完一轮把累计用量写进这里的快照（`on_round` 回调，
见 `services/ai/engine.py` 的 `RoundProgress`），界面每隔几秒读一次。

## 两个用量口径：job 级累计 与 当前成员局部量

引擎报的累计 token 是**一个引擎实例**的累计，而一个引擎实例 = **一个成员**
（子代理模式下每个分片各起一个，见 `services/ai/subagent.py`）。回调那一层只把
「我是谁」贴到帧上（`report`），token 一个字节都不动；而快照是整条覆盖写的 ——
只报局部量的话，换成员那一瞬间抽屉顶部那个数会**当场回退**（实测 run 13：
S3 收尾约 493,531 → 汇总第一轮 34,960）。

所以这里多记一本账（`_JobLedger`，就在 `publish` 里做，它是每一帧的唯一写入口）：

* `job_tokens`：**跨成员**的本次分析累计，**单调不减**（缺成员的按 0 入账）；
* `live_tokens`：**当前成员**的局部量（它的本意，换成员就从 0 重新开始）；
* `run.tokens_input` / `tokens_output`：落库的账（权威口径，跑完才有 —— 不在这里）。

## 三条纪律

1. **这是显示用的缓存，不是账。** 落库的账在 `ai_analysis_run` / `ai_analysis_trace`，
   权威口径是 `services/ai/usage.py`。这里只回答「现在跑到哪了、已经花了多少」，
   而且**允许查不到**（进程重启、多进程部署、跑在别的 worker 里）—— 查不到时返回
   `None`，界面显示「进度不可用」，**不显示 0**。
   **逐轮的账本已经从这一层搬走了**（工作包 E）：每一轮结束会往
   `ai_analysis_round_event` 写一条独立、立刻提交的事件行（见
   `services/ai/round_events.py`）。`ai_analysis_job.progress_json` 里那份快照**只做 UI
   快照** —— 它是整条覆盖写的、只带最近 8 轮，拿它当「这次跑了多少」的账必然丢。
   内存快照读不到时，`snapshot()` 会**从那份事件账本补一份**（重启、换进程、SSE 重连）。
2. **跑完就清。** 快照的存在时间不该超过一次分析：`clear()` 在 `_execute_analysis`
   的出口（含异常路径）调用；另外每条快照带 `updated_at`，超过 `MAX_AGE_SECONDS`
   没有更新就视为过期（进程里留下的残影不该被当成「正在跑」）。
   **事件行不清**：它就是要活过这一次进程的（重启后按它恢复）。
3. **不能因为进度而出错。** 写快照失败、读快照失败一律吞掉：它是给界面看的一眼，
   而它服务的是一条要花钱的分析路径 —— 为了显示进度把分析弄挂，是本末倒置。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from services.ai import round_events
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

# 「思考过程」标签页一次带走多少轮。**上限由载荷决定、不由省空间决定**：整个快照每
# 3 秒（`static/js/ai_stream_status.js` 的 POLL_INTERVAL_MS）重发一次，30 轮全带上就是
# 每帧上百 KB，而界面本来就滚不到那么上面去。超出时如实标 `rounds_truncated`。
MAX_LIVE_ROUNDS = 8

# 一个 run 的内存事件账本最多留几行。家族最多几十轮，这个数只是防呆；超出时从最早的
# 丢起，逐成员合计因此变成**下界**（不是算错）—— 而重启后从库里补的那一份是全量的。
MAX_LEDGER_EVENTS = 400


@dataclass(frozen=True)
class ProgressSnapshot:
    """一眼进度。字段与 `RoundProgress` 同名同义（累计值、未上报是 `None`）。"""

    run_id: int
    project_id: int
    index: int
    max_rounds: int
    status: str
    # 上游没报就是 `None`（**不是 0**）。见 `live_tokens`。
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
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
    # 逐轮的「思考过程」：每条的形状与 `ai_usage_service.run_usage()["rounds"][i]` 相同
    # （由 `trace_evidence.live_round_entry` 产出），**累积**最近 `MAX_LIVE_ROUNDS` 轮。
    # 累积是必要的：界面可能在第 5 轮才打开抽屉，看不到前面几轮等于没有过程可看。
    rounds: tuple[Any, ...] = ()
    rounds_seen: int = 0
    rounds_truncated: bool = False
    # **job 级**累计 token（跨成员，**单调不减**）。见 `_JobLedger` 与模块 docstring。
    # 与 `live_tokens` 是两个量，不许互相顶替：那个是**当前成员**的局部量。
    # 读不到（一次都没上报）就是 `None` —— 同样不许兜成 0。
    # 绕开 `publish` 手搓出来的快照（截图脚本、测试替身）见 `__post_init__`。
    job_tokens: Optional[int] = None
    # 有成员**没上报用量**（上游一次都没给这个成员的 token 数）：上面那个数是
    # **已知下界**，界面必须说出来。它**不影响单调性** —— 没上报的按 0 入账，
    # 后面的数只会更大，所以「不知道缺多少」不能变成「数字回退」的理由。
    job_tokens_partial: bool = False
    # 上面那个数**不含正在跑的那次调用**（引擎是跑完一轮才报的）。`status == "final"`
    # 那一帧之后这个成员不再发请求，所以它是 `False` —— 这是「比真实花费小」的唯一说明。
    job_tokens_pending_call: bool = False
    # **逐成员**的账（分片 / 汇总 / 复核各一份，形状见 `round_events.member_totals`）。
    # 需求要的是「每个成员分别显示输入/输出/缓存、工具请求/执行/失败/截断、耗时、候选数」，
    # 而快照原先只有一个跨成员的合计数 —— 那份账在界面上只能回答「一共花了多少」。
    #
    # 默认空元组：不传它的调用方（老代码、测试替身、绕开 publish 手搓的帧）行为与这一层
    # 之前**完全一样**（界面在没有它时不画那一块）。
    members: tuple[Any, ...] = ()
    # 这份快照是从哪来的：
    #   * `""`      —— 进程内的实时快照（正在跑，本进程看着它）；
    #   * `"ledger"` —— **从逐轮事件账本补的**（`round_events.restored_snapshot`）。
    # 两者是同一种形状、含义不同：后者说明本进程没有它的实时快照（跑在别的 worker 里、
    # 或者是被中断的那一次）。界面据此换一句说明 —— 混为一谈就会把「读不到实时进度」
    # 说成「正在跑」。
    source: str = ""
    # 运行本身的状态与「有没有结束」（只有账本那一份带得回来：实时那一份不需要它，
    # 界面此时正连着运行）。空串 = 不知道。
    run_status: str = ""
    run_finished: bool = False

    def __post_init__(self) -> None:
        """**直接构造**的快照（截图脚本、测试替身）没有账本，job 那一份就等于局部量。

        `publish` 那条路一定会显式传 `job_tokens`（哪怕是 `None`），所以这一段只对
        「绕开 publish 手搓出来的帧」生效 —— 那种帧描述的是一个成员的第几轮，两个量本来
        就是同一个数。不让它兜住的后果是：这类帧在界面上忽然一个数都不显示
        （截图里那一行少了「本次已用 N tokens」），而**与真帧同源同形**正是它们的用途。
        """
        if self.job_tokens is None:
            live = self.live_tokens
            if live is not None:
                object.__setattr__(self, "job_tokens", live)

    @property
    def live_tokens(self) -> Optional[int]:
        """**当前成员**这一份运行已消耗的 token（输入 + 输出）。**读不到就是 `None`。**

        它是**下界**（只含上游已上报的部分），与预算那一档的口径一致 —— 拿它去和上限比
        时，「下界已经超了」是确定的结论，「下界没超」不能反过来说没超。文案里必须写清。

        **它是「当前成员」的量，不是整次分析的量**：子代理模式下一个成员一个引擎实例，
        换成员就从 0 重新开始。跨成员那一份看 `job_tokens`（界面顶部读的是它）——
        两个量混用一个字段正是「换个分片数字就掉回三万」的成因。

        `None` 要**原样交出去**，不许 `or 0` 兜成 0：`ai_stream_status.progressText` 里
        「用量没上报就只说轮次，不补一个 0」那句守卫就是等这个 `None` 的 —— 兜成 0 之后
        它永远不会触发，界面上于是出现「分析中：第 1/8 轮 · 本次已用 0 tokens」，
        把「不知道花了多少」说成了「一个都没花」。
        """
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return int(self.prompt_tokens) + int(self.completion_tokens)

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
            # job 级那一份（跨成员、单调不减）与它的两条标注 —— 界面顶部读的是**它**。
            # 三个键与 `live_tokens` 一起发出去是有意的：谁用哪个量由界面自己决定，
            # 而**同一个字段不许有两个含义**（那正是这次要修的缺陷的形态）。
            "job_tokens": self.job_tokens,
            "job_tokens_partial": self.job_tokens_partial,
            "job_tokens_pending_call": self.job_tokens_pending_call,
            # 逐成员的账（见字段说明）与「这一份是实时快照还是账本补的」。
            "members": [dict(item) for item in self.members],
            "source": self.source,
            "run_status": self.run_status,
            "run_finished": self.run_finished,
            "age_seconds": max(0, int(time.monotonic() - self.updated_at)),
            # 逐轮过程（思考过程标签页）。`rounds_seen` 是**一共跑过几轮**：截断时界面要说
            # 「只列出最近 N 轮」，没有这个数就只能沉默地少给几轮。
            "rounds": [dict(item) for item in self.rounds],
            "rounds_seen": self.rounds_seen,
            "rounds_truncated": self.rounds_truncated,
        }


_lock = threading.Lock()
_snapshots: dict[int, ProgressSnapshot] = {}
# 每条运行一本 job 级 token 的账（见 `_JobLedger`）。`publish` 里建、`clear` 里丢；
# 另外按**它自己的**时间戳兜一层过期（不是「快照不在就丢」——快照被挤掉只说明界面看不到
# 那一帧，不等于这次运行结束了）。留着不放的唯一效果：同一个 run_id 的下一次会带着
# 上一次的入账继续加，而那个数看起来完全正常。
_ledgers: dict[int, "_JobLedger"] = {}


def _local_tokens(progress: Any) -> Optional[int]:
    """这一帧上报的「当前成员局部累计」token（输入 + 输出）。**读不到就是 `None`。**

    与 `ProgressSnapshot.live_tokens` 同一条口径（任一个没上报就是 `None`，不许兜成 0）。
    单列一个函数是为了「长得像进度的任何对象」都不会把写快照弄挂：脏值只是这一个数
    读不到，不是这一帧丢了。
    """
    prompt = getattr(progress, "prompt_tokens", None)
    completion = getattr(progress, "completion_tokens", None)
    if prompt is None or completion is None:
        return None
    try:
        return int(prompt) + int(completion)
    except (TypeError, ValueError):
        return None


@dataclass
class _JobLedger:
    """一次分析（一个 run）的 **job 级 token** 记账。**调用方必须已持 `_lock`。**

    ## 为什么需要它

    引擎报的累计值是**一个引擎实例**的累计，而一个引擎实例 = **一个成员**
    （`services/ai/subagent.py` 每个分片各起一个引擎，各报各的）。换成员那一帧上，
    `prompt_tokens` 从上一个成员的收尾值变成新成员从 0 开始的值 —— 快照是整条覆盖写的，
    于是抽屉顶部那个数当场回退（实测 493,531 → 34,960）。

    不能在回调那一层（`subagent._call_engine` 的 `report`）累加：那里只贴「我是谁」。
    而**帧只有这一个写入口**，所以在这里看得出来「换成员了」——判据就是 `agent_index`
    （连同 `agent` 标签）变了。

    ## 三条口径

    1. **单调不减。** 已经交出去的数永远不会变小：换成员时把上一个成员的**峰值**入账，
       新成员从 0 开始时把它加回去。成员自己的局部量也取各帧的**最大值** —— 某一轮上游
       没报 token 时，引擎的累计值会整份变成 `None`（`_sum_optional` 的口径），取最后
       一个已知值会让数字来回跳。
    2. **有成员没上报 = 已知下界。** 没上报的那一块按 0 入账，并记下这件事
       （`partial`，界面要说出来）。**它同样不回退**：缺了一块不知道有多大，
       不是数字变小的理由。两条路都要记：**跑完一轮却没带用量**（`missing_seen`）、
       以及**被换下去时整块没上报**（`banked_partial`）—— 只记后者的话，
       **最后一个成员**（汇总 / 对账）没上报就没人记账了，而那正是最该说清的一处。
    3. **不含还没返回的那次调用。** 引擎是跑完一轮才报的，所以任何一帧里的数都不含正在
       飞的那一次请求；`status == "final"` 那一帧之后这个成员不再发请求，标注才收掉。
    """

    # 上一个成员是谁。`seen` 用来区分「这是第一帧」与「真的换过成员」—— 没有它，
    # 第一帧（S1 刚起步）会被当成一次换成员，凭空记下一次「有人没上报」。
    seen: bool = False
    agent: str = ""
    agent_index: int = 0
    status: str = ""
    # 已经换下去的那些成员之和：`banked` 是**已知的那部分**，`banked_known` 记「有没有
    # 人报过数」（一个都没报时不许拿 0 冒充一个数），`banked_partial` 记「有没有人整块
    # 没报」。三个分开是必要的：「0 已知」与「不知道」在界面上是两句不同的话。
    banked: int = 0
    banked_known: bool = False
    banked_partial: bool = False
    # 见过「跑完了一轮、却没带用量」的帧（`_sum_optional` 的口径：任一轮没上报，
    # 这个成员的累计值整份就是 `None`）。它同样让 job 那个数变成下界。
    missing_seen: bool = False
    # 当前成员已报过的局部量（各帧最大值）。
    member_peak: Optional[int] = None
    # 已经报过的**逐轮事件行**（形状 = `round_events.event_fields` 的产物，也就是写进
    # `ai_analysis_round_event` 的那一份）。
    #
    # 它存在的理由只有一条：**逐成员的账要有两个来源、一个算法**。正在跑的时候事件还没
    # 全在库里（每轮写一条、但不保证读得到），而重启之后库是唯一来源。两处各写一遍求和，
    # 迟早会算出两个数 —— 所以实时这一份也走 `round_events.member_totals`，与从库里读的
    # 那一份是同一个归约函数。上限只是防呆（一个家族 100 轮以内），超了从最早的丢起
    # （丢掉的后果是逐成员合计变成下界，不是算错）。
    events: list = field(default_factory=list)
    # 最后一帧的时间（`time.monotonic()`）。**过期只按它判，不按快照在不在**：
    # 快照过期或被挤掉只说明界面看不到那一帧，不代表这条运行结束了 —— 跟着快照一起
    # 丢掉账，下一次报进来的数就会从当前成员重新数，也就是又回退一次。
    last_seen: float = 0.0

    def remember(self, row: dict) -> None:
        """记一条逐轮事件行。**同一 (成员, 轮次) 原位替换**（重报不许记两遍）。"""
        key = (str(row.get("member", "")), int(row.get("round", 0) or 0))
        for position, existing in enumerate(self.events):
            if (str(existing.get("member", "")), int(existing.get("round", 0) or 0)) == key:
                self.events[position] = row
                return
        self.events.append(row)
        if len(self.events) > MAX_LEDGER_EVENTS:
            del self.events[: len(self.events) - MAX_LEDGER_EVENTS]

    def advance(
        self,
        *,
        agent: str,
        agent_index: int,
        index: int,
        local: Optional[int],
        status: str,
    ) -> None:
        """记一帧。`index` 是**这个成员内部**的轮次（0 = 引擎的 `on_start` 那一帧）。"""
        if self.seen and (agent, agent_index) != (self.agent, self.agent_index):
            # **换成员**：把上一个成员的峰值入账，新成员从 0 开始 —— 「加回去」就是修复本身。
            self.banked += self.member_peak or 0
            self.banked_known = self.banked_known or self.member_peak is not None
            self.banked_partial = self.banked_partial or self.member_peak is None
            self.member_peak = None
        self.seen = True
        self.agent = agent
        self.agent_index = agent_index
        self.status = status
        if local is None:
            # `index >= 1` = 这一轮真的跑完了。跑完却没带用量，这个成员的账就永远读不到
            # 了（不是「还没轮到」，那是 `on_start` 那一帧的事）。
            if index >= 1:
                self.missing_seen = True
            return
        # 峰值而不是最后一个值：见口径 1。
        self.member_peak = local if self.member_peak is None else max(self.member_peak, local)

    @property
    def job_tokens(self) -> Optional[int]:
        """跨成员的累计（已知下界）。**一次都没上报过就是 `None`**（不是 0）。

        `banked_known` 那一项不能省：一个成员都没报过数时，`banked + member_peak or 0`
        会算出 0 —— 而 0 是一个结论（一个 token 都没花），与「不知道」是两件事。
        """
        if not (self.banked_known or self.member_peak is not None):
            return None
        return self.banked + (self.member_peak or 0)

    @property
    def partial(self) -> bool:
        """上面那个数是不是**已知下界**（有成员没上报用量）。"""
        return self.banked_partial or self.missing_seen

    @property
    def pending_call(self) -> bool:
        """上面那个数是不是**不含还没返回的那次调用**。"""
        return self.status != "final"


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
    # 账本按**自己的**时间戳过期（不是「快照不在就丢」）：快照被挤掉只说明界面看不到
    # 那一帧，不代表这条运行结束了 —— 跟着快照一起丢，下一次报进来的数就会从当前成员
    # 重新数，也就是又回退一次。跑完那一次由 `clear()` 显式清掉（模块纪律第 2 条）。
    for run_id in [
        key for key, item in _ledgers.items() if now - item.last_seen > MAX_AGE_SECONDS
    ]:
        _ledgers.pop(run_id, None)


def _as_int(value: Any) -> int:
    """读一个整数，读不出来就是 0。**这一层读不出来的东西不该让整帧进度丢掉。**

    与 `_local_tokens` 同一条纪律：脏值只让这一个字段缺失，不是这一帧作废
    （`publish` 的宽 `except` 会把异常咽掉，代价是这一轮**整个**快照没写 —— 而那正是
    「思考过程」一帧都收不到的那种症状）。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _merge_rounds(
    previous: Optional[ProgressSnapshot],
    entry: Any,
    agent: str,
    agent_index: int = 0,
    agent_total: int = 0,
) -> tuple[tuple[Any, ...], int, bool]:
    """把这一轮接在**已有的那几轮**后面。返回 `(rounds, rounds_seen, truncated)`。

    ## 为什么要在锁内读上一条快照

    同一个 run 的快照是**整条覆盖写**的（`_snapshots[run_id] = snapshot`），所以每一轮
    都必须把前面几轮带上，否则界面上永远只剩最后一轮。放在锁内读是为了不与并发的另一轮
    互相盖掉（子代理模式是顺序跑的，但同一进程里可能同时跑着别的项目）。

    ## 同一轮被报两次时**原位替换**，不追加

    引擎的 `_emit` 是每轮的唯一出口，但一次重试/重问可能让同一个 `round_index` 出现两次
    （例如协议纠错那一轮）。按 `round_index` 去重更接近「一轮一行」的读法，也让重放同一帧
    幂等（界面每 3 秒拿到的是同一份列表，不该越滚越长）。

    **判重必须带上 `agent`**：子代理模式下每个分片的引擎都从第 1 轮开始编号，
    只按 `round_index` 判重会把「第二个分片的第一轮」当成「第一个分片最后一轮的重报」
    而替换掉它（见下面那段注释）。

    **替换是原位替换、比较是整条列表**（两条都是后来才补上的，理由见注释）。

    ## 分片位次也在这里补齐

    `agent` / `agent_index` / `agent_total` 在引擎那一层是空的（引擎每次只跑一个成员，
    标签由 `subagent` 在回调外层贴，见 `trace_evidence.live_round_entry` 的 docstring），
    而这个进度对象知道 —— 这里补，让实时那一份与落库那份的 `agent` 含义一致，
    并且让界面能写出「分片 S3 (3/7)」这种位次（`static/js/ai_stream_status.js` 的
    `agentText` 读的就是这两个键）。
    """
    if not isinstance(entry, dict) or not entry:
        # 拿不到这一轮的明细（老调用方、测试替身）：保留已有的那几轮，别把它清掉。
        if previous is None:
            return (), 0, False
        return previous.rounds, previous.rounds_seen, previous.rounds_truncated

    item = dict(entry)
    if agent:
        # 引擎不知道自己在哪个分片里（标签由 subagent 在回调外层贴），而进度对象知道。
        # 这里补一次，让实时那一份与落库那份的 `agent` 含义一致。
        item["agent"] = str(item.get("agent") or agent)
    # 家族位次：**有这个键界面才写得出「分片 S3 (3/7)」**，也才排得出家族顺序
    # （`static/js/ai_think_log.js` 的 `familyOrder`）。读不到（0）就不写 ——
    # 编一个位次会被当成事实。
    if agent_index > 0:
        item.setdefault("agent_index", agent_index)
    if agent_total > 0:
        item.setdefault("agent_total", agent_total)
    # 成员内轮次：引擎那一层是空的（`record.agent_round` 恒为 0），而这个成员的
    # `round_index` **就是**它在本成员内的序号（每个成员的引擎各自从 1 开始编号）。
    # 界面上「第 N 轮」显示的正是这个字段（`buildRoundBlocks`），不补的话它只能退回到
    # `round_index` —— 巧合之上没有契约：同一个 `round_index` 在落库那份里是**家族全局**
    # 序号（见 `models/ai_analysis/trace.py`），两个来源的同一个键含义不同。
    if not _as_int(item.get("agent_round")):
        item["agent_round"] = _as_int(item.get("round_index"))

    existing = list(previous.rounds) if previous is not None else []
    # 「一共跑过几轮」用上一条记的那个数继续累加：截断之后列表会变短，拿列表长度当总数
    # 等于把「已经跑了 12 轮」说成「只跑了 8 轮」。
    seen = previous.rounds_seen if previous is not None else 0
    index = item.get("round_index")
    # **判重看整条列表，不是只看最后一条。** 原先只比 `existing[-1]`：同一个
    # (agent, round) 恰好落在末尾时才替换，否则就**再 append 一条**。而「一轮一行」是
    # 这个列表的契约（上面那段）—— 一旦某条晚到的帧重报了不在末尾的那一轮（重试、重连
    # 之后补发、慢回调），面板上就会出现两遍，而且第二遍在末尾：界面上是「第 8 轮 /
    # 第 1 轮 / 第 2 轮 / **第 5 轮**」这种**轮次往回跳**的样子，用户读到的就是「乱序」。
    # 原位替换同时保住两件事：一轮一行，且**不改变已有顺序**。
    target = None
    if index is not None:
        wanted = (index, str(item.get("agent") or ""))
        for position, existing_item in enumerate(existing):
            if (existing_item.get("round_index"),
                    str(existing_item.get("agent") or "")) == wanted:
                target = position
                break
    if target is not None:
        existing[target] = item
    else:
        existing.append(item)
        seen += 1
    seen = max(seen, len(existing))
    return tuple(existing[-MAX_LIVE_ROUNDS:]), seen, seen > MAX_LIVE_ROUNDS


def publish(run_id: int, project_id: int, progress: Any) -> None:
    """把一轮的进度写进快照，并把这一轮**落进逐轮事件账本**。**任何异常都吞掉**。

    ## 两条出路，一条纪律

    * **内存快照**（界面每 3 秒读的那一份）：跑动过程中的一眼，跑完就清；
    * **事件账本**（数据库，见 `services/ai/round_events.py`）：每一轮一条、立刻提交，
      重启之后还在 —— 需求里「不丢账」「不重复计费」「每个成员分别显示」都建在它上面。

    两条出路**读的是同一份行**（`round_events.event_fields(progress)`）：这一行既进内存
    账本（逐成员的账要在没落库时也算得出来），也写进库。算两遍的话，两处迟早对不上。

    **进展快照与落库顺序是有意的**：先把界面那一份写好（它坏了只是一眼看不成），再写库
    （它自己吞异常、自己管事务）。这条纪律与这个模块的三条纪律是同一条：为显示服务的
    东西坏了，代价不能是分析白跑。

    事件行那一段**单独包一层 try**：它算不出来时只是「这一轮不记事件」，**不许把这一帧
    快照也带走**（两者是两份东西，一份坏不该让另一份也没有）。
    """
    try:
        agent = str(getattr(progress, "agent", "") or "")
        run_key = int(run_id)
        # 这一轮的**事件行**（纯函数，不碰数据库）。`None` = 这一帧不是「一轮跑完」
        # （引擎的 `on_start` 帧，index=0）—— 它不记账。
        try:
            event_row = round_events.event_fields(progress)
        except Exception as exc:  # noqa: BLE001 —— 见 docstring：别带走这一帧快照
            log_print(
                f"⚠️ 整理 AI 逐轮事件失败（只影响这一次的事件账）: run={run_id} "
                f"{type(exc).__name__}：{exc}",
                "AI",
                force=True,
            )
            event_row = None
        with _lock:
            previous = _snapshots.get(run_key)
            rounds, rounds_seen, rounds_truncated = _merge_rounds(
                previous,
                getattr(progress, "round_entry", None),
                agent,
                agent_index=int(getattr(progress, "agent_index", 0) or 0),
                agent_total=int(getattr(progress, "agent_total", 0) or 0),
            )
            # job 级那一笔账**与快照在同一把锁里**：它是一份跨帧的状态（上一个成员的峰值、
            # 已经入账多少），分开算的话同时跑着的另一条分析会插进来（子代理是顺序跑的，
            # 但同一进程里可以同时跑着别的项目）。
            ledger = _ledgers.get(run_key)
            if ledger is None:
                ledger = _JobLedger()
                _ledgers[run_key] = ledger
            ledger.advance(
                agent=agent,
                agent_index=int(getattr(progress, "agent_index", 0) or 0),
                index=int(getattr(progress, "index", 0) or 0),
                local=_local_tokens(progress),
                status=str(getattr(progress, "status", "") or ""),
            )
            if event_row is not None:
                ledger.remember(event_row)
            # 逐成员的账：与「重启后从库里补的那一份」共用同一个归约函数
            # （`round_events.member_totals`），所以两条路上的数不会分叉。
            members = tuple(round_events.member_totals(run_key, rows=ledger.events))
            ledger.last_seen = time.monotonic()
            job_tokens = ledger.job_tokens
            job_partial = ledger.partial
            job_pending = ledger.pending_call
        snapshot = ProgressSnapshot(
            run_id=int(run_id),
            project_id=int(project_id),
            index=int(getattr(progress, "index", 0) or 0),
            max_rounds=int(getattr(progress, "max_rounds", 0) or 0),
            status=str(getattr(progress, "status", "") or ""),
            prompt_tokens=getattr(progress, "prompt_tokens", None),
            completion_tokens=getattr(progress, "completion_tokens", None),
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
            rounds=rounds,
            rounds_seen=rounds_seen,
            rounds_truncated=rounds_truncated,
            # job 级那一份是**算出来**的（跨帧的账，见 `_JobLedger`），不是从这一帧读的：
            # 帧上只有「当前成员」的局部量。
            job_tokens=job_tokens,
            job_tokens_partial=job_partial,
            job_tokens_pending_call=job_pending,
            # 逐成员的账（分片 / 汇总 / 复核），见字段说明。
            members=members,
            updated_at=time.monotonic(),
        )
        now = time.monotonic()
        with _lock:
            _snapshots[snapshot.run_id] = snapshot
            _prune_locked(now)
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ 写分析进度快照失败（不影响分析）: run={run_id} {exc}", "AI", force=True)
        return
    # **落库在最后一步**（见 docstring）：内存那一份已经写好了，这一笔才是重启之后还在的账。
    # `record` 自己吞异常、自己提交（它不许持着写事务跨过那次模型调用）。
    if event_row is not None:
        round_events.record(run_key, project_id, progress, fields=event_row)


def snapshot(run_id: int) -> Optional[ProgressSnapshot]:
    """读一眼进度。读不到返回 `None`，**不是 0**。

    ## 两条来路

    1. **进程内的实时快照**（正在跑、本进程看着它）—— 原样返回，与以前一字不差；
    2. 内存里没有（页面刚刷新、跑在别的 worker 里、或者 **worker 已经被杀**）→
       **从逐轮事件账本补一份**（`round_events.restored_snapshot`）。这就是需求里
       「SSE 断开再连：从数据库补快照」——不是只发新的增量，也不是让这一页永远读不到。

    第 2 条**只服务「还没有落库结论」的运行**（判据在那个函数里）：跑完的运行该读它落库
    的结论与 trace，再从事件拼一份进度出来只会让抽屉在两种状态之间来回跳。它同样只在
    **读不到实时快照时**发生 —— 正在跑的分析一次多余的库查询都不会有。
    """
    try:
        now = time.monotonic()
        with _lock:
            _prune_locked(now)
            item = _snapshots.get(int(run_id))
        if item is not None and now - item.updated_at <= MAX_AGE_SECONDS:
            return item
        return _restore_from_ledger(int(run_id))
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ 读分析进度快照失败: run={run_id} {exc}", "AI", force=True)
        return None


def _restore_from_ledger(run_id: int) -> Optional[ProgressSnapshot]:
    """内存快照不在时，从逐轮事件账本补一份。**任何异常都吞掉**（读进度不许出错）。

    「发现这条运行已经死了」这件事也在这里发生（见 `round_events._recover_if_dead` 的
    docstring）：内存快照不在、库里只有事件，说明写它的那个进程已经不在了 —— 那一刻顺手
    把事件账本补成落库的 trace 与 run 汇总，**不重跑任何模型调用**。
    """
    try:
        return round_events.restored_snapshot(run_id)
    except Exception as exc:  # noqa: BLE001 —— 补不到就是「读不到进度」，不是错误
        log_print(f"⚠️ 从逐轮事件账本补进度失败: run={run_id} {exc}", "AI", force=True)
        return None


def clear(run_id: int) -> None:
    """分析结束（成功或失败）时清掉。**幂等**，清不存在的键不报错。"""
    try:
        with _lock:
            _snapshots.pop(int(run_id), None)
            # **账本一起清**（模块纪律第 2 条：快照的存在时间不该超过一次分析）。
            # 留着它，同一个 run_id 的下一次会带着这一次的入账继续加。
            _ledgers.pop(int(run_id), None)
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ 清分析进度快照失败（不影响分析）: run={run_id} {exc}", "AI", force=True)


def reset_for_tests() -> None:
    """测试用：清空整张表（测试库是会话级共用的，跨用例的残影会让断言飘）。"""
    with _lock:
        _snapshots.clear()
        _ledgers.clear()
    # 恢复路径的「这个 run 已经补过账了」也是进程内状态，一起清（测试之间会重用 run id）。
    round_events.reset_recovery_memo()
