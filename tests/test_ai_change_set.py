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
    change = from_weekly_payload(_weekly_payload(delta_truncated=True))

    assert "不是全部改动" in change.summary


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
    一起看才看得见，所以要在清单里点明这两件事是一件事。"""
    change = from_weekly_payload(_weekly_payload())

    assert change.bundle_lines, "表与生成物没有被配成一组"
    assert "CfgItem" in change.bundle_lines[0]
    assert "这些改动是一件事" in change.summary
    assert "表与其生成物" in change.summary


def test_no_bundle_section_when_nothing_pairs():
    change = from_weekly_payload(_weekly_payload(files=[{"file_path": OTHER, "latest_commit_id": "c1"}]))

    assert change.bundle_lines == ()
    assert "这些改动是一件事" not in change.summary


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
