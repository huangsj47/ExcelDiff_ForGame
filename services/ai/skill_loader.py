"""加载平台内置 skill 与项目专属知识。

## 加载什么

| 内容 | 位置 | 是否进初始提示词 |
|---|---|---|
| 平台 skill 正文 | `skills/version-diff-review/SKILL.md` | **是**，无条件 |
| 平台 skill 的 references | 同上 `references/` | 否，按需 `read_reference` |
| 项目知识包说明 | `skills/projects/<slug>/KNOWLEDGE.md` | 否，按需 |
| 项目知识文档 | 同上 `references/` | 否，按需 |
| 项目子 skill 正文 | 同上 `<子目录>/SKILL.md` | 否，按需 |
| **索引**（上面的 name + description + 可读文件名） | —— | **是**，体积极小 |

## 为什么项目侧只注入索引

「自动加载项目 skills」不能理解成「把项目下所有 skill 正文都塞进提示词」——用户
传 5～10 个 skill 就会撑爆上下文预算，而这正是渐进式披露要解决的问题。所以固定的
提示词开销是「平台 skill 正文 + 项目索引」，正文一律按需取。模型从索引里知道有
哪些文档可读，就足够决定要不要读。

## 不做缓存

文件都很小（几 KB），每次分析开始时重读一遍的开销可以忽略，换来的是**改完立即生效**
（不需要重启进程）。这正是不采用 `lru_cache` 的原因——本仓库要评审的那份外部工具
就因为在规则加载上加了 `lru_cache`，导致运行期改规则必须重启才生效。

## 内容哈希就是版本号

`revision` 由全部参与内容哈希合成，进而进入分析结果的幂等键。这样「提示词/规则/
项目知识改了就重跑」是自动成立的，不依赖任何人记得手工改版本号——外部工具那份手工
维护的 `ITERATIVE_ANALYSIS_PROMPT_VERSION` 就是反例（改了 prompt 忘了改版本号，
用户永远拿不到新结果）。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from services.ai.skill_contract import (
    PLATFORM_SKILL_RELATIVE_PATH,
    PROJECT_PACK_MANIFEST,
    PROJECT_PACKS_RELATIVE_PATH,
    SkillContractError,
    parse_frontmatter,
)

# 项目 skill 根目录。可用环境变量改，便于换部署形态或让测试指向临时目录。
SKILL_PROJECTS_ROOT_ENV = "SKILL_PROJECTS_ROOT"

# 目录名/文件名的合法字符：字母数字、连字符、下划线、中文。
# 刻意**不包含点**：点会让 `..` 残留在 slug 里（`G119/../../etc` 会变成
# `g119-..-..-etc`）。虽然那仍是单个目录名、不构成穿越，但保留 `..` 没有任何好处，
# 只会让后续任何一处按分隔符再处理它的代码多一个隐患。
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_一-鿿-]+")


class SkillLoadError(RuntimeError):
    """skill 目录缺失或不可用。"""


@dataclass(frozen=True)
class SkillDocument:
    """一份可读的 markdown 文档。"""

    name: str
    description: str
    path: Path
    text: str
    content_hash: str

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class LoadedSkills:
    """一次分析要用到的全部 skill 内容。"""

    platform_skill: SkillDocument
    platform_references: tuple[SkillDocument, ...]
    project_manifest: SkillDocument | None
    project_references: tuple[SkillDocument, ...]
    project_skills: tuple[SkillDocument, ...]
    # 可读文档白名单：键是模型在 `read_reference` 里写的名字，值是文件所在路径。
    readable: dict[str, Path] = field(default_factory=dict)
    # 项目知识包的目录名（None 表示该项目没有知识包）。
    project_slug: str | None = None
    # 全部参与内容的哈希合成，用作分析结果的版本标识。
    revision: str = ""

    @property
    def project_documents(self) -> tuple[SkillDocument, ...]:
        """项目侧的全部文档（说明 + 知识文档 + 子 skill）。"""
        documents: list[SkillDocument] = []
        if self.project_manifest is not None:
            documents.append(self.project_manifest)
        documents.extend(self.project_references)
        documents.extend(self.project_skills)
        return tuple(documents)


def project_pack_slug(project_code: str) -> str:
    """把项目代号映射成目录名。

    项目代号来自数据库（例如 `G119`），目录名统一小写并把不合法字符折成连字符。
    映射必须是**确定性**的：管理页展示的目录名与实际落盘目录要能对上。
    """
    slug = _SAFE_SEGMENT_RE.sub("-", str(project_code or "").strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


def safe_join(root: Path, *segments: str) -> Path:
    """把若干**单个**路径片段拼到 root 下，并确认结果没跑到 root 外面。

    只接受**单段**片段（内部不含路径分隔符）。调用方传 `a/b` 是一种契约违反：
    它会让调用方越过一层目录，而这个函数的用途只是「把一个受控名字挂到根下」。
    即便片段都由服务端生成也要校验 —— 项目代号来自数据库，可能是历史遗留的任意
    字符串。

    包含性判断用 `Path.resolve()` 之后的 `relative_to`，**不用字符串前缀比较**——
    前缀比较会把 `/a/bc` 判成在 `/a/b` 里面。
    """
    if not segments:
        raise SkillLoadError("safe_join 至少需要一个片段")
    for segment in segments:
        text = str(segment or "")
        if not text or text in {".", ".."}:
            raise SkillLoadError(f"非法的路径片段：{segment!r}")
        if Path(text).is_absolute():
            raise SkillLoadError(f"不接受绝对路径：{segment!r}")
        # 单段契约：pathlib 会把 `a//b` 折叠成 `a/b`，所以不能靠 parts 里有没有
        # 空段来判断，必须直接看有没有分隔符。
        if "/" in text or "\\" in text:
            raise SkillLoadError(f"路径片段里不能含分隔符（只接受单段）：{segment!r}")
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*segments)
    try:
        candidate.resolve().relative_to(resolved_root)
    except ValueError as exc:
        raise SkillLoadError(f"路径逃出了 skill 根目录：{candidate}") from exc
    return candidate


def _read_document(path: Path, *, require_frontmatter: bool) -> SkillDocument:
    text = path.read_text(encoding="utf-8")
    description = ""
    if require_frontmatter:
        try:
            fields, _body = parse_frontmatter(text)
        except SkillContractError as exc:
            raise SkillLoadError(f"{path} 的 frontmatter 不合法：{exc}") from exc
        description = fields.get("description", "")
    return SkillDocument(
        name=path.name,
        description=description,
        path=path,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def resolve_projects_root(repo_root: Path) -> Path:
    """项目 skill 根目录。

    默认是仓库内的 `skills/projects`；`SKILL_PROJECTS_ROOT` 可覆盖（相对路径按仓库根
    解析）。抽成函数而不是模块常量，是为了让测试能指向临时目录。
    """
    override = str(os.environ.get(SKILL_PROJECTS_ROOT_ENV) or "").strip()
    if not override:
        return (repo_root / PROJECT_PACKS_RELATIVE_PATH).resolve()
    candidate = Path(override)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve()


def _collect_references(directory: Path) -> tuple[SkillDocument, ...]:
    if not directory.is_dir():
        return ()
    return tuple(_read_document(path, require_frontmatter=False) for path in sorted(directory.glob("*.md")))


def _collect_project_sub_skills(pack_dir: Path) -> tuple[SkillDocument, ...]:
    """项目目录下用户新建/上传的子 skill（每个是一个含 SKILL.md 的文件夹）。"""
    documents: list[SkillDocument] = []
    for child in sorted(path for path in pack_dir.glob("*") if path.is_dir()):
        if child.name == "references":
            continue
        skill_md = child / "SKILL.md"
        if skill_md.is_file():
            documents.append(_read_document(skill_md, require_frontmatter=True))
    return tuple(documents)


def build_readable_index(
    platform_references: Iterable[SkillDocument],
    project_documents: Iterable[SkillDocument],
) -> dict[str, Path]:
    """构造 `read_reference` 的白名单。

    键是 basename。**跨层重名直接报错**而不是让某一层静默胜出：重名意味着模型索要
    `foo.md` 时拿到哪一份是不可预期的，而这两份内容可能给出冲突的规则。
    """
    readable: dict[str, Path] = {}
    for document in (*tuple(platform_references), *tuple(project_documents)):
        existing = readable.get(document.name)
        if existing is not None and existing != document.path:
            raise SkillLoadError(
                f"可读文档重名：{document.name} 同时存在于 {existing} 与 {document.path}。"
                "重名会让模型索要时拿到哪一份变得不确定，请改名"
            )
        readable[document.name] = document.path
    return readable


def load_skills(
    repo_root: Path,
    *,
    project_code: str | None = None,
    projects_root: Path | None = None,
) -> LoadedSkills:
    """加载平台 skill，以及（若给了项目）该项目的专属知识。

    `project_code` 为空时只加载平台 skill —— 这是「项目还没配 skill」的正常情形，
    不是错误。
    """
    platform_dir = repo_root / PLATFORM_SKILL_RELATIVE_PATH
    platform_md = platform_dir / "SKILL.md"
    if not platform_md.is_file():
        raise SkillLoadError(
            f"平台内置 skill 缺失：{platform_md}。它是分析功能的必需依赖，"
            "请确认部署包里带上了 skills/ 目录"
        )

    platform_skill = _read_document(platform_md, require_frontmatter=True)
    platform_references = _collect_references(platform_dir / "references")

    manifest: SkillDocument | None = None
    project_references: tuple[SkillDocument, ...] = ()
    project_skills: tuple[SkillDocument, ...] = ()
    slug: str | None = None

    if project_code:
        slug = project_pack_slug(project_code)
        root = projects_root or resolve_projects_root(repo_root)
        if slug:
            pack_dir = safe_join(root, slug)
            if pack_dir.is_dir():
                manifest_path = pack_dir / PROJECT_PACK_MANIFEST
                if manifest_path.is_file():
                    manifest = _read_document(manifest_path, require_frontmatter=True)
                project_references = _collect_references(pack_dir / "references")
                project_skills = _collect_project_sub_skills(pack_dir)
            # 目录不存在不是错误：项目可能还没维护过自己的 skill。

    documents = (
        ((manifest,) if manifest else ())
        + project_references
        + project_skills
    )
    readable = build_readable_index(platform_references, documents)

    hasher = hashlib.sha256()
    for document in (platform_skill, *platform_references, *documents):
        hasher.update(document.name.encode("utf-8"))
        hasher.update(document.content_hash.encode("utf-8"))

    return LoadedSkills(
        platform_skill=platform_skill,
        platform_references=platform_references,
        project_manifest=manifest,
        project_references=project_references,
        project_skills=project_skills,
        readable=readable,
        project_slug=slug,
        revision=hasher.hexdigest()[:12],
    )


def build_skill_index(loaded: LoadedSkills) -> str:
    """渲染注入提示词的「可读文档索引」。

    只列文件名与它们各自的用途说明，**不列正文**——正文由模型按需索取。
    """
    lines: list[str] = []

    lines.append("## 本 skill 自带的可读文档（项目无关）")
    if loaded.platform_references:
        for document in loaded.platform_references:
            lines.append(f"- `{document.name}`（{document.char_count} 字）")
    else:
        lines.append("- （无）")

    project_documents = loaded.project_documents
    lines.append("")
    if project_documents:
        lines.append(f"## 项目专属知识（项目代号 {loaded.project_slug}）")
        lines.append("这些文档描述**这个项目**的事实，判断「属于哪个系统」「按什么规则编号」时先读它们：")
        for document in project_documents:
            if document.description:
                lines.append(f"- `{document.name}` —— {document.description}")
            else:
                lines.append(f"- `{document.name}`（{document.char_count} 字）")
    else:
        lines.append("## 项目专属知识")
        lines.append("（该项目尚未维护专属知识文档。缺少项目事实时，不要凭文件名的样子猜，"
                     "在报告里标注信息缺口。）")

    return "\n".join(lines)
