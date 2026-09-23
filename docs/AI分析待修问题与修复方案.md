# AI 分析待修问题与修复方案（2026-09-23）

本文记录本轮排查出的问题，以及**准备怎么修**。每条都分开写「实测到的」与
「还没证实的」——本仓吃过「报告自洽但结论是编的」的亏，请按这个分栏来读。

**§三 的修复已落地**（见该节，含变异验证）；**§一、§二 还没有动任何产品代码**，
探针与产物都在 `%TEMP%` 下。

排序按「该不该现在动」：一、二是同一处的两个面，建议一起动；**三、四都已定案**
（三已修，四的修法明确）；**只有一、二还在等你拍板**。

---

## 一、模型走错输出通道，整轮被判「返回无法解析」

### 现象

用户报的那一行：

```
分片 S2 · 第 1/15 轮 · 返回无法解析 · 输入 14.7k tokens · 2.3 s
```

回答里没有可解析的 JSON，原文是一段**带全角竖线分隔的工具调用信封**，形如
`<|DSML|> call … <|DSML|> invoke name="think" …`（`invoke` 名是 `think`，
正文是一段「分诊」推理）。

### 实测到的机理（逐处到 文件:行）

1. **模型返回**：`llm_client.py:266 _extract_message_text` 只读
   `choices[0].message.content`。响应里**没有 `tool_calls` 字段可用**。
2. **解析**：`engine.py:1199` → `protocol.py:306 parse_json_candidates`，6 类候选逐个
   `json.loads` 全失败。其中唯一那个「剥推理块」的候选是 `protocol.py:125`
   `_THINK_BLOCK_RE = re.compile(r"<think\b.*?</think\s*>", re.S | re.I)` ——
   **只认半角 `<think>`**，对上面那段一个字都不动。
3. **逐条兜底全部落空**（探针实测为否）：`salvage_report_markdown` → `None`；
   `looks_like_truncated_json` → `False`；两个 `repair_*` → `None`；
   `looks_like_markdown_report`（数报告章节标题，阈值 2）→ `False`。
4. **抛错**：`protocol.py:695`
   `raise ProtocolError("回答里没有可解析的 JSON。原文开头：…")`。
5. **「返回无法解析」这五个字是前端词表**，不是后端打的：`static/js/ai_think_log.js:88`
   `unparsable: '返回无法解析'`，那一行在 `:220-227` 拼装。

### 这一轮的代价（实测）

| 项 | 结论 | 依据 |
|---|---|---|
| 活白干了吗 | **没有** | S2 第 2 轮 `outcome=requests` / `parsed_ok=true`，正常索取到 3 条 `file_diff` + 1 条 `file_content` |
| 花钱 | ≈ ¥0.008（14.7k 输入里 13.8k 是缓存读） | 单价表 |
| 花轮次 | 1 轮（从该片 `max_rounds` 扣） | `engine.py:792` 的循环，重试走同一循环 |
| 花纠正额度 | **1 次** | `engine.py:1288` |
| 后续轮次 | 还在，S2 跑到第 5 轮、S4 第 4 轮都正常 | 同一份进度快照 |

### 现有的兜底（实测触发过，不是推断）

`engine.py:1286-1300` 的 `else` 分支会带一段纠正提示重问，提示由
`protocol.py:1355 build_correction_hint` 生成。这一支**真的触发过**：
`ai_analysis_job.id=17` 的进度快照里，S2 第 1 轮条目带着逐字的 `correction_hint`
（内容就是「你上一轮的返回不符合协议…请只返回一个可被 json.loads 解析的 JSON 对象…」）。
重试后第 2 轮恢复正常。

另有三处独立佐证：把原文喂进真引擎跑一遍 → 2 次调用、`succeeded`；
`run 38` 的 `V1` 第 1 轮同形态同样恢复；`ai_analysis_trace` 表有 `correction_hint` 列，
API 与用量面板都会渲染它。

**用户当时读的「思考过程」抽屉不渲染 `correction_hint`**（该 JS 里没有这个字段），
所以在那个面板上，「返回无法解析」看起来就是**一个没有下文的失败**。

### 风险：纠正额度耗尽会整片阵亡

