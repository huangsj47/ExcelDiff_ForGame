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
  - 进展（2026-03-08）：已新增 `services/agent_task_result_service.py`，将 `agent_report_task_result` 任务结果回传编排从 `agent_management_handlers.py` 下沉，入口层保留薄包装，主处理文件降至 1782 行。
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
  - 进展（2026-03-08）：`commit_operation_handlers` 的确认/拒绝与优先处理接口已补充 `SQLAlchemyError` 分支和 JSON 结构校验，减少直接 `except Exception` 路径。
  - 进展（2026-03-08）：`commit_operation_handlers` 继续收敛作者映射、缓存解析、merge_diff 处理与更新字段接口异常边界；`task_worker_service` 的同步线程执行分支改为 `NON_CRITICAL_SYNC_THREAD_ERRORS`，去除对应 `except Exception`。
  - 进展（2026-03-08）：`commit_operation_handlers` 的 `get_commit_diff_data` / `refresh_merge_diff` 已改为 `COMMIT_OPERATION_UNEXPECTED_ERRORS` 明确异常集合，去除该文件剩余裸 `except Exception`。
  - 进展（2026-03-08）：`agent_management_handlers` 的环境变量/数值解析与 commit_time 解析辅助函数已改为明确异常集合（`_NUMERIC_PARSE_ERRORS` 等），默认管理员权限同步链路中的 auth backend 发现与模型导入分支已收敛裸 `except Exception`。
  - 进展（2026-03-08）：`agent_management_handlers` 的 release 查询/下载/列表/回滚接口已统一收敛到 `_AGENT_RELEASE_HANDLER_ERRORS`，替换对应 `except Exception` 分支并保持原有错误响应语义。
  - 进展（2026-03-08）：`agent_management_handlers` 的 register/heartbeat/incident/upsert temp cache/get temp cache/resolve temp cache/claim task 接口已统一收敛到 `_AGENT_ENDPOINT_HANDLER_ERRORS`，移除该文件剩余裸 `except Exception`。
  - 进展（2026-03-08）：`agent_task_result_service` 的 `handle_agent_report_task_result` 已收敛到 `AGENT_TASK_RESULT_HANDLER_ERRORS`，移除该服务内裸 `except Exception`。
  - 进展（2026-03-08）：`agent_commit_diff_dispatch` 的 `_int_env` / `_safe_json_loads` / `_payload_matches_commit` / 缓存清理与派发兜底分支已收敛到明确异常集合（`_NUMERIC_PARSE_ERRORS`、`_JSON_PARSE_ERRORS`、`_AGENT_COMMIT_DIFF_DISPATCH_ERRORS`），移除该模块裸 `except Exception`。
  - 进展（2026-03-08）：`agent_release_service` 的 manifest 读取、版本解析、时间解析、git commit 探测与临时文件清理分支已收敛为明确异常集合（`_JSON_PARSE_ERRORS`、`_SUBPROCESS_DETECT_ERRORS`、`ValueError`/`OSError`），移除该模块裸 `except Exception`。
  - 进展（2026-03-08）：`model_loader` 的 models/app 模块加载分支已收敛到 `MODEL_LOADER_IMPORT_ERRORS`，移除该模块裸 `except Exception`，并保持模块不可用时的兼容回退行为。
  - 进展（2026-03-08）：`repository_sync_status` 的记录/清理同步错误分支已收敛到 `REPOSITORY_SYNC_STATUS_ERRORS` 与 `REPOSITORY_SYNC_ROLLBACK_ERRORS`，移除该模块裸 `except Exception` 并保持回滚兜底。
  - 进展（2026-03-08）：`core_navigation_handlers` 的路由探测/`url_for` 包装/Qkit 登录兜底与首页路由异常分支已收敛到 `CORE_NAVIGATION_HELPER_ERRORS`、`CORE_NAVIGATION_HANDLER_ERRORS`，移除该模块裸 `except Exception`。
  - 进展（2026-03-08）：`app_blueprint_bootstrap_service` / `db_migration_service` / `commit_diff_new_page_service` 已收敛为明确异常集合（蓝图注册、表结构迁移、作者映射兜底），移除对应裸 `except Exception`。
  - 进展（2026-03-08）：`repository_update_api_service` 的异步更新 worker 兜底分支已收敛到 `REPOSITORY_UPDATE_WORKER_ERRORS`，移除该模块剩余裸 `except Exception`。
  - 进展（2026-03-08）：`app_request_logging_service` 的 Agent 访问日志过滤分支已收敛到 `REQUEST_LOG_MESSAGE_ERRORS` / `REQUEST_LOG_STATUS_PARSE_ERRORS`，移除该模块剩余裸 `except Exception`。
  - 进展（2026-03-08）：`app_bootstrap_db_service` 已将启动建表/表探测/SQLite 诊断/版本缓存清理分支收敛到 `DB_STARTUP_*_ERRORS` 明确异常集合，移除该模块裸 `except Exception` 并保留原有日志兜底语义。
  - 进展（2026-03-08）：`app_security_bootstrap_service` 的 auth backend 导入兜底与 `public_login_url` 路由构建分支已收敛到 `APP_SECURITY_*_ERRORS`，移除该模块剩余裸 `except Exception`。
  - 进展（2026-03-08）：`auth_bootstrap_service` 的 qkit 路由探测/注册、auth 路由诊断、默认数据初始化与 auth 模块初始化分支已收敛到 `AUTH_*_ERRORS` 明确异常集合，移除该模块剩余裸 `except Exception`。
  - 进展（2026-03-08）：`repository_maintenance_api_service` 的缓存重建、缓存状态查询与手动同步外层兜底分支已收敛到 `REPOSITORY_MAINTENANCE_*_ERRORS` 明确异常集合，移除该模块剩余裸 `except Exception`。
  - 进展（2026-03-08）：`repository_update_form_service` 的全量同步异常兜底、异步重筛外层兜底与表单提交流程外层兜底已收敛到 `REPOSITORY_UPDATE_FORM_*_ERRORS` 明确异常集合，移除该模块剩余裸 `except Exception`。
  - 进展（2026-03-08）：`commit_diff_view_service` 的作者映射兜底、缓存数据处理兜底与 Excel 主流程兜底分支已收敛到 `COMMIT_DIFF_VIEW_*_ERRORS` 明确异常集合，移除该模块剩余裸 `except Exception`。
  - 下一步：继续按模块将通用 `except Exception` 拆分为更具体异常（IO/网络/数据校验）并补充错误标签。
  - 验收：关键流程改为“可预期异常 + 明确兜底”；异常标签可观测。

