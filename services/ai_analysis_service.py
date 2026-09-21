#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis service（真实执行器）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from models import (
    Commit,
    Project,
    Repository,
    WeeklyVersionConfig,
    db,
)
from models.ai_analysis import (
    # 测试按 `ai_service.AiProjectAnalysisConfig` 直接建行 / 数列（属性访问，不是 import），
    # 所以这个「本文件不直接用」的模型类必须留着 —— ruff 的 F401 会想删它。
    AiProjectAnalysisConfig,  # noqa: F401 —— 测试按属性取
    AiAnalysisAnomaly,
    AiAnalysisRun,
    AiAnalysisTrace,
    AiWeeklyAnalysisState,
)
from models.ai_analysis.project_config import (
    DEFAULT_MAX_FILES_PER_RUN,
)
from services.ai.analysis_budget import budget_gate_reason, early_stop_guard

# 基线的读侧：从库里取「上一次为止的结论」，做成提示词里那段「已经报过的问题」与
# 本轮该抹掉的指纹。**保留这里的同名引用**（下面两处调用点与既有测试都按这几个名字 import）。
# 为什么单独一层见 `services/ai/baseline_source.py` 的模块抬头：纯函数留在 `baseline.py`，
# 碰库的那一半搬出去，顺带让本文件回到 2000 行硬上限之内。
#
# **`# noqa: F401` 必须写在每个别名那一行**，不能只写在 `from … import (` 那一行：
# ruff 的 F401 是按「具体哪个名字没用」报的，诊断落在别名行上，写在开头那一行盖不住它 ——
# `ruff check --select F401 --fix` 会把这些**故意留着的回导**当垃圾删掉（真发生过一次：
# 删掉 `_baseline_findings` 之后 `tests/test_ai_baseline_needs_structured_conclusion.py`
# 在收集阶段就 ImportError）。
from services.ai.baseline_source import (
    baseline_digest as _baseline_digest,  # noqa: F401 —— 测试按这个名字 import
    baseline_findings as _baseline_findings,  # noqa: F401 —— 测试按这个名字 import
    previous_run as _previous_run,  # noqa: F401 —— 测试按这个名字 import
    skipped_unstructured_runs as _skipped_unstructured_runs,  # noqa: F401 —— 测试按这个名字 import
    suppressed as _suppressed,
)
from services.ai.budget import effective_prompt_budget
from services.ai.change_set import from_commit_payload, from_weekly_payload

# 同上面那个模型类：本文件不直接调它，但测试用 `monkeypatch` 打的是
# `ai_service.build_probe_client` 这个名字（属性访问），删掉就等于让补丁落空。
from services.ai.endpoint_service import build_probe_client  # noqa: F401 —— 测试按属性打补丁

# 读侧形态（进行中 / 有结论 / 最近一次失败）：只依赖 run 行与两个标签函数，
# 与「怎么跑一次分析」没有耦合，单独一层也好单测。
from services.ai.conclusion_view import (  # noqa: F401 —— 本文件的读侧路由仍在用
    _conclusion_payload,
    _created_at_display,
    _in_progress_result,
    _last_attempt_failed_result,
    _parse_response_payload,
)
from services.ai.engine import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineLimits,
    EngineOutcome,
    run_analysis,
)
from services.ai.engine import (
    failed as engine_failed,
)
from services.ai.llm_client import LLMError
from services.ai.platform_provider import PlatformContextProvider
from services.ai.project_config_source import (  # noqa: F401 —— 调用点与测试仍在用
    _coerce_timeout,
    _get_project_api_key,
    _get_project_config_row,
    _price_table_from_config,
    _price_version_for,
    _resolve_base_name,
    _utcnow,
    build_endpoint_client,
    build_weekly_group_key,
    get_project_analysis_config,
    get_project_api_key_status,
    project_price_table,
    set_project_api_key,
    update_project_analysis_config,
)
from services.ai.project_facts import (
    generated_prefixes,
)
from services.ai.provenance import current_provenance
from services.ai.result_payload import (
    failed_result,
    result_payload,
)
from services.ai.rules import RuleThresholds
from services.ai.run_cache_source import (  # noqa: F401 —— 启动清理与流式入口仍在用
    ANALYSIS_CACHE_DAYS,
    _analysis_cache_cutoff,
    _is_run_fresh,
    _json_dumps,
    _sse_event,
    _stream_cached_run,
    cleanup_expired_analysis_runs,
    fail_orphaned_analysis_runs,
)
from services.ai.run_progress import clear as clear_run_progress
from services.ai.run_progress import publish as publish_run_progress
from services.ai.scope_sampling import (  # noqa: F401 —— 任务服务与测试仍在用
    _decide_scope,
    _limit_items,
    _repo_priority,
    _sample_with_repo_fairness,
    _summarize_weekly_files,
    has_weekly_changes,
    weekly_snapshot_digest,
)
from services.ai.prompt import platform_prompt_chars
from services.ai.skill_loader import describe_load_error, load_skills
from services.ai.subagent import plan_family, run_family_with_seed, subagent_mode_of
from services.ai.trace_evidence import encode_evidence
from services.ai.usage import encode_tools
from services.ai.weekly_state import get_or_create_weekly_state
from services.ai.weekly_sync_gate import group_config_ids, weekly_sync_in_flight
from utils.logger import log_print

MAX_FILES_DEFAULT = DEFAULT_MAX_FILES_PER_RUN

# 变更清单「全列」的字符上限。一行约 60 字符（`  - [M] <path>`），所以 60,000 字符
# 约合 1,000 个文件。实测线上那一轮 767 个文件的全量清单是 45,777 字符 —— 装得下，
# 所以取样退化成兜底而不是常态。取值不更大的原因：清单每轮都要进提示词，而它每涨
# 一万字符就直接挤掉一条 diff（单条上限 11,000 字符）。
MAX_LIST_CHARS = 60_000

# 「分析范围」的取值之一：不筛。其余取值见 `_filter_delta_files_by_focus`。
FOCUS_ALL = "all"

EXECUTION_VERSION = "latest"

# 仓库根目录。**这个常量不能搬到 `services/ai/` 下面去**：它是按 `__file__` 的层数算出来的
# （`services/xxx.py` → `parents[1]`，而 `services/ai/xxx.py` 要用 `parents[2]`），
# 原样搬过去会静默指到 `services/` —— 表现是「平台内置 skill 缺失」，而不是路径写错。
# 它只被本文件的 `_load_project_skills` 用，所以留在原地。
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_TEMPLATE = """以下内容用于补充平台内置的分析协议。

内置协议（检查维度、输出格式、证据与置信度门槛、反误报条款）由平台强制提供，
请**不要在这里重复，也不要试图放宽**它们。这里只写本项目特有的事实与偏好。

请按下面几项填写；没有把握的**留空即可，不要编造** —— 模型会把缺失当作
「信息缺口」标出来，而编造出来的事实会被当成真的。

1. 技术栈与工程结构
   （例：Unity + C# + Lua；配表放在 <哪个仓库或目录> 下，生成物形如 <产物文件名>）
2. 配表规范要点
   （例：ID 的编号规则与号段划分：共几位、哪几位表示什么；能分表则分表）
3. 重点模块（这些模块的改动需要压测或完整回归）
   （例：登录、充值、匹配、战斗、邮件、排行榜、全服推送）
4. 本项目的红线与历史高频事故
5. 输出偏好
   （例：风险点请附复现步骤与影响范围；报告控制在 800 字内）
"""
# 关键路径的模式与「重点表名」都不再是这里的模块常量：前者是平台默认值（可被项目覆盖），
# 后者是项目自己声明的事实。两者都搬去了 `services/ai/project_facts.py` —— 留在这里
# 就等于留了第二份事实源，而它当年正是「写错了也没人知道」的那一份。


