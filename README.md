# 配表代码版本 Diff 平台

基于 Flask + SQLite 的版本变更确认平台，面向 Git/SVN 仓库的提交同步、差异分析、周版本追踪与确认流转。

## 项目定位

该平台用于管理"项目 -> 仓库 -> 提交 -> 差异确认"全链路，重点解决：

- 多仓库（Git/SVN）提交记录统一管理
- Excel/代码/图片/二进制文件的差异展示
- 变更状态审核（待确认/已确认/已拒绝）与批量操作
- 周版本（时间窗口）聚合对比与状态联动
- 缓存与后台任务体系（加速 Diff 计算，降低重复开销）
- **多租户账号与权限体系（RBAC）**：三级角色 + 项目隔离 + 审批工作流

主服务入口为 `app.py`，默认监听 `0.0.0.0:8002`。

## 功能全景

## 0. 账号与权限系统 (RBAC) 
平台支持两套账号后端，便于本地调试与线上 Qkit 登录并存。

### 0.0 双账号后端切换（local / qkit）

| 后端 | 环境变量 | 说明 |
|------|----------|------|
| **本地账号系统** | `AUTH_BACKEND=local` | 使用 `auth_*` 表，支持注册/改密/本地密码登录 |
| **Qkit账号系统** | `AUTH_BACKEND=qkit` | 使用 `qkit_auth_*` 表，登录入口 `/qkit_auth/login`，每请求远端 JWT 校验 |

切换规则：
- 两套账号数据表隔离，切换后只读取当前后端对应表，避免本地调试与 Qkit 数据混淆。
- Qkit 模式下屏蔽本地注册、改密、重置密码和本地登录页；仅保留用户管理与项目申请审批。

以下 RBAC 说明默认针对 `local` 后端；`qkit` 后端沿用同样的平台角色/项目角色语义。

### 0.1 三级角色模型

| 角色 | 标识 | 权限范围 |
|------|------|----------|
| **平台管理员** | `platform_admin` | 全局管理：用户管理、角色分配、审批所有申请、查看所有项目 |
| **项目管理员** | `project_admin` | 项目维度：管理所属项目成员、分配职能、查看项目数据 |
| **普通用户** | `normal` | 仅可访问已加入的项目，提交加入/创建项目申请 |

### 0.2 项目隔离

- 每位用户只能看到和访问 **自己所属的项目**
- 平台管理员可跨项目查看所有数据
- 项目成员通过 `auth_user_projects` 关联表管理

### 0.3 职能系统

预设 11 种职能，可按项目维度分配给用户：

| 职能 | 说明 | 特殊标记 |
|------|------|----------|
| **主QA✦** | 主测试 | ⚡ `is_lead_qa=True` — 自动提权为项目管理员 |
| QA | 测试 | — |
| 主策划 | 主策划 | — |
| 策划 | 策划 | — |
| 主程序 | 主程序 | — |
| 程序 | 程序 | — |
| 主美 | 主美术 | — |
| 美术 | 美术 | — |
| PM | 项目经理 | — |
| 其他 | 其他职能 | — |
| 管理员 | 管理员 | — |

**关键业务规则：** 当某用户被分配「主QA✦」职能到某项目时，系统自动将其在该项目中的角色提升为**项目管理员**；移除所有「主QA✦」后自动降级为普通成员。

### 0.4 审批工作流

1. **加入项目申请**：普通用户可在首页申请加入已有项目 → 平台管理员在用户管理页审批
2. **创建项目申请**：普通用户可申请创建全新项目 → 审批通过后自动创建项目，申请人成为项目管理员

### 0.5 认证页面

| 页面 | 路由 | 说明 |
|------|------|------|
| 登录（local） | `/auth/login` | 用户名 + 密码登录 |
| 登录（qkit） | `/qkit_auth/login` | 跳转 Qkit 登录服务并回调 `/qkit_auth/after_login` |
| 注册 | `/auth/register` | 自助注册，默认角色为普通用户 |
| 修改密码 | `/auth/change-password` | 已登录用户自行修改密码 |
| 用户管理 | `/auth/users` | 平台管理员专属：用户列表、角色变更、审批中心 |
| 项目成员管理 | `/auth/project/<id>/members` | 项目管理员专属：成员增删、导入配置与导入执行 |

