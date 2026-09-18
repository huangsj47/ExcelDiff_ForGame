# -*- coding: utf-8 -*-
"""AI 取数：`file_content` 的「配表」分支必须真的能跑（services/ai/platform_provider.py）

## 为什么需要这个文件

`PlatformContextProvider.file_content` 用「这个路径是不是配表」来决定把内容渲染成
文本表格还是直接解码成文本：

    from services.excel_cache_service import is_excel_file     # ← 这个模块根本不存在
    if is_excel_file(path):
        rendered = _read_excel_sheets(raw, max_rows=self._max_rows)
        ...

`services/excel_cache_service.py` **不在仓库里**，而 `is_excel_file` 在
`ExcelDiffCacheService` / `WeeklyExcelCacheService` 上都是**实例方法**，
不是模块级函数。于是这条 import 必然抛 `ModuleNotFoundError`，而且它在
`try` 之外（那个 try 只包住取 Git 内容），所以：

* 走不到 `_read_excel_sheets`（真 xlsx 的内容永远拿不到）；
* 抛出的是异常而不是可读说明 —— 与本文件所在模块的契约相反
  （「拿不到」要明说，不能让调用方以为「没有内容」）。

## 分支判定为什么按 openpyxl 的实际能力选（而不是平台的「配表」清单）

`_read_excel_sheets` 用 `openpyxl.load_workbook`，**只认 OOXML 工作簿**
（`.xlsx` / `.xlsm` / `.xltx` / `.xltm`）。所以本分支的判定必须是「openpyxl 读得动吗」：

* 平台的配表清单（`ExcelDiffCacheService.is_excel_file` = `.xlsx/.xls/.xlsm/.xlsb/.csv`）
  比 openpyxl 的能力**宽**。其中 `.csv` 是会踩坑的那个：CSV 本来是纯文本，
  下面的文本分支能原样交给模型完整内容；一旦按「配表」进表格分支，
  `load_workbook` 解析失败 → 模型只会收到「内容无法解析成文本表格」，
  **反而丢掉了本来读得到的内容**（等于把「读到了」说成「读不了」）。
  `.tsv` 同理（且它连平台的配表清单都不在）。
* `.xls` / `.xlsb` 是 OLE/二进制格式，openpyxl 也不支持；它们进文本分支会得到
  下面那条「不是文本也不是配表」的显式说明，不会抛异常。

回归保护：`tests/test_ai_platform_provider.py` 覆盖的是渲染层（`render_diff_payload`），
本文件覆盖的是取数层的这个分支。
"""
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from services.ai.platform_provider import (  # noqa: E402
    PlatformContextProvider,
    _is_openpyxl_workbook,
)
from services.ai.skill_loader import LoadedSkills, SkillDocument  # noqa: E402

COMMIT = 'a' * 40


class TestWorkbookPredicate:
    """判定必须跟 `_read_excel_sheets`（openpyxl）的实际能力对齐。

    锁定这条判断，避免有人「顺手统一成平台的配表清单」而让 CSV/TSV 丢掉可读内容。
    """

    @pytest.mark.parametrize('path', [
        'config/道具表.xlsx', 'config/道具表.XLSX', 'config/道具表.xlsm',
        'config/道具表.xltx', 'config/道具表.xltm',
    ])
    def test_openpyxl_readable_paths_are_workbooks(self, path):
        assert _is_openpyxl_workbook(path) is True, path

    @pytest.mark.parametrize('path', [
        'config/道具表.csv', 'config/道具表.tsv', 'config/道具表.xls',
        'config/道具表.xlsb', 'services/foo.py', 'assets/blob.bin', '', 'noext',
    ])
    def test_everything_else_is_not(self, path):
        assert _is_openpyxl_workbook(path) is False, (
            f'{path!r} 被判成工作簿了。\n'
            f'`.csv`/`.tsv` 走文本分支才能把完整内容交给模型，'
            f'`.xls`/`.xlsb` openpyxl 根本打不开。'
        )


@pytest.fixture()
def provider():
    """只需要能构造出来的 provider —— `_commit_row` / 取内容都会被替换。"""
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
    """把「查提交行 + 从 git 取内容」替换成内存里的字节，只留被测分支。"""
    def _install(content: bytes):
        monkeypatch.setattr(
            PlatformContextProvider, '_commit_row',
            lambda self, commit, path: SimpleNamespace(commit_id=commit, path=path, repository=object()),
        )

        def _get_content(repository, commit_id, path):
            return content

        import services.vcs_content_service as vcs

        monkeypatch.setattr(vcs, 'get_file_content_from_git', _get_content, raising=False)
    return _install


def _xlsx_bytes(rows, sheet_name='道具表'):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = sheet_name
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row, start=1):
            sheet.cell(row=row_index, column=col_index, value=value)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


