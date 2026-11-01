"""A shared GitPython command must finish one object stream before the next."""

import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace

import pytest

from services.ai import frozen_repo as module
from tests.test_ai_frozen_repo_read_scope import _commit, _git


@pytest.fixture(autouse=True)
def clean_caches():
    module.reset_caches()
    yield
    module.reset_caches()


@pytest.mark.parametrize("operation", ["read_bytes", "blob_size", "all_tracked_paths", "has_object"])
@pytest.mark.parametrize("first_operation", ["read_bytes", "all_tracked_paths"])
def test_git_commands_cannot_overlap_an_unconsumed_object_stream(operation, first_operation):
    streaming = threading.Event()
    release = threading.Event()
    second_boundary = threading.Event()
    overlap = threading.Event()
    active = threading.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = threading.RLock()

        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                second_boundary.set()
                self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()

    class Stream(BytesIO):
        def __init__(self, path):
            super().__init__(path.encode())
            self.path = path

        def read(self, size=-1):
            if self.path == "first.lua":
                streaming.set()
                assert release.wait(5)
            data = super().read(size)
            if not data:
                active.clear()
            return data

    class Blob:
        def __init__(self, path):
            self.path = path
            self.size = len(path)

        @property
        def data_stream(self):
            active.set()
            return Stream(self.path)

    class Tree:
        def __getitem__(self, path):
            return Blob(path)

        def traverse(self):
            active.set()
            streaming.set()
            assert release.wait(5)
            try:
                yield SimpleNamespace(type="blob", path="first.lua")
                yield SimpleNamespace(type="blob", path="second.lua")
            finally:
                active.clear()

    class Repository:
        def commit(self, tip):
            if active.is_set():
                overlap.set()
                second_boundary.set()
            return SimpleNamespace(tree=Tree())

    reader = module.FrozenTreeReader(module.FrozenRepository(repository_id=7, tip="fixed"), repo=Repository())
    reader._git_lock = ObservedLock()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = (pool.submit(reader.read_bytes, "first.lua") if first_operation == "read_bytes"
                 else pool.submit(reader.all_tracked_paths))
        assert streaming.wait(5)
        method = getattr(reader, operation)
        second = pool.submit(method) if operation == "all_tracked_paths" else pool.submit(method, "second.lua")
        try:
            assert second_boundary.wait(2), "Second operation neither waited nor reached the command boundary"
            assert not overlap.is_set(), "A new command touched a shared cat-file process before stream EOF"
        finally:
            release.set()
        assert first.result(timeout=2) == (b"first.lua" if first_operation == "read_bytes"
                                           else ("first.lua", "second.lua"))
        expected = {"read_bytes": b"second.lua", "blob_size": 10,
                    "all_tracked_paths": ("first.lua", "second.lua"), "has_object": True}
        assert second.result(timeout=2) == expected[operation]


def test_shared_real_git_reader_preserves_exact_blob_bytes_and_metadata(tmp_path):
    _git(tmp_path, "init", "-q")
    contents = {f"dir{number % 3}/file{number:02}.bin":
                bytes(range(256)) * (number + 1) + f"tail-{number}".encode()
                for number in range(32)}
    for name, body in contents.items():
        target = tmp_path / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(body)
    tip = _commit(tmp_path, "concurrent reader fixture")
    reader = module.FrozenTreeReader(module.FrozenRepository(repository_id=7, tip=tip, local_path=str(tmp_path)))
    starting = threading.Barrier(8)
    groups = [list(contents)[offset::8] for offset in range(8)]

    def read_group(names):
        starting.wait(timeout=5)
        for name in names:
            assert reader.blob_size(name) == len(contents[name])
            assert reader.read_bytes(name) == contents[name]
            assert set(reader.all_tracked_paths()) == set(contents)
            assert reader.has_object(tip)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(read_group, groups))
