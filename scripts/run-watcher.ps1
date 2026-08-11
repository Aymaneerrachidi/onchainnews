$ErrorActionPreference = "Stop"
$BriefRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $BriefRoot
& uv run solana-brief watcher
exit $LASTEXITCODE
