# 本地起一个小仓库，把「全量 / 超预算 / 增量」三条分析路径各跑一遍

对着**正在运行的平台实例**做端到端验证：造一个很小的本地 git 仓库当被测数据，注册进去，
然后按顺序跑全量分析、分片上下文超预算、增量分析，看过程有没有问题、token 花在哪。

它补的是单测补不上的那一段：单测用假 provider 能证明「代码按设计走」，**证明不了**
真模型、真分片、真队列、真同步跑起来是不是这样。

## 文件

| 文件 | 干什么 |
|---|---|
| `make_fixture.py` | 造数。`build` 建第一轮的仓库，`round2`/`round3` 在主分支上继续提交 |
| `drive.py` | 驱动平台的 HTTP 接口（建仓库 / 同步 / 建周版本配置 / 触发分析），token 走 `X-Admin-Token` |
| `phases.py` | 三阶段的编排：隔离窗口 → 调预算 → 推提交 → 等同步 → 触发分析 → 读数 |
| `report.py` | 读一次运行的结果：分片有没有真跑、模型调用了几次、增量给了什么基准 |

生成物（裸库 `origin.git` + 工作副本 `gitsrc`）落在 `.pytest_tmp/e2e/`，**不进库**。

## 造数设计（每一处都对应一个要验的判据）

- `config/物品表.xlsx` —— 表头在第 1 行（最常见的形态）
- `config/技能表.xlsx` —— **表头在第 3 行**（上面两行是标题/说明），走「特殊表头」那条路
- `src/battle_logic.py` —— 代码正文那条路。里面埋**两个真问题**：
  技能等级 50 时蓝耗归零、51 级起跳回满额；毫秒换秒用整除、不足 1000 毫秒得 0
- 第二轮的其中一个提交是**回填日期**的（`GIT_AUTHOR_DATE` 设成比上一条更早），专治
  `git log --since` 遇到更旧的 tip 会停住整个遍历这件事

`url` 必须是**裸库**：平台会对它 `git fetch` + `checkout` + `pull`，失败后还有一条自愈
路径会 `reset --hard`，而 `force_reclone` 更会直接把目录删掉重来。

## 前置

- 平台在 `127.0.0.1:8002` 跑着（脚本打的是**运行中那个进程的内存队列**；另起一个进程
  enqueue 送不到它的 worker，`load_pending_tasks` 只在启动时读一次库）
- 仓库根 `.env` 里有 `ADMIN_API_TOKEN`（脚本只把它放进请求头，不落盘、不回显）
- 项目 id 在 `drive.PROJECT_ID` 里改

## 怎么跑

```bash
python scripts/e2e_local_repo/make_fixture.py build     # 造第一轮并推到裸库
python scripts/e2e_local_repo/drive.py create-repo      # 注册成平台仓库（会打印 id）
python scripts/e2e_local_repo/drive.py sync <repo_id>   # 手工同步一次（也可等后台调度）
python scripts/e2e_local_repo/drive.py config <repo_id> # 建周版本配置
python scripts/e2e_local_repo/phases.py overbudget      # 阶段二：降预算跑全量
python scripts/e2e_local_repo/phases.py incremental     # 阶段三：推提交再跑增量
python scripts/e2e_local_repo/report.py <run_id>        # 读某一次运行的结果
```

## 三个坑（都踩过）

1. **配置的窗口必须在未来**。窗口一过，调度器把它置 `completed` 并 `continue`
   （`services/task_worker_service.py:1450`），之后**再也不产生同步任务**；而更新分支
   不会把 status 改回 `active`，所以「把过期周版本顺延一周」是救不回来的 —— 实测
   00:06 就踩到了（`end_time` 写的当天 23:59）。
2. **分析范围不是触发的那条配置**。`services/ai_analysis_service.py:365-369` 取的是
   「同项目 + 同 start/end」的**全部**配置，所以新配置会和同窗口的老配置共用一个输入集
   —— 想让被测仓库独占，窗口得挑一段没别人用的。
3. **同值 PUT 不会重建缓存**。更新分支只在 `time_changed` 里 `delete()` 缓存并建同步
   任务（`weekly_version_logic.py:502-509`），所以「改窗口」会顺带清空缓存并换掉
   `group_key`（基线跟着没了，增量会退化成首跑）。要让缓存按新提交重算又不动窗口，
   只能等调度器那轮（每 15 分钟）。
