#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JSON 请求体的形状校验：根节点必须是对象。

## 为什么需要这一层

路由里普遍写着 `payload = request.get_json(silent=True) or {}`，然后直接
`payload.get(...)` / `dict(payload)`。这个 `or {}` 只兜 **falsy**，而 `[1]`、`"str"`、
`123`、`true` 都是 **truthy** —— 它们原样穿过，紧接着在 `.get` / `dict()` 上抛
AttributeError / TypeError，被兜成 **500**。

这些输入是**调用方可控**的，所以这不是「理论上的边界情况」：

* `/api/agents/*` 既不需要登录、也不做 CSRF 校验（探针与 Agent 装机场景），
  任何人发一个 `[1]` 就能打出 500；
* 其余写接口只需一个登录会话（CSRF token 就在自己的 session 里）。

代价也不只是「状态码不好看」：响应里拿不到「哪里错了」，用户只看到服务器内部错误；
日志里堆的是未捕获异常栈，真正的故障会被这些噪声埋掉。个别 handler 更糟 —— 它把
`'list' object has no attribute 'get'` 这句**内部异常文本**当 message 回给调用方
（`agent_upsert_temp_cache` 的 400 分支），既没用又算信息泄漏。

## 为什么是「工具」而不是一个全局 before_request

全局钩子会顺带改掉那些**今天压根不读 body** 的接口的行为（它们收到数组 body 现在是
200），那是未经要求的契约变更。本模块只替换掉「今天就会 500 的读法」，一处
实现、各调用点一行。

## 没带 body 与 body 不是合法 JSON 为什么不报错

保持 `silent=True` 的既有语义：这两种情况都得到 `{}`，等价于「什么都没提交」，
由各接口自己的必填校验去报（缺字段本来就是 400）。把「不是合法 JSON」也升成 400
属于另一件事（会改变 `Content-Type` 写歪时的既有行为），不在本次范围内。
"""

from __future__ import annotations

from typing import Any

from flask import jsonify, request

STRUCTURE_ERROR_MESSAGE = "请求体必须是 JSON 对象。"


def read_json_object(message: str = STRUCTURE_ERROR_MESSAGE, *, silent: bool = True):
    """读 JSON 请求体，返回 `(payload, error_response)`。

    * 正常 body / 没有 body / body 不是合法 JSON → `(dict, None)`；
    * 根节点是数组 / 字符串 / 数字 / 布尔 → `(None, (响应, 400))`。

    用法（两行，不改变各接口原有的错误响应形状）：

        payload, error = read_json_object()
        if error is not None:
            return error

    `silent=False` 供原先写 `request.get_json()`（不带 silent，即**希望**
    Content-Type 不对时报 415）的调用点使用：形状判定之外，其它语义保持原样。

    响应体同时带 `success` 与 `status` 两个键：仓库里两种判定都在用
    （多数接口读 `data.success`，仓库排序等页面读 `data.status === 'success'`），
    只给其中一个，另一类前端会拿到 `undefined` 并把「请求体不合法」显示成
    「未知错误」。
    """
    payload: Any = request.get_json(silent=silent)
    if payload is None or isinstance(payload, dict):
        return payload or {}, None
    return None, (
        jsonify({"success": False, "status": "error", "message": message}),
        400,
    )