def _select_listed_files(
    delta_files: List[dict], max_files: int
) -> tuple[List[dict], bool]:
    """决定变更清单里**列哪些文件**，返回 (清单, 是否截断)。

    ## 为什么默认全列

    清单里一行就是一个路径（`  - [M] code/qz_pub/xxx/SeasonRankCfgMod.lua`，约 60 字符），
    **便宜**；而它换来的是模型能自己判断「该看什么」。实测线上那一轮 767 个文件的
    全量清单是 45,777 字符，只占提示词预算的一小部分 —— 也就是说绝大多数版本都会走到
    「全列」这一支，取样只是兜底。

    以前 200 这个上限同时管着「列多少」和「能读多少」，代价是没列出来的 567 个文件
    连 `file_diff` 都被拒。现在两者分开了：白名单给全部改动文件，**这里只影响名字列不列**。
    所以即使退化到取样，模型仍然能靠 `commit_detail` 查出提交的完整文件清单再点名索取
    （`render_change_summary` 的截断说明里写清了这条路）。
    """
    total_chars = sum(len(str(item.get("file_path") or "")) + 10 for item in delta_files)
    if total_chars <= MAX_LIST_CHARS:
        return list(delta_files), False
    sampled = _sample_with_repo_fairness(delta_files, max_files)
    return sampled, len(sampled) < len(delta_files)


def build_commit_payload(commit_id: int) -> dict:
    commit = Commit.query.get_or_404(commit_id)
    repo = db.session.get(Repository, commit.repository_id)
    project_id = repo.project_id if repo else None
    config = get_project_analysis_config(project_id) if project_id else None
    payload = {
        "mode": "commit",
        "scope": "full",
        "execution": {
            "version": EXECUTION_VERSION,
            "streaming_preferred": True,
        },
        "prompt": config["prompt_template"] if config else DEFAULT_PROMPT_TEMPLATE,
        "commit": {
            "id": commit.id,
            "commit_id": commit.commit_id,
            "path": commit.path,
            "operation": commit.operation,
            "author": commit.author,
            "message": commit.message,
            "commit_time": commit.commit_time.isoformat() if commit.commit_time else None,
        },
        "repository": {
            "id": repo.id if repo else None,
            "name": repo.name if repo else None,
            "resource_type": getattr(repo, "resource_type", None) if repo else None,
        },
    }
    return payload


def _resource_type_of(repo) -> str:
    """仓库的资源类型，**空值按「代码」算**。

    `models/repository.py` 写明取值是 `'table' / 'res' / 'code'`，而这一列**可空**
    （写入侧还有一条裸赋值会写进 NULL）。界面上那一栏的选项是这么分的：

        {% if (cfg.repository.resource_type or 'code') == 'table' %}…配表…{% else %}…代码…

    也就是 **NULL 与 `'res'` 都算代码仓库**。这里原先回的是空串，而调用方拿它去比
    `== "code"` —— `"" != "code"`，于是用户选「只看代码仓库」时，那些 `resource_type`
    为空的仓库的改动**一条都不会进输入**，而报告上写着「仅代码仓库」，模型据此把
    一个缺口说成覆盖完整。两侧必须同一套判据。
    """
    kind = str(getattr(repo, "resource_type", "") or "").strip().lower()
    return kind or "code"


def _filter_delta_files_by_focus(
    delta_files: List[dict], focus: Optional[str], configs: List[WeeklyVersionConfig]
) -> Tuple[List[dict], str]:
    """按用户选的「分析范围」筛文件，返回 (筛选后的清单, 给人看的范围名)。

    ## 为什么要有这个

    一个 767 个文件的版本，**人比任何自动策略都清楚这周该看哪一半**：这周改的是配表数值，
    下几周才轮到代码。让用户先选范围，比在服务端猜「哪 200 个更重要」准得多，而且成本是
    线性的（筛选之后清单短了、额度也集中了）。

    取值：`all`（或空）不筛；`table` / `code` 按仓库的 `resource_type` 筛；数字按
    仓库 id 筛。**认不出来的一律不筛**（`focus` 是 URL 参数，不能让它把分析变成空跑）。
    """
    text = str(focus or "").strip().lower()
    if not text or text == FOCUS_ALL:
        return list(delta_files), ""

    repo_by_id = {cfg.repository_id: cfg.repository for cfg in configs}

    if text in ("table", "code"):
        # **只有 `'table'` 算配表**，其余（含 `'res'` 与空值）都算代码 —— 与模板里
        # 「`resource_type or 'code'` 是否等于 `'table'`」那三行是同一套判据。
        # 判据分叉的后果见 `_resource_type_of`：选「只看代码仓库」会静默吞掉老仓库。
        want_table = text == "table"
        kept = [
            item for item in delta_files
            if (_resource_type_of(repo_by_id.get(item.get("repository_id"))) == "table")
            is want_table
        ]
        label = "仅配表仓库" if want_table else "仅代码仓库"
        return kept, label

    try:
        repo_id = int(text)
    except (TypeError, ValueError):
        return list(delta_files), ""

    repo = repo_by_id.get(repo_id)
    if repo is None:
        return list(delta_files), ""
    kept = [item for item in delta_files if item.get("repository_id") == repo_id]
    return kept, f"仅仓库「{repo.name}」"


def build_weekly_payload(
    config_id: int,
    *,
    force_full: bool = False,
    focus: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[AiWeeklyAnalysisState], Optional[str]]:
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    configs = WeeklyVersionConfig.query.filter(
        WeeklyVersionConfig.project_id == config.project_id,
        WeeklyVersionConfig.start_time == config.start_time,
        WeeklyVersionConfig.end_time == config.end_time,
    ).order_by(WeeklyVersionConfig.repository_id.asc()).all()
    if not configs:
        return None, None, "no_configs"

    group_key = build_weekly_group_key(config)
    base_name = _resolve_base_name(config)
    project_config = get_project_analysis_config(config.project_id)
    state = AiWeeklyAnalysisState.query.filter_by(group_key=group_key).first()
    last_analyzed_at = None if force_full else (state.last_analyzed_at if state else None)

    summary, details, skip_reason = _summarize_weekly_files(configs, last_analyzed_at)
    if skip_reason and not force_full:
        return None, state, skip_reason

    scope, policy = _decide_scope(summary, last_analyzed_at)
    max_files = int(project_config.get("max_files_per_run") or MAX_FILES_DEFAULT)

    repo_details = details.get("repos", [])
    repo_details.sort(key=lambda item: (item.get("priority", 1), item.get("repository_name", "")), reverse=True)

    delta_files = details.get("delta_files", [])
    focus_label = ""
    if focus:
        delta_files, focus_label = _filter_delta_files_by_focus(delta_files, focus, configs)
        if not delta_files:
            return None, state, "focus_empty"
        # **计数要跟着筛选走**（三个都跟）：不跟的话提示词会告诉模型「本次变更共 767 个
        # 文件」而它只看得到 19 个 —— 「把清单当全量」的镜像错误，这次是把全量说大了。
        summary = {
            **summary,
            "total_files": len(delta_files),
            "delta_files": len(delta_files),
            "batch_files": len(delta_files),
        }
    list_files, list_truncated = _select_listed_files(delta_files, max_files)

    truncated = list_truncated
    payload = {
        "mode": "weekly",
        "scope": scope,
        "execution": {
            "version": EXECUTION_VERSION,
            "streaming_preferred": True,
        },
        "prompt": project_config.get("prompt_template") or DEFAULT_PROMPT_TEMPLATE,
        "policy": {
            "reason": policy,
            "max_files": max_files,
            "sample_strategy": "priority_then_commit_count",
            "allow_cross_file": True,
        },
        # 用户选的「分析范围」。`label` 会被渲染进提示词（见 change_set._scope_note），
        # 所以报告与界面都能说清「这次只看了一半」。
        "focus": {"key": str(focus or FOCUS_ALL), "label": focus_label},
        "group": {
            "key": group_key,
            "base_name": base_name,
            "project_id": config.project_id,
            # 这一组都有哪些配置。**不是给模型看的**：它是「这份快照由哪些配置的缓存行
            # 组成」的账，`_update_weekly_state` 用它算快照指纹
            # （`scope_sampling.weekly_snapshot_digest`），调度器据此拦掉「输入一字未变
            # 却还要再分析一遍」。也顺便让落库的 request_payload 自己说清楚覆盖了谁。
            "config_ids": [item.id for item in configs],
            "start_time": config.start_time.isoformat() if config.start_time else None,
            "end_time": config.end_time.isoformat() if config.end_time else None,
        },
        "summary": summary,
        "repositories": repo_details,
        # 白名单：本批次**全部**改动过的文件。模型能读的 diff 就是这个集合。
        "delta_files": delta_files,
        # 提示词里**列出来**的那部分。绝大多数版本与 `delta_files` 相同（见下面的说明）。
        "list_files": list_files,
        "delta_truncated": truncated,
    }
    payload["policy"]["truncated"] = truncated
    if truncated:
        payload["policy"]["truncation_reason"] = "token_budget"
    return payload, state, None


