# Daily Solana Memecoin Brief

A local, novelty-first Solana intelligence brief. It reports measurable changes, never recommendations, and uses only free sources: Dexscreener, RugCheck, Helius free tier, Jupiter's no-key lite quote API, X public account metadata, and Telegram Bot API. Bubblemaps is linked for inspection but is not queried.

Every morning report opens with **the read**: the day's picks, one descriptive sentence each, above the fold. Below that sit the full evidence dossiers, the rolling 24-hour launch tape, and the screening funnel. This is deliberately described as discovery-feed coverage—not a claim to index every mint created on Solana, most of which never form a tracked market.

## The daily journal

The report opens with **what ran today**, not a shortlist. A coin enters through one of two doors:

- **Created inside the last 24 hours** and up at least 30%.
- **Any age, doing a 5x or better** on the day.

Everything above $150,000 market cap and $50,000 of 24-hour volume that clears one of those doors is recorded, ranked by size of run. This is deliberately the opposite of an editorial cut: a coin is *not* dropped for failing a safety check. It is shown with the problem written on its row.

Only conditions that make a coin uninvestable at any price remove it from the record, and those are listed separately under "ran, but disqualified":

- a live mint authority (supply can be inflated)
- a live freeze authority (holders can be frozen)
- liquidity neither locked nor burned (it can be pulled)
- bundled supply, meaning the top 10 circulating wallets hold more than 50%
- a manufactured tape, meaning many trades from very few wallets

Everything softer becomes a label: elevated concentration, a thin pool, paid boosts, a reused ticker, no linked socials, a very young pair, or a **fade** — up strongly on the day but down more than 15% in the last hour. That last one is the coin that hit an all-time high and gave it back, which the 24-hour number alone hides.

Runners are grouped by **shared lore**: Dexscreener's trending metas first, then a shared significant word in the name, which is how copycats of one story cluster. A lore that has already produced recent mints is flagged rather than presented as original.

## Tracked wallets

`[kol].wallets` holds a deduplicated leaderboard (66 wallets, named). Two questions are answered from their on-chain activity:

- **Conviction** — which coins several independent wallets bought inside the window, and which of them are still holding versus already closed. A coin bought by `min_buyers_to_flag` or more is flagged.
- **Where they made money** — realised SOL per mint, reconstructed from each wallet's own balance deltas.

Buys and sells are read from balance deltas rather than by classifying swap instructions, so the arithmetic stays correct across every DEX, aggregator and bot router. Network fees are excluded, wrapped SOL is ignored so the SOL leg of a swap is not double-counted, and a multi-hop route splits its SOL across the mints it moved. A wallet that opened a position before the window and closed it inside counts as a trader even though it has no buy to show.

The list is ordered by reported PnL and scanned newest-first with its own paced request budget, so a partial scan still carries the strongest wallets. Helius answers a burst of heavy wallet-history calls with `429 max usage reached` when the plan's credits are spent; the scan degrades to a warning and the rest of the report still ships.

## The editorial tracks

Alongside the journal, three narrower tracks answer "which few names are worth a close look". Each requires the market-cap and liquidity floors, a passed safety gate, and an unreused ticker.

| Track | Question it answers | Extra bar |
| --- | --- | --- |
| `NEW` | What launched in the last 24h and is already working? | Created inside the window; 3 strength and 2 interest signals; max 5 |
| `MOVER` | What is strongest today, at any age? | Up to 120d old; at least +25% in 24h on $100k volume and 0.5x turnover; max 5 |
| `CTO` | Which takeover is actually alive? | Claimed within 7d with measurable post-claim activity; max 3 |

The wash-trading proxy scales with age: volume above 25x pool depth rejects a pair under 24h old, while an established name is allowed 75x, because a genuinely violent day legitimately turns a pool over many times. Holder concentration is measured on circulating holders only — RugCheck's raw `topHolders` list includes the AMM vault, lockers and burn addresses, and summing it verbatim reports healthy tokens at 60-86%.

## Discovery