`max_corrections` 默认 **2**（`engine.py:185`），**每个分片各自独立、不可配、不进共享池**
（`subagent.py:539-541` 只覆盖了 requests/rounds；`ai_analysis_service.py:655` 与
`auto_sizing.py` 都没有这个字段）。

用完时走 `engine.py:1279-1285`：

```python
elif limits.max_corrections <= 0:
    _emit(RoundRecord(round_index, "unparsable", note=…))
    degradation = DEGRADE_PROTOCOL
    break
```

**实测（真引擎 + 真原文 + 只改额度）**：`max_corrections=2` → 重问 1 次 → `succeeded`；
**`max_corrections=0` → 1 次调用后 `failed` / `protocol_corrections_exhausted`，整片 1 轮就死。**

留痕是有的（分片整片跑不成时 `family_ledger._shard_never_ran` 会置 `subagent_gap`，
报告末尾逐条列「信息缺口（平台补充）」），但**两处容易读漏**：
`DEGRADE_PROTOCOL` 的标签（「连续多轮无法解析出协议要求的 JSON」）把「模型不肯写 JSON」
与「这一片整个没交回结论」共用一句话，看不出后者的后果。

> ⚠️ **未证实**：生产里到底有多少分片真的烧完过 2 次额度再撞上这个形态，**没有统计**。
> run 44 的 trace 还没落库（跑完才批量写），历史里的 `unparsable` 多是别的形态。
> **这是风险推演，不是已发生的事故。**

### 结构性成因：平台根本不发工具定义

**实测**：请求体的唯一构造点 `llm_client.py:600-626` 只往 body 里放 `model` / `messages`，
条件加 `temperature` / `stream` / `max_tokens` —— **没有任何一处加 `tools` 或 `tool_choice`**。
全仓 grep `tool_choice` 在 `services/` 下零命中；`usage.py` 里那些 `tools` 是平台**自己**的
用量统计键，不是 API 字段。

而 `SKILL.md` 正文有 **6 处**把平台的请求类型（`file_diff` / `file_content` /
`read_reference` / `find_references` / `evidence` / `commit_detail`）称作「**工具**」，
却没有一句说「平台不向你下发任何工具定义，你没有任何可调用的工具；索取上下文只有一种
方式 —— 在 JSON 里写 `requests`」。

S4 那条信封里模型**自造了一个工具名**（`get_file_diff`），而参数体是平台协议 JSON
一字不差 —— 这个错位的直接显形。

> ⚠️ **未证实**：那段标记是不是 DeepSeek 的原生格式，本环境**没能核实**（检索两次内部报错）。
> 上面的结论**不依赖**这一条，只依赖「平台不发 tools」。
> ⚠️ **未切开**：请求经本机网关 `127.0.0.1:15721` 转发，「是模型吐的、还是网关把上游工具
> 调用序列化进了 content」**无法判定**。要切开需要往上游发一次**带 `tools` 的对照请求**——
> 那是出网调用，**没有做，等你决定**。

### 准备怎么修

**先破一个看起来最直接的方案：单纯「剥掉标记再解析」是无效的。**
两段真实原文剥掉整块信封后**都变成空串**（S2 那条整个信封里只有一个 `think`，没有任何请求；
run 38 V1 那条只有一条地址）。**壳里没有 JSON 可剥。**

**主线：协议层「识别到工具调用信封 → 就地抽取请求」**，挂在**已有的**修复循环里当第三项。

本仓已经走过三次同一副骨架（`engine.py:1204-1211` 的
「续写块拼接」/「正文裸引号转义」），加第三项是**扩展，不是新机制**：

1. `protocol.py` 新增 `repair_dsml_tool_calls_payload(text) -> str | None`。
   **判据保守**：只吃两种形态 —— ① `invoke` 体本身是协议请求 JSON（`{"type": …}`）；
   ② 空体但 `invoke` 属性带 20 位十六进制地址（`evidence` 是唯一「地址在属性里」的类型）。
   **抽不到就返回 `None`，绝不伪造 `final`，绝不回一个空 `requests` 的 `need_more_context`**
   （那会撞「`need_more_context` 必须给出非空 requests」，反而把纠正提示搞得更差）。
2. `engine.py:1204-1211` 的元组加第三项。
3. 剥壳产物**照走** `sanitize_requests` 与 `ground_payload` —— 越权 commit/路径照样被丢、
   照样记账，**安全边界一点没松**。
