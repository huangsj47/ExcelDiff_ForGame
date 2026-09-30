# G119 系统与配表映射（qz_config）

> **用途**：把「一个改动路径 / 表名」翻译成「哪个系统、多少张表会跟着动、回归面多大」，以及避开这张仓库里真实存在的几个坑。

> **`code/qz_pub/cfg/*CfgMod.lua` 这个「每张表的代码说明书」入口**、以及 `docs/` 下可读的项目文档，
> 都在 **`repo-layout.md`**。**动手前先读它** —— 尤其 `*CfgMod.lua`，它比本文件更权威（带字段与代码契约的文档块）。
> 本文件里的表名、分类、规模数字来自仓库扫描；**内容范围与生效状态**在仓库里**读不到**，见 `game-overview.md`。

## 1. 三套编号体系（同一个数字在三套里含义完全不同）

| 编号体系 | 写在哪 | 规则 | 有人强制吗 |
|---|---|---|---|
| ① **老表的表 ID** | xlsx 里由 `A2` 指认的那一列（通常是 `id`） | README 写「6 位，前两位为类型」 | **没有**。工具只检查「主键是数字」 |
| ② **目录 / 文件名编号** | `config/<NNN>_<名>/` 目录 + 文件名前缀 `【NN】` | 2–3 位（`【40】`、`【158】`、`【990】`） | **没有**，只是给人找表用的 |
| ③ **新表的 displayName 段位** | `schema.displayName`，同时写进生成的 Excel 文件名 | **8 位一段**，如 `怪物(16010001~16020000)` | 段位没人管；但「id 不能重复」有两道闸门，很死 |

| 位数 | 1 | 2 | 3 | 4 | 5 | **6** | 7 | **8** | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| 占比 | 6.8% | 8.5% | 3.3% | 12.2% | 18.9% | **17.4%** | 12.8% | **19.0%** | 1.0% | 0.2% |

→ **6 位是少数派，8 位数量差不多甚至更多。** 引用时说明口径，**不要为了凑 6 位去改已有 id**（改 id 会打断所有引用它的地方）。

## 2. ⚠️ ID 不是全局唯一的 —— 物品表要 `(bigType, id)` 双键（治 `config_id`）

**【事实】物品表（`CfgGoods*`）靠 `(nBigType, id)` 两个字段才能定位一行，光看 `id` 不行。**
实测有 **39 个 id 在不同表之间重复**，且**导表不报错**。例：`400004` 既是「家园币」又是「谎言之杖」；`120002–120004` 在 `CfgGoods_bag` / `CfgGoods_bullet` / `CfgGoods_normal` 三张表里撞。

→ **写引用时的铁律**：所有跨表引用物品的地方必须**同时带 `nBigType` + `id`**（`CfgShop`、`CfgAppraise`、`CfgItemInspect`、`CfgRecommendedSet.items` 全是这个形态）。**这是「我引用的东西不对」最高频的原因。**
→ 分析含义：看到只改 `id` 的改动，**先问它改的是哪个 `bigType` 段**；只改 `id` 而 `bigType` 没跟上是典型误改。

**物品 `bigType` 速查**（来自 `CfgGoodsType` 与各分表）：`1` 消耗 / `2` 普通（回收物、材料、梦灵瓶、贵重品）/ `3` 武器 / `4` 货币（元宝、货币、角色币、家园币）/ `9` 背包 / `12` 子弹 / `13` 虚拟（坐骑、声望、熟练度、凭证、角色属性，id 是小整数） / `26` 家具（**已废弃，见 §7**）。

## 3. 业务分类目录总览（21 类 / 167 个 xlsx / 354 个工作表）

