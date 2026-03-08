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
- [x] 1. 拆分 `app.py`（按领域路由/编排职责拆分到 `routes/*` 和 `services/*`）
  - 进展（2026-03-08）：已将 schema 迁移辅助逻辑下沉到 `services/db_migration_service.py`，`create_tables()` 改为服务编排调用。
  - 进展（2026-03-08）：已新增 `services/app_bootstrap_db_service.py`，将 `create_tables` 与启动期缓存清理实现迁出，`app.py` 仅保留入口封装。
  - 进展（2026-03-08）：已新增 `services/auth_bootstrap_service.py`，将 Auth 初始化、qkit 兜底路由与诊断逻辑迁出，`app.py` 启动层改为单行调用。
  - 进展（2026-03-08）：已新增 `services/app_routing_bootstrap_service.py`，将端点短名别名注册与模板过滤器注册迁出，`app.py` 启动入口进一步精简。
  - 进展（2026-03-08）：已新增 `services/app_runtime_wiring_service.py`，将 commit diff / weekly / task worker 运行时 wiring 编排迁出，入口仅保留单点调用。
  - 进展（2026-03-08）：已新增 `services/app_security_bootstrap_service.py`，将权限校验钩子、CSRF钩子、403/404处理与模板全局注册迁出，入口层改为统一配置调用。
  - 进展（2026-03-08）：已新增 `services/repository_update_form_service.py`，将仓库编辑表单处理与切换清理流程迁出，`app.py` 仅保留路由入口封装。
  - 进展（2026-03-08）：已新增 `services/repository_update_api_service.py`，将仓库更新/复用/批量凭据 API 与异步更新 worker 迁出，入口路由改为薄封装调用。
  - 进展（2026-03-08）：已新增 `services/commit_status_api_service.py`，将单条/批量提交状态更新逻辑迁出，`app.py` 仅保留兼容入口与依赖注入。
  - 进展（2026-03-08）：已新增 `services/repository_maintenance_api_service.py`，将缓存重建、缓存状态、clone状态、重试同步与手动同步流程迁出，`app.py` 进一步收敛为入口编排。
  - 进展（2026-03-08）：已新增 `services/commit_diff_page_service.py`，将完整文件diff与刷新diff流程迁出，`app.py` 进一步聚焦路由入口与依赖注入。
  - 进展（2026-03-08）：已新增 `services/commit_diff_view_service.py`，将 commit diff 主页面渲染与 Excel 缓存回退逻辑迁出，`app.py` 保留薄入口与兼容注释。
  - 进展（2026-03-08）：已新增 `services/excel_diff_api_service.py`，将 Excel diff API（Agent派发/HTML缓存/数据缓存/实时回退）迁出，`app.py` 仅保留入口封装。
  - 进展（2026-03-08）：已新增 `services/commit_list_page_service.py`，将提交列表筛选/分页/作者映射与仓库分组逻辑迁出，`app.py` 仅保留兼容入口。
  - 进展（2026-03-08）：已新增 `services/commit_diff_new_page_service.py`，将 `commit_diff_new` 页面逻辑迁出，`app.py` 继续收敛为入口编排。
  - 进展（2026-03-08）：已新增 `services/app_request_logging_service.py`，将 Agent 访问日志过滤与 admin 请求日志钩子迁出，`app.py` 启动入口仅保留配置调用。
  - 进展（2026-03-08）：已新增 `services/app_blueprint_bootstrap_service.py`，将蓝图注册与失败日志追踪下沉，`app.py` 入口改为单行编排调用。
  - 进展（2026-03-08）：已新增 `services/app_lifecycle_bootstrap_service.py`，将生命周期管理器构建、Auth 默认数据初始化与 atexit 注册日志迁出，入口层保留兼容壳函数。
  - 进展（2026-03-08）：已新增 `services/app_template_context_service.py`，将模板上下文处理器注册逻辑迁出，`app.py` 仅保留配置调用。
  - 进展（2026-03-08）：已新增 `services/commit_route_scope_service.py`，将 commit 访问校验与 `*_with_path` 路由分发逻辑统一下沉，入口层减少重复实现。
  - 进展（2026-03-08）：已新增 `services/repository_misc_page_service.py`，将仓库编辑页渲染与本地目录存在性检查迁出，`app.py` 继续保持薄封装。
  - 进展（2026-03-08）：`app.py` 已降至 1014 行，并新增回归测试守护文件长度预算（<2000 行）。
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
  - 进展（2026-03-08）：`task_worker_service` 第三批运行链路异常已收敛（任务状态更新失败兜底、Git/SVN预修复异常、Excel任务入队异常），将多处 `except Exception` 收敛为明确异常集合。
  - 进展（2026-03-08）：`task_worker_service` 第四批分支刷新异常已收敛（仓库分支拉取与 worker 外层错误处理），减少通用 `except Exception` 并保持回滚兜底。
  - 进展（2026-03-08）：`task_worker_service` 第五批任务执行异常已收敛（worker 主循环、Excel/周版本/自动同步主流程），并将仓库类型/不存在错误改为 `ValueError` 进入统一异常路径。
  - 进展（2026-03-08）：已新增 `services/branch_refresh_service.py`，将异步分支刷新 worker 逻辑从 `task_worker_service` 下沉为独立服务，`task_worker_service` 保留兼容入口包装。
  - 进展（2026-03-08）：已新增 `services/task_worker_weekly_handlers.py`，将周版本同步与周版本 Excel 缓存任务处理逻辑下沉，`task_worker_service` 仅保留兼容包装与依赖注入。
  - 进展（2026-03-08）：`commit_diff_page_service` / `excel_diff_api_service` / `commit_operation_handlers` 已补充 `SQLAlchemyError`、`ValueError`、`RuntimeError` 分层异常分支；`git_service` 顶层编码初始化分支收敛了裸 `except`。
  - 下一步：继续按模块将通用 `except Exception` 拆分为更具体异常（IO/网络/数据校验）并补充错误标签。
  - 验收：关键流程改为“可预期异常 + 明确兜底”；异常标签可观测。

