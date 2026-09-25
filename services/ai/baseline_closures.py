"""模型对**上一轮结论**的收口声明：这一轮它说哪几条已经修好、哪几条被推翻了。

## 它解决什么

`incremental_baseline` 有一条硬规则：**「旧结论不能因为模型没有重复输出就视为已修复」**
—— 于是「上一轮报过、这一轮结论里没有」的条目一律**继续当成在挂**。这条规则本身是对的
（静默消失是这套结构最该防的失真），但它有一个副作用：**平台没有任何通道能收到「已经
修好了」这句话**。模型在正文里写了「已修复」，那只是一段文字；下一轮做基线时，条目
照样当成在挂喂回去。

实测（2026-09-25）撞了两次：

* run 73 的模型读了新代码，在正文里写下「已修复（不再列入清单）」，而平台那一节同时
  写着「2 条需要重新确认」——同一份报告两种说法；
* **下一轮更糟**：run 74 的模型拿到的清单里那两条还在，于是报告又把它们写成
  「上次遗留，仍成立」。同一个事实在两轮报告里翻转了一次，而读者无从判断哪次是对的
  （那两条其实是上一轮就修好的）。

## 这条通道**不是**「模型说了算」

收到声明只做一件事：把这条从「在挂」移到**「本轮声明已修复 / 已被推翻」**，并且原样
保留模型给的理由。措辞一律带「声明」二字（`baseline_state` 也是 `declared_fixed` /
`declared_overturned`）—— 平台**没有独立复核过**它，这一点必须留在字面上。

它也不会静默删除任何东西：条目仍然出现在结论载荷的审计轨迹（`retracted_findings`）
与报告那一节里，只是不再算作「仍然成立的问题」。

**少写是信息不足，编证据是假事实。** 这条通道两个都不制造：它只把「模型说过什么」
如实记下来，并把「平台没核过」印在旁边。

## 2026-09-25 晚：从「可选的两档」改成「逐条枚举」

上面那一版是**可选**字段（模型想写才写），真机跑了两次**一次都没收到**（机理见
`prompt-contract-cannot-carry-optional-fields` 那条教训：模型对「已经在写的那份结构」
从不出错，对可选的新数组一次没填过）。所以现在清单里有几条就要求几条状态
（`standing` / `fixed` / `overturned`），缺了当场要求补齐 —— 与 `dimensions` 同一个手法
（它一次没漏过，因为它必填且要逐条枚举）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 模型能给出的三种状态。**`standing` 是 2026-09-25 晚加的**，它把这条通道从「只补缺的
# 那半边」变成「清单里每一条都要交代」：
#
# * `fixed` / `overturned` —— 收口（那条从「在挂」挪出去，带模型的理由）；
# * `standing` —— 仍然成立（就是模型把这条结论照原样再报一次）。
#
# 为什么要它：真机实测（run 75 / 76）模型**只写正文、不填这个字段**，两次都收不到。
# 而平台**逼得住**的字段有先例 —— `dimensions` 一次没漏过，因为它是**必填且要逐条枚举**
# 的。所以这条通道也做成枚举：清单里有几条就要有几条状态，缺了平台当场要求补（见
# `protocol.missing_baseline_statuses` 与 `engine` 的纠正那一支）。
#
# `standing` 还顺手解决另一件事：模型换个说法重报旧结论时指纹会变（实测相似度 0.643
# 配不上兜底门槛 0.72，同一条问题于是在清单里出现两份）。现在它带上原指纹，平台按精确
# 指纹认，不再靠标题相似度猜。
DECLARED_STANDING = "standing"
DECLARED_FIXED = "fixed"
DECLARED_OVERTURNED = "overturned"
DECLARED_STATUSES = (DECLARED_STANDING, DECLARED_FIXED, DECLARED_OVERTURNED)
DECLARED_STATUS_LABELS = {
    DECLARED_STANDING: "仍成立",
    DECLARED_FIXED: "声明已修复",
    DECLARED_OVERTURNED: "声明已被推翻",
}
# 写进提示词给模型看的枚举（与上面那份是同一件事，两处必须逐字一致）。
DECLARED_STATUS_ENUM = " | ".join(DECLARED_STATUSES)
# **只有收口的那两种要写理由**：`standing` 是「这条还在」，它没有新话要说 —— 要求它也写
# 一句等于让模型为每条旧结论编一段文字（正是「不要为它补写证据」要防的那件事）。
CLOSING_STATUSES = (DECLARED_FIXED, DECLARED_OVERTURNED)
# 一条声明为什么要写理由：这句话是本轮唯一能解释「凭什么说它修好了」的东西，
# 而它是**给人看的**（平台不复核）。空理由的声明等于没有声明。
REASON_MAX_CHARS = 200
# 一次声明最多多少条。上限而不是目标：一份清单几十条时，逐条声明本身没有价值，
# 而且它会挤掉真正要看的内容。
MAX_CLOSURES = 50
# 指纹的形状：`rules.anomaly_fingerprint` 的前 16 位十六进制（提示词里每条清单项
# 末尾那个 `#…`）。**判形状，不判它在不在** —— 在不在要拿本次的清单去比，那是
# `incremental_baseline` 手里的事实（它才知道上一轮报了哪几条）。
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}")


@dataclass(frozen=True)
class BaselineClosure:
    """一条收口声明。`reason` 是模型的理由原文（截断到 `REASON_MAX_CHARS`）。"""

    fingerprint: str
    status: str
    reason: str

    @property
    def label(self) -> str:
        return DECLARED_STATUS_LABELS.get(self.status, self.status)


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def coerce_closures(value: Any) -> tuple[tuple[BaselineClosure, ...], tuple[tuple[str, str], ...]]:
    """把 `baseline_updates` 读成 `(声明, 问题)`。

    第二条是 `(拿到的值, 为什么不算)` —— **由调用方记进丢弃账**，不在这里吞掉：
    这个仓库的纪律是「丢弃要有账」，而这一层不知道账记在哪（协议层记进 `dropped`）。

    `None` / 缺键 = 模型这次没声明任何收口，**不是错误**（这是可选字段）。
    """
    if value is None:
        return (), ()
    if not isinstance(value, list):
        return (), (("", "baseline_updates 必须是数组"),)

    closures: list[BaselineClosure] = []
    problems: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            problems.append((str(entry)[:80], "这一条不是一个对象"))
            continue
        fingerprint = _as_str(entry.get("fingerprint")).lower()
        status = _as_str(entry.get("status")).lower()
        reason = _as_str(entry.get("reason"))[:REASON_MAX_CHARS]
        if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
            problems.append(
                (
                    _as_str(entry.get("fingerprint"))[:80],
                    "fingerprint 必须是清单里那条末尾的 16 位十六进制",
                )
            )
            continue
        if status not in DECLARED_STATUSES:
            problems.append((f"{fingerprint} · {status}", f"status 只能是 {DECLARED_STATUS_ENUM}"))
            continue
        if status in CLOSING_STATUSES and not reason:
            problems.append((f"{fingerprint} · {status}", "没有写理由 —— 空理由的声明不算收口"))
            continue
        if fingerprint in seen:
            problems.append((fingerprint, "同一条结论声明了两次，只留第一条"))
            continue
        if len(closures) >= MAX_CLOSURES:
            problems.append((fingerprint, f"一次最多声明 {MAX_CLOSURES} 条，多余的没有收下"))
            continue
        seen.add(fingerprint)
        closures.append(BaselineClosure(fingerprint=fingerprint, status=status, reason=reason))
    return tuple(closures), tuple(problems)
