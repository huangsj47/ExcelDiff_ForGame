@echo off
setlocal EnableExtensions

echo ========================================
echo   Installing project dependencies
echo ========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found in PATH.
    pause
    exit /b 1
)

if not exist "requirements.txt" (
    echo [ERROR] requirements.txt was not found.
    pause
    exit /b 1
)

echo [INFO] Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [WARN] pip upgrade failed. Continue with current version.
)

echo [INFO] Installing dependencies from requirements.txt ...
python -m pip install --prefer-binary -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

echo [INFO] Dependency installation completed.
pause
exit /b 0

