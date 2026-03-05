@echo off
setlocal EnableExtensions

cd /d "%~dp0\.."
python scripts\rollback_agent_release.py %*
exit /b %ERRORLEVEL%