def _load_project_skills(project_id: int) -> Tuple[object, str]:
    """加载平台 skill 与项目知识包。返回 `(loaded, 失败原因)`，成功时原因是空串。
    **加载失败不阻断分析**，但原因要交出去：成因都在用户自己能改的地方，只写日志等于让他猜。"""
    project = db.session.get(Project, project_id)
    code = getattr(project, "code", None) if project else None
    try:
        return load_skills(_REPO_ROOT, project_code=code), ""
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ AI 分析：skill 加载失败（project={project_id}）: {exc}")
        return None, describe_load_error(exc, _REPO_ROOT)


def _apply_model_window(
    client: object, project_config: dict, limits: EngineLimits, *, platform_chars: int = 0
) -> Tuple[EngineLimits, str]:
    """按模型窗口的水位压提示词预算。返回 `(额度, 说明)`。

    窗口**尽量向端点问**（`/v1/models` 里有时会声明）：端点不支持列模型、没声明窗口、
    或者报了个不合理的值，就按 `budget.DEFAULT_CONTEXT_TOKENS`（1M）这个**口径值**处理，
    并在说明里写明「按默认值处理」——**不能让人以为平台问到了**，那是假设不是事实。

    压到「窗口 × 60%」，理由与取值见 `budget.effective_prompt_budget`：以前的规则是
    「只在按 1:1 算也超窗时才压」，而窗口问不到时它什么都不做 —— 于是一个把预算配到
    2M 字的项目会拿一份 2M 字符的提示词去撞模型，必然被拒，整次分析连结论一起作废。

    ## 水位是压**整份提示词**的，所以 `platform_chars` 要参与

    进到 `limits.prompt_char_budget` 的那个数已经是「配置值 + 平台内置提示词」
    （见调用处）。窗口管的是整份提示词的总长，所以先把总数压到水位，再把内置那一段
    减掉、还给用户的部分 —— 否则内置提示词会被算两次：一次在水位里、一次在加法里。

    说明为空表示没压（默认预算加内置那段之后仍低于「窗口问不到」时的水位）。
    """
    model = str(project_config.get("api_model") or "").strip()
    window: object = None
    if model:
        contexts = getattr(client, "model_contexts", None)
        if contexts is not None:
            try:
                window = contexts().get(model)
            except LLMError as exc:
                # 问不到窗口不是失败点：下面按默认窗口继续（见 docstring）。
                log_print(f"AI 分析：取模型上下文窗口失败（按默认窗口处理）: {exc}")

    budget, note = effective_prompt_budget(limits.prompt_char_budget, window)
    if not note:
        return limits, ""
    # 压完再减去内置那一段：**用户的额度不能被平台自己的提示词吃掉**。窗口小到连
    # 内置提示词都装不下时落到 0 —— 那时组装侧还有条目下限兜着，而这一轮的说明已经
    # 把「窗口太小」讲清楚了。
    return replace(limits, prompt_char_budget=max(0, budget - platform_chars)), note


def _configured_int(value: object, default: int) -> int:
    """项目配置里的一项 → 整数。**只有「没填过」才回落到默认值。**

    没填过读出来是 `None`（配置行是懒创建的），也可能是一串空白 —— 这两种回落到默认值。
    **但 `0` 是用户填的值**：`max_tool_requests` 的范围就是 `0..100`，而平台为 0 专门写了
    两句话（`prompt._budget_line` 与 `protocol.build_budget_exhausted_hint`）。这里原先三处
    都写 `or`，`0 or 40` 求值成 40 —— 那两句因此永远执行不到，模型照常读 40 份 diff 并计费。
    与 `endpoint_service` 的「刻意不把空串当成 0」是同一条纪律的两个方向。
    """
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return int(value)


def _engine_limits(project_config: dict, *, platform_chars: int = 0) -> EngineLimits:
    """项目配置 → 引擎额度。

    `platform_chars` 是**平台内置提示词**的字符数（`prompt.platform_prompt_chars`）。
    配置里那一栏是给**用户内容**的额度 —— 变更清单、取回的上下文、历史结论基线、以及
    项目自己的知识包与补充指令；内置那一段由平台出，加在上面。少于这个数，一个只想
    给 100k 的项目会连带被内置提示词吃掉十几 k 的上下文额度（而它并不知情）。

    **项目自定义的 skill（知识包、补充指令）不走这个加法**：它们在系统提示词里，仍然
    从用户额度里扣 —— 那是用户自己要带的内容。
    """
    defaults = EngineLimits()
    requests = _configured_int(
        project_config.get("max_tool_requests"), defaults.max_tool_requests
    )
    return EngineLimits(
        max_rounds=_configured_int(
            project_config.get("max_analysis_rounds"), defaults.max_rounds
        ),
        max_tool_requests=requests,
        # 条数上限**不得小于**索取次数：小于就会出现「付了 N 次索取、只带走 max_items 条」
        # —— 取回来的上下文被 `enforce_budget` 按条数静默裁掉，白花额度（见
        # `context_tools.DEFAULT_MAX_TOOL_REQUESTS` 的说明）。索取次数是用户可配的
        # （取值上限 100），所以这个下限必须跟着**配置**走，只在两个默认值上成立是不够的：
        # 用户把索取上限调到 40 的那一刻，20 条的条数上限就会开始丢他的东西。
        max_items=max(defaults.max_items, requests),
        prompt_char_budget=max(0, platform_chars)
        + _configured_int(
            project_config.get("prompt_char_budget"), defaults.prompt_char_budget
        ),
    )


# 基线的读侧（`previous_run` / `baseline_findings` / `baseline_digest` / `suppressed`）
# 搬去了 `services/ai/baseline_source.py`：它们只依赖 run / anomaly 两张表与纯函数那一层
# `baseline.py`，与「怎么跑一次分析」没有耦合，而这个文件贴着 2000 行的硬上限。
# 这里保留同名引用，下面两处调用点与既有测试的 import 都不用改。


