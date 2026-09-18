# -*- coding: utf-8 -*-
"""两个「日志刷屏」的源头。

## 报障形态

本地日志被同一个仓库反复刷满：

    🔧 工作目录: …\\repos\\Ta69ee7_r10d76f_4
    Git命令执行失败: [WinError 267] 目录名称无效。
    ⚠️ [RESET] git clean -fd 失败
    … git gc --prune=now 失败
    📝 已记录仓库 r10d76f 的同步错误: 同步失败: … fatal: repository 'https://example.com/x.git/' not found

两分钟一轮，永远刷下去。两个独立的毛病叠在一起：

1. **重置一个不存在的目录**：`_reset_repository_to_head` 无条件跑
   `git reset/clean/gc` —— 工作目录根本没克隆成功时，三条命令各失败一次（每条两行
   日志），而这些命令本来就没有东西可重置。
2. **失败的仓库每 2 分钟被重新调度**：`schedule_repository_sync_tasks` 只按
   `clone_status='completed'` 筛选，而失败分支只在「手动重试策略 = 重克隆」时才把
   clone_status 置成 failed —— 地址写错/仓库被删的仓库会一直留在 completed 里，
   被定时器每 2 分钟撞一次墙。

两条都修：目录不存在就跳过重置（说一句就够）；近期失败过的仓库进入退避窗口，
窗口内不再自动调度（**手动重试不受影响**，它不走调度器）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import services.task_worker_service as worker  # noqa: E402


def _fake_git_service(local_path):
    calls = []

    def _run(command, timeout=None):
        calls.append(command)
        raise AssertionError('工作目录不存在时不该真的去跑 git')

    return SimpleNamespace(local_path=local_path, _run_git_command=_run), calls


class TestResetSkipsAMissingWorktree:
    def test_a_missing_worktree_is_not_reset(self, tmp_path):
        missing = str(tmp_path / 'not-cloned-yet')
        git_service, calls = _fake_git_service(missing)
        repository = SimpleNamespace(id=4, name='r10d76f')

        worker._reset_repository_to_head(git_service, repository)

        assert calls == [], (
            f'工作目录不存在却还是跑了 git 命令（每条都会以「目录名称无效」失败并刷屏）：{calls}'
        )

    def test_an_existing_worktree_is_still_reset(self, tmp_path):
        """反向自检：目录真在的时候，重置一步都不能少。"""
        existing = tmp_path / 'repo'
        existing.mkdir()
        runs = []

        def _run(command, timeout=None):
            runs.append(command)
            return SimpleNamespace(returncode=0)

        git_service = SimpleNamespace(local_path=str(existing), _run_git_command=_run)
        worker._reset_repository_to_head(git_service, SimpleNamespace(id=1, name='demo'))

        assert ['git', 'reset', '--hard', 'HEAD'] in runs
        assert ['git', 'clean', '-fd'] in runs
        assert ['git', 'gc', '--prune=now'] in runs


class TestFailedRepositoriesBackOff:
    def _repository(self, error, minutes_ago):
        if error is None:
            return SimpleNamespace(id=1, name='demo', last_sync_error=None, last_sync_error_time=None)
        return SimpleNamespace(
            id=1, name='demo', last_sync_error=error,
            last_sync_error_time=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        )

    def test_a_recent_failure_is_skipped(self):
        assert worker._sync_failure_backoff_active(self._repository('clone failed', 1)) is True
        assert worker._sync_failure_backoff_active(self._repository('clone failed', 29)) is True

    def test_the_window_expires(self):
        """退避不是放弃：窗口过后照旧自动重试（瞬时故障要能自愈）。"""
        assert worker._sync_failure_backoff_active(self._repository('clone failed', 31)) is False
        assert worker._sync_failure_backoff_active(self._repository('clone failed', 600)) is False

    def test_a_healthy_repository_is_never_skipped(self):
        assert worker._sync_failure_backoff_active(self._repository(None, 0)) is False

    def test_an_error_without_a_timestamp_is_not_a_backoff(self):
        """库里可能只有错误文本、没有时间（历史数据）：不能因此永久停掉自动同步。"""
        repository = SimpleNamespace(id=1, name='demo', last_sync_error='boom',
                                     last_sync_error_time=None)
        assert worker._sync_failure_backoff_active(repository) is False

    def test_a_naive_timestamp_is_read_as_utc(self):
        """SQLite 取回来的 DateTime 不带时区 —— 按 UTC 解释，不能让比较炸掉。"""
        naive = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None)
        repository = SimpleNamespace(id=1, name='demo', last_sync_error='boom',
                                     last_sync_error_time=naive)
        assert worker._sync_failure_backoff_active(repository) is True

    def test_the_scheduler_skips_the_backed_off_repository(self, monkeypatch):
        """调度器那一层：退避窗口内的仓库不再被派 auto_sync 任务。"""
        repository = self._repository('clone failed', 5)
        created = []

        class _Query:
            def filter_by(self, **_kwargs):
                return self

            def all(self):
                return [repository]

        monkeypatch.setattr(worker, '_Repository', SimpleNamespace(query=_Query()))
        monkeypatch.setattr(worker, 'create_auto_sync_task', lambda repo_id: created.append(repo_id))
        monkeypatch.setattr(worker, '_app', _FakeApp(), raising=False)

        worker.schedule_repository_sync_tasks()

        assert created == [], f'退避窗口内还是派了同步任务：{created}'


class _FakeApp:
    def app_context(self):
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            yield

        return _ctx()
