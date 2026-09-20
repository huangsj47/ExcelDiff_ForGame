# -*- coding: utf-8 -*-
"""同一次取数，**两条部署路径必须给出同一份文本**（编码是唯一实现的那一份）。

## 这一组钉的是什么

平台有两套部署形态读同一份文件的正文：

* **单机模式**：平台自己有工作副本，`PlatformContextProvider.file_content` 直接读；
* **platform/agent 模式**：平台被禁止 clone，正文由业务节点上的
  `services/agent_file_content_reader.read_file_content_for_agent` 读回来。

两条路读到的**字节完全相同**，所以文本也必须相同 —— 否则「同一次索取在单机与多节点下
给出不同的结论」，而且**不报错**：模型从正文里写出的行号、取值、结论都会跟着各自那一份走。

修前这条纪律在「行窗口」上守住了（`utils/content_window.slice_lines` 唯一实现），在
「编码」上漏了：

* 平台侧 `raw.decode("utf-8")` 严格解码，失败就回「[无法展示的内容] …不是文本…」——
  **一条假的信息缺口**：GBK 的 lua（中文项目里很常见）明明读得到，模型却被告知读不到，
  于是把「读得到但没解码」写成「这里没有内容」；
* Agent 侧 `raw.decode('utf-8', errors='replace')` —— 同一串字节给出另一份文本（乱码）。

于是同一个文件在「diff 引擎」（`services/diff_service._decode_text` 的五级兜底）、
「AI 平台本地」、「AI Agent」三处是三份不同的文本。

现在三处全部走 `utils.text_decoding`，本文件断言的就是**两条部署路径的一致性**
（不是「某个函数返回什么」—— 那种断言抓不住「另一边又自己写了一份」）。
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.ai.platform_provider import PlatformContextProvider  # noqa: E402
from services.ai.skill_loader import LoadedSkills, SkillDocument  # noqa: E402

COMMIT = 'a' * 40
LUA = 'code/qz_pub/battle/BattleMgr.lua'

# 一份 GBK 的中文 lua：字节流里既有 ASCII 又有汉字。它在按 utf-8 严格解码时必然失败
# （这正是修前平台侧那条「无法展示的内容」被触发的条件），而 gbk 一档解得开。
GBK_LUA = (
    '-- 战斗管理：这一份是 GBK 编码的，和上一个版本的差异在下面\n'
    'local M = {}\n'
    'function M.攻击力(单位)\n'
    '    return 单位.基础攻击 * 2\n'
    'end\n'
    'return M\n'
).encode('gbk')

ASCII_LUA = b'local M = {}\nfunction M.run()\n    return 1\nend\nreturn M\n'

# 「含非法字节的二进制」：按 utf-8 解不出来（修前两侧会分道扬镳），
# 但**没有**魔数也没有 NUL，所以按「能不能解码」判不出来 —— 它是「编码不兼容」还是
# 「二进制」由 `utils.text_decoding.looks_binary` 说了算，两端必须是同一个判据。
BINARY_JUNK = b'\xff\xfe\x80\x81\x9d\x7f\x00not-a-\xff\xfe-workbook'

# 带 NUL 的二进制：两侧都应给「无法展示」那句话，且**逐字相同**。
BINARY_NUL = b'\x00\x01\x02\x03\xff\xfe\xfd'


@pytest.fixture()
def provider():
    """只需能构造出来 —— `_commit_row` 与取内容都会被替换。"""
    doc = SkillDocument(
        name='SKILL.md', description='', path=Path('SKILL.md'), text='平台协议',
        content_hash='h',
    )
    return PlatformContextProvider(
        loaded=LoadedSkills(
            platform_skill=doc,
            platform_references=(),
            project_manifest=None,
            project_references=(),
            project_skills=(),
            readable={},
            project_slug=None,
            revision='rev',
        )
    )


@pytest.fixture()
def platform_read(monkeypatch, provider):
    """平台本地那条路：把「查提交行 + 从 git 取内容」换成内存里的字节。"""
    def _install(content: bytes):
        monkeypatch.setattr(
            PlatformContextProvider, '_commit_row',
            lambda self, commit, path: SimpleNamespace(
                commit_id=commit, path=path, repository=SimpleNamespace(id=1)
            ),
        )
        import services.vcs_content_service as vcs

        monkeypatch.setattr(
            vcs, 'get_file_content_from_git',
            lambda repository, commit_id, path: content,
            raising=False,
        )
        # 点名一个覆盖全文的窗口：不点名会去挑「改动附近」，那要多读一次补丁，
        # 与被测的编码口径无关。
        return provider.file_content(COMMIT, LUA, lines='1-1000')
    return _install


@pytest.fixture(scope='module')
def repository_id():
    """Agent 侧要按 id 查真实的仓库行（`read_file_content_for_agent` 会查库）。"""
    from app import app, create_tables, db
    from models import Project, Repository

    token = uuid.uuid4().hex[:10]
    with app.app_context():
        create_tables()
        project = Project(code=f'ENC{token}', name=f'encoding-{token}')
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id, name=f'repo-enc-{token}', type='git',
            url='https://example.invalid/game.git', branch='main', clone_status='completed',
        )
        db.session.add(repository)
        db.session.commit()
        return repository.id


@pytest.fixture()
def agent_read(monkeypatch, repository_id):
    """Agent 端那条路：`read_file_content_for_agent` 的真实调用。"""
    def _install(content: bytes):
        from app import app

        with app.app_context():
            from services.agent_file_content_reader import read_file_content_for_agent

            monkeypatch.setattr(
                'services.vcs_content_service.get_file_content_from_git',
                lambda repository, commit_id, file_path: content,
            )
            return read_file_content_for_agent({
                'repository_id': repository_id,
                'commit_id': COMMIT,
                'file_path': LUA,
                'lines': '1-1000',
            })
    return _install


def _platform_body(rendered: str):
    """平台本地渲染出来的**正文行**（去掉抬头与 `行号│` 前缀）。

    `_render_text_content` 的形态是「一行抬头 + 若干 `N│原文`」。这里把前缀剥掉再比，
    比的是「模型实际读到的那段文本」——抬头里的出处（平台本地 / Agent）本来就该不同，
    那是**来源**，不是内容。
    """
    head, _, body = rendered.partition('\n')
    assert head.startswith('文件正文：'), (
        f'平台本地那条路没有给出正文（多半是走了失败/无法展示那条分支）：{rendered!r}'
    )
    lines = []
    for line in body.split('\n'):
        lines.append(line.split('│', 1)[1] if '│' in line else line)
    return lines


class TestSameBytesSameText:
    """同一串字节 → 两条部署路径的正文逐行相同。"""

    @pytest.mark.parametrize('raw, label', [
        (GBK_LUA, 'GBK 的中文 lua'),
        (ASCII_LUA, '纯 ASCII'),
    ])
    def test_text_content_is_identical(self, platform_read, agent_read, raw, label):
        local = platform_read(raw)
        agent = agent_read(raw)

        assert local is not None and agent is not None, f'{label}：有一侧什么都没给'
        assert _platform_body(local) == agent['content'].split('\n'), (
            f'{label}：两条路的正文不同。\n'
            f'—— 平台本地 ——\n{local!r}\n—— Agent ——\n{agent.get("content")!r}'
        )

    def test_gbk_decodes_to_readable_chinese(self, platform_read, agent_read):
        """GBK 那一份必须真的解出汉字，而不是替换符 —— 这是「假的信息缺口」的回归点。"""
        text = agent_read(GBK_LUA)['content']

        assert '战斗管理' in text and '攻击力' in text, (
            f'GBK 的 lua 没有被正确解码（模型读到的是乱码，而它会把乱码当内容用）：{text!r}'
        )
        assert '�' not in text, f'出现了替换符 —— 说明走的是 `errors="replace"` 那条路：{text!r}'
        assert '无法展示' not in platform_read(GBK_LUA), (
            '平台本地把一份**读得到**的 GBK 文本说成了「无法展示」——'
            '这就是那条假的信息缺口：模型会把「读到了但没解码」写成「这里没有内容」。'
        )

    def test_binary_bytes_give_the_same_notice(self, platform_read, agent_read):
        """含 NUL 的二进制：两侧给**同一句话**（含「无法展示」，且不等于「没有内容」）。"""
        local = platform_read(BINARY_NUL)
        agent = agent_read(BINARY_NUL)

        assert agent.get('kind') == 'binary', (
            f'Agent 侧把二进制当成文本回传了（平台侧会给「无法展示」，两端于是两份文本）：{agent!r}'
        )
        assert local == agent['content'], (
            f'同一个字节序列在两个部署下给出了不同的话：\n— 平台本地 —\n{local!r}\n'
            f'— Agent —\n{agent.get("content")!r}'
        )
        assert '无法展示' in local and '没有内容' in local, (
            f'这句话必须同时说清「没法用文本核对」与「不等于没有内容」：{local!r}'
        )

    def test_undecodable_junk_is_the_same_verdict_on_both_sides(self, platform_read, agent_read):
        """含非法字节的二进制：两侧给出**同一个判决**，且判决是「不是文本」。

        `BINARY_JUNK` 里既有按 utf-8 非法的字节（`\\xff\\xfe\\x80`），也有 NUL ——
        判据是 `looks_binary`（魔数 / NUL），不是「能不能按 utf-8 解码」。
        """
        local = platform_read(BINARY_JUNK)
        agent = agent_read(BINARY_JUNK)

        assert agent.get('kind') == 'binary', (
            f'Agent 侧把一串二进制当文本回传了：{agent!r}'
        )
        assert local == agent['content'], (
            f'同一个字节序列在两个部署下给出了不同的文本：\n— 平台本地 —\n{local!r}\n'
            f'— Agent —\n{agent.get("content")!r}'
        )

    def test_invalid_utf8_without_nul_is_text_on_both_sides(self, platform_read, agent_read):
        """**只有**「按 utf-8 解不出来」时不算二进制：两侧都给文本，且逐行相同。

        这一条与上一条是一对：判据若退回「utf-8 解不出来就是二进制」，两侧又会分道扬镳
        （平台侧回那句话、Agent 侧回乱码），而中文项目里 GBK 的 lua 正是这个形态。
        """
        raw = '-- 注释：这一段是 GBK\nlocal a = 1\n'.encode('gbk')
        local = platform_read(raw)
        agent = agent_read(raw)

        assert _platform_body(local) == agent['content'].split('\n'), (local, agent)
        assert '注释' in agent['content'], agent


class TestReferenceSearchUsesTheSameJudgement:
    """关键词检索的「是不是文本」判据也必须是这一份。

    修前 `services/ai/reference_search.is_binary` 是「按 utf-8 解不出来就算二进制」，
    于是**GBK 的 lua 一个词都搜不到**：文件被跳过，抬头却只写「N 个不是文本（配表等二进制，
    没搜）」—— 把「编码不兼容」记成了「它是二进制」。而模型正是拿这句话决定能不能说
    「本批次没有别处引用」。
    """

    def test_gbk_source_is_searchable(self):
        from services.ai.reference_search import search_files

        text = 'local 攻击力 = 100\nfunction M.攻击力()\nend\n'
        gbk = text.encode('gbk')

        def reader(path, commit):
            return gbk

        result = search_files([('code/battle.lua', COMMIT)], '攻击力', reader=reader)

        assert result.binary == 0, (
            'GBK 的 lua 被判成二进制跳过了搜索 —— 抬头会把原因写成「它是二进制」'
            f'（binary={result.binary}）'
        )
        assert result.scanned == 1, result
        assert [hit.line for hit in result.hits] == [1, 2], (
            f'GBK 的源码里搜不到中文标识符（命中行：{[h.line for h in result.hits]}）'
        )
        assert '攻击力' in result.hits[0].text, result.hits[0].text

    def test_a_real_zip_is_still_skipped(self):
        from services.ai.reference_search import search_files

        def reader(path, commit):
            return b'PK\x03\x04\xff\xfe\x00binary'

        result = search_files([('config/道具表.xlsx', COMMIT)], 'target', reader=reader)

        assert result.binary == 1 and result.scanned == 0, result

    def test_the_legacy_utf8_verdict_is_gone(self):
        """反向保险：**只有**「编码不兼容」的内容不许再被判成二进制。"""
        from services.ai.reference_search import is_binary

        assert is_binary(GBK_LUA) is False
        assert is_binary('中文'.encode('utf-8')) is False
        assert is_binary(b'PK\x03\x04') is True
        assert is_binary(BINARY_NUL) is True
        assert is_binary(None) is True


class TestTheDecoderItself:
    """`utils.text_decoding` 的契约：**任何**字节都能出文本，绝不 None、绝不抛异常。"""

    def test_never_returns_none_and_never_raises(self):
        from utils.text_decoding import decode_text_bytes

        samples = [b'', None, b'\x00', b'\xff\xfe', GBK_LUA, ASCII_LUA, BINARY_JUNK,
                   bytes(range(256))]
        for raw in samples:
            got = decode_text_bytes(raw)
            assert isinstance(got, str), f'{raw!r} → {got!r}'

    def test_encoding_order_is_part_of_the_contract(self):
        """顺序是口径的一部分：utf-8 优先、GBK 第二、latin-1 兜底。"""
        from utils.text_decoding import TEXT_ENCODINGS, decode_text_bytes

        assert list(TEXT_ENCODINGS) == ['utf-8', 'gbk', 'gb2312', 'latin-1', 'cp1252']
        assert decode_text_bytes('中文'.encode('gbk')) == '中文'
        assert decode_text_bytes('中文'.encode('utf-8')) == '中文'
        # 只有 latin-1 能解的一串字节：不能返回空、不能抛
        assert decode_text_bytes(b'\x81\x8d\x8f\x90\x9d') != ''

    def test_looks_binary_is_not_a_utf8_test(self):
        """**这一条是 reference_search 那个 bug 的判据**：编码不兼容 ≠ 二进制。"""
        from utils.text_decoding import looks_binary

        assert looks_binary(GBK_LUA) is False, (
            'GBK 的 lua 被判成了二进制 —— 于是关键词检索会跳过它，'
            '抬头却只写「不是文本（配表等二进制，没搜）」。'
        )
        assert looks_binary(b'PK\x03\x04') is True
        assert looks_binary(b'\x89PNG\r\n') is True
        assert looks_binary(BINARY_NUL) is True
        assert looks_binary(b'') is False
