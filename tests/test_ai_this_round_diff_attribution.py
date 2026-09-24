# -*- coding: utf-8 -*-
"""预取给模型的「本次差异」，必须来自**本轮 delta 的那条提交**。

## 这一条钉的是哪一次错归因

实测 run 55：job 请求增量，唯一的 `delta_file` 是 `config/物品表.xlsx@159b068`
（`diff_base_commit_id=baf3148`），真实增量是**皮甲 180 → 190**。Git 两端文件、差异缓存
与只读重放三方一致。而报告四次写「本次差异是铁剑 200 → 260」（那是**上一笔**
`baf3148` 的改动），还据此编了一条「提交信息与差异不一致」的流程风险。

机理不在模型，在平台：第 1 轮预取把 **`file_diff(baf3148, 物品表)`** 当成「本次输入的
改动文件的差异」交给了它，而那份文本里**连一句出处都没有**（没有增量基线，落库那一份
也没合并多条提交，于是 `_batch_provenance` 无话可说）—— 读起来就是「本次改了什么」。

## 错在哪一行

`AnalysisScope.commits` 的构造顺序是「**本轮输入**的提交在前，窗口里其余的在后」
（`change_set.build`），而 `commit_of_path` 原先按「遍历取最后一个匹配」判「最新」——
两者一撞，**本轮输入的文件反而最容易取到窗口里更早的那条提交**：

    commits        = (159b068, 2e9e53cb, 59e435b3, b8dd9ed2, baf3148)   ← 159b068 在最前
    物品表 的匹配  =  159b068            …            baf3148            ← 取最后一个
    commit_of_path = baf3148   ✗（应为 159b068）

所以这里**不用「谁排在后面」这种间接判据**，直接断言「取到的是写侧为这次输入冻结下来
的那条提交」，并同时钉住「窗口里更早那条提交也在 `commits` 里」这个前提 —— 少了它，
这条用例在回归时会**假绿**（旧代码也能过）。
"""
from __future__ import annotations

from services.ai.change_set import from_weekly_payload
from services.ai.evidence_prefetch import diff_requests
from services.ai.scope import AnalysisScope

TABLE = "config/物品表.xlsx"
SKILLS = "config/技能表.xlsx"

# run 55 的真实形状：本轮输入的只有 TABLE，它这次落在 NEW_COMMIT 上；
# OLD_COMMIT 是**上一笔**（也在窗口里，也改过 TABLE），且排在 `commits` 的后面。
NEW_COMMIT = "159b0682413b502a9aa83daf801882e2b6eb63c9"
OLD_COMMIT = "baf314817d7edecabbe49e43e49576ad94c1fdcf"
BASE_COMMIT = "59e435b354ee2c5086f2e9c8e25670362f3b62e9"
PLAIN_COMMIT = "b8dd9ed22b39b564dfb5be7c72e24e622d80d71c"
FIRST_COMMIT = "2e9e53cb44011bc40faaf37361b116a6edefc2dd"


def _run_55_payload(**overrides) -> dict:
    """run 55 的 `request_payload` 里与归因有关的那几片（其余字段与本判据无关）。"""
    payload = {
        "mode": "weekly",
        "scope": "incremental",
        "delta_files": [
            {
                "repository_id": 3,
                "file_path": TABLE,
                "latest_commit_id": NEW_COMMIT,
                "diff_base_commit_id": BASE_COMMIT,
                "commit_count": 4,
            }
        ],
        "summary": {"batch_files": 1, "window_commits": 5},
        # 窗口账：按 `window_commit_ids` 的顺序（**不是**时间序 —— 那句查询没有 orderBy，
        # 而 `commit_time` 还可能被回填，本仓库的 fixture 里就有一条回填日期的提交）。
        # TABLE 在 NEW_COMMIT 与 OLD_COMMIT 里都出现 —— 这就是撞车的那一对。
        #
        # **本轮输入的那条排在更早的位置**，这是刻意的：真实 run 55 里恰巧排在最后，
        # 于是「按窗口顺序取最后一个」也能碰对；但那个顺序没有任何承诺，把判据建在它上面
        # 就是错的。这里把两种机制**分别**逼出来（见下面两条前提断言）。
        "window_commit_ids": [FIRST_COMMIT, NEW_COMMIT, BASE_COMMIT, PLAIN_COMMIT, OLD_COMMIT],
        "window_commit_files": {
            FIRST_COMMIT: {"paths": [SKILLS, TABLE, "src/battle_logic.py"], "repository_id": 3},
            BASE_COMMIT: {"paths": [TABLE, "src/battle_logic.py"], "repository_id": 3},
            PLAIN_COMMIT: {"paths": [SKILLS, "src/battle_logic.py"], "repository_id": 3},
            OLD_COMMIT: {"paths": [TABLE], "repository_id": 3},
            NEW_COMMIT: {"paths": [TABLE], "repository_id": 3},
        },
    }
    payload.update(overrides)
    return payload


def _scope(payload: dict | None = None) -> AnalysisScope:
    return from_weekly_payload(payload or _run_55_payload()).scope


# ==========================================================================
#  一、前提：旧代码正是在这个形状上取错的
# ==========================================================================


