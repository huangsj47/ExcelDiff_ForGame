#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按文件选用表头坐标：一套仓库可以并存多种表头格式。

## 为什么需要这一层

`Repository.header_rows` / `header_name_row` / `key_columns` 是**仓库级标量** ——
一个仓库一套坐标，全文件通用。而一个配表仓库里可以并存多种表头格式，实测的
`qz_config` 就有三种：

| 形态 | 表头块 | 字段名行 | A 列语义 | 数据起始 |
|---|---|---|---|---|
| 常规自描述 | 5 行（含 TYPE/DEFAULT/EXPORT 三个标记行） | 第 2 行 | **标记列** | 第 6 行 |
| 编辑器式 | 4 行 | 第 2 行 | **真正的 id 字段** | 第 5 行 |
| 简化（`A1 = SKIP`） | 1 行 | 第 1 行 | 哨兵 + 备注 | 第 2 行 |

**A 列的语义三种完全不同**，所以任何按列号硬编码的解析器在这三套之间必然错列。
标量表达不了这件事，缺的不是「解析能力」，是「**按文件选择**」。

## 这一层不重写解析，只换坐标的来源

坐标的口径与含义**一个字都没改**：`header_rows` 仍是「表头块占前几行（含第 1 行）」，
`header_name_row` 仍是「字段名在第几物理行」，规范化的活仍由
`DiffService._header_row_count` / `_header_name_row`（`services/diff_excel_reader.py`）干。
本模块只回答一个问题：**这张表该用哪一组坐标。**

这也意味着引擎那边几乎不用动：`_compare_excel_data` 本来就是**每文件规范化一次**
（`services/diff_excel_compare.py` 顶部算一次、逐 sheet 下传），按文件选坐标与那个结构
天然契合 —— 调用方换个取值来源即可。

## 没配就是今天的行为

`header_profiles` 为空/NULL ⇒ 走仓库标量（`default_profile_for`），逐字不变。
这条是硬约束：平台里绝大多数仓库不会配这个字段。

## 匹配优先级（从具体到泛化）

1. `file_name`   —— 仓库内相对路径精确匹配（只写 basename 也认）
2. `dir_prefix`  —— 目录前缀，支持 `*`/`**`；**多处命中取最长前缀**
3. `path_regex`  —— 正则，按用户填的顺序取**首个**命中
4. `header_detect` —— 读表头判特征（**只有这一档会去读文件**）
5. 兜底：仓库的三个标量

`dir_prefix` 排在正则前面且「最长优先」，是因为它是这一族里**最不容易写错**的表达
（用户举的例子就是「匹配到 `EditorCfgTool/excel/actor` 的走第二种表头」），
而最长优先让「先写通用规则、再写特例」符合直觉 —— 特例更长，自然赢。

`header_detect` 排最后：前三种都是纯字符串比较、零成本，只有它要打开文件，
所以放在所有便宜判据之后。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from utils.logger import log_print

# 匹配方式。顺序即优先级，`MATCH_KINDS` 同时是校验层的白名单。
MATCH_FILE_NAME = "file_name"
MATCH_DIR_PREFIX = "dir_prefix"
MATCH_PATH_REGEX = "path_regex"
MATCH_HEADER_DETECT = "header_detect"
MATCH_KINDS: Tuple[str, ...] = (
    MATCH_FILE_NAME,
    MATCH_DIR_PREFIX,
    MATCH_PATH_REGEX,
    MATCH_HEADER_DETECT,
)

# 界面上的说明文案。放在这里而不是模板里：校验错误、帮助页、界面提示三处要用同一句话，
# 各写一份必然漂移（本仓库在这类「同一个口径两份副本」上已经栽过 —— 见
# `services/ai/endpoint_service.py` 里 `auto_weekly_enabled` 的那段注释）。
MATCH_LABELS: Dict[str, str] = {
    MATCH_FILE_NAME: "固定文件名",
    MATCH_DIR_PREFIX: "目录前缀",
    MATCH_PATH_REGEX: "路径正则",
    MATCH_HEADER_DETECT: "表头特征",
}

