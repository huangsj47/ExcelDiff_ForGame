"""Deterministic, pageable inventory for one AI analysis input snapshot.

## 两个入口，同一份指纹（这一条是本模块存在的理由）

`build_manifest` 从一批变更行造一份计划；`ManifestPlan.from_dict` 从**已经落库的那一份**
（`payload["manifest"]`）还原。后者不是多余的：一次运行里读这份计划的**有四个人** ——
提示词里那句「分配指纹」、落库 payload、`read_reference` 读 `change-manifest` 时看到的
S 标签、以及成员实际分到的文件（`subagent.apply_manifest`）。四个人各造一份的结果是
「模型读到的 S 标签」与「它实际分到的文件」互相矛盾，而默认 3 个分片时两个数**巧合相等**
（提示词原先写死 3），所以只有 `subagent_count != 3` 时才现形。

## 分页：`render_manifest_reference` 是**唯一**的生产通路

`platform_provider.read_reference("change-manifest")` → 本函数 → `windowed_view` 按
`^#{1,6} ` 切段，模型用 `lines` 点名第几段。所以**节号必须等于页号**（第一段从正文第一行
开始，正文若以标题行开头就会多出一段 —— 见 `render_manifest_reference` 的说明）。

（这里原先还有一个 `manifest_page`：零生产调用点，只被测试用。留着它比删掉更危险 ——
下一个人会以为「分页已经实现了」，而真正在跑的是上面那条路。已删。）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Optional, Sequence

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

    @classmethod
    def from_dict(cls, payload: Any) -> "ManifestPlan":
        """落库的 `payload["manifest"]` → 计划（**坏数据回空计划，不抛也不编**）。

        分配指纹**优先用落库的那一个**：这份计划要与账本（同一个 payload）逐字对齐，
        而「重算」只有在缺值时才是补位。分片数缺失时从 S 标签里认回来（`S7` → 7），
        认不出来才退回 1 —— 退回 3（历史默认值）会让读的人以为这份清单真的分了 3 片。
        """
        data = payload if isinstance(payload, Mapping) else {}
        raw = data.get("entries")
        entries: list[ManifestEntry] = []
        for row in raw if isinstance(raw, (list, tuple)) else ():
            entry = _entry_from_dict(row)
            if entry is not None:
                entries.append(entry)
        stored = str(data.get("assignment_digest") or "").strip()
        return cls(
            entries=tuple(entries),
            shard_count=_shard_count(data, entries),
            assignment_digest=stored or _digest(entries),
        )

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


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _entry_from_dict(row: Any) -> Optional[ManifestEntry]:
    item = row if isinstance(row, Mapping) else {}
    path = normalize_path(str(item.get("path") or ""))
    if not path:
        return None

    def names(key: str) -> tuple[str, ...]:
        value = item.get(key)
        return tuple(str(one) for one in value) if isinstance(value, (list, tuple)) else ()

    return ManifestEntry(
        repository_id=_int(item.get("repository_id")),
        repository_name=str(item.get("repository_name") or ""),
        commit=str(item.get("commit") or ""),
        path=path,
        extension=str(item.get("extension") or PurePosixPath(path).suffix.lower()),
        directory=str(item.get("directory") or PurePosixPath(path).parent),
        source=str(item.get("source") or "delta"),
        risk_features=names("risk_features"),
        assigned_shards=names("assigned_shards"),
    )


def _shard_count(data: Mapping[str, Any], entries: Sequence[ManifestEntry]) -> int:
    count = _int(data.get("shard_count"))
    if count >= 1:
        return count
    labels = [
        int(name[1:])
        for item in entries
        for name in item.assigned_shards
        if name[1:].isdigit()
    ]
    return max(labels) if labels else 1


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


def _digest(entries: Sequence[ManifestEntry]) -> str:
    """分配指纹：**(路径, 提交, 分片标签) 三样，按入口顺序**。

    `build_manifest` 与 `from_dict` 共用它 —— 两处各写一遍的话，「还原」出来的指纹
    迟早与造出来的不等，而那个差异只表现为「提示词里的指纹与账本对不上」。
    """
    body = json.dumps(
        [(item.path, item.commit, item.assigned_shards) for item in entries],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
    return ManifestPlan(
        entries=tuple(entries),
        shard_count=count,
        assignment_digest=_digest(entries),
    )


def render_manifest_reference(plan: ManifestPlan, *, page_size: int = 100) -> str:
    """完整清单 → 可点名分页的 Markdown（`read_reference("change-manifest")` 的唯一来源）。

    ## 为什么抬头那两行并进第 1 页

    `windowed_view._split_document` 按 `^#{1,6} ` 切段，而**第一段永远从正文第一行开始**：
    正文若以标题行开头（旧版这里是 `# change-manifest`），它就自成一段，于是
    「第 N 节」= 「第 N-1 页」—— 提示词里那句「`lines` 写页号」就成了假话（模型点名第 3 页
    拿到的是第 2 页，而它不会知道自己点错了）。所以清单的抬头（文件数 / 分片数 / 指纹）
    **并进第 1 页**，节号与页号从此严格相等（`test_ai_task_e_completion` 钉着这一点）。

    `page_size` 默认 100：一页约 8.5k 字符，加上 `windowed_view` 的抬头仍落在
    `context_tools.DEFAULT_TOOL_LIMITS["read_reference"]`（11,000）之内。
    """
    size = max(1, min(int(page_size or 100), 500))
    total = len(plan.entries)
    lines: list[str] = []
    for start in range(0, max(total, 1), size):
        chunk = plan.entries[start : start + size]
        page = start // size + 1
        span = f"{start + 1}-{start + len(chunk)}" if chunk else "0"
        lines.extend((f"## 第 {page} 页（{span}）", ""))
        if page == 1:
            lines.extend(
                (
                    f"`change-manifest`（完整清单）共 {total} 个文件；"
                    f"分片数 {plan.shard_count}；分配指纹 `{plan.assignment_digest}`。",
                    '本节号就是页号：`lines` 写 `"2"` 拿到的就是第 2 页。',
                    "",
                )
            )
        for item in chunk:
            shards = ",".join(item.assigned_shards)
            lines.append(
                f"- [{shards}] `{item.path}` @ `{item.commit[:12]}` "
                f"source={item.source or 'delta'}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