| 分类目录 | 装什么 | 关键表 |
|---|---|---|
| `10_role` | 角色与成长（角色、创角名字、等级、属性状态、主角状态机） | `CfgRole`、`CfgRoleIndex`、`CfgPlayerLevel`、`RoleStateRulesCfg`、`CfgRoleCustomController` |
| `20_scene` | 场景与关卡（地图、副本、场景物件、难度、天气、剧情演出） | `CfgMap`、`CfgScene`、`CfgDungeon`、**`CfgDifficultyRegistration`（多难度唯一权威）**、`CfgDungeonLootTemplate`、`CfgSceneLoot`、`CfgTriggerEvent`、`DramaCfg` |
| `30_goods` | 物品与装备（物品分表、图鉴、鉴定、身体改造） | `CfgGoods*`、`CfgGoodsType`、`CfgAppraise`、`CfgItemManual*`、`CfgTxtTemplate` |
| `40_monster` | 怪物 AI 与掉落 | **`CfgMonsterAI`（19 xlsx 合 1 张表）**、`CfgMonsterDrop`、`CfgMonsterHate`、`CfgMonsterManual*`、`CfgMonsterSensory`、`CfgMonsterType` |
| `50_weapon` | 武器 | `WeaponCfg`（5 sheet）、`WeaponStatsCfg`（5 sheet）、`WeaponCategoryCfg`、`EquipAimCfg`、`CfgSiphonInteractShow`（吸灵器按钮） |
| `60_skill` | 技能与战斗规则 | `SkillCfg`（6 sheet）、`HitCfg`（17 sheet）、`AoeCfg`、`ExplodeCfg`、`RoleAttrCfg`、`PassiveSkillGroupCfg`、`StiffTypeCfg`、`SkillLibCfg` |
| `70_task` | 任务与条件 | `CfgConditionGroup`（4408 行）、`CfgTaskConditionType`、`CfgStoryTask`（梦主委托） |
| `80_settlement` | 撤离结算 | `CfgWithdrawSettlement`、`CfgWithdrawEvaluate`、`CfgWithdrawFailGoods` |
| `90_tutorial` | 教学关 | `CfgTutorialChapter`、`CfgTutorialFlow`、`CfgTutorialReward` |
| `130_multi_dup` | 多人副本 | `CfgDup_multiLevel(+Group)`、`RandomAffixCfg/PoolCfg/VoteCfg`、`CfgM5Progress` |
| `150_shopping_mall` | 商城与充值 | **新：`CfgStoreGoods`、`CfgStoreType`（2026-09 新增）**；老：`CfgShoppingMall`、`CfgDiamond*`、`CfgDrawPool`、`CfgPay` |
| `200_dressup` | 外观 | `CfgDressUp`、`CfgDressUpAction`（动作库 / 表情） |
| `301_effect` | 通用特效 | — |
| `360_guide` | 引导 | `CfgGuide`、`CfgGuideTips` |
| `990_qa` | QA 工具表（**非游戏内容**） | `CfgResourceShot`、`CfgResourceShotEffect` |
| `asset` | 模型 / 动作 / 特效 / 材质 / 图标 / 音效 / Timeline | **`CfgModel`（4 xlsx / 13 sheet 合 1 张表）**、`CfgActing`、`CfgSfx`、`CfgIco`、`CfgMaterial`、`CfgHitEffect`、`CfgVideo`、`CfgTimeline` |
| `hero` | 英雄 / 外骨骼 | `CfgHero`、`CfgHeroLevel`、`CfgHeroLock`、`CfgHeroType` |
| `phone_preset` | 机型适配 | `CfgPhonePresetGpu`、`CfgPhonePresetModel` |
| `shop` | 局内 NPC 商店（**非商业化**） | `CfgShop`（`商店.xlsx`） |
| `wwise` | Wwise 音频 | `WwiseEventCfg`、`WwiseSoundbankCfg`、`WwiseStateGroupCfg`、`WwiseSwitchGroupCfg`、`WwiseGameParameterCfg` |
| **`rank`** ★新域 | 排行榜 | `排行榜_CfgRank.xlsx` → `CfgRank`（赛季榜 `s` / 周榜 `w`，明细见 `repo-layout.md` §4 的 `RankCfgMod`） |
| **`season`** ★新域 | 赛季与段位 | `赛季配置表`、`段位名称表`、`子段位等级表`、`赛季段位积分道具表`、`赛季段位奖励表`、`PVE搜撤评分表` |
| **`490_home`** ★新域 | 家园 | `【490】家园家具表_CfgHomeFurniture.xlsx`、`【490】家园家具类别表_CfgHomeCategory.xlsx` |
| `(root)` | 根目录散表 | `常量表`（`ConstCfg`）、`奖励模式_CfgRewardMode`、`技能指令表`、`系统功能开放表`（`CfgSystemOpen`）、`邮件表_CfgMail`、`语言表` / `本地化字符串` / `Patch语言表`、`HUD按钮状态表_CfgMainUIActionState`、`界面按键提示`、`通用提示框表`、`合规颜色表`、`伤害飘字`、`UI预制体变体表`、`自动化` / `自动化配置组`、`工具文档表` |

