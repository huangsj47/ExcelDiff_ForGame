#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""这一轮同步该按什么口径采集提交 —— **分支 tip 变了没有**优先，日期水位线兜底。

## 为什么不能只看日期

增量同步原先的起点是

    since_date = max(repository.start_date, commits_log 里最新 commit_time)

交给 `git log --since`。而**提交日期可以回填**：自动导表那类工具提交时带上原始日期，
于是新提交的 `commit_time` 可能早于水位线。比「自己被抓不到」更重的是**它会污染整轮扫描**：
git 按提交日期倒序遍历，第一个遇到的 tip 就比水位线旧，**整个遍历当场停住**。实测
（手法见 `.pytest_tmp/probe_git_range.py`，那是探针不是用例）：

```text
c1(10:00) → c2(12:00) → c3(09:00，回填的 tip)
git log --since=11:00   →  []          # c2 日期明明更新，也一起丢了
git log c1..c3          →  [c3, c2]    # 区间不看日期
```

一次推送里只要 tip 是回填的，**那一整批提交（含日期正常的）全都进不了 `commits_log`**，
而平台不会说「有提交没采到」—— 评审者看到的是一个静悄悄少了改动的版本。

## 改用 tip

tip 是 `git rev-parse` 拿的 sha，**与提交日期无关**：

* `tip` 没变 → 什么都没推，采集结果必然为空（原口径也是这样）；
* `tip` 变了、旧 tip 还在对象库里 → 采集区间 `<旧 tip>..<新 tip>`：回填多少都抓得到；
* 旧 tip 不在（force-push 重写了历史）→ 退回日期水位线，并把「退回了」如实回报。

`commits_log` 的判重键是 `(commit_id, path)`，所以按区间重采一遍不会插重行 —— 这也是
「旧 tip 变了就按区间重来」安全的前提。

## `start_date` 仍然作数

它是用户显式声明的下界（「只看这之后的提交」），所以区间结果里**早于它的提交照样丢掉** ——
与日期口径下的行为一致，不是漏洞。换算走 `beijing_wallclock_to_utc_naive`：库里存的是
naive-UTC 墙钟，而 `start_date` 是用户按北京时间填的，不换算就差 8 小时。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Optional, Sequence, Tuple

from utils.logger import log_print


@dataclass(frozen=True)
class SyncWindow:
    """这一轮的采集口径。`rev_range` 与 `since_date` **互斥**，前者优先。"""

    tip: str = ""
    rev_range: str = ""
    since_date: Optional[datetime] = None
    #: 给人看的一句话（进日志），说明为什么走这一支
    reason: str = ""


def resolve_sync_window(
    git_service: Any,
    repository: Any,
    *,
    latest_known_commit_time: Optional[datetime] = None,
) -> SyncWindow:
    """决定这一轮按区间还是按日期采集。

    只读 git（两个命令：`rev-parse` 与 `cat-file -e`），**不写库、不抛异常** ——
    git 不可用时退回日期口径并如实说明，同步不该因为定序/判定失败而中断。
    """
    start_date = getattr(repository, "start_date", None)
    since_date = start_date
    if latest_known_commit_time and (since_date is None or latest_known_commit_time > since_date):
        since_date = latest_known_commit_time
    fallback = SyncWindow(since_date=since_date, reason="按提交日期水位线")

    try:
        tip = str(local_branch_tip(git_service) or "").strip()
    except Exception as exc:  # noqa: BLE001 —— 读不到 tip 不是错误，退回原口径
        log_print(f"⚠️ 读本地分支 tip 失败，退回日期水位线: {exc}", "SYNC")
        return fallback
    if not tip:
        return SyncWindow(since_date=since_date, reason="读不到本地分支 tip，按提交日期水位线")

    previous_tip = str(getattr(repository, "last_synced_tip", "") or "").strip()
    if not previous_tip:
        return SyncWindow(tip=tip, since_date=since_date, reason="首次同步，按提交日期水位线")
    if previous_tip == tip:
        # 用 `tip..tip` 表达一个确定为空的区间。若退回日期路径，调度器每次检查都会把
        # 水位线之后的几百条历史重新扫描、逐文件再找前一提交；实测两个未变仓库每 2 分钟
        # 白跑约 40 秒。空区间仍走既有的去重/记账路径，但 git 能立即返回。
        return SyncWindow(tip=tip, rev_range=f"{tip}..{tip}", reason="分支 tip 未变（空区间）")

    try:
        known = bool(local_commit_exists(git_service, previous_tip))
    except Exception:  # noqa: BLE001
        known = False
    if not known:
        # force-push 把旧 tip 重写了：区间不成立，退回日期口径（这是能拿到的最好口径）。
        log_print(
            f"⚠️ 上次同步的 tip {previous_tip[:8]} 已不在本地对象库里"
            "（多半是 force-push），本轮退回提交日期水位线",
            "SYNC", force=True,
        )
        return SyncWindow(tip=tip, since_date=since_date, reason="旧 tip 已不存在，按提交日期水位线")

    return SyncWindow(
        tip=tip,
        rev_range=f"{previous_tip}..{tip}",
        reason=f"分支 tip 变了（{previous_tip[:8]} → {tip[:8]}），按提交区间",
    )


