"""把平台的 payload 转成引擎要的三样东西。

## 为什么单独一层

引擎（`engine.run_analysis`）要的是字符串 + `AnalysisScope`，而平台那边的 payload 是
两个**形状不同**的 dict：单提交模式给一个 `commit` 对象（带 message/author/时间），周版本
模式给一列 `delta_files`（每项只有 `latest_commit_id`，**没有 message**）。把这两种形状的
差异挡在这一层，引擎就只需要认识一种输入，两种模式的接线也不用各写一遍。

## 周版本模式的固有信息缺口（不掩饰）

`weekly_version_diff_cache` 只存 `latest_commit_id`，不存提交信息。所以周版本的变更清单
里**没有提交说明**。这不是可以就地补上的东西 —— 它需要回源到仓库，而那正是模型用
`commit_detail` 这项工具去做的事。所以这里如实留空，不编造一份「提交信息」出来。

## 表与生成物

`bundles` 的说明行会被拼进变更清单。但**平台只写它核实过的东西**：这一组之所以在一起，
唯一依据是「文件名里出现了同一个记号」。所以清单里那一段的小标题与措辞都是
「疑似、待确认」，不是「表与其生成物是同一次改动」—— 后者是**项目事实**，由项目
知识包自己写（G119 写在 `references/config-table-spec.md`）。识别规则与项目无关，
前缀由项目声明（见 `bundles.py` 与 `project_facts.py`）。

同时这里会把**本轮的配对结果**记一笔（`ChangeSet.bundle_note` 与一行日志）：
「0 组」既可能是「这个项目本来就没得配」，也可能是「我们根本不会配」，两者在
日志里必须分得开。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from services.ai.bundles import build_bundles, describe_bundles
from services.ai.manifest import ManifestPlan, build_manifest
from services.ai.prompt import CommitSummary, FileChange, render_change_summary
from services.ai.project_facts import DEFAULT_PREFIX_DECLARATION, PrefixDeclaration
from services.ai.scope import AnalysisScope, normalize_path
from utils.logger import log_print

# 变更清单里最多列几组「同记号关联」（疑似同一次改动的那些）。列太多会把清单本身挤长，
# 而它每一轮都在提示词里；剩下多少组由 `describe_bundles` 自己说明。
DEFAULT_BUNDLE_LIMIT = 12
# **只在「payload 里没有 manifest」时兜底**（老 payload、单提交模式）。有 payload 就用
# payload 那份：一次运行的提示词、落库账、`read_reference` 的 S 标签、成员归属必须读
# 同一个指纹，各自造一份的下场见 `manifest.py` 的抬头。
DEFAULT_MANIFEST_SHARDS = 3
MANIFEST_REFERENCE = "change-manifest"

_SCOPE_LABELS = {
    "full": "全量",
    "incremental": "增量（只含上次分析之后变化的部分）",
}


@dataclass(frozen=True)
class ChangeSet:
    """引擎的输入。"""

    # 给模型看的变更清单（已经渲染好，含省略说明与表的关联说明）。
    summary: str
    scope: AnalysisScope
    # 本次涉及的全部路径，规范化且去重，保持原有顺序。用于配对「表 ↔ 生成物」。
    paths: tuple[str, ...] = ()
    commits: tuple[CommitSummary, ...] = ()
    # 「文件名里有同一个记号」的那几组说明行（单文件单元不会出现）。
    bundle_lines: tuple[str, ...] = ()
    # 「本轮配对了几组、用的是哪个前缀、前缀是谁声明的」那一句话。
    # **它是一个结论字段，不是装饰**：没有它，「这个项目本来就没得配」与
    # 「平台根本不会配」在界面上、日志里都长得一模一样。
    bundle_note: str = ""
    manifest: ManifestPlan = ManifestPlan((), DEFAULT_MANIFEST_SHARDS, "")

    @property
    def is_empty(self) -> bool:
        return not self.commits


def from_commit_payload(
    payload: Mapping[str, object],
    *,
    readable_references: Iterable[str] = (),
    bundle_limit: int = DEFAULT_BUNDLE_LIMIT,
    prefixes: Optional[PrefixDeclaration] = None,
) -> ChangeSet:
    """单提交模式：一个提交、一个文件。"""
    commit = dict(payload.get("commit") or {})
    commit_id = str(commit.get("commit_id") or "")
    path = normalize_path(str(commit.get("path") or ""))

    commits: tuple[CommitSummary, ...] = ()
    if commit_id:
        commits = (
            CommitSummary(
                commit=commit_id,
                message=str(commit.get("message") or ""),
                author=str(commit.get("author") or ""),
                commit_time=str(commit.get("commit_time") or ""),
                files=(FileChange(path=path, operation=_operation(commit.get("operation"))),)
                if path
                else (),
            ),
        )

    return build(
        commits,
        readable_references=readable_references,
        scope_note=_scope_note(payload),
        bundle_limit=bundle_limit,
        prefixes=prefixes,
    )


def from_weekly_payload(
    payload: Mapping[str, object],
    *,
    readable_references: Iterable[str] = (),
    bundle_limit: int = DEFAULT_BUNDLE_LIMIT,
    prefixes: Optional[PrefixDeclaration] = None,
) -> ChangeSet:
    """周版本模式：一列 delta 文件，按 `latest_commit_id` 归到各自的提交下。

    同一个提交下可能有多个文件（同一个提交改了多张表），所以这里按提交分组，而不是
    一个文件一个提交 —— 否则变更清单里会出现同一个提交号重复十几次。

    ## 两个清单不是一回事

    * `delta_files` = 本批次**全部**改动过的文件 → 决定**白名单**（模型能读哪些 diff）。
    * `list_files`  = 提示词里**列出来**的那部分 → 决定模型**看得见哪些名字**。

    以前它们是同一份：取样 200 个，既是清单也是白名单，于是没列出来的 567 个文件连
    `file_diff` 都会被拒 —— 模型说「还有 567 个没看到」时，它其实**连读都不允许**。
    现在名字没列全只是「不知道路」，不是「读不到」：白名单给全部，模型可以用
    `commit_detail` 查出某个提交的完整文件清单再点名索取。
    """
    grouped: dict[str, list[FileChange]] = {}
    for item in payload.get("list_files") or payload.get("delta_files") or []:
        entry = dict(item or {})
        commit_id = str(entry.get("latest_commit_id") or "")
        path = normalize_path(str(entry.get("file_path") or ""))
        if not commit_id or not path:
            continue
        grouped.setdefault(commit_id, []).append(FileChange(path=path, operation="M"))

    commits = tuple(
        CommitSummary(commit=commit_id, files=tuple(files))
        for commit_id, files in grouped.items()
    )

    # 白名单：本批次全部改动过的文件。缺失时（老 payload / 单提交模式）退回清单本身。
    whitelist: dict[str, list[str]] = {}
    for item in payload.get("delta_files") or []:
        entry = dict(item or {})
        commit_id = str(entry.get("latest_commit_id") or "")
        path = normalize_path(str(entry.get("file_path") or ""))
        if commit_id and path:
            whitelist.setdefault(commit_id, []).append(path)

    # 截断说明由 `render_change_summary` 统一写（它会说清「没列出来但可以索取」），
    # 这里不再重复一句同义的话 —— 两处各写一半的后果是改一处漏一处。
    #
    # 真实总数：**`summary.batch_files`**（这次装进输入的那一批）优先，缺值时退
    # `summary.total_files`，再缺值用**白名单的条数**（`delta_files` 就是本批次全部
    # 改动文件，所以它的条数也是本批总数）。
    #
    # 为什么不能用 `total_files`：它是**窗口总数**（这个周版本一共改了多少文件），
    # 而 `scope=incremental` 时输入里只有水位线之后变化的那一部分 —— 实测 847 vs 19。
    # 拿窗口总数当「本次变更的文件共 N 个」，下面那句「还有 M 个的名字没列出来，但
    # 你可以读到它们的 diff」就变成了假话：那 M 个根本不在白名单里，模型照它去点名
    # 索取时请求被 `protocol` 静默丢掉（只进 trace，不给模型任何回执），于是它把一个
    # **不存在**的取数缺口写进报告。
    #
    # **也不能在缺值时退回「清单长度」**：那正是「把清单当全量」的老毛病，而截断说明
    # 要不要写、写多少，全看这个数。
    whitelist_total = sum(len(paths) for paths in whitelist.values())
    summary = payload.get("summary") or {}
    declared_total = _positive_int(summary.get("batch_files"))
    if declared_total is None:
        declared_total = _positive_int(summary.get("total_files"))
    total_files = declared_total if declared_total is not None else (whitelist_total or None)

    # **落库那份优先**：`payload["manifest"]` 是按配置的成员数（`subagent_count`）算的，
    # 而提示词、`read_reference` 的 S 标签、成员实际分到的文件都从这一份出发。这里若各造
    # 一份（原先写死 3 个分片），模型读到的 S 标签就与它分到的文件对不上 —— 默认 3 时两个
    # 数巧合相等，所以这个 bug 只在 `subagent_count != 3` 时现形。缺这份键（老 payload）
    # 时才退回平台默认分片数。
    persisted = ManifestPlan.from_dict(payload.get("manifest"))
    manifest = (
        persisted
        if persisted.entries
        else build_manifest(
            payload.get("delta_files") or (),
            shard_count=DEFAULT_MANIFEST_SHARDS,
        )
    )
    return build(
        commits,
        readable_references=readable_references,
        scope_note=_scope_note(payload),
        bundle_limit=bundle_limit,
        total_files=total_files,
        whitelist=whitelist or None,
        prefixes=prefixes,
        manifest=manifest,
        extra_inputs=_extra_inputs(payload.get("delta_files") or ()),
    )


def build(
    commits: Sequence[CommitSummary],
    *,
    readable_references: Iterable[str] = (),
    scope_note: str = "",
    bundle_limit: int = DEFAULT_BUNDLE_LIMIT,
    total_files: Optional[int] = None,
    whitelist: Optional[Mapping[str, Iterable[str]]] = None,
    prefixes: Optional[PrefixDeclaration] = None,
    manifest: Optional[ManifestPlan] = None,
    extra_inputs: Sequence[tuple[str, str]] = (),
) -> ChangeSet:
    """渲染清单并算出白名单范围。两种模式共用。

    `whitelist` 给白名单一个**独立于清单**的来源（提交号 → 该提交改动过的全部路径）。
    不传时白名单就是清单里那些文件（单提交模式的正常情形）。两者分开是必须的：
    清单可以为了省字符而只列一部分，而白名单少一个路径，模型就**彻底读不到**那个文件。

    `prefixes` 是**项目声明的生成物前缀**（不传时用平台默认值）。配对规则本身与项目
    无关，但「产物叫什么前缀」是项目事实，见 `project_facts.py`。

    `extra_inputs` 是**本轮输入里不是本轮改动的那几条**（`(路径, 来源)`，来源取值
    `compensation` / `dependency`）。它们会单独成一小节逐条列出 —— 只给一句计数时，
    模型分不清哪个文件不是本轮改的，报告里就会把补偿项当成新变更。
    """
    ordered = tuple(commits)
    rendered_paths = _collect_paths(ordered)

    if whitelist is None:
        resolved_whitelist: Mapping[str, Iterable[str]] = {
            commit.commit: [change.path for change in commit.files if change.path]
            for commit in ordered
            if commit.commit and commit.files
        }
    else:
        resolved_whitelist = whitelist

    # 「表 ↔ 生成物」的配对按**白名单**（全部改动）来算，而不是按列出来的那部分：
    # 配对的另一半常常是没被列进清单的那个文件，只按清单配对等于把最容易出问题的一对
    # 拆开（表改了、产物没跟上，正是要靠配对才看得见）。
    paths = _collect_whitelist_paths(resolved_whitelist) or rendered_paths

    declaration = prefixes or DEFAULT_PREFIX_DECLARATION
    bundles = build_bundles(paths, generated_prefixes=declaration.prefixes)
    bundle_lines = tuple(describe_bundles(bundles, limit=bundle_limit))
    pair_count = sum(1 for bundle in bundles if bundle.is_multi)
    # 每个项目都记一笔（一次分析一行，不是每轮一行）：这一行是「0 组是因为项目本来
    # 就没得配，还是因为平台不会配」的唯一出口。声明坏掉时一并见光。
    bundle_note = declaration.describe(pair_count)
    log_print(bundle_note + (f"；{declaration.warning}" if declaration.warning else ""), "AI")

    body = render_change_summary(ordered, total_files=total_files)
    if scope_note:
        body = f"{scope_note}\n\n{body}"
    if bundle_lines:
        # 小标题与措辞**只写平台核实过的事实**：平台看到的是「文件名里有同一个记号」，
        # 它没有核实这些文件之间是什么关系（在别的项目里 `Item.csv` 与脚本 `Item.py`
        # 也会凑成一对）。所以这里是「疑似、请确认」，不是「这是一件事」。
        body += (
            "\n## 文件名疑似相关的改动（平台按名字推断，未经核实）\n\n"
            + "\n".join(bundle_lines)
            + "\n\n上面这些关联的唯一依据是**文件名里出现了同一个记号**（例如都含 "
            "`CfgItem`）。平台**没有核实**它们之间是什么关系，也没有核实它们是否真的"
            "属于同一次改动。请把它们当成「值得一起看一眼」的线索：**先确认**，"
            "再据此推理。确认不了就按本批次实际改了什么如实写，不要把它当成前提。\n"
        )

    resolved_manifest = manifest or build_manifest(
        (
            {"latest_commit_id": commit_id, "file_path": path}
            for commit_id, paths_ in resolved_whitelist.items()
            for path in paths_
        ),
        shard_count=DEFAULT_MANIFEST_SHARDS,
    )
    manifest_summary = resolved_manifest.summary()
    # 这一段是**模型知道「完整清单在哪、怎么翻」的唯一出口**：`SKILL.md` 的可读文档清单
    # 里没有 `change-manifest`，平台提示词的可读文档索引读的也不是 `scope.readable_references`。
    # 不写这一句，`read_reference` 那条分页通路就是「存在但没人知道」。
    body += (
        "\n## 确定性文件分工\n\n"
        f"完整 manifest 共 {manifest_summary['total']} 个文件，"
        f"已分配 {manifest_summary['assigned']} 个（{manifest_summary['assigned_rate']:.1%}）；"
        f"分配指纹 `{manifest_summary['assignment_digest'][:12]}`"
        f"（分片数 {resolved_manifest.shard_count}）。"
        "**上面这份清单只是取样，完整的那一份可以用 `read_reference` 读 `change-manifest`**："
        "它按小节点名分段，**第 N 节就是第 N 页（每页 100 个文件）**，"
        '`lines` 写页号即可点名（例如 `lines`: `"3"` 拿第 3 页）。'
        + _assignment_note(resolved_manifest)
    )
    if extra_inputs:
        body += _extra_inputs_section(extra_inputs)

    return ChangeSet(
        summary=body,
        scope=AnalysisScope(
            commits=tuple(
                commit_id for commit_id, _ in resolved_whitelist.items() if commit_id
            ),
            paths_by_commit={
                commit_id: frozenset(paths_)
                for commit_id, paths_ in resolved_whitelist.items()
                if commit_id
            },
            readable_references=frozenset(
                [str(name) for name in readable_references if name] + [MANIFEST_REFERENCE]
            ),
        ),
        paths=paths or rendered_paths,
        commits=ordered,
        bundle_lines=bundle_lines,
        bundle_note=bundle_note,
        manifest=resolved_manifest,
    )


def _assignment_note(manifest: ManifestPlan) -> str:
    """「分工」那一段的落款。**单代理运行时不许说「分片先检查自己的文件」** ——
    那时没有分片，也没有那份任务书，这句话会让模型去找一个不存在的东西，或者以为
    自己只该看一部分文件。

    为什么要留那半句条件句：`shard_count` 是按**配置的成员数**算的，而「这一次真的拆没拆」
    由 `plan_family` 决定（子代理没开、维度不足 2 个时不拆）。change_set 拿不到 plan（它
    在 `build()` 之后才算），所以分片那支写的是**条件句**：无论这一次是真拆了还是没拆，
    读起来都是真话。
    """
    if manifest.shard_count > 1:
        return (
            "如果你是被分配了文件的某个分片代理，先检查分到你的那些文件（见任务书里的"
            "「确定性文件分工」）；**单代理运行时没有分片**，清单里每个文件都在你的检查范围内。"
            "跨文件证据始终可以读取整个白名单。\n"
        )
    return (
        "本次没有分片（一个分析代理负责全部文件）：清单里每个文件都在你的检查范围内。"
        "跨文件证据可以读取整个白名单。\n"
    )


def _extra_inputs(rows: Iterable[Mapping[str, object]]) -> tuple[tuple[str, str], ...]:
    """本轮输入里 `source != delta` 的那些 → `(路径, 来源)`，按原顺序去重。

    来源只有两种（`scope_sampling.build_weekly_payload` 写的）：`compensation`（上一轮
    装进了输入却没取到证据）与 `dependency`（与本轮变更文件同名的源表 / 生成物）。
    """
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for item in rows or ():
        entry = dict(item or {})
        source = str(entry.get("source") or "delta").strip() or "delta"
        path = normalize_path(str(entry.get("file_path") or ""))
        if source == "delta" or not path or path in seen:
            continue
        seen.add(path)
        result.append((path, source))
    return tuple(result)


def _extra_inputs_section(rows: Sequence[tuple[str, str]]) -> str:
    """补偿 / 依赖核查项**逐条**列出来（只有一句计数时，模型不知道是哪几条）。"""
    listed = "\n".join(f"- `{path}`（source={source}）" for path, source in rows)
    return (
        "\n## 本轮输入中的补偿/依赖核查项\n\n"
        f"这一轮有 {len(rows)} 个文件**不是本轮新增改动**，是平台按来源挑进来的核查项：\n\n"
        f"{listed}\n\n"
        "**补偿项**（`source=compensation`）是上一轮装进了输入、却一个证据都没取到的文件；"
        "**依赖项**（`source=dependency`）是与本轮变更文件同名的源表或生成物（平台只按文件名"
        "推断，**没有核实**它们之间是什么关系）。报告里必须把它们与「本轮改了什么」分开写，"
        "并单独标明来源。\n"
    )


def _collect_whitelist_paths(whitelist: Mapping[str, Iterable[str]]) -> tuple[str, ...]:
    """白名单里的全部路径，规范化 + 去重 + 保持先后顺序（顺序稳定性同 `_collect_paths`）。"""
    seen: set[str] = set()
    result: list[str] = []
    for paths in whitelist.values():
        for raw in paths:
            path = normalize_path(str(raw or ""))
            if not path or path in seen:
                continue
            seen.add(path)
            result.append(path)
    return tuple(result)


def _collect_paths(commits: Iterable[CommitSummary]) -> tuple[str, ...]:
    """全部路径，规范化 + 去重 + 保持先后顺序。

    顺序稳定是有意为之：`build_bundles` 的输出顺序跟着它走，而这份清单每轮都进提示词，
    顺序飘忽会让「这一轮和上一轮差在哪」变成不可读的。
    """
    seen: set[str] = set()
    result: list[str] = []
    for commit in commits:
        for change in commit.files:
            path = normalize_path(str(change.path or ""))
            if not path or path in seen:
                continue
            seen.add(path)
            result.append(path)
    return tuple(result)


def _operation(value: object) -> str:
    """把操作类型收敛到 A/M/D。认不出来的一律按 M（修改）——**不猜成删除或新增**：
    猜错方向会让模型按错误的前提推理。"""
    text = str(value or "").strip().upper()
    return text if text in ("A", "M", "D") else "M"


def _positive_int(value: object) -> Optional[int]:
    """转成正整数；转不出来或 <= 0 时返回 `None`（0 与缺值同义：这条数没得用）。"""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _nonneg_int(value: object) -> Optional[int]:
    """转成非负整数；**读不出来（含缺键）时返回 `None`**。

    与 `_positive_int` 只差一处，但那一处很要命：`_positive_int` 把 0 与缺值都当 `None`，
    于是「这轮 0 个补偿项」与「这份 payload 里根本没有这个概念」被写成同一句话。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _scope_note(payload: Mapping[str, object]) -> str:
    scope = str(payload.get("scope") or "")
    label = _SCOPE_LABELS.get(scope)
    note = f"本次分析范围：{label}。" if label else ""

    # **范围判定（full / incremental）说的是分析口径，不是「输入覆盖了整个版本」。**
    # `_decide_scope` 在「命中关键路径」「本批占比大」这几种情形下也回 `full`，而那几
    # 种情形下输入**仍然是水位线之后的那一部分**（判定只改标签，不会再查一次全量）。
    # 不写清这一点，模型读到「本次分析范围：全量」就会对「本版本没问题」下结论 ——
    # 而窗口里另外那些文件它一个都没看过。
    summary = payload.get("summary") or {}
    window = _positive_int(summary.get("window_files"))
    batch = _positive_int(summary.get("batch_files"))
    if window and batch and batch < window:
        if label:
            note = f"本次分析范围：{label}（**这是分析口径，不等于「输入装了整个版本」**）。"
        note += (
            f"**这次输入覆盖的是本版本改动过的 {window} 个文件里的 {batch} 个**"
            f"（其余 {window - batch} 个在上次分析时就已在窗口里，这次没有重新给）。"
            "报告里不要把它说成「本版本整体没问题」。"
        )
    extras: list[str] = []
    compensation = _nonneg_int(summary.get("compensation_files"))
    dependency = _nonneg_int(summary.get("dependency_files"))
    if compensation is None and dependency is None:
        # 老 payload 里没有这两个键：**一个字都不说**（「没记录」不该编成「0 个」）。
        pass
    elif not compensation and not dependency:
        # **0 与「没有这个概念」要分得开**：两个都是 0 时说清「这轮输入就是新增改动本身」，
        # 而缺键时上面那支什么都不说 —— 原先两者都表现为「不提」，读的人分不出来。
        note += "本次输入里没有补偿项与依赖核查项（两者都是 0 个）：这一轮的输入就是本批次的新增改动。"
    else:
        if compensation:
            extras.append(f"{compensation} 个上轮未覆盖补偿项")
        elif compensation == 0:
            extras.append("0 个上轮未覆盖补偿项")
        else:
            extras.append("上轮未覆盖补偿项的数量未记录")
        if dependency:
            extras.append(f"{dependency} 个依赖核查项")
        elif dependency == 0:
            extras.append("0 个依赖核查项")
        else:
            extras.append("依赖核查项的数量未记录")
        note += "本次输入还包含" + "、".join(extras) + "；它们不是本轮新增改动，报告须单独标明来源。"
    return note + _focus_note(payload)


def _focus_note(payload: Mapping[str, object]) -> str:
    # 用户选的分析范围（只看配表仓库 / 只看某个仓库…）。**必须写进提示词**：不写的话
    # 模型以为自己看到的就是整个版本，会把「这个范围内没发现问题」说成「本版本没问题」。
    focus = payload.get("focus")
    focus_label = str((focus or {}).get("label") or "") if isinstance(focus, Mapping) else ""
    if not focus_label:
        return ""
    return (
        f"**本次只分析了{focus_label}**，其它仓库的改动不在这次输入里 —— "
        "报告里要写明这一点，不要把结论说成覆盖了整个版本。"
    )
