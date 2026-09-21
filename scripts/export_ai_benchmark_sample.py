#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从生产库里**只读**导出一个 AI 分析运行，做成评测样本（默认脱敏）。

    # 先看看有哪些运行可以导
    python scripts/export_ai_benchmark_sample.py --list-runs --limit 20

    # 导出 Run 20（默认脱敏），顺带生成一份待人工填写的金标骨架
    python scripts/export_ai_benchmark_sample.py --run-id 20 \\
        --out benchmarks/ai_analysis/data/run20

    # 明确不要脱敏（只在本机私有目录里用）
    python scripts/export_ai_benchmark_sample.py --run-id 20 --out /tmp/run20 --no-redact

## 三条硬约束

1. **只读。** 连接串是 `file:<db>?mode=ro`，全程没有一条写语句。生产库上可能正跑着
   真实分析（复测文档第 6 节），导出工具绝不允许成为它的第二个写入方。
2. **默认脱敏。** 仓库路径、提交号、仓库名/URL、以及**散落在自由文本里的**路径
   （工具标签 `file_diff <commit> <path>`、证据正文）都要换掉 —— 只换结构化字段
   而漏掉正文里的路径，等于没脱敏。`--no-redact` 是显式开关，不脱敏时会在样本里
   落 `redacted: false`，A/B 报告也会带上这一条。
3. **假名必须稳定。** 假名 = `sha1(salt + 原值)` 的前 12 位（路径保留扩展名）。
   同一条路径在任何一次导出里都映射到同一个假名 —— 否则两次跑之间做差、算覆盖率
   全都对不上。salt 默认空（跨机器可比）；路径本身就是敏感信息时用 `--salt`，
   但**之后所有导出必须用同一个 salt**。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from benchmarks.ai_analysis.dataset import (  # noqa: E402
    FINDING_ROW_FIELDS,
    MANIFEST_SCHEMA,
    RUN_ROW_FIELDS,
    TRACE_ROW_FIELDS,
    BenchmarkSample,
    GoldLabel,
    normalize_path,
)
from benchmarks.ai_analysis.metrics import (  # noqa: E402
    EVIDENCE_SCOPE_LEDGER_BY_PAIR,
    EVIDENCE_SCOPE_TRACE_DETAIL,
)
from benchmarks.ai_analysis.profile import (  # noqa: E402
    coverage_from_traces,
    evidence_files_from_traces,
    record_like,
    stage_rows_from_payload,
)

DEFAULT_DB_RELATIVE = os.path.join("instance", "diff_platform.db")

# 从 request_payload 里可能装着「本次要看的文件」的键名。顺序即优先级：
# `delta_files` 是**冻结下来的变化清单**（Run 20 里是 1009 个，正是复测文档里
# 「严格证据覆盖 42/1009」的那个分母），`list_files` 是采样后交给模型的窗口。
_CHANGE_FILE_KEYS = (
    "delta_files", "change_files", "changed_files", "files", "batch_files",
    "window_files", "planned_files", "list_files", "manifest", "file_list",
)