> ★新域 = 这一分类是**近期新建或扩建**的域（另见 `lua/` 侧的 `470_daily_lottery`（每日抽奖）、`480_dye`（染色）、`21_pcg`）。
> **它们的共同特征是「内容在建、尚未对玩家生效」** —— 看到这些域在动，先回 `game-overview.md` 的 §3 状态表
> 与 §4.2 判定规则处理（是「已落地」还是「在建」），**不要贴版本标签**。
> ⚠️ 上面各分类的「表数量、规模」数字来自 2026-09-30 的扫描，仓库在持续增长；**以实际目录为准**。

> 另有 **54 张很久没人改的表**（最后改动早于 2025-01-01）、**70 页不进游戏的说明/对照/矩阵页**、**32 个编辑器表**（怪物行为树 25、地牢生成参数 4、GM 时间轴 3）、**9 批新表**（怪物 / Buff / 被动技能，字段由程序定义）、**3 页自定义表**（表头只有一行、`A1` 写 `SKIP`）。

## 4. 表 → 系统 → 回归面（治 `module_coupling` / `config_linkage`）

拿到改动路径时先对下面这张表。**「会跟着动的表」就是本次回归面的下限。**

| 改的是什么 | 主表 | 会跟着动的表 | 属于哪个职能 |
|---|---|---|---|
| 新增 / 改一个怪物、调 AI | `CfgActor`（新表，编辑器改）、`CfgMonsterAI` | `CfgMonsterDrop`、`CfgMonsterHate`、`CfgMonsterSensory`、`CfgMonsterType`、`CfgMonsterTemplates`、`CfgSpawnProbability`、`CfgRespawnCycle`、`SkillLibCfg`（怪物技能库） | 怪物 AI |
| 角色模型 / 动作 / 特效图标 | `CfgModel`、`CfgActing` | `CfgSfx`、`CfgIco`、`CfgMaterial`、`CfgTexture`、`CfgModelSkin`、`CfgMonsterAnimCtrl`、`CfgAnimSoundEffect`、`CfgRoleDead` | 渲染表现 |
| 新增技能 / Buff / AOE / 爆炸 | `SkillCfg` | `BuffCfg`（新表）、`HitCfg`、`AoeCfg`、`ExplodeCfg`、`RoleAttrCfg`、`PsSkSpcCfg`、`PassiveSkillGroupCfg`、`BuildSkillGroupCfg`、`StiffTypeCfg` | 战斗 / 技能 |
| 关卡副本 / 难度 / 掉落 | `CfgScene`、`CfgDungeon`、`CfgDifficultyRegistration` | `CfgSceneLoot`、`CfgDungeonLootTemplate`、`CfgMap`、`CfgMap2Res`、`CfgDup_multiLevel`、`CfgGadget`、`CfgMapLevelLayer`、`CfgSceneLevelLayerType` | 关卡地图 |
| 角色 / 创角 / 外观皮肤 / 捏脸 | `CfgRole`、`CfgRoleIndex` | `CfgPlayerLevel`、`CfgDressUp`、`CfgRoleCustomController`、`CfgRoleCapsuleInfo`、`CfgHero` | 角色 |
| 物品 / 装备 / 武器 | `CfgGoods*`、`CfgGoodsType` | `WeaponCfg`、`WeaponStatsCfg`、`CfgItemManual*`、`GoodsEffectCfg`、`CfgTxtTemplate`、`CfgShop`、`CfgAppraise` | 道具 / 武器 |
| 商城商品 / 礼包 / 奖池 / 充值 | `CfgStoreGoods`、`CfgStoreType` | `CfgRewardMode`、`CfgRandomRewardPool` | 商业化 |
| 常量 / 开功能 / 引导 / 邮件 / 文本 | `ConstCfg`、`CfgSystemOpen` | `CfgTutorialFlow`、`CfgMail`、`CfgLanguage` | 系统 |

**三条会放大回归面的常识（本项目实测成立）**：

1. **公共表的改动回归面按调用方算，不按改动点算。** `ConstCfg`（10 个 sheet 合 1 张）、`CfgConditionGroup`（4408 行）、`RoleAttrCfg`（282 行）、`CfgGoodsType`、`CfgLanguage` 被大量模块读 —— `ConstCfg` 历史上一次清理就牵动全项目常量。
2. **`CfgMap` 是全项目历史提交最多的表**（20 行 / 63 字段），改它基本等于改关卡玩法。
3. **一张表 = 一个热更单元**（导出结果是按文件整体重写的）。表越大、改得越频繁，单次热更体积越大 —— 这解释了很多「只改了一行却要整表回归」的情形。

