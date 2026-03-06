@echo off
chcp 65001 >nul 2>&1
title Agent build_zip

cd /d "%~dp0"

echo ========================================
echo   Agent - build_zip
echo ========================================
echo.


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
echo   Starting Build Agent...
echo   Press Ctrl+C to stop
echo ========================================
echo.

"%PYTHON_EXE%" build_zip.py
set "APP_EXIT=%ERRORLEVEL%"

if not "%APP_EXIT%"=="0" (
    echo [ERROR] Agent exited with code %APP_EXIT%.
    goto :fail_with_code
)

echo [INFO] Agent stopped build.
exit /b 0

:fail_with_code
exit /b %APP_EXIT%

:fail
exit /b 1
