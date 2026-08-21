# Daily Solana Memecoin Brief

A local, novelty-first Solana intelligence brief. It reports measurable changes, never recommendations, and uses Dexscreener, RugCheck, Helius free tier, Jupiter's no-key lite quote API, Telegram Bot API, plus an optional operator-supplied X API account. Bubblemaps is linked for inspection but is not queried.

Every morning report opens with **the read**: the day's picks, one descriptive sentence each, above the fold. Below that sit the full evidence dossiers, the rolling 24-hour launch tape, and the screening funnel. This is deliberately described as discovery-feed coverage—not a claim to index every mint created on Solana, most of which never form a tracked market.

## The daily journal

The report opens with **what ran today**, not a shortlist. A coin enters through one of two doors:

- **Created inside the last 24 hours** and up at least 30%.
- **Any age, doing a 5x or better** on the day.
- **Bought by two or more tracked wallets**, whatever the price has done.

The third door is the one that earns its keep. The tracked-wallet scan runs during discovery rather than after it, so a coin several of these wallets are buying enters the record on conviction alone — before the move, which is the only reason to watch them at all. On a live day it surfaced a coin up 1.3%: invisible to every price test, bought by two wallets off the leaderboard.

A Dexscreener boost is that company's advertising product and plenty of honest teams buy one, so it is not treated as evidence of anything.

Everything above $250,000 market cap that clears the organic-runner gate is recorded for the public recap, ranked by real volume, trades and holder count before raw percent move. Paid boosts, weak holder distribution, dead socials, unlocked LP, high concentration, wash-trading shapes and hard fades are excluded rather than shown to the client.

Only conditions that make a coin uninvestable at any price remove it from the record, and those are listed separately under "ran, but disqualified":

- a live mint authority (supply can be inflated)
- a live freeze authority (holders can be frozen)
- liquidity neither locked nor burned (it can be pulled)
- bundled supply, meaning the top 10 circulating wallets hold more than 50%
- a manufactured tape, meaning many trades from very few wallets

A second gate removes coins whose market is manufactured rather than bought, or whose move is already over. These are not all rugs — some are perfectly safe to hold — but none of them is a record of what the market did, and one on a broadcast costs more credibility than an empty slot:

- volume above 150x the pool's own depth, or above 30x market cap, in 24 hours
- an average trade under $15, which is dust rather than demand
- fewer than 200 holders, which is not a distribution yet
- fewer than 300 trades in 24 hours, which is not a tape
- a book that is 85% buys across 300+ trades, which has no sellers in it
- **the pump-and-die shape**: a large printed gain with under 8% of the day's volume in the last six hours, meaning the move finished hours ago
- RugCheck's own `rugged` verdict, or a top 10 holding more than half the supply

Trade *speed* is deliberately not one of these tests. An earlier rule rejected anything above forty trades a minute; measured against a coin the client confirmed was normal, a hot launch runs 219 trades a minute over the hour and 428 over five minutes, and every coin the rule rejected turned out to trade larger size than that reference. One of them had 13,158 holders. Speed is what a crowd looks like; a bot looks like dust, so only the average trade size decides.

`[journal].venues` limits the record to chosen venues, `["pumpswap"]` by default. Note that pump.fun coins do not all end up there: on a measured day the biggest runner had migrated to Raydium, so this silently drops names. Empty the list to cover every venue.

A reused ticker and a paid Dexscreener boost are both shown on the row rather than treated as disqualifying. A boost is that company's advertising product, bought by honest teams; a shared ticker is common enough that rejecting every one of them threw away real runners.

On a live day this took sixteen coins to six. It removed one pool trading 1,112x its own liquidity, three printing 52 to 277 trades a minute, and three whose top ten wallets held 62-89% of the supply — including one showing a 244x. Every coin that survived carries a real holder count, from 371 to 7,563.

## Was the smart money in it?

The tracked wallets are a check on the day's runners, not a way into the record: a coin earns its place by moving, and the wallets answer whether the people who usually catch these moves were in it.

The absence is the interesting half. Measured on live data, these 66 wallets touch only about **one runner in seven**, so silence from all of them is the norm and not evidence by itself. Above `expect_tracked_wallets_above` the silence is reported on the row anyway, because a coin doing 10x or better that not one of them bought, sold or held was moved by somebody else. Lower that threshold as more wallets are added and coverage improves.

