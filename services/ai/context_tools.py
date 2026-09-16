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

见 `budget`。这里额外做的选择是 Excel 结构化 diff 用**保留首尾**的截断（`truncate_text_middle`）：
只砍尾巴会让排在后面的整张表完全不可见，而「配表 A 改了、配表 B 也要跟着改」正是
这个平台最关心的风险。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from services.ai.budget import ContextItem, truncate_text, truncate_text_middle
from services.ai.protocol import ContextRequest, DroppedItem
from services.ai.scope import normalize_path

# 单条上下文的字符上限。Excel 的 diff 通常是最大的，但也不该无限大。
#
# 这三个 11,000 是**与预算对账出来的**，不是拍的：150 个提交的版本上，变更摘要要占掉约
# 39,000 字符，历史结论基线摘要再占 6,000（增量分析每轮都带），加上平台 skill 与知识包
# 约 12,000，预算 200,000 里剩下约 143,000 给上下文；按 12 次索取分摊，每条约 11,900，
# 取下限 11,000，留一点余量给「多轮里累积的回复文本」。
# 原先是 14,000 —— 12 次 × 14,000 = 168,000，加上摘要就已经超预算，模型索要 12 个文件
# 会有一半被截断。**条数上限 8 与 `DEFAULT_MAX_TOOL_REQUESTS` 是两回事**：前者约束同一次
# 分析里能同时带多少条，后者约束整个分析期间能要多少次。
DEFAULT_TOOL_LIMITS: Mapping[str, int] = {
    "commit_detail": 6_000,
    "file_diff": 11_000,
    "file_content": 11_000,
    "read_reference": 11_000,
}

# 工具请求的总预算（按「次数」计，不是按条数）。
DEFAULT_MAX_TOOL_REQUESTS = 12

# 结构化内容（按行块排列）用保留首尾的截断；纯文本用普通截断。
_STRUCTURED_KINDS = frozenset({"file_diff"})


@runtime_checkable
class ContextProvider(Protocol):
    """真实取数的四个入口。返回值语义：

    * 返回字符串 → 成功。空字符串表示「确实没有内容」（例如该文件在此提交里是新增，
      没有可对比的旧版本），调用方会原样告诉模型。
    * 返回 `None` → 拿不到（仓库没检出、commit 不存在、文件二进制不可解析……）。
    * 抛异常 → 同上，但会被记进 trace 的原因里。
    """

    def commit_detail(self, commit: str) -> str | None: ...

    def file_diff(self, commit: str, path: str) -> str | None: ...

    def file_content(self, commit: str, path: str) -> str | None: ...

    def read_reference(self, name: str) -> str | None: ...


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
    # 截断过的条数。
    truncated: int = 0

    @property
    def has_failures(self) -> bool:
        return any(item.meta.get("tool_failed") for item in self.items)


def _cache_key(request: ContextRequest) -> tuple[str, str, str, str]:
    return (
        request.type,
        request.commit or "",
        normalize_path(request.path),
        request.name or "",
    )


def describe_request(request: ContextRequest) -> str:
    """给模型看的一行标签。也用于续跑摘要（`budget.build_continuation_summary`）。"""
    if request.type == "read_reference":
        return f"read_reference {request.name}"
    if request.type == "commit_detail":
        return f"commit_detail {request.commit[:12]}"
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


@dataclass
class ContextTools:
    """带缓存、预算与记账的工具执行器。

    一次分析创建一个实例（缓存随之只活一次，见模块注释）。
    """

    provider: ContextProvider
    max_tool_requests: int = DEFAULT_MAX_TOOL_REQUESTS
    limits: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_TOOL_LIMITS))

    _cache: dict[tuple[str, str, str, str], ContextItem] = field(
        default_factory=dict, init=False, repr=False
    )
    _executions: int = field(default=0, init=False, repr=False)
    _cache_hits: int = field(default=0, init=False, repr=False)
    # 累计请求数。**必须是跨轮次的累计值**：预算按「一次分析总共能要几次」计，
    # 如果按每一轮的下标判断，第二轮又从 0 开始，预算永远不会触发（第一版就是这样）。
    _requests_seen: int = field(default=0, init=False, repr=False)

    @property
    def executions(self) -> int:
        return self._executions

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    @property
    def requests_seen(self) -> int:
        return self._requests_seen

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

        text = str(raw)
        if not text.strip():
            return ContextItem(
                kind=request.type,
                label=label,
                text=_empty_text(request),
                meta={"tool_empty": True},
            )

        limit = self._limit_for(request.type)
        if request.type in _STRUCTURED_KINDS:
            text, truncated = truncate_text_middle(text, limit)
        else:
            text, truncated = truncate_text(text, limit)

        meta: dict[str, Any] = {"original_chars": len(str(raw))}
        if truncated:
            meta["truncated"] = True
            meta["limit"] = limit
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
        truncated = 0

        for index, request in enumerate(requests):
            if self._requests_seen >= self.max_tool_requests:
                refused += 1
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

            key = _cache_key(request)
            cached = self._cache.get(key)
            if cached is not None:
                cache_hits += 1
                self._cache_hits += 1
                items.append(cached)
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

            executions += 1
            self._executions += 1
            if item.meta.get("truncated"):
                truncated += 1
            self._cache[key] = item
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
            truncated=truncated,
        )

    def _fetch(self, request: ContextRequest) -> str | None:
        if request.type == "commit_detail":
            return self.provider.commit_detail(request.commit)
        if request.type == "file_diff":
            return self.provider.file_diff(request.commit, normalize_path(request.path))
        if request.type == "file_content":
            return self.provider.file_content(request.commit, normalize_path(request.path))
        if request.type == "read_reference":
            return self.provider.read_reference(request.name)
        # 走到这里说明 `sanitize_requests` 漏了一种类型。宁可如实报失败，也不要
        # 静默返回空——那会让模型以为拿到了内容。
        return None
