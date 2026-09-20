"""项目专属知识包（`skills/projects/<slug>/`）的读 / 写 / 删。

## 为什么单独一个模块

在这之前，`skills/projects/<项目代号>/` 这一包东西**只能靠人往服务器上放文件**：
`skill_loader` 只会读，`skill_contract` 只会校验，没有任何写入口。运维要改一份
项目知识文档，得先 SSH 上去、再手敲 `frontmatter` —— 而 `frontmatter` 的语法比看起来
严得多（`description` 里出现 `": "` 就会被 YAML 截断，所以 `skill_contract` 直接判非法）。

这里补的就是那条写路径，并且**把契约校验当成写入的闸门**：先在一份影子目录上落盘、
跑 `validate_project_pack`，通过了才动真目录。

## 三条设计约束

1. **校验只有一份实现**。「什么算合法」全部来自 `services/ai/skill_contract.py`，
   本模块不重复实现任何一条规则，只做两件它不做的事：**归因**（把问题落到界面上某一栏）
   与**写入编排**（影子目录 → 校验 → 落地）。另写一套判定的代价是两边迟早不一致，
   于是「界面上存得进去、分析时加载报错」这种最难查的故障就会出现。

2. **绝不写 pack 目录之外的任何位置**。`skills/version-diff-review/` 是平台内置 skill，
   它由平台维护、随仓库分发；从这套接口写进去等于让项目管理员能改所有项目的评审规程。
   路径一律 `safe_join(projects_root, ...)`，且 `projects_root` 与加载器读的**同一个**
   `resolve_projects_root()` —— 写进去的东西必须真的会被加载到，否则界面上显示
   「已保存」而分析时读的是别处。

3. **校验不通过时磁盘上不留半截状态**。见 `_apply_with_validation`。

## 为什么不复用 `services/ai_analysis_service.py`

那个模块是分析编排（跑模型、攒提示词、写 run 记录），导入它会拉起一大串运行期依赖；
而「改一个 markdown 文件」不该需要这些。本模块只依赖 `skill_loader` 与 `skill_contract`。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from services.ai.endpoint_service import ConfigValidationError, FieldError
from services.ai.skill_contract import (
    DESCRIPTION_MAX_CHARS,
    PLATFORM_SKILL_RELATIVE_PATH,
    PROJECT_PACK_MANIFEST,
    SKILL_MD_MAX_LINES,
    SKILL_NAME_MAX_CHARS,
    SKILL_NAME_RE,
    _mentioned_markdown_files,
    validate_project_pack,
)
from services.ai.skill_loader import (
    SkillLoadError,
    project_pack_slug,
    resolve_projects_root,
    safe_join,
)
from utils.timezone_utils import BEIJING_TZ

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# 额度
# ---------------------------------------------------------------------------
#
# 单文件 64 KB：reference 会被按需读进提示词，而提示词的字符预算默认只有几万字。
# 一份 64 KB 的中文文档约 2 万字，已经占掉预算的一大半 —— 再大就必然被压缩，
# 用户拿到的是「我明明写了，模型却说没看到」。所以上限既是防滥用，也是防误用。
MAX_FILE_BYTES = 64 * 1024
# 整包 512 KB：一包十几份文档、外加若干子 skill 的合理规模。
# 全部文档都会被哈希进 `skill_revision`，也是分析开始时要遍历的东西。
MAX_PACK_BYTES = 512 * 1024

# 知识包里的三类内容。`kind` 是接口与界面共用的枚举，**不要随手加第四种**：
# 每加一种就要在 `describe_pack` / 读写 / 界面分组三处同步。
KIND_MANIFEST = "manifest"
KIND_REFERENCE = "reference"
KIND_SKILL = "skill"
KINDS = (KIND_MANIFEST, KIND_REFERENCE, KIND_SKILL)

# 子 skill 的固定文件名与知识文档目录，与 `skill_contract` / `skill_loader` 保持一致。
SKILL_MD_NAME = "SKILL.md"
REFERENCES_DIR_NAME = "references"
_MD_SUFFIX = ".md"

# 文件名白名单。**这是本模块最重要的一行**。
#
# 与 `SKILL_NAME_RE` 同源（`^[a-z0-9-]+$`）：小写字母、数字、连字符，仅此三种。
# 它一次性挡掉的东西列表比看起来长：
#
#   * `/` 与 `\`（含 `..%2f` 这类先编码再被 Flask 解码出来的分隔符）—— 有分隔符就能
#     越过一层目录；
#   * `.` —— 于是 `..` / `...` / `a..b` 全部不可能出现，盘符（`C:`）也不可能（`:` 不在表里）；
#   * 空白、控制字符、全角空格、`‮` 之类的双向控制符 —— 文件名里带这些，
#     界面上显示的名字与实际落盘的名字会不一致，用户以为自己删除的是另一个文件；
#   * 大小写混写 —— 见 `normalize_reference_name` 与 `_check_segment` 的说明。
#
# **只允许小写**不是为了好看：Windows 与 macOS 的文件系统不区分大小写，`Foo.md` 与
# `foo.md` 在磁盘上是同一个文件，但在 `build_readable_index()` 里是两个不同的键 ——
# 于是「跨层重名直接报错」那道防线会在最需要它的平台上失效（平台 skill 里有
# `incident-checklist.md`、项目包里再有 `Incident-Checklist.md`，重名检查看不到冲突，
# 模型索要时拿到哪一份却是不确定的）。强制小写把这类歧义从源头去掉。
_NAME_SEGMENT_RE = SKILL_NAME_RE

# 子 skill 目录名与 reference 的文件名（去掉扩展名后）都走上面那条白名单。
_SKILL_DIR_MAX_CHARS = SKILL_NAME_MAX_CHARS

# Windows 的保留设备名。**带着扩展名也一样被保留**（`con.md` 同样建不出来），
# 所以这里按「词干」匹配。命中它的后果不是「名字不好看」，而是 `os.replace` 在
# Windows 上抛 OSError —— 用户看到的是一句和文件名毫无关系的系统错误。
_WINDOWS_RESERVED_RE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------
#
# ## 为什么有「空白模板」和「建包模板」两份，而不是一份
#
# `skill_contract._check_references()` 是**双向**的：正文里用行内代码提到的每个
# `.md` 都必须在 `references/` 下真实存在，反之亦然。这条规则有一个直接推论：
#
#     不存在任何一种「先加文档、再改清单」或「先改清单、再加文档」的顺序能成功 ——
#     两种顺序里总有一次保存会撞上双向校验。
#
# 所以两件事必须**在同一次写入里完成**，这就是下面两份模板存在的原因：
#
#   * `MANIFEST_TEMPLATE`（空白模板）——正文里**一个 `.md` 都不提**，可以单独保存。
#     自己手写清单的用户从这份开始，等真的建好文档再把文件名写进反引号。
#   * `SCAFFOLD_MANIFEST_TEMPLATE`（建包模板）——正文里提了两份起始文档，因此
#     它**只能**配合 `SCAFFOLD_REFERENCES` 一起写（`create_pack_from_template`）。
#
# 同理，`write_reference` 会在同一次写入里把引用行补进清单，`delete_reference` 会在
# 同一次写入里把那一行摘掉。用户不需要知道这条规则，但界面上会如实说明
# 「新建文档会同时在清单里加一行引用」，免得他以为清单被谁改过。

# 空白模板：正文里刻意不出现任何 `` `xxx.md` ``。
# **必须自带 frontmatter**：`parse_frontmatter` 要求 `---` 在第 0 列，而
# `_read_document(require_frontmatter=True)` 在缺 frontmatter 时直接抛
# `SkillLoadError` —— 那会让整次分析加载 skill 失败。模板不能是「空文件 + 提示语」。
MANIFEST_TEMPLATE = """---
name: {slug}
description: {slug} 项目的配表与玩法约定
---

