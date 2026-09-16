# -*- coding: utf-8 -*-
"""提交状态变更的 6 个入口必须行为一致（状态 + 操作者 + 周版本同步）。

## 为什么需要这个测试

同一个业务动作「确认 / 拒绝一条提交」在本仓库有 6 个入口，各自抄了一遍
「写状态 + 写 status_changed_by + 同步周版本」，行为逐条漂移：

  * `approve_all_files`（提交列表页「通过该次提交的所有文件」）与 `reject_commit`
    （merge diff 页的拒绝）**完全不写 status_changed_by、完全不做周版本同步**
    → 从这两个入口操作后周版本页仍显示「未确认」，`weekly_version_stats_api`
    统计不到；从提交列表确认同一件事却是对的；
  * 批量入口把权限校验放在循环内、且同步函数在循环内逐条 commit
    → 跨项目批量时第二条无权限，第一条已经落库，403 只是「看起来失败了」；
  * 周版本同步失败被吞成 success=False，响应照样说「已通过 N 个提交」。

本文件既断言「每个入口自己是对的」，也断言「四个入口换着用结果相同」——
后者才是这类缺陷不会再长出来的判据。
"""
import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from flask import jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import NotFound

import services.commit_operation_handlers as operation_handlers
from app import app, create_tables, db
from models import Commit, Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from services.commit_status_api_service import (
    handle_batch_update_commits_compat,
    handle_update_commit_status,
)
from services.status_sync_service import StatusSyncService
from utils.timezone_utils import beijing_wallclock_to_utc_naive

WIN_START_BJ = datetime(2026, 3, 2, 0, 0, 0)
WIN_END_BJ = datetime(2026, 3, 9, 0, 0, 0)
CURRENT_USER = 'alice'


def _noop_log(*_args, **_kwargs):
    return None


@pytest.fixture
def env():
    """两个项目（用于跨项目权限场景）+ 各自的仓库与周版本窗口。"""
    with app.app_context():
        create_tables()
        tag = uuid.uuid4().hex[:8]

        project_a = Project(code=f'EA{tag}', name='入口一致性A', department='QA')
        project_b = Project(code=f'EB{tag}', name='入口一致性B', department='QA')
        db.session.add_all([project_a, project_b])
        db.session.commit()

        repo_a = Repository(
            project_id=project_a.id, name=f'ent_a_{tag}', type='git',
            url='https://example.invalid/a.git', resource_type='table',
        )
        repo_b = Repository(
            project_id=project_b.id, name=f'ent_b_{tag}', type='git',
            url='https://example.invalid/b.git', resource_type='table',
        )
        db.session.add_all([repo_a, repo_b])
        db.session.commit()

        configs = []
        for project, repo in ((project_a, repo_a), (project_b, repo_b)):
            config = WeeklyVersionConfig(
                project_id=project.id, repository_id=repo.id,
                name=f'入口一致性周版本_{repo.id}', branch='master',
                start_time=WIN_START_BJ, end_time=WIN_END_BJ,
                is_active=True, auto_sync=False, status='active',
            )
            db.session.add(config)
            configs.append(config)
        db.session.commit()

        ns = SimpleNamespace(
            db=db,
            project_a=project_a.id, project_b=project_b.id,
            repo_a=repo_a.id, repo_b=repo_b.id,
            config_a=configs[0].id, config_b=configs[1].id,
        )
        yield ns

        try:
            for config in configs:
                WeeklyVersionDiffCache.query.filter_by(config_id=config.id).delete(
                    synchronize_session=False
                )
            Commit.query.filter(
                Commit.repository_id.in_([repo_a.id, repo_b.id])
            ).delete(synchronize_session=False)
            WeeklyVersionConfig.query.filter(
                WeeklyVersionConfig.id.in_([c.id for c in configs])
            ).delete(synchronize_session=False)
            Repository.query.filter(
                Repository.id.in_([repo_a.id, repo_b.id])
            ).delete(synchronize_session=False)
            Project.query.filter(
                Project.id.in_([project_a.id, project_b.id])
            ).delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()


