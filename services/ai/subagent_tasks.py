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
from services.ai.scope import normalize_path
from services.ai.skill_contract import UNCLASSIFIED_LABEL, dimension_ids_of
from services.ai.skill_loader import LoadedSkills
from services.ai.verdict import (
    Finding,
    assign_findings,
    strip_verdict_block,
    verdict_instructions,
)

# 对账轮一次核对几条。**只核对最严重的几条**：对账要的是深度（去找反证、指出证据够不够），
# 而不是把整份报告重读一遍 —— 后者正是汇总那一次已经做过的事。
DEFAULT_VERIFY_ITEMS = 3
MAX_VERIFY_ITEMS = 5

# 一个成员至少要有的索取额度与轮次。低于它就别拆了：一个只有 2 次索取的成员既看不深，
# 又要多花一次整份提示词的钱，得不偿失。
MIN_MEMBER_TOOL_REQUESTS = 2
MIN_MEMBER_ROUNDS = 2

# 对账轮每条结论预留多少次索取：它要**去找反证**（读那个文件、查那个标识符的另一端），
# 每条至少要留「读一处、核一处」的余量。
VERIFY_REQUESTS_PER_ITEM = 2
# 预留的**下限**（要核的条数很少时也要真去查一次）与**上限**（不许它吃掉池的一大块）。
MIN_VERIFY_REQUESTS = 3
MAX_VERIFY_REQUESTS = 12
# 对账轮预留的轮次：一次找反证 + 一次按地址补读。
VERIFY_ROUNDS_RESERVE = 2
#: 对账轮**每条结论**最多留几条依据。它与常规轮不同：常规轮的 `evidence` 是「支撑这条
#: 结论的几个坐标」（3 条是防注水），而**对账轮的 `evidence` 是它核过的清单** —— 一条
#: 结论有几条断言、每一条核自哪里，本来就多于 3 条。沿用常规轮那个 3 会把它核过的部分
#: 抹掉：实测 run 58 有 5 条对账轮的结论被那句「仅保留前 3 条」削过，而**削掉的正是
#: 「这一处我也去看过」** —— 报告里那句「已核」于是没有对应的记录可回看。
#:
#: 仍然要有个上限（模型可以把依据列到几十条），取 12：够覆盖「读 diff + 读正文 + 查
#: 引用 + 逐条断言的坐标」这一整套，又不至于让一条结论的依据长过它自己的正文。
VERIFY_MAX_EVIDENCE = 12

#: 任务书里逐条列出的必读文件上限。**它是提示词长度与「照做」之间的折中**：
#: 列太多会把分片任务书撑大（而每个成员都要付这一份的钱），列太少则「必读」这句话
#: 落不到具体文件上。超出的那些由 `change-manifest` 分段给出（同样带仓库与提交）。
#:
#: 旧值是 200，那时每行只有一个裸路径；现在每行要写「路径 + 提交 + 仓库」三样，
#: 同样的行数会长出两倍多，所以收紧到 60。真实批次里每片的分配量在几十条这一档
#: （实测 run 58：120 个文件分给若干片），60 够用；**它绑住的是超大批次**，
#: 而那种情况本来就不该靠任务书把文件名逐个念完。
MANDATORY_LIST_MAX_ITEMS = 60