# {slug} 项目知识包

这份文件是**知识包的入口**：模型先看到的是上面那句 `description`，
正文里用反引号写出的每份文档才会被按需读取。

## 这个项目是什么

- 技术栈与工程结构：<例如 Unity + C# + Lua>
- 配表在哪里、产物提交不提交：<写清楚，模型会据此判断哪些改动要一起看>

## 重点模块

- <改了要压测或完整回归的模块>

## 本项目的红线与历史高频事故

- <踩过的坑写在这里，模型会拿它当警示>

## 可读的知识文档

把 `references/` 下真实存在的文档用反引号列在下面（例如 ── 一行一个，
「文件名」加一句它管什么）。**正文里列了而没有文件，或者文件存在而这里没列，
保存时都会被拒绝** —— 前者让模型去索要一份读不到的文档，后者那份文档永远不会被读到。
"""

# 建包模板的正文里提了两份起始文档，与 `SCAFFOLD_REFERENCES` 一一对应。
#
# **这里只给形状，不给具体值**：`<…>` 里写的是「该填什么」，不是某个项目的真实
# 事实。历史教训是这两行写成过 `<例如 Unity + C# + Lua，配表在 qz_config 下>` /
# `<例如导表产物是 CfgXxx.lua>` —— `qz_config` 是平台第一个项目的**真实仓库名**、
# `CfgXxx.lua` 是它的**真实产物约定**。管理员新建项目时改掉了技术栈却留着这一行，
# 它就作为「项目事实」进了系统提示词，而 skill 明确告诉模型「事实以项目知识包为准」，
# 于是模型去找一个不存在的目录、或者把别的目录当成配表目录。
# 范本位置放具体值的代价不是「平台自动错」，而是「人会照抄」。
SCAFFOLD_MANIFEST_TEMPLATE = """---
name: {slug}
description: {slug} 项目的配表与玩法约定
---

# {slug} 项目知识包

这份文件是**知识包的入口**：模型先看到的是上面那句 `description`，
正文里用反引号写出的每份文档才会被按需读取。请把下面每一节换成这个项目的真实情况。

## 这个项目是什么

- 技术栈与工程结构：<语言 / 引擎 / 工程结构一句话；配表在哪个仓库或目录下>
- 配表的产物形态与是否提交：<产物文件名形如什么、要不要提交进版本库>

## 重点模块（改了要压测或完整回归）

- <一局流程的阶段划分、关键交互链>

## 本项目的红线与历史高频事故

- <踩过的坑写在这里，模型会拿它当警示>

## 可读的知识文档

模型判断「属于哪个系统」「按什么规则编号」时会来读下面这些文档：

- `config-table-spec.md` —— 配表规范：ID 段位、命名、分表原则
- `gameplay-semantics.md` —— 玩法语义：一局流程的阶段划分与交互链
"""

# 建包时一并落盘的起始文档。名字必须与 `SCAFFOLD_MANIFEST_TEMPLATE` 正文里
# 引用的文件名**逐字一致** —— 不一致就会被 `_check_references()` 拒绝，
# 于是「从模板创建」这个按钮永远点不动。
SCAFFOLD_REFERENCES: tuple[tuple[str, str], ...] = (
    (
        "config-table-spec",
        "把这一节换成这个项目的真实规范：ID 段位、命名规则、分表原则、已放出 ID 的处理。",
    ),
    (
        "gameplay-semantics",
        "把这一节换成这个项目的真实语义：核心流程的阶段划分、关键交互链、容易混淆的同名概念。",
    ),
)


def scaffold_reference_body(name: str, hint: str) -> str:
    return (
        f"# {name}\n\n"
        f"{hint}\n\n"
        "## 事实\n\n"
        "- <写「模型猜不到、但判断时必须知道」的事实>\n"
        "- <写清楚边界：哪些属于这个系统、哪些不属于>\n\n"
        "## 判断规则\n\n"
        "1. <看到什么现象，应当归到哪一类>\n"
        "2. <容易误判的地方，先排除什么>\n"
    )


# 新建子 skill 时的正文骨架。**不含 frontmatter**：frontmatter 由服务端按
# 「目录名 + 一句话说明」拼出来。让用户手写 frontmatter 是必然出错的 ——
# 它看起来像 YAML 但不是 YAML（本平台刻意不引入 PyYAML 依赖），
# 值里一个 `": "` 就会被判非法，而错误信息对策划来说完全不可读。
SKILL_BODY_TEMPLATE = """# {name}

