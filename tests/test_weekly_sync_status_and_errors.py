# -*- coding: utf-8 -*-
"""周版本同步：结局可见性（无数据 / 失败 / 部分失败）与「初始缓存遮罩」。

## 为什么需要这个测试

`process_weekly_version_sync()` 过去在**四条完全不同的路径**上都 `return None`：

    (a) 找不到 config   (b) config 被禁用   (c) 窗口内没有任何提交   (d) 正常跑完

调用方（`services/task_worker_weekly_handlers.py`）拿不到任何区分信息，只能无条件
把 BackgroundTask 标成 `completed`。后果是运维**无法区分「真的失败了」和「这周本来
就没数据」**：前者要排查，后者是常态。同一段代码里，单文件 diff 失败被 `continue`
吞掉之后，循环结束仍然打「周版本同步完成」—— 日志说完成、任务标 completed，而事实
是有一部分文件压根没有缓存。

第三个缺陷在读取侧：`weekly_version_files_api()` 用
`any(cache.last_sync_time is not None ...)` 判断「初始缓存是否就绪」，只要**任意
一个**文件成功就解锁页面。首轮同步是逐个文件写缓存的，于是同步跑到第 1 个文件时
用户就看到了「大部分文件还没缓存」的半成品列表，且没有任何「不完整」提示。

## 这些测试变红意味着什么

- `test_no_commits_*`：「窗口内无数据」又被当成失败（运维白排查）或又被标成
  completed（真失败将无法与之区分）。
- `test_missing_config_*` / `test_inactive_config_*`：配置缺失/禁用又丢掉了中文原因，
  界面上只剩「暂无数据」。
- `test_single_file_failure_*`：单文件失败又被吞掉，任务被标成「全部完成」。
- `test_initial_cache_*` / `test_files_api_*`：「部分文件成功就放行」回归了，
  用户会拿半成品当完整结果确认。
- `test_window_*`：窗口换算被改回「直接比较 config.start_time」，窗口开头 8 小时的
  提交被静默丢弃。
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import services.weekly_version_logic as weekly_logic  # noqa: E402
from services.weekly_version_sync_status import (  # noqa: E402
    WEEKLY_SYNC_COMPLETED,
    WEEKLY_SYNC_FAILED_CONFIG_INACTIVE,
    WEEKLY_SYNC_FAILED_CONFIG_MISSING,
    WEEKLY_SYNC_PARTIAL_FAILED,
    WEEKLY_SYNC_SKIPPED_NO_COMMITS,
    initial_cache_readiness,
    task_status_and_message,
)
from utils.timezone_utils import BEIJING_TZ  # noqa: E402

WIN_START_BJ = datetime(2026, 3, 2, 0, 0, 0)   # 用户在 datetime-local 里填的北京墙钟
WIN_END_BJ = datetime(2026, 3, 9, 0, 0, 0)


# ---------------------------------------------------------------------------
# 桩：SQLAlchemy 列表达式 / 查询链的替身
# ---------------------------------------------------------------------------
class _Field:
    """列表达式的替身：任何比较/排序都返回自身，供 filter()/order_by() 使用。"""

    def __eq__(self, _other):
        return self

    def __ne__(self, _other):
        return self

    def __lt__(self, _other):
        return self

    def __le__(self, _other):
        return self

    def __gt__(self, _other):
        return self

    def __ge__(self, _other):
        return self

    def in_(self, _values):
        return self

    def desc(self):
        return self

    def asc(self):
        return self


class _ListQuery:
    """只支撑 `.filter().order_by().all()` 这一条链。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


class _DistinctPathQuery:
    """支撑 collect_target_file_paths 的 `with_entities().distinct().limit().all()`。"""

    def __init__(self, paths):
        self._paths = list(paths)

    def filter(self, *_args, **_kwargs):
        return self

    def with_entities(self, *_args, **_kwargs):
        return self

    def distinct(self):
        return self

    def limit(self, _n):
        return self

    def all(self):
        return [(path,) for path in self._paths]


class _FakeCommitModel:
    """Commit 模型的替身：列用 _Field，query 链按用例给的形态返回。"""

    def __init__(self, *, rows=(), paths=None):
        self.repository_id = _Field()
        self.commit_time = _Field()
        self.path = _Field()
        self.query = _ListQuery(rows) if paths is None else _DistinctPathQuery(paths)


