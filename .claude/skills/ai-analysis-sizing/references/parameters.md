# 参数逐条：含义、默认值、耦合与代码位置

> 这份文件是给「要动这些数字的人」看的。默认值与取值范围与 `docs/AI分析使用说明.md`
> 第 10 节一致（那一节的受众是管理员，这一份讲的是**它们之间怎么咬合**）。
>
> **2026-09-23 配置面收敛之后**：周版本路径的数值参数全部由平台推导（下表「平台推导」
> 一列），用户只配预算与开关。推导的事实源是 `services/ai/auto_sizing.py`，校准出处是
> `measurements.md` 的「runs 38~41」一节。

## 一览

| 配置键（界面标签） | 默认 | 范围 | 平台推导 | 代码位置 |
|---|---|---|---|---|
| `prompt_char_budget`（提示词字符预算） | 560,000 | 10,000 ~ 2,000,000 | **否（唯一容量旋钮）** | `services/ai/budget.py: DEFAULT_TOTAL_CHARS` |
| `max_analysis_rounds`（最大分析轮次） | 10/片（周版本）；8（单提交默认） | —— | 是（`ROUNDS_PER_SHARD`） | `services/ai/auto_sizing.py` |
| `max_tool_requests`（上下文索取上限） | 按预算反解（默认 ≈48~50/片）；40（单提交默认） | 8 ~ 60/片 | 是 | `services/ai/auto_sizing.py: derive_family_sizing` |
| `subagent_count`（子代理数量） | 5（目标，钳 2~6 且 ≤ 维度数） | —— | 是（`SHARD_TARGET`） | `services/ai/auto_sizing.py` |
| 单次异常上限（报告的结论条数上限） | 20（周版本平台常量；单提交默认 20 可读老行） | —— | 是（`ANOMALY_RUN_CAP`） | `services/ai/auto_sizing.py`；周版本路径在 `ai_analysis_service` 里覆盖 |
| 每片异常上限 | clamp(ceil(20×2÷片数), 4, 12) | —— | 是 | `services/ai/auto_sizing.py` |
| 取样上限（清单过长时） | clamp(文件数, 200, 500) | —— | 是（`sampling_cap_for`） | `services/ai/auto_sizing.py` |
| 请求超时 | 300 秒平台内置 | —— | 是（不可配） | `models/ai_analysis/project_config.py: DEFAULT_REQUEST_TIMEOUT_SECONDS` |

这七个「平台推导」键在配置接口上**收到即报错**（`endpoint_service.RETIRED_FIELDS`），
模型层的列与出厂默认仍在 —— 单提交分析路径继续读它们。

## 单条上下文上限（不是配置项，但决定预算算式）

`services/ai/context_tools.py: DEFAULT_TOOL_LIMITS`：

| 工具 | 单条上限（字） |
|---|---|
| `file_diff` | 11,000 |
| `file_content`（配表/代码正文） | 11,000（与取数侧 `utils.content_window.CONTENT_MAX_CHARS` 同值，**必须同值**） |
| `read_reference` | 11,000 |
| `find_references` | 8,000 |
| `commit_detail` | 6,000 |

算式里用的是这些值的**最大值**（11,000，即 `auto_sizing.ITEM_CHARS`）—— 因为预算必须
放得下最坏的那一种索取。

## 四条耦合

### 1. 用户预算 ≥ 基线 + 清单 + 知识包 + 索取次数 × 单条上限

```
配置值 ≥ 6,000（历史结论基线，auto_sizing.BASELINE_CHARS）+ 变更清单字数
       + 项目知识包 + 项目补充指令
       + 每片索取次数 × 11,000（auto_sizing.ITEM_CHARS）
```

收敛后这条耦合的**用法反过来了**：不是先填索取次数再验预算，而是**平台按预算反解
索取次数**（`(生效额度 − 清单 − 基线) ÷ 11,000`，钳 8~60）。「想多看点内容」的唯一
动作是加预算；窗口不够时平台压预算并说明（见第 3 条）。

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
（后者管的是报告里最多列几条异常）。两个数都在「条数」这个词上，但一个管输入、一个管输出。
理由：条数上限小于索取次数时，会出现「付了 N 次索取、只带走 20 条」——取回来的上下文被
按条数静默裁掉，白花额度。所以索取次数高过 40 之后，条数上限自动跟着走，不需要手工配。

### 3. 预算的上限是窗口水位

`services/ai/budget.py: effective_prompt_budget` / `context_watermark_chars`：
预算会被压到「模型窗口 × 60%」（`COMPACT_AT_RATIO`，留 40% 给回复与估算误差）。
端点声明了窗口就用真的，问不到就按 1M token 的 60% = 600,000 字这个**口径值**处理，
并在说明里写明「按默认值处理」。

**水位压的是「平台内置 + 配置值」这个和**，压完再把内置那段还给平台、剩下的才是用户的
（`_apply_model_window` 的 `platform_chars`）。所以**配置值填得比窗口水位大是无效配置**：
窗口 200k 时用户那部分最终只有约 104,000 字。默认 560,000 + 内置 16,000 = 576,000 仍低于
600,000，所以「问不到窗口」的项目行为不变。

### 4. 分片额度：串行共享池（2026-09-23 起，替代「每成员一份」）

`services/ai/subagent.py: FamilyQuota`。`MEMBER_BUDGET_PERCENT` 已退休。口径：

- 全家共一个**索取池 + 轮次池**（池 = 分片数 × 每片名义额；名义额由
  `auto_sizing` 推导，是保底而不是封顶）；
- 分片**串行**执行，每片开跑前按「池剩余 − 后续角色 × 下限」算**当片上限**，
  用了多少按实耗扣池 —— **前片用剩的滚给后片**（run 41 里「S2 双顶饿死、S1/S3
  各剩十几二十次」的浪费正是它要解决的）；
- 汇总有保底下限（`MIN_MEMBER_ROUNDS = 2` / `MIN_MEMBER_TOOL_REQUESTS = 2`，可越池），
  对账轮只用剩余、不占预留；
- 随成员变化的数字（当片上限、已用）只进任务书与后续轮次消息，**共享前缀里只有
  家族常量** —— 提示词缓存不变量（seed 前缀全成员逐字节相同）靠这个成立；
- 预算计划的 `family_pool` 节把池总量 / 名义额 / 下限 / 推导依据摆给用户，
  `job_theoretical_max` 按「池 + 汇总下限」算。

一次分析的模型调用次数 = **各代理各自跑了几轮之和**（每个代理的每一轮 = 一次调用）；
代理个数 = 分片数 + 1（汇总）+ 0 或 1（对账轮）。

## 上下文超长时的三级补救（读预算相关报告时要知道）

`services/ai/engine.py`：上游以「超长」拒绝时，按「丢得越少越先试」补救 ——
① 历史留着、本轮条目按 1/4 额度重压；② 连历史一起丢；③ 换成一段必然装得下的「收尾」
提示词（只带清单开头与已取内容目录）。每一步都先算出「确实压小了」才发。

补救成功之后，**实测到的上限**（刚被拒的那条提示词有多大）会被收进本次运行剩下的轮次：
下一轮按更小的预算组装，不再撞一次。这也是为什么「预算配得比窗口大」的代价不只是
白配 —— 它会让每一次分析都走一遍补救、并以「降级」收尾。
