#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI analysis service（真实执行器）。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from models import (
    Commit,
    Project,
    Repository,
    WeeklyVersionConfig,
    db,
)
from models.ai_analysis import (
    # 「用户点了全量」那一个取值（`run_weekly_analysis_background` 的判据）：
    # 常量只有一份，在 `models/ai_analysis/job.py`，本文件不另立字面量。
    MODE_FULL,
    AiAnalysisAnomaly,
    AiAnalysisRun,
    AiAnalysisTrace,
    # 测试按 `ai_service.AiProjectAnalysisConfig` 直接建行 / 数列（属性访问，不是 import），
    # 所以这个「本文件不直接用」的模型类必须留着 —— ruff 的 F401 会想删它。
    AiProjectAnalysisConfig,  # noqa: F401 —— 测试按属性取
    AiWeeklyAnalysisState,
)
from models.ai_analysis.project_config import (
    DEFAULT_AUTO_WEEKLY_ENABLED,
    DEFAULT_MAX_ANOMALIES_PER_RUN,
    DEFAULT_MAX_FILES_PER_RUN,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)
from services.ai import project_gate
from services.ai.analysis_budget import budget_gate_reason, early_stop_guard

# 计划（工作包 B）：`auto_sizing.plan_analysis` 是**纯函数**，取数与落库在下面这一层 ——
# 运行侧与预估端点读的必须是**同一份**（`payload["plan"]`），不能再各推一次。
from services.ai.analysis_plan import (
    attach_plan,
    compose_skip_guards,
    make_single_run_guard,
    plan_of,
    single_member_limits,
)
from services.ai.auto_sizing import (
    ANOMALY_RUN_CAP,
    MODE_FAMILY,
    derive_family_sizing,  # noqa: F401 —— 测试按这个名字打补丁/取用
    sampling_cap_for,
)

# 增量基线的编排块（AI-P0-02）：拿基准、冻目标快照、推进指针。**全部逻辑在那边**，
# 本文件只按名字取用 —— 本文件贴着长度闸门（WARN 1800 / ERROR 2000），新增逻辑写在这里
# 只会把它推过硬上限。
#
# 与下面 `baseline_source` 那一块**同一个坑**：`# noqa: F401` 必须写在**每个别名自己
# 那一行**。写在 `from … import (` 那一行盖不住按名字报的 F401，`ruff check --fix` 会把
# 测试要用的别名当垃圾删掉（真发生过：它删掉了 `_complete_coverage_ratio`，而
# `tests/test_ai_diff_snapshot_baseline.py` 按那个名字断言「阈值可配置」）。
from services.ai.baseline_blocks import (
    advance_weekly_state,
    resolve_baseline,
    seal_target_snapshot,
)
from services.ai.baseline_blocks import (
    complete_coverage_ratio as _complete_coverage_ratio,  # noqa: F401 —— 测试按这个名字取
)
from services.ai.baseline_blocks import (
    snapshot_digest_for as _snapshot_digest_for,
)

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
)
from services.ai.baseline_source import (
    baseline_findings as _baseline_findings,  # noqa: F401 —— 测试按这个名字 import
)
from services.ai.baseline_source import (
    previous_run as _previous_run,  # noqa: F401 —— 测试按这个名字 import
)
from services.ai.baseline_source import (
    skipped_unstructured_runs as _skipped_unstructured_runs,  # noqa: F401 —— 测试按这个名字 import
)
from services.ai.baseline_source import (
    suppressed as _suppressed,
)
from services.ai.budget import effective_prompt_budget
from services.ai.budget_plan import build_budget_plan, derive_tool_limits
from services.ai.change_set import from_commit_payload, from_weekly_payload

# 读侧形态（进行中 / 有结论 / 最近一次失败）：只依赖 run 行与两个标签函数，
# 与「怎么跑一次分析」没有耦合，单独一层也好单测。
from services.ai.conclusion_view import (  # noqa: F401 —— 本文件的读侧路由仍在用
    _conclusion_payload,
    _created_at_display,
    _in_progress_result,
    _last_attempt_failed_result,
    _parse_response_payload,
)

# 覆盖账本（「这次看了多少、缺什么」）：只在落库那一刻算一次 —— 它要读已落库的请求
# 载荷 + 逐轮明细 + 按工具计数，而分析过程中那几样还散在内存里（见 `_persist_outcome`）。
from services.ai.coverage_ledger import ledger_from_run as coverage_ledger

# 同上面那个模型类：本文件不直接调它，但测试用 `monkeypatch` 打的是
# `ai_service.build_probe_client` 这个名字（属性访问），删掉就等于让补丁落空。
from services.ai.endpoint_service import build_probe_client  # noqa: F401 —— 测试按属性打补丁
from services.ai.engine import (
    STATUS_DEGRADED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineLimits,
    EngineOutcome,
    run_analysis,
)
from services.ai.engine import (
    failed as engine_failed,
)
from services.ai.incremental_baseline import reconcile_result as reconcile_incremental_result