## 5. ⚠️ 合表陷阱（治 `config_data` / `module_coupling`）

**27 张表是多 sheet 合成一张表**（同一个 `A1`），其中**跨多个 xlsx 文件的只有 2 例**：`CfgMonsterAI`（**19 个 Excel**）、`CfgModel`（**4 个 Excel**）。这是**合法的做法**，不要当成「重复表」。

| 大表 | 规模 | 陷阱 |
|---|---|---|
| `CfgMonsterAI` | 19 xlsx / 19 sheet / 469 字段 | 每只怪一个 Excel；`aiTree` 编号固定为「树号 × 10000」 |
| `HitCfg` | 17 sheet / 885 行 | 每页列都不一样；「护甲片 / 生成坐标 / 召唤怪 / 追踪导弹」4 页是 `hidden`，**数据不进游戏** |
| `CfgModel` | 4 xlsx / 13 sheet / 710 行 | 跨文件合表 |
| `CfgLanguage` | 12 sheet | — |
| `ConstCfg` | 10 sheet | 全项目常量，改动面最广 |
| `SkillCfg` | 6 sheet / 117 行 | ⚠️ **见下** |
| `CfgMonsterHate`、`CfgScene`、`WeaponCfg`、`WeaponStatsCfg`、`WwiseSoundbankCfg` | 5–6 sheet | 每页一套默认值 |
| `CfgConditionGroup`、`CfgIco`、`CfgSfx`、`WwiseEventCfg` | 4 sheet | — |

## 6. ⚠️ 主键重复与导出失败：只报不拦（治 `config_id` / `config_data`）

这三条是**受控实验**结论（把导出器复制到沙盒、跑真实导出器 + LuaJIT 实跑验证，未改动 `qz_config` 本身）：

| 场景 | 结果 |
|---|---|
| **同一张表内主键重复**（同 sheet、跨 sheet、跨 xlsx） | ⚠️ **只报不拦**：`EXPORT_EXCEL.log` 里留一条 `Error: …第7行的主键与6行的主键重复`，但**导出照常成功**；导出的 lua 里两条并列写出，**程序取后面那条，前面那条彻底消失**（记录数从 2 变 1）。**工具会给报错，但不阻断。** |
| **同一张表（同 `A1`）出现在不同目录下** | `(导出失败)…存在下` —— **两个都没导出**。这是「合法跨文件合表」的分界线：**可以跨文件，但必须在同一个目录** |
| **同一张表内两个 sheet 的工作表名相同** | `导出失败 … 已添加了具有相同键的项` |

> 注意：工作表名叫 `Sheet1` 的在 49 个文件里都有、全部正常导出 —— 因为它们的 `A1` **不同**（是不同的表）。**只有属于同一张表（同 `A1`）的两个 sheet 重名才会撞。**

**其它已实测的导出行为**：

- **加列只能加在最后一列**。插在中间 → `tSheetN_KeyIndexMap` 整行重写、后面所有字段编号集体 +1。
- **主键空着的行会被悄悄丢掉**（日志报 `主键为空，已忽略该条数据`），条数对不上就往这查。
- **仓库现状：三条规则目前一处都没违反** —— 现有配置靠**人工分段 + 说明页恰好不导出**维持，**不是靠工具护栏**。所以「新加 id / 新合表」这类改动没有安全网，评审时要主动查重。

## 7. ⚠️ 停用 / 废弃的判定：看 `A1`，不看文件名（治 `config_data`）

| 状态 | 怎么做到的 | 效果 |
|---|---|---|
| **停用** | ① `A1` 留空　② 或 sheet 设为 `hidden` | 整个 sheet **不导出** |
| **留痕** | 文件名加 `——废弃`（`git mv`，不是删除） | **不参与导出判断**，只是留个能搜到的历史 |
| **真删除** | `git rm` 文件 + 删掉导出的 lua + 去掉 `cfgHeader.lua` 里的 require 行 | 导出结果消失 |

**⚠️ 文件名里的 `——废弃` / `-old` 完全不阻止导出（已用受控实验定论）**：`废弃名A——废弃.xlsx` 与 `废弃名B-old.xlsx` 都正常报「导出 1 条数据」并生成 lua。

→ **靠文件名判断废弃一定会误判。** 真正不导出的只有 `A1` 留空 / sheet hidden 两条。仓库里现存 6 个带 `——废弃` 的文件之所以失效，是**改名 + 清空 `A1` 两件事一起做的**。
→ 反过来，**文件名没带 `——废弃` 的表也可能是停用的**（449 个 sheet 里 67 个 `A1` 为空，全是说明页 / 规划页 / 旧配置页）。