MATCH_HINTS: Dict[str, str] = {
    MATCH_FILE_NAME: "仓库里的相对路径，如 config/奖励模式_CfgRewardMode.xlsx；只写文件名也认",
    MATCH_DIR_PREFIX: "目录前缀，如 EditorCfgTool/excel/actor；支持 * 与 **；多处命中取最长的那条",
    MATCH_PATH_REGEX: "正则，如 ^config/.*\\.xlsx$；忽略大小写；多条命中取靠上的那条",
    MATCH_HEADER_DETECT: "表头判据，如 r1:A == SKIP 或 within30:A has TYPE, EXPORT",
}

# 内置预设：**用通用语汇命名，不含任何具体项目的特征**。
# 用户可以直接引用（绑定里填 `builtin:multi5`），也可以照着新建自己的方案。
BUILTIN_PROFILES: Dict[str, "HeaderProfile"] = {}


def _register_builtins() -> None:
    for key, label, rows, name_row, marker in (
        ("builtin:single", "单行表头", 1, 1, None),
        ("builtin:multi5", "多行表头（5 行，字段名在第 2 行）", 5, 2, "A"),
        ("builtin:editor4", "编辑器式表头（4 行，A 列是 id）", 4, 2, None),
    ):
        BUILTIN_PROFILES[key] = HeaderProfile(
            key=key,
            label=label,
            header_rows=rows,
            header_name_row=name_row,
            marker_column=marker,
        )


@dataclass(frozen=True)
class HeaderProfile:
    """一组表头坐标 + 一个名字。

    `key` 是稳定标识（绑定按它引用方案）；`builtin:*` 是内置预设的保留前缀。
    `reason` 不参与比较，只用于日志与界面回显「这张表为什么用这套坐标」——
    没有它的话，「为什么这张表的列名不对」只能靠人肉重算一遍匹配。
    """

    key: str
    label: str
    header_rows: Optional[int] = None
    header_name_row: Optional[int] = None
    key_columns: Optional[str] = None
    marker_column: Optional[str] = None
    reason: str = ""

    @property
    def is_builtin(self) -> bool:
        return self.key.startswith("builtin:")

    def describe(self) -> str:
        bits = [f"表头 {self.header_rows or 1} 行", f"名称行 {self.header_name_row or 1}"]
        if self.key_columns:
            bits.append(f"关键列 {self.key_columns}")
        if self.marker_column:
            bits.append(f"{self.marker_column} 列是标记列")
        return " · ".join(bits)


@dataclass(frozen=True)
class HeaderBinding:
    """一条「什么文件用哪个方案」的规则。"""

    match: str
    value: str
    profile_key: str
    pattern: Any = None  # 预算好的正则（path_regex / glob），避免每次比对都编译


@dataclass(frozen=True)
class HeaderProfileConfig:
    profiles: Tuple[HeaderProfile, ...] = ()
    bindings: Tuple[HeaderBinding, ...] = ()

    def by_key(self, key: str) -> Optional[HeaderProfile]:
        for profile in self.profiles:
            if profile.key == key:
                return profile
        return BUILTIN_PROFILES.get(key)

    def of_kind(self, kind: str) -> List[HeaderBinding]:
        return [b for b in self.bindings if b.match == kind]


_register_builtins()

# 判据里的列名：A..ZZ。够用了 —— 配表的列数不会到三位数。
_COLUMN_RE = re.compile(r"^[A-Za-z]{1,3}$")
# 范围：`r12` 单行 / `within12` 前 N 行 / `anywhere` 全表（sheet 有多少行看多少行）。
_SCOPE_RE = re.compile(r"^(r|within|anywhere)(\d*)$", re.IGNORECASE)
# 一个字面量条件：`<范围>:<列> <op> <值>`
_CONDITION_RE = re.compile(
    r"^(?P<scope>r\d+|within\d*|anywhere)\s*:\s*(?P<column>[A-Za-z]{1,3})\s*"
    r"(?P<op>==|has)\s*(?P<values>.+)$",
    re.IGNORECASE,
)


