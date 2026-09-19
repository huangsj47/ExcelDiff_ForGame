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
    _budget_line,
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


def test_the_first_round_asks_for_the_value_sanity_dimension():
    """数值改动要按「放在这个系统里合不合理」看（用户要求「高风险的值要报出来」）。

    这条与 `config_data` 那条是**两件事**，所以分开钉：上面那条管「这一行自不自洽」，
    这条管「这个数本身合不合理」（量级突变、经济闭环）。

    钉四件事：维度 id 出现、第一轮就要看、**索取比较基准**这一步写进第一轮，以及
    「疑似即可报、但缺基准要写明且置信度只能 high」—— 用户在这个维度上明确要的是
    召回率（宁可多报一条待确认，也不能让数量级写错的配置悄悄放过去），而放宽门槛的
    前提是**把「缺基准」这件事写在结论里**，否则它会退化成「这个值看起来很大」。
    """
    message = _message()
    assert "value_sanity" in message
    assert "几个数量级" in message
    assert "经济闭环" in message
    assert "整表统计" in message, "没告诉它比较基准从哪来"
    assert "file_content" in message
    assert "缺基准" in message
    assert "very_high" in message


def test_the_first_round_hint_steps_are_numbered_without_gaps():
    """步骤编号连续 —— 中间漏号会让人以为少了一步（真实缺陷形态：插了一步没顺延）。"""
    import re

    from services.ai.prompt import _FIRST_ROUND_HINT

    numbers = [int(n) for n in re.findall(r"^(\d+)\. \*\*", _FIRST_ROUND_HINT, flags=re.M)]
    assert numbers == list(range(1, len(numbers) + 1)), f"第一轮提示词的步骤编号不连续：{numbers}"


def test_the_first_round_does_not_render_context_items():
    message = _message(items=[ContextItem(kind="file_diff", label="l", text="不该出现")])
    assert "不该出现" not in message


def test_the_first_round_asks_for_the_config_data_dimension():
    """配表类改动要按「数据本身是否说得通」看（用户要求「重点分析配置数据是否有问题」）。

    这个维度**只在第一轮提示里点名是不够的**：`config_data` 的问题长得最像「只是改了句
    文案」，而第一轮的实际动作是挑 4~8 个文件去要 diff —— 不在分诊这一步说出来，模型
    就会按「改了哪个模块」挑文件，把只改了一列文案的那类改动整个放过去。所以这里钉的是
    三件事：维度 id 出现、被要求在这一轮看、以及那个最容易被放过的具体形态被写出来。
    """
    message = _message()
    assert "config_data" in message
    assert "只改了一列文案" in message
    assert "漏填必填列" in message
    assert "复制粘贴" in message


def test_later_rounds_render_the_fetched_context():
    message = _message(
        round_index=2,
        items=[ContextItem(kind="file_diff", label="file_diff abc config/x.xlsx", text="+ 一行改动")],
    )
    assert "+ 一行改动" in message
    assert "上一轮索要的上下文" in message


def test_the_round_counter_is_visible():
    assert "第 3/8 轮" in _message(round_index=3, max_rounds=8)


# --------------------------------------------------------------------------
# 历史结论基线（增量分析）
# --------------------------------------------------------------------------


def test_the_first_round_carries_the_baseline_in_full():
    """基线要整份给：模型得逐条对照才知道哪些是新的。"""
    digest = "# 这个版本截至上次分析已经报过的问题\n共 1 条：仍待处理 1。\n- [high] 【道具】ID 被删除 #12345678\n"

    message = _message(baseline_digest=digest)

    assert "【道具】ID 被删除" in message
    assert "#12345678" in message


def test_a_first_run_without_history_says_nothing_extra():
    """没有历史结论时不要硬塞一句空的「暂无」——第一轮的消息已经够长了。

    （第一次分析的那种说明由 `build_baseline_digest` 自己写在摘要里：它知道上下文，
    这里不知道。）
    """
    message = _message()
    assert "已经报过的问题" not in message


def test_later_rounds_remind_instead_of_resending_the_baseline():
    """**后续轮次只提醒一句，不重发整份。**

    重发一遍要花约 6,000 字符，而那是上下文条目的额度：多发一次基线，模型就少看一个
    文件的 diff。但完全不说也不行 —— 多轮之后注意力会从第一轮飘走，而「不要重复报」
    是输出层面的硬要求。所以两头都要断言：条目不在，要求还在。
    """
    digest = "# 这个版本截至上次分析已经报过的问题\n共 1 条：仍待处理 1。\n- [high] 【道具】ID 被删除 #12345678\n"

    message = _message(round_index=2, baseline_digest=digest)

    assert "#12345678" not in message, "整份基线被重发了，白花 6,000 字符"
    assert "【道具】ID 被删除" not in message
    assert "不要把它里面的问题当作" in message, "提醒也没了：模型会开始重复报"
    assert "已修复" in message and "已被推翻" in message, "没说要标注状态"