<一句话说明这个 skill 解决什么问题。上面那句 description 是索引，
这里写展开的说明。>

## 什么时候适用

- <触发条件，例如「改动涉及地宫楼层配置时」>

## 该怎么做

1. <步骤>
2. <步骤>

## 这个项目的既定事实

- <把「模型猜不到、但判断时必须知道」的事实写在这里>
"""


# ---------------------------------------------------------------------------
# 请求级错误
# ---------------------------------------------------------------------------
#
# 一律走 `FieldError` / `ConfigValidationError` —— 与 `/config` 保存接口**同一个错误
# 形状**（`{field, label, message}` 列表）。界面因此只需要一套错误渲染逻辑：
# 顶部摘要列出全部（可点击跳到对应控件），能归到某一栏的再在那一栏下面标红。
# 另立一种形状的代价是前端要写第二套渲染，而第二套往往就是「只弹一个 toast」。


def _field_error(field: str, label: str, message: str) -> ConfigValidationError:
    return ConfigValidationError([FieldError(field, label, message)])


def _reject(field: str, label: str, message: str) -> ConfigValidationError:
    return _field_error(field, label, message)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackLocation:
    """一个项目的知识包在磁盘上的位置。"""

    project_code: str
    slug: str
    pack_dir: Path
    projects_root: Path

    @property
    def exists(self) -> bool:
        return self.pack_dir.is_dir()


def locate_pack(project_code: str, *, repo_root: Path | None = None) -> PackLocation:
    """把项目代号解析成知识包目录。**只解析，不创建**。

    `slug` 为空（项目代号是空的、或者整串都是非法字符）时直接拒绝：那时
    `safe_join(root, "")` 会指向 `projects/` 本身，往里写就等于把文件扔在所有
    项目的公共父目录下 —— 它不会被任何项目加载，却会被 `validate_all()` 当成
    一个叫不出名字的包。
    """
    root = resolve_projects_root(repo_root or _REPO_ROOT)
    slug = project_pack_slug(project_code)
    if not slug:
        raise _reject(
            "name",
            "项目代号",
            "这个项目没有可用的项目代号，无法定位它的知识包目录。请先在项目设置里填写项目代号。",
        )
    try:
        pack_dir = safe_join(root, slug)
    except SkillLoadError as exc:  # pragma: no cover - safe_join 对 slug 已不可能失败
        raise _reject("name", "项目代号", f"项目代号无法映射到合法目录：{exc}") from exc
    _reject_platform_target(pack_dir)
    return PackLocation(project_code=project_code, slug=slug, pack_dir=pack_dir, projects_root=root)


def _reject_platform_target(candidate: Path) -> None:
    """确认 candidate 不在平台内置 skill 目录里。

    这是「**绝不允许通过这套接口写 `skills/version-diff-review/`**」这句承诺的
    可执行形式。默认布局下它不可能命中（内置 skill 不在 `skills/projects/` 下），
    但 `SKILL_PROJECTS_ROOT` 是**可配的**：一旦有人把它配成 `skills`，那么
    代号恰好是 `version-diff-review` 的项目就会把包目录解析到内置 skill 上，
    而「包目录必须在自己内部」那条检查完全看不出问题 —— 它确实在自己内部。

    用 `relative_to()` 而不是字符串比较：后者会把 `skills/version-diff-review-old`
    误判成在内置目录里。
    """
    platform_dir = (_REPO_ROOT / PLATFORM_SKILL_RELATIVE_PATH).resolve()
    try:
        candidate.resolve().relative_to(platform_dir)
    except ValueError:
        return
    raise _reject(
        "name",
        "项目代号",
        "这个项目代号会映射到平台内置的评审规程目录上，拒绝操作。"
        "平台内置的评审规程由平台维护，不通过项目知识包维护。",
    )


# ---------------------------------------------------------------------------
# 名字校验（白名单）
# ---------------------------------------------------------------------------


def _check_segment(raw: str, *, label: str, max_chars: int = _SKILL_DIR_MAX_CHARS) -> str:
    """校验一个**单段**名字并返回它。任何不合法的输入都在这里被拒。

    **为什么不能靠前端校验**：前端的输入框限制只作用于「用户在界面上打字」这一条路径，
    而接口是可以直接调的（curl / 脚本 / 或者某个改坏了的页面）。这里的规则必须自己
    成立 —— 前端那层只是提前告诉用户，不承担安全职责。

    顺序也有讲究：先剥掉明显是路径的东西（含分隔符、绝对路径、`..`），再套白名单。
    白名单本身已经能挡住它们，但**分开报错**才能让用户看懂问题出在哪 ——
    「文件名里有 `/`」比「名字不合法」有用得多。
    """
    text = "" if raw is None else str(raw)
    if not text:
        raise _reject("name", label, f"{label}不能为空。")
    if text != text.strip():
        raise _reject("name", label, f"{label}首尾有空格，请去掉。")
    # 路径相关的一切先逐个点名 —— 这些是攻击面，也是用户最容易犯的错（从资源管理器
    # 复制路径时会带上完整路径）。
    if "/" in text or "\\" in text:
        raise _reject("name", label, f"{label}里不能含路径分隔符（`/` 或 `\\`），只能是一个名字。")
    if Path(text).is_absolute() or re.match(r"^[A-Za-z]:", text):
        raise _reject("name", label, f"{label}不能是绝对路径或盘符。")
    if text in {".", ".."} or ".." in text:
        raise _reject("name", label, f"{label}里不能出现 `..`。")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise _reject("name", label, f"{label}里不能含控制字符。")
    if len(text) > max_chars:
        raise _reject("name", label, f"{label}太长了（{len(text)} 字符，上限 {max_chars}）。")
    if _WINDOWS_RESERVED_RE.match(text):
        raise _reject(
            "name",
            label,
            f"{label}不能是 Windows 的保留设备名（{text!r}）。"
            "这类名字在 Windows 上创建会直接失败，而且失败得很晚（写到一半才报错）。",
        )
    if not _NAME_SEGMENT_RE.match(text):
        raise _reject(
            "name",
            label,
            f"{label}只能用小写字母、数字和连字符（当前是 {text!r}）。"
            "中文名、下划线、空格都不行 —— 这个名字会被写进磁盘路径。",
        )
    if text.startswith("-") or text.endswith("-") or "--" in text:
        raise _reject("name", label, f"{label}不能以连字符开头或结尾，也不能有连续连字符。")
    return text


def normalize_reference_name(raw: str) -> str:
    """把用户填的名字规范成「不带扩展名的词干」，并校验。

    允许两种写法：`config-table-spec` 与 `config-table-spec.md`。前者是界面上更自然的
    输入（用户不该关心扩展名），后者是从磁盘上抄下来的形态。**只接受 `.md`**：
    写成 `.txt` / `.markdown` / `.MD` 都直接拒绝 —— `.MD` 在大写不敏感的文件系统上
    会落成 `foo.MD`，而 `_collect_references()` 只 `glob("*.md")`，于是文件在磁盘上
    存在、在列表里看不见（`glob` 在 Windows 上也不区分大小写，但 Linux 上会漏），
    这种「看运气」的行为不该出现在部署产物里。
    """
    text = "" if raw is None else str(raw)
    if text.lower().endswith(_MD_SUFFIX):
        stem = text[: -len(_MD_SUFFIX)]
        # `foo.MD` / `foo.Md` 会被上面那行认成「带 .md」，但落盘时是另一个名字。
        # 这里按字面比对，只放行小写的 `.md`。
        if text != stem + _MD_SUFFIX:
            raise _reject("name", "文件名", "扩展名只能是小写的 `.md`。")
    elif "." in text:
        raise _reject("name", "文件名", "只允许 markdown 文档（扩展名 `.md`），其它扩展名不支持。")
    else:
        stem = text
    return _check_segment(stem, label="文件名")


def normalize_skill_name(raw: str) -> str:
    """校验子 skill 的目录名。

    目录名会同时成为 frontmatter 的 `name`（服务端拼的），所以它必须满足
    `SKILL_NAME_RE` —— 与 `SKILL_NAME_MAX_CHARS` 同一套限额。
    """
    return _check_segment(raw, label="子 skill 目录名")


# ---------------------------------------------------------------------------
# 大小校验
# ---------------------------------------------------------------------------


def _check_content_size(text: str, *, label: str, field: str = "content") -> None:
    size = len(text.encode("utf-8"))
    if size > MAX_FILE_BYTES:
        raise _reject(
            field,
            label,
            f"内容太长了（{size // 1024} KB，上限 {MAX_FILE_BYTES // 1024} KB）。"
            "文档会被按需读进提示词，超长会被压缩，模型反而看不到重点 —— 请拆成多份文档。",
        )


def _check_pack_size(pack_dir: Path, *, extra_bytes: int = 0) -> None:
    total = extra_bytes
    if pack_dir.is_dir():
        for path in pack_dir.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    if total > MAX_PACK_BYTES:
        raise _reject(
            "__pack__",
            "知识包",
            f"这个知识包的总大小会超过 {MAX_PACK_BYTES // 1024} KB（当前约 {total // 1024} KB）。"
            "全部文档都会被读进分析，请删掉不再需要的文档或拆分项目。",
        )


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------


def _atomic_write(target: Path, text: str) -> None:
    """把文本原子地写到 target。

    `os.replace` 在同一文件系统上是原子的：读侧要么看到旧内容、要么看到新内容，
    **不会看到写了一半的文件**。落盘期间进程被杀也不会留下半截 —— 这正是
    `skill_loader` 每次分析都会重读这些文件、而分析可能并发进行所要求的。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except BaseException:
        # 失败时清掉临时文件：否则 pack 目录里会留下 `.foo.md.xxxx.tmp`，
        # 它会被 `validate_all()` 的目录遍历看到（虽然不参与加载），
        # 也会让用户以为「删除没删干净」。
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _apply_with_validation(pack_dir: Path, mutate, *, edited_rel: str) -> None:
    """先在一份**影子目录**上做改动并校验，通过了才动真目录。

    「先把内容写进临时目录再校验、通过后再原子替换」这句话落到实现上是两步：

    1. `shutil.copytree` 把整个 pack 复制到系统临时目录，把 `mutate` 应用在副本上，
       跑 `validate_project_pack`。**副本与真目录的目录结构完全一致**，所以校验看到的
       就是落地后的真实形态 —— 如果只把「要写的那个文件」单独拿去校验，
       `_check_references()` 那类**跨文件**规则（正文提到的文档必须真实存在、
       references 下的文档必须被正文提到）就全都看不到了，而那正是最容易写错的部分。
    2. 只有校验返回空列表，才把**同一个 `mutate`** 作用到真目录上。

    用同一个 `mutate` 而不是「校验时用一份逻辑、落地时再写一遍」，是为了让
    「校验过的结构」与「落盘的结构」不可能分叉 —— 两份逻辑迟早会分叉。

    代价是每次写都要复制一遍整个 pack。这是刻意的取舍：pack 的量级是几十 KB，
    复制它的开销远小于「校验通过但落地落成另一个样子」的排查成本。

    校验失败时抛 `ConfigValidationError`，真目录**一个字节都没被碰过**。
    """
    staged_parent = Path(tempfile.mkdtemp(prefix="pack-stage-"))
    try:
        staged_pack = staged_parent / pack_dir.name
        if pack_dir.is_dir():
            shutil.copytree(pack_dir, staged_pack)
        else:
            staged_pack.mkdir(parents=True, exist_ok=True)
        mutate(staged_pack)
        problems = validate_project_pack(staged_pack)
        if problems:
            raise ConfigValidationError(_attribute_problems(problems, edited_rel=edited_rel))
    finally:
        shutil.rmtree(staged_parent, ignore_errors=True)

    # 到这里说明校验通过了。真目录可能还不存在（首次创建知识包）。
    pack_dir.mkdir(parents=True, exist_ok=True)
    mutate(pack_dir)


