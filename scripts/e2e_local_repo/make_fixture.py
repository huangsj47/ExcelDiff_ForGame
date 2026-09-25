# -*- coding: utf-8 -*-
"""造一份「少量」的本地 git 测试数据：小的 Excel 配表 + 一个小的代码模块（py 与 lua）。

设计意图（每一条都对应一个要验的判据）：

* `config/物品表.xlsx` —— 表头在第 1 行（最常见的形态），改动它验增量做差；
* `config/技能表.xlsx` —— **表头在第 3 行**（上面两行是标题/说明），走的是「特殊表头」
  那条路，验多套表头方案会不会被认出来；
* `src/battle_logic.py` —— 代码正文那条路（`AI 读代码正文的两条来源`）。里面埋**一个真
  问题**与**一个看起来像 bug 的有意设计**（`min_severity/min_confidence=high` 的配置下
  既要报得出来、又不能报错），与 tests/test_ai_live_endpoint.py 的双向检查同一手法。
* `code/qz_server/src/battle/BattleMgr.lua` —— 2026-09-25 加，**同一个口径的第二份**。
  `.py` 那条路绕开了两样只有 lua 才碰得到的东西：方案里的 lua 提取、以及 `code/` 这个
  路径前缀（平台按前缀判资源类型）；用 `.py` 验过的结论不能直接当成「lua 也这样」。

第二轮的提交里有一个是**回填日期**的（`GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` 设成比
上一个提交更早），专治 `commit-dates-are-backfilled` 那条：老口径 `git log --since`
遇到这种 tip 会停住整个遍历。

**工作目录可用 `E2E_FIXTURE_DIR` 换**（默认 `e2e`）：同一台机器上要同时留两份互不干扰
的联调数据时用得上（老项目的仓库 url 指着默认那份，重建它会把老项目正在用的裸库删掉）。
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 生成物（裸库 + 工作副本）落在 .pytest_tmp 下 —— 它已在 .gitignore 里，
# 进库的只有这几个脚本。
#
# 2026-09-25：目录名可用 `E2E_FIXTURE_DIR` 换掉。**原因是同一台机器上可能同时需要
# 两份互不干扰的联调数据**：一份是平台里已经注册过的老项目（它的仓库 url 指着
# `.pytest_tmp/e2e/origin.git`），另一份是给新项目用的干净数据。落在同一个目录里的话，
# 重建 A 的那一下会把 B 正在用的裸库删掉——而 B 那边看到的是「同步失败」，不是
# 「仓库没了」。
BASE = ROOT / ".pytest_tmp" / (os.environ.get("E2E_FIXTURE_DIR") or "e2e")
SRC = BASE / "gitsrc"
ORIGIN = BASE / "origin.git"


def _rmtree(path):
    """Windows 上 git 会把 `.git/objects/**` 标成只读，`shutil.rmtree` 会 PermissionError。"""
    def _on_error(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_on_error)


def _git(*args, env_extra=None, cwd=None):
    """在**工作副本**里跑一条 git。

    `cwd` 的默认值写成 `None` 再在调用点回落到 `SRC`，而不是直接写 `cwd=SRC`：
    默认值是在 `def` 那一刻绑定的，之后改模块里的 `SRC` 它就跟着不走 —— 而
    `tests/test_ai_e2e_fixture_self_consistency.py` 正是靠改 `SRC` 把整个造数过程
    挪进临时目录的（不挪的话，跑一次测试就把本地那份 e2e 仓库重新建一遍，
    正在联调平台的人会莫名其妙地掉数据）。
    """
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@example.com",
        "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@example.com",
    })
    env.update(env_extra or {})
    subprocess.run(["git", *args], cwd=str(cwd or SRC), env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _commit(message, when):
    _git("add", "-A")
    _git("commit", "-m", message, env_extra={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})


def _battle_lua():
    """lua 那份被改动文件的路径，只写一处 —— 三处各拼一遍迟早会分叉。"""
    return SRC / "code" / "qz_server" / "src" / "battle" / "BattleMgr.lua"


# ---------------------------------------------------------------------------
#  Excel
# ---------------------------------------------------------------------------

# 造数必须**逐字节可复现** —— 同一个 `build()` 跑两次要给出同一批提交哈希。
#
# 这条不是洁癖：平台里记的是**提交哈希**（`Repository.last_synced_tip`、`ai_analysis_run`
# 的基线、`commits_log` 的每一行）。造数只要每次重来都换一批哈希，重建一次就会让平台
# 已记录的那些身份**全部失效**，而症状是「同步成功、增量分析却对不上」这类看不出因果的怪事。
#
# 实测踩到过：连着两次 `build()` 给出的 c3 是 `2caae19` 与 `1d219e0`，差别只在
# openpyxl 写进 `docProps/core.xml` 的 `dcterms:created/modified`（它默认取**当下时刻**）。
# 那份时间戳落在 xlsx 字节里 → blob 变了 → 从 c1 起每一条提交的哈希全变。
_XLSX_STAMP = datetime(2026, 9, 1, 0, 0, 0)
# zip 条目头里的写入时刻（zip 能表示的最早时刻）。它同样落在文件字节里，所以也要钉死。
_ZIP_STAMP = (1980, 1, 1, 0, 0, 0)


def _new_workbook():
    """建一个**时间戳固定**的工作簿（理由见 `_XLSX_STAMP`）。"""
    from openpyxl import Workbook
    wb = Workbook()
    wb.properties.created = _XLSX_STAMP
    wb.properties.modified = _XLSX_STAMP
    return wb


def _save_workbook(workbook, path):
    """把工作簿写进 `path`，并**抹掉所有随时间变化的字节**。

    不能用 `workbook.save()`。它底下是 `openpyxl.writer.excel.save_workbook`，那里有一行
    `workbook.properties.modified = datetime.datetime.utcnow()` —— **无条件覆写**刚设好的
    值（`created` 不覆写，所以只改 `created` 会以为已经修好了）；而 zip 的每个条目头
    又各带一个写入时刻。两处都在文件字节里，git 认的就是字节。

    所以这里自己写：内容走 `ExcelWriter.write_data()`（它照 `workbook.properties` 写，
    不覆写时间戳），条目按文件名排序、时间戳钉成 `_ZIP_STAMP`，字节就完全确定了。
    """
    import io
    import zipfile

    from openpyxl.writer.excel import ExcelWriter

    workbook.properties.modified = _XLSX_STAMP
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        ExcelWriter(workbook, archive).write_data()
    buffer.seek(0)
    with zipfile.ZipFile(buffer) as source, zipfile.ZipFile(
        path, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for item in sorted(source.infolist(), key=lambda entry: entry.filename):
            info = zipfile.ZipInfo(item.filename, date_time=_ZIP_STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = item.external_attr
            target.writestr(info, source.read(item.filename))


def _write_items(rows, *, extra_header_rows=0):
    """物品表：表头第 1 行。"""
    wb = _new_workbook()
    ws = wb.active
    ws.title = "物品"
    for _ in range(extra_header_rows):
        ws.append([])
    ws.append(["id", "名称", "类型", "价格", "描述"])
    for row in rows:
        ws.append(row)
    return wb


def _write_skills(rows):
    """技能表：**表头在第 3 行**，上面两行是标题与说明（特殊表头）。"""
    wb = _new_workbook()
    ws = wb.active
    ws.title = "技能"
    ws.append(["技能配置表"])
    ws.append(["说明：冷却单位毫秒，伤害为基准值"])
    ws.append(["技能id", "技能名", "冷却", "伤害", "备注"])
    for row in rows:
        ws.append(row)
    return wb


ITEMS_V1 = [
    [1001, "初级药水", "消耗品", 50, "恢复 100 点生命"],
    [1002, "中级药水", "消耗品", 150, "恢复 300 点生命"],
    [1003, "铁剑", "武器", 200, "攻击力 +10"],
    [1004, "皮甲", "防具", 180, "防御力 +8"],
    [1005, "回城卷轴", "消耗品", 30, "回到主城"],
]

SKILLS_V1 = [
    [2001, "火球术", 3000, 120, "单体"],
    [2002, "冰霜新星", 8000, 90, "范围"],
    [2003, "治疗术", 6000, 200, "自身"],
]

# 第二轮：改价格、加物品；技能表调数值；代码里再动一处
ITEMS_V2 = [
    [1001, "初级药水", "消耗品", 60, "恢复 100 点生命"],
    [1002, "中级药水", "消耗品", 150, "恢复 300 点生命"],
    [1003, "铁剑", "武器", 200, "攻击力 +10"],
    [1004, "皮甲", "防具", 180, "防御力 +8"],
    [1005, "回城卷轴", "消耗品", 30, "回到主城"],
    [1006, "高级药水", "消耗品", 400, "恢复 800 点生命"],
    [1007, "秘银剑", "武器", 900, "攻击力 +35"],
]

SKILLS_V2 = [
    [2001, "火球术", 3000, 120, "单体"],
    [2002, "冰霜新星", 8000, 90, "范围"],
    [2003, "治疗术", 6000, 200, "自身"],
    [2004, "陨石术", 20000, 500, "范围，冷却与伤害需重新平衡"],
]

# `build()` 的 **c2 之后、到 tip 为止**，`config/物品表.xlsx` 就停在这个状态：
# 1005 降到 25，没有 1006/1007。`build()` 的 c3 只改技能表与战斗逻辑，没碰它。
#
# 单独提出来是因为**第三轮（`round3`）是 `build()` 的续集**，必须以这里为基 ——
# 见 `round3` 的说明（拿 `ITEMS_V2` 当基会把 1006/1007 悄悄塞进历史）。
ITEMS_AT_BUILD_TIP = ITEMS_V1[:4] + [[1005, "回城卷轴", "消耗品", 25, "回到主城"]]

LOGIC_V1 = '''# -*- coding: utf-8 -*-
"""战斗结算。配表读进来之后在这里算伤害。"""

DEFAULT_CRIT_RATE = 0.15


def calc_damage(base, attack, defense, crit_rate=None):
    """伤害 = (基础 + 攻击 - 防御)，暴击翻倍。"""
    rate = DEFAULT_CRIT_RATE if crit_rate is None else crit_rate
    raw = base + attack - defense
    if rate > 0 and _roll(rate):
        raw = raw * 2
    return max(raw, 1)


def mana_cost(skill_level, base_cost):
    """等级越高，蓝耗越低。"""
    return base_cost * (1 - 0.02 * skill_level)


def _roll(rate):
    import random
    return random.random() < rate
'''

# 第二轮：改一处真 bug（冷却单位换算），保留那个「看起来像 bug」的有意设计
LOGIC_V2 = LOGIC_V1.replace(
    "    return base_cost * (1 - 0.02 * skill_level)",
    "    # 等级上限 50，超过之后不再减免 —— 这是**有意**的，不是漏判\n"
    "    if skill_level > 50:\n"
    "        return float(base_cost)\n"
    "    return base_cost * (1 - 0.02 * skill_level)",
) + '''

def cooldown_seconds(raw):
    """配表里的冷却单位是毫秒，这里换成秒。"""
    return raw // 1000
'''

# 第六轮：把蓝耗减免的**上限**从「跳回原价」改成「收敛到下限」。
#
# 50 级那一段原先直接返回 `base_cost`，曲线是断的；这一版让它在线性减免算出的值低于
# 下限（原价的 20%）时钳到下限 —— 等级越高越省，但不会在 51 级突然翻五倍。50 这个
# 上限口径保留：上一轮报告的争议点是「50 是不是设计上限」，这里只修曲线的连续性。
#
# **下限写成字面量，不新加模块常量**：`LOGIC_V1/V2` 是已经 push 出去的历史，改它们会
# 换掉**每一条**提交的哈希（见 `_XLSX_STAMP` 那一段的同一类理由）；所以这一版必须自洽，
# 不能引用一个只存在于新代码里的名字。
LOGIC_V3 = LOGIC_V2.replace(
    "    # 等级上限 50，超过之后不再减免 —— 这是**有意**的，不是漏判\n"
    "    if skill_level > 50:\n"
    "        return float(base_cost)\n"
    "    return base_cost * (1 - 0.02 * skill_level)",
    "    # 等级上限 50，超过之后不再继续减免 —— 这是**有意**的。\n"
    "    # 减免到下限（原价的 20%）就钳住，不再像以前那样在 51 级跳回原价。\n"
    "    level = min(skill_level, 50)\n"
    "    return max(base_cost * (1 - 0.02 * level), base_cost * 0.2)",
)
assert LOGIC_V3 != LOGIC_V2, "round6 的替换没命中 —— LOGIC_V2 又被改过了？"


# ---------------------------------------------------------------------------
#  Lua 代码（2026-09-25 加）
# ---------------------------------------------------------------------------
# 为什么要加：这个仓库的真实被测数据是**配表 + lua 代码**两只脚，而造数原先只有 xlsx
# 与一个 `.py`。`.py` 走的是「代码正文」那条路没错，但它同时绕开了两样只有 lua 才会
# 碰到的东西：**方案里的 lua 提取**与**`code/` 这个目录前缀**（平台按路径前缀判资源
# 类型）。用 `.py` 验过的结论，不能直接当成「lua 也这样」。
#
# 埋的东西与 `LOGIC_V1/V2` 同一套口径：**一个真问题** + **一个看起来像 bug 的有意设计**。
#   * 真问题：`can_join_team` 的成员上限校验被删掉（注释还写着「校验移到客户端」）——
#     服务端不再拦，越权加队；
#   * 有意设计：`mana_cost` 里 `level > 50` 之后不再减免（注释写明是有意的）。
# v1 → v2 的 diff 就是「当前版本」那次全量分析要看的东西。
BATTLE_LUA_V1 = """-- 战斗管理：伤害结算与队伍校验
local BattleMgr = {}

