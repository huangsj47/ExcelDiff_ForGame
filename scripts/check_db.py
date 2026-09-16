"""快速查看 SQLite 库里的表，以及 commit 相关表的结构与样例行（开发期排查用）。

只认 SQLite 文件：直接连 instance/ 下的库文件，不读 DATABASE_URL / DB_BACKEND，
所以平台若配成 MySQL，这个脚本看到的是另一个库。
"""

import pathlib
import sqlite3
import sys
from pathlib import Path

# `python scripts/check_db.py` 时 sys.path[0] 是 scripts/，不是仓库根，
# 下面 from utils... 会 ModuleNotFoundError（原先躺在根目录才恰好能跑）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.runtime_paths import resolve_runtime_path  # noqa: E402

# 锚定仓库根而不是 CWD：原先是 sqlite3.connect('instance/diff_platform.db')，
# 在别的目录下执行时 sqlite3 会**静默建出一个空库**，然后打印「没有任何表」，
# 看起来像数据库坏了。同类的 CWD 相对路径问题见 utils/runtime_paths.py。
# 可选位置参数：不传则看仓库默认库，传了就看指定的那个文件。
# 有了它，测试才能拿一个「确定不存在」的路径来验证下面的拒绝分支，
# 而不是依赖「本机恰好还没有 instance/diff_platform.db」。
if len(sys.argv) > 1:
    db_path = str(pathlib.Path(sys.argv[1]))
else:
    db_path = resolve_runtime_path("instance/diff_platform.db")
print(f"数据库文件: {db_path}")

if not Path(db_path).exists():
    # sqlite3.connect 对不存在的路径是「创建」而不是报错。这个脚本的用途是**查看**
    # 已有库；放任它建出一个空库再打印「没有任何表」，只会把人引到错误的结论上。
    raise SystemExit(
        f"数据库文件不存在: {db_path}\n"
        "（本脚本只读 SQLite 文件，不负责建库；先启动一次平台或用 scripts/init_database.py 建表）"
    )

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 检查所有表
cursor.execute("SELECT name FROM `sqlite_master` WHERE type='table'")
tables = cursor.fetchall()
print('Available tables:', [table[0] for table in tables])

# 检查commit相关的表结构
for table_name in [table[0] for table in tables]:
    if 'commit' in table_name.lower():
        print(f'\nTable: {table_name}')
        cursor.execute(f"PRAGMA table_info(`{table_name}`)")
        columns = cursor.fetchall()
        for col in columns:
            print(f"  {col[1]} ({col[2]})")
        
        # 查看前几条记录
        cursor.execute(f"SELECT * FROM `{table_name}` LIMIT 3")
        rows = cursor.fetchall()
        print(f"  Sample data: {rows}")

conn.close()
