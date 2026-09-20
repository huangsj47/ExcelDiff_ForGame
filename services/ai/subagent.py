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

import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, MutableMapping, Sequence

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
from services.ai.prompt import build_system_prompt, build_user_message
from services.ai.prompt_cache import mark_cache_breakpoint
from services.ai.protocol import Anomaly, DroppedItem, unclassified_anomalies
from services.ai.report_document import demote_headings
from services.ai.rules import KIND_ANOMALY_CAP, RuleThresholds, rank_anomalies
from services.ai.scope import AnalysisScope
from services.ai.skill_contract import (
    DIMENSION_IDS,
    UNCLASSIFIED_LABEL,
    dimension_ids_of,
)
from services.ai.skill_loader import LoadedSkills
from utils.logger import log_print



# 数据模型（`MemberPlan`/`MemberOutcome`/`Candidate`/`FamilyResult`）与「平台侧对账 +
# 谁没交回结论」的文字搬到 `services/ai/family_ledger.py`（那个文件的 docstring 写了
# 为什么）。原处回导：本模块剩下的代码仍按旧名字调它们，引擎与测试也一直是从
# `services.ai.subagent` 取这些名字的。
from services.ai.family_ledger import (  # noqa: F401 —— 本模块与测试仍在按旧名字用
    CANDIDATE_BLOCK_MAX_CHARS,
    CANDIDATE_MAX_ITEMS_PER_MEMBER,
    CANDIDATE_TEXT_MAX_CHARS,
    Candidate,
    FamilyResult,
    MemberOutcome,
    MemberPlan,
    ROLE_SUBAGENT,
    ROLE_SYNTHESIS,
    ROLE_VERIFY,
    VERIFY_LABEL,
    _findings_text,
    _gap_lines,
    _markdown_excerpt,
    _path_named_in_findings,
    _shard_never_ran,
    reconcile_candidates,
)

# 子代理模式下**只对周版本**生效。单提交分析不拆：那一次改动的规模本来就不需要分工，
# 拆了只会让「这一次提交改了什么」多绕一圈。
WEEKLY_MODE = "weekly"

# 成员数上限。与配置项的 `SUBAGENT_COUNT_RANGE` 是同一件事，这里再钉一次是因为
# `plan_family` 也可能被别的调用方按数字直接调（测试就是）。
MAX_SUBAGENTS = 6
DEFAULT_SUBAGENT_COUNT = 3

# 对账轮一次核对几条。**只核对最严重的几条**：对账要的是深度（去找反证、指出证据够不够），
# 而不是把整份报告重读一遍 —— 后者正是汇总那一次已经做过的事。
DEFAULT_VERIFY_ITEMS = 3
MAX_VERIFY_ITEMS = 5

# 一个成员至少要有的索取额度与轮次。低于它就别拆了：一个只有 2 次索取的成员既看不深，
# 又要多花一次整份提示词的钱，得不偿失。
MIN_MEMBER_TOOL_REQUESTS = 2
MIN_MEMBER_ROUNDS = 2


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


