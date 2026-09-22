#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目代号的唯一性口径：**折成同一个知识包目录的代号不许并存**。

## 为什么需要一个额外的判据

`project.code` 上已经有 `unique=True`，但它**大小写敏感**（SQLite 的 TEXT UNIQUE 走
BINARY 排序）。而项目知识包的目录名走 `skill_loader.project_pack_slug`，它会 `lower()`
并把非法字符折成连字符 —— 于是 `QAREV42` 与 `qarev42`、`QAREV-42` 与 `qarev_42` 都映到
**同一个目录**。

这不是「改个名就能躲开」的事：Windows 的文件系统同样不区分大小写，两个只差大小写的
代号在那台机器上**本来就只能是同一个目录**。后果是第二个项目读写知识包时直接落到第一个
项目的包上（REV-KNOW-001）。`project_pack_service` 那一侧已经有归属标记兜底（访问时
明确拒绝），但更早、更清楚的做法是**在建项目那一步就说不行** —— 否则用户会看到一个
莫名其妙「代号被占用」的知识包面板，而不是「这个代号不能起」。

## 判据只有一份

四个建项目入口（Web 建项目、Agent 上报项目、Qkit 建项目申请的两个分支）都调这里。
入口各写一遍的话，早晚会有一个漏掉 —— 而漏掉的那个入口就是漏洞本身。
"""
from __future__ import annotations

from typing import Optional

from models import Project, db
from services.ai.skill_loader import project_pack_slug


def slug_conflict(code: str, *, exclude_project_id: Optional[int] = None) -> Optional[Project]:
    """找与本代号**折成同一个知识包目录**、但代号原文不同的那个项目。没有就返回 `None`。

    代号原文**完全相同**的不算冲突 —— 那种情况由调用方既有的「代号已存在」判据处理，
    这里只负责它盖不住的那一类。`exclude_project_id` 给「改自己」的场景留的。

    代价是一次全表扫描（只取 id 与 code 两列）。建项目是低频动作，而判据本身是**派生值**
    （slug 由 Python 算，SQL 里没有这一列），所以只能查回来再比。
    """
    slug = project_pack_slug(code or "")
    if not slug:
        return None
    wanted = str(code or "").strip()
    for project_id, existing_code in db.session.query(Project.id, Project.code).all():
        if exclude_project_id is not None and project_id == exclude_project_id:
            continue
        if str(existing_code or "").strip() == wanted:
            continue
        if project_pack_slug(existing_code or "") == slug:
            return db.session.get(Project, project_id)
    return None


def slug_conflict_message(code: str, project: Project) -> str:
    """给用户看的一句话。**要说出「为什么」，不能只说「已存在」**。

    只说「代号已存在」的话，用户会去项目列表里找 `qarev42` 却只看到 `QAREV42`，
    然后以为平台坏了。
    """
    return (
        f"项目代号「{code}」已被项目「{project.name}」（代号 {project.code}）占用："
        "两个代号折成同一个项目知识包目录（大小写不同、或非法字符折算后相同），"
        "而 Windows 上那样两个目录本来就是同一个。请换一个明显不同的代号。"
    )