Everything softer becomes a label:Everything softer becomes a label: elevated concentration, a thin pool, paid boosts, a reused ticker, no linked socials, a very young pair, or a **fade** — up strongly on the day but down more than 15% in the last hour. That last one is the coin that hit an all-time high and gave it back, which the 24-hour number alone hides.

Runners are grouped by **shared lore**: Dexscreener's trending metas first, then a shared significant word in the name, which is how copycats of one story cluster. A lore that has already produced recent mints is flagged rather than presented as original.

## Tracked wallets

`[kol].wallets` holds a deduplicated leaderboard (66 wallets, named). Two questions are answered from their on-chain activity:

- **Conviction** — which coins several independent wallets bought inside the window, and which of them are still holding versus already closed. A coin bought by `min_buyers_to_flag` or more is flagged.
- **Where they made money** — realised SOL per mint, reconstructed from each wallet's own balance deltas.

Buys and sells are read from balance deltas rather than by classifying swap instructions, so the arithmetic stays correct across every DEX, aggregator and bot router. Network fees are excluded, wrapped SOL is ignored so the SOL leg of a swap is not double-counted, and a multi-hop route splits its SOL across the mints it moved. A wallet that opened a position before the window and closed it inside counts as a trader even though it has no buy to show.

The list is ordered by reported PnL and scanned newest-first with its own paced request budget, so a partial scan still carries the strongest wallets. Helius answers a burst of heavy wallet-history calls with `429 max usage reached` when the plan's credits are spent; the scan degrades to a warning and the rest of the report still ships.

## Monitored X accounts

Set `X_BEARER_TOKEN` in `.env` to enable the social evidence layer. `[x].accounts` in `config.toml` is the bounded desk list. The daily run searches only public posts from those handles during the report window and records posts, replies, quotes and reposts. Likes are not presented as observable KOL actions.

Every match retains the author, timestamp, interaction type, engagement metrics and original post URL. Matching a contract address or unique cashtag is labelled confirmed; a linked project account is probable; a name-only match is possible. The report never converts timing into a claim of causation. If no source-linked match exists, the card says so explicitly.

The summary is extractive and deterministic: it shortens the original post without inventing a narrative. The X source is optional and degrades independently, so a missing key or exhausted quota never blocks Dexscreener, Helius, Telegram or web delivery.

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
# Put your Helius key in .env. Add X_BEARER_TOKEN if social evidence is wanted.
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
uv run solana-brief pulse
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

The watcher polls only active watchlist tokens. Its default interval is one hour. It sends Telegram messages for exactly four event classes:

- Pool-balance proxy removal above the configured threshold, 10% by default.
- Registry-cluster wallets beginning to reduce their supply share.
- Holder growth changing from positive daily growth to an intraday decline.
- Creator-linked supply outflow.

It does not send price, volume, or recommendation alerts. Run one diagnostic cycle with `watcher --once`. Telegram credentials are required for an actual alert delivery.

## Hourly runner pulse

`uv run solana-brief pulse` runs one market check, refreshes `web/data/latest.json`, and records every runner that survives the journal screen into `web/data/pulse-state.json`. When the same mint passes 3 times inside 12 hours, it renders a clean signal image in `output/pulse-images/` and sends a Telegram pulse. If X posting credentials are present, it uploads that image to X and posts it.

The hourly pulse intentionally runs as a cheap Solana-only market check by default: smaller Birdeye ranked scan, no holder snapshots, no Helius enrichment, no tracked-wallet history, and no monitored-account X search. Those deep checks stay in the morning report; running them hourly would burn API credits fast. Edit `[pulse]` in `config.toml` if you want a different window, pass count, cooldown, image template path, or posting behavior. When you share the final image template, set `pulse.image_template_path` to that PNG/JPG and the token logo/stats will be rendered on top.

## Replay and tuning

After a live date has been archived, change thresholds in `config.toml` and run:

```powershell
uv run solana-brief replay YYYY-MM-DD
```

