#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一次分析导出成**一份单文件 markdown**：元信息 → 报告原文 → 异常清单附录。

## 为什么是纯函数、为什么单独一个文件

三个理由，按重要性排：

1. **同一份字不许写两遍。** 「风险等级」「分析范围」「触发方式」这几个词，抽屉的 meta 行、
   历次结论列表的每一行、导出文档的表头，是**同一批人的同一批字**。散在路由里各拼一次，
   改一处就会漂移一处（这个仓库反复栽在这上面）。所以它们在这里各有一个 `*_label`。
2. 它不碰数据库、不碰 Flask —— 输入全是字符串与字典，于是可以被完整单测（**正文逐字**、
   表格列、文件名这些最容易悄悄坏掉的地方都是纯函数断言）。
3. `services/ai_analysis_service.py` 贴着仓库的长度闸门（1999 行，
   `scripts/check_file_length.py --strict` 在 2000 行报错），新逻辑一律进新文件。

## 三条口径

* **报告原文逐字带上，不改写、不重排、不改标题层级。** 这份文档是拿出去给人看的
  （交给策划/QA、贴进工单），它必须和模型说过的**一模一样**；平台加的元信息与附录都在
  正文之外，且用 `---` 与正文隔开。想「润色一下」的念头到此为止。
* **异常清单不写「处置」列。** 处置状态（待确认/已确认/已忽略）在库里**从来没有写入
  路径**（见 `models/ai_analysis/anomaly.py`，`services/ai_analysis_service.py` 那条
  INSERT 不碰它），整列必然是「待确认」—— 那是在一份要发出去的文档里印一行假信息。
  等写路径做出来了再加（README 的「后续优化方向」里记着这件事）。
* **没有达门槛的异常时，说一句「本次没有达到门槛的异常条目」，不画空表。** 一张只有表头的
  表会被读成「这一项还没填」。

## 这一层不知道的事

「这次运行有没有可导出的结论」「这个人能不能看这个项目」都不在这里判断 —— 那是路由的
事（按 run 自己的 project_id 判权，与 `/runs/<id>/usage` 同一条口径）。这里只负责把
已经拿到手的东西拼成一份文档。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from utils.timezone_utils import format_beijing_time

# ---------------------------------------------------------------------------
#  给人看的字
# ---------------------------------------------------------------------------
# **每一个都要与既有的那处在字面上一致**（不一致就是两套说法）：
#   * 风险等级 —— 抽屉 meta 行、历次结论列表；
#   * 分析范围 —— `services/ai/change_set.py::_SCOPE_LABELS`（「全量」/「增量」）；
#   * 触发方式 —— `templates/ai_usage_dashboard.html` 的「定时 / 手动」；
#   * 严重度与置信度 —— `services/ai/skill_contract.py` 的 SEVERITIES / CONFIDENCES，
#     这两档是**契约**（更低的置信度按契约只能写进正文，不该出现在给人工跟进的清单里）；
#   * 维度 —— `docs/AI分析使用说明.md` §「九个维度都要留痕」里那份中文名。
RISK_LABELS = {
    "high": "高",
    "mid_high": "中高",
    "medium": "中",
    "mid_low": "中低",
    "low": "低",
}

SCOPE_LABELS = {
    "full": "全量",
    "incremental": "增量（只含上次分析之后变化的部分）",
}

TRIGGER_LABELS = {
    "manual": "手动",
    "scheduled": "定时",
}

STATUS_LABELS = {
    "pending": "排队中",
    "running": "分析中",
    "succeeded": "已有结论",
    "failed": "分析失败",
}

SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高",
}

CONFIDENCE_LABELS = {
    "high": "高",
    "very_high": "很高",
}

# 九个检查维度的中文名。**顺序与 `skill_contract.DIMENSION_IDS` 一致**（那张表是运行期
# 契约），这里只是把 id 翻成人话；映射里查不到的 id 原样显示 —— 编一个中文名比显示
# `foo_bar` 更糟（后者一眼能看出是没见过的值，前者看起来像个正经维度）。
DIMENSION_LABELS = {
    "config_id": "配置 ID",
    "config_value": "配置取值",
    "config_data": "配置数据本身",
    "value_sanity": "数值是否合理",
    "config_linkage": "单表连锁",
    "module_coupling": "模块耦合",
    "code_logic": "代码逻辑",
    "version_branch": "版本分支",
    "process": "流程",
}

# 「这次没有可导出的结论」的判定只此一份：跑完了、而且真的落了报告正文。
EXPORTABLE_STATUSES = ("succeeded",)


