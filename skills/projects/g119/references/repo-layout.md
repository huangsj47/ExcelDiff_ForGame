# 平台可读范围与代码地图

> **用途**：告诉分析**两个仓库里都有什么、手里的表 / 改动该去哪找依据**，以及**别读什么**。


## 1. 配置仓库 `qz_config`

```
qz_config/
├── config/              # 配表本体（xlsx）—— 全部的表都在这里
│   ├── 10_role/ 20_scene/ 21_pcg/ 30_goods/ 40_monster/ 50_weapon/ 60_skill/
│   ├── 70_task/ 80_settlement/ 90_tutorial/ 130_multi_dup/ 150_shopping_mall/
│   ├── 200_dressup/ 301_effect/ 360_guide/ 490_home/ 990_qa/
│   ├── asset/ hero/ phone_preset/ rank/ season/ shop/ wwise/
│   └── (root 散表) 常量表.xlsx、奖励模式_CfgRewardMode.xlsx、技能指令表.xlsx、
│                   系统功能开放表.xlsx、邮件表_CfgMail.xlsx、语言表.xlsx、
│                   HUD按钮状态表_CfgMainUIActionState.xlsx、界面按键提示.xlsx、
│                   通用提示框表_cfgGeneralTips.xlsx、合规颜色表.xlsx、伤害飘字.xlsx、
│                   本地化字符串.xlsx、Patch语言表.xlsx、UI预制体变体表.xlsx、
│                   自动化.xlsx、自动化配置组.xlsx、工具文档表.xlsx
├── lua/
│   ├── client/          # 客户端导出产物（CfgXxx.lua）—— 这一侧是「导表产物」的主要形态
│   ├── server/          # 服务端导出产物（CfgXxx.lua）
│   └── code/            # 导出 / 校验 / 转换工具链：ExportDiff.lua、checkClient.lua、
│                        #   checkServer.lua、check/ convert/ weaponPropertyTemplate/
├── ExportExcelTool/     # 导表工具（含 EXPORT_EXCEL.log、xlua、ExportExcelTool.exe）
├── EditorCfgTool/       # 编辑器侧工具链
├── webconfig/           # 新表编辑器（行为树、地牢生成参数、GM 时间轴等）
├── CustomTableExport/
├── ExportAllExcel.bat / .sh   ExportDiff.bat   创建导表右键菜单.bat
└── README.md            # ⚠️ 里面的 ID 规范与分类表已被实测证伪，见 system-map.md §1
```

**关键路径**（命中即把本次分析升级为全量，见 `project-facts.md`）：`ExportExcelTool/`、`EditorCfgTool/`、**`lua/code/`**。
**`lua/code/`** 是配置工具链：`ExportDiff.lua`（**差异导出**核心，基于 `utils/TableDiff` 逐表 `_diffData`）、
`checkClient.lua` / `checkClientInit.lua` / `checkServer.lua` / `check/`（**导表校验**层）、`convert/`、`weaponPropertyTemplate/`。
它与根目录的 `ExportDiff.bat` 是**配置差异导出**链路 —— **改它会改变「diff 显示什么」与「导出报什么错」，即改变判据本身**，
所以被声明成关键路径；改动它时除了常规风险，还要提示**平台自己看到的 diff 口径可能同时变了**。

**两个仓库里都有 `lua` 概念，别混**：
- `qz_config/lua/{client,server}/` = **导表产物**，每次导表整体重写。
- `qz_luaworkspace/code/qz_pub/cfg/` = **配置的代码访问层**（`*CfgMod.lua`），是人写的、带文档块，**不是产物**。

## 2. 代码仓库 `qz_luaworkspace`

