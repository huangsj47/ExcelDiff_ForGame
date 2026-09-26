"""AI 版本分析的内部实现。

按职责拆分，避免继续把 `services/ai_analysis_service.py` 那个单文件撑大。
**每个模块抬头都写了自己为什么单独一个文件**，这份清单只是索引。

## 契约与协议（零业务依赖）

- `skill_contract`  —— skill 的结构契约与校验（零第三方依赖）
- `protocol`        —— 模型输出的 JSON 容错解析、协议与接地校验
- `scope`           —— 分析范围（全量 / 增量 / 按仓库）的取值与判定
- `pricing`         —— 模型单价表与费用估算
- `bundles`         —— 提示词里几段固定文本的拼装

## 提示词与预算

- `prompt`          —— 系统提示词 / 用户消息的拼装（版本哈希的来源）
- `budget`          —— 每轮提示词字符水位与分级压缩（纯函数）
- `prompt_cache`    —— 提示词缓存标记（只声明「这个端点接受哪种约定」，不猜）
- `windowed_view`   —— 超长内容的「分段 + 点名」视图
- `skill_loader`    —— 加载平台 skill 与项目知识包，带内容哈希

## 引擎与工具

- `llm_client`      —— OpenAI 兼容客户端（SSE、超时、退避重试）
- `engine`          —— 多轮循环编排
- `context_tools`   —— 白名单工具：按需索取上下文与 skill 文档
- `reference_search`—— 关键词检索（在本次改动的文件里找标识符出现在哪几行）
- `subagent`        —— 子代理模式：分片 → 汇总（→ 可选对账轮）
- `family_ledger`   —— 子代理的数据模型与平台侧对账（谁交回了什么、候选去哪儿了）
- `run_progress`    —— 逐轮进度的事件发布

## 取数（平台侧）

- `platform_provider` —— 接到平台取数链路上的 `ContextProvider`：区分「拿不到」与
  「没有内容」，并把结构化 diff 渲染成模型读得懂的文本
- `excel_source`      —— 配表正文的取数：工作簿 → 工作表文本，按字符额度精确收敛
- `excel_view`        —— 配表差异的渲染叶子（单元格与表头的排版）
- `stored_diff_source`—— 平台**已经算好并落库**的那份 diff（唯一读库的一段）
- `sheet_stats`       —— 配表整表统计（列上限 / 分位数 / 去重取值 / 空值率）
- `baseline_source`   —— 上一轮结论的取数来源（增量分析的基线）
- `project_config_source` —— 项目级 AI 配置与接口凭据的读写（含端点客户端构造）
- `run_cache_source`  —— 分析记录的保留期与复用（缓存判定 / 回放 / 过期清理）
- `scope_sampling`    —— 这一轮按什么范围跑（变更清单取样、全量还是增量）
- `change_set`        —— 本批次变更集合的组装
- `project_facts`     —— 项目声明的检查维度与仓库事实
- `weekly_sync_gate`  —— 「同步还没跑完就别分析」的闸门
- `weekly_state`      —— 周版本分析状态的落库
- `analysis_budget`   —— 项目 / 平台两级用量闸门
- `platform_budget`   —— 平台级预算的存取
- `usage`             —— token 与费用的口径
- `usage_statistics`  —— 用量统计的查询与基线
- `trace_evidence`    —— 逐轮证据的落库与「失败说明」前缀的识别

## 结果与呈现（都是纯函数）

- `rules`           —— 异常归一化、语义去重、封顶
- `result_payload`  —— 落库 / 下发给界面的结果字典
- `report_document` —— 导出成一份单文件 markdown
- `conclusion_view` —— 结论的读侧形态（中文口径在这里算）
- `baseline`        —— 增量对比：仍成立 / 已修复 / 已被推翻
- `anomaly_disposition` —— 人工处置的取值校验与四个字段的口径
- `project_pack_service` —— 项目专属知识包（`skills/projects/<代号>/`）的读写

## 约定

- 只有需要跨模块共享的常量才放到 `skill_contract` 里（它是契约的单一事实源）。
- `prompt.py` / `rules.py` 的内容参与版本哈希（`*_SOURCE_FILES`）：**往里加字节要同步
  那个元组**，否则版本号会静默不变。
"""
