$ErrorActionPreference = "Stop"
$TaskName = "Solana Launch Collector"
$Runner = Join-Path -Path $PSScriptRoot -ChildPath "run-launch-collector.ps1"
$ResolvedRunner = (Resolve-Path -LiteralPath $Runner).Path
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ResolvedRunner`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Continuously capture Pump.fun coin creation instructions into the daily Solana launch index." `
    -Force
Start-ScheduledTask -TaskName $TaskName
Write-Output "Registered and started '$TaskName'."
