# -*- coding: utf-8 -*-
"""必读清单的进度（P1b）：**这一片分到的文件里还有几条没取到证据**。

两个问题各测一层：

1. **判据**（纯函数）：什么算「取到证据」—— 失败说明不算、本地缓存的那句指针不算、
   仓库对不上不算。这些都在 `MandatoryProgress.observe` 里，逐个形态钉住；
2. **接线**：这个数**真的进了每轮发给模型的消息**（判据对不对没有意义，如果它没被送出去）。
   这一条走**生产入口** `run_analysis`，假模型 + 假取数口。

「没清单时一个字都不加」也有一条：单代理、汇总、对账轮走的都是那条路，它们的提示词必须
与从前逐字节相同。
"""
from __future__ import annotations

from services.ai.budget import ContextItem
from services.ai.family_ledger import AssignedFile
from services.ai.mandatory_progress import MandatoryProgress, progress_of
from services.ai.protocol import ContextRequest
from tests.test_ai_engine import (
    COMMIT,
    LUA,
    TABLE,
    FakeProvider,
    ScriptedClient,
    _anomaly,
    _final,
    _run,
)

REPO = 7


def _file(repository_id: int, commit: str, path: str, source: str = "delta") -> AssignedFile:
    return AssignedFile(repository_id=repository_id, commit=commit, path=path, source=source)


def _request(kind: str, commit: str, path: str, repository_id="") -> ContextRequest:
    return ContextRequest(
        type=kind, commit=commit, path=path, repository_id=repository_id
    )


def _item(text: str = "diff 正文", **meta) -> ContextItem:
    return ContextItem(kind="file_diff", label="file_diff " + COMMIT[:12], text=text, meta=meta)


# ==========================================================================
# 判据
# ==========================================================================


def test_a_fetched_file_is_ticked_off():
    progress = progress_of([_file(REPO, COMMIT, TABLE)])
    progress.observe(_request("file_diff", COMMIT, TABLE, REPO), _item())
    assert progress.remaining == 0
    assert "已全部取到证据" in progress.note()


def test_the_commit_may_be_a_short_prefix():
    """模型可以只写短号（仓库里通常 7~12 位），平台存的是全号 —— 两边按前缀认。"""
    entry = _file(REPO, "a" * 40, TABLE)
    progress = progress_of([entry])
    progress.observe(_request("file_diff", "a" * 12, TABLE, REPO), _item())
    assert progress.remaining == 0


def test_a_short_prefix_below_the_floor_does_not_match():
    """4 个字符的号不当成匹配：它在两个仓库里撞车的概率不低，而错记比漏记贵得多。"""
    progress = progress_of([_file(REPO, "abcd" + "0" * 36, TABLE)])
    progress.observe(_request("file_diff", "abcd", TABLE, REPO), _item())
    assert progress.remaining == 1


def test_a_failure_notice_is_not_evidence():
    """`[取数失败] …` 长得像内容 —— 按它记账会让进度行显示「已经读完了」。"""
    progress = progress_of([_file(REPO, COMMIT, TABLE)])
    progress.observe(
        _request("file_diff", COMMIT, TABLE, REPO),
        _item("[取数失败] file_diff：平台读不到这一份。**这不等于「没有改动」**"),
    )
    assert progress.remaining == 1


def test_a_tool_failure_is_not_evidence():
    progress = progress_of([_file(REPO, COMMIT, TABLE)])
    progress.observe(
        _request("file_diff", COMMIT, TABLE, REPO),
        _item("内容不可用", tool_failed=True),
    )
    assert progress.remaining == 1


def test_the_repeat_pointer_is_not_evidence():
    """本地缓存命中给的是「见上文那一节」的指针，正文不在这一条里（`context_tools` 第 4 条）。"""
    progress = progress_of([_file(REPO, COMMIT, TABLE)])
    progress.observe(
        _request("file_diff", COMMIT, TABLE, REPO),
        _item("见上文那一节", repeat_pointer=True),
    )
    assert progress.remaining == 1


def test_a_named_repository_must_match():
    """同一条 `(提交, 路径)` 在两个仓库里是两份不同的内容（P1a）：认错等于提前归零。"""
    progress = progress_of([_file(REPO, COMMIT, TABLE)])
    progress.observe(_request("file_diff", COMMIT, TABLE, 9), _item())
    assert progress.remaining == 1
    # 反之，模型**没点名**仓库时不因此判不匹配 —— 那是「它没写」，不是「它写的是另一个」。
    progress.observe(_request("file_diff", COMMIT, TABLE), _item())
    assert progress.remaining == 0


