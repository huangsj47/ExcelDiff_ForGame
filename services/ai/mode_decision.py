#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预检（`/ai-analysis/usage/estimate`）的事实、参数解析与**模式裁决**。

## 为什么单独一个文件

`services/ai_usage_service.py` 已经贴着 2000 行的文件长度硬上限
（`scripts/check_file_length.py`），而 2026-11-01 的契约 1.6 又给预检加了一整块
「模式裁决」（`mode_decision`）与「同一份快照已有结论」。继续堆在那个文件里，
下一次改动会先撞长度闸门。

搬出来的都是**纯拼装 / 只读查询 / 参数归一**，没有一条写库、没有一个网络调用：
URL 参数解析、这一轮会进输入的账、平台内置提示词与窗口来源那两行说明、
模式裁决（与 `job_service._decide_effective_mode` 同一套判据）、
以及「同一份快照是否已有可复用结论」（判据委托 `job_service`）。

`ai_usage_service` 仍按原名回导这些函数，既有调用点与测试不用改。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from services.ai.budget import COMPACT_AT_RATIO
from services.ai.job_service import UPGRADE_REASON_NO_CONCLUSION_BASELINE
from services.ai.usage import MODE_FULL, MODE_INCREMENTAL
from services.app_routing_bootstrap_service import MAX_INTEGER_ID

#: 「平台**建议**整窗重跑」的原因短码（`scope_sampling.decide_scope` 的第二返回值）。
#: 它们是**建议**，不是升级原因：契约 1.1 之后 `scope` 仍是 `incremental`、基线照旧保留，
#: 平台也不会替用户把它改成 full。预检把它们放进 `mode_decision.suggestion_reason`，
#: 而 `mode_decision.upgrade_reason` 只留真实的升级原因（契约 1.6）。
SUGGESTION_REASONS = ("delta_count_high", "delta_ratio_high", "critical_path_detected")

#: 真实升级原因（`mode_decision.upgrade_reason` 的取值域）。**不是**建议原因：
#: `no_conclusion_baseline` = 没有可复用的结论基线（输入只能整窗读，模式仍是增量）；
#: `force_full_rebuild` = 输入没变但旧结论不可复用（改了规则/模型），平台重建一次；
#: `runtime_scope_upgrade` = 运行侧把这一次的 scope 真的升成了 full 的其他情形。
REAL_UPGRADE_REASONS = (
    "no_conclusion_baseline",
    "force_full_rebuild",
    "runtime_scope_upgrade",
)


def _real_upgrade_reason(raw: Any) -> str:
    """把运行侧/状态侧的原因短码折成**真实升级原因**（契约 1.6）。

    `first_run` 是历史字面值（2026-11-01 之前「没有结论基线」写的就是它）—— 读侧仍认，
    但对外只说 `no_conclusion_baseline`；建议原因（`delta_ratio_high` 等）**不升级**，
    所以一律折成空串；认不出来的非空短码归到 `runtime_scope_upgrade`（确实有什么让
    scope 变了，只是这一层不认识它的名字）。空值保持空。
    """
    code = str(raw or "").strip().lower()
    if not code:
        return ""
    if code in ("first_run", "no_conclusion_baseline"):
        return UPGRADE_REASON_NO_CONCLUSION_BASELINE
    if code in SUGGESTION_REASONS:
        return ""
    if code in REAL_UPGRADE_REASONS:
        return code
    return "runtime_scope_upgrade"


def _mode_decision(
    mode: str,
    *,
    suggested_full: bool = False,
    suggestion_reason: str = "",
    upgrade_reason: str = "",
) -> dict[str, Any]:
    """预检的**模式裁决**（契约 1.6）：用户要什么 / 平台会跑什么 / 为什么不一样。

    * `effective` 与 `job_service._decide_effective_mode` **同一套判据**：只有用户显式
      要全量才是 full，其余都是 incremental（没有结论基线也**不再**升 full）；
    * `suggested_full` / `suggestion_reason` 是平台的**建议**（变化多 / 比例高 / 命中
      关键路径），它不改变 `effective` —— 界面据此提示「建议整窗重跑」，由用户自己选；
    * `upgrade_reason` **只留真实升级原因**（见 `REAL_UPGRADE_REASONS`）：把
      `delta_ratio_high` 这类建议原因写进来，会让界面弹一句「平台把这次增量升成了全量」
      —— 而那件事已经不再发生了（实测那条「首次全量后一直全量」的固化链）；
    * `can_continue_incremental`：平台真的会跑增量时才为真（= 用户没有显式点全量）。
      界面拿它决定「继续增量」那个选项是不是一句真话。
    """
    requested = MODE_FULL if str(mode or "").strip().lower() == MODE_FULL else MODE_INCREMENTAL
    effective = requested
    return {
        "requested": requested,
        "effective": effective,
        "suggested_full": bool(suggested_full),
        "suggestion_reason": str(suggestion_reason or "") if suggested_full else "",
        "upgrade_reason": _real_upgrade_reason(upgrade_reason),
        "can_continue_incremental": effective == MODE_INCREMENTAL,
    }


