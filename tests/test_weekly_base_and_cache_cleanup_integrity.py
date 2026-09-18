"""周版本基准列与缓存清理的三条不变量。

## 为什么值得单独钉

这三处都是「看着对、但会静默产生错数据」的类型：

1. **基准列不能事后回填**。`merged_diff_data` 是按 `base_commit=None` 算出来的
   （整份文件读作新增）；若随后把 `base_commit_id` 改成 VCS 里的真实提交，同一行里就有了
   两个互相矛盾的真相。而该列既是 Excel 缓存键的一部分（`generate_cache_key`），也是
   「删除了哪些行」的比较基准（`weekly_deleted_excel_helpers`），所以 payload 与列必须同源。
2. **版本清理不能碰带人工处置状态的那张表**。`WeeklyVersionExcelCache` 是纯派生缓存，
   按版本号删是安全的；`WeeklyVersionDiffCache` 同一行上带着
   `confirmation_status` / `overall_status` / `status_changed_by`（待确认 / 已确认 / 已忽略），
   整行 delete 会把人的结论一起抹掉。它的口径过期由读侧闸门负责，不靠删除。
3. **注释不许指向不存在的文档章节**。原先两处注释写着「见报告里的「需要接线」」，
   而全仓任何文档里都没有这一节（grep 过 `*.md`），照它去查的人什么也查不到。

## 静态断言之前必须先剥注释

被删掉的那段回填代码，**在注释里被原样提及**（「这里原先有一段基准版本优化」）。
不剥注释就断言「回填不存在」，会因为注释而红；反过来，若哪天有人把断言改成
「必须出现某写法」，注释又会让它假绿。所以本文件一律先过 `_strip_py_comments`，
并且专门留了反向自检证明「剥」这一步不是摆设。
"""
from __future__ import annotations

import io
import tokenize
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def _strip_py_comments(source: str) -> str:
    """按 tokenize 的判定去掉注释，而不是朴素地截断 `#` 之后的内容。

    朴素截断会把字符串里的 `#` 也当成注释起点 —— 而这里要断言的「不存在」恰恰是
    注释里引用过的写法（见模块 docstring）。
    """
    lines = source.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        if start_row == end_row:
            line = lines[start_row - 1]
            lines[start_row - 1] = line[:start_col] + line[end_col:]
    return "\n".join(lines)


def _function_body(source: str, header: str) -> str:
    start = source.find(header)
    assert start >= 0, f"找不到 {header}"
    end = source.find("\ndef ", start + 1)
    return source[start:end] if end > 0 else source[start:]


def _method_body(source: str, header: str) -> str:
    """取一个**类方法**的函数体：切到下一条同缩进（4 空格）的 `def` 为止。

    `_function_body` 切的是顶层 `def`（`"\\ndef "`），用在类方法上会一路吃到文件末尾。
    下面要断言的是「这张表在这个方法里没被碰」，范围一旦吃到末尾，就会扫到别的函数里
    合法出现的同名表 —— 断言假红。
    """
    start = source.find(header)
    assert start >= 0, f"找不到 {header}"
    end = source.find("\n    def ", start + 1)
    return source[start:end] if end > 0 else source[start:]


# --------------------------------------------------------------------------
# 一、payload 与基准列同源
# --------------------------------------------------------------------------


def test_the_merged_payload_and_the_base_column_come_from_the_same_baseline():
    """两处写 `base_commit_id` 都必须用同一个 `base_commit`（payload 就是拿它算的）。"""
    code = _strip_py_comments(_read("services/weekly_version_logic.py"))
    body = _function_body(code, "def generate_weekly_merged_diff(")

    assert "merged_diff_data = _generate_merged_diff_data(" in body
    # 更新分支与新缓存分支各一处，都从 base_commit 派生
    assert body.count("base_commit.commit_id if base_commit else None") == 2
    # 构造之后再改这一列 = 与 payload 不同源：「内容读作全新增、列上写着真实提交」
    assert "new_cache.base_commit_id" not in body
    # 注意不能断言 `"real_base_commit" not in body` —— 它是本函数另一处合法调用的子串
    # （`get_real_base_commit_from_vcs`）。要断的是那个**局部变量的赋值**。
    assert "real_base_commit = " not in body


