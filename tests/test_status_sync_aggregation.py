# -*- coding: utf-8 -*-
"""周版本 ↔ 提交 双向状态同步：聚合口径与「审核窗口」边界。

## 为什么需要这个测试

`StatusSyncService` 的两条链路由同一个「相关提交」集合驱动，而这个集合此前混进了
`cache.base_commit_id`。基准提交**不属于本周审核对象**：它是窗口起点之前的最后一个
提交（`weekly_version_logic.generate_weekly_merged_diff` 里
`commit_time < 窗口起点 order_by desc` 选出来的），代表上一个周版本的既有状态。

由此产生两类缺陷：

1. **聚合口径被历史基准污染**（`sync_commit_to_weekly`）
   窗口内所有提交都确认了，聚合状态却因为「上周的基准还没确认」而停在待确认；
2. **反向同步改写窗口外的历史**（`sync_weekly_to_commit`）
   确认本周 2 条变更时，把窗口前的基准提交一起改了 —— 共更新 3 条。

另外 `_sync_merged_diff_status` 的处理方式是「按本次变更分支判断 + 保留旧值」，
而不是「按窗口内所有提交重算」：
  - 把一条提交退回待确认时只检查「还有没有别的 confirmed」，**完全忽略 rejected**
    → 还有拒绝，周聚合却被清成待确认；
  - 反过来，只要「还有别的 confirmed」就完全不更新 → 一条提交退回待确认后，
    周聚合仍显示已确认。

## 语义（本文件的判定标准）

按窗口内**所有**提交重算：有 rejected → rejected（拒绝优先）；全部 confirmed →
confirmed；其余 → pending。

## 时区

窗口是北京墙钟（用户在 datetime-local 里填的），`Commit.commit_time` 是 naive-UTC
墙钟，两者差 8 小时且不报错。本文件的提交时间全部经
`beijing_wallclock_to_utc_naive` 换算后再入库，与生产写入路径一致。
"""
import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

import services.weekly_version_logic as weekly_logic
from app import app, create_tables, db
from models import Commit, Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from services.status_sync_service import StatusSyncService
from utils.timezone_utils import beijing_wallclock_to_utc_naive

# 审核窗口（用户在浏览器里填的北京墙钟）：2026-03-02 00:00 ~ 2026-03-09 00:00
WIN_START_BJ = datetime(2026, 3, 2, 0, 0, 0)
WIN_END_BJ = datetime(2026, 3, 9, 0, 0, 0)


def _commit_time(bj_naive: datetime):
    """北京墙钟 → 库里该存的 naive-UTC 墙钟（与生产写入路径同口径）。"""
    return beijing_wallclock_to_utc_naive(bj_naive)


class _StubWeeklyExcelCacheService:
    @staticmethod
    def needs_merged_diff_cache(_config_id, _file_path):
        return False


@pytest.fixture
def env():
    """项目/仓库/周版本配置 + 唯一化的清理。

    用唯一的 code / 仓库名：conftest 的隔离库是整个进程共用的，固定值会撞 UNIQUE。
    """
    with app.app_context():
        create_tables()
        tag = uuid.uuid4().hex[:8]

        project = Project(code=f'SY{tag}', name='状态同步测试项目', department='QA')
        db.session.add(project)
        db.session.commit()

        repo = Repository(
            project_id=project.id,
            name=f'sync_repo_{tag}',
            type='git',
            url='https://example.invalid/sync.git',
            resource_type='table',
        )
        db.session.add(repo)
        db.session.commit()

        config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repo.id,
            name='状态同步测试周版本',
            branch='master',
            start_time=WIN_START_BJ,
            end_time=WIN_END_BJ,
            is_active=True,
            auto_sync=False,
            status='active',
        )
        db.session.add(config)
        db.session.commit()

        ns = SimpleNamespace(
            db=db, config_id=config.id, repo_id=repo.id, project_id=project.id,
        )
        yield ns

        try:
            WeeklyVersionDiffCache.query.filter_by(config_id=config.id).delete(
                synchronize_session=False
            )
            Commit.query.filter_by(repository_id=repo.id).delete(synchronize_session=False)
            WeeklyVersionConfig.query.filter_by(id=config.id).delete(synchronize_session=False)
            Repository.query.filter_by(id=repo.id).delete(synchronize_session=False)
            Project.query.filter_by(id=project.id).delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()


