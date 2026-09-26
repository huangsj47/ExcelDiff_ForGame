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

## 计划（2026-09-23 工作包 B）：规模由**快照事实**决定，预算只是**上限**

上面那套 `derive_family_sizing` 的因果方向是错的，而且已经被实测证伪：它**从提示词字符
预算的上限反解每片索取次数**（580k → ≈50 次），分片数又写死目标 5。于是「只有 3 个变更
文件、1,597 字 diff」的配置 3 照样开 5 个分片、每片 50 次索取 —— 实测 run 45 多代理
**979,910 token / 18 分 45 秒**，而同快照、同模型、同提示词的 run 46 单代理只要
**114,333 token / 2 分 59 秒**（两者都 3/3 文件证据、都命中 2/2 金标）。相差的 88% token
买的不是覆盖率，是「多个角色重复审阅同样的 3 个文件」。
（以上为**实测**；下面的阈值为**初值**，见 `SMALL_BATCH_*`。文档：
`docs/AI分析全链路复测与强制改造指引-2026-09-23.md` §1 / §3.B。）

`plan_analysis` 是新的入口，纯函数，顺序是：

1. **小批次直接单分析者**：文件数、**差异载荷估算字符数**、单文件最大值三个确定性
   事实都在门槛内 → 一个分析者看完，且它仍然覆盖**全部**声明维度（维度是检查清单，不是
   分工依据）；
2. 超门槛才分工：先按文件/提交/生成物/引用关系构造**确定性变更簇**（`cluster_changes`），
   簇太大或归属不清时**才**允许 `should_consult_planner` → 1 次受限规划调用
   （输入只有元数据，见 `planner_metadata`），返回的 JSON 必须过 `validate_grouping`
   （越权路径/漏文件/空簇/超上限一律退回确定性分组）；
3. 每片的名义额由**它自己那一簇的证据体积**推导，`derive_family_sizing` 算出来的那个数
   **降级为上限**（"预算只是上限，不是用满目标"），再加上单次硬上限（`single_run_*`）。

为什么把「阈值」和「上界」分开写：改预算**不该**改变分工（那是规模的事），改规模
**不该**让单次花费无界（那是预算的事）。两者混在一个公式里正是原来那个 bug。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence

# 分片数的**预算档目标**。2026-09-23（工作包 B）起它的角色变了：以前它是「本周开几片」
# 的答案（于是 3 个文件也开 5 片），现在**只是 `derive_family_sizing` 给出的预算上限**
# ——真正开几片由 `plan_analysis` 按快照事实（独立变更簇与证据体积）推导，并且
# `min(推导值, 这个上限)`。改它不会改变小批次的行为（那一档根本不走家族路径）。
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


# ==========================================================================
# 计划（工作包 B）
# ==========================================================================

#: 计划的版本。**它进 `analysis_revision`**（见 `compose_analysis_revision`）：改了这个
#: 模块里任何会改变分工/额度/裁剪的口径，就必须同时改这个号，否则「基线口径已变」那句
#: 提示不会出现，旧结论会被当成新口径下的产物静默复用。
PLAN_VERSION = "2026-09-23.1"

# --- 小批次门槛（**初值，待基准校准**）---
#
# 这三个数**不是**「8 个文件天然安全」这类事实，它们是从两条实测出发的一个起点：
#
# * run 46（3 个文件、1,597 字 diff）：单代理 114,333 token、2 分 59 秒、3/3 文件证据、
#   命中 2/2 金标 —— 这一档分工没有任何收益，只有成本；
# * run 45 同快照多代理：979,910 token、18 分 45 秒 —— 多花的钱买的是重复取证。
#
# 所以门槛的意义是「**低于它，分工一定亏**」。等大版本金标（配置 1）跑出「分工开始有
# 召回收益」的那一档之后，这三个数要按它校准 —— 现在它们是**初值**，不是结论。
SMALL_BATCH_MAX_FILES = 8
SMALL_BATCH_MAX_TOTAL_CHARS = 80_000
# 单文件超限单独一档：一个文件自己就吃掉小批次总量的一半时，它需要的是**分页读**
# （D 工作包的行窗/续读），不是「再开一个分片」—— 分片只会让两个成员各读一半的表。
SMALL_BATCH_MAX_FILE_CHARS = 40_000

# 家族成员数（分片）的绝对上下限。**2~5 个变更簇**是指引给的范围；下限 2 是「一个成员
# 就是单代理」那条既有判据（`subagent.plan_family` 的 `count < 2` 分支）。
FAMILY_MIN_MEMBERS = 2
FAMILY_MAX_MEMBERS = 5

# 单代理计划的额度。8 轮 / 40 次是**项目出厂默认**（`EngineLimits` 的 max_rounds=8、
# `DEFAULT_MAX_TOOL_REQUESTS`=40）：run 46 实测只用了 4 轮 / 11 次就拿到 3/3 证据与
# 2/2 金标，所以这一档不是「够用」而是「够宽」。
SINGLE_AGENT_ROUNDS = 8
SINGLE_AGENT_REQUESTS = 40

# --- 单次硬上限（token）---
#
# 「未设置项目预算」落到 1 亿 token/月，那是**月度**闸门，不是单次上限 —— 它允许一次
# 分析把整个月的额度烧完。这两个数补的是「一次分析最多花多少」。
#
# 初值的来历（**实测**）：run 45 的重型多代理是 979,910 token，run 44 是 1,043,444 ——
# 小计划上限取 150 万 = 那两轮实测的约 1.4~1.5 倍，够跑完一次完整的家族分析，但拦住
# 「因为某个上限配错而跑成 1 亿」；大计划（500+ 文件、分工之后）取 800 万，同样是
# runs 38~41 里最贵那一次（约 4.0M raw token）的两倍。**都是初值**。
#
# 「用户可改」的入口是项目配置里那个**周期** token 上限（`budget_token_limit`，管理员能改）：
# 它比这两个初值更紧时单次也不许超过它（只收紧、不放松 —— 周期留空解析成 100M/月，
# 要是拿它当单次上限就等于「一次可以花一亿」，正是这里要修的那件事）。
SINGLE_RUN_TOKEN_CAP_SMALL = 1_500_000
SINGLE_RUN_TOKEN_CAP_LARGE = 8_000_000

#: 预留给报告收尾的额度占单次上限的比例（下限见下）。收尾 = 汇总那一次调用 + 报告正文，
#: 它是**一次完整的模型调用**，不能被前面的探索吃掉 —— 没有它，预算耗尽时连「有哪些没查」
#: 都写不出来，产出的报告是残缺的而不是「带缺口的」。
REPORT_RESERVE_RATIO = 0.15
REPORT_RESERVE_FLOOR = 60_000

#: 平台内置提示词（skill + 项目知识包）的保守预留字数。口径出处：
#: `models/ai_analysis/project_config.py` 里那段预算推导写着「平台 skill 与项目知识包
#: （约 12,000）」。用于「本轮保守预留」的估算，**不是**真实探测到的值。
PLATFORM_OVERHEAD_RESERVE_CHARS = 12_000

# 每片索取由**它那一簇的证据体积**推导时的两个常数：
#   * 每读一份 diff/正文 ≈ 11,000 字（`ITEM_CHARS`，取数侧单条上限）；
#   * 除开取证之外还要留几次「清单分诊 + 边界确认」的索取（run 46 单代理 11 次里有
#     3 次是 file_diff、3 次 file_content，其余是这个用途）。
DISCOVERY_REQUESTS = 6
#: 一轮大致能吃掉几次索取（run 41 的 S2：50 次 / 10 轮 = 5）。用于把索取次数折成轮次。
REQUESTS_PER_ROUND = 5
#: 一个成员在一次分析里能真正消化的 diff 体量（字符）。超过它就该再开一个成员 ——
#: 这是「分片目标由**证据体积**推导」的那个体积。≈ 27 条 × 11,000 字。
MEMBER_EVIDENCE_CHARS = 300_000

#: 簇多到这个数以上、或者有文件归属不清时，才允许 1 次受限规划模型调用。
PLANNER_MAX_CLUSTERS = 12

#: 规划模型调用返回的 JSON 形状（写进计划，界面与日志都能核对它到底被要求了什么）。
PLANNER_JSON_CONTRACT = (
    '{"groups":[{"id":"g1","paths":["a/b.lua"],"objective":"…",'
    '"qa_dimensions":["code_logic"]}],"shared_paths":[],"reason":"…"}'
)



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


# ==========================================================================
# 快照事实：计划唯一的规模输入
# ==========================================================================

