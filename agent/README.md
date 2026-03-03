# Agent 独立运行包

## 1. 配置
1. 复制 `.env.example` 为 `.env`
2. 按需修改以下最少配置：
   - `PLATFORM_BASE_URL`
   - `AGENT_SHARED_SECRET`
   - `AGENT_CODE`
   - `AGENT_PROJECT_CODES`
   - `AGENT_DEFAULT_ADMIN_USERNAME`
   - `AGENT_LOCAL_TASK_TYPES`（默认 `auto_sync`）

## 2. 启动
```bash
pip install -r requirements.txt
python start_agent.py
```

说明：
- `auto_sync` 默认在 Agent 本地执行（拉取仓库日志），结果回传平台入库。
- 未列入 `AGENT_LOCAL_TASK_TYPES` 的任务会走平台 `execute-proxy` 过渡执行。

## 3. 打包分发
```bash
python build_zip.py
```
执行后会在当前目录输出 `agent_package_*.zip`，可直接发给其他用户部署。
