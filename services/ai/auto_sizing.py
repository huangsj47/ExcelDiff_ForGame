# -*- coding: utf-8 -*-
"""按预算与规模**自动推导**周版本家族的有效参数。

## 为什么是这个模块（2026-09-23，配置面收敛）

配置面收敛之后，用户只配「开关与钱」：定时分析、间隔、token/成本总预算、提示词字符
预算、子代理开关、对账轮开关。数值一栏（分片数、每片索取/轮次、取样上限、异常上限）
全部由平台按预算与本周清单规模推导 —— 推导结果写进运行的账（`budget_plan` 的
`family_pool` 节与 per-shard 表），用户看得到「为什么是这个数」，但改不了它，
也**不需要**改它：校准常数来自 runs 38~41 的真实测量（见
`.claude/skills/ai-analysis-sizing/references/measurements.md`），公式复现那几轮
「反解出来的极限」，而不是拍脑袋。

## 与 budget_plan 的分工

`budget_plan` 是「配置 → 可见计划」（把已经定下来的数字报给 UI 与预估端点）；
本模块是「预算 → 推导」（在数字定下来**之前**算它们）。校准常数需要独立成块并
引用 measurements.md，所以不并入那边。

## 校准口径（runs 38~41，deepseek-v4-flash，1M 窗口）

* 水位 = 窗口 × 60% ≈ 600k 字；580k 预算反解每片索取 ≈ 50（`(580k − 清单 − 6k) // 11k`）；
* run 40（40 次/8 轮/3 片）：三条分片索取 36/50 用尽/48，**两个上限双双击穿**；
* run 41（50 次/10 轮/3 片）：S2 双顶（50/50 + 10 轮）降级，S1 用 32、S3 用 36 ——
  富余额度没有出路（这正是「串行共享池」要解决的，池在 `subagent.FamilyQuota`）；
* 轮次 10：S2 用满 10 轮仍有产出，8 轮不够；
* 分片目标 5：用户明确要求；钳制 2~6 且不超过维度数（`subagent.group_dimensions`）。

单提交分析**不走**本模块：它的规模本来就在「小/中」档（几到几十个文件），
出厂默认（`models/ai_analysis/project_config.py`）就是为它定的。
"""
from __future__ import annotations

from dataclasses import dataclass

# 分片数：默认目标 5（用户要求），实际钳制在 `plan_family` 里做（2~MAX_SUBAGENTS 且
# 不超过维度数）——这里只给目标值。
SHARD_TARGET = 5

# 每片轮次。校准：run 41 的 S2 用满 10 轮仍有产出、run 40 的 8 轮不够（见模块 docstring）。
ROUNDS_PER_SHARD = 10

# 每片索取名义额的上下限。上限 60 防的是「预算配得离谱大时算出几百次」——每片一次
# 索取约 11k 字，60 次已经远超窗口水位，再高只是个没有意义的数字。下限 8 是防饿死
# 的底线（低于它一个分片连「清单分诊 + 抽查几条」都做不完）。
REQUESTS_FLOOR_PER_SHARD = 8
REQUESTS_CEILING_PER_SHARD = 60

# 反解索取上限用的三个口径（与 budget/引擎同一套数）：
#   * 单条上下文 ≈ 11,000 字（`context_tools` 的单条上限，反解除数取保守侧）；
#   * 历史结论基线每轮 ≈ 6,000 字（`baseline.build_baseline_digest` 的实测口径）；
#   * 清单里每个列出的文件 ≈ 73 字（实测：200 个名字 ≈ 14.6k 字符）。
ITEM_CHARS = 11_000
BASELINE_CHARS = 6_000
LIST_CHARS_PER_FILE = 73

# 清单取样上限（替代用户配置的 max_files_per_run）。下限 200 = 旧默认；上限 500 =
# 清单字数别把预算吃穿（500 个名字 ≈ 36.5k 字，仍只占 580k 的 6%）。
SAMPLING_CAP_MIN = 200
SAMPLING_CAP_MAX = 500

# 周版本路径的「单次异常上限」平台常量（`max_anomalies_per_run` 收敛后的值）。
# 19~23 条是一个分片在 10 轮里**报得出也写得完**的量级（run 41 主结论 19 条）。
ANOMALY_RUN_CAP = 20


