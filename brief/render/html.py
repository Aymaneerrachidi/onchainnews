from __future__ import annotations

import html
import json
from collections import Counter

from brief.models import Brief, Candidate, LaunchRecord
from brief.render.formatting import money, pct, ratio


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _candidate(candidate: Candidate, index: int) -> str:
    token = candidate.token
    signal = candidate.signals
    badges = " / ".join([candidate.track, *candidate.badges]) or "SCREENED"
    age = "N/A" if signal.age_hours is None else f"{signal.age_hours:.0f}H"
    buys = "N/A" if signal.buy_imbalance_6h is None else f"{signal.buy_imbalance_6h * 100:.0f}%"
    holders = "UNAVAILABLE" if signal.holder_growth_24h is None else f"{signal.holder_growth_24h:+,}"
    exit_depth = "UNAVAILABLE" if candidate.exit_liquidity_sol is None else f"{candidate.exit_liquidity_sol:,.2f} SOL"
    authority = "UNAVAILABLE" if candidate.safety.mint_authority_renounced is None else ("RENOUNCED" if candidate.safety.mint_authority_renounced else "LIVE")
    top10 = "UNAVAILABLE" if candidate.safety.top10_pct is None else f"{candidate.safety.top10_pct:.1f}%"
    lp = "UNAVAILABLE" if candidate.safety.lp_locked_or_burned_pct is None else f"{candidate.safety.lp_locked_or_burned_pct:.0f}%"
    warnings = "".join(f"<li>{_e(warning)}</li>" for warning in candidate.warnings)
    warning_block = f'<ul class="warning-list">{warnings}</ul>' if warnings else '<p class="quiet">No candidate-level warnings.</p>'
    strength = "".join(f"<li>{_e(reason)}</li>" for reason in candidate.strength_reasons)
    interest = "".join(f"<li>{_e(reason)}</li>" for reason in candidate.interest_reasons)
    proof_badge = f"STRONG {len(candidate.strength_reasons)} / INTEREST {len(candidate.interest_reasons)}"
    cto = ""
    if candidate.cto:
        claim = candidate.cto.claim_date.strftime("%d %b %Y") if candidate.cto.claim_date else "date unavailable"
        cto = f'<dt>CTO claim</dt><dd>{_e(claim)}</dd>'
    return f"""
<details class="token-row searchable" data-search="{_e(token.symbol)} {_e(token.name)} {_e(token.mint)} {_e(badges)}">
  <summary>
    <span class="row-id">{index:02d}</span>
    <span class="token-name"><b>${_e(token.symbol)}</b><i>{_e(token.name)}</i></span>
    <span class="token-badge">[{_e(proof_badge)}]</span>
    <data value="{token.market_cap}">{_e(money(token.market_cap))}<i>MCAP</i></data>
    <data value="{token.volume_24h}">{_e(money(token.volume_24h))}<i>24H VOL</i></data>
    <data value="{signal.turnover:.4f}">{_e(ratio(signal.turnover))}<i>TURNOVER</i></data>
    <span class="disclosure">+</span>
  </summary>
  <div class="token-dossier">
    <p class="token-read">{_e(candidate.read)}</p>
    <dl>
      <dt>Age</dt><dd>{_e(age)}</dd>
      <dt>6h buys</dt><dd>{_e(buys)}</dd>
      <dt>Acceleration</dt><dd>{_e(pct(signal.acceleration, 0))}</dd>
      <dt>Holder delta</dt><dd>{_e(holders)}</dd>
      <dt>Exit below 5% impact</dt><dd>{_e(exit_depth)}</dd>
      <dt>Mint authority</dt><dd>{_e(authority)}</dd>
      <dt>LP locked/burned</dt><dd>{_e(lp)}</dd>
      <dt>Top 10</dt><dd>{_e(top10)}</dd>
      {cto}
    </dl>
    <div class="dossier-notes">
      <div class="editorial-proof">
        <section><h3>Why it reads strong</h3><ul>{strength}</ul></section>
        <section><h3>Why it is interesting today</h3><ul>{interest}</ul></section>
      </div>
      <h3>Unknowns and warnings</h3>
      {warning_block}
      <a href="{_e(token.url)}" target="_blank" rel="noreferrer">OPEN DEXSCREENER ↗</a>
    </div>
  </div>
</details>"""


def _candidate_section(section_id: str, title: str, candidates: list[Candidate]) -> str:
    rows = "".join(_candidate(candidate, index) for index, candidate in enumerate(candidates, 1))
    if not rows:
        rows = '<div class="empty-state searchable" data-search="">NO TOKENS CLEARED THIS SECTION.</div>'
    return f"""
<section id="{section_id}" class="report-section reveal">
  <header class="section-header"><h2>{_e(title)}</h2><output>{len(candidates):02d}</output></header>
  <div class="token-ledger">{rows}</div>
</section>"""