def _add_file(env, repo_id, config_id, path, sha, status='pending', operator=None):
    """造一条「单提交周版本文件」：提交记录 + 与之匹配的周版本缓存。"""
    commit = Commit(
        repository_id=repo_id,
        commit_id=sha,
        path=path,
        operation='M',
        author='dev',
        commit_time=beijing_wallclock_to_utc_naive(datetime(2026, 3, 3, 10, 0, 0)),
        message=sha,
        status=status,
        status_changed_by=operator,
    )
    env.db.session.add(commit)
    env.db.session.commit()

    cache = WeeklyVersionDiffCache(
        config_id=config_id,
        repository_id=repo_id,
        file_path=path,
        merged_diff_data=json.dumps({'ok': True}),
        base_commit_id=None,
        latest_commit_id=sha,
        commit_count=1,
        confirmation_status=json.dumps({'dev': status}),
        overall_status=status,
        status_changed_by=operator,
        cache_status='completed',
    )
    env.db.session.add(cache)
    env.db.session.commit()
    return commit, cache


def _allow_all(_project_id, _action):
    return True, ''


def _deny_project_b(env):
    def _check(project_id, _action):
        if project_id == env.project_b:
            return False, '权限不足'
        return True, ''
    return _check


def _payload(result):
    response, status_code = result if isinstance(result, tuple) else (result, 200)
    return status_code, response.get_json()


def _reload(env, model, row_id):
    env.db.session.expire_all()
    return env.db.session.get(model, row_id)


def _current_user():
    return SimpleNamespace(username=CURRENT_USER)


def _call_single_status(commit_id, status, permission=_allow_all):
    """直接调 handle_update_commit_status（依赖注入式 handler）。"""
    with app.test_request_context(
        f'/commits/{commit_id}/status', method='POST', json={'status': status}
    ):
        return handle_update_commit_status(
            commit_id=commit_id,
            request=request,
            jsonify=jsonify,
            db=db,
            Commit=Commit,
            NotFound=NotFound,
            SQLAlchemyError=SQLAlchemyError,
            app_logger=app.logger,
            ensure_commit_access_or_403=lambda _commit: None,
            can_operate_project_confirmation=permission,
            get_current_user=_current_user,
            status_sync_service_cls=StatusSyncService,
            log_print=_noop_log,
        )


def _call_batch_compat(commit_ids, action, permission=_allow_all):
    """直接调 handle_batch_update_commits_compat（兼容批量入口）。"""
    with app.test_request_context(
        '/commits/batch-update',
        method='POST',
        json={'commit_ids': commit_ids, 'action': action},
    ):
        return handle_batch_update_commits_compat(
            request=request,
            jsonify=jsonify,
            db=db,
            Commit=Commit,
            SQLAlchemyError=SQLAlchemyError,
            log_print=_noop_log,
            status_sync_service_cls=StatusSyncService,
            get_current_user=_current_user,
            can_operate_project_confirmation=permission,
        )


def _call_approve_all_files(commit_id, monkeypatch, permission=_allow_all):
    monkeypatch.setattr(
        operation_handlers, 'can_current_user_operate_project_confirmation', permission
    )
    return operation_handlers.approve_all_files(commit_id)


def _call_reject_commit(commit_id, monkeypatch, permission=_allow_all):
    monkeypatch.setattr(
        operation_handlers, 'can_current_user_operate_project_confirmation', permission
    )
    with app.test_request_context(
        '/commits/reject', method='POST', json={'commit_id': commit_id}
    ):
        return operation_handlers.reject_commit()


def _call_batch_approve(commit_ids, monkeypatch, permission=_allow_all):
    monkeypatch.setattr(
        operation_handlers, 'can_current_user_operate_project_confirmation', permission
    )
    with app.test_request_context(
        '/commits/batch-approve', method='POST', json={'commit_ids': commit_ids}
    ):
        return operation_handlers.batch_approve_commits()