#: 断言类型的**中文说法**（给复核轮看的那一份）。与 `protocol.CLAIM_KINDS` 一一对应 ——
#: 把 `negative_scope` 这种标识符直接印给模型，它会当成又一个要照抄的字符串，而不是
#: 一个「这类断言要查范围」的提示；而**哪一类要查范围**正是这一段话的全部目的。
_CLAIM_KIND_LABELS = {
    "fact": "正向事实",
    "negative_scope": "否定性范围声明：查过的范围必须覆盖它声称的范围",
    "inference": "推断：要机制证据，不是「我觉得会」",
}


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

    汇总那一次**保下限**（它是唯一产出最终报告的一步，不许被前面的分片饿死）；
    对账轮**规划时就有预留**（`verify_reserve`），分片与汇总都消费不到那一份 ——
    池的账是「分片怎么花 + 汇总的底线 + 对账的预留」三笔，谁也拿不走别人那一笔。

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
    #: 对账轮的**预留**（规划时划走，见 `verify_reserve`）。分片与汇总的上限都把这一份
    #: 减掉，对账轮则至少拿得到这一份 —— 于是「报告正文有 20 条结论、只裁 3 条」那种
    #: 「正文与复核状态不一致」不可能再因为额度而出现。
    verify_requests: int = 0
    verify_rounds: int = 0
    #: **启动前**的覆盖预检那句话（`mandatory_progress.coverage_precheck`）：分到各片的
    #: 必读文件比池还多时，它在这里留一份，由 `run_family` 在开跑前打进日志。
    #: 空串 = 够用（绝大多数批次）；它不参与任何额度计算。
    coverage_note: str = ""

    def shard_caps(
        self,
        members_after: int,
        *,
        mandatory_floor: int = 0,
        later_mandatory_floor: int = 0,
    ) -> tuple[int, int]:
        """一个分片开跑前的「当片上限」：`(索取上限, 轮次上限)`。

        `members_after` 是本片**之后**还要跑的分片数（不含汇总）；汇总的下限与**对账轮的
        预留**都在这里一并扣掉 —— 分片永远消费不到那一份（见 `verify_reserve`）。

        ## `mandatory_floor`：必读清单的**保底**（P1b）

        前两个参数来自 `mandatory_request_floor`：本片必读清单**超出名义额**的条数，以及
        后面几片各自的那个数。语义是「这一片至少要能把它分到的文件各读一次」——
        而读一次要一次索取，所以它是索取额度的下限。

        三个刻意的口径：

        * **减去名义额**再算保底。名义额本来就是这一片可自由支配的索取次数，一份 diff
          一次索取，`名义额 ≥ 必读条数` 时保底是 0 ⇒ **行为与加这一项之前逐字节相同**。
          直接用条数当保底会在每一条真实批次上生效，而它**不是**「多给」：池子就那么大，
          前片多拿意味着后片少拿（实测口径下第 4 片会被压到 10 次），与「保底」正好相反。
        * **后面的分片也要留**（`later_mandatory_floor`）。只保住当前这一片，等于把
          「必读清单」这件事推给最先跑的那一片 —— 而饿死恰恰发生在最后一片。
        * **只在索取这一维**。读 N 个文件要 N 次索取，但引擎一轮能执行多条，轮次数与
          文件数没有对应关系；给轮次也加一个按文件数的保底只会虚占池子。
        """
        floor = max(0, int(mandatory_floor or 0))
        return (
            self._cap(
                left=self.requests_left,
                nominal=self.requests_nominal,
                floor=floor,
                reserved=(
                    members_after * self.requests_nominal
                    + MIN_MEMBER_TOOL_REQUESTS
                    + self.verify_requests
                    + max(0, int(later_mandatory_floor or 0))
                ),
            ),
            self._cap(
                left=self.rounds_left,
                nominal=self.rounds_nominal,
                reserved=(
                    members_after * self.rounds_nominal
                    + MIN_MEMBER_ROUNDS
                    + self.verify_rounds
                ),
            ),
        )

    def synthesis_caps(self) -> tuple[int, int]:
        """汇总那一次的上限：池内剩余**减去对账轮的预留**，仍保下限。

        「保下限」这一档可以越池（汇总若连下限都拿不到，报告就没了）—— 但那一档只到
        下限为止，**不许多拿预留的那一份**：原先它取 `max(剩余, 下限)`，池见底时会把
        对账轮的最后一格也一起拿走。
        """
        return (
            max(
                self.requests_left - self.verify_requests,
                MIN_MEMBER_TOOL_REQUESTS,
            ),
            max(self.rounds_left - self.verify_rounds, MIN_MEMBER_ROUNDS),
        )

    def verify_caps(self) -> tuple[int, int]:
        """对账轮的上限：**至少是预留的那一份**，池里还剩的也一并给它（剩余回流）。

        不保「不越池」那一档之外的任何约束：汇总真的越池时，预留照样拿得到 —— 这正是
        预留的意义。它仍然是**可跳过的一道**（预算早停管得着它），跳过与否与额度无关。
        """
        return (
            max(self.verify_requests, self.requests_left),
            max(self.verify_rounds, self.rounds_left),
        )

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
    def _cap(*, left: int, nominal: int, floor: int = 0, reserved: int) -> int:
        """名义额 + 保底 + 富余，且不许超池、池空时如实给 0（引擎会如实降级）。

        `floor` 是**必读清单的保底**（见 `shard_caps`）：它不被富余的计算吞掉 ——
        富余是「别人省下来的」，保底是「这一片本来就该有的」，把保底并进 `nominal`
        会让「池紧时保底优先于后片的富余」这件事在算式里消失。
        """
        if left <= 0:
            return 0
        rolled = max(0, left - nominal - floor - reserved)
        return min(left, nominal + floor + rolled)


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