def _same_snapshot_reused_run(config) -> Optional[int]:
    """当前输入（同一份已冻结快照）已有可复用结论时，返回那条运行的 id。

    判据**原样委托** `job_service.reusable_conclusion_for`（建 job 用的是同一个函数）：
    预检说「再点一次不会新建运行」，建 job 时就必须真的复用上 —— 两处各算一遍就会分叉。
    读不到时返回 `None`：这里只是**提示**，不该因为一次读失败让预检变成 500。
    """
    if config is None:
        return None
    try:
        from services.ai import job_service

        hit = job_service.reusable_conclusion_for(config)
    except Exception:  # noqa: BLE001 —— 预检读不到这件事不阻断估算
        return None
    return (hit or {}).get("run_id")


def _reuse_sentence(reused_run_id: Optional[int]) -> str:
    """「同一份快照已有可复用结论」那一句（没有就空串，不占位）。"""
    if not reused_run_id:
        return ""
    return (
        f"同一份快照已经有可复用的结论（运行 #{reused_run_id}）："
        "再点一次会直接复用它，不会新建运行（本次模型消耗为零）。"
    )


def _weekly_payload_facts(config_id: int, *, force_full: bool = False) -> dict[str, Any]:
    """这一轮会进输入的账，以及运行侧已经能确定的范围裁决。

    ## 为什么敢在**只读**端点上读它

    `build_weekly_payload` 是**平台自己的账**：读快照缓存行、按基线做差、把上轮没取到
    证据的文件按风险补回来（`scope_sampling`）。它不发任何模型请求，也没有副作用
    （不建 run、不写状态行）—— 与报告 §5.5 要求的「调用模型之前先看清要做什么」是同一件事。

    「不另立血缘」在这里是硬要求：补偿集与增量集的算法只有这一份实现（`_compensation_entries`
    / `_select_delta_entries`）。在这里重算一遍，两处迟早会在「风险排序」「焦点筛选」
    这些细节上分叉 —— 用户看到的预检数字与真正跑的内容对不上，比不给数字更坏。

    拿不到时回 `(None, None, 说明)`：界面如实写「没有算出来」并把原因转述出来。
    """
    from services.ai_analysis_service import build_weekly_payload

    try:
        payload, _state, skip_reason = build_weekly_payload(
            config_id, force_full=force_full
        )
    # 目录里那三处 `{"delta": None, ...}` 的失败返回也必须带上这个键，否则调用方读
    # `facts["plan"]` 会 KeyError（失败路径的字段集与成功路径不一致是最难查的一类）。
    except Exception as exc:  # noqa: BLE001 —— 预检读不到账不该让整个估算变成 500
        return {"delta": None, "compensation": None, "planned": None,
                "scope": "", "reason": "", "suggested_full": False, "plan": None,
                "note": f"本次的输入账没有算出来（{exc}）。"}
    if payload is None:
        return {"delta": None, "compensation": None, "planned": None,
                "scope": "", "reason": "", "suggested_full": False, "plan": None,
                "note": "本次的输入账没有算出来（平台裁决："
                + str(skip_reason or "未知") + "）。"}
    summary = payload.get("summary") or {}
    delta = summary.get("delta_files")
    compensation = summary.get("compensation_files")
    policy = payload.get("policy") or {}
    scope = str(payload.get("scope") or "")
    reason = str(policy.get("reason") or "")
    suggested_full = policy.get("suggested_full")
    if not isinstance(suggested_full, bool):
        # 老 payload / 只回二元组的调用点没有这个键：用原因短码兜底。这三个短码**只说明
        # 「平台建议整窗重跑」**，不再是「已经升成全量」（契约 1.1 / 1.4）。
        suggested_full = reason in SUGGESTION_REASONS
    return {
        "delta": int(delta) if isinstance(delta, int) else None,
        "compensation": int(compensation) if isinstance(compensation, int) else None,
        # 估算缩放必须用**真正会交给引擎的文件数**。它通常等于 delta，但全量预检
        # 会以 force_full 重建 payload，此时这里就是整窗文件数。由服务端算这一项，
        # 避免页面把上一次增量预检缓存的 files= 带进全量预检。
        "planned": len(payload.get("delta_files") or []),
        "scope": scope,
        "reason": reason,
        "suggested_full": bool(suggested_full),
        # **同一份计划**（工作包 B）：`build_weekly_payload` 已经算好并挂上去了，
        # 预估这一侧只读不推 —— 再推一次就会出现「确认框说 5 片、实际跑 3 片」。
        "plan": payload.get("plan") if isinstance(payload.get("plan"), dict) else None,
        "note": "",
    }


