# -*- coding: utf-8 -*-
"""WP11：配表（xlsx）也能被 `find_references` 搜到。

## 这一组钉的是哪一句**错误的「没有」**

`reference_index` 原先在 `is_binary` 那一处把配表整类跳过（xlsx 的字节是 zip 容器），
`repo_reference` 的覆盖账甚至写着「配表/生成物等，没搜」。于是「`Item_1001` 还被哪张表
引用」这个问题只能靠 `file_content` 一张张表去猜 —— 而配表单条上限只有 11,000 字符，
猜不完。报告里于是出现一句「已覆盖的范围内没有其它引用」，而它其实是「配表根本没搜」。

## 这一组守的五条性质

1. **真的能搜到**：临时工作簿里的 `Item_1001` / `CfgRewardMode` 建索引后能命中，且命中
   带着**路径 + 行号**，行号旁边写着「第几张表、表内第几行」（这个坐标要能回到表里核对）；
2. **两条生产路径都算数**：本批次检索（`PlatformContextProvider.find_references`）与冻结
   仓库检索（`repo_reference.search_frozen_repository`）；
3. **限制是真的**：> 5 MB 跳过、第 4 张表 / 第 201 行不索引，且**不抛异常**（宁可少索引
   一条，也不能让整批索引在这一行炸掉）；
4. **真正的二进制一个字都没变**：图片、压缩包、openpyxl 打不开的 `.xls` 仍然计入
   `binary`（= 没搜，抬头要说出来），不是被静默吞掉；打不开的 `.xlsx` 也退回这一档；
5. **240 文件的预热门槛仍然管着配表**：xlsx 也占名额，排在门槛之外的照样算 `unindexed`。

## 为什么断言的是「账」而不只是「文本里有那个词」

`scanned` / `binary` / `oversized` / `unindexed` 是模型判断「能不能下否定结论」的唯一依据，
也是这个工具最容易骗人的地方：漏掉一类缺口，「没搜到」就被读成「不存在」。只断言
「命中文本里有 `Item_1001`」的话，一个把配表**整类**静默跳过的实现也能让别的用例变绿 ——
所以这里的每一条都同时看命中与缺口账。
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from services.ai.reference_index import (
    MAX_INDEX_FILES,
    XLSX_MAX_INDEX_BYTES,
    XLSX_MAX_INDEX_ROWS,
    XLSX_MAX_INDEX_SHEETS,
    SnapshotReferenceIndex,
)
from services.ai.repo_reference import search_frozen_repository

COMMIT = "a" * 40
XLSX_PATH = "config/[30]道具表_CfgItem.xlsx"
TIP = "f" * 40


# ---------------------------------------------------------------------------
#  样本与桩
# ---------------------------------------------------------------------------


def _workbook_bytes(sheets: dict) -> bytes:
    """在内存里造一份**真的**工作簿（`openpyxl` 写的字节，不是手搓的 zip）。

    直接用 openpyxl 写而不是拼字节：这几条用例测的是「真实配表能不能被索引」，
    手搓的样本一旦格式不合法，测的就变成了「异常分支」。
    """
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(title=name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _reader(files: dict):
    def read(path, commit):
        return files.get((path, commit))

    return read


class _FrozenReader:
    """`repo_reference` 要的最小读取器：`tracked_paths()` + `read_bytes(path)` + `frozen`。"""

    def __init__(self, files: dict):
        self.files = dict(files)
        self.frozen = SimpleNamespace(repository_id=7, name="配置仓", tip=TIP)

    def tracked_paths(self):
        return tuple(self.files)

    def read_bytes(self, path):
        return self.files.get(path)


def _missing_account(result) -> tuple:
    """四本缺口账（一个字都不能少、也不能互相顶替）。"""
    return (result.binary, result.oversized, result.undecodable, result.unindexed)


# ---------------------------------------------------------------------------
#  一、索引层：表内文本真的进了索引
# ---------------------------------------------------------------------------


def test_a_config_id_inside_a_workbook_is_found_with_its_path_and_row(tmp_path):
    """**WP11 的核心验收**：真的造一份临时 xlsx（两行 `Item_1001` / `CfgRewardMode`），
    索引之后 `find_references` 能命中它，而且命中带着能回表核对的坐标。

    行号取表里的**物理行号**、再加一句「第几张表」：`file_content` 的 `lines` 对配表表示
    「第几张表」，只给索引里的序号的话，两条路对不上。
    """
    from openpyxl import Workbook

    workbook_path = tmp_path / "goods.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "道具表"
    sheet.append(["Item_1001"])
    sheet.append(["CfgRewardMode"])
    book.save(workbook_path)
    raw = workbook_path.read_bytes()

    index = SnapshotReferenceIndex.build([(XLSX_PATH, COMMIT)], reader=lambda _path, _commit: raw)
    result = index.search("Item_1001")

    assert [(hit.path, hit.line) for hit in result.hits] == [(XLSX_PATH, 1)]
    assert "Item_1001" in result.hits[0].text
    assert "第1张表" in result.hits[0].text and "第1行" in result.hits[0].text, (
        "命中要能回到表里核对（表序号 + 物理行号）"
    )
    assert result.scanned == 1 and result.files_total == 1
    assert _missing_account(result) == (0, 0, 0, 0), (
        f"配表进了索引，就不该同时出现在任何一本缺口账里：{_missing_account(result)}"
    )


def test_the_second_row_is_indexed_and_points_at_row_two():
    raw = _workbook_bytes({"道具表": [["Item_1001"], ["CfgRewardMode"]]})

    index = SnapshotReferenceIndex.build([(XLSX_PATH, COMMIT)], reader=lambda _path, _commit: raw)
    hits = index.search("CfgRewardMode").hits

    assert [hit.line for hit in hits] == [2]
    assert "第2行" in hits[0].text


@pytest.mark.parametrize("path", ["config/goods.xlsx", "config/goods.xlsm"])
def test_both_workbook_extensions_are_indexed(path):
    """`.xlsx` 与 `.xlsm` 都是 openpyxl 读得动的 OOXML 工作簿（扩展名判定只有一处）。"""
    raw = _workbook_bytes({"道具表": [["Item_1001"]]})

    index = SnapshotReferenceIndex.build([(path, COMMIT)], reader=lambda _path, _commit: raw)

    assert [(hit.path, hit.line) for hit in index.search("Item_1001").hits] == [(path, 1)]


def test_the_sheet_index_is_part_of_the_hit_coordinate():
    """命中里要写清是**第几张表**：`file_content` 的 `lines` 对配表就是表序号。"""
    raw = _workbook_bytes({"属性表": [["Item_1001"]], "技能表": [["Item_1001"]]})

    index = SnapshotReferenceIndex.build([(XLSX_PATH, COMMIT)], reader=lambda _path, _commit: raw)
    hits = index.search("Item_1001").hits

    assert [hit.line for hit in hits] == [1, 2], "两张表各有一行，索引按表顺序排"
    assert "第1张表" in hits[0].text and "第2张表" in hits[1].text


# ---------------------------------------------------------------------------
#  二、两条生产路径：`find_references` 真的能拿到这条命中
# ---------------------------------------------------------------------------


def test_the_batch_search_finds_the_workbook_through_find_references(monkeypatch):
    """本批次检索（单机模式）：`find_references` 的命中里出现配表路径与行号。

    这条走的是**生产入口**，不是直接调 `SnapshotReferenceIndex` —— `build()` 建好索引
    但上层没把配表路径带进来的话，用户看到的仍然是「配表没搜」。
    """
    import services.vcs_content_service as vcs
    from services.ai import platform_provider as pp
    from services.ai import provider_search as agent_mode_module
    from services.ai.platform_provider import PlatformContextProvider
    from services.ai.scope import AnalysisScope

    raw = _workbook_bytes({"道具表": [["Item_1001"], ["CfgRewardMode"]]})
    scope = AnalysisScope.from_iterables(
        commits=(COMMIT,), paths_by_commit={COMMIT: [XLSX_PATH]}, readable_references=[]
    )
    files = {(XLSX_PATH, COMMIT): raw}
    monkeypatch.setattr(
        vcs, "get_file_content_from_git", lambda repo, commit, path: files.get((path, commit))
    )
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: False)
    # 部署模式在两个模块里各被问一次（与 `test_ai_find_references` 同一手法）。
    monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: False)
    provider = PlatformContextProvider(loaded=SimpleNamespace(readable={}), scope=scope)
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=1))

    text = provider.find_references("Item_1001")

    assert f"{XLSX_PATH}:1:" in text, f"配表里的 id 没被搜到：{text}"
    assert "第1张表" in text and "第1行" in text
    assert "1/1" in text, "配表要算进「真的读了并建进索引」的那一边"
    assert "不是文本" not in text, "配表已经索引了，覆盖率那句就不该再说它没搜"


def test_the_frozen_repository_search_reaches_into_the_workbook():
    """冻结仓库检索（`repo_reference`）：配表与代码同一个范围、同一份覆盖账。"""
    raw = _workbook_bytes({"道具表": [["Item_1001"]]})
    reader = _FrozenReader({
        XLSX_PATH: raw,
        "scripts/item.lua": b"local item = {id = 'Item_1001'}\n",
    })

    outcome = search_frozen_repository(reader, "Item_1001", cache={})

    assert outcome is not None
    assert (outcome.files_total, outcome.indexed) == (2, 2)
    assert outcome.coverage_complete is True, "两份都读进索引了，覆盖账不许自相矛盾"
    assert f"{XLSX_PATH}:1:" in outcome.text
    assert "scripts/item.lua:1:" in outcome.text, "配表进来之后，代码命中照旧一条不少"
    assert "配表的命中行号是**表内物理行**" in outcome.text, (
        "不提醒这一句，模型会拿 文件:417 去开第 417 张表"
    )


# ---------------------------------------------------------------------------
#  三、限制：大文件 / 超行数 / 超表数 —— 跳过且不抛
# ---------------------------------------------------------------------------


def test_an_oversized_workbook_is_skipped_and_counted_without_raising():
    """> 5 MB 跳过：5 MB 之上基本是嵌入图片或导出的全量备份，读它只会拖死首次查询。

    这里**不用**手搓的合法工作簿去凑 5 MB（那要写几万行，用例会变慢）：大小判定发生在
    解析之前，所以一份 5 MB 的字节就够了 —— 若实现把顺序写反（先解析再判大小），
    这条会以一个**异常**结束，正是要挡的那种回归。
    """
    raw = b"PK\x03\x04" + b"\x00" * (XLSX_MAX_INDEX_BYTES + 1)
    path = "config/huge.xlsx"

    index = SnapshotReferenceIndex.build([(path, COMMIT)], reader=lambda _path, _commit: raw)
    result = index.search("Item_1001")

    assert result.hits == ()
    assert result.oversized == 1 and result.binary == 0, "超限记 oversized，不是 binary"
    assert result.scanned == 0 and result.unindexed == 0


def test_only_the_first_rows_are_indexed_and_the_limit_is_a_boundary():
    """只索引每张表的前 200 行，且第 200 行**在**、第 201 行**不在**（边界要钉住）。"""
    rows = [[f"Item_{1000 + index}"] for index in range(1, XLSX_MAX_INDEX_ROWS + 1)]
    rows.append(["Item_9999"])
    raw = _workbook_bytes({"道具表": rows})

    index = SnapshotReferenceIndex.build([(XLSX_PATH, COMMIT)], reader=lambda _path, _commit: raw)

    assert index.search("Item_9999").hits == (), "第 201 行在限制之外"
    boundary = index.search(f"Item_{1000 + XLSX_MAX_INDEX_ROWS}").hits
    assert [hit.line for hit in boundary] == [XLSX_MAX_INDEX_ROWS], "第 200 行必须在限制之内"


def test_only_the_first_sheets_are_indexed():
    sheets = {f"表{index}": [["Item_1001"]] for index in range(1, XLSX_MAX_INDEX_SHEETS + 1)}
    sheets["表4"] = [["Item_4004"]]
    raw = _workbook_bytes(sheets)

    index = SnapshotReferenceIndex.build([(XLSX_PATH, COMMIT)], reader=lambda _path, _commit: raw)

    assert len(index.search("Item_1001").hits) == XLSX_MAX_INDEX_SHEETS, "前三张表都要索引"
    assert index.search("Item_4004").hits == (), "第 4 张表在限制之外"


# ---------------------------------------------------------------------------
#  四、真正的二进制一个字都没变
# ---------------------------------------------------------------------------


def test_real_binaries_are_still_skipped_and_counted_as_binary():
    """图片/压缩包/OLE 表格仍然「不是文本」：豁免只开给 openpyxl 读得动的工作簿。

    `.xls` 是 OLE（`d0 cf 11 e0`），openpyxl 打不开 —— 它与 `.xlsx` 长得很像，
    按扩展名「像表格就解析」会在这里炸掉整批索引。
    """
    files = {
        ("assets/icon.png", COMMIT): b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
        ("assets/pack.zip", COMMIT): b"PK\x03\x04" + b"\x00" * 64,
        ("config/legacy.xls", COMMIT): b"\xd0\xcf\x11\xe0" + b"\x00" * 64,
    }

    index = SnapshotReferenceIndex.build(list(files), reader=_reader(files))
    result = index.search("Item_1001")

    assert result.hits == ()
    assert result.binary == 3 and result.scanned == 0
    assert _missing_account(result) == (3, 0, 0, 0)


def test_a_corrupt_workbook_falls_back_to_the_binary_account():
    """打不开的 `.xlsx` 退回「不是文本」那一本账 —— 既不静默消失，也不炸索引。"""
    path = "config/broken.xlsx"

    index = SnapshotReferenceIndex.build(
        [(path, COMMIT)], reader=lambda _path, _commit: b"PK\x03\x04 not a real workbook"
    )
    result = index.search("Item_1001")

    assert result.hits == ()
    assert result.binary == 1 and result.scanned == 0
    assert _missing_account(result) == (1, 0, 0, 0)


# ---------------------------------------------------------------------------
#  五、240 文件的预热门槛仍然管着配表
# ---------------------------------------------------------------------------


def test_the_file_cap_still_counts_workbooks():
    """xlsx **也占名额**：排在预热门槛之外就索引不到，且如实算成 `unindexed`。

    这条挡的是「配表单开一条通道、绕过门槛」那种写法：配表仓库里前 240 个文件可能全是
    表，绕过去就等于把首次查询的成本翻倍（而 Agent 侧只等 40 秒）。
    """
    raw = _workbook_bytes({"道具表": [["Item_1001"]]})
    entries = [(f"scripts/f{index:04d}.lua", COMMIT) for index in range(MAX_INDEX_FILES)]
    entries.append((XLSX_PATH, COMMIT))

    index = SnapshotReferenceIndex.build(
        entries,
        reader=lambda path, _commit: raw if path.endswith(".xlsx") else "local x = 1\n",
        max_files=MAX_INDEX_FILES,
    )
    result = index.search("Item_1001")

    assert index.indexed_files == MAX_INDEX_FILES
    assert result.hits == (), "门槛之外的配表不许被索引（它排在 240 个之后）"
    assert result.files_total == MAX_INDEX_FILES + 1
    assert result.unindexed == 1 and result.truncated_files is True