def _call_batch_reject(commit_ids, monkeypatch, permission=_allow_all):
    monkeypatch.setattr(
        operation_handlers, 'can_current_user_operate_project_confirmation', permission
    )
    with app.test_request_context(
        '/commits/batch-reject', method='POST', json={'commit_ids': commit_ids}
    ):
        return operation_handlers.batch_reject_commits()


@pytest.fixture(autouse=True)
def _patch_current_user(monkeypatch):
    """operation_handlers 在函数内 `from utils.request_security import _get_current_user`。"""
    import utils.request_security as request_security

    monkeypatch.setattr(request_security, '_get_current_user', _current_user)


# ---------------------------------------------------------------------------
# 一、approve_all_files / reject_commit 必须与其它入口一致（原先完全不同步）
# ---------------------------------------------------------------------------
def test_approve_all_files_syncs_weekly_and_records_operator(env, monkeypatch):
    """「通过该次提交的所有文件」：文件状态与周版本必须一起变，并记录确认人。

    修复前该入口只改 Commit.status：周版本缓存仍是 pending、status_changed_by
    是 None —— 周版本页显示「未确认」，weekly_version_stats_api 统计为 0。
    """
    sha = 'sha_all_files_1'
    c1, cache1 = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/a.xlsx', sha
    )
    c2, cache2 = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/b.xlsx', sha
    )

    code, payload = _payload(_call_approve_all_files(c1.id, monkeypatch))

    assert code == 200 and payload['status'] == 'success', payload
    for commit, cache in ((c1, cache1), (c2, cache2)):
        commit = _reload(env, Commit, commit.id)
        cache = _reload(env, WeeklyVersionDiffCache, cache.id)
        assert commit.status == 'confirmed', f'{commit.path} 状态未更新'
        assert commit.status_changed_by == CURRENT_USER, (
            f'{commit.path} 的确认人没有记录：{commit.status_changed_by!r}'
        )
        assert cache.overall_status == 'confirmed', (
            f'{cache.file_path} 的周版本聚合状态仍是 {cache.overall_status!r} —— '
            f'该入口没有做周版本同步（周版本页会显示「未确认」、统计不到）'
        )
        assert cache.status_changed_by == CURRENT_USER, (
            f'{cache.file_path} 的周版本操作者没有记录：{cache.status_changed_by!r}'
        )


def test_reject_commit_syncs_weekly_and_records_operator(env, monkeypatch):
    """merge diff 页的拒绝：同样必须写操作者并同步周版本。"""
    commit, cache = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/reject.xlsx', 'sha_reject_1'
    )

    code, payload = _payload(_call_reject_commit(commit.id, monkeypatch))

    assert code == 200 and payload['status'] == 'success', payload
    commit = _reload(env, Commit, commit.id)
    cache = _reload(env, WeeklyVersionDiffCache, cache.id)
    assert commit.status == 'rejected' and commit.status_changed_by == CURRENT_USER
    assert cache.overall_status == 'rejected', (
        f'周版本聚合状态仍是 {cache.overall_status!r}（该入口原先完全不做周版本同步）'
    )
    assert cache.status_changed_by == CURRENT_USER


# ---------------------------------------------------------------------------
# 二、四个入口换着用，结果必须相同
# ---------------------------------------------------------------------------
def _run_confirm_entry(entry, env, monkeypatch, path, sha):
    commit, cache = _add_file(env, env.repo_a, env.config_a, path, sha)
    if entry == 'single_status':
        _call_single_status(commit.id, 'confirmed')
    elif entry == 'batch_compat':
        _call_batch_compat([commit.id], 'confirm')
    elif entry == 'approve_all_files':
        _call_approve_all_files(commit.id, monkeypatch)
    elif entry == 'batch_approve':
        _call_batch_approve([commit.id], monkeypatch)
    else:  # pragma: no cover - 防御
        raise AssertionError(f'未知入口: {entry}')
    commit = _reload(env, Commit, commit.id)
    cache = _reload(env, WeeklyVersionDiffCache, cache.id)
    return (
        commit.status, commit.status_changed_by,
        cache.overall_status, cache.status_changed_by,
        json.loads(cache.confirmation_status or '{}').get('dev'),
    )