> 在 `AUTH_BACKEND=qkit` 模式下：`/auth/register` 与 `/auth/change-password` 会被禁用并引导到 Qkit 登录流程。

### 0.6 安全机制

- **密码存储**：Werkzeug `pbkdf2:sha256` 哈希
- **CSRF 保护**：HMAC-SHA256 令牌校验，所有 POST/PUT/DELETE 请求自动拦截
- **会话管理**：Flask server-side session
- **输入校验**：用户名 ≥ 3 字符，密码 ≥ 6 字符
- **账户禁用**：管理员可禁用用户，禁用后无法登录

## 1. 项目管理

- 项目新增、列表、详情、删除
- 项目合并视图（左侧周版本，右侧仓库列表）
- 项目维度状态同步管理页面

## 2. 仓库管理（Git/SVN）

- 仓库新增（Git/SVN 双类型）
- 仓库编辑、删除、连接测试、克隆失败重试
- 仓库顺序调整（拖拽/交换）
- 按项目批量更新仓库凭据
- 复用本地仓库并触发更新/缓存

## 3. 提交记录管理

- 提交列表分页与筛选（作者、路径、版本、操作类型、状态）
- 单条提交状态更新
- 批量通过/拒绝
- “同一次提交”文件批量确认
- 文件维度提交历史查询

## 4. Diff 引擎与展示

- 统一差异服务（`DiffService`）支持：
  - 文本 Diff
  - Excel Diff
  - 图片 Diff
  - 二进制文件差异信息
- 提交 Diff 页面（新样式、全量并排视图）
- 合并 Diff（多提交聚合对比）
- 指定提交区间对比（同文件不同提交版本）

## 5. Excel 缓存体系

- 原始差异缓存（`DiffCache`）
- HTML 渲染缓存（`ExcelHtmlCache`）
- 高优先级 Diff 任务插队
- 缓存状态查询与重建
- 缓存策略管理、按项目统计、批量清理

## 6. 周版本管理（Weekly Version）

- 周版本配置 CRUD（支持按项目创建、按仓库或“全部仓库”创建）
- 按时间范围聚合周版本
- 周版本文件列表、文件级 Diff、完整对比、上一版本查看
- 文件状态确认与批量确认
- 周版本统计（总数/待确认/已确认/已拒绝）
- 周版本 Excel 缓存统计、清理、重建

## 7. 状态同步（提交 <-> 周版本）

- 提交状态变更后，自动同步到周版本文件状态
- 周版本文件状态变更后，反向同步到提交记录
- 同步映射查询（按配置/仓库/项目）
- 一键清空确认状态

## 8. 后台任务与定时调度

- 优先级后台任务队列（Excel Diff、周版本同步、缓存清理等）
- 启动时加载数据库中的待处理任务
- 定时任务：
  - 每日 `04:00` 缓存清理
  - 每 `2` 分钟检查周版本自动同步

## 9. 跨仓对比能力

- 仓库间提交差异对比（时间窗口 + 时间差阈值）
- 同文件不同提交版本对比

## 技术架构

## 核心栈

- Backend: `Python`, `Flask`, `Flask-SQLAlchemy`, `Flask-CORS`
- DB: `SQLite`（默认 `instance/diff_platform.db`）
- VCS: `GitPython` + 命令行 `git` / `svn`
- Excel: `openpyxl`, `pandas`, `xlrd`, `xlwt`
- Scheduler: `schedule`

## 分层结构

- `app.py`: 主应用与核心路由（当前主链路）
- `auth/`: 本地账号与权限模块（`AUTH_BACKEND=local`）
  - `models.py`: RBAC 数据模型（用户、职能、项目关联、审批申请）
  - `routes.py`: 认证/管理路由（`/auth/*`）
  - `services.py`: 业务逻辑（注册、审批、自动提权等）
  - `providers.py`: 认证提供者抽象（支持数据库用户 + `.env` 环境变量管理员）
  - `decorators.py`: 权限装饰器
  - `templates/`: 认证相关页面模板
- `qkit_auth/`: Qkit 账号后端（`AUTH_BACKEND=qkit`）
  - `models.py`: Qkit 专用数据表（`qkit_auth_*`）
  - `providers.py`: 每请求 JWT 远端校验 Provider
  - `routes.py`: `/qkit_auth/*` 登录回调 + `/auth/*` 管理/审批路由
  - `services.py`: 项目维度导入、冲突处理、权限锁定与增量同步逻辑
