@echo off
set "PIDFILE=%~dp0.dashboard.pid"
if exist "%PIDFILE%" set /p PID=<"%PIDFILE%"
if defined PID taskkill /PID %PID% /F >nul 2>&1
if exist "%PIDFILE%" del "%PIDFILE%"
echo panel stopped.
ping -n 3 127.0.0.1 >nul