def _add_commit(env, path, sha, bj_naive, status='pending', operator=None):
    commit = Commit(
        repository_id=env.repo_id,
        commit_id=sha,
        path=path,
        operation='M',
        author='dev',
        commit_time=_commit_time(bj_naive),
        message=sha,
        status=status,
        status_changed_by=operator,
    )
    env.db.session.add(commit)
    env.db.session.commit()
    return commit


def _add_cache(env, path, base, latest, commit_count, overall='pending',
               operator=None, confirmation=None):
    cache = WeeklyVersionDiffCache(
        config_id=env.config_id,
        repository_id=env.repo_id,
        file_path=path,
        merged_diff_data=json.dumps({'ok': True}),
        base_commit_id=base.commit_id if base else None,
        latest_commit_id=latest.commit_id if latest else None,
        commit_authors=json.dumps(['dev']),
        commit_messages=json.dumps(['m']),
        commit_times=json.dumps([]),
        commit_count=commit_count,
        confirmation_status=confirmation or json.dumps({'dev': overall}),
        overall_status=overall,
        status_changed_by=operator,
        cache_status='completed',
    )
    env.db.session.add(cache)
    env.db.session.commit()
    return cache


def _reload(env, cache_id):
    env.db.session.expire_all()
    return env.db.session.get(WeeklyVersionDiffCache, cache_id)


# ---------------------------------------------------------------------------
# 一、周版本 → 提交：反向同步只覆盖审核窗口内的提交
# ---------------------------------------------------------------------------
def test_weekly_confirm_does_not_touch_base_commit_before_window(env):
    """确认本周文件只应确认窗口内的提交，不能连窗口前的历史基准一起确认。

    修复前 `_find_related_commits` 无条件把 `cache.base_commit_id` 放进集合：
    本周确认 2 条变更（win0000001/2）却更新了 3 条，窗口前的基准提交
    base000001（上周已拒绝）被改写成已确认 —— 历史审核结果被本周审核覆盖。
    """
    path = 'config/sync/confirm_scope.xlsx'
    base = _add_commit(
        env, path, 'base000001', datetime(2026, 3, 1, 10, 0, 0),
        status='rejected', operator='last_week_reviewer',
    )
    c1 = _add_commit(env, path, 'win0000001', datetime(2026, 3, 2, 10, 0, 0))
    c2 = _add_commit(env, path, 'win0000002', datetime(2026, 3, 4, 10, 0, 0))
    cache = _add_cache(env, path, base, c2, 2)
    cache.status_changed_by = 'alice'
    env.db.session.commit()

    result = StatusSyncService(env.db).sync_weekly_to_commit(env.config_id, path, 'confirmed')

    env.db.session.expire_all()
    base = env.db.session.get(Commit, base.id)
    c1 = env.db.session.get(Commit, c1.id)
    c2 = env.db.session.get(Commit, c2.id)

    assert base.status == 'rejected', (
        f'窗口前的基准提交 base000001 被改成了 {base.status}。\n'
        f'基准是上一个周版本的既有状态（窗口起点之前的最后一个提交），'
        f'本周审核不得改写它 —— 否则历史审核结果会被本周操作覆盖。'
    )
    assert base.status_changed_by == 'last_week_reviewer', (
        f'窗口前基准提交的操作者被改成了 {base.status_changed_by!r}'
    )
    assert (c1.status, c2.status) == ('confirmed', 'confirmed'), (
        f'窗口内提交未被正确确认：{c1.status} / {c2.status}'
    )
    assert (c1.status_changed_by, c2.status_changed_by) == ('alice', 'alice')
    assert result['updated_count'] == 2, (
        f"updated_count={result['updated_count']}，应为 2（只有窗口内的两条提交）"
    )


