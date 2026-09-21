#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weekly analysis state tracking for incremental runs.
"""

from datetime import datetime, timezone
from .. import db
from ..big_text import BigText


class AiWeeklyAnalysisState(db.Model):
    __tablename__ = "ai_weekly_analysis_state"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    group_key = db.Column(db.String(200), nullable=False, unique=True)

    base_name = db.Column(db.String(120))
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)

    last_analyzed_at = db.Column(db.DateTime)
    last_analysis_run_id = db.Column(db.Integer)
    last_scope = db.Column(db.String(20))
    last_summary = db.Column(BigText)
    last_triggered_at = db.Column(db.DateTime)
    # 「上一次分析的是哪一份快照」的内容指纹。与 `last_analyzed_at` 的区别是**两者的
    # 判据不同**：时间水位线只在**跑完整了**的 run 上推进（降级的 run 不推进，否则模型
    # 没真读到的变更会被标成「已看过」），而这个指纹**每次跑完都写**（降级也写）——
    # 同一份输入再分析一遍不会有不同的结果，只会白花一次钱。
    # 计算与用途见 `services/ai/scope_sampling.py::weekly_snapshot_digest`。
    last_snapshot_digest = db.Column(db.String(64))

    # ------------------------------------------------------------------
    # 两个基线指针（2026-09-21 补，复测文档 AI-P0-02）
    #
    # 根因是 `last_analyzed_at` **一个时间水位表示了三件事**：
    #   ① 哪些缓存行算「新变化」（`updated_at > last_analyzed_at`）；
    #   ② 模型有没有完整读过上一份快照；
    #   ③ 下一次是首次全量还是增量。
    #
    # 降级运行不推进它是**对的**（94% 的文件没取到证据，标成「已读」是撒谎），
    # 但代价是 ③ 跟着一起废掉：手工路径看到 `last_analyzed_at is None`，于是每一次
    # 都按 `first_run` 做约 1000 个文件的全量分析 —— 实测 Run 20 相对 Run 15 只有
    # 45 个路径身份变化，却把 1009 个文件全放进了白名单。
    #
    # 拆成两个指针之后，三件事各归各的：
    # ------------------------------------------------------------------

    # 「最近一次**产出了可复用结论**的运行」（succeeded 或 degraded 且有结构化结论）。
    # 这就是**结论基线**：下一轮增量继承它的结论，即使那次是降级交付。
    # 注意它**不代表完整覆盖** —— 降级结论可以当基线，但不许说成「已完整检查」。
    last_concluded_run_id = db.Column(db.Integer)

    # 「最近一次达到**平台完整覆盖门槛**的快照」（见 `services/ai/snapshot_store.py`）。
    # 只有它才能支撑「已完整检查」的声明，也是判断「这次算不算首次」的依据。
    # 降级运行会推进 `last_concluded_run_id` 但**不推进它**。
    #
    # 与 `last_analyzed_at` 的分工：那个时间水位线保留原语义（只在 succeeded 时推进），
    # 供既有的缓存行增量筛选继续用；这两个指针是**按快照做差**那条新路径的输入。
    last_complete_snapshot_id = db.Column(db.Integer)

    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project = db.relationship("Project", backref="ai_weekly_states")

    def __repr__(self):
        return f"<AiWeeklyAnalysisState {self.group_key}>"
