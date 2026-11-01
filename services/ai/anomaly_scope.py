# -*- coding: utf-8 -*-
"""一条结论**在本轮要不要参与定级**的纯判据（WP27 的判据，WP28 搬出来）。

## 为什么单独一个模块

这套判据原先住在 `result_payload._out_of_scope` 里，只有「生成结论载荷」那个调用方。
WP28 起**增量基线**也要用它：继承来的条目必须按本轮的范围重算一次 `out_of_scope`，
否则上一轮标成背景发现的那条在下一轮就变回正式结论（审查已复现：范围外 critical
经增量继承后标记消失，风险 low→high）。

两个调用方（`result_payload` 的定级、`incremental_baseline` 的继承重算）**必须用同一份
判据**：各写一份的话，「本轮参与定级」与「继承后参与定级」会在边界上分叉，而分叉的
表现是同一类结论在两轮报告里一高一低 —— 没有任何异常，只是等级对不上。

这里的四个函数与搬家前**逐字一致**（语义不许改）：`result_payload` 仍以
`_change_paths_of` / `_ref_path` / `_claims_touch` / `_out_of_scope` 四个旧名字导入它们，
老调用方与老测试不受影响。
"""

from __future__ import annotations

from typing import Iterable, Mapping

from services.ai import ref_shapes
from services.ai.scope import normalize_path


def change_paths_of(payload: Mapping) -> frozenset[str]:
    """**本次配表差异**的路径集合（`payload["delta_files"]` 的 `file_path`，已归一化）。

    这是「可核实关联」的锚点（WP27）：`delta_files` 是写侧按 profile 收窄过的**本次要
    评估的那批改动**，结论引用了这里的文件，才算参与了这次评估。读取放开（`tables` 的
    `allowed_paths=None`）之后模型能读到整个仓库，与这次配表无关的背景发现会明显变多，
    它们不该参与定级。

    缺键 / 形状不对 / 空路径一律跳过：**拿不准就是空集**，结论于是落到「关联不足」那一侧
    （保守方向：宁可不参与定级，也不许背景发现把等级抬上去）。空集**不是**「全放行」，
    见 `out_of_scope` 的 `allowed is None` 分支。

    **单提交模式（`mode=commit`）的例外**：`build_commit_payload` 没有 `delta_files`
    （单提交的输入就是那一条提交），它的「本次改动」锚点是 `payload["commit"]["path"]`。
    不补这一处，单提交运行里所有有路径的结论都会落到背景发现、等级退化成规模兜底 ——
    那是 WP27 不该波及的另一条输入形态。只在这个键**存在**时读它：老周版本 payload
    （无 `delta_files`、无 `commit`）仍是空集，保守口径不变。
    """
    paths: set[str] = set()
    for item in payload.get("delta_files") or ():
        if not isinstance(item, Mapping):
            continue
        path = normalize_path(str(item.get("file_path") or ""))
        if path:
            paths.add(path)
    if not paths:
        # `delta_files` 缺键 / 空列表时才看单提交锚点（两者不会同时出现在生产 payload 里）。
        commit = payload.get("commit")
        if isinstance(commit, Mapping):
            path = normalize_path(str(commit.get("path") or ""))
            if path:
                paths.add(path)
    return frozenset(paths)


def ref_path(ref) -> str:
    """一条依据（`path:line` / `path:sheet!A1` / 裸路径）里的文件路径。

    切分复用 `ref_shapes._split_ref`：它同时是「这条依据能不能照着定位」的判据
    （`is_locatable_ref` 走的就是它）。自己再写一套冒号切分，迟早会在
    `config/x.xlsx:Sheet1!A1:C5` 这种**自带冒号**的单元格区间上与它说不到一起。
    """
    text = str(ref or "").strip()
    if not text:
        return ""
    path, _locator = ref_shapes._split_ref(text)
    if path:
        return normalize_path(path)
    # 裸路径（没有定位符）：`_split_ref` 只认「路径:定位符」那一形态，整串就是路径。
    # `_ref_body` 是它那边同一份清洗（剥掉模型爱加的引号 / 反引号 / 括号）。
    return normalize_path(ref_shapes._ref_body(text))


def claims_touch(claims: Iterable[object], change_paths: frozenset[str]) -> bool:
    """这几条原子断言（`Anomaly.claims`）里，有没有一条的依据**指向本次配表差异**。

    命中即「有可核实关联」：一条代码消费方 / 未改动引用方表的结论，本身指认的文件不在
    本次改动里，但它引用了这次改过的表 —— 读的人能照着那条依据去核，它就该参与定级
    （否则真问题会跟着背景发现一起被单列，等级反而下降）。

    形状读法与 `Anomaly.claims` 一致（每项有 `evidence_refs`），同时容忍字典形态：
    这个函数也会被 `_anomaly_entry` 那一侧调用，而那里拿到的不一定还是 dataclass。
    **读不出证据 = 没有关联**（保守方向同上）。
    """
    if not change_paths:
        return False
    for claim in claims or ():
        if isinstance(claim, Mapping):
            refs = claim.get("evidence_refs") or ()
        else:
            refs = getattr(claim, "evidence_refs", ()) or ()
        if isinstance(refs, str):
            refs = (refs,)
        for ref in refs:
            if ref_path(ref) in change_paths:
                return True
    return False


def out_of_scope(
    file_path: str,
    allowed: frozenset[str] | None,
    *,
    change_paths: frozenset[str] = frozenset(),
    claims: Iterable[object] = (),
) -> bool:
    """这条结论是不是**不该参与本次定级**的结论（范围外 / 关联不足）。

    ## 两种判据（`allowed` 非 None 时逐字保持旧口径）

    * `allowed is not None`（老 payload / 人工收窄）：引用的文件不在白名单里就是越界 ——
      那是「模型读了一个不允许它读的文件」。空集合时任何有路径的结论都算越界（一条都不
      许读）。
    * `allowed is None`（WP20 起 `tables` 的默认）：读取放开之后「不在白名单」不再等于
      越界，判据换成**与本次配表差异有没有可核实关联**（WP27）：

      1. 没有 `file_path` 的跨文件 / 流程结论放行（旧口径：按越界吞掉会把它静默降级，
         用户看到等级变低却不知道有一条关键结论被丢掉）；
      2. 引用的文件本身就在 `change_paths` 里 → 放行（配表差异本身）；
      3. 某条断言的依据指向 `change_paths` 里的文件 → 放行（代码消费方 / 未改动表对
         本次配表差异的结论）；
      4. 其余 → True：**背景发现**，照常进 `anomalies`（带 `out_of_scope=True`，报告里
         仍看得见），但不参与风险等级与「模型报出 N 条」的计数。

    「什么都不许读」不等于「什么都不许说」：无路径的那一类在两种判据下都放行。
    """
    text = normalize_path(file_path)
    if allowed is not None:
        # 旧口径逐字不动：有路径且不在白名单里才算越界。
        if not text:
            return False
        return text not in allowed
    if not text:
        return False
    if text in change_paths:
        return False
    if claims_touch(claims, change_paths):
        return False
    return True