## 8. ⚠️ 孤儿结果：lua 里有这张表 ≠ 这张表还活着（治 `config_id` / `process`）

**导表工具不会回收已删除的导出文件**：源 Excel / sheet 已删，`lua/` 里的产物还在。

| 孤儿结果 | 证据 |
|---|---|
| `lua/server/10_role/CfgRoleBodyIndex.lua` | 注释指向 `【10】角色表_CfgRole.xlsx` 的 `角色裸模替换` sheet —— **该 sheet 现在不存在** |
| `lua/{client,server}/10_role/CfgRoleSetAttribute.lua` | 来自已删除的 `【15】角色定制状态表_CfgRoleSetAttribute-76ec8856.xlsx` |
| `lua/{client,server}/10_role/CfgPlayerActive.lua` | 来自已删除的 `角色活跃度表_CfgPlayerActive.xlsx` |
| `lua/client/wwise/WwiseStateCfg.lua`、`WwiseSwitchCfg.lua` | 源文件只剩 `StateGroup` / `SwitchGroup` 一个 sheet；游戏真正读的是 `WwiseStateGroupCfg` |
| **`CfgFurnitureBigType` / `CfgFurnitureSmallType` / `CfgFurnitureAchievement`** | 源头 `【30】物品表（26 家具道具）_CfgGoods_furniture.xlsx` **已不在主干**（2026-05-18 删除）；`code/` 里搜 `CfgFurniture` = **0 处引用**。⚠️ **这三张最容易被当成「家园家具表」的答案** —— 它们连内容都对得上。 |
| ⚠️ **家园家具的正确落点**（2026-09-30 已存在） | 真正的家园家具在 **`config/490_home/`**：`【490】家园家具表_CfgHomeFurniture.xlsx`、`【490】家园家具类别表_CfgHomeCategory.xlsx`。**别再把 `CfgFurniture*` 或 `【30】物品表（26 家具道具）` 指给它**（那是废弃链路，见上一行） |

另有 4 张**身份不明**的产物（开头没有 `--所在Excel文件:` 注释、也不在 `EditorCfgTool/` 里）：`CfgNoHeadMonster`、`CfgMonsterValue`、`CfgMonsterDestruction`、`CfgDebugMonsterData`。

**判断「一张表是否还活着」的四步**：① 源 xlsx 还在 `config/<目录>/` 吗 → ② 目标 sheet 的 `A1` 还非空吗 → ③ sheet 是不是 `hidden` → ④ `code/` 里还有人读它吗。

## 9. ⚠️ 表已生成但程序还没接（治 `config_linkage` / `process`）

**看到这些表在动，不等于功能已生效。** 不要把「配置已铺」写成「功能已上线」：

| 表 | 状态 |
|---|---|
| `CfgStoreGoods` / `CfgStoreType` | **2026-09 新增**的新一代商城表：`config/150_shopping_mall/【158】商城商品表_CfgStoreGoods.xlsx`、`【159】商城直购表_CfgStoreType.xlsx`。**表已生成，程序那边还没接上去读它** —— 判据：`code/qz_pub/cfg/` 下**只有**老的 `ShoppingMallCfgMod.lua` 与 `PayCfgMod.lua`，**没有** StoreGoods / StoreType 的 CfgMod（见 `repo-layout.md` §4）。同目录另外 6 个文件是老的付费/充值链路：`【150】充值和月卡表`、`【152】付费商品管理_CfgPay`、`【153】兑换码礼包管理_CfgCdGift`、`【154】奖池抽取表`、`【155】商城表`、`【157】充值返利` |
| `CfgDressUp` | 外观装扮表，**程序里还没真的读它**，功能还在做 |
| `CfgGuideTips` | 引导图文弹窗表，**目前不生效**，只是启动时被加载了一下 |
| `CfgItemInspect` | 回收物检视动作表，**一行数据都没有，是空表** |
| `CfgGuide` | 只有 3 列 1 行，程序里没人真的读它 |
| `CfgStoryTask`（梦主委托） | **委托玩法入口疑似尚未接线**；`commit_item` 必须 ≥ 3 元才会进索引 |
| 老商城 `CfgShoppingMall` / `CfgDiamond*` / `CfgDrawPool` / `CfgPay` | **很久没人改**（停在 2024-08~10），已被 `CfgStoreGoods` 取代。⚠️ 有人改这批表时**先确认它还有没有在用** |
| 已被取代但源头还在、程序不读的：`CfgRoleExpressionAction`、`CfgEnhancePower`、`CfgLevelLimit(ByServer)`、`CfgName_*` | 改之前先问程序 |