def test_later_rounds_stay_silent_about_the_baseline_when_there_is_none():
    message = _message(round_index=2, baseline_digest="")
    assert "不要把它里面的问题当作" not in message


def test_a_whitespace_only_baseline_counts_as_no_baseline():
    """上游渲染失败时可能给回一串空白，别把它当成一份基线插进提示词。"""
    message = _message(baseline_digest="   \n\n  ")
    assert "已经报过的问题" not in message


def test_budget_notes_are_passed_through_and_flagged_as_important():
    message = _message(budget_notes=["有 3 条 file_diff 上下文因条数上限未提供给你。"])
    assert "条数上限" in message
    assert "上下文完整性提示" in message


def test_empty_budget_notes_produce_no_section():
    assert "上下文完整性提示" not in _message(budget_notes=["", "   "])


def test_the_remaining_budget_is_stated():
    assert "12 次" in _message(requests_remaining=12)


class TestTheBudgetSentenceTellsTheTruthOnEveryRound:
    """`_budget_line` 的三句话。

    ## 线上真实发生过的那一次

    这里曾经只有一句「本次分析**总共**可索取 {remaining} 次上下文」，而 `remaining` 是
    剩余。第 2 轮起，一个用光额度的分片读到的是「本次分析总共可索取 **0 次**上下文」，
    它把这句原样抄进了报告的「信息缺口」（连「（跨轮次累计…）」那半句都抄了）。用户
    拿着这句话来问「平台这次为什么只给了 0 次额度」—— 查不到，因为额度本来是够的。

    **这段文字会被模型原样转述给用户**，所以它必须自己站得住：三个数（总额 / 已用 /
    还剩）缺一个就会长出一个查不出来的故障。
    """

    def test_it_never_reports_the_remaining_count_as_the_total(self):
        message = _message(requests_total=20, requests_remaining=0)

        assert "总共可索取 20 次" in message, "总额是 20，不许被写成 0"
        assert "已经用掉 20 次" in message
        assert "还能再索取 0 次" in message
        assert "总共可索取 0 次" not in message, "这就是线上那句话"

    def test_it_counts_what_was_used_from_the_two_numbers(self):
        message = _message(requests_total=8, requests_remaining=3)

        assert "总共可索取 8 次" in message
        assert "已经用掉 5 次" in message
        assert "还能再索取 3 次" in message

    def test_the_first_round_sentence_is_byte_identical(self):
        """用掉 0 次时**逐字保持原样**：子代理模式的共享消息必须与「同额度的单代理」
        逐字节相同，改一个字就让 prompt cache 全部失效（症状是悄悄贵好几倍）。"""
        assert _budget_line(requests_total=8, requests_remaining=8) == (
            "本次分析总共可索取 8 次上下文（跨轮次累计，"
            "重复索要同一个文件也计入）。额度用完就只能基于已有证据出报告，"
            "所以请优先要最关键的。"
        )

    def test_a_zero_budget_says_it_is_the_config_not_an_exhausted_budget(self):
        """上限配成 0 是**配置**，不是「用完了」—— 两件事对用户意味着不同的下一步。"""
        message = _message(requests_total=0, requests_remaining=0)

        assert "不允许索取上下文" in message
        assert "上限是 0 次" in message
        assert "用完" not in message, "「用完」会让人去查额度怎么被花掉的"

    def test_an_unknown_total_only_reports_what_is_left(self):
        """调用方不知道总额时（测试替身、探针）不许编一个总额出来。"""
        message = _message(requests_remaining=5)

        assert "还能再索取 5 次" in message
        assert "总共" not in message


def test_budget_exhaustion_forces_convergence():
    message = _message(budget_exhausted=True)
    assert "耗尽" in message
    assert "final" in message


def test_a_zero_budget_exhaustion_hint_says_there_was_never_a_budget():
    """收敛指令里那句「预算已耗尽」，在额度是 0 时也要换成「没有配置额度」。

    与上面那条同一个根因：模型会把这句话写进报告，说法错了用户就查错方向。
    """
    message = _message(requests_total=0, requests_remaining=0, budget_exhausted=True)

    assert "没有配置上下文索取额度" in message
    assert "预算已耗尽" not in message


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


