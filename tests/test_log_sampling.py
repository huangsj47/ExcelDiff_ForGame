# -*- coding: utf-8 -*-
"""任务 I：热路径日志的采样与汇总。

## 为什么需要这个

复测文档 7.1 实测：一次同步的日志写到 3,826 行时，其中 **1,003 + 1,005 = 2,008 行**
只是「检查本地路径 / 路径是否存在」——占全文 **52.5%**。这两行在
`services/vcs_content_service.py::get_file_content_from_git` 里**按文件**打印，
条数与仓库文件数成正比；而 `log_print` 每次都要 open/append/close 一次
`logs/runlog.log`，所以它同时是噪音和同步 I/O。

## 这个文件钉住什么

改动方向是「采样 + 汇总」，**不是删日志**。所以三条必须同时成立：

1. 同一批上千个路径**不再刷屏**（`TestHotPathDoesNotFlood`）；
2. **计数仍然回答得了**「扫了多少个、多少个不存在」——汇总行里要看得见
   （`TestSummaryCountsSurvive`）。这一条是这次改动最容易做丢的：把日志删干净
   也能让第 1 条变绿，但诊断能力就没了；
3. `LOG_SAMPLE_MODE=all` 时逐条全打，排查时还能拿回原始明细
   （`TestDebugSwitch`）。

另外钉住「按 `job_id/run_id/stage` 定位」不下滑：采样后的行与结构化事件都要
带上这四个字段（`TestDiagnosticContextIsQueryable`）。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from services import log_sampling  # noqa: E402


@pytest.fixture()
def captured(monkeypatch):
    """把采样器打出来的行截下来（不落到 logs/runlog.log）。

    **要连带堵住真日志的几条入口。** 只桩 `log_sampling.log_print` 的话，
    `vcs_content_service` / `git_service` 里那句 `from utils.logger import log_print`
    绑定的是各自模块里的另一个名字：一旦热路径改回无条件打印，那些行会直接落到
    `logs/runlog.log` 而**不会**出现在 `captured` 里 —— 断言照绿，回归照漏
    （这个洞是变异验证试出来的：把热路径改回去，只有静态那条断言红了）。
    """
    lines: list[tuple[str, str]] = []

    def _fake_log_print(message, log_type='INFO', force=False):
        lines.append((log_type, message))

    monkeypatch.setattr(log_sampling, 'log_print', _fake_log_print)
    for module_path in ('services.vcs_content_service', 'services.weekly_file_sync',
                        'utils.logger'):
        module = sys.modules.get(module_path) or __import__(module_path, fromlist=['x'])
        if hasattr(module, 'log_print'):
            monkeypatch.setattr(module, 'log_print', _fake_log_print)
    safe_print_module = sys.modules.get('utils.safe_print') or __import__('utils.safe_print', fromlist=['x'])
    monkeypatch.setattr(safe_print_module, 'log_print', _fake_log_print)
    monkeypatch.setattr(safe_print_module, 'safe_print', _fake_log_print)
    log_sampling.reset_log_sampling()
    yield lines
    log_sampling.reset_log_sampling()


def _messages(lines):
    return [message for _type, message in lines]


class TestHotPathDoesNotFlood:
    def test_a_thousand_paths_do_not_produce_a_thousand_lines(self, captured):
        """1000 个文件 → 日志必须收敛到一个常数级行数。

        原来这里是 1000 × 2 = 2000 行。
        """
        with log_sampling.log_sampling_scope('weekly_diff', flush_every=200):
            for index in range(1000):
                log_sampling.log_sampled(
                    'vcs.git_local_path',
                    '检查本地路径',
                    f'检查本地路径: /repos/demo/code/file_{index}.lua（存在=True）',
                    outcome=log_sampling.OUTCOME_HIT,
                    log_type='GIT',
                )

        assert len(captured) <= 12, (
            f'1000 个路径还是打出了 {len(captured)} 行，没有收敛：\n'
            + '\n'.join(_messages(captured)[:20])
        )
        # 但「扫过」这件事必须留痕：首条 + 汇总。
        assert any('检查本地路径: /repos/demo/code/file_0.lua' in m for m in _messages(captured))

    def test_the_real_hot_path_collapses(self, captured, monkeypatch, tmp_path):
        """对**真的那个调用点**计数：`get_file_content_from_git` 每次两条 → 一条采样记录。

        这条用真实函数而不是采样器 API：把源头改回去（恢复那两行 log_print）
        这一条就会红。
        """
        from services import vcs_content_service

        worktree = tmp_path / 'repos' / 'demo'
        worktree.mkdir(parents=True)

        class _FakeBlob:
            data_stream = SimpleNamespace(read=lambda: b'x')

        class _FakeTree(dict):
            def __getitem__(self, key):
                return _FakeBlob()

        class _FakeCommit:
            tree = _FakeTree()

        class _FakeRepo:
            def __init__(self, _path):
                pass

            def commit(self, _commit_id):
                return _FakeCommit()

        import git as git_module

        monkeypatch.setattr(git_module, 'Repo', _FakeRepo)
        monkeypatch.setattr(
            vcs_content_service, 'get_git_service',
            lambda _repository: SimpleNamespace(local_path=str(worktree)),
        )

        repository = SimpleNamespace(id=1, name='demo')

        with log_sampling.log_sampling_scope('weekly_diff', flush_every=1000):
            for index in range(300):
                content = vcs_content_service.get_file_content_from_git(
                    repository, 'a' * 40, f'code/file_{index}.lua',
                )
                assert content == b'x'

        path_lines = [
            m for m in _messages(captured)
            if '检查本地路径' in m or '路径是否存在' in m
        ]
        assert len(path_lines) <= 6, (
            f'300 个文件打出了 {len(path_lines)} 行路径日志，热路径没被采样：\n'
            + '\n'.join(path_lines[:20])
        )

    def test_the_flood_messages_are_no_longer_unconditional_in_the_source(self):
        """源头自检：那两行不允许再以「无条件 log_print」的形态存在。

        静态断言**先剥注释**——本项目里注释会原样引用要禁掉的写法
        （`.claude/rules` 与复测文档都在引 `log_print(f"检查本地路径`）。
        """
        for relative in ('services/vcs_content_service.py', 'services/git_service.py'):
            source = _strip_comments(os.path.join(ROOT_DIR, relative))
            assert 'log_print(f"检查本地路径' not in source, (
                f'{relative} 里「检查本地路径」又变回无条件 log_print 了'
            )
            assert "log_print(f'检查本地路径" not in source, (
                f'{relative} 里「检查本地路径」又变回无条件 log_print 了'
            )
            assert 'log_print(f"路径是否存在' not in source, (
                f'{relative} 里「路径是否存在」又变回无条件 log_print 了'
            )


class TestSummaryCountsSurvive:
    def test_the_summary_answers_how_many_and_how_many_missing(self, captured):
        """汇总行必须同时给出**总数**与**命中/未命中**。

        「扫了多少个文件、有多少不存在」是原来那 2008 行在回答的问题；
        汇总行要是只写「检查本地路径: 1000 次」，这一条就退化了。
        """
        with log_sampling.log_sampling_scope('weekly_diff'):
            for index in range(7):
                log_sampling.log_sampled(
                    'vcs.git_local_path', '检查本地路径',
                    f'检查本地路径: /repos/demo/f{index}',
                    outcome=log_sampling.OUTCOME_HIT, log_type='GIT',
                )
            for index in range(3):
                log_sampling.log_sampled(
                    'vcs.git_local_path', '检查本地路径',
                    f'检查本地路径: /repos/demo/missing{index}',
                    outcome=log_sampling.OUTCOME_MISS, log_type='GIT',
                )

        summaries = [m for m in _messages(captured) if '处理 10 次' in m]
        assert summaries, f'没有汇总行：{_messages(captured)}'
        assert '7 次命中' in summaries[-1], summaries[-1]
        assert '3 次未命中' in summaries[-1], summaries[-1]

    def test_the_last_sample_is_kept(self, captured):
        """最后一条要留下来——排查时先看的是「最后卡在哪个文件」。"""
        with log_sampling.log_sampling_scope('weekly_diff'):
            for index in range(50):
                log_sampling.log_sampled(
                    'vcs.git_local_path', '检查本地路径',
                    f'检查本地路径: /repos/demo/f{index}',
                    outcome=log_sampling.OUTCOME_HIT, log_type='GIT',
                )

        assert any('f49' in m for m in _messages(captured)), (
            f'最后一条样本被采样掉了，无法回答「卡在哪个文件」：{_messages(captured)}'
        )

    def test_outcomes_are_counted_by_label(self, captured):
        """没有命中/未命中语义的类目，仍要给出总次数（不能凭空造 0 次命中）。"""
        with log_sampling.log_sampling_scope('sync'):
            for index in range(5):
                log_sampling.log_sampled(
                    'git.command', '执行Git命令',
                    f'🔧 执行Git命令: git status ({index})', log_type='GIT',
                )

        summaries = [m for m in _messages(captured) if '处理 5 次' in m]
        assert summaries, f'没有汇总行：{_messages(captured)}'
        assert '命中' not in summaries[-1]

    def test_two_groups_are_summarised_separately(self, captured):
        with log_sampling.log_sampling_scope('sync'):
            for index in range(4):
                log_sampling.log_sampled('a', 'A 类', f'A{index}', log_type='GIT')
            for index in range(6):
                log_sampling.log_sampled('b', 'B 类', f'B{index}', log_type='GIT')

        totals = [m for m in _messages(captured) if '处理' in m]
        assert any('A 类: 处理 4 次' in m for m in totals), totals
        assert any('B 类: 处理 6 次' in m for m in totals), totals


class TestDebugSwitch:
    def test_all_mode_prints_every_line(self, captured, monkeypatch):
        monkeypatch.setenv(log_sampling.LOG_SAMPLE_MODE_ENV, 'all')
        with log_sampling.log_sampling_scope('sync'):
            for index in range(60):
                log_sampling.log_sampled(
                    'vcs.git_local_path', '检查本地路径',
                    f'检查本地路径: /repos/demo/f{index}',
                    outcome=log_sampling.OUTCOME_HIT, log_type='GIT',
                )

        detail = [m for m in _messages(captured) if '检查本地路径: /repos/demo/f' in m]
        assert len(detail) == 60, f'all 模式应当逐条全打，实际 {len(detail)} 条'

    def test_all_mode_still_summarises(self, captured, monkeypatch):
        """放开明细不等于丢掉计数：汇总仍是回答「扫了多少」的那一行。"""
        monkeypatch.setenv(log_sampling.LOG_SAMPLE_MODE_ENV, 'all')
        with log_sampling.log_sampling_scope('sync'):
            for index in range(60):
                log_sampling.log_sampled(
                    'vcs.git_local_path', '检查本地路径',
                    f'检查本地路径: /repos/demo/f{index}',
                    outcome=log_sampling.OUTCOME_MISS, log_type='GIT',
                )
        assert any('处理 60 次' in m and '60 次未命中' in m for m in _messages(captured))

    def test_off_mode_keeps_only_the_summary(self, captured, monkeypatch):
        monkeypatch.setenv(log_sampling.LOG_SAMPLE_MODE_ENV, 'off')
        with log_sampling.log_sampling_scope('sync'):
            for index in range(30):
                log_sampling.log_sampled(
                    'vcs.git_local_path', '检查本地路径',
                    f'检查本地路径: /repos/demo/f{index}',
                    outcome=log_sampling.OUTCOME_HIT, log_type='GIT',
                )
        detail = [m for m in _messages(captured) if m.startswith('检查本地路径: /repos')]
        assert detail == [], f'off 模式不该有明细：{detail}'
        assert any('处理 30 次' in m for m in _messages(captured))

    def test_the_default_is_aggregate(self, captured, monkeypatch):
        monkeypatch.delenv(log_sampling.LOG_SAMPLE_MODE_ENV, raising=False)
        with log_sampling.log_sampling_scope('sync'):
            for index in range(30):
                log_sampling.log_sampled(
                    'vcs.git_local_path', '检查本地路径',
                    f'检查本地路径: /repos/demo/f{index}',
                    outcome=log_sampling.OUTCOME_HIT, log_type='GIT',
                )
        detail = [m for m in _messages(captured) if m.startswith('检查本地路径: /repos')]
        assert len(detail) <= 2, f'默认（聚合）模式不该逐条打：{len(detail)} 条'
        assert any('处理 30 次' in m for m in _messages(captured))


class TestUnscopedCallsStillReport:
    def test_a_long_run_without_an_explicit_scope_still_flushes(self, captured):
        """没有作用域时（生产里真正发生的那种调用）不能把计数憋在内存里。

        周版本同步那条链路上没有人会去开作用域。如果只有开了作用域才汇总，
        这次改动就等于「把日志删了」——原来那 2008 行至少在回答「扫了多少」。
        """
        for index in range(500):
            log_sampling.log_sampled(
                'vcs.git_local_path', '检查本地路径',
                f'检查本地路径: /repos/demo/f{index}',
                outcome=log_sampling.OUTCOME_HIT, log_type='GIT',
            )

        summaries = [m for m in _messages(captured) if '检查本地路径: 处理' in m]
        assert summaries, f'没有作用域时一条汇总都没有：{len(captured)} 行'
        assert len(captured) <= 20, f'没有作用域时又刷屏了：{len(captured)} 行'
        assert any('处理 400 次' in m for m in summaries), (
            f'滚动汇总没打出来（应当每 {log_sampling.DEFAULT_FLUSH_EVERY} 条一次）：{summaries}'
        )

    def test_the_explicit_scope_is_the_exact_way(self, captured):
        """要**精确**总数就开作用域：作用域收口时给的是最终值。

        这一条与上一条一起说明口径：滚动汇总给的是「至少扫了这么多」，
        收口汇总给的是「一共扫了这么多」。
        """
        with log_sampling.log_sampling_scope('weekly_diff', flush_every=1000):
            for index in range(500):
                log_sampling.log_sampled(
                    'vcs.git_local_path', '检查本地路径',
                    f'检查本地路径: /repos/demo/f{index}',
                    outcome=log_sampling.OUTCOME_HIT, log_type='GIT',
                )

        assert any('检查本地路径: 处理 500 次' in m for m in _messages(captured))


class TestDiagnosticContextIsQueryable:
    def test_sampled_lines_carry_job_run_and_stage(self, captured):
        with log_sampling.bind_diagnostics(job_id=17, run_id=20, stage='weekly_diff'):
            with log_sampling.log_sampling_scope('weekly_diff'):
                for index in range(5):
                    log_sampling.log_sampled(
                        'vcs.git_local_path', '检查本地路径',
                        f'检查本地路径: /repos/demo/f{index}',
                        outcome=log_sampling.OUTCOME_HIT, log_type='GIT',
                    )

        joined = '\n'.join(_messages(captured))
        assert 'job_id=17' in joined, joined
        assert 'run_id=20' in joined, joined
        assert 'stage=weekly_diff' in joined, joined

    def test_per_file_timing_lands_in_the_performance_panel_with_the_context(self, monkeypatch):
        """**耗时**的落点是性能面板，不是文字日志 —— 按 job/run/stage 查就靠这里。

        文字行已经被采样收敛成一个计数，再想从日志里 grep「某个 job 的某一阶段花了
        多久」是 grep 不到的；真正逐文件的耗时一直在 `perf_metrics_service.record`
        里（`total_ms`/`read_ms`/`diff_ms` + file_path），这一次改动把诊断上下文也
        并进了它的 tags。
        """
        from types import SimpleNamespace

        from services import vcs_content_service

        recorded = []
        fake_commit = SimpleNamespace(
            commit_id='a' * 40, path='code/x.lua', operation='M',
            commit_time=None, repository=None,
            repository_id=1,
        )
        project = SimpleNamespace(code='DEMO')
        repository = SimpleNamespace(id=1, project_id=9, project=project, type='git')
        fake_commit.repository = repository

        monkeypatch.setattr(
            vcs_content_service, 'get_perf_metrics_service',
            lambda: SimpleNamespace(record=lambda *a, **kw: recorded.append((a, kw))),
        )
        monkeypatch.setattr(
            vcs_content_service.ExcelDiffCacheService if hasattr(vcs_content_service, 'ExcelDiffCacheService')
            else vcs_content_service, '_noop', lambda: None, raising=False,
        )
        monkeypatch.setattr(
            vcs_content_service, 'get_file_content_from_git',
            lambda _repo, _commit, _path: b'content',
        )

        class _FakeDiffService:
            def process_diff(self, *_args, **_kwargs):
                return {'type': 'text', 'sections': []}

        import services.diff_service as diff_service_module

        monkeypatch.setattr(diff_service_module, 'DiffService', _FakeDiffService)
        monkeypatch.setattr(
            vcs_content_service, '_collect_excel_metrics',
            lambda _data: {'sheet_count': 0, 'changed_rows': 0, 'summary': {}},
        )

        rows = []
        monkeypatch.setattr(
            log_sampling, 'log_print',
            lambda message, log_type='INFO', force=False: rows.append((log_type, message)),
        )
        log_sampling.reset_log_sampling()
        try:
            with log_sampling.bind_diagnostics(job_id=17, run_id=20, stage='weekly_diff'):
                vcs_content_service.get_unified_diff_data(fake_commit)
        finally:
            log_sampling.reset_log_sampling()

        assert recorded, '逐文件的耗时没有落到性能面板'
        tags = recorded[0][1].get('tags') or {}
        assert tags.get('job_id') == 17, tags
        assert tags.get('run_id') == 20, tags
        assert tags.get('stage') == 'weekly_diff', tags
        metrics = recorded[0][1].get('metrics') or {}
        assert 'total_ms' in metrics, metrics

    def test_stage_events_merge_the_ambient_context(self, captured, monkeypatch):
        """结构化事件是「按 job/run/stage 查耗时/失败」的那条路。"""
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            log_sampling, 'log_structured_event',
            lambda event, **fields: events.append((event, fields)),
        )

        with log_sampling.bind_diagnostics(job_id=17, run_id=20, stage='weekly_diff'):
            log_sampling.log_stage_event(
                'git_command', duration_ms=12.5, returncode=128, failed=True,
            )

        assert events, '没有打出结构化事件'
        event, fields = events[-1]
        assert event == 'git_command'
        assert fields['job_id'] == 17
        assert fields['run_id'] == 20
        assert fields['stage'] == 'weekly_diff'
        assert fields['duration_ms'] == 12.5
        assert fields['failed'] is True

    def test_the_context_is_scoped_and_does_not_leak(self, captured):
        with log_sampling.bind_diagnostics(job_id=1, run_id=2, stage='a'):
            assert log_sampling.current_diagnostics()['stage'] == 'a'
        assert log_sampling.current_diagnostics() == {}


def _capture_bare_print(monkeypatch, captured):
    """把裸 `print` 也接进 `captured`，返回同一个列表。

    任务 B 的两个文件里还有没改的裸 `print`（`_collect_previous_commits_threaded`
    末尾那几行汇总、几条错误行），它们在生产里同样落 `runlog.log`
    （`utils/logger` 启动时把 `builtins.print` 换成了 `safe_log_print`）。「这次还剩
    多少行」必须把它们算进去，否则断言等于在数一个被过滤过的集合。

    **只能在测试函数体内打这个桩**：`tests/conftest.py::pytest_runtest_call` 会在每个
    测试的函数体之前把 `builtins.print` 强制恢复成原始对象（那是防 `app.py` 模块级
    副作用的），写在 fixture 里会被它盖掉 —— 症状是「断言拿到空列表，而 print 明明
    打到了屏幕上」。
    """
    import builtins

    def _fake_print(*args, **_kwargs):
        captured.append(('STDOUT', ' '.join(str(arg) for arg in args)))

    monkeypatch.setattr(builtins, 'print', _fake_print)
    return captured


class TestTaskBThreadedAndWeeklyPaths:
    """任务 B：`threaded_git_service` 与 `weekly_file_sync` 上剩下的那 816 行。

    这两条链路的形状和任务 I 不一样，各有一个坑：

    * `_collect_previous_commits_threaded` 的逐文件行是在 **worker 线程**里打的，
      而采样作用域是**线程本地**的。worker 里调 `log_sampled` 只会拿到那个线程自己的
      隐式采样器 —— 6 个 worker 就是 6 份「首条 + 汇总」，而且计数被切成 6 份，
      「这次扫了多少个文件」谁也答不出来。所以采样器由**父线程建、交给 worker 用**，
      末尾也由父线程 flush。
    * `weekly_file_sync.get_real_base_commit_from_vcs` 是**按文件调用**的单线程路径，
      没有现成的作用域（开作用域那段归 task_worker 的负责人）。它的收口点是
      `describe_weekly_file_totals` —— 每个同步**恰好调一次**，且在同一个线程里，
      所以那里 flush 一次就能把这一轮的账结清，不必去动别人的文件。
    """

    FILE_COUNT = 30

    @classmethod
    def _git_repo(cls, path):
        """真的建一个仓库：`git log --follow` 的空结果、`iter_commits(paths=...)`
        的行为都不是能靠假对象糊过去的。

        两次提交的时间**刻意差一周**：同刻提交会让「窗口前最后一个提交」变成
        `iter_commits` 的行序问题（本仓库真出过那个 bug），这里要的是确定性。
        日期用 git CLI + 环境变量写死 —— `index.commit(commit_date=...)` 只吃 git
        自己的格式，`repo.git.update_environment()` 在 `index.commit` 上又不生效
        （实测两次提交的 `committed_date` 都还是「现在」）。
        """
        import subprocess

        import git

        names = [f'config/file_{index}.xlsx' for index in range(cls.FILE_COUNT)]
        repo = git.Repo.init(str(path))
        with repo.config_writer() as writer:
            writer.set_value('user', 'name', 'log-sampling')
            writer.set_value('user', 'email', 'log-sampling@example.com')
        (path / 'config').mkdir(parents=True, exist_ok=True)

        def _commit(message, stamp):
            subprocess.run(['git', 'add', '-A'], cwd=str(path), check=True, capture_output=True)
            subprocess.run(
                ['git', 'commit', '-m', message], cwd=str(path), check=True, capture_output=True,
                env={**os.environ, 'GIT_AUTHOR_DATE': stamp, 'GIT_COMMITTER_DATE': stamp},
            )

        for name in names:
            (path / name).write_text('v1\n', encoding='utf-8')
        _commit('first', cls.FIRST_DATE)
        first = repo.commit('HEAD')
        for name in names:
            (path / name).write_text('v2\n', encoding='utf-8')
        _commit('second', cls.SECOND_DATE)
        second = repo.commit('HEAD')
        return repo, names, first, second

    FIRST_DATE = '2026-01-01T00:00:00+0000'
    SECOND_DATE = '2026-01-08T00:00:00+0000'

    @staticmethod
    def _threaded_service(local_path=None):
        """只借那两个方法用：`__init__` 会去解析仓库 URL，这套测试不需要那一段。"""
        from services.threaded_git_service import ThreadedGitService

        service = ThreadedGitService.__new__(ThreadedGitService)
        service.max_workers = 6
        service.local_path = local_path
        return service

    @staticmethod
    def _commit_dicts(names, latest, extra=None):
        from datetime import datetime, timezone

        when = datetime.fromtimestamp(latest.committed_date, tz=timezone.utc)
        rows = [{'path': name, 'commit_id': latest.hexsha, 'version': latest.hexsha[:8],
                 'operation': 'M', 'commit_time': when, 'message': 'x'}
                for name in names]
        if extra:
            rows.append({'path': extra, 'commit_id': 'f' * 40, 'version': 'f' * 8,
                         'operation': 'M', 'commit_time': when, 'message': 'x'})
        return rows

    def test_the_threaded_prev_commit_scan_collapses(self, captured, monkeypatch, tmp_path):
        """31 个文件（含 1 个仓库里不存在的路径）→ 常数级行数，计数不许被线程切碎。"""
        lines = _capture_bare_print(monkeypatch, captured)
        repo, names, _first, second = self._git_repo(tmp_path / 'work')
        service = self._threaded_service()
        commits = self._commit_dicts(names, second, extra='config/never_existed.xlsx')

        merged = service._collect_previous_commits_threaded(repo, commits)

        # 30 个文件各自找到了一条更早的提交（第 31 个找不到）—— 先把「事实」钉住，
        # 后面的计数才有意义。
        assert len(merged) == len(commits) + self.FILE_COUNT, len(merged)
        messages = _messages(lines)
        assert len(lines) <= 16, (
            f'31 个文件打出了 {len(lines)} 行，没有收敛：\n' + '\n'.join(messages[:25])
        )
        assert any('处理 31 次' in m and '30 次命中' in m and '1 次未命中' in m for m in messages), messages
        # 汇总只能有一条：6 个 worker 各打一份就不能说「这次扫了 31 个」
        assert sum(1 for m in messages if '完成处理文件' in m and '处理 31 次' in m) == 1, messages

    def test_the_threaded_scan_with_no_files_is_not_reported_as_a_failure(self, captured, monkeypatch, tmp_path):
        """空输入不许打「多线程收集前一次提交失败」。

        原先末尾那句 `processing_time/len(files_commits)` 在空输入上除零，异常被外层
        接住 → 日志里出现一句「失败」+「降级到串行处理」，而其实什么都没发生。
        """
        lines = _capture_bare_print(monkeypatch, captured)
        repo, _names, _first, _second = self._git_repo(tmp_path / 'empty')
        service = self._threaded_service()

        assert service._collect_previous_commits_threaded(repo, []) == []

        joined = '\n'.join(_messages(lines))
        assert '多线程收集前一次提交失败' not in joined, joined
        assert '降级到串行处理' not in joined, joined

    def test_the_file_history_lookup_counts_hits_and_misses(self, captured, monkeypatch, tmp_path):
        """`get_file_commit_history` 逐文件调用 → 汇总行里要看得见「查了几次、几次有」。"""
        from services import weekly_file_sync

        lines = _capture_bare_print(monkeypatch, captured)
        repo, names, _first, _second = self._git_repo(tmp_path / 'hist')
        service = self._threaded_service(local_path=str(repo.working_dir))

        for name in names:
            assert service.get_file_commit_history(name), name
        assert service.get_file_commit_history('config/never_existed.xlsx') == []

        # 真实收口点：每次同步末尾那一行汇总（同时也是采样计数的收口）。
        weekly_file_sync.describe_weekly_file_totals({'updated': len(names)}, len(names))

        messages = _messages(lines)
        assert any(
            '处理 31 次' in m and '30 次命中' in m and '1 次未命中' in m for m in messages
        ), messages

    def test_the_weekly_base_lookup_counts_every_exit(self, captured, monkeypatch, tmp_path):
        """`get_real_base_commit_from_vcs` 的三条出口各记一笔，收口点在同步末尾。"""
        from datetime import datetime, timedelta, timezone

        from services import weekly_file_sync

        lines = _capture_bare_print(monkeypatch, captured)
        repo, names, first, second = self._git_repo(tmp_path / 'weekly')
        service = self._threaded_service(local_path=str(repo.working_dir))
        earliest = min(first.committed_date, second.committed_date)
        earliest_utc = datetime.fromtimestamp(earliest, tz=timezone.utc)

        class _FakeCommitQuery:
            def filter_by(self, **_kwargs):
                return self

            def first(self):
                return None

        class _FakeCommitModel:
            """不落库：这条用例测的是日志，库那一侧由别的文件管。"""

            # 库里没有这个基准 → 走「创建新的基准提交记录」那条路（也是要计数的出口之一）
            query = _FakeCommitQuery()

            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        monkeypatch.setattr(weekly_file_sync, 'Commit', _FakeCommitModel)
        monkeypatch.setattr(
            weekly_file_sync, 'db',
            SimpleNamespace(session=SimpleNamespace(add=lambda _row: None, commit=lambda: None)),
        )
        import services.threaded_git_service as threaded_module

        monkeypatch.setattr(threaded_module, 'ThreadedGitService', lambda *_a, **_k: service)

        def _config(start_utc_naive):
            # config.start_time 是**北京墙钟**，窗口由 weekly_window_in_utc 换算成 UTC。
            return SimpleNamespace(
                start_time=start_utc_naive.replace(tzinfo=None) + timedelta(hours=8),
                end_time=start_utc_naive.replace(tzinfo=None) + timedelta(days=1, hours=8),
                repository=SimpleNamespace(
                    type='git', url='https://example.com/r.git', root_directory=None,
                    username=None, token=None, id=7,
                ),
            )

        # ① 窗口起点早于两条提交 → 该文件在窗口前没有基准（📭 未找到周版本开始前的提交）
        assert weekly_file_sync.get_real_base_commit_from_vcs(
            _config(earliest_utc - timedelta(days=1)), names[0]) is None
        # ② 窗口起点落在两条提交之间（1/1 之后、1/8 之前）→ 命中，回查到 1/1 那条
        hit = weekly_file_sync.get_real_base_commit_from_vcs(
            _config(earliest_utc + timedelta(days=3)), names[1])
        assert hit is not None and hit.commit_id == first.hexsha
        # ③ 仓库里根本没有这个文件（📭 未找到文件…的提交历史）
        assert weekly_file_sync.get_real_base_commit_from_vcs(
            _config(earliest_utc + timedelta(days=3)), 'config/never_existed.xlsx') is None

        weekly_file_sync.describe_weekly_file_totals({'updated': 3}, 3)

        messages = _messages(lines)
        # 断言要带 `GIT` 前缀：`ThreadedGitService.get_file_commit_history` 那一组的标签
        # 也叫「获取文件提交历史」，少一个前缀就会被它蒙混过去（实测过一次）。
        assert any('GIT获取文件提交历史: 处理 3 次' in m for m in messages), messages
        assert any('1 次未命中' in m and '未找到文件提交历史' in m for m in messages), messages
        assert any('1 次未命中' in m and '未找到周版本开始前的提交' in m for m in messages), messages
        assert len(lines) <= 12, (
            f'3 个文件打出了 {len(lines)} 行：\n' + '\n'.join(messages)
        )


def _strip_comments(path: str) -> str:
    """把注释原地抹成空格 —— 静态断言前必须先剥注释。

    只抹 `tokenize` 认定的 COMMENT 段、且**保持原有行列偏移**：字符串（含 f-string）
    一律原样保留，否则「要禁的写法在字符串里」就匹配不到了。
    """
    import io
    import tokenize

    with io.open(path, 'r', encoding='utf-8') as handle:
        source = handle.read()

    lines = source.splitlines(keepends=True)
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            (start_row, start_col), (_end_row, end_col) = token.start, token.end
            line = lines[start_row - 1]
            lines[start_row - 1] = (
                line[:start_col] + ' ' * (end_col - start_col) + line[end_col:]
            )
    except (tokenize.TokenError, IndexError, SyntaxError):
        return source
    return ''.join(lines)

