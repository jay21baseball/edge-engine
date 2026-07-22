# edge-engine always-on scanner launcher.
# Registered as a Scheduled Task by install_task.ps1, or run directly.

# Must be Continue, not Stop. Python's logging writes to stderr, and in
# PowerShell 5.1 a native command's stderr is wrapped in an ErrorRecord — with
# ErrorActionPreference=Stop the very first log line throws a terminating
# NativeCommandError and kills the scanner before it does any work.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$env:PYTHONPATH = "src"
$env:PYTHONUNBUFFERED = "1"

if (Test-Path ".\secrets.local.ps1") { . .\secrets.local.ps1 }

$logDir = Join-Path $PSScriptRoot "data"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$log = Join-Path $logDir ("watch-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

"=== edge-engine watch started $(Get-Date -Format 'u') ===" | Out-File -FilePath $log -Append -Encoding utf8

# Redirect via cmd rather than PowerShell. PowerShell 5.1 wraps every stderr
# line from a native exe in an ErrorRecord, which floods the log with
# NativeCommandError noise around perfectly normal INFO logging. cmd redirects
# the raw stream.
cmd /c "python -m edge_engine.scan watch >> ""$log"" 2>&1"
