"""AI 版本分析的内部实现。

按职责拆分，避免继续把 `services/ai_analysis_service.py` 那个 783 行的单文件撑大：

- `skill_contract`  —— skill 的结构契约与校验（零第三方依赖）
- `llm_client`      —— OpenAI 兼容客户端（SSE、超时、退避重试）
- `protocol`        —— 模型输出的 JSON 容错解析、协议与接地校验
- `budget`          —— 提示词字符预算与分级压缩（纯函数）
- `context_tools`   —— 白名单工具：按需索取上下文与 skill 文档
- `skill_loader`    —— 加载平台 skill 与项目知识包，带内容哈希
- `rules`           —— 异常归一化、语义去重、封顶
- `engine`          —— 多轮循环编排

只有需要跨模块共享的常量才放到 `skill_contract` 里（它是契约的单一事实源）。
"""
