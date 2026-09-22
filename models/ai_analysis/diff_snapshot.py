#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""不可变的 Diff 快照：增量分析的基准与目标。

## 为什么要有它（复测文档 AI-P0-02）

改动之前没有「快照」这个实体。最接近的两样东西都不够用：

* `ai_weekly_analysis_state.last_snapshot_digest` —— 一个 64 字符的哈希标量。
  它能回答「输入变了没有」，**不能回答「变了哪几个」**，所以它只能用来跳过重复分析，
  没法当作差的基准。历史快照也无法重建。
* `weekly_version_diff_cache.updated_at` —— 缓存行的**写库时刻**。增量筛选拿
  `updated_at > last_analyzed_at` 做差，而那既不是「内容变了」也不是「谁变了」：
  它是「什么时候写的」。同步任务每 2~3 分钟跑一遍，任何一次重算都会顶高它。

再加上 `last_analyzed_at` 同时被当成三件事用（见
`models/ai_analysis/weekly_state.py` 上那段），降级运行又刻意不推进它 ——
于是手工路径每次都看到 `None`，每次都按「首次全量」把约 1000 个文件全放进白名单。
实测 Run 20 相对 Run 15 只有 **45 个路径身份变化**，却分析了 1009 个文件。

## 条目身份 = `(config_id, file_path)`

`weekly_version_diff_cache` 天然就是一张 manifest：`file_path` 在
`(config_id, file_path)` 下唯一（`weekly_version_logic.generate_weekly_merged_diff`
就是按这一对定位行的），`repository_id` 由 `config_id` 决定。

**做差的判据是内容身份，不是时间**：

```text
条目内容身份 = (base_commit_id, latest_commit_id, diff_version, commit_count)
本次要分析 = target 中存在、而 base 中没有或身份不同的条目
```

`diff_version` 必须进身份：比较口径变了（见 `docs/代码架构说明.md` 第 3.4 节）
就**必须**重新分析，哪怕 commit 一个都没动。

`commit_count` 也必须进身份 —— 它是**窗口内碰过这个文件的提交条数**。提交是按
`commit_time` 定序挑 base/latest 的，而自动导表那类工具回填的提交可能**日期早于已有
提交、推送却在之后**：它落在窗口中间，base/latest 一个都不动，可合并 diff（窗口内该文件
的提交按序合起来）已经变了、缓存行也重写了。少了这一项，判据说「没变」，而基准每轮都
往前推 —— 这一处改动**永远**补不回来。`weekly_file_sync.weekly_cache_is_unchanged`
判「要不要重写这一行」时早就把它算作内容变化了，两个判据必须是同一个口径。

## 快照是**只增不删**的

`weekly_version_diff_cache` 只在这两处会整批删行：改周版本时间范围
（`weekly_version_logic.py:500`）、删除周版本配置（`:521`）。同步本身只增/改，不删。
所以正常情况下 `target − base` 不会出现「条目消失」；只有整批重建时才会，
那时差集等于「全部新增」——这是**正确**的（基准整体没了，只能重来），
但读侧要说得出这件事，不能让人以为「一夜之间全改了」。

## 为什么不把 manifest 塞进一个 JSON 大文本

`run.request_payload` 那种做法（整份白名单存 BigText）没有按条目查询的能力：
每次做差都要把两份上千条的 JSON 反序列化再比。而这张表要服务的是
**每一次**分析起跑前的做差。独立表 + 复刻 `weekly_version_diff_cache` 的索引形态，
做差就是一条 SQL。
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import db

#: 快照刚建好、条目正在往里写。此状态下**不允许**被选为做差基准 ——
#: 半份快照做出来的差集是错的，而那正是同步闸门要防的那件事。
STATUS_OPEN = "open"
#: 条目写完、冻结。**只有冻结的快照才能当基准**，也才代表「当时的那一刻」。
STATUS_SEALED = "sealed"

SNAPSHOT_STATUSES = (STATUS_OPEN, STATUS_SEALED)

#: 条目在两份快照之间的三种关系（`snapshot_store.diff_snapshots` 的返回值）。
CHANGE_ADDED = "added"
CHANGE_CHANGED = "changed"
CHANGE_UNCHANGED = "unchanged"


