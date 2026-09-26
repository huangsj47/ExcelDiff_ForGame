# -*- coding: utf-8 -*-
"""单次 token 硬上限：**在再发起一次调用之前**就该停住（P1-1）。

## 这一组盯的是哪一种假绿

原先这道闸只接在家族分支的 `should_skip` 上。要钉住它真的生效，光断言「有个函数返回了
非空 reason」是不够的 —— 那种实现照样能把超额的请求发出去。所以这里的判据落在**行为**上：

* 假 client 记录每一次调用；上限不够时 **`len(client.calls)` 必须停在那个数上**；
* 停不是静默的：`degradation` 必须是 `DEGRADE_BUDGET`，而不是 `succeeded`；
* 反向的一半同样要钉：上限宽裕时**不许**拦（否则「永远拦」也能让上面那条通过）。

## 缺用量那一路必须单独测

上游没报 `prompt_tokens`/`completion_tokens` 时，把「未知」当成 0 就等于无限放行 ——
单次上限永远判「还没超」。所以有一条用例专门喂一个不报用量的 client，断言它**仍然被拦住**。
"""
from __future__ import annotations

import dataclasses

from services.ai.analysis_plan import make_single_run_guard
from services.ai.auto_sizing import plan_analysis, single_run_guard
from services.ai.budget_gate import SingleRunBudget
from services.ai.engine import (
    DEGRADATION_LABELS,
    DEGRADE_BUDGET,
    DEGRADE_NONE,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    EngineLimits,
    EngineOutcome,
    run_analysis,
)
from services.ai.llm_client import ChatResult
from services.ai.rules import RuleThresholds
from services.ai.subagent import attach_seed, plan_family, run_family
from tests.test_ai_analysis_plan import DIMENSIONS, _config_three
from tests.test_ai_engine import (
    COMMIT,
    TABLE,
    FakeProvider,
    _final,
    _loaded,
    _requests,
    _scope,
)
from tests.test_ai_subagent_family import CHANGE_SUMMARY, LIMITS

# 一份「上限 1000、本轮预留 200、收尾预留 100」的账 —— 三个数都取小整数，
# 好让下面的算式一眼可验：付得起的条件是 `已花 + 300 <= 1000`。
CAP, ROUND_RESERVE, REPORT_RESERVE = 1000, 200, 100


class CountingClient:
    """按脚本回答，并把每一次调用记下来 —— 判据就是**它被叫了几次**。

    `prompt_tokens` / `completion_tokens` 可置 `None`，用来演「上游没报用量」
    （`ChatResult` 的既有口径：`None` 是「没报」，与「报了 0」是两件事）。
    """

    def __init__(self, *replies, prompt_tokens=None, completion_tokens=None):
        self._replies = list(replies)
        self._prompt = prompt_tokens
        self._completion = completion_tokens
        self.calls: list[list[dict]] = []

    def complete(self, messages, **kwargs):
        # 形参用 `**kwargs` 收：引擎会按配置传 `temperature`，配了输出上限时还会传
        # `max_tokens`。少收一个就是 TypeError，表现不是「测试失败」而是「这一轮炸了」。
        self.calls.append([dict(item) for item in messages])
        index = min(len(self.calls) - 1, len(self._replies) - 1)
        return ChatResult(
            text=self._replies[index],
            model="fake",
            prompt_tokens=self._prompt,
            completion_tokens=self._completion,
        )


def _budget(**overrides) -> SingleRunBudget:
    fields = {
        "total_token_budget": CAP,
        "round_reserve_tokens": ROUND_RESERVE,
        "report_reserve_tokens": REPORT_RESERVE,
    }
    fields.update(overrides)
    return SingleRunBudget(**fields)


def _run(client, budget, **overrides):
    kwargs = {
        "client": client,
        "provider": FakeProvider(),
        "loaded": _loaded(),
        "scope": _scope(),
        "change_summary": CHANGE_SUMMARY,
        "thresholds": RuleThresholds(),
        "single_run_budget": budget,
    }
    kwargs.update(overrides)
    return run_analysis(**kwargs)


# ==========================================================================
# 一、闸门真的拦在调用之前
# ==========================================================================


