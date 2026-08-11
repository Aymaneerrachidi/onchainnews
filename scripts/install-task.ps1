$ErrorActionPreference = "Stop"
$TaskName = "Daily Solana Memecoin Brief"
$Runner = Join-Path -Path $PSScriptRoot -ChildPath "run.ps1"
$ResolvedRunner = (Resolve-Path -LiteralPath $Runner).Path
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$ResolvedRunner`""
$Trigger = New-ScheduledTaskTrigger -Daily -At "06:45"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Generate and deliver the novelty-first Solana daily brief." `
    -Force
Write-Output "Registered '$TaskName' for 06:45 daily."
