# 配表代码版本 Diff 平台 TODO 修复清单（持续更新）

更新时间: 2026-03-02（最近更新: #28-#40 批量修复 + 集成测试 + app.py空行清理 + 启动脚本）
范围: app.py / services/* / models/* / static/* / templates/* / tests/*

状态说明:
- [x] 已完成
- [ ] 待完成（含部分完成）

---

## P0（立即修复）

- [x] 1) 高危写接口鉴权与授权（RBAC）
- [x] 2) CORS 收敛与 Secret Key 环境化
- [x] 3) 凭据泄漏链路治理（存储/日志/页面）
- [x] 4) CSRF 防护（表单 + AJAX + API）
- [x] 5) 仓库名与本地路径安全约束

## P1（近期高优先）

- [x] 6) 前后端 API 契约统一（批量更新/状态字段兼容）
- [x] 7) GitService URL 字段命名不一致修复
- [x] 8) 异步线程应用上下文与会话访问修复
- [x] 9) 周版本合并 diff 占位逻辑替换为真实策略
- [x] 10) 失效状态接口治理（旧 diff-status 接口降级）
- [ ] 11) 模型双轨定义彻底收敛（已完成 app-first 动态加载，仍保留双轨定义，需最终单一事实源）

## P2（性能与稳定性优化）

- [x] 12) 提交列表重复 count 查询削减
- [x] 13) 分页参数限流（per_page 上限）
- [x] 14) 项目级缓存统计 N+1 查询优化（聚合查询）
- [x] 15) 分支探测改异步任务，降低页面阻塞
- [x] 16) 线程池配置一致性与懒初始化
- [x] 17) 清理接口批量删除优化（避免循环 + sleep）

---

## Excel Diff / 缓存专项（2026-03-02 本轮新增）

- [x] 18) 逻辑 Bug：周版本缓存更新时确认状态重置判断失效
  - 问题: 更新 `latest_commit_id` 后再比较，条件恒不成立，导致新提交不会触发状态重置。
  - 修复: 先保存 `previous_latest_commit_id` 再比较。

- [x] 19) 逻辑 Bug：`/api/excel-cache/logs` 未按 `source='excel_cache'` 过滤
  - 问题: 混入非 Excel 缓存日志，分页与统计不准确。
  - 修复: 改为基于 `logs_query = OperationLog.query.filter_by(source='excel_cache')` 查询。

- [x] 20) 性能优化：`needs_merged_diff_cache` 不再对 Excel 文件近似恒返回 True
  - 问题: 会反复创建重复周版本 Excel 缓存任务，造成队列与 DB 负载。
  - 修复: 基于最新 `WeeklyVersionDiffCache` + 已存在 `WeeklyVersionExcelCache` 判定是否需要生成。

- [x] 21) 逻辑 Bug：周版本 Excel 文件识别遗漏 `.xlsm/.xlsb`
  - 问题: 周版本缓存服务仅识别 `.xlsx/.xls/.csv`，会漏掉宏表和二进制表，导致缓存任务不生成。
  - 修复: 扩展 `is_excel_file` 支持 `.xlsm/.xlsb`。

- [x] 22) 性能优化：`ExcelDiffCacheService.get_cached_diff` 中 `db.session.expire_all()` 全局失效开销较高
  - 建议: 改为定向失效或在明确需要时刷新，避免每次命中检查都触发会话全量失效。
  - 修复: 改为查询级 `populate_existing()` 刷新，移除会话级 `expire_all()`。

- [ ] 23) 性能优化：缓存清理路径中 `query.all() + 逐条 delete` 内存和事务压力较大
  - 涉及: `DiffCache`、`ExcelHtmlCache`、`WeeklyVersionExcelCache` 清理逻辑。
  - 建议: 改为分批主键删除或数据库侧批量删除策略。

- [x] 24) 性能优化：`ExcelHtmlCacheService.get_cache_statistics*` 通过 `.all()` 遍历计算体积
  - 建议: 使用 SQL 聚合（`sum(length(...))`）替代 Python 全量遍历。
  - 修复: `get_cache_statistics` 与 `get_cache_statistics_by_repositories` 改为 `sum(length(...))` 聚合统计。

- [ ] 25) 稳定性优化：`create_weekly_excel_cache_task` 缺少任务去重
  - 建议: 入队前检查同 `(config_id, file_path, task_type)` 的 `pending/processing` 任务，避免重复积压。

- [ ] 26) 逻辑优化：周版本 Excel 合并 diff 当前主要基于首尾提交
  - 风险: 多提交场景可能丢失中间语义变化。
  - 建议: 引入真正的多段合并策略，与 `generate_merged_diff_data` 的分段思路对齐。

- [ ] 27) 索引优化：周版本 Excel 缓存命中查询缺少组合索引
  - 查询模式: `(config_id, file_path, base_commit_id, latest_commit_id, diff_version, cache_status)`
  - 建议: 增加组合索引，降低热路径查询成本。

---

## 深度审查新增（2026-03-02 代码审计补充）

### 🔴 P0 — 逻辑正确性 / 运行时崩溃

- [x] 28) Bug：`ExcelHtmlCacheService.save_html_cache` 上下文泄露导致 DetachedInstanceError
  - 文件: `services/excel_html_cache_service.py` L74-113
  - 问题: `with flask_app.app_context()` 块结束后（L85），代码在上下文外继续修改 ORM 对象 (`existing_cache.html_content = ...`)，可能触发 SQLAlchemy `DetachedInstanceError`。`get_cached_html` (L35-47) 同样存在：`with` 块内查询，块外读取 `cache_record` 属性。
  - 修复: 所有 return/commit/rollback 统一移入 `with app_context()` 块内；`created_at` 转 str() 避免延迟加载；rollback 加异常保护。

- [x] 29) Bug：大数据集（>100行）Excel Diff 准确性严重不足
  - 文件: `services/diff_service.py` (`_fast_row_matching` / `_find_position_based_matches`)
  - 修复: `search_range` 改为 `max(10, int(data_size * 0.1))`，自适应搜索范围；对未哈希命中的行进行二次位置匹配。

- [x] 30) Bug：`processing_commits` 集合非线程安全
  - 修复: 在 `ExcelDiffCacheService` 和 `WeeklyExcelCacheService` 中引入 `threading.Lock()` 保护 `set` 操作。

### 🟡 P1 — 性能热路径

- [x] 31) 性能：`_calculate_row_similarity` 热路径中重复创建 `normalize_value` 闭包
  - 修复: 移除冗余闭包创建，`normalize_value` 统一为类级别复用。

- [x] 32) 性能：`_quick_similarity_check` 预检阈值过低，形同虚设
  - 修复: 预检条件改为 `matching_key_cols >= max(1, len(key_columns) // 2)`，与注释语义对齐。

- [x] 33) 性能：`save_cached_diff` 保存后执行无意义的验证查询
  - 修复: 已移除 commit() 后的冗余验证查询。

- [x] 34) 性能：`_cleanup_old_logs` 在每次日志写入时都触发
  - 修复: 改为计数器触发（每 50 次写入清理一次），并使用子查询批量 DELETE。

- [x] 35) 性能：`_cleanup_old_cache` 使用 `.offset().all()` 全量加载 + 逐条删除
  - 修复: 改为子查询批量 DELETE：`DiffCache.query.filter(DiffCache.id.in_(subquery)).delete(synchronize_session=False)`。

### 🟡 P2 — 可维护性 / 安全加固

- [x] 36) 安全：缓存 key 使用 MD5 哈希
  - 文件: `services/excel_html_cache_service.py` L27-28, `services/weekly_excel_cache_service.py` L29-30
  - 问题: MD5 碰撞容易构造。虽然此处不是密码场景，但恶意用户若能控制 `file_path` 或 `commit_id`，理论上可构造哈希碰撞覆盖他人缓存。
  - 修复: `ExcelHtmlCacheService` 已改用 `hashlib.sha256`（其余服务已在前轮修复）。

- [x] 37) 性能：`_calculate_row_hash` 仅使用前5列，碰撞风险高
  - 修复: 扩展为使用全部列计算哈希；空行返回特殊标记 `__EMPTY_ROW__` 而非 `0`。

- [x] 38) 性能：`get_runtime_models` 在每个方法中频繁调用
  - 修复: 在 `ExcelHtmlCacheService` 和 `WeeklyExcelCacheService` 中引入 `_models_cache` / `_get_model` 缓存机制，首次调用后缓存模型引用。

- [x] 39) 逻辑：`cleanup_old_cache` 执行 3 次独立全表查询后内存去重
  - 修复: 合并为一条 `or_()` 查询：`DiffCache.query.filter(or_(time_cond, status_cond, version_cond)).delete(synchronize_session=False)`。

- [x] 40) 数据安全：`diff_data` 存储为 `db.Text` 无大小限制
  - 修复: `save_cached_diff` 中序列化后检查大小（上限 10MB = `MAX_DIFF_DATA_BYTES`），超限时替换为只含 stats/headers 的摘要数据。

---

## 建议执行顺序（更新版）

### 第一优先级（影响正确性和稳定性，立即修复）：
1. **#28** — `save_html_cache` 上下文泄露 → 直接导致运行时崩溃
2. **#29** — 大数据集 diff 准确性 → 核心功能正确性
3. **#30** — `processing_commits` 线程安全 → 并发数据竞争

### 第二优先级（性能热路径优化）— ✅ 全部完成：
4. ~~**#23** — 缓存清理批量删除~~ → 部分由 #35 覆盖
5. ~~**#35** — `_cleanup_old_cache` 子查询批量 DELETE~~
6. ~~**#33** — `save_cached_diff` 冗余验证查询~~
7. ~~**#34** — 日志写入计数器触发~~
8. ~~**#31** — `normalize_value` 闭包热路径~~
9. ~~**#32** — `_quick_similarity_check` 阈值修正~~

### 第三优先级（中期改善）— 大部分完成：
10. **#25** — 周版本任务去重（待完成）
11. **#27** — 周版本组合索引（待完成）
12. ~~**#39** — `cleanup_old_cache` 合并 OR 查询~~
13. ~~**#37** — 哈希碰撞风险修复~~
14. ~~**#38** — `get_runtime_models` 模型缓存~~

### 第四优先级（长期改善）— 大部分完成：
15. **#26** — 多段合并策略（待完成，变更面大）
16. **#11** — 模型双轨收敛（待完成）
17. ~~**#36** — MD5 → SHA256~~
18. ~~**#40** — diff_data 大小限制~~

---

## app.py 拆分计划（按顺序执行）

- [x] A1) 拆分前基线校验与风险约束
  - 目标: 固化当前行为，避免拆分引入回归。
  - 动作: 补充/确认关键静态断言与核心回归测试入口。

- [x] A2) 提取纯工具函数模块（无 Flask 上下文依赖）
  - 范围: `clean_json_data`、`validate_excel_diff_data`、`safe_json_serialize`、`get_excel_column_letter`、`format_cell_value`。
  - 结果: `app.py` 改为导入使用，行为保持一致。

- [x] A3) 提取 ExcelDiffCacheService 到 `services/` 独立模块
  - 要求: 通过依赖注入或运行时模型加载，避免循环依赖。

- [x] A4) 提取安全与请求校验逻辑到 `utils/request_security.py`
  - 范围: 管理员鉴权、CSRF token 读取与同源校验等纯逻辑函数。

- [x] A5) 拆分缓存管理路由为 Blueprint
  - 范围: `/admin/excel-cache/*`、`/admin/weekly-excel-cache/*`、`/api/excel-*cache*` 等。
  - 进度: 已完成全部缓存管理路由迁移到 `routes/cache_management_routes.py`，`app.py` 中对应路由定义已移除。

- [x] A6) 拆分周版本路由为 Blueprint
  - 范围: `weekly-version-config` 相关接口及辅助函数。
  - 进度: 已新增 `routes/weekly_version_management_routes.py` 并迁移周版本路由注册；通过 `app.register_blueprint(..., name="")` 保持原 endpoint 命名兼容。

- [x] A7) 拆分提交/差异路由为 Blueprint
  - 范围: commits、diff、batch status 相关接口。
  - 进度: 已新增 `routes/commit_diff_routes.py` 并迁移提交/差异/批量状态相关路由注册，`app.py` 中对应 `@app.route` 已移除；通过 `app.register_blueprint(..., name=\"\")` 保持旧 endpoint 兼容。

- [x] A8) 收敛为应用装配层（主入口仅保留初始化与注册）
  - 目标: `app.py` 以 app/db 初始化、模型定义与 blueprint 注册为主。
  - 进度: 已新增 `routes/core_management_routes.py` 承接剩余核心路由注册；`app.py` 中 `@app.route(...)` 已全部迁移为蓝图注册。
