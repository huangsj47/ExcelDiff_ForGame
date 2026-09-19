#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""按 `commit_id` 找回提交记录 —— 「短 SHA 才是前缀」这一条口径的唯一实现。

## 为什么要单独一个模块

平台上有两处「拿一个 `commit_id` 字串，去 `Commit` 表里找回那一行」的查询：

* `services/weekly_version_file_handlers.py`（取不到文件内容时，退回到那条提交的 diff 页）
* `services/weekly_deleted_excel_helpers.py`（「删除前的上一版」链接）

两处都写成了同一句：`len(commit_id) >= 40` 就精确匹配，否则
`commit_id.like(f"{commit_id}%")`。**这是给 Git 短 SHA 写的**（`a1b2c3` 是
`a1b2c3d4…` 的缩写，所以要按前缀找全长的那一行）。而 SVN 的 `commit_id` 是
`r19` 这样的**修订号**（`services/svn_service.py` 里是 `f'r{revision}'`）——
它不是任何东西的缩写，前缀匹配在这里纯属**匹配到了别的版本**：

    like("r1%")  同时匹配 r1、r10、r11、r19、r100 …

`.first()` 拿哪一条取决于数据库的返回顺序（没有 `order_by`），于是：

* 「删除前的上一版」链接可能指向**另一个版本** —— 用户点开看到的是别人的 diff，
  而页面本身没有任何异常；
* `weekly_version_file_handlers` 那条更直接：它会 302 到**错误的提交**上去，
  用户以为自己打开的是这个文件的那一版。

**`r` 不是十六进制数字**，所以 `^r\d+$` 与 Git 的 `[0-9a-f]{7,40}` 不可能互相
误判 —— 形状本身就是可靠的判据，不需要再读 `repository.type`（那个参数在有的
调用点上拿到的是 `None`，靠它分支反而会漏）。
"""
from __future__ import annotations

import re

# SVN 修订号：`r` + 数字。Git 的 SHA 是十六进制，`r` 不在其中，两者形状不相交。
_SVN_REVISION_RE = re.compile(r"^r\d+$")


def is_svn_revision(commit_id) -> bool:
    """这个 `commit_id` 是不是 SVN 的修订号（`r19` 这种）。

    注意它回答的是**形状**问题，不是「这个仓库是不是 SVN」——
    平台把 SVN 的修订号原样存成 `commit_id`（带 `r` 前缀），所以形状就够用了。
    """
    return bool(_SVN_REVISION_RE.match(str(commit_id or "").strip()))


def svn_revision_number(commit_id) -> str:
    """`r19` → `'19'`；本来就没有前缀就原样返回。

    不用 `str.replace('r', '')`：那个会把字串里**每一个** `r` 都删掉。
    今天 `r19` 里只有一个 `r` 所以看不出差别，但那是「输入恰好简单」而不是
    「写法正确」—— 换一个来源（`r19` 之外的形态）就会静默算出另一个修订号。
    """
    text = str(commit_id or "").strip()
    if _SVN_REVISION_RE.match(text):
        return text[1:]
    return text


def find_commit_by_commit_id(
    commit_model,
    *,
    repository_id,
    file_path,
    commit_id,
):
    """在同一个仓库 + 同一个路径下，按 `commit_id` 找回那条提交（找不到回 `None`）。

    顺序是**先精确、后前缀**，且前缀只对「不像 SVN 修订号」的串才试：

    1. 精确匹配 —— 全长 SHA、SVN 的 `r19` 都在这一步命中；
    2. 还找不到，且这个串不是 SVN 修订号 → 当成 Git 短 SHA，按前缀找一条。

    精确优先本身也是修一个既有毛病：原实现是「短串先试前缀」，于是即使表里
    **正好有** `commit_id == 'a1b2c3'` 这一行，也可能先被 `like` 匹配到
    `a1b2c3ff…` 那一行去。
    """
    text = str(commit_id or "").strip()
    if not text:
        return None
    query = commit_model.query.filter(
        commit_model.repository_id == repository_id,
        commit_model.path == file_path,
    )
    exact = query.filter(commit_model.commit_id == text).first()
    if exact is not None:
        return exact
    if is_svn_revision(text):
        # SVN 修订号没有「缩写」这回事：精确找不到就是找不到，**不许退化成前缀**。
        return None
    if not hasattr(commit_model.commit_id, "like"):
        return None
    return query.filter(commit_model.commit_id.like(f"{text}%")).first()