def _platform_prompt_chars_for(project_id: int) -> tuple[int, str]:
    """平台内置提示词一共多少字（`prompt.platform_prompt_chars`，与运行侧**同一个**函数）。

    返回 `(字数, 拿不到时的说明)`。拿不到时回 0 并且**明说**：悄悄回 0 等于告诉用户
    「平台一段内置提示词都不占」，那是一个假事实（而界面那一行正是拿它解释「生效值里
    为什么有一部分不属于你」）。加载失败本身不阻断估算 —— 估算只是几行数字。
    """
    from services.ai.prompt import platform_prompt_chars

    try:
        from services.ai_analysis_service import _load_project_skills

        loaded, failure = _load_project_skills(project_id)
    except Exception as exc:  # noqa: BLE001 —— 读不到内置提示词不该让估算变成 500
        return 0, f"平台内置提示词的字符数没有读出来（{exc}），下面的生效值里**不含**它。"
    if loaded is None:
        return 0, (
            "平台内置提示词的字符数没有读出来（"
            + str(failure or "分析协议未加载")
            + "），下面的生效值里**不含**它。"
        )
    return platform_prompt_chars(loaded), ""


def _estimate_window_note(
    *, clamp_note: str, window_tokens: int, watermark: int, platform_note: str
) -> str:
    """预估端点那一行「模型窗口来源」的原文（界面直接渲染它）。

    三件事按这个顺序说：**先说这次没问端点**（这是前提）、再说被压到哪里（如果有压）、
    最后说内置提示词那一份有没有算进来。顺序不能反 —— 先说「未声明窗口」会让读者以为
    平台问过了，而事实是这一条路径**没有探测**。
    """
    parts = [
        f"运行前不探测模型：这里的生效预算是按平台默认窗口 {window_tokens:,} token 的 "
        f"{int(COMPACT_AT_RATIO * 100)}% 水位（{watermark:,} 字）算的；"
        "任务真正启动前会向端点问一次真实窗口，届时以那一个为准（可能更小）。"
    ]
    if clamp_note:
        parts.append(clamp_note)
    if platform_note:
        parts.append(platform_note)
    return "".join(parts)


def _first_arg(args: Mapping[str, Any], *names: str) -> str:
    for name in names:
        if name in args:
            value = args.get(name)
            if value is None:
                continue
            return str(value).strip()
    return ""


def _parse_int(raw: str) -> Optional[int]:
    """把查询参数里的整数读出来。**任何非法值都回落 `None`（= 不筛），绝不抛异常。**

    上界不是洁癖：工具把值直接绑给 pysqlite 时，超过 64 位有符号数的整数会抛
    `OverflowError: Python int too large to convert to SQLite INTEGER`，而那不是
    `SQLAlchemyError`、仓库里也没有兜它的处理器 —— 管理员把地址栏里的项目号改错一位
    就白屏。超出这个范围的 id 不可能存在，按「不筛」处理与按「筛不到」处理在这里等价，
    都能给出一个正常页面。
    """
    if not raw or raw.lower() in {"all", "none", "0"}:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 < value <= MAX_INTEGER_ID else None


def parse_estimate_args(args: Mapping[str, Any]) -> tuple[Optional[int], dict[str, Any]]:
    """URL query → 估算参数 `(project_id, {mode, planned_files, baseline_reusable, notes})`。

    与 `parse_usage_filters` 同一条口径：**任何非法值都回落，绝不抛**。这些参数从地址栏
    来，让它们变成 500 的话，页面直接白屏而用户没有任何办法回到正常状态。

    `project_id` 是唯一一个「回落不了」的：估算必须知道是哪个项目（历史运行、价格表、
    分片数都是按项目取的），所以它缺失时返回 `None`，由路由回 400 —— 那不是一次非法
    访问，是一个缺少必填参数的请求。
    """
    notes: list[str] = []
    raw_project = _first_arg(args, "project", "project_id")
    project_id = _parse_int(raw_project)
    if raw_project and project_id is None:
        notes.append("项目编号不是有效数字。")

    raw_mode = _first_arg(args, "mode").lower()
    mode = raw_mode if raw_mode in (MODE_FULL, MODE_INCREMENTAL) else MODE_FULL
    if raw_mode and raw_mode not in (MODE_FULL, MODE_INCREMENTAL):
        notes.append(f"模式「{raw_mode}」认不出来，已按全量估算。")

    raw_files = _first_arg(args, "files", "planned_files")
    planned_files = _parse_int(raw_files)
    if raw_files and planned_files is None:
        notes.append(f"目标文件数「{raw_files}」不是有效数字，已按「不知道」处理。")

    raw_baseline = _first_arg(args, "baseline", "reusable").lower()
    baseline: Optional[bool] = None
    if raw_baseline:
        baseline = raw_baseline not in ("0", "false", "no", "off")

    raw_config = _first_arg(args, "config", "config_id")
    config_id = _parse_int(raw_config)
    if raw_config and config_id is None:
        notes.append("周版本配置编号不是有效数字，已按未指定分组处理。")

    return project_id, {
        "mode": mode,
        "planned_files": planned_files,
        "baseline_reusable": baseline,
        "config_id": config_id,
        "notes": notes,
    }