# ---------------------------------------------------------------------------
# 归因：把校验器的问题文本落到界面上某一栏
# ---------------------------------------------------------------------------
#
# `skill_contract.validate_project_pack()` 回的是**字符串列表**（它是给人看的，
# 也是给 `validate_all()` 的 CLI 用的）。界面需要的却是「哪一栏错了」。
# 这一层**只做归因，不做判定** —— 它不决定谁合法，只决定把已经判定的问题摆在哪里。
#
# 归因规则（按可靠度从高到低）：
#   1. 问题里点名了某个文件 → 若正是编辑器里打开的那份，落到它的正文/说明栏；
#      否则落到「知识包」这一层，label 用那个文件名（让用户知道该去改哪一份）。
#   2. 问题在说 frontmatter 的某个键（`name` / `description`）→ 落到对应输入框。
#   3. 以上都认不出来 → 落到「知识包」这一层。宁可位置笼统，也不能把问题丢掉。

_INVOLVED_SUB_SKILL_RE = re.compile(r"^(?P<dir>[^/：:\s]+)/：")
_INVOLVED_REFERENCE_RE = re.compile(r"^references/(?P<name>[^\s，]+)")


def _involved_rel(problem: str) -> str | None:
    """从问题文本里认出它说的是哪一份文件（认不出来返回 None）。"""
    match = _INVOLVED_SUB_SKILL_RE.match(problem)
    if match:
        return f"{match.group('dir')}/{SKILL_MD_NAME}"
    match = _INVOLVED_REFERENCE_RE.match(problem)
    if match:
        return f"{REFERENCES_DIR_NAME}/{match.group('name')}"
    if problem.startswith("正文里提到了 "):
        # 「正文里提到了 `foo.md`，但 references/ 下没有这个文件」——
        # 缺的是 foo.md，所以要用户回到编辑器里改 KNOWLEDGE.md 或补上文档，
        # 这里按「正文」归因比按文件名归因更贴近用户要做的事。
        return None
    if problem.endswith("却没有目录（skill-creator 要求长 reference 带 TOC）"):
        # 校验器这条消息直接以文件名开头，没有目录前缀。
        return f"{REFERENCES_DIR_NAME}/{problem.split(' ', 1)[0]}"
    return None


