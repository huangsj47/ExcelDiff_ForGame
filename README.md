# 配表代码版本 Diff 平台（v2025.9.18）

基于 Flask + SQLite 的版本变更确认平台，面向 Git/SVN 仓库的提交同步、差异分析、周版本追踪与确认流转。

## 项目定位

该平台用于管理“项目 -> 仓库 -> 提交 -> 差异确认”全链路，重点解决：

- 多仓库（Git/SVN）提交记录统一管理
- Excel/代码/图片/二进制文件的差异展示
- 变更状态审核（待确认/已确认/已拒绝）与批量操作
- 周版本（时间窗口）聚合对比与状态联动
- 缓存与后台任务体系（加速 Diff 计算，降低重复开销）

主服务入口为 `app.py`，默认监听 `0.0.0.0:8002`。

## 功能全景

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
- `models/`: 数据模型（项目、仓库、提交、缓存、周版本、任务、操作日志）
- `services/`: Git/SVN 同步、Diff 计算、缓存、状态同步等服务
- `tasks/`: 后台任务与清理任务
- `routes/`: Blueprint/Express 路由（部分为兼容保留）
- `templates/`: 页面模板
- `static/`: 前端 JS/CSS
- `utils/`: 数据库、重试、时区、URL 辅助工具

## 目录说明

```text
.
├── app.py
├── config.py
├── requirements.txt
├── models/
├── services/
├── tasks/
├── routes/
├── templates/
├── static/
├── utils/
├── tests/
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

- `SECRET_KEY`
- `SQLALCHEMY_DATABASE_URI`（默认 SQLite）
- `DIFF_LOGIC_VERSION`（用于缓存版本控制）
- 定时任务频率（每日清理 + 每 2 分钟周版本检查）

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

## 测试

运行测试：

```bash
pytest
```

当前测试侧重结构与逻辑验证，业务全链路集成覆盖较少，建议在关键流程增加接口级与端到端用例。

## Node 兼容模块说明

仓库中保留了 `server.js` + `routes/*.js` 的 Express 版本实现，用于历史兼容/过渡。

- 主链路已迁移到 Python Flask（`app.py`）
- `package.json` 当前标注为废弃状态
- 新功能开发建议统一在 Python 主服务侧进行

## 已知现状

- `app.py` 体量较大（单体路由集中），后续可继续拆分到 Blueprint
- 默认无登录鉴权模块，部署到内网/公网前需补充访问控制
- SQLite 在高并发写入下存在瓶颈，建议中长期迁移数据库
