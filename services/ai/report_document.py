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
* **异常清单的「处置」列是导出那一刻的状态快照，不是模型结论的一部分。** 模型从来不写这一列
  （它压根不知道人处理到哪一步）；平台在导出时按 `fingerprint` 现查这一次运行的
  `AiAnalysisAnomaly` 行，把「待确认 / 已确认 / 已忽略」贴上去。所以**同一份报告隔天再导，
  这一列可能不一样** —— 那是预期：报告原文逐字不变，变的是人工处置的进度。查不到记录的
  那一格写 `-`，**不回落成「待确认」**（那等于替用户断言「还没人处理过」）。

  > 这一条原先写的是「处置状态在库里**从来没有写入路径**，整列必然是待确认，印出来就是
  > 假信息」。那个**前提已经不成立**了：写入路径（`services/ai/anomaly_disposition.py`
  > 与三个接口）和界面（`static/js/ai_anomaly_disposition.js`）都已交付。
  > 留着旧说法比没有更糟 —— 它会让后来的人以为这一列不可做。
* **没有达门槛的异常时，说一句「本次没有达到门槛的异常条目」，不画空表。** 一张只有表头的
  表会被读成「这一项还没填」。

## 这一层不知道的事

「这次运行有没有可导出的结论」「这个人能不能看这个项目」都不在这里判断 —— 那是路由的
事（按 run 自己的 project_id 判权，与 `/runs/<id>/usage` 同一条口径）。这里只负责把
已经拿到手的东西拼成一份文档。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

from utils.timezone_utils import format_beijing_time

from services.ai import skill_contract
from services.ai.skill_contract import DEFAULT_DIMENSION_SPECS, dimension_labels_of

# ---------------------------------------------------------------------------
#  给人看的字
# ---------------------------------------------------------------------------
# **每一个都要与既有的那处在字面上一致**（不一致就是两套说法）：
#   * 风险等级 —— 抽屉 meta 行、历次结论列表；
#   * 分析范围 —— `services/ai/change_set.py::_SCOPE_LABELS`（「全量」/「增量」）；
#   * 触发方式 —— `templates/ai_usage_dashboard.html` 的「定时 / 手动」；
#   * 严重度与置信度 —— `services/ai/skill_contract.py` 的 SEVERITIES / CONFIDENCES，
#     这两档是**契约**（更低的置信度按契约只能写进正文，不该出现在给人工跟进的清单里）；
#   * 维度 —— **这次分析当时生效的那份清单**（结果里存的 `dimension_specs`，见
#     `dimension_labels_from_payload`）；只有那份缺失时才回落到
#     `services/ai/skill_contract.DEFAULT_DIMENSION_SPECS` 里的中文名。
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
    # **降级完成**：跑完了、有报告，但流程没走完（轮次用尽 / 额度用尽 / 没按协议输出 JSON /
    # 上下文超长后收尾 / 有分片没跑成）。原先这张表里没有它，于是这种运行在历次结论里显示的
    # 是原始英文码 `degraded` —— 而它恰恰是最需要被读出来的一档（「这次不是正常跑完的」）。
    # 措辞与消耗面板上那一档逐字一致（`templates/ai_usage_dashboard.html` 的
    # `{ succeeded: '完成', degraded: '降级完成', failed: '失败' }`）。
    "degraded": "降级完成",
    "failed": "分析失败",
}

SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高",
    # 平台赋值的等级（口径①「证据不足降一档」：`high` → `medium`）。模型写不出它
    # （`skill_contract.SEVERITIES` 只给 `critical` / `high`），但裁决与落库的值**就是**
    # 它，导出要如实显示降到了哪一档 —— 缺这一行的下场是按 `_label` 回落成原文
    # `medium`，在一份中文报告里显示一个英文码值。
    "medium": "中",
}

CONFIDENCE_LABELS = {
    "high": "高",
    "very_high": "很高",
}