# 读侧「最近一次结论」那一段（`get_latest_*_result` / `_read_latest_result` …）已经搬到
# `services/ai/latest_result.py`：本文件顶着仓库 2000 行的 ERROR 闸门
# （`scripts/check_file_length.py`），而那一段只读 run 行、与「怎么跑一次分析」没有耦合。
#
# **仍然从这里重导出**：路由与十几处测试都是按 `ai_analysis_service.<名字>` 这个**属性**
# 取用的（`routes/ai_analysis_routes.py`、`tests/test_ai_history_survives_restart.py` …），
# 直接删掉就等于把它们全部打断。本文件自己只直接调 `get_latest_weekly_result`
# （在 `_reusable_conclusion` 里），另外九个名字**纯粹是回导** —— 所以它们各自都需要
# 一行 `# noqa: F401`，否则下一次 `ruff --fix` 就会把它们按「没用到的 import」删掉。
#
# **`# noqa: F401` 逐名写在别名那一行，不写在 `from … import (` 那一行** —— 理由见上面
# `baseline_source` 那一段：写在开头盖不住按名字报的 F401，`--fix` 会把它们当垃圾删掉。
from services.ai.latest_result import (
    STALE_REASON_INTERRUPTED,  # noqa: F401 —— 路由与测试按属性名取用
    STALE_REASON_RULES_CHANGED,  # noqa: F401 —— 路由与测试按属性名取用
    _focus_from_run,  # noqa: F401 —— 路由与测试按属性名取用
    _latest_concluded_run,  # noqa: F401 —— 测试按属性名取用
    _parse_iso_datetime,  # noqa: F401 —— 测试按属性名取用
    _read_latest_result,  # noqa: F401 —— 路由与测试按属性名取用
    _stale_note,  # noqa: F401 —— 测试按属性名取用
    get_latest_commit_result,  # noqa: F401 —— 路由与测试按属性名取用
    get_latest_weekly_result,
    select_primary_weekly_config,  # noqa: F401 —— 调用点与测试仍在用
)
from services.ai.llm_client import LLMError
from services.ai.manifest import build_manifest
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
    weekly_batch_configs,
)
from services.ai.project_facts import (
    generated_prefixes,
)
from services.ai.prompt import platform_prompt_chars
from services.ai.provenance import current_provenance
from services.ai.result_payload import (
    coverage_notice_text,
    failed_result,
    result_payload,
)
from services.ai.rules import RuleThresholds
from services.ai.run_cache_source import (  # noqa: F401 —— 启动清理与流式入口仍在用
    ANALYSIS_CACHE_DAYS,
    CONCLUDED_STATUSES,
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
from services.ai.skill_contract import DIMENSION_IDS, dimension_ids_of
from services.ai.skill_loader import describe_load_error, load_skills
from services.ai.snapshot_store import DEFAULT_COMPENSATION_MAX_FILES
from services.ai.subagent import (
    MIN_MEMBER_ROUNDS,
    MIN_MEMBER_TOOL_REQUESTS,
    apply_manifest,
    plan_family,
    run_family_with_seed,
    subagent_mode_of,
)
from services.ai.trace_evidence import encode_evidence
from services.ai.usage import encode_tools
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


def _concluded_run(state) -> Optional[AiAnalysisRun]:
    """这个分组的**结论基线**那条运行（`ai_weekly_analysis_state.last_concluded_run_id`）。

    降级也算（它跑完了、有可复用的结论，只是浅），所以指针与「时间水位线」不是一回事。
    指不到就回 `None`：补偿集宁可不补，也不去猜「上一次是哪一次」。
    """
    if state is None:
        return None
    run_id = getattr(state, "last_concluded_run_id", None)
    if not run_id:
        return None
    try:
        return db.session.get(AiAnalysisRun, run_id)
    except Exception:  # noqa: BLE001 —— 读不到基线不是「这次分析起不来」的理由
        return None


def build_weekly_payload(
    config_id: int,
    *,
    force_full: bool = False,
    focus: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[AiWeeklyAnalysisState], Optional[str]]:
    config = WeeklyVersionConfig.query.get_or_404(config_id)
    # 输入集 = 这个**批次**的全部配置。判据必须与分组键同源（同项目 + 同窗口 +
    # **同版本名**）—— 只按「项目 + 窗口」取会把同一窗口下另一个名字的周版本配置
    # 也拉进来，于是报告标题写着一个版本、清单里却有另一个版本的文件（REV-AI-002）。
    configs = weekly_batch_configs(config)
    if not configs:
        return None, None, "no_configs"

    group_key = build_weekly_group_key(config)
    base_name = _resolve_base_name(config)
    project_config = get_project_analysis_config(config.project_id)
    state = AiWeeklyAnalysisState.query.filter_by(group_key=group_key).first()

    # **做差的基准是快照，不是时间水位线**（AI-P0-02）。`force_full` 时永远是 None ——
    # 全量模式不看基线。退回时间水位线的只有一种情形：这个分组还没有任何冻结快照
    # （升级上来的老分组），见 `baseline_blocks.resolve_baseline`。
    baseline, baseline_account = resolve_baseline(group_key, state, force_full=force_full)
    summary, details, skip_reason = _summarize_weekly_files(
        configs,
        baseline,
        # 补偿集的事实来源：**上一次有可复用结论的运行**（降级也算）。它那 94% 没取到
        # 证据的文件不许就这么算了 —— 按风险排序后回到这一轮的输入里。
        base_run=_concluded_run(state),
        compensation_max=_configured_int(
            project_config.get("compensation_max_files"), DEFAULT_COMPENSATION_MAX_FILES
        ),
    )
    if skip_reason and not force_full:
        return None, state, skip_reason

    scope, policy = _decide_scope(summary, baseline)

    repo_details = details.get("repos", [])
    repo_details.sort(key=lambda item: (item.get("priority", 1), item.get("repository_name", "")), reverse=True)

    delta_files = details.get("delta_files", [])
    # 清单取样上限按本周规模推导（配置面收敛，2026-09-23）：clamp(文件数, 200, 500)。
    # 旧的 `max_files_per_run` 配置已收掉 —— 公式与 `derive_family_sizing` 同一份常数
    # （`auto_sizing.sampling_cap_for`），不是第二个事实源。
    max_files = sampling_cap_for(len(delta_files))
    compensation_files = list(details.get("compensation_files") or [])
    focus_label = ""
    if focus:
        delta_files, focus_label = _filter_delta_files_by_focus(delta_files, focus, configs)
        if not delta_files:
            return None, state, "focus_empty"
        # **计数要跟着筛选走**（四个都跟）：不跟的话提示词会告诉模型「本次变更共 767 个
        # 文件」而它只看得到 19 个 —— 「把清单当全量」的镜像错误，这次是把全量说大了。
        # 补偿项那一份同样要跟着筛：它已经算进 `delta_files` 里，两本账不能对不上。
        kept = {item.get("file_path") for item in delta_files}
        compensation_files = [
            item for item in compensation_files if item.get("file_path") in kept
        ]
        summary = {
            **summary,
            "total_files": len(delta_files),
            "delta_files": len(delta_files),
            "batch_files": len(delta_files),
            "compensation_files": len(compensation_files),
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
        # 这次做差的**基准**（快照 id / 指纹 / 条数 / 覆盖面）。落进 request_payload 之后，
        # 「这次为什么是增量、基准是哪一份」不必再去猜状态行的当前值 —— 状态行是**会变的**，
        # 而这份账冻结在运行记录上（同 `coverage_ledger` 读 request_payload 的口径）。
        "baseline": baseline_account,
        # **补偿项单独列一份**（复测文档 :163 的验收：「这 5 个文件 + 必要依赖和**明确
        # 列出的补偿项**」）。它们已经在 `delta_files` 里（模型读得到），这里再显式列一遍
        # 是为了让「这周真的改了什么」与「上一轮漏看了什么」在账上分得开 —— 混在一起，
        # 报告里的变更数会被补偿项虚增。
        "compensation_files": compensation_files,
        "repositories": repo_details,
        # 白名单：本批次**全部**改动过的文件。模型能读的 diff 就是这个集合。
        "delta_files": delta_files,
        # 提示词里**列出来**的那部分。绝大多数版本与 `delta_files` 相同（见下面的说明）。
        "list_files": list_files,
        "delta_truncated": truncated,
    }
    payload["manifest"] = build_manifest(
        delta_files,
        shard_count=max(1, int(project_config.get("subagent_count") or 1)),
    ).to_dict()
    payload["policy"]["truncated"] = truncated
    if truncated:
        payload["policy"]["truncation_reason"] = "token_budget"
    attach_weekly_plan(payload)
    return payload, state, None


def attach_weekly_plan(payload: dict) -> object:
    """给这份 payload 算一份计划并挂上去（`payload["plan"]`）。**周版本唯一的计划入口**。

    ## 为什么在 `build_weekly_payload` 里算（而不是在真正开跑时）

    因为**预估端点也必须读同一份**。预估发生在用户点「开始」之前，那时还没有运行记录，
    唯一的共同载体就是这份 payload（`ai_usage_service._weekly_payload_facts` 本来就调
    `build_weekly_payload`）。计划算在这里，于是「预估说 5 片、实际跑 3 片」在结构上
    不可能发生 —— 这不是一致性检查，是**同一份数据**。

    ## 预算取**配置值**（窗口未探测），这一点如实写在计划里

    真正生效的额度要等 `_apply_model_window` 向端点问过窗口才知道，而那个探测需要一次
    网络往返（预估端点承诺**不探测模型**）。所以这里用项目配置的提示词预算当**上限**：
    计划里的每片额度本来就是上限（真正的单条上限由 `tool_limits` 在开跑时按生效额度
    再压一次），所以「窗口比配置小」时不会超发，只是计划里的名义额偏宽松。
    `thresholds.user_chars_source` 把这件事写出来。

    ## 维度清单用**项目声明**的那一份（拿不到时退回出厂清单）

    「单分析者仍要覆盖全部声明维度」这句话必须落在计划上，所以这里真的去加载 skill
    （`_load_project_skills` 已有失败兜底）。加载失败时退回出厂清单，并在计划的
    `thresholds.dimensions_source` 里说明 —— 一份「不知道项目声明了什么」的计划不该
    看起来像「项目声明了出厂那 9 个」。
    """
    project_id = (payload.get("group") or {}).get("project_id")
    project_config = get_project_analysis_config(project_id) if project_id else {}
    loaded, failure = _load_project_skills(project_id) if project_id else (None, "没有项目号")
    dimensions = dimension_ids_of(getattr(loaded, "dimensions", ()) or ()) if loaded else ()
    source = "项目声明" if dimensions else f"平台出厂清单（读不到项目声明：{failure or '未知'}）"
    if not dimensions:
        dimensions = DIMENSION_IDS
    configured = _configured_int(
        project_config.get("prompt_char_budget"), EngineLimits().prompt_char_budget
    )
    plan = attach_plan(
        payload,
        effective_budget={
            "user_chars": max(0, configured),
            "single_run_token_limit": project_config.get("single_run_token_limit"),
            # `output_tokens` / `single_run_token_limit` 目前都不是项目配置里的列（配置面
            # 2026-09-23 收敛过一轮），所以它们通常取不到值 → 计划用平台初值。真正能改的
            # 那个是下面这个**周期**上限（`budget_token_limit`，管理员可改）：它比单次初值
            # 更紧时单次也不许超过它 —— 「用户可覆盖单次上限」的现成入口就是它。
            "period_token_limit": project_config.get("budget_token_limit"),
            "output_tokens": project_config.get("max_output_tokens"),
        },
        dimensions=dimensions,
        # **手动关掉子代理 = 强制单代理**（指引 §3.B）。这条判据在计划里，不在配置读取侧：
        # 计划是唯一决定分工的地方，写在这里就不可能出现「界面关了、计划又开了」。
        subagent_enabled=bool(project_config.get("subagent_enabled")),
        verify=bool(project_config.get("subagent_verify")),
        cost_limit=project_config.get("budget_cost_limit"),
    )
    plan.thresholds["user_chars_source"] = "项目配置的提示词预算（窗口未探测，运行时会再压）"
    plan.thresholds["dimensions_source"] = source
    payload["plan"] = plan.to_dict()
    log_print(
        f"AI 分析计划：模式 {plan.mode}、{plan.member_count} 个成员、"
        f"单次上限 {plan.total_token_budget:,} token（{plan.reason}）",
        "AI",
    )
    return plan


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


def _delta_bases(payload: Mapping[str, object]) -> dict:
    """增量那一段的基线表，键 `(repository_id, latest_commit_id, file_path)`。

    来源是 payload 的 `delta_files`（写入侧在 `_summarize_weekly_files` 里按做差基准
    填的 `diff_base_commit_id`）。仓库 ID 不能省：SVN 修订号只在单个仓库内唯一，两个
    SVN 仓库完全可能同时出现相同修订号和相同相对路径。
    """
    bases: dict = {}
    for item in payload.get("delta_files") or ():
        if not isinstance(item, Mapping):
            continue
        base = str(item.get("diff_base_commit_id") or "").strip()
        commit = str(item.get("latest_commit_id") or "").strip()
        path = str(item.get("file_path") or "").strip()
        repository_id = item.get("repository_id")
        if base and commit and path:
            bases[(repository_id, commit, path)] = base
    return bases


def _cache_rows(payload: Mapping[str, object]) -> dict:
    """增量那一段的**缓存行主键**表，键 `(repository_id, latest_commit_id, file_path)`。

    缓存行是按 `config_id` 写的（一个周版本一行），而 AI 取数原先只按
    `(repository_id, path, latest_commit_id)` 查、取 id 最大的那一行 —— 两个周窗口的
    终点指向同一条提交时（回填日期的提交、同刻提交），它会**猜**，而且可能猜中另一个
    窗口那一份（REV-AI-003）。写侧（`scope_sampling._summarize_weekly_files`）把来源行
    一并写进每条 delta，这里抽出来交给 provider。
    """
    rows: dict = {}
    for item in payload.get("delta_files") or ():
        if not isinstance(item, Mapping):
            continue
        row_id = item.get("cache_row_id")
        commit = str(item.get("latest_commit_id") or "").strip()
        path = str(item.get("file_path") or "").strip()
        if row_id is None or not commit or not path:
            continue
        rows[(item.get("repository_id"), commit, path)] = int(row_id)
    return rows


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
        return replace(
            limits,
            tool_limits=derive_tool_limits(
                prompt_char_budget=limits.prompt_char_budget,
                max_tool_requests=limits.max_tool_requests,
            ),
        ), ""
    # 压完再减去内置那一段：**用户的额度不能被平台自己的提示词吃掉**。窗口小到连
    # 内置提示词都装不下时落到 0 —— 那时组装侧还有条目下限兜着，而这一轮的说明已经
    # 把「窗口太小」讲清楚了。
    #
    # ★ 这个 `effective` 是**用户内容**的额度，它的两个去处是「派生单条上限」与「报给 UI」，
    # **不是** `limits.prompt_char_budget`。
    #
    # 2026-09-22 修：这一行原先把 `effective` 赋给了 `prompt_char_budget`，那是**扣了两次**
    # 平台提示词 —— 引擎比的是**整份提示词**（`engine.estimate_chars(messages)`，而 `messages`
    # 里第一条就是系统提示词，见 `subagent.build_seed_messages`），所以它要的是压过的**总数**
    # `budget`；把「总数 − 平台那段」再交给它，等于平台那段先从水位里扣一次、又在比较里扣一次。
    # 后果是重度压缩的项目白少用 `platform_chars` 那么多额度（本机 560k 的配置走的是**没压**
    # 那条路，所以线上一直没暴露）。同一个数还让界面与预估端点对不上：预估侧报的是**内容额度**，
    # 运行侧报的是总数，于是「配置 560,000 / 生效 580,000」这种读不通的话就出现了。
    effective = max(0, budget - platform_chars)
    return replace(
        limits,
        prompt_char_budget=budget,
        tool_limits=derive_tool_limits(
            prompt_char_budget=effective,
            max_tool_requests=limits.max_tool_requests,
        ),
    ), note


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
        # 分片额度**不超过全次上限**：项目把「单次异常上限」调小（比如 5）却留下分片额度
        # 10，会变成「每个分片照报 10 条、汇总再砍掉一半」—— 白写的那些 token 正是这
        # 一栏要省的。两个值都从同一份 project_config 里读，所以夹在这里是免费的。
        max_anomalies_per_subagent=max(
            1,
            min(
                _configured_int(
                    project_config.get("max_anomalies_per_subagent"),
                    defaults.max_anomalies_per_subagent,
                ),
                _configured_int(
                    project_config.get("max_anomalies_per_run"),
                    DEFAULT_MAX_ANOMALIES_PER_RUN,
                ),
            ),
        ),
    )