def normalize_path(path: Any) -> str:
    """把路径统一成「相对仓库根、正斜杠、无首尾斜杠」的样子。

    三件事都是必须的：git 里存的是 `/` 而用户在 Windows 上会填 `\\`；
    UI 回填时可能带上仓库根前缀的 `./`；首尾斜杠会让「前缀相等」判错。
    """
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def normalize_column(raw: Any) -> Optional[str]:
    """列字母规范化成大写；不合法返回 None。"""
    text = str(raw or "").strip().upper()
    if not text or not _COLUMN_RE.match(text):
        return None
    return text


def column_index(raw: Any) -> Optional[int]:
    """列字母 → 0-based 序号（`A` → 0）。"""
    column = normalize_column(raw)
    if column is None:
        return None
    index = 0
    for char in column:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _coerce_int(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value


def _coerce_text(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


# ==========================================================================
# 解析：JSON 文本 → 配置对象
# ==========================================================================


def parse_config(raw: Any) -> HeaderProfileConfig:
    """把仓库上那一列 JSON 文本读成配置。

    **读不出来时当作「没配」而不是抛错**：这一列坏掉的后果应该是「退回仓库标量」，
    而不是「这个仓库的 diff 全线 500」。坏值会打一条日志 —— 静默退回会让
    「我明明配了却不生效」变成一桩无头案。
    """
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if isinstance(raw, (dict, list)):
        payload: Any = raw
    elif not text:
        return HeaderProfileConfig()
    else:
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            log_print(f"⚠️ 表头方案配置读不出来（{type(exc).__name__}: {exc}），已退回仓库默认坐标", "EXCEL", force=True)
            return HeaderProfileConfig()

    if not isinstance(payload, dict):
        return HeaderProfileConfig()

    profiles: List[HeaderProfile] = []
    for item in payload.get("profiles") or []:
        if not isinstance(item, dict):
            continue
        key = _coerce_text(item.get("key"))
        if not key or key.startswith("builtin:"):
            # `builtin:` 是保留前缀：让用户自建的方案占用它，就等于允许「覆盖内置预设」，
            # 而覆盖是静默的 —— 别人看到界面上写着「单行表头」，实际坐标来自某人的自定义。
            continue
        profiles.append(
            HeaderProfile(
                key=key,
                label=_coerce_text(item.get("label")) or key,
                header_rows=_coerce_int(item.get("header_rows")),
                header_name_row=_coerce_int(item.get("header_name_row")),
                key_columns=_coerce_text(item.get("key_columns")),
                marker_column=normalize_column(item.get("marker_column")),
            )
        )

    bindings: List[HeaderBinding] = []
    for item in payload.get("bindings") or []:
        if not isinstance(item, dict):
            continue
        match = str(item.get("match") or "").strip().lower()
        value = _coerce_text(item.get("value"))
        profile_key = _coerce_text(item.get("profile"))
        if match not in MATCH_KINDS or not value or not profile_key:
            continue
        bindings.append(
            HeaderBinding(
                match=match,
                value=value,
                profile_key=profile_key,
                pattern=_compile_matcher(match, value),
            )
        )
    return HeaderProfileConfig(profiles=tuple(profiles), bindings=tuple(bindings))


def _compile_matcher(match: str, value: str):
    """预算匹配器。编译失败的返回 None（`path_regex` 的坏正则由校验层拦，这里只兜底）。"""
    if match == MATCH_PATH_REGEX:
        try:
            return re.compile(value, re.IGNORECASE)
        except re.error:
            return None
    if match == MATCH_DIR_PREFIX and ("*" in value or "?" in value):
        return _glob_prefix_regex(value)
    return None


def _glob_prefix_regex(pattern: str) -> Optional[re.Pattern]:
    """把 `*` / `**` 风格的前缀写成「匹配路径的某个前缀」的正则。

    末尾的 `(?:/|$)` 是关键：`EditorCfgTool/excel/*` 要能匹配
    `EditorCfgTool/excel/actor/xxx.xlsx`（`*` 吃掉 `actor`），但不该匹配
    `EditorCfgTool/excel2/...`。
    """
    out: List[str] = []
    index = 0
    text = pattern.replace("\\", "/")
    while index < len(text):
        if text[index:index + 2] == "**":
            out.append(".*")
            index += 2
        elif text[index] == "*":
            out.append("[^/]*")
            index += 1
        elif text[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(text[index]))
            index += 1
    try:
        return re.compile("^" + "".join(out) + r"(?:/|$)", re.IGNORECASE)
    except re.error:
        return None


# ==========================================================================
# 表头判据
# ==========================================================================


def parse_detection(expression: str) -> List[Dict[str, Any]]:
    """把一行判据解成条件列表；解不出来返回空列表（调用方当作「判据不成立」）。

    语法（`&&` 连接多个条件，每个条件一个）：

        r1:A == SKIP                     第 1 行的 A 列等于字面量 SKIP
        within30:A has TYPE, EXPORT       前 30 行的 A 列里 TYPE 与 EXPORT 都出现过
        anywhere:A has 基础字段            整张表的 A 列里出现过「基础字段」

    两个分隔符的分工是硬的：**`&&` 只连接条件，`,` 只分隔一个 `has` 里的多个值**。
    值里的空格是**有意义的**（中文表头常带空格），所以只 strip 首尾。
    """
    text = str(expression or "").strip()
    if not text:
        return []
    conditions: List[Dict[str, Any]] = []
    for chunk in text.split("&&"):
        chunk = chunk.strip()
        if not chunk:
            return []
        parsed = _parse_condition(chunk)
        if parsed is None:
            return []
        conditions.append(parsed)
    return conditions


def _parse_condition(chunk: str) -> Optional[Dict[str, Any]]:
    match = _CONDITION_RE.match(chunk)
    if match is None:
        return None
    scope_raw = match.group("scope").lower()
    scope = _SCOPE_RE.match(scope_raw)
    if scope is None:
        return None
    kind = scope.group(1).lower()
    if kind == "anywhere":
        within = None
    else:
        within = _coerce_int(scope.group(2))
        if within is None or within < 1:
            return None
    column = normalize_column(match.group("column"))
    if column is None:
        return None
    op = match.group("op").lower()
    raw_values = match.group("values").strip()
    if op == "has":
        # **多值用逗号分隔，不用 `&&`。** `&&` 是条件连接符；如果它同时当值分隔符，
        # `within30:A has TYPE && EXPORT` 就有两种读法（两个条件 / 一个条件两个值），
        # 而解析器只会挑一种 —— 用户写的和他以为的可以不是一回事。
        # 值里的空格是**有意义的**（中文表头常带空格），所以只 strip 首尾。
        values = [piece.strip() for piece in re.split(r"[,，]", raw_values) if piece.strip()]
        if not values:
            return None
        return {"kind": kind, "within": within, "column": column, "op": "has", "values": values}
    if op == "==":
        if kind != "r":
            # `withinN:A == X` 没有意义（前 N 行里「等于 X」的行通常不止一行）。
            return None
        return {"kind": "r", "within": within, "column": column, "op": "==", "values": [raw_values]}
    return None


def evaluate_detection(
    expression: str, probe: Callable[[str, Optional[int]], Sequence[str]]
) -> bool:
    """判据成立吗。

    `probe(column, within_rows)` 返回**该列前 N 行的原始文本**（`within_rows=None`
    表示整表），**含空串、按行对齐** —— 第 i 个元素对应第 i+1 行。

    为什么必须是「按行对齐、含空串」而不是「非空值的列表」：`r2:A == id` 问的是
    **第 2 行那一格**。如果 probe 把空行滤掉了，一个空行就会让「第 2 行」变成
    「第 3 行」，而判据照样给出 True/False —— 错得没有任何痕迹。滤空是 `has`
    自己该做的事（它问的是「出现没出现过」），不是 probe 该做的。

    做成回调是因为本模块要保持纯函数可测：真实的读表在调用方
    （`services/excel_header_probe.py`），测试直接喂一串假值。

    判据解不出来时返回 False —— **不是** True。一个写错的判据如果默认成立，
    后果是整个仓库的表都被套上错误的坐标，而且不报错。
    """
    conditions = parse_detection(expression)
    if not conditions:
        return False
    for condition in conditions:
        try:
            cells = list(probe(condition["column"], condition["within"]) or [])
        except Exception as exc:  # noqa: BLE001 —— 读不出表头不该让整次 diff 失败
            log_print(
                f"⚠️ 表头判据求值失败（{type(exc).__name__}: {exc}），这条规则按不成立处理",
                "EXCEL",
                force=True,
            )
            return False
        if condition["op"] == "==":
            index = (condition["within"] or 1) - 1
            if index >= len(cells) or str(cells[index]).strip() != condition["values"][0]:
                return False
        else:
            present = {str(cell).strip() for cell in cells if str(cell).strip()}
            if not all(value in present for value in condition["values"]):
                return False
    return True


def validate_detection(expression: str) -> Optional[str]:
    """判据能不能解析；能就返回 None，不能就返回一句给人看的原因。"""
    text = str(expression or "").strip()
    if not text:
        return "表头特征判据不能为空"
    for chunk in text.split("&&"):
        chunk = chunk.strip()
        if not chunk:
            return "判据里有一个空条件（多余的 &&？）"
        if _CONDITION_RE.match(chunk) is None:
            return (
                f"读不懂这个条件：{chunk!r}。"
                "写法是「范围:列 运算符 值」，范围形如 r1 / within30 / anywhere，"
                "运算符是 == 或 has"
            )
        if _parse_condition(chunk) is None:
            return f"这个条件说不通：{chunk!r}（within 行数要大于 0；== 只能配 r 单行）"
    return None


# ==========================================================================
# 匹配
# ==========================================================================


def _dir_prefix_length(pattern: HeaderBinding, norm_path: str) -> int:
    """命中返回匹配到的长度（越长越优先）；不命中返回 -1。"""
    if pattern.pattern is not None:
        found = pattern.pattern.match(norm_path)
        return found.end() if found else -1
    prefix = normalize_path(pattern.value).lower()
    if not prefix:
        return -1
    path = norm_path.lower()
    if path == prefix:
        return len(prefix)
    if path.startswith(prefix + "/"):
        return len(prefix)
    return -1


def _file_name_hit(binding: HeaderBinding, norm_path: str) -> bool:
    value = normalize_path(binding.value).lower()
    if not value:
        return False
    path = norm_path.lower()
    if path == value:
        return True
    # 只写了文件名时按 basename 比。这条便利是有代价的（不同目录下的同名文件会一起命中），
    # 所以界面上的提示写的是「写全路径更精确」。
    return "/" not in value and path.rsplit("/", 1)[-1] == value


def _regex_hit(binding: HeaderBinding, norm_path: str) -> bool:
    if binding.pattern is None:
        return False
    return binding.pattern.search(norm_path) is not None


def default_profile_for(repository, reason: str = "default") -> HeaderProfile:
    """仓库的三个标量 → 兜底方案。**没配 `header_profiles` 时走的就是它。**"""
    return HeaderProfile(
        key="default",
        label="仓库默认",
        header_rows=getattr(repository, "header_rows", None),
        header_name_row=getattr(repository, "header_name_row", None),
        key_columns=getattr(repository, "key_columns", None),
        marker_column=None,
        reason=reason,
    )


def resolve_for_file(
    repository,
    file_path: Any,
    *,
    probe: Optional[Callable[[str, Optional[int]], Sequence[str]]] = None,
    config: Optional[HeaderProfileConfig] = None,
) -> HeaderProfile:
    """这张表该用哪一组坐标。

    `probe` 只有配了 `header_detect` 规则时才会被调用；不传就等于「不做表头特征匹配」，
    前三种纯路径规则照常工作。**只有这一档会去读文件** —— diff 主路径上不传 probe
    就与今天一样，一次多余的解析都没有。
    """
    cfg = config if config is not None else parse_config(getattr(repository, "header_profiles", None))
    if not cfg.bindings:
        return default_profile_for(repository)

    norm_path = normalize_path(file_path)

    def pick(binding: HeaderBinding, how: str) -> Optional[HeaderProfile]:
        profile = cfg.by_key(binding.profile_key)
        if profile is None:
            return None
        return replace(profile, reason=f"{how}（{MATCH_LABELS.get(binding.match, binding.match)}：{binding.value}）")

    # 1. 固定文件名 —— 最具体
    for binding in cfg.of_kind(MATCH_FILE_NAME):
        if _file_name_hit(binding, norm_path):
            hit = pick(binding, "命中固定文件名")
            if hit is not None:
                return hit

    # 2. 目录前缀 —— 多处命中取**最长**的
    best: Optional[Tuple[int, HeaderProfile]] = None
    for binding in cfg.of_kind(MATCH_DIR_PREFIX):
        length = _dir_prefix_length(binding, norm_path)
        if length < 0:
            continue
        hit = pick(binding, "命中目录前缀")
        if hit is None:
            continue
        if best is None or length > best[0]:
            best = (length, hit)
    if best is not None:
        return best[1]

    # 3. 路径正则 —— 按用户填的顺序取首个命中
    for binding in cfg.of_kind(MATCH_PATH_REGEX):
        if _regex_hit(binding, norm_path):
            hit = pick(binding, "命中路径正则")
            if hit is not None:
                return hit

    # 4. 表头特征 —— 唯一需要读文件的一档，放最后
    if probe is not None:
        for binding in cfg.of_kind(MATCH_HEADER_DETECT):
            if evaluate_detection(binding.value, probe):
                hit = pick(binding, "命中表头特征")
                if hit is not None:
                    return hit

    return default_profile_for(repository, reason="没有规则命中")


def header_kwargs_for(
    repository,
    file_path: Any,
    *,
    raw: Optional[bytes] = None,
    sheet_name: str = "",
    only: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """引擎调用点的收口：一次拿到四个关键字参数。

    调用点从「三行各写一遍的 `getattr`」变成一行 `**header_kwargs_for(...)` ——
    这里才是「一个仓库的坐标从哪来」的**唯一**答案。原先那三行在 8 处重复，
    而重复的代价不是啰嗦：任何一处漏改，那张表就会用另一套坐标去读，
    表现是「同一张表在两种页面上列头不一样」，而且不报错。

    `raw` 传文件字节才会启用「表头特征」匹配（需要真去读表头）；不传就等于不做
    那一档，前三种纯路径规则照常工作。**探测器是惰性的**：只有真的配了
    `header_detect` 规则才会 import openpyxl 并打开工作簿。

    `only` 只取其中几项，给那些**不收全部四个**的入口用。目前只有一处：
    AI 渲染正文（`_read_excel_sheets`）只用坐标，不用 `marker_column` ——
    那一列的语义是「diff 时不算数据变更」，而模型读正文是「看内容」，
    把备注列从正文里抹掉只会让它少看到东西。
    """
    config = parse_config(getattr(repository, "header_profiles", None))
    probe = None
    if raw is not None and any(b.match == MATCH_HEADER_DETECT for b in config.bindings):
        # 延迟 import：没配表头特征的仓库，这条路径上连 openpyxl 都不加载。
        from services.excel_header_probe import make_probe

        probe = make_probe(raw, sheet_name=sheet_name)
    profile = resolve_for_file(repository, file_path, probe=probe, config=config)
    kwargs: Dict[str, Any] = {
        "key_columns": profile.key_columns,
        "header_rows": profile.header_rows,
        "header_name_row": profile.header_name_row,
        "marker_column": profile.marker_column,
    }
    if only is not None:
        kwargs = {name: kwargs[name] for name in only}
    return kwargs


# ==========================================================================
# 校验（表单 / 接口用）
# ==========================================================================


def validate_config(payload: Any) -> List[str]:
    """配置能不能用。返回**给人看的中文原因**列表，空列表 = 通过。

    这一层要比 `parse_config` 严：`parse_config` 是读取侧，它对坏值的态度是「跳过、
    退回默认」（不能让一条坏配置把 diff 打死）；这里是写入侧，坏值必须**拦下来**
    —— 放进去的东西读的时候会被静默丢掉，用户看到「保存成功」而配置不生效。
    """
    if payload in (None, "", []):
        return []
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as exc:
            return [f"这段 JSON 读不出来：{exc}"]
    if not isinstance(payload, dict):
        return ["配置的最外层要是一个对象（含 profiles 与 bindings 两项）"]

    errors: List[str] = []
    seen_keys = set()
    for index, item in enumerate(payload.get("profiles") or [], start=1):
        where = f"方案 {index}"
        if not isinstance(item, dict):
            errors.append(f"{where}：应该是一个对象")
            continue
        key = _coerce_text(item.get("key"))
        if not key:
            errors.append(f"{where}：缺少标识 key")
        elif key.startswith("builtin:"):
            errors.append(f"{where}：key 不能用 builtin: 开头（那是内置预设的保留前缀）")
        elif key in seen_keys:
            errors.append(f"{where}：key「{key}」重复了")
        else:
            seen_keys.add(key)
        if not _coerce_text(item.get("label")):
            errors.append(f"{where}：缺少名称")
        rows = _coerce_int(item.get("header_rows"))
        if rows is None or rows < 1:
            errors.append(f"{where}：表头行数要是大于 0 的整数")
        name_row = _coerce_int(item.get("header_name_row"))
        if name_row is not None and name_row < 1:
            errors.append(f"{where}：名称行要是大于 0 的整数")
        if rows is not None and name_row is not None and rows >= 1 and name_row > rows:
            errors.append(f"{where}：名称行（{name_row}）不能超过表头行数（{rows}）")
        marker = item.get("marker_column")
        if marker not in (None, "") and normalize_column(marker) is None:
            errors.append(f"{where}：标记列要写成列字母，如 A")

    known_keys = {p.key for p in parse_config(payload).profiles} | set(BUILTIN_PROFILES)
    for index, item in enumerate(payload.get("bindings") or [], start=1):
        where = f"规则 {index}"
        if not isinstance(item, dict):
            errors.append(f"{where}：应该是一个对象")
            continue
        match = str(item.get("match") or "").strip().lower()
        if match not in MATCH_KINDS:
            errors.append(f"{where}：匹配方式要是 {'/'.join(MATCH_KINDS)} 之一")
            continue
        value = _coerce_text(item.get("value"))
        if not value:
            errors.append(f"{where}：匹配内容不能为空")
        elif match == MATCH_PATH_REGEX:
            try:
                re.compile(value)
            except re.error as exc:
                errors.append(f"{where}：正则写错了 —— {exc}")
        elif match == MATCH_HEADER_DETECT:
            reason = validate_detection(value)
            if reason:
                errors.append(f"{where}：{reason}")
        profile_key = _coerce_text(item.get("profile"))
        if not profile_key:
            errors.append(f"{where}：没有选方案")
        elif profile_key not in known_keys:
            errors.append(f"{where}：找不到方案「{profile_key}」")
    return errors


def all_profile_choices(config: HeaderProfileConfig) -> List[HeaderProfile]:
    """界面下拉里能选的方案：内置预设 + 用户自建。"""
    return list(BUILTIN_PROFILES.values()) + list(config.profiles)
