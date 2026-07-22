# Telegram command listener. Started alongside run_watch.ps1 by
# "START EVERYTHING.bat" — the scanner pushes alerts, this answers commands.

# Continue, not Stop — see the note in run_watch.ps1: Python logging on stderr
# becomes a terminating NativeCommandError under Stop in PowerShell 5.1.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"

if (Test-Path ".\secrets.local.ps1") { . .\secrets.local.ps1 }

$logDir = Join-Path $PSScriptRoot "data"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$log = Join-Path $logDir ("bot-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

"=== forge bot started $(Get-Date -Format 'u') ===" | Out-File -FilePath $log -Append -Encoding utf8

# cmd handles the redirect — see the note in run_watch.ps1.
cmd /c "python -m edge_engine.scan bot >> ""$log"" 2>&1"
