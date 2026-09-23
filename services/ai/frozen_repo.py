#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冻结仓库的**只读访问**：把「本次分析读的是哪个版本」从模型手里拿回服务端。

## 它解决什么（工作包 D 的头号信息缺口）

run 45/46 的报告都把「伤害函数旧名是否仍被其它模块调用」写成信息缺口。根因不是额度不够，
而是**范围**：`platform_provider.find_references` 只用 `scope.batch_paths()` 建索引，
`file_content` 的白名单也只允许本批次路径。于是「小 diff 恰好改了公共接口」时，模型
既不能证伪「调用方没改」（调用方不在批次里，搜不到），也不能证实它（读不到）。

这一层把范围放宽到**本次仓库冻结 tip 的 Git 跟踪文件**，同时把三件事钉死：

1. **版本由服务端定**。tip 来自 `repository.last_synced_tip`（工作包 A 之后它就是
   「这一轮缓存的提交都可达的那个 tip」），模型给的 commit / 分支一概忽略。读取走
   `commit.tree[path]`（不可变对象）——**绝不读可变工作区**：工作副本此刻的内容可能与
   本轮的快照不是一回事（同步每 2~3 分钟一跑），读它等于给模型一份「没有版本的证据」。
2. **路径先过纯函数判据**（`resolve_repo_path`）：绝对路径、盘符、UNC、`..`、控制字符
   一律拒；拒的时候**给理由**（见「拒绝要给理由」一节）。
3. **命中范围要如实记账**：读不到就说读不到，拿不准就说不确定 —— 这个模块里没有一处
   「读失败 → 空结果」的降级。

## 为什么「找不到仓库」必须与「没有命中」分开

`find_references` 的返回值如果是一段空文本，「没有调用方」与「我们根本没搜」在模型眼里
长得一模一样，而它会选那句能写进结论的。所以这一层把三态分开：
`reader` 拿不到 → `None`（调用方必须报「无法检索」）；路径不在跟踪树里 → `False`
（确定结论，可以直说）；判不出来 → `None` 且带 `reason`。调用方按 `Optional[bool]` 收。

## 拒绝要给理由（而不是只回一句「拒绝」）

越权请求的**原因**是模型下一轮唯一能改正的东西。实测里「被拒」被读成「平台取数失败」，
于是报告里多出一条假的信息缺口（`protocol.sanitize_requests` 的注释记着那次 28 条被拒）。
所以本模块的每个拒绝理由都写成「你给的是什么 + 为什么不行 + 能改成什么」，
由 `_reject_text` 渲染给模型。

## 缓存：跨运行复用，但键里必须有 tip

Git 取数与搜索索引可以跨运行复用（同一份快照上重复读同一个 blob 是纯浪费），
但**缓存键含仓库 ID 与冻结 tip**：tip 是 sha，强推之后 tip 必变，于是旧条目再也命中不了。
命中前还要核对那个 commit 对象**还在不在本地对象库里** —— 万一对象库被重建，旧条目
会被丢掉并按「读不到」如实回报，而不是继续吐旧内容。

进程内缓存的规模上限是**防呆**：单次分析最多读几百个 blob。缓存只是加速，
不是证据来源 —— 每次读取都要能重放。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple

from services.ai.scope import normalize_path
from utils.logger import log_print

#: 索引里**不会出现**的路径（Git 从不跟踪 `.git/` 下的东西，这里只是显式写出来）。
_NEVER_TRACKED_DIRS = (".git",)

#: 不宜送模型的路径。判据是**文件名 / 目录名**（大小写不敏感），不是扩展名黑名单 ——
#: 「凭证」这件事在命名上有很强的惯例，而漏掉一个的代价是把密钥送进提示词。
#:
#: 这一条是**拒绝读取**，不是「读不到」：理由会明确告诉模型「这类文件不许读」，
#: 免得它把拒绝读成「这个文件不存在」。
_EXCLUDED_NAMES = (
    ".env",
    ".netrc",
    ".git-credentials",
    "_netrc",
    "credentials",
    "secrets",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
)
_EXCLUDED_EXTS = (
    ".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".ppk", ".kdbx", ".sqlite", ".db",
)
_EXCLUDED_DIRS = (".ssh", ".aws", ".gnupg", ".docker", ".kube")

