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
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

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
#: 内容身份：`(base_commit_id, latest_commit_id, diff_version, commit_count)`。
#: 与 `AiDiffSnapshotItem.identity()` 逐项对应 —— 改那边必须改这里（`commit_count`
#: 是 2026-09 补的第四项，别名当时漏了，而漏掉一个类型注解不会有任何报错）。
Identity = Tuple[Optional[str], Optional[str], Optional[str], int]


def identity_latest(identity: Optional[Sequence[Any]]) -> Optional[str]:
    """身份元组里的 `latest_commit_id`。

    下标**只写在这一处**：身份的项数变过一次（加 `commit_count` 时是往末尾加的），
    调用点直接写 `identity[1]` 的话，将来在中间插一项就会静默错位。
    """
    if not identity or len(identity) < 2:
        return None
    return identity[1]

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
    """`(file_path, base, latest, diff_version, commit_count)` 的内容指纹。

    **与 `scope_sampling.weekly_snapshot_digest` 逐字相同**（见模块抬头）。两边的输入
    来自同一条查询、同一个排序、同一个分隔符；`None` 一律折成空串，`commit_count`
    两边都折成 `int(x or 0)` 再转字符串（不然 NULL 与 0 会写出两个不同的指纹）。
    """
    parts = sorted(
        "%s|%s|%s|%s|%s|%s"
        % (
            getattr(row, "file_path", None) or "",
            getattr(row, "base_commit_id", None) or "",
            getattr(row, "latest_commit_id", None) or "",
            getattr(row, "diff_version", None) or "",
            str(int(getattr(row, "commit_count", 0) or 0)),
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
        # 去重键与唯一索引**同一份**（`uq_ai_snapshot_item_key`，含仓库）。真出现重复
        # 也不能让 INSERT 撞唯一索引 —— 那时整条 run 都建不出来，代价远大于少一条。
        key = (row.repository_id, row.config_id, row.file_path)
        if key in seen:
            # `file_path` 在 `(仓库, config_id, file_path)` 下唯一是这张表的前提。
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
                commit_count=row.commit_count,
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


def snapshot_commit_ids(snapshot_id: Optional[int]) -> FrozenSet[str]:
    """一份快照的条目引用到的**全部提交号**（`latest` 与 `base` 都算）。

    用途是**来源核对**（P0 工作包 A）：某个仓库的历史被强推/重建之后，快照里记着的
    提交可能已经不在当前 tip 上。`base_commit_id` 也要算 —— 条目身份是
    `(base, latest, diff_version, commit_count)`，base 指向一个不存在的提交时，
    「这个文件变了没有」这个比较的两端之一已经不在了。
    """
    ids: set = set()
    for row in snapshot_items(snapshot_id).values():
        latest = getattr(row, "latest_commit_id", None)
        base = getattr(row, "base_commit_id", None)
        for value in (latest, base):
            text = str(value or "").strip()
            if text:
                ids.add(text)
    return frozenset(ids)


def entries_outside_history(
    snapshot: Any, reachable: Iterable[str]
) -> Tuple[ItemKey, ...]:
    """这份快照里**指向当前历史之外**的条目键（`(config_id, file_path)`）。

    `reachable` 是当前 tip 上可达的提交集合（来自
    `services/repository_sync_window.reachable_commits`）。**空集合时返回空元组** ——
    「问不到可达集合」不是「全都不在历史上」，把它当后者会让每一份历史快照都被判成
    过期。与平台别处「未知 ≠ 0」是同一条纪律。

    这是**只读**核对：本函数不删任何东西。历史运行与其快照保留为冻结输入（见
    `services/weekly_window_reconcile.py` 的模块抬头），核对只回答「它还准不准」，
    不动它 —— 冻结输入被就地改写等于伪造审计线索。
    """
    known = {str(item or "").strip() for item in (reachable or ()) if str(item or "").strip()}
    if not known:
        return ()
    outside: List[ItemKey] = []
    for key, row in snapshot_items(
        snapshot if isinstance(snapshot, int) else getattr(snapshot, "id", None)
    ).items():
        latest = str(getattr(row, "latest_commit_id", None) or "").strip()
        if latest and latest not in known:
            outside.append(key)
    return tuple(outside)


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


# ---------------------------------------------------------------------------
#  未读账：没取到证据的那些文件**与原因**（P1b）
# ---------------------------------------------------------------------------
#
# `uncovered_paths` 只说得出「哪些没读到」，说不出「为什么没读到」。而下一轮要回答的
# 恰恰是后者：**没索取**的要催，**被额度拒**的要给额度，**读不到**的要换个坐标或如实
# 写成信息缺口 —— 三件事的处置完全不同，混在一个集合里只能一律重排一次。
#
# 这份账落在 `run.request_payload["unread"]` 上（**运行结束后追加的派生账**，不是对
# 冻结输入的改写：`delta_files` 那些键一个字节都不动）。它同时是**补偿游标**：连着几轮
# 没读到的文件，下一轮补偿时排在前面（此前每轮都从零重算，等于没有记忆）。

UNREAD_NOT_REQUESTED = "not_requested"
UNREAD_REFUSED = "refused_by_budget"
UNREAD_UNREADABLE = "unreadable"
UNREAD_NO_RESULT = "no_result"

#: 原因 → 给报告与面板读的中文说法。**一份**，两处各写一遍必然对不上。
UNREAD_REASON_LABELS = {
    UNREAD_NOT_REQUESTED: "模型一次都没索取过（覆盖缺口，不是取数失败）",
    UNREAD_REFUSED: "索取过，被额度拒（提高「单个 agent 可索取次数」可解）",
    UNREAD_UNREADABLE: "索取过但读不到（路径或仓库对不上 / 平台读不了这一份）",
    UNREAD_NO_RESULT: "索取过，跑到结束都没有结果落下来",
}

#: 「被额度拒」在逐轮明细里的那一句开头（`context_tools.execute` 的拒绝分支写的）。
#: **判据落在文案上**，所以有一条测试会扫发出方源码核对它还写得出来 ——
#: 与 `trace_evidence.FAILURE_NOTICE_PREFIXES` 是同一套做法（改那句话就要改这里）。
REFUSED_REASON_PREFIX = "超出本次工具请求总预算"

#: `request_payload["unread"]["items"]` 最多留几条。线上的窗口可能有上千个没读到的文件
#: （实测一次 1009 里 967 个），整份塞进 payload 会让每一次读这条 run 的接口都背上它；
#: 而**计数照旧是全量的**，截断的条数也如实写出来。
UNREAD_MAX_ITEMS = 120


def _trace_rows(run: Any) -> Tuple[bool, List[Any]]:
    """`(有没有逐轮明细, 逐轮明细行)`。没有明细 = 未知（与 `evidence_paths` 同一条纪律）。"""
    from models.ai_analysis import AiAnalysisTrace

    rows = (
        AiAnalysisTrace.query
        .filter(AiAnalysisTrace.run_id == getattr(run, "id", None))
        .order_by(AiAnalysisTrace.id.asc())
        .all()
    )
    collected = any(getattr(row, "executed_json", None) is not None for row in rows)
    return collected, list(rows)


def _input_files(run: Any) -> List[dict]:
    """本次输入的文件 `[{path, repository_id, commit}]`（顺序即 payload 里的顺序）。"""
    payload = _payload_mapping(run)
    out: List[dict] = []
    seen: set = set()
    rows = list(payload.get("delta_files") or []) + list(
        payload.get("compensation_files") or []
    )
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("file_path") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append({
            "path": path,
            "repository_id": item.get("repository_id"),
            "commit": str(item.get("latest_commit_id") or "").strip(),
        })
    return out


def _asked_paths(rows: Sequence[Any]) -> Tuple[set, set]:
    """逐轮明细 → `(索取过的路径, 被额度拒过的路径)`。

    两个来源都要读：模型自己写的请求在 `requests_json`，被拒的那些进 `dropped_json`
    （拒绝**不执行**，所以它在 requests 里可能看得到、也可能因为那一轮没记而看不到）。
    """
    asked: set = set()
    refused: set = set()
    for row in rows:
        item = decode_evidence(row)
        for request in item.get("requests") or ():
            if not isinstance(request, Mapping):
                continue
            if str(request.get("type") or "") not in FILE_EVIDENCE_KINDS:
                continue
            path = str(request.get("path") or "").strip()
            if path:
                asked.add(path)
        for dropped in item.get("dropped") or ():
            if not isinstance(dropped, Mapping):
                continue
            if not str(dropped.get("reason") or "").startswith(REFUSED_REASON_PREFIX):
                continue
            _commit, path, _lines = parse_evidence_label(dropped.get("detail"))
            if path:
                refused.add(path)
    return asked, refused


def unread_files(run: Any) -> Optional[List[dict]]:
    """本次输入里**一条证据都没取到**的文件，逐条带上原因。未知时返回 `None`。

    「未知」的判据与 `evidence_paths` 逐字相同（没有逐轮明细）：那时不许把整份输入
    当成「没读到」—— 那是把未知当结论。
    """
    if run is None:
        return None
    covered = evidence_paths(run)
    if covered is None:
        return None
    collected, rows = _trace_rows(run)
    if not collected:
        return None
    asked, refused = _asked_paths(rows)
    unreadable = {
        path
        for path in (
            parse_evidence_label(item.get("label"))[1]
            for row in rows
            for item in (decode_evidence(row).get("executed") or [])
            if isinstance(item, Mapping)
            and str(item.get("kind") or "") in FILE_EVIDENCE_KINDS
            and (item.get("failed") or item.get("empty"))
        )
        if path
    }
    out: List[dict] = []
    for entry in _input_files(run):
        path = entry["path"]
        if path in covered:
            continue
        if path in refused:
            reason = UNREAD_REFUSED
        elif path in unreadable:
            reason = UNREAD_UNREADABLE
        elif path in asked:
            reason = UNREAD_NO_RESULT
        else:
            reason = UNREAD_NOT_REQUESTED
        out.append({**entry, "reason": reason})
    return out


def unread_streaks(run: Any) -> dict:
    """上一条运行里那些没读到的文件 → **它已经连着几轮没读到了**（1 = 上一轮第一次）。

    给补偿排序用（`scope_sampling._compensation_entries`）。老运行没有这份账 ⇒ 空表 ⇒
    排序退回原来的口径（行为与从前逐字相同）。
    """
    payload = _payload_mapping(run) if run is not None else {}
    section = payload.get("unread")
    if not isinstance(section, Mapping):
        return {}
    out: dict = {}
    for item in section.get("items") or ():
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        try:
            streak = int(item.get("streak") or 0)
        except (TypeError, ValueError):
            streak = 0
        out[path] = max(1, streak)
    return out


def record_unread(run: Any) -> Optional[dict]:
    """把未读账写回 `run.request_payload["unread"]`（**调用方负责提交**）。未知时不动它。

    游标在这里延续：上一轮也没读到的那些，`streak` 加一 —— 「连着三轮没读到」与
    「这一轮才漏」在下一轮的补偿排序里不是一回事。
    """
    files = unread_files(run)
    if files is None:
        return None
    payload = _payload_mapping(run)
    previous = unread_streaks(_run_by_id(payload.get("base_run_id")))
    items: List[dict] = []
    counts: dict = {}
    for item in files:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
        items.append({**item, "streak": previous.get(item["path"], 0) + 1})
    section = {
        "total": len(items),
        "counts": counts,
        "labels": {key: UNREAD_REASON_LABELS[key] for key in counts},
        "shown": min(len(items), UNREAD_MAX_ITEMS),
        "items": items[:UNREAD_MAX_ITEMS],
    }
    payload["unread"] = section
    run.request_payload = json.dumps(payload, ensure_ascii=False)
    return section


def _run_by_id(run_id: Any) -> Any:
    """按 id 取上一条运行（拿它的未读游标）。取不到就是 `None` —— 不猜。"""
    if not run_id:
        return None
    try:
        from models.ai_analysis import AiAnalysisRun

        return db.session.get(AiAnalysisRun, int(run_id))
    except (TypeError, ValueError):  # pragma: no cover —— 坏 id 只该让游标断开
        return None
