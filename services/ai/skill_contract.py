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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

# 平台内置 skill 的父目录：直接挂在它下面的**每个子目录**都是一个平台 skill
# （`projects/` 除外 —— 那是项目知识包）。平台 skill 不随项目走，`load_skills`
# **无条件**把它们的正文注入每一次分析的系统提示词。
PLATFORM_SKILLS_RELATIVE_PATH = "skills"
# 承载「检查维度 + 报告结构」契约的那一份平台 skill。
#
# **只有它**要求正文里写出那几组枚举（`_check_body_contract`）：那份文档是**报告格式**
# 的载体，运行期常量以它为准。别的平台 skill 是各自领域的方法论（例如资产发放安全审查），
# 它们不该被要求复述版本评审的报告章节 —— 那只会逼它们抄一份不属于自己的清单，
# 而抄出来的那份一旦与代码分叉就是静默的错。
BODY_CONTRACT_SKILL_NAME = "version-diff-review"
# 平台内置 skill 的位置（仓库根相对）。**历史调用方按这个常量找承载契约的那一份**；
# 「全部平台 skill」要用 `iter_platform_skill_dirs()`。
PLATFORM_SKILL_RELATIVE_PATH = f"{PLATFORM_SKILLS_RELATIVE_PATH}/{BODY_CONTRACT_SKILL_NAME}"
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

# 平台出厂默认的检查维度：id + 报告里显示的中文名。
#
# **「id」与「中文名」必须成对放在一起。** 它们原先分居两处（id 在本模块、中文名在
# `report_document.DIMENSION_LABELS`），加一个维度要改两处、漏一处就出现「报告里那一格
# 显示的是英文 id」——而英文 id 看起来完全正常（它就是个正经标识符），不会有人发现。
# 现在这里是唯一一份，`report_document` 与项目声明都从它派生。
#
# 排列是有意的：`config_id`（标识符）→ `config_value`（取值边界）→ `config_data`
# （这一行/这一格自己是否说得通）→ `value_sanity`（这个值放在这个系统里合不合理）
# 是**同一张表由细到整**的四层，一层比一层往外；`module_coupling`
# 放在 `config_linkage` 之后，因为两者是同一族问题的两个尺度 —— `config_linkage`
# 看**一张表**改动的连锁，`module_coupling` 看**模块之间**的耦合。
# **顺序还有第二个用途**：子代理模式按这个顺序把维度**相邻地**切给各成员
# （`subagent.group_dimensions`）—— 相邻即相关，所以改这个顺序会改变分工。
#
# `config_data` 是 2026-09-18 加的（用户要求「重点分析配置数据是否有问题」）：配表 diff
# 里最常见的问题不在标识符也不在数值边界，而在**数据自己说不通** —— 只改了一列文案、
# 描述与取值互相打架、必填列漏填、类型/格式不合法、复制粘贴出来的重复行。这些原先散在
# `config_value` 的「数值」口径之外，没有归属。
#
# `value_sanity` 是同一天接着加的（用户要求「根据每种系统或玩法的配置，判断这个值是否
# 合理，高风险的值要报出来」）：前三层都在问「这个值写得对不对」，这一层问的是
# **这个值放在这个系统里合不合理** —— 量级突变（道具价值 1000 → 1000000）、
# 经济闭环被打破（售价 100 的东西卖给商店能卖 10000）、单位量纲、与本系统其它档位的
# 比例。它可以是格式完全正确、行内也自洽的一个数，而数量级错一位就是线上事故。
@dataclass(frozen=True)
class DimensionSpec:
    """一个检查维度：运行期用的 `id` + 给人看的中文名 `label`。"""

    id: str
    label: str


DEFAULT_DIMENSION_SPECS: tuple[DimensionSpec, ...] = (
    DimensionSpec("config_id", "配置 ID"),
    DimensionSpec("config_value", "配置取值"),
    DimensionSpec("config_data", "配置数据本身"),
    DimensionSpec("value_sanity", "数值是否合理"),
    DimensionSpec("config_linkage", "单表连锁"),
    DimensionSpec("module_coupling", "模块耦合"),
    DimensionSpec("code_logic", "代码逻辑"),
    DimensionSpec("version_branch", "版本分支"),
    DimensionSpec("process", "流程"),
)

# 平台出厂默认的 id 序列。**这是「项目没声明时」的那一份**，也是 SKILL.md 正文里枚举的
# 那一份（`_check_body_contract` 按它校验文档）。项目可以在自己的知识包里声明自己的
# 维度清单，那时本次生效的清单来自声明，而不是这里 —— 见 `LoadedSkills.dimensions`。
#
# 保留这个元组（而不是把常量拆到各处）是刻意的：文档校验、默认值、以及「声明与自己
# 相同」的判断都需要一份确定的出厂值。
DIMENSION_IDS = tuple(spec.id for spec in DEFAULT_DIMENSION_SPECS)