#: 单次读取的字节上限（**初值**，与 `reference_index.MAX_INDEX_FILE_BYTES` 同一量级）。
#: 超了就说「太大，不读」——把一份 50MB 的生成物读进内存再解码，是纯损失。
MAX_READ_BYTES = 1_500_000

#: 进程内 blob 缓存的条目上限（防呆，见模块抬头）。
BLOB_CACHE_MAX_ENTRIES = 600
#: 跟踪文件清单的缓存条数（每个 (仓库, tip) 一条，正常只会有几条）。
TREE_CACHE_MAX_ENTRIES = 8


# ---------------------------------------------------------------------------
#  路径判据（纯函数）
# ---------------------------------------------------------------------------


def resolve_repo_path(raw: Any) -> Tuple[str, str]:
    """模型给的路径 → `(规范化路径, 拒绝理由)`。**理由非空时路径是空串**。

    先归一化（反斜杠、`./`、首尾引号空白，与 `scope.normalize_path` 同一套），再判：

    * **绝对路径**：以 `/` 或 `\\` 开头、带盘符（`C:`）、UNC（`//server/share`）；
    * **`..`**：任何一段是 `..` 都拒（`a/../b` 也拒 —— 归一化之后它看着无害，但它说明
      调用方在试图走出去，而合法的读取不需要这个写法）；
    * **控制字符 / NUL**：路径会被原样拼进给模型看的回执里；
    * 归一化之后为空、或以 `~` 开头。

    返回的路径**一律是正斜杠、相对、无 `./` 前缀**的形态（git 树里的写法）。
    """
    text = normalize_path(raw)
    if not text:
        return "", "路径是空的"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return "", "路径里含控制字符（换行、NUL 等），不是一条合法的仓库路径"
    if text.startswith("/"):
        return "", f"`{text}` 是绝对路径。只接受仓库内的相对路径（例如 `scripts/net/proto.lua`）"
    if text.startswith("~"):
        return "", f"`{text}` 是家目录写法，不是仓库内的相对路径"
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return "", f"`{text}` 带盘符，是绝对路径。只接受仓库内的相对路径"
    segments = [part for part in text.split("/") if part not in ("", ".")]
    if not segments:
        return "", f"`{text}` 归一化之后是空的"
    if any(part == ".." for part in segments):
        return "", (
            f"`{text}` 里有 `..`。仓库内的读取不需要它，"
            "而它正是「读到仓库外」的写法，所以一律拒绝"
        )
    if any(part in _NEVER_TRACKED_DIRS for part in segments[:-1]):
        return "", f"`{text}` 落在 `{_NEVER_TRACKED_DIRS[0]}/` 目录下，Git 不跟踪那里的内容"
    return "/".join(segments), ""


def exclusion_reason(path: str) -> str:
    """这条路是不是「不宜送模型的路径」（凭证 / 密钥库）。返回理由，空串 = 可以读。"""
    text = str(path or "")
    segments = [part for part in text.split("/") if part]
    if not segments:
        return ""
    lowered = [part.lower() for part in segments]
    for part in lowered[:-1]:
        if part in _EXCLUDED_DIRS:
            return f"`{text}` 落在 `{part}/` 目录下（凭证/密钥类目录），不送进模型上下文"
    name = lowered[-1]
    stem, ext = os.path.splitext(name)
    if name in _EXCLUDED_NAMES or stem in _EXCLUDED_NAMES:
        return f"`{text}` 是凭证/密钥类文件（按文件名判据），不送进模型上下文"
    if ext in _EXCLUDED_EXTS:
        return f"`{text}` 是凭证/密钥库/数据库文件（`{ext}`），不送进模型上下文"
    return ""


def reject_text(path: str, reason: str) -> str:
    """把一次拒绝渲染成**给模型看的一句话**。

    三件事缺一不可：你给的是什么（`path`）、为什么不行（`reason`）、还能怎么做。
    只回一句「已拒绝」会得到两个后果 —— 模型把它读成「平台取数失败」（写进信息缺口），
    或者原样再要一次。
    """
    return (
        f"[路径被拒] `{path}`：{reason}。\n"
        "**这一条没有被读取**，也不代表那个文件不存在 —— 请换个仓库内的相对路径再要一次；"
        "如果它本来就是仓库外的文件，请把这件事写成信息缺口。"
    )


