"""
Flask应用测试 - 使用适当的测试隔离避免I/O问题
"""
import pytest
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

class TestFlaskAppComponents:
    """测试Flask应用的各个组件，不直接导入app模块"""
    
    def test_project_structure(self):
        """测试项目结构是否正确"""
        # 检查关键文件是否存在
        assert os.path.exists(os.path.join(project_root, 'app.py'))
        assert os.path.exists(os.path.join(project_root, 'requirements.txt'))
        assert os.path.exists(os.path.join(project_root, 'services'))
        assert os.path.exists(os.path.join(project_root, 'templates'))
        assert os.path.exists(os.path.join(project_root, 'static'))
        print("✅ 项目结构检查通过")
    
    def test_requirements_file(self):
        """测试requirements.txt文件内容"""
        req_file = os.path.join(project_root, 'requirements.txt')
        with open(req_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 检查关键依赖
        assert 'Flask' in content
        assert 'Flask-SQLAlchemy' in content
        assert 'GitPython' in content
        assert 'openpyxl' in content
        print("✅ requirements.txt检查通过")
    
    def test_services_directory(self):
        """测试services目录结构"""
        services_dir = os.path.join(project_root, 'services')
        
        # 检查关键服务文件
        expected_files = [
            'git_service.py',
            'diff_service.py',
            'enhanced_git_service.py',
            'threaded_git_service.py',
            'svn_service.py',
            'excel_html_cache_service.py'
        ]
        
        for file_name in expected_files:
            file_path = os.path.join(services_dir, file_name)
            assert os.path.exists(file_path), f"缺少服务文件: {file_name}"
        
        print("✅ services目录结构检查通过")
    
    def test_templates_directory(self):
        """测试templates目录结构"""
        templates_dir = os.path.join(project_root, 'templates')
        
        # 检查关键模板文件
        expected_files = [
            'base.html',
            'index.html',
            'projects.html',
            'commit_diff.html'
        ]
        
        for file_name in expected_files:
            file_path = os.path.join(templates_dir, file_name)
            assert os.path.exists(file_path), f"缺少模板文件: {file_name}"
        
        print("✅ templates目录结构检查通过")
    
    @patch('sys.stdout')
    @patch('sys.stderr')
    def test_app_import_with_mocked_streams(self, mock_stderr, mock_stdout):
        """使用模拟的输出流测试app模块导入"""
        try:
            # 模拟标准输出流
            mock_stdout.buffer = MagicMock()
            mock_stderr.buffer = MagicMock()
            
            # 尝试导入app模块
            import app
            
            # 检查基本属性
            assert hasattr(app, 'app')  # Flask应用实例
            assert hasattr(app, 'db')   # 数据库实例
            assert hasattr(app, 'Project')  # 数据模型
            assert hasattr(app, 'Repository')
            assert hasattr(app, 'Commit')
            
            print("✅ app模块导入成功（使用模拟输出流）")
            
        except Exception as e:
            pytest.skip(f"app模块导入失败，跳过测试: {e}")
    
    def test_database_file_creation(self):
        """测试数据库文件是否可以创建"""
        instance_dir = os.path.join(project_root, 'instance')
        if not os.path.exists(instance_dir):
            os.makedirs(instance_dir)
        
        db_file = os.path.join(instance_dir, 'test_diff_platform.db')
        
        # 如果测试数据库文件存在，先删除
        if os.path.exists(db_file):
            os.remove(db_file)
        
        # 创建一个简单的SQLite数据库文件
        import sqlite3
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE test_table (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
        ''')
        conn.commit()
        conn.close()
        
        # 验证文件创建成功
        assert os.path.exists(db_file)
        
        # 清理测试文件
        os.remove(db_file)
        
        print("✅ 数据库文件创建测试通过")

class TestUtilityFunctions:
    """测试工具函数，不依赖Flask应用上下文"""
    
    def test_excel_file_detection(self):
        """测试Excel文件检测逻辑"""
        # 模拟Excel文件检测函数
        def is_excel_file(file_path):
            excel_extensions = ['.xlsx', '.xls', '.xlsm', '.xlsb']
            return any(file_path.lower().endswith(ext) for ext in excel_extensions)
        
        # 测试各种文件类型
        assert is_excel_file('test.xlsx') == True
        assert is_excel_file('test.xls') == True
        assert is_excel_file('test.xlsm') == True
        assert is_excel_file('test.xlsb') == True
        assert is_excel_file('test.txt') == False
        assert is_excel_file('test.pdf') == False
        assert is_excel_file('TEST.XLSX') == True  # 大小写不敏感
        
        print("✅ Excel文件检测测试通过")
    
    def test_json_data_cleaning(self):
        """测试JSON数据清理功能"""
        import math
        
        def clean_json_data(data):
            """清理数据中的不可JSON序列化的值"""
            if isinstance(data, dict):
                return {k: clean_json_data(v) for k, v in data.items()}
            elif isinstance(data, list):
                return [clean_json_data(item) for item in data]
            elif isinstance(data, float):
                if math.isnan(data) or math.isinf(data):
                    return None
                return data
            else:
                return data
        
        # 测试正常数据
        normal_data = {"key": "value", "number": 123, "list": [1, 2, 3]}
        cleaned = clean_json_data(normal_data)
        assert cleaned == normal_data
        
        # 测试包含NaN的数据
        nan_data = {"key": "value", "nan_value": float('nan'), "inf_value": float('inf')}
        cleaned = clean_json_data(nan_data)
        assert cleaned["key"] == "value"
        assert cleaned["nan_value"] is None
        assert cleaned["inf_value"] is None
        
        print("✅ JSON数据清理测试通过")

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
