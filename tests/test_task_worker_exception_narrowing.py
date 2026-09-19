import os
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

import services.repo_worktree_cleanup as cleanup
import services.task_worker_service as worker


def test_force_remove_repo_worktree_returns_false_when_fallback_delete_fails(tmp_path, monkeypatch):
    """删除逻辑住在 `services/repo_worktree_cleanup.py`（原先在 task_worker_service 里，
    那个文件已贴着 2000 行硬上限）。patch 目标要跟着实现走 ——
    patch `worker.shutil` 只会改到另一个模块的 `shutil`，这条用例就会变成
    「真删了一次临时目录、然后断言它还在」。"""
    repo_dir = tmp_path / "repo_keep"
    repo_dir.mkdir()

    monkeypatch.setattr(
        cleanup.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("permission denied")),
    )
    monkeypatch.setattr(
        cleanup.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.SubprocessError("rmdir failed")),
    )

    assert cleanup.force_remove_repo_worktree(str(repo_dir)) is False
    assert os.path.exists(repo_dir)


def test_cleanup_git_processes_handles_process_runtime_errors():
    class _FakeProc:
        def __init__(self):
            self.kill_called = False

        def poll(self):
            raise OSError("broken process handle")

        def kill(self):
            self.kill_called = True

    proc = _FakeProc()
    worker._active_git_processes = {proc}

    worker.cleanup_git_processes()

    assert proc.kill_called is True
    assert proc not in worker._active_git_processes


def test_update_task_status_with_retry_rolls_back_on_sqlalchemy_error(monkeypatch):
    class _FakeTask:
        def __init__(self):
            self.status = "pending"
            self.retry_count = 0
            self.started_at = None
            self.completed_at = None
            self.error_message = None

    class _FakeSession:
        def __init__(self):
            self.rollback_called = 0

        def get(self, _model, _task_id):
            return _FakeTask()

        def commit(self):
            raise SQLAlchemyError("commit failed")

        def rollback(self):
            self.rollback_called += 1

    fake_session = _FakeSession()
    fake_db = SimpleNamespace(session=fake_session)

    monkeypatch.setattr(worker, "_db", fake_db)
    monkeypatch.setattr(worker, "_BackgroundTask", object)

    with pytest.raises(SQLAlchemyError):
        worker.update_task_status_with_retry(123, "processing")

    assert fake_session.rollback_called == 1


def test_create_auto_sync_task_rolls_back_on_sqlalchemy_error(monkeypatch):
    class _FakeQuery:
        def filter_by(self, **_kwargs):
            return self

        def first(self):
            raise SQLAlchemyError("query failed")

    class _FakeBackgroundTask:
        query = _FakeQuery()

    class _FakeSession:
        def __init__(self):
            self.rollback_called = 0

        def rollback(self):
            self.rollback_called += 1

    fake_session = _FakeSession()
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeBackgroundTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))

    task_id = worker.create_auto_sync_task(101)
    assert task_id is None
    assert fake_session.rollback_called == 1


def test_load_pending_tasks_rolls_back_on_sqlalchemy_error(monkeypatch):
    class _OrderColumn:
        def asc(self):
            return self

    class _FakeQuery:
        def filter_by(self, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def all(self):
            raise SQLAlchemyError("list failed")

    class _FakeBackgroundTask:
        query = _FakeQuery()
        priority = _OrderColumn()
        created_at = _OrderColumn()

    class _FakeSession:
        def __init__(self):
            self.rollback_called = 0

        def rollback(self):
            self.rollback_called += 1

    fake_session = _FakeSession()
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeBackgroundTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))

    worker.load_pending_tasks()
    assert fake_session.rollback_called == 1


