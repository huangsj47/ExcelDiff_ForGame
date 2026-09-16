"""skill 加载器：越权、歧义与版本标识。

## 三条要守住的静默失效

1. **路径穿越**：项目代号来自数据库，可能是历史遗留的任意字符串。一旦拼出
   `skills/projects/../../app.py` 这种路径，平台就会去读仓库外的文件并把内容注入
   提示词。所以拼装一律走 `safe_join`，且用 `resolve()` 后的 `relative_to` 判断
   包含性 —— 字符串前缀比较会被 `/a/bc` 骗过。
2. **跨层重名**：平台 skill 与项目知识包里如果都有 `incident-checklist.md`，模型
   索要时拿到哪一份就不可预期了，而两份内容可能给出冲突的规则。必须直接报错，
   不能让某一层静默胜出。
3. **版本标识**：`revision` 是分析结果幂等键的一部分。它必须**由内容算出**，否则
   「改了提示词/项目知识却复用旧结果」这类问题会悄悄发生 —— 这正是要评审的那份
   外部工具踩过的坑（手工维护 prompt 版本号，改 prompt 忘改版本号）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.ai.skill_loader import (
    SKILL_PROJECTS_ROOT_ENV,
    LoadedSkills,
    SkillLoadError,
    build_readable_index,
    build_skill_index,
    load_skills,
    project_pack_slug,
    resolve_projects_root,
    safe_join,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_repo(
    tmp_path: Path,
    *,
    platform_body: str = "# 平台协议\n\n平台正文。\n",
    platform_refs: tuple[str, ...] = (),
    project_slug: str = "g119",
    project_manifest: str | None = "---\nname: pack\ndescription: 项目包\n---\n\n# 项目包\n",
    project_refs: tuple[str, ...] = (),
    sub_skills: tuple[str, ...] = (),
) -> Path:
    """搭一个最小仓库：平台 skill + 一个项目知识包。"""
    root = tmp_path
    platform = root / "skills" / "version-diff-review"
    _write(
        platform / "SKILL.md",
        "---\nname: version-diff-review\ndescription: 平台协议\n---\n\n" + platform_body,
    )
    for name in platform_refs:
        _write(platform / "references" / name, f"# {name}\n")

    pack = root / "skills" / "projects" / project_slug
    if project_manifest is not None:
        _write(pack / "KNOWLEDGE.md", project_manifest)
        for name in project_refs:
            _write(pack / "references" / name, f"# {name}\n")
    for name in sub_skills:
        _write(
            pack / name / "SKILL.md",
            f"---\nname: {name}\ndescription: 子 skill\n---\n\n# {name}\n",
        )
    return root


# --------------------------------------------------------------------------
# 正常加载
# --------------------------------------------------------------------------


def test_loads_the_real_platform_skill_and_g119_pack():
    loaded = load_skills(REPO_ROOT, project_code="G119")

    assert loaded.platform_skill.name == "SKILL.md"
    assert loaded.platform_skill.char_count > 1000
    assert loaded.project_slug == "g119"
    assert loaded.project_manifest is not None
    # 项目事实文档必须在可读清单里，否则模型读不到它们。
    assert {"gameplay-semantics.md", "config-table-spec.md"} <= set(loaded.readable)
    assert loaded.revision


def test_a_project_without_its_own_pack_is_not_an_error():
    """「项目还没维护 skill」是正常状态，不是错误 —— 报错会让新项目无法分析。"""
    loaded = load_skills(REPO_ROOT, project_code="NOT_A_REAL_PROJECT")

    assert loaded.project_documents == ()
    assert loaded.project_manifest is None
    assert loaded.platform_skill.char_count > 0


def test_no_project_code_loads_platform_only():
    loaded = load_skills(REPO_ROOT, project_code=None)
    assert loaded.project_documents == ()
    assert loaded.project_slug is None


def test_missing_platform_skill_is_a_hard_error(tmp_path):
    """平台 skill 是必需依赖，缺了要明确报错而不是静默降级成「没有提示词」。"""
    (tmp_path / "skills").mkdir()
    with pytest.raises(SkillLoadError):
        load_skills(tmp_path, project_code=None)


def test_project_sub_skills_are_discovered(tmp_path):
    root = _make_repo(tmp_path, project_refs=("a.md",), sub_skills=("my-extra-skill",))
    loaded = load_skills(root, project_code="g119")

    assert [document.name for document in loaded.project_skills] == ["SKILL.md"]
    assert "my-extra-skill" in str(loaded.project_skills[0].path)
    assert "SKILL.md" in loaded.readable


# --------------------------------------------------------------------------
# 路径穿越
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "segment",
    [
        "..",
        "../x",
        "a/../../b",
        "",
        ".",
        "/abs/path",
        "C:/win",
        "a//b",
        "a/b",
        "a\\b",
    ],
)
def test_safe_join_blocks_traversal(segment):
    """只接受单段片段。

    `a/b` 与 `a//b` 也要拒：虽然它们规范化后仍在根内（`pathlib` 会把 `//` 折叠），
    但允许它们等于让调用方越过一层目录，而这个函数的用途只是把**一个受控名字**
    挂到根下。
    """
    with pytest.raises(SkillLoadError):
        safe_join(REPO_ROOT / "skills", segment)


def test_safe_join_allows_a_normal_segment():
    joined = safe_join(REPO_ROOT / "skills", "projects", "g119")
    assert joined.name == "g119"


@pytest.mark.parametrize(
    "raw",
    ["G119", "g119", "G 119", "破碎之地 2026", "../etc", "G119/../../etc", "..", "/"],
)
def test_project_slug_never_contains_a_path_separator_or_dotdot(raw):
    """代号要先过一遍 slug 化，结果里不可能再出现分隔符或上跳段。"""
    slug = project_pack_slug(raw)
    assert "/" not in slug and "\\" not in slug
    assert ".." not in slug
    assert not slug.startswith(("-", "."))


def test_project_slug_is_deterministic():
    """管理页展示的目录名与实际落盘目录必须能对上，所以映射不能有随机性。"""
    assert project_pack_slug("G119") == project_pack_slug("G119")
    assert project_pack_slug("G119") == "g119"


def test_projects_root_can_be_overridden_by_env(tmp_path, monkeypatch):
    custom = tmp_path / "elsewhere"
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(custom))
    assert resolve_projects_root(REPO_ROOT) == custom.resolve()

    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, "relative/dir")
    assert resolve_projects_root(REPO_ROOT) == (REPO_ROOT / "relative/dir").resolve()

    monkeypatch.delenv(SKILL_PROJECTS_ROOT_ENV, raising=False)
    assert resolve_projects_root(REPO_ROOT).name == "projects"


# --------------------------------------------------------------------------
# 跨层重名
# --------------------------------------------------------------------------


def _doc(tmp_path: Path, relative: str, text: str = "# x\n"):
    from services.ai.skill_loader import _read_document

    path = tmp_path / relative
    _write(path, text)
    return _read_document(path, require_frontmatter=False)


def test_duplicate_basenames_across_layers_are_rejected(tmp_path):
    """同一文件名同时出现在平台层与项目层时必须报错，而不是让某一层静默胜出。

    静默胜出的后果：模型索要 `incident-checklist.md`，实际拿到哪一份取决于字典
    插入顺序，而两份可能给出互相冲突的规则。
    """
    platform = _doc(tmp_path, "platform/incident-checklist.md")
    project = _doc(tmp_path, "project/incident-checklist.md")

    with pytest.raises(SkillLoadError) as excinfo:
        build_readable_index((platform,), (project,))
    assert "重名" in str(excinfo.value)


def test_same_file_appearing_twice_is_not_a_false_positive(tmp_path):
    """同一个 path 传两次不算重名冲突（否则调用方稍一重复就炸）。"""
    document = _doc(tmp_path, "a/x.md")
    assert build_readable_index((document,), (document,)) == {"x.md": document.path}


# --------------------------------------------------------------------------
# 版本标识随内容变化
# --------------------------------------------------------------------------


def test_revision_tracks_project_content(tmp_path):
    """项目知识改了，revision 必须变 —— 否则会复用旧结果，用户拿不到新规则下的分析。"""
    root = _make_repo(tmp_path, project_refs=("a.md",))
    before = load_skills(root, project_code="g119").revision

    _write(root / "skills" / "projects" / "g119" / "references" / "a.md", "# a\n改过了\n")
    after = load_skills(root, project_code="g119").revision

    assert before != after


def test_revision_tracks_platform_content(tmp_path):
    """平台 skill 正文改了同理。"""
    root = _make_repo(tmp_path)
    before = load_skills(root, project_code="g119").revision

    _write(
        root / "skills" / "version-diff-review" / "SKILL.md",
        "---\nname: version-diff-review\ndescription: 平台协议\n---\n\n# 改过的协议\n",
    )
    after = load_skills(root, project_code="g119").revision

    assert before != after


def test_revision_is_stable_when_nothing_changes(tmp_path):
    """反向自检：内容没变时 revision 必须一致，否则幂等键永远不命中、每次重跑。"""
    root = _make_repo(tmp_path, project_refs=("a.md",))
    assert load_skills(root, project_code="g119").revision == load_skills(
        root, project_code="g119"
    ).revision


# --------------------------------------------------------------------------
# 索引体积极小（提示词预算的保证）
# --------------------------------------------------------------------------


def test_skill_index_lists_names_without_bodies():
    """注入的索引只能有名字和描述，**不能带正文** —— 否则「按需读取」就白设计了。"""
    loaded = load_skills(REPO_ROOT, project_code="G119")
    index = build_skill_index(loaded)

    assert "incident-checklist.md" in index
    assert "gameplay-semantics.md" in index
    # 正文里的标志性内容不得出现在索引里。
    assert "吸灵器" not in index
    assert "事故计数" not in index
    # 索引必须远小于正文总量。
    body_total = sum(document.char_count for document in loaded.project_documents)
    assert len(index) < body_total / 5


def test_skill_index_says_so_when_a_project_has_no_documents():
    loaded = load_skills(REPO_ROOT, project_code=None)
    index = build_skill_index(loaded)
    assert "尚未维护专属知识文档" in index


# --------------------------------------------------------------------------
# 知识包内部一致性
# --------------------------------------------------------------------------


def _segments(raw: str) -> dict[tuple[str, str], str]:
    """从一行行 `| ... | ... |` 里抽出 `(起, 止) -> 名称`。"""
    import re

    return {
        (start, end): name.strip()
        for name, start, end in re.findall(
            r"\|\s*([^|（）]+?)\s*（`?(\d+)`?[–-]`?(\d+)`?）\s*\|", raw
        )
    }


def _type_segments(raw: str) -> dict[tuple[str, str], str]:
    import re

    return {
        (start, end): name.strip()
        for start, end, name in re.findall(
            r"\|\s*`(\d+)`[–-]`(\d+)`\s*\|\s*([^|]+?)\s*\|", raw
        )
    }


def test_the_id_segment_tables_in_the_knowledge_pack_agree():
    """**同一份文档里的两张表必须说同一件事。**

    这里出过一次真实的矛盾：号段表写 `70–79 物品相关`，表划分表写 `任务（70–79）任务节点表、
    任务组`。模型读到两个不同的说法，70–79 段的改动就会被归错系统，进而给错回归范围 ——
    而它不会报错，只会安静地给出一个错的结论。

    判据是两个字的重合：号段说「任务相关」，表划分就该出现「任务」。这不需要两张表的
    措辞完全一致（它们本来就一张写「角色相关（含属性）」、一张写「角色」），但**不能
    一个说任务、一个说物品**。
    """
    raw = (REPO_ROOT / "skills/projects/g119/references/config-table-spec.md").read_text(
        encoding="utf-8"
    )
    types = _type_segments(raw)
    systems = _segments(raw)

    assert types, "号段表没解析出来 —— 表头或格式变了，下面的断言会变成空转"
    assert systems, "表划分表没解析出来 —— 表头或格式变了"

    shared = set(types) & set(systems)
    assert len(shared) >= 6, f"两张表对得上号的段太少（{sorted(shared)}），检查解析是否失效"

    for segment in sorted(shared):
        type_name = types[segment]
        system_name = systems[segment]
        bigrams = {type_name[index : index + 2] for index in range(len(type_name) - 1)}
        assert bigrams & {
            system_name[index : index + 2] for index in range(len(system_name) - 1)
        }, (
            f"{segment[0]}–{segment[1]} 段在两处说法不一致："
            f"号段表说「{type_name}」，表划分表说「{system_name}」"
        )
