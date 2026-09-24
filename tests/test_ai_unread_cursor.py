# -*- coding: utf-8 -*-
"""未读账与补偿游标（P1b）：**没取到证据的那些文件，为什么没取到，以及下一轮谁先补**。

三件事各测一层：

1. **原因**（`snapshot_store.unread_files`）：未索取 / 被额度拒 / 读不到 / 要过但没结果 ——
   四种处置完全不同（催它、给额度、换坐标、如实写缺口），混成一个集合就只能一律重排；
2. **游标**（`record_unread` / `unread_streaks`）：连着几轮没读到的会被记成 2、3…
   而补偿排序（`compensation_rank`）**先看这个数** —— 否则每轮排出来的次序一样，
   它会被一直挤在名额之外；
3. **启动前预检**（`coverage_precheck`）：必读集比池还多时，开跑前就说得出来。
"""
from __future__ import annotations

import json

import pytest

from app import app, create_tables, db
from services.ai.scope_sampling import compensation_rank
from services.ai.snapshot_store import (
    UNREAD_NO_RESULT,
    UNREAD_NOT_REQUESTED,
    UNREAD_REFUSED,
    UNREAD_UNREADABLE,
    record_unread,
    unread_files,
    unread_streaks,
)
from tests.test_ai_diff_snapshot_baseline import (
    _make_group,
    _paths,
    _record_evidence,
    _run_once,
)


class _Facts:
    """`facts.why(路径, 表清单)` 的替身：命中的路径返回一个理由，其余返回空。"""

    def __init__(self, hits=()):
        self.hits = set(hits)

    def why(self, path, tables=()):
        return "命中重点表" if path in self.hits else ""


class _Entry:
    """白名单条目（`scope_sampling` 只按属性读这几个字段）。"""

    def __init__(self, path, *, repository_id=1, commit_count=1):
        self.file_path = path
        self.repository_id = repository_id
        self.commit_count = commit_count


def _trace(run, *, executed=(), requests=(), dropped=(), round_index=2):
    """落一轮逐轮明细。**轮次从 2 起**：`_record_evidence` 占着第 1 轮（那张表按
    `(run, 轮次)` 唯一，同一轮写两次会撞唯一索引）。"""
    from models.ai_analysis import AiAnalysisTrace

    db.session.add(
        AiAnalysisTrace(
            run_id=run.id,
            round_index=round_index,
            executed_json=json.dumps({"items": len(executed), "details": list(executed)}),
            requests_json=json.dumps({"count": len(requests), "items": list(requests)}),
            dropped_json=json.dumps({"details": list(dropped)}),
        )
    )
    db.session.commit()


def _evidence(path, commit="c0001"):
    return {
        "kind": "file_diff",
        "label": f"file_diff {commit[:12]} {path}",
        "failed": False,
        "empty": False,
    }


def _failure(path, commit="c0001"):
    return {
        "kind": "file_diff",
        "label": f"file_diff {commit[:12]} {path}",
        "failed": True,
        "reason": "[取数失败] 平台读不到这一份",
    }


def test_each_unread_file_says_why_it_was_not_read():
    """四个原因各自归位 —— 它们的处置完全不同。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=8)
        result = _run_once(group)
        run = result["run"]
        read, refused, unreadable, asked = _paths(group, [0, 1, 2, 3])
        never = _paths(group, [4])[0]
        _record_evidence(run, [read], group["commits"])
        _trace(
            run,
            executed=[_evidence(read), _failure(unreadable)],
            requests=[
                {"type": "file_diff", "path": refused},
                {"type": "file_diff", "path": unreadable},
                {"type": "file_diff", "path": asked},
            ],
            dropped=[
                {
                    "kind": "request",
                    "reason": "超出本次工具请求总预算（3 次），未执行",
                    "detail": f"file_diff c0001 {refused}",
                }
            ],
        )

        by_path = {item["path"]: item for item in unread_files(run)}
        assert read not in by_path, "取到证据的文件不该出现在未读账里"
        assert by_path[refused]["reason"] == UNREAD_REFUSED
        assert by_path[unreadable]["reason"] == UNREAD_UNREADABLE
        assert by_path[asked]["reason"] == UNREAD_NO_RESULT
        assert by_path[never]["reason"] == UNREAD_NOT_REQUESTED
        assert by_path[never]["repository_id"] == group["cfg"].repository_id


def test_the_refused_reason_has_a_live_producer():
    """「被额度拒」是按文案认的 —— 发出方那句还在不在，扫源码钉住。

    与 `trace_evidence.FAILURE_NOTICE_PREFIXES` 同一套做法：改那句话就必须改这里，
    否则这一档会**静默失效**（所有被拒的都会落到「要过但没结果」那一档，看着仍然有账）。
    """
    from pathlib import Path

    from services.ai.snapshot_store import REFUSED_REASON_PREFIX

    source = Path("services/ai/context_tools.py").read_text(encoding="utf-8")
    assert REFUSED_REASON_PREFIX in source, (
        "`context_tools.execute` 的拒绝分支不再写那句话了 —— 未读账认不出「被额度拒」"
    )


def test_an_unknown_coverage_reports_nothing_rather_than_everything():
    """没有逐轮明细 ⇒ **未知**，不是「一个都没读到」（与 `evidence_paths` 同一条纪律）。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=6)
        run = _run_once(group)["run"]
        assert unread_files(run) is None
        assert record_unread(run) is None
        assert unread_streaks(run) == {}


