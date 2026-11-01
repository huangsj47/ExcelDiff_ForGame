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

## 范围链断了，旧结论就不继承（P1，2026-11-01）

`previous_run` 原先只按 `target_type/target_key` 取最近一条有结论的运行。可分析范围是
会变的（`analysis_profile` / `focus` / 仓库集），范围一变，上一轮那批结论回答的就是
**另一批文件**的问题：实测把 `profile=all` 下代码仓库的 critical 当基线、再以
`profile=tables` 跑，那条 critical 被原样继承进新报告，风险等级也被它抬高。

第一版修法（「签名不同就跳过、继续往回找」）又开了个更贵的口子。真实形状：

    A（tables）报了问题 X → B（all）改了那张表、X 修好了 → C（tables）只改了另一张表

C 开始分析时，候选序列是 B、A。跳过 B 取 A，A 的 X 会原样回到 C 的报告里 ——
**B 的修复被 A 的旧结论覆盖**，而 B 正好夹在中间：只有它知道 X 已经不成立了。

所以判据是**链式**的，不是「找最近一条同范围的」：调用方给出 `scope_signature`
（见 `analysis_scope_filter`）时，只看**最近一条候选**（最近一次留下结构化结论的
运行）。它同范围 → 它就是基线；它不同范围、或旧 payload 判不了范围 → **链在这里断了，
本轮不继承**，不再往回找。中间夹着失败 / 没有结构化结论的运行不影响这条链 —— 它们
本来就不在候选序列里（没有留下「上一轮的问题全集」）。

**旧 payload 一律当成链断**（2026-11 之前的运行没有 `analysis_profile` /
`repositories`）：宁可这一轮少继承，也不把另一个范围的结论当成本轮基线 —— 继承错的
方向是「把已经不在范围内的 critical 抬进报告」，比「少带一次历史」贵得多。代价是升级后
第一轮看不到历史清单，直到有一次新 payload 的运行落库。

## 人工忽略的第三种来源：分组级台账（P0，2026-11）

原先「人工已忽略」只从**上一轮那批行**的 `disposition` 读。而交付时被忽略的条目会被
`result_payload` 从 `anomalies` 里剔掉 —— 于是这一轮的行里没有它；只要再夹一次
「有结构化结论但 0 行」的运行，`baseline_findings` / `suppressed` 就同时变成空的，
那条误报下一轮会以 `pending` 身份重新报出来，人工的判断被静默作废。

所以读侧多了一份**分组级**的来源（`AiAnalysisIgnoredFingerprint`，写侧在
`services/ai/anomaly_disposition.set_disposition`）：忽略的集合 =
「上一轮 `disposition=ignored` 的指纹」∪「台账里这个 `target_key` 的指纹」。
前者是本轮看得见的处置，后者是**行消失之后仍然成立**的那份判断。

两条边界：

* `target_key` 为空（单提交分析）时**不查台账** —— 那种运行没有分组可隔离，
  跨组按指纹抑制会误伤别的版本的同名结论，宁可退回旧逻辑；
* 台账**总是**被并进**对账输入**（`previous_anomaly_rows`）—— 对账器拿到空表会直接
  早退，于是「曾被忽略的条目复活了」这个事实（`suppressed_previous`）在 P0 那个形状里
  根本不会被写出来；而且摘要（`baseline_findings`）与对账输入必须是**同一份候选集**，
  否则模型对一条台账条目的 `baseline_updates` 会被记成「指纹对不上本次清单」，那句
  错误提示本身是假的。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, or_

from models import db
from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisRun
from models.ai_analysis.anomaly import AiAnalysisIgnoredFingerprint
from services.ai.analysis_scope_filter import scope_signature
from services.ai.baseline import (
    DISPOSITION_IGNORED,
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


def payload_scope_signature(payload: Any) -> Optional[Tuple[Any, ...]]:
    """一份 `request_payload` 的范围签名；**判不了就回 `None`**。

    判不了有两种：payload 不是映射 / 坏 JSON，或者**缺 `analysis_profile` /
    `repositories` 任何一个键**。后者是 2026-11 之前的老 payload —— 那时范围还没落进
    账里。`scope_signature` 对缺键有默认值（`all / all / 空仓库`），拿默认值去比会把
    「一条老记录」误判成「就是 all 范围」；这里显式区分出「无法判」，调用方才能把它
    判成**链断**（这一轮不继承），而不是拿默认值当成同一范围。
    """
    data = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, Mapping):
        return None
    if "analysis_profile" not in data or "repositories" not in data:
        return None
    return scope_signature(data)


