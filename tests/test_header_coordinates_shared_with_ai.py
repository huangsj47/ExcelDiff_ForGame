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
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

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


@pytest.fixture(scope='module')
def configured_repository_id():
    """配了表头坐标的仓库行（`header_rows=2` / `header_name_row=2`）。

    模块级（而不是挂在某个类里）：下面两组用例都要用它 —— 一组证明 Agent **回落**时
    读得到这一行，另一组证明 payload 给了坐标时**不听**这一行。同一个仓库两种用法，
    对照才干净。
    """
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


class TestTheAgentPathUsesTheRepositoryConfig:
    """Agent 那条路也要用仓库配置（两端同一个渲染函数的**同一个坐标**）。

    这条是**回落**路径：payload 里没有表头坐标时，Agent 读本节点那一行（见
    `TestTheAgentSideObeysThePayload` 最后一组）。
    """

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


# ---------------------------------------------------------------------------
# 表头坐标必须随 payload 走（2026-09-20）
# ---------------------------------------------------------------------------
#
# 上一组证明的是「Agent 认识仓库的表头配置」。但如果它只认**自己库里**那一行，
# 两个节点的库不是同一份时（节点用了旧数据库、配置改了还没同步），同一次索取在单机与
# 多节点下会渲染出**两套列名**：模型据此写出的「这个取值不在允许集合里」全是错的，
# 而界面上完全看不出来。
#
# 所以坐标由**请求方**给（payload 里的 `header_rows` / `header_name_row`），Agent 侧
# 以它为准；payload 没带才回落到本节点那一行，并且**必须留痕** —— 静默回落正是这个
# 毛病能被藏起来的原因。


class TestTheAgentSideObeysThePayload:
    """payload 里的表头坐标说了算（真 xlsx：断言的是渲染出来的列名，不是「参数传进去了」）。"""

    def _read(self, monkeypatch, repository_id, payload, rows=TABLE, path=PATH_XLSX):
        from app import app

        raw = _workbook_bytes(rows)
        with app.app_context():
            from services.agent_file_content_reader import read_file_content_for_agent

            monkeypatch.setattr(
                'services.vcs_content_service.get_file_content_from_git',
                lambda repository, commit_id, file_path: raw,
            )
            return read_file_content_for_agent({
                'repository_id': repository_id, 'commit_id': COMMIT, 'file_path': path, **payload,
            })

    def test_the_payload_config_overrides_the_local_repository_row(
        self, monkeypatch, configured_repository_id
    ):
        """本节点这一行是 `2|2`，payload 说 `1|1` —— 必须按 payload 渲染。

        按本节点那一行渲染的话列名会是 `id/名称/品质`（第 2 行），而请求方（平台本地那条
        路读的是同一份 payload）要的是 `角色属性表`（第 1 行）：同一次索取于是有两份文本，
        模型看到的是哪一份取决于它问的是哪个节点。
        """
        got = self._read(
            monkeypatch, configured_repository_id, {'header_rows': 1, 'header_name_row': 1}
        )
        rendered = got['content']

        assert got['header_config_source'] == 'payload', f'没用 payload 的坐标：{got}'
        assert _ai_labels(rendered) == ['角色属性表'], (
            f'渲染用的是本节点那一行的 2|2（而不是 payload 的 1|1）：\n{rendered}'
        )
        assert _ai_labels(rendered) != NAME_ROW, '按本节点的配置渲染了 —— payload 被忽略了'
        # 1|1 = 未配置：第 1 行按列名吃掉，剩下的（含真正的字段名行）都算数据。
        assert _PARTICIPATING.search(rendered).group(1) == str(len(TABLE) - 1), rendered

    def test_a_payload_that_says_unconfigured_is_not_a_fallback(
        self, monkeypatch, configured_repository_id
    ):
        """payload 里**明确写了一对 `None`**：按未配置渲染，不许回落到本节点的 `2|2`。

        「请求方说没配」与「请求方没说」是两件事。前者回落到本节点那一行，就回到了
        「两端各按各的库渲染」这个毛病本身 —— 而仓库没配表头坐标的占绝大多数，
        真回落到那一步的话，这个修法对最常见的那些仓库一点用都没有。
        """
        got = self._read(
            monkeypatch, configured_repository_id,
            {'header_rows': None, 'header_name_row': None},
        )

        assert got['header_config_source'] == 'payload', (
            f'payload 给了值（哪怕是未配置）就不该回落：{got}'
        )
        assert _ai_labels(got['content']) == ['角色属性表'], got['content']

    def test_a_payload_without_the_keys_falls_back_and_says_so(
        self, monkeypatch, configured_repository_id
    ):
        """payload 没带这两个键（漏传的调用方 / 改动之前排队的旧任务）：回落 + **留痕**。

        回落读的是本节点自己那一行，值可能已经过时；不说出来的话，「两端列名不一致」
        这件事在两边都看不见 —— 那正是这一整批要消灭的毛病。
        """
        warnings = []
        monkeypatch.setattr(
            'services.agent_file_content_reader.log_print',
            lambda message, *args, **kwargs: warnings.append(str(message)),
        )
        got = self._read(monkeypatch, configured_repository_id, {})

        assert got['header_config_source'] == 'repository', got
        # 回落值本身仍然要正确用上（本节点这一行是 2|2）
        assert _ai_labels(got['content']) == NAME_ROW, got['content']
        assert warnings, '回落必须写日志 —— 静默回落正是这个毛病能被藏起来的原因'
        assert '没带表头坐标' in warnings[0], warnings
        assert 'header_rows=2' in warnings[0] and 'header_name_row=2' in warnings[0], (
            f'日志里要能看出回落成了什么值：{warnings[0]}'
        )
        assert '表头坐标取自节点本地的仓库行' in got['message'], (
            f'回落这件事也要能从上一次取数的结果里看出来：{got["message"]}'
        )


