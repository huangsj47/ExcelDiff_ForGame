"""提示词缓存（prompt cache）：带不带缓存标记、带哪一种、被拒了怎么办。

## 这个模块解决什么

多轮分析里每一轮都把「系统提示词 + 之前所有轮次」重发一遍。上游的 prompt cache 正是
为这件事存在的：命中的那部分输入 token 按**命中价**计（通常差一个数量级），首 token
延迟也跟着降。能不能命中只取决于一件事：**这次请求的开头与上一次是不是逐字节相同**。

我们这一半已经做对了 —— `engine.run_analysis` 的消息是 append-only 的（system 只构造
一次，之后每轮只 append user / assistant），所以第 2 轮及以后的请求天然是上一轮的
**append-extension**。`tests/test_ai_prompt_cache.py` 把这条性质钉住，防止将来有人把
循环改成「每轮重建 messages」。

本模块处理的是另一半：**要不要在请求里显式声明缓存断点**。这是两件不同的事，混在
一起会得出错误结论（实测数据见下）。

## 三条纪律

**一、能力是声明出来的，不是猜出来的。**
`cache_control` 不是 OpenAI 协议的一部分，是各家网关自己扩展的标记。按「模型名里有
claude」「主机名里有 anthropic」去猜，对**内网网关**天然失效：一个私有域名既可能是
Anthropic 兼容层，也可能是一个完全不认这个字段的转发器 —— 而猜错的代价是一次 400。
所以这里不猜：`prompt_cache_format` 由部署者声明（默认 `none` = 没声明 = 不发标记），
`prompt_cache_mode` 决定声明的强度（见 `resolve_cache_marker`）。

（这条不是我们的洁癖。deepseek-harness 的能力表 `docs/config-catalog.md` 里写着同一件
事：私有网关的 URL 什么也说明不了，按 URL 推断出来的结论「对大多数 OpenAI 兼容网关都是
错的」，所以每一项能力都必须由部署方显式声明。）

**二、分析绝不能因为缓存标记而失败。**
省钱是优化，跑不出结论是事故。所以 `llm_client.complete` 在带标记失败时会去掉标记
**重试一次**，并把该端点记进进程内的黑名单（后续请求不再带标记）。

**三、没有标记 ≠ 没有缓存。**
DeepSeek / OpenAI 这类端点做的是**自动前缀缓存**：什么都不用声明，只要前缀稳定就命中。
2026-09-18 在本机 DeepSeek 形态的网关上实测，一次三轮的 append-only 对话逐轮命中
2176/3043、2944/3661、3584/4279（71.5% → 83.8%），**全程没有带任何标记**。
所以「不发标记」与「关掉缓存」是两件事，只是恰好由同一个模式值表达。

## 断点放在哪里

带标记的形态是「把消息内容从字符串换成一个内容块数组，在最后一块上挂
`cache_control`」。缓存以**断点处的前缀**为单位命中，所以断点必须落在**稳定前缀的末尾**
（跨轮次不变、跨运行也不变的那一段），绝不能落在每轮都在变的内容上 —— 那等于每次都
重新写一遍缓存，还多付一次写入价。`engine.run_analysis` 因此只挂三个断点（Anthropic
上限 4 个，留一个余量），每一个的位置都由 `tests/test_ai_prompt_cache.py` 钉住。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Mapping

# --- 模式（要不要加断点）----------------------------------------------------

# `off`：永不。`auto`：只在端点声明了约定时才加（默认，保守）。
# `explicit`：总是加 —— 用于「我知道这个网关收，但配不出/查不到它属于哪一类」的情形。
CACHE_MODE_OFF = "off"
CACHE_MODE_AUTO = "auto"
CACHE_MODE_EXPLICIT = "explicit"
PROMPT_CACHE_MODES = (CACHE_MODE_OFF, CACHE_MODE_AUTO, CACHE_MODE_EXPLICIT)
# 默认 `auto` + `none` 的结果是**不发标记**，见模块 docstring 第一条与第三条。
DEFAULT_PROMPT_CACHE_MODE = CACHE_MODE_AUTO

# --- 形态（怎么加）----------------------------------------------------------

# `none`：没有声明（默认）。`anthropic`：`{"type": "ephemeral"}` 这一种约定。
# 值域是一份**白名单**而不是自由文本：写错一个字母就会静默退化成「不发标记」，
# 而那看起来与「这个端点不支持缓存」一模一样。
CACHE_FORMAT_NONE = "none"
CACHE_FORMAT_ANTHROPIC = "anthropic"
PROMPT_CACHE_FORMATS = (CACHE_FORMAT_NONE, CACHE_FORMAT_ANTHROPIC)
DEFAULT_PROMPT_CACHE_FORMAT = CACHE_FORMAT_NONE

# 唯一实现的一种标记。`{"type": "ephemeral"}` 是各家兼容层（LiteLLM / OpenRouter /
# Anthropic 兼容网关）共同认的那一种；TTL 默认 5 分钟，多轮分析一次跑几分钟，够用。
_ANTHROPIC_MARKER: Mapping[str, str] = {"type": "ephemeral"}

# 消息上用来**内部**标记「这里要一个断点」的键。它绝不会出现在请求体里：
# 发送前一律由 `strip_cache_breakpoints` / `apply_cache_breakpoints` 摘掉。
# 之所以用消息上的标记而不是 `complete()` 的额外参数：`complete(messages, temperature=)`
# 是本模块与引擎之间的稳定接口，测试里的假 client 都按它实现，加参数会把它们全打穿。
CACHE_BREAKPOINT_KEY = "_cache_breakpoint"

# 一次请求最多挂几个断点。Anthropic 的硬上限是 4，超了直接 400 —— 而 400 会触发
# 下面的「去掉标记重试」，等于把整个功能关掉。留一个余量：编排层现在只用 3 个。
MAX_CACHE_BREAKPOINTS = 4

# 端点黑名单的进程内存储。键是 `(base_url, model)`，值是失败原因（给日志看）。
# 一次分析创建一个 `LLMClient`（见 `ai_analysis_service.build_endpoint_client`），
# 所以「记住这个端点不收」必须放在**进程**级的容器里，放在实例上等于没记。
_REJECTED: dict[tuple[str, str], str] = {}
_REJECTED_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 模式与形态的归一
# --------------------------------------------------------------------------


def normalize_cache_mode(value: Any) -> str:
    """把配置里的模式读成白名单里的一个值，读不出来用默认（`auto`）。

    **不落回 `off`**：`off` 是一个明确的「我就要关掉」的决定，而读不出来的值更可能是
    「这行是老数据 / 有人手工改过库」。默认值与「明确关掉」是两件事。
    """
    text = str(value or "").strip().lower()
    return text if text in PROMPT_CACHE_MODES else DEFAULT_PROMPT_CACHE_MODE


def normalize_cache_format(value: Any) -> str:
    """把配置里的形态读成白名单里的一个值，读不出来用 `none`（= 没声明）。"""
    text = str(value or "").strip().lower()
    return text if text in PROMPT_CACHE_FORMATS else DEFAULT_PROMPT_CACHE_FORMAT


def resolve_cache_marker(mode: Any, cache_format: Any) -> Mapping[str, str] | None:
    """这次请求要不要带缓存标记；带的话用哪一种。`None` = 不带。

    | mode | cache_format | 结果 |
    |---|---|---|
    | `off` | 任意 | 不带 |
    | `auto`（默认） | `none`（默认） | 不带 —— **默认保守**：端点没声明就不发 |
    | `auto` | `anthropic` | 带 |
    | `explicit` | 任意 | 带（用户明确声称这个端点接受标记） |

    `explicit` 在没声明形态时按 `anthropic` 形态发：那是唯一实现的一种约定，而
    「explicit」这句话本身就是「按已知的那一种发」。
    """
    resolved_mode = normalize_cache_mode(mode)
    if resolved_mode == CACHE_MODE_OFF:
        return None
    resolved_format = normalize_cache_format(cache_format)
    if resolved_mode == CACHE_MODE_EXPLICIT:
        return dict(_ANTHROPIC_MARKER)
    # auto：只有端点**声明**了约定才发。
    if resolved_format == CACHE_FORMAT_ANTHROPIC:
        return dict(_ANTHROPIC_MARKER)
    return None


# --------------------------------------------------------------------------
# 消息上的断点标记
# --------------------------------------------------------------------------


def mark_cache_breakpoint(message: dict) -> dict:
    """在**消息**上标一个断点。就地改并返回同一个对象，便于链式构造。"""
    message[CACHE_BREAKPOINT_KEY] = True
    return message


def strip_cache_breakpoints(messages: Iterable[Mapping[str, Any]]) -> list[dict]:
    """发送用的纯净副本：只去掉内部标记，内容一字不动。

    **不带标记时也必须过这一道**：内部键漏进请求体就是一个未知字段，而未知字段正是
    会被网关 400 掉的东西 —— 一个「为了省钱的开关」把分析整个搞失败，是最难查的一类
    回归（失败发生在 HTTP 层，日志里只有一句 400）。
    """
    return [
        {key: value for key, value in message.items() if key != CACHE_BREAKPOINT_KEY}
        for message in messages
    ]


def apply_cache_breakpoints(
    messages: Iterable[Mapping[str, Any]], marker: Mapping[str, str] | None
) -> list[dict]:
    """发送用的请求体消息：带标记的那几条换成「内容块 + cache_control」。

    `marker` 为 `None` 时等价于 `strip_cache_breakpoints`（这一支保证「关掉缓存标记」
    这条路径发出去的东西与功能上线前**逐字节相同**）。

    只转换**内容非空字符串**的消息。内容是空的还挂个断点，等于让上游去缓存一段空前缀，
    而各家对这种请求的处理并不一致 —— 这种消息本来就不该出现，出现了就当它没有标记。
    """
    if marker is None:
        return strip_cache_breakpoints(messages)

    result: list[dict] = []
    used = 0
    for message in messages:
        clean = {key: value for key, value in message.items() if key != CACHE_BREAKPOINT_KEY}
        marked = bool(message.get(CACHE_BREAKPOINT_KEY)) and used < MAX_CACHE_BREAKPOINTS
        content = clean.get("content")
        if marked and isinstance(content, str) and content.strip():
            clean["content"] = [
                {"type": "text", "text": content, "cache_control": dict(marker)}
            ]
            used += 1
        result.append(clean)
    return result


# --------------------------------------------------------------------------
# 端点黑名单：这个端点拒过缓存标记，本进程后续请求不再带
# --------------------------------------------------------------------------


def endpoint_key(base_url: str, model: str) -> tuple[str, str]:
    """黑名单的键。归一化到小写并去空白：同一个端点被写成两种大小写时不该记两条。"""
    return (str(base_url or "").strip().lower(), str(model or "").strip().lower())


def mark_cache_marker_rejected(base_url: str, model: str, reason: str = "") -> None:
    """记住「这个端点不接受缓存标记」。

    只在**去掉标记重试成功之后**才调用（见 `llm_client.complete`）：第一次失败的
    原因可能压根不是标记（密钥过期、网关抽风），那时候把端点拉黑等于把功能永久关掉。
    """
    with _REJECTED_LOCK:
        _REJECTED[endpoint_key(base_url, model)] = str(reason or "")


def cache_marker_rejection_reason(base_url: str, model: str) -> str | None:
    """这个端点拒过标记吗？拒过返回原因，没拒过返回 `None`。"""
    with _REJECTED_LOCK:
        return _REJECTED.get(endpoint_key(base_url, model))


def cache_marker_rejections() -> dict[tuple[str, str], str]:
    """当前记下的全部黑名单（副本）。给日志与测试用。"""
    with _REJECTED_LOCK:
        return dict(_REJECTED)


def reset_cache_marker_rejections() -> None:
    """清空黑名单。给测试用 —— 它是进程级状态，用例之间必须互不影响。"""
    with _REJECTED_LOCK:
        _REJECTED.clear()
