# Whale tracker. Polls each tracked wallet's public trade feed and texts you
# every new trade the moment it lands. Free - Polymarket activity is public.
# Its own process because it polls fast (every ~45s), around the clock.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"
if (Test-Path ".\secrets.local.ps1") { . .\secrets.local.ps1 }

$logDir = Join-Path $PSScriptRoot "data"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$log = Join-Path $logDir ("whales-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

"=== whale tracker started $(Get-Date -Format 'u') ===" | Out-File -FilePath $log -Append -Encoding utf8
cmd /c "python -m edge_engine.scan whales --interval 45 >> ""$log"" 2>&1"
