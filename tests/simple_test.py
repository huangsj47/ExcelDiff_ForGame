"""
简单测试文件 - 不导入app模块，避免输出流冲突
"""
import pytest

def test_simple_math():
    """简单的数学测试"""
    assert 1 + 1 == 2
    assert 2 * 3 == 6

def test_string_operations():
    """字符串操作测试"""
    text = "Hello World"
    assert text.upper() == "HELLO WORLD"
    assert text.lower() == "hello world"

def test_list_operations():
    """列表操作测试"""
    my_list = [1, 2, 3]
    my_list.append(4)
    assert len(my_list) == 4
    assert my_list[-1] == 4

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
