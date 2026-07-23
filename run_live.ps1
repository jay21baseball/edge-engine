# Live divergence recorder. Its own process because it polls every 60 seconds
# during games - far faster than the 15-minute main scanner. Free: ESPN and
# Polymarket only, no paid data.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
if (Test-Path ".\secrets.local.ps1") { . .\secrets.local.ps1 }

$logDir = Join-Path $PSScriptRoot "data"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$log = Join-Path $logDir ("live-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

"=== live recorder started $(Get-Date -Format 'u') ===" | Out-File -FilePath $log -Append -Encoding utf8
cmd /c "python -m edge_engine.scan live --interval 60 >> ""$log"" 2>&1"
