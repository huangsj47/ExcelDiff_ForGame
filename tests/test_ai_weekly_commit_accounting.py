# -*- coding: utf-8 -*-
"""周版本清单里的**提交账**：三个数分开说，以及 `commit_detail` 的白名单（工作包 A）。

## 修的是报告里那条自相矛盾

2026-09-23 实测的报告里同时出现了两句话：

* 变更清单：「本次变更共 **3 个提交**」——它来自 `from_weekly_payload` 按每个文件的
  `latest_commit_id` 分组，数的是「有多少个不同的『文件最后一次改动所在的提交』」；
* `file_diff` 的出处：`（出处：平台已落库的**合并差异**，覆盖本批次的 **5/6** 条提交）`
  ——它说的是**这一个文件**的合并差异覆盖了几条提交。

两个数本来就可以不相等，而平台此前只给了第一个、把它叫成「提交数」，于是读报告的人
只能得出「平台前后矛盾」这个结论。这一组钉三件事：

1. 三个数**各自有名有姓**地出现在清单里（实际提交数 / 文件最新提交数 / 合并差异覆盖数）；
2. 那个「合并差异覆盖数」的**累加口径**必须写清楚 —— 它就是出处那一行「5/6」的来源，
   写成一个去重后的数会让两处**再次**对不上；
3. 窗口里那些「改的文件没被列进本次输入」的可达提交，**`commit_detail` 问得到**
   （白名单覆盖窗口真实可达的提交），而 `file_diff` 的路径白名单**一点没松**。
"""
from __future__ import annotations

from services.ai import window_commits
from services.ai.change_set import from_weekly_payload


def _payload(*, latest: list, counts: list, window_ids: list):
    """一份最小 payload：`delta_files` 是清单的来源，`window_commit_ids` 是写侧带下来的。"""
    return {
        "scope": "full",
        "summary": {"batch_files": len(latest)},
        "delta_files": [
            {"file_path": f"f{index}.lua", "latest_commit_id": commit,
             "commit_count": counts[index], "repository_id": 1}
            for index, commit in enumerate(latest)
        ],
        "window_commit_ids": window_ids,
    }


def test_the_three_numbers_are_stated_and_are_not_the_same_number():
    """**核心**：三个数都出现，且各自口径写清楚。

    这里刻意让三个数**互不相等**（3 / 2 / 9）—— 相等时这条断言就退化成「印了个数字」。
    """
    payload = _payload(
        latest=["a" * 40, "b" * 40, "a" * 40],          # 文件最新提交数 = 2
        counts=[6, 3, 0],                                # 合并差异覆盖数 = 6+3+0 = 9
        window_ids=["a" * 40, "b" * 40, "c" * 40],       # 实际提交数 = 3
    )

    summary = from_weekly_payload(payload).summary

    assert "本窗口实际提交数：3" in summary
    assert "文件最新提交数：2" in summary
    assert "合计 9" in summary and "最多 6" in summary
    # 口径必须逐字说明「按文件累加」——它就是出处那行「覆盖 5/6 条提交」的来源。
    assert "按文件累加" in summary
    assert "不要把它们当成互相矛盾" in summary

    # 「合并差异覆盖不止一条提交」的文件必须被点名 —— 它就是清单里那个
    # `## 提交 <id>` 小标题会误导读者的地方（那写的是「文件最后一次改动」，不是
    # 「这整段差异都出自这条提交」）。
    assert "## 提交 <id>" in summary
    assert "`f0.lua`（6 条）" in summary and "`f1.lua`（3 条）" in summary
    # 覆盖数 <= 1 的文件（f2.lua 的 commit_count 是 0）不进这一句：它们没有歧义。
    tail = summary.split("覆盖了不止一条提交", 1)[1].split("\n")[0]
    assert "f2.lua" not in tail

    # 与出处那一行的口径对齐：单文件最大值 = 出处里那个「6」。
    facts = window_commits.facts_from_payload(payload, window_commit_ids=payload["window_commit_ids"])
    assert facts.actual == 3 and facts.latest == 2
    assert facts.merged_total == 9 and facts.merged_max == 6


def test_an_unrecorded_window_commit_count_is_not_written_as_zero():
    """写侧没带窗口提交清单时，**如实说「本次没有记录」**，不许写成 0。

    `0` 与「没有记录」在报告里是完全不同的两句话：前者会被读成「这个窗口没有提交」，
    而后者只是「这次没算」。
    """
    payload = _payload(latest=["a" * 40], counts=[2], window_ids=[])

    summary = from_weekly_payload(payload).summary

    assert "本窗口实际提交数：**本次没有记录**" in summary
    assert "本窗口实际提交数：0" not in summary
    assert window_commits.facts_from_payload(payload).actual is None


def test_the_window_commits_extend_commit_detail_but_not_file_diff():
    """**核心（白名单）**：窗口可达提交进 `commit_detail` 白名单，**不进** `file_diff` 白名单。

    `test.lua` 的最新提交是 `a`，而窗口里还有一条 `c`（它改的文件没被列进本次输入）。
    期望：

    * `scope.resolve_commit("c"*40)` 成功 —— `commit_detail` 问得到；
    * `scope.path_allowed("c"*40, "test.lua")` 仍为 False —— 路径那一层白名单
      **一点没松**（给一条提交配上它当时的完整文件集需要再查一次库，而 `change_set`
      是纯函数层；用猜的路径去补，等于把白名单从「平台核对过」降成「平台猜的」）。
    """
    payload = _payload(
        latest=["a" * 40], counts=[2], window_ids=["a" * 40, "c" * 40],
    )

    scope = from_weekly_payload(payload).scope

    assert scope.resolve_commit("c" * 40) == "c" * 40
    assert scope.resolve_commit("a" * 40) == "a" * 40
    assert scope.path_allowed("a" * 40, "f0.lua") is True
    assert scope.path_allowed("c" * 40, "f0.lua") is False
    # 不在窗口清单里的提交照样进不来
    assert scope.resolve_commit("d" * 40) is None


def test_a_payload_without_the_window_list_keeps_the_old_scope():
    """老 payload（没有 `window_commit_ids`）行为**逐字不变**：白名单还是那些最新提交。"""
    payload = _payload(latest=["a" * 40], counts=[1], window_ids=[])

    scope = from_weekly_payload(payload).scope

    assert scope.resolve_commit("a" * 40) == "a" * 40
    assert scope.resolve_commit("c" * 40) is None
