# -*- coding: utf-8 -*-
"""平台 payload → 引擎输入。

这一层最容易出错的地方不是渲染，而是**白名单**：变更清单给模型看的路径，必须与
`AnalysisScope` 允许它索取的路径是同一批。两者一旦不一致，模型会看到文件却要不来
（表现为「模型不听话，一直在要一个读不到的文件」），或者反过来能要来清单外的文件。
所以最后几条专门做端到端断言。
"""
from __future__ import annotations

from services.ai.change_set import (
    DEFAULT_BUNDLE_LIMIT,
    from_commit_payload,
    from_weekly_payload,
)
from services.ai.protocol import ContextRequest, sanitize_requests

TABLE = "config/[30]道具表_CfgItem.xlsx"
LUA = "build/lua/CfgItem.lua"
OTHER = "src/lua/absorber.lua"


def _commit_payload(**overrides) -> dict:
    commit = {
        "id": 7,
        "commit_id": "a" * 40,
        "path": TABLE,
        "operation": "M",
        "author": "zhangsan",
        "message": "道具表新增 3 个 ID",
        "commit_time": "2026-09-16T10:00:00",
    }
    commit.update(overrides.pop("commit", {}))
    payload = {"mode": "commit", "scope": "full", "commit": commit}
    payload.update(overrides)
    return payload


def _weekly_payload(files=None, **overrides) -> dict:
    payload = {
        "mode": "weekly",
        "scope": "incremental",
        "delta_files": files
        if files is not None
        else [
            {"file_path": TABLE, "latest_commit_id": "c1"},
            {"file_path": LUA, "latest_commit_id": "c1"},
            {"file_path": OTHER, "latest_commit_id": "c2"},
        ],
    }
    payload.update(overrides)
    return payload


# ==========================================================================
# 单提交模式
# ==========================================================================


def test_a_commit_payload_keeps_the_subject_line_and_author():
    change = from_commit_payload(_commit_payload())

    assert len(change.commits) == 1
    commit = change.commits[0]
    assert commit.commit == "a" * 40
    assert commit.message == "道具表新增 3 个 ID"
    assert commit.author == "zhangsan"
    assert [item.path for item in commit.files] == [TABLE]
    assert change.paths == (TABLE,)
    assert "道具表新增 3 个 ID" in change.summary


def test_a_commit_without_an_id_produces_no_commits():
    """没有 commit_id 就没法建白名单，也就没法安全地允许任何索取 —— 宁可空。"""
    change = from_commit_payload(_commit_payload(commit={"commit_id": ""}))

    assert change.commits == ()
    assert change.is_empty
    assert change.scope.commits == ()


def test_an_unknown_operation_is_treated_as_a_modification():
    """认不出来的操作类型按 M 处理，**不猜成 A/D**。

    猜成删除会让模型以为「这个文件被删了」并据此推理（比如报「生成物没跟着删」），
    而实际只是平台的取值与预期不同。
    """
    change = from_commit_payload(_commit_payload(commit={"operation": "??"}))

    assert change.commits[0].files[0].operation == "M"


# ==========================================================================
# 周版本模式
# ==========================================================================


def test_weekly_files_are_grouped_by_their_commit():
    """**同一个提交下的多个文件要归到一个提交里。**

    一个文件一个提交的话，变更清单里同一个提交号会重复出现十几次，模型会以为这是一堆
    不同的提交，而「共 N 个提交」这个计数也就没意义了。
    """
    change = from_weekly_payload(_weekly_payload())

    assert [commit.commit for commit in change.commits] == ["c1", "c2"]
    assert len(change.commits[0].files) == 2
    assert len(change.commits[1].files) == 1
    assert "共 2 个提交、3 个文件" in change.summary


def test_weekly_commits_have_no_message_and_that_is_not_faked():
    """周版本的缓存表只存 `latest_commit_id`，不存提交信息。

    这里**如实留空**，不去编一份「提交信息」出来。缺的信息由模型用 `commit_detail`
    回源索取 —— 这正是那项工具存在的理由；编出来的话，模型会以为它已经知道这次改动
    的意图。
    """
    change = from_weekly_payload(_weekly_payload())

    assert all(commit.message == "" for commit in change.commits)
    assert "提交信息" not in change.summary