@dataclass(frozen=True)
class SnapshotFacts:
    """冻结快照的**确定性事实**（计划的唯一规模输入）。

    ## 为什么是这些字段

    指引 §2 点名要从快照算的六样：文件数、**差异载荷估算**字符数、单文件最大值、
    二进制/表格/代码比例、关键路径、截断状态。它们的共同点是**都不依赖用户配了多少钱**
    —— 这正是要修的那条因果：改预算不该改变分工。

    ## `chars_estimated`：估算与实测必须分开

    大版本（1000+ 个文件）逐行读 `merged_diff_data` 会把预估端点拖慢一个数量级，所以
    超过 `EXACT_DIFF_SAMPLE_MAX` 个文件时按**等距抽样**放大，并把这件事记在
    `chars_estimated` 上。计划里带着它，读的人才知道「80,000 字」是量出来的还是推出来的
    —— 两者都可用，但**不许看起来一样**（与「未上报不许写成 0」是同一条纪律）。

    `entries` 是每个变更文件的一行元数据（路径/提交/后缀/来源/字符数），
    `cluster_changes` 只吃它。它**不含任何表格正文** —— 规划模型能看到的上限就是它。
    """

    file_count: int = 0
    diff_payload_chars: int = 0
    max_file_diff_chars: int = 0
    # 输入本身被截断过（清单取样、hunk 截断…）。截断时「没有超限」这句话不成立。
    truncated: bool = False
    chars_estimated: bool = False
    sample_size: int = 0
    fingerprint: str = ""
    entries: tuple[dict, ...] = ()
    critical_paths: int = 0
    table_files: int = 0
    code_files: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | "SnapshotFacts" | None) -> "SnapshotFacts":
        """从 Mapping / 自身造一份。**坏值一律回 0**（计划必须能算出来，不能抛）。"""
        if isinstance(value, SnapshotFacts):
            return value
        data = dict(value or {}) if isinstance(value, Mapping) else {}
        raw_entries = data.get("entries")
        entries: list[dict] = []
        for row in raw_entries if isinstance(raw_entries, (list, tuple)) else ():
            item = _entry(row)
            if item is not None:
                entries.append(item)
        return cls(
            file_count=_int(data.get("file_count")) or len(entries),
            # **差异载荷估算字符数**（旧键名 `rendered_diff_chars` 仍要认）。
            #
            # 名字改过：这个数**不是**渲染成模型看到的 Markdown 之后的长度，而是库里的
            # `merged_diff_data` 解码再 `json.dumps(ensure_ascii=False)` 的**载荷**长度
            # ——它比渲染后偏大（含 JSON 键名与结构），所以门槛判定偏保守。原先叫
            # 「渲染后 diff 字数」，那是**名不副实**：读的人会以为平台真的渲染过一遍。
            #
            # 旧键名必须继续认：`snapshot_facts` 冻在 `request_payload` 里，已经在库里的
            # 那几十条运行（含实测的 run 45/46/52/53）都写着旧名字。不认的话它们的事实
            # 会被读成 0，而那会让「当时凭什么这么分工」变成一句空话。
            diff_payload_chars=_int(
                data.get("diff_payload_chars", data.get("rendered_diff_chars"))
            ),
            max_file_diff_chars=_int(data.get("max_file_diff_chars")),
            truncated=bool(data.get("truncated")),
            chars_estimated=bool(data.get("chars_estimated")),
            sample_size=_int(data.get("sample_size")),
            fingerprint=str(data.get("fingerprint") or ""),
            entries=tuple(entries),
            critical_paths=_int(data.get("critical_paths")),
            table_files=_int(data.get("table_files")),
            code_files=_int(data.get("code_files")),
        )

    @property
    def small_batch_exceeded_by(self) -> str:
        """没进小批次第一道门槛的**原因**（空串 = 三条都过）。

        返回一句话而不是布尔：计划与界面要回答的是「为什么这次要分工」，
        而「8」这个数字本身答不了那个问题。
        """
        if self.file_count > SMALL_BATCH_MAX_FILES:
            return f"变更文件 {self.file_count} 个 > 门槛 {SMALL_BATCH_MAX_FILES} 个"
        if self.diff_payload_chars > SMALL_BATCH_MAX_TOTAL_CHARS:
            return (
                f"差异载荷估算共 {self.diff_payload_chars:,} 字 > 门槛 "
                f"{SMALL_BATCH_MAX_TOTAL_CHARS:,} 字"
            )
        if self.max_file_diff_chars > SMALL_BATCH_MAX_FILE_CHARS:
            return (
                f"单个文件最大 {self.max_file_diff_chars:,} 字 > 门槛 "
                f"{SMALL_BATCH_MAX_FILE_CHARS:,} 字（它需要分页读，不是再开一个分片）"
            )
        if self.truncated:
            return "本次输入被截断过（截断状态下「没有超限」证明不了）"
        return ""

    @property
    def is_small_batch(self) -> bool:
        return not self.small_batch_exceeded_by

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_count": self.file_count,
            "diff_payload_chars": self.diff_payload_chars,
            "max_file_diff_chars": self.max_file_diff_chars,
            "truncated": self.truncated,
            "chars_estimated": self.chars_estimated,
            "sample_size": self.sample_size,
            "fingerprint": self.fingerprint,
            "critical_paths": self.critical_paths,
            "table_files": self.table_files,
            "code_files": self.code_files,
            "entries": [dict(item) for item in self.entries],
            "thresholds": {
                "max_files": SMALL_BATCH_MAX_FILES,
                "max_total_chars": SMALL_BATCH_MAX_TOTAL_CHARS,
                "max_file_chars": SMALL_BATCH_MAX_FILE_CHARS,
            },
            "small_batch": self.is_small_batch,
            "small_batch_exceeded_by": self.small_batch_exceeded_by,
        }

    def digest(self) -> str:
        """这份事实的短指纹：计划的「它是为哪一份输入算的」那一栏。

        **不含时间、不含预算** —— 同一份输入在任何时候算出来的计划指纹都相同，
        所以「预估用的计划」与「真正跑的计划」是不是同一份，可以直接比这个值。
        """
        body = json.dumps(
            {
                "files": self.file_count,
                "chars": self.diff_payload_chars,
                "max_file": self.max_file_diff_chars,
                "truncated": self.truncated,
                "paths": [item.get("path") for item in self.entries],
                "commits": [item.get("commit") for item in self.entries],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


#: `file_content` 那一类的后缀（与 `manifest._features` 的 `structured_data` 同一组）。
_TABLE_SUFFIXES = {".xlsx", ".xls", ".csv"}
_CODE_SUFFIXES = {".lua", ".py", ".js", ".ts", ".cs", ".cpp", ".h", ".java", ".json", ".xml"}


def _entry(row: Any) -> Optional[dict]:
    """一行变更文件的元数据。**只留规划需要的键**（正文一个字都不进来）。"""
    item = row if isinstance(row, Mapping) else {}
    path = str(item.get("path") or item.get("file_path") or "").strip()
    if not path:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    directory = str(PurePosixPath(path).parent)
    return {
        "path": path,
        "commit": str(item.get("commit") or item.get("latest_commit_id") or "").strip(),
        "directory": directory,
        "suffix": suffix,
        "source": str(item.get("source") or "delta"),
        "chars": max(0, _int(item.get("chars"))),
    }


# ==========================================================================
# 确定性变更簇
# ==========================================================================

#: 「同源」判据：同目录同主名的「源表 ↔ 生成物」算同一个变更簇。
#:
#: 为什么必须归一簇：`services/ai/dependency_resolver.py` 已经承认这种关系（同名源表与
#: 生成物算一跳依赖）。把它们分给两个成员，一个只看表、一个只看代码，两边都只能看到
#: 「一半的改动」—— 而这一半一半正是「跨文件一致性」那类问题最容易漏的形态。
_GENERATED_SUFFIXES = {".lua", ".json", ".xml", ".bytes", ".txt"}


def _cluster_key(item: Mapping[str, Any]) -> tuple[str, str]:
    """确定性分簇的键：**提交 + 顶层目录**。

    这两样是指引点名的「按文件/提交/生成物/引用关系」里**不需要任何推测**的两样：
    同一批提交改的东西通常是一次改动（可一起看），同一目录的东西通常同属一个模块。
    引用关系（谁调用谁）需要索引，只在 `should_consult_planner` 那一档才值得算。
    """
    commit = str(item.get("commit") or "")
    path = str(item.get("path") or "")
    parts = [seg for seg in path.split("/") if seg]
    top = parts[0] if len(parts) > 1 else ""
    return commit, top


def cluster_changes(entries: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, ...], ...]:
    """把变更文件分成**确定性**的簇（同一份输入永远得到同一份分簇）。

    规则（按这个顺序，先到先得）：

    1. **同源对**并簇：同目录同主名的「表 ↔ 生成物」（`item.xlsx` 与 `item.lua`）；
    2. 剩下的按 `_cluster_key`（提交 + 顶层目录）分组；
    3. 每组内部按路径排序，组之间按键排序 —— 顺序稳定，「同一份配置永远得到同一套分工」
       （与 `subagent.group_dimensions` 同一条契约）。

    空输入回空元组（调用方据此走单代理）。
    """
    items = [item for item in entries if str(item.get("path") or "").strip()]
    if not items:
        return ()

    # ---- 1. 同源对：同目录同主名的「表 ↔ 生成物」 ----
    by_stem: dict[tuple[str, str], list[str]] = {}
    for item in items:
        path = str(item["path"])
        parent = str(PurePosixPath(path).parent)
        stem = PurePosixPath(path).stem
        by_stem.setdefault((parent, stem), []).append(path)

    merged: dict[str, set[str]] = {}
    claimed: set[str] = set()
    for (parent, stem), paths in by_stem.items():
        suffixes = {PurePosixPath(path).suffix.lower() for path in paths}
        table_like = bool(suffixes & _TABLE_SUFFIXES)
        source_like = bool(suffixes & _GENERATED_SUFFIXES)
        if not (table_like and source_like and len(paths) > 1):
            continue
        key = f"{parent}/{stem}"
        bucket = merged.setdefault(key, set())
        for path in paths:
            bucket.add(path)
            claimed.add(path)

    # ---- 2. 提交 + 顶层目录 ----
    groups: dict[tuple[str, str], set[str]] = {}
    for item in items:
        path = str(item["path"])
        if path in claimed:
            continue
        groups.setdefault(_cluster_key(item), set()).add(path)

    ordered: list[tuple[str, ...]] = []
    for key in sorted(merged):
        ordered.append(tuple(sorted(merged[key])))
    for key in sorted(groups):
        bucket = groups[key]
        if bucket:
            ordered.append(tuple(sorted(bucket)))
    return tuple(ordered)


def cluster_chars(
    clusters: Sequence[Sequence[str]], entries: Sequence[Mapping[str, Any]]
) -> tuple[int, ...]:
    """每个簇的证据体积（字符）= 簇内文件的 diff 字符数之和。

    `entries` 里没有字符数那一项时（老 payload / 抽样之外）回 0 —— 那时成员的额度由
    「按文件数摊」兜底（见 `plan_analysis`），而不是当成「这一簇不用读」。
    """
    sizes = {str(item.get("path")): max(0, _int(item.get("chars"))) for item in entries}
    return tuple(sum(sizes.get(path, 0) for path in group) for group in clusters)


def should_consult_planner(facts: SnapshotFacts) -> bool:
    """**才允许**那 1 次受限规划模型调用的两种情形。

    规划调用本身是一次付费模型请求，所以它必须有一个判据、而不是默认走。两种情形：
    * 簇太多（> `PLANNER_MAX_CLUSTERS`）：确定性规则分得太碎，谁该和谁一起看需要判断；
    * **归属不清**：有文件没有提交号（引用/补偿项）—— 「提交 + 目录」这条键对它失效，
      确定性分组只能把它塞进一个目录簇里，而它可能与那一簇毫无关系。
    """
    clusters = cluster_changes(facts.entries)
    if len(clusters) > PLANNER_MAX_CLUSTERS:
        return True
    return any(not str(item.get("commit") or "").strip() for item in facts.entries)


def planner_metadata(facts: SnapshotFacts) -> dict[str, Any]:
    """规划调用的**全部**输入（元数据、diff 摘要与关联图，**不含任何整表/正文**）。

    这一份是要被断言的东西：它是「不输入整表」那句话的落地形态。里面只有路径、提交、
    后缀、体量与分簇 —— `_entry` 已经把正文挡在外面了，这里再显式列一遍键名，
    免得将来有人顺手把 `merged_diff_data` 塞进来。
    """
    clusters = cluster_changes(facts.entries)
    sizes = cluster_chars(clusters, facts.entries)
    return {
        "file_count": facts.file_count,
        "diff_payload_chars": facts.diff_payload_chars,
        "max_file_diff_chars": facts.max_file_diff_chars,
        "truncated": facts.truncated,
        "chars_estimated": facts.chars_estimated,
        "files": [
            {
                # **只用这四个键**：路径、提交、类型、体量。正文与整表都不在这里。
                "path": item["path"],
                "commit": item["commit"],
                "suffix": item["suffix"],
                "chars": item["chars"],
            }
            for item in facts.entries
        ],
        "clusters": [
            {"id": f"c{index}", "paths": list(group), "chars": sizes[index - 1]}
            for index, group in enumerate(clusters, start=1)
        ],
        "contract": PLANNER_JSON_CONTRACT,
        "note": (
            "只给元数据与分簇：路径/提交/类型/体量。**没有**表格正文、没有整表 dump —— "
            "需要正文的推理在分片自己的轮次里做。"
        ),
    }


@dataclass(frozen=True)
class GroupingValidation:
    """规划模型那份 JSON 的校验结果。"""

    accepted: bool
    reason: str
    problems: tuple[str, ...] = ()


def validate_grouping(
    proposal: Any,
    facts: SnapshotFacts,
    *,
    dimensions: Sequence[str] = (),
    max_members: int = FAMILY_MAX_MEMBERS,
) -> GroupingValidation:
    """校验规划模型给的分组建议。**任何一条不过就整份退回确定性分组。**

    六条判据（指引 §3.B 逐条对应）：

    1. 形状：`groups` 是非空列表，每组有 `id` / `paths` / `objective` / `qa_dimensions`；
    2. **路径全在快照内**（越权路径 = 它想读一个不属于这次的输入 —— 最要紧的一条）；
    3. 每个文件**至少被分配一次**（漏掉的文件就是没人看，而且报告里不会说）；
    4. `shared_paths` 也必须在快照内（关键依赖可共享，但不能凭空多出文件）；
    5. 分片**不空**（空组 = 白花一次整份提示词的钱）；
    6. 总数不超上限。

    `dimensions` 只用来挡「编出来的维度名」—— 一个不存在的维度进了任务书，模型会去
    「检查」一项本项目根本没有的清单。
    """
    problems: list[str] = []
    data = proposal if isinstance(proposal, Mapping) else {}
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, (list, tuple)) or not raw_groups:
        return GroupingValidation(False, "返回里没有 groups 列表", ("groups 缺失或为空",))

    known_paths = {str(item.get("path")) for item in facts.entries}
    known_dims = {str(item).strip() for item in dimensions if str(item or "").strip()}
    seen: set[str] = set()
    for position, row in enumerate(raw_groups, start=1):
        group = row if isinstance(row, Mapping) else {}
        if not str(group.get("id") or "").strip():
            problems.append(f"第 {position} 组没有 id")
        paths = group.get("paths")
        if not isinstance(paths, (list, tuple)) or not paths:
            problems.append(f"第 {position} 组没有 paths（空组只会白花一次整份提示词的钱）")
            continue
        # **目标**是分工的意义所在，空目标等于「这一组看什么都行」。
        if not str(group.get("objective") or "").strip():
            problems.append(f"第 {position} 组没有 objective")
        for path in paths:
            text = str(path or "").strip()
            if text not in known_paths:
                problems.append(f"第 {position} 组里有不在本次快照内的路径：{text!r}")
            else:
                seen.add(text)
        dims = group.get("qa_dimensions")
        for name in dims if isinstance(dims, (list, tuple)) else ():
            if known_dims and str(name).strip() not in known_dims:
                problems.append(f"第 {position} 组点名了一个不存在的维度：{name!r}")

    for path in data.get("shared_paths") or ():
        if str(path or "").strip() not in known_paths:
            problems.append(f"shared_paths 里有不在本次快照内的路径：{path!r}")

    missing = sorted(known_paths - seen)
    if missing:
        problems.append(
            f"{len(missing)} 个文件没有被分配进任何组（例如 {missing[0]!r}）——"
            "漏掉的文件就是没人看，而且报告里不会说"
        )
    if len(raw_groups) > max_members:
        problems.append(f"分了 {len(raw_groups)} 组，超过上限 {max_members} 组")

    if problems:
        return GroupingValidation(False, "建议没有通过平台校验，退回确定性分组", tuple(problems))
    return GroupingValidation(True, "按规划建议分组")


