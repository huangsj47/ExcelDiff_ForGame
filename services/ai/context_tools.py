"""白名单工具：执行模型索要的上下文，并把它变成可记账的 `ContextItem`。

## 三层分离

| 层 | 职责 | 在哪 |
|---|---|---|
| 请求的**合法性** | 类型在白名单内、commit 属于本批次、path 属于该 commit | `protocol.sanitize_requests`（已实现） |
| 请求的**执行** | 真正去读 git / SVN / 数据库 / skill 文档 | 由调用方注入的 provider |
| 执行的**记账** | 缓存、截断、失败、预算 | 本模块 |

provider 是注入的（四个返回字符串的方法），所以本模块不 import git、不 import Flask、
不碰数据库，可以被完整单测；真实 provider 在服务层，负责把平台既有能力接上。

## 三条必须做对的事

### 1. 失败与「确实没有内容」必须区分开

工具抛异常时**不能**只给模型一个空字符串。空字符串会被理解成「这个文件没有改动」，
于是模型基于「无变更」下结论——而真相是「我们没读到」。所以失败要产出一段明确说
「获取失败、内容不可用、不要猜测」的文本。

### 2. 缓存的生命周期是**一次分析**，不是进程

同一个文件在多个轮次里被反复索要是常态（模型会忘）。不缓存就要一遍遍重跑 Excel diff，
那是秒级操作。但缓存**不能跨分析复用**：仓库被重新导入或 force-push 之后，同一个
commit 的 diff 会变，而按 commit 做的全局缓存会一直吐旧结果——分析报告和页面上看到的
diff 对不上，这是最难查的一类不一致。

### 3. 每一次截断都带省略量

见 `budget`。**保留首尾**那种截断（`truncate_text_middle`）**不在这一层**：只砍尾巴会让
排在后面的整张表完全不可见，而「配表 A 改了、配表 B 也要跟着改」正是这个平台最关心的
风险 —— 所以它落在 `windowed_view.render_window` 里「切不开的 diff」那一支
（`total <= 1`）。这一层对剩下的纯文本一律只砍尾巴。

### 4. 同一份正文不重发第二遍，只给一个指针

模型重复索要同一个文件是常态（见第 2 条）。命中内存缓存时**不再把正文原样 append 一遍**：
那份 11,000 字的 diff 在提示词里出现两次，既多花 token（第二次出现时它是**新增内容**，
按未命中价计），又把模型的注意力稀释到两份一模一样的东西上。所以第二次及以后只给一条
**指针**：正文在哪（`### [file_diff] <label>` 那一节）、不要再要一遍。

**指针必须说清「内容在哪里」，否则会起反作用**：模型看不到正文会以为没拿到，于是再要
一次 —— 而重复索取照样消耗额度（第 2 条），结果是额度被耗光、上下文还是没看到。
所以指针文本里带上了那一条的**原始 label**，与 `render_context_items` 渲染出的标题
逐字一致，模型可以按标题回查。

首次出现的正文**一字不动**：缓存是为了不再重发，不是为了改写第一次给了什么。

### 5. 跨成员共享的正文缓存要给**全文**，不能给指针

子代理模式（`services/ai/subagent.py`）下，N 个成员各跑一条独立的对话，但共用一份
`body_cache`（由 `run_family` 建、随 `body_cache=` 传进引擎）。触发它的场景很实际：
S1 为了查耦合读了表 A 的 diff，S2 也要读同一份 —— 有了它，第二家不必再取一次
（Excel 结构化差异是秒级操作），也照样拿到全文。

**共享命中与「同一成员内重复索取」是两件事，给的文本必须不同**：

* 同一成员内重复 → 给**指针**（第 4 条）。那句话是「见上文那一节」，在那个成员的对话里
  为真；
* 跨成员命中 → 必须给**完整正文**。「见上文」在**另一个成员的对话里是假话** ——
  它的上文里根本没有那一节，模型会去找一个不存在的东西，然后要么当自己看过了、
  要么再要一次。

所以共享命中时给的是 `ContextItem` 原件（它是 frozen 的，N 个成员共用一份没有风险），
同时把这一条**也写进本成员的本地缓存** —— 于是同一个成员再要第二次时，才退化成指针。

### 6. 截断账的唯一口径是「交付」：谁拿到的是截断正文，就算谁一条

第 5 条那条分支给出去的是**已经截断过**的正文（原件上的 `truncated` 是真值），所以它
**也要记一笔截断** —— 记的是「这一家拿到的内容是残缺的」，与 `produced_chars` 在那条
分支上记全文长度是同一个道理（那一条早就承认「这次真的把正文交给了模型」）。

这一笔原先漏了（记账只在真正执行的那条路上做），后果是**两本账对不上**：同一份被截断的
正文交给 N 个成员时，逐条明细里出现 N 次（`trace_evidence.summarize_executed` 读的就是
条目上的 `truncated`），而汇总计数只记 1 次。线上实测 run 13：逐 trace 的
`dropped_json.truncated` 求和 18、明细求和 25（run 12 是 18 : 26，run 11 是 17 : 23）。

口径选定「交付」而不是「本次执行」：**这份账是给用户看「哪些取证被截断了」的** —— B 拿到
的正文确实是截断的，它据此写下的结论同样有覆盖缺口，少记就等于把 B 那一份缺口藏起来。
「把明细里的 truncated 抹掉」也能让两数相等，但那是拿掉真信息去凑数，不做。于是三本账
（`ToolBatch.truncated`、`stats[kind]["truncated"]`、逐条明细）按同一份求和后必须相等：

    Σ_轮 batch.truncated
      == Σ_kind tool_stats[kind].truncated
      == Σ_轮 Σ_details details[].truncated   （一轮条数 ≤ TRACE_LIST_MAX_ITEMS 时）
      == Σ_轮 len([i for i in batch.items if i.meta.get("truncated")])

**读这份账的人要知道**：同一条正文被 k 个成员各拿到一次就记 k 条。所以这个数说的是
「有多少次交付拿到的是残缺内容」（＝明细的行数），不是「有几份不同的取证被截断了」。

判据落在 `meta["truncated"]` 上，**不是**「命中了共享缓存」上：共享缓存里也有失败条目
（`_shared_get` 的 docstring），它给出去的是「取不到」的说明，与截断是两件事。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, MutableMapping, Protocol, runtime_checkable

from services.ai.budget import ContextItem, truncate_text
from services.ai.evidence_store import blob_id_of, evidence_id_of
from services.ai.protocol import EVIDENCE_REQUEST_TYPE, ContextRequest, DroppedItem
from services.ai.scope import normalize_path
from services.ai.trace_evidence import failure_notice
from services.ai.windowed_view import render_window, windowed_kinds
from utils.content_window import CONTENT_MAX_CHARS
from utils.logger import log_print

# 单条上下文的字符上限。Excel 的 diff 通常是最大的，但也不该无限大。
#
# 这三个 11,000 是**与预算对账出来的**，不是拍的：150 个提交的版本上，变更摘要要占掉约
# 39,000 字符，历史结论基线摘要再占 6,000（增量分析每轮都带），加上平台 skill 与知识包
# 约 12,000，预算 200,000 里剩下约 143,000 给上下文；按 12 次索取分摊，每条约 11,900，
# 取下限 11,000，留一点余量给「多轮里累积的回复文本」。
# 原先是 14,000 —— 12 次 × 14,000 = 168,000，加上摘要就已经超预算，模型索要 12 个文件
# 会有一半被截断。**条数上限（`budget.DEFAULT_MAX_ITEMS`，现为 40）与
# `DEFAULT_MAX_TOOL_REQUESTS` 是两回事**：前者约束同一次分析里能同时带多少条，
# 后者约束整个分析期间能要多少次。
# 注意上一句那个「12 次索取」是对账**当时**的旧值：这两个数字都改过
# （条数上限 8 → 20 → 40、索取次数 12 → 20 → 40），所以这里一律不写死 ——
# 写死的那个数会先于这句话本身过期（本条注释原先就写着「条数上限 8」）。
DEFAULT_TOOL_LIMITS: Mapping[str, int] = {
    "commit_detail": 6_000,
    "file_diff": 11_000,
    # `file_content` 用的是 `utils.content_window.CONTENT_MAX_CHARS`：**正文在取数侧就按
    # 这个上限切好**（切在行边界上，并把「给的是哪一段、共多少行」写进抬头）。这里若写一个
    # 更大的数，取数侧会先把抬头写好、再由下面的预算层砍一刀尾巴 —— 抬头说的行数与正文
    # 就对不上了，而那个行号正是模型写进结论里的坐标。
    "file_content": CONTENT_MAX_CHARS,
    "read_reference": 11_000,
    # 检索给的是**位置**（`路径:行号: 那一行`），不是正文，所以单条可以给很多：
    # 8,000 字够列几十条命中，而「这个字段还有谁在用」的答案本来就不需要正文。
    "find_references": 8_000,
}

# 工具请求的总预算（按「次数」计，不是按条数）。
#
# 12 → 20：线上那一轮 767 个文件里，模型只能看 12 个 diff，而它连一份 767 行的清单
# 都消化不了 —— 决定结论质量的是「看了多少内容」，不是「知道有多少名字」。
#
# 20 → 40（2026-09-19）：线上真实的一次周版本分析里，一个分片跑完 3 次就报「本轮上下文
# 额度已用尽」，取不到导出配置与消费侧代码，`config_id` 那一整个维度只能写成信息缺口。
# 当时的额度是**按成员平分**的（`总上限 ÷ (成员数 + 1)`），20 次在 3 个分片下等于每人 5 次。
# 这个数字是按「**单个 agent 够用**」定的：40 次给一个分析 agent。
#
# 后来平分那条规则本身也改了（2026-09-20，`subagent.plan_family`）：每个成员拿配置值的
# `MEMBER_BUDGET_PERCENT`%（默认 100%）而不是「总额 ÷ 成员数」——用户填的是「一个 agent
# 能看多少」，不是「全家加起来能看多少」。所以 40 在 3 个分片下是**每人 40 次**（原先
# 平分时是 10 次、后来七成时是 28 次）：一个成员的上限从此就等于这个配置值本身。
#
# 它与另外三个数字**必须一起动**（`test_the_prompt_budget_can_honor_the_request_budget`
# 与 `test_the_context_item_cap_never_wastes_a_paid_request` 各盯一半）：
# `budget.DEFAULT_MAX_ITEMS`（条数上限不许小于索取次数，否则「付了 N 次、只带走 M 条」）、
# `models.ai_analysis.project_config.DEFAULT_PROMPT_CHAR_BUDGET`（要装得下 N × 单条上限）、
# `DEFAULT_TOOL_LIMITS`（单条上限）。
DEFAULT_MAX_TOOL_REQUESTS = 40

# 这三样超长时走「分段 + 点名」（`services/ai/windowed_view.py`），不走下面的截断：
# 它们的共同点是**内容有结构**（改动块 / 小节 / 文件清单），于是「第几段」是一个模型
# 说得清、也对得上的坐标。`file_content` 不在里面 —— 它本来就按行窗口取，那个坐标
# 比段号更准。`find_references` 也不在：它的结果是**命中清单**，超长时该收窄关键词，
# 而不是一段段翻（翻出来的是同一批命中的不同片段，没有价值）。
_WINDOWED_KINDS = frozenset(windowed_kinds())


@runtime_checkable
class ContextProvider(Protocol):
    """真实取数的五个入口。返回值语义：

    * 返回字符串 → 成功。空字符串表示「确实没有内容」（例如该文件在此提交里是新增，
      没有可对比的旧版本），调用方会原样告诉模型。
    * 返回 `None` → 拿不到（仓库没检出、commit 不存在、文件二进制不可解析……）。
    * 抛异常 → 同上，但会被记进 trace 的原因里。
    """

    def commit_detail(self, commit: str) -> str | None: ...

    def file_diff(self, commit: str, path: str) -> str | None: ...

    def file_content(
        self, commit: str, path: str, lines: str = "", repository_id: str = ""
    ) -> str | None: ...

    def read_reference(self, name: str) -> str | None: ...

    def find_references(self, query: str, path: str = "") -> str | None: ...


@dataclass(frozen=True)
class ToolBatch:
    """一次批量执行的结果。"""

    items: tuple[ContextItem, ...] = ()
    # 被拒绝执行或执行失败的记账。
    dropped: tuple[DroppedItem, ...] = ()
    # 实际发生的取数次数（不含缓存命中）。
    executions: int = 0
    cache_hits: int = 0
    # 因为超出预算而没执行的请求数。
    refused_by_budget: int = 0
    # 那些请求**分别是哪几个**（`_human_request_label` 的一行，点名到文件 / 查询）。
    #
    # 原先只有上面那个计数。计数能说明「有几条没轮到」，说明不了**缺的是哪几块** ——
    # 而用户看到「额度用尽，还有文件没看」时，第二个问题一定是「哪些」。
    # 这一条是从 `execute` 的拒绝分支里直接记下来的，不靠回头去 `dropped` 里按文案
    # 匹配（那种判据改一个字就静默失效）。
    refused_items: tuple[str, ...] = ()
    # 截断过的条数。口径是**交付**：谁拿到的是截断正文就算谁一条（含跨成员复用同一条，
    # 见模块 docstring 第 6 条）—— 它必须恒等于 `batch.items` 里带 `truncated` 的条数，
    # 也就是 `trace_evidence.summarize_executed` 写进 `executed_json.details` 的那个数。
    truncated: int = 0

    @property
    def has_failures(self) -> bool:
        return any(item.meta.get("tool_failed") for item in self.items)


# 每个工具类型记的账。键名就是消耗面板上按类型展开的那些列；新增一项要同步这里与前端表头。
_STAT_COUNTERS = (
    "calls",              # 计入索取额度的次数（被预算拒绝的不算 —— 它没消耗额度）
    "executions",         # 真正执行取数的次数（不含命中本地缓存）
    "cache_hits",         # 工具结果在**本次分析内**的内存缓存命中（不是 prompt cache）
    "failed",             # 执行失败、内容不可用
    "truncated",          # 交给模型时是截断正文的条数（含跨成员复用同一条，见模块 docstring 第 6 条）
    "refused_by_budget",  # 因超出索取上限而未执行
    "source_chars",       # 工具取回的原始字符数
    "produced_chars",     # 实际交给模型的字符数（截断后）
    "avoided_duplicate_chars",  # 本成员重复索取时，用指针省下的提示词字符
    "cross_member_replayed_chars",  # 跨成员缓存命中后仍需重发的正文字符
    "unscoped_content_requests",  # file_content 未指定行/工作表，可能退化成大范围读取
    "prefetched",         # **平台预取**的条数（不占模型的索取额度，见 `prefetch()`）
)


def _human_request_label(request: ContextRequest) -> str:
    """给**用户**看的一行：这次没轮到的到底是哪一块。

    与 `describe_request` 分开：那一条是模型回查内容的**地址**（`### [kind] label`），
    所以带类型前缀与 12 位 commit —— 拿它直接拼进「没轮到的包括：」那段话，用户看到的
    是 `file_content 9e315a3abcde config/x.xlsx`，读不出「缺的是哪个文件」。

    这里只留「要的是哪一块」，且**不猜**：拿不到路径就说不出路径，不编一个文件名。
    """
    if request.type == "read_reference":
        return f"参考文档 {request.name}"
    if request.type == EVIDENCE_REQUEST_TYPE:
        # 按地址取回的那一份，用户要认的是「哪一份证据」，而地址就是它的名字。
        return f"证据 {request.name}"
    if request.type == "find_references":
        return f"引用扫描 {request.query}" if request.query else "引用扫描"
    if request.type == "commit_detail":
        commit = (request.commit or "")[:8]
        return f"提交 {commit} 的改动详情" if commit else "提交详情"
    path = normalize_path(request.path)
    if not path:
        return request.type
    # 窗口是地址的一部分（同一文件的两段是两个请求），说「哪几行」用户才知道缺哪段。
    return f"{path}（{request.lines} 行）" if request.lines else path


def _meta_chars(item: ContextItem) -> int:
    """条目里记的「工具取回多少字符」。读不到返回 0。

    0 在这里是「没记」，而不是「取到了 0 个字」—— 调用方只在成功分支调它（见 execute）。
    """
    try:
        return max(0, int(item.meta.get("original_chars") or 0))
    except (TypeError, ValueError):
        return 0


# 缓存键：同一份**内容**才叫命中。
#
# `lines` 必须在键里。第一版漏了它，后果是「模型先要第 100–200 行、再要第
# 300–400 行，第二次拿到的是一句『你已经拿到这份内容了，见上文』」—— 而它上文里
# 只有第一段。这条错会静默地把「看另一段」变成「以为看过另一段」。
#
# `query` 也必须在一起，理由一模一样，而**后果更大**：`find_references` 没有 commit、
# 也没有单个文件（`path` 是可选的**范围前缀**，多数请求根本不带），所以那五项对它的每
# 一次检索都是同一串值 —— 漏掉 `query` 就等于「整次分析里第二次起的每一次检索都命中
# 第一次那条」。线上真实的一次：模型请求 `find_references(CfgRewardMode)`，拿回来的那
# 一节标着 `find_references _calcSegmentedBonus`，于是那一整块「引用扫描未执行」，
# `config_id` 与 `module_coupling` 只能写成信息缺口。子代理模式下还会跨成员扩散
# （`body_cache` 是共享的）。
#
# 键里的每一项都必须是**决定返回内容**的字段。加字段时的判据就是这一条：
# `protocol.ContextRequest` 上除了「模型自己看的说明」之外，没有一项可以漏。
CacheKey = tuple[str, str, str, str, str, str, str]


def _cache_key(request: ContextRequest) -> CacheKey:
    return (
        request.type,
        request.commit or "",
        normalize_path(request.path),
        request.name or "",
        request.lines or "",
        request.query or "",
        # 同一条相对路径在两个仓库里都有时，`repository_id` 决定读到的是哪一份 ——
        # 它进了「决定内容的字段」那一类的判据（见上面 CacheKey 的说明）。
        request.repository_id or "",
    )


def _request_of_key(key: CacheKey) -> ContextRequest:
    """把缓存键还原成一个请求，**只为了写一句给人看的记账**（`describe_request`）。

    记账里必须说清「对不上的是哪一条」，否则读的人是知道「有一条缓存坏了」，却不知道
    坏的是哪一份 —— 而这一条的用途正是让人去查那一份内容。字段顺序与 `_cache_key`
    一处定义（两处各写一遍必然漂移，而漂移的表现是记账里印出另一个文件的路径）。
    """
    parts = [str(part or "") for part in tuple(key)]
    parts += [""] * (7 - len(parts))
    return ContextRequest(
        type=parts[0],
        commit=parts[1],
        path=parts[2],
        name=parts[3],
        lines=parts[4],
        query=parts[5],
        repository_id=parts[6],
    )


def describe_request(request: ContextRequest) -> str:
    """给模型看的一行标签。也用于续跑摘要（`budget.build_continuation_summary`）。

    **这一段是模型回查内容的地址**（`### [kind] label`），所以「要了哪一段」必须写进去：
    同一个文件的两段窗口若共用一行标签，正文里就会出现两个标题一样的 `###` 节，
    而缓存指针说的「见上文那一节」于是指向一个不唯一的位置。
    """
    if request.lines:
        head = (
            f"read_reference {request.name}"
            if request.type == "read_reference"
            else (
                f"{request.type} {(request.commit or '')[:12]} "
                f"{normalize_path(request.path)}"
            ).strip()
        )
        return f"{head} lines={request.lines}"
    if request.type == "read_reference":
        return f"read_reference {request.name}"
    if request.type == EVIDENCE_REQUEST_TYPE:
        # 地址本身就是「这一节讲的是哪一份内容」，所以它必须逐字写进标题 ——
        # 尾部那句「按需索取」指的就是这个地址（`subagent._render_candidates`）。
        return f"evidence {request.name}"
    if request.type == "commit_detail":
        return f"commit_detail {request.commit[:12]}"
    if request.type == "find_references":
        scope = normalize_path(request.path)
        return f"find_references {request.query}" + (f"（范围 {scope}）" if scope else "")
    return f"{request.type} {(request.commit or '')[:12]} {normalize_path(request.path)}"


def _failure_text(request: ContextRequest, reason: str) -> str:
    """取数失败时给模型的文本。

    **必须明确说「不要猜」**。只给一段空文本会让模型把「我们没读到」当成「这里没有
    改动」，然后基于这个前提继续推理——报告里就会出现一条看起来很有把握、实际建立在
    读取失败之上的结论。
    """
    return (
        f"[取数失败] {describe_request(request)} 没有取到内容：{reason}\n"
        "这份内容**没有**被分析到，不代表它没有问题、也不代表它没有改动。\n"
        "请基于其它已有证据继续判断；如果这个文件对你的结论是必需的，"
        "请在报告里把它写成「信息缺口」，不要凭猜测给出结论。"
    )


def _empty_text(request: ContextRequest) -> str:
    """工具执行成功但没有内容。

    与失败区分开：这可能是「新增文件没有可对比的旧版本」这类**确定性**结论，模型可以
    据此推理，只是不能把「空」当成「没问题」。
    """
    return (
        f"[无内容] {describe_request(request)} 取数成功但没有返回内容"
        "（常见原因：本次是新增文件、上一版本不存在同名文件，或该文件是二进制且无可解析差异）。\n"
        "**不要把「无内容」等同于「没有风险」**。"
    )


def _repeat_text(item: ContextItem) -> str:
    """第二次及以后索要同一份内容时，给模型的那段**指针**（正文不再重发）。

    两个必须说清的事，少一个这段文本就白写：

    1. **内容在哪** —— 用与 `prompt.render_context_items` 渲染出的标题**逐字一致**的
       写法（`### [kind] label`）指过去。换句话说，模型可以按这个标题回查。
    2. **不用再要** —— 不说这句，模型会以为「这一轮没给」而重新索取，而重复索取照样
       消耗额度（见模块 docstring 第 2 条）：额度耗光了，它还是没看到那份内容。
    """
    return (
        f"[已在上文给出] {item.label} 的正文在更早的轮次里已经完整给过你一次，"
        f"见上文那一节 `### [{item.kind}] {item.label}`。这一轮不再重复附上。\n"
        "**你已经拿到这份内容了**，请直接使用上文给出的那一份，不要因为最近一轮里没有"
        "再出现它就当成没拿到。确实需要重看时，也以上文的内容为准；**不要再次索取同一个"
        "文件或同一份内容** —— 重复索取不会带来新内容，只会占掉索取额度。"
    )


def _repeat_item(item: ContextItem) -> ContextItem:
    """把一条命中缓存的上下文换成指针条目。

    `kind` 与 `label` **原样保留**：标题就是模型回查内容的地址，改一个字它就找不到
    那一节了。`meta` 里不记 `original_chars` / `truncated` —— 这一条没有「取回了多少字」
    可言，凭空记一个数会让「这个类型很省」的结论多出一份重复的字符量。
    """
    return ContextItem(
        kind=item.kind,
        label=item.label,
        text=_repeat_text(item),
        meta={
            "repeat_pointer": True,
            "chunk_id": item.meta.get("chunk_id", ""),
            "evidence_id": item.meta.get("evidence_id", item.meta.get("chunk_id", "")),
        },
    )


def _mark_prefetched(item: ContextItem) -> ContextItem:
    """给预取条目盖一个痕（`meta["prefetch"]`），**其余一字不动**。

    `label` 与 `meta`（含 `cache_key` / `evidence_id` / `truncated`）都必须原样保留：
    前者的标题是模型回查内容的地址，后者是 `forget()` 与记账读的东西。只多一个键，
    于是读 trace 或落库明细的人能一眼分出「这条是模型要的」还是「这条是平台预取的」。
    """
    return ContextItem(
        kind=item.kind,
        label=item.label,
        text=item.text,
        meta={**item.meta, "prefetch": True},
    )


def _with_chunk_id(item: ContextItem, key: CacheKey) -> ContextItem:
    """为证据正文生成稳定地址；相同请求与内容在不同成员中得到同一 id。

    `meta` 里同时留下 `blob_id`（**只按内容**算的 id，见 `evidence_store`）与
    `cache_key`（算 `evidence_id` 用过的那些输入）。后者不是装饰：`evidence_store.verify`
    要重算一次这个 id 并比对，而**没有 `cache_key` 就重算不了** —— 那样 `verify` 只能
    永远返回「不知道」，于是共享缓存读回来的每一份正文都无从自证。
    """
    evidence_id = evidence_id_of(key, item.text)
    return ContextItem(
        kind=item.kind,
        label=item.label,
        text=item.text,
        meta={
            **item.meta,
            "chunk_id": evidence_id,
            "evidence_id": evidence_id,
            "blob_id": blob_id_of(item.text),
            # 存的是**键本身**（一个 6 元组），不是它的摘要：hash 要按原样重算。
            "cache_key": tuple(str(part or "") for part in key),
        },
    )


@dataclass
class ContextTools:
    """带缓存、预算与记账的工具执行器。

    一次分析创建一个实例（缓存随之只活一次，见模块注释）。

    `body_cache` 是**跨成员**共享的那一份（子代理模式，见模块 docstring 第 5 条）：
    由 `subagent.run_family` 建一次、每个成员各自 new 一个 `ContextTools` 时传进来。
    **不要**把整个 `ContextTools` 共享给 N 个成员 —— 那会把每成员的索取额度并成一份
    （`_requests_seen` 在实例上），而「每个成员各有一份额度」正是分工的代价被摊平的方式。
    """

    provider: ContextProvider
    max_tool_requests: int = DEFAULT_MAX_TOOL_REQUESTS
    limits: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_TOOL_LIMITS))
    # 跨成员共享的正文缓存。`None` = 单代理（今天的全部行为不变）。
    body_cache: MutableMapping[CacheKey, ContextItem] | None = None

    _cache: dict[CacheKey, ContextItem] = field(
        default_factory=dict, init=False, repr=False
    )
    _executions: int = field(default=0, init=False, repr=False)
    _cache_hits: int = field(default=0, init=False, repr=False)
    # 累计请求数。**必须是跨轮次的累计值**：预算按「一次分析总共能要几次」计，
    # 如果按每一轮的下标判断，第二轮又从 0 开始，预算永远不会触发（第一版就是这样）。
    _requests_seen: int = field(default=0, init=False, repr=False)
    # 按工具类型的记账。与上面几个总数分开：总数是给预算逻辑用的，这份是给「这次分析把
    # 索取额度花在哪了、取回了多少字」用的（消耗面板按类型展示）。
    _stats: dict[str, dict[str, int]] = field(default_factory=dict, init=False, repr=False)
    # 「当前这次执行是**平台预取**」。由 `prefetch()` 短暂打开，`execute()` 里面读它决定
    # 记账口径（见 `_count_call`）。用实例上的一个开关而不是给 `execute` 加参数：那会
    # 改掉一个被引擎与测试大量调用的公开签名，而这里要的只是「同一段执行逻辑的另一种
    # 记账口径」。
    _prefetching: bool = field(default=False, init=False, repr=False)

    @property
    def executions(self) -> int:
        return self._executions

    def __post_init__(self) -> None:
        """把**本轮生效的单条上限**交给取数侧（只对那些认这件事的 provider）。

        ## 为什么在这里

        `file_content` 的正文在取数侧就被切（那才是给模型看到的一页），所以取数侧的
        上限必须等于计划推导出来的那个数 —— 否则用户把提示词预算调高、正文还是只有
        11,000 字，而计划上写着 30,333（工作包 D 点名的「隐藏截断」）。而**只有这一层**
        同时知道「生效的 `limits`」与「provider 是谁」，所以交接点在这里。

        用鸭子类型而不是往 `ContextProvider` 协议上加方法：那五个方法是有意做窄的
        （假 provider、探针、测试用的桩一大堆），加一个「配置自己」的方法会让每个实现
        都得跟一遍。认这个约定的 provider 实现 `apply_tool_limits(limits)` 即可，
        不实现就什么都不会发生（行为与从前逐字相同）。
        """
        apply_limits = getattr(self.provider, "apply_tool_limits", None)
        if not callable(apply_limits):
            return
        try:
            apply_limits(self.limits)
        except Exception as exc:  # noqa: BLE001 —— 交接失败只该让「页大小」退回初值
            log_print(f"⚠️ AI 取数：把单条上限交给取数侧失败：{type(exc).__name__}: {exc}")

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    @property
    def requests_seen(self) -> int:
        return self._requests_seen

    @property
    def stats(self) -> dict[str, dict[str, int]]:
        """按类型的记账，**深拷贝**——调用方改不坏内部状态。"""
        return {kind: dict(counters) for kind, counters in self._stats.items()}

    def _bump(self, kind: str, counter: str, amount: int = 1) -> None:
        """给某个工具类型的某一项记账。

        计数名写错会当场 KeyError（桶在第一次记账时就按 `_STAT_COUNTERS` 建全），
        而不是悄悄多出一个没人读的键 —— 用量数据「记了但没人看」和「没记」一样糟。
        """
        bucket = self._stats.setdefault(kind, {name: 0 for name in _STAT_COUNTERS})
        bucket[counter] += amount

    def _count_call(self, kind: str) -> None:
        """「这次取数算不算**模型的索取**」这一笔账（唯一的分岔点）。

        * 模型自己要的 → `calls`（它同时由 `_requests_seen` 计进索取额度）；
        * **平台预取的** → `prefetched`。

        为什么预取不算 `calls`：`calls` 在面板上就是「索取次数」，而 `requests_remaining`
        会把这个数交给模型（「本轮还剩 N 次」）。把平台自己的预取算进去，等于告诉模型
        「你已经要过 6 次了」—— 而它一次都没要过；更糟的是额度会被预取吃掉，P3 省下来的
        轮次又赔回去。**但正文与字符数照记**（`produced_chars` 等由调用处照旧记），
        否则面板上的「这次取回多少字」会与提示词里真实有多少字对不上 ——
        那才是「白拿又追不到」的隐藏上下文。
        """
        self._bump(kind, "prefetched" if self._prefetching else "calls")

    @property
    def requests_remaining(self) -> int:
        return max(0, self.max_tool_requests - self._requests_seen)

    def _limit_for(self, request_type: str) -> int:
        return int(self.limits.get(request_type, DEFAULT_TOOL_LIMITS.get(request_type, 8_000)))

    def _render(self, request: ContextRequest, raw: str | None) -> ContextItem:
        """把 provider 的返回渲染成一条带记账的上下文。"""
        label = describe_request(request)
        if raw is None:
            return ContextItem(
                kind=request.type,
                label=label,
                text=_failure_text(request, "读取失败或内容不可用"),
                meta={"tool_failed": True, "reason": "provider 返回空值"},
            )

        body = str(raw)
        if not body.strip():
            return ContextItem(
                kind=request.type,
                label=label,
                text=_empty_text(request),
                meta={"tool_empty": True},
            )

        limit = self._limit_for(request.type)
        # 这一刀砍在**哪个约束**上，必须有个名字。`meta["limit"]` 只是那个数字，读 trace 的
        # 人拿着它不知道该去调哪个旋钮（单条上限？窗口水位？模型窗口？），只能猜 —— 而
        # 「截断不可归因」正是任务 E3 点名的毛病之一。
        #
        # 取数侧这一刀**只可能**是「本工具本轮的条数上限」（`_limit_for` → 计划里的
        # `tool_limits`，见 `budget_plan.derive_tool_limits`）。`file_content` 那条另有一个
        # **provider 侧的夹子**（`utils.content_window.CONTENT_MAX_CHARS`），但它在取数时就
        # 生效了，这里拿到的正文已经 ≤ 它 —— 所以真在这里被砍，砍它的仍是条数上限。
        # 名字与 `budget.shrink_item` 的 `item_shrink_level_N` 同一族（也顺带与
        # `tests/test_ai_task_e8_paths.py` 里那几条断言取的名字一致），都进
        # `trace_evidence.summarize_executed` 的 `truncated_by`。
        cap_name = f"tool_limit_{request.type}"
        meta: dict[str, Any] = {"original_chars": len(body)}
        if request.type in _WINDOWED_KINDS:
            # 超长时**分段 + 让模型点名**，而不是从中间砍一刀（见 windowed_view）：
            # 抬头的「共 K 段 / 这是第几段 / 怎么要别的段」三件事，缺一件这份内容就有
            # 一部分是**永远拿不到**的，而模型不会知道自己少看了什么。
            text, window_meta = render_window(
                kind=request.type, label=label, text=body, window=request.lines, limit=limit
            )
            meta.update(window_meta)
            if meta.get("truncated"):
                meta["limit"] = limit
                meta["truncated_by"] = cap_name
            return ContextItem(kind=request.type, label=label, text=text, meta=meta)

        # 剩下的全是**纯文本**：只砍尾巴（保留首尾的中间省略是 `file_diff` 的待遇，而它
        # 已经在上面那条分支里返回了 —— 这里原先还挂着一个 `_STRUCTURED_KINDS` 的判据，
        # 它只含 `file_diff`、是 `_WINDOWED_KINDS` 的子集，所以那个分支**永远执行不到**；
        # 已删。`truncate_text_middle` 本身**没有死**：切不开的 diff 仍然走它，调用点是
        # `windowed_view.render_window` 里 `total <= 1` 那一支）。
        text, truncated = truncate_text(body, limit)
        if truncated:
            meta["truncated"] = True
            meta["limit"] = limit
            meta["truncated_by"] = cap_name
        return ContextItem(kind=request.type, label=label, text=text, meta=meta)

    def execute(
        self, requests: Iterable[ContextRequest]
    ) -> ToolBatch:
        """按顺序执行请求。

        **每一次请求都计入预算，命中缓存也算**。命中缓存省下的是我们的耗时，而不是
        模型的「索取额度」——把重复索要也计入，预算才真正起到「逼模型收敛」的作用
        （反复要同一个文件正是它没在收敛的信号）。命中缓存时不会重复执行取数。

        预算是**一次分析的总量**（跨轮次累计），不是每轮一份。
        """
        items: list[ContextItem] = []
        dropped: list[DroppedItem] = []
        executions = 0
        cache_hits = 0
        refused = 0
        refused_labels: list[str] = []
        truncated = 0

        for index, request in enumerate(requests):
            # 预取**不查也不占**索取额度：它不是模型要的（见 `_count_call`）。其余一切
            # 照旧 —— 缓存、记账、失败降级走的都是同一段代码，这正是「同一本账」。
            if not self._prefetching:
                if self._requests_seen >= self.max_tool_requests:
                    refused += 1
                    # 被拒的请求**不算 calls** —— 它没有消耗额度（额度由下面那行
                    # `self._requests_seen += 1` 记）。算进去会让「额度花在哪了」对不上总数。
                    self._bump(request.type, "refused_by_budget")
                    refused_labels.append(_human_request_label(request))
                    dropped.append(
                        DroppedItem(
                            "request",
                            index,
                            f"超出本次工具请求总预算（{self.max_tool_requests} 次），未执行",
                            f"{describe_request(request)}（累计第 {self._requests_seen + 1} 次）",
                        )
                    )
                    continue
                self._requests_seen += 1
            if request.type == "file_content" and not str(request.lines or "").strip():
                self._bump(request.type, "unscoped_content_requests")

            if request.type == EVIDENCE_REQUEST_TYPE:
                item, note = self._evidence_item(request, index)
                if note is not None:
                    dropped.append(note)
                self._count_call(request.type)
                if item.meta.get("tool_failed"):
                    self._bump(request.type, "failed")
                else:
                    # 按地址取回**不执行取数**（正文已经在手里），所以它算一次缓存命中 ——
                    # 与「同一成员内重复索取」记同一类账：省下的都是取数，不是额度。
                    cache_hits += 1
                    self._cache_hits += 1
                    self._bump(request.type, "cache_hits")
                    self._bump(request.type, "source_chars", _meta_chars(item))
                    self._bump(request.type, "produced_chars", len(item.text))
                    if item.meta.get("truncated"):
                        # 口径是**交付**（模块 docstring 第 6 条）：交出去的确实是截断正文。
                        truncated += 1
                        self._bump(request.type, "truncated")
                items.append(item)
                continue

            key = _cache_key(request)
            cached = self._cache.get(key)
            if cached is not None:
                cache_hits += 1
                self._cache_hits += 1
                self._count_call(request.type)
                self._bump(request.type, "cache_hits")
                # 命中缓存时这些字符是**上一轮已经取过**的，仍然算这一类型的产出 ——
                # 否则「这个类型很省」的结论会凭空少掉一半字符。
                self._bump(request.type, "source_chars", _meta_chars(cached))
                # 但**这一轮真正进提示词的**只有指针的长度（见模块 docstring 第 4 条）。
                # `produced_chars` 的口径就是「实际交给模型的字符数」，所以这里记的是
                # 指针那一小段，不是 11,000 字正文。
                pointer = _repeat_item(cached)
                self._bump(request.type, "produced_chars", len(pointer.text))
                self._bump(
                    request.type,
                    "avoided_duplicate_chars",
                    max(0, len(cached.text) - len(pointer.text)),
                )
                items.append(pointer)
                continue

            shared, shared_dropped = self._shared_get(key)
            if shared_dropped is not None:
                # 读到的那一条**自证不了**（见 `_shared_get`）：不拿它去下结论，退回真取数。
                dropped.append(shared_dropped)
            if shared is not None:
                # 别的成员已经取过这一份。**给全文，不给指针** —— 见模块 docstring 第 5 条。
                # 记账口径与本地命中一致（省下的是取数，不是模型的索取额度），只有
                # `produced_chars` 不同：这次真的把正文交给了模型，所以算全文的长度。
                cache_hits += 1
                self._cache_hits += 1
                self._count_call(request.type)
                self._bump(request.type, "cache_hits")
                self._bump(request.type, "source_chars", _meta_chars(shared))
                self._bump(request.type, "produced_chars", len(shared.text))
                self._bump(request.type, "cross_member_replayed_chars", len(shared.text))
                # 复用的若是**一条已经截断过的正文**，这一笔截断也要记（模块 docstring
                # 第 6 条）：明细侧本来就这么记，计数侧原先只在真正执行的那条路上记 ——
                # 于是同一条正文在明细里出现 N 次、在计数里只记 1 次，两本账对不上
                # （线上实测 25 : 18）。判据落在 `truncated` 上而不是「命中了共享缓存」上：
                # 共享缓存里也有**失败**条目，它给出去的是「取不到」的说明，不是截断正文。
                if shared.meta.get("truncated"):
                    truncated += 1
                    self._bump(request.type, "truncated")
                # 也存进本地：同一个成员再要第三次时，它的上文里确实已经有这一节了，
                # 那时才轮到指针。
                self._cache[key] = shared
                items.append(shared)
                continue

            try:
                item = self._render(request, self._fetch(request))
            except Exception as exc:  # noqa: BLE001 —— 工具失败不能作废整轮
                # 收窄到 Exception 而不是 BaseException：KeyboardInterrupt / SystemExit
                # 这类必须继续向上传播。工具失败（仓库没检出、git 超时、Excel 解析炸了）
                # 只该让这一条变成「内容不可用」，不该让整轮分析失败。
                dropped.append(
                    DroppedItem(
                        "request",
                        index,
                        f"执行工具时出错（{type(exc).__name__}），已降级为「内容不可用」",
                        describe_request(request),
                    )
                )
                item = ContextItem(
                    kind=request.type,
                    label=describe_request(request),
                    text=_failure_text(request, f"{type(exc).__name__}: {exc}"),
                    meta={"tool_failed": True, "reason": str(exc)},
                )

            item = _with_chunk_id(item, key)

            executions += 1
            self._executions += 1
            self._count_call(request.type)
            self._bump(request.type, "executions")
            # 「失败」的判据是**这两条之一**，缺一不可：
            #
            # * `meta.tool_failed`：provider 返回 `None` 或抛异常 —— 取数层自己知道它没拿到；
            # * `failure_notice(text)`：provider **返回了一句话**，而那句话是失败说明
            #   （`[配表解析失败]`、`[读不到差异]`、`[检索不到]`…）。
            #
            # 只看前者会漏掉整整一类：`ContextProvider` 的契约里「拿不到」可以是 `None`、
            # 也可以是一句说明（那一层刻意用后者把「为什么拿不到」带给模型）。这类返回
            # 在 trace 里被记成失败（`trace_evidence` 走的就是 `failure_notice`），
            # 而用量页按工具类型的「失败」列读的是这里的计数 —— 两处口径不同，于是
            # 面板上「N 条取不到」与「失败 0 次」同时出现，而后者被当成「取数都很顺」。
            #
            # 判据是两个来源共用的那一个函数，不在这里另写一份前缀表。
            if item.meta.get("tool_failed") or failure_notice(item.text):
                # 失败条目没有 original_chars。**不补 0**：让 failed 这个计数自己说明字符
                # 数的缺口，而不是把「没取到」伪装成「取到了 0 个字符」。
                self._bump(request.type, "failed")
            else:
                self._bump(request.type, "source_chars", _meta_chars(item))
                self._bump(request.type, "produced_chars", len(item.text))
            if item.meta.get("truncated"):
                truncated += 1
                self._bump(request.type, "truncated")
            self._cache[key] = item
            self._shared_put(key, item)
            items.append(item)

        if refused:
            items.append(
                ContextItem(
                    kind="budget_notice",
                    label="工具请求预算耗尽",
                    text=(
                        f"本次可用的上下文索取次数已用完（上限 {self.max_tool_requests} 次），"
                        f"有 {refused} 个请求没有执行。请基于已有证据输出 final。"
                    ),                    meta={"refused_by_budget": refused},
                )
            )

        return ToolBatch(
            items=tuple(items),
            dropped=tuple(dropped),
            executions=executions,
            cache_hits=cache_hits,
            refused_by_budget=refused,
            refused_items=tuple(refused_labels),
            truncated=truncated,
        )

    def prefetch(self, requests: Iterable[ContextRequest]) -> ToolBatch:
        """**平台自己发起**的预取：同一本账、同一套证据地址，但**不占模型的索取额度**。

        与 `execute` 的关系：执行路径**逐字相同**（白名单之外的一切 —— 缓存、跨成员共享、
        截断、失败降级、证据 id —— 都走同一段代码），只有两处口径不同：

        1. **不查也不增 `_requests_seen`**（额度是模型的，见 `_count_call`）；
        2. 记账落 `prefetched` 而不是 `calls`，条目上盖 `meta["prefetch"] = True`。

        ## 为什么要有它，而不是让调用方直接 `execute`

        「预取」曾经是很容易写歪的一件事：直接 `execute` 就吃掉了模型的额度；自己拼一段
        文本塞进提示词就成了**白拿又追不到**的隐藏上下文（面板上的取数字符与提示词里真实
        有多少字对不上，复核的人无从发现）。这一层把「预取的正文也必须是 ContextItem、
        也必须记账、也必须能按 evidence_id 取回」变成**结构上的**约束。

        ## 一个必须由调用方处理的后果（只在这一处出现）

        预取**写本地缓存**，而本地命中给的是指针（模块 docstring 第 4 条：「见上文那一节」）。
        所以「预取到了」必须真的等于「进了提示词」—— 被预算裁掉的条目要用 `forget()` 摘掉，
        否则模型之后要同一份内容时会被告知「看上面」，而上面没有那一节。
        调用方的判据已经备好：`evidence_prefetch.forget_unfitted(prefetch.items, items, tools)`。
        """
        pending = tuple(requests)
        if not pending:
            return ToolBatch()
        self._prefetching = True
        try:
            batch = self.execute(pending)
        finally:
            # `finally`：预取里抛异常（provider 炸了也会在 execute 里被吞掉，这里是兜底）
            # 不能让 `_prefetching` 一直开着 —— 那会把**之后模型自己的索取**全记成预取。
            self._prefetching = False
        return replace(batch, items=tuple(_mark_prefetched(item) for item in batch.items))

    def forget(self, items: Iterable[ContextItem]) -> int:
        """把**没能交付出去**的条目从本地缓存里摘掉，返回摘掉几条。

        只被一个场景用到（见 `prefetch` 的说明）：预取写进了缓存，而预算把某几条挡在
        提示词之外 —— 那时后续的重复索取会给出一条「见上文那一节」的指针，而那一节并不存在。

        只摘**本地** `_cache`，不动跨成员共享的 `body_cache`：共享命中给的是**全文**
        （模块 docstring 第 5 条），它对任何成员都是真话，而且摘掉它会让别的成员白取一次。
        """
        removed = 0
        for item in items:
            key = item.meta.get("cache_key")
            if not key:
                continue
            if self._cache.pop(tuple(key), None) is not None:
                removed += 1
        return removed

    def _evidence_item(
        self, request: ContextRequest, index: int
    ) -> tuple[ContextItem, DroppedItem | None]:
        """按 `evidence_id` 把**原件**取回来（`type: evidence`）。

        ## 为什么它不走 `_fetch` / `_render`

        那两层的职责是「去仓库取数，然后按这个类型的字符上限渲染」。而这一条既不去取数
        （正文就在证据仓里），也不该被重渲染：`_render` 会按 `_limit_for(type)` 再砍一刀
        （`evidence` 没有自己的上限，落到默认的 8,000 字），于是任务书里那句
        「原文 11,000 字，按需索取」**当场变成假话** —— 拿回来的比承诺的少。

        所以这里直接交原件：`text` 一个字不动，`meta` 保留当初的 `original_chars` /
        `truncated`（记账行照旧如实说明「这份被截断过」）。唯一改的是 `label` ——
        它必须是一条**唯一**的地址，否则提示词里会出现两节标题相同的 `###`，
        而「见上文那一节」那种话就指向了一个不唯一的位置。

        ## 三种取不到，各自说清

        证据仓没有（单代理路径）、地址不在仓里、地址在但**自证不了**（`verify` 为假）——
        三种都给一段明确说「取不到、不要猜」的文本（`_failure_text`），并在后两种记一条账。
        给一段空文本是最坏的处理：那会被读成「这份内容没有改动」。
        """
        store = self.body_cache
        by_id = getattr(store, "by_id", None)
        label = describe_request(request)
        if not callable(by_id):
            return (
                ContextItem(
                    kind=EVIDENCE_REQUEST_TYPE,
                    label=label,
                    text=_failure_text(
                        request,
                        "本次运行没有共享证据仓（按地址取回只在子代理模式下可用）",
                    ),
                    meta={"tool_failed": True, "reason": "no_evidence_store"},
                ),
                None,
            )

        item = by_id(request.name)
        if item is None:
            return (
                ContextItem(
                    kind=EVIDENCE_REQUEST_TYPE,
                    label=label,
                    text=_failure_text(
                        request, "这个地址不在本次运行的证据仓里（它可能来自上一次分析）"
                    ),
                    meta={"tool_failed": True, "reason": "evidence_not_found"},
                ),
                DroppedItem(
                    "evidence", index, "按地址取证据：仓里没有这个地址", request.name
                ),
            )

        verify = getattr(store, "verify", None)
        if callable(verify) and not verify(request.name):
            # **不交出可能坏了的内容**：读回来的这一份与它自称的地址对不上，说明它已经不是
            # 当初那一份了。这时候给模型一段「它说过的话」比给一份别的内容安全得多。
            return (
                ContextItem(
                    kind=EVIDENCE_REQUEST_TYPE,
                    label=label,
                    text=_failure_text(
                        request, "这一份的正文与它自称的证据地址对不上（hash 校验失败）"
                    ),
                    meta={"tool_failed": True, "reason": "evidence_hash_mismatch"},
                ),
                DroppedItem(
                    "evidence",
                    index,
                    "按地址取证据：正文与地址对不上（hash 校验失败），未交出",
                    request.name,
                ),
            )

        return (
            ContextItem(
                kind=EVIDENCE_REQUEST_TYPE,
                label=label,
                text=item.text,
                meta={**item.meta, "evidence_id": request.name, "evidence_of": item.label},
            ),
            None,
        )

    def _shared_get(self, key: CacheKey) -> tuple[ContextItem | None, DroppedItem | None]:
        """跨成员缓存里有没有这一份 → `(条目, 记账)`。**失败的条目也算命中**。

        「这个文件这次取不到」在同一个进程里几秒之内不会变（Agent 离线、这个提交里确实
        没有这个路径），让 N 个成员各等一遍 15 秒的上限只会拖长整次分析。给出去的是同一句
        带着原因的失败说明 —— 它本来就要求模型写成信息缺口，不会长成「没问题」。

        ## 命中时校验一次 hash（第二个返回值就是为它准备的）

        这条分支**给出去的是全文**（模块 docstring 第 5 条），所以它是「平台替另一份正文
        作保」的地方：交出去的必须是当初取回来的那一份。而这层保证原先**一个字都没有** ——
        读完从不校验，读到什么都照发。缓存被谁改过、条目被一条形状相同的顶掉，都不会报错，
        只是静默地把**别的内容**当成「那个文件的 diff」交给模型，而模型据此写下的结论
        看起来完全正常。

        `EvidenceStore.verify` 答不了「这条对不对」（内容质量不是它的事），它能答的是
        「这条还是它自称的那一条吗」。校验不过就**当没命中**：调用方退回真取数，并记一条
        `shared_cache` 的账 —— 静默用一份可疑的内容比重新取一次贵得多。

        `body_cache` 不是 `EvidenceStore` 时（测试里的普通 dict、别处的实现）**不校验**：
        那一层没有 `verify` 可言，凭空判它「不可信」会把所有非本类的缓存实现变成永不命中。
        """
        if self.body_cache is None:
            return None, None
        item = self.body_cache.get(key)
        if item is None:
            return None, None
        verify = getattr(self.body_cache, "verify", None)
        evidence_id = str(item.meta.get("evidence_id") or "")
        if callable(verify) and evidence_id and not verify(evidence_id):
            return None, DroppedItem(
                "shared_cache",
                0,
                "共享缓存里这一条的正文与它自称的证据地址对不上（hash 校验失败），已改回重新取数",
                describe_request(_request_of_key(key)),
            )
        return item, None

    def _shared_put(self, key: CacheKey, item: ContextItem) -> None:
        if self.body_cache is not None:
            self.body_cache[key] = item

    def _fetch(self, request: ContextRequest) -> str | None:
        if request.type == "commit_detail":
            return self.provider.commit_detail(request.commit)
        if request.type == "file_diff":
            return self.provider.file_diff(request.commit, normalize_path(request.path))
        if request.type == "file_content":
            return self.provider.file_content(
                request.commit,
                normalize_path(request.path),
                request.lines,
                repository_id=request.repository_id,
            )
        if request.type == "read_reference":
            return self.provider.read_reference(request.name)
        if request.type == "find_references":
            # 这个工具**没有 commit**：它搜整个批次（每份文件用各自最后那次提交的内容）。
            # `path` 在这里是**范围前缀**而不是某一条提交改过的某个文件 —— 见
            # `protocol.sanitize_requests` 里那一支的校验。
            return self.provider.find_references(request.query, request.path)
        # 走到这里说明 `sanitize_requests` 漏了一种类型。宁可如实报失败，也不要
        # 静默返回空——那会让模型以为拿到了内容。
        return None
