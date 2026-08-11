$ErrorActionPreference = "Stop"
$BriefRoot = Split-Path -Parent $PSScriptRoot
$Runner = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run-launch-collector.ps1")).Path
$Startup = [Environment]::GetFolderPath("Startup")
$LinkPath = Join-Path $Startup "Solana Launch Collector.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($LinkPath)
$Shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Shortcut.Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
$Shortcut.WorkingDirectory = $BriefRoot
$Shortcut.WindowStyle = 7
$Shortcut.Description = "Continuously capture Pump.fun launches for the Solana morning brief."
$Shortcut.Save()
Write-Output "Installed per-user launch collector startup shortcut at '$LinkPath'."
