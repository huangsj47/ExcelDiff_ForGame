# -*- coding: utf-8 -*-
"""平台取数的渲染层。

这里最要紧的一条：**「拿不到」不能说成「没有改动」**。

平台几乎所有取数函数失败都返回 `None`，而 `ContextProvider` 契约里 `""` 是「确实没有
内容」、`None` 是「拿不到」。混起来用，模型会把「我们没读到」读成「这里没问题」，然后
给出一条建立在读取失败之上的、看起来很确定的结论。所以下面每一条「失败」的分支都必须
渲染成**明说失败**的文本。
"""
from __future__ import annotations

from services.ai.platform_provider import (
    DEFAULT_MAX_ROWS_PER_SHEET,
    PlatformContextProvider,
    render_diff_payload,
)
from services.ai.skill_loader import LoadedSkills, SkillDocument

TABLE = "config/[30]道具表_CfgItem.xlsx"


def _excel(sheets=None, **overrides) -> dict:
    payload = {
        "type": "excel",
        "file_path": TABLE,
        "sheets": sheets
        if sheets is not None
        else {
            "道具表": {
                "rows": [
                    {"row_number": 12, "status": "added", "data": {"ID": "1001", "攻击": "30"}},
                    {
                        "row_number": 45,
                        "status": "modified",
                        "data": {"ID": "1200"},
                        "cell_changes": [{"column": "攻击", "old_value": "30", "new_value": "45"}],
                    },
                ],
                "stats": {"added": 1, "removed": 0, "modified": 1},
            }
        },
        "summary": {"added": 1, "removed": 0, "modified": 1, "total": 2},
    }
    payload.update(overrides)
    return payload


# ==========================================================================
# Excel：结构化差异必须变成读得懂的文本
# ==========================================================================


def test_an_excel_diff_is_rendered_as_rows_not_as_a_binary_notice():
    """**这条是这个模块存在的理由。**

    配表改动的主战场是 Excel。把「二进制文件无法显示差异内容」交给模型，等于告诉它
    「这里没什么可看的」，而它必须从「哪张表、哪一行、哪个字段从什么变成什么」里
    才能看出「ID 被删了但代码还在用」这类问题。
    """
    text = render_diff_payload(_excel())

    assert "道具表" in text
    assert "第 12 行" in text and "ID=1001" in text
    assert "攻击: 30 → 45" in text
    assert "二进制" not in text
    assert "无法显示" not in text


def test_row_statuses_are_translated():
    text = render_diff_payload(_excel())

    assert "[新增]" in text
    assert "[修改]" in text


def test_unchanged_rows_are_left_out():
    """整表几千行时，未变的行是纯噪音，而且会把真正变化的行挤出单条上限。"""
    text = render_diff_payload(
        _excel(
            {
                "S": {
                    "rows": [
                        {"row_number": 1, "status": "unchanged", "data": {"ID": "1"}},
                        {"row_number": 2, "status": "added", "data": {"ID": "2"}},
                    ],
                    "stats": {"added": 1},
                }
            }
        )
    )

    assert "ID=2" in text
    assert "ID=1" not in text


def test_an_empty_cell_is_shown_as_empty_not_as_none():
    """`None` 在配表里就是「这个格子是空的」，而**空格子被改成有值**（或反过来）是
    一类真实缺陷（漏配、错行）。渲染成 `None` 会让模型以为那是字面文本。"""
    text = render_diff_payload(
        _excel(
            {
                "S": {
                    "rows": [
                        {
                            "row_number": 3,
                            "status": "modified",
                            "cell_changes": [
                                {"column": "名称", "old_value": "匕首", "new_value": None}
                            ],
                        }
                    ],
                    "stats": {"modified": 1},
                }
            }
        )
    )

    assert "名称: 匕首 → （空）" in text


def test_rows_beyond_the_cap_are_omitted_and_counted():
    rows = [
        {"row_number": index, "status": "added", "data": {"ID": str(index)}} for index in range(200)
    ]
    text = render_diff_payload(_excel({"S": {"rows": rows, "stats": {"added": 200}}}))

    assert f"另有 {200 - DEFAULT_MAX_ROWS_PER_SHEET} 行差异未列出" in text


