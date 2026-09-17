# -*- coding: utf-8 -*-
"""改名要把本地工作副本目录一起搬走。

## 缺陷形态

仓库的本地路径**没有存在库里**，是每次按当前名字实时算出来的
（`utils/path_security.py:build_repository_local_path` 的
`{project}_{sanitize(repo)}_{id}`）。而改名过去只写 `repository.name`：

* 平台下一次读这个仓库时算出的路径与盘上的目录不是同一个；
* 该仓库被判成「未克隆」，重新 clone 一份；
* 旧目录成孤儿，只存在于本地的状态静默失联。

中文名放大了触发面（`abc` → `配置表` 会改变 sanitize 结果；中文名之间互改不会，
因为两个都折叠成 fallback `repository`），但根因与中文无关，ASCII 改名一样中招。

## 口径

失败就**放弃这次改名**（rollback + 红字提示），不改成「名字落库、目录没动」：
那样平台会换个目录重新 clone，用户看到的是「改个名字而已，怎么重新拉了一遍」。
「改名没成功但你知道为什么」严格优于「改名成功了，但平台在另一个目录上悄悄干活」。

这里的用例直接调 `handle_update_repository_form`（与
tests/test_repository_update_form_exception_narrowing.py 同一套注入方式），
并把 `relocate_repository_local_dir` 换成**真的**实现 + 临时 repos 基目录，
所以磁盘行为是真的被验证的，不是断言「某个函数被调用过」。
"""
from __future__ import annotations

import os
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import services.repository_update_form_service as update_form_service
from services.repository_local_dir_service import (
    BLOCKING_REASONS,
    REASON_MOVED,
    REASON_SAME_PATH,
    REASON_SOURCE_MISSING,
    REASON_TARGET_EXISTS,
    relocate_repository_local_dir,
    undo_repository_local_dir_move,
)

PROJECT_CODE = "PROJ"
REPO_ID = 7


@pytest.fixture()
def repos_base(tmp_path, monkeypatch):
    """把工作副本基目录指到 tmp，并返回它。

    `build_repository_local_path` 不传 base_dir 时读 `AGENT_REPOS_BASE_DIR`
    （见 utils/runtime_paths.py），所以设它就能让被测代码与断言算出同一个路径。
    """
    base = tmp_path / "repos"
    base.mkdir()
    monkeypatch.setenv("AGENT_REPOS_BASE_DIR", str(base))
    return base


def _path_for(repos_base, name):
    return os.path.join(str(repos_base), f"{PROJECT_CODE}_{_segment(name)}_{REPO_ID}")


def _segment(name):
    """镜像 utils/path_security.py:_sanitize_segment 的结果。

    刻意**重新实现**而不是调它：如果哪天有人放宽了那个函数（例如让中文进目录），
    这里会立刻不匹配而失败 —— 那正是本文件要拦的改动。
    """
    import re

    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(name))
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned or "repository"


def _deps(*, session=None, flashes=None):
    flashes = flashes if flashes is not None else []
    redirects = []

    def _flash(message, category):
        flashes.append((category, str(message)))

    def _redirect(target):
        redirects.append(target)
        return {"redirect": target}

    def _url_for(endpoint, **kwargs):
        return f"/{endpoint}/{kwargs.get('repository_id', '')}"

    if session is None:
        class _Session:
            def rollback(self):
                pass

            def commit(self):
                pass

        session = _Session()

    return dict(
        redirect=_redirect,
        url_for=_url_for,
        flash=_flash,
        db=SimpleNamespace(session=session),
        validate_repository_name=lambda name: bool(name) and "/" not in name and " " not in name,
        normalize_repository_name=lambda name: str(name or "").strip(),
        repository_name_error_message="仓库名称不合法",
        relocate_repository_local_dir=relocate_repository_local_dir,
        undo_repository_local_dir_move=undo_repository_local_dir_move,
        BLOCKING_REASONS=BLOCKING_REASONS,
        REASON_MOVED=REASON_MOVED,
        log_print=lambda *_a, **_k: None,
        create_auto_sync_task=lambda *_a, **_k: None,
        app=SimpleNamespace(app_context=lambda: nullcontext()),
        Commit=SimpleNamespace(),
        Repository=SimpleNamespace(),
        DiffCache=SimpleNamespace(),
        clear_repository_state_for_switch_func=lambda **_k: {},
    ), flashes, redirects


