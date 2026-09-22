from benchmarks.ai_analysis.dataset import BenchmarkSample, GoldIssue, GoldLabel
from benchmarks.ai_analysis.metrics import evaluate_sample
from services.ai import dependency_resolver
from services.ai.budget_plan import build_budget_plan, derive_tool_limits
from services.ai.change_set import DEFAULT_MANIFEST_SHARDS, from_weekly_payload
from services.ai.context_tools import DEFAULT_TOOL_LIMITS
from services.ai.coverage_ledger import build_ledger
from services.ai.dependency_resolver import DependencyDocument, resolve_dependencies
from services.ai.engine import EngineLimits
from services.ai.manifest import ManifestPlan, build_manifest, render_manifest_reference
from services.ai.protocol import ContextRequest, parse_payload, sanitize_requests
from services.ai.reference_index import SnapshotReferenceIndex
from services.ai.subagent import apply_manifest, build_member_task, plan_family
from services.ai.windowed_view import split_segments

# 一个全 40 位的提交号（白名单里存完整号，`read_reference` 的标签里只有 12 位前缀）。
LATEST = "f844faa6f59c3f1b571226e92ac03dbd22a6b790"
OLDER = "322ece563731088a334a1d71870016078d76bbc8"


def test_gold_issue_keeps_qa_family_trigger_evidence_and_expected_disposition():
    issue = GoldIssue.from_dict(
        {
            "issue_id": "G-1",
            "title": "协议编号位移",
            "family": "protocol_compatibility",
            "severity": "critical",
            "file_paths": ["proto/a.lua"],
            "evidence": ["proto/a.lua:10", "proto/a.lua:20"],
            "trigger": "旧客户端发送编号 7",
            "expected_disposition": "active",
        }
    )

    payload = issue.to_dict()

    assert payload["family"] == "protocol_compatibility"
    assert payload["evidence"] == ["proto/a.lua:10", "proto/a.lua:20"]
    assert payload["trigger"] == "旧客户端发送编号 7"
    assert payload["expected_disposition"] == "active"


def test_benchmark_reports_family_and_critical_recall_and_token_efficiency():
    sample = BenchmarkSample(
        sample_id="run-x",
        run_row={"tokens_input": 900, "tokens_output": 100, "duration_ms": 2000},
        findings=[
            {
                "id": 1,
                "title": "协议编号位移",
                "severity": "critical",
                "file_path": "proto/a.lua",
                "evidence": "proto/a.lua:10",
            }
        ],
    )
    gold = GoldLabel(
        sample_id="run-x",
        change_files=["proto/a.lua", "config/reward.xlsx"],
        issues=[
            GoldIssue(
                "G-1",
                "协议编号位移",
                "critical",
                family="protocol_compatibility",
                file_paths=["proto/a.lua"],
                evidence=["proto/a.lua:10"],
                trigger="旧客户端发送编号 7",
                expected_disposition="active",
            ),
            GoldIssue(
                "G-2",
                "奖励重复发放",
                "high",
                family="economy",
                file_paths=["config/reward.xlsx"],
                evidence=["config/reward.xlsx:2"],
                trigger="重复领取",
                expected_disposition="active",
            ),
        ],
    )

    result = evaluate_sample(sample, gold)

    assert result["recall"]["critical"] == 1.0
    assert result["recall"]["by_family"]["protocol_compatibility"]["recall"] == 1.0
    assert result["recall"]["by_family"]["economy"]["recall"] == 0.0
    assert result["efficiency"]["tokens_per_matched_finding"] == 1000.0
    assert result["efficiency"]["tokens_per_locatable_evidence"] == 1000.0


