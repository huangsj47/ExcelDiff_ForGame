# 配表代码版本 Diff 平台

面向配置/代码仓库的变更确认平台，支持 Git/SVN 提交采集、差异展示、确认流转、周版本聚合，以及平台 + Agent 分布式执行。

## 这是什么

这个平台解决的是“项目 -> 仓库 -> 提交 -> 差异 -> 确认”的完整链路，目标是：
- 让变更可视、可追踪、可确认
- 让 Excel/文本等高频差异场景可落地
- 让单机与分布式（平台 + 多 Agent）都能稳定运行

## 核心功能（重点）

1. 仓库接入与同步
- 支持 Git / SVN 仓库接入与同步
- 提供仓库管理、连接测试、手动同步、状态追踪

2. 提交与差异确认
- 提交列表查询、状态流转（待确认/已确认/已拒绝）
- 支持批量确认/拒绝
- 支持文件维度差异查看与追溯

3. Excel 差异与缓存体系
- Excel 差异渲染与缓存（Diff 缓存 + HTML 缓存）
- 大结果缓存与回传优化，降低重复计算

4. 周版本管理
- 周版本配置、自动同步、文件级确认
- 周版本统计与状态联动

5. 平台 + Agent 分布式执行
- 平台负责任务编排与落库
- Agent 负责仓库拉取、任务执行与结果回传
- 支持 Agent 节点监控、任务调度、故障重试

6. Agent 自更新能力
- 支持 release 包发布与 Agent 自动更新
- 支持一键回滚到上一 release 或指定版本

## Excel/CSV 单元格的比较口径（`DIFF_LOGIC_VERSION` 1.9.0）

**表里怎么写就怎么比。** Excel/CSV 一律**按文本原样读取**
（`dtype=str` + `keep_default_na=False`），不做类型推断、不做 NA 转换；
比较与展示都不 `strip`、不把看起来像空值的文本当空。

这意味着下面这些「屏幕上看着差不多、字面量其实不同」的改动**会被报成变更**：

| 表里怎么写 | 报不报变更 |
|---|---|
| `00123` → `123` | ✅ 报（前导零变了） |
| `1.10` → `1.1` | ✅ 报（尾零变了） |
| `TRUE` → `true` | ✅ 报（大小写变了） |
| `NULL` / `null` / `None`（文本） → 空 | ✅ 报 |
| `null` → `None` | ✅ 报（两个不同取值） |
| `'  x  '` → `'x'` | ✅ 报（首尾空格变了） |
| 清空一个单元格 | ✅ 报 |
| `123` → `123` | ❌ 不报 |

**1.9.0 之前这些全部不会报。** 那时 pandas 的默认读取会在比较之前就把
`00123`→`123`、`1.10`→`1.1`、`TRUE`→`True`，并把文本
`NULL`/`null`/`nan`/`N/A`/`None`/`<NA>` 转成 `NaN`（于是真实取值被当成空值）；
展示层又把文本 `null`/`None` 渲染成空串。对本平台（变更确认）来说这是最坏的失败
方式 —— 不是报错，而是把「有变更」显示成「没变更」，审核者看不到也就不会核对。

因此升级到 1.9.0 后 **diff 数量会比以前多**，这是预期的。旧版本的 diff 缓存会因
版本号变化自动失效并重算（见 `DIFF_LOGIC_VERSION`，`app.py` 与 `config.py` 各有一份
字面量，必须同时改，一致性由 `tests/test_diff_logic_version_single_source.py` 锁定）。

配套的展示层约定：`.excel-cell` 用 `white-space: pre-wrap`，**原样呈现空格并允许换行**。
这既让「空格差异」在界面上真的看得见（否则 HTML 会折叠空格，审核者会看到一行
「看不出差异」的变更行），也让超长的配置串不再被 `ellipsis` 静默截断。

## 账号与权限

支持两套后端：
- `AUTH_BACKEND=local`：本地账号密码体系
- `AUTH_BACKEND=qkit`：Qkit 登录体系

统一采用 RBAC 思路（平台管理员 / 项目管理员 / 普通用户）与项目级权限隔离。

## 部署模式

通过 `DEPLOYMENT_MODE` 控制：
- `single`：单机一体模式（默认）
- `platform`：平台控制面模式（推荐生产）
- `agent`：进程以 Agent 循环运行（多用于调试）

## 快速开始

1. 安装依赖
```bash
pip install -r requirements.txt
```

2. 准备配置
```bash
cp .env.simple .env
```
Windows:
```bat
copy .env.simple .env
```

3. 启动平台
Linux/macOS:
```bash
bash start.sh
```
Windows:
```bat
start.bat
```

4. 访问平台
- `http://127.0.0.1:8002`

## 平台 + Agent 最小落地路径（推荐）

1. 平台机：
- `.env` 设置 `DEPLOYMENT_MODE=platform`
- 配置统一 `AGENT_SHARED_SECRET`

2. Agent 节点机：
- 部署 `agent/` 目录
- 配置 `agent/.env` 中 `PLATFORM_BASE_URL`、`AGENT_SHARED_SECRET`、`AGENT_NAME`
- 启动 `start_agent.sh` 或 `start_agent.bat`

3. 平台侧确认节点在线：
- 管理页 `/admin/agents`

## Agent 发布与回滚（常用命令）

发布新版：
```bash
python scripts/publish_agent_release.py
```

回滚到上一版：
```bash
python scripts/publish_agent_release.py --rollback --rollback-steps 1
```

回滚到指定版：
```bash
python scripts/publish_agent_release.py --rollback --rollback-target-version <版本号>
```

独立回滚脚本：
```bash
python scripts/rollback_agent_release.py --steps 1
```

## 文档导航

- 平台配置与部署总说明：[`平台配置说明.md`](./平台配置说明.md)
- 代码架构与模块实现说明：[`代码架构说明.md`](./代码架构说明.md)
- Agent 独立运行说明：[`agent/README.md`](./agent/README.md)

## 你需要优先关注的配置项

- 平台：`AUTH_BACKEND` / `DEPLOYMENT_MODE` / `AGENT_SHARED_SECRET` / `FLASK_SECRET_KEY`
- Agent：`PLATFORM_BASE_URL` / `AGENT_SHARED_SECRET` / `AGENT_NAME`

## 代码质量工具（新增）

1. 安装开发依赖
```bash
pip install -r requirements-dev.txt
```

2. 运行增量 Ruff（仅检查改动文件）
```bash
python scripts/run_ruff_changed.py
```

3. 运行文件长度守卫
```bash
python scripts/check_file_length.py --strict
```

4. 启用 pre-commit
```bash
pre-commit install
pre-commit run --all-files
```

## 说明

当前 README 为“重点版”，用于快速理解与落地。详细参数、模式差异、发布回滚细节以 [`平台配置说明.md`](./平台配置说明.md) 为准。