def test_the_earlier_commit_really_does_come_last():
    """**前提断言**：这个 fixture 把两种取错的机制都逼出来了。

    判据落在「取到的是写侧冻结的那条」上，但这个 fixture 必须**真的**能把两种旧写法
    都考倒，否则用例会在「换个顺序碰巧也对」的时候假绿：

    1. `commits` 里那条更早的提交排在**后面** —— 旧写法「遍历取最后一个匹配」会取到它
       （`_extra_commit_ids` 会把本轮输入的那条从 `commits` 里**去掉**，因为它已经是
       白名单键，于是它反而排到了最前）；
    2. 窗口账里那条更早的提交也排在**后面** —— 单靠「按窗口顺序取最后一个」也会取到它。

    两条缺一不可：只有 (1) 时，「白名单优先」那一层是死代码；只有 (2) 时，「不靠遍历
    顺序」那一层是死代码。判别不了死代码，就等于没验。
    """
    scope = _scope()
    payload = _run_55_payload()
    order = payload["window_commit_ids"]

    assert scope.commits.index(OLD_COMMIT) > scope.commits.index(NEW_COMMIT), "(1) 不成立"
    assert order.index(OLD_COMMIT) > order.index(NEW_COMMIT), "(2) 不成立"
    assert TABLE in scope.paths_by_commit[OLD_COMMIT]
    assert TABLE in scope.paths_by_commit[NEW_COMMIT]
    # 遍历取最后一个 —— 旧写法会得到 OLD_COMMIT，这就是那次错归因。
    assert [c for c in scope.commits if TABLE in scope.paths_by_commit[c]][-1] == OLD_COMMIT


# ==========================================================================
#  二、判据：取的是写侧为这次输入冻结的那条提交
# ==========================================================================


def test_the_delta_file_is_asked_for_at_this_round_commit():
    """**核心断言**：`commit_of_path` 给的是 delta 的那条，不是窗口里更早的那条。"""
    scope = _scope()

    assert scope.commit_of_path(TABLE) == NEW_COMMIT
    assert scope.commit_of_path("./" + TABLE) == NEW_COMMIT, "归一化后仍要判对"


def test_the_prefetch_asks_for_this_round_diff():
    """预取据此发出的请求，必须是本轮那条提交 —— 它是模型第 1 轮读到的「本次差异」。"""
    requests = diff_requests(_scope())

    by_path = {item.path: item.commit for item in requests}
    assert by_path[TABLE] == NEW_COMMIT, (
        "预取要了上一笔的差异 —— 它会被当成「本次输入的改动文件的差异」交给模型"
    )
    assert OLD_COMMIT not in by_path.values() or by_path[TABLE] != OLD_COMMIT


def test_a_window_path_still_gets_a_commit_that_touched_it():
    """窗口里**没有**装进本次输入的文件照旧有值（它们读到的是合并差异，出处会写明）。

    判据只钉「是改过它的某条提交、且稳定」，不钉「哪一条」：对这些文件而言落库那一份
    与提交号无关，而 `window_commit_ids` 本身没有顺序承诺，钉死它等于把一句不该有的
    承诺写进测试。
    """
    scope = _scope()
    for path in (SKILLS, "src/battle_logic.py"):
        commit = scope.commit_of_path(path)
        assert commit in scope.commits
        assert path in scope.paths_by_commit[commit]
        assert commit == scope.commit_of_path(path), "同一份 scope 两次要给出同一个答案"


def test_every_answer_is_a_pair_the_whitelist_allows():
    """**不变量**：`commit_of_path` 给出的 `(提交, 路径)` 必须在授权表里。

    这个值会被拿去发 `file_diff` 请求，而那个请求要过 `path_allowed` —— 派生出一条自己
    授权不了的配对，预取那一条就会被**静默丢掉**，看起来像「平台取数失败」。

    （第一版实现就踩了这个坑：映射是从 `window_commit_files` 拼的，而某个提交一旦是
    白名单键，它的 `paths_by_commit` 就**只收白名单那几条**，于是窗口账里它改过的别的
    路径会派出一条越权的配对。从授权表派生之后不可能再发生。）
    """
    scope = _scope()

    for path in scope.batch_paths():
        commit = scope.commit_of_path(path)
        assert commit is not None, f"{path} 拿不到提交"
        assert scope.path_allowed(commit, path), (
            f"({commit[:12]}, {path}) 不在授权表里 —— 照它发的请求会被丢掉"
        )


# ==========================================================================
#  三、防「改过头」：手工构造的 scope 语义不变
# ==========================================================================


def test_a_hand_built_scope_keeps_the_old_meaning():
    """没有冻结事实时（手工构造的 scope / 单提交模式）退回遍历，与从前逐字一致。

    这一条挡的是「顺手把遍历删掉」的改法：那会让所有手工构造 scope 的调用方与测试
    突然拿不到任何提交，而失败点会跑到很远的地方去。
    """
    scope = AnalysisScope(
        commits=("c1", "c2"),
        paths_by_commit={"c1": frozenset({"a.xlsx", "b.lua"}), "c2": frozenset({"a.xlsx"})},
    )

    assert scope.latest_commit_by_path == {}, "默认必须是空 —— 空 = 不知道 = 退回旧行为"
    assert scope.commit_of_path("a.xlsx") == "c2"
    assert scope.commit_of_path("b.lua") == "c1"
