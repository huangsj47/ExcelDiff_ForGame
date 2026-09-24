"""子代理模式：把一次周版本分析拆给几个成员，再由主代理汇总。

## 它解决什么

一次周版本是几百个文件（线上 G119 有一次 767 个）。一个 agent 在 8 轮 / 20 次索取里
把这些全看一遍是不可能的 —— 报告里那句「未读到 X，`code_logic`、`version_branch` 两个
维度无法判断」就是这么来的。几个成员各认领几个维度去深挖，覆盖面才是真的。

## 三件本模块刻意做成这样的事

**一、分工由平台决定，不由模型决定。** 维度按**清单里的相邻顺序**均分给各成员
（`group_dimensions`），同一份配置永远得到同一套分工。让模型自己分工的代价是
「这次 3 个、下次 2 个、覆盖还不一样」，而报告要能解释自己是怎么来的。

**二、省钱的依据是「共享前缀」，不是「并发」。** 所有成员的请求前两条消息**逐字节相同**
（system + 含整份变更清单的第一条 user 消息），成员私有的差异**全部**排在共享前缀之后
（任务书那一条）。于是第一个成员写下的 prompt cache，后面每个成员（含汇总那一次）都能
命中。为此有三条硬规矩，改动前先读 `build_seed_messages` 与 `plan_family` 的说明：

* 共享消息里不许出现任何随成员变化的内容 —— 所以每轮的额度取**家族常量**
  （`max_rounds`、`max_tool_requests` 都由 `plan_family` 统一下调后写进共享消息）；
* 共享消息**只构造一次**，N+1 个成员共用（引擎那边拷贝后再用）；
* **顺序执行**。并发会让 N 个请求同时 miss，省钱的机制当场失效。

**三、谁没跑成、谁报的没进最终报告，都必须看得见。** 一个成员失败、或者因为预算不足被
跳过，**绝不许表现为「那个维度没问题」**；主代理漏掉一条候选，平台侧要自己查出来
（`reconcile_candidates`），**保证不建立在模型听话上**。

**四、成员之间共享的是缓存与额度来源，不是实例。** 一家子共用一份正文缓存
（`run_family` 里建、`context_tools` 第 5 条）—— 同一个文件被两个成员各要一次时只取一次，
两边都拿到**全文**；但索取额度是**每个成员各自一份**（`plan.limits`），谁也别想替别人花。
预算一侧同理：每片开跑前读一次**含本次运行已消耗**的判定（`should_skip` 的第二个参数），
不够就跳过并点名 —— 判据与起跑前的闸门是同一个 `budget_status`。

## 与引擎的分工

本模块不碰数据库、不碰 Flask，也不自己拼提示词：共享消息用引擎自己的
`prompt.build_system_prompt` / `build_user_message` 拼（这样它才与单代理运行时逐字节相同），
每个成员的执行走 `engine.run_analysis(seed_messages=…, task_message=…)`。
落库由 `services/ai_analysis_service.py` 负责，一家子只落**一条** `AiAnalysisRun`。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from services.ai.auto_sizing import FamilySizing, conservative_member_tokens
from services.ai.budget import ContextItem, truncate_text
from services.ai.engine import (
    DEGRADATION_LABELS,
    DEGRADE_CONTEXT,
    DEGRADE_MARKDOWN,
    DEGRADE_NONE,
    DEGRADE_PROTOCOL,
    DEGRADE_REQUESTS,
    DEGRADE_ROUNDS,
    DEGRADE_SUBAGENT,
    DEGRADE_VERIFY,
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineLimits,
    EngineOutcome,
    RoundProgress,
    RoundRecord,
    run_analysis,
)
from services.ai.evidence_store import EvidenceStore

# 数据模型（`MemberPlan`/`MemberOutcome`/`Candidate`/`FamilyResult`）与「平台侧对账 +
# 谁没交回结论」的文字搬到 `services/ai/family_ledger.py`（那个文件的 docstring 写了
# 为什么）。原处回导：本模块剩下的代码仍按旧名字调它们，引擎与测试也一直是从
# `services.ai.subagent` 取这些名字的。
from services.ai.family_ledger import (  # noqa: F401 —— 本模块与测试仍在按旧名字用
    CANDIDATE_BLOCK_MAX_CHARS,
    CANDIDATE_MAX_ITEMS_PER_MEMBER,
    CANDIDATE_TEXT_MAX_CHARS,
    KIND_SHARD_GAP,
    ROLE_SUBAGENT,
    ROLE_SYNTHESIS,
    ROLE_VERIFY,
    SYNTHESIS_LABEL,
    VERIFY_LABEL,
    AssignedFile,
    Candidate,
    EvidenceRef,
    FamilyResult,
    MemberOutcome,
    MemberPlan,
    _gap_lines,
    _markdown_excerpt,
    _shard_never_ran,
    evidence_index_of,
    evidence_refs_for,
    mandatory_request_floor,
    reconcile_candidates,
)
from services.ai.mandatory_progress import coverage_precheck
from services.ai.manifest import ManifestPlan, build_manifest
from services.ai.protocol import Anomaly, DroppedItem
from services.ai.report_document import demote_headings
from services.ai.rules import (
    DEFAULT_MAX_ANOMALIES,
    KIND_ANOMALY_CAP,
    RuleThresholds,
    normalize_anomalies,
)
from services.ai.scope import AnalysisScope
from services.ai.skill_contract import (
    DIMENSION_IDS,
    dimension_ids_of,
)
from services.ai.skill_loader import LoadedSkills

# 分片的任务书与提示词构造（2026-09-23 拆去独立模块）：本文件只留下「怎么跑」与「怎么汇总」，
# 而「写给模型的那几段话怎么拼」整段搬走了 —— 这一段是**纯构造**（不碰库、不跑引擎、不读时钟），
# 本文件顶着仓库 2000 行的 ERROR 闸门（`scripts/check_file_length.py`）。
#
# **仍然从这里重导出**：`services/ai/engine.py:1438` 是**函数内**延迟 import
# `build_cap_section` / `build_unclassified_section` 的（两个模块级互导会成环），
# 而十几个测试一直是按 `services.ai.subagent.<名字>` 取的。所以下面的名字一个都不能省 ——
# `# noqa: F401` 逐名写在那一行（写在 `from … import (` 那一行盖不住按名字报的 F401，
# `ruff --fix` 会把它们当垃圾删掉，本文件顶部的 `family_ledger` 那一段记着这个教训）。
from services.ai.subagent_tasks import (
    CAP_TITLE,  # noqa: F401 —— 测试按属性名取用
    DEFAULT_VERIFY_ITEMS,
    MAX_VERIFY_ITEMS,
    MIN_MEMBER_ROUNDS,
    MIN_MEMBER_TOOL_REQUESTS,
    VERIFY_MAX_EVIDENCE,
    FamilyPlan,
    FamilyQuota,
    _cap_limit_of,  # noqa: F401 —— 测试按属性名取用
    _percent_of,
    _pool_note,  # noqa: F401 —— 测试按属性名取用
    _render_candidates,  # noqa: F401 —— 测试按属性名取用
    attach_seed,
    build_cap_section,
    build_member_task,
    build_synthesis_task,
    build_unclassified_section,
    build_verify_task,
    pool_exhausted_note,
    verify_reserve,
    verify_reserve_for,  # noqa: F401 —— 估算侧按这个名字从本模块取（转出，见上面那段）
    verify_section,
)
from services.ai.verdict import (
    INDEPENDENT_REQUEST_TYPES,
    EvidenceGaps,
    Reduction,
    evidence_gaps_of,
    parse_verdicts,
    reduce_findings,
    render_ruling,
)
from utils.logger import log_print

# 子代理模式下**只对周版本**生效。单提交分析不拆：那一次改动的规模本来就不需要分工，
# 拆了只会让「这一次提交改了什么」多绕一圈。
WEEKLY_MODE = "weekly"

# 成员数上限。与配置项的 `SUBAGENT_COUNT_RANGE` 是同一件事，这里再钉一次是因为
# `plan_family` 也可能被别的调用方按数字直接调（测试就是）。
MAX_SUBAGENTS = 6
# 与模型层常量同源（models/ai_analysis/project_config.py）。**2026-09-23 起它不再决定
# 周版本的分片数** —— 那个数由 `auto_sizing.plan_analysis` 按快照事实推导（独立变更簇
# 2~5 个），本常量只剩两个读者：单提交路径不拆分片、`plan_family` 的老调用方。
# 它**不等于**任何推导目标，别再按它去解释「为什么是 5 片」。
DEFAULT_SUBAGENT_COUNT = 5


# 每个成员能拿到的额度 = 配置值的百分之多少（向上取整）。
#
# **不是「把总额平分下去」。** 用户配的是「一个分析 agent 能看多少内容」，所以每个成员的
# 额度必须以它为基准，而不是以「总额 ÷ 成员数」为基准 —— 后者会让分片数越多、每个分片
# 看得越少，与「多开几个分片来看得更全」正好相反（见 `plan_family` 的说明）。
#
# ## 70 → 100（2026-09-20）
#
# 取 70 的理由是「留三成余地给成员之间互相补位的重复索取」。**那个理由站不住**：重复索取
# 是各算各的账（同一个文件两个成员都要，各自扣各自己的次数，只有内容命中缓存），所以留
# 三成并不会「省下」什么 —— 它只是把每个成员的上限压低了三成。
#
# 真正的代价是**那个上限根本填不满提示词预算**：预算不变量
# （`test_the_prompt_budget_can_honor_the_request_budget`）是按
# 「配置的索取次数 × 单条上限」算的，也就是按 40 次算的；70% 之后成员最多只能要 28 次，
# 剩下那 30% 的预算**没有任何成员够得着**。真实那一轮也印证了这一点：五个 agent 全部
# 用满自己的 14/14，而它们的字符合计只有 26k~55k —— 先把次数用光，字符预算还剩大半。
# 所以卡住分析的是这个百分比，不是预算。
#
# 代价如实说：全家总的索取次数上限从 `成员数 × 70% + 汇总那一份` 变成
# `成员数 × 100% + 汇总那一份`（3 个分片：2.1 倍 → 3 倍）。额度是**上限不是预扣**，
# 只有真的用掉才花钱；而「用了多少」在报告与用量面板里都看得到。
MEMBER_BUDGET_PERCENT = 100
# 汇总失败后的保全报告只保存各分片报告的有限摘录。分片的完整轮次与结构化异常仍在 trace
# 和结果载荷中；这里的目的只是让人能读到已经完成的工作，不能再造一份无限膨胀的报告。
SALVAGE_MEMBER_REPORT_CHARS = 12_000


# --------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------


def group_dimensions(
    count: int, dimensions: Sequence[str] = DIMENSION_IDS
) -> tuple[tuple[str, ...], ...]:
    """把 `dimensions` 按**相邻顺序**均分成最多 `count` 组。**确定性的**。

    三条性质（都有用例钉着，见 `tests/test_ai_subagent_plan.py`）：

    * 每组**非空** —— 一个没分到维度的成员只会白花一次整份提示词的钱；
    * 每组是清单里**连续的一段** —— 顺序即相关性（`skill_contract` 里写明了这个顺序
      是有意的），所以相邻的维度落在同一个成员身上；
    * 全部维度**恰好出现一次** —— 漏一个 = 那个维度没人看，重复 = 两边各看一半。

    `count` 的钳制方向与旧实现一致（下限 2、上限 `MAX_SUBAGENTS`），但**多了一条**：
    不超过清单长度。清单短于 `count` 时少开几个成员是唯一正确的做法 —— 旧实现在这种情况
    下**没得选**，只能要么开出空成员、要么退化成「一个成员全看完」，而后者是用户花了
    (n+1) 倍的钱拿到单代理效果且无人提示。

    **刻意没有「查不到就退化」的分支**：任何 `count`、任何非空清单都能分出来。清单为空
    是调用方的契约违反（`plan_family` 要求维度清单非空），所以这里直接抛错而不是猜一个
    分组 —— 猜出来的分工会让报告的「本次由 S1/S2 分工」立不住。
    """
    items = tuple(str(item).strip() for item in dimensions if str(item or "").strip())
    if not items:
        raise ValueError("维度清单为空，无法分工（调用方应先确认清单非空）")
    wanted = min(max(2, int(count)), MAX_SUBAGENTS, len(items))

    base, extra = divmod(len(items), wanted)
    groups: list[tuple[str, ...]] = []
    start = 0
    for position in range(wanted):
        size = base + (1 if position < extra else 0)
        groups.append(items[start : start + size])
        start += size
    return tuple(groups)


def plan_family(
    *,
    mode: str,
    enabled: bool,
    count: int,
    limits: EngineLimits,
    verify: bool = False,
    verify_items: int = 0,
    dimensions: Sequence[str] = DIMENSION_IDS,
    sizing: "FamilySizing | None" = None,
) -> FamilyPlan | None:
    """要不要开子代理、怎么分。**不适用时返回 `None`**，调用方走原来的单代理路径。

    返回 `None` 的三种情形，都是「拆了没意义」：
    * 不是周版本（单提交的规模本来就不需要分工）；
    * 配置里没开（默认就是关的：这是一条会让消耗成倍上升的功能，必须由人主动打开）；
    * `count < 2`（一个成员就是原来的单代理，白白多花一次汇总的钱）。

    ## `dimensions`：本项目生效的维度清单

    默认是平台出厂那九个。项目声明了自己的清单时，`run_family_with_seed` 会在开跑前用
    **加载出来的那一份**重算分工（`apply_dimensions`）—— 生产路径不会用到这里的默认值，
    它是给「直接调 `plan_family` 的调用方（测试、探针）」的确定行为。

    ## `verify` 依附在子代理模式上

    `verify` 是对账轮（找反证）。**没开子代理就没有对账轮** —— 单代理那条路的报告本来
    就没有「几个分片各自的结论」需要核对，而这条功能的价值正是交叉核对。这一点写在这里
    而不是让配置界面去解释：`subagent_verify` 在 `subagent_enabled` 关掉时不生效，
    界面上的说明文案也是这么写的。

    ## 清单太短时不硬拆（但要说清）

    清单只有 1 个维度时**没有任何拆法**：`group_dimensions` 会给出 1 组，那就是原来的
    单代理，只会白白多花一次汇总的钱 —— 与「`count < 2`」是同一条理由，所以这里也返回
    `None`。它与上面三种情形不同：那三种是调用方自己的配置，这一种取决于**项目声明**，
    所以额外写一行日志 —— 用户按「开了 6 个分片」的预期看报告，得能查到为什么没跑。

    ## 为什么额度取「家族常量」而不是按成员各算一份

    每个成员的额度都要写进**共享消息**（「本次分析总共可索取 N 次」），而共享消息必须在
    所有成员之间逐字节相同。所以这里算出**一个大家一样的数字**，再拿它去拼共享消息。
    对账轮不额外分一份：它核对的是已经拿到的证据，用剩下的额度就够（详见
    `build_verify_task`）。

    ## 为什么每个成员拿 `MEMBER_BUDGET_PERCENT`% 而不是「总额 ÷ 成员数」

    原先取的是 `总额 // (count + 1)`（3 个分片 + 1 次汇总 = 每人 **25%**）。那等于把
    「设置的那个额度」当成**全家共享的一锅**去分，而用户配它时的意思不是这个 —— 他填的是
    「**一个**分析 agent 能看多少内容」，不是「四个 agent 加起来能看多少」。线上的表现
    很直白：配置 20 次 → 每个分片 5 次，一个分片跑 3 次就报「本轮上下文额度已用尽」，
    `references/test-scope-and-regression.md` 这类该读的文档一个都读不到，报告里只好写成
    「因额度耗尽未读」。而**分片数越多，每个分片反而看得越少**（4 个分片时每人 20%），
    与「多开几个分片来看得更全」的直觉正好相反。

    现在每个成员拿 `MEMBER_BUDGET_PERCENT`%（默认 100%，向上取整）——它是一条**下限**，
    不是从总额里划走的配额。取 100 而不是 70 的理由见
    `MEMBER_BUDGET_PERCENT` 那里的长注释（一句话：重复索取各算各的账，留三成余地省不下
    任何东西，只是让成员够不着已经付过预算的那部分额度）。代价如实说：开启子代理后，
    整次分析的**总**索取次数上限最高会到 `成员数 × 100% + 汇总那一份`（3 个分片 =
    3 倍），这是这条功能本来就有的量级（模型调用次数已经是 `分片数 + 1` 倍），而且
    **只有真的用掉才花钱**：额度是上限，不是预扣。

    ## 但那个「下限 2」不许把「配成 0」顶回去

    `MIN_MEMBER_TOOL_REQUESTS` 是给「上限调得很小」兜底的（否则会把成员饿成 0 次，子代理
    模式就白开了）。可它**不能**把配置里的 0 顶成 2：上限配 0 是一个明确的意思 ——「这次
    分析一次上下文都不给」，用户按这个意思配的，报告里也按这个意思说（见
    `prompt._budget_line`）。所以最后拿总额再夹一次：成员额度**永远不超过**配置的总额度。
    """
    if mode != WEEKLY_MODE or not enabled:
        return None
    # 配置面收敛后（2026-09-23），周版本的数值参数由 `auto_sizing` 按预算与本周规模
    # 推导，`count` 参数传的就是推导出的分片数 —— 库里冻结的 `subagent_count` 不再
    # 进这条路径（它在界面上已收掉）。直接调 `plan_family` 的调用方（测试）仍可手传
    # `count`，行为与从前一致。
    size = int(count or 0)
    if size < 2:
        return None
    size = min(size, MAX_SUBAGENTS)
    if sizing is not None:
        # 推导值覆盖 `limits` 里的三个数字（索取/轮次/每片异常）。注意顺序：先覆盖
        # 再做下面的成员名义额夹取 —— 名义额就是推导值本身，不会变小。
        limits = replace(
            limits,
            max_tool_requests=sizing.requests_per_shard,
            max_rounds=sizing.rounds_per_shard,
            max_anomalies_per_subagent=sizing.anomalies_per_subagent,
        )

    dimension_ids = tuple(str(item).strip() for item in dimensions if str(item or "").strip())
    if len(dimension_ids) < 2:
        # 见 docstring「清单太短时不硬拆」：这是一次**明确的**不拆，写日志交出去。
        log_print(
            "⚠️ AI 分析：项目声明的检查维度只有 "
            f"{len(dimension_ids)} 个，无法分工，本次按单代理路径跑（不额外花分片的钱）"
        )
        return None

    total_requests = int(limits.max_tool_requests)
    total_rounds = int(limits.max_rounds)
    member_requests = min(
        total_requests,
        max(MIN_MEMBER_TOOL_REQUESTS, _percent_of(total_requests, MEMBER_BUDGET_PERCENT)),
    )
    member_rounds = min(
        total_rounds,
        max(MIN_MEMBER_ROUNDS, _percent_of(total_rounds, MEMBER_BUDGET_PERCENT)),
    )
    family_limits = replace(
        limits, max_rounds=member_rounds, max_tool_requests=member_requests
    )

    groups = group_dimensions(size, dimension_ids)
    # 清单比配置的分片数短时**少开几个成员**（而不是开出空成员）。旧实现没有这一档，
    # 它只有「按表分」与「一个成员全看完」两条路，后者是静默的。这里的差别是显式的：
    # 成员数少一个都写在 `plan.count` 上（面板的「分片代理」表按它列），而且任务书里
    # 会说明为什么。
    count_note = ""
    if len(groups) < size:
        count_note = (
            f"（本项目生效的维度清单共 {len(dimension_ids)} 个，"
            f"按相邻顺序最多只能分成 {len(groups)} 组，所以本次开了 {len(groups)} 个分片，"
            f"而不是配置里的 {size} 个。）"
        )
        # 这句话同时写进汇总任务书与日志：任务书让**模型**知道分工是什么样，日志让
        # **配了那个数字的人**查得到「为什么 6 变成了 3」——只写在提示词里的话，
        # 用户面对面板上少了的几行只能猜。
        log_print(
            "ℹ️ AI 分析：本次分片数按项目声明的维度清单收敛："
            f"维度 {len(dimension_ids)} 个 → 分片 {len(groups)} 个（配置里是 {size} 个）"
        )

    members = tuple(
        MemberPlan(index=position, label=f"S{position}", role=ROLE_SUBAGENT, dimensions=group)
        for position, group in enumerate(groups, start=1)
    )
    synthesis = MemberPlan(
        index=len(members) + 1,
        # 显式给名字（见 `family_ledger.SYNTHESIS_LABEL`）：空串会让 trace、面板与几处
        # 日志渲染出病句，而且实时进度只能靠位次去猜「这一段是不是汇总」。
        label=SYNTHESIS_LABEL,
        role=ROLE_SYNTHESIS,
        # 汇总那一次的「维度」是全部：它要保证清单上的每个维度都有人答过，而不是只管自己
        # 那几组。
        dimensions=dimension_ids,
    )
    # 家族共享池：池 = 实际开出的成员数 × 名义额（维度比目标少、少开了成员时，池跟着
    # 缩 —— 池是「这几个成员能花的钱」，不是「配置面收敛前那个数字」）。汇总与对账轮
    # 从池内剩余取用，见 `FamilyQuota`。
    # 对账轮的预留**在规划时就从池里划走**（见 `verify_reserve`）：run 57 的 250 次是在
    # 复核之前被分片与汇总吃光的，于是 20 条结论只裁了 3 条，而正文与尾部还各说各话。
    reserve_requests, reserve_rounds = verify_reserve(
        enabled=bool(verify), items=_clamp_verify_items(verify_items)
    )
    quota = FamilyQuota(
        requests_pool=member_requests * len(members),
        rounds_pool=member_rounds * len(members),
        requests_nominal=member_requests,
        rounds_nominal=member_rounds,
        verify_requests=reserve_requests,
        verify_rounds=reserve_rounds,
    )
    return FamilyPlan(
        count=len(members),
        members=members,
        synthesis=synthesis,
        limits=family_limits,
        verify=bool(verify),
        verify_items=_clamp_verify_items(verify_items),
        count_note=count_note,
        quota=quota,
    )


def apply_dimensions(plan: FamilyPlan, dimensions: Sequence[str]) -> FamilyPlan:
    """按**本次加载出来的**维度清单重算分工。

    ## 为什么要有这一步，而不是让 `plan_family` 自己拿清单

    `plan_family` 的调用方（`services/ai_analysis_service.py`）手里没有 `LoadedSkills`
    —— 它把 skill 装进 `engine_args` 传给引擎。而 `run_family_with_seed` 从 `engine_args`
    里拿得到它。所以清单在这一层合流：**平台里只有一处知道「本次生效的清单是什么」，
    就是 `LoadedSkills.dimensions`**，分工从它派生，提示词也从它派生（`skill_loader`
    把同一份清单拼进正文）—— 两边不可能是两份。

    清单与计划里的一致时原样返回（不制造无意义的差异）。
    """
    dimension_ids = tuple(str(item).strip() for item in dimensions if str(item or "").strip())
    if not dimension_ids:
        return plan
    if tuple(plan.synthesis.dimensions) == dimension_ids and all(
        member.dimensions for member in plan.members
    ):
        return plan
    groups = group_dimensions(len(plan.members), dimension_ids)
    members = tuple(
        replace(member, dimensions=group)
        for member, group in zip(plan.members, groups)
    )
    return replace(
        plan,
        count=len(members),
        members=members,
        synthesis=replace(plan.synthesis, index=len(members) + 1, dimensions=dimension_ids),
    )


def apply_manifest(plan: FamilyPlan, manifest: ManifestPlan) -> FamilyPlan:
    """Attach deterministic discovery ownership without narrowing read permissions."""
    if manifest.shard_count != plan.count:
        manifest = build_manifest(
            (
                {
                    "repository_id": item.repository_id,
                    "repository_name": item.repository_name,
                    "commit": item.commit,
                    "path": item.path,
                    "source": item.source,
                }
                for item in manifest.entries
            ),
            shard_count=plan.count,
        )
    members = tuple(
        replace(
            member,
            assigned_files=tuple(
                AssignedFile(
                    repository_id=item.repository_id,
                    commit=item.commit,
                    path=item.path,
                    source=item.source,
                )
                for item in manifest.entries
                if member.label in item.assigned_shards
            ),
        )
        for member in plan.members
    )
    quota = plan.quota
    if quota is not None:
        # 启动前的覆盖预检（P1b）：分给各片的必读文件（**按三元组去重** —— 一个文件落在
        # 两个分片里只需要被读一次）比池还多时，在这里留下那句话，`run_family` 开跑前
        # 打进日志。放在这里是因为**只有这里同时知道清单与池**（清单刚挂上）。
        quota = replace(
            quota,
            coverage_note=coverage_precheck(
                assigned=len(
                    {
                        (item.repository_id, item.commit, item.path)
                        for member in members
                        for item in member.assigned_files
                    }
                ),
                pool=quota.requests_pool,
            ),
        )
    return replace(plan, members=members, quota=quota)


def _clamp_verify_items(value: Any) -> int:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return DEFAULT_VERIFY_ITEMS
    return max(1, min(size, MAX_VERIFY_ITEMS))


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def run_family(
    *,
    client: Any,
    provider: Any,
    loaded: LoadedSkills,
    scope: AnalysisScope,
    change_summary: str,
    plan: FamilyPlan,
    thresholds: RuleThresholds | None = None,
    project_knowledge: str = "",
    project_instructions: str = "",
    baseline_digest: str = "",
    on_round: Callable[[RoundProgress], None] | None = None,
    on_start: Callable[[RoundProgress], None] | None = None,
    should_skip: Callable[[MemberPlan, int], str] | None = None,
    run_analysis_fn: Callable[..., EngineOutcome] = run_analysis,
    single_run_budget: Any | None = None,
) -> FamilyResult:
    """顺序跑 N 个成员 + 1 次汇总，返回一家子的结果。

    ## 为什么是顺序，不是并发

    省钱的机制是「第一个成员写缓存、后面读」——并发会让 N 个请求**同时** miss，
    一个都省不下。而且顺序执行让 `db.session` 保持在一条线程上（落库在调用方，
    它本来就不是线程安全的）。

    ## 一家子共享一份正文缓存

    `body_cache` 在这里建、传给每个成员（见 `context_tools` 第 5 条）。**共享的是缓存，
    不是 `ContextTools` 实例** —— 每个成员的索取额度是各自的一份（`plan.limits`），
    合成一个实例会把它们并成一份，分工的代价也就跟着变成「总额度被 N 个人抢」。

    ## `should_skip`：预算不足时的早停

    每跑一个成员之前问一次「还跑得了吗」，第二个参数是**这家子到目前为止已消耗的
    token 数**（没有它，判定只能看已落库的量，而这次运行的花费还没落库 —— 结果就是
    每一片都以为「还没超」，一路跑到把预算超穿）。回答非空就**跳过**它，并把那句话原样
    写进成员的去向（它会进报告的信息缺口）。这是本模块**唯一**允许少跑一个成员的理由，
    而少跑的那一个必须被点名 —— 静默跳过等于把「没人看过」写成「没问题」。

    **汇总那一次不查预算**：它是唯一产出最终报告的一步，跳过它等于这一家子白跑 ——
    真的付不起就不该起跑（那是起跑前的闸门管的，见 `analysis_budget.budget_gate_reason`）。

    ## `single_run_budget`：`should_skip` 挡不住的那一半

    `should_skip` 只在**成员开跑前**看一眼，一个成员自己跑起来之后花多少没人管 ——
    上限被撑破正是从这个缺口漏出去的。传了 `single_run_budget` 之后，**同一个账本**
    会跟着进到每一个成员的引擎里：引擎在每轮开头读它、每次调用后扣它（判据与保证的
    边界见 `services/ai/budget_gate.py`）。于是

    * 成员开跑前：`should_skip` 用家族下界估值（既有闸门，一行不动）；
    * 成员跑起来之后：引擎读的是**逐次调用累出来的同一份账**，而它跨全部成员共享 ——
      所以它同时就是「这家子到现在花了多少」。

    ## 这道闸拦「探路」，不拦「交付」

    **汇总那一次不接这个账本**，与上面「汇总不查预算」是同一条产品决定：它是唯一产出
    最终报告的一步，而分片们已经把钱花了。拦下它，等于把一份「已查部分 + 缺口」的报告
    换成一次彻底的失败 —— 同样的钱，更差的结果（实测就撞上过这条：一次
    `test_an_exhausted_single_run_cap_stops_the_exploration_without_new_model_calls`
    在改动后从 degraded 掉成 failed，因为分片被跳过的同时汇总也被拦了）。真的付不起
    就该在起跑前被 `budget_gate_reason` 挡住 —— 那是「这次运行要不要开始」，
    不是「报告要不要写」。

    分片与对账轮都要接：那两步是**探路**，停下来只是「这一块没查完」，会如实进信息缺口。

    不传它时（老调用点与测试）行为与从前**逐字节相同**：`_call_engine` 只在它不是 `None`
    时才把那个关键字交给 `run_analysis_fn`（测试注入的假实现不认这个参数）。

    ## `run_analysis_fn`

    默认就是引擎的 `run_analysis`；测试用它注入假实现（本模块因此不必碰网络）。
    """
    steps: list[MemberOutcome] = []
    body_cache: MutableMapping[Any, ContextItem] = EvidenceStore()
    # 覆盖预检要在**开跑前**说出来（`apply_manifest` 算好的那一句）：等跑完再从报告里
    # 读到「本次有 N 个文件未取到证据」时，钱已经花了、这一轮也过去了。
    _precheck = str(getattr(plan.quota, "coverage_note", "") or "")
    if _precheck:
        log_print(_precheck, "AI")
    spent_tokens = 0
    # 家族共享池（串行滚动，见 `FamilyQuota`）。手搓的 `FamilyPlan`（测试）没带账本时，
    # 按「名义额即池」重建一份 —— 行为等价于「每片各拿名义额、互不借贷」的旧口径。
    fallback_reserve = verify_reserve(enabled=bool(plan.verify), items=plan.verify_items)
    quota = plan.quota or FamilyQuota(
        requests_pool=int(plan.limits.max_tool_requests) * len(plan.members),
        rounds_pool=int(plan.limits.max_rounds) * len(plan.members),
        requests_nominal=int(plan.limits.max_tool_requests),
        rounds_nominal=int(plan.limits.max_rounds),
        verify_requests=fallback_reserve[0],
        verify_rounds=fallback_reserve[1],
    )
    for position, member in enumerate(plan.members):
        reason = should_skip(member, spent_tokens) if should_skip is not None else ""
        if reason:
            steps.append(MemberOutcome(plan=member, skipped_reason=reason))
            continue
        # 当片上限 = 名义额 + **必读清单保底** + 前片省下的富余（后面几片的名义额、
        # 它们的必读保底、汇总下限与对账轮的预留都先被扣下）。保底的口径见
        # `mandatory_request_floor`：名义额够读必读清单时它是 0，行为与从前逐字节相同。
        members_after = len(plan.members) - (position + 1)
        nominal = quota.requests_nominal
        cap_requests, cap_rounds = quota.shard_caps(
            members_after,
            mandatory_floor=mandatory_request_floor(member, nominal),
            later_mandatory_floor=sum(
                mandatory_request_floor(item, nominal)
                for item in plan.members[position + 1:]
            ),
        )
        if cap_requests <= 0:
            # 池见底要**说清是哪一步、还剩多少**（引擎那句「额度用尽」说的是这一次成员的
            # 上限，读日志的人分不出「池真没了」与「这一片本来就只有这么多」）。
            log_print(pool_exhausted_note(quota, member.label or "分片"), "AI")
        member_limits = replace(
            plan.limits, max_tool_requests=cap_requests, max_rounds=cap_rounds
        )
        step = _run_one(
            member,
            plan=plan,
            client=client,
            provider=provider,
            loaded=loaded,
            scope=scope,
            change_summary=change_summary,
            thresholds=thresholds,
            project_knowledge=project_knowledge,
            project_instructions=project_instructions,
            baseline_digest=baseline_digest,
            on_round=on_round,
            on_start=on_start,
            body_cache=body_cache,
            run_analysis_fn=run_analysis_fn,
            member_limits=member_limits,
            pool_note=_pool_note(quota, cap_requests, cap_rounds, role_line=""),
            single_run_budget=single_run_budget,
        )
        steps.append(step)
        if step.outcome is not None:
            # 扣账按实跑数：轮次按 len(rounds)（引擎的 +2 格式重问轮不双记），索取按
            # requests_used —— 名义额是「上限不是预扣」，只有真用掉的才滚不出去。
            quota.spend(step.outcome.requests_used, len(step.outcome.rounds))
        spent_tokens += _tokens_of(step.outcome, output_cap=plan.limits.max_output_tokens or 0)

    # 汇总那一次：池内剩余全给它，但保下限、**可越池**（唯一产出最终报告的一步，不许
    # 被前面的分片饿死 —— 见 FamilyQuota.synthesis_caps）。
    synthesis_caps = quota.synthesis_caps()
    if synthesis_caps[0] <= 0:
        log_print(pool_exhausted_note(quota, "汇总"), "AI")
    synthesis_limits = replace(
        plan.limits,
        max_tool_requests=synthesis_caps[0],
        max_rounds=synthesis_caps[1],
    )
    raw_synthesis_outcome = _run_synthesis(
        plan=plan,
        steps=steps,
        client=client,
        provider=provider,
        loaded=loaded,
        scope=scope,
        change_summary=change_summary,
        thresholds=thresholds,
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
        baseline_digest=baseline_digest,
        on_round=on_round,
        on_start=on_start,
        body_cache=body_cache,
        run_analysis_fn=run_analysis_fn,
        member_limits=synthesis_limits,
        pool_note=_pool_note(
            quota,
            synthesis_caps[0],
            synthesis_caps[1],
            role_line="汇总这一次从池内剩余取用（保底、可越池）。",
        ),
    )
    if raw_synthesis_outcome is not None:
        quota.spend(
            raw_synthesis_outcome.requests_used, len(raw_synthesis_outcome.rounds)
        )
    synthesis_outcome = _salvage_failed_synthesis(
        synthesis=raw_synthesis_outcome,
        steps=steps,
        thresholds=thresholds or RuleThresholds(),
    )
    synthesis_step = MemberOutcome(
        plan=plan.synthesis,
        outcome=synthesis_outcome,
        # 保全成功也必须在成员账里留下“汇总调用失败”的原始事实。
        error=(
            raw_synthesis_outcome.error_message
            if raw_synthesis_outcome.status == STATUS_FAILED
            else ""
        ),
    )
    steps.append(synthesis_step)

    # 对账轮**在汇总之后**：它核对的正是汇总出来的那份报告（最高严重度那几条），放在
    # 汇总之前没有东西可核。它用的也是共享前缀，所以那一轮同样吃缓存。
    #
    # 汇总没跑成时**不跑它**：它核对的对象就是那份报告，而报告不存在 —— 跑下去要么是空转
    # （任务书里只剩「主代理没报出任何结论」那一支），要么是让模型对着一份失败的运行凭空
    # 产出「对账结论」。这一条与预算无关，所以不走 `should_skip`。
    if plan.verify and raw_synthesis_outcome.status != STATUS_FAILED:
        # 预算早停同样管它：这时再花一整份提示词去买一道**复核**，而复核的对象（报告）
        # 已经产出了 —— 跳过它并在报告里点名（`DEGRADE_VERIFY`）比挤掉下一个版本更划算。
        verify_member = MemberPlan(
            index=plan.count + 2, label=VERIFY_LABEL, role=ROLE_VERIFY, dimensions=()
        )
        reason = should_skip(verify_member, spent_tokens) if should_skip is not None else ""
        if reason:
            steps.append(MemberOutcome(plan=verify_member, skipped_reason=reason))
        else:
            verify_caps = quota.verify_caps()
            if verify_caps[0] <= 0:
                log_print(pool_exhausted_note(quota, VERIFY_LABEL), "AI")
            steps.append(
                _run_verify(
                    plan=plan,
                    member=verify_member,
                    synthesis=synthesis_outcome,
                    client=client,
                    provider=provider,
                    loaded=loaded,
                    scope=scope,
                    change_summary=change_summary,
                    thresholds=thresholds,
                    project_knowledge=project_knowledge,
                    project_instructions=project_instructions,
                    baseline_digest=baseline_digest,
                    on_round=on_round,
                    on_start=on_start,
                    body_cache=body_cache,
                    run_analysis_fn=run_analysis_fn,
                    # 对账轮是**探路**（找反证），所以要接账本 —— 它没跑成只是
                    # 「结论没经过复核」，如实写进报告即可（见 `run_family` 的
                    # `single_run_budget` 一节：拦探路，不拦交付）。
                    single_run_budget=single_run_budget,
                    member_limits=replace(
                        plan.limits,
                        max_tool_requests=verify_caps[0],
                        max_rounds=verify_caps[1],
                    ),
                    pool_note=_pool_note(
                        quota,
                        verify_caps[0],
                        verify_caps[1],
                        role_line="对账轮只用池内剩余，不保下限（它本来就是可跳过的一道）。",
                    ),
                )
            )

    candidates = tuple(
        candidate for step in steps for candidate in step.candidates
    )
    outcome = aggregate_outcomes(
        synthesis=synthesis_outcome,
        steps=steps,
        candidates=candidates,
        # 本次生效的维度清单（汇总那一份就是全部维度）。「未归类」那一节按它算。
        dimensions=plan.synthesis.dimensions,
        # 本次配置的条数上限：合入对账轮新发现时不许把它顶穿（见 `aggregate_outcomes`）。
        anomaly_limit=int(thresholds.max_anomalies) if thresholds is not None else None,
    )
    return FamilyResult(outcome=outcome, steps=tuple(steps), candidates=candidates)


def _salvage_failed_synthesis(
    *,
    synthesis: EngineOutcome,
    steps: Sequence[MemberOutcome],
    thresholds: RuleThresholds,
) -> EngineOutcome:
    """汇总失败时，把已完成分片保全成一份**明确降级**的结果。

    这不是再次“假装汇总”：平台仅做既有的门槛、证据裁剪、近似去重和条数封顶，并把
    各分片原报告按负责维度并列。若没有任何成功分片或结构化结论，仍返回原失败结果。
    """
    if synthesis.status != STATUS_FAILED:
        return synthesis

    usable = tuple(
        step
        for step in steps
        if step.plan.role == ROLE_SUBAGENT
        and step.outcome is not None
        and step.outcome.status != STATUS_FAILED
        and step.outcome.usable
    )
    if not usable:
        return synthesis

    raw: list[Anomaly] = []
    for step in usable:
        for candidate in step.candidates:
            item = candidate.anomaly
            source_ids = tuple(dict.fromkeys((*item.source_candidate_ids, candidate.id)))
            raw.append(replace(item, source_candidate_ids=source_ids))
    normalized = normalize_anomalies(raw, thresholds)

    reason = synthesis.error_message.strip() or "汇总模型调用失败（未返回具体原因）"
    sections = [
        "# 汇总失败后的分片保全报告\n\n"
        "> ⚠️ 本次最终汇总调用失败。以下内容来自已成功完成的分析分片，平台仅执行了"
        "规则门槛、近似去重和条数上限；**未完成跨分片归并、冲突裁决与独立对账**。"
        "这是一份可追溯的降级结果，不应当作完整报告。\n\n"
        f"失败原因：{reason}"
    ]
    for step in usable:
        outcome = step.outcome
        assert outcome is not None
        body, truncated = truncate_text(
            outcome.report_markdown.strip(), SALVAGE_MEMBER_REPORT_CHARS
        )
        dimensions = "、".join(step.plan.dimensions) or "未声明"
        note = "\n\n> 此分片正文过长，保全报告只显示前段；完整记录请查看逐轮明细。" if truncated else ""
        sections.append(
            f"## 分片 {step.plan.label}（负责：{dimensions}）\n\n"
            f"{demote_headings(body).strip()}{note}"
        )

    return replace(
        synthesis,
        status=STATUS_DEGRADED,
        anomalies=normalized.anomalies,
        dropped=(*synthesis.dropped, *normalized.dropped),
        report_markdown=_assemble_report(sections[0], sections[1:]),
        degradation=DEGRADE_SUBAGENT,
        error_message=reason,
    )


def run_family_with_seed(*, plan: FamilyPlan, limits: EngineLimits | None = None, **engine_args: Any) -> EngineOutcome:
    """调用方（`services/ai_analysis_service.py`）的**唯一入口**：装配共享前缀、跑完一家子、
    返回合成后的那一个结果。

    ## 为什么签名里有个用不上的 `limits`

    调用方手里那份参数是照着 `engine.run_analysis` 的签名攒的（单代理那条路原样用它）。
    子代理这条路每个成员用的是 `plan.limits`（家族常量，见 `plan_family`），所以这里把
    `limits` **收下但不使用** —— 收下是为了让调用方**同一个 dict 能喂给两条路**，
    否则那 14 个参数就要在两处各写一遍，而「两处不一致」正是最难查的一类 bug。

    ## 维度清单在这里合流

    调用方手里没有 `LoadedSkills`（它在 `engine_args` 里），所以「本次生效的维度清单」
    在这里才拿得到：分工按它重算（`apply_dimensions`），而提示词里的那份清单由
    `skill_loader.load_skills` 拼进正文 —— 同一份 `loaded.dimensions`，不存在两份。
    """
    prepared = attach_seed(
        plan,
        loaded=engine_args["loaded"],
        change_summary=engine_args["change_summary"],
        baseline_digest=engine_args.get("baseline_digest", ""),
        project_knowledge=engine_args.get("project_knowledge", ""),
        project_instructions=engine_args.get("project_instructions", ""),
    )
    prepared = apply_dimensions(prepared, dimension_ids_of(engine_args["loaded"].dimensions))
    return run_family(plan=prepared, **engine_args).outcome


def _tokens_of(outcome: EngineOutcome | None, *, output_cap: int) -> int:
    """一个成员烧掉多少 token，用于 `should_skip` 的**下界**。

    ## 上游没报用量时**按保守估算，不当 0**（工作包 B）

    原先这里返回 0（上游没报就算 0）。它的用途是单次硬上限的判定，而 0 的含义是
    「还没花钱」—— 于是「上游不回 usage」的那些成员会让闸门每一片都判「还没超」，
    一路跑到把单次上限超穿。现在缺用量时按「请求字符 + 每轮固定前缀 + 输出上限」
    估一个下界（`auto_sizing.conservative_member_tokens`），方向是**先不拦**但**不许当 0**。

    **落库那一份走的是 `_sum_optional`**：读不到就是 `None`（「未上报」），仍然如实
    记成未上报 —— 闸门要的是「至少花了这么多」，账要的是「上游到底报没报」。两者
    读者不同，口径也应当不同（这条区别在本函数的返回值与 `run.tokens_input` 上各有一半）。
    """
    tokens, _estimated = conservative_member_tokens(
        outcome, output_cap=max(0, int(output_cap or 0))
    )
    return tokens


def _run_one(
    member: MemberPlan,
    *,
    plan: FamilyPlan,
    client: Any,
    provider: Any,
    loaded: LoadedSkills,
    scope: AnalysisScope,
    change_summary: str,
    thresholds: RuleThresholds | None,
    project_knowledge: str,
    project_instructions: str,
    baseline_digest: str,
    on_round: Callable[[RoundProgress], None] | None,
    on_start: Callable[[RoundProgress], None] | None,
    body_cache: MutableMapping[Any, ContextItem],
    run_analysis_fn: Callable[..., EngineOutcome],
    member_limits: EngineLimits,
    pool_note: str,
    single_run_budget: Any | None = None,
) -> MemberOutcome:
    """跑一个成员（子代理或汇总），把它报出的候选结论抽出来。

    `member_limits` 是编排层按共享池算出的**当片上限**（名义额 + 前片滚下来的富余，
    见 `run_family`），`pool_note` 是写进它私有任务书的额度账 —— 引擎与 `ContextTools`
    一行不动，额度对引擎来说就是 `limits.max_tool_requests` 那个数字。

    ## 抽的是**过门槛之前**的 `payload.anomalies`

    `outcome.anomalies` 已经过「门槛过滤 + 去重 + 封顶 10 条」。若每个成员先封顶再汇总，
    「N 个成员各报 12 条、最后只剩 10 条」会把**跨维度的发现静默吃掉**。所以这里读的是
    原始那批，门槛/去重/封顶只在最终那一次做。
    """
    outcome = _call_engine(
        client=client,
        provider=provider,
        loaded=loaded,
        scope=scope,
        change_summary=change_summary,
        limits=member_limits,
        thresholds=thresholds,
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
        baseline_digest=baseline_digest,
        plan=plan,
        member=member,
        task_message=build_member_task(member, plan, pool_note=pool_note),
        on_round=on_round,
        on_start=on_start,
        body_cache=body_cache,
        run_analysis_fn=run_analysis_fn,
        single_run_budget=single_run_budget,
    )
    candidates = _candidates_of(member, outcome, evidence_index_of(body_cache))
    return MemberOutcome(
        plan=member,
        outcome=outcome,
        error="" if outcome.status != STATUS_FAILED else outcome.error_message,
        candidates=candidates,
        candidate_total=_raw_candidate_count(outcome),
    )


def _raw_candidate_count(outcome: EngineOutcome) -> int:
    """这个成员**一共**报了多少条候选（封顶之前）。"""
    if outcome.payload is None:
        return 0
    return len(outcome.payload.anomalies)


def _candidates_of(
    member: MemberPlan,
    outcome: EngineOutcome,
    evidence_index: Mapping[str, EvidenceRef] | None = None,
) -> tuple[Candidate, ...]:
    """抽候选（见 `_run_one`），并把**每条候选背后的正文地址**一并记下来。

    `evidence_index` 是共享证据仓的地址表（`evidence_index_of(body_cache)`）：按候选点名的
    文件路径查出它的地址（`family_ledger.evidence_refs_for`）。查不到就是空元组 ——
    任务书那边会回落到贴它自己写的证据。
    """
    raw: Sequence[Anomaly] = ()
    if outcome.payload is not None:
        raw = outcome.payload.anomalies
    index = evidence_index or {}
    items = tuple(
        Candidate(
            member_label=member.label,
            index=position,
            anomaly=item,
            evidence_ids=tuple(
                ref.evidence_id for ref in evidence_refs_for(index, item.file_path)
            ),
        )
        for position, item in enumerate(raw, start=1)
    )
    return items[:CANDIDATE_MAX_ITEMS_PER_MEMBER]


def _run_verify(
    *,
    plan: FamilyPlan,
    member: MemberPlan,
    synthesis: EngineOutcome,
    client: Any,
    provider: Any,
    loaded: LoadedSkills,
    scope: AnalysisScope,
    change_summary: str,
    thresholds: RuleThresholds | None,
    project_knowledge: str,
    project_instructions: str,
    baseline_digest: str,
    on_round: Callable[[RoundProgress], None] | None,
    on_start: Callable[[RoundProgress], None] | None,
    body_cache: MutableMapping[Any, ContextItem],
    run_analysis_fn: Callable[..., EngineOutcome],
    member_limits: EngineLimits,
    pool_note: str,
    single_run_budget: Any | None = None,
) -> MemberOutcome:
    """跑对账轮。它**不是分片**：维度是空的，`role` 是 `verify`，面板上标签是 `V1`。

    `member` 由 `run_family` 建好传进来（那里要用它先问一次预算），所以这里不再自己造一个 ——
    两处各造一个的下场是 `index` / `label` 迟早对不上，而标签正是面板上的那一列。
    `member_limits` 是池内剩余推出的当轮上限（不保下限），`pool_note` 是它的额度账。
    """
    outcome = _call_engine(
        client=client,
        provider=provider,
        loaded=loaded,
        scope=scope,
        change_summary=change_summary,
        limits=member_limits,
        # **对账轮不套常规轮那个「依据最多 3 条」的夹子**（判据见 `VERIFY_MAX_EVIDENCE`）。
        # 阈值对象是 frozen 的，替换出一份**只改这一项**的副本交下去 —— 其余门槛
        # （严重度、条数上限、近似去重）与常规轮必须完全一致，否则同一批结论在两轮里
        # 会按两套标准过筛。
        thresholds=(
            replace(thresholds, max_evidence=VERIFY_MAX_EVIDENCE)
            if thresholds is not None
            else None
        ),
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
        baseline_digest=baseline_digest,
        plan=plan,
        member=member,
        task_message=build_verify_task(
            plan,
            synthesis,
            evidence_index=evidence_index_of(body_cache),
            pool_note=pool_note,
            # 复核挑谁要按三个确定性信号加权，其中两条（跨仓、归属可疑）要读范围账：
            # 「这个文件被本窗口几条提交改过」「证据里的路径属于哪个仓库」。
            scope=scope,
        ),
        on_round=on_round,
        on_start=on_start,
        body_cache=body_cache,
        run_analysis_fn=run_analysis_fn,
        single_run_budget=single_run_budget,
    )
    return MemberOutcome(
        plan=member,
        outcome=outcome,
        error="" if outcome.status != STATUS_FAILED else outcome.error_message,
        # **对账轮不进候选池**：候选池的用途是「主代理有没有漏掉某个分片报的东西」
        # （`reconcile_candidates`），而对账轮跑在汇总**之后** —— 汇总不可能采纳一份
        # 还没产生的结论，把它塞进候选池只会给每次运行都记上几条「找不到去向」的假账。
        # 它新发现的问题写在报告末尾那一节里，读报告的人一定看得到。
        candidates=(),
        candidate_total=_raw_candidate_count(outcome),
    )


def _run_synthesis(
    *,
    plan: FamilyPlan,
    steps: Sequence[MemberOutcome],
    client: Any,
    provider: Any,
    loaded: LoadedSkills,
    scope: AnalysisScope,
    change_summary: str,
    thresholds: RuleThresholds | None,
    project_knowledge: str,
    project_instructions: str,
    baseline_digest: str,
    on_round: Callable[[RoundProgress], None] | None,
    on_start: Callable[[RoundProgress], None] | None,
    body_cache: MutableMapping[Any, ContextItem],
    run_analysis_fn: Callable[..., EngineOutcome],
    member_limits: EngineLimits,
    pool_note: str,
) -> EngineOutcome:
    """汇总那一次。`member_limits` 是池内剩余推出的上限（保下限、可越池，见
    `FamilyQuota.synthesis_caps`），`pool_note` 是写进汇总任务书的额度账。"""
    return _call_engine(
        client=client,
        provider=provider,
        loaded=loaded,
        scope=scope,
        change_summary=change_summary,
        limits=member_limits,
        thresholds=thresholds,
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
        baseline_digest=baseline_digest,
        plan=plan,
        member=plan.synthesis,
        task_message=build_synthesis_task(
            plan,
            steps,
            evidence_index=evidence_index_of(body_cache),
            pool_note=pool_note,
        ),
        on_round=on_round,
        on_start=on_start,
        body_cache=body_cache,
        run_analysis_fn=run_analysis_fn,
    )


def _call_engine(
    *,
    client: Any,
    provider: Any,
    loaded: LoadedSkills,
    scope: AnalysisScope,
    change_summary: str,
    limits: EngineLimits,
    thresholds: RuleThresholds | None,
    project_knowledge: str,
    project_instructions: str,
    baseline_digest: str,
    plan: FamilyPlan,
    member: MemberPlan,
    task_message: str,
    on_round: Callable[[RoundProgress], None] | None,
    on_start: Callable[[RoundProgress], None] | None,
    body_cache: MutableMapping[Any, ContextItem],
    run_analysis_fn: Callable[..., EngineOutcome],
    single_run_budget: Any | None = None,
) -> EngineOutcome:
    """调一次引擎，把「我是谁」贴到进度上。

    `RoundProgress` 里的 `agent` / `agent_index` / `agent_total` 是**给界面看的**：
    抽屉要显示「分片 S1 (1/3) · 第 2 轮」。汇总那一次的 `agent` 是空串（它就是这个
    分析的主代理），靠 `agent_index == agent_total` 认出是汇总。

    同一个 `report` 也接 `on_start`：那一帧（引擎在第一次模型调用之前发）**同样要贴归属**，
    否则从上一个分片跑完到汇总第一轮跑完之间，快照里留的还是上一个分片的名字 ——
    界面会写着「分片 S3 · 第 2 轮」，而主代理其实已经在跑了。这一帧是**唯一**能让
    「谁在跑」在那一整段里正确的机会（下一次回调要等那一轮跑完，可能是几分钟之后）。
    """
    def report(progress: RoundProgress) -> None:
        if on_round is None:
            return
        on_round(
            replace(
                progress,
                agent=member.label,
                agent_index=member.index,
                # 对账轮开着时它是第 n+2 步 —— 分母要跟着变，否则抽屉上会出现
                # 「(4/4)」之后又冒出第 5 个的怪事。
                agent_total=plan.count + 1 + (1 if plan.verify else 0),
            )
        )

    # 只有真给了账本才把这个关键字交下去：测试注入的 `run_analysis_fn` 是假实现，
    # 多塞一个它不认的参数就等于把那些用例打断（而「不传时行为逐字节相同」是这几个
    # 子代理专属参数的既有承诺，见 `run_analysis` 的 docstring）。
    optional_kwargs: dict[str, Any] = (
        {} if single_run_budget is None else {"single_run_budget": single_run_budget}
    )
    # 必读清单（P1b）：**空清单不传**。汇总与对账轮本来就没有清单（`MemberPlan`
    # 的默认值），而分片在没有 manifest 的运行里也没有 —— 那时引擎那一段进度恒为空，
    # 提示词与从前逐字相同。
    if member.assigned_files:
        optional_kwargs["mandatory_files"] = tuple(member.assigned_files)
    return run_analysis_fn(
        client=client,
        provider=provider,
        loaded=loaded,
        scope=scope,
        change_summary=change_summary,
        limits=limits,
        thresholds=thresholds,
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
        baseline_digest=baseline_digest,
        on_round=report if on_round is not None else None,
        on_start=report if on_round is not None else None,
        seed_messages=plan.seed_messages,
        task_message=task_message,
        body_cache=body_cache,
        **optional_kwargs,
    )


# --------------------------------------------------------------------------
# 汇总与对账
# --------------------------------------------------------------------------


# 成员降级的轻重。**缺一个成员**（覆盖面少一块）比「某一次结论是在被压过的提示词上
# 得出的」更要紧：前者是「这块没人看过」，后者是「这块看过了但看得不够」。
_DEGRADE_RANK: Mapping[str, int] = {
    DEGRADE_NONE: 0,
    DEGRADE_ROUNDS: 1,
    DEGRADE_REQUESTS: 2,
    DEGRADE_MARKDOWN: 3,
    DEGRADE_PROTOCOL: 3,
    DEGRADE_CONTEXT: 4,
    # 对账轮没跑成排在「缺一个分片」**之前**：缺分片是「有一块维度没人看过」，
    # 而对账轮是可选的一道复核 —— 结论仍然完整，只是少了「找反证」这一步。
    DEGRADE_VERIFY: 5,
    DEGRADE_SUBAGENT: 6,
}


def _worst(codes: Sequence[str]) -> str:
    return max(codes, key=lambda code: _DEGRADE_RANK.get(code, 0), default=DEGRADE_NONE)


def _verify_missed(steps: Sequence[MemberOutcome]) -> bool:
    """对账轮**开了但是没跑成**。

    没开的时候 `steps` 里压根没有这一条（`run_family` 只在 `plan.verify` 时才追加），
    所以这里不判「配置开没开」—— 「没开」与「开了没跑成」是两件事，只有后者要报。
    """
    return any(
        step.plan.role == ROLE_VERIFY and (step.skipped_reason or step.failed)
        for step in steps
    )


def _verify_ran(steps: Sequence[MemberOutcome]) -> bool:
    """对账轮**真的给出了东西**（跑成了，不是被跳过 / 失败 / 压根没开）。

    与 `_verify_missed` 不是一回事：那个判「报了缺口没有」，这个判「有没有可读的裁决」。
    报告里那一节要不要出现，取决于后者 —— 一次没能跑成的复核没有任何裁决可渲染
    （它由信息缺口那一节如实点名）。
    """
    return any(
        step.plan.role == ROLE_VERIFY
        and step.outcome is not None
        and step.outcome.status != STATUS_FAILED
        for step in steps
    )


def verify_did_independent_work(steps: Sequence[MemberOutcome]) -> bool:
    """对账轮**有没有自己取过新证据**（P0-01）。

    判据是它**实际执行过**的取数类型（`EngineOutcome.tool_stats` 的 `calls`），不是它
    自己说做了什么：`evidence`（按地址取回已有原文）与 `read_reference`（读 skill 文档）
    都**不算** —— 它们看的是这次之前就有的东西。

    为什么非要判这一条：实测 run 58 的复核轮 6 次索取**全部**是 `evidence`，一次独立检索
    都没有，而报告里那三条写的是「反证不成立（维持原结论）」—— 读的人会把它当成
    「有人独立去搜过、没搜到」。事实核对无误，但它答的是另一个问题。
    """
    for step in steps:
        if step.plan.role != ROLE_VERIFY or step.outcome is None:
            continue
        stats = step.outcome.tool_stats or {}
        for kind in INDEPENDENT_REQUEST_TYPES:
            bucket = stats.get(kind) or {}
            if int(bucket.get("calls") or 0) > 0:
                return True
    return False


def _anomaly_limit(dropped: Sequence[DroppedItem], fallback: int | None = None) -> int:
    """本次生效的**条数上限**。

    取值次序：**记账里那次运行的上限** → 调用方给的本次配置上限（`thresholds.max_anomalies`）
    → 平台默认值（`rules.DEFAULT_MAX_ANOMALIES`）。

    记账优先，因为这一份渲染的是**当时那次运行**的事实，而配置是调用方此刻手里的值 ——
    「用户改了配置、翻看旧报告」时两者不是同一个数（与 `_cap_limit_of` 同一条理由，
    那里连上限的原文都是按当时那次记的）。记账里没有（这一次没触发封顶，所以没记）时
    才用配置值：**合入对账轮的新发现不许把用户配的上限顶穿**。
    """
    for item in dropped:
        if getattr(item, "kind", "") != KIND_ANOMALY_CAP:
            continue
        parsed = _cap_limit_of(item)
        if parsed.isdigit():
            return int(parsed)
    if fallback is not None and int(fallback) >= 0:
        return int(fallback)
    return DEFAULT_MAX_ANOMALIES


def _reduce_with_verify(
    synthesis: EngineOutcome,
    steps: Sequence[MemberOutcome],
    *,
    threshold_limit: int | None = None,
) -> Reduction:
    """把对账轮的结果**应用**到主结论上（`verdict.reduce_findings`）。

    ## 三个输入各是什么

    * **主结论**：`synthesis.anomalies`（已过门槛、已去重、已按严重度封顶）；
    * **对账轮的裁决**：它 `report_markdown` 里那个 json 块（`verdict.parse_verdicts`）；
    * **对账轮的新发现**：`outcome.anomalies` —— 这一份以前被整条丢掉（连「未归类」
      那一节都进不去），现在经同一道校验（结构、重复、条数上限）合入清单。

    ## 对账轮没跑成 / 没给裁决时

    一条结论都不改（`reduce_findings` 的空输入就是恒等），并在报告里写明「本次复核对结论
    一条都没生效」。**不拿一个不存在的复核去动真实结论** —— 与「没有结论时按规模定级、
    并明说不是模型结论」是同一条口径。

    ## 正文编号与证据缺口

    * `body_text` 传的是**汇总写的那份正文**（`synthesis.report_markdown`）：`F#` 要映射到
      的正文编号只存在于它里面（`verdict.assign_body_labels`）；
    * `gaps` 是本次运行**已知的证据缺口**（`verdict.evidence_gaps_of`），它让「引用的文件
      被截断过 / 额度用尽导致它需要的那块没轮到」的结论不得维持 `very_high`。**只在复核
      真的跑出了东西时才传**：那一节是这些降级唯一会被说明的地方，复核没跑成时报告里
      没有那一节，压了置信度就是**静默改动** —— 而静默正是这一批缺陷的共同形态
      （降了不写、撤了不留痕）。所以宁可在这条路上不压。
    """
    step = next((item for item in steps if item.plan.role == ROLE_VERIFY), None)
    outcome = step.outcome if step is not None and not step.failed else None
    limit = _anomaly_limit(synthesis.dropped, threshold_limit)
    gaps = (
        evidence_gaps_of(item.outcome for item in steps)
        if _verify_ran(steps)
        else EvidenceGaps()
    )
    if outcome is None:
        return reduce_findings(
            synthesis.anomalies,
            limit=limit,
            body_text=synthesis.report_markdown,
            gaps=gaps,
        )
    return reduce_findings(
        synthesis.anomalies,
        new=outcome.anomalies,
        verdicts=parse_verdicts(outcome.report_markdown),
        limit=limit,
        body_text=synthesis.report_markdown,
        gaps=gaps,
        # 取证方式由**平台按它实际执行过的取数类型**判定，不采信它自己说做了什么。
        verify_independent=verify_did_independent_work(steps),
    )


def aggregate_outcomes(
    *,
    synthesis: EngineOutcome,
    steps: Sequence[MemberOutcome],
    candidates: Sequence[Candidate] = (),
    dimensions: Sequence[str] = DIMENSION_IDS,
    anomaly_limit: int | None = None,
) -> EngineOutcome:
    """把一家子的账合成**一个** `EngineOutcome`（落库那一层只认一个）。

    ## `dimensions` 是**本次生效的维度清单**

    `run_family` 传的是 `plan.synthesis.dimensions`（来自 `LoadedSkills.dimensions`）。
    它只用于一件事：把落不进清单的条目单独列成「未归类」那一节（`build_unclassified_section`）
    —— 那一节是「一条发现都不许消失」的最后一道保证。默认值是平台出厂那九个，
    供直接调用本函数的调用方（测试）使用。

    ## `anomaly_limit` 是本次配置的条数上限

    `run_family` 从 `thresholds.max_anomalies` 传进来。它只在**合入对账轮新发现**时用得上
    （那是唯一可能让清单超过上限的地方），而记账里没有上限原文时（这一次没触发封顶）
    就取它 —— **平台自己不许把用户配的上限顶穿**。

    ## 轮次为什么要重编号

    `ai_analysis_trace` 上有 `uq_ai_trace_run_round`（run_id + round_index 唯一），而一家子
    只落**一条运行**。所以每个成员的轮次在这里被重编成**家族内全局递增**的序号
    （顺序执行 → 确定），每个成员自己那几轮另存在 `agent_round` 里（界面显示「S1 · 第 2/4 轮」
    用它）。这样那个唯一约束一个字节都不用改。

    ## 缓存 token 用 `_sum_optional` 的口径

    任一成员没上报就是 `None`（**不是 0**）：一轮报了 900/1000、另一个没报，只把报了的那几
    个加起来会得出一个偏高、且看起来完全正常的命中率。
    """
    report_source = synthesis.report_markdown or ""
    report = report_source
    # **复核裁决先算**：后面那几节（未归类、条数上限、信息缺口）都要与它对齐，
    # 而且 `anomalies` 最终取的就是它算出来的活动清单（见文件末尾的 `anomalies=`）。
    reduction = _reduce_with_verify(synthesis, steps, threshold_limit=anomaly_limit)
    # 对账轮的整份原文**不再进报告正文**（AI-P1-01：同一件事在报告里出现三遍 ——
    # 模型稿一遍、平台裁决节一遍、对账轮原文一遍，读的人还得自己辨认哪一句已经失效）。
    # 它作为**独立存档**放进结论载荷（`verify_report_markdown`），报告只留平台那几节。
    verify_text = "".join(
        verify_section(step) for step in steps if step.plan.role == ROLE_VERIFY
    )
    # 「复核标注」是平台对结论说的话，**排在正文之前**（2026-09-24 起，run 57）：
    # 读者先看到的是正文里那些**未经复核**的断言，读到尾部才知道「复核只覆盖 3 条」——
    # 实测那一轮正文写着 20 条结论、只裁了 3 条，正文的高严重度陈述与尾部的「待人工核验」
    # 互相矛盾。前置 + 开头写明覆盖数，这条矛盾第一眼就能看见。
    #
    # **模型原文一个字都不改**（顺序变了，内容没变）——AI-P1-01 时期「裁决节取代正文」的
    # 形态用户实测后明确不要，所以草稿仍是正文主体，标注只点名被改判的条目（标题 + 等级
    # 变化 + 理由）。落库的异常表 / 下一轮基线 / `final_findings` 仍然只认 `reduce_findings`
    # 那一份，「单一结论清单」的关切由落库层承担，呈现层不重复造第二份清单。
    ruling_text = render_ruling(reduction, review_ran=_verify_ran(steps))
    # 「未归类」也排在信息缺口之前：它对读者同样是**结论的一部分**（有几条发现不属于
    # 任何维度），而信息缺口永远收尾。
    unclassified_text = build_unclassified_section(synthesis.anomalies, dimensions)
    # 「结论条数上限」排在「未归类」之后：两节都是**对上面那份结论清单本身的补充**
    # （一节说「有几条不属于任何维度」，一节说「有几条根本没列进来」），而信息缺口
    # 说的是「没看到」，永远收尾。
    cap_text = build_cap_section(synthesis.dropped)
    # 复核裁决**传进去**（`reduction=`）：被裁决撤销的候选，去向是「已撤销」而不是
    # 「找不到去向」—— 不传的话，同一条候选会在正文的「已推翻」与这一节的「需要人工看一眼」
    # 里各说一遍，而两遍互相矛盾。**传上面算好的那一个对象，不在这里重算**（见 `:1519`）：
    # 两处各算一次迟早会出现「对账说撤销、落库说还在」。
    gaps_text, gap_dropped = reconcile_candidates(
        candidates, synthesis, steps=steps, reduction=reduction
    )
    # 平台自己那几节（次序即正文里的次序，见各节自己的说明）。
    platform_sections = tuple(
        item for item in (unclassified_text, cap_text, gaps_text) if item
    )
    # 汇总没跑成时**没有报告**：那段缺口说明写进 `error_message`（见下面那个分支）。
    # 往一份空报告后面追加一段「信息缺口」等于凭空造出一份看得见的报告，而这次其实
    # 什么都没得到 —— 两份东西都不能给。所以失败时一个字都不追加。
    # ## 草稿什么时候另存一份
    #
    # **只有有裁决标注时才有 `draft_markdown`**（下面 `if ruling_text:` 那一支）。判据不是
    # 「正文变了没有」：没有裁决节时平台那几节是**接在草稿后面**的（`else` 支），草稿仍然在
    # 正文开头 —— 那时另存一份就是同一段字节在载荷里出现两次。有裁决节时正文虽然也以草稿
    # 开头，但这份**独立存档**是给外部读侧（API/SSE 消费者可能只认这个键取「模型原稿」）
    # 保留的兼容层，取值永远是模型的原始草稿、不含任何平台拼接。
    draft_markdown = ""
    if synthesis.status != STATUS_FAILED:
        if ruling_text:
            # 有裁决 = 平台对结论说了话，而**读者要先知道复核覆盖了多少条**：
            # 次序是 复核标注 → 模型草稿 → 未归类 / 条数上限 / 信息缺口。
            # 草稿里可能整段写着裁决之前的等级（如「critical，仍成立」），标注节逐条点名
            # 了被改判的结论并写明「原 X → 新 Y」，读者对着读即可，不需要平台替他删改
            # 模型的原文 —— 被改判后的**规范值**在落库异常表与 `final_findings` 里，
            # 报告呈现不承担那份口径。
            report = _assemble_report(ruling_text, (report_source, *platform_sections))
            draft_markdown = report_source
        elif platform_sections:
            # 没有裁决节：草稿就是正文里**唯一那份结论**，平台那几节接在它后面
            # （次序：模型正文 → 未归类 / 条数上限 / 信息缺口）。**不另存**。
            report = _assemble_report(report, platform_sections)
        # 两个分支都不成立时 `report` 逐字不变：这时模型写的那份正文**就是**这份报告里
        # 唯一的结论，没有第二份口径要它让位（没开对账轮是绝大多数运行的形状）。

    rounds = _merge_rounds(steps)
    dropped = tuple(item for step in steps if step.outcome for item in step.outcome.dropped)
    # 被复核裁掉/被平台校验拒收的条目**也在这本账上**（`verdict.KIND_VERIFY`）：它们不是
    # 「模型没报」，而是「报了但去向是撤销或被拒」——不记的话，那几条就是静默消失。
    dropped = (*dropped, *gap_dropped, *reduction.rejected)
    # 「额度用尽没轮到的那几块」同样要跨成员合起来。只留计数的话，报告末尾只能说
    # 「有 N 个请求没执行」，说不出是哪几个 —— 而这一家子有几个成员，缺的那几块可能
    # 分别来自不同成员。
    refused_requests = tuple(
        label for step in steps if step.outcome for label in step.outcome.refused_requests
    )

    # 真缺口只认两种：「分片压根没跑」（`_shard_never_ran`）与「汇总交回的结论里没有
    # 一条声明来源于这条候选」（`KIND_SHARD_GAP`）。**待复核不在此列**（`KIND_DEFERRED`）：
    # 那是汇总主动交代的处置，报告里另有一节逐条写明理由 —— 把它算进来会把
    # 「五个代理全部 succeeded、零真缺口」的运行（run 40）整体判成「有分片没有跑成」，
    # 一句与事实相反的降级标签。
    has_gap = _shard_never_ran(steps) or any(
        item.kind == KIND_SHARD_GAP for item in gap_dropped
    )
    degradation = _worst(
        [
            *(step.outcome.degradation for step in steps if step.outcome),
            DEGRADE_VERIFY if _verify_missed(steps) else DEGRADE_NONE,
            DEGRADE_SUBAGENT if has_gap else DEGRADE_NONE,
        ]
    )
    if synthesis.status == STATUS_FAILED:
        # 汇总没跑成 = 这一家子没有报告。降级与否已经不重要了，原样把失败原因带出去；
        # 但**「谁没跑成」这件事不能跟着丢** —— 它写进错误信息（面板上看得见），
        # 而不是写进一份不存在的报告。
        status = STATUS_FAILED
        degradation = synthesis.degradation
        error_message = "\n".join(
            item for item in (synthesis.error_message, *_gap_lines(steps)) if item
        )
    elif degradation:
        status = STATUS_DEGRADED
        error_message = DEGRADATION_LABELS.get(degradation, "")
    else:
        status = STATUS_SUCCEEDED
        error_message = ""

    return EngineOutcome(
        status=status,
        payload=synthesis.payload,
        # **只有 `final_findings` 里的那些进这里**：被复核撤销的条目不在其中，于是
        # 异常表、下一轮基线（读的是异常表的行）、以及面板上那份活动清单都不会再把它
        # 当成「仍然成立的问题」。**这是这条链路上唯一的取舍点** —— 落库、读侧、导出
        # 读的都是这一个字段或它的投影。
        anomalies=reduction.active_anomalies(),
        dropped=dropped,
        refused_requests=refused_requests,
        report_markdown=report,
        # 复核裁决**结构化地**带出去（AI-P0-05）：`result_payload` 只认这个字段，不再从
        # 报告正文末尾那行 HTML 注释里反向解析。`changed` 为假时给 `None` —— 与原先
        # 「没有影响就不写那个块」的语义一致，读侧按「没有裁决」处理。
        verdict=reduction.as_dict() if reduction.changed else None,
        # 草稿与对账轮原文的存档（AI-P1-01）：报告正文里没有它们，读侧要看得去这两处。
        draft_markdown=draft_markdown,
        verify_report_markdown=verify_text,
        rounds=rounds,
        # 本次生效的维度清单跟着汇总那一次带出来（它来自 `LoadedSkills.dimensions`，
        # 见 `run_analysis`）。落库那一份（`result_payload`）据此把 category 翻成中文名，
        # 导出文档才不会把项目自己声明的维度显示成「未归类」。
        dimension_specs=synthesis.dimension_specs,
        requests_used=sum(step.outcome.requests_used for step in steps if step.outcome),
        cache_hits=sum(step.outcome.cache_hits for step in steps if step.outcome),
        # 与缓存那两个字段同一口径：**只要有一个成员没上报，整次就是 `None`**。
        # 这里以前是 `sum(...)`，读不到的成员会被当成 0 加进去 —— 子代理模式下一次有
        # n+1 次模型调用，少算一次会让「这次花了多少」偏小，而那个数看起来完全正常。
        prompt_tokens=_sum_optional(
            [step.outcome.prompt_tokens for step in steps if step.outcome]
        ),
        completion_tokens=_sum_optional(
            [step.outcome.completion_tokens for step in steps if step.outcome]
        ),
        degradation=degradation,
        error_message=error_message,
        cache_read_tokens=_sum_optional(
            [step.outcome.cache_read_tokens for step in steps if step.outcome]
        ),
        cache_write_tokens=_sum_optional(
            [step.outcome.cache_write_tokens for step in steps if step.outcome]
        ),
        cache_source=next(
            (step.outcome.cache_source for step in steps if step.outcome and step.outcome.cache_source),
            "",
        ),
        tool_stats=_merge_tool_stats(steps),
        context_chars=sum(step.outcome.context_chars for step in steps if step.outcome),
        duration_ms=sum(step.outcome.duration_ms for step in steps if step.outcome),
        subagents=tuple(_member_block(step) for step in steps),
        subagent_skipped=tuple(
            f"{step.plan.label}：{step.skipped_reason}" for step in steps if step.skipped_reason
        ),
    )


def _assemble_report(head: str, sections: Sequence[str]) -> str:
    """把报告拼成一份：`head` 在前，平台那几节依次接在后面。

    与原先「一节一节 `.rstrip()` 之后追加 `\\n\\n`、最后整体 `.strip() + "\\n"`」逐字等价
    —— 逐节追加的写法在节数变成五个之后就没人读得懂了，而**这一份文本是有契约的**：
    它是 `ai_analysis_run.response_text`、是抽屉与导出显示的那份报告。

    空节丢掉（`render_ruling` / `build_cap_section` 在无事可说时返回空串），节间恰好一个
    空行，结尾恰好一个换行。
    """
    parts = [head.strip(), *(str(item).strip() for item in sections if str(item).strip())]
    body = "\n\n".join(item for item in parts if item)
    return (body + "\n") if body else ""


def _sum_optional(values: Sequence[int | None]) -> int | None:
    """与 `engine._sum_optional` 同一条口径：**只要有一个没上报，整家就是 `None`**。"""
    if not values or any(value is None for value in values):
        return None
    return sum(int(value) for value in values if value is not None)


def _merge_rounds(steps: Sequence[MemberOutcome]) -> tuple[RoundRecord, ...]:
    merged: list[RoundRecord] = []
    for step in steps:
        if step.outcome is None:
            continue
        for record in step.outcome.rounds:
            merged.append(
                replace(
                    record,
                    index=len(merged) + 1,
                    agent=step.plan.label,
                    agent_round=record.index,
                )
            )
    return tuple(merged)


def _merge_tool_stats(
    steps: Sequence[MemberOutcome],
) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for step in steps:
        if step.outcome is None:
            continue
        for kind, counters in (step.outcome.tool_stats or {}).items():
            bucket = merged.setdefault(kind, {})
            for name, value in counters.items():
                bucket[name] = bucket.get(name, 0) + int(value)
    return merged


def _member_block(step: MemberOutcome) -> dict[str, Any]:
    """一个成员在 `response_payload["subagents"]` 里的一行。**纯数据**（JSON 安全）。"""
    outcome = step.outcome
    return {
        "label": step.plan.label or SYNTHESIS_LABEL,
        "role": step.plan.role,
        "index": step.plan.index,
        "dimensions": list(step.plan.dimensions),
        "status": outcome.status if outcome is not None else "skipped",
        "rounds": outcome.rounds_used if outcome is not None else 0,
        "requests": outcome.requests_used if outcome is not None else 0,
        "tokens_input": outcome.prompt_tokens if outcome is not None else 0,
        "tokens_output": outcome.completion_tokens if outcome is not None else 0,
        "cache_read_tokens": outcome.cache_read_tokens if outcome is not None else None,
        "cache_write_tokens": outcome.cache_write_tokens if outcome is not None else None,
        # 报出的条数按**原始**条数记（`candidate_total`）：`candidates` 是给汇总用的、
        # 已经按 30 条封过顶，拿它当「报出几条」会让面板少报。
        "anomalies": step.candidate_total or len(step.candidates),
        "report_chars": len(outcome.report_markdown or "") if outcome is not None else 0,
        "skipped_reason": step.skipped_reason,
        "error": step.error,
    }


# --------------------------------------------------------------------------
# 落库侧要的两件小事
# --------------------------------------------------------------------------


def subagent_mode_of(outcome: EngineOutcome) -> tuple[str, int] | None:
    """`(subagent_mode, subagent_count)` —— 没开子代理时返回 `None`（列写 NULL）。"""
    if not outcome.subagents:
        return None
    count = sum(1 for item in outcome.subagents if item.get("role") == ROLE_SUBAGENT)
    return ("subagents", count) if count else None