4. 必须守 `engine.py:1233` 那条纪律：这条修复路径**不许再 `_emit` 第二份 RoundRecord**
   （同一轮发两份会撞 `uq_ai_trace_run_round`），要落回循环体尾部的常规处理。

**实测收益**（端到端 `parse → sanitize` 跑过）：

| 样本 | 抽出请求 | 结果 |
|---|---|---|
| run 44 **S4** #1 | 4 条（3 `file_diff` + 1 `evidence`） | `parse_payload` 通过；`sanitize_requests` 放行 3 条、丢 1 条（地址位数不合形状） |
| run 38 **V1** #1 | 1 条（`evidence`） | `parse_payload` 通过 |
| run 44 **S2** #1 | 0 条（纯 `think`） | 返回 `None` → 走今天的重问，**行为逐字节不变** |

即：**有请求的那种整轮救回（省 1 轮 + 1 次纠正额度）；纯思考那种救不了，仍靠重试。**

**配套小改：这类形态的纠正提示别再抄原文。** `build_correction_hint` 现在会把模型的
那段信封**原样抄一遍**发回去 —— 对这种病，抄回去等于又示范了一遍。改成一句正面事实。

**提示词层要不要改：要，但不作主线。** 输出契约在 `SKILL.md:61` 已经写得很硬
（「你**只能**输出一个可被 `json.loads` 直接解析的 JSON 对象」），模型照旧没走；
再加一句更硬的话边际效力很低，而且**离线验证不了**。要改的是那处**事实缺失**
（把请求类型叫「工具」却没说没有工具），且必须写成**正向事实** ——
**写「禁止 X」会把 X 的字面量送进上下文**：`protocol.py:1388` 现在就写着「不要输出
`<think>` 块」，而 `think` 恰恰是出现过的 `invoke` 名。

### 怎么验证

1. **先写测试**：样本用 S2、S4 两段**逐字原文**（S4 必须从 `raise` 变成解析通过 +
   `sanitize_requests` 放行 3 条；S2 必须仍返回 `None`），再加两条反向样本
   （普通 JSON、普通 markdown **不许**被误吃）。
2. **变异验证**（本仓硬要求）：把新增的那一项注释掉 → 测试必须变红；
   只留 S2 样本 → 必须仍绿（证明它真的走旧路）。还原**只用副本二进制写回并校验 sha1**。
3. 上线只看一个数：这类形态的 `unparsable` 轮里，有多少变成了
   `round_notes` 里的「已自动修复，未重发」。

---

## 二、纠正额度「所有协议错误共用 2 次」

见 §一 的「风险」一节。`max_corrections` 是**所有协议错误共用**的一个池子，
一个已经烧掉 2 次的分片（例如前面撞过两次输出截断）再遇一次这种信封就整片阵亡。

**准备怎么修（未定，等你拍板）**：给「模型没走输出通道」这一类一条**独立小额度**，
或让纠正额度**只在模型给出不同失败时才扣**。

**必须守住的护栏**：`engine.py:1287` 那句注释说得对 ——
「重问也要占一轮：否则一个不肯说 JSON 的模型能把循环变成无限次重试」。
新额度**仍受循环上界约束**，否则就是把一个降级路径改成无限重试。

---

## 三、思考面板：重开抽屉不刷新 + 分片排序（已修复）

用户报的两条：

> 关掉抽屉再重新打开，如果这时 AI 在分析中，抽屉**不会马上刷新**分片思考信息，
> 只有**新**分片思考内容出来了才会刷新；其次，分片信息的**排序**没有按预期规则，现在是乱序。

### 一）重开抽屉不刷新：根因与用户那句描述逐字吻合

复现用的是**当时真在跑的那次分析**（run 44 / job 17 / config 3），真浏览器
（Playwright + 系统 Chrome），全程只发只读 GET。逐拍取值：

| 拍 | `weeklyAiRunId` | `aiBudgetWatch` | `data.progress` | 卡片数 | 抽屉标签 |
|---|---|---|---|---|---|
| 开抽屉后 | 44 | object | `rounds=8` | 8 | think |
| 关抽屉后 | 44 | **null** | — | 8（留着） | report |
| 重开（同步那一刻） | 44 | null | — | 0（被 `setRun(null)` 清掉） | report |
| 重开 +400ms | 44 | **null** | **从未调用** | **0** | report |
| 重开 +2s / +6s | 44 | null | — | 0 | report |