def _repo(name, project_code=PROJECT_CODE):
    """一个字段够用的 git 仓库对象。

    `server_url` / `branch` 是必须给的：改名的 git 分支会读它们
    （services/repository_update_form_service.py 的 `_field_changed("url", ...)`），
    缺了会在改名**之后**抛 AttributeError，于是测试看到的是「异常」而不是
    「目录没搬」—— 那会掩盖真正要验的行为。
    """
    return SimpleNamespace(
        id=REPO_ID,
        project_id=20,
        project=SimpleNamespace(code=project_code),
        type="git",
        name=name,
        url="git@example.com:x.git",
        server_url=None,
        branch="main",
        current_version=None,
        path_regex=None,
        display_order=0,
    )


def _run(repos_base, *, old_name, new_name, session=None, extra_form=None):
    repo = _repo(old_name)
    form = {"name": new_name, "display_order": "0", "category": "", "resource_type": "",
            "url": repo.url, "server_url": "", "token": "", "branch": "main"}
    form.update(extra_form or {})
    deps, flashes, redirects = _deps(session=session)
    result = update_form_service.handle_update_repository_form(
        repository=repo, request=SimpleNamespace(form=form), **deps)
    return repo, flashes, redirects, result


# ---------------------------------------------------------------------------
#  真的搬目录
# ---------------------------------------------------------------------------

class TestRenameMovesTheWorkingCopy:
    def test_ascii_rename_moves_the_directory(self, repos_base):
        """回归用例：这正是原来会把工作副本留成孤儿的那一步。"""
        old_path = _path_for(repos_base, "cfg_v1")
        os.makedirs(old_path)
        open(os.path.join(old_path, "marker.txt"), "w").close()

        repo, flashes, _redirects, _result = _run(
            repos_base, old_name="cfg_v1", new_name="cfg_v2")

        new_path = _path_for(repos_base, "cfg_v2")
        assert os.path.isdir(new_path), (
            f"改名后工作副本没有跟过去：{new_path} 不存在 —— "
            "平台会判定为「未克隆」并重新 clone 一份，旧目录成孤儿"
        )
        assert not os.path.isdir(old_path), f"旧目录还留在 {old_path}"
        assert os.path.isfile(os.path.join(new_path, "marker.txt")), "内容没跟着搬过去"
        assert repo.name == "cfg_v2"
        assert not [f for f in flashes if f[0] == "error"], f"不该报错：{flashes}"

    def test_chinese_to_chinese_rename_is_a_noop_on_disk(self, repos_base):
        """两个中文名都 sanitize 成 fallback `repository`，路径相同 ——
        这是最常见的情形，必须无副作用，且不能报错。"""
        old_path = _path_for(repos_base, "中文甲")
        os.makedirs(old_path)

        repo, flashes, _redirects, _result = _run(
            repos_base, old_name="中文甲", new_name="中文乙")

        assert os.path.isdir(old_path), "路径没变，目录不该被动过"
        assert repo.name == "中文乙", "显示名仍然要改"
        assert not [f for f in flashes if f[0] == "error"], f"同一个路径不该报错：{flashes}"

    def test_rename_without_a_clone_succeeds(self, repos_base):
        """还没克隆过的仓库改名不该被拦住 —— 没有东西会被孤儿化。"""
        repo, flashes, _redirects, _result = _run(
            repos_base, old_name="never_cloned", new_name="still_not_cloned")

        assert repo.name == "still_not_cloned"
        assert not [f for f in flashes if f[0] == "error"], f"不该报错：{flashes}"

    def test_chinese_name_lands_in_an_ascii_directory(self, repos_base):
        """中文名不能进目录名：磁盘上必须是纯 ASCII（用户确认的口径）。
        顺带钉住唯一性靠 `_{id}`，而不是靠名字 —— 两个中文名折叠成同一个
        fallback 段也不会互相覆盖。"""
        os.makedirs(_path_for(repos_base, "cfg_v1"))

        _run(repos_base, old_name="cfg_v1", new_name="中文配置库")

        entries = os.listdir(str(repos_base))
        assert entries, "应该有一个目录"
        for entry in entries:
            assert entry.isascii(), f"目录名出现了非 ASCII 字符：{entry!r}"
            assert entry.endswith(f"_{REPO_ID}"), f"目录名丢掉了 id 后缀：{entry!r}"
        # 中文名折叠成 fallback 段，所以目录名里不会出现任何中文
        assert not any("一" <= ch <= "鿿" for ch in entries[0])


