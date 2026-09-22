# -*- coding: utf-8 -*-
"""知识包有**归属**：代号折成同一个目录的两个项目不共用一份包（REV-KNOW-001）。

## 缺陷

`project_pack_slug` 会 `lower()`，所以代号 `QAREV42` 与 `qarev42` 映到**同一个目录**。
而 `project.code` 的唯一约束是**大小写敏感**的（SQLite 的 TEXT UNIQUE 走 BINARY 排序），
两个项目完全建得出来、也合法共存。Windows 的文件系统同样不区分大小写 —— 所以这不是
「改个名就能躲开」的事：在那个平台上这两个代号**本来就只能是同一个目录**。

权限那一道只看「你是不是这个 `project_id` 的成员」（`utils/request_security`），
**中间没有任何一步反查「这个目录属于谁」**。于是 B 用自己的项目 URL：

* 读 → 200，内容是 A 的；
* 写 → 直接覆盖 A 的文件（知识包里**没有作者字段、也没有审计**，面板上只看得到
  `modified_at` 变成了 B 那次的时间）；
* 跑分析 → `load_skills` 把 A 的 references 与子 skill **整份灌进 B 的提示词**。
  这一条最隐蔽：没有任何 HTTP 状态码看得出来。

## 修法

包目录里写一个**归属标记**（`.pack-owner`），记**代号原文**（大小写敏感）。三处读入口
（`locate_pack`、`load_skills`、`declaration_path`）各查一次；写入口都经 `locate_pack`，
所以读写都挡住。

标记记代号原文而不是 `project_id`，是为了**不改任何签名** —— 三处调用方手上原本就有
`code`。没有标记的包按「未认领」放行（仓库里手工维护的 `skills/projects/g119/` 就没有
标记），第一次写入时补上，之后这个包就归它了。
"""
import uuid
from pathlib import Path

import pytest

from services.ai.endpoint_service import ConfigValidationError
from services.ai.project_facts import read_declarations
from services.ai.project_pack_service import (
    create_pack_from_template,
    locate_pack,
    write_reference,
)
from services.ai.skill_loader import PACK_OWNER_FILENAME, load_skills

REPO_ROOT = Path(__file__).resolve().parents[1]


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def owned_pack(tmp_path, monkeypatch):
    """一个真知识包，归**大写**那个代号；返回 (大写代号, 小写变体, 项目根)。"""
    from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV

    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(root))
    upper = _uid("QAREV").upper()
    create_pack_from_template(upper, repo_root=tmp_path)
    write_reference(upper, "secret-doc", "# 机密\n\nA 项目专属，B 不该看得到。",
                    repo_root=tmp_path)
    return upper, upper.lower(), root


def _doc(root: Path, upper: str) -> Path:
    return root / upper.lower() / "references" / "secret-doc.md"


def test_the_owner_writes_the_marker(tmp_path, monkeypatch):
    from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV

    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(root))
    code = _uid("QAREV").upper()
    create_pack_from_template(code, repo_root=tmp_path)

    marker = root / code.lower() / PACK_OWNER_FILENAME
    assert marker.is_file(), "包建出来了却没有归属标记，下一个项目就能直接接管它"
    assert marker.read_text(encoding="utf-8").strip() == code


def test_a_case_variant_code_cannot_even_locate_the_pack(owned_pack):
    _upper, lower, _root = owned_pack
    with pytest.raises(ConfigValidationError):
        locate_pack(lower, repo_root=REPO_ROOT)


def test_a_case_variant_code_cannot_overwrite_the_owner(owned_pack):
    """最要紧的一条：写方向也要挡住，且**原文件一个字节都不能变**。"""
    upper, lower, root = owned_pack
    before = _doc(root, upper).read_text(encoding="utf-8")

    with pytest.raises(ConfigValidationError):
        write_reference(lower, "secret-doc", "# 被别的项目改掉了", repo_root=REPO_ROOT)

    assert _doc(root, upper).read_text(encoding="utf-8") == before


def test_the_owner_still_uses_its_own_pack(owned_pack):
    """反方向：归属校验不许把主人自己挡在门外。"""
    upper, _lower, _root = owned_pack
    assert locate_pack(upper, repo_root=REPO_ROOT).pack_dir.is_dir()


def test_analysis_does_not_load_another_projects_knowledge(owned_pack):
    """最隐蔽的那条：B 跑分析时，A 的知识包不许进它的提示词。"""
    _upper, lower, _root = owned_pack

    loaded = load_skills(REPO_ROOT, project_code=lower)

    assert loaded.project_manifest is None, "把别的项目的知识包加载进来了"
    assert loaded.project_references == ()
    assert loaded.project_skills == ()


def test_another_projects_declarations_are_not_read(owned_pack):
    """维度清单、关键路径模式、生成物前缀都不许跟着别人的声明走。"""
    _upper, lower, _root = owned_pack

    declarations = read_declarations(lower, repo_root=REPO_ROOT)

    assert declarations.fields == {}


def test_an_unclaimed_pack_is_still_usable(tmp_path, monkeypatch):
    """反方向：没有标记的包（仓库里手工维护的那种）按「未认领」放行，不许一刀切拒绝。"""
    from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV

    root = tmp_path / "projects"
    (root / "handmade").mkdir(parents=True)
    (root / "handmade" / "KNOWLEDGE.md").write_text(
        "---\nname: handmade\ndescription: 手工维护的包\n---\n\n正文。\n", encoding="utf-8"
    )
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(root))

    location = locate_pack("handmade", repo_root=tmp_path)

    assert location.pack_dir.is_dir()
