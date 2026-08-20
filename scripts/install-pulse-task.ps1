$ErrorActionPreference = "Stop"
$TaskName = "Hourly Solana Runner Pulse"
$Runner = Join-Path -Path $PSScriptRoot -ChildPath "run-pulse.ps1"
$ResolvedRunner = (Resolve-Path -LiteralPath $Runner).Path
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ResolvedRunner`""
$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(2)) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Run the hourly sustained-runner pulse and publish the live web snapshot." `
    -Force
Start-ScheduledTask -TaskName $TaskName
Write-Output "Registered and started '$TaskName'."