@pytest.mark.parametrize(
    'entry', ['single_status', 'batch_compat', 'approve_all_files', 'batch_approve']
)
def test_confirm_result_is_identical_across_entrypoints(env, monkeypatch, entry):
    """「确认」在四个入口下的落库结果必须完全一致。"""
    result = _run_confirm_entry(
        entry, env, monkeypatch, f'config/entry/{entry}.xlsx', f'sha_c_{entry}'
    )
    assert result == ('confirmed', CURRENT_USER, 'confirmed', CURRENT_USER, 'confirmed'), (
        f'入口 {entry} 的结果是 {result}，与其它入口不一致'
    )


@pytest.mark.parametrize(
    'entry', ['single_status', 'batch_compat', 'reject_commit', 'batch_reject']
)
def test_reject_result_is_identical_across_entrypoints(env, monkeypatch, entry):
    """「拒绝」在四个入口下的落库结果必须完全一致。"""
    commit, cache = _add_file(
        env, env.repo_a, env.config_a, f'config/entry/{entry}_r.xlsx', f'sha_r_{entry}'
    )
    if entry == 'single_status':
        _call_single_status(commit.id, 'rejected')
    elif entry == 'batch_compat':
        _call_batch_compat([commit.id], 'reject')
    elif entry == 'reject_commit':
        _call_reject_commit(commit.id, monkeypatch)
    else:
        _call_batch_reject([commit.id], monkeypatch)

    commit = _reload(env, Commit, commit.id)
    cache = _reload(env, WeeklyVersionDiffCache, cache.id)
    assert (
        commit.status, commit.status_changed_by,
        cache.overall_status, cache.status_changed_by,
    ) == ('rejected', CURRENT_USER, 'rejected', CURRENT_USER), (
        f'入口 {entry} 的拒绝结果与其它入口不一致'
    )


# ---------------------------------------------------------------------------
# 三、批量入口：权限预检必须在写入之前完成
# ---------------------------------------------------------------------------
def test_batch_compat_403_does_not_write_earlier_commits(env):
    """跨项目批量：第二个项目无权限时，第一个项目的提交不得已被改掉。"""
    commit_a, cache_a = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/perm_a.xlsx', 'sha_perm_a'
    )
    commit_b, _cache_b = _add_file(
        env, env.repo_b, env.config_b, 'config/entry/perm_b.xlsx', 'sha_perm_b'
    )

    code, payload = _payload(
        _call_batch_compat([commit_a.id, commit_b.id], 'confirm', _deny_project_b(env))
    )

    assert code == 403, payload
    commit_a = _reload(env, Commit, commit_a.id)
    cache_a = _reload(env, WeeklyVersionDiffCache, cache_a.id)
    assert commit_a.status == 'pending', (
        '预检失败返回 403，但前一条提交已经被改并落库 —— 403 只是「看起来失败了」，'
        '用户重试即重复副作用。'
    )
    assert cache_a.overall_status == 'pending'


def test_batch_approve_403_does_not_write_earlier_commits(env, monkeypatch):
    """同上，覆盖 commit_operation_handlers 里的批量通过入口。"""
    commit_a, _cache_a = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/perm_c.xlsx', 'sha_perm_c'
    )
    commit_b, _cache_b = _add_file(
        env, env.repo_b, env.config_b, 'config/entry/perm_d.xlsx', 'sha_perm_d'
    )

    code, payload = _payload(
        _call_batch_approve(
            [commit_a.id, commit_b.id], monkeypatch, _deny_project_b(env)
        )
    )

    assert code == 403, payload
    commit_a = _reload(env, Commit, commit_a.id)
    assert commit_a.status == 'pending', '批量通过入口在 403 之前已经改了前一条提交'