def _label(table: Mapping[str, str], value: Any, default: str = "-") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    return table.get(text, text)


def risk_label(level: Any) -> str:
    return _label(RISK_LABELS, level)


def scope_label(scope: Any) -> str:
    return _label(SCOPE_LABELS, scope)


def trigger_label(source: Any) -> str:
    return _label(TRIGGER_LABELS, source)


def status_label(status: Any) -> str:
    return _label(STATUS_LABELS, status)


def severity_label(severity: Any) -> str:
    return _label(SEVERITY_LABELS, severity)


def confidence_label(confidence: Any) -> str:
    return _label(CONFIDENCE_LABELS, confidence)


def dimension_label(category: Any) -> str:
    return _label(DIMENSION_LABELS, category)


def is_exportable(*, status: Any, report_text: Any) -> bool:
    """这次运行能不能导出一份文档。

    **两条都要**：状态是成功，且确实有报告正文。只看状态不行 —— 失败也可能留下半份
    文本；只看正文也不行 —— 一次「模型没答上来、平台按规模估了个等级」的运行，
    `report_text` 是空的，导出会得到一份只有元信息表的文件（那比不给更糟：它看起来
    像一份正常报告）。
    """
    return (
        str(status or "").strip() in EXPORTABLE_STATUSES
        and bool(str(report_text or "").strip())
    )


# 「分析时间」在文档里与界面上那行「最近分析」是**同一个时刻的同一种写法**。
# 格式串与 `services/ai_analysis_service._created_at_display` 一致，由
# tests/test_ai_report_document.py::test_the_display_time_matches_the_drawer 钉着
# （两边各写一份格式串，迟早会有一边被改掉而没人发现）。
BEIJING_DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"


def beijing_display(when: Any) -> str:
    """库里那个 naive-UTC 的 `created_at` → 北京时间字符串。None → 空串。"""
    if when is None:
        return ""
    return format_beijing_time(when, BEIJING_DISPLAY_FORMAT)


# ---------------------------------------------------------------------------
#  文件名
# ---------------------------------------------------------------------------
# **刻意不用 `werkzeug.utils.secure_filename`，也不用
# `utils/path_security._sanitize_segment`**：它们会把中文整段抹掉（前者对非 ASCII 直接
# 返回空串，后者同理），而本平台的项目名与周版本名几乎全是中文 —— 用它们的结果是每个人
# 下载到的文件都叫 `AI-20260912.md`，看不出是哪一份。
#
# 于是自己挡：只处理**真的会让文件系统或响应头出问题**的字符。路径分隔符与 Windows 保留
# 字符（`\ / : * ? " < > |`）、控制字符（含换行 —— 它能注入响应头）、以及结尾的点与空格
# （Windows 上会被静默去掉，于是实际文件名与显示的不一致）。其余原样保留。
_FILENAME_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_WHITESPACE = re.compile(r"\s+")

# 单段上限：整个文件名（含扩展名）留在常见文件系统的舒服区间里。项目名与目标名各限一段，
# 于是「项目名很长」不会把日期和扩展名挤掉。
FILENAME_PART_MAX_CHARS = 40


def safe_filename_part(text: Any, *, max_chars: int = FILENAME_PART_MAX_CHARS) -> str:
    """把一段文本变成可以放进文件名的一小段。**中文保留。**

    空白（含换行、制表符）折成一个空格并去掉首尾；保留字符换成 `_`；结尾的点去掉
    （`报告.` 在 Windows 上会被存成 `报告`）。全部被换掉或本来就是空 → `未命名`，
    不返回空串（空串会让拼出来的文件名变成 `AI分析报告--20260912.md`）。
    """
    cleaned = _WHITESPACE.sub(" ", str(text or "")).strip()
    cleaned = _FILENAME_FORBIDDEN.sub("_", cleaned)
    cleaned = cleaned.rstrip(". ")
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip(". ")
    return cleaned or "未命名"


def report_filename(
    *,
    project_name: Any,
    target_label: Any,
    when: Any,
    prefix: str = "AI分析报告",
) -> str:
    """`AI分析报告-<项目>-<目标>-20260912.md`。

    日期取**北京时间**，与 `_created_at_display`（界面上那行「最近分析」）同源 ——
    文件名按 UTC 算的话，晚上 8 点之后导出的文件会标着前一天的日期。
    """
    day = format_beijing_time(when, "%Y%m%d") if when is not None else "未知日期"
    parts = [
        safe_filename_part(prefix),
        safe_filename_part(project_name),
        safe_filename_part(target_label),
        safe_filename_part(day),
    ]
    return "-".join(parts) + ".md"


