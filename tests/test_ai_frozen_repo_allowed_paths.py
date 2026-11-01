# -*- coding: utf-8 -*-
"""冻结仓库读取的收口机制（WP17）+ `tables` 读取放开后的接线（WP20）。

## 机制（保留）

`FrozenTreeReader` 只要文件在 Git 跟踪树里就能读 **任意** 路径，`allowed_paths` 这一层
把它按**显式声明的路径集合**收窄：集合外回「不在本次分析范围」（不是「读不到」），
检索（`find_references`）走同一份收窄后的清单 —— 否则「先搜出范围外的命中位置，再按
位置去读」就是一条绕过的路；`allowed_paths=None` 时行为**逐字不变**。三条都要钉。

## WP20：生产路径不再声明集合，读取放开到全部跟踪文件

WP7/WP17 曾把 `allowed_paths` 当成 `profile=tables` 的收口点（「本批次配表仓库的那些
路径」）。用户指出配置表不是孤立的：表与表之间、配表与代码仓库之间都有关联 —— 这条收口
让未改动的引用方表与代码仓库的消费方代码全都读不到，跨表/跨仓库取证直接退化。WP20 之后
写侧 `tables` 给的是 `None`（`analysis_scope_filter.allowed_paths_for_profile`），所以：

* 代码仓库的路径**不再被拒**，未改动的配表文件同样可读（见「WP20 接线」一节）；
* 显式集合的机制用例仍在 —— 升级前落库的老 payload 里还带着集合，将来的手工收窄也靠它。

## 范围的口径是**路径集合**（不是仓库）

显式声明时，同一仓库里**没被声明**的路径也拒绝 —— 这是 WP17 的明确口径。WP20 不用
「放宽这一层」来实现读取放开，而是由写侧**不再声明**集合（`None` = 整棵跟踪树）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.ai.analysis_scope_filter import allowed_paths_for_profile
from services.ai.frozen_repo import (
    FrozenRepository,
    FrozenTreeReader,
    build_read_scope,
    build_read_scope_multi,
    out_of_scope_text,
    reset_caches,
    resolve_frozen_read,
)
from services.ai.platform_provider import PlatformContextProvider
from services.ai.protocol import ContextRequest, sanitize_requests
from services.ai.repo_reference import search_frozen_repository
from services.ai.scope import AnalysisScope
from tests.test_ai_frozen_repo_read_scope import _commit, _git

TABLE_REPO_ID = 7
CODE_REPO_ID = 8
BATCH_COMMIT = "b" * 40

#: 配表仓库里的两个文件：`.lua` 是**配表证据**（WP14 明确「tables 不按后缀筛」），
#: `.xlsx` 是配表本体。
TABLE_LUA = "config/CfgItem.lua"
TABLE_XLSX = "config/[30]道具表_CfgItem.xlsx"
CODE_LUA = "code/battle.lua"

#: 手写的一份**显式**允许集合（老 payload / 将来的人工收窄）。WP20 之后写侧 `tables`
#: 不再产出集合 —— 生产路径的可读范围是整棵跟踪树（`allowed_paths=None`）。
ALLOWED = frozenset({TABLE_LUA})

#: 两个仓库里都出现同一个符号：用来证明「检索也只搜允许的范围」。
SYMBOL = "CalcDamage"


@pytest.fixture(autouse=True)
def _clean_caches():
    """缓存键含 tip，但**跨用例的进程内缓存**会让「范围是否生效」变得看不出来。"""
    reset_caches()
    yield
    reset_caches()


def _make_repo(path, files: dict[str, bytes]) -> SimpleNamespace:
    path.mkdir()
    _git(path, "init", "-q")
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    tip = _commit(path, "初始提交")
    return SimpleNamespace(path=path, tip=tip, name=path.name)


@pytest.fixture()
def two_repos(tmp_path):
    """一个配表仓库 + 一个代码仓库（`profile=tables` 只允许读前者声明的路径）。"""
    table = _make_repo(
        tmp_path / "config_repo",
        {
            TABLE_LUA: (
                "local M = {}\n"
                f"function M.{SYMBOL}(atk, def)\n"
                "    return atk - def\n"
                "end\n"
                "return M\n"
            ).encode("utf-8"),
            TABLE_XLSX: b"PK\x03\x04 not a real workbook, but a tracked blob",
        },
    )
    code = _make_repo(
        tmp_path / "code_repo",
        {
            CODE_LUA: (
                "local proto = require('config.CfgItem')\n"
                f"return proto.{SYMBOL}(10, 2)\n"
            ).encode("utf-8"),
        },
    )
    return SimpleNamespace(table=table, code=code)


def _frozen(repo, repository_id: int, *, allowed=ALLOWED) -> FrozenRepository:
    return FrozenRepository(
        repository_id=repository_id,
        name=repo.name,
        branch="main",
        tip=repo.tip,
        local_path=str(repo.path),
        source="测试给定",
        allowed_paths=allowed,
    )


# --------------------------------------------------------------------------
# 一、读取入口：集合内可读、集合外回「不在本次分析范围」
# --------------------------------------------------------------------------


def test_a_path_inside_the_allowed_range_is_readable(two_repos):
    reader = FrozenTreeReader(_frozen(two_repos.table, TABLE_REPO_ID))

    assert reader.restricted
    data, why = reader.read_within_limit(TABLE_LUA)

    assert data is not None and SYMBOL.encode() in data
    assert why == ""


def test_an_allowed_path_may_be_a_lua_inside_the_table_repository(two_repos):
    """**tables 不按后缀筛**（WP14）：配表仓库里的 `.lua` 是配表证据，必须读得到。"""
    reader = FrozenTreeReader(_frozen(two_repos.table, TABLE_REPO_ID))

    assert TABLE_LUA in reader.tracked_paths()
    assert reader.read_bytes(TABLE_LUA) is not None


def test_a_path_outside_the_allowed_set_is_refused_even_if_the_tree_tracks_it(two_repos):
    """显式集合的口径是**路径集合**，不是「整个配表仓库」：没被声明的路径也拒绝。

    拒绝理由必须说「不在本次分析范围」——说成「读不到 / 不存在 / 不在跟踪树」会让模型
    去核对拼写（那是永远改不对的），或者把平台的范围决定写成平台故障。
    """
    reader = FrozenTreeReader(_frozen(two_repos.table, TABLE_REPO_ID))

    assert TABLE_XLSX in reader.all_tracked_paths()
    assert TABLE_XLSX not in reader.tracked_paths()
    assert reader.out_of_scope(TABLE_XLSX) is True
    assert reader.read_bytes(TABLE_XLSX) is None
    _, why = reader.read_within_limit(TABLE_XLSX)
    assert "不在本次分析范围" in why
    # **必须与「读不到 / 不存在」分开**：否则模型会去核对拼写、或者写成平台故障。
    assert "这不是「读不到」" in why
    assert "这个文件不存在" in why
    assert "不在跟踪树" not in why, f"把范围外说成了「不在跟踪树里」：{why}"


def test_an_empty_allowed_set_denies_everything(two_repos):
    """空列表 = **一条都不许**（与 `change_set._allowed_path_set` 同一语义）。"""
    reader = FrozenTreeReader(_frozen(two_repos.table, TABLE_REPO_ID, allowed=[]))

    assert reader.restricted
    assert reader.read_bytes(TABLE_LUA) is None
    _, why = reader.read_within_limit(TABLE_LUA)
    assert "不在本次分析范围" in why
    assert "只有 0 条路径" in why


def test_the_reason_text_itself_denies_being_a_read_failure():
    """文案本身可单独断言（两个入口共用它：`read_within_limit` 与 `resolve_frozen_read`）。"""
    text = out_of_scope_text(CODE_LUA, ALLOWED)

    assert CODE_LUA in text
    assert "只有 1 条路径" in text
    assert "这不是「读不到」" in text


# --------------------------------------------------------------------------
# 二、WP20 接线：`tables` 不再声明集合 → 代码仓库 / 未改动配表文件都可读
# --------------------------------------------------------------------------


def test_the_tables_profile_no_longer_declares_a_path_set(two_repos):
    """**WP20 的金标**：`tables` 的读取范围是整棵跟踪树，代码仓库不再被拒。

    把两层接起来看：`allowed_paths_for_profile("tables", ...)` 返回 `None`（写侧口径），
    `FrozenTreeReader` 拿到 `None` 就不按路径集合收窄 —— 于是：

    * `TABLE_XLSX`（配表仓库内、**不在本批次改动里**）读得到 → 跨表引用取证；
    * `CODE_LUA`（代码仓库）读得到 → 谁消费这张表、枚举/协议怎么定义。
    """
    declared = allowed_paths_for_profile("tables", [{"file_path": TABLE_XLSX}])
    assert declared is None, "tables 又按 delta 收窄了读取范围"

    table_reader = FrozenTreeReader(
        _frozen(two_repos.table, TABLE_REPO_ID, allowed=declared)
    )
    code_reader = FrozenTreeReader(_frozen(two_repos.code, CODE_REPO_ID, allowed=declared))

    assert table_reader.restricted is False
    assert table_reader.read_bytes(TABLE_XLSX) is not None, (
        "未改动的配表仓库文件读不到 —— 跨表引用取证退化"
    )

    assert code_reader.out_of_scope(CODE_LUA) is False
    data, why = code_reader.read_within_limit(CODE_LUA)
    assert data is not None and SYMBOL.encode() in data, "代码仓库的消费方读不到"
    assert why == ""

    # 检索（`find_references`）同样放开：要在代码仓库里搜得到这张表的消费方。
    outcome = search_frozen_repository(code_reader, SYMBOL)
    assert outcome is not None and f"{CODE_LUA}:2" in outcome.text


# --------------------------------------------------------------------------
# 三、`allowed_paths=None`：旧行为逐字不变（兼容口径；也是现在 `tables` 的取值）
# --------------------------------------------------------------------------


def test_allowed_paths_none_keeps_the_old_unrestricted_reading(two_repos):
    reader = FrozenTreeReader(_frozen(two_repos.code, CODE_REPO_ID, allowed=None))

    assert reader.allowed_paths is None
    assert reader.restricted is False
    assert reader.out_of_scope(CODE_LUA) is False
    assert reader.read_bytes(CODE_LUA) is not None
    assert CODE_LUA in reader.tracked_paths()


def test_a_missing_allowed_paths_key_behaves_like_none(two_repos):
    """冻结对象**没有**这个字段（老 payload / 老调用点）时也是不限制。"""
    frozen = FrozenRepository(
        repository_id=CODE_REPO_ID,
        name=two_repos.code.name,
        branch="main",
        tip=two_repos.code.tip,
        local_path=str(two_repos.code.path),
        source="测试给定",
    )
    reader = FrozenTreeReader(frozen)

    assert reader.allowed_paths is None
    assert reader.read_bytes(CODE_LUA) is not None
    scope = build_read_scope(frozen)
    assert scope.allowed_paths is None
    assert scope.restricted is False
    assert resolve_frozen_read(scope, (reader,), CODE_LUA).ok


# --------------------------------------------------------------------------
# 四、范围对象与读取决策：显式集合时拒绝理由来自范围，不是来自跟踪树
# --------------------------------------------------------------------------


def test_the_read_scope_carries_both_the_tree_and_the_allowed_set(two_repos):
    """`paths` 是「仓库里有没有它」，`allowed_paths` 是「这次准不准读它」——两个维度。"""
    scope = build_read_scope(_frozen(two_repos.table, TABLE_REPO_ID))

    assert scope.paths, "paths 收窄成 allowed 会让协议层把范围外说成「不在跟踪树里」"
    assert TABLE_XLSX in scope.paths
    assert scope.allowed_paths == ALLOWED
    assert scope.out_of_scope(TABLE_XLSX) is True
    assert scope.out_of_scope(TABLE_LUA) is False


def test_resolve_frozen_read_refuses_out_of_scope_before_the_tree_lookup(two_repos):
    table_frozen = _frozen(two_repos.table, TABLE_REPO_ID)
    code_frozen = _frozen(two_repos.code, CODE_REPO_ID)
    readers = (FrozenTreeReader(table_frozen), FrozenTreeReader(code_frozen))
    scope = build_read_scope_multi((table_frozen, code_frozen))

    refused = resolve_frozen_read(scope, readers, CODE_LUA)
    allowed = resolve_frozen_read(scope, readers, TABLE_LUA)

    assert refused.ok is False
    assert "不在本次分析范围" in refused.refusal
    assert allowed.ok is True
    assert allowed.path == TABLE_LUA


def test_the_multi_repository_union_does_not_close_an_undeclared_repository(two_repos):
    """一个仓库声明了范围、另一个没声明：没声明的那个按「整棵树」算（原义）。"""
    restricted = _frozen(two_repos.table, TABLE_REPO_ID)
    unrestricted = _frozen(two_repos.code, CODE_REPO_ID, allowed=None)
    scope = build_read_scope_multi((restricted, unrestricted))

    assert scope.allowed_paths is not None
    assert TABLE_LUA in scope.allowed_paths
    assert CODE_LUA in scope.allowed_paths, "没声明范围的仓库被整个关掉了"


# --------------------------------------------------------------------------
# 五、检索（find_references）：显式集合时只索引允许的范围
# --------------------------------------------------------------------------


def test_the_frozen_search_only_indexes_paths_inside_the_allowed_range(two_repos):
    """`find_references` 与正文走**同一份**清单：范围外一个字都不读。"""
    table_reader = FrozenTreeReader(_frozen(two_repos.table, TABLE_REPO_ID))
    code_reader = FrozenTreeReader(_frozen(two_repos.code, CODE_REPO_ID))

    table_outcome = search_frozen_repository(table_reader, SYMBOL)
    code_outcome = search_frozen_repository(code_reader, SYMBOL)

    assert table_outcome is not None
    assert f"{TABLE_LUA}:2" in table_outcome.text
    assert code_outcome is not None
    assert CODE_LUA not in code_outcome.text, "范围外的命中位置被搜出来了"
    assert code_reader.tracked_paths() == ()


# --------------------------------------------------------------------------
# 六、协议层：第二个入口同样按 allowed_paths 兜底（显式集合）
# --------------------------------------------------------------------------


def _batch_scope() -> AnalysisScope:
    return AnalysisScope(
        commits=(BATCH_COMMIT,),
        paths_by_commit={BATCH_COMMIT: frozenset({TABLE_LUA, CODE_LUA})},
    )


def test_the_protocol_drops_an_out_of_scope_path_with_the_range_reason():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit=BATCH_COMMIT, path=CODE_LUA)],
        _batch_scope(),
        repo_paths=frozenset({TABLE_LUA, CODE_LUA}),
        allowed_paths=ALLOWED,
    )

    assert allowed == ()
    assert len(dropped) == 1
    assert "不在本次分析范围" in dropped[0].reason
    assert "不在跟踪树" not in dropped[0].reason


def test_the_protocol_allows_an_in_range_path():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit=BATCH_COMMIT, path=TABLE_LUA)],
        _batch_scope(),
        repo_paths=frozenset({TABLE_LUA, CODE_LUA}),
        allowed_paths=ALLOWED,
    )

    assert dropped == ()
    assert [item.path for item in allowed] == [TABLE_LUA]


def test_the_protocol_without_allowed_paths_keeps_the_old_behaviour():
    """**反向样本**：不给 `allowed_paths` 时判据与从前逐字相同（冻结范围放行）。"""
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_content", commit=BATCH_COMMIT, path=CODE_LUA)],
        _batch_scope(),
        repo_paths=frozenset({TABLE_LUA, CODE_LUA}),
    )

    assert dropped == ()
    assert [item.path for item in allowed] == [CODE_LUA]


def test_the_protocol_rejects_a_find_references_prefix_outside_the_range():
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="find_references", query=SYMBOL, path="code/")],
        _batch_scope(),
        repo_paths=frozenset({TABLE_LUA, CODE_LUA}),
        allowed_paths=ALLOWED,
    )

    assert allowed == ()
    assert len(dropped) == 1
    assert "不在本次分析范围" in dropped[0].reason


# --------------------------------------------------------------------------
# 七、生产入口：`PlatformContextProvider` 把范围传进冻结读取
#
# 写侧 `tables` 现在给 `None`（整棵跟踪树），所以 provider 在生产路径上不再拒绝代码仓库；
# 显式集合那一条仍保留 —— 老 payload 里带着集合时，这一层必须照旧拒绝。
# --------------------------------------------------------------------------


def _provider(two_repos, *, allowed=ALLOWED) -> PlatformContextProvider:
    provider = PlatformContextProvider(
        loaded=SimpleNamespace(readable={}),
        scope=AnalysisScope(
            commits=(BATCH_COMMIT,),
            paths_by_commit={BATCH_COMMIT: frozenset({TABLE_LUA})},
        ),
        frozen_repository=(
            _frozen(two_repos.table, TABLE_REPO_ID, allowed=allowed),
            _frozen(two_repos.code, CODE_REPO_ID, allowed=allowed),
        ),
        allowed_paths=allowed,
    )
    # 「本批次里没有这个路径」那一步直接给定（真机上它是 `commits_log` 查不到行）。
    provider._commit_row = lambda commit, path, repository_id="": (None, ())
    # 表头坐标那条路要查库；测试没有 app 上下文，直接给定「查不到」——
    # 不替换掉会刷一条「Working outside of application context」的告警日志，
    # 而噪音会盖住真正的失败信号（与既有冻结仓库用例同一手法）。
    provider._repository_row = lambda repository_id: None
    return provider


def test_the_provider_reads_the_table_repository_inside_the_range(two_repos):
    text = _provider(two_repos).file_content(BATCH_COMMIT, TABLE_LUA, lines="1-3")

    assert text is not None
    assert SYMBOL in text
    assert "冻结版本" in text


def test_an_explicit_range_still_makes_the_provider_refuse_a_code_repository_path(two_repos):
    """**显式集合的机制**：老 payload（升级前写入）带着集合时，provider 这一层仍拒绝。

    WP20 之后写侧不再产出集合（生产路径见下面那条「放行」用例），但已经落库的请求
    payload 里还带着它 —— 这些运行必须按当时的声明执行。
    """
    text = _provider(two_repos).file_content(BATCH_COMMIT, CODE_LUA, lines="1-3")

    assert text is not None
    assert "不在本次分析范围" in text
    assert f"return proto.{SYMBOL}" not in text


def test_the_provider_reads_the_code_repository_when_no_range_is_declared(two_repos):
    """**WP20 的生产口径**：`allowed_paths=None`（现在的 `tables`）→ 代码仓库的消费方可读。

    输入仍只含配表仓库的改动（provider 的 `scope.paths_by_commit` 里没有 `CODE_LUA`），
    但冻结读取按整棵跟踪树放行 —— 这正是本轮要拿到的「配表-代码关联证据」。
    """
    text = _provider(two_repos, allowed=None).file_content(BATCH_COMMIT, CODE_LUA, lines="1-3")

    assert text is not None
    assert f"return proto.{SYMBOL}" in text
    assert "不在本次分析范围" not in text


def test_the_provider_without_allowed_paths_keeps_reading_any_tracked_file(two_repos):
    """`None` 与「缺键」逐字相同（旧行为兼容）—— 这条不依赖上面那条的显式参数。"""
    provider = PlatformContextProvider(
        loaded=SimpleNamespace(readable={}),
        scope=AnalysisScope(
            commits=(BATCH_COMMIT,),
            paths_by_commit={BATCH_COMMIT: frozenset({TABLE_LUA})},
        ),
        frozen_repository=(
            _frozen(two_repos.table, TABLE_REPO_ID, allowed=None),
            _frozen(two_repos.code, CODE_REPO_ID, allowed=None),
        ),
    )
    provider._commit_row = lambda commit, path, repository_id="": (None, ())
    provider._repository_row = lambda repository_id: None

    text = provider.file_content(BATCH_COMMIT, CODE_LUA, lines="1-3")

    assert text is not None
    assert f"return proto.{SYMBOL}" in text
    assert "不在本次分析范围" not in text


# --------------------------------------------------------------------------
# 八、分仓白名单（P1-3）：同名相对路径不许跨仓库借用另一份白名单
#
# 白名单是**相对路径**集合，本身没有仓库身份。两个仓库都有 `config/items.json` 时，
# 把同一份扁平集合交给每个 `FrozenTreeReader`，代码仓库那条同名路径就会因为字符串相同
# 被放行 —— 但它属于另一个仓库的改动，不在本次读取范围内。
# --------------------------------------------------------------------------

SAME_PATH = "config/items.json"
TABLE_CONTENT = b'{"owner": "table"}'
CODE_CONTENT = b'{"owner": "code"}'


def _same_path_repos(tmp_path) -> SimpleNamespace:
    """两个仓库各自跟踪**同一个相对路径**（P1-3 的触发形态）。"""
    table = _make_repo(tmp_path / "config_repo", {SAME_PATH: TABLE_CONTENT})
    code = _make_repo(tmp_path / "code_repo", {SAME_PATH: CODE_CONTENT})
    return SimpleNamespace(table=table, code=code)


def _provider_for_same_path(tmp_path, **provider_kwargs) -> PlatformContextProvider:
    repos = _same_path_repos(tmp_path)
    provider = PlatformContextProvider(
        loaded=SimpleNamespace(readable={}),
        scope=AnalysisScope(
            commits=(BATCH_COMMIT,),
            paths_by_commit={BATCH_COMMIT: frozenset({SAME_PATH})},
        ),
        frozen_repository=(
            _frozen(repos.table, TABLE_REPO_ID, allowed=None),
            _frozen(repos.code, CODE_REPO_ID, allowed=None),
        ),
        **provider_kwargs,
    )
    provider._commit_row = lambda commit, path, repository_id="": (None, ())
    provider._repository_row = lambda repository_id: None
    return provider


def test_a_per_repository_range_does_not_leak_the_same_path_to_another_repository(tmp_path):
    """**P1-3 的核心断言**：显式分仓白名单只对声明它的仓库生效。

    配表仓库读得到自己那批改动里的 `config/items.json`；代码仓库同样跟踪这个相对路径，
    但白名单里**没有它** —— 必须回「不在本次分析范围」，而不是拿同名路径借道。
    """
    provider = _provider_for_same_path(
        tmp_path, allowed_paths_by_repo={TABLE_REPO_ID: [SAME_PATH]}
    )

    table_text = provider.file_content(
        BATCH_COMMIT, SAME_PATH, lines="1-1", repository_id=str(TABLE_REPO_ID)
    )
    code_text = provider.file_content(
        BATCH_COMMIT, SAME_PATH, lines="1-1", repository_id=str(CODE_REPO_ID)
    )

    assert table_text is not None and TABLE_CONTENT.decode() in table_text
    assert code_text is not None
    assert "不在本次分析范围" in code_text, (
        f"代码仓库借同名路径穿过了配表仓库的白名单：{code_text}"
    )
    assert CODE_CONTENT.decode() not in code_text


def test_the_per_repository_range_wins_over_the_flat_one(tmp_path):
    """分仓声明存在时**不再退回扁平集合** —— 退回就是 P1-3 本身。

    扁平集合（老 payload / 未来 strict）里那条路径同时匹配两个仓库；只按它放行等于
    「有仓库身份的白名单建好了却不用」。映射里没有的仓库 = 没有为它声明任何路径。
    """
    provider = _provider_for_same_path(
        tmp_path,
        allowed_paths=[SAME_PATH],
        allowed_paths_by_repo={TABLE_REPO_ID: [SAME_PATH]},
    )

    table_text = provider.file_content(
        BATCH_COMMIT, SAME_PATH, lines="1-1", repository_id=str(TABLE_REPO_ID)
    )
    code_text = provider.file_content(
        BATCH_COMMIT, SAME_PATH, lines="1-1", repository_id=str(CODE_REPO_ID)
    )

    assert table_text is not None and TABLE_CONTENT.decode() in table_text
    assert code_text is not None and "不在本次分析范围" in code_text
    assert CODE_CONTENT.decode() not in code_text


def test_an_old_payload_without_a_per_repository_map_keeps_the_flat_range(tmp_path):
    """老 payload（**没有分仓键**）逐字回到旧行为：扁平集合对每个仓库都生效。

    升级前落库的请求里只有 `allowed_paths`；它们必须按当时的声明执行，不能因为新代码
    多了一个键就把那些运行收窄（那会让本来能读的路径变成「不在本次分析范围」）。
    """
    provider = _provider_for_same_path(tmp_path, allowed_paths=[SAME_PATH])

    code_text = provider.file_content(
        BATCH_COMMIT, SAME_PATH, lines="1-1", repository_id=str(CODE_REPO_ID)
    )

    assert code_text is not None
    assert CODE_CONTENT.decode() in code_text
    assert "不在本次分析范围" not in code_text
