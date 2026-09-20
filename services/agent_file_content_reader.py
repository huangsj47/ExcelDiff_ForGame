#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 端：从**本机的工作副本**里读一个文件的正文，按窗口切成给模型看的那一段。

与 `services/agent_file_content_dispatch.py` 是一对：那边是平台端「向 Agent 要」，
这边是 Agent 端「读给它」。分开成两个文件还有个实际理由 —— 平台的
`task_worker_service.py` 已经贴着 2000 行的硬上限（`scripts/check_file_length.py`），
把这段读内容的逻辑塞进去会直接顶穿它。

## 为什么是 git（而不是新引入一个取代码的服务）

工作副本在同步（auto_sync）时就已经建好了，读历史版本用 `git show <commit>:<path>`
即可 —— git 是开源工具、本来就在依赖里，不引入任何新服务、新端口、新凭证。
平台侧的 `get_file_content_from_git` 走的是同一条路：**同一份实现，两端行为一致**。

## 为什么按窗口切，而不是把整份文件传回去

模型问正文是为了看**改动周围**长什么样（改动本身已经在 diff 里了）。一份几千行的
lua 整个传回去，代价是三份：跨节点传输、库里留一份、以及最贵的 —— 提示词里塞进几千行
与本次评审无关的代码。所以按请求里的 `lines`（如 `"1180-1260"`）切；没给窗口就从开头
给到字符上限，并**如实写上整份文件有多少行、给的是哪一段**，让模型知道它手里的是片段。
"""

from __future__ import annotations

from models import Repository, db
from utils.content_window import CONTENT_MAX_CHARS, slice_lines
from utils.text_decoding import binary_content_notice, text_or_notice


def read_file_content_for_agent(payload: dict) -> dict:
    """读一个文件的正文，返回会给模型看的那一段。

    返回体（会被原样 JSON 落到 `AgentTask.result_summary`）：
    `{file_path, commit_id, content, start_line, end_line, total_lines, truncated, message}`
    """
    repository_id = payload.get('repository_id')
    commit_id = str(payload.get('commit_id') or '')
    file_path = str(payload.get('file_path') or '')
    if not repository_id or not commit_id or not file_path:
        raise ValueError("file_content 任务缺少 repository_id/commit_id/file_path")

    repository = db.session.get(Repository, int(repository_id))
    if repository is None:
        raise ValueError(f"file_content 任务的目标仓库不存在: {repository_id}")

    from services.vcs_content_service import get_file_content_from_git

    raw = get_file_content_from_git(repository, commit_id, file_path)
    if raw is None:
        # 取不到就**明说**：抛异常 → Agent 报 failed → 平台侧把原因写给模型。
        # 不能返回空正文 —— 那与「这个文件是空的」分不开。
        raise RuntimeError(f"读取文件内容失败（工作副本里没有 {commit_id[:8]} 的这个路径）")

    if isinstance(raw, str):
        text = raw
    else:
        # **配表要先分流**（2026-09-19）。xlsx 是 ZIP，按 UTF-8 解码（哪怕 errors='replace'）
        # 得到的是二进制乱码，而抬头还会写「共 N 行；下面是第 a–b 行」—— 模型据此写出的
        # 结论全是错的，比「读不到」更糟（它不知道自己拿到的不是内容）。
        #
        # 渲染函数直接复用平台本地那条路的 `_read_excel_sheets`（模块级纯函数，不碰
        # Flask/DB）：两端必须是**同一份实现**，否则同一次索取在单机与多节点下会给出
        # 不同的文本 —— 那正是 `utils/content_window` 那条纪律要防的事。
        # 从别的模块 import 一个下划线开头的 helper 在本仓库有先例
        # （`services/task_worker_service.py` 就是这么引 `_attach_author_display` 的）。
        from services.ai.platform_provider import (
            DEFAULT_MAX_ROWS_PER_SHEET,
            _is_openpyxl_workbook,
            _read_excel_sheets,
        )

        if _is_openpyxl_workbook(file_path):
            # `lines` 在配表上是「第几张工作表」（见 `platform_provider.parse_sheet_window`），
            # 与平台本地那条路同一套坐标 —— 两端必须一致，否则同一次索取在单机与多节点下
            # 给出不同的文本。
            #
            # 表头坐标（`Repository.header_rows` / `header_name_row`）从**这个仓库行**上读：
            # Agent 本来就要按 `repository_id` 把仓库查出来（上面那几行），所以这两个值
            # 与平台本地那条路拿到的是同一行记录上的同一对字段。**不放进 payload** 是有意的：
            # 放进 payload 就多一份可能过期的副本，而 `_matches` 的请求指纹里也不含它们 ——
            # 配置改了之后旧任务会被当成同一份请求复用，正文却按新配置渲染。
            rendered = _read_excel_sheets(
                raw,
                max_rows=int(payload.get('max_rows') or DEFAULT_MAX_ROWS_PER_SHEET),
                path=file_path,
                window=str(payload.get('lines') or ''),
                char_budget=int(payload.get('max_chars') or 0) or CONTENT_MAX_CHARS,
                header_rows=getattr(repository, 'header_rows', None),
                header_name_row=getattr(repository, 'header_name_row', None),
            )
            if rendered is None:
                raise RuntimeError(
                    f"配表内容无法解析成文本表格（{file_path} 不是可读的 OOXML 工作簿，"
                    "或文件已损坏）"
                )
            return {
                "file_path": file_path,
                "commit_id": commit_id,
                # `kind` 是给平台侧的分流标记：见到它就直接用 `content`，**不要再按行号
                # 包一层**（配表的正文没有「第 a–b 行」这个概念）。
                "kind": "excel",
                "content": rendered,
                "message": f"file_content completed (excel, {len(rendered)} chars)",
            }

        # 解码走 `utils.text_decoding`（**两端唯一一份实现**）。
        #
        # 修前这里是自己写的 `raw.decode('utf-8')` + `errors='replace'` 兜底，而平台本地
        # 那条路是严格 `raw.decode("utf-8")`、失败回一句「[无法展示的内容] …不是文本…」。
        # 同一串字节于是有两份不同的文本：GBK 的 lua（本仓库最常见的形态之一）在 Agent 侧
        # 是一堆带替换符的乱码、在平台侧干脆被判成「读不到」—— 而模型的结论正是从这段正文
        # 里写出来的，行号与取值都会跟着错。
        text = text_or_notice(raw)
        if text is None:
            # 真正的二进制（魔数 / NUL）：回**同一句话**，并用 `kind: "binary"` 告诉平台侧
            # 「不要再按行号包一层」（见 `platform_provider._render_agent_file_content`）。
            # 那句话的模板在 `utils.text_decoding` 里 —— 两端逐字相同是有意的：同一次索取
            # 在单机与多节点下必须给出同一份文本，否则「哪句话是真结论」就取决于部署方式。
            notice = binary_content_notice(file_path)
            return {
                "file_path": file_path,
                "commit_id": commit_id,
                "kind": "binary",
                "content": notice,
                "message": "file_content completed (binary, 无法以文本形式核对)",
            }

    # 行数与「切哪一段」都由 `utils/content_window.slice_lines` 决定 —— 那是窗口规则的
    # 唯一实现，平台侧读得到正文时用的是同一个函数（两端各写一遍必然漂移，而漂移的表现
    # 是同一份请求在不同部署下给出不同行号）。
    window = slice_lines(
        text, payload.get('lines'), max_chars=int(payload.get('max_chars') or 0) or CONTENT_MAX_CHARS
    )

    return {
        "file_path": file_path,
        "commit_id": commit_id,
        "content": window.content,
        "start_line": window.start_line,
        "end_line": window.end_line,
        "total_lines": window.total_lines,
        "truncated": window.truncated,
        "message": (
            f"file_content completed "
            f"({window.start_line}-{window.end_line}/{window.total_lines})"
        ),
    }
