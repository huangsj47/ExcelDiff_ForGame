#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diff 快照的**落库 / 读取 / 做差**（AI-P0-02 的数据层）。

模型与判据在 `models/ai_analysis/diff_snapshot.py` 的模块抬头里写死了（条目身份是
`(config_id, file_path)`、内容身份是 `(base_commit_id, latest_commit_id, diff_version)`）；
这一层是读写它的**唯一入口**，也是「这一次和上一次差在哪」这句话的唯一实现。

## 为什么 `content_digest` 必须与 `weekly_snapshot_digest` 逐字相同

`AiAnalysisRun.active_key`（「同一目标 + 同一份输入」的数据库唯一认领）里含这个指纹，
而它同时是**幂等键**：换一套算法，所有历史活动运行的键就不再匹配 —— 同一次输入会被
放行第二次，而代价是静默的（没有任何一处会报错，只是多花一次钱）。

所以 `_digest_of` 与 `services/ai/scope_sampling.weekly_snapshot_digest` 用的是同一串
格式化、同一个分隔符、同一个 sha1，`tests/test_ai_diff_snapshot_baseline.py` 拿同一批
数据把两个值对起来量 —— 那是防止两份实现各自漂移的唯一办法。

## 做差为什么不看 `updated_at`

那是**写库时刻**（同步每 2~3 分钟跑一遍，任何一次重算都会顶高它），既不是「内容变了」
也不是「谁变了」。实测 Run 20 与 Run 15 之间只有 45 个路径身份变化，用 `updated_at`
判却是整批都「新」。

## 完整覆盖门槛

`covers_completely` 回答的是「这份快照**模型真的看完了**没有」，与「这份清单是不是
冻结的」（`status == sealed`）**不是一回事**：冻结只说明清单完整，覆盖说的是证据。
判据的输入是覆盖账本（`services/ai/coverage_ledger.build_ledger`），**拿不到覆盖数据时
返回 False** —— 不许把未知当完整，那正是「94% 没读到却被标成已分析」的翻版。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from models import WeeklyVersionDiffCache, db
from models.ai_analysis import AiDiffSnapshot, AiDiffSnapshotItem
from models.ai_analysis.diff_snapshot import (
    CHANGE_ADDED,  # noqa: F401 —— 由本模块转出，读侧按名字取用
    CHANGE_CHANGED,  # noqa: F401
    CHANGE_UNCHANGED,  # noqa: F401
    STATUS_SEALED,
)
from services.ai.coverage_ledger import FILE_EVIDENCE_KINDS, parse_evidence_label
from services.ai.header_scope import header_scope_fingerprint, scoped_version
from services.ai.trace_evidence import decode_evidence

#: 条目键：`(config_id, file_path)`。与模型上的唯一索引是同一对。
ItemKey = Tuple[int, str]
#: 内容身份：`(base_commit_id, latest_commit_id, diff_version)`。
Identity = Tuple[Optional[str], Optional[str], Optional[str]]

#: 完整覆盖门槛的**默认值**（可被项目配置的 `complete_coverage_ratio` 覆盖）。
#:
#: 定在 0.9 而不是 1.0：证据是按「(本批次最新提交, 文件)」去重的，模型翻某个文件更早
#: 那条提交时按保守口径不算覆盖 —— 那类条目在真实运行里是常态。定 1.0 会让门槛永远
#: 不达标，于是 `last_complete_snapshot_id` 永远为空、「已完整检查」这句话永远说不出口
#: （说不出口是对的，但那时这个门槛就成了一件摆设，而不是一把尺子）。
DEFAULT_COMPLETE_COVERAGE_RATIO = 0.9

#: 完整覆盖门槛的配置键（`ai_project_analysis_config` 的那份 dict 里）。
COMPLETE_COVERAGE_CONFIG_KEY = "complete_coverage_ratio"

#: 补偿集的默认上限（可被项目配置的 `compensation_max_files` 覆盖）。
#:
#: **它必须显著小于 `scope_sampling.FULL_ANALYSIS_FILE_THRESHOLD`（50）**：补偿项也算
#: 进本轮的输入规模，而输入规模正是「要不要干脆跑全量」的判据之一。补偿集比阈值还大，
#: 会让每一次增量都被自己顶成全量。
DEFAULT_COMPENSATION_MAX_FILES = 20
COMPENSATION_CONFIG_KEY = "compensation_max_files"