- `models/`: 数据模型（项目、仓库、提交、缓存、周版本、任务、操作日志）
- `services/`: Git/SVN 同步、Diff 计算、缓存、状态同步等服务
- `tasks/`: 后台任务与清理任务
- `routes/`: Flask Blueprint 路由
- `templates/`: 页面模板
- `static/`: 前端 JS/CSS
- `utils/`: 数据库、重试、时区、URL、请求安全辅助工具

## 目录说明

```text
.
├── app.py                    # 主应用入口
├── config.py                 # 配置文件
├── requirements.txt
├── auth/                     # 账号与权限模块
│   ├── __init__.py           # Provider 初始化
│   ├── models.py             # RBAC 数据模型
│   ├── routes.py             # 认证路由 Blueprint
│   ├── services.py           # 业务逻辑层
│   ├── providers.py          # 认证提供者（DB / ENV）
│   ├── decorators.py         # 权限装饰器
│   └── templates/            # 登录/注册/管理页面
├── qkit_auth/                # Qkit账号后端模块
│   ├── config.py             # Qkit接入配置读取
│   ├── models.py             # qkit_auth_* 数据模型
│   ├── providers.py          # Qkit认证提供者
│   ├── routes.py             # Qkit登录与管理路由
│   ├── services.py           # 导入/审批/成员管理逻辑
│   └── templates/            # Qkit管理页面模板
├── models/
├── services/
├── tasks/
├── routes/
├── templates/
├── static/
├── utils/
├── tests/
│   └── test_auth_e2e.py      # 账号系统端到端测试（62 用例）
├── instance/                 # SQLite 数据库目录（运行后生成）
├── repos/                    # 本地仓库工作目录
└── logs/runlog.log
```

## 快速开始

## 1. 环境准备

- Python 3.9+（建议 3.11/3.12）
- 本机可用 `git` 命令
- 若使用 SVN 仓库，需要安装 `svn` 命令

## 2. 安装依赖

```bash
pip install -r requirements.txt
```

Windows 可选：

```bat
install_deps.bat
```

## 3. 初始化数据库

```bash
python init_database.py
```

如果仅需重建数据库：

```bash
python recreate_db.py
```

## 4. 启动服务

```bash
python app.py
```

访问：

- `http://127.0.0.1:8002`

## 关键配置项

当前主要配置位于 `app.py` / `config.py`：

- `AUTH_BACKEND`（`local` / `qkit`）
- `SECRET_KEY`
- `SQLALCHEMY_DATABASE_URI`（默认 SQLite）
- `DIFF_LOGIC_VERSION`（用于缓存版本控制）
- `DEPLOYMENT_MODE`（`single`/`platform`/`agent`）
- `AGENT_SHARED_SECRET`（平台与 agent 通信密钥）
- `QKIT_LOCAL_HOST` / `QKIT_LOGIN_HOST`（Qkit 模式登录回调地址）
- `QKIT_PUBLIC_BASE_URL`（可选；反向代理场景下用于固定回调公网地址，如 `https://diff.example.com`）
- `QKIT_AUTH_CHECK_JWT_API`（Qkit 模式每请求 JWT 校验接口）
- `QKIT_REQUEST_TIMEOUT_SECONDS`（Qkit 接口超时，默认 5 秒）
- `QKIT_REDMINE_API_URL`（项目成员导入接口地址）
- 兼容旧变量名：`LOCAL_HOST` / `LOGIN_HOST` / `AUTH_CHECK_JWT_API`（优先读取 `QKIT_*`）
- 定时任务频率（每日清理 + 每 2 分钟周版本检查）
- `PERF_METRICS_MAX_EVENTS`（`/admin/performance` 事件窗口总容量，默认 `8000`）
- `PERF_METRICS_MAX_SCOPE_SHARE`（单分片最大占比，默认 `0.35`）
- `PERF_METRICS_MIN_SCOPE_EVENTS`（单分片软上限最小值，默认 `300`）

## 平台 + Agent 模式

