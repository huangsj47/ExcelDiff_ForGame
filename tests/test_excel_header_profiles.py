# -*- coding: utf-8 -*-
"""一个仓库里并存多种表头格式时，「这张表用哪一组坐标」怎么定。

## 这一层要守住的东西

坐标的口径**一个字都没改**（表头块占前几行、字段名在第几物理行、关键列从 1 开始），
变的只是「这组坐标从哪来」。所以这个文件的用例分三类：

1. **没配就是今天的行为** —— 平台里绝大多数仓库不会配 `header_profiles`，
   这条路必须逐字不变（`test_a_repository_without_bindings_keeps_its_scalars`）。
2. **匹配的优先级与边界** —— 四种匹配方式谁先谁后、目录前缀为什么取最长、
   路径规范化（Windows 的反斜杠 / git 的正斜杠 / 大小写）为什么必须先做。
3. **坏值的方向** —— 判据解不出来时必须判**不成立**，配置坏掉时必须**退回标量**。
   这两处的反向选择（默认成立 / 抛错）后果都是静默错配整仓的坐标。

## 为什么要单独一个文件而不是塞进现有用例

现有 `tests/test_column_names_come_from_the_name_row.py` 守的是「名称行怎么生效」，
`tests/test_header_coordinates_shared_with_ai.py` 守的是「两端坐标一致」。
本文件守的是**选择**这一步，前两者守的是**生效**那一步 —— 混在一起写，
一个失败会同时指向两个不同的层。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services.excel_header_profiles import (
    BUILTIN_PROFILES,
    MATCH_DIR_PREFIX,
    MATCH_FILE_NAME,
    MATCH_HEADER_DETECT,
    MATCH_PATH_REGEX,
    default_profile_for,
    evaluate_detection,
    normalize_path,
    parse_config,
    parse_detection,
    resolve_for_file,
    validate_config,
    validate_detection,
)


def _repo(**kwargs):
    """只带三个标量的仓库替身 —— `resolve_for_file` 只读这三个属性。"""
    base = {"header_rows": None, "header_name_row": None, "key_columns": None,
            "header_profiles": None}
    base.update(kwargs)
    return SimpleNamespace(**base)


def _config(profiles=(), bindings=()):
    return json.dumps({"profiles": list(profiles), "bindings": list(bindings)}, ensure_ascii=False)


def _profile(key, label, rows, name_row, key_columns=None, marker=None):
    return {"key": key, "label": label, "header_rows": rows,
            "header_name_row": name_row, "key_columns": key_columns,
            "marker_column": marker}


# ==========================================================================
# 一、没配 = 今天的行为
# ==========================================================================


def test_a_repository_without_bindings_keeps_its_scalars():
    """**核心不变量**：没配 `header_profiles` 的仓库逐字不变。

    平台里绝大多数仓库没有这一列，所以「不配」这条路必须是今天的行为 ——
    这一条红了就说明这次改动动到了不该动的东西。
    """
    repo = _repo(header_rows=3, header_name_row=2, key_columns="2")

    profile = resolve_for_file(repo, "config/10_role/【10】角色表_CfgRole.xlsx")

    assert profile.key == "default"
    assert (profile.header_rows, profile.header_name_row, profile.key_columns) == (3, 2, "2")
    assert profile.marker_column is None, "没配方案的仓库不许凭空多出一个标记列"


def test_an_empty_or_all_null_repository_still_resolves():
    """空仓库（三个标量都是 NULL）也要能给出一个方案，且值是 `None`。

    不能在这里替用户填上「1」—— 规范化是引擎那一层的事（`_header_row_count`
    把空值当 1），这一层填了就等于把「没配」和「配了 1」混成同一件事。
    """
    profile = resolve_for_file(_repo(), "a.xlsx")

    assert (profile.header_rows, profile.header_name_row, profile.key_columns) == (None, None, None)


def test_a_broken_json_falls_back_without_raising(caplog):
    """这一列坏掉 = 退回标量，**不是**整个仓库的 diff 全线失败。"""
    repo = _repo(header_rows=5, header_profiles="{这不是 JSON")

    profile = resolve_for_file(repo, "a.xlsx")

    assert profile.header_rows == 5
    assert profile.reason == "default"


def test_a_binding_pointing_at_a_missing_profile_is_skipped():
    """规则指向一个不存在的方案 → 当它没命中，继续往下走，最终退回标量。

    静默「用方案列表里的第一个」比退回标量更坏：用户删了一个方案却忘了删引用它的
    规则，那时他会看到一张表被套上**另一个**方案的坐标，而界面上什么都看不出来。
    """
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("p1", "多行", 5, 2)],
            bindings=[{"match": MATCH_DIR_PREFIX, "value": "config", "profile": "gone"}],
        ),
    )

    profile = resolve_for_file(repo, "config/a.xlsx")

    assert profile.key == "default"


# ==========================================================================
# 二、优先级
# ==========================================================================


def test_a_fixed_file_name_beats_a_directory_rule():
    """固定文件名最具体，必须先命中 —— 哪怕目录规则也匹配。"""
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("special", "简化", 1, 1), _profile("common", "多行", 5, 2)],
            bindings=[
                {"match": MATCH_DIR_PREFIX, "value": "config", "profile": "common"},
                {"match": MATCH_FILE_NAME, "value": "config/奖励模式_CfgRewardMode.xlsx",
                 "profile": "special"},
            ],
        ),
    )

    assert resolve_for_file(repo, "config/奖励模式_CfgRewardMode.xlsx").key == "special"
    assert resolve_for_file(repo, "config/别的表.xlsx").key == "common"


def test_the_longest_directory_prefix_wins():
    """目录前缀**取最长的**那条 —— 于是「先写通用规则、再写特例」符合直觉。

    取「首个命中」的话，结果取决于用户在界面上把哪一行排在上面：先写的通用规则
    （`config`）会吃掉后面所有特例。而最长优先让特例天然赢，与书写顺序无关。
    本仓库另一处犯过同族的错：`g119_rules.py` 的 `SRC_BASE_DIR` 是「靠前的目录优先」，
    那是文件系统查找，语义不同 —— 不要照搬。
    """
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("wide", "宽", 1, 1), _profile("narrow", "窄", 5, 2)],
            bindings=[
                # 故意把**通用**的那条写在前面：最长优先必须仍然选中窄的。
                {"match": MATCH_DIR_PREFIX, "value": "config", "profile": "wide"},
                {"match": MATCH_DIR_PREFIX, "value": "config/30_goods", "profile": "narrow"},
            ],
        ),
    )

    assert resolve_for_file(repo, "config/30_goods/a.xlsx").key == "narrow"
    assert resolve_for_file(repo, "config/10_role/a.xlsx").key == "wide"


def test_a_directory_prefix_does_not_match_a_sibling_with_the_same_stem():
    """`config` 不该匹配 `config2/...` —— 前缀相等要按**路径分量**比，不是字符串前缀。"""
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("p1", "多行", 5, 2)],
            bindings=[{"match": MATCH_DIR_PREFIX, "value": "config", "profile": "p1"}],
        ),
    )

    assert resolve_for_file(repo, "config/a.xlsx").key == "p1"
    assert resolve_for_file(repo, "config2/a.xlsx").key == "default"
    assert resolve_for_file(repo, "config_backup/a.xlsx").key == "default"


def test_the_first_matching_regex_wins_by_config_order():
    """正则按**用户填的顺序**取首个命中（与目录前缀的「最长优先」不同）。

    原因：正则之间没有「谁更具体」这个可比较的量（两个正则的匹配长度不代表具体程度），
    所以顺序是用户唯一能表达优先级的手段 —— 界面上也要照这个顺序排。
    """
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("first", "第一", 1, 1), _profile("second", "第二", 5, 2)],
            bindings=[
                {"match": MATCH_PATH_REGEX, "value": r"\.xlsx$", "profile": "first"},
                {"match": MATCH_PATH_REGEX, "value": r"^config/", "profile": "second"},
            ],
        ),
    )

    assert resolve_for_file(repo, "config/a.xlsx").key == "first"


def test_a_regex_that_does_not_compile_is_skipped_not_fatal():
    """坏正则跳过就好 —— 写入侧会被拦下，读取侧不该因为历史坏数据打死整条路。"""
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("p1", "多行", 5, 2)],
            bindings=[
                {"match": MATCH_PATH_REGEX, "value": "([unclosed", "profile": "p1"},
                {"match": MATCH_PATH_REGEX, "value": r"\.xlsx$", "profile": "p1"},
            ],
        ),
    )

    assert resolve_for_file(repo, "a.xlsx").key == "p1"


def test_a_directory_glob_matches_whole_segments_only():
    """`*` 只吃**一个路径分量**，`**` 才跨目录。

    目录前缀的语义是「这个路径**位于**那个目录（或其子树）之下」，所以
    `EditorCfgTool/excel/*` 命中的是「excel 下任意一个子目录，连同它的整棵子树」——
    深一层仍然命中（那正是「目录下所有文件」的意思）。`*` 与 `**` 的区别体现在
    **能不能跨中间那段**：`EditorCfgTool/*/actor` 只认中间恰好一段。
    """
    one = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("p1", "编辑器式", 4, 2)],
            bindings=[{"match": MATCH_DIR_PREFIX, "value": "EditorCfgTool/*/actor", "profile": "p1"}],
        ),
    )
    deep = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("p1", "编辑器式", 4, 2)],
            bindings=[{"match": MATCH_DIR_PREFIX, "value": "EditorCfgTool/**/actor", "profile": "p1"}],
        ),
    )

    # `*` = 中间恰好一段：子目录下的文件照命中（含更深层，因为那是同一个目录的子树）
    assert resolve_for_file(one, "EditorCfgTool/excel/actor/a.xlsx").key == "p1"
    assert resolve_for_file(one, "EditorCfgTool/excel/actor/deep/a.xlsx").key == "p1"
    # 但中间两段就不行 —— 这正是 `*` 与 `**` 的区别
    assert resolve_for_file(one, "EditorCfgTool/a/b/actor/a.xlsx").key == "default"
    assert resolve_for_file(deep, "EditorCfgTool/a/b/actor/a.xlsx").key == "p1"

    # `*` 匹配的是**任意一段**，所以 `excel2` 也命中 —— 这不是漏匹配（它确实匹配通配符）。
    # 要排除它得写死那一段：`EditorCfgTool/excel/actor`。
    assert resolve_for_file(one, "EditorCfgTool/excel2/actor/a.xlsx").key == "p1"


def test_a_double_star_prefix_crosses_directories():
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("p1", "编辑器式", 4, 2)],
            bindings=[{"match": MATCH_DIR_PREFIX, "value": "EditorCfgTool/**/actor", "profile": "p1"}],
        ),
    )

    assert resolve_for_file(repo, "EditorCfgTool/excel/actor/a.xlsx").key == "p1"
    assert resolve_for_file(repo, "EditorCfgTool/a/b/actor/a.xlsx").key == "p1"


# ==========================================================================
# 三、路径规范化
# ==========================================================================


@pytest.mark.parametrize("raw,expected", [
    ("config\\a.xlsx", "config/a.xlsx"),
    ("./config/a.xlsx", "config/a.xlsx"),
    ("/config/a.xlsx", "config/a.xlsx"),
    ("config/a.xlsx/", "config/a.xlsx"),
    ("././config\\a.xlsx", "config/a.xlsx"),
    (None, ""),
])
def test_paths_are_normalized(raw, expected):
    assert normalize_path(raw) == expected


def test_windows_backslashes_match_the_same_rule_as_forward_slashes():
    """仓库里存的是 `/`，而用户在 Windows 上会填 `\\` —— 两边都要认。"""
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("p1", "编辑器式", 4, 2)],
            bindings=[{"match": MATCH_DIR_PREFIX, "value": "EditorCfgTool\\excel", "profile": "p1"}],
        ),
    )

    assert resolve_for_file(repo, "EditorCfgTool/excel/actor/a.xlsx").key == "p1"
    assert resolve_for_file(repo, "EditorCfgTool\\excel\\actor\\a.xlsx").key == "p1"


def test_matching_ignores_case():
    """Windows 的路径大小写不敏感，配表仓库里两种情况都有。"""
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("p1", "编辑器式", 4, 2)],
            bindings=[{"match": MATCH_DIR_PREFIX, "value": "editorcfgtool/excel", "profile": "p1"}],
        ),
    )

    assert resolve_for_file(repo, "EditorCfgTool/Excel/actor/a.xlsx").key == "p1"


def test_a_bare_file_name_matches_by_basename():
    """只写文件名也认 —— 便利是有的，代价是不同目录下的同名文件会一起命中。"""
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("p1", "简化", 1, 1)],
            bindings=[{"match": MATCH_FILE_NAME, "value": "奖励模式_CfgRewardMode.xlsx", "profile": "p1"}],
        ),
    )

    assert resolve_for_file(repo, "config/奖励模式_CfgRewardMode.xlsx").key == "p1"
    assert resolve_for_file(repo, "奖励模式_CfgRewardMode.xlsx").key == "p1"
    # 写全路径时按全路径比：换个目录就不该命中
    assert resolve_for_file(repo, "other/奖励模式_CfgRewardMode.xlsx").key == "p1"
    assert resolve_for_file(repo, "config/别的.xlsx").key == "default"


# ==========================================================================
# 四、表头特征判据
# ==========================================================================


def _probe_from(column_values):
    """假 probe：`{列: [前 N 行的原始文本]}`，**按行对齐、含空串**，忽略 within。

    空串必须留着 —— `r2:A == id` 问的是第 2 行那一格，probe 替它滤掉空行的话，
    一个空行就让「第 2 行」变成「第 3 行」，而判据照样给答案。
    """
    calls = []

    def probe(column, within):
        calls.append((column, within))
        return column_values.get(column, [])

    probe.calls = calls
    return probe


def test_a_single_row_equality_condition():
    probe = _probe_from({"A": ["SKIP"]})
    assert evaluate_detection("r1:A == SKIP", probe) is True
    assert evaluate_detection("r1:A == TYPE", probe) is False


def test_a_within_condition_needs_all_values_present():
    probe = _probe_from({"A": ["CfgRole", "id", "TYPE", "DEFAULT", "EXPORT"]})

    assert evaluate_detection("within30:A has TYPE, EXPORT", probe) is True
    assert evaluate_detection("within30:A has TYPE, NOPE", probe) is False


def test_conditions_are_combined_with_and():
    probe = _probe_from({"A": ["基础字段", "id"], "B": ["名字", "name"]})

    assert evaluate_detection("r1:A == 基础字段 && r2:A == id", probe) is True
    assert evaluate_detection("r1:A == 基础字段 && r2:A == 别的", probe) is False


def test_a_blank_row_does_not_shift_the_row_number():
    """**契约**：probe 按行对齐（空行留空串），`r2:A` 才是第 2 行。

    这是本模块最容易写错、且写错了不留痕迹的一处：如果 probe 把空行滤掉，
    `r2:A == id` 会去看**第 3 行**，然后照样给出一个确定的 True/False ——
    用户看到的是「判据明明写对了却不生效」，而判据本身没问题。
    滤空是 `has` 自己该做的事（它问「出现没出现过」），不是 probe 该做的。
    """
    aligned = _probe_from({"A": ["基础字段", "", "id"]})

    assert evaluate_detection("r2:A == id", aligned) is False, "第 2 行是空的，不该成立"
    assert evaluate_detection("r3:A == id", aligned) is True
    # `has` 才该无视空行
    assert evaluate_detection("within3:A has 基础字段, id", aligned) is True


def test_the_probe_is_told_which_column_and_range_to_read():
    """判据自己声明要看哪一列、多少行 —— reader 不该替它猜。"""
    probe = _probe_from({"C": ["x"]})

    evaluate_detection("within12:C has x", probe)

    assert probe.calls == [("C", 12)]


def test_an_unparseable_expression_is_false_not_true():
    """**方向很重要**：解不出来的判据判「不成立」。

    反向（默认成立）的后果是：一个手滑写错的判据把整个仓库的表都套上错误的坐标，
    而且不报错 —— 用户看到的是「diff 的列名全乱了」，与那条判据看不出关系。
    """
    probe = _probe_from({"A": ["SKIP"]})

    for broken in ("", "   ", "随便写点什么", "r1:A", "within0:A has X", "within30:A == X"):
        assert evaluate_detection(broken, probe) is False, broken


def test_a_probe_that_raises_does_not_kill_the_diff():
    """读不出表头（坏文件、加密工作簿）时判据按不成立处理，不让整次 diff 失败。"""
    def broken(column, within):
        raise RuntimeError("工作簿坏了")

    assert evaluate_detection("r1:A == SKIP", broken) is False


def test_detection_expressions_are_validated_for_the_form():
    assert validate_detection("r1:A == SKIP") is None
    assert validate_detection("within30:A has TYPE, EXPORT") is None
    assert validate_detection("anywhere:A has 基础字段") is None

    assert validate_detection("") is not None
    assert validate_detection("r1:A") is not None
    assert validate_detection("r1:A == x && ") is not None
    assert "==" in validate_detection("within30:A == X")


def test_detection_is_only_probed_when_a_rule_asks_for_it():
    """**性能约束**：没配 `header_detect` 规则的仓库，一次文件都不读。

    diff 主路径每张表都会调 `resolve_for_file`；如果这里无条件去探测表头，
    等于给每一次 diff 加一次 openpyxl 解析 —— 而绝大多数仓库根本没用这个功能。
    """
    probe = _probe_from({"A": ["SKIP"]})
    repo = _repo(
        header_rows=5,
        header_profiles=_config(
            profiles=[_profile("p1", "多行", 5, 2)],
            bindings=[{"match": MATCH_DIR_PREFIX, "value": "config", "profile": "p1"}],
        ),
    )

    resolve_for_file(repo, "config/a.xlsx", probe=probe)

    assert probe.calls == [], "只有路径规则时不该去读表头"


def test_header_detection_runs_after_the_cheap_rules():
    """便宜的先判：路径规则命中时就不必读表头了。"""
    probe = _probe_from({"A": ["SKIP"]})
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            profiles=[_profile("bypath", "按路径", 5, 2), _profile("bydetect", "按表头", 1, 1)],
            bindings=[
                {"match": MATCH_HEADER_DETECT, "value": "r1:A == SKIP", "profile": "bydetect"},
                {"match": MATCH_PATH_REGEX, "value": r"\.xlsx$", "profile": "bypath"},
            ],
        ),
    )

    profile = resolve_for_file(repo, "a.xlsx", probe=probe)

    assert profile.key == "bypath"
    assert probe.calls == [], "路径规则已经命中，还去读了表头"


def test_header_detection_catches_what_paths_cannot():
    """路径规则覆盖不到时，判据是最后一档。"""
    probe = _probe_from({"A": ["SKIP"]})
    repo = _repo(
        header_rows=5,
        header_profiles=_config(
            profiles=[_profile("bydetect", "简化", 1, 1)],
            bindings=[{"match": MATCH_HEADER_DETECT, "value": "r1:A == SKIP", "profile": "bydetect"}],
        ),
    )

    assert resolve_for_file(repo, "随便哪个目录/x.xlsx", probe=probe).key == "bydetect"
    # 判据不成立时退回标量
    assert resolve_for_file(repo, "随便哪个目录/x.xlsx",
                            probe=_probe_from({"A": ["TYPE"]})).key == "default"


# ==========================================================================
# 五、内置预设与校验
# ==========================================================================


def test_the_builtin_presets_are_named_without_any_project_vocabulary():
    """内置预设**不含任何具体项目的名词** —— 这是「不针对某个项目定制」的判据。

    名称里出现某个项目的表名/目录名，就等于把那个项目的约定悄悄变成了平台的默认。
    """
    assert set(BUILTIN_PROFILES) == {"builtin:single", "builtin:multi5", "builtin:editor4"}
    for profile in BUILTIN_PROFILES.values():
        assert profile.is_builtin
        assert profile.header_rows and profile.header_rows >= 1


def test_a_builtin_preset_can_be_referenced_directly():
    repo = _repo(
        header_rows=1,
        header_profiles=_config(
            bindings=[{"match": MATCH_DIR_PREFIX, "value": "EditorCfgTool/excel",
                       "profile": "builtin:editor4"}],
        ),
    )

    profile = resolve_for_file(repo, "EditorCfgTool/excel/actor/a.xlsx")

    assert (profile.header_rows, profile.header_name_row) == (4, 2)
    assert profile.is_builtin


def test_a_user_profile_cannot_take_over_a_builtin_key():
    """`builtin:` 是保留前缀：否则界面上写着「单行表头」，实际坐标来自别人的自定义。"""
    config = parse_config(_config(profiles=[_profile("builtin:single", "偷换", 9, 9)]))

    assert config.profiles == ()
    assert BUILTIN_PROFILES["builtin:single"].header_rows == 1


@pytest.mark.parametrize("bad,needle", [
    ({"profiles": [{"key": "p1", "header_rows": 5, "header_name_row": 2}], "bindings": []}, "缺少名称"),
    ({"profiles": [{"key": "p1", "label": "x", "header_rows": 0}], "bindings": []}, "表头行数"),
    ({"profiles": [{"key": "p1", "label": "x", "header_rows": 3, "header_name_row": 5}], "bindings": []},
     "不能超过表头行数"),
    ({"profiles": [{"key": "p1", "label": "x", "header_rows": 1}],
      "bindings": [{"match": "path_regex", "value": "([bad", "profile": "p1"}]}, "正则写错了"),
    ({"profiles": [{"key": "p1", "label": "x", "header_rows": 1}],
      "bindings": [{"match": "path_regex", "value": "a", "profile": "gone"}]}, "找不到方案"),
    ({"profiles": [{"key": "p1", "label": "x", "header_rows": 1}],
      "bindings": [{"match": "magic", "value": "a", "profile": "p1"}]}, "匹配方式"),
])
def test_validation_rejects_what_the_reader_would_silently_drop(bad, needle):
    """**写入侧要比读取侧严。**

    读取侧对坏值的态度是「跳过、退回默认」（一条坏配置不能打死整仓 diff）；
    如果写入侧也照那个态度，用户会看到「保存成功」而配置不生效 —— 那正是本仓库
    反复出现的那类缺陷（`important_tables` 曾经只写不读）。
    """
    errors = validate_config(json.dumps(bad, ensure_ascii=False))
    assert errors, f"这份配置应当被拦下：{bad}"
    assert any(needle in item for item in errors), errors


def test_validation_accepts_a_well_formed_config():
    assert validate_config(_config(
        profiles=[_profile("p1", "多行", 5, 2, key_columns="2", marker="A")],
        bindings=[{"match": MATCH_DIR_PREFIX, "value": "config", "profile": "p1"}],
    )) == []


def test_validation_rejects_a_name_row_beyond_the_header_block():
    errors = validate_config(_config(profiles=[_profile("p1", "x", 2, 3)]))
    assert errors and "不能超过表头行数" in errors[0]


def test_an_empty_config_is_valid_and_means_todays_behaviour():
    assert validate_config(None) == []
    assert validate_config("") == []
    assert parse_config("").bindings == ()
    assert parse_config(None).profiles == ()


def test_default_profile_reads_the_repository_scalars():
    repo = _repo(header_rows=3, header_name_row=None, key_columns="1,2")
    profile = default_profile_for(repo)
    assert (profile.header_rows, profile.header_name_row, profile.key_columns) == (3, None, "1,2")
    assert profile.describe()
