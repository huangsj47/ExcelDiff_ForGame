#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐轮事件账本：**每一轮跑完就在事务里落一条**，不再等整次运行结束。

## 为什么需要它（缺陷：运行可观测性与恢复）

原先只有 `ai_analysis_trace` 一张账，而它是**整次运行跑完**才批量写进去的
（`services/ai_analysis_service._persist_outcome`）。于是运行中的进度只能靠
`ai_analysis_job.progress_json` 里那个**最近 8 轮**的窗口（`run_progress.MAX_LIVE_ROUNDS`）
看：窗口外的轮次看不见，而 **worker 中途被杀就整份账都没了** —— trace 一行没写、
run 的 token 三列还是 NULL，用户重新打开抽屉只看到「这次运行没有留下逐轮记录」。

所以这一层把「一轮结束」做成一个**独立、幂等、立刻提交**的事件行：

* 唯一键 `(job_id, run_id, member, round)` —— 同一轮重复上报只是覆盖，不会多记一笔；
* 事务在**这一轮结束时**就提交，**不跨越模型调用**（SQLite 上写事务不长时间持有）；
* 进程重启后**按事件恢复**：已完成的轮次与 token 账都还在库里，**不需要重跑模型**。

## 为什么另起一张表，而不是提前写 `ai_analysis_trace`

`trace` 的唯一键是 `(run_id, round_index)`，而 `round_index` 是**家族内全局递增**的序号
（见 `models/ai_analysis/trace.py` 的说明）—— 运行中每个成员只知道自己在**本成员内**的
轮次号，全局序号要等 famille 跑完、由 `subagent._merge_rounds` 重编。运行中写 trace 就得
先赌一个全局序号，赌错了就是唯一键冲突。事件的键用**成员内**轮次，运行中就是确定的。

`trace` 列里也有几列是落库时才拼得出的（结论载荷、覆盖账本），事件行只记**这一轮花了
什么、拿到了什么**，两者是互补的：事件负责「跑的时候与被打断之后」，trace 负责「跑完之后
的完整明细」。恢复路径（`services/ai/round_events.reconcile_interrupted_run`）会把事件
补成 trace 行，所以被中断的运行最终也能在同一个面板上读。

## 口径：`None` 是「未上报」，不是 0

token 与缓存四列照抄全库同一条口径（见 `services/ai/usage.py`）：**上游没报就是 NULL**，
`0` 是一个确定的结论（这一轮确实没花）。读取侧（`round_events.member_totals`）据此区分
「未上报」与「零」，界面不许把前者显示成后者。

## 为什么诊断数据（指纹 / 推理 token / 三类耗时）也落在这张表上

因为它们与逐轮用量是**同一件事的三个面**：「这一轮花了多少」「这一轮的钱花在哪
（模型调用还是本地取数）」「这一轮为什么没有复用上一次的前缀」。分开记就要在两张表上
各写一遍轮次键，而两边的轮次口径一旦分叉（成员内序号 vs 家族全局序号），对齐它们的
代码就会在某次改动里静默错位。

**不另建表、不另造一套账**：新列落在这一张上，写入口仍然是唯一那一个
（`round_events.record`），读入口仍然是 `round_events.member_totals` 与
`round_events.events_for_run`。诊断值的算法与口径在
`services/ai/request_fingerprint.py`（哈希是本机诊断指标，**不是命中率**，见那里的
模块文档）。

## 为什么没有指向 run/project 的外键（**实测过的硬约束**）

这张表**刻意不声明** `ForeignKey`。两条都是这一行 `nullable=False` 会踩到的：

1. `tests/test_delete_project_cleans_every_project_scoped_table.py` 有一条静态护栏：
   schema 里任何「指向 project.id 或指向 project 子表的**非空外键**」所在的表，
   都必须在 `repository_admin_handlers.delete_project` 的源码里出现。加了外键，那条
   护栏立刻变红，而那个清单不在本工作包的文件主权内；
2. 那条护栏守的是真问题（删项目会撞 NOT NULL），但它的解法是「显式清理」，而本表
   真正的清理时机是**保留期清理**（run 被删时它的逐轮事件也该走）。