def test_create_weekly_sync_task_rolls_back_on_sqlalchemy_error(monkeypatch):
    class _FakeQuery:
        def filter_by(self, **_kwargs):
            return self

        def first(self):
            return None

    class _FakeBackgroundTask:
        query = _FakeQuery()

        def __init__(self, **kwargs):
            self.id = 999
            for key, value in kwargs.items():
                setattr(self, key, value)

    class _FakeSession:
        def __init__(self):
            self.rollback_called = 0

        def add(self, _obj):
            return None

        def flush(self):
            raise SQLAlchemyError("flush failed")

        def rollback(self):
            self.rollback_called += 1

    fake_session = _FakeSession()
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeBackgroundTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))

    task_id = worker.create_weekly_sync_task(321)
    assert task_id is None
    assert fake_session.rollback_called == 1


def _fake_task_row(row_id, status, task_type='excel_diff'):
    return SimpleNamespace(
        id=row_id,
        status=status,
        started_at='2026-01-01T00:00:00+00:00' if status == 'processing' else None,
        task_type=task_type,
        repository_id=1,
        commit_id='c' * 40,
        file_path='config/30_goods/item.xlsx',
        priority=5,
    )


def test_load_pending_tasks_requeues_interrupted_tasks_in_the_same_call(monkeypatch):
    """重启时被中断（库里还停在 processing）的任务必须**在同一次启动里**重新入队。

    `load_pending_tasks` 曾经把「processing 改回 pending」放在装载循环之后，且只改库、
    不再入队。单机模式下内存队列是唯一执行路径（worker 只从队列取任务），所以那些任务
    要等到**下一次**重启才会跑；期间它们还是 pending，还会占着业务键堵住同键任务的重建
    （add_excel_diff_task 等按 pending/processing 去重）。

    这里断言的是**行为**，不是语句顺序：假库按行的实时 status 回答，所以把重置放回后面
    就会立刻红 —— 那时 pending 查询只看到 id=2，队列里也就只有它。
    """

    class _OrderColumn:
        def asc(self):
            return self

    class _Query:
        def filter_by(self, **kwargs):
            self._status = kwargs.get('status')
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def all(self):
            return [row for row in rows if row.status == self._status]

    class _FakeBackgroundTask:
        query = _Query()
        priority = _OrderColumn()
        created_at = _OrderColumn()

    class _FakeSession:
        def __init__(self):
            self.committed = 0
            self.rollback_called = 0

        def commit(self):
            self.committed += 1

        def rollback(self):
            self.rollback_called += 1

    interrupted = _fake_task_row(1, 'processing')
    waiting = _fake_task_row(2, 'pending')
    rows = [interrupted, waiting]

    enqueued = []
    fake_session = _FakeSession()
    monkeypatch.setattr(worker, "_BackgroundTask", _FakeBackgroundTask)
    monkeypatch.setattr(worker, "_db", SimpleNamespace(session=fake_session))
    monkeypatch.setattr(worker, "background_task_queue", SimpleNamespace(put=enqueued.append))
    monkeypatch.setattr(worker, "TaskWrapper", lambda priority, counter, data: (priority, data))
    monkeypatch.setattr(worker, "check_and_create_auto_sync_tasks", lambda: None)

    worker.load_pending_tasks()

    queued_ids = sorted(entry[1]['task_id'] for entry in enqueued)
    assert queued_ids == [1, 2], "被中断的任务没有在同一次启动里重新入队"
    assert len(enqueued) == 2, "同一个任务被重复入队了"
    assert interrupted.status == 'pending', "库里那行没有被收回成 pending"
    assert interrupted.started_at is None
    assert fake_session.rollback_called == 0


# ---------------------------------------------------------------------------
# 「临时暂停后台缓存任务」：写入方与读取方必须真的接上
#
# 这个开关由 `refresh_merge_diff` 在删缓存前后设/清，目的是别让后台线程把它刚删掉的
# 那批缓存在同一瞬间算完写回去（那正是「点了重新计算、刷新后没变化」的成因）。
# 而它曾经**只有写没有读**：`is_tasks_paused()` / `wait_if_paused()` 在
# `services/background_task_service.py` 之外零调用者，工作线程主循环从来没看过它 ——
# 日志里印着「🔄 临时暂停后台缓存任务处理...」，后台照旧在跑。
# ---------------------------------------------------------------------------

