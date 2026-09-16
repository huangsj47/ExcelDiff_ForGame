"""提示词组装：顺序、优先级、以及「不预拉 diff」这件事要说清楚。

提示词不像代码那样有确定性行为，所以这里的用例只守**结构性质**，不守具体措辞：

* 内置协议必须真的在里面（否则「强制使用 skill」是句空话）；
* 三段内容的**顺序**与**优先级声明**必须一致（模型对靠后的内容更敏感，顺序写反等于
  把优先级写反）；
* 变更数据里**不能出现 diff**，而且必须明确告诉模型「diff 不在这里」——只给一份文件
  清单却不说明，模型会开始猜内容；
* 省略量、剩余额度这些记账必须传给模型。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.ai.budget import ContextItem
from services.ai.prompt import (
    PROMPT_SOURCE_FILES,
    CommitSummary,
    FileChange,
    build_system_prompt,
    build_user_message,
    prompt_version,
    render_change_summary,
    render_context_items,
)
from services.ai.skill_loader import LoadedSkills, SkillDocument

PLATFORM_BODY = "# 角色与方法\n\n你是配表评审专家。必须给出证据。\n"
PROJECT_BODY = "# G119 知识包\n\n事实以本知识包为准。\n"


def _doc(name: str, text: str, description: str = "说明") -> SkillDocument:
    return SkillDocument(
        name=name,
        description=description,
        path=Path("/tmp") / name,
        text=text,
        content_hash="hash-" + name,
    )


def _loaded(*, with_project: bool = True) -> LoadedSkills:
    return LoadedSkills(
        platform_skill=_doc("version-diff-review", PLATFORM_BODY, "版本评审协议"),
        platform_references=(_doc("incident-checklist.md", "事故清单", "看到什么改动要警惕什么"),),
        project_manifest=_doc("g119-knowledge", PROJECT_BODY, "G119 知识包") if with_project else None,
        # 注意括号的位置：`f(a=(x),)` 里的逗号在括号**外面**，是关键字参数分隔符，
        # 不是元组构造符；要写成 `f(a=(x,))` 或 `((x,) if c else ())` 才是元组。
        # 第一版就在这里踩了坑：project_skills 变成了一个裸的 SkillDocument。
        project_references=(
            (_doc("config-table-spec.md", "配表规范正文", "ID 段位与命名规范"),)
            if with_project
            else ()
        ),
        project_skills=(
            (_doc("shop-rules", "商城规则正文", "商城相关的项目专属检查"),) if with_project else ()
        ),
        readable={"incident-checklist.md": Path("/tmp/a.md")},
        project_slug="g119" if with_project else None,
        revision="rev-1",
    )


def _commit(**overrides) -> CommitSummary:
    base = {
        "commit": "a" * 40,
        "message": "修复道具属性",
        "author": "zhangsan",
        "commit_time": "2026-09-16T10:00:00",
        "files": (FileChange(path="config/30_goods/item.xlsx", operation="M"),),
    }
    base.update(overrides)
    return CommitSummary(**base)


# ==========================================================================
# 系统提示词
# ==========================================================================


def test_the_platform_skill_body_is_injected_verbatim():
    """「强制使用 skill」的落点就在这里：正文无条件进提示词，不是「请参考」。"""
    system = build_system_prompt(_loaded())
    assert PLATFORM_BODY.strip() in system


def test_the_primacy_notice_comes_first():
    """优先级声明必须在最前面，不能指望模型把长文读完才看到。"""
    system = build_system_prompt(_loaded())
    assert system.index("优先级高于你的通用习惯") < system.index(PLATFORM_BODY.strip())


def test_project_knowledge_comes_after_the_platform_protocol():
    """顺序即优先级：模型对靠后的内容更敏感，项目事实要排在强制协议之后。"""
    system = build_system_prompt(_loaded())
    assert system.index(PLATFORM_BODY.strip()) < system.index(PROJECT_BODY.strip())


def test_project_instructions_come_last_and_are_marked_lowest_priority():
    system = build_system_prompt(_loaded(), project_instructions="本项目只看配表")
    assert system.index(PROJECT_BODY.strip()) < system.index("本项目只看配表")
    assert "不得放宽内置协议" in system


def test_the_conflict_rule_is_stated_for_project_facts():
    """「事实以项目为准、门槛以协议为准」这条必须写出来。

    否则模型看到项目里写着「ID 六位制」而协议里没提，会不知道该信哪个。
    """
    system = build_system_prompt(_loaded())
    assert "以内置协议为准" in system


def test_project_documents_are_listed_as_an_index_not_inlined():
    """渐进式披露：项目文档给索引与用途，正文按需索取。全部内联会直接撑爆预算。"""
    system = build_system_prompt(_loaded())
    assert "config-table-spec.md" in system
    assert "ID 段位与命名规范" in system
    assert "配表规范正文" not in system, "项目文档正文不该被内联进提示词"


def test_project_sub_skills_are_indexed_by_name_and_description():
    system = build_system_prompt(_loaded())
    assert "shop-rules" in system
    assert "商城相关的项目专属检查" in system
    assert "商城规则正文" not in system


def test_a_project_without_a_pack_gets_an_explicit_notice():
    """没有项目知识包时要明说，并引导模型把缺的知识标成信息缺口。

    否则模型会拿通用常识补上 G119 的具体规范，而它并不真的知道。
    """
    system = build_system_prompt(_loaded(with_project=False))
    assert "没有配置专属知识包" in system
    assert "信息缺口" in system


def test_project_instructions_are_omitted_when_empty():
    """断言的是**章节标题**，不是「项目补充指令」这个词。

    那四个字在开头的优先级声明里本来就会出现（说明它优先级最低），拿词去断言会把
    一个正确的实现判红——第一版就是这么写的。
    """
    system = build_system_prompt(_loaded(), project_instructions="   ")
    assert "# 项目补充指令（优先级最低）" not in system


def test_extra_project_knowledge_is_appended_to_the_pack():
    system = build_system_prompt(_loaded(), project_knowledge="本周只评审配表")
    assert "本周只评审配表" in system


def test_prompt_version_is_derived_from_the_prompt_source():
    """与 rules_version 同理：写死的版本号会让「改了提示词但用例复用了旧结果」。"""
    import hashlib

    import services.ai.prompt as prompt_module

    base = Path(prompt_module.__file__).resolve().parent
    digest = hashlib.sha1()
    for name in PROMPT_SOURCE_FILES:
        digest.update(name.encode("utf-8"))
        digest.update((base / name).read_bytes())

    assert prompt_version() == f"prompt-{digest.hexdigest()[:12]}"


# ==========================================================================
# 变更数据
# ==========================================================================


def test_change_summary_lists_commits_and_files_with_operations():
    text = render_change_summary(
        [
            _commit(
                files=(
                    FileChange(path="config/30_goods/item.xlsx", operation="M"),
                    FileChange(path="config/40_monster/monster.xlsx", operation="D"),
                )
            )
        ]
    )
    assert "2 个文件" in text
    assert "[M] config/30_goods/item.xlsx" in text
    assert "[D] config/40_monster/monster.xlsx" in text


def test_change_summary_counts_commits_and_files_across_the_batch():
    """计数必须准确：模型用它们判断自己看到的是不是全部。"""
    text = render_change_summary(
        [
            _commit(commit="a" * 40),
            _commit(commit="b" * 40, files=(FileChange(path="x.py"), FileChange(path="y.py"))),
        ]
    )
    assert "共 2 个提交、3 个文件" in text


def test_change_summary_exposes_the_omission_note():
    text = render_change_summary([_commit()], omitted_files_note="本批次只包含前 50 个文件。")
    assert "只包含前 50 个文件" in text


def test_change_summary_survives_a_commit_with_no_message():
    text = render_change_summary([_commit(message="", author="", commit_time="")])
    assert "a" * 40 in text


# ==========================================================================
# 上下文渲染
# ==========================================================================


def test_context_items_are_rendered_with_their_accounting():
    """记账必须给模型看：不告诉它「这份内容被截断过」，它会把残缺当全部。"""
    text = render_context_items(
        [
            ContextItem(
                kind="file_diff",
                label="file_diff abc123 config/x.xlsx",
                text="+ 100012 攻击力 100",
                meta={"original_chars": 50_000, "truncated": True, "limit": 14_000},
            )
        ]
    )
    assert "file_diff abc123 config/x.xlsx" in text
    assert "原文 50000 字" in text
    assert "14000" in text
    assert "+ 100012 攻击力 100" in text


def test_a_failed_tool_result_is_labelled_as_failed_in_the_prompt():
    text = render_context_items(
        [ContextItem(kind="file_diff", label="l", text="取数失败", meta={"tool_failed": True})]
    )
    assert "取数失败" in text


def test_an_empty_context_list_says_so_explicitly():
    assert "没有附带任何上下文" in render_context_items([])


# ==========================================================================
# user 消息
# ==========================================================================


def _message(**overrides) -> str:
    base = {
        "change_summary": "本次变更共 1 个提交、1 个文件。\n",
        "round_index": 1,
        "max_rounds": 8,
        "requests_remaining": 12,
    }
    base.update(overrides)
    return build_user_message(**base)


def test_the_first_round_says_no_diff_is_included():
    """**这条是平台原来那份提示词的病根**：它写着「请基于以下变更 diff」，而 payload
    里根本没有 diff。现在除了不给，还要明确说「不在这里，需要就点名要」。"""
    message = _message()
    assert "没有任何 diff" in message
    assert "file_diff" in message
    assert "不要凭文件名猜测" in message


def test_the_first_round_does_not_render_context_items():
    message = _message(items=[ContextItem(kind="file_diff", label="l", text="不该出现")])
    assert "不该出现" not in message


def test_later_rounds_render_the_fetched_context():
    message = _message(
        round_index=2,
        items=[ContextItem(kind="file_diff", label="file_diff abc config/x.xlsx", text="+ 一行改动")],
    )
    assert "+ 一行改动" in message
    assert "上一轮索要的上下文" in message


def test_the_round_counter_is_visible():
    assert "第 3/8 轮" in _message(round_index=3, max_rounds=8)


def test_budget_notes_are_passed_through_and_flagged_as_important():
    message = _message(budget_notes=["有 3 条 file_diff 上下文因条数上限未提供给你。"])
    assert "条数上限" in message
    assert "上下文完整性提示" in message


def test_empty_budget_notes_produce_no_section():
    assert "上下文完整性提示" not in _message(budget_notes=["", "   "])


def test_the_remaining_budget_is_stated():
    assert "12 次" in _message(requests_remaining=12)


def test_budget_exhaustion_forces_convergence():
    message = _message(budget_exhausted=True)
    assert "耗尽" in message
    assert "final" in message


def test_a_correction_hint_is_appended_as_the_last_block():
    message = _message(correction_hint="status 不认识")
    assert "status 不认识" in message
    assert message.rstrip().endswith("status 不认识")


def test_no_correction_section_when_there_is_no_error():
    assert "上一轮的问题" not in _message()


def test_a_later_round_without_context_still_renders_the_empty_notice():
    """第二轮但一条上下文都没取到（工具全失败）时也要有输出，而不是留一段空白
    让模型自己猜发生了什么。"""
    message = _message(round_index=2)
    assert "没有附带任何上下文" in message


@pytest.mark.parametrize("round_index", [0, -1])
def test_a_degenerate_round_index_is_treated_as_the_first_round(round_index):
    """不能因为轮次下标异常就把上下文渲染漏掉或重复渲染。"""
    message = _message(round_index=round_index)
    assert "没有任何 diff" in message
