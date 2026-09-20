# -*- coding: utf-8 -*-
"""表头坐标：AI 取数层必须认识仓库的 `header_rows` / `header_name_row`。

## 缺陷形态（修前）

diff 引擎的列名由 `Repository.header_name_row` 决定（列名取表头块里的第几行）、
表头块由 `Repository.header_rows` 决定；而 AI 取数层
（`services/ai/platform_provider._read_excel_sheets`）**完全不认识这两个配置**，
一律「第一个非空行当列名」。于是「第 1 行是大标题、第 2 行才是字段名」
（`header_name_row=2` 存在的唯一理由）的表上：

* diff 侧列名取第 2 行，AI 侧把第 1 行当列名 —— **两边列名不是同一套**；
* 第 2 行（真正的字段名行）在 AI 侧被当成**数据第一行**统计进去：每个文本列于是多出
  一个假的「只出现一次的取值」（字段名自己），而模型正是拿这些取值分布判断
  「这个值在不在允许集合里」。`header_rows=3` 的表则第 2、3 行都被当成数据。

两端现在都读同一份仓库配置，本文件断言的就是「**两侧的列名与数据行坐标一致**」，
而不是「某个函数有没有收到某个参数」。

## 为什么这一条也算「同一个概念有多份实现」

「列名是哪一行 / 表头占几行」是**一个**概念，diff 引擎早就有权威答案
（`services/diff_service.py::_header_row_count` / `_header_name_row`），AI 取数层却自己
发明了「第一个非空行」。规范化那两个函数**不重写**：AI 侧直接调 `DiffService` 的那两个
staticmethod，免得「几算合法、越界怎么办」再出现第二套判断。
"""
from __future__ import annotations

import io
import os
import re
import sys
import uuid

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.ai.platform_provider import _read_excel_sheets  # noqa: E402
from services.diff_service import DiffService  # noqa: E402

COMMIT = 'a' * 40
PATH_XLSX = 'config/角色属性表.xlsx'

# 物理第 1 行是大标题、第 2 行才是字段名的表 —— `header_name_row=2` 就是为它存在的。
# `header_rows=2` 说明表头块占物理第 1、2 行。
TITLE_ROW = ['角色属性表', None, None]
NAME_ROW = ['id', '名称', '品质']
BODY_ROWS = [
    ['1', '甲', 'A'],
    ['2', '乙', 'B'],
    ['3', '丙', 'A'],
]
TABLE = [TITLE_ROW, NAME_ROW, *BODY_ROWS]

# 表头块占 3 行（第 1 行标题、第 2 行字段名、第 3 行单位说明）的表。
THREE_ROW_TABLE = [
    ['角色属性表', None, None],
    ['id', '名称', '品质'],
    ['编号', '显示名', '枚举'],
    *BODY_ROWS,
]

_HEAD = re.compile(r'- 整表统计（[^）]*）')
_COLUMN = re.compile(r'第 (\d+) 列（(?:列名|首行)「(.*?)」）')
_PARTICIPATING = re.compile(r'其余 (\d+) 行参与统计')


def _workbook_bytes(rows):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = 'Sheet1'
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row, start=1):
            sheet.cell(row=row_index, column=col_index, value=value)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def _ai_labels(rendered: str):
    """AI 渲染出来的**列名**（按列序）。"""
    return [label for _number, label in _COLUMN.findall(rendered)]


def _ai_stats_heading(rendered: str) -> str:
    """那行「整表统计（…）」的抬头（里面有「其余 N 行参与统计」）。"""
    found = _HEAD.search(rendered)
    return found.group(0) if found else ''


def _diff_columns(rows, *, header_rows=None, header_name_row=None):
    """diff 引擎给出的列名（页面与 AI 平时读的那一份）。"""
    raw = _workbook_bytes(rows)
    payload = DiffService().process_diff(
        PATH_XLSX, raw, raw, header_rows=header_rows, header_name_row=header_name_row
    )
    return payload['sheets']['Sheet1']['headers']


