"""OpenAI 兼容的模型客户端。

## 为什么用 `requests` 手写而不用官方 SDK

`requests==2.31.0` 已经在 `requirements.txt` 里，而 `openai` / `anthropic` 都不在。
这个仓库的依赖一直很克制，为了一个 `POST /chat/completions` 引入一整套 SDK 不划算，
而且内部网关的兼容层经常只实现子集，SDK 反而会因为严格校验而失败。

## 重试栈刻意做得很浅

要评审的那份外部工具叠了四层重试（HTTP 4 次 × 预算降级 4 次 × 整单 2 次 × 批量队尾
1 次），单个任务最坏 32 次调用，且每层各自 sleep、没有全局 deadline —— 一个坏任务
就能拖垮整个队列。这里只保留**一层**：传输层最多 `MAX_ATTEMPTS` 次，指数退避 + 上限，
并尊重上游的 `Retry-After`。上层的「换更小的上下文再试」是另一回事（那是构造问题，
不是网络问题），由编排层决定，不在这里叠。

## 密钥绝不出现在错误信息里

错误信息会被写进日志、写进数据库的 `error_message`、再展示到页面上。上游返回体、
URL、异常字符串都可能带上密钥（内网网关把 token 放在 query 里并不罕见），所以一律
先过 `redact_secret`。

## 提示词缓存标记：带了要能跑，带不了要能退

多轮分析的每一轮都把「系统提示词 + 之前所有轮次」重发一遍，上游的 prompt cache 就是
为这件事存在的。要不要显式挂缓存断点由 `services/ai/prompt_cache.py` 决定（能力是
**声明**出来的，不是按主机名猜出来的），这里只负责一件事：**分析绝不能因为缓存标记
而失败**。所以带标记的请求失败时，会自动去掉标记重试一次；重试成功说明这个端点不认
这个字段，于是把它记进进程内的黑名单（后续请求不再带）并记一条日志 —— 用户看到的
是一次正常的分析，而不是「HTTP 400 未知字段」。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlparse, urlunparse

import requests

from services.ai.prompt_cache import (
    DEFAULT_PROMPT_CACHE_FORMAT,
    DEFAULT_PROMPT_CACHE_MODE,
    apply_cache_breakpoints,
    cache_marker_rejection_reason,
    mark_cache_marker_rejected,
    normalize_cache_format,
    normalize_cache_mode,
    resolve_cache_marker,
)
from utils.logger import log_print
from utils.security_utils import sanitize_text

# 传输层重试上限。刻意不设成 4 层叠加 —— 见模块 docstring。
MAX_ATTEMPTS = 3
# 指数退避的基础与上限（秒）。上限防止上游持续过载时把 worker 占死。
BACKOFF_BASE_SECONDS = 1.5
BACKOFF_MAX_SECONDS = 20.0
# 默认单次请求超时（秒）。多轮分析里每一轮都可能是一次完整生成，所以给得比较宽。
DEFAULT_TIMEOUT_SECONDS = 300
# 模型列表最多回传多少条：防止某个网关返回上千条把页面和上下文都撑坏。
MAX_MODELS = 500
# 模型列表响应体大小上限：防御上游返回异常大的响应。
MODELS_RESPONSE_MAX_BYTES = 2 * 1024 * 1024

# `/v1/models` 里声明上下文窗口的常见字段名。各家写法不一样（vLLM 是 `max_model_len`，
# 有的网关是 `context_length`，有的用 `context_window`），这里都认一遍。
CONTEXT_WINDOW_KEYS = (
    "context_length",
    "context_window",
    "max_context_length",
    "max_context_tokens",
    "max_model_len",
    "n_ctx",
)
# 窗口的合理区间。落在外面的值一律当成「没声明」而不是当真：把 `max_tokens`（单次最多
# 生成多少）误读成窗口、或把 `n_ctx` 报成 0，都会让预算被压到一个荒唐的值。
_MIN_PLAUSIBLE_CONTEXT = 4_000
_MAX_PLAUSIBLE_CONTEXT = 100_000_000

# 这些 HTTP 状态码值得重试：限流与上游故障。
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# 少数 OpenAI 兼容网关会在连接复用/请求解析瞬态失败时回 400，并只给这句固定文本。
# 它没有任何“哪个配置字段不合法”的语义；真实运行中一次这样的响应曾让已经完成的
# 三个分析分片全部作废。只对白名单中的**精确通用文案**重试，避免把 invalid model / key
# 这类确定性的 400 也打三遍。
RETRYABLE_BAD_REQUEST_DETAILS = frozenset({"invalid http request received."})

# 会被收窄的异常元组（与仓库其它模块的 `*_ERRORS` 约定一致，
# 便于 tests/test_*_exception_narrowing.py 这类守卫统一检查）。
LLM_TRANSPORT_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)
LLM_RESPONSE_ERRORS = (
    ValueError,  # json.JSONDecodeError 是它的子类，不必单列
    TypeError,
    KeyError,
    AttributeError,
)


class LLMError(RuntimeError):
    """模型调用相关的错误基类。"""


class LLMConfigError(LLMError):
    """配置问题（地址非法、缺模型名等）。**不该重试**——重试也不会变好。"""


class LLMTransportError(LLMError):
    """传输/上游故障。已耗尽重试次数。"""


class LLMResponseError(LLMError):
    """拿到了响应但无法解析。"""


def redact_secret(text: str, secret: str | None = None) -> str:
    """抹掉文本里的密钥与 URL 里的凭据。

    两层：先把显式传入的密钥替换掉（上游有时会把 token 回显在错误体里），再过一遍
    仓库既有的 `sanitize_text`（它认常见的凭据形态）。宁可多抹，也不能把密钥写进
    日志或数据库。
    """
    redacted = str(text or "")
    secret = str(secret or "").strip()
    if secret:
        redacted = redacted.replace(secret, "<redacted>")
    return sanitize_text(redacted)


def normalize_base_url(base_url: str) -> str:
    """把用户填的各种形态归一成「协议 + 主机 + 可选前缀」的 base。

    用户会填出很多形态，最常见的是把**完整端点**粘进来：

        https://host/v1/chat/completions   →  https://host/v1
        https://host/v1/models             →  https://host/v1
        https://host/v1/                   →  https://host/v1
        https://host                       →  https://host

    不归一的话会拼出 `https://host/v1/chat/completions/models` 这种低级错误，而它
    表现为「模型列表拿不到」——用户只会以为自己 token 配错了。
    """
    raw = str(base_url or "").strip()
    if not raw:
        raise LLMConfigError("未配置模型接口地址")

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise LLMConfigError(f"模型接口地址必须以 http:// 或 https:// 开头：{redact_secret(raw)}")
    if not parsed.netloc:
        raise LLMConfigError(f"模型接口地址缺少主机名：{redact_secret(raw)}")

    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break

    # 去掉 URL 里的凭据：这个地址会进日志，凭据不该跟着走。
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, path, "", "", "")).rstrip("/")


def chat_completions_url(base_url: str) -> str:
    return f"{normalize_base_url(base_url)}/chat/completions"


def models_url(base_url: str) -> str:
    return f"{normalize_base_url(base_url)}/models"


def _positive_int(raw: Any) -> int | None:
    """把可能是数字也可能是数字字符串的值读成正整数，读不出来返回 None。

    字符串也认：同一个字段有的网关给 `65536`、有的给 `"65536"`。
    `True`/`False` 与 `"abc"` 一律算「没给」—— 认不出来就当没有，**不猜**。
    """
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _build_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = str(api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _retry_after_seconds(response: "requests.Response | None") -> float | None:
    """读上游的 `Retry-After`（秒数形态）。

    有它就以它为准——这是上游在明确告诉我们多久之后再来，比我们自己猜退避曲线准。
    只认数字形态，HTTP-date 形态在本场景不常见，解析它得不偿失。
    """
    if response is None:
        return None
    raw = str(response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, BACKOFF_MAX_SECONDS)


def _backoff_seconds(attempt: int) -> float:
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)


def _should_retry_status(status_code: int, detail: str = "") -> bool:
    if status_code in RETRYABLE_STATUS_CODES:
        return True
    return (
        status_code == 400
        and str(detail or "").strip().lower() in RETRYABLE_BAD_REQUEST_DETAILS
    )


@dataclass(frozen=True)
class ChatResult:
    """一次对话补全的结果。"""

    text: str
    model: str = ""
    # 输入 / 输出 token。**`None` = 上游没报这个字段**，与「报了 0」是两件事 ——
    # 拿 0 代替 None，界面上就会出现一个确定的 `¥0.00`，而实际是「不知道花了多少」。
    # 与下面两个缓存字段同一条口径（见 `_extract_usage`）。
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str = ""
    # prompt cache 的账目。`None` = 上游没报这个字段，与「报了 0」（全部未命中）是
    # 两件事，不许用 0 代替 None —— 面板要能显示「未上报」而不是「命中率 0%」。
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # 上面两个数是从哪个字段读到的；`""` = 没读到。换网关会换字段名，留着便于排查。
    cache_source: str = ""

    @property
    def total_tokens(self) -> int | None:
        """输入 + 输出。**任何一个没上报就是 `None`** —— 拿报了的那个当总数会得出一个
        偏小却看起来完全正常的数字（与 `engine._sum_optional` 同一条口径）。"""
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens


def _extract_message_text(payload: Any) -> str:
    """从响应体里取出正文。

    兼容几种真实存在的形态：标准 `choices[0].message.content`、以及部分网关只给
    `choices[0].text`。取不到就报错，**不返回空字符串** —— 空字符串会被上层当成
    「模型什么都没说」，从而去走协议纠错回路，把一个解析问题伪装成模型问题。
    """
    if not isinstance(payload, dict):
        raise LLMResponseError("响应体不是 JSON 对象")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError("响应体里没有 choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMResponseError("choices[0] 不是对象")

    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        # 有些网关把 content 拆成分片数组。
        if isinstance(content, list):
            parts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("text")
            ]
            if parts:
                return "".join(parts)
    text = first.get("text")
    if isinstance(text, str):
        return text
    raise LLMResponseError("响应体里找不到可用的正文（message.content / text 均为空）")


def _extract_usage(payload: Any) -> tuple[int | None, int | None]:
    """读上游报的输入 / 输出 token。**没报就是 `None`，不是 0。**

    这里以前对「缺字段」和「确实是 0」返回同一个 0，而同一个文件里的
    `non_negative_int`（就在下面）恰恰是为区分这两件事写的。代价很具体：

    * `run.tokens_input` 落库成 0 而不是 NULL —— 而模型那一列明写「`None` = 上游没报，
      `0` = 报了且确实是 0，这个区分必须保住」；
    * `pricing.estimate_cost` 里那句 `if tokens_input is None: 上游没有返回 token 数，
      无法估算` **永远触发不到**，于是算出一个确定的 `¥0.00` 摆在界面上；
    * `analysis_budget` 的「有 N 处 token 数上游未上报，已用量是下界」也就不会出现，
      用户读到的 0 与「确实没花」分不开。

    换句话说：平台在**钱照付**的同时，把「没花钱」摆给用户看。
    """
    if not isinstance(payload, dict):
        return None, None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None
    return non_negative_int(usage.get("prompt_tokens")), non_negative_int(
        usage.get("completion_tokens")
    )


def non_negative_int(value: Any) -> int | None:
    """把上游给的数读成非负整数。**0 是合法值**，`None` 才是「没给」。

    用量字段（输入 / 输出 / 缓存读写）**全部**走这一条：把读不到的东西变成 0，
    会把「命中率 0%」与「上游根本没报缓存字段」、「这次没花钱」与「不知道花了多少」
    显示成同一个样子 —— 而这几种在面板上必须能分开。bool 要单独挡掉（`True` 不是 1）。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _extract_cache_usage(payload: Any) -> tuple[int | None, int | None, str]:
    """读「命中/写入缓存的输入 token」，返回 `(读、写、来源标记)`。

    各家网关对 prompt cache 的命名不一样，按语义最明确的顺序探测：

    1. `usage.prompt_tokens_details.cached_tokens` —— 把命中数放在嵌套对象里的那类；
    2. `usage.prompt_cache_hit_tokens` —— 直接给 hit/miss 两个平铺字段的那类。这类缓存是
       自动的、不必在请求里声明任何东西，**本项目最可能命中的就是它**；
    3. `usage.cache_read_input_tokens` —— 用 read/write 区分缓存读写的那类（部分网关做
       兼容层时会用）。

    不写具体厂商名：这个文件有一条守卫（`tests/test_ai_budget_vs_model_window.py`）禁止
    出现模型名，为的是拦住「凭模型名猜上下文窗口」那种会过期的对照表。这里只认字段名。

    **读不到返回 `(None, None, "")`，不是 0。** 来源标记是为了回答「为什么这周突然没有
    命中率了」——换个网关就可能换一套字段名。

    第 2 类还会同时报 `prompt_cache_miss_tokens`，拿它做一次交叉校验：命中 + 未命中应当
    等于 `prompt_tokens`。不等说明字段读错了对象，**一律不采信** —— 拆错的分子分母比
    没有更糟，因为它会算出一个看起来很合理但错误的命中率。
    """
    if not isinstance(payload, dict):
        return None, None, ""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None, None, ""

    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = non_negative_int(details.get("cached_tokens"))
        if cached is not None:
            return cached, non_negative_int(details.get("cache_creation_tokens")), "prompt_tokens_details.cached_tokens"

    hit = non_negative_int(usage.get("prompt_cache_hit_tokens"))
    if hit is not None:
        miss = non_negative_int(usage.get("prompt_cache_miss_tokens"))
        prompt = non_negative_int(usage.get("prompt_tokens"))
        if miss is not None and prompt is not None and hit + miss != prompt:
            return None, None, ""
        return hit, None, "prompt_cache_hit_tokens"

    read = non_negative_int(usage.get("cache_read_input_tokens"))
    if read is not None:
        write = non_negative_int(usage.get("cache_creation_input_tokens"))
        return read, write, "cache_read_input_tokens"

    return None, None, ""


