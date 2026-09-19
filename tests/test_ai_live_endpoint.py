"""对着**真实模型**跑一遍 AI 分析：协议能不能对上、方法论到底有没有用。

## 为什么需要这个文件

前面那些模块的单测全都用假 provider，它们能证明「代码按设计走」，**证明不了「这套
方法论对真实模型有效」** —— 提示词写得再讲究，模型不照做就是零。所以这里拿真端点跑：

1. 客户端的两个端点（`/chat/completions`、`/models`）在真实服务上是不是这个形态；
2. 把真实的 `SKILL.md` + 项目知识包 + 变更清单喂给模型，看它**是否按协议**先索取
   文件、再输出带证据的结论；
3. **双向检查**：种进去的真问题要被报出来，种进去的「看起来像 bug 的合理设计」
   （阶段性屏蔽 / 灰度开关 / 故意提前 return）**不许**被报出来。

第 3 条的正向尤其重要：只测「报得出来」会让提示词一路滑向「宁可多报」，
而本功能的第一目标是精度优先。

## 怎么跑

    裸跑即可（本机有代理时会真的调用模型，约 10~60 秒）：
        python -m pytest tests/test_ai_live_endpoint.py -q

    只想跑不快的那部分：
        python -m pytest -m "not live"

## 关于密钥

**绝不把 token 写进这个文件**（仓库会推到 GitHub）。运行时从 `~/.codex/hooks.json`
的命令模板里读出来，读不到就 skip。也可以直接用环境变量覆盖：

    AIDIFF_LIVE_BASE_URL / AIDIFF_LIVE_API_KEY / AIDIFF_LIVE_MODEL

## 这个文件里有一段「手写的编排循环」

`services/ai/engine.py` 还没写，所以下面的 `_run_analysis_loop` 用二十几行把编排
**临时**实现了一遍（首轮不给 diff → 模型索要 → 执行 → 加进消息 → 再来一轮）。
engine.py 落地之后，这段应当换成调用 engine —— 它现在的作用是：**在引擎写出来之前就
能验证方法论本身**，并且顺带把「引擎该做什么」用可执行的代码固定下来。

## 不会碰的东西

不启动服务、不写数据库、不读 `instance/`。只对 127.0.0.1 上的本地代理发 HTTP 请求。
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.parse
from pathlib import Path

import pytest

from services.ai.context_tools import ContextTools
from services.ai.engine import EngineLimits, RoundProgress, run_analysis
from services.ai.llm_client import LLMClient
from services.ai.prompt import (
    CommitSummary,
    FileChange,
    build_system_prompt,
    build_user_message,
    render_change_summary,
)
from services.ai.protocol import (
    STATUS_NEED_MORE_CONTEXT,
    ProtocolError,
    ground_payload,
    parse_payload,
    sanitize_requests,
)
from services.ai.rules import RuleThresholds, normalize_anomalies
from services.ai.scope import AnalysisScope
from services.ai.skill_loader import load_skills

REPO_ROOT = Path(__file__).resolve().parents[1]

# 测试用的提交与文件。commit 用 40 位哈希，与真实一致（协议里的短前缀解析也依赖它）。
COMMIT = "f" * 40
GOODS_TABLE = "config/30_goods/goods.xlsx"
REWARD_SCRIPT = "scripts/reward_service.lua"
ACTIVITY_SCRIPT = "scripts/activity_service.lua"
CHANGED_PATHS = (GOODS_TABLE, REWARD_SCRIPT, ACTIVITY_SCRIPT)


# ==========================================================================
# 端点发现（读配置，不读源码里的常量）
# ==========================================================================


def _endpoint_from_codex_config() -> tuple[str, str] | None:
    """从 `~/.codex/hooks.json` 的命令模板里解析出端点与 bearer。

    只解析，不回显。取不到就返回 None（调用方 skip）。
    """
    config = Path.home() / ".codex" / "hooks.json"
    if not config.is_file():
        return None
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    commands: list[str] = []

    def _walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "command" and isinstance(value, str):
                    commands.append(value)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    for command in commands:
        url = re.search(r"'(http[^']+)'", command)
        token = re.search(r"-H 'Authorization: Bearer ([^']+)'", command)
        if url and token:
            parsed = urllib.parse.urlsplit(url.group(1))
            return f"{parsed.scheme}://{parsed.netloc}", token.group(1)
    return None


def _live_settings() -> tuple[str, str, str] | None:
    base_url = (os.environ.get("AIDIFF_LIVE_BASE_URL") or "").strip()
    api_key = (os.environ.get("AIDIFF_LIVE_API_KEY") or "").strip()
    if base_url and api_key:
        return base_url, api_key, os.environ.get("AIDIFF_LIVE_MODEL", "deepseek-v4-flash")

    discovered = _endpoint_from_codex_config()
    if discovered is None:
        return None
    origin, token = discovered
    # 代理的 OpenAI 兼容根路径就是 /v1（实测 /v1/models 与 /v1/chat/completions 都在）。
    return f"{origin}/v1", token, os.environ.get("AIDIFF_LIVE_MODEL", "deepseek-v4-flash")


def _is_reachable(base_url: str, timeout: float = 0.6) -> bool:
    parsed = urllib.parse.urlsplit(base_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_SETTINGS = _live_settings()
_REACHABLE = bool(_SETTINGS) and _is_reachable(_SETTINGS[0])

_SKIP_REASON = (
    "本地 AI 代理不可达（未发现 ~/.codex/hooks.json 里的端点，或端口没监听）。"
    "用 AIDIFF_LIVE_BASE_URL / AIDIFF_LIVE_API_KEY 可以指向别的端点。"
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _REACHABLE, reason=_SKIP_REASON),
]


@pytest.fixture(scope="module")
def client() -> LLMClient:
    assert _SETTINGS is not None
    base_url, api_key, model = _SETTINGS
    return LLMClient(base_url=base_url, api_key=api_key, model=model, timeout_seconds=180)


@pytest.fixture(scope="module")
def model_name() -> str:
    assert _SETTINGS is not None
    return _SETTINGS[2]


# ==========================================================================
# 造的变更数据：真问题 + 一个**不该报**的合理设计
# ==========================================================================

# 真问题 1：删除了已经放出的道具 ID（事故清单里权重最高的一类风险）
# 真问题 2：奖励发放顺序反了（先发后扣 → 可以刷奖励）
GOODS_DIFF = """## 文件：config/30_goods/goods.xlsx（道具表 CfgGoods）