def _to_naive_utc(value: Any) -> Optional[datetime]:
    """带时区的值折成 naive-UTC；`commit_time` 是 aware UTC，与库里的口径不同。"""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
#  问 git 的两件事 + 一份采集参数口径 —— 实现都在这里，`GitService` 不加方法
#
#  这样放有两个理由：`git_service.py` 有 1800 行的硬预算（一个测试钉着），而这几件事
#  只在「这一轮采集口径」这一处被用到。前两个只需要 `git_service` 有 `_run_git_command`
#  与 `_git_cmd_success` —— 跨模块取它的私有执行器，与
#  `commit_ordering.order_commits_by_topology` 是同一手法。
# ---------------------------------------------------------------------------


def local_branch_tip(git_service: Any) -> Optional[str]:
    """本地工作副本当前 HEAD 的 sha；读不到返回 `None`。

    **这是「这一轮有没有新东西」唯一可靠的判据**：提交日期可以被回填，tip 不能。
    调用方必须**先**跑过 `clone_or_update_repository()`（它会 `git fetch` + checkout +
    pull），否则拿到的是上一轮的 tip。

    任何失败都返回 `None`（不是抛）：读不到 tip 只该让同步退回日期口径，不该中断。
    """
    try:
        result = git_service._run_git_command(['git', 'rev-parse', 'HEAD'], timeout=30)
    except Exception:  # noqa: BLE001
        return None
    if not git_service._git_cmd_success(result):
        return None
    lines = str(getattr(result, 'stdout', '') or '').strip().splitlines()
    return lines[0].strip() if lines else None


def local_commit_exists(git_service: Any, commit_id: Any) -> bool:
    """这个提交还在不在本地对象库里（`git cat-file -e`）。

    force-push 重写历史之后，上一轮记下的 tip 就查不到了 —— 那时区间口径不成立，
    调用方要退回日期水位线。`cat-file -e` 不需要对象可解析，只要在就行。
    """
    text = str(commit_id or '').strip()
    if not text:
        return False
    try:
        return bool(git_service._git_cmd_success(
            git_service._run_git_command(['git', 'cat-file', '-e', text], timeout=30)
        ))
    except Exception:  # noqa: BLE001
        return False


def iter_commits_args(branch: Any, *, since_date: Any = None, limit: int = 100,
                      rev_range: Any = "") -> tuple:
    """`repo.iter_commits` 的 `(rev, kwargs, 一行说明)`。

    **区间口径只给 `rev`，既不带 `since`，也不带 `max_count`。** 这是整条修法里最容易
    被「顺手补上」的一处：
    `since` 看上去只是个下界，可 `git log --since` 的语义是「按提交日期剪枝并停止遍历」，
    与区间一起用等于把回填的提交**再丢一次**。`max_count` 也不能保留：同步完成后会把
    水位直接推进到新区间终点；若这里截掉第 1001 个及更早的提交，它们之后永远不会再
    进入同步窗口。

    两个采集器（`GitService.get_commits` 与 `ThreadedGitService._get_commits_base_threaded`）
    共用这一份：抄成两份，早晚有一份被改回带 `since` 的样子。
    """
    if rev_range:
        return rev_range, {}, f"按完整提交区间获取：{rev_range}"
    kwargs = {'max_count': limit}
    if since_date:
        kwargs['since'] = since_date
        return branch, kwargs, f"增量同步，从 {since_date} 开始获取最多 {limit} 个提交"
    return branch, kwargs, f"全量同步，获取最多 {limit} 个提交"


def apply_start_date(commits: Sequence[Dict[str, Any]], start_date: Any) -> list:
    """丢掉早于 `start_date` 的提交（用户显式声明的下界，换算成 naive-UTC 再比）。

    只在**区间口径**下调用：日期口径已经把 `start_date` 交给 git 了。
    换算失败（`start_date` 是脏数据）时**原样返回**，不静默丢数据。
    """
    if not start_date:
        return list(commits or ())
    try:
        from utils.timezone_utils import beijing_wallclock_to_utc_naive

        floor = beijing_wallclock_to_utc_naive(start_date)
    except Exception as exc:  # noqa: BLE001 —— 换算不了就不筛，别把提交悄悄丢掉
        log_print(f"⚠️ 起始日期换算失败，长度不筛: {exc}", "SYNC")
        return list(commits or ())
    if floor is None:
        return list(commits or ())

    kept = []
    for commit in commits or ():
        stamp = _to_naive_utc((commit or {}).get("commit_time"))
        if stamp is None or stamp >= floor:
            kept.append(commit)
    dropped = len(commits or ()) - len(kept)
    if dropped:
        log_print(f"🔍 [SYNC] 起始日期 {start_date} 之前的提交丢掉 {dropped} 条", "SYNC")
    return kept