class _BgQuery:
    """BackgroundTask.query 的替身：按调用顺序依次吐出预设值。"""

    def __init__(self, values):
        self._values = list(values)
        self._idx = 0

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._values)

    def first(self):
        if self._idx >= len(self._values):
            return None
        value = self._values[self._idx]
        self._idx += 1
        return value


def _bg_task(task_id, status, **extra):
    base = dict(id=task_id, status=status, created_at=datetime.now(timezone.utc),
                started_at=None, error_message=None)
    base.update(extra)
    return SimpleNamespace(**base)


class _FakeCacheOpLogger:
    def __init__(self):
        self.calls = []

    def log_cache_operation(self, message, log_type='info', **_kwargs):
        self.calls.append((log_type, message))


def _cache(path, ready=True, cache_status='completed'):
    """WeeklyVersionDiffCache 行的替身：字段与 files API 真正读到的那些对齐。"""
    return SimpleNamespace(
        file_path=path,
        last_sync_time=datetime.now(timezone.utc) if ready else None,
        cache_status=cache_status,
        commit_count=1,
        commit_authors='["alice"]',
        commit_messages='["m1"]',
        commit_times='["2026-03-09T12:00:00"]',
        overall_status='pending',
        status_changed_by='',
        confirmation_status='{"dev": "pending"}',
        merged_diff_data='{"operations": ["M"]}',
    )


# ---------------------------------------------------------------------------
# 一、process_weekly_version_sync 的四种结局
# ---------------------------------------------------------------------------
def _run_sync(monkeypatch, *, config, commits, failing_paths=(), generated=None):
    """用纯桩跑一遍 process_weekly_version_sync，返回 (outcome, 日志列表, 操作日志桩)。"""
    logs = []
    cache_op = _FakeCacheOpLogger()
    generated = generated if generated is not None else []

    monkeypatch.setattr(weekly_logic, 'log_print', lambda message, *a, **k: logs.append(str(message)))
    monkeypatch.setattr(weekly_logic, '_weekly_excel_cache_service', cache_op)
    monkeypatch.setattr(weekly_logic, 'Commit', _FakeCommitModel(rows=commits))
    monkeypatch.setattr(
        weekly_logic, 'db',
        SimpleNamespace(session=SimpleNamespace(
            get=lambda _model, _cid: config,
            rollback=lambda: None,
        )),
    )

    def fake_generate(_config, file_path, _commits):
        generated.append(file_path)
        if file_path in failing_paths:
            raise RuntimeError(f'fake diff failure for {file_path}')

    monkeypatch.setattr(weekly_logic, 'generate_weekly_merged_diff', fake_generate)

    return weekly_logic.process_weekly_version_sync(1), logs, cache_op