def run_scope_signature(run: Optional[AiAnalysisRun]) -> Optional[Tuple[Any, ...]]:
    """一条运行**自己账上**冻结的范围签名（`None` = 判不了 / 没有这行）。"""
    if run is None:
        return None
    return payload_scope_signature(getattr(run, "request_payload", None))


def run_matches_scope(run: Optional[AiAnalysisRun], signature: Optional[tuple]) -> bool:
    """这条运行能不能当**同一分析范围**的基线。

    `signature is None`（调用方没给范围）时恒真 —— 与这条边界出现之前逐字相同，老调用
    方与老测试的行为不变。给了范围时「解析不出签名」也算不兼容：那正是旧 payload 的
    情形，拿不准就不认它。
    """
    if signature is None:
        return True
    return run_scope_signature(run) == signature


def previous_run(
    target_type: str,
    target_key: Optional[str],
    *,
    scope_signature: Optional[tuple] = None,
) -> Optional[AiAnalysisRun]:
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

    ## `scope_signature`：同一分组 ≠ 同一范围，**链断了就不继承**（P1，2026-11-01）

    给出签名时只看**最近一条候选**（也就是下面这条 SQL 的第一行）：解析它自己的
    `request_payload`（`payload_scope_signature`）得到的签名与本次相同 → 返回它；
    签名不同、或旧 payload 根本没有 `analysis_profile` / `repositories`（判不了）
    → **返回 `None`**，**不往回找第二条**。

    ## 为什么不往回找（一次审查报告里的形状）

    A（tables）报了问题 X → B（all）改了那张表、把 X 修好 → C（tables）只改了另一张表。
    C 的候选序列是 B、A：跳过 B 取 A，A 的 X 就会原样回到 C 的报告里 —— **B 的修复被
    A 的旧结论覆盖**。范围链断在 B 这里，意味着「B 之后」没有一条可比的、完整的
    问题全集；拿更早的 A 当全集，报出来的就是已经不成立的旧账。

    越过失败 / 没有结构化结论的运行是另一回事：它们**不在候选序列里**（没有留下
    「上一轮的问题全集」），越过它们不构成换范围，所以上面这条判据只对候选判。
    **旧 payload 一律视为链断**是刻意的：宁可少继承一次，也不把另一范围的结论当本轮
    基线（理由见模块抬头）。不给签名时行为与这条边界出现之前逐字相同。
    """
    if not target_key:
        return None
    query = (
        AiAnalysisRun.query.filter_by(target_type=target_type, target_key=target_key)
        .filter(AiAnalysisRun.status.in_(CONCLUDED_STATUSES))
        # NULL（失败/未完成，以及加列之前的历史行）一律不算：拿不准就不当基线。
        .filter(AiAnalysisRun.conclusion_structured.is_(True))
        .order_by(AiAnalysisRun.created_at.desc())
    )
    nearest = query.first()
    if nearest is None or scope_signature is None:
        return nearest
    if run_matches_scope(nearest, scope_signature):
        return nearest
    # 链断：最近一条候选的范围与本次不同（或旧 payload 判不了范围）。这里**不许**再往
    # 回找 —— 跳过它取更早那条，会让中间那次的修复被旧结论覆盖（见 docstring 的 A/B/C）。
    return None


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


# 回退链最多往回看几条运行。
#
# 为什么必须封顶：这是**读库**路径，而「一直往回找到有结论的那条」在一段长历史里
# 既可能变成整段扫描，也会把一条几周前的结论当成「上一轮的结论全集」—— 那已经不是
# 同一个版本周期里的账了。三次足够越过「连续几次降级/失败」这种真实形状。
BASELINE_FALLBACK_LIMIT = 3


def _anomaly_rows(run_id: int) -> List[dict]:
    """恢复上一轮结论行，以及仅保存在运行载荷中的范围标记。"""
    rows = [row.to_dict() for row in AiAnalysisAnomaly.query.filter_by(run_id=run_id).all()]
    run = db.session.get(AiAnalysisRun, run_id)
    try:
        conclusion = json.loads(run.response_payload) if run and run.response_payload else {}
    except (TypeError, ValueError):
        conclusion = {}
    if not isinstance(conclusion, Mapping):
        return rows
    # 数据表没有 out_of_scope 列；按指纹从同一轮冻结的结论载荷恢复，
    # 避免下一轮仅改了别的文件时把已有风险误判成背景发现。
    scope_marks = {
        str(item.get("fingerprint")): item["out_of_scope"]
        for item in conclusion.get("anomalies") or ()
        if isinstance(item, Mapping)
        and item.get("fingerprint")
        and isinstance(item.get("out_of_scope"), bool)
    }
    for row in rows:
        fingerprint = str(row.get("fingerprint") or "")
        if fingerprint in scope_marks:
            row["out_of_scope"] = scope_marks[fingerprint]
    return rows


def _run_is_baseline(
    run: Optional[AiAnalysisRun], scope_signature: Optional[tuple] = None
) -> bool:
    """这条运行能不能当「上一批结论」的来源。

    判据与 `previous_run` 的 SQL 是**同一件事**（`CONCLUDED_STATUSES` +
    `conclusion_structured is True` + 范围签名）。这里用 Python 判，只因为回退链手里
    已经有行对象；改一处必须改另一处 —— 否则「挑基线」与「回退找基线」会指向两条不同
    的运行，而两边都不报错（`tests/test_ai_baseline_needs_structured_conclusion.py`
    有一条用例同时断言 `previous_run` 挑中的就是回退挑中的那一条）。

    签名那一半用 `run_matches_scope`（同一个判据）。**这个函数只回答「这一条能不能
    用」**；「回退链要不要在一条不兼容的运行上停住」由调用方判（`_rows_of_baseline_run`
    里的 `_scope_breaks_the_chain`）—— 写成「这里判不兼容、调用方跳过它继续往前」就是
    旧的逐条跳过，会让链断之后仍然继承更早那份结论。
    """
    return (
        run is not None
        and str(run.status or "") in CONCLUDED_STATUSES
        and run.conclusion_structured is True
        and run_matches_scope(run, scope_signature)
    )


def _scope_breaks_the_chain(
    run: Optional[AiAnalysisRun], scope_signature: Optional[tuple]
) -> bool:
    """这条运行是不是回退链上那道**必须停下**的坎（范围不同 / 旧 payload 判不了）。

    判据刻意只认**候选**（跑完了 + 有结构化结论）：

    * 失败 / 未完成：`_concluded_run_before` 的序列里根本没有它，谈不上跨范围；
    * 只有 markdown 的降级：没有留下「上一轮的问题全集」，越过它去读更早那份**正是**
      回退链存在的理由（`test_previous_anomaly_rows_falls_back_past_a_run_without_a_conclusion`）。

    范围不兼容是另一种东西：它说明这中间**换过分析范围**，更早那条同范围的结论与现在
    的输入之间隔着一整轮看不见的账（那一轮可能已经把旧问题修好），不能再当本轮基线。
    判据与 `previous_run` 的链式判定同源，两处必须一起改。
    """
    if scope_signature is None or run is None:
        return False
    if str(run.status or "") not in CONCLUDED_STATUSES:
        return False
    if run.conclusion_structured is not True:
        return False
    return not run_matches_scope(run, scope_signature)


def _concluded_run_before(run: AiAnalysisRun) -> Optional[AiAnalysisRun]:
    """紧挨着 `run` 之前的那一条**跑完了**的运行（不管它有没有结构化结论）。

    **一步只退一条运行**，不直接跳到「上一条有结论的运行」：后者在连续降级时一步就跨
    过去了，`BASELINE_FALLBACK_LIMIT` 等于不存在 —— 而「连续几次没留下结论」正是这条
    回退链唯一要越过的形状。

    `created_at` 相同时再按 id 排：同一秒内跑完两次（真机上少见，批量补跑与测试里常见）
    时，「更早」只能由 id 决定，否则回退要么原地打转、要么整条跳过去。
    """
    created = getattr(run, "created_at", None)
    if created is None:
        # 没有时间就没有「更早」可言（未 flush 的行 / 极老的行）：保守地不再往回退。
        return None
    return (
        AiAnalysisRun.query.filter_by(
            target_type=run.target_type, target_key=run.target_key
        )
        .filter(AiAnalysisRun.status.in_(CONCLUDED_STATUSES))
        .filter(
            or_(
                AiAnalysisRun.created_at < created,
                and_(
                    AiAnalysisRun.created_at == created,
                    AiAnalysisRun.id < run.id,
                ),
            )
        )
        .order_by(AiAnalysisRun.created_at.desc(), AiAnalysisRun.id.desc())
        .first()
    )


def previous_anomaly_rows(
    run_id: int, *, scope_signature: Optional[tuple] = None
) -> List[dict]:
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

    ## 传入的运行**不是**可用基线时，往回退（2026-11 产品审查第 5 条）

    `reconcile_result` 拿到空列表会**直接早退**（原样返回结果），等于告诉它「本轮没有
    上一轮结论」。而「人工已忽略」这件事只记在这批行上（`disposition == ignored`）：
    一旦读成空表，被忽略的条目下一轮就没有任何东西挡着它原样重报。

    所以：传入的运行若是**没有结构化结论**的（只有 markdown 的降级、失败、加列之前的
    NULL 老行），就沿与 `previous_run` 同一套判据往回退，最多
    `BASELINE_FALLBACK_LIMIT` 条运行，返回**最近一条可用基线**的行。形状不变（仍是
    `to_dict()` 那一份），调用方一行都不用改。

    今天真正的调用方（`ai_analysis_service._run_engine_and_persist`）拿的是
    `previous_run` 挑出来的运行，它本来就卡了「有结构化结论」—— 所以这条回退是
    **兜底**：函数自己被直接喂一条降级行时，也不该变成瞎的。

    **`scope_signature` 是可选参数**：给了就与 `previous_run` 同一条链 —— 传入的运行
    本身是范围不兼容的候选时**直接回空表**；回退途中遇到第一条范围不兼容的候选也停住
    （`_scope_breaks_the_chain`），**不越过它**去取更早那条同范围的结论。理由与
    `previous_run` 的 A/B/C 相同：中间那一轮可能已经把旧问题修好，更早那份「问题全集」
    已经过期，跳过它继承等于让修复被旧结论覆盖。失败 / 只有 markdown 的运行不在这条
    判据里（它们不是候选，越过它们正是回退链的用途）。不给签名时行为与这条边界出现
    之前逐字相同。

    ## 为什么「有结论但 0 行」**不**往回退，但**要**补台账

    那不是「没读进来」，是**这一轮真的没有在挂结论**（本轮没报新问题，或者旧的都被
    逐条声明修复了 —— 声明收口只留在 `retracted_findings`，不进这张表）。往回退一层
    会把已经关掉的条目重新翻出来当「仍成立」，与 `baseline.py` 那条「上一次那批就是
    当前问题全集」正好相反。空表在这里是一个**结论**，不是缺失。

    但「0 行」不等于「这个分组没有人工忽略」：被忽略的条目**本来就不在行里**（交付时
    被 `result_payload` 剔掉了）。所以台账里的那一批**总是**并进来（见
    `_with_ignored_ledger`）—— 少了它对账器会直接早退（它的第一句判据就是
    `if not old_rows`），P0 那个形状里「曾被忽略的条目又回来了」这件事
    （`suppressed_previous`）根本不会被写出来。

    **为什么「总是」而不是只在那批为空时**：`baseline_findings` 那一侧（喂给模型的
    摘要）也把台账并了进来，于是摘要里会出现只存在于台账的指纹；模型按协议必须对
    「清单上的每一条」给出 `baseline_updates`，而合并器的「哪些指纹算认识」= 这里返回
    的行。两边不同源的话，模型对一条台账条目的交代会被记成「指纹对不上本次清单」——
    那句错误提示本身是假的，读者会去找一个不存在的抄写错误。
    """
    run = AiAnalysisRun.query.filter_by(id=run_id).first()
    if run is None:
        # 运行被删了（保留期清理）：没有行可读，与「读到空表」走同一条路。
        return []
    return _with_ignored_ledger(
        _rows_of_baseline_run(run, scope_signature), run.target_key
    )