def _persist_outcome(
    run: AiAnalysisRun,
    outcome: EngineOutcome,
    result: dict,
    *,
    pricing_version: str = "",
) -> None:
    """把引擎结果落库：run 上写状态与账目，逐轮写 trace，逐条写异常。

    `error_message` 这一列此前**从来没有被写入过**，于是失败的分析在界面上永远是
    「分析中」，用户无从判断。这是这次要修的一部分。

    ## 为什么这里要把「账」写全

    这批列（`tool_requests_used` / `context_chars` / `anomalies_found` / `dropped_count`
    与新的用量列）此前**存在但从来没被写过**，一直是 NULL —— 于是「这次花了多少、
    上下文塞了多少、丢了几条」在库里根本没有记录，面板再怎么做也没有数据可读。
    写全它们才算把采集侧接上。

    `pricing_version` 由调用方传（它在建引擎时已经读过项目配置，见 `_execute_analysis`），
    这里不为了一个版本号再查一次库。
    """
    run.status = "failed" if outcome.status == STATUS_FAILED else "succeeded"
    run.finished_at = _utcnow()
    # 结论形态：模型按协议给了结构化结论 → True；只有一份 markdown 报告 → False；
    # 失败 → None（下面那支会把结论字段清空，本来就没有结论）。
    # 判据是 `outcome.payload` 而不是 `outcome.status`：降级分两种，有 payload 的那种
    # （轮次/额度/上下文用尽）结论仍然是结构化的，只是浅；没有 payload 的那种
    # （`DEGRADE_MARKDOWN`）一条结构化结论都没有。**只有后者不能当基线**。
    run.conclusion_structured = bool(outcome.payload is not None) if run.status == "succeeded" else None
    if run.status == "failed":
        # 失败**不写结论字段**。以前这里照样写 response_payload / response_text，
        # 于是库里那条失败记录长得和成功记录一样：有「结论」、有风险等级、有范围，
        # 前端只判「有没有结果」就把徽章显示成「已有结果」并附上「风险等级 high」。
        # 而那个 high 根本不是模型给的 —— 是 services/ai/result_payload.py 的 determine_risk_level 按变更规模
        # （total_files >= 120）估出来的兜底值，模型压根没答上来。
        # 失败应当只留错误，让「有没有成功结论」这件事在数据层就无歧义。
        run.response_payload = None
        run.response_text = ""
    else:
        run.response_payload = _json_dumps(result)
        run.response_text = outcome.report_markdown or outcome.error_message or ""
    run.rounds_used = outcome.rounds_used
    run.tokens_input = outcome.prompt_tokens
    run.tokens_output = outcome.completion_tokens
    # 用量：`None`（上游没报）原样落 NULL，**不许写成 0** —— 读取侧就是靠 NULL 与 0
    # 的差别来区分「未上报」与「确实没命中」的（见 services/ai/usage.py）。
    run.cache_read_tokens = outcome.cache_read_tokens
    run.cache_write_tokens = outcome.cache_write_tokens
    run.cache_source = outcome.cache_source or None
    run.duration_ms = outcome.duration_ms
    run.tool_requests_used = outcome.requests_used
    run.context_chars = outcome.context_chars
    run.tool_stats_json = encode_tools(outcome.tool_stats)
    run.anomalies_found = len(result.get("anomalies") or [])
    run.dropped_count = len(outcome.dropped)
    run.pricing_version = pricing_version or None
    run.error_message = outcome.error_message or None
    # 子代理模式：这次是按分片跑的，就如实记下来（没开时两列是 NULL = 老行为）。
    # 落在这里而不是 `result` 里：它是**这一次运行怎么跑的**，与「结论是什么」无关。
    mode = subagent_mode_of(outcome)
    run.subagent_mode = mode[0] if mode else None
    run.subagent_count = mode[1] if mode else None

    for record in outcome.rounds:
        db.session.add(
            AiAnalysisTrace(
                run_id=run.id,
                round_index=record.index,
                # 这一轮是哪个分片代理跑的（NULL = 主代理自己那几轮，也是所有老行的情形）。
                # `round_index` 是家族内全局序号，成员内部的轮次另存在 `agent_round` 里。
                agent=record.agent or None,
                agent_round=record.agent_round or None,
                outcome=record.status,
                parsed_ok=record.status != "unparsable",
                # 这一轮要了什么、拿到了什么、丢了什么、模型原样返回了什么 —— 编码只在
                # `trace_evidence` 一处（写库侧与读库侧共用），这里不拼 JSON：
                # 原先只写计数，于是「取数失败」与「真的读了一份 diff」在面板上长得一样。
                **encode_evidence(record),
                error=record.note or None,
                # 逐轮用量。这几列同样一直是 NULL：没有它们，「钱花在第几轮」答不上来，
                # 而提示词每轮都把上一轮的上下文重发一遍，后几轮才是贵的那些。
                tokens_input=record.prompt_tokens,
                tokens_output=record.completion_tokens,
                cache_read_tokens=record.cache_read_tokens,
                cache_write_tokens=record.cache_write_tokens,
                request_chars=record.prompt_chars,
                context_chars=record.context_chars,
                duration_ms=record.duration_ms,
            )
        )

    for item in result.get("anomalies") or []:
        db.session.add(
            AiAnalysisAnomaly(
                run_id=run.id,
                project_id=run.project_id,
                fingerprint=item["fingerprint"],
                title=item["title"] or "（无标题）",
                category=item["category"],
                severity=item["severity"],
                confidence=item["confidence"],
                evidence=_json_dumps(item["evidence"]),
                commit_ref=item["commit_ref"],
                file_path=item["file_path"],
                impact=item["impact"],
                suggestion=item["suggestion"],
            )
        )

    db.session.commit()


def _create_run(
    *,
    project_id: int,
    target_type: str,
    target_id: int,
    target_key: Optional[str],
    response_mode: str,
    scope: str,
    trigger_source: str,
    payload: dict,
) -> AiAnalysisRun:
    """建一条 running 的 run 记录。

    **先落库再跑**：分析要花几十秒到几分钟，这期间界面上必须能看到「正在分析」。等跑完
    才写库的话，用户在这段时间里看到的是一片空白，看起来像按钮没生效。

    溯源四个值（prompt / skill / rules / model）在这里一次写全：它们同时是缓存键，
    缺一个就会出现「改了 skill 却还在复用旧结论」——那个坑已经踩过一次。
    """
    summary = payload.get("summary") or {}
    run = AiAnalysisRun(
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target_key=target_key,
        status="running",
        response_mode=response_mode,
        scope=scope,
        trigger_source=trigger_source,
        trace_id=f"{target_type}-{target_id}-{int(_utcnow().timestamp())}",
        **current_provenance(project_id),
        request_payload=_json_dumps(payload),
        delta_summary=_json_dumps(
            {
                "delta_files": summary.get("delta_files", 0),
                "total_files": summary.get("total_files", 0),
                "scope": scope,
            }
        ),
        started_at=_utcnow(),
    )
    db.session.add(run)
    db.session.commit()
    return run


def _execute_analysis(
    run: AiAnalysisRun,
    *,
    project_id: int,
    payload: dict,
    project_config: dict,
    target_type: str,
    target_key: Optional[str],
) -> dict:
    """跑一次真实分析并把结果落库。**不抛异常** —— 失败要变成带原因的结论。

    调用方在后台线程或 SSE 生成器里跑，抛出去只会变成一个没人看的堆栈，而用户那边
    是「分析中」永远转下去。

    这层是**薄壳**：真正的活在 `_run_engine_and_persist` 里，这里只负责「跑完之后一定
    要把进度快照清掉」。清不掉的后果是界面一直显示「正在跑」—— 那条进程内的表没有
    别的清理时机（`run_progress` 的过期时限只是兜底）。

    ## 「不抛异常」这句承诺是**这一层**的责任，`_run_engine_and_persist` 不保证它

    上面那句承诺此前只有 `try/finally` 兜着，一个 `except` 都没有 —— 也就是
    「不抛异常」靠的是「下游一处都不会抛」这个假设。而下游做的事包括：读配置、
    解 JSON、连模型、写库。任何一处没想到的异常都会**带着「运行号早就发给界面了」**
    穿出生成器：SSE 在浏览器那头表现为连接静默断开，界面只剩一句
    「AI 分析失败或连接中断」，而真正的原因只留在服务端的日志里，
    库里那条 run 还停在 `running`（一小时后才被 `effective_status` 翻成失败）。

    用户报的正是这一幕。所以凡是**已经有运行号**的失败，都必须变成带原因的结论：
    在这里兜住，把原因写进 `error_message` 落库，界面就能照常渲染出「失败 + 原因」。
    """
    try:
        return _run_engine_and_persist(
            run,
            project_id=project_id,
            payload=payload,
            project_config=project_config,
            target_type=target_type,
            target_key=target_key,
        )
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：这一层的承诺就是这一句
        return _record_unexpected_failure(run, payload, project_config, exc)
    finally:
        clear_run_progress(run.id)