def test_the_worker_loop_actually_reads_the_pause_flag():
    source = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "services", "task_worker_service.py"),
        encoding="utf-8-sig",
    ).read()
    start = source.index("def background_task_worker")
    end = source.index("\ndef ", start + 10)
    body = source[start:end]
    assert "is_tasks_paused()" in body, (
        "工作线程又不看暂停标志了 —— 这个开关会退化成「只写不读」，"
        "而日志里照样印着「临时暂停」"
    )
    assert "continue" in body, "暂停时应当是「跳过这一轮」，不是阻塞在工作线程里"


def test_pausing_the_flag_makes_the_pause_visible_to_readers():
    from services.background_task_service import (
        is_tasks_paused,
        pause_background_tasks,
        resume_background_tasks,
    )

    resume_background_tasks()          # 别的用例可能留下过 True
    assert is_tasks_paused() is False
    try:
        pause_background_tasks()
        assert is_tasks_paused() is True
    finally:
        resume_background_tasks()
    assert is_tasks_paused() is False


def test_resume_is_called_from_a_finally_so_no_path_can_strand_the_flag():
    """**这条是可用性问题**：`refresh_merge_diff` 的四段 `except` 各自 `return`。

    恢复语句原先写在 `try` 的最后一句，于是任何一条失败路径都会跳过它 ——
    `_task_paused` 永远留在 True、后台工作线程从此再也不领任务。
    一次「重新计算」失败 = 整个平台的缓存同步静默停摆，而日志里只有「重新计算失败」。
    """
    source = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "services", "commit_operation_handlers.py"),
        encoding="utf-8-sig",
    ).read()
    start = source.index("def refresh_merge_diff")
    end = source.index("\ndef ", start + 10)
    body = source[start:end]
    pause_at = body.index("pause_background_tasks()")
    finally_at = body.index("finally:")
    resume_at = body.index("resume_background_tasks()")
    assert pause_at < finally_at < resume_at, (
        "恢复语句必须在 finally 里：写在 try 末尾的话，四段 except 会一起跳过它"
    )


# ---------------------------------------------------------------------------
# 删工作副本目录的前置判断
#
# ============================ 安全铁律 ============================
# **危险目标（空值 / "." / ".." / 盘符根 / 平台源码根）只许喂给
# `_removal_target_is_dangerous` —— 那是个纯判断函数，不碰文件系统。**
#
# 绝对不许把它们喂给 `_force_remove_repo_worktree`。它内部是
# `shutil.rmtree` + `rmdir /s /q` 兜底，一旦守卫那行被改坏（哪怕只是本地调试时
# 临时注掉、或做变异验证时注掉），这一喂就是**整台机器被递归删除**。
#
# 写这个文件的人就是这么把 `C:\` 交给它、并且真的跑了一次 `rmdir /s /q C:\` ——
# 代价是这台机器的 `C:\Python\Python3.13\Lib`（标准库）与整个工作目录被删掉。
# 所以「空值会被拒」那一条用 `monkeypatch.chdir(tmp_path)` 兜住：即使守卫完全失效，
# 被删的也只是一个一次性的临时目录。
# ==================================================================
# ---------------------------------------------------------------------------

