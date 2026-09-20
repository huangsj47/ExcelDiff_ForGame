"""字符预算与规则层 —— 一个负责「别把上下文撑爆」，一个负责「别把清单撑爆」。

两者都是纯函数，所以这里能对边界做穷尽式的断言。有两类性质是本文件重点守的：

1. **绝不静默裁剪**。压缩与丢弃都必须留下痕迹：文本里有标记、`meta` 里有原始长度、
   `notes` / `dropped` 里有可读的原因。反过来说，**没有丢东西时不能凭空产出一句
   「有内容被省略了」**——那会让模型去重新索取它本来就有的东西。两个方向都有用例。
2. **阈值决定条数，且单调**。`min_severity` 从 `high` 提到 `critical` 之后，报出来的
   条数只能变少不能变多。这条性质把「报得太多」变成一个可解释、可回归的配置问题，
   而不是反复改提示词去试探模型。

下面每条守卫都配了反向用例。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from services.ai.budget import (
    MAX_SHRINK_LEVEL,
    SHRINK_LEVEL_LIMITS,
    TRUNCATION_SUFFIX,
    ContextItem,
    build_continuation_summary,
    enforce_budget,
    estimate_chars,
    limit_item_count,
    shrink_item,
    trim_total_chars,
    truncate_text,
)
from services.ai.protocol import Anomaly
from services.ai.windowed_view import render_window
from services.ai.rules import (
    DEFAULT_MAX_ANOMALIES,
    KIND_ANOMALY_CAP,
    MIN_SIMILARITY_TOKENS,
    RULE_SOURCE_FILES,
    RulesConfigError,
    RuleThresholds,
    anomaly_fingerprint,
    cap_anomalies,
    containment,
    is_probable_duplicate,
    normalize_anomalies,
    rules_version,
    tokenize,
)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def _item(kind: str = "file_diff", label: str = "dummy", size: int = 100) -> ContextItem:
    return ContextItem(kind=kind, label=label, text="x" * size)


def _anomaly(**overrides) -> Anomaly:
    base = {
        "title": "【配表】道具 ID 被删除",
        "category": "config_id",
        "severity": "critical",
        "confidence": "high",
        "evidence": ("30_goods/item.xlsx 第 12 行 100012 被删除",),
        "commit": COMMIT_A,
        "file_path": "config/30_goods/item.xlsx",
    }
    base.update(overrides)
    return Anomaly(**base)


# ==========================================================================
# budget：截断
# ==========================================================================


def test_short_text_is_returned_untouched():
    text, truncated = truncate_text("abc", 100)
    assert text == "abc"
    assert not truncated


def test_text_at_exactly_the_limit_is_not_truncated():
    """边界：等于上限不算超。差一位就差一条无用的截断标记。"""
    text, truncated = truncate_text("x" * 50, 50)
    assert text == "x" * 50
    assert not truncated


def test_truncated_result_fits_inside_the_limit():
    """**截断标记本身也算在预算内**。

    很容易写成 `content[:limit] + SUFFIX`，那样结果比调用方给的上限还长 —— 于是
    「按预算裁剪」的每一步都悄悄超预算，最后总量对不上。这里直接断言长度。
    """
    text, truncated = truncate_text("x" * 10_000, 100)
    assert truncated
    assert len(text) <= 100
    assert text.endswith(TRUNCATION_SUFFIX)


def test_truncation_leaves_a_visible_mark():
    text, _ = truncate_text("汉字" * 1000, 50)
    assert TRUNCATION_SUFFIX.strip() in text


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_is_rejected(limit):
    """上限为 0 会产出「只剩一个截断标记」的怪文本，属于调用方的错，明确拒绝。"""
    with pytest.raises(ValueError):
        truncate_text("abc", limit)


# ==========================================================================
# budget：分级压缩
# ==========================================================================


def test_level_zero_is_a_no_op():
    original = _item(size=10_000)
    assert shrink_item(original, 0) == original


def test_level_one_truncates_to_the_first_tier():
    shrunk = shrink_item(_item(size=10_000), 1)
    assert shrunk.char_count <= SHRINK_LEVEL_LIMITS[0]
    assert shrunk.meta["truncated_from"] == 10_000


def test_level_two_is_stricter_than_level_one():
    original = _item(size=10_000)
    assert shrink_item(original, 2).char_count <= SHRINK_LEVEL_LIMITS[1]
    assert shrink_item(original, 2).char_count < shrink_item(original, 1).char_count


def test_shrinking_an_already_short_item_changes_nothing():
    """反向自检：不能让 1 级压缩把本来不长的内容也打上「被截断」的标记。"""
    original = _item(size=10)
    assert shrink_item(original, 1) == original
    assert "truncated_from" not in shrink_item(original, 1).meta


def test_the_last_level_replaces_content_with_a_readable_note():
    summary = shrink_item(_item(size=10_000), MAX_SHRINK_LEVEL)
    assert summary.char_count < 400
    assert "10000" in summary.text
    assert summary.meta["omitted_for_budget"] is True
    assert summary.meta["original_chars"] == 10_000


def test_shrinking_preserves_kind_and_label():
    """压缩后仍要知道「这条原本是什么」，否则模型无法决定要不要重新索取。"""
    shrunk = shrink_item(_item(kind="file_content", label="a/b.py", size=10_000), 1)
    assert (shrunk.kind, shrunk.label) == ("file_content", "a/b.py")


# ==========================================================================
# budget：条数与总量
# ==========================================================================


def test_items_within_the_count_limit_are_untouched():
    items = [_item(label=f"i{n}") for n in range(3)]
    kept, dropped, notes = limit_item_count(items, 5)
    assert kept == tuple(items)
    assert dropped == 0
    assert notes == ()


def test_over_the_count_limit_keeps_the_most_recent():
    items = [_item(label=f"i{n}") for n in range(5)]
    kept, dropped, notes = limit_item_count(items, 2)
    assert [item.label for item in kept] == ["i3", "i4"]
    assert dropped == 3
    assert notes and "3" in notes[0]


def test_count_limit_of_zero_drops_everything_and_says_so():
    kept, dropped, notes = limit_item_count([_item(), _item()], 0)
    assert kept == ()
    assert dropped == 2
    assert notes


def test_count_limit_of_zero_on_empty_input_is_quiet():
    """没有内容可丢时不该产出一句「有 0 条被省略」。"""
    kept, dropped, notes = limit_item_count([], 0)
    assert (kept, dropped, notes) == ((), 0, ())


def test_char_trim_drops_the_oldest_first():
    items = [_item(label=f"i{n}", size=100) for n in range(4)]
    kept, dropped, notes = trim_total_chars(items, 250)
    assert [item.label for item in kept] == ["i2", "i3"]
    assert dropped == 2
    assert notes


def test_char_trim_within_budget_is_quiet():
    items = [_item(size=10) for _ in range(2)]
    kept, dropped, notes = trim_total_chars(items, 10_000)
    assert kept == tuple(items)
    assert (dropped, notes) == (0, ())


def test_char_budget_of_zero_on_empty_input_is_quiet():
    """与条数上限对称的那条：没有内容可丢时不该凭空产出一条空说明。

    这个位置就是上面那个「尾逗号把空元组包了一层」的 bug 的另一半——`limit_item_count`
    被用例抓到之后，回过头发现 `trim_total_chars` 有一模一样的写法。
    """
    kept, dropped, notes = trim_total_chars([], 0)
    assert (kept, dropped, notes) == ((), 0, ())


def test_char_budget_of_zero_drops_everything_and_says_so():
    kept, dropped, notes = trim_total_chars([_item(), _item()], 0)
    assert kept == ()
    assert dropped == 2
    assert notes


def test_char_trim_actually_gets_under_the_budget():
    items = [_item(size=1000) for _ in range(10)]
    kept, _dropped, _notes = trim_total_chars(items, 2500)
    assert sum(item.char_count for item in kept) <= 2500


# ==========================================================================
# budget：整体
# ==========================================================================


def test_budget_enforcement_is_a_no_op_when_within_budget():
    items = [_item(size=100) for _ in range(3)]
    result = enforce_budget(items, max_items=10, total_chars=10_000)
    assert result.items == tuple(items)
    assert result.omitted_total == 0
    assert result.shrink_level == 0
    assert result.notes == (), "没有丢任何东西就不该说「有内容被省略」"


def test_mild_overflow_is_solved_by_truncation_without_losing_items():
    """先压单条再丢条目：能靠截断解决就不要丢整条（丢条目损失的信息更多）。"""
    items = [_item(label="big", size=30_000)]
    result = enforce_budget(items, max_items=10, total_chars=6_000)
    assert len(result.items) == 1
    assert result.omitted_total == 0
    assert result.shrink_level >= 1
    assert result.total_chars <= 6_000


def test_many_short_items_do_not_get_replaced_by_summaries():
    """200 条各 100 字的短上下文：该丢条目，不该把内容压成说明文字。

    这是「某一级没改动就停止升级」要守住的行为——一路压到最简会把本来就不长的内容
    全部换成说明，信息被毁掉而问题根本没解决。
    """
    items = [_item(label=f"i{n}", size=100) for n in range(200)]
    result = enforce_budget(items, max_items=8, total_chars=10_000)

    assert len(result.items) == 8
    assert result.omitted_by_count == 192
    assert result.shrink_level == 0
    assert all(not item.meta.get("omitted_for_budget") for item in result.items)
    assert all(item.char_count == 100 for item in result.items)


def test_a_lot_of_long_items_gets_both_compressed_and_trimmed():
    items = [_item(label=f"i{n}", size=20_000) for n in range(40)]
    result = enforce_budget(items, max_items=5, total_chars=8_000)

    assert len(result.items) == 5
    assert result.shrink_level >= 1
    assert result.omitted_total > 0
    assert result.total_chars <= 8_000
    # 40 条各 20,000 字：留下的 5 条应当压到第 2 级（1,200 字）就够，而不是被换成说明
    # 文字。原先这里只断言 `shrink_level >= 1`，所以「内容全被换成说明」也能通过。
    assert all(not item.meta.get("omitted_for_budget") for item in result.items)
    assert result.total_chars > 1_000, f"留下的应该还是真实内容，实际只有 {result.total_chars} 字"


def test_a_batch_over_the_item_cap_keeps_real_content():
    """**回归守卫**：一次索要的条数超过条数上限时，不能把内容全换成说明文字。

    真实触发路径：模型一次索要 12 个文件（预算本来就允许 12 次请求），而条数上限是 8。
    体积判断若把「反正会被丢掉的那 4 条」也算进去，循环就永远满足不了条件 —— 条数是
    丢条目才能解决的，靠压缩解决不了 —— 于是一路升到 3 级、把 12 条内容全部换成说明。
    修之前的实测结果：保留 8 条共 682 字的说明文字；修之后是 32,000 字的真实内容。
    """
    items = [_item(label=f"i{n}", size=14_000) for n in range(12)]

    # 预算装得下 8 条各 14,000 字时：只丢条目，一点内容都不用压
    roomy = enforce_budget(items, max_items=8, total_chars=120_000)
    assert roomy.shrink_level == 0
    assert roomy.total_chars == 8 * 14_000

    # 150 个提交的真实残额：变更摘要本身要占掉约 39,000 字，剩下的不够装 8 条完整 diff
    tight = enforce_budget(items, max_items=8, total_chars=80_000)
    assert len(tight.items) == 8
    assert tight.omitted_by_count == 4
    assert tight.shrink_level == 1, "截断到 4,000 字就已经进预算了，不该继续升级"
    assert all(not item.meta.get("omitted_for_budget") for item in tight.items), "内容被换成了说明文字"
    assert tight.total_chars == 8 * 4_000, "留下的应该是 8 条各 4,000 字的真实内容"


def test_going_over_the_item_cap_alone_does_not_trigger_shrinking():
    """条数超标但体积没超标：只丢条目，不要说「已压缩到第 N 级」。

    一句不该出现的压缩说明会让模型以为内容被截断过，进而反复重新索取。
    """
    items = [_item(label=f"i{n}", size=100) for n in range(12)]
    result = enforce_budget(items, max_items=8, total_chars=120_000)

    assert len(result.items) == 8
    assert result.omitted_by_count == 4
    assert result.shrink_level == 0
    assert not any("压缩到第" in note for note in result.notes)
    assert all(item.char_count == 100 for item in result.items)


def test_content_is_only_replaced_by_markers_when_nothing_else_can_fit():
    """最后一级（换成说明文字）只在连截断都装不下时才允许出现。

    把预算压到比「条数上限 × 1,200 字」还小，才轮到它。
    """
    items = [_item(label=f"i{n}", size=14_000) for n in range(12)]
    result = enforce_budget(items, max_items=8, total_chars=600)
    assert result.shrink_level == MAX_SHRINK_LEVEL
    assert all(item.meta.get("omitted_for_budget") for item in result.items)
    assert result.notes, "内容被换成了说明文字，必须告诉模型"


def test_every_omission_comes_with_a_note():
    """**本文件最要紧的一条性质**：任何内容损失都必须有一句可读的说明。

    没有它，模型会以为自己看到了全部，然后基于残缺信息给出很确定的结论。
    """
    items = [_item(label=f"i{n}", size=20_000) for n in range(40)]
    result = enforce_budget(items, max_items=5, total_chars=8_000)

    assert result.omitted_total > 0
    assert result.notes, "丢了内容却没有任何说明"
    joined = " ".join(result.notes)
    assert "省略" in joined or "截断" in joined


def test_enforcement_is_idempotent():
    """已经压进预算的结果再压一次不该继续缩水（否则每轮都会再丢一点）。"""
    items = [_item(label=f"i{n}", size=20_000) for n in range(40)]
    first = enforce_budget(items, max_items=5, total_chars=8_000)
    second = enforce_budget(first.items, max_items=5, total_chars=8_000)
    assert second.items == first.items
    assert second.omitted_total == 0


def test_continuation_summary_lists_only_recent_labels():
    items = [_item(label=f"i{n}") for n in range(10)]
    summary = build_continuation_summary(items, keep=2)
    assert "10" in summary
    assert "i8" in summary and "i9" in summary
    assert "i0" not in summary


def test_continuation_summary_says_the_content_is_not_attached():
    """摘要必须说清「这些内容本轮没带上」，否则模型会以为自己已经看过了。"""
    summary = build_continuation_summary([_item()], keep=1)
    assert "未" in summary


def test_estimate_chars_sums_message_contents():
    assert estimate_chars([{"content": "abc"}, {"content": "de"}]) == 5
    assert estimate_chars([{"content": None}, {}]) == 0


# ==========================================================================
# 模型上下文窗口：**规则已经改了**，用例搬到了 tests/test_ai_budget_vs_model_window.py
#
# 原先这里有 `clamp_to_model_window` 的三条用例，守的是「只在按最乐观的 1 字 1 token
# 也算得超窗时才压」。那条规则后来被 `budget.effective_prompt_budget`（窗口 × 60% 水位）
# 取代 —— 理由见那边的 docstring：旧规则在**窗口问不到**时什么都不做，于是把预算配得很大
# 的项目必然撞窗被拒、整次分析作废。新规则与它的全部用例都在那个文件里。
# ==========================================================================


# ==========================================================================
# rules：相似度
# ==========================================================================


def test_tokenize_extracts_latin_words():
    assert "login" in tokenize("Login timeout in auth_module")


def test_tokenize_uses_bigrams_for_chinese():
    """中文按二元组切分：单字区分度太低（「删除」与「增加」单字集合高度重叠）。"""
    assert "删除" in tokenize("道具被删除")
    assert "道具" in tokenize("道具被删除")


def test_tokenize_handles_a_single_chinese_character():
    assert tokenize("删") == frozenset({"删"})


def test_tokenize_of_empty_text_is_empty():
    assert tokenize("") == frozenset()
    assert tokenize(None) == frozenset()


def test_containment_of_identical_sets_is_one():
    assert containment(tokenize("道具被删除"), tokenize("道具被删除")) == 1.0


def test_containment_of_disjoint_sets_is_zero():
    assert containment(tokenize("道具被删除"), tokenize("登录超时")) == 0.0


def test_containment_of_two_empty_sets_is_zero_not_one():
    """**这条是刻意的，也是最容易写错的一处。**

    数学上 `|∅∩∅| / min(0,0)` 是 0/0，很多实现按约定返回 1.0。在这里那等于把所有
    「没有可用词」的条目互相判成重复，去重后只剩一条——静默丢结论。
    """
    assert containment(frozenset(), frozenset()) == 0.0


def test_containment_is_not_symmetric():
    """这不是缺陷而是选它的理由：`{a}` 被 `{a,b,c}` 完全覆盖。

    对称的度量（Jaccard）会因为这个 `b,c` 而把相似度拉到 1/3，于是「同一条问题多说
    了半句」就被判成两条。
    """
    short, long = tokenize("道具ID被删除"), tokenize("道具ID被删除且没有备份")
    assert containment(short, long) == 1.0


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("道具ID被删除", "道具ID被删除且没有备份", 0.500),
        ("【配表】道具ID被删除", "道具ID被删除", 0.714),
        ("【配置表】道具ID被删除", "道具ID被删除", 0.625),
        ("道具ID被删除", "道具ID被删除（同一问题换了个说法）", 0.357),
        ("等级上限配错", "等级上限配错导致无法升级", 0.455),
        ("道具ID被删除", "登录重连超时未处理", 0.000),
        ("商城价格配置不一致", "邮件附件领取失败", 0.000),
    ],
)
def test_jaccard_is_weak_on_chinese_titles(left, right, expected):
    """**这条用例记录的是「当初为什么没选 Jaccard」的实测数据。**

    上面前 5 行是**同一条问题**的不同说法，Jaccard 只有 0.36~0.71 —— 用 0.7 当门槛的话
    大半都合并不掉，而去重就没在工作了。最后两行是无关标题，都是 0，说明问题不在
    「区分度不够」而在分母。

    这里只断言 Jaccard 的数值，用来钉住 `containment` 这个选择的前提；一旦哪天换了
    分词方式，这条会先红，而不是让去重悄悄退化。
    """
    left_tokens, right_tokens = tokenize(left), tokenize(right)
    union = left_tokens | right_tokens
    measured = len(left_tokens & right_tokens) / len(union) if union else 0.0
    assert measured == pytest.approx(expected, abs=0.001)


def test_containment_catches_the_pairs_jaccard_misses():
    """同一条问题的 5 种说法，containment 全部判为 1.0 —— 这正是换掉度量的收益。"""
    pairs = [
        ("道具ID被删除", "道具ID被删除且没有备份"),
        ("【配表】道具ID被删除", "道具ID被删除"),
        ("【配置表】道具ID被删除", "道具ID被删除"),
        ("道具ID被删除", "道具ID被删除（同一问题换了个说法）"),
        ("等级上限配错", "等级上限配错导致无法升级"),
    ]
    for left, right in pairs:
        assert containment(tokenize(left), tokenize(right)) == 1.0, left


# ==========================================================================
# rules：去重
# ==========================================================================


def test_same_commit_and_similar_title_is_a_duplicate():
    assert is_probable_duplicate(_anomaly(), _anomaly(title="【配表】道具ID被删除"))


def test_different_commit_is_never_a_duplicate():
    """跨提交的相似标题几乎必然是两件独立的事，合成一条会漏掉一处。"""
    assert not is_probable_duplicate(_anomaly(), _anomaly(commit=COMMIT_B))


def test_different_file_is_never_a_duplicate():
    """两个文件里的同一个问题要分别跟进；合并会让其中一处永远没人管。"""
    assert not is_probable_duplicate(
        _anomaly(), _anomaly(file_path="config/40_monster/monster.xlsx")
    )


def test_global_anomalies_without_a_path_can_still_be_duplicates():
    """有些异常是全局性的（例如「没走配表评审流程」），没有文件路径可比。"""
    left = _anomaly(file_path="")
    right = _anomaly(file_path="")
    assert is_probable_duplicate(left, right)


def test_unrelated_titles_are_not_duplicates():
    """反向自检：去重不能是无差别合并。"""
    assert not is_probable_duplicate(_anomaly(), _anomaly(title="登录重连超时未处理"))


def test_a_completely_reworded_title_is_not_merged():
    """**记录能力边界**：只换了几个词、语序也变了的重述不会被合并。

    这是刻意留的边界而不是待修的缺陷：漏合并只是让用户多看一条（他可以直接忽略），
    而错合并会让一条真实问题永远消失且不留痕迹。真正语义级的等价判断要靠模型的
    「不要跨维度重复报同一条」约束，以及人工处置闭环。
    """
    assert not is_probable_duplicate(
        _anomaly(title="道具ID被删除"), _anomaly(title="删除了已放出的道具ID")
    )


def test_titles_below_the_token_floor_are_only_merged_when_identical():
    """词太少时「被覆盖」说明不了任何事：`登录超时` 被任何含它的长标题覆盖都是 1.0。"""
    short = _anomaly(title="登录超时")
    assert len(tokenize("登录超时")) < MIN_SIMILARITY_TOKENS

    assert is_probable_duplicate(short, _anomaly(title="登录超时")), "完全相同就该合并"
    assert not is_probable_duplicate(short, _anomaly(title="登录超时后奖励未发放"))


def test_an_empty_title_never_merges():
    assert not is_probable_duplicate(_anomaly(title=""), _anomaly(title=""))


def test_threshold_is_respected():
    left = _anomaly(title="道具ID被删除")
    right = _anomaly(title="删除了已放出的道具ID")
    assert is_probable_duplicate(left, right, threshold=0.5)
    assert not is_probable_duplicate(left, right, threshold=0.99)


# ==========================================================================
# rules：指纹
# ==========================================================================


def test_fingerprint_is_stable_for_the_same_anomaly():
    assert anomaly_fingerprint(_anomaly()) == anomaly_fingerprint(_anomaly())


def test_fingerprint_absorbs_punctuation_and_spacing():
    """标点、空白、大小写这类排版差异不该改变身份，否则处置结果会无谓地丢。"""
    base = _anomaly(title="道具ID被删除")
    assert anomaly_fingerprint(base) == anomaly_fingerprint(_anomaly(title="道具 ID 被删除"))
    assert anomaly_fingerprint(base) == anomaly_fingerprint(_anomaly(title="道具ID被删除。"))


def test_fingerprint_does_not_absorb_reordering_or_extra_words():
    """**记录能力边界**：中文二元组对语序敏感，加词/换序都会改变指纹。

    第一版我把这里写反了，docstring 里声称「词集能吸收措辞抖动」——实测不成立
    （`道具被删除` 的二元组是 {道具,具被,被删,删除}，`被删除道具` 是 {被删,删除,除道,道具}）。
    这不是缺陷，但意味着**指纹精确匹配必须配一次 `is_probable_duplicate` 的模糊兜底**，
    否则用户会遇到「我明明忽略过了又冒出来」。
    """
    base = _anomaly(title="道具ID被删除")
    assert anomaly_fingerprint(base) != anomaly_fingerprint(_anomaly(title="被删除道具"))
    assert anomaly_fingerprint(base) != anomaly_fingerprint(
        _anomaly(title="道具ID被删除且没有备份")
    )
    # 而模糊匹配能把这两种都兜住——这正是兜底那一步存在的理由。
    assert is_probable_duplicate(base, _anomaly(title="道具ID被删除且没有备份"))


def test_fingerprint_differs_across_files():
    assert anomaly_fingerprint(_anomaly()) != anomaly_fingerprint(
        _anomaly(file_path="config/40_monster/monster.xlsx")
    )


def test_fingerprint_differs_across_commits():
    assert anomaly_fingerprint(_anomaly()) != anomaly_fingerprint(_anomaly(commit=COMMIT_B))


# ==========================================================================
# rules：阈值
# ==========================================================================


def test_defaults_match_the_documented_values():
    thresholds = RuleThresholds()
    assert thresholds.min_severity == "high"
    assert thresholds.max_anomalies == DEFAULT_MAX_ANOMALIES


def test_from_config_reads_the_project_row():
    thresholds = RuleThresholds.from_config(
        {"min_severity": "CRITICAL", "min_confidence": "very_high", "max_anomalies_per_run": "3"}
    )
    assert thresholds.min_severity == "critical"
    assert thresholds.min_confidence == "very_high"
    assert thresholds.max_anomalies == 3


def test_from_config_falls_back_when_the_row_is_empty():
    assert RuleThresholds.from_config(None) == RuleThresholds()
    assert RuleThresholds.from_config({}) == RuleThresholds()


def test_from_config_clamps_an_absurd_cap():
    """数值字段来自数据库，可能是任意字符串。越界值夹到区间内，而不是让整轮崩掉。"""
    assert RuleThresholds.from_config({"max_anomalies_per_run": "999999"}).max_anomalies == 200
    assert RuleThresholds.from_config({"max_anomalies_per_run": "-5"}).max_anomalies == 0
    assert RuleThresholds.from_config({"max_anomalies_per_run": "abc"}).max_anomalies == (
        DEFAULT_MAX_ANOMALIES
    )


def test_an_unknown_severity_is_rejected_not_silently_downgraded():
    """**不降级**：悄悄退回默认值会让「我明明调严了却报得一样多」变成静默错误。

    配置在写入时就被校验，所以走到这里唯一的原因是有人直接改了库 —— 让这一轮明确
    失败，比悄悄换一套告警口径让人信任。
    """
    with pytest.raises(RulesConfigError):
        RuleThresholds.from_config({"min_severity": "medium"})


def test_passes_depends_on_both_thresholds():
    strict = RuleThresholds(min_severity="critical", min_confidence="very_high")
    assert not strict.passes(_anomaly())  # critical/high → confidence 不够
    assert strict.passes(_anomaly(confidence="very_high"))
    assert not strict.passes(_anomaly(severity="high", confidence="very_high"))


def test_raising_a_threshold_can_only_shrink_the_output():
    """单调性：门槛调严只会报得更少。这是这套配置能被人信任的根本原因。"""
    anomalies = [
        _anomaly(title="道具ID被删除", severity="critical", confidence="very_high"),
        _anomaly(title="等级上限配错", severity="high", confidence="high"),
    ]
    loose = normalize_anomalies(anomalies, RuleThresholds(min_severity="high", min_confidence="high"))
    tight = normalize_anomalies(
        anomalies, RuleThresholds(min_severity="critical", min_confidence="very_high")
    )
    assert len(tight.anomalies) <= len(loose.anomalies)
    assert len(tight.anomalies) == 1


def test_thresholds_are_part_of_the_revision_component():
    """门槛变了就是另一个问题，幂等键必须随之变化，否则用户以为新门槛没生效。"""
    assert RuleThresholds().revision_component() != RuleThresholds(
        min_severity="critical"
    ).revision_component()


# ==========================================================================
# rules：归一化
# ==========================================================================


def test_normalizing_an_empty_list_says_nothing():
    result = normalize_anomalies([], RuleThresholds())
    assert result.anomalies == ()
    assert result.dropped == ()


def test_a_passing_anomaly_survives_unchanged():
    """反向自检：归一化不能顺手改掉合格条目。"""
    anomaly = _anomaly()
    result = normalize_anomalies([anomaly], RuleThresholds())
    assert result.anomalies == (anomaly,)


def test_sub_threshold_items_are_dropped_with_the_offending_value():
    result = normalize_anomalies(
        [_anomaly(severity="high", confidence="high")],
        RuleThresholds(min_severity="critical"),
    )
    assert result.anomalies == ()
    assert any("门槛" in item.reason for item in result.dropped)
    assert any(item.detail == "high/high" for item in result.dropped)


def test_dropped_records_carry_the_original_index():
    """记账里的下标必须是模型原始数组里的位置，否则「第 3 条为什么被丢了」答不上来。

    刻意让**两个不同阶段**各丢一条（门槛过滤 + 近似去重），因为「下标改成过滤后的
    位置」这个 bug 在只有一个丢弃阶段、且丢弃都发生在末尾时是看不出来的 —— 第一版
    用例就是这样，变异验证时全绿，说明它没在测东西。
    """
    result = normalize_anomalies(
        [
            _anomaly(title="合格的一条"),
            _anomaly(title="等级上限配错", severity="high"),
            _anomaly(title="严重度不够被丢", severity="high"),
            _anomaly(title="道具ID被删除"),
            _anomaly(title="【配表】道具ID被删除"),
        ],
        RuleThresholds(min_severity="critical"),
    )
    assert [item.index for item in result.dropped] == [1, 2, 4]


def test_evidence_beyond_the_cap_is_trimmed_and_accounted():
    result = normalize_anomalies(
        [_anomaly(evidence=("一", "二", "三", "四", "五"))], RuleThresholds()
    )
    assert len(result.anomalies[0].evidence) == 3
    assert result.evidence_trimmed == 1
    assert any(item.kind == "evidence" for item in result.dropped)


def test_evidence_trimming_does_not_happen_for_dropped_items():
    """先过滤再裁剪：被丢掉的条目不该留下「证据被裁了」的记账，那会让人以为漏了什么。"""
    result = normalize_anomalies(
        [_anomaly(severity="high", evidence=("一", "二", "三", "四"))],
        RuleThresholds(min_severity="critical"),
    )
    assert result.evidence_trimmed == 0
    assert not any(item.kind == "evidence" for item in result.dropped)


def test_duplicates_are_merged_and_counted():
    result = normalize_anomalies(
        [
            _anomaly(title="道具ID被删除"),
            _anomaly(title="道具ID被删除（同一问题换了个说法）"),
        ],
        RuleThresholds(),
    )
    assert len(result.anomalies) == 1
    assert result.duplicates_removed == 1
    assert any("去重" in item.reason for item in result.dropped)


def test_capping_keeps_the_more_severe_items():
    """**封顶不能按顺序截断**：模型把唯一一条 critical 放在最后是完全可能的。

    标题刻意选成互不相似的词组 —— 否则它们会先被近似去重合并掉，这条用例就变成在测
    去重而不是封顶了（第一版用 `普通问题0`/`普通问题1` 就踩了这个坑：词集高度重叠，
    被判定为同一条）。
    """
    anomalies = [
        _anomaly(title="道具ID被删除", severity="high"),
        _anomaly(title="等级上限配错", severity="high"),
        _anomaly(title="登录重连超时", severity="high"),
        _anomaly(title="邮件附件遗漏", severity="high"),
        _anomaly(title="最严重的一条", severity="critical", confidence="very_high"),
    ]

    result = normalize_anomalies(anomalies, RuleThresholds(max_anomalies=2))

    titles = [item.title for item in result.anomalies]
    assert "最严重的一条" in titles
    assert result.capped == 3
    assert result.duplicates_removed == 0


def test_capped_items_are_recorded_one_by_one():
    """「少报 3 条」不如「少报的是哪 3 条」，后者才能判断被砍的是不是关键那条。"""
    anomalies = [
        _anomaly(title="道具ID被删除"),
        _anomaly(title="等级上限配错"),
        _anomaly(title="登录重连超时"),
        _anomaly(title="邮件附件遗漏"),
        _anomaly(title="商城价格不符"),
    ]
    result = normalize_anomalies(anomalies, RuleThresholds(max_anomalies=2))

    # 按 `kind` 挑，**不按 reason 的措辞挑**：措辞是给人读的，会改；而「哪几条是被上限
    # 截掉的」这件事要能被读取侧稳定地问出来（`KIND_ANOMALY_CAP`，与协议层那个
    # `"anomaly"` 分开）。
    cap_records = [item for item in result.dropped if item.kind == KIND_ANOMALY_CAP]
    assert len(cap_records) == 3
    assert len({item.index for item in cap_records}) == 3
    assert all("上限" in item.reason for item in cap_records)


def test_output_order_is_rank_order_even_without_capping():
    """排序与是否触发封顶无关。

    否则「多报了一条导致超限」会让整份清单的顺序突然变化，用户以为改动很大。
    """
    anomalies = [
        _anomaly(title="道具ID被删除", severity="high"),
        _anomaly(title="最严重的一条", severity="critical", confidence="very_high"),
        _anomaly(title="登录重连超时", severity="high"),
    ]
    result = normalize_anomalies(anomalies, RuleThresholds(max_anomalies=10))

    assert result.capped == 0
    assert result.anomalies[0].title == "最严重的一条"


def test_cap_anomalies_reports_how_many_were_dropped():
    anomalies = [
        _anomaly(title="道具ID被删除"),
        _anomaly(title="等级上限配错"),
        _anomaly(title="登录重连超时"),
        _anomaly(title="邮件附件遗漏"),
        _anomaly(title="商城价格不符"),
    ]
    kept, dropped = cap_anomalies(anomalies, 2)
    assert len(kept) == 2
    assert dropped == 3


def test_cap_of_zero_returns_nothing_but_counts_everything():
    kept, dropped = cap_anomalies([_anomaly()], 0)
    assert kept == ()
    assert dropped == 1


def test_duplicates_do_not_consume_cap_slots():
    """先去重再封顶：否则重复项各占一个名额，最后报出的条数少于上限却没有任何一条
    被记为「因超限被丢」。"""
    anomalies = [
        _anomaly(title="道具ID被删除"),
        _anomaly(title="道具ID被删除（重复）"),
        _anomaly(title="等级上限配错"),
    ]
    result = normalize_anomalies(anomalies, RuleThresholds(max_anomalies=2))

    assert len(result.anomalies) == 2
    assert result.capped == 0
    assert result.duplicates_removed == 1


# ==========================================================================
# rules：版本
# ==========================================================================


def test_rules_version_is_stable_and_labelled():
    version = rules_version()
    assert version.startswith("rules-")
    assert version == rules_version()


def test_rules_version_is_derived_from_the_rule_source():
    """**这条防的是「版本号被写死成常量」。**

    幂等键里「规则变了就重跑」全靠这个哈希。如果有人为了省一次文件读把它改成
    `return "rules-v1"`，那么以后每次改规则都会继续复用旧结果，而用户看到的是
    「我的规则改动没生效」——极难排查。所以这里按同样的算法独立重算一遍来比对。
    """
    import hashlib
    from pathlib import Path

    import services.ai.rules as rules_module

    base = Path(rules_module.__file__).resolve().parent
    digest = hashlib.sha1()
    for name in RULE_SOURCE_FILES:
        digest.update(name.encode("utf-8"))
        digest.update((base / name).read_bytes())

    assert rules_version() == f"rules-{digest.hexdigest()[:12]}"


def test_a_modified_threshold_object_is_detected_by_revision_component():
    """真正影响结果的输入变化，一定要在幂等键里体现出来。"""
    base = RuleThresholds()
    assert base.revision_component() != replace(base, max_anomalies=1).revision_component()
    assert base.revision_component() != replace(
        base, similarity_threshold=0.5
    ).revision_component()


# ==========================================================================
# budget：压缩层的「不许静默 + 不许说小」
#
# 这三条都是 2026-09 复核出来的：它们不在取数侧（`context_tools` / `windowed_view`），
# 而在**压缩侧** —— 内容已经渲染好了，`enforce_budget` 再按预算砍第二刀。同一批毛病
# 在取数侧修过（见 60493bf），压缩侧原样重演了一遍。
# ==========================================================================


def _windowed_item(*, hunks: int, lines_per_hunk: int, label: str = "dummy") -> ContextItem:
    """一条真的走 `render_window` 渲染过的条目（带抬头、带 `meta["segments"]`）。"""
    body = "\n".join(
        f"@@ -{index * lines_per_hunk},{lines_per_hunk} +{index * lines_per_hunk},{lines_per_hunk} @@\n"
        + "\n".join(f"+ line {row}" for row in range(lines_per_hunk))
        for index in range(hunks)
    )
    text, meta = render_window(
        kind="file_diff", label=label, text=body, window="", limit=11_000
    )
    return ContextItem(kind="file_diff", label=label, text=text, meta=dict(meta))


def test_the_omitted_note_quotes_the_original_size_not_the_previous_tier():
    """第 3 级那句「原本 N 字」必须量的是**原文**，不是上一级的产物。

    压缩是逐级递进的，第 3 级拿到的是第 2 级压完的 1,200 字。原先这里直接写
    `item.char_count`，实测一份 10,999 字的差异被写成「原本 1200 字」—— 模型据此
    判断「这份东西本来就不长，看不看无所谓」，于是不再索取。
    """
    item = _windowed_item(hunks=40, lines_per_hunk=40)
    real_size = item.char_count
    assert real_size > SHRINK_LEVEL_LIMITS[0]

    summary = shrink_item(shrink_item(item, 1), MAX_SHRINK_LEVEL)
    assert f"原本 {real_size} 字" in summary.text
    assert summary.meta["original_chars"] == real_size


def test_the_shrink_layer_marks_what_it_cut_as_truncated():
    """压缩砍掉的也要算「截断」。

    `engine._batch_notes` 与消耗面板的「截断」列读的都是 `meta["truncated"]`，而这一层
    原先只写 `truncated_from` / `shrink_level` —— 那两个键**没有任何消费方**，于是
    「这一条被砍了」在提示词和面板上都不存在（`truncated` 还停在取数那一刻的 False）。
    """
    item = _windowed_item(hunks=40, lines_per_hunk=12)
    assert item.meta["truncated"] is False, "前提：取数侧自己没截断，这一条是被压缩砍的"

    shrunk = shrink_item(item, 1)
    assert shrunk.char_count < item.char_count
    assert shrunk.meta["truncated"] is True


def test_a_shrunk_windowed_item_retracts_the_segment_claim_in_its_header():
    """抬头那句「这里是第 N 段」被压缩砍过之后不再成立，必须在正文里撤回。

    它印在正文第一行 —— 恰好是尾截断唯一砍不到的位置 —— 模型会照着它认为第 1-N 段
    都到手了，只去要第 N+1 段，被砍掉的几段永远拿不到。这与 `windowed_view` 的
    overflow 分支防的是同一件事，只是砍的位置换到了预算侧。
    """
    item = _windowed_item(hunks=40, lines_per_hunk=12)
    shrunk = shrink_item(item, 1)
    lines = shrunk.text.splitlines()

    assert lines[0].startswith("[file_diff]"), "抬头仍在第一行（撤回句不能把它挤走）"
    assert "被预算又砍了一次" in lines[1], "撤回句要排在抬头之后、正文之前"
    assert "不成立" in lines[1]


def test_the_retraction_note_is_not_added_to_content_that_has_no_segments():
    """反向自检：切不出段的普通内容没有段号可撤，不许凭空多出一句话。

    也不能因为这句话把本来装得下的内容挤出去 —— 它占的额度必须从上限里先扣掉。
    """
    plain = ContextItem(kind="file_content", label="a.py", text="x" * 9_000, meta={})
    shrunk = shrink_item(plain, 1)

    assert "被预算又砍了一次" not in shrunk.text
    assert shrunk.char_count <= SHRINK_LEVEL_LIMITS[0]

    windowed = shrink_item(_windowed_item(hunks=40, lines_per_hunk=40), 1)
    assert windowed.char_count <= SHRINK_LEVEL_LIMITS[0], "加了撤回句也不能超过这一级的上限"
