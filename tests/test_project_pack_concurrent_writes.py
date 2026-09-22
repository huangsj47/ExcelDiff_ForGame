# -*- coding: utf-8 -*-
"""并发写知识包时，清单里**不许丢引用**（REV-KNOW-002）。

## 缺陷

清单是「读真目录 → 合并 → 影子校验 → 落盘」四步。`_atomic_write` 只保证**单文件**原子
（读侧看不到写了一半的文件），它管不了两个线程的读-改-写互相覆盖：

    T1 读 M0 ──┐                        T2 读 M0 ──┐
               └─ 合并出 M0+left                    └─ 合并出 M0+right
    T1 落盘（文件 + M0+left）            T2 落盘（文件 + M0+right）   ← left 那一行没了

结果是**两份文件都在、清单里只提了一条**。而真目录落地之后**不再复验**，所以这个不一致
状态被静默留在盘上，要等下一次 `validate_all()` / 面板校验才报出来。`runtime_entry`
用 `threaded=True` 跑 Flask，两个并发 PUT 真的会落在两条线程上。

## 修法

写路径整段进 `_pack_write_lock`（按包目录分片的进程内锁），**并把清单的读取挪进
`mutate` 里**——只在落盘那一步上锁是没用的，两个线程在进入之前就已经各读到同一份旧清单。

## 这个用例为什么要人为制造窗口

不加干预时两个线程往往自然错开（IO 太快），**未修的代码也能通过** —— 那就是假绿。
所以这里把 `_append_reference_mention` 换成一个会在**读清单之后**对暗号的版本：
两个线程都到了才放行。加锁时它们到不齐（第二个进不来），依次超时后各自照常完成；
不加锁时它们必然同时到，于是必然读到同一份旧清单。
"""
import threading
import uuid
from pathlib import Path

import pytest

from services.ai import project_pack_service
from services.ai.project_pack_service import (
    PROJECT_PACK_MANIFEST,
    create_pack_from_template,
    write_reference,
)
from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV

BOOTSTRAP_REFS = ("config-table-spec.md", "gameplay-semantics.md")


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def pack(tmp_path, monkeypatch):
    """建一个真知识包。位置走 `SKILL_PROJECTS_ROOT`（与本仓库既有包测试同一手法）。"""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(root))
    code = _uid("qa")
    create_pack_from_template(code)
    return code, root


def _rendezvous(monkeypatch, barrier):
    """把「合并清单」换成一个会等另一个线程的版本（见模块 docstring）。"""
    original = project_pack_service._append_reference_mention

    def slow(text, file_name, description):
        try:
            barrier.wait(timeout=1.5)
        except threading.BrokenBarrierError:
            pass
        return original(text, file_name, description)

    monkeypatch.setattr(
        project_pack_service, "_append_reference_mention", slow
    )


def _mentioned(manifest_text: str) -> set:
    return {
        name
        for name in ("left-doc.md", "right-doc.md")
        if name in manifest_text
    }


def test_two_concurrent_adds_keep_both_manifest_lines(pack, monkeypatch):
    code, root = pack
    barrier = threading.Barrier(2)
    _rendezvous(monkeypatch, barrier)
    errors: list = []

    def add(name: str) -> None:
        try:
            write_reference(code, name.removesuffix(".md"), f"# {name}\n\n正文。", repo_root=root)
        except BaseException as exc:  # noqa: BLE001 —— 线程里的异常要带回主线程再断言
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=add, args=(name,))
        for name in ("left-doc.md", "right-doc.md")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    pack_dir = root / code
    on_disk = {path.name for path in (pack_dir / "references").glob("*.md")}
    assert {"left-doc.md", "right-doc.md"}.issubset(on_disk), on_disk

    manifest = (pack_dir / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")
    assert _mentioned(manifest) == {"left-doc.md", "right-doc.md"}, (
        "两份文件都在，清单里却只提了一条 —— 后写的把先写的那一行覆盖掉了"
    )


def test_sequentially_the_manifest_can_hold_both(pack):
    """顺序写时两条引用都在 —— 这条是**前提**，不是结论。

    没有它的话，上面那条用例红了会分不清是「并发丢了」还是「这个断言本来就不可能成立」。
    （第一版写的是一条「建包自带的引用没被搅掉」的用例：它在**有锁无锁时都是绿的**，
    等于什么都没守，所以换成了这条。）
    """
    code, root = pack
    for name in ("left-doc.md", "right-doc.md"):
        write_reference(code, name.removesuffix(".md"), f"# {name}\n\n正文。")

    manifest = (root / code / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")
    assert _mentioned(manifest) == {"left-doc.md", "right-doc.md"}
    for bootstrap in BOOTSTRAP_REFS:
        assert bootstrap in manifest, f"建包自带的引用被覆盖掉了：{bootstrap}"
