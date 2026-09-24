"""分析的合法作用域：模型能引用什么、能读什么。

## 为什么单独一个模块

「哪些 commit 合法」「某个路径是否属于该 commit 改动过的文件」「哪些文档可读」这三件
事同时被两处需要：**输出接地校验**（异常条目里的 commit/path 必须真实）与**工具调用
白名单**（模型索要上下文时的作用域限制）。放在任一方都会让两个模块互相依赖，所以独立
出来。

## 接地校验为什么不能靠「相信模型」

模型编一个看起来合理的 commit 短哈希、或者要一个这个提交根本没改过的文件，是很容易
发生的（它见过太多类似仓库）。如果不校验：

* 异常条目会指向一个不存在的提交，前端渲染出一个点不开的链接，跟进的人查不到东西；
* 工具白名单失守就意味着**模型可以诱导服务端去读任意文件**——把仓库外的内容拉进
  提示词，甚至把 .env 之类的敏感文件带进模型上下文。

所以作用域是**默认拒绝**的：不在允许集合里的，一律丢弃并记账。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping


def normalize_path(raw: str) -> str:
    """归一化模型给出的文件路径。

    模型经常把分隔符写成反斜杠（它见过太多 Windows 仓库），也可能加一个 `./` 前缀，
    还可能在首尾带上空白或引号。这些都不改变它指的是哪个文件，所以归一化后再比对，
    而不是因为写法差异就判成越权。
    """
    text = str(raw or "").strip().strip("'\"").strip()
    if not text:
        return ""
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _as_int(raw) -> int | None:
    """仓库 id 转整数（转不出来返回 `None`，**不猜**）。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value


def build_entries(triples: Iterable[tuple]) -> tuple[ScopeEntry, ...]:
    """把 `(仓库, 提交, 路径)` 原始三元组归一化成一份**确定性**的 entries。

    三件事一起做：路径归一化、去重、排序（同一份输入两次构造得到逐字节相同的元组 ——
    它会被写进日志与断言里，顺序不稳的话「两次分析为什么不一样」就没法回答）。

    读不出仓库 id、或提交/路径为空的条目**丢掉**（`_note_repository` 同一条口径：
    宁可少一条归属退回旧行为，也不要拿一个猜出来的仓库去收窄查询）。
    """
    unique: set[ScopeEntry] = set()
    for raw in triples:
        try:
            repository_id, commit_id, path = raw
        except (TypeError, ValueError):
            continue
        repo = _as_int(repository_id)
        commit = str(commit_id or "").strip()
        normalized = normalize_path(path)
        if repo is None or not commit or not normalized:
            continue
        unique.add(ScopeEntry(repository_id=repo, commit_id=commit, path=normalized))
    return tuple(sorted(unique))



@dataclass(frozen=True, order=True)
class ScopeEntry:
    """本批次里**真实存在**的一条三元组：某个仓库的某条提交改动了某个文件。

    ## 为什么「提交 → 仓库集合」加「提交 → 路径集合」不够

    这两张表**各自都是真的**，但把它们**叉乘**起来得到的配对里有编出来的：SVN 修订号只在
    单个仓库内唯一，两个仓库同窗时完全可能都有 revision 42 —— 42 在配置仓改的是
    `config/x.xlsx`、在代码仓改的是 `code/y.lua`。叉乘会同时「授权」`配置仓 + 42 + y.lua`
    与 `代码仓 + 42 + x.xlsx`，而模型点错仓库时平台读的是**另一个仓库的同名文件**（名字
    一样、内容不同）：回执抬头写着它问的那一条，报告照着实读到的内容写，**没有任何一处
    看得出来读错了**。

    所以授权与取数都按三元组判：三个都对上才叫存在。

    ## 空 = **不知道**，不是「什么都不许」

    手工构造的 scope（单提交模式、既有测试）没有这份事实。空时所有以它为准的新判据一律
    **退回旧行为**（不收窄），与加这个字段之前逐字一致 —— 带默认值的新字段，缺省时行为
    不变，这是这一整轮改造的硬约束。
    """

    repository_id: int
    commit_id: str
    path: str