# 结构化脱敏用的键名分类。**按语义脱敏，不只靠正则扫文本** ——
# `repository_name: "qz_luaworkspace代码"` 这种值既不是路径也没有扩展名，正则扫不到，
# 但它明明白白是公司内部信息。
_PATH_KEYS = (
    "file_path", "path", "file", "local_path", "root_directory", "workdir",
    "config_path", "renamed_from", "source_path", "target_path",
)
_COMMIT_KEYS = (
    "commit_id", "latest_commit_id", "previous_commit_id", "base_commit_id",
    "commit", "commit_ref", "sha", "hexsha", "target_snapshot_id",
)
_NAME_KEYS = (
    "repository_name", "project_name", "project_code", "group", "target_key",
    "name", "url", "repo_url", "svn_url",
)
_TEXT_KEYS = (
    # `label` / `title` 走文本脱敏而**不是整体哈希**，理由是它们各自是唯一的信息载体：
    #   * `label` 形如 `file_diff <commit> <path>` —— 把整条哈希掉，路径就没了，
    #     证据覆盖直接归零（实测踩过：evidence_files 42 → 0）；
    #   * `title` 是给人标金标时读的那句话 —— 哈希掉之后标注的人看不懂，样本白导。
    # 它们里面夹带的路径与提交号由 `text()` 逐个替掉，正文留着。
    "label", "title",
    "prompt", "message", "evidence", "impact", "suggestion", "detail", "reason",
    "summary", "delta_summary", "report_markdown", "response_text", "text",
    "budget_notes", "correction_hint", "error", "error_message", "note",
    "description", "truncation_reason", "skipped_reason",
)
# 自由文本里出现的路径 token（与 metrics.py 同一族的放宽版：正文里可能带引号/括号）。
_TEXT_PATH_RE = re.compile(
    r"[A-Za-z0-9_./\\\-一-鿿]+\.(?:xlsx|xlsm|xlsb|xls|csv|lua|py|json|ts|js|cs|"
    r"cpp|h|java|txt|xml|yaml|yml|md|proto|prefab|asset|ini|toml|sql)\b",
    re.IGNORECASE,
)
_TEXT_COMMIT_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_TEXT_URL_RE = re.compile(r"\b(?:https?|ssh|svn|git)://[^\s\"'<>]+", re.IGNORECASE)
# **只有目录、没有文件名**的路径（`a/b/C/`、`b/C`）。正文里常见，而带扩展名那条
# 正则抓不到它 —— 实测就是它把仓库目录漏在报告正文里的。
# 段首必须是字母/下划线（挡住 `3/4` 这种数值比值），至少两段。
_TEXT_DIR_RE = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_.\-一-鿿]*/)+"
    r"(?:[A-Za-z_][A-Za-z0-9_.\-一-鿿]*)?"
)
# 已经是假名的形态（`path_<12位十六进制>.<ext>` / `commit_<12位>` / `name_<12位>` …）。
_PSEUDONYM_RE = re.compile(
    r"^(?:path|commit|name|url|id|dir)_[0-9a-f]{12}(?:\.[A-Za-z0-9]{1,8})?$"
)
# 脱敏**覆盖不到**的东西：写进样本里，免得下一个人以为「脱敏过了」就等于干净。
REDACTION_LIMITS = (
    "散文里的裸标识符（协议名 / 表名 / 符号名 / 玩法术语）：没有名字清单就认不出来，**没有被替换**",
    "目录路径（`a/b/C/` 这种）会换成 `dir_<hash>`，但散文里孤立出现的单个目录名仍可能留下",
    "数值、时间、数量、状态这些不含标识符的字段原样保留",
)