def _with_ignored_ledger(rows: List[dict], target_key: Optional[str]) -> List[dict]:
    """把分组台账并进对账输入（按指纹；台账里已有的指纹以台账的处置为准）。

    两件事，缺一不可：

    * **行上写着 ignored、而台账里有它** → 以台账为准（与 `baseline_findings` 同一条
      口径）。否则这一行会按「仍成立」被带回清单，而摘要那边已经把它列在「已忽略」组里。
    * **台账里有、行里没有** → 补一行（优先取台账指的那条原始结论行，见
      `_ignored_row_dict`）。被忽略的条目交付时不在行里，这一档才是常态。
    """
    ledger = ignored_fingerprints(target_key)
    if not ledger:
        return rows
    merged: List[dict] = []
    seen = set()
    for row in rows:
        fingerprint = str(row.get("fingerprint") or "")
        if fingerprint and fingerprint in ledger:
            row = dict(row, disposition=DISPOSITION_IGNORED)
        merged.append(row)
        if fingerprint:
            seen.add(fingerprint)
    for fingerprint, entry in ledger.items():
        if fingerprint in seen:
            continue
        merged.append(_ignored_row_dict(fingerprint, entry))
    return merged


def _rows_of_baseline_run(
    run: AiAnalysisRun, scope_signature: Optional[tuple] = None
) -> List[dict]:
    """`run` 自己（或往回退到的那条可用基线）的那批行。台账**不**在这里补。

    ## 回退链在**第一条范围不兼容**的候选上停住

    原先每一步都「范围不同就跳过」，于是从一条不同范围的运行喂进来时，会退到更早那条
    同范围的结论上 —— 正是 `previous_run` 那条链的另一半（同一个 A/B/C 形状，只是入口
    换成了「直接把 B 的行读出来」）。现在这里与 `previous_run` 同一条链：遇到范围不兼容
    的**候选**就回空表，不越过它；只有「跑完了但没有结构化结论」的运行才允许越过
    （判据与理由见 `_scope_breaks_the_chain`）。

    `scope_signature=None` 时行为与这条边界之前逐字相同。
    """
    if _run_is_baseline(run, scope_signature):
        return _anomaly_rows(run.id)
    if not str(run.target_key or ""):
        # 分组键为空时 `previous_run` 也直接回 None：照直回退会扫到**别组**的运行。
        return []
    if _scope_breaks_the_chain(run, scope_signature):
        return []
    cursor = run
    for _ in range(BASELINE_FALLBACK_LIMIT):
        cursor = _concluded_run_before(cursor)
        if cursor is None:
            return []
        if _scope_breaks_the_chain(cursor, scope_signature):
            return []
        if _run_is_baseline(cursor, scope_signature):
            return _anomaly_rows(cursor.id)
    return []