# id → 中文名的出厂映射。`report_document` 的导出文档用它把 category 翻成人话。
DIMENSION_LABELS: dict[str, str] = {spec.id: spec.label for spec in DEFAULT_DIMENSION_SPECS}

# 「不在本次生效的维度清单内」这一组的名字。**它是一个显式的分组，而不是「显示不出来」**：
# 平台对落不进清单的条目一律**保留**（见 `protocol._coerce_anomalies`），再用这个名字
# 把它们单独列出来 —— 少了一条发现，读报告的人必须看得出来。
UNCLASSIFIED_LABEL = "未归类"

# 声明里的 id 形状：小写字母开头，只含小写字母、数字、下划线。**比 `Config_ID` 这种写法
# 直接判非法**而不是悄悄折成小写：折了之后声明里的 `Config_ID` 与模型写的 `config_id`
# 看起来是同一个，而报告里显示的又是声明原样，三方对不上时没人查得出来。
DECLARED_DIMENSION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# 声明里一项的形状：`id=中文名`。用**第一个** `=` 切分，所以中文名里可以带 `=`。
DECLARED_DIMENSION_SEPARATOR = "="
# 中文名上限。它是给人看的一格表格文字，超长会把报告的维度列撑坏。
DECLARED_DIMENSION_LABEL_MAX_CHARS = 24
# 一份声明最多几个维度。每个维度都会出现在提示词、成员任务书、报告与导出文档里，
# 声明一份几十项的清单是误用（而且每个维度都是一份要花钱看的职责）。
MAX_DECLARED_DIMENSIONS = 24


def build_dimensions(items: Sequence[str]) -> tuple[tuple[DimensionSpec, ...], str]:
    """把一串 `id=中文名` 声明项校验成维度清单。

    返回 `(清单, 问题)`：问题非空时清单是**空元组**，调用方据此回落到平台默认值，
    并**必须**把问题交出去（见 `project_facts.dimensions` 的 warning）—— 一份坏掉的声明
    悄悄退化成默认值，跟「项目本来就没声明」在行为上一模一样。

    **顺序按声明原样保留**，因为它是有意义的：子代理模式按这个顺序把维度相邻地切给
    各成员，所以顺序就是「哪些维度该被同一个人看」。
    """
    specs: list[DimensionSpec] = []
    problems: list[str] = []
    seen: set[str] = set()

    for raw in items:
        text = str(raw or "").strip()
        if not text:
            continue
        identifier, separator, label = text.partition(DECLARED_DIMENSION_SEPARATOR)
        identifier, label = identifier.strip(), label.strip()
        if not separator or not identifier or not label:
            problems.append(f"`{text}` 不是 `id=中文名` 的形状")
            continue
        if not DECLARED_DIMENSION_ID_RE.match(identifier):
            problems.append(
                f"`{identifier}` 不是合法的维度 id（要求小写字母开头，"
                "只含小写字母、数字、下划线，且不超过 40 字符）"
            )
            continue
        if len(label) > DECLARED_DIMENSION_LABEL_MAX_CHARS:
            problems.append(
                f"`{identifier}` 的中文名超过 {DECLARED_DIMENSION_LABEL_MAX_CHARS} 字"
            )
            continue
        if identifier in seen:
            # 重复的 id 会让同一个维度被切给两个成员（顺序里有两条同名项），
            # 于是两边各看一半、报告里出现两行同名维度。
            problems.append(f"`{identifier}` 重复出现")
            continue
        seen.add(identifier)
        specs.append(DimensionSpec(id=identifier, label=label))

    if len(specs) > MAX_DECLARED_DIMENSIONS:
        problems.append(f"维度数量超过上限（{MAX_DECLARED_DIMENSIONS} 个）")
    if problems:
        return (), "；".join(problems)
    if not specs:
        return (), "没有解析出任何维度"
    return tuple(specs), ""


def dimension_ids_of(specs: Iterable[DimensionSpec]) -> tuple[str, ...]:
    return tuple(spec.id for spec in specs)


def dimension_labels_of(specs: Iterable[DimensionSpec]) -> dict[str, str]:
    return {spec.id: spec.label for spec in specs}


def is_platform_default_dimensions(specs: Sequence[DimensionSpec]) -> bool:
    """这份清单是否与平台出厂默认**逐字相同**（id 与顺序都一样）。

    它的用途是「要不要往提示词里追加一段项目清单」：与出厂默认相同时追加只是白花
    提示词预算（模型看到的清单没变），而不同时必须追加，否则模型会按正文里那九个写。
    """
    return dimension_ids_of(specs) == DIMENSION_IDS