**对照组**：同一台机器、同一次运行，URL **不带** `?ai_job=` 时重开是**好的**
（+300ms 就有 8 张卡）—— 因为那条走 `refreshWeeklyAiLatest`，不经过
`resumeWeeklyAiJob`。**这就是「读代码觉得应该有、实际没有」的原因。**

机理链（**我独立核过每一跳**，不是转述）：

1. `subscribeWeeklyAiJob` 的**第一句**就是 `closeWeeklyAiStream()`
   （`merged_project_view.html:5577`，逐字未动）；
2. `closeWeeklyAiStream` → `stopAiBudgetWatch()`（同文件 `:4564`）；
3. `stopAiBudgetWatch` → `AiStreamStatus.watchRun` 的 `stop()`，把 `stopped` 置真；
4. 而**第一帧是 `watchRun` 在返回之前就发出去的**，它的响应回来时走
   `if (stopped || !data || !data.success) return data || null;`
   —— 这一句在 `opts.onProgress(data)` **之前**（`ai_stream_status.js` 的 `.then`）
   → **`onProgress` 一次都没被调用**；
5. 面板于是停在 `unwatch()` 留下的终态说法上，抽屉也被切回「完整结论」；
6. 之后**唯一**还会重画那个面板的是 SSE 每跑完一轮推来的 `progress` 帧 ——
   **逐字就是用户那句「只有新分片思考内容出来了才会刷新」。**

原先的顺序是「先起轮询、后订阅」，而订阅自己会**把刚起来的这次轮询当场掐掉**。

**修法（落到一条规则上）**：谁掐掉轮询，谁就得保证之后有人把它接回来 ——
把 `startAiBudgetWatch` 挪到 `subscribeWeeklyAiJob` **之后**，与「点开跑」那条路一致
（那里也是先订阅、等 SSE 的 `run` 帧才起轮询）。顺带对齐一处同族缺陷：这条路
（重开抽屉 / 带 `?ai_job=` 刷新附着）本页**不是发起方**，原先却没撤「才刚发起」那个时刻
—— 加了 `AiThinkLog.markExternalRun()`，与 `/latest` 那条路同一口径。

### 二）分片排序

修复前真浏览器渲染出来的卡片头，编号列是 `8,1,2,3,4,1,2,3`：

```
分片 S5 · 第 8/23 轮 · 给出结论
第 1/23 轮 · 索取上下文      ← 一个字的前缀都没有
第 2/23 轮 · 索取上下文
第 3/23 轮 · 索取上下文
第 4/23 轮 · 给出结论
分片 V1 · 第 1/23 轮 · 索取上下文
…
```

四条机理：① 实时载荷**从来不家族带位次**（`agent_index`/`agent_total` 一个字节都没进
`rounds[i]`）；② `shardText` 按**空名字早退**，而汇总那几轮 `agent` 就是空的 → 整句前缀为空；
③ 渲染端**不排序**，顺序完全由 payload 决定；④ 8 轮窗口**静默截断**，`rounds_truncated`
服务端发了、渲染端没读 → 列表看起来「从第 3 轮开始」。

修法：`run_progress` 补家族位次与成员内轮次（并加 `_as_int`，脏值只让这一个字段缺失、
不让整帧丢掉）；前端加 `familyOrder` / `roundPosition` / `truncationNote`，
**位次不全时一律不排**（落库那份没有位次，硬排写不出合法比较器）。

> ⚠️ **我先前的一个推断被否证了。** 我猜「同一轮出现两遍」是乱序的成因 ——
> **124 帧真实采样里 0 次重复、0 次回跳**。判重「只看末尾」那个洞是真的
> （已按防御性修掉并补了测试），但它**不是这次的成因**。用户那句「乱序」，
> 按真实渲染读出来是「编号列非单调 + 中间三张卡没有归属」——
> 这一条是**推断**，用户原话只有「乱序」两个字。

### 三）一处已知遗留（有意没做，不是漏）

