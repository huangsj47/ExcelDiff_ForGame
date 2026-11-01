# -*- coding: utf-8 -*-
"""`analysis_profile` 在**输入侧**的落法：`tables` = 只把配置表仓库的改动装进输入。

## 口径变过一次，这个模块把新口径收在一处

2026-11-01 之前 `tables` 被实现成「只保留能被 `is_excel_file` 认出的后缀」（见
`services/ai_analysis_service.build_weekly_payload` 里的旧 `allowed_paths`）。那等于说
**配置表仓库里除 xlsx 以外的东西都不是配表风险** —— 而真实项目里配表的加载器、
导表产物、协议文件往往就躺在同一个仓库里（`CfgItem.lua`、`item.py`、`enum.json`、
`item.proto`）。它们变了的风险恰恰要通过配表维度回答，却被后缀挡在输入之外：模型连
「这张表的 ID 在代码里怎么被引用」都看不到，报告却写着「仅配表，覆盖完整」。

现在的口径是**按仓库**：

* `tables`（默认）：只留 `Repository.resource_type == "table"` 的配置，**这些配置下的
  全部改动文件**（不按后缀）进 `delta_files`；代码/资源仓库（`code` / `res` / NULL）
  的配置整体不进输入；
* `all`：与「没有 profile 这个概念」时逐字相同；
* 项目**没有**配表仓库时退回 `all`：否则一个文件都进不了输入，用户点了分析却只得到
  「没有变化」—— 那是把配置错误伪装成业务结论。回退要留痕（`profile_fallback`）。

## 2026-11-01 WP20：**输入收窄 ≠ 读取收窄**

用户指出配置表不是孤立的：表与表之间有关联，配表与代码仓库的代码也有关联 —— 改了
`item.xlsx` 里的 ID，消费方在 `ItemManager.lua`、枚举在 `ItemEnum.cs`、协议在
`item.proto`，而它们**往往不在本批次改动里、甚至在另一个仓库里**。如果连 `allowed_paths`
也收成 delta 路径，模型按需取数时就读不到这些文件：「这张表的 ID 在代码里怎么被引用」
这类跨表/跨仓库取证直接退化，报告却写着「仅配表，覆盖完整」。

所以两个概念必须拆开（本模块只负责第一件，第二件由 `allowed_paths_for_profile` 的
返回值表达）：

* **分析输入**（`configs` / `delta_files` / 清单 / 快照 / 增量分母）：仍然只含配置表
  仓库的**改动文件** —— 这是 WP14 的收敛，不回退；
* **读取范围**（`allowed_paths`；模型按需 `file_content` / `find_references` / 冻结仓库）：
  `tables` 下**不限制**（`None`）—— 允许读全部**跟踪文件**：配表仓库里未改动的引用方
  表 / 生成物 / 导表工具，以及代码仓库里谁消费这张表、枚举定义、协议与常量。

`None` 与空列表的语义差保持不变：**`None` = 允许读全部跟踪文件，`[]` = 一条都不许**。

## 为什么单独一个模块

`services/ai_analysis_service.py` 贴着 2000 行硬上限（`scripts/check_file_length.py`）。
这两个函数是**纯判据**：只读配置对象上的 `repository` 与 delta 字典，不碰库、不发请求，
搬出来之后服务层只留接线。判「哪个仓库算配表」的**唯一事实源**是
`services/ai/focus_scope.resource_type_of`（空值按 code）—— 不在这里另写一份，否则
`focus=table` 与 `profile=tables` 会在 NULL 仓库上给出不同答案。
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence, Tuple

from services.ai.focus_scope import FOCUS_ALL, resource_type_of

__all__ = [
    "PROFILE_ALL",
    "PROFILE_FALLBACK_NO_TABLE_REPOSITORY",
    "PROFILE_TABLES",
    "allowed_paths_by_repository",
    "allowed_paths_for_profile",
    "effective_config_ids",
    "filter_configs_for_profile",
    "scope_signature",
    "scope_signature_for_facts",
]

PROFILE_TABLES = "tables"
PROFILE_ALL = "all"
# 项目一张配表都没有时的回退原因码（落 `policy.decision.profile_fallback`）。
PROFILE_FALLBACK_NO_TABLE_REPOSITORY = "no_table_repository"


def _normalize(profile: object) -> str:
    """归一成值域内的一档；认不出来按 `tables`（保守那一侧，与 `skill_contract` 同口径）。"""
    text = str(profile or "").strip().lower()
    return PROFILE_ALL if text == PROFILE_ALL else PROFILE_TABLES


def filter_configs_for_profile(
    configs: Sequence[Any], profile: object
) -> Tuple[List[Any], str]:
    """按 profile 收窄本批次的配置集，返回 `(配置清单, 回退原因)`。

    回退原因只有一种：`tables` 但整个批次没有配表仓库。那时**原样返回**全部配置，
    并把 `no_table_repository` 交出去（调用方据此把生效 profile 改成 `all` 并落账）。

    `all`（或认不出来的脏值按 `tables` 处理）之外的任何取值都不会被静默当成 `all`：
    把未知当 `all` 会让提示词里那句「仅配表」与实际输入相反，而报告看不出来。
    """
    items = list(configs)
    if _normalize(profile) == PROFILE_ALL:
        return items, ""
    kept = [
        cfg
        for cfg in items
        if resource_type_of(getattr(cfg, "repository", None)) == "table"
    ]
    if not kept:
        return items, PROFILE_FALLBACK_NO_TABLE_REPOSITORY
    return kept, ""


def allowed_paths_for_profile(
    profile: object, delta_files: Sequence[dict]
) -> Optional[List[str]]:
    """本批次**允许模型读的路径**：`tables` 与 `all` 都返回 `None`（= 不限制）。

    为什么全放开（WP20）：`profile=tables` 收窄的是**输入**（`filter_configs_for_profile`
    已经把代码/资源仓库的配置与快照剔掉），不是**读取**。配置表之间的引用、配表与代码
    仓库之间的消费关系，证据多半躺在**未改动**的文件里（引用方表、导表产物、导表工具、
    代码仓库的消费方/枚举/协议）。把这里收成 delta 路径，等于让模型只能看改动本身，
    却指望它回答「这个改动会影响谁」—— 那一跳的证据恰好被平台自己挡住了。

    语义边界（与 `change_set._allowed_path_set` / `frozen_repo` 同一口径）：

    * `None`：允许读全部**跟踪文件**（读侧按冻结 tip 的 Git 跟踪树兜底）；
    * `[]`：一条都不许 —— 写侧若真要知道「什么都不许读」，必须显式给空列表，
      不能靠「这张表没有 delta」推出空列表（那会把「没改动」误当成「不许读」）。

    两个参数保留是为了不改调用点（`build_weekly_payload` 贴着行数上限），但**都不再
    影响返回值**：读取范围与输入清单是两件事，混在同一个返回值里正是上一轮把跨表取证
    挡掉的错误。

    关联定级（WP27）：`None` 时 `result_payload._out_of_scope` 不再恒为 False，而是按
    「文件在本次 `delta_files` 里，或 `claims.evidence_refs` 命中某条 delta」判定 ——
    有可核实关联的代码结论参与定级，无关联的背景发现标 `out_of_scope` 单列（仍可见、
    不计等级）。约束一方面在提示词（`change_set._profile_note` 说明「结论优先落在配表
    改动上」），另一方面由这条判据承担。
    """
    return None


def allowed_paths_by_repository(delta_files: Sequence[dict]) -> dict:
    """把扁平白名单按**仓库**拆开：`{repository_id: [路径, ...]}`（保序去重）。

    ## 为什么必须按仓库拆（P1-3）

    `allowed_paths` 是**相对路径**集合，本身不含仓库身份。两个仓库都有
    `config/items.json` 时，把同一份集合交给每一个仓库的 `FrozenTreeReader`，代码仓库
    那条同名路径就会因为「白名单里有这个字符串」被放行 —— 而它属于另一个仓库的改动，
    不该在本次读取范围里。拆开之后，每个仓库只认**自己那批改动**产生的路径。

    缺 `repository_id` 的条目无法归属，直接跳过（不猜它属于哪个仓库）；调用方据此
    决定是否退回扁平集合。
    """
    grouped: dict = {}
    for item in delta_files or ():
        repository_id = item.get("repository_id")
        path = str(item.get("file_path") or "").strip()
        if repository_id is None or not path:
            continue
        paths = grouped.setdefault(repository_id, [])
        if path not in paths:
            paths.append(path)
    return grouped


def effective_config_ids(configs: Sequence[Any], profile: object) -> List[int]:
    """本批次**实际装进输入**的配置 id（按 profile 收窄之后）。

    ## 为什么指纹必须与 `build_weekly_payload` 用同一份 id（P1-4）

    `seal_snapshot` 冻的是 `payload["group"]["config_ids"]`，而那一份来自
    `build_weekly_payload` 里**收窄后**的 `configs`（`tables` 只剩配表仓库的配置）。
    而调度去重（`snapshot_already_analyzed`）与「当前缓存是否已有快照」
    （`job_service._current_target_snapshot`）原先读的是**整组** config id —— 混合仓库
    项目两边的指纹永远不等：同快照复用与自动跳过同时失效，每个周期白跑一次分析。

    回退档（没有配表仓库）由 `filter_configs_for_profile` 原样返回全部配置，于是这里
    得到的仍是整组 id —— 与 `build_weekly_payload` 把生效 profile 改成 `all` 的口径一致。
    """
    kept, _fallback = filter_configs_for_profile(configs, profile)
    return [cfg.id for cfg in kept if getattr(cfg, "id", None) is not None]


def _profile_of(value: object) -> str:
    """`analysis_profile` 的签名取值：缺键 / 空值按 `all`，**不再二次归一**。

    `tables` 项目一张配表都没有时，`build_weekly_payload` 已经把生效档改写成 `all` 并
    落进 payload；这里若再按「认不出来就 tables」归一，签名会把那一轮回退判成另一个
    范围 —— 同一个 payload 两次算出的签名必须逐字相等。
    """
    return str(value or "").strip().lower() or PROFILE_ALL


def _focus_of(value: object) -> str:
    """`focus.key` 的签名取值：缺键 / 脏值 / 空串一律按 `all`（与 `focus_scope` 同口径）。"""
    if not isinstance(value, Mapping):
        return FOCUS_ALL
    return str(value.get("key") or "").strip().lower() or FOCUS_ALL


def _repository_ids_of(value: object) -> Tuple[Any, ...]:
    """`repositories` 里的仓库 id 集合：去重、按字符串排序（int / str 混排也稳定）。"""
    if not isinstance(value, (list, tuple)):
        return ()
    ids = {
        item.get("repository_id")
        for item in value
        if isinstance(item, Mapping) and item.get("repository_id") is not None
    }
    return tuple(sorted(ids, key=str))


def scope_signature(payload: Optional[Mapping[str, Any]]) -> Tuple[str, str, Tuple[Any, ...]]:
    """这一轮**分析范围**的签名：`(analysis_profile, focus.key, 输入仓库 id 集合)`。

    ## 为什么需要它（P1，2026-11-01）

    `baseline_source.previous_run` 原先只按 `target_type/target_key` 取最近一条有结论的
    运行。但分析范围可以中途改（profile / focus / 仓库集），范围一改，上一轮那批结论
    回答的就是**另一批文件**的问题：实测把 `profile=all` 下代码仓库的 critical 当基线、
    再以 `profile=tables` 跑，那条 critical 被原样继承进新报告，风险等级也被它抬高。
    所以「同一个分组」不等于「同一个范围」，签名是这两件事之间的那条边界。

    口径（读侧只认 payload 里**冻结的生效值**，不回头读配置）：

    * `analysis_profile`：缺键按 `all`；`tables` 没有配表仓库时 payload 写的是回退后的
      `all`，以 payload 为准；
    * `focus.key`：缺键 / 空值按 `all`；
    * 输入仓库 id 集合：`payload["repositories"]` 的 `repository_id`（去重排序；仓库集
      已按 profile 收窄，所以它同时反映「这次到底装进了哪些仓库」）。

    **缺 `repositories` 键时回空元组**：`group.config_ids` 存的是配置 id，不查库换不出
    仓库 id —— 本函数是纯函数、不碰库、不做判定。这个空元组**不是**「范围是空」的
    结论：2026-11 之前的旧 payload 压根没有这个键，读侧（`baseline_source`）会把这类
    payload 判成「无法判」直接跳过，不会拿这里的默认值去比。
    """
    data = payload if isinstance(payload, Mapping) else {}
    return (
        _profile_of(data.get("analysis_profile")),
        _focus_of(data.get("focus")),
        _repository_ids_of(data.get("repositories")),
    )


def scope_signature_for_facts(
    analysis_profile: object, focus: object, repository_ids: Sequence[Any]
) -> Tuple[str, str, Tuple[Any, ...]]:
    """payload 还没组装出来时，用同样的三样事实算**同一份**签名。

    为什么需要：`ai_analysis_service._concluded_run` 在 `build_weekly_payload` **中途**
    就要判范围（补偿集要读上一轮结论），那时 payload 还不存在。三样事实必须与最终
    payload 同源 —— `analysis_profile` 传回退后的生效值、`focus` 传本次入参、仓库 id 取
    **已按 profile 收窄**的配置（`_summarize_weekly_files` 正是为这些配置建
    `repositories`），于是这里算出的就是 `scope_signature(payload)` 那一份。
    """
    return scope_signature(
        {
            "analysis_profile": analysis_profile,
            "focus": {"key": focus},
            "repositories": [{"repository_id": item} for item in repository_ids],
        }
    )
