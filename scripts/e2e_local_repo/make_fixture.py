# -*- coding: utf-8 -*-
"""造一份「少量」的本地 git 测试数据：小的 Excel 配表 + 一个小的代码模块。

设计意图（每一条都对应一个要验的判据）：

* `config/物品表.xlsx` —— 表头在第 1 行（最常见的形态），改动它验增量做差；
* `config/技能表.xlsx` —— **表头在第 3 行**（上面两行是标题/说明），走的是「特殊表头」
  那条路，验多套表头方案会不会被认出来；
* `src/battle_logic.py` —— 代码正文那条路（`AI 读代码正文的两条来源`）。里面埋**一个真
  问题**与**一个看起来像 bug 的有意设计**（`min_severity/min_confidence=high` 的配置下
  既要报得出来、又不能报错），与 tests/test_ai_live_endpoint.py 的双向检查同一手法。

第二轮的提交里有一个是**回填日期**的（`GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` 设成比
上一个提交更早），专治 `commit-dates-are-backfilled` 那条：老口径 `git log --since`
遇到这种 tip 会停住整个遍历。
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 生成物（裸库 + 工作副本）落在 .pytest_tmp 下 —— 它已在 .gitignore 里，
# 进库的只有这几个脚本。
BASE = ROOT / ".pytest_tmp" / "e2e"
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


# ---------------------------------------------------------------------------
#  Excel
# ---------------------------------------------------------------------------

def _write_items(rows, *, extra_header_rows=0):
    """物品表：表头第 1 行。"""
    from openpyxl import Workbook
    wb = Workbook()
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
    from openpyxl import Workbook
    wb = Workbook()
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

    # ── c1：初始导入 ────────────────────────────────────────────────
    _write_items(ITEMS_V1).save(SRC / "config" / "物品表.xlsx")
    _write_skills(SKILLS_V1).save(SRC / "config" / "技能表.xlsx")
    (SRC / "src" / "battle_logic.py").write_text(LOGIC_V1, encoding="utf-8")
    _commit("初始导入配表与战斗逻辑", "2026-09-15T10:00:00+08:00")

    # ── c2：一次正常的改动 ─────────────────────────────────────────
    # 这一版就是 `ITEMS_AT_BUILD_TIP`（第三轮以它为基，别在这里现写一份 —— 两处各写
    # 一份的话，c2 改了而 round3 没跟着改，round3 的「干净小 delta」就又脏了）。
    _write_items(ITEMS_AT_BUILD_TIP).save(SRC / "config" / "物品表.xlsx")
    (SRC / "src" / "battle_logic.py").write_text(
        LOGIC_V1.replace("def calc_damage", "def calc_damage_v1"), encoding="utf-8")
    _commit("回城卷轴降价，伤害函数改名", "2026-09-18T14:00:00+08:00")

    # ── c3：全量分析的「当前版本」 ─────────────────────────────────
    _write_skills(SKILLS_V1 + [[2004, "陨石术", 20000, 500, "范围"]]).save(
        SRC / "config" / "技能表.xlsx")
    (SRC / "src" / "battle_logic.py").write_text(LOGIC_V2, encoding="utf-8")
    _commit("新增陨石术，蓝耗加上等级上限", "2026-09-20T11:00:00+08:00")
    _git("push", "-q", "-u", "origin", "master")

    print("已生成:", SRC, "→", ORIGIN)
    print(subprocess.run(["git", "log", "--oneline", "--format=%h %ci %s"],
                         cwd=str(SRC), capture_output=True, text=True).stdout)


def round2():
    """第二轮：三件改动，其中一件是**回填日期**的。"""
    _write_items(ITEMS_V2).save(SRC / "config" / "物品表.xlsx")
    _commit("初级药水涨价，新增高级药水与秘银剑", "2026-09-22T10:00:00+08:00")

    _write_skills(SKILLS_V2).save(SRC / "config" / "技能表.xlsx")
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
    _write_items(items).save(SRC / "config" / "物品表.xlsx")
    _commit("铁剑涨价到 260", "2026-09-23T09:00:00+08:00")
    _git("push", "-q", "origin", "master")
    print(subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(SRC),
                         capture_output=True, text=True).stdout)


if __name__ == "__main__":
    {"build": build, "round2": round2, "round3": round3}[
        sys.argv[1] if len(sys.argv) > 1 else "build"]()
