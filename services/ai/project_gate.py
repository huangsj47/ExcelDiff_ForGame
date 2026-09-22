"""项目级互斥：同一个项目下，同时只允许一条 AI 分析在跑。

## 为什么要有这一层

已有的两层互斥都是 **target 级**的：job 级按 `target_type|target_key|focus` 算
（`job_service.active_key_for`），run 级按**快照内容**算。于是「同一个目标重复点」会被
附着到同一条 job 上（那是好行为，别动它），但**同一项目下两个不同目标可以同时跑** ——
一次分析约 3.4 元，用户要的是「这个项目里有分析在跑，就别再让我触发新的」。

## 判据必须同时查两张表

`AiAnalysisJob` 有 `project_id`（NOT NULL），按项目过滤零 join。**但单提交那条入口根本
不建 job** —— 它直接进 `_create_run`（`ai_analysis_service.stream_commit_analysis`）。
只查 job 表会漏掉那一路，而漏掉的症状正是「项目里其实在跑，界面却说没有」。

## 定时路径不受这一层约束

它只在**两个手动入口**上判定（`job_service.create_or_attach_job` 与
`stream_commit_analysis`）。定时是平台自己的调度，若也被「项目里有分析在跑」挡住，
一次卡住的手动分析会把该项目后面的定时分析全部挡掉（`STALE_RUNNING_SECONDS` 要一小时
才放开）。定时撞上正在跑的分析时，走既有的 run 级闸门即可。
"""

from __future__ import annotations

from models.ai_analysis import AiAnalysisJob, AiAnalysisRun
from models.ai_analysis.job import ACTIVE_JOB_STATES

#: run 的活动态。`succeeded` / `degraded` / `failed` 都是终态。
ACTIVE_RUN_STATES = ("pending", "running")

#: 给用户看的名字。`job.target_type` 用的是模型层那两个取值。
_TARGET_LABELS = {"weekly": "周版本分析", "commit": "单提交分析"}


def _job_label(job) -> str:
    return _TARGET_LABELS.get(str(getattr(job, "target_type", "") or ""), "AI 分析")


def _active_runs(project_id: int) -> list:
    """这个项目里还活着的 run。

    **僵尸 running 不算**（`AiAnalysisRun.is_stale_running`）：进程被杀留下的那一条
    永远不会自己结束，拿它挡住新分析等于让用户干等一小时 —— 而那个进程已经不存在了。
    """
    runs = (
        AiAnalysisRun.query.filter(
            AiAnalysisRun.project_id == project_id,
            AiAnalysisRun.status.in_(ACTIVE_RUN_STATES),
        )
        .order_by(AiAnalysisRun.created_at.desc())
        .all()
    )
    return [run for run in runs if not run.is_stale_running]


def describe_active_analysis(
    project_id, *, exclude_active_key=None, exclude_run_id=None, exclude_target=None
):
    """这个项目现在有没有分析在跑。有就回一条**给用户看的**说明，没有回 `None`。

    `exclude_*` 不是可有可无的：**「同一目标重复点会附着到那条在跑的 job」是既有契约**
    （`create_or_attach_job` 第 2 步），而附着那一瞬间那条 job 本来就是活的 ——
    不把它排除掉，附着会被自己刚认领的那条 job 挡住，等于把既有行为改坏。

    * `exclude_active_key`：跳过「**这次请求本来就该附着上去**」的那条 job。
      按**输入指纹**排除而不是按 job_id：并发那一下（第一次查的时候对手还没提交）
      根本拿不到对方的 id，而那正是这条排除存在的理由。
    * `exclude_target` / `exclude_run_id`：跳过**这个请求所针对的那一个目标**自己。
      同一个目标在处理中，由既有的两层闸门裁决（job 的 `active_key` 唯一索引、
      run 的认领唯一索引），结论是「附着 / skipped-already_running」而不是「项目忙」——
      在这里拦住会把那套正确行为改坏。这道闸门管的是**别的目标**。

    **这条排除规则不是可选的**：两条既有回归用例正好从两侧钉住它 ——
    `test_ai_job_protocol` 的并发同 `active_key` 那条，与
    `test_ai_run_lifecycle_idempotency` 的「手工入口撞上同输入的 run」那条。
    """
    if not project_id:
        return None

    query = AiAnalysisJob.query.filter(
        AiAnalysisJob.project_id == project_id,
        AiAnalysisJob.state.in_(tuple(ACTIVE_JOB_STATES)),
    )
    if exclude_active_key is not None:
        query = query.filter(AiAnalysisJob.active_key != exclude_active_key)
    job = query.order_by(AiAnalysisJob.id.desc()).first()
    if job is not None:
        return {
            "kind": "job",
            "job_id": job.id,
            "run_id": job.run_id,
            "target_type": job.target_type,
            "label": _job_label(job),
        }

    for run in _active_runs(project_id):
        if exclude_run_id is not None and run.id == exclude_run_id:
            continue
        if exclude_target is not None and (
            str(run.target_type or ""), run.target_id
        ) == (str(exclude_target[0]), exclude_target[1]):
            continue
        return {
            "kind": "run",
            "job_id": None,
            "run_id": run.id,
            "target_type": run.target_type,
            "label": _TARGET_LABELS.get(str(run.target_type or ""), "AI 分析"),
        }
    return None


def project_busy_message(info) -> str:
    """拒绝时说的那句话。**要说清「在跑的是哪一条」与「为什么不能一起跑」** ——
    只说「忙碌中」的话，用户会反复再点，而每一次点击都在试探同一道闸门。
    """
    info = info or {}
    label = info.get("label") or "AI 分析"
    # 刚建的 job 还没有运行号（worker 要等同步跑完才建 run），那时报的是任务号 ——
    # 编一个「运行号 None」出来，用户拿着它去哪儿都查不到。
    where = (
        f"运行号 {info['run_id']}" if info.get("run_id") else f"任务号 {info.get('job_id')}"
    )
    return (
        f"这个项目已经有一次「{label}」在跑（{where}）。"
        "等它跑完再发起新的：一次分析会实际计费，同时跑两条既不会更快，"
        "账也是各算各的。"
    )