Replay is read-only for feature and outcome history and fails closed when an exact archived endpoint/request combination is missing. The output is written to `output/replay-YYYY-MM-DD.html`. Running the same archive with the same config is deterministic; changed thresholds can be compared without refetching present-day data.

## Delivery and scheduling

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, then set `telegram_enabled = true` in `config.toml`. The daily static report is written atomically to `output/latest.html`.

Email delivery uses Resend: set `RESEND_API_KEY` in `.env`, then `email_enabled = true`, `email_from`, and `email_to` in `config.toml`. The full report is rendered as a flat, inline-styled email (email clients cannot open the interactive HTML) and sent to every address in `email_to`. It wears the same fomo brand as the site and the overlays — the navy `#221D4B` masthead, the blue `#516AF6` stat band, the lavender `#EAEDFF` paper, the KOL/lore chips and the red rubber stamp — with every style inline and no `class` or `<style>` block, because Gmail and friends share none of the interactive page. Preview it offline before it faces an inbox: `uv run python scripts/email-preview.py` writes `output/email-preview.html`, and `--report-url <url>` writes the CTA-button variant. The default sender `onboarding@resend.dev` only delivers in testing — verify a domain in Resend and switch `email_from` to `brief@yourdomain.com` before relying on it. A missing credential warns and skips, exactly like Telegram.

The morning Telegram message is a digest, not the whole report: a header, the picks with one line each, up to three on-chain flags, any degraded sources, and an optional link. Set `delivery.report_url` to wherever `latest.html` is reachable and it is appended to the message. Set `delivery.telegram_digest = false` to send the full report instead. A missing credential warns and skips delivery; it never loses the report that already rendered.

The visual interface runs locally at `http://127.0.0.1:8765`. It filters the report, shows a live clock and scheduled-run countdown, monitors refresh state, and can start a new brief. It binds only to the local machine and exposes no wallet or trading action.

On Windows, `scripts/install-interface-startup.ps1` makes the local interface start automatically after sign-in. The 06:45 scheduled task replaces `output/latest.html` every morning, and the interface serves that new file immediately without needing a restart.

For cron, with the host timezone matching `run.timezone`:

```cron
45 6 * * * cd /absolute/path/to/repo && /absolute/path/to/uv run solana-brief run >> data/cron.log 2>&1
@reboot cd /absolute/path/to/repo && /absolute/path/to/uv run solana-brief watcher >> data/watcher.log 2>&1
```

On Windows, review and run `scripts/install-task.ps1` for the 06:45 daily job. Review and run `scripts/install-watcher-task.ps1` to register the watcher at logon and start it immediately.
Run `scripts/install-pulse-task.ps1` to register the hourly runner pulse locally; it calls `scripts/run-pulse.ps1`, refreshes the live JSON, commits the pulse state, and pushes through the same Vercel flow.