def _is_about(front_matter_file_name: str, problem: str, edited_rel: str) -> bool:
    """这条问题说的是不是当前正在编辑的那份文件。"""
    if problem.startswith(f"{front_matter_file_name}: "):
        return True
    return bool(edited_rel) and problem.startswith(f"{edited_rel}: ")


def _attribute_problems(problems: list[str], *, edited_rel: str) -> list[FieldError]:
    edited_name = Path(edited_rel).name
    errors: list[FieldError] = []
    for problem in problems:
        errors.append(_attribute_one(problem, edited_rel=edited_rel, edited_name=edited_name))
    return errors


def _attribute_one(problem: str, *, edited_rel: str, edited_name: str) -> FieldError:
    involved = _involved_rel(problem)

    # 正在编辑的这份文件自身的问题：能落到具体输入框的就落进去。
    if involved is None or involved == edited_rel:
        if _is_about(edited_name, problem, edited_rel):
            # 形如「KNOWLEDGE.md: frontmatter 的 `description` 里含 ': '」——
            # 冒号后面那段才是用户要看的话。
            detail = problem.split(": ", 1)[1] if ": " in problem else problem
        else:
            detail = problem
        if "frontmatter" in detail:
            for key, field, label in (
                ("description", "description", "一句话说明（description）"),
                ("name", "name", "名字（name）"),
            ):
                if f"`{key}`" in detail:
                    return FieldError(field, label, detail)
        return FieldError("content", f"{edited_name} 正文", detail)

    # 说的是别的文件（或整个包）：归到包这一层，但 label 点明是哪一份。
    return FieldError("__pack__", involved, problem)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeEntry:
    """列表里的一行。"""

    kind: str
    name: str
    rel_path: str
    size: int
    modified_at: str
    usage: str
    # 能不能通过界面删除。清单文件为 False（删了整包就废了）。
    removable: bool
    # 子 skill 的目录名（kind == skill 时才有意义，删除时用它）。
    dir_name: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "rel_path": self.rel_path,
            "size": self.size,
            "modified_at": self.modified_at,
            "usage": self.usage,
            "removable": self.removable,
            "dir_name": self.dir_name,
        }


def _describe_file(path: Path) -> tuple[int, str]:
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=BEIJING_TZ)
    return stat.st_size, modified.strftime("%Y-%m-%d %H:%M")