- 平台新增接口：
  - `POST /api/agents/register`：Agent 注册；可选自动创建项目代号（若传入）
  - `POST /api/agents/heartbeat`：Agent 心跳上报
  - `GET /api/agents`：查看 Agent 状态（管理员）
  - `GET /admin/agents`：Agent 节点监控页（管理员）
  - `POST /api/agents/tasks/claim`：Agent 领取任务
  - `POST /api/agents/tasks/<task_id>/result`：Agent 回传任务结果
  - `GET /api/agents/tasks`：查看 Agent 任务状态（管理员）
  - `POST /api/agents/releases/latest`：Agent 查询最新 release
  - `GET /api/agents/releases/<version>/package`：Agent 下载 release 包
  - `GET /api/agents/releases/admin/list`：管理员查看 release 列表
  - `POST /api/agents/releases/admin/rollback`：管理员一键回滚 latest 到上一版/指定版
- 项目代号规则：
  - 不传/空：不创建项目代号
  - 不存在：平台自动创建项目
  - 已存在且已绑定当前 Agent：幂等通过
  - 已存在且被其他主体占用：返回冲突，不覆盖
- 独立 Agent 运行包位于 `agent/`，可单独打包分发：
  - `python agent/build_zip.py`
- 平台支持 Agent release 发布（自更新）：
  - `python scripts/publish_agent_release.py`
  - `python scripts/publish_agent_release.py --rollback --rollback-steps 1`（回滚到上一版）
  - `python scripts/rollback_agent_release.py --steps 1`（独立回滚脚本）
  - Windows: `scripts\\publish_agent_release.bat`
  - Linux/macOS: `bash scripts/publish_agent_release.sh`
  - 默认产物目录：`instance/agent_releases/`
  - 可用环境变量 `AGENT_RELEASES_DIR` 自定义目录
- `DEPLOYMENT_MODE=platform` 时，新增 `excel_diff/auto_sync/weekly_sync/weekly_excel_cache` 任务会下发到 `agent_tasks`。
- 平台管理员在 `platform/agent` 模式下创建项目时，可在首页选择绑定目标 Agent。
- 若某 Agent 配置了 `AGENT_DEFAULT_ADMIN_USERNAME`，对应用户可直接在平台创建项目：
  - 仅能绑定到自己被授权的 Agent；
  - 若被多个 Agent 同时授权，可在这些 Agent 中选择；
  - 平台不会自动删除历史项目/绑定，删除操作仅由管理员手工执行。
- 当前执行策略：
  - `auto_sync/excel_diff/weekly_sync/weekly_excel_cache/temp_cache_fetch`：统一由 Agent 本地执行（平台仅负责任务编排与结果落库）
- 相关 Agent 配置：
  - `AGENT_NAME`（建议必填）
  - `AGENT_CODE`（可留空，自动根据 `AGENT_NAME + AGENT_HOST` 生成）
  - `AGENT_PROJECT_CODES`（可留空）
  - `AGENT_DEFAULT_ADMIN_USERNAME`（历史累计写入，只新增不删除；可用于“按Agent授权创建项目”）
  - `AGENT_HEARTBEAT_INTERVAL_SECONDS`（心跳上报间隔）
  - `AGENT_REGISTER_RETRY_INTERVAL_SECONDS`（注册失败/鉴权失效重试间隔）
  - `AGENT_METRICS_INTERVAL_SECONDS=300`（CPU/内存/磁盘上报周期）
  - `AGENT_LOCAL_TASK_TYPES`（默认全量本地任务；保留为兼容配置）
  - `AGENT_TEMP_CACHE_THRESHOLD_BYTES`（支持表达式写法，如 `1*1024_1024`）
  - `AGENT_REPOS_BASE_DIR=agent_repos`

建议在生产环境替换 `SECRET_KEY`，并根据数据规模考虑迁移到 MySQL/PostgreSQL。

## 常用维护脚本

- `python check_db.py`: 检查数据库表结构与示例数据
- `python init_html_cache.py`: 初始化 HTML 缓存表
- `python incremental_cache_system.py`: 增量缓存/同步辅助脚本
- `python extract_diff_lines.py`: Diff 行提取调试脚本

## 常见接口分组（主应用）

以下为高频接口分组（非完整清单）：