所以这里只存 id，并在 `services/ai/round_events` 里按 `run_id` 读写；run 被删时它的逐轮
事件**必须显式清**（没有外键，删父行带不走它）—— 生产路径是
`services/ai/run_cache_source.cleanup_expired_analysis_runs`，把这张表并进那个「先子后父、
同一个事务」的批量删除里（`forget_run` 是测试与工具用的入口）。漏掉的后果不是「几行垃圾」
而是**错数据**：run 的 id 会被后面新建的 run 复用，下一次运行的逐轮视图会读到这一批的行。
"""

from datetime import datetime, timezone

from .. import db
from ..big_text import BigText


class AiAnalysisRoundEvent(db.Model):
    __tablename__ = "ai_analysis_round_event"

    id = db.Column(db.Integer, primary_key=True)
    # 这条事件属于哪一次运行。**非空**：事件的全部意义就是它挂在某次运行上。
    run_id = db.Column(db.Integer, nullable=False, index=True)
    # 触发这次运行的 job（可空：脚本 / 测试直接建 run 时没有 job）。唯一键里有它，
    # 所以可空性必须写清楚：SQLite 的唯一索引把 NULL 当**互不相同**，于是
    # `(job_id, run_id, member, round)` 在 job_id 为 NULL 时**不产生约束作用** ——
    # 这就是下面同时保留第二个唯一键的原因（已实测：只留四列那个键，NULL 那一行能重复插入）。
    job_id = db.Column(db.Integer, index=True)
    # 项目号只作筛选用（面板按项目聚合），同上：不做外键。
    project_id = db.Column(db.Integer, index=True)

    # 这一轮是**谁**跑的。空串 = 主代理自己那几轮（单代理运行，以及子代理模式的汇总）；
    # `S1`/`S2`… = 第几个分片代理（见 `services/ai/subagent.py`）；`V1` = 对账轮。
    member = db.Column(db.String(40), nullable=False, default="")
    # 这个成员在家族里的位次（1 起；汇总/主代理那一次是 `count+1`，对账是 `count+2`）。
    member_index = db.Column(db.Integer, default=0)
    member_total = db.Column(db.Integer, default=0)

    # 这一轮在**那个成员内部**的序号（第 1 轮就是 1）。与 `member` 一起构成唯一键 ——
    # 家族全局序号要等跑完才编得出来（见模块 docstring）。
    round = db.Column(db.Integer, nullable=False, index=True)
    status = db.Column(db.String(30))
    parsed_ok = db.Column(db.Boolean, default=False)

    # 这一轮的用量。**NULL = 上游没报**（不是 0）。四列都是**本轮值**，不是累计值 ——
    # 成员的合计由读取侧求和（与 `ai_analysis_trace` 逐列同名同义）。
    tokens_input = db.Column(db.Integer)
    tokens_output = db.Column(db.Integer)
    cache_read_tokens = db.Column(db.Integer)
    cache_write_tokens = db.Column(db.Integer)
    # 输出 token 里有多少是**隐藏推理**（上游报 `completion_tokens_details` 那一类字段
    # 时）。**NULL = 上游没报**，与「报了 0」是两件事 —— 逐轮事件账本与全库同一条口径
    # （见 `services/ai/usage.py`）。这一格是「输出 token 涨了，是写得更长还是想得更久」
    # 的唯一依据，也是「能不能下调单次输出上限」的前置数据。
    reasoning_tokens = db.Column(db.Integer)
    # **缓存/命中字段是从哪读到的**（`cache_read_tokens` 的来源标记），与 run 上那一列
    # `cache_source` 同义；空/NULL = 没读到。它回答的是「为什么这个端点从来不上报缓存」。
    usage_source = db.Column(db.String(60))
    # 推理 token 是从哪个字段读到的（与上一列分开：两者是**两次独立的探测**，一个端点
    # 完全可能报缓存却不报推理 —— 合成一列就说不清是哪一项没有）。空/NULL = 没读到。
    reasoning_source = db.Column(db.String(60))
    # 三类耗时**分开记**（原先把模型调用与本地取数/建索引混在一个 `duration_ms` 里）。
    #
    # 优先级说明（指引实测）：本样本 99.7% 的时间在模型调用链，所以这三样是次要的观测值，
    # 不是优化的靶子。**NULL = 这一轮没有这一类耗时**（没发生 / 没分开量），不是 0。
    model_call_ms = db.Column(db.Integer)
    tool_fetch_ms = db.Column(db.Integer)
    index_build_ms = db.Column(db.Integer)

    # ---- 逐请求指纹（诊断「缓存为什么没命中」）-------------------------------
    #
    # 它回答的是一个此前**没有任何数据**能回答的问题：这次请求与上一次在哪个消息开始
    # 分叉。四个诊断值由 `services/ai/request_fingerprint.py` 在**每次模型调用之前**
    # 对真正发出去的消息序列算出来（去掉内部缓存断点之后的那一份）。
    #
    # **哈希是本机诊断指标，不是命中率。** 提供商按 token / 自己的内部单元匹配缓存：
    # 哈希相同**不保证**缓存命中（服务端可能已过期），哈希不同也**不保证**未命中
    # （我们这一侧多一个不参与匹配的字段就会让哈希变掉）。别把它当命中率用。
    #
    # **这里不存提示词正文**（只存哈希与计数），也不存 API key —— 端点标识是
    # `normalize_base_url` 归一之后的地址，URL 里的凭据已经被它去掉。
    request_fingerprint = db.Column(db.String(64))
    stable_prefix_fingerprint = db.Column(db.String(64))
    # 与**上一个请求**（同一次运行、同一个成员内的上一次调用）的最长公共前缀。
    # **NULL = 没有可比的上一个请求**（这一次运行的第一次调用），不是「公共前缀 0 条」。
    prefix_common_messages = db.Column(db.Integer)
    prefix_common_chars = db.Column(db.Integer)
    # 分叉原因，取值见 `request_fingerprint.DIVERGENCE_REASONS`：
    # `initial` / `append` / `compaction` / `prompt_change` / `snapshot_change` / `other`。
    # **`other` 才是要去看代码的那一类**（前缀变了而提示词版本与快照都没变）。
    prefix_divergence_reason = db.Column(db.String(30))
    # 指纹的其余形状与版本号（消息条数、请求字符数、稳定前缀条数、prompt 版本、快照 id、
    # 是否压缩、模型、端点）。单独占列的只有上面那几个会被筛/被比的字段，其余进这一份
    # JSON —— 它们要按整体读，拆成十列只会让这张表再宽十格。
    fingerprint_json = db.Column(BigText)

    # 工具账（**计数**，不是明细）：要了几次、执行了几条、几条取不到、几条被截断、
    # 几条因额度被拒、几条在入白名单时被丢。明细仍在 `entry_json` 里（有上限），
    # 计数没有上限 —— 「这一轮要了 19 次、只执行 3 次」这种事必须量得准。
    #
    # **NULL = 这一轮没有上报计数**（老调用方、测试替身给的帧里没有那一份计数），
    # 不是 0。与 token 四列同一条口径，读取侧（`round_events.member_totals`）据此
    # 给出「已上报」与「下界」两种说法。
    tool_requests = db.Column(db.Integer)
    tool_executed = db.Column(db.Integer)
    tool_failed = db.Column(db.Integer)
    tool_truncated = db.Column(db.Integer)
    tool_refused = db.Column(db.Integer)
    tool_dropped = db.Column(db.Integer)

    # 这一轮报出了几条**候选结论**（模型这一轮交回的 `anomalies` 条数）。NULL = 这一轮
    # 不是交结论的那一轮（或引擎没报）—— 不是 0，0 表示「交了结论，一条都没有」。
    candidates = db.Column(db.Integer)

    duration_ms = db.Column(db.Integer)
    context_chars = db.Column(db.Integer)
    request_chars = db.Column(db.Integer)
    # 这一轮的明细，形状与 `trace_evidence.live_round_entry` **逐字相同**（抽屉渲染器
    # 只认这一种形状）。存下来是为了「重启后重新打开抽屉」还能画出过程 —— 实时那份
    # 只在内存里，重启就没了。
    entry_json = db.Column(BigText)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    # 同一轮重复上报（重试、晚到的帧）时更新这一列；`created_at` 保持第一次的时间。
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        # 真正的去重键：一次运行里「哪个成员的第几轮」只该有一行。
        db.UniqueConstraint(
            "run_id", "member", "round", name="uq_ai_round_event_run_member_round"
        ),
        # 需求里写明的键（`job_id/run_id/member/round`）。与上面那个并存不是冗余：
        # job_id 可空时这一个不生效，而上面那个生效（见 `job_id` 列的说明）。
        db.UniqueConstraint(
            "job_id", "run_id", "member", "round", name="uq_ai_round_event_job_member_round"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AiAnalysisRoundEvent run={self.run_id} member={self.member!r} "
            f"round={self.round} {self.status}>"
        )
