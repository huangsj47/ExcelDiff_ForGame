# -*- coding: utf-8 -*-
"""P1a：`(仓库, 提交, 路径)` 是**一条身份**，不是一个提示。

## 这一层挡的是什么

`commits_log` 跨仓库共用、SVN 修订号只在单仓内唯一 —— 两个仓库同窗、又都有 revision 42
是常态。修之前取数侧的做法是「收窄到本批次的几个仓库，再 `order_by(id.desc()).first()`」，
于是「两个仓库都有这条 `(提交, 路径)`」时它按自增 id 挑了一个：读回来的是**另一个仓库的
同名文件**，名字一样、内容不同，而回执抬头写着模型问的那一条，**没有任何一处看得出来
读错了**。报告照着实读到的内容写，看起来完全正常。

四条判据（各自的用例在下面）：

1. **歧义要说出来**：不许猜，回一句可照做的拒绝（请点名 `repository_id`，候选是谁）；
2. **点名了就按点名的读**：点 A 仓只读 A 仓那一份；
3. **不存在的三元组要拒绝**：点名一个本批次里没有这条改动的仓库，不许退回另一仓；
4. **归属表是三态**：「这个键上没有归属」= 不知道（照旧放行），不是「不存在」——
   把缺失当成否定，任何一处 attribution 不全都会变成静默的假拒绝。

第 4 条是这轮最容易被改坏的一条：`AnalysisScope.entries` 只在 payload 逐条带
`repository_id` 时才非空，而**既有 19 个测试文件**构造的是「repo 盲」的旧 scope。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app import app, create_tables, db
from models import Commit, Project, Repository
from services.ai.change_set import from_weekly_payload
from services.ai.evidence_prefetch import diff_requests
from services.ai.platform_provider import PlatformContextProvider
from services.ai.protocol import ContextRequest, sanitize_requests
from services.ai.repository_identity import lookup_commit, lookup_commit_files
from services.ai.scope import AnalysisScope, ScopeEntry, build_entries

PATH = "config/[30]道具表_CfgItem.xlsx"
OTHER_PATH = "config/[31]技能表_CfgSkill.xlsx"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _seed_two_repositories(*, same_path: bool = True):
    """两个仓库 + 同一条修订号。`same_path` 决定两边是否改同一个文件。

    同一条 `(提交, 路径)` 落在两个仓库里，是本文件每一类判据的**前提**（也是这个 bug 的
    形状）；不同路径那种用来钉住「点错仓库」这一支。
    """
    revision = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    project = Project(code=_uid("P"), name="三元组身份")
    db.session.add(project)
    db.session.flush()
    repos = []
    for name in ("配置仓", "代码仓"):
        repository = Repository(
            project_id=project.id, name=_uid(name), type="svn",
            url=f"https://svn.example.invalid/{_uid('r')}", branch="trunk",
            clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()
        repos.append(repository)
    first, second = repos
    db.session.add_all([
        # **先加第一个、后加第二个**：后建的 id 更大 —— 正是「按 id 取更大的那一行」
        # 会读到第二个仓库的形状。
        Commit(repository_id=first.id, commit_id=revision, path=PATH, operation="M",
               author="a", commit_time=now, message="配置仓那一份"),
        Commit(repository_id=second.id, commit_id=revision,
               path=PATH if same_path else OTHER_PATH, operation="M",
               author="b", commit_time=now, message="代码仓那一份"),
    ])
    db.session.commit()
    return revision, first.id, second.id


def _provider(scope) -> PlatformContextProvider:
    return PlatformContextProvider(
        loaded=SimpleNamespace(readable={}, skills={}, dimensions=()),
        scope=scope,
    )


def _scope(*, revision, paths_by_commit=None, repository_ids_by_commit=None, entries=()):
    return AnalysisScope(
        commits=(revision,),
        paths_by_commit=paths_by_commit or {revision: frozenset({PATH, OTHER_PATH})},
        repository_ids_by_commit=repository_ids_by_commit or {},
        readable_references=frozenset(),
        entries=entries,
    )


# ---------------------------------------------------------------------------
#  一、取数层：查到 / 查不到 / **答不出来**
# ---------------------------------------------------------------------------


class TestTheLookupAnswersWithThreeStates:
    def test_the_same_pair_in_two_repositories_is_not_guessed_between(self):
        """两个仓库都有这条 `(提交, 路径)` ⇒ `(None, 候选)`，不是「随便挑一个」。"""
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories()

            row, candidates = lookup_commit(revision, PATH)

            assert row is None, "歧义时不许猜"
            assert candidates == (first, second)

    def test_naming_a_repository_reads_that_one(self):
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories()

            for wanted in (first, second):
                row, candidates = lookup_commit(revision, PATH, wanted)
                assert row is not None, f"点名仓库 {wanted} 该读得到"
                assert row.repository_id == wanted
                assert candidates == (first, second), "候选照旧要报全（调用方要写进拒绝里）"

    def test_naming_a_repository_that_does_not_have_this_file_says_who_does(self):
        """点错仓库：行是 `None`，但候选留着 —— 调用方说得出「有它的是谁」。"""
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories(same_path=False)

            row, candidates = lookup_commit(revision, PATH, second)

            assert row is None
            assert candidates == (first,), "第二个仓库改的是另一个文件，不该出现在候选里"

    def test_a_single_candidate_is_read_without_being_asked(self):
        """只有一个仓库有这条 ⇒ 照旧直接给（新判据不许把常态也变成拒绝）。"""
        with app.app_context():
            create_tables()
            revision, first, _second = _seed_two_repositories(same_path=False)

            row, candidates = lookup_commit(revision, PATH)

            assert row is not None and row.repository_id == first
            assert candidates == (first,)

    def test_a_missing_pair_is_empty_on_both_sides(self):
        with app.app_context():
            create_tables()
            revision, _first, _second = _seed_two_repositories()

            row, candidates = lookup_commit(revision, "config/没有这个文件.xlsx")

            assert row is None and candidates == ()


class TestTheCommitFileList:
    def test_both_repositories_files_are_kept(self):
        """`commit_detail` 的清单**合并**本批次各仓库（假阴性比噪声贵）。"""
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories(same_path=False)

            rows, candidates = lookup_commit_files(revision)

            assert candidates == (first, second)
            assert {row.path for row in rows} == {PATH, OTHER_PATH}

    def test_naming_a_repository_narrows_the_list(self):
        with app.app_context():
            create_tables()
            revision, first, _second = _seed_two_repositories(same_path=False)

            rows, _candidates = lookup_commit_files(revision, first)

            assert [row.path for row in rows] == [PATH]


class TestTheRefusalTextIsActionable:
    def test_the_ambiguous_diff_asks_for_a_repository(self):
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories()
            provider = _provider(_scope(revision=revision))

            text = provider.file_diff(revision, PATH)

            assert text is not None and text.startswith("[取数失败]"), text
            assert "repository_id" in text and str(first) in text and str(second) in text

    def test_a_wrong_repository_name_says_which_ones_have_it(self):
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories(same_path=False)
            provider = _provider(_scope(revision=revision))

            text = provider.file_diff(revision, PATH, repository_id=second)

            assert text is not None and text.startswith("[取数失败]"), text
            assert str(first) in text, "要说得出「有它的是哪一个」"

    def test_the_refusal_is_not_a_silent_none(self):
        """**不许**回 `None`：那会被上层渲染成「读取失败或内容不可用」，模型从中读不出
        「换个仓库再问」这条出路。"""
        with app.app_context():
            create_tables()
            revision, _first, _second = _seed_two_repositories()
            provider = _provider(_scope(revision=revision))

            assert provider.file_diff(revision, PATH) is not None


# ---------------------------------------------------------------------------
#  二、授权层：点名仓库要过闸门，且**缺归属不等于不存在**
# ---------------------------------------------------------------------------


class TestTheRequestGate:
    def test_a_repository_outside_the_batch_is_refused(self):
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories()
            scope = _scope(
                revision=revision,
                repository_ids_by_commit={revision: frozenset({first})},
            )

            kept, dropped = sanitize_requests(
                [ContextRequest(type="file_diff", commit=revision, path=PATH,
                                repository_id=str(second))],
                scope,
            )

            assert kept == ()
            assert "不在本批次里" in dropped[0].reason
            assert str(first) in dropped[0].reason, "理由里要带上正确的编号"

    def test_a_triple_that_does_not_exist_is_refused_with_the_real_owner(self):
        """同一条提交在两个仓库里改的是不同文件：点错仓库要拒，并说出它属于谁。"""
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories(same_path=False)
            scope = _scope(
                revision=revision,
                repository_ids_by_commit={revision: frozenset({first, second})},
                entries=build_entries([
                    (first, revision, PATH),
                    (second, revision, OTHER_PATH),
                ]),
            )

            kept, dropped = sanitize_requests(
                [ContextRequest(type="file_diff", commit=revision, path=PATH,
                                repository_id=str(second))],
                scope,
            )

            assert kept == ()
            assert "在本批次里不存在" in dropped[0].reason
            assert str(first) in dropped[0].reason, "要说得出它属于哪个仓库"

    def test_the_named_triple_that_exists_goes_through(self):
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories(same_path=False)
            scope = _scope(
                revision=revision,
                repository_ids_by_commit={revision: frozenset({first, second})},
                entries=build_entries([
                    (first, revision, PATH),
                    (second, revision, OTHER_PATH),
                ]),
            )

            kept, dropped = sanitize_requests(
                [ContextRequest(type="file_diff", commit=revision, path=PATH,
                                repository_id=str(first))],
                scope,
            )

            assert not dropped
            assert kept[0].repository_id == str(first), "点名的仓库要跟着请求走到取数层"

    def test_two_repositories_are_two_requests_not_one(self):
        """去重键要含仓库：同一条 `(提交, 路径)` 两个仓库各要一次是**两条**。"""
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories()
            scope = _scope(revision=revision)

            kept, _dropped = sanitize_requests(
                [
                    ContextRequest(type="file_diff", commit=revision, path=PATH,
                                    repository_id=str(first)),
                    ContextRequest(type="file_diff", commit=revision, path=PATH,
                                    repository_id=str(second)),
                ],
                scope,
            )

            assert len(kept) == 2, f"另一条被当成重复吞掉了：{kept}"

    def test_the_same_request_twice_is_still_deduped(self):
        """反方向：同一个仓库的同一份还是只执行一次（免得去重键形同虚设）。"""
        with app.app_context():
            create_tables()
            revision, first, _second = _seed_two_repositories()
            scope = _scope(revision=revision)

            kept, _dropped = sanitize_requests(
                [
                    ContextRequest(type="file_diff", commit=revision, path=PATH,
                                    repository_id=str(first)),
                    ContextRequest(type="file_diff", commit=revision, path=PATH,
                                    repository_id=str(first)),
                ],
                scope,
            )

            assert len(kept) == 1

    def test_a_scope_without_entries_allows_what_it_used_to(self):
        """**没有归属表时行为逐字不变**（19 个既有测试文件构造的就是这种 scope）。"""
        with app.app_context():
            create_tables()
            revision, _first, second = _seed_two_repositories()
            scope = _scope(revision=revision)  # 没有 entries，也没有仓库集合

            kept, dropped = sanitize_requests(
                [ContextRequest(type="file_diff", commit=revision, path=PATH,
                                repository_id=str(second))],
                scope,
            )

            assert not dropped, f"缺归属被当成了「不存在」：{dropped}"
            assert len(kept) == 1


class TestTheAttributionIsThreeStates:
    """`scope.entry_allowed` 的三种返回值各有各的用途，合并任意两个都是错。"""

    def test_an_unknown_key_is_not_a_denial(self):
        scope = AnalysisScope(
            commits=("a" * 12,),
            paths_by_commit={"a" * 12: frozenset({PATH})},
            entries=(ScopeEntry(repository_id=1, commit_id="a" * 12, path=PATH),),
        )

        assert scope.entry_allowed(1, "a" * 12, PATH) is True
        assert scope.entry_allowed(2, "a" * 12, PATH) is False
        assert scope.entry_allowed(2, "a" * 12, "config/别的.xlsx") is None, (
            "这个键上没有归属 ⇒ 不知道，不是「不存在」"
        )
        assert scope.entry_allowed(2, "b" * 12, PATH) is None, "别的提交号同理"
        assert AnalysisScope().entry_allowed(1, "a" * 12, PATH) is None, "整个 scope 没归属"

    def test_the_helpers_are_deterministic(self):
        entries = build_entries([
            (2, "a" * 12, PATH),
            (1, "a" * 12, PATH),
            (1, "a" * 12, PATH),  # 重复的丢掉
            (None, "a" * 12, PATH),  # 读不出仓库的丢掉
            (3, "", PATH),  # 提交为空的丢掉
        ])

        assert [entry.repository_id for entry in entries] == [1, 2]
        assert AnalysisScope(entries=entries).entries_for(1, "a" * 12) == (
            ScopeEntry(repository_id=1, commit_id="a" * 12, path=PATH),
        )
        assert AnalysisScope(entries=entries).repositories_for_path(PATH, "a" * 12) == frozenset({1, 2})


class TestThePrefetchCarriesTheRepository:
    def test_a_unique_owner_is_written_into_the_request(self):
        scope = AnalysisScope(
            commits=("a" * 12,),
            paths_by_commit={"a" * 12: frozenset({PATH})},
            latest_commit_by_path={PATH: "a" * 12},
            input_paths=(PATH,),
            entries=(ScopeEntry(repository_id=7, commit_id="a" * 12, path=PATH),),
        )

        (request,) = diff_requests(scope)

        assert request.repository_id == "7", (
            "预取是**平台自己发起**的：不带仓库号时模型根本没机会纠正读错了仓库"
        )

    def test_without_attribution_the_request_stays_repo_blind(self):
        """没有归属表时不写仓库（照旧交给取数层判）—— 不许编一个仓库号出来。"""
        scope = AnalysisScope(
            commits=("a" * 12,),
            paths_by_commit={"a" * 12: frozenset({PATH})},
            latest_commit_by_path={PATH: "a" * 12},
            input_paths=(PATH,),
        )

        (request,) = diff_requests(scope)

        assert request.repository_id == ""


class TestTheReferenceSearchCoversEveryRepository:
    """`find_references` 的 Agent 回退**按仓库各派一次**。

    原先只取「第一条查得出来归属的路径」那个仓库，另一个仓库的文件从来没被搜过，而回执
    还写着「范围内共 N 个文件」—— 模型据此写下「没有其它引用」，看起来证据齐全。
    """

    def test_both_repositories_are_searched(self, monkeypatch):
        import services.agent_file_content_dispatch as dispatch
        import services.ai.platform_provider as pp
        from services.ai import provider_search as agent_mode_module

        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories(same_path=False)
            scope = _scope(
                revision=revision,
                repository_ids_by_commit={revision: frozenset({first, second})},
                entries=build_entries([
                    (first, revision, PATH),
                    (second, revision, OTHER_PATH),
                ]),
            )
            provider = _provider(scope)

            calls: list[tuple] = []

            def fake_request(repository, *, query, entries, prefix="", total_files=0):
                calls.append((getattr(repository, "id", None), tuple(entries), total_files))
                return {"status": "ready", "text": f"仓库 {getattr(repository, 'id', None)} 的命中"}

            monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: True)
            monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: True)
            monkeypatch.setattr(dispatch, "request_references", fake_request)

            text = provider.find_references("CfgItem")

            assert [item[0] for item in calls] == [first, second], (
                f"两个仓库都要被搜到：{calls}"
            )
            assert calls[0][1] == ((PATH, revision),), "派给第一个仓库的只有它自己那些文件"
            assert calls[1][1] == ((OTHER_PATH, revision),)
            assert f"仓库 {first}" in text and f"仓库 {second}" in text, (
                f"两段结果都要在，且各自标出是哪个仓库：{text}"
            )

    def test_a_repo_blind_scope_still_searches_once(self, monkeypatch):
        """没有归属表时退回单仓旧行为（一次派发，与从前逐字一致）。"""
        import services.agent_file_content_dispatch as dispatch
        import services.ai.platform_provider as pp
        from services.ai import provider_search as agent_mode_module

        with app.app_context():
            create_tables()
            revision, first, _second = _seed_two_repositories()
            provider = _provider(_scope(revision=revision))
            monkeypatch.setattr(
                provider, "_repository_of", lambda pairs: SimpleNamespace(id=first)
            )

            calls: list = []

            def fake_request(repository, *, query, entries, prefix="", total_files=0):
                calls.append((getattr(repository, "id", None), tuple(entries)))
                return {"status": "ready", "text": "命中"}

            monkeypatch.setattr(pp, "is_agent_dispatch_mode", lambda: True)
            monkeypatch.setattr(agent_mode_module, "is_agent_dispatch_mode", lambda: True)
            monkeypatch.setattr(dispatch, "request_references", fake_request)

            provider.find_references("CfgItem")

            assert len(calls) == 1 and calls[0][0] == first


class TestThePayloadPlumbing:
    def test_the_triples_reach_the_scope(self):
        """写侧 payload 的每一条本来就带 `repository_id` —— 别在归并时把它叉乘掉。"""
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories(same_path=False)

            scope = from_weekly_payload({
                "scope": "full",
                "delta_files": [
                    {"latest_commit_id": revision, "file_path": PATH,
                     "repository_id": first},
                    {"latest_commit_id": revision, "file_path": OTHER_PATH,
                     "repository_id": second},
                ],
            }).scope

            assert scope.entries_of_path(PATH, revision) == (
                ScopeEntry(repository_id=first, commit_id=revision, path=PATH),
            )
            assert scope.entry_allowed(second, revision, PATH) is False, (
                "叉乘会把这条配对也「授权」出去 —— 而它在两个仓库里都不存在"
            )


class TestTheAttributionResolvesTheAmbiguity:
    """归属**知道**这条 `(提交, 路径)` 属于谁时，就不必再回一句「请点名仓库」。

    那不是猜：归属是写侧冻结的事实（payload 的每一条本来就带 `repository_id`）。
    回一句「请点名仓库」只有在归属也不知道时才是对的。
    """

    def test_a_known_owner_is_read_without_asking(self):
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories()
            scope = _scope(
                revision=revision,
                repository_ids_by_commit={revision: frozenset({first, second})},
                entries=build_entries([(second, revision, PATH)]),
            )

            row, _candidates = _provider(scope)._commit_row(revision, PATH)

            assert row is not None, "归属已经说了是第二个仓库，不该回一句请点名"
            assert row.repository_id == second

    def test_an_unknown_owner_still_asks(self):
        """反方向：没有归属时照旧**不猜**（这正是上面那条的前提）。"""
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories()
            scope = _scope(
                revision=revision,
                repository_ids_by_commit={revision: frozenset({first, second})},
            )

            row, candidates = _provider(scope)._commit_row(revision, PATH)

            assert row is None and candidates == (first, second)

    def test_a_contradiction_between_the_two_sources_falls_back(self):
        """归属与批次**矛盾**（归属说 A、批次里只有 B）时退回批次集合。

        拿一个对不上的集合去查只会得到「查不到」—— 那会把一处内部矛盾伪装成
        「这条不存在」，而模型据此写下的是一句更硬的结论。
        """
        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories()
            scope = _scope(
                revision=revision,
                repository_ids_by_commit={revision: frozenset({second})},
                entries=build_entries([(first, revision, PATH)]),
            )

            row, candidates = _provider(scope)._commit_row(revision, PATH)

            assert row is not None and row.repository_id == second, (
                "退回批次集合之后，第二个仓库那一行照旧查得到"
            )
            # 候选**只在批次里找**（REV-AI-001）：归属说的那个仓库不在本批次里，
            # 它不进候选 —— 否则这句拒绝会把别的项目的仓库编号报给模型。
            assert candidates == (second,)


class TestTheWindowAccountCarriesPerRepositoryPaths:
    """窗口账（`window_commit_files`）**逐仓库**记路径。

    同一条修订号落在两个仓库里时，旧形状只有一个 `repository_id`（先到的那个），
    另一个仓库的路径虽然进了 `paths`，但取数侧按那一个仓库收窄 —— 那些路径的 diff
    连行都查不出来，模型只能把「读不到」写成结论。
    """

    def _two_configs(self, first: int, second: int):
        """两个周版本配置，各绑一个仓库（窗口覆盖住刚才那些提交）。

        窗口的两端是**北京墙钟**（`weekly_window_in_utc` 才换成 UTC），所以这里给足
        余量 —— 差 8 小时正是这个函数存在的理由。
        """
        from datetime import timedelta

        from models import WeeklyVersionConfig

        project_id = db.session.get(Repository, first).project_id
        now = datetime.now(timezone.utc)
        configs = []
        for repository_id in (first, second):
            cfg = WeeklyVersionConfig(
                project_id=project_id, repository_id=repository_id, name=_uid("w"),
                branch="trunk",
                start_time=now - timedelta(days=7),
                end_time=now + timedelta(days=2),
                # **不启用、不自动同步**：默认值是启用的，而测试库是会话级共用的 ——
                # 一条 active + auto_sync 的配置会真的被调度器排进任务（本机跑测试时
                # 那个后台线程就在同一个进程里），于是别的用例莫名其妙地多出一条 run。
                is_active=False,
                auto_sync=False,
                status="archived",
            )
            db.session.add(cfg)
            configs.append(cfg)
        db.session.commit()
        return configs

    def test_the_payload_carries_each_repositorys_paths(self):
        from services.ai.window_commits import window_commit_files

        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories(same_path=False)
            configs = self._two_configs(first, second)

            entry = window_commit_files(configs).get(revision)

            assert entry is not None, "这条修订号两个仓库都在窗口里"
            by_repo = entry["repositories"]
            assert set(by_repo) == {str(first), str(second)}, by_repo
            assert by_repo[str(first)] == [PATH]
            assert by_repo[str(second)] == [OTHER_PATH]
            # 老读者要的扁平那一份照旧在（两个仓库的并集）。
            assert set(entry["paths"]) == {PATH, OTHER_PATH}

    def test_the_batch_scope_gets_both_repositories_and_both_triples(self):
        """窗口账进 `change_set` 之后：两个仓库都要被收窄进去，两条三元组都要有归属。"""
        from services.ai.change_set import from_weekly_payload
        from services.ai.window_commits import window_commit_files

        with app.app_context():
            create_tables()
            revision, first, second = _seed_two_repositories(same_path=False)
            configs = self._two_configs(first, second)
            account = window_commit_files(configs)

            scope = from_weekly_payload({
                "scope": "full",
                # 白名单是**另一条**提交：窗口账只给「还不是白名单键」的那些提交补条目。
                "delta_files": [
                    {"latest_commit_id": "f" * 40, "file_path": "config/别的.xlsx",
                     "repository_id": first},
                ],
                "window_commit_files": account,
            }).scope

            assert scope.repository_ids_by_commit.get(revision) == frozenset({first, second}), (
                "只记了先到的那个仓库 —— 另一个仓库的路径会永远读不到"
            )
            assert scope.entry_allowed(first, revision, PATH) is True
            assert scope.entry_allowed(second, revision, OTHER_PATH) is True
            assert scope.entry_allowed(first, revision, OTHER_PATH) is False
