# -*- coding: utf-8 -*-
"""累积基线与变更单元。

两条性质最要紧，其余都是为了把它们钉住：

1. **人工的分诊结果不能在下一轮被作废。** 标成「已忽略」的结论不再出现在报告里，
   但**文件又变了的时候必须回来**（证据过期了）—— 否则「忽略」等于「永远看不见」。
2. **同一次改动里的表与生成物是一个评审单元。** 分开看，每一侧都正常，而
   「表改了、生成物没跟上」这类问题只有一起看才看得见。
"""
from __future__ import annotations

from services.ai.baseline import (
    DEFAULT_BASELINE_CHARS,
    DISPOSITION_CONFIRMED,
    DISPOSITION_IGNORED,
    DISPOSITION_PENDING,
    DISPOSITIONS,
    STATE_NEEDS_RECHECK,
    STATE_OPEN,
    STATE_SUPPRESSED,
    BaselineFinding,
    build_baseline_digest,
    classify,
    partition_incoming,
    suppressed_fingerprints,
)
from services.ai.bundles import (
    KIND_GENERATED_PAIR,
    KIND_SINGLE,
    build_bundles,
    companions_of,
    describe_bundles,
)


def _finding(fingerprint: str, *, path: str = "src/a.lua", disposition: str = DISPOSITION_PENDING,
             title: str = "【吸灵器】窗帘拖拽第二段不出现", severity: str = "high") -> BaselineFinding:
    return BaselineFinding(
        fingerprint=fingerprint,
        title=title,
        severity=severity,
        file_path=path,
        disposition=disposition,
    )


# ==========================================================================
# 防漂移：处置状态必须与模型层一致
# ==========================================================================


def test_dispositions_agree_with_the_model_layer():
    """本模块刻意不 import 模型层（那会把 SQLAlchemy 拉进来，纯逻辑就没法脱库单测了），
    代价是两边可能各自漂移。漂移的后果是静默的：人工标了「忽略」，基线却认不出这个值，
    于是那条结论每轮都被重新提起 —— 正是这套机制要消灭的行为。
    """
    from models.ai_analysis.anomaly import DISPOSITIONS as MODEL_DISPOSITIONS

    assert tuple(DISPOSITIONS) == tuple(MODEL_DISPOSITIONS)


# ==========================================================================
# 已忽略项的抑制与复活
# ==========================================================================


def test_an_ignored_finding_is_suppressed():
    findings = classify([_finding("a" * 8, disposition=DISPOSITION_IGNORED)])

    assert findings[0].state == STATE_SUPPRESSED
    assert suppressed_fingerprints(findings) == frozenset({"a" * 8})


def test_an_ignored_finding_comes_back_when_its_file_changes_again():
    """**这条是「忽略」不会变成「永远看不见」的保证。**

    文件又变了，原来的证据就过期了：那时必须重新确认一次，而不是因为「人工忽略过」
    就永远沉默。
    """
    findings = classify(
        [_finding("a" * 8, path="config/[30]道具表_CfgItem.xlsx", disposition=DISPOSITION_IGNORED)],
        changed_paths=["config/[30]道具表_CfgItem.xlsx"],
    )

    assert findings[0].state == STATE_NEEDS_RECHECK
    assert suppressed_fingerprints(findings) == frozenset(), "复活了的结论不该仍被抑制"


def test_an_ignored_finding_whose_file_did_not_change_stays_quiet():
    """反证：没变就继续安静，不要因为别处改了文件就把它翻出来。"""
    findings = classify(
        [_finding("a" * 8, path="config/other.xlsx", disposition=DISPOSITION_IGNORED)],
        changed_paths=["config/[30]道具表_CfgItem.xlsx"],
    )
    assert findings[0].state == STATE_SUPPRESSED


def test_pending_and_confirmed_findings_stay_visible():
    findings = classify(
        [
            _finding("a" * 8, disposition=DISPOSITION_PENDING),
            _finding("b" * 8, disposition=DISPOSITION_CONFIRMED),
        ]
    )
    assert [item.state for item in findings] == [STATE_OPEN, STATE_OPEN]


def test_path_matching_survives_cosmetic_differences():
    """`./a/b.xlsx` 与 `a/b.xlsx` 是同一个文件。

    不归一化的后果很隐蔽：文件明明又变了，结论却因为「路径字符串不相等」被判成没变，
    于是一个已经过期的证据被当成还有效。
    """
    findings = classify(
        [_finding("a" * 8, path="./config/[30]道具表_CfgItem.xlsx", disposition=DISPOSITION_IGNORED)],
        changed_paths=["config/[30]道具表_CfgItem.xlsx"],
    )
    assert findings[0].state == STATE_NEEDS_RECHECK


# ==========================================================================
# 判重
# ==========================================================================