# 检查维度的中文名映射。**从 `skill_contract` 的出厂清单派生**，不再手抄一份 ——
# 中文名与 id 分居两处时，加一个维度漏改一边的后果是报告里那一格显示英文 id，
# 而英文 id 看起来完全正常（它就是个正经标识符），不会有人发现。
#
# ## 这一份是**兜底**，不是权威
#
# 上面那份是平台出厂清单。项目可以在知识包里声明自己的维度清单
# （`LoadedSkills.dimensions`），那时导出该用的是**这一次分析当时生效的那一份** ——
# 它随结果一起落库（`result_payload` 的 `dimension_specs`），由
# `dimension_labels_from_payload` 读出来交给 `build_report_markdown`。
#
# **不许在导出时现查项目当前声明**：导出发生在很久之后，那时项目可能已经改过声明。
# 按今天的声明去翻译当时的结论，会把一条当时合法的发现显示成「未归类（performance）」
# ——那不是「显示得不准」，是按新口径**改写历史**，而报告读起来完全正常。
#
# 结果里没有这份清单时（`dimension_specs` 缺失 / 为空）回落到这一份：不是「兼容旧
# 数据」，只是「字段缺失时别崩」——那一格仍然会响亮地显示成「未归类（<原始 id>）」，
# 而不是把原始 id 当成一个正常维度名。
DIMENSION_LABELS: dict[str, str] = dimension_labels_of(DEFAULT_DIMENSION_SPECS)

# 没有归属的那一组怎么显示：`未归类（<原始 category>）`。
UNCLASSIFIED_LABEL = skill_contract.UNCLASSIFIED_LABEL
# category 都没给（模型漏写了）时的显示。
UNCLASSIFIED_UNKNOWN = f"{UNCLASSIFIED_LABEL}（未标注）"

# 「这次没有可导出的结论」的判定只此一份：跑完了、而且真的落了报告正文。
#
# `degraded` 与 `succeeded` 一样是**跑完了、有结论**（见
# `run_cache_source.CONCLUDED_STATUSES`）。把降级排除在外的后果是**倒退**：这些运行在
# `status` 能原生表示 degraded 之前存的就是 succeeded，本来就是可导出的；不改这一处，
# 它们会从「能导出」变成「不能导出」，而报告正文一个字都没少。
EXPORTABLE_STATUSES = ("succeeded", "degraded")


def dimension_labels_from_payload(payload: Any) -> dict[str, str]:
    """从**这一次结果自己存下来的**清单里取 id → 中文名（`result_payload` 的
    `dimension_specs`）。

    这是导出侧拿到「本次分析当时生效的清单」的**唯一**入口，也是它唯一该用的入口：
    在导出时现查项目当前声明会按新清单重新贴标签（见 `DIMENSION_LABELS` 上面那段）。

    读不出来（键缺失、值不是列表、条目形状不对）就回落到平台出厂那一份 —— 那一份是
    **兜底**，不是权威：它照样会把认不出的 category 显示成「未归类（<原始 id>）」。
    一条坏数据不该让整份导出失败，也不该让那一格悄悄变成一个看似正常的维度名。
    """
    raw = payload.get("dimension_specs") if isinstance(payload, Mapping) else None
    labels: dict[str, str] = {}
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            identifier = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            if identifier and label:
                labels[identifier] = label
    return labels or DIMENSION_LABELS


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


def dimension_label(category: Any, labels: Mapping[str, str] | None = None) -> str:
    """category → 报告里那一格的字。

    `labels` 是**本次分析当时生效的**清单（`dimension_labels_from_payload`）；不传就是
    平台出厂那一份。

    认不出来的值显示成 `未归类（<原始 category>）`：**原始 id 原样保留**（读的人要能拿它
    去对照项目声明/模型输出），但明确写出「平台不认识它」——只显示原始 id 会被读成一个
    正常的维度名。空值显示成 `未归类（未标注）`。
    """
    text = str(category or "").strip()
    if not text:
        return UNCLASSIFIED_UNKNOWN
    table = labels or DIMENSION_LABELS
    return table.get(text, f"{UNCLASSIFIED_LABEL}（{text}）")


