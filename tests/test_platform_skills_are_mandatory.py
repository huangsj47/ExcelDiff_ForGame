#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「所有项目都必须加载的底层 skill」这条机制本身。

## 它在解决什么

用户 2026-09-23 要的是：把一份资产发放安全审查的 skill 变成**每个项目都默认加载**的
底层内容，形态与平台既有的那份一致（`SKILL.md` + `references/`）。

平台原先只支持**一个**硬编码目录（`PLATFORM_SKILL_RELATIVE_PATH = "skills/version-diff-review"`），
所以这条需求要动加载器本身。这一组钉的就是那次改动的三条承诺：

1. `skills/` 下**每个**子目录（`projects/` 除外）都是一份平台 skill，全部无条件进提示词；
2. 顺序是**确定的**，且承载报告契约的那一份在最前 —— 提示词靠前缀命中缓存，
   顺序随文件系统返回顺序变的话，同样的内容每次都要从零付全价；
3. 目录在而 `SKILL.md` 不在是**部署不完整**，不是「这个项目没配」—— 静默跳过等于
   一份底层 skill 悄悄没生效，而它看起来与正常加载一模一样。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.ai import skill_contract as contract
from services.ai.prompt import build_system_prompt
from services.ai.skill_contract import (
    BODY_CONTRACT_SKILL_NAME,
    iter_platform_skill_dirs,
    validate_all,
)
from services.ai.skill_loader import SkillLoadError, load_skills

REPO_ROOT = Path(__file__).resolve().parents[1]

CONTRACT_SKILL = """---
name: {name}
description: 平台内置的报告契约载体，测试用最小实现。
---

# 版本变更评审

正文。
"""

PLAIN_SKILL = """---
name: {name}
description: {description}
---

# {title}

正文。
"""


def _write_skill(root: Path, name: str, body: str, *, references: dict[str, str] | None = None) -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    for file_name, text in (references or {}).items():
        ref_dir = skill_dir / "references"
        ref_dir.mkdir(exist_ok=True)
        (ref_dir / file_name).write_text(text, encoding="utf-8")
    return skill_dir


def _mini_repo(tmp_path: Path, **extra_skills) -> Path:
    """一个最小可加载的平台目录：必须有承载契约的那一份，否则加载直接失败。"""
    _write_skill(tmp_path, BODY_CONTRACT_SKILL_NAME,
                 CONTRACT_SKILL.format(name=BODY_CONTRACT_SKILL_NAME))
    for name, spec in extra_skills.items():
        _write_skill(tmp_path, name, PLAIN_SKILL.format(name=name, **spec))
    return tmp_path


# --------------------------------------------------------------------------
#  顺序
# --------------------------------------------------------------------------


class TestTheLoadOrderIsDeterministic:
    def test_the_contract_skill_comes_first_even_if_it_sorts_last(self, tmp_path):
        """`version-diff-review` 按字母序排在 `zzz-…` 后面，仍然必须最前。"""
        root = _mini_repo(tmp_path, **{"zzz-later": {"description": "后加的", "title": "Z"}})
        names = [path.name for path in iter_platform_skill_dirs(root)]
        assert names == [BODY_CONTRACT_SKILL_NAME, "zzz-later"], names

    def test_the_rest_are_sorted_so_the_prompt_prefix_is_stable(self, tmp_path):
        """其余按目录名排序 —— 提示词前缀要能命中缓存，顺序必须只取决于内容。"""
        root = _mini_repo(tmp_path, **{
            "middle-skill": {"description": "中间", "title": "M"},
            "alpha-skill": {"description": "最前", "title": "A"},
        })
        names = [path.name for path in iter_platform_skill_dirs(root)]
        assert names == [BODY_CONTRACT_SKILL_NAME, "alpha-skill", "middle-skill"], names

    def test_the_project_packs_directory_is_not_a_platform_skill(self, tmp_path):
        """`skills/projects/` 装的是项目知识包，它随项目代号寻址，不是底层 skill。"""
        root = _mini_repo(tmp_path)
        (root / "skills" / "projects" / "g119").mkdir(parents=True)
        names = [path.name for path in iter_platform_skill_dirs(root)]
        assert "projects" not in names, names


