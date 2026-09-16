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


@dataclass(frozen=True)
class AnalysisScope:
    """一次分析里被允许引用的东西。"""

    # 本批次真实存在的 commit 全哈希。
    commits: tuple[str, ...] = ()
    # 全哈希 -> 该 commit 实际改动过的文件路径集合。
    paths_by_commit: Mapping[str, frozenset[str]] = field(default_factory=dict)
    # 可读的文档名（skill 的 references 与项目知识文档）。
    readable_references: frozenset[str] = frozenset()

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