def _utcnow() -> datetime:
    """naive UTC —— 库里存的就是 naive（`DateTime` 无时区），比较前必须统一。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
#  落库
# ---------------------------------------------------------------------------


def _cache_rows(config_ids: Sequence[int]) -> List[WeeklyVersionDiffCache]:
    if not config_ids:
        return []
    return (
        WeeklyVersionDiffCache.query
        .filter(WeeklyVersionDiffCache.config_id.in_(list(config_ids)))
        .all()
    )


def _digest_of(rows: Sequence[Any], scope: str = "") -> str:
    """`(file_path, base, latest, diff_version)` 的内容指纹。

    **与 `scope_sampling.weekly_snapshot_digest` 逐字相同**（见模块抬头）。两边的输入
    来自同一条查询、同一个排序、同一个分隔符；`None` 一律折成空串。
    """
    parts = sorted(
        "%s|%s|%s|%s|%s"
        % (
            getattr(row, "file_path", None) or "",
            getattr(row, "base_commit_id", None) or "",
            getattr(row, "latest_commit_id", None) or "",
            getattr(row, "diff_version", None) or "",
            scope,
        )
        for row in rows
    )
    if not parts:
        return ""
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def seal_snapshot(
    config_ids: Sequence[int], *, group_key: str, project_id: Optional[int]
) -> Optional[AiDiffSnapshot]:
    """把这一批配置**当前这一份清单**冻结成快照。同一份指纹已存在就复用那一份。

    返回 `None` 只有一种情形：这批配置一行缓存都没有（空清单不是一份快照 —— 拿它当
    基准会把整份窗口都算成「新增」）。

    **只 `flush` 不 `commit`**：调用方（`_create_run`）随后就在同一个事务里提交，
    让它一起落库/一起回滚。快照与它服务的那条 run 必须同生共死。
    """
    if not config_ids or not group_key:
        return None
    rows = _cache_rows(config_ids)
    # 比较口径（表头坐标 / 多套表头方案）：并进内容身份，于是「改了怎么读」在做差里
    # 等价于「换了一份输入」。理由见 `services/ai/header_scope.py` 的模块抬头。
    scope = header_scope_fingerprint(config_ids)
    digest = _digest_of(rows, scope)
    if not digest:
        return None

    existing = AiDiffSnapshot.query.filter_by(
        group_key=group_key, content_digest=digest
    ).first()
    if existing is not None:
        return existing

    snapshot = AiDiffSnapshot(
        project_id=project_id,
        group_key=group_key,
        content_digest=digest,
        item_count=len(rows),
        complete=False,
        status=STATUS_SEALED,
        sealed_at=_utcnow(),
    )
    db.session.add(snapshot)
    db.session.flush()

    seen: set = set()
    for row in rows:
        key = (row.config_id, row.file_path)
        if key in seen:
            # `file_path` 在 `(config_id, file_path)` 下唯一是这张表的前提；真出现重复
            # 也不能让 INSERT 撞唯一索引 —— 那时整条 run 都建不出来，代价远大于少一条。
            continue
        seen.add(key)
        db.session.add(
            AiDiffSnapshotItem(
                snapshot_id=snapshot.id,
                config_id=row.config_id,
                repository_id=row.repository_id,
                file_path=row.file_path,
                base_commit_id=row.base_commit_id,
                latest_commit_id=row.latest_commit_id,
                diff_version=scoped_version(row.diff_version, scope),
            )
        )
    db.session.flush()
    return snapshot


def mark_complete(snapshot_id: Optional[int]) -> None:
    """把一份快照标成「已达完整覆盖门槛」。调用方负责提交。"""
    if not snapshot_id:
        return
    snapshot = db.session.get(AiDiffSnapshot, snapshot_id)
    if snapshot is not None and not snapshot.complete:
        snapshot.complete = True


# ---------------------------------------------------------------------------
#  读取
# ---------------------------------------------------------------------------


def _newest(*conditions) -> Optional[AiDiffSnapshot]:
    return (
        AiDiffSnapshot.query.filter(*conditions)
        .order_by(AiDiffSnapshot.created_at.desc(), AiDiffSnapshot.id.desc())
        .first()
    )


def latest_sealed_snapshot(group_key: str) -> Optional[AiDiffSnapshot]:
    """这个组最近一份**冻结**的快照 —— 做差的默认基准。

    这里**不看 `complete`**：降级运行的目标快照照样能当基准（复测文档 AI-P0-02：
    「降级结论可以作为下一轮的结论基线，但不能伪装为完整覆盖」）。完整与否是
    `latest_complete_snapshot` 的事，它只决定「已完整检查」这句话能不能说。
    """
    if not group_key:
        return None
    return _newest(
        AiDiffSnapshot.group_key == group_key, AiDiffSnapshot.status == STATUS_SEALED
    )


def latest_complete_snapshot(group_key: str) -> Optional[AiDiffSnapshot]:
    """这个组最近一份**达到完整覆盖门槛**的快照（`last_complete_snapshot_id` 的取值域）。"""
    if not group_key:
        return None
    return _newest(
        AiDiffSnapshot.group_key == group_key,
        AiDiffSnapshot.status == STATUS_SEALED,
        AiDiffSnapshot.complete.is_(True),
    )


def snapshot_items(snapshot_id: Optional[int]) -> Dict[ItemKey, AiDiffSnapshotItem]:
    """快照的全部条目，按 `(config_id, file_path)` 索引。"""
    if not snapshot_id:
        return {}
    rows = AiDiffSnapshotItem.query.filter_by(snapshot_id=snapshot_id).all()
    return {(row.config_id, row.file_path): row for row in rows}


@dataclass(frozen=True)
class BaselineSnapshot:
    """做差基准的**内存形态**：`{键: 内容身份}` 一张表，够做差就够了。

    刻意不把 ORM 行交出去：做差每轮都要跑，带着一整份 ORM 对象（上千条）只是占内存，
    而调用方要的只有「这个键的身份变了没有」。
    """

    snapshot_id: int
    content_digest: str = ""
    item_count: int = 0
    complete: bool = False
    items: Dict[ItemKey, Identity] = field(default_factory=dict)

    def account(self) -> dict:
        """写进 payload 的那一份账（`baseline` 键）。"""
        return {
            "kind": "snapshot",
            "snapshot_id": self.snapshot_id,
            "content_digest": self.content_digest,
            "item_count": self.item_count,
            "complete": self.complete,
        }


def load_baseline(group_key: str) -> Optional[BaselineSnapshot]:
    """这个组当前的做差基准（最近一份冻结快照）。一份都没有时返回 `None`。"""
    snapshot = latest_sealed_snapshot(group_key)
    if snapshot is None:
        return None
    items = {
        key: row.identity() for key, row in snapshot_items(snapshot.id).items()
    }
    return BaselineSnapshot(
        snapshot_id=snapshot.id,
        content_digest=snapshot.content_digest or "",
        item_count=snapshot.item_count or len(items),
        complete=bool(snapshot.complete),
        items=items,
    )


def _identity_map(source: Any) -> Dict[ItemKey, Identity]:
    """`AiDiffSnapshot` / 快照 id / `BaselineSnapshot` / 现成的 `{键: 身份}` → 身份表。"""
    if source is None:
        return {}
    if isinstance(source, BaselineSnapshot):
        return dict(source.items)
    if isinstance(source, Mapping):
        return {tuple(key): tuple(value) for key, value in source.items()}
    snapshot_id = source if isinstance(source, int) else getattr(source, "id", None)
    return {key: row.identity() for key, row in snapshot_items(snapshot_id).items()}


def diff_snapshots(base: Any, target: Any) -> Tuple[Dict[ItemKey, Identity], ...]:
    """两份快照的差集，返回 `(added, changed, unchanged)` 三张表。

    键都是 `(config_id, file_path)`，值都是**目标那一侧**的内容身份。

    * `added`：目标里有、基准里没有 —— 新改动的文件；
    * `changed`：两边都有，但内容身份不同（新提交 / 换了比较基准 / 换了比较口径）；
    * `unchanged`：内容身份逐字相同 —— **这一批不需要重新分析**。

    `diff_snapshots_full` 另给 `removed`（基准里有、目标里没有）。正常情况下它是空的：
    `weekly_version_diff_cache` 只增不删，只有改时间范围 / 删配置会整批重建，那时
    `removed` 会等于整份基准，而 `added` 等于整份目标 —— 读侧要能把这件事说出来，
    不能让人以为「一夜之间全改了」。
    """
    full = diff_snapshots_full(base, target)
    return full["added"], full["changed"], full["unchanged"]


def diff_snapshots_full(base: Any, target: Any) -> Dict[str, Dict[ItemKey, Identity]]:
    base_items = _identity_map(base)
    target_items = _identity_map(target)
    added: Dict[ItemKey, Identity] = {}
    changed: Dict[ItemKey, Identity] = {}
    unchanged: Dict[ItemKey, Identity] = {}
    for key, identity in target_items.items():
        old = base_items.get(key)
        if old is None:
            added[key] = identity
        elif tuple(old) != tuple(identity):
            changed[key] = identity
        else:
            unchanged[key] = identity
    removed = {key: value for key, value in base_items.items() if key not in target_items}
    return {
        "added": added,
        "changed": changed,
        "unchanged": unchanged,
        "removed": removed,
    }


# ---------------------------------------------------------------------------
#  完整覆盖门槛
# ---------------------------------------------------------------------------


def _count(value: Any) -> Optional[int]:
    """非负整数；读不出来（含 `None`）就是 `None`。**不把「没记录」写成 0**。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def coverage_ratio(ledger: Optional[Mapping[str, Any]]) -> Optional[float]:
    """覆盖账本 → 「取到证据的比例」（**保守口径**：按 `(本批次最新提交, 文件)` 去重）。

    没有采集过逐轮明细时返回 `None`（未知），不是 0 —— 与消耗面板「未上报 ≠ 0」
    同一条口径。
    """
    if not ledger:
        return None
    evidence = ledger.get("evidence_coverage") or {}
    if not evidence.get("collected"):
        return None
    by_pair = evidence.get("by_pair") or {}
    covered = _count(by_pair.get("covered"))
    total = _count(by_pair.get("total"))
    if covered is None or not total:
        return None
    return covered / total