def test_a_non_file_tool_does_not_tick_anything():
    """`commit_detail` 只给名单、`find_references` 只给位置 —— 都不是「看过这个文件」。"""
    progress = progress_of([_file(REPO, COMMIT, TABLE)])
    progress.observe(_request("commit_detail", COMMIT, TABLE, REPO), _item())
    progress.observe(_request("find_references", COMMIT, TABLE, REPO), _item())
    assert progress.remaining == 1


def test_the_note_counts_and_lists_the_missing_ones():
    entries = [
        _file(REPO, COMMIT, TABLE, source="compensation"),
        _file(REPO, COMMIT, LUA),
    ]
    progress = progress_of(entries)
    note = progress.note()
    assert "2 个文件里" in note and "还有 2 个没取到证据" in note
    assert TABLE in note and LUA in note
    assert "信息缺口：该文件未取到证据" in note, "没读到时的去处必须写着（否则会被写成「没有风险」）"
    # 补偿项照旧由 `AssignedFile.describe()` 那边标，这里只保证地址三样都写全。
    assert "仓库 7" in note and COMMIT[:12] in note


def test_the_note_does_not_print_the_whole_list():
    entries = [_file(REPO, COMMIT, f"code/m{index}.lua") for index in range(20)]
    note = progress_of(entries).note(limit=3)
    assert "还有 20 个没取到证据" in note
    assert "另有 17 个" in note, "截断了却不说，读的人会以为清单就只有这 3 条"
    assert note.count("\n- ") == 4, "3 条 + 「另有」那一行"


def test_no_list_means_no_line():
    assert progress_of(()).note() == ""
    assert MandatoryProgress().note() == ""


# ==========================================================================
# 接线：它真的进了每轮消息
# ==========================================================================


def test_the_round_message_carries_the_progress():
    """第 2 轮那条进度行必须写着「还剩几条」，并把剩下的**列出来**。

    单代理那条路上跑着**平台预取**（`evidence_prefetch`），它会把本批次里最该先看的
    几个文件取回来 —— 那些当然算这条文件「取到证据」（内容真的交到模型手上了）。
    所以这里用一条**这一轮取不到的**条目代表「还剩着的那些」，钉的是另一半：没取到的
    那几条留在清单上、被列出来、数得对。
    """
    client = ScriptedClient(_requests_one(TABLE), _final(_anomaly()))
    missing = _file(REPO, COMMIT, "config/[31]本次没取到的表.xlsx")
    mandatory = (_file(REPO, COMMIT, TABLE), missing)
    _run(client, mandatory_files=mandatory)

    second = client.calls[1][-1]["content"]
    assert "必读清单进度" in second, second[-400:]
    assert "还有 1 个没取到证据" in second
    assert "本次没取到的表" in second, "剩的那一条要能被照着索取"
    assert f"仓库 {REPO}" in second, "地址三样都要写全（少一样模型就得自己猜）"


def test_a_fully_covered_list_says_so():
    """都取到了就说都取到了（预取回来的也算）—— 否则模型会把额度再花一遍。"""
    client = ScriptedClient(_requests_one(TABLE), _final(_anomaly()))
    mandatory = (_file(REPO, COMMIT, TABLE), _file(REPO, COMMIT, LUA))
    _run(client, mandatory_files=mandatory)
    assert "已全部取到证据" in client.calls[1][-1]["content"]


def test_a_failed_fetch_stays_on_the_list():
    """取不到 ⇒ 它仍在清单上（而且这一点不许被写成「没问题」）。"""
    client = ScriptedClient(_requests_one(TABLE), _final(_anomaly()))
    mandatory = (_file(REPO, COMMIT, TABLE),)
    _run(client, provider=FakeProvider(failing=True), mandatory_files=mandatory)
    second = client.calls[1][-1]["content"]
    assert "还有 1 个没取到证据" in second


def test_a_run_without_a_mandatory_list_says_nothing_about_one():
    """单代理 / 汇总 / 对账轮（没有清单）的提示词必须与从前逐字节相同。"""
    client = ScriptedClient(_requests_one(TABLE), _final(_anomaly()))
    _run(client)
    assert "必读清单" not in client.all_user_text()


def test_an_empty_list_is_the_same_as_no_list():
    with_param = ScriptedClient(_requests_one(TABLE), _final(_anomaly()))
    without_param = ScriptedClient(_requests_one(TABLE), _final(_anomaly()))
    _run(with_param, mandatory_files=())
    _run(without_param)
    assert with_param.calls == without_param.calls, (
        "空清单与不传参数必须给出同一条消息 —— 否则「不传时逐字节相同」这句承诺已经不成立"
    )


def _requests_one(path: str) -> str:
    """一轮「我要这个文件的 diff」的回答。"""
    import json

    return json.dumps(
        {
            "status": "need_more_context",
            "requests": [
                {
                    "type": "file_diff",
                    "commit": COMMIT,
                    "path": path,
                    "repository_id": str(REPO),
                }
            ],
        },
        ensure_ascii=False,
    )