def test_weekly_reject_does_not_touch_base_commit_before_window(env):
    """拒绝同理：窗口前的基准提交不得被反向同步改写。"""
    path = 'config/sync/reject_scope.xlsx'
    base = _add_commit(
        env, path, 'base000002', datetime(2026, 3, 1, 10, 0, 0),
        status='confirmed', operator='last_week_reviewer',
    )
    c1 = _add_commit(env, path, 'win0000003', datetime(2026, 3, 3, 10, 0, 0))
    c2 = _add_commit(env, path, 'win0000004', datetime(2026, 3, 5, 10, 0, 0))
    cache = _add_cache(env, path, base, c2, 2)
    cache.status_changed_by = 'bob'
    env.db.session.commit()

    result = StatusSyncService(env.db).sync_weekly_to_commit(env.config_id, path, 'rejected')

    env.db.session.expire_all()
    base = env.db.session.get(Commit, base.id)
    c1 = env.db.session.get(Commit, c1.id)
    c2 = env.db.session.get(Commit, c2.id)

    assert base.status == 'confirmed', '窗口前的基准提交被本周的拒绝操作改写了'
    assert base.status_changed_by == 'last_week_reviewer'
    assert (c1.status, c2.status) == ('rejected', 'rejected')
    assert result['updated_count'] == 2


# ---------------------------------------------------------------------------
# 二、提交 → 周版本：合并 diff 的聚合状态按窗口内提交重算
# ---------------------------------------------------------------------------
def test_merged_weekly_keeps_rejected_while_any_window_commit_is_rejected(env):
    """窗口内还有 rejected 时，周聚合必须保持 rejected（拒绝优先）。

    修复前退回待确认的分支只问「还有没有别的 confirmed」，对 rejected 视而不见：
    win0000003 退回待确认后，周聚合从 rejected 被清成 pending，
    而 win0000004 明明还是拒绝状态。
    """
    path = 'config/sync/rejected_priority.xlsx'
    # 基准是窗口前的历史提交（上周被拒绝），不参与本周聚合
    base = _add_commit(
        env, path, 'base000003', datetime(2026, 3, 1, 10, 0, 0),
        status='rejected', operator='last_week_reviewer',
    )
    c1 = _add_commit(
        env, path, 'win0000005', datetime(2026, 3, 2, 10, 0, 0),
        status='rejected', operator='bob',
    )
    _add_commit(
        env, path, 'win0000006', datetime(2026, 3, 4, 10, 0, 0),
        status='rejected', operator='bob',
    )
    c3 = _add_commit(env, path, 'win0000007', datetime(2026, 3, 6, 10, 0, 0))
    cache = _add_cache(env, path, base, c3, 3, overall='rejected', operator='bob')

    StatusSyncService(env.db).sync_commit_to_weekly(c1.id, 'pending')

    cache = _reload(env, cache.id)
    assert cache.overall_status == 'rejected', (
        f'窗口内仍有拒绝提交（win0000006），周聚合却是 {cache.overall_status!r}。\n'
        f'聚合语义：有 rejected → rejected；全部 confirmed → confirmed；其余 → pending。'
    )


def test_merged_weekly_does_not_stay_confirmed_when_a_commit_returns_to_pending(env):
    """窗口内出现待确认提交时，周聚合必须从已确认退回待确认。

    修复前「还有别的 confirmed 就不更新」，于是 win0000009 已退回待确认，
    周聚合仍停留在已确认。
    """
    path = 'config/sync/confirmed_stale.xlsx'
    base = _add_commit(
        env, path, 'base000004', datetime(2026, 3, 1, 10, 0, 0), status='confirmed',
    )
    _add_commit(
        env, path, 'win0000008', datetime(2026, 3, 2, 10, 0, 0),
        status='confirmed', operator='alice',
    )
    c2 = _add_commit(
        env, path, 'win0000009', datetime(2026, 3, 4, 10, 0, 0),
        status='confirmed', operator='alice',
    )
    cache = _add_cache(env, path, base, c2, 2, overall='confirmed', operator='alice')

    StatusSyncService(env.db).sync_commit_to_weekly(c2.id, 'pending')

    cache = _reload(env, cache.id)
    assert cache.overall_status == 'pending', (
        f'窗口内已有待确认提交（win0000009），周聚合却是 {cache.overall_status!r}'
    )
    assert cache.status_changed_by is None, (
        f'回到待确认后仍留着操作者 {cache.status_changed_by!r}'
    )