Dexscreener's public feeds surface only trending metas, takeovers, profiles and paid boosts, roughly four hundred tokens. Birdeye ranks every token with real liquidity by 24-hour volume, which is the pool the journal draws from. Only `sort_by=v24hUSD` is used: sorting by market cap returns tokens with fabricated supply, observed at $108 trillion. Requires `BIRDEYE_API_KEY`; without it discovery falls back to the Dexscreener feeds.

## First run

```powershell
uv sync --dev
Copy-Item .env.example .env
# Put your free Helius key in .env before this command.
uv run solana-brief run --no-telegram
```

The first successful Helius run is a baseline. The next run at least 20 hours later produces 24-hour holder, concentration, whale, retention, and creator changes. Seven-day trajectories appear after a week. If Helius is missing or fails, the rest of the brief still ships and each watched token is explicitly marked unavailable.

Useful commands:

```powershell
uv run solana-brief run
uv run solana-brief run --dry-run
uv run solana-brief status
uv run solana-brief watch add <MINT> --symbol TICKER
uv run solana-brief watch list
uv run solana-brief watch remove <MINT>
uv run solana-brief mark <MINT> traded
uv run solana-brief mark <MINT> skipped
uv run solana-brief replay 2026-08-06
uv run solana-brief weekly
uv run solana-brief watcher --once
uv run solana-brief watcher
uv run solana-brief interface
uv run solana-brief collector
uv run solana-brief unretire <MINT>
uv run solana-brief prune --vacuum
uv run pytest
```

## Archive size

Every HTTP body is archived so any past date can be re-scored offline. Helius holder pages are large, and storing them verbatim grew the database past a gigabyte in a single run. Bodies are now zlib-compressed on write (roughly a 4x reduction measured on real runs) and anything older than `run.archive_retention_days` is deleted on every committed run.

`prune --vacuum` compresses any rows written before compression existed and rewrites the file to reclaim space. It is lossless: replay for every archived date keeps working. The file can only shrink when no other process holds the database, so stop the collector and the interface first—the command says so when it is blocked.

## What is stored and computed

The SQLite ledger at `data/brief.db` holds:

- Full ex-LP/CEX/burn holder snapshots and owner balances, with top-10, top-50, and Gini values.
- Wallet first-funder and first-acquisition traces, launch-window wallets, creator-linked supply, pool-vault samples, and cross-token holder overlap.
- A global shared-funder cluster registry with tokens seen and matured seven-day outcomes.
- Per-token metric baselines for z-score anomaly detection.
- Featured/excluded observations with 24h, 72h, and 7d forward returns.
- Operator `traded`/`skipped` feedback.
- Every real HTTP response body before parsing, plus its date, endpoint, sanitized parameters, request body, and status for deterministic replay.
- Data-quality baselines and one-time intelligence events such as migrations and retired-token reappearances.

The on-chain section is ordered by loss-relevant events: LP removal, reused global clusters, creator or whale outflow, holder divergence, then structural concentration changes. It also detects pump.fun-style migrations, launch snipers/bundles, CEX-funded coordination, recurring early wallets from prior winners, cross-token holder overlap, fresh-wallet growth, retention, and token-specific anomalies.

The candidate sections show Jupiter-computed SOL exit capacity at less than 5% quoted impact. Multi-venue tokens include the 24-hour volume split by DEX. The scorecard reports Q1, median, Q3, the percentage losing at least 90%, the excluded control set, and traded-versus-skipped outcomes.

## Watchlist and free-tier budget

`[holders].watchlist_limit` defaults to five. Manual entries take priority. Empty slots are filled with fresh hard-filter survivors and remain stable so daily diffs are meaningful. Helius calls share one rate limiter, provenance is cached, holder pages are 1,000 accounts each, and all page/history limits are configurable.

RugCheck pool accounts, burn addresses, and configured exchange wallets are excluded from concentration. Populate `known_cex_wallets` with hot/deposit wallets you have independently verified; the project intentionally ships no guessed exchange list.

## Between-brief watcher

The watcher polls only active watchlist tokens. It sends Telegram messages for exactly four event classes:

