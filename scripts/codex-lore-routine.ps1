param(
    [ValidateSet("prepare", "finalize")]
    [string]$Stage = "prepare",
    [string]$Source = "web/data/latest.json",
    [string]$Artifact = "output/enriched-current-all.json",
    [string]$Queue = "output/codex-lore-queue.md"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if ($Stage -eq "finalize") {
    uv run python scripts/enrich_existing.py --source $Source --output $Artifact --merge-curated-only
    if ($LASTEXITCODE -ne 0) { throw "Curated lore merge failed." }

    $final = Get-Content -LiteralPath $Artifact -Raw | ConvertFrom-Json
    $withLore = @($final.coins | Where-Object { -not [string]::IsNullOrWhiteSpace($_.lore) }).Count
    $withoutLore = @($final.coins | Where-Object { [string]::IsNullOrWhiteSpace($_.lore) }).Count
    Write-Host "Codex lore finalized: $withLore with lore, $withoutLore intentionally blank."
    exit 0
}

uv run python scripts/enrich_existing.py --source $Source --output $Artifact
if ($LASTEXITCODE -ne 0) { throw "Evidence enrichment failed." }

$data = Get-Content -LiteralPath $Artifact -Raw | ConvertFrom-Json
$unresolved = @($data.coins | Where-Object { [string]::IsNullOrWhiteSpace($_.lore) })
$lines = @(
    "# Codex lore research queue"
    ""
    "Research every contract below. Start with linked deployer/project posts, then exact CA searches, then ticker/name lore and story searches. For every unresolved meme, explicitly check Know Your Meme and TikTok/Douyin/short-form trend results; coins such as LA PEACE and Gucci Morty can originate outside crypto. A meme page proves the underlying trend, but the token still needs contract-linked or deployer-linked evidence connecting it to that meme. Publish real origin, event, utility or creator context only. Trading calls, price reactions, generic community copy and ticker collisions are not lore. If nothing defensible exists, leave it blank."
    ""
)

foreach ($coin in $unresolved) {
    $symbol = [string]$coin.symbol
    $name = [string]$coin.name
    $mint = [string]$coin.mint
    $chain = [string]$coin.chain
    $lines += ('## `${0}` - {1}' -f $symbol, $name)
    $lines += "- Chain: $chain"
    $lines += ('- Contract: `{0}`' -f $mint)
    $lines += ('- Searches: `"{0}"`, `"{0}" lore`, `"{0}" story`, `${1} lore`, `${1} story`, `"{2}" lore`, `"{2}" story`, `site:knowyourmeme.com "{2}"`, `"{2}" TikTok trend meme`, `"{2}" Douyin meme`' -f $mint, $symbol, $name)
    $lines += ""
}

$queuePath = Join-Path $repoRoot $Queue
$queueDirectory = Split-Path -Parent $queuePath
New-Item -ItemType Directory -Force -Path $queueDirectory | Out-Null
$lines | Set-Content -LiteralPath $queuePath -Encoding utf8
Write-Host "Codex lore queue prepared: $($unresolved.Count) contracts -> $queuePath"
Write-Host "After researched entries are added to brief/curated_lore.json, run:"
Write-Host "  powershell -File scripts/codex-lore-routine.ps1 -Stage finalize"