class AiDiffSnapshot(db.Model):
    """某一时刻某个周版本组的完整文件清单（不可变）。"""

    __tablename__ = "ai_diff_snapshot"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    # 周版本的分组键（`ai_analysis_service.build_weekly_payload` 的 `group.key`）。
    # 快照按**组**取，不按单个 config：变更清单来自这一批全部仓库的缓存行。
    group_key = db.Column(db.String(200), nullable=False)

    # 与 `scope_sampling.weekly_snapshot_digest` **逐字相同**的算法（那是个纯函数，
    # 这里只是把结果落库）。**算法改过一次**（2026-09，加进 `commit_count`）：改它
    # 有代价 —— `AiAnalysisRun.active_key` 的输入指纹里含这个值，换算法会让所有
    # 「同目标同输入」的历史活动运行突然不再匹配，同一次输入被放行第二次。那次是有意
    # 承担的：旧算法把「回填的旧日期提交改变了窗口内容」也算成「同一份输入」（见模块
    # docstring），而漏掉的代价比多跑一次大得多。再动它请照这个标准掂量。
    content_digest = db.Column(db.String(64), nullable=False)

    # 条目数（做差时先比它：数目相等且 digest 相等就是同一份，不必捞条目）。
    item_count = db.Column(db.Integer, nullable=False, default=0)

    # 这一份快照有没有达到「完整覆盖门槛」（决定它能否支撑「已完整检查」的声明，
    # 以及能否成为 `last_complete_snapshot_id`）。**与 status 不是一回事**：
    # 冻结了只说明「这份清单是完整的」，覆盖门槛说的是「模型真的看完了」。
    # 门槛判据见 `services/ai/snapshot_store.py::covers_completely`。
    complete = db.Column(db.Boolean, nullable=False, default=False)

    status = db.Column(db.String(20), nullable=False, default=STATUS_SEALED)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    sealed_at = db.Column(db.DateTime, nullable=True)

    project = db.relationship("Project", backref="ai_diff_snapshots")
    items = db.relationship(
        "AiDiffSnapshotItem",
        backref="snapshot",
        cascade="all, delete-orphan",
        passive_deletes=False,
    )

    __table_args__ = (
        # 取某个组的最近一份（`latest_complete_snapshot` 与做差基准的挑选）。
        db.Index("idx_ai_snapshot_group_created", "group_key", "created_at"),
        # 按指纹去重：同一份输入不重复建快照（同步每 2 分钟跑，但不该每 2 分钟建一份）。
        db.Index("idx_ai_snapshot_group_digest", "group_key", "content_digest"),
    )

    @property
    def is_sealed(self) -> bool:
        return self.status == STATUS_SEALED

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.id,
            "group_key": self.group_key,
            "content_digest": self.content_digest,
            "item_count": self.item_count,
            "complete": bool(self.complete),
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "sealed_at": self.sealed_at.isoformat() if self.sealed_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<AiDiffSnapshot {self.id} {self.group_key[:24]} "
            f"{self.item_count}项 {self.status}>"
        )


class AiDiffSnapshotItem(db.Model):
    """快照里的一条：某个 `(config_id, file_path)` 在那一刻的内容身份。"""

    __tablename__ = "ai_diff_snapshot_item"

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(
        db.Integer,
        db.ForeignKey("ai_diff_snapshot.id", ondelete="CASCADE"),
        nullable=False,
    )

    config_id = db.Column(db.Integer, nullable=False)
    repository_id = db.Column(db.Integer, nullable=True)
    file_path = db.Column(db.String(1000), nullable=False)

    # 内容身份四元组。**做差比的就是这四个**（见模块 docstring）：
    # * `latest_commit_id` 变了 = 这个文件有了新提交；
    # * `base_commit_id` 变了 = 比较基准换了（同样要重看）；
    # * `diff_version` 变了 = 比较口径变了（`DIFF_LOGIC_VERSION`），必须重看；
    # * `commit_count` 变了 = 窗口内碰过它的提交条数变了（回填的旧日期提交也算，
    #   见模块 docstring 上那段）。
    base_commit_id = db.Column(db.String(100), nullable=True)
    latest_commit_id = db.Column(db.String(100), nullable=True)
    diff_version = db.Column(db.String(40), nullable=True)
    commit_count = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        # 条目在快照内唯一 —— 做差按 `(config_id, file_path)` 取，重复条目会让
        # 差集出现「同一个文件两次」。
        db.Index(
            "uq_ai_snapshot_item_key",
            "snapshot_id", "config_id", "file_path",
            unique=True,
        ),
        # 做差时按 `(snapshot_id, config_id)` 批量捞（一个快照可能跨多个 config）。
        db.Index("idx_ai_snapshot_item_config", "snapshot_id", "config_id"),
    )

    def identity(self) -> tuple:
        """内容身份。两份快照里同一个键的这个元组不同 = 这个文件要重看。

        `commit_count` 折成 `int(x or 0)`：老库里的行在这一列上是 NULL，而做差另一侧
        读的是缓存行上的整数 —— 不折就会出现 `None != 1`，把每一行都判成变了。
        """
        return (self.base_commit_id, self.latest_commit_id, self.diff_version,
                int(self.commit_count or 0))

    def to_dict(self) -> dict:
        return {
            "config_id": self.config_id,
            "repository_id": self.repository_id,
            "file_path": self.file_path,
            "base_commit_id": self.base_commit_id or "",
            "latest_commit_id": self.latest_commit_id or "",
            "diff_version": self.diff_version or "",
            "commit_count": int(self.commit_count or 0),
        }

    def __repr__(self) -> str:
        return f"<AiDiffSnapshotItem {self.snapshot_id}:{self.file_path}>"
