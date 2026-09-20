"""项目事实的声明与读取：关键路径、生成物前缀。

## 这个模块解决的是什么

平台要能复用于大部分项目，所以「本项目哪些改动危险」「本项目的导表产物叫什么前缀」
这类**项目事实**不该写死在平台代码里。此前它们就是模块常量，而且**失效时一声不响**：

* `CRITICAL_PATH_PATTERNS` 的每条模式都要求**前导斜杠**（`/config/`），而 git 提交里的
  路径是相对路径（`config/[30]道具表_CfgItem.xlsx`），于是 `_is_critical_path()` 恒为
  False —— 「命中了关键路径就把增量分析升级成全量分析」这条通道**从来没触发过**，
  不报错、不留痕。
* `Cfg` 前缀写在 `bundles.py` 的默认值里，不叫 `CfgXxx` 的项目里「表 ↔ 生成物」配对
  恒为 0 组，提示词里一个字都不提 —— 「这个项目本来就没得配」与「我们根本不会配」
  长得一模一样。

本模块是这两类声明的**唯一读取入口**，并且把「值是从哪来的」当成返回值的一部分交出去
（`source` / `detail` / `warning`）：**「项目没声明，用平台默认」与「项目声明了『没有』」
必须能分开**，否则又回到「一声不响」。

## 两类声明各自的落点（为什么不是同一个地方）

* **关键路径**：
  * **表名** → `Repository.important_tables`（界面上已有的「重点表名」那一栏）。
    复用它是刻意的：平台里已经有这个字段、已经有写入路径与界面，再造一套声明通道
    等于让管理员在两个地方填同一件事。在这之前它**只写不读**，管理员填的重点
    从未影响过任何判定。
  * **路径模式** → 本模块的 `DEFAULT_CRITICAL_PATH_PATTERNS` 是平台默认（保留原清单，
    只把「前导斜杠」这个 bug 修掉），项目可用知识包里的 `critical_path_patterns` 覆盖。
    配表驱动项目的危险路径（`cfg/`、`code/**/cfg/` 之类）不在默认清单里，那是**项目
    事实**，只能由项目自己声明 —— 平台替它猜一个，猜错的代价是每次分析都被升级成全量。
* **生成物前缀** → **项目知识包**（`skills/projects/<slug>/references/project-facts.md`）。
  理由：它就是「项目事实」，而知识包在这个仓库里已经是项目事实的落点（见
  `skills/projects/g119/KNOWLEDGE.md`「这里放什么」）；并且它已经有平台读写入口
  （`services/ai/project_pack_service.py`，保存时会跑契约校验、写坏了存不进去）。
  另外两条路都不如它：写进平台常量＝换个项目就不成立（就是这次要修的病）；
  写进项目 AI 配置则**没有那一栏的界面**（模板里逐字段列了控件），等于要求管理员
  手工调接口才能声明。

## 声明文件的形态

    skills/projects/<slug>/references/project-facts.md

frontmatter 是机器读的声明，沿用知识包既有的 `key: value` 约定（`skill_contract.
parse_frontmatter`，刻意不引入 PyYAML），正文写给人看。三个键都可选：

    generated_prefixes: Cfg, CfgMod
    critical_path_patterns: /cfg/, \\.sql$
    dimensions: performance=性能与耗时, protocol=协议兼容性, resource=资源引用

值写 `none`（也接受 `无` / `-` / `null`）表示**项目明确声明「没有」**：那时前缀是空
元组、模式是空元组，**不回落平台默认**。「本项目没有可配对的产物」与「平台不知道这个
项目该怎么配」是两件不同的事，只有前者才该被记成「0 组」。

## `dimensions` 的写法（为什么是 `id=中文名`）

它是本项目**生效的检查维度清单**，取自「一份要好写、又要能被严格校验」这两条：

* **好写**：一行 `id=中文名`，多个用逗号分隔（与上面两个键同一套分隔符）。
  `id` 是模型要写进 `category` 的那个标识符，中文名只进报告与导出文档 —— 两样都要，
  因为平台不允许出现「报告里那一格显示英文 id」这种半成品。
  **中文名里不要写逗号**（含中文逗号）：它就是分隔符，写了会被切开并判非法
  （判非法而不是猜着切，是因为猜错的代价是一份谁都没发现的、少了几个维度的清单）。
* **可严格校验**：`id` 的形状有正则钉着（小写字母开头、只含小写字母数字下划线），
  重复、缺中文名、形状不对都**判非法**而不是猜。声明坏掉时**回落平台默认并带 warning**，
  与上面两个键同一条纪律：坏掉的声明不能悄悄退化成「项目没声明」。
* **顺序有意义**：子代理模式按这个顺序把维度**相邻地**切给各分片（相邻即相关），
  所以声明里的顺序就是「哪些维度该被同一个人看」。
* **`none` 对这一个键是非法值**：声明成「没有维度」的意思是「本次分析没有任何检查
  维度」，而那时模型报出的每一条异常都只能进「未归类」——一个自相矛盾的配置。
  写 `none` 会被告警并按「未声明」处理。

没声明时**与今天逐字一致**：生效清单就是平台出厂的那九个（含中文名与顺序），提示词里
也不会多出任何一段。

## 读不到时怎么办

* 文件不存在 / 没有那个键 → 用平台默认值（`source=SOURCE_DEFAULT`）。这是**绝大多数
  项目**的正常状态，不是错误。
* 文件在、但 frontmatter 不合法 → 用平台默认值，并带一句 `warning` 交出去。坏掉的
  声明不能悄悄退化成默认值，否则「我明明声明了」会变成只有手工查磁盘才查得出来的问题。
* 模式里混进了非法正则 → 丢掉那一条并告警；一条都不剩时回落默认清单（同样告警）。

## 模式与路径的比对口径

路径先按 `normalize_path` 归一（反斜杠转正斜杠、去掉 `./`），比对时转小写；声明的
模式**大小写不敏感**（编译时加 `re.IGNORECASE`），免得「声明里写了大写、改动里的
路径是小写」变成一次静默失配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from services.ai.bundles import DEFAULT_GENERATED_PREFIXES
from services.ai.scope import normalize_path
from services.ai.skill_contract import (
    DEFAULT_DIMENSION_SPECS,
    DimensionSpec,
    SkillContractError,
    build_dimensions,
    parse_frontmatter,
)
from services.ai.skill_loader import (
    SkillLoadError,
    project_pack_slug,
    resolve_projects_root,
    safe_join,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 声明的落点：知识包 references/ 下的这一份文档。它同时会进可读文档索引（模型按需读），
# 所以正文要写给人看 —— frontmatter 是给平台读的那一段。
DECLARATIONS_FILENAME = "project-facts.md"
DECLARATIONS_DIRNAME = "references"
DECLARATIONS_RELATIVE_PATH = f"{DECLARATIONS_DIRNAME}/{DECLARATIONS_FILENAME}"

KEY_GENERATED_PREFIXES = "generated_prefixes"
KEY_CRITICAL_PATH_PATTERNS = "critical_path_patterns"
# 本次分析生效的检查维度清单。**默认是平台出厂的那九个**（`DEFAULT_DIMENSION_SPECS`），
# 项目声明了就按声明走 —— 没有配表相关维度的项目不必每轮为五个不存在的维度编理由，
# 非配表项目（性能、协议、资源引用…）也不必把真实发现写进一个不属于自己的类别。
KEY_DIMENSIONS = "dimensions"

# 平台默认的关键路径模式。
#
# **每一条都写成「路径分量起点」**（`(?:^|/)config/`），不能写成 `/config/`：
# 后者要求前导斜杠，而 git 的 `Commit.path` 是**不带前导斜杠**的相对路径，于是
# 「命中关键路径 → 升级为全量分析」这条通道在 git 仓库上一次都没触发过，且不报错。
# 写成分量起点之后，`config/x` 与 `/config/x`（SVN 风格）都命中，而 `myconfig/x`、
# `deconfig/x` **不**命中 —— 那两种路径跟 `config/` 这个目录没有关系。
#
# 这份清单是**平台默认**，不是「通用真理」：它带着 Web 后端的口味（`auth`/`payment`/
# `billing`），对配表驱动的项目基本用不上。真正的项目事实由项目自己声明 ——
# 表名走 `Repository.important_tables`，路径模式走知识包里的 `critical_path_patterns`。
DEFAULT_CRITICAL_PATH_PATTERNS = (
    r"(?:^|/)config/",
    r"(?:^|/)configs/",
    r"(?:^|/)sql/",
    r"(?:^|/)schema/",
    r"(?:^|/)migrations/",
    r"(?:^|/)auth/",
    r"(?:^|/)permission/",
    r"(?:^|/)payment/",
    r"(?:^|/)billing/",
    r"\.sql$",
)

# 「值写成这些」＝ 项目明确声明「没有」。
_NONE_TOKENS = frozenset({"none", "无", "-", "null"})

# 声明项之间的分隔符。界面那一栏的提示语写的是「用英文逗号分隔」，但中文逗号、顿号、
# 分号、换行都是人手打字时的自然写法，而它们**不可能**是表名/模式的一部分（表名里带
# 顿号的文件名在实际项目里不存在），所以一起收。
# 少收一种的后果不是报错，而是「我明明填了、它就是没命中」—— 那正是这一批要消灭的形态。
_LIST_SPLIT_RE = re.compile(r"[,，、;；\r\n]+")

# 「名字片段的起点」判据：前面不能是 ASCII 字母或数字。
# **必须按 ASCII 判，不能用 `\w`/`isalnum()`**：中文在它们眼里算字母数字，于是
# `图标表` 里的 `道具表` 会被判成「前面有字母」而漏配。
# 与 `bundles._NOT_AT_COMPONENT_START` 是同一套判据，两处都用到它（那里判记号，
# 这里判表名），所以两处的注释也刻意写成同一条理由。
_NOT_AT_COMPONENT_START = re.compile(r"[A-Za-z0-9]")

# 取值来源。**它们是返回值的一部分**，调用方据此把「默认」与「项目声明」写进日志 ——
# 只回一个值而不回来源，等于把这次要消灭的「一声不响」换个地方重演。
SOURCE_DEFAULT = "default"          # 项目没声明 → 平台默认值
SOURCE_PROJECT = "project"          # 项目声明了值
SOURCE_PROJECT_EMPTY = "project_empty"   # 项目明确声明「没有」

# 一次扫描最多留几条命中理由（日志一行装得下、也看得完）。
_MAX_REASONS = 3


# ---------------------------------------------------------------------------
# 读声明文件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Declarations:
    """一份读出来的声明。`fields` 为空表示「这个项目没声明」。"""

    fields: dict
    # 给日志/报告看的一句话：值是从哪儿来的（含相对路径）。
    detail: str
    # 声明文件的仓库相对路径（没有文件时是空串）。告警里要用它指出「改哪个文件」。
    path_label: str = ""
    # 声明坏掉时的原因（空串 = 没坏）。**不能吞掉**：坏掉的声明会退化成一个
    # 「平台从没被声明过」，与「项目本来就没声明」在行为上一模一样。
    warning: str = ""


def declaration_path(project_code: Optional[str], *, repo_root: Path = _REPO_ROOT) -> Optional[Path]:
    """把项目代号解析成声明文件的路径。解析不出来（代号为空）时返回 None。"""
    slug = project_pack_slug(project_code or "")
    if not slug:
        return None
    try:
        root = resolve_projects_root(repo_root)
        return safe_join(root, slug, DECLARATIONS_DIRNAME, DECLARATIONS_FILENAME)
    except SkillLoadError:
        # 代号全是非法字符之类 —— 按「没有知识包」处理，与 `load_skills` 的口径一致。
        return None


def read_declarations(project_code: Optional[str], *, repo_root: Path = _REPO_ROOT) -> Declarations:
    """读项目知识包里的声明。读不到是正常的（返回空声明），读坏了要带警告。"""
    path = declaration_path(project_code, repo_root=repo_root)
    if path is None or not path.is_file():
        return Declarations(fields={}, detail="项目知识包里没有声明文件（用平台默认值）")

    relative = f"skills/projects/{path.parent.parent.name}/{DECLARATIONS_RELATIVE_PATH}"
    try:
        text = path.read_text(encoding="utf-8")
        fields, _body = parse_frontmatter(text)
    except SkillContractError as exc:
        return Declarations(
            fields={},
            detail=f"{relative} 读不出声明（用平台默认值）",
            path_label=relative,
            warning=f"{relative} 的 frontmatter 不合法，已按「未声明」处理：{exc}",
        )
    except OSError as exc:
        return Declarations(
            fields={},
            detail=f"{relative} 读不出来（用平台默认值）",
            path_label=relative,
            warning=f"{relative} 读不出来，已按「未声明」处理：{exc}",
        )
    return Declarations(
        fields=fields, detail=f"项目声明（{relative}）", path_label=relative
    )


def _list_value(raw: str, *, what: str, where: str) -> tuple[Optional[tuple[str, ...]], str]:
    """把一个逗号分隔的声明值读成元组。

    返回 `(值, 警告)`；`值 is None` 表示「没有有效取值」（调用方决定回落默认还是报错）。
    声明为空（`none`）时返回**空元组**，这是一个有意义的取值，不是「没读到」。
    """
    items = tuple(item.strip() for item in _LIST_SPLIT_RE.split(str(raw or "")) if item.strip())
    if not items:
        return None, f"{where} 的 {what} 是空的，已按「未声明」处理"
    if len(items) == 1 and items[0].lower() in _NONE_TOKENS:
        return (), ""
    return items, ""


# ---------------------------------------------------------------------------
# 关键路径
# ---------------------------------------------------------------------------


def declared_important_tables(raw: object) -> tuple[str, ...]:
    """把界面上那一栏「重点表名」读成一串声明。

    界面存进来的是一整段文本（`Repository.important_tables`，`Text` 列），按逗号分隔。
    读不出来（列是 None / 空串 / 全空白）时返回空元组 —— 空元组与「没声明」是同一个
    意思，调用方据此走平台默认的关键路径模式。
    """
    if raw is None:
        return ()
    return tuple(
        item.strip()
        for item in _LIST_SPLIT_RE.split(str(raw))
        if item.strip()
    )


def important_table_hit(path: str, declared: Sequence[str]) -> str:
    """改动路径是否命中项目声明的「重点表」，命中则返回命中的那一条声明原文。

    ## 判据（为什么这么定）

    1. **按表名/文件名比，不按整条路径精确比**。管理员填的是「表名」（界面上那一栏
       就叫「重点表名」），而改动路径普遍是 `config/[30]道具表_CfgItem.xlsx` 这种带
       目录前缀与 ID 段位前缀的形式，要求他把路径一字不差地抄一遍是难为人的。
    2. **同时按 basename、去扩展名的 stem、整条路径比**（忽略大小写）：三种写法
       他抄哪一种都能命中 —— 「带不带目录前缀」和「带不带扩展名」都不该是命中与否的
       决定因素。
    3. **包含匹配要求「名字分量的起点」**：声明项出现在 stem 里，且它前面不是 ASCII
       字母数字。于是「道具表」命中 `[30]道具表_CfgItem.xlsx`（前面是 `]`），
       而「item」**不**命中 `MyItemTable.xlsx`（前面是 `y`）—— 后者与「Item」是
       两个不相干的名字。
    4. 中文是连续书写、没有词边界，所以这条包含匹配**允许**「道具表」命中
       `道具表备份.xlsx`。这是刻意放宽的：这个判定的唯一后果是**升级为全量分析**
       （多花额度、看得更全），不是给出一个错的结论；反过来漏掉一张重点表，代价是
       该看的东西没看。方向不对称，所以口径往「宁可多认」那一侧倒。

    参数 `path` 用改动路径的原始形态（正反斜杠都可以，内部会归一）。
    """
    normalized = normalize_path(str(path or ""))
    if not normalized:
        return ""
    lower_path = normalized.lower()
    name = lower_path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name

    for item in declared:
        token = str(item or "").strip().lower()
        if not token:
            continue
        if token in (lower_path, name, stem):
            return str(item).strip()
        for match in re.finditer(re.escape(token), stem):
            start = match.start()
            if start > 0 and _NOT_AT_COMPONENT_START.match(stem[start - 1]):
                continue
            return str(item).strip()
    return ""


@dataclass(frozen=True)
class CriticalPathFacts:
    """一个项目的关键路径事实：生效的模式 + 它们是从哪来的。"""

    patterns: tuple[str, ...]
    patterns_source: str
    warning: str = ""

    @property
    def detail(self) -> str:
        """给日志看的一句话（含来源）。"""
        if self.patterns_source == SOURCE_PROJECT:
            return f"路径模式=项目声明 {len(self.patterns)} 条"
        if self.patterns_source == SOURCE_PROJECT_EMPTY:
            return "路径模式=项目声明「没有」"
        return f"路径模式=平台默认 {len(self.patterns)} 条"

    def why(self, path: str, declared_tables: Sequence[str] = ()) -> str:
        """命中则返回「为什么算关键路径」，未命中返回空串。

        先看项目声明的**重点表**再看**路径模式**：前者是项目自己写下的确切事实，
        后者是平台（或项目）给的模式。两者语义不重合（一个是「表」，一个是「路径」），
        命中**任一**即算关键路径。
        """
        table = important_table_hit(path, declared_tables)
        if table:
            return f"项目声明的重点表「{table}」"
        normalized = normalize_path(str(path or ""))
        if not normalized:
            return ""
        lower = normalized.lower()
        for pattern in self.patterns:
            try:
                if re.search(pattern, lower, re.IGNORECASE):
                    return f"命中关键路径模式 {pattern}"
            except re.error:
                # 编译期已经筛过一遍（见 `_compile_patterns`），这里是防御性的：
                # 一个坏模式**不能**把整次分析炸掉。
                continue
        return ""


DEFAULT_CRITICAL_PATH_FACTS = CriticalPathFacts(
    patterns=DEFAULT_CRITICAL_PATH_PATTERNS,
    patterns_source=SOURCE_DEFAULT,
)


def declared_important_tables_by_repo(repos: Mapping[int, object]) -> dict:
    """`{仓库 id: 该仓库声明的重点表}`。

    `repos` 是 `{repository_id: Repository}`。抽出来是为了让调用方（周版本汇总）只写
    一行 —— 「重点表名怎么读」这件事只有这里一份，调用方不需要知道它是个逗号分隔的
    `Text` 列。
    """
    return {
        repo_id: declared_important_tables(getattr(repo, "important_tables", ""))
        for repo_id, repo in repos.items()
    }


@dataclass(frozen=True)
class CriticalPathScan:
    """一批改动文件的关键路径扫描结果。

    `reasons` 里每条是「路径（为什么算关键路径）」，最多留 `_MAX_REASONS` 条：
    范围判定说「升级为全量」的时候必须说得出**是谁**把它升上来的，否则「关键路径」
    这四个字只是个无法核对的结论。
    """

    hit: bool
    reasons: tuple[str, ...]
    facts: CriticalPathFacts

    @property
    def source(self) -> str:
        return self.facts.patterns_source

    def log_line(self) -> str:
        """升级为全量时写进日志的那一行。"""
        return (
            f"⚠️ AI 分析：命中关键路径，范围判定升级为全量（{self.facts.detail}）："
            + "；".join(self.reasons)
        )


def scan_critical_paths(
    entries: Iterable[tuple], facts: CriticalPathFacts = DEFAULT_CRITICAL_PATH_FACTS
) -> CriticalPathScan:
    """扫一批 `(路径, 该仓库声明的重点表)`，返回命中情况。

    `facts` 是路径模式那一侧（平台默认或项目声明）；表名那一侧逐条传进来，因为
    **不同仓库可以声明不同的重点表**。
    """
    reasons: list[str] = []
    for path, declared_tables in entries:
        reason = facts.why(path, declared_tables)
        if reason and len(reasons) < _MAX_REASONS:
            reasons.append(f"{path}（{reason}）")
    return CriticalPathScan(hit=bool(reasons), reasons=tuple(reasons), facts=facts)


def _compile_patterns(raw: str, where: str) -> tuple[Optional[tuple[str, ...]], str]:
    """把声明里的模式逐条编译一遍：坏模式丢掉并告警，一条都不剩时交给调用方回落默认。"""
    items, warning = _list_value(raw, what=KEY_CRITICAL_PATH_PATTERNS, where=where)
    if items is None:
        return None, warning
    if not items:
        return (), ""
    good: list[str] = []
    bad: list[str] = []
    for item in items:
        try:
            re.compile(item)
        except re.error as exc:
            bad.append(f"{item}（{exc}）")
            continue
        good.append(item)
    if bad:
        problem = (
            f"{where} 的 {KEY_CRITICAL_PATH_PATTERNS} 里有非法正则，已丢弃："
            + "；".join(bad)
        )
        if not good:
            problem += "。一条都没剩，本轮回落平台默认清单"
            return None, problem
        return tuple(good), problem
    return tuple(good), ""


def critical_path_facts(
    project_code: Optional[str], *, repo_root: Path = _REPO_ROOT
) -> CriticalPathFacts:
    """读一个项目的关键路径事实（模式部分）。取不到就用平台默认。"""
    declarations = read_declarations(project_code, repo_root=repo_root)
    raw = declarations.fields.get(KEY_CRITICAL_PATH_PATTERNS)
    if raw is None:
        return CriticalPathFacts(
            patterns=DEFAULT_CRITICAL_PATH_PATTERNS,
            patterns_source=SOURCE_DEFAULT,
            warning=declarations.warning,
        )

    where = declarations.path_label or "项目声明"
    patterns, pattern_warning = _compile_patterns(raw, where)
    warnings = "；".join(item for item in (declarations.warning, pattern_warning) if item)
    if patterns is None:
        return CriticalPathFacts(
            patterns=DEFAULT_CRITICAL_PATH_PATTERNS,
            patterns_source=SOURCE_DEFAULT,
            warning=warnings,
        )
    return CriticalPathFacts(
        patterns=patterns,
        patterns_source=SOURCE_PROJECT if patterns else SOURCE_PROJECT_EMPTY,
        warning=warnings,
    )


# ---------------------------------------------------------------------------
# 生成物前缀
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrefixDeclaration:
    """一次分析要用的生成物前缀，以及「它从哪来」。"""

    prefixes: tuple[str, ...]
    source: str
    detail: str
    warning: str = ""

    @property
    def is_empty(self) -> bool:
        """前缀为空 —— 这时**不可能**配出任何一组，且原因已经明确。"""
        return not self.prefixes

    def describe(self, pair_count: int) -> str:
        """「本轮表↔产物配对：N 组（…）」那一行。

        这一行是这次要补的东西本身：在它之前，配对结果不外露，于是「这个项目本来
        就没得配」与「我们根本不会配」长得一模一样。
        """
        prefixes = "、".join(self.prefixes) if self.prefixes else "（空）"
        return f"表↔产物配对：{pair_count} 组（前缀={prefixes}；{self.detail}）"


DEFAULT_PREFIX_DECLARATION = PrefixDeclaration(
    prefixes=tuple(DEFAULT_GENERATED_PREFIXES),
    source=SOURCE_DEFAULT,
    detail="项目未声明生成物前缀，用平台默认值",
)


# ---------------------------------------------------------------------------
# 检查维度清单
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionsDeclaration:
    """本次分析生效的检查维度清单，以及「它从哪来」。

    `source` 与另外两组事实同一套取值（`SOURCE_DEFAULT` / `SOURCE_PROJECT`）：
    「项目没声明、用平台默认」与「项目声明了」必须分得开 —— 只回一份清单而不回来源，
    等于把这次要消灭的「一声不响」换个地方重演（第一版不知道某条结论是按哪份清单得出的）。
    """

    dimensions: tuple[DimensionSpec, ...]
    source: str
    detail: str
    warning: str = ""

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(spec.id for spec in self.dimensions)

    @property
    def labels(self) -> dict[str, str]:
        return {spec.id: spec.label for spec in self.dimensions}


DEFAULT_DIMENSIONS_DECLARATION = DimensionsDeclaration(
    dimensions=DEFAULT_DIMENSION_SPECS,
    source=SOURCE_DEFAULT,
    detail="项目未声明检查维度清单，用平台默认值",
)


def dimensions_of(declarations: Declarations) -> DimensionsDeclaration:
    """把一份（已经读出来的）声明解析成生效的维度清单。

    抽成独立函数是为了让两个入口共用同一套解析：生产走 `dimensions(project_code)`，
    而已经自己定位到知识包的调用方（`skill_loader.load_skills` 手里就有 `pack_dir`）
    可以只读一次文件、再调这里，不必为了拿维度再解析一遍路径。
    """
    raw = declarations.fields.get(KEY_DIMENSIONS)
    if raw is None:
        return replace(DEFAULT_DIMENSIONS_DECLARATION, warning=declarations.warning)

    where = declarations.path_label or "项目声明"
    items, warning = _list_value(raw, what=KEY_DIMENSIONS, where=where)
    warnings = "；".join(item for item in (declarations.warning, warning) if item)
    if items is None:
        # 值是空的（`dimensions:` 后面什么都没有）——读不出任何一项，按「未声明」处理。
        return replace(
            DEFAULT_DIMENSIONS_DECLARATION,
            detail="项目声明的检查维度读不出来，用平台默认值",
            warning=warnings,
        )
    if not items:
        # `none` 对这一项是非法值：见模块 docstring。**不能**照 `generated_prefixes`
        # 那一支写成「空清单」——一个空的维度清单会让每一条异常都进「未归类」。
        return replace(
            DEFAULT_DIMENSIONS_DECLARATION,
            detail="项目把 dimensions 声明成了「没有」，用平台默认值",
            warning="；".join(
                item
                for item in (
                    warnings,
                    f"{where} 的 {KEY_DIMENSIONS} 写成了「没有」；平台不接受一个空的检查维度"
                    "清单（那样每条异常都只能进「未归类」），已按「未声明」处理",
                )
                if item
            ),
        )

    specs, problem = build_dimensions(items)
    if problem:
        return replace(
            DEFAULT_DIMENSIONS_DECLARATION,
            detail="项目声明的检查维度不合法，用平台默认值",
            warning="；".join(
                item
                for item in (
                    warnings,
                    f"{where} 的 {KEY_DIMENSIONS} 不合法，已按「未声明」处理：{problem}",
                )
                if item
            ),
        )
    return DimensionsDeclaration(
        dimensions=specs,
        source=SOURCE_PROJECT,
        detail=f"检查维度来自{declarations.detail}",
        warning=warnings,
    )


def dimensions(
    project_code: Optional[str], *, repo_root: Path = _REPO_ROOT
) -> DimensionsDeclaration:
    """读一个项目声明的检查维度清单。取不到就用平台默认（出厂那九个）。"""
    return dimensions_of(read_declarations(project_code, repo_root=repo_root))


def generated_prefixes(
    project_code: Optional[str], *, repo_root: Path = _REPO_ROOT
) -> PrefixDeclaration:
    """读一个项目声明的生成物前缀。取不到就用平台默认（`DEFAULT_GENERATED_PREFIXES`）。"""
    declarations = read_declarations(project_code, repo_root=repo_root)
    raw = declarations.fields.get(KEY_GENERATED_PREFIXES)
    if raw is None:
        return PrefixDeclaration(
            prefixes=DEFAULT_PREFIX_DECLARATION.prefixes,
            source=SOURCE_DEFAULT,
            detail=DEFAULT_PREFIX_DECLARATION.detail,
            warning=declarations.warning,
        )

    items, warning = _list_value(
        raw, what=KEY_GENERATED_PREFIXES, where=declarations.path_label or "项目声明"
    )
    warnings = "；".join(item for item in (declarations.warning, warning) if item)
    if items is None:
        return PrefixDeclaration(
            prefixes=DEFAULT_PREFIX_DECLARATION.prefixes,
            source=SOURCE_DEFAULT,
            detail="项目声明的生成物前缀读不出来，用平台默认值",
            warning=warnings,
        )
    if not items:
        return PrefixDeclaration(
            prefixes=(),
            source=SOURCE_PROJECT_EMPTY,
            detail="项目声明「没有可配对的生成物前缀」",
            warning=warnings,
        )
    return PrefixDeclaration(
        prefixes=items,
        source=SOURCE_PROJECT,
        detail=f"前缀来自{declarations.detail}",
        warning=warnings,
    )
