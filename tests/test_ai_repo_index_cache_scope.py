# -*- coding: utf-8 -*-
"""WP21：仓库索引缓存键必须带上**读取范围**（审查报告 P1-2）。

## 钉的是一句静默的错答案

`repo_reference.search_frozen_repository` 的索引缓存（`_INDEX_CACHE`，测试里用共享的
`cache={}` 代替）是**进程级**的：不同请求、不同调用点共用同一份。而索引条目来自
`reader.tracked_paths()` —— 那一层已经按 `allowed_paths` 过滤过，所以「同一仓库 /
同一 tip / 同一前缀」下，全量 reader 与受限 reader 要建的是**两份不同的索引**。

缓存键漏掉范围时，两种顺序都会出错，而且都不报错：

* 先全量建索引、再受限搜 → 受限那次命中全量索引，**把范围外的代码路径搜了出来**（越界）；
* 先受限建索引、再全量搜 → 全量那次复用受限索引，**代码路径根本没被搜**（漏证据），
  而回执还会说「覆盖了该范围内的全部跟踪文件」。

两条都断言**结果文本里的命中位置**，并用 `files_total`（分母）与缓存条目数钉住
「到底建了几份索引」—— 只断言「没抛异常」的话，两种顺序都能过。

## 为什么是同一个仓库里的两条路径

范围是**路径集合**（`allowed_paths`），不是仓库。同一个冻结 tip 上，
`allowed_paths={配表路径}` 把 `tracked_paths()` 收窄成一条；两个 reader 共享同一个
`FrozenRepository`，仓库 ID 与 tip 也相同 —— 缓存键里除范围外的每一项都撞在一起，
正是要复现的形态（跨仓库的键本来就不同，测不出这个洞）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.ai.frozen_repo import FrozenRepository, FrozenTreeReader, reset_caches
from services.ai.repo_reference import (
    MAX_REPO_INDEX_FILES,
    _index_key,
    reset_index_cache,
    search_frozen_repository,
)
from tests.test_ai_frozen_repo_read_scope import _commit, _git

REPO_ID = 7
#: 配表路径：受限 reader 只有这一条。
TABLE_LUA = "config/CfgItem.lua"
#: 代码路径：受限 reader **不许**看到它（同一条冻结树里的另一条路径）。
CODE_LUA = "code/battle.lua"
SYMBOL = "CalcDamage"
ALLOWED = frozenset({TABLE_LUA})
INDEX_VERSION = "repo-reference-index/v1"


@pytest.fixture(autouse=True)
def _clean_caches():
    """进程内缓存会跨用例复用；不清掉的话「这一条到底有没有命中缓存」看不出来。"""
    reset_caches()
    reset_index_cache()
    yield
    reset_caches()
    reset_index_cache()


@pytest.fixture()
def repo(tmp_path):
    """一个真 git 仓库：配表路径与代码路径各一条，都含同一个符号。"""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    files = {
        TABLE_LUA: (
            "local M = {}\n"
            f"function M.{SYMBOL}(atk, def)\n"
            "    return atk - def\n"
            "end\n"
            "return M\n"
        ).encode("utf-8"),
        CODE_LUA: (
            "local proto = require('config.CfgItem')\n"
            f"return proto.{SYMBOL}(10, 2)\n"
        ).encode("utf-8"),
    }
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    tip = _commit(path, "初始提交")
    return SimpleNamespace(path=path, tip=tip, name=path.name, files=files)


@pytest.fixture()
def frozen(repo):
    """两个 reader 共享的冻结对象：仓库 ID 与 tip 相同，只有读取范围不同。"""
    return FrozenRepository(
        repository_id=REPO_ID,
        name=repo.name,
        branch="main",
        tip=repo.tip,
        local_path=str(repo.path),
        source="测试给定",
    )


def _search(reader, cache):
    outcome = search_frozen_repository(reader, SYMBOL, cache=cache)
    assert outcome is not None
    return outcome


# --------------------------------------------------------------------------
#  一、先全量、后受限：受限那次不得复用全量索引（否则越界）
# --------------------------------------------------------------------------


def test_an_index_built_from_the_full_tree_is_not_reused_by_a_restricted_search(frozen):
    """**顺序 A**：全量建索引 → 受限搜。命中里不许出现范围外的代码路径。"""
    unrestricted = FrozenTreeReader(frozen, allowed_paths=None)
    restricted = FrozenTreeReader(frozen, allowed_paths=ALLOWED)
    cache: dict = {}

    full = _search(unrestricted, cache)
    scoped = _search(restricted, cache)

    assert f"{CODE_LUA}:2:" in full.text, "全量那次本来就该搜到代码路径"
    assert CODE_LUA not in scoped.text, (
        "受限那次复用了全量索引：范围外的代码路径被搜了出来（越界）"
    )
    assert f"{TABLE_LUA}:2:" in scoped.text, "配表路径在范围内，命中不许丢"
    assert (scoped.files_total, scoped.indexed) == (1, 1), (
        f"受限索引的分母应当只有 1 条，实际 {scoped.files_total} 条 —— 复用了全量索引"
    )
    assert len(cache) == 2, "两个范围必须是两条缓存条目，而不是共用一条"


# --------------------------------------------------------------------------
#  二、先受限、后全量：全量那次必须重新建索引（否则漏搜）
# --------------------------------------------------------------------------


def test_an_index_built_from_a_restricted_scope_is_not_reused_by_the_full_search(frozen):
    """**顺序 B**：受限建索引 → 全量搜。代码路径必须被搜到（不能复用受限索引）。"""
    restricted = FrozenTreeReader(frozen, allowed_paths=ALLOWED)
    unrestricted = FrozenTreeReader(frozen, allowed_paths=None)
    cache: dict = {}

    scoped = _search(restricted, cache)
    full = _search(unrestricted, cache)

    assert CODE_LUA not in scoped.text, "受限那次就不该出现代码路径"
    assert f"{CODE_LUA}:2:" in full.text, (
        "全量那次复用了受限索引：代码路径根本没被搜（漏证据）"
    )
    assert (full.files_total, full.indexed) == (2, 2), (
        f"全量索引的分母应当有 2 条，实际 {full.files_total} 条 —— 复用了受限索引"
    )
    assert len(cache) == 2, "两个范围必须是两条缓存条目，而不是共用一条"


# --------------------------------------------------------------------------
#  三、`None`（不限制）与空集合（一条都不许）不是同一格
# --------------------------------------------------------------------------


def test_an_empty_scope_is_keyed_apart_from_no_scope(frozen):
    """把空集合折成 `None` 是一处安全口子：受限 reader 会直接吃到全量索引。"""
    no_scope = _index_key(frozen, "", MAX_REPO_INDEX_FILES, INDEX_VERSION, None)
    empty_scope = _index_key(frozen, "", MAX_REPO_INDEX_FILES, INDEX_VERSION, frozenset())

    assert no_scope != empty_scope, "空集合是「一条都不许」，不能与「不限制」同键"

    cache: dict = {}
    full = _search(FrozenTreeReader(frozen, allowed_paths=None), cache)
    empty = _search(FrozenTreeReader(frozen, allowed_paths=()), cache)

    assert f"{CODE_LUA}:2:" in full.text
    assert (empty.files_total, empty.indexed) == (0, 0), (
        "空范围复用了全量索引 —— 「一条都不许」的范围读到了内容"
    )
    assert len(cache) == 2


# --------------------------------------------------------------------------
#  四、没有 `allowed_paths` 属性的老 reader：按 `None`（旧行为逐字不变）
# --------------------------------------------------------------------------


class _LegacyReader:
    """老调用点的最小读取器：只有 `tracked_paths()` / `read_bytes()`。"""

    def __init__(self, frozen, files):
        self.frozen = frozen
        self._files = dict(files)

    def tracked_paths(self):
        return tuple(self._files)

    def read_bytes(self, path):
        return self._files.get(path)


def test_a_reader_without_the_allowed_paths_attribute_keeps_the_old_behaviour(frozen, repo):
    """缺属性按 `None`：老 reader 与「不限制」共用同一格缓存，不会各自建一份。"""
    cache: dict = {}

    full = _search(FrozenTreeReader(frozen, allowed_paths=None), cache)
    legacy = _search(_LegacyReader(frozen, repo.files), cache)

    assert f"{CODE_LUA}:2:" in full.text
    assert f"{CODE_LUA}:2:" in legacy.text, "老 reader 的索引不该被范围键区别对待"
    assert legacy.files_total == 2
    assert len(cache) == 1, (
        "没有 allowed_paths 的老 reader 应当落在 None 那一格（旧行为），"
        f"实际建了 {len(cache)} 条缓存"
    )
