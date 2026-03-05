# Agent 独立运行包

## 1. 配置
1. 复制 `.env.example` 为 `.env`
2. 按需修改以下最少配置：
   - `PLATFORM_BASE_URL`
   - `AGENT_SHARED_SECRET`
   - `AGENT_NAME`
   - `AGENT_PROJECT_CODES`（可留空）
   - `AGENT_DEFAULT_ADMIN_USERNAME`
   - `AGENT_LOCAL_TASK_TYPES`（默认 `auto_sync`）
   - `AGENT_METRICS_INTERVAL_SECONDS`（默认 300 秒）

## 2. 启动
```bash
pip install -r requirements.txt
python start_agent.py
```

说明：
- `AGENT_CODE` 可不配置，Agent 启动时会根据 `AGENT_NAME + AGENT_HOST` 自动生成唯一标识。
- `AGENT_PROJECT_CODES` 允许为空，空时不会自动创建任何项目代号。
- `AGENT_DEFAULT_ADMIN_USERNAME` 为历史累计写入策略（只新增，不自动删除）：
  - 每次 Agent 注册都会把该用户名写入数据库映射；
  - 该 Agent 已绑定/新绑定项目会追加该用户为项目管理员；
  - 若用户尚未注册，会先写入预分配，待其注册/登录后自动生效；
  - 即使 `AGENT_PROJECT_CODES` 为空，该用户也可在平台创建项目并默认绑定到该 Agent。
- Agent 会按配置周期上报 CPU/内存/磁盘/系统信息到平台。
- `AGENT_LOCAL_TASK_TYPES` 控制哪些任务在 Agent 本地执行：
  - 默认 `auto_sync`：仅仓库增量扫描在 Agent 本地执行；
  - `all`：`auto_sync/excel_diff/weekly_sync` 全部本地执行；
  - `none`：全部任务走平台 `execute-proxy`；
  - 逗号列表：如 `auto_sync,excel_diff`。
- `AGENT_ALLOW_EXECUTE_PROXY` 默认 `false`：
  - `false`：本地执行失败后不回退平台代理执行（更符合控制面/数据面拆分）；
  - `true`：允许旧路径回退到平台 `/execute-proxy`（过渡兼容）。

## 3. 打包分发
```bash
python build_zip.py
```
执行后会在当前目录输出 `agent_package_*.zip`，可直接发给其他用户部署。
