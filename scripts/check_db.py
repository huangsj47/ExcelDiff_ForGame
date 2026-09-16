import sqlite3

conn = sqlite3.connect('instance/diff_platform.db')
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