@dataclass(frozen=True)
class FamilySizing:
    """一次周版本分析的家族有效参数（全部由 `derive_family_sizing` 推导）。"""

    shard_count: int
    # 每片的名义额（索取/轮次）。串行共享池在名义额之上滚动富余（见 FamilyQuota）。
    requests_per_shard: int
    rounds_per_shard: int
    # 家族共享池 = 分片数 × 名义额。汇总与对账轮从池内剩余取用。
    family_requests_pool: int
    family_rounds_pool: int
    sampling_cap: int
    anomalies_per_subagent: int
    # 推导依据的一句话（进 budget_plan 的 family_pool 节，给用户看「为什么是这个数」）。
    note: str


def sampling_cap_for(file_count: int) -> int:
    """清单取样上限：clamp(真实文件数, 200, 500)。

    单独暴露这个函数，因为它的用点（`build_weekly_payload` 选清单）发生在**预算还没
    压窗口之前**，而 `derive_family_sizing` 的其余推导要等 `_apply_model_window` 之后；
    同一个公式只许有一份（`SAMPLING_CAP_*` 常量），两处各算会漂移。
    """
    return _clamp(max(0, int(file_count)), SAMPLING_CAP_MIN, SAMPLING_CAP_MAX)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def derive_family_sizing(
    *,
    effective_user_chars: int,
    file_count: int,
    dimension_count: int = 9,
    anomaly_run_cap: int = ANOMALY_RUN_CAP,
) -> FamilySizing:
    """按生效的用户内容额度与本周清单规模，推导一次周版本分析的家族参数。

    `effective_user_chars` 是 `_apply_model_window` 压完窗口、扣掉平台内置提示词之后
    的**用户内容**额度（560k 配置 + 内置后压到水位 ≈ 584k，580k 配置不压 ≈ 580k）。
    `file_count` 是本周真实变更文件数（决定取样上限与清单字数）。

    公式（校准见模块 docstring 的 runs 38~41）：

    * 取样上限 = clamp(文件数, 200, 500) —— 名字进提示词的量；
    * 每片索取 = clamp((额度 − 清单 − 基线) // 11k, 8, 60) —— 580k 复现出实测的 ≈50；
    * 每片轮次 = 10；
    * 每片异常 = clamp(ceil(全次上限 × 2 ÷ 片数), 4, 12) —— 「分片 × 每片 ≥ 全次上限」
      仍然成立（`_engine_limits` 的既有理由），×2 留出汇总去重/门槛滤掉的那部分。
    """
    files = max(0, int(file_count))
    sampling_cap = sampling_cap_for(files)
    list_chars = sampling_cap * LIST_CHARS_PER_FILE

    budget = max(0, int(effective_user_chars))
    solvable = max(0, budget - list_chars - BASELINE_CHARS)
    requests_per_shard = _clamp(solvable // ITEM_CHARS, REQUESTS_FLOOR_PER_SHARD, REQUESTS_CEILING_PER_SHARD)

    shards = _clamp(int(SHARD_TARGET), 2, 6)
    if dimension_count and dimension_count > 0:
        # 维度比目标少时少开几个分片（group_dimensions 的钳制在这里先取整，避免
        # 「推导说 5 片、实际只能开 3 片」的账目对不上）。
        shards = min(shards, max(2, int(dimension_count)))

    anomalies_per_subagent = _clamp(
        -(-anomaly_run_cap * 2 // max(1, shards)), 4, 12
    )

    note = (
        f"按生效内容额度 {budget:,} 字与本周 {files} 个变更文件推导："
        f"清单取样 {sampling_cap} 个名字（≈{list_chars:,} 字）、每片索取 "
        f"{requests_per_shard} 次（(额度−清单−基线{BASELINE_CHARS:,})÷{ITEM_CHARS:,}，"
        f"校准自 runs 38~41 的实测极限）、每片 {ROUNDS_PER_SHARD} 轮、{shards} 个分片；"
        f"索取/轮次为家族共享池（{shards}×{requests_per_shard} 与 {shards}×{ROUNDS_PER_SHARD}），"
        "串行滚动、前片用剩的滚给后片。"
    )
    return FamilySizing(
        shard_count=shards,
        requests_per_shard=requests_per_shard,
        rounds_per_shard=ROUNDS_PER_SHARD,
        family_requests_pool=shards * requests_per_shard,
        family_rounds_pool=shards * ROUNDS_PER_SHARD,
        sampling_cap=sampling_cap,
        anomalies_per_subagent=anomalies_per_subagent,
        note=note,
    )
