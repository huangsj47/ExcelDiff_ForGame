# 平台代码规范整改 TODO（Superpowers 扫描）

更新时间：2026-03-07

## 扫描基线
- 文件长度超限（`python scripts/check_file_length.py`）：
  - `app.py` 3403 行（ERROR）
  - `services/git_service.py` 2419 行（ERROR）
  - `services/weekly_version_logic.py` 2084 行（ERROR）
  - `services/agent_management_handlers.py` 1913 行（WARNING）
- 异常处理密度（`except Exception`）Top：
  - `app.py` 54 处
  - `services/task_worker_service.py` 51 处
  - `services/git_service.py` 47 处
  - `services/weekly_version_logic.py` 35 处
- 调试输出残留：
  - `app.py` 中 `_original_print(...)` 22 处

## TODO 列表（按优先级）

### P0（先做）
- [ ] 1. 拆分 `app.py`（按领域路由/编排职责拆分到 `routes/*` 和 `services/*`）
  - 进展（2026-03-08）：已将 schema 迁移辅助逻辑下沉到 `services/db_migration_service.py`，`create_tables()` 改为服务编排调用。
  - 进展（2026-03-08）：已新增 `services/app_bootstrap_db_service.py`，将 `create_tables` 与启动期缓存清理实现迁出，`app.py` 仅保留入口封装。
  - 验收：`app.py` 降到 < 2000 行；路由注册和容器初始化清晰分层。
- [x] 2. 建立静态检查基线（`ruff` + `flake8` 二选一，推荐先 `ruff`）
  - 已完成：新增 `pyproject.toml`（ruff 规则）、`scripts/run_ruff_changed.py`（增量检查）、`.github/workflows/quality-gate.yml`（CI 门禁）。
  - 验收：CI 基于改动文件阻断新增违规，避免一次性清理历史存量。
- [x] 3. 清理 `_original_print` 调试输出，统一走 `utils.logger.log_print`
  - 已完成：`app.py` 启动/蓝图注册/初始化链路已移除 `_original_print`，统一走 `log_print`。
  - 验收：生产路径不再出现 `_original_print`；日志级别可配置。
- [ ] 4. 缩减裸 `except Exception`（先处理 `app.py`、`task_worker_service.py`）
  - 进展（2026-03-08）：已清理裸 `except:`，并将 DB rollback 相关分支收敛为 `SQLAlchemyError`。
  - 进展（2026-03-08）：`reuse_repository_and_update`/`update_repository_and_cache`/`batch_update_credentials` 已按 `NotFound`、`SQLAlchemyError`、参数错误分类处理，并新增接口异常单测覆盖。
  - 进展（2026-03-08）：`update_commit_status`/`batch_update_commits_compat` 已补充 JSON 参数校验与数据库异常分类处理，新增提交状态接口异常单测。
  - 进展（2026-03-08）：`task_worker_service` 首批工具函数异常已收敛（目录删除、进程清理、任务状态更新、ID解析），并补充异常路径单测。
  - 进展（2026-03-08）：`task_worker_service` 第二批任务调度函数异常已收敛（任务创建/加载/调度），并补充数据库异常回滚测试。
  - 下一步：继续按模块将通用 `except Exception` 拆分为更具体异常（IO/网络/数据校验）并补充错误标签。
  - 验收：关键流程改为“可预期异常 + 明确兜底”；异常标签可观测。

### P1（随后）
- [ ] 5. 拆分 `services/git_service.py`（clone/sync/diff/excel 逻辑分模块）
  - 验收：单文件 < 1800 行；核心函数长度 < 120 行。
- [ ] 6. 拆分 `services/weekly_version_logic.py`（查询、聚合、渲染分离）
  - 验收：核心 API 只做编排，查询与渲染抽离。
- [ ] 7. 统一错误响应规范（HTTP code + `status/message/error_type/retry_after_seconds`）
  - 验收：commit diff、excel diff、merge diff 响应结构一致。
- [x] 8. 增加 pre-commit 钩子（格式化 + 基础 lint + 文件长度守卫）
  - 已完成：新增 `.pre-commit-config.yaml`，接入 `ruff` + `scripts/check_file_length.py --strict`。
  - 验收：本地提交前可自动发现问题，减少回归。

### P2（持续改进）
- [ ] 9. 为高复杂函数补充分层单测（边界条件、异常分支、缓存回退）
  - 验收：新增针对性测试，不仅是 happy path。
- [ ] 10. 统一服务层输入/输出模型（dataclass/pydantic）
  - 验收：跨模块 payload 字段约束明确，减少隐式字段依赖。

## 修改风险建议

### 风险 1：大文件拆分导致路由导入循环或初始化顺序问题（高）
- 触发场景：`app.py` 拆分后依赖注入顺序变化，蓝图注册失败。
- 建议：
  - 先做“无行为变化拆分”（仅搬运代码，不改逻辑）。
  - 每拆一个模块即跑全量测试。
  - 增加应用启动冒烟测试（蓝图注册、关键路由可达）。

### 风险 2：异常收敛后吞错行为改变（高）
- 触发场景：把宽泛 `except Exception` 改细后，历史“静默成功”路径变为显式失败。
- 建议：
  - 先给关键链路加错误指标与日志标签，再改异常边界。
  - 改造以“灰度文件/灰度仓库”逐步启用。

### 风险 3：引入 lint 门禁导致历史存量一次性爆雷（中）
- 触发场景：直接全仓启用严格规则，阻塞正常开发。
- 建议：
  - 采用 baseline 方式：先只拦截“新增/修改代码”。
  - 存量问题分批清理（按目录和优先级推进）。

### 风险 4：日志清理影响线上排障（中）
- 触发场景：删除 `_original_print` 后缺少关键现场信息。
- 建议：
  - 先迁移到结构化日志，不直接删；观察一周后再下线冗余日志。
  - 对 `AgentTask`、`BackgroundTask` 保留关键 ID 链路日志。

### 风险 5：服务层模型收敛引发字段兼容性问题（中）
- 触发场景：平台与 agent 的 payload 字段重命名或默认值变化。
- 建议：
  - 先定义版本化 schema（`schema_version`），保留兼容解析窗口。
  - 增加 contract test，覆盖 platform->agent 关键任务类型。
