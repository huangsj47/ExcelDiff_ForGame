@echo off
chcp 65001 >nul 2>&1
title 配表代码版本Diff平台

echo ========================================
echo   配表代码版本Diff平台 - 启动脚本
echo ========================================
echo.

:: 检查 Python 是否安装
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8+ 并添加到 PATH
    pause
    exit /b 1
)

:: 显示 Python 版本
echo [信息] 检测到 Python:
python --version
echo.

:: 检查虚拟环境是否存在，不存在则创建
if not exist "venv" (
    echo [信息] 未检测到虚拟环境，正在创建...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [警告] 虚拟环境创建失败，将使用全局 Python 环境
        goto :install_deps_global
    )
    echo [信息] 虚拟环境创建成功
)

:: 激活虚拟环境
echo [信息] 激活虚拟环境...
call venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo [警告] 虚拟环境激活失败，将使用全局 Python 环境
    goto :install_deps_global
)

:install_deps
:: 安装依赖
if exist "requirements.txt" (
    echo [信息] 正在检查并安装依赖...
    pip install -r requirements.txt -q
    if %errorlevel% neq 0 (
        echo [警告] 部分依赖安装可能失败，尝试继续启动...
    ) else (
        echo [信息] 依赖安装完成
    )
) else (
    echo [警告] 未找到 requirements.txt 文件
)
goto :start_app

:install_deps_global
if exist "requirements.txt" (
    echo [信息] 正在检查并安装依赖（全局环境）...
    pip install -r requirements.txt -q
    if %errorlevel% neq 0 (
        echo [警告] 部分依赖安装可能失败，尝试继续启动...
    ) else (
        echo [信息] 依赖安装完成
    )
)

:start_app
echo.

:: 检查 .env 配置文件，不存在则自动生成（含随机密钥）
if not exist ".env" (
    echo [提示] 未检测到 .env 配置文件，正在自动生成...
    python -c "import secrets; fk=secrets.token_urlsafe(48); ap=secrets.token_urlsafe(16); at=secrets.token_urlsafe(32); lines=['# ============================================================','#  配表代码版本Diff平台 - 环境配置文件 (自动生成)','# ============================================================','','# 服务器配置','HOST=0.0.0.0','PORT=8002','','# Flask 安全密钥 (已自动随机生成)',f'FLASK_SECRET_KEY={fk}','','# 管理员账号配置','ADMIN_USERNAME=admin',f'ADMIN_PASSWORD={ap}','',f'# 管理员 API Token',f'ADMIN_API_TOKEN={at}','','# 是否启用管理员安全校验 (true/false)','ENABLE_ADMIN_SECURITY=true','','# 数据库配置','DB_BACKEND=sqlite','','# 调试与日志','DEBUG_LOG=false','','# 分支刷新冷却时间 (秒)','BRANCH_REFRESH_COOLDOWN_SECONDS=120']; f=open('.env','w',encoding='utf-8'); f.write('\n'.join(lines)+'\n'); f.close(); print(f'  FLASK_SECRET_KEY = {fk}'); print(f'  ADMIN_USERNAME   = admin'); print(f'  ADMIN_PASSWORD   = {ap}'); print(f'  ADMIN_API_TOKEN  = {at}')"
    if %errorlevel% neq 0 (
        echo [警告] 自动生成 .env 失败，尝试从模板复制...
        if exist ".env.simple" (
            copy .env.simple .env >nul
            echo [提示] 已从 .env.simple 复制默认配置，请手动修改密钥
        )
    ) else (
        echo [信息] .env 文件已自动生成，密钥已随机生成
        echo [提示] 请记录上面的管理员密码，或在 .env 文件中查看
    )
    echo.
)

echo ========================================
echo   正在启动应用...
echo   按 Ctrl+C 停止服务
echo ========================================
echo.

:: 设置 Flask 环境变量
set FLASK_APP=app.py
set FLASK_ENV=production
set PYTHONIOENCODING=utf-8

:: 启动应用 (.env 文件由 python-dotenv 自动加载)
python app.py

:: 如果应用退出
echo.
echo [信息] 应用已停止
pause
