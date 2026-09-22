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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

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
