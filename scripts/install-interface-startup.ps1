$ErrorActionPreference = "Stop"
$BriefRoot = Split-Path -Parent $PSScriptRoot
$Runner = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run-interface.ps1")).Path
$Startup = [Environment]::GetFolderPath("Startup")
$LinkPath = Join-Path $Startup "Solana Brief Interface.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($LinkPath)
$Shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$Shortcut.Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
$Shortcut.WorkingDirectory = $BriefRoot
$Shortcut.WindowStyle = 7
$Shortcut.Description = "Serve the latest daily Solana market brief on localhost."
$Shortcut.Save()
Write-Output "Installed per-user interface startup shortcut at '$LinkPath'."
