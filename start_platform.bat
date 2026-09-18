@echo off
chcp 65001 >nul
:: 双击启动可视化测试平台 (Windows)
set DIR=%~dp0
set PY=%DIR%.venv\Scripts\python.exe

if not exist "%PY%" (
    echo 找不到 %PY%
    echo 请先创建项目虚拟环境：python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

curl -s -m 2 -o nul http://127.0.0.1:8765/api/rounds
if %ERRORLEVEL% EQU 0 (
    echo 服务已在运行
) else (
    echo 启动测试平台服务...
    if not exist "%DIR%logs" mkdir "%DIR%logs"
    start /B "" "%PY%" server.py --port 8765 > "%DIR%logs\platform.log" 2>&1
    timeout /T 2 /NOBREAK >nul
)

start http://127.0.0.1:8765
echo 已打开浏览器：http://127.0.0.1:8765