**⚠️ 商城/充值相关改动的判读**：`150_shopping_mall/` 下的改动目前**多数处于「表在铺、程序未接」阶段** —— 商业化整体在建（见 `game-overview.md` §3）。
**不要**把「程序还没接读」判成「商城配置错误」，也**不要**贴版本标签；按 `game-overview.md` §4.2 走 `process` 维度。

## 10. ⚠️ 只在单端生效的表（`config_linkage` 的现成抓手）

**这些改动不存在「两端都要回归」，写建议时要分清：**

| 只在**服务器**上用（手机里看不到） | 只在**客户端**上用（服务器拿不到） |
|---|---|
| `CfgMonsterDrop`（怪物掉落）、`CfgWeatherAffixPool`（天气随机池）、`CfgWithdrawFailGoods`（撤离失败保底）、`CfgM5Progress`（测试历程表 —— **表名里的编号是历史命名，是既有表名，不是版本线索，不要在结论里引申**）、`RandomAffixCfg` / `RandomAffixPoolCfg`（随机词条本体与池） | `CfgTriggerEvent`（**整表仅客户端**，480 行）、`CfgAppraiseLevel`（鉴定评语）、`CfgAppraiseScale`（鉴定 3D 缩放）、`CfgSiphonInteractShow`（吸灵器按钮）、`EquipAimCfg`（武器准心）、`PreInputChannelTypeCfg` / `PreInputCmdTypeCfg`（主角预输入，只对客户端生效）、`CfgResourceShot*`（QA 工具，非游戏内容） |

其余表**默认两端共用**，改动时主流程两端都要回归 —— 不能听改动者说改了哪一端就只测哪一端。

## 11. 表与产物要一起看（本项目事实）

**⚠️ 先分清三层，它们的身份完全不同：**

| 层 | 位置 | 谁写的 | 改动的含义 |
|---|---|---|---|
| **① 表** | `qz_config/config/<分类>/*.xlsx` | 策划 | 改内容 |
| **② 导表产物** | `qz_config/lua/{client,server}/<分类>/CfgXxx.lua` | **导出器生成**，每次导表整体重写 | **不该手改** |
| **③ 代码访问层** | `qz_luaworkspace/code/qz_pub/cfg/*CfgMod.lua` | **程序手写**，带 `---@module` 文档块 | 改的是**读取逻辑与契约** |

- **改 ③ 和改 ① 是两件完全不同的事**：`RankCfgMod.lua` 里改了 `BoardId` 映射 ≠ 配表改了。看到 ③ 在动，要看的是**调用方与契约**，不是数值。
- ① 与 ② **必须当成一次改动看**（同一次导表产生）。**改了 `【30】物品表_CfgItem.xlsx`，`CfgItem.lua` 必然跟着变。**
- 只看一侧看不出这类问题：**表里加了 ID、产物里没有对应项**（或反过来，产物里还留着表里已删掉的条目）。每一侧单独看都是正常的。
- 判「这张表程序接了没」的**最快判据**：`qz_pub/cfg/` 下**有没有对应的 `*CfgMod.lua`**（见 `repo-layout.md` §4）。
- **文件名完全不参与导出**：决定导出结果叫什么的是每个 **sheet 的 `A1`**（新表是 `schema` 里的 `config.fileName` / `globalName`）。实证：`hero/英雄.xlsx` → 导出 `CfgHero`、`CfgHeroLevel`、`CfgHeroType`、`CfgHeroLock`；`常量表.xlsx` → `ConstCfg`；**4 个名字不同的 xlsx 一起导出成同一个 `CfgModel`**；**19 个 `【41】AI行为表_id_N_xxx_CfgMonsterAI.xlsx` 一起导出成同一个 `CfgMonsterAI`**。
- 因此：判断「某个 sheet 是什么表」**永远看 `A1`**；判断「某个 lua 从哪来」看产物开头那行 `--所在Excel文件: <xlsx名>` / `--所在标签页: <sheet名>` 注释（合表时用 `、` 连接）。
- **导表链路自身的脚本改动也算「配置表变更」**：批量导出、差异导出、导表工具、右键菜单，以及 `ExportExcelTool/`、`EditorCfgTool/`（关键路径，见 `project-facts.md`）。