def render_dimension_section(specs: Sequence[DimensionSpec]) -> str:
    """把「本项目适用的维度清单」渲染成一段给模型看的正文。

    ## 为什么需要它（以及为什么它必须与校验用同一份）

    平台 SKILL.md 正文里逐条展开的是**出厂默认的九个维度**，而 `skill_contract` 要求
    正文枚举与运行期常量一致 —— 那一份是平台出厂值，换项目不变。项目在自己的知识包里
    声明了自己的维度清单时，**只有这一段能告诉模型换成哪些**。

    所以它由 `skill_loader.load_skills` 在读声明之后拼进**进提示词的那一份正文**里
    （紧跟在 SKILL.md 正文之后），与 `LoadedSkills.dimensions` 是同一个来源、同一份
    对象 —— 校验、分工、任务书、报告分组读的都是它，不存在「校验按 A、提示词按 B」。
    """
    lines = [
        "## 本项目适用的维度清单（**以本节为准**）",
        "",
        "上面正文里逐条展开的那九个维度是**平台出厂默认**。本项目在自己的知识包"
        "（`references/project-facts.md`）里声明了自己的维度清单，**本次分析生效的是下面"
        "这一份**，不是上面那一份。",
        "",
    ]
    for index, spec in enumerate(specs, start=1):
        lines.append(f"{index}. `{spec.id}` —— {spec.label}")
    lines.extend(
        [
            "",
            "三条纪律：",
            "",
            "- `anomalies[].category` 与 `dimensions[].id` **只能**写上面这几个 id。"
            "写别的 id 平台**不会丢弃**那条发现，但会把它归到「未归类」里单独列出来 ——"
            "那等于这条发现没有人认领，所以别这么写。",
            "- `final` 里的 `dimensions` 必须把上面这几个维度**逐一**留痕（没命中的写 "
            "`hit: false` 并说明理由），一个都不能空着。",
            "- **顺序是有意的**：顺序相邻的维度在语义上相关，平台按这个顺序把维度分给"
            "分片代理（相邻的几个会落在同一个分片身上）。",
        ]
    )
    return "\n".join(lines)


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

# 模型可以索要的上下文类型。**必须与 SKILL.md 里给模型看的清单逐字一致**：
# 文档里写了而运行期不认的类型，模型会一直请求、一直被丢弃，看起来像「模型不听话」，
# 实际是两边没对齐。
#
# `evidence` 是 E5 那个共享证据仓的**按地址取回**（任务书里 `@evidence_id=…（原文 N 字，
# 按需索取）`）。它 2026-09-22 才补进这份契约：在此之前它只活在 SKILL.md 的**正文**里
# （因为提取器会逐字比对，文档里加一行 JSON 就会让契约校验红），于是**契约校验看不见
# 这个类型** —— 协议侧只能靠 `ALLOWED_REQUEST_TYPES = (*REQUEST_TYPES, …)` 自己拼一份
# 绕过去。绕过去的那段时间里，「文档写了、服务端不认」这条最古老的病正好被重新引入：
# 只要有人把这个类型从 `protocol` 里挪走，没有任何一条校验会说话。
REQUEST_TYPES = (
    "commit_detail",
    "file_diff",
    "file_content",
    "read_reference",
    "find_references",
    "evidence",
)

# 异常条目只接受这两档严重度与置信度。更低的置信度按契约只能写进报告正文，
# 不该出现在给人工跟进的清单里。
SEVERITIES = ("critical", "high")
CONFIDENCES = ("high", "very_high")

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


def _extract_string_enum(body: str, key: str) -> tuple[str, ...]:
    """取出形如 `"key": "a | b | c"` 的枚举定义。"""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"', body)
    if not match:
        raise SkillContractError(f'SKILL.md 里找不到形如 `"{key}": "a | b | c"` 的枚举定义')
    return tuple(item.strip() for item in match.group(1).split("|") if item.strip())


def extract_category_enum(body: str) -> tuple[str, ...]:
    """从 SKILL.md 的 JSON 骨架里取出 `category` 的枚举值。

    模型的 `category` 必须落在 `DIMENSION_IDS` 里，而它只能从 SKILL.md 得知这个集合。
    两边一旦不同步，模型就会输出服务端不认的类别、异常被静默丢弃 —— 所以这里读回来
    比对。取不到就抛错，不静默返回空。
    """
    return _extract_string_enum(body, "category")


def extract_request_types(body: str) -> tuple[str, ...]:
    """取出 SKILL.md 里给模型看的可索要上下文类型。

    它们在文档里是四个独立的 JSON 对象（不是 `|` 枚举），所以按 `"type": "X"` 逐个抓。
    """
    found = re.findall(r'"type"\s*:\s*"([A-Za-z_]+)"', body)
    if not found:
        raise SkillContractError('SKILL.md 里找不到形如 `"type": "file_diff"` 的请求类型定义')
    ordered: list[str] = []
    for item in found:
        if item not in ordered:
            ordered.append(item)
    return tuple(ordered)