def _active_config(**overrides):
    base = dict(
        id=1,
        name='第一周版本',
        is_active=True,
        repository_id=7,
        repository=SimpleNamespace(id=7, name='repo_sync'),
        start_time=WIN_START_BJ,
        end_time=WIN_END_BJ,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _commit(commit_id, path):
    return SimpleNamespace(commit_id=commit_id, path=path, commit_time=WIN_START_BJ,
                           message='m', author='alice')


def test_no_commits_is_skipped_not_completed(monkeypatch):
    """窗口内没有任何提交 → 结局是「跳过」，不是 completed，也不产生失败告警。

    变红意味着：「这周本来就没改动」这种**常态**又被记成了失败（运维白排查），
    或又被记成了 completed（真失败将无法与之区分）。
    """
    outcome, logs, cache_op = _run_sync(monkeypatch, config=_active_config(), commits=[])

    assert outcome.status == WEEKLY_SYNC_SKIPPED_NO_COMMITS
    task_status, message = task_status_and_message(outcome)
    assert task_status == 'skipped', f'无提交的任务状态应为 skipped，实际 {task_status}'
    assert task_status != 'completed'
    assert outcome.is_failure is False, '无数据不是失败，不应计入失败告警'
    assert outcome.failed_files == 0
    assert '无提交' in message

    # 不产生失败告警：操作日志必须是 info，不能是 error，日志里也不能出现失败标记
    assert [log_type for log_type, _ in cache_op.calls] == ['info'], cache_op.calls
    assert not any('❌' in line for line in logs), logs
    # 更不能出现与事实不符的「完成」
    assert not any('周版本同步完成' in line for line in logs), logs


def test_missing_config_reports_chinese_reason_and_counts_as_failure(monkeypatch):
    """配置不存在 → 明确的中文原因，调用方按失败记。

    变红意味着：配置被删/配错时任务又被标成 completed，界面上只留「暂无数据」。
    """
    outcome, _logs, _cache_op = _run_sync(monkeypatch, config=None, commits=[])

    assert outcome.status == WEEKLY_SYNC_FAILED_CONFIG_MISSING
    task_status, message = task_status_and_message(outcome)
    assert task_status == 'failed'
    assert outcome.is_failure is True
    assert '不存在' in message and '1' in message, message


def test_inactive_config_reports_chinese_reason(monkeypatch):
    """配置被禁用 → 同样要有明确原因，并标 failed。

    变红意味着：运维有意停用的配置与「真的坏了」在任务列表里又混为一谈。
    """
    outcome, _logs, _cache_op = _run_sync(
        monkeypatch, config=_active_config(is_active=False), commits=[])

    assert outcome.status == WEEKLY_SYNC_FAILED_CONFIG_INACTIVE
    task_status, message = task_status_and_message(outcome)
    assert task_status == 'failed'
    assert '禁用' in message, message


# ---------------------------------------------------------------------------
# 一之三、界面：配置卡片必须显示「为什么这个周版本没有数据」
# ---------------------------------------------------------------------------
def _render_config_page(monkeypatch, *, task, config_id=901, finished=False):
    from flask import render_template

    import app as app_module

    config = SimpleNamespace(
        id=config_id,
        name='第 X 周版本',
        branch='master',
        status='active',
        start_time=WIN_START_BJ,
        end_time=WIN_END_BJ,
        repository=SimpleNamespace(type='git', name='repo_ui'),
    )
    group = {
        'version_name': '第 X 周版本',
        'start_time': WIN_START_BJ,
        'end_time': WIN_END_BJ,
        'configs': [config],
        'status': 'active',
        'cycle_type': 'weekly',
        'category': 'active',
        'created_at': WIN_START_BJ,
    }
    with app_module.app.app_context():
        with app_module.app.test_request_context('/weekly-version-config/1'):
            return render_template(
                'weekly_version_config.html',
                project=SimpleNamespace(id=1, name='UI 测试项目'),
                repositories=[], configs=[], grouped_versions=[group],
                active_versions=[group], future_versions=[], ended_versions=[],
                sync_task_by_config={str(config_id): task} if task else {},
                sync_finished_by_config={str(config_id)} if finished else set(),
                pagination={'page': 1, 'per_page': 20, 'total': 1, 'total_pages': 1,
                            'has_prev': False, 'has_next': False,
                            'prev_num': None, 'next_num': None},
            )


def test_config_page_shows_sync_failure_reason(monkeypatch):
    """配置卡片要显示任务上的 error_message —— 它就是「为什么没有数据」的答案。

    变红意味着：`error_message` 又没有任何周版本模板读它，用户看到的仍然只有
    「暂无数据」，而真正的原因（无提交 / 被禁用 / 部分文件失败）只存在于日志里。
    """
    task = SimpleNamespace(commit_id='901', status='partial_failed',
                           error_message='周版本同步部分失败：共 3 个文件，失败 1 个')
    html = _render_config_page(monkeypatch, task=task)

    assert '周版本同步部分失败' in html, '配置卡片没有把同步失败原因渲染出来'
    assert 'partial_failed' in html, '状态的 title 也应带上，便于鼠标悬停定位'


def test_config_page_escapes_sync_error_message(monkeypatch):
    """error_message 里可能夹带文件路径等外部输入，必须自动转义，不得 |safe。

    变红意味着：出现了存储型 XSS —— 提交路径可以由仓库内容决定，攻击者能借此
    在别人的浏览器里执行脚本。
    """
    task = SimpleNamespace(commit_id='901', status='failed',
                           error_message='<script>alert(1)</script>config/a.lua')
    html = _render_config_page(monkeypatch, task=task)

    assert '<script>alert(1)</script>' not in html, 'error_message 被当成 HTML 输出了（XSS）'
    assert '&lt;script&gt;' in html, '应当看到被转义后的文本'


def test_config_page_without_sync_task_still_renders(monkeypatch):
    """没有同步任务时页面照常渲染（模板对缺失是容错的）。"""
    html = _render_config_page(monkeypatch, task=None)

    assert '第 X 周版本' in html


def test_latest_weekly_sync_tasks_works_against_the_real_model():
    """用真实 BackgroundTask 模型跑一次「取最近任务」，确保配置页的查询链是可用的。

    变红意味着：配置页会因为这块提示信息直接 500 —— 那是比「看不到原因」更糟的回归。
    """
    import app as app_module
    from models import BackgroundTask
    from services.weekly_version_logic import latest_weekly_sync_tasks

    with app_module.app.app_context():
        result = latest_weekly_sync_tasks(BackgroundTask, [SimpleNamespace(id=99999991)])

    assert isinstance(result, dict)
    assert result == {}


# ---------------------------------------------------------------------------
# 一之二、调用方：worker handler 必须按结局落库，而不是无条件 completed
# ---------------------------------------------------------------------------
class _HandlerError(Exception):
    pass


def _run_handler(sync_impl):
    """跑一遍 handle_weekly_sync_task，返回它写下去的状态序列。

    异常元组用**生产代码里那两个常量**，而不是自定义异常：handler 对「可捕获异常」
    与「必须冒泡的异常」的处理本来就依赖这个元组，换成自定义异常会测出假绿。
    """
    from flask import Flask

    from services.task_worker_service import (
        NON_CRITICAL_TASK_EXECUTION_ERRORS,
        NON_CRITICAL_TASK_STATUS_ERRORS,
    )
    from services.task_worker_weekly_handlers import handle_weekly_sync_task

    updates = []

    def fake_update(task_id, status, message=None):
        updates.append((task_id, status, message))

    app = Flask(__name__)
    handle_weekly_sync_task(
        task={'config_id': 5, 'task_id': 77},
        app=app,
        update_task_status_with_retry=fake_update,
        process_weekly_version_sync=sync_impl,
        non_critical_task_status_errors=NON_CRITICAL_TASK_STATUS_ERRORS,
        non_critical_task_execution_errors=NON_CRITICAL_TASK_EXECUTION_ERRORS,
        log_print=lambda *_a, **_k: None,
    )
    return updates


def test_worker_handler_marks_no_commit_sync_as_skipped_not_completed():
    """无提交时调用方必须落 skipped —— 变红说明调用方又无条件写 completed。

    这正是「真正的失败和本来就没数据长得一模一样」的源头：任务状态由调用方写，
    生产代码返回什么结局都没用，只要调用方不认。
    """
    from services.weekly_version_sync_status import no_commits_outcome

    updates = _run_handler(lambda _cid: no_commits_outcome(_active_config()))

    assert updates[0] == (77, 'processing', None), updates
    assert updates[1][1] == 'skipped', updates
    assert updates[1][1] != 'completed'
    assert '无提交' in (updates[1][2] or ''), updates


def test_worker_handler_marks_partial_failure_with_reason():
    """有文件失败时调用方必须落 partial_failed 并带上失败明细，不能写 completed。"""
    from services.weekly_version_sync_status import build_partial_failure_outcome

    outcome = build_partial_failure_outcome(
        4, [('config/b.lua', 'boom'), ('config/c.lua', 'boom2')])

    updates = _run_handler(lambda _cid: outcome)

    assert updates[1][1] == 'partial_failed', updates
    assert 'config/b.lua' in (updates[1][2] or ''), updates


def test_worker_handler_marks_hard_failure_as_failed():
    """抛异常（真失败）时调用方仍要落 failed，并保留原因。"""
    def boom(_cid):
        raise RuntimeError('数据库炸了')

    updates = _run_handler(boom)

    assert updates[1][1] == 'failed', updates
    assert '数据库炸了' in (updates[1][2] or ''), updates


def test_single_file_failure_is_reported_and_task_is_not_completed(monkeypatch):
    """单文件 diff 失败必须累计上报，任务状态不能是「全部完成」，日志不能说「完成」。

    变红意味着：`continue` 吞掉单文件失败的老问题回归 —— 一部分文件没有缓存，
    而任务和日志都宣称同步完成。
    """
    commits = [
        _commit('c1', 'config/a.lua'),
        _commit('c2', 'config/b.lua'),
        _commit('c3', 'config/c.lua'),
    ]
    generated = []
    outcome, logs, cache_op = _run_sync(
        monkeypatch, config=_active_config(), commits=commits,
        failing_paths={'config/b.lua'}, generated=generated,
    )

    assert outcome.status == WEEKLY_SYNC_PARTIAL_FAILED
    task_status, message = task_status_and_message(outcome)
    assert task_status == 'partial_failed'
    assert task_status != 'completed'
    assert outcome.is_failure is True

    # 失败要计数 + 前几个文件名/原因
    assert outcome.failed_files == 1
    assert outcome.total_files == 3
    assert 'config/b.lua' in message and '1 个' in message, message

    # 一个文件失败不能中断其余文件
    assert set(generated) == {'config/a.lua', 'config/b.lua', 'config/c.lua'}

    # 日志里不能出现「完成」这种与事实不符的字样
    assert not any('周版本同步完成' in line for line in logs), logs
    assert any('部分失败' in line for line in logs), logs
    assert [log_type for log_type, _ in cache_op.calls] == ['error'], cache_op.calls


def test_all_files_succeed_is_completed(monkeypatch):
    """全部文件成功才是 completed —— 上面几条的反向保险。"""
    commits = [_commit('c1', 'config/a.lua'), _commit('c2', 'config/b.lua')]
    outcome, logs, cache_op = _run_sync(monkeypatch, config=_active_config(), commits=commits)

    assert outcome.status == WEEKLY_SYNC_COMPLETED
    assert task_status_and_message(outcome)[0] == 'completed'
    assert outcome.failed_files == 0 and outcome.total_files == 2
    assert any('周版本同步完成' in line for line in logs), logs
    assert [log_type for log_type, _ in cache_op.calls] == ['success'], cache_op.calls


# ---------------------------------------------------------------------------
# 二、初始缓存遮罩：全部目标文件都成功才放行
# ---------------------------------------------------------------------------
def test_initial_cache_not_ready_when_only_some_targets_succeeded():
    """目标 3 个文件、只有 1 个成功 → **不放行**（旧实现的 any() 会放行）。

    变红意味着：首轮同步跑到第一个文件时页面就解锁，用户看到「大部分文件还没缓存」
    的半成品列表，而且没有任何「不完整」提示。
    """
    ready, detail = initial_cache_readiness(
        {'config/a.lua', 'config/b.lua', 'config/c.lua'},
        [_cache('config/a.lua')],
    )

    assert ready is False, detail
    assert '目标文件 3 个' in detail and '已完成 1 个' in detail, detail


def test_initial_cache_ready_only_when_every_target_succeeded():
    """全部目标文件都成功 → 放行。"""
    ready, detail = initial_cache_readiness(
        {'config/a.lua', 'config/b.lua'},
        [_cache('config/a.lua'), _cache('config/b.lua')],
    )

    assert ready is True, detail
    assert '目标文件 2 个' in detail and '未完成 0 个' in detail, detail


def test_initial_cache_not_ready_when_a_cache_row_is_incomplete():
    """已有缓存行但 last_sync_time 为空 / cache_status 不是 completed → 不放行。"""
    ready, detail = initial_cache_readiness(
        set(),
        [_cache('config/a.lua'), _cache('config/b.lua', ready=False)],
    )
    assert ready is False, detail

    ready, detail = initial_cache_readiness(
        set(),
        [_cache('config/a.lua'), _cache('config/b.lua', cache_status='failed')],
    )
    assert ready is False, detail


def test_initial_cache_not_ready_when_target_set_is_empty():
    """目标集合为空（窗口内无提交、也没有缓存行）→ 判为未就绪，交给上层触发首轮同步。

    变红意味着：「空就等于就绪」会让新建配置直接显示空列表，且不再触发同步。
    """
    ready, detail = initial_cache_readiness(set(), [])

    assert ready is False
    assert '目标文件集合为空' in detail, detail


# ---------------------------------------------------------------------------
# 三、端到端：files API 的 sync_blocking 由「全部目标文件都成功」决定
# ---------------------------------------------------------------------------
def _call_files_api(monkeypatch, *, config_id, target_paths, caches, sync_tasks):
    from flask import Flask

    config = SimpleNamespace(
        id=config_id,
        project_id=1,
        name='遮罩测试版本',
        is_active=True,
        auto_sync=True,
        created_at=datetime.now(timezone.utc),
        # 遮罩要枚举「窗口内有提交的文件」，所以窗口不能缺
        start_time=WIN_START_BJ,
        end_time=WIN_END_BJ,
        repository=SimpleNamespace(id=7, name='repo_mask', enable_id_confirmation=False),
    )

    monkeypatch.setattr(
        weekly_logic, 'WeeklyVersionConfig',
        SimpleNamespace(query=SimpleNamespace(get_or_404=lambda _cid: config)))
    monkeypatch.setattr(
        weekly_logic, 'WeeklyVersionDiffCache',
        SimpleNamespace(query=SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(all=lambda: list(caches)))))
    monkeypatch.setattr(
        weekly_logic, 'Commit', _FakeCommitModel(paths=target_paths))
    monkeypatch.setattr(
        weekly_logic, 'BackgroundTask',
        SimpleNamespace(
            task_type=_Field(), commit_id=_Field(), status=_Field(), id=_Field(),
            # 依次对应：latest_sync_task / existing_sync_task / completed_sync_task
            query=_BgQuery(sync_tasks),
        ))
    monkeypatch.setattr(weekly_logic, '_create_weekly_sync_task', lambda _cid: 9001)
    monkeypatch.setattr(weekly_logic, '_has_project_access', lambda _pid: True)
    # 遮罩日志的「结论翻转」记忆是模块级的，逐用例屏蔽掉以免互相干扰
    monkeypatch.setattr(weekly_logic, 'should_log_mask_decision', lambda _cid, _ready: False)

    app = Flask(__name__)
    with app.app_context():
        with app.test_request_context(f'/weekly-version-config/{config_id}/files'):
            response = weekly_logic.weekly_version_files_api(config_id)
    if isinstance(response, tuple):  # 异常分支：把原因带进断言消息
        raise AssertionError(f'files API 异常: {response[0].get_json()}')
    return response.get_json()


