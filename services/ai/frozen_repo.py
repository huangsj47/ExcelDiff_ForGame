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
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

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

    ## 为什么是**一组**仓库而不是一个（2026-09-24，run 57）

    一次周版本分析覆盖的仓库可以不止一个（配置仓库 + 代码仓库同窗是常态）。此前这里只装
    得下一个 `FrozenRepository`，而挑哪一个由 `_batch_repository()` 按仓库 id 升序取第一
    —— 于是**代码仓库的文件全都落在「不在本次冻结版本的 Git 跟踪文件里」这一句里**。
    实测 run 57：7 条被拒请求中有 4 条是 `code/qz_*`，而那两个文件在代码仓库自己的 tip 上
    确实被跟踪。

    所以这里改成装一组，`paths` 是**并集**：判据仍然是「本项目已接入仓库的只读 Git 跟踪
    内容」，没有放宽到任意文件；`resolve_repo_path`（绝对路径 / 盘符 / `..` / 控制字符）与
    `exclusion_reason`（凭证类）两条判据在多仓之前照旧各判一次。
    """

    frozen: FrozenRepository = field(default_factory=FrozenRepository)
    paths: frozenset = frozenset()
    #: 判不出来的原因（`paths` 为空且它非空时，调用方必须报「无法检索」而不是「没有」）。
    #: **只在整份不可用时非空** —— 部分仓库失败走 `note`，否则 `available` 会跟着变假，
    #: 协议层就会退回「只认本批次」，把能读的那些仓库一起关掉。
    reason: str = ""
    #: 部分仓库没冻结成功时的说明（`paths` 仍然可用）。给人看，不参与判据。
    note: str = ""
    #: **本次冻结的全部仓库**（`frozen` 是其中第一个，留给只认单仓的旧读法）。
    frozens: tuple = ()
    #: 仓库 id -> 这一个仓库跟踪的路径集合。
    paths_by_repository: Mapping[Any, frozenset] = field(default_factory=dict)
    #: 路径 -> 跟踪它的仓库 id 元组（**顺序确定**，按 id 升序）。同一条相对路径在两个仓库里
    #: 都存在时，它就是「该读哪一个」的歧义判据 —— 调用方必须据此报可操作错误，不许猜。
    repositories_by_path: Mapping[str, tuple] = field(default_factory=dict)

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

    def candidates(self, path: str) -> tuple:
        """跟踪这条路径的仓库 id（0 个 = 谁都不跟踪；≥2 个 = 有歧义，别猜）。"""
        return tuple(self.repositories_by_path.get(str(path or ""), ()))

    @property
    def identity(self) -> str:
        """「这一份冻结版本」的稳定标识：**每个仓库的 (id, tip) 都要在里面**。

        只取第一个仓库的 tip 是不够的：代码仓库换了 tip 而配置仓库没换时，快照其实变了，
        而标识没变 —— 判「快照是否变化」的地方会据此说「没变」（`request_fingerprint`）。
        按仓库 id 升序拼，内容确定、可比较。空 scope 给空串（= 不知道）。
        """
        parts = []
        items = self.frozens or ((self.frozen,) if self.frozen.available else ())
        for item in items:
            tip = str(getattr(item, "tip", "") or "")
            if not tip:
                continue
            parts.append(f"{getattr(item, 'repository_id', None)}:{tip[:40]}")
        return "|".join(sorted(parts))


def repository_label(repository_id: Any, name: Any = "") -> str:
    """一个仓库给人看的短名：`编号（名字）`；拿不到名字就只给编号。

    **名字一律从调用方手上那份冻结对象/仓库行里取**（解析冻结版本时就从仓库行上取下来了）：
    为了凑一个名字再去查一次库，既多一次查询，也会在没有 app 上下文的调用点（测试、探针）
    刷出警告，而那种警告会淹掉真的失败。
    """
    text = str(name or "")
    return f"{repository_id}（{text}）" if text else str(repository_id)


def _label_of(frozen: Any) -> str:
    """`FrozenRepository` → 短名。"""
    return repository_label(
        getattr(frozen, "repository_id", None), getattr(frozen, "name", "")
    )


def reader_for_repository(readers: Sequence[Any], repository_id: Any) -> Any:
    """在一组读取器里按仓库 id 找那一个。找不到返回 `None`。"""
    for reader in readers or ():
        if getattr(getattr(reader, "frozen", None), "repository_id", None) == repository_id:
            return reader
    return None


def ambiguous_path_text(scope: RepoReadScope, path: str) -> str:
    """同一条相对路径**被多个仓库跟踪**时给模型的那份可操作拒绝。

    两个仓库里都有 `config/item.xlsx` 是可能的，而两个版本的内容与含义都可能不同 ——
    **猜一个等于把另一个仓库的内容当成这个仓库的交给模型**。所以这里不读，只列出候选
    仓库并说明怎么指定（带 `repository_id`，或给一条改过它的提交）。
    """
    candidates = scope.candidates(path)
    labels = [
        _label_of(item)
        for item in (getattr(scope, "frozens", ()) or ())
        if getattr(item, "repository_id", None) in candidates
    ]
    named = "、".join(labels) or "、".join(str(item) for item in candidates)
    return (
        f"[需要在多个仓库之间指定] `{path}`：本项目的 {len(candidates)} 个"
        f"仓库里都有这条路径（{named}）。**平台不替你猜是哪一个** —— 在请求里带上"
        " `repository_id` 指明仓库；或者给一条**改过这个文件**的提交（那条路读的是"
        "该提交上的版本）。\n"
        "**这不等于「读不到」**。"
    )


def not_tracked_text(scope: RepoReadScope, path: str) -> str:
    """「不在任何冻结仓库的跟踪树里」那句。**说清查了几个仓库** —— 少写这个数，
    「不在跟踪树里」会被读成「这个文件不存在」。"""
    count = len(getattr(scope, "frozens", ()) or ())
    return (
        f"[路径不在本轮版本里] `{path}`：它不在本次冻结的 {count} 个仓库的"
        "Git 跟踪文件清单里（拼错、改过名，或它在**其它项目**的仓库里）。\n"
        "**这不等于「没有引用」** —— 请核对路径后另要一次，"
        "或者把这件事写成信息缺口。"
    )


def build_read_scope(
    frozen: Optional[FrozenRepository], *, reader: Optional[FrozenTreeReader] = None
) -> RepoReadScope:
    """把**一个**冻结对象变成一份范围说明。保留给只认单仓的调用方与既有测试。"""
    if frozen is None:
        return RepoReadScope(reason="本次没有可用的冻结仓库")
    reader = reader or FrozenTreeReader(frozen)
    paths = reader.tracked_paths()
    if paths is None:
        return RepoReadScope(
            frozen=frozen,
            reason=f"列不出冻结版本 {frozen.tip[:8]} 的跟踪文件（对象库读不到这个提交）",
        )
    return RepoReadScope(
        frozen=frozen,
        paths=frozenset(paths),
        frozens=(frozen,),
        paths_by_repository={getattr(frozen, "repository_id", None): frozenset(paths)},
        repositories_by_path={
            path: (getattr(frozen, "repository_id", None),) for path in paths
        },
    )


def build_read_scope_multi(
    frozens: Sequence[FrozenRepository],
    *,
    readers: Optional[Mapping[Any, FrozenTreeReader]] = None,
) -> RepoReadScope:
    """把**一组**冻结对象合成一份范围说明（`paths` = 并集）。

    ## 一个一个来，坏的那个不拖垮整体

    某个仓库列不出跟踪树（对象库缺那个提交、本地副本不在）时，只把它从这一份里去掉、
    把原因写进 `note`；**其余仓库照常可读**。整份一起失败会让「配置仓库能读、代码仓库
    临时读不了」变成「这次什么都不能读」—— 那是把一次局部故障放大成全面降级。

    全都没成时 `paths` 为空、`reason` 非空：调用方据此退回「只搜本批次」（既有语义）。
    """
    kept: list[FrozenRepository] = []
    by_repo: dict = {}
    by_path: dict = {}
    failures: list[str] = []
    for frozen in frozens or ():
        if frozen is None:
            continue
        reader = (readers or {}).get(getattr(frozen, "repository_id", None))
        reader = reader or FrozenTreeReader(frozen)
        paths = reader.tracked_paths()
        if paths is None:
            failures.append(
                f"{getattr(frozen, 'name', '') or frozen.repository_id}"
                f"（列不出 {frozen.tip[:8]} 的跟踪文件）"
            )
            continue
        kept.append(frozen)
        repository_id = getattr(frozen, "repository_id", None)
        cleaned = frozenset(str(path) for path in paths if str(path or "").strip())
        by_repo[repository_id] = cleaned
        for path in cleaned:
            by_path.setdefault(path, []).append(repository_id)
    if not kept:
        reason = "本次没有可用的冻结仓库"
        if failures:
            reason += "：" + "；".join(failures)
        return RepoReadScope(reason=reason)
    return RepoReadScope(
        frozen=kept[0],
        paths=frozenset(by_path),
        note=("；".join(failures) if failures else ""),
        frozens=tuple(kept),
        paths_by_repository=by_repo,
        repositories_by_path={
            path: tuple(sorted(ids, key=lambda item: (item is None, item)))
            for path, ids in by_path.items()
        },
    )


def resolve_frozen_repositories(
    repositories: Sequence[Any],
    *,
    scope_commits: Sequence[str] = (),
    git_service_for: Any = None,
) -> Tuple[Tuple[FrozenRepository, ...], str]:
    """把**一批**仓库解析成各自的冻结版本。返回 `(成功的那些, 说明)`。

    ## 一个仓库失败只记一笔，不让整批失败

    某个仓库不是 git、没有本地副本、tip 不在对象库里 —— 这些都不该让**别的仓库**也读不了。
    实测 run 57 的形状正是「配置仓库冻结成功、代码仓库没被冻结」，而当时整批只能挑一个。
    失败的仓库连原因一起写进 `note`（给日志与回执），成功的照常交出去。

    全部失败时返回 `((), note)`：调用方据此退回「只搜本批次」，那是既有语义。
    """
    frozens: list[FrozenRepository] = []
    failures: list[str] = []
    for repository in repositories or ():
        if repository is None:
            continue
        service = None
        if callable(git_service_for):
            try:
                service = git_service_for(repository)
            except Exception:  # noqa: BLE001 —— 拿不到就交给下一档自己解析
                service = None
        frozen, reason = resolve_frozen_repository(
            repository, scope_commits=scope_commits, git_service=service
        )
        if frozen is None:
            name = str(getattr(repository, "name", "") or getattr(repository, "id", ""))
            failures.append(f"{name}（{reason}）")
            continue
        frozens.append(frozen)
    note = "；".join(failures)
    return tuple(frozens), note



__all__ = [
    "BLOB_CACHE_MAX_ENTRIES",
    "MAX_READ_BYTES",
    "FrozenRepository",
    "FrozenTreeReader",
    "RepoReadScope",
    "ambiguous_path_text",
    "build_read_scope",
    "build_read_scope_multi",
    "cache_sizes",
    "exclusion_reason",
    "not_tracked_text",
    "provider_repo_paths",
    "reader_for_repository",
    "reject_text",
    "repository_label",
    "reset_caches",
    "resolve_frozen_repositories",
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
