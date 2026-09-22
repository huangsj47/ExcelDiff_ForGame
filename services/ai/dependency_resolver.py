"""Bounded one-hop dependency discovery inside a frozen snapshot."""

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
    normalized_roots = tuple(dict.fromkeys(normalize_path(path) for path in roots if path))
    edges: dict[tuple[str, str, str, str], DependencyEdge] = {}
    for source in normalized_roots:
        source_text = docs.get(source, "")
        symbols = _symbols(source_text)
        source_ids = set(_ID_RE.findall(source_text))
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
            shared_ids = sorted(source_ids & set(_ID_RE.findall(target_text)))
            if shared_ids:
                token = shared_ids[0]
                edge = DependencyEdge(source, target, "config_id", token, "medium")
                edges[(source, target, edge.kind, edge.token)] = edge
    ordered_edges = tuple(sorted(edges.values(), key=lambda edge: (edge.source, edge.target, edge.kind)))
    closure = tuple(
        dict.fromkeys((*normalized_roots, *(edge.target for edge in ordered_edges)))
    )
    return DependencyResult(normalized_roots, closure, ordered_edges)

