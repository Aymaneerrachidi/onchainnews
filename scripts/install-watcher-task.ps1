$ErrorActionPreference = "Stop"
$TaskName = "Solana Memecoin Threat Watcher"
$Runner = Join-Path -Path $PSScriptRoot -ChildPath "run-watcher.ps1"
$ResolvedRunner = (Resolve-Path -LiteralPath $Runner).Path
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ResolvedRunner`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Poll flagged Solana tokens and push material threat alerts." `
    -Force
Start-ScheduledTask -TaskName $TaskName
Write-Output "Registered and started '$TaskName'."