def test_manifest_assigns_every_file_deterministically_and_pages_without_truncation():
    rows = [
        {
            "repository_id": 1,
            "repository_name": "config",
            "latest_commit_id": f"{index:040x}",
            "file_path": f"config/items/CfgItem{index}.xlsx",
            "source": "delta",
        }
        for index in range(1005)
    ]

    first = build_manifest(rows, shard_count=3)
    second = build_manifest(list(reversed(rows)), shard_count=3)

    assert len(first.entries) == 1005
    assert first.assignment_digest == second.assignment_digest
    assert all(entry.assigned_shards for entry in first.entries)
    assert first.assigned_rate == 1.0
    # 分页走的是**真的那条通路**（`render_manifest_reference` + `windowed_view` 按 `##`
    # 切节），不是另写一个只给测试用的分页器：第 N 节必须就是第 N 页，否则提示词里那句
    # 「`lines` 写页号」是错的，模型点名第 3 页会拿到第 2 页。
    segments, _note = split_segments(render_manifest_reference(first), kind="read_reference")
    assert len(segments) == 11
    for page, segment in enumerate(segments, start=1):
        assert segment.lstrip().startswith(f"## 第 {page} 页"), page
    assert "共 1005 个文件" in segments[0] and "分配指纹" in segments[0], (
        "总文件数与分配指纹要落在第 1 节里：它们自成一段的话，页号与节号就差了 1"
    )
    biggest = max(len(segment) for segment in segments)
    assert biggest <= DEFAULT_TOOL_LIMITS["read_reference"] - 1_000, (
        f"一页 {biggest} 字，加上抬头就装不下 read_reference 的额度了"
    )


def test_dependency_resolver_records_one_hop_edges_and_confidence():
    documents = [
        DependencyDocument(
            path="code/shop.lua",
            text='local item = require("config.CfgItem")\nlocal id = 10001',
        ),
        DependencyDocument(path="config/CfgItem.lua", text="return {[10001] = {price=5}}"),
        DependencyDocument(path="config/CfgItem.xlsx", text="10001,price,5"),
    ]

    result = resolve_dependencies(["code/shop.lua"], documents)

    targets = {edge.target for edge in result.edges}
    assert "config/CfgItem.lua" in targets
    assert "config/CfgItem.xlsx" in targets
    assert all(edge.source == "code/shop.lua" for edge in result.edges)
    assert all(edge.confidence in {"high", "medium", "low"} for edge in result.edges)


def test_dependency_resolver_pins_all_three_edge_kinds_on_one_fixture():
    """三种边各自的**判据与置信度**逐条钉死（性能改写不许把结果改掉）。

    这个模块**未接线**（见模块抬头），所以它的正确性没有任何生产路径在替你兜底 ——
    只有这一组断言。改里面的循环之前先看这里。
    """
    documents = [
        # 根 1：`require` 里点名了 `config.CfgItem` → 符号边（high）。
        DependencyDocument(path="code/shop.lua", text='local cfg = require("config.CfgItem")'),
        DependencyDocument(path="config/CfgItem.lua", text="return {}"),
        # 与根 1 **同 stem** 的生成物 → 名字对（generated_pair，medium）。
        DependencyDocument(path="build/lua/shop.lua", text="return {}"),
        # 根 2：只靠一个编号与目标关联 → 编号边（config_id，medium）。
        DependencyDocument(path="config/CfgShop.lua", text="local npc = 10001"),
        DependencyDocument(path="config/CfgPrice.lua", text="return { [10001] = 3 }"),
        # 什么都不沾：不许凭空连边。
        DependencyDocument(path="code/unrelated.lua", text="local x = 1"),
    ]

    result = resolve_dependencies(["code/shop.lua", "config/CfgShop.lua"], documents)

    triples = {(edge.source, edge.target, edge.kind, edge.confidence) for edge in result.edges}
    assert triples == {
        ("code/shop.lua", "config/CfgItem.lua", "symbol", "high"),
        ("code/shop.lua", "build/lua/shop.lua", "generated_pair", "medium"),
        ("config/CfgShop.lua", "config/CfgPrice.lua", "config_id", "medium"),
    }
    assert "code/unrelated.lua" not in {edge.source for edge in result.edges}
    assert "code/unrelated.lua" not in {edge.target for edge in result.edges}
    # 闭包 = 根 + 全部一跳目标，去重且保持首次出现的顺序。
    assert result.closure == (
        "code/shop.lua",
        "config/CfgShop.lua",
        "build/lua/shop.lua",
        "config/CfgItem.lua",
        "config/CfgPrice.lua",
    )


