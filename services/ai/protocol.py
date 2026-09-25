"""模型输出的解析、校验与接地。

## 三层职责，失败代价逐层降低

1. **解析**（`parse_json_candidates` / `parse_payload`）：模型可能返回带思考块、
   代码围栏或前后解释文字的 JSON。这一层尽量把它救回来。
2. **协议校验**：结构性错误（status 不认识、`final` 却没有报告正文）→ 抛
   `ProtocolError`，由编排层把它作为「纠正提示」重问一轮。这类错误重问一次通常就
   好了，且重问比硬猜安全。
3. **接地校验**（`ground_payload`）：**逐条**校验，不合法的**丢弃并记账**，不让整单
   失败。区别在于：协议错误是「我没看懂」，接地失败是「这一条不可信」——后者丢掉
   那一条就够了，为它作废其余 9 条正确结论代价太大。

    模型编造 commit、或者引用一个该提交根本没改过的文件，都是常见的。不校验的话，
    前端会渲染出点不开的链接，跟进的人查不到东西。

## 但有一条**永远不许**被丢弃：`category` 不是「可不可信」的问题

「这条结论可不可信」可以逐条判，因为判据（commit 与路径是否真实）是平台手里的事实；
「这条结论属于哪个维度」不是 —— 维度清单是**项目可声明**的
（`LoadedSkills.dimensions`，见 `skill_contract.render_dimension_section`），这一层没有
它自己的来源。若按平台出厂值判一次，就正好成了「校验按 A、提示词按 B」：声明了自己清单的
项目里，模型按提示词写下的真实发现会被静默丢掉，而报告看起来完全正常（只是少了一条）。

所以这里的口径是：**category 只决定它归到哪一组，不决定它留不留下**。落不进清单的条目
按原样保留（原始 category 一个字不改），由知道清单的那一层归到「未归类」并单独列出来
（`unclassified_anomalies`、`subagent.build_unclassified_section`、
`report_document.dimension_label`）。少一条发现是静默的，多一个「未归类」是响亮的。

清单可以由调用方**传进来**（`parse_payload(dimension_ids=…)`，引擎把
`LoadedSkills.dimensions` 的 id 传下来），但传进来也只用于**记账**（说明这一条为什么
会显示成「未归类」），不参与任何一条发现的取舍 —— 传与不传，`anomalies` 都是同一份。

## 「像不像一份报告」这个判定为什么重要

多轮预算耗尽时，模型可能已经输出了完整的 Markdown 报告、只是没按 JSON 协议收尾。
这时直接判失败会让用户白等一场。所以最后一个兜底是：如果回答里有足够多的约定章节
标题，就把它当报告降级返回（异常清单为空）。这个判定复用 `REPORT_SECTIONS`——**格式
约束同时当健康检查用**，是笔很划算的买卖。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from services.ai.baseline_closures import BaselineClosure, coerce_closures
from services.ai.claims import (  # noqa: F401 —— Claim 在本模块重新导出（既有导入点按 protocol 走）
    CLAIM_KINDS,
    CLAIM_SOURCE_LAYERS,
    Claim,
    _as_evidence,
    _as_str,
    parse_claims,
)
# 中间轮的字段收窄搬到 `mid_round` 了（`protocol.py` 顶着 2000 行的 ERROR 闸门）。
# `MID_ROUND_FIELDS` 一并回导：它是「中间轮认哪四个字段」的唯一出处，读代码的人会按名字找它。
from services.ai.mid_round import MID_ROUND_FIELDS, mid_round_drops  # noqa: F401
from services.ai.reference_search import MIN_QUERY_WEIGHT, normalize_query, query_weight
from services.ai.scope import AnalysisScope, normalize_path
from services.ai.skill_contract import (
    CONFIDENCES,
    DIMENSION_IDS,
    REPORT_SECTIONS,
    REQUEST_TYPES,
    SEVERITIES,
)

STATUS_NEED_MORE_CONTEXT = "need_more_context"
STATUS_FINAL = "final"
STATUSES = (STATUS_NEED_MORE_CONTEXT, STATUS_FINAL)


# `reason` 的硬长度上限（字符）。
#
# 这个字段原先**没有任何长度约束**，而长 `reason` 是**平台自己要来的**：SKILL.md 与
# 提示词里有五处要求模型「把判断写进 `reason`」。收紧的办法是**换字段**（判断写成
# `reason_code`，`reason` 只留一句可选补充）而不是把模型按提示词写下的那句话砍掉 ——
# 砍掉的正是它被要求说的东西。真的超了上限时截断并记账（不静默截断：静默截断读起来
# 像「模型只写了这么点」）。
REASON_MAX_CHARS = 200
# `reason_code` 是一个**短标识**（形如 `need_config_pair`），不该有段落那么长。
REASON_CODE_MAX_CHARS = 60

# 「按 evidence_id 取回原件」的请求类型。
#
# 它存在的理由：E5 的共享证据仓给每条正文发了一个稳定地址，而中间协议收窄之后，汇总与
# 对账的任务书只给地址与体量（`@evidence_id=abc123（原文 11,000 字，按需索取）`）。
# **那个承诺必须真的能兑现**，否则它就是平台自己写下的一句假话。
EVIDENCE_REQUEST_TYPE = "evidence"
# 允许的请求类型**就是**契约里那一份（现在含 `evidence`）。
#
# 2026-09-22 收口：这里曾经写成 `(*REQUEST_TYPES, EVIDENCE_REQUEST_TYPE)` —— 因为当时
# `evidence` 只写在 SKILL.md 的正文里，没进 `skill_contract.REQUEST_TYPES`（提取器逐字
# 比对，文档加一行 JSON 校验就红）。那个绕法让**协议比契约多一个成员**，而契约校验正是
# 防「文档写了、服务端不认」的那道闸 —— 绕过去的代价是那道闸对这个类型失效。
# 现在两处是同一个元组，那个失效面没有了；**别再拼第二份**。
ALLOWED_REQUEST_TYPES = REQUEST_TYPES

# 工具类型、severity、confidence 的允许集合定义在 `skill_contract` 里 —— 它们是
# 契约的一部分（必须与 SKILL.md 里给模型看的枚举逐字一致），由校验器自动守住。
REQUEST_TYPES_NEEDING_COMMIT = ("commit_detail", "file_diff", "file_content")
REQUEST_TYPES_NEEDING_PATH = ("file_diff", "file_content")

# 认「窗口」(`lines`) 的工具：返回长内容的四个。单位各不相同（见 `ContextRequest.lines`），
# 但语法与规范化是同一条 —— 于是「哪一段」这件事在协议层只需要一处解析。
_WINDOW_TYPES = ("file_content", "file_diff", "read_reference", "commit_detail")

# 报告健康检查需要命中多少个章节标题才认为「这像一份报告」。
REPORT_HEALTH_MIN_SECTIONS = 2

# `evidence_id` 的规范形状：sha256 的前 20 位十六进制（见 `evidence_store.blob_id_of`）。
# **判形状而不是判它在不在**：查得到与否是执行层的事（那里才知道有没有这一份），
# 而形状是协议自己的约定 —— 不像它的按畸形请求丢掉，别拿去执行。
_EVIDENCE_ID_RE = re.compile(r"[0-9a-f]{20}")

# 去掉模型可能带上的推理块。
_THINK_BLOCK_RE = re.compile(r"<think\b.*?</think\s*>", re.S | re.I)
# 代码围栏（含语言标注）。
_FENCED_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\s*(.*?)```", re.S)
_FENCE_MARKER_RE = re.compile(r"```[a-zA-Z0-9_+-]*")


class ProtocolError(ValueError):
    """模型的返回不符合协议。会被转成「纠正提示」重问一轮。"""


@dataclass(frozen=True)
class ContextRequest:
    type: str
    commit: str = ""
    path: str = ""
    name: str = ""
    # 要**哪一段**。空表示「你替我挑一段」：
    # `file_content` 会给改动附近那一段（见 `ai/platform_provider._render_text_content`），
    # 其余三个从第一段开始、按字符上限装到装不下为止。
    #
    # 为什么要有这个字段：代码文件的正文动辄几千行，整份给既超预算又没用（从中间截断的
    # 正文等于没有上下文）。让它点名要哪一段，比让平台猜它想看哪里准得多，也省得多。
    #
    # **单位随工具变**（四个认它的工具各自在返回文本的抬头里写明），这正是这个字段
    # 不再只叫「行窗口」的原因：`file_content` 是行号 `"1180-1260"`、`file_diff` 是第几个
    # 改动块、`read_reference` 是第几小节、`commit_detail` 是第几个改动文件。
    lines: str = ""
    # `find_references` 的搜索词（一个标识符：字段名、协议名、函数名、配置 ID）。
    # 只有这一个类型用它；其余类型的请求里它一律被清空（见 `sanitize_requests`）。
    query: str = ""
    # **哪一个仓库**（`Repository.id`，形如 `"2"`）。**加在最后**：`ContextRequest` 在测试
    # 与脚本里有按位置构造的写法（`tests/test_ai_live_thinking_snapshot.py`、
    # `scripts/shot_ai_drawer.py`），插在中间会静默错位。
    #
    # 只在**背景核查**那条路上有意义：同一条相对路径（`config/item.xlsx`）在本项目的两个
    # 仓库里都存在时，平台不替模型猜是哪一个，而是回一份可操作的拒绝，请它带上这个字段。
    # 指向的是**仓库**而不是提交：那条路读的是服务端固定的冻结 tip，模型给不给 commit
    # 都不影响读到的版本。
    #
    # **它不进 `describe()` 的标签**（那份标签是被逐字断言的地址格式，见 `context_tools`
    # 的模块说明），但要进缓存键 —— 它决定返回的是哪个仓库的正文。
    repository_id: str = ""

    def describe(self) -> str:
        if self.lines:
            # 标签是回查内容的地址，所以「要了哪一段」必须写进去（同一个文件的两段
            # 若共用一行标签，正文里就会出现两个标题一样的 `###` 节）。
            head = (
                f"read_reference {self.name}"
                if self.type == "read_reference"
                else f"{self.type} {self.commit[:12]} {self.path}".strip()
            )
            return f"{head} lines={self.lines}"
        if self.type == "read_reference":
            return f"read_reference {self.name}"
        if self.type == EVIDENCE_REQUEST_TYPE:
            # 地址就是这条请求的全部内容（`name` 装的是 `evidence_id`）。
            return f"evidence {self.name}"
        if self.type == "commit_detail":
            return f"commit_detail {self.commit[:12]}"
        if self.type == "find_references":
            scope = normalize_path(self.path)
            return f"find_references {self.query}" + (f"（范围 {scope}）" if scope else "")
        return f"{self.type} {self.commit[:12]} {self.path}"


@dataclass(frozen=True)
class Anomaly:
    title: str
    category: str
    severity: str
    confidence: str
    evidence: tuple[str, ...]
    commit: str = ""
    file_path: str = ""
    impact: str = ""
    suggestion: str = ""
    # 这条结论**来源于哪几条分片候选**（`family_ledger.Candidate.id`，形如 `S1-3`）。
    #
    # 汇总那一次的任务书要求每条结论把来源编号原样带回来（一对多、多对一都允许）。
    # 平台据此对账：**编号对不上才是真缺口**。在这之前，平台是从标题、文件路径、
    # 证据文本里**反推**血缘的（`family_ledger` 那三手），而在真机上它产生的全是假缺口
    # ——run 20 的 `S3-3` 与最终结论 `F5` 讨论的是同一个问题（同名协议拆在两个文件里），
    # 三条启发式一条都没对上，于是被报成「找不到去向」，而那 4 条假缺口又是
    # `subagent_gap` 降级的唯一触发源。显式血缘把这一整类误判整个删掉。
    #
    # 单代理路径永远是空的：没有分片，也就没有候选编号可言。
    source_candidate_ids: tuple[str, ...] = ()
    # 这条结论**由哪些可以逐项核实的原子断言组成**（P0-01，见 `Claim`）。
    #
    # 空元组 = 模型没给（旧形态 / 没按协议写）。**平台不因此丢弃这条结论**，但也
    # **不再把它当作可以「已核实」的整体** —— 无法逐项裁决的复合断言正是
    # 「未核实却 confirmed」的成因（run 58 的 F3：标题断言「断言中断进程」，
    # 复核轮自己在理由里承认这一点没核实，整条却仍是 confirmed）。
    claims: tuple["Claim", ...] = ()





# 候选编号两侧可能附带的装饰符（模型爱写 `[S1-2]`、`S1-2、`、`（S1-2）`）。剥掉它们
# 是**规范写法**，不是宽容：编号是平台发出去的固定字面量，两侧的括号与标点从来不是它
# 的一部分。剥的代价为零，不剥的代价是「模型写对了格式、平台却说没交回血缘」。
_CANDIDATE_ID_STRIP = "[]【】{}()（）<>「」 \t\r\n\"'`，,、。;；:："


def _as_candidate_ids(value: Any) -> tuple[str, ...]:
    """把 `source_candidate_ids` 读成一组编号。**去空、去重、保序。**

    接受数组，也接受**裸写的一个字符串**（模型把一对一的条目直接写成
    `"source_candidate_ids": "S1-2"` 是最常见的一种偏差）。认不出来的形状给空元组：
    这一栏缺失的后果是「这条候选没有血缘」，而那已经被 `family_ledger` 单独处理
    （一条都没交回时只如实说一句，不逐条报假缺口）。
    """
    if isinstance(value, str):
        raw: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        return ()
    out: list[str] = []
    for item in raw:
        if isinstance(item, (dict, list, tuple)):
            continue
        text = str(item or "").strip().strip(_CANDIDATE_ID_STRIP).strip().upper()
        if text and text not in out:
            out.append(text)
    return tuple(out)


@dataclass(frozen=True)
class DimensionReview:
    id: str
    hit: bool
    note: str = ""


CANDIDATE_DISPOSITION_STATUSES = frozenset({"adopted", "rejected", "deferred"})


@dataclass(frozen=True)
class CandidateDisposition:
    """汇总代理对一条分片候选的机器可读处置。"""

    candidate_id: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class DroppedItem:
    """条目级记账：**被丢弃**的条目及原因，以及**被归到「未归类」**的条目。

    `kind` 的取值为 `anomaly` / `dimension` / `request` / `subagent` / `deferred` /
    `unclassified` / `mid_round_field` / `reason` / `reason_code` / `shared_cache` /
    `evidence` / `claim`。`unclassified` 那一条与其他几种**不是一回事**：那条发现**没有被丢掉**（它在
    `payload.anomalies` 里，报告里也列着），这里只是把「为什么它的维度显示成未归类」
    记下来。它借用这一个结构是因为 trace 是平台里唯一一条按条目把记录带到面板上的
    通道（`result_payload` 的 `dropped` → `trace_evidence.summarize_dropped`）。

    `claim` 也是**不是一回事**的那一类：丢的是**一条原子断言**（缺正文、类型认不出来），
    不是那条结论 —— 结论照旧留下，只是它因此不能算「已核实」（见 `verdict`）。

    `deferred` 与 `subagent` 同样要分开：前者是汇总**主动**把候选标成待复核（有理由、
    报告里另有一节），后者才是「汇总一声不响地丢了某条发现」的真缺口 —— `subagent.py`
    的降级判定只认后者。

    `mid_round_field` 与 `reason` / `reason_code` 同样不是「结论被丢了」：它们记的是
    **格式约束**（E4）。中间轮不接受的那四个字段确实没有读者，但「它本来写了什么」
    必须留痕 —— 否则读的人只看到「这一轮只说了这么点」。
    """

    kind: str
    index: int
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class AnalysisPayload:
    status: str
    reason: str = ""
    # 这一轮**为什么**是这样：一个短标识，不是一段叙述。
    #
    # `reason` 与它是**两个字段**而不是一个带长度上限的字段：`reason` 原先被提示词要求
    # 承载「你的分诊判断」这类内容（SKILL.md 与提示词里有五处这么写），那本来就是一段话；
    # 而平台真正需要机器处理的只有「是哪一类」，那是一个短标识。把两者合成一个字段、
    # 再给它加长度上限，被砍掉的恰好是平台让模型说的那句话。
    reason_code: str = ""
    requests: tuple[ContextRequest, ...] = ()
    report_markdown: str = ""
    anomalies: tuple[Anomaly, ...] = ()
    dimensions: tuple[DimensionReview, ...] = ()
    candidate_dispositions: tuple[CandidateDisposition, ...] = ()
    # 模型对**上一轮结论**的收口声明（哪几条修好了 / 被推翻了）。语义与边界写在
    # `baseline_closures` 的模块 docstring 里 —— 一句话：它只把「模型说过什么」如实
    # 记下来，**平台不据此认定问题真的没了**。
    baseline_closures: tuple[BaselineClosure, ...] = ()
    dropped: tuple[DroppedItem, ...] = field(default_factory=tuple)

    @property
    def is_final(self) -> bool:
        return self.status == STATUS_FINAL


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def parse_json_candidates(text: str) -> list[Any]:
    """把模型回答里可能的 JSON 逐个试出来，返回成功解析的结果列表。

    候选按「可信度从高到低」生成：原文最先，其次是剥掉推理块/围栏的形态，最后是从
    首尾大括号切出来的片段。逐个 `json.loads`，能解析成功的都返回 —— 调用方取第一个
    符合协议的。
    """
    raw = str(text or "")
    if not raw.strip():
        return []

    candidates: list[str] = []

    def _add(value: str) -> None:
        value = value.strip()
        if value and value not in candidates:
            candidates.append(value)

    _add(raw)
    _add(_THINK_BLOCK_RE.sub("", raw))

    for block in _FENCED_BLOCK_RE.findall(raw):
        _add(block)
    _add(_FENCE_MARKER_RE.sub("", raw))

    # 首尾大括号之间切片：模型在 JSON 前后写了说明文字时靠这一步。
    for candidate in list(candidates):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            _add(candidate[start : end + 1])

    parsed: list[Any] = []
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        parsed.append(value)
    return parsed


def _coerce_requests(value: Any) -> tuple[ContextRequest, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProtocolError("requests 必须是数组")
    requests: list[ContextRequest] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        requests.append(
            ContextRequest(
                type=_as_str(entry.get("type")),
                commit=_as_str(entry.get("commit")),
                path=_as_str(entry.get("path")),
                name=_as_str(entry.get("name")),
                lines=_as_str(entry.get("lines")),
                # `find_references` 的搜索词。**漏掉这一个字段的后果不是「差一点」**：
                # `sanitize_requests` 会按「搜索词太短（至少 3 个字）」把每一条
                # `find_references` 都丢掉，于是这个工具端到端**从来没有执行过**，
                # 而丢掉的理由还把责任推给了模型（它明明按 SKILL.md 写了 query）。
                query=_as_str(entry.get("query")),
            )
        )
    return tuple(requests)


def _coerce_anomalies(
    value: Any, dimension_ids: Iterable[str] = DIMENSION_IDS
) -> tuple[tuple[Anomaly, ...], tuple[DroppedItem, ...]]:
    """把 `anomalies` 逐条读成 `Anomaly`。**category 不在清单内的条目不丢。**

    ## 为什么 category 不再被丢弃

    这里原先写的是 `if category not in DIMENSION_IDS: 丢弃`。于是模型给出一个不在
    清单里的 category（换一个项目、换一套维度时这是必然会发生的）时，那一条异常**从
    报告里彻底消失**：报告看起来完全正常，只是少了一条，而没有任何人会去数。异常清单
    里没有它、报告正文里没有它，只剩 trace 里一条谁都看不到的记录 —— 这正是这套结构
    最该避免的失真形态。

    现在的口径：**category 只决定它归到哪一组，不决定它留不留下。** 落不进「本次生效的
    维度清单」的条目按原样保留（原始 category 一个字不改），由知道清单的那一层归到
    「未归类」并单独列出来（`unclassified_anomalies` / `subagent` 的报告段 /
    `report_document` 的维度列）。

    ## `dimension_ids` 只用来**记账**，不参与任何取舍

    清单由调用方给（引擎手里那份 `LoadedSkills.dimensions`）。它在这里只有一个用途：
    给「category 不在清单内」的条目留一条 `unclassified` 记录 —— 否则界面/报告上明明
    写着「未归类（performance）」，而没有任何地方说明**为什么**它没被认领。

    判据仍然是「结构问题才丢」：缺标题、severity/confidence 不在两档内、证据为空。
    那些不是「归错组」，而是这一条根本立不住（没有证据的断言无法跟进），且都记账。

    **记账刻意排在全部结构校验之后**：那两条记录写的是「未丢弃，已归入未归类」，
    而一条随后因为缺证据被丢掉的条目会让这句话变成假的（trace 里同时出现「已归入
    未归类」与「evidence 为空」）。
    """
    if value is None:
        return (), ()
    if not isinstance(value, list):
        raise ProtocolError("anomalies 必须是数组")

    allowed = {str(item).strip() for item in dimension_ids if str(item or "").strip()}
    kept: list[Anomaly] = []
    dropped: list[DroppedItem] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            dropped.append(DroppedItem("anomaly", index, "条目不是对象"))
            continue

        title = _as_str(entry.get("title"))
        if not title:
            dropped.append(DroppedItem("anomaly", index, "缺少标题"))
            continue

        # category 缺失也保留：没有归属的**发现**仍然是发现（下面的 reason 记着这件事），
        # 中文名那一格会显示成「未归类（未标注）」。丢掉它等于平台替模型做了一个
        # 「这条不重要」的判断，而平台没有这个依据。
        category = _as_str(entry.get("category"))

        severity = _as_str(entry.get("severity")).lower()
        if severity not in SEVERITIES:
            dropped.append(DroppedItem("anomaly", index, "severity 不在允许集合内", severity))
            continue

        confidence = _as_str(entry.get("confidence")).lower()
        if confidence not in CONFIDENCES:
            # 达不到门槛的判断只该写进报告正文，不该进这份给人工跟进的清单。
            dropped.append(
                DroppedItem("anomaly", index, "confidence 未达门槛", confidence)
            )
            continue

        evidence = _as_evidence(entry.get("evidence"))
        if not evidence:
            # 没有证据的断言无法跟进，也是空泛表述的主要来源。
            dropped.append(DroppedItem("anomaly", index, "evidence 为空"))
            continue

        # 原子断言（P0-01）。**解析失败不丢整条结论**：断言是「这条结论怎么被核实」的
        # 依据，缺了它这条结论仍然是一条结论（只是不能算 confirmed，见 `verdict`）。
        # 解析与判据都在 `services/ai/claims.py`（那里是它的第二个读者所在层），
        # 这里只把它的记账包成平台统一的 `DroppedItem`。
        claims, claim_drops = parse_claims(entry.get("claims"), index)
        dropped.extend(
            DroppedItem("claim", item.index, item.reason, item.detail)
            for item in claim_drops
        )

        # 这一条**留下来了**，只是没有归属（或归属不在清单内）—— 记账在这里写，
        # 上面那个「未丢弃」的说法才不会是假的。
        if not category:
            dropped.append(
                DroppedItem("unclassified", index, "缺 category（未丢弃，已归入「未归类」）", "")
            )
        elif category not in allowed:
            dropped.append(
                DroppedItem(
                    "unclassified",
                    index,
                    "category 不在本次生效的维度清单内（未丢弃，已归入「未归类」）",
                    category,
                )
            )

        kept.append(
            Anomaly(
                title=title,
                category=category,
                severity=severity,
                confidence=confidence,
                evidence=evidence,
                commit=_as_str(entry.get("commit")),
                file_path=normalize_path(_as_str(entry.get("file_path"))),
                impact=_as_str(entry.get("impact")),
                suggestion=_as_str(entry.get("suggestion")),
                # 候选血缘（AI-P0-06）：汇总按任务书把来源编号带回来，平台按它核对
                # 「这条候选有没有去向」。解析不做任何合法性判断（编号集合是
                # `family_ledger` 那边的事），只负责把形状读成一组字符串。
                source_candidate_ids=_as_candidate_ids(entry.get("source_candidate_ids")),
                claims=claims,
            )
        )
    return tuple(kept), tuple(dropped)


def unclassified_anomalies(
    anomalies: Iterable[Anomaly], dimension_ids: Iterable[str]
) -> tuple[Anomaly, ...]:
    """落在**本次生效的维度清单**之外的那些条目（要归到「未归类」里）。

    这是「不丢掉发现」的最后一步：解析层保留它们、这一层把它们点名出来，所以报告里
    一定有一处写着「这条不属于本次清单里的任何维度」。`dimension_ids` 由调用方给
    （`LoadedSkills.dimensions` 的那一份，或多成员计划里汇总那一份）—— **平台里只有
    一个地方能回答「本次清单是什么」，就是它**。

    category 为空的条目也算在内：它同样没有被认领。
    """
    allowed = set(dimension_ids)
    return tuple(item for item in anomalies if item.category not in allowed)


def _coerce_dimensions(value: Any) -> tuple[tuple[DimensionReview, ...], tuple[DroppedItem, ...]]:
    """把 `dimensions` 逐条读成 `DimensionReview`。**id 不在清单内的条目不丢。**

    与 `_coerce_anomalies` 同一条口径（那里的注释解释了为什么这一层不判集合）：模型
    按**项目声明的清单**写了 `performance`，而这里若按平台出厂值判，那条留痕会消失 ——
    提示词要求它写的维度，在「逐一交代」表里反而看不到，报告读起来完全正常。
    """
    if value is None:
        return (), ()
    if not isinstance(value, list):
        raise ProtocolError("dimensions 必须是数组")

    kept: list[DimensionReview] = []
    dropped: list[DroppedItem] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            dropped.append(DroppedItem("dimension", index, "条目不是对象"))
            continue
        identifier = _as_str(entry.get("id"))
        if not identifier:
            # 空 id 是结构问题：既分不了组、也没法显示（这一条本来就没说它是什么维度）。
            dropped.append(DroppedItem("dimension", index, "缺少 id"))
            continue
        kept.append(
            DimensionReview(
                id=identifier,
                hit=bool(entry.get("hit")),
                note=_as_str(entry.get("note")),
            )
        )
    return tuple(kept), tuple(dropped)


def _coerce_candidate_dispositions(
    value: Any,
) -> tuple[tuple[CandidateDisposition, ...], tuple[DroppedItem, ...]]:
    """读取候选处置表；坏行逐条记账，不作废其余最终报告。"""
    if value is None:
        return (), ()
    if not isinstance(value, list):
        raise ProtocolError("candidate_dispositions 必须是数组")
    kept: list[CandidateDisposition] = []
    dropped: list[DroppedItem] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            dropped.append(DroppedItem("candidate_disposition", index, "条目不是对象"))
            continue
        candidate_ids = _as_candidate_ids(entry.get("candidate_id"))
        candidate_id = candidate_ids[0] if candidate_ids else ""
        status = _as_str(entry.get("status")).lower()
        reason = _as_str(entry.get("reason"))
        if not candidate_id:
            dropped.append(DroppedItem("candidate_disposition", index, "缺 candidate_id"))
            continue
        if candidate_id in seen:
            dropped.append(
                DroppedItem("candidate_disposition", index, "candidate_id 重复", candidate_id)
            )
            continue
        if status not in CANDIDATE_DISPOSITION_STATUSES:
            dropped.append(
                DroppedItem("candidate_disposition", index, "status 不在允许集合内", status)
            )
            continue
        if status != "adopted" and not reason:
            dropped.append(
                DroppedItem(
                    "candidate_disposition", index, f"{status} 必须说明 reason", candidate_id
                )
            )
            continue
        seen.add(candidate_id)
        kept.append(CandidateDisposition(candidate_id, status, reason))
    return tuple(kept), tuple(dropped)


def _clip_field(value: str, limit: int, kind: str, label: str) -> tuple[str, tuple[DroppedItem, ...]]:
    """按硬上限截断一个**模型给的自由文本字段**，超了就截断并记一条账。

    **不许静默截断**：截断之后的文本进的是 trace 与提示词，读的人看到的是一段自己收尾
    的话 —— 「模型只写了这么点」与「平台砍掉了后半段」在界面上长得一模一样，而后者
    意味着我们**替模型改写了它的判断**。所以账要记，且账里写清原文多长。
    """
    text = str(value or "")
    if len(text) <= limit:
        return text, ()
    return text[:limit], (
        DroppedItem(
            kind,
            0,
            f"{label} 超过 {limit} 字符上限（原文 {len(text)} 字符），已截断",
            # 把**被砍掉的那一段的开头**留在账里：它是「模型本来还想说什么」，而这一条
            # 账的唯一用途就是让人看得到那件事。
            _safe_repr(text[limit : limit + 80]),
        ),
    )



def _select_payload_object(parsed: Iterable[Any]) -> dict:
    """从解析结果里挑出符合协议外壳的那个对象。

    优先取带 `status` 的；没有就取第一个字典（模型可能漏了 status，交给协议校验
    去报错，报出的信息比「不是 JSON」更准确）。
    """
    fallback: dict | None = None
    for value in parsed:
        if not isinstance(value, dict):
            continue
        if _as_str(value.get("status")):
            return value
        if fallback is None:
            fallback = value
    if fallback is not None:
        return fallback
    raise ProtocolError("回答里找不到可解析的 JSON 对象")


def parse_payload(
    text: str, *, dimension_ids: Iterable[str] = DIMENSION_IDS
) -> AnalysisPayload:
    """把模型回答解析成 `AnalysisPayload`。

    结构性错误抛 `ProtocolError`（编排层据此重问），条目级问题只丢弃并记账。

    `dimension_ids` 是**本次生效的**维度清单（`LoadedSkills.dimensions` 的 id 那一份），
    由引擎传进来；不传就是平台出厂那一份。它**只影响记账**（见 `_coerce_anomalies`），
    不影响任何一条发现的去留 —— 落不进清单的条目照样按原样保留。
    """
    parsed = parse_json_candidates(text)
    if not parsed:
        preview = str(text or "").strip()[:200]
        raise ProtocolError(f"回答里没有可解析的 JSON。原文开头：{preview}")

    raw = _select_payload_object(parsed)

    status = _as_str(raw.get("status")).lower()
    if status not in STATUSES:
        raise ProtocolError(f"status 必须是 {STATUSES} 之一，实际是 {status!r}")

    requests = _coerce_requests(raw.get("requests"))
    if status == STATUS_NEED_MORE_CONTEXT and not requests:
        raise ProtocolError("status 为 need_more_context 时必须给出非空的 requests")

    reason, dropped_reason = _clip_field(
        _as_str(raw.get("reason")), REASON_MAX_CHARS, "reason", "reason"
    )
    reason_code, dropped_reason_code = _clip_field(
        _as_str(raw.get("reason_code")), REASON_CODE_MAX_CHARS, "reason_code", "reason_code"
    )

    # **中间轮只留那四个字段**（`MID_ROUND_FIELDS`）。其余四个字段在这一轮没有读者 ——
    # 与其「解析了不用」（白花输出 token，还更容易撞上单次输出上限），不如丢弃并记账。
    # 收窄**只管中间轮**：`final` 那一支必须照旧拿到全部字段，差一点就是整份结论丢掉。
    dropped_mid: tuple[DroppedItem, ...] = ()
    report_markdown = ""
    anomalies: tuple[Anomaly, ...] = ()
    dimensions: tuple[DimensionReview, ...] = ()
    dispositions: tuple[CandidateDisposition, ...] = ()
    closures: tuple[BaselineClosure, ...] = ()
    dropped_entries: tuple[DroppedItem, ...] = ()
    if status == STATUS_NEED_MORE_CONTEXT:
        dropped_mid = mid_round_drops(raw)
    else:
        report_markdown = _as_str(raw.get("report_markdown")) or _as_str(raw.get("report"))
        anomalies, dropped_anomalies = _coerce_anomalies(
            raw.get("anomalies"), dimension_ids=dimension_ids
        )
        dimensions, dropped_dimensions = _coerce_dimensions(raw.get("dimensions"))
        dispositions, dropped_dispositions = _coerce_candidate_dispositions(
            raw.get("candidate_dispositions")
        )
        closures, closure_problems = coerce_closures(raw.get("baseline_updates"))
        dropped_closures = tuple(
            DroppedItem("baseline_update", index, reason, detail)
            for index, (detail, reason) in enumerate(closure_problems)
        )
        dropped_entries = (
            dropped_anomalies
            + dropped_dimensions
            + dropped_dispositions
            + dropped_closures
        )

    if status == STATUS_FINAL:
        if not report_markdown:
            raise ProtocolError("status 为 final 时必须给出非空的 report_markdown")
        if not dimensions:
            # dimensions 是「每个维度都过了一遍」的证据（清单 = `DIMENSION_IDS`，也就是
            # SKILL.md 的「九个检查维度」）。允许为空等于允许模型只挑好说的说，这正是它
            # 要防的事。条数不写死在这里 —— 写死的那一版曾经写着「六个」，而过了一年没人
            # 发现（`tests/test_ai_dimension_count_stays_in_sync.py` 现在钉住这一条）。
            raise ProtocolError("status 为 final 时必须给出非空的 dimensions（未命中的也要写）")

    return AnalysisPayload(
        status=status,
        reason=reason,
        reason_code=reason_code,
        requests=requests,
        report_markdown=report_markdown,
        anomalies=anomalies,
        dimensions=dimensions,
        candidate_dispositions=dispositions,
        baseline_closures=closures,
        dropped=dropped_entries + dropped_mid + dropped_reason + dropped_reason_code,
    )


# --------------------------------------------------------------------------
# 接地校验
# --------------------------------------------------------------------------


def ground_payload(payload: AnalysisPayload, scope: AnalysisScope) -> AnalysisPayload:
    """逐条校验异常条目的 commit / file_path 是否真实，丢弃不合法的并记账。

    **不抛异常**：一条越权的条目不该作废其余正确的结论。丢弃的记录进 `dropped`，
    最终写进 trace，这样「为什么这次只报了 3 条」是可追溯的。
    """
    kept: list[Anomaly] = []
    dropped = list(payload.dropped)

    for index, anomaly in enumerate(payload.anomalies):
        resolved = scope.resolve_commit(anomaly.commit)
        if resolved is None:
            dropped.append(
                DroppedItem(
                    "anomaly",
                    index,
                    "commit 不属于本批次（可能是模型编造的）",
                    anomaly.commit,
                )
            )
            continue

        file_path = anomaly.file_path
        if file_path and not scope.path_allowed(resolved, file_path):
            dropped.append(
                DroppedItem(
                    "anomaly",
                    index,
                    "file_path 不在该 commit 改动过的文件里",
                    file_path,
                )
            )
            continue

        kept.append(
            Anomaly(
                title=anomaly.title,
                category=anomaly.category,
                severity=anomaly.severity,
                confidence=anomaly.confidence,
                evidence=anomaly.evidence,
                # 回写成全哈希：短前缀在前端点不开，也没法用于去重。
                commit=resolved,
                file_path=file_path,
                impact=anomaly.impact,
                suggestion=anomaly.suggestion,
                # 血缘原样带过去：这一层校验的是 commit / file_path 是否真实，
                # 与「这条结论来源于哪几条候选」无关（漏传等于把血缘静默丢掉）。
                source_candidate_ids=anomaly.source_candidate_ids,
                # 同理：断言这一层一个字都不校验，漏传等于把「这条结论由什么组成」
                # 静默丢掉 —— 而复核轮与渲染都按它工作。
                claims=anomaly.claims,
            )
        )

    return AnalysisPayload(
        status=payload.status,
        reason=payload.reason,
        requests=payload.requests,
        report_markdown=payload.report_markdown,
        anomalies=tuple(kept),
        dimensions=payload.dimensions,
        candidate_dispositions=payload.candidate_dispositions,
        dropped=tuple(dropped),
    )


# 控制字符（`\n`、`\r`、`\t` 等 0x00-0x1f，以及 DEL）。**不含空格**：空格是正常路径里
# 会出现的字符，把它算进去会让「文件名里有空格」的请求整批被拒。
_CONTROL_CHARS = frozenset(chr(code) for code in range(0x20)) | {"\x7f"}

# 回显给模型/日志的字段长度上限。字段来自模型，可以任意长 —— 不做上限的话，一条畸形的
# 超长路径会把「上一轮被拒」那句话本身撑爆（那句话要进提示词）。
_ECHO_MAX_CHARS = 200


def _has_control_chars(value) -> bool:
    """字段里是否出现控制字符（含换行）。"""
    return any(char in _CONTROL_CHARS for char in str(value or ""))


def _safe_repr(value) -> str:
    """把可能有害的字段转义成**只含可见字符**的一段文本，用于记账与回显。

    用 `repr` 而不是原值：换行会变成两个可见字符 `\\n`，于是它进了提示词也只是「路径里
    有个奇怪的转义」，而不是一行新的指令。
    """
    text = repr(str(value or ""))
    if len(text) <= _ECHO_MAX_CHARS:
        return text
    return text[: _ECHO_MAX_CHARS - 1] + "…"


def sanitize_requests(
    requests: Iterable[ContextRequest],
    scope: AnalysisScope,
    *,
    repo_paths: Optional[frozenset] = None,
) -> tuple[tuple[ContextRequest, ...], tuple[DroppedItem, ...]]:
    """工具白名单：把越权的上下文请求丢掉。

    这是「模型不能诱导服务端读任意文件」的落点。四重校验：类型在集合内、需要的字段
    齐全、commit 能解析到本批次、path 属于该 commit 改动过的文件。任一不满足就丢弃
    （不报错——模型偶尔写错一个字段不该作废整轮），并记账。

    ## `repo_paths`：冻结仓库的只读范围（工作包 D 的 P1）

    `file_content` 原先只允许本批次改动过的路径 —— 小 diff 恰好改了公共接口时，
    「调用方改了没有」既证实不了也证伪不了。给了 `repo_paths`（**本次冻结 tip 上 Git
    跟踪文件的路径集合**）之后：

    * `file_content` 的路径只要**在这个集合里**就放行，`commit` 允许为空或不是本批次的
      提交（读取走的是冻结版本，模型给的 commit 只用于描述它想看的范围）；
    * `find_references` 的 `path` 前缀只要**匹配到集合里的路径**就放行。

    **这里不是「信任模型」**：集合是服务端从冻结 tip 的对象库里列出来的，而真正的读取
    还会在取数层再判一次（`frozen_repo` 的 tracked 检查 + 凭证排除 + 路径形状判据）。
    这一层只回答「这条请求该不该被执行」。

    `repo_paths=None`（默认）时行为**逐字不变**。空集合与 `None` 是两件事：空集合意味着
    「仓库里一个跟踪文件都没有」（几乎不可能），那会让每一条仓库范围请求都被判越权 ——
    所以调用方拿不到集合时必须传 `None`。

    ## 另加一道：字段里带控制字符的请求按畸形丢掉

    请求字段（`commit` / `path` / `name` / `query` / `repository_id`）会被**原样拼进下一轮的提示词**：平台
    用它们写一句「你上一轮这些索取没有被执行：<detail>（<reason>）」（`engine._rejected_note`），
    那句话是**平台自己写的话**，不在任何数据封套里（封套见 `prompt._wrap_untrusted`）。
    一个带换行的路径因此能在提示词里伪造出一行新指令，而且不经过数据封套那条路。

    正常的请求用不到控制字符（`normalize_path` 本来就会 strip 掉首尾空白），所以判据取
    「出现即畸形」，理由用 `repr` 转义后记账 —— 不把原样的控制字符回显出去。
    """
    repo_set = None if repo_paths is None else frozenset(repo_paths)
    allowed: list[ContextRequest] = []
    dropped: list[DroppedItem] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    # **路径形状先判一次**：绝对路径 / 带 `..` 的写法在这里就拒掉，并给出可照做的理由。
    # 不先判的话，它们会落进「不在跟踪树里」那条更含糊的理由里 —— 而模型据那条理由只会
    # 反复换路径（`frozen_repo.resolve_repo_path` 是同一套判据的唯一实现，这里 import 它
    # 而不是再写一份）。
    #
    # **无条件 import**（原先只在 `repo_set is not None` 时）：`file_content` 的两条路
    # 都要这一份判据 —— 批次授权那条同样需要「这个路径形状本身合不合法」，
    # 而它正是 `sanitize_requests` 里 path 相关的唯一安全边界。
    from services.ai.frozen_repo import resolve_repo_path

    for index, request in enumerate(requests):
        request_type = str(request.type or "").strip()
        if request_type not in ALLOWED_REQUEST_TYPES:
            dropped.append(DroppedItem("request", index, "type 不在白名单内", request_type))
            continue

        unsafe = next(
            (
                value
                for value in (
                    request.commit,
                    request.path,
                    request.name,
                    request.query,
                    request.repository_id,
                )
                if _has_control_chars(value)
            ),
            "",
        )
        if unsafe:
            dropped.append(
                DroppedItem(
                    "request",
                    index,
                    "字段里含控制字符（换行等），按畸形请求丢弃",
                    _safe_repr(unsafe),
                )
            )
            continue

        if request_type == "read_reference":
            if not scope.reference_allowed(request.name):
                dropped.append(
                    DroppedItem("request", index, "文档不在可读清单里", request.name)
                )
                continue
            key = (request_type, "", "", request.name)
            if key in seen:
                continue
            seen.add(key)
            allowed.append(
                ContextRequest(type=request_type, name=request.name, lines=request.lines)
            )
            continue

        if request_type == EVIDENCE_REQUEST_TYPE:
            # **按地址取回原件**（E5）。它与其他五种有一处根本不同：它不指向仓库里的
            # 任何东西，指向的是**本次运行自己的证据仓**，所以这里既不校验 commit、
            # 也不校验 path —— 能校验的只有「这个 id 长得对不对」。
            #
            # 判形状而不是判「在不在」：在不在是执行层才知道的事（`EvidenceStore.by_id`），
            # 而这里丢掉一条形状不对的请求，理由要说得清（「这不是一个证据地址」），
            # 而不是让它跑到执行层再回一句含义不明的「取不到」。
            evidence_id = str(request.name or "").strip().lower()
            if _EVIDENCE_ID_RE.fullmatch(evidence_id) is None:
                dropped.append(
                    DroppedItem(
                        "request",
                        index,
                        "evidence 必须带一个合法地址（上下文抬头里那个 20 位十六进制的 "
                        "evidence_id），这次给的形状不对",
                        _safe_repr(request.name),
                    )
                )
                continue
            key = (request_type, "", "", evidence_id)
            if key in seen:
                continue
            seen.add(key)
            allowed.append(ContextRequest(type=request_type, name=evidence_id))
            continue

        if request_type == "find_references":
            # 这个工具**不带 commit**：它搜的是整个批次改动的文件（每份用各自最后那次
            # 提交的内容），所以它没有「某一条提交」可以校验，取而代之的是两件事 ——
            # 关键词得写得够具体（"id" 这种词会把整批都搜出来），以及可选的 `path`
            # 前缀必须真的匹配到本批次的改动文件。
            #
            # 「够不够具体」按**信息量权重**判、不按字符数：单位要跟着文字系统走，否则
            # 中文二字词（`队伍`/`匹配`）一律被误拒，而拉丁泛词（`get`）照样放行。
            query = normalize_query(request.query)
            if query_weight(query) < MIN_QUERY_WEIGHT:
                dropped.append(
                    DroppedItem(
                        "request",
                        index,
                        "搜索词太短（三个字母或两个汉字起），换个具体一点的标识符",
                        query,
                    )
                )
                continue
            prefix = normalize_path(request.path)
            if prefix and not scope.prefix_allowed(prefix) and not _repo_matches_prefix(
                repo_set, prefix
            ):
                dropped.append(
                    DroppedItem(
                        "request",
                        index,
                        "path 前缀匹配不到本批次改动过的任何文件"
                        + (
                            "、也不在本次冻结版本的跟踪文件里"
                            if repo_set is not None
                            else ""
                        ),
                        prefix,
                    )
                )
                continue
            key = (request_type, "", prefix, query)
            if key in seen:
                continue
            seen.add(key)
            allowed.append(
                ContextRequest(type=request_type, path=prefix, query=query)
            )
            continue

        if request_type in _REPO_SCOPED_TYPES:
            # `file_content` 有**两条**可通的路（本批次授权读那条提交上的版本 / 冻结仓库的
            # 背景版本），判据与顺序都在 `_content_request` 里。它必须排在下面那条
            # 「只有本批次一条路」的分支**之前** —— 那条会对 `file_content` 直接下结论。
            prepared, drop = _content_request(
                request, index, request_type, scope, repo_set, resolve_repo_path
            )
            if prepared is None:
                dropped.append(drop)
                continue
            key = (
                request_type,
                prepared.commit,
                normalize_path(prepared.path),
                prepared.lines,
                # 仓库也进这条去重键：同一条路径、同一条提交、**两个仓库**各要一次是两条
                # 请求，只去重掉一条等于把另一个仓库的正文静默吞掉。
                prepared.repository_id,
            )
            if key in seen:
                continue
            seen.add(key)
            allowed.append(prepared)
            continue

        resolved = scope.resolve_commit(request.commit)
        if resolved is None:
            dropped.append(
                DroppedItem("request", index, "commit 不属于本批次", request.commit)
            )
            continue

        if request_type in REQUEST_TYPES_NEEDING_PATH:
            if not normalize_path(request.path):
                # 没给 path、或给的 path 归一化之后是空的：这是**请求格式**的问题，
                # 不是「提交配错了」。两种理由必须分开说 —— 混成同一句会让模型跑去换提交，
                # 而它该做的是把 path 补上（下一轮照原样再要一次也不会被执行）。
                dropped.append(
                    DroppedItem(
                        "request",
                        index,
                        f"{request_type} 必须带 path（这次没给，或给了归一化后为空的路径）",
                        repr(request.path),
                    )
                )
                continue
            if not scope.path_allowed(resolved, request.path):
                # **原因里必须带上是哪条提交，能换的又是哪条。**
                #
                # 这条记录有两个读者，两边都因为「只说『不属于』」吃过亏：
                # * **模型**：它只收到「本轮没有附带任何上下文」，于是把「我把
                #   (commit, path) 配错了」写成「平台取数失败」，还写进报告的信息缺口 ——
                #   读者会去找一个不存在的平台故障。实测那一轮 28 条被拒（占索取数 23%）。
                # * **人**：`detail` 原先只记 path、不记 commit，事后根本判不出是谁配错了。
                #
                # 本批次里改过这个文件的是哪条提交，平台是知道的（`commit_of_path`，
                # `find_references` 用的也是它）—— 说出来，模型下一轮就能问对。
                suggested = scope.commit_of_path(request.path)
                if suggested:
                    reason = (
                        f"这个文件不在 commit {resolved[:12]} 的改动清单里；"
                        f"本批次里改过它的是 {suggested[:12]}，换那条提交再问"
                    )
                else:
                    reason = (
                        f"这个文件不在 commit {resolved[:12]} 的改动清单里，"
                        "也不在本批次改动过的任何文件里"
                    )
                dropped.append(
                    DroppedItem("request", index, reason, f"{request.path}（配的是 {resolved[:12]}）")
                )
                continue
            path = normalize_path(request.path)
        else:
            path = ""

        # 窗口（`lines`）：**不是只有 `file_content` 认它**。四个会返回长内容的工具都用这一套
        # 语法点名「要哪一段」，只是单位不同 —— `file_content` 是行号（`1180-1260`）、
        # `file_diff` 是改动块、`read_reference` 是文档小节、`commit_detail` 是第几个文件。
        # 工具会在返回文本的抬头里写明「共几段 / 这是第几段 / 怎么要别的段」。
        #
        # **不合法一律清空**，而不是丢掉整条请求：内容本身仍然有用，模型把窗口写坏的代价
        # 只能是「拿到的还是默认那一段」。但格式必须是规范形态（`1180-1260`），这样它进得了
        # 去重键、也进得了日志 —— 否则同一个文件的两段窗口会被去重成一条，模型要第二段时
        # 拿回第一段。
        lines = _normalize_line_window(request.lines) if request_type in _WINDOW_TYPES else ""

        # 点名了仓库时先过仓库闸门（P1a）。它排在**去重键之前**：被拒的请求不该占用
        # 一个去重位，否则同一轮里「先点名一个错的仓库、再点名对的」会被当成重复丢掉。
        named = str(request.repository_id or "").strip()
        if named:
            reason = _repository_gate(scope, resolved, path, named)
            if reason:
                dropped.append(
                    DroppedItem("request", index, reason, f"{path}（仓库 {named}）")
                )
                continue

        # 仓库是去重键的第 5 位：同一条提交、同一条路径、**两个仓库**各要一次是两条请求，
        # 只去重掉一条等于把另一个仓库的内容静默吞掉（与 `file_content` 那条支路同一个理由）。
        key = (request_type, resolved, path, lines, named)
        if key in seen:
            continue  # 同一轮内重复索要不重复执行
        seen.add(key)
        allowed.append(
            ContextRequest(
                type=request_type,
                commit=resolved,
                path=path,
                lines=lines,
                repository_id=named,
            )
        )

    return tuple(allowed), tuple(dropped)


def _repository_gate(scope, commit: str, path: str, raw_repository_id: str) -> str:
    """点名了仓库时，这条 `(仓库, 提交, 路径)` 该不该被拒（空串 = 放行）。

    ## 两道判据，判的都是「这条三元组存不存在」

    1. **仓库在不在本批次里**（`repository_ids_by_commit`）。本批次带这个提交号的是哪几个
       仓库是写侧冻结的事实，点名一个不在其中的仓库没有意义 —— 多半是把别处的编号写了
       进来。集合为空（手工构造的 scope、单提交模式）时**不判**：那是「不知道」，不是
       「一个都不许」。
    2. **这条三元组本身**（`scope.entry_allowed`，纯三态）。它拦的是本批次里真实存在的
       那种错配：两个仓库同窗、都有 revision 42，而 42 在 A 仓改的是 `config/x.xlsx`、
       在 B 仓改的是 `code/y.lua` —— 模型点名 A 仓却要 `y.lua` 时，取数层会**读到 B 仓的
       同名文件**（或者查不到而回一句含糊的失败），而这里的拒绝说得出「它属于哪个仓库」。

    理由里**必须带上候选**：模型下一轮照它改一个字段就能问对，而「这个仓库不在本批次里」
    这类话不写出候选，它只能盲试。仓库名不在这层的职责里（要冻结对象才拿得到），所以这里
    只说编号 —— 面向上层的拒绝里那句带名字的版本由取数层发（`identity_refusal`）。
    """
    try:
        named = int(str(raw_repository_id).strip())
    except (TypeError, ValueError):
        return "repository_id 必须是一个仓库编号（数字），这次给的形状不对"
    allowed = frozenset(scope.repository_ids_by_commit.get(commit) or ())
    if allowed and named not in allowed:
        others = "、".join(str(item) for item in sorted(allowed))
        return (
            f"仓库 {named} 不在本批次里：本批次带 commit {commit[:12]} 的是仓库 {others}。"
            "请照这些编号改一个再问。"
        )
    if scope.entry_allowed(named, commit, path) is False:
        owners = "、".join(str(item) for item in sorted(scope.repositories_for_path(path, commit)))
        return (
            f"这条 `(仓库 {named}, commit {commit[:12]}, {path})` 在本批次里不存在："
            f"这个文件在本批次里属于仓库 {owners}。"
            "**不同仓库里的同名文件内容并不相同**，点名别的仓库会读到另一份。"
        )
    return ""


# 行窗口的规范形态：`1180-1260`（单行写成 `1180`）。上限只是防呆 —— 真正的夹紧在
# `utils/content_window.slice_lines` 里按实际行数做。
_LINE_WINDOW_RE = re.compile(r"(\d{1,7})\s*(?:[-~—到]\s*(\d{1,7}))?")


def _normalize_line_window(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = _LINE_WINDOW_RE.fullmatch(text)
    if match is None:
        return ""
    start = int(match.group(1))
    if start < 1:
        return ""
    if match.group(2):
        end = int(match.group(2))
        if end < start:
            return ""
        return f"{start}-{end}"
    return str(start)


# --------------------------------------------------------------------------
# 冻结仓库范围（`sanitize_requests` 的 `repo_paths` 那一支）
# --------------------------------------------------------------------------

#: 走**仓库范围**判据的请求类型（工作包 D 的 P1）。`file_diff` 刻意不在里面：
#: 它要一条提交才算得出差异，而「同一条提交上任意文件」这个概念对 diff 不成立
#: （未改动 = 没有差异，给它是给一份空的东西）。
_REPO_SCOPED_TYPES = frozenset({"file_content"})


def _repo_matches_prefix(repo_set, prefix: str) -> bool:
    """这个前缀匹配到冻结版本里的跟踪文件吗（`repo_set` 为 `None` 时恒 False）。"""
    if repo_set is None:
        return False
    text = str(prefix or "")
    if not text:
        return False
    return any(item == text or item.startswith(text) for item in repo_set)


def _content_request(
    request: ContextRequest,
    index: int,
    request_type: str,
    scope,
    repo_set,
    resolve_repo_path,
):
    """`file_content` 的判据：`(可执行的请求, None)` 或 `(None, 记账)`。

    ## 两条路，**先判批次授权**

    同一条路径在两种意义上都可以是被允许的，而它们读到的**不是同一份东西**：

    1. **本批次授权**（`commit` 是本批次的、且这个文件在那条提交的改动清单里）→ 读
       **那条提交上**的版本。周内改过、tip 上已经删掉的路径因此仍然读得到（实测 run 57
       的 `config/150_shopping_mall/…`：周内提交里确实有，tip 上已无）；「改了公共接口、
       去看**当时**的调用方」也走这条。
    2. **背景核查**（路径在本项目冻结仓库的跟踪树里）→ 读服务端固定的**冻结 tip** 版本。
       `commit` 可以不给、也可以给一条不属于本批次的 —— 它只用来描述范围。硬要求模型
       写对一条本批次的提交，会把「我已经知道调用方路径了、直接读」这条最省的路径又变成
       一次试错。

    ## 为什么顺序不能反（2026-09-24，run 57）

    原先这条判据在批次那条**之前**返回，于是只要冻解范围可用，**每一条** `file_content`
    都走第 2 条：批次授权连问都不问，而第 2 条的路径判据是「在不在**第一个**冻结仓库的
    当前 tip 跟踪树里」—— 一次周版本分析同时覆盖配置仓库与代码仓库时，代码仓库的文件
    全部落进那一句拒绝里。实测 run 57 的 7 条被拒请求中有 4 条是 `code/qz_*`。

    ## 路径形状先判、且判据只有一份

    `resolve_repo_path` 来自 `frozen_repo`（绝对路径 / 盘符 / `..` / 控制字符各有一条
    可照做的理由）。在这里先判一次，是为了让模型**这一轮**就拿到准确的原因；
    真正的读取还会在取数层再判一次（那里才是安全边界）。**扩大范围到「本项目全部仓库」
    只放宽了第 2 条的路径集合，这两条判据一步都没绕过。**
    """
    raw_path = str(request.path or "")
    resolved = scope.resolve_commit(request.commit)
    # 仓库维度**只在这一层做形状归一**（去空白）；「这个仓库是不是真的跟踪这条路径」是
    # 取数层的判据（那里才有 `paths_by_repository`）—— 这一层只回答「该不该执行」，
    # 多判一次反而会多一句可能过期的拒绝理由。
    repository_id = str(request.repository_id or "").strip()

    if repo_set is None:
        # **没有冻结范围**：只有本批次一个授权来源，判据与本函数出现之前**逐字相同**
        # （`sanitize_requests` 的契约：`repo_paths=None` 时行为不变）。
        if resolved is None:
            return None, DroppedItem("request", index, "commit 不属于本批次", request.commit)
        if not normalize_path(raw_path):
            return None, DroppedItem(
                "request",
                index,
                f"{request_type} 必须带 path（这次没给，或给了归一化后为空的路径）",
                repr(request.path),
            )
        if not scope.path_allowed(resolved, raw_path):
            return None, DroppedItem(
                "request",
                index,
                _batch_mismatch_reason(scope, resolved, raw_path),
                f"{raw_path}（配的是 {resolved[:12]}）",
            )
        return (
            ContextRequest(
                type=request_type,
                commit=resolved,
                path=normalize_path(raw_path),
                lines=_normalize_line_window(request.lines),
            ),
            None,
        )

    normalized, reject = resolve_repo_path(raw_path)
    if reject:
        return None, DroppedItem("request", index, f"路径不合法：{reject}", raw_path)
    lines = _normalize_line_window(request.lines)

    if resolved is not None and scope.path_allowed(resolved, raw_path):
        # 第 1 条：读**那条提交上**的版本。
        return (
            ContextRequest(
                type=request_type,
                commit=resolved,
                path=normalized,
                lines=lines,
                repository_id=repository_id,
            ),
            None,
        )

    if normalized in repo_set:
        # 第 2 条：读冻结 tip 上那一版。`commit` 原样带着（可能是空的、也可能是模型随手
        # 写的那一条）——取数层对「不在本批次」的路径一律读冻结版本，不看它。
        # `repository_id` 同理：同一条路径在多个仓库里都有时，取数层据此读指定仓库的那一版；
        # 给错了（那个仓库没跟踪它）取数层会回一份说清「跟踪它的是哪几个」的拒绝。
        return (
            ContextRequest(
                type=request_type,
                commit=str(request.commit or "").strip(),
                path=normalized,
                lines=lines,
                repository_id=repository_id,
            ),
            None,
        )

    # 两条都不通。理由要**说清是哪一条不通**，模型才知道下一步该换路径还是换提交。
    if resolved is None:
        return None, DroppedItem(
            "request",
            index,
            "这个路径不在本次冻结版本的 Git 跟踪文件里（拼错、改过名，或在别的仓库）",
            normalized,
        )
    suggested = scope.commit_of_path(normalized)
    hint = f"；本批次里改过它的是 {suggested[:12]}，换那条提交再问" if suggested else ""
    return None, DroppedItem(
        "request",
        index,
        f"这个路径既不在 commit {resolved[:12]} 的改动清单里，"
        f"也不在本项目冻结仓库的跟踪文件里（拼错、改过名，或它在别的项目里）{hint}",
        f"{normalized}（配的是 {resolved[:12]}）",
    )


def _batch_mismatch_reason(scope, resolved: str, raw_path: str) -> str:
    """「这个文件不在这条提交的改动清单里」那一句（**单一来源**）。

    两个读者，两边都因为「只说『不属于』」吃过亏：

    * **模型**：它只收到「本轮没有附带任何上下文」，于是把「我把 (commit, path) 配错了」
      写成「平台取数失败」，还写进报告的信息缺口 —— 读者会去找一个不存在的平台故障。
      实测那一轮 28 条被拒（占索取数 23%）。
    * **人**：`detail` 原先只记 path、不记 commit，事后根本判不出是谁配错了。

    本批次里改过这个文件的是哪条提交，平台是知道的（`commit_of_path`，
    `find_references` 用的也是它）—— 说出来，模型下一轮就能问对。
    """
    suggested = scope.commit_of_path(raw_path)
    if suggested:
        return (
            f"这个文件不在 commit {resolved[:12]} 的改动清单里；"
            f"本批次里改过它的是 {suggested[:12]}，换那条提交再问"
        )
    return (
        f"这个文件不在 commit {resolved[:12]} 的改动清单里，"
        "也不在本批次改动过的任何文件里"
    )


# --------------------------------------------------------------------------
# 健康检查与纠正提示
# --------------------------------------------------------------------------


def looks_like_markdown_report(text: str) -> bool:
    """回答是否已经像一份报告（用于轮次耗尽时的降级）。

    判据是命中的约定章节标题数。这些标题在 SKILL.md 里被固定下来，**格式约束因此
    同时充当健康检查**——不需要额外让模型输出一个「我完成了」的标记。
    """
    content = _THINK_BLOCK_RE.sub("", str(text or ""))
    hits = sum(1 for section in REPORT_SECTIONS if f"# {section}" in content)
    return hits >= REPORT_HEALTH_MIN_SECTIONS


# 抢正文用：`report_markdown` 的键与开引号。**不能**用一个吃掉整个字符串值的正则 ——
# 实测模型会把长字符串切成好几段（`"第一段","第二段"`），吃整段的写法只能抢到第一段
# （run 7：957 字 / 全长 55k）。所以只匹配到开引号，之后按字符扫（见 `_read_json_string`）。
_REPORT_MARKDOWN_OPENING_RE = re.compile(r'"report_markdown"\s*:\s*"')
# 续写块：逗号 + 引号。
_CONTINUATION_RE = re.compile(r'\s*,\s*"')
# 续写块的最小长度。JSON 的键名（`"anomalies"`、`"dimensions"`）永远比它短 ——
# 没有这道闸，正常收尾的 `"report_markdown": "…", "anomalies": [` 会把键名吃进正文。
_CONTINUATION_CHUNK_MIN = 40

# 输出被截断时发给模型的纠正提示。与 `build_correction_hint` 分开写：那个说的是
# 「你没按协议」，这个说的是「你写太长了」——**模型的应对完全相反**
# （前者要它改格式，后者要它砍内容）。
TRUNCATED_OUTPUT_HINT = (
    "上一轮的回答**被截断了**（单次输出有长度上限，JSON 没有收尾，因此无法解析）。"
    "请**压缩篇幅后完整重发**：正文按后果排序、同类条目合并成一条写，只保留能改变"
    "结论的内容；`dimensions` 与 `anomalies` 必须完整，整份 JSON 必须能解析。"
)


def _decode_json_string_body(body: str) -> str | None:
    """把一段 JSON 字符串的**内容**（不含首尾引号）解码成真文本；解不开返回 None。"""
    try:
        return json.loads(f'"{body}"')
    except ValueError:
        # 转义序列本身被截断（例如末尾是 `\u12`）——这一段救不回来。
        return None


def _read_json_string(content: str, start: int) -> tuple[str | None, int]:
    """从 `start`（开引号**之后**的那一位）读一个 JSON 字符串。

    返回 `(值, 下一个位置)`。**没有闭合引号时读到文本末尾为止** —— 被截断的那种情况
    本来就没有闭合引号，按「就到这里」处理正是我们要的。
    """
    chars: list[str] = []
    index = start
    while index < len(content):
        char = content[index]
        if char == "\\":
            if index + 1 >= len(content):
                break
            chars.append(content[index:index + 2])
            index += 2
            continue
        if char == '"':
            return _decode_json_string_body("".join(chars)), index + 1
        chars.append(char)
        index += 1
    return _decode_json_string_body("".join(chars)), len(content)


def salvage_report_markdown(text: str) -> str | None:
    """从**被截断的**响应里把 `report_markdown` 的正文抢出来；抢不到返回 None。

    ## 为什么需要它

    汇总那一步的正文常常两万多 token（实测 run 5/6/7 分别是 25.9k / 20.6k / 28.2k），
    撞上网关的单次输出上限就断在半截，`json.loads` 必然失败。而整份响应里最值钱的就是
    这段正文：不抢的话，用户拿到的是一坨 **JSON 源码**。

    最糟的一点是它**还会被误判成「像一份报告」**——`looks_like_markdown_report` 是在
    整段文本里数章节标题，而 JSON 字符串里那些 `\\n# 变更理解` 照样能数到，于是走了
    markdown 降级那条路，把 JSON 原文当正文存了下来（2026-09-21 run 7 实测：55k 字的
    报告被包在 `{"status": "final", "report_markdown": "…"` 里面，界面与导出都是这样）。

    ## 还要接着吃「续写块」

    拿 run 7 的原文试过：**只读第一个字符串只能抢到 957 字（全长 55k）**。因为模型写
    长字符串时是这么断的 —— `"第一段","第二段","第三段…`：每一段都是合法字符串，但
    段与段之间只有逗号、没有键名，整份 JSON 因此不合法。这不是截断，是模型自己的切分
    习惯，两种形态都会走到这里，所以续写块也要接上。

    接的条件有两条，缺一不可：**必须是「逗号 + 引号」**的续写形状，且这一段**够长**
    （`_CONTINUATION_CHUNK_MIN`）。后者是防 `"report_markdown": "…", "anomalies": […]`
    这种正常收尾 —— 那里逗号后面也是一个字符串（键名），但键名永远很短。

    ## 只抢正文，不抢 `anomalies`

    截断点通常落在正文之后（它是最后、最大的一个字段），此时 `anomalies` 数组要么没
    开始、要么断在半截。**按半个数组解析出来的结论比没有更危险**：条目不全却看起来是
    一份完整清单。所以这里只抢正文，结构化结论该没有还是没有，降级标签照旧。
    """
    content = _THINK_BLOCK_RE.sub("", str(text or ""))
    merged = _report_chunks(content)
    if merged is None:
        return None
    report, _opening_end, _end, _chunk_count = merged
    return report if report.strip() else None


def _report_chunks(content: str) -> tuple[str, int, int, int] | None:
    """把 `report_markdown` 的正文（含续写块）从 `content` 里读出来。

    返回 `(拼接后的正文, 开引号之后的位置, 最后一段之后的位置, 段数)`；开不了头
    （没有 `report_markdown`、或第一个字符串空/救不回来）返回 None。`salvage_report_markdown`
    与 `repair_split_string_payload` 共用这一份扫描逻辑 —— 两者的差别只在**拿这些段去干嘛**
    （一个只要正文，一个要把整份 JSON 拼回去），段怎么读必须是同一套判据。
    """
    opening = _REPORT_MARKDOWN_OPENING_RE.search(content)
    if opening is None:
        return None
    value, position = _read_json_string(content, opening.end())
    if value is None or not value.strip():
        return None
    parts = [value]
    while True:
        separator = _CONTINUATION_RE.match(content, position)
        if separator is None:
            break
        chunk, next_position = _read_json_string(content, separator.end())
        if chunk is None or len(chunk) < _CONTINUATION_CHUNK_MIN:
            break
        # 切点落在哪都是可能的，但**标题必须留在行首**：下游按 `^# ` 切章节，把
        # `# 变更内容摘要` 粘在上一段的句尾会让那一节整个丢掉。
        if chunk.startswith("#") and not parts[-1].endswith("\n"):
            parts.append("\n\n")
        parts.append(chunk)
        position = next_position
    return "".join(parts), opening.end(), position, len(parts)


def repair_split_string_payload(text: str) -> str | None:
    """把模型**切成多段字符串**的 final payload 拼回一份能解析的 JSON；拼不回返回 None。

    ## 这是实测过的一种失败形态（2026-09-23 run 38 的 S2 第 7 轮）

    模型写两万 token 的正文时会把字符串断开重开：`"report_markdown": "第一段","第二段",`
    段与段之间只有逗号、没有键名，整份 JSON 因此**不合法**——但括号是配平的、
    `finish_reason` 也是 `stop`。于是三道判据全数绕过：`parse_payload` 解析失败、
    `looks_like_truncated_json` 判否（配平）、`looks_like_markdown_report` 判真
    （正文里的 `\\n# 变更理解` 照样能数到章节标题）—— 整轮被当成 markdown 报告重发。
    重发的代价不只是 token：模型按「原样转成 JSON」交回的正文**普遍更短**
    （那一轮 16.5k token 的正文，重发回来只剩 6.7k），内容先丢了一截。

    而 `anomalies` / `dimensions` 写在正文**之后**、本来就是好的：把续写块拼回一个
    字符串，这份 JSON 就能解析 —— 不用重发、不用降级、正文一个字不少。

    ## 拼不回的形态

    * **没有被切开**（段数为 1）：这不是「切开」的病，交给别的分支按各自判据处理；
    * **连后续字段也坏了**：拼回后仍解析失败，同样交给别的分支 —— 调用方拿 None/解析失败
      走原有路径，本函数不堵任何路。
    """
    content = _THINK_BLOCK_RE.sub("", str(text or ""))
    merged = _report_chunks(content)
    if merged is None:
        return None
    report, opening_end, end, chunk_count = merged
    if chunk_count < 2:
        return None
    # 开引号之前与最后一段之后的内容原样保留：前者是 `{"status": "final", …`，
    # 后者是 `,"anomalies": …}` —— 病只在中间那几段，只修中间。最后一段的闭合引号
    # 在扫描时被吃掉了，这里补回来。
    return (
        content[:opening_end]
        + json.dumps(report, ensure_ascii=False)[1:-1]
        + '"'
        + content[end:]
    )


# report_markdown 之后的下一个顶级键。final 载荷必有 `dimensions` 与 `anomalies`
# （协议要求，见 parse_payload 的校验），所以它们的位置就是正文字符串的**真实边界**
# —— 引号转义修复要靠它定位「哪个引号是闭合引号、哪些是体内裸引号」。
_NEXT_FINAL_KEY_RE = re.compile(
    r'\s*,\s*"(?:anomalies|dimensions|candidate_dispositions)"\s*:'
)


def repair_unescaped_quotes_payload(text: str) -> str | None:
    """把 report_markdown 体内**未转义的双引号**转义回去；不属于这种病返回 None。

    ## 实测形态（2026-09-23 run 41 的汇总第 4 轮，trace 存档了完整原文）

    模型写正文时原样引用了带双引号的代码 —— `require("headcode/LaunchArgs")` ——
    而没有做 JSON 转义。`json.loads` 在那个引号处认定字符串提前闭合，于是「括号
    配平、finish_reason=stop 的合法开头 JSON」又一次绕过截断判据、掉进 markdown
    降级分支整轮重发（与续写块是同一个漏斗的两种病）。

    ## 为什么边界是「下一个顶级键」而不是「下一个引号」

    体内的裸引号长得和闭合引号一模一样，逐个猜必错。但 final 载荷的
    `anomalies` / `dimensions` / `candidate_dispositions` **一定**写在正文之后
    （协议校验钉着），所以「第一个出现在正文之后的顶级键」之前的最后一个引号
    才是真实闭合。定位错了也不怕：修完的文本还要过一遍 `parse_payload`，
    解析失败就按没修过处理（同 `repair_split_string_payload` 的规矩，不堵路）。

    ## 体内没有裸引号时返回 None

    那就不是这种病（错在别处），交回原分支 —— 别让调用方的「失败留痕」把病因
    记到引号头上。
    """
    content = _THINK_BLOCK_RE.sub("", str(text or ""))
    opening = _REPORT_MARKDOWN_OPENING_RE.search(content)
    if opening is None:
        return None
    following = _NEXT_FINAL_KEY_RE.search(content, opening.end())
    if following is None:
        return None
    closing = content.rfind('"', opening.end(), following.start())
    if closing < opening.end():
        return None
    body = content[opening.end():closing]
    out: list[str] = []
    index = 0
    total = len(body)
    has_raw_quote = False
    while index < total:
        char = body[index]
        if char == "\\":
            # 已有的合法转义（`\n`、`\"`…）整对保留，**不许二次转义**。
            out.append(body[index:index + 2])
            index += 2
            continue
        if char == '"':
            out.append('\\"')
            has_raw_quote = True
            index += 1
            continue
        out.append(char)
        index += 1
    if not has_raw_quote:
        return None
    return content[:opening.end()] + "".join(out) + content[closing:]


# --------------------------------------------------------------------------
# 输出通道错位：模型把「取数请求」写成了工具调用信封
# --------------------------------------------------------------------------
#
# ## 实测形态（`ai_analysis_trace` 里 run 38 / 44 / 45 共 5 轮）
#
# 模型不写 JSON，而是吐一段**工具调用信封**：外面包着调用标签、里面每条是一个
# `invoke` 标签（标签名两侧是全角竖线 `｜｜` + `DSML`），参数体放在开标签与闭标签之间。
# 原文样本（逐字取自 `ai_analysis_trace.response_text`，见下面的用例注释）里出现过
# 三种 `invoke`：
#  * 体是**协议请求 JSON**（`{"type": "file_diff", …}`）—— 能就地救回来；
#  * 空体、**地址写在属性里**（`name="<20 位十六进制>"`）—— 只有 `evidence` 是这个形状；
#  * 体是一段**思考散文**（`think`）—— 里面没有任何请求，救不了，只能重问。
#
# ## 判据为什么必须保守
#
# 从一段**自由文本**里认出「模型想要什么」是猜；猜错的代价不是少一条请求，而是平台**替
# 模型编了一份请求**（它会进白名单、会被执行、会在报告里留下没有证据的结论）。所以这里
# 只吃上面那两种有明确形状的形态，其余一律 `None`，交回原有的纠正提示：
#  * 纯思考（`think`）、未知工具名、普通正文 → `None`；
#  * 抽出来的东西**照样**要过 `parse_payload → sanitize_requests → ground_payload`：
#    本函数只把它拼回一份 JSON 文本，白名单一步都不少（越权 commit / 畸形地址照样被丢、
#    照样记账）。
#  * **绝不伪造 `final`**，也**绝不**回一个空 `requests` 的 `need_more_context` —— 后者会
#    撞上「`need_more_context` 必须给出非空 requests」，反而把纠正提示变得更差。

# 信封里的任何标签（`calls` / `invoke` / `parameter`）。竖线**全角半角都认**：本机抓到的
# 四个真实样本用的都是全角（`U+FF5C`），而半角竖线在别处也可能出现，认它不增加风险
# —— 判据的严格性在下面那两个形状上，不在标记本身。
_DSML_TAG_RE = re.compile(r"<\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}[^<>]*>", re.I)
# `invoke` 的开标签：捕获属性串（可能以 `/` 结尾 = 自闭合）。
_DSML_INVOKE_OPEN_RE = re.compile(
    r"<\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*invoke\b([^<>]*)>", re.I
)
# 任意一个 DSML 闭标签。它同时也是「下一条 invoke 已经开始」的界碑（见 `_dsml_body`）。
_DSML_CLOSE_RE = re.compile(r"<\s*/\s*[|｜]{1,2}\s*DSML[|｜]{1,2}[^<>]*>", re.I)
# 属性里的 `name="…"`（信封里同一行可能出现两个 `name`：前一个是工具名）。
_DSML_ATTR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_:.-]*)\s*=\s*\"([^\"]*)\"")
# `evidence` 的地址形状（`evidence_store.blob_id_of` 的 20 位十六进制）。与
# `_EVIDENCE_ID_RE` 是**同一条约定**，用 `fullmatch` 判「整条属性值就是一个地址」
# （为什么不能是 `search`，见 `_dsml_request` 里那段）。
_DSML_ADDRESS_RE = re.compile(r"[0-9a-fA-F]{20}")


def looks_like_tool_call_envelope(text: str) -> bool:
    """回答「这段文本是不是一段工具调用信封」。

    只判**标记在不在**，不判它里面有没有可用请求：两种读者需要的正是这个粗判据 ——
    引擎据它把这一轮的纠正额度记到「输出通道错位」那一本账上（而不是协议那本），
    并据此换一句**正面事实**的提醒。
    """
    return _DSML_TAG_RE.search(str(text or "")) is not None


def _dsml_body(content: str, start: int) -> str:
    """一个 `invoke` 开标签之后、下一个 DSML 标签之前的正文。

    界碑取「最近的闭标签」与「下一条 invoke 的开标签」里更靠前的那个：实测样本里两种
    收尾都出现过（`</… invoke>`、以及自闭合 `/>` 后直接跟下一条），只认闭标签会把
    「自闭合 + 下一条开标签」之间的那一段（也就是下一条的属性）当成正文。
    """
    ends = [len(content)]
    for pattern in (_DSML_CLOSE_RE, _DSML_INVOKE_OPEN_RE):
        match = pattern.search(content, start)
        if match is not None:
            ends.append(match.start())
    return content[start : min(ends)]


def _dsml_request(content: str, match: re.Match) -> dict | None:
    """把一个 `invoke` 读成一条**协议请求对象**；认不出这个形状返回 None。"""
    attributes = match.group(1) or ""
    # 自闭合（`… name="…" />`）的体一定是空的，别把下一条的属性当体。
    body = "" if attributes.rstrip().endswith("/") else _dsml_body(content, match.end()).strip()
    if body:
        try:
            value = json.loads(body)
        except (ValueError, TypeError):
            return None
        # `invoke` 名（`get_file_diff` / `request` / …）**一律不看**：它正是模型自己编的
        # 那部分（run 44 的 S4 编出过 `get_file_diff`）。可信的只有体里那个平台协议对象。
        if not isinstance(value, dict) or not _as_str(value.get("type")):
            return None
        return value
    # 空体：唯一认识的形态是「地址写在属性里」。**在全部属性值里找那个地址**，不取「第一个
    # `name`」—— 信封里 `name="evidence"`（工具名）就在地址前面，取第一个会把工具名当地址。
    #
    # 判据是 `fullmatch`（整条属性值就是一个 20 位地址），**不是 `search`**：`search` 会把
    # `commit="<40 位提交号>"` 的前 20 位当地址，于是平台**凭空造出**一条地址合法的
    # `evidence` 请求（它能过白名单，执行层只会回一句「取不到」）—— 那正是「替模型编请求」。
    for _key, raw in _DSML_ATTR_RE.findall(attributes):
        value = raw.strip()
        if _DSML_ADDRESS_RE.fullmatch(value) is not None:
            return {"type": EVIDENCE_REQUEST_TYPE, "name": value}
    return None


def repair_dsml_tool_calls_payload(text: str) -> str | None:
    """认出工具调用信封、把里面的请求抽成一份协议 JSON；抽不到返回 `None`。

    ## 实测收益（`ai_analysis_trace` 的真实原文，逐字喂进 `parse_payload`）

    | 样本 | 抽出请求 | 结果 |
    |---|---|---|
    | run 44 **S4** 第 1 轮 | 4 条（3 `file_diff` + 1 `evidence`） | 解析通过；`sanitize_requests` 放行 3 条、丢 1 条（地址只有 16 位，形状不对） |
    | run 38 **V1** 第 1 轮 | 1 条（`evidence`） | 解析通过 |
    | run 44 **S2** 第 1 轮 | 0 条（纯思考） | 返回 `None` → 走原有的重问，**行为逐字不变** |

    即：**有请求的那种整轮救回**（省一轮 + 一次纠正额度），纯思考那种救不了、仍靠重试。

    ## 合成的按语

    拼回去的 JSON 除了 `requests` 还带上 `reason_code` / `reason`：这一份 payload 得能
    说清「我的请求是从信封里抽出来的」，而不是**看起来**像模型自己按协议写的一样。

    留痕分工要说准（本条是**实测**，不是推断）：这两个字段跟着 payload 走，而 payload
    除了 `candidate_dispositions` 之外没有别的读者；**trace 上真正留痕的是引擎那一轮的
    `note`**（"模型把取数请求写成了工具调用信封（工具调用信封抽取）…未重发"）。两处都写，
    是为了让「读 trace 的人」与「读这份 payload 的人」都不会把它当成模型的正常输出。
    """
    content = str(text or "")
    if not content.strip() or _DSML_TAG_RE.search(content) is None:
        return None
    requests: list[dict] = []
    for match in _DSML_INVOKE_OPEN_RE.finditer(content):
        entry = _dsml_request(content, match)
        if entry is not None:
            requests.append(entry)
    # 一条都没抽到：**不许**回一个空 `requests` 的 need_more_context（见本节开头），
    # 老老实实返回 None，让纠正提示那条老路去处理。
    if not requests:
        return None
    return json.dumps(
        {
            "status": STATUS_NEED_MORE_CONTEXT,
            "reason_code": "recovered_from_tool_envelope",
            "reason": "平台已从工具调用信封里抽取本轮的取数请求（这几条是信封里的原始内容）。",
            "requests": requests,
        },
        ensure_ascii=False,
    )


def looks_like_truncated_json(text: str) -> bool:
    """回答「这段文本是不是一份没收尾的 JSON 对象」。

    判据是**括号没配平**（跳过字符串内部）：以 `{` 开头、扫到文本结束深度仍大于 0。
    为什么不只看「结尾不是 `}`」：模型常把 JSON 写完后再补一句说明（`{…}\\n以上。`），
    那种回答是**完整**的、只是不合协议，该给的提示是「你没按协议」，而不是「你写太长了」
    —— 两者要模型做的事正好相反。

    这段文本是**被截断**还是**不完整**，配合 `salvage_report_markdown` 一起用：抢得到
    `report_markdown` 说明断点在正文之后，抢不到（例如它在要上下文的半截被切断）也照样
    是截断 —— 那时同样该叫它压短，而不是叫它改格式。
    """
    content = _THINK_BLOCK_RE.sub("", str(text or "")).strip()
    if not content.startswith("{"):
        return False
    depth = 0
    in_string = False
    escaped = False
    for char in content:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return depth > 0


# 「输出通道错位」要说给模型的那句**正面事实**。
#
# 为什么写成正面事实、而不是「禁止…」：本仓已经吃过一次亏 —— `build_correction_hint`
# 第 2 条写着「不要输出 `<think>` 块」，而 `think` 恰恰是实测里出现过的 `invoke` 名
# （run 44 的 S2 那一轮）。**写「禁止 X」会把 X 的字面量送进上下文。**
#
# 也**不复述那段信封的写法**：旧行为是把模型那段错误信封原样抄回去，对这种病等于又示范
# 了一遍（run 45 的汇总第 1 轮就是这么被重问的）。所以这里只说清两件事：没有可调用的
# 工具、以及这些名字到底该写在哪。
_OUTPUT_CHANNEL_FACT = (
    "本次分析里**没有可以直接调用的 API 工具**：平台只接受一个 JSON 对象，"
    "而 `commit_detail`、`file_diff`、`file_content`、`read_reference`、`find_references`、"
    "`evidence` 这六个名字是**写在 `requests` 数组里的平台取数类型**，由平台代为执行、"
    "结果附在下一轮。想取什么就按这些类型写进 `requests`。"
)


def build_channel_mismatch_hint() -> str:
    """模型走了「工具调用」这条不存在的通道时，发给它的纠正提示。

    这一句**只讲事实、不举反例**（理由见 `_OUTPUT_CHANNEL_FACT` 上面那段），也**不回显**
    模型上一轮那段信封 —— 两个读者都因此受益：模型不必再读一遍自己的错误写法，人读 trace
    时看到的是「平台怎么纠正的」而不是「模型又抄了什么」。
    """
    return (
        "你上一轮把取数请求写成了**对工具的调用**，而这条通道本次不存在。"
        + _OUTPUT_CHANNEL_FACT
        + "\n请只返回那个 JSON 对象：`status` 为 `need_more_context`，"
        "把要取的内容逐条写进 `requests`。"
    )


def build_correction_hint(
    error: Exception, *, dimension_ids: Iterable[str] = DIMENSION_IDS
) -> str:
    """把协议错误转成下一轮要说给模型听的话。

    ## 为什么这句里不再有「N 个维度」

    这里写过 `len(DIMENSION_IDS)`，也就是**平台出厂**的维度数。可维度清单是项目可声明的
    （`LoadedSkills.dimensions`，见 `skill_contract.render_dimension_section`），而这一层
    拿不到它 —— 一个声明了 12 个维度的项目，模型从提示词里读到 12 个，纠正提示却说
    「9 个维度都要写」：**校验按 A、提示词按 B**，而它既不会报错、也没人会去数。

    所以这句话改成**指回模型手里的那份清单**（系统提示词里那一节），不写任何数字。
    模型看不到清单时（例如清单就是出厂默认那九个，正文里逐条展开了）它照样知道要写几个。

    ## `dimension_ids`：清单与出厂默认不同时，把 id 逐个列出来

    与出厂默认相同时**一个字节都不加** —— 那九个 id 已经在 SKILL.md 正文里逐条展开过，
    再抄一遍只是白花提示词预算（而默认行为逐字不变是这个仓库的硬要求）。

    与出厂默认不同时，模型手里那份清单在正文末尾的追加节里，而这一轮它是**被纠正**的
    一轮 —— 把 id 逐个点名写进这句话，它就不必回去翻那一节才知道自己该写哪几个
    （`report` 里已经出现过「模型按别的清单写」的真实故障形态）。**仍然只列 id、不写
    条数**：条数一旦写死就会与清单漂移，而漂移不会报错（见上面那段）。
    """
    ids = tuple(str(item).strip() for item in dimension_ids if str(item or "").strip())
    declared = ""
    if ids and ids != DIMENSION_IDS:
        declared = "\n本次生效的维度清单是：" + "、".join(f"`{item}`" for item in ids) + "。"
    # **错误原文里带着工具调用信封时，不回显它。**
    #
    # `ProtocolError` 的「原文开头：…」是给人读的线索，抄进这句提示里就变成了给模型的
    # **示范**（这段文本本来就是模型自己吐错了的东西）。换成同一句正面事实，模型的应对
    # 与「禁止写法」完全一样，但上下文里不再出现那个形态。
    if looks_like_tool_call_envelope(str(error)):
        detail = _OUTPUT_CHANNEL_FACT
    else:
        detail = str(error)
    return (
        f"你上一轮的返回不符合协议：{detail}。\n"
        "请严格修正后重新返回：\n"
        "1. 只返回一个可被 json.loads 解析的 JSON 对象；\n"
        "2. 不要输出 <think> 块、不要用代码围栏包住 JSON、不要写 JSON 之外的说明文字；\n"
        "3. status 只能是 need_more_context 或 final；\n"
        "4. final 必须同时给出非空的 report_markdown 和 dimensions"
        "（系统提示词里那份**本项目适用的维度清单**上的每一个维度都要写，"
        "未命中的写 hit 为 false 并说明理由）；\n"
        "5. 所有自然语言内容使用中文。"
        f"{declared}"
    )


# 三份收敛指令（额度耗尽 / 没有额度 / 最后一轮）共用的尾巴，**必须逐字相同**：
# 它管的是**交回来的形态** —— 证据不足的维度要按协议写 hit=false，而不是整段省掉。
# 以前「预算耗尽」那段文案在 prompt 与 protocol 里各有一份、两句话不一样，下场是改一处
# 漏一处；所以这里只留一份常量。
_CONVERGE_TAIL = (
    "对于证据不足的维度，在 dimensions 里写 hit 为 false "
    "并在 note 里说明「信息不足」，同时在报告里标注信息缺口。"
)


def build_budget_exhausted_hint(*, requests_total: int | None = None) -> str:
    """**上下文索取额度**耗尽时注入的收敛指令。

    **不报错退出**——模型手上已有的证据通常够写一份报告了，强制它收敛比作废整轮好。

    ## `requests_total == 0` 要换一句话

    「**用完**了」与「**从来没有**」在模型那里会长成同一句话，而模型会把这句话原样转述
    进报告的信息缺口。项目把上限配成 0 时，它写出来的是「额度用完」，用户据此去查额度
    怎么会被用完 —— 查不到，因为那是配置。所以 0 这一支明说「没有配置额度」。

    **只管索取额度，不管轮次** —— 轮次先耗尽的那一种见 `build_final_round_hint`。
    """
    if requests_total == 0:
        opening = (
            "本次分析**没有配置上下文索取额度**（上限 0 次），读不到任何文件内容、"
            "也做不了跨文件检索。请基于当前已有的证据直接输出 final，禁止请求上下文。"
        )
    else:
        opening = (
            "补充上下文的预算已耗尽。请基于当前已有的证据直接输出 final，"
            "禁止继续请求上下文。"
        )
    return opening + _CONVERGE_TAIL


def build_markdown_reemit_hint() -> str:
    """把上一条 markdown 报告**原样**转成协议 JSON 的纠正提示。

    引擎原先遇到「模型给了 markdown 而不是 JSON」是直接收工的：正文留下来，结构化结论
    整份放弃 —— 哪怕还剩三轮、纠正额度一次没用（实测 run 10 的 S3 跑满 8 轮后写成
    markdown，它负责的 code_logic / version_branch / process 三个维度**一条结构化结论都
    没进清单**；run 6 也出过同一形态）。而「把上一条原样转成 JSON」比「重新写一份报告」
    容易得多：内容已经在对话里，模型只需要换一种包装。

    所以措辞的重心是**不许借机重写**（新增/省略/改写都会让已经写实的证据变形），以及
    只做格式转换、不要再索取上下文。
    """
    return (
        "你上一条回答是一份 **markdown 报告**，而协议要求的是 JSON。"
        "请把**上一条的内容原样**转成协议 JSON（`status` 为 `final`）：结论、证据、影响、"
        "建议都要**逐条搬过来**，不要新增、不要省略、不要趁这次重写或合并。"
        "只输出这个 JSON，不要再索取上下文。" + _CONVERGE_TAIL
    )


def build_final_round_hint(*, round_index: int, max_rounds: int) -> str:
    """**最后一轮**的收敛指令（轮次先耗尽的那一条路）。

    只按索取额度判「该收尾了」是不够的：额度没花完、轮次先到顶，模型就完全不知道
    这是最后一条消息 —— 实测 run 10 的 S3 跑满 8 轮（只用了 38/40 次索取）之后写了一段
    markdown 叙述，而不是协议 JSON：它负责的三个维度（`code_logic` / `version_branch` /
    `process`）**一条结构化结论都没有**，报告里只能按「跑完了但结论没交回」标出来
    （见 `family_ledger._shard_gap_lines`）。同一形态在 run 6 已经出过一次，那次是 5 轮。

    ## 为什么要把机制说给模型听

    模型对「还剩 2 次索取」是有判断力的：不说清，它就会把这 2 次花掉。而**最后一轮索取
    回来的内容要等下一轮才会送到它手上，下一轮不存在** —— 那些内容平台照样去取，只是
    永远到不了模型眼前。所以这里不是客套地让它「收尾」，而是告诉它这件事实：现在索取
    等于白花一轮，且这一轮之后没有任何机会再交结论。
    """
    return (
        f"**这是本次分析的最后一条消息（第 {round_index}/{max_rounds} 轮）。**"
        "你这一轮索取回来的内容要等**下一轮**才会送到你手上，而下一轮不存在 ——"
        "现在再索取等于把这一轮白白花掉：平台会去取，但你永远看不到。"
        "所以本轮必须直接输出 final（按协议给 JSON），不要写成 markdown 叙述。"
        + _CONVERGE_TAIL
    )
