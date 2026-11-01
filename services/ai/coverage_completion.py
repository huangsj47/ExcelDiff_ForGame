"""Bounded completion of frozen input diffs, using model delivery receipts.

Listing a filename or fetching its body is insufficient. Only content in a
successful model call advances this ledger. No project-specific rules, extra
rounds or budget exemptions live here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from services.ai.mandatory_progress import _commit_matches
from services.ai.protocol import ContextRequest
from services.ai.scope import normalize_path


@dataclass
class CoverageCompletion:
    entries: tuple = ()
    done: set = field(default_factory=set)
    segments: dict = field(default_factory=dict)
    received: dict = field(default_factory=dict)
    attempted: set = field(default_factory=set)

    @property
    def missing_count(self):
        return len(self.entries) - len(self.done)

    def observe(self, receipts):
        for row in receipts:
            if (row.get("kind") != "file_diff" or row.get("failed") or row.get("empty")
                    or row.get("omitted_for_budget") or row.get("shrink_level")):
                continue
            for index, entry in enumerate(self.entries):
                if normalize_path(row.get("path", "")) != normalize_path(entry.path):
                    continue
                # Production manifests and delivery receipts both resolve repository identity.
                if str(row.get("repository_id") or "") != str(entry.repository_id or ""):
                    continue
                if not _commit_matches(row.get("commit", ""), entry.commit):
                    continue
                total = int(row.get("segments") or 1)
                self.segments[index] = total
                parts = row.get("complete_segments")
                if parts is None:
                    parts = (1,) if total == 1 and not row.get("truncated") else ()
                have = self.received.setdefault(index, set())
                have.update(part for part in parts if 1 <= part <= total)
                if len(have) == total:
                    self.done.add(index)

    def next_requests(self, count):
        requests = []
        # First touch all files, then fill remaining segments. Never repeat a failed address.
        ordered = sorted(range(len(self.entries)), key=lambda i: i in self.segments)
        for index in ordered:
            if index in self.done or len(requests) >= count:
                continue
            entry = self.entries[index]
            if index in self.segments:
                have = self.received.get(index, set())
                windows = _missing_windows(self.segments[index], have)
            else:
                windows = ("",)
            for window in windows:
                if (index, window) in self.attempted:
                    continue
                self.attempted.add((index, window))
                requests.append(ContextRequest(type="file_diff", commit=entry.commit,
                                               path=entry.path, repository_id=str(entry.repository_id or ""),
                                               lines=window))
                break
        return tuple(requests)

    def note(self):
        if not self.entries:
            return ""
        return (f"本次冻结输入 {len(self.entries)} 个文件，完整差异已交付 {len(self.done)} 个，"
                f"尚有 {self.missing_count} 个未完整交付（可能只读过部分段落）。"
                "先覆盖剩余差异，再深入取关联正文；不得把未交付部分写成无风险。"
                "报告只提交结构化业务条目和必要摘要，平台负责生成重复的风险与测试章节。")


def completed_paths(entries, receipts):
    """Legacy path consumers may advance a path only when all its repository identities completed."""
    from types import SimpleNamespace

    coverage = CoverageCompletion(tuple(SimpleNamespace(**entry) for entry in entries))
    coverage.observe(receipts)
    missing_paths = {entry.path for i, entry in enumerate(coverage.entries) if i not in coverage.done}
    return {entry.path for entry in coverage.entries if entry.path not in missing_paths}


def _missing_windows(total, received):
    start = None
    for number in range(1, total + 2):
        missing = number <= total and number not in received
        if missing and start is None:
            start = number
        elif not missing and start is not None:
            yield str(start) if start == number - 1 else f"{start}-{number - 1}"
            start = None
