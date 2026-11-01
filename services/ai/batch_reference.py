"""The same bounded reference pages for legacy per-file commit snapshots."""
import threading
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from services.ai.reference_index import snapshot_digest
from services.ai.repo_reference import search_frozen_repository


class ReferenceBodies:
    """Small byte-bounded snapshot body cache shared across prefix pages."""

    def __init__(self, max_bytes=32 * 1024 * 1024, max_entries=600):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.values = OrderedDict()
        self.bytes = 0
        self.lock = threading.RLock()

    def read(self, key, reader):
        with self.lock:
            if key in self.values:
                value, _cost = self.values[key]
                self.values.move_to_end(key)
                return value
            value = reader()
            if value is None:
                return None
            cost = (len(value) if isinstance(value, bytes) else len(str(value)) * 4) + len(repr(key)) * 4 + 128
            if cost <= self.max_bytes:
                self.values[key] = (value, cost)
                self.bytes += cost
                while self.bytes > self.max_bytes or len(self.values) > self.max_entries:
                    _key, (_body, removed) = self.values.popitem(last=False)
                    self.bytes -= removed
            return value


def bounded_git_reader(repository, legacy_reader):
    """Use bounded immutable blob reads wherever a real local Git tree exists."""
    from services.ai import frozen_repo
    from services.ai.excel_source import _is_openpyxl_workbook
    from services.ai.reference_index import MAX_INDEX_FILE_BYTES, XLSX_MAX_INDEX_BYTES, IndexReadLimit

    if str(getattr(repository, "type", "")).lower() != "git":
        return legacy_reader
    service = frozen_repo._git_service_for(repository)
    local_path = str(getattr(service, "local_path", "") or "")
    if not local_path or not Path(local_path).is_dir():
        return legacy_reader
    readers = {}

    def read(path, commit):
        if commit not in readers:
            readers[commit] = frozen_repo.FrozenTreeReader(frozen_repo.FrozenRepository(
                repository_id=getattr(repository, "id", None), tip=commit, local_path=local_path))
        source = readers[commit]
        bound = XLSX_MAX_INDEX_BYTES if _is_openpyxl_workbook(path) else MAX_INDEX_FILE_BYTES
        size = source.blob_size(path)
        if size is not None and size > bound:
            raise IndexReadLimit("blob exceeds indexing bound")
        return source.read_bytes(path, max_bytes=bound)

    return read


class BatchReader:
    allowed_paths = None

    def __init__(self, repository_id, pairs, read):
        self.commits = dict(pairs)
        self.read = read
        self.frozen = SimpleNamespace(repository_id=repository_id,
                                      tip=snapshot_digest(sorted(self.commits.items())))

    def tracked_paths(self):
        return tuple(sorted(self.commits))

    def read_bytes(self, path):
        return self.read(path, self.commits[path])


def search_batch_references(pairs, query, *, reader, repository_id, prefix="", cursor="",
                            max_output_chars=7_000, total_files=0, cache=None):
    adapter = BatchReader(repository_id, pairs, reader)
    outcome = search_frozen_repository(adapter, query, prefix=prefix, cursor=cursor,
                                       max_output_chars=max_output_chars - 180, cache=cache)
    marker = f"仓库冻结版本 `{adapter.frozen.tip[:12]}` 的 **Git 跟踪文件**"
    text = outcome.text.replace(marker, "**本批次改动过的文件**")
    text = text.replace("全部**跟踪文件", "全部**本批次文件")
    text = text.replace("仓库里没有别处引用", "本批次里没有别处引用")
    text += f"\n本页搜索覆盖了本批次 {outcome.indexed}/{outcome.files_total} 个文件。"
    text += f"索引版本 `{outcome.index_version}`，快照 `{adapter.frozen.tip[:12]}`。"
    if outcome.unindexed:
        text += f"{outcome.unindexed} 个还没索引到本页；文件数到了上限就停了，请按 cursor 续取。没搜到不代表整批里没有。"
    declared = max(outcome.files_total, int(total_files or 0))
    if declared > outcome.files_total:
        gap = declared - outcome.files_total
        text = text.replace("coverage_complete=true", "coverage_complete=false")
        text = text.replace("所以「没有命中」是可信的结论。", "但平台声明的清单更大，不能据此下全批次否定结论。")
        text = text.replace(f"files_total={outcome.files_total} ", f"files_total={declared} ")
        text += f"\n[批次清单缺口] 平台声明 {declared} 个文件，实际只收到 {outcome.files_total} 个；缺 {gap} 个未搜。"
        outcome = replace(outcome, unindexed=outcome.unindexed + gap, files_total=declared)
    return replace(outcome, text=text)