def covers_completely(
    *,
    ledger: Optional[Mapping[str, Any]] = None,
    snapshot_files: Optional[int] = None,
    base_complete: bool = False,
    threshold: Optional[float] = None,
) -> bool:
    """这份快照达没达到「完整覆盖门槛」。

    三条都要成立：

    1. **有覆盖数据**。拿不到（老运行没留逐轮明细、或账本算不出来）直接 `False` ——
       把未知当完整，就是替平台声称「已完整检查」；
    2. **整份快照都在这次的输入里**（`batch_files >= snapshot_files`）。增量运行只装了
       变化的那一部分，无论它自己覆盖得多好，都不足以说「这一份快照看完了」；
       除非**基准快照本身已达门槛**（`base_complete`）—— 那是一条传递口径：上一份已完整
       检查、这次又把基准之后的全部变化都装进了输入，于是这一份也是已完整检查的；
    3. **取到证据的比例 ≥ 阈值**（默认 `DEFAULT_COMPLETE_COVERAGE_RATIO`，可配置）。
    """
    if not ledger:
        return False
    ratio = coverage_ratio(ledger)
    if ratio is None:
        return False
    limit = DEFAULT_COMPLETE_COVERAGE_RATIO if threshold is None else float(threshold)
    if not base_complete:
        counts = ledger.get("counts") or {}
        batch = _count(counts.get("batch_files"))
        whole = _count(snapshot_files)
        if not batch or not whole or batch < whole:
            return False
    return ratio >= limit


