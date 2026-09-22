"""一次运行内的共享证据仓：正文只存一份，并可按稳定 evidence_id 回查。

## 两个 id，回答的是两个不同的问题

| id | 怎么算 | 回答的问题 |
|---|---|---|
| `evidence_id` | `sha256(请求 key + \\x00 + 正文)[:20]` | 「**这份坐标**的内容在哪」—— 模型点名的 (工具, 提交, 路径, 窗口) |
| `blob_id` | `sha256(正文)[:20]` | 「**这段字节**在哪几处出现过」 |

`evidence_id` 的 hash 输入里**含请求 key**，所以「同一份 blob 只存一份」在 id 层面从来
不成立 —— 成立的是「同一 **window** 只存一份」。同一个文件被两个不同的提交改过、或者
同一份正文在两条不同的请求坐标下被取回，它们的 `evidence_id` 必然不同。要回答「这几条
其实是同一份正文」，唯一的判据是 `blob_id`（只按内容算，见 `blob_id_of`）。

## 回查要能自证没损坏（`verify`）

按 id 把正文交回给模型时，平台等于做了一个承诺：「这就是当初那一份」。而读的路径上
没有任何东西能保证这一点 —— 内存缓存被谁改过、条目被一条形状相同的别的条目顶掉，
都不会报错，只会静默地交出一份**别的**内容。所以读回来时重算一次 `evidence_id` 并比对。

前提是 `meta` 里留着**算这个 id 用过的那些输入**（`cache_key`）—— 没有它就没法重算，
`verify` 只能永远返回「不知道」。所以 `context_tools._with_chunk_id` 把 `cache_key` 与
`blob_id` 一并写进 `meta`，这两处是**一对**改动。

`verify` 的判据是「重算出来的 id 与它自称的一致」，不是「这条内容看起来合理」：它管的是
**损坏与错位**，不是内容质量。
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator, MutableMapping
from typing import Any

from services.ai.budget import ContextItem

# `evidence_id` 与 `blob_id` 都是 sha256 的前 20 位十六进制（与 `context_tools` 里那个
# 生成式同一套）。长度写在这里是因为协议层要按它判「模型给的 id 长得对不对」。
ID_HEX_CHARS = 20


def blob_id_of(text: str) -> str:
    """**只按内容**算的 id：`sha256(正文)[:20]`。

    刻意不含请求 key —— 含了就只能回答「同一 window」，答不了「同一段字节」。
    """
    return hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()[:ID_HEX_CHARS]


def evidence_id_of(cache_key: Any, text: str) -> str:
    """`evidence_id` 的唯一生成式（`context_tools._with_chunk_id` 与 `verify` 共用）。

    两处各写一遍 hash 拼法，迟早会在某次「顺手调一下分隔符」之后对不上 —— 而那种错
    不会报错，只会让 `verify` 从此对每一份正文都说「损坏」，于是整条共享缓存静默失效。
    """
    digest = hashlib.sha256()
    digest.update("\x1f".join(str(part or "") for part in tuple(cache_key)).encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(str(text).encode("utf-8", errors="replace"))
    return digest.hexdigest()[:ID_HEX_CHARS]


class EvidenceStore(MutableMapping[Any, ContextItem]):
    """按请求 key 存正文，同时按 `evidence_id` / `blob_id` 建两份索引。

    `MutableMapping` 那一面是给 `context_tools` 的缓存用的（`body_cache=` 的契约）；
    `by_id` / `by_blob` / `blob_index` / `verify` 是给它真正的用途 ——
    **让「按证据地址回查」成为一件做得到的事**（`type: evidence` 的请求）。
    """

    def __init__(self) -> None:
        self._by_request: dict[Any, ContextItem] = {}
        self._by_id: dict[str, ContextItem] = {}
        # blob_id → [evidence_id, ...]（保序、去重）。**不是一对一的**：同一段字节可以在
        # 多个坐标下各存一份（见模块 docstring 的表格）。
        self._by_blob: dict[str, list[str]] = {}

    # -- 索引 -----------------------------------------------------------------
    #
    # 三份索引都由这两个方法维护（增与删各一处）。散在调用点上写，一定会出现「删了
    # `_by_request` 忘了删 `_by_blob`」——而那表现为「按地址还能查到一份已经不存在的
    # 正文」，正是这一层最该避免的失真。

    @staticmethod
    def _evidence_id_of(item: ContextItem) -> str:
        return str(item.meta.get("evidence_id") or item.meta.get("chunk_id") or "")

    def _index(self, item: ContextItem) -> None:
        evidence_id = self._evidence_id_of(item)
        if evidence_id:
            self._by_id[evidence_id] = item
            # `blob_id` **现场按正文重算**，不信 `meta` 里存的那个：索引的用途是回答
            # 「这段字节在哪」，用一份可以被改写的副本去建它，等于索引本身也可被伪造。
            blob = blob_id_of(item.text)
            bucket = self._by_blob.setdefault(blob, [])
            if evidence_id not in bucket:
                bucket.append(evidence_id)

    def _unindex(self, item: ContextItem) -> None:
        evidence_id = self._evidence_id_of(item)
        if not evidence_id:
            return
        if self._by_id.get(evidence_id) is item:
            self._by_id.pop(evidence_id, None)
        bucket = self._by_blob.get(blob_id_of(item.text))
        if bucket and evidence_id in bucket:
            bucket.remove(evidence_id)
            if not bucket:
                self._by_blob.pop(blob_id_of(item.text), None)

    # -- MutableMapping -------------------------------------------------------

    def __getitem__(self, key: Any) -> ContextItem:
        return self._by_request[key]

    def __setitem__(self, key: Any, value: ContextItem) -> None:
        previous = self._by_request.get(key)
        if previous is not None and previous is not value:
            self._unindex(previous)
        self._by_request[key] = value
        self._index(value)

    def __delitem__(self, key: Any) -> None:
        value = self._by_request.pop(key)
        self._unindex(value)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._by_request)

    def __len__(self) -> int:
        return len(self._by_request)

    # -- 回查 -----------------------------------------------------------------

    def by_id(self, evidence_id: str) -> ContextItem | None:
        """按 `evidence_id` 取正文。取不到返回 `None`（调用方据此报「取不到」）。"""
        return self._by_id.get(str(evidence_id or ""))

    def by_blob(self, blob_id: str) -> tuple[ContextItem, ...]:
        """这一份内容在各坐标上的条目（`blob_index()` 那一行的展开）。

        **返回条目而不是 id**，因为调用方要问的是「这段字节长什么样」，多半还要比一比
        各坐标下的 `label`。查不到返回空元组（不是 `None`：这里本来就是一对多）。
        """
        items: list[ContextItem] = []
        for evidence_id in self._by_blob.get(str(blob_id or ""), ()):
            item = self._by_id.get(evidence_id)
            if item is not None:
                items.append(item)
        return tuple(items)

    def blob_index(self) -> dict[str, list[str]]:
        """`{blob_id: [evidence_id, ...]}`，**深拷贝**（调用方改不坏内部索引）。"""
        return {blob: list(ids) for blob, ids in self._by_blob.items() if ids}

    def verify(self, evidence_id: str) -> bool:
        """这条地址指向的正文还是当初那一份吗。

        返回 `True` 的条件有三个，缺一不可：**地址在**、`meta` 里留着 `cache_key`、
        重算出来的 id 与它自称的一致。任何一个不满足都是 `False` —— 这一层的读者
        （`context_tools` 的共享命中分支与 `type: evidence` 的取回）要做的动作是
        「改用真取数 / 报取不到」，所以宁可判「不可信」，不赌它是对的。

        **不能只判「地址在不在」**：那一条早就成立了（`_by_id` 的键就是它），而那正是
        这次要补的东西 —— 原先读完从不校验。
        """
        item = self.by_id(evidence_id)
        if item is None:
            return False
        cache_key = item.meta.get("cache_key")
        if not isinstance(cache_key, (list, tuple)) or not cache_key:
            return False
        return evidence_id_of(cache_key, item.text) == str(evidence_id)

    def summary(self) -> dict[str, int]:
        return {
            "request_keys": len(self._by_request),
            "unique_evidence": len(self._by_id),
            "stored_chars": sum(len(item.text) for item in self._by_id.values()),
        }