def _percent_of(total: int, percent: int) -> int:
    """`total` 的 `percent`%，**向上取整**。

    向上取整是这里的口径：这条规则是「每个成员**至少**有设置值的 N%」，取整取小了就
    违背了它（40 的 70% = 28 正好，25 的 70% = 17.5 取 17 就少给了）。用整数算，
    不引 float —— 额度是要写进提示词、也要与数据库里的整数比对的量。
    """
    total = max(0, int(total))
    return -(-total * int(percent) // 100)





@dataclass(frozen=True)
class FamilyPlan:
    """一家子的计划：几个成员 + 一份共享前缀 + 一套家族常量额度。

    `limits` 里的两个数字**必须对所有成员（含汇总那一次）相同**：它们被写进了共享消息的
    正文（「本次分析总共可索取 N 次」），一个一个地调就会让共享消息不再逐字节相同，
    于是 prompt cache 全部失效 —— 而那**不会报错**，只会悄悄贵好几倍。

    `verify` 打开时会在汇总之后再跑一次「找反证」（`build_verify_task`），它同样用这份
    共享前缀（所以也吃缓存），日志与面板上的标签是 `V1`。
    """

    count: int
    members: tuple[MemberPlan, ...]
    synthesis: MemberPlan
    limits: EngineLimits
    seed_messages: tuple[Mapping[str, Any], ...] = ()
    verify: bool = False
    verify_items: int = DEFAULT_VERIFY_ITEMS
    # 「成员数为什么与配置的不一样」的一句话（空 = 一样）。项目声明的维度清单比配置的
    # 分片数短时成员会少几个 —— 少开成员是**显式**的：这句话会写进汇总任务书，
    # 而不是让用户自己对着面板数为什么 6 变成了 3。
    count_note: str = ""

    @property
    def all_steps(self) -> tuple[MemberPlan, ...]:
        return (*self.members, self.synthesis)








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
    size = int(count or 0)
    if size < 2:
        return None
    size = min(size, MAX_SUBAGENTS)

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
        label="",
        role=ROLE_SYNTHESIS,
        # 汇总那一次的「维度」是全部：它要保证清单上的每个维度都有人答过，而不是只管自己
        # 那几组。
        dimensions=dimension_ids,
    )
    return FamilyPlan(
        count=len(members),
        members=members,
        synthesis=synthesis,
        limits=family_limits,
        verify=bool(verify),
        verify_items=_clamp_verify_items(verify_items),
        count_note=count_note,
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


def _clamp_verify_items(value: Any) -> int:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return DEFAULT_VERIFY_ITEMS
    return max(1, min(size, MAX_VERIFY_ITEMS))


def build_seed_messages(
    *,
    loaded: LoadedSkills,
    change_summary: str,
    limits: EngineLimits,
    baseline_digest: str = "",
    project_knowledge: str = "",
    project_instructions: str = "",
) -> tuple[Mapping[str, Any], ...]:
    """一家子共用的**前两条消息**：system + 含整份变更清单的第一条 user 消息。

    ## 它必须与「按同一份额度跑的单代理」逐字节相同

    拼装用的是引擎自己的 `build_system_prompt` / `build_user_message`，参数也与引擎在
    第 1 轮用的完全一样（`requests_remaining` 就是 `limits.max_tool_requests`，`items` /
    `budget_notes` / `correction_hint` 都是空）。**额度那一句也在这条消息里**，而分摊之后
    每个成员的额度比项目配置的小（`plan.limits`），所以：

    * 与**同额度**的单代理运行：前两条消息逐字节相同；
    * 与**项目配置那份额度**的单代理运行：只有系统消息相同（断点①那一段跨运行恒定）。

    这两条都写成了测试（`tests/test_ai_subagent_cache.py`）。不要把第二句写成第一句 ——
    额度必须如实告诉模型它能花多少，那是它决定「一次要完还是逐步逼近」的依据。

    ## 断点挂在这里，不挂在任务书上

    断点②（跨运行复用的那个）挂在共享消息末尾 —— 它是整份提示词里最大的一段（清单顶到
    上限时约 87,000 字符）。成员各自的任务书只带断点③（可挪动的那个），于是「第一个成员
    写缓存、其余成员读缓存」这件事才成立。

    返回的是**两个打了标记的副本**：调用方（引擎）会再拷贝一次，成员之间不会互相改到。
    """
    system_prompt = build_system_prompt(
        loaded,
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
    )
    first_user = build_user_message(
        change_summary=change_summary,
        round_index=1,
        max_rounds=limits.max_rounds,
        baseline_digest=baseline_digest,
        requests_remaining=limits.max_tool_requests,
        requests_total=limits.max_tool_requests,
        # 本次生效的维度清单。**必须与引擎第 1 轮传的是同一份**（`run_analysis` 里那
        # 一处取的是 `loaded.dimensions`），否则「按同一份额度跑的单代理」与家庭成员
        # 的第 1 条消息不再逐字节相同 —— 而那是这套机制省钱的**全部**依据。
        dimension_ids=dimension_ids_of(loaded.dimensions),
    )
    return (
        mark_cache_breakpoint({"role": "system", "content": system_prompt}),
        mark_cache_breakpoint({"role": "user", "content": first_user}),
    )


def attach_seed(
    plan: FamilyPlan,
    *,
    loaded: LoadedSkills,
    change_summary: str,
    baseline_digest: str = "",
    project_knowledge: str = "",
    project_instructions: str = "",
) -> FamilyPlan:
    """给计划装上共享前缀。**一家子只装一次**（顺序执行 → 也只用写一次缓存）。"""
    return replace(
        plan,
        seed_messages=build_seed_messages(
            loaded=loaded,
            change_summary=change_summary,
            limits=plan.limits,
            baseline_digest=baseline_digest,
            project_knowledge=project_knowledge,
            project_instructions=project_instructions,
        ),
    )


# --------------------------------------------------------------------------
# 任务书
# --------------------------------------------------------------------------


def build_member_task(member: MemberPlan, plan: FamilyPlan) -> str:
    """一个子代理的任务书（它第 1 轮的 user 消息，也是唯一私有的一段）。

    六件事都要说清，否则会出两种具体的错：**它以为自己的读取范围只有自己那几个维度**
    （于是跨模块耦合永远发现不了），或者**它以为没人管的维度要自己兜底**（于是重复劳动、
    还把别人的活干浅了）。
    """
    others = [
        f"- {item.label}：{'、'.join(item.dimensions)}"
        for item in plan.members
        if item.index != member.index
    ]
    blocks = [
        f"# 分工：你是 {member.label}（第 {member.index}/{plan.count} 个分片代理）",
        (
            "本次周版本分析由平台拆给了 "
            f"{plan.count} 个分片代理并行深挖，最后由主代理汇总成报告。"
            "**你只负责下面这几个维度，把它们看深、看透** —— 覆盖面由分工保证，"
            "你要保证的是深度。"
        ),
        "## 你负责的维度\n\n" + "\n".join(f"- `{item}`" for item in member.dimensions),
        (
            "## 你的读取权限\n\n"
            "**你可以读取本批次改动的任何一个文件，不受上面的维度限制。** "
            "跨模块耦合、协议的另一端、生成物与表是不是一起改了 —— 这些本来就只有"
            "越出自己那几块去看才发现得了，所以白名单给的是**全部**改动文件。"
            "上面那几行是**你的职责**，不是你的视野边界。"
        ),
    ]
    if others:
        blocks.append(
            "## 别的分片代理负责什么（你不要替它们兜底）\n\n"
            + "\n".join(others)
            + "\n\n你读到了属于它们维度的问题也可以报（宁可多报，由主代理去重），"
            "但不要为了它们专门花索取额度。"
        )
    blocks.extend(
        [
            (
                "## 输出要求\n\n"
                "与常规分析**完全一样**：走同一套 JSON 协议、同样的精度要求。"
                "唯一的差别是 `report_markdown` 只写**你这几个维度的发现与依据**"
                "（不必写整版报告，主代理会把它们汇总成最终报告）。"
                "`dimensions` 里**系统提示词那份「本项目适用的维度清单」上的每一个维度"
                f"都要留痕（本次共 {len(plan.synthesis.dimensions)} 个）**：你负责的那几个"
                "写 `hit` 与理由，其余写 `hit: false` 并注明「由 S? 负责」即可。"
            ),
            (
                "**不许因为分了工就降低标准**：每一条结论都要有具体证据（文件、字段、"
                "ID、行、提交），拿不准的按原口径写成「待确认」，取不到的内容写成"
                "「信息缺口」——**绝不许写成「没问题」**。"
            ),
        ]
    )
    return "\n\n".join(blocks).rstrip() + "\n"


def build_synthesis_task(plan: FamilyPlan, steps: Sequence[MemberOutcome]) -> str:
    """主代理的任务书：各分片的候选结论 + 谁没跑成 + 汇总纪律。

    ## 为什么候选要带 `[S1-2]` 这样的编号

    编号是**平台发出去的**，所以平台能查「这一条进了最终报告没有」。模型被要求采纳时
    把编号带进 `evidence`；即便它没带，平台侧还有第二手（同文件匹配，见
    `reconcile_candidates`）。漏掉的候选会被平台追加进报告的「信息缺口（平台补充）」——
    **这条保证不建立在模型听话上**。
    """
    blocks = [
        "# 分工：你是主代理（汇总）",
        (
            f"本次周版本分析由 {plan.count} 个分片代理分头深挖，"
            "它们的候选结论都在下面。**你的任务是把它们审一遍、合成一份完整的报告**，"
            "而不是另起一份。"
        ),
        (
            "## 纪律（四条）\n\n"
            "1. **每一条候选都要有去向**：要么进你的 `anomalies`（采纳，并在证据里保留它的"
            "编号，例如 `[S1-2]`），要么在报告正文与 `dimensions` 里说明为什么不采纳"
            "（重复、证据不足、与其它条目冲突）。**不许一声不响地丢掉**。\n"
            f"2. **各分片负责的维度必须都有交代**：系统提示词那份「本项目适用的维度清单」"
            f"上的 {len(plan.synthesis.dimensions)} 个维度一个都不能空着；"
            "某个维度没人报出问题，也要写 `hit: false` 与理由。\n"
            "3. **报告是最终报告**：七个章节写全，长度不受分片影响。"
            "你还可以用工具去核对候选里可疑的地方（文件、行号、提交），"
            "也可以补充分片漏掉的发现。\n"
            "4. **篇幅要收着写**：单次输出有硬上限，写超了会被**截断**，"
            "整份 JSON 作废、你的结论一条都留不下。所以同类候选合并成一条写，"
            "只写能改变结论的内容；候选很多时按后果排序写透前几条，"
            "其余用一行列出来即可 —— 但 `dimensions` 与 `anomalies` 必须完整。"
        ),
        # 分工表**无论有没有候选都要给**：纪律第 2 条要求「维度都有交代」，而主代理
        # 只有知道每个成员负责哪几个维度，才知道该有哪些维度的交代 —— 一个成员没报出
        # 任何东西时，这一行就是它唯一的存在证明。
        "## 各分片的分工\n\n"
        + "\n".join(
            f"- {item.label}：{'、'.join(item.dimensions)}" for item in plan.members
        )
        + (f"\n\n{plan.count_note}" if plan.count_note else ""),
    ]

    candidates_block = _render_candidates(plan, steps)
    if candidates_block:
        blocks.append(candidates_block)

    gaps = _gap_lines(steps)
    if gaps:
        blocks.append(
            "## 没能交回结论的分片（**必须写成信息缺口**）\n\n"
            + "\n".join(gaps)
            + "\n\n上面这几块**这一次没有结论可用** —— 是「压根没跑」还是「跑了但结论没交回来」，"
            "每条自己写着。报告里必须如实写明"
            "（写进「信息缺口」与对应的 `dimensions`），"
            "**绝不能因为没人报出问题就当成「没问题」**。"
        )

    blocks.append(
        "## 额度\n\n"
        f"你和每个分片代理的额度是一样的（各 {plan.limits.max_tool_requests} 次索取、"
        f"最多 {plan.limits.max_rounds} 轮）。汇总不需要重新通读整批，"
        "把额度花在核对可疑条目上。"
    )
    return "\n\n".join(blocks).rstrip() + "\n"


def build_verify_task(plan: FamilyPlan, synthesis: EngineOutcome) -> str:
    """对账轮的任务书：把最严重的几条交出去，**要求它去找反证**。

    ## 为什么是「找反证」，而不是「再评审一遍」

    汇总那一次已经在评审了，再问一遍「你觉得对不对」得到的只会是同一批理由的复述 ——
    而且模型对自己刚写下的结论天然是**确认偏误**的一方。所以这一轮的指令是反过来的：
    你的任务是**推翻它们**，每条都要给出「能证明它不成立的具体文件与行」，找不到就明说
    「未找到反证」。这两种答复都必须是**有代价的**：说「未找到」也要写明你去哪里找过。

    ## 只核对最严重的几条

    对账要的是深度（证据够不够、有没有误报），不是覆盖面 —— 覆盖是汇总那一次的事。
    所以按严重度取前 `plan.verify_items` 条（默认 3）。这一轮不额外分索取额度：
    核对几条结论用剩下的额度足够，而多分一份就会让每个分片的额度少一点。

    ## 它用同一份共享前缀

    对账轮的请求形状与分片一致（system + 共享变更清单 + 本任务书），所以**它也吃缓存** ——
    这是一次额外的模型调用里最贵的那一段。
    """
    # 排序复用 `rules.rank_anomalies`（严重度 → 置信度，同档保持模型给的顺序）——
    # 对账要挑的「最严重的几条」必须与封顶时挑的是同一个口径，两套排序迟早会不一致。
    ranked = [
        anomaly
        for _index, anomaly in rank_anomalies(list(enumerate(synthesis.anomalies)))[
            : plan.verify_items
        ]
    ]
    blocks = [
        "# 分工：你是对账轮（找反证）",
        (
            f"本次周版本分析由 {plan.count} 个分片代理分头深挖、主代理汇总成了报告，"
            "已经交给评审者。**你的任务不是再评审一遍，而是去找它们的反证。**"
        ),
        (
            "## 纪律（四条）\n\n"
            "1. **对下面每一条，明确回答「反证成立」还是「未找到反证」**，两种都要给依据。\n"
            "2. **反证必须落到具体位置**：哪个文件、哪几行、哪个配置行 —— 指不出来就不算反证，"
            "只能写成「未找到反证（我查了哪里、没查到）」。\n"
            "3. **不要重复确认它是**：原文里的理由不是新证据；你要找的是**能推翻它的东西**"
            "（这段代码根本不会走到、这个字段在这次改动里没变、这个判定在服务端另有一道校验、"
            "这是阶段性屏蔽…）。\n"
            "4. **确实推翻不了就如实说**。「未找到反证」是一个完全合格的答复，"
            "编一条反证比说没找到糟得多。"
        ),
    ]
    if not ranked:
        blocks.append(
            "## 待核对结论\n\n主代理这次没有报出任何达到门槛的结论。"
            "你要做的是判断这件事本身是否站得住：本批次里有没有被整份报告漏掉的改动"
            "（**对照系统提示词那份「本项目适用的维度清单」逐条看一遍**，"
            "尤其是最容易被放过去的取值合理性与跨模块耦合两类），"
            "有就在 `anomalies` 里报出来，没有就写「未找到反证」。"
        )
    else:
        lines = []
        for index, anomaly in enumerate(ranked, start=1):
            lines.append(f"### {index}. {anomaly.title}")
            lines.append(f"- 维度：{anomaly.category} · 严重度 {anomaly.severity}")
            if anomaly.file_path:
                lines.append(f"- 位置：{anomaly.file_path}")
            if anomaly.impact:
                lines.append(f"- 它说会造成：{truncate_text(anomaly.impact, CANDIDATE_TEXT_MAX_CHARS)[0]}")
            for evidence in anomaly.evidence[:3]:
                lines.append(f"- 它的证据：{truncate_text(str(evidence), CANDIDATE_TEXT_MAX_CHARS)[0]}")
        blocks.append("## 待核对结论（逐条回答）\n\n" + "\n".join(lines))
    blocks.append(
        "## 输出\n\n"
        "走同一套 JSON 协议（`status` / `report_markdown` / `anomalies` 都一样）。"
        "对账结论写在 `report_markdown` 里，**一条一段**，形如 "
        "「第 1 条 ××：反证成立 —— <哪个文件哪一行说明了什么>」或 "
        "「第 1 条 ××：未找到反证（查了 <文件/行>）」；"
        "`anomalies` 只放**你在找反证的过程中新发现的**问题，没有就给空数组。"
    )
    return "\n\n".join(blocks).rstrip() + "\n"


def _render_candidates(plan: FamilyPlan, steps: Sequence[MemberOutcome]) -> str:
    """把各成员报出的候选结论渲染成任务书里的一段（含「还有几条没列出来」）。"""
    lines: list[str] = []
    total = 0
    for step in steps:
        if not step.ran:
            continue
        block: list[str] = []
        for candidate in step.candidates[:CANDIDATE_MAX_ITEMS_PER_MEMBER]:
            block.append(candidate.describe())
        hidden = step.candidate_total - len(step.candidates)
        if hidden > 0:
            block.append(
                f"（{step.plan.label} 另有 {hidden} 条候选因篇幅未列出；"
                "平台记着它们，需要时可以让它只报那几条。）"
            )
        if step.outcome is not None and not step.candidates:
            # 报了「没有发现」也要留下痕迹：与「没跑成」是两件事。
            excerpt = _markdown_excerpt(step.outcome)
            block.append(
                f"（{step.plan.label}：**没有报出符合门槛的结论**"
                + (f"；它的报告片段：\n{excerpt}" if excerpt else "。")
                + "）"
            )
        if not block:
            continue
        text = f"### {step.plan.label}（{'、'.join(step.plan.dimensions)}）\n\n" + "\n\n".join(block)
        total += len(text)
        if total > CANDIDATE_BLOCK_MAX_CHARS:
            lines.append(
                f"（候选结论过多，{step.plan.label} 起的部分未列出 —— 本次共 "
                f"{sum(len(item.candidates) for item in steps)} 条候选。）"
            )
            break
        lines.append(text)
    if not lines:
        return ""
    return "## 各分片报出的候选结论\n\n" + "\n\n".join(lines)












UNCLASSIFIED_TITLE = f"## {UNCLASSIFIED_LABEL}（不在本次维度清单内的条目）"

UNCLASSIFIED_INTRO = (
    "下面这几条的 `category` 不在本次生效的维度清单里。**平台把它们原样保留了下来"
    "（一条都没有丢弃）**，但它们不属于清单上的任何一个维度，所以单独列在这里。"
    "请人工判断该归到哪里；如果它其实属于本项目该有的一个维度，"
    "就把它加进知识包 `references/project-facts.md` 的 `dimensions` 声明里。"
)


def build_unclassified_section(
    anomalies: Sequence[Anomaly], dimension_ids: Sequence[str]
) -> str:
    """把**落不进本次维度清单**的条目单独列成报告里的一节。

    ## 为什么要有这一节（而且必须在报告里，不只是 trace）

    换成非配表项目之后，真实的发现（性能回归、协议不兼容、资源引用丢失…）很容易落不进
    清单里的任何一个 id。若那些条目只是被「保留在异常清单里」，读报告的人看到的是
    一份**看起来完全正常**的报告 —— 少的那一条没有任何人会发现。所以平台在这里显式地
    说：这几条不属于任何维度，你们要人工过一眼。

    「保留」这件事发生在解析层（`protocol._coerce_anomalies` 不再按集合丢弃），
    「点名」发生在这一层 —— 因为只有这一层手里有**本次生效的清单**
    （`plan.synthesis.dimensions`，来自 `LoadedSkills.dimensions`）。
    """
    ids = tuple(str(item).strip() for item in dimension_ids if str(item or "").strip())
    items = unclassified_anomalies(anomalies, ids)
    if not items:
        return ""
    lines: list[str] = [
        UNCLASSIFIED_TITLE,
        "",
        UNCLASSIFIED_INTRO,
        "",
        f"本次生效的维度清单（{len(ids)} 个）：" + "、".join(f"`{item}`" for item in ids),
        "",
    ]
    for index, item in enumerate(items, start=1):
        category = item.category or "（未标注）"
        lines.append(
            f"{index}. **{item.title}**（category = `{category}` · "
            f"严重度 {item.severity} · 置信度 {item.confidence}）"
        )
        if item.file_path:
            lines.append(f"   - 文件：`{item.file_path}`")
        for evidence in item.evidence[:3]:
            lines.append(f"   - 证据：{truncate_text(str(evidence), CANDIDATE_TEXT_MAX_CHARS)[0]}")
    return "\n".join(lines).rstrip() + "\n"


CAP_TITLE = "## 结论条数上限（平台补充）"


def build_cap_section(dropped: Sequence[DroppedItem]) -> str:
    """把**被上限截掉的那几条**单独列成报告里的一节。

    ## 为什么要有这一节

    条数上限（`RuleThresholds.max_anomalies`，默认 10）生效时，清单是**静默变短**的：
    截断发生在归一化那一层，报告正文读起来完全正常，少的几条没有任何痕迹。
    「这次只报了 10 条」与「这次恰好有 10 条」在界面上长得一模一样 —— 而前者意味着
    还有几条**按严重度排在后面、这次没列出来**的结论，用户只要多问一次就能拿到。

    记账本身一直有（`cap_anomalies` 逐条记进 `dropped`），但那份记账只有 trace 读，
    报告里没有。所以这一节补的就是「读报告的人能不能知道」。

    ## 与「信息缺口」的区别（两节不能合成一节）

    信息缺口说的是「**没看到**」（分片没跑成、候选没进报告）；这一节说的是
    「**看到了、也成立、但没位置**」。合成一节，读的人会把「被截掉」理解成「没查到」，
    于是要么去补一次根本没缺的分析，要么以为这几条已经被否掉了。

    `dropped` 传整个列表进来（不是只传这一种），按 `kind` 自己挑 —— 调用点手里只有
    那一份 outcome 的记账，让它先过滤一遍等于把 `KIND_ANOMALY_CAP` 这个约定复制出去。
    """
    items = [item for item in dropped if getattr(item, "kind", "") == KIND_ANOMALY_CAP]
    if not items:
        return ""
    limit = _cap_limit_of(items[0])
    lines = [
        CAP_TITLE,
        "",
        f"上面的结论清单**没有列全**：本次条数上限是 {limit} 条，另有 {len(items)} 条"
        "**被截掉了**。它们不是「没查到」，也不是「不成立」—— 是**这次没有位置**"
        "（保留与截断都按严重度从高到低，被截掉的都排在保留的那些之后）。"
        "要多拿到这几条，把上限调大之后重跑一次即可；下面是它们的线索：",
        "",
    ]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item.detail or '（平台没有记下标题）'}")
    return "\n".join(lines).rstrip() + "\n"


