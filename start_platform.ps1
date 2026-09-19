<#
.SYNOPSIS
    启动可视化测试平台服务并自动在浏览器中打开 (PowerShell 版)
#>
$ErrorActionPreference = "Stop"

$scriptDir = $PSScriptRoot
$venvPy = Join-Path $scriptDir ".venv\Scripts\python.exe"
$logDir = Join-Path $scriptDir "logs"
$logFile = Join-Path $logDir "platform.log"
$serverUrl = "http://127.0.0.1:8765"

if (-not (Test-Path $venvPy)) {
    Write-Host "[错误] 未找到虚拟环境 Python: $venvPy" -ForegroundColor Red
    Write-Host "请先在项目根目录创建虚拟环境：" -ForegroundColor Yellow
    Write-Host "  python -m venv .venv" -ForegroundColor Gray
    Write-Host "  .venv\Scripts\pip install -r requirements.txt" -ForegroundColor Gray
    Read-Host "按回车键退出..."
    exit 1
}

# 检查服务是否已在运行
function Test-ServerUp {
    try {
        $resp = Invoke-WebRequest -Uri "$serverUrl/api/rounds" -TimeoutSec 2 -UseBasicParsing -ErrorAction SilentlyContinue
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (Test-ServerUp) {
    Write-Host "[平台] 服务已在运行中，正在打开浏览器..." -ForegroundColor Green
    Start-Process $serverUrl
    exit 0
}

Write-Host "[平台] 正在启动测试平台服务..." -ForegroundColor Cyan
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$env:PYTHONIOENCODING = "utf-8"

# 后台启动 server.py
$serverScript = Join-Path $scriptDir "server.py"
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $venvPy
$psi.Arguments = "`"$serverScript`" --port 8765"
$psi.WorkingDirectory = $scriptDir
$psi.RedirectStandardOutput = $false
$psi.RedirectStandardError = $false
$psi.UseShellExecute = $true
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

[System.Diagnostics.Process]::Start($psi) | Out-Null

# 等待就绪（最多 10 秒）
$ready = $false
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 1
    if (Test-ServerUp) {
        $ready = $true
        break
    }
}

if ($ready) {
    Write-Host "[平台] 服务已就绪，正在打开浏览器：$serverUrl" -ForegroundColor Green
    Start-Process $serverUrl
} else {
    Write-Host "[警告] 服务启动超时，请检查日志目录或日志文件：$logFile" -ForegroundColor Yellow
}