def test_the_removal_guard_is_a_pure_function_that_never_touches_the_disk():
    """判据必须能与「真的去删」分开 —— 这是上一条铁律成立的前提。

    **走 AST 而不是 grep 源码**：这个函数的 docstring 里逐字写着
    「这个目录**不能**被 rmtree 掉吗？」（本仓注释会原样引用要防的东西，
    这是已知的坑），按字符串找会把它自己判成违规。AST 只看真实的调用与属性访问。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cleanup.removal_target_is_dangerous))
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Name):
            used.add(node.id)
    for forbidden in ("rmtree", "remove", "rmdir", "unlink", "subprocess", "system", "run"):
        assert forbidden not in used, (
            f"_removal_target_is_dangerous 里出现了 {forbidden} —— "
            f"判据与删除必须分开，否则验证「危险目标会被拒」就只能真的去删"
        )


@pytest.mark.parametrize(
    "target_factory",
    [
        lambda: cleanup.repo_root(),
        lambda: os.path.dirname(cleanup.repo_root()),
        lambda: os.path.abspath(os.sep),
        lambda: os.path.abspath(os.path.join(cleanup.repo_root(), "..", "..")),
        lambda: os.path.abspath(
            cleanup.resolve_runtime_path(None, default_relative=cleanup.default_repos_base_dir())
        ),
    ],
    ids=["platform_root", "platform_parent", "drive_root", "grandparent", "repos_root"],
)
def test_a_dangerous_removal_target_is_recognised(target_factory):
    """平台源码根及其祖先、盘符根、repos 根本身 —— 都必须被判成危险。

    注意这里**只调判断函数**，绝不调删除函数（见上方铁律）。
    """
    assert cleanup.removal_target_is_dangerous(target_factory()) is True


def test_the_guard_uses_commonpath_not_string_prefix():
    """`C:\a` 与 `C:\ab` 的字符串前缀相同但不是祖先关系。

    `str.startswith` 会把它们判成一家人，`os.path.commonpath` 不会。
    """
    import os as _os

    parent = os.path.abspath(os.path.join(cleanup.repo_root(), ".."))
    sibling = parent + "NotAnAncestor"
    # 这个 sibling 只是名字像，不是平台根的祖先 —— 不该被判危险
    assert cleanup.removal_target_is_dangerous(sibling) is False
    assert sibling.startswith(parent)          # 字符串前缀确实相同
    assert _os.path.commonpath([sibling, parent]) != os.path.normcase(parent)


def test_a_real_worktree_is_not_dangerous():
    """**不能靠「一律拒绝」通过** —— 真工作副本必须照删，否则重克隆永远清不干净。"""
    repos_root = os.path.abspath(
        cleanup.resolve_runtime_path(None, default_relative=cleanup.default_repos_base_dir())
    )
    assert cleanup.removal_target_is_dangerous(os.path.join(repos_root, "PROJ_repo_7")) is False


def test_a_real_worktree_is_actually_deleted(tmp_path, monkeypatch):
    """端到端：一个真的在 repos 根下的目录，`_force_remove_repo_worktree` 要删掉它。"""
    repos_root = tmp_path / "repos"
    worktree = repos_root / "PROJ_repo_7"
    worktree.mkdir(parents=True)
    (worktree / "file.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(cleanup, "default_repos_base_dir", lambda: str(repos_root))

    assert cleanup.force_remove_repo_worktree(str(worktree)) is True
    assert not worktree.exists()


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_local_path_deletes_nothing(blank, tmp_path, monkeypatch):
    """**空值那一路**：`os.path.abspath("")` 返回的是当前工作目录，不是空串 ——
    原先的守卫 `if not os.path.abspath(...)` 从写下来那天起就没执行过。

    这条用 `chdir` 到一次性临时目录来测：即使守卫完全失效，被删的也只是这个临时目录，
    碰不到仓库、更碰不到盘符根。**这是本条用例唯一安全的测法。**
    """
    monkeypatch.chdir(tmp_path)
    # 保险丝：万一 chdir 没生效（或 tmp_path 恰好就是仓库），立刻失败而不是继续删
    assert os.path.abspath(os.getcwd()) != cleanup.repo_root(), (
        "当前工作目录就是平台源码根 —— 这条用例不允许在这种情况下运行"
    )

    assert cleanup.force_remove_repo_worktree(blank) is True
    assert os.path.exists(tmp_path), "空值应当被拒绝删除，临时目录必须还在"
    assert os.path.exists(cleanup.repo_root()), "平台源码根必须完好"
