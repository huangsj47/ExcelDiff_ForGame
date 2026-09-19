# -*- coding: utf-8 -*-
"""`find_references`：在本批次改动的文件里找一处标识符的其它出现位置。

## 这个文件守的五条性质

1. **只在本批次的改动文件里搜。** 这是白名单纪律的延续 —— 这个工具能触达的内容不超过
   模型本来就能用 `file_content` 逐个索取的那些文件，所以它**不扩大读取面**。
2. **「没搜到」与「没搜完」必须分得开。** 抬头里要写出扫了几个、跳过了几个、有没有到
   上限就停：只有扫全了才能说「本批次里没有别处引用」，否则那句话是编的。
   线上一个周版本有 767 个文件，而一次搜索最多扫 240 个。
3. **越权的搜索词会被丢掉并说明原因**：太短的词（"id"）会把整批都搜出来，匹配不到任何
   改动文件的范围前缀则只会得到一句「没命中」——那会被读成「那里没有引用」。
4. **两端同一条实现**：平台本地与 Agent 端用的是同一份 `search_files` + 同一个
   `get_file_content_from_git`，只是跑在不同进程里。
5. **平台本地读不到时如实回报，并去问 Agent**（platform/agent 模式下平台被禁止 clone，
   本地永远读不到 —— 那正是「AI 看不到代码 diff」那个老问题的同一个根）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from services.ai import reference_search as rs
from services.ai.protocol import ContextRequest, parse_payload, sanitize_requests
from services.ai.reference_search import (
    SearchBudget,
    is_binary,
    render_result,
    search_files,
    search_text,
)
from services.ai.scope import AnalysisScope, normalize_path

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


# ==========================================================================
# 一、纯函数：逐行找
# ==========================================================================


def test_hits_carry_the_line_number_and_the_line_itself():
    text = "local x = 1\nlocal target_id = 5\nprint(target_id)\n"

    hits = search_text(text, "target_id", path="a.lua", limit=10)

    assert [(hit.line, hit.text) for hit in hits] == [
        (2, "local target_id = 5"),
        (3, "print(target_id)"),
    ]
    assert hits[0].render() == "a.lua:2: local target_id = 5"


def test_the_search_is_case_insensitive():
    """同一个标识符在不同语言里的写法不一样（`TargetId` / `target_id`），
    大小写敏感会让「还有谁在用」这个问题漏掉一半答案。"""
    text = "TargetID = 1\nTARGETID = 2\n"

    assert len(search_text(text, "targetid", path="a.lua", limit=10)) == 2


def test_hits_are_capped_by_the_caller():
    text = "\n".join(f"target {index}" for index in range(50))

    assert len(search_text(text, "target", path="a.lua", limit=3)) == 3


# ==========================================================================
# 二、纯函数：读一堆文件
# ==========================================================================


def _reader(files: dict):
    def read(path, commit):
        return files.get(path)

    return read


def test_binary_and_unreadable_files_are_counted_not_ignored():
    """跳过要有理由、要计数：`0/0` 与「扫了 3 个、跳过 5 个」对模型是两件事。"""
    files = {
        "a.lua": "target = 1\n",
        "b.xlsx": b"PK\x03\x04binary",
        "c.lua": None,
        "d.lua": "nothing here\n",
    }
    result = search_files(
        [(path, COMMIT_A) for path in files], "target", reader=_reader(files)
    )

    assert [hit.path for hit in result.hits] == ["a.lua"]
    assert result.scanned == 2, "只算真的搜过的（a.lua 与 d.lua）"
    assert result.binary == 1 and result.missing == 1
    assert result.files_total == 4


def test_a_reader_that_raises_does_not_kill_the_search():
    files = {"a.lua": "target = 1\n"}

    def read(path, commit):
        if path == "boom.lua":
            raise RuntimeError("工作副本里没有这个文件")
        return files.get(path)

    result = search_files(
        [("boom.lua", COMMIT_A), ("a.lua", COMMIT_A)], "target", reader=read
    )

    assert result.missing == 1 and [hit.path for hit in result.hits] == ["a.lua"]


def test_the_file_cap_stops_the_scan_and_says_so():
    files = {f"f{index}.lua": "target = 1\n" for index in range(20)}
    pairs = [(path, COMMIT_A) for path in files]

    result = search_files(pairs, "target", reader=_reader(files), max_files=5)

    assert result.scanned == 5
    assert result.truncated_files is True, "扫到上限就停了，这件事必须能被说出来"
    assert len(result.hits) == 5


def test_the_hit_cap_stops_the_collection():
    files = {f"f{index}.lua": "target\n" * 10 for index in range(20)}
    pairs = [(path, COMMIT_A) for path in files]

    result = search_files(pairs, "target", reader=_reader(files), max_hits=12)

    assert len(result.hits) == 12
    assert result.truncated_hits is True


def test_the_scan_order_is_the_one_it_was_given():
    """顺序确定 → 「扫了前 N 个」这句话可复现，「为什么没搜到那个文件」才有答案。"""
    files = {"b.lua": "target\n", "a.lua": "target\n", "c.lua": "target\n"}

    result = search_files(
        [(path, COMMIT_A) for path in files], "target", reader=_reader(files)
    )

    assert [hit.path for hit in result.hits] == ["b.lua", "a.lua", "c.lua"]


def test_a_zip_is_binary_even_when_it_decodes():
    assert is_binary(b"PK\x03\x04") is True
    assert is_binary(b"plain text") is False
    assert is_binary(None) is True


# ==========================================================================
# 三、渲染：三个数都要在
# ==========================================================================


def _result(**overrides) -> rs.SearchResult:
    data = dict(query="target_id", hits=(rs.Hit("a.lua", 12, "target_id = 1"),),
                files_total=10, scanned=4, missing=2, binary=4, prefix="")
    data.update(overrides)
    return rs.SearchResult(**data)


def test_the_header_carries_the_coverage_numbers():
    text = render_result(_result())

    assert "命中 1 处" in text and "1 个文件" in text
    assert "4/10" in text, "扫了几个 / 共几个"
    assert "4 个不是文本" in text and "2 个读不到" in text


def test_no_hits_after_a_full_scan_may_be_read_as_an_answer():
    text = render_result(_result(hits=(), scanned=10, missing=0, binary=0))

    assert "没有出现这个关键词" in text
    assert "不代表整批里没有" not in text, "扫全了就不该再说这句"


def test_no_hits_after_a_partial_scan_must_not_be_read_as_an_answer():
    """**这是这个工具最容易骗人的地方**：只扫了一部分时的「没命中」不是结论。"""
    text = render_result(_result(hits=(), truncated_files=True))

    assert "没搜到" in text and "不代表整批里没有" in text


def test_the_scope_is_stated():
    text = render_result(_result(prefix="scripts/optional/"))
    assert "范围：scripts/optional/" in text
    assert "仅限" in text and "本批次改动过的文件" in text


# ==========================================================================
# 四、白名单：搜索词与范围
# ==========================================================================


def _scope() -> AnalysisScope:
    return AnalysisScope.from_iterables(
        commits=(COMMIT_A, COMMIT_B),
        paths_by_commit={
            COMMIT_A: ["scripts/optional/activity.lua", "config/goods.xlsx"],
            COMMIT_B: ["scripts/optional/activity.lua", "scripts/net/proto.lua"],
        },
        readable_references=["incident-checklist.md"],
    )


def _sanitize(*requests):
    return sanitize_requests(list(requests), _scope())


def test_a_search_needs_no_commit_and_is_allowed():
    allowed, dropped = _sanitize(
        ContextRequest(type="find_references", query="target_id")
    )

    assert dropped == ()
    assert allowed == (ContextRequest(type="find_references", path="", query="target_id"),)


def test_the_query_survives_the_real_parse_path():
    """**这个工具曾经端到端从未执行过**：模型按 SKILL.md 发
    `{"type": "find_references", "query": "target_id"}`，而 `_coerce_requests` 构造
    `ContextRequest` 时漏了 `query=` —— 解析出来永远是空串，于是 `sanitize_requests`
    把每一条都按「搜索词太短」丢掉，丢掉的理由还把责任推给模型。

    上面那几条用例都直接构造 `ContextRequest`，正好绕过这一层，所以谁也没发现。
    这条从**真解析入口**进去，钉住「模型发什么，拿到的就是什么」。
    """
    payload = parse_payload(json.dumps({
        "status": "need_more_context",
        "reason": "先看看这个字段还有谁在用",
        "requests": [{"type": "find_references", "query": "target_id", "path": "scripts/"}],
    }, ensure_ascii=False))

    request = payload.requests[0]
    assert request.query == "target_id", "解析层把 query 丢了 —— 这个工具就永远跑不起来"
    allowed, dropped = _sanitize(request)
    assert dropped == (), f"一条格式正确的搜索请求被丢掉了：{dropped}"
    assert [r.query for r in allowed] == ["target_id"]


def test_a_too_short_query_is_dropped_with_a_reason():
    """"id" 这种词会把整批都搜出来，纯属浪费额度与上下文。"""
    allowed, dropped = _sanitize(ContextRequest(type="find_references", query="id"))

    assert allowed == ()
    assert "太短" in dropped[0].reason


def test_a_prefix_that_matches_nothing_is_dropped():
    """允许一个匹配不到东西的前缀只会得到「没命中」，而模型会把它读成「那里没有引用」。"""
    allowed, dropped = _sanitize(
        ContextRequest(type="find_references", query="target_id", path="scripts/legacy/")
    )

    assert allowed == ()
    assert "匹配不到" in dropped[0].reason


def test_a_prefix_that_matches_is_kept():
    allowed, _ = _sanitize(
        ContextRequest(type="find_references", query="target_id", path="scripts\\optional\\")
    )

    assert allowed[0].path == "scripts/optional/", "前缀要归一化（反斜杠常见）"
    assert allowed[0].query == "target_id"


def test_the_same_search_twice_in_one_round_is_deduped():
    allowed, _ = _sanitize(
        ContextRequest(type="find_references", query="target_id"),
        ContextRequest(type="find_references", query="target_id"),
    )

    assert len(allowed) == 1


def test_two_different_queries_are_two_requests():
    allowed, _ = _sanitize(
        ContextRequest(type="find_references", query="target_id"),
        ContextRequest(type="find_references", query="target_name"),
    )

    assert [item.query for item in allowed] == ["target_id", "target_name"]


def test_other_types_never_carry_a_query():
    """`query` 只有这一个类型认它 —— 别的类型带上它必须被清空，否则它会进缓存键。"""
    allowed, _ = _sanitize(
        ContextRequest(
            type="file_diff", commit=COMMIT_A, path="config/goods.xlsx", query="target_id"
        )
    )

    assert allowed[0].query == ""


# ==========================================================================
# 五、范围：本批次的文件与各自最后那次提交
# ==========================================================================


def test_batch_paths_are_deduped_and_ordered():
    scope = _scope()

    assert scope.batch_paths() == (
        "config/goods.xlsx",
        "scripts/optional/activity.lua",
        "scripts/net/proto.lua",
    ), "去重、按提交顺序、同一提交内按路径排序（确定性）"


def test_a_path_is_searched_at_the_last_commit_that_touched_it():
    """周版本里同一个文件被多条提交改过是常态，而搜索要看的是它最近的样子。"""
    scope = _scope()

    assert scope.commit_of_path("scripts/optional/activity.lua") == COMMIT_B
    assert scope.commit_of_path("config/goods.xlsx") == COMMIT_A
    assert scope.commit_of_path(normalize_path("scripts/optional/activity.lua")) == COMMIT_B


# ==========================================================================
# 六、工具层：走到 provider 的第五个门
# ==========================================================================


def test_the_tool_calls_the_provider_with_query_and_prefix():
    from services.ai.context_tools import ContextTools
    from tests.test_ai_context_tools import FakeProvider

    provider = FakeProvider(find_references="a.lua:12: target_id = 1")
    ContextTools(provider).execute(
        [ContextRequest(type="find_references", query="target_id", path="scripts/")]
    )

    assert provider.calls == [("find_references", ("target_id", "scripts/"))]


def test_the_label_carries_the_query_so_the_repeat_pointer_points_somewhere_real():
    from services.ai.context_tools import describe_request

    assert describe_request(
        ContextRequest(type="find_references", query="target_id")
    ) == "find_references target_id"
    assert "范围 scripts/" in describe_request(
        ContextRequest(type="find_references", query="target_id", path="scripts/")
    )


# ==========================================================================
# 七、两端取数
# ==========================================================================


def _provider(scope=None):
    from services.ai.platform_provider import PlatformContextProvider

    return PlatformContextProvider(loaded=SimpleNamespace(readable={}), scope=scope)


def test_without_a_scope_it_says_so_instead_of_pretending():
    text = _provider().find_references("target_id")

    assert "检索不可用" in text and "信息缺口" in text


def test_the_local_path_searches_the_batch(monkeypatch):
    """单机模式：平台自己有工作副本，本地搜。"""
    import services.vcs_content_service as vcs
    from services.ai import platform_provider as pp

    files = {
        ("scripts/optional/activity.lua", COMMIT_B): "print(target_id)\n",
        ("config/goods.xlsx", COMMIT_A): b"PK\x03\x04",
        ("scripts/net/proto.lua", COMMIT_B): "local x = 1\n",
    }
    monkeypatch.setattr(
        vcs, "get_file_content_from_git", lambda repo, commit, path: files.get((path, commit))
    )
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: False)
    provider = _provider(_scope())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=1))

    text = provider.find_references("target_id")

    assert "scripts/optional/activity.lua:1: print(target_id)" in text
    assert "2/3" in text, "扫了 2 个文本文件、共 3 个改动文件"
    assert "1 个不是文本" in text, "配表是二进制（跳过），这件事要说出来"

    second = provider.find_references("target_id")
    assert second == text, "同一个词问两遍不该让节点重扫一遍"


def test_the_platform_asks_the_agent_when_it_cannot_read_locally(monkeypatch):
    """platform/agent 模式下平台被禁止 clone —— 本地取数是**确定**读不到的，
    这时必须去问业务节点上的 Agent，而不是回一句「读不到」。"""
    import services.agent_file_content_dispatch as dispatch
    from services.ai import platform_provider as pp

    calls: list[dict] = []

    def fake(repository, *, query, entries, prefix=""):
        calls.append({"query": query, "entries": entries, "prefix": prefix})
        return {"status": "ready", "text": "a.lua:1: hit", "scanned": 3}

    monkeypatch.setattr(dispatch, "request_references", fake)
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: True)
    provider = _provider(_scope())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=7))

    text = provider.find_references("target_id", "scripts/")

    assert text == "a.lua:1: hit"
    assert len(calls) == 1
    assert calls[0]["query"] == "target_id"
    assert calls[0]["prefix"] == "scripts/"
    assert all(path.startswith("scripts/") for path, _ in calls[0]["entries"])
    assert calls[0]["entries"][0][1] == COMMIT_B, "每条路径配它自己最后那次提交"


def test_a_pending_agent_answer_is_not_a_conclusion(monkeypatch):
    import services.agent_file_content_dispatch as dispatch
    from services.ai import platform_provider as pp

    monkeypatch.setattr(
        dispatch,
        "request_references",
        lambda repository, *, query, entries, prefix="": {
            "status": "pending",
            "message": "Agent 当前离线，取数任务已排队",
        },
    )
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: True)
    provider = _provider(_scope())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=7))

    text = provider.find_references("target_id")

    assert "检索还没回来" in text
    assert "不等于「没有其它引用」" in text, "这句话缺了就会被读成「这里没有引用」"


def test_the_scan_budget_stops_it_from_scanning_forever(monkeypatch):
    """一次搜索最多扫 240 个文件，而一次分析里所有搜索加起来有总上限 ——
    用完时如实说「这一轮没搜」，不能悄悄返回「没命中」。"""
    import services.vcs_content_service as vcs
    from services.ai import platform_provider as pp

    monkeypatch.setattr(vcs, "get_file_content_from_git", lambda repo, commit, path: "target\n")
    monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: False)
    provider = _provider(_scope())
    monkeypatch.setattr(provider, "_repository_of", lambda pairs: SimpleNamespace(id=1))
    provider._search_budget = SearchBudget(limit=0)

    text = provider.find_references("target_id")

    assert "检索额度用尽" in text


def test_the_agent_side_uses_the_same_search(monkeypatch):
    """Agent 端不是另一套实现：同一个 `search_files` + 同一个读文件函数。"""
    import services.vcs_content_service as vcs
    from app import app as flask_app
    from app import create_tables, db
    from models import Project, Repository
    from services.agent_reference_search import search_references_for_agent

    with flask_app.app_context():
        create_tables()
        project = Project(code="p_rs", name="检索测试")
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id, name="r_rs", type="git",
            url="https://example.invalid/rs.git", branch="main",
            resource_type="code", clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()
        repository_id = repository.id

        monkeypatch.setattr(
            vcs,
            "get_file_content_from_git",
            lambda repo, commit, path: {"a.lua": "target_id = 1\n", "b.lua": "x = 2\n"}.get(path),
        )
        outcome = search_references_for_agent(
            {
                "repository_id": repository_id,
                "query": "target_id",
                "prefix": "",
                "entries": [["a.lua", COMMIT_A], ["b.lua", COMMIT_A]],
            }
        )

    assert outcome["hits"] == 1 and outcome["scanned"] == 2
    assert "a.lua:1: target_id = 1" in outcome["text"]


# ==========================================================================
# 八、Agent 派发：两次不同的检索不能互相复用
# ==========================================================================


def test_two_searches_are_not_the_same_request(monkeypatch):
    """`_matches` 靠 payload 里的三个键认「这是不是同一份请求」，而检索既没有提交也没有
    单个文件 —— 签名必须含关键词与范围，否则查 A 的那次会被当成查 B 的复用，
    模型会拿到**另一个范围**的结果，而且完全看不出来。"""
    from services.agent_file_content_dispatch import (
        _matches,  # noqa: PLC0415
        request_references,
    )

    captured: dict = {}

    def fake_request_from_agent(**kwargs):
        captured.update(kwargs)
        return {"status": "unavailable", "message": "（这条用例只关心请求长什么样）"}

    import services.agent_file_content_dispatch as dispatch  # noqa: PLC0415

    monkeypatch.setattr(dispatch, "_request_from_agent", fake_request_from_agent)
    request_references(
        SimpleNamespace(id=3, project_id=9),
        query="target_id",
        entries=[["a.lua", COMMIT_A]],
        prefix="scripts/",
    )

    task = SimpleNamespace(
        payload=json.dumps(
            {
                "commit_id": captured["commit_id"],
                "file_path": captured["file_path"],
                "lines": captured["lines"],
            }
        )
    )
    same = _matches(
        task, commit_id="", file_path="", lines=captured["lines"]
    )
    other_query = _matches(
        task, commit_id="", file_path="", lines="target_name|scripts/|1"
    )
    other_scope = _matches(task, commit_id="", file_path="", lines="target_id||1")

    assert same is True
    assert other_query is False, "换了关键词必须是另一份请求"
    assert other_scope is False, "换了范围必须是另一份请求"
    assert captured["extra_payload"]["query"] == "target_id"
    assert captured["extra_payload"]["entries"] == [["a.lua", COMMIT_A]]


# ==========================================================================
# 九、测试桩的守卫（与 `file_content` 那条同一条纪律）
# ==========================================================================


class TestEveryProviderStubKnowsTheNewDoor:
    """协议加了第五个方法时，**测试里的每一个桩都要跟着加** —— 这是踩过的坑。

    桩少了这个方法不会报错，只在真的索取 `find_references` 时抛 `TypeError`，而取数失败
    是被接住的：表现不是红，是**用例静默退化**成「这一轮什么都没拿到」。
    """

    def _stubs(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        seen = 0
        for path in sorted((root / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if "read_reference" not in names:
                continue
            seen += 1
            if "find_references" not in names:
                offenders.append(path.name)
        return offenders, seen

    def test_no_stub_is_missing_the_search_method(self):
        offenders, seen = self._stubs()

        assert seen >= 3, f"一个 provider 桩都没扫到（seen={seen}），这条守卫是空的"
        assert not offenders, f"这些文件的 provider 桩少了 find_references：{offenders}"