def test_partition_incoming_separates_new_from_already_known():
    """模型重复报一条旧问题，**不算新增**。

    算进去会让「本次新增 N 条」一路虚高，也看不出它其实是老问题被又报了一遍。
    """
    previous = [_finding("a" * 8), _finding("b" * 8)]
    incoming = [_finding("b" * 8, title="换个说法的同一条"), _finding("c" * 8)]

    fresh, already = partition_incoming(previous, incoming)

    assert [item.fingerprint for item in fresh] == ["c" * 8]
    assert [item.fingerprint for item in already] == ["b" * 8]


# ==========================================================================
# 摘要渲染
# ==========================================================================


def test_a_resurrected_finding_survives_the_round_trip_into_the_digest():
    """**从分诊到渲染整条链路**：忽略过 → 文件又变 → 重新出现在摘要里。

    拆开看，两半各自都对（`classify` 判得出 `needs_recheck`，`build_baseline_digest`
    也渲染得出这个分组），但拼起来曾经是错的：渲染那一步缺省 `changed_paths` 又判了
    一遍状态，把刚复活的结论判回 `suppressed`，从摘要里抹掉 —— 而且报告里只是少一条，
    没有任何提示。所以这条必须**跨两个函数**断言，不能只测一半。
    """
    previous = classify(
        [_finding("a" * 8, path="config/[30]道具表_CfgItem.xlsx", disposition=DISPOSITION_IGNORED)],
        changed_paths=["config/[30]道具表_CfgItem.xlsx"],
    )
    assert suppressed_fingerprints(previous) == frozenset(), "前提：它已经不再被抑制"

    text = build_baseline_digest(previous)

    assert "需要重新确认" in text
    assert f"#{'a' * 8}" in text, "复活了的结论没进摘要"
    assert "【吸灵器】窗帘拖拽第二段不出现" in text


def test_the_digest_orders_groups_by_urgency_not_by_input_order():
    """分组顺序固定：需要重新确认 → 仍待处理 → 已忽略。

    顺序飘忽会让「上一轮和这一轮差在哪」变成不可读的，而这段文本每轮都在提示词里。
    """
    findings = classify(
        [
            _finding("z" * 8, disposition=DISPOSITION_IGNORED, title="已忽略的"),
            _finding("y" * 8, title="待处理的"),
            _finding("x" * 8, path="src/b.lua", title="要重确认的"),
        ],
        changed_paths=["src/b.lua"],
    )

    text = build_baseline_digest(findings)

    assert text.index("需要重新确认") < text.index("仍待处理") < text.index("已忽略")


def test_the_digest_is_deterministic():
    findings = classify([_finding("a" * 8), _finding("b" * 8, path="src/b.lua")])
    assert build_baseline_digest(findings) == build_baseline_digest(findings)


def test_the_header_tells_the_model_not_to_re_report():
    """「不要重复报」这条必须写在提示词里，而不只存在于代码注释里。

    模型看不到它，就会把上一轮的七八条再报一遍 —— 基线反而让报告更长。
    """
    text = build_baseline_digest(classify([_finding("a" * 8)]))
    assert "不要当作新发现重复报" in text
    assert "仍成立 / 已修复 / 已被推翻" in text


def test_an_empty_baseline_states_that_this_is_the_first_run():
    text = build_baseline_digest([])
    assert "第一次分析" in text


def test_omission_is_accounted_for_and_keeps_the_urgent_ones():
    """超长时先丢最不要紧的，并**如实写出省略了多少**。

    静默截断会让模型以为自己看到了全部历史结论 —— 和 `budget.py` 里同一条原则。
    """
    findings = classify(
        [_finding(f"{index:08d}", path=f"src/f{index}.lua", title=f"【模块】第 {index} 条问题") for index in range(60)]
        + [
            _finding(
                "9" * 8,
                path="config/[30]道具表_CfgItem.xlsx",
                disposition=DISPOSITION_IGNORED,
                title="【道具】ID 被删除",
            )
        ],
        changed_paths=["config/[30]道具表_CfgItem.xlsx"],
    )

    text = build_baseline_digest(findings, max_chars=1_200)

    assert len(text) <= 1_200 + 400, "省略说明本身也要算进去，不能只管条目"
    assert "没有列出" in text, "省略了内容却没有说明"
    assert "【道具】ID 被删除" in text, "需要重新确认的那条被丢掉了 —— 它才是本轮最该看的"


def test_a_tiny_budget_still_produces_a_readable_digest():
    """预算小到装不下任何条目时，也要给出说明而不是半截文本。"""
    findings = classify([_finding(f"{index:08d}", title="标题" * 40) for index in range(20)])
    text = build_baseline_digest(findings, max_chars=200)
    assert "没有列出" in text
    assert text.startswith("# 这个版本截至上次分析已经报过的问题")


def test_the_default_budget_is_small_enough_to_share_the_prompt():
    """基线摘要与上下文条目抢同一份 prompt 预算，不能放任它长大。"""
    assert DEFAULT_BASELINE_CHARS <= 10_000


# ==========================================================================
# 变更单元
# ==========================================================================


def test_a_table_and_its_generated_file_form_one_unit():
    bundles = build_bundles(
        ["config/[30]道具表_CfgItem.xlsx", "build/lua/CfgItem.lua"]
    )

    assert len(bundles) == 1
    assert bundles[0].kind == KIND_GENERATED_PAIR
    assert bundles[0].members == ("config/[30]道具表_CfgItem.xlsx", "build/lua/CfgItem.lua")