def test_change_summary_states_the_true_total_when_the_list_is_truncated():
    """清单被截断时，首行必须写出**截断前**的真实文件数。

    线上那个周版本真实变更 767 个文件、清单只列了 200 个，而首行写的是「共 200 个文件」——
    模型于是写出「本版本共 67 个提交、200 个文件」，读者与它自己都以为这就是全量，
    后面「结论强度受限」的免责声明因此显得没来由（没人知道还有 567 个文件没进清单）。

    `summary.total_files` 一直躺在 payload 里，只是从没进过提示词。
    """
    commits = [
        _commit(files=(FileChange(path="a.py"), FileChange(path="b.py"))),
        _commit(commit="c" * 40, files=(FileChange(path="c.py"),)),
    ]
    text = render_change_summary(commits, total_files=767)

    assert "767" in text, "没有写出截断前的真实文件数"
    assert "3 个" in text, "没有写出清单里实际有多少个"
    assert "还有 764 个文件的名字没有列出来" in text, "没有说明模型看不到多少名字"
    # 「名字没列出来」不等于「读不到」：截断说明必须给出发现路径的办法，
    # 否则模型会把「看不到名字」当成「读不到内容」，信息缺口的免责声明就白写了。
    assert "commit_detail" in text, "没有告诉模型怎么查出没列出来的那些文件"
    assert "读不到" in text or "读到它们的 diff" in text, (
        "没有说明这些文件其实是可读的"
    )
    assert "本次只看到" in text, "没有给出「只看到 M/N」的写法引导"


def test_change_summary_keeps_the_plain_wording_when_nothing_is_omitted():
    """没截断时保持原来的句子 —— 不要凭空多出一段免责声明。"""
    text = render_change_summary(
        [_commit(files=(FileChange(path="a.py"), FileChange(path="b.py")))]
    )
    assert "共 1 个提交、2 个文件" in text
    assert "没有列出来" not in text


def test_change_summary_does_not_trust_a_total_smaller_than_the_list():
    """总数比清单还小时按清单算 —— 脏数据不能把首行写小。"""
    text = render_change_summary(
        [_commit(files=(FileChange(path="a.py"), FileChange(path="b.py")))],
        total_files=1,
    )
    assert "共 1 个提交、2 个文件" in text


def test_from_weekly_payload_passes_the_real_total_into_the_prompt():
    """接线守卫：真实文件数必须从 payload 一路传到渲染出来的提示词里。

    只测 `render_change_summary(..., total_files=767)` 只能证明那个参数管用，
    证明不了它被传了 —— 把 `change_set` 里那一行删掉，那些用例照样全绿。
    """
    from services.ai.change_set import from_weekly_payload

    payload = {
        "summary": {"total_files": 767, "delta_files": 767},
        "delta_files": [
            {"latest_commit_id": "a" * 40, "file_path": "config/奖励模式_CfgRewardMode.xlsx"},
            {"latest_commit_id": "b" * 40, "file_path": "code/qz_pub/cfg/SeasonRankCfgMod.lua"},
        ],
        "delta_truncated": True,
    }
    summary = from_weekly_payload(payload, readable_references=()).summary

    assert "767" in summary, "真实文件数没有进提示词"
    assert "2 个" in summary, "清单里实际有几个也没写"
    assert "没有列出来" in summary, "没有告诉模型它看不到大部分文件"


# ==========================================================================
# 字节稳定性（prompt cache 的前提）
# ==========================================================================


def test_the_same_input_always_produces_the_same_bytes():
    """**相同输入 → 逐字节相同的提示词。**

    上游的 prompt cache 按**前缀逐字节匹配**。提示词里只要有一处不确定的东西 ——
    一个时间戳、一个随机 id、一次按集合顺序遍历出来的输出 —— 跨运行命中就归零，
    而**归零是静默的**：分析照样跑完，只是每一次都按未命中价计费、首 token 也慢一截，
    没有任何报错会指向这里。所以这条用例守的不是「函数是纯的」这个事实，而是
    「将来别往里面加一个时间戳」这个纪律。
    """
    kwargs = {
        "project_knowledge": "本周只评审配表",
        "project_instructions": "本项目只看配表",
    }
    assert build_system_prompt(_loaded(), **kwargs) == build_system_prompt(_loaded(), **kwargs)

    items = (
        ContextItem(
            kind="file_diff",
            label="file_diff abc123 config/x.xlsx",
            text="+ 100012 攻击力 100",
            meta={"original_chars": 50_000, "truncated": True, "limit": 14_000},
        ),
    )
    baseline = "# 已报过的问题\n- [high] 旧问题\n"
    first = _message(items=items, baseline_digest=baseline)
    assert first == _message(items=items, baseline_digest=baseline)
    # 第一轮与后续轮次都要稳（后续轮次带的是上下文条目，渲染路径不同）。
    later = _message(round_index=2, items=items, budget_notes=["有 1 条被截断"])
    assert later == _message(round_index=2, items=items, budget_notes=["有 1 条被截断"])
    assert first.encode("utf-8") == _message(items=items, baseline_digest=baseline).encode("utf-8")