def ignored_fingerprints(
    target_key: Optional[str],
) -> Dict[str, AiAnalysisIgnoredFingerprint]:
    """某个周版本分组的人工忽略台账：`fingerprint → 行`。

    **分组键为空就回空**（与 `previous_run` 对空 key 回 `None` 是同一条边界）：
    单提交分析没有分组可隔离，按指纹跨组抑制会误伤别的版本的同名结论。
    """
    key = str(target_key or "").strip()
    if not key:
        return {}
    rows = AiAnalysisIgnoredFingerprint.query.filter_by(target_key=key).all()
    return {str(row.fingerprint or ""): row for row in rows if row.fingerprint}


def _ignored_row_dict(fingerprint: str, ledger: AiAnalysisIgnoredFingerprint) -> dict:
    """台账行 → 对账器认的那种行形状。

    **优先取台账指的那一条原始结论行**（`last_run_id` + 指纹）：台账只存了标题与文件，
    而对账器把历史的等级、证据、断言一路带进本轮结果 —— 只从台账拼一份「没有证据、
    等级默认 high」的行，复活条目在面板上会看起来像一条残缺的新发现。
    那条原始行按 `last_run_id` 级联存在于库里（删运行会连台账一起删），所以通常取得到。

    取不到时（极端：原始行被单独清掉、或老库的 `last_run_id` 为空）退回台账里那几列：
    标题 + 文件路径 + `disposition=ignored` 足以让对账器判「这一条仍被人工忽略」。
    """
    if ledger.last_run_id:
        original = AiAnalysisAnomaly.query.filter_by(
            run_id=ledger.last_run_id, fingerprint=fingerprint
        ).first()
        if original is not None:
            # 原始行自己的处置也按台账覆盖：台账才是这条「忽略」的持久事实。
            return dict(original.to_dict(), disposition=DISPOSITION_IGNORED)
    return {
        "run_id": ledger.last_run_id,
        "fingerprint": fingerprint,
        "title": str(ledger.title or "（此前被人工忽略的条目）"),
        "category": "",
        "severity": "",
        "confidence": "",
        "evidence": [],
        "commit_ref": "",
        "file_path": str(ledger.file_path or ""),
        "impact": "",
        "suggestion": "",
        "disposition": DISPOSITION_IGNORED,
        "claims": "[]",
    }