def test_similarly_named_modules_are_not_merged():
    """`CfgItem` 与 `CfgItemSub` 是两张不同的表，硬凑到一起会让模型看错对象。"""
    bundles = build_bundles(
        [
            "config/[30]道具表_CfgItem.xlsx",
            "build/lua/CfgItem.lua",
            "config/[30]道具表_CfgItemSub.xlsx",
            "build/lua/CfgItemSub.lua",
        ]
    )

    assert len(bundles) == 2
    assert {bundle.key for bundle in bundles} == {"CfgItem", "CfgItemSub"}
    assert all(bundle.kind == KIND_GENERATED_PAIR for bundle in bundles)


def test_a_path_without_a_token_is_its_own_unit():
    bundles = build_bundles(["src/lua/absorber.lua", "config/other.csv"])
    assert [bundle.kind for bundle in bundles] == [KIND_SINGLE, KIND_SINGLE]
    assert [bundle.members for bundle in bundles] == [("src/lua/absorber.lua",), ("config/other.csv",)]


def test_a_too_short_token_is_not_treated_as_a_module_name():
    """`Cfg.lua` 里的记号只有一个字符 —— 真当成模块名的话，所有 `Cfg*` 文件都会被并成一家。"""
    bundles = build_bundles(["Cfg.lua", "build/lua/CfgItem.lua"])
    assert len(bundles) == 2, "短记号把不相关的文件凑到了一起"


def test_a_lone_table_is_a_single_unit_until_its_product_shows_up():
    """只改了表、产物没跟着变 —— 这本身就是要让模型看见的信号（可能漏了导表）。"""
    bundles = build_bundles(["config/[30]道具表_CfgScene.xlsx"])
    assert len(bundles) == 1
    assert bundles[0].kind == KIND_SINGLE
    assert bundles[0].members == ("config/[30]道具表_CfgScene.xlsx",)


def test_unit_order_follows_first_appearance_and_is_stable():
    paths = [
        "src/lua/absorber.lua",
        "build/lua/CfgItem.lua",
        "config/[30]道具表_CfgItem.xlsx",
        "src/lua/reward.lua",
    ]
    first = build_bundles(paths)
    second = build_bundles(paths)

    assert first == second
    # absorber 在最前，CfgItem 组按「第一个成员（lua，位置 1）」排第二，reward 最后
    assert [bundle.key for bundle in first] == ["src/lua/absorber.lua", "CfgItem", "src/lua/reward.lua"]
    assert first[1].members == ("build/lua/CfgItem.lua", "config/[30]道具表_CfgItem.xlsx")


def test_duplicate_paths_collapse():
    bundles = build_bundles(["src/a.lua", "src/a.lua", "./src/a.lua"])
    assert len(bundles) == 1
    assert bundles[0].members == ("src/a.lua",)


def test_companions_lookup_returns_the_other_members():
    bundles = build_bundles(["config/[30]道具表_CfgItem.xlsx", "build/lua/CfgItem.lua"])

    assert companions_of(bundles, "config/[30]道具表_CfgItem.xlsx") == ("build/lua/CfgItem.lua",)
    assert companions_of(bundles, "build/lua/CfgItem.lua") == ("config/[30]道具表_CfgItem.xlsx",)
    # 不属于任何多成员单元的路径：返回空，调用方不需要特判
    assert companions_of(bundles, "src/other.lua") == ()


def test_companions_lookup_normalizes_the_requested_path():
    bundles = build_bundles(["config/[30]道具表_CfgItem.xlsx", "build/lua/CfgItem.lua"])
    assert companions_of(bundles, "./config/[30]道具表_CfgItem.xlsx") == ("build/lua/CfgItem.lua",)


def test_the_requested_member_can_be_put_first():
    """模型索要的是表，回来先看到生成物会让人以为拿错了。"""
    bundle = build_bundles(["config/[30]道具表_CfgItem.xlsx", "build/lua/CfgItem.lua"])[0]
    reordered = bundle.with_member_first("build/lua/CfgItem.lua")
    assert reordered.members[0] == "build/lua/CfgItem.lua"
    assert set(reordered.members) == set(bundle.members)


def test_describe_lists_only_multi_member_units():
    bundles = build_bundles(
        ["config/[30]道具表_CfgItem.xlsx", "build/lua/CfgItem.lua", "src/lone.lua"]
    )
    lines = describe_bundles(bundles)
    assert len(lines) == 1
    assert "CfgItem" in lines[0]
    assert "必须一起看" in lines[0]


def test_describe_reports_how_many_groups_it_left_out():
    paths = []
    for index in range(5):
        paths.extend([f"config/t_CfgM{index}.xlsx", f"build/CfgM{index}.lua"])
    lines = describe_bundles(build_bundles(paths), limit=2)
    assert len(lines) == 3
    assert "另有 3 组" in lines[-1]
