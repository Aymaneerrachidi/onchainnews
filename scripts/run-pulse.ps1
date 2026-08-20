$ErrorActionPreference = "Stop"
$BriefRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $BriefRoot
& uv run solana-brief pulse
$PulseExit = $LASTEXITCODE
if ($PulseExit -ne 0) {
    exit $PulseExit
}

& (Join-Path $PSScriptRoot "publish-web.ps1")
exit $LASTEXITCODE