def _launch(launch: LaunchRecord, index: int, generated_at) -> str:
    token = launch.token
    created = token.pair_created_at
    created_text = created.strftime("%d %b / %H:%M") if created else "TIME UNAVAILABLE"
    age_hours = max(0.0, (generated_at - created).total_seconds() / 3600) if created else None
    age = f"{age_hours:.1f}H" if age_hours is not None else "N/A"
    buys = token.txns_6h.buy_ratio
    buy_text = f"{buys:.0%}" if buys is not None else "N/A"
    evidence = "".join(f"<li>{_e(item)}</li>" for item in launch.signals) or "<li>No standout signal crossed its configured threshold.</li>"
    reasons = "".join(f"<li>{_e(item)}</li>" for item in launch.reasons)
    return f"""
<details class="launch-row searchable" data-search="{_e(token.symbol)} {_e(token.name)} {_e(token.mint)} {_e(launch.status)} {_e(' '.join(launch.reasons))} {_e(' '.join(launch.signals))}">
  <summary>
    <span class="row-id">{index:03d}</span>
    <time datetime="{_e(created.isoformat() if created else '')}">{_e(created_text)}<i>PAIR CREATED</i></time>
    <span class="token-name"><b>${_e(token.symbol)}</b><i>{_e(token.name)}</i></span>
    <strong class="launch-status status-{_e(launch.status.lower())}">{_e(launch.status)}</strong>
    <data>{_e(money(token.market_cap))}<i>MCAP</i></data>
    <data>{_e(money(token.liquidity_usd))}<i>LIQUIDITY</i></data>
    <data>{_e(money(token.volume_24h))}<i>24H VOL</i></data>
    <span class="disclosure">+</span>
  </summary>
  <div class="launch-detail">
    <dl>
      <dt>Age at report</dt><dd>{_e(age)}</dd>
      <dt>DEX</dt><dd>{_e(token.dex_id)}</dd>
      <dt>6h transactions</dt><dd>{token.txns_6h.total:,}</dd>
      <dt>6h buys</dt><dd>{_e(buy_text)}</dd>
    </dl>
    <section><h3>Screening result</h3><ul>{reasons}</ul></section>
    <section><h3>Measured interest</h3><ul>{evidence}</ul><a href="{_e(token.url)}" target="_blank" rel="noreferrer">OPEN DEXSCREENER ↗</a></section>
  </div>
</details>"""



def _runner(candidate: Candidate, index: int) -> str:
    token = candidate.token
    signal = candidate.signals
    age = "N/A" if signal.age_hours is None else (
        f"{signal.age_hours:.0f}H" if signal.age_hours < 48 else f"{signal.age_hours / 24:.0f}D"
    )
    multiple = f"{candidate.run_multiple:.1f}x" if candidate.run_multiple >= 2 else f"{token.price_change_24h:+.0f}%"
    labels = "".join(f"<li>{_e(label)}</li>" for label in candidate.risk_labels)
    label_block = f'<ul class="risk-list">{labels}</ul>' if labels else '<p class="quiet">No flags raised.</p>'
    kol = ""
    if candidate.kol_buyers:
        kol = (
            f'<span class="kol-chip">{len(candidate.kol_buyers)} KOL</span>'
        )
    lore = f'<span class="lore-chip">{_e(candidate.lore)}</span>' if candidate.lore else ""
    fade = ' class="faded"' if candidate.faded_from_peak is not None else ""
    return f"""
<details class="runner-row searchable" data-search="{_e(token.symbol)} {_e(token.name)} {_e(token.mint)} {_e(candidate.lore)} {_e(' '.join(candidate.risk_labels))}">
  <summary{fade}>
    <span class="row-id">{index:02d}</span>
    <span class="token-name"><b>${_e(token.symbol)}</b><i>{_e(token.name)}</i></span>
    <data class="run-size">{_e(multiple)}<i>24H</i></data>
    <data>{_e(pct(token.price_change_1h, 0))}<i>1H</i></data>
    <data>{_e(money(token.market_cap))}<i>MCAP</i></data>
    <data>{_e(money(token.volume_24h))}<i>VOL</i></data>
    <data>{_e(money(token.liquidity_usd))}<i>LIQ</i></data>
    <data>{_e(age)}<i>AGE</i></data>
    <span class="chips">{kol}{lore}</span>
    <span class="disclosure">+</span>
  </summary>
  <div class="runner-detail">
    <dl>
      <dt>Turnover</dt><dd>{_e(ratio(signal.turnover))}</dd>
      <dt>6h buys</dt><dd>{_e("N/A" if signal.buy_imbalance_6h is None else f"{signal.buy_imbalance_6h:.0%}")}</dd>
      <dt>6h trades</dt><dd>{token.txns_6h.total:,}</dd>
      <dt>6h change</dt><dd>{_e(pct(token.price_change_6h, 0))}</dd>
      <dt>DEX</dt><dd>{_e(token.dex_id)}</dd>
      <dt>Lore</dt><dd>{_e(candidate.lore or "unclustered")}{"" if candidate.lore_is_fresh else " (repeat)"}</dd>
    </dl>
    <section><h3>Flags</h3>{label_block}</section>
    <section>
      <h3>KOL buyers</h3>
      {_kol_detail(candidate)}
      <a href="{_e(token.url)}" target="_blank" rel="noreferrer">OPEN DEXSCREENER &#8599;</a>
    </section>
  </div>
</details>"""


def _runner_section(brief: Brief) -> str:
    rows = "".join(_runner(candidate, index) for index, candidate in enumerate(brief.runners, 1))
    if not rows:
        rows = '<div class="empty-state">NOTHING RAN TODAY THAT CLEARED THE FLOORS.</div>'
    return f"""
<section id="runners" class="report-section reveal">
  <header class="section-header"><h2>Runners of the day</h2><output>{len(brief.runners):02d}</output></header>
  <div class="runner-ledger">{rows}</div>
</section>"""


def _lore_section(brief: Brief) -> str:
    if not brief.lore_groups:
        return ""
    groups = sorted(brief.lore_groups.items(), key=lambda item: len(item[1]), reverse=True)
    blocks = "".join(
        f"""
<li class="lore-group searchable" data-search="{_e(name)}">
  <b>{_e(name)}</b>
  <span>{len(members)} runners</span>
  <p>{_e(", ".join("$" + candidate.token.symbol for candidate in members))}</p>
</li>"""
        for name, members in groups
    )
    return f"""
<section id="lore" class="report-section reveal">
  <header class="section-header"><h2>Shared lore</h2><output>{len(groups):02d}</output></header>
  <ul class="lore-list">{blocks}</ul>
</section>"""