def test_merged_weekly_confirms_when_all_window_commits_confirmed(env):
    """窗口内全部确认 → 周聚合 confirmed；窗口前的基准不参与判定。

    修复前聚合集合里混进了窗口前的基准（base000005 是历史 pending），
    于是窗口内三条全部确认也永远凑不齐 confirmed，周聚合停在 pending。
    """
    path = 'config/sync/all_confirmed.xlsx'
    base = _add_commit(
        env, path, 'base000005', datetime(2026, 3, 1, 10, 0, 0),
        status='pending', operator=None,
    )
    _add_commit(
        env, path, 'win0000010', datetime(2026, 3, 2, 10, 0, 0),
        status='confirmed', operator='alice',
    )
    _add_commit(
        env, path, 'win0000011', datetime(2026, 3, 4, 10, 0, 0),
        status='confirmed', operator='alice',
    )
    c3 = _add_commit(env, path, 'win0000012', datetime(2026, 3, 6, 10, 0, 0))
    cache = _add_cache(env, path, base, c3, 3)
    cache.status_changed_by = 'alice'
    env.db.session.commit()

    StatusSyncService(env.db).sync_commit_to_weekly(c3.id, 'confirmed')

    cache = _reload(env, cache.id)
    assert cache.overall_status == 'confirmed', (
        f'窗口内三条提交（win0000010/11/12）全部确认，周聚合却是 {cache.overall_status!r}。\n'
        f'窗口前的基准提交 base000005（历史 pending）不属于本周审核对象，不应参与聚合。'
    )
    assert json.loads(cache.confirmation_status or '{}').get('dev') == 'confirmed'
    base = env.db.session.get(Commit, base.id)
    assert base.status == 'pending', '窗口前的基准提交不应被本周的确认带上去'


def test_merged_weekly_stays_pending_when_one_window_commit_is_pending(env):
    """反向保险：窗口内还有待确认时，不得因为「别的都确认了」而标成已确认。"""
    path = 'config/sync/still_pending.xlsx'
    base = _add_commit(
        env, path, 'base000006', datetime(2026, 3, 1, 10, 0, 0), status='confirmed',
    )
    c1 = _add_commit(
        env, path, 'win0000013', datetime(2026, 3, 2, 10, 0, 0),
        status='confirmed', operator='alice',
    )
    c2 = _add_commit(env, path, 'win0000014', datetime(2026, 3, 4, 10, 0, 0))
    cache = _add_cache(env, path, base, c2, 2, overall='confirmed', operator='alice')

    StatusSyncService(env.db).sync_commit_to_weekly(c1.id, 'confirmed')

    cache = _reload(env, cache.id)
    assert cache.overall_status == 'pending', (
        f'窗口内 win0000014 仍是待确认，周聚合却是 {cache.overall_status!r}'
    )