def test_the_backfill_detector_would_catch_the_removed_code():
    """反向自检：把被删掉的那段原样喂进来，检测必须仍然判红。"""
    removed = """
                real_base_commit = get_real_base_commit_from_vcs(config, file_path)
                if real_base_commit:
                    new_cache.base_commit_id = real_base_commit.commit_id
    """
    code = _strip_py_comments(removed)

    assert "real_base_commit" in code
    assert "new_cache.base_commit_id" in code


def test_comment_stripping_is_not_a_no_op_for_these_assertions():
    """证明 `_strip_py_comments` 真的在起作用，而不是恰好没有干扰。

    被删的那段代码现在只活在注释里（「这里原先有一段基准版本优化」）。所以：
    原文里必须还能找到它，剥掉注释后必须找不到 —— 两个方向都变了，才说明「剥」
    这一步是有意义的。
    """
    raw = _read("services/weekly_version_logic.py")

    assert "基准版本优化" in raw, "那段说明注释没了？这条反向自检本身失效了"
    assert "基准版本优化" not in _strip_py_comments(raw), "剥注释没有生效"


# --------------------------------------------------------------------------
# 二、版本清理的范围（钉在**真正会跑**的路径上）
# --------------------------------------------------------------------------
#
# 这一段原先钉的是 `tasks/cache_cleanup.py`。那个模块（连同整个 `tasks/` 包，634 行）
# **没有任何调用者**，已整包删除 —— 也就是说：旧断言保护的是一个永远不会执行的函数，
# 而它声称要清的周版本 Excel 缓存（每行一整份 HTML/CSS/JS，本库最占空间的表）
# **从上线起就没被清过**，只能靠管理页上的手动按钮。这正是「静态断言钉错了位置」
# 的代价：断言是绿的，功能是不跑的。
#
# 清理现在挂在两条真会跑的路径上：启动时的 `app.clear_version_mismatch_cache()`
# → `app_bootstrap_db_service.clear_startup_version_mismatch_cache()`（按版本号），
# 以及每日 `cleanup_cache` 分支（按 90 天过期）。断言随之改到这里。


def test_the_version_cleanup_targets_the_weekly_excel_cache():
    code = _strip_py_comments(_read("services/weekly_excel_cache_service.py"))
    body = _method_body(code, "def cleanup_version_mismatch_cache(")

    assert "WeeklyVersionExcelCache.query.filter(" in body
    assert "WeeklyVersionExcelCache.diff_version" in body
    # 比对的必须是**运行时**版本：写死字面量的话，升级 DIFF_LOGIC_VERSION 之后
    # 清理会按旧号做事 —— 正是「刚生成的当前版本缓存被当成过期数据删掉」那个形态。
    #
    # 断言写成「具体的比较式」，而不是 `"DIFF_LOGIC_VERSION" not in body`：
    # 方法自己的 docstring 里就写着这个常量名（用来解释「升级会让整库旧行变不可达」），
    # 那条断言会因为 docstring 而假红 —— 剥注释挡不住 docstring。
    assert "WeeklyVersionExcelCache.diff_version != self.diff_logic_version" in body
    assert "!= DIFF_LOGIC_VERSION" not in body, "清理按写死的版本号比对，升级后会删错数据"
    assert "= DIFF_LOGIC_VERSION" not in body


def test_the_version_cleanup_is_wired_into_the_startup_path():
    """**接线断言**：方法存在不等于会跑 —— 这一段存在的唯一理由就是那次教训。"""
    bootstrap = _strip_py_comments(_read("services/app_bootstrap_db_service.py"))
    body = _function_body(bootstrap, "def clear_startup_version_mismatch_cache(")

    assert "weekly_excel_cache_service.cleanup_version_mismatch_cache()" in body, (
        "启动清理没有调用周版本 Excel 缓存的版本清理 —— 又变成一段跑不到的代码"
    )
    # 必填关键字参数：漏传一次就永久不跑，而它的失败形态（日志上什么都不发生）
    # 与「本来就没东西可清」一模一样，界面上也看不出来。
    assert "weekly_excel_cache_service," in body.split("):", 1)[0], (
        "weekly_excel_cache_service 不是必填参数 —— 可选参数忘了传 = 静默不清理"
    )
    assert "weekly_excel_cache_service=weekly_excel_cache_service," in _read("app.py"), (
        "app.py 没有把服务传进去，启动清理会 TypeError（或漏清理）"
    )