def describe_pack(project_code: str, *, repo_root: Path | None = None) -> dict:
    """列出一个项目知识包的全部内容。**目录不存在不是错误**。

    「还没维护过自己的知识」是绝大多数项目的初始状态，界面要按空态处理并给创建入口，
    而不是把它渲染成一次失败 —— 把正常状态报成错误，用户会以为平台坏了。
    """
    location = locate_pack(project_code, repo_root=repo_root)

    entries: list[KnowledgeEntry] = []
    if location.exists:
        manifest_path = location.pack_dir / PROJECT_PACK_MANIFEST
        if manifest_path.is_file():
            size, modified = _describe_file(manifest_path)
            entries.append(
                KnowledgeEntry(
                    kind=KIND_MANIFEST,
                    name=PROJECT_PACK_MANIFEST,
                    rel_path=PROJECT_PACK_MANIFEST,
                    size=size,
                    modified_at=modified,
                    usage="知识包的入口。模型先看到它的一句话说明，正文里列出的文档才会被按需读取。",
                    removable=False,
                )
            )

        references_dir = location.pack_dir / REFERENCES_DIR_NAME
        if references_dir.is_dir():
            for path in sorted(references_dir.glob("*" + _MD_SUFFIX)):
                size, modified = _describe_file(path)
                entries.append(
                    KnowledgeEntry(
                        kind=KIND_REFERENCE,
                        name=path.name,
                        rel_path=f"{REFERENCES_DIR_NAME}/{path.name}",
                        size=size,
                        modified_at=modified,
                        usage="按需读取：模型判断「属于哪个系统」「按什么规则编号」时会来读这一份。",
                        removable=True,
                    )
                )

        for child in sorted(path for path in location.pack_dir.glob("*") if path.is_dir()):
            if child.name == REFERENCES_DIR_NAME:
                continue
            skill_md = child / SKILL_MD_NAME
            if not skill_md.is_file():
                continue
            size, modified = _describe_file(skill_md)
            entries.append(
                KnowledgeEntry(
                    kind=KIND_SKILL,
                    name=child.name,
                    rel_path=f"{child.name}/{SKILL_MD_NAME}",
                    size=size,
                    modified_at=modified,
                    usage="项目自建的子 skill：它的说明会进提示词索引，正文由模型按需读取。",
                    removable=True,
                    dir_name=child.name,
                )
            )

    return {
        "slug": location.slug,
        "projects_root": str(location.projects_root),
        "exists": location.exists,
        "entries": [entry.as_dict() for entry in entries],
        "limits": {
            "max_file_bytes": MAX_FILE_BYTES,
            "max_pack_bytes": MAX_PACK_BYTES,
            "max_name_chars": _SKILL_DIR_MAX_CHARS,
            "max_description_chars": DESCRIPTION_MAX_CHARS,
            "max_skill_md_lines": SKILL_MD_MAX_LINES,
        },
        "manifest_template": MANIFEST_TEMPLATE.replace("{slug}", location.slug),
        "scaffold": {
            "available": not (location.pack_dir / PROJECT_PACK_MANIFEST).is_file(),
            "manifest": SCAFFOLD_MANIFEST_TEMPLATE.replace("{slug}", location.slug),
            "documents": [name for name, _hint in SCAFFOLD_REFERENCES],
        },
        "skill_body_template": SKILL_BODY_TEMPLATE.replace("{name}", "<子 skill 目录名>"),
    }


def read_entry(project_code: str, kind: str, name: str, *, repo_root: Path | None = None) -> dict:
    """读一份文档的正文。

    文件不存在时回**可读的拒绝**而不是让 `FileNotFoundError` 冒成 500：
    「两个人同时开着这个面板、其中一个先删掉了」是真实会发生的时序，
    那时另一个人的界面应该看到「已被删除」，而不是服务器内部错误。
    """
    location = locate_pack(project_code, repo_root=repo_root)
    rel_path = _rel_path_for(kind, name)
    target = _resolve_existing(location, rel_path)
    if not target.is_file():
        raise _reject("__pack__", rel_path, f"`{rel_path}` 不存在，可能已经被删掉了。")
    text = target.read_text(encoding="utf-8")
    size, modified = _describe_file(target)
    return {
        "kind": kind,
        "name": target.name if kind != KIND_SKILL else name,
        "rel_path": rel_path,
        "content": text,
        "size": size,
        "modified_at": modified,
    }


def _rel_path_for(kind: str, name: str) -> str:
    """把 (类型, 名字) 换成一个 pack 目录内的相对路径。**这是唯一的路径拼装处**。

    三类内容各自只有一种形状：
      * manifest    → `KNOWLEDGE.md`（名字由契约固定，不接受用户输入）
      * reference   → `references/<名字>.md`
      * skill       → `<目录名>/SKILL.md`
    `kind` 是路由段的一部分，因此这里的 `else` 分支不是「兜底」而是「拒绝」——
    多一种 kind 就意味着多一条能落到磁盘上的路径，必须显式加进来。
    """
    if kind == KIND_MANIFEST:
        return PROJECT_PACK_MANIFEST
    if kind == KIND_REFERENCE:
        return f"{REFERENCES_DIR_NAME}/{normalize_reference_name(name)}{_MD_SUFFIX}"
    if kind == KIND_SKILL:
        return f"{normalize_skill_name(name)}/{SKILL_MD_NAME}"
    raise _reject("name", "内容类型", f"不认识的内容类型 {kind!r}。")


def _resolve_existing(location: PackLocation, rel_path: str) -> Path:
    """把相对路径拼到 pack 目录下，并确认它**真的在 pack 目录里**。

    这里是纵深防御的第二道（第一道是名字白名单、第三道是 `safe_join` 的
    `resolve()` + `relative_to()`）：即便将来有人给 `rel_path` 加了一条能带分隔符的
    来源，这一步也会拦下来。用 `relative_to()` 而不是字符串前缀比较 ——
    前缀比较会把 `<pack>/../pack-elsewhere/x` 判成「在里面」。
    """
    try:
        candidate = safe_join(location.pack_dir, *rel_path.split("/"))
    except SkillLoadError as exc:
        raise _reject("name", "文件路径", f"路径不合法：{exc}") from exc

    resolved = candidate.resolve()
    pack_resolved = location.pack_dir.resolve()
    try:
        resolved.relative_to(pack_resolved)
    except ValueError as exc:
        raise _reject("name", "文件路径", "只能在这个项目的知识包目录里操作。") from exc
    _reject_platform_target(resolved)
    return candidate


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


def _require_manifest_first(location: PackLocation) -> None:
    """新建知识文档 / 子 skill 之前，知识包清单必须已经存在。

    **这是前置检查，不是又一次契约校验**：`validate_project_pack` 对「既没有
    KNOWLEDGE.md 也没有 SKILL.md 的目录」只会回一句带绝对路径的
    「… 下既没有 SKILL.md 也没有 KNOWLEDGE.md」，对策划完全不可读。
    这里提前换成一句能照着做的话。判定的仍然是同一条契约（清单是入口）。
    """
    if (location.pack_dir / PROJECT_PACK_MANIFEST).is_file():
        return
    raise _reject(
        "__pack__",
        "知识包",
        "这个项目还没有知识包清单（KNOWLEDGE.md）。请先点「从模板创建知识包」，"
        "再把文档加进去 —— 清单是知识包的入口，模型靠它才知道有哪些文档可读。",
    )