def test_the_round_it_cannot_afford_is_never_sent():
    """第 1 轮花了 900，第 2 轮就该被拦下 —— **请求一个字节都不发出去**。

    付得起第 2 轮的条件是 `900 + 300 <= 1000`，它不成立（差 200）。
    """
    client = CountingClient(_requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}), _final(), prompt_tokens=500, completion_tokens=400)

    outcome = _run(client, _budget())

    assert len(client.calls) == 1, "付不起的那一轮还是发出去了"
    assert outcome.degradation == DEGRADE_BUDGET
    assert outcome.status != STATUS_SUCCEEDED
    # 停下来的理由要能给人看，而不是只留一个枚举名。
    assert DEGRADATION_LABELS[DEGRADE_BUDGET] in outcome.error_message


def test_a_round_it_can_still_afford_runs_normally():
    """反向的一半：上限宽裕时**不许**拦。

    没有这条，一个「无条件返回 blocked」的实现也能让上面那条全绿 —— 而那正是
    「闸门装反了」这个 bug 的样子。
    """
    client = CountingClient(_requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}), _final(), prompt_tokens=100, completion_tokens=50)

    outcome = _run(client, _budget())

    assert len(client.calls) == 2, "上限够用却把第二轮拦掉了"
    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.degradation == DEGRADE_NONE


def test_the_lookahead_is_the_same_gate_pushed_one_round_ahead():
    """前瞻**不许**写成第二个算式：它是同一道闸门作用在「本轮结束后的预计花费」上。

    比较对象的算式只写一遍（`spent + max(本轮预留, 上一轮实花)`），四个档位逐档同判 ——
    没有这条，「前瞻」和「闸门」迟早会各自漂移，而那种漂移不报错，只表现为
    「模型有时被告知、有时不被告知」。
    """
    budget = _budget()

    for spent, last in ((0, None), (400, 400), (700, 60), (900, 1000)):
        budget.spent_tokens = spent
        expected = single_run_guard(
            budget, spent_tokens=spent + max(ROUND_RESERVE, last or 0)
        )
        assert budget.next_round_affordable(last) == (not expected["blocked"]), (spent, last)


def test_the_last_affordable_round_is_told_the_money_is_running_out():
    """**钱这条也要提前说一声**（2026-09-26 真机 run 3 的直接教训）。

    闸门只在**钱已经花掉之后**才说话，而轮次与索取额度各自都有提前的收敛指令 ——
    钱这条原先一句话都没有。run 3 因此跑了 9 轮、9 轮全在索取上下文、**一次结论都没写**，
    第 10 轮开头被拦下，整次运行以「没有拿到可用的结论」收场。

    判据落在**提示词正文**上（这一条要验的就是「模型有没有被告知」）：
    * 第 1 轮不许说 —— 那时按同一道闸门还付得起下一轮，说了就是过早收敛；
    * 第 2 轮必须说 —— 它的下一轮已经付不起了（`400 + max(200, 400) + 200 + 100 > 1000`）。

    数字：每轮花 400（prompt 200 + completion 200）、本轮预留 200、收尾预留 100、上限 1000。
    """
    client = CountingClient(
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}),
        _final(),
        prompt_tokens=200,
        completion_tokens=200,
    )

    outcome = _run(client, _budget())

    first = "\n".join(item["content"] for item in client.calls[0])
    second = "\n".join(item["content"] for item in client.calls[1])
    # 判据取这段指令**自己**的一句话（「token 预算」四个字在内置协议里本来就有，
    # 拿它当针会指到别的段落上去）。
    needle = "再索取一轮很可能就没有下一轮"
    assert needle not in first, "第 1 轮还付得起下一轮，却让模型提前收敛"
    assert needle in second, "钱只够这一轮了，模型却没被告知 —— 它会一路索取到被拦"
    # 钱是**估算**，不许说成轮次那条硬事实的措辞（同一句话会被模型抄进报告的信息缺口）。
    assert "最后一条消息" not in second, "钱这条按「最后一轮」说话 —— 那是一句估算，不是事实"
    # 停下来的理由仍然是钱（闸门在下一轮开头拦下），不是「轮次用尽」。
    assert outcome.degradation == DEGRADE_BUDGET


def test_an_unreported_usage_is_estimated_instead_of_treated_as_zero():
    """上游不报用量时**仍然要拦得住** —— 把「未知」当 0 等于无限放行。

    `prompt_tokens`/`completion_tokens` 都是 `None`：这一轮的钱只能靠估。
    真按 0 记的话，第 2 轮会被放出去（`len(client.calls) == 2`），
    而「单次上限」就成了一句永远不会生效的话。
    """
    client = CountingClient(_requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}), _final())  # 两个用量字段都是 None

    outcome = _run(client, _budget())

    assert len(client.calls) == 1, "没报用量就一路放行 —— 单次上限成了一句话"
    assert outcome.degradation == DEGRADE_BUDGET


