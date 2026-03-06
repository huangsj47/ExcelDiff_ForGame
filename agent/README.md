# Agent 独立运行包

## 1. 配置
1. 复制 `.env.example` 为 `.env`
2. 按需修改以下最少配置：
   - `PLATFORM_BASE_URL`
   - `AGENT_SHARED_SECRET`
   - `AGENT_NAME`
   - `AGENT_PROJECT_CODES`（可留空）
   - `AGENT_DEFAULT_ADMIN_USERNAME`
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
- `AGENT_LOCAL_TASK_TYPES`（可选高级项）控制哪些任务在 Agent 本地执行：
  - 默认全量本地执行：`auto_sync/excel_diff/weekly_sync/weekly_excel_cache/temp_cache_fetch`；
  - 当前版本会强制补齐上述任务类型，建议保持默认不配置。
- `AGENT_TEMP_CACHE_THRESHOLD_BYTES` 支持表达式写法，例如：`1*1024_1024`。
- Agent 支持 release 包自更新（默认开启）：
  - 定时查询平台 `/api/agents/releases/latest`；
  - 有新版本时下载 `/api/agents/releases/<version>/package`；
  - 校验 SHA256 后覆盖 Agent 目录并自动重启进程。
- 自更新配置项：
  - `AGENT_AUTO_UPDATE_ENABLED`（默认 `true`）
  - `AGENT_AUTO_UPDATE_CHECK_INTERVAL_SECONDS`（默认 `300`）
  - `AGENT_AUTO_UPDATE_REQUEST_TIMEOUT_SECONDS`（默认 `15`）
  - `AGENT_AUTO_UPDATE_DOWNLOAD_TIMEOUT_SECONDS`（默认 `120`）
  - `AGENT_AUTO_UPDATE_INSTALL_DEPS`（默认 `true`）
  - `AGENT_AUTO_UPDATE_PIP_TIMEOUT_SECONDS`（默认 `900`）
- 依赖安装行为说明（重要）：
  - `AGENT_AUTO_UPDATE_INSTALL_DEPS=true` 时，Agent 使用“当前运行解释器”执行 `python -m pip install -r requirements.txt`；
  - 使用 venv 启动时，依赖会安装到该 venv；
  - 使用系统 Python 启动时，依赖会安装到系统环境（可能需要管理员权限，且不推荐生产这样做）。

## 3. 打包分发
```bash
python build_zip.py
```
执行后会在当前目录输出 `agent_package_*.zip`，可直接发给其他用户部署。

## 4. 平台发布 release（推荐）
在平台项目根目录执行：
```bash
python scripts/publish_agent_release.py
```
可选参数：
- `--version <版本号>`
- `--notes "<发布说明>"`
- `--force`（覆盖同版本）
- `--allow-dirty`（允许未提交改动时发布，不建议）
- `--rollback --rollback-steps 1`（回滚 latest 到上一版）
- `--rollback --rollback-target-version <版本号>`（回滚到指定版本）

也可使用独立回滚脚本：
```bash
python scripts/rollback_agent_release.py --steps 1
```

脚本包装：
- Windows：`scripts\\publish_agent_release.bat` / `scripts\\rollback_agent_release.bat`
- Linux/macOS：`bash scripts/publish_agent_release.sh` / `bash scripts/rollback_agent_release.sh`
