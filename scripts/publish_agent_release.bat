@echo off
setlocal EnableExtensions

cd /d "%~dp0\.."
python scripts\publish_agent_release.py %*
exit /b %ERRORLEVEL%
