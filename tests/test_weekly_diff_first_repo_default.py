# -*- coding: utf-8 -*-
"""周版本 diff 页要默认落在**第一个**仓库。

## 缺陷形态（线上实测）

打开 `/weekly-version-config/1/diff`，标签页渲染出来了，但高亮的不是第一个，
而是**最后新建**的那个仓库的标签。

根因是一条链，不是偶发：

* `services/weekly_version_logic.py` 按 `created_at DESC` 取 config，并沿用这个
  顺序把它们 append 进 `version_groups[key]['configs']` —— 于是 `configs[0]`
  是「最新建的」；
* `templates/merged_project_view.html` 的入口链接用 `version.configs[0].id`
  （Jinja 一份、`inactive_versions_json` 驱动的 JS 一份）；
* 而 diff 页的标签顺序是 `repository.name` 升序，高亮由
  `cfg.id == current_config_id` 决定。

三处排序口径互不相同（入口 `created_at DESC`、标签名称升序、配置列表 API 无序），
所以「点进去高亮的不是第一个」是必然的。

## 现在的口径

「第一个仓库」= 该版本内 config 按 `(仓库 display_order, 仓库名, 仓库 id, config id)`
排序后的首个 —— 实现在 `services/repository_ordering.py`，入口链接、标签顺序、
配置列表 API 共用这一把尺子。

**URL 里的 `config_id` 仍然是当前仓库**：深链、刷新、前进后退都不受影响，
只是入口链接现在指向的正是排第一的那个。
"""
from __future__ import annotations

import os
import re
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.repository_ordering import (  # noqa: E402
    repository_order_key,
    weekly_config_order_key,
)


def _repo(repo_id, name, display_order=0):
    return SimpleNamespace(id=repo_id, name=name, display_order=display_order)


def _config(config_id, repo):
    return SimpleNamespace(id=config_id, repository=repo)


# ---------------------------------------------------------------------------
#  排序键本身
# ---------------------------------------------------------------------------

class TestTheCanonicalOrderKey:
    def test_display_order_wins_over_name(self):
        """用户拖拽排过的顺序优先于名称 —— 那是他自己排的。"""
        first = _config(1, _repo(1, "zzz", display_order=0))
        second = _config(2, _repo(2, "aaa", display_order=1))
        assert sorted([second, first], key=weekly_config_order_key) == [first, second]

    def test_name_breaks_ties_so_unordered_projects_keep_todays_order(self):
        """`display_order` 对所有仓库默认是 0（没人拖过就是全 0）。
        这一级退化成「按名称」—— 与 diff 页原来的标签顺序一致，
        所以存量部署不会看到顺序变化。"""
        b = _config(2, _repo(2, "b_repo"))
        a = _config(1, _repo(1, "a_repo"))
        assert sorted([b, a], key=weekly_config_order_key) == [a, b]

    def test_id_makes_the_key_total(self):
        """同名同序（重名仓库是可能的，库里没有唯一约束）时靠 id 定序，
        否则顺序由数据库返回顺序决定 —— 那正是本缺陷的成因之一。"""
        x = _config(7, _repo(3, "same"))
        y = _config(9, _repo(3, "same"))
        assert sorted([y, x], key=weekly_config_order_key) == [x, y]

    def test_a_junk_display_order_does_not_raise(self):
        """`display_order` 是从表单 `int()` 来的，历史数据可能是 NULL 或脏值。
        脏值只该影响排序，不该把整页打成 500。"""
        for junk in (None, "", "not-an-int", [], object()):
            repo = SimpleNamespace(id=1, name="r", display_order=junk)
            key = repository_order_key(repo)
            assert isinstance(key, tuple), f"{junk!r} 让排序键炸了"

    def test_a_missing_repository_sorts_last_instead_of_raising(self):
        """仓库被删但配置行还在时，页面仍然要能打开。"""
        orphan = _config(5, None)
        real = _config(6, _repo(1, "a"))
        assert sorted([orphan, real], key=weekly_config_order_key) == [real, orphan]
        assert repository_order_key(None) == (1, 0, "", 0)


# ---------------------------------------------------------------------------
#  两条渲染路径必须指向同一个 config
# ---------------------------------------------------------------------------

class TestBothRenderPathsAgree:
    """合并视图有两个渲染路径：服务端 Jinja（活跃版本卡片）和
    `inactive_versions_json` 驱动的 JS（非活跃版本）。它们都从同一份
    `version_groups` 派生，所以「第一个」必须是同一个。"""

    @pytest.fixture()
    def groups(self):
        """刻意让 `created_at` 顺序与规范顺序**相反**：
        最新建的（config 3）属于排序最靠后的仓库，第一个仓库的 config 最早建。
        这正是线上形态 —— 旧实现会让入口指向 config 3。"""
        first = _config(11, _repo(1, "a_first"))
        middle = _config(12, _repo(2, "m_middle"))
        newest = _config(13, _repo(3, "z_newest"))
        # 模拟 `created_at DESC` 的追加顺序：最新的在最前
        return [newest, middle, first]

    def test_the_entry_link_target_is_the_canonical_first(self, groups):
        ordered = sorted(groups, key=weekly_config_order_key)
        assert ordered[0].id == 11, (
            "入口链接会指向 configs[0]，而它必须是规范顺序里的第一个；"
            f"现在指向 {ordered[0].id}（线上形态是 13 = 最新建的那个）"
        )

    def test_the_js_path_serializes_the_same_order(self, groups):
        """`inactive_versions_json` 是按 `version['configs']` 的顺序 append 的，
        所以排序后的列表首项 = JS 里 `version.configs[0].id`。"""
        ordered = sorted(groups, key=weekly_config_order_key)
        serialized = [{"id": c.id} for c in ordered]
        assert serialized[0]["id"] == ordered[0].id == 11

    def test_sorting_is_stable_and_idempotent(self, groups):
        once = sorted(groups, key=weekly_config_order_key)
        assert sorted(once, key=weekly_config_order_key) == once


# ---------------------------------------------------------------------------
#  源码级：三处必须用同一个 key，不许再长出第四种排序
# ---------------------------------------------------------------------------

def _weekly_logic_source():
    path = os.path.join(PROJECT_ROOT, "services", "weekly_version_logic.py")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestNoFourthOrderingAppears:
    def test_the_merged_view_sorts_the_group_configs(self):
        """合并视图那条 `for group in version_groups.values():` 循环里必须有排序，
        否则 `configs[0]` 又回到 `created_at DESC` 的顺序。"""
        src = _weekly_logic_source()
        block = re.search(
            r"for group in version_groups\.values\(\):(.*?)\n\n", src, re.S)
        assert block, "找不到分组循环 —— 结构变了，这条用例要跟着改"
        assert "weekly_config_order_key" in block.group(1), (
            "分组循环里没有按统一口径排序：入口链接会指向最新建的仓库，"
            "而不是第一个（线上症状）"
        )

    def test_the_diff_page_tabs_use_the_same_key(self):
        """标签顺序与「第一个」必须是同一把尺子。"""
        src = _weekly_logic_source()
        assert "all_configs.sort(key=weekly_config_order_key)" in src, (
            "diff 页标签没有用统一口径排序"
        )
        assert "key=lambda c: c.repository.name" not in src, (
            "标签又回去按仓库名排序了 —— 那样「默认选中第一个」又会各说各话"
        )
