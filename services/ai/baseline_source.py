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

## 唯一的例外：用户**显式**点的全量（`run_ignores_history`）

那一档 `baseline_digest` 返回空串（旧结论一个字都不进模型输入），而 `suppressed` **照常生效**。
这**不是**破坏上面那条同源规则，是一个**刻意的产品决定**，理由写两遍免得下一个人修回去：

* 「用户点了全量」的语义是**从零重判** —— 旧结论一旦进了提示词，模型就会把它当既成事实
  复述。实测有过一次：报告把两条**从未存在过**的物品 ID 写成「本期删除」的高风险，
  根因就是旧结论被注入了全量运行。
* 而「人工忽略」是**用户的分类账**，不是模型的输入。用户已经判定「这条不用再报」，
  重跑一次不该把他的话作废 —— 所以他忽略过的条目在全量里**仍然被抑制**。
* 上面那条同源规则要防的是「报告里被抹掉、摘要里也没说」这种**两头不靠**。全量档里
  摘要整段不出现，不存在「没说」的问题；被抑制的条目是用户自己要求抹掉的，不是平台
  静默抹掉的。

判据取**运行账上的 `reason`**，不是 `run.scope`：平台把增量**升格**成全量（`delta_ratio_high`
等）时，做差基线仍在同一快照上、结论可比，那时**必须**照常注入 —— 有测试钉着
（`tests/test_ai_analysis_service.py::test_the_second_run_carries_the_first_runs_findings_as_a_baseline`）。
能区分这两种「全量」的只有 `payload["baseline"]` 的 `reason`。
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from services.ai.baseline import (
    DISPOSITION_PENDING,
    FORCE_FULL_REASON,
    FORCE_FULL_REBUILD_REASON,
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


def previous_anomaly_rows(run_id: int) -> List[dict]:
    """上一轮结论的**完整行形状**（喂给 `incremental_baseline.reconcile_result`）。

    ## 为什么键清单只能有一份（真机实测，2026-09-24）

    这张表原先是在调用处**手抄**的（`ai_analysis_service` 里那段字典字面量），抄下来的
    那份少了 `claims` —— P0-01 落库的原子断言。于是 `incremental_baseline` 抬头那句承诺
    「历史结论的断言清单原样带回」在真机上是空的：

        run 62（增量）的 12 条继承项，`claims` 全是 `[]`；其中 3 条在上一轮（run 61）
        明明带着 3264 / 2210 / 3927 字节的断言。

    而 `_historical_anomaly` 读的是 `row.get("claims")`：键不在就**回空数组**，不报错、
    不告警 —— 那三条结论「当时凭什么算核实过了」在下一轮静默消失（下一轮基线、导出、
    面板都读它）。

    所以这里直接给 `AiAnalysisAnomaly.to_dict()` 的结果：**读侧那一份形状就是键清单**，
    它已经带 `claims`，以后新增字段也不必回来改第二处（手抄的那份必然会再漏一次）。
    """
    return [
        row.to_dict()
        for row in AiAnalysisAnomaly.query.filter_by(run_id=run_id).all()
    ]


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


def run_ignores_history(baseline_account: Optional[Mapping[str, Any]]) -> bool:
    """这次运行要不要**从零重判**（旧结论一个字都不进模型输入）。

    只认运行账上 `reason == "force_full"` 这一种 —— 那是**用户显式点了全量**留下的标记
    （`services/ai/baseline_blocks.py::resolve_baseline` 写的）。**不要**改成看
    `run.scope == "full"`：平台把增量升格成全量时 `scope` 也是 `full`，但那时做差基线
    仍在同一快照上、结论可比，旧结论**必须**照常注入（有测试钉着，见模块 docstring）。
    老分组的 `watermark` 过渡态同样不关 —— 它不是用户要的全量。

    **平台自己升的那次全量重看不在此列**（`FORCE_FULL_REBUILD_REASON`，2026-09-25 加的）：
    那一档用户点的是增量，只是「输入没变、而上一轮结论不可复用」，旧结论一条都没失效 ——
    它必须照常注入（真机 run 68 借用了这一档的语义，报告退化成首跑、继承 0 条）。所以这里
    **只认 `FORCE_FULL_REASON` 这一个字面值**，不要写成「reason 以 force_full 开头」之类。

    `None` / 缺键 / 脏值一律**不关**（保守：宁可多带一次旧结论，也不要因为账上一个
    字段没读到就静默改变一次全量评审的输入）。
    """
    if not isinstance(baseline_account, Mapping):
        return False
    return str(baseline_account.get("reason") or "") == FORCE_FULL_REASON


def has_previous_conclusion(baseline_account: Optional[Mapping[str, Any]]) -> bool:
    """这次运行**有没有上一轮结论可以对照** —— 决定要不要做「历史结论延续（平台）」那一节。

    **判据不是「做差基准是不是快照」**（那是取数侧的事）。平台自己升的那次全量重看
    （`FORCE_FULL_REBUILD_REASON`）做差基准是 `None`，但上一轮结论仍在、这份报告仍然是
    「这个版本截至现在的这一份」—— 少了它，那一节连同「需要重新确认」的清单一起从报告里
    消失（真机 run 68 就是这个形状）。用户显式点的全量则相反：它要求从零重判，
    `baseline_digest` 整段不给，对账也就无从谈起。
    """
    if not isinstance(baseline_account, Mapping):
        return False
    if str(baseline_account.get("reason") or "") == FORCE_FULL_REBUILD_REASON:
        return True
    return str(baseline_account.get("kind") or "") == "snapshot"


def baseline_digest(
    target_type: str,
    target_key: Optional[str],
    change: ChangeSet,
    *,
    baseline_account: Optional[Mapping[str, Any]] = None,
) -> str:
    """给模型看的「已经报过的问题」。取不到就是空串（提示词里那一段整个不出现）。

    `changed_paths` 传「上次报过、这次又变了」的文件：那类结论的证据已经过期，要重新
    确认 —— 包括人工标过「已忽略」的。这是「忽略」不会变成「永远看不见」的保证。

    **先 `classify` 再渲染，两件事必须分开做**：`build_baseline_digest` 刻意不收
    `changed_paths`，因为它再判一遍状态会把刚判成「需要重新确认」的结论判回「已忽略」
    并从摘要里抹掉 —— 而且是静默的（报告里只是少一条）。

    `baseline_account` 传本次运行的基线账（`payload["baseline"]`）。用户**显式**点全量时
    直接返回空串（理由与产品决定见模块 docstring）。它是**关键字可选**的：只传三个位置
    参数的调用方（既有测试与老代码）行为逐字不变。
    """
    if run_ignores_history(baseline_account):
        return ""
    findings = baseline_findings(target_type, target_key)
    if not findings:
        return ""
    skipped = skipped_unstructured_runs(
        target_type, target_key, previous_run(target_type, target_key)
    )
    note = ""
    # 平台自己升的那次全量重看：清单照样给（判据见 `run_ignores_history`），但要交代
    # **这一轮的口径变了** —— 重看一遍的全部意义就是按新规则重判，模型不知道就会照抄
    # 旧结论的等级与依据。
    reason = str((baseline_account or {}).get("reason") or "")
    if reason == FORCE_FULL_REBUILD_REASON:
        note = (
            "（本轮是**全量重看**：上一轮那份结论本轮不能直接复用（评审规程或模型换过，"
            "或者它没留下可比对的结论），平台把这份内容整个重看了一遍。下面这份清单是"
            "本版本截至上一轮报过的问题 —— 逐条给出现在的状态，**并按本轮的规则重新判断**"
            "它的等级与依据，不要照抄旧等级。）"
        )
    if skipped:
        note += (
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