def _record_unexpected_failure(
    run: AiAnalysisRun, payload: dict, project_config: dict, exc: Exception
) -> dict:
    """把「跑的过程中冒出来的异常」写成一条失败结论（并落库）。

    **它自己也不许抛**：调用它的时候现场已经出过一次事故了，再抛一次就是
    「连失败都报不出来」。所以落库那一步单独兜一层，失败只写日志。
    """
    message = f"分析中断：{type(exc).__name__}: {exc}"
    log_print(f"❌ AI 分析异常中断 {message}（run={run.id}）", "AI", force=True)

    try:
        summary = payload.get("summary") or {}
    except Exception:  # noqa: BLE001 —— payload 形态异常时别把收尾也弄挂
        summary = {}

    try:
        # 异常很可能来自一次失败的写库（列超长、连接断了…），会话此时是脏的，
        # 不回滚的话下面这句 `_persist_outcome` 会直接 PendingRollbackError ——
        # 那就成了「失败的原因报不出来，只报得出回滚失败」。
        db.session.rollback()
    except Exception as rollback_exc:  # noqa: BLE001
        log_print(f"⚠️ AI 分析：回滚会话失败（{rollback_exc}），继续尝试落库失败结论", "AI")

    result = failed_result(summary, message)
    try:
        _persist_outcome(
            run, engine_failed(message), result,
            pricing_version=_price_version_for(project_config),
        )
    except Exception as persist_exc:  # noqa: BLE001
        log_print(
            f"❌ AI 分析：失败结论也没能落库（run={run.id} {type(persist_exc).__name__}: "
            f"{persist_exc}），库里这条运行会停在 running",
            "AI",
            force=True,
        )
    return result


def _run_engine_and_persist(
    run: AiAnalysisRun,
    *,
    project_id: int,
    payload: dict,
    project_config: dict,
    target_type: str,
    target_key: Optional[str],
) -> dict:
    summary = payload.get("summary") or {}

    # 正式分析用配置里的单次请求超时（默认 300 秒，界面可改），**不是**探测用的 30 秒。
    # 这两者混用会让分析必然超时，见 build_endpoint_client 的说明。
    configured_timeout = get_project_analysis_config(project_id).get("request_timeout_seconds")
    client, errors = build_endpoint_client(
        project_id, {}, timeout_seconds=_coerce_timeout(configured_timeout)
    )
    if client is None:
        message = "接口配置不完整：" + "；".join(item["message"] for item in errors)
        result = failed_result(summary, message)
        _persist_outcome(
            run, engine_failed(message), result,
            pricing_version=_price_version_for(project_config),
        )
        return result

    loaded, skill_failure = _load_project_skills(project_id)
    if loaded is None:
        message = skill_failure or "分析协议（skill）加载失败，未发起分析。"
        result = failed_result(summary, message)
        _persist_outcome(
            run, engine_failed(message), result,
            pricing_version=_price_version_for(project_config),
        )
        return result

    readable = sorted(getattr(loaded, "readable", {}) or {})
    # 生成物前缀是**项目事实**：由项目知识包的声明决定，没有声明时用平台默认值。
    # 从 `loaded.project_slug` 取而不是再查一次项目：slug 就是加载器刚刚用的那一个，
    # 两处各算一遍迟早会在「项目代号里有怪字符」这种边界上分叉。
    prefixes = generated_prefixes(getattr(loaded, "project_slug", None))
    if prefixes.warning:
        log_print(f"⚠️ AI 分析：生成物前缀声明有问题 —— {prefixes.warning}", "AI", force=True)
    change = (
        from_commit_payload(payload, readable_references=readable, prefixes=prefixes)
        if payload.get("mode") == "commit"
        else from_weekly_payload(payload, readable_references=readable, prefixes=prefixes)
    )

    # 配置里那个「提示词字符预算」是**用户内容**的额度，不含平台内置提示词（见
    # `prompt.platform_prompt_chars` 的说明）。这里把内置那一段加回去，让引擎按
    # 「整份提示词」去组装与压缩；项目知识包、补充指令不在这个加法里 —— 它们在系统
    # 提示词里，仍然从用户的额度里扣。
    platform_chars = platform_prompt_chars(loaded)
    limits, budget_note = _apply_model_window(
        client, project_config, _engine_limits(project_config, platform_chars=platform_chars),
        platform_chars=platform_chars,
    )
    if budget_note:
        log_print(f"⚠️ AI 分析：{budget_note}", "AI", force=True)
    # 这一份参数**两条路共用**（单代理 / 子代理），键名就是 `engine.run_analysis` 的形参名。
    # 分成两处各写一遍的话，14 个参数里总会有一处漏掉，而漏掉的那个是静默的。
    engine_args = {
        "client": client,
        # 周版本批次分析读平台**已算好并落库**的那一份合并 diff（就是周版本页面读的
        # 同一个 payload），而不是现场用本地工作副本重算 —— 见
        # `platform_provider._weekly_stored_diff`。单提交分析要关掉它：那时模型问的是
        # 「这一条提交改了什么」，而缓存里那一份覆盖的是一个窗口（可能好几条提交）。
        # 子代理模式下**所有成员共用一个 provider**：它的记忆（agent 取回的正文与差异）
        # 因此也共享，N 个成员不会把同一份内容取 N 遍。
        "provider": PlatformContextProvider(
            # `scope` 只有 `find_references` 用：那个工具要知道「本批次改了哪些文件」，
            # 而这正是 scope 才知道的事（工具本身不带 commit，见 ai/reference_search.py）。
            loaded=loaded, scope=change.scope,
            use_stored_batch_diff=(payload.get("mode") != "commit"),
        ),
        "loaded": loaded,
        "scope": change.scope,
        "change_summary": change.summary,
        "limits": limits,
        "thresholds": RuleThresholds.from_config(project_config),
        "project_knowledge": project_config.get("project_knowledge") or "",
        "project_instructions": project_config.get("prompt_template") or "",
        "baseline_digest": _baseline_digest(target_type, target_key, change),
        # 每跑完一轮把累计用量写进进程内的进度快照（services/ai/run_progress.py，界面
        # 一边跑一边轮询它）。**它是给界面看的一眼，不是账** —— 账在 `_persist_outcome`。
        "on_round": lambda progress: publish_run_progress(run.id, project_id, progress),
        # 第一次模型调用**之前**也报一帧。没有它，从开跑到第一轮跑完之间（可以是几分钟）
        # 快照是空的，界面显示「分析中：进度不可用」，而结论面板一直挂着
        # 「AI 分析进行中...」—— 看起来像卡住了，实际它正在干活。子代理模式下它还负责
        # 把归属从上一个分片换成汇总/主代理（见 subagent._call_engine 的 report）。
        "on_start": lambda progress: publish_run_progress(run.id, project_id, progress),
    }
    # 子代理模式（services/ai/subagent.py）：默认关、只对周版本生效，不适用时返回 None
    # 走原来的单代理路径。`verify` 是对账轮，它依附在子代理上 —— 没开子代理时不生效。
    plan = plan_family(
        mode=payload.get("mode") or "",
        enabled=bool(project_config.get("subagent_enabled")),
        count=project_config.get("subagent_count") or 0,
        verify=bool(project_config.get("subagent_verify")),
        limits=limits,
    )
    outcome = (
        run_analysis(**engine_args)
        if plan is None
        else run_family_with_seed(
            plan=plan,
            # 每片开跑前看一眼预算（含本次已消耗的）：判据与起跑闸门同一个 `budget_status`，
            # 只是多算了这一家子已花掉的 token —— 否则前面几片的花费还没落库，每一片都
            # 看到「还没超」。被跳过的分片会进报告的信息缺口。
            should_skip=early_stop_guard(project_id, entry="subagent"),
            **engine_args,
        )
    )

    result = result_payload(
        outcome,
        payload,
        suppressed=_suppressed(target_type, target_key, change),
        context_budget_note=budget_note,
    )
    _persist_outcome(
        run, outcome, result, pricing_version=_price_version_for(project_config)
    )
    return result


