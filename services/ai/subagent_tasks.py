"""分片的任务书与提示词构造：把一份 `FamilyPlan` 摊成写给每个成员的消息。

## 这个模块为什么单独存在

从 `services/ai/subagent.py` 拆出来（2026-09-23）：那个文件顶着仓库 2000 行的 ERROR 闸门
（`scripts/check_file_length.py`）。这一段本身是**纯构造** —— 输入是计划（`FamilyPlan`
与它的 `FamilyQuota`）、已加载的 skill、变更清单与额度，输出是一串消息；它不碰库、不跑
引擎、不读时钟。所以拆开之后它单独好测，而 `subagent.py` 那边只留下「怎么跑」与「怎么汇总」。

## 家族计划的两个类也在这里

`FamilyQuota` / `FamilyPlan` 跟着一起搬，是**为了让依赖只有一个方向**：本模块要用它们，
`subagent.py` 也要用（`plan_family` 造、`run_family` 花）。留在 `subagent.py` 就得由本模块
反过来 import 它 —— 那是环。搬过来之后本模块不 import `subagent`，方向单一。

## 回导

`subagent.py` 仍然把这里的名字**按原样重导出**（`from services.ai.subagent_tasks import …`）：
`services/ai/engine.py:1438` 是**函数内**延迟 import 这几个名字的
（`build_cap_section` / `build_unclassified_section`，模块级互导会成环），而十几个测试
一直是从 `services.ai.subagent` 取它们。搬家不改这些调用点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from services.ai.budget import truncate_text
from services.ai.engine import EngineLimits, EngineOutcome
from services.ai.family_ledger import (
    CANDIDATE_BLOCK_MAX_CHARS,
    CANDIDATE_MAX_ITEMS_PER_MEMBER,
    CANDIDATE_TEXT_MAX_CHARS,
    EvidenceRef,
    MemberOutcome,
    MemberPlan,
    _gap_lines,
    _markdown_excerpt,
    evidence_refs_for,
)
from services.ai.prompt import build_system_prompt, build_user_message
from services.ai.prompt_cache import mark_cache_breakpoint
from services.ai.protocol import Anomaly, DroppedItem, unclassified_anomalies
from services.ai.report_document import demote_headings
from services.ai.rules import KIND_ANOMALY_CAP
from services.ai.skill_contract import UNCLASSIFIED_LABEL, dimension_ids_of
from services.ai.skill_loader import LoadedSkills
from services.ai.verdict import assign_findings, strip_verdict_block, verdict_instructions

# 对账轮一次核对几条。**只核对最严重的几条**：对账要的是深度（去找反证、指出证据够不够），
# 而不是把整份报告重读一遍 —— 后者正是汇总那一次已经做过的事。
DEFAULT_VERIFY_ITEMS = 3
MAX_VERIFY_ITEMS = 5

# 一个成员至少要有的索取额度与轮次。低于它就别拆了：一个只有 2 次索取的成员既看不深，
# 又要多花一次整份提示词的钱，得不偿失。
MIN_MEMBER_TOOL_REQUESTS = 2
MIN_MEMBER_ROUNDS = 2

def _percent_of(total: int, percent: int) -> int:
    """`total` 的 `percent`%，**向上取整**。

    向上取整是这里的口径：这条规则是「每个成员**至少**有设置值的 N%」，取整取小了就
    违背了它（40 的 70% = 28 正好，25 的 70% = 17.5 取 17 就少给了）。用整数算，
    不引 float —— 额度是要写进提示词、也要与数据库里的整数比对的量。
    """
    total = max(0, int(total))
    return -(-total * int(percent) // 100)


@dataclass
class FamilyQuota:
    """家族共享池的账本（`run_family` 持有，串行滚动 —— 2026-09-23 起）。

    ## 为什么共享：runs 38~41 的实测浪费

    每代理各一份额度时，重分片撞顶降级、轻分片的富余**永远没有出路**：run 41 的 S2
    撞满 50 次索取 + 10 轮双顶（`requests_exhausted`），而 S1 只用 32、S3 只用 36 ——
    18 + 14 次富余白放着。分片本来就是**串行**执行的，前片用剩的滚动给后片天然安全，
    也不需要动 `ContextTools` 的实例字段（它有「不许共享实例」的既有纪律）：编排层
    在每片开跑前算一份「当片上限」，跑完按实耗扣池。

    ## 上限怎么算：名义额保底 + 富余滚动

    每片**保证拿到名义额**（后面几片的名义额在开跑前就被预留），前面省下来的部分才滚
    给当前这片 —— 富余流向**最早需要它的分片**，但谁也不会因此被压到名义额以下：

        cap_i = 名义额 + max(0, 池剩余 − 名义额 − 后面几片的名义额 − 汇总下限)

    汇总那一次**保下限、可越池**（它是唯一产出最终报告的一步，不许被前面的分片饿死）；
    对账轮只用剩余、不保下限（它本来就是可跳过的一道）。

    扣账按**实跑数**：轮次按 `len(outcome.rounds)`（引擎的 `+2` 格式重问轮不计入名义
    额度，按实耗扣就不会双记），索取按 `outcome.requests_used`。被 `should_skip` 跳过的
    成员不扣池 —— 但它的名义额在后面的预留里已经按「会跑」算过，方向保守（多留少花），
    与「判错的方向必须落在多写一次」同一条纪律。
    """

    requests_pool: int
    rounds_pool: int
    requests_nominal: int
    rounds_nominal: int
    requests_used: int = 0
    rounds_used: int = 0

    def shard_caps(self, members_after: int) -> tuple[int, int]:
        """一个分片开跑前的「当片上限」：`(索取上限, 轮次上限)`。

        `members_after` 是本片**之后**还要跑的分片数（不含汇总）；汇总的下限在这里
        一并预留（`MIN_MEMBER_*`）。
        """
        return (
            self._cap(
                left=self.requests_left,
                nominal=self.requests_nominal,
                reserved=members_after * self.requests_nominal + MIN_MEMBER_TOOL_REQUESTS,
            ),
            self._cap(
                left=self.rounds_left,
                nominal=self.rounds_nominal,
                reserved=members_after * self.rounds_nominal + MIN_MEMBER_ROUNDS,
            ),
        )

    def synthesis_caps(self) -> tuple[int, int]:
        """汇总那一次的上限：池内剩余全给，但保下限、**可越池**（不许被饿死）。"""
        return (
            max(self.requests_left, MIN_MEMBER_TOOL_REQUESTS),
            max(self.rounds_left, MIN_MEMBER_ROUNDS),
        )

    def verify_caps(self) -> tuple[int, int]:
        """对账轮的上限：只用池内剩余，不保下限（可跳过的一道）。"""
        return (self.requests_left, self.rounds_left)

    def spend(self, requests_used: int, rounds_used: int) -> None:
        """跑完一个成员按实耗扣池。"""
        self.requests_used += max(0, int(requests_used or 0))
        self.rounds_used += max(0, int(rounds_used or 0))

    @property
    def requests_left(self) -> int:
        return max(0, self.requests_pool - self.requests_used)

    @property
    def rounds_left(self) -> int:
        return max(0, self.rounds_pool - self.rounds_used)

    @staticmethod
    def _cap(*, left: int, nominal: int, reserved: int) -> int:
        """名义额 + 富余，且不许超池、池空时如实给 0（引擎会如实降级）。"""
        if left <= 0:
            return 0
        rolled = max(0, left - nominal - reserved)
        return min(left, nominal + rolled)


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
    # 家族共享池（串行滚动，见 `FamilyQuota`）。`plan_family` 总是建好它；None 只留给
    # 直接手搓 `FamilyPlan` 的调用方（测试）—— `run_family` 会按 `limits × 成员数`
    # 重建一份，行为退回「名义额即池」。
    quota: FamilyQuota | None = None

    @property
    def all_steps(self) -> tuple[MemberPlan, ...]:
        return (*self.members, self.synthesis)

def build_seed_messages(
    *,
    loaded: LoadedSkills,
    change_summary: str,
    limits: EngineLimits,
    baseline_digest: str = "",
    project_knowledge: str = "",
    project_instructions: str = "",
    budget_line_override: str = "",
) -> tuple[Mapping[str, Any], ...]:
    """一家子共用的**前两条消息**：system + 含整份变更清单的第一条 user 消息。

    ## 家族口径的额度行（2026-09-23 起）

    `budget_line_override` 非空时，额度那一句用它（家族共享池的口径，见
    `_family_budget_line`）；空串 = 单代理口径（`_budget_line`），逐字不变。家族行是
    **家族常量的纯函数**（池总量、分片数、名义额都来自 `plan.quota`），所有成员拿到的
    是同一串字节 —— prompt cache 仍然全命中。「随成员变化的数字」（当片上限、已用量）
    只进成员私有的任务书，永远不进共享前缀。

    与「同额度的单代理运行」的**跨模式**逐字节相同（前两条消息）就此让位：家族行说
    的是共享池，单代理行说的是它自己的额度，两句话本来就不该是同一句。跨运行复用里
    最值钱的那一段（系统消息，断点①）不受影响；家族内部（N+1 个成员 + 对账轮）的
    复用是这套机制省钱的主体，完整保留。

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
        # 一处取的是 `loaded.dimensions`），否则家族成员之间的第 1 条消息不再逐字节
        # 相同 —— 而那是这套机制省钱的**全部**依据。
        dimension_ids=dimension_ids_of(loaded.dimensions),
        budget_line_override=budget_line_override,
    )
    return (
        mark_cache_breakpoint({"role": "system", "content": system_prompt}),
        mark_cache_breakpoint({"role": "user", "content": first_user}),
    )


