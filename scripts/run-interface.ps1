$ErrorActionPreference = "Stop"
$BriefRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $BriefRoot
& uv run solana-brief interface --no-browser --port 8765 *>> data\interface-server.log
exit $LASTEXITCODE
