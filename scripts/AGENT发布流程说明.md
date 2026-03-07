# Agent 发布流程说明（platform + agent）

本文回答两个常见问题：
- 要不要先执行 `agent/打包agent.bat`？
- `publish_agent_release` 应该在平台/agent 启动前还是启动后执行？

## 结论（推荐顺序）

1. 更新代码并启动/重启平台（先让平台代码到最新）。
2. 执行 `scripts/publish_agent_release.bat` 发布 Agent release。
3. 启动或保持 Agent 运行，等待其自动更新并重启。

简化记忆：**先平台，后 publish，再 agent 生效**。

## 为什么是这个顺序

- `publish_agent_release.bat` 会直接把 `agent` 目录打包为 release，并更新 `instance/agent_releases/latest.json`。
- Agent 自更新是通过平台接口拉取最新 release 信息并下载包。
- 所以应先保证平台已是最新代码并可正常提供 release 接口，再发布 release。

## `打包agent.bat` 和 `publish_agent_release.bat` 的区别

- `agent/打包agent.bat`：生成 `agent_package_*.zip`，主要用于手动分发/离线包，不会更新平台的 latest release 指针。
- `scripts/publish_agent_release.bat`：才是平台+agent 自更新链路使用的发布动作（会更新 latest）。

结论：**走自更新链路时，不需要先执行 `打包agent.bat`。**

## 标准操作步骤（Windows）

在仓库根目录执行：

```bat
REM 1) 启动/重启平台（按你的现有方式）

REM 2) 发布 Agent release
scripts\publish_agent_release.bat

REM 可选：指定版本号和备注
scripts\publish_agent_release.bat --version 20260307-01 --notes "修复merge-diff和weekly-sync"

REM 3) 启动 Agent（若已在运行可不重启，等待自动更新）
```

## 关于“更新触发慢”

- Agent 会按 `AGENT_AUTO_UPDATE_CHECK_INTERVAL_SECONDS` 轮询检查更新。
- 当前默认建议值为 `60` 秒（最小 `30` 秒）。
- 如果仍觉得慢，可在 `agent/.env` 调小该值。

## 回滚

```bat
scripts\rollback_agent_release.bat --steps 1
```

或指定版本：

```bat
scripts\rollback_agent_release.bat --target-version 20260307-01
```

