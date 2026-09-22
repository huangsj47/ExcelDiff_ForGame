"""Reusable inverted index for reference queries within a frozen snapshot."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterable

from services.ai.reference_search import MAX_HITS, Hit, is_binary
from utils.text_decoding import decode_text_bytes

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d{3,}")


@dataclass(frozen=True)
class IndexedSearchResult:
    query: str
    hits: tuple[Hit, ...]
    files_total: int
    scanned: int
    missing: int
    binary: int
    truncated_files: bool
    truncated_hits: bool
    prefix: str
    index_version: str
    snapshot_digest: str

    @property
    def hit_files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(hit.path for hit in self.hits))


@dataclass(frozen=True)
class SnapshotReferenceIndex:
    entries: tuple[tuple[str, str], ...]
    lines_by_path: dict[str, tuple[str, ...]]
    hits_by_token: dict[str, tuple[Hit, ...]]
    missing_paths: frozenset[str]
    binary_paths: frozenset[str]
    version: str
    snapshot_digest: str

    @classmethod
    def build(
        cls,
        entries: Iterable[tuple[str, str]],
        *,
        reader: Callable[[str, str], object],
        version: str = "reference-index/v1",
    ) -> "SnapshotReferenceIndex":
        pairs = tuple(dict.fromkeys((str(path), str(commit)) for path, commit in entries))
        lines_by_path: dict[str, tuple[str, ...]] = {}
        token_hits: dict[str, list[Hit]] = {}
        missing: set[str] = set()
        binary: set[str] = set()
        for path, commit in pairs:
            try:
                content = reader(path, commit)
            except Exception:  # a missing blob is an index gap, not an index failure
                content = None
            if content is None:
                missing.add(path)
                continue
            if is_binary(content):
                binary.add(path)
                continue
            if isinstance(content, bytes):
                content = decode_text_bytes(content)
            lines = tuple(str(content).splitlines())
            lines_by_path[path] = lines
            for number, line in enumerate(lines, start=1):
                tokens = {match.group(0).lower() for match in _TOKEN_RE.finditer(line)}
                for token in tokens:
                    token_hits.setdefault(token, []).append(
                        Hit(path=path, line=number, text=line.strip()[:200])
                    )
        digest_body = "\n".join(f"{path}\0{commit}" for path, commit in pairs)
        return cls(
            entries=pairs,
            lines_by_path=lines_by_path,
            hits_by_token={key: tuple(value) for key, value in token_hits.items()},
            missing_paths=frozenset(missing),
            binary_paths=frozenset(binary),
            version=version,
            snapshot_digest=hashlib.sha256(digest_body.encode("utf-8")).hexdigest(),
        )

    def search(self, query: str, *, prefix: str = "", max_hits: int = MAX_HITS) -> IndexedSearchResult:
        needle = str(query or "").strip().lower()
        paths = tuple(path for path, _commit in self.entries if not prefix or path.startswith(prefix))
        allowed = set(paths)
        exact = self.hits_by_token.get(needle)
        if exact is None:
            found: list[Hit] = []
            for path in paths:
                for number, line in enumerate(self.lines_by_path.get(path, ()), start=1):
                    if needle in line.lower():
                        found.append(Hit(path=path, line=number, text=line.strip()[:200]))
            candidates = found
        else:
            candidates = [hit for hit in exact if hit.path in allowed]
        clipped = tuple(candidates[:max_hits])
        missing = len(self.missing_paths & allowed)
        binary = len(self.binary_paths & allowed)
        return IndexedSearchResult(
            query=query,
            hits=clipped,
            files_total=len(paths),
            scanned=sum(path in self.lines_by_path for path in paths),
            missing=missing,
            binary=binary,
            truncated_files=False,
            truncated_hits=len(candidates) > len(clipped),
            prefix=prefix,
            index_version=self.version,
            snapshot_digest=self.snapshot_digest,
        )

