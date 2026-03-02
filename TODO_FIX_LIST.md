# 配表代码版本 Diff 平台 TODO 修复清单（精简版）

更新时间: 2026-03-03  
维护原则: 该文件仅保留“仍需继续实现”的任务；已完成或无需继续调整的内容已移除。

---

## 当前仍需推进

- 暂无阻塞性待办（P0/P1/P2 已清理完毕）。

---

## 本轮已落地并从待办移除

- 代码 Review 优化（2026-03-03）
  1) `ExcelDiffCacheService.is_excel_file` 扩展名识别不一致（遗漏 `.xlsb/.csv`）已修复。  
  2) 启动期 `clear_version_mismatch_cache` 使用 `all()+逐条 delete+sleep` 的低效清理逻辑已改为服务层批量清理。

- 日志解耦收尾（safe_print）
  `utils/safe_print.py` 不再运行时 `from app import ...`，改为仅读取 `sys.modules['app']`，避免隐式导入和循环依赖副作用。

- SQLAlchemy 2 兼容收尾（Auth 服务层）
  `auth/services.py` 已移除所有 `Query.get()`，统一改为 `db.session.get(...)`，避免 LegacyAPIWarning 持续堆积。

- #11 模型双轨定义收敛  
  `services/model_loader.py` 已改为：模型对象（`db` + ORM）统一以 `models` 为单一事实源，仅非模型运行时对象才回退 `app`。

- #25 周版本任务去重  
  已在 `create_weekly_excel_cache_task` 增加 `(config_id, file_path, task_type)` 在 `pending/processing` 下的去重检查。

- #26 周版本 Excel 合并策略  
  周版本 Excel diff 已优先复用 `merged_diff_data` 中真实合并/分段结果，不再固定“首尾提交”策略。

- #27 周版本缓存命中组合索引  
  `WeeklyVersionExcelCache` 已新增组合索引：`idx_weekly_excel_lookup`。

- #23 缓存清理 `query.all() + 逐条 delete`  
  已被既有批量删除优化覆盖，无需再单独保留。

## 2026-03-03 增量完成（本轮）
- 已修复：项目成员新增接口的 `role` 入参校验缺失，现仅允许 `admin/member`。
- 已补测：新增非法 `role` 的服务层与 API 回归测试，防止回归。

## 2026-03-03 增量完成（下一批）
- 已完成：SQLAlchemy2 兼容尾项，`app.py` 与 `tasks/weekly_sync_tasks.py` 残留 `.query.get()` 全部替换为 `db.session.get(...)`。
- 已补测：新增 app/tasks 兼容静态回归测试，防止后续引入旧 API。

## 2026-03-03 增量完成（再下一批）
- 已完成：项目成员新增逻辑加固，`add_user_to_project` 新增用户/项目存在性校验，避免异常关联数据。
- 测试结论：该变更会改动错误分支，必须补测。
- 已补测：新增服务层与 API 层“成员新增不存在用户/项目”回归用例并通过。

## 2026-03-03 增量完成（下一批）
- 已完成：加入项目申请逻辑加固，`request_join_project` 新增项目存在性校验，拒绝对不存在项目发起申请。
- 测试结论：该变更会改动请求失败分支，必须补测。
- 已补测：新增服务层与 API 层“加入不存在项目”回归用例并通过。
