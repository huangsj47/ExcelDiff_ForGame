# -*- coding: utf-8 -*-
"""代码（.lua）文件的 diff：AI 必须读到**页面上那一份**。

## 用户报的现象

周版本 AI 分析的结论里写着「战斗逻辑、吸灵器链、拓扑组队、支付等代码改动**均未取到
diff 正文**，上述代码类结论为『建议 + 待人工确认』」。而那个 lua 仓库就配在项目里、
周版本页面上也有 diff。

## 追下去是什么

`PlatformContextProvider.file_diff` 原先只有一条取数路：`get_unified_diff_data`
（现场用平台**本地工作副本**重算一遍）。三件事叠在一起，代码文件就成了唯一读不到的：

* **代码文件没有单文件 diff 缓存** —— `DiffCache` / `ExcelDiffCacheService` 都是配表
  专用的（`is_excel_file`），所以配表看得见、代码看不见。这不是「谁忘了写」，
  而是「缓存只做了一半」；
* platform/agent 模式下平台**被显式禁止** clone（`get_file_content_from_git` 直接
  返回 `None`），那条实时路径必然一个字节也读不到；
* 读不到时文本比较器把 `None` 当空串（`DiffService._decode_text(None) == ""`），
  算出来的载荷与「两版逐字节相同」**一模一样**（都是空补丁），渲染层只能说
  「取到了记录但没有补丁内容」—— **那是一句假话**：模型据此认定「这里没什么可看的」，
  而不是「我读不到」。

同时平台其实**早就把这份 diff 算好并落库了**：周版本同步时
`weekly_version_logic.generate_weekly_merged_diff` 会把每个文件（含代码）的合并 diff
写进 `WeeklyVersionDiffCache.merged_diff_data`，周版本页面读的就是它
（`weekly_version_file_diff_api`）。

## 修法（这一组测试守的就是这三条）

1. `file_diff` **先读那份已落库的 diff**（页面同源），读不到才退回实时计算；
2. `get_unified_diff_data` 在「当前版本内容读不出来」时返回 **error 载荷**，不再产出
   一个冒充「没有改动」的空补丁 —— 配表侧一直有这道闸门（`_read_excel_data(None)`
   直接抛错），只有文本漏了；
3. 渲染层认识 `segmented_diff`（一个文件被窗口内**不相邻**的几次提交改过时的形态），
   否则代码文件在那种窗口里又变回「取数失败」。

`file_content` 那条路在 platform/agent 模式下**仍然是 None**（它同样要读本地工作副本，
而平台的 diff/内容缓存都没有代码全文）—— 这是本次没修的另一半，见文末那条测试。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import app, create_tables, db
from models import Commit, Project, Repository
from models.weekly_version import WeeklyVersionConfig, WeeklyVersionDiffCache
from services.ai.platform_provider import (
    PlatformContextProvider,
    render_diff_payload,
)
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
# 「取到了记录但没有补丁内容」是修前那句话。它把「读不到」说成了「没有内容」，
# 所以整组测试都拿它当反面判据（而不是断言某个正面措辞）。
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


def _seed(*, stored_payload: object = "default", operation: str = "M",
          envelope_extra: dict | None = None):
    """造一个代码仓库 + 两条提交 + 一条周版本合并 diff 缓存行。

    全部用 uuid 后缀：测试库是**会话级共用**的，按全局计数断言会「全量绿、子集红」。
    `stored_payload` 传 `None` 表示「没有已落库的 diff」（模拟实时路径才是唯一来源）。
    """
    now = datetime.now(timezone.utc)
    with app.app_context():
        create_tables()
        project = Project(code=_uid("PT"), name="代码 diff 溯源")
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
                   operation=operation, author="b", commit_time=now - timedelta(days=1),
                   message="无敌帧"),
        ])
        config = WeeklyVersionConfig(
            project_id=project.id, repository_id=repository.id, name=_uid("周版本"),
            branch="main", start_time=now - timedelta(days=7), end_time=now,
        )
        db.session.add(config)
        db.session.flush()

        if stored_payload != "default":
            payload = stored_payload
        else:
            # 代码文件的合并 diff 是 `get_commit_range_diff` 的产物：补丁在 `patch` 里
            # （与文本侧的 `raw_diff` 不是一个键，见 `_render_code` / `_render_text`）。
            payload = {"type": "code", "file_path": LUA, "patch": PATCH, "hunks": []}
        if payload is not None:
            envelope = {
                "file_path": LUA, "file_type": "text", "base_commit": base_sha,
                "latest_commit": head_sha, "commits_count": 1,
                "commit_ids": [head_sha], "operations": ["M"],
                "merge_strategy": "single",
                "diff_data": payload, "merged_diff": payload,
            }
            envelope.update(envelope_extra or {})
            db.session.add(WeeklyVersionDiffCache(
                config_id=config.id, repository_id=repository.id, file_path=LUA,
                file_type="code",
                merged_diff_data=json.dumps(envelope, ensure_ascii=False),
                base_commit_id=base_sha, latest_commit_id=head_sha,
                cache_status="completed", overall_status="pending",
            ))
        db.session.commit()
        return SimpleNamespace(
            project_id=project.id, repository_id=repository.id,
            head_sha=head_sha, base_sha=base_sha, config_id=config.id,
        )


def _provider(tmp_path) -> PlatformContextProvider:
    return PlatformContextProvider(loaded=_loaded(tmp_path))


@pytest.fixture(autouse=True)
def _platform_mode(monkeypatch):
    """整组测试跑在 **platform** 模式（平台 + Agent）下，也就是缺口真正发生的那种部署。

    两个理由：一是这个模式里平台**被禁止** clone（`get_file_content_from_git` 立刻
    返回 `None`），缺口才现形 —— 单机模式下平台自己有工作副本，实时那条路本来是好的；
    二是它让整组测试**不碰网络**（不设置的话，取不到工作副本时 GitService 会真的去
    `git clone` 那个 `example.invalid`，测试变成看 DNS 脸色的慢用例）。
    """
    monkeypatch.setenv("DEPLOYMENT_MODE", "platform")


# ==========================================================================
#  一、代码 diff 的来源：页面那一份
# ==========================================================================


def test_the_code_diff_is_the_one_the_page_shows(tmp_path):
    """**这一条就是用户报的那个缺口。**

    已落库的周版本合并 diff 就在库里，AI 却看不到 —— 因为 `file_diff` 只用本地工作副本
    现场重算，而 platform/agent 模式下平台没有工作副本。
    """
    seeded = _seed()
    with app.app_context():
        text = _provider(tmp_path).file_diff(seeded.head_sha, LUA)

    assert text is not None, "已落库的 diff 就在库里，却还是取不到"
    assert "target.invincible" in text, text
    assert FALSE_CLAIM not in text, text


def test_the_stored_diff_wins_over_recomputing_it(tmp_path, monkeypatch):
    """顺序：**先读已落库的那一份**，读不到才实时重算。

    这一条是行为断言（不是静态扫描）：如果实现被改成「先实时、失败才读缓存」，
    实时那条路在真实部署里是**读不到**（返回空补丁而不是异常），于是顺序一反就又回到
    用户报的缺口上 —— 而这个测试会红，因为实时返回的是另一份内容。
    """
    seeded = _seed()
    monkeypatch.setattr(
        "services.vcs_content_service.get_unified_diff_data",
        lambda commit, previous: {
            "type": "text", "file_path": LUA,
            "raw_diff": "@@ -1 +1 @@\n-实时算出来的\n+实时算出来的另一版\n",
            "hunks": [],
        },
    )
    with app.app_context():
        text = _provider(tmp_path).file_diff(seeded.head_sha, LUA)

    assert "target.invincible" in text, f"读的不是已落库的那一份：{text}"
    assert "实时算出来的" not in text, text


def test_recomputing_is_still_the_fallback(tmp_path, monkeypatch):
    """没有已落库的 diff 时，实时计算照旧 —— 单机模式（平台自己 clone）行为不变。

    这是「只在该修的地方改」的守卫：反过来写（无论有没有缓存都只读缓存）会让所有
    没跑过周版本同步的文件连一次 diff 都拿不到。
    """
    seeded = _seed(stored_payload=None)
    monkeypatch.setattr(
        "services.vcs_content_service.get_unified_diff_data",
        lambda commit, previous: {
            "type": "text", "file_path": LUA,
            "raw_diff": "@@ -1 +1 @@\n-旧\n+新\n", "hunks": [],
        },
    )
    with app.app_context():
        text = _provider(tmp_path).file_diff(seeded.head_sha, LUA)

    assert text is not None and "-旧" in text and "+新" in text, text


def test_a_cache_row_for_another_commit_is_not_used(tmp_path, monkeypatch):
    """缓存行按 `latest_commit_id` 精确匹配：问的是别的提交就不能拿这一行顶。

    「拿最近的凑」是一类很容易顺手写下的近似（按 file_path 取最新一行），代价是
    模型问 A 提交、拿到 B 提交的 diff，而它没有任何办法发现。
    """
    seeded = _seed()
    monkeypatch.setattr(
        "services.vcs_content_service.get_unified_diff_data",
        lambda commit, previous: {
            "type": "text", "file_path": LUA,
            "raw_diff": "@@ -1 +1 @@\n-旧\n+新\n", "hunks": [],
        },
    )
    other_sha = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    with app.app_context():
        db.session.add(Commit(repository_id=seeded.repository_id, commit_id=other_sha,
                              path=LUA, operation="M", author="c",
                              commit_time=datetime.now(timezone.utc), message="再改一次"))
        db.session.commit()
        row = Commit.query.filter_by(commit_id=other_sha, path=LUA).first()
        text = _provider(tmp_path).file_diff(row.commit_id, LUA)

    assert "实时" not in text or "+新" in text, text
    assert "target.invincible" not in text, f"拿了别的提交的缓存行：{text}"


def test_a_merged_window_says_which_commits_it_covers(tmp_path):
    """一个窗口里不止一条提交时，必须告诉模型「这不是单独某一条提交的改动」。

    模型的索取形状是 `file_diff(commit=X, path=p)` —— 一个问「提交 X 改了什么」的问法，
    而缓存里那份覆盖的是整段窗口。不说清楚，它会把整段窗口的改动都算到 X 头上，
    而它没有任何办法发现（读报告的人更没有）—— 这正是这一层最忌讳的那类错。
    """
    seeded = _seed(envelope_extra={
        "commit_ids": ["0f1e2d3c" * 5, "9a8b7c6d" * 5],
        "commits_count": 2, "merge_strategy": "consecutive",
    })
    with app.app_context():
        text = _provider(tmp_path).file_diff(seeded.head_sha, LUA)

    assert "合并差异" in text and "2 条提交" in text, text
    assert "不是单独某一条提交的改动" in text, text
    # 说明里带上覆盖区间，读报告的人才能自己去核对
    assert "0f1e2d3c" in text and "9a8b7c6d" in text, text
    # 补丁本身仍然要在（说明是加在正文之前的，不是替代它）
    assert "target.invincible" in text, text


def test_a_single_commit_window_needs_no_provenance_note(tmp_path):
    """只有一条提交时，这份 diff 逐字就是「这条提交的差异」，加说明只是噪音。"""
    seeded = _seed()
    with app.app_context():
        text = _provider(tmp_path).file_diff(seeded.head_sha, LUA)

    assert "合并差异" not in text, text
    assert text.startswith("代码差异："), text


def test_a_single_commit_analysis_does_not_borrow_the_batch_window(tmp_path):
    """**单提交分析要关掉这条来源。**

    它问的是「这一条提交改了什么」，而缓存里那一份覆盖的是整段窗口 —— 两者在
    页面上的呈现本来就不一样（提交页走 `get_diff_data`，周版本页走合并 diff）。
    借错了不会报错，只会让模型把别的提交的改动算到这一条上。
    """
    seeded = _seed(stored_payload=None)
    merged = _seed()
    assert merged.repository_id != seeded.repository_id
    with app.app_context():
        text = PlatformContextProvider(
            loaded=_loaded(tmp_path),
            use_stored_batch_diff=False,
        ).file_diff(merged.head_sha, LUA)

    # 关掉之后只剩实时那条路；这里没有工作副本，于是给的是「读不到」而不是
    # 从缓存里借来的那份窗口 diff。
    assert text is not None, "取不到时也必须给一句话（None 是「拿不到」，会变成「取数失败」）"
    assert "target.invincible" not in text, text
    assert "取数失败" in text or "这不等于" in text, text


# ==========================================================================
#  二、「读不到」不许说成「没有改动」
# ==========================================================================


def test_an_unreadable_version_is_reported_as_an_error_not_as_an_empty_diff(
    tmp_path, monkeypatch
):
    """**这是那句假话的根源。**

    平台上几乎所有取数失败都返回 `None`，而文本比较器把 `None` 当空串 —— 于是
    「当前版本读不出来」（本地工作副本不存在 / 提交里没有这个路径）算出来的载荷，
    与「两版逐字节相同」是同一个东西：空补丁。上层拿它没辙，只能写
    「取到了记录但没有补丁内容」。

    配表侧一直有这道闸门（`_read_excel_data(None)` 直接抛错），文本侧漏了 ——
    所以「代码仓库看不到 diff」只发生在代码文件上。
    """
    seeded = _seed(stored_payload=None)

    def _only_the_baseline_is_readable(repository, commit_id, path):
        # 当前版本读不到（None），基线读得到：这正是「本地没有工作副本 / 路径对不上」。
        return None if commit_id == seeded.head_sha else b"function BattleMgr:onHit() end\n"

    monkeypatch.setattr(
        "services.vcs_content_service.get_file_content_from_git",
        _only_the_baseline_is_readable,
    )
    with app.app_context():
        from services.commit_diff_logic import resolve_previous_commit
        from services.vcs_content_service import get_unified_diff_data

        row = Commit.query.filter_by(commit_id=seeded.head_sha, path=LUA).first()
        payload = get_unified_diff_data(row, resolve_previous_commit(row))

    assert payload.get("type") == "error", payload
    assert "这不等于" in str(payload.get("message")), payload
    # 反面：不许再产出一个「看起来像没改动」的文本载荷
    assert payload.get("type") != "text" or payload.get("raw_diff"), payload


def test_a_deleted_file_is_still_rendered_as_a_deletion(tmp_path, monkeypatch):
    """删除态**不能**被上面那道闸门吃掉。

    「文件被删了」与「读不到」都表现为 `current_content is None`，区别只能由提交的
    操作码来说（删除是显式声明的，见 `DiffService.process_deleted_file`）。
    闸门写宽一格，被删掉的 lua 就会从「整份删除」变成「读取出错」。
    """
    seeded = _seed(stored_payload=None, operation="D")
    monkeypatch.setattr(
        "services.vcs_content_service.get_file_content_from_git",
        lambda repository, commit_id, path: (
            None if commit_id == seeded.head_sha else b"function BattleMgr:onHit() end\n"
        ),
    )
    with app.app_context():
        from services.commit_diff_logic import resolve_previous_commit
        from services.vcs_content_service import get_unified_diff_data

        row = Commit.query.filter_by(commit_id=seeded.head_sha, path=LUA).first()
        payload = get_unified_diff_data(row, resolve_previous_commit(row))

    assert payload.get("type") == "text", payload
    assert "onHit" in str(payload.get("raw_diff")), payload


def test_the_empty_patch_notice_refuses_to_be_read_as_no_change():
    """就算真到了「有记录、没正文」这一步，也不许让模型读成「这里没问题」。

    这句话是模型唯一能看到的信号。修前它是「取到了记录但没有补丁内容」——
    一个把「我没读到」和「没有改动」合并掉的说法，而这正是本模块存在的理由
    （见 `services/ai/platform_provider.py` 的模块文档）。
    """
    for kind, key in (("text", "raw_diff"), ("code", "patch")):
        text = render_diff_payload({"type": kind, "file_path": LUA, key: ""})
        assert text is not None, kind
        assert FALSE_CLAIM not in text, text
        assert "这不等于" in text, text


# ==========================================================================
#  三、分段合并（代码文件在周版本里最常见的形态之一）
# ==========================================================================


def _segmented():
    def segment(index, patch):
        return {
            "type": "code", "file_path": LUA, "patch": patch,
            "segment_info": {"current": f"c{index}", "previous": f"p{index}",
                             "segment_index": index, "total_segments": 2},
        }

    return {
        "type": "segmented_diff", "file_path": LUA, "total_segments": 2,
        "segments": [segment(1, "@@ -3 +3 @@\n-甲\n+乙\n"),
                     segment(2, "@@ -9 +9 @@\n-丙\n+丁\n")],
    }


def test_a_segmented_code_diff_is_rendered_segment_by_segment():
    """`segmented_diff` 是「一个文件被窗口内不相邻的几次提交改过」的形态。

    渲染层原先不认识它（没有 `sheets`、也没有 `patch`）→ 一路落到 `return None`
    → 模型看到的是「取数失败」。配表侧早有 `extract_excel_diff_from_payload` 把分段
    合并起来，文本/代码侧没有对应的东西。
    """
    text = render_diff_payload(_segmented(), path=LUA)

    assert text is not None, "分段 diff 又变回「取数失败」了"
    assert "-甲" in text and "+乙" in text, text
    assert "-丙" in text and "+丁" in text, text
    # 两段各自标出「哪两条提交之间」：拼成一整段连续补丁会让模型读成一次改动
    # （`@@` 行号本来也不连续）。
    assert "第 1/2 段" in text and "第 2/2 段" in text, text
    assert "p1 → c1" in text, text


def test_a_segmented_diff_with_no_usable_segment_says_so():
    """一段都渲染不出来时必须明说，不能返回 `None`（那是「取不到内容」的意思）。

    `render_diff_payload` 对认不出来的结构返回 `None`（契约见它的文档），而
    「分段里一段都认不出来」是**内容问题**不是协议问题 —— 说成「取数失败」会让模型
    去重试一个永远重试不出来的东西。
    """
    text = render_diff_payload(
        {"type": "segmented_diff", "file_path": LUA, "total_segments": 1,
         "segments": [{"认不出来的结构": True}, "连字典都不是"]},
        path=LUA,
    )
    assert text is not None and "没有一段" in text, text


def test_a_stored_segmented_diff_reaches_the_model(tmp_path):
    """已落库的载荷是分段形态时，整条路也要通（缓存行的外壳 → 载荷 → 渲染）。"""
    seeded = _seed(stored_payload={"type": "segmented_diff", "file_path": LUA,
                                   "total_segments": 2,
                                   "segments": _segmented()["segments"]})
    with app.app_context():
        text = _provider(tmp_path).file_diff(seeded.head_sha, LUA)

    assert text is not None and "-甲" in text, text
    assert FALSE_CLAIM not in text, text


# ==========================================================================
#  四、文件正文：原来的「另一半」现在也通了（说明见下面两条）
# ==========================================================================


def test_reading_a_code_file_body_says_why_when_there_is_no_source(tmp_path):
    """平台本地读不到代码全文时：**给出可读的原因**，而不是 None/空串。

    这条原先断言的是 `file_content(...) is None`（「已知没修的另一半」）。现在它有了
    第二条来源（platform/agent 模式下让 Agent 在自己的工作副本上读，见
    `services/agent_file_content_dispatch.py`），所以这段测试改成正面的负面断言：

    * 项目**没绑 Agent** 时取不到 —— 这仍是事实，不能假装拿到了；
    * 但**必须说清楚为什么**（绑没绑、在不在线、是不是还在路上），并且**明确否掉**
      「读不到 = 没有内容 / 没有改动」这个读法 —— 那句话正是本文件存在的理由。

    深一层的行为（派发、有界等待、复用、失败重试）在
    `tests/test_ai_file_content_from_agent.py`。
    """
    seeded = _seed()
    with app.app_context():
        text = _provider(tmp_path).file_content(seeded.head_sha, LUA)

    assert text is not None and text != '', f'不能返回 None/空串（空串=「确实没有内容」）：{text!r}'
    assert '没有绑定 Agent' in text, f'原因要说清楚：{text!r}'
    assert '不等于「没有内容」' in text and '不等于「没有改动」' in text, text
    assert '信息缺口' in text, '要让模型知道该把它写成信息缺口，而不是推断出一个结论'

