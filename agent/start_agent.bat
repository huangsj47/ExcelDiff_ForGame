@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Agent Startup

cd /d "%~dp0"
set "LOG_FILE=%~dp0agent.log"
set "ACTIVE_LOG_FILE=%LOG_FILE%"
call :select_log_file
if errorlevel 1 goto :fail

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
call :ensure_venv_python
if errorlevel 1 goto :fail

if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
)

echo [INFO] Using Python: %PYTHON_EXE%
echo [INFO] Agent log file: %ACTIVE_LOG_FILE%
if /I "%PYTHON_EXE%"=="python" (
    echo [WARN] Running with system Python.
    echo [WARN] If AGENT_AUTO_UPDATE_INSTALL_DEPS=true, self-update will install deps into system Python.
) else (
    echo [INFO] Running with virtual environment Python.
    echo [INFO] If AGENT_AUTO_UPDATE_INSTALL_DEPS=true, deps will be installed into this venv.
)
echo [INFO] Installing dependencies...
"%PYTHON_EXE%" -m pip install --upgrade pip >nul
if exist "..\app.py" if exist "..\requirements.txt" (
    echo [INFO] Platform runtime detected. Installing platform dependencies from ..\requirements.txt ...
    "%PYTHON_EXE%" -m pip install --prefer-binary -r "..\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Platform dependency installation failed.
        goto :fail
    )
)
echo [INFO] Installing agent dependencies from requirements.txt ...
"%PYTHON_EXE%" -m pip install --prefer-binary -r "requirements.txt"
if errorlevel 1 (
    echo [ERROR] Agent dependency installation failed.
    goto :fail
)

echo.
echo ========================================
echo   Starting Agent...
echo   Press Ctrl+C to stop
echo ========================================
echo.

call :append_log_line "Agent process start"
if errorlevel 1 goto :fail_log_write

call :run_agent_with_log "%PYTHON_EXE%" "start_agent.py" "%ACTIVE_LOG_FILE%"
set "APP_EXIT=%ERRORLEVEL%"

call :append_log_line "Agent process exit code=%APP_EXIT%"
if errorlevel 1 (
    echo [WARN] Failed to append exit code to log file: %ACTIVE_LOG_FILE%
)

if not "%APP_EXIT%"=="0" (
    echo [ERROR] Agent exited with code %APP_EXIT%.
    goto :fail_with_code
)

echo [INFO] Agent stopped normally.
exit /b 0

:fail_with_code
exit /b %APP_EXIT%

:fail_log_write
echo [ERROR] Unable to write startup logs: %ACTIVE_LOG_FILE%
echo [ERROR] The log file may be locked by another process.
exit /b 1

:fail
exit /b 1

:ensure_venv_python
if not exist "venv\Scripts\python.exe" (
    echo [INFO] Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        exit /b 1
    )
    exit /b 0
)

venv\Scripts\python.exe -c "import sys; print(sys.executable)" >nul 2>&1
if not errorlevel 1 exit /b 0

echo [WARN] Existing venv is invalid and will be recreated.
if exist "venv" (
    rmdir /s /q "venv"
)
if exist "venv" (
    echo [ERROR] Failed to remove invalid venv directory. Close related processes and retry.
    exit /b 1
)

python -m venv venv
if errorlevel 1 (
    echo [ERROR] Failed to recreate virtual environment.
    exit /b 1
)
exit /b 0

:append_log_line
setlocal
set "LOG_MESSAGE=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$p=$env:ACTIVE_LOG_FILE; $m=$env:LOG_MESSAGE; " ^
    "try { Add-Content -Path $p -Value (\"[{0}] [INFO] {1}\" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8; exit 0 } " ^
    "catch { exit 1 }" >nul 2>&1
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:run_agent_with_log
setlocal
set "RUN_PY=%~1"
set "RUN_SCRIPT=%~2"
set "RUN_LOG=%~3"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$py=$env:RUN_PY; $script=$env:RUN_SCRIPT; $log=$env:RUN_LOG; " ^
    "try { & $py $script *>> $log; exit $LASTEXITCODE } " ^
    "catch { Write-Host $_.Exception.Message; exit 1 }"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:select_log_file
set "ACTIVE_LOG_FILE=%LOG_FILE%"
call :can_write_log "%ACTIVE_LOG_FILE%"
if not errorlevel 1 exit /b 0

if not exist "%~dp0logs" mkdir "%~dp0logs" >nul 2>&1
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString(\"yyyyMMdd_HHmmss\")"') do set "RUN_TS=%%i"
if not defined RUN_TS set "RUN_TS=%RANDOM%"
set "ACTIVE_LOG_FILE=%~dp0logs\agent_%RUN_TS%.log"
call :can_write_log "%ACTIVE_LOG_FILE%"
if not errorlevel 1 (
    echo [WARN] agent.log is locked by another process; switched log output to:
    echo [WARN]   %ACTIVE_LOG_FILE%
    exit /b 0
)

echo [ERROR] Unable to write logs to both agent.log and fallback logs file.
echo [ERROR] Please close tools that lock log files and retry.
exit /b 1

:can_write_log
setlocal
set "TARGET_PATH=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$p=$env:TARGET_PATH; try { " ^
    "$dir=[System.IO.Path]::GetDirectoryName($p); " ^
    "if($dir -and -not (Test-Path $dir)){ New-Item -ItemType Directory -Path $dir -Force | Out-Null }; " ^
    "$fs=[System.IO.File]::Open($p,[System.IO.FileMode]::OpenOrCreate,[System.IO.FileAccess]::Write,[System.IO.FileShare]::ReadWrite); " ^
    "$fs.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