def baseline_findings(
    target_type: str, target_key: Optional[str], *, scope_signature: Optional[tuple] = None
) -> List[BaselineFinding]:
    """上一次**可以当基线**的那次运行报出的那批结论 **∪ 分组台账里的忽略项**。

    **取「上一次运行的那批」而不是把历次运行并起来**：每次运行产出的本来就是「这个版本
    当前仍成立的问题全集」（skill 里定死了这个语义），所以上一次那批就是当前基线。
    并起来反而会把已经修好的旧条目重新翻出来。

    **台账那一半不是「历次运行」**：它只回答一个问题 ——「这个分组里，用户判定过哪些
    指纹不用再报」。被忽略的条目交付时不再进行表，于是它只能记在这里；少了它，中间夹
    一次「有结论但 0 行」的运行，这份判断就从读侧消失（P0 真漏口，见模块抬头）。
    两半按指纹取**并集**：上一轮行里那一条若还在，用行上的完整字段（标题/等级/文件），
    处置一律按台账判成 `ignored`。

    `scope_signature` 传给 `previous_run`：**范围链断在哪一条，就一条运行结论都不继承**
    （`previous_run` 回 `None`），见模块抬头。**忽略台账不受范围影响** —— 那是用户对
    指纹的判定，不是上一轮结论的一部分（两半的来源本来就不同）。
    """
    ignored = ignored_fingerprints(target_key)
    findings: List[BaselineFinding] = []
    seen = set()
    previous = previous_run(target_type, target_key, scope_signature=scope_signature)
    if previous is not None:
        for row in _anomaly_rows(previous.id):
            fingerprint = str(row.get("fingerprint") or "")
            if not fingerprint:
                continue
            finding = BaselineFinding(
                fingerprint=fingerprint,
                title=row.get("title") or "",
                severity=row.get("severity") or "high",
                category=row.get("category") or "",
                file_path=row.get("file_path") or "",
                commit_ref=row.get("commit_ref") or "",
                disposition=row.get("disposition") or DISPOSITION_PENDING,
                background=row.get("out_of_scope") is True,
            )
            # 台账是「忽略」这条判断的持久来源：行上还写着 pending（例如原始处置行被
            # 重写过）也以台账为准 —— 两者的并集才是「哪些不再报」。
            if fingerprint in ignored:
                finding = replace(finding, disposition=DISPOSITION_IGNORED)
            findings.append(finding)
            seen.add(fingerprint)
    for fingerprint, ledger in ignored.items():
        if fingerprint in seen:
            continue
        # 上一轮的行里已经没有它了：标题与文件只能从台账来。等级不落台账（它不是
        # 抑制判据），用 `BaselineFinding` 的默认值 —— 它只出现在基线摘要的「已忽略」
        # 组里，那一组默认不再进报告，等级不影响任何决策。
        findings.append(
            BaselineFinding(
                fingerprint=fingerprint,
                title=str(ledger.title or ""),
                file_path=str(ledger.file_path or ""),
                disposition=DISPOSITION_IGNORED,
            )
        )
    return findings


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