local MAX_TEAM_MEMBER = 5

function BattleMgr.calc_damage(base, attack, defense)
    local raw = base + attack - defense
    if raw < 1 then
        raw = 1
    end
    return raw
end

function BattleMgr.mana_cost(level, base_cost)
    return base_cost * (1 - 0.02 * level)
end

function BattleMgr.can_join_team(team, player_id)
    if #team.members >= MAX_TEAM_MEMBER then
        return false
    end
    return true
end

return BattleMgr
"""

BATTLE_LUA_V2 = """-- 战斗管理：伤害结算与队伍校验
local BattleMgr = {}

-- 队伍成员上限改成由客户端与匹配服一起保证，服务端不再单独拦
function BattleMgr.calc_damage(base, attack, defense)
    local raw = base + attack - defense
    if raw < 1 then
        raw = 1
    end
    return raw
end

function BattleMgr.mana_cost(level, base_cost)
    -- 等级上限 50，超过之后不再减免 —— 这是**有意**的，不是漏判
    if level > 50 then
        return base_cost
    end
    return base_cost * (1 - 0.02 * level)
end

function BattleMgr.can_join_team(team, player_id)
    return true
end

return BattleMgr
"""


# 第四轮：`can_join_team` 从「一律放行」变成「按调用方给的上限兜底一次」。
#
# **这一笔必须真的改到行为**，不能只是一行注释：`round4` 的用途是「两笔提交各对应哪条
# 结论」，其中一笔若只是补注释，增量分析里就只剩一笔真改动，「归因到哪一笔」这件事
# 根本验不到（而且提交信息会与改动不符 —— 信息说改了上限归属，diff 里只有一行注释，
# 读报告的人分不清「模型看错了」还是「造数造歪了」）。
#
# 埋的点：`if not limit then return true end` —— **fail-open**。调用方忘记传上限时校验
# 静默消失，而提交信息说的正是「改由客户端与匹配服保证」。它与 `mana_cost` 那个
# 「看起来像 bug 的有意设计」相反：这是真问题，且这一轮才引入（上一轮是「一律返回 true」，
# 也是问题，所以增量报告应当把它写成「上次遗留、仍然成立」而不是「本次新增」）。
BATTLE_LUA_V3 = """-- 战斗管理：伤害结算与队伍校验
local BattleMgr = {}

