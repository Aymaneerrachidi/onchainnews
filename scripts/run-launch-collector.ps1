$ErrorActionPreference = "Stop"
$BriefRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $BriefRoot
& uv run solana-brief collector *>> data\launch-collector.log
exit $LASTEXITCODE