### P1（随后）
- [ ] 5. 拆分 `services/git_service.py`（clone/sync/diff/excel 逻辑分模块）
  - 进展（2026-03-08）：已新增 `services/git_diff_helpers.py`，将 unified diff 解析、基础 diff 生成、初始提交 diff 生成、DataFrame 比较从主类中拆出；`git_service.py` 已降至 2267 行。
  - 验收：单文件 < 1800 行；核心函数长度 < 120 行。
- [ ] 6. 拆分 `services/weekly_version_logic.py`（查询、聚合、渲染分离）
  - 进展（2026-03-08）：已新增 `services/weekly_deleted_excel_helpers.py`，将“Excel 删除态识别/回退版本定位/提示 HTML 组装”拆出；`weekly_version_logic.py` 已降至 1968 行（脱离 ERROR 区间）。
  - 验收：核心 API 只做编排，查询与渲染抽离。
- [x] 7. 统一错误响应规范（HTTP code + `status/message/error_type/retry_after_seconds`）
  - 已完成（2026-03-08）：新增 `services/api_response_models.py` + `services/api_response_service.py`，并接入 `commit_diff_page_service`、`excel_diff_api_service`、`commit_operation_handlers` 的 commit diff / excel diff / merge diff 响应链路。
  - 验收：commit diff、excel diff、merge diff 响应结构一致。
- [x] 8. 增加 pre-commit 钩子（格式化 + 基础 lint + 文件长度守卫）
  - 已完成：新增 `.pre-commit-config.yaml`，接入 `ruff` + `scripts/check_file_length.py --strict`。
  - 验收：本地提交前可自动发现问题，减少回归。

### P2（持续改进）
- [ ] 9. 为高复杂函数补充分层单测（边界条件、异常分支、缓存回退）
  - 进展（2026-03-08）：已新增 `tests/test_todo_contract_and_service_split_followups.py`，覆盖统一响应契约、git diff helper 行为、weekly 删除态缓存回退路径与服务拆分静态守护。
  - 验收：新增针对性测试，不仅是 happy path。
- [ ] 10. 统一服务层输入/输出模型（dataclass/pydantic）
  - 进展（2026-03-08）：已引入 `ErrorResponsePayload` / `SuccessResponsePayload`（dataclass），并作为 diff 相关服务统一输出模型。
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