**落库那一份的汇总轮仍然没有分片前缀。** 原因是结构性的：`AiAnalysisTrace` 表
**没有** `agent_index` / `agent_total` 列（实测 `models/ai_analysis/trace.py:45-51`
只有 `agent` / `agent_round`），实时那一路的位次来自**内存里的进度对象**、根本不落库。
要做到两边一致，得**改表**或者**在读侧推导**（按 `agent` 首次出现的 `round_index` 排位次）
—— 那是另一个决定，这轮没做。**它不影响顺序**（落库那份本来就由
`order_by(round_index asc)` 保证），只是「第 1 轮」前面少一句归属。

### 四）验证

- **我自己跑的**：`test_ai_drawer_resume_watch.py` + `test_ai_think_log_frontend.py`
  + `test_ai_live_thinking_snapshot.py` → **83 passed**。
- **我独立核过的**：上面那条机理链的每一跳（`5577 → 4564 → stop() → .then 里
  `stopped` 在 `onProgress` 之前` ）、`markExternalRun` 确实存在且已导出、
  `rounds_seen` / `rounds_truncated` 的键名与前端读的一致、`_as_int` 的落点。
- **变异验证**（交由执行方做的，脚本在 `%TEMP%`，副本二进制写回 + sha1 校验）：
  7 项变异**全部由绿变红**（顺序换回、删 `markExternalRun`、不排序、空 `agent` 早退、
  不挂截断提示、判重只看末尾、不补位次），还原后 sha1 与变异前一致。
- **那一次 live 测试的红**（`test_the_reward_ordering_bug_is_reported`）：它带
  `pytest.mark.live`、跑的是**真实模型**，断言的是模型报告正文里有没有提到种进去的问题 ——
  与本次改动（进度快照 + 前端显示）无因果关系，且与本仓既有记录
  「AI 实测用例会偶发红」一致。**没有重跑它**（避免再花钱）。

---

## 四、偶发测试失败：断言被别的用例「喂」出了数字（已定案）

### 现象与复现

`tests/test_ai_weekly_sync_gate.py::TestBothEntrypoints` 里两个用例**偶发**红
（单跑、整文件跑都绿）。插桩连跑 8 遍抓到：

```
iter 8: 1 failed, 4160 passed in 52.71s
FAILED ...::test_the_background_entry_skips_without_creating_a_run
E   AssertionError: 跳过了却还是留下了一条 run 记录
E   assert 53 == 0
E     where 53 = ...filter_by(target_id=49).count()
```

### 定案的证据（同一个 worker 的库，全量倒出来对照）

| 事实 | 数据 |
|---|---|
| 断言数到的 | `target_id=49` → **53** 条 |
| 库里 `(target_type='weekly', target_id=49)` 桶 | run id `[80…132]` → **53** 条 |
| 这 53 条的 `project_id` | **全部 == 49**（`target_id` 写的是**项目 id**） |
| 这 53 条的 `target_key` | **全部 == `'group-a'`** |
| 闸门自己那条 run 会长什么样 | `target_key` 形如 `"49\|202603010000\|202603080000\|W1"` |

**两边数字精确相等（53 == 53），而且这 53 条没有一条带闸门那种 `target_key`。**
结论有两层：

1. **闸门是对的。** 它确实在建 run 之前就返回了 —— 没有一条 run 是它写的。
2. **红的是测试，不是被测代码。** 它的 `count()` 把整整 53 条**别人的**运行数了进来。

污染方是**把项目 id 当 `target_id` 存**的那批用例（`target_id` 一列三义：
commit_id / config_id / project_id，见 `models/ai_analysis/analysis_run.py:36`）。
全测试套有 5 个文件这么写：`test_ai_drawer_stream_state.py:1187`、
`test_ai_usage_budget_ui.py:1170`、`test_ai_usage_dual_scope.py:113`、
`test_ai_usage_filters_and_budget.py:146`、`test_ai_usage_statistics.py:111`。
项目 id 与配置 id 走同一个自增空间（本次快照：config `1..50`、project `1..69`），
**撞号是迟早的事**，撞上哪个数字取决于该 worker 当时的水位。

干扰只在**同 worker 内**：每个 xdist worker 有自己独立的库
（`tests/conftest.py` 用 `NamedTemporaryFile(prefix="diff_platform_test_")` 按进程建），
而「谁和谁同 worker」随 `-k` 的收集结果变化 —— 这解释了为什么只在某些 selection 上见到。