def test_the_incremental_scope_is_stated_in_the_summary():
    """增量时要告诉模型它看到的是**一部分**。不说的话，它会按「这个版本只改了这些」
    来评估影响面与回归范围。"""
    change = from_weekly_payload(_weekly_payload())

    assert "增量" in change.summary


def test_a_truncated_file_list_is_disclosed():
    """清单被截断时必须说明，而且要**同时说清「没列出来 ≠ 读不到」**。

    平台现在用两份清单表达截断：`delta_files` 是全部改动（白名单），`list_files` 是
    列出来的那部分。老实说「还有 N 个没列出来」是不够的 —— 模型会把它当成「读不到」，
    于是白写一段信息缺口。所以要给出发现路径的办法（`commit_detail`）。
    """
    change = from_weekly_payload(
        _weekly_payload(
            files=[
                {"file_path": TABLE, "latest_commit_id": "c1"},
                {"file_path": LUA, "latest_commit_id": "c1"},
                {"file_path": OTHER, "latest_commit_id": "c2"},
            ],
            list_files=[{"file_path": TABLE, "latest_commit_id": "c1"}],
            delta_truncated=True,
        )
    )

    assert "还有 2 个文件的名字没有列出来" in change.summary
    assert "commit_detail" in change.summary, "没说清怎么找出没列出来的那些文件"


def test_a_full_file_list_carries_no_truncation_note():
    """全列时不许出现截断说明 —— 那会让模型以为有东西没看到。"""
    change = from_weekly_payload(_weekly_payload())

    assert "没有列出来" not in change.summary
    assert "本次变更共 2 个提交、3 个文件" in change.summary


def test_a_wrong_commit_pairing_says_which_commit_to_use():
    """**路径对、提交配错**时，拒绝的理由必须说出「该换哪条提交」。

    这是实测里最贵的一条：模型在 178 个提交的批次里反复把 (commit, path) 配错，
    平台把请求丢掉、只回一句「本轮没有附带任何上下文」（`prompt.render_context_items`
    在没有条目时的那句话），于是模型把「我配错了」写成「平台取数失败」，
    还郑重写进报告的**信息缺口** —— 读者会去找一个不存在的平台故障。那一轮 28 条被拒、
    占索取总数的 23%，报告里因此多了一条错误的信息缺口。

    平台本来就知道该用哪条提交（`commit_of_path`，`find_references` 用的也是它），
    没有理由不说。`detail` 里也要留下配错的那条 —— 否则事后无从判断是谁配错了。
    """
    change = from_weekly_payload(
        _weekly_payload(
            files=[
                {"file_path": TABLE, "latest_commit_id": "c1"},
                {"file_path": LUA, "latest_commit_id": "c2"},
            ],
            list_files=[],
            delta_truncated=True,
        )
    )
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="file_diff", commit="c1", path=LUA)], change.scope
    )

    assert allowed == (), "配错的 (commit, path) 不该放行"
    assert len(dropped) == 1
    reason, detail = dropped[0].reason, dropped[0].detail
    assert "c2" in reason, f"没告诉模型该换哪条提交：{reason}"
    assert "c1" in reason, f"没说清它配的是哪条：{reason}"
    assert LUA in detail and "c1" in detail, f"detail 里没有可追溯的配对：{detail}"


def test_the_whitelist_covers_files_that_are_not_listed():
    """**核心不变量**：名字没列出来，不等于读不到。

    这是「清单」与「白名单」拆开的意义所在。以前两者是同一份（取样 200 个），没列出来的
    文件连 `file_diff` 都会被 `sanitize_requests` 丢掉；现在白名单给全部改动文件，
    清单只是名字没列全。
    """
    change = from_weekly_payload(
        _weekly_payload(
            files=[
                {"file_path": TABLE, "latest_commit_id": "c1"},
                {"file_path": LUA, "latest_commit_id": "c1"},
                {"file_path": OTHER, "latest_commit_id": "c2"},
            ],
            list_files=[{"file_path": TABLE, "latest_commit_id": "c1"}],
            delta_truncated=True,
        )
    )

    # 清单里只列了 TABLE，但白名单必须认 LUA 与 OTHER
    allowed, dropped = sanitize_requests(
        [
            ContextRequest(type="file_diff", commit="c1", path=LUA),
            ContextRequest(type="file_diff", commit="c2", path=OTHER),
        ],
        change.scope,
    )
    assert [request.path for request in allowed] == [LUA, OTHER], (
        f"没列出来的文件读不到（被丢掉的原因：{[(item.reason, item.detail) for item in dropped]}）"
    )
    # 越权仍然要被拒：这个提交根本没碰过的文件
    refused, _ = sanitize_requests(
        [ContextRequest(type="file_diff", commit="c1", path="src/从未改动.lua")],
        change.scope,
    )
    assert refused == (), "白名单放宽到了「没改动过的文件」，这是越权"