def _cap_limit_of(item: DroppedItem) -> str:
    """从记账的措辞里取回上限值，取不到就说「未记录」。

    **不从 `thresholds` 再读一次**：这一节渲染的是**当时那次运行**的事实，而
    `thresholds` 是调用方此刻手里的值 —— 两者在「用户改了配置、翻看旧报告」时不是
    同一个数。记账里带着当时的值，就只从记账里取。
    """
    matched = re.search(r"上限（(\d+) 条）", item.reason or "")
    return matched.group(1) if matched else "未记录"


def verify_section(step: MemberOutcome) -> str:
    """对账轮跑成之后追加到报告末尾的那一节（含抬头，模型写的那段原文在下面）。

    **模型写的那段要降一级标题再贴**：它本身就是一份完整报告（7 个一级标题一个不少），
    原样贴进来整份文档就有两套一级标题。理由与实现见
    `report_document.demote_headings`（放在那边是因为本文件已经 1800+ 行，而它是纯
    markdown 工具，与文档结构那件事同一处）。
    """
    body = (step.outcome.report_markdown or "").strip() if step.outcome else ""
    if not body:
        return ""
    return (
        "## 对账结果（找反证）\n\n"
        "以下是**对照着去推翻**前面那几条结论的结果：平台在汇总之后又跑了一次独立核对，"
        "要它去找反证（能证明某条结论不成立的具体文件与行）。两种答复都算结论 ——"
        "「反证成立」意味着那一条**不该按原样采信**；「未找到反证」意味着有人去找过、没找到。\n\n"
        + demote_headings(body)
    )


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

    ## `run_analysis_fn`

    默认就是引擎的 `run_analysis`；测试用它注入假实现（本模块因此不必碰网络）。
    """
    steps: list[MemberOutcome] = []
    body_cache: dict[Any, ContextItem] = {}
    spent_tokens = 0
    for member in plan.members:
        reason = should_skip(member, spent_tokens) if should_skip is not None else ""
        if reason:
            steps.append(MemberOutcome(plan=member, skipped_reason=reason))
            continue
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
        )
        steps.append(step)
        spent_tokens += _tokens_of(step.outcome)

    synthesis_outcome = _run_synthesis(
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
    )
    synthesis_step = MemberOutcome(
        plan=plan.synthesis,
        outcome=synthesis_outcome,
        error="" if synthesis_outcome.status != STATUS_FAILED else synthesis_outcome.error_message,
    )
    steps.append(synthesis_step)

    # 对账轮**在汇总之后**：它核对的正是汇总出来的那份报告（最高严重度那几条），放在
    # 汇总之前没有东西可核。它用的也是共享前缀，所以那一轮同样吃缓存。
    #
    # 汇总没跑成时**不跑它**：它核对的对象就是那份报告，而报告不存在 —— 跑下去要么是空转
    # （任务书里只剩「主代理没报出任何结论」那一支），要么是让模型对着一份失败的运行凭空
    # 产出「对账结论」。这一条与预算无关，所以不走 `should_skip`。
    if plan.verify and synthesis_outcome.status != STATUS_FAILED:
        # 预算早停同样管它：这时再花一整份提示词去买一道**复核**，而复核的对象（报告）
        # 已经产出了 —— 跳过它并在报告里点名（`DEGRADE_VERIFY`）比挤掉下一个版本更划算。
        verify_member = MemberPlan(
            index=plan.count + 2, label=VERIFY_LABEL, role=ROLE_VERIFY, dimensions=()
        )
        reason = should_skip(verify_member, spent_tokens) if should_skip is not None else ""
        if reason:
            steps.append(MemberOutcome(plan=verify_member, skipped_reason=reason))
        else:
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
    )
    return FamilyResult(outcome=outcome, steps=tuple(steps), candidates=candidates)


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


def _tokens_of(outcome: EngineOutcome | None) -> int:
    """一个成员烧掉多少 token。没跑成的算 0（上游没报的也算 0）。

    **这里刻意不返回 `None`**：它的用途是 `early_stop_guard` 的**下界**——「已经确定烧掉
    的」够不够触发提前收尾。读不到的那部分按 0 算等于「先不拦」，代价是最多多跑一轮；
    反过来把它当成已知就会凭一个猜出来的数掐断一次正在进行的分析。
    （落库那一份走的是 `_sum_optional`，读不到就是 `None` —— 两者读者不同，口径也应当不同。）
    """
    if outcome is None:
        return 0
    return max(0, int(outcome.prompt_tokens or 0)) + max(0, int(outcome.completion_tokens or 0))


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
) -> MemberOutcome:
    """跑一个成员（子代理或汇总），把它报出的候选结论抽出来。

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
        limits=plan.limits,
        thresholds=thresholds,
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
        baseline_digest=baseline_digest,
        plan=plan,
        member=member,
        task_message=build_member_task(member, plan),
        on_round=on_round,
        on_start=on_start,
        body_cache=body_cache,
        run_analysis_fn=run_analysis_fn,
    )
    candidates = _candidates_of(member, outcome)
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


