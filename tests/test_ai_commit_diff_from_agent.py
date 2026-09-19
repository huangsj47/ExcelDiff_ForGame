# -*- coding: utf-8 -*-
"""AI 读「某一条提交改了这个文件的什么」的第二条来源：Agent。

## 这条是被什么逼出来的

一次周版本 AI 分析的「信息缺口」写着「**未读到任何代码 diff 与协议 diff**，
`code_logic`、`version_branch` 两个维度无法判断」。`tests/test_ai_code_diff_source.py`
那组解决的是「平台本地**有**那一份、但没去读」（读已落库的周版本合并 diff）。

这一组解决的是另一半：**平台本地根本没有那一份**。

* 单提交分析刻意不读窗口合并缓存（`use_stored_batch_diff=False`）——它问的是「这一条
  提交改了什么」，窗口合并的那份会张冠李戴；
* 于是只剩实时重算，而实时重算要读两个版本的文件内容，platform/agent 模式下平台
  **被禁止 clone**（`get_file_content_from_git` 直接返回 `None`）。

两条本地来源都不成立，`file_diff` 交出去的就是一句「取数失败」——模型据此写「未读到
任何代码 diff」。而**业务节点上算得出来**：那里有工作副本。所以补上第三条来源：

    `PlatformContextProvider.file_diff` → 本地两条都没有真差异
      → `services/agent_file_content_dispatch.request_file_diff`（平台派任务）
      → `services/agent_file_diff_reader.read_file_diff_for_agent`（节点上现算）
      → 回传 → 平台补一行出处交给模型

## 本文件断言三层

1. **Agent 端怎么算**（`agent_file_diff_reader`）：在工作副本上算并**就地渲染成文本**
   （渲染函数与平台侧是同一份），取不到/算不出就抛 —— 返回空串在这个契约里等于
   「确实没有差异」，那比取不到更坏；
2. **provider 怎么用**：本地有真差异就用本地的（一次往返都不该付），本地没有才去问；
   问到了就用，问不到要把**两个原因**都说清楚，并指向「写成信息缺口」；
3. **出处**：这条路上给的是**这一条提交**的差异，不是整个窗口的合并差异 —— 不说明，
   模型会把「这一条改了」读成「这个文件这一周就改了这些」。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import app, create_tables, db
from models import Commit, Project, Repository
from services.ai.platform_provider import PlatformContextProvider
from services.ai.skill_loader import LoadedSkills, SkillDocument

LUA = "code/qz_pub/battle/BattleMgr.lua"
PATCH = "\n".join([
    "@@ -118,7 +118,9 @@ function BattleMgr:onHit(target)",
    "     local damage = self:calcDamage(target)",
    "-    target.hp = target.hp - damage",
    "+    if target.invincible then",
    "+        return",
    "+    end",
    "+    target.hp = target.hp - damage",
])
# 「取到了记录但没有补丁内容」是把「读不到」说成「没有内容」的那句话。整组测试都拿它
# 当反面判据（而不是断言某个正面措辞），与 `test_ai_code_diff_source.py` 同一口径。
FALSE_CLAIM = "取到了记录但没有补丁内容"


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _loaded(tmp_path) -> LoadedSkills:
    return LoadedSkills(
        platform_skill=SkillDocument(
            name="SKILL.md", description="", path=tmp_path / "SKILL.md",
            text="平台协议", content_hash="h",
        ),
        platform_references=(), project_manifest=None, project_references=(),
        project_skills=(), readable={}, project_slug=None, revision="rev",
    )


def _seed(*, stored_payload="absent"):
    """造一个代码仓库 + 两条提交（可带一条已落库的周版本合并 diff 缓存行）。

    `stored_payload=None` 表示「有缓存行但载荷为空」；默认 `"absent"` 表示**没有**缓存行
    —— 这一组关心的是「本地什么都没有」时的事。
    """
    now = datetime.now(timezone.utc)
    with app.app_context():
        create_tables()
        project = Project(code=_uid("PT"), name="提交级 diff 由节点现算")
        db.session.add(project)
        db.session.flush()
        repository = Repository(
            project_id=project.id, name=_uid("lua"), type="git",
            url="https://example.invalid/lua.git", branch="main",
            resource_type="code", clone_status="completed",
        )
        db.session.add(repository)
        db.session.flush()

        base_sha = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        head_sha = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        db.session.add_all([
            Commit(repository_id=repository.id, commit_id=base_sha, path=LUA,
                   operation="M", author="a", commit_time=now - timedelta(days=2),
                   message="基线"),
            Commit(repository_id=repository.id, commit_id=head_sha, path=LUA,
                   operation="M", author="b", commit_time=now - timedelta(days=1),
                   message="无敌帧"),
        ])
        if stored_payload != "absent":
            payload = {"type": "code", "file_path": LUA, "patch": PATCH, "hunks": []} \
                if stored_payload is None else stored_payload
            from models.weekly_version import WeeklyVersionConfig, WeeklyVersionDiffCache

            config = WeeklyVersionConfig(
                project_id=project.id, repository_id=repository.id, name=_uid("周版本"),
                branch="main", start_time=now - timedelta(days=7), end_time=now,
            )
            db.session.add(config)
            db.session.flush()
            db.session.add(WeeklyVersionDiffCache(
                config_id=config.id, repository_id=repository.id, file_path=LUA,
                file_type="code", merged_diff_data=json.dumps({
                    "file_path": LUA, "base_commit": base_sha, "latest_commit": head_sha,
                    "commits_count": 1, "commit_ids": [head_sha], "merge_strategy": "single",
                    "diff_data": payload, "merged_diff": payload,
                }, ensure_ascii=False),
                base_commit_id=base_sha, latest_commit_id=head_sha,
                cache_status="completed", overall_status="pending",
            ))
        db.session.commit()
        return SimpleNamespace(
            project_id=project.id, repository_id=repository.id,
            head_sha=head_sha, base_sha=base_sha,
        )


@pytest.fixture(autouse=True)
def _platform_mode(monkeypatch):
    """整组跑在 **platform** 模式：缺口只在这种部署里现形，而且它让测试不碰网络
    （不设置的话，平台会真的去 clone 那个 `example.invalid`）。"""
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")


@pytest.fixture(scope="module")
def repository_id():
    """一个真实的仓库行（Agent 侧按 id 查库的那几处要用）。

    作用域是 module：`create_tables()` 每次都会把整张表清单打进日志，一用例一次会把
    输出淹掉；项目 code 带 uuid 后缀，所以不会撞会话级共用测试库里的唯一约束。
    """
    return _seed().repository_id


def _stub_agent(monkeypatch, outcome=None):
    """接住 `request_file_diff`（不碰网络、不派真任务），把调用参数记下来。"""
    import services.agent_file_content_dispatch as dispatch

    calls = []

    def _fake(repository, **kwargs):
        calls.append(dict(kwargs))
        return dict(outcome if outcome is not None else {
            "status": "ready", "kind": "diff", "file_path": LUA,
            "content": f"代码差异：{LUA}\n{PATCH}\n",
            "original_chars": len(PATCH), "truncated": False,
        })

    monkeypatch.setattr(dispatch, "request_file_diff", _fake)
    return calls


# ==========================================================================
#  一、provider 侧：本地没有才去问，问到了才用
# ==========================================================================


def test_a_single_commit_diff_is_recomputed_on_the_business_node(tmp_path, monkeypatch):
    """**这一条就是「模型点名要某一条提交的 diff」的回归。**

    单提交分析关掉窗口合并缓存之后只剩实时重算，而那条路在这里必然读不到。两条本地
    来源都拿不出真差异时**必须去问业务节点** —— 那里有工作副本，也算得出来。
    """
    seeded = _seed()
    calls = _stub_agent(monkeypatch)
    with app.app_context():
        text = PlatformContextProvider(
            loaded=_loaded(tmp_path), use_stored_batch_diff=False
        ).file_diff(seeded.head_sha, LUA)

    assert calls, "本地取不到时没去问业务节点 —— 这条路又断了"
    assert calls[0]["commit_id"] == seeded.head_sha, calls
    assert calls[0]["file_path"] == LUA, calls
    assert "target.invincible" in text, text
    assert FALSE_CLAIM not in text, text
    assert "取数失败" not in text, f"拿到了真差异却还在说取数失败：{text}"


def test_the_recomputed_diff_says_it_is_only_this_commit(tmp_path, monkeypatch):
    """出处必须写明「只含这一条提交」。

    周版本分析平时拿到的是**整个窗口的合并差异**（`_batch_provenance` 在合并了多条提交
    时会说明）。这条路上给的是这一条提交与前一次提交之间的差异 —— 不说清楚，模型会把
    「这一条改了」读成「这个文件这一周就改了这些」，而报告里没有任何东西能纠正它。
    """
    seeded = _seed()
    _stub_agent(monkeypatch)
    with app.app_context():
        text = PlatformContextProvider(
            loaded=_loaded(tmp_path), use_stored_batch_diff=False
        ).file_diff(seeded.head_sha, LUA)

    assert "只含这一条提交" in text, text
    assert "合并差异" not in text, f"它只是这一条提交的改动，不能说成窗口合并的那份：{text}"
    assert seeded.head_sha[:8] in text, "说明里要带上问的是哪一条提交"


def test_a_stored_window_diff_still_wins_and_costs_no_round_trip(tmp_path, monkeypatch):
    """周版本分析里已落库的那一份仍是首选：业务节点一次都不该被叫醒。

    反过来的写法（有 Agent 就先去问）会让每次 `file_diff` 都多一次十几秒的往返，
    而且拿到的是**窄**的一份（单提交 vs 窗口合并）—— 模型读到的改动反而变少了。
    """
    seeded = _seed(stored_payload=None)
    calls = _stub_agent(monkeypatch)
    with app.app_context():
        text = PlatformContextProvider(loaded=_loaded(tmp_path)).file_diff(seeded.head_sha, LUA)

    assert not calls, f"本地就有那一份，不该去打扰业务节点：{calls}"
    assert "target.invincible" in text, text


def test_a_stored_failure_payload_falls_back_before_asking_the_node(tmp_path, monkeypatch):
    """落库的那一份本身就是「取不到」时：先实时重算，重算能出来就不必麻烦业务节点。

    「已落库」不等于「已算好」——同步时算失败也会落一行（载荷的 `type` 是 `error`）。
    它渲染出来是一句完整的「取数失败」，**看着像内容**，所以必须显式认出来：否则它会被
    当成答案交出去，而两条能算出来的路一条都没走过。
    """
    seeded = _seed(stored_payload={
        "type": "error", "file_path": LUA, "message": "无法读取当前版本的文件内容",
    })
    calls = _stub_agent(monkeypatch)
    monkeypatch.setattr(
        "services.vcs_content_service.get_unified_diff_data",
        lambda commit, previous: {
            "type": "text", "file_path": LUA,
            "raw_diff": "@@ -1 +1 @@\n-旧\n+新\n", "hunks": [],
        },
    )
    with app.app_context():
        text = PlatformContextProvider(loaded=_loaded(tmp_path)).file_diff(seeded.head_sha, LUA)

    assert not calls, f"实时重算已经出来了，不该再问业务节点：{calls}"
    assert "-旧" in text and "+新" in text, text
    assert "取数失败" not in text, text


def test_a_stored_failure_payload_goes_to_the_node_when_recomputing_fails(tmp_path, monkeypatch):
    """落库的是失败载荷、实时重算也读不到 → 还是要去业务节点（这是模型唯一剩下的路）。"""
    seeded = _seed(stored_payload={
        "type": "error", "file_path": LUA, "message": "无法读取当前版本的文件内容",
    })
    calls = _stub_agent(monkeypatch)
    with app.app_context():
        text = PlatformContextProvider(loaded=_loaded(tmp_path)).file_diff(seeded.head_sha, LUA)

    assert calls, "两条本地来源都是失败，却把失败说明当成答案交出去了"
    assert "target.invincible" in text, text


def test_single_machine_mode_never_dispatches_to_an_agent(tmp_path, monkeypatch):
    """单机模式下本地就是**唯一**的取数点：那里算不出来的原因换台机器算也一样。

    判据是部署模式（`is_agent_dispatch_mode`）而不是「有没有绑定 Agent」——后者在单机
    部署里可能是历史遗留的一行绑定，按它派任务等于给单机模式凭空加一条取数路。
    """
    monkeypatch.setenv("DEPLOYMENT_MODE", "single")
    seeded = _seed()
    calls = _stub_agent(monkeypatch)
    monkeypatch.setattr(
        "services.vcs_content_service.get_file_content_from_git",
        lambda repository, commit_id, path: None,
    )
    with app.app_context():
        text = PlatformContextProvider(
            loaded=_loaded(tmp_path), use_stored_batch_diff=False
        ).file_diff(seeded.head_sha, LUA)

    assert not calls, f"单机模式不该派 Agent 任务：{calls}"
    assert text and ("这不等于" in text or "取数失败" in text), text


def test_two_analysis_in_one_process_do_not_share_the_answer(tmp_path, monkeypatch):
    """一次分析一个 provider：上一条提交的答案不许喂给下一条。

    记忆放在**实例**上正是为了这个（放模块级会跨分析复用）。同一份请求只等一次是本实例
    内的优化，不是全局缓存 —— 两个 provider 各问一次是对的（第二次分析时 Agent 那边
    可能已经修好了）。
    """
    seeded = _seed()
    calls = _stub_agent(monkeypatch)
    with app.app_context():
        PlatformContextProvider(
            loaded=_loaded(tmp_path), use_stored_batch_diff=False
        ).file_diff(seeded.head_sha, LUA)
        PlatformContextProvider(
            loaded=_loaded(tmp_path), use_stored_batch_diff=False
        ).file_diff(seeded.head_sha, LUA)

    assert len(calls) == 2, f"两次分析被合成了一次（记忆泄漏到实例之外了）：{calls}"


def test_the_same_diff_is_asked_once_within_an_analysis(tmp_path, monkeypatch):
    """一次分析里同一份 diff 只等一次（模型常对同一个文件问两次）。

    第二次再等一轮 = 一次分析白花十几秒，而答案与第一次逐字相同。
    """
    seeded = _seed()
    calls = _stub_agent(monkeypatch)
    provider = PlatformContextProvider(loaded=_loaded(tmp_path), use_stored_batch_diff=False)
    with app.app_context():
        first = provider.file_diff(seeded.head_sha, LUA)
        second = provider.file_diff(seeded.head_sha, LUA)

    assert len(calls) == 1, f"同一份请求问了 {len(calls)} 次：{calls}"
    assert first == second, "两次的回答必须一致（第二次不该变成另一句话）"


# ==========================================================================
#  二、问不到时：两个原因都要说，并指向「信息缺口」
# ==========================================================================


def test_when_the_node_cannot_answer_both_reasons_reach_the_model(tmp_path, monkeypatch):
    """问不到业务节点时，**两个原因都要在**：平台本地为什么读不到、业务节点那边怎么了。

    只留一个会把另一半藏起来：只看到「平台读不到」的人会去查工作副本，而真正该做的是给
    项目绑一个 Agent；只看到「没绑 Agent」的人不会知道这条提交本身有没有问题。
    并且必须告诉模型该拿它怎么办（写成信息缺口），否则它会据此推断出一个结论。
    """
    seeded = _seed()
    _stub_agent(monkeypatch, {"status": "unavailable", "message": "项目没有绑定 Agent 节点"})
    with app.app_context():
        text = PlatformContextProvider(
            loaded=_loaded(tmp_path), use_stored_batch_diff=False
        ).file_diff(seeded.head_sha, LUA)

    assert "没有绑定 Agent" in text, f"业务节点那一侧的原因丢了：{text}"
    assert "这不等于" in text, f"平台那一侧的失败说明丢了：{text}"
    assert "信息缺口" in text, f"要告诉模型该把它写成信息缺口：{text}"


def test_a_pending_round_trip_is_not_reported_as_no_change(tmp_path, monkeypatch):
    """还没取回来（`pending`）也是一种「不知道」，不能说成「没有改动」。"""
    seeded = _seed()
    _stub_agent(monkeypatch, {
        "status": "pending", "message": "已向 Agent 索取（task_id=7），15 秒内没等到",
    })
    with app.app_context():
        text = PlatformContextProvider(
            loaded=_loaded(tmp_path), use_stored_batch_diff=False
        ).file_diff(seeded.head_sha, LUA)

    assert "还没取回来" in text, text
    assert "不等于「没有改动」" in text, text
    assert "信息缺口" in text, text


def test_an_empty_answer_from_the_node_is_not_read_as_no_change():
    """节点回传的差异是空的 → 明说「读不到」，不说成「没有改动」。

    空串在这个契约里的含义是「确实没有差异」（`ContextProvider` 的约定），而这里拿到
    空只可能是回传出了问题 —— 两者必须分开。
    """
    from services.ai.platform_provider import _render_agent_file_diff

    text = _render_agent_file_diff({"kind": "diff", "file_path": LUA, "content": ""}, path=LUA, commit="a" * 40)

    assert "读不到差异" in text, text
    assert "不等于「没有改动」" in text, text
    assert "信息缺口" in text, text


def test_picking_the_window_never_waits_for_the_business_node(tmp_path, monkeypatch):
    """挑「改动在哪一段」是一次启发式，不该为它等一轮业务节点往返。

    `file_content` 没点名窗口时会先看一眼补丁的块头（`_default_window`）。那次索取只值
    一个行号，而向业务节点要一次要等十几秒 —— 挑不到就退回文件开头，抬头会说明这一段
    是怎么来的（`ask_agent=False` 就是这条边界）。
    """
    seeded = _seed()
    calls = _stub_agent(monkeypatch)
    with app.app_context():
        text = PlatformContextProvider(
            loaded=_loaded(tmp_path), use_stored_batch_diff=False
        ).file_diff(seeded.head_sha, LUA, ask_agent=False)

    assert not calls, f"挑个窗口位置就把业务节点叫醒了：{calls}"
    assert text and "这不等于" in text, text


# ==========================================================================
#  三、Agent 侧：在工作副本上算并就地渲染
# ==========================================================================


class TestAgentSideReader:
    """`services.agent_file_diff_reader.read_file_diff_for_agent`。"""

    @pytest.fixture()
    def head_sha(self):
        return _seed().head_sha

    def _read(self, monkeypatch, repository_id, head_sha, payload, *, diff_data=None, raise_exc=None):
        with app.app_context():
            from services.agent_file_diff_reader import read_file_diff_for_agent

            def _fake(row, previous):
                if raise_exc is not None:
                    raise raise_exc
                return diff_data if diff_data is not None else {
                    "type": "code", "file_path": LUA, "patch": PATCH, "hunks": [],
                }

            monkeypatch.setattr("services.vcs_content_service.get_unified_diff_data", _fake)
            monkeypatch.setattr(
                "services.commit_diff_logic.resolve_previous_commit", lambda row: None
            )
            return read_file_diff_for_agent(
                {"repository_id": repository_id, "commit_id": head_sha, "file_path": LUA, **payload}
            )

    def test_the_rendered_patch_comes_back_with_the_diff_marker(self, monkeypatch, repository_id, head_sha):
        got = self._read(monkeypatch, repository_id, head_sha, {})

        assert got["kind"] == "diff", f"平台据此改口径，标记不能少：{got}"
        assert "target.invincible" in got["content"], got
        assert got["truncated"] is False
        assert got["message"].startswith("file_diff completed")

    def test_a_huge_diff_is_truncated_at_the_tool_limit(self, monkeypatch, repository_id, head_sha):
        """截断在**取数侧**做，且上限与预算层给 `file_diff` 的那一条相同。

        取数侧不切、留给预算层切的话，后一刀会砍在半行中间，而模型据此写进结论里的
        定位就成了假的。
        """
        from services.agent_file_diff_reader import FILE_DIFF_MAX_CHARS
        from services.ai.budget import TRUNCATION_SUFFIX
        from services.ai.context_tools import DEFAULT_TOOL_LIMITS
        from utils.content_window import CONTENT_MAX_CHARS

        assert FILE_DIFF_MAX_CHARS == CONTENT_MAX_CHARS == DEFAULT_TOOL_LIMITS["file_diff"], (
            "三处上限必须是一个数（取数侧、内容窗口、预算层）"
        )

        huge = {
            "type": "code", "file_path": LUA,
            "patch": "\n".join(f"+添了一行内容 {i}" for i in range(4000)), "hunks": [],
        }
        got = self._read(monkeypatch, repository_id, head_sha, {}, diff_data=huge)

        assert got["truncated"] is True
        assert len(got["content"]) == FILE_DIFF_MAX_CHARS, len(got["content"])
        assert got["content"].endswith(TRUNCATION_SUFFIX), "截断要说出来"
        assert got["original_chars"] > FILE_DIFF_MAX_CHARS

    def test_an_unrenderable_structure_raises_instead_of_returning_empty(self, monkeypatch, repository_id, head_sha):
        """算不出可渲染的结构必须抛 —— 返回空串在这个契约里等于「确实没有差异」。"""
        with pytest.raises(Exception) as excinfo:
            self._read(
                monkeypatch, repository_id, head_sha, {},
                diff_data={"认不出来的结构": True},
            )
        assert "无法渲染" in str(excinfo.value), str(excinfo.value)

    def test_a_commit_row_that_does_not_exist_says_which_one(self, monkeypatch, repository_id):
        with app.app_context():
            from services.agent_file_diff_reader import read_file_diff_for_agent

            with pytest.raises(Exception) as excinfo:
                read_file_diff_for_agent({
                    "repository_id": repository_id, "commit_id": "f" * 40,
                    "file_path": "no/such/file.lua",
                })
            message = str(excinfo.value)
        assert "ffffffff" in message and "no/such/file.lua" in message, message

    def test_the_payload_is_validated(self, repository_id):
        with app.app_context():
            from services.agent_file_diff_reader import read_file_diff_for_agent

            for payload in [
                {"commit_id": "a" * 40, "file_path": LUA},
                {"repository_id": repository_id, "file_path": LUA},
                {"repository_id": repository_id, "commit_id": "a" * 40},
            ]:
                with pytest.raises(Exception) as excinfo:
                    read_file_diff_for_agent(payload)
                assert "file_diff 任务缺少" in str(excinfo.value)

    def test_a_missing_repository_row_says_which_id(self):
        """节点上的库里没有这个仓库时：抛出**带 id 的原因**（这套部署最常见的配置错）。"""
        with app.app_context():
            from services.agent_file_diff_reader import read_file_diff_for_agent

            with pytest.raises(Exception) as excinfo:
                read_file_diff_for_agent({
                    "repository_id": 999999999, "commit_id": "a" * 40, "file_path": LUA,
                })
            assert "999999999" in str(excinfo.value), str(excinfo.value)


class TestAgentWiring:
    """这条任务在 Agent 侧真的会被执行到（接线断了的表现是「任务类型不支持」）。"""

    def test_the_type_is_allowed_and_required(self, monkeypatch):
        """**已部署的节点不改任何环境变量也要能跑。**

        少了它的表现不是报错，是「AI 报告里一直有读不到 diff 的信息缺口」——静默降级，
        没人会去查，所以它必须在必做集合里（与 `file_content` 同一个理由）。
        """
        import agent.config as config
        import agent.executor as executor
        from agent.config import load_settings

        assert "file_diff" in config._AGENT_TASK_TYPE_ALLOWED
        assert "file_diff" in config._AGENT_TASK_TYPE_REQUIRED

        monkeypatch.setenv("AGENT_LOCAL_TASK_TYPES", "auto_sync,commit_diff")
        assert "file_diff" in load_settings().local_task_types, (
            "显式配置把 file_diff 过滤掉了 —— 部署方不会知道要加它"
        )

        seen = {}
        monkeypatch.setattr(
            executor, "_execute_task_via_local_runtime",
            lambda task_type, task: seen.update(type=task_type) or ("completed", {}, None, None),
        )
        executor.execute_task(
            {"task_type": "file_diff", "payload": {}}, SimpleNamespace(local_task_types=["file_diff"])
        )
        assert seen.get("type") == "file_diff", f"没被路由到本地运行时：{seen}"

    def test_the_inline_entrypoint_has_a_branch_for_it(self, monkeypatch):
        """`execute_task_inline_for_agent` 里必须有这个分支（Agent 走的就是它）。"""
        seeded = _seed()
        with app.app_context():
            import services.task_worker_service as worker

            monkeypatch.setattr(
                "services.vcs_content_service.get_unified_diff_data",
                lambda row, previous: {
                    "type": "code", "file_path": LUA, "patch": PATCH, "hunks": [],
                },
            )
            monkeypatch.setattr(
                "services.commit_diff_logic.resolve_previous_commit", lambda row: None
            )
            got = worker.execute_task_inline_for_agent(
                "file_diff",
                {"repository_id": seeded.repository_id, "commit_id": seeded.head_sha,
                 "file_path": LUA},
            )

        assert got["kind"] == "diff", got
        assert "target.invincible" in got["content"], got

    def test_the_window_argument_is_ignored_for_diffs(self, monkeypatch):
        """diff 请求没有 `lines`（它就是「改了哪几行」，再给窗口没有意义）。

        两种请求共用同一套「同一份请求才复用」的判据，所以这里顺带钉住：带了 `lines`
        的 payload 不会被当成本次请求的命中对象（`_matches` 对 diff 恒比空串）。
        """
        import services.agent_file_content_dispatch as dispatch
        from services.agent_file_content_dispatch import FILE_DIFF_TASK_TYPE

        assert FILE_DIFF_TASK_TYPE == "file_diff"
        assert "lines" not in dispatch.request_file_diff.__code__.co_varnames, (
            "diff 请求不该有窗口参数 —— 有了就会有调用方传，而 Agent 侧根本不看它"
        )
