@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Agent Startup

cd /d "%~dp0"

echo ========================================
echo   Agent - Startup Script
echo ========================================
echo.

if not exist ".env" (
    if not exist ".env.example" (
        echo [ERROR] .env.example not found.
        goto :fail
    )

    copy /Y ".env.example" ".env" >nul
    if errorlevel 1 (
        echo [ERROR] Failed to create .env from .env.example.
        goto :fail
    )

    for /f %%i in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString(\"N\").Substring(0,8)"') do set "RAND_SUFFIX=%%i"
    if not defined RAND_SUFFIX set "RAND_SUFFIX=%RANDOM%"
    set "RANDOM_AGENT_NAME=Agent_!RAND_SUFFIX!"

    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$p='.env'; $c=Get-Content $p -Raw; " ^
        "$c=$c -replace '(?m)^AGENT_NAME=.*$','AGENT_NAME=!RANDOM_AGENT_NAME!'; " ^
        "$c=$c -replace '(?m)^AGENT_DEFAULT_ADMIN_USERNAME=.*$','AGENT_DEFAULT_ADMIN_USERNAME='; " ^
        "Set-Content -Path $p -Value $c -Encoding UTF8"

    if errorlevel 1 (
        echo [WARN] .env generated, but failed to rewrite AGENT_NAME/AGENT_DEFAULT_ADMIN_USERNAME.
    ) else (
        echo [INFO] .env created: AGENT_NAME=!RANDOM_AGENT_NAME!, AGENT_DEFAULT_ADMIN_USERNAME=empty
    )
)

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    goto :fail
)

set "PYTHON_EXE=python"
if not exist "venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment...
    python -m venv venv
)
if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
)

echo [INFO] Using Python: %PYTHON_EXE%
if /I "%PYTHON_EXE%"=="python" (
    echo [WARN] Running with system Python.
    echo [WARN] If AGENT_AUTO_UPDATE_INSTALL_DEPS=true, self-update will install deps into system Python.
) else (
    echo [INFO] Running with virtual environment Python.
    echo [INFO] If AGENT_AUTO_UPDATE_INSTALL_DEPS=true, deps will be installed into this venv.
)
echo [INFO] Installing dependencies...
"%PYTHON_EXE%" -m pip install --upgrade pip >nul
"%PYTHON_EXE%" -m pip install --prefer-binary -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    goto :fail
)

echo.
echo ========================================
echo   Starting Agent...
echo   Press Ctrl+C to stop
echo ========================================
echo.

"%PYTHON_EXE%" start_agent.py
set "APP_EXIT=%ERRORLEVEL%"

if not "%APP_EXIT%"=="0" (
    echo [ERROR] Agent exited with code %APP_EXIT%.
    goto :fail_with_code
)

echo [INFO] Agent stopped normally.
exit /b 0

:fail_with_code
exit /b %APP_EXIT%

:fail
exit /b 1