def test_the_daily_sweep_also_expires_the_weekly_excel_cache():
    """每日 04:00 的 `cleanup_cache` 也要覆盖这张表（按 90 天过期，不是按版本）。"""
    code = _strip_py_comments(_read("services/task_worker_service.py"))
    body = _function_body(code, "def background_task_worker(")

    assert "_weekly_excel_cache_service.cleanup_expired_cache()" in body, (
        "每日清理没有覆盖周版本 Excel 缓存"
    )
    # None 是「执行失败」，不能被当成 0 混进成功日志里（同分支另外两处已有先例）
    assert "weekly_cleaned is None" in body


def test_version_cleanup_spares_the_table_that_carries_dispositions():
    """`WeeklyVersionDiffCache` 不在这里删 —— 它带着人的处置结论。"""
    code = _strip_py_comments(_read("services/weekly_excel_cache_service.py"))
    body = _method_body(code, "def cleanup_version_mismatch_cache(")

    assert "WeeklyVersionDiffCache.query" not in body, (
        "按版本号整行删了带处置状态的那张表 —— 人工确认/忽略的结论会被一起抹掉"
    )


def test_the_reason_that_table_is_spared_stays_written_down():
    """理由必须留在**清理方法自己身上**：否则后来者会顺手把这张表补进清理清单。

    这段理由原先写在 `tasks/cache_cleanup.py`（已删除）。删掉那个模块时，
    代码里就再也没有一处说明「为什么这张表不能按版本号删」了 —— 理由必须跟着搬走。
    """
    code = _strip_py_comments(_read("services/weekly_excel_cache_service.py"))
    body = _method_body(code, "def cleanup_version_mismatch_cache(")

    assert "confirmation_status" in body, "没说清那张表上带着人工处置状态"
    assert "is_merged_diff_cache_current" in body, "没指出它的口径过期由哪道闸门负责"


def test_the_cleanup_detector_would_catch_a_blanket_delete():
    """反向自检：真有人按版本号整行删这张表，上面那条断言必须判红。"""
    snippet = """
        WeeklyVersionDiffCache.query.filter(
            WeeklyVersionDiffCache.diff_version != DIFF_LOGIC_VERSION
        ).delete()
    """
    body = _strip_py_comments(snippet)
    assert "WeeklyVersionDiffCache.query" in body

    # 同时证明 `_method_body` 的切分真的只在方法内：它必须切掉文件里**后面**的
    # 同名表（needs_merged_diff_cache 里合法地查这张表），否则上面的断言无从成立。
    code = _strip_py_comments(_read("services/weekly_excel_cache_service.py"))
    assert "WeeklyVersionDiffCache.query" in code, (
        "文件里别处不再查这张表了？那上面的断言失去了参照物"
    )
    assert "WeeklyVersionDiffCache.query" not in _method_body(
        code, "def cleanup_version_mismatch_cache("
    )


# --------------------------------------------------------------------------
# 三、注释不再指向不存在的文档章节
# --------------------------------------------------------------------------


def test_the_stale_pointer_to_a_missing_report_section_is_gone():
    for path in ("models/weekly_version.py", "services/weekly_excel_cache_service.py"):
        assert "需要接线" not in _read(path), f"{path} 还在指向不存在的报告章节"


def test_the_comments_point_at_the_real_gate():
    """替代那个失效指针的，是真正把关的位置。"""
    assert "is_merged_diff_cache_current" in _read("models/weekly_version.py")
    assert "is_merged_diff_cache_current" in _read("services/weekly_excel_cache_service.py")
