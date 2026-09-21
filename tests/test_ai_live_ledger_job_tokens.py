# -*- coding: utf-8 -*-
"""抽屉顶部那个数（「本次已用 N tokens」）在**换成员**时不许回退 —— job 级累计的账。

## 这一组守的是什么（缺陷 AI-P1-08）

引擎报的累计 token 是**一个引擎实例**的累计，而一个引擎实例 = **一个成员**
（子代理模式下一家子有 n 个分片 + 汇总 + 对账，各起一个引擎，见
`services/ai/subagent.py`）。回调那一层（`subagent._call_engine` 的 `report`）只把
「我是谁」贴到帧上，**token 一个字节都不动**；而快照是**整条覆盖写**的
（`run_progress.publish`）。于是换成员那一瞬间，界面上的数从上一个成员收尾时的
几十万掉回新成员的第一轮值 —— 实测 run 13：S3 收尾约 493,531 → MAIN 首轮 34,960。

修法在**快照这一层**（`services/ai/run_progress.py`）：它是每一帧的唯一写入口，
能看出 `agent_index` 变了 —— 换成员时把上一个成员的峰值**入账**，新成员从 0 开始时
把已入账的值加回去。于是快照里同时有三个含义不同的量：

* `job_tokens`：**跨成员**的本次分析累计，单调不减（缺成员的按 0 入账，是**已知下界**）；
* `live_tokens`：**当前成员**的局部量（换成员就从头开始，这是它本来的含义）；
* 落库的账（`run.tokens_input` / `tokens_output`）—— 权威口径，跑完才有（不在这里测）。

## 为什么两处切换都要测

分片之间（S1→S2）与「最后一个分片 → 汇总」（S3→MAIN）在代码上走的是同一段逻辑，
但**只有后者被报障过**（S3 收尾 493,531 紧接着 MAIN 第一轮 34,960，落差最大）。
汇总 → 对账（MAIN→V1）是同一段逻辑的第三次经过：少测一处，下一个「第 n+1 个成员」
就会再犯一次。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import app as flask_app
from app import create_tables, db
from models.ai_analysis import AiAnalysisRun
from services.ai import run_progress
from tests.test_ai_analysis_service import _FakeClient


class _SilentAfter(_FakeClient):
    """前 n 次调用上报用量，之后**一个 token 都不回**（真实存在的网关行为）。

    落库那一份（`_sum_optional`）因此整份变成 `None`，而快照那一份只剩一个已知下界 ——
    两者都必需：一个说「这次花了多少不知道」，一个说「至少这么多」。
    """

    def __init__(self, reporting_calls: int):
        super().__init__()
        self._reporting = reporting_calls
        self._calls = 0

    def complete(self, messages, *, temperature=None):
        from dataclasses import replace

        result = super().complete(messages, temperature=temperature)
        self._calls += 1
        if self._calls > self._reporting:
            result = replace(result, prompt_tokens=None, completion_tokens=None)
        return result


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODULE = "static/js/ai_stream_status.js"


def _frame(
    index: int,
    *,
    prompt: int | None,
    completion: int | None,
    agent: str = "",
    agent_index: int = 0,
    agent_total: int = 0,
    status: str = "requests",
) -> SimpleNamespace:
    """一帧进度，形状与 `services/ai/engine.py` 的 `RoundProgress` 逐键相同。"""
    return SimpleNamespace(
        index=index,
        max_rounds=8,
        status=status,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_tokens=None,
        cache_write_tokens=None,
        requests_used=index,
        requests_remaining=8 - index,
        items_chars=100,
        elapsed_ms=1000,
        agent=agent,
        agent_index=agent_index,
        agent_total=agent_total,
        round_entry=None,
    )


def _family_frames() -> list[SimpleNamespace]:
    """一次真实形状的「3 个分片 + 汇总 + 对账」：每个成员的累计值都是**局部**的。

    每个成员的第一帧是引擎的 `on_start`（`index=0`、token 是 `None`）—— 报障的那一帧
    就是它：换成员之后它先把数字抹掉，等这个成员第一轮跑完才冒出一个局部的小数。
    """
    return [
        _frame(0, prompt=None, completion=None, agent="S1", agent_index=1, agent_total=5,
               status="starting"),
        _frame(1, prompt=100_000, completion=0, agent="S1", agent_index=1, agent_total=5),
        _frame(2, prompt=180_000, completion=20_000, agent="S1", agent_index=1,
               agent_total=5, status="final"),
        _frame(0, prompt=None, completion=None, agent="S2", agent_index=2, agent_total=5,
               status="starting"),
        _frame(1, prompt=250_000, completion=30_000, agent="S2", agent_index=2,
               agent_total=5, status="final"),
        _frame(0, prompt=None, completion=None, agent="S3", agent_index=3, agent_total=5,
               status="starting"),
        _frame(2, prompt=450_000, completion=43_531, agent="S3", agent_index=3,
               agent_total=5, status="final"),
        # 报障的那一帧：汇总开始跑（`agent` 空 = 主代理自己），token 还没有。
        _frame(0, prompt=None, completion=None, agent="", agent_index=4, agent_total=5,
               status="starting"),
        _frame(1, prompt=30_000, completion=4_960, agent="", agent_index=4, agent_total=5),
        _frame(2, prompt=40_000, completion=9_960, agent="", agent_index=4, agent_total=5,
               status="final"),
        # 对账轮（开着时它是第 n+2 步）。
        _frame(0, prompt=None, completion=None, agent="对账", agent_index=5, agent_total=5,
               status="starting"),
        _frame(1, prompt=10_000, completion=1_000, agent="对账", agent_index=5,
               agent_total=5, status="final"),
    ]


def _publish_all(frames, *, run_id: int = 90001) -> list:
    """逐帧写进快照，每写完一帧读一次（界面就是这么轮询的）。"""
    seen = []
    for item in frames:
        run_progress.publish(run_id, 1, item)
        snap = run_progress.snapshot(run_id)
        assert snap is not None
        seen.append(snap)
    return seen


@pytest.fixture(autouse=True)
def _clean_snapshots():
    run_progress.reset_for_tests()
    yield
    run_progress.reset_for_tests()


# ==========================================================================
#  一、job 级累计：单调不减、换成员不回零
# ==========================================================================


def test_the_job_total_never_goes_backwards_over_a_whole_family_run():
    """**这一条就是缺陷本身。** 逐帧看：已经出现过的数永远不许变小。"""
    snaps = _publish_all(_family_frames())

    numbers = [snap.job_tokens for snap in snaps]
    known = [value for value in numbers if value is not None]
    assert known == sorted(known), f"job 累计回退了：{numbers}"

    # 具体的账：每个成员各自从 0 开始编号，但 job 那一栏必须是**加起来**的。
    assert numbers == [
        None,          # 一次调用都还没返回
        100_000,
        200_000,
        200_000,       # S1→S2：新成员还没有数，接着用 S1 的收尾值
        480_000,
        480_000,       # S2→S3
        973_531,
        973_531,       # S3→MAIN：**报障的那一帧，不回零**
        1_008_491,
        1_023_491,
        1_023_491,     # MAIN→对账
        1_034_491,
    ], numbers


def test_switching_from_the_last_shard_to_the_synthesis_does_not_reset():
    """S3 收尾 493,531 → MAIN 第一轮 34,960。**这两帧之间不许掉回 34,960。**"""
    snaps = _publish_all(_family_frames())
    by_index = {(snap.agent, snap.agent_index, snap.index): snap for snap in snaps}

    s3_final = by_index[("S3", 3, 2)]
    main_start = by_index[("", 4, 0)]
    main_first = by_index[("", 4, 1)]

    assert s3_final.job_tokens == 973_531
    assert s3_final.live_tokens == 493_531, "成员局部量（这一个分片烧掉的）"
    assert main_start.job_tokens == 973_531, (
        "汇总一开始跑，数字就掉了 —— 这正是报障的那一幕"
    )
    assert main_first.job_tokens == 973_531 + 34_960, main_first.job_tokens
    assert main_first.live_tokens == 34_960, "局部量仍是这一段的（两个量不是一回事）"


def test_switching_from_the_synthesis_to_the_review_shard_does_not_reset():
    """第三处切换（汇总 → 对账）走的是同一段逻辑，也必须不回零。"""
    snaps = _publish_all(_family_frames())
    before = [item for item in snaps if item.agent == ""][-1]
    after_start = [item for item in snaps if item.agent == "对账"][0]
    after_first = [item for item in snaps if item.agent == "对账"][1]

    assert before.job_tokens == 1_023_491
    assert after_start.job_tokens == 1_023_491, "换到对账轮时数字掉了"
    assert after_first.job_tokens == 1_034_491


def test_a_member_that_never_reports_is_a_lower_bound_that_still_never_regresses():
    """**有成员整块没上报**（上游一个 token 数都不给）：按 0 入账 + 标 `partial`。

    这时 job 那个数是已知下界（缺的那一块不知道有多大），但**它同样不许回退** ——
    「读不到」不是「变小」的理由，否则界面会从 500 掉到 100，看起来像记错了账。
    """
    frames = [
        _frame(1, prompt=300, completion=200, agent="S1", agent_index=1, agent_total=3),
        # S2 全程没上报（引擎的累计值整份是 None）
        _frame(0, prompt=None, completion=None, agent="S2", agent_index=2, agent_total=3,
               status="starting"),
        _frame(2, prompt=None, completion=None, agent="S2", agent_index=2, agent_total=3,
               status="final"),
        # S3 又报数了
        _frame(0, prompt=None, completion=None, agent="S3", agent_index=3, agent_total=3,
               status="starting"),
        _frame(1, prompt=80, completion=20, agent="S3", agent_index=3, agent_total=3,
               status="final"),
    ]

    snaps = _publish_all(frames, run_id=90002)

    numbers = [snap.job_tokens for snap in snaps]
    known = [value for value in numbers if value is not None]
    assert known == sorted(known), f"有成员没上报时回退了：{numbers}"
    assert numbers == [500, 500, 500, 500, 600], numbers
    # 缺的那一块（S2）必须说出来：**换下去那一帧**起就一直挂着（第一帧还没换过成员）。
    assert [snap.job_tokens_partial for snap in snaps] == [False, False, True, True, True]


def test_a_run_that_never_reports_anything_still_says_no_number():
    """一个 token 都没上报时**不许报 0**（0 是结论：一次都没花）。"""
    snaps = _publish_all(
        [
            _frame(0, prompt=None, completion=None, agent="S1", agent_index=1,
                   agent_total=2, status="starting"),
            _frame(1, prompt=None, completion=None, agent="S1", agent_index=1,
                   agent_total=2, status="final"),
        ],
        run_id=90003,
    )

    assert [snap.job_tokens for snap in snaps] == [None, None]
    assert [snap.live_tokens for snap in snaps] == [None, None]


def test_the_pending_call_is_marked_and_only_while_one_is_in_flight():
    """`job_tokens` **不含还没返回的那次调用**（引擎是跑完一轮才报的）。

    `final` 那一帧之后这个成员不再发请求，标注就该收掉 —— 它是这一层唯一能说清
    「这个数为什么比真实花费小」的东西。
    """
    snaps = _publish_all(
        [
            _frame(0, prompt=None, completion=None, agent="S1", agent_index=1,
                   agent_total=2, status="starting"),
            _frame(1, prompt=10, completion=5, agent="S1", agent_index=1, agent_total=2),
            _frame(2, prompt=20, completion=5, agent="S1", agent_index=1, agent_total=2,
                   status="final"),
        ],
        run_id=90004,
    )

    assert [snap.job_tokens_pending_call for snap in snaps] == [True, True, False]


def test_the_ledger_survives_the_snapshot_being_evicted(monkeypatch):
    """**快照被挤掉 ≠ 这次运行结束了。**

    账若跟着快照一起丢（快照有 `MAX_ENTRIES` 与过期两条清理路），下一次报进来的数会
    从当前成员重新数 —— 也就是**又回退一次**，而这一条正是这次要修的东西。
    """
    monkeypatch.setattr(run_progress, "MAX_ENTRIES", 0)  # 每写一帧就把自己挤掉
    for item in _family_frames():
        run_progress.publish(91000, 1, item)
        assert run_progress.snapshot(91000) is None, "快照没被挤掉，这个用例没测到东西"

    monkeypatch.setattr(run_progress, "MAX_ENTRIES", 200)
    run_progress.publish(
        91000, 1,
        _frame(0, prompt=None, completion=None, agent="对账", agent_index=5, agent_total=5,
               status="starting"),
    )

    snap = run_progress.snapshot(91000)
    assert snap is not None
    assert snap.job_tokens == 1_034_491, "账没了：数字从当前成员重新数了一遍"


def test_clearing_the_run_drops_the_ledger_too():
    """跑完就清（模块纪律第 2 条）——**账本要跟着清**。

    留着的后果：同一个 run_id 的下一次（测试里常这样复用）会带着上一次的入账继续加，
    数字凭空多出一截，而且没人看得出来。
    """
    _publish_all(_family_frames(), run_id=90005)
    assert run_progress.snapshot(90005).job_tokens == 1_034_491

    run_progress.clear(90005)
    run_progress.publish(
        90005, 1, _frame(1, prompt=70, completion=30, agent="S1", agent_index=1,
                         agent_total=2)
    )

    assert run_progress.snapshot(90005).job_tokens == 100, "上一次的入账跟过来了"


def test_a_single_agent_run_is_the_same_number_as_before():
    """没开子代理时（没有 `agent_index`）job 与成员局部是同一个数，行为与以前一致。"""
    snaps = _publish_all(
        [
            _frame(1, prompt=1_200, completion=300),
            _frame(2, prompt=2_000, completion=400, status="final"),
        ],
        run_id=90006,
    )

    assert [snap.job_tokens for snap in snaps] == [1_500, 2_400]
    assert [snap.live_tokens for snap in snaps] == [1_500, 2_400]
    assert all(snap.job_tokens_partial is False for snap in snaps)


def test_a_frame_without_token_fields_still_publishes():
    """老调用方 / 测试替身（没有 `prompt_tokens` 这些键）不许把写快照弄挂。"""
    class _Bare:
        index = 1
        max_rounds = 8
        status = "requests"

    run_progress.publish(90007, 1, _Bare())

    snap = run_progress.snapshot(90007)
    assert snap.index == 1 and snap.job_tokens is None


def test_a_hand_built_snapshot_uses_its_own_amount_as_the_job_total():
    """**绕开 `publish` 手搓出来的快照**（截图脚本、测试替身）没有账本。

    那种帧只描述一个成员的第几轮，两个量本来就是同一个数 —— 所以 `job_tokens` 跟着
    局部量走。不让它兜住的话，这类帧在界面上会**一个数都不显示**（截图里那一行少了
    「本次已用 N tokens」），而「与真帧同源同形」正是它们的用途。
    """
    snap = run_progress.ProgressSnapshot(
        run_id=1, project_id=1, index=1, max_rounds=8, status="requests",
        prompt_tokens=180_000, completion_tokens=9_400,
        cache_read_tokens=None, cache_write_tokens=None,
        requests_used=2, requests_remaining=0, items_chars=0, elapsed_ms=0,
        updated_at=0.0,
    )

    assert snap.job_tokens == 189_400 and snap.live_tokens == 189_400

    # 一个 token 都没有的手搓帧同样不编数字。
    bare = run_progress.ProgressSnapshot(
        run_id=1, project_id=1, index=0, max_rounds=8, status="starting",
        prompt_tokens=None, completion_tokens=None,
        cache_read_tokens=None, cache_write_tokens=None,
        requests_used=0, requests_remaining=0, items_chars=0, elapsed_ms=0,
        updated_at=0.0,
    )

    assert bare.job_tokens is None


def test_the_payload_carries_all_three_numbers():
    """三个量各自有名字，**不许复用同一个字段**（这一条钉的是命名与形状）。"""
    _publish_all(_family_frames(), run_id=90008)

    payload = run_progress.snapshot(90008).to_dict()

    assert payload["job_tokens"] == 1_034_491, "job 级（跨成员、单调不减）"
    assert payload["live_tokens"] == 11_000, "当前成员局部（最后一个是对账轮）"
    assert payload["job_tokens_partial"] is False
    assert payload["job_tokens_pending_call"] is False
    assert json.dumps(payload, ensure_ascii=False)


# ==========================================================================
#  二、最终值 = 落库那份账（真跑一家子，只有 HTTP 是假的）
# ==========================================================================

def _fake_client(reporting_calls: int | None):
    """假 client：`reporting_calls=None` 时行为与 `_FakeClient` 逐字相同。"""
    if reporting_calls is None:
        return _FakeClient()
    return _SilentAfter(reporting_calls)


def _run_a_family(monkeypatch, *, reporting_calls: int | None = None):
    """真跑一次「2 个分片 + 汇总」并把最后那一帧快照留下来（清快照那一步换掉）。"""
    import services.ai_analysis_service as ai_service
    from tests.test_ai_run_budget_warning import _prepare_weekly_run
    from tests.test_ai_subagent_wiring import _enable_subagents

    client = _fake_client(reporting_calls)
    with flask_app.app_context():
        create_tables()
        ai_service, project, cfg = _prepare_weekly_run(monkeypatch)
        _enable_subagents(project.id, count=2)
        monkeypatch.setattr(ai_service, "build_endpoint_client", lambda *a, **k: (client, []))
        # 跑完就清是设计（见模块 docstring），但这一条要读**最后那一帧**：
        # 清快照那一步换成记账，别让快照在断言之前消失。
        cleared: list[int] = []
        monkeypatch.setattr(ai_service, "clear_run_progress", lambda run_id: cleared.append(run_id))

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] in ("succeeded", "degraded"), outcome
        run_id = outcome["run_id"]
        assert run_id in cleared, "跑完没有清快照（界面会一直显示「正在跑」）"
        return outcome, run_progress.snapshot(run_id), db.session.get(AiAnalysisRun, run_id)


def test_the_final_job_total_matches_the_run_that_got_persisted(monkeypatch):
    """**最终值要与落库那一份一致**：job 累计不是另算一套账。

    全员都上报时，`job_tokens` 必须**逐字节等于** `run.tokens_input + tokens_output`
    （落库那一份是 `_sum_optional` 跨成员求和，两者是同一个事实的两条来路）。
    """
    outcome, snap, run = _run_a_family(monkeypatch)

    assert snap is not None, "跑完连一帧快照都没有 —— 界面全程「进度不可用」"
    assert run.tokens_input is not None and run.tokens_output is not None, (
        "这个假 client 每次调用都报用量，落库不该是 None"
    )
    assert snap.job_tokens == run.tokens_input + run.tokens_output, (
        f"job 累计 {snap.job_tokens} 与落库的 "
        f"{run.tokens_input + run.tokens_output} 对不上"
    )
    assert snap.job_tokens_partial is False
    # **不是「最后一个成员」的局部量**：一家子（2 个分片 + 汇总）各一次调用，
    # 最后一帧的局部量是 200，而 job 那一栏是全部加起来。
    assert snap.live_tokens == 200 and snap.job_tokens == 200 * 3, (
        f"live={snap.live_tokens} job={snap.job_tokens}"
    )


def test_a_silent_member_leaves_a_marked_lower_bound_not_a_wrong_total(monkeypatch):
    """有成员没上报时：**落库那份是 `None`**（任何成员没上报就整份不报），

    而 job 累计是**已知下界** + `partial` 标记。差额因此是「明确说明的」——
    界面说的是「至少这么多，还有一块没上报」，不是一个假装完整的数。
    """
    outcome, snap, run = _run_a_family(monkeypatch, reporting_calls=1)

    assert snap is not None
    # 只有第一个成员报了数：下界 = 那一次调用的 200。
    assert snap.job_tokens == 200, snap.job_tokens
    assert snap.job_tokens_partial is True, "有成员整块没上报，必须标出来"
    # 落库那一份更保守：有成员没上报 → 整份 `None`（不是 200，也不是 600）。
    assert run.tokens_input is None and run.tokens_output is None
    assert outcome["status"] in ("succeeded", "degraded"), outcome


# ==========================================================================
#  三、前端那一行：读的是 job 累计，且与「当前成员」分开
# ==========================================================================

_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const sandbox = { console: console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'ai_stream_status.js' });
const S = sandbox.AiStreamStatus;
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = { lines: [], job: [], member: [], watch: [] };

for (const pair of cases.progressText) {
    out.lines.push(S.progressText(pair[0], pair[1]));
    out.job.push(S.jobTokens(pair[0]));
    out.member.push(S.memberTokens(pair[0]));
}

function fakeFetch(frames) {
    const queue = frames.slice();
    return function () {
        const frame = queue.length > 1 ? queue.shift() : queue[0];
        if (!frame) return Promise.reject(new Error('offline'));
        return Promise.resolve({ json: function () { return Promise.resolve(frame); } });
    };
}

(async function () {
    for (const spec of cases.watches) {
        const el = { textContent: '初始文案' };
        const ticks = [];
        const watch = S.watchRun(spec.runId, {
            metaEl: el,
            fetchImpl: fakeFetch(spec.frames),
            setInterval: function (fn) { ticks.push(fn); return ticks.length; },
            clearInterval: function () { }
        });
        await watch.first;
        const steps = [el.textContent];
        for (let index = 0; index < (spec.ticks || 1); index += 1) {
            ticks[0]();
            await new Promise(function (resolve) { setTimeout(resolve, 0); });
            steps.push(el.textContent);
        }
        out.watch.push({ steps: steps });
    }
    process.stdout.write(JSON.stringify(out));
})().catch(function (err) {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
});
"""