# --------------------------------------------------------------------------
#  加载与注入
# --------------------------------------------------------------------------


class TestEveryPlatformSkillIsLoadedUnconditionally:
    def test_a_second_platform_skill_lands_in_the_prompt(self, tmp_path):
        root = _mini_repo(tmp_path, **{
            "grant-safety-review": {"description": "资产发放审查", "title": "资产发放安全审查"},
        })
        loaded = load_skills(root)
        assert [doc.name for doc in loaded.platform_skills] == [
            BODY_CONTRACT_SKILL_NAME, "grant-safety-review",
        ]
        prompt = build_system_prompt(loaded)
        assert "资产发放安全审查" in prompt
        assert "版本变更评审" in prompt

    def test_it_loads_without_any_project(self, tmp_path):
        """「项目还没配 skill」是常态，底层 skill 不依赖它。"""
        root = _mini_repo(tmp_path, **{
            "grant-safety-review": {"description": "资产发放审查", "title": "资产发放安全审查"},
        })
        loaded = load_skills(root, project_code=None)
        assert loaded.project_slug is None
        assert len(loaded.platform_skills) == 2

    def test_the_platform_skill_document_name_is_the_directory_name(self, tmp_path):
        """文档名取目录名，不取文件名 —— 两份平台 skill 的正文文件都叫 `SKILL.md`，
        取文件名的话它们会重名，而 `build_readable_index` 对重名是硬错。"""
        root = _mini_repo(tmp_path, **{
            "grant-safety-review": {"description": "资产发放审查", "title": "资产发放安全审查"},
        })
        loaded = load_skills(root)
        names = [doc.name for doc in loaded.platform_skills]
        assert names == [BODY_CONTRACT_SKILL_NAME, "grant-safety-review"], names

    def test_both_platform_skills_references_are_readable(self, tmp_path):
        root = _mini_repo(tmp_path)
        _write_skill(
            root, "grant-safety-review",
            PLAIN_SKILL.format(name="grant-safety-review", description="审查", title="审查") ,
            references={"scenarios.md": "# 正反例\n"},
        )
        loaded = load_skills(root)
        assert "scenarios.md" in loaded.readable

    def test_the_contract_skill_is_still_the_singular_one(self, tmp_path):
        """`platform_skill` 这个单数入口的语义不能变：历史调用方按它找报告契约。"""
        root = _mini_repo(tmp_path, **{
            "grant-safety-review": {"description": "审查", "title": "审查"},
        })
        loaded = load_skills(root)
        assert loaded.platform_skill is loaded.platform_skills[0]
        assert loaded.platform_skill.name == BODY_CONTRACT_SKILL_NAME

    def test_changing_a_platform_skill_changes_the_revision(self, tmp_path):
        """新加的底层 skill 也必须进 revision —— 否则「skill 变了，旧结论作废」漏掉它。"""
        root = _mini_repo(tmp_path)
        before = load_skills(root).revision
        _write_skill(root, "grant-safety-review",
                     PLAIN_SKILL.format(name="grant-safety-review", description="审查", title="审查"))
        assert load_skills(root).revision != before

    def test_the_dimension_section_reaches_the_plural_copy(self, tmp_path, monkeypatch):
        """项目声明了自己的维度清单时，那一段追加进的是**复数那一份**。

        提示词读的是 `platform_skills`，而追加那一步重新绑定的是 `platform_skill`
        —— 两份分叉的话，「项目声明了维度清单」会变成提示词里不生效（报告里那九个维度
        还是出厂口径），而 `revision` 已经变了、缓存也作废了：一次白跑的全量分析。
        现在复数那一份是**由 `platform_skill` 现拼**的，这条断言钉住那个不变式。

        分支能被走到，靠的是项目声明了一份**不等于出厂九个**的清单（否则追加是空操作）。
        """
        from services.ai.project_facts import dimensions as declared_dimensions
        from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV

        pack = tmp_path / "p_dim" / "references"
        pack.mkdir(parents=True)
        (pack / "project-facts.md").write_text(
            "---\nname: p_dim\ndescription: 项目事实\n"
            "generated_prefixes: Cfg\n"
            "dimensions: performance=性能与耗时, protocol=协议兼容性\n"
            "---\n\n正文。\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(tmp_path))

        # 前置断言：这一档确实是活的（清单与出厂不同，追加那一步真的会跑）。
        loaded_probe = declared_dimensions("P_DIM", repo_root=REPO_ROOT)
        assert not contract.is_platform_default_dimensions(loaded_probe.dimensions)

        loaded = load_skills(REPO_ROOT, project_code="P_DIM")
        assert "## 本项目适用的维度清单" in loaded.platform_skill.text
        assert loaded.platform_skills[0].text == loaded.platform_skill.text, (
            "维度清单那一段只追加到了单数那一份 —— 提示词读的是复数那一份，声明等于没生效"
        )


# --------------------------------------------------------------------------
#  坏掉的部署要吵，不要静默
# --------------------------------------------------------------------------


class TestABrokenPlatformSkillIsLoud:
    def test_a_platform_dir_without_skill_md_is_an_error(self, tmp_path):
        """目录在、`SKILL.md` 不在 = 解压到一半或手工建错了。

        静默跳过的话，这一份底层内容对**每一次**分析都不生效，而页面上、日志里
        任何地方都看不出来 —— 只有模型的表现会不一样。
        """
        root = _mini_repo(tmp_path)
        (root / "skills" / "half-extracted").mkdir()
        with pytest.raises(SkillLoadError) as excinfo:
            load_skills(root)
        assert "half-extracted" in str(excinfo.value)

    def test_a_missing_contract_skill_is_still_an_error(self, tmp_path):
        (tmp_path / "skills").mkdir(parents=True)
        with pytest.raises(SkillLoadError) as excinfo:
            load_skills(tmp_path)
        assert "平台内置 skill 缺失" in str(excinfo.value)


# --------------------------------------------------------------------------
#  契约校验：只有承载报告格式的那一份要复述那几组枚举
# --------------------------------------------------------------------------


class TestTheBodyContractAppliesToExactlyOneSkill:
    def test_a_plain_platform_skill_is_not_asked_for_the_report_contract(self, tmp_path):
        """资产审查那类 skill 是**另一个领域的方法论**，逼它抄一份版本评审的报告章节
        只会抄出一份迟早与代码分叉的清单。

        这里断的是「**不额外**被要求」：合成出来的契约 skill 是个最小桩，它自己本来就
        过不了正文契约（那一档由下面那条单独钉），所以不能断言 `validate_all` 全空 ——
        要断言的是**普通那份没被卷进来**。
        """
        root = _mini_repo(tmp_path)
        _write_skill(root, "grant-safety-review",
                     PLAIN_SKILL.format(name="grant-safety-review", description="审查", title="审查"))
        failures = validate_all(root)
        assert "skills/grant-safety-review" not in failures, failures

    def test_the_contract_skill_still_has_to_satisfy_it(self, tmp_path):
        """承载契约那一份少了那几组枚举时要报出来（否则这条校验形同虚设）。"""
        root = _mini_repo(tmp_path)
        failures = validate_all(root)
        assert f"skills/{BODY_CONTRACT_SKILL_NAME}" in failures, failures
        assert any("category" in problem for problem in failures[f"skills/{BODY_CONTRACT_SKILL_NAME}"])

    def test_the_real_repo_passes(self):
        """真仓库里那两份平台 skill 都要过 —— 这是这次改动的最终交付判据。"""
        assert validate_all(REPO_ROOT) == {}

    def test_the_two_real_platform_skills_are_both_present(self):
        names = [path.name for path in iter_platform_skill_dirs(REPO_ROOT)]
        assert names[0] == BODY_CONTRACT_SKILL_NAME, names
        assert "grant-safety-review" in names, names