def is_exportable(*, status: Any, report_text: Any) -> bool:
    """这次运行能不能导出一份文档。

    **两条都要**：状态是「有结论」（`EXPORTABLE_STATUSES`：succeeded 或 degraded），
    且确实有报告正文。只看状态不行 —— 失败也可能留下半份文本；只看正文也不行 ——
    一次「模型没答上来、平台按规模估了个等级」的运行，`report_text` 是空的，导出会得到
    一份只有元信息表的文件（那比不给更糟：它看起来像一份正常报告）。
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


def _wallclock_text(value: Any) -> str:
    """北京墙钟（naive datetime）→ `2026-09-08 00:00`。**不做时区换算**（见
    `weekly_target_label`：这两个值本来就是北京时间）。读不出来就是空串。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = _WHITESPACE.sub(" ", str(value)).strip()
    return text


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

    **这两个值已经是北京墙钟，不要再换算。** `start_time` / `end_time` 是用户在
    `<input type="datetime-local">` 里填的、原样入库的北京墙钟（见
    `utils/timezone_utils` 那两套墙钟的说明），页面上显示它们的地方（
    `weekly_version_logic.py:207`、`weekly_version_file_handlers.py:199`）都是直接
    `strftime`。原来是走 `format_beijing_time`（那个是给 naive-UTC 列用的）——
    等于**又加了一次 8 小时**：配置 `2026-03-02 00:00 ~ 2026-03-08 23:59` 在导出文档里
    写成 `2026-03-02 08:00 ~ 2026-03-09 07:59`，文件名里也是这个错时间，
    而同一份配置在周版本页面上显示的是正确的那一对。
    """
    base = _WHITESPACE.sub(" ", str(name or "")).strip()
    head = f"周版本 {base}" if base else "周版本"
    starts = _wallclock_text(start)
    ends = _wallclock_text(end)
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


def coverage_scope_note(coverage: Mapping[str, Any] | None) -> str:
    """账本 → 「分析范围」那一格的限定语（没账本 / 没缺口时是空串）。

    空串是**默认**：没有覆盖数据时一个字都不加（见 `_meta_rows` 里那段）。
    """
    if not isinstance(coverage, Mapping):
        return ""
    return str(coverage.get("scope_note") or "").strip()


def coverage_table_rows(coverage: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """账本 → 元信息表里那几行。**只搬运** —— 措辞与数字都由账本给（见
    `services/ai/coverage_ledger.coverage_rows`：数字与口径是同一件事）。

    形状不对的条目直接丢掉：这里的东西会被写进一张发出去的表格，一条坏数据不该让整份
    导出失败，也不该在表里留下半行。
    """
    if not isinstance(coverage, Mapping):
        return []
    rows: list[tuple[str, str]] = []
    for item in coverage.get("rows") or ():
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        name, value = str(item[0] or "").strip(), str(item[1] or "").strip()
        if name and value:
            rows.append((name, value))
    return rows


def coverage_gap_lines(coverage: Mapping[str, Any] | None) -> list[str]:
    """账本 → 「覆盖与缺口」那一段的每一条（**逐字搬运**账本里的 `gaps`）。"""
    if not isinstance(coverage, Mapping):
        return []
    return [str(one).strip() for one in coverage.get("gaps") or () if str(one or "").strip()]


def coverage_for_run(run: Any) -> dict:
    """一条 `AiAnalysisRun` → 覆盖账本。**导出路由用的一行入口。**

    实现在 `services/ai/coverage_ledger.py`（那边要读库：`request_payload` + 逐轮明细 +
    按工具计数）。import 刻意放在函数里 —— 本模块的其余部分全是纯函数（不碰库、不碰
    Flask，所以能被直接单测），不能因为这一个补账的入口把模型层拉进模块级的 import。
    """
    from services.ai import coverage_ledger

    return coverage_ledger.ledger_from_run(run)


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
    scope_note: str = "",
    coverage_rows: Sequence[tuple[str, str]] = (),
) -> Sequence[tuple[str, str]]:
    # 「分析范围」那一格在**有覆盖账本时**要带上限定语：`全量` 是**分析口径**，不等于
    # 「整个版本都看过了」——不加限定语，读者会把它读成「全读完了」，而实测一次周版本分析
    # 只取到过 60/996 个文件的证据（见 `services/ai/coverage_ledger.py`）。
    # 限定语由账本给（`scope_note`）：**没有覆盖数据时一个字都不加** —— 拿不确定当结论，
    # 与不加限定语一样糟。
    scope_value = scope_label(scope)
    if scope_note:
        scope_value = f"{scope_value}（{scope_note}）"
    rows: list[tuple[str, str]] = [
        ("项目", project_label),
        ("目标", target_label),
        ("分析时间", f"{created_at_display}（北京时间）" if created_at_display else "-"),
        ("风险等级", risk_label(risk_level)),
        ("定级依据", _reasons_text(risk_reasons)),
        ("分析范围", scope_value),
    ]
    # 覆盖那几行**紧跟分析范围**：范围与「实际覆盖了多少」是同一件事的两半，隔开摆就没人
    # 会把它们连起来读。行的字由账本给（数字与口径是同一件事，见 `coverage_rows`）。
    for name, value in coverage_rows:
        rows.append((str(name), str(value)))
    rows.append(("触发方式", trigger_label(trigger_source)))
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


def demote_headings(markdown: str) -> str:
    """把一段 markdown 的标题整体降一级（`#` → `##`，最多降到六级）。

    ## 为什么需要它

    报告末尾的「对账结果（找反证）」那一节，贴进来的是**对账轮交回的完整报告** ——
    7 个一级标题一个不少。原样嵌进去，整份文档就有了**两套一级标题**（实测那次：
    15 个一级标题，7 个各出现两次），而报告的契约是「固定 7 个一级标题、顺序固定」
    （`skill_contract.REPORT_SECTIONS`，`docs/AI分析使用说明.md` 也是这么写给测试同学看的）。
    读的人会以为收到两份报告；按 `^# ` 切的解析拿到的段数也不对。降一级之后，
    它整体挂在那一个 `##` 下面。

    ## 围栏里的 `#` 不动

    代码块里的 `# 注释` 是代码不是标题。逐行跟踪围栏状态。
    局限说清楚：只认 ```` ``` ```` / `~~~` 开头的行，**缩进四格的代码块不认** ——
    模型交回来的报告里没有这种写法，真出现时降的是里面的 `#`，不影响正文结论。
    """
    out: list[str] = []
    in_fence = False
    for line in markdown.split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            out.append(line)
            continue
        # 只处理 1~5 级：`######` 再降就是七级，CommonMark 里不成立
        if not in_fence and re.match(r"^#{1,5}\s", line):
            out.append("#" + line)
        else:
            out.append(line)
    return "\n".join(out)


def is_unclassified(category: Any, labels: Mapping[str, str] | None = None) -> bool:
    """这个 category 是不是「不在本次生效的清单里」（含干脆没给 category）。

    只用于「附录要不要多写一句说明」，所以判据跟着 `dimension_label` 走 —— 两边用同一份
    清单（`labels`），否则会出现「表里显示的是未归类、说明那句话却不出现」这种自相矛盾的
    附录。
    """
    text = str(category or "").strip()
    return not text or text not in (labels or DIMENSION_LABELS)


def anomaly_rows(
    anomalies: Iterable[Mapping[str, Any]],
    labels: Mapping[str, str] | None = None,
    dispositions: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """异常清单 → 表格行（**已经翻成给人看的字**，纯函数，可单测）。

    只读取渲染需要的这几个键：`severity` / `confidence` / `category` / `title` /
    `file_path` / `impact` / `evidence` / `suggestion` / `fingerprint`。缺的键按空处理 ——
    老记录的 payload 里没有 `evidence` 这个键（它是后加的）。

    `labels` 是**本次分析当时生效的**维度清单（见 `dimension_label`）。

    `dispositions` 是 `fingerprint → 人工处置的中文名`，由调用方（路由）在**导出这一刻**
    按这一次运行现查 `AiAnalysisAnomaly` 行翻好传进来。这里只做查表，不认识处置状态本身 ——
    中文名的单一来源是 `models/ai_analysis/anomaly.py::DISPOSITION_LABELS`，
    在这里再抄一份映射表就是同一个事实的第二份抄本。

    `unclassified` 是**平台自己算的**（不是模型给的字段）：它标记「这一条的 category
    不在本次生效的清单里」，`build_report_markdown` 据此决定要不要加那句说明。
    """
    rows: list[dict[str, Any]] = []
    for item in anomalies or ():
        if not isinstance(item, Mapping):
            continue
        evidence = item.get("evidence") or []
        if isinstance(evidence, str):
            evidence = [evidence]
        rows.append(
            {
                # **三格都要过 `_cell`**：`*_label` 认不出来的值原样回落，而那个值来自
                # 模型输出 —— 一个带换行与竖线的值能把附录表切成两行，凭空多出一行假数据
                # （这份文件是要发出去的）。
                "severity": _cell(severity_label(item.get("severity"))),
                "confidence": _cell(confidence_label(item.get("confidence"))),
                "dimension": _cell(dimension_label(item.get("category"), labels)),
                # 查不到就写 `-`，**不许回落成「待确认」**：那等于替用户断言「这一条还没人
                # 处理过」，而我们其实只是没找到它的记录 —— 与这份文件反复在防的那种
                # 「印一行假信息」是同一件事。
                "disposition": _cell(
                    (dispositions or {}).get(str(item.get("fingerprint") or ""))
                    or DISPOSITION_UNKNOWN
                ),
                "unclassified": is_unclassified(item.get("category"), labels),
                "title": _cell(item.get("title")),
                "file_path": _cell(item.get("file_path")),
                "impact": _cell(item.get("impact")),
                "evidence": [_cell(one) for one in evidence if str(one or "").strip()],
                "suggestion": str(item.get("suggestion") or "").strip(),
                # 逐条断言与各自的裁决（P0-01）。渲染成**已经翻好的一句话**（`display`
                # 与 `status_label` 都是服务端算好的，见 `verdict.ClaimReview`）——
                # 导出与界面读的是同一份，不在这里另造说法。
                "claims": [_claim_text(one) for one in item.get("claims") or ()],
            }
        )
    return rows


def _claim_text(claim: Any) -> str:
    """一条断言在导出里那一行。认不出的形状返回空串（**不印半行假信息**）。"""
    if not isinstance(claim, Mapping):
        return ""
    display = str(claim.get("display") or claim.get("statement") or "").strip()
    if not display:
        return ""
    parts = [
        f"`{str(claim.get('claim_id') or '').strip()}`",
        str(claim.get("status_label") or "").strip(),
        f"—— {display}",
    ]
    scope = str(claim.get("checked_scope") or "").strip()
    if scope:
        parts.append(f"（查过：{scope}）")
    return _cell(" ".join(part for part in parts if part))


APPENDIX_TITLE = "异常清单（平台按门槛过滤后）"

# 「覆盖与缺口」那一段的标题。**它不是一级标题**（用粗体行）：这份文档对外承诺「报告正文
# 固定 7 个一级标题」（`skill_contract.REPORT_SECTIONS`），平台自己再插一个一级标题会把
# 那个承诺打破，而读者是拿它当目录用的。
COVERAGE_TITLE = "本次覆盖与缺口"

# 附录里查不到处置记录时那一格写什么。见 `anomaly_rows` 里那段注释：不回落成「待确认」。
DISPOSITION_UNKNOWN = "-"

# 附录开头那句。**它必须说明「这份清单不是全部」**：达到门槛才进来，没达到的只写在正文里。
# 不写这一句，读者会把附录当成「模型发现的所有问题」——那正是平台一直在避免的那种误读。
#
# 「处置」列那两句也必须在这里说，因为它有一个**反直觉的地方**：这一列是导出那一刻现查的，
# 不是分析时的快照 —— 同一份报告隔天再导，「已确认」的条数可能变多。不写明的话，
# 两次导出拿到不同结果的人会以为平台在改历史。
APPENDIX_INTRO = (
    "平台只列达到门槛（严重度 / 置信度 / 条数）的条目；"
    "未达到门槛的问题只写在正文里，不在这里。"
    "「处置」列是**导出这一刻**平台上的人工处置状态（`-` 表示平台上没有这一条的记录）；"
    "已被标成「已忽略」的条目本来就不在这张表里。"
)

EMPTY_APPENDIX = "本次没有达到门槛的异常条目。"

# 附录里有「未归类」条目时补的那一句。**这一句不能省**：一份发出去的报告里出现
# 「未归类」，读的人必须知道那是什么意思、以及该拿什么去对照 —— 否则它会被读成
# 「平台算不出来」，而真实含义是「这条发现不属于本次生效的任何维度，但没有被丢掉」。
UNCLASSIFIED_NOTE = (
    "维度列写着「未归类」的条目，是模型给的 `category` 不在本次生效的维度清单里。"
    "**平台没有丢弃它们**（按原样列在上面），但它们不属于清单上的任何一个维度 —— "
    "需要人工判断该归到哪里。若本项目在知识包 `references/project-facts.md` 里声明了"
    "自己的维度清单，请先对照那份清单确认它是否属于某个自有维度。"
)


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
    # **本次分析当时生效的**维度清单（id → 中文名）。路由从这条运行自己的
    # `response_payload.dimension_specs` 读出来（`dimension_labels_from_payload`）——
    # 见 `DIMENSION_LABELS` 上面那段（现查项目当前声明是篡改历史）。不传 / 传空
    # 就是平台出厂那一份。
    dimension_labels: Mapping[str, str] | None = None,
    # `fingerprint → 人工处置的中文名`，**导出这一刻**现查（见 `anomaly_rows`）。
    # 不传就是整列 `-`：宁可写「不知道」，也不替用户断言「这一条还没人处理过」。
    dispositions: Mapping[str, str] | None = None,
    # **这一次运行的覆盖账本**（`services/ai/coverage_ledger.ledger_from_run(run)`）。
    # 传了它，元信息表里就会出现「覆盖（版本清单 / 列出的名字 / 取到证据）」那几行、
    # 「分析范围」那一格带上限定语，表下再列出缺口。不传（`None`）时这份文档与以前
    # **逐字相同** —— 覆盖数据是「额外的诚实」，不是让老调用方跟着改的理由。
    coverage: Mapping[str, Any] | None = None,
    title: str = "AI 变更风险分析报告",
) -> str:
    """拼出整份文档。**报告原文逐字出现在中间**，前后各一条 `---` 把它隔开。

    元信息里那句「该等级仅按变更规模估算，不是模型评估结果」不是这里写的 —— 它随
    `risk_reasons` 一起从 `result_payload` 来（原文带上）。**警示语的单一来源是产生它的
    那一层**：在这里另写一句，两处迟早会说得不一样。

    维度那一列同理：中文名来自 `dimension_labels`（本次分析**当时**生效的清单），
    而不是在导出时现查项目声明 —— 理由见 `DIMENSION_LABELS` 上面那段。

    「处置」那一列**反过来**：它要的就是导出这一刻的现状（见模块抬头第 2 条口径），
    所以由调用方现查、翻好中文名传进来。

    **覆盖那一组行与「覆盖与缺口」那一段只在调用方传了账本时出现**（`coverage`）：
    「分析范围：全量」本来是这一份文档里唯一关于「看了多少」的字，而它是分析口径、不是
    覆盖率 —— 传了账本，读者才看得到「这个版本 996 个文件里 60 个取到过证据」。没传时
    这份文档与以前**逐字相同**（覆盖是额外的诚实，不是让老调用方跟着改的理由）。
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
        scope_note=coverage_scope_note(coverage),
        coverage_rows=coverage_table_rows(coverage),
    ):
        lines.append(f"| {name} | {_cell(value)} |")

    # 缺口那几句话紧跟在元信息表之后、正文之前：读者是在这里建立「这份报告看了多少」的
    # 前提的，等读完正文再看到它就晚了（而正文里的每一条结论都建立在这个前提上）。
    gap_lines = coverage_gap_lines(coverage)
    if gap_lines:
        lines.append("")
        lines.append(f"**{COVERAGE_TITLE}**")
        lines.append("")
        for one in gap_lines:
            lines.append(f"- {one}")

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

    rows = anomaly_rows(anomalies, dimension_labels, dispositions)
    if not rows:
        lines.append(EMPTY_APPENDIX)
    else:
        # 「未归类」那一句紧跟在附录开头那句说明之后、表格之前：读的人在看到表里那些
        # 「未归类（xxx）」之前先知道它是什么意思。
        if any(row["unclassified"] for row in rows):
            lines.append(UNCLASSIFIED_NOTE)
            lines.append("")
        lines.append("| 严重度 | 置信度 | 维度 | 处置 | 标题 | 文件 | 影响 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for row in rows:
            lines.append(
                "| {severity} | {confidence} | {dimension} | {disposition} | {title} | "
                "{file} | {impact} |".format(
                    severity=row["severity"],
                    confidence=row["confidence"],
                    dimension=row["dimension"],
                    disposition=row["disposition"],
                    title=row["title"] or "-",
                    file=row["file_path"] or "-",
                    impact=row["impact"] or "-",
                )
            )
        # 证据与建议单独列在表下：它们是多行的长文本，塞进表格单元格里会把表撑得没法读。
        blocks = [row for row in rows if row["evidence"] or row["suggestion"] or row["claims"]]
        if blocks:
            lines.append("")
            for index, row in enumerate(blocks, 1):
                lines.append(f"**{index}. {row['title'] or '（无标题）'}**")
                lines.append("")
                if row["claims"]:
                    # 断言清单排在证据之前：它回答的是「这条结论凭什么算核过了」，
                    # 而证据是「它引用了什么」。读者要按这个次序读才对得上。
                    lines.append("- 断言：" + "；".join(row["claims"]))
                for one in row["evidence"]:
                    lines.append(f"- 证据：{one}")
                if row["suggestion"]:
                    lines.append(f"- 建议：{_WHITESPACE.sub(' ', row['suggestion']).strip()}")
                lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"
