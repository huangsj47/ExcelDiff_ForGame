# -*- coding: utf-8 -*-
"""导出文档这一个纯函数模块：**给人看的字、表格形状、报告原文逐字**。

这一层不碰库、不碰 Flask，所以这里全部是直接调用断言 —— 没有夹具、没有 test_client。
导出文档是**拿出去给人看的**（交给策划/QA、贴进工单），它坏掉的方式都很安静：

* 报告原文被「顺手润色」了一下（少了半句、标题层级变了）—— 没人会发现，直到有人拿着
  这份文件去对质；
* 表格里混进一个 `|`，那一行被切成多列；
* 附录多出一整列「处置」—— 而那一列的值从来没有写入路径，永远是「待确认」；
* 文件名把中文抹掉，于是每个人下载到的都叫 `AI-20260912.md`。

所以下面按这几类各钉一组。
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

import services.ai_analysis_service as ai_service
from services.ai import report_document as doc


# ---------------------------------------------------------------------------
#  标签：一个词只有一种说法
# ---------------------------------------------------------------------------
def test_the_labels_are_the_words_the_rest_of_the_product_uses():
    assert doc.risk_label("mid_high") == "中高"
    assert doc.scope_label("incremental") == "增量（只含上次分析之后变化的部分）"
    assert doc.trigger_label("scheduled") == "定时"
    assert doc.status_label("succeeded") == "已有结论"
    assert doc.severity_label("critical") == "严重"
    assert doc.confidence_label("very_high") == "很高"
    assert doc.dimension_label("value_sanity") == "数值是否合理"


def test_the_scope_and_trigger_words_match_the_places_that_already_say_them():
    """「全量」「增量」「手动」「定时」这四个词在别处已经有了，必须逐字一致。"""
    from services.ai.change_set import _SCOPE_LABELS

    assert doc.scope_label("full") == _SCOPE_LABELS["full"]
    assert doc.scope_label("incremental").startswith(_SCOPE_LABELS["incremental"])
    # templates/ai_usage_dashboard.html:1829 写的是 `scheduled ? '定时' : '手动'`
    assert doc.trigger_label("manual") == "手动"
    assert doc.trigger_label("scheduled") == "定时"


def test_the_dimension_labels_cover_every_dimension_the_contract_allows():
    """契约说什么维度，文档里就要有对应的中文名 —— 少一个就会漏出英文 id。"""
    from services.ai.skill_contract import CONFIDENCES, DIMENSION_IDS, SEVERITIES

    assert set(doc.DIMENSION_LABELS) == set(DIMENSION_IDS)
    assert set(doc.SEVERITY_LABELS) == set(SEVERITIES)
    assert set(doc.CONFIDENCE_LABELS) == set(CONFIDENCES)


def test_an_unknown_value_shows_itself_instead_of_a_made_up_word():
    """认不出来的值原样显示：编一个中文名比显示 `foo_bar` 更糟（后者一眼可疑）。"""
    assert doc.risk_label("bogus") == "bogus"
    assert doc.severity_label("") == "-"
    assert doc.scope_label(None) == "-"


# ---------------------------------------------------------------------------
#  「有没有可导出的结论」
# ---------------------------------------------------------------------------
def test_a_succeeded_run_with_a_report_is_exportable():
    assert doc.is_exportable(status="succeeded", report_text="# 变更理解\n...") is True


def test_a_succeeded_run_without_a_report_is_not_exportable():
    """「模型没答上来、平台按规模估了个等级」那种运行：有状态、没正文。

    放它过去，导出的是一份**只有元信息表**的文件 —— 它看起来像一份正常报告。
    """
    assert doc.is_exportable(status="succeeded", report_text="") is False
    assert doc.is_exportable(status="succeeded", report_text="   \n ") is False
    assert doc.is_exportable(status="succeeded", report_text=None) is False


def test_a_failed_or_running_run_is_not_exportable():
    assert doc.is_exportable(status="failed", report_text="# 半份") is False
    assert doc.is_exportable(status="running", report_text="# 半份") is False
    assert doc.is_exportable(status="pending", report_text="# 半份") is False


# ---------------------------------------------------------------------------
#  文件名
# ---------------------------------------------------------------------------
def test_the_filename_keeps_chinese_and_the_date_is_beijing():
    """**不用 `secure_filename`** 的原因就在这一条：它会把中文整段抹掉。"""
    name = doc.report_filename(
        project_name="配表平台",
        target_label="周版本 第42周版本",
        when=datetime(2026, 9, 12, 16, 30),  # UTC → 北京 09-13 00:30
    )
    assert name == "AI分析报告-配表平台-周版本 第42周版本-20260913.md"


def test_the_filename_date_is_not_utc():
    """晚上 8 点之后（UTC 已是第二天之前）导出的文件不许标前一天。"""
    utc = datetime(2026, 9, 12, 16, 30)
    assert "20260913" in doc.report_filename(project_name="P", target_label="T", when=utc)
    # 15:59 UTC 仍是北京的 23:59，同一天
    assert "20260912" in doc.report_filename(
        project_name="P", target_label="T", when=datetime(2026, 9, 12, 15, 59)
    )


def test_the_filename_replaces_only_what_would_break_a_file_or_a_header():
    """路径分隔符、Windows 保留字符、以及**换行**（它能注入响应头）。

    换行折成空格而不是 `_`：它是空白，与「字符本身不合法」是两回事。
    """
    part = doc.safe_filename_part('a/b\\c:d*e?f"g<h>i|j\nk')
    assert part == "a_b_c_d_e_f_g_h_i_j k"


def test_the_filename_drops_a_trailing_dot_because_windows_would():
    assert doc.safe_filename_part("报告.") == "报告"
    assert doc.safe_filename_part("报告... ") == "报告"


def test_an_empty_or_all_forbidden_part_is_still_a_name():
    """空串会让文件名变成 `AI分析报告--20260912.md`（两个连字符），看不出缺了什么。"""
    assert doc.safe_filename_part("") == "未命名"
    assert doc.safe_filename_part("   ") == "未命名"
    assert doc.safe_filename_part("///") == "___"


def test_the_filename_parts_have_a_length_cap():
    long_name = "配" * 200
    part = doc.safe_filename_part(long_name)
    assert len(part) == doc.FILENAME_PART_MAX_CHARS
    name = doc.report_filename(project_name=long_name, target_label=long_name, when=None)
    # 项目名与目标名各限一段，于是日期与扩展名还在
    assert name.endswith("-未知日期.md")


def test_the_filename_has_no_path_separator_so_it_cannot_be_a_path():
    """它只会出现在 `Content-Disposition` 里，从不参与拼路径 —— 但分隔符必须没有：
    少了这一条，将来谁把它 join 进目录就会出问题。"""
    name = doc.report_filename(
        project_name="../..", target_label="a/b", when=datetime(2026, 9, 12)
    )
    assert "/" not in name and "\\" not in name and ":" not in name


# ---------------------------------------------------------------------------
#  目标名
# ---------------------------------------------------------------------------
def test_the_commit_label_shows_a_short_sha_and_the_subject():
    label = doc.commit_target_label("f" * 40, "修复奖励发放顺序")
    assert label == "提交 ffffffffffff（修复奖励发放顺序）"


def test_the_commit_label_survives_a_missing_message():
    assert doc.commit_target_label("abc123", "") == "提交 abc123"
    assert doc.commit_target_label("", "") == "提交"


def test_a_long_commit_subject_is_folded_and_truncated():
    label = doc.commit_target_label("abc", "第一行\n第二行 " + "长" * 100)
    assert "\n" not in label, "提交信息要折成一行，否则元信息表那一行会断掉"
    assert "…" in label, "截断了就要说一声"
    assert label.startswith("提交 abc（第一行 第二行 ")


def test_the_weekly_label_carries_the_version_window():
    """同名周版本靠时间窗分得开 —— 少了它，两份报告的标签是一样的。

    **这里的两个值是北京墙钟，标签原样显示、不做换算。** `WeeklyVersionConfig.
    start_time/end_time` 是用户在 `<input type="datetime-local">` 里填的、原样入库的
    北京墙钟（见 `utils/timezone_utils` 那两套墙钟的说明），页面上显示它的地方也都是
    直接 `strftime`（`weekly_version_logic.py:207`、`weekly_version_file_handlers.py:199`）。

    这条原来写的是 `2026-09-08 08:00 ~ 2026-09-15 07:59` —— 那是把它当成 naive-UTC
    又转了一次北京时间，**整整多加了 8 小时**，于是导出文档（与文件名）里的时间窗
    和周版本页面上显示的对不上。错的期望把错的实现钉成了绿的。
    """
    label = doc.weekly_target_label(
        "第42周版本", datetime(2026, 9, 8), datetime(2026, 9, 14, 23, 59)
    )
    assert label == "周版本 第42周版本（2026-09-08 00:00 ~ 2026-09-14 23:59）"


def test_the_weekly_label_without_a_window_is_just_the_name():
    assert doc.weekly_target_label("第42周版本") == "周版本 第42周版本"


# ---------------------------------------------------------------------------
#  文档本体
# ---------------------------------------------------------------------------
REPORT = "# 变更理解\n\n把奖励发放从先扣后发改成先发后扣。\n\n## 测试建议\n\n- 跑一遍日常任务\n"


def _build(**overrides):
    kwargs = dict(
        project_label="配表平台",
        target_label="提交 abcdef123456（修复奖励）",
        run_id=123,
        created_at_display="2026-09-12 19:13:04",
        risk_level="high",
        risk_reasons=["模型报出 2 条达门槛的问题", "其中含 critical"],
        scope="full",
        trigger_source="manual",
        model="qwen3-max",
        report_text=REPORT,
        anomalies=[],
    )
    kwargs.update(overrides)
    return doc.build_report_markdown(**kwargs)


def test_the_report_text_appears_verbatim():
    """**逐字**：导出的是模型说过的话，平台一个字都不改。"""
    text = _build()
    assert REPORT in text


def test_the_report_text_is_not_reformatted():
    body = _build(report_text="# 只有一级标题\n\n正文  with   spaces\n")
    assert "# 只有一级标题\n\n正文  with   spaces" in body


def test_the_document_is_meta_then_report_then_appendix():
    text = _build()
    meta_at = text.index("| 风险等级 |")
    report_at = text.index("# 变更理解")
    appendix_at = text.index(doc.APPENDIX_TITLE)
    assert meta_at < report_at < appendix_at
    # 正文两侧各有一条分隔线
    assert text.count("\n---\n") == 2


def test_the_meta_table_carries_the_things_a_reader_needs_to_locate_this_run():
    text = _build()
    for expected in (
        "配表平台",
        "提交 abcdef123456（修复奖励）",
        "2026-09-12 19:13:04（北京时间）",
        "| 风险等级 | 高 |",
        "模型报出 2 条达门槛的问题；其中含 critical",
        "| 分析范围 | 全量 |",
        "| 触发方式 | 手动 |",
        "| 是否降级 | 未降级 |",
        "qwen3-max",
        "123",
    ):
        assert expected in text, f"元信息里没有 {expected!r}"


def test_the_risk_reasons_come_from_the_payload_verbatim():
    """那句「不是模型评估结果」的警示语是**上游写的**，这里只搬运。

    在这里另写一份警示语，两处迟早说得不一样 —— 而这一份是要发出去的。
    """
    warning = "⚠️ 本次未取得完整结论（模型没答上来），该等级仅按变更规模估算，**不是**模型评估结果"
    text = _build(risk_level="medium", risk_reasons=[warning])
    assert warning in text


def test_a_degraded_run_says_so_right_next_to_the_risk_level():
    text = _build(degradation_label="轮次耗尽，按已有证据出报告")
    assert "| 是否降级 | 轮次耗尽，按已有证据出报告 |" in text


def test_no_anomalies_means_a_sentence_not_an_empty_table():
    text = _build(anomalies=[])
    assert doc.EMPTY_APPENDIX in text
    assert "| 严重度 |" not in text


def test_the_appendix_has_six_columns_and_no_disposition_column():
    """**处置列不许出现**：库里的处置状态从来没有写入路径（`models/ai_analysis/anomaly.py`
    定义了它、没有任何地方写它），整列必然是「待确认」—— 那是在要发出去的文档里印假信息。
    """
    text = _build(
        anomalies=[
            {
                "severity": "critical",
                "confidence": "very_high",
                "category": "config_id",
                "title": "7007 悬空",
                "file_path": "config/a.xlsx",
                "impact": "引用不到",
                "suggestion": "补上",
                "evidence": ["a.xlsx 第 3 行"],
            }
        ]
    )
    assert "| 严重度 | 置信度 | 维度 | 标题 | 文件 | 影响 |" in text
    assert "| 严重 | 很高 | 配置 ID | 7007 悬空 | config/a.xlsx | 引用不到 |" in text
    assert "处置" not in text
    assert "待确认" not in text
    # 证据与建议在表下的明细块里（长文本塞进单元格会把表撑得没法读）
    assert "- 证据：a.xlsx 第 3 行" in text
    assert "- 建议：补上" in text


def test_a_pipe_in_a_title_does_not_break_the_table():
    text = _build(
        anomalies=[{"severity": "high", "title": "a | b\nc", "suggestion": "x\ny"}]
    )
    row = [line for line in text.splitlines() if line.startswith("| ") and "a \\| b" in line]
    assert len(row) == 1
    # 按「没被转义的竖线」切，仍然是 6 个单元格 —— 转义的那个不参与切列
    cells = re.split(r"(?<!\\)\|", row[0])[1:-1]
    assert len(cells) == 6, cells
    # 建议是多行长文本，塞进单元格会把表撑坏：折成一行放在表下
    assert "- 建议：x y" in text


def test_suppressed_items_are_counted_but_not_listed():
    """被人工忽略的条目不进清单（尊重分诊结果），但要说一声有几条 —— 不说的话，
    「报告里提过、附录里没有」看起来像平台漏了。"""
    text = _build(
        anomalies=[{"severity": "high", "title": "留着的那条"}],
        suppressed_count=3,
    )
    assert "另有 3 条此前已被人工标记忽略，不在此列。" in text
    assert "留着的那条" in text


def test_the_appendix_says_the_list_is_not_everything():
    """附录必须说明「没达门槛的只在正文里」，否则会被读成「模型发现的所有问题」。"""
    assert doc.APPENDIX_INTRO in _build(anomalies=[{"title": "t"}])


def test_an_empty_report_still_produces_a_readable_document():
    text = _build(report_text="")
    assert "（这次运行没有报告正文。）" in text


def test_the_display_time_matches_the_one_the_drawer_shows():
    """同一个时刻在导出文档与抽屉 meta 行里必须是**同一种写法**。

    两处各写一份格式串，迟早有一边被改掉而没人发现（界面上是 `2026-09-12 19:13:04`，
    文档里变成别的样子，人就会以为是两次不同的分析）。
    """
    when = datetime(2026, 9, 12, 11, 13, 4)
    assert doc.beijing_display(when) == ai_service._created_at_display(
        SimpleNamespace(created_at=when)
    )


def test_a_missing_time_is_not_an_invented_one():
    assert doc.beijing_display(None) == ""
    assert "| 分析时间 | - |" in _build(created_at_display="")


def test_the_meta_table_never_breaks_on_a_pipe_in_a_value():
    text = _build(project_label="a | b", target_label="c|d")
    assert "| 项目 | a \\| b |" in text
    assert "| 目标 | c\\|d |" in text


def test_every_meta_row_is_a_two_cell_row():
    text = _build()
    lines = text.splitlines()
    separator = lines.index("| --- | --- |")
    assert lines[0].startswith("# ")
    assert lines[2] == "| 项 | 内容 |"
    assert separator > 2, "元信息表里一行都没有？"
    for line in lines[3:separator]:
        # 恰好两个单元格 = 三个竖线（值里的竖线已经转义）
        assert line.count("|") == 3, line


def test_attribute_rows_are_dropped_when_empty():
    """没有模型名 / 没有运行号时不写一行空的 —— 空行读起来像「这一项没填」。"""
    text = _build(model="", run_id=None)
    assert "分析模型" not in text
    assert "运行号" not in text
    assert "分析焦点" not in text


def test_the_focus_row_appears_only_when_the_user_picked_a_scope():
    assert "| 分析焦点 | 仅配表仓库 |" in _build(focus_label="仅配表仓库")
    assert "分析焦点" not in _build(focus_label="")


def test_an_anomaly_row_keeps_a_string_evidence_as_one_line():
    """老记录的 payload 里 `evidence` 有写成字符串的（不是数组）—— 不许把它拆成字。"""
    rows = doc.anomaly_rows([{"title": "t", "evidence": "整段证据"}])
    assert rows[0]["evidence"] == ["整段证据"]
    assert doc.anomaly_rows([{"title": "t", "evidence": []}])[0]["evidence"] == []


def test_an_anomaly_row_ignores_junk_entries():
    rows = doc.anomaly_rows([None, "字符串", 3, {"title": "真的"}])
    assert [row["title"] for row in rows] == ["真的"]


def test_the_document_ends_with_exactly_one_newline():
    text = _build()
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_the_report_body_keeps_its_own_trailing_boundary():
    """正文前后的空行由文档补齐（`---` 与正文之间必须隔一个空行，否则 markdown 里
    分隔线会变成标题的下划线）。"""
    text = _build(report_text="第一行\n\n\n")
    assert re.search(r"\n---\n\n第一行\n\n---\n", text)