_RESULTS: dict = {}


def _run_node() -> dict:
    if not shutil.which("node"):
        pytest.skip("环境里没有 Node，跳过真实运行的断言")
    if _RESULTS:
        return _RESULTS
    workdir = PROJECT_ROOT / ".pytest_tmp"
    workdir.mkdir(exist_ok=True)
    driver = workdir / "ai_live_ledger_driver.js"
    payload = workdir / "ai_live_ledger_cases.json"

    def job(value, *, member=None, partial=False, pending=True, agent="", index=0, total=0):
        item = {"round": 2, "max_rounds": 8, "job_tokens": value}
        if member is not None:
            item["live_tokens"] = member
        if partial:
            item["job_tokens_partial"] = True
        item["job_tokens_pending_call"] = pending
        item["agent"] = agent
        item["agent_index"] = index
        item["agent_total"] = total
        return item

    cases = {
        # 0：汇总在跑，job 是加起来的那一份，本分片是它自己那一份。
        "progressText": [
            [job(1_008_491, member=34_960, agent="", index=4, total=4), "running"],
            # 1：同一个数，但不带「本分片」（局部量与 job 相同 = 还没换过成员）。
            [job(34_960, member=34_960, agent="S1", index=1, total=4), "running"],
            # 2：有成员整块没上报。
            [job(600, member=100, partial=True, agent="S3", index=3, total=4), "running"],
            # 3：**旧快照**（没有 job 那几个键）：退回成员局部量，文案与以前一字不差。
            [{"round": 2, "max_rounds": 8, "live_tokens": 92_329}, "running"],
            # 4：job 读不到（一个 token 都没上报）→ 只说轮次，不补 0。
            [job(None, member=None, agent="S1", index=1, total=4), "running"],
        ],
        # 报障那一幕的逐帧：S3 收尾 → 汇总 on_start（`round=0`）→ 汇总第一轮。
        "watches": [
            {
                "runId": 7,
                "ticks": 2,
                "frames": [
                    {"success": True, "status": "running",
                     "progress": {"round": 2, "max_rounds": 8, "job_tokens": 973_531,
                                  "live_tokens": 493_531, "job_tokens_pending_call": True,
                                  "agent": "S3", "agent_index": 3, "agent_total": 4}},
                    {"success": True, "status": "running",
                     "progress": {"round": 0, "max_rounds": 8, "job_tokens": 973_531,
                                  "live_tokens": None, "job_tokens_pending_call": True,
                                  "agent": "", "agent_index": 4, "agent_total": 4}},
                    {"success": True, "status": "running",
                     "progress": {"round": 1, "max_rounds": 8, "job_tokens": 1_008_491,
                                  "live_tokens": 34_960, "job_tokens_pending_call": True,
                                  "agent": "", "agent_index": 4, "agent_total": 4}},
                ],
            }
        ],
    }
    driver.write_text(_DRIVER, encoding="utf-8")
    payload.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        ["node", str(driver), str(PROJECT_ROOT / MODULE), str(payload)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"共享模块在 node 里跑不起来（浏览器里就是整个抽屉不动）：\n"
        f"{result.stdout}\n{result.stderr}"
    )
    _RESULTS.update(json.loads(result.stdout))
    return _RESULTS


