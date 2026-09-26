"""把用户配置转换成一次运行可见、可审计的预算计划。"""
from __future__ import annotations

from typing import Mapping

from services.ai.context_tools import DEFAULT_TOOL_LIMITS
# `utils.content_window.CONTENT_MAX_CHARS` 不再在这里被读：取数侧的页大小现在由
# `derive_tool_limits` 推导出来的 `file_content` 决定（见 `PROVIDER_MAX_CHARS_KEY`），
# 那个常量只剩「没有别的依据时的初值」这一个身份（`platform_provider` 用它当默认值）。

#: `file_content` 那一档「取数侧真正会给多少字」的键名。
#:
#: **这个键是必须的，不是装饰。** 正文在**取数侧**就按这个额度切好了（切在行边界上，
#: 并把「这是哪一段、整份多少字、下一页怎么要」写进抬头），所以计划里的 `file_content`
#: 必须**等于**取数侧真正交付的那一页 —— 报一个取数侧给不出的数，界面那一行就是一句谎话
#: （用户按它去理解「为什么还是只有一万字」，永远找不到答案）。
#:
#: ## 2026-09-24（工作包 D 的 P2）：它**不再**被 `CONTENT_MAX_CHARS` 夹住
#:
#: 原先这里取 `min(content, CONTENT_MAX_CHARS)` —— 因为取数侧硬夹在 11,000，用户把提示词
#: 预算调到再高也没用，而计划里那个 30,333 是**给不出来的**。现在取数侧的页大小由计划
#: 自己推导（`ContextTools` 构造时经 `apply_tool_limits` 交给 provider），超过一页的正文
#: 用 `next_cursor` **继续要**（`utils.content_window.page_lines`）。于是这个键与
#: `tool_limits["file_content"]` 是同一个数，而「隐藏截断」在结构上消失：
#: 模型看得见「这不是全部」，也拿得到剩下的。
PROVIDER_MAX_CHARS_KEY = "file_content_provider_max_chars"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def derive_tool_limits(
    *,
    prompt_char_budget: int,
    max_tool_requests: int,
    remaining_chars: int | None = None,
    remaining_requests: int | None = None,
) -> dict[str, int]:
    """按有效提示词预算推导单次取数上限，避免总预算变大但单条永远卡 11k。

    ## 两种口径：总预算（起跑前）与剩余（跑到一半）

    不给 `remaining_*` 时按**总预算**算 —— 那是起跑前的口径（计划、预估端点都用它）。
    给了就按**剩余**算：跑到第 5 轮时前面几轮花掉的额度不该再被算一遍，否则「剩余还
    很宽」的判断永远是错的（按总量算出来的单条上限在预算快耗尽时仍然那么大）。

    判据是「给没给」，不是「剩余是不是 0」：剩余真的为 0 时按 0 算出来的额度由下面的
    下限兜住 —— 静默退回总预算会让「这一轮已经没有余地了」这件事消失。

    ## `file_content` 的那一页：`file_content_provider_max_chars`

    返回的 `file_content` 就是**取数侧真正交付的一页**（它由 `ContextTools` 转交给
    provider，见 `PROVIDER_MAX_CHARS_KEY` 的说明）：切在行边界上、抬头写明「这是第几段 /
    整份多少字 / 下一页的 `lines` 怎么写」。所以这个额外的键与 `file_content` 同值 ——
    它存在的理由是让界面能明确回答「模型实际最长得看到多少字」，而不是只报一个"允许"。

    `file_diff` / `read_reference` 不走这条：它们的正文由平台自己拼，分段靠
    `windowed_view` 的段号 + 点名（同一件事的另一种坐标）。
    """
    budget = max(
        0, int(remaining_chars if remaining_chars is not None else prompt_char_budget)
    )
    requests = max(
        1, int(remaining_requests if remaining_requests is not None else max_tool_requests)
    )
    share = int((budget * 0.65) / max(4, min(requests, 12)))
    content = _clamp(share, 11_000, 32_000)
    return {
        "commit_detail": _clamp(share, 6_000, 20_000),
        "file_diff": content,
        "file_content": content,
        "read_reference": content,
        "find_references": _clamp(share, 8_000, 20_000),
        PROVIDER_MAX_CHARS_KEY: content,
    }


