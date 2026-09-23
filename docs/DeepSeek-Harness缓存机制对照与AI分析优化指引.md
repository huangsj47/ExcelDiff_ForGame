# DeepSeek Harness 缓存机制对照与本平台改造指引

> 2026-09-23，基于本机 `../deepseek-harness` 源码、本平台当前代码、SQLite 历史运行 45/46 和 DeepSeek 官方文档。平台尚未正式部署，可以调整协议和表结构；但须以风险召回及证据质量为验收前提。本文供后续 AI 实施，具体任务以“实施顺序与验收”一节为准。

## 结论与证据边界

DeepSeek Harness 的可借鉴之处是**稳定、可重建的请求前缀**：系统消息、工具定义和调用配置有确定顺序；会话事件只追加，下一次请求从事件日志重建为上次请求的前缀加新消息。它还用真实 API 测试验证首轮之后 `cacheReadTokens > 0`。这个测试**没有**公布整体命中率，也不能证明 Harness 比本平台的 82%–84% 更高。DeepSeek 的缓存本来就是自动启用的，无需发送 Anthropic 式 `cache_control`；相同前缀只是命中的必要条件，服务端持久化与有效期仍由 DeepSeek 决定。[官方缓存规则](https://api-docs.deepseek.com/guides/kv_cache/)；[Harness 架构说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/implemented/architecture/2026-07-05-reconstructable-requests.md)。

本平台的 `services/ai/engine.py::run_analysis` 已做到单条分析会话内 system 只构造一次、后续 user/assistant 追加；`services/ai/subagent.py` 也构造共享前缀。`services/ai/prompt_cache.py` 默认 `auto + none` 不发送显式标记，符合 DeepSeek 自动缓存机制。故**不应为了“引入 Harness 缓存”重写现有对话协议或盲目加缓存标记**。当前最值得优化的是多分片重复推理、过长输出及缓存失效时缺乏定位数据。

| 本地记录 | 模型轮次 | 输入 token（含缓存命中） | 缓存读取 | 命中率 | 未命中输入 | 输出 token | 总耗时 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| run 45，多成员 | 33 | 733,484 | 615,680 | 83.9% | 117,804 | 246,426 | 18 分 45 秒 |
| run 46，单成员 | 4 | 76,058 | 62,208 | 81.8% | 13,850 | 38,275 | 2 分 59 秒 |

来源：`instance/diff_platform.db` 的 `ai_analysis_run` 与 `ai_analysis_trace`。run 45/46 所用周版本快照已被此前检查发现含不可达旧提交，**只可用于成本、轮次和缓存行为比较，不能用来声称准确率相同**；详见 [全链路复测指引](AI分析全链路复测与强制改造指引-2026-09-23.md)。两轮 `trace.duration_ms` 合计分别占总时长约 99.7%/99.2%，本样本中瓶颈在模型调用链路，不能靠加快 Git 读文件解决 20 分钟问题。命中率较高的 run 45 反而消耗更多 token 和时间，**不能把命中率当唯一目标**。

## Harness 到本平台的机制对照

| 机制 | Harness 的可核查实现 | 本平台现状 | 判断 |
| --- | --- | --- | --- |
| 稳定前缀 | `packages/core/session` 事件追加；`agent-loop/src/agent.ts` 从 `deriveMessages()` 生成请求，`request/header` 只在首次、恢复、配置改变或新消息系列时写入完整快照；`invariant` 可独立重建校验。 | `engine.py` 在内存里追加消息；`tests/test_ai_prompt_cache.py` 检查追加性质。 | 单次运行已具备基本条件；缺请求级指纹与跨进程可重建性。无需照搬整套事件存储。 |
| 稳定系统/工具顺序 | `system-prompt/src/index.ts` 按 order/name 排系统段与工具定义；真实改变才替换系统节点。 | `prompt.py::build_system_prompt` 固定拼平台段、项目知识、补充指令；五类只读取数是 JSON `requests` 协议，未向模型 API 注册原生工具。 | 检查所有集合遍历排序和知识文件版本即可；不必仅为缓存改成原生 tool calls。 |
| 多轮证据 | 工具结果进入可重放会话；相同历史成为下轮前缀，直到压缩。 | 后续轮也保留历史；`budget.py::compact_history` 会改写旧消息；`engine.py` 在超提示词字符预算时调用。 | 压缩必然可能令后续请求从改写处开始失去命中；记录压缩原因与影响范围，别为了命中率禁用必要压缩。 |
| 缓存用量 | `llm-deepseek/.../translate.ts::mapUsage` 将 `prompt_tokens - cacheRead` 存为 `inputTokens`，缓存读取单列；端到端测试只要求后续请求命中数大于零。 | `llm_client.py` 存 `tokens_input = prompt_tokens`（**包含**命中），另存 `cache_read_tokens`；`pricing.py` 算价格时扣除命中输入。 | 两套 `inputTokens` **口径不同**。跨系统比较必须先统一为 `prompt_total = uncached + cache_read`。 |
| 缓存开关 | DeepSeek 官方 Chat Completions 自动缓存，不靠客户端显式断点。 | `prompt_cache_mode=auto`、`prompt_cache_format=none` 默认无标记；其他网关可声明 Anthropic 形态。 | DeepSeek 端保持默认；不要按模型名或 URL 猜测标记能力。 |

DeepSeek 当前规则是请求边界、检测到的共同前缀、长输入的固定 token 间隔均可能形成缓存单元；首次出现的相同片段未必立刻命中，缓存也会在数小时到数天的不活跃期后被清理。因此“5 小时后增量分析”的正确性必须依赖**平台持久化的上一版结论和快照**，绝不能依赖提供商缓存仍在。[官方说明](https://api-docs.deepseek.com/guides/kv_cache/)。Harness 测试注释提到 64 token 是测试环境的历史粒度假设；当前官方规则未承诺固定为 64，实施时不要写死这个数。

## 本平台的具体机会

1. **优先减掉重复推理，而不是继续抬高缓存命中率。** run 45 比 run 46 多 29 次模型请求、208,151 个输出 token。高命中只降低一部分输入成本，对输出生成和重复决策无效。沿用 [全链路复测指引](AI分析全链路复测与强制改造指引-2026-09-23.md) 的规模分流、按变更区域分工、一次汇总和条件复核方案；不能按“维度数多”就开多个都审相同文件的成员。压缩中间轮输出为请求清单和证据卡，但保留必要的 QA 判断与证据。输出 token 可能含隐藏推理 token，新增 `reasoning_tokens` 观测后再下调 `max_output_tokens`。
2. **把共享前缀做成可验证约束。** 系统规则、项目知识摘要、同快照变更清单应处于成员专属任务书、运行 ID、时间戳和轮次预算等可变内容之前。明确对各成员发出的实际 `messages` 序列化字节进行前缀测试；确保 diff 文件、提交和知识索引排序稳定。模型名/配置/知识或 diff 逻辑真改变时允许自然失效，不要把旧前缀强行复用到错误版本。项目知识过长时仍按需读取正文，不为缓存率把整套项目文档提前塞进系统提示词。
3. **补能诊断缓存失效的逐请求账。** 目前 trace 有用量和时长，却没有“本次请求与上次在哪个消息开始分叉”。每次调用前，对**发送前去掉内部缓存断点后的真实消息**按固定 JSON 规则编码，在本机计算整请求哈希、稳定前缀哈希、与前一请求的最长公共前缀消息数/字符数，并记录 prompt 版本、快照 ID、角色、是否压缩、模型和端点标识；不得保存 API key 或为该诊断重复落库存储完整提示词。哈希是本机诊断指标，提供商按 token/内部单元匹配，哈希相同也不保证缓存命中。逐轮保存上游报告的命中、未命中、总输入、输出、reasoning 和 `usage` 来源；`None` 仍表示未上报。
4. **分开优化两层缓存。** 上游 prompt cache 复用相同前缀；平台 `ContextTools` 的 `body_cache` 避免重复向 Git/Excel 取数。模型已见过同一原文时后续只给定位引用，其他成员只获取其任务必需的原文。跨运行内容缓存要以冻结仓库 tip/对象 hash、路径、表解析配置、查询窗口和逻辑版本为键，历史改写后必须失效；它不能替代模型对本轮新 diff 的复核。
5. **不要照搬 Harness 的高复杂度部分。** 事件溯源、不可变会话和全量工具快照适合通用多工具代理；本平台目前的 JSON `requests` 协议、单次有限轮分析与落库 trace 更窄。若只为命中率迁移整套 session/event store，收益没有证据。Harness 的 `in-history` 系统提示词更新还依赖特定适配器能力，本平台当前 Chat Completions 网关未验证，不能直接插入中途 system 消息。

## 强制实施顺序与验收

平台未部署；允许直接调整 trace 表结构和分析协议，不写兼容旧运行的复杂迁移。下面任务可以拆给多个实施者，但共享的模型请求封装与统计口径只能由一处实现，避免两组同时改 `engine.py`。

**A. 先做可观测性（独立任务，可先合并）。** 在 `llm_client.py` 返回 `completion_tokens_details.reasoning_tokens`（上游未报留 `None`）；在 `engine.py` 每轮调用前后产出 `request_fingerprint`、`stable_prefix_fingerprint`、`prefix_divergence_reason`（`initial / append / compaction / prompt_change / snapshot_change / other`）及逐轮用量；通过现有 `ai_analysis_trace` 持久化。必要时新建纯函数模块实现规范化和比较。测试覆盖相同前缀追加、内部断点变化但实际请求不变、首条动态消息变化、压缩、上游未报用量、不同项目知识。不要把原始敏感 prompt 放进诊断表。

**B. 校准请求构造（依赖 A 的测试，改动范围尽量小）。** 检查 `build_system_prompt`、`subagent` 共享种子、`build_user_message` 的真实发出字节；固定非语义集合的排序。把运行时字段留在稳定前缀之后；若某字段必须改变 system 语义则保留变化。增加“相同快照的分片共享前缀”和“下一轮是上轮消息追加，除压缩外”测试。DeepSeek 端不启用显式 `cache_control`；其他网关保持能力声明和失败回退。

**C. 降低重复分析成本（与 B 串行改编排层）。** 先修 [全链路复测指引](AI分析全链路复测与强制改造指引-2026-09-23.md) 中的周版本快照污染、分片重叠和工具/报告协议问题。小规模变更默认单分析者；大规模分片按文件/业务区域划分，明确归属与交叉复核条件。中间轮返回短证据卡，最终报告只生成一次。复核者读候选与证据引用，存在冲突/高风险/缺证时再取原文；禁止因为缓存命中率高就放任并行成员重复读表、重复写全报告。

**D. 用同快照 A/B 验收（依赖 A–C）。** 冻结仓库 tip、diff、知识包、模型/温度及价格表；各模式至少重复数轮以区分服务端缓存冷热和输出波动。记录 `hit / prompt_total`、`prompt_total - hit`、输出及 reasoning token、模型调用轮次、墙钟时间、每个风险的证据与 QA 金标召回/误报。另做暖缓存与冷缓存分组；别把不同快照或不同质量的报告直接比成本。接受变更须先满足金标召回不下降、证据引用可核对、严重误报不增加，然后比较总费用和耗时；命中率单独上升不算通过。没有金标时仅上线观测与确定性去重，不调低质量预算。

## 两个实施陷阱

- **统计口径陷阱：** DeepSeek `prompt_tokens` 含缓存命中，本平台 `tokens_input` 也是总数；Harness 的 `inputTokens` 已减去 `cacheReadTokens`。比较价格时不能再从 Harness 的 `inputTokens` 减一次命中。推荐 UI 同时列总输入、未命中输入、缓存输入、输出/推理及未上报状态。上游 `prompt_cache_hit_tokens + prompt_cache_miss_tokens != prompt_tokens` 时现有解析会拒绝缓存字段，此保护应保留。
- **稳定前缀陷阱：** `compact_history`、提示词/知识更新、不同 diff、服务端缓存过期都会降低命中；这不是都能修的 bug。为了复用缓存而隐藏真实变更或禁止必要压缩，会直接损害报告正确性。用 A 的分叉原因判断是否是平台意外重写，再决定修复。

## 核查入口

- Harness 本地：`../deepseek-harness/packages/core/agent-loop/src/agent.ts`、`packages/core/system-prompt/src/index.ts`、`packages/llm/llm-deepseek/src/protocols/chat-completions/translate.ts`、`packages/core/agent-loop/tests/request-cache.e2e.ts`、`.agents/notes/implemented/architecture/2026-07-05-reconstructable-requests.md`。
- 本平台：`services/ai/engine.py`、`prompt.py`、`prompt_cache.py`、`llm_client.py`、`budget.py`、`subagent.py`、`pricing.py`、`tests/test_ai_prompt_cache.py`。
- 外部一手资料：[DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)、[DeepSeek Harness 请求重建设计](https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/implemented/architecture/2026-07-05-reconstructable-requests.md)。