# ---------------------------------------------------------------------------
#  「当前 tip 上还有哪些提交」—— 可达集合与它的指纹
#
#  为什么必须有这一层（P0，2026-09-23 实测）：测试仓库被重建/强推成 3 个提交之后，
#  `repository.last_synced_tip` 已经等于远端 tip，**而配置 3 的 weekly_version_diff_cache
#  仍指向 e54c73df / aa3fd90b / 2971caf7 等不在当前历史里的提交**（三行缓存并集 9 个
#  提交）。`commits_log` 同时留着新旧两段历史，`process_weekly_version_sync` 按
#  仓库 ID + 时间取全部行 —— **只比 tip 是不够的**，必须问出「tip 上到底还有哪些提交」。
#
#  取法用 `git rev-list <tip>`：它是**从 tip 出发沿父边可达**的提交集合，与提交日期
#  无关（日期可回填，见模块抬头）。不做 `--since` 剪枝：那会按提交日期停住遍历，
#  与「可达」是两件事。
# ---------------------------------------------------------------------------

#: 一次 `git rev-list` 最多取多少个提交。它同时决定两件事：
#:
#: * **指纹**覆盖的范围（前缀集合的内容 + 总数）；
#: * **快路径**能直接回答「可不可达」的范围。
#:
#: 超出部分**不猜**：`commit_reachable` 回落 `git merge-base --is-ancestor` 逐条判定。
#: 定 5000 是因为「万级提交的仓库全量 `rev-list` 要几百毫秒到几秒」，而这一判定在
#: 手动分析派发与 worker 每个 tick 都可能跑一次；5000 条覆盖任意一个周窗口绰绰有余。
REACHABLE_PREFIX_LIMIT = 5000


@dataclass(frozen=True)
class ReachableCommits:
    """当前分支 tip 上的可达提交集合与它的指纹。

    `known=False`（`reason` 非空）时**不许当成「一个提交都没有」**：那是「问不到」，
    调用方必须退回原行为（日期口径 / 不判可达），而不是把整份缓存判成非法。
    这与平台别处「未知 ≠ 0」是同一条纪律。
    """

    branch: str = ""
    tip: str = ""
    #: `git rev-list --count <tip>`，可达提交总数（不受前缀上限影响）。
    count: int = 0
    #: 可达集合的指纹。`known` 为假时是空串 —— **空指纹不许与非空指纹相等**
    #: （两个仓库都问不到时不能因此判成「同一份历史」）。
    digest: str = ""
    #: 可达提交的前缀集合（最多 `REACHABLE_PREFIX_LIMIT` 个），用于「在不在里面」的快路径。
    commits: FrozenSet[str] = field(default_factory=frozenset)
    #: 前缀集合是否被上限截断（截断时「不在集合里」**不能**直接判成不可达）。
    truncated: bool = False
    #: 问不到时的原因（给人看）。空串 = 取到了。
    reason: str = ""

    @property
    def known(self) -> bool:
        return bool(self.tip) and not self.reason

    def describe(self) -> str:
        """一行日志用的说明。指纹只给前 12 位 —— 完整值在库里/日志里没人逐字比对。"""
        if not self.known:
            return f"可达集合未知（{self.reason or '未取得'}）"
        tail = "，已按上限截断" if self.truncated else ""
        return (
            f"可达集合 {self.count} 个提交，指纹 {self.digest[:12]}"
            f"（{self.branch or '-'}@{self.tip[:8]}{tail}）"
        )