def end_with_a_terminal_event(events: Iterable[str]) -> Iterable[str]:
    """把一串 SSE 事件包成「**一定以 `result` 或 `error` 收尾**」的一串。

    ## 为什么需要这一层（它拦的是哪一段）

    `_execute_analysis` 已经承诺不抛异常，但一次流式分析里**不止**那一件事会抛：
    建运行记录之前的 payload 构造、跑完之后推进周版本水位线、甚至把结论转成 JSON。
    任何一处抛出去，生成器就结束了（WSGI 只是把连接关掉）—— 浏览器那头是
    `EventSource` 收到一个**没有 `data` 的 `error` 事件**，而界面上没有 `data` 的
    `error` 只能说出那句含糊的「AI 分析失败或连接中断」，原因一个字都不在里面。
    用户报了这句话，这就是它的来源之一。

    所以这里把「兜底收尾」放在**流的最外层**：还活着就把原因发出去。它不需要知道
    运行号 —— 走到这里时运行记录要么还没建（那就没有任何要修正的库状态），
    要么结论早已落库（`_persist_outcome` 在 `result` 之前就写完了）。

    顺带钉住一件更容易被忽略的事：**正常跑完的流不许被这层改动**。它只在异常路径上
    追加事件，成功路径逐字透传（`return` 之后不能再 yield —— 那会给已经收尾的流
    补一个 `error`，把一次成功的分析显示成失败）。
    """
    try:
        yield from events
    except Exception as exc:  # noqa: BLE001 —— 这一层的存在意义就是兜住任何异常
        message = f"分析中断：{type(exc).__name__}: {exc}"
        log_print(f"❌ AI 分析的 SSE 流异常中断（{message}），已把原因发给界面", "AI", force=True)
        yield _sse_event("error", {"message": message})


def stream_commit_analysis(commit_id: int, user_label: str = "") -> Iterable[str]:
    commit = Commit.query.get_or_404(commit_id)
    repo = db.session.get(Repository, commit.repository_id)
    project_id = repo.project_id if repo else None
    if not project_id:
        yield _sse_event("error", {"message": "Project not found."})
        return

    cached_run = (
        AiAnalysisRun.query.filter_by(target_type="commit", target_id=commit_id)
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )
    if _is_run_fresh(cached_run):
        yield from _stream_cached_run(cached_run)
        return
    if not _get_project_api_key(project_id):
        yield _sse_event("error", {"message": "Project API key not configured."})
        return

    # 预算闸门（手动·单提交）。放在缓存复用之后：回放一份已有结论不花钱，不该被拦；
    # 也放在真正开跑之前：超预算就不发起任何请求。
    budget_reason = budget_gate_reason(project_id, entry="commit_manual")
    if budget_reason:
        yield _sse_event("error", {"message": budget_reason})
        return

    payload = build_commit_payload(commit_id)
    run = _create_run(
        project_id=project_id,
        target_type="commit",
        target_id=commit_id,
        target_key=None,
        response_mode="streaming",
        scope="full",
        trigger_source="manual",
        payload=payload,
    )
    # **开跑之前**先把运行号发出去：界面要按它轮询「跑到第几轮、现在超了没」
    # （`/ai-analysis/runs/<id>/progress`），而分析是阻塞跑的 —— 等结束再给号，
    # 那个提示就只能在跑完之后才可能出现，也就没有意义了。
    yield _sse_event("run", {"run_id": run.id})

    result = _execute_analysis(
        run,
        project_id=project_id,
        payload=payload,
        project_config=get_project_analysis_config(project_id),
        target_type="commit",
        target_key=None,
    )
    for line in (result.get("report_markdown") or "").splitlines():
        yield _sse_event("chunk", {"text": line})
    # 带上 run_id：抽屉里那一行「本次消耗」的「明细」按钮要按**运行记录**取数
    # （`/ai-analysis/runs/<id>/usage`），而 SSE 的 result 事件只带结论本身。
    # 只加在事件上，不进 `response_payload` —— 那是落库的结论，不该混入运行身份。
    yield _sse_event("result", {**result, "run_id": run.id})


def stream_weekly_analysis(
    config_id: int, trigger_source: str = "manual", focus: Optional[str] = None
) -> Iterable[str]:
    payload, state, skip_reason = build_weekly_payload(config_id, focus=focus)
    if skip_reason == "no_change":
        cached = get_latest_weekly_result(config_id)
        if cached and cached.get("run_id"):
            run = db.session.get(AiAnalysisRun, cached["run_id"])
            if run and _is_run_fresh(run):
                yield from _stream_cached_run(run)
                return
        payload, state, skip_reason = build_weekly_payload(config_id, force_full=True, focus=focus)
    if skip_reason == "focus_empty":
        yield _sse_event(
            "error",
            {"message": "选定的分析范围里没有改动过的文件，换一个范围再试。"},
        )
        return
    if payload is None:
        yield _sse_event("error", {"message": "Weekly analysis payload not ready."})
        return

    project_id = payload["group"]["project_id"]

    # 预算闸门（手动·周版本）。与 commit 那条同理：缓存复用之后、真正开跑之前。
    budget_reason = budget_gate_reason(project_id, entry="weekly_manual")
    if budget_reason:
        yield _sse_event("error", {"message": budget_reason})
        return

    if not _get_project_api_key(project_id):
        yield _sse_event("error", {"message": "Project API key not configured."})
        return

    group_key = payload["group"]["key"]
    run = _create_run(
        project_id=project_id,
        target_type="weekly",
        target_id=config_id,
        target_key=group_key,
        response_mode="streaming",
        scope=payload.get("scope", "full"),
        trigger_source=trigger_source,
        payload=payload,
    )
    # 与单提交那条同理：运行号必须在**开跑之前**给出去，界面才能一边跑一边问进度。
    yield _sse_event("run", {"run_id": run.id})

    result = _execute_analysis(
        run,
        project_id=project_id,
        payload=payload,
        project_config=get_project_analysis_config(project_id),
        target_type="weekly",
        target_key=group_key,
    )
    for line in (result.get("report_markdown") or "").splitlines():
        yield _sse_event("chunk", {"text": line})

    _update_weekly_state(payload, run, state, engine_status=result.get("status"))
    # 带上 run_id：抽屉里那一行「本次消耗」的「明细」按钮要按**运行记录**取数
    # （`/ai-analysis/runs/<id>/usage`），而 SSE 的 result 事件只带结论本身。
    # 只加在事件上，不进 `response_payload` —— 那是落库的结论，不该混入运行身份。
    yield _sse_event("result", {**result, "run_id": run.id})