def extract_severity_enum(body: str) -> tuple[str, ...]:
    return _extract_string_enum(body, "severity")


def extract_confidence_enum(body: str) -> tuple[str, ...]:
    return _extract_string_enum(body, "confidence")


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
    """正文里给模型看的每一个枚举，都必须等于运行期用的那一组。

    这四组枚举不同步的后果都是**静默**的：模型输出服务端不认的值 → 那一条被丢弃，
    而报告里看不出少了东西；反过来文档里少写了一项 → 模型永远不会用那种类型请求，
    该读的上下文读不到。

    ## `category` 这一项比的是**平台出厂默认**，这是对的

    `DIMENSION_IDS` 是出厂值，而 SKILL.md 就是出厂的那份 skill（它随平台发版，
    与任何项目无关）。项目声明了自己的维度清单时，生效清单来自声明 —— 那一段由
    `skill_loader` 追加进**进提示词的正文**，不落盘，所以不影响这里。

    这里**不能**改成「文档必须等于某个项目的清单」：文档是平台级的，它没有项目。
    也**不能**因为「落不进清单的条目现在不丢了」就删掉这条校验 —— 它防的是
    「文档与服务端对不上」，而那个方向（模型按文档写、平台按别的口径理解）依然存在。
    """
    problems: list[str] = []

    checks = (
        ("category", extract_category_enum, DIMENSION_IDS),
        ("请求类型", extract_request_types, REQUEST_TYPES),
        ("severity", extract_severity_enum, SEVERITIES),
        ("confidence", extract_confidence_enum, CONFIDENCES),
    )
    for label, extractor, expected in checks:
        try:
            declared = extractor(body)
        except SkillContractError as exc:
            problems.append(str(exc))
            continue
        if set(declared) != set(expected):
            problems.append(
                f"SKILL.md 里的 {label} 与运行期常量不一致："
                f"文档写的是 {sorted(declared)}，代码要求 {sorted(expected)}"
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
    """校验全部平台 skill 与全部项目知识包，返回 {相对路径: 问题列表}（只含失败的）。"""
    failures: dict[str, list[str]] = {}

    for skill_dir in iter_platform_skill_dirs(root):
        rel_path = f"{PLATFORM_SKILLS_RELATIVE_PATH}/{skill_dir.name}"
        # 正文契约只对承载报告格式的那一份成立，见 `BODY_CONTRACT_SKILL_NAME`。
        # `name` 必须等于目录名这一条对**所有**平台 skill 都成立（打包产物按目录名命名）。
        problems = validate_skill_dir(
            skill_dir, require_body_contract=skill_dir.name == BODY_CONTRACT_SKILL_NAME
        )
        if problems:
            failures[rel_path] = problems

    packs_root = root / PROJECT_PACKS_RELATIVE_PATH
    for pack_dir in sorted(path for path in packs_root.glob("*") if path.is_dir()):
        problems = validate_project_pack(pack_dir)
        if problems:
            failures[f"{PROJECT_PACKS_RELATIVE_PATH}/{pack_dir.name}"] = problems

    return failures


def iter_platform_skill_dirs(root: Path) -> list[Path]:
    """列出全部平台 skill 目录，**承载契约的那一份排在最前**。

    顺序是确定的（其余按目录名排序）。这**不是**整洁癖：平台 skill 的正文会按这个顺序
    拼进系统提示词，而提示词要靠前缀命中缓存 —— 顺序随文件系统的返回顺序变的话，
    同样的内容会算出不同的前缀，每次分析都从零开始付全价。

    `skills/projects/` 被排除：它装的是项目知识包，随 `project_code` 寻址，不是平台 skill。
    """
    platform_root = root / PLATFORM_SKILLS_RELATIVE_PATH
    if not platform_root.is_dir():
        return []
    packs_name = Path(PROJECT_PACKS_RELATIVE_PATH).name
    found = [
        path
        for path in sorted(platform_root.glob("*"))
        if path.is_dir() and path.name != packs_name
    ]
    contract = [path for path in found if path.name == BODY_CONTRACT_SKILL_NAME]
    others = [path for path in found if path.name != BODY_CONTRACT_SKILL_NAME]
    return contract + others


def iter_reference_files(skill_dir: Path) -> Iterable[Path]:
    """列出 skill 的 references/ 下的全部 md。"""
    references_dir = skill_dir / "references"
    if not references_dir.is_dir():
        return ()
    return tuple(sorted(references_dir.glob("*.md")))
