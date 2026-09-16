"""
简化的HTML缓存表初始化脚本
通过Flask应用上下文直接创建表
"""
import sys
from pathlib import Path

# 必须**早于**下面所有 `from services...`：`python scripts/init_html_cache.py` 时
# sys.path[0] 是 scripts/，不是仓库根，晚一步就是 ModuleNotFoundError。
#
# 这里原先也有一行 sys.path.insert，但写在了 `from services.model_loader import ...`
# **之后** —— 对本次导入已经来不及，是一句死代码；它之所以看起来「能用」，
# 只是因为文件当时躺在仓库根目录、sys.path[0] 恰好就是根。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.model_loader import get_runtime_models  # noqa: E402


def init_html_cache_table():
    """初始化HTML缓存表"""
    try:
        app, db, ExcelHtmlCache = get_runtime_models("app", "db", "ExcelHtmlCache")
        import os

        with app.app_context():
            print("🚀 开始初始化HTML缓存表...")

            # 确保instance目录存在
            instance_dir = 'instance'
            if not os.path.exists(instance_dir):
                try:
                    os.makedirs(instance_dir)
                    print(f"✅ 创建instance目录: {os.path.abspath(instance_dir)}")
                except Exception as e:
                    print(f"❌ 创建instance目录失败: {e}")
                    return False
            else:
                print(f"ℹ️ instance目录已存在: {os.path.abspath(instance_dir)}")

            # 创建所有表（如果不存在）
            db.create_all()
            print("✅ 数据库表创建完成")
            
            # 验证表是否创建成功
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            tables = inspector.get_table_names()
            
            if 'excel_html_cache' in tables:
                print("✅ excel_html_cache表创建成功")
                
                # 显示表结构
                columns = inspector.get_columns('excel_html_cache')
                print(f"📋 表结构验证 - 共 {len(columns)} 个字段:")
                for col in columns:
                    print(f"   - {col['name']} ({col['type']})")
                
                return True
            else:
                print("❌ excel_html_cache表创建失败")
                return False
                
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = init_html_cache_table()
    if success:
        print("\n🎉 HTML缓存表初始化成功！")
        print("\n📝 接下来可以:")
        print("   1. 运行 python app.py 启动应用")
        print("   2. 访问 /admin/excel-cache 查看缓存管理界面")
        print("   3. 访问任意Excel文件差异页面测试缓存功能")
    else:
        print("\n❌ 初始化失败，请检查错误信息")
        sys.exit(1)
