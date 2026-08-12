$ErrorActionPreference = "Stop"
$BriefRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $BriefRoot
& uv run solana-brief run
$BriefExit = $LASTEXITCODE
if ($BriefExit -ne 0) {
    exit $BriefExit
}

& (Join-Path $PSScriptRoot "publish-web.ps1")
exit $LASTEXITCODE