def test_an_unrecognised_scope_adds_no_note():
    change = from_weekly_payload(_weekly_payload(scope="whatever"))

    assert "本次分析范围" not in change.summary


def test_a_weekly_entry_missing_its_commit_is_skipped():
    change = from_weekly_payload(
        _weekly_payload(files=[{"file_path": TABLE, "latest_commit_id": ""}, {"file_path": LUA, "latest_commit_id": "c9"}])
    )

    assert [commit.commit for commit in change.commits] == ["c9"]
    assert change.paths == (LUA,)


# ==========================================================================
# 表与生成物
# ==========================================================================


def test_a_table_and_its_generated_file_are_called_out():
    """**这是 bundling 的接线处。** 分开看每一侧都正常，「表改了、产物没跟上」只有
    一起看才看得见，所以要在清单里点出「这几个文件名有关联」并标成**待确认**。"""
    change = from_weekly_payload(_weekly_payload())

    assert change.bundle_lines, "表与生成物没有被配成一组"
    assert "CfgItem" in change.bundle_lines[0]
    assert "疑似同一次改动，待确认" in change.summary
    # 平台**没有**核实过它们之间的关系，所以不能把「表与其生成物」当成事实写进提示词。
    assert "表与其生成物" not in change.summary
    assert "这些改动是一件事" not in change.summary


def test_no_bundle_section_when_nothing_pairs():
    change = from_weekly_payload(_weekly_payload(files=[{"file_path": OTHER, "latest_commit_id": "c1"}]))

    assert change.bundle_lines == ()
    assert "疑似同一次改动" not in change.summary
    # 「0 组」必须留下记录，且要说清是**谁**的前缀：`bundle_note` 就是那一行。
    assert "0 组" in change.bundle_note
    assert "Cfg" in change.bundle_note


def test_similarly_named_modules_stay_separate():
    change = from_weekly_payload(
        _weekly_payload(
            files=[
                {"file_path": "config/t_CfgItem.xlsx", "latest_commit_id": "c1"},
                {"file_path": "build/CfgItem.lua", "latest_commit_id": "c1"},
                {"file_path": "config/t_CfgItemSub.xlsx", "latest_commit_id": "c1"},
                {"file_path": "build/CfgItemSub.lua", "latest_commit_id": "c1"},
            ]
        )
    )

    assert len(change.bundle_lines) == 2


def test_the_bundle_section_is_capped_and_says_so():
    files = []
    for index in range(DEFAULT_BUNDLE_LIMIT + 3):
        files.append({"file_path": f"config/t_CfgM{index}.xlsx", "latest_commit_id": "c1"})
        files.append({"file_path": f"build/CfgM{index}.lua", "latest_commit_id": "c1"})

    change = from_weekly_payload(_weekly_payload(files=files))

    assert len(change.bundle_lines) == DEFAULT_BUNDLE_LIMIT + 1
    assert "另有 3 组" in change.bundle_lines[-1]


# ==========================================================================
# 路径
# ==========================================================================


def test_paths_are_deduplicated_and_keep_their_first_appearance_order():
    """顺序稳定是有意的：它决定 bundle 的排列，而这份清单每轮都进提示词。"""
    change = from_weekly_payload(
        _weekly_payload(
            files=[
                {"file_path": OTHER, "latest_commit_id": "c1"},
                {"file_path": "./" + TABLE, "latest_commit_id": "c1"},
                {"file_path": TABLE, "latest_commit_id": "c2"},
            ]
        )
    )

    assert change.paths == (OTHER, TABLE), "归一化后应当去重，且保持首次出现的位置"


# ==========================================================================
# 端到端：清单与白名单必须是同一批路径
# ==========================================================================