def _numbers(line: str) -> list[int]:
    """从那一行字里取出所有 `N tokens` 形态的数（用来判「单调不减」）。"""
    import re

    return [int(value) for value in re.findall(r"(\d+)(?= tokens)", line)]


def test_the_line_reads_the_job_total_not_the_shard_local_amount():
    """**界面上那个数 = job 累计。** 左边写着当前是哪个分片，所以右边必须说清是整次分析。"""
    line = _run_node()["lines"][0]

    assert "本次分析已用 1008491 tokens" in line, line
    assert line.startswith("分析中：分片 汇总 (4/4) · 第 2/8 轮 · "), line
    assert "其中本分片 34960" in line, line
    assert "尚未落库" in line, line


def test_the_two_amounts_are_two_fields():
    """命名与计算都不复用：`jobTokens` 与 `memberTokens` 是两个函数、两个键。"""
    result = _run_node()

    assert result["job"][0] == 1_008_491
    assert result["member"][0] == 34_960
    assert result["job"][1] == result["member"][1] == 34_960, "没换过成员时两者相等"
    # 旧快照（没有 `job_tokens` 这个键）：退回成员局部量 —— 老调用方的行为不变。
    assert result["job"][3] == 92_329 and result["member"][3] == 92_329


def test_a_partial_job_total_says_so_in_the_line():
    line = _run_node()["lines"][2]

    assert "600" in line, line
    assert "没上报" in line, line
    assert "其中本分片 100" in line, line