def verify_reserve(*, enabled: bool, items: int) -> tuple[int, int]:
    """对账轮的**预留**额度 `(索取, 轮次)`：规划时就从池里划走，分片与汇总都不许动它。

    ## 为什么必须预留（实测 run 57）

    池 = 分片数 × 名义额（那次 5×50 = 250），而复核是**最后**一步：分片按
    「名义额 + 富余」拿，富余来自 `池剩余 − 名义额 − 后面几片的名义额 − 汇总下限`
    —— **复核不在任何人的预留里**。于是分片一路把池吃到见底，汇总又取
    `max(剩余, 下限)`（可越池），到复核时只剩 0 次：报告正文写着 20 条结论，
    只有 3 条被裁决，尾部还另写一句「待人工核验」—— 正文与复核状态不一致，
    而读者先看到的是正文。

    预留量按**要核的条数**算（每条给 `VERIFY_REQUESTS_PER_ITEM` 次：读一处、核一处），
    下限保证真能去查一次、上限保证它吃不掉池的一大块。`enabled` 为假（这一轮不开对账）
    时是 `(0, 0)`，一分不占。
    """
    if not enabled:
        return (0, 0)
    size = max(1, int(items or 0))
    requests = max(
        MIN_VERIFY_REQUESTS, min(size * VERIFY_REQUESTS_PER_ITEM, MAX_VERIFY_REQUESTS)
    )
    return (requests, VERIFY_ROUNDS_RESERVE)


def verify_reserve_for(config: Mapping[str, Any] | None) -> tuple[int, int]:
    """配置 dict → 对账轮的预留（给**估算侧**用的那一个调用口）。

    运行侧手里有 `plan.quota`（它按汇总出来的真实条数算），估算侧只有配置 ——
    所以它只能用**规划期的默认条数**（跑之前不知道会出几条结论，`plan_family` 在
    `verify_items` 缺省时拿到的也是这个值）。口径必须与运行侧是同一个函数：两份各算
    一遍的话，预估端点的理论上限会与运行侧对不上，而那个数正是用户判断「这个额度够
    不够」的依据。

    **它在这里而不是在调用方**：估算端点那个文件顶着 2000 行的 ERROR 闸门
    （`scripts/check_file_length.py`），调用方多一行就过不去；而这段判断与
    `verify_reserve` 是同一件事，放在一起也更好读。
    """
    data = dict(config or {})
    return verify_reserve(
        enabled=bool(data.get("subagent_verify")), items=DEFAULT_VERIFY_ITEMS
    )