def test_files_api_blocks_until_every_target_file_is_cached(monkeypatch):
    """3 个目标文件只缓存了 1 个 → 页面必须继续阻塞（旧实现会在这里放行）。

    变红意味着：半成品列表又开始被当成完整结果展示。
    """
    payload = _call_files_api(
        monkeypatch,
        config_id=31,
        target_paths=['config/a.lua', 'config/b.lua', 'config/c.lua'],
        caches=[_cache('config/a.lua')],
        sync_tasks=[None, None, None],
    )

    assert payload['success'] is True
    assert payload['sync_blocking'] is True, payload
    assert payload['total_files'] == 1


def test_files_api_unblocks_when_every_target_file_is_cached(monkeypatch):
    """全部目标文件都缓存完成 → 放行。"""
    payload = _call_files_api(
        monkeypatch,
        config_id=32,
        target_paths=['config/a.lua', 'config/b.lua'],
        caches=[_cache('config/a.lua'), _cache('config/b.lua')],
        sync_tasks=[None, None, None],
    )

    assert payload['success'] is True
    assert payload['sync_blocking'] is False, payload
    assert payload['total_files'] == 2


def test_files_api_keeps_blocking_while_sync_task_is_running(monkeypatch):
    """同步任务还在 processing → 仍然阻塞（顺带覆盖 skipped 之外的终态分支）。"""
    running = _bg_task(9100, 'processing')
    payload = _call_files_api(
        monkeypatch,
        config_id=33,
        target_paths=['config/a.lua', 'config/b.lua'],
        caches=[_cache('config/a.lua')],
        sync_tasks=[running, running, None],
    )

    assert payload['sync_blocking'] is True, payload


