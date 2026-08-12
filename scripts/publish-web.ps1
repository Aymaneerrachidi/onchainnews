$ErrorActionPreference = "Stop"
$BriefRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $BriefRoot

$Snapshot = "web/data/latest.json"
& git fetch origin main --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Error "Could not fetch origin/main; the local report remains available but Vercel was not updated."
    exit 1
}

& git merge-base --is-ancestor origin/main HEAD
if ($LASTEXITCODE -ne 0) {
    Write-Error "origin/main contains commits missing locally. Resolve the branch before automatic publishing resumes."
    exit 1
}

& git diff --quiet HEAD -- $Snapshot
if ($LASTEXITCODE -eq 0) {
    Write-Output "Public snapshot is unchanged; no Vercel deployment is needed."
    exit 0
}

$Stamp = Get-Date -Format "yyyy-MM-dd"
& git commit --only -m "Daily brief $Stamp" -- $Snapshot
if ($LASTEXITCODE -ne 0) {
    Write-Error "Could not commit the public snapshot."
    exit 1
}

& git push origin HEAD:main
if ($LASTEXITCODE -ne 0) {
    Write-Error "Snapshot was committed locally but could not be pushed to Vercel's Git branch."
    exit 1
}

Write-Output "Published $Snapshot; Vercel production deployment queued."
