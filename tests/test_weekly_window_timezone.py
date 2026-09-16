# -*- coding: utf-8 -*-
"""周版本时间窗口的时区口径：窗口是北京墙钟，commit_time 是 naive-UTC 墙钟。

## 为什么需要这个测试

`WeeklyVersionConfig.start_time/end_time` 是用户在 `<input type="datetime-local">`
里填的值，原样 `datetime.fromisoformat` 入库 → **naive 北京墙钟**。
`Commit.commit_time` 由 `datetime.fromtimestamp(ts, tz=timezone.utc)` 写入，
SQLite 绑定参数时丢弃 tzinfo → **naive-UTC 墙钟**。

修复前，SQL 里直接 `Commit.commit_time >= config.start_time`，两种墙钟相减等价于
**整体偏移 8 小时**，而且不报错、不抛异常。实测后果：

    窗口填 3/2 00:00 ~ 3/9 00:00（北京时间）
    实际按 3/2 08:00 ~ 3/9 08:00（北京时间）生效
      → 每周窗口【前 8 小时】的提交被静默丢弃
      → 基准版本被选成「窗口内第一个提交」而不是窗口前最后一个
      → 窗口首日的变更整体对审核者不可见

这是本平台最坏的失败方式：不是报错，而是**漏掉整段变更**。

## 这个测试怎么测

用**真实模型 + 真实（隔离的）SQLite 库**造三种提交，它们的真实北京时间分别是
窗口前、窗口头 30 分钟、窗口末尾 1 小时，然后用生产代码自己的
`weekly_window_in_utc(config)` 构造窗口查询，断言命中的正好是后两条。

刻意不依赖宿主机时区：所有时间都是写死的常量，判定的是「换算是否正确」，
而不是「宿主机是几点」—— 所以这个测试在 UTC 机器和 UTC+8 机器上结论一致。
"""
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.timezone_utils import (  # noqa: E402
    BEIJING_TZ,
    beijing_wallclock_to_utc_naive,
    utc_naive_to_beijing_wallclock,
)

# 窗口（用户在北京时间下填的）：2026-03-02 00:00 ~ 2026-03-09 00:00
WIN_START_BJ = datetime(2026, 3, 2, 0, 0, 0)
WIN_END_BJ = datetime(2026, 3, 9, 0, 0, 0)


def _utc_naive_for_beijing_wallclock(y, m, d, hh, mm):
    """给定「真实北京时间」，返回库里该存的 naive-UTC 墙钟（Commit.commit_time 的形态）。"""
    aware_bj = datetime(y, m, d, hh, mm, tzinfo=BEIJING_TZ)
    return aware_bj.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 一、换算函数的语义
# ---------------------------------------------------------------------------
class TestWallclockConversion:

    def test_beijing_midnight_maps_to_previous_day_16_utc(self):
        """北京时间 3/2 00:00 就是 UTC 3/1 16:00。"""
        assert beijing_wallclock_to_utc_naive(WIN_START_BJ) == datetime(2026, 3, 1, 16, 0, 0)

    def test_roundtrip(self):
        back = utc_naive_to_beijing_wallclock(beijing_wallclock_to_utc_naive(WIN_START_BJ))
        assert back == WIN_START_BJ

    def test_none_is_passed_through(self):
        assert beijing_wallclock_to_utc_naive(None) is None
        assert utc_naive_to_beijing_wallclock(None) is None

    def test_result_is_naive(self):
        """必须是 naive —— 库里的列是 naive，带 tzinfo 的值去比较会出问题。"""
        assert beijing_wallclock_to_utc_naive(WIN_START_BJ).tzinfo is None
        assert utc_naive_to_beijing_wallclock(WIN_START_BJ).tzinfo is None

    def test_aware_input_is_respected(self):
        """已经是 aware 的入参按其自身时区解释（不要再当成北京时间额外加 8 小时）。"""
        aware_utc = datetime(2026, 3, 1, 16, 0, 0, tzinfo=timezone.utc)
        assert beijing_wallclock_to_utc_naive(aware_utc) == datetime(2026, 3, 1, 16, 0, 0)

    def test_eight_hour_offset_is_exactly_eight_hours(self):
        """偏移量必须正好是 8 小时（这是本缺陷的全部内容，不该有别的差）。"""
        for hour in (0, 6, 12, 18, 23):
            naive_bj = datetime(2026, 3, 2, hour, 0, 0)
            assert (
                naive_bj - beijing_wallclock_to_utc_naive(naive_bj)
            ) == __import__('datetime').timedelta(hours=8)