def write_manifest(project_code: str, content: str, *, repo_root: Path | None = None) -> dict:
    """新建 / 覆盖知识包清单 `KNOWLEDGE.md`。"""
    location = locate_pack(project_code, repo_root=repo_root)
    text = _normalize_text(content)
    _check_content_size(text, label=PROJECT_PACK_MANIFEST)
    _check_pack_size(location.pack_dir, extra_bytes=len(text.encode("utf-8")))

    def mutate(root: Path) -> None:
        _atomic_write(root / PROJECT_PACK_MANIFEST, text)

    _apply_with_validation(location.pack_dir, mutate, edited_rel=PROJECT_PACK_MANIFEST)
    return {"rel_path": PROJECT_PACK_MANIFEST, "slug": location.slug}


def write_reference(
    project_code: str,
    name: str,
    content: str,
    *,
    description: str = "",
    repo_root: Path | None = None,
) -> dict:
    """新建 / 覆盖一份知识文档 `references/<name>.md`。

    **引用行与文件在同一次写入里落地**（还有比这更重要的细节吗）：

    `_check_references()` 是双向的，所以「先建文档再改清单」和「先改清单再建文档」
    这两种顺序**都不成立** —— 第一种会被「没有被正文提到」拒绝，第二种会被
    「提到了但文件不存在」拒绝。这不是可以绕过的策略问题，是双向约束的必然结果。

    所以这份函数的契约是：**只要清单里还没有它的引用行，就顺手补一行**，两处改动
    在同一次影子目录写入里完成、一次校验通过。用户看到的是「新建成功」，
    而清单里多出来的那一行（`- "文件名" —— 说明`）是让这次操作成立的必要部分；
    界面上会提前说明这件事，不让他以为是别人改的。

    已经有了引用行（用户自己写过）就不动清单 —— 那一行是他的措辞，不该被覆盖。
    """
    location = locate_pack(project_code, repo_root=repo_root)
    rel_path = _rel_path_for(KIND_REFERENCE, name)
    file_name = Path(rel_path).name
    _require_manifest_first(location)
    text = _normalize_text(content)
    _check_content_size(text, label=file_name)
    _check_pack_size(location.pack_dir, extra_bytes=len(text.encode("utf-8")))

    manifest_path = location.pack_dir / PROJECT_PACK_MANIFEST
    current_manifest = manifest_path.read_text(encoding="utf-8")
    updated_manifest = _append_reference_mention(
        current_manifest, file_name, _normalize_text(description)
    )
    mention_added = updated_manifest != current_manifest

    def mutate(root: Path) -> None:
        _atomic_write(safe_join(root, REFERENCES_DIR_NAME, file_name), text)
        if updated_manifest != current_manifest:
            _atomic_write(root / PROJECT_PACK_MANIFEST, updated_manifest)

    # 归因的目标是**用户正在编辑的那份文档**：清单那一行的改动是我们替他做的，
    # 把问题标到清单上他会找不到地方改。
    _apply_with_validation(location.pack_dir, mutate, edited_rel=rel_path)
    return {"rel_path": rel_path, "slug": location.slug, "mention_added": mention_added}


def _append_reference_mention(manifest_text: str, file_name: str, description: str) -> str:
    """在清单正文末尾补一行对 `file_name` 的引用（已经有了就不动）。"""
    if file_name in _mentioned_names(manifest_text):
        return manifest_text
    note = description or "（把这一行换成它的用途说明）"
    body = manifest_text if manifest_text.endswith("\n") else manifest_text + "\n"
    return f"{body}\n- `{file_name}` —— {note}\n"