def test_a_dict_key_order_never_leaks_into_the_prompt():
    """**同一个 dict、不同的插入顺序 → 同样的提示词。**

    记账字段（`ContextItem.meta`）是从各处拼出来的字典，插入顺序取决于调用方怎么写的。
    一旦有人把某处改成 `json.dumps(meta)` 或者直接遍历 `meta` 输出，顺序就会漏进提示词：
    同一个文件、同一份内容，两次运行的提示词不同 —— 缓存命中率会莫名其妙地掉一半，
    而两边的提示词人眼看过去一模一样。
    """
    forward = {"original_chars": 50_000, "truncated": True, "limit": 14_000}
    backward = {"limit": 14_000, "truncated": True, "original_chars": 50_000}

    def _render(meta: dict) -> str:
        return render_context_items(
            [ContextItem(kind="file_diff", label="l", text="正文", meta=meta)]
        )

    assert _render(forward) == _render(backward)
    assert _render(forward) == _render(dict(reversed(list(forward.items()))))


# 提示词的输入必须是确定的：这几个模块里出现下面任何一种写法，跨运行的缓存命中就会归零。
# `engine.py` 用 `time.monotonic()` 记耗时（那不进提示词），所以只禁 `time.time()`。
_FORBIDDEN_IN_PROMPT_SOURCES = ("datetime.now", "datetime.utcnow", "time.time()", "random")
_PROMPT_SOURCE_MODULES = ("prompt.py", "budget.py", "engine.py", "skill_loader.py", "baseline.py")


def _code_without_comments_or_docstrings(source: str) -> str:
    """剥掉注释与文档字符串，只留会执行的代码。

    **必须先剥再断言**：本仓库的注释里会原样引用「要禁掉的写法」来讲解为什么禁它
    （上面那段就是），不剥的话禁令会打在注释上 —— 假失败一次，就会有人把断言删掉。
    文档字符串同理：它是给人看的文字，不是会执行的代码。

    做法是**把那些行/片段清空**，而不是把 token 重新拼起来 —— 重新拼会改变空白与换行
    （`time.time()` 会被拆到三行上），于是 `in` 永远匹配不到，守卫变成一条永远绿的假用例。
    """
    import ast
    import io
    import tokenize

    lines = source.splitlines()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        # `body` 不一定是个列表：`ast.Lambda` 与 `ast.IfExp` 也叫 body，但装的是表达式。
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                for index in range(first.value.lineno - 1, first.value.end_lineno):
                    lines[index] = ""

    blanked = "\n".join(lines)
    for token in tokenize.generate_tokens(io.StringIO(blanked).readline):
        if token.type == tokenize.COMMENT:
            row, column = token.start
            lines[row - 1] = lines[row - 1][:column]
    return "\n".join(lines)


def test_the_prompt_sources_stay_deterministic():
    """静态守卫：提示词组装链路上不许出现时间戳 / 随机数。

    这条防的是「将来有人往提示词里加一个时间戳」—— 它的后果是缓存命中率静默归零，
    而没有任何一处会报错（见上面那两条用例的说明）。
    """
    import services.ai.baseline as baseline_module
    import services.ai.budget as budget_module
    import services.ai.engine as engine_module
    import services.ai.prompt as prompt_module
    import services.ai.skill_loader as skill_loader_module

    modules = (
        prompt_module,
        budget_module,
        engine_module,
        skill_loader_module,
        baseline_module,
    )
    assert [Path(module.__file__).name for module in modules] == list(_PROMPT_SOURCE_MODULES)

    for module in modules:
        code = _code_without_comments_or_docstrings(
            Path(module.__file__).read_text(encoding="utf-8")
        )
        for forbidden in _FORBIDDEN_IN_PROMPT_SOURCES:
            assert forbidden not in code, (
                f"{Path(module.__file__).name} 里出现了 `{forbidden}`。"
                "提示词的输入必须是确定的：一次不确定的输入会让 prompt cache 的跨运行"
                "命中静默归零（分析照跑，只是每次都比上一次贵）。"
            )
