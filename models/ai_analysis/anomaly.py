#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 分析产出的单条异常，以及它的人工处置状态。

## 为什么要单独一张表

原来异常只是 `ai_analysis_run.response_payload` 里的一段 JSON。那样的东西**没法被处置**：
「已确认 / 已忽略」没有地方存，跟进的人只能对着一段文本自己记。单独一行一条之后，
处置状态、处置人、处置时间都能落下，而且「已忽略的条目在后续重跑中不再提示」才有依据。

## 为什么 disposition 存英文码

存 `pending` / `confirmed` / `ignored` 而不是中文：这些值会进筛选条件与 URL 查询参数，
ASCII 码值在任何后端、任何编码设置下都不会出问题，中文标签只出现在界面上
（`DISPOSITION_LABELS`）。
"""

import json
from datetime import datetime, timezone

from .. import db
from ..big_text import BigText

# 处置状态。**改这里就要同步改 `DISPOSITION_LABELS`**（有一条测试盯着这件事）。
DISPOSITIONS = ("pending", "confirmed", "ignored")
DISPOSITION_LABELS = {
    "pending": "待确认",
    "confirmed": "已确认",
    "ignored": "已忽略",
}
DEFAULT_DISPOSITION = "pending"


class AiAnalysisAnomaly(db.Model):
    __tablename__ = "ai_analysis_anomaly"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(
        db.Integer, db.ForeignKey("ai_analysis_run.id"), nullable=False, index=True
    )
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False, index=True)

    # 异常的身份指纹（见 services/ai/rules.py::anomaly_fingerprint）。
    # 用于把上一轮的处置继承到这一轮：模型每次重跑措辞都会略有不同，靠标题原文匹配不上。
    fingerprint = db.Column(db.String(32), index=True)

    title = db.Column(db.String(500), nullable=False)
    category = db.Column(db.String(50))
    severity = db.Column(db.String(20))
    confidence = db.Column(db.String(20))
    # 证据留存为 JSON 数组文本（与 AiAnalysisRun.request_payload 等保持一致的存法）。
    evidence = db.Column(BigText)
    commit_ref = db.Column(db.String(100))
    file_path = db.Column(db.String(500))
    impact = db.Column(BigText)
    suggestion = db.Column(BigText)
    # 这条结论的**原子断言**与各自的裁决结果（P0-01，JSON 数组文本，与 `evidence` 同一存法）。
    #
    # 为什么必须落库：`claims` 不是运行期的中间产物，而是「这条结论凭什么算核实过了」的
    # 答案 —— 下一轮的基线、冻结结论的重放、导出文档、异常面板都要读它。不存的话，
    # 「哪一条断言没被证实」只活在这一次运行的报告文字里，任何一次回看都只能重读那段话。
    claims = db.Column(BigText)

    disposition = db.Column(db.String(20), default=DEFAULT_DISPOSITION, index=True)
    disposition_by = db.Column(db.String(100))
    disposition_at = db.Column(db.DateTime)
    disposition_note = db.Column(BigText)

    # 这条结论**上一轮被人判定过「不用再管」**、而这一轮又重新出现了（相关文件又变了，
    # 或者模型又报了一遍）。它不是「当前处置」，而是**来历**：处置状态在 `disposition`
    # 里，这里说的是「它曾经被人工忽略过」—— 复活的条目长得和一条全新发现一模一样时，
    # 重新看到它的人就不知道上一轮已经有人判过一次了。
    #
    # 落库而不是只留在 payload 里：面板、导出、下一轮基线读的都是这张表，只写在一次
    # 运行的 `result` 字典里的话，刷新一次页面就消失（而且旧行为正是这样）。
    # 老行（加列之前）是 NULL，读成 False —— 那正是它们该有的语义：没有这个标记。
    suppressed_previous = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    run = db.relationship("AiAnalysisRun", backref="anomalies")

    # 注意这里**没有**给 (run_id, fingerprint) 加唯一约束。
    # 去重是 services/ai/rules.py 的职责，且它有自己的记账（「哪条被合并了」）。再加一道
    # 唯一约束并不会让去重更正确，却会让「去重逻辑回归」从「报告里多一条重复项」升级成
    # 「整个分析在写库时 IntegrityError 失败」—— 用一个更严重的故障去兜一个更轻的问题。
    __table_args__ = (db.Index("idx_ai_anomaly_run_disposition", "run_id", "disposition"),)

    def to_dict(self):
        """这一行的对外形态（读接口直接回它）。

        `evidence` 出成**数组**而不是库里那个 JSON 文本：它在库里是 `_json_dumps`
        存下的字符串，原样给界面，界面就得自己 `JSON.parse` —— 而解析失败时它只能
        要么丢掉证据（静默），要么显示一段带转义符的原文。归一到这一层，
        两处读法就只有一份口径。

        解析失败回空数组而不是抛：这是读路径，一条证据存坏了不该让整次读取 500
        —— 那一行其余的字段（标题、严重度、文件）都还是好的，而它们才是用户要的。

        ## 为什么中文名与时间显示在这里算（而不是界面自己映射）

        与本仓库既有的口径一致（见 `conclusion_view` 的 `scope_label` / `risk_label`）：
        **中文口径在服务端算，界面不自己映射**。两处各写一份映射表，改一处必然漏另一处，
        而漏掉的那一处显示的是英文码值 —— 它看起来像正经标识符，不会有人发现。

        时间同理，而且更硬：`disposition_at` 是**库里的 naive-UTC**，
        界面自己 `new Date()` 会按浏览器时区渲染（本机 UTC+8 时与旁边那张
        `created_at_display` 的北京时间差 8 小时），而仓库另有测试明令禁止前端做时区换算
        （见 `tests/test_ai_analysis_time_display.py`）。所以给一个算好的
        `disposition_at_display`，界面直接印。
        """
        try:
            evidence = json.loads(self.evidence) if self.evidence else []
        except (TypeError, ValueError):
            evidence = []
        if not isinstance(evidence, list):
            evidence = []
        # 断言清单与证据同一套读法（存的是 JSON 文本，出成数组；坏掉回空的 `[]`，
        # 不让一条存坏的字段把整次读取打成 500）。
        try:
            claims = json.loads(self.claims) if self.claims else []
        except (TypeError, ValueError):
            claims = []
        if not isinstance(claims, list):
            claims = []
        from services.ai.report_document import (
            CATEGORY_GROUP_LABELS,
            beijing_display,
            category_group,
            category_label,
            confidence_label,
            severity_label,
        )

        # 分组也要一并给出，**不让界面自己按 category 猜**：哪些 id 算「配表」是
        # `skill_contract.TABLE_DIMENSION_IDS` 的事实，界面上再抄一份的话，加/改维度时
        # 面板会把配表结论归进「代码·流程」里 —— 而两种分组看起来都正常，不会有人发现。
        group = category_group(self.category)
        return {
            "id": self.id,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "title": self.title,
            "category": self.category,
            # category 的中文名（`配置 ID`）。认不出的码值原样返回 —— 与
            # `severity_label` 同一条兜底：不猜、也不显示成空。分组标题已经把
            # 「未归类」说出来了，这里不再写「未归类（xxx）」那样的一整句。
            "category_label": category_label(self.category),
            # 面板按它分组（`table` / `code_process` / `unclassified`）+ 那一组的中文标题。
            # 放在同一行里：界面拿到的这两样必须**同源**，否则会出现「标题写配表、
            # 里面却混着代码结论」这种自相矛盾的清单。
            "category_group": group,
            "category_group_label": CATEGORY_GROUP_LABELS.get(group, ""),
            "severity": self.severity,
            # 中文名（`严重` / `高`）。认不出的码值原样返回 —— 与
            # `report_document._label` 同一条兜底：不猜、也不显示成空。
            "severity_label": severity_label(self.severity),
            "confidence": self.confidence,
            "confidence_label": confidence_label(self.confidence),
            "evidence": evidence,
            # 逐条原子断言与各自的裁决（P0-01）。每一条自己带着 `status` / `status_label`
            # 与平台改写后的 `display` —— 中文口径在服务端算，界面不自己映射（同本文档）。
            "claims": claims,
            "commit_ref": self.commit_ref,
            "file_path": self.file_path,
            "impact": self.impact,
            "suggestion": self.suggestion,
            "disposition": self.disposition,
            "disposition_by": self.disposition_by,
            "disposition_at": self.disposition_at.isoformat() if self.disposition_at else None,
            "disposition_at_display": beijing_display(self.disposition_at),
            "disposition_note": self.disposition_note,
            # 「曾被人工忽略」的来历（不是当前处置，见列上的说明）。老行是 NULL，
            # 这里折成 False —— 界面按布尔值用，不该为「加列之前的历史行」多一个分支。
            "suppressed_previous": bool(self.suppressed_previous),
        }

    def __repr__(self):
        return f"<AiAnalysisAnomaly {self.id} {self.severity} {self.disposition}>"


class AiAnalysisIgnoredFingerprint(db.Model):
    """人工忽略的**持久台账**：一条指纹在某个周版本分组里被判定「不用再报」。

    ## 为什么不能只靠 `AiAnalysisAnomaly.disposition`（这正是它修的那个真漏口）

    「已忽略」原先只记在**某一次运行的行**上。而那一轮交付时，被忽略的条目会被
    `result_payload` 从 `anomalies` 里剔掉（只留一个 `suppressed_count`）—— 于是它
    **不在这一轮的行里**。而基线读侧（`baseline_source.baseline_findings`）只读
    「最近一次有结构化结论的运行」的那批行：

        run A：报了 5 条，其中 1 条被人工忽略
        run B：有结构化结论，但 0 行（本轮没报新问题 / 旧条目都被声明修复了）
        run C：模型又把那条误报报了出来 → 读侧读 B 的 0 行 → 什么都挡不住它

    结果就是那条误报以 `pending` 身份重新出现，人工的判断被**静默作废**，而且没有
    任何地方说得出这是为什么。台账把这份判断从「行」搬到「分组」上：B 报了几行、
    有没有报，都不再影响「这条已经被人判过不用管」还在不在。

    ## 为什么身份是 `(target_key, fingerprint)` 而不是指纹

    抑制是**分组内**的判决：同一条误报在另一个周版本分组里可能确实是个问题。所以
    唯一键是这两个字段的组合。`target_key` 为空（单提交分析）时**不写台账** ——
    那种运行没有分组可隔离，宁可退回旧逻辑，也不跨组扫（`baseline_source` 那一侧
    同一条判据）。

    ## 为什么 `project_id` 没有外键

    `services/repository_admin_handlers.delete_project` 有一份**手写的**「项目下要清
    哪些表」清单，而它不在本工作包的所有权内；给 `project_id` 加一个非空外键，会让
    删除项目在那份清单更新之前直接失败（`NOT NULL constraint failed` / FK 冲突），
    代价远大于收益。台账行仍然会**随 `last_run_id` 级联删掉**（见那一列的说明），
    所以正常删除路径上不会留下孤儿行。

    ## 为什么有一条 `file_path`

    契约里的列清单没有它，但少了它，「忽略不是删除」这条保证会在这张台账上失效：
    抑制的判据是「人工已忽略 **且** 相关文件没有再变」（`baseline.classify`），而那
    一判据要的正是文件路径。台账只在「上一轮的行里已经没有它」时才是唯一来源，
    没有路径就只剩「无条件抑制」—— 那等于把一次忽略变成一张永久封条。
    """

    __tablename__ = "ai_analysis_ignored_fingerprint"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, nullable=False, index=True)
    # 周版本的分组键（`build_weekly_group_key`：project|start|end|name）。空 = 不写台账。
    target_key = db.Column(db.String(200), nullable=True)
    fingerprint = db.Column(db.String(32), nullable=False)
    # 标题可空：台账的职责是「挡住这条指纹」，标题只是给基线摘要与排查用的线索。
    title = db.Column(db.String(500))
    # 抑制判据要用的文件路径（见类 docstring）。空 = 这条结论本来就没有文件归属，
    # 那种条目不会因为「文件又变了」复活，与 `baseline.classify` 的既有口径一致。
    file_path = db.Column(db.String(500))
    ignored_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    # 这条忽略是在哪一次运行上做的（审计用）。**跟着运行一起死**：删运行、保留期清理、
    # 全量重置最终都会 DELETE `ai_analysis_run`，而这三条路径都不在本工作包的所有权内
    # （`run_cache_source` / `usage_statistics`），应用层补不上 DELETE；它们又都是批量
    # DELETE，ORM 事件也不会触发。所以用数据库级的 `ON DELETE CASCADE`。
    # 可空：老库/已清理的运行没有 run 可指，读取侧不依赖它。
    last_run_id = db.Column(
        db.Integer,
        db.ForeignKey("ai_analysis_run.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # 唯一键就是台账的身份：同一个分组里同一条指纹只该有一行。
    # 写侧是「先查再插」，但**最终裁决权在数据库**：两处界面同时处置同一条时，
    # 后插入的那一笔会撞唯一键、整笔处置一起回滚（用户看到一次明确的失败），
    # 而不是在库里留下两行互相矛盾的「忽略时间」—— 读侧取哪一行都会像是对的。
    #
    # 写成**具名唯一索引**而不是 `UniqueConstraint`：老库上补唯一性只能走
    # `CREATE UNIQUE INDEX`（SQLite 不支持 `ALTER TABLE ADD CONSTRAINT`），
    # 而新建库由 `create_all` 建出同名索引 —— 两条路落成同一个对象，迁移才有幂等可言。
    __table_args__ = (
        db.Index(
            "uq_ai_ignored_fingerprint_target",
            "target_key",
            "fingerprint",
            unique=True,
        ),
    )

    def __repr__(self):
        return (
            f"<AiAnalysisIgnoredFingerprint {self.id} {self.target_key!r} "
            f"{self.fingerprint!r}>"
        )
