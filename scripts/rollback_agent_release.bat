@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title Agent Release Rollback

cd /d "%~dp0\.."
echo ========================================
echo   Agent Release - Rollback
echo ========================================
echo.

python scripts\rollback_agent_release.py %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo [INFO] Rollback finished successfully.
) else (
    echo [ERROR] Rollback failed with exit code %EXIT_CODE%.
)

if /I not "%RELEASE_SCRIPT_NO_PAUSE%"=="1" (
    echo.
    echo [INFO] Press any key to close this window...
    pause >nul
)

exit /b %EXIT_CODE%
