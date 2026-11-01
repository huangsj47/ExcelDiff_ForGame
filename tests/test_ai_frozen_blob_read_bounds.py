from io import BytesIO
from types import SimpleNamespace

import pytest

from services.ai import frozen_repo as module


class Stream(BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


def reader_for(blob):
    reader = module.FrozenTreeReader(module.FrozenRepository(repository_id=7, tip="abc", local_path="."))
    reader._commit = lambda: SimpleNamespace(tree={"a.lua": blob})
    return reader


@pytest.fixture(autouse=True)
def clean():
    module.reset_caches()
    yield
    module.reset_caches()


def test_known_oversized_blob_is_rejected_before_streaming_any_bytes():
    stream = Stream(b"tiny")
    reader = reader_for(SimpleNamespace(size=module.MAX_READ_BYTES + 1, data_stream=stream))
    data, reason = reader.read_within_limit("a.lua")
    assert data is None and "上限" in reason
    assert stream.read_sizes == []


def test_unknown_or_untrustworthy_blob_size_still_uses_a_bounded_read():
    stream = Stream(b"x" * 50)
    reader = reader_for(SimpleNamespace(data_stream=stream))
    assert reader.read_bytes("a.lua", max_bytes=20) is None
    assert stream.read_sizes and all(0 <= size <= 21 for size in stream.read_sizes)
    assert module.cache_sizes()["blobs"] == 0


def test_blob_cache_evicts_by_total_bytes_as_well_as_entry_count(monkeypatch):
    monkeypatch.setattr(module, "BLOB_CACHE_MAX_BYTES", 30)
    for number in range(4):
        reader = reader_for(SimpleNamespace(size=20, data_stream=Stream(b"x" * 20)))
        reader.frozen = module.FrozenRepository(repository_id=number, tip="abc", local_path=".")
        assert reader.read_bytes("a.lua") == b"x" * 20
    assert module.cache_sizes()["blob_bytes"] <= 30
    assert module.cache_sizes()["blobs"] <= 1


def test_short_stream_reads_are_joined_until_eof_without_returning_a_partial_blob():
    class ShortStream(Stream):
        def read(self, size=-1):
            return super().read(min(3, size))
    stream = ShortStream(b"all original content")
    reader = reader_for(SimpleNamespace(size=20, data_stream=stream))
    assert reader.read_bytes("a.lua", max_bytes=30) == b"all original content"


def test_cache_hit_does_not_outlive_the_frozen_git_object():
    reader = reader_for(SimpleNamespace(size=4, data_stream=Stream(b"data")))
    assert reader.read_bytes("a.lua") == b"data"
    reader._commit = lambda: None
    assert reader.read_bytes("a.lua") is None