- Pool-balance proxy removal above the configured threshold, 10% by default.
- Registry-cluster wallets beginning to reduce their supply share.
- Holder growth changing from positive daily growth to an intraday decline.
- Creator-linked supply outflow.

It does not send price, volume, or recommendation alerts. Run one diagnostic cycle with `watcher --once`; the persistent command defaults to a five-minute interval. Telegram credentials are required for an actual alert delivery.

## Replay and tuning

After a live date has been archived, change thresholds in `config.toml` and run:

```powershell
uv run solana-brief replay YYYY-MM-DD
```

Replay is read-only for feature and outcome history and fails closed when an exact archived endpoint/request combination is missing. The output is written to `output/replay-YYYY-MM-DD.html`. Running the same archive with the same config is deterministic; changed thresholds can be compared without refetching present-day data.

## Delivery and scheduling

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, then set `telegram_enabled = true` in `config.toml`. The daily static report is written atomically to `output/latest.html`.

The morning Telegram message is a digest, not the whole report: a header, the picks with one line each, up to three on-chain flags, any degraded sources, and an optional link. Set `delivery.report_url` to wherever `latest.html` is reachable and it is appended to the message. Set `delivery.telegram_digest = false` to send the full report instead. A missing credential warns and skips delivery; it never loses the report that already rendered.

The visual interface runs locally at `http://127.0.0.1:8765`. It filters the report, shows a live clock and scheduled-run countdown, monitors refresh state, and can start a new brief. It binds only to the local machine and exposes no wallet or trading action.

On Windows, `scripts/install-interface-startup.ps1` makes the local interface start automatically after sign-in. The 06:45 scheduled task replaces `output/latest.html` every morning, and the interface serves that new file immediately without needing a restart.

For cron, with the host timezone matching `run.timezone`:

```cron
45 6 * * * cd /absolute/path/to/repo && /absolute/path/to/uv run solana-brief run >> data/cron.log 2>&1
@reboot cd /absolute/path/to/repo && /absolute/path/to/uv run solana-brief watcher >> data/watcher.log 2>&1
```

On Windows, review and run `scripts/install-task.ps1` for the 06:45 daily job. Review and run `scripts/install-watcher-task.ps1` to register the watcher at logon and start it immediately.

The broad launch count comes from the continuous Helius collector, not Dexscreener's small trending sample. Run `scripts/install-launch-collector-startup.ps1` once to install the no-admin per-user startup shortcut. The collector listens to the official Pump program create instructions, writes them to `launch_events`, heals short disconnects from the latest 1,000 program transactions, and states the exact beginning of coverage in every report. Its first full rolling window is available after 24 hours of continuous collection; Dexscreener then supplies market data only for captured mints that established a tracked market.

## Configuration

Every decision threshold is in `config.toml`: hard filters, novelty, retirement, Helius request/page budgets, daily/weekly comparison age, divergence bounds, cluster and whale thresholds, anomaly baseline/sample size, social age, smart-money history, LP removal, reappearance, DEX dominance, Jupiter impact, and watcher cadence.

`[thresholds].chains` controls which chains are screened and defaults to `["solana"]`. The Dexscreener discovery and pair endpoints are chain-agnostic, so adding a chain is a configuration change—but RugCheck authority/LP checks and every Helius holder, cluster and creator analysis are Solana-only. Names on another chain would therefore ship with the safety layer marked unavailable, which is why the default stays as it is.

No automated execution, wallet connection, paid data source, sentiment scraping, or generated hype narrative is included.

## Hosted demo

The site lives in `web/` and is deployed on Vercel. It is a dependency-free static page that renders `web/data/latest.json`, which every run writes alongside the HTML report — so the site and the report can never disagree.

The pipeline itself is not serverless: a run takes several minutes, spends Helius credits and depends on the local SQLite ledger. The deploy model is therefore snapshot-based:

```powershell
uv run solana-brief run          # writes web/data/latest.json
git add web/data/latest.json
git commit -m "Snapshot"
git push                         # Vercel redeploys automatically
```

`.vercelignore` anchors its paths with a leading slash. An unanchored `data/` would also match `web/data/`, which is the snapshot the site needs.
