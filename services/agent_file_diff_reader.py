#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 端：在**本机的工作副本**上算「某一条提交改了这个文件的什么」，渲染成给模型读的文本。

与 `services/agent_file_content_reader.py` 是一对（那边给正文、这边给 diff），
与平台端的 `services/agent_file_content_dispatch.request_file_diff` 也是一对。

## 为什么这件事只能由 Agent 做

实时算一条提交的 diff 要读**两个版本的文件内容**，而 platform/agent 模式下平台
**被禁止 clone**（`get_file_content_from_git` 直接返回 None）—— 业务节点才是有工作副本的
那一端。这正是报告里那句「未读到任何代码 diff」的来路：周版本分析读的是平台已落库的
**窗口合并** diff（那份在平台本地有），而模型要「某一条提交改了什么」时，平台本地那条路
必然算不出东西。

## 为什么在 Agent 侧就渲染成文本，而不是把结构传回去

* 一份 Excel 表的结构化差异可以有几千行（逐行 + 逐单元格的旧值新值），而
  `AgentTask.result_summary` 是一个 TEXT 列 —— 把几 MB 的结构塞进去，**每一次取数都要
  跨节点传一遍、库里再留一份**。超大载荷本平台另有 `AgentTempCache` 那套（提交页的
  `commit_diff` 用的就是它），但那对「按需取一条差异」是过度设计。
* 渲染函数 `render_diff_payload` 是**纯函数**（模块级、不碰 DB 与 Flask），Agent 侧
  直接 import 同一份实现即可 —— 两端渲染出来的文本逐字一致，这正是 `content_window`
  那条「同一份规则只有一份实现」的同一条纪律。
* 传文本还有一个好处：大小可控（下面按 `FILE_DIFF_MAX_BYTES` 截断并**如实写明截断了**），
  而结构的大小取决于表有多大。
"""

from __future__ import annotations

from models import Commit, Repository, db
from services.ai.budget import truncate_bytes
from services.ai.platform_provider import (
    DEFAULT_MAX_ROWS_PER_SHEET,
    render_diff_payload,
)

# 回传给平台的差异文本上限（**字节**，不是字符）。
#
# ## 为什么这里砍一刀就会失真（原先的 11,000 字符为什么是错的）
#
# 平台拿到这段文本之后还要**按改动块分段**（`context_tools` → `windowed_view.render_window`），
# 抬头会写「共 N 段 / 这里是第几段 / 要看别的写 `lines`」—— 而 N 是在**砍过的文本上**数出来的。
# 实测同一份 8 段的差异：整份交给平台是「共 8 段」，先经 Agent 砍到 11,000 再交给平台是
# 「共 4 段」，被砍掉的 5–8 段**没有任何坐标能点回来**（平台从没收到它们），而模型读到的是
# 一份**看起来完整**的段清单 —— 比直接说「被截断了」更危险。
#
# 段的坐标要成立，前提就是**平台拿到整份**。所以这把刀不该按「模型的单条预算」来定，
# 那是平台层的事（`DEFAULT_TOOL_LIMITS["file_diff"]` 一个字没动，模型每次仍然只拿到
# 11,000 字）—— 它只该按**回传通道的物理上限**来定。
#
# ## 48,000 这个数是算出来的
#
# `AgentTask.result_summary` 是 `db.Column(db.Text)`，MySQL 下 TEXT 是 65,535 **字节**，
# 而落库的是**整个 JSON**（还有 file_path / commit_id / original_chars / message 等字段，
# 另加 JSON 里每个换行转义成 `\n` 的两字节开销）。取 48,000 给信封与转义留出余量 ——
# 实测最坏形态（顶满额度的中文短行 / ASCII 补丁）落库 JSON 约 50 KB，**余量约 15 KB**。
# 这个值在 SQLite（TEXT 无实际上限）与 MySQL（65,535 字节）**两边都安全**，所以不必先去
# 确认生产用哪种库。超过那一列的后果不是「少看一段」而是**整条回传失败**（strict 模式下
# commit 抛 DataError → 任务停在 processing 等租约重跑）。
#
# 相比原先的 11,000 **字符**：中文内容约 1.45 倍，ASCII 为主的代码补丁约 4.4 倍 —— 后者正是
# 「AI 看不到代码 diff」的主战场。
FILE_DIFF_MAX_BYTES = 48_000


def read_file_diff_for_agent(payload: dict) -> dict:
    """算一条提交对一个文件的差异，返回平台侧直接给模型看的文本。

    返回体（会被原样 JSON 落到 `AgentTask.result_summary`）：
    `{file_path, commit_id, kind, content, original_chars, truncated, message}`

    `kind` 是给平台侧的分流标记：见到它就直接用 `content`，**不要再按行号包一层**
    （那是正文那条路的形状）。
    """
    repository_id = payload.get('repository_id')
    commit_id = str(payload.get('commit_id') or '')
    file_path = str(payload.get('file_path') or '')
    if not repository_id or not commit_id or not file_path:
        raise ValueError("file_diff 任务缺少 repository_id/commit_id/file_path")

    repository = db.session.get(Repository, int(repository_id))
    if repository is None:
        raise ValueError(f"file_diff 任务的目标仓库不存在: {repository_id}")

    # 与平台侧 `PlatformContextProvider._commit_row` 同一条查法（同一个提交可能有多行，
    # 取最新那一行）。**不能在这里换一种查法**：两端取到不同的行时，同一次索取在不同
    # 部署下会给出不同的差异。
    row = (
        Commit.query.filter_by(commit_id=commit_id, path=file_path)
        .order_by(Commit.id.desc())
        .first()
    )
    if row is None:
        raise RuntimeError(
            f"读取差异失败（平台数据库里没有 {commit_id[:8]} 的这个路径 {file_path}）"
        )

    from services.commit_diff_logic import resolve_previous_commit
    from services.vcs_content_service import get_unified_diff_data

    previous = resolve_previous_commit(row)
    diff_data = get_unified_diff_data(row, previous)

    rendered = render_diff_payload(
        diff_data, path=file_path, max_rows_per_sheet=DEFAULT_MAX_ROWS_PER_SHEET
    )
    if rendered is None:
        # 认不出来的结构：**不能**返回空串（那在这个契约里等于「确实没有差异」）。
        # 抛异常 → 平台侧把原因如实写给模型，它才知道这是个信息缺口。
        kind = ''
        try:
            kind = str((diff_data or {}).get('type') or '')
        except AttributeError:
            kind = type(diff_data).__name__
        raise RuntimeError(f"差异结构无法渲染（type={kind or '未知'}），请人工核对该提交")

    content, truncated = truncate_bytes(rendered, FILE_DIFF_MAX_BYTES)
    return {
        "file_path": file_path,
        "commit_id": commit_id,
        "kind": "diff",
        "content": content,
        # 字符数（不是字节数）：平台拿它写给模型看的那句「原文约 N 字」。平台只看这个数，
        # 所以两端的单位必须是「字」。
        "original_chars": len(rendered),
        "truncated": truncated,
        "message": (
            f"file_diff completed ({len(content)}/{len(rendered)} chars, "
            f"{len(content.encode('utf-8'))} bytes)"
        ),
    }
