"""平台内置 skill 的结构契约。

## 为什么单独一个模块

skill 的 `SKILL.md` 同时被三方读取：**平台运行期**（注入提示词）、**测试**（守住契约）、
以及 `skill-creator` 的校验器。三方对「合法」的判定必须一致，否则会出现「本地能跑、
校验器判不合法」或者反过来。把判定集中在这里，三边引用同一份常量。

## 为什么不用 PyYAML 解析 frontmatter

`yaml` 在本环境里能 import，但它**没有在任何 requirements 里声明**——它是
`pre-commit` 的传递依赖。运行期代码依赖传递依赖，等于把「某天 pre-commit 换了实现」
变成线上故障。所以这里自己解析，并且**只接受最简单的 `key: value` 单行标量**：
一旦出现多行、引号、列表等 YAML 特性，就直接拒绝，而不是猜。

拒绝的边界按「**PyYAML 会不会也这么理解**」来划——我们不用 PyYAML，但 skill-creator
的校验器用，所以本模块接受的形态必须是 PyYAML 也接受的**子集**。典型例子：值里出现
`: `（冒号加空格）时纯量标量会被截断，所以直接判非法，而不是容忍。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# 平台内置 skill 的位置（仓库根相对）。
PLATFORM_SKILL_RELATIVE_PATH = "skills/version-diff-review"
# 项目知识包的父目录。每个子目录是一个项目专属知识包。
PROJECT_PACKS_RELATIVE_PATH = "skills/projects"
# 项目知识包内描述自身的文件。
PROJECT_PACK_MANIFEST = "KNOWLEDGE.md"

# skill-creator 的 quick_validate.py 只放行这几个键，多一个就判不合法。
ALLOWED_FRONTMATTER_KEYS = frozenset(
    {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
)
REQUIRED_FRONTMATTER_KEYS = ("name", "description")

# 与 skill-creator 的 quick_validate.py 逐条对齐的限额。
SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")
SKILL_NAME_MAX_CHARS = 64
DESCRIPTION_MAX_CHARS = 1024
COMPATIBILITY_MAX_CHARS = 500
# SKILL.md 正文的建议上限（skill-creator: "Keep SKILL.md under 500 lines"）。
SKILL_MD_MAX_LINES = 500
# 超过这个行数的 reference 必须带目录（skill-creator: ">300 lines, include a TOC"）。
REFERENCE_TOC_THRESHOLD_LINES = 300

# 六个检查维度。**这是运行期契约**：模型的 `dimensions[].id` 与异常的 `category`
# 都必须落在这个集合里，服务端按它校验。改这里就必须同步改 SKILL.md 里的枚举。
DIMENSION_IDS = (
    "config_id",
    "config_value",
    "config_linkage",
    "code_logic",
    "version_branch",
    "process",
)

# 报告结构。**这也是运行期契约**：「轮次耗尽但回答像报告」的降级判定会数这些标题，
# 命中足够多才认为这份回答可当报告用。改这里就必须同步改 SKILL.md。
REPORT_SECTIONS = (
    "变更理解",
    "变更内容摘要",
    "影响面分析",
    "风险评估",
    "测试建议",
    "回归建议",
    "上线与回滚关注点",
)

# 值里不允许出现的片段：`": "` 会被 YAML 当成新的键值对，`" #"` 会被当成注释。
_FORBIDDEN_VALUE_FRAGMENTS = (": ", " #")
# 值不允许以这些字符开头：YAML 会把它们当成流式集合、锚点、标签或块标量的引导符。
_FORBIDDEN_VALUE_PREFIXES = ("\"", "'", "[", "{", "&", "*", "!", "|", ">", "%", "@", "`", "#")


class SkillContractError(ValueError):
    """skill 结构不符合契约。"""


_FRONTMATTER_DELIMITER = "---"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """把 SKILL.md 拆成 (frontmatter 字典, 正文)。

    只接受 `key: value` 单行标量。任何 YAML 特性（注释、引号、列表、多行）都会抛
    `SkillContractError`，而不是被猜着解析 —— 猜错的代价是运行期拿到一个诡异的字符串。
    """
    if not text.startswith(_FRONTMATTER_DELIMITER):
        raise SkillContractError(
            "SKILL.md 必须以 `---` 开头，且 `---` 必须在第 0 列（前面不能有空行或 BOM）"
        )

    closing = text.find("\n" + _FRONTMATTER_DELIMITER, len(_FRONTMATTER_DELIMITER))
    if closing == -1:
        raise SkillContractError("frontmatter 只有开始标记 `---`，没有结束标记")

    raw_frontmatter = text[len(_FRONTMATTER_DELIMITER) : closing]
    body = text[closing + len(_FRONTMATTER_DELIMITER) + 1 :]

    fields: dict[str, str] = {}
    for offset, line in enumerate(raw_frontmatter.splitlines(), start=2):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            raise SkillContractError(
                f"frontmatter 第 {offset} 行是注释；本模块不引入 YAML 依赖，只支持 `key: value`"
            )
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillContractError(f"frontmatter 第 {offset} 行缺少 `:`：{line.strip()!r}")
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise SkillContractError(f"frontmatter 第 {offset} 行的键或值为空：{line.strip()!r}")
        if key in fields:
            raise SkillContractError(f"frontmatter 里 `{key}` 重复出现")
        for fragment in _FORBIDDEN_VALUE_FRAGMENTS:
            if fragment in value:
                raise SkillContractError(
                    f"frontmatter 的 `{key}` 里含 {fragment!r}。"
                    "这种写法在 YAML 里会被截断或当成注释，请改写措辞"
                )
        if value.startswith(_FORBIDDEN_VALUE_PREFIXES):
            raise SkillContractError(
                f"frontmatter 的 `{key}` 以 {value[0]!r} 开头；这类引导符在 YAML 里另有含义，"
                "请改写措辞"
            )
        fields[key] = value

    return fields, body


def extract_category_enum(body: str) -> tuple[str, ...]:
    """从 SKILL.md 的 JSON 骨架里取出 `category` 的枚举值。

    模型的 `category` 必须落在 `DIMENSION_IDS` 里，而它只能从 SKILL.md 得知这个集合。
    两边一旦不同步，模型就会输出服务端不认的类别、异常被静默丢弃 —— 所以这里读回来
    比对。取不到就抛错，不静默返回空。
    """
    match = re.search(r'"category"\s*:\s*"([^"]+)"', body)
    if not match:
        raise SkillContractError('SKILL.md 里找不到形如 `"category": "a | b | c"` 的枚举定义')
    return tuple(item.strip() for item in match.group(1).split("|") if item.strip())


def extract_report_sections(body: str) -> tuple[str, ...]:
    """从 SKILL.md 的报告结构代码块里取出固定的一级标题。

    降级判定要按这些标题数「这份回答像不像一份报告」，所以标题集合必须与运行期一致。
    """
    # 找同时出现「报告结构」与全部标题的最小代码块，避免误取正文里别处的 `# `。
    for block in re.findall(r"```[a-zA-Z]*\n(.*?)```", body, re.S):
        headings = tuple(
            line.lstrip("#").strip()
            for line in block.splitlines()
            if line.startswith("# ")
        )
        if headings and set(REPORT_SECTIONS).issubset(set(headings)):
            return headings
    raise SkillContractError("SKILL.md 里找不到包含全部报告章节的一级标题清单")


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _check_reference_file(path: Path, problems: list[str]) -> None:
    text = path.read_text(encoding="utf-8")
    if _line_count(text) > REFERENCE_TOC_THRESHOLD_LINES and "目录" not in text:
        problems.append(
            f"{path.name} 超过 {REFERENCE_TOC_THRESHOLD_LINES} 行却没有目录"
            "（skill-creator 要求长 reference 带 TOC）"
        )


def validate_skill_dir(
    skill_dir: Path,
    *,
    require_body_contract: bool = True,
    require_name_matches_dir: bool = True,
) -> list[str]:
    """校验一个 skill 目录，返回问题列表（空列表 = 通过）。

    两个开关对应两种形态：

    * `require_body_contract`：是否要求正文里定义检查维度与报告结构。只有**平台内置
      skill** 需要（它是这些契约的载体）；项目知识包不需要。
    * `require_name_matches_dir`：是否要求 `name` 等于目录名。这条是为了 `.skill`
      打包产物命名一致（`package_skill.py` 用目录名做文件名），**只对平台内置 skill
      有意义**；项目目录名由项目代号决定，不该反过来约束 frontmatter。
    """
    problems: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    manifest = skill_dir / PROJECT_PACK_MANIFEST
    front_matter_file = skill_md if skill_md.is_file() else manifest
    if not front_matter_file.is_file():
        return [f"{skill_dir} 下既没有 SKILL.md 也没有 {PROJECT_PACK_MANIFEST}"]

    try:
        fields, body = parse_frontmatter(front_matter_file.read_text(encoding="utf-8"))
    except SkillContractError as exc:
        return [f"{front_matter_file.name}: {exc}"]

    unknown = sorted(set(fields) - ALLOWED_FRONTMATTER_KEYS)
    if unknown:
        problems.append(
            f"frontmatter 出现未允许的键 {', '.join(unknown)}；"
            f"只允许 {', '.join(sorted(ALLOWED_FRONTMATTER_KEYS))}"
        )

    for key in REQUIRED_FRONTMATTER_KEYS:
        if not fields.get(key):
            problems.append(f"frontmatter 缺少必需的 `{key}`")

    name = fields.get("name", "")
    if name:
        if not SKILL_NAME_RE.match(name):
            problems.append(f"name {name!r} 必须是 kebab-case（只含小写字母、数字、连字符）")
        elif name.startswith("-") or name.endswith("-") or "--" in name:
            problems.append(f"name {name!r} 不能以连字符开头或结尾，也不能有连续连字符")
        if len(name) > SKILL_NAME_MAX_CHARS:
            problems.append(f"name 过长（{len(name)} 字符，上限 {SKILL_NAME_MAX_CHARS}）")
        if require_name_matches_dir and name != skill_dir.name:
            problems.append(f"name {name!r} 与目录名 {skill_dir.name!r} 不一致（打包产物按目录名命名）")

    description = fields.get("description", "")
    if description:
        if "<" in description or ">" in description:
            problems.append("description 不能含尖括号")
        if len(description) > DESCRIPTION_MAX_CHARS:
            problems.append(
                f"description 过长（{len(description)} 字符，上限 {DESCRIPTION_MAX_CHARS}）"
            )

    compatibility = fields.get("compatibility", "")
    if len(compatibility) > COMPATIBILITY_MAX_CHARS:
        problems.append(
            f"compatibility 过长（{len(compatibility)} 字符，上限 {COMPATIBILITY_MAX_CHARS}）"
        )

    if _line_count(front_matter_file.read_text(encoding="utf-8")) > SKILL_MD_MAX_LINES:
        problems.append(f"{front_matter_file.name} 超过 {SKILL_MD_MAX_LINES} 行，应拆到 references/")

    if require_body_contract:
        problems.extend(_check_body_contract(body))

    problems.extend(_check_references(skill_dir, body))
    return problems


def _check_body_contract(body: str) -> list[str]:
    problems: list[str] = []
    try:
        declared = extract_category_enum(body)
    except SkillContractError as exc:
        problems.append(str(exc))
    else:
        if set(declared) != set(DIMENSION_IDS):
            problems.append(
                "SKILL.md 里 category 的枚举与运行期的 DIMENSION_IDS 不一致："
                f"文档写的是 {sorted(declared)}，代码要求 {sorted(DIMENSION_IDS)}"
            )

    try:
        sections = extract_report_sections(body)
    except SkillContractError as exc:
        problems.append(str(exc))
    else:
        if tuple(sections) != REPORT_SECTIONS:
            problems.append(
                "SKILL.md 里的报告章节与运行期的 REPORT_SECTIONS 不一致："
                f"文档写的是 {list(sections)}，代码要求 {list(REPORT_SECTIONS)}"
            )
    return problems


def _check_references(skill_dir: Path, body: str) -> list[str]:
    """references/ 里的文件与正文里列出的文件名必须双向一致。

    单向检查不够：正文列了但文件不存在 → 模型索要一个读不到的文档；文件存在但正文
    没列 → 那份文档永远不会被读到（渐进式披露的入口就是正文里那张表）。
    """
    problems: list[str] = []
    references_dir = skill_dir / "references"
    on_disk = (
        sorted(path.name for path in references_dir.glob("*.md")) if references_dir.is_dir() else []
    )
    mentioned = sorted(_mentioned_markdown_files(body))

    for name in on_disk:
        _check_reference_file(references_dir / name, problems)

    for name in sorted(set(mentioned) - set(on_disk)):
        problems.append(f"正文里提到了 `{name}`，但 references/ 下没有这个文件")
    for name in sorted(set(on_disk) - set(mentioned)):
        problems.append(f"references/{name} 没有被正文提到，模型不会知道它可读")
    return problems


def _mentioned_markdown_files(body: str) -> set[str]:
    """取出正文里以行内代码形式提到的 .md 文件名（取 basename）。

    **必须先剥掉代码围栏**：围栏是三个反引号，朴素的 `` `([^`]+)` `` 会把「围栏的第 3 个
    反引号」和「闭合围栏的第 1 个反引号」配成一对，于是**其后所有行内代码的配对整体
    错位**，`.md` 文件名会落进跨界的片段里而提取不到。这个坑实际踩到过——含一段
    ```json 骨架的 SKILL.md 会让全部 references 都被判成「没被正文提到」。

    取 basename 而不是整段：文档里自然写的是 `references/foo.md` 这种带路径的形式。
    """
    without_fences = re.sub(r"```.*?```", "", body, flags=re.S)
    return {
        Path(span).name
        for span in re.findall(r"`([^`]+)`", without_fences)
        if span.strip().endswith(".md")
    }


def validate_project_pack(pack_dir: Path) -> list[str]:
    """校验一个项目知识包目录。

    项目包不定义检查维度与报告结构（那是平台内置 skill 的职责），也不要求 `name`
    与目录名一致（目录名由项目代号决定）。
    """
    problems = validate_skill_dir(
        pack_dir, require_body_contract=False, require_name_matches_dir=False
    )
    if problems:
        return problems
    # 项目包里除了扁平知识，还可以有用户新建/上传的子 skill（每个是一个含 SKILL.md
    # 的文件夹）。它们同样要走契约校验 —— 否则用户可以塞进任何东西，而平台会把它
    # 注入提示词。
    for child in sorted(path for path in pack_dir.glob("*") if path.is_dir()):
        if child.name == "references":
            continue
        if (child / "SKILL.md").is_file():
            problems.extend(
                f"{child.name}/：{item}"
                for item in validate_skill_dir(
                    child, require_body_contract=False, require_name_matches_dir=False
                )
            )
    return problems


def validate_all(root: Path) -> dict[str, list[str]]:
    """校验平台 skill 与全部项目知识包，返回 {相对路径: 问题列表}（只含失败的）。"""
    failures: dict[str, list[str]] = {}

    platform_dir = root / PLATFORM_SKILL_RELATIVE_PATH
    problems = validate_skill_dir(platform_dir)
    if problems:
        failures[PLATFORM_SKILL_RELATIVE_PATH] = problems

    packs_root = root / PROJECT_PACKS_RELATIVE_PATH
    for pack_dir in sorted(path for path in packs_root.glob("*") if path.is_dir()):
        problems = validate_project_pack(pack_dir)
        if problems:
            failures[f"{PROJECT_PACKS_RELATIVE_PATH}/{pack_dir.name}"] = problems

    return failures


def iter_reference_files(skill_dir: Path) -> Iterable[Path]:
    """列出 skill 的 references/ 下的全部 md。"""
    references_dir = skill_dir / "references"
    if not references_dir.is_dir():
        return ()
    return tuple(sorted(references_dir.glob("*.md")))
