# edge-engine always-on scanner launcher.
# Registered as a Scheduled Task by install_task.ps1, or run directly.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"

$logDir = Join-Path $PSScriptRoot "data"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$log = Join-Path $logDir ("watch-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

"=== edge-engine watch started $(Get-Date -Format 'u') ===" | Out-File -FilePath $log -Append -Encoding utf8
python -m edge_engine.scan watch *>&1 | Tee-Object -FilePath $log -Append
