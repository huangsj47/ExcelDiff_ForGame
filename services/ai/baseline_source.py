"""基线的**读侧**：从库里取「上一次为止的结论」，做成这一轮要用的那两样东西。

## 为什么要与 `baseline.py` 分开

`baseline.py` 是一层**纯函数**（它的模块抬头写死了：「本模块不读文件、不碰数据库」），
所以「哪一次运行算基线、上一批结论长什么样」这件事不能写在它里面。这一层就是那件事，
它把 DB 行翻译成 `BaselineFinding`，再交给 `baseline.py` 去分类与渲染。

从 `services/ai_analysis_service.py` 拆出来：那个文件贴着仓库的 2000 行硬上限
（`scripts/check_file_length.py --strict`），而这一组函数只依赖 run / anomaly 两张表与
`baseline.py`，与「怎么跑一次分析」没有耦合。

## 两个入口的产出

* `baseline_digest(...)` → 塞进提示词的那段「已经报过的问题」
* `suppressed(...)` → 本轮**不该再进报告**的指纹（人工已忽略且文件没再变的）

两者都从 `baseline_findings()` 出发。**它们必须用同一批输入**：一边判「这条还要不要看」、
另一边渲染「这条已报过」，两边取值不同的话，会出现「报告里被抹掉了、但摘要里也没说」
这种两头不靠的条目。
"""

from __future__ import annotations

from typing import List, Optional

from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from services.ai.baseline import (
    DISPOSITION_PENDING,
    BaselineFinding,
    build_baseline_digest,
    classify,
    suppressed_fingerprints,
)
from services.ai.change_set import ChangeSet
from services.ai.run_cache_source import CONCLUDED_STATUSES


def previous_run(target_type: str, target_key: Optional[str]) -> Optional[AiAnalysisRun]:
    """最近一条**可以当基线**的运行。

    ## 为什么要卡 `conclusion_structured`

    `baseline.py` 把上一次那批结论当成「这个版本当前仍成立的问题全集」。这个语义只在
    结构化结论上成立：只有一份 markdown 的那次（`DEGRADE_MARKDOWN`，模型没按协议给
    JSON）**一条结构化结论都没有**，拿它当基线会得出「共 0 条：无」，于是模型把上次
    报过的问题全部当新发现重报一遍 —— 而且没有任何地方说得出这是为什么。

    所以这里**往回找**最近一条真的产出了结构化结论的运行，而不是止步于「最近一条
    status 是 succeeded 的」。往回退不等于把历次并起来（`baseline_findings` 的注释
    解释了为什么不能并）：被跳过的那些运行什么结论都没留下，退到上一条完整的
    「问题全集」正是本来该用的那份。

    ## status 那一半：`degraded` **也是**「跑完了、有结论」

    这一条原来写的是 `.filter(status == "succeeded")`。写入侧把 `status` 改成原生区分
    succeeded / degraded / failed 之后，它把**所有降级运行**挡在基线之外 —— 而
    `conclusion_structured` 那一半正好相反：有结构化 payload 的降级被写入侧判成「有
    结论」（见 `tests/test_ai_baseline_needs_structured_conclusion.py` 的 docstring：
    「有 payload 的降级**应当**能当基线」）。两边自相矛盾的后果是上一批已知问题全部
    被当新发现重报，而基线看上去只是「上上次那批」——完全看不出发生过什么。

    「跑完了、有结论」= `CONCLUDED_STATUSES`（succeeded / degraded），与读侧其余五处
    同一份口径；「这次是不是浅的」由 `conclusion_structured` 单独判，两把尺子分开。

    跳过了就更要说 —— 那句说明由 `baseline_digest` 加在摘要里。
    """
    if not target_key:
        return None
    return (
        AiAnalysisRun.query.filter_by(target_type=target_type, target_key=target_key)
        .filter(AiAnalysisRun.status.in_(CONCLUDED_STATUSES))
        # NULL（失败/未完成，以及加列之前的历史行）一律不算：拿不准就不当基线。
        .filter(AiAnalysisRun.conclusion_structured.is_(True))
        .order_by(AiAnalysisRun.created_at.desc())
        .first()
    )