# ---------------------------------------------------------------------------
# 二、端到端：窗口查询必须命中「窗口前 8 小时」那一段的提交
# ---------------------------------------------------------------------------
@pytest.fixture()
def seeded():
    """建一个项目/仓库/周版本配置 + 三条真实在北京时间不同位置的提交。

    每次调用用唯一的 project.code / 仓库名：conftest 的隔离库是整个会话共用的，
    固定 code 会撞 UNIQUE 约束。teardown 清掉自己造的行，避免污染其它测试。
    """
    import app as app_module
    from models import Project, Repository, Commit, WeeklyVersionConfig

    db = app_module.db
    tag = uuid.uuid4().hex[:8]

    with app_module.app.app_context():
        db.create_all()

        project = Project(code=f'TZ{tag}', name='时区测试项目', department='QA')
        db.session.add(project)
        db.session.commit()

        repo = Repository(
            project_id=project.id,
            name=f'tz_repo_{tag}',
            type='git',
            url='https://example.invalid/tz.git',
            resource_type='table',
        )
        db.session.add(repo)
        db.session.commit()

        config = WeeklyVersionConfig(
            project_id=project.id,
            repository_id=repo.id,
            name='时区窗口测试',
            branch='master',          # NOT NULL
            start_time=WIN_START_BJ,  # 北京墙钟，原样入库（与表单行为一致）
            end_time=WIN_END_BJ,
            is_active=True,
            auto_sync=False,
            status='active',
        )
        db.session.add(config)
        db.session.commit()

        # 三条提交，真实北京时间分别是：
        #   before : 3/1 10:00 —— 窗口**之前**
        #   early  : 3/2 00:30 —— 窗口开始后 30 分钟（正是修复前被丢掉的那一段）
        #   late   : 3/8 23:00 —— 窗口结束前 1 小时
        specs = [
            ('before0001', 2026, 3, 1, 10, 0),
            ('early00001', 2026, 3, 2, 0, 30),
            ('late000001', 2026, 3, 8, 23, 0),
        ]
        for commit_id, y, m, d, hh, mm in specs:
            db.session.add(Commit(
                repository_id=repo.id,
                commit_id=commit_id,
                path='config/tz.xlsx',
                commit_time=_utc_naive_for_beijing_wallclock(y, m, d, hh, mm),
                status='pending',
            ))
        db.session.commit()

        ns = SimpleNS(flask_app=app_module.app, db=db, config_id=config.id,
                      repo_id=repo.id, project_id=project.id)
        yield ns

        # teardown：只删自己造的行
        try:
            Commit.query.filter_by(repository_id=repo.id).delete(synchronize_session=False)
            WeeklyVersionConfig.query.filter_by(id=config.id).delete(synchronize_session=False)
            Repository.query.filter_by(id=repo.id).delete(synchronize_session=False)
            Project.query.filter_by(id=project.id).delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _commits_in_window(ns):
    """用**生产代码自己的**换算函数构造窗口查询，返回命中的 commit_id 集合。"""
    from models import Commit, WeeklyVersionConfig
    from services.weekly_version_logic import weekly_window_in_utc

    config = ns.db.session.get(WeeklyVersionConfig, ns.config_id)
    start_utc, end_utc = weekly_window_in_utc(config)
    rows = Commit.query.filter(
        Commit.repository_id == ns.repo_id,
        Commit.commit_time >= start_utc,
        Commit.commit_time <= end_utc,
    ).order_by(Commit.commit_time.asc()).all()
    return [row.commit_id for row in rows]