def groups_from_proposal(proposal: Any) -> tuple[tuple[str, ...], ...]:
    """把**已通过校验**的建议折成分簇（保持它给的顺序）。"""
    data = proposal if isinstance(proposal, Mapping) else {}
    result: list[tuple[str, ...]] = []
    for row in data.get("groups") or ():
        group = row if isinstance(row, Mapping) else {}
        paths = tuple(
            str(path).strip() for path in (group.get("paths") or ()) if str(path or "").strip()
        )
        if paths:
            result.append(paths)
    return tuple(result)


# ==========================================================================
# 计划
# ==========================================================================

#: 计划的两种模式。`single` 走 `engine.run_analysis`（单代理）；`family` 走
#: `subagent.run_family`（n 个分片 + 汇总 + 可选对账）。
MODE_SINGLE = "single"
MODE_FAMILY = "family"


@dataclass(frozen=True)
class PlanMember:
    """计划里的一个成员（分片 / 汇总 / 单代理那一个分析者）。"""

    index: int
    label: str
    role: str
    paths: tuple[str, ...]
    objective: str
    qa_dimensions: tuple[str, ...]
    max_rounds: int
    max_tool_requests: int
    max_output_tokens: int

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["paths"] = list(self.paths)
        payload["qa_dimensions"] = list(self.qa_dimensions)
        return payload


