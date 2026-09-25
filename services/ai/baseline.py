"""累积基线：把「上一次为止的结论」带进下一轮分析。

## 为什么需要它

版本周期里同一个版本会被分析十几次（自动轮询、手动补跑、新提交触发）。此前的做法只让
**变更集**增量化（`scope=incremental` 时只把 delta 文件送给模型），但模型对上一次的结论
一无所知。后果有三条，第三条最要命：

1. 跨提交的判断做不了 —— 新提交推翻了上次关于旧提交的结论，没人会发现。
2. 报告是「每次一份」而不是「这个版本截至目前一份」，QA 要拼 N 份才看得出全貌。
3. **旧问题被反复重报，而人工已经把它们判成「忽略」了** —— 每次分析都把 QA 的分诊成果
   作废一遍。这是增量评审用不下去的根本原因。

## 三条行为约定（都是有意的）

* **累积一份**：基线是「这个版本截至目前的所有结论」，每次增量在它上面增删改。
* **已忽略的默认不再提**：`disposition == ignored` 且相关文件没有再变时，不进基线摘要。
  尊重人工判断，而不是每轮再问一次。
* **文件再变则重新确认**：某条结论涉及的**文件**又被改动时，它的证据已经过期，状态置为
  `needs_recheck` 并**重新出现在基线里**（已忽略的也一样）。这是「忽略」不会变成
  「永远看不见」的保证。

## 纯函数

本模块不读文件、不碰数据库：输入是一组结论值，输出是渲染好的文本与分组结果。基线要不要
生效、哪些文件算「又变了」，由调用方（引擎）决定后传进来。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence, Tuple

from services.ai.scope import normalize_path
from services.ai.skill_contract import SEVERITIES

# 与 `models.ai_analysis.anomaly.DISPOSITIONS` 必须一致。
# 这里**刻意不 import 模型层**：本模块要能在没有 app 上下文的情况下被完整单测，
# 而模型层会拉进 SQLAlchemy。两边一致性由 `test_ai_baseline.py` 里的防漂移用例守着。
DISPOSITION_PENDING = "pending"
DISPOSITION_CONFIRMED = "confirmed"
DISPOSITION_IGNORED = "ignored"
DISPOSITIONS = (DISPOSITION_PENDING, DISPOSITION_CONFIRMED, DISPOSITION_IGNORED)

# 结论在基线上的状态。
STATE_OPEN = "open"  # 仍成立，等着处理
STATE_NEEDS_RECHECK = "needs_recheck"  # 相关文件又变了，证据过期，要重新确认
STATE_SUPPRESSED = "suppressed"  # 人工已忽略且没有新变化，不再出现

# 基线摘要的字符上限。它和上下文条目**抢同一份 prompt 预算**，所以不能放任它长大；
# 超限时按「先丢最不要紧的」压缩，并明确写出省略了多少（同 `budget.py` 的原则）。
DEFAULT_BASELINE_CHARS = 6_000

# 运行账上 `baseline.reason` 的取值之一：**用户显式点了全量**。
#
# 只有这一个取值会让「旧结论不进模型输入」（判据在 `baseline_source.run_ignores_history`）。
# 它**定义在这里**（而不是在写它的 `baseline_blocks.py`）是因为本模块是叶子：写侧与读侧
# 都能 import 它而不产生环，两处各写一个字面量迟早会漂。
FORCE_FULL_REASON = "force_full"

# 运行账上 `baseline.reason` 的**第二个**取值：**平台自己决定的全量重看**。
#
# 两个 `force_full` 的**语义不同**，所以账上必须分得开（真机 run 68 的实测）：
#
#   * 用户显式点全量 → `FORCE_FULL_REASON`：语义是**从零重判**，旧结论不该进模型输入
#     （理由见 `baseline_source` 的模块 docstring，那里记着一次真实的注入事故）；
#   * 输入没变、而上一轮结论不可复用 → **这个**：用户点的是**增量**，平台只是发现
#     「拿旧快照做差没有意义了」，于是把这份内容**重新看一遍**。旧结论在这件事上
#     一条都没失效 —— 报告仍然是「这个版本截至目前的那一份」，那批问题仍然要带上。
#
# run 68 的形态：`baseline = {"kind":"none","reason":"force_full"}`、快照与上一轮**同一份**
# （输入一字未变）、`基线继承 0 条` —— 报告退化成首跑，模型手里一份历史清单都没有。
# 根因就是这里只有一个取值：平台自己升的那次，借用了「用户点了全量」的语义。
FORCE_FULL_REBUILD_REASON = "force_full_rebuild"

_STATE_ORDER = (STATE_NEEDS_RECHECK, STATE_OPEN, STATE_SUPPRESSED)

# 短标签用于计数行与分组标题，补充说明只在有内容时才拼在后面（不拼成双层括号）。
_STATE_LABELS = {
    STATE_NEEDS_RECHECK: "需要重新确认",
    STATE_OPEN: "仍待处理",
    STATE_SUPPRESSED: "已忽略",
}
_STATE_NOTES = {
    STATE_NEEDS_RECHECK: "相关文件又变了，证据已过期",
    STATE_OPEN: "",
    STATE_SUPPRESSED: "默认不再提，文件再变时会重新出现",
}


def _render_heading(state: str, count: int) -> str:
    head = f"## {_STATE_LABELS[state]}"
    note = _STATE_NOTES[state]
    if note:
        head += f" · {note}"
    return f"{head}（{count} 条）"


@dataclass(frozen=True)
class BaselineFinding:
    """一条历史结论在基线里的样子。

    只带「模型需要用来对照」的字段：指纹用于判重，标题与严重度用于对照，
    文件用于失效判定。**不带长正文**（证据、建议、影响面）—— 那些留给上一轮的报告，
    基线里放它们会吃掉预算，而模型只需要知道「这条已经报过」。
    """

    fingerprint: str
    title: str
    severity: str = "high"
    category: str = ""
    file_path: str = ""
    commit_ref: str = ""
    disposition: str = DISPOSITION_PENDING
    state: str = STATE_OPEN

    @property
    def suppressed(self) -> bool:
        return self.state == STATE_SUPPRESSED


def classify(
    findings: Iterable[BaselineFinding],
    *,
    changed_paths: Iterable[str] = (),
) -> Tuple[BaselineFinding, ...]:
    """按「相关文件有没有又变」定出每条结论的状态。

    `changed_paths` 是**本轮之前已经分析过、这次又变了**的文件（引擎从 delta 里算）。
    路径比较走 `normalize_path`，避免 `./a/b.xlsx` 与 `a/b.xlsx` 被当成两个文件。
    """
    changed = {normalize_path(path) for path in changed_paths if str(path or "").strip()}
    result = []
    for finding in findings:
        path = normalize_path(finding.file_path) if finding.file_path else ""
        if path and path in changed:
            state = STATE_NEEDS_RECHECK
        elif finding.disposition == DISPOSITION_IGNORED:
            state = STATE_SUPPRESSED
        else:
            state = STATE_OPEN
        result.append(replace(finding, state=state))
    return tuple(result)


def suppressed_fingerprints(findings: Iterable[BaselineFinding]) -> frozenset:
    """不该再出现在报告与异常清单里的指纹。

    只包含「人工已忽略**且**没有新变化」的那些：文件又变了的会带上 `needs_recheck`，
    不在此列 —— 否则「忽略」就变成了「永远看不见」。
    """
    return frozenset(item.fingerprint for item in findings if item.suppressed)


def partition_incoming(
    previous: Iterable[BaselineFinding],
    incoming: Iterable[BaselineFinding],
) -> Tuple[Tuple[BaselineFinding, ...], Tuple[BaselineFinding, ...]]:
    """把本轮模型报出来的结论分成 `(真正新增的, 已知的)`。

    判重按指纹。**已知的那部分不算新增**：模型重复报一条旧问题，不代表版本里多了一个
    问题，把它算进「本次新增 N 条」会让计数一路虚高，也看不出它其实是老问题。
    """
    known = {item.fingerprint for item in previous}
    fresh: list[BaselineFinding] = []
    already: list[BaselineFinding] = []
    for item in incoming:
        (already if item.fingerprint in known else fresh).append(item)
    return tuple(fresh), tuple(already)


def build_baseline_digest(
    findings: Iterable[BaselineFinding],
    *,
    max_chars: int = DEFAULT_BASELINE_CHARS,
    note: str = "",
) -> str:
    """渲染给模型看的基线摘要。**入参必须是 `classify()` 的输出。**

    这里**刻意不接收 `changed_paths`、也不再判一次状态**。原因是一个踩过的坑：调用方
    先 `classify(..., changed_paths=...)` 算出状态（要拿 `suppressed_fingerprints` 去过滤
    报告，也要看有几条要重新确认），再交给本函数渲染；如果本函数拿着一份缺省的
    `changed_paths` 又判一遍，那条刚刚因为「文件又变了」而复活（`needs_recheck`）的结论
    就会被重新判回 `suppressed`，**从摘要里消失** —— 也就是「忽略」又变回了「永远看不见」，
    正是这个模块存在的意义所在。而且它是静默的：报告里少一条，没人会注意到。

    所以「哪些文件又变了」只从 `classify()` 这一个入口进来。渲染只管排版。

    结构固定（需要重新确认 → 仍待处理 → 已忽略），**同一批输入永远产出同一段文本**：
    它每轮都在提示词里，顺序飘忽会让「上一轮和这一轮差在哪」变成不可读的。

    超长时按组从后往前丢（先丢已忽略，再丢待处理），并在开头写明省略了多少 ——
    静默截断会让模型以为自己看到了全部历史结论。

    `note` 是调用方要额外交代的一句话（目前只有一种：这份清单**不是**最近那次分析留下的，
    因为最近那次一条结构化结论都没给出）。放在抬头之后、条目之前 —— 它修饰的是整份清单，
    读到第一条结论时就已经该知道。本函数不认识它的内容，只负责把它排在正确的位置。
    """
    classified = tuple(findings)
    groups = {
        state: [item for item in classified if item.state == state] for state in _STATE_ORDER
    }

    header = _render_header(classified, groups)
    tail = f"\n{note.strip()}\n" if note.strip() else ""
    if not any(groups.values()):
        return header + "\n（暂无历史结论：这是这个版本的第一次分析。）\n" + tail

    kept, omitted = _fit_groups(groups, max_chars - len(header))
    lines = [header]
    if tail:
        lines.append(tail.strip())
    if omitted:
        lines.append(
            f"（为控制长度，{omitted} 条较早的结论没有列出。"
            "需要的可以重新索取 —— 你看到的不是全部历史。）"
        )
    for state in _STATE_ORDER:
        entries = kept.get(state) or []
        if not entries:
            continue
        lines.append("")
        lines.append(_render_heading(state, len(entries)))
        for item in entries:
            lines.append(_render_entry(item))
    return "\n".join(lines).rstrip() + "\n"


def _render_header(
    classified: Sequence[BaselineFinding],
    groups: Mapping[str, Sequence[BaselineFinding]],
) -> str:
    counts = "、".join(
        f"{_STATE_LABELS[state]} {len(groups[state])}" for state in _STATE_ORDER if groups[state]
    )
    return (
        "# 这个版本截至上次分析已经报过的问题\n"
        f"共 {len(classified)} 条：{counts or '无'}。\n"
        "这些是**已知问题，不要再当成「本轮新发现」**；但它们**必须出现在「风险评估」那一节"
        "里**并标出「（上次遗留，仍成立）」—— 那一节是**当前仍成立的问题全集**，不是本轮"
        "增量。逐条给出它现在的状态：仍成立 / 已修复 / 已被推翻（说明依据）。"
        "**旧结论与「本轮确认」用同一种写法**（`R#（等级，模块）现象与后果`，整节连续编号），"
        "但只许用下面清单里已有的信息（标题、等级）——**不要为它补写证据或位置**，它本轮"
        "没有被重新取证，补出来的「证据」是编的。"
        "下面每条开头的 `[critical]` / `[high]` 是**平台内部的码值**，写进正文时一律写成"
        "中文（严重 / 高）。"
        "标为「已忽略」的默认不要再提，除非你发现它正是因为这次改动而重新成立的。\n"
        "**每条末尾的 `#…` 是「问题指纹」（16 位十六进制）**，只用来让你逐条对照"
        "「这是不是上次那一条」—— 它**不是可以读取的证据地址**，不要拿它去发 "
        "`evidence` 请求。要复核某条旧结论的原文，按它的 `文件路径` 与 `提交` 重新取证。"
    )


def _render_entry(item: BaselineFinding) -> str:
    where = f" ({item.file_path})" if item.file_path else ""
    commit = f" @{item.commit_ref[:12]}" if item.commit_ref else ""
    return f"- [{_prompt_severity(item.severity)}] {item.title}{where}{commit} #{item.fingerprint}"


# 平台赋值的等级 → 写进**提示词**的那个等级。
#
# 这两个键来自 `verdict.SEVERITY_STEP_DOWN` 的**像集减去模型的闭集**（口径①：「证据不足」
# 降一档，`critical` → `high` → `medium` → `low`）。有测试钉着这个覆盖关系（
# `tests/test_ai_verdict_rules_evidence.py`），加一档就要在这里补一行。
#
# **刻意不 import `verdict`**：本模块是「累积基线」这一层，`verdict` 是裁决那一层，
# 两者没有调用关系，为两个字符串拉一条依赖不划算。反过来，`SEVERITIES`（模型的闭集）
# 是**真的**要按它判，所以 import 它 —— 那是纪律本身，不是这一层的私有知识。
_PROMPT_SEVERITY_FOLD = {"medium": "high", "low": "high"}


def _prompt_severity(severity: str) -> str:
    """写进**下一轮提示词**的等级：平台赋值的等级折回模型能写的那个闭集。

    ## 为什么必须折

    这一份渲染的是「这个版本截至上次分析已经报过的问题」，模型会**照抄它看到的等级**。
    口径①让「证据不足」的结论降一档（`critical` → `high` → `medium`），而 `medium` / `low`
    是**平台赋值**的等级 —— 模型自己不许写它们（`skill_contract.SEVERITIES` 只给
    `critical` / `high`，那是给模型的闭集，`protocol` 会按「severity 不在允许集合内」
    把越界的条目**丢掉**）。真把 `[medium]` 写进去，下一轮就有一条结论静默消失。

    ## 折的只是**这一个字段**，不是掩盖降档

    裁决节（给人看的）、`final_findings`、异常表、导出读的仍然是平台降出来的**真实等级**
    （`medium`）。两个读者、两处口径：**给模型看的**要落在它能写的集合里，**给人看的**
    要如实说降到了哪一档。所以这里不写「medium 其实还是 high」这类话，也不改上游的值。

    不在折回表里的（空串、以及将来可能出现的别的值）**原样返回**：这一层不认识它，
    就不许替它编一个等级（编出来的等级在提示词里看不出任何异常）。
    """
    text = str(severity or "").strip().lower()
    if text in SEVERITIES:
        return text
    return _PROMPT_SEVERITY_FOLD.get(text, text)


def _fit_groups(
    groups: Mapping[str, Sequence[BaselineFinding]],
    budget: int,
) -> Tuple[dict, int]:
    """从最不要紧的组开始丢，直到装进 `budget`。返回 `(保留的组, 丢掉的条数)`。

    丢的顺序是「已忽略 → 仍待处理 → 需要重新确认」的**反向**：已忽略本来就默认不再提，
    丢掉它损失最小；需要重新确认的是本轮最该看的东西，最后才动。
    """
    kept = {state: list(groups.get(state) or []) for state in _STATE_ORDER}
    omitted = 0
    for state in reversed(_STATE_ORDER):
        while kept[state] and _size(kept) > budget:
            kept[state].pop()
            omitted += 1
    if _size(kept) > budget:
        # 只剩「需要重新确认」还是超长：整组丢到这个上限内，并如实记账。
        while kept[STATE_NEEDS_RECHECK] and _size(kept) > budget:
            kept[STATE_NEEDS_RECHECK].pop()
            omitted += 1
    return kept, omitted


def _size(kept: Mapping[str, Sequence[BaselineFinding]]) -> int:
    return sum(len(_render_entry(item)) + 1 for entries in kept.values() for item in entries)