def test_the_model_can_request_exactly_what_the_summary_shows():
    """**清单里出现的文件必须都能要来。**

    两边不一致时，模型会一直索取一个读不到的文件、一直被拒，看起来像「模型不听话」，
    实际是我们自己没对齐 —— 而 `REQUEST_TYPES` 与 SKILL.md 的清单也是同一个教训。
    """
    change = from_weekly_payload(_weekly_payload())
    requests = tuple(
        ContextRequest(type="file_diff", commit=commit, path=path)
        for commit, paths in change.scope.paths_by_commit.items()
        for path in sorted(paths)
    )

    allowed, dropped = sanitize_requests(requests, change.scope)

    assert dropped == (), f"清单里的文件被白名单拒了：{[item.detail for item in dropped]}"
    assert len(allowed) == len(requests) == 3


def test_a_file_outside_the_summary_cannot_be_requested():
    """反方向：不在清单里的文件要不到。**这是「模型不能读任意文件」的落点。**"""
    change = from_weekly_payload(_weekly_payload())

    allowed, dropped = sanitize_requests(
        (ContextRequest(type="file_diff", commit="c1", path="src/secret/keys.lua"),),
        change.scope,
    )

    assert allowed == ()
    assert len(dropped) == 1


def test_a_commit_outside_the_summary_cannot_be_requested():
    change = from_weekly_payload(_weekly_payload())

    allowed, dropped = sanitize_requests(
        (ContextRequest(type="commit_detail", commit="d" * 40),), change.scope
    )

    assert allowed == ()
    assert len(dropped) == 1


def test_readable_references_are_carried_into_the_scope():
    change = from_weekly_payload(
        _weekly_payload(), readable_references=["incident-checklist.md", "config-table-spec.md"]
    )

    allowed, dropped = sanitize_requests(
        (ContextRequest(type="read_reference", name="config-table-spec.md"),), change.scope
    )

    assert dropped == ()
    assert len(allowed) == 1


def test_a_reference_that_was_not_offered_cannot_be_requested():
    change = from_weekly_payload(_weekly_payload(), readable_references=["incident-checklist.md"])

    allowed, _dropped = sanitize_requests(
        (ContextRequest(type="read_reference", name="project-secrets.md"),), change.scope
    )

    assert allowed == ()


# ==========================================================================
# 给模型的那份总数必须是「这一批」，不是「这个版本」
#
# 2026-09 复核出来的：`summary.total_files` 是**窗口总数**（这个周版本一共有过多少
# 改动文件），而 `scope=incremental` 时输入里只有水位线之后变化的那一部分 —— 实测
# 847 vs 19。原先这里直接拿窗口总数当「本次变更的文件共 N 个」，于是那段「还有 M 个
# 的名字没列出来，**而且你可以读到它们的 diff**」宣称了 828 个白名单里根本没有的文件
# 读得到。模型照着自己去点名索取，请求被按白名单静默丢掉、不给任何回执，于是它把
# 一个**不存在**的取数缺口写进报告。
# ==========================================================================


def _sized_weekly_payload(window: int, batch: int, listed: int, **overrides) -> dict:
    """窗口里 `window` 个文件，这次装进输入 `batch` 个，清单里列出 `listed` 个。"""
    def files(count: int) -> list:
        return [{"file_path": f"src/f{index}.lua", "latest_commit_id": "c1"} for index in range(count)]

    payload = _weekly_payload(files=files(batch), list_files=files(listed))
    payload["summary"] = {"total_files": window, "batch_files": batch, "window_files": window}
    payload.update(overrides)
    return payload


def test_the_listed_total_is_the_batch_not_the_whole_version_window():
    """19 个装进输入的一批，不许被写成「本次变更的文件共 847 个」。"""
    change = from_weekly_payload(_sized_weekly_payload(window=847, batch=19, listed=19))

    assert "本次变更共 1 个提交、19 个文件" in change.summary
    assert "847 个文件" in change.summary, "窗口总数要如实出现在「覆盖了多少」那句里"
    assert "你可以读到它们的 diff" not in change.summary, (
        "19 个就是全部白名单，没有任何「名字没列出来但读得到」的文件"
    )


def test_the_readability_claim_only_appears_when_the_whitelist_really_has_them():
    """反向自检：**取样**没列出来的那些确实在白名单里，那句「你可以读到」要照写。"""
    change = from_weekly_payload(_sized_weekly_payload(window=847, batch=847, listed=200))

    assert "本次变更的文件共 847 个，下面列出其中的 200 个" in change.summary
    assert "还有 647 个文件的名字没有列出来" in change.summary
    assert "你可以读到它们的 diff" in change.summary


