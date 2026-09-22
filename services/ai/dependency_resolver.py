"""Bounded one-hop dependency discovery inside a frozen snapshot.

# ⚠️ 这个模块**没有接线**（截止 2026-09-22）

生产路径上跑的是**另一个更窄的实现**：`services/ai/scope_sampling.py:40-59` 的
`_same_stem_dependency_entries`。两者差别不是实现细节，是能力：

| | 这里 | 生产在跑的那个 |
|---|---|---|
| 边的种类 | 符号（`require`/`import`/`from`/`register`）、同 stem 生成物、共享编号 | **只有同 stem** |
| 置信度 | high / medium / medium | 没有这个概念 |
| 候选池 | 一个冻结快照里的全部文档 | `WeeklyVersionDiffCache` 的**版本窗口**（不是快照） |

所以「本版本没改、但改了它就要跟着看」那类依赖，生产上**找不到** —— 因为窗口里没有它。
把本模块接上去要连带解决两件事（快照级文档从哪来、置信度往哪落库），那是单独一波。

在那之前：**不许**把这里当成「已经有了依赖闭包」。它现在只有一组单元用例
（`tests/test_ai_task_e_completion.py`）在守它自己的行为。

# 性能（接线前先看这条）

`resolve_dependencies` 是「根 × 文档」的双层循环，所以**每个文档的正则扫描必须只做一次**：
`_ID_RE.findall(target_text)` 原先写在目标循环体内，1000 份文档就是 1000×1000 次扫描。
现在编号集合按文档预计算一遍（`_symbols` 本来就只在根那一层算）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from services.ai.scope import normalize_path

_SYMBOL_RE = re.compile(
    r"(?:require\s*\(?\s*|from\s+|import\s+|register\s*\(\s*)[\"']?"
    r"([A-Za-z_][A-Za-z0-9_./]+)",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"(?<!\d)(\d{4,12})(?!\d)")


@dataclass(frozen=True)
class DependencyDocument:
    path: str
    text: str


@dataclass(frozen=True)
class DependencyEdge:
    source: str
    target: str
    kind: str
    token: str
    confidence: str


@dataclass(frozen=True)
class DependencyResult:
    roots: tuple[str, ...]
    closure: tuple[str, ...]
    edges: tuple[DependencyEdge, ...]


def _symbols(text: str) -> set[str]:
    out: set[str] = set()
    for match in _SYMBOL_RE.finditer(text or ""):
        token = match.group(1).replace(".", "/").rstrip("/")
        leaf = token.rsplit("/", 1)[-1]
        if leaf:
            out.add(leaf.lower())
    return out


def resolve_dependencies(
    roots: Iterable[str], documents: Iterable[DependencyDocument]
) -> DependencyResult:
    docs = {
        normalize_path(item.path): str(item.text or "")
        for item in documents
        if normalize_path(item.path)
    }
    # 编号集合**按文档预计算一次**（见模块抬头的性能那一段）：它在下面那个双层循环里
    # 每一对都要用，写在循环体内就是 O(根 × 文档) 次正则扫描。
    document_ids = {path: set(_ID_RE.findall(text)) for path, text in docs.items()}
    normalized_roots = tuple(dict.fromkeys(normalize_path(path) for path in roots if path))
    edges: dict[tuple[str, str, str, str], DependencyEdge] = {}
    for source in normalized_roots:
        source_text = docs.get(source, "")
        symbols = _symbols(source_text)
        source_ids = document_ids.get(source, set())
        source_stem = PurePosixPath(source).stem.lower()
        for target, target_text in docs.items():
            if target == source:
                continue
            target_stem = PurePosixPath(target).stem.lower()
            if target_stem in symbols:
                edge = DependencyEdge(source, target, "symbol", target_stem, "high")
                edges[(source, target, edge.kind, edge.token)] = edge
                continue
            if target_stem == source_stem:
                edge = DependencyEdge(source, target, "generated_pair", target_stem, "medium")
                edges[(source, target, edge.kind, edge.token)] = edge
                continue
            shared_ids = sorted(source_ids & document_ids.get(target, set()))
            if shared_ids:
                token = shared_ids[0]
                edge = DependencyEdge(source, target, "config_id", token, "medium")
                edges[(source, target, edge.kind, edge.token)] = edge
    ordered_edges = tuple(sorted(edges.values(), key=lambda edge: (edge.source, edge.target, edge.kind)))
    closure = tuple(
        dict.fromkeys((*normalized_roots, *(edge.target for edge in ordered_edges)))
    )
    return DependencyResult(normalized_roots, closure, ordered_edges)

