#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""运行期路径锚点：让「相对路径」永远相对于**仓库根目录**，而不是当前工作目录。

## 为什么需要这个模块

平台有多处用相对路径表示运行期数据的位置：

* `instance/diff_platform.db`（SQLite 数据库文件）
* `repos/`（git / svn 工作副本目录）

这些路径此前都用 `os.path.abspath(...)` 解析 —— 而 `os.path.abspath` 是**相对
当前工作目录**的。于是同一个程序，从不同目录启动就会落到不同的数据上：

    在仓库根目录启动:  <仓库根>/instance/diff_platform.db
    在 C:\ 启动:       C:\instance\diff_platform.db
    在 %TEMP% 启动:    C:\Users\<用户>\AppData\Local\Temp\instance\diff_platform.db

实测（修复前）：

    cd %TEMP% ; python <仓库根>/app.py
    → 运行期数据库路径变成 ...\Temp\instance\diff_platform.db
    → 而 /api/system/info 报告的路径来自 config.py 的 DATABASE_CONFIG
      （也是 abspath），两者「碰巧」都指向 %TEMP% 下的新库

后果按危害排序：

1. **看起来像数据丢失**：换个方式启动（Windows 服务、systemd 的
   `WorkingDirectory`、`cd /` 后用绝对路径启动）就会新建一个空库，
   提交列表、确认状态、周版本配置全部「消失」，而旧库还在磁盘上。
2. **工作副本被整体重克隆**：`repos/` 同理。所有仓库在新位置都判定为
   「未克隆」，于是把每个仓库重新 clone 一遍（磁盘与带宽成本），
   旧的 `repos/` 变成无人引用的孤儿目录。
3. **诊断信息说谎**：`/api/system/info` 报的路径与实际在用的库可能是两个
   不同文件（若某个路径下恰好存在历史残留的同名库，页面还会显示它的
   表清单与文件大小）。

## 判定规则

* 绝对路径：原样返回（显式指定就该听显式指定的）。
* 相对路径：锚定到仓库根目录，与调用时的 CWD 无关。

仓库根目录 = 本文件所在目录的上一级（`utils/` 的父目录）。用
`__file__` 而不是 `os.getcwd()`，正是为了让结果与 CWD 无关。
"""

from __future__ import annotations

import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 节点仓库目录（工作副本根）的显式覆盖变量。Agent 与平台**读同一个变量**，见
# `default_repos_base_dir()` 与 `agent/repo_paths.py`。
REPOS_BASE_DIR_ENV = "AGENT_REPOS_BASE_DIR"
# 默认值——**不要改**：平台现有部署的工作副本都在 `<仓库根>/repos`，
# 改这个默认值会让平台把所有仓库重新 clone 一遍（见本模块顶部文档第 2 条）。
DEFAULT_REPOS_BASE_DIR = "repos"


def repo_root() -> str:
    """仓库根目录（绝对路径，与 CWD 无关）。"""
    return _REPO_ROOT


def default_repos_base_dir(env=None) -> str:
    """工作副本根目录的默认 base_dir。

    取值规则：`AGENT_REPOS_BASE_DIR`（去空白后非空）优先，否则 `repos`。

    为什么平台侧也要读 Agent 的环境变量：Agent 节点上的 Diff 是**平台代码在 Agent
    进程内**执行的（`agent/executor.py` 把平台源码根加进 sys.path 后 `import app`，
    再调 `services.task_worker_service.execute_task_inline_for_agent`）。同一个
    repository，「auto_sync 写入的工作副本」与「Diff 读取的工作副本」必须是同一个
    目录，否则同步明明成功、Diff 仍报未克隆，于是把每个仓库重新 clone 一遍。
    两侧都从这一个变量取值，一致性就是**构造出来的**，而不是靠两处默认值碰巧同名。

    变量没设时返回 `repos`，平台现有部署的行为逐字节不变。
    """
    source = os.environ if env is None else env
    try:
        raw = str(source.get(REPOS_BASE_DIR_ENV) or "").strip()
    except Exception:
        raw = ""
    return raw or DEFAULT_REPOS_BASE_DIR


def resolve_runtime_path(path: str, *, default_relative: str = "") -> str:
    r"""把运行期路径解析成绝对路径。

    * `path` 为空 → 用 `default_relative`（同样是仓库根相对）。
    * 绝对路径 → 原样返回。
    * 相对路径 → 以仓库根目录为基准。

    不做 realpath：那会解析软链接，可能把路径指到别处（软链接的解析另有
    `utils/path_security.py` 的包含性校验负责）。但会做 normpath —— 统一分隔符
    （避免 `...\ExcelDiff_ForGame\instance/custom_diff.db` 这种混用），
    并折叠 `..` 与重复分隔符。
    """
    candidate = str(path or "").strip() or str(default_relative or "").strip()
    if not candidate:
        return _REPO_ROOT
    if os.path.isabs(candidate):
        return os.path.normpath(candidate)
    return os.path.normpath(os.path.join(_REPO_ROOT, candidate))
