"""异常清单的规则层：门槛过滤、证据裁剪、近似去重、封顶。

## 为什么这一层必须独立于提示词

「这次报得太多了」和「这条怎么没报」是两个方向相反、但都只能靠调阈值解决的问题。
如果把门槛写在提示词里（「只报告严重问题」），每次调整都要改提示词、重新验证模型行为，
而且模型对「严重」的理解会随上下文漂移——同一份 diff 换个说法就可能多报两条。

阈值放在这里，它就是一个**可配置、可单测、结果可复现**的确定性函数：`min_severity`
从 `high` 调到 `critical`，报出来的条数必然单调不增。这是能被人信任的性质。

## 规则版本为什么用源码哈希

幂等键里要有一个「规则变了就重跑」的成分。手工维护版本号一定会漏改（旧工具维护的
`ITERATIVE_ANALYSIS_PROMPT_VERSION` 就是这个问题：改了提示词忘改版本号，用户永远
拿不到新结果）。这里直接对规则源码取哈希——改了就变，不可能忘。

代价是**改注释也会让历史结果失效**。这个方向是安全的：多跑一次只花时间，而少跑一次
会让用户看着旧报告以为是最新的。

## 纯函数

与 `budget.py` 同理：不读文件、不发请求、不碰数据库。唯一的 I/O 是读取本模块自身的
源码来算版本号。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from services.ai.protocol import Anomaly, DroppedItem

# 严重度与置信度的可比序。数字只用于比较，不对外暴露。
SEVERITY_RANK = {"high": 1, "critical": 2}
CONFIDENCE_RANK = {"high": 1, "very_high": 2}

DEFAULT_MIN_SEVERITY = "high"
DEFAULT_MIN_CONFIDENCE = "high"
# 单次分析最多留下多少条结论。**必须与
# `models.ai_analysis.project_config.DEFAULT_MAX_ANOMALIES_PER_RUN` 一致** ——
# 这里是不带项目配置时的兜底，那里是配置表读不出值时的兜底，两把闸门卡在同一次分析上，
# 取不同的数就会出现「配置说要留 15 条，规则层在第 10 条就截了」。
#
# 10 → 15（2026-09-20，与模型层同一次改动，理由见那边的注释）：这是事后闸门，
# 不花任何 token。
DEFAULT_MAX_ANOMALIES = 15
DEFAULT_MAX_EVIDENCE = 3
# 近似去重的门槛。语义见 `title_containment`。
DEFAULT_SIMILARITY_THRESHOLD = 0.75
# 标题词数低于这个值时**不做**相似度合并（完全相同的措辞仍会合并）。
MIN_SIMILARITY_TOKENS = 4

# 参与规则版本哈希的源文件（相对本模块所在目录）。公开出来是因为「哪些文件定义了规则」
# 本身就是需要被测试断言的事实（版本号必须由这些文件的内容算出来，不能是写死的常量）。
RULE_SOURCE_FILES = ("rules.py",)


class RulesConfigError(ValueError):
    """规则配置不合法。

    **不降级、不猜**：把不认识的 `min_severity` 悄悄退回默认值，会让「管理员以为他把
    门槛调严了、实际没生效」变成静默错误。配置在写入时就校验，这里遇到不合法值唯一的
    解释是有人直接改了库，此时让这一轮明确失败比悄悄换一套告警口径好。
    """


# --------------------------------------------------------------------------
# 相似度
# --------------------------------------------------------------------------

# 拉丁字母/数字词，保留 `_`、`.`、`-`（路径与字段名的常见构成）。
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9_.\-]+")
# 中日韩统一表意文字。中文没有词间空格，按字符切分后取二元组。
_CJK_RE = re.compile(r"[一-鿿]")


def tokenize(text: str) -> frozenset[str]:
    """把一段文本切成用于相似度比较的词集。

    中文按**字符二元组**而不是单字：单字的区分度太低（「道具被删除」和「道具被增加」
    单字集合高度重叠），二元组能保留「删除/增加」这种关键差别。拉丁词按整词取，
    并把路径也作为整体加进去（`a/b.xlsx` 与 `a/c.xlsx` 应当有区分度）。
    """
    lowered = str(text or "").lower()
    tokens: set[str] = set(_LATIN_TOKEN_RE.findall(lowered))

    cjk = _CJK_RE.findall(lowered)
    if len(cjk) == 1:
        tokens.add(cjk[0])
    else:
        tokens.update(cjk[index] + cjk[index + 1] for index in range(len(cjk) - 1))
    return frozenset(tokens)


def containment(left: frozenset[str], right: frozenset[str]) -> float:
    """较短一侧被另一侧覆盖的比例（overlap coefficient）。

    **为什么不用 Jaccard**：本项目是中文场景，而中文标题的词集大小差异很大。实测同一
    问题的若干种重述（本机测量，见 `tests/test_ai_budget_and_rules.py` 的对照表）：

    | 同一条问题的不同说法 | Jaccard | containment |
    |---|---|---|
    | `道具ID被删除` / `道具ID被删除且没有备份` | 0.500 | **1.000** |
    | `【配表】道具ID被删除` / `道具ID被删除` | 0.714 | **1.000** |
    | `【配置表】道具ID被删除` / `道具ID被删除` | 0.625 | **1.000** |
    | `道具ID被删除` / `道具ID被删除（同一问题换了个说法）` | 0.357 | **1.000** |
    | `等级上限配错` / `等级上限配错导致无法升级` | 0.455 | **1.000** |
    | 两条互不相关的标题 | 0.000 | 0.000 |

    Jaccard 的分母随较长一侧膨胀，于是「同一条问题多说了半句」就会被判成两条；而
    containment 只看较短一侧是否被覆盖，正好对上「同一条被展开描述」这个真实形态。
    代价是短标题容易被长标题吞掉，所以另有 `MIN_SIMILARITY_TOKENS` 兜底。

    两个空集返回 0.0：数学上是 0/0，若按某些实现约定返回 1.0，所有「没有可用词」的
    条目会互相判成重复并静默去重到只剩一条。
    """
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def is_probable_duplicate(
    left: Anomaly, right: Anomaly, *, threshold: float = DEFAULT_SIMILARITY_THRESHOLD
) -> bool:
    """两条异常是否应当合并成一条。

    **这是近似去重，不是语义去重** —— 它抓的是「同一条结论被换了个说法又报了一遍」，
    抓不住真正的语义等价（例如完全换词重述）。这个能力边界是刻意保留的：

    * 漏合并的代价是用户多看一条重复项，他可以直接忽略；
    * 错合并的代价是一条真实问题**永远消失**，而且没有任何痕迹。

    所以门槛宁紧勿松，且短标题直接不判（词太少时「被覆盖」说明不了任何事）。

    三个条件同时成立才算重复：

    1. **同 commit**。跨提交的相似标题几乎必然是两件独立的事。
    2. **同文件**。两个不同文件里的「道具 ID 被删除」是两个不同位置的问题，跟进的人
       要分别去看；合成一条会让其中一处永远没人管。任一侧没有文件路径时（有些异常
       是全局性的，例如「本次改动没走配表评审流程」）跳过这条判断。
    3. **标题足够相似**。用标题而不是证据：同一个问题被重述时标题最稳定，而证据的
       措辞（行号、字段名）本来就容易变。
    """
    if (left.commit or "") != (right.commit or ""):
        return False
    if left.file_path and right.file_path and left.file_path != right.file_path:
        return False

    left_tokens = tokenize(left.title)
    right_tokens = tokenize(right.title)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        # 措辞完全相同，无论多短都是同一条。
        return True
    if min(len(left_tokens), len(right_tokens)) < MIN_SIMILARITY_TOKENS:
        return False
    return containment(left_tokens, right_tokens) >= threshold


def anomaly_fingerprint(anomaly: Anomaly) -> str:
    """一条异常的稳定身份指纹。

    用途是**人工处置闭环**：用户把某条标成「已忽略」后，后续重跑报出同一条时应当
    继承处置结果，而不是重新冒出来让用户再点一次。

    指纹是 `commit + 文件 + 标题词集（排序后）` 的哈希，它吸收的是**标点、空白、大小写**
    这类差异。**它不吸收增删词与语序变化** —— 中文按二元组切分，「被删除道具」与
    「道具被删除」的二元组集合并不相同，加了半句话更是完全不同。

    所以**只用指纹做精确匹配是不够的**：调用方在指纹没命中时还应当退回复用
    `is_probable_duplicate` 对历史已处置条目做一次模糊匹配。这一点写在文档里而不是
    悄悄留给调用方猜 —— 少了这一步，用户会发现「我明明忽略过了，怎么又冒出来了」。
    """
    tokens = " ".join(sorted(tokenize(anomaly.title)))
    payload = "\x1f".join((anomaly.commit or "", anomaly.file_path or "", tokens))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 阈值
# --------------------------------------------------------------------------


def _as_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class RuleThresholds:
    """规则层可调的门槛。全部来自项目配置，不由提示词决定。"""

    min_severity: str = DEFAULT_MIN_SEVERITY
    min_confidence: str = DEFAULT_MIN_CONFIDENCE
    max_anomalies: int = DEFAULT_MAX_ANOMALIES
    max_evidence: int = DEFAULT_MAX_EVIDENCE
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD

    def __post_init__(self) -> None:
        for label, value, ranks in (
            ("min_severity", self.min_severity, SEVERITY_RANK),
            ("min_confidence", self.min_confidence, CONFIDENCE_RANK),
        ):
            if value not in ranks:
                raise RulesConfigError(
                    f"{label} 只能是 {sorted(ranks)} 之一，实际是 {value!r}"
                )
        if not 0.0 <= self.similarity_threshold <= 1.0:
            raise RulesConfigError(
                f"similarity_threshold 必须在 0~1 之间，实际是 {self.similarity_threshold}"
            )

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "RuleThresholds":
        """从项目配置行构造。缺字段用默认值，**不合法值抛错**（见 `RulesConfigError`）。"""
        source = dict(config or {})
        return cls(
            min_severity=str(source.get("min_severity") or DEFAULT_MIN_SEVERITY).strip().lower(),
            min_confidence=str(
                source.get("min_confidence") or DEFAULT_MIN_CONFIDENCE
            ).strip().lower(),
            max_anomalies=_as_int(
                source.get("max_anomalies_per_run"),
                default=DEFAULT_MAX_ANOMALIES,
                minimum=0,
                maximum=200,
            ),
            max_evidence=DEFAULT_MAX_EVIDENCE,
            similarity_threshold=DEFAULT_SIMILARITY_THRESHOLD,
        )

    def passes(self, anomaly: Anomaly) -> bool:
        """单条异常是否达到门槛。"""
        return (
            SEVERITY_RANK.get(anomaly.severity, 0) >= SEVERITY_RANK[self.min_severity]
            and CONFIDENCE_RANK.get(anomaly.confidence, 0)
            >= CONFIDENCE_RANK[self.min_confidence]
        )

    def revision_component(self) -> str:
        """影响分析结果的配置成分，用在幂等键里。

        门槛变了就是另一个问题，**必须**重跑——沿用上一次的结果会让用户以为新门槛
        没生效。把它放在这里而不是编排层，是因为「哪些字段会影响结果」这件事只有
        本模块知道。
        """
        return (
            f"sev={self.min_severity};conf={self.min_confidence};"
            f"cap={self.max_anomalies};ev={self.max_evidence};"
            f"sim={self.similarity_threshold:g}"
        )


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------


# 「超出本次上限、按严重度截掉」这一种丢弃的 `kind`。
#
# **不能复用 `"anomaly"`**：那个 kind 已经被协议层占着（`protocol._coerce_anomalies`
# 用它记「severity 不在允许集合内」「evidence 为空」这类**模型自己写坏了**的条目）。
# 两者混在一个 kind 里，读取侧就只能靠 reason 的措辞去分辨 —— 而措辞是会改的，
# 「哪几条是被上限截掉的」是要写进报告正文让用户回看的东西，不能靠一句话认。
KIND_ANOMALY_CAP = "anomaly_cap"


@dataclass(frozen=True)
class NormalizeResult:
    anomalies: tuple[Anomaly, ...]
    # 被丢弃/合并的记账。进 trace，用来解释「为什么只报了 3 条」。
    dropped: tuple[DroppedItem, ...] = ()
    duplicates_removed: int = 0
    capped: int = 0
    evidence_trimmed: int = 0


def _trim_evidence(
    anomaly: Anomaly, index: int, max_evidence: int
) -> tuple[Anomaly, DroppedItem | None]:
    if max_evidence <= 0 or len(anomaly.evidence) <= max_evidence:
        return anomaly, None
    kept = anomaly.evidence[:max_evidence]
    return (
        replace(anomaly, evidence=kept),
        DroppedItem(
            "evidence",
            index,
            f"证据条数超出上限，仅保留前 {max_evidence} 条",
            f"{len(anomaly.evidence)} 条",
        ),
    )


def _rank_key(anomaly: Anomaly) -> tuple[int, int]:
    return (
        SEVERITY_RANK.get(anomaly.severity, 0),
        CONFIDENCE_RANK.get(anomaly.confidence, 0),
    )


def rank_anomalies(
    items: Iterable[tuple[int, Anomaly]],
) -> list[tuple[int, Anomaly]]:
    """按严重度、置信度降序排列 `(原始下标, 异常)`。

    这是**唯一**的排序实现，封顶与归一化都走它。用稳定排序，同档内保持模型给的原始
    顺序——模型的输出顺序里含有它的判断（先说的通常更重要），同档内不该被打乱。
    """
    return sorted(items, key=lambda pair: _rank_key(pair[1]), reverse=True)


def cap_anomalies(
    anomalies: Iterable[Anomaly], max_items: int
) -> tuple[tuple[Anomaly, ...], int]:
    """按上限封顶，**优先保留更严重的**。

    不能按顺序截断：模型输出顺序没有保证，直接砍掉后 N 条完全可能把唯一一条
    `critical` 砍掉，只留一堆 `high`——那比少报更糟，因为它看起来像「已经挑过了」。

    **无论是否触发封顶，返回值一律按同一套次序给出**。这一点是刻意的：如果只在封顶时
    才排序，那么「多报了一条导致超限」会让整份清单的顺序突然变化，用户会以为改动很大。
    返回值已经是最终展示顺序，前端直接渲染即可，不需要再排一次——两处各排一次是
    「顺序不一致」这类 bug 的常见来源。
    """
    if max_items < 0:
        max_items = 0
    ranked = rank_anomalies(enumerate(anomalies))
    kept = tuple(anomaly for _, anomaly in ranked[:max_items])
    return kept, max(0, len(ranked) - max_items)


def normalize_anomalies(
    anomalies: Iterable[Anomaly],
    thresholds: RuleThresholds,
) -> NormalizeResult:
    """门槛过滤 → 证据裁剪 → 近似去重 → 封顶。

    顺序不是随意的：

    * **先过滤再裁剪**。给一条马上要被丢掉的异常裁证据是白做功，更糟的是让记账里
      出现「证据被裁了」但那条异常根本不存在，看 trace 的人会以为漏了什么。
    * **先去重再封顶**。反过来的话，两条重复项会各占一个名额，把别的条目挤掉，然后
      去重又只剩一条——最终报出的条数少于上限，却没有任何一条被记为「因超限被丢」。

    记账里的 `index` **始终是模型原始数组里的下标**（各阶段一路带下来）。否则同一份
    trace 里会出现两套编号，「第 3 条为什么被丢了」就没法回答。
    """
    kept: list[tuple[int, Anomaly]] = []
    dropped: list[DroppedItem] = []
    evidence_trimmed = 0

    for index, anomaly in enumerate(anomalies):
        if not thresholds.passes(anomaly):
            dropped.append(
                DroppedItem(
                    "anomaly",
                    index,
                    f"未达告警门槛（需要 severity≥{thresholds.min_severity} "
                    f"且 confidence≥{thresholds.min_confidence}）",
                    f"{anomaly.severity}/{anomaly.confidence}",
                )
            )
            continue
        trimmed, note = _trim_evidence(anomaly, index, thresholds.max_evidence)
        if note is not None:
            evidence_trimmed += 1
            dropped.append(note)
        kept.append((index, trimmed))

    unique: list[tuple[int, Anomaly]] = []
    duplicates_removed = 0
    for index, anomaly in kept:
        if any(
            is_probable_duplicate(anomaly, existing, threshold=thresholds.similarity_threshold)
            for _, existing in unique
        ):
            duplicates_removed += 1
            dropped.append(
                DroppedItem("anomaly", index, "与已有条目判定为同一问题（近似去重）", anomaly.title)
            )
            continue
        unique.append((index, anomaly))

    capped_items, capped = cap_anomalies((item for _, item in unique), thresholds.max_anomalies)
    if capped:
        max_items = max(0, thresholds.max_anomalies)
        # 逐条记账而不是记一条汇总：被砍掉的是哪几条要能查，否则用户看到「少报 3 条」
        # 也没法判断被砍的是不是关键那条。
        #
        # `detail` 里带上文件路径：这条记账不只是给 trace 看的，报告末尾那节
        # 「结论条数上限（平台补充）」用的就是它 —— 只给一个标题，用户没法回看是哪份表。
        for index, anomaly in rank_anomalies(unique)[max_items:]:
            detail = f"{anomaly.severity} {anomaly.title}"
            if anomaly.file_path:
                detail = f"{detail}（{anomaly.file_path}）"
            dropped.append(
                DroppedItem(
                    KIND_ANOMALY_CAP,
                    index,
                    f"超出本次上限（{max_items} 条），已按严重度优先保留",
                    detail,
                )
            )

    return NormalizeResult(
        anomalies=capped_items,
        dropped=tuple(dropped),
        duplicates_removed=duplicates_removed,
        capped=capped,
        evidence_trimmed=evidence_trimmed,
    )


# --------------------------------------------------------------------------
# 版本
# --------------------------------------------------------------------------


def rules_version() -> str:
    """规则层的版本标识（源码内容哈希）。

    每次分析开始时算一次。不缓存：本模块的源码在进程运行期间不会变，但缓存会让
    「改了规则重启后忘了更新版本」这一类调试变得困惑，而省下的只是一次十几 KB 的读。
    """
    digest = hashlib.sha1()
    base = Path(__file__).resolve().parent
    for name in RULE_SOURCE_FILES:
        digest.update(name.encode("utf-8"))
        try:
            digest.update((base / name).read_bytes())
        except OSError:
            # 打包成 zipapp / 冻结分发时源码可能不可读。此时退化成「只认文件名」，
            # 版本号在同一个构建里仍然稳定，只是跨构建不再区分——比抛异常好。
            digest.update(b"<unreadable>")
    return f"rules-{digest.hexdigest()[:12]}"