def has_scope_mismatch(baseline_account: Optional[Mapping[str, Any]]) -> bool:
    """这次运行的 state 指针那一轮，与本次范围**是否不兼容**（写侧落的 `scope_mismatch`）。

    写侧（`build_weekly_payload`）在「指针指着一条运行、而它的 payload 范围与本次不同」
    时置真。读侧用它做两件事：旧结论**一个字都不进模型输入**（与用户点全量同一档），
    以及报告里说明「历史结论未继承」。

    **它只表达「指针那一轮」这一格**：范围链也可能断在更近的一条候选上、而指针那一轮
    恰好同范围（罕见：指针过期，或加列之前的老 payload）。那种形状下账上读不出这件事
    —— `previous_run` 已经回 `None`，但 `baseline` 上没有留痕，报告也就不会说「未继承」。
    补齐它要改写侧（`build_weekly_payload` / `_run_engine_and_persist`），不在本模块；
    本模块能保证的只有「链断 → 不把旧运行的行交出去」。

    `None` / 缺键 / 脏值一律**不算**（保守：宁可多带一次旧结论，也不要因为账上少一格
    就静默关掉历史）。
    """
    if not isinstance(baseline_account, Mapping):
        return False
    return baseline_account.get("scope_mismatch") is True


def baseline_digest(
    target_type: str,
    target_key: Optional[str],
    change: ChangeSet,
    *,
    baseline_account: Optional[Mapping[str, Any]] = None,
    scope_signature: Optional[tuple] = None,
) -> str:
    """给模型看的「已经报过的问题」。取不到就是空串（提示词里那一段整个不出现）。

    `changed_paths` 传「上次报过、这次又变了」的文件：那类结论的证据已经过期，要重新
    确认 —— 包括人工标过「已忽略」的。这是「忽略」不会变成「永远看不见」的保证。

    **先 `classify` 再渲染，两件事必须分开做**：`build_baseline_digest` 刻意不收
    `changed_paths`，因为它再判一遍状态会把刚判成「需要重新确认」的结论判回「已忽略」
    并从摘要里抹掉 —— 而且是静默的（报告里只是少一条）。

    `baseline_account` 传本次运行的基线账（`payload["baseline"]`）。用户**显式**点全量时
    直接返回空串（理由与产品决定见模块 docstring）。**范围不一致时同样返回空串**：那一轮
    的结论回答的是另一批文件的问题，喂给模型只会让它当既成事实复述（P1 的同一类失效）。
    `scope_signature` 与 `previous_run` 同一份判据：给了时**链断也不注入**上一轮的行
    （`previous_run` 回 `None`，`baseline_findings` 只剩台账那一半 —— 那不是从某条运行
    继承来的结论）。不给时行为与这条边界之前逐字相同。
    """
    if run_ignores_history(baseline_account) or has_scope_mismatch(baseline_account):
        return ""
    from services.ai.business_baseline import business_digest
    business = business_digest(previous_run(target_type, target_key, scope_signature=scope_signature))
    findings = baseline_findings(target_type, target_key, scope_signature=scope_signature)
    if not findings:
        return business
    skipped = skipped_unstructured_runs(
        target_type,
        target_key,
        previous_run(target_type, target_key, scope_signature=scope_signature),
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
    return "\n\n".join(filter(None, (
        build_baseline_digest(classify(findings, changed_paths=change.paths), note=note), business)))


def suppressed(
    target_type: str,
    target_key: Optional[str],
    change: ChangeSet,
    *,
    scope_signature: Optional[tuple] = None,
) -> frozenset:
    """人工已忽略、且相关文件没有再变的指纹。这些不再进清单。

    忽略的两个来源都算进来（上一轮行上的 `disposition=ignored` + 分组台账），
    判据仍是 `baseline.classify` 那一份：相关文件又变了的（`needs_recheck`）
    **不在**结果里 —— 「忽略」不等于永远看不见。

    **范围链断不关闭抑制**（与用户点全量同一档）：抑制里有一半是**分组台账** ——
    用户自己的分类账，不是模型的输入、也不是从某条运行继承来的结论；范围换了就把它
    作废，等于让用户已经判定「不用再报」的条目重新冒出来。上一轮**行上**的那一半照旧
    随基线一起消失（链断 → 没有上一轮那批行），两半的来源本来就不同（见
    `baseline_findings`）。
    """
    findings = baseline_findings(target_type, target_key, scope_signature=scope_signature)
    if not findings:
        return frozenset()
    return suppressed_fingerprints(classify(findings, changed_paths=change.paths))