这正是 `test-db-is-session-shared` 那条已知形态：**测试库会话级共用、没有逐用例重置，
全局 `count()` 断言会「全量绿、子集红」**。

### ⚠️ 我先前给的两个说法都要收回

1. **污染源指认错了。** 我先前说是 `test_ai_analysis_service.py:917`/`:936` 那两条
   `target_type="commit"`、`target_id=1` 的 run。复现把它证伪了：命中的是
   `target_id=49` 的 **weekly** 型，与那两条无关。（那两条不清理仍是真实的卫生问题，
   但**不是**这次的解释。）
2. **我提的修法是错的。** 我先前说「给断言补 `target_type="weekly"`」——
   **补了也没用**：这 53 条*本身就是* `target_type='weekly'`。少的那一维不是
   `target_type`，而是「这条 run 是不是**本测试**可能写出来的」。

### 一个顺手排掉的嫌疑人

同文件的 `test_the_background_entry_proceeds_when_the_sync_is_done` 走
`run_weekly_analysis_background`，但它在 `services/ai_analysis_service.py:1712`
的 `missing_api_key` 那道闸就返回了 —— **在创建任何 run 之前**。它留不下 run，
也正因为如此，它**不能**当作「happy path 会建 run」的正面佐证（见下面第 2 条）。

### 还有一次不是这个原因（已切开）

同一批连跑里的另一次（189 failed / 271 errors）是**另一回事**，与测试隔离无关：

```
AssertionError: Node 执行失败：
assert 3221225794 == 0   # node driver.js 的 returncode
```

`3221225794` = `0xC0000142` = `STATUS_DLL_INIT_FAILED` —— Windows 在 8 路并发下
**起不来 node 进程**。环境资源问题，不是产品缺陷。**两次复现要分开算数。**

### 同族的第二例（**没复现，只是记一笔**）

跑那批宽回归时还见过**一次**：
`test_ai_run_lifecycle_idempotency.py::test_the_commit_stream_says_already_running_instead_of_replaying_itself`
红（1 failed / 4180 passed）。**单跑绿，随后连跑 3 遍全绿（各 4181 passed）** ——
5 次里红 1 次，**没有 traceback**。

它可疑的地方与本节同族：判据落在 `project_gate.describe_active_analysis(project_id, …)`
这个**项目级**闸门上（`services/ai_analysis_service.py:1541`）—— 共用的库 + id 复用之下，
别的用例留下的活跃运行会让它**第一次调用就被挡下**，于是那句
`assert "run" in [name for name, _ in first]` 直接落空。

> ⚠️ **这条是推断，没有复现。** 记在这里是为了下次再见到能立刻对上号，
> **不是结论**，也**不要**据此去改那条用例。

### 准备怎么修

1. **把断言改成相对的**：调用前先记一次 `count()`，调用后断言**没有增加**。
   这是真正的行为断言（「这次跳过没有新建任何东西」），且对库里已有的行免疫。
2. **补一条正面佐证**（否则第 1 条会退化成假绿）：现在**没有任何一条**用例证明
   「同步跑完时这条路径**真的会**建 run」—— 那个 sibling 用例卡在
   `missing_api_key` 就返回了。缺了它，「没增加」与「这函数压根不建 run」无法区分
   —— 正是 `differential-tests-must-prove-the-branch-is-live` 那类假绿。
   要补的用例：同步已完成 + 打桩的 API key 齐备，断言**确实多了一条** run。
3. **不要**改成按 `target_key` 去筛。`target_key` 的格式是实现细节，它一旦改名，
   筛选会**恒返回 0** —— 断言照样绿，而且这次是静默的假绿。
4. 那 5 个把项目 id 当 `target_id` 的文件是给未来所有 `target_id` 断言埋的雷：
   不要求这次一起改，但值得记一笔。

**验证**：修完要能扛住这次抓到的那个 worker 组合 —— 把污染方与闸门用例放进同一进程跑，
断言必须仍然正确（先红后绿各验一次）。

---

## 附：本轮已修并提交的（不在本文范围）

`AI 分析报告口径`、`3.11 f-string 守卫`、`临时库判据大小写`、`修改行层叠上下文`、
`合并视图未声明全局`、`结论那一轮的错误提示`——各自已成 commit。