def test_an_unreported_usage_is_booked_as_a_conservative_estimate():
    """账本本身要看得见那笔估算，并且**标着它是估算**。

    这一条直接量账（不设上限，于是闸门一律放行，账照样要记）：

    * 缺用量时记的是「请求字符 + 配置的输出上限」，所以那个上限必须出现在账上 ——
      按 0 记的话这里会是几百而不是几万；
    * `spent_estimated` 必须为真：报告与界面据此才能写「其中含保守估算的部分」，
      而不是把估出来的数当真值展示。

    后一半是反向对照：上游报了用量时，账要**正好**等于两个数之和，且**不许**标估算。
    """
    silent = _budget(total_token_budget=0)
    _run(
        CountingClient(_requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}), _final()),
        silent,
        limits=EngineLimits(max_output_tokens=50_000),
    )

    assert silent.spent_tokens >= 50_000, "缺用量那一笔没按输出上限保守估"
    assert silent.spent_estimated is True

    reported = _budget(total_token_budget=0)
    _run(
        CountingClient(_final(), prompt_tokens=1_234, completion_tokens=56),
        reported,
    )

    assert reported.spent_tokens == 1_290
    assert reported.spent_estimated is False


def test_a_report_of_the_halt_says_the_figure_contains_an_estimate():
    """估算出来的账必须**自己说**是估算（`single_run_guard` 的那句 mark）。

    报告与界面据此才能写「其中含保守估算的部分」，而不是把估算当成真值展示。
    """
    budget = _budget()

    budget.note_usage(800, estimated=True)

    assert budget.affordable() is False
    assert "保守估算" in budget.stop_note()


def test_no_configured_cap_is_not_a_cap_of_zero():
    """`total_token_budget = 0` 是**没配上限**，不是「上限为 0」——一律放行。"""
    client = CountingClient(_requests({"type": "file_diff", "commit": COMMIT, "path": TABLE}), _final(), prompt_tokens=999_999, completion_tokens=999_999)

    outcome = _run(client, _budget(total_token_budget=0))

    assert len(client.calls) == 2
    assert outcome.status == STATUS_SUCCEEDED


def test_a_cap_below_the_reserves_blocks_before_spending_anything():
    """上限连「一轮预留 + 收尾预留」都不够时，**第一轮就不发**。

    这是刻意选的：真付不起就不该起跑。这一次的结局是 `failed` 加一句能对上号的理由，
    而不是先把钱花了再报一句「超预算了」。
    """
    client = CountingClient(_final())

    outcome = _run(client, _budget(total_token_budget=100))

    assert client.calls == []
    assert outcome.status == STATUS_FAILED
    assert outcome.degradation == DEGRADE_BUDGET


# ==========================================================================
# 二、判据只有一份（不许出现第二套口径）
# ==========================================================================


def _real_plan():
    """一份**真的由规划器算出来**的计划（不是手搓的替身）。

    这个文件的其余用例都在手搓三个数，那是对的 —— 它们测的是算术。但「两处口径一致」
    这件事必须在**真实计划**上验：手搓的计划里口径不会漂，真实计划里才会
    （`total_token_budget` 与两个预留都是规划器按窗口反解出来的）。
    """
    return plan_analysis(_config_three(), 580_000, DIMENSIONS)


def _boundary(plan) -> int:
    """付得起的上限：「已花」越过它就拦。判据只有 `single_run_guard` 那一份算式。"""
    return plan.total_token_budget - plan.round_reserve_tokens - plan.report_reserve_tokens


def test_the_account_and_the_plan_guard_share_one_formula():
    """`SingleRunBudget.check()` 与 `single_run_guard(plan, …)` 必须逐步同判。

    这条防的是「引擎一套、编排层另一套」—— 那种分叉只会表现为「同一个上限，两条路
    一个拦得住一个拦不住」，而它不会报任何错。
    """
    plan = _real_plan()
    edge = _boundary(plan)

    for spent, estimated in ((0, False), (edge, False), (edge + 1, False), (edge + 1, True)):
        budget = SingleRunBudget.from_plan(plan, spent_tokens=spent, spent_estimated=estimated)
        expected = single_run_guard(plan, spent_tokens=spent, spent_estimated=estimated)
        assert budget.check() == expected, spent