function BattleMgr.calc_damage(base, attack, defense)
    local raw = base + attack - defense
    if raw < 1 then
        raw = 1
    end
    return raw
end

function BattleMgr.mana_cost(level, base_cost)
    -- 等级上限 50，超过之后不再减免 —— 这是**有意**的，不是漏判
    if level > 50 then
        return base_cost
    end
    return base_cost * (1 - 0.02 * level)
end

-- 队伍成员上限改由客户端与匹配服一起保证：服务端不再自己定常量，
-- 只按调用方（战斗服）传进来的 limit 兜底一次。
function BattleMgr.can_join_team(team, player_id, limit)
    if not limit then
        return true
    end
    return #team.members < limit
end

return BattleMgr
"""


# 第五轮：**只改这一个文件、只改这一处行为** —— 把 round4 的 fail-open 堵掉。
#
# 为什么要单独一轮、而且只动一个文件：增量分析这条路**要求 delta 小且不在关键路径上**
# （`scope_sampling._decide_scope`：`delta_count >= 50`、`delta/total >= 0.30`、
# 命中关键路径，三条各能把增量升格成全量）。而这个造数只有 4 个受跟踪文件、其中两个在
# `config/` 下 —— 任何一次「配表 + 代码」的改动都必然升格成**全量**（实测 job 42：
# 2/4 = 0.50 触发 `delta_ratio_high`）。所以「增量能不能跑起来」这件事，只有拿
# **单文件、非关键路径**的 delta 才验得到。
#
# 改的内容对着上一轮报告里那条「上次遗留（仍成立）」：服务端不传上限时直接放行。
# 这一版补回服务端自己的兜底常量 —— 于是下一轮的增量报告要回答「那条旧结论现在还算不算数」。
BATTLE_LUA_V4 = """-- 战斗管理：伤害结算与队伍校验
local BattleMgr = {}