def test_a_missing_job_total_prints_no_number():
    line = _run_node()["lines"][4]

    assert line == "分析中：分片 S1 (1/4) · 第 2/8 轮", line
    assert "0 tokens" not in line


def test_an_old_snapshot_keeps_the_old_wording():
    """老调用方（快照里没有 job 那个键）：那一行与以前**一字不差**。"""
    line = _run_node()["lines"][3]

    assert line == "分析中：第 2/8 轮 · 本次已用 92329 tokens（尚未落库，只含上游已上报的部分）", line


def test_the_line_never_goes_backwards_while_the_watch_polls():
    """**报障那一幕的逐帧**：S3 收尾 → 汇总起步 → 汇总第一轮。数只许往上走。"""
    steps = _run_node()["watch"][0]["steps"]

    assert len(steps) == 3, steps
    assert "973531" in steps[0], steps[0]
    # 汇总 `on_start`（`round=0`）：一轮还没跑完，这时**不显示数**（不编一个 0），
    # 但也**不许**把上一段那个数换成别的。
    assert "正在调用模型" in steps[1], steps[1]
    assert "0 tokens" not in steps[1], steps[1]
    assert "1008491" in steps[2], steps[2]
    # 那一行里**唯一**一个「N tokens」形态的数就是 job 累计 —— 本分片的量只以
    # 「其中本分片 N」出现，不是一个像账本一样的数（两个含义不许混成一句话）。
    assert _numbers(steps[2]) == [1_008_491], steps[2]
    assert "其中本分片 34960" in steps[2], steps[2]

    numbers = [value for step in steps for value in _numbers(step)]
    assert numbers == sorted(numbers), f"轮询到的数回退了：{numbers}"
