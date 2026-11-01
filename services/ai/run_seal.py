#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建 run 之前**重冻**目标快照（WP9：seal 重试丢快照的修复）。

## 为什么必须每次尝试都重冻

`_create_run` 的幂等判据是 `active_key` 上的 **UNIQUE 索引**：撞上时它先
`db.session.rollback()` 再重试。而 `snapshot_store.seal_snapshot` **只 flush 不 commit**
（快照必须与它服务的那条 run 同生共死），于是那次 rollback 会把刚冻好的快照一起回滚掉。

原先 seal 调用在重试循环**外面**：第一次撞唯一索引、第二次成功的那条路上，第二条 run
的 `request_payload["snapshot"]` 记着一个**已经被回滚掉的 snapshot_id**。后果不是「少一行
账」，而是：这次运行没有快照 → 下一轮 `resolve_baseline` 找不到基准 → 真首跑（整窗
全量），也就是审查报告 2.3 节点名的那个放大项。

所以 seal 必须在**每次** `INSERT` 之前做一次：第一次那一份随 rollback 消失，第二次重新
冻一份（同一份内容指纹，库里已有就复用），再与 run 一起提交。

## 为什么单独一个模块

`services/ai_analysis_service.py` 贴着 2000 行硬上限（契约 §0），而这段「重试语义」值得
一段完整的解释；抽出来之后调用点只留一行。
"""

from __future__ import annotations

from typing import Optional

from services.ai.baseline_blocks import seal_target_snapshot


def seal_attempt_snapshot(payload: dict) -> Optional[dict]:
    """冻一份目标快照并写进 `payload["snapshot"]`；没有可冻的清单时返回 `None`。

    * 成功：把账（`snapshot_id` / `content_digest` / `item_count` / `complete`）写进
      payload，随这次 `INSERT` 一起去提交；
    * 失败/空清单：**清掉上一次尝试留下的账**。不清的话，重试路径会把一个已经随
      rollback 消失的 id 写进新 run 的 payload —— 那正是本模块要修的那种「账上有、
      库里没有」。

    单提交模式没有「这一批配置」的清单（payload 里没有 `group.config_ids`），
    `seal_target_snapshot` 本来就返回 `None`，所以这里不需要按模式分支。
    """
    account = seal_target_snapshot(payload)
    if account:
        payload["snapshot"] = account
    else:
        payload.pop("snapshot", None)
    return account
