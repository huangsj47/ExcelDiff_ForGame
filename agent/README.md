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
- Agent 会按配置周期上报 CPU/内存/磁盘/系统信息到平台。
- `auto_sync` 默认在 Agent 本地执行（拉取仓库日志），结果回传平台入库。
- 未列入 `AGENT_LOCAL_TASK_TYPES` 的任务会走平台 `execute-proxy` 过渡执行。

## 3. 打包分发
```bash
python build_zip.py
```
执行后会在当前目录输出 `agent_package_*.zip`，可直接发给其他用户部署。
