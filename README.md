# 配表代码版本 Diff 平台

🎯 面向**配置 / 代码仓库**的变更确认平台：Git/SVN 提交采集 → 差异展示 → 逐级确认 → 周版本聚合，
并支持**平台 + Agent** 分布式执行与 **AI 变更风险分析**。

给谁用：想知道「这周改了什么、要不要回归、该测哪儿」的**策划 / QA / 版本管理员**，
以及需要把这条链路接进自己项目流程的**部署与运维同学**。

## 核心功能（重点）

🚀 **六项基础能力 + 一项 Beta 能力：**

1. 🗂️ **仓库接入与同步**
   - Git / SVN 仓库接入与同步、连接测试、手动同步、状态追踪

2. ✅ **提交与差异确认**
   - 提交列表查询、状态流转（待确认 / 已确认 / 已拒绝）、批量确认与拒绝
   - 文件维度的差异查看与追溯

3. 📊 **Excel 差异与缓存体系**
   - Excel 逐单元格渲染与对比（「表里怎么写就怎么比」的口径见
     [代码架构说明.md](./docs/代码架构说明.md) 第 3.4 节）
   - Diff 缓存 + HTML 缓存、大结果回传优化，避免重复计算

4. 🗓️ **周版本管理**
   - 周版本配置、自动同步、文件级确认、统计与状态联动

5. 🛰️ **平台 + Agent 分布式执行**
   - 平台负责任务编排与落库；Agent 负责仓库拉取、任务执行与结果回传
   - Agent 节点监控、任务调度、故障重试

6. 🔄 **Agent 自更新能力**
   - release 包发布与 Agent 自动更新，一键回滚到上一 release 或指定版本

7. 🤖 **AI 变更风险分析（Beta）**
   - 对**周版本**或**单次提交**产出面向回归的风险说明：影响面分析、**测试建议**、
     **回归建议**、上线与回滚关注点。模型**初始只拿到变更清单**（提交信息 + 文件路径），
     diff 正文要它自己点名索取（五个只读工具，一轮要 4~8 个文件），证据按门槛过滤后落库
   - 抽屉里有「**完整结论**」与「**思考过程**」两个标签，能**导出 md**、能**翻历次结论**；
     增量分析会带上「上次报过的问题」，要求标注仍成立 / 已修复 / 已被推翻
   - 消耗可查：顶部导航 →「**AI 消耗**」（token、缓存命中率、工具调用与费用估算）
   - 📖 面向测试同学的完整说明（含它**看不到什么**）：
     [AI分析使用说明.md](./docs/AI分析使用说明.md)

## ⚡ 快速开始

**1. 📦 安装依赖**（**Python ≥ 3.9**；CI 跑 3.11）

```bash
pip install -r requirements.txt
```

> 🗄️ **数据库不用准备**：默认 SQLite（`instance/diff_platform.db`，启动时自动建表）。
> 要换 MySQL 再设 `DATABASE_URL`，见 [平台配置说明.md](./docs/平台配置说明.md)。
>
> 🧪 **要跑测试 / lint** 另装：`pip install -r requirements-dev.txt`
> （测试约 6000 条，并发跑：`python -m pytest -n 8 --dist loadfile`；门禁与 CI 见
> [代码架构说明.md](./docs/代码架构说明.md) 第 7.2 节）。

**2. ⚙️ 准备配置**

```bash
cp .env.simple .env          # Windows: copy .env.simple .env
```

