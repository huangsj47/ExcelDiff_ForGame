# -*- coding: utf-8 -*-
"""同步节拍：**上一轮真正跑完，才有下一次机会**。

## 为什么

`setup_schedule` 原先写死 `sched_module.every(2).minutes.do(schedule_weekly_sync_tasks)`，
而一条 `weekly_sync` 要逐仓走，大仓一轮 **340~420 秒**（实测：421=339s、425=342s、
427=352s、431=418s、436=409s）。2 分钟的节拍**本来就追不上**：

* 队列里于是**永远**有一条优先级 3 的同步在等 —— 低优先级任务（`auto_sync` /
  `weekly_excel_cache` / `weekly_ai_analysis`）永远轮不到（那条链子在
  `create_weekly_sync_task` 的 docstring 里）；
* 而 AI 分析那道闸门判的正是「有没有同步在写缓存」—— 于是「同步在跑」接近于稳态，
  用户才会**反复**撞上那句「等待 Diff 同步完成」。

节奏的正确形态不是「缩短周期让同步更勤」，而是「一轮跑完再排下一轮」：单个配置的
同步任务本身已经按 config 去重（`create_weekly_sync_task`），但**同一批里的另一条
配置**照样会在同一个 tick 里被排上 —— 所以要有一条按**分组**的判据。
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import services.task_worker_service as worker
from app import app as flask_app
from app import create_tables, db
from models import BackgroundTask, Project, Repository
from models.weekly_version import WeeklyVersionConfig
from services.ai_analysis_service import build_weekly_group_key


class _FakeJob:
    """`schedule` 的 Job 替身：只记下「什么周期、挂的是哪个函数」。"""

    def __init__(self, amount, unit="minutes"):
        self.amount = amount
        self.unit = unit
        self.at_time = None

    @property
    def minutes(self):
        self.unit = "minutes"
        return self

    @property
    def seconds(self):
        self.unit = "seconds"
        return self

    @property
    def day(self):
        self.unit = "day"
        return self

    def at(self, when):
        self.at_time = when
        return self

    def do(self, func, *args, **kwargs):
        _REGISTERED.append(self)
        _REGISTERED_FUNCS.append(func)
        return func


_REGISTERED: list = []
_REGISTERED_FUNCS: list = []


class _FakeSchedule:
    def clear(self):
        _REGISTERED.clear()
        _REGISTERED_FUNCS.clear()

    def every(self, amount=1):
        return _FakeJob(amount)

    def run_pending(self):
        return None


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed_two_configs_in_one_group():
    """同一项目、同一窗口、**同一版本名**的两条配置 —— 它们同属一个批次。

    名字必须共用（真实的多仓库批次是 `f"{版本名} - {仓库名}"`）：批次判据是
    「同项目 + 同窗口 + 同版本名」（`project_config_source.weekly_batch_configs`），
    随手起两个不同的随机名在平台眼里就是两个版本。
    """
    now = datetime.now(timezone.utc)
    with flask_app.app_context():
        create_tables()
        project = Project(code=_uid("CD"), name="节拍用例")
        db.session.add(project)
        db.session.flush()
        configs = []
        base_name = _uid("W")
        for index in range(2):
            repository = Repository(
                project_id=project.id, name=_uid(f"repo{index}"), type="git",
                url="https://example.invalid/r.git", branch="main",
                resource_type="code" if index else "table",
            )
            db.session.add(repository)
            db.session.flush()
            # 窗口要**覆盖现在**：`schedule_weekly_sync_tasks` 拿北京墙钟与 `end_time`
            # 比，窗口一过就把配置置 completed 并跳过（那就测不到节拍了）。
            config = WeeklyVersionConfig(
                project_id=project.id, repository_id=repository.id,
                name=f"{base_name} - {repository.name}",
                branch="main", start_time=now - timedelta(days=7),
                end_time=now + timedelta(days=7),
                is_active=True, auto_sync=True, status="active",
            )
            db.session.add(config)
            configs.append(config)
        db.session.commit()
        return {
            "config_ids": [cfg.id for cfg in configs],
            "config_id": configs[0].id,
            "sibling_config_id": configs[1].id,
        }


def _pending_sync_tasks(config_ids):
    with flask_app.app_context():
        return BackgroundTask.query.filter(
            BackgroundTask.task_type == "weekly_sync",
            BackgroundTask.commit_id.in_([str(item) for item in config_ids]),
            BackgroundTask.status.in_(["pending", "processing"]),
        ).all()


# ==========================================================================
#  一、注册的周期本身
# ==========================================================================


class TestTheRegisteredPeriod:
    def test_the_weekly_sync_period_is_longer_than_a_round(self):
        """**行为断言**（不靠字符串匹配）：把假的 `schedule` 注入进去，看真实注册了什么。

        判据是「周期明显大于单轮耗时」：实测大仓一轮 340~420 秒、小仓 17 秒，
        一批两仓合计约 7 分钟。取 15 分钟 = 单轮最长实测值的两倍以上。
        """
        fake = _FakeSchedule()
        saved = sys.modules.get("schedule")
        saved_initialized = worker._schedule_initialized
        sys.modules["schedule"] = fake
        try:
            worker._schedule_initialized = False
            worker.setup_schedule(include_cleanup=False)
        finally:
            worker._schedule_initialized = saved_initialized
            if saved is None:
                sys.modules.pop("schedule", None)
            else:
                sys.modules["schedule"] = saved

        jobs = [
            (job, func)
            for job, func in zip(_REGISTERED, _REGISTERED_FUNCS)
            if func is worker.schedule_weekly_sync_tasks
        ]
        assert jobs, "周版本同步调度器没有注册进定时器"
        job = jobs[0][0]
        assert job.unit == "minutes", job.unit
        assert job.amount >= 10, (
            f"周版本同步的周期是 {job.amount} 分钟 —— 实测单轮要 340~420 秒，"
            "周期比单轮还短就一定会堆队列、并让 AI 分析永远撞上闸门"
        )


# ==========================================================================
#  二、同一批**正在写缓存**时 → 本轮不排新的
#
#  （判据只认 `processing`：把「排队中的同步」也算进来会让同步停摆 ——
#   那条反向保险在 tests/test_weekly_sync_dedup_blocks_starvation.py 里。）
# ==========================================================================


class TestTheGroupWideGuard:
    def _run_scheduler(self, monkeypatch):
        # 让饿死让路那条判据不要插手：它看的是**全库**有没有等太久的任务，
        # 而测试库是会话级共用的，别的用例留下的行会让整轮直接跳过。
        monkeypatch.setattr(worker, "_starvation_yield_note", lambda now, limit=3: "")
        worker.schedule_weekly_sync_tasks()

    def test_a_group_with_a_running_sync_does_not_get_another_one(self, monkeypatch):
        """**这一条才是「一轮跑完再排下一轮」。**

        `create_weekly_sync_task` 的去重是按 config 的：同一批里的另一条配置照样会在
        同一个 tick 里被排上。大仓一轮 340~420 秒 + 每 tick 补一条 = 队列永远不空、
        worker 被钉死在同步上、AI 分析的闸门永远在拦人。
        """
        seeded = _seed_two_configs_in_one_group()
        with flask_app.app_context():
            db.session.add(
                BackgroundTask(
                    task_type="weekly_sync", commit_id=str(seeded["config_id"]),
                    priority=3, status="processing",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
                )
            )
            db.session.commit()
            before = len(_pending_sync_tasks(seeded["config_ids"]))

            self._run_scheduler(monkeypatch)

            after = _pending_sync_tasks(seeded["config_ids"])
        assert len(after) == before, (
            "这一批已经有同步在写缓存，本轮却又排了一条 —— 队列会越堆越深，"
            f"AI 分析于是永远撞上闸门：{[(t.id, t.commit_id, t.status) for t in after]}"
        )

    def test_an_idle_group_still_gets_its_sync(self, monkeypatch):
        """反面：没有在跑的同步时**必须**照排 —— 别把同步本身停摆了。

        （判据写错的形态有两种，这一条守的是「反向过头」那一种。）
        """
        seeded = _seed_two_configs_in_one_group()
        before = len(_pending_sync_tasks(seeded["config_ids"]))
        assert before == 0, f"这个用例需要干净的起点，实际有 {before} 条"

        self._run_scheduler(monkeypatch)

        after = _pending_sync_tasks(seeded["config_ids"])
        assert len(after) == 2, (
            "空闲的批次没有被排上同步（同步停摆和同步堆队列一样是故障）："
            f"{[(t.id, t.commit_id) for t in after]}"
        )

    def test_a_task_claimed_mid_tick_does_not_starve_its_own_batch(self, monkeypatch):
        """本 tick 里刚排上、立刻被 worker 认领的任务，不许挡住同批的另一条配置。

        2026-09-23 真机饿死形态（PID 45456）：调度器先给组里 config 1 建任务，worker
        ~3ms 内认领成 processing；循环走到 config 2 时组闸门看到「本批正在写缓存」——
        每个 tick 如此，config 2 从进程启动起一次同步都没跑过，1141 行周版本缓存停在
        前一天。而前一天 worker 认领慢，同一 tick 三条任务 6ms 内全部建出：同一份代码，
        竞速输赢决定饿不饿死。闸门要挡的是**上一批**（那条已有
        `test_a_group_with_a_running_sync_does_not_get_another_one` 钉着），本 tick
        自己刚建的那条是这一批的一部分。
        """
        seeded = _seed_two_configs_in_one_group()
        original_create = worker.create_weekly_sync_task

        def create_and_claim_immediately(config_id):
            """模拟 worker 在调度循环走到下一个配置之前就把任务认领走（真机 ~3ms）。"""
            task_id = original_create(config_id)
            if task_id:
                with flask_app.app_context():
                    row = db.session.get(BackgroundTask, task_id)
                    if row is not None and row.status == 'pending':
                        row.status = 'processing'
                        db.session.commit()
            return task_id

        monkeypatch.setattr(worker, "create_weekly_sync_task", create_and_claim_immediately)

        self._run_scheduler(monkeypatch)

        after = _pending_sync_tasks(seeded["config_ids"])
        assert len(after) == 2, (
            "本 tick 刚被认领的那条把同批另一条配置饿死了 —— 那一条这个 tick 又没排上，"
            "每个 tick 都这样就是永远不同步："
            f"{[(t.id, t.commit_id, t.status) for t in after]}"
        )


# ==========================================================================
#  三、收尾那一行日志必须**只说实际发生的事**
#
#  真机证据（只读 DB + 日志）：`14:19:06 → 15:20:08` 整 61 分钟里
#  「调度了 1 组周版本AI分析任务」出现 61 次，而同期 `background_tasks` 里
#  `weekly_ai_analysis` **只建了 1 条**。旧实现把 `len(grouped)`（**扫到的分组数**）
#  无条件打印出来，循环体里四条 `continue` 一条都拦不住它 —— 排查时据此把「调度器在
#  空转」当成了事实。所以这一组守三件事：三个数是真的、跳过按原因分桶、**没事也要打印**。
# ==========================================================================


def _read_schedule_log(monkeypatch):
    """跑一次真实的调度，把日志行收起来（其余一切照旧）。"""
    lines: list = []
    monkeypatch.setattr(worker, "log_print", lambda msg, *a, **k: lines.append(str(msg)))
    worker.schedule_weekly_ai_analysis_tasks()
    return lines


def _summary_line(lines):
    rows = [line for line in lines if line.startswith("周版本AI分析调度：")]
    assert rows, f"这一轮调度没有留下收尾那一行（静默与说谎一样坏）：{lines}"
    return rows[-1]


def _numbers(line: str) -> dict:
    import re

    found = re.findall(r"检查 (\d+) 组，新建 (\d+) 个，复用 (\d+) 个，跳过 (\d+) 个", line)
    assert found, f"收尾那一行没有说清三个量：{line}"
    checked, created, reused, skipped = (int(item) for item in found[0])
    return {"checked": checked, "created": created, "reused": reused, "skipped": skipped}


def _block_every_gate(monkeypatch, *, reused: bool):
    """把「要不要真排一条分析」之外的判据都放开，好走到建任务那一步。

    （真实判据各有自己的用例守着：闸门 see test_ai_weekly_sync_gate，预算见
    test_ai_usage_filters_and_budget，间隔/输入指纹见 test_weekly_ai_auto_trigger_gate。）
    """
    built: list = []
    # **配置要显式给，不能返回 `{}`。** 这一行原本是 `lambda _pid: {}`，而调度器读开关
    # 时带兜底（`project_cfg.get("auto_weekly_enabled", DEFAULT_AUTO_WEEKLY_ENABLED)`）——
    # 于是这一组用例真正的行为取决于**那个兜底值**，不是这里。2026-09-22 把默认值从
    # 「开」改成「关」之后，它们全部停在第一道闸后面，报出来的失败是「记账不准」，
    # 而真正的原因是「这组用例没有声明自己的前提」。
    # 它想说的本来就是「闸门都放开，走到建任务那一步」，那就把开关明说成开的。
    monkeypatch.setattr(
        worker, "get_project_analysis_config",
        lambda _pid: {"auto_weekly_enabled": True},
    )
    monkeypatch.setattr(worker, "has_weekly_changes", lambda *_a, **_k: True)
    monkeypatch.setattr(worker, "snapshot_already_analyzed", lambda *_a, **_k: False)
    monkeypatch.setattr(worker, "weekly_sync_in_flight", lambda *_a, **_k: "")
    monkeypatch.setattr(worker, "budget_gate_reason", lambda *_a, **_k: "")
    monkeypatch.setattr(worker, "_pending_weekly_analysis_task_exists", lambda _key: reused)
    monkeypatch.setattr(
        worker, "create_weekly_ai_analysis_task",
        lambda *a, **k: built.append(a[0]) or 4242,
    )
    return built


class TestTheScheduleLogTellsTheTruth:
    def test_the_three_counts_add_up_and_the_skips_are_bucketed(self, monkeypatch):
        """三个数必须自洽（检查 = 新建 + 复用 + 跳过），且跳过按原因分桶。"""
        from models.ai_analysis import AiWeeklyAnalysisState

        seeded = _seed_two_configs_in_one_group()
        built = _block_every_gate(monkeypatch, reused=False)
        with flask_app.app_context():
            # 让这一组**未到间隔**：那条判据排在最前面（预算/变更/闸门都在它之后），
            # 所以这样构造最省事，也正好落在「每分钟都跳过」这个真实形态上。
            config = db.session.get(WeeklyVersionConfig, seeded["config_id"])
            group_key = build_weekly_group_key(config)
            state = AiWeeklyAnalysisState.query.filter_by(group_key=group_key).first()
            if state is None:
                state = AiWeeklyAnalysisState(
                    project_id=config.project_id, group_key=group_key, base_name="W",
                )
                db.session.add(state)
            state.last_triggered_at = datetime.now(timezone.utc)
            db.session.commit()

            line = _summary_line(_read_schedule_log(monkeypatch))

        numbers = _numbers(line)
        assert numbers["checked"] == numbers["created"] + numbers["reused"] + numbers["skipped"], line
        assert numbers["checked"] >= 1, line
        assert numbers["skipped"] >= 1, line
        # **只判这一组**：测试库是会话级共用的，别的用例留下的活跃分组也会被扫到。
        assert seeded["config_id"] not in built, f"未到间隔却还是建了任务：{built}"
        assert "未到间隔" in line, f"跳过没有按原因分桶：{line}"
        assert "未到间隔 0" not in line, line

    def test_nothing_to_do_still_prints_a_line(self, monkeypatch):
        """**没事也要打印。**「无变化就不打印」会让「调度器死了」与「调度器空转」
        长得一模一样 —— 而这一轮排查正是被这一类比方带偏的。
        """

        class _NoConfigs:
            def filter_by(self, **_kwargs):
                return self

            def all(self):
                return []

        monkeypatch.setattr(
            worker, "_WeeklyVersionConfig", type("_M", (), {"query": _NoConfigs()})
        )

        line = _summary_line(_read_schedule_log(monkeypatch))

        assert _numbers(line) == {"checked": 0, "created": 0, "reused": 0, "skipped": 0}, line

    def test_a_built_task_is_counted_as_created(self, monkeypatch):
        """**新建**数取的是「真的建了新行」。"""
        _seed_two_configs_in_one_group()
        built = _block_every_gate(monkeypatch, reused=False)

        line = _summary_line(_read_schedule_log(monkeypatch))
        numbers = _numbers(line)

        assert numbers["checked"] == numbers["created"] + numbers["reused"] + numbers["skipped"], line
        assert numbers["created"] >= 1, line
        assert built, "记账说建了任务，实际没有走到建任务那一步"

    def test_a_deduplicated_task_is_counted_as_reused(self, monkeypatch):
        """反面：队列里已经有这一组的任务时，`create` 返回的是**既有 id** —— 那叫复用，
        记成「新建」就是同一个谎换了个小数（排查时照样会以为调度器在干活）。"""
        _seed_two_configs_in_one_group()
        _block_every_gate(monkeypatch, reused=True)

        line = _summary_line(_read_schedule_log(monkeypatch))

        assert _numbers(line)["reused"] >= 1, line