def reachable_digest(
    branch: str, tip: str, count: int, commits: Sequence[str]
) -> str:
    """可达集合的指纹：**分支 + tip + 总数 + 有序提交前缀**一起哈希。

    为什么不只哈希 tip：`tip` 相同而对象库被重建过（同 sha 不可能，但「同一个分支名
    指向了内容相同的新建历史」是可能的）时，只比 tip 会判成「没变」。带上总数与前缀
    内容之后，任何一处变了指纹就变 —— 「强推同时间戳的新 tip」同样会变（tip 变了）。

    前缀按 `git rev-list` 的输出顺序拼接（**不排序**）：顺序本身就是拓扑信息，
    排序会把它抹掉。
    """
    payload = "\n".join(
        [str(branch or ""), str(tip or ""), str(int(count or 0))]
        + [str(item or "") for item in commits]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def reachable_commits(
    git_service: Any,
    *,
    tip: Optional[str] = None,
    branch: str = "",
    limit: int = REACHABLE_PREFIX_LIMIT,
    timeout: int = 60,
) -> ReachableCommits:
    """从 `tip` 出发**批量**取可达提交集合（`git rev-list`）并算指纹。

    `tip` 为空时自己 `rev-parse HEAD`（内部走 `local_branch_tip`）。调用方必须**先**
    跑过 `clone_or_update_repository()`，否则拿到的是上一轮的 tip。

    **任何失败都返回 `known=False` 的对象（不抛）**：读不到可达集合只该让调用方退回
    原口径并如实说明，不该中断同步。这条与 `resolve_sync_window` 是同一条纪律。
    """
    if not tip:
        try:
            tip = local_branch_tip(git_service)
        except Exception as exc:  # noqa: BLE001
            return ReachableCommits(reason=f"读本地分支 tip 失败: {exc}")
    text = str(tip or "").strip()
    if not text:
        return ReachableCommits(reason="读不到本地分支 tip")

    try:
        listed = git_service._run_git_command(
            ['git', 'rev-list', f'--max-count={int(limit)}', text], timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001
        return ReachableCommits(branch=branch, tip=text, reason=f"rev-list 执行失败: {exc}")
    if not git_service._git_cmd_success(listed):
        return ReachableCommits(
            branch=branch, tip=text,
            reason=f"rev-list 未成功（提交 {text[:8]} 可能不在本地对象库里）",
        )
    commits = [
        line.strip()
        for line in str(getattr(listed, 'stdout', '') or '').splitlines()
        if line.strip()
    ]
    if not commits:
        # tip 存在却 rev-list 为空：几乎只可能是命令被截断/失败，**不当作「零提交」**。
        return ReachableCommits(branch=branch, tip=text, reason="rev-list 返回空结果")

    # 总数单独问一次（`--count`），因为它不受前缀上限影响 —— 「截断了」这一事实本身
    # 必须能从 `count > len(commits)` 看出来。
    count = len(commits)
    try:
        counted = git_service._run_git_command(
            ['git', 'rev-list', '--count', text], timeout=timeout
        )
        if git_service._git_cmd_success(counted):
            lines = str(getattr(counted, 'stdout', '') or '').strip().splitlines()
            if lines:
                count = int(lines[0].strip() or 0)
    except Exception:  # noqa: BLE001 —— 问不到总数就退回前缀条数，不影响可达判定
        count = len(commits)

    truncated = count > len(commits)
    return ReachableCommits(
        branch=str(branch or ""),
        tip=text,
        count=count,
        digest=reachable_digest(branch, text, count, commits),
        commits=frozenset(commits),
        truncated=truncated,
    )


def commit_reachable(
    git_service: Any, reachable: ReachableCommits, commit_id: Any
) -> Optional[bool]:
    """这个提交还在不在**当前 tip 的可达集合**里。`None` = 判不出来（未知）。

    快路径先查前缀集合；不在里面且**没被截断**时可以直接答 False（前缀就是全集）。
    被截断时回落 `git merge-base --is-ancestor <提交> <tip>`：退出码 0 = 是祖先，
    1 = 不是，其余（128：对象不存在/命令失败）= 未知。

    **「不在集合里」与「判不出来」必须分开**：前者是强推/回退的证据，后者只是没问到。
    把后者当 False 会让一个临时读不到对象库的仓库被判成历史被重写，进而把好缓存删掉。
    """
    text = str(commit_id or "").strip()
    if not text or not reachable.known:
        return None
    if text in reachable.commits:
        return True
    if not reachable.truncated:
        return False
    try:
        result = git_service._run_git_command(
            ['git', 'merge-base', '--is-ancestor', text, reachable.tip], timeout=60
        )
    except Exception:  # noqa: BLE001
        return None
    code = getattr(result, 'returncode', None)
    if code == 0:
        return True
    if code == 1:
        return False
    return None


def partition_reachable(
    git_service: Any, reachable: ReachableCommits, commit_ids: Sequence[Any]
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """把一批提交号分成 `(不可达, 判不出来)` 两堆，都去重且保持首次出现的顺序。

    调用方拿两堆做**方向相反**的事：不可达的删（历史被重写了），判不出来的只记日志
    （见 `commit_reachable` 的「不在集合里 vs 判不出来」）。
    """
    unreachable: list = []
    unknown: list = []
    for raw in commit_ids or ():
        text = str(raw or "").strip()
        if not text:
            continue
        verdict = commit_reachable(git_service, reachable, text)
        if verdict is True:
            continue
        target = unknown if verdict is None else unreachable
        if text not in target:
            target.append(text)
    return tuple(unreachable), tuple(unknown)
