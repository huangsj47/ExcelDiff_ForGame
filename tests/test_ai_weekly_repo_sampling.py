# -*- coding: utf-8 -*-
"""周版本取样的仓库公平性 + 仓库优先级守卫。

## 缺陷形态（线上可见）

周版本配置里挂了两个仓库：配表仓库（19 个文件）与代码仓库（748 个文件），
`max_files_per_run = 200`。取样是「按全局优先级排序后取前 200」，于是配表被整个
挤出清单 —— 线上那轮进榜的配表只有 6 张，另外十几张模型从头到尾没机会看到、
白名单也不允许它读。而配表改的恰恰是数值、ID、奖励这些评审最关心的东西。

排序键里那个 `priority` 本该救场，但它是**退化的**：`_repo_priority` 当时写的是
`if resource_type == "code" or repo_type == "git": return 2`，而线上两个仓库的
`type` 都是 `git`，于是双双返回 2 —— `policy.sample_strategy` 写着
`priority_then_commit_count`，实际只按 `commit_count` 排。

**这两处必须一起修**：只把优先级修对，748 个 lua 会先把 200 个名额吃光，
配表一张都进不去，比修之前更糟。所以取样改成「各仓库轮流发牌」。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.ai_analysis_service import (
    _repo_priority,
    _sample_with_repo_fairness,
    select_primary_weekly_config,
)

# 线上那轮的真实量级
CODE_FILES = 748
TABLE_FILES = 19
MAX_FILES = 200


def _online_items():
    """复刻线上：代码仓库 748 个 lua + 配表仓库 19 张 xlsx，按全局优先级降序。"""
    items = [
        {"repository_id": 2, "priority": 2, "commit_count": 5, "file_path": f"code/x{i}.lua"}
        for i in range(CODE_FILES)
    ]
    items += [
        {"repository_id": 1, "priority": 1, "commit_count": 2, "file_path": f"config/t{i}.xlsx"}
        for i in range(TABLE_FILES)
    ]
    items.sort(key=lambda x: (x["priority"], x["commit_count"], x["file_path"]), reverse=True)
    return items


# --------------------------------------------------------------------------
# 取样公平性
# --------------------------------------------------------------------------

def test_a_small_repository_is_not_squeezed_out_of_the_sample():
    """核心回归：文件少的仓库不能被整个挤出清单。

    线上那轮配表只进了 6 张、其余十几张模型完全看不到；这里要求 19 张全进。
    """
    picked = _sample_with_repo_fairness(_online_items(), MAX_FILES)
    table_picked = [x for x in picked if x["repository_id"] == 1]
    assert len(table_picked) == TABLE_FILES, (
        f'配表只进了 {len(table_picked)}/{TABLE_FILES} 张 —— 文件少的仓库又被挤出去了'
    )
    assert len(picked) == MAX_FILES, f"没取满 {MAX_FILES}，实际 {len(picked)}"


def test_the_big_repository_takes_the_remaining_slots():
    """轮流发牌之后，多出来的名额应当全归文件多的仓库，不浪费配额。"""
    picked = _sample_with_repo_fairness(_online_items(), MAX_FILES)
    assert len([x for x in picked if x["repository_id"] == 2]) == MAX_FILES - TABLE_FILES


def test_the_sample_keeps_the_global_priority_order():
    """展示口径不能变：返回的清单仍按全局优先级排，不是轮转顺序。

    清单会被渲染进提示词，顺序变了等于又换了一种「谁更重要」的口径。
    """
    items = _online_items()
    picked = _sample_with_repo_fairness(items, MAX_FILES)
    key = lambda x: (x["priority"], x["commit_count"], x["file_path"])  # noqa: E731
    assert picked == sorted(picked, key=key, reverse=True)


def test_the_sample_is_deterministic():
    """同样的输入必须给出同样的清单 —— 否则同一版本两次分析看到的文件不同。"""
    assert _sample_with_repo_fairness(_online_items(), MAX_FILES) == \
           _sample_with_repo_fairness(_online_items(), MAX_FILES)


def test_nothing_changes_when_everything_fits():
    items = _online_items()[:5]
    assert _sample_with_repo_fairness(items, MAX_FILES) == items


def test_a_single_repository_behaves_exactly_like_a_plain_truncation():
    """只有一个仓库时，轮流发牌必须退化成原来的「取前 N 条」。

    这条拦的是「为了公平把单仓库场景也改了」—— 那会让单仓库项目的取样行为
    无谓地变化。
    """
    only_code = [x for x in _online_items() if x["repository_id"] == 2]
    assert _sample_with_repo_fairness(only_code, 50) == only_code[:50]


def test_a_zero_or_negative_limit_means_no_limit():
    items = _online_items()
    assert _sample_with_repo_fairness(items, 0) == items


@pytest.mark.parametrize("max_files", [1, 2, 20])
def test_a_tiny_limit_does_not_crash_or_overrun(max_files):
    """上限比仓库数还小时也不能越界或死循环。"""
    picked = _sample_with_repo_fairness(_online_items(), max_files)
    assert len(picked) == max_files
    assert len(set(id(x) for x in picked)) == max_files, "取到了重复条目"


# --------------------------------------------------------------------------
# 仓库优先级
# --------------------------------------------------------------------------

def test_a_git_code_repository_outranks_a_table_repository():
    """`type == "git"` 不是「这是代码仓库」的判据 —— 配表仓库也可以是 git。"""
    code = SimpleNamespace(resource_type="code", type="git")
    table = SimpleNamespace(resource_type="table", type="git")
    assert _repo_priority(code) > _repo_priority(table), (
        "两个都是 git 时优先级又相等了 —— 线上就是这么退化的"
    )


def test_the_priority_is_decided_by_resource_type_alone():
    """同样的 resource_type，type 是 git 还是 svn 都不该改变优先级。"""
    assert _repo_priority(SimpleNamespace(resource_type="table", type="git")) == \
           _repo_priority(SimpleNamespace(resource_type="table", type="svn"))
    assert _repo_priority(SimpleNamespace(resource_type="code", type="git")) == \
           _repo_priority(SimpleNamespace(resource_type="code", type="svn"))


def test_a_repository_without_a_resource_type_is_not_treated_as_code():
    for missing in (None, "", "unknown"):
        assert _repo_priority(SimpleNamespace(resource_type=missing, type="git")) == 1, (
            f"resource_type={missing!r} 被当成了代码仓库"
        )


# --------------------------------------------------------------------------
# 分组身份与取样优先级解耦
# --------------------------------------------------------------------------

def test_the_primary_config_is_still_the_lowest_id():
    """`select_primary_weekly_config` 决定新建分组的 base_name（进而决定 group_key），
    必须保持既有口径：取 id 最小的那个。

    它以前跟着（当时退化的）仓库优先级走，实际效果就是「id 最小」。优先级修好之后
    若继续跟随，就会变成「代码仓库当主」—— 那是分组身份变更，已有分组的增量水位线
    会全部对不上。
    """
    code_repo = SimpleNamespace(resource_type="code", type="git")
    table_repo = SimpleNamespace(resource_type="table", type="git")
    # 故意让 id 大的是配表、id 小的是代码，验证选的是 id 而不是优先级
    configs = [
        SimpleNamespace(id=1, repository=code_repo),
        SimpleNamespace(id=2, repository=table_repo),
    ]
    assert select_primary_weekly_config(configs).id == 1
    # 反过来也一样：选的仍是 id 最小的
    configs = [
        SimpleNamespace(id=5, repository=table_repo),
        SimpleNamespace(id=9, repository=code_repo),
    ]
    assert select_primary_weekly_config(configs).id == 5