For no-laptop operation, the repo includes `.github/workflows/onchain-rundown.yml`. Add these repository secrets in GitHub Actions: `HELIUS_API_KEY`, `BIRDEYE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `RESEND_API_KEY`, optional `X_BEARER_TOKEN` for monitored-account evidence, and optional OAuth 1.0a posting secrets `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`. OAuth 1.0a posting tokens must be regenerated after the X app permission is changed from Read to Read and write. The workflow runs the morning report at 04:45 UTC and the pulse every hour, commits `web/data/latest.json` plus `web/data/pulse-state.json`, and lets Vercel redeploy from `main`.

The broad launch count comes from the continuous Helius collector, not Dexscreener's small trending sample. Run `scripts/install-launch-collector-startup.ps1` once to install the no-admin per-user startup shortcut. The collector listens to the official Pump program create instructions, writes them to `launch_events`, heals short disconnects from the latest 1,000 program transactions, and states the exact beginning of coverage in every report. Its first full rolling window is available after 24 hours of continuous collection; Dexscreener then supplies market data only for captured mints that established a tracked market.

## Configuration

Every decision threshold is in `config.toml`: hard filters, novelty, retirement, Helius request/page budgets, daily/weekly comparison age, divergence bounds, cluster and whale thresholds, anomaly baseline/sample size, social age, smart-money history, LP removal, reappearance, DEX dominance, Jupiter impact, and watcher cadence.

`[thresholds].chains` controls which chains are screened and defaults to `["solana"]`. The Dexscreener discovery and pair endpoints are chain-agnostic, so adding a chain is a configuration change—but RugCheck authority/LP checks and every Helius holder, cluster and creator analysis are Solana-only. Names on another chain would therefore ship with the safety layer marked unavailable, which is why the default stays as it is.

No automated execution, wallet connection, sentiment scoring, or generated hype narrative is included. X API usage is optional and uses the operator's own API plan.

## Hosted demo

The site lives in `web/` and is deployed on Vercel. It is a dependency-free static page that renders `web/data/latest.json`, which every run writes alongside the HTML report — so the site and the report can never disagree.

The pipeline itself is not serverless: a run takes several minutes, spends Helius credits and depends on the local SQLite ledger. The deploy model is therefore snapshot-based:

```powershell
uv run solana-brief run          # writes web/data/latest.json
git add web/data/latest.json
git commit -m "Snapshot"
git push                         # Vercel redeploys automatically
```

The installed Windows 06:45 task runs `scripts/run.ps1`. After a successful brief and Telegram digest, that script calls `scripts/publish-web.ps1`, which commits only `web/data/latest.json` and pushes `main`. Existing staged or unstaged work in other files is never included. If the remote branch has diverged, publishing stops instead of rebasing or overwriting work.

`.vercelignore` anchors its paths with a leading slash. An unanchored `data/` would also match `web/data/`, which is the snapshot the site needs.

## Stream overlay

`/overlay` is an OBS Browser Source. It reads the same snapshot as the site, so
what is on the broadcast and what is on the site cannot disagree, and no PNG is
designed by hand for a stream.

Mode comes from the query string, which makes **OBS scene switching the control
surface** — no backend and no companion app. Add one Browser Source per look and
toggle its visibility.

| URL | Use |
| --- | --- |
| `/overlay?mode=ticker` | Bottom strip of the day's runners. Safe to leave up all stream |
| `/overlay?mode=board&max=8` | Leaderboard panel for the segment |
| `/overlay?mode=card` | One coin, large, with contract and a QR to open it |
| `/overlay?mode=lower3` | Lower third while talking about one name |

Extra parameters: `coin=BOT` pins a symbol, `rotate=8` cycles every 8 seconds,
`side=left` moves the card or board, `at=tl|tr|bl|br` pins it to a corner,
`refresh=30` sets the live price interval, `nocontrol=1` opts a source out of the dock.

### Driving it live

`/control` is a **Custom Browser Dock** (View → Docks → Custom Browser Docks). It
lists the day's coins and puts whichever he clicks on screen, with prev/next,
random, a 10-second auto-cycle, and a hide that clears every overlay at once
without touching his scene.

Docks and browser sources share one origin inside OBS, so the dock writes the
state to `localStorage` and the overlays read it. No server, no account, no
latency, and it keeps working with the internet down. Each source keeps its own
mode from its own URL, so the dock never fights a scene: it only decides which
coin is up and whether anything shows.

Storage events are not delivered in every context OBS runs these in, so the
overlay also polls the key twice a second. That is imperceptible and never
misses a click.

### The two scenes

His camera scene has him centre-right, so the panel goes left. His trading scene
is a terminal with the camera bottom-left and content everywhere else, so the
panel tucks into a corner and is shown only for the segment.

| Scene | Suggested source |
| --- | --- |
| Camera | `/overlay?mode=card&at=tl` and `/overlay?mode=ticker` |
| Trading | `/overlay?mode=lower3` or `/overlay?mode=card&at=br` |

In OBS: **Sources → + → Browser**, paste the URL, set width and height to the
canvas size, and tick *Shutdown source when not visible* and *Refresh browser
when scene becomes active*. The page background is transparent, so nothing is
composited over the scene except the panel itself.

The QR opens `[overlay].trade_url_template` with `{mint}` substituted, so a
viewer scans instead of copying a contract address off a video. Point it at the
client's own referral link and the traffic is attributed to him.

Prices refresh from Dexscreener every 30 seconds while the overlay is on air, so
the numbers do not go stale on camera between daily runs. Everything expensive —
safety, tracked wallets, lore — stays from the morning run.