-- 调用方没传上限时用这个兜底：服务端不能完全不设防。
local DEFAULT_TEAM_MEMBER_LIMIT = 5

function BattleMgr.calc_damage(base, attack, defense)
    local raw = base + attack - defense
    if raw < 1 then
        raw = 1
    end
    return raw
end

function BattleMgr.mana_cost(level, base_cost)
    -- 等级上限 50，超过之后不再减免 —— 这是**有意**的，不是漏判
    if level > 50 then
        return base_cost
    end
    return base_cost * (1 - 0.02 * level)
end

function BattleMgr.can_join_team(team, player_id, limit)
    local effective = limit or DEFAULT_TEAM_MEMBER_LIMIT
    return #team.members < effective
end

return BattleMgr
"""

# 第七轮：**只改这个文件**，改动是一件正常的小重构 —— 兜底上限从文件内的 `local` 挂到
# 模块上导出，客户端与编辑器按同一个数读。它**不是**修 bug、也不制造 bug。
#
# 为什么这一轮要挑这个文件（这一轮的用途只有一个：验旧结论的**收口通道**）：
# `BATTLE_LUA_V4` 已经把「服务端不传上限就放行」那条修好了，但模型没有任何结构化通道
# 能说「这条已经修好了」—— 实测 run 73 在正文里写了「已修复」，run 74 又把它写成
# 「上次遗留，仍成立」（同一个事实在两轮报告里翻转）。这一轮让这个文件重新进 delta，
# 合并器把它判成 `needs_recheck`，模型被要求重新看它一眼，正好撞上那条旧结论。
BATTLE_LUA_V5 = """-- 战斗管理：伤害结算与队伍校验
local BattleMgr = {}