# ---------------------------------------------------------------------------
#  补偿集的事实来源：上一条运行的覆盖缺口
# ---------------------------------------------------------------------------


def _payload_mapping(run: Any) -> dict:
    raw = getattr(run, "request_payload", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def whitelist_paths(run: Any) -> List[str]:
    """这条运行**装进输入**的文件路径（`request_payload.delta_files`）。

    单提交模式没有这个键（它的对象就是那一条提交），所以返回空列表 —— 补偿集是
    周版本的概念。
    """
    payload = _payload_mapping(run)
    paths: List[str] = []
    for item in payload.get("delta_files") or ():
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("file_path") or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def evidence_paths(run: Any) -> Optional[set]:
    """这条运行里**取到过证据**的文件路径。

    返回 `None` 表示**没有逐轮明细**（老运行 / 还没跑完）：那时「看过哪些」是未知，
    与「一个都没看」是两件事 —— 合并它们会让补偿集把整份窗口再喂一遍。
    """
    from models.ai_analysis import AiAnalysisTrace

    collected = False
    paths: set = set()
    rows = (
        AiAnalysisTrace.query
        .filter(AiAnalysisTrace.run_id == getattr(run, "id", None))
        .order_by(AiAnalysisTrace.id.asc())
        .all()
    )
    for row in rows:
        if getattr(row, "executed_json", None) is None:
            continue
        collected = True
        for item in decode_evidence(row).get("executed") or ():
            if not isinstance(item, Mapping):
                continue
            if str(item.get("kind") or "") not in FILE_EVIDENCE_KINDS:
                continue
            if item.get("failed") or item.get("empty"):
                continue
            _commit, path, _lines = parse_evidence_label(item.get("label"))
            if path:
                paths.add(path)
    return paths if collected else None


def uncovered_paths(run: Any) -> Optional[set]:
    """这条运行**装了却一条证据都没取到**的文件路径。未知时返回 `None`。

    这就是补偿集的原料（复测文档 AI-P0-02：「base_run 中未覆盖且按风险策略需要补偿的
    项目进入本轮输入」）。
    """
    if run is None:
        return None
    covered = evidence_paths(run)
    if covered is None:
        return None
    return {path for path in whitelist_paths(run) if path not in covered}