def test_dependency_resolver_scans_each_document_for_ids_once(monkeypatch):
    """编号扫描**按文档只做一次**，不许在「根 × 目标」的双层循环里重跑。

    改前：`_ID_RE.findall(target_text)` 写在目标循环体内，1000 份文档就是 1000×1000 次
    正则扫描 —— 这个模块要接线时第一个撞上的就是它。判据用**调用次数**，不看耗时
    （耗时测试在 CI 上只会变成偶发红）。
    """
    calls: list[str] = []
    real = dependency_resolver._ID_RE

    class Counting:
        pattern = real.pattern

        def findall(self, text):  # noqa: D102 —— 只数次数，行为原样转发
            calls.append(text)
            return real.findall(text)

    monkeypatch.setattr(dependency_resolver, "_ID_RE", Counting())
    documents = [
        DependencyDocument(path=f"config/T{index}.lua", text=f"local id = {10001 + index}")
        for index in range(6)
    ]

    resolve_dependencies(["config/T0.lua", "config/T1.lua", "config/T2.lua"], documents)

    assert len(calls) == len(documents), (
        f"编号扫描跑了 {len(calls)} 次，文档只有 {len(documents)} 份 —— "
        "它又回到按「根 × 目标」重算了"
    )


def test_scope_dependency_closure_adds_same_stem_generated_pair_only_once():
    from types import SimpleNamespace

    from services.ai.scope_sampling import _same_stem_dependency_entries

    root = SimpleNamespace(config_id=1, file_path="config/CfgItem.xlsx")
    generated = SimpleNamespace(config_id=1, file_path="build/lua/CfgItem.lua")
    unrelated = SimpleNamespace(config_id=1, file_path="build/lua/CfgMonster.lua")

    result = _same_stem_dependency_entries(
        [root, generated, unrelated], [root], already={(1, root.file_path)}, limit=10
    )

    assert result == [generated]


def test_weekly_change_set_carries_full_manifest_into_member_assignments():
    rows = [
        {
            "repository_id": 1,
            "repository_name": "repo",
            "latest_commit_id": f"{index + 1:040x}",
            "file_path": f"code/module_{index}.lua",
        }
        for index in range(9)
    ]
    change = from_weekly_payload({"delta_files": rows, "list_files": rows})
    plan = plan_family(
        mode="weekly",
        enabled=True,
        count=3,
        limits=EngineLimits(),
    )
    assert plan is not None

    assigned = apply_manifest(plan, change.manifest)

    assert change.manifest.summary()["assigned"] == 9
    assert set().union(*(set(member.assigned_paths) for member in assigned.members)) == {
        row["file_path"] for row in rows
    }
    assert "确定性文件分工" in build_member_task(assigned.members[0], assigned)
    allowed, dropped = sanitize_requests(
        [ContextRequest(type="read_reference", name="change-manifest", lines="2")],
        change.scope,
    )
    assert not dropped
    assert allowed[0].lines == "2"


def test_coverage_ledger_separates_assigned_inspected_and_evidence_layers():
    """三层各有各的分子：**分了片 / 碰过这个文件 / 看的是这一版**。

    `inspection_coverage` 的判据必须与 `_evidence_files` 同源（`failed` / `empty` 不算、
    白名单外的路径不算）：它此前另写了一遍统计，于是**「取数失败」被算成了「已检查」**
    （这一条断言原先固化的就是这个 bug）。
    """
    manifest = build_manifest(
        [
            {"latest_commit_id": LATEST, "file_path": "a.lua"},
            {"latest_commit_id": LATEST, "file_path": "b.lua"},
            {"latest_commit_id": LATEST, "file_path": "c.lua"},
        ],
        shard_count=2,
    )
    ledger = build_ledger(
        request_payload={
            "mode": "weekly",
            "delta_files": [
                {"latest_commit_id": LATEST, "file_path": "a.lua"},
                {"latest_commit_id": LATEST, "file_path": "b.lua"},
                {"latest_commit_id": LATEST, "file_path": "c.lua"},
            ],
            "manifest": manifest.to_dict(),
        },
        executed=[
            {"kind": "file_diff", "label": f"file_diff {LATEST[:12]} a.lua"},
            # 取不到：**不算已检查**（它没有任何内容进入分析）
            {
                "kind": "file_content",
                "label": f"file_content {LATEST[:12]} b.lua",
                "failed": True,
            },
            # 取的是更早那条提交的版本：**算「碰过这个文件」**（已检查），
            # 但**不算「看过这一版」**（保守口径的证据）。
            {"kind": "file_diff", "label": f"file_diff {OLDER[:12]} c.lua"},
            # 白名单外的路径：平台根本不会去读，漏进来也不算
            {"kind": "file_diff", "label": f"file_diff {LATEST[:12]} ../../etc/passwd"},
        ],
    )

    assert ledger["assignment_coverage"] == {"covered": 3, "total": 3, "ratio": 1.0}
    assert ledger["inspection_coverage"] == {"covered": 2, "total": 3, "ratio": 2 / 3}
    assert ledger["evidence_coverage"]["by_pair"]["covered"] == 1
    assert ledger["evidence_coverage"]["by_path"]["covered"] == 2


