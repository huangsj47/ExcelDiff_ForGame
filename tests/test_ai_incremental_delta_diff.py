# -*- coding: utf-8 -*-
"""增量分析的**每个文件的差异**是「上一轮 → 这一轮」，不是整个版本窗口。

## 为什么要单独钉

改动之前，增量只做了一件事：**少给几个文件**。每个仍被选中的文件，正文还是那一行
缓存里**整窗口**的合并 diff —— 与全量那一轮看到的逐字相同（`_weekly_stored_diff` 按
`(repository_id, file_path, latest_commit_id)` 取那一行，那一行覆盖的是整个窗口）。

于是「增量」这个词在两个维度上含义不同，而模型只看得到第一个：文件名维度是「上次之后
变化的那些」，正文维度却是「这周一共改了什么」。代价不只是多付一遍 token —— 模型会把
窗口早期的改动当成**这次**的改动来汇报。

现在写入侧给每个变化文件填一个比较基线（`diff_base_commit_id` = 上一轮身份里的
`latest_commit_id`），取数侧按 `上一轮 → 这一轮` 现算两点 diff。这一组钉三件事：

1. **基线填对了**：只在「上一轮看过、且这一轮换了提交」时才填 —— 新文件与补偿项不能
   有基线（那时整窗口**就是**它的全部改动）；
2. **取数真的走了那一段**：被比较的是 `(这一轮的 latest, 上一轮的 latest)`；
3. **模型知道这一点**：范围说明里必须写明「这 N 个只给了新增那一段」，差异正文里
   必须带出处说明。少了第 3 条，模型会把「这里没有」读成「这周没改过」 —— 与
   `_batch_provenance`（说明「比你以为的宽」）是同一件事的两个方向。

## 测试库是会话级共用的

断言一律按本用例自己造的行过滤，不写全局 count()。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from services.ai import snapshot_store
from services.ai.change_set import from_weekly_payload
from services.ai.platform_provider import PlatformContextProvider

_PATH = "config/table_{index}.xlsx"
_T0 = datetime(2026, 9, 14, 0, 0, 0)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _group(*, files: int = 4) -> dict:
    project = Project(code=_uid("P"), name=_uid("proj"), department="QA")
    db.session.add(project)
    db.session.flush()
    repo = Repository(
        project_id=project.id, name=_uid("repo"), type="git",
        url=f"https://example.com/{_uid('r')}.git", branch="main",
        resource_type="table", clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    cfg = WeeklyVersionConfig(
        project_id=project.id, repository_id=repo.id, name=_uid("weekly"),
        branch="main", start_time=_T0, end_time=_T0 + timedelta(days=7),
        is_active=True, auto_sync=True, status="active",
    )
    db.session.add(cfg)
    db.session.flush()
    commits = {}
    for index in range(files):
        path = _PATH.format(index=index)
        commit = f"{index:02d}" + uuid.uuid4().hex[:38]
        commits[path] = commit
        db.session.add(
            WeeklyVersionDiffCache(
                config_id=cfg.id, repository_id=repo.id, file_path=path,
                file_type="excel", merged_diff_data="{}",
                base_commit_id="b" * 40, latest_commit_id=commit,
                commit_count=1, cache_status="completed", diff_version="1.18.0",
            )
        )
    db.session.commit()
    return {"project": project, "repo": repo, "cfg": cfg, "commits": commits}


def _paths(group, indices) -> list:
    return [_PATH.format(index=index) for index in indices]


def _move(group, indices) -> dict:
    """这些文件有了新提交（换掉 `latest_commit_id`），返回新提交号。"""
    moved = {}
    for index in indices:
        path = _PATH.format(index=index)
        row = WeeklyVersionDiffCache.query.filter_by(
            config_id=group["cfg"].id, file_path=path
        ).one()
        row.latest_commit_id = f"n{index:02d}" + uuid.uuid4().hex[:37]
        moved[path] = row.latest_commit_id
        group["commits"][path] = row.latest_commit_id
    db.session.commit()
    return moved


def _entry(payload, path) -> dict:
    for item in payload.get("delta_files") or ():
        if item.get("file_path") == path:
            return item
    raise AssertionError(f"delta_files 里没有 {path}")


class TestTheBaselineIsFilledOnlyWhenItIsReal:
    def test_a_file_that_changed_since_last_time_carries_its_previous_latest(self):
        with app.app_context():
            create_tables()
            group = _group()
            # 第一轮：没有基线 → 全量，顺手把这一份清单冻成快照。
            payload, _state, skip = ai_service.build_weekly_payload(group["cfg"].id)
            assert skip is None
            run = ai_service._create_run(
                project_id=group["project"].id, target_type="weekly",
                target_id=group["cfg"].id, target_key=payload["group"]["key"],
                response_mode="blocking", scope=payload["scope"],
                trigger_source="manual", payload=payload,
            )
            run.status = "succeeded"
            db.session.commit()

            before = dict(group["commits"])
            moved = _move(group, [1, 2])

            payload2, _state2, skip2 = ai_service.build_weekly_payload(group["cfg"].id)
            assert skip2 is None, skip2
            for path, new_commit in moved.items():
                entry = _entry(payload2, path)
                assert entry.get("diff_base_commit_id") == before[path], (
                    f"{path} 的比较基线不是上一轮看到的那条提交"
                )
                assert entry["latest_commit_id"] == new_commit

            # **没变的那些不许有基线** —— 它们根本不该进 delta；真进来了（补偿/依赖）
            # 也该给整窗口那一份，因为整窗口就是它们的全部改动。
            for index in (0, 3):
                path = _PATH.format(index=index)
                for item in payload2.get("delta_files") or ():
                    if item.get("file_path") == path:
                        assert not item.get("diff_base_commit_id"), (
                            "没换提交的文件被填了比较基线 —— 会把它整窗口的改动砍掉"
                        )

    def test_full_scope_never_carries_a_baseline(self):
        """全量分析要的是整窗口，逐文件砍成一段是错的。"""
        with app.app_context():
            create_tables()
            group = _group()
            ai_service.build_weekly_payload(group["cfg"].id)[0]
            run = ai_service._create_run(
                project_id=group["project"].id, target_type="weekly",
                target_id=group["cfg"].id,
                target_key=ai_service.build_weekly_payload(group["cfg"].id)[0]["group"]["key"],
                response_mode="blocking", scope="full", trigger_source="manual",
                payload=ai_service.build_weekly_payload(group["cfg"].id)[0],
            )
            run.status = "succeeded"
            db.session.commit()
            _move(group, [1])

            payload, _state, skip = ai_service.build_weekly_payload(
                group["cfg"].id, force_full=True
            )
            assert skip is None
            assert payload["scope"] == "full"
            for item in payload["delta_files"]:
                assert not item.get("diff_base_commit_id"), (
                    "全量分析里带了比较基线 —— 每个文件只剩一段，剩下的改动看不见了"
                )


class TestTheReadPathComparesTheTwoVersions:
    """取数真的走「上一轮 → 这一轮」，而且模型被告知这件事。"""

    def _provider(self, payload, *, use_stored=True):
        return PlatformContextProvider(
            loaded=SimpleNamespace(readable={}, skills={}),
            use_stored_batch_diff=use_stored,
            delta_bases=ai_service._delta_bases(payload),
        )

    def test_the_diff_is_computed_between_the_two_versions(self, monkeypatch):
        with app.app_context():
            create_tables()
            group = _group()
            db.session.add(
                WeeklyVersionDiffCache(
                    config_id=group["cfg"].id, repository_id=group["repo"].id,
                    file_path=_PATH.format(index=1), file_type="excel",
                    merged_diff_data="{}", base_commit_id="b" * 40,
                    latest_commit_id="x" * 40, commit_count=1,
                    cache_status="completed", diff_version="1.18.0",
                )
            )
            db.session.commit()
            old_latest = "x" * 40
            new_latest = group["commits"][_PATH.format(index=1)]
            payload = {
                "delta_files": [{
                    "file_path": _PATH.format(index=1),
                    "latest_commit_id": new_latest,
                    "diff_base_commit_id": old_latest,
                }]
            }
            provider = self._provider(payload)

            seen = []

            def _fake_unified_diff(current, previous):
                seen.append((current.latest_commit_id, previous.commit_id))
                return {"type": "excel", "sheets": {}}

            monkeypatch.setattr(
                "services.vcs_content_service.get_unified_diff_data", _fake_unified_diff
            )
            monkeypatch.setattr(
                "services.ai.platform_provider.render_diff_payload",
                lambda *_a, **_k: "SHEET 渲染结果",
            )
            row = WeeklyVersionDiffCache.query.filter_by(
                config_id=group["cfg"].id, file_path=_PATH.format(index=1),
                latest_commit_id=new_latest,
            ).one()
            text, failed = provider._local_file_diff(row, new_latest, _PATH.format(index=1))

            assert seen == [(new_latest, old_latest)], (
                f"比较的不是「上一轮 → 这一轮」：{seen}"
            )
            assert failed is False
            assert "上一轮分析之后新增的那一段" in text, (
                "差异正文里没有出处说明 —— 模型会把「这里没有」读成「这周没改过」"
            )
            assert old_latest[:8] in text and new_latest[:8] in text, text[:200]

    def test_without_a_baseline_it_falls_back_to_the_whole_window(self, monkeypatch):
        """新文件 / 补偿项没有基线 → 照旧给平台已落库的整窗口那一份。"""
        with app.app_context():
            create_tables()
            group = _group()
            path = _PATH.format(index=0)
            commit = group["commits"][path]
            payload = {"delta_files": [{"file_path": path, "latest_commit_id": commit}]}
            provider = self._provider(payload)
            assert ai_service._delta_bases(payload) == {}

            called = []

            def _fake_stored(*_args, **_kwargs):
                called.append(True)
                return {"type": "excel", "sheets": {}}, "出处说明"

            monkeypatch.setattr(
                "services.ai.platform_provider._weekly_stored_diff", _fake_stored
            )
            monkeypatch.setattr(
                "services.ai.platform_provider.render_diff_payload",
                lambda *_a, **_k: "整窗口渲染结果",
            )
            row = WeeklyVersionDiffCache.query.filter_by(
                config_id=group["cfg"].id, file_path=path
            ).one()
            text, failed = provider._local_file_diff(row, commit, path)

            assert called, "没有回落到「平台已落库的那一份」"
            assert failed is False
            assert "整窗口渲染结果" in text

    def test_a_failed_delta_read_falls_back_to_the_wider_one(self, monkeypatch):
        """增量那一段取不到时**退回更宽的那一份**，而不是交一句「没有改动」。

        退回更宽是安全的：更宽那一份自带 `_batch_provenance` 的出处说明（覆盖了整个
        批次）。反过来交一句空的等于告诉模型「这里没什么可看的」。
        """
        with app.app_context():
            create_tables()
            group = _group()
            path = _PATH.format(index=2)
            commit = group["commits"][path]
            payload = {
                "delta_files": [{
                    "file_path": path, "latest_commit_id": commit,
                    "diff_base_commit_id": "y" * 40,
                }]
            }
            provider = self._provider(payload)

            monkeypatch.setattr(
                "services.vcs_content_service.get_unified_diff_data",
                lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("取不到")),
            )
            monkeypatch.setattr(
                "services.ai.platform_provider._weekly_stored_diff",
                lambda *_a, **_k: ({"type": "excel", "sheets": {}}, "（出处：整窗口）"),
            )
            monkeypatch.setattr(
                "services.ai.platform_provider.render_diff_payload",
                lambda *_a, **_k: "整窗口渲染结果",
            )
            row = WeeklyVersionDiffCache.query.filter_by(
                config_id=group["cfg"].id, file_path=path
            ).one()
            text, failed = provider._local_file_diff(row, commit, path)

            assert failed is False
            assert "整窗口渲染结果" in text, "取不到增量那一段时没有退回更宽的那一份"


class TestTheModelIsToldAboutTheNarrowerDiff:
    def test_the_scope_note_counts_them_and_says_what_it_means(self):
        payload = {
            "scope": "incremental",
            "summary": {"window_files": 100, "batch_files": 3},
            "delta_files": [
                {"file_path": "a.xlsx", "latest_commit_id": "c1",
                 "diff_base_commit_id": "b1"},
                {"file_path": "b.xlsx", "latest_commit_id": "c2",
                 "diff_base_commit_id": "b2"},
                {"file_path": "c.xlsx", "latest_commit_id": "c3"},
            ],
        }
        change = from_weekly_payload(payload)
        assert "只给了上次分析之后新增的那一段" in change.summary, change.summary
        assert "其中 2 个文件" in change.summary, change.summary
        assert "不要把这部分说成「没有改动」" in change.summary, change.summary

    def test_a_full_payload_says_nothing_about_it(self):
        """没有基线时一个字都不说 —— 「没这回事」与「0 个」要分得开。"""
        payload = {
            "scope": "full",
            "summary": {"window_files": 100, "batch_files": 100},
            "delta_files": [{"file_path": "a.xlsx", "latest_commit_id": "c1"}],
        }
        change = from_weekly_payload(payload)
        assert "只给了上次分析之后新增的那一段" not in change.summary, change.summary


class TestTheCacheIsKeyedByTheRealComparison:
    def test_the_baseline_goes_into_the_cache_key(self, monkeypatch):
        """`get_unified_diff_data` 的读缓存与写缓存必须拿到同一个基线。

        服务头部那条注释写得很清楚：读的时候不传、让它自解析「权威基线」，写的时候用
        真实基线落库，两边就会各说各话 —— 请求「c3 对 c1」的区间比较会直接命中先前
        「c3 对 c2」留下的缓存行，返回 c2 的内容，不报错也不重算（本文件历史缺陷）。
        所以这里断言：传下去的那个对象**带 `commit_id`**。
        """
        with app.app_context():
            create_tables()
            group = _group()
            path = _PATH.format(index=1)
            commit = group["commits"][path]
            payload = {
                "delta_files": [{
                    "file_path": path, "latest_commit_id": commit,
                    "diff_base_commit_id": "z" * 40,
                }]
            }
            provider = PlatformContextProvider(
                loaded=SimpleNamespace(readable={}, skills={}),
                delta_bases=ai_service._delta_bases(payload),
            )
            captured = {}

            def _fake_unified_diff(_current, previous):
                captured["previous"] = previous
                return {"type": "excel", "sheets": {}}

            monkeypatch.setattr(
                "services.vcs_content_service.get_unified_diff_data", _fake_unified_diff
            )
            monkeypatch.setattr(
                "services.ai.platform_provider.render_diff_payload",
                lambda *_a, **_k: "x",
            )
            row = WeeklyVersionDiffCache.query.filter_by(
                config_id=group["cfg"].id, file_path=path
            ).one()
            provider._local_file_diff(row, commit, path)

            assert getattr(captured["previous"], "commit_id", None) == "z" * 40


def _strip_comments(source: str) -> str:
    """剥掉注释再断言 —— 本仓库的注释习惯是**把要禁掉的写法原样写进注释**。"""
    without_docstrings = re.sub(r'""".*?"""', " ", source, flags=re.S)
    return re.sub(r"#[^\n]*", " ", without_docstrings)


def test_the_run_wiring_hands_the_bases_to_the_provider():
    """**接线了不等于被用了。**

    `_delta_diff` 与 `_delta_bases` 的行为由上面几条用例保证，而「起分析时真的把
    `payload` 里的基线交给了 provider」只能在这里钉 —— 漏掉它不会有任何报错：模型照旧
    拿到整窗口的正文，报告完全正常，只是多付了 token、把窗口早期的改动当成这次的。

    这条是**源码级**接线断言（与 `tests/test_weekly_snapshot_digest_gate.py` 里
    「调度器有没有查指纹」同一种手法），它证明的是「调用在」，不是「值传对了」；
    值那一半由 `test_the_baseline_goes_into_the_cache_key` 等用例覆盖。
    """
    from pathlib import Path

    source = _strip_comments(
        Path("services/ai_analysis_service.py").read_text(encoding="utf-8")
    )
    assert "delta_bases=_delta_bases(payload)" in source, (
        "起分析时没有把增量基线交给 provider —— 增量会退回「整窗口 diff，只是少给几个文件」"
    )
