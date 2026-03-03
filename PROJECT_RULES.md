# 项目开发规则

## 📏 文件长度限制规则

为保持代码可维护性，项目对 Python 源文件的行数设置了以下阈值：

| 级别 | 阈值 | 说明 |
|------|------|------|
| ⚠️ WARNING | ≥ 1500 行 | 提醒开发者考虑拆分 |
| ❌ ERROR | ≥ 2000 行 | 新功能**必须**写入新文件或已有的较短文件 |

### 检查方式

```bash
# 日常检查
python scripts/check_file_length.py

# CI / pre-commit（超限返回非零退出码）
python scripts/check_file_length.py --strict
```

### 拆分指导

当文件接近或超过阈值时，优先考虑以下拆分策略：

1. **业务逻辑** → `services/` 目录（例如 `services/commit_diff_logic.py`）
2. **路由处理器** → `routes/` 目录（例如 `routes/core_management_routes.py`）
3. **工具函数** → `utils/` 目录
4. **数据模型** → `models/` 目录

使用**依赖注入 (DI)** 模式（`configure_xxx()` 函数）避免循环导入。

## 🧪 测试要求

- 拆分前后必须运行完整测试套件：`python -m pytest tests/ -q`
- 源码审计类测试（检查特定字符串在文件中的位置）需要同步更新文件路径