def test_the_report_rows_carry_the_assigned_and_inspected_layers():
    """三层覆盖率要**摆在报告里**，不能只算不显示（算了不显示 = 没人看得到）。"""
    manifest = build_manifest(
        [
            {"latest_commit_id": LATEST, "file_path": "a.lua"},
            {"latest_commit_id": LATEST, "file_path": "b.lua"},
            {"latest_commit_id": LATEST, "file_path": "c.lua"},
        ],
        shard_count=2,
    )
    ledger = build_ledger(
        request_payload={
            "mode": "weekly",
            "delta_files": [
                {"latest_commit_id": LATEST, "file_path": "a.lua"},
                {"latest_commit_id": LATEST, "file_path": "b.lua"},
                {"latest_commit_id": LATEST, "file_path": "c.lua"},
            ],
            "manifest": manifest.to_dict(),
        },
        executed=[{"kind": "file_diff", "label": f"file_diff {LATEST[:12]} a.lua"}],
    )

    rows = dict(ledger["rows"])
    assert "3 / 3" in rows["覆盖（已分配）"]
    assert "1 / 3" in rows["覆盖（已检查）"], "已检查那行必须与 inspection_coverage 同源"
    names = [name for name, _ in ledger["rows"]]
    assert names.index("覆盖（版本清单）") < names.index("覆盖（已分配）")
    assert names.index("覆盖（已分配）") < names.index("覆盖（已检查）")
    assert names.index("覆盖（已检查）") < names.index("覆盖（取到证据）")


def test_the_missing_evidence_gap_spells_out_the_assigned_and_inspected_counts():
    """缺口那句要说清「已分配 / 已检查」：只说「还有 N 个没看」看不出是分片没跑到、
    还是跑了没取到证据。"""
    manifest = build_manifest(
        [
            {"latest_commit_id": LATEST, "file_path": "a.lua"},
            {"latest_commit_id": LATEST, "file_path": "b.lua"},
        ],
        shard_count=2,
    )
    ledger = build_ledger(
        request_payload={
            "mode": "weekly",
            "delta_files": [
                {"latest_commit_id": LATEST, "file_path": "a.lua"},
                {"latest_commit_id": LATEST, "file_path": "b.lua"},
            ],
            "manifest": manifest.to_dict(),
        },
        executed=[{"kind": "file_diff", "label": f"file_diff {LATEST[:12]} a.lua"}],
    )

    gaps = "\n".join(ledger["gaps"])
    assert "没有取到证据" in gaps
    assert "已分配 2 / 已检查 1" in gaps


def test_zero_compensation_or_dependency_is_not_the_same_as_unrecorded():
    """「0 个补偿项」与「这份 payload 里没有这个概念」必须在报告里长得不一样。

    两者都写成「一行都不显示」时，读者分不清「这轮没有补偿」和「这轮平台没记」 ——
    与 `_count` 的 `None` 语义是同一条口径（`:113`）。
    """
    rows_of = lambda payload: dict(  # noqa: E731 —— 用例内的短读法
        build_ledger(request_payload=payload, executed=[], tool_stats={})["rows"]
    )

    zeroed = rows_of(
        {
            "mode": "weekly",
            "scope": "full",
            "summary": {"batch_files": 1, "window_files": 1, "compensation_files": 0, "dependency_files": 0},
            "delta_files": [{"latest_commit_id": LATEST, "file_path": "a.lua"}],
        }
    )
    assert "0 个" in zeroed["补偿输入"], zeroed["补偿输入"]
    assert "0 个" in zeroed["依赖输入"], zeroed["依赖输入"]

    unrecorded = rows_of(
        {
            "mode": "weekly",
            "scope": "full",
            "summary": {"batch_files": 1, "window_files": 1},
            "delta_files": [{"latest_commit_id": LATEST, "file_path": "a.lua"}],
        }
    )
    assert unrecorded["补偿输入"] == "未记录", unrecorded["补偿输入"]
    assert unrecorded["依赖输入"] == "未记录", unrecorded["依赖输入"]