def pool_exhausted_note(quota: "FamilyQuota", stage: str) -> str:
    """池耗尽那一刻的告警：**阶段名 + 池剩余数**（原先只有引擎那句「额度用尽」）。

    引擎那句说的是「**这一次**成员的上限用完了」，它分不出三种完全不同的情形：池真没了、
    这一片本来就只分到这么多、还是后面的成员还有预留。读日志的人要能一眼看出后面还有
    多少钱 —— run 57 的对账轮就是在池见底之后才开跑的，而日志里只有一句「额度用尽」。
    """
    tail = (
        f"对账轮的预留 {quota.verify_requests} 次不受影响"
        if quota.verify_requests
        else "对账轮没有预留"
    )
    return (
        f"⚠️ AI 分析：家族共享池已耗尽（阶段 {stage}）：剩余 "
        f"{quota.requests_left} 次索取 / {quota.rounds_left} 轮，"
        f"已用 {quota.requests_used}/{quota.requests_pool} 次索取、"
        f"{quota.rounds_used}/{quota.rounds_pool} 轮；{tail}。"
        "后面的成员只能基于已有证据出结论。"
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
    # **确定性分工 → 确定性取证**（P1b）。分片是平台按 `(仓库, 路径, 提交)` 定下来的，
    # 而取证此前完全靠模型自主：平台只预取 `DEFAULT_MAX_FILES` 个 diff，剩下的没有任何
    # 机制要求「分到这个文件的人」去读它 —— 实测 run 58 分配 120 个、取到证据的只有 63 个，
    # 20 个补偿项里 14 个第二次仍然没读。这一段把那句话落到具体文件上：清单里每一条都
    # 写好了它的三样（路径、提交、仓库），照着填就能索取，不必它自己再去找。
    if member.assigned_files:
        visible = member.assigned_files[:MANDATORY_LIST_MAX_ITEMS]
        compensation = [item for item in visible if item.is_compensation]
        listed = "\n".join(
            f"- {item.describe()}" + ("　**← 补偿项**" if item.is_compensation else "")
            for item in visible
        )
        omitted = len(member.assigned_files) - len(visible)
        tail = (
            f"\n- ……另有 {omitted} 个，请用 `read_reference` 读取 "
            "`change-manifest` 的后续分段（那一份里每条都带仓库与提交）"
            if omitted
            else ""
        )
        blocks.append(
            "## 确定性文件分工（**必读清单**）\n\n"
            f"平台给你分配了 {len(member.assigned_files)} 个优先检查文件。"
            "**先把下面这些各取一次 `file_diff`，再去做你那几个维度的深挖。**"
            "每行已经把这条 diff 需要的三样都写好了（路径、提交、仓库），照着填即可 —— "
            "**这是本次分析的覆盖底线**：它们一条都没被读过，报告里的覆盖率就不成立。"
            "\n\n"
            + listed
            + tail
            + (
                "\n\n"
                f"其中 **{len(compensation)} 条是补偿项**（标了「← 补偿项」的那些）："
                "它们**不是本轮新改的**，而是上一轮没取到证据的那些。优先级高于新改动 —— "
                "上一轮已经漏过一次，再漏一次就没有下一轮可补了。"
                if compensation
                else ""
            )
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


#: 复核优先级的三个**确定性**信号，以及各自的权重（见 `rank_verify_candidates`）。
#: 权重全是小整数、顺序确定：同一份输入两次挑出的一定是同一批（复核挑谁这件事本身
#: 不能带随机性，否则「哪几条被核过」就不可复现了）。
VERIFY_WEIGHT_WEAK_EVIDENCE = 3
VERIFY_WEIGHT_CROSS_REPOSITORY = 2
VERIFY_WEIGHT_BASELINE_CONFLICT = 2
#: 「证据薄弱」的字符阈值：证据合起来短于它，基本指不到具体位置（文件、行、字段）。
WEAK_EVIDENCE_CHARS = 40
#: 证据里出现这些词，说明模型**自己**就没核实过 —— 正是复核该接手的。
_UNVERIFIED_MARKERS = ("待确认", "未找到", "尚未确认", "无法确认", "需人工")

#: 从一段文本里挑路径用的形状判据。**只认平台跟踪清单里有的那些**（见 `_paths_in`），
#: 所以宽一点不会误伤：认不出来的 token 直接丢掉。
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./\-]*/[A-Za-z0-9_./\-]*[A-Za-z0-9_]")


def _weak_evidence(anomaly: Anomaly) -> bool:
    """这条结论的证据**薄**：一条没有、合起来短得指不到位置，或者它自己写着「待确认」。

    第三种是「模型自己就没核实过」的显式标记 —— 那种结论最该被复核，而它在按严重度
    排序时一点优势都没有（严重度是它自己填的）。
    """
    joined = " ".join(str(item or "") for item in anomaly.evidence).strip()
    if not joined:
        return True
    if len(joined) < WEAK_EVIDENCE_CHARS:
        return True
    return any(marker in joined for marker in _UNVERIFIED_MARKERS)


def _repositories_of_commit(scope: Any, commit: str) -> frozenset:
    mapping = getattr(scope, "repository_ids_by_commit", None) or {}
    return frozenset(mapping.get(str(commit or ""), ()) or ())


def _paths_in(text: str, scope: Any) -> tuple[str, ...]:
    """从一段文本里挑出**平台认识的**路径（对不上跟踪清单的一律丢掉）。

    只认清单里有的那些，是为了不把散文里的斜杠当成路径：判据落在平台的跟踪表上，
    误报与漏报都只可能来自「这条路径不在本次范围里」——而那本来就不是复核的题目。
    """
    known = getattr(scope, "latest_commit_by_path", None) or {}
    if not known:
        return ()
    found: list[str] = []
    for raw in _PATH_TOKEN_RE.findall(str(text or "")):
        path = normalize_path(raw)
        if path and path in known and path not in found:
            found.append(path)
    return tuple(found)


def _cross_repository(anomaly: Anomaly, scope: Any) -> bool:
    """这条结论**跨仓库**：它自己的提交在一个仓库，而它引的证据落在另一个仓库里。

    ## 为什么这类要优先复核

    run 57：平台只冻结了配置仓库，代码仓库的文件一律被拒（同一句「不在本次冻结版本的
    Git 跟踪文件里」）—— 于是模型给出的跨仓结论**恰恰是最缺证据的那一类**，而它自己
    感觉不到（它会把「读不到」写成信息缺口，但仍按推断下结论）。这一条判据就是把这批
    结论挑出来复核。
    """
    own = _repositories_of_commit(scope, anomaly.commit)
    if not own:
        return False
    for path in _paths_in(_evidence_text(anomaly), scope):
        repositories = _repositories_of_commit(
            scope, (getattr(scope, "latest_commit_by_path", None) or {}).get(path, "")
        )
        if repositories and not (repositories & own):
            return True
    return False


def _baseline_conflict(anomaly: Anomaly, scope: Any) -> bool:
    """这条结论引的那份差异**归属可疑**：同一个文件被本窗口的多条提交改过，而它引的
    不是这个文件最近的那一条。

    ## 为什么这类要优先复核（实测 run 55）

    报告把**上一笔提交**（铁剑 200→260）反复写成「本次差异」（本次是皮甲 180→190）——
    两笔改的是同一个配表，合并差异里两条都在，而清单的小标题只写「这个文件最后一次
    改动落在哪条提交」。引错版本是这一类结论的典型失效模式，而它**读起来毫无破绽**：
    差异正文确实出自这个文件。所以有这种形状的结论排前面。
    """
    path = normalize_path(anomaly.file_path)
    if not path or scope is None:
        return False
    touched = [
        commit_id
        for commit_id, paths in (getattr(scope, "paths_by_commit", None) or {}).items()
        if path in (paths or ())
    ]
    if len(touched) < 2:
        return False
    latest = str((getattr(scope, "latest_commit_by_path", None) or {}).get(path, "") or "")
    return bool(latest) and str(anomaly.commit or "") != latest


def _evidence_text(anomaly: Anomaly) -> str:
    parts = [str(anomaly.file_path or ""), str(anomaly.impact or ""), str(anomaly.suggestion or "")]
    parts.extend(str(item or "") for item in anomaly.evidence)
    return " ".join(parts)


def verify_weight_reasons(anomaly: Anomaly, scope: Any = None) -> tuple[str, ...]:
    """这条结论被复核**优先**的理由（空元组 = 只是按严重度排到的）。

    进任务书给模型与读日志的人看：复核挑谁如果不说出来，「只核了 3 条」那件事看起来
    就像随机的。
    """
    reasons: list[str] = []
    if _weak_evidence(anomaly):
        reasons.append("证据薄弱")
    if _cross_repository(anomaly, scope):
        reasons.append("跨仓库依赖")
    if _baseline_conflict(anomaly, scope):
        reasons.append("归属可疑（同一文件被本窗口多条提交改过）")
    return tuple(reasons)


def verify_weight(anomaly: Anomaly, scope: Any = None) -> int:
    score = 0
    if _weak_evidence(anomaly):
        score += VERIFY_WEIGHT_WEAK_EVIDENCE
    if _cross_repository(anomaly, scope):
        score += VERIFY_WEIGHT_CROSS_REPOSITORY
    if _baseline_conflict(anomaly, scope):
        score += VERIFY_WEIGHT_BASELINE_CONFLICT
    return score


def rank_verify_candidates(
    anomalies: Sequence[Anomaly], *, items: int, scope: Any = None
) -> tuple[Finding, ...]:
    """挑对账轮要核的那几条。**编号仍是 `assign_findings` 那一套**（F1…Fn 按严重度）。

    ## 在「严重度 → 置信度」之上再加三个确定性信号

    原先就是取严重度最高的前 N 条（`assign_findings` 的次序）。最严重的**未必是最可能
    错的**，而复核的价值在于**改判**：证据薄、跨仓库、差异归属可疑这三类才是它边际收益
    最高的地方（各自的机理见 `_weak_evidence` / `_cross_repository` /
    `_baseline_conflict`）。

    ## 编号必须原样保留

    `verdict` 按 `[F#]` 认结论（`assign_findings` 的编号，与封顶同一套严重度排序）。
    所以这里只**挑子集**，不重排编号 —— 重排会让裁决打到另一条结论上，而那种错没有任何
    征兆（报告里只会少一条裁决、多一条没被裁决的）。同样权重的排在前面的仍是**严重度
    序**（`findings` 本来就是那个序）—— 拿 `Finding.position`（= 模型给出结论的顺序）
    当次键会把次序整体打乱，`test_it_takes_the_most_severe_first` 正是钉这一条的。
    """
    findings = assign_findings(anomalies)
    if not findings:
        return ()
    size = max(1, int(items or 0))
    ordered = sorted(
        enumerate(findings),
        key=lambda item: (-verify_weight(item[1].anomaly, scope), item[0]),
    )
    return tuple(finding for _, finding in ordered[:size])


def build_verify_task(
    plan: FamilyPlan,
    synthesis: EngineOutcome,
    *,
    evidence_index: Mapping[str, EvidenceRef] | None = None,
    pool_note: str = "",
    scope: Any = None,
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
    # 挑选复用 `verdict.assign_findings`（内部就是 `rules.rank_anomalies`：严重度 → 置信度，
    # 同档保持模型给的顺序）—— 编号由它发，两处各排一次会让**裁决打到另一条结论上**。
    # `rank_verify_candidates` 只在它之上按三个确定性信号加权（见那边的说明）。
    ranked = rank_verify_candidates(
        synthesis.anomalies, items=plan.verify_items, scope=scope
    )
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
            reasons = verify_weight_reasons(anomaly, scope)
            if reasons:
                lines.append(
                    "- 复核优先级：平台按**"
                    + "、".join(reasons)
                    + "**把它排在前面（不是判定它错，是这几类最需要反证）"
                )
            if anomaly.file_path:
                lines.append(f"- 位置：{anomaly.file_path}")
            if anomaly.impact:
                lines.append(f"- 它说会造成：{truncate_text(anomaly.impact, CANDIDATE_TEXT_MAX_CHARS)[0]}")
            # 断言清单（P0-01）：一条结论常常是**复合断言**，逐条回答才让「哪一半没核实」
            # 有地方安放。不给这份清单，复核只能整条回答，而实测就是那么出错的 ——
            # run 58 的 F3 标题断言「断言中断进程」，复核在理由里承认没核实，整条仍被
            # 标成 confirmed。
            if anomaly.claims:
                lines.append(
                    "- **它的断言（逐条回答，编号照抄）**："
                    + "；".join(
                        f"`{claim.claim_id}`（{_CLAIM_KIND_LABELS.get(claim.kind, claim.kind)}）"
                        f"{truncate_text(claim.statement, CANDIDATE_TEXT_MAX_CHARS)[0]}"
                        for claim in anomaly.claims
                    )
                )
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
