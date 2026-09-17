# -*- coding: utf-8 -*-
"""改名后的文件，要能拿改名前的路径去基线版本里读到内容。

## 缺陷形态（线上实测）

配表改名很常见（`【40】怪物表_Object_物件.xlsx` → 加「——废弃」后缀、名字里换个字）。
改名之后：

* 页头「对比版本」是对的 —— 平台顺着改名找到了旧名最后一次改动的那条提交；
* 但**读内容用的是新路径**，而这个路径在基线版本里还不存在 → 取到空 →
  上层把「上一版什么都没有」当成事实，整份表被渲染成「新增工作表」。

线上 6140（`config/60_skill/【62】Buff表——废弃.xlsx`）：8e269af8 只做了一次改名，
把旧名基线和新名文件逐格比，真实差异是 **5 个单元格**；页面写的是「257 行全部新增」，
`summary.modified = 0`。评审者会以为这 257 行是相对 f027b5fb 新加的。

修法见 `services/vcs_content_service.py::_find_rename_source`：按新路径取不到时，
顺 `git log --follow` / `diff-tree -M` 把旧名找出来，按旧名读基线内容。
"""
from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

OLD_PATH = 'config/60_skill/Buff表.xlsx'
NEW_PATH = 'config/60_skill/Buff表——废弃.xlsx'
CONTENT = b'PK\x03\x04 fake-xlsx-payload-for-rename-test'


def _git(repo_dir, *args):
    return subprocess.run(['git', '-C', repo_dir, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def renamed_repo(tmp_path, monkeypatch):
    """建一个真仓库：先有旧名，再改名，之后旧名最后一次改动作为基线。

    返回 (repository_stub, 基线 sha, 改名提交 sha)。
    """
    import git

    repo_dir = tmp_path / 'repo'
    repo_dir.mkdir()
    _git(str(repo_dir), 'init', '-q', '-b', 'main')
    _git(str(repo_dir), 'config', 'user.email', 't@example.com')
    _git(str(repo_dir), 'config', 'user.name', 'tester')

    (repo_dir / 'config' / '60_skill').mkdir(parents=True)
    target = repo_dir / 'config' / '60_skill' / 'Buff表.xlsx'
    target.write_bytes(CONTENT)
    _git(str(repo_dir), 'add', '-A')
    _git(str(repo_dir), 'commit', '-q', '-m', '加入 Buff 表')

    # 旧名最后一次改动 —— 这就是页头「对比版本」那一版
    target.write_bytes(CONTENT + b'-edited')
    _git(str(repo_dir), 'add', '-A')
    _git(str(repo_dir), 'commit', '-q', '-m', '改一格')
    baseline_sha = _git(str(repo_dir), 'rev-parse', 'HEAD')

    # 改名（提交信息与线上同形）
    _git(str(repo_dir), 'mv', OLD_PATH, NEW_PATH)
    _git(str(repo_dir), 'commit', '-q', '-m', '迁移到新的编辑方式')
    rename_sha = _git(str(repo_dir), 'rev-parse', 'HEAD')

    # 之后旧名的那条历史仍在（HEAD 上只有新名）
    repository = SimpleNamespace(id=1, type='git', name='r', local_path=str(repo_dir))
    monkeypatch.setattr(
        'services.vcs_content_service.get_git_service',
        lambda _repo: SimpleNamespace(local_path=str(repo_dir)),
    )
    return repository, baseline_sha, rename_sha


class TestRenameAwareBaselineContent:
    def test_reads_the_old_path_when_the_new_path_is_not_there_yet(self, renamed_repo):
        from services.vcs_content_service import get_file_content_from_git

        repository, baseline_sha, _ = renamed_repo
        content = get_file_content_from_git(repository, baseline_sha, NEW_PATH)

        assert content == CONTENT + b'-edited', (
            "基线版本里这个文件还叫旧名，按新路径取内容会给空 —— "
            "上一层会把空内容当成「上一版什么都没有」，整份表显示成「新增工作表」"
        )

    def test_the_normal_path_still_reads_directly(self, renamed_repo):
        from services.vcs_content_service import get_file_content_from_git

        repository, baseline_sha, _ = renamed_repo
        assert get_file_content_from_git(repository, baseline_sha, OLD_PATH) == CONTENT + b'-edited'

    def test_a_path_that_never_existed_still_returns_none(self, renamed_repo):
        """别为了修改名把「真的没有这个文件」也变成「有内容」。"""
        from services.vcs_content_service import get_file_content_from_git

        repository, baseline_sha, _ = renamed_repo
        assert get_file_content_from_git(repository, baseline_sha, 'config/从来没有过.xlsx') is None

    def test_rename_commit_itself_resolves_to_the_old_name(self, renamed_repo):
        """基线正好是改名那次提交时，新路径同样取不到，也要回退到旧名。"""
        from services.vcs_content_service import get_file_content_from_git

        repository, _, rename_sha = renamed_repo
        content = get_file_content_from_git(repository, rename_sha, NEW_PATH)
        assert content in (CONTENT, CONTENT + b'-edited')