> ⚠️ **`.env.simple` 是模板，复制后必须替换两个密钥，否则平台不会启动。**
>
> `FLASK_SECRET_KEY` 与 `AGENT_SHARED_SECRET` 在模板里是占位串
> （形如 `__REPLACE_ME_WITH_A_RANDOM_...__`），不是可用密钥。生成随机密钥
> （两个键各生成一次，不要复用同一个值）：
>
> ```bash
> python -c "import secrets;print(secrets.token_urlsafe(48))"
> ```
>
> 填入 `.env`；Agent 节点机的 `AGENT_SHARED_SECRET` 必须与平台侧完全一致。
>
> 校验有**两道门**（启动脚本 + 进程入口）、各自的判定标准、以及本地调试的临时放行办法，
> 见 [平台配置说明.md](./docs/平台配置说明.md) 第 1.2 节。
>
> 🔑 **还要设 `ADMIN_PASSWORD`**（模板里是**空的**）。它不是密钥，上面那两道门都**不检查它**
> —— 但它是你登录平台的唯一凭据：**留空则平台照常启动，却没有任何账号能登录**。
>
> 💡 省事的话**跳过本步直接跑 `start.sh`**：`.env` 不存在时脚本会自动生成一个并**把
> `ADMIN_USERNAME` / `ADMIN_PASSWORD` 打印出来**。反过来，先手动建了 `.env`，这条自动
> 生成的路就不会走了。

**3. ▶️ 启动平台**

```bash
bash start.sh                # Windows: start.bat
```

**4. 🌐 访问平台** —— `http://127.0.0.1:8002`

## 🧭 你该读哪份文档

| 文档 | 面向谁 | 里面有什么 |
|---|---|---|
| [AI分析使用说明.md](./docs/AI分析使用说明.md) | 🧪 测试同学 / 策划 | AI 分析怎么用、报告怎么读、**它看不到什么** |
| [平台配置说明.md](./docs/平台配置说明.md) | 🛠️ 部署与运维 | `.env` 每个键、账号与权限、运行模式、发布与回滚、接口调用方式 |
| [代码架构说明.md](./docs/代码架构说明.md) | 💻 二次开发 | 架构图、调用链路、目录与模块、数据层、CI 门禁、技术债 |
| [agent/README.md](./agent/README.md) | 🛰️ Agent 节点 | Agent 的单独部署与运行 |

## 🔧 你需要优先关注的配置项

- **平台**：`AUTH_BACKEND` / `DEPLOYMENT_MODE` / `AGENT_SHARED_SECRET` / `FLASK_SECRET_KEY` / `ADMIN_PASSWORD`
- **Agent**：`PLATFORM_BASE_URL` / `AGENT_SHARED_SECRET` / `AGENT_NAME`

> 账号体系（`local` / `qkit`）与三种运行模式的差异，见
> [平台配置说明.md](./docs/平台配置说明.md) 第 2 / 3 节。

## 🤝 平台 + Agent 最小落地路径（推荐）

1. 🖥️ **平台机**：`.env` 设 `DEPLOYMENT_MODE=platform`，配置统一的 `AGENT_SHARED_SECRET`
2. 🛰️ **Agent 节点机**：部署 `agent/` 目录，在 `agent/.env` 配 `PLATFORM_BASE_URL` /
   `AGENT_SHARED_SECRET` / `AGENT_NAME`，启动 `start_agent.sh`（Windows 用 `start_agent.bat`）
3. ✅ **平台侧确认节点在线**：管理页 `/admin/agents`

### 📦 发布与回滚（常用命令）

```bash
python scripts/publish_agent_release.py                                 # 发布新版
python scripts/publish_agent_release.py --rollback --rollback-steps 1   # 回滚到上一版
python scripts/publish_agent_release.py --rollback --rollback-target-version <版本号>
python scripts/rollback_agent_release.py --steps 1                      # 独立回滚脚本
```

> 🔒 Agent 自更新带**三条 fail-closed 安全规则**：`version` 必须是单一安全路径段、
> 下载地址必须与 `PLATFORM_BASE_URL` 同源、`package_sha256` 必填且校验；
> 发布清单不满足即**拒绝安装**。细节见
> [平台配置说明.md](./docs/平台配置说明.md) 第 8 节。

## 🗺️ 后续优化方向

- 🤖 **AI 分析**：异常清单的结构化渲染（数据层已具备，见
  [AI分析使用说明.md](./docs/AI分析使用说明.md) 第 4 节）。
- 🧩 **AI 框架拓展**：参考 <https://github.com/alibaba/open-code-review.git> 。