def test_files_api_treats_skipped_sync_as_settled(monkeypatch):
    """最近一次同步是 skipped（窗口内无提交）→ 首轮已结束，不再阻塞、也不再派发任务。

    变红意味着：无数据的周版本会被判成「首轮未结束」，每次轮询都重新派发同步任务。
    """
    skipped = _bg_task(9200, 'skipped', error_message='时间窗口内无提交数据，本次同步跳过')
    payload = _call_files_api(
        monkeypatch,
        config_id=34,
        target_paths=['config/a.lua', 'config/b.lua', 'config/c.lua'],
        caches=[_cache('config/a.lua')],
        sync_tasks=[skipped, None, skipped],
    )

    assert payload['success'] is True
    assert payload['sync_blocking'] is False, payload
    assert payload['sync_triggered'] is False, payload


# ---------------------------------------------------------------------------
# 四、窗口换算：北京窗口「起点 + 30 分钟」的提交必须被算进同步范围
# ---------------------------------------------------------------------------
@pytest.fixture()
def seeded_window():
    """建项目/仓库/配置 + 一条「北京窗口起点 + 30 分钟」的提交，用完即删。"""
    import app as app_module
    from models import Commit, Project, Repository, WeeklyVersionConfig

    db = app_module.db
    tag = uuid.uuid4().hex[:8]

    with app_module.app.app_context():
        db.create_all()

        project = Project(code=f'WS{tag}', name='同步结局测试项目', department='QA')
        db.session.add(project)
        db.session.commit()

        repo = Repository(
            project_id=project.id,
            name=f'ws_repo_{tag}',
            type='git',
            url='https://example.invalid/ws.git',
            resource_type='table',
        )
        db.session.add(repo)
        db.session.commit()

        config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repo.id,
            name='同步窗口测试',
            branch='master',
            start_time=WIN_START_BJ,
            end_time=WIN_END_BJ,
            is_active=True,
            auto_sync=False,
            status='active',
        )
        db.session.add(config)
        db.session.commit()

        # 真实北京时间 2026-03-02 00:30 —— 即窗口起点 + 30 分钟。
        # 入库后的 naive-UTC 墙钟是 2026-03-01 16:30，**看起来比窗口起点早 8 小时**，
        # 所以「直接拿 config.start_time 比大小」会把它丢掉。
        aware_bj = datetime(2026, 3, 2, 0, 30, tzinfo=BEIJING_TZ)
        commit_time_utc_naive = aware_bj.astimezone(timezone.utc).replace(tzinfo=None)
        db.session.add(Commit(
            repository_id=repo.id,
            commit_id='early00001',
            path='config/tz.xlsx',
            commit_time=commit_time_utc_naive,
            status='pending',
        ))
        db.session.commit()

        yield SimpleNamespace(
            flask_app=app_module.app, db=db, config_id=config.id, repo_id=repo.id)

        try:
            Commit.query.filter_by(repository_id=repo.id).delete(synchronize_session=False)
            WeeklyVersionConfig.query.filter_by(id=config.id).delete(synchronize_session=False)
            Repository.query.filter_by(id=repo.id).delete(synchronize_session=False)
            Project.query.filter_by(id=project.id).delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()