# ---------------------------------------------------------------------------
#  拦住改名
# ---------------------------------------------------------------------------

class TestBlockedRenameKeepsTheNameUnchanged:
    """被拦住时，**库里**的名字必须保持不变。

    这里不去断言 `repo.name`：它是没挂在 session 上的 SimpleNamespace，
    Python 属性赋值不受 `rollback()` 影响。真实运行时 `repository` 是 session
    里的 ORM 对象，`rollback()` 会把它 expire 掉，下次访问属性时重新从库里读，
    于是拿到的仍是旧名字 —— 那正是「不落库」的实现方式。所以这里断言**可观察的
    契约**：handler 中途返回（redirect 回编辑页而不是配置页）、有错误提示、
    session 被 rollback、两份目录都没被动过。
    """

    def test_existing_target_blocks_the_rename(self, repos_base):
        """目标目录已存在时不能改名：既不能覆盖它，也不能假装搬成功。"""
        old_path = _path_for(repos_base, "cfg_v1")
        new_path = _path_for(repos_base, "cfg_v2")
        os.makedirs(old_path)
        os.makedirs(new_path)
        open(os.path.join(new_path, "someone_elses.txt"), "w").close()

        _repo_obj, flashes, redirects, _result = _run(
            repos_base, old_name="cfg_v1", new_name="cfg_v2")

        assert redirects == ["/edit_repository/7"], (
            f"被拦住时应停在编辑页让用户处理，实际去了 {redirects}")
        assert os.path.isdir(old_path) and os.path.isfile(
            os.path.join(new_path, "someone_elses.txt")), "两份副本都不能被删"
        errors = [m for c, m in flashes if c == "error"]
        assert errors and new_path in errors[0], (
            f"提示里要给出目标路径，用户才知道去哪处理：{errors}")

    def test_oserror_blocks_the_rename(self, repos_base, monkeypatch):
        """目录被占用（Windows 上很常见）时放弃改名，并说明可能原因。"""
        os.makedirs(_path_for(repos_base, "cfg_v1"))

        def _boom(_old, _new):
            raise OSError(32, "The process cannot access the file")

        monkeypatch.setattr(os, "rename", _boom)

        _repo_obj, flashes, redirects, _result = _run(
            repos_base, old_name="cfg_v1", new_name="cfg_v2")

        assert redirects == ["/edit_repository/7"], "被拦住时应停在编辑页"
        errors = [m for c, m in flashes if c == "error"]
        assert errors and "占用" in errors[0], f"提示要说清可能原因：{errors}"

    def test_blocked_rename_rolls_back_the_session(self, repos_base):
        """拦住时必须 rollback：此时会话里已经有 repository.name = new_name 了。"""
        os.makedirs(_path_for(repos_base, "cfg_v1"))
        os.makedirs(_path_for(repos_base, "cfg_v2"))

        calls = {"rollback": 0}

        class _Session:
            def rollback(self):
                calls["rollback"] += 1

            def commit(self):
                raise AssertionError("被拦住时不该走到 commit")

        _run(repos_base, old_name="cfg_v1", new_name="cfg_v2", session=_Session())

        assert calls["rollback"] == 1, "拦住改名必须 rollback，否则脏会话可能落库"


# ---------------------------------------------------------------------------
#  commit 失败 → 把目录搬回去
# ---------------------------------------------------------------------------

