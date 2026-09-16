"""变更单元（bundle）：把「一次改动里的同一件事」合成的评审粒度。

## 为什么要合

配表驱动的项目里，**一张表与它的生成物是同一次改动**：改了 `[30]道具表_CfgItem.xlsx`，
产物 `CfgItem.lua` 跟着变。把它们当成两个独立的 diff 看，有两个代价：

1. **丢掉最关键的关联。** 「表里加了 ID，生成的代码里有没有对应项」「表删了条目，生成的
   代码还留着旧逻辑」这类问题，只有在两边一起看的时候才看得见；分开看，每一侧都正常。
2. **浪费索取次数。** 模型要为同一个逻辑改动花两次 `file_diff`，而次数是与「能看多少个
   文件」直接挂钩的稀缺资源（见 `context_tools.DEFAULT_MAX_TOOL_REQUESTS`）。

## 识别规则（与项目无关）

按「生成物模块名」这个**共同记号**配对：两个路径的**文件名**里出现同一个
`<前缀><名字>` 记号（默认前缀 `Cfg`，例：`CfgItem`、`CfgModuleSub`），它们就属于同一个
变更单元。表名里的 `[30]道具表_` 前缀、目录、扩展名都不参与匹配。

项目用什么前缀由项目知识包决定（G119 是 `Cfg`，产物是 `CfgXxx.lua`），默认值只是
「配表项目的常见约定」；识别不出记号的路径各自成为一个单元 —— **不猜，也不硬凑**。

## 边界

* 只看**文件名**，不看路径：`config/<id>/CfgItem.xlsx` 与 `build/CfgItem.lua` 也会配成一对
  （这正是想要的）。
* 记号必须**完整**匹配：`CfgItem` 与 `CfgItemSub` 是两对不同的东西，不会被合并。
* 输出顺序按「单元内第一个成员在原序列里的位置」稳定排列，同一批输入永远得到同一个结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, Tuple

from services.ai.scope import normalize_path

# 生成物模块名的前缀。G119 的产物是 `CfgXxx.lua`，表名形如 `[30]道具表_CfgItem.xlsx`。
DEFAULT_GENERATED_PREFIXES = ("Cfg",)

# 单元类型，用于在提示词里说明「这一组为什么在一起」。
KIND_GENERATED_PAIR = "generated_pair"
KIND_SINGLE = "single"

# 记号后的部分至少要有这么多字符，否则 `Cfg.lua` 这种会被当成一个巨大的公共记号。
_MIN_TOKEN_TAIL = 2


@dataclass(frozen=True)
class Bundle:
    """一个评审单元。`members` 是**规范化后的路径**，保持原始出现顺序。"""

    key: str
    kind: str
    members: Tuple[str, ...]

    @property
    def is_multi(self) -> bool:
        return len(self.members) > 1

    @property
    def label(self) -> str:
        """给人看的一句话说明。"""
        if self.kind == KIND_GENERATED_PAIR:
            return f"配表改动 {self.key}（表与其生成物，必须一起看）"
        return self.members[0] if self.members else self.key

    def with_member_first(self, path: str) -> "Bundle":
        """把 `path` 挪到成员列表首位。

        模型索要的是某一个具体文件；把它索要的那个排在最前面，读起来才顺 ——
        否则「我要的是表，回来先看到生成物」会让人以为拿错了。
        """
        if path not in self.members:
            return self
        rest = tuple(item for item in self.members if item != path)
        return Bundle(key=self.key, kind=self.kind, members=(path,) + rest)


def _token_of(path: str, prefixes: Sequence[str]) -> str:
    """取出文件名里的「生成物模块名」记号，取不到返回空串。"""
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    for prefix in prefixes:
        for match in re.finditer(re.escape(prefix) + r"[A-Za-z0-9_]+", stem):
            if len(match.group(0)) - len(prefix) >= _MIN_TOKEN_TAIL:
                return match.group(0)
    return ""


def build_bundles(
    paths: Iterable[str],
    *,
    generated_prefixes: Sequence[str] = DEFAULT_GENERATED_PREFIXES,
) -> Tuple[Bundle, ...]:
    """把路径分组成评审单元。

    **顺序稳定**：单元按「第一个成员出现的位置」排列，成员保持输入顺序。这既是为了输出
    可预期，也是为了让提示词里的清单在两次分析之间可以直接对比。
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = normalize_path(str(raw or ""))
        if not path or path in seen:
            continue
        seen.add(path)
        ordered.append(path)

    tokens: dict[str, list[str]] = {}
    singles: list[str] = []
    position: dict[str, int] = {}
    for index, path in enumerate(ordered):
        token = _token_of(path, generated_prefixes)
        if token:
            tokens.setdefault(token, []).append(path)
        else:
            singles.append(path)
        position.setdefault(path, index)

    bundles: list[Bundle] = []
    for token, members in tokens.items():
        # 同一记号的多个文件就是一个单元；只有一个文件时退化成单文件单元。
        kind = KIND_GENERATED_PAIR if len(members) > 1 else KIND_SINGLE
        bundles.append(Bundle(key=token, kind=kind, members=tuple(members)))
    for path in singles:
        bundles.append(Bundle(key=path, kind=KIND_SINGLE, members=(path,)))

    bundles.sort(key=lambda bundle: min(position[member] for member in bundle.members))
    return tuple(bundles)


def bundle_index(bundles: Iterable[Bundle]) -> Mapping[str, Bundle]:
    """路径 → 它所属的单元。用于「模型索要某个路径时，顺带把同单元的一起给它」。"""
    return {member: bundle for bundle in bundles for member in bundle.members}


def companions_of(
    bundles: Iterable[Bundle],
    path: str,
) -> Tuple[str, ...]:
    """返回 `path` 所在单元的**其它**成员（不含它自己）。

    取数入口用它把「表 + 生成物」一次性返回。`path` 不属于任何单元时返回空元组 ——
    调用方照常只返回它自己，不需要特判。
    """
    target = normalize_path(str(path or ""))
    bundle = bundle_index(bundles).get(target)
    if bundle is None:
        return ()
    return tuple(member for member in bundle.members if member != target)


def describe_bundles(bundles: Iterable[Bundle], *, limit: int = 0) -> list[str]:
    """把多成员单元渲染成几行说明，用于在变更清单里点出「这几件事是一件事」。

    只列多成员单元：单文件单元没有需要说明的关联，列出来只是噪音。
    """
    multi = [bundle for bundle in bundles if bundle.is_multi]
    lines: list[str] = []
    for bundle in multi[: limit or None]:
        lines.append(f"- {bundle.label}：{' ｜ '.join(bundle.members)}")
    if limit and len(multi) > limit:
        lines.append(f"（另有 {len(multi) - limit} 组同类关联未列出。）")
    return lines
