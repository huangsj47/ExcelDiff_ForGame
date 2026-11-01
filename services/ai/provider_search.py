# -*- coding: utf-8 -*-
"""`find_references` 的取数实现：冻结仓库 / 平台本地 / 业务节点 Agent。

## 为什么单独一个模块

这三条来源各自的取舍（搜多大范围、命中怎么渲染、覆盖率的分母是谁）是**同一件事的三个
分支**，而它们原先住在 `platform_provider` 里 —— 那个文件同时装着 diff 渲染、正文分页、
提交查询与缓存回查，早就贴着 2000 行的红线。搬出来的是**整段**而不是零散函数：这三个
方法互相调用、共用同一个索引缓存与归属表，切开反而要来回 import。

## 形式是 mixin，不是「函数 + 传 provider」

状态（`_search_cache` / `_reference_index` / `_scope`）留在 `PlatformContextProvider` 的
`__init__` 里，这里只放方法：`self.xxx` 照旧解析，搬动是**纯位移**（没有一行逻辑改动），
`monkeypatch.setattr(provider, "_repository_of", …)` 那类测试桩也照旧生效 —— 打桩打的是
实例属性，与定义在哪个类里无关。

## 失败说明的发出方在这里

`[检索不到]` / `[检索还没回来]` 两句由本模块发出，`[检索不可用]` 仍由 provider 的
`find_references` 发出。`tests/test_ai_trace_evidence.py::test_the_prefixes_are_the_ones_
the_provider_emits` 扫源码核对每个前缀都还有发出方 —— 扫描清单必须**同时**包含这两个
模块，否则「搬走一个发出方」会静默地让那条守卫失效（它只会在前缀清单里留一条死代码）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional, Sequence

from services.ai.batch_reference import ReferenceBodies, bounded_git_reader, search_batch_references
from services.ai.reference_cursor import decode_cursor, encode_cursor
from services.ai.repo_reference import search_frozen_repositories
from services.ai.repository_identity import attribution_map
from services.ai.scope import normalize_path
from services.ai.trace_evidence import failure_notice
from services.deployment_mode import is_agent_dispatch_mode
from utils.logger import log_print


def _relay_scope(repository_id, pairs, query, prefix, total_files):
    from services.ai.reference_index import snapshot_digest
    return hashlib.sha256(repr((repository_id, snapshot_digest(pairs), query,
                               prefix, total_files)).encode()).hexdigest()[:24]


class ReferenceSearchMixin:
    """`PlatformContextProvider` 的检索段（见模块 docstring）。"""

    def _search_frozen_repo(self, query: str, prefix: str, cursor: str = "",
                            max_output_chars: int = 7_800) -> Optional[str]:
        """冻结版本的仓库范围检索（**每个仓库各搜一次**，见 `search_frozen_repositories`）。

        **不可用时返回 `None`** —— 调用方退回「只搜本批次」那条路，并补一句范围声明
        （「搜不到」与「没搜那么宽」在模型那里必须分得开）。
        """
        readers, _reason = self._resolved_readers()
        if not readers:
            return None
        extra = (
            {"max_files": int(self._repo_index_files)}
            if self._repo_index_files
            else {}
        )
        try:
            return search_frozen_repositories(readers, query, prefix=prefix, cursor=cursor,
                                               max_output_chars=max_output_chars, **extra)
        except ValueError as exc:
            return f"[检索不到] {exc}。这不等于没有其它引用，请清空 cursor 重查。"

    def _narrow_scope_note(self) -> str:
        """退回「只搜本批次」时补在结果前面的一句**范围声明**（拿得到范围时为空串）。

        没有它，模型会把「本批次里没有命中」读成「仓库里没有引用」—— 而这两句话的证据
        强度差着一个数量级，报告里的结论也因此分叉。
        """
        _reader, reason = self._resolved_reader()
        if _reader is not None:
            return ""
        return (
            f"[范围说明] 本次**没有**仓库冻结范围可用（{reason}），"
            "所以下面这次检索**只覆盖本批次改动过的文件**，"
            "未改动过的文件（例如别的模块里的调用方）不在里面。\n"
            "**「没有命中」只代表本批次里没有**，不能说成「仓库里没有引用」。\n\n"
        )

    def _with_narrow_scope_note(self, text: str) -> str:
        """给结果补上范围声明。**失败说明原样返回**。

        ## 为什么失败时不能补

        `[检索不到]` / `[检索还没回来]` 这几句是**取数层的失败说明**，而记账那一层
        （`trace_evidence.failure_notice` 与 `context_tools` 的 `failed` 计数）按**开头**
        认它们：在它们前面插一段话，这条失败就会被记成「成功取到内容」——
        同一个面板上「失败 0 条」与明细里列出的失败条数于是对不上，而后者被当成
        「取数都很顺」。

        代价是这种情况下范围声明缺席 —— 可以接受：那几句失败说明自己就带着
        「**这不等于「没有其它引用」**，请写成信息缺口」，而「一条都没搜成」比
        「只搜了本批次」是更强的限定。
        """
        note = self._narrow_scope_note()
        if not note:
            return text
        if failure_notice(text):
            return text
        return note + text

    def _search_local(
        self,
        pairs: Sequence[tuple[str, str]],
        query: str,
        *,
        prefix: str,
        cursor: str = "",
        max_output_chars: int = 7_800,
    ) -> Optional[str]:
        """平台本地的工作副本。**单机模式**走这条；多节点模式返回 `None`（改问 Agent）。

        与 `_content_from_agent` 同一套记忆：同一个进程里同一份检索只做一次
        （模型可能对同一个词问两遍，而建索引是要真读文件的）。

        索引使用真实文件/命中分页；查询前缀改变时也能继续读取新范围。
        同一冻结文件的正文由有界缓存复用，cursor 与输出额度同时进入结果缓存键。

        ## 每条路径按**自己的**仓库读

        `pairs` 在跨仓库批次里横跨两个仓库，而 blob 要按仓库取（同一个 `(提交, 路径)`
        在两个仓库里是两份不同的内容）。所以 reader 逐条查归属；定不出归属的那些
        （`_repositories_of` 答不出来）退回 `_repository_of` 那一个 —— 与从前一致，
        而不是把整批都算成「读不到」。
        """
        from services.vcs_content_service import get_file_content_from_git

        key = (query, prefix, cursor, max_output_chars, tuple(pairs))
        if key in self._search_cache:
            return self._search_cache[key]
        # 一次就够的判断：没有本地工作副本时 `get_file_content_from_git` 会返回 None，
        # 于是每个文件都算「读不到」——那会把整批白读一遍，还给出一句
        # 「N 个都读不到」的假话（真相是平台本地根本没有这个仓库）。
        fallback = self._repository_of(pairs)
        if fallback is None:
            return None
        if is_agent_dispatch_mode():
            # 多节点模式下平台被禁止 clone：本地读不到是**确定**的，别去读一遍。
            return None

        attribution = self._attribution(pairs)
        readers = {}
        if not hasattr(self, "_batch_reference_bodies"):
            self._batch_reference_bodies = ReferenceBodies()

        def reader(path: str, commit: str):
            repository = fallback
            repository_id = attribution.get((normalize_path(path), str(commit or "")))
            if repository_id is not None:
                repository = self._repository_row(repository_id) or fallback
            identity = getattr(repository, "id", None)
            if identity not in readers:
                readers[identity] = bounded_git_reader(
                    repository, lambda item, revision, repo=repository: get_file_content_from_git(repo, revision, item))
            return self._batch_reference_bodies.read((identity, path, commit),
                                                     lambda: readers[identity](path, commit))

        try:
            result = search_batch_references(pairs, query, reader=reader,
                                              repository_id=getattr(fallback, "id", None),
                                              prefix=prefix, cursor=cursor,
                                              max_output_chars=max_output_chars - len(self._narrow_scope_note()))
            text = result.text
        except ValueError as exc:
            return f"[检索不到] {exc}。这不等于没有其它引用，请清空 cursor 重查。"
        self._search_cache[key] = text
        return text

    def _search_from_agent(
        self,
        pairs: Sequence[tuple[str, str]],
        query: str,
        *,
        prefix: str,
        total_files: int,
        cursor: str = "",
        max_output_chars: int = 7_800,
    ) -> Optional[str]:
        """业务节点上的 Agent 拿它自己的工作副本搜（platform/agent 模式的唯一取数点）。

        ## 跨仓库的批次**按仓库各派一次**

        原先只取第一个仓库（`_repository_of`），于是另一个仓库的文件从来没被搜过，而结论
        那一句「范围内共 N 个文件」还把它排除在外 —— 模型据此写下「没有其它引用」，看起来
        证据齐全。现在按归属分组各派一次，每段写明是哪个仓库（两段各自的分母不同，不说清
        就成了对同一个问题给出两个数）。

        归属定不出来时（手工构造的 scope）退回单仓旧行为，与从前逐字一致。
        """
        attribution = self._attribution(pairs)
        groups: dict[int, list] = {}
        for pair in pairs:
            repository_id = attribution.get((normalize_path(pair[0]), str(pair[1] or "")))
            if repository_id is not None:
                groups.setdefault(repository_id, []).append(pair)
        if not groups:
            return self._search_agent_one(
                self._repository_of(pairs), pairs, query, prefix=prefix, total_files=total_files,
                cursor=cursor, max_output_chars=max_output_chars
            )

        blocks, receipts = [], []
        group_ids = tuple(sorted(groups))
        group_key = hashlib.sha256(repr((group_ids, pairs, query, prefix)).encode()).hexdigest()[:24]
        try:
            address = decode_cursor(cursor) if cursor else {}
        except ValueError as exc:
            return f"[检索不到] {exc}。请清空 cursor 重查。"
        owner = address.get("repo")
        if "after" in address and address.get("group") != group_key:
            return "[检索不到] cursor 不属于当前多仓库批次，请清空 cursor 重查。"
        active = [item for item in group_ids if owner is None or str(item) == owner]
        if not active:
            return "[检索不到] cursor 所属仓库不在当前批次里，请清空 cursor 重查。"
        queue_after = address.get("after")
        capacity = max(1, max_output_chars // 2_000)
        if owner is None and len(active) > capacity:
            active = active[:capacity]
            queue_after = capacity
        available = max_output_chars - len(self._narrow_scope_note()) - 600
        budget = max(1_200, available // len(active) - 180)
        own_next = ""
        for repository_id in active:
            group = groups[repository_id]
            in_scope = sum(1 for item, _c in group if not prefix or item.startswith(prefix))
            block = self._search_agent_one(
                self._repository_row(repository_id),
                group,
                query,
                prefix=prefix,
                total_files=in_scope,
                cursor=cursor,
                max_output_chars=budget,
            )
            receipt_lines = [line for line in block.splitlines() if line.startswith("[reference_page]")]
            for line in receipt_lines:
                receipts.append(f"【仓库 {self._describe_repository(repository_id)}】{line}")
                block = block.replace(line + "\n", "", 1)
            if owner:
                match = re.search(r'next_cursor="([^"]*)"', "\n".join(receipt_lines))
                own_next = match.group(1) if match else ""
            if len(groups) > 1:
                block = (
                    f"【仓库 {self._describe_repository(repository_id)}】"
                    f"本段只覆盖这个仓库里那 {len(group)} 个改动文件。\n{block}"
                )
            blocks.append(block)
        if queue_after is not None:
            queued = ""
            if own_next:
                next_address = decode_cursor(own_next)
                queued = encode_cursor(next_address["scope"], next_address["repo"], 0, 0,
                                       remote=next_address.get("remote", ""), after=queue_after, group=group_key)
                receipts = [line.replace(own_next, queued) for line in receipts]
                blocks = [body.replace(own_next, queued) for body in blocks]
            elif queue_after < len(group_ids):
                target = group_ids[queue_after]
                group = groups[target]
                total = sum(1 for path, _commit in group if not prefix or path.startswith(prefix))
                queued = encode_cursor(_relay_scope(target, group, query, prefix, total), target, 0, 0,
                                       after=queue_after + 1, group=group_key)
            receipts.insert(0, f'[reference_repository_page] next_cursor="{queued}" '
                               f'pending_repositories={max(0, len(group_ids) - queue_after)}')
        return "\n".join(receipts) + "\n\n" + "\n\n".join(blocks)

    def _search_agent_one(
        self,
        repository,
        pairs: Sequence[tuple[str, str]],
        query: str,
        *,
        prefix: str,
        total_files: int,
        cursor: str = "",
        max_output_chars: int = 7_800,
    ) -> str:
        """向**一个**仓库的业务节点派一次检索（`_search_from_agent` 的分段实现）。"""
        from services.agent_file_content_dispatch import request_references

        if repository is None:
            return (
                "[检索不到] 本批次的改动文件里找不到对应的仓库，无法确定去哪个节点上搜。"
            )
        relay_scope = _relay_scope(getattr(repository, "id", None), pairs, query, prefix, total_files)
        try:
            remote_cursor = decode_cursor(cursor, scope=relay_scope).get("remote", "") if cursor else ""
            extra = {"cursor": remote_cursor, "max_output_chars": max(1_200, max_output_chars - 600)}
            # Old provider test doubles may implement only the original optional
            # parameters. A real continuation must never be silently discarded.
            import inspect
            parameters = inspect.signature(request_references).parameters
            if not any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
                if remote_cursor and "cursor" not in parameters:
                    raise ValueError("Agent 不支持检索游标，请升级后重查")
                extra = {name: value for name, value in extra.items() if name in parameters}
            outcome = request_references(
                repository,
                query=query,
                # **整批**：Agent 也按快照建一次索引，前缀交给它自己的 `search()`。
                # 只发前缀命中的那一批会让 Agent 的索引缓存按前缀分叉 —— 同一个 bug
                # 换个进程再犯一次（第二次查询看到的还是第一个前缀的范围）。
                entries=pairs,
                prefix=prefix,
                # 覆盖率的分母：**本批次里落在这个前缀范围内的文件数**（与本地那条路
                # `index.search(prefix=...)` 算出来的 `files_total` 一致）。两条路对分母的
                # 口径必须一样，否则同一个周版本在单机与多节点下印出互相矛盾的覆盖率，
                # 而模型正是拿这个数决定能不能说「没有其它引用」。
                total_files=total_files,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 —— 取一次检索失败只该让这一条降级
            log_print(f"⚠️ AI 取数：向 Agent 检索失败 {query}: {type(exc).__name__}: {exc}")
            return f"[检索不到] `{query}`：向 Agent 检索时出错（{exc}）。"

        status = str(outcome.get("status") or "")
        if status == "ready":
            rendered = str(outcome.get("text") or "")
            if rendered:
                remote_next = str(outcome.get("next_cursor") or "")
                if remote_next:
                    relay = encode_cursor(relay_scope, getattr(repository, "id", None), 0, 0, remote=remote_next)
                    rendered = rendered.replace(remote_next, relay)
                return rendered
        reason = str(outcome.get("message") or "原因未知")
        # 两句的尾巴逐字相同 —— 用常量而不是抄两遍：`trace_evidence` 按**开头**认这几句，
        # 而两条分支各写一份尾巴的那天起，「还没回来」与「读不到」的原因就会开始漂移。
        tail = (
            "。**这不等于「没有其它引用」。**"
            "这一轮请只依据已取得的证据判断，并在报告里把它写成信息缺口。"
        )
        if status == "pending":
            return f"[检索还没回来] `{query}`：{reason}" + tail
        return f"[检索不到] `{query}`：{reason}" + tail

    def _repository_of(self, pairs: Sequence[tuple[str, str]]):
        """这批路径属于哪个仓库。取第一条能查出提交行的路径的仓库。

        **这是「不知道各自归属」时的兜底**（`_repositories_of` 答不出来才走这里），
        语义与从前逐字一致：跨仓库的批次只用第一个仓库。`_repositories_of` 说得清每条
        路径各自属于哪个仓库时**不要**走这条 —— 那会把另一个仓库的文件算到这一个头上。
        """
        for path, commit in pairs:
            row, _candidates = self._commit_row(commit, path)
            repository = getattr(row, "repository", None)
            if repository is not None:
                return repository
        return None

    def _repositories_of(self, pairs: Sequence[tuple[str, str]]) -> tuple[int, ...]:
        """这批 `(路径, 提交)` 各自属于哪个仓库（去重、升序）。空 = **不知道**。

        与 `_repositories_for(commit)` 的分工：那个答的是「这条提交号在本批次里可能属于
        哪些仓库」（按提交收窄查询用），这个答的是「**这批文件**分别落在哪个仓库上」
        （按仓库派发检索用）。多仓批次里两者不等价，而 `find_references` 要的是后者。
        """
        return tuple(sorted(set(self._attribution(pairs).values())))

    def _attribution(self, pairs: Sequence[tuple[str, str]]) -> dict:
        """`{(路径, 提交): 仓库 id}` —— 逐条的仓库归属（定不出来的键不出现，见模块说明）。"""
        return attribution_map(pairs, scope=self._scope, batch_of=self._repositories_for)