def skipped_unstructured_runs(
    target_type: str, target_key: Optional[str], baseline: Optional[AiAnalysisRun]
) -> int:
    """比 `baseline` 更新、却没留下结构化结论的那几条运行数。

    只用于在基线上如实说一句「中间有一次分析没给出可比对的结论」。没有它的话，
    那次降级在这条链路上是完全静默的：基线看上去就是「上上次那批」，用户不知道
    中间那次白跑了。

    （status 这一半与 `previous_run` 同一份口径：**有结论的两种形态**都要数进来，
    否则「只有 markdown 的那次降级」被跳过时这句话永远不出现 —— 而那正是最需要
    说一句的情形：它是降级里唯一一种真的没给出结论的。）
    """
    if not target_key or baseline is None:
        return 0
    since = baseline.finished_at or baseline.created_at
    query = (
        AiAnalysisRun.query.filter_by(target_type=target_type, target_key=target_key)
        .filter(AiAnalysisRun.status.in_(CONCLUDED_STATUSES))
        .filter(AiAnalysisRun.conclusion_structured.is_(False))
    )
    if since is not None:
        query = query.filter(AiAnalysisRun.created_at > since)
    return query.count()


def baseline_findings(target_type: str, target_key: Optional[str]) -> List[BaselineFinding]:
    """上一次**可以当基线**的那次运行报出的那批结论。

    **取「上一次运行的那批」而不是把历次运行并起来**：每次运行产出的本来就是「这个版本
    当前仍成立的问题全集」（skill 里定死了这个语义），所以上一次那批就是当前基线。
    并起来反而会把已经修好的旧条目重新翻出来。
    """
    previous = previous_run(target_type, target_key)
    if previous is None:
        return []
    return [
        BaselineFinding(
            fingerprint=row.fingerprint or "",
            title=row.title or "",
            severity=row.severity or "high",
            category=row.category or "",
            file_path=row.file_path or "",
            commit_ref=row.commit_ref or "",
            disposition=row.disposition or DISPOSITION_PENDING,
        )
        for row in AiAnalysisAnomaly.query.filter_by(run_id=previous.id).all()
        if row.fingerprint
    ]


def baseline_digest(target_type: str, target_key: Optional[str], change: ChangeSet) -> str:
    """给模型看的「已经报过的问题」。取不到就是空串（提示词里那一段整个不出现）。

    `changed_paths` 传「上次报过、这次又变了」的文件：那类结论的证据已经过期，要重新
    确认 —— 包括人工标过「已忽略」的。这是「忽略」不会变成「永远看不见」的保证。

    **先 `classify` 再渲染，两件事必须分开做**：`build_baseline_digest` 刻意不收
    `changed_paths`，因为它再判一遍状态会把刚判成「需要重新确认」的结论判回「已忽略」
    并从摘要里抹掉 —— 而且是静默的（报告里只是少一条）。
    """
    findings = baseline_findings(target_type, target_key)
    if not findings:
        return ""
    skipped = skipped_unstructured_runs(
        target_type, target_key, previous_run(target_type, target_key)
    )
    note = ""
    if skipped:
        note = (
            f"（这中间有 {skipped} 次分析**没有给出可比对的结论**"
            "——模型没按协议输出，只留下一份 markdown 报告。所以上面的清单是更早那次"
            "留下的，**不代表这中间没有问题**。）"
        )
    return build_baseline_digest(classify(findings, changed_paths=change.paths), note=note)


def suppressed(target_type: str, target_key: Optional[str], change: ChangeSet) -> frozenset:
    """人工已忽略、且相关文件没有再变的指纹。这些不再进清单。"""
    findings = baseline_findings(target_type, target_key)
    if not findings:
        return frozenset()
    return suppressed_fingerprints(classify(findings, changed_paths=change.paths))
