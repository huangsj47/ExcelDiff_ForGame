# -*- coding: utf-8 -*-
"""AI 读代码正文的第二条来源：Agent（`file_content` 在 platform/agent 模式下的通路）。

## 为什么需要这条通路

配表的正文平台拿得到（`DiffCache` / `ExcelDiffCacheService` 都是配表专用），代码文件的
正文平台**没有**：platform/agent 模式下 `get_file_content_from_git` 直接返回 None
（日志里那句「请由 Agent 节点提供数据」），因为只有业务节点才有工作副本、也才连得通
代码服务器。于是 `file_content` 在那种部署里恒为「拿不到」—— 症状不是报错，而是
AI 报告里永远有一条读不到正文的信息缺口。

通路：平台派 `AgentTask(file_content)` → Agent 在自己的工作副本上用 git 读（开源工具，
无新依赖）→ 回传落库 → 平台读到。

## 本文件断言三层

1. **Agent 端怎么读**（`agent_file_content_reader`）：按窗口切，取不到就抛（不能
   返回空正文 —— 那与「这个文件是空的」分不开）；
2. **平台端怎么要**（`agent_file_content_dispatch`）：有界等待、离线不干等、同参数
   只问一次、失败有上限地重试；
3. **provider 怎么用**（`PlatformContextProvider.file_content`）：本地读得到就用本地，
   读不到才找 Agent，两者都不行时给出**明确否掉「没有内容/没有改动」**的说明；
   以及**默认窗口落在本次改动附近**（而不是文件开头）—— 这是「AI 能不能看到它需要
   的内容」的关键一环，挑窗口的规则在本文件最后两组用例里。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.ai.platform_provider import PlatformContextProvider  # noqa: E402
from services.ai.skill_loader import LoadedSkills, SkillDocument  # noqa: E402
from utils.content_window import DEFAULT_WINDOW_LINES  # noqa: E402

COMMIT = 'a' * 40


@pytest.fixture(scope='module')
def repository_id():
    """一个真实的仓库行（按 id 查库的那几处要用）。

    * 项目 code 必须唯一：测试库是会话级共用的，固定值第二次就撞唯一约束
      （实测报 `UNIQUE constraint failed: project.code`）；
    * 作用域是 module：`create_tables()` 每次都会把整张表清单打进日志，
      一用例一次会把输出淹掉。
    """
    from app import app, create_tables, db
    from models import Project, Repository

    token = uuid.uuid4().hex[:10]
    with app.app_context():
        create_tables()
        project = Project(code=f'FC{token}', name=f'agent-file-content-{token}')
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id, name=f'repo-fc-{token}', type='git',
            url='https://example.invalid/game.git', branch='main', clone_status='completed',
        )
        db.session.add(repository)
        db.session.commit()
        yield repository.id


# ---------------------------------------------------------------------------
# Agent 端：从工作副本里读
# ---------------------------------------------------------------------------


class TestAgentSideReader:
    """`services/agent_file_content_reader.read_file_content_for_agent`：Agent 读到的是什么。"""

    def _read(self, monkeypatch, repository_id, payload, content):
        from app import app

        with app.app_context():
            from services.agent_file_content_reader import read_file_content_for_agent

            monkeypatch.setattr(
                'services.vcs_content_service.get_file_content_from_git',
                lambda repository, commit_id, path: content,
            )
            return read_file_content_for_agent(
                {'repository_id': repository_id, 'commit_id': COMMIT, 'file_path': 'src/fight.lua', **payload}
            )

    def test_the_requested_window_is_what_comes_back(self, monkeypatch, repository_id):
        text = '\n'.join(f'line {i}' for i in range(1, 3001))
        got = self._read(
            monkeypatch, repository_id, {'lines': '1180-1260', 'max_chars': 60_000}, text.encode('utf-8')
        )
        assert got['start_line'] == 1180 and got['end_line'] == 1260
        assert got['total_lines'] == 3000, '整份文件多少行必须带回去（模型据此判断手上是不是片段）'
        assert got['content'].split('\n')[0] == 'line 1180'
        assert got['truncated'] is False

    def test_without_a_window_the_first_segment_comes_back(self, monkeypatch, repository_id):
        text = '\n'.join(f'line {i}' for i in range(1, 3001))
        got = self._read(monkeypatch, repository_id, {}, text.encode('utf-8'))
        assert (got['start_line'], got['end_line']) == (1, DEFAULT_WINDOW_LINES)
        assert got['total_lines'] == 3000
        assert len(got['content'].split('\n')) == DEFAULT_WINDOW_LINES

    def test_the_char_limit_says_it_was_cut(self, monkeypatch, repository_id):
        text = '\n'.join(f'line {i}' for i in range(1, 3001))
        got = self._read(monkeypatch, repository_id, {'max_chars': 200}, text.encode('utf-8'))
        assert got['truncated'] is True
        assert got['end_line'] < DEFAULT_WINDOW_LINES
        assert len(got['content']) <= 200

    def test_non_utf8_content_is_still_read(self, monkeypatch, repository_id):
        """GBK 的 lua 是常见的：读得到的内容不能因为解码失败就说成读不了。"""
        text = '\n'.join(f'-- 注释 {i}' for i in range(1, 101))
        got = self._read(monkeypatch, repository_id, {}, text.encode('gbk'))
        assert got['total_lines'] == 100, f'解码失败不该丢掉内容：{got}'

    def test_an_unreadable_path_raises_instead_of_returning_empty(self, monkeypatch, repository_id):
        """取不到必须抛 —— 返回空正文与「这个文件是空的」分不开。"""
        with pytest.raises(Exception) as excinfo:
            self._read(monkeypatch, repository_id, {}, None)
        assert '读取文件内容失败' in str(excinfo.value)

    def test_the_payload_is_validated(self, monkeypatch, repository_id):
        from app import app

        with app.app_context():
            from services.agent_file_content_reader import read_file_content_for_agent

            for payload in [
                {'commit_id': COMMIT, 'file_path': 'a.lua'},
                {'repository_id': repository_id, 'file_path': 'a.lua'},
                {'repository_id': repository_id, 'commit_id': COMMIT},
            ]:
                with pytest.raises(Exception) as excinfo:
                    read_file_content_for_agent(payload)
                assert 'file_content 任务缺少' in str(excinfo.value)

    def test_a_missing_repository_row_says_which_id(self):
        """节点上的库里没有这个仓库时：抛出**带 id 的原因**，而不是空正文。

        这是这套部署最常见的配置错（节点用了另一个数据库 / 还没同步过这个仓库），
        所以失败信息里必须能直接看出是哪个 id —— 否则只能看到一句「取数失败」。
        """
        from app import app

        with app.app_context():
            from services.agent_file_content_reader import read_file_content_for_agent

            with pytest.raises(Exception) as excinfo:
                read_file_content_for_agent(
                    {'repository_id': 999999999, 'commit_id': COMMIT, 'file_path': 'a.lua'}
                )
            assert '999999999' in str(excinfo.value), str(excinfo.value)


# ---------------------------------------------------------------------------
# 平台端：向 Agent 要（用替身，不碰网络）
# ---------------------------------------------------------------------------


class _Column:
    """`AgentTask.id` 的替身：被测代码会写 `order_by(AgentTask.id.desc())`。"""

    def desc(self):
        return self


class _FakeTask:
    def __init__(self, **kwargs):
        self.id = kwargs.pop('id', 1)
        self.task_type = kwargs.pop('task_type', 'file_content')
        self.project_id = kwargs.pop('project_id', None)
        self.repository_id = kwargs.pop('repository_id', None)
        self.status = kwargs.pop('status', 'pending')
        self.payload = json.dumps(kwargs.pop('payload', {}), ensure_ascii=False)
        self.result_summary = kwargs.pop('result_summary', None)
        self.error_message = kwargs.pop('error_message', None)
        if self.result_summary is not None and not isinstance(self.result_summary, str):
            self.result_summary = json.dumps(self.result_summary, ensure_ascii=False)


class _FakeTaskQuery:
    """只实现被测代码用到的查询形状（`filter_by().order_by().limit().all()`）。

    `filter_by` 会把条件累积在**新建的实例**上（见 `_FakeAgentTask.query` 是属性）——
    真 SQLAlchemy 每次链式调用都得到一个新的 Query，共用实例会让「上一次查询的条件」
    悄悄带到下一次，测出来的行为就与线上不一样了。
    """

    def __init__(self, store):
        self._store = store
        self._filters = {}
        self._limit = None

    def filter_by(self, **kwargs):
        self._filters.update(kwargs)
        return self

    def order_by(self, *_args):
        return self

    def limit(self, count):
        self._limit = count
        return self

    def all(self):
        rows = [
            row
            for row in self._store.rows
            if all(getattr(row, key, None) == value for key, value in self._filters.items())
        ]
        rows.sort(key=lambda row: row.id, reverse=True)
        return rows[: self._limit] if self._limit else rows


class _FakeAgentTask:
    """`AgentTask` 的替身：`id` 要能 `.desc()`（被测代码写 `order_by(AgentTask.id.desc())`）。"""

    def __init__(self, store):
        self.id = _Column()
        self._store = store

    @property
    def query(self):
        return _FakeTaskQuery(self._store)


class _Store:
    """AgentTask 的极简替身：够被测代码用，且能模拟「Agent 干完了」。"""

    def __init__(self):
        self.rows = []
        self._next_id = 1
        self.on_refresh = None

    def add(self, **kwargs):
        task = _FakeTask(id=self._next_id, **kwargs)
        self._next_id += 1
        self.rows.append(task)
        return task


@pytest.fixture()
def dispatch(monkeypatch):
    """把 dispatch 模块的模型与派发入口换成替身，返回一个可操作的装置。

    时钟也换掉：`request_file_content` 的等待上限靠 `time.monotonic()` 判，真钟 + 空转的
    sleep 桩会让上限看起来「没生效」（实测空转了几百万轮）。换成受控时钟后，
    「等了多久」是确定的，断言才有意义。
    """
    import services.agent_file_content_dispatch as module

    store = _Store()
    counts = {'enqueue': 0, 'sleep': 0, 'refresh': 0}
    clock = {'now': 1000.0}
    agent = SimpleNamespace(
        id=7, status='online', last_heartbeat=datetime.now(timezone.utc)
    )

    class _Session:
        def get(self, model, ident):
            return agent

        def commit(self):
            return None

        def rollback(self):
            return None

        def expire(self, task):
            return None

        def refresh(self, task):
            counts['refresh'] += 1
            if store.on_refresh is not None:
                store.on_refresh(task)

    monkeypatch.setattr(module, 'AgentTask', _FakeAgentTask(store))
    monkeypatch.setattr(module, 'db', SimpleNamespace(session=_Session()))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: clock['now']))
    monkeypatch.setattr(
        module, 'AgentProjectBinding',
        SimpleNamespace(query=SimpleNamespace(filter_by=lambda **k: SimpleNamespace(first=lambda: SimpleNamespace(agent_id=7)))),
    )

    def _enqueue(**kwargs):
        counts['enqueue'] += 1
        return store.add(status='pending', **kwargs)

    def _enqueue_once(**kwargs):
        """`enqueue_agent_task_once` 的替身：先去重判定，再落一条。

        真入口（`services/agent_task_enqueue_service.py`）的契约是「判定 + 插入 + 提交
        在锁里一气做完」，返回 `(task, created)`。这里只需要**同样的可观察行为**：
        判定为「已有活跃任务」时复用那一条。锁与提交由那条入口自己的用例
        （`tests/test_agent_task_enqueue_dedup.py`）盯着，不在这一组的范围里。
        """
        counts['enqueue'] += 1
        find_existing = kwargs.pop('find_existing', None)
        existing = find_existing() if callable(find_existing) else None
        if existing is not None:
            return existing, False
        return store.add(status='pending', **kwargs), True

    monkeypatch.setattr(module, 'enqueue_agent_task_once', _enqueue_once)

    def _sleep(seconds):
        counts['sleep'] += 1
        clock['now'] += float(seconds)

    return SimpleNamespace(
        module=module, store=store, counts=counts, agent=agent, sleep=_sleep, clock=clock,
        repository=SimpleNamespace(id=11, project_id=3),
    )


class TestPlatformSideDispatch:
    """`services/agent_file_content_dispatch.py`：派发、等待、复用、失败都要如实。"""

    def _request(self, dispatch, **kwargs):
        kwargs.setdefault('repository', dispatch.repository)
        kwargs.setdefault('commit_id', COMMIT)
        kwargs.setdefault('file_path', 'src/fight.lua')
        kwargs.setdefault('sleep_func', dispatch.sleep)
        return dispatch.module.request_file_content(**kwargs)

    def test_a_missing_project_is_unavailable_with_a_reason(self, dispatch):
        got = self._request(dispatch, repository=SimpleNamespace(id=11, project_id=None))
        assert got['status'] == 'unavailable'
        assert '缺少项目信息' in got['message']
        assert dispatch.counts['enqueue'] == 0, '连项目都没有就别派任务了'

    def test_without_a_bound_agent_it_says_so(self, dispatch, monkeypatch):
        monkeypatch.setattr(
            dispatch.module, 'AgentProjectBinding',
            SimpleNamespace(query=SimpleNamespace(filter_by=lambda **k: SimpleNamespace(first=lambda: None))),
        )
        got = self._request(dispatch)
        assert got['status'] == 'unavailable', (
            '「项目没绑 Agent」不是「还在路上」，必须分开 —— 否则每次分析都白等一轮'
        )
        assert '没有绑定 Agent' in got['message']
        assert dispatch.counts['enqueue'] == 0

    def test_an_offline_agent_queues_without_waiting(self, dispatch):
        dispatch.agent.status = 'offline'
        got = self._request(dispatch)
        assert got['status'] == 'pending'
        assert dispatch.counts['enqueue'] == 1, '离线也要把任务排上（它上线后会跑）'
        assert dispatch.counts['sleep'] == 0, '离线时干等一轮上限等于把每次分析都拖慢'

    def test_a_stale_heartbeat_counts_as_offline(self, dispatch):
        dispatch.agent.status = 'online'
        dispatch.agent.last_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=30)
        got = self._request(dispatch)
        assert got['status'] == 'pending'
        assert dispatch.counts['sleep'] == 0

    def test_the_body_comes_back_within_the_wait(self, dispatch):
        body = {'content': 'line 1\nline 2', 'start_line': 1, 'end_line': 2, 'total_lines': 2}
        dispatch.store.on_refresh = lambda task: setattr(task, 'status', 'completed') or setattr(
            task, 'result_summary', json.dumps(body)
        )
        got = self._request(dispatch)
        assert got['status'] == 'ready'
        assert got['content'] == 'line 1\nline 2'
        assert got['total_lines'] == 2

    def test_the_wait_is_bounded_when_the_agent_never_answers(self, dispatch):
        got = self._request(dispatch, wait_seconds=1.0)
        assert got['status'] == 'pending', '等不到不是失败：任务还在队列里，下次直接命中'
        assert '没等到' in got['message']
        waited = dispatch.clock['now'] - 1000.0
        assert waited <= 1.0 + dispatch.module.FILE_CONTENT_POLL_SECONDS, (
            f'等了 {waited:.2f}s，超过上限了 —— 一次分析里二十次取数就是几十分钟的干等'
        )
        assert dispatch.counts['sleep'] >= 1, '在线时应该真的等一等（几秒通常就回来了）'

    def test_a_failed_task_reports_the_agents_own_words(self, dispatch):
        dispatch.store.on_refresh = lambda task: setattr(task, 'status', 'failed') or setattr(
            task, 'error_message', '读取文件内容失败（工作副本里没有这个路径）'
        )
        got = self._request(dispatch)
        assert got['status'] == 'unavailable'
        assert '工作副本里没有这个路径' in got['message'], '失败原因要透传，不能只说一句「取数失败」'

    def test_an_already_fetched_body_is_reused(self, dispatch):
        dispatch.store.add(
            task_type='file_content', project_id=3, repository_id=11, status='completed',
            payload={'commit_id': COMMIT, 'file_path': 'src/fight.lua', 'lines': ''},
            result_summary={'content': 'cached', 'start_line': 1, 'end_line': 1, 'total_lines': 1},
        )
        got = self._request(dispatch)
        assert got['status'] == 'ready' and got['content'] == 'cached'
        assert dispatch.counts['enqueue'] == 0, '同一个文件问两次不该派两次任务'
        assert dispatch.counts['sleep'] == 0

    def test_another_commit_or_path_is_not_reused(self, dispatch):
        dispatch.store.add(
            task_type='file_content', project_id=3, repository_id=11, status='completed',
            payload={'commit_id': 'b' * 40, 'file_path': 'src/fight.lua', 'lines': ''},
            result_summary={'content': 'wrong commit', 'start_line': 1, 'end_line': 1, 'total_lines': 1},
        )
        dispatch.store.on_refresh = lambda task: setattr(task, 'status', 'completed') or setattr(
            task, 'result_summary', '{"content": "fresh", "start_line": 1, "end_line": 1, "total_lines": 1}'
        )
        got = self._request(dispatch)
        assert got['content'] == 'fresh', '缓存必须按 (commit, path, lines) 精确匹配，不能张冠李戴'

    def test_a_different_window_is_a_different_request(self, dispatch):
        dispatch.store.add(
            task_type='file_content', project_id=3, repository_id=11, status='completed',
            payload={'commit_id': COMMIT, 'file_path': 'src/fight.lua', 'lines': ''},
            result_summary={'content': 'the default window', 'start_line': 1, 'end_line': 1, 'total_lines': 1},
        )
        dispatch.store.on_refresh = lambda task: setattr(task, 'status', 'completed') or setattr(
            task, 'result_summary', '{"content": "the named window", "start_line": 1, "end_line": 1, "total_lines": 1}'
        )
        got = self._request(dispatch, lines='1180-1260')
        assert got['content'] == 'the named window', (
            '模型点名要另一段时不能把默认那一段又给它一次'
        )

    def test_a_dispatch_failure_is_reported_not_raised(self, dispatch, monkeypatch):
        def _boom(**_kwargs):
            raise RuntimeError('数据库写不进去')

        monkeypatch.setattr(dispatch.module, 'enqueue_agent_task_once', _boom)
        got = self._request(dispatch)
        assert got['status'] == 'unavailable'
        assert '数据库写不进去' in got['message'], '派发失败的原因要能被看见'

    def _failed_once(self, dispatch, *, times=1):
        """造出「同参数已经失败过 N 次」的现场。"""
        for _ in range(times):
            dispatch.store.add(
                task_type='file_content', project_id=3, repository_id=11, status='failed',
                payload={'commit_id': COMMIT, 'file_path': 'src/fight.lua', 'lines': ''},
                error_message='读取文件内容失败（工作副本里没有这个路径）',
            )

    def test_an_old_failure_is_retried_not_obeyed(self, dispatch):
        """失败过一次之后**还会再派**：失败往往是配置问题，而配置会被修好。

        只认那条失败记录的话，「工作副本补上了 / 节点换成了正确的数据库」之后，
        报告里会一直挂着同一句读不到 —— 而且没有任何人会去查。
        """
        self._failed_once(dispatch)
        dispatch.agent.status = 'offline'  # 只为让这次断言不依赖等待
        got = self._request(dispatch)
        assert got['status'] == 'pending'
        assert dispatch.counts['enqueue'] == 1, '旧的失败记录不该把这条路堵死'

    def test_retrying_stops_after_the_attempt_cap(self, dispatch):
        """但也**有上限**：真取不到时不能每次分析都白等一轮上限（15 秒）。"""
        limit = dispatch.module.FILE_CONTENT_MAX_ATTEMPTS
        self._failed_once(dispatch, times=limit)
        dispatch.agent.status = 'offline'
        got = self._request(dispatch)
        assert got['status'] == 'unavailable', f'连续失败到 {limit} 次就该停手：{got}'
        assert '不再自动重试' in got['message']
        assert '工作副本里没有这个路径' in got['message'], '停手时也要说清最后那次失败的原因'
        assert dispatch.counts['enqueue'] == 0

    def test_a_completed_task_after_a_failure_still_wins(self, dispatch):
        """失败之后成功取回来的那一份必须能用（新的在前，不能被旧失败挡住）。"""
        self._failed_once(dispatch)
        dispatch.store.add(
            task_type='file_content', project_id=3, repository_id=11, status='completed',
            payload={'commit_id': COMMIT, 'file_path': 'src/fight.lua', 'lines': ''},
            result_summary={'content': 'finally', 'start_line': 1, 'end_line': 1, 'total_lines': 1},
        )
        got = self._request(dispatch)
        assert got['status'] == 'ready' and got['content'] == 'finally'
        assert dispatch.counts['enqueue'] == 0

    def test_the_payload_carries_what_the_agent_needs(self, dispatch, monkeypatch):
        captured = {}

        def _enqueue(**kwargs):
            kwargs.pop('find_existing', None)
            captured.update(kwargs)
            return dispatch.store.add(status='pending', **kwargs), True

        monkeypatch.setattr(dispatch.module, 'enqueue_agent_task_once', _enqueue)
        dispatch.agent.status = 'offline'
        self._request(dispatch, lines='1180-1260', max_chars=1234)
        payload = captured['payload']
        assert payload['commit_id'] == COMMIT
        assert payload['file_path'] == 'src/fight.lua'
        assert payload['lines'] == '1180-1260', '模型点的那一段要原样带到 Agent 那边'
        assert payload['max_chars'] == 1234
        assert captured['task_type'] == 'file_content'
        assert captured['repository_id'] == 11


# ---------------------------------------------------------------------------
# Provider：两条来源的优先级与「读不到」的说法
# ---------------------------------------------------------------------------


@pytest.fixture()
def provider():
    doc = SkillDocument(
        name='SKILL.md', description='', path=Path('SKILL.md'), text='平台协议', content_hash='h',
    )
    return PlatformContextProvider(
        loaded=LoadedSkills(
            platform_skill=doc, platform_references=(), project_manifest=None,
            project_references=(), project_skills=(), readable={}, project_slug=None, revision='rev',
        )
    )


@pytest.fixture()
def platform_mode(monkeypatch):
    """平台本地读不到正文（platform/agent 模式的常态）+ `_commit_row` 可用。"""
    monkeypatch.setattr(
        PlatformContextProvider, '_commit_row',
        lambda self, commit, path: SimpleNamespace(
            commit_id=commit, path=path, repository=SimpleNamespace(id=11, project_id=3)
        ),
    )
    import services.vcs_content_service as vcs

    monkeypatch.setattr(vcs, 'get_file_content_from_git', lambda *a, **k: None)


class TestProviderLayering:
    @pytest.fixture(autouse=True)
    def _no_window_picking(self, monkeypatch):
        """本类测的是「正文从哪条来源来、读不到怎么说」——窗口挑选另有专门用例。

        默认窗口要读一次 diff 才能定位改动（`_default_window`），桩掉它这些用例才只
        依赖它们自己声明的输入。
        """
        monkeypatch.setattr(PlatformContextProvider, '_default_window', lambda self, c, p: '')

    def test_the_local_work_copy_wins(self, provider, monkeypatch):
        """单机模式下平台自己读得到，就不该跨节点去要一份。"""
        import services.agent_file_content_dispatch as dispatch_module
        import services.vcs_content_service as vcs

        monkeypatch.setattr(
            PlatformContextProvider, '_commit_row',
            lambda self, commit, path: SimpleNamespace(
                commit_id=commit, path=path, repository=SimpleNamespace(id=11, project_id=3)
            ),
        )
        monkeypatch.setattr(vcs, 'get_file_content_from_git', lambda *a, **k: 'local line\n')
        called = {'n': 0}
        monkeypatch.setattr(
            dispatch_module, 'request_file_content',
            lambda *a, **k: called.__setitem__('n', called['n'] + 1) or {},
        )
        got = provider.file_content(COMMIT, 'src/fight.lua')
        assert 'local line' in got
        assert called['n'] == 0, '本地读得到还去派 Agent 任务是白跑一趟'

    def test_the_agent_body_is_rendered_with_line_numbers(self, provider, platform_mode, monkeypatch):
        import services.agent_file_content_dispatch as dispatch_module

        monkeypatch.setattr(
            dispatch_module, 'request_file_content',
            lambda *a, **k: {
                'status': 'ready', 'content': 'local a = 1\nreturn a',
                'start_line': 1180, 'end_line': 1181, 'total_lines': 2000,
            },
        )
        got = provider.file_content(COMMIT, 'src/fight.lua')
        assert '1180│local a = 1' in got, f'行号是模型写证据的坐标，必须给：{got!r}'
        assert '1181│return a' in got
        assert '共 2000 行' in got, '要告诉模型这是一段而不是全文'
        assert 'Agent' in got, '要说明内容是从业务节点取回来的（决定它该怎么复核）'

    def test_a_body_still_on_its_way_is_reported_honestly(self, provider, platform_mode, monkeypatch):
        import services.agent_file_content_dispatch as dispatch_module

        calls = {'n': 0}

        def _pending(*_a, **_k):
            calls['n'] += 1
            return {'status': 'pending', 'message': 'Agent 当前离线，取数任务已排队'}

        monkeypatch.setattr(dispatch_module, 'request_file_content', _pending)
        first = provider.file_content(COMMIT, 'src/fight.lua')
        assert '还没取回来' in first and 'Agent 当前离线' in first
        assert '不等于「没有内容」' in first, (
            '这句话是本模块存在的理由：读不到全都会被读成「没改动」'
        )
        second = provider.file_content(COMMIT, 'src/fight.lua')
        assert second == first
        assert calls['n'] == 1, '同一个文件问两次不该等两次（每次都是一轮上限）'

    def test_an_unavailable_body_says_it_is_not_no_change(self, provider, platform_mode, monkeypatch):
        import services.agent_file_content_dispatch as dispatch_module

        monkeypatch.setattr(
            dispatch_module, 'request_file_content',
            lambda *a, **k: {'status': 'unavailable', 'message': '项目没有绑定 Agent 节点'},
        )
        got = provider.file_content(COMMIT, 'src/fight.lua')
        assert '项目没有绑定 Agent 节点' in got
        assert '不等于「没有内容」' in got and '不等于「没有改动」' in got
        assert '信息缺口' in got, '要让模型知道该把它写成信息缺口，而不是推断出一个结论'

    def test_a_dispatch_crash_only_degrades_this_one_fetch(self, provider, platform_mode, monkeypatch):
        import services.agent_file_content_dispatch as dispatch_module

        def _boom(*_a, **_k):
            raise RuntimeError('Agent 接口 500')

        monkeypatch.setattr(dispatch_module, 'request_file_content', _boom)
        got = provider.file_content(COMMIT, 'src/fight.lua')
        assert isinstance(got, str) and got != ''
        assert 'Agent 接口 500' in got, '异常原因不能被吞掉'

    def test_the_window_the_model_asked_for_is_passed_through(self, provider, platform_mode, monkeypatch):
        import services.agent_file_content_dispatch as dispatch_module

        seen = {}

        def _capture(repository, **kwargs):
            seen['repository'] = repository
            seen.update(kwargs)
            return {'status': 'unavailable', 'message': 'x'}

        monkeypatch.setattr(dispatch_module, 'request_file_content', _capture)
        provider.file_content(COMMIT, 'src/fight.lua', '1180-1260')
        assert seen['lines'] == '1180-1260'
        assert seen['commit_id'] == COMMIT and seen['file_path'] == 'src/fight.lua'
        assert seen['repository'].project_id == 3, '要按项目找绑定的 Agent 节点'


# ---------------------------------------------------------------------------
# 默认窗口落在哪：按改动位置挑，而不是文件开头
# ---------------------------------------------------------------------------


def _patch(*hunks):
    """按 `(新版本起始行, 行数)` 造一段渲染好的补丁（块头是唯一要解析的东西）。"""
    return "\n".join(
        f"@@ -{start},{count} +{start},{count} @@ def f():\n-    old\n+    new" for start, count in hunks
    )


class TestWindowPlacingMath:
    """挑窗口的纯函数（不碰库、不碰文件）。"""

    def test_hunk_headers_are_parsed_from_the_new_side(self):
        from services.ai.platform_provider import _changed_line_starts

        starts = _changed_line_starts(_patch((1180, 7), (2205, 3)))
        assert starts == [1180, 2205], (
            '块头里有两个行号，要的是**新版本**那一个 —— file_content 给的正是当前版本的正文'
        )

    def test_a_hunk_without_a_count_is_parsed(self):
        from services.ai.platform_provider import _changed_line_starts

        assert _changed_line_starts('@@ -1180 +1180 @@') == [1180]

    def test_a_diff_without_hunks_yields_nothing(self):
        from services.ai.platform_provider import _changed_line_starts

        assert _changed_line_starts('[文本] x.lua：本次没有可展示的差异。') == []
        assert _changed_line_starts('') == []

    def test_the_change_is_inside_the_window_with_room_before_it(self):
        from services.ai.platform_provider import _window_around_changes

        window = _window_around_changes([1180], span=400)
        start, end = (int(part) for part in window.split('-'))
        assert start < 1180 and end >= 1180, f'改动必须落在窗口里：{window}'
        assert end - start + 1 == 400
        assert start == 1080, '改动之前留四分之一屏（函数签名通常就在那几行）'

    def test_a_change_near_the_top_does_not_go_before_line_one(self):
        from services.ai.platform_provider import _window_around_changes

        assert _window_around_changes([5], span=400) == '1-400'

    def test_two_far_apart_regions_give_a_single_window_not_a_merged_one(self):
        """窗口是一段连续区间：两处改动隔得远时只能覆盖第一处，不能「合并」成一大段。"""
        from services.ai.platform_provider import _window_around_changes

        window = _window_around_changes([1180, 2200], span=400)
        start, end = (int(part) for part in window.split('-'))
        assert start <= 1180 <= end
        assert end - start + 1 == 400
        assert end < 2200, f'不该为了覆盖第二处而给出一大段：{window}'


class TestDefaultWindowFollowsTheChange:
    """`file_content` 没点名窗口时给哪一段（这是「AI 能不能看到它需要的内容」的关键）。"""

    @pytest.fixture()
    def make(self, provider, monkeypatch):
        """装一台「正文读得到、diff 是给定的补丁」的 provider。"""
        import services.vcs_content_service as vcs

        monkeypatch.setattr(
            PlatformContextProvider, '_commit_row',
            lambda self, commit, path: SimpleNamespace(
                commit_id=commit, path=path, repository=SimpleNamespace(id=11, project_id=3)
            ),
        )

        def _install(content, patch_text, *, platform_mode=False):
            monkeypatch.setattr(
                vcs, 'get_file_content_from_git', lambda *a, **k: None if platform_mode else content
            )
            monkeypatch.setattr(
                PlatformContextProvider,
                'file_diff',
                # `**kwargs`：挑窗口那条路会带 `ask_agent=False`（它不该为了挑坐标去等
                # 业务节点，见 `_default_window`）。
                lambda self, commit, path, **kwargs: patch_text,
            )
            return provider

        return _install

    def _text(self, lines=3000):
        return '\n'.join(f'line {i}' for i in range(1, lines + 1))

    def test_the_window_lands_on_the_change_not_on_the_file_head(self, make):
        got = make(self._text(), _patch((1180, 7))).file_content(COMMIT, 'src/fight.lua')
        assert '\n1180│line 1180\n' in got, f'改动那一行必须在给出去的正文里：{got[:200]!r}'
        assert '\n1080│line 1080\n' in got
        # 锚上换行再判：`1081│line 1081` 里也有子串 「1│line 1」，不锚会永远为真。
        assert '\n1│line 1\n' not in got, '给了文件开头那一段等于没给（改动在第 1180 行）'
        assert '不是全文' in got and '共 3000 行' in got

    def test_the_header_says_the_window_was_picked_by_the_platform(self, make):
        """一段从第 1080 行开始的正文，不说来源就像随机截的。"""
        got = make(self._text(), _patch((1180, 7))).file_content(COMMIT, 'src/fight.lua')
        assert '按本次改动的位置自动选' in got, got[:200]

    def test_a_named_window_still_wins_and_is_not_called_automatic(self, make):
        got = make(self._text(), _patch((1180, 7))).file_content(
            COMMIT, 'src/fight.lua', '2000-2100'
        )
        assert '2000│line 2000' in got and '2100│line 2100' in got
        assert '按本次改动的位置自动选' not in got, '模型自己点的那一段不能说成是平台挑的'

    def test_without_a_usable_patch_it_falls_back_and_says_nothing_extra(self, make):
        """补丁拿不到（认不出/不存在）→ 退回文件开头，且**不能**声称是按改动挑的。"""
        got = make(self._text(500), '').file_content(COMMIT, 'src/fight.lua')
        assert '1│line 1' in got and '共 500 行' in got
        assert '按本次改动的位置自动选' not in got, '没挑到就不能这么说 —— 抬头里每句话都会被当成事实'

    def test_the_agent_path_gets_the_same_window(self, make, monkeypatch):
        """platform/agent 模式下窗口是平台算好、当参数带给 Agent 的（两端必须一致）。"""
        import services.agent_file_content_dispatch as dispatch_module

        seen = {}

        def _capture(_repository, **kwargs):
            seen.update(kwargs)
            return {
                'status': 'ready', 'content': 'line 1180\nline 1181',
                'start_line': 1180, 'end_line': 1181, 'total_lines': 3000,
            }

        monkeypatch.setattr(dispatch_module, 'request_file_content', _capture)
        got = make(None, _patch((1180, 7)), platform_mode=True).file_content(
            COMMIT, 'src/fight.lua'
        )
        assert seen['lines'] == '1080-1479', (
            f'带过去的窗口与平台侧自己切的那一套必须相同：{seen.get("lines")!r}'
        )
        assert '1180│line 1180' in got
        assert '按本次改动的位置自动选' in got


class TestAgentWiring:
    """Agent 侧这条任务真的会被执行到（接线断了的表现是「任务类型不支持」）。

    Agent 的链子是 `agent/executor.execute_task` →（`local_task_types` 里认这个类型）
    → `_execute_task_via_local_runtime` → `services.task_worker_service.execute_task_inline_for_agent`。

    最隐蔽的坏法有两种，各有一条用例：类型不在**本地任务集合**里（配置问题，见
    `agent/config.py` 的必做集合）、以及在 `execute_task_inline_for_agent` 里没有分支
    （那会抛「不支持的任务类型」，平台侧只看到一句笼统的取数失败）。
    """

    def test_the_executor_routes_the_type_to_the_local_runtime(self, monkeypatch):
        import agent.executor as executor

        seen = {}
        monkeypatch.setattr(
            executor, '_execute_task_via_local_runtime',
            lambda task_type, task: seen.update(type=task_type, task=task) or ('completed', {}, None, None),
        )
        settings = SimpleNamespace(local_task_types=['file_content'])
        status, _summary, error, _payload = executor.execute_task(
            {'task_type': 'file_content', 'payload': {'commit_id': COMMIT, 'file_path': 'a.lua'}},
            settings,
        )
        assert seen.get('type') == 'file_content', f'没被路由到本地运行时：{seen}'
        assert status == 'completed' and error is None

    def test_the_type_is_required_even_if_the_env_var_omits_it(self, monkeypatch):
        """**显式配了 `AGENT_LOCAL_TASK_TYPES` 且没写 file_content 的部署也要能跑。**

        少了它的表现不是报错，是「AI 报告里一直有读不到正文的信息缺口」—— 静默降级，
        没人会去查，所以它必须在必做集合里。
        """
        import agent.executor as executor
        from agent.config import load_settings

        monkeypatch.setenv('AGENT_LOCAL_TASK_TYPES', 'auto_sync,commit_diff')
        assert 'file_content' in load_settings().local_task_types, (
            '显式配置把 file_content 过滤掉了 —— 部署方不会知道要加它'
        )

        seen = {}
        monkeypatch.setattr(
            executor, '_execute_task_via_local_runtime',
            lambda task_type, task: seen.update(type=task_type) or ('completed', {}, None, None),
        )
        executor.execute_task(
            {'task_type': 'file_content', 'payload': {}}, SimpleNamespace(local_task_types=['file_content'])
        )
        assert seen.get('type') == 'file_content'

    def test_the_inline_entrypoint_has_a_branch_for_it(self, monkeypatch, repository_id):
        """`execute_task_inline_for_agent` 里必须有这个分支（Agent 走的就是它）。"""
        from app import app

        with app.app_context():
            import services.task_worker_service as worker

            monkeypatch.setattr(
                'services.vcs_content_service.get_file_content_from_git',
                lambda repository, commit_id, path: 'line 1\nline 2\nline 3',
            )
            got = worker.execute_task_inline_for_agent(
                'file_content',
                {'repository_id': repository_id, 'commit_id': COMMIT, 'file_path': 'src/fight.lua'},
            )
        assert got['total_lines'] == 3, got
        assert got['content'].split('\n') == ['line 1', 'line 2', 'line 3']
        assert got['message'].startswith('file_content completed')


class TestEveryProviderStubFollowsTheProtocol:
    """协议加了参数时，**测试里的每一个桩都要跟着改** —— 这是本次踩到的坑。

    `file_content` 新增了可选的行窗口 `lines`。桩少了这个形参不会报错，只会抛 TypeError，
    而取数失败是被接住的（引擎把它记成「这一条没拿到」）：表现不是红，是**用例静默退化**
    ——`tests/test_ai_context_compaction.py` 里四条「历史涨到撑破预算」的用例一起变成了
    「历史根本没涨」，报出来的话是「跑了这么多轮都没压过历史，说明约束没生效」。

    所以这条守卫扫的是**源码里的每一个 `def file_content`**（不是 import 进来的那几个）：
    新写的桩忘了这个形参，这里会直接指名道姓。
    """

    def _stubs_missing_lines(self):
        import ast
        import pathlib

        offenders = []
        seen = 0
        for path in sorted((pathlib.Path(PROJECT_ROOT) / 'tests').glob('test_*.py')):
            # `utf-8-sig`：仓库里有带 BOM 的测试文件，`ast.parse` 见到 U+FEFF 直接抛。
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'file_content':
                    seen += 1
                    names = [arg.arg for arg in node.args.args] + [arg.arg for arg in node.args.kwonlyargs]
                    if 'lines' not in names:
                        offenders.append(f'{path.name}:{node.lineno}')
        return offenders, seen

    def test_no_stub_drops_the_window_argument(self):
        offenders, seen = self._stubs_missing_lines()
        # 扫到 0 个桩说明扫描本身失效了（改名、换目录、读文件失败都会被 except 吞掉），
        # 那种「守卫永远为真」的假绿比漏报更坏。
        assert seen >= 3, f'一个 file_content 桩都没扫到（seen={seen}），这条守卫是空的'
        assert not offenders, (
            '这些桩的 file_content 少了 lines 形参（取数会抛 TypeError，用例会静默退化）：'
            + ', '.join(offenders)
        )

    def test_the_scan_can_actually_find_them(self):
        """反向自检：扫描器认得出桩，而且真的会报出缺参数的写法。"""
        import ast

        source = (
            'class P:\n'
            '    def file_content(self, commit, path):\n'
            '        return ""\n'
            '    def file_content_ok(self, commit, path, lines=""):\n'
            '        return ""\n'
        )
        found = [
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == 'file_content'
        ]
        assert found == [2], '扫描器没认出这个桩（反向自检失败，守卫会永远为真）'
