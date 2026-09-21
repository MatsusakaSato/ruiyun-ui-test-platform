@echo off
chcp 65001 >nul
:: 双击启动可视化测试平台 (Windows)
set DIR=%~dp0
set PY=%DIR%.venv\Scripts\python.exe
set LOG=%DIR%logs\platform.log
:: 服务输出重定向到文件时 Python 默认走本地 ANSI 代码页（中文系统为 GBK），
:: 显式约定 UTF-8，保证日志里的中文不乱码
set PYTHONIOENCODING=utf-8

if not exist "%PY%" (
    echo [错误] 找不到虚拟环境 Python: %PY%
    echo 请先初始化环境：
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

curl -s -m 2 -o nul http://127.0.0.1:8765/api/rounds
if %ERRORLEVEL% EQU 0 (
    echo 测试平台服务已在运行，直接打开控制台...
    goto open_browser
)

echo 启动测试平台服务...
if not exist "%DIR%logs" mkdir "%DIR%logs"

:: 使用 start /B 后台启动服务并重定向日志
start "" /B cmd /c "cd /d "%DIR%" && "%PY%" "%DIR%server.py" --port 8765 >> "%LOG%" 2>&1"

:: 等待服务就绪（最多 10 秒，每秒探测一次）
set /a WAIT=0
:wait_loop
timeout /T 1 /NOBREAK >nul
curl -s -m 1 -o nul http://127.0.0.1:8765/api/rounds
if %ERRORLEVEL% EQU 0 goto open_browser
set /a WAIT+=1
if %WAIT% LSS 10 goto wait_loop

echo.
echo [警告] 服务启动超时，请查看日志文件：%LOG%
pause
exit /b 1

:open_browser
start http://127.0.0.1:8765
echo 已打开浏览器：http://127.0.0.1:8765