class TestCommitFailureMovesTheDirectoryBack:
    def test_directory_is_restored_when_commit_fails(self, repos_base):
        """目录已经搬过去了、但名字没落库 —— 路径会重算回旧位置，
        于是又变成「未克隆」。必须搬回去。

        这里同时断言「回退被真的尝试过」：只断言「目录在旧位置」在
        「压根没搬过」的实现上也会通过（那样这条用例就白写了）。
        """
        from sqlalchemy.exc import SQLAlchemyError

        old_path = _path_for(repos_base, "cfg_v1")
        new_path = _path_for(repos_base, "cfg_v2")
        os.makedirs(old_path)

        undo_calls = []

        def _recording_undo(*, old_path, new_path):
            undo_calls.append((old_path, new_path))
            return undo_repository_local_dir_move(old_path=old_path, new_path=new_path)

        class _Session:
            def rollback(self):
                pass

            def commit(self):
                raise SQLAlchemyError("boom")

        repo = _repo("cfg_v1")
        form = {"name": "cfg_v2", "display_order": "0", "category": "", "resource_type": "",
                "url": repo.url, "server_url": "", "token": "", "branch": "main"}
        deps, flashes, _redirects = _deps(session=_Session())
        deps["undo_repository_local_dir_move"] = _recording_undo
        update_form_service.handle_update_repository_form(
            repository=repo, request=SimpleNamespace(form=form), **deps)

        assert undo_calls == [(old_path, new_path)], (
            f"commit 失败后必须尝试把目录搬回去，实际 {undo_calls}")
        assert os.path.isdir(old_path), (
            f"commit 失败后目录应该被搬回 {old_path}，否则库里的旧名字指向一个空位置")
        assert not os.path.isdir(new_path)
        errors = [m for c, m in flashes if c == "error"]
        assert errors and "更新仓库失败" in errors[0]

    def test_undo_failure_is_reported_not_swallowed(self, repos_base, monkeypatch):
        """搬不回去时必须如实告知 —— 绝不假装没事。"""
        from sqlalchemy.exc import SQLAlchemyError

        os.makedirs(_path_for(repos_base, "cfg_v1"))

        class _Session:
            def rollback(self):
                pass

            def commit(self):
                raise SQLAlchemyError("boom")

        real_rename = os.rename

        def _flaky(src, dst):
            # 第一次（正向迁移）成功，第二次（搬回去）失败
            if _path_for(repos_base, "cfg_v2") == dst:
                return real_rename(src, dst)
            raise OSError(32, "locked")

        monkeypatch.setattr(os, "rename", _flaky)

        _repo_obj, flashes, _redirects, _result = _run(
            repos_base, old_name="cfg_v1", new_name="cfg_v2", session=_Session())

        errors = " ".join(m for c, m in flashes if c == "error")
        assert "未能回退" in errors and "cfg_v2" in errors, (
            f"回退失败必须明说工作副本现在在哪：{errors}")


# ---------------------------------------------------------------------------
#  relocate_* 自身的分支（纯函数，不经表单）
# ---------------------------------------------------------------------------

class TestRelocateReasonCodes:
    def test_reason_codes_cover_every_outcome(self, repos_base):
        old_path = _path_for(repos_base, "a")
        os.makedirs(old_path)

        assert relocate_repository_local_dir(
            project_code=PROJECT_CODE, old_name="中文甲", new_name="中文乙",
            repository_id=REPO_ID)["reason"] == REASON_SAME_PATH

        assert relocate_repository_local_dir(
            project_code=PROJECT_CODE, old_name="ghost", new_name="x",
            repository_id=REPO_ID)["reason"] == REASON_SOURCE_MISSING

        assert relocate_repository_local_dir(
            project_code=PROJECT_CODE, old_name="a", new_name="b",
            repository_id=REPO_ID)["reason"] == REASON_MOVED

        os.makedirs(_path_for(repos_base, "a"))
        os.makedirs(_path_for(repos_base, "c"))
        assert relocate_repository_local_dir(
            project_code=PROJECT_CODE, old_name="a", new_name="c",
            repository_id=REPO_ID)["reason"] == REASON_TARGET_EXISTS

    def test_only_two_reasons_are_blocking(self):
        """拦住改名的只有两种：目标已存在、搬不动。
        「路径相同」「还没克隆」都必须放行 —— 否则中文仓库之间改名会被误拦。"""
        assert BLOCKING_REASONS == frozenset({REASON_TARGET_EXISTS, "failed"})