def test_a_very_long_cell_is_clipped():
    text = render_diff_payload(
        _excel(
            {
                "S": {
                    "rows": [{"row_number": 1, "status": "added", "data": {"备注": "长" * 500}}],
                    "stats": {"added": 1},
                }
            }
        )
    )

    assert "…" in text
    assert "长" * 500 not in text


def test_a_deleted_worksheet_is_called_out():
    text = render_diff_payload({"type": "excel", "file_path": TABLE, "sheets": {"S": {"operation": "deleted"}}})

    assert "该工作表已被删除" in text


def test_a_new_worksheet_is_called_out():
    text = render_diff_payload(
        {"type": "excel", "file_path": TABLE, "sheets": {"S": {"operation": "added", "rows": []}}}
    )

    assert "新增" in text


def test_a_parse_failure_is_never_rendered_as_no_change():
    """解析失败与「没有改动」是**两件相反的事**。把它渲染成一张空表，模型就会说
    「这张表本次没有改动」—— 而它其实是我们没解析出来。"""
    text = render_diff_payload(
        {"type": "excel", "file_path": TABLE, "error": True, "message": "Excel文件处理失败: bad zip"}
    )

    assert "解析失败" in text
    assert "bad zip" in text
    assert "不等于" in text


def test_an_excel_file_that_was_deleted_is_called_out():
    text = render_diff_payload({"type": "excel", "file_path": TABLE, "operation": "deleted"})

    assert "已被删除" in text
    assert "存档" in text, "删除整张表的风险点是老存档仍引用，要点出来"


# ==========================================================================
# 代码 / 二进制 / 出错
# ==========================================================================


def test_a_code_diff_keeps_the_patch():
    text = render_diff_payload({"type": "code", "file_path": "src/a.lua", "patch": "+ 新增一行"})

    assert "+ 新增一行" in text
    assert "src/a.lua" in text


def test_a_binary_file_says_it_cannot_be_shown_but_did_change():
    """**这条是最容易写错的一条。** 二进制文件确实变了，只是我们展示不了。

    写成「没有内容」，模型就会把它当成「没改动」跳过。
    """
    text = render_diff_payload({"type": "binary", "file_path": "a.bin", "message": "二进制文件无法显示差异内容"})

    assert "无法展示" in text
    assert "不等于" in text
    assert "确实变了" in text


def test_a_tool_error_is_explicit():
    text = render_diff_payload({"type": "error", "file_path": "a.xlsx", "message": "没有权限"})

    assert "取数失败" in text
    assert "没有权限" in text
    assert "不等于" in text


def test_an_unrecognised_structure_returns_none_rather_than_empty_text():
    """认不出来必须返回 `None`。返回空串会被上层当成「确实没有差异」—— 那又是同一种
    撒谎，只是换了个位置。"""
    assert render_diff_payload({"不相干的键": 1}) is None
    assert render_diff_payload(None) is None
    assert render_diff_payload(123) is None


def test_a_structure_without_a_type_is_still_understood():
    """有些分支不写 `type`。有 sheets 就当 Excel，有 patch 就当代码。"""
    assert "道具表" in render_diff_payload({"sheets": {"道具表": {"rows": []}}})
    assert "+ x" in render_diff_payload({"patch": "+ x"})


def test_a_plain_string_passes_through():
    assert render_diff_payload("已经是文本了") == "已经是文本了"


# ==========================================================================
# read_reference：只读白名单里的
# ==========================================================================


def _loaded(tmp_path) -> LoadedSkills:
    def doc(name: str, text: str) -> SkillDocument:
        return SkillDocument(
            name=name, description="", path=tmp_path / name, text=text, content_hash="h"
        )

    return LoadedSkills(
        platform_skill=doc("SKILL.md", "平台协议"),
        platform_references=(),
        project_manifest=None,
        project_references=(),
        project_skills=(),
        readable={},
        project_slug=None,
        revision="rev",
    )