def _candidates_of(member: MemberPlan, outcome: EngineOutcome) -> tuple[Candidate, ...]:
    raw: Sequence[Anomaly] = ()
    if outcome.payload is not None:
        raw = outcome.payload.anomalies
    items = tuple(
        Candidate(member_label=member.label, index=position, anomaly=item)
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
) -> MemberOutcome:
    """跑对账轮。它**不是分片**：维度是空的，`role` 是 `verify`，面板上标签是 `V1`。

    `member` 由 `run_family` 建好传进来（那里要用它先问一次预算），所以这里不再自己造一个 ——
    两处各造一个的下场是 `index` / `label` 迟早对不上，而标签正是面板上的那一列。
    """
    outcome = _call_engine(
        client=client,
        provider=provider,
        loaded=loaded,
        scope=scope,
        change_summary=change_summary,
        limits=plan.limits,
        thresholds=thresholds,
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
        baseline_digest=baseline_digest,
        plan=plan,
        member=member,
        task_message=build_verify_task(plan, synthesis),
        on_round=on_round,
        on_start=on_start,
        body_cache=body_cache,
        run_analysis_fn=run_analysis_fn,
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
) -> EngineOutcome:
    return _call_engine(
        client=client,
        provider=provider,
        loaded=loaded,
        scope=scope,
        change_summary=change_summary,
        limits=plan.limits,
        thresholds=thresholds,
        project_knowledge=project_knowledge,
        project_instructions=project_instructions,
        baseline_digest=baseline_digest,
        plan=plan,
        member=plan.synthesis,
        task_message=build_synthesis_task(plan, steps),
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


def aggregate_outcomes(
    *,
    synthesis: EngineOutcome,
    steps: Sequence[MemberOutcome],
    candidates: Sequence[Candidate] = (),
    dimensions: Sequence[str] = DIMENSION_IDS,
) -> EngineOutcome:
    """把一家子的账合成**一个** `EngineOutcome`（落库那一层只认一个）。

    ## `dimensions` 是**本次生效的维度清单**

    `run_family` 传的是 `plan.synthesis.dimensions`（来自 `LoadedSkills.dimensions`）。
    它只用于一件事：把落不进清单的条目单独列成「未归类」那一节（`build_unclassified_section`）
    —— 那一节是「一条发现都不许消失」的最后一道保证。默认值是平台出厂那九个，
    供直接调用本函数的调用方（测试）使用。

    ## 轮次为什么要重编号

    `ai_analysis_trace` 上有 `uq_ai_trace_run_round`（run_id + round_index 唯一），而一家子
    只落**一条运行**。所以每个成员的轮次在这里被重编成**家族内全局递增**的序号
    （顺序执行 → 确定），每个成员自己那几轮另存在 `agent_round` 里（界面显示「S1 · 第 2/4 轮」
    用它）。这样那个唯一约束一个字节都不用改。

    ## 缓存 token 用 `_sum_optional` 的口径

    任一成员没上报就是 `None`（**不是 0**）：一轮报了 900/1000、另一个没报，只把报了的那几
    个加起来会得出一个偏高、且看起来完全正常的命中率。
    """
    report = synthesis.report_markdown or ""
    # 对账轮那一节排在「信息缺口」**之前**：它是对报告本身的补充（结论该不该采信），
    # 而信息缺口是「哪些东西没看到」—— 后者永远在最后，读的人一眼能看到缺口在哪。
    verify_text = "".join(
        verify_section(step) for step in steps if step.plan.role == ROLE_VERIFY
    )
    if verify_text and synthesis.status != STATUS_FAILED:
        report = (report.rstrip() + "\n\n" + verify_text).strip() + "\n"
    # 「未归类」也排在信息缺口之前、对账轮之后：它对读者同样是**结论的一部分**
    # （有几条发现不属于任何维度），而信息缺口永远收尾。
    unclassified_text = build_unclassified_section(synthesis.anomalies, dimensions)
    if unclassified_text and synthesis.status != STATUS_FAILED:
        report = (report.rstrip() + "\n\n" + unclassified_text).strip() + "\n"
    # 「结论条数上限」排在「未归类」之后：两节都是**对上面那份结论清单本身的补充**
    # （一节说「有几条不属于任何维度」，一节说「有几条根本没列进来」），而信息缺口
    # 说的是「没看到」，永远收尾。
    cap_text = build_cap_section(synthesis.dropped)
    if cap_text and synthesis.status != STATUS_FAILED:
        report = (report.rstrip() + "\n\n" + cap_text).strip() + "\n"
    gaps_text, gap_dropped = reconcile_candidates(candidates, synthesis, steps=steps)
    # 汇总没跑成时**没有报告**：那段缺口说明写进 `error_message`（见下面那个分支）。
    # 往一份空报告后面追加一段「信息缺口」等于凭空造出一份看得见的报告，而这次其实
    # 什么都没得到 —— 两份东西都不能给。
    if gaps_text and synthesis.status != STATUS_FAILED:
        report = (report.rstrip() + "\n\n" + gaps_text).strip() + "\n"

    rounds = _merge_rounds(steps)
    dropped = tuple(item for step in steps if step.outcome for item in step.outcome.dropped)
    dropped = (*dropped, *gap_dropped)
    # 「额度用尽没轮到的那几块」同样要跨成员合起来。只留计数的话，报告末尾只能说
    # 「有 N 个请求没执行」，说不出是哪几个 —— 而这一家子有几个成员，缺的那几块可能
    # 分别来自不同成员。
    refused_requests = tuple(
        label for step in steps if step.outcome for label in step.outcome.refused_requests
    )

    has_gap = _shard_never_ran(steps) or bool(gap_dropped)
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
        anomalies=synthesis.anomalies,
        dropped=dropped,
        refused_requests=refused_requests,
        report_markdown=report,
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
        "label": step.plan.label or "汇总",
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
