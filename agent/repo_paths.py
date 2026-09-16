#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Agent 侧仓库目录解析：与平台 `utils/runtime_paths.py` 同规则、同语义。

## 为什么需要这个模块

`AGENT_REPOS_BASE_DIR` 默认是相对路径，而 Agent 侧此前用
`os.path.abspath(settings.repos_base_dir)`（**相对 CWD**）解析它，平台侧
`build_repository_local_path()` 的默认 base_dir 是 `repos`（相对**仓库根**）。
于是同一个 repository 的落点有两个：

    CWD = <安装根>/agent（agent/start_agent.bat 的 `cd /d "%~dp0"`）
      auto_sync 写入: <安装根>/agent/agent_repos/G119_qz_pub_7
      Diff   读取  : <安装根>/repos/G119_qz_pub_7

后果按危害排序：

1. **同步成功，Diff 找不到工作副本**：Agent 节点上的 Diff 由平台代码在 Agent
   进程内执行（`agent/executor.py`），读的是平台算出来的目录。两侧不一致时
   同步任务报 completed，紧接着的 diff 却判定「未克隆」，把仓库重新 clone 一遍。
2. **落点随启动方式漂移**：`agent/start_agent.bat` 会 `cd /d "%~dp0"`，但服务
   管理器、计划任务、手工 `python <绝对路径>/start_agent.py` 不一定 —— 换个
   CWD 就换了一个 `agent_repos/`，旧目录成为无人引用的孤儿。
3. **诊断信息说谎**：日志里报的本地目录与实际被写入的目录可能是两个路径。

## 判定规则（与 `utils/runtime_paths.py` 完全一致）

* 绝对路径：原样返回（显式指定就该听显式指定的）。
* 相对路径：锚定 `runtime_anchor()`，与调用时的 CWD 无关。
* 空值：用 `AGENT_REPOS_BASE_DIR`，仍未设置则 `repos` —— 与平台默认值**同名**，
  于是「默认配置」下两侧必然落进同一个目录。

`runtime_anchor()`：

* 平台源码与 agent 同级（`<安装根>/agent/` 与 `<安装根>/utils/`）→ `<安装根>`，
  与 `utils.runtime_paths.repo_root()` 是同一个目录。这是必须对齐的一侧：Diff
  读的目录由平台代码算，两边必须逐字符相同。
* agent 目录被单独打包部署（`agent/build_zip.py` 只打包 agent/，解压后没有
  `app.py`）→ agent 安装根 `AGENT_DIR`。此时没有平台运行时，不存在必须对齐的
  另一侧，但仍然不能依赖 CWD。

本模块**不 import 平台 utils**：独立部署时它根本不存在（同
`agent/handlers/auto_sync.py` 顶部对 `build_repository_local_path` 的 try/except）。
锚点用 `__file__` 推导，再用平台模块不可用时的等价实现。两条实现是否仍然一致，
由 `tests/test_agent_repo_path_unification.py` 直接对比平台函数来守住。
"""

from __future__ import annotations

import os

# Agent 安装根 = 本文件所在目录（`agent/`）。用 `__file__` 而不是 `os.getcwd()`：
# 启动脚本会 `cd` 到 agent 目录，但服务管理器/计划任务/手工调用不会。
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))

# 与 utils/runtime_paths.py 里的同名常量必须一致：两侧读同一个环境变量，
# 才能保证「sync 写入」与「diff 读取」是同一个目录。
REPOS_BASE_DIR_ENV = "AGENT_REPOS_BASE_DIR"
DEFAULT_REPOS_BASE_DIR = "repos"

# 平台源码根的判定依据：与 `agent/executor.py::_ensure_platform_runtime_import_path()`
# 用的是同一件事（上一级目录里有没有 app.py）—— 那个函数决定 Diff 是否由平台代码
# 在本进程内执行，锚点必须跟着它走，不能出现「平台在跑、锚点却不是平台根」。
_PLATFORM_MARKERS = ("app.py", os.path.join("utils", "runtime_paths.py"))


def platform_root():
    """平台源码根（绝对路径）；agent 单独部署时返回 None。"""
    parent = os.path.dirname(AGENT_DIR)
    for marker in _PLATFORM_MARKERS:
        if os.path.exists(os.path.join(parent, marker)):
            return parent
    return None


def runtime_anchor() -> str:
    """相对路径的锚点目录（绝对路径，与 CWD 无关）。"""
    return platform_root() or AGENT_DIR


def default_repos_base_dir(env=None) -> str:
    """`AGENT_REPOS_BASE_DIR` 优先，否则 `repos`（与平台默认值同名）。"""
    source = os.environ if env is None else env
    try:
        raw = str(source.get(REPOS_BASE_DIR_ENV) or "").strip()
    except Exception:
        raw = ""
    return raw or DEFAULT_REPOS_BASE_DIR


def resolve_repos_base_dir(configured=None, *, env=None) -> str:
    """把配置里的仓库根目录解析成绝对路径。

    * `configured` 为空 → `AGENT_REPOS_BASE_DIR`，再为空 → `repos`。
    * 绝对路径 → normpath 后原样返回（幂等：已解析过的值再解析一次不变）。
    * 相对路径 → 锚定 `runtime_anchor()`。

    不做 realpath：软链接的解析与越界校验另有 `utils/path_security.py` 负责，
    这里若解析软链接就可能把路径指到别处（与平台 `resolve_runtime_path` 同一取舍）。
    """
    raw = str(configured or "").strip() or default_repos_base_dir(env)
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(runtime_anchor(), raw))
