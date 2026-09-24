# -*- coding: utf-8 -*-
"""工作包 D 的 P1：**放宽到冻结仓库的只读范围**（跨未改文件核查）。

## 这一组钉的是哪一句信息缺口

run 45/46 的报告都把「伤害函数旧名是否仍被其它模块调用」写成信息缺口。根因不是额度，
是**范围**：`find_references` 只搜本批次改动的文件，`file_content` 只读本批次路径。
小 diff 恰好改了公共接口时，模型**既不能证伪「调用方没改」也不能证实它**。

所以这里的 fixture 就是那个形态：**只改公共函数的定义、不动调用方**。
断言四件事 ——

1. 仓库范围搜索能在**未改动**的调用方里找到旧名（`scripts/combat/attack.lua`）；
2. **同一轮**里可以直接读到那个未改文件的冻结版本行窗（不必先「发命中地址」再要一轮）；
3. 读取走的是**冻结对象**（`commit.tree`），工作副本被改动也不影响（反向样本）；
4. 同名符号在**别的仓库 / 别的分支**不得混进来（反向样本）。

## 另外五条安全判据（都有反向样本）

绝对路径、带 `..` 的路径、**符号链接逃逸**、凭证类文件、跨仓库 —— 每一条都断言
**拒绝的理由**（不是只断言「没返回内容」）。理由本身是模型下一轮唯一能改正的东西，
而「被拒」被读成「平台取数失败」在线上实测里真的发生过（报告里多出一条假的信息缺口）。

## 环境

全部在 `tmp_path` 里 `git init` 出真仓库（**不碰线上库、不碰 8002**），也不需要
`commits_log`（`_commit_row` 查不到行时正好走「不在本批次」那条路 —— 那正是被测分支）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.ai.frozen_repo import (
    FrozenRepository,
    FrozenTreeReader,
    build_read_scope,
    exclusion_reason,
    reset_caches,
    resolve_frozen_repository,
    resolve_repo_path,
)
from services.ai.platform_provider import PlatformContextProvider
from services.ai.protocol import ContextRequest, sanitize_requests
from services.ai.repo_reference import search_frozen_repository
from services.ai.scope import AnalysisScope
from services.ai import provider_search as agent_mode_module

# 旧名 / 新名：fixture 的公共函数更名（只改定义处，不改调用方）。
OLD_NAME = "CalcDamage"
NEW_NAME = "CalcHitDamage"
DEFINER = "scripts/net/proto.lua"
CALLER = "scripts/combat/attack.lua"

BATCH_COMMIT = "b" * 40


# ---------------------------------------------------------------------------
#  真 git 仓库
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """在 repo 里跑一条 git 命令（失败即断言失败）。与仓库既有测试同一手法。"""
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.email=qa@example.com",
         "-c", "user.name=QA", "-c", "init.defaultBranch=main", *args],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, f"git {' '.join(args)} 失败: {result.stderr}"
    return result.stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture(autouse=True)
def _clean_caches():
    """每个用例从干净的进程内缓存开始（缓存本身有跨运行复用的性质，见下第 9 组）。"""
    reset_caches()
    yield
    reset_caches()


@pytest.fixture()
def renames_only_the_definition(tmp_path: Path):
    """一个真仓库：第 1 次提交定义 + 调用，第 2 次**只改定义处**的函数名。

    返回 `SimpleNamespace(path, tip, changed_paths)` —— `changed_paths` 就是本批次
    真正改动过的文件（只有一个），调用方不在里面（这正是那个信息缺口的形状）。
    """
    repo = tmp_path / "game"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "scripts/net").mkdir(parents=True)
    (repo / "scripts/combat").mkdir(parents=True)
    (repo / DEFINER).write_text(
        "local M = {}\n"
        f"function M.{OLD_NAME}(atk, def)\n"
        "    return math.max(0, atk - def)\n"
        "end\n"
        "return M\n",
        encoding="utf-8",
    )
    (repo / CALLER).write_text(
        "local proto = require('net.proto')\n"
        "local function hit(attacker, target)\n"
        f"    return proto.{OLD_NAME}(attacker.atk, target.def)\n"
        "end\n"
        "return hit\n",
        encoding="utf-8",
    )
    (repo / ".env").write_text("DB_PASSWORD=hunter2\n", encoding="utf-8")
    _commit(repo, "初版：定义 + 调用方")

    # 第二批：**只改定义处**（调用方一个字都没动）。
    (repo / DEFINER).write_text(
        "local M = {}\n"
        f"function M.{NEW_NAME}(atk, def)\n"
        "    return math.max(0, atk - def)\n"
        "end\n"
        "return M\n",
        encoding="utf-8",
    )
    tip = _commit(repo, "把伤害函数改名（调用方未改）")

    return SimpleNamespace(
        path=repo, tip=tip, changed_paths=frozenset({DEFINER})
    )


def _frozen(repo: SimpleNamespace, *, tip: str = "") -> FrozenRepository:
    return FrozenRepository(
        repository_id=7,
        name="game",
        branch="main",
        tip=tip or repo.tip,
        local_path=str(repo.path),
        source="测试给定",
    )


def _provider(repo: SimpleNamespace, **overrides) -> PlatformContextProvider:
    """一个**不需要数据库**的 provider：`_commit_row` 查不到行 → 走「不在本批次」那条路。"""
    kwargs = {
        "loaded": SimpleNamespace(readable={}),
        "frozen_repository": _frozen(repo),
        "scope": AnalysisScope(commits=(BATCH_COMMIT,), paths_by_commit={
            BATCH_COMMIT: renames_only_the_definition.__name__ and frozenset({DEFINER})
        }),
    }
    kwargs.update(overrides)
    provider = PlatformContextProvider(**kwargs)
    # 「本批次里没有这个路径」这一步在这里**直接给定**：真机上它是
    # `commits_log` 查不到对应行（测试环境没有那个库）。不替换掉的话，每次调用都会
    # 打一条「Working outside of application context」的告警日志 —— 噪音会盖住真正的
    # 失败信号，而这条分支正是被测的东西。
    provider._commit_row = lambda commit, path, repository_id="": (None, ())
    return provider


# ---------------------------------------------------------------------------
#  一、金标：只改公共函数定义、未改调用方
# ---------------------------------------------------------------------------


def test_the_repo_wide_search_finds_the_old_name_in_an_unchanged_caller(
    renames_only_the_definition,
):
    """**本工作包的核心断言**：旧名在**未改动**的调用方里被搜出来。

    这一条在修之前必然是空的 —— 调用方不在本批次改动清单里，索引只建在本批次上。
    """
    provider = _provider(renames_only_the_definition)

    text = provider.find_references(OLD_NAME)

    assert f"{CALLER}:3" in text, text
    assert f"proto.{OLD_NAME}(attacker.atk, target.def)" in text
    # **定义处反而不该命中**：tip 上那个函数已经叫新名字了。这一条同时证明
    # 「读的是冻结版本」而不是「读的是某一版旧的」—— 取错版本时这里会冒出 DEFINER:2。
    assert f"{DEFINER}:" not in text, text
    assert NEW_NAME not in text, "搜的是旧名，不该把新名当命中" 


def test_the_search_reports_its_scan_scope_and_the_frozen_tip(
    renames_only_the_definition,
):
    """回执必须说清「搜的是哪个版本、覆盖了多少」—— 不然「没搜到」会被读成「没有」。"""
    provider = _provider(renames_only_the_definition)

    text = provider.find_references(OLD_NAME)

    assert "冻结版本" in text and renames_only_the_definition.tip[:12] in text
    assert "Git 跟踪文件" in text, "范围口径要写出来（不是「本批次改动过的文件」）"
    assert "覆盖账" in text
    assert "本次覆盖了该范围内的**全部**跟踪文件" in text, (
        "小仓库扫全了 → 这句必须出现（否则模型不敢下结论）"
    )


def test_the_same_round_can_read_the_unchanged_callers_frozen_window(
    renames_only_the_definition,
):
    """**同一轮**直接读已知未改文件的冻结版本行窗 —— 不必先「发命中地址」再要一轮。

    这是工作包 D 明说的那条省时省钱的路径：模型已经知道调用方路径时可以直接
    `file_content`，读取走的是同一个冻结版本。
    """
    provider = _provider(renames_only_the_definition)

    text = provider.file_content(BATCH_COMMIT, CALLER, lines="1-10")

    assert text is not None
    assert "文件正文：" in text and CALLER in text
    assert f"proto.{OLD_NAME}" in text, "读到的是**改动之后**那个文件在 tip 上的样子"
    assert f"冻结版本 {renames_only_the_definition.tip[:12]}" in text, (
        "出处必须写明「这是冻结版本上的内容」—— 否则模型会把它与本批次那条提交的内容混起来"
    )
    assert "第 1–5 行" in text, "行窗照旧生效"


def test_the_frozen_window_says_how_to_continue(renames_only_the_definition):
    """回执四件（total_chars / returned_range / next_cursor / truncated）都在。"""
    provider = _provider(renames_only_the_definition)

    text = provider.file_content(BATCH_COMMIT, CALLER, lines="1-2")

    assert "total_chars=" in text and "returned_range=1-2" in text
    assert "truncated=否" in text
    assert 'next_cursor="3-4"' in text, text


# ---------------------------------------------------------------------------
#  二、读的是冻结对象，不是可变工作区（反向样本）
# ---------------------------------------------------------------------------


def test_a_dirty_worktree_does_not_change_what_is_read(renames_only_the_definition):
    """**反向样本**：提交之后把工作副本改脏，读到的仍是冻结版本的内容。

    读工作区等于把「没有版本的证据」交给模型：同步每 2~3 分钟一跑、checkout 也跟着走，
    而模型会把读到的东西当成「这个版本的代码就是这样」。
    """
    repo = renames_only_the_definition
    provider = _provider(repo)
    (repo.path / CALLER).write_text("-- 工作副本被改脏了（未提交）\n", encoding="utf-8")

    text = provider.file_content(BATCH_COMMIT, CALLER, lines="1-5")

    assert "工作副本被改脏了" not in text
    assert f"proto.{OLD_NAME}" in text


def test_the_cached_blob_is_keyed_by_the_tip_not_by_the_repository(
    renames_only_the_definition,
):
    """**反向样本**：强推/新提交换了 tip 之后，同一个进程内不得继续吐旧内容。

    缓存键里有 tip（sha），所以第二次解析必然 miss。这条防的是「进程内缓存按仓库缓存」
    那种写法 —— 它的症状是「分析报告与页面上的 diff 对不上」，且不报错。
    """
    repo = renames_only_the_definition
    first = _provider(repo).file_content(BATCH_COMMIT, CALLER, lines="1-5")
    (repo.path / CALLER).write_text(
        f"local proto = require('net.proto')\nreturn proto.{NEW_NAME}\n", encoding="utf-8"
    )
    new_tip = _commit(repo.path, "调用方也跟着改了")

    second = _provider(repo, frozen_repository=_frozen(repo, tip=new_tip)).file_content(
        BATCH_COMMIT, CALLER, lines="1-5"
    )

    assert OLD_NAME in first
    assert NEW_NAME in second and OLD_NAME not in second, (
        "换了 tip 之后读到的还是旧内容 —— 缓存没有按 tip 分离"
    )


# ---------------------------------------------------------------------------
#  三、跨仓库 / 跨分支不得混入（反向样本）
# ---------------------------------------------------------------------------


def test_a_same_named_symbol_in_another_repository_is_not_mixed_in(
    renames_only_the_definition, tmp_path: Path
):
    """同名符号在**别的仓库**里也有，本仓库的检索不得把它带进来。

    判据是「读到的东西必须来自冻结 tip 的那棵对象树」：另一个仓库的对象库在这个进程里
    也许打得开，但它不是这次分析的版本 —— 混进来的后果是报告里出现一个别的项目的路径。
    """
    other = tmp_path / "other_game"
    other.mkdir()
    _git(other, "init", "-q")
    (other / "src").mkdir()
    (other / "src/legacy.lua").write_text(
        f"return {OLD_NAME}(1, 2)\n", encoding="utf-8"
    )
    other_tip = _commit(other, "另一个仓库里也有同名调用")

    provider = _provider(renames_only_the_definition)
    text = provider.find_references(OLD_NAME)

    assert "src/legacy.lua" not in text, "别的仓库的文件混进来了"
    assert other_tip[:12] not in text
    # 本仓库那条命中仍在。
    assert f"{CALLER}:3" in text


def test_the_reader_never_reads_a_second_repositorys_tree(
    renames_only_the_definition, tmp_path: Path
):
    """读取器只认自己被冻结的那棵树：拿另一个仓库的 tip 去问它，一个字节都读不到。"""
    other = tmp_path / "other_game"
    other.mkdir()
    _git(other, "init", "-q")
    (other / "src").mkdir()
    (other / "src/legacy.lua").write_text(f"{OLD_NAME}\n", encoding="utf-8")
    other_tip = _commit(other, "别的仓库")

    reader = FrozenTreeReader(_frozen(renames_only_the_definition))

    assert reader.tracked_paths() is not None
    assert "src/legacy.lua" not in reader.tracked_paths()
    assert reader.read_bytes("src/legacy.lua") is None
    # 别的仓库的提交号在本仓库的对象库里根本不存在（更谈不上读它）。
    assert reader.has_object(other_tip) is False


# ---------------------------------------------------------------------------
#  四、超索引上限只报覆盖不足，不报「无引用」
# ---------------------------------------------------------------------------


def test_an_index_page_limit_reports_insufficient_coverage_not_no_references(
    renames_only_the_definition,
):
    """**金标那条**：超过一页时只能说「已覆盖的这些里没有」，不能说「仓库里没有」。

    这条与「没搜到 ≠ 没搜完」是同一件事的另一半：这一页之外的文件**一个字都没读**。
    """
    # 让调用方落在**没有被索引**的那一页里（按目录分页：`scripts/combat/` 排在后面）。
    provider = _provider(renames_only_the_definition, repo_index_files=1)

    text = provider.find_references(OLD_NAME)

    assert "还没索引" in text, text
    assert "**覆盖不足**" in text
    assert "下一页起点" in text and "scripts/" in text, (
        "分页必须给出**可以照抄**的下一页范围，否则「还有没索引的」是句没法行动的话"
    )


def test_the_index_pages_by_directory_so_the_second_query_can_reach_it(
    renames_only_the_definition,
):
    """按目录分页：把 `path` 前缀写成下一页起点之后，那一段真的被索引到了。"""
    provider = _provider(renames_only_the_definition, repo_index_files=1)

    first = provider.find_references(OLD_NAME)
    second = provider.find_references(OLD_NAME, path="scripts/combat/")

    assert f"{CALLER}:3" not in first, (
        "第一页只索引了 scripts/net/ 那一个，不该看到调用方"
        "（看到了说明页大小没生效，这条用例就白测了）"
    )
    assert f"{CALLER}:3" in second, second


# ---------------------------------------------------------------------------
#  五、路径与凭证的拒绝（每条都断言**理由**）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad, keyword",
    [
        ("../../etc/passwd", ".."),
        ("/etc/passwd", "绝对路径"),
        ("C:\\Windows\\win.ini", "盘符"),
        ("~/.ssh/id_rsa", "家目录"),
    ],
)
def test_unsafe_paths_are_refused_with_a_reason(
    renames_only_the_definition, bad, keyword
):
    """越权路径必须被拒，且**理由里要含可照做的信息**（不是只回一句「拒绝」）。"""
    provider = _provider(renames_only_the_definition)

    text = provider.file_content(BATCH_COMMIT, bad, lines="1-5")

    assert text and "[路径被拒]" in text
    assert keyword in text, f"理由里没有点出问题所在：{text}"
    assert "root:" not in text and "hunter2" not in text, "拒了却还把内容带出来了"


def test_the_pure_path_judgement_is_the_only_implementation():
    """判据本身（纯函数）：通过的路径一定是**规范化后的相对路径**。"""
    assert resolve_repo_path("scripts\\net\\proto.lua")[0] == "scripts/net/proto.lua"
    assert resolve_repo_path("./scripts/net/proto.lua")[0] == "scripts/net/proto.lua"
    assert resolve_repo_path("//server/share/x")[1], "UNC 写法必须被拒"
    assert resolve_repo_path("")[1]
    assert resolve_repo_path("scripts/../../x")[1]


def test_credential_like_files_are_refused_with_a_reason(renames_only_the_definition):
    """凭证类文件**在跟踪树里也要拒**（拒绝理由是「凭证类」，不是「找不到」）。

    `.env` 是**真被提交进仓库**的（fixture 里就有一个），所以这条能区分
    「按凭证规则拒」与「碰巧不在跟踪树里」—— 少了这个区分，规则退化也测不出来。
    """
    provider = _provider(renames_only_the_definition)

    text = provider.file_content(BATCH_COMMIT, ".env", lines="1-2")

    assert "[路径被拒]" in text and "凭证" in text
    assert "hunter2" not in text
    assert exclusion_reason(".env") and exclusion_reason(".ssh/id_rsa")


def test_a_tracked_symlink_cannot_escape_the_tree(renames_only_the_definition):
    """**符号链接逃逸的反向样本**：被跟踪的软链接读出来是**链接目标文本**。

    Git 把软链接存成一个 mode 120000 的 blob，内容就是那个目标串 —— 所以即使仓库里
    有一个指向 `../../secret.txt` 的链接，读它也**只会得到那串路径**，不会读到仓库外的
    文件。这条断言钉的正是这个性质（而不是「我们碰巧没实现跟随」）。
    """
    repo = renames_only_the_definition
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo.path), input="../../secret.txt", capture_output=True,
        text=True, encoding="utf-8",
    )
    assert blob.returncode == 0, blob.stderr
    sha = blob.stdout.strip()
    _git(repo.path, "update-index", "--add", "--cacheinfo", f"120000,{sha},escape.lua")
    # **不能走 `_commit`**：它先跑 `git add -A`，而工作区里没有 `escape.lua` 这个文件
    # （软链接项只在索引里），`-A` 会把它从索引里删掉 —— 于是「nothing to commit」。
    _git(repo.path, "commit", "-q", "-m", "加一个指向仓库外的软链接")
    tip = _git(repo.path, "rev-parse", "HEAD").strip()
    (repo.path.parent / "secret.txt").write_text("TOP_SECRET\n", encoding="utf-8")

    reader = FrozenTreeReader(_frozen(repo, tip=tip))

    data = reader.read_bytes("escape.lua")
    assert data == b"../../secret.txt", data
    assert b"TOP_SECRET" not in data
    provider = _provider(repo, frozen_repository=_frozen(repo, tip=tip))
    text = provider.file_content(BATCH_COMMIT, "escape.lua", lines="1")
    assert "TOP_SECRET" not in text


# ---------------------------------------------------------------------------
#  六、拿不到仓库 → 「无法检索」，不是空命中
# ---------------------------------------------------------------------------


def test_a_missing_frozen_tip_says_unable_to_search_instead_of_returning_empty(
    renames_only_the_definition,
):
    """列不出跟踪树时**明确报「无法检索」** —— 空命中会被读成「没有调用方」。"""
    bogus = _frozen(renames_only_the_definition, tip="deadbeef" * 5)
    provider = _provider(renames_only_the_definition, frozen_repository=bogus)

    text = provider.find_references(OLD_NAME)

    assert text and text.strip()
    assert "[无法检索]" in text
    assert "没有在仓库范围内搜过" in text
    assert "这不等于「没有其它引用」" in text


def test_resolving_without_a_local_clone_reports_why():
    """没有本地工作副本时解析失败并**说明原因**（不抛、不假装有范围）。"""
    repository = SimpleNamespace(id=3, name="r", branch="main", type="git",
                                 clone_status="pending", last_synced_tip="a" * 40)

    frozen, reason = resolve_frozen_repository(repository, scope_commits=("a" * 40,))

    assert frozen is None
    assert "clone_status" in reason, reason


def test_no_frozen_scope_falls_back_to_the_batch_and_says_the_range_is_narrower(
    renames_only_the_definition, monkeypatch
):
    """没有冻结范围时退回本批次那条路，**并写明范围只有本批次**。"""
    import services.ai.platform_provider as pp
    from services.ai import reference_index as ri

    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: False)
    # 部署模式在**两个**模块里各被问一次：`_diff_from_agent` / `_content_from_agent`
    # 住在 `platform_provider`，而 `find_references` 那一段（2026-09-24 起）住在
    # `provider_search` —— 只打一处的话，另一条路会照旧按真实部署模式走。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: False)
    provider = PlatformContextProvider(
        loaded=SimpleNamespace(readable={}),
        scope=AnalysisScope(
            commits=(BATCH_COMMIT,),
            paths_by_commit={BATCH_COMMIT: frozenset({DEFINER})},
        ),
    )
    monkeypatch.setattr(
        provider, "_repository_of", lambda pairs: SimpleNamespace(id=1)
    )
    # 索引的读取口给出**只有定义处**的那份内容（调用方不在本批次里，所以搜不到）。
    monkeypatch.setattr(
        ri.SnapshotReferenceIndex,
        "build",
        classmethod(
            lambda cls, entries, **kwargs: cls(
                entries=tuple(entries),
                indexed_files=len(tuple(entries)),
                lines_by_path={DEFINER: (f"function M.{OLD_NAME}(atk, def)",)},
                lines_by_token={OLD_NAME.lower(): ((0, 1),)},
                missing_paths=frozenset(),
                binary_paths=frozenset(),
                oversized_paths=frozenset(),
                undecodable_paths=frozenset(),
                version="v",
                snapshot_digest="d",
            )
        ),
    )

    text = provider.find_references(OLD_NAME)

    assert "[范围说明]" in text
    assert "只覆盖本批次改动过的文件" in text
    assert "不能说成「仓库里没有引用」" in text


# ---------------------------------------------------------------------------
#  七、协议层：仓库范围的白名单
# ---------------------------------------------------------------------------


def _scope_with_batch() -> AnalysisScope:
    return AnalysisScope(
        commits=(BATCH_COMMIT,),
        paths_by_commit={BATCH_COMMIT: frozenset({DEFINER})},
        repository_ids_by_commit={BATCH_COMMIT: frozenset({7})},
    )


def test_sanitize_allows_a_tracked_path_outside_the_batch_when_repo_paths_are_given():
    """给了冻结范围之后：**不在本批次**的跟踪文件也能读（`commit` 甚至可以留空）。"""
    repo_paths = frozenset({DEFINER, CALLER, "scripts/net/util.lua"})

    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit="", path=CALLER, lines="1-20")],
        _scope_with_batch(),
        repo_paths=repo_paths,
    )

    assert [item.path for item in allowed] == [CALLER]
    assert dropped == ()
    assert allowed[0].commit == "", "读取走冻结版本，模型给的 commit 不再参与定范围"


def test_sanitize_rejects_a_path_outside_the_frozen_tree_with_a_reason():
    """不在跟踪树里的路径**不执行**，理由说清「不在本轮版本里」。"""
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit="", path="scripts/nope.lua")],
        _scope_with_batch(),
        repo_paths=frozenset({DEFINER, CALLER}),
    )

    assert allowed == ()
    assert len(dropped) == 1
    assert "不在本次冻结版本的 Git 跟踪文件里" in dropped[0].reason


def test_sanitize_rejects_an_unsafe_path_shape_before_the_tree_lookup():
    """绝对路径 / `..` 在**形状判据**这一关就被拒（理由比「不在跟踪树里」准确）。"""
    for bad in ("../../etc/passwd", "/etc/passwd", "C:\\Windows\\win.ini"):
        allowed, dropped = sanitize_requests(
            [ContextRequest(type="file_content", commit="", path=bad)],
            _scope_with_batch(),
            repo_paths=frozenset({DEFINER, CALLER}),
        )
        assert allowed == (), bad
        assert "路径不合法" in dropped[0].reason, (bad, dropped[0].reason)


def test_sanitize_keeps_the_batch_only_judgement_when_no_repo_paths_are_given():
    """**反向样本**：不给 `repo_paths` 时判据与从前逐字相同（本批次之外一律拒）。"""
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit=BATCH_COMMIT, path=CALLER)],
        _scope_with_batch(),
    )

    assert allowed == ()
    assert "不在 commit" in dropped[0].reason


def test_sanitize_allows_a_repo_prefix_for_find_references():
    """`find_references` 的前缀只要匹配到冻结版本里的文件就放行。"""
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="find_references", query=OLD_NAME, path="scripts/combat/")],
        _scope_with_batch(),
        repo_paths=frozenset({DEFINER, CALLER}),
    )

    assert [item.path for item in allowed] == ["scripts/combat/"]
    assert dropped == ()


def test_sanitize_still_rejects_a_prefix_that_matches_nothing():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="find_references", query=OLD_NAME, path="nope/")],
        _scope_with_batch(),
        repo_paths=frozenset({DEFINER, CALLER}),
    )

    assert allowed == ()
    assert "匹配不到" in dropped[0].reason


def test_the_scope_object_carries_the_tree_and_the_tip(renames_only_the_definition):
    """`RepoReadScope` 就是交给协议层的那份事实：路径集合 + 冻结版本。"""
    scope = build_read_scope(_frozen(renames_only_the_definition))

    assert scope.available
    assert scope.contains(CALLER) and not scope.contains("scripts/nope.lua")
    assert scope.matches_prefix("scripts/combat/")
    assert scope.frozen.tip == renames_only_the_definition.tip


def test_the_reader_reports_unknown_for_an_unresolvable_tree(
    renames_only_the_definition,
):
    """`is_tracked` 的三态：`None`（判不出来）不是 `False`（确定不在）。"""
    reader = FrozenTreeReader(_frozen(renames_only_the_definition, tip="f" * 40))

    assert reader.tracked_paths() is None
    assert reader.is_tracked(CALLER) is None


def test_search_returns_none_when_the_tree_cannot_be_listed(
    renames_only_the_definition,
):
    reader = FrozenTreeReader(_frozen(renames_only_the_definition, tip="f" * 40))

    assert search_frozen_repository(reader, OLD_NAME) is None


# ---------------------------------------------------------------------------
#  八、**同一轮**：一次 `requests` 数组里同时索取搜索与正文（金标的那句）
# ---------------------------------------------------------------------------


def _loaded_skills():
    """一份最小可用的 skill 装载结果（引擎要它来拼系统提示词与维度表）。"""
    from pathlib import Path as _Path

    from services.ai.skill_loader import LoadedSkills, SkillDocument

    def doc(name: str, text: str) -> SkillDocument:
        return SkillDocument(
            name=name, description="说明", path=_Path("/tmp") / name, text=text,
            content_hash="h-" + name,
        )

    return LoadedSkills(
        platform_skill=doc("version-diff-review", "# 角色与方法\n\n你是配表评审专家。\n"),
        platform_references=(doc("incident-checklist.md", "事故清单"),),
        project_manifest=doc("g119-knowledge", "# G119\n"),
        project_references=(),
        project_skills=(),
        readable={"incident-checklist.md": _Path("/tmp/a.md")},
        project_slug="g119",
        revision="rev-1",
    )


class _ScriptedClient:
    """按脚本回答的假模型（脚本跑完后重复最后一条）。"""

    def __init__(self, *replies: str):
        from services.ai.llm_client import ChatResult

        self._replies = list(replies)
        self._result = ChatResult
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, temperature=None):
        self.calls.append([dict(item) for item in messages])
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return self._result(text=self._replies[index], model="fake", prompt_tokens=10,
                            completion_tokens=5)


def test_one_round_carries_both_the_hits_and_the_frozen_window(
    renames_only_the_definition,
):
    """**金标那句**：模型把「搜索旧名」与「读已知未改文件的行窗」放进**同一个
    `requests` 数组**，服务端一轮执行完并按原顺序回传。

    断言的是**行为**：第 1 轮那一批里两条都执行了（不是一条被白名单拒掉），而第 2 轮
    发给模型的提示词里两段内容都在（回灌）。
    """
    import json as _json

    from services.ai.engine import run_analysis
    from services.ai.rules import RuleThresholds

    requests_payload = _json.dumps(
        {
            "status": "need_more_context",
            "reason": "改了公共函数，要确认调用方是否还在用旧名",
            "requests": [
                {"type": "find_references", "query": OLD_NAME},
                {
                    "type": "file_content",
                    "commit": BATCH_COMMIT,
                    "path": CALLER,
                    "lines": "1-6",
                },
            ],
        },
        ensure_ascii=False,
    )
    final_payload = _json.dumps(
        {
            "status": "final",
            "report_markdown": "# 变更理解\n\n伤害函数改名，调用方未同步。\n",
            "anomalies": [],
            "dimensions": [{"id": "module_coupling", "hit": True, "note": "调用方仍在用旧名"}],
        },
        ensure_ascii=False,
    )
    client = _ScriptedClient(requests_payload, final_payload)
    provider = _provider(renames_only_the_definition)

    outcome = run_analysis(
        client=client,
        provider=provider,
        loaded=_loaded_skills(),
        scope=AnalysisScope(
            commits=(BATCH_COMMIT,),
            paths_by_commit={BATCH_COMMIT: frozenset({DEFINER})},
            readable_references=frozenset({"incident-checklist.md"}),
        ),
        change_summary="本次变更共 1 个提交、1 个文件。\n",
        thresholds=RuleThresholds(),
    )

    first_round = outcome.rounds[0]
    assert first_round.request_count == 2, (
        f"有一半请求没有执行：{[(item.label, item.text[:80]) for item in first_round.executed]}"
        f"；被拒：{[item.reason for item in first_round.dropped]}"
    )
    labels = [item.label for item in first_round.executed]
    assert any("find_references" in label for label in labels), labels
    assert any(CALLER in label for label in labels), labels
    # 回灌：第 2 轮发给模型的内容里，命中位置与那段冻结正文都在。
    second_round_text = "\n".join(
        item["content"] for item in client.calls[1] if item["role"] == "user"
    )
    assert f"{CALLER}:3" in second_round_text
    assert f"冻结版本 {renames_only_the_definition.tip[:12]}" in second_round_text
