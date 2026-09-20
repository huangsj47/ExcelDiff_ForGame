"""一次分析的**读侧形态**：把 `ai_analysis_run` 的一行翻译成界面要的那几个字段。

## 为什么要单独一层

读侧以前是「有新结论就返回、失败一律折叠成 None」，于是界面里 `status === 'failed'`
那个分支是死的、用户看到的是「暂无分析结果」——**失败与「没分析过」被混成了一件事**。
所以这里把三种形态分开（进行中 / 有结论 / 最近一次失败），每种都给出自己那几个字段，
并且**中文口径在服务端算**（`scope_label` / `risk_label`）：码值直接印出来是
「风险等级 mid_high | 范围 full」这种半中半英的串，而导出报告与历史列表里同一件事
都是中文，三份抽屉模板各写一份映射表的话改一处必然漏两处。

从 `services/ai_analysis_service.py` 拆出来：那个文件已经贴着仓库的 2000 行硬上限
（`scripts/check_file_length.py --strict`），而这一组函数只依赖 run 行与两个标签函数，
与「怎么跑一次分析」没有耦合。
"""

from __future__ import annotations

import json
from typing import Optional

from models.ai_analysis import AiAnalysisRun
from services.ai.report_document import risk_label, scope_label
from utils.timezone_utils import format_beijing_time


def _parse_response_payload(raw: Optional[str]) -> Optional[dict]:
    """结论那一列的 JSON。解析不出来就是 `None` —— 调用方按「给不出结论」处理。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# 界面上的「最近分析」时间一律走北京时间（UTC+8）。
#
# 库里存的是 naive-UTC 墙钟（SQLite 会丢掉 tzinfo，口径见 utils/timezone_utils），
# 而 created_at 直接 isoformat() 出来是「带时间、不带偏移、还带微秒」的串，界面上
# 就长成 2026-09-17T12:53:28.872891 —— 既不是北京时间，也不是人看的格式。
#
# 为什么在**服务端**格式化而不是交给前端：ES 规范里「带时间但不带偏移」的 ISO 串
# 按**浏览器本地时区**解析，非 UTC+8 的机器上再转换一次就又多错 8 小时。在服务端
# 算好、前端只负责显示，这类错就没有发生的余地。
def _created_at_display(run: AiAnalysisRun) -> Optional[str]:
    created_at = getattr(run, "created_at", None)
    if created_at is None:
        return None
    # format_beijing_time 把 naive 入参当 UTC 解释，与库里的 naive-UTC 口径一致
    return format_beijing_time(created_at, "%Y-%m-%d %H:%M:%S")


def _in_progress_result(run: AiAnalysisRun) -> dict:
    """「正在进行中」的读侧形态：给得出身份，给不出结论。"""
    return {
        "run_id": run.id,
        "status": "running",
        "in_progress": True,
        "scope": run.scope,
        "trigger_source": run.trigger_source,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "created_at_display": _created_at_display(run),
        "response_text": "",
        "result": None,
    }


def _conclusion_payload(run: AiAnalysisRun) -> dict:
    """一条**有结论的** run 的读侧形态。"""
    result = _parse_response_payload(run.response_payload)
    return {
        "run_id": run.id,
        "status": run.status,
        "scope": run.scope,
        # **中文口径在服务端算，界面不自己映射**：`scope` 与 `result.risk_level` 都是码值
        # （`full` / `mid_high`），直接印出来是「风险等级 mid_high | 范围 full」这种半中
        # 半英的串，而导出报告与历史列表里同一件事都是中文。三份抽屉模板各写一份映射表
        # 的话，改一处必然漏两处。
        "scope_label": scope_label(run.scope),
        "risk_label": _risk_label_of(result),
        "trigger_source": run.trigger_source,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "created_at_display": _created_at_display(run),
        "response_text": run.response_text,
        "result": result,
    }


def _risk_label_of(result: object) -> str:
    """结论里的风险等级 → 中文。取不到就是空串（界面按「-」显示）。"""
    if not isinstance(result, dict):
        return ""
    return risk_label(result.get("risk_level"))


def _last_attempt_failed_result(run: AiAnalysisRun) -> dict:
    """「最近一次失败」的读侧形态：给得出失败原因，给不出结论。

    这条路径以前根本走不到 —— 读侧只在「有新结论」时返回值，失败一律折叠成 None，
    于是界面里那个 `status === 'failed'` 分支（「上次分析失败：<原因>」）是死的，
    用户看到的是「暂无分析结果」。失败与「没分析过」是两件事，不能混。
    """
    return {
        "run_id": run.id,
        "status": "failed",
        "in_progress": False,
        "scope": run.scope,
        "scope_label": scope_label(run.scope),
        "trigger_source": run.trigger_source,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "created_at_display": _created_at_display(run),
        "error_message": run.error_message or "",
        "response_text": "",
        "result": None,
    }