# ---------------------------------------------------------------------------
#  冻结对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenRepository:
    """本次分析读的那个**版本**：一个仓库 + 一个 tip。"""

    repository_id: Any = None
    name: str = ""
    branch: str = ""
    #: 冻结的提交（sha）。所有读取都以它为根 —— 它是**不可变对象**，与工作区无关。
    tip: str = ""
    #: 本地工作副本的路径（只在**读对象库**时用，绝不读这里的文件）。
    local_path: str = ""
    #: 这个 tip 是怎么定下来的（给人看，进日志与回执）。
    source: str = ""

    @property
    def available(self) -> bool:
        return bool(self.tip) and bool(self.local_path)

    def describe(self) -> str:
        if not self.available:
            return f"冻结仓库不可用（{self.source or '未解析'}）"
        return (
            f"{self.name or self.repository_id}@{self.branch or '-'} "
            f"tip {self.tip[:8]}（{self.source}）"
        )


def _local_worktree_exists(local_path: Any) -> bool:
    try:
        return bool(local_path) and Path(str(local_path)).is_dir()
    except OSError:
        return False


def _git_service_for(repository: Any):
    """这个仓库的 git 服务实例；拿不到返回 None（不抛）。

    与 `services/ai/snapshot_consistency._git_service_for` 同一套取法：优先用
    `weekly_version_logic` 注入的工厂（同一仓库只有一个实例），退回
    `vcs_content_service` 的进程内缓存工厂。
    """
    try:
        from services.weekly_version_logic import _get_git_service

        if _get_git_service is not None:
            return _get_git_service(repository)
    except Exception:  # noqa: BLE001 —— 退回下一个工厂
        pass
    try:
        from services.vcs_content_service import get_git_service

        return get_git_service(repository)
    except Exception as exc:  # noqa: BLE001
        log_print(f"⚠️ AI 取数：拿不到仓库 {getattr(repository, 'id', None)} 的 git 服务：{exc}")
        return None


def resolve_frozen_repository(
    repository: Any,
    *,
    scope_commits: Sequence[str] = (),
    git_service: Any = None,
) -> Tuple[Optional[FrozenRepository], str]:
    """解析出本次分析要读的那个冻结版本。返回 `(对象, 原因)`，对象为 `None` 时原因非空。

    ## tip 从哪来（顺序固定，且要能说清是哪一档）

    1. `repository.last_synced_tip` —— 工作包 A 之后它就是「本轮缓存的提交都可达」的那个
       tip（`snapshot_consistency` 用它判缓存是否过期）。**首选它**：读的就是这一轮分析
       所依据的那份历史的末端。
    2. 退而求其次：本批次提交里**最后一个在本地对象库里存在**的提交（`scope_commits`
       的顺序即窗口顺序）。它仍然是不可变对象，但它可能不是分支末端 —— 所以 `source`
       要写明这一档，让人能看出「这次读的不是 tip」。

    两档都不成立时返回 `None` + 原因（**不是**一份空的范围）。

    ## 什么情况算「不可用」

    仓库不是 git、平台没有本地工作副本（`clone_status != completed`，platform/agent
    模式下必然如此）、本地目录不在、tip 解析不出来。这几种都**不报错**，只如实回报 ——
    调用方据此退回「只搜本批次」并声明覆盖范围，而不是假装搜过全仓。
    """
    if repository is None:
        return None, "本批次里没有解析到仓库"
    kind = str(getattr(repository, "type", "") or "").strip().lower()
    if kind and kind != "git":
        return None, f"仓库类型是 {kind}（当前只支持 git 的冻结读取）"
    status = str(getattr(repository, "clone_status", "") or "").strip().lower()
    if status != "completed":
        return None, "平台没有这个仓库的本地工作副本（clone_status != completed）"
    service = git_service if git_service is not None else _git_service_for(repository)
    if service is None:
        return None, "拿不到该仓库的 git 服务"
    local_path = getattr(service, "local_path", "")
    if not _local_worktree_exists(local_path):
        return None, f"本地工作副本不在（{local_path}）"

    declared = str(getattr(repository, "last_synced_tip", "") or "").strip()
    if declared:
        reader = FrozenTreeReader(
            FrozenRepository(
                repository_id=getattr(repository, "id", None),
                name=str(getattr(repository, "name", "") or ""),
                branch=str(getattr(repository, "branch", "") or ""),
                tip=declared,
                local_path=str(local_path),
                source="repository.last_synced_tip（本轮同步时的分支 tip）",
            )
        )
        if reader.has_object(declared):
            return reader.frozen, ""
        log_print(
            f"⚠️ AI 取数：仓库 {getattr(repository, 'id', None)} 记录的 tip {declared[:8]} "
            "不在本地对象库里，退回本批次提交"
        )
    for commit in scope_commits or ():
        text = str(commit or "").strip()
        if not text:
            continue
        reader = FrozenTreeReader(
            FrozenRepository(
                repository_id=getattr(repository, "id", None),
                name=str(getattr(repository, "name", "") or ""),
                branch=str(getattr(repository, "branch", "") or ""),
                tip=text,
                local_path=str(local_path),
                source="本批次的提交（不是分支 tip —— 记录里没有可用的 last_synced_tip）",
            )
        )
        if reader.has_object(text):
            return reader.frozen, ""
    return None, (
        "本次没有可用的冻结版本：仓库没有记录 last_synced_tip，"
        "本批次的提交也都不在本地对象库里"
    )