@dataclass(frozen=True)
class AnalysisPlan:
    """**一份**已定下来的计划：运行侧与预估端点读同一份，不各推一次。

    ## 它至少落什么（指引 §3.B 逐项）

    `plan_version` / 快照指纹 / 模式 / 分组（每个成员的路径与目标）/ 每成员轮次·索取·
    输出上限 / 总 token 上限 / 预留给报告收尾的额度 / 原因（`reason` + `thresholds`）。
    这些进 `payload["plan"]`（落进 `run.request_payload`）与预估端点的返回，两边逐字相同。

    ## 为什么 `family` 里嵌一个 `FamilySizing`

    `subagent.plan_family` / `FamilyQuota` 已经按 `FamilySizing` 这套字段工作（串行共享
    池、汇总保底都在那边）。计划不另造一套平行结构，而是把这几个数**算好放在这里** ——
    否则「计划说 3 片、家族跑了 5 片」会成为一种只体现在账目上的分叉。
    """

    plan_version: str
    snapshot_fingerprint: str
    mode: str
    members: tuple[PlanMember, ...]
    dimensions: tuple[str, ...]
    clusters: tuple[dict, ...]
    shared_paths: tuple[str, ...]
    total_token_budget: int
    report_reserve_tokens: int
    round_reserve_tokens: int
    reason: str
    thresholds: dict
    estimate: dict
    planner: dict
    facts: dict
    family: Optional[FamilySizing] = None
    synthesis: Optional[PlanMember] = None

    @property
    def is_single(self) -> bool:
        return self.mode == MODE_SINGLE

    @property
    def member_count(self) -> int:
        """**分片**数（不含汇总/对账）—— `plan_family` 的 `count` 要的就是它。"""
        return len(self.members)

    def covered_dimensions(self) -> tuple[str, ...]:
        """全部成员覆盖到的维度（去重、保持清单顺序）。"""
        seen: list[str] = []
        for member in (*self.members, self.synthesis):
            if member is None:
                continue
            for name in member.qa_dimensions:
                if name not in seen:
                    seen.append(name)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "mode": self.mode,
            "members": [item.to_dict() for item in self.members],
            "synthesis": self.synthesis.to_dict() if self.synthesis is not None else None,
            "dimensions": list(self.dimensions),
            "clusters": [dict(item) for item in self.clusters],
            "shared_paths": list(self.shared_paths),
            "total_token_budget": self.total_token_budget,
            "report_reserve_tokens": self.report_reserve_tokens,
            "round_reserve_tokens": self.round_reserve_tokens,
            "reason": self.reason,
            "thresholds": dict(self.thresholds),
            "estimate": dict(self.estimate),
            "planner": dict(self.planner),
            "facts": dict(self.facts),
            "family": asdict(self.family) if self.family is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["AnalysisPlan"]:
        """落库的那一份 → 计划对象。**坏数据回 `None`**（调用方退回「现算一份」）。"""
        data = raw if isinstance(raw, Mapping) else None
        if not data or not str(data.get("mode") or "").strip():
            return None

        def _member(row: Any) -> Optional[PlanMember]:
            item = row if isinstance(row, Mapping) else None
            if not item:
                return None
            return PlanMember(
                index=_int(item.get("index")),
                label=str(item.get("label") or ""),
                role=str(item.get("role") or ""),
                paths=tuple(str(path) for path in (item.get("paths") or ())),
                objective=str(item.get("objective") or ""),
                qa_dimensions=tuple(str(name) for name in (item.get("qa_dimensions") or ())),
                max_rounds=_int(item.get("max_rounds")),
                max_tool_requests=_int(item.get("max_tool_requests")),
                max_output_tokens=_int(item.get("max_output_tokens")),
            )

        members = tuple(
            member for member in (_member(row) for row in (data.get("members") or ())) if member
        )
        family_raw = data.get("family") if isinstance(data.get("family"), Mapping) else None
        family = None
        if family_raw:
            family = FamilySizing(
                shard_count=_int(family_raw.get("shard_count")),
                requests_per_shard=_int(family_raw.get("requests_per_shard")),
                rounds_per_shard=_int(family_raw.get("rounds_per_shard")),
                family_requests_pool=_int(family_raw.get("family_requests_pool")),
                family_rounds_pool=_int(family_raw.get("family_rounds_pool")),
                sampling_cap=_int(family_raw.get("sampling_cap")),
                anomalies_per_subagent=_int(family_raw.get("anomalies_per_subagent")),
                note=str(family_raw.get("note") or ""),
            )
        return cls(
            plan_version=str(data.get("plan_version") or ""),
            snapshot_fingerprint=str(data.get("snapshot_fingerprint") or ""),
            mode=str(data.get("mode")),
            members=members,
            dimensions=tuple(str(name) for name in (data.get("dimensions") or ())),
            clusters=tuple(
                dict(item) for item in (data.get("clusters") or ()) if isinstance(item, Mapping)
            ),
            shared_paths=tuple(str(path) for path in (data.get("shared_paths") or ())),
            total_token_budget=_int(data.get("total_token_budget")),
            report_reserve_tokens=_int(data.get("report_reserve_tokens")),
            round_reserve_tokens=_int(data.get("round_reserve_tokens")),
            reason=str(data.get("reason") or ""),
            thresholds=dict(data.get("thresholds") or {}),
            estimate=dict(data.get("estimate") or {}),
            planner=dict(data.get("planner") or {}),
            facts=dict(data.get("facts") or {}),
            family=family,
            synthesis=_member(data.get("synthesis")),
        )


# ==========================================================================
# 口径版本（进 analysis_revision 的三样）
# ==========================================================================

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 「diff/快照口径」的源码范围。变了就等于**模型看到的差异本身换了口径**（窗口怎么切、
#: 表怎么按文本读、hunk 怎么截断），旧结论与新结论不可比。
_DIFF_SNAPSHOT_SOURCES = (
    "commit_diff_logic.py",
    "utils/content_window.py",
    "services/ai/stored_diff_source.py",
    "services/ai/snapshot_store.py",
)

#: 「证据协议」的源码范围：模型怎么提请求、平台怎么执行与记账（白名单、证据 id、
#: 截断标记、逐轮账本）。它变了，同一份输入产出的结论形态也会变。
_EVIDENCE_PROTOCOL_SOURCES = (
    "services/ai/protocol.py",
    "services/ai/evidence_store.py",
    "services/ai/context_tools.py",
)


