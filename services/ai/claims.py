# -*- coding: utf-8 -*-
"""原子断言：**一条结论由哪几件可以分别核实的事实组成**，以及逐条的裁决结果。

## 为什么需要这一层（P0-01，2026-09-24）

实测 run 58（job 27）的 F3：标题写「取档失败路径改为**断言中断进程**」，而对账轮在自己的
理由里逐字写着「`assert(false)` 是中断整个进程还是仅中断本次登录请求，**未能核实**」——
整条仍被标成 `critical` / `confirmed`。同一轮的 F2 写「未见旧数据迁移」，复核只重读了
已有依据、扫的范围是「本批 120 个文件」（窗口有 1343 个），也裁成 `confirmed`。

根因不是复核不诚实，是**粒度**：一条结论是一句**复合断言**，而裁决只能落在整条上，
「这句里有一半没核实」在那套结构里无处安放。所以：

1. 结论自己带 `claims[]`（每条只含一个可核实的事实 + 它的类型 + 取材层）；
2. 复核轮**逐条**回答（证实了没有 / 反证成不成立 / 查过什么范围 / 是独立取证还是复读）；
3. **任一断言没被证实 ⇒ 整条不得 `confirmed`**，标题也只能由已证实的部分构成；
4. 「只重看了已有依据」叫**原证据复读**，不许显示成「反证不成立（独立核过）」。

## 判据全在平台这一侧

模型只回答三件事（`verified` / `unverified` / `refuted` + 查过的范围 + 取证方式）。
`unreadable` 与「范围不足」这两个状态**由平台算**：

* 依据地址**全都不可定位**（`ref_shapes.is_locatable_ref` 一条都不认）⇒ 读不到，
  不是「已证实」；
* `negative_scope`（「没有迁移」「未见引用」）说自己证实了，但**说不出查了哪儿**、
  或者没有明确声明「它声称的那片范围我都查过了」⇒ 不算证实，措辞降为
  「已检查范围内未发现」。**两样缺一不可**：只打个勾说「都查过了」不算声明，
  否则范围外推只要写一个 `true` 就能洗白。

## 措辞只在这一处生成

每一条断言对外的那句话（`ClaimReview.display`）在这里算好，三个渲染点
（报告裁决节 / 异常面板 / 导出文档）都印它 —— 各写一份必然说不到一起。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, NamedTuple, Sequence

from services.ai.budget import truncate_text
from services.ai.ref_shapes import is_locatable_ref

# --------------------------------------------------------------------------
# 断言（模型产出）
# --------------------------------------------------------------------------

#: 一条断言的**事实类型**。三种的「证实」判据不同（见模块 docstring）：
#:
#: * `fact` —— 正向事实（「调用改成了 `assert(x)`」）：读到原文即可证实；
#: * `negative_scope` —— **否定性范围声明**（「没有迁移」「未见引用」）：它声称的是一片
#:   范围里的「不存在」，所以证实它要求**检索范围覆盖声明范围** —— 只扫了本批 120 个
#:   文件就说「整个项目没有」，那不是证实，是范围外推（run 58 的 F2 正是这样被裁成
#:   confirmed 的）；
#: * `inference` —— 推断（后果、因果、影响）：由别处的事实推出来的，只有拿到支撑这条
#:   推理链的机制证据才算证实。
CLAIM_KINDS = ("fact", "negative_scope", "inference")

#: 断言的取材层，与提示词里的三层材料标注同源（`change_set._material_legend`）：
#: 本轮输入 / 窗口内更早的提交 / 项目背景。**只有第一层能支撑「本次改动了什么」**，
#: 所以这一栏要跟着断言一起存下来。
CLAIM_SOURCE_LAYERS = ("current_delta", "window_history", "project_background")


@dataclass(frozen=True)
class Claim:
    """一条**原子**断言：一句话，一个可独立核实的事实。"""

    claim_id: str
    kind: str
    statement: str
    source_layer: str = ""
    evidence_refs: tuple[str, ...] = ()


class ClaimDrop(NamedTuple):
    """解析一条断言时的记账（交给调用方包成 `protocol.DroppedItem`）。

    本模块不认识 `DroppedItem`（它是 `protocol` 的东西，而 `protocol` 要导入这里），
    所以只回一个三元组 —— 跨模块的记账结构留在**有那个概念的那一层**组装。
    """

    index: int
    reason: str
    detail: str


# `claims[]` 里那几个键**各接受哪些写法**。模型爱把 `statement` 写成 `text`/`claim`、
# 把 `claim_id` 写成 `id` —— 认这些变体是**规范写法**而不是宽容：这一栏是平台随后逐条
# 裁决、逐条渲染的锚点，因为一个近义词就把整条断言丢掉，代价远大于收益。
_CLAIM_KEY_ALIASES = {
    "claim_id": ("claim_id", "id", "claim", "key"),
    "kind": ("kind", "type", "claim_type"),
    "statement": ("statement", "text", "content", "summary", "assertion"),
    "source_layer": ("source_layer", "layer", "source", "material"),
    "evidence_refs": ("evidence_refs", "evidence", "refs", "basis"),
}


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _as_evidence(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, list):
        return tuple(
            item for item in (_as_str(entry) for entry in value) if item
        )
    return ()


def _claim_field(entry: Mapping[str, Any], name: str) -> Any:
    for key in _CLAIM_KEY_ALIASES[name]:
        if key in entry:
            return entry.get(key)
    return None


def parse_claims(value: Any, index: int = 0) -> tuple[tuple[Claim, ...], tuple[ClaimDrop, ...]]:
    """把一条异常里的 `claims[]` 读成 `Claim` 元组（附记账）。

    ## 三条口径

    1. **编号由平台定**：模型给了就用（原样大写、去重），没给就按次序补 `C1`/`C2`…
       平台随后要用这些编号去问复核轮「这一条证实了吗」，所以编号**必须**是确定的、
       且与任务书里发出去的那一份逐字一致。
    2. **缺 `statement` 的丢掉并记账**：没有正文的断言无从裁决，留着只会让「关键断言
       已核实」这句话失去意义。丢的是**这一条断言**，不是那条结论。
    3. **`kind` 认不出来时按 `fact` 处理并记账**。这里有个取舍：`negative_scope` 的
       证实判据比 `fact` 严（要求检索范围覆盖声明范围），把一条否定性声明误判成 `fact`
       会放过一次范围外推。但反过来默认成 `negative_scope` 会让**所有**没写 `kind` 的
       断言都要求全范围检索，那等于把复核轮变成一个必然报「范围不足」的机器。
       取 `fact` + 记账：账面上看得见「这条没标类型」，而提示词里 `kind` 是必填。
    """
    if value is None:
        return (), ()
    if isinstance(value, str):
        raw: list[Any] = [value] if value.strip() else []
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        return (), ()

    kept: list[Claim] = []
    dropped: list[ClaimDrop] = []
    seen: set[str] = set()
    for position, item in enumerate(raw):
        # 裸写一个字符串也算一条断言（模型把一对一的清单写成字符串是常见偏差）。
        entry = {"statement": item} if isinstance(item, str) else item
        if not isinstance(entry, dict):
            dropped.append(ClaimDrop(index, "claim 不是对象", str(position)))
            continue
        statement = _as_str(_claim_field(entry, "statement"))
        if not statement:
            dropped.append(ClaimDrop(index, "claim 缺少正文", str(position)))
            continue
        raw_id = _as_str(_claim_field(entry, "claim_id")).upper()
        claim_id = raw_id or f"C{len(kept) + 1}"
        if claim_id in seen:
            # 重复编号会让「这条断言证实了吗」指向两条不同的正文。后出现的那条改个号，
            # 而不是丢掉 —— 断言本身是有效的。
            claim_id = f"{claim_id}-{len(kept) + 1}"
        seen.add(claim_id)
        kind = _as_str(_claim_field(entry, "kind")).strip().lower()
        if kind not in CLAIM_KINDS:
            # **只有模型真的写了 `kind` 才记账**：一个字都没写时按 `fact` 处理是正常的
            # 兜底（提示词里它是必填，但缺一栏不该在 trace 里刷出一堆假账）。
            if _claim_field(entry, "kind") is not None:
                dropped.append(
                    ClaimDrop(index, "claim 的 kind 不在清单内（按正向事实处理）", kind or "空")
                )
            kind = "fact"
        layer = _as_str(_claim_field(entry, "source_layer")).strip().lower()
        if layer and layer not in CLAIM_SOURCE_LAYERS:
            layer = ""
        kept.append(
            Claim(
                claim_id=claim_id,
                kind=kind,
                statement=statement,
                source_layer=layer,
                evidence_refs=_as_evidence(_claim_field(entry, "evidence_refs")),
            )
        )
    return tuple(kept), tuple(dropped)


# --------------------------------------------------------------------------
# 裁决结果（平台产出）
# --------------------------------------------------------------------------

#: 一条断言在裁决之后的状态。**由平台写入，不由模型自填**（模型只回答「证实 / 未证实 /
#: 反证成立」，`unreadable` 是平台那两条判据给的）。
CLAIM_VERIFIED = "verified"
CLAIM_UNVERIFIED = "unverified"
CLAIM_REFUTED = "refuted"
CLAIM_UNREADABLE = "unreadable"
CLAIM_STATUSES = (CLAIM_VERIFIED, CLAIM_UNVERIFIED, CLAIM_REFUTED, CLAIM_UNREADABLE)
CLAIM_STATUS_LABELS = {
    CLAIM_VERIFIED: "已证实",
    CLAIM_UNVERIFIED: "待核查",
    CLAIM_REFUTED: "反证成立",
    CLAIM_UNREADABLE: "证据读不到",
}
#: 复核轮**可以**自己回答的三个（`unreadable` 只由平台判：要么它明确说读不到，要么这条
#: 断言的依据地址全都不可定位）。给模型的值少一个，就少一个可以乱填的格子。
CLAIM_REPLY_STATUSES = (CLAIM_VERIFIED, CLAIM_UNVERIFIED, CLAIM_REFUTED)
_CLAIM_STATUS_ALIASES = {
    "confirm": CLAIM_VERIFIED,
    "confirmed": CLAIM_VERIFIED,
    "已证实": CLAIM_VERIFIED,
    "证实": CLAIM_VERIFIED,
    "成立": CLAIM_VERIFIED,
    "unconfirmed": CLAIM_UNVERIFIED,
    "pending": CLAIM_UNVERIFIED,
    "待核查": CLAIM_UNVERIFIED,
    "未证实": CLAIM_UNVERIFIED,
    "未核实": CLAIM_UNVERIFIED,
    "refute": CLAIM_REFUTED,
    "refuted": CLAIM_REFUTED,
    "反证成立": CLAIM_REFUTED,
    "已证伪": CLAIM_REFUTED,
    "unreadable": CLAIM_UNREADABLE,
    "读不到": CLAIM_UNREADABLE,
}

#: 这一轮复核**是怎么**得到结论的。与 `status` 分开记：`status` 说「证实了没有」，
#: `basis` 说「凭什么」——「重看了一遍已有的依据、没找到反证」与「自己去搜了一遍、
#: 没找到反证」不是一回事，任务书上写着要的是后者，报告里必须分得清。
VERIFY_BASIS_INDEPENDENT = "independent"
VERIFY_BASIS_REPLAY = "replay"
VERIFY_BASIS_NONE = ""
VERIFY_BASIS_LABELS = {
    VERIFY_BASIS_INDEPENDENT: "独立取证",
    VERIFY_BASIS_REPLAY: "原证据复读（未经独立反证）",
    VERIFY_BASIS_NONE: "未取证",
}

#: 会**独立取证**的那些取数类型：只有它们能带来「这次之前没有的东西」。
#: `evidence`（按地址取回已有原文）与 `read_reference`（读 skill 文档）都不算 ——
#: run 58 的复核轮 6 次索取**全部**是 `evidence`，一次独立取证都没有，而报告里那三条的
#: 措辞是「反证不成立（维持原结论）」。
INDEPENDENT_REQUEST_TYPES = (
    "file_diff",
    "file_content",
    "commit_detail",
    "find_references",
)

#: 「查过什么」那一栏的上限。它在报告里是**一行**里的一个分句，写成长段落会把那一节顶成
#: 散文 —— 而这一节的用途是让人一眼扫出「哪几条还没被证实」。
_SCOPE_MAX_CHARS = 240


@dataclass(frozen=True)
class ClaimVerdict:
    """复核轮对**一条断言**的回答（`verdicts[].claims[]` 里的一项）。"""

    claim_id: str
    status: str = ""
    basis: str = ""
    #: 为了证实它**查过什么**（仓库 / 版本 / 路径或符号 / 覆盖范围）。否定性范围声明必须
    #: 带上它 —— 「整个项目没有迁移」这种话，读者要能看出这个「没有」是在多大范围里成立的。
    checked_scope: str = ""
    #: 复核轮是否明确声明「范围之内都查过了」。**只有声明过、且 `checked_scope` 说得清查了
    #: 哪儿，否定性声明才可能算已证实**（见 `claim_review_of`）。
    scope_complete: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ClaimReview:
    """一行里**一条断言**的裁决结果。三个渲染点（裁决节 / 异常面板 / 导出）都读它。"""

    claim: Claim
    status: str = CLAIM_UNVERIFIED
    basis: str = VERIFY_BASIS_NONE
    checked_scope: str = ""
    reason: str = ""
    #: 否定性范围声明**范围不足**：状态仍是「待核查」，但措辞要写成「已检查范围内未发现」
    #: —— 它说的不是「这条断言错了」，而是「在查过的范围里没有」。
    narrowed: bool = False

    @property
    def status_label(self) -> str:
        """这一条断言的**读者向状态词**。

        `narrowed`（否定性范围声明、而复核查过的范围不够）不是第五种 `status` —— 库里仍
        记 `unverified`，但对外那一格必须写「已检查范围内未发现」：写「待核查」会让人以为
        复核什么都没查，写「已证实」则是把「我没查到」说成了「不存在」。这个词曾经由
        `display` 自己带着前缀表达，而现在三个渲染点都要把状态词印在前面，两处各带一份
        就成了「待核查 —— 已检查范围内未发现：…」（同一行两个状态词）。
        """
        if self.narrowed and self.status == CLAIM_UNVERIFIED:
            return "已检查范围内未发现"
        return CLAIM_STATUS_LABELS.get(self.status, CLAIM_STATUS_LABELS[CLAIM_UNVERIFIED])

    @property
    def basis_label(self) -> str:
        return VERIFY_BASIS_LABELS.get(self.basis, VERIFY_BASIS_LABELS[VERIFY_BASIS_NONE])

    @property
    def heading(self) -> str:
        """一条断言**对外那一行**（状态词 + 断言正文），不含编号与查过范围。

        ## 为什么要有它（2026-09-24，run 63）

        三个渲染点（报告明细节 / 异常面板 / 导出）一直是各自拼
        `status_label + " —— " + display`，而 `display` 对非 `verified` 的状态**本来就带
        状态前缀** —— 印出来是「证据读不到 —— 证据读不到：次数记账在批次交付之前执行…」。
        口径归一到这一处：**状态词只说一次**，`heading` 就是那一行，三个渲染点照印。

        与 `display` 的分工：`display` 是**标题安全**的那一句（`compose_title` 要用它当
        标题，所以 `verified` 不许带前缀、未证实的必须带）—— 它不适合当行首标签；
        `heading` 是行首标签的形态。两份都由同一组属性算，不存在口径分叉。
        """
        return f"{self.status_label}：{self.claim.statement}"

    @property
    def display(self) -> str:
        """平台改写后的断言正文，**标题安全**那一份（`compose_title` 读它）。

        四态的措辞各不相同，因为它们的可信度完全不同：`待核查：…` 与
        `已检查范围内未发现：…` 都**不许**读成「已经确认过了」。
        不带前缀的那一态只有 `verified` —— 已证实的断言当标题就是它本身。
        """
        if self.status == CLAIM_VERIFIED:
            return self.claim.statement
        return self.heading

    def as_dict(self) -> dict:
        return {
            "claim_id": self.claim.claim_id,
            "kind": self.claim.kind,
            "statement": self.claim.statement,
            "source_layer": self.claim.source_layer,
            "evidence_refs": list(self.claim.evidence_refs),
            "status": self.status,
            "status_label": self.status_label,
            "basis": self.basis,
            "basis_label": self.basis_label,
            "checked_scope": self.checked_scope,
            "reason": self.reason,
            "narrowed": bool(self.narrowed),
            "display": self.display,
            # 渲染点印的那一行（`display` 是标题安全那一份，两者不可互替）。
            "heading": self.heading,
        }


@dataclass(frozen=True)
class ClaimOutcome:
    """一条结论的**全部断言**裁决之后的合成结果（`verdict._apply_claims` 用它建行）。"""

    reviews: tuple[ClaimReview, ...] = ()
    #: 没被证实的那些（待核查 / 读不到 / 反证成立）。非空 ⇒ **整条不得 `confirmed`**。
    blocked: tuple[ClaimReview, ...] = ()
    basis: str = VERIFY_BASIS_NONE
    #: 平台重排后的标题（只由已证实的断言构成；有未证实的就写明还有几条待核查）。
    title: str = ""
    #: 平台说明（`blocked` 非空时给 reader 的一句「哪几条没被证实」）。
    note: str = ""


# --------------------------------------------------------------------------
# 读复核轮的回答
# --------------------------------------------------------------------------


def parse_claim_verdicts(entry: Mapping[str, Any]) -> tuple[ClaimVerdict, ...]:
    """读出这一条 finding 的**逐条断言**回答（`claims[]`）。

    键名认几种常见写法（`claim_id`/`id`、`status`/`verdict`、`scope`/`checked_scope`）：
    这一栏缺失的后果是那条断言按「未证实」处理，而**未证实会挡住整条结论的 confirmed**
    —— 因为一个近义词让整条结论降档，那是平台的解析在制造假问题。

    编号**原样大写**（与 `Claim.claim_id` 的写法对齐）；平台发出的编号就是它，认不出来的
    编号在应用那一步自然对不上，不做额外校验（`claim_review_of` 按编号匹配）。
    """
    raw = entry.get("claims")
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[ClaimVerdict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        claim_id = str(
            item.get("claim_id") or item.get("id") or item.get("claim") or ""
        ).strip().upper()
        if not claim_id:
            continue
        out.append(
            ClaimVerdict(
                claim_id=claim_id,
                status=_normalize_claim_status(item.get("status") or item.get("verdict")),
                basis=_normalize_basis(item.get("basis") or item.get("evidence_kind")),
                checked_scope=scope_text(
                    item.get("checked_scope") or item.get("scope") or item.get("searched")
                ),
                scope_complete=_as_bool(
                    item.get("scope_complete")
                    if "scope_complete" in item
                    else item.get("complete")
                ),
                reason=_as_text(item.get("reason")),
            )
        )
    return tuple(out)


def _normalize_claim_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in CLAIM_REPLY_STATUSES or text == CLAIM_UNREADABLE:
        return text
    return _CLAIM_STATUS_ALIASES.get(text, "")


def _normalize_basis(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in (VERIFY_BASIS_INDEPENDENT, VERIFY_BASIS_REPLAY):
        return text
    # 中文/近义词：复核轮偶尔会写「独立」「复读」。
    if text in ("独立", "独立取证", "新检索", "新取证"):
        return VERIFY_BASIS_INDEPENDENT
    if text in ("复读", "原证据复读", "一致性检查", "重看"):
        return VERIFY_BASIS_REPLAY
    return VERIFY_BASIS_NONE


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in ("true", "yes", "1", "是", "完整", "全部", "覆盖")


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item).strip() for item in value if str(item or "").strip())
    return str(value or "").strip()


#: 「查过什么」那一栏被渲染成中文时用的键名与量词。**结构化与自由文本都收**：
#: 提示词里给的是结构化形状（平台据此渲染出一致的措辞），但模型写成一句话也不该丢。
_SCOPE_KEYS = (
    ("repositories", "仓库"),
    ("repository", "仓库"),
    ("commits", "版本"),
    ("commit", "版本"),
    ("paths", "路径"),
    ("path", "路径"),
    ("symbols", "符号"),
    ("symbol", "符号"),
)


def scope_text(value: Any) -> str:
    """把复核轮报的「查过什么」渲染成一句中文。

    **为什么由平台渲染而不是让模型随便写**：这一段在三个地方显示（裁决节、异常面板、
    导出文档），各写一遍必然说不到一起。结构化形状（对象）与一句话（字符串）都收 ——
    前者是提示词里要求的形状，后者是最常见的偏差。
    """
    if isinstance(value, str):
        return truncate_text(value, _SCOPE_MAX_CHARS)[0].strip()
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for key, label in _SCOPE_KEYS:
        if key not in value:
            continue
        raw = value.get(key)
        if isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw if str(x or "").strip()]
        else:
            text = str(raw or "").strip()
            items = [text] if text else []
        if not items:
            continue
        # 同名键（`repository` 与 `repositories`）只渲染一次。
        if any(part.startswith(f"{label} ") for part in parts):
            continue
        shown = "、".join(items[:4])
        if len(items) > 4:
            shown += f" 等 {len(items)} 个"
        parts.append(f"{label} {shown}")
    total, checked = value.get("files_total"), value.get("files_checked")
    if total is not None or checked is not None:
        parts.append(f"文件 {_as_count(checked)}/{_as_count(total)}")
    return truncate_text("；".join(parts), _SCOPE_MAX_CHARS)[0].strip()


def _as_count(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "?"


# --------------------------------------------------------------------------
# 平台的两条判据 + 合成
# --------------------------------------------------------------------------


def claim_review_of(
    claim: Claim, answer: ClaimVerdict | None, default_basis: str
) -> ClaimReview:
    """一条断言的裁决结果。**两条平台判据在模型回答之上**，各自都能单独把状态压下去。

    判据 ①：**依据全都不可定位 ⇒ 读不到**。一条断言说自己有依据、而那几个地址一个都不
    成形（`is_locatable_ref`），那它不是「已证实」，是「没读到能核的东西」—— 与「引用的
    文件被截断过」用的是同一把尺子（口径 ③）。

    判据 ②：**否定性范围声明必须说清「查过哪儿」且声明覆盖完整**。这是 run 58 的 F2
    那条的直接对策：它写「未见旧数据迁移」，而复核只重读了已有依据、范围是「本批 120 个
    文件」（窗口有 1343 个）。**两样缺一不可**：只写 `scope_complete: true` 而说不出
    查了哪儿，不算声明（否则模型只要打个勾就能把范围外推洗成已证实）。
    """
    if answer is None:
        return ClaimReview(
            claim=claim, status=CLAIM_UNVERIFIED, basis=VERIFY_BASIS_NONE
        )
    status = answer.status or CLAIM_UNVERIFIED
    basis = answer.basis or default_basis
    scope = answer.checked_scope
    # 判据 ①
    if status == CLAIM_VERIFIED and claim.evidence_refs:
        if not any(is_locatable_ref(ref) for ref in claim.evidence_refs):
            status = CLAIM_UNREADABLE
    # 判据 ②
    narrowed = False
    if claim.kind == "negative_scope":
        if status == CLAIM_VERIFIED and not (answer.scope_complete and scope):
            status = CLAIM_UNVERIFIED
        if status != CLAIM_VERIFIED and scope:
            # 查了一片范围、但那不是它声称的那一片 —— 措辞要说的是「查过的范围里没有」，
            # 不是「这条断言被否掉了」。
            narrowed = True
    return ClaimReview(
        claim=claim,
        status=status,
        basis=basis,
        checked_scope=scope,
        reason=answer.reason,
        narrowed=narrowed,
    )


def compose_title(origin_title: str, reviews: Sequence[ClaimReview]) -> str:
    """把标题换成**平台能站得住的那一份**。

    规则：所有断言都证实 ⇒ 原标题照旧（它本来就是模型对这条结论的概括）。有断言没证实 ⇒
    **只拿已证实的断言当标题**，并写明还有几条待核查 —— 于是「中断进程」这种没被证实的
    肯定断言不会出现在标题上（run 58 的 F3 正是这么被读成结论的）。

    用词全部来自**模型自己写的断言正文**（`ClaimReview.display`），平台不另造一句话：
    这一层换的是「拿哪几句当标题」，不是替模型重写结论。
    """
    if not reviews:
        return origin_title
    pending = [review for review in reviews if review.status != CLAIM_VERIFIED]
    if not pending:
        return origin_title
    verified = [review for review in reviews if review.status == CLAIM_VERIFIED]
    if verified:
        return "；".join(review.display for review in verified) + (
            f"（另有 {len(pending)} 条断言待核查）"
        )
    return "；".join(review.display for review in pending)


def claim_outcome(
    claims: Sequence[Claim],
    answers: Sequence[ClaimVerdict],
    *,
    origin_title: str,
    verify_independent: bool = False,
) -> ClaimOutcome:
    """把「这条结论的断言」与「复核的回答」合成一份结果（纯函数，确定性）。

    `verify_independent` 由调用方按**对账轮实际执行过的取数类型**判定
    （`subagent.verify_did_independent_work`），不采信模型自述。默认 `False` = 按复读记
    —— 这是**保守**的那一侧：平台没看到新检索时，不该把「重看了一遍已有依据」说成
    独立反证核验。
    """
    default_basis = (
        VERIFY_BASIS_INDEPENDENT if verify_independent else VERIFY_BASIS_REPLAY
    )
    reviews = tuple(
        claim_review_of(claim, _answer_for(answers, claim.claim_id), default_basis)
        for claim in claims
    )
    blocked = tuple(review for review in reviews if review.status != CLAIM_VERIFIED)
    return ClaimOutcome(
        reviews=reviews,
        blocked=blocked,
        basis=default_basis,
        title=compose_title(origin_title, reviews),
        note=_blocked_note(reviews, blocked),
    )


def _blocked_note(
    reviews: Sequence[ClaimReview], blocked: Sequence[ClaimReview]
) -> str:
    """平台说明：哪几条断言没被证实、以及它带来的后果（`verdict` 那一侧的动作）。

    没有未证实的断言时返回空串 —— 那种情况下平台**不该**在报告里多说一句话。

    ## 只点名编号，不重抄断言正文（2026-09-24，run 63）

    从前这里是「`C1`（证据读不到：次数记账在批次交付之前执行…）、`C2`（…）」—— 而同一行
    下面那行「逐条断言」把每一条的正文与状态各印了一遍，`verdict_render._row_line` 的头部又把
    这件事说了第三遍。一句话在一行里出现两次是排版问题，出现三次就成了没人读的账。
    现在这里只给**编号**（读的人顺着编号去下面那行看逐条状态），说清后果与出处。
    三处（报告 / 面板 / 导出）读的都是同一份 `ClaimReview`，措辞不会分叉。
    """
    if not blocked:
        return ""
    named = "、".join(f"`{review.claim.claim_id}`" for review in blocked[:3])
    if len(blocked) > 3:
        named += f" 等 {len(blocked)} 条"
    head = (
        f"复核逐条核过这条结论的 {len(reviews)} 条断言，其中 {len(blocked)} 条**没有被证实**："
        f"{named}"
    )
    if any(review.status == CLAIM_REFUTED for review in blocked):
        head += "（含**反证成立**的断言，平台不替人决定整条撤不撤）"
    return (
        head + "。含未证实断言的结论**不能算「反证不成立」**，平台按「证据不足」处理"
        "（等级与置信度降到哪一档，写在上面那一行的开头）；"
        "已证实的那部分仍然成立，逐条状态见下面那行"
    )


def _answer_for(
    answers: Sequence[ClaimVerdict], claim_id: str
) -> ClaimVerdict | None:
    return next((item for item in answers if item.claim_id == claim_id), None)


def claim_lines(reviews: Sequence[ClaimReview]) -> str:
    """逐条断言那几行（**一条断言一行**）。没有断言时返回空串。

    ## 一条一行，不是挤成一段（2026-09-24，run 63）

    原先它们是**用「；」串起来的一行**，而这一行在报告里是 400+ 字 —— 真机实测渲染出来
    是**一整段没有停顿的文字**（本仓库的渲染器把缩进丢掉，那一行不成列表）。三五个字段
    挤在一起，读者只能跳过它，而它恰恰是「这条结论凭什么算核过了」的唯一答案。

    现在每条断言自成一行（`- ` 开头，与上面那一条结论同级 —— 渲染器不认缩进，这是它
    支持的最深一层）：第一条给「逐条断言」这个标签，其余各行接着说。

    **顺序固定为「先待核查、后已证实」**：这一段的用途是让人一眼看到「哪几条还没立住」，
    而按模型给的次序排会把没证实的那条埋在中间。同组内保持模型给的次序（确定性）。
    """
    if not reviews:
        return ""
    pending = [review for review in reviews if review.status != CLAIM_VERIFIED]
    verified = [review for review in reviews if review.status == CLAIM_VERIFIED]
    lines: list[str] = []
    for position, review in enumerate((*pending, *verified)):
        scope = f"（查过：{review.checked_scope}）" if review.checked_scope else ""
        label = "逐条断言：" if position == 0 else ""
        # 状态词只说一次（`heading` 的定义）：从前这里是「**状态** —— 状态：正文」，
        # 而 `display` 自己就带着状态前缀 —— run 63 的报告里每一行都印了两遍。
        lines.append(
            f"- {label}`{review.claim.claim_id}` **{review.status_label}**："
            f"{review.claim.statement}{scope}"
        )
    return "\n".join(lines)


def claims_instructions() -> str:
    """提示词里「逐条断言」那一段契约（`verdict.verdict_instructions` 拼进去）。"""
    return (
        "### 逐条断言（`claims`，**每条结论都要给**）\n\n"
        "结论里那一行「它的断言」是平台发给你的编号（`C1`、`C2`…）。**对每一条分别回答**，"
        "写在同一条裁决的 `claims` 数组里：\n\n"
        "* `claim_id`：照抄平台给的编号；\n"
        "* `status`：`verified`（证实了）/ `unverified`（没证实）/ `refuted`（反证成立）；\n"
        "* `basis`：`independent`（你自己**去取了新东西**才得出的结论）/ `replay`"
        "（只是**重看了结论已有的依据**、没做新检索）。**这两个不许混**："
        "「我重看了一遍，没看出问题」写成 `independent` 会让报告把复读说成独立反证核验；\n"
        "* `checked_scope`：**为了核实它你查过什么**（仓库 / 版本 / 路径 / 符号 / 文件数）。"
        "否定性范围声明（「没有迁移」「未见引用」）**必须**给，而且要在 "
        "`scope_complete: true` 里明确说「它声称的那片范围我都查过了」—— "
        "只查了本批文件却说「整个项目没有」，平台会把这条判成**未证实**，"
        "并写成「已检查范围内未发现」；\n"
        "* `reason`：一句话说明依据或为什么核不了。\n\n"
        "**为什么非拆不可**：一条结论常常是几件互不相干的事实拼起来的。整条回答时，"
        "「其中一件没核实」无处安放，平台只能把它和别的一起算成已核实 —— 而那正是"
        "最不该出现的读法。**没核实就写没核实，这不是扣分项**：报告会把它写成"
        "「待核查」，读者据此知道还差什么。\n\n"
    )
