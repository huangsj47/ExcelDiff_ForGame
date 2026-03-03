@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Diff Platform Startup

echo ========================================
echo   Diff Platform - Startup Script
echo ========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    echo         Install Python 3.9+ and add it to PATH, then retry.
    goto :fail
)

echo [INFO] Detected Python:
python --version
echo.

set "PYTHON_EXE=python"

if not exist "venv\Scripts\python.exe" (
    echo [INFO] Virtual environment not found. Creating...
    python -m venv venv
    if errorlevel 1 (
        echo [WARN] Failed to create virtual environment. Will use global Python.
    ) else (
        echo [INFO] Virtual environment created.
    )
)

if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
    echo [INFO] Using virtual environment Python: !PYTHON_EXE!
) else (
    echo [WARN] Using global Python.
)

echo [INFO] Upgrading pip...
"%PYTHON_EXE%" -m pip install --upgrade pip >nul
if errorlevel 1 (
    echo [WARN] pip upgrade failed. Continue with current version.
)

if exist "requirements.txt" (
    echo [INFO] Installing dependencies from requirements.txt ...
    "%PYTHON_EXE%" -m pip install --prefer-binary -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed.
        echo         Fix the error above and run start.bat again.
        goto :fail
    )
    echo [INFO] Dependency installation completed.
) else (
    echo [WARN] requirements.txt not found. Skipping dependency installation.
)

echo [INFO] Ensuring .env exists and format is valid...
"%PYTHON_EXE%" -m utils.env_bootstrap --env-path ".env"
if errorlevel 1 (
    echo [WARN] .env bootstrap failed.
    if not exist ".env" (
        if exist ".env.simple" (
            copy /Y ".env.simple" ".env" >nul
            if errorlevel 1 (
                echo [ERROR] Failed to copy .env.simple to .env.
                goto :fail
            )
            echo [INFO] Copied .env.simple to .env. Update secrets manually.
        ) else (
            echo [ERROR] .env.simple not found. Cannot create .env.
            goto :fail
        )
    ) else (
        echo [ERROR] .env exists but bootstrap failed. Please check Python traceback.
        goto :fail
    )
)
echo.

echo ========================================
echo   Starting application...
echo   Press Ctrl+C to stop
echo ========================================
echo.

set "FLASK_APP=app.py"
set "FLASK_ENV=production"
set "PYTHONIOENCODING=utf-8"

"%PYTHON_EXE%" app.py
set "APP_EXIT=%ERRORLEVEL%"

echo.
if not "%APP_EXIT%"=="0" (
    echo [ERROR] Application exited with code %APP_EXIT%.
    goto :fail_with_code
)

echo [INFO] Application stopped normally.
pause
exit /b 0

:fail_with_code
pause
exit /b %APP_EXIT%

:fail
pause
exit /b 1