def _sources_digest(names: Sequence[str], *, prefix: str) -> str:
    """一组源码文件的短哈希（`rules_version` / `prompt_version` 同一手法）。

    读不到文件（打包成 zipapp / 冻结分发）时退化成「只认文件名」：同一个构建里仍然稳定，
    只是跨构建不再区分 —— 比抛异常好（那会让**整次分析**在建 run 时失败）。
    """
    digest = hashlib.sha1()
    for name in names:
        digest.update(name.encode("utf-8"))
        try:
            digest.update((_REPO_ROOT / name).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return f"{prefix}-{digest.hexdigest()[:12]}"


def diff_snapshot_revision() -> str:
    """模型看到的**差异**是用哪一版口径算/渲染的。"""
    return _sources_digest(_DIFF_SNAPSHOT_SOURCES, prefix="diff")


def evidence_protocol_revision() -> str:
    """「请求 → 执行 → 证据」这套协议是哪一版。"""
    return _sources_digest(_EVIDENCE_PROTOCOL_SOURCES, prefix="ev")


def compose_analysis_revision(thresholds_component: str) -> str:
    """`analysis_revision` 列的取值：**门槛 + 口径四件套**（指引 §3.B 第 4 条）。

    口径四件套 = diff/快照（`diff_snapshot_revision`）、提示词（`prompt_version`，
    在上面的溯源里是独立一列）、证据协议（`evidence_protocol_revision`）、
    规划版本（`PLAN_VERSION`）。它回答的是「这份结论是在哪套尺子下量出来的」。

    ## 为什么后面三样折成一个短哈希，而不是各占一段

    这一列是 `VARCHAR(80)`。门槛成分本身最长可以到 50 字（`sev=…;conf=…;cap=…;ev=…;sim=…`，
    而 `sim` 是个浮点数，位数不定），再并排写三样就会溢出 —— 而溢出在 SQLite 上**不报错**
    （静默截断），在 MySQL 上直接报错，两种后端的行为还不一样。所以：

    * 门槛成分**原样保留**（它是「用户改了配置」与「平台改了代码」这两类变化里唯一
      能从字面读出来的那一类，也是 `_is_run_fresh` 的既有读者看得懂的那一段）；
    * 另外三样折成 12 位十六进制（48 bit，碰撞概率在「一个项目几十次分析」的量级上
      可以忽略），前面带一个 `x=` 说明它**只覆盖这三样**。

    结果长度 ≈ 65~70 字，留在 80 以内；真的超了就在末尾截断（那种门槛配置本身已经
    非法，`RuleThresholds.from_config` 会先抛）。**读侧与写侧调的是同一个函数**
    （`provenance.current_provenance`），所以「预估说口径没变、运行时又变了」不可能发生。
    """
    extras = hashlib.sha1(
        "|".join(
            (PLAN_VERSION, diff_snapshot_revision(), evidence_protocol_revision())
        ).encode("utf-8")
    ).hexdigest()[:12]
    base = str(thresholds_component or "").strip() or "unknown"
    # 列宽 80：见 docstring 的说明，正常配置下远用不到这个截断。
    return f"{base};x={extras}"[:80]


# ==========================================================================
# 计划推导
# ==========================================================================

def _clamp_member_requests(evidence_chars: int, ceiling: int) -> int:
    """一簇的证据体积 → 该成员的名义索取次数，且**不超过预算给出的上限**。

    这里就是那条被改掉的因果：以前是「预算 ÷ 11k = 索取次数」（于是 3 个文件也开 50 次），
    现在「这一簇有多少字要读 = 索取次数」，预算只负责**封顶**。
    """
    wanted = -(-max(0, int(evidence_chars)) // ITEM_CHARS) + DISCOVERY_REQUESTS
    return _clamp(min(max(0, int(ceiling)), wanted), REQUESTS_FLOOR_PER_SHARD, REQUESTS_CEILING_PER_SHARD)


def _member_rounds(requests: int, ceiling: int) -> int:
    """索取次数 → 轮次（一轮大约吃掉 `REQUESTS_PER_ROUND` 次），同样只被 `ceiling` 封顶。"""
    wanted = -(-max(1, int(requests)) // REQUESTS_PER_ROUND)
    return _clamp(min(max(1, int(ceiling)), wanted), 2, ROUNDS_PER_SHARD)


def _output_cap_for(mode: str, output_tokens: Optional[int]) -> int:
    """单次输出的名义上限（token）。用户配了就用他的，否则用平台的保守值。

    它**会进计划**，是「本轮保守预留」那个算式里的输出部分；这一版**不改真实请求体**
    （`EngineLimits.max_output_tokens` 保持 `None` = 用端点默认值）—— 收紧每一个角色的
    `max_output_tokens` 是工作包 D 的压缩项，要先在金标上量过召回，B 只负责把它算清、
    记全。
    """
    if output_tokens and int(output_tokens) > 0:
        return int(output_tokens)
    # 保守值：run 45 各阶段输出 24k~52k token，一次调用写不完那么多，但预留按**一次调用
    # 可能写的上限**算 —— 8,000 与 `EngineLimits.max_output_tokens` 的注释里那个量级一致。
    return 8_000


def _round_reserve(requests_per_member: int, rounds_per_member: int, *, list_chars: int, output_cap: int) -> int:
    """**本轮**的保守预留（token）：下一次模型调用最多可能吃掉多少。

    口径 = 平台内置提示词 + 变更清单 + 历史结论基线 + 本轮要带进去的若干条上下文 +
    输出上限。**字符 ≈ token** 是平台自己的换算口径（1M 窗口 → 水位 600,000 **字**，
    见 `budget.COMPACT_AT_RATIO` 与 `models/ai_analysis/project_config.py` 的推导），
    所以这里不做任何额外系数。

    为什么不用「提示词字符预算」当预留：那是**上限**（580k），而实测调用只有 19k~40k
    token 一轮（run 46：76,058 输入 / 4 轮）。按上限预留会把小计划的 150 万上限变成
    「只够两轮」，闸门天天误伤。这里按「这一轮**真的会**带多少」算，`DISCOVERY` 之外的
    次数就是它的依据。
    """
    rounds = max(1, int(rounds_per_member))
    per_round_items = max(1, -(-max(1, int(requests_per_member)) // rounds))
    return int(
        PLATFORM_OVERHEAD_RESERVE_CHARS
        + max(0, int(list_chars))
        + BASELINE_CHARS
        + per_round_items * ITEM_CHARS
        + max(0, int(output_cap))
    )


def _dimension_groups(dimensions: Sequence[str], count: int) -> tuple[tuple[str, ...], ...]:
    """把维度清单按**相邻顺序**均分成 `count` 组（`subagent.group_dimensions` 的同形规则）。

    维度在这里是**检查清单**：每组的成员都要认领自己那一组，而**所有成员合起来必须覆盖
    全部维度**（`AnalysisPlan.covered_dimensions` 断言这一点）。原先那条「把 9 个维度
    机械均分成 5 个角色」的错误不是「分了维度」，而是**用维度的条数决定分片数**。
    """
    items = tuple(str(item).strip() for item in dimensions if str(item or "").strip())
    if not items:
        return ()
    wanted = max(1, min(int(count), len(items)))
    base, extra = divmod(len(items), wanted)
    groups: list[tuple[str, ...]] = []
    start = 0
    for position in range(wanted):
        size = base + (1 if position < extra else 0)
        groups.append(items[start : start + size])
        start += size
    return tuple(groups)


def _merge_groups(groups: Sequence[Sequence[str]], count: int) -> tuple[tuple[str, ...], ...]:
    """把 `len(groups)` 个簇**连续地**并成 `count` 份（确定性，簇不拆开）。

    簇不能被拆：一个变更簇之所以是簇，是因为它们**要一起看**。簇比成员多时并簇、
    比成员少时成员数就取簇数（每个成员一个簇）。
    """
    total = len(groups)
    if total == 0:
        return ()
    wanted = max(1, min(int(count), total))
    size, extra = divmod(total, wanted)
    merged: list[tuple[str, ...]] = []
    start = 0
    for position in range(wanted):
        take = size + (1 if position < extra else 0)
        bucket: list[str] = []
        for group in groups[start : start + take]:
            bucket.extend(group)
        merged.append(tuple(bucket))
        start += take
    return tuple(merged)


def plan_analysis(
    snapshot_facts: "SnapshotFacts | Mapping[str, Any] | None",
    effective_budget: Any,
    dimensions: Sequence[str] = (),
    *,
    subagent_enabled: bool = True,
    verify: bool = False,
    proposal: Any = None,
    output_tokens: Optional[int] = None,
    cost_limit: Optional[str] = None,
) -> AnalysisPlan:
    """**唯一的**规模/分工判定：快照事实 → 一份计划。纯函数。

    参数：

    * `snapshot_facts`：`SnapshotFacts` 或它的 Mapping 形态（见那个类的字段）；
    * `effective_budget`：用户内容字符额度（int），或 Mapping（键 `user_chars` /
      `output_tokens` / `single_run_token_limit`）。**它只是上限**；
    * `dimensions`：本次生效的检查维度清单（合起来必须被覆盖完）；
    * `subagent_enabled`：**显式关掉时强制单代理** —— 自动规划不许把它打开
      （指引 §3.B：`DEFAULT_SUBAGENT_ENABLED` 改 False 之后，开不开由管理员说了算）；
    * `proposal`：**已经拿到**的那一份规划模型建议（最多 1 次调用，由调用方发起）。
      没通过 `validate_grouping` 就退回确定性分组，并把原因写进计划的 `planner` 栏。

    返回的计划里每一项都能回答「为什么是这个数」（`reason` + `thresholds`）。
    """
    facts = SnapshotFacts.from_mapping(snapshot_facts)
    budget = dict(effective_budget) if isinstance(effective_budget, Mapping) else {
        "user_chars": effective_budget
    }
    user_chars = max(0, _int(budget.get("user_chars")))
    output_cap = _output_cap_for("", output_tokens if output_tokens is not None else budget.get("output_tokens"))
    user_cap_tokens = _int(budget.get("single_run_token_limit")) or None
    # 「单次硬上限**用户可以改**」的现成入口：项目配置里那个**周期** token 上限
    # （`budget_token_limit`，真实存在、管理员能改的一列；留空时解析成 100M/月）。
    # 它管的是「这个周期一共能花多少」，在单次这一侧只做**粗界**：单次当然不许超过
    # 整个周期的上限（否则会出现「单次允许 8M、这个月只允许 2M」这种自相矛盾的计划）。
    # 「这个周期**还剩**多少」是月度闸门（`analysis_budget.early_stop_guard`）的事，
    # 不在这里重复算 —— 两处都算必然分叉。周期上限**比出厂默认更松时不做任何事**。
    period_cap_tokens = _int(budget.get("period_token_limit")) or None

    dims = tuple(str(item).strip() for item in dimensions if str(item or "").strip())
    list_chars = sampling_cap_for(facts.file_count) * LIST_CHARS_PER_FILE

    # `derive_family_sizing` 仍然是「预算 → 每个成员能有多少额度」的唯一事实源，
    # 但它在这里的角色**降级为上限**：真正决定每片索取/轮次的是簇的证据体积。
    ceiling = derive_family_sizing(
        effective_user_chars=user_chars,
        file_count=facts.file_count,
        dimension_count=len(dims) or 9,
    )

    # ---- 第一道门槛：小批次直接单分析者 ----
    #
    # `exceeded` **为空**才是小批次（它是「没进门槛的原因」，空串 = 三条都过）。
    # 判据写成「没超」而不是「超了」：把两边写反的后果是两个方向同时错
    # （3 个文件开 5 片、847 个文件走单代理），而它们都不会报错。
    exceeded = facts.small_batch_exceeded_by
    forced_single = not subagent_enabled
    if forced_single or not exceeded:
        scale = exceeded or (
            f"变更文件 {facts.file_count} 个、差异载荷估算 {facts.diff_payload_chars:,} 字、"
            f"单文件最大 {facts.max_file_diff_chars:,} 字，三条都在门槛内"
        )
        reason = (
            f"管理员关闭了子代理 → **强制**单代理（自动规划不会把它打开）；规模判定：{scale}"
            if forced_single
            else f"小批次（{scale}）→ 单分析者；它仍然覆盖全部声明维度"
        )
        return _single_plan(
            facts=facts,
            dims=dims,
            user_chars=user_chars,
            output_cap=output_cap,
            user_cap_tokens=user_cap_tokens,
        period_cap_tokens=period_cap_tokens,
            cost_limit=cost_limit,
            reason=reason,
            thresholds_extra={"small_batch_exceeded_by": exceeded, "forced_single": forced_single},
            # 小批次这一档额度就是出厂默认（8 轮 / 40 次）；`ceiling` 只在
            # 「大版本却分不出簇」那一支用得上，见 `_single_plan` 的说明。
            ceiling=ceiling,
        )

    # ---- 第二道：确定性变更簇 →（必要时）1 次受限规划 → 校验 ----
    clusters = cluster_changes(facts.entries)
    # 规划建议里每个组的 `objective` / `qa_dimensions` 是**它给出的分工理由**，
    # 平台校验通过之后要真的用上（只用它的分簇、丢掉它的目标是白问一次模型）。
    declared: list[tuple[frozenset, str, tuple[str, ...]]] = []
    planner: dict = {"consulted": False, "accepted": False, "problems": [], "reason": ""}
    if proposal is not None:
        # 调用方已经把建议拿回来了（它才是发起那次调用的地方 —— 本函数是纯函数，
        # 不联网）。**仍然在这里校验**，因为「模型给的分组能不能用」是计划的判据。
        planner["consulted"] = True
        validation = validate_grouping(proposal, facts, dimensions=dims)
        planner["accepted"] = validation.accepted
        planner["reason"] = validation.reason
        planner["problems"] = list(validation.problems)
        if validation.accepted:
            clusters = groups_from_proposal(proposal)
            declared = [
                (
                    frozenset(str(path).strip() for path in (row.get("paths") or ())),
                    str(row.get("objective") or "").strip(),
                    tuple(str(name) for name in (row.get("qa_dimensions") or ())),
                )
                for row in (proposal.get("groups") or ())
                if isinstance(row, Mapping)
            ]

    # 成员数由**证据体积与独立簇数**推导：既不是维度数，也不是预算反解出来的片数。
    needed = max(
        FAMILY_MIN_MEMBERS,
        -(-max(1, facts.diff_payload_chars) // MEMBER_EVIDENCE_CHARS),
    )
    # **`ceiling.shard_count` 刻意不在这个 min 里。** 它来自 `derive_family_sizing`，
    # 而那个函数把分片数按**维度数**夹过一次（`min(目标, max(2, 维度数))`）—— 那正是指引
    # 明令去掉的「把 N 个维度机械均分成 N 个角色」那根耦合：一个只声明 3 个维度的项目，
    # 847 个文件也只会开 3 个成员（实测：同一份 847 文件的输入，5 个维度给 5 个成员、
    # 3 个维度给 3 个成员 —— 上一版就是从这里漏进来的）。
    #
    # 成员是**按变更簇切出来的工作量**，每个成员都要覆盖**全部**维度清单，所以维度数
    # 不是它的上界。真正的上界是 `len(clusters)`（没东西可分就不拆）与
    # `FAMILY_MAX_MEMBERS`。`ceiling` 仍然管每个成员**能分到多少额度**（下面用到）。
    member_count = _clamp(
        min(needed, len(clusters), FAMILY_MAX_MEMBERS),
        FAMILY_MIN_MEMBERS,
        FAMILY_MAX_MEMBERS,
    )
    merged = _merge_groups(clusters, member_count)
    if len(merged) < FAMILY_MIN_MEMBERS:
        # **只有一个变更簇**：没有可分的东西（一个簇之所以是簇，就是它们要一起看 ——
        # 把同一张表和它的生成物分给两个人，两边都只看得到一半的改动）。大版本也只好
        # 一个分析者看完，这是**明确的**不拆，不是漏判，所以写进 reason。
        return _single_plan(
            facts=facts,
            dims=dims,
            user_chars=user_chars,
            output_cap=output_cap,
            user_cap_tokens=user_cap_tokens,
        period_cap_tokens=period_cap_tokens,
            cost_limit=cost_limit,
            reason=(
                f"超过小批次门槛（{exceeded}），但确定性分簇只给出 {len(merged)} 个变更簇"
                "—— 没有可分的东西（同一簇要一起看），本次仍由单分析者看完"
            ),
            thresholds_extra={
                "small_batch_exceeded_by": exceeded,
                "forced_single": False,
                "clusters": len(merged),
            },
            ceiling=ceiling,
        )

    dim_groups = _dimension_groups(dims, member_count)
    # **体量未知时按上限配，不按下限配。** 「量不出这一簇有多少字」不等于「这一簇很小」：
    # 按 0 算会让每一片都落到 8 次索取的下限，而真正的大版本会在跑到一半时才发现不够
    # （那时代价已经付了）。未知一律走保守侧 —— 与「未上报的 token 不许当 0」同一条纪律。
    unknown_volume = facts.diff_payload_chars <= 0 and facts.file_count > 0
    members: list[PlanMember] = []
    for position, paths in enumerate(merged, start=1):
        evidence = sum(
            max(0, _int(item.get("chars"))) for item in facts.entries if item.get("path") in set(paths)
        )
        if evidence <= 0:
            # 拿不到字符数（老 payload / 抽样之外）时**按文件数摊**，不当作「这一簇不用读」。
            evidence = len(paths) * (facts.diff_payload_chars // max(1, facts.file_count))
        requests = (
            _clamp(ceiling.requests_per_shard, REQUESTS_FLOOR_PER_SHARD, REQUESTS_CEILING_PER_SHARD)
            if unknown_volume
            else _clamp_member_requests(evidence, ceiling.requests_per_shard)
        )
        rounds = _member_rounds(requests, ceiling.rounds_per_shard)
        own = set(paths)
        # 规划建议里那一组的 `objective` / `qa_dimensions`：并簇之后一个成员可能覆盖
        # 好几组，按顺序拼起来；平台自己的兜底目标只在**没有**任何声明时用。
        declared_here = [item for item in declared if item[0] <= own]
        objective = "；".join(item[1] for item in declared_here if item[1]) or (
            f"负责第 {position} 个变更簇（{len(paths)} 个文件，"
            f"≈{evidence:,} 字差异）：逐条核对该簇的改动与它的跨文件影响"
        )
        declared_dims = tuple(
            name for item in declared_here for name in item[2] if name in set(dims)
        )
        members.append(
            PlanMember(
                index=position,
                label=f"S{position}",
                role="shard",
                paths=tuple(paths),
                objective=objective,
                # 规划建议点名了维度就用它（校验已经挡掉「编出来的维度」），否则按清单
                # 相邻顺序均分 —— 两条路的**并集**都必须覆盖全部维度，由
                # `AnalysisPlan.covered_dimensions` 与验收用例分别钉住。
                qa_dimensions=declared_dims
                or (dim_groups[position - 1] if position - 1 < len(dim_groups) else ()),
                max_rounds=rounds,
                max_tool_requests=requests,
                max_output_tokens=output_cap,
            )
        )

    requests_nominal = max((item.max_tool_requests for item in members), default=0)
    rounds_nominal = max((item.max_rounds for item in members), default=0)
    family = FamilySizing(
        shard_count=len(members),
        requests_per_shard=requests_nominal,
        rounds_per_shard=rounds_nominal,
        family_requests_pool=sum(item.max_tool_requests for item in members),
        family_rounds_pool=sum(item.max_rounds for item in members),
        sampling_cap=sampling_cap_for(facts.file_count),
        anomalies_per_subagent=ceiling.anomalies_per_subagent,
        note=(
            f"按**快照事实**分工：{facts.file_count} 个变更文件、差异载荷估算 "
            f"{facts.diff_payload_chars:,} 字"
            + ("（体量为抽样估算）" if facts.chars_estimated else "")
            + f"、单文件最大 {facts.max_file_diff_chars:,} 字 → {len(members)} 个变更簇；"
            f"每片的名义额由**本簇证据体积**推导（每片 {requests_nominal} 次索取 / "
            f"{rounds_nominal} 轮，预算给出的上限是 {ceiling.requests_per_shard} 次 ——"
            "预算只是上限，不是用满目标）；"
            f"家族共享池 {sum(item.max_tool_requests for item in members)} 次索取 / "
            f"{sum(item.max_rounds for item in members)} 轮，串行滚动、前片用剩的滚给后片。"
        ),
    )
    synthesis = PlanMember(
        index=len(members) + 1,
        label="",
        role="synthesis",
        paths=(),
        objective="汇总各分片的结论、按责任表补齐未覆盖维度、写唯一一份报告",
        qa_dimensions=dims,
        max_rounds=_clamp(ceiling.rounds_per_shard, 1, ROUNDS_PER_SHARD),
        max_tool_requests=_clamp(ceiling.requests_per_shard, 1, REQUESTS_CEILING_PER_SHARD),
        max_output_tokens=output_cap,
    )
    return _assemble_plan(
        facts=facts,
        mode=MODE_FAMILY,
        members=tuple(members),
        synthesis=synthesis,
        dims=dims,
        clusters=clusters,
        shared_paths=(),
        family=family,
        planner=planner,
        user_chars=user_chars,
        output_cap=output_cap,
        user_cap_tokens=user_cap_tokens,
        period_cap_tokens=period_cap_tokens,
        cost_limit=cost_limit,
        list_chars=list_chars,
        reason=(
            f"超过小批次门槛（{exceeded}）→ 按 {len(members)} 个变更簇分工；"
            f"每片额度由该簇证据体积推导，预算（{ceiling.requests_per_shard} 次/片）只作上限"
        ),
        thresholds_extra={"small_batch_exceeded_by": exceeded, "forced_single": False},
    )


def _single_plan(
    *,
    facts: SnapshotFacts,
    dims: Sequence[str],
    user_chars: int,
    output_cap: int,
    user_cap_tokens: Optional[int],
    period_cap_tokens: Optional[int],
    cost_limit: Optional[str],
    reason: str,
    thresholds_extra: Mapping[str, Any],
    ceiling: Optional[FamilySizing] = None,
) -> AnalysisPlan:
    """单分析者的计划：**一个**成员，覆盖**全部**声明维度。

    ## 额度：小批次用出厂默认，**大版本但分不出簇**时按预算上限抬上去

    「一个变更簇」不等于「很小」：800 个文件同属一次提交、同一个顶层目录时确定性分簇
    只给出 1 个簇，那时也只能一个分析者看完 —— 但它要读的东西比 3 个文件多两个数量级。
    给 8 轮 / 40 次会当场饿死（报告里只能写「额度用尽」），所以要按预算允许的上限抬。
    """
    paths = tuple(str(item.get("path")) for item in facts.entries)
    if facts.is_small_batch or ceiling is None:
        rounds, requests = SINGLE_AGENT_ROUNDS, SINGLE_AGENT_REQUESTS
    else:
        rounds = max(SINGLE_AGENT_ROUNDS, int(ceiling.rounds_per_shard))
        requests = max(SINGLE_AGENT_REQUESTS, int(ceiling.requests_per_shard))
    member = PlanMember(
        index=1,
        label="",
        role="single",
        paths=paths,
        objective="独立完成本次周版本的完整评审（规模在小批次门槛内，分工只有成本没有收益）",
        qa_dimensions=tuple(dims),
        max_rounds=rounds,
        max_tool_requests=requests,
        max_output_tokens=output_cap,
    )
    return _assemble_plan(
        facts=facts,
        mode=MODE_SINGLE,
        members=(member,),
        synthesis=None,
        dims=dims,
        clusters=cluster_changes(facts.entries),
        shared_paths=(),
        family=None,
        planner={"consulted": False, "accepted": False, "problems": [], "reason": ""},
        user_chars=user_chars,
        output_cap=output_cap,
        user_cap_tokens=user_cap_tokens,
        period_cap_tokens=period_cap_tokens,
        cost_limit=cost_limit,
        list_chars=sampling_cap_for(facts.file_count) * LIST_CHARS_PER_FILE,
        reason=reason,
        thresholds_extra=thresholds_extra,
    )


def _assemble_plan(
    *,
    facts: SnapshotFacts,
    mode: str,
    members: tuple[PlanMember, ...],
    synthesis: Optional[PlanMember],
    dims: Sequence[str],
    clusters: Sequence[Sequence[str]],
    shared_paths: Sequence[str],
    family: Optional[FamilySizing],
    planner: Mapping[str, Any],
    user_chars: int,
    output_cap: int,
    user_cap_tokens: Optional[int],
    period_cap_tokens: Optional[int],
    cost_limit: Optional[str],
    list_chars: int,
    reason: str,
    thresholds_extra: Mapping[str, Any],
) -> AnalysisPlan:
    """把已经定下来的数字装成一份计划（含单次硬上限与两个预留）。"""
    total_requests = sum(item.max_tool_requests for item in members) + (
        synthesis.max_tool_requests if synthesis is not None else 0
    )
    total_rounds = sum(item.max_rounds for item in members) + (
        synthesis.max_rounds if synthesis is not None else 0
    )
    nominal_requests = max((item.max_tool_requests for item in members), default=SINGLE_AGENT_REQUESTS)
    nominal_rounds = max((item.max_rounds for item in members), default=SINGLE_AGENT_ROUNDS)

    cap = int(user_cap_tokens or 0) or (
        SINGLE_RUN_TOKEN_CAP_SMALL if mode == MODE_SINGLE else SINGLE_RUN_TOKEN_CAP_LARGE
    )
    # 周期上限只做**收紧**（见 `plan_analysis` 里 `period_cap_tokens` 的说明）：
    # 它比上面这个数更紧时才是这一次的硬上限，更松时什么也不做 —— 否则「周期留空 =
    # 100M/月」会被当成「单次可以花 100M」，那正是这个 P0 要修的东西。
    cap_source = "用户配置" if user_cap_tokens else "平台初值"
    if period_cap_tokens and int(period_cap_tokens) < cap:
        cap = int(period_cap_tokens)
        cap_source = "周期上限（用户配置，收紧）"
    report_reserve = max(REPORT_RESERVE_FLOOR, int(cap * REPORT_RESERVE_RATIO))
    round_reserve = _round_reserve(
        nominal_requests, nominal_rounds, list_chars=list_chars, output_cap=output_cap
    )
    return AnalysisPlan(
        plan_version=PLAN_VERSION,
        snapshot_fingerprint=facts.fingerprint or facts.digest(),
        mode=mode,
        members=members,
        synthesis=synthesis,
        dimensions=tuple(dims),
        clusters=tuple({"paths": list(group), "chars": chars} for group, chars in zip(clusters, cluster_chars(clusters, facts.entries))),
        shared_paths=tuple(shared_paths),
        total_token_budget=cap,
        report_reserve_tokens=report_reserve,
        round_reserve_tokens=round_reserve,
        reason=reason,
        thresholds={
            "plan_version": PLAN_VERSION,
            "small_batch_max_files": SMALL_BATCH_MAX_FILES,
            "small_batch_max_total_chars": SMALL_BATCH_MAX_TOTAL_CHARS,
            "small_batch_max_file_chars": SMALL_BATCH_MAX_FILE_CHARS,
            "family_min_members": FAMILY_MIN_MEMBERS,
            "family_max_members": FAMILY_MAX_MEMBERS,
            "member_evidence_chars": MEMBER_EVIDENCE_CHARS,
            "discovery_requests": DISCOVERY_REQUESTS,
            "requests_per_round": REQUESTS_PER_ROUND,
            "item_chars": ITEM_CHARS,
            "list_chars_per_file": LIST_CHARS_PER_FILE,
            "single_agent_rounds": SINGLE_AGENT_ROUNDS,
            "single_agent_requests": SINGLE_AGENT_REQUESTS,
            "single_run_token_cap_small": SINGLE_RUN_TOKEN_CAP_SMALL,
            "single_run_token_cap_large": SINGLE_RUN_TOKEN_CAP_LARGE,
            "single_run_token_cap": cap,
            "single_run_token_cap_source": cap_source,
            "report_reserve_ratio": REPORT_RESERVE_RATIO,
            "report_reserve_floor": REPORT_RESERVE_FLOOR,
            "report_reserve_tokens": report_reserve,
            "round_reserve_tokens": round_reserve,
            "user_chars": user_chars,
            "cost_limit": str(cost_limit or ""),
            **dict(thresholds_extra),
        },
        estimate=_estimate_block(
            mode=mode,
            members=members,
            synthesis=synthesis,
            total_requests=total_requests,
            total_rounds=total_rounds,
            cap=cap,
            report_reserve=report_reserve,
            round_reserve=round_reserve,
            output_cap=output_cap,
        ),
        planner=dict(planner),
        facts={
            **facts.to_dict(),
            "clusters": len(clusters),
        },
        family=family,
    )


def _estimate_block(
    *,
    mode: str,
    members: Sequence[PlanMember],
    synthesis: Optional[PlanMember],
    total_requests: int,
    total_rounds: int,
    cap: int,
    report_reserve: int,
    round_reserve: int,
    output_cap: int,
) -> dict[str, Any]:
    """计划的**估算公式与结果**（进 UI 预览与落库计划，两边同一份）。

    口径与「实测」分开写：这里给的是**理论上限**与「撞上上限要多少轮」，不是预测。
    费用**不在这一层算** —— 没有项目价格表时编一个金额比不给更坏（`pricing` 的口径）。
    """
    return {
        "chars_per_token": 1,
        "formula": (
            "上限口径：每成员索取 = min(预算上限, ceil(本簇 diff 字 ÷ 11,000) + 6)，"
            "轮次 = ceil(索取 ÷ 5)；单次 token 上限 = "
            f"{cap:,}（字符≈token，与平台水位 600,000 字 / 1M 窗口同一口径）"
        ),
        "members": len(members),
        "has_synthesis": synthesis is not None,
        "requests_total": total_requests,
        "rounds_total": total_rounds,
        "output_tokens_per_member": output_cap,
        "round_reserve_tokens": round_reserve,
        "report_reserve_tokens": report_reserve,
        "mode": mode,
    }


def single_run_guard(
    plan: AnalysisPlan,
    *,
    spent_tokens: int = 0,
    spent_estimated: bool = False,
) -> dict[str, Any]:
    """单次硬上限的判定：`已花 + 本轮保守预留 + 收尾预留` 是否越过上限。

    返回 `{blocked, reason, spent, estimated, headroom}`。**纯算术**，不查库、不看时钟：

    * `spent_tokens` 由调用方给（子代理路径上是「本次运行已消耗的下界」，见
      `subagent._tokens_of`：没上报的按 0 算 = 先不拦）；
    * `spent_estimated=True` 表示这个数里有**估算**成分（上游缺用量时按「请求字符 +
      配置的输出上限」补的），报告与界面必须把「估算」两个字说出来 ——
      **未知不许当 0**（那等于无限放行），也不许当成精确值。

    超过时返回非空 `reason`：调用方据此**停止探索**（跳过还没跑的分片），产出一份
    「已查 / 未查」分开的报告，而不是再发一次模型调用。
    """
    cap = max(0, int(plan.total_token_budget))
    reserve = max(0, int(plan.round_reserve_tokens)) + max(0, int(plan.report_reserve_tokens))
    spent = max(0, int(spent_tokens))
    headroom = cap - spent - reserve
    mark = "（其中含保守估算的部分）" if spent_estimated else ""
    if cap and headroom < 0:
        return {
            "blocked": True,
            "reason": (
                f"单次分析上限 {cap:,} token：已花 {spent:,}{mark} + 本轮保守预留 "
                f"{plan.round_reserve_tokens:,} + 报告收尾预留 {plan.report_reserve_tokens:,}"
                f" 已超出 → 停止探索，未跑的成员进信息缺口（不再新增模型调用）"
            ),
            "spent": spent,
            "estimated": bool(spent_estimated),
            "headroom": headroom,
        }
    return {
        "blocked": False,
        "reason": "",
        "spent": spent,
        "estimated": bool(spent_estimated),
        "headroom": headroom,
    }


def single_run_lookahead(
    plan: AnalysisPlan,
    *,
    spent_tokens: int = 0,
    last_round_tokens: int = 0,
    spent_estimated: bool = False,
) -> dict[str, Any]:
    """「这一轮之后**还付得起下一轮**吗」—— 同一道闸门向前推一轮。

    ## 为什么要有它

    闸门（`single_run_guard`）在每一轮**开头**才判一次，付不起就 `break`（见
    `services/ai/engine.py` 循环里那一道）。也就是说它只在**钱已经花掉之后**才说话：
    2026-09-26 真机（项目 1 的 run 3）跑了 9 轮、**9 轮全在索取上下文、一次结论都没写**，
    第 10 轮开头被拦下，整个运行以「没有拿到可用的结论」收场 —— 而模型那时还以为自己
    有的是轮次。轮次与索取额度都各有提前的收敛指令（`round_hints.build_final_round_hint`
    与 `build_budget_exhausted_hint`），**唯独钱这条没有**；这一条补的就是它。

    ## 算式不另写一份

    判的就是闸门那条不等式（`已花 + 预留 > 上限`），只是把里面**未知的「本轮结束后已花
    多少」**代出来：闸门判 `spent_after + 预留 > cap`，这里用 `spent + 本轮预计` 当
    `spent_after`。本轮预计取 `max(round_reserve_tokens, last_round_tokens)` ——
    `round_reserve_tokens` 是平台对这一轮的估算（首轮没有实测可依据，只能用它），
    而**上一轮真的花了多少**通常更准（run 3：单轮实测 202,283，预留只有约 140,000，
    低估 45%）。取 `max()` 而不是「乘个系数」：这两项都有出处，系数没有。

    返回值的形状与 `single_run_guard` 逐字相同，`reason` 里写的是**已经推过一轮**的口径
    （「本轮预留」那一项是加上去的那一轮的）。
    """
    estimate = max(
        max(0, int(getattr(plan, "round_reserve_tokens", 0) or 0)),
        max(0, int(last_round_tokens or 0)),
    )
    return single_run_guard(
        plan,
        spent_tokens=max(0, int(spent_tokens)) + estimate,
        spent_estimated=spent_estimated,
    )


def conservative_tokens_for(prompt_chars: int, output_tokens: int) -> int:
    """上游没报用量时的**保守估算**：请求字符 + 配置的输出上限。

    「字符 ≈ token」是平台自己的换算口径（与 `_round_reserve` 同一处注释）。返回的这个数
    必须被**标记为估算**（`single_run_guard` 的 `spent_estimated`），不能当成精确值、
    更不能把未知当 0（那等于无限放行）。
    """
    return max(0, int(prompt_chars)) + max(0, int(output_tokens))


def conservative_member_tokens(outcome: Any, *, output_cap: int) -> tuple[int, bool]:
    """一个成员「至少花了多少」：**上游没报用量时的保守估算**（附「这是估算」标记）。

    上游报了就按它；没报时用「取回并放进提示词的上下文 + 每一轮的固定前缀 + 输出上限」
    估一个下界。两条纪律：

    * **未知不许当 0** —— 那等于无限放行（单次上限永远判「还没超」）；
    * **也不许当真值** —— 返回的第二个值就是「这里含估算」，报告的措辞要跟着它走。

    `None` 的成员（没跑）算 0 且不算估算：那是「压根没跑」，不是「跑了但不知道花了多少」。
    """
    if outcome is None:
        return 0, False
    reported = max(0, _int(getattr(outcome, "prompt_tokens", None))) + max(
        0, _int(getattr(outcome, "completion_tokens", None))
    )
    if getattr(outcome, "prompt_tokens", None) is not None or getattr(
        outcome, "completion_tokens", None
    ) is not None:
        return reported, False
    rounds = len(getattr(outcome, "rounds", ()) or ())
    estimated = conservative_tokens_for(
        _int(getattr(outcome, "context_chars", None)), output_cap
    )
    # 每一轮都要把「平台段 + 清单 + 历史基线」重发一次，而 `context_chars` 只算取回来的
    # 那部分内容 —— 不加这一项会低估。加了偏大也没关系：这一档本来就是**下界**估算，
    # 用途是「别把它当成 0」。
    estimated += rounds * (PLATFORM_OVERHEAD_RESERVE_CHARS + BASELINE_CHARS)
    estimated += rounds * ITEM_CHARS
    return max(0, estimated), True