```
qz_luaworkspace/
├── code/
│   ├── qz_client_lua/   # 客户端 Lua
│   ├── qz_pub/          # 双端公共 —— 改动影响面最大
│   │   ├── cfg/         # ★ 配置访问层（*CfgMod.lua，57 个）—— 见 §4
│   │   ├── base/ cfgInit/ const/ container/ core/ headcode/ utils/
│   │   ├── protocols/   # 协议
│   │   ├── hotfix/      # 热更
│   │   ├── lbsRegion/   # LBS / 地区榜
│   │   ├── navimapper/  # 导航地图
│   │   ├── ugc/         # UGC 平台（在建）
│   │   └── DebugCode/ usedAssets/ UM_pub/ zoneinfo/
│   └── qz_server/       # 服务端（conf/ db/ doc/ log/ reload/ src/ var/ tcstart）
├── tools/
└── docs/                # ★ 可读的项目文档 —— 见 §5
```

规模事实：**3600+ 个 Lua 文件，横跨 4 个 Git 仓库**；技能系统单独约 **5 万行**，Hit 类型 **300+** 种。

## 3. ⚠️ 模块名前缀 = 这段代码跑在哪一端（治 `code_logic` / `module_coupling`）

**读代码时最高频的一条判据。改动落在哪个前缀上，决定要不要两端都回归。**

| 前缀 | 全称 | 跑在哪 | 例子 |
|---|---|---|---|
| `Clt` | Client | **仅客户端** | `CltPlayerActSkMod` |
| `Scs` | Scene Server | 场景服 | `ScsPlayerActSkMod` |
| `Svr` | Server | 服务端通用 | `SvrSceneRoleAtkMod` |
| `Gac` | Game Application Client | 客户端进程（常用于「发给客户端的协议」） | `GacRoleActMsgMod` |
| `Gas` / `Gbs` / `Tms` / `Lgs` / `Dbs` | 各服务进程 | 对应进程 | `GasSaLogMod`、`GbsLbsRankBoardMod` |
| `Pub` | Public | **双端共用** | `PubWeaponMgrComp` |
| 无前缀 | 双端共用核心逻辑 | 双端 | `BuffMod`、`RoleAttrMod` |

> ⚠️ **最大的坑（反误报关键）**：`qz_pub/` 目录下的 `Scs*` 模块，**在单人副本时会跑在客户端的 Lua 虚拟机里**。
> 所以 **「服务端代码」不等于「另一台机器」**，也**不能**因为「这是 Scs 模块」就断言「客户端改不了 / 必须重启服务端」。

**变量名前缀（匈牙利命名）**：`n`=number、`s`=string、`b`=boolean、`t`=table、`f`=function、`u`/`go`/`cs`=Unity 对象、`lg_`=模块级「全局」（**会被热更保留**）、`_` 开头=文件内私有。
战斗里最常见两个变量：`tPerpetratorObj` / `tPerpetratorRole`（施加者）、`tVictimObj` / `tVictimRole`（受害者）。

## 4. ★ `code/qz_pub/cfg/*CfgMod.lua` —— 每张表的「代码说明书」（首选入口）

**这是本项目最好用的一个事实来源，优先于知识包里的任何描述。**

- 每个 `*CfgMod.lua` 是**某张（或某一组）配置表的代码访问层**，内含 `_cfg.<表名>` 的读取函数。
- **很多文件开头有 `---@module` 文档块，直接写明**：该表在 `qz_config` 里的**配表路径**、**字段含义**、**代码与配置的契约**、以及**未接入时的兜底行为**。
- 例（`RankCfgMod.lua`，全文照录其要点）：明写「配表见 `qz_config/config/rank/排行榜_CfgRank.xlsx`，导出为 `_cfg.CfgRank`」、逐字段说明 `type`（`s`=赛季榜/`w`=周榜）、`refreshDay`、`rewardCycle`；给出**写侧玩法角色 → 配置 id 的契约** `BoardId = { Dan = 1, Carry = 2 }`；给出 **UM 榜名格式** `g119_rank{id}_{周期后缀}`；指明奖励 id 指向 `CfgRewardMode`；并写明 **「配表未接入时各读取接口返回 `nil` / `0`，调用方自行兜底，不得报错」**。
- 例（`SettlementCfgMod.lua`）：文件名是 `Settlement`，实际读的是 `_cfg.CfgWithdrawSettlement` / `CfgWithdrawEvaluate` / `CfgWithdrawEvaluateLevel` —— **靠名字猜会猜错，必须看文件内容。**
- 例（`ActSkCfgMod.lua`）：`require "60_skill/SkillCfg"`、`SkillLibCfg`、`BuildSkillGroupCfg` → 它对应的是**技能系列**表。

