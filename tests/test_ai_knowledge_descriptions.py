from pathlib import Path

import pytest

from services.ai import project_pack_service as packs
from services.ai.skill_loader import _read_document, build_skill_index, load_skills

ROOT = Path(__file__).resolve().parents[1]


def test_reference_description_shared_by_panel_and_model(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_PROJECTS_ROOT", str(tmp_path))
    packs.create_pack_from_template("sample", repo_root=ROOT)
    packs.write_reference("sample", "rewards", "# 奖励说明\n", description="奖励的物品名称与场景用途", repo_root=ROOT)
    entry = next(e for e in packs.describe_pack("sample", repo_root=ROOT)["entries"] if e["name"] == "rewards.md")
    assert entry["description"] == "奖励的物品名称与场景用途"
    loaded = load_skills(ROOT, project_code="sample")
    revision = loaded.revision
    assert "rewards.md` —— 奖励的物品名称与场景用途" in build_skill_index(loaded)
    packs.write_reference(
        "sample",
        "rewards",
        packs.read_entry("sample", "reference", "rewards", repo_root=ROOT)["content"],
        description="新的奖励规则简介",
        repo_root=ROOT,
    )
    saved = packs.read_entry("sample", "reference", "rewards", repo_root=ROOT)
    assert saved["description"] == "新的奖励规则简介"
    assert saved["content"].count("description:") == 1
    assert "# 奖励说明" in saved["content"]
    assert load_skills(ROOT, project_code="sample").revision != revision


def test_edit_description_preserves_machine_declarations(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_PROJECTS_ROOT", str(tmp_path))
    packs.create_pack_from_template("sample", repo_root=ROOT)
    body = "---\nname: sample-facts\ndescription: 原简介\ngenerated_prefixes: Cfg\n---\n\n# 事实\n"
    packs.write_reference("sample", "extra-facts", body, description="新简介", repo_root=ROOT)
    saved = packs.read_entry("sample", "reference", "extra-facts", repo_root=ROOT)
    assert "generated_prefixes: Cfg" in saved["content"]
    assert saved["description"] == "新简介"
    # Callers saving raw Markdown without an explicit description keep metadata.
    packs.write_reference("sample", "extra-facts", saved["content"], repo_root=ROOT)
    assert packs.read_entry("sample", "reference", "extra-facts", repo_root=ROOT)["description"] == "新简介"


def test_legacy_reference_uses_heading_instead_of_generic_usage(tmp_path):
    path = tmp_path / "legacy.md"
    path.write_text("# 回收物的出售与估值\n\n旧正文\n", encoding="utf-8")
    assert _read_document(path, require_frontmatter=False).description == "回收物的出售与估值"


def test_bad_metadata_is_editable_but_rejected_for_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_PROJECTS_ROOT", str(tmp_path))
    packs.create_pack_from_template("sample", repo_root=ROOT)
    packs.write_reference("sample", "bad", "# 正常正文\n", repo_root=ROOT)
    target = tmp_path / "sample/references/bad.md"
    target.write_text("---\ndescription: broken: yaml\n---\n# 正文\n", encoding="utf-8")
    entries = packs.describe_pack("sample", repo_root=ROOT)["entries"]
    assert "格式有误" in next(e for e in entries if e["name"] == "bad.md")["description"]
    assert "broken: yaml" in packs.read_entry("sample", "reference", "bad", repo_root=ROOT)["content"]
    from services.ai.skill_loader import SkillLoadError

    with pytest.raises(SkillLoadError):
        load_skills(ROOT, project_code="sample")


def test_g119_descriptions_agree_with_the_model_index():
    entries = packs.describe_pack("G119", repo_root=ROOT)["entries"]
    loaded = load_skills(ROOT, project_code="G119")
    documents = {d.name: d for d in loaded.project_documents}
    for entry in entries:
        assert entry["description"] == documents[entry["name"]].description
        assert entry["description"]
    assert len({e["description"] for e in entries}) == len(entries)
    assert {"version-scope.md", "config-pipelines.md", "implementation-map.md"} <= set(documents)


@pytest.mark.parametrize("description", ["x" * 1025, "一行\n另一行"])
def test_invalid_description_does_not_write_partial_reference(tmp_path, monkeypatch, description):
    monkeypatch.setenv("SKILL_PROJECTS_ROOT", str(tmp_path))
    packs.create_pack_from_template("sample", repo_root=ROOT)
    with pytest.raises(packs.ConfigValidationError):
        packs.write_reference("sample", "bad", "# 正文\n", description=description, repo_root=ROOT)
    assert not (tmp_path / "sample/references/bad.md").exists()