def build_budget_plan(
    *,
    configured_prompt_chars: int,
    effective_prompt_chars: int,
    platform_chars: int,
    max_rounds: int,
    max_tool_requests: int,
    shard_count: int = 1,
    verify: bool = False,
    tool_limits: Mapping[str, int] | None = None,
    window_note: str = "",
    window_source: str = "",
    reserved_output_chars: int = 0,
    family_pool: Mapping[str, object] | None = None,
    plan: Mapping[str, object] | None = None,
    verify_reserve: tuple[int, int] | None = None,
) -> dict:
    """返回可直接落库/下发 UI 的预算事实，不做费用预测。

    `window_source` / `reserved_output_chars` 是 E3 要的两项：

    * **窗口来源**回答「生效预算这个数是哪来的」——「端点未声明、按默认值」与
      「端点声明了窗口」是两件事，计划里不写清就会被读成「平台问到了端点」；
    * **保留输出空间**回答「水位为什么只到 60%」——那 40% 是留给模型回复与估算误差的，
      它不是一个被丢掉的名额（`budget.COMPACT_AT_RATIO` 的注释）。

    两者默认空 / 0：老调用方（还没接这两项的那些）拿到的是「未说明」，而不是一个
    编出来的默认值 —— 「不知道」与「按默认窗口算的」在界面上是两句话。

    ## `family_pool`：串行共享池的账（2026-09-23 起）

    子代理模式开了池之后，`per_role` 里的两个数字是**每片的名义额**（保底），而全家
    实际能花多少由池决定 —— 计划不带上它，界面与预估端点就会按
    `roles × 名义额` 高估整次分析的理论上限。给了 `family_pool`（键：`requests_pool` /
    `rounds_pool` / `*_nominal` / `synthesis_floor_*` / `note`）时，`job_theoretical_max`
    改按「池 + 汇总保底下限」算（汇总可越池，见 `subagent.FamilyQuota`）。老调用方不传
    时行为与从前逐字相同。

    ## `verify_reserve`：估算侧带进来的对账预留

    运行侧手里有 `plan.quota`，把预留放在 `family_pool` 里带进来；**估算侧拿不到 plan**
    （它算的是「如果按这套配置跑会怎样」），只能自己按配置算一份 —— 走这个关键字。
    两个来路算的是同一份数（同一个 `verify_reserve`），谁给就以谁为准。
    """
    shards = max(1, int(shard_count))
    family = shards > 1
    roles = shards + 1 + (1 if verify else 0) if family else 1
    limits = dict(tool_limits or DEFAULT_TOOL_LIMITS)
    configured = max(0, int(configured_prompt_chars))
    effective = max(0, int(effective_prompt_chars))
    pool = dict(family_pool or {}) or None
    if pool is not None:
        # **三层都要算进去**：分片怎么花（池）＋ 汇总的保底（可越池）＋ 对账轮的预留。
        #
        # 最后一层原先漏了，于是计划里的理论上限比实际能发出去的次数**小**：实测 run 58
        # 的面板写 252，而那一次真的发出了 256 次。原因是对账轮跑在「池内剩余 + 它那一份
        # 预留」上，而编排层**不为它记账**（`run_family` 只对分片与汇总调 `quota.spend`），
        # 于是它的那几次发生在池的账之外 —— 上限公式必须单独把它们加上，否则这个数永远
        # 比实际小，而「理论上限」正是用户拿来判断「这次到底花了多少」的那把尺子。
        reserve_requests = max(0, int(pool.get("verify_requests") or 0))
        reserve_rounds = max(0, int(pool.get("verify_rounds") or 0))
        if verify_reserve is not None:
            # 显式给的那个压过池里带的（两者本来同源，估算侧那一份是**配置级**的估算）。
            reserve_requests = max(0, int(verify_reserve[0] or 0))
            reserve_rounds = max(0, int(verify_reserve[1] or 0))
        theoretical_max = {
            "rounds": max(0, int(pool.get("rounds_pool") or 0))
            + max(0, int(pool.get("synthesis_floor_rounds") or 0))
            + reserve_rounds,
            "tool_requests": max(0, int(pool.get("requests_pool") or 0))
            + max(0, int(pool.get("synthesis_floor_requests") or 0))
            + reserve_requests,
        }
    else:
        theoretical_max = {
            "rounds": roles * max(0, int(max_rounds)),
            "tool_requests": roles * max(0, int(max_tool_requests)),
        }
    return {
        "prompt_chars": {
            "configured": configured,
            "effective": effective,
            "platform_overhead": max(0, int(platform_chars)),
            "clamped": effective < configured,
            "reason": str(window_note or ""),
            "window_source": str(window_source or ""),
        },
        "per_role": {
            "max_rounds": max(0, int(max_rounds)),
            "max_tool_requests": max(0, int(max_tool_requests)),
        },
        "roles": {
            "count": roles,
            "shards": shards if family else 0,
            "synthesis": family,
            "verify": bool(verify and family),
        },
        "job_theoretical_max": theoretical_max,
        "family_pool": {
            "requests_pool": max(0, int(pool.get("requests_pool") or 0)),
            "rounds_pool": max(0, int(pool.get("rounds_pool") or 0)),
            "requests_nominal": max(0, int(pool.get("requests_nominal") or 0)),
            "rounds_nominal": max(0, int(pool.get("rounds_nominal") or 0)),
            "synthesis_floor_requests": max(0, int(pool.get("synthesis_floor_requests") or 0)),
            "synthesis_floor_rounds": max(0, int(pool.get("synthesis_floor_rounds") or 0)),
            # 对账轮的预留（上面那两项已经把它算进 `job_theoretical_max` 了）。
            "verify_requests": reserve_requests,
            "verify_rounds": reserve_rounds,
            "note": str(pool.get("note") or ""),
        }
        if pool is not None
        else None,
        "tool_limits": {key: max(0, int(value)) for key, value in limits.items()},
        "reserved_output": {"chars": max(0, int(reserved_output_chars))},
        # 计划（工作包 B）：模式 / 分组 / 每成员额度 / 单次分析预算 / 两个预留 /
        # 阈值与估算公式。**原样放进预算计划**，于是「这次是怎么分工的、为什么」在
        # 落库的载荷与预估端点的返回里都能读到，不需要再去别处推一遍。
        # 不给时是 `None`（老调用方逐字不变），不是一份编出来的空计划。
        "plan": dict(plan or {}) or None,
    }