# ---------------------------------------------------------------------------
#  读取器（读的是**对象库**，不是工作区）
# ---------------------------------------------------------------------------


def _bounded_put(cache: "Dict[Any, Any]", key: Any, value: Any, limit: int) -> None:
    """往有界缓存里放一条（超上限丢最早的）。**只做规模防呆**，不参与正确性。"""
    cache[key] = value
    while len(cache) > max(1, int(limit)):
        cache.pop(next(iter(cache)), None)


class FrozenTreeReader:
    """对冻结 tip 的只读访问：跟踪清单 + 单个 blob 的读取。

    ## 加载 `git.Repo` 只做一次

    打开一个仓库对象要读 objects 目录的元信息，而一次 `find_references` 会读几百个文件 ——
    逐文件 `git.Repo(...)`（`vcs_content_service.get_file_content_from_git` 的写法）在这里
    是纯开销。所以本类在实例上缓存 `repo` 对象，同一个 tip 上的多次读取共用它。

    ## 每一个失败都返回 `None`，Never 抛

    取数层有一条纪律：读一个文件失败只该少一条证据，不该作废整轮分析。本类的每个公开
    方法都遵守它（`git.Repo` 打开失败、`ls-tree` 失败、blob 解不出来……全部折成 `None`）。
    """

    def __init__(self, frozen: FrozenRepository, *, repo: Any = None):
        self.frozen = frozen
        self._repo = repo
        self._repo_failed = False

    # -- 内部 ---------------------------------------------------------------

    def _repository(self):
        """惰性打开 gitpython 的仓库对象；失败返回 None（并记住，不反复重试）。"""
        if self._repo is not None:
            return self._repo
        if self._repo_failed or not self.frozen.local_path:
            return None
        try:
            import git

            self._repo = git.Repo(str(self.frozen.local_path))
            return self._repo
        except Exception as exc:  # noqa: BLE001
            self._repo_failed = True
            log_print(
                f"⚠️ AI 取数：打开仓库 {self.frozen.repository_id} 失败：{type(exc).__name__}: {exc}"
            )
            return None

    def _commit(self):
        """冻结的那个提交对象；拿不到返回 None。"""
        repo = self._repository()
        if repo is None or not self.frozen.tip:
            return None
        try:
            return repo.commit(self.frozen.tip)
        except Exception:  # noqa: BLE001 —— 对象不在/坏了都算「读不到」
            return None

    def has_object(self, commit: Any) -> bool:
        """这个提交还在本地对象库里吗（解析不出来就算不在）。"""
        text = str(commit or "").strip()
        repo = self._repository()
        if not text or repo is None:
            return False
        try:
            repo.commit(text)
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- 跟踪清单 -----------------------------------------------------------

    def tracked_paths(self) -> Optional[Tuple[str, ...]]:
        """冻结 tip 上**全部 Git 跟踪文件**的路径（按仓库树顺序，确定性）。

        返回 `None` 表示**判不出来**（对象库读不到、tip 不在）—— 调用方必须把它与
        「空列表」分开：空列表是「这个仓库一个文件都没跟踪」，那是另一件（几乎不可能
        发生的）事。
        """
        key = (self.frozen.repository_id, self.frozen.tip, str(self.frozen.local_path))
        cached = _TREE_CACHE.get(key)
        if cached is not None:
            return cached
        commit = self._commit()
        if commit is None:
            return None
        try:
            paths = tuple(
                str(entry.path)
                for entry in commit.tree.traverse()
                if str(getattr(entry, "type", "")) == "blob"
            )
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：列跟踪文件失败 {self.frozen.tip[:8]}：{exc}")
            return None
        _bounded_put(_TREE_CACHE, key, paths, TREE_CACHE_MAX_ENTRIES)
        return paths

    def is_tracked(self, path: str) -> Optional[bool]:
        """这个路径在冻结 tip 上是不是一个跟踪文件。`None` = 判不出来。"""
        paths = self.tracked_paths()
        if paths is None:
            return None
        return str(path or "") in frozenset(paths)

    # -- 读取 ---------------------------------------------------------------

    def read_bytes(self, path: str) -> Optional[bytes]:
        """读冻结 tip 上这个路径的**原始字节**。不在跟踪树里 / 读不到都返回 `None`。

        ## 为什么是 `commit.tree[path]` 而不是读工作区文件

        工作副本此刻的内容与本轮快照未必是同一个版本（同步每 2~3 分钟一跑，checkout
        也跟着走）。读工作区等于把「没有版本的证据」交给模型，而它会当成「这个版本的
        代码就是这样」。`commit.tree[path]` 读的是不可变对象 —— tip 定了，内容就定了。

        ## 改名兜底不做

        `get_file_content_from_git` 在路径不存在时会去猜「改名前的名字」（`--follow`）。
        这里**刻意不做**：冻结范围这个工具的前提是「模型手里有一个它在别处看到过的路径」，
        而猜出来的那条路径在跟踪树里**不存在**，把它交出去等于凭空造一个文件位置。
        调用方拿到 `None` 会说「这个路径不在本轮版本里」，这比一个猜出来的名字准确。
        """
        text = str(path or "").strip()
        if not text:
            return None
        key = (self.frozen.repository_id, self.frozen.tip, text)
        cached = _BLOB_CACHE.get(key)
        if cached is not None:
            return cached
        commit = self._commit()
        if commit is None:
            return None
        try:
            blob = commit.tree[text]
        except KeyError:
            return None
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：读 {text}@{self.frozen.tip[:8]} 失败：{exc}")
            return None
        try:
            data = blob.data_stream.read()
        except Exception as exc:  # noqa: BLE001
            log_print(f"⚠️ AI 取数：读 blob 失败 {text}：{exc}")
            return None
        _bounded_put(_BLOB_CACHE, key, data, BLOB_CACHE_MAX_ENTRIES)
        return data

    def read_within_limit(self, path: str) -> Tuple[Optional[bytes], str]:
        """读一份正文，并**先说清限制**：`(字节, 理由)`。

        * `(data, "")` —— 读到了；`data` 为 `None` 时理由非空。
        * 超过 `MAX_READ_BYTES` → `(None, "太大")`：**不是**「读到了但截断了」——
          读一半再解码只会得到一份看起来完整的半截文件。
        """
        data = self.read_bytes(path)
        if data is None:
            return None, "这个路径在本次冻结版本里读不到（不在跟踪树里，或对象库缺这个 blob）"
        if len(data) > MAX_READ_BYTES:
            return None, (
                f"这个文件有 {len(data):,} 字节，超过单文件读取上限 "
                f"{MAX_READ_BYTES:,} 字节，本次不读（**这不等于它没有内容**）"
            )
        return data, ""


