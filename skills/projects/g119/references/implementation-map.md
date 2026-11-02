---
description: 地宫MultiLevel和TGD从选关、地图难度、布局资源到Loot与撤离结算的消费方入口，区分logicalId与resourceId、三条进场路径和GM跳过条件。
---

# 地宫与运行期关联取证入口

本文件是仓库内代码取证的定位索引，不执行客户端/MCP/GM或服务器操作，不扩展平台权限。实现可能随提交变化，必须冻结核对。

## 1. UI和配置链

入口：LobbyViewNew→MultiLevelCltMod→MultiLevelMainView/MultiLevelEquipView→MultiLevelSvrMod。

| 关联 | 核查内容 |
|---|---|
| CfgDup_multiLevel.mapIds→CfgMap.DifficultyLevel | 玩法、地图与难度组 |
| 难度组+难度→CfgDifficultyRegistration.id | 难度UID、scales撤离门槛、playMaxDuration局长、词条/崩塌参数 |
| CfgDungeon→resourceId、DungeonPickMod | 逻辑条目与布局资源不同，同难度可随机选多布局 |
| CfgDungeonData/CfgDungeonUnityData资源表 | 房间/点位/资源名与烘焙文件，不能只改逻辑条目便认定资源已切换 |
| CfgTGDMapElement及TGD生成/加载 | 房间prefab、尺寸、端口、楼层、导航与动态加载 |

文档记录当前nDungeonId可能表示resourceId，旧版可能先logical→resource映射。895xx等新编号或多个地图共用ID，须查语义、选择与加载，不能直接判串图。

## 2. 进入路径分开验证

- 单人直开：MultiLevelSvrMod.startAction→SingleMultiLevelSvrMod.singleStart→GasSceneManagerMod.newScene。
- 匹配补人：requestMatching→GasMatchFlowMod.begin→GlsMatchHandoffMod。
- 队伍召集：GlsTeamMultiLevelMod.callUp*→GlsTeamDupHandoffMod。
- QaMap.enterDungeon/gmEnterMultiScene不跑完整MultiLevel生命周期，不能替代正常UI验证进度、预开始、结算。

## 3. 建场与楼层

SvrDupMod.startDup→SvrMultiLevelMod.onStartDup→ScsDungeonMod.preInit/init/start/afterStart；客户端SceneDataMod→CltDungeonMod/CltDungeonRoomDynamicLoadMod。预开始syncPreStart驱动，卡Perform时查服务端硬校验，不用一个HUD/布尔值证明已进入Battle。

TGD作者参数、生成结果、已保存房间、运行时楼层不同。楼层模型为地上1起、地下负数、无0层；跨层房间和地图相机层号不等价，空层消失不直接证明配置丢失。

## 4. Loot关联

副本/难度→布局→房间/候选点→容器/散点模板→掉落包→物品双键→拾取/背包→提交结算。

少一环是证据缺口；旧ID搜索0不是无引用证明。共用模板可能是复用，上限0可能关闭一类容器；核实替代产出/难度目标后再判断是否影响撤离。

## 5. 撤离结算

CltEntityComponentRetreatArea→MainUIView_BornRetreat→ScsBornRetreatTriggerMod.finishRetreat→SvrMultiLevelMod.syncDupResult→MultiLevelSvrMod.setDupResult→CltSettlementUtil/SettlementResultView。

核实区域、位置/存活校验、提交金额的个人/队伍/场景范围、预览/最终结果、幂等发奖、掉线/退出与死亡留场。源码索引记场景总额，与M5介绍“全队共享”可能范围不同；既不自动推广为设计要求，也不直接判多队漏洞。

gmForceRetreat可跳过区域/位置/价值，不代替正常上交撤离。单人、非最后队员、最后撤离者、死亡留场者分开测；服务端成功不等于客户端到账和展示正确。