def _publish_progress(run_id: int, project_id: int, progress) -> None:
    """同时更新快速内存快照与跨进程可读的 job 快照。"""
    publish_run_progress(run_id, project_id, progress)
    try:
        from services.ai import job_service, run_progress

        snap = run_progress.snapshot(run_id)
        if snap is not None:
            job_service.persist_progress(run_id, snap.to_dict())
    except Exception as exc:  # noqa: BLE001 —— 展示进度失败不能中断付费分析
        log_print(f"⚠️ 持久化 AI 进度失败（不影响分析）: run={run_id} {exc}", "AI", force=True)


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

    ## 三种终态要**原生**落进 `status` 列

    原先只有一句 `"failed" if outcome.status == STATUS_FAILED else "succeeded"`，于是
    引擎的 succeeded / degraded 在列上合成一个值：实测库里 13 条完成运行里 12 条
    payload 是 degraded，而 `status` 列全是 `succeeded` —— 「这次是降级交付」只能去解
    整坨 payload 才看得出来。降级原因另存一列（短标识，不是那段给人看的文字）。

    **水位线的判据不在这一列上**（见 `_update_weekly_state`：它读引擎给的
    `engine_status`）—— 两把尺子不要合并。
    """
    if outcome.status == STATUS_FAILED:
        run.status = STATUS_FAILED
    elif outcome.status == STATUS_DEGRADED:
        run.status = STATUS_DEGRADED
    else:
        run.status = STATUS_SUCCEEDED
    # 降级原因用**短标识**（`DEGRADE_*`），给人看的那句话仍在 payload 的
    # `degradation_label` 里 —— 不在这边再抄一份，两处各自演化迟早对不上。
    run.degradation = outcome.degradation or None
    # **认领就此释放**（见 `_create_run` 的幂等一节）。不释放的后果是这份输入
    # 从此再也发起不了分析 —— 一条永远解不开的死锁。
    run.active_key = None
    run.finished_at = _utcnow()
    # 结论形态：模型按协议给了结构化结论 → True；只有一份 markdown 报告 → False；
    # 失败 → None（下面那支会把结论字段清空，本来就没有结论）。
    # 判据是 `outcome.payload` 而不是 `outcome.status`：降级分两种，有 payload 的那种
    # （轮次/额度/上下文用尽）结论仍然是结构化的，只是浅；没有 payload 的那种
    # （`DEGRADE_MARKDOWN`）一条结构化结论都没有。**只有后者不能当基线**。
    #
    # **例外是失败**：原先这里写的是 `if run.status == "succeeded"`，靠「降级也存成
    # succeeded」顺带算对了。`status` 能原生表示 degraded 之后那句话对降级变成假 ——
    # 于是所有降级运行会在**基线上静默消失**（下一轮把上轮报过的问题全部当新发现重报）。
    # 所以这里判的必须是「有没有失败」，而不是「是不是 succeeded」。
    run.conclusion_structured = (
        bool(outcome.payload is not None) if outcome.status != STATUS_FAILED else None
    )
    if run.status == STATUS_FAILED:
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
        # 增量结果会在模型返回后由平台把上一轮仍有效的结论确定性合并进来。正文必须读
        # `result` 的最终形态；继续读 `outcome` 会让 payload 有累积结论、页面正文却只剩
        # 本轮模型那几条，两份真相当场分叉。
        run.response_text = result.get("report_markdown") or outcome.error_message or ""
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
                error=(
                    "；".join(
                        item
                        for item in (
                            record.note,
                            f"finish_reason={record.finish_reason}" if record.finish_reason else "",
                        )
                        if item
                    )
                    or None
                ),
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
                # 平台延续的历史结论保留人工处置；相关文件再次变化时，合并器已经把它
                # 退回 pending，避免一次旧的“已确认/已忽略”覆盖新证据。
                disposition=item.get("disposition") or "pending",
            )
        )

    # **覆盖与缺口**（审计 P1-03）：挂进 payload，`report_markdown` 一个字节都不动。
    #
    # 落在这里（逐轮明细已 `add`、`coverage_ledger` 的查询会顺手 flush）而不是更早，
    # 是因为账本要读那些明细才能算出「哪些文件真的被看过」。导出那份 `.md` 早就有这一段
    # （`routes/ai_analysis_routes.ai_run_report_md` 现算账本传给 `report_document`），
    # 而**抽屉读的是这一份落库的 payload** —— 真机验证时它里面「本次覆盖与缺口」「覆盖
    # （版本清单）」「覆盖（取到证据）」这些关键词一个都没有，用户第一眼看到的那份报告
    # 于是既不说看了多少、也不说缺了什么。
    #
    # **为什么是独立一个键、而不是追加进报告正文**：正文被别的环节当**字符串判据**用 ——
    # `services/ai/family_ledger.reconcile_candidates` 靠它核对候选编号与候选的
    # `file_path` 有没有被点名（那条判据的明文取舍是「正文里提到过不算采纳」），而覆盖段
    # 里恰好会列出**取数失败的文件路径**；下一轮的基线摘要也会读回正文。写成独立一个键，
    # 这些判据连碰都碰不到它。屏幕侧由
    # `static/js/ai_context_notice.js` 贴到结论后面（见 `result_payload.coverage_notice_text`）。
    # （这里原先还提了一句 `verdict.read_ruling` —— 那个从正文取回裁决块的函数已随
    # AI-P0-05 删除，正文里不再有机器 json 可读。）
    # 取值只在**还没被判定为失败**的那条路上做：失败不写结论字段（`response_payload`
    # 保持 `None`）是上面那段注释里写死的一条口径 —— 前端只判这个字段就能把失败显示成
    # 「已有结果 · 风险等级 high」，这一段不许把它破掉。
    if run.response_payload is not None:
        try:
            coverage_notice = coverage_notice_text(coverage_ledger(run))
        except Exception as coverage_exc:  # noqa: BLE001 —— 一段补充说明不该毁掉整条结论
            coverage_notice = ""
            log_print(
                f"⚠️ AI 分析：覆盖账本没能算出来（{type(coverage_exc).__name__}: {coverage_exc}），"
                f"抽屉里「本次覆盖与缺口」这一段会缺席（run={run.id}）",
                "AI",
                force=True,
            )
        if coverage_notice:
            result["coverage_notice"] = coverage_notice
            # 上面已经写过一次 payload，这里带上覆盖段**重写**（`result` 是同一个对象，
            # 调用方拿它当 SSE 的 `result` 事件下发 —— 于是「刚跑完」那一次也带得动这段话）。
            run.response_payload = _json_dumps(result)

    db.session.commit()


class ActiveAnalysisConflict(RuntimeError):
    """同一目标 + 同一份输入**已经有一条活动运行**（数据库的唯一约束拦下的）。

    它不是错误，是一次**幂等命中**：调用方应当附着到 `run` 上（把运行号交给界面，
    让它接着看那一次的进度与结论），而不是再发起一次。建一条活动运行是「要花钱」的
    前置条件 —— 拦住它 = 拦住计费。

    为什么要有这个类型而不是返回 None：调用方必须**显式**处理这件事。悄悄返回
    None 或者悄悄复用，都会让「手工与定时同时触发」这类并发在代码里看不出发生过。
    """

    def __init__(self, run: AiAnalysisRun):
        super().__init__(f"已有一次分析在进行中（运行 #{run.id}）")
        self.run = run


def _analysis_claim_key(
    *,
    project_id: int,
    target_type: str,
    target_id: Optional[int],
    target_key: Optional[str],
    scope: str,
    payload: dict,
) -> str:
    """「同一目标 + 同一份输入」的指纹 —— 活动运行唯一约束的键。

    它要能把两件事分开（这是需求里那一句「变更后应允许新建」）：

    * **同一次输入**：手工连按两次、手工与定时同时触发 → 同一个键 → 第二次被拦下，
      两次只花一次钱；
    * **输入确实变了的新一轮**：周版本的缓存行内容变了（快照指纹跟着变）→ 换一个键
      → 照常允许新建。不换的话，快照更新之后这个目标再也分析不了（复用旧结论）；
      反过来若键里不含输入，任何时候都拦，用户就永远无法重新分析。

    目标身份用 `group_key`（周版本）而不是 config_id：一次分析的输入是**整批**仓库的
    缓存行，同一批里的两条配置各存一份会因为键不同而各跑一次，而那两次看到的清单
    逐字相同。

    **不含溯源**（提示词 / skill / 规则 / 模型的版本号）：那几个是「这份结论能不能
    复用」的判据（`_is_run_fresh`），不是「是不是同一份输入」。把它们并进来会让
    「改一条规则」与「换一份快照」在幂等键上变得一样，而在一次 23 分钟的分析中途
    改配置并不构成再跑一次的理由。
    """
    group = payload.get("group") or {}
    focus = (payload.get("focus") or {}).get("key") or FOCUS_ALL
    if target_type == "weekly":
        # 内容身份：这一批配置当前这份快照（见 scope_sampling.weekly_snapshot_digest）。
        # 算不出来时是空串 —— 宁可退化成「同一目标只有一次」，也不能因为一个指纹
        # 算不出来就放行第二次真金白银的调用。
        digest = _snapshot_digest_for(payload) or ""
    else:
        # 单提交：`target_id` 本身就是内容身份（一条提交的内容不可变）。
        digest = ""
    raw = "|".join(
        [
            str(project_id),
            str(target_type),
            str(target_key or target_id or group.get("key") or ""),
            str(scope),
            str(focus),
            digest,
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _holds_active_claim(run: AiAnalysisRun) -> bool:
    """这条运行**现在**还占着那份输入吗。

    `pending` / `running` 且不是僵尸才算占着。「还在排队」也算 —— 它马上就要跑，
    放行第二次只会在它跑完之后再花一次钱。

    僵尸（超过 `STALE_RUNNING_SECONDS` 没动静）**不算**：进程可能只是慢，但界面早已
    按 `effective_status` 把它报成失败了，认领就不能比界面的口径活得更久。
    """
    if run is None:
        return False
    if (run.status or "") not in ("pending", "running"):
        return False
    return not run.is_stale_running


def _claim_holder(claim_key: str) -> Optional[AiAnalysisRun]:
    """这个键现在握在谁手里。**握在一条已经死掉的运行上就地清掉**并把位置让出来。

    为什么必须判「它还活着吗」而不是「有没有这一行」：`active_key` 由 `_persist_outcome`
    在跑完时清空，但那不是唯一的结束方式 —— 进程被杀、平台重启
    （`fail_orphaned_analysis_runs` 只改 `status`，不认识这一列）、落库本身失败，
    都会留下一条**带着认领的死人**。只按「有没有这一行」判的话，那份输入从此再也
    发起不了分析：一条永远解不开的死锁，比不加约束更糟。
    """
    try:
        holder = (
            AiAnalysisRun.query.filter(AiAnalysisRun.active_key == claim_key)
            .order_by(AiAnalysisRun.created_at.desc())
            .first()
        )
    except Exception as exc:  # noqa: BLE001 —— 查不动时按「没人占着」处理，理由见下
        # 查不动（表还没建出来 / 连接断了）就当成没有占用者：让这次分析照常建起来。
        # 反过来（当成有人占着）会把「读一次库失败」升级成「这个目标再也分析不了」，
        # 而重复一次分析的代价只是钱，不是数据。
        log_print(f"⚠️ AI 分析：读取活动运行认领失败（{exc}），按无占用处理", "AI")
        return None
    if holder is None:
        return None
    if _holds_active_claim(holder):
        return holder
    try:
        holder.active_key = None
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 —— 清不掉就让下面那次 INSERT 去撞约束
        db.session.rollback()
        log_print(
            f"⚠️ AI 分析：清理失效的活动运行认领失败（run={holder.id}）：{exc}", "AI"
        )
    return None


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

    ## 幂等：活动运行唯一约束（**不是**「先查再插」）

    `active_key` 是「同一目标 + 同一份输入」的指纹，表上有一个 **UNIQUE** 索引，
    而它**跑完就清空**（`_persist_outcome`）。于是「同一份输入同时只允许一条活动运行」
    由数据库裁决：两个进程、两个标签页同时按下去，只有一个 INSERT 会成功，另一个拿到
    `IntegrityError` —— 那正是要的信号，不是要吞掉的错误。

    为什么不「先查再插」：查与插之间永远有一个窗口，而**连按两次按钮恰好就落在那个
    窗口里**（前端那句 `startBtn.disabled = true` 只挡得住一个页面内的重复点击，
    关掉抽屉再打开就能重按）。

    冲突时抛 `ActiveAnalysisConflict`（带**已有那条运行**），由调用方决定怎么告诉界面：
    周版本那两份抽屉附着上去接着看，单提交那份说清「已经有一次在跑」。
    """
    summary = payload.get("summary") or {}
    # 冻结这次的**目标快照**（AI-P0-02）：位置在**所有闸门之后**，所以冻下来的必定是
    # 一份同步已经写完的清单。它同时是「这次分析的是哪一份」的账与下一次做差的基准。
    # 出错只记日志（见 `baseline_blocks.seal_target_snapshot`），不阻断这次分析。
    snapshot_account = seal_target_snapshot(payload)
    if snapshot_account:
        payload["snapshot"] = snapshot_account
    # 溯源在重试路径上会被用两次，所以先算一次（它要哈希源码文件，不便宜）。
    provenance = current_provenance(project_id)
    claim_key = _analysis_claim_key(
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target_key=target_key,
        scope=scope,
        payload=payload,
    )

    def _build() -> AiAnalysisRun:
        return AiAnalysisRun(
            project_id=project_id,
            target_type=target_type,
            target_id=target_id,
            target_key=target_key,
            status="running",
            response_mode=response_mode,
            scope=scope,
            trigger_source=trigger_source,
            trace_id=f"{target_type}-{target_id}-{int(_utcnow().timestamp())}",
            active_key=claim_key,
            **provenance,
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

    for attempt in range(2):
        run = _build()
        db.session.add(run)
        try:
            db.session.commit()
            return run
        except IntegrityError:
            # 唯一索引挡下了「同一份输入的第二次并发发起」—— 一个模型请求都还没发出去。
            db.session.rollback()
            holder = _claim_holder(claim_key)
            if holder is None and attempt == 0:
                # 占用者刚好在这一瞬间结束了（`_claim_holder` 顺手清掉了它的认领）：
                # 那份输入现在是空的，再建一次。
                continue
            if holder is None:
                # 第二次还是撞约束，而按认领查不到人 —— 认不出来就不猜，把原始错误
                # 抛出去（它是真异常，不是幂等命中）。
                raise
            log_print(
                f"AI 分析：同一份输入已有活动运行（run={holder.id}，"
                f"{holder.target_type}:{holder.target_key or holder.target_id}），"
                f"本次不重复发起（target={target_type}:{target_key or target_id}）",
                "AI",
                force=True,
            )
            raise ActiveAnalysisConflict(holder) from None
    # 循环里每一条分支都已经 return 或 raise，走到这里说明上面的控制流被改坏了。
    # 用 RuntimeError 而不是 assert：`python -O` 会把 assert 整句剥掉。
    raise RuntimeError("_create_run：重试路径没有收敛（这是一处代码缺陷，不是运行期状态）")


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

    # 正式分析的单次请求超时用平台默认（300 秒），**不是**探测用的 30 秒 —— 这两者
    # 混用会让分析必然超时，见 build_endpoint_client 的说明。2026-09-23 配置面收敛后
    # 这一栏不再可配（`endpoint_service.RETIRED_FIELDS`），这里直接读模型层默认常量；
    # 老配置行里存的值继续躺在库里，但已无人读取。
    client, errors = build_endpoint_client(
        project_id, {}, timeout_seconds=_coerce_timeout(DEFAULT_REQUEST_TIMEOUT_SECONDS)
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
            manifest=change.manifest,
            use_stored_batch_diff=(payload.get("mode") != "commit"),
            # 增量分析里每个变化文件的**比较基线**（上一轮看到的那条提交），键
            # `(latest_commit_id, file_path)`。有它时那个文件的 diff 是
            # 「上一轮 → 这一轮」两点对比，而不是整窗口的合并差异 —— 见
            # `platform_provider._delta_diff`。单提交分析不带（`delta_files` 只在周
            # 版本 payload 里）。
            delta_bases=_delta_bases(payload),
            # 每条 delta 来自哪一行周版本缓存（REV-AI-003）。有它时落库 diff 按行主键取，
            # 两个周窗口终点撞上同一条提交时不会取错窗口。
            cache_row_ids=_cache_rows(payload),
        ),
        "loaded": loaded,
        "scope": change.scope,
        "change_summary": change.summary,
        "limits": limits,
        # 周版本路径的「单次异常上限」用平台常量（`auto_sizing.ANOMALY_RUN_CAP`）：
        # 2026-09-23 收敛后这一栏不再可配，库里老行存的自定义值对周版本已失效。
        # 单提交分析继续读列（`RuleThresholds.from_config` 的原逻辑）。
        "thresholds": (
            replace(
                RuleThresholds.from_config(project_config),
                max_anomalies=ANOMALY_RUN_CAP,
            )
            if (payload.get("mode") or "") == "weekly"
            else RuleThresholds.from_config(project_config)
        ),
        "project_knowledge": project_config.get("project_knowledge") or "",
        "project_instructions": project_config.get("prompt_template") or "",
        # 用户**显式**点的全量：旧结论一个字都不进模型输入（判据与理由见
        # `baseline_source.run_ignores_history`）。注意 `suppressed` **不跟着关** ——
        # 那是用户的分类账，不是模型的输入。
        "baseline_digest": _baseline_digest(
            target_type, target_key, change, baseline_account=payload.get("baseline")
        ),
        # 每跑完一轮把累计用量写进进程内的进度快照（services/ai/run_progress.py，界面
        # 一边跑一边轮询它）。**它是给界面看的一眼，不是账** —— 账在 `_persist_outcome`。
        "on_round": lambda progress: _publish_progress(run.id, project_id, progress),
        # 第一次模型调用**之前**也报一帧。没有它，从开跑到第一轮跑完之间（可以是几分钟）
        # 快照是空的，界面显示「分析中：进度不可用」，而结论面板一直挂着
        # 「AI 分析进行中...」—— 看起来像卡住了，实际它正在干活。子代理模式下它还负责
        # 把归属从上一个分片换成汇总/主代理（见 subagent._call_engine 的 report）。
        "on_start": lambda progress: _publish_progress(run.id, project_id, progress),
    }
    # 子代理模式（services/ai/subagent.py）：默认关、只对周版本生效，不适用时返回 None
    # 走原来的单代理路径。`verify` 是对账轮，它依附在子代理上 —— 没开子代理时不生效。
    #
    # **数值参数读的是已落库的那份计划**（工作包 B，2026-09-23）：分片数、每片索取/轮次/
    # 每片异常全部来自 `payload["plan"]`，而那一份是 `build_weekly_payload` 算好、预估
    # 端点也读过的那一份。这里**不再现推一次** —— 现推就会出现「确认框说 5 片、实际跑 3 片」
    # 那种只体现在账目上的分叉（这正是这次要修的东西）。库里冻结的 `subagent_count`
    # 在界面上已收掉，不进这条路径。
    analysis_plan = plan_of(payload)
    if analysis_plan is None:
        # 老 payload（这次改动之前建的那一份）没有计划。**现算一份并写回去**，而不是
        # 退回旧公式：旧公式正是「3 个文件也开 5 片」的来源。补记之后这份计划也进
        # request_payload，事后能查「当时凭什么这么分工」。
        log_print("AI 分析：这份 payload 里没有计划，按当前口径现算一份并补记", "AI")
        analysis_plan = attach_weekly_plan(payload)
    sizing = (
        analysis_plan.family
        if (payload.get("mode") or "") == "weekly"
        and analysis_plan.mode == MODE_FAMILY
        and bool(project_config.get("subagent_enabled"))
        else None
    )
    if sizing is None and analysis_plan.members:
        # **单代理路径也要听计划的**：这一档的轮次/索取由计划里的成员决定（见
        # `analysis_plan.single_member_limits` 的说明）。
        engine_args["limits"] = single_member_limits(limits, analysis_plan)
    plan = plan_family(
        mode=payload.get("mode") or "",
        enabled=bool(project_config.get("subagent_enabled")),
        count=(analysis_plan.member_count if sizing is not None else 0),
        verify=bool(project_config.get("subagent_verify")),
        limits=limits,
        sizing=sizing,
    )
    if plan is not None:
        plan = apply_manifest(plan, change.manifest)
    budget_plan_payload = build_budget_plan(
        configured_prompt_chars=_configured_int(
            project_config.get("prompt_char_budget"), EngineLimits().prompt_char_budget
        ),
        # `limits.prompt_char_budget` 是**整份提示词**的额度（引擎按它比总长），而这一栏
        # 报的是**用户内容**的额度 —— 两者差一个平台内置提示词。减掉它让运行侧与预估端点
        # （`ai_usage_service.analysis_estimate` 的 `effective_prompt_chars`）说的是同一个数，
        # 否则界面会出现「配置 560,000 / 当前预估生效 580,000」这种读不通的对照。
        effective_prompt_chars=max(0, limits.prompt_char_budget - platform_chars),
        platform_chars=platform_chars,
        # 有家族计划时报**每片的名义额**（保底）；池账与推导依据走 `family_pool` 那一节。
        max_rounds=(plan.limits if plan is not None else limits).max_rounds,
        max_tool_requests=(plan.limits if plan is not None else limits).max_tool_requests,
        shard_count=plan.count if plan is not None else 1,
        verify=bool(plan and plan.verify),
        tool_limits=limits.tool_limits,
        window_note=budget_note,
        family_pool=(
            {
                "requests_pool": plan.quota.requests_pool,
                "rounds_pool": plan.quota.rounds_pool,
                "requests_nominal": plan.quota.requests_nominal,
                "rounds_nominal": plan.quota.rounds_nominal,
                "synthesis_floor_requests": MIN_MEMBER_TOOL_REQUESTS,
                "synthesis_floor_rounds": MIN_MEMBER_ROUNDS,
                "note": (sizing.note if sizing is not None else ""),
            }
            if plan is not None and plan.quota is not None
            else None
        ),
        # 计划本身（模式 / 分组 / 每成员额度 / 单次上限 / 两个预留 / 阈值与估算公式）。
        # **与预估端点返回的那一份逐字相同** —— 两边都是同一个 `payload["plan"]`。
        plan=analysis_plan.to_dict(),
    )
    outcome = (
        run_analysis(**engine_args)
        if plan is None
        else run_family_with_seed(
            plan=plan,
            # 每片开跑前看一眼预算，**两把尺子都要看**：
            #   * `early_stop_guard`：这个项目/这个月还有钱吗（既有闸门，判据与起跑同源）；
            #   * `make_single_run_guard`：**这一次**还能花多少（单次硬上限，
            #     「已花 + 本轮保守预留 + 收尾预留」）。超了就跳过这个成员并在报告的
            #     信息缺口里点名 —— 不再新增模型调用（那是「预算不足时报告可读」的判据）。
            should_skip=compose_skip_guards(
                early_stop_guard(project_id, entry="subagent"),
                make_single_run_guard(analysis_plan),
            ),
            **engine_args,
        )
    )

    result = result_payload(
        outcome,
        payload,
        suppressed=_suppressed(target_type, target_key, change),
        context_budget_note=budget_note,
        budget_plan=budget_plan_payload,
    )
    baseline_account = payload.get("baseline") or {}
    if baseline_account.get("kind") == "snapshot":
        previous = _previous_run(target_type, target_key)
        if previous is not None:
            previous_rows = [
                {
                    "fingerprint": row.fingerprint,
                    "title": row.title,
                    "category": row.category,
                    "severity": row.severity,
                    "confidence": row.confidence,
                    "evidence": row.evidence,
                    "commit_ref": row.commit_ref,
                    "file_path": row.file_path,
                    "impact": row.impact,
                    "suggestion": row.suggestion,
                    "disposition": row.disposition,
                }
                for row in AiAnalysisAnomaly.query.filter_by(run_id=previous.id).all()
            ]
            result = reconcile_incremental_result(
                result,
                previous_rows,
                changed_paths=change.paths,
                previous_run_id=previous.id,
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

    例外是**闸门**：`_blocked_sse` 发的是 `waiting`（周版本）或 `error`（单提交），
    两者都是**明确的**收尾 —— 界面知道「这次没有发起」，不会误读成静默断流。
    """
    try:
        yield from events
    except Exception as exc:  # noqa: BLE001 —— 这一层的存在意义就是兜住任何异常
        message = f"分析中断：{type(exc).__name__}: {exc}"
        log_print(f"❌ AI 分析的 SSE 流异常中断（{message}），已把原因发给界面", "AI", force=True)
        yield _sse_event("error", {"message": message})


def _blocked_sse(target_type: str, *, reason: str, message: str, run_id=None, extra=None) -> str:
    """「这一次**没有**发起分析」的一个 SSE 事件。两种事件名都是**明确的**（都不静默），
    差别只是收件人认哪一种：周版本那两份抽屉认识 `waiting`（能把「等待同步」与
    「已经有一次在跑」分开说，后者还要保持禁用并附着到那一次运行上）；单提交那份抽屉
    （`commit_diff_new.html`）不在本次改动范围内，它的 `error` 分支已能如实说
    「未开始 / 没有产生消耗」—— 给它发 `waiting` 只会变成「与服务器的连接中断了」，那是假话。

    `extra` 是给周版本抽屉的附加事实（如 `intent_registered`），**由服务端给**
    —— 界面按「服务端说这次没有发起、而且已经登记了」来决定保持禁用并轮询，不靠猜。
    """
    if target_type == "weekly":
        payload = {"reason": reason, "message": message, "run_id": run_id}
        payload.update(extra or {})
        return _sse_event("waiting", payload)
    return _sse_event("error", {"message": message})


def _already_running_message(run: AiAnalysisRun) -> str:
    return (
        f"这个目标已经有一次分析在进行中（运行 #{run.id}，"
        f"开始于 {_created_at_display(run)}），本次没有重复发起 —— "
        "重复发起会再花一次钱。"
    )


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

    # 项目级互斥（手动·单提交）：与周版本那条**同一道闸门、同一个说法**。
    # **要排除这个提交自己在跑的那一条** —— 那是 `already_running`，下面 `_create_run`
    # 会带着运行号把它判出来，界面据此附着上去接着看进度；在这里提前改成「项目忙」
    # 就把那个能力丢掉了（连按两次会变成一句「等它跑完」，而不是直接看到在跑的那条）。
    busy = project_gate.describe_active_analysis(
        project_id, exclude_target=("commit", commit_id)
    )
    if busy is not None:
        yield _blocked_sse(
            "commit", reason="project_busy",
            message=project_gate.project_busy_message(busy),
        )
        return

    payload = build_commit_payload(commit_id)
    try:
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
    except ActiveAnalysisConflict as conflict:
        # 同一个提交已经在跑（连按两次、或另一个标签页先按了）：**不重复发起**。
        # 这条路上没有「附着上去」这一步 —— 那份抽屉不在这一版的改动范围内，
        # 而它的 `error` 分支会如实说「分析没有发起，没有产生消耗」。
        yield _blocked_sse(
            "commit", reason="already_running",
            message=_already_running_message(conflict.run), run_id=conflict.run.id,
        )
        return
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


def run_weekly_analysis_background(
    config_id: int,
    task_id: Optional[int] = None,
    trigger_source: str = "scheduled",
    requested_mode: Optional[str] = None,
    focus: Optional[str] = None,
) -> dict:
    """后台跑一次周版本分析。

    `trigger_source`：这次运行**是谁发起**的，落进 `AiAnalysisRun.trigger_source`
    （用量面板按它显示「手动 / 定时」）。默认 `"scheduled"`（调度器排的）；
    被「等同步跑完就自动开始」的登记唤醒的那一次要传 `"manual"` —— 那是**用户点出来的**
    一次分析，只是被同步闸门推迟了。不传就记成「定时」是一条会误导人的账
    （用户明明点过，面板上却写着系统自己跑的）。

    ## `requested_mode` 与 `focus`：用户点的东西必须走到这里（第二波收尾补上的那一跳）

    这两个形参在 P0-01 之前**不存在**，于是「用户点了全量」与「用户选了仅配表」在
    POST → worker 这一跳上被静默丢掉：范围由平台自己裁决，而界面上写着「全量 /
    仅配表仓库」。默认值（`None`）让**老调用方逐字保持原行为**（平台自己裁决、不筛）。

    * `requested_mode == "full"` → **强制全量**。走的是 `build_weekly_payload(
      force_full=True, …)` —— 那是这条链上既有的、正确的入口（`force_full` 让
      `resolve_baseline` 永远返回 None，`_decide_scope` 于是判 `("full", "first_run")`），
      不在这里另塞一个变量把 `scope` 改掉。全量**不看基线**，所以也**不走**「没有变化
      就复用结论」那一支：用户要的是一份对目标快照的完整分析。
    * `focus` 非空 → 传给 `build_weekly_payload(focus=…)`（筛清单、筛补偿项、
      计数跟着走、范围名进提示词）。不传（`None`）时**读它那条 job**：job 行上有
      `focus`，而任务行上有 `job_id` —— 见 `job_service.focus_for_task`。

    **「用户点了全量、平台却想跑增量」不需要在调用模型前让用户确认**：那是同一个方向
    上的加强（全量包含增量），确认框在**发起之前**（`POST /jobs` 那条路上，P1-03 的
    成本预估在那里）。反过来（平台想把增量升成全量）的决策落在 worker 里，快照冻结
    之后，worker 没法同步问人 —— 那一档由 job 行的 `effective_mode` / `upgrade_reason`
    如实记账、报告里说明，**不装作问过**。
    """
    # **执行前再查一次开关。** `schedule_weekly_ai_analysis_tasks` 在建任务时查过，
    # 但任务一旦入队就独立于开关了：关掉自动分析**不会**取消已经排队的任务，而重启时
    # `load_pending_tasks` 还会把上次残留的 `processing` 任务改回 `pending` 重新入队
    # （`task_worker_service.py:1273-1279`）。于是「关掉开关 + 重启」必定跑一次 ——
    # 这正是用户报的「停止了自动分析后仍然自动触发」。
    #
    # 闸设在这里是安全的：本函数是**后台路径的唯一入口**（两个调用点都在
    # `task_worker_service`）。P0-01 之后**手动那一侧也走这条**（`POST /jobs` 建 job →
    # 任务 → 本函数），所以它不再只管「自动」—— 开关那一支的判据见下面 `trigger_source`
    # 那一行（手动来的照跑）。
    config = db.session.get(WeeklyVersionConfig, config_id)
    if config is not None:
        project_cfg = get_project_analysis_config(config.project_id)
        # **这个开关只管「自动」。** 用户点出来的那一次（`trigger_source="manual"`，
        # 例如被同步闸门推迟后由登记唤醒的那一次）不受它管 —— 同一个用户动作不该因为
        # 换了一条执行路径就被否掉。P0-01 之后抽屉里那个按钮走的就是这条（`POST /jobs`），
        # 它的触发来源是 `manual`。
        # 默认值取常量、不写字面量：`get_project_analysis_config` 总会带上这个键
        # （`row.resolved()` 或 `FIELD_DEFAULTS`），所以今天这个兜底**不会触发**；
        # 但它一旦写成与常量相反的字面量，将来某条路径真的缺键时就会静默走反默认。
        if trigger_source != "manual" and not project_cfg.get(
            "auto_weekly_enabled", DEFAULT_AUTO_WEEKLY_ENABLED
        ):
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

    # 用户选的「分析范围」：显式给的优先，否则读它那条 job（任务行上有 `job_id`）。
    focus = _weekly_focus_for_task(task_id, focus)
    if _requests_full_analysis(requested_mode):
        # 用户说的「全量」是**指令**：不看基线、也**不**走「没有变化就复用结论」那一支。
        payload, state, skip_reason = build_weekly_payload(
            config_id, force_full=True, focus=focus
        )
    else:
        payload, state, skip_reason = build_weekly_payload(config_id, focus=focus)
        if skip_reason == "no_change":
            # 与流式入口同一条口径（见那里的注释）：有可复用的结论就**不建 run**。
            reusable = _reusable_conclusion(config_id, state)
            if reusable is not None:
                return {
                    "status": "skipped",
                    "reason": "no_change",
                    "reused_run_id": reusable.id,
                }
            payload, state, skip_reason = build_weekly_payload(
                config_id, force_full=True, focus=focus
            )
    if payload is None:
        # **把真实的理由带出去**（`no_configs` / `focus_empty`）：一律压成 `payload_empty`
        # 会让「你选的范围里没有改动过的文件」与「这个分组一条配置都没有」在 job 上
        # 长得一样，而用户该做的事完全不同。
        return {"status": "skipped", "reason": skip_reason or "payload_empty"}

    project_id = payload["group"]["project_id"]
    if not _get_project_api_key(project_id):
        return {"status": "skipped", "reason": "missing_api_key"}

    group_key = payload["group"]["key"]
    try:
        run = _create_run(
            project_id=project_id,
            target_type="weekly",
            target_id=config_id,
            target_key=group_key,
            response_mode="blocking",
            scope=payload.get("scope", "full"),
            # 归一化：这一列是 String(20)，而且用量面板按 `scheduled` / 其余分「定时 / 手动」
            # —— 认不出来的值一律记成 `scheduled`（不把脏值写进库）。
            trigger_source=trigger_source if trigger_source in ("manual", "scheduled") else "scheduled",
            payload=payload,
        )
    except ActiveAnalysisConflict as conflict:
        # 手工那一次正拿着这份输入的认领（实测里那条：「手工运行结束时，同一分组的
        # 后台 AI 任务仍在 pending；没有数据库幂等键阻止它再跑一次」）。
        # **跳过且不推进水位线**：水位线不动，下一个调度周期会重新排队 —— 与上面
        # 同步闸门那道同一个口径（推进了就变成「跳过这一次，这一周都不再尝试」）。
        log_print(
            f"周版本自动分析推迟（already_running）: config_id={config_id} "
            f"—— 已有活动运行 run={conflict.run.id}",
            "AI",
            force=True,
        )
        return {
            "status": "skipped",
            "reason": "already_running",
            "message": _already_running_message(conflict.run),
            "run_id": conflict.run.id,
        }

    # run 一创建就与 job 绑定；否则第一轮回调发生时持久化进度找不到接收行。
    if task_id is not None:
        try:
            from models import BackgroundTask
            from services.ai import job_service

            task_row = db.session.get(BackgroundTask, task_id)
            if task_row is not None and getattr(task_row, "job_id", None):
                policy = payload.get("policy") or {}
                runtime_reason = ""
                if (
                    str(requested_mode or "").strip().lower() == "incremental"
                    and run.scope == "full"
                ):
                    runtime_reason = str(policy.get("reason") or "runtime_scope_upgrade")
                job_service.mark_running(
                    task_row.job_id,
                    run_id=run.id,
                    task_id=task_id,
                    effective_mode=run.scope,
                    upgrade_reason=runtime_reason,
                )
                db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            log_print(f"⚠️ 绑定 AI run 与 job 失败（不影响分析）: run={run.id} {exc}", "AI", force=True)

    result = _execute_analysis(
        run,
        project_id=project_id,
        payload=payload,
        project_config=get_project_analysis_config(project_id),
        target_type="weekly",
        target_key=group_key,
    )

    _update_weekly_state(payload, run, state, engine_status=result.get("status"))
    # **这里的二态是有意的，不要顺手改成三态。** 它报的是「这个**后台任务**成没成」，
    # 收件人 `task_worker_task_handlers` 只认 `succeeded` / `skipped`，其余一律落
    # `failed` —— 把 `degraded` 原样传出去会让每一次降级分析都变成一条失败任务。
    # 「这次交付降级了没有」记在 `run.status` / `run.degradation` 上（见 `_persist_outcome`）。
    return {
        "status": "succeeded" if result.get("status") != "failed" else "failed",
        "run_id": run.id,
        "error_message": result.get("error_message") or None,
    }


def _requests_full_analysis(requested_mode) -> bool:
    """用户**明确**要求全量吗。

    只认 `full`（`models.ai_analysis.MODE_FULL` 那一个字面值）：`None` / `incremental`
    / 认不出来的值一律返回 False —— 这一支的默认行为是「平台自己裁决」，
    把脏值当全量的后果是**静默多花钱**（全量不看基线、不增量）。
    """
    return str(requested_mode or "").strip().lower() == MODE_FULL


def _weekly_focus_for_task(task_id, focus):
    """这次分析的范围：显式给的优先，否则读它那条 job。

    `focus` 只落在 `AiAnalysisJob.focus` 上，而任务行上有 `job_id` —— 所以执行侧
    只要拿到 `task_id` 就能把用户选的范围读回来，**不需要改
    `create_weekly_ai_analysis_task` / `register_waiting_analysis_intent` 的签名**。
    取用与判据都在 `job_service.focus_for_task`（含「任务行上那一列可能是意图 id」
    那一坑的处置）。

    读不到就返回 `None` = **不筛**：范围是**缩窄**输入的东西，读不到它只会让这次分析
    看全，不会让它看漏（看漏才是那个「静默把一半输入丢掉」的缺陷）。
    """
    if focus:
        return focus
    if task_id is None:
        return None
    try:
        from services.ai.job_service import focus_for_task

        return focus_for_task(task_id)
    except Exception as exc:  # noqa: BLE001 —— 读不到范围不该让这次分析起不来
        log_print(
            f"⚠️ 周版本分析：读不到这次分析的范围（按「不筛」处理）: "
            f"task_id={task_id}, {type(exc).__name__}: {exc}",
            "AI",
            force=True,
        )
        return None


def _update_weekly_state(
    payload: dict,
    run: AiAnalysisRun,
    state: Optional[AiWeeklyAnalysisState],
    *,
    engine_status: Optional[str],
) -> None:
    """推进这个周版本分组的指针（**三路**，判据是引擎状态）。

    ## 为什么判据只能是 `engine_status`

    它是「模型这次**读全了没有**」的判据，而 `run.status` 回答的是「这次**交付**是什么
    形态」（succeeded / degraded / failed，见 `_persist_outcome`）—— 两把尺子。降级
    （例如「上下文索取额度用尽，基于已有证据出结论」）恰恰是最不该推进时间水位线的
    那一类：线上有一次 767 个文件里有 748 个 `.lua` 的 diff 根本没读到，却被标成
    「已分析」，增量从此只看得到水位线之后的新文件。

    ## 三路（AI-P0-02 把「一个值当三件事用」拆开了）

    * **succeeded** → 时间水位线 + 运行号 + 结论基线 + （达标时）完整覆盖指针 + 指纹；
    * **degraded** → **只推结论基线**（且必须是结构化结论）+ 指纹；时间水位线与完整覆盖
      指针都不推 —— 降级结论可以当下一轮的基线，但**不能伪装为完整覆盖**；
    * **失败 / 认不出来的状态** → 一个都不推，**连指纹也不写**（写了指纹，调度器下一轮
      就会以「输入逐字相同」跳过它，而这个版本其实一次都没跑成）。

    具体实现在 `services/ai/baseline_blocks.advance_weekly_state`（本文件贴着长度闸门）。
    """
    advance_weekly_state(payload, run, state, engine_status=engine_status)


def _reusable_conclusion(
    config_id: int, state: Optional[AiWeeklyAnalysisState]
) -> Optional[AiAnalysisRun]:
    """没有新变化时该复用的那条结论。

    **先看状态行的结论基线指针**（`last_concluded_run_id`），它才是「上一轮那份可复用的
    结论」；指针指不到或那条不可用了，才退回读侧口径（`get_latest_weekly_result`）。

    复用的前提是 `_is_run_fresh`：交付形态有结论 + 有内容 + 没过保留期 + 溯源一致。
    **复用不建 run**，所以它同时也是「没有新变化不花钱」这条验收的实现位置。
    """
    pointer = getattr(state, "last_concluded_run_id", None) if state else None
    candidate = db.session.get(AiAnalysisRun, pointer) if pointer else None
    if candidate is not None and _is_run_fresh(candidate):
        return candidate

    cached = get_latest_weekly_result(config_id)
    run_id = (cached or {}).get("run_id") if isinstance(cached, dict) else None
    fallback = db.session.get(AiAnalysisRun, run_id) if run_id else None
    if fallback is not None and _is_run_fresh(fallback):
        return fallback
    return None

