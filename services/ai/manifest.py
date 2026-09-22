"""Deterministic, pageable inventory for one AI analysis input snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from services.ai.scope import normalize_path

_CRITICAL_MARKERS = (
    "protocol",
    "proto",
    "payment",
    "currency",
    "reward",
    "login",
    "serverlist",
    "cfg",
)


@dataclass(frozen=True)
class ManifestEntry:
    repository_id: int
    repository_name: str
    commit: str
    path: str
    extension: str
    directory: str
    source: str
    risk_features: tuple[str, ...]
    assigned_shards: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["risk_features"] = list(self.risk_features)
        payload["assigned_shards"] = list(self.assigned_shards)
        return payload


@dataclass(frozen=True)
class ManifestPlan:
    entries: tuple[ManifestEntry, ...]
    shard_count: int
    assignment_digest: str

    @property
    def assigned_rate(self) -> float:
        if not self.entries:
            return 1.0
        return sum(bool(item.assigned_shards) for item in self.entries) / len(self.entries)

    def summary(self) -> dict[str, Any]:
        by_shard = {f"S{index}": 0 for index in range(1, self.shard_count + 1)}
        for item in self.entries:
            for shard in item.assigned_shards:
                by_shard[shard] = by_shard.get(shard, 0) + 1
        return {
            "total": len(self.entries),
            "assigned": sum(bool(item.assigned_shards) for item in self.entries),
            "assigned_rate": round(self.assigned_rate, 6),
            "by_shard": by_shard,
            "assignment_digest": self.assignment_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "shard_count": self.shard_count,
            "entries": [item.to_dict() for item in self.entries],
        }


def _features(path: str, source: str) -> tuple[str, ...]:
    lowered = path.lower()
    result: list[str] = []
    if any(marker in lowered for marker in _CRITICAL_MARKERS):
        result.append("critical_path")
    if PurePosixPath(path).suffix.lower() in {".xlsx", ".xls", ".csv", ".json", ".lua"}:
        result.append("structured_data")
    if source in {"compensation", "dependency"}:
        result.append(source)
    return tuple(result)


def _shards(key: str, *, shard_count: int, critical: bool) -> tuple[str, ...]:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    primary = digest[0] % shard_count
    result = [f"S{primary + 1}"]
    if critical and shard_count > 1:
        secondary = (primary + 1 + digest[1] % (shard_count - 1)) % shard_count
        result.append(f"S{secondary + 1}")
    return tuple(dict.fromkeys(result))


def build_manifest(rows: Iterable[Mapping[str, Any]], *, shard_count: int) -> ManifestPlan:
    count = max(1, int(shard_count or 1))
    canonical: dict[tuple[int, str, str], Mapping[str, Any]] = {}
    for row in rows or ():
        path = normalize_path(str(row.get("file_path") or row.get("path") or ""))
        commit = str(row.get("latest_commit_id") or row.get("commit") or "").strip()
        if not path:
            continue
        repository_id = int(row.get("repository_id") or 0)
        canonical.setdefault((repository_id, path, commit), row)

    entries: list[ManifestEntry] = []
    for repository_id, path, commit in sorted(canonical):
        row = canonical[(repository_id, path, commit)]
        source = str(row.get("source") or "delta")
        features = _features(path, source)
        key = f"{repository_id}\0{path}\0{commit}"
        entries.append(
            ManifestEntry(
                repository_id=repository_id,
                repository_name=str(row.get("repository_name") or ""),
                commit=commit,
                path=path,
                extension=PurePosixPath(path).suffix.lower(),
                directory=str(PurePosixPath(path).parent),
                source=source,
                risk_features=features,
                assigned_shards=_shards(
                    key, shard_count=count, critical="critical_path" in features
                ),
            )
        )
    digest_body = json.dumps(
        [(item.path, item.commit, item.assigned_shards) for item in entries],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return ManifestPlan(
        entries=tuple(entries),
        shard_count=count,
        assignment_digest=hashlib.sha256(digest_body.encode("utf-8")).hexdigest(),
    )


def manifest_page(
    plan: ManifestPlan,
    *,
    cursor: int = 0,
    limit: int = 100,
    shard: str = "",
) -> dict[str, Any]:
    rows = [item for item in plan.entries if not shard or shard in item.assigned_shards]
    start = max(0, int(cursor or 0))
    size = max(1, min(int(limit or 100), 500))
    end = min(start + size, len(rows))
    return {
        "total": len(rows),
        "cursor": start,
        "next_cursor": end if end < len(rows) else None,
        "items": [item.to_dict() for item in rows[start:end]],
        "assignment_digest": plan.assignment_digest,
    }


def render_manifest_reference(plan: ManifestPlan, *, page_size: int = 100) -> str:
    """Render the full manifest as numbered Markdown sections for read_reference paging."""
    size = max(1, min(int(page_size or 100), 500))
    lines = [
        "# change-manifest",
        "",
        f"总文件数：{len(plan.entries)}；分配指纹：`{plan.assignment_digest}`。",
    ]
    for start in range(0, len(plan.entries), size):
        page = start // size + 1
        lines.extend(("", f"## 第 {page} 页（{start + 1}-{min(start + size, len(plan.entries))}）", ""))
        for item in plan.entries[start : start + size]:
            shards = ",".join(item.assigned_shards)
            lines.append(
                f"- [{shards}] `{item.path}` @ `{item.commit[:12]}` "
                f"source={item.source or 'delta'}"
            )
    return "\n".join(lines).rstrip() + "\n"