class TestBothSidesUseTheSameColumnNames:
    """两侧的列名必须是同一套（同一张表、同一份仓库配置）。"""

    def test_name_row_two_gives_the_same_columns_on_both_sides(self):
        """`header_name_row=2`：diff 侧与 AI 侧的列名都取物理第 2 行。"""
        engine_columns = _diff_columns(TABLE, header_rows=2, header_name_row=2)
        rendered = _read_excel_sheets(
            _workbook_bytes(TABLE), max_rows=50, path=PATH_XLSX,
            header_rows=2, header_name_row=2,
        )

        assert engine_columns == NAME_ROW, f'diff 侧的列名不是第 2 行：{engine_columns}'
        assert _ai_labels(rendered) == engine_columns, (
            f'两侧列名不是同一套。\n— diff 引擎 —\n{engine_columns}\n'
            f'— AI 取数 —\n{_ai_labels(rendered)}\n完整渲染：\n{rendered}'
        )
        assert '角色属性表' not in _ai_labels(rendered), (
            f'AI 侧把大标题行当成了列名（那正是 `header_name_row=2` 要修的事）：{rendered}'
        )

    def test_the_name_row_is_not_counted_as_data(self):
        """第 2 行（字段名行）不许进统计 —— 否则每个文本列都多一个假的「只出现一次」的值。"""
        rendered = _read_excel_sheets(
            _workbook_bytes(TABLE), max_rows=50, path=PATH_XLSX,
            header_rows=2, header_name_row=2,
        )

        assert _PARTICIPATING.search(rendered).group(1) == str(len(BODY_ROWS)), (
            f'参与统计的行数不是数据行数（{len(BODY_ROWS)}）：{_ai_stats_heading(rendered)}'
        )
        # 品质列只有 A、B 两个取值；把字段名行当数据会变成三个（多一个「品质(1)」）。
        assert '不同取值 2' in rendered, f'字段名行被当成数据统计了：{rendered}'
        assert '品质(1)' not in rendered, (
            f'字段名「品质」出现在取值分布里 —— 模型会把它当成一个真实的枚举值：{rendered}'
        )

    def test_header_rows_three_keeps_two_header_rows_out_of_the_data(self):
        """`header_rows=3`：物理第 2、3 行都不进统计与正文。"""
        engine_columns = _diff_columns(THREE_ROW_TABLE, header_rows=3, header_name_row=2)
        rendered = _read_excel_sheets(
            _workbook_bytes(THREE_ROW_TABLE), max_rows=50, path=PATH_XLSX,
            header_rows=3, header_name_row=2,
        )

        assert engine_columns == NAME_ROW, engine_columns
        assert _ai_labels(rendered) == engine_columns, (engine_columns, rendered)
        assert _PARTICIPATING.search(rendered).group(1) == str(len(BODY_ROWS)), rendered
        assert '表头共 3 行' in _ai_stats_heading(rendered), (
            f'被当成表头的那几行必须如实说出来（否则看起来像凭空少了两行）：{rendered}'
        )

    def test_header_rows_without_a_name_row_keeps_row_one_as_labels(self):
        """只配 `header_rows=3`（名称行仍是默认的第 1 行）：列名仍是第 1 行，2、3 行进表头块。"""
        table = [
            ['id', '名称', '品质'],
            ['编号', '显示名', '枚举'],
            ['单位', '-', '-'],
            *BODY_ROWS,
        ]
        engine_columns = _diff_columns(table, header_rows=3)
        rendered = _read_excel_sheets(
            _workbook_bytes(table), max_rows=50, path=PATH_XLSX, header_rows=3,
        )

        assert engine_columns == ['id', '名称', '品质'], engine_columns
        assert _ai_labels(rendered) == engine_columns, (engine_columns, rendered)
        assert _PARTICIPATING.search(rendered).group(1) == str(len(BODY_ROWS)), rendered
        assert '编号' not in rendered and '单位' not in rendered, (
            f'表头块里的行漏进正文了：{rendered}'
        )


class TestUnconfiguredKeepsTodaysBehaviour:
    """**不传就是今天的行为**：列名取第一个非空行，其余行全算数据。

    默认口径必须明确（仓库没配表头坐标的占绝大多数），所以这一条是回归保护：
    文案与行数都与修前逐字一致（`tests/test_ai_platform_provider.py` 那条「首行按列名」
    的断言也建立在它上面）。
    """

    def test_first_non_empty_row_is_the_label_row(self):
        rendered = _read_excel_sheets(_workbook_bytes(TABLE), max_rows=50, path=PATH_XLSX)

        # 只有非空的列名会被写进统计行（空列名不写「（首行「」）」），所以这里比的是
        # 「这一行的非空取值」—— 大标题行只有一个格子有字。
        assert _ai_labels(rendered) == ['角色属性表'], (
            f'未配置时的列名不是第一个非空行：{_ai_labels(rendered)}'
        )
        assert '首行按列名' in _ai_stats_heading(rendered), (
            f'未配置时的统计抬头文案变了（既有断言按「首行按列名」认统计块）：{rendered}'
        )
        assert _PARTICIPATING.search(rendered).group(1) == str(len(TABLE) - 1), rendered
        assert '（首行「角色属性表」）' in rendered, rendered


class TestTheAgentPathUsesTheRepositoryConfig:
    """Agent 那条路也要用仓库配置（两端同一个渲染函数的**同一个坐标**）。"""

    @pytest.fixture(scope='module')
    def configured_repository_id(self):
        from app import app, create_tables, db
        from models import Project, Repository

        token = uuid.uuid4().hex[:10]
        with app.app_context():
            create_tables()
            project = Project(code=f'HDR{token}', name=f'header-coords-{token}')
            db.session.add(project)
            db.session.flush()
            repository = Repository(
                project_id=project.id, name=f'repo-hdr-{token}', type='git',
                url='https://example.invalid/game.git', branch='main', clone_status='completed',
                # 这一行就是被测的东西：仓库上的表头配置。
                header_rows=2, header_name_row=2,
            )
            db.session.add(repository)
            db.session.commit()
            return repository.id

    def test_agent_renders_with_the_repository_header_config(
        self, monkeypatch, configured_repository_id
    ):
        from app import app

        raw = _workbook_bytes(TABLE)
        with app.app_context():
            from services.agent_file_content_reader import read_file_content_for_agent

            monkeypatch.setattr(
                'services.vcs_content_service.get_file_content_from_git',
                lambda repository, commit_id, file_path: raw,
            )
            got = read_file_content_for_agent({
                'repository_id': configured_repository_id,
                'commit_id': COMMIT,
                'file_path': PATH_XLSX,
            })

        engine_columns = _diff_columns(TABLE, header_rows=2, header_name_row=2)
        rendered = got['content']
        assert _ai_labels(rendered) == engine_columns, (
            f'Agent 侧列名与 diff 引擎不是同一套：\n— diff —\n{engine_columns}\n'
            f'— Agent —\n{_ai_labels(rendered)}\n{rendered}'
        )
        assert _PARTICIPATING.search(rendered).group(1) == str(len(BODY_ROWS)), rendered