def test_window_start_plus_30min_commit_is_in_scope(seeded_window):
    """窗口起点 + 30 分钟的提交必须落在同步范围内（目标文件集合里要有它）。

    变红意味着：窗口比较又改回「直接比较 config.start_time」，窗口偏移 8 小时，
    每周窗口**前 8 小时的提交被静默丢弃** —— 不报错，只是漏掉整段变更。
    """
    from models import Commit, WeeklyVersionConfig

    with seeded_window.flask_app.app_context():
        config = seeded_window.db.session.get(WeeklyVersionConfig, seeded_window.config_id)
        target_paths = weekly_logic.collect_target_file_paths(
            Commit, seeded_window.repo_id, *weekly_logic.weekly_window_in_utc(config))

        # 前提：这条提交的库内值确实「看起来」早于窗口起点（正是 8 小时陷阱的形状）
        row = Commit.query.filter_by(commit_id='early00001').first()
        assert row.commit_time < config.start_time, (
            '构造前提不成立：本用例要的就是「库内 naive-UTC 值看起来早于 config.start_time」'
        )
        assert 'config/tz.xlsx' in target_paths, (
            f'窗口起点 + 30 分钟的提交没被算进同步范围，实际目标文件={target_paths}'
        )


# ---------------------------------------------------------------------------
# 一之四、周期性同步不许把用户按在「请稍候」上（2026-09-20）
#
# 同步是周期性的：每几分钟派一轮，一轮要跑好几分钟。所以「最新一条任务是 pending」
# 在稳态下几乎恒真 —— 实测两条配置都显示「同步任务排队中，请稍候」，而它们的
# 27 个文件在上一轮就同步完了。用户看到那句话就一直等一个根本不用等的东西。
# ---------------------------------------------------------------------------
def test_a_queued_periodic_sync_does_not_tell_the_user_to_wait(monkeypatch):
    """已经出过一轮结论之后再排队的同步：**不许**说「请稍候」。

    变红意味着：数据早就绪，页面却一直劝用户等（这正是这次报上来的现象）。
    """
    task = SimpleNamespace(commit_id='901', status='pending', error_message=None)

    html = _render_config_page(monkeypatch, task=task, finished=True)

    assert '请稍候' not in html, '已经同步过的配置又被劝着等'
    assert '同步正常' in html