# ---------------------------------------------------------------------------
# 三、重置路径不得残留操作者
# ---------------------------------------------------------------------------
def test_clear_all_confirmation_status_also_clears_weekly_operator(env):
    """清空确认状态时，周版本缓存的操作者必须和状态一起清掉。

    修复前只重置了 overall_status / confirmation_status，漏了 status_changed_by；
    而且过滤条件是 `overall_status != 'pending'`，于是「状态已回到待确认、操作者还
    留着」的行整行被跳过 —— 清空之后界面上仍显示着上一个确认人。
    """
    rejected_path = 'config/sync/clear_rejected.xlsx'
    base1 = _add_commit(
        env, rejected_path, 'base000007', datetime(2026, 3, 1, 10, 0, 0),
        status='confirmed', operator='alice',
    )
    c1 = _add_commit(
        env, rejected_path, 'win0000015', datetime(2026, 3, 2, 10, 0, 0),
        status='rejected', operator='bob',
    )
    cache1 = _add_cache(
        env, rejected_path, base1, c1, 1, overall='rejected',
        operator='bob', confirmation=json.dumps({'dev': 'rejected'}),
    )

    # 「新同步重置」后的残留形态：状态已回 pending、操作者没清
    residual_path = 'config/sync/clear_residual.xlsx'
    base2 = _add_commit(
        env, residual_path, 'base000008', datetime(2026, 3, 1, 10, 0, 0), status='confirmed',
    )
    c2 = _add_commit(
        env, residual_path, 'win0000016', datetime(2026, 3, 2, 10, 0, 0), status='pending',
    )
    cache2 = _add_cache(
        env, residual_path, base2, c2, 1, overall='pending',
        operator='carol', confirmation=json.dumps({'dev': 'confirmed'}),
    )

    result = StatusSyncService(env.db).clear_all_confirmation_status()
    assert result['success'] is True

    cache1 = _reload(env, cache1.id)
    cache2 = _reload(env, cache2.id)
    assert cache1.overall_status == 'pending'
    assert cache1.status_changed_by is None, (
        f'清空后周版本仍残留操作者 {cache1.status_changed_by!r}'
    )
    assert cache2.status_changed_by is None, (
        f'状态已是 pending、操作者残留为 {cache2.status_changed_by!r} 的行被整行跳过了'
    )
    assert json.loads(cache2.confirmation_status or '{}').get('dev') == 'pending'

    c1 = env.db.session.get(Commit, c1.id)
    assert c1.status == 'pending' and c1.status_changed_by is None


def test_weekly_resync_reset_clears_operator(env, monkeypatch):
    """周版本重新同步（latest_commit 变了）重置确认状态时，操作者必须一起清掉。

    修复前只重置了 confirmation_status / overall_status，status_changed_by 留着
    上一次的确认人 → 界面在「待确认」上显示着上一个操作者。
    """
    path = 'config/sync/resync_reset.lua'
    base = _add_commit(
        env, path, 'base000009', datetime(2026, 3, 1, 10, 0, 0), status='confirmed',
    )
    c1 = _add_commit(
        env, path, 'win0000017', datetime(2026, 3, 3, 10, 0, 0),
        status='confirmed', operator='alice',
    )
    cache = _add_cache(
        env, path, base, c1, 1, overall='confirmed',
        operator='alice', confirmation=json.dumps({'dev': 'confirmed'}),
    )
    # 时间戳故意设成很久以前：重置路径不再显式写 updated_at（列自带 onupdate），
    # 下面断言它仍被刷新 —— 否则「删掉冗余赋值」就成了静默的行为变更。
    cache.updated_at = datetime(2020, 1, 1, 0, 0, 0)
    env.db.session.commit()
    c2 = _add_commit(env, path, 'win0000018', datetime(2026, 3, 5, 10, 0, 0))

    monkeypatch.setattr(
        weekly_logic, '_generate_merged_diff_data',
        lambda *_args, **_kwargs: {'diff': 'updated'},
    )
    monkeypatch.setattr(
        weekly_logic, '_weekly_excel_cache_service', _StubWeeklyExcelCacheService(),
    )

    config = env.db.session.get(WeeklyVersionConfig, env.config_id)
    weekly_logic.generate_weekly_merged_diff(config, path, [c1, c2])

    cache = _reload(env, cache.id)
    assert cache.overall_status == 'pending'
    assert cache.status_changed_by is None, (
        f'重新同步后状态回到待确认，操作者却仍是 {cache.status_changed_by!r}'
    )
    assert cache.updated_at is not None and cache.updated_at > datetime(2026, 1, 1), (
        f'重新同步后 updated_at 没被刷新（{cache.updated_at}）—— 列的 onupdate 未生效'
    )