def test_a_partial_batch_says_how_much_of_the_version_it_covers():
    """范围判定回 `full` ≠ 输入装了整个版本 —— 判定只改标签，不会再查一次全量。

    不说清这一点，模型读到「本次分析范围：全量」就会对「本版本没问题」下结论，
    而窗口里另外 347 个文件它一个都没看过。
    """
    change = from_weekly_payload(
        _sized_weekly_payload(window=847, batch=500, listed=500, scope="full")
    )

    assert "本次分析范围：全量" in change.summary
    assert "不等于「输入装了整个版本」" in change.summary
    assert "本版本改动过的 847 个文件里的 500 个" in change.summary


def test_a_first_run_does_not_get_the_coverage_sentence():
    """反向自检：首跑时窗口总数 == 本批，别凭空多出一句「只覆盖了一部分」。"""
    change = from_weekly_payload(
        _sized_weekly_payload(window=847, batch=847, listed=847, scope="full")
    )

    assert "本次分析范围：全量。" in change.summary
    assert "这次输入覆盖的是" not in change.summary


# ==========================================================================
#  三层材料：只有「本轮输入」能支撑「本次改了什么」
# ==========================================================================


class TestTheThreeKindsOfMaterialAreNamed:
    """`change_set` 渲染的清单要把三类材料**点名分开**。

    run 55/57 的病：报告把上一笔提交、以及窗口里更早的改动都写成「本次差异」，还据此
    编出「提交信息与差异不一致」的风险。提示词原先只说「共 N 个提交、M 个文件」，
    没有一处说清这些材料里哪一类才是「本次」。所以这三条要在**真入口渲染出来的文本**里，
    并且**第一类要写明它才是唯一能支撑「本次改了什么」的那一类**。
    """

    def _summary(self):
        from services.ai.change_set import from_weekly_payload

        payload = {
            "scope": "incremental",
            "delta_files": [
                {
                    "file_path": "config/[30]道具表_CfgItem.xlsx",
                    "operation": "M",
                    "latest_commit_id": "a" * 40,
                    "repository_id": 1,
                },
            ],
            "commits": [],
            "summary": {"window_files": 3, "batch_files": 1},
            "window_commit_ids": ["a" * 40, "b" * 40],
        }
        return from_weekly_payload(payload).summary

    def test_the_three_kinds_are_named_in_the_rendered_summary(self):
        text = self._summary()

        assert "本轮输入（本次改动）" in text, text
        assert "窗口内更早的提交" in text, text
        assert "项目背景" in text, "没有点名第三类（当前冻结版本）"

    def test_it_says_only_the_first_supports_this_round(self):
        text = self._summary()

        assert "只有第一类能支撑「本次改动了什么」" in text, (
            "没有明说哪一类才算「本次」 —— 这正是 run 55 把上一笔提交写成「本次差异」的原因"
        )
        assert "不是本次的改动" in text, "第二类没有写明「不是本次」"

    def test_the_background_kind_is_not_mistaken_for_this_round(self):
        """第三类最容易与被混淆的第二类混起来：当前 tip 不是「本次改了它」的证据。"""
        text = self._summary()

        assert "当前冻结版本" in text
        assert "不能**拿它当「本次改了它」的证据" in text or "不能" in text, text

    def test_the_legend_reaches_the_prompt_not_just_the_change_set(self):
        """接线：`summary` 之外还要看它有没有进第一轮那条消息（`change_block`）。"""
        from services.ai.change_set import from_weekly_payload
        from services.ai.prompt import change_block

        payload = {
            "scope": "incremental",
            "delta_files": [
                {
                    "file_path": "config/[30]道具表_CfgItem.xlsx",
                    "operation": "M",
                    "latest_commit_id": "a" * 40,
                    "repository_id": 1,
                },
            ],
            "commits": [],
            "window_commit_ids": ["a" * 40],
        }
        summary = from_weekly_payload(payload).summary

        block = change_block(summary, round_index=1)

        assert "本轮输入（本次改动）" in block, (
            "标注没进第一轮消息 —— 那它就只是躺在 ChangeSet 里，模型看不到"
        )