def test_a_compensation_or_dependency_file_without_evidence_becomes_a_gap():
    """补偿/依赖核查项**没取到证据**时要单独成一条缺口。

    它们进输入的唯一理由就是「上一轮没看到，这一轮补上」/「改了它就要跟着看」——
    没取到证据等于这件事没做成，而原先的缺口里一个字都不提（`gap_notes` 通篇不讲）。
    """
    payload = {
        "mode": "weekly",
        "scope": "full",
        "summary": {
            "batch_files": 3,
            "window_files": 3,
            "compensation_files": 1,
            "dependency_files": 1,
        },
        "delta_files": [
            {"latest_commit_id": LATEST, "file_path": "config/本轮改的.xlsx"},
            {
                "latest_commit_id": LATEST,
                "file_path": "config/上轮未读的.xlsx",
                "source": "compensation",
            },
            {
                "latest_commit_id": LATEST,
                "file_path": "code/同名生成物.lua",
                "source": "dependency",
            },
        ],
    }
    ledger = build_ledger(
        request_payload=payload,
        executed=[{"kind": "file_diff", "label": f"file_diff {LATEST[:12]} config/本轮改的.xlsx"}],
        tool_stats={},
    )

    gaps = "\n".join(ledger["gaps"])
    assert "补偿" in gaps and "依赖" in gaps, gaps
    assert "config/上轮未读的.xlsx" in gaps and "code/同名生成物.lua" in gaps, gaps
    assert "config/本轮改的.xlsx" not in gaps, "取到证据的那条不是缺口"
    # 按来源分别计数，落进 counts 里（供报告与后续接线用）
    assert ledger["counts"]["pending_by_source"]["compensation"]["total"] == 1
    assert ledger["counts"]["pending_by_source"]["dependency"]["pending"] == 1

    # 全部取到证据时不留缺口（否则这句话会变成每次都有、没人再看）
    covered = build_ledger(
        request_payload=payload,
        executed=[
            {"kind": "file_diff", "label": f"file_diff {LATEST[:12]} config/本轮改的.xlsx"},
            {"kind": "file_diff", "label": f"file_diff {LATEST[:12]} config/上轮未读的.xlsx"},
            {"kind": "file_diff", "label": f"file_diff {LATEST[:12]} code/同名生成物.lua"},
        ],
        tool_stats={},
    )
    assert not any("补偿" in one for one in covered["gaps"]), covered["gaps"]


def test_the_persisted_manifest_round_trips_without_moving_its_fingerprint():
    """落库的 `payload["manifest"]` 必须能**原样还原**（含分配指纹与逐条分片标签）。

    提示词、`read_reference` 的 S 标签、成员归属四者要读同一份指纹，靠的就是这个还原：
    `from_dict(...).assignment_digest` 与落库那份不相等，模型看到的指纹就和账本对不上。
    """
    plan = build_manifest(
        [
            {
                "repository_id": 1,
                "repository_name": "config",
                "latest_commit_id": f"{index:040x}",
                "file_path": f"config/items/CfgItem{index}.xlsx",
                "source": "dependency" if index % 3 == 0 else "delta",
            }
            for index in range(37)
        ],
        shard_count=4,
    )

    restored = ManifestPlan.from_dict(plan.to_dict())

    assert restored == plan, "还原出来的计划与原来那份逐字段不等"
    assert restored.assignment_digest == plan.assignment_digest
    assert restored.shard_count == 4
    assert restored.summary() == plan.summary()
    assert [entry.to_dict() for entry in restored.entries] == [
        entry.to_dict() for entry in plan.entries
    ]

    # 坏数据（缺键 / 不是字典 / 空）不抛，也不编：空计划 + 最小可用的分片数（1，**不是 3**：
    # 退回 3 会让人以为这份清单真的分了 3 片）。
    empty = ManifestPlan.from_dict({"entries": [{"path": ""}, "坏行"]})
    assert empty.entries == () and empty.shard_count == 1
    assert ManifestPlan.from_dict(None) == ManifestPlan(
        (), 1, build_manifest([], shard_count=1).assignment_digest
    )
    # 分片数缺失时从 S 标签里认出来（不许悄悄按 3 算）
    assert ManifestPlan.from_dict({"entries": [{"path": "a.lua", "assigned_shards": ["S7"]}]}).shard_count == 7