def _remove_reference_mention(manifest_text: str, file_name: str) -> str:
    """把提到 `file_name` 的那些**行**整行删掉（只删行，不动别的行）。"""
    kept: list[str] = []
    for line in manifest_text.split("\n"):
        if file_name in _mentioned_names(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def _mentioned_names(text: str) -> set[str]:
    """复用契约里的同一套提取规则（先剥代码围栏，再取行内代码里的 basename）。

    刻意调 `skill_contract._mentioned_markdown_files` 而不是自己写一遍正则：
    「哪些写法算提到了这份文档」必须与校验器**逐字一致**，否则会出现
    「我们以为补上了引用行，校验器不认」——用户看到的是保存莫名其妙地失败。
    """
    return _mentioned_markdown_files(text)


def create_pack_from_template(project_code: str, *, repo_root: Path | None = None) -> dict:
    """从模板创建一个**可用的**知识包：清单 + 两份起始文档，一次写完。

    必须一次写完，理由与 `write_reference` 相同：建包模板的正文里引用了那两份文档，
    分开写的话中间那一步必然不合法。建出来的包立刻就是合法的、能被加载的，
    用户可以在此基础上改内容（而不是先面对一个「存不进去的模板」）。

    已经建过就不覆盖：那会把用户已经写好的清单冲掉。要重建请先自己清空。
    """
    location = locate_pack(project_code, repo_root=repo_root)
    if (location.pack_dir / PROJECT_PACK_MANIFEST).is_file():
        raise _reject(
            "__pack__",
            "知识包",
            "这个项目已经有知识包清单了，模板不会覆盖它。要重建请先手动清空 KNOWLEDGE.md 的内容。",
        )

    manifest_text = SCAFFOLD_MANIFEST_TEMPLATE.replace("{slug}", location.slug)
    documents = {
        f"{name}{_MD_SUFFIX}": scaffold_reference_body(name, hint)
        for name, hint in SCAFFOLD_REFERENCES
    }
    total = len(manifest_text.encode("utf-8")) + sum(
        len(text.encode("utf-8")) for text in documents.values()
    )
    _check_content_size(manifest_text, label=PROJECT_PACK_MANIFEST)
    _check_pack_size(location.pack_dir, extra_bytes=total)

    def mutate(root: Path) -> None:
        _atomic_write(root / PROJECT_PACK_MANIFEST, manifest_text)
        for file_name, text in documents.items():
            _atomic_write(safe_join(root, REFERENCES_DIR_NAME, file_name), text)

    _apply_with_validation(location.pack_dir, mutate, edited_rel=PROJECT_PACK_MANIFEST)
    return {"slug": location.slug, "documents": sorted(documents)}


def write_skill(
    project_code: str,
    name: str,
    *,
    description: str,
    body: str,
    repo_root: Path | None = None,
) -> dict:
    """新建 / 整体覆盖一个子 skill（`<目录名>/SKILL.md`）。

    frontmatter **由服务端拼**，只从用户那里取两个值：目录名与一句话说明。
    让用户手写 frontmatter 是必然出错的（见 `SKILL_BODY_TEMPLATE` 的注释）。
    拼出来的形态一定是契约接受的那一种：`---` 在第 0 列、两个键、值里不含
    `": "` / `" #"` 之类的 YAML 引导符（后者由 `parse_frontmatter` 兜底判非法）。
    """
    location = locate_pack(project_code, repo_root=repo_root)
    dir_name = normalize_skill_name(name)
    _require_manifest_first(location)

    description_text = _normalize_text(description)
    if not description_text:
        raise _reject(
            "description",
            "一句话说明（description）",
            "这一栏必填。它就是模型在索引里看到的全部内容 —— 不写，模型就不知道该不该读它。",
        )
    if len(description_text) > DESCRIPTION_MAX_CHARS:
        raise _reject(
            "description",
            "一句话说明（description）",
            f"太长了（{len(description_text)} 字，上限 {DESCRIPTION_MAX_CHARS}）。",
        )

    body_text = _normalize_text(body)
    if not body_text.strip():
        raise _reject("content", "SKILL.md 正文", "正文不能为空。可以点「插入最小模板」拿到一份骨架。")

    markdown = _compose_skill_markdown(dir_name, description_text, body_text)
    _check_content_size(markdown, label=SKILL_MD_NAME)
    _check_pack_size(location.pack_dir, extra_bytes=len(markdown.encode("utf-8")))

    rel_path = f"{dir_name}/{SKILL_MD_NAME}"

    def mutate(root: Path) -> None:
        skill_dir = safe_join(root, dir_name)
        skill_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(skill_dir / SKILL_MD_NAME, markdown)

    _apply_with_validation(location.pack_dir, mutate, edited_rel=rel_path)
    return {"rel_path": rel_path, "slug": location.slug, "content": markdown}


def _compose_skill_markdown(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n"


# ---------------------------------------------------------------------------
# 删
# ---------------------------------------------------------------------------


def delete_reference(project_code: str, name: str, *, repo_root: Path | None = None) -> dict:
    """删掉一份知识文档，**同时把清单里对它的引用行摘掉**。

    与 `write_reference` 对称：不摘那一行的话，删完之后 `_check_references()` 会报
    「正文里提到了 `foo.md`，但 references/ 下没有这个文件」—— 于是这个删除**永远
    不可能成功**，用户只会在「删除」和「先改清单」之间来回撞墙。

    所以这里替他摘掉那一行，并在界面的二次确认里**明说**「会同时移除清单里的引用」。
    刻意只删提到它的整行，其余行一个字不动 —— 清单里其他内容是用户写的，
    自动改写它必须做到「只动与这次操作直接相关的那一行」。
    """
    location = locate_pack(project_code, repo_root=repo_root)
    rel_path = _rel_path_for(KIND_REFERENCE, name)
    file_name = Path(rel_path).name
    target = _resolve_existing(location, rel_path)
    if not target.is_file():
        raise _reject("name", "文件名", f"`{rel_path}` 不存在，可能已经被删掉了。")

    manifest_path = location.pack_dir / PROJECT_PACK_MANIFEST
    current_manifest = manifest_path.read_text(encoding="utf-8")
    updated_manifest = _remove_reference_mention(current_manifest, file_name)
    mention_removed = updated_manifest != current_manifest

    def mutate(root: Path) -> None:
        candidate = safe_join(root, REFERENCES_DIR_NAME, file_name)
        if candidate.is_file():
            candidate.unlink()
        if mention_removed:
            _atomic_write(root / PROJECT_PACK_MANIFEST, updated_manifest)

    _apply_with_validation(location.pack_dir, mutate, edited_rel=rel_path)
    return {"rel_path": rel_path, "slug": location.slug, "mention_removed": mention_removed}


def delete_skill(project_code: str, name: str, *, repo_root: Path | None = None) -> dict:
    """删掉一个子 skill —— **连它的目录一起删干净**。

    只删 `SKILL.md` 会让目录留下来：`skill_loader` 会忽略没有 SKILL.md 的目录
    （读不到内容），但 `validate_project_pack` 的遍历会跳过它、用户却在文件管理器里
    看到一个空壳目录 —— 「删了但还在」。所以这里按目录删。
    """
    location = locate_pack(project_code, repo_root=repo_root)
    dir_name = normalize_skill_name(name)
    skill_dir = _resolve_existing(location, f"{dir_name}/{SKILL_MD_NAME}")
    if not skill_dir.is_file():
        raise _reject("name", "子 skill", f"`{dir_name}` 不存在，可能已经被删掉了。")

    def mutate(root: Path) -> None:
        candidate = safe_join(root, dir_name)
        if candidate.is_dir():
            shutil.rmtree(candidate)

    # 删掉之后整包也必须仍然合法（例如它不能是包里唯一的内容）。
    _apply_with_validation(location.pack_dir, mutate, edited_rel=f"{dir_name}/{SKILL_MD_NAME}")
    return {"rel_path": f"{dir_name}/{SKILL_MD_NAME}", "slug": location.slug}


def _normalize_text(raw: object) -> str:
    """统一换行并去掉 BOM。

    BOM 必须去掉：`parse_frontmatter` 要求 `---` 在**第 0 列**，而带 BOM 的文本里
    第 0 个字符是 `\\ufeff` —— Windows 上「用记事本另存为 UTF-8」就会带上它。
    用户看不出任何区别，只会拿到一句「必须以 `---` 开头」。
    """
    text = "" if raw is None else str(raw)
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")
