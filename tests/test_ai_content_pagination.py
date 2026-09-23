# -*- coding: utf-8 -*-
"""工作包 D 的 P2：**拆掉隐藏截断**（大文件读取改成可继续的行分页）。

## 钉的三件事

1. **正文不再被 11,000 固定夹住**：一次分析里真正生效的页大小来自计划
   （`budget_plan.derive_tool_limits` → `ContextTools` → `provider.apply_tool_limits`），
   用户把提示词预算调高，模型真的能看到更多（不是「允许了 30,333、实际给 11,000」）。
2. **被截断这件事在回执里看得见**：`total_chars` / `returned_range` / `next_cursor` /
   `truncated` 四项都在给模型的文本里，而 `next_cursor` 是**可以直接抄进下一轮
   `lines`** 的字符串。
3. **下一页接得上**：照着 `next_cursor` 一直要下去，能走到文件末尾，且**每一行恰好被
   覆盖一次**（不重不漏）。这一条是分页唯一真正的正确性判据 —— 少了它，「继续要」
   可能给出重叠或跳过的段落，而模型不会知道。

## 为什么不能「只把 11,000 改成更大的常数」

改大之后「一份 40,000 字的文件」与「一份 12,000 字的文件」在提示词里长得一样：
都是「拿到了一份看起来完整的东西」。缺的从来不是额度，是**「这不是全部、以及怎么要
剩下的」**那句话。所以这里的断言里有一条是「同一份文件、两次索取、两页不重叠」。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from services.ai.budget_plan import (
    PROVIDER_MAX_CHARS_KEY,
    build_budget_plan,
    derive_tool_limits,
)
from services.ai.context_tools import ContextTools
from services.ai.platform_provider import (
    DEFAULT_CONTENT_MAX_CHARS,
    PlatformContextProvider,
)
from services.ai.protocol import ContextRequest
from utils.content_window import (
    CONTENT_MAX_CHARS,
    DEFAULT_WINDOW_LINES,
    page_lines,
)

CODE = "scripts/net/proto.lua"


def _file(lines: int, *, width: int = 40) -> str:
    """一份确定性的假代码文件（每行宽度固定，于是字符数与行号可以互相推算）。"""
    return "\n".join(
        f"local line_{index:05d} = '{'x' * max(0, width - 24)}'"
        for index in range(1, lines + 1)
    )


# ---------------------------------------------------------------------------
#  一、纯函数：分页与回执
# ---------------------------------------------------------------------------


def test_a_page_carries_its_receipt():
    text = _file(50)

    page = page_lines(text, "1-10", max_chars=0)

    assert page.start_line == 1 and page.end_line == 10
    assert page.total_lines == 50
    assert page.total_chars == len(text)
    assert page.returned_chars == len(page.content)
    assert page.truncated is False, "没有给字符上限 → 没被字符砍过"
    assert page.next_cursor == "11-20", "窗口没到末尾 → 要给出下一页的坐标"


def test_the_last_page_has_no_cursor():
    text = _file(10)

    page = page_lines(text, "5-10", max_chars=0)

    assert page.next_cursor == ""
    assert page.end_line == page.total_lines


def test_a_char_cap_marks_the_page_truncated_and_moves_the_cursor():
    text = _file(400, width=60)

    page = page_lines(text, "", max_chars=2_000)

    assert page.truncated is True
    assert page.end_line < page.total_lines
    assert len(page.content) <= 2_000
    assert page.next_cursor.split("-")[0] == str(page.end_line + 1), (
        "下一页必须从**被截断的那一行之后**接上，否则会漏掉或重复"
    )


def test_walking_the_cursors_covers_every_line_exactly_once():
    """**分页的正确性判据**：跟着 `next_cursor` 一直要，最后覆盖全文、不重不漏。"""
    text = _file(300, width=60)
    total_lines = 300
    seen: list[int] = []
    spec = ""
    for _ in range(50):  # 上界只是防死循环，正常几页就到底
        page = page_lines(text, spec, max_chars=2_000)
        seen.extend(range(page.start_line, page.end_line + 1))
        if not page.next_cursor:
            break
        spec = page.next_cursor
    else:  # pragma: no cover - 走到这里说明分页没有终点
        pytest.fail("跟着 next_cursor 走不到文件末尾")

    assert seen == list(range(1, total_lines + 1)), "分页出现了重叠或跳行"


def test_a_named_window_wider_than_the_cap_is_still_a_page():
    """模型点名了一大段（超过一页）时，给的也是**一页 + 续页坐标**，不是半截。"""
    text = _file(600, width=60)

    page = page_lines(text, "1-600", max_chars=3_000)

    assert page.truncated and page.next_cursor
    assert page.end_line < 600


# ---------------------------------------------------------------------------
#  二、真正生效的上限来自计划，不是常量
# ---------------------------------------------------------------------------


def test_the_plan_reports_the_page_the_provider_will_really_deliver():
    """计划里那个「取数侧真正会给多少字」**就是**它会给的（不再是 11,000 的替身）。"""
    planned = derive_tool_limits(prompt_char_budget=560_000, max_tool_requests=40)

    assert planned["file_content"] == 30_333
    assert planned[PROVIDER_MAX_CHARS_KEY] == planned["file_content"], (
        "报给界面的「实际能拿到多少字」必须等于取数侧的那一页 —— 否则那一行是假的"
    )
    assert planned[PROVIDER_MAX_CHARS_KEY] > CONTENT_MAX_CHARS


def test_a_bigger_prompt_budget_really_gives_more_text():
    """**这一条是 P2 的核心**：预算变大之后，模型真的能看到更多正文。

    修之前：抬头永远说「共 N 行；下面是第 1–276 行」，上限 11,000 兜底 ——
    300 行 × 35 字的文件在被夹住之前只有 276 行能进提示词。
    """
    # 一页 400 行（默认窗口）× 112 字 ≈ 45k 字：**两种页大小都装不下**，
    # 于是「页变大 → 真的多给」这件事才量得出来（文件太小的话两种页都不截断，等于没测）。
    text = _file(600, width=120)
    provider = PlatformContextProvider(
        loaded=SimpleNamespace(readable={}), scope=None
    )
    big = derive_tool_limits(prompt_char_budget=560_000, max_tool_requests=40)

    small_read = provider.file_content.__wrapped__ if False else None  # noqa: F841
    # 直接走「取数侧渲染」那一层（不经过数据库）：provider 的页大小由 apply_tool_limits 定。
    provider.apply_tool_limits({"file_content": DEFAULT_CONTENT_MAX_CHARS})
    default_page = provider._render_text_page(text, path=CODE, lines="")
    provider.apply_tool_limits(big)
    planned_page = provider._render_text_page(text, path=CODE, lines="")

    assert "truncated=是" in default_page, "默认页大小下这份文件应当被砍过（否则这条用例没意义）"
    assert "truncated=是" in planned_page
    assert len(planned_page) > len(default_page) + 10_000, (
        f"预算变大之后正文没变多：{len(default_page)} → {len(planned_page)}"
    )
    assert "truncated=是" in planned_page


def test_the_provider_page_size_comes_from_context_tools():
    """交接点：`ContextTools` 构造时把本轮生效的单条上限交给取数侧。"""
    provider = PlatformContextProvider(loaded=SimpleNamespace(readable={}), scope=None)
    limits = derive_tool_limits(prompt_char_budget=560_000, max_tool_requests=40)

    ContextTools(provider=provider, limits=limits)

    assert provider._content_max_chars == limits["file_content"]


def test_a_provider_that_does_not_want_limits_is_left_alone():
    """**反向样本**：不认这个约定的 provider（假 provider / 探针）一个字节都不该被改。"""
    from tests.test_ai_engine import FakeProvider

    provider = FakeProvider()

    ContextTools(provider=provider)  # 不该抛

    assert not hasattr(provider, "_content_max_chars")


def test_a_non_positive_limit_never_shrinks_the_page():
    """0 / 负数不生效（切出一段空正文比不生效更糟）。"""
    provider = PlatformContextProvider(loaded=SimpleNamespace(readable={}), scope=None)

    provider.apply_tool_limits({"file_content": 0})
    provider.apply_tool_limits({"file_content": -5})
    provider.apply_tool_limits(None)

    assert provider._content_max_chars == DEFAULT_CONTENT_MAX_CHARS


# ---------------------------------------------------------------------------
#  三、回执落到给模型的文本里（端到端，无数据库）
# ---------------------------------------------------------------------------


class _FrozenProvider(PlatformContextProvider):
    """一个不需要数据库的 provider：把冻结版本指向一个真 git 仓库。"""

    def __init__(self, frozen, **kwargs):
        super().__init__(
            loaded=SimpleNamespace(readable={}), scope=None, frozen_repository=frozen,
            **kwargs,
        )
        self._commit_row = lambda commit, path: None


def _cursor_of(text: str) -> str:
    """从回执里取 `next_cursor`；末页写的是「无」（**不是空**，见 `_page_receipt`）。

    必须**按引号切**：回执后面紧跟的是换行与正文，按空格切会把 `"29-56"
1│local`
    整段当成游标 —— 而那样一个坏 `lines` 会被 `parse_line_window` 当成「没给」，
    于是下一次索取静默地回到文件开头（这一条在写测试时真的踩过一次）。
    """
    if 'next_cursor="' not in text:
        return ""
    return text.split('next_cursor="')[1].split('"')[0]


def test_the_receipt_reaches_the_model_and_the_next_page_continues(
    frozen_code_repo,
):
    """端到端：一次索取带回收执，照 `next_cursor` 再要一次能接着往下读。"""
    provider = _FrozenProvider(frozen_code_repo, content_max_chars=2_000)

    first = provider.file_content("c" * 40, CODE, lines="1-400")

    assert "｜回执：total_chars=" in first
    assert "truncated=是" in first
    cursor = _cursor_of(first)
    assert cursor, first
    second = provider.file_content("c" * 40, CODE, lines=cursor)

    assert "｜回执：" in second
    # 第二页从第一页的下一行开始（不重叠、不跳行）。
    first_end = int(first.split("returned_range=1-")[1].split(" ")[0])
    assert f"returned_range={first_end + 1}-" in second, second


def test_the_whole_file_can_be_walked_through_file_content(frozen_code_repo):
    """跟着 `next_cursor` 一直要，能把整份文件读完（每一行恰好一次）。"""
    provider = _FrozenProvider(frozen_code_repo, content_max_chars=2_000)
    seen: list[int] = []
    spec = ""
    for _ in range(30):
        text = provider.file_content("c" * 40, CODE, lines=spec)
        start, end = (
            int(text.split("returned_range=")[1].split(" ")[0].split("-")[0]),
            int(text.split("returned_range=")[1].split(" ")[0].split("-")[1]),
        )
        seen.extend(range(start, end + 1))
        cursor = _cursor_of(text)
        if not cursor:
            break
        spec = cursor
    else:  # pragma: no cover
        pytest.fail("跟着 next_cursor 走不到文件末尾")

    assert seen == list(range(1, len(seen) + 1)), "分页有重叠或跳行"


def test_the_default_window_is_still_the_change_neighbourhood(repo_with_batch, monkeypatch):
    """**反向样本**：这一层只改了「给多少」，没改「从哪儿开始给」。

    没点名窗口时，本批次里的文件仍然按**改动位置**挑窗口（`_default_window`），
    不该因为加分页就退化成「总是从第 1 行开始」。
    """
    import services.vcs_content_service as vcs

    provider, frozen = repo_with_batch
    # 真机上这一步是 `get_file_content_from_git(repository, commit, path)`（读工作副本）。
    # 测试里直接给同一份字节 —— 要钉的是「窗口从哪儿开始」，不是 git 的读法。
    raw = (Path(frozen.frozen.local_path) / frozen.changed_path).read_bytes()
    monkeypatch.setattr(vcs, "get_file_content_from_git", lambda repo, commit, path: raw)

    text = provider.file_content(frozen.batch_commit, frozen.changed_path, lines="")

    assert "这一段是按本次改动的位置自动选的" in text, text
    assert "第 520–560 行" in text, "窗口仍按改动位置挑（`_default_window` 的结果）"


def test_the_budget_plan_renders_the_page_fact():
    """计划里要能读到「一页多大」这件事本身（界面与报告都靠它）。"""
    limits = derive_tool_limits(prompt_char_budget=200_000, max_tool_requests=20)
    plan = build_budget_plan(
        configured_prompt_chars=200_000,
        effective_prompt_chars=200_000,
        platform_chars=12_000,
        max_rounds=8,
        max_tool_requests=20,
        tool_limits=limits,
    )

    assert plan["tool_limits"]["file_content"] == limits[PROVIDER_MAX_CHARS_KEY]
    assert plan["tool_limits"][PROVIDER_MAX_CHARS_KEY] == limits["file_content"]


def test_the_default_page_size_is_still_the_initial_value():
    """`CONTENT_MAX_CHARS` 仍是**没有别的依据时**的初值（不是硬上限）。"""
    assert DEFAULT_CONTENT_MAX_CHARS == CONTENT_MAX_CHARS
    provider = PlatformContextProvider(loaded=SimpleNamespace(readable={}), scope=None)

    assert provider._content_max_chars == CONTENT_MAX_CHARS


def test_the_default_window_line_count_is_untouched():
    """窗口行数的默认值没被顺手改掉（它是另一件事：一屏给多少行）。"""
    assert DEFAULT_WINDOW_LINES == 400


@pytest.fixture()
def frozen_code_repo(tmp_path):
    """一个只有一份长代码文件的真仓库（不需要数据库）。"""
    from services.ai.frozen_repo import FrozenRepository, reset_caches
    from tests.test_ai_frozen_repo_read_scope import _git

    reset_caches()
    repo = tmp_path / "paged"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "scripts/net").mkdir(parents=True)
    (repo / "scripts/net/proto.lua").write_text(_file(400, width=60), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "长文件")
    tip = _git(repo, "rev-parse", "HEAD").strip()
    yield FrozenRepository(
        repository_id=11, name="paged", branch="main", tip=tip,
        local_path=str(repo), source="测试给定",
    )
    reset_caches()


@pytest.fixture()
def repo_with_batch(tmp_path):
    """本批次里的文件（用来钉「默认窗口仍按改动位置挑」）。"""
    from services.ai.frozen_repo import FrozenRepository, reset_caches
    from tests.test_ai_frozen_repo_read_scope import _git

    reset_caches()
    repo = tmp_path / "batchrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "scripts/net").mkdir(parents=True)
    changed = "scripts/net/combat.lua"
    (repo / changed).write_text(_file(600, width=50), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "初版")
    tip = _git(repo, "rev-parse", "HEAD").strip()
    frozen = SimpleNamespace(
        frozen=FrozenRepository(
            repository_id=12, name="batchrepo", branch="main", tip=tip,
            local_path=str(repo), source="测试给定",
        ),
        batch_commit=tip,
        changed_path=changed,
    )

    provider = PlatformContextProvider(
        loaded=SimpleNamespace(readable={}), scope=None,
        frozen_repository=frozen.frozen,
    )
    # 这一档要走的正是「本批次里的文件」那条路：给它一行 `commits_log` 行。
    row = SimpleNamespace(
        commit_id=tip, path=changed, repository_id=12,
        repository=SimpleNamespace(id=12, name="batchrepo", branch="main"),
    )
    provider._commit_row = lambda commit, path: row if path == changed else None
    provider._default_window = lambda commit, path: "520-560"
    yield provider, frozen
    reset_caches()