@dataclass(frozen=True)
class AnalysisScope:
    """一次分析里被允许引用的东西。"""

    # 本批次真实存在的 commit 全哈希。
    commits: tuple[str, ...] = ()
    # 全哈希 -> 该 commit 实际改动过的文件路径集合。
    paths_by_commit: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # 全哈希 -> **本批次里带这个提交号的仓库**集合。
    #
    # **SVN 修订号只在单个仓库内唯一**，两个仓库完全可能同时有 revision 42。取数侧
    # （`platform_provider._commit_row` / `_commit_rows`）查的是一张**跨仓库共用**的
    # `commits_log` 表，不带仓库维度时它会按 `id` 取到另一个仓库那一行 —— 于是 A 项目
    # 的分析里冒出 C 项目的文件（REV-AI-001）。这里把「本批次哪些仓库有这个号」交下去。
    #
    # 空 / 缺省 = **不知道**（手工构造的 scope、单提交模式）：那时取数侧不收窄，与
    # 加这个字段之前的行为逐字一致。
    repository_ids_by_commit: Mapping[str, frozenset[int]] = field(default_factory=dict)
    # 可读的文档名（skill 的 references 与项目知识文档）。
    readable_references: frozenset[str] = frozenset()
    # 路径 -> **这个文件的当前版本落在哪条提交上**（写侧冻结的事实，见 `commit_of_path`）。
    #
    # 为什么不能靠遍历 `commits` 推出来：那个元组的顺序是「**本轮输入**的提交在前，
    # 窗口里其余的按查库顺序在后」（见 `change_set.build`），既不是时间序、也没有
    # 「最后一条最新」这回事。而 `commit_of_path` 原先按「最后一个匹配就赢」取，
    # 于是**本轮输入的文件反而最容易取到窗口里更早的那条提交** —— 实测 run 55：
    # `config/物品表.xlsx` 的本次改动在 `159b068`（皮甲 180→190），却因为
    # `baf3148`（上一轮的铁剑 200→260）排在后面而被选中，预取就把**上一轮的差异**
    # 当成本次差异给了模型，报告照着写了出来。
    #
    # 空 = **不知道**（手工构造的 scope、单提交模式）：那时退回遍历，与加这个字段之前
    # 的行为逐字一致。
    latest_commit_by_path: Mapping[str, str] = field(default_factory=dict)
    # 本轮真正装入模型输入的文件；窗口旧提交仍在 paths_by_commit，供按需取证。
    # None 表示手工构造的旧 scope 未提供此事实，预取退回 batch_paths；
    # 空元组表示本轮明确没有输入文件，不能回退预取窗口历史。
    input_paths: tuple[str, ...] | None = None
    # 本批次里**真实存在**的三元组（见 `ScopeEntry`）。**这是归属，不是授权范围**：
    # 授权仍然是上面那两张表的事，它只回答「这条路径在本批次里属于哪个仓库」。
    #
    # 因此下面几个以它为准的判据一律是三态的：**这个键上有归属**才判是与否，**这个键上
    # 没有归属**一律返回「不知道」并放行给旧的授权判据。写成「不在 entries 里就拒绝」会
    # 让任何一处 attribution 缺失（老 payload 没带 `repository_id`、窗口补偿进来的路径
    # 不在 `delta_files` 里）都变成**静默的假拒绝** —— 而假拒绝的方向最贵：模型收到的是
    # 「这个文件你没资格读」，于是把它写进信息缺口。
    entries: tuple[ScopeEntry, ...] = ()

    def resolve_commit(self, raw: str) -> str | None:
        """把模型给的 commit 标识解析成全哈希。

        接受全哈希与**唯一**的短前缀。前缀有歧义（匹配到多个）时返回 None 而不是
        猜一个 —— 猜错会让异常条目挂到另一个提交上，比直接丢弃更糟。
        """
        text = str(raw or "").strip().lower()
        if not text:
            return None
        for commit in self.commits:
            if commit.lower() == text:
                return commit
        if len(text) < 4:
            # 太短的前缀匹配面太大，没有意义。
            return None
        matches = [commit for commit in self.commits if commit.lower().startswith(text)]
        if len(matches) == 1:
            return matches[0]
        return None

    def changed_paths(self, commit: str) -> frozenset[str]:
        return self.paths_by_commit.get(commit, frozenset())

    def path_allowed(self, commit: str, raw_path: str) -> bool:
        """该路径是否属于这个 commit 改动过的文件。

        这是工具白名单的核心：**模型不能读一个这个提交没碰过的文件**。这条约束把
        「读什么」从模型手里拿回到服务端。
        """
        path = normalize_path(raw_path)
        if not path:
            return False
        return path in self.changed_paths(commit)

    def reference_allowed(self, raw_name: str) -> bool:
        name = str(raw_name or "").strip()
        return bool(name) and name in self.readable_references

    # -- 仓库归属（P1a：`(仓库, 提交, 路径)` 三元组） -------------------------
    #
    # 这四条都遵守同一条契约：**没有归属就说「不知道」**，绝不把「不知道」说成
    # 「不存在」。把空集当否定用，会让 attribution 缺失变成静默的假拒绝。

    def entries_for(self, repository_id, commit_id) -> tuple[ScopeEntry, ...]:
        """某个仓库的某条提交在本批次里改过的文件（空 = 不知道）。

        确定性：同一份 entries 两次调用得到同一个顺序（按 `(仓库, 提交, 路径)` 排序）。
        """
        wanted_commit = str(commit_id or "").strip()
        wanted_repo = _as_int(repository_id)
        if wanted_repo is None or not wanted_commit:
            return ()
        return tuple(
            sorted(
                entry
                for entry in self.entries
                if entry.repository_id == wanted_repo and entry.commit_id == wanted_commit
            )
        )

    def entries_of_path(self, raw_path: str, commit_id: str = "") -> tuple[ScopeEntry, ...]:
        """这条路径在本批次里出现在哪些三元组上（`commit_id` 给了就再收窄一层）。

        空 = **不知道**：可能是这条路径不在本批次里，也可能是这个 scope 根本没有 entries。
        两者要分开只能看 `self.entries` 是否为空。
        """
        path = normalize_path(raw_path)
        if not path:
            return ()
        wanted_commit = str(commit_id or "").strip()
        return tuple(
            sorted(
                entry
                for entry in self.entries
                if entry.path == path and (not wanted_commit or entry.commit_id == wanted_commit)
            )
        )

    def repositories_for_path(self, raw_path: str, commit_id: str = "") -> frozenset[int]:
        """这条路径在本批次里**真实**属于哪几个仓库（空 = 不知道）。

        给「同名路径的归属有歧义」那条拒绝用的：有归属时说得出「它在本批次里属于哪一个」，
        没有归属时只说「不知道」，不编一个候选。
        """
        return frozenset(entry.repository_id for entry in self.entries_of_path(raw_path, commit_id))

    def entry_allowed(self, repository_id, commit_id, raw_path) -> bool | None:
        """这条三元组是不是本批次里真实存在的那一条。

        `None` = **不知道**（这个键上没有归属），调用方照旧走原来的授权判据；`True` /
        `False` = 有归属、这就是判据。

        **为什么是三态**：见 `entries` 字段的说明 —— 把「这个键上没有归属」当成
        `False`，任何一处 attribution 缺失都会变成假拒绝。
        """
        path = normalize_path(raw_path)
        commit = str(commit_id or "").strip()
        repo = _as_int(repository_id)
        if not path or not commit or repo is None:
            return None
        known = self.entries_of_path(path, commit)
        if not known:
            return None
        return any(entry.repository_id == repo for entry in known)

    # -- 批次级的查询（给「一次要看很多文件」的工具用） ----------------------

    def batch_paths(self) -> tuple[str, ...]:
        """本批次改动过的**全部**路径，去重且保持提交顺序（确定性）。

        `find_references` 用它当搜索范围。顺序是确定的：同一批次两次搜索得到同一份列表，
        于是「扫了前 N 个」这句话可复现 —— 随机顺序会让「为什么没搜到那个文件」变得没法解释。
        """
        seen: list[str] = []
        for commit in self.commits:
            for path in sorted(self.paths_by_commit.get(commit, ())):
                if path not in seen:
                    seen.append(path)
        return tuple(seen)

    def commit_of_path(self, raw_path: str) -> str | None:
        """这个路径**当前那一版**落在哪条提交上（周版本里就是它最后一次被改的那条）。

        取最后一次：搜索要看的是这个文件当前的样子的最近一次改动之后的样子，而周版本里
        同一个文件被多条提交改过是常态（合并 diff 就是为这件事存在的）；预取也正是拿这个
        值去取「这个文件在本批次里的差异」。

        ## 判据优先取**冻结的事实**，不靠 `commits` 的遍历顺序

        `self.commits` 的顺序是「本轮输入的提交在前、窗口其余在后」，**不是时间序** ——
        所以「遍历取最后一个匹配」会把更早的提交判成「最新」（实测 run 55 的张冠李戴，
        见 `latest_commit_by_path` 的字段说明）。有冻结事实时先查它；没有时（手工构造的
        scope）才退回遍历，语义与从前一致。
        """
        path = normalize_path(raw_path)
        if not path:
            return None
        known = self.latest_commit_by_path.get(path)
        if known:
            return known
        found: str | None = None
        for commit in self.commits:
            if path in self.paths_by_commit.get(commit, ()):
                found = commit
        return found

    def prefix_allowed(self, raw_prefix: str) -> bool:
        """这个前缀（目录或文件）**至少匹配到本批次的一个改动文件**。

        与 `path_allowed` 是同一个纪律的两个形态：那个管「能不能读这一个文件」，
        这个管「能不能把搜索范围缩到这一片」。允许一个匹配不到任何东西的前缀没有意义
        （结果必然是「没命中」，而模型会把它读成「那里没有引用」），所以直接丢掉并说明。
        """
        prefix = normalize_path(raw_prefix)
        if not prefix:
            return False
        return any(path == prefix or path.startswith(prefix) for path in self.batch_paths())

    @classmethod
    def from_iterables(
        cls,
        *,
        commits: Iterable[str],
        paths_by_commit: Mapping[str, Iterable[str]],
        readable_references: Iterable[str],
    ) -> "AnalysisScope":
        return cls(
            commits=tuple(commits),
            paths_by_commit={
                commit: frozenset(normalize_path(path) for path in paths if normalize_path(path))
                for commit, paths in paths_by_commit.items()
            },
            readable_references=frozenset(readable_references),
        )