**配对规则（平台的名字前缀配不上它，要按这条用）**：

```
code/qz_pub/cfg/{X}CfgMod.lua   ←→   配置表 {X}
  · {X} 本身以 Cfg 结尾 → 就是表名本身（WeaponStatsCfgMod → WeaponStatsCfg；RoleAttrCfgMod → RoleAttrCfg）
  · {X} 不以 Cfg 结尾 → {X} 只是简写，必须按文件内容核对，不能靠名字猜
      已知不一致的例子：SettlementCfgMod → CfgWithdrawSettlement 系列
                        CfgMapResMod       → CfgMap2Res（表名带 2）
                        ActSkCfgMod        → SkillCfg 系列
```

> 平台侧的 `generated_prefixes: Cfg` 配对**长期会是 0 组**，原因就是上面这条命名错位（`Cfg` 落在名字中间）。**那是如实的「本项目本来就没得靠名字配」，不是「平台不会配」** —— 这层关系由本节这条规矩来补。

**⚠️ 反过来看也极有用**：**新表如果没有对应的 `*CfgMod.lua`，说明程序还没接读。**
例：`config/150_shopping_mall/` 下 2026-09 新增的 `【158】商城商品表_CfgStoreGoods.xlsx`、`【159】商城直购表_CfgStoreType.xlsx`，在 `qz_pub/cfg/` 里**只有老的 `ShoppingMallCfgMod.lua` 与 `PayCfgMod.lua`**，**没有 StoreGoods / StoreType 的 CfgMod** → 印证「表已生成、程序还没接」。

## 5. ★ 可读的项目文档（`qz_luaworkspace/docs/`）

**找一个改动的「设计意图」时先来这里**，比从 diff 反推可靠得多。

| 路径 | 内容 |
|---|---|
| `docs/战斗框架md/`（**19 篇**，2026-09-21） | 整体架构与代码地图；模块系统与双端代码复用（`moduleDefStart` / `refModuleFunc` / Pub 继承 / `isGac`）；一帧的生命周期；角色对象与数据模型；角色状态机；属性系统；**伤害计算（完整的伤害体系公式、`doDamage` 扣血全流程）**；技能系统 `ActSk`（节点图 + Transition + Hit + Behaviour）；Buff 系统（准入检查链、feature/callbacks）；武器与子弹（开火流程、瞬时弹/实体弹、命中检测）；怪物 AI 与行为树；3C 与表现层；**双端同步模型（谁是权威、AOI、协议、防作弊）**；**配置系统与编辑器（Excel → Lua 全链路、战斗相关配置表）**；调试与排查手册（GM 指令、伤害打印、日志检索、常见坑）；另有 3 篇实战（一次开枪的完整旅程 / 一个 Buff 的一生 / 怪物技能从 AI 到表现）+ 新人上手任务清单 |
| `docs/superpowers/specs/`、`docs/superpowers/plans/` | **按日期归档的设计文档与实施计划** —— 查历史设计决策的首选，搜关键词常有收获 |
| `docs/bug-analysis/` | 真实缺陷分析归档（如 `phone-lockscreen-relogin-loading-98-analysis.md`、`xluatools-csharp-hotfix-analysis.md`、`mcp-mobile-lan-debug-analysis.md`）—— 判「这是不是已修过的老问题」时有用 |
| `docs/main-ui-action-state.md` | 主 HUD 按钮状态机 |
| `docs/trap-configuration.md` | 陷阱配置 |
| `docs/行为树入门.html`、`docs/tcstart 卡死急救手册.html` | 行为树入门；服务端卡死急救 |
| `code/qz_pub/const/` | 各类枚举常量（`SkillConst.lua` 是战斗最重要的一个） |