class TestExcelBranchIsReachable:
    """真 xlsx：必须渲染成文本表格，而不是抛异常、也不是「无法解析」。"""

    def test_real_xlsx_returns_rendered_table(self, provider, fetch):
        fetch(_xlsx_bytes([['ID', '攻击'], ['1001', '30']]))
        got = provider.file_content(COMMIT, 'config/道具表.xlsx')
        assert isinstance(got, str), f'返回的不是文本：{got!r}'
        assert '道具表' in got, f'没有渲染工作表名：{got!r}'
        assert '1001' in got and '攻击' in got, f'没有渲染单元格内容：{got!r}'
        assert '无法解析' not in got, f'真 xlsx 被说成解析不了：{got!r}'

    def test_xlsm_extension_also_renders(self, provider, fetch):
        """`.xlsm` 也是 openpyxl 能读的 OOXML 工作簿。"""
        fetch(_xlsx_bytes([['ID'], ['1001']]))
        got = provider.file_content(COMMIT, 'config/道具表.xlsm')
        assert isinstance(got, str) and '1001' in got, got

    def test_parse_failure_of_a_workbook_is_explicit(self, provider, fetch):
        """扩展名像工作簿但内容坏掉时：给可读说明，**不是**异常，也不是「没有内容」。"""
        fetch(b'not a workbook at all')
        got = provider.file_content(COMMIT, 'config/道具表.xlsx')
        assert isinstance(got, str), f'抛异常/返回 None 都不允许：{got!r}'
        assert got != '', '空串在这个契约里是「确实没有内容」，不能说谎'
        assert '无法解析' in got, f'解析失败必须明说：{got!r}'


class TestNonWorkbookPathsGoToText:
    """`.csv` / `.tsv` / 普通文本：必须**逐行原样**给模型，不能进表格分支。

    「原样」的判定标准是**内容行一字不改地出现在返回里**（带行号，见
    `platform_provider._render_text_content`），不是「返回与文件字节逐字相同」——
    后者会把抬头与行号一起钉死，而那两样正是模型据以定位的证据。
    """

    def _assert_lines_are_intact(self, got, lines):
        """每一行都必须以 `行号│原文` 的形态出现，且顺序不变。"""
        assert isinstance(got, str) and got, f'不允许返回 None/空串：{got!r}'
        for number, text in enumerate(lines, start=1):
            assert f'{number}│{text}' in got, (
                f'第 {number} 行没按「行号│原文」给出来：{text!r}\n完整返回：{got!r}'
            )

    def test_csv_is_returned_as_text(self, provider, fetch):
        fetch('id,name\n1,Alice\n'.encode('utf-8'))
        got = provider.file_content(COMMIT, 'config/道具表.csv')
        assert '无法解析' not in got, (
            f'CSV 被当成工作簿解析了（模型会收到「无法解析」而不是这张表）：{got!r}'
        )
        self._assert_lines_are_intact(got, ['id,name', '1,Alice'])

    def test_tsv_is_returned_as_text(self, provider, fetch):
        fetch('id\tname\n1\tAlice\n'.encode('utf-8'))
        got = provider.file_content(COMMIT, 'config/道具表.tsv')
        assert '无法解析' not in got, f'TSV 被当成工作簿解析了：{got!r}'
        self._assert_lines_are_intact(got, ['id\tname', '1\tAlice'])

    def test_plain_python_file_is_returned_as_text(self, provider, fetch):
        fetch('def f():\n    return 1\n'.encode('utf-8'))
        got = provider.file_content(COMMIT, 'services/foo.py')
        self._assert_lines_are_intact(got, ['def f():', '    return 1'])

    def test_the_window_says_which_lines_it_is_and_how_many_there_are(
        self, provider, fetch
    ):
        """抬头是模型写结论时的坐标系（「第 1180 行」），行号必须从 1 起、总数要对。"""
        fetch(''.join(f'line {i}\n' for i in range(1, 11)).encode('utf-8'))
        got = provider.file_content(COMMIT, 'services/foo.py')
        assert '共 10 行' in got, f'没告诉模型这个文件有多少行：{got!r}'
        assert '第 1–10 行' in got, f'没告诉模型这是哪一段：{got!r}'

    def test_binary_junk_says_it_cannot_be_shown(self, provider, fetch):
        """非文本非配表：明说无法展示，且**不**说成「没有内容」。"""
        fetch(b'\xff\xfe\x00\x01\x02')
        got = provider.file_content(COMMIT, 'assets/blob.bin')
        assert isinstance(got, str) and got != '', f'不允许返回 None/空串：{got!r}'
        assert '没有内容' in got or '无法展示' in got, got


class TestContentEmptyContract:
    """既有契约保持不变：取到空内容 → 空串；取不到 → None。"""

    def test_empty_content_is_empty_string(self, provider, fetch):
        fetch(b'')
        assert provider.file_content(COMMIT, 'config/道具表.xlsx') == ''

    def test_missing_commit_row_is_none(self, provider, monkeypatch):
        monkeypatch.setattr(PlatformContextProvider, '_commit_row', lambda self, c, p: None)
        assert provider.file_content(COMMIT, 'config/道具表.xlsx') is None