def test_the_engine_denial_is_the_same_verdict_as_the_family_guard():
    """家族那道 `should_skip` 与引擎这道闸，在同一份账上给出**同一个答案**。

    编排层用 `make_single_run_guard` 决定跳不跳成员，引擎用 `SingleRunBudget.affordable`
    决定发不发下一轮。两者口径必须一致：一个成员被判「跑不起」的额度状态，
    在引擎里也该是「付不起」—— 否则会出现「成员被判跑不起所以跳过」而引擎那边其实
    还愿意跑，或者反过来：成员起跑了、引擎却半路喊停。
    """
    plan = _real_plan()
    edge = _boundary(plan)
    guard = make_single_run_guard(plan)

    # 阈值两侧各取一个，外加「远远不够」那一档。
    for spent in (0, edge, edge + 1, plan.total_token_budget):
        skipped = bool(guard(plan.members[0], spent))
        blocked = not SingleRunBudget.from_plan(plan, spent_tokens=spent).affordable()
        assert skipped == blocked, spent


# ==========================================================================
# 三、家族路径：同一个账本要进到每一个成员的引擎里
# ==========================================================================


class RecordingEngine:
    """假引擎：只记下它收到了什么，不真跑。"""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return EngineOutcome(status=STATUS_SUCCEEDED, report_markdown="# 变更理解\nx")


def _family_plan():
    plan = plan_family(mode="weekly", enabled=True, count=2, limits=LIMITS)
    assert plan is not None
    return plan


def _family():
    return attach_seed(_family_plan(), loaded=_loaded(), change_summary=CHANGE_SUMMARY)


def _run_family(engine, **overrides):
    kwargs = {
        "client": object(),
        "provider": None,
        "loaded": _loaded(),
        "scope": _scope(),
        "change_summary": CHANGE_SUMMARY,
        "run_analysis_fn": engine,
    }
    kwargs.update(overrides)
    return run_family(plan=_family(), **kwargs)


def test_the_exploration_steps_share_one_account_and_the_report_step_is_exempt():
    """**探路的分片拿到同一个账本；产出报告的那一步不接闸。**

    两半都要钉，而且它们是同一条产品决定的两面：

    * 分片拿到的是**同一个对象** —— 成员 A 花掉的钱，成员 B 的引擎在轮首要看得见。
      各发一份的话，「这家子花了多少」在成员内部就又是盲的。
    * 汇总**不接**：它是唯一产出最终报告的一步，拦下它等于把「已查部分 + 缺口」的报告
      换成一次彻底的失败（同样的钱、更差的结果）。真的付不起该由起跑前的闸门挡。
    """
    roles: list[str] = []

    class RoleAware(RecordingEngine):
        def __call__(self, **kwargs):
            # 角色按**任务书的抬头**认（`subagent_tasks` 里三种任务书的首行各不相同）。
            # 不按调用顺序猜：顺序那套在「对账轮开着/关着」两种情形下含义不同。
            task = str(kwargs.get("task_message") or "")
            roles.append("汇总" if task.startswith("# 分工：你是主代理") else "分片")
            return super().__call__(**kwargs)

    engine = RoleAware()
    budget = _budget()

    _run_family(engine, single_run_budget=budget)

    assert len(roles) == 3, roles
    assert roles.count("汇总") == 1, roles
    for role, call in zip(roles, engine.calls):
        if role == "汇总":
            assert "single_run_budget" not in call, "汇总那一步被接了闸 —— 报告会被整份丢掉"
        else:
            assert call["single_run_budget"] is budget, "分片没拿到共享账本"


def test_the_verify_round_also_carries_the_account():
    """对账轮是**探路**（找反证），所以要接账本；它没跑成只是「结论没经过复核」，
    如实写进报告即可 —— 与汇总那种「拦下就等于整份报告没了」是两回事。
    """
    roles: list[str] = []

    class RoleAware(RecordingEngine):
        def __call__(self, **kwargs):
            task = str(kwargs.get("task_message") or "")
            roles.append("对账轮" if task.startswith("# 分工：你是对账轮") else "其他")
            return super().__call__(**kwargs)

    engine = RoleAware()
    plan = attach_seed(
        dataclasses.replace(_family_plan(), verify=True),
        loaded=_loaded(),
        change_summary=CHANGE_SUMMARY,
    )
    budget = _budget()

    run_family(
        client=object(),
        provider=None,
        plan=plan,
        loaded=_loaded(),
        scope=_scope(),
        change_summary=CHANGE_SUMMARY,
        run_analysis_fn=engine,
        single_run_budget=budget,
    )

    assert "对账轮" in roles, roles
    index = roles.index("对账轮")
    assert engine.calls[index]["single_run_budget"] is budget