- 项目与仓库
  - `GET/POST /projects`
  - `GET /projects/<project_id>/repositories`
  - `POST /repositories/git`
  - `POST /repositories/svn`
  - `POST /repositories/<repository_id>/sync`
- 提交与 Diff
  - `GET /repositories/<repository_id>/commits`
  - `GET /commits/<commit_id>/diff`
  - `GET /commits/<commit_id>/diff/new`
  - `GET /commits/<commit_id>/full-diff`
  - `POST /commits/<commit_id>/status`
  - `POST /commits/batch-approve`
  - `POST /commits/batch-reject`
- 周版本
  - `GET /projects/<project_id>/weekly-version-config`
  - `GET/POST /projects/<project_id>/weekly-version-config/api`
  - `GET/PUT/DELETE /projects/<project_id>/weekly-version-config/api/<config_id>`
  - `GET /weekly-version-config/<config_id>/files`
  - `POST /weekly-version-config/<config_id>/file-status`
- 缓存运维
  - `GET /api/excel-html-cache/stats`
  - `GET /api/excel-cache/stats-by-project`
  - `POST /admin/excel-cache/cleanup-expired`
  - `POST /admin/excel-cache/clear-all-diff-cache`
- 认证与用户管理
  - `GET/POST /auth/login` — 登录（`qkit` 模式下跳转到 `/qkit_auth/login`）
  - `GET /qkit_auth/login` — Qkit 登录入口（直接可访问）
  - `GET /qkit_auth/after_login` — Qkit 回调入口（接收 `qkitjwt`）
  - `GET /qkit_auth/logout` — Qkit 登出
  - `GET/POST /auth/register` — 注册（仅 `local` 模式）
  - `GET/POST /auth/change-password` — 修改密码（仅 `local` 模式）
  - `GET /auth/users` — 用户管理页（管理员）
  - `GET /auth/api/me` — 当前用户信息
  - `POST /auth/api/users/<id>/role` — 修改用户角色
  - `POST /auth/api/users/<id>/toggle-active` — 启用/禁用用户
  - `POST /auth/api/users/<id>/reset-password` — 重置密码（仅 `local` 模式；`qkit` 模式禁用）
  - `POST /auth/api/users/<id>/functions` — 分配职能（仅 `local` 模式）
  - `DELETE /auth/api/users/<id>/functions/<fid>` — 移除职能（仅 `local` 模式）
- 项目成员与审批
  - `GET /auth/project/<id>/members` — 项目成员管理页
  - `POST /auth/api/project/<id>/members` — 添加成员
  - `DELETE /auth/api/project/<id>/members/<uid>` — 移除成员
  - `POST /auth/api/project/<id>/members/<uid>/role` — 修改成员角色
  - `GET /auth/api/project/<id>/qkit-import-config` — 获取项目导入配置（qkit）
  - `POST /auth/api/project/<id>/qkit-import-config` — 保存项目导入配置（qkit）
  - `POST /auth/api/project/<id>/qkit-import` — 按项目增量导入用户（qkit）
  - `POST /auth/api/request-join-project` — 申请加入项目
  - `POST /auth/api/join-requests/<id>/handle` — 审批加入申请
  - `POST /auth/api/request-create-project` — 申请创建项目
  - `POST /auth/api/create-requests/<id>/handle` — 审批创建申请

## 测试

运行全部测试：

```bash
pytest
```

### 账号系统端到端测试 
```bash
python tests/test_auth_e2e.py
```

独立运行的 E2E 测试套件，使用临时 SQLite 数据库，覆盖 **62 个用例 / 11 个测试组**：

| # | 测试组 | 用例数 | 覆盖范围 |
|---|--------|--------|----------|
| 1 | 注册 / 登录 / 登出 | 8 | 正常流程 + 重复注册 + 密码不一致 + 错误密码 |
| 2 | 管理员登录与权限 | 4 | 管理员认证 + 用户管理页访问 + API 身份验证 |
| 3 | 密码修改 | 3 | 修改密码 + 新密码登录 + 旧密码失效 |
| 4 | 项目隔离 | 5 | 用户只能访问所属项目 + 管理员跨项目 |
| 5 | 项目加入申请 → 审批 | 5 | 提交申请 + 重复拒绝 + 审批通过 + 可见性验证 |
| 6 | 项目创建申请 → 审批 | 8 | 提交创建申请 + 审批 + 项目自动创建 + 申请人自动升为项目管理员 |
| 7 | 主QA 自动提权 | 6 | 分配主QA → 自动升级项目管理员 → 移除主QA → 自动降级 |
| 8 | 角色权限控制 | 6 | 普通用户越权拒绝 + 管理员操作验证 |
| 9 | 安全测试 | 6 | CSRF + SQL 注入 + XSS + 输入校验 |
| 10 | 边界条件 | 5 | 禁用用户 + 空输入 + 不存在资源 + 重复操作 |
| 11 | 项目成员管理 | 4 | 添加/重复/角色修改/移除成员 |