工作表 CfgGoods：
- [删除] 行 100012：id=100012, name=新手礼包, quality=3, sell_price=100
- [修改] 行 100088：attack 100 → 120
- [新增] 行 100201：id=100201, name=测试道具, quality=1, sell_price=0

工作表 CfgGoodsSell（商城表）：
- [未变化] 行 5：goods_id=100012, price=680  （注意：这里仍引用 100012）
"""

REWARD_DIFF = """## 文件：scripts/reward_service.lua（奖励发放）

@@ function RewardService:claimReward(roleId, itemId, price, count)
-    self:costToken(roleId, price)        -- 先扣代币
-    if not self:checkToken(roleId, price) then return false end
-    self:addItem(roleId, itemId, count)  -- 再发道具
+    self:addItem(roleId, itemId, count)  -- 先发道具
+    self:costToken(roleId, price)        -- 再扣代币
+    if not self:checkToken(roleId, price) then return false end
"""

# **不该报**的合理设计：阶段性屏蔽 + 灰度开关 + 故意提前 return。
# 内置 skill 的反误报条款明确列了这几种，模型必须认出它们不是缺陷。
ACTIVITY_DIFF = """## 文件：scripts/activity_service.lua（活动服务）

@@ function ActivityService:enter(roleId)
+    -- 阶段性屏蔽：暑期活动尚未上线，先整体关闭入口（策划确认，上线前会打开）
+    if not FEATURE_FLAGS.summer_event_enabled then
+        return false
+    end
+    -- 灰度：仅对白名单玩家开放，用于联调保护
+    if not GRAY_RELEASE:isWhiteList(roleId) then
+        return false
+    end
     return self:doEnter(roleId)