def test_the_streak_counts_consecutive_rounds():
    """连着两轮没读到 ⇒ `streak` 是 2；上一轮读到过的 ⇒ 重新从 1 开始。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=6)
        first = _run_once(group)
        keep = _paths(group, [0])[0]
        _record_evidence(first["run"], [keep], group["commits"])
        _trace(first["run"], executed=[_evidence(keep)])
        section = record_unread(first["run"])
        assert section["total"] == 5 and section["counts"] == {UNREAD_NOT_REQUESTED: 5}
        assert all(item["streak"] == 1 for item in section["items"])

        # 第二轮：同样的输入，仍然一个都没读 —— 那两个文件应当是「连续两轮」。
        second = _run_once(group, force_full=True)
        second["run"].request_payload = json.dumps(
            {**json.loads(second["run"].request_payload), "base_run_id": first["run"].id}
        )
        _record_evidence(second["run"], [keep], group["commits"])
        _trace(second["run"], executed=[_evidence(keep)])
        section = record_unread(second["run"])
        streaks = {item["path"]: item["streak"] for item in section["items"]}
        assert streaks and set(streaks) == set(_paths(group, range(6))) - {keep}
        assert all(value == 2 for value in streaks.values()), streaks
        assert unread_streaks(second["run"]) == streaks


def test_the_second_round_reuses_the_cursor_from_the_first():
    """游标从 payload 里读回来（`base_run_id` → 上一轮那条 run 的未读账）。"""
    with app.app_context():
        create_tables()
        group = _make_group(file_count=6)
        first = _run_once(group)
        first["run"].request_payload = json.dumps(
            {
                **json.loads(first["run"].request_payload),
                "unread": {"items": [{"path": _paths(group, [0])[0], "streak": 4}]},
            }
        )
        second = _run_once(group, force_full=True)
        second["run"].request_payload = json.dumps(
            {**json.loads(second["run"].request_payload), "base_run_id": first["run"].id}
        )
        _record_evidence(second["run"], [], group["commits"])
        _trace(second["run"], executed=[])
        section = record_unread(second["run"])
        streaks = {item["path"]: item["streak"] for item in section["items"]}
        assert streaks[_paths(group, [0])[0]] == 5, "上一条运行记着 4 轮，这一轮该是 5"


def test_a_consecutively_unread_file_overtakes_a_riskier_one():
    """补偿排序：**连续未读**压过「命中关键路径」。"""
    risky = _Entry(_paths(None, [1])[0], commit_count=9)
    stale = _Entry(_paths(None, [2])[0], commit_count=1)
    facts = _Facts(hits=[risky.file_path])
    common = {"tables": (), "facts": facts}
    ranked = sorted(
        [risky, stale],
        key=lambda entry: compensation_rank(
            entry,
            repo_priority=1,
            streaks={stale.file_path: 2},
            **common,
        ),
        reverse=True,
    )
    assert ranked[0] is stale, "连着两轮没读到的被挤到后面了 —— 与从前没区别"
    # 没有未读账（老运行）时退回按风险排 —— 加这一项之前就是这个次序。
    ranked = sorted(
        [risky, stale],
        key=lambda entry: compensation_rank(entry, repo_priority=1, streaks={}, **common),
        reverse=True,
    )
    assert ranked[0] is risky


@pytest.mark.parametrize(
    "assigned,pool,expected",
    [
        (40, 250, ""),
        (250, 250, ""),
        (260, 250, "缺口 10 条"),
        (0, 0, ""),
    ],
)
def test_the_coverage_precheck_says_it_before_the_run(assigned, pool, expected):
    from services.ai.mandatory_progress import coverage_precheck

    note = coverage_precheck(assigned=assigned, pool=pool)
    if expected:
        assert expected in note, note
    else:
        assert note == "", note


def test_the_precheck_is_attached_to_the_plan_it_gates():
    """接上 `apply_manifest`：清单挂上去的那一刻，预检那句话就留在额度账上了。"""
    from services.ai.engine import EngineLimits
    from services.ai.family_ledger import AssignedFile
    from services.ai.manifest import build_manifest
    from services.ai.subagent import apply_manifest, plan_family

    plan = plan_family(mode="weekly", enabled=True, count=2, limits=EngineLimits())
    assert plan is not None and plan.quota is not None
    manifest = build_manifest(
        [
            {
                "repository_id": 1,
                "repository_name": "repo",
                "commit": f"{index:040x}",
                "path": f"code/m{index}.lua",
            }
            for index in range(plan.quota.requests_pool + 5)
        ],
        shard_count=plan.count,
    )
    attached = apply_manifest(plan, manifest)
    assert attached.quota.coverage_note, "必读集比池还多，却没在启动前说出来"
    assert "覆盖预检" in attached.quota.coverage_note
    # 必读集在池内时一个字都不说（那是绝大多数批次）。
    small = build_manifest(
        [
            {
                "repository_id": 1,
                "repository_name": "repo",
                "commit": "a" * 40,
                "path": "code/only.lua",
            }
        ],
        shard_count=plan.count,
    )
    assert apply_manifest(plan, small).quota.coverage_note == ""
    assert all(isinstance(item, AssignedFile) for item in attached.members[0].assigned_files)