### P1（随后）
- [x] 5. 拆分 `services/git_service.py`（clone/sync/diff/excel 逻辑分模块）
  - 进展（2026-03-08）：已新增 `services/git_diff_helpers.py`，将 unified diff 解析、基础 diff 生成、初始提交 diff 生成、DataFrame 比较从主类中拆出。
  - 进展（2026-03-08）：已新增 `services/git_excel_parser_helpers.py`，将 Excel 解析与 diff 组装主逻辑下沉为 helper；`git_service.py` 主类方法改为薄包装。
  - 进展（2026-03-08）：已移除未使用的旧并行/批处理方法，`git_service.py` 已降至 1655 行；函数长度检查结果中无 `>=120` 行的核心函数。
  - 验收：单文件 < 1800 行；核心函数长度 < 120 行。
- [x] 6. 拆分 `services/weekly_version_logic.py`（查询、聚合、渲染分离）
  - 进展（2026-03-08）：已新增 `services/weekly_deleted_excel_helpers.py`，将“Excel 删除态识别/回退版本定位/提示 HTML 组装”拆出；`weekly_version_logic.py` 已降至 1968 行（脱离 ERROR 区间）。
  - 进展（2026-03-08）：已新增 `services/weekly_excel_merge_helpers.py`，将 segmented 合并/Excel payload 提取/缓存回退加载逻辑拆出；`weekly_version_logic.py` 已降至 1795 行。
  - 进展（2026-03-08）：`weekly_version_logic.py` 已进一步降至 1796 行，核心缓存提取/合并/删除态判断均已拆到服务 helper，主入口以编排为主。
  - 验收：核心 API 只做编排，查询与渲染抽离。
- [x] 7. 统一错误响应规范（HTTP code + `status/message/error_type/retry_after_seconds`）
  - 已完成（2026-03-08）：新增 `services/api_response_models.py` + `services/api_response_service.py`，并接入 `commit_diff_page_service`、`excel_diff_api_service`、`commit_operation_handlers` 的 commit diff / excel diff / merge diff 响应链路。
  - 验收：commit diff、excel diff、merge diff 响应结构一致。
- [x] 8. 增加 pre-commit 钩子（格式化 + 基础 lint + 文件长度守卫）
  - 已完成：新增 `.pre-commit-config.yaml`，接入 `ruff` + `scripts/check_file_length.py --strict`。
  - 进展（2026-03-08）：长度守卫 warning 阈值从 1500 调整为 1800（error 仍为 2000），与当前渐进拆分阶段的文件预算一致，减少无效 warning 噪音。
  - 验收：本地提交前可自动发现问题，减少回归。

