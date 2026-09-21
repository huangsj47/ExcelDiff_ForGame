# 参数逐条：含义、默认值、耦合与代码位置

> 这份文件是给「要动这些数字的人」看的。默认值与取值范围与 `docs/AI分析使用说明.md`
> 第 10 节一致（那一节的受众是管理员，这一份讲的是**它们之间怎么咬合**）。

## 一览

| 配置键（界面标签） | 默认 | 范围 | 代码位置 |
|---|---|---|---|
| `prompt_char_budget`（提示词字符预算） | 560,000 | 10,000 ~ 2,000,000 | `services/ai/budget.py: DEFAULT_TOTAL_CHARS` |
| `max_analysis_rounds`（最大分析轮次） | 8 | 1 ~ 30 | `services/ai/engine.py: EngineLimits.max_rounds` |
| `max_tool_requests`（上下文索取上限） | 40 | 0 ~ 100 | `services/ai/context_tools.py: DEFAULT_MAX_TOOL_REQUESTS` |
| `subagent_count`（子代理数量） | 3 | 1 ~ 6 | `services/ai/subagent.py: DEFAULT_SUBAGENT_COUNT / MAX_SUBAGENTS` |
| 单次异常上限（报告的结论条数上限） | 20 | 0 ~ 200 | 结论落地时的截断；**与「上下文条数上限」是两个旋钮** |
| 取样上限（清单过长时） | 200 | 1 ~ 5,000 | 清单渲染时决定列多少个文件 |
| `max_conclusions` 之类 | —— | —— | 见配置面板 |

## 单条上下文上限（不是配置项，但决定预算算式）

`services/ai/context_tools.py: DEFAULT_TOOL_LIMITS`：

| 工具 | 单条上限（字） |
|---|---|
| `file_diff` | 11,000 |
| `file_content`（配表/代码正文） | 11,000（与取数侧 `utils.content_window.CONTENT_MAX_CHARS` 同值，**必须同值**） |
| `read_reference` | 11,000 |
| `find_references` | 8,000 |
| `commit_detail` | 6,000 |

算式里用的是这些值的**最大值**（11,000）—— 因为预算必须放得下最坏的那一种索取。

## 四条耦合

### 1. 用户预算 ≥ 基线 + 清单 + 知识包 + 索取次数 × 单条上限

```
配置值 ≥ 6,000（历史结论基线）+ 变更清单字数
       + 项目知识包 + 项目补充指令
       + max_tool_requests × 11,000
```

**平台内置提示词不在右边**：它由平台加在左边之上
（`services/ai/prompt.py: platform_prompt_chars` → `ai_analysis_service._engine_limits`
的 `platform_chars` 参数）。**项目知识包与补充指令在右边**：它们在系统提示词里，但仍然是
用户自己要带的内容。

守它的测试：`tests/test_ai_models_and_migration.py::test_the_prompt_budget_can_honor_the_request_budget`
（还断言「内置 + 用户预算 ≤ 默认窗口的水位」）与
`tests/test_ai_budget_vs_model_window.py::test_the_builtin_prompt_does_not_eat_the_users_budget`。

### 2. **上下文**条数上限跟着索取次数走（别与结论条数混了）

`services/ai_analysis_service.py: _engine_limits` 里 `max_items = max(默认 40, max_tool_requests)`。
这是**每一轮能带走多少条上下文**的上限，不是配置项，也不是报告里那个「结论条数上限」
（后者是配置项「单次异常上限」，默认 20，管的是报告里最多列几条异常）。两个数都在
「条数」这个词上，但一个管输入、一个管输出。
理由：条数上限小于索取次数时，会出现「付了 N 次索取、只带走 20 条」——取回来的上下文被
按条数静默裁掉，白花额度。所以索取上限调过 40 之后，条数上限自动跟着走，不需要手工配。

### 3. 预算的上限是窗口水位

`services/ai/budget.py: effective_prompt_budget` / `context_watermark_chars`：
预算会被压到「模型窗口 × 60%」（`COMPACT_AT_RATIO`，留 40% 给回复与估算误差）。
端点声明了窗口就用真的，问不到就按 1M token 的 60% = 600,000 字这个**口径值**处理，
并在说明里写明「按默认值处理」。

**水位压的是「平台内置 + 配置值」这个和**，压完再把内置那段还给平台、剩下的才是用户的
（`_apply_model_window` 的 `platform_chars`）。所以**配置值填得比窗口水位大是无效配置**：
窗口 200k 时用户那部分最终只有约 104,000 字。默认 560,000 + 内置 16,000 = 576,000 仍低于
600,000，所以「问不到窗口」的项目行为不变。

### 4. 分片额度不按分片平分

`services/ai/subagent.py: MEMBER_BUDGET_PERCENT = 100`：每个成员拿到的就是配置值**本身**。
所以：

- 整次分析的索取次数上限 ≈ 分片数 × 配置值 + 汇总那一份（额度是上限，用不掉不花钱）；
- 一次分析的模型调用次数 = **各代理各自跑了几轮之和**（每个代理的每一轮 = 一次调用）；
  代理个数 = 分片数 + 1（汇总）+ 0 或 1（对账轮）；
- `MIN_MEMBER_ROUNDS = 2` / `MIN_MEMBER_TOOL_REQUESTS = 2` 是成员的下限，
  分片数开得越多，每一片的维度越窄、越容易「跑完了但没什么可报」。

## 上下文超长时的三级补救（读预算相关报告时要知道）

`services/ai/engine.py`：上游以「超长」拒绝时，按「丢得越少越先试」补救 ——
① 历史留着、本轮条目按 1/4 额度重压；② 连历史一起丢；③ 换成一段必然装得下的「收尾」
提示词（只带清单开头与已取内容目录）。每一步都先算出「确实压小了」才发。

补救成功之后，**实测到的上限**（刚被拒的那条提示词有多大）会被收进本次运行剩下的轮次：
下一轮按更小的预算组装，不再撞一次。这也是为什么「预算配得比窗口大」的代价不只是
白配 —— 它会让每一次分析都走一遍补救、并以「降级」收尾。
