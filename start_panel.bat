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
set "PYPATH="
cd /d "%~dp0"
for /f "delims=" %%p in ('where pythonw 2^>nul') do if not defined PYPATH set "PYPATH=%%p"
if not defined PYPATH for /f "delims=" %%p in ('where python 2^>nul') do if not defined PYPATH set "PYPATH=%%p"
if not defined PYPATH (
  echo [x] 没找到 Python。请先安装 Python 3，并在安装时勾选 Add python.exe to PATH。
  pause
  exit /b 1
)
rem 有 Tailscale 就只监听 Tailscale 地址（手机/别的电脑能访问，局域网不暴露）
set "HOST=127.0.0.1"
for /f "delims=" %%i in ('tailscale ip -4 2^>nul') do set "HOST=%%i"
rem 用 PowerShell 启动：完全脱离当前控制台，关掉这个窗口不会把服务一起带走
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath '%PYPATH%' -ArgumentList 'dashboard.py','--host','%HOST%' -WorkingDirectory '%~dp0'"
ping -n 3 127.0.0.1 >nul
start "" "http://%HOST%:8770/"