## 缓存机制与相关逻辑说明

### 1) Excel Diff 数据缓存（`DiffCache`）

- 缓存维度：`repository_id + commit_id + file_path`，并结合 `diff_version` 与 `cache_status` 控制命中。
- 命中条件：`cache_status='completed'` 且 `diff_version == DIFF_LOGIC_VERSION`。
- 生成流程：优先查缓存，未命中时实时计算 diff，并写入 `DiffCache`。
- 版本治理：旧版本缓存会被标记或清理，避免新旧 diff 逻辑混用。
- 保留策略：
  - 普通缓存按数量窗口保留（默认最大 1000 条，超出清理旧记录）。
  - 长耗时缓存（`is_long_processing=True`）可延长保留（默认约 90 天）。

### 2) Excel HTML 渲染缓存（`ExcelHtmlCache`）

- 目标：减少重复渲染开销，缓存渲染后的 `html/css/js`。
- 缓存键：`repository_id + commit_id + file_path + diff_logic_version`。
- 命中后直接返回渲染结果；未命中则按 diff 数据生成并入库。
- 清理策略：支持按版本清理、按过期时间清理和手动重建。

### 3) 周版本缓存（`WeeklyVersionDiffCache` / `WeeklyVersionExcelCache`）

- `WeeklyVersionDiffCache`：保存时间窗口内文件级合并 diff 元数据（基准/最新提交、提交列表、确认状态等）。
- `WeeklyVersionExcelCache`：保存周版本 Excel 合并 diff 的 HTML 结果。
- 生成判定：仅当对应最新提交对尚无可用 `WeeklyVersionExcelCache` 时才创建缓存任务，避免重复任务堆积。
- 状态联动：当检测到 `latest_commit_id` 变化时，会重置确认状态为 `pending`，确保新变更不被旧确认状态覆盖。

### 4) 维护与观测

- 提供缓存统计、按项目聚合统计、过期清理、全量清理、重建等管理接口。
- Excel 缓存日志接口已按 `source='excel_cache'` 过滤，避免混入非目标日志。
- 定时任务默认包含每日缓存清理及周版本同步检查。


## 数据库表概览（双账号后端）

### local 后端（`auth_` 前缀）

| 表名 | 说明 |
|------|------|
| `auth_users` | 用户信息（用户名、密码哈希、角色、启用状态） |
| `auth_functions` | 职能定义（主QA✦、QA、策划、程序、美术 等 11 种预设） |
| `auth_user_functions` | 用户-职能关联（支持按项目维度分配） |
| `auth_user_projects` | 用户-项目归属（含项目级角色 admin/member） |
| `auth_project_join_requests` | 项目加入申请 |
| `auth_project_create_requests` | 项目创建申请 |
| `auth_project_pre_assignments` | 预分配（邀请）记录 |

### qkit 后端（`qkit_auth_` 前缀）

| 表名 | 说明 |
|------|------|
| `qkit_auth_users` | Qkit映射用户（用户名=邮箱前缀） |
| `qkit_auth_user_projects` | Qkit项目成员关系（含项目角色与导入锁定标记） |
| `qkit_auth_project_join_requests` | 项目加入申请 |
| `qkit_auth_project_create_requests` | 项目创建申请 |
| `qkit_auth_project_pre_assignments` | 预分配记录 |
| `qkit_auth_project_import_configs` | 项目导入配置（token/host/project） |
| `qkit_auth_import_blocks` | 导入阻断记录（删除后防回流） |

运行时通过 `AUTH_BACKEND` 选择读取哪套账号表；两套数据彼此隔离，切换不会互相覆盖。