def test_the_first_sync_still_tells_the_user_to_wait(monkeypatch):
    """**反自检**：首轮同步（还没有任何一轮出过结论）时那句话必须原样在。

    少了这一条，一个「把排队提示整个删掉」的实现能让上面那条全绿 ——
    而首轮同步确实没有数据可看，那时不提示就是让人对着一张空表发呆。
    """
    task = SimpleNamespace(commit_id='901', status='pending', error_message=None)

    html = _render_config_page(monkeypatch, task=task, finished=False)

    assert '同步任务排队中，请稍候。' in html


def test_the_queued_round_is_still_mentioned(monkeypatch):
    """也不能反过来把排队这件事藏掉：用户得知道「你看到的可能是上一轮的数据」。"""
    task = SimpleNamespace(commit_id='901', status='processing', error_message=None)

    html = _render_config_page(monkeypatch, task=task, finished=True)

    assert '新一轮同步进行中' in html


def test_configs_with_finished_sync_works_against_the_real_model():
    """用真实 BackgroundTask 模型跑一遍，确认这条查询链可用（查不动会退回「请稍候」）。"""
    import app as app_module
    from models import BackgroundTask
    from services.weekly_version_sync_status import configs_with_finished_sync

    with app_module.app.app_context():
        result = configs_with_finished_sync(BackgroundTask, [SimpleNamespace(id=99999992)])

    assert result == set()