def test_the_prompt_manifest_is_the_persisted_one_even_when_the_shard_count_is_not_three():
    """**同一份快照只能有一个 manifest、一个指纹。**

    prompt 那一份原先写死 3 个分片，落库那一份按配置的成员数（`subagent_count`）——
    两者不等时：模型读到 `read_reference` 里的 S 标签是 3 分片版的，而它实际分到的文件
    是另一套。默认 3 时两个数**巧合相等**，所以这个 bug 只在 `subagent_count != 3` 时现形。
    """
    rows = [
        {
            "repository_id": 1,
            "repository_name": "repo",
            "latest_commit_id": f"{index + 1:040x}",
            "file_path": f"code/module_{index}.lua",
        }
        for index in range(12)
    ]
    persisted = build_manifest(rows, shard_count=5).to_dict()
    payload = {"delta_files": rows, "list_files": rows, "manifest": persisted}

    change = from_weekly_payload(payload)

    assert change.manifest.shard_count == 5, "prompt 用的是落库那份，不是写死的 3"
    assert change.manifest.assignment_digest == persisted["assignment_digest"]
    assert persisted["assignment_digest"][:12] in change.summary, "提示词里的指纹不是落库那个"
    # 写死 3 分片算出来的那份指纹**不许**出现在提示词里
    three = build_manifest(rows, shard_count=3)
    assert three.assignment_digest != persisted["assignment_digest"]
    assert three.assignment_digest[:12] not in change.summary

    # `read_reference` 的 S 标签与成员归属读的是**同一个 plan**
    reference = render_manifest_reference(change.manifest)
    assert "[S5" in reference or ",S5" in reference, reference[:400]
    plan = plan_family(mode="weekly", enabled=True, count=5, limits=EngineLimits())
    assert plan is not None and plan.count == 5
    assigned = apply_manifest(plan, change.manifest)
    for member in assigned.members:
        assert set(member.assigned_paths) == {
            entry.path for entry in change.manifest.entries if member.label in entry.assigned_shards
        }, f"{member.label} 分到的文件与 manifest 的 S 标签对不上"
    assert set().union(*(set(member.assigned_paths) for member in assigned.members)) == {
        row["file_path"] for row in rows
    }


def test_the_summary_tells_the_model_how_to_page_the_full_manifest():
    """**工具存在但没人告诉模型它存在** —— 这条通路要写进提示词才算通。

    `read_reference` 读 `change-manifest` 能按页拿到完整清单（`render_manifest_reference`
    的分节 = 分页），而 `SKILL.md` 的可读文档清单里没有它：不写这一句，模型只能看见
    「列出来的那几个名字」。
    """
    rows = [
        {
            "repository_id": 1,
            "repository_name": "repo",
            "latest_commit_id": f"{index + 1:040x}",
            "file_path": f"code/module_{index}.lua",
        }
        for index in range(6)
    ]
    change = from_weekly_payload({"delta_files": rows, "list_files": rows[:2]})

    assert "change-manifest" in change.summary
    assert "read_reference" in change.summary
    assert "页号" in change.summary, "没说清 `lines` 该写什么"
    assert "每页 100 个文件" in change.summary


