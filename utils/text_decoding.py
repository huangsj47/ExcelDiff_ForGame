#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一段字节 → 文本：**全平台唯一的一份解码实现**。

## 为什么必须有这一个模块

同一份文件在一次索取里会被**两条部署路径**读到：单机模式下平台自己读工作副本，多节点
模式下由业务节点（Agent）读。两条路读到的字节完全相同，但只要解码口径不同，模型拿到的
文本就不同 —— 于是「同一份请求在单机与多节点下给出不同的结论」，而且**不报错**。

这个毛病在「行窗口」上早就守住了（`utils/content_window.slice_lines` 是唯一实现，
`platform_provider._render_agent_file_content` 与 `agent_file_content_reader` 都走它），
在「编码」上却漏了：平台侧原先 `raw.decode("utf-8")` 严格解码、失败就回一句
「[无法展示的内容] …不是文本…」（**一条假的信息缺口**：GBK 的 lua 明明读得到，
模型却被告知读不到，于是把「读到了但没读懂」写成「这里没有内容」）；Agent 侧则是
`raw.decode('utf-8', errors='replace')` —— 同一串字节，一边说读不了、一边给出乱码。
本仓库的 `services/diff_service.py` 早就有一份五级兜底（`utf-8 → gbk → gb2312 →
latin-1 → cp1252`），所以线上同一个文件在「diff 引擎」与「AI 取数」里是两种文本。

## 这份实现的契约

* `decode_text_bytes(raw)`：**任何**字节都能出文本，**绝不返回 None、绝不抛异常**。
  顺序是 `utf-8 → gbk → gb2312 → latin-1 → cp1252`，最后一档 `errors='replace'` 兜底。
  顺序本身就是口径的一部分（utf-8 优先，中文项目里 GBK 第二），改顺序就是改文本。
* `looks_binary(raw)`：**真正的二进制**判据（ZIP/PNG/OLE/gzip 魔数，或含 NUL 字节）。

## 为什么「能不能解码」不能当二进制判据

`latin-1`（以及 `cp1252` 的绝大部分）能解码**任意**字节序列，所以「解不出来」判不出
二进制；反过来「按 utf-8 解不出来」也判不出 —— 中文项目里 GBK 的 lua、gb2312 的配置
都是合法文本，按 utf-8 解必然失败。原先 `services/ai/reference_search.is_binary` 就是
这么写的：任何非 UTF-8 内容都被判成二进制并跳过搜索，抬头还只写「N 个不是文本（配表等
二进制，没搜）」，于是**GBK 的 lua 一个词都搜不到**，而模型看到的那句话把「编码不兼容」
说成了「它是二进制」。判据换成 `looks_binary` 之后，编码不兼容不再等于二进制。

魔数清单与 `services/diff_service.looks_like_text`（NUL 那一条）、
`services/ai/reference_search._BINARY_MAGIC` 是同一套口径：一处改了，另外两处必须跟着改。
"""

from __future__ import annotations

from typing import Optional

# 依次尝试的编码。**顺序是口径的一部分**：utf-8 优先（今天的绝大多数文件），
# 中文项目里 GBK 第二，`gb2312` 是 GBK 的子集（老配置表），`latin-1` 是「任何字节都能解」
# 的那一档，`cp1252` 是 Windows 西文环境里比 latin-1 更常见的写法。
# 这五档与 `services/diff_service._decode_text` 原先那份逐字一致 —— 收敛而不是新发明。
TEXT_ENCODINGS = ('utf-8', 'gbk', 'gb2312', 'latin-1', 'cp1252')

# 二进制魔数：ZIP（xlsx 也是 ZIP）、PNG、OLE（.xls/.xlsb）、gzip。
# 与 `services/ai/reference_search._BINARY_MAGIC` 同一份清单（那份现在引用这里）。
BINARY_MAGIC = (b'PK\x03\x04', b'\x89PNG', b'\xd0\xcf\x11\xe0', b'\x1f\x8b')

# 「是不是文本」只嗅开头这么多字节：判断类型不需要读完整个文件，而大文件读全文纯属浪费。
# 与 `services/diff_service._TEXT_SNIFF_BYTES` 同一个值。
_BINARY_SNIFF_BYTES = 8192


def decode_text_bytes(raw) -> str:
    """字节 → 文本。**任何输入都有返回，绝不 None、绝不抛异常。**

    `str` 原样返回（两条路上游的返回类型并不统一：`get_file_content_from_git` 在
    某些分支返回的是已经解好的字符串），`None`/空 返回空串。
    """
    if raw is None:
        return ''
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        # 走到这里说明上游给了一个既不是文本也不是字节的东西。**不当成错误**：
        # 取数层的契约是「拿不到」要明说，「读到了但形态意外」不该让整次分析炸掉。
        return str(raw)
    if not raw:
        return ''
    data = bytes(raw)
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # 理论到不了这里（latin-1 对任意字节都成功），留一档是**防止编排顺序被改动**
    # （例如有人把 cp1252 提到 latin-1 前面）：那时这里仍然保证出文本。
    return data.decode('utf-8', errors='replace')


def looks_binary(raw) -> bool:
    """这段字节是不是**真正的二进制**（而不是「按 utf-8 解不出来」的文本）。

    两条判据，与 `services/diff_service.looks_like_text` 同源：

    * 开头命中二进制魔数（ZIP/PNG/OLE/gzip）—— 配表 xlsx 走的就是这一条；
    * 前若干字节里含 NUL —— `.bin`/`.woff2`/加密资源这类没有固定魔数的二进制的可靠信号。

    `None` 算「没有可展示的文本」（与 `reference_search.is_binary` 原先对 `None` 的口径
    一致：调用方拿到 None 时本来就该走「读不到」那条路）；**空字节串是「空文本」**，
    返回 False —— 空内容的契约是空串（`ContextProvider` 的「确实没有内容」），不是这句话。
    """
    if raw is None:
        return True
    if isinstance(raw, str):
        return '\x00' in raw
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return True
    data = bytes(raw)
    if not data:
        return False
    if any(data.startswith(magic) for magic in BINARY_MAGIC):
        return True
    return b'\x00' in data[:_BINARY_SNIFF_BYTES]


def binary_content_notice(path: str) -> str:
    """读到真正的二进制时给模型的那句话。

    **只写「不是文本」，不写「没有内容」** —— 这两件事在模型那里必须分得开：前者说
    「这份东西没法用文本核对」（它可能是张图、一个压缩包），后者说「这里什么都没有」。
    两句话的模板放在这里（而不是各写一遍）是因为平台本地与 Agent 两条路都要发它：
    措辞不一致就等于同一份索取在两种部署下给出不同的说法。

    措辞里带 `[无法展示的内容]` 这个前缀是有意的：`services/ai/trace_evidence.py` 的
    `FAILURE_NOTICE_PREFIXES` 按前缀认「这一轮到底看没看到东西」，前缀变了那边就认不出来
    （面板上会把一次取数失败显示成「读到了内容」）。
    """
    return (
        f"[无法展示的内容] {path}：这份内容的字节不是文本（配表会走配表那条路），"
        "无法以文本形式核对。**这不等于「没有内容」。**"
    )


def text_or_notice(raw) -> Optional[str]:
    """给模型看的正文：是文本就返回解好的文本，是二进制返回 `None`。

    调用方拿到 `None` 时应发 `binary_content_notice(path)` —— 拆成两个函数是为了让
    「解码」与「措辞」各自可单测，而两条部署路径**都调这一个函数**做判断。
    """
    if looks_binary(raw):
        return None
    return decode_text_bytes(raw)