def _kol_detail(candidate: Candidate) -> str:
    if not candidate.kol_buyers:
        gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
        traders = [row for row in (gmgn.get("renownedTraders") or []) if not row.get("suspicious")]
        if traders:
            names = "".join(
                f"<li>{_e(str(row.get('name') or row.get('address') or '')[:40])}"
                f"{' (holding)' if row.get('holding') else ' (closed)'}</li>"
                for row in traders[:12]
            )
            return f"<ul>{names}</ul>"
        count = int(gmgn.get("kolCount") or 0)
        if count:
            return f'<p class="quiet">GMGN mapped {count} renowned/KOL wallets on this token.</p>'
        return '<p class="quiet">No KOL participation was verified.</p>'
    names = "".join(
        f"<li>{_e(name)}{' (holding)' if name in candidate.kol_holders else ' (closed)'}</li>"
        for name in candidate.kol_buyers
    )
    realised = (
        f'<p class="{"gain" if candidate.kol_realised_sol >= 0 else "loss"}">'
        f'Realised {candidate.kol_realised_sol:+,.1f} SOL on {candidate.kol_sol_spent:,.1f} SOL bought.</p>'
    )
    return f"<ul>{names}</ul>{realised}"


def _kol_section(brief: Brief) -> str:
    if not brief.kol_wallet_count:
        return ""
    conviction = "".join(
        f"""
<li class="searchable" data-search="{_e(c.token.symbol)} {_e(c.token.mint)} kol">
  <b>${_e(c.token.symbol)}</b>
  <data>{max(len(c.kol_buyers), int((c.provider_evidence.get("gmgn", {}) or {}).get("kolCount") or 0))} KOL</data>
  <span>{_e(", ".join(c.kol_buyers[:10]) or "GMGN renowned-wallet coverage")}{"..." if len(c.kol_buyers) > 10 else ""}</span>
  <i>{_e("still holding: " + ", ".join(c.kol_holders[:4]) if c.kol_holders else "see token-specific GMGN outcomes")}</i>
</li>"""
        for c in brief.kol_flagged
    ) or '<li class="empty-state">No coin was bought by enough tracked wallets today.</li>'
    profit = "".join(
        f"""
<li class="searchable" data-search="{_e(symbol)} {_e(mint)}">
  <b>${_e(symbol)}</b>
  <data class="{'gain' if realised >= 0 else 'loss'}">{realised:+,.1f} SOL</data>
  <span>{traders} tracked wallet(s) traded it &middot; <code>{_e(mint[:4] + ".." + mint[-4:])}</code></span>
</li>"""
        for mint, symbol, realised, traders in brief.kol_profit_table
    ) or '<li class="empty-state">No realised profit or loss measured in this window.</li>'
    return f"""
<section id="kol" class="report-section reveal">
  <header class="section-header"><h2>Tracked wallets</h2><output>{brief.kol_wallet_count:02d}</output></header>
  <div class="split-grid">
    <section>
      <h3 class="sub-head">Conviction &mdash; bought by several wallets</h3>
      <ul class="kol-list">{conviction}</ul>
    </section>
    <section>
      <h3 class="sub-head">Where they made money (realised, this window)</h3>
      <ul class="kol-list">{profit}</ul>
    </section>
  </div>
</section>"""


def report_picks(brief: Brief) -> list[Candidate]:
    """The day's picks: new-and-moving first, then movers, then CTOS.

    Single definition shared by the site and the email so the two can never
    disagree. Mirror it in any other renderer that shows picks.
    """
    return [*brief.new_and_moving, *brief.movers, *brief.ctos]


def render_html(brief: Brief) -> str:
    sc = brief.scorecard
    generated_iso = brief.generated_at.isoformat()
    generated = brief.generated_at.strftime("%a %d %b %Y / %H:%M %Z")
    window_start = brief.window_start or brief.generated_at
    window_text = f"{window_start.strftime('%d %b / %H:%M')} → {brief.generated_at.strftime('%d %b / %H:%M %Z')}"
    shortlist = list(brief.new_and_moving)
    picks = report_picks(brief)
    pick_rows = "".join(
        f"""
<li class="pick searchable" data-search="{_e(candidate.token.symbol)} {_e(candidate.token.name)} {_e(candidate.token.mint)} {_e(candidate.track)}">
  <span class="pick-track track-{_e(candidate.track.lower())}">{_e(candidate.track)}</span>
  <span class="pick-symbol">${_e(candidate.token.symbol)}</span>
  <p class="pick-read">{_e(candidate.read)}</p>
  <a href="{_e(candidate.token.url)}" target="_blank" rel="noreferrer">CHART ↗</a>
</li>"""
        for candidate in picks
    ) or (
        '<li class="empty-state">NOTHING CLEARED THE BAR TODAY. AN EMPTY BRIEF IS A RESULT, NOT AN OUTAGE.</li>'
    )
    rejection_counts: Counter[str] = Counter()
    for launch in brief.launches_last_24h:
        if launch.status == "SHORTLIST":
            continue
        for reason in launch.reasons or [launch.status.lower()]:
            rejection_counts[reason] += 1
    screening = "".join(
        f'<li><b>{_e(reason)}</b><span>{count} launch{"es" if count != 1 else ""}</span></li>'
        for reason, count in rejection_counts.most_common(12)
    ) or '<li class="empty-state">No rejection reasons in this window.</li>'
    current_launch_mints = {launch.token.mint for launch in brief.launches_last_24h}
    daily_onchain = [finding for finding in brief.onchain if finding.mint in current_launch_mints]
    findings = "".join(
        f"""
<article class="finding searchable" data-search="{_e(finding.symbol)} {_e(finding.mint)} {_e(finding.headline)}">
  <span class="finding-index">{index:02d}</span>
  <div class="finding-body">
    <h3>${_e(finding.symbol)}</h3>
    <p class="finding-head">{_e(finding.headline)}</p>
    {''.join(f'<p>{_e(detail)}</p>' for detail in finding.details)}
    <a href="{_e(finding.bubblemap_url)}" target="_blank" rel="noreferrer">BUBBLEMAP ↗</a>
  </div>
  <samp>{_e(finding.status.upper())}</samp>
</article>"""
        for index, finding in enumerate(daily_onchain, 1)
    ) or '<div class="empty-state">NO MATERIAL ON-CHAIN CHANGE SINCE THE PRIOR SNAPSHOT.</div>'
    metas = "".join(
        f"""
<li class="searchable" data-search="{_e(meta.name)}">
  <b>{_e(meta.name)}</b><data>{_e(pct(meta.change_24h))}</data><span>{_e(money(meta.market_cap))}</span><span>{meta.token_count} TOKENS</span>
</li>"""
        for meta in brief.metas
    ) or '<li class="empty-state">META DATA UNAVAILABLE.</li>'
    statuses = "".join(
        f'<li class="{"ok" if status.available else "down"}"><b>{_e(status.name)}</b><span>{_e(status.detail)}</span><output>{"OK" if status.available else "--"}</output></li>'
        for status in brief.source_statuses
    )
    exclusions = "".join(
        f'<li class="searchable" data-search="{_e(item.token.symbol)} {_e(item.token.mint)} {_e(" ".join(item.reasons))}"><b>${_e(item.token.symbol)}</b><span>{_e(" / ".join(item.reasons))}</span><i>{_e(item.stage)}</i></li>'
        for item in brief.excluded
    ) or '<li class="empty-state">NO FILTERED TOKENS.</li>'
    weekly = "".join(f'<li>{_e(note)}</li>' for note in brief.weekly_notes) or '<li>Not enough matured 72h observations for a weekly cohort.</li>'
    quality = "".join(f'<li>{_e(alert)}</li>' for alert in brief.quality_alerts) or '<li>No material data-quality shift detected.</li>'
    generated_json = json.dumps(generated_iso)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Local Solana on-chain intelligence brief with holder, liquidity, cluster and outcome changes.">