def test_the_assignment_sentence_does_not_talk_about_shards_in_a_single_agent_run():
    """没有分片时说「分片先检查自己的文件」是**错话**：单代理运行时根本没有分片，
    也没有那份任务书 —— 模型会去找一个不存在的东西，或者以为自己只该看一部分。"""
    rows = [
        {"repository_id": 1, "latest_commit_id": LATEST, "file_path": f"code/m_{index}.lua"}
        for index in range(4)
    ]

    single = from_weekly_payload(
        {
            "delta_files": rows,
            "list_files": rows,
            "manifest": build_manifest(rows, shard_count=1).to_dict(),
        }
    )
    assert "本次没有分片" in single.summary
    assert "先检查自己的文件" not in single.summary

    sharded = from_weekly_payload(
        {
            "delta_files": rows,
            "list_files": rows,
            "manifest": build_manifest(rows, shard_count=3).to_dict(),
        }
    )
    assert "分片" in sharded.summary
    assert "本次没有分片" not in sharded.summary
    # 条件句里那半句必须留着：`subagent_count>=2` 但子代理没开时，这句话仍要是真话
    assert "单代理运行时没有分片" in sharded.summary


def test_the_summary_lists_the_compensation_and_dependency_paths_with_their_source():
    """补偿/依赖核查项要**逐条**出现在提示词里（原先只有一句计数）。

    模型只看得到「本轮改了什么」，不知道哪几条不是本轮改动 —— 报告里就会把补偿项
    当成新变更，于是「这周改了什么」那本账错了。
    """
    payload = {
        "delta_files": [
            {"latest_commit_id": LATEST, "file_path": "config/本轮改的.xlsx"},
            {
                "latest_commit_id": LATEST,
                "file_path": "config/上轮未读的.xlsx",
                "source": "compensation",
            },
            {
                "latest_commit_id": LATEST,
                "file_path": "code/同名生成物.lua",
                "source": "dependency",
            },
        ],
        "list_files": [
            {"latest_commit_id": LATEST, "file_path": "config/本轮改的.xlsx"},
        ],
        "summary": {
            "batch_files": 3,
            "window_files": 3,
            "compensation_files": 1,
            "dependency_files": 1,
        },
    }

    change = from_weekly_payload(payload)

    assert "## 本轮输入中的补偿/依赖核查项" in change.summary
    section = change.summary.split("## 本轮输入中的补偿/依赖核查项", 1)[1]
    assert "`config/上轮未读的.xlsx`" in section
    assert "compensation" in section
    assert "`code/同名生成物.lua`" in section
    assert "dependency" in section
    assert "`config/本轮改的.xlsx`" not in section, "本轮真正的改动不属于这一节"
    # 平台**没有核实**这些文件之间是什么关系：不许把它写成结论
    assert "必须把它们与「本轮改了什么」分开写" in section
    # 与提示词里那句计数说明对得上（两处说同一件事）
    assert "1 个上轮未覆盖补偿项" in change.summary
    assert "1 个依赖核查项" in change.summary


def test_the_summary_says_zero_when_there_are_no_compensation_or_dependency_inputs():
    """「0 个补偿项」要写出来 —— 不写的话它与「平台没记这个概念」长得一模一样。"""
    payload = {
        "delta_files": [{"latest_commit_id": LATEST, "file_path": "config/本轮改的.xlsx"}],
        "list_files": [{"latest_commit_id": LATEST, "file_path": "config/本轮改的.xlsx"}],
        "summary": {
            "batch_files": 1,
            "window_files": 1,
            "compensation_files": 0,
            "dependency_files": 0,
        },
    }

    change = from_weekly_payload(payload)

    assert "0 个" in change.summary
    assert "没有补偿项" in change.summary and "依赖核查项" in change.summary, (
        "两个都是 0 时也要说一句「这轮就是新增改动本身」，否则与「平台没记这个概念」一样"
    )
    assert "## 本轮输入中的补偿/依赖核查项" not in change.summary, "一条都没有时不摆空小节"

    # 老 payload（没有这两个键）不许编出一个 0
    old = from_weekly_payload(
        {
            "delta_files": [{"latest_commit_id": LATEST, "file_path": "config/本轮改的.xlsx"}],
            "list_files": [{"latest_commit_id": LATEST, "file_path": "config/本轮改的.xlsx"}],
        }
    )
    assert "补偿" not in old.summary
    assert "依赖核查项" not in old.summary