def run_weekly_analysis_background(config_id: int, task_id: Optional[int] = None) -> dict:
    # **执行前再查一次开关。** `schedule_weekly_ai_analysis_tasks` 在建任务时查过，
    # 但任务一旦入队就独立于开关了：关掉自动分析**不会**取消已经排队的任务，而重启时
    # `load_pending_tasks` 还会把上次残留的 `processing` 任务改回 `pending` 重新入队
    # （`task_worker_service.py:1273-1279`）。于是「关掉开关 + 重启」必定跑一次 ——
    # 这正是用户报的「停止了自动分析后仍然自动触发」。
    #
    # 闸设在这里是安全的：本函数是**后台路径的唯一入口**（两个调用点都在
    # `task_worker_service`），手动分析走的是 `stream_weekly_analysis`，不受影响。
    config = db.session.get(WeeklyVersionConfig, config_id)
    if config is not None:
        project_cfg = get_project_analysis_config(config.project_id)
        if not project_cfg.get("auto_weekly_enabled", True):
            log_print(
                f"周版本自动分析已关闭，跳过已排队的任务: config_id={config_id}", "AI", force=True,
            )
            return {"status": "skipped", "reason": "auto_weekly_disabled"}

        # 预算闸门（后台·周版本）。与开关那道闸同理放在这里：本函数是后台路径的
        # 唯一入口，闸设在这里既覆盖调度器，也覆盖「开关被关掉之前就已经排好队」的
        # 残留任务。**记成 skipped 并写日志**，不静默 —— 静默跳过的表现是
        # 「自动分析不跑了，但没有任何地方说为什么」。
        budget_reason = budget_gate_reason(config.project_id, entry="weekly_background")
        if budget_reason:
            log_print(
                f"周版本自动分析被预算拦截，跳过已排队的任务: config_id={config_id} "
                f"project={config.project_id} —— {budget_reason}",
                "AI",
                force=True,
            )
            return {"status": "skipped", "reason": "over_budget", "message": budget_reason}

        # 同步闸门（后台·周版本）。与上面两道同理放在这里：调度器那道拦的是「还没排队」，
        # 这道拦的是「已经排好队了」—— 重启时 `load_pending_tasks` 会把上次残留的
        # `processing` 任务改回 pending 再跑一次，那正好撞上启动期的同步。
        # **跳过且不推进水位线**：`last_analyzed_at` 不变，下一个调度周期会重新排队。
        sync_reason = weekly_sync_in_flight(group_config_ids(config))
        if sync_reason:
            log_print(
                f"周版本自动分析推迟（{sync_reason}）: config_id={config_id}",
                "AI",
                force=True,
            )
            return {"status": "skipped", "reason": "sync_in_flight", "message": sync_reason}

    payload, state, skip_reason = build_weekly_payload(config_id)
    if skip_reason == "no_change":
        cached = get_latest_weekly_result(config_id)
        if cached and cached.get("run_id"):
            run = db.session.get(AiAnalysisRun, cached["run_id"])
            if run and _is_run_fresh(run):
                return {"status": "skipped", "reason": "no_change"}
        payload, state, skip_reason = build_weekly_payload(config_id, force_full=True)
    if payload is None:
        return {"status": "skipped", "reason": "payload_empty"}

    project_id = payload["group"]["project_id"]
    if not _get_project_api_key(project_id):
        return {"status": "skipped", "reason": "missing_api_key"}

    group_key = payload["group"]["key"]
    run = _create_run(
        project_id=project_id,
        target_type="weekly",
        target_id=config_id,
        target_key=group_key,
        response_mode="blocking",
        scope=payload.get("scope", "full"),
        trigger_source="scheduled",
        payload=payload,
    )

    result = _execute_analysis(
        run,
        project_id=project_id,
        payload=payload,
        project_config=get_project_analysis_config(project_id),
        target_type="weekly",
        target_key=group_key,
    )

    _update_weekly_state(payload, run, state, engine_status=result.get("status"))
    return {
        "status": "succeeded" if result.get("status") != "failed" else "failed",
        "run_id": run.id,
        "error_message": result.get("error_message") or None,
    }


def _update_weekly_state(
    payload: dict,
    run: AiAnalysisRun,
    state: Optional[AiWeeklyAnalysisState],
    *,
    engine_status: Optional[str],
) -> None:
    """推进这个周版本分组的「分析水位线」。

    **只有真正跑完整了的 run 才推进。** `last_analyzed_at` 是增量分析的水位线
    （`_summarize_weekly_files` 用它筛 `updated_at > last_analyzed_at` 的文件），
    一次没跑完的分析如果把它推到当前时刻，那批变更就被整体判成「已看过」——
    下一次分析直接返回 no_change 静默跳过，用户再点多少次都跑不动。

    这里刻意**不看 `run.status`**：`_persist_outcome` 把 `degraded`（降级但有报告，
    例如「上下文索取额度用尽，基于已有证据出结论」）也存成 `"succeeded"`，所以
    `run.status` 分不出「跑完了」与「没跑完就出结论」。而 degraded 恰恰是水位线
    最不该推进的那一类：线上有一次 767 个文件里有 748 个 `.lua` 的 diff 根本没读到，
    却被标成「已分析」，增量从此只看得到水位线之后的新文件。

    判据用引擎侧的 `outcome.status`（`STATUS_SUCCEEDED` 才推进），
    所以这是个**必填的关键字参数** —— 将来新增调用点时，忘了传会直接报错，
    而不是悄悄退回一个分不出 degraded 的判据。
    """
    if engine_status != STATUS_SUCCEEDED:
        # **降级的 run 不推进时间水位线，但要留下内容指纹**（理由见
        # `_remember_snapshot_digest`）：否则同一份输入会被一遍遍重新分析。
        _remember_snapshot_digest(payload, state)
        return
    group = payload.get("group") or {}
    summary = payload.get("summary") or {}
    if not group:
        return
    if not state:
        # 并发首跑时「先查后插」必有一个输家（见 `get_or_create_weekly_state`）——
        # 这里原本就是那个写法，而它撞约束的代价是**结论落库了却交付不出去**。
        state = get_or_create_weekly_state(
            project_id=group.get("project_id"),
            group_key=str(group.get("key") or ""),
            base_name=str(group.get("base_name") or ""),
            start_time=_parse_iso_datetime(group.get("start_time")),
            end_time=_parse_iso_datetime(group.get("end_time")),
        )

    state.last_snapshot_digest = _snapshot_digest_for(payload) or state.last_snapshot_digest
    state.last_analyzed_at = _utcnow()
    state.last_analysis_run_id = run.id
    state.last_scope = run.scope
    state.last_summary = _json_dumps(summary)
    state.last_triggered_at = run.started_at or _utcnow()
    state.updated_at = _utcnow()
    db.session.commit()


def _snapshot_digest_for(payload: dict) -> str:
    """这次分析对应的快照指纹；算不出来返回空串（调用方保留原值）。"""
    group = payload.get("group") or {}
    if not group:
        return ""
    try:
        return weekly_snapshot_digest(list(group.get("config_ids") or []))
    except Exception:  # pragma: no cover - 指纹算不出来不该影响交付
        return ""


def _remember_snapshot_digest(payload: dict, state) -> None:
    """**降级路径**记下「这次分析的是哪一份快照」（成功路径在上面一起写）。

    为什么降级也要记：`last_analyzed_at` 只在跑完整了时推进是有意的（否则模型没真读到
    的变更会被标成「已看过」，线上真出过 767 个文件里 748 个没读到却被标成已分析），
    可这样一来「降级跑完 → 水位线不动 → 下个周期又判有新变化 → 同一份输入再分析一遍」
    就会一直转，每小时烧一次全量分析，而输入一字未变。

    指纹只写失败的日志、不抛：它只影响**下一次**要不要跳过，不影响这次的交付。
    """
    try:
        digest = _snapshot_digest_for(payload)
        if not digest:
            return
        group = payload.get("group") or {}
        if not state:
            state = get_or_create_weekly_state(
                project_id=group.get("project_id"),
                group_key=str(group.get("key") or ""),
                base_name=str(group.get("base_name") or ""),
                start_time=_parse_iso_datetime(group.get("start_time")),
                end_time=_parse_iso_datetime(group.get("end_time")),
            )
        state.last_snapshot_digest = digest
        state.updated_at = _utcnow()
        db.session.commit()
    except Exception as exc:  # pragma: no cover - 只影响下次是否跳过
        db.session.rollback()
        log_print(f"⚠️ AI 分析：记快照指纹失败（不影响本次交付）: {exc}", "AI", force=True)