def test_read_reference_reads_a_whitelisted_document(tmp_path):
    target = tmp_path / "spec.md"
    target.write_text("配表规范正文", encoding="utf-8")
    loaded = _loaded(tmp_path)
    loaded = LoadedSkills(**{**loaded.__dict__, "readable": {"spec.md": target}})

    provider = PlatformContextProvider(loaded=loaded)

    assert provider.read_reference("spec.md") == "配表规范正文"


def test_read_reference_refuses_a_document_that_was_not_offered(tmp_path):
    """模型拼一个路径出来时不能给它读到。

    **必须用一个真实存在的文件来测。** 第一版拿 `../../etc/passwd` 测，它在多数环境下
    本来就不存在 —— 于是「不在白名单」与「文件不存在」两条路都返回 `None`，测试通过了
    却什么也没证明（变异测试里把白名单检查删掉，它照样通过）。

    现在改成：磁盘上确实有一个文件，但**没被放进可读清单**，而且用绝对路径去要它。
    只有真的检查了白名单才会被拒。
    """
    offered = tmp_path / "spec.md"
    offered.write_text("配表规范正文", encoding="utf-8")
    secret = tmp_path / "secret.md"
    secret.write_text("不该读到的内容", encoding="utf-8")

    loaded = _loaded(tmp_path)
    provider = PlatformContextProvider(
        loaded=LoadedSkills(**{**loaded.__dict__, "readable": {"spec.md": offered}})
    )

    assert provider.read_reference(str(secret)) is None, "不在清单里的文件被读到了"
    assert provider.read_reference("secret.md") is None
    assert provider.read_reference("../../etc/passwd") is None
    assert provider.read_reference("随便什么.md") is None
    # 正面：在清单里的那个读得到（否则上面几条可能只是「什么都读不到」）
    assert provider.read_reference("spec.md") == "配表规范正文"


def test_read_reference_reports_a_missing_file_as_none(tmp_path):
    loaded = _loaded(tmp_path)
    loaded = LoadedSkills(**{**loaded.__dict__, "readable": {"gone.md": tmp_path / "gone.md"}})

    provider = PlatformContextProvider(loaded=loaded)

    assert provider.read_reference("gone.md") is None


# ==========================================================================
# 整表统计：判断「这个值合不合理」要有比较基准
# ==========================================================================
#
# `value_sanity` 维度要求模型判断「这个值放在这个系统里合不合理」。配表动辄几千行，
# **整表塞进提示词是不可能的**（`file_content` 单条上限 11,000 字符），而只给改动的那
# 一个单元格就等于让模型拿孤零零一个数字猜。所以平台在读表格内容时附一段**整表统计**，
# 让「1000000 是不是大得离谱」有东西可比 —— 这就是「不要根据单一数据判断」的落点。


def _workbook_bytes(sheets) -> bytes:
    """把 {表名: [[行], …]} 造成一个真的 xlsx（openpyxl 在内存里写）。"""
    import io

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


def _stats_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("  - ")]


def test_excel_content_states_the_whole_table_distribution():
    """数值列要给 min / 中位 / P90 / max —— 量级异常靠这组数字才看得出来。"""
    from services.ai.platform_provider import _read_excel_sheets

    rows = [["id", "价值"]] + [[index, index] for index in range(1, 101)]
    text = _read_excel_sheets(_workbook_bytes({"道具表": rows}), max_rows=5)

    assert "整表统计" in text
    assert "其余 100 行参与统计" in text, f"行数没说清（首行按列名）：{text[:400]}"
    assert "第 2 列（首行「价值」）" in text
    assert "最小 1｜" in text
    assert "中位 50.5｜" in text
    assert "P90 90.1｜" in text
    assert "最大 100" in text