<meta name="robots" content="noindex,nofollow">
<title>Solana Brief / {_e(brief.generated_at.strftime('%d %b %Y'))}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%23e61919'/><path d='M5 8h22v4H5zm0 12h22v4H5z' fill='%230b0b0b'/></svg>">
<style>
:root{{--paper:#e9e7df;--paper-2:#f3f1ea;--ink:#0b0b0b;--muted:#66645e;--line:#24231f;--red:#e61919;--green:#237343;--unit:clamp(.85rem,1.1vw,1rem)}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;overflow-x:hidden;background:var(--paper);color:var(--ink);font-family:"IBM Plex Mono","Cascadia Code","Courier New",monospace;font-size:var(--unit);font-variant-numeric:tabular-nums}}
body:before{{content:"";position:fixed;inset:0;pointer-events:none;opacity:.055;z-index:4;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.55'/%3E%3C/svg%3E")}}
a{{color:inherit;text-underline-offset:.24em}}a:hover{{color:var(--red)}}button,input,summary{{font:inherit}}button:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible{{outline:3px solid var(--red);outline-offset:2px}}.skip{{position:fixed;left:1rem;top:-5rem;background:var(--red);color:white;padding:.75rem;z-index:5}}.skip:focus{{top:1rem}}
.topbar{{position:sticky;top:0;z-index:3;display:grid;grid-template-columns:auto 1fr auto;background:var(--ink);color:var(--paper);border-bottom:4px solid var(--red)}}.topbar>a{{padding:1rem 1.25rem;text-decoration:none;font-weight:800}}.topbar nav{{display:flex;align-items:stretch;overflow:auto;border-left:1px solid #45433e}}.topbar nav a{{padding:1rem;border-right:1px solid #45433e;text-decoration:none;font-size:.75rem;white-space:nowrap}}.topbar nav a:hover,.topbar nav a.active{{background:var(--red);color:white}}.run-control{{display:flex;grid-column:3}}.run-control button{{border:0;border-left:1px solid #45433e;background:var(--red);color:white;padding:0 1.25rem;cursor:pointer;font-weight:800;white-space:nowrap}}.run-control button:disabled{{background:#45433e;color:#a8a59c;cursor:wait}}
main{{max-width:96rem;margin:0 auto;border-left:1px solid var(--line);border-right:1px solid var(--line)}}.masthead{{display:grid;grid-template-columns:2fr 1fr;border-bottom:1px solid var(--line)}}.mast-title{{min-width:0;padding:2rem 1.5rem 2.6rem;border-right:1px solid var(--line);overflow:hidden}}.mast-title h1{{font-family:Impact,"Franklin Gothic Heavy",sans-serif;font-size:clamp(4rem,11vw,11rem);letter-spacing:-.045em;line-height:.78;margin:0;text-transform:uppercase;white-space:nowrap}}.mast-title p{{margin:2rem 0 0;max-width:62ch;overflow-wrap:anywhere}}.mast-telemetry{{min-width:0;display:grid;grid-template-rows:repeat(3,1fr)}}.telemetry-cell{{padding:1.25rem;border-bottom:1px solid var(--line);display:flex;flex-direction:column;justify-content:space-between;gap:1rem}}.telemetry-cell:last-child{{border:0}}.telemetry-cell span,.telemetry-cell time{{font-size:.7rem;color:var(--muted)}}.telemetry-cell output{{font-size:clamp(1.2rem,2vw,2rem);font-weight:800}}
.score-strip{{display:grid;grid-template-columns:repeat(6,1fr);background:var(--line);gap:1px;border-bottom:1px solid var(--line)}}.score-strip>div{{background:var(--paper-2);padding:1.2rem}}.score-strip span{{display:block;font-size:.67rem;color:var(--muted);margin-bottom:.75rem}}.score-strip output{{font-size:clamp(1.2rem,2.5vw,2.4rem);font-weight:800}}.score-strip .critical output{{color:var(--red)}}
.utility{{display:grid;grid-template-columns:1fr auto;border-bottom:1px solid var(--line)}}.search{{display:flex;align-items:center;padding:.8rem 1rem;gap:.7rem}}.search input{{width:100%;border:0;background:transparent;color:var(--ink);outline:0}}.search input::placeholder{{color:var(--muted)}}.result-count{{padding:.8rem 1rem;border-left:1px solid var(--line)}}
.report-section{{border-bottom:1px solid var(--line)}}.section-header{{display:grid;grid-template-columns:1fr auto;background:var(--ink);color:var(--paper)}}.section-header h2{{font-family:Impact,"Franklin Gothic Heavy",sans-serif;font-size:clamp(2rem,5vw,5rem);letter-spacing:-.025em;line-height:.9;margin:0;padding:1rem 1.4rem;text-transform:uppercase;font-weight:400}}.section-header output{{display:grid;place-items:center;min-width:5.5rem;border-left:1px solid #45433e;color:var(--red);font-size:2rem}}
.finding{{display:grid;grid-template-columns:5.5rem 1fr auto;border-bottom:1px solid var(--line);background:var(--paper-2)}}.finding:last-child{{border:0}}.finding-index{{font-size:2.4rem;padding:1.2rem;border-right:1px solid var(--line);color:var(--red);font-weight:800}}.finding-body{{padding:1.2rem 1.4rem}}.finding-body h3{{font-size:1.5rem;margin:0 0 .65rem}}.finding-body p{{margin:.35rem 0;max-width:85ch;line-height:1.5}}.finding-head{{font-weight:800}}.finding-body a{{display:inline-block;margin-top:.8rem;font-size:.75rem;font-weight:800}}.finding>samp{{padding:1.2rem;border-left:1px solid var(--line);font-size:.65rem}}
.meta-list,.status-list,.retrospective,.quality-list,.filtered-list,.warning-list{{list-style:none;margin:0;padding:0}}.meta-list li{{display:grid;grid-template-columns:2fr repeat(3,1fr);padding:.9rem 1.2rem;border-bottom:1px solid var(--line)}}.meta-list li:last-child{{border:0}}.meta-list data{{font-weight:800}}.token-row{{border-bottom:1px solid var(--line);background:var(--paper-2)}}.token-row[hidden],.searchable[hidden]{{display:none}}.token-row summary{{display:grid;grid-template-columns:4rem minmax(12rem,2fr) minmax(8rem,1fr) repeat(3,minmax(7rem,1fr)) 3rem;align-items:center;min-height:5rem;cursor:pointer;list-style:none}}.token-row summary::-webkit-details-marker{{display:none}}.token-row summary:hover{{background:#dedbd1}}.row-id{{align-self:stretch;display:grid;place-items:center;border-right:1px solid var(--line);color:var(--red)}}.token-name{{padding:.8rem 1rem;display:flex;flex-direction:column}}.token-name b{{font-size:1.15rem}}.token-name i,.token-row data i{{font-size:.62rem;color:var(--muted);font-style:normal;margin-top:.3rem}}.token-badge{{font-size:.7rem}}.token-row data{{padding:.8rem;border-left:1px solid var(--line);display:flex;flex-direction:column;font-weight:800}}.disclosure{{font-size:1.5rem;text-align:center}}.token-row[open] .disclosure{{color:var(--red)}}.token-dossier{{display:grid;grid-template-columns:2fr 1fr;border-top:1px solid var(--line);background:var(--paper)}}.token-dossier dl{{display:grid;grid-template-columns:repeat(4,1fr);margin:0}}.token-dossier dt,.token-dossier dd{{margin:0;padding:.8rem;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}}.token-dossier dt{{font-size:.63rem;color:var(--muted)}}.token-dossier dd{{font-weight:800}}.dossier-notes{{padding:1rem}}.warning-list li{{color:var(--red);margin-bottom:.5rem}}.quiet{{color:var(--muted)}}.dossier-notes a{{display:inline-block;margin-top:1rem;font-weight:800;font-size:.75rem}}
.runner-row{{border-bottom:1px solid var(--line);background:var(--paper-2)}}.runner-row summary{{display:grid;grid-template-columns:3.4rem minmax(9rem,1.6fr) repeat(6,minmax(5rem,.8fr)) minmax(6rem,auto) 2.6rem;align-items:center;min-height:4.4rem;cursor:pointer;list-style:none}}.runner-row summary::-webkit-details-marker{{display:none}}.runner-row summary:hover{{background:#dedbd1}}.runner-row summary.faded{{background:#f0e4e4}}.runner-row data{{padding:.7rem .5rem;border-left:1px solid var(--line);display:flex;flex-direction:column;font-weight:800}}.runner-row data i{{font-size:.58rem;color:var(--muted);font-style:normal;margin-top:.25rem}}.run-size{{color:var(--green)}}.chips{{display:flex;gap:.35rem;padding:0 .5rem;flex-wrap:wrap}}.kol-chip,.lore-chip{{font-size:.58rem;font-weight:800;padding:.2rem .4rem;white-space:nowrap}}.kol-chip{{background:var(--red);color:#fff}}.lore-chip{{background:var(--ink);color:var(--paper)}}.runner-detail{{display:grid;grid-template-columns:1.4fr 1fr 1fr;border-top:1px solid var(--line);background:var(--paper)}}.runner-detail>*{{padding:1rem;border-right:1px solid var(--line)}}.runner-detail dl{{display:grid;grid-template-columns:1fr 1fr;margin:0;padding:1rem}}.runner-detail dt,.runner-detail dd{{margin:0;padding:.4rem;border-bottom:1px solid #aaa79e}}.runner-detail dt{{font-size:.63rem;color:var(--muted)}}.runner-detail dd{{font-weight:800}}.runner-detail h3{{margin:0 0 .6rem;font-size:.72rem}}.runner-detail ul{{margin:0;padding-left:1.1rem;line-height:1.5;font-size:.78rem}}.risk-list{{list-style:none;padding:0;margin:0}}.risk-list li{{color:var(--red);font-size:.76rem;margin-bottom:.35rem}}.kol-list{{list-style:none;margin:0;padding:0}}.kol-list li{{display:grid;grid-template-columns:7rem 7rem 1fr;gap:.8rem;align-items:baseline;padding:.75rem 1.2rem;border-bottom:1px solid var(--line);font-size:.78rem}}.kol-list li i{{grid-column:1/-1;color:var(--muted);font-style:normal;font-size:.68rem}}.kol-list b{{font-size:.95rem}}.kol-list data{{font-weight:800}}.gain{{color:var(--green)}}.loss{{color:var(--red)}}.sub-head{{margin:0;padding:1rem 1.2rem;font-size:.72rem;border-bottom:1px solid var(--line);background:var(--paper-2)}}.lore-list{{list-style:none;margin:0;padding:0}}.lore-group{{display:grid;grid-template-columns:12rem 7rem 1fr;gap:1rem;align-items:baseline;padding:1rem 1.3rem;border-bottom:1px solid var(--line)}}.lore-group b{{font-size:1.05rem}}.lore-group p{{margin:0;color:var(--muted)}}
.pick-list{{list-style:none;margin:0;padding:0}}.pick{{display:grid;grid-template-columns:7rem 9rem 1fr auto;align-items:center;gap:1rem;padding:1.1rem 1.3rem;border-bottom:1px solid var(--line);background:var(--paper-2)}}.pick:last-child{{border-bottom:0}}.pick-track{{justify-self:start;padding:.3rem .6rem;font-size:.62rem;font-weight:800;background:var(--ink);color:var(--paper)}}.track-mover{{background:var(--red);color:#fff}}.track-cto{{background:var(--green);color:#fff}}.pick-symbol{{font-size:1.35rem;font-weight:800}}.pick-read{{margin:0;line-height:1.5;max-width:88ch}}.pick a{{font-size:.7rem;font-weight:800;white-space:nowrap}}.token-read{{grid-column:1/-1;margin:0;padding:1rem 1.2rem;border-bottom:1px solid var(--line);line-height:1.55;font-weight:800}}
.coverage-note{{margin:0;padding:1rem 1.3rem;border-bottom:1px solid var(--line);max-width:none;font-size:.75rem;color:var(--muted);line-height:1.5}}.launch-tape{{max-height:46rem;overflow:auto}}.launch-row{{border-bottom:1px solid var(--line);background:var(--paper-2)}}.launch-row summary{{display:grid;grid-template-columns:4rem 10rem minmax(11rem,2fr) 8rem repeat(3,minmax(7rem,1fr)) 3rem;align-items:center;min-height:4.7rem;cursor:pointer;list-style:none}}.launch-row summary::-webkit-details-marker{{display:none}}.launch-row summary:hover{{background:#dedbd1}}.launch-row time,.launch-row data{{padding:.8rem;border-right:1px solid var(--line);display:flex;flex-direction:column;font-weight:800}}.launch-row time i,.launch-row data i{{font-size:.6rem;color:var(--muted);font-style:normal;margin-top:.3rem}}.launch-status{{align-self:stretch;display:grid;place-items:center;border-left:1px solid var(--line);border-right:1px solid var(--line);font-size:.68rem}}.status-shortlist{{background:var(--ink);color:var(--paper)}}.status-filtered{{background:var(--red);color:white}}.status-cleared,.status-monitored{{color:var(--green)}}.launch-detail{{display:grid;grid-template-columns:1fr 1.2fr 1.2fr;border-top:1px solid var(--line);background:var(--paper)}}.launch-detail>*,.launch-detail dl{{margin:0;padding:1rem;border-right:1px solid var(--line)}}.launch-detail dl{{display:grid;grid-template-columns:1fr 1fr}}.launch-detail dt,.launch-detail dd{{margin:0;padding:.45rem;border-bottom:1px solid #aaa79e}}.launch-detail dt{{font-size:.65rem;color:var(--muted)}}.launch-detail h3{{margin:0 0 .7rem;font-size:.8rem}}.launch-detail ul{{margin:0;padding-left:1.2rem;line-height:1.5}}.launch-detail a{{display:inline-block;margin-top:1rem;font-size:.72rem;font-weight:800}}
.selection-method{{display:grid;grid-template-columns:12rem 1fr 1fr;border-bottom:1px solid var(--line);background:var(--paper-2)}}.selection-method>h2{{font-size:1rem;margin:0;padding:1.2rem;border-right:1px solid var(--line)}}.selection-method>div{{padding:1.2rem;border-right:1px solid var(--line)}}.selection-method>div:last-child{{border-right:0}}.selection-method h3,.editorial-proof h3,.dossier-notes>h3{{font-size:.72rem;margin:0 0 .65rem}}.selection-method p{{font-size:.76rem;line-height:1.5;margin:0}}.selection-rule{{grid-column:1/-1;border-top:1px solid var(--line);color:var(--muted)}}.editorial-proof{{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--line);margin:-1rem -1rem 1rem}}.editorial-proof section{{padding:1rem;border-right:1px solid var(--line)}}.editorial-proof section:last-child{{border-right:0}}.editorial-proof ul{{margin:0;padding-left:1.1rem;font-size:.75rem;line-height:1.55}}
.outcome-row{{display:grid;grid-template-columns:repeat(6,1fr);border-bottom:1px solid var(--line)}}.outcome-row div{{padding:1rem;border-right:1px solid var(--line)}}.outcome-row span{{display:block;color:var(--muted);font-size:.65rem;margin-bottom:.5rem}}.outcome-row output{{font-weight:800;font-size:1.25rem}}.split-grid{{display:grid;grid-template-columns:1fr 1fr}}.split-grid>section:first-child{{border-right:1px solid var(--line)}}.retrospective li,.quality-list li{{padding:1rem 1.2rem;border-bottom:1px solid var(--line);line-height:1.5}}.filtered-list{{max-height:32rem;overflow:auto}}.filtered-list li{{display:grid;grid-template-columns:10rem 1fr auto;gap:1rem;padding:.7rem 1rem;border-bottom:1px solid var(--line);font-size:.75rem}}.filtered-list i{{color:var(--muted)}}.status-list li{{display:grid;grid-template-columns:minmax(12rem,1fr) 3fr 4rem;border-bottom:1px solid var(--line)}}.status-list li>*{{padding:.8rem 1rem}}.status-list output{{text-align:center;border-left:1px solid var(--line);font-weight:800}}.status-list .down output{{background:var(--red);color:white}}.status-list .ok output{{color:var(--green)}}.empty-state{{padding:2rem 1.4rem;color:var(--muted)}}
footer{{display:grid;grid-template-columns:1fr auto;padding:1.2rem 1.4rem;background:var(--ink);color:var(--paper);font-size:.7rem}}.reveal{{opacity:0;transition:opacity 240ms ease}}.reveal.visible{{opacity:1}}#run-message{{position:fixed;right:1rem;bottom:1rem;max-width:30rem;background:var(--ink);color:var(--paper);border-left:5px solid var(--red);padding:1rem;z-index:3;display:none}}
@media(max-width:900px){{.masthead,.split-grid{{grid-template-columns:1fr}}.runner-row summary{{grid-template-columns:3rem 1fr 5rem 5rem 2.5rem}}.runner-row summary data:nth-of-type(n+3){{display:none}}.runner-detail{{grid-template-columns:1fr}}.lore-group{{grid-template-columns:1fr}}.pick{{grid-template-columns:6rem 1fr;row-gap:.5rem}}.pick-read{{grid-column:1/-1}}.mast-title{{border-right:0;border-bottom:1px solid var(--line)}}.score-strip,.outcome-row{{grid-template-columns:repeat(3,1fr)}}.selection-method{{grid-template-columns:1fr 1fr}}.selection-method>h2{{grid-column:1/-1;border-right:0;border-bottom:1px solid var(--line)}}.token-row summary{{grid-template-columns:3rem 2fr 1fr 3rem}}.token-row summary data,.token-badge{{display:none}}.token-dossier{{grid-template-columns:1fr}}.token-dossier dl{{grid-template-columns:repeat(2,1fr)}}.launch-row summary{{grid-template-columns:3rem 9rem 2fr 7rem 3rem}}.launch-row summary data{{display:none}}.launch-detail{{grid-template-columns:1fr}}.launch-detail>*{{border-right:0;border-bottom:1px solid var(--line)}}.split-grid>section:first-child{{border-right:0;border-bottom:1px solid var(--line)}}}}
@media(max-width:620px){{.topbar{{grid-template-columns:auto 1fr auto}}.topbar nav{{display:none}}.topbar>a{{padding:.9rem 1rem}}.run-control button{{padding:0 .85rem;font-size:.75rem}}.mast-title h1{{font-size:20vw}}.score-strip,.outcome-row{{grid-template-columns:repeat(2,1fr)}}.finding{{grid-template-columns:3.5rem 1fr}}.finding-index{{font-size:1.4rem;padding:.8rem}}.finding>samp{{display:none}}.meta-list li{{grid-template-columns:2fr 1fr}}.meta-list li span{{display:none}}.launch-row summary{{grid-template-columns:3rem 7rem 1fr 3rem}}.launch-status{{display:none}}.launch-row .token-name{{min-width:0}}.filtered-list li{{grid-template-columns:6rem 1fr}}.filtered-list i{{display:none}}.status-list li{{grid-template-columns:1fr 3rem}}.status-list li span{{grid-column:1/-1;grid-row:2}}footer{{grid-template-columns:1fr;gap:.6rem}}}}
@media(prefers-reduced-motion:reduce){{html{{scroll-behavior:auto}}.reveal{{opacity:1;transition:none}}}}
</style>
</head>
<body>
<a class="skip" href="#content">SKIP TO CONTENT</a>
<header class="topbar">
  <a href="#top">SB/26</a>
  <nav aria-label="Report sections"><a href="#runners">RUNNERS</a><a href="#lore">LORE</a><a href="#kol">WALLETS</a><a href="#picks">THE READ</a><a href="#shortlist">NEW</a><a href="#movers">MOVERS</a><a href="#ctos">CTOS</a><a href="#onchain">ON-CHAIN</a><a href="#method">METHOD</a><a href="#scorecard">OUTCOMES</a></nav>
  <div class="run-control"><button id="run-brief" type="button">RUN BRIEF</button></div>
</header>
<main id="content">
  <section class="masthead" id="top">
    <div class="mast-title"><h1>24H//BRIEF</h1><p>Every Pump.fun create observed on-chain, the launches that established a tracked market, and only the strongest measurable names from the prior 24 hours.</p></div>
    <div class="mast-telemetry">
      <div class="telemetry-cell"><span>ROLLING WINDOW</span><output>{_e(window_text)}</output></div>
      <div class="telemetry-cell"><span>REPORT / LOCAL CLOCK</span><output>{_e(generated)}</output><time id="clock">--:--:--</time></div>
      <div class="telemetry-cell"><span>NEXT DAILY RUN / 06:45</span><output id="countdown">--H --M</output></div>
    </div>
  </section>
  <section class="score-strip" aria-label="What happened today">
    <div><span>RUNNERS TODAY</span><output>{len(brief.runners)}</output></div>
    <div><span>NEW / 24H</span><output>{sum(1 for c in brief.runners if c.signals.age_hours is not None and c.signals.age_hours <= 24)}</output></div>
    <div><span>5X OR BETTER</span><output>{sum(1 for c in brief.runners if c.run_multiple >= 5)}</output></div>
    <div><span>KOL FLAGGED</span><output>{len(brief.kol_flagged) if brief.kol_wallet_count else '--'}</output></div>
    <div><span>SHARED LORES</span><output>{len(brief.lore_groups)}</output></div>
    <div><span>WALLETS TRACKED</span><output>{brief.kol_wallet_count or '--'}</output></div>
  </section>
  <section id="picks" class="report-section reveal">
    <header class="section-header"><h2>The read</h2><output>{len(picks):02d}</output></header>
    <ul class="pick-list">{pick_rows}</ul>
  </section>
  <div class="utility"><label class="search">/ <input id="filter" type="search" placeholder="FILTER SYMBOL, MINT, EVENT OR REASON" autocomplete="off"></label><output class="result-count" id="result-count">ALL RECORDS</output></div>
  {_runner_section(brief)}
  {_lore_section(brief)}
  {_kol_section(brief)}
  {_candidate_section('shortlist', 'New / launched in this 24h window', shortlist)}
  {_candidate_section('movers', 'Movers / strongest names of the day, any age', brief.movers)}
  {_candidate_section('ctos', 'Community takeovers / claimed recently, still active', brief.ctos)}
  <section id="onchain" class="report-section reveal"><header class="section-header"><h2>On-chain changes / today's launches only</h2><output>{len(daily_onchain):02d}</output></header>{findings}</section>
  <section class="report-section reveal"><header class="section-header"><h2>Meta rotation</h2><output>{len(brief.metas):02d}</output></header><ul class="meta-list">{metas}</ul></section>
  <aside class="selection-method" id="method" aria-label="Daily selection definition">
    <h2>How today is selected</h2>
    <div><h3>Strongest means</h3><p>{_e(brief.strongest_definition)}</p></div>
    <div><h3>Interesting means</h3><p>{_e(brief.interesting_definition)}</p></div>
    <div class="selection-rule"><h3>The cut</h3><p>{_e(brief.selection_rule)}</p></div>
  </aside>
  <section id="screening" class="report-section reveal"><header class="section-header"><h2>Why the rest did not make the brief</h2><output>{len(brief.launches_last_24h) - len(shortlist):02d}</output></header><p class="coverage-note">{_e(brief.discovery_note)} Individual rejected names stay out of the client view.</p><ul class="filtered-list">{screening}</ul></section>
  <section id="scorecard" class="report-section reveal"><header class="section-header"><h2>30d outcome scorecard</h2><output>{sc.featured_count:02d}</output></header><div class="outcome-row"><div><span>FEATURED</span><output>{sc.featured_count}</output></div><div><span>72H Q1</span><output>{_e(pct(sc.featured_q1_72h))}</output></div><div><span>72H MEDIAN</span><output>{_e(pct(sc.featured_median_72h))}</output></div><div><span>72H Q3</span><output>{_e(pct(sc.featured_q3_72h))}</output></div><div><span>72H UP</span><output>{_e(pct(sc.featured_up_pct_72h, 0))}</output></div><div><span>LOSS &gt;90%</span><output>{_e(pct(sc.featured_crash_pct_72h, 0))}</output></div></div></section>
  <section class="report-section reveal"><header class="section-header"><h2>Data quality</h2><output>{len(brief.quality_alerts):02d}</output></header><ul class="quality-list">{quality}</ul></section>
  <section id="coverage" class="report-section reveal"><header class="section-header"><h2>Data coverage</h2><output>{len(brief.source_statuses):02d}</output></header><ul class="status-list">{statuses}</ul></section>
  <footer><span>Data, not advice. Everything here can go to zero.</span><span>LOCAL / READ-ONLY / SOLANA</span></footer>
</main>
<output id="run-message" role="status" aria-live="polite"></output>
<script>
const generatedAt={generated_json};
const clock=document.querySelector('#clock');
const countdown=document.querySelector('#countdown');
const runButton=document.querySelector('#run-brief');
const message=document.querySelector('#run-message');
const filter=document.querySelector('#filter');
const resultCount=document.querySelector('#result-count');
function tick(){{const now=new Date();clock.textContent=new Intl.DateTimeFormat([],{{hour:'2-digit',minute:'2-digit',second:'2-digit'}}).format(now);let next=new Date(now);next.setHours(6,45,0,0);if(next<=now)next.setDate(next.getDate()+1);const diff=next-now;countdown.textContent=`${{String(Math.floor(diff/36e5)).padStart(2,'0')}}H ${{String(Math.floor(diff%36e5/6e4)).padStart(2,'0')}}M`;}}
tick();setInterval(tick,1000);
const revealObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{{if(entry.isIntersecting)entry.target.classList.add('visible')}}),{{threshold:.04}});document.querySelectorAll('.reveal').forEach(item=>revealObserver.observe(item));
const navLinks=[...document.querySelectorAll('.topbar nav a')];const navObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{{if(entry.isIntersecting){{navLinks.forEach(link=>link.classList.toggle('active',link.hash===`#${{entry.target.id}}`));}}}}),{{rootMargin:'-20% 0px -70%'}});document.querySelectorAll('section[id]').forEach(section=>navObserver.observe(section));
filter.addEventListener('input',()=>{{const query=filter.value.trim().toLowerCase();let shown=0;document.querySelectorAll('.searchable').forEach(item=>{{const match=!query||(item.dataset.search||'').toLowerCase().includes(query);item.hidden=!match;if(match)shown++;}});resultCount.textContent=query?`${{shown}} MATCHES`:'ALL RECORDS';}});document.addEventListener('keydown',event=>{{if(event.key==='/'&&document.activeElement!==filter){{event.preventDefault();filter.focus();}}}});
function showMessage(text){{message.textContent=text;message.style.display='block';}}
async function state(){{try{{const response=await fetch('/api/state',{{cache:'no-store'}});if(!response.ok)throw new Error();const data=await response.json();if(data.running){{runButton.disabled=true;runButton.textContent='RUNNING';showMessage(data.message||'Brief run in progress.');}}else{{runButton.disabled=false;runButton.textContent='RUN BRIEF';if(data.completed_at&&new Date(data.completed_at)>new Date(generatedAt))location.reload();if(data.error)showMessage(`Run failed: ${{data.error}}`);}}}}catch{{runButton.disabled=true;runButton.textContent='STATIC VIEW';}}}}
runButton.addEventListener('click',async()=>{{runButton.disabled=true;runButton.textContent='STARTING';try{{const response=await fetch('/api/run',{{method:'POST'}});const data=await response.json();if(!response.ok)throw new Error(data.error||'Run could not start');showMessage('Brief run started. The report will refresh when complete.');}}catch(error){{showMessage(error.message);runButton.disabled=false;runButton.textContent='RUN BRIEF';}}}});
state();setInterval(state,4000);
</script>
</body></html>"""