### P2（持续改进）
- [ ] 9. 为高复杂函数补充分层单测（边界条件、异常分支、缓存回退）
  - 进展（2026-03-08）：已新增 `tests/test_todo_contract_and_service_split_followups.py`，覆盖统一响应契约、git diff helper 行为、weekly 删除态缓存回退路径与服务拆分静态守护。
  - 进展（2026-03-08）：已新增 `tests/test_commit_diff_input_models_and_handlers.py`，覆盖 diff 请求输入模型、`refresh_merge_diff` 非法请求分支、agent 模式 `force_retry` 输入链路。
  - 进展（2026-03-08）：已新增 `tests/test_commit_operation_handlers_error_round3.py`，覆盖批量确认/拒绝、单条拒绝、优先处理与更新字段接口的非法请求/数据库异常路径。
  - 进展（2026-03-08）：`tests/test_commit_diff_input_models_and_handlers.py` 已补充 `get_commit_diff_data` 与 `refresh_merge_diff` fallback 异常分支测试，验证 `unexpected_error` 返回契约。
  - 进展（2026-03-08）：已新增 `tests/test_agent_management_exception_narrowing.py`，覆盖 agent 管理辅助函数的数值解析、时间解析及 auth 导入失败回退分支。
  - 进展（2026-03-08）：`tests/test_agent_management_exception_narrowing.py` 已补充 release 相关异常分支覆盖（latest/download/list/rollback），验证收敛后错误路径稳定返回。
  - 进展（2026-03-08）：`tests/test_agent_management_exception_narrowing.py` 已补充 register/heartbeat/get cache/resolve cache/claim task 的 fallback 异常分支，覆盖 `_AGENT_ENDPOINT_HANDLER_ERRORS` 返回契约。
  - 进展（2026-03-08）：`tests/test_agent_management_exception_narrowing.py` 已补充 `agent_task_result_service` 的 fallback 异常分支（KeyError/SQLAlchemyError），覆盖任务回传失败回滚契约。
  - 进展（2026-03-08）：`tests/test_agent_commit_diff_dispatch_retry_guard.py` 已补充环境变量解析与派发失败（KeyError）异常分支，覆盖 `agent_commit_diff_dispatch` 收敛后的兜底返回契约。
  - 进展（2026-03-08）：`tests/test_agent_release_update.py` 已补充 `detect_git_commit_id` 子进程异常、manifest 非法 JSON、版本非法输入等分支覆盖，验证 `agent_release_service` 收敛后返回契约。
  - 进展（2026-03-08）：`tests/test_model_loader_service_decoupling.py` 已补充 import 运行时异常（`RuntimeError`）回退分支覆盖，验证 `model_loader` 收敛后的兼容性。
  - 进展（2026-03-08）：已新增 `tests/test_repository_sync_status_exception_narrowing.py`，覆盖记录/清理同步错误成功路径、数据库异常与回滚失败兜底分支。
  - 进展（2026-03-08）：已新增 `tests/test_core_navigation_exception_narrowing.py`，覆盖路由探测异常、`url_for` 构建异常与首页模板异常兜底分支，验证 `core_navigation_handlers` 收敛后的返回契约。
  - 进展（2026-03-08）：已新增 `tests/test_small_service_exception_narrowing.py`，覆盖蓝图注册失败、数据库迁移执行失败回滚与 `commit_diff_new` 作者映射失败兜底分支。
  - 进展（2026-03-08）：已新增 `tests/test_repository_update_api_exception_narrowing.py`，覆盖仓库异步更新 worker 的已知异常兜底分支与同步错误记录行为。
  - 进展（2026-03-08）：已新增 `tests/test_app_request_logging_exception_narrowing.py`，覆盖 Agent 访问日志过滤（2xx 抑制/5xx保留/消息读取异常）与过滤器幂等注册分支。
  - 进展（2026-03-08）：已新增 `tests/test_app_bootstrap_db_exception_narrowing.py`，覆盖启动建表流程中的目录创建失败、表探测失败后继续创建、SQLite 诊断字节格式兜底与版本缓存清理回滚失败分支。
  - 进展（2026-03-08）：已新增 `tests/test_app_security_bootstrap_exception_narrowing.py`，覆盖 auth 模块导入失败回退、`public_login_url` 的 auth 优先/管理员登录回退/构建失败硬编码回退分支。
  - 进展（2026-03-08）：已新增 `tests/test_auth_bootstrap_exception_narrowing.py`，覆盖 qkit 路由探测失败回退注册、路由注册失败日志、auth 路由诊断失败兜底、`initialize_auth_subsystem` 的 ImportError/默认数据初始化异常/模块初始化异常分支。
  - 进展（2026-03-08）：已新增 `tests/test_repository_maintenance_api_exception_narrowing.py`，覆盖缓存重建失败、缓存状态查询失败、手动同步中 commit payload 缺字段触发的外层异常兜底与错误记录分支。
  - 进展（2026-03-08）：已新增 `tests/test_repository_update_form_exception_narrowing.py`，覆盖表单提交异常触发回滚、异步重筛中 `force_full_sync` 异常兜底日志与流程继续返回分支。
  - 进展（2026-03-08）：已新增 `tests/test_commit_diff_view_exception_narrowing.py`，覆盖作者映射失败回退、缓存数据处理异常回退与 Excel 主流程异常兜底分支。
  - 验收：新增针对性测试，不仅是 happy path。
- [x] 10. 统一服务层输入/输出模型（dataclass/pydantic）
  - 进展（2026-03-08）：已引入 `ErrorResponsePayload` / `SuccessResponsePayload`（dataclass），并作为 diff 相关服务统一输出模型。
  - 进展（2026-03-08）：已新增 `CommitDiffQueryInput` / `MergeDiffRefreshInput`（dataclass）并接入 `commit_operation_handlers`、`excel_diff_api_service` 的请求解析链路。
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