class LLMClient:
    """一个项目维度上的模型客户端。

    实例持有 base_url / api_key / model，但**不持有连接**——每次调用现开现关，避免
    长连接在多线程 Flask 下被跨请求复用。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        model: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        prompt_cache_mode: str = DEFAULT_PROMPT_CACHE_MODE,
        prompt_cache_format: str = DEFAULT_PROMPT_CACHE_FORMAT,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self._api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.timeout_seconds = max(1, int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS))
        # 注入 sleep 让重试测试不必真的等待。
        self._sleep = sleep
        # 缓存标记的两个开关。归一化放在构造时做一次：读配置的路径有三四条
        # （探测 / 正式分析 / 测试连接），每条都归一化一遍必然有人漏。
        self.prompt_cache_mode = normalize_cache_mode(prompt_cache_mode)
        self.prompt_cache_format = normalize_cache_format(prompt_cache_format)

    # -- 内部 ---------------------------------------------------------------

    def _raise_for_status(self, response: "requests.Response", *, purpose: str) -> None:
        """把非 2xx 的响应转成异常。

        **3xx 也算失败**：请求一律 `allow_redirects=False`（密钥不能跟着 302 跑到
        另一台主机上去），所以 3xx 会原样回到这里。若不显式处理，`status_code < 400`
        会把它判成成功，接着 `response.json()` 在登录页的 HTML 上抛一个含义不明的
        解析错误 —— 用户看到的是「响应不是 OpenAI 形态」，而真实原因是端点把他重定向了。
        """
        if 200 <= response.status_code < 300:
            return

        detail = ""
        try:
            detail = response.text[:500]
        except requests.exceptions.RequestException:
            detail = ""

        if 300 <= response.status_code < 400:
            location = str(response.headers.get("Location") or "").strip()
            raise LLMConfigError(
                f"{purpose}收到重定向（HTTP {response.status_code} → {redact_secret(location, self._api_key)}）。"
                "出于安全考虑不自动跟随（避免密钥被转发到其它主机），请把地址改成最终端点"
            )

        message = (
            f"{purpose}失败（HTTP {response.status_code}）："
            f"{redact_secret(detail, self._api_key)}"
        )
        if _should_retry_status(response.status_code, detail):
            raise LLMTransportError(message)
        # 401/403 是配置问题，重试无意义，必须让用户看到「密钥不对」而不是「网络错误」。
        raise LLMConfigError(message)

    def _request(
        self,
        method: str,
        url: str,
        *,
        purpose: str,
        json_body: dict | None = None,
        stream: bool = False,
    ) -> "requests.Response":
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = None
            try:
                response = requests.request(
                    method,
                    url,
                    headers=_build_headers(self._api_key),
                    json=json_body,
                    timeout=self.timeout_seconds,
                    stream=stream,
                    allow_redirects=False,
                )
            except LLM_TRANSPORT_ERRORS as exc:
                last_error = exc
            except requests.exceptions.RequestException as exc:  # 兜住 requests 的其它异常
                last_error = exc
            else:
                if 200 <= response.status_code < 300:
                    return response
                try:
                    # 非 2xx（含 3xx）统一走这里转成异常；见 _raise_for_status。
                    self._raise_for_status(response, purpose=purpose)
                except LLMTransportError as exc:
                    last_error = exc
                except LLMConfigError:
                    response.close()
                    raise  # 配置问题不重试
                finally:
                    if response.status_code >= 300:
                        response.close()

                delay = _retry_after_seconds(response)
                if delay is None:
                    delay = _backoff_seconds(attempt)
                if attempt < MAX_ATTEMPTS:
                    self._sleep(delay)
                continue

            if attempt < MAX_ATTEMPTS:
                self._sleep(_backoff_seconds(attempt))

        raise LLMTransportError(
            f"{purpose}失败，已重试 {MAX_ATTEMPTS} 次：{redact_secret(str(last_error), self._api_key)}"
        )

    # -- 对外 ---------------------------------------------------------------

    def _models_entries(self) -> list:
        """请求 `/v1/models` 并返回 `data` 数组。`list_models` 与 `model_contexts` 共用。"""
        response = self._request(
            "GET", models_url(self.base_url), purpose="获取模型列表"
        )
        try:
            # 限制读取量：`content` 会一次性读进内存，上游异常时可能非常大。
            raw = response.content[:MODELS_RESPONSE_MAX_BYTES]
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except LLM_RESPONSE_ERRORS as exc:
            raise LLMResponseError(
                f"模型列表响应无法解析：{redact_secret(str(exc), self._api_key)}"
            ) from exc
        finally:
            response.close()

        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise LLMResponseError("模型列表响应不是 OpenAI 形态（缺少 data 数组）")
        return entries

    @staticmethod
    def _entry_identifier(entry: Any) -> str:
        """一条模型记录的 id。端点的写法不止一种，这里统一取。"""
        if isinstance(entry, dict):
            identifier = entry.get("id") or entry.get("name")
        else:
            identifier = entry
        return str(identifier or "").strip()

    def list_models(self) -> tuple[str, ...]:
        """取可用模型 id 列表。

        端点不支持时抛 `LLMConfigError` —— 调用方据此提示「请手动填写模型名」，
        **不要**把它当成硬失败去阻断配置保存。
        """
        models: list[str] = []
        for entry in self._models_entries():
            text = self._entry_identifier(entry)
            if text and text not in models:
                models.append(text)

        if not models:
            raise LLMResponseError("模型列表为空")
        return tuple(sorted(models)[:MAX_MODELS])

    def model_contexts(self) -> dict[str, int]:
        """取「模型 id → 上下文窗口（token 数）」。

        用途只有一个：判断项目配的字符预算会不会**明显**超出模型窗口（见
        `budget.clamp_to_model_window`）。所以这里刻意**什么都不猜**：

        * 端点没声明窗口 → 返回空 dict，调用方按「未知」处理，不压缩任何东西；
        * 不维护「模型名 → 窗口」对照表。那是猜出来的数字，模型一迭代就过期，
          而按过期的窗口压预算会静默地砍掉分析质量；
        * 值落在明显不合理的范围外（< 4,000 或 > 1 亿 token）时当成没声明 ——
          把 `max_tokens`、`n_ctx` 之类的字段误读成窗口是最容易犯的错。
        """
        contexts: dict[str, int] = {}
        for entry in self._models_entries():
            if not isinstance(entry, dict):
                continue
            text = self._entry_identifier(entry)
            if not text or text in contexts:
                continue
            for key in CONTEXT_WINDOW_KEYS:
                tokens = _positive_int(entry.get(key))
                if tokens is not None and _MIN_PLAUSIBLE_CONTEXT <= tokens <= _MAX_PLAUSIBLE_CONTEXT:
                    contexts[text] = tokens
                    break
        return contexts

    def _cache_marker(self) -> Mapping[str, str] | None:
        """这次请求要带的缓存标记；`None` = 不带。

        三种情况都会走到 `None`：模式是 `off`、端点没声明约定（`auto` 的默认）、
        以及**这个端点在本进程里拒过标记**（见 `complete` 的兜底）。
        """
        if cache_marker_rejection_reason(self.base_url, self.model) is not None:
            return None
        return resolve_cache_marker(self.prompt_cache_mode, self.prompt_cache_format)

    def _request_body(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None,
        marker: Mapping[str, str] | None,
        stream: bool = False,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """请求体。**消息一律过一遍断点处理**：内部标记绝不能漏进 JSON。

        `marker is None` 时发出去的东西与缓存功能上线前逐字节相同（见
        `prompt_cache.strip_cache_breakpoints`）—— 「默认关掉」这条路径必须是真的关掉。
        `max_tokens` 同理：**不配就不出现在请求体里**（`None` 不是「发一个 0」）。

        `max_tokens` 的用途只有一个：把「单次输出上限」这件事从网关的隐性默认值变成项目
        可配的显式值。它**不改变分析逻辑**，也不与 `finish_reason == "length"` 那条纠正
        路径冲突 —— 后者本来就要处理「撞上上限」这件事（见 `engine` 里 `TRUNCATED_OUTPUT_HINT`
        那一支），配了它只是让撞上与否由我们说了算。
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": apply_cache_breakpoints(messages, marker),
        }
        if temperature is not None:
            body["temperature"] = temperature
        if stream:
            body["stream"] = True
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return body

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """一次非流式补全。**不因缓存标记失败**，见模块 docstring。"""
        marker = self._cache_marker()
        if marker is None:
            return self._complete_once(
                messages, temperature=temperature, marker=None, max_tokens=max_tokens
            )

        try:
            return self._complete_once(
                messages, temperature=temperature, marker=marker, max_tokens=max_tokens
            )
        except LLMError as exc:
            # 安全阀。未知字段被 4xx 拒绝是最常见的失败形态，而它的后果是**整个分析
            # 跑不起来** —— 用一个省钱的优化换掉一次分析，这笔账怎么算都是亏的。
            #
            # 只对 `LLMError` 兜底（本模块自己的异常树：配置 / 传输 / 响应），
            # 不碰 KeyboardInterrupt 这类必须继续向上传播的东西。
            reason = redact_secret(f"{type(exc).__name__}: {exc}", self._api_key)
            try:
                result = self._complete_once(
                    messages, temperature=temperature, marker=None, max_tokens=max_tokens
                )
            except LLMError:
                # 去掉标记**仍然**失败 → 与标记无关（密钥、网络、网关故障）。
                # 这时候不能把端点拉黑：拉黑等于把这个功能永久关掉，而它其实没问题。
                # 抛出去的是「不带标记那一次」的错误 —— 那正是我们本来会发的请求。
                raise
            mark_cache_marker_rejected(self.base_url, self.model, reason)
            log_print(
                f"⚠️ AI 分析：{self.base_url} 不接受提示词缓存标记（{reason}），"
                "已自动去掉标记重试成功，本次分析照常；本进程后续请求不再带标记。"
                "若该端点确实支持，请核对项目配置里的 prompt_cache_mode / prompt_cache_format。",
                "AI",
                force=True,
            )
            return result

    def _complete_once(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None,
        marker: Mapping[str, str] | None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """发一次请求并把响应解析成 `ChatResult`。带不带标记由 `marker` 决定。"""
        if not self.model:
            raise LLMConfigError("未配置模型名")
        if not messages:
            raise LLMConfigError("消息为空")

        body = self._request_body(
            messages, temperature=temperature, marker=marker, max_tokens=max_tokens
        )

        response = self._request(
            "POST",
            chat_completions_url(self.base_url),
            purpose="模型调用",
            json_body=body,
        )
        try:
            payload = response.json()
        except LLM_RESPONSE_ERRORS as exc:
            raise LLMResponseError(
                f"模型响应不是合法 JSON：{redact_secret(str(exc), self._api_key)}"
            ) from exc
        finally:
            response.close()

        prompt_tokens, completion_tokens = _extract_usage(payload)
        cache_read, cache_write, cache_source = _extract_cache_usage(payload)
        finish_reason = ""
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish_reason = str(choices[0].get("finish_reason") or "")

        return ChatResult(
            text=_extract_message_text(payload),
            model=str(payload.get("model") or self.model) if isinstance(payload, dict) else self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cache_source=cache_source,
        )

    def stream(
        self, messages: list[dict[str, str]], *, temperature: float | None = None
    ) -> Iterator[str]:
        """流式补全，逐段产出正文增量。

        用 `iter_lines` 而不是自己按字节切：SSE 的一帧以空行结束，`iter_lines` 已经
        帮我们分好行，剩下的只是按 `data:` 前缀取值。

        **这条路径不读 `usage`**（它只产出正文增量，返回类型就是 `Iterator[str]`），
        所以用量会全部丢掉。目前没有生产调用者 —— 分析走的是非流式的 `complete()`，
        界面上的「流式」是平台自己往前端推的进度流，不是这里。将来若要切到流式，必须
        先给这个方法一个能带出用量的返回形态，否则面板会静默变成空的。

        **这条路径也不挂缓存标记**（`marker=None`）：它没有生产调用者，为一个跑不到的
        分支加一层「带了标记失败要重试」的复杂度不划算；但消息仍然要过一遍断点处理，
        否则引擎在消息上留的内部标记会漏进 JSON 体。
        """
        if not self.model:
            raise LLMConfigError("未配置模型名")
        if not messages:
            raise LLMConfigError("消息为空")

        body = self._request_body(messages, temperature=temperature, marker=None, stream=True)

        response = self._request(
            "POST",
            chat_completions_url(self.base_url),
            purpose="模型流式调用",
            json_body=body,
            stream=True,
        )
        try:
            yield from self._iter_sse_content(response, self._api_key)
        finally:
            response.close()

    @staticmethod
    def _iter_sse_content(response: "requests.Response", api_key: str) -> Iterator[str]:
        for raw_line in response.iter_lines(decode_unicode=True):
            line = (raw_line or "").strip()
            if not line or line.startswith(":"):
                continue  # SSE 的心跳/注释帧
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                return
            try:
                payload = json.loads(data)
            except LLM_RESPONSE_ERRORS:
                continue  # 单帧坏了不中断整个流，后面的内容仍然有效
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices:
                continue
            delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield content


def resolve_default_timeout_seconds() -> int:
    """从环境变量读默认超时，非法值回落到常量。"""
    raw = str(os.environ.get("AI_REQUEST_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
