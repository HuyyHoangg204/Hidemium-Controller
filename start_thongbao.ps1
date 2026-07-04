# start_thongbao.ps1 — Chay trong PowerShell
# Usage: .\start_thongbao.ps1

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Tim Python co pymongo
$PythonCandidates = @(
    "$ScriptDir\.venv\Scripts\python.exe",
    "$ScriptDir\venv\Scripts\python.exe",
    "python", "python3"
)

$Python = $null
foreach ($p in $PythonCandidates) {
    try {
        $test = & $p -c "import pymongo; print('ok')" 2>$null
        if ($test -eq "ok") { $Python = $p; break }
    } catch {}
}

if (-not $Python) {
    Write-Host "[ERROR] Khong tim thay Python co pymongo!" -ForegroundColor Red
    Write-Host "Dang cai pymongo vao venv..."
    & "$ScriptDir\.venv\Scripts\pip.exe" install pymongo --quiet
    $Python = "$ScriptDir\.venv\Scripts\python.exe"
}

# Tim Node
$Node = "C:\Program Files\nodejs\node.exe"
if (-not (Test-Path $Node)) { $Node = "node" }

$env:PYTHON        = $Python
$env:INTERVAL_MINUTES = "60"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host " Thong bao Cookie Veo3 - Daemon" -ForegroundColor Cyan
Write-Host " Python  : $Python" -ForegroundColor Green
Write-Host " Node    : $Node" -ForegroundColor Green
Write-Host " Interval: 60 phut" -ForegroundColor Yellow
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

& $Node thongbao.js --daemon