"""

FAKE_DIFFS = {
    GOODS_TABLE: GOODS_DIFF,
    REWARD_SCRIPT: REWARD_DIFF,
    ACTIVITY_SCRIPT: ACTIVITY_DIFF,
}

# 断言用的关键词。「删除了已放出的 ID」与「先给后扣」是本平台最该抓到的两类。
DELETED_ID_HINTS = ("100012", "删除")
ORDER_HINTS = ("顺序", "先发", "先给", "后扣", "刷")
FALSE_POSITIVE_HINTS = ("summer_event", "阶段性屏蔽", "灰度", "白名单", "FEATURE_FLAGS")


class ScriptedProvider:
    """按脚本提供上下文。真实 provider 还没接上，但接口形态与之一致。

    额外记账：把「模型要了什么」记下来，供断言用。
    """

    def __init__(self, skills):
        self.skills = skills
        self.requests: list[tuple] = []

    def commit_detail(self, commit):
        self.requests.append(("commit_detail", commit))
        return (
            f"提交 {commit}\n"
            "作者：qa\n"
            "信息：暑期活动预热 —— 道具表调整 + 奖励发放重构\n"
            "改动文件：\n"
            + "\n".join(f"  - [M] {path}" for path in CHANGED_PATHS)
        )

    def file_diff(self, commit, path):
        self.requests.append(("file_diff", commit, path))
        return FAKE_DIFFS.get(path, f"（{path} 无差异）")

    def file_content(self, commit, path, lines=""):
        self.requests.append(("file_content", commit, path))
        return FAKE_DIFFS.get(path, f"（{path} 的完整内容略）")

    def find_references(self, query, path=""):
        self.requests.append(("find_references", query))
        return f"{query} 命中 1 处：a.lua:12"

    def read_reference(self, name):
        self.requests.append(("read_reference", name))
        for document in (
            *self.skills.platform_references,
            *self.skills.project_documents,
        ):
            if document.name == name:
                return document.text
        return None


def _scope() -> AnalysisScope:
    return AnalysisScope.from_iterables(
        commits=(COMMIT,),
        paths_by_commit={COMMIT: CHANGED_PATHS},
        readable_references=tuple(
            list(_load().readable.keys())
        ),
    )


_SKILLS_CACHE = {}


def _load():
    if "skills" not in _SKILLS_CACHE:
        _SKILLS_CACHE["skills"] = load_skills(REPO_ROOT, project_code="G119")
    return _SKILLS_CACHE["skills"]


@pytest.fixture(scope="module")
def skills():
    return _load()


# ==========================================================================
# 手写编排循环（engine.py 的临时替身）
# ==========================================================================


class LoopOutcome:
    def __init__(self):
        self.rounds = 0
        self.payload = None
        self.normalized = None
        self.grounded = None
        self.raw_responses: list[str] = []
        self.protocol_errors: list[str] = []
        self.items = []
        self.tokens_input = 0
        self.tokens_output = 0

    @property
    def reported_titles(self) -> list[str]:
        return [anomaly.title for anomaly in (self.normalized.anomalies if self.normalized else ())]

    def summary(self) -> str:
        lines = [
            f"轮次：{self.rounds}",
            f"工具调用：{len(self.items)} 条上下文",
            f"token：in={self.tokens_input} out={self.tokens_output}",
            f"协议错误：{self.protocol_errors or '无'}",
        ]
        if self.normalized is not None:
            lines.append(f"报出异常 {len(self.normalized.anomalies)} 条：")
            for anomaly in self.normalized.anomalies:
                lines.append(
                    f"  [{anomaly.severity}/{anomaly.confidence}] {anomaly.title}"
                    f" @ {anomaly.file_path or '-'}"
                )
            if self.normalized.dropped:
                lines.append(f"（丢弃/合并 {len(self.normalized.dropped)} 条）")
        return "\n".join(lines)


def _run_analysis_loop(client: LLMClient, skills, *, max_rounds: int = 4) -> LoopOutcome:
    outcome = LoopOutcome()
    scope = _scope()
    provider = ScriptedProvider(skills)

    change_summary = render_change_summary(
        [
            CommitSummary(
                commit=COMMIT,
                message="暑期活动预热 —— 道具表调整 + 奖励发放重构",
                author="qa",
                commit_time="2026-09-16T10:00:00",
                files=tuple(
                    FileChange(path=path, operation="M") for path in CHANGED_PATHS
                ),
            )
        ]
    )
    system_prompt = build_system_prompt(
        skills, project_knowledge="", project_instructions=""
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

    context_items: list = []
    escalation_notes: list[str] = []
    correction_hint = ""

    for round_index in range(1, max_rounds + 1):
        outcome.rounds = round_index
        messages.append(
            {
                "role": "user",
                "content": build_user_message(
                    change_summary=change_summary,
                    round_index=round_index,
                    max_rounds=max_rounds,
                    items=context_items,
                    budget_notes=escalation_notes,
                    requests_remaining=max(0, 12 - len(context_items)),
                    correction_hint=correction_hint,
                ),
            }
        )
        result = client.complete(messages)
        outcome.tokens_input += result.prompt_tokens
        outcome.tokens_output += result.completion_tokens
        outcome.raw_responses.append(result.text)
        messages.append({"role": "assistant", "content": result.text})

        try:
            payload = parse_payload(result.text)
        except ProtocolError as exc:
            outcome.protocol_errors.append(str(exc))
            correction_hint = (
                f"你上一轮的返回不符合协议：{exc}。请只返回一个 JSON 对象，"
                "不要输出 JSON 之外的任何文字。"
            )
            continue

        correction_hint = ""
        if payload.status == STATUS_NEED_MORE_CONTEXT and not payload.is_final:
            allowed, dropped = sanitize_requests(payload.requests, scope)
            if not allowed:
                escalation_notes.append(
                    f"你上一轮索要的上下文都不在允许范围内（{len(dropped)} 条被拒），"
                    "请只索要本次提交改动过的文件。"
                )
                continue
            tools = ContextTools(provider, max_tool_requests=12)
            batch = tools.execute(allowed)
            context_items.extend(batch.items)
            # 执行层的记账在 `batch.dropped` 里（被拒的请求、执行失败），压缩层的
            # 记账在每条的 `meta` 上（已由 `render_context_items` 渲染给模型）。
            # 两者分工不同，这里把执行层的原因转成给模型看的一句话。
            escalation_notes.extend(
                f"{record.reason}：{record.detail}" if record.detail else record.reason
                for record in batch.dropped
            )
            continue

        outcome.payload = payload
        outcome.grounded = ground_payload(payload, scope)
        outcome.normalized = normalize_anomalies(
            outcome.grounded.anomalies, RuleThresholds()
        )
        outcome.items = context_items
        return outcome

    outcome.items = context_items
    return outcome


@pytest.fixture(scope="module")
def analysis(client, skills) -> LoopOutcome:
    return _run_analysis_loop(client, skills)


# ==========================================================================
# 1) 端点形态
# ==========================================================================


def test_model_list_endpoint_is_openai_shaped(client):
    """`/models` 的返回形态。Step 3.4 的「获取可用模型」按钮就靠它。"""
    models = client.list_models()

    assert models, "端点返回了空的模型列表"
    assert all(isinstance(item, str) and item for item in models)


def test_a_completion_round_trips(client, model_name):
    result = client.complete(
        [{"role": "user", "content": "只回复两个字：收到"}], temperature=0
    )

    assert "收到" in result.text
    assert result.model
    assert result.prompt_tokens > 0
    assert result.completion_tokens > 0


def test_the_configured_model_works_even_if_the_list_omits_it(client, model_name):
    """**实测到的一条真事实**：能用的模型名不一定出现在 `/models` 里。

    本机代理上 `deepseek-v4-flash` 可以正常对话，但 84 条模型列表里**没有**它
    （列表里有 `deepseek-flash` / `deepseek-v4-flash-inhouse-yd`，就是没有这一个）。

    这条用例把该事实钉住：模型输入框必须是**可自由填写的文本**，`/models` 的结果只
    作为建议（datalist）。若做成只能从列表里选的 `<select>`，用户反而选不到自己
    在用的模型 —— 这正是 Step 3.4 里那个决定的原因。
    """
    try:
        models = client.list_models()
    except Exception:  # noqa: BLE001 —— 列表取不到不影响本用例的结论
        models = []

    result = client.complete(
        [{"role": "user", "content": "只回复两个字：收到"}], temperature=0
    )
    assert result.text, "模型名可用但对话失败，说明结论不对"

    if models and model_name not in models:
        pytest.skip(
            f"本机代理的模型列表里确实没有 {model_name}（共 {len(models)} 条），"
            "但它可以正常对话 —— 模型列表只是建议，不能当权威。"
        )


# ==========================================================================
# 2) 协议与结构
# ==========================================================================


def test_the_run_reaches_a_final_payload(analysis):
    assert analysis.payload is not None, (
        f"没有收敛到 final。协议错误：{analysis.protocol_errors}\n"
        f"最后一轮原文：{analysis.raw_responses[-1][:800] if analysis.raw_responses else '（无）'}"
    )
    assert analysis.payload.is_final


def test_the_model_asked_for_a_specific_file_before_concluding(analysis):
    """多轮按需索取是核心机制。模型若一步到位给结论，说明它没按协议先取证。"""
    assert analysis.rounds >= 2, (
        "只跑了一轮 —— 首轮就给了 final，说明没有先索取 diff。"
        f"报告开头：{analysis.raw_responses[0][:400] if analysis.raw_responses else ''}"
    )


def test_every_dimension_is_reviewed(analysis):
    """`dimensions` 是「每个维度都过了一遍」的证据，空着等于允许只挑好说的说。"""
    from services.ai.skill_contract import DIMENSION_IDS

    reviewed = {item.id for item in analysis.payload.dimensions}
    assert reviewed == set(DIMENSION_IDS), f"漏掉的维度：{set(DIMENSION_IDS) - reviewed}"


def test_the_report_has_the_agreed_sections(analysis):
    """固定章节同时也是「回答像不像一份报告」的健康检查依据。"""
    from services.ai.skill_contract import REPORT_SECTIONS

    report = analysis.payload.report_markdown
    hits = [section for section in REPORT_SECTIONS if f"# {section}" in report]
    assert len(hits) >= 5, f"报告只命中了 {hits}，实际内容：{report[:600]}"


def test_every_reported_anomaly_carries_evidence(analysis):
    """没有证据的断言无法跟进，也是空泛表述的主要来源。"""
    for anomaly in analysis.normalized.anomalies:
        assert anomaly.evidence, f"{anomaly.title} 没有证据"
        assert all(item.strip() for item in anomaly.evidence)


def test_reported_anchors_are_grounded_in_the_batch(analysis):
    """接地校验：模型报出的 commit / 文件必须真的在本次变更里。"""
    for anomaly in analysis.grounded.anomalies:
        assert anomaly.commit == COMMIT
        if anomaly.file_path:
            assert anomaly.file_path in CHANGED_PATHS


# ==========================================================================
# 3) 双向检查：该报的要报，不该报的不许报
# ==========================================================================


def test_the_deleted_released_id_is_reported(analysis):
    """种进去的真问题 1：删除了已放出的道具 ID 100012（商城表仍在引用它）。

    这是事故清单里权重最高的一类风险，报不出来说明提示词或 skill 没起作用。
    """
    blob = " ".join(
        f"{anomaly.title} {' '.join(anomaly.evidence)} {anomaly.impact}"
        for anomaly in analysis.normalized.anomalies
    )
    assert any(hint in blob for hint in DELETED_ID_HINTS), (
        "没有报出「删除已放出的道具 ID 100012」这类风险。\n"
        f"实际报出：{analysis.reported_titles}\n\n{analysis.summary()}"
    )


def test_the_reward_ordering_bug_is_reported(analysis):
    """种进去的真问题 2：奖励发放变成「先发后扣」，可以刷奖励。"""
    blob = " ".join(
        f"{anomaly.title} {' '.join(anomaly.evidence)} {anomaly.impact} {anomaly.suggestion}"
        for anomaly in analysis.normalized.anomalies
    )
    assert any(hint in blob for hint in ORDER_HINTS), (
        "没有报出「奖励发放顺序反了（先发后扣）」这类风险。\n"
        f"实际报出：{analysis.reported_titles}\n\n{analysis.summary()}"
    )


def test_the_staged_and_gray_release_pattern_is_not_reported(analysis):
    """**反误报：这条是本文件最重要的一条。**

    `activity_service.lua` 里的改动是阶段性屏蔽 + 灰度白名单 + 故意提前 return ——
    内置 skill 的反误报条款明确把它们列为「看起来像 bug 的合理设计」。把它们报出来
    就是误报，而误报的代价是没人再信任这份清单。

    只所以断言「不报」而不是「报了也无所谓」：本功能的第一目标是精度优先。这条用例
    红掉，说明提示词正在滑向「宁可多报」。
    """
    offending = [
        anomaly
        for anomaly in analysis.normalized.anomalies
        if anomaly.file_path == ACTIVITY_SCRIPT
        or any(hint in f"{anomaly.title} {' '.join(anomaly.evidence)}" for hint in FALSE_POSITIVE_HINTS)
    ]
    assert not offending, (
        "把「阶段性屏蔽 / 灰度白名单 / 故意提前 return」当成了缺陷上报（误报）：\n"
        + "\n".join(f"  - [{a.severity}] {a.title} :: {' '.join(a.evidence)}" for a in offending)
        + f"\n\n完整结果：\n{analysis.summary()}"
    )


def test_the_run_is_readable_as_a_scorecard(analysis):
    """把结果打出来。这条不判对错，只为让人**看见**这次分析到底产出了什么 ——
    「AI 分析有没有用」最终要靠人看这份输出下判断。"""
    print("\n" + "=" * 72)
    print("AI 分析实测结果（真实模型）")
    print("=" * 72)
    print(analysis.summary())
    print("-" * 72)
    print("报告正文（前 1200 字）：")
    print((analysis.payload.report_markdown if analysis.payload else "")[:1200])
    print("=" * 72)


# ==========================================================================
# 4) 提示词缓存：第 2 轮及以后必须命中上行缓存
# ==========================================================================


def test_every_round_after_the_first_hits_the_provider_prefix_cache(client, skills):
    """**对着真模型验收 prompt cache。**

    多轮循环的全部意义建立在一条性质上：第 N+1 轮的请求是第 N 轮的 **append-extension**
    （逐字节前缀相同）。上游按前缀匹配缓存，所以只要这条性质成立、且这个端点做前缀缓存，
    第 2 轮及以后就必须报出**非零**的命中 token。

    这条断言是 deepseek-harness 的 `request-cache.e2e.ts` 用的同一把尺子
    （那边写的是「每一次请求 `cacheReadTokens > 0`」）。假 client 能证明「我们按设计
    发了消息」，**证不了「上游真的命中了」** —— 上游的缓存粒度（DeepSeek 侧实测是
    64 token 一块）、最小块数、TTL 都会影响结果，那些只能对着真端点看。

    端点上没上报缓存字段时 skip（那说明这个网关不暴露缓存用量，而不是我们发错了）。
    """
    provider = ScriptedProvider(skills)
    rounds: list[RoundProgress] = []
    change_summary = render_change_summary(
        [
            CommitSummary(
                commit=COMMIT,
                message="暑期活动预热 —— 道具表调整 + 奖励发放重构",
                author="qa",
                commit_time="2026-09-16T10:00:00",
                files=tuple(FileChange(path=path, operation="M") for path in CHANGED_PATHS),
            )
        ]
    )

    outcome = run_analysis(
        client=client,
        provider=provider,
        loaded=skills,
        scope=_scope(),
        change_summary=change_summary,
        # 真模型上轮次越少越好：这条用例要的是「两轮之间的前缀」，不是完整报告。
        limits=EngineLimits(max_rounds=3, max_tool_requests=4),
        on_round=rounds.append,
    )

    assert outcome.rounds_used >= 2, (
        "只跑了一轮，没有「第二轮」可验：这条用例需要模型先索取一次上下文。"
        f"报告开头：{(outcome.report_markdown or '')[:300]}"
    )
    reported = [item for item in outcome.rounds if item.cache_read_tokens is not None]
    if not reported:
        pytest.skip(
            "该端点没有上报任何缓存字段（cache_read_tokens 全为 None），"
            "无法验收缓存命中。这不代表我们发错了请求。"
        )

    first, *later = outcome.rounds
    # 逐轮记账本身也要对得上（回调与 RoundRecord 是两份数据，别只信一个）。
    assert [item.index for item in rounds] == [item.index for item in outcome.rounds]
    for record in later:
        assert record.cache_read_tokens is not None, (
            f"第 {record.index} 轮上游没报缓存字段，而第 1 轮报了 —— "
            "上游要么一致性有问题，要么我们中途换了请求形态"
        )
        assert record.cache_read_tokens > 0, (
            f"第 {record.index} 轮一个缓存 token 都没命中：说明这一轮的请求不是上一轮的前缀。"
            f"逐轮命中数：{[item.cache_read_tokens for item in outcome.rounds]}"
        )
    print(
        "\n逐轮 prompt token / 命中："
        + "；".join(
            f"第 {item.index} 轮 {item.prompt_tokens} / {item.cache_read_tokens}"
            for item in outcome.rounds
        )
    )