def _family_budget_line(quota: FamilyQuota, shards: int) -> str:
    """共享前缀里那句「全家共享池」的额度行（家族常量的纯函数，见 `build_seed_messages`）。

    这句话模型会原样转述进报告，所以它必须自己站得住（`prompt._budget_line` 的教训）：
    池总量、分片数、名义额、滚动规则、用完的后果，一件不少。
    """
    return (
        f"本次分析由 {shards} 个分片代理串行深挖、共享一个索取池：全家共可索取 "
        f"{quota.requests_pool} 次上下文、跑 {quota.rounds_pool} 轮（跨轮次累计，"
        "重复索要同一个文件也计入）。每个分片先按名义额 "
        f"{quota.requests_nominal} 次索取规划，**你这一段的实际上限与全家已用的次数"
        "写在你的任务书里**；你用剩的额度会滚给后面的分片，所以按你需要去拿，"
        "不必为后面的分片省着。额度用完就只能基于已有证据出报告，"
        "所以请优先要最关键的。"
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
    """给计划装上共享前缀。**一家子只装一次**（顺序执行 → 也只用写一次缓存）。

    额度行走家族口径（`_family_budget_line`）；没有 `quota` 的手搓计划（测试）退回
    单代理口径，行为与从前一致。
    """
    return replace(
        plan,
        seed_messages=build_seed_messages(
            loaded=loaded,
            change_summary=change_summary,
            limits=plan.limits,
            baseline_digest=baseline_digest,
            project_knowledge=project_knowledge,
            project_instructions=project_instructions,
            budget_line_override=(
                _family_budget_line(plan.quota, plan.count)
                if plan.quota is not None
                else ""
            ),
        ),
    )


# --------------------------------------------------------------------------
# 任务书
# --------------------------------------------------------------------------


def _pool_note(
    quota: FamilyQuota, cap_requests: int, cap_rounds: int, *, role_line: str
) -> str:
    """成员私有任务书里的「额度账」：当片上限 + 全家已用（随成员变化，永不进共享前缀）。

    与共享前缀里那句家族口径行（`_family_budget_line`）配套：那句说池，这段说
    「你这一段」。两个数字必须一致地出现在同一份任务书里，模型才知道名义额之外的
    部分从哪来（前面分片省下的富余）。
    """
    return (
        f"{role_line}全家共享池 {quota.requests_pool} 次索取 / {quota.rounds_pool} 轮，"
        f"串行滚动；你开跑前全家已用 {quota.requests_used} 次索取 / {quota.rounds_used} 轮。"
        f"**你这一段的上限是 {cap_requests} 次索取、{cap_rounds} 轮**"
        f"（名义额 {quota.requests_nominal} 次 / {quota.rounds_nominal} 轮"
        "，名义额之外的部分是前面分片省下来的富余）。"
        "你用剩的会滚给后面的分片，所以按你需要去拿，不必为后面省着；"
        "额度用完就只能基于已有证据出报告。"
    )


def build_member_task(member: MemberPlan, plan: FamilyPlan, *, pool_note: str = "") -> str:
    """一个子代理的任务书（它第 1 轮的 user 消息，也是唯一私有的一段）。

    六件事都要说清，否则会出两种具体的错：**它以为自己的读取范围只有自己那几个维度**
    （于是跨模块耦合永远发现不了），或者**它以为没人管的维度要自己兜底**（于是重复劳动、
    还把别人的活干浅了）。
    """
    # 条数额度（见 `EngineLimits.max_anomalies_per_subagent`）。写进任务书的那个数字
    # **就是**额度本身：模型照它写，省下的是输出 token —— 事后封顶省不了这笔钱。
    cap = max(1, int(plan.limits.max_anomalies_per_subagent))
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
    if member.assigned_paths:
        visible = member.assigned_paths[:200]
        listed = "\n".join(f"- `{path}`" for path in visible)
        omitted = len(member.assigned_paths) - len(visible)
        tail = (
            f"\n- ……另有 {omitted} 个，请用 "
            "`read_reference` 读取 `change-manifest` 的后续分段"
            if omitted
            else ""
        )
        blocks.append(
            "## 确定性文件分工\n\n"
            f"平台给你分配了 {len(member.assigned_paths)} 个优先检查文件。每个变更文件"
            "至少属于一个分片；关键路径可能同时属于两个分片。\n\n"
            + listed
            + tail
        )
    blocks.extend(
        [
            (
                "## 输出要求\n\n"
                "与常规分析**完全一样**：走同一套 JSON 协议、同样的精度要求。"
                "唯一的差别是 `report_markdown` 只写**你这几个维度的发现与依据**"
                "（不必写整版报告，主代理会把它们汇总成最终报告）。\n\n"
                f"**条数额度：`anomalies` 最多 {cap} 条。** 这是额度不是目标 —— 按严重度"
                f"从高到低取前 {cap} 条，**低风险与小问题不用写**（它们本来也会被汇总那道"
                "按严重度挡在外面，写出来只是白花时间）。上面那句「宁可多报，由主代理去重」"
                f"是**在这 {cap} 条之内**说的：越出自己维度的要报，但别拿它把额度撑满。\n\n"
                "每条的 `evidence` 写到**看得懂就行**，不要为了显得严谨把整段文件贴进去。\n\n"
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
    if pool_note.strip():
        # 额度账是**私有段**（随成员变化），放共享前缀会毁掉 prompt cache。
        blocks.append("## 额度账（你这一段）\n\n" + pool_note.strip())
    return "\n\n".join(blocks).rstrip() + "\n"


def build_synthesis_task(
    plan: FamilyPlan,
    steps: Sequence[MemberOutcome],
    *,
    evidence_index: Mapping[str, EvidenceRef] | None = None,
    pool_note: str = "",
) -> str:
    """主代理的任务书：各分片的候选结论 + 谁没跑成 + 汇总纪律。

    ## 候选编号是**平台对账的唯一判据**（AI-P0-06）

    编号是**平台发出去的**（`[S1-2]`），所以平台能查「这一条进了最终报告没有」。查的
    方式在 2026-09-21 换过一次：以前是平台从报告正文、结论的文件路径和标题措辞里**反推**
    血缘（三手启发式），而真机上它产生的全是假缺口 —— 同名协议拆在两个文件里、标题换个
    说法、证据为空，三手就一条都对不上，于是被报成「找不到去向」，还把那一次运行判成了
    `subagent_gap` 降级。现在改为**显式血缘**：每条结论在 `source_candidate_ids` 里列出
    它来源于哪几条候选（可以一对多、多对一），平台只比这一组编号。

    **所以这一条纪律是硬的**：不带编号的结论，平台无法把它和候选对上；而一条编号都没
    交回时，平台的账只能笼统地说一句「无法按编号对账」——那是**读的人拿不到结论去向**
    的一种失败，不是「平台查不出问题」。
    """
    blocks = [
        "# 分工：你是主代理（汇总）",
        (
            f"本次周版本分析由 {plan.count} 个分片代理分头深挖，"
            "它们的候选结论都在下面。**你的任务是把它们审一遍、合成一份完整的报告**，"
            "而不是另起一份。"
        ),
        (
            "## 纪律（五条）\n\n"
            "1. **每一条候选都要有去向并可机器读取**：在顶层 `candidate_dispositions` 数组"
            "逐条写 `{candidate_id, status, reason}`；status 只能是 adopted、rejected、deferred。"
            "采纳用 adopted，明确不成立或重复用 rejected，证据不足待复核用 deferred。"
            "**不许一声不响地丢掉**。\n"
            "2. **采纳的每条结论都要带 `source_candidate_ids`**：列上它来源于哪几条候选，"
            "形如 `\"source_candidate_ids\": [\"S1-2\"]`。两条候选合成一条就列两个"
            "（`[\"S1-2\", \"S2-5\"]`），一条候选拆成两条就两条都写上它。"
            "**这是平台对账的唯一判据** —— 没带编号的结论，平台查不出这条候选的去向，"
            "报告末尾就会多出一段「需要人工看一眼」。\n"
            f"3. **各分片负责的维度必须都有交代**：系统提示词那份「本项目适用的维度清单」"
            f"上的 {len(plan.synthesis.dimensions)} 个维度一个都不能空着；"
            "某个维度没人报出问题，也要写 `hit: false` 与理由。\n"
            "4. **报告是最终报告**：七个章节写全，长度不受分片影响。"
            "你还可以用工具去核对候选里可疑的地方（文件、行号、提交），"
            "也可以补充分片漏掉的发现。"
            "候选的「证据」那一栏给的是**地址**（`@evidence_id=…`，后面写着那份原件的字数）："
            "要看原文时按 `{\"type\": \"evidence\", \"name\": \"<那个地址>\"}` 索取，"
            "平台会把原件原样附回来（不重新取数）；**不要凭地址猜内容**，"
            "也不要因为它是一条地址就当成「证据缺失」——地址就是证据的入口。\n"
            "5. **篇幅要收着写**：单次输出有硬上限，写超了会被**截断**，"
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

    candidates_block = _render_candidates(plan, steps, evidence_index)
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
        + (
            pool_note.strip()
            if pool_note.strip()
            else (
                f"你和每个分片代理的额度是一样的（各 {plan.limits.max_tool_requests} 次索取、"
                f"最多 {plan.limits.max_rounds} 轮）。"
            )
        )
        + "\n\n汇总不需要重新通读整批，把额度花在核对可疑条目上。\n\n"
        + f"每个分片代理报上来的条目**最多 {max(1, int(plan.limits.max_anomalies_per_subagent))} "
        "条**（平台给的额度，按严重度取的前几条）。所以候选清单是**有上限的抽样**，"
        "不是「全版本只有这些」—— 别因为条数少就推断这个版本没问题。"
    )
    return "\n\n".join(blocks).rstrip() + "\n"


def build_verify_task(
    plan: FamilyPlan,
    synthesis: EngineOutcome,
    *,
    evidence_index: Mapping[str, EvidenceRef] | None = None,
    pool_note: str = "",
) -> str:
    """对账轮的任务书：把最严重的几条交出去，**要求它去找反证 + 给出结构化裁决**。

    ## 为什么是「找反证」，而不是「再评审一遍」

    汇总那一次已经在评审了，再问一遍「你觉得对不对」得到的只会是同一批理由的复述 ——
    而且模型对自己刚写下的结论天然是**确认偏误**的一方。所以这一轮的指令是反过来的：
    你的任务是**推翻它们**，每条都要给出「能证明它不成立的具体文件与行」，找不到就明说
    「未找到反证」。这两种答复都必须是**有代价的**：说「未找到」也要写明你去哪里找过。

    ## 每条发现带一个平台发的编号（`F1`…）

    对账轮的正文写「第 1 条建议降级」这种话，平台**没法可靠地把它对上任何一条结论**
    （模型的编号、措辞、顺序都可能与清单不同）。所以编号由平台发出去：清单里每条发现
    在任务书里带 `[F1]`，裁决按这个编号回。编号与清单的次序都由
    `verdict.assign_findings` 定（与封顶同一套严重度排序），三处必然一致。

    ## 结构化裁决（`verdict.verdict_instructions`）

    这一块就是 AI-P0-02 的入口：以前对账轮写的「建议撤掉 / 建议降级」只能停在一段文字里，
    平台的结论清单一个字都不改（实测那次：报告正文写「反证成立…建议撤掉」，异常表里
    仍是 `critical`/`very_high`）。现在它按 `verdicts` 数组回，平台按编号逐条应用。

    ## 只核对最严重的几条

    对账要的是深度（证据够不够、有没有误报），不是覆盖面 —— 覆盖是汇总那一次的事。
    所以按严重度取前 `plan.verify_items` 条（默认 3）。这一轮不额外分索取额度：
    核对几条结论用剩下的额度足够，而多分一份就会让每个分片的额度少一点。

    ## 它用同一份共享前缀

    对账轮的请求形状与分片一致（system + 共享变更清单 + 本任务书），所以**它也吃缓存** ——
    这是一次额外的模型调用里最贵的那一段。
    """
    # 排序复用 `verdict.assign_findings`（内部就是 `rules.rank_anomalies`：严重度 → 置信度，
    # 同档保持模型给的顺序）—— 对账要挑的「最严重的几条」必须与封顶时挑的是同一个口径，
    # 两套排序迟早会不一致；而编号也由它发，两处各排一次会让**裁决打到另一条结论上**。
    ranked = assign_findings(synthesis.anomalies)[: plan.verify_items]
    blocks = [
        "# 分工：你是对账轮（找反证）",
        (
            f"本次周版本分析由 {plan.count} 个分片代理分头深挖、主代理汇总成了报告，"
            "已经交给评审者。**你的任务不是再评审一遍，而是去找它们的反证。**"
        ),
        (
            "## 纪律（五条）\n\n"
            "1. **对下面每一条，明确回答「反证成立」还是「未找到反证」**，两种都要给依据。\n"
            "2. **反证必须落到具体位置**：哪个文件、哪几行、哪个配置行 —— 指不出来就不算反证，"
            "只能写成「未找到反证（我查了哪里、没查到）」。\n"
            "3. **不要重复确认它是**：原文里的理由不是新证据；你要找的是**能推翻它的东西**"
            "（这段代码根本不会走到、这个字段在这次改动里没变、这个判定在服务端另有一道校验、"
            "这是阶段性屏蔽…）。\n"
            "4. **确实推翻不了就如实说**。「未找到反证」是一个完全合格的答复，"
            "编一条反证比说没找到糟得多。\n"
            "5. **篇幅要收着写**：单次输出有硬上限，写超了会被**截断**，整份 JSON 作废、"
            "你这一轮的核对结果一条都留不下（实测有过：28k token 的回答断在半截）。"
            "同类结论合并成一条写，只写能改变判断的内容。"
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
        for index, finding in enumerate(ranked, start=1):
            anomaly = finding.anomaly
            lines.append(f"### {index}. [{finding.finding_id}] {anomaly.title}")
            lines.append(f"- 维度：{anomaly.category} · 严重度 {anomaly.severity}")
            if anomaly.file_path:
                lines.append(f"- 位置：{anomaly.file_path}")
            if anomaly.impact:
                lines.append(f"- 它说会造成：{truncate_text(anomaly.impact, CANDIDATE_TEXT_MAX_CHARS)[0]}")
            refs = evidence_refs_for(evidence_index or {}, anomaly.file_path)
            if refs:
                # 找反证要读的正是**那份正文**。原先这里贴的是结论自己的复述（模型写的话），
                # 而反证必须落到「哪个文件、哪几行」上 —— 给地址让它自己去读那一份，
                # 比让它对着复述推理更接近这一轮的目的。
                for ref in refs:
                    lines.append(f"- 它的证据：{ref.describe()}")
            else:
                for evidence in anomaly.evidence[:3]:
                    lines.append(f"- 它的证据：{truncate_text(str(evidence), CANDIDATE_TEXT_MAX_CHARS)[0]}")
        blocks.append("## 待核对结论（逐条回答）\n\n" + "\n".join(lines))
        blocks.append(verdict_instructions())
    blocks.append(
        "## 输出\n\n"
        "走同一套 JSON 协议（`status` / `report_markdown` / `anomalies` 都一样）。"
        "对账结论写在 `report_markdown` 里，**一条一段**，形如 "
        "「第 1 条 ××：反证成立 —— <哪个文件哪一行说明了什么>」或 "
        "「第 1 条 ××：未找到反证（查了 <文件/行>）」；"
        "`anomalies` 只放**你在找反证的过程中新发现的**问题，没有就给空数组"
        "（这些新发现平台会照收 —— 与主结论同一道校验之后合入清单）。"
    )
    if pool_note.strip():
        # 对账轮的额度账：只用池内剩余、不保下限 —— 它本来就是可跳过的一道。
        blocks.append("## 额度（你这一段）\n\n" + pool_note.strip())
    return "\n\n".join(blocks).rstrip() + "\n"


def _render_candidates(
    plan: FamilyPlan,
    steps: Sequence[MemberOutcome],
    evidence_index: Mapping[str, EvidenceRef] | None = None,
) -> str:
    """把各成员报出的候选结论渲染成任务书里的一段（含「还有几条没列出来」）。

    `evidence_index` 是**共享证据仓**的地址表（`evidence_index_of(body_cache)`）。给了它，
    每条候选「证据」那一栏就换成地址（`file_diff @evidence_id=…（原文 N 字，按需索取）`）——
    汇总/对账要看正文时按地址取原件，任务书里不再复述一遍。查不到地址的候选照旧贴它
    自己写的那几行（**空元组是常态**，不是异常）。
    """
    lines: list[str] = []
    total = 0
    for step in steps:
        if not step.ran:
            continue
        block: list[str] = []
        for candidate in step.candidates[:CANDIDATE_MAX_ITEMS_PER_MEMBER]:
            block.append(candidate.describe(evidence_index))
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

    **它回的那个结构化裁决块要摘掉**（`verdict.strip_verdict_block`）：那一份已经由平台
    按裁决**结果**渲染成「复核标注（平台）」一节，原样留着只会让同一件事在报告里出现
    两遍，其中一遍还是未加工的 json。摘掉的是**字节区间**，正文一个字不动。
    """
    body = (step.outcome.report_markdown or "").strip() if step.outcome else ""
    if not body:
        return ""
    return (
        "## 对账结果（找反证）\n\n"
        "以下是**对照着去推翻**前面那几条结论的结果：平台在汇总之后又跑了一次独立核对，"
        "要它去找反证（能证明某条结论不成立的具体文件与行）。两种答复都算结论 ——"
        "「反证成立」意味着那一条**不该按原样采信**；「未找到反证」意味着有人去找过、没找到。\n\n"
        + demote_headings(strip_verdict_block(body))
    )
