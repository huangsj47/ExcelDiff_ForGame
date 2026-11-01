"""Slow snapshot reads must only block callers requesting the same index page."""

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from services.ai import repo_reference as module


class Reader:
    allowed_paths = None

    def __init__(self, identity, read):
        self.frozen = SimpleNamespace(repository_id=identity, tip="fixed")
        self.read = read

    def tracked_paths(self):
        return ("a.lua",)

    def read_bytes(self, path):
        return self.read()


@pytest.mark.parametrize("separate_caches", [False, True])
def test_slow_builds_do_not_block_a_different_repository_or_injected_cache(separate_caches):
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    cache = {}

    def first_read():
        first_started.set()
        assert release_first.wait(5)
        return b"firstNeedle"

    def second_read():
        second_started.set()
        return b"secondNeedle"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(module.search_frozen_repository, Reader(1, first_read), "firstNeedle", cache=cache)
        assert first_started.wait(5)
        second = pool.submit(module.search_frozen_repository, Reader(1 if separate_caches else 2, second_read),
                             "secondNeedle", cache={} if separate_caches else cache)
        try:
            assert second_started.wait(2), "Unrelated cache misses are serialized behind a slow blob read"
            assert "a.lua:1: secondNeedle" in second.result(timeout=2).text
        finally:
            release_first.set()
        assert "a.lua:1: firstNeedle" in first.result(timeout=2).text


def observe_joined_build(monkeypatch):
    joined = threading.Event()
    original = Future.result

    def result(future, *args, **kwargs):
        if threading.current_thread().name.startswith("index-join"):
            joined.set()
        return original(future, *args, **kwargs)

    monkeypatch.setattr(Future, "result", result)
    return joined


@pytest.mark.parametrize("cache_bytes", [1, module.INDEX_CACHE_MAX_BYTES])
def test_same_page_callers_share_one_build_even_when_the_result_is_not_cached(monkeypatch, cache_bytes):
    monkeypatch.setattr(module, "INDEX_CACHE_MAX_BYTES", cache_bytes)
    joined = observe_joined_build(monkeypatch)
    reading = threading.Event()
    release = threading.Event()
    reads = []
    cache = {}

    def read():
        reads.append(1)
        reading.set()
        assert release.wait(5)
        return b"sharedNeedle"

    reader = Reader(1, read)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="index-join") as pool:
        first = pool.submit(module.search_frozen_repository, reader, "sharedNeedle", cache=cache)
        assert reading.wait(5)
        second = pool.submit(module.search_frozen_repository, reader, "sharedNeedle", cache=cache)
        try:
            assert joined.wait(2), "The second caller never joined the in-flight index build"
            assert not cache, "An unfinished index must not be published"
        finally:
            release.set()
        assert "a.lua:1: sharedNeedle" in first.result(timeout=2).text
        assert "a.lua:1: sharedNeedle" in second.result(timeout=2).text
    assert reads == [1]
    assert len(cache) == (0 if cache_bytes == 1 else 1)


def test_failed_build_wakes_joined_callers_and_a_later_call_can_retry(monkeypatch):
    joined = observe_joined_build(monkeypatch)
    building = threading.Event()
    release = threading.Event()
    original = module.SnapshotReferenceIndex.build
    attempts = []
    cache = {}

    def build(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            building.set()
            assert release.wait(5)
            raise RuntimeError("temporary index build failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(module.SnapshotReferenceIndex, "build", build)
    reader = Reader(1, lambda: b"retriedNeedle")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="index-join") as pool:
        first = pool.submit(module.search_frozen_repository, reader, "retriedNeedle", cache=cache)
        assert building.wait(5)
        second = pool.submit(module.search_frozen_repository, reader, "retriedNeedle", cache=cache)
        try:
            assert joined.wait(2)
        finally:
            release.set()
        for task in (first, second):
            with pytest.raises(RuntimeError, match="temporary index build failure"):
                task.result(timeout=2)
    assert not cache
    retry = module.search_frozen_repository(reader, "retriedNeedle", cache=cache)
    assert "a.lua:1: retriedNeedle" in retry.text
    assert len(attempts) == 2


def test_build_admission_is_bounded_and_releases_its_bookkeeping(monkeypatch):
    monkeypatch.setattr(module, "INDEX_BUILD_MAX_ACTIVE", 1)
    waiting_for_slot = threading.Event()
    reading = threading.Event()
    release = threading.Event()
    second_reading = threading.Event()
    original_wait = module._INDEX_READY.wait

    def wait(*args, **kwargs):
        waiting_for_slot.set()
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(module._INDEX_READY, "wait", wait)

    def first_read():
        reading.set()
        assert release.wait(5)
        return b"firstNeedle"

    def second_read():
        second_reading.set()
        return b"secondNeedle"

    cache = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(module.search_frozen_repository, Reader(1, first_read), "firstNeedle", cache=cache)
        assert reading.wait(5)
        second = pool.submit(module.search_frozen_repository, Reader(2, second_read), "secondNeedle", cache=cache)
        try:
            assert waiting_for_slot.wait(2)
            assert not second_reading.is_set()
            assert len(module._INDEX_BUILDING) == 1
        finally:
            release.set()
        assert "a.lua:1: firstNeedle" in first.result(timeout=2).text
        assert "a.lua:1: secondNeedle" in second.result(timeout=2).text
    assert not module._INDEX_BUILDING
