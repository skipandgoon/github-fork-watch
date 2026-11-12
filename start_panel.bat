@echo off
setlocal
if not "%~1"=="" set "FORK_WATCH_OWNER=%~1"
if not defined FORK_WATCH_OWNER (
  echo [x] 未设置 GitHub 账号。
  echo     先执行：setx FORK_WATCH_OWNER 你的用户名
  echo     或直接：start_panel.bat 你的用户名
  pause
  exit /b 1
)
set "PY="
cd /d "%~dp0"
where pythonw >nul 2>&1 && set "PY=pythonw"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [x] 没找到 Python。请先安装 Python 3，并在安装时勾选 Add python.exe to PATH。
  pause
  exit /b 1
)
rem 有 Tailscale 就只监听 Tailscale 地址（手机/别的电脑能访问，局域网不暴露）
set "HOST=127.0.0.1"
for /f "delims=" %%i in ('tailscale ip -4 2^>nul') do set "HOST=%%i"
start "" "%PY%" dashboard.py --host %HOST%
ping -n 3 127.0.0.1 >nul
start "" "http://%HOST%:8770/"