class Pseudonymizer:
    """稳定假名。同一输入永远得到同一输出（纯哈希 + 固定 salt）。

    **幂等**：已经是一个假名的值原样返回。少了这一条，任何一次「对脱敏后的数据再脱敏」
    都会把假名哈希第二遍 —— 于是同一个文件在 `change_files` 里是一个假名、在
    `findings` 里是另一个，覆盖率静默变成 0，而两边看起来都很正常。
    """

    def __init__(self, salt: str = "", enabled: bool = True) -> None:
        self.salt = salt or ""
        self.enabled = bool(enabled)

    def _already_a_pseudonym(self, text: str) -> bool:
        return _PSEUDONYM_RE.match(text) is not None

    def pseudonym(self, value: Any, *, prefix: str = "id", keep_extension: bool = False) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if not self.enabled:
            return text
        if self._already_a_pseudonym(text):
            return text
        digest = hashlib.sha1((self.salt + text).encode("utf-8")).hexdigest()[:12]
        extension = ""
        if keep_extension:
            _base, dot, tail = text.rpartition(".")
            if dot and tail and len(tail) <= 8 and tail.isalnum():
                extension = "." + tail.lower()
        return f"{prefix}_{digest}{extension}"

    def path(self, value: Any) -> str:
        return self.pseudonym(normalize_path(value), prefix="path", keep_extension=True)

    def commit(self, value: Any) -> str:
        """提交号假名：**先归一到 12 位再哈希**。

        归一这一步是必须的，不是顺手：库里同一个提交在 `delta_files` 里是全 40 位 SHA、
        在逐轮标签里是 12 位短 SHA。直接对原值哈希，两者得到两个不同的假名，
        于是 `coverage_ledger` 的**保守口径**（按 `(提交, 路径)` 配对，要求标签里的短
        SHA 与白名单里的全 SHA 前缀一致）在脱敏样本上恒为 0 —— 实测 42 个文件
        在未脱敏样本上按对算是 42、脱敏后变成 0，而宽松口径还是 32。
        取前 12 位再哈希，前缀关系在假名上保持成立，两种口径就都还在。
        """
        text = str(value or "").strip().lower()
        if not text:
            return ""
        if not self.enabled:
            return text
        # 幂等要在**截断之前**判：`commit_ddac9d88c224` 取前 12 位是 `commit_ddac9`，
        # 拿去再哈希就又变成一个新假名。
        if self._already_a_pseudonym(text):
            return text
        return self.pseudonym(text[:12], prefix="commit")

    def text(self, value: Any) -> str:
        """自由文本：先换 URL，再换文件路径，再换纯目录路径，最后换提交号。

        顺序有讲究：文件路径那条会把路径**整段**吃掉（含它前面的目录），所以要先跑；
        否则目录那条先动手，文件路径就被切碎了，文件名也一起丢掉。
        """
        raw = "" if value is None else str(value)
        if not raw or not self.enabled:
            return raw
        raw = _TEXT_URL_RE.sub(lambda match: self.pseudonym(match.group(0), prefix="url"), raw)
        raw = _TEXT_PATH_RE.sub(lambda match: self.path(match.group(0)), raw)
        raw = _TEXT_DIR_RE.sub(
            lambda match: self.pseudonym(match.group(0), prefix="dir"), raw
        )
        raw = _TEXT_COMMIT_RE.sub(lambda match: self.commit(match.group(0)), raw)
        return raw

    def structure(self, value: Any) -> Any:
        """结构化脱敏：**按键名**决定怎么换，递归走一遍。

        为什么不能只扫文本：`repository_name: "qz_luaworkspace代码"` 既不是路径也不带
        扩展名，正则扫不到，但它就是公司内部信息。键名是这份数据里最可靠的语义来源，
        用它比用正则猜准得多。

        **叶子上的兜底是 `text()`，而 `text()` 不作用在序列化后的 JSON 上。**
        这一条是实测踩出来的：`\\b[0-9a-fA-F]{7,40}\\b` 会把 `"duration_ms": 1595548`
        里的 7 位数字当成提交短 SHA 换掉，于是 JSON 里出现 `"duration_ms": commit_xxx`
        —— 文件还在、内容却再也 `json.loads` 不回来了（实测在 73,136 字节处报
        `Expecting value`）。按值改、在序列化**之前**改，才不会破坏结构。
        """
        if not self.enabled:
            return value
        if isinstance(value, dict):
            result: Dict[str, Any] = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if lowered in _PATH_KEYS and isinstance(item, str) and item:
                    result[key] = self.path(item)
                elif lowered in _COMMIT_KEYS and isinstance(item, str) and item:
                    result[key] = self.commit(item)
                elif lowered in _NAME_KEYS and isinstance(item, str) and item:
                    result[key] = self.pseudonym(item, prefix="name")
                elif lowered in _TEXT_KEYS and isinstance(item, str) and item:
                    result[key] = self.text(item)
                else:
                    result[key] = self.structure(item)
            return result
        if isinstance(value, list):
            return [self.structure(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


# ---------------------------------------------------------------------------
#  只读读取
# ---------------------------------------------------------------------------

def open_readonly(db_path: str) -> sqlite3.Connection:
    """只读连接。`mode=ro` 由 sqlite 保证 —— 不靠「我记得别写」这种自觉。"""
    absolute = os.path.abspath(db_path)
    if not os.path.isfile(absolute):
        raise FileNotFoundError(f"数据库不存在：{absolute}")
    uri = "file:{}?mode=ro".format(absolute.replace("\\", "/").replace("?", "%3f").replace("#", "%23"))
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> List[str]:
    cursor = connection.execute(f"PRAGMA table_info({table})")
    return [row["name"] for row in cursor.fetchall()]


def _select_rows(connection: sqlite3.Connection, table: str, fields: Iterable[str],
                 where: str = "", params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    """按白名单取列：**库里多出来的列不会被自动带出去**（新列可能含敏感内容）。"""
    available = set(_table_columns(connection, table))
    columns = [name for name in fields if name in available]
    if not columns:
        return []
    sql = f"SELECT {', '.join(columns)} FROM {table}"
    if where:
        sql += f" WHERE {where}"
    cursor = connection.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def list_runs(db_path: str, limit: int = 20) -> List[Dict[str, Any]]:
    connection = open_readonly(db_path)
    try:
        rows = _select_rows(
            connection, "ai_analysis_run",
            ("id", "project_id", "target_type", "target_key", "status", "scope",
             "trigger_source", "model", "tokens_input", "tokens_output",
             "duration_ms", "anomalies_found", "subagent_count", "created_at"),
        )
    finally:
        connection.close()
    rows.sort(key=lambda row: (row.get("id") or 0), reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
#  导出
# ---------------------------------------------------------------------------

def _redact_payload(raw: Any, pseudonymizer: Pseudonymizer) -> Any:
    """JSON 文本列：按结构脱敏后重新序列化。

    不是 JSON 的（老行/坏行）退回纯正则 —— 那种情况下没有结构可保，只能尽力而为。
    """
    payload = _load_json(raw)
    if payload is None:
        return pseudonymizer.text(raw)
    return json.dumps(pseudonymizer.structure(payload), ensure_ascii=False)


def _redact_run_row(run_row: Dict[str, Any], pseudonymizer: Pseudonymizer) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for key, value in run_row.items():
        if key in ("request_payload", "response_payload"):
            redacted[key] = _redact_payload(value, pseudonymizer)
        elif key in ("response_text", "delta_summary", "target_key", "target_id", "error_message"):
            redacted[key] = pseudonymizer.text(value)
        else:
            redacted[key] = value
    return redacted


def _redact_trace_row(trace_row: Dict[str, Any], pseudonymizer: Pseudonymizer) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for key, value in trace_row.items():
        if key in ("requests_json", "executed_json", "dropped_json"):
            redacted[key] = _redact_payload(value, pseudonymizer)
        elif key in ("response_text", "error", "correction_hint", "budget_notes"):
            redacted[key] = pseudonymizer.text(value)
        else:
            redacted[key] = value
    return redacted


def _redact_finding(finding: Dict[str, Any], pseudonymizer: Pseudonymizer) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for key, value in finding.items():
        if key in ("file_path",):
            redacted[key] = pseudonymizer.path(value)
        elif key in ("title", "evidence", "impact", "suggestion", "commit_ref", "category",
                     "fingerprint"):
            redacted[key] = pseudonymizer.text(value)
        else:
            redacted[key] = value
    return redacted


def _change_files(request_payload: Any, trace_rows: List[Dict[str, Any]],
                  findings: List[Dict[str, Any]]) -> Tuple[List[str], str]:
    """本次变化的文件清单（覆盖率的分母候选）。

    三个来源按可靠性排序取并集，并把**来源**记下来：`request_payload` 的清单是
    引擎自己冻结的（最可靠），trace 的取数标签只说明「读过」，findings 只说明
    「报过」。人工标金标时以 request_payload 那一份为准，其余两份是兜底。
    """
    paths: Dict[str, None] = {}
    source = "none"
    payload = _load_json(request_payload)
    if isinstance(payload, dict):
        for key in _CHANGE_FILE_KEYS:
            raw = payload.get(key)
            if not isinstance(raw, list):
                continue
            for item in raw:
                if isinstance(item, str):
                    paths.setdefault(normalize_path(item), None)
                elif isinstance(item, dict):
                    candidate = item.get("path") or item.get("file_path")
                    if candidate:
                        paths.setdefault(normalize_path(candidate), None)
            if paths:
                source = "request_payload"
    if not paths:
        from services.ai.trace_evidence import decode_evidence

        for row in trace_rows:
            if row.get("requests_json") is None:
                continue
            for item in decode_evidence(record_like(row)).get("requests") or []:
                path = item.get("path")
                if path:
                    paths.setdefault(normalize_path(path), None)
        if paths:
            source = "trace_requests"
    if not paths:
        for finding in findings:
            path = finding.get("file_path")
            if path:
                paths.setdefault(normalize_path(path), None)
        if paths:
            source = "findings"
    return list(paths), source


def _load_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def export_run(db_path: str, run_id: int, out_dir: str, *, redact: bool = True,
               salt: str = "", write_gold_template: bool = True) -> Dict[str, Any]:
    """导出一个 run 的评测样本。返回写出去的清单（供 CLI 打印与测试断言）。"""
    pseudonymizer = Pseudonymizer(salt=salt, enabled=redact)
    connection = open_readonly(db_path)
    try:
        run_rows = _select_rows(connection, "ai_analysis_run", RUN_ROW_FIELDS, "id = ?", (run_id,))
        if not run_rows:
            raise LookupError(f"没有 id={run_id} 的 ai_analysis_run")
        run_row = _redact_run_row(run_rows[0], pseudonymizer)

        trace_rows = _select_rows(connection, "ai_analysis_trace", TRACE_ROW_FIELDS,
                                  "run_id = ? ORDER BY round_index", (run_id,))
        trace_rows = [_redact_trace_row(row, pseudonymizer) for row in trace_rows]

        finding_rows = _select_rows(connection, "ai_analysis_anomaly", FINDING_ROW_FIELDS,
                                    "run_id = ?", (run_id,))
        findings = [_redact_finding(row, pseudonymizer) for row in finding_rows]
    finally:
        connection.close()

    change_files, change_source = _change_files(run_row.get("request_payload"), trace_rows, findings)
    # 这里**不再**把 change_files 过一遍假名：三个来源（request_payload / trace /
    # findings）在上一步都已经脱敏过，再映射一次会把假名当原值哈希第二遍，
    # 于是 change_files 与 findings / 证据覆盖里的路径**对不上** —— 覆盖率静默变 0。

    evidence_files = evidence_files_from_traces(trace_rows)
    coverage = coverage_from_traces(
        trace_rows,
        request_payload=_load_json(run_row.get("request_payload")),
        tool_stats=_load_json(run_row.get("tool_stats_json")),
    )

    sample_id = f"run{run_id}"
    # 阶段账走 `response_payload["subagents"]`（唯一来源），复用面板那一份解析。
    stages = stage_rows_from_payload(run_row.get("response_payload"))
    sample = BenchmarkSample(
        sample_id=sample_id,
        run_row=run_row,
        inputs={
            "change_files": change_files,
            "change_files_source": change_source,
            "evidence_detail_collected": any(
                row.get("executed_json") is not None for row in trace_rows
            ),
            "request_payload": _load_json(run_row.get("request_payload")),
        },
        outputs={
            "status": run_row.get("status"),
            "degradation": run_row.get("degradation"),
            "conclusion_structured": run_row.get("conclusion_structured"),
            "response_text": run_row.get("response_text"),
            "response_payload": _load_json(run_row.get("response_payload")),
        },
        findings=findings,
        stages=stages,
        trace_rows=trace_rows,
        evidence_files=evidence_files,
        coverage=coverage,
        created_at=datetime.now(timezone.utc).isoformat(),
        redacted=bool(redact),
        notes=(
            "由 scripts/export_ai_benchmark_sample.py 只读导出；"
            + ("路径 / 提交号 / 仓库名已替换为稳定假名" if redact else "**未脱敏**，不要在共享目录里保存")
        ),
    )
    sample.inputs["redaction"] = {
        "enabled": bool(redact),
        "level": "结构化按键名 + 自由文本按路径/目录/提交号" if redact else "none",
        "not_covered": list(REDACTION_LIMITS) if redact else [],
    }

    samples_dir = os.path.join(out_dir, "samples")
    sample.save(os.path.join(samples_dir, f"{sample_id}.json"))

    written: Dict[str, Any] = {
        "sample_id": sample_id,
        "sample_path": os.path.join(samples_dir, f"{sample_id}.json"),
        "redacted": bool(redact),
        "change_files": len(change_files),
        "change_files_source": change_source,
        "evidence_files": len(evidence_files),
        "findings": len(findings),
        "trace_rows": len(trace_rows),
    }
    # 两个口径**都印出来、都带名字**：`evidence_files` 是逐轮明细口径（主），
    # `evidence_files_by_pair` 是台账保守口径（次）。只印一个，看的人就会以为
    # 这就是「覆盖率」——而它们回答的是两个问题（见 metrics.evidence_coverage_scopes）。
    ledger_counts = (coverage or {}).get("counts") or {}
    written["coverage_scopes"] = {
        "main": {
            "scope": EVIDENCE_SCOPE_TRACE_DETAIL,
            "covered": len(evidence_files),
            "total": len(change_files),
            "note": "逐轮明细口径：取到过内容的文件（file_diff/file_content，非失败非空）",
        },
        "secondary": {
            "scope": EVIDENCE_SCOPE_LEDGER_BY_PAIR,
            "covered": ledger_counts.get("evidence_files_by_pair"),
            "total": ledger_counts.get("window_files") or ledger_counts.get("batch_files"),
            "note": "台账保守口径：再要求标签里的短提交号命中白名单里的 latest_commit_id",
        },
    }

    if write_gold_template:
        template = GoldLabel.template(sample_id, change_files=change_files)
        template_path = os.path.join(out_dir, "gold_templates", f"{sample_id}.json")
        template.save(template_path)
        written["gold_template_path"] = template_path
        written["gold_template_note"] = (
            "金标骨架写在 gold_templates/ 而不是 gold/ —— 放进 gold/ 会被 load_dataset "
            "当成一份**真的**金标（0 条问题），于是召回率静默变成 0%。"
        )

    manifest_path = os.path.join(out_dir, "manifest.json")
    existing = _load_json(_read_text(manifest_path)) or {}
    exported = list(existing.get("exported") or [])
    exported.append({
        "sample_id": sample_id,
        "run_id": run_id,
        "exported_at": sample.created_at,
        "redacted": bool(redact),
    })
    _write_text(manifest_path, json.dumps({
        "schema": MANIFEST_SCHEMA,
        "out_dir": os.path.abspath(out_dir),
        "salt_fingerprint": hashlib.sha1((salt or "").encode("utf-8")).hexdigest()[:8] if redact else "",
        "exported": exported,
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    written["manifest_path"] = manifest_path
    return written


def _read_text(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _write_text(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="只读导出一个 AI 分析运行作为评测样本")
    parser.add_argument("--db", default=os.path.join(ROOT_DIR, DEFAULT_DB_RELATIVE),
                       help="SQLite 库路径（只读打开）")
    parser.add_argument("--run-id", type=int, default=0, help="要导出的 ai_analysis_run.id")
    parser.add_argument("--out", default="", help="输出目录（样本写到 <out>/samples/）")
    parser.add_argument("--list-runs", action="store_true", help="列出最近几次运行后退出")
    parser.add_argument("--limit", type=int, default=20, help="--list-runs 显示多少条")
    parser.add_argument("--no-redact", action="store_true",
                       help="**不脱敏**（路径/提交号原样导出）。默认脱敏，不要往共享目录导。")
    parser.add_argument("--salt", default=os.environ.get("AI_BENCHMARK_SALT", ""),
                       help="假名用的 salt（默认空；换了 salt 假名就全变了）")
    parser.add_argument("--no-gold-template", action="store_true", help="不生成金标骨架")
    args = parser.parse_args(argv)

    if args.list_runs:
        for row in list_runs(args.db, limit=args.limit):
            print(
                f"run {row.get('id'):>5}  project={row.get('project_id')}  "
                f"status={row.get('status')}  scope={row.get('scope')}  "
                f"tokens={row.get('tokens_input')}/{row.get('tokens_output')}  "
                f"findings={row.get('anomalies_found')}  {row.get('created_at')}"
            )
        return 0

    if not args.run_id:
        parser.error("要么 --run-id，要么 --list-runs")
    if not args.out:
        parser.error("--out 是必需的（样本目录）")

    written = export_run(
        args.db, args.run_id, args.out,
        redact=not args.no_redact,
        salt=args.salt,
        write_gold_template=not args.no_gold_template,
    )
    print(json.dumps(written, ensure_ascii=False, indent=2, sort_keys=True))
    if not written["redacted"]:
        sys.stderr.write(
            "⚠️ 这是**未脱敏**导出（--no-redact）：路径与提交号原样落盘，不要放进共享目录或提交进仓库。\n"
        )
    else:
        sys.stderr.write(
            "ℹ️ 已脱敏（路径/目录/提交号/仓库名 → 稳定假名）。**脱敏不等于干净**，"
            "下面这些没有被替换：\n"
            + "".join(f"   · {item}\n" for item in REDACTION_LIMITS)
        )
    if not written["change_files"]:
        sys.stderr.write(
            "⚠️ 没能自动推断「本次变化的文件」：金标骨架里的 change_files 是空的，"
            "变化文件覆盖率会是 None（未知）。请人工补齐后再跑 A/B。\n"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