-- 队伍人数上限的兜底值：挂在模块上导出，客户端与编辑器按同一个数读
-- （原先只是文件内的 local，别的模块要读只能各自再抄一份）。
BattleMgr.DEFAULT_TEAM_MEMBER_LIMIT = 5

function BattleMgr.calc_damage(base, attack, defense)
    local raw = base + attack - defense
    if raw < 1 then
        raw = 1
    end
    return raw
end

function BattleMgr.mana_cost(level, base_cost)
    -- 等级上限 50，超过之后不再减免 —— 这是**有意**的，不是漏判
    if level > 50 then
        return base_cost
    end
    return base_cost * (1 - 0.02 * level)
end

function BattleMgr.can_join_team(team, player_id, limit)
    local effective = limit or BattleMgr.DEFAULT_TEAM_MEMBER_LIMIT
    return #team.members < effective
end

return BattleMgr
"""

# 第八轮：与 `BATTLE_LUA_V5` 同一个文件、同一类小重构。用途只有一个：让这个文件再进一次
# delta，复测模型的收口通道有没有真的被叫起来（第七轮那次没叫起来，见 `CLOSURE_RULE`）。
BATTLE_LUA_V6 = """-- 战斗管理：伤害结算与队伍校验
local BattleMgr = {}

-- 队伍人数上限的兜底值：挂在模块上导出，客户端与编辑器按同一个数读
BattleMgr.DEFAULT_TEAM_MEMBER_LIMIT = 5

function BattleMgr.calc_damage(base, attack, defense)
    local raw = base + attack - defense
    if raw < 1 then
        raw = 1
    end
    return raw
end

function BattleMgr.mana_cost(level, base_cost)
    -- 等级上限 50，超过之后不再减免 —— 这是**有意**的，不是漏判
    if level > 50 then
        return base_cost
    end
    return base_cost * (1 - 0.02 * level)