# ---------------------------------------------------------------------------
#  目标名（提交号 / 周版本）
# ---------------------------------------------------------------------------
def commit_target_label(commit_id: Any, message: Any = "", *, short_chars: int = 12) -> str:
    """`提交 f844faa6c1d2`（提交信息折成一行附在后面，太长就截）。

    提交号截前 12 位：**这是给人看的标签，不是身份** —— 完整 40 位在文档里另有一行
    （元信息表里那句就是完整的），截断只为了让表头与文件名短到能一眼读完。
    """
    sha = _WHITESPACE.sub(" ", str(commit_id or "")).strip()
    short = sha[:short_chars] if short_chars > 0 else sha
    head = f"提交 {short}" if short else "提交"
    subject = _WHITESPACE.sub(" ", str(message or "")).strip()
    if not subject:
        return head
    if len(subject) > 60:
        subject = subject[:60] + "…"
    return f"{head}（{subject}）"


def weekly_target_label(
    name: Any,
    start: Any = None,
    end: Any = None,
    *,
    separator: str = " ~ ",
) -> str:
    """`周版本 2026-09-08周（2026-09-08 00:00 ~ 2026-09-14 23:59）`。

    时间窗是**这个周版本的边界**（`WeeklyVersionConfig.start_time/end_time`），不是
    「什么时候跑的分析」—— 后者在元信息表里另有一行。少了它，两个名字很像的周版本
    靠文件名分不开。
    """
    base = _WHITESPACE.sub(" ", str(name or "")).strip()
    head = f"周版本 {base}" if base else "周版本"
    starts = format_beijing_time(start, "%Y-%m-%d %H:%M") if start is not None else ""
    ends = format_beijing_time(end, "%Y-%m-%d %H:%M") if end is not None else ""
    if not starts and not ends:
        return head
    return f"{head}（{starts}{separator}{ends}）"


# ---------------------------------------------------------------------------
#  文档本体
# ---------------------------------------------------------------------------
def _cell(text: Any) -> str:
    """表格单元格里的文本。

    `|` 必须转义（不转义会把这一行切成多列），换行必须折掉（表格里换行会让那一行断掉）。
    """
    return _WHITESPACE.sub(" ", str(text or "")).replace("|", "\\|").strip()


def _reasons_text(reasons: Iterable[Any]) -> str:
    """定级依据：一条一行，折成同一行用「；」连起来（表格里不能换行）。"""
    items = [str(item).strip() for item in (reasons or []) if str(item or "").strip()]
    return "；".join(items)


def _meta_rows(
    *,
    project_label: str,
    target_label: str,
    run_id: Optional[int],
    created_at_display: str,
    risk_level: Any,
    risk_reasons: Iterable[Any],
    scope: Any,
    trigger_source: Any,
    model: str,
    degradation_label: str,
    focus_label: str,
) -> Sequence[tuple[str, str]]:
    rows: list[tuple[str, str]] = [
        ("项目", project_label),
        ("目标", target_label),
        ("分析时间", f"{created_at_display}（北京时间）" if created_at_display else "-"),
        ("风险等级", risk_label(risk_level)),
        ("定级依据", _reasons_text(risk_reasons)),
        ("分析范围", scope_label(scope)),
        ("触发方式", trigger_label(trigger_source)),
    ]
    if focus_label:
        rows.append(("分析焦点", focus_label))
    # 降级要**紧跟着风险等级说**：等级那一行的依据里已经带了降级那句话（见
    # `result_payload.risk_level_from_outcome`），但读者扫表时未必会看「定级依据」那一格。
    # 没降级时写「未降级」而不是留空 —— 空着读起来像「这一项没填」。
    rows.append(("是否降级", degradation_label or "未降级"))
    if model:
        rows.append(("分析模型", model))
    if run_id is not None:
        # 运行号是**回到平台查这一次的凭据**（消耗明细在 /ai-analysis/runs/<id>/usage）。
        rows.append(("运行号", str(run_id)))
    return rows


