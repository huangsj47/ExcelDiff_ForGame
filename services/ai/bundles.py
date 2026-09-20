"""变更单元（bundle）：把「一次改动里的同一件事」合成的评审粒度。

## 为什么要合

配表驱动的项目里，一张表与它的生成物通常由**同一次导表**产生：改了
`[30]道具表_CfgItem.xlsx`，产物 `CfgItem.lua` 跟着变。把它们当成两个独立的 diff 看，
有两个代价：

1. **丢掉最关键的关联。** 「表里加了 ID，生成的代码里有没有对应项」「表删了条目，生成的
   代码还留着旧逻辑」这类问题，只有在两边一起看的时候才看得见；分开看，每一侧都正常。
2. **浪费索取次数。** 模型要为同一个逻辑改动花两次 `file_diff`，而次数是与「能看多少个
   文件」直接挂钩的稀缺资源（见 `context_tools.DEFAULT_MAX_TOOL_REQUESTS`）。

**但「表与生成物必须一起看」是项目事实，不是平台事实。** 平台手里只有文件名，它
**不知道**两个文件之间到底是什么关系（见下面「识别规则」）。所以平台在这一层只做一件事：
把「这几个文件里出现了同一个记号」这个**可观测的事实**说出来，并明确标成「待确认」；
那句领域规则由项目知识包自己写（G119 写在 `references/config-table-spec.md`）。

## 识别规则（与项目无关）

按「生成物模块名」这个**共同记号**配对：两个路径的**文件名**里出现同一个
`<前缀><名字>` 记号（默认前缀 `Cfg`，例：`CfgItem`、`CfgModuleSub`），它们就被放进
同一个单元。表名里的 `[30]道具表_` 前缀、目录、扩展名都不参与匹配。

前缀**由项目自己声明**：`services/ai/project_facts.py` 从项目知识包的
`references/project-facts.md` 读 `generated_prefixes`（不声明时用平台默认值；声明成
`none` 表示「本项目没有可配对的产物前缀」）。`build_bundles` 的 `generated_prefixes`
形参就是这条通道的接口 —— 在这之前它是个**没有任何生产调用方传过**的形参，
于是不叫 `CfgXxx` 的项目里配对恒为 0 组，而且不报错、不留痕。

识别不出记号的路径各自成为一个单元 —— **不猜，也不硬凑**。

## 两个条件，都来自线上真实数据

拿 G119 线上一次周版本的**全部 767 个改动文件**跑过一遍，结果是 762 个单元里只有 2 组
多成员，而且两组都是错的。两次误配各自暴露了一个必须补上的条件：

1. **记号必须从名字分量的起点开始。** 生成物叫 `<模块名>CfgMod.lua`（`BagAttrCfgMod`、
   `DramaCfgMod`、`RoleAttrCfgMod`、`SeasonRankCfgMod`、`TrapCfgMod`），`Cfg` 出现在
   名字**中间**、后面拖着共有的 `Mod`。只按「`Cfg` + 后续字符」抓，这 5 个互不相干的
   lua 会共用一个记号 `CfgMod` 被并成一组 —— 等于在提示词里断言「这 5 个文件是一次改动」。
2. **一组里必须同时有「表」和「生成物」。** `奖励模式_CfgRewardMode.xlsx` 与
   `奖励模式表_CfgRewardMode.xlsx` 都是表，记号相同但**没有生成物**；配对说明写的是
   「表与其生成物，必须一起看」，把它们并起来就是把一句不存在的话写进提示词。

同一份数据里，**真正该配对的那些反而配不上**。项目的知识包里写着「导表产物是
`CfgXxx.lua`」、表名形如 `[30]道具表_CfgItem.xlsx`，规则就是照这个写的；但**这一次
改动到的产物**都是 `code/qz_pub/cfg/<模块名>CfgMod.lua` 这一种，而它们的表（`角色属性表`、
`背包属性表`、`剧情表` …）在名字层面与产物**没有任何共同记号**：表名是中文、没有 `Cfg`，
产物里的模块名是英文。名字配对在这里做不到，**也不该硬做**（靠猜「RoleAttr 就是
角色属性」配出来的对，猜错了没人会发现）。所以这个项目里 bundle 会长期是 0 组 ——
这是**如实**的结果，不是没生效：宁可什么都不说，也不说一句「这几个文件是一件事」
而其实不是。**「0 组」这件事现在会被记一笔**（见 `change_set.build` 里的
`PrefixDeclaration.describe`），这样「这个项目本来就没得配」与「我们根本不会配」
才分得开。

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

# 生成物模块名的前缀。G119 的知识里写的产物是 `CfgXxx.lua`、表名形如 `[30]道具表_CfgItem.xlsx`，
# 这个默认值就是照那个项目的约定取的 —— 它**只是默认值**：项目可以在知识包里声明自己的
# 前缀（见 `project_facts.generated_prefixes`），声明了就以声明为准。
DEFAULT_GENERATED_PREFIXES = ("Cfg",)

# 单元类型，用于在提示词里说明「这一组为什么在一起」。
# `KIND_GENERATED_PAIR` 这个名字是历史叫法（取值 `generated_pair` 也照旧）—— 它现在的
# 含义是「同记号跨了表与产物两侧」，**不是**「平台确认了这是表与其生成物」：平台判这一组
# 的唯一依据是文件名（见 `_spans_table_and_code`），它没有任何办法核实两者的真实关系。
KIND_GENERATED_PAIR = "generated_pair"
KIND_SINGLE = "single"

# 「表」那一侧的文件后缀。判定「这一组算不算一次改动」需要知道记号两侧是不是**不同
# 性质**的产物（见 `_spans_table_and_code`），所以这里只需要分得出「表」与「非表」，
# 不需要认全所有配表格式。
_TABLE_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv")

# 记号后的部分至少要有这么多字符，否则 `Cfg.lua` 这种会被当成一个巨大的公共记号。
_MIN_TOKEN_TAIL = 2

# 记号必须从**名字分量的起点**开始。前缀前不能是 ASCII 字母数字：
# `SeasonRankCfgMod` 里的 `Cfg` 前面是 `k` → 不是分量起点，不认；
# `[30]道具表_CfgItem`（前面是 `_`）与 `图标表CfgItem`（前面是中文）→ 认。
# 下划线与中文都算分量边界，因为这两种写法在配表项目里都是分隔符。
# 必须按 **ASCII** 判，不能用 `\w`：中文在 `\w` 里算字母数字，上面那个中文例子会被误判。
_NOT_AT_COMPONENT_START = re.compile(r"[A-Za-z0-9]")


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
        """给人看的一句话说明。

        **只写平台能观测到的事实**：平台看到的是「这几个文件名里有同一个记号」，
        它没有核实过这些文件之间是什么关系。所以这里不写「表与其生成物」这种断言 ——
        在别的项目里 `Item.csv` 与同名脚本 `Item.py` 也会凑成一对，那时「必须一起看」
        就是一句平台自己都不知道真假的话。领域规则（表与产物为什么该一起看）属于
        项目知识包，见模块文档。
        """
        if self.kind == KIND_GENERATED_PAIR:
            return (
                f"同一记号「{self.key}」的 {len(self.members)} 个文件"
                "（疑似同一次改动，待确认）"
            )
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
    """取出文件名里的「生成物模块名」记号，取不到返回空串。

    前缀必须落在**名字分量的起点**上，理由见模块文档第 1 条（`<模块名>CfgMod.lua` 这种
    命名会让中间那个 `Cfg` 变成一个全项目共有的假记号）。
    """
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    for prefix in prefixes:
        for match in re.finditer(re.escape(prefix) + r"[A-Za-z0-9_]+", stem):
            if len(match.group(0)) - len(prefix) < _MIN_TOKEN_TAIL:
                continue
            start = match.start()
            if start > 0 and _NOT_AT_COMPONENT_START.match(stem[start - 1]):
                continue
            return match.group(0)
    return ""


def _is_table_side(path: str) -> bool:
    return path.lower().endswith(_TABLE_SUFFIXES)


def _spans_table_and_code(members: Sequence[str]) -> bool:
    """这一组的成员是不是跨越了「表」和「生成物」两侧。

    全是表、或全是生成物时返回 False：那时共有的记号只是这个项目里的命名约定
    （`<模块名>CfgMod.lua` 就是），不是「一次改动」的证据。理由见模块文档第 2 条。
    """
    return len({_is_table_side(path) for path in members}) > 1


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
        if len(members) > 1 and _spans_table_and_code(members):
            bundles.append(
                Bundle(key=token, kind=KIND_GENERATED_PAIR, members=tuple(members))
            )
            continue
        # 只有一个成员，或者同记号的成员**全在同一侧** → 不成一组，各自成为一个单元。
        # 后一种情况下那个记号不是「模块名」，而是项目共有的命名约定（见模块文档），
        # 并成一组等于在提示词里断言「这几个文件是一次改动」。
        for path in members:
            bundles.append(Bundle(key=path, kind=KIND_SINGLE, members=(path,)))
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
    """把多成员单元渲染成几行说明，用于在变更清单里点出「这几个文件名有关联」。

    只列多成员单元：单文件单元没有需要说明的关联，列出来只是噪音。
    说明的措辞一律是**平台可观测的事实 + 待确认**（见 `Bundle.label`），不下断言。
    """
    multi = [bundle for bundle in bundles if bundle.is_multi]
    lines: list[str] = []
    for bundle in multi[: limit or None]:
        lines.append(f"- {bundle.label}：{' ｜ '.join(bundle.members)}")
    if limit and len(multi) > limit:
        lines.append(f"（另有 {len(multi) - limit} 组同类关联未列出。）")
    return lines
