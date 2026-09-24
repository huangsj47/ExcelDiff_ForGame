# -*- coding: utf-8 -*-
"""一次分析覆盖多个仓库时，只读范围必须是**本项目全部仓库**。

## 这一组钉的是哪一次误拒

实测 run 57（G119 周版本，配置仓库 + 代码仓库同窗）：`ai_analysis_trace` 里 **7 条**被拒
请求逐条都是同一句「这个路径不在本次冻结版本的 Git 跟踪文件里（拼错、改过名，或在别的
仓库）」，其中 4 条是 `code/qz_*` —— 那两个文件在**代码仓库自己的 tip** 上确实被跟踪。
项目 2 有 3 个仓库，而 `PlatformContextProvider` 按仓库 id 升序只冻结了第 1 个
（配置仓库），于是代码仓库的文件全被判成「不在跟踪树里」。

还有第二条被同一句话盖住的误拒：周内真实存在、**tip 上已删除**的历史路径
（run 57 的 `config/150_shopping_mall/…`）。它同时被批次授权（`(commit, path)` 在白名单里），
却因为仓库范围的路径判据**排在**批次授权之前而被拒 —— 那条顺序是本组第 3 节钉的。

## 判据为什么不落在「有没有第二个仓库」上

「冻结了两个仓库」这句话用静态断言钉不住 —— 它只证明某个列表有两项，不证明**读得到**。
所以这里真的建两个 git 仓库，再走**协议层 → 取数层**这两道门：协议层放行、取数层读出
正文，两步都断言。只看协议层会漏掉「放行了但取数读的是另一个仓库」。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.ai.frozen_repo import (
    FrozenRepository,
    build_read_scope_multi,
    reset_caches,
)
from services.ai.platform_provider import PlatformContextProvider
from services.ai.protocol import ContextRequest, sanitize_requests
from services.ai.scope import AnalysisScope

# 两个仓库里**同名**的路径：它撑起「歧义不许猜」那一节。
SHARED = "config/item.lua"
# 只有代码仓库才有的路径：它撑起「多仓」那一节（run 57 的 code/qz_* 就是这个形状）。
CODE_ONLY = "code/server/reward.lua"
# 只有配置仓库才有的路径：坏仓库那一节要用它证明「坏掉的那个仓库的路径没混进来」。
CONFIG_ONLY = "config/skills.lua"
# 周内改过、tip 上已删除：它撑起「批次授权优先」那一节。
DELETED_AT_TIP = "config/retired/old_table.lua"

BATCH_COMMIT = "b" * 40
CONFIG_REPO_ID = 1
CODE_REPO_ID = 2


def _git(repo: Path, *args: str) -> str:
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
    reset_caches()
    yield
    reset_caches()


def _write(repo: Path, relative: str, text: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture()
def two_repos(tmp_path: Path):
    """两个真仓库：配置仓库 + 代码仓库，各含同名 `config/item.lua`。

    配置仓库的 tip 上**没有** `config/retired/old_table.lua`（它在更早的提交里，之后被删）
    —— 那是 run 57 里「周内真实存在、tip 已删除」那个形状。
    """
    config = tmp_path / "cfg"
    config.mkdir()
    _git(config, "init", "-q")
    _write(config, SHARED, "return { price = 180 }\n")
    _write(config, DELETED_AT_TIP, "return { retired = true }\n")
    _write(config, CONFIG_ONLY, "return { skills = {} }\n")
    older = _commit(config, "旧表还在")
    (config / DELETED_AT_TIP).unlink()
    _write(config, SHARED, "return { price = 190 }\n")
    config_tip = _commit(config, "皮甲涨价，旧表下线")

    code = tmp_path / "code"
    code.mkdir()
    _git(code, "init", "-q")
    _write(code, SHARED, "return { price = 999, note = '这一个是代码仓库的' }\n")
    _write(code, CODE_ONLY, "local M = {}\nfunction M.grant() end\nreturn M\n")
    code_tip = _commit(code, "发奖开关")

    return SimpleNamespace(
        config=config, config_tip=config_tip, config_older=older,
        code=code, code_tip=code_tip,
    )


def _frozen(repo: Path, repository_id: int, name: str, tip: str) -> FrozenRepository:
    return FrozenRepository(
        repository_id=repository_id, name=name, branch="main",
        tip=tip, local_path=str(repo), source="测试给定",
    )


def _provider(two_repos, *, changed_paths=None) -> PlatformContextProvider:
    """两个仓库都冻结，`_commit_row` 直接给定（测试环境没有那个库）。

    代码仓库里 `code/server/reward.lua` 在 tip 上存在、但**不在本批次** —— 那正是 run 57
    里被误拒的那一类。
    """
    config = _frozen(two_repos.config, CONFIG_REPO_ID, "cfg", two_repos.config_tip)
    code = _frozen(two_repos.code, CODE_REPO_ID, "code", two_repos.code_tip)
    scope = AnalysisScope(
        commits=(BATCH_COMMIT,),
        paths_by_commit={BATCH_COMMIT: frozenset(changed_paths or {SHARED})},
    )
    provider = PlatformContextProvider(
        loaded=SimpleNamespace(readable={}),
        frozen_repository=(config, code),
        scope=scope,
    )
    provider._commit_row = lambda commit, path: None
    return provider


# ==========================================================================
#  一、多仓：代码仓库里的**非批次**文件读得到
# ==========================================================================


def test_a_file_only_the_second_repository_tracks_is_allowed_and_readable(two_repos):
    """run 57 的主症状：`code/qz_*` 在代码仓库 tip 上存在，却因只冻结了配置仓库被拒。"""
    provider = _provider(two_repos)
    scope = provider._scope

    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit="", path=CODE_ONLY)],
        scope,
        repo_paths=provider.repo_tracked_paths(),
    )

    assert dropped == (), f"代码仓库跟踪的文件被拒了：{[item.reason for item in dropped]}"
    assert [item.path for item in allowed] == [CODE_ONLY]

    text = provider.file_content("", CODE_ONLY, "1-10")
    assert text and "function M.grant" in text, "协议层放行了，取数层却读不出正文"
    # 取自哪个仓库要写在回执里：同名路径在两个仓库里都存在，不写就分不清是哪一份。
    assert "code" in text


def test_the_union_of_tracked_paths_covers_both_repositories(two_repos):
    """`repo_tracked_paths()` 是**并集** —— 协议层拿的就是它。"""
    paths = _provider(two_repos).repo_tracked_paths()

    assert SHARED in paths
    assert CODE_ONLY in paths, "代码仓库的路径没进并集，协议层照样会拒它"


def test_a_path_no_repository_tracks_is_still_rejected(two_repos):
    """并集不是「什么都放行」：谁都不跟踪的路径照旧拒，且**不说**「没有引用」。"""
    provider = _provider(two_repos)

    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit="", path="code/nowhere.lua")],
        provider._scope,
        repo_paths=provider.repo_tracked_paths(),
    )

    assert allowed == ()
    assert len(dropped) == 1
    assert "不在本次冻结版本" in dropped[0].reason


def test_unsafe_shapes_and_credentials_are_still_rejected(two_repos):
    """范围变大**一步都没有**放宽形状与凭证两条判据（这是安全边界，不是功能）。"""
    provider = _provider(two_repos)

    for bad in ("../../../etc/passwd", "/etc/passwd", "C:/Windows/win.ini", ".env"):
        allowed, dropped = sanitize_requests(
            [ContextRequest(type="file_content", commit="", path=bad)],
            provider._scope,
            repo_paths=provider.repo_tracked_paths(),
        )
        assert allowed == (), f"{bad} 被放行了"
        assert dropped, f"{bad} 被静默丢弃（应当记账）"


def test_background_read_of_the_same_path_says_it_must_not_guess(two_repos):
    """同一条相对路径在两个仓库里都有 → **不替模型猜**，列出候选并给可操作的做法。"""
    provider = _provider(two_repos)

    text = provider.file_content("", SHARED, "1-10")

    assert text is not None
    assert "多个仓库" in text, text
    assert "cfg" in text and "code" in text, "没列出候选仓库，模型无从选择"
    assert "repository_id" in text, "没给出怎么指定的做法"


# ==========================================================================
#  二、部分冻结失败不许把整份范围关掉
# ==========================================================================


def test_one_unlistable_repository_does_not_disable_the_rest(two_repos):
    """一个仓库列不出跟踪树时，**其余照常可读** —— 局部故障不该放大成全面降级。"""
    good = _frozen(two_repos.code, CODE_REPO_ID, "code", two_repos.code_tip)
    broken = _frozen(two_repos.config, CONFIG_REPO_ID, "cfg", "0" * 40)

    scope = build_read_scope_multi((good, broken))

    assert scope.available, "一个仓库坏了就把整份判成不可用"
    assert scope.contains(CODE_ONLY)
    assert not scope.contains(CONFIG_ONLY), "坏仓库的路径不该混进来"
    assert not scope.contains(DELETED_AT_TIP), "tip 上已删除的路径不该出现在跟踪树里"
    assert CODE_REPO_ID in scope.candidates(CODE_ONLY)
    assert scope.note, "坏掉的仓库必须记一笔，否则读不到会被当成不存在"


def test_the_scope_identity_covers_every_repository(two_repos):
    """快照标识要**覆盖每个仓库的 tip**：代码仓库换了 tip 而配置仓库没换 = 快照变了。"""
    config = _frozen(two_repos.config, CONFIG_REPO_ID, "cfg", two_repos.config_tip)
    code = _frozen(two_repos.code, CODE_REPO_ID, "code", two_repos.code_tip)
    other_code = _frozen(two_repos.code, CODE_REPO_ID, "code", two_repos.config_older)

    base = build_read_scope_multi((config, code)).identity
    moved = build_read_scope_multi((config, other_code)).identity

    assert base and moved and base != moved, "只取第一个仓库的 tip，换了第二个也看不出来"


# ==========================================================================
#  三、批次授权优先于「当前 tip 清单」
# ==========================================================================


def test_a_batch_authorized_path_survives_the_current_tip_check(two_repos):
    """周内真实存在、**tip 上已删除**的路径：批次授权命中就该放行。

    这是 run 57 里 `config/150_shopping_mall/…` 那条误拒的形状 —— 它在周内提交里确实有，
    当前 tip 上已无路径。原来的顺序是「冻结范围的路径判据先返回」，批次授权连问都没问。
    """
    provider = _provider(two_repos, changed_paths={DELETED_AT_TIP, SHARED})
    scope = provider._scope
    assert not provider.repo_read_scope().contains(DELETED_AT_TIP), "前提：tip 上确实没有它"

    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit=BATCH_COMMIT, path=DELETED_AT_TIP)],
        scope,
        repo_paths=provider.repo_tracked_paths(),
    )

    assert dropped == (), f"批次已授权的历史路径仍被拒：{[item.reason for item in dropped]}"
    # 带着**解析后的批次提交**往下走：取数层据此读那条提交上的版本，而不是冻结 tip。
    assert allowed[0].commit == BATCH_COMMIT


def test_a_batch_commit_with_a_path_outside_the_batch_still_falls_back_to_the_tree(two_repos):
    """批次授权不命中、但路径在某个冻结仓库里 → 走背景核查那条路（**不是拒绝**）。"""
    provider = _provider(two_repos)

    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit=BATCH_COMMIT, path=CODE_ONLY)],
        provider._scope,
        repo_paths=provider.repo_tracked_paths(),
    )

    assert dropped == (), f"路径在仓库里却被拒：{[item.reason for item in dropped]}"
    assert [item.path for item in allowed] == [CODE_ONLY]


def test_without_a_frozen_scope_the_batch_only_judgement_is_unchanged(two_repos):
    """没有 `repo_paths` 时**逐字**沿用老判据（`sanitize_requests` 的既有契约）。"""
    provider = _provider(two_repos)

    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit=BATCH_COMMIT, path=CODE_ONLY)],
        provider._scope,
    )

    assert allowed == ()
    assert "不在 commit" in dropped[0].reason
    assert BATCH_COMMIT[:12] in dropped[0].reason
