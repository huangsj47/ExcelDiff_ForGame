@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title Agent Release Publish

cd /d "%~dp0\.."
echo ========================================
echo   Agent Release - Publish
echo ========================================
echo.

python scripts\publish_agent_release.py %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo [INFO] Publish finished successfully.
) else (
    echo [ERROR] Publish failed with exit code %EXIT_CODE%.
)

if /I not "%RELEASE_SCRIPT_NO_PAUSE%"=="1" (
    echo.
    echo [INFO] Press any key to close this window...
    pause >nul
)

exit /b %EXIT_CODE%