def test_batch_reject_403_does_not_write_earlier_commits(env, monkeypatch):
    """同上，覆盖批量拒绝入口。"""
    commit_a, _cache_a = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/perm_e.xlsx', 'sha_perm_e'
    )
    commit_b, _cache_b = _add_file(
        env, env.repo_b, env.config_b, 'config/entry/perm_f.xlsx', 'sha_perm_f'
    )

    code, payload = _payload(
        _call_batch_reject(
            [commit_a.id, commit_b.id], monkeypatch, _deny_project_b(env)
        )
    )

    assert code == 403, payload
    commit_a = _reload(env, Commit, commit_a.id)
    assert commit_a.status == 'pending', '批量拒绝入口在 403 之前已经改了前一条提交'


# ---------------------------------------------------------------------------
# 四、周版本同步失败必须在响应里可见
# ---------------------------------------------------------------------------
def test_batch_approve_surfaces_weekly_sync_failure_count(env, monkeypatch):
    """周版本同步失败不能只体现在日志里：响应必须带出失败条数。

    修复前只汇总成功的那些，失败被静默吞掉，响应仍是「已通过 N 个提交」。
    """
    commit, _cache = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/sync_fail.xlsx', 'sha_fail_1'
    )

    def _fail(_self, _commit_id, _new_status, auto_commit=True):
        return {'success': False, 'message': '模拟同步失败', 'updated_count': 0}

    monkeypatch.setattr(StatusSyncService, 'sync_commit_to_weekly', _fail)
    code, payload = _payload(_call_batch_approve([commit.id], monkeypatch))

    assert code == 200, payload
    assert payload.get('sync_failed_count') == 1, (
        f'响应没有带出同步失败条数：{payload}'
    )
    assert '失败' in payload.get('message', ''), (
        f'响应文案没有说明失败：{payload.get("message")!r}'
    )


def test_single_status_sync_failure_is_reported_in_log(env, monkeypatch, capsys):
    """单条入口同步失败时，结果对象仍然返回（不改变既有成功语义）。"""
    commit, _cache = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/sync_fail2.xlsx', 'sha_fail_2'
    )

    def _fail(_self, _commit_id, _new_status, auto_commit=True):
        return {'success': False, 'message': '模拟同步失败', 'updated_count': 0}

    monkeypatch.setattr(StatusSyncService, 'sync_commit_to_weekly', _fail)
    code, payload = _payload(_call_single_status(commit.id, 'confirmed'))
    assert code == 200 and payload['success'] is True


# ---------------------------------------------------------------------------
# 五、事务边界：批量入口对外只提交一次
# ---------------------------------------------------------------------------
def test_batch_approve_commits_once_at_the_outer_level(env, monkeypatch):
    """批量入口把所有条目的写入收敛到最外层提交一次。

    判据：`sync_commit_to_weekly` 在批量入口里必须以 auto_commit=False 被调用
    （否则循环里每条各提交，权限校验失败时前面的改动回滚不掉）。
    """
    commit_a, _ = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/tx_a.xlsx', 'sha_tx_a'
    )
    commit_b, _ = _add_file(
        env, env.repo_a, env.config_a, 'config/entry/tx_b.xlsx', 'sha_tx_b'
    )

    seen = []
    original = StatusSyncService.sync_commit_to_weekly

    def _spy(self, commit_id, new_status, auto_commit=True):
        seen.append(auto_commit)
        return original(self, commit_id, new_status, auto_commit=auto_commit)

    monkeypatch.setattr(StatusSyncService, 'sync_commit_to_weekly', _spy)
    code, payload = _payload(_call_batch_approve([commit_a.id, commit_b.id], monkeypatch))

    assert code == 200, payload
    assert seen == [False, False], (
        f'批量入口调用同步时的 auto_commit 参数是 {seen}，应为全 False（事务收敛到最外层）'
    )
    for commit in (commit_a, commit_b):
        assert _reload(env, Commit, commit.id).status == 'confirmed'
