# -*- coding: utf-8 -*-
"""AI 取数：`file_content` 的「文档（.docx）」分支。

## 为什么需要这个文件

`.docx` 是 ZIP，`looks_binary` 认得出（魔数 `PK\\x03\\x04`）—— 于是它原先一律走
「[无法展示的内容] …这份内容的字节不是文本」。而项目里真正有信息量的东西常常正是
这类文件：访谈记录、需求文档、评审纪要。模型看得到文件名、看不到一个字，只能靠猜。

所以两条路（平台本地 / 业务节点）都要在**解码之前**分流，并且用**同一份渲染实现**
（`services/ai/docx_view`）—— 同一次索取在单机与多节点下必须给出同一份文本，
这是 `utils/content_window` 那条纪律（两端各写一遍必然漂移）。

## 这里守的几件事

1. 解析得出来的 docx → 给**文本 + 行号**（与其他文本文件同一套坐标：模型已经会读
   「共 N 行、下面是第 a–b 行」）；
2. 解析不出来的 → 一句**显式的解析失败**，而不是空正文（「解析不了」与「文档是空的」
   是两件事，前者要人去看那个文件）；
3. 被修订删掉的文字（`w:delText`）**不许**混进正文 —— 那会让模型把已经不存在的内容
   当成事实写进结论，比读不到更糟。
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.ai.docx_view import is_docx, render_docx_text  # noqa: E402
from services.ai.platform_provider import PlatformContextProvider  # noqa: E402
from services.ai.skill_loader import LoadedSkills, SkillDocument  # noqa: E402

COMMIT = 'a' * 40
_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def _docx(*blocks: str) -> bytes:
    """造一个最小可读的 .docx（`word/document.xml` 就是全部正文）。"""
    body = ''.join(blocks)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('word/document.xml', document)
        archive.writestr('[Content_Types].xml', '<Types/>')
    return buffer.getvalue()


def _para(text: str, *, style: str = '', numbered: bool = False) -> str:
    properties = ''
    if style:
        properties += f'<w:pStyle w:val="{style}"/>'
    if numbered:
        properties += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
    run = f'<w:r><w:t>{text}</w:t></w:r>'
    return f'<w:p><w:pPr>{properties}</w:pPr>{run}</w:p>'


def _table(*rows: tuple[str, ...]) -> str:
    cells = ''.join(
        '<w:tr>' + ''.join(f'<w:tc>{_para(cell)}</w:tc>' for cell in row) + '</w:tr>'
        for row in rows
    )
    return f'<w:tbl>{cells}</w:tbl>'


# ==========================================================================
# 一、渲染器
# ==========================================================================


class TestTheRenderer:
    def test_paragraphs_headings_and_lists_come_out_structured(self):
        raw = _docx(
            _para('新手体验问题讨论', style='Heading1'),
            _para('会议时间：2026-05-26'),
            _para('首次进入的引导太长', numbered=True),
            _para('', style=''),  # 空段落：丢掉
        )

        text = render_docx_text(raw)

        assert text.splitlines() == [
            '# 新手体验问题讨论',
            '会议时间：2026-05-26',
            '- 首次进入的引导太长',
        ]

    def test_tables_are_rendered_row_by_row(self):
        raw = _docx(_table(('时长', '流失率'), ('30 秒', '12%'), ('2 分钟', '31%')))

        text = render_docx_text(raw)

        assert '（表：3 行 × 2 列）' in text
        assert '| 30 秒 | 12% |' in text

    def test_deleted_revisions_do_not_leak_into_the_text(self):
        """**这条是「宁可少读，不许读错」。**

        Word 把修订删掉的文字放在 `w:delText` 里。把它一起读进来，模型就会把**已经不
        存在的内容**当作正文写成结论 —— 那比「读不到」更糟：读不到它会说「信息不足」，
        读错了它什么都不说。
        """
        raw = _docx(
            '<w:p><w:r><w:t>奖励从 </w:t></w:r>'
            '<w:del><w:r><w:delText>1000</w:delText></w:r></w:del>'
            '<w:r><w:t>2000</w:t></w:r></w:p>'
        )

        text = render_docx_text(raw)

        assert '2000' in text
        assert '1000' not in text, '被修订删掉的文字进了正文'

    @pytest.mark.parametrize('raw', [b'not a zip at all', b'', None])
    def test_unreadable_input_returns_none_or_empty(self, raw):
        result = render_docx_text(raw)
        assert result is None or result == ''

    def test_a_zip_without_the_document_part_is_unreadable(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('word/styles.xml', '<styles/>')

        assert render_docx_text(buffer.getvalue()) is None

    @pytest.mark.parametrize(
        'path,expected',
        [
            ('策划工作台/访谈反馈/记录.docx', True),
            ('config/道具表.xlsx', False),
            ('a.doc', False),  # OLE 老格式：读不了，就该照旧说「无法展示」
            ('a.docx.bak', False),
            ('', False),
        ],
    )
    def test_the_predicate_is_extension_based(self, path, expected):
        assert is_docx(path) is expected


# ==========================================================================
# 二、平台本地那条路
# ==========================================================================


@pytest.fixture()
def provider():
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
def fetch(monkeypatch):
    def _install(content):
        monkeypatch.setattr(
            PlatformContextProvider, '_commit_row',
            lambda self, commit, path, repository_id="": (SimpleNamespace(
                commit_id=commit, path=path, repository=object()
            ), ()),
        )
        import services.vcs_content_service as vcs

        monkeypatch.setattr(vcs, 'get_file_content_from_git', lambda *a, **k: content, raising=False)
    return _install


class TestThePlatformPath:
    def test_a_docx_comes_back_as_numbered_text(self, provider, fetch):
        """**核心性质**：它不再是「无法展示的内容」。"""
        fetch(_docx(_para('新手体验问题讨论', style='Heading1'), _para('首次引导太长')))

        text = provider.file_content(COMMIT, '策划工作台/访谈反馈/记录.docx')

        assert '无法展示' not in text
        assert '新手体验问题讨论' in text
        assert '1│' in text or '1 |' in text, f'没有行号：{text[:120]!r}'

    def test_a_broken_docx_says_so_instead_of_returning_nothing(self, provider, fetch):
        fetch(b'PK\x03\x04 but not really a zip')

        text = provider.file_content(COMMIT, '策划工作台/访谈反馈/记录.docx')

        assert '[文档解析失败]' in text
        assert '不等于「没有内容」' in text

    def test_the_window_still_works_on_the_rendered_text(self, provider, fetch):
        """模型点名要哪一段时，切的是**渲染后**的行 —— 与其他文本文件同一套坐标。"""
        fetch(_docx(*(_para(f'第 {index} 段') for index in range(1, 21))))

        text = provider.file_content(COMMIT, 'notes.docx', lines='5-7')

        assert '第 5 段' in text and '第 7 段' in text
        assert '第 8 段' not in text


# ==========================================================================
# 三、业务节点（Agent）那条路 —— 两端必须给出同一份文本
# ==========================================================================


class TestTheAgentPath:
    def _read(self, monkeypatch, content, path='策划工作台/记录.docx'):
        import services.agent_file_content_reader as reader
        import services.vcs_content_service as vcs

        monkeypatch.setattr(
            reader.db, 'session',
            SimpleNamespace(get=lambda model, key: SimpleNamespace(id=key)),
            raising=False,
        )
        monkeypatch.setattr(vcs, 'get_file_content_from_git', lambda *a, **k: content, raising=False)
        return reader.read_file_content_for_agent({
            'repository_id': 1, 'commit_id': COMMIT, 'file_path': path,
        })

    def test_the_agent_renders_the_same_text(self, monkeypatch):
        content = _docx(_para('新手体验问题讨论', style='Heading1'), _para('首次引导太长'))

        result = self._read(monkeypatch, content)

        assert '无法展示' not in result['content']
        assert '新手体验问题讨论' in result['content']
        assert result.get('total_lines'), '没有按行给窗口（与其他正文来源不一致）'

    def test_the_agent_refuses_a_broken_docx_out_loud(self, monkeypatch):
        with pytest.raises(RuntimeError, match='无法解析'):
            self._read(monkeypatch, b'PK\x03\x04 nope')