def anomaly_rows(anomalies: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """异常清单 → 表格行（**已经翻成给人看的字**，纯函数，可单测）。

    只读取渲染需要的这几个键：`severity` / `confidence` / `category` / `title` /
    `file_path` / `impact` / `evidence` / `suggestion`。缺的键按空处理 —— 老记录的
    payload 里没有 `evidence` 这个键（它是后加的）。
    """
    rows: list[dict[str, str]] = []
    for item in anomalies or ():
        if not isinstance(item, Mapping):
            continue
        evidence = item.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        rows.append(
            {
                "severity": severity_label(item.get("severity")),
                "confidence": confidence_label(item.get("confidence")),
                "dimension": dimension_label(item.get("category")),
                "title": _cell(item.get("title")),
                "file_path": _cell(item.get("file_path")),
                "impact": _cell(item.get("impact")),
                "evidence": [_cell(one) for one in evidence if str(one or "").strip()],
                "suggestion": str(item.get("suggestion") or "").strip(),
            }
        )
    return rows


APPENDIX_TITLE = "异常清单（平台按门槛过滤后）"

# 附录开头那句。**它必须说明「这份清单不是全部」**：达到门槛才进来，没达到的只写在正文里。
# 不写这一句，读者会把附录当成「模型发现的所有问题」——那正是平台一直在避免的那种误读。
APPENDIX_INTRO = (
    "平台只列达到门槛（严重度 / 置信度 / 条数）的条目；"
    "未达到门槛的问题只写在正文里，不在这里。"
)

EMPTY_APPENDIX = "本次没有达到门槛的异常条目。"


def build_report_markdown(
    *,
    project_label: str = "",
    target_label: str = "",
    run_id: Optional[int] = None,
    created_at_display: str = "",
    risk_level: Any = "",
    risk_reasons: Iterable[Any] = (),
    scope: Any = "",
    trigger_source: Any = "",
    model: str = "",
    degradation_label: str = "",
    focus_label: str = "",
    report_text: str = "",
    anomalies: Iterable[Mapping[str, Any]] = (),
    suppressed_count: int = 0,
    title: str = "AI 变更风险分析报告",
) -> str:
    """拼出整份文档。**报告原文逐字出现在中间**，前后各一条 `---` 把它隔开。

    元信息里那句「该等级仅按变更规模估算，不是模型评估结果」不是这里写的 —— 它随
    `risk_reasons` 一起从 `result_payload` 来（原文带上）。**警示语的单一来源是产生它的
    那一层**：在这里另写一句，两处迟早会说得不一样。
    """
    lines: list[str] = [f"# {title}", ""]
    lines.append("| 项 | 内容 |")
    lines.append("| --- | --- |")
    for name, value in _meta_rows(
        project_label=project_label,
        target_label=target_label,
        run_id=run_id,
        created_at_display=created_at_display,
        risk_level=risk_level,
        risk_reasons=risk_reasons,
        scope=scope,
        trigger_source=trigger_source,
        model=model,
        degradation_label=degradation_label,
        focus_label=focus_label,
    ):
        lines.append(f"| {name} | {_cell(value)} |")

    lines.append("")
    lines.append("---")
    lines.append("")
    body = str(report_text or "").rstrip("\n")
    lines.append(body if body else "（这次运行没有报告正文。）")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"## {APPENDIX_TITLE}")
    lines.append("")
    if suppressed_count:
        # 被人工忽略的条目**不进清单**（尊重分诊结果），但要说一声有几条 —— 不说的话，
        # 「报告里提过、附录里没有」看起来像平台漏了。
        lines.append(f"另有 {suppressed_count} 条此前已被人工标记忽略，不在此列。")
        lines.append("")
    lines.append(APPENDIX_INTRO)
    lines.append("")

    rows = anomaly_rows(anomalies)
    if not rows:
        lines.append(EMPTY_APPENDIX)
    else:
        lines.append("| 严重度 | 置信度 | 维度 | 标题 | 文件 | 影响 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for row in rows:
            lines.append(
                "| {severity} | {confidence} | {dimension} | {title} | {file} | {impact} |".format(
                    severity=row["severity"],
                    confidence=row["confidence"],
                    dimension=row["dimension"],
                    title=row["title"] or "-",
                    file=row["file_path"] or "-",
                    impact=row["impact"] or "-",
                )
            )
        # 证据与建议单独列在表下：它们是多行的长文本，塞进表格单元格里会把表撑得没法读。
        blocks = [row for row in rows if row["evidence"] or row["suggestion"]]
        if blocks:
            lines.append("")
            for index, row in enumerate(blocks, 1):
                lines.append(f"**{index}. {row['title'] or '（无标题）'}**")
                lines.append("")
                for one in row["evidence"]:
                    lines.append(f"- 证据：{one}")
                if row["suggestion"]:
                    lines.append(f"- 建议：{_WHITESPACE.sub(' ', row['suggestion']).strip()}")
                lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"
