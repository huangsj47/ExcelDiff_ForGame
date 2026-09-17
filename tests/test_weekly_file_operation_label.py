# -*- coding: utf-8 -*-
"""周版本列表里给文件上色的操作类型，必须跟**窗口内的最终状态**一致。

## 线上反馈

`config/奖励模式_CfgRewardMode.xlsx` 在同一周里被建了删、删了建：

    d1a0fa97 A（新增）→ 938f37b6 D（删除）→ f255c3ba A（重新新增）
             → 7e4587c3 M → f0724d7d M（改 id 规范）

它在窗口结束时**存在**，diff 页比的也是窗口首末两版，显示 5 行新增。但周版本
列表把文件名标成红色 —— 旧的 `primary_operation` 口径是「操作序列里出现过 D
就标红」，于是列表说「删除文件」、页面说「新增内容」，两者互相打脸。

评审者会照着列表的颜色决定先看哪个文件：把一个仍然存在的文件标成删除，比不标
更糟。现在的口径跟最终状态走（见 `services/weekly_deleted_excel_helpers.py`）：

* 最后一次操作是删除 → `D`（文件在窗口结束时确实不存在，与 diff 页的
  「已删除」判定同源）；
* 首次操作是新增（且没被删掉）→ `A`（窗口内新建、且仍然存在）；
* 其余 → `M`。
"""
from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.weekly_deleted_excel_helpers import resolve_primary_operation  # noqa: E402

WEEKLY_LOGIC = os.path.join(PROJECT_ROOT, 'services', 'weekly_version_logic.py')


class TestOperationLabel:
    @pytest.mark.parametrize('operations,expected,why', [
        # 线上 奖励模式_CfgRewardMode.xlsx：建→删→建→改→改，最终存在
        (['A', 'D', 'M', 'M', 'M'], 'A', '窗口内新建、中途被删、最后仍然存在 → 新增'),
        # 线上 奖励模式表_CfgRewardMode.xlsx：建→删，最终不存在
        (['A', 'D'], 'D', '最后一步是删除 → 删除文件（与 diff 页的已删除一致）'),
        (['A'], 'A', '窗口内新增'),
        (['M'], 'M', '只有修改'),
        (['M', 'M', 'M'], 'M', '多次修改'),
        (['M', 'D'], 'D', '先改后删'),
        (['D'], 'D', '只有删除'),
        (['D', 'A'], 'M', '删掉又建回来、且窗口前就存在 → 修改'),
        (['A', 'D', 'A'], 'A', '建删建，最终存在 → 新增'),
        (['A', 'D', 'A', 'D'], 'D', '最后一次仍是删除 → 删除'),
        (['m', 'd'], 'D', '大小写不敏感'),
        (['DELETED'], 'D', '兼容长写法'),
        ([], 'M', '没有操作记录时按修改处理'),
        (None, 'M', '载荷里没有 operations 时按修改处理'),
    ])
    def test_the_label_follows_the_final_state(self, operations, expected, why):
        assert resolve_primary_operation(operations) == expected, why

    def test_a_deleted_file_is_still_red(self):
        """别为了修「误标红」把真正的删除也一起放过。"""
        assert resolve_primary_operation(['A', 'D']) == 'D'
        assert resolve_primary_operation(['M', 'D']) == 'D'

    def test_the_reported_case_is_not_red(self):
        """线上那条：标红会让评审者以为文件没了，而它就在那儿。"""
        assert resolve_primary_operation(['A', 'D', 'M', 'M', 'M']) == 'A'


class TestTheListUsesThisRule:
    def test_the_weekly_list_resolves_through_the_helper(self):
        with open(WEEKLY_LOGIC, encoding='utf-8') as handle:
            text = handle.read()
        assert '_resolve_primary_operation_helper(file_operations)' in text, (
            '周版本列表没有走 resolve_primary_operation —— 色标口径又各算各的了'
        )

    def test_the_old_membership_rule_is_gone(self):
        """「操作里出现过 D 就标红」这行不许回来。"""
        with open(WEEKLY_LOGIC, encoding='utf-8') as handle:
            text = handle.read()
        assert "'D' in file_operations" not in text, (
            '「序列里出现过 D 就标成删除」的旧口径又回来了 —— '
            '被删掉又建回来的文件会在列表里标红，而它的 diff 页显示的是新增内容'
        )
