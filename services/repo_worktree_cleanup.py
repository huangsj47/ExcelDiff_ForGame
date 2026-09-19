#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""删掉某个仓库的本地工作副本 —— 以及「这个目录到底能不能删」的判据。

## 为什么单独一个模块

这段逻辑原先在 `services/task_worker_service.py` 里，而那个文件已经贴着 2000 行的
硬上限（`scripts/check_file_length.py --strict`）。更要紧的是**它值得被单独看见**：
这是整个代码库里少数几个会**递归删除磁盘目录**的地方，读它的人应该一眼看到
「判据」与「动手」是分开的两个函数。

## 一条用血的教训换来的纪律

**危险目标（空值 / `"."` / `".."` / 盘符根 / 平台源码根）只许喂给
`removal_target_is_dangerous` —— 那是个纯判断函数，不碰文件系统。
绝对不许把它们喂给 `force_remove_repo_worktree`。**

`force_remove_repo_worktree` 内部是 `shutil.rmtree` + `rmdir /s /q` 兜底。一旦守卫
那行被改坏（哪怕只是本地调试或变异验证时临时注掉），把一个危险目标交给它就是
**整台机器被递归删除**。这不是假设：这份代码的第一版把
`os.path.abspath(os.sep)`（即 `C:\\`）连同 `""`、`".."` 一起喂给了删除函数，
随后做变异验证时注掉了守卫 —— `rmdir /s /q C:\\` 真的跑了，代价是那台机器的
`C:\\Python\\Python3.13\\Lib`（标准库）与整个工作目录被删掉。

所以：

* 判据必须是**纯函数**（有测试钉着：它的 AST 里不许出现 rmtree/remove/rmdir/
  subprocess）；
* 验证「危险目标会被拒」时只调判据，**永远不调删除函数**；
* 真要测删除函数，就 `monkeypatch.chdir(临时目录)` 再喂空值 —— 即使守卫完全失效，
  被删的也只是一个一次性的临时目录。
"""
from __future__ import annotations

import os
import shutil
import subprocess

from utils.logger import log_print
from utils.runtime_paths import (
    default_repos_base_dir,
    repo_root,
    resolve_runtime_path,
)


def removal_target_is_dangerous(target: str) -> bool:
    """这个目录**不能**被 rmtree 掉吗？

    `force_remove_repo_worktree` 删的是一整棵目录树，所以这里回答的不是「参数看着像
    不像空」，而是「它有没有被证明是**一个具体的、可以被删的目录**」。三条拒绝理由，
    每条都对应一种拿到错误路径的真实方式：

    * **文件系统根**（`C:\\`、`/`）—— `abspath` 的退化产物；
    * **平台源码根或它的任何祖先** —— `"."` / `".."` / 只写了盘符，`abspath` 一算就落到
      平台自己身上。这是最要命的一种：`shutil.rmtree` 自顶向下走，先把内容删光、
      最后 `os.rmdir` 才因为「这是进程的当前目录」在 Windows 上失败，于是**函数返回
      「删不掉」，而 app.py / services / templates / instance 已经没了** ——
      「删了个寂寞」与「删了整个平台」是同一种结局，从返回值上分不出来；
    * **就是 repos 根目录本身** —— 那一下会把**所有仓库**的工作副本一起删掉。

    【为什么**不**要求「必须在 repos 根之下」】
    那样更严，但会误伤一个真实存在的调用形态：`services/git_service.py::_get_local_path`
    在 service 不是按仓库构造时返回 `self.root_directory or 'temp_repo'`，那不保证落在
    repos 根里。而这条函数拒绝之后是「已经没了」，调用方会接着去重新 clone ——
    留着一个没清干净的目录，重 clone 照样失败，而且失败原因看起来与「清理」无关。
    **空值那一路的危险已由入参检查挡住**（见 `force_remove_repo_worktree` 的第一段），
    不靠这一条兜。
    """
    platform_root = repo_root()
    normalized = os.path.normcase(target)
    if normalized == os.path.normcase(os.path.abspath(os.sep)):
        return True
    # ancestor-or-equal：`os.path.commonpath` 比字符串前缀可靠（`C:\a` 与 `C:\ab`）
    try:
        if os.path.commonpath([normalized, os.path.normcase(platform_root)]) == normalized:
            return True
    except ValueError:          # 不同盘符，没有公共路径 —— 不构成「祖先」关系
        pass
    repos_root = os.path.normcase(
        os.path.abspath(
            resolve_runtime_path(None, default_relative=default_repos_base_dir())
        )
    )
    return normalized == repos_root


def force_remove_repo_worktree(local_path: str) -> bool:
    """强制删除某个仓库的工作副本目录。

    **前置判断必须能真的挡住东西。** 原来写的是：

        target = os.path.abspath(str(local_path or "").strip())
        if not target:
            return True

    `os.path.abspath("")` 返回的是**当前工作目录**，永远不是空串 —— 那句守卫从写下来
    那天起就没有执行过。它的意图（「别把平台自己删了」）是对的，只是判据落在一个
    不可能成立的条件上；判据改由 `removal_target_is_dangerous` 承担。

    返回 `True` 表示「目标现在不存在了」（包括「本来就不存在」与「被拒绝了」）——
    调用方据此继续后面的步骤；返回 `False` 表示确实删不掉。
    """
    raw = str(local_path or "").strip()
    if not raw:
        log_print("⚠️ 拒绝删除工作副本：没有拿到本地路径（空值）", "SYNC", force=True)
        return True
    target = os.path.abspath(raw)
    if removal_target_is_dangerous(target):
        log_print(
            f"⚠️ 拒绝删除工作副本：{target} 没有被证明是一个仓库工作副本目录"
            f"（是文件系统根 / 平台源码根或其祖先 / repos 根目录本身）",
            "SYNC",
            force=True,
        )
        return True
    if not os.path.exists(target):
        return True

    try:
        shutil.rmtree(target, ignore_errors=False)
    except (OSError, PermissionError) as exc:
        log_print(f"⚠️ 删除仓库目录失败，尝试命令行兜底: {target} | {exc}", "SYNC", force=True)

    if os.path.exists(target):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["cmd", "/c", "rmdir", "/s", "/q", target],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                shutil.rmtree(target, ignore_errors=True)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            log_print(f"⚠️ 目录删除兜底失败: {target} | {exc}", "SYNC", force=True)

    if os.path.exists(target):
        log_print(f"❌ 无法删除仓库目录: {target}", "SYNC", force=True)
        return False
    return True
