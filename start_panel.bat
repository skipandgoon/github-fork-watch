@echo off
setlocal
set "PY="
cd /d "%~dp0"
where pythonw >nul 2>&1 && set "PY=pythonw"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [x] 没找到 Python。请先安装 Python 3，并在安装时勾选 Add python.exe to PATH。
  pause
  exit /b 1
)
start "" "%PY%" dashboard.py
ping -n 3 127.0.0.1 >nul
start "" "http://127.0.0.1:8770/"