# ---------------------------------------------------------------------------
#  进程内缓存（跨运行复用；键里含仓库 ID 与 tip，见模块抬头）
# ---------------------------------------------------------------------------

_TREE_CACHE: Dict[Any, Tuple[str, ...]] = {}
_BLOB_CACHE: Dict[Any, bytes] = {}


def reset_caches() -> None:
    """清空进程内缓存（**给测试与「导入/强推之后强制重读」用**）。

    生产路径不需要主动调用：键里含 tip，强推之后 tip 变了就再也命中不了旧条目。
    这个函数存在是为了让测试能在同一个进程里干净地重来一遍。
    """
    _TREE_CACHE.clear()
    _BLOB_CACHE.clear()


def cache_sizes() -> Dict[str, int]:
    """当前缓存规模（测试与排查用）。"""
    return {"trees": len(_TREE_CACHE), "blobs": len(_BLOB_CACHE)}


@dataclass(frozen=True)
class RepoReadScope:
    """交给协议层与模板层的一份**范围说明**：哪些路径可读、读的是哪个版本。

    它只携带事实（路径集合 + 一行出处），不携带 reader —— 协议层要的是「这个路径合法吗」，
    读取仍然由 `platform_provider` 走 `FrozenTreeReader`（那是唯一碰 git 的地方）。
    """

    frozen: FrozenRepository = field(default_factory=FrozenRepository)
    paths: frozenset = frozenset()
    #: 判不出来的原因（`paths` 为空且它非空时，调用方必须报「无法检索」而不是「没有」）。
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.paths) and not self.reason

    def contains(self, path: str) -> bool:
        return str(path or "") in self.paths

    def matches_prefix(self, prefix: str) -> bool:
        text = str(prefix or "")
        if not text:
            return False
        return any(item == text or item.startswith(text) for item in self.paths)


