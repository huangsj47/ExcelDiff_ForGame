# -*- coding: utf-8 -*-
"""检查维度清单是**项目可声明**的，而且只有一份。

## 这一组守的三件事

1. **默认逐字不变。** 项目没声明时，行为必须与「九个维度写死在平台里」那版**完全相同**
   —— 进提示词的那份正文与磁盘上的 SKILL.md 一个字节不差，提示词里不会多出任何一段。
   这一条是硬要求：它在 4595 条既有用例的眼皮底下，任何多出来的字节都要能解释。
2. **声明了自己清单的项目，模型看到的与平台用的是同一份。** 提示词末尾那段
   「本项目适用的维度清单」、子代理的分工与任务书里的条数、报告里的「未归类」判定，
   读的都是 `LoadedSkills.dimensions` —— 三处各留一份常量正是「校验按 A、提示词按 B」
   的来源，而那种错**不会报错**。
3. **坏掉的声明不静默。** 与 `project_facts` 的其余两组事实同一条纪律：读不出来、
   形状不对、`none`、id 重复 —— 一律回落平台默认，**并且带一句 warning**（还会写日志）。
   悄悄退化成「项目没声明」的话，「我明明声明了」会变成只有手工查磁盘才查得出来的问题。
4. **三处接线读的都是同一份清单**（第五节）：单代理路径的报告正文、随结果落库的那一份、
   以及第一轮的开场指令。前两处以前各按**平台出厂值**做事：单代理的报告正文里根本没有
   「未归类」那一节，导出文档把项目自有的维度显示成「未归类（performance）」——
   而这两处都不会报错，只是悄悄说错。

## 为什么这个通道放在知识包里

见 `services/ai/project_facts.py` 的模块 docstring：项目事实的落点已经是
`skills/projects/<slug>/references/project-facts.md`（平台读写入口、契约校验都在），
再开一条通道等于让管理员在两个地方填同一件事。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from services.ai.engine import EngineLimits, run_analysis
from services.ai.llm_client import ChatResult
from services.ai.project_facts import KEY_DIMENSIONS, dimensions
from services.ai.prompt import build_system_prompt, build_user_message
from services.ai.protocol import parse_payload
from services.ai.report_document import (
    UNCLASSIFIED_LABEL,
    build_report_markdown,
    dimension_label,
    dimension_labels_from_payload,
)
from services.ai.result_payload import result_payload
from services.ai.scope import AnalysisScope
from services.ai.skill_contract import (
    DEFAULT_DIMENSION_SPECS,
    DIMENSION_IDS,
    MAX_DECLARED_DIMENSIONS,
    build_dimensions,
    dimension_ids_of,
    render_dimension_section,
)
from services.ai.skill_contract import (
    UNCLASSIFIED_LABEL as CONTRACT_UNCLASSIFIED_LABEL,
)
from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV, load_skills
from services.ai.subagent import (
    apply_dimensions,
    build_member_task,
    build_synthesis_task,
    build_unclassified_section,
    plan_family,
    run_family_with_seed,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_SKILL = REPO_ROOT / "skills" / "version-diff-review" / "SKILL.md"

PROJECT_CODE = "P_DIM"
# 一份**不是**平台默认的清单（非配表项目的样子），顺序有意：相邻即相关。
DECLARED = "performance=性能与耗时, protocol=协议兼容性, resource=资源引用丢失"
DECLARED_IDS = ("performance", "protocol", "resource")

LIMITS = EngineLimits(max_rounds=8, max_tool_requests=20)

# 追加那一节的标题。**判据必须落在标题上，不能落在「本项目适用的维度清单」这个词组上**：
# SKILL.md 正文自己会提到那个词组（它要告诉模型去看那一节），拿词组当判据必然假失败。
SECTION_HEADING = "## 本项目适用的维度清单"


def _write_pack(projects_root: Path, slug: str, frontmatter: str) -> None:
    pack = projects_root / slug / "references"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "project-facts.md").write_text(
        f"---\nname: {slug}\ndescription: {slug} 的项目事实\n{frontmatter}\n---\n\n正文。\n",
        encoding="utf-8",
    )


def _load(tmp_path, monkeypatch, *, project_code: str = PROJECT_CODE, frontmatter: str = ""):
    """把项目根指向临时目录并加载 skill（走的正是生产入口 `load_skills`）。"""
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
    slug = project_code.strip().lower()
    _write_pack(tmp_path, slug, f"generated_prefixes: Cfg\n{frontmatter}".strip())
    return load_skills(REPO_ROOT, project_code=project_code)


# ==========================================================================
# 一、默认：一个字节都不变
# ==========================================================================


class TestWithoutADeclarationNothingChanges:
    def test_the_prompt_text_is_the_file_itself(self, tmp_path, monkeypatch):
        """没声明 `dimensions` 时，进提示词的那份正文就是磁盘上那份 SKILL.md。

        **这是硬要求**：`LoadedSkills.platform_skill.text` 会被原样拼进系统提示词，
        任何多出来的字节都会改变每一次分析的输入（而幂等键也跟着变）。
        """
        loaded = _load(tmp_path, monkeypatch)

        assert loaded.platform_skill.text == PLATFORM_SKILL.read_text(encoding="utf-8")
        assert loaded.platform_skill.content_hash == loaded.platform_skill.content_hash

    def test_the_effective_list_is_the_platform_default(self, tmp_path, monkeypatch):
        loaded = _load(tmp_path, monkeypatch)

        assert loaded.dimensions == DEFAULT_DIMENSION_SPECS
        assert tuple(spec.id for spec in loaded.dimensions) == DIMENSION_IDS
        assert loaded.dimension_warning == ""

    def test_the_system_prompt_gains_no_section(self, tmp_path, monkeypatch):
        loaded = _load(tmp_path, monkeypatch)
        system = build_system_prompt(loaded)

        assert SECTION_HEADING not in system, "没声明就不该多出这一段"
        assert PLATFORM_SKILL.read_text(encoding="utf-8") in system

    def test_declaring_the_default_list_explicitly_changes_nothing_either(
        self, tmp_path, monkeypatch
    ):
        """把出厂那九个**原样**声明一遍也是允许的，且同样不改动提示词。

        追加那一节的目的只是「覆盖正文里那九个」，清单与出厂值相同时覆盖是空操作 ——
        白花提示词预算，还会让两份内容看起来像是有分歧。
        """
        declared = ", ".join(f"{spec.id}={spec.label}" for spec in DEFAULT_DIMENSION_SPECS)
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {declared}")

        assert tuple(spec.id for spec in loaded.dimensions) == DIMENSION_IDS
        assert loaded.platform_skill.text == PLATFORM_SKILL.read_text(encoding="utf-8")
        assert SECTION_HEADING not in build_system_prompt(loaded)

    def test_a_project_without_a_knowledge_pack_is_the_declaration_free_case(self):
        """项目代号没有知识包（绝大多数项目的现状）→ 同样走默认。"""
        loaded = load_skills(REPO_ROOT, project_code="NO_SUCH_PROJECT_ZZZ")

        assert loaded.dimensions == DEFAULT_DIMENSION_SPECS
        assert loaded.dimension_warning == ""

    def test_the_revision_moves_when_the_list_changes(self, tmp_path, monkeypatch):
        """清单变了 → 提示词变了 → `revision` 变 → 「旧结论作废」是自动的。

        与「改了 SKILL.md 就重跑」同一条机制：不依赖任何人记得手工改版本号。
        这里比的是**同一次加载里的两个项目**（同一个项目根、只差一个声明），
        所以版本差异只可能来自那份清单。
        """
        declared = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")
        plain = _load(tmp_path, monkeypatch, project_code="P_PLAIN")

        assert declared.revision != plain.revision
        assert declared.platform_skill.text != plain.platform_skill.text


# ==========================================================================
# 二、声明之后：提示词与运行期读的是**同一份**
# ==========================================================================


class TestTheDeclaredListIsTheOnlyOne:
    def test_it_is_read_in_order_with_labels(self, tmp_path, monkeypatch):
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")

        assert tuple(spec.id for spec in loaded.dimensions) == DECLARED_IDS
        assert loaded.dimensions[0].label == "性能与耗时"
        assert loaded.dimension_warning == ""
        assert dimensions(PROJECT_CODE, repo_root=REPO_ROOT) is not None

    def test_the_prompt_section_lists_exactly_the_effective_ids(self, tmp_path, monkeypatch):
        """**这是「不允许校验按 A、提示词按 B」的落点。**

        提示词里那段清单必须逐条等于 `LoadedSkills.dimensions`，因为分工、任务书条数、
        报告里的「未归类」判定读的都是后者。少一条多一条，两边就分叉了。
        """
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")
        system = build_system_prompt(loaded)

        section = render_dimension_section(loaded.dimensions)
        assert section in system, "声明之后提示词里必须有那一节"

        listed = [
            line.split("`")[1]
            for line in section.splitlines()
            if line[:1].isdigit() and "`" in line
        ]
        assert tuple(listed) == DECLARED_IDS
        assert tuple(listed) == tuple(spec.id for spec in loaded.dimensions)

    def test_the_production_entry_point_replans_from_the_loaded_list(
        self, tmp_path, monkeypatch
    ):
        """**生产路径**：`plan_family`（它拿不到 skill）按默认清单算完，`run_family_with_seed`
        在开跑前按**加载出来的**清单重算分工。

        这一条刻意走**生产入口**（`run_family_with_seed`，`ai_analysis_service` 调的就是它），
        而不只是调 `apply_dimensions`：只测那个函数的话，接线掉了我这边照样绿 ——
        而那正是「模型看到的清单是 A、平台分工按 B」的入口。
        """
        from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome
        from services.ai.scope import AnalysisScope
        from services.ai.subagent import run_family_with_seed

        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")
        # 调用方（ai_analysis_service）手里没有 `LoadedSkills`，所以它按默认清单规划。
        plan = plan_family(mode="weekly", enabled=True, count=3, limits=LIMITS)
        assert plan is not None

        tasks: list[str] = []

        def fake_run(**kwargs):
            tasks.append(kwargs["task_message"])
            return EngineOutcome(status=STATUS_SUCCEEDED, report_markdown="# 变更理解\nx")

        run_family_with_seed(
            plan=plan,
            client=None,
            provider=None,
            loaded=loaded,
            scope=AnalysisScope(),
            change_summary="本次变更共 1 个提交、1 个文件。",
            run_analysis_fn=fake_run,
        )

        assert tasks, "一个成员都没跑起来"
        # 三个分片 + 汇总，每个成员手里的维度都必须是**声明的那一份**。
        for task in tasks:
            assert "config_id" not in task, f"分工里还留着平台默认的维度：{task[:200]}"
        assert "performance" in tasks[0]

    def test_the_split_and_the_task_book_use_the_same_list(self, tmp_path, monkeypatch):
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")
        plan = plan_family(
            mode="weekly",
            enabled=True,
            count=3,
            limits=LIMITS,
            dimensions=[spec.id for spec in loaded.dimensions],
        )
        assert plan is not None

        plan = apply_dimensions(plan, [spec.id for spec in loaded.dimensions])
        assert plan.synthesis.dimensions == DECLARED_IDS
        assert [name for member in plan.members for name in member.dimensions] == list(
            DECLARED_IDS
        )
        task = build_member_task(plan.members[0], plan)
        assert f"本次共 {len(DECLARED_IDS)} 个" in task
        assert f"{len(DECLARED_IDS)} 个维度一个都不能空着" in build_synthesis_task(plan, ())

    def test_the_report_groups_by_the_same_list(self, tmp_path, monkeypatch):
        """落不进**声明的那份**清单的条目进「未归类」；声明过的 id **不进**。

        这一条同时钉住两个方向：既不能把清单外的条目弄丢，也不能把清单内的条目误判成
        未归类（那会让项目自己声明的维度在报告里看起来像没人认领）。
        """
        anomalies = [
            {"title": "已声明的条目", "category": "performance", "evidence": ["x"]},
            {"title": "未声明的条目", "category": "something_else", "evidence": ["y"]},
        ]
        section = build_unclassified_section(_as_anomalies(anomalies), DECLARED_IDS)

        assert "未声明的条目" in section
        assert "已声明的条目" not in section
        assert "`performance`" in section, "那一节要写出本次生效的清单，读的人才对得上"

    def test_the_export_document_marks_unknown_categories_as_unclassified(self):
        """导出文档里认不出来的 category 显示成「未归类（<原始 id>）」并加一句说明。

        只显示原始 id 会被读成一个正常的维度名。这里是**没有清单可查**时的形态
        （`build_report_markdown` 不传 `dimension_labels`）：回落到平台出厂那一份，
        而那份清单认得出来的 id 照样会显示成中文名。
        """
        assert dimension_label("performance").startswith(UNCLASSIFIED_LABEL)
        assert "performance" in dimension_label("performance")
        assert CONTRACT_UNCLASSIFIED_LABEL == UNCLASSIFIED_LABEL

        markdown = build_report_markdown(
            project_label="项目",
            target_label="提交 abc",
            report_text="# 变更理解\n正文",
            anomalies=[{"title": "性能回归", "category": "performance", "evidence": ["e"]}],
        )
        assert f"{UNCLASSIFIED_LABEL}（performance）" in markdown
        assert "**平台没有丢弃它们**" in markdown, "必须说明这些条目没有被丢掉"

    def test_the_default_list_is_not_marked_unclassified(self):
        markdown = build_report_markdown(
            project_label="项目",
            target_label="提交 abc",
            report_text="# 变更理解\n正文",
            anomalies=[{"title": "正常的", "category": "code_logic", "evidence": ["e"]}],
        )
        assert UNCLASSIFIED_LABEL not in markdown


def _as_anomalies(raw: list[dict]):
    """把几条最小 JSON 走一遍**生产解析路径**，拿到真正的 `Anomaly`。

    刻意不用 `Anomaly(...)` 手工构造：那会绕过 `parse_payload` —— 而「清单外的 category
    不许被丢弃」这件事正是在那里发生的。
    """
    payload = {
        "status": "final",
        "report_markdown": "# 变更理解\n正文",
        "dimensions": [{"id": "process", "hit": False, "note": "没命中"}],
        "anomalies": [
            {
                "title": item["title"],
                "category": item["category"],
                "severity": "high",
                "confidence": "high",
                "evidence": item["evidence"],
            }
            for item in raw
        ],
    }
    return parse_payload(json.dumps(payload, ensure_ascii=False)).anomalies


# ==========================================================================
# 三、一份发现都不许消失
# ==========================================================================


class TestNoFindingIsLost:
    def test_a_category_outside_the_effective_list_survives_parsing(self):
        """清单外的 category：**保留**（原始值不改），并进「未归类」那一节。"""
        anomalies = _as_anomalies(
            [
                {"title": "已声明的条目", "category": "performance", "evidence": ["ev1"]},
                {"title": "未声明的条目", "category": "process", "evidence": ["ev2"]},
            ]
        )

        assert [item.title for item in anomalies] == ["已声明的条目", "未声明的条目"]
        assert anomalies[0].category == "performance", "原始 category 必须原样保留"
        assert anomalies[0].evidence == ("ev1",), "它自己的证据也要在"

        section = build_unclassified_section(anomalies, DECLARED_IDS)
        assert "已声明的条目" not in section, "performance 在声明清单里，不该进未归类"

        section = build_unclassified_section(anomalies, ("process",))
        assert "已声明的条目" in section and "未声明的条目" not in section

    def test_an_anomaly_without_a_category_is_listed_as_unclassified(self):
        """连 category 都没写也不丢：它没有归属，所以进未归类，并标明「未标注」。"""
        payload = parse_payload(
            json.dumps(
                {
                    "status": "final",
                    "report_markdown": "# 变更理解\n正文",
                    "dimensions": [{"id": "process", "hit": False, "note": ""}],
                    "anomalies": [
                        {
                            "title": "没写 category",
                            "severity": "high",
                            "confidence": "high",
                            "evidence": ["ev"],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

        assert len(payload.anomalies) == 1
        assert any(item.kind == "unclassified" for item in payload.dropped)
        section = build_unclassified_section(payload.anomalies, ("process",))
        assert "（未标注）" in section


class TestTheUnclassifiedSectionReachesTheReport:
    """「未归类」必须在**报告正文**里，不是一个只有 trace 才看得到的记录。

    换成非配表项目之后，真实的发现很容易落不进清单里的任何 id。若它们只是被「保留在
    异常清单里」，读报告的人看到的是一份**看起来完全正常**的报告 —— 少的那一条没有
    任何人会发现。这是这条链路里最该避免的失真形态，所以它有一条端到端用例。
    """

    def _outcome(self):
        from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome
        from services.ai.protocol import Anomaly

        return EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 变更理解\n正文",
            anomalies=(
                Anomaly(
                    title="性能回归",
                    category="performance",
                    severity="high",
                    confidence="high",
                    evidence=("耗时从 12ms 涨到 400ms",),
                ),
                Anomaly(
                    title="正常的流程问题",
                    category="process",
                    severity="high",
                    confidence="high",
                    evidence=("e2",),
                ),
            ),
        )

    def test_the_section_is_appended_for_items_outside_the_effective_list(self):
        from services.ai.subagent import aggregate_outcomes

        merged = aggregate_outcomes(synthesis=self._outcome(), steps=(), dimensions=("process",))

        assert "## 未归类" in merged.report_markdown
        assert "性能回归" in merged.report_markdown
        assert "正常的流程问题" not in merged.report_markdown.split("## 未归类")[1]
        assert merged.anomalies == self._outcome().anomalies, "异常清单本身一条都不许少"
        # 报告原文逐字在前，平台补的那一节在后。
        assert merged.report_markdown.startswith("# 变更理解\n正文")

    def test_nothing_is_appended_when_every_item_is_in_the_list(self):
        """全部归得上时不追加任何东西 —— 默认行为逐字不变。"""
        from services.ai.engine import STATUS_SUCCEEDED, EngineOutcome
        from services.ai.protocol import Anomaly
        from services.ai.subagent import aggregate_outcomes

        synthesis = EngineOutcome(
            status=STATUS_SUCCEEDED,
            report_markdown="# 变更理解\n正文",
            anomalies=(
                Anomaly(
                    title="正常的流程问题",
                    category="process",
                    severity="high",
                    confidence="high",
                    evidence=("e",),
                ),
            ),
        )

        merged = aggregate_outcomes(synthesis=synthesis, steps=())

        assert "未归类" not in merged.report_markdown
        assert merged.report_markdown.startswith("# 变更理解")


# ==========================================================================
# 四、坏声明：回落默认，但必须说清
# ==========================================================================


class TestABrokenDeclarationIsNeverSilent:
    @pytest.mark.parametrize(
        ("label", "value"),
        [
            ("形状不对（缺中文名）", "performance"),
            ("id 形状不合法", "Performance=性能"),
            ("id 重复", "a=甲, a=乙"),
            ("中文名过长", "a=" + "很长" * 20),
        ],
    )
    def test_it_falls_back_to_the_default_with_a_warning(
        self, tmp_path, monkeypatch, label, value
    ):
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {value}")

        assert loaded.dimensions == DEFAULT_DIMENSION_SPECS, label
        assert loaded.dimension_warning, f"{label}：坏声明必须交出一句 warning"
        assert KEY_DIMENSIONS in loaded.dimension_warning

    def test_declaring_none_is_refused_for_this_key(self, tmp_path, monkeypatch):
        """`none` 对这一个键是非法值：一个空的检查维度清单自相矛盾。

        声明成「本项目没有检查维度」等于让模型报出的每一条异常都只能进「未归类」——
        平台按「未声明」处理（回落出厂那九个）并告警，而不是照字面给一个空清单。
        """
        loaded = _load(tmp_path, monkeypatch, frontmatter="dimensions: none")

        assert loaded.dimensions == DEFAULT_DIMENSION_SPECS
        assert "没有" in loaded.dimension_warning

    def test_the_other_keys_keep_working_alone(self, tmp_path, monkeypatch):
        """声明通道是**同一个文件**：`dimensions` 写坏了，其余两组事实不受影响。"""
        monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))
        _write_pack(
            tmp_path,
            "p_dim",
            "generated_prefixes: Mod\ndimensions: bogus",
        )

        from services.ai.project_facts import generated_prefixes

        assert generated_prefixes(PROJECT_CODE) .prefixes == ("Mod",)
        assert dimensions(PROJECT_CODE).dimensions == DEFAULT_DIMENSION_SPECS


class TestTheDeclarationSyntax:
    """`build_dimensions` 是这套写法的**唯一**解析与校验入口。"""

    def test_it_keeps_the_declared_order(self):
        specs, problem = build_dimensions(["b=乙", "a=甲", "c=丙"])

        assert problem == ""
        assert [spec.id for spec in specs] == ["b", "a", "c"], "顺序是一切（相邻即相关）"

    def test_it_accepts_underscores_and_digits(self):
        specs, problem = build_dimensions(["save_data_2=存档结构"])

        assert problem == "" and specs[0].id == "save_data_2"

    def test_it_refuses_a_label_without_a_separator(self):
        specs, problem = build_dimensions(["performance"])

        assert specs == () and "id=中文名" in problem

    def test_it_refuses_an_empty_declaration(self):
        specs, problem = build_dimensions([])

        assert specs == () and problem

    def test_it_refuses_too_many_dimensions(self):
        items = [f"d{index}=维度{index}" for index in range(MAX_DECLARED_DIMENSIONS + 1)]

        specs, problem = build_dimensions(items)

        assert specs == () and str(MAX_DECLARED_DIMENSIONS) in problem

    def test_the_default_specs_are_self_consistent(self):
        """出厂清单自己必须过得了这套校验（否则「声明成出厂那九个」会判非法）。"""
        items = [f"{spec.id}={spec.label}" for spec in DEFAULT_DIMENSION_SPECS]

        specs, problem = build_dimensions(items)

        assert problem == ""
        assert specs == DEFAULT_DIMENSION_SPECS


# ==========================================================================
# 五、三处接线：单代理路径的报告正文、随结果落库的清单、第一轮的开场指令
# ==========================================================================
#
# 这三处此前各按**平台出厂值**做事，而三处都不会报错：
#
# * 单代理路径（单提交分析、或周版本没开子代理）的报告正文里**没有**「未归类」那一节
#   —— 那一条发现仍在异常清单里，但报告读起来完全正常，少的那一条没人会发现；
# * 导出文档把项目自己声明的维度显示成「未归类（performance）」；
# * 第一轮的开场指令点名了两个「某个项目碰巧有的维度」，别的项目读到的是噪音。
#
# 走的是**生产入口**（`run_analysis` / `run_family_with_seed` / `load_skills`），
# 不是直接调那几个渲染函数：只测函数的话，接线掉了我这边照样绿 —— 而在这一组里，
# 「接线掉了」正是要防的那件事。

COMMIT = "c" * 40
FILE = "config/perf.lua"
# 模型报出的报告原文。**刻意不带结尾换行**：它会被解析层 `strip()`，而下面有一条
# 「全部归得上时报告正文逐字不变」的断言 —— 拿带换行的样子去比，比的就不是「平台有没有
# 动过它」而是「换行被吃掉了没有」。
REPORT = "# 变更理解\n\n正文"
# 落在声明清单内 / 外的两条发现（用标题区分：报告里那一节的正文同时列着清单本身，
# 拿 id 当判据会把「清单那一行」也算进去）。
TITLES = {"performance": "清单内的发现", "code_logic": "清单外的发现"}


class _ScriptedClient:
    """按脚本回答的假模型（形态与 `tests/test_ai_engine.py::ScriptedClient` 相同）。"""

    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages, *, temperature=None):
        self.calls.append([dict(item) for item in messages])
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return ChatResult(
            text=self._replies[index], model="fake", prompt_tokens=10, completion_tokens=5
        )


def _scope() -> AnalysisScope:
    return AnalysisScope.from_iterables(
        commits=(COMMIT,),
        paths_by_commit={COMMIT: (FILE,)},
        readable_references=(),
    )


def _final_reply(*, categories: tuple[str, ...], report: str = REPORT) -> str:
    return json.dumps(
        {
            "status": "final",
            "report_markdown": report,
            # 任何 id 都能过协议校验（条目级不判集合），这里给清单里的第一个。
            "dimensions": [{"id": DECLARED_IDS[0], "hit": False, "note": "未命中"}],
            "anomalies": [
                {
                    "title": TITLES[category],
                    "category": category,
                    "severity": "high",
                    "confidence": "high",
                    "evidence": ["e"],
                    "commit": COMMIT,
                    "file_path": FILE,
                }
                for category in categories
            ],
        },
        ensure_ascii=False,
    )


def _run_engine(loaded, *, categories=(), report=REPORT, **overrides):
    """跑一次**真的** `run_analysis`（第 1 轮就 final，所以取数口用不上）。"""
    kwargs = {
        "client": _ScriptedClient(_final_reply(categories=categories, report=report)),
        "provider": None,
        "loaded": loaded,
        "scope": _scope(),
        "change_summary": "本次变更共 1 个提交、1 个文件。\n",
    }
    kwargs.update(overrides)
    return run_analysis(**kwargs)


class TestTheSingleAgentPathAlsoGetsTheUnclassifiedSection:
    """单代理路径的报告正文里也要有「未归类」那一节（与子代理路径同一份渲染函数）。

    判据落在**标题**上（`## 未归类`），不落在「本项目适用的维度清单」这类词组上。
    """

    def test_an_out_of_list_finding_is_grouped_in_the_report_body(self, tmp_path, monkeypatch):
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")

        outcome = _run_engine(loaded, categories=("performance", "code_logic"))

        assert [item.title for item in outcome.anomalies] == ["清单内的发现", "清单外的发现"], (
            "两条都不许丢（这一节只是分组，不是过滤）"
        )
        assert "## 未归类" in outcome.report_markdown
        section = outcome.report_markdown.split("## 未归类", 1)[1]
        assert "清单外的发现" in section, "落不进本次清单的那一条必须出现在这一节里"
        assert "清单内的发现" not in section, (
            "在声明清单里的 `performance` 不该被归到「未归类」—— 那会让项目自己声明的"
            "维度在报告里看起来像没人认领"
        )
        assert outcome.report_markdown.startswith("# 变更理解"), "报告原文逐字在前"

    def test_nothing_is_appended_when_every_finding_is_in_the_list(self, tmp_path, monkeypatch):
        """全部归得上时**不追加任何东西** —— 不许多出一节空标题。"""
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")

        outcome = _run_engine(loaded, categories=("performance",))

        assert outcome.report_markdown == REPORT, "报告正文被改动了"
        assert "未归类" not in outcome.report_markdown

    def test_the_weekly_path_without_subagents_is_this_same_call(self, tmp_path, monkeypatch):
        """周版本**没开子代理**时走的就是这条单代理路径（`plan_family` 返回 `None`）。

        先把这件事断言出来，这条用例才有意义 —— 否则它只是把上一条重写了一遍。
        """
        assert plan_family(mode="weekly", enabled=False, count=3, limits=LIMITS) is None
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")

        outcome = _run_engine(loaded, categories=("code_logic",))

        assert "## 未归类" in outcome.report_markdown
        assert "清单外的发现" in outcome.report_markdown

    def test_a_family_member_does_not_append_it(self, tmp_path, monkeypatch):
        """分片代理手里那一份是**素材**（`seed_messages` 非空），最终报告由汇总产出。

        它在这里也追加一遍的话，汇总那一份会变成两节「未归类」。
        """
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")

        outcome = _run_engine(
            loaded,
            categories=("code_logic",),
            seed_messages=(
                {"role": "system", "content": "共享前缀"},
                {"role": "user", "content": "共享清单"},
            ),
            task_message="你是 S1。",
        )

        assert "未归类" not in outcome.report_markdown

    def test_the_family_report_has_exactly_one_such_section(self, tmp_path, monkeypatch):
        """端到端：**真引擎**跑一家子，最终报告里只有一节「未归类」。

        上面那条钉的是「分片不追加」，这条钉的是**结果**：两条路各追加一次的话，这里会
        出现两节。它同时证明汇总那一份用的是 `LoadedSkills.dimensions`（声明的那一份）。
        """
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")
        plan = plan_family(mode="weekly", enabled=True, count=3, limits=LIMITS)
        assert plan is not None

        outcome = run_family_with_seed(
            plan=plan,
            client=_ScriptedClient(_final_reply(categories=("performance", "code_logic"))),
            provider=None,
            loaded=loaded,
            scope=_scope(),
            change_summary="本次变更共 1 个提交、1 个文件。\n",
        )

        assert outcome.report_markdown.count("## 未归类") == 1, outcome.report_markdown
        assert "清单外的发现" in outcome.report_markdown


class TestTheEffectiveListIsStoredWithTheResult:
    """落库那一份结构：`response_payload.dimension_specs`（id + 中文名，按声明顺序）。

    **为什么要落库**：导出文档要把 `category` 翻成中文名，而导出发生在很久之后 ——
    那时项目可能已经改过声明。现查项目当前声明等于按新清单**改写历史**。
    """

    def test_the_outcome_carries_the_list(self, tmp_path, monkeypatch):
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")

        outcome = _run_engine(loaded, categories=("performance",))

        assert outcome.dimension_specs == loaded.dimensions
        assert tuple(spec.id for spec in outcome.dimension_specs) == DECLARED_IDS

    def test_the_result_payload_stores_it(self, tmp_path, monkeypatch):
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")

        outcome = _run_engine(loaded, categories=("performance",))
        stored = result_payload(outcome, {}, suppressed=frozenset())

        assert stored["dimension_specs"] == [
            {"id": "performance", "label": "性能与耗时"},
            {"id": "protocol", "label": "协议兼容性"},
            {"id": "resource", "label": "资源引用丢失"},
        ]

    def test_the_subagent_path_carries_it_too(self, tmp_path, monkeypatch):
        """子代理模式的落库那一份来自**汇总那一次**（`aggregate_outcomes` 里带出来）。"""
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")
        plan = plan_family(mode="weekly", enabled=True, count=3, limits=LIMITS)

        outcome = run_family_with_seed(
            plan=plan,
            client=_ScriptedClient(_final_reply(categories=("performance",))),
            provider=None,
            loaded=loaded,
            scope=_scope(),
            change_summary="本次变更共 1 个提交、1 个文件。\n",
        )

        assert outcome.dimension_specs == loaded.dimensions

    def test_the_export_reads_the_stored_list(self):
        """结果里存着清单 → 导出按**它**渲染，项目自有的维度不再显示成「未归类」。"""
        stored = {
            "dimension_specs": [
                {"id": "performance", "label": "性能与耗时"},
                {"id": "protocol", "label": "协议兼容性"},
            ]
        }
        labels = dimension_labels_from_payload(stored)

        markdown = build_report_markdown(
            project_label="项目",
            target_label="提交 abc",
            report_text="# 变更理解\n正文",
            anomalies=[
                {"title": "性能回归", "category": "performance", "evidence": ["e"]},
                {"title": "说不清的", "category": "code_logic", "evidence": ["e"]},
            ],
            dimension_labels=labels,
        )

        assert "性能与耗时" in markdown, "项目自己声明的维度必须显示成它声明的中文名"
        assert "未归类（performance）" not in markdown
        assert "未归类（code_logic）" in markdown, "真落不进清单的仍要响亮地标出来"

    def test_a_missing_or_broken_list_falls_back_to_the_factory_one(self):
        """字段缺失 / 形状坏掉时回落到出厂清单 —— 这是**兜底**，不是「兼容旧数据」。"""
        from services.ai import report_document as doc

        assert doc.dimension_labels_from_payload({}) == doc.DIMENSION_LABELS
        assert doc.dimension_labels_from_payload({"dimension_specs": "x"}) == doc.DIMENSION_LABELS
        assert doc.dimension_labels_from_payload(
            {"dimension_specs": [{"id": "", "label": ""}]}
        ) == doc.DIMENSION_LABELS
        assert doc.dimension_labels_from_payload(
            {"dimension_specs": [{"id": "performance", "label": "性能与耗时"}]}
        ) == {"performance": "性能与耗时"}


class TestTheProtocolLayerFollowsTheEffectiveList:
    """解析层与纠正提示拿到「本次生效的清单」之后各自该做什么。

    两件都**不影响任何一条发现的去留**（那一条口径由 `TestNoFindingIsLost` 钉着），
    所以这里断的是各自那一件小事：账里留一条 `unclassified` 记录（说明界面上那一格
    为什么写着「未归类」），以及纠正提示把本次清单的 id 点名出来。
    """

    def _reply(self) -> str:
        return json.dumps(
            {
                "status": "final",
                "report_markdown": "# 变更理解\n正文",
                "dimensions": [{"id": DECLARED_IDS[0], "hit": False, "note": ""}],
                "anomalies": [
                    {
                        "title": "性能回归",
                        "category": "performance",
                        "severity": "high",
                        "confidence": "high",
                        "evidence": ["e"],
                    }
                ],
            },
            ensure_ascii=False,
        )

    def test_the_unclassified_record_follows_the_effective_list(self):
        """同一个 category：出厂清单下要记账（它落不进那一份），项目清单下不该记。"""
        raw = self._reply()

        default = parse_payload(raw)
        declared = parse_payload(raw, dimension_ids=DECLARED_IDS)

        assert any(
            item.kind == "unclassified" and item.detail == "performance"
            for item in default.dropped
        ), "落不进清单的条目要留一条记录，否则「为什么显示成未归类」无从解释"
        assert not any(item.kind == "unclassified" for item in declared.dropped)
        assert declared.anomalies == default.anomalies, "清单不参与任何一条发现的去留"

    def test_the_correction_hint_names_the_effective_ids(self):
        from services.ai.protocol import ProtocolError, build_correction_hint

        hint = build_correction_hint(
            ProtocolError("dimensions 缺了"), dimension_ids=DECLARED_IDS
        )

        for identifier in DECLARED_IDS:
            assert f"`{identifier}`" in hint
        # **不许出现字面条数**：条数写死就会与清单漂移，而漂移不会报错
        # （`tests/test_ai_protocol_and_scope.py` 那条用例钉着同一件事）。
        assert not re.search(r"\d+\s*个维度", hint), hint
        assert "performance" not in build_correction_hint(ProtocolError("x")), (
            "清单与出厂那份相同时不该把 id 再抄一遍"
        )


class TestTheFirstRoundHintFollowsTheEffectiveList:
    """第一轮的开场指令不该把「某个项目碰巧有的两个维度」当作普适内容。

    判据（任务里给的）：一个声明了 `performance` / `protocol` / `resource` 的项目读到
    这一段时，**不应该觉得自己被要求去看「配表数值」**。
    """

    def test_a_project_without_them_does_not_read_the_config_steps(self, tmp_path, monkeypatch):
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")

        message = build_user_message(
            change_summary="本次变更共 1 个提交、1 个文件。\n",
            round_index=1,
            max_rounds=8,
            requests_remaining=12,
            dimension_ids=dimension_ids_of(loaded.dimensions),
        )

        assert "config_data" not in message
        assert "value_sanity" not in message
        assert "只改了一列文案" not in message
        assert "经济闭环" not in message, "「按配表数值看」那两步对这个项目是噪音"
        # 编号必须顺延（中间漏号会让人以为少了一步）。
        numbers = [int(n) for n in re.findall(r"^(\d+)\. \*\*", message, flags=re.M)]
        assert numbers == list(range(1, len(numbers) + 1)), numbers

    def test_the_factory_list_keeps_both_steps(self):
        """出货那一份逐字不变：清单里本来就有这两个维度，一个字节都不该少。"""
        from services.ai.prompt import _first_round_hint

        default = _first_round_hint()

        assert "config_data" in default and "value_sanity" in default
        assert "经济闭环" in default
        assert len(re.findall(r"^\d+\. \*\*", default, flags=re.M)) == 5

    def test_the_engine_passes_the_effective_list_into_the_first_round(
        self, tmp_path, monkeypatch
    ):
        """**接线**：引擎第 1 轮发给模型的那条消息里也不该有那两步。

        只测 `build_user_message` 的话，引擎忘了传 `dimension_ids` 这条链照样绿 ——
        而那正是「模型看到的清单是 A、开场指令按 B」的入口。
        """
        loaded = _load(tmp_path, monkeypatch, frontmatter=f"dimensions: {DECLARED}")
        client = _ScriptedClient(_final_reply(categories=("performance",)))

        _run_engine(loaded, categories=("performance",), client=client)

        first_user = next(
            item["content"] for item in client.calls[0] if item["role"] == "user"
        )
        assert "config_data" not in first_user
        assert "value_sanity" not in first_user
        assert "先分诊" in first_user, "开场指令本身还得在"