def test_the_stats_cover_rows_that_the_body_did_not_show():
    """**这条是整表统计存在的理由**：正文按行数裁剪，统计必须覆盖被裁掉的那些行。

    否则「基准」只覆盖前几行 —— 而那几行恰恰是最正常的几行，异常值全在后面。
    """
    from services.ai.platform_provider import _read_excel_sheets

    rows = [["id", "价值"]] + [[index, 100] for index in range(1, 301)]
    rows.append([301, 1000000])  # 异常值排在最后，正文根本看不到
    text = _read_excel_sheets(_workbook_bytes({"道具表": rows}), max_rows=3)

    assert "其余 301 行参与统计" in text
    assert "最大 1000000" in text, "被裁掉的行没有参与统计，基准是残缺的"
    assert "正文只展示了前 3 行" in text, "正文被裁剪这件事没有如实说出来"


def test_the_stats_come_before_the_body_so_truncation_cannot_eat_them():
    """统计必须排在正文之前。

    `file_content` 单条上限 11,000 字符，而截断是**只砍尾巴**的（`budget.truncate_text`）——
    统计放在正文后面，大表一截断就先把基准砍掉了，而基准正是这条路径存在的意义。
    """
    from services.ai.platform_provider import _read_excel_sheets

    rows = [["id", "价值"]] + [[index, index] for index in range(1, 51)]
    text = _read_excel_sheets(_workbook_bytes({"道具表": rows}), max_rows=50)

    assert text.index("整表统计") < text.index("- 1 ｜ 1"), (
        f"整表统计被排到了正文后面，截断会先丢掉它：{text[:300]}"
    )


def test_text_columns_show_their_value_distribution():
    """枚举列要能看到「有哪些取值、各占多少」——「不在允许集合里」靠它判断。"""
    from services.ai.platform_provider import _read_excel_sheets

    rows = [["id", "品质"]]
    rows += [[index, "A"] for index in range(1, 51)]
    rows += [[index, "B"] for index in range(51, 101)]
    rows += [[101, "Z"]]
    text = _read_excel_sheets(_workbook_bytes({"道具表": rows}), max_rows=2)

    assert "不同取值 3" in text, f"枚举取值数没报出来：{text[:500]}"
    assert "最多：A(50)、B(50)、Z(1)" in text


def test_empty_rows_are_not_counted_as_data():
    """整行为空的行不算数据行 —— 否则行数与中位数都会被空行稀释。"""
    from services.ai.platform_provider import _read_excel_sheets

    rows = [["id", "价值"], [1, 10], [None, None], [2, 20], ["", "  "]]
    text = _read_excel_sheets(_workbook_bytes({"道具表": rows}), max_rows=10)

    assert "其余 2 行参与统计" in text, f"空行被算成数据行了：{text[:400]}"
    assert "中位 15" in text


def test_a_wide_table_says_how_many_columns_were_not_summarised():
    """列太多时不能无限下发统计（它要挤在 11,000 字符额度里），但要如实交代。"""
    from services.ai.platform_provider import _read_excel_sheets

    header = [f"c{index}" for index in range(30)]
    rows = [header, list(range(30))]
    text = _read_excel_sheets(_workbook_bytes({"宽表": rows}), max_rows=5)

    assert "另有 6 列未做统计" in text, f"截掉的列数没交代：{_stats_lines(text)[-3:]}"


def test_a_mixed_column_reports_both_shapes():
    """数值占少数的列（例如「1」「2」「未开放」混在一起）也要给出取值分布。"""
    from services.ai.platform_provider import _read_excel_sheets

    rows = [["id", "类型"]] + [[index, "未开放"] for index in range(1, 10)] + [[10, 1], [11, 2]]
    text = _read_excel_sheets(_workbook_bytes({"道具表": rows}), max_rows=3)

    assert "不同取值 3" in text
    assert "其中数值 2" in text, "少数数值没有被交代，模型会以为这一列完全没有数值"


def test_a_sheet_with_only_empty_rows_gets_no_stats_block():
    """整张空表不该冒出一段「按 0 行算出」的统计。"""
    from services.ai.platform_provider import _read_excel_sheets

    text = _read_excel_sheets(_workbook_bytes({"空表": [[None, None]]}), max_rows=5)

    assert "整表统计" not in text