class TestTheHeaderConfigTravelsEndToEnd:
    """端到端：平台真的派一条 `file_content` 任务 → Agent 按 payload 渲染。

    这一组不摆任何替身：真的 `AgentTask` 行、真的 AgentNode/绑定、真的仓库行。
    假 harness 能证明「payload 里有这两个键」，证明不了「Agent 拿到它之后渲染出的是
    同一套列名」—— 而后者才是这个缺陷的形态。
    """

    @pytest.fixture(scope='module')
    def agent_bound_repository(self):
        """一个**未配置表头坐标**的仓库 + 绑定在上面的在线 Agent（都是真实的行）。

        仓库的配置留空是有意的：它就是「节点自己那一行」。端到端要证明的正是
        「渲染听 payload 的，不听这一行的」——本地这一行未配置时按它渲染的列名会是
        『角色属性表』（第一个非空行），而请求方要的是第 2 行。
        """
        from app import app, create_tables, db
        from models import AgentNode, AgentProjectBinding, Project, Repository

        token = uuid.uuid4().hex[:10]
        with app.app_context():
            create_tables()
            project = Project(code=f'PAY{token}', name=f'payload-header-{token}')
            db.session.add(project)
            db.session.flush()
            repository = Repository(
                project_id=project.id, name=f'repo-pay-{token}', type='git',
                url='https://example.invalid/game.git', branch='main', clone_status='completed',
            )
            db.session.add(repository)
            node = AgentNode(
                agent_code=f'node-{token}', agent_name=f'node-{token}', agent_token=token,
                status='online', last_heartbeat=datetime.now(timezone.utc),
            )
            db.session.add(node)
            db.session.flush()
            db.session.add(AgentProjectBinding(
                agent_id=node.id, project_id=project.id, project_code=project.code,
            ))
            db.session.commit()
            return SimpleNamespace(repository_id=repository.id, project_id=project.id)

    def _dispatch(self, agent_bound_repository, *, path, header_rows, header_name_row):
        """按请求方那一行仓库的配置派一次取数，返回**落库后**的 payload。

        `wait_seconds=0`：不等 Agent 回来（这个用例要的是它派出去的那份 payload）。
        任务留在队列里也无妨 —— 用例之间用不同的 `path` 隔开，各自认自己的那份。
        """
        from app import app
        from models import AgentTask

        requester = SimpleNamespace(
            id=agent_bound_repository.repository_id,
            project_id=agent_bound_repository.project_id,
            header_rows=header_rows, header_name_row=header_name_row,
        )
        with app.app_context():
            from services.agent_file_content_dispatch import request_file_content

            outcome = request_file_content(
                requester, commit_id=COMMIT, file_path=path,
                wait_seconds=0.0, sleep_func=lambda _seconds: None,
            )
            assert outcome['status'] == 'pending', outcome
            task = (
                AgentTask.query.filter_by(
                    task_type='file_content', repository_id=requester.id
                )
                .order_by(AgentTask.id.desc())
                .first()
            )
            return json.loads(task.payload)

    def _render_via_payload(self, monkeypatch, agent_bound_repository, payload, rows=TABLE):
        from app import app

        raw = _workbook_bytes(rows)
        with app.app_context():
            from services.agent_file_content_reader import read_file_content_for_agent

            monkeypatch.setattr(
                'services.vcs_content_service.get_file_content_from_git',
                lambda repository, commit_id, file_path: raw,
            )
            return read_file_content_for_agent(payload)

    def test_the_dispatched_payload_carries_the_requester_header_config(
        self, agent_bound_repository
    ):
        payload = self._dispatch(
            agent_bound_repository, path='config/角色属性表.xlsx',
            header_rows=2, header_name_row=2,
        )

        assert payload['header_rows'] == 2, f'派出去的 payload 里没有表头行数：{payload}'
        assert payload['header_name_row'] == 2, f'派出去的 payload 里没有名称行：{payload}'
        assert payload['repository_id'] == agent_bound_repository.repository_id

    def test_the_agent_renders_the_same_columns_as_the_platform(
        self, monkeypatch, agent_bound_repository
    ):
        """**两端的列名必须是同一套**：本节点那一行未配置，payload 说 `2|2`。

        只断言「payload 里有两个键」是不够的：Agent 可能收到了却不用（这正是它修前的
        形态 —— 只用自己库里那一行）。所以这里断言的是渲染出来的列名与 diff 引擎在
        `2|2` 下给出的那一份逐字相同。
        """
        payload = self._dispatch(
            agent_bound_repository, path='config/角色属性表_端到端.xlsx',
            header_rows=2, header_name_row=2,
        )
        got = self._render_via_payload(monkeypatch, agent_bound_repository, payload)

        engine_columns = _diff_columns(TABLE, header_rows=2, header_name_row=2)
        assert got['header_config_source'] == 'payload', got
        assert _ai_labels(got['content']) == engine_columns == NAME_ROW, (
            f'两端列名不是同一套（本节点那一行是未配置的，payload 是 2|2）：\n'
            f'— diff/请求方 —\n{engine_columns}\n— Agent —\n{_ai_labels(got["content"])}\n'
            f'{got["content"]}'
        )
        assert _PARTICIPATING.search(got['content']).group(1) == str(len(BODY_ROWS))

    def test_two_header_configs_are_two_dispatches(self, agent_bound_repository):
        """同一份文件、两种表头配置 → **两条任务**（不是复用同一条）。

        复用上了的表现是「Agent 拿按旧配置渲染的正文回答按新配置的请求」：列名与数据行
        坐标都是旧的，而界面上完全看不出来（不派发、不等待、也没有痕迹）。
        """
        from app import app
        from models import AgentTask

        path = 'config/角色属性表_两种配置.xlsx'
        first = self._dispatch(
            agent_bound_repository, path=path, header_rows=None, header_name_row=None
        )
        second = self._dispatch(
            agent_bound_repository, path=path, header_rows=2, header_name_row=2
        )
        with app.app_context():
            # 只看这个路径的任务：同一个仓库上还有其它用例派出去的任务，
            # 拿全量 count() 去比会「全量绿、子集红」（测试库是会话级共用的）。
            count = sum(
                1
                for row in AgentTask.query.filter_by(
                    task_type='file_content',
                    repository_id=agent_bound_repository.repository_id,
                ).all()
                if json.loads(row.payload).get('file_path') == path
            )

        assert first['header_rows'] is None and second['header_rows'] == 2
        assert count == 2, (
            f'两种表头配置共用了一条任务 —— 第二个请求会拿到按 1|1 渲染的正文：{count}'
        )