def test_the_family_does_not_pass_the_key_when_there_is_no_account():
    """不传账本时**这个关键字根本不出现** —— 老调用点与测试的假引擎才不会被多塞一个参数。

    判据用的是一个**签名严格**的假引擎（没有 `**kwargs`）：只要多传来一个它不认的
    关键字，这里就是 `TypeError`。
    """
    seen: list[str] = []

    def strict_engine(*, limits, task_message, **rest):
        seen.append(task_message)
        # 白名单式断言：账本这个键不该出现。
        assert "single_run_budget" not in rest, rest.keys()
        return EngineOutcome(status=STATUS_SUCCEEDED, report_markdown="# 变更理解\nx")

    _run_family(strict_engine)

    assert len(seen) == 3


# ==========================================================================
# 四、生产接线：真跑一次之后，账本**确实**被交到了引擎手上
# ==========================================================================


def test_a_real_weekly_run_hands_the_account_to_the_engine(monkeypatch):
    """**「接线了」不等于「被用了」。**

    上面所有用例都是直接调 `run_analysis(single_run_budget=…)` —— 它们证明不了
    `ai_analysis_service` 真的会传这个参数。这里跑一次**真正的周版本分析**
    （假 client、假取数、真服务路径），用一个记账用的假引擎把交过来的参数截下来。

    判据有两层：
    1. 那个关键字**在**（没接线的实现这里就是 `KeyError`）；
    2. 它带的三个数**等于落库那份计划里的三个数** —— 传一个别的计划算出来的账，
       等于在拦一个不存在的上限。
    """
    from datetime import datetime, timezone

    from app import app, create_tables, db
    from models.ai_analysis import AiAnalysisRun
    from services import ai_analysis_service as ai_service
    from services.ai.analysis_plan import plan_of
    from tests.test_ai_analysis_service import (
        COMMIT_SHA,
        TABLE_PATH,
        _create_project,
        _create_repo,
        _create_weekly_config,
        _FakeClient,
        _seed_diff_cache,
        _uid,
    )

    handed: list = []

    def recording_engine(**kwargs):
        handed.append(kwargs.get("single_run_budget"))
        return EngineOutcome(status=STATUS_SUCCEEDED, report_markdown="# 变更理解\nx")

    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("table"), "svn", "table")
        cfg = _create_weekly_config(
            project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
        )
        _seed_diff_cache(
            cfg, repo, TABLE_PATH, datetime.now(timezone.utc), commit_id=COMMIT_SHA
        )
        db.session.commit()

        ai_service.set_project_api_key(project.id, "k")
        ai_service.update_project_analysis_config(
            project.id,
            {
                "api_base_url": "http://127.0.0.1:15721/v1",
                "api_model": "deepseek-v4-flash",
                "subagent_enabled": False,
            },
        )
        db.session.commit()

        monkeypatch.setattr(ai_service, "build_endpoint_client", lambda *a, **k: (_FakeClient(), []))
        monkeypatch.setattr(ai_service, "run_analysis", recording_engine)

        outcome = ai_service.run_weekly_analysis_background(cfg.id)
        assert outcome["status"] == "succeeded", outcome

        run = db.session.get(AiAnalysisRun, outcome["run_id"])
        # `request_payload` 落库时是 JSON 文本（这一份就是事后审计读的那一份）。
        plan = plan_of(__import__("json").loads(run.request_payload))
        assert plan is not None, "这一份 payload 里没有计划"

    assert handed, "引擎没有被调用"
    budget = handed[0]
    assert isinstance(budget, SingleRunBudget), "账本没有交到引擎手上"
    assert budget.total_token_budget == plan.total_token_budget
    assert budget.round_reserve_tokens == plan.round_reserve_tokens
    assert budget.report_reserve_tokens == plan.report_reserve_tokens
    # 一次全新的运行：已花从 0 起算，而且这一刻还算不上估算。
    assert budget.spent_tokens == 0
    assert budget.spent_estimated is False