end

-- 队伍是否已满：调用方拿它做前置判断，不必各写一遍 `#team.members >= limit`。
function BattleMgr.team_is_full(team, limit)
    local effective = limit or BattleMgr.DEFAULT_TEAM_MEMBER_LIMIT
    return #team.members >= effective
end

function BattleMgr.can_join_team(team, player_id, limit)
    return not BattleMgr.team_is_full(team, limit)
end

return BattleMgr
"""


def _reset_origin():
    """平台会去 fetch/checkout/pull 这个 url，所以它必须是**裸库**。

    不能把工作副本本身当 url：`clone_or_update_repository` 的更新分支会
    `git pull --no-rebase origin master`，失败后还有一条自愈路径会 `reset --hard`
    + `clean -fd`，而 `force_reclone` 更会直接把那个目录删掉重来
    （`services/repository_maintenance_api_service.py:138-155`）。裸库不会被碰。
    """
    if ORIGIN.exists():
        _rmtree(ORIGIN)
    subprocess.run(["git", "init", "--bare", "-q", str(ORIGIN)], check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/master"],
                   cwd=str(ORIGIN), check=True)


def build():
    BASE.mkdir(parents=True, exist_ok=True)
    _reset_origin()
    if SRC.exists():
        _rmtree(SRC)
    SRC.mkdir(parents=True)
    _git("init", "-q", "-b", "master")
    _git("remote", "add", "origin", ORIGIN.as_posix())

    (SRC / "config").mkdir()
    (SRC / "src").mkdir()
    (SRC / "code" / "qz_server" / "src" / "battle").mkdir(parents=True)

    # ── c1：初始导入 ────────────────────────────────────────────────
    _save_workbook(_write_items(ITEMS_V1), SRC / "config" / "物品表.xlsx")
    _save_workbook(_write_skills(SKILLS_V1), SRC / "config" / "技能表.xlsx")
    (SRC / "src" / "battle_logic.py").write_text(LOGIC_V1, encoding="utf-8")
    _battle_lua().write_text(BATTLE_LUA_V1, encoding="utf-8")
    _commit("初始导入配表与战斗逻辑", "2026-09-15T10:00:00+08:00")

    # ── c2：一次正常的改动 ─────────────────────────────────────────
    # 这一版就是 `ITEMS_AT_BUILD_TIP`（第三轮以它为基，别在这里现写一份 —— 两处各写
    # 一份的话，c2 改了而 round3 没跟着改，round3 的「干净小 delta」就又脏了）。
    _save_workbook(_write_items(ITEMS_AT_BUILD_TIP), SRC / "config" / "物品表.xlsx")
    (SRC / "src" / "battle_logic.py").write_text(
        LOGIC_V1.replace("def calc_damage", "def calc_damage_v1"), encoding="utf-8")
    _commit("回城卷轴降价，伤害函数改名", "2026-09-18T14:00:00+08:00")

    # ── c3：全量分析的「当前版本」 ─────────────────────────────────
    _save_workbook(_write_skills(SKILLS_V1 + [[2004, "陨石术", 20000, 500, "范围"]]),
                   SRC / "config" / "技能表.xlsx")
    (SRC / "src" / "battle_logic.py").write_text(LOGIC_V2, encoding="utf-8")
    _battle_lua().write_text(BATTLE_LUA_V2, encoding="utf-8")
    _commit("新增陨石术，蓝耗加上等级上限", "2026-09-20T11:00:00+08:00")
    _git("push", "-q", "-u", "origin", "master")

    print("已生成:", SRC, "→", ORIGIN)
    print(subprocess.run(["git", "log", "--oneline", "--format=%h %ci %s"],
                         cwd=str(SRC), capture_output=True, text=True).stdout)


def round2():
    """第二轮：三件改动，其中一件是**回填日期**的。"""
    _save_workbook(_write_items(ITEMS_V2), SRC / "config" / "物品表.xlsx")
    _commit("初级药水涨价，新增高级药水与秘银剑", "2026-09-22T10:00:00+08:00")

    _save_workbook(_write_skills(SKILLS_V2), SRC / "config" / "技能表.xlsx")
    # ★ 回填：日期比上一条更早，推送却在之后 —— 老口径的 `--since` 会被它整个停住
    _commit("技能表补录陨石术备注", "2026-09-19T09:00:00+08:00")

    # 这一笔在内容没变时会成空提交 —— `_commit` 用 check=True，会当场抛出来。
    # 上一次它是**静默**失败的：调用方 `subprocess.run(...)` 没带 check，push 就没跑。
    (SRC / "src" / "battle_logic.py").write_text(
        LOGIC_V2 + chr(10) + "# e2e: 第二轮补注释" + chr(10), encoding="utf-8")
    _commit("战斗逻辑补注释", "2026-09-22T10:30:00+08:00")
    _git("push", "-q", "origin", "master")

    print("第二轮完成:")
    print(subprocess.run(["git", "log", "--oneline", "--format=%h %ci %s"],
                         cwd=str(SRC), capture_output=True, text=True).stdout)


def round3():
    """第三轮：**只改一个文件里的一格**（铁剑价格），让增量做差有一个干净的小 delta。

    ## 它必须以 `build()` 的产物为基，**不是** `ITEMS_V2`

    原先这里拿 `ITEMS_V2` 当基，而 `ITEMS_V2` 含 1006（高级药水）/ 1007（秘银剑）两行。
    `round2()` 全仓**从来没有被调用过**（`phases.py` 只调 `round3`），所以在一个
    `build()` 出来的仓库上跑 `phases.py incremental`，那笔「干净的小 delta」会**新增
    1006/1007** —— 而真实 Git 历史里这两个 ID 从未存在过。

    实测 run 52 / run 53 的报告把它们写成「本期删除」的高风险，污染源就是这里：
    那个仓库是 `build` + `round3` 造出来的，从没跑过 `round2`。

    ## `round2` 与 `round3` 是 `build()` 之后**两条互斥的续集**

    两条都以 `build()` 的 tip 为起点，各自往下走一步。跑了 `round2`（真加了 1006/1007）
    之后再跑 `round3`，就会把这两行又删回去 —— 那不是「干净的小 delta」，是另一件事。
    要验「新增两行」那条路就单独跑 `round2`，别接 `round3`。
    """
    items = [list(row) for row in ITEMS_AT_BUILD_TIP]
    for row in items:
        if row[0] == 1003:
            row[3] = 260
    _save_workbook(_write_items(items), SRC / "config" / "物品表.xlsx")
    _commit("铁剑涨价到 260", "2026-09-23T09:00:00+08:00")
    _git("push", "-q", "origin", "master")
    print(subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(SRC),
                         capture_output=True, text=True).stdout)


def round4():
    """第四轮：**两个提交**，模拟「改了几笔」之后的增量分析。

    与 `round3` 的分工：`round3` 是**一笔干净的 delta**（只改铁剑价格一格），用来钉住
    「增量做差能不能归因到那一格」；这一轮**故意是两笔、两个文件、两种类型**
    （配表 + lua 代码），因为增量分析真正要回答的是「这一次改的那几笔，各自有没有问题」
    —— 只有一笔的时候，「哪一笔对应哪条结论」这件事根本验不到。

    两笔之间**不留空提交**：lua 那一笔真的改了行为（`can_join_team` 由「一律放行」改成
    「按传入的 limit 兜底」，见 `BATTLE_LUA_V3`），提交信息与改动一致。
    """
    items = [list(row) for row in ITEMS_AT_BUILD_TIP]
    for row in items:
        if row[0] == 1002:
            row[3] = 175
    _save_workbook(_write_items(items), SRC / "config" / "物品表.xlsx")
    _commit("中级药水涨价到 175", "2026-09-24T10:00:00+08:00")

    _battle_lua().write_text(BATTLE_LUA_V3, encoding="utf-8")
    _commit("队伍成员上限改由客户端保证", "2026-09-24T10:30:00+08:00")
    _git("push", "-q", "origin", "master")
    print(subprocess.run(["git", "log", "--oneline", "--format=%h %ci %s"],
                         cwd=str(SRC), capture_output=True, text=True).stdout)


def round5():
    """第五轮：**只改 lua 一个文件**，用来真正走到增量分析那条路。

    与 `round4` 的分工：`round4` 是「两笔、两类文件」，它必然被升格成全量（见
    `BATTLE_LUA_V4` 上面的说明）；这一轮是「一笔、一个非关键路径文件」，delta/total
    = 1/4 = 0.25 < 0.30，且路径里没有 `config/` —— 三条升格条件一条都不命中，
    `_decide_scope` 才会返回 `("incremental", "delta_small")`。
    """
    _battle_lua().write_text(BATTLE_LUA_V4, encoding="utf-8")
    _commit("服务端补回队伍人数兜底上限", "2026-09-25T10:00:00+08:00")
    _git("push", "-q", "origin", "master")
    print(subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(SRC),
                         capture_output=True, text=True).stdout)


def round6():
    """第六轮：再一笔**单文件、非关键路径**的改动（`src/battle_logic.py`），跑增量。

    改的内容对着前两轮报告里那条「上次遗留（仍成立）」的蓝耗曲线：`> 50` 时直接跳回
    全额，于是 50 级近乎不耗蓝、51 级突然恢复原价。这一版把它改成**收敛到设计下限**，
    而不是跳回原价 —— 也就是上一轮报告自己在「缓解」里写的那条做法。

    为什么还是单文件：与 `round5` 同一条理由（见 `BATTLE_LUA_V4` 上面的说明）——
    delta 得小且不在关键路径上，否则 `_decide_scope` 会把它升格成全量。
    """
    (SRC / "src" / "battle_logic.py").write_text(LOGIC_V3, encoding="utf-8")
    _commit("蓝耗减免改成收敛到下限，不再跳回原价", "2026-09-25T10:30:00+08:00")
    _git("push", "-q", "origin", "master")
    print(subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(SRC),
                         capture_output=True, text=True).stdout)


def round7():
    """第七轮：**只改 `code/qz_server/src/battle/BattleMgr.lua`**（单文件、非关键路径）。

    为什么这一轮要挑这个文件：第六轮之后，上一轮报告里那两条「服务端队伍人数上限」的
    结论其实**早就修好了**（`BATTLE_LUA_V4` 补回了兜底常量），但模型没有任何结构化通道
    能说这句话 —— 实测 run 73 在正文里写了「已修复」，run 74 又把它写成「上次遗留，
    仍成立」。这一轮让这个文件**重新进入 delta**（合并器于是把它判成 `needs_recheck`），
    模型会被要求重新看它一眼，正好用来验 `baseline_updates` 这条收口通道。

    改动本身是一件正常的小重构：把兜底上限从文件内的 `local` 挂到模块上导出，客户端与
    编辑器按同一个数读，不必各处再抄一份。**不是修 bug、也不制造 bug** —— 这一轮要看的
    是「模型有没有把已经修好的旧结论如实收口」，而不是它能不能发现新问题。

    为什么还是单文件：见 `BATTLE_LUA_V4` 上面的说明 —— delta/total = 1/4 = 0.25 < 0.30，
    且路径里没有 `config/`，三条升格条件一条都不命中。
    """
    _battle_lua().write_text(BATTLE_LUA_V5, encoding="utf-8")
    _commit("队伍上限兜底值导出到模块，客户端与编辑器按同一个数读",
            "2026-09-25T11:00:00+08:00")
    _git("push", "-q", "origin", "master")
    print(subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(SRC),
                         capture_output=True, text=True).stdout)


def round8():
    """第八轮：**再改一次服务端 lua**（单文件、非关键路径），用来复测收口通道。

    与 `round7` 同一手法、同一目的：那两条「服务端队伍人数上限」的结论在冻结版本上早就
    修好了，而模型上一轮只在正文里说了「已修复」、结构化字段里什么都没交（见
    `baseline.CLOSURE_RULE` 的 docstring）。这一轮让同一个文件重新进 delta（合并器判成
    `needs_recheck`），再看一次它交不交 `baseline_updates`。

    改的是一件正常的小重构：把「队伍是否已满」抽成一个函数，调用处不再各写一遍
    `#team.members >= limit`。**不是修 bug、也不制造 bug。**
    """
    _battle_lua().write_text(BATTLE_LUA_V6, encoding="utf-8")
    _commit("队伍是否已满抽成一个函数，调用处不再各写一遍判断",
            "2026-09-25T11:30:00+08:00")
    _git("push", "-q", "origin", "master")
    print(subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(SRC),
                         capture_output=True, text=True).stdout)


if __name__ == "__main__":
    {"build": build, "round2": round2, "round3": round3, "round4": round4,
     "round5": round5, "round6": round6, "round7": round7,
     "round8": round8}[sys.argv[1] if len(sys.argv) > 1 else "build"]()