def test_window_includes_commits_in_the_first_eight_hours(seeded):
    """窗口开始后最初 8 小时内的提交必须被选中（修复前它们被整体丢掉）。"""
    with seeded.flask_app.app_context():
        hit = _commits_in_window(seeded)

    assert 'early00001' in hit, (
        f'窗口「3/2 00:00 ~ 3/9 00:00」（北京时间）内的提交 early00001（北京 3/2 00:30）没被选中。\n'
        f'实际命中：{hit}\n'
        f'原因：窗口存的是北京墙钟、commit_time 存的是 naive-UTC 墙钟，直接比大小会整体偏移\n'
        f'8 小时 → 实际生效窗口变成 3/2 08:00 ~ 3/9 08:00，于是**每周窗口前 8 小时的提交\n'
        f'被静默丢弃**，审核者完全看不到那段变更。'
    )
    assert 'late000001' in hit, f'窗口末尾的提交没被选中：{hit}'


def test_window_excludes_commits_before_the_window(seeded):
    """反向保险：窗口之前的提交不得被选中（换算不能把边界算反）。"""
    with seeded.flask_app.app_context():
        hit = _commits_in_window(seeded)

    assert 'before0001' not in hit, (
        f'窗口之前的提交 before0001（北京 3/1 10:00）被错误地选进了窗口：{hit}'
    )


def test_base_commit_is_the_last_one_before_the_window(seeded):
    """基准版本必须是窗口**之前**的最后一个提交。

    修复前：窗口偏移 8 小时后，窗口前 8 小时的提交（early00001）会被误判为基准，
    于是窗口首日的变更被折叠进基准，对该文件而言「整周都没变更」。
    """
    from models import Commit, WeeklyVersionConfig
    from services.weekly_version_logic import weekly_window_in_utc

    with seeded.flask_app.app_context():
        config = seeded.db.session.get(WeeklyVersionConfig, seeded.config_id)
        start_utc, _ = weekly_window_in_utc(config)
        base = Commit.query.filter(
            Commit.repository_id == seeded.repo_id,
            Commit.path == 'config/tz.xlsx',
            Commit.commit_time < start_utc,
        ).order_by(Commit.commit_time.desc()).first()

        assert base is not None, '没有找到基准提交'
        assert base.commit_id == 'before0001', (
            f'基准版本被选成了 {base.commit_id}，应为 before0001（窗口前最后一个提交）。\n'
            f'选错基准会把窗口首日的变更折叠进去，导致该文件看起来「整周无变更」。'
        )


# ---------------------------------------------------------------------------
# 三、结构守卫：不许再出现「窗口与 commit_time 直接比较」
# ---------------------------------------------------------------------------
def test_no_direct_comparison_between_window_and_commit_time():
    """源码级守卫：`Commit.commit_time` 不得与 `config.start_time/end_time` 直接比较。

    这类比较是静默的（不报错、只是结果偏 8 小时），代码评审很容易看漏。
    新增查询请走 `weekly_window_in_utc(config)` / `beijing_window_to_utc_naive(...)`。
    """
    import re

    offenders = []
    for rel in ('services/weekly_version_logic.py', 'services/status_sync_service.py'):
        path = os.path.join(PROJECT_ROOT, rel)
        with open(path, encoding='utf-8') as fh:
            for lineno, line in enumerate(fh, 1):
                code = line.split('#', 1)[0]
                if 'commit_time' not in code:
                    continue
                # 只认真正的缺陷形状：右侧是**属性访问**（config.start_time / cfg.end_time）。
                # 局部变量（例如换算后的 config_start_time）不匹配 —— 那些是修复后的写法。
                if re.search(r'commit_time\s*[<>=!]+\s*[\w\.]*\.(start_time|end_time)', code):
                    offenders.append(f'{rel}:{lineno}: {line.strip()}')

    assert not offenders, (
        '以下位置直接把 Commit.commit_time 与 config 的时间窗口比较了。\n'
        '窗口是 naive 北京墙钟、commit_time 是 naive-UTC 墙钟，直接比较会整体偏移\n'
        '8 小时且不报错（每周窗口前 8 小时的提交被静默丢弃）：\n  '
        + '\n  '.join(offenders)
        + '\n请改用 weekly_window_in_utc(config) 或 beijing_window_to_utc_naive(...)。'
    )
