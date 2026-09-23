"""读侧：某个目标「最近一次分析」的取数与形态。

## 为什么单独一个模块

从 `services/ai_analysis_service.py` 拆出来：那个文件贴着仓库的 2000 行硬上限
（`scripts/check_file_length.py` 的 `ERROR_THRESHOLD`）。这一段本身与「怎么跑一次分析」
没有耦合 —— 它只读 run 行、读几个版本标签函数，再按读侧口径拼出 `/latest` 的响应体，
所以拆开之后两边的测试都还在原地。

## 这里的核心口径：**库里只要有结论，就必须看得见**

判定顺序见 `_read_latest_result` 的 docstring。三条容易改错的地方单独记在这里：

* 溯源自（提示词 / skill / 规则 / 模型的版本号）只决定「这份结论能不能拿来**跳过重跑**」，
  不决定「这份结论还**看不看得到**」—— 混用会让改一次提示词就把所有历史结论变成
  「未分析」，而结论一直躺在库里（`_latest_concluded_run` 的 docstring）；
* `degraded` 也是「有结论」：它跑完了、有报告正文、有结构化结论形态，只是浅。
  与 `run_cache_source.CONCLUDED_STATUSES` 是同一份清单，两处必须一起改；
* 「取不到」与「没有结论」是两件事，`_read_latest_result` 第 4 步才轮到「真的没分析过」。

## 与 `conclusion_view` 的分工

`services/ai/conclusion_view.py` 定的是**一条 run 长什么样**（`_conclusion_payload` 等），
本模块定的是**哪一条 run 该被看见**。所以形态函数都从那边取，这里不重写一份。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from models import Commit, Repository, WeeklyVersionConfig, db
from models.ai_analysis import AiAnalysisRun
from services.ai.conclusion_view import (
    _conclusion_payload,
    _created_at_display,
    _in_progress_result,
    _last_attempt_failed_result,
    _parse_response_payload,
)
from services.ai.job_service import FOCUS_ALL
from services.ai.project_config_source import build_weekly_group_key
from services.ai.provenance import current_provenance
from services.ai.run_cache_source import (
    CONCLUDED_STATUSES,
    _analysis_cache_cutoff,
    _is_run_fresh,
)


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
    """该目标最近一条**真的有结论**的运行。**刻意不看溯源。**

    溯源自（提示词 / skill / 规则 / 模型的版本号）只该决定「这份结论能不能拿来跳过
    重跑」—— 那是省一次调用的事；不该决定「这份结论还看不看得到」—— 那是用户以为
    自己的分析白做了的事。两者混用一把尺子的后果已经出现过：改过 `prompt.py` 或
    `SKILL.md` 之后，所有历史结论在界面上一起变成「未分析」，而结论一直躺在库里。

    ## 「有结论」= succeeded 或 degraded

    `run.status` 回答的是「这次**交付**是什么形态」：`degraded` = 有结论但浅（有报告
    正文、有结构化结论形态），**与 succeeded 同等对待**；`failed` 才是没有结论。写入侧
    已经原生区分三态，这里若不跟着放宽，降级运行在 `/latest` 上整条看不见 —— 界面拿到
    「没有结果」还会去自动开跑一次（**再花一次钱**）。

    ## 这一条**必须**和 `run_cache_source._is_run_fresh` 一起改

    `_read_latest_result` 第 1 步用 `_is_run_fresh` 判「最新那条能不能直接用」，第 3 步
    才退到这里。只放宽这一处的话：第 1 步仍判不可用，第 3 步又命中**同一条**记录，于是
    `concluded.id == run.id` 成立 → 判成 `STALE_REASON_RULES_CHANGED` → 界面对用户说
    「该结论由旧版评审规程产出（提示词/规则/模型已更新）」。那是一句**假话** ——
    提示词一个字都没改，变的是我们把 degraded 写进了 `status` 列。
    """
    run = (
        AiAnalysisRun.query.filter(*conditions)
        # `degraded` 也是「有结论」：它跑完了、有报告正文、有结构化结论形态，只是浅。
        # `run.status` 回答的是**交付形态**，不是「模型读全了没有」（那看引擎状态）。
        # 见 `run_cache_source.CONCLUDED_STATUSES`，以及本函数 docstring 里「必须一起改」那段。
        .filter(AiAnalysisRun.status.in_(CONCLUDED_STATUSES))
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
