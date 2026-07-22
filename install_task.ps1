# Registers edge-engine as a Windows Scheduled Task that starts at logon,
# runs hidden, and restarts itself if it dies.
#
#   .\install_task.ps1            install / update
#   .\install_task.ps1 -Remove    uninstall
#
# Runs under your own user account. No admin rights required, no system or
# security settings are modified.

param([switch]$Remove)

$TaskName = "EdgeEngineScanner"

if ($Remove) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Removed scheduled task '$TaskName'."
    } catch {
        Write-Host "No task named '$TaskName' was registered."
    }
    return
}

$script = Join-Path $PSScriptRoot "run_watch.ps1"
if (-not (Test-Path $script)) { throw "run_watch.ps1 not found next to this script." }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 5) -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch {}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "edge-engine prediction market scanner" | Out-Null

Write-Host "Registered '$TaskName' - starts at logon, restarts on failure."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Logs:               data\watch-<date>.log"
Write-Host "Remove with:        .\install_task.ps1 -Remove"