def _parse_iso_datetime(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _focus_from_run(run: AiAnalysisRun) -> dict:
    """这次 run 用的「分析范围」。存在它自己的 request_payload 里。

    为什么要读出来给界面：结果是按范围筛过的，界面若只说「最近分析」而不说范围，
    用户会把「只看配表仓库」那份结论当成对全版本的结论 —— 与「把清单当全量」是同一类
    错误。老记录没有这个字段，读不到就按「全部」处理。
    """
    stored = _parse_response_payload(run.request_payload)
    focus = (stored or {}).get("focus") if isinstance(stored, dict) else None
    if isinstance(focus, dict):
        return {
            "key": str(focus.get("key") or FOCUS_ALL),
            "label": str(focus.get("label") or ""),
        }
    return {"key": FOCUS_ALL, "label": ""}


# 「这份结论不是最新那一次」的两种成因。它们要分开说，因为用户该做的事不一样：
# 前者要重新分析才有新规则下的结论，后者只要等/重跑上一次没跑完的那次。
STALE_REASON_RULES_CHANGED = "rules_changed"
STALE_REASON_INTERRUPTED = "interrupted"


def _latest_concluded_run(conditions) -> Optional[AiAnalysisRun]:
    """该目标最近一条**真的有结论**的成功记录。**刻意不看溯源。**

    溯源自（提示词 / skill / 规则 / 模型的版本号）只该决定「这份结论能不能拿来跳过
    重跑」—— 那是省一次调用的事；不该决定「这份结论还看不看得到」—— 那是用户以为
    自己的分析白做了的事。两者混用一把尺子的后果已经出现过：改过 `prompt.py` 或
    `SKILL.md` 之后，所有历史结论在界面上一起变成「未分析」，而结论一直躺在库里。
    """
    run = (
        AiAnalysisRun.query.filter(*conditions)
        .filter(AiAnalysisRun.status == "succeeded")
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )
    if run is None or not (run.response_text or run.response_payload):
        return None
    ts = run.finished_at or run.created_at
    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts < _analysis_cache_cutoff():
        return None
    return run


def _stale_note(reason: str, *, concluded: AiAnalysisRun, newest: AiAnalysisRun) -> str:
    """把「这份结论为什么不是最新那一次」写成给人看的一句话。

    在服务端写死一句话，而不是让三份抽屉模板各自拼：那三份是逐字复制的，让它们
    各自拼文案就是三处会各自演化 —— 而这个仓库里「同一句话在多处各自演化」正是
    反复出问题的地方（见 `static/js/ai_usage_line.js` 的存在理由）。
    """
    at = _created_at_display(concluded)
    if reason == STALE_REASON_RULES_CHANGED:
        return f"该结论由旧版评审规程产出（提示词/规则/模型已更新），以下是 {at} 的结论，仅供参考"
    return (
        f"最近一次分析未完成（{_created_at_display(newest)}，{newest.effective_status}），"
        f"以下是 {at} 的结论"
    )


def _read_latest_result(conditions, *, project_id: Optional[int] = None) -> Optional[dict]:
    """读侧的统一口径：**库里只要有结论，就必须看得见。**

    判定顺序（每一步都要能回答「用户接下来该做什么」）：

    1. 最新那条可用（`_is_run_fresh`：成功 + 有内容 + 未过期 + 溯源一致）→ 直接给；
    2. 最新那条正在跑（且不是僵尸）→ 如实报「进行中」，不能报成「没有结果」。
       报成没有结果的后果不只是少显示一条：界面拿到「没有结果」会去自动开跑一次，
       于是同一次分析在用户眼里变成两次、页面上永远挂着「进行中」——用户报过这个。
       进程已死的幽灵记录由启动时的 `fail_orphaned_analysis_runs` 清掉，所以这里
       剩下的 running 是真的在跑；
    3. 否则**退回到最近一条真有结论的成功记录**，并带上 `stale` / `stale_reason` /
       `stale_note` 如实说明它是旧的。这是用户报的那条：「我进行过 AI 分析，重启
       服务后变成未分析」—— 结论还在，只是最新那次不是它（规则变了、或最新那次被
       重启打断成 failed），而旧口径把「最新那条不可用」直接等同于「没有结论」；
    4. 一条结论都没有时，才轮到「最近一次失败」这条形态（有原因、没结论）；连失败都没有就返回
       None —— 那才是真的没分析过。**僵尸 running 也走这一档**（下面按 `effective_status` 判）。

    `project_id` 只用于溯源现算（见 `_is_run_fresh` 的 `expected` 参数）。
    """
    run = (
        AiAnalysisRun.query.filter(*conditions)
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )
    if run is None:
        return None
    expected = current_provenance(project_id) if project_id else None
    if _is_run_fresh(run, expected=expected):
        return _conclusion_payload(run)
    if run.status == "running" and not run.is_stale_running:
        return _in_progress_result(run)

    concluded = _latest_concluded_run(conditions)
    if concluded is None:
        return _last_attempt_failed_result(run) if run.effective_status == "failed" else None
    if concluded.id == run.id:
        # 唯一那条成功记录就是最新这条，却没过 `_is_run_fresh` —— 只可能是溯源变了
        # （时间窗与 status 在 `_latest_concluded_run` 里已经判过一遍）。
        reason = STALE_REASON_RULES_CHANGED
    else:
        reason = STALE_REASON_INTERRUPTED
    payload = _conclusion_payload(concluded)
    payload["stale"] = True
    payload["stale_reason"] = reason
    payload["stale_note"] = _stale_note(reason, concluded=concluded, newest=run)
    # 最新那条的身份也带上：界面要能说清「挡住它的那一次是什么状态」。
    payload["newest_run"] = {
        "run_id": run.id,
        "status": run.effective_status,
        "created_at_display": _created_at_display(run),
    }
    return payload


def get_latest_weekly_result(config_id: int) -> Optional[dict]:
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    group_key = build_weekly_group_key(config)
    payload = _read_latest_result(
        (AiAnalysisRun.target_type == "weekly", AiAnalysisRun.target_key == group_key),
        project_id=config.project_id,
    )
    if payload and payload.get("run_id"):
        # `focus` 只有周版本有（单提交不分范围），所以只在周版本这条路径上补。
        run = db.session.get(AiAnalysisRun, payload["run_id"])
        payload["focus"] = _focus_from_run(run) if run else {"key": FOCUS_ALL, "label": ""}
    return payload


def get_latest_commit_result(commit_id: int) -> Optional[dict]:
    commit = db.session.get(Commit, commit_id)
    repo = db.session.get(Repository, commit.repository_id) if commit else None
    return _read_latest_result(
        (AiAnalysisRun.target_type == "commit", AiAnalysisRun.target_id == commit_id),
        project_id=repo.project_id if repo else None,
    )


def select_primary_weekly_config(configs: List[WeeklyVersionConfig]) -> WeeklyVersionConfig:
    """挑一个 config，用来给**新建分组**命名（`base_name`）并定窗口。

    **刻意不跟随取样的仓库优先级。** 这个值决定 `AiWeeklyAnalysisState.base_name`，
    而 base_name 参与 `group_key` 的计算 —— 换个仓库当 primary 就是换了分组身份，
    已有分组的增量水位线会对不上。所以这里保持既有口径：**取 id 最小的那个**
    （部署上就是先建的配表仓库那一侧）。

    以前这里写的是 `sorted(configs, key=(_repo_priority, -cfg.id), reverse=True)[0]`。
    当时 `_repo_priority` 对两个仓库都返回 2（`type == "git"` 那一句），于是实际
    行为就是「id 最小」；把它显式写出来，是为了在 `_repo_priority` 修好之后不再
    阴差阳错地改成「代码仓库当主」。
    """
    return min(configs, key=lambda cfg: cfg.id)