def test_a_completed_task_does_not_keep_a_stale_failure_reason():
    """跑成功的任务不许留着上一轮的失败原因。

    真实成因（2026-09-20 本机实测）：调度器先把卡住的 pending 标成 failed 并写下
    「任务超时，已被调度器重置」，随后 worker 真的把它跑完了、状态改回 completed ——
    而那句失败原因留在列上。配置页的模板**优先显示 error_message**，于是一次成功的同步
    在界面上报着一次不存在的失败（库里 5 条 completed 任务带着这句话），
    用户照着它去查一个没发生的问题。

    变红意味着：`completed` 那条分支不再清理 error_message。
    """
    import app as app_module
    from models import BackgroundTask, db
    from services.task_worker_service import update_task_status_with_retry

    with app_module.app.app_context():
        task = BackgroundTask(
            task_type='weekly_sync', commit_id='900001', status='pending',
            error_message='任务超时，已被调度器重置',
        )
        db.session.add(task)
        db.session.commit()
        task_id = task.id

        update_task_status_with_retry(task_id, 'completed')

        db.session.expire_all()
        stored = db.session.get(BackgroundTask, task_id)
        assert stored.status == 'completed'
        assert stored.error_message is None, (
            f'跑成功的任务还挂着上一轮的失败原因：{stored.error_message!r}'
        )
        db.session.delete(stored)
        db.session.commit()


def test_a_failed_task_still_records_its_reason():
    """**反自检**：清 error_message 只对 completed 成立，failed 必须照样写。

    少了这一条，一个「无条件清空 error_message」的实现能让上面那条全绿 ——
    而那正是把「为什么这个周版本没数据」重新变回不可见。
    """
    import app as app_module
    from models import BackgroundTask, db
    from services.task_worker_service import update_task_status_with_retry

    with app_module.app.app_context():
        task = BackgroundTask(
            task_type='weekly_sync', commit_id='900002', status='pending',
        )
        db.session.add(task)
        db.session.commit()
        task_id = task.id

        update_task_status_with_retry(task_id, 'failed', '配置已被禁用')

        db.session.expire_all()
        stored = db.session.get(BackgroundTask, task_id)
        assert stored.status == 'failed'
        assert stored.error_message == '配置已被禁用'
        db.session.delete(stored)
        db.session.commit()