def test_snapshot_reference_index_reads_each_blob_once_for_many_queries():
    calls = []
    entries = [(f"code/f{index}.lua", f"{index:040x}") for index in range(300)]

    def reader(path, commit):
        calls.append((path, commit))
        index = int(path.removeprefix("code/f").removesuffix(".lua"))
        return f"local Protocol{index} = true\nshared_symbol = {index}\n"

    # `max_files` **显式给**：这条测的是「多次查询只读一遍 blob」这个缓存不变量，与索引的
    # 预热门槛（`reference_index.MAX_INDEX_FILES`，调用方的成本策略）无关。不显式给的话，
    # 门槛一动（240 → 767）这条用例就跟着红，而它要守的东西一个字都没变。门槛本身的账
    # 由 E2 自己的用例钉着（`len(reads) == MAX_INDEX_FILES` + 抬头 `240/280`）。
    index = SnapshotReferenceIndex.build(
        entries, reader=reader, version="test-v1", max_files=len(entries)
    )
    first = index.search("Protocol299")
    for _ in range(14):
        repeated = index.search("shared_symbol")

    assert len(calls) == 300
    assert first.files_total == 300
    assert first.hits[0].path == "code/f299.lua"
    assert repeated.scanned == 300
    assert repeated.truncated_files is False
    assert repeated.index_version == "test-v1"


def test_protocol_keeps_candidate_disposition_table():
    payload = parse_payload(
        """{
          "status": "final",
          "report_markdown": "# 报告",
          "anomalies": [],
          "dimensions": [{"id": "config_id", "hit": false, "note": "未发现"}],
          "candidate_dispositions": [
            {"candidate_id": "S1-1", "status": "rejected", "reason": "与 S2-1 重复"},
            {"candidate_id": "S2-1", "status": "adopted", "reason": "证据更完整"}
          ]
        }""",
        dimension_ids=("config_id",),
    )

    assert [(item.candidate_id, item.status) for item in payload.candidate_dispositions] == [
        ("S1-1", "rejected"),
        ("S2-1", "adopted"),
    ]


def test_context_render_exposes_stable_evidence_id():
    from services.ai.context_tools import ContextTools
    from services.ai.prompt import render_context_items

    class Provider:
        def file_diff(self, commit, path):
            return "@@ -1 +1 @@\n-old\n+new"

    request = ContextRequest(type="file_diff", commit="a" * 40, path="a.lua")
    item = ContextTools(Provider()).execute([request]).items[0]

    rendered = render_context_items([item])
    assert item.meta["evidence_id"] == item.meta["chunk_id"]
    assert f"evidence_id={item.meta['evidence_id']}" in rendered


def test_budget_plan_exposes_configured_effective_and_job_wide_limits():
    tool_limits = derive_tool_limits(prompt_char_budget=560_000, max_tool_requests=40)
    plan = build_budget_plan(
        configured_prompt_chars=2_000_000,
        effective_prompt_chars=560_000,
        platform_chars=20_000,
        max_rounds=8,
        max_tool_requests=40,
        shard_count=3,
        verify=True,
        tool_limits=tool_limits,
        window_note="端点未声明窗口，按默认值压到 600,000 字",
    )

    assert plan["prompt_chars"]["configured"] == 2_000_000
    assert plan["prompt_chars"]["effective"] == 560_000
    assert plan["roles"]["count"] == 5
    assert plan["job_theoretical_max"]["rounds"] == 40
    assert plan["job_theoretical_max"]["tool_requests"] == 200
    assert 11_000 <= plan["tool_limits"]["file_diff"] <= 32_000


def test_engine_usage_keeps_finish_reason_for_output_truncation_audit():
    from services.ai.engine import _usage_of
    from services.ai.llm_client import ChatResult

    usage = _usage_of(ChatResult(text="{}", finish_reason="length"))

    assert usage["finish_reason"] == "length"


def test_evidence_store_keeps_one_body_and_supports_id_lookup():
    from services.ai.budget import ContextItem
    from services.ai.evidence_store import EvidenceStore

    item = ContextItem(
        kind="file_diff", label="a.lua", text="+ changed", meta={"evidence_id": "ev-1"}
    )
    store = EvidenceStore()
    store[("file_diff", "a")] = item
    store[("file_diff", "alias")] = item

    assert store.by_id("ev-1") is item
    assert store.summary() == {
        "request_keys": 2,
        "unique_evidence": 1,
        "stored_chars": len(item.text),
    }