def build_read_scope(
    frozen: Optional[FrozenRepository], *, reader: Optional[FrozenTreeReader] = None
) -> RepoReadScope:
    """把冻结对象变成一份范围说明（列一次跟踪树）。**拒绝的理由会写进 `reason`。**"""
    if frozen is None:
        return RepoReadScope(reason="本次没有可用的冻结仓库")
    reader = reader or FrozenTreeReader(frozen)
    paths = reader.tracked_paths()
    if paths is None:
        return RepoReadScope(
            frozen=frozen,
            reason=f"列不出冻结版本 {frozen.tip[:8]} 的跟踪文件（对象库读不到这个提交）",
        )
    return RepoReadScope(frozen=frozen, paths=frozenset(paths))


__all__ = [
    "BLOB_CACHE_MAX_ENTRIES",
    "MAX_READ_BYTES",
    "FrozenRepository",
    "FrozenTreeReader",
    "RepoReadScope",
    "build_read_scope",
    "cache_sizes",
    "exclusion_reason",
    "provider_repo_paths",
    "reject_text",
    "reset_caches",
    "resolve_frozen_repository",
    "resolve_repo_path",
]


def provider_repo_paths(provider: Any):
    """取 provider 报出来的**冻结版本跟踪文件集合**；拿不到返回 `None`。

    ## 为什么用鸭子类型而不是往 `ContextProvider` 协议上加方法

    `ContextProvider` 是有意做窄的（五个取数方法），而它的实现有一大堆：真实 provider、
    假 provider、探针、测试里的桩。往协议上加一项，等于要求每一个都跟上 —— 而它们
    大多数根本不懂「仓库冻结范围」这回事。

    **拿不到就给 `None`**（不是空集合）：空集合会让 `sanitize_requests` 把每一条
    仓库范围的请求都判成越权，而理由会说「不在跟踪树里」—— 一句假话。
    """
    getter = getattr(provider, "repo_tracked_paths", None)
    if not callable(getter):
        return None
    try:
        paths = getter()
    except Exception as exc:  # noqa: BLE001 —— 拿不到范围只该让判据退回本批次
        log_print(f"⚠️ AI 分析：取冻结版本范围失败：{type(exc).__name__}: {exc}")
        return None
    if not paths:
        return None
    return frozenset(paths)
