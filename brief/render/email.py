"""The daily report as an email.

Email clients are hostile to modern web design. Gmail strips many style blocks,
Outlook still prefers tables, web fonts are unreliable, and SVG does not belong
in a production inbox. This renderer is intentionally table-first and fully
inline-styled so the report reads like a polished morning memo everywhere.
"""
from __future__ import annotations

import html
from datetime import datetime

from brief.config import Settings
from brief.lifecycle import tier_for_peak
from brief.models import Brief, Candidate
from brief.render.formatting import money, pct


INK = "#111322"
INK_2 = "#262A3D"
MUTED = "#687085"
SOFT = "#8E96AF"
PAPER = "#EEF0F7"
SHEET = "#FBFBFE"
SURFACE = "#FFFFFF"
NIGHT = "#12152A"
BLUE = "#405CF5"
VIOLET = "#7B49F4"
GREEN = "#14B878"
RED = "#D84A3A"
AMBER = "#B77A22"
LINE = "#DFE3EF"
LINE_DARK = "#2A3154"
TRACK = "#E8EBF5"

FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',"
    "Helvetica,Arial,sans-serif"
)

CHAIN_NAMES = {
    "solana": "Solana",
    "ethereum": "Ethereum",
    "bsc": "BNB Chain",
    "base": "Base",
    "robinhood": "Robinhood",
}

UNKNOWN_MARKERS = ("unknown", "no contract safety source", "not published")


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _txt(
    size: int,
    weight: int,
    color: str,
    leading: float = 1.45,
    tracking: float = 0.0,
    transform: str = "",
) -> str:
    parts = [
        f"font-family:{FONT}",
        f"font-size:{size}px",
        f"font-weight:{weight}",
        f"line-height:{leading}",
        f"color:{color}",
        "margin:0",
    ]
    if tracking:
        parts.append(f"letter-spacing:{tracking}em")
    if transform:
        parts.append(f"text-transform:{transform}")
    return ";".join(parts)


def _chain_name(chain: str) -> str:
    return CHAIN_NAMES.get(str(chain or "").lower(), str(chain or "unknown"))


def email_subject(brief: Brief, settings: Settings) -> str:
    prefix = str(settings.get("delivery", "email_subject_prefix", "Fomo Onchain"))
    runners = _newsletter_recap_candidates(brief, settings)
    date = brief.generated_at.strftime("%d %b")
    if not runners:
        return f"{prefix} | quiet tape | {date}"
    top = max(runners, key=lambda c: c.run_multiple)
    return f"{prefix} | ${top.token.symbol} {top.run_multiple:.0f}x and {len(runners) - 1} more | {date}"


def _pulse_item(item: tuple) -> tuple[Candidate, int, str]:
    candidate, passes = item[:2]
    level = str(item[2]) if len(item) >= 3 else ""
    return candidate, int(passes), level


def pulse_email_subject(items: list[tuple], settings: Settings) -> str:
    """A compact, recognisable subject for a confirmed hourly runner alert."""
    prefix = str(settings.get("delivery", "email_subject_prefix", "Fomo Onchain"))
    first, _, level = _pulse_item(items[0])
    extra = f" +{len(items) - 1}" if len(items) > 1 else ""
    milestone = f" {level}" if level else ""
    return f"{prefix} runner alert | ${first.token.symbol}{milestone}{extra}"


def _age(candidate: Candidate) -> str:
    hours = candidate.signals.age_hours
    if hours is None:
        return "age unknown"
    if hours < 1:
        return f"{hours * 60:.0f} min old"
    if hours < 48:
        return f"{hours:.0f}h old"
    return f"{hours / 24:.1f}d old"


def _size(candidate: Candidate) -> str:
    run = float(candidate.peak_multiple or candidate.run_multiple or 1.0)
    if run >= 2:
        return f"{run:.1f}x"
    return pct(candidate.token.price_change_24h, 0)


def _candidate_tier(candidate: Candidate) -> str:
    if candidate.runner_tier:
        return candidate.runner_tier
    peak = max(
        float(candidate.peak_market_cap or 0),
        float(candidate.observed_peak_market_cap or 0),
        float(candidate.token.market_cap or 0),
    )
    return tier_for_peak(peak)


def _is_unknown(label: str) -> bool:
    return any(marker in label.lower() for marker in UNKNOWN_MARKERS)


def _split_labels(labels: list[str]) -> tuple[list[str], list[str]]:
    risks = [label for label in labels if not _is_unknown(label)]
    gaps = [label for label in labels if _is_unknown(label)]
    return risks, gaps


def _newsletter_recap_candidates(brief: Brief, settings: Settings) -> list[Candidate]:
    """Coins that deserve recap coverage, not only the clean public winners.

    `brief.runners` is intentionally strict. The client-facing newsletter needs
    the wider tape: coins that ran, why they mattered, and why some did not
    clear the organic-quality gate. This keeps the recap useful without turning
    questionable moves into endorsements.
    """
    limit = int(settings.get("delivery", "newsletter_observed_limit", 14) or 14)
    recap_rows = brief.recap.get("all", []) if brief.recap else []
    if recap_rows:
        # The machine recap deliberately retains every measured threshold
        # crossing, including contract-risk and wash-shaped moves. That is
        # useful audit tape, but it is not the public newsletter. Once the
        # engine has produced an approved runner set, never let a larger blocked
        # cap displace those names merely because the raw ledger sorts by peak.
        approved = [
            candidate
            for candidate in brief.runners
            if _candidate_tier(candidate) in {"S", "A", "B"}
        ]
        approved.sort(
            key=lambda candidate: float(
                candidate.peak_market_cap
                or candidate.observed_peak_market_cap
                or candidate.token.market_cap
            ),
            reverse=True,
        )
        return approved[:limit]
    min_score = float(settings.get("delivery", "newsletter_min_observed_runner_score", 25) or 25)
    max_manip = float(settings.get("delivery", "newsletter_max_observed_manipulation", 70) or 70)
    min_volume = float(settings.get("delivery", "newsletter_min_observed_volume", 250_000) or 250_000)
    min_mcap = float(settings.get("delivery", "newsletter_min_observed_market_cap", 250_000) or 250_000)
    excluded_risk_terms = [
        str(term).strip().lower()
        for term in settings.get("delivery", "newsletter_excluded_risk_terms", []) or []
        if str(term).strip()
    ]

    chosen: list[Candidate] = list(brief.runners)
    seen = {candidate.token.mint for candidate in chosen}
    observed: list[Candidate] = []
    for candidate in brief.blocked_runners:
        if candidate.token.mint in seen:
            continue
        if candidate.token.volume_24h < min_volume or candidate.token.market_cap < min_mcap:
            continue
        if candidate.scores.get("runner", 0.0) < min_score:
            continue
        if candidate.scores.get("manipulation", 100.0) > max_manip:
            continue
        labels = " | ".join(candidate.risk_labels).lower()
        if excluded_risk_terms and any(term in labels for term in excluded_risk_terms):
            continue
        observed.append(candidate)

    observed.sort(
        key=lambda candidate: (
            candidate.scores.get("runner", 0.0),
            candidate.run_multiple,
            candidate.token.volume_24h,
        ),
        reverse=True,
    )
    for candidate in observed:
        if len(chosen) >= limit:
            break
        chosen.append(candidate)
        seen.add(candidate.token.mint)

    chosen.sort(
        key=lambda candidate: (
            candidate.token.chain_id.lower() == "solana",
            candidate.scores.get("runner", 0.0),
            candidate.run_multiple,
            candidate.token.volume_24h,
        ),
        reverse=True,
    )
    return chosen


def _chip(text: str, background: str, color: str = SURFACE) -> str:
    return (
        f'<span style="display:inline-block;background:{background};'
        f'border-radius:999px;padding:7px 10px;{_txt(11, 700, color, 1.0, 0.03, "uppercase")}">'
        f"{_e(text)}</span>"
    )


def _mini_stat(label: str, value: str, color: str = INK) -> str:
    return (
        '<td width="25%" style="padding:0 7px 0 0;vertical-align:top">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SHEET};border:1px solid {LINE};border-radius:14px"><tr>'
        f'<td style="padding:13px 12px 12px">'
        f'<div style="{_txt(10, 750, MUTED, 1.2, 0.08, "uppercase")}">{_e(label)}</div>'
        f'<div style="{_txt(17, 800, color, 1.15)};padding-top:7px">{_e(value)}</div>'
        "</td></tr></table></td>"
    )


def _bar(share: float, color: str = BLUE, height: int = 8) -> str:
    filled = max(4, min(100, round(share * 100)))
    empty = 100 - filled
    empty_cell = (
        f'<td width="{empty}%" bgcolor="{TRACK}" style="background:{TRACK};'
        f'font-size:0;line-height:{height}px">&nbsp;</td>'
        if empty > 0
        else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:separate;table-layout:fixed;border-radius:999px;overflow:hidden">'
        f'<tr><td width="{filled}%" bgcolor="{color}" style="background:{color};'
        f'font-size:0;line-height:{height}px">&nbsp;</td>{empty_cell}</tr></table>'
    )


def _section(title: str, note: str = "") -> str:
    note_html = (
        f'<div style="{_txt(14, 450, MUTED, 1.55)};padding-top:7px">{_e(note)}</div>'
        if note
        else ""
    )
    return (
        '<tr><td style="padding:34px 30px 12px">'
        f'<div style="{_txt(19, 800, INK, 1.2, -0.02)}">{_e(title)}</div>'
        f"{note_html}</td></tr>"
    )


def _clean_lore(value: str) -> str:
    cleaned = " ".join(str(value or "").replace("-", " ").replace("_", " ").split())
    return cleaned.strip()


def _theme_title(lore: str, members: list[Candidate]) -> str:
    cleaned = _clean_lore(lore)
    haystack = " ".join([cleaned, *(c.token.symbol for c in members), *(c.token.name for c in members)]).lower()
    if any(word in haystack for word in ("privacy", "zec", "monero")):
        return "Privacy Meta"
    if any(word in haystack for word in ("dog", "cat", "frog", "horse", "animal", "ape", "penguin")):
        return "Animal Runners"
    if "ore" in haystack and any(c.token.symbol.lower() == "ore" for c in members):
        return "Ore Continues Outperforming"
    if cleaned:
        return f"{cleaned.title()} Meta"
    return "More Cooks"


def _descriptor(candidate: Candidate) -> str:
    token = candidate.token
    name = str(token.name or "").strip()
    symbol = str(token.symbol or "").strip("$").strip()
    lore = _clean_lore(candidate.lore)
    bits: list[str] = []
    if name and name.lower() != symbol.lower():
        bits.append(name)
    if lore and lore.lower() not in " ".join(bits).lower():
        bits.append(lore)
    if not bits:
        bits.append(_chain_name(token.chain_id).lower())
    return ", ".join(bits[:2]).lower()


def _compact_copy(value: object, limit: int = 190) -> str:
    text = " ".join(str(value or "").split()).strip(" .")
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (clipped or text[: limit - 1]).rstrip() + "…"


def _coin_thesis(candidate: Candidate) -> str:
    """Choose one sourced, coin-specific lead instead of metric boilerplate."""
    news = [
        _compact_copy(row.get("summary"))
        for row in candidate.news_evidence
        if _compact_copy(row.get("summary"))
    ]
    if news:
        return news[0]

    if candidate.x_interactions:
        interaction = max(
            candidate.x_interactions,
            key=lambda row: row.like_count + row.repost_count * 2 + row.reply_count + row.quote_count * 2,
        )
        summary = _compact_copy(interaction.summary, 155)
        if summary:
            return f"X thesis — @{interaction.author_handle}: {summary}"

    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    wallet_flow = gmgn.get("walletFlow", {}) or {}
    kol_count = max(
        len(set(candidate.kol_buyers)),
        int(wallet_flow.get("kolCount") or 0),
        int(gmgn.get("kolCount") or 0),
    )
    smart_count = max(
        int(wallet_flow.get("smartMoneyCount") or 0),
        int(gmgn.get("smartMoneyCount") or 0),
    )
    renowned_traders = int(gmgn.get("renownedTraderCount") or 0)
    profitable_traders = int(gmgn.get("renownedProfitableCount") or 0)
    holding_traders = int(gmgn.get("renownedHoldingCount") or 0)
    lore = _clean_lore(candidate.lore)
    descriptor = _descriptor(candidate)
    if renowned_traders >= 2:
        return (
            f"GMGN mapped {renowned_traders} renowned traders: "
            f"{profitable_traders} profitable and {holding_traders} still holding"
        )
    if gmgn.get("cto") and (kol_count or smart_count):
        return f"community takeover with {smart_count} smart-money and {kol_count} renowned/KOL wallets mapped by GMGN"
    if lore:
        return f"the {lore.lower()} narrative produced one of the window's strongest verified peaks"
    if descriptor not in {"solana", "base", "bnb chain", "ethereum", "unknown"}:
        return descriptor
    if kol_count >= 20 and smart_count:
        return f"an unusually KOL-heavy move with {kol_count} renowned and {smart_count} smart-money wallets on GMGN"
    if kol_count >= 2:
        outcome = "finished net profitable" if candidate.kol_realised_sol > 0.1 else "clustered around the same ticker"
        return f"KOL consensus trade: {kol_count} tracked or GMGN-tagged wallets {outcome}"
    if smart_count:
        return f"GMGN maps {smart_count} smart-money wallet{'s' if smart_count != 1 else ''} around the token"

    if candidate.drawdown_from_peak_pct is not None and candidate.drawdown_from_peak_pct >= 40:
        return f"an early runner that gave back {candidate.drawdown_from_peak_pct:.0f}% from its window high"
    if candidate.token.txns_24h.total >= 1_000:
        return f"a broad tape move with {candidate.token.txns_24h.total:,} trades in the window"
    return _descriptor(candidate)


def _reason_bits(candidate: Candidate) -> list[str]:
    bits: list[str] = []
    if candidate.drawdown_from_peak_pct is not None and candidate.drawdown_from_peak_pct >= 20:
        bits.append(
            f"Peaked earlier in the window and is now {candidate.drawdown_from_peak_pct:.0f}% off the high"
        )
    for evidence in candidate.news_evidence[:1]:
        summary = str(evidence.get("summary") or "").strip()
        if summary:
            bits.append(summary)
    if candidate.x_interactions:
        interaction = max(
            candidate.x_interactions,
            key=lambda row: row.like_count + row.repost_count * 2 + row.reply_count,
        )
        summary = " ".join(str(interaction.summary or "").split())
        if summary:
            bits.append(f"@{interaction.author_handle}: {summary[:190]}")
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    wallet_flow = gmgn.get("walletFlow", {}) or {}
    renowned = max(int(gmgn.get("kolCount") or 0), int(wallet_flow.get("kolCount") or 0))
    smart = max(int(gmgn.get("smartMoneyCount") or 0), int(wallet_flow.get("smartMoneyCount") or 0))
    structure: list[str] = []
    if smart:
        structure.append(f"{smart} smart-money")
    if renowned:
        structure.append(f"{renowned} renowned/KOL")
    if candidate.safety.top10_pct is not None:
        structure.append(f"top 10 at {candidate.safety.top10_pct:.0f}%")
    bundler = gmgn.get("bundlerRate")
    insider = gmgn.get("insiderRate")
    if bundler is not None:
        structure.append(f"bundlers {float(bundler):.1%}")
    if insider is not None:
        structure.append(f"insiders {float(insider):.1%}")
    if structure:
        bits.append("GMGN structure: " + ", ".join(structure[:4]))
    traders = list(gmgn.get("renownedTraders") or [])
    if traders:
        trusted = [row for row in traders if not row.get("suspicious")]
        leaders = sorted(
            trusted,
            key=lambda row: float(row.get("profitUsd") or 0),
            reverse=True,
        )[:2]
        names = ", ".join(
            f"{row.get('name') or str(row.get('address') or '')[:8]} {money(float(row.get('profitUsd') or 0))}"
            for row in leaders
        )
        if names:
            bits.append(f"GMGN KOL outcomes: {names}")
    # For a faded runner, its current-cap turnover sentences use the wrong
    # denominator. The verified peak recap is the useful first read.
    if candidate.observed_peak_market_cap and candidate.read:
        bits.append(candidate.read)
    elif candidate.dex_evidence:
        bits.extend(candidate.dex_evidence[:2])
    if candidate.read and candidate.read not in bits:
        bits.append(candidate.read)
    risks, gaps = _split_labels(candidate.risk_labels)
    if risks:
        bits.append("flag: " + risks[0])
    elif gaps:
        bits.append("gap: " + gaps[0])
    seen: set[str] = set()
    unique: list[str] = []
    for bit in bits:
        text = " ".join(str(bit).split())
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            unique.append(text)
    return unique[:3]


def _name_list(names: list[str], limit: int = 4) -> str:
    if not names:
        return ""
    shown = names[:limit]
    suffix = f" +{len(names) - limit}" if len(names) > limit else ""
    return ", ".join(shown) + suffix


def _kol_wallet_recap(candidate: Candidate) -> str:
    if candidate.kol_flows:
        buyers = [flow.name for flow in candidate.kol_flows if flow.bought]
        holders = [flow.name for flow in candidate.kol_flows if flow.holding]
        sellers = [flow.name for flow in candidate.kol_flows if flow.sold and not flow.holding]
        best = max(candidate.kol_flows, key=lambda flow: flow.realised_sol)
        bits: list[str] = []
        if buyers:
            bits.append(f"bought: {_name_list(buyers)}")
        if holders:
            bits.append(f"still holding: {_name_list(holders)}")
        if sellers:
            bits.append(f"closed/sold: {_name_list(sellers)}")
        closed_pnl = sum(
            flow.realised_sol for flow in candidate.kol_flows if flow.bought and flow.sold
        )
        if holders and abs(closed_pnl) >= 0.1:
            bits.append(f"closed trades {closed_pnl:+.1f} SOL; open positions excluded")
        elif abs(candidate.kol_realised_sol) >= 0.1:
            bits.append(f"realised {candidate.kol_realised_sol:+.1f} SOL")
        if best.realised_sol > 0.1:
            bits.append(f"best wallet {best.name} +{best.realised_sol:.1f} SOL")
        return "Tracked wallets — " + "; ".join(bits)
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    traders = list(gmgn.get("renownedTraders") or [])
    if traders:
        count = int(gmgn.get("renownedTraderCount") or len(traders))
        profitable = int(gmgn.get("renownedProfitableCount") or 0)
        holding = int(gmgn.get("renownedHoldingCount") or 0)
        realised = float(gmgn.get("renownedRealizedProfitUsd") or 0)
        trusted = [row for row in traders if not row.get("suspicious")]
        best = max(trusted, key=lambda row: float(row.get("profitUsd") or 0), default=None)
        bits = [f"{count} renowned traded", f"{profitable} profitable", f"{holding} holding"]
        if abs(realised) >= 1:
            bits.append(f"realised {money(realised)}")
        if best and float(best.get("profitUsd") or 0) > 0:
            bits.append(f"best {best.get('name') or str(best.get('address') or '')[:8]} {money(float(best.get('profitUsd') or 0))}")
        return "GMGN KOL tape — " + "; ".join(bits)
    if candidate.kol_wallets_scanned:
        return f"Tracked wallets — none of {candidate.kol_wallets_scanned} scanned wallets touched it"
    return ""


def _theme_groups(brief: Brief, runners: list[Candidate]) -> list[tuple[str, list[Candidate]]]:
    by_mint = {candidate.token.mint: candidate for candidate in runners}
    used: set[str] = set()
    groups: list[tuple[str, list[Candidate]]] = []

    lore_groups = getattr(brief, "lore_groups", {}) or {}
    for lore, members in sorted(lore_groups.items(), key=lambda item: (len(item[1]), max(c.run_multiple for c in item[1])), reverse=True):
        kept = [by_mint[c.token.mint] for c in members if c.token.mint in by_mint and c.token.mint not in used]
        if len(kept) < 2:
            continue
        kept.sort(key=lambda c: c.token.market_cap, reverse=True)
        groups.append((_theme_title(lore, kept), kept))
        used.update(c.token.mint for c in kept)

    remaining = [candidate for candidate in runners if candidate.token.mint not in used]
    if not groups and len(remaining) > 5:
        lead = remaining[:3]
        groups.append(("Top Cooks", lead))
        used.update(c.token.mint for c in lead)
        remaining = [candidate for candidate in runners if candidate.token.mint not in used]
    if remaining:
        groups.append(("More Cooks", remaining))
    return groups


def _lead_recap(candidate: Candidate) -> str:
    token = candidate.token
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    hit_mcap = max(
        token.market_cap,
        float(candidate.peak_market_cap or 0.0),
        float(candidate.observed_peak_market_cap or 0.0),
        float(gmgn.get("kline24hPeakMarketCap") or 0.0),
    )
    thesis = _coin_thesis(candidate)
    if candidate.news_evidence:
        headline = f"${token.symbol} led on a verified news catalyst"
    elif candidate.x_interactions:
        interaction = max(
            candidate.x_interactions,
            key=lambda row: row.like_count + row.repost_count * 2 + row.reply_count + row.quote_count * 2,
        )
        headline = f"${token.symbol} caught @{interaction.author_handle}'s attention"
    elif len(set(candidate.kol_buyers)) >= 2:
        headline = f"${token.symbol} became the KOL consensus trade"
    elif candidate.drawdown_from_peak_pct is not None and candidate.drawdown_from_peak_pct >= 40:
        headline = f"${token.symbol} ran early, then faded"
    else:
        headline = f"${token.symbol} led the day's tape"
    return (
        '<tr><td style="padding:28px 30px 0">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:24px;overflow:hidden"><tr>'
        f'<td style="padding:25px 25px 23px">'
        f'<div style="{_txt(13, 800, BLUE, 1.2, 0.10, "uppercase")}">Lead read</div>'
        f'<div style="{_txt(30, 850, INK, 1.12, -0.035)};padding-top:10px">'
        f'{_e(headline)}</div>'
        f'<div style="{_txt(16, 450, INK_2, 1.65)};padding-top:13px">'
        f'Hit {_e(money(hit_mcap))} after a {_e(_size(candidate))} move. {_e(thesis)}.</div>'
        f'<div style="{_txt(13, 500, MUTED, 1.55)};padding-top:12px">'
        f'{_e(money(token.volume_24h))} volume / {_e(money(token.liquidity_usd))} liquidity / {_e(_age(candidate))}</div>'
        f'<div style="padding-top:17px"><a href="{_e(token.url)}" target="_blank" '
        f'style="display:inline-block;background:{INK};color:{SURFACE};border-radius:999px;'
        f'padding:12px 18px;text-decoration:none;{_txt(13, 800, SURFACE, 1.0)}">Open chart</a></div>'
        '</td></tr></table></td></tr>'
    )


def _coin_recap_line(candidate: Candidate) -> str:
    token = candidate.token
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    hit_mcap = max(
        token.market_cap,
        float(candidate.peak_market_cap or 0.0),
        float(candidate.observed_peak_market_cap or 0.0),
        float(gmgn.get("kline24hPeakMarketCap") or 0.0),
    )
    thesis = _coin_thesis(candidate)
    thesis_key = thesis.casefold()
    reasons = [
        reason for reason in _reason_bits(candidate)
        if reason.casefold() not in thesis_key and thesis_key not in reason.casefold()
    ]
    kol_recap = _kol_wallet_recap(candidate)
    kol_html = (
        '<tr><td width="18" style="vertical-align:top;padding-top:7px">'
        f'<div style="width:5px;height:5px;border-radius:999px;background:{VIOLET};font-size:0;line-height:0">&nbsp;</div>'
        '</td>'
        f'<td style="{_txt(13, 650, VIOLET, 1.58)};padding-top:3px">{_e(kol_recap)}</td></tr>'
        if kol_recap
        else ""
    )
    # The email is a morning read, not the analyst console. One supporting
    # sentence plus the KOL outcome is enough; the linked app keeps every raw
    # metric and risk label for readers who want to drill down.
    supporting = reasons[:1]
    caveat = next((reason for reason in reasons if reason.lower().startswith(("flag:", "gap:"))), None)
    if caveat and caveat not in supporting:
        supporting.append(caveat)
    reason_html = "".join(
        '<tr><td width="18" style="vertical-align:top;padding-top:7px">'
        f'<div style="width:5px;height:5px;border-radius:999px;background:{BLUE};font-size:0;line-height:0">&nbsp;</div>'
        '</td>'
        f'<td style="{_txt(13, 450, MUTED, 1.58)};padding-top:3px">{_e(reason)}</td></tr>'
        for reason in supporting
    )
    wallet_flow = gmgn.get("walletFlow", {}) or {}
    gmgn_kols = max(int(wallet_flow.get("kolCount") or 0), int(gmgn.get("kolCount") or 0))
    gmgn_smart = max(
        int(wallet_flow.get("smartMoneyCount") or 0),
        int(gmgn.get("smartMoneyCount") or 0),
    )
    holders = int(candidate.safety.holder_count or gmgn.get("holders") or 0)
    move_to_high = float(gmgn.get("kline24hPeakFromOpenPct") or 0)
    move = (
        f"+{move_to_high:.0f}% to 24h high"
        if move_to_high >= 0
        else f"{move_to_high:.0f}% to 24h high"
    ) if gmgn.get("kline24hPeakFromOpenPct") is not None else f"{token.price_change_24h:+.0f}% in 24h"
    facts = [move, _age(candidate)]
    if max(len(candidate.kol_buyers), gmgn_kols):
        facts.append(f"{max(len(candidate.kol_buyers), gmgn_kols)} KOL")
    if gmgn_smart:
        facts.append(f"{gmgn_smart} smart money")
    if holders:
        facts.append(f"{holders:,} holders")
    details = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="padding-top:7px">'
        f"{kol_html}{reason_html}</table>"
        if kol_html or reason_html
        else ""
    )
    return (
        '<tr><td style="padding:0 0 15px">'
        f'<div style="{_txt(17, 750, INK, 1.35)}">'
        f'<a href="{_e(token.url)}" target="_blank" style="color:{INK};text-decoration:none">${_e(token.symbol)}</a>'
        f' <span style="color:{MUTED};font-weight:500">-&gt; hit</span> {_e(money(hit_mcap))}'
        f'<span style="color:{MUTED};font-weight:500">, {_e(thesis)}</span></div>'
        f'<div style="{_txt(12, 600, SOFT, 1.45)};padding-top:4px">'
        f'{_e(" / ".join(facts))}</div>'
        f"{details}"
        '</td></tr>'
    )


def _theme_section(title: str, members: list[Candidate]) -> str:
    rows = "".join(_coin_recap_line(candidate) for candidate in members)
    return (
        '<tr><td style="padding:0 30px 14px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:22px;overflow:hidden"><tr>'
        f'<td style="padding:22px 22px 7px">'
        f'<div style="{_txt(21, 850, INK, 1.2, -0.025)};padding-bottom:16px">{_e(title)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'
        '</td></tr></table></td></tr>'
    )


def _peak_tier_section(title: str, members: list[Candidate]) -> str:
    ordered = sorted(
        members,
        key=lambda candidate: float(
            candidate.peak_market_cap or candidate.observed_peak_market_cap or candidate.token.market_cap
        ),
        reverse=True,
    )
    return _theme_section(title, ordered)


def _masthead(brief: Brief, runners: list[Candidate]) -> str:
    when = brief.generated_at.strftime("%d %b %Y")
    window = f"{brief.generated_at.strftime('%H:%M')} {brief.generated_at.tzname() or ''}".strip()
    lead = (
        f"{len(runners)} coins crossed $250K. Peak first, then the context behind the move."
        if runners
        else "No runner cleared the desk today. That is still a useful morning read."
    )
    return (
        '<tr><td style="padding:0 14px 0">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{NIGHT};border-radius:26px;overflow:hidden">'
        '<tr><td style="padding:30px 30px 28px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:top">'
        f'<div style="{_txt(13, 800, "#BFC7FF", 1.2, 0.12, "uppercase")}">Daily Memecoin Recap</div>'
        f'<div style="{_txt(36, 850, SURFACE, 1.06, -0.035)};padding-top:12px">'
        'Fomo <span style="color:#8EA0FF">Onchain</span></div>'
        f'<div style="{_txt(11, 650, "#8F9BE4", 1.2, 0.08, "uppercase")};padding-top:7px">fomo onchain</div>'
        f'<div style="{_txt(16, 450, "#D7DCF8", 1.6)};padding-top:14px;max-width:520px">{_e(lead)}</div>'
        "</td>"
        '<td align="right" style="vertical-align:top;white-space:nowrap;padding-left:18px">'
        f'<div style="{_txt(13, 750, SURFACE, 1.2)}">{_e(when)}</div>'
        f'<div style="{_txt(12, 500, "#AAB3E8", 1.4)};padding-top:6px">{_e(window)}</div>'
        "</td></tr></table>"
        f'<div style="height:1px;background:{LINE_DARK};line-height:1px;font-size:0;margin-top:26px">&nbsp;</div>'
        f'<div style="{_txt(12, 650, "#AAB3E8", 1.55)};padding-top:18px">'
        "Simple read: what hit, where it peaked, what drove attention, and what changed after."
        "</div>"
        "</td></tr></table></td></tr>"
    )


def _stat_band(brief: Brief, runners: list[Candidate]) -> str:
    chains = {candidate.token.chain_id.lower() for candidate in runners}
    fresh = sum(1 for c in runners if c.signals.age_hours is not None and c.signals.age_hours <= 24)
    big = sum(1 for c in runners if c.run_multiple >= 5)
    cells = [
        ("Recap coins", str(len(runners))),
        ("Clean", str(len(brief.runners))),
        ("Fresh", str(fresh)),
        ("Did 5x+", str(big)),
    ]
    columns = "".join(_mini_stat(label, value, BLUE if label == "Did 5x+" else INK) for label, value in cells)
    chain_line = ", ".join(sorted(_chain_name(chain) for chain in chains)) if chains else "No active chains"
    return (
        '<tr><td style="padding:18px 23px 0 30px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f"{columns}</tr></table>"
        f'<div style="{_txt(12, 500, MUTED, 1.4)};padding:12px 7px 0 0">Chains covered: {_e(chain_line)}</div>'
        "</td></tr>"
    )


def _hero(candidate: Candidate) -> str:
    token = candidate.token
    hit_mcap = max(token.market_cap, float(candidate.observed_peak_market_cap or 0.0))
    risks, gaps = _split_labels(candidate.risk_labels)
    risk_text = " / ".join(risks[:3])
    gap_text = " / ".join(gaps[:2])
    kol_text = f"{len(candidate.kol_buyers)} tracked wallets" if candidate.kol_buyers else "No tracked wallet hit"
    last_hour_color = RED if token.price_change_1h < 0 else GREEN
    return (
        '<tr><td style="padding:0 30px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:24px;overflow:hidden">'
        '<tr><td style="padding:26px 26px 24px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:top;padding-right:18px">'
        f'{_chip(_chain_name(token.chain_id), BLUE)}'
        f'<div style="{_txt(38, 850, INK, 1.0, -0.035)};padding-top:18px">${_e(token.symbol)}</div>'
        f'<div style="{_txt(15, 500, MUTED, 1.45)};padding-top:7px">{_e(token.name)} / {_e(_age(candidate))}</div>'
        "</td>"
        '<td align="right" style="vertical-align:top;white-space:nowrap">'
        f'<div style="{_txt(46, 850, BLUE, 0.95, -0.045)}">{_e(_size(candidate))}</div>'
        f'<div style="{_txt(12, 750, MUTED, 1.2, 0.08, "uppercase")};padding-top:7px">top runner</div>'
        "</td></tr></table>"
        f'<div style="{_txt(17, 450, INK_2, 1.7)};padding-top:22px">{_e(candidate.read)}</div>'
        f'<div style="padding-top:22px">{_bar(1.0, BLUE, 10)}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="padding-top:20px"><tr>'
        f'{_mini_stat("Peak mcap", money(hit_mcap))}'
        f'{_mini_stat("Volume", money(token.volume_24h))}'
        f'{_mini_stat("Liquidity", money(token.liquidity_usd))}'
        f'{_mini_stat("1 hour", pct(token.price_change_1h, 0), last_hour_color)}'
        "</tr></table>"
        f'<div style="{_txt(13, 650, VIOLET, 1.5)};padding-top:18px">{_e(kol_text)}</div>'
        + (
            f'<div style="{_txt(13, 650, RED, 1.55)};padding-top:10px">{_e(risk_text)}</div>'
            if risk_text
            else ""
        )
        + (
            f'<div style="{_txt(12, 500, SOFT, 1.55)};padding-top:8px">{_e(gap_text)}</div>'
            if gap_text
            else ""
        )
        + f'<div style="padding-top:22px">'
        f'<a href="{_e(token.url)}" target="_blank" '
        f'style="display:inline-block;background:{INK};color:{SURFACE};border-radius:999px;'
        f'padding:13px 19px;text-decoration:none;{_txt(13, 800, SURFACE, 1.0)}">Open chart</a>'
        f'<span style="{_txt(11, 500, MUTED, 1.5)};padding-left:12px;word-break:break-all">{_e(token.mint)}</span>'
        "</div>"
        "</td></tr></table></td></tr>"
    )


def _runner_row(candidate: Candidate, share: float) -> str:
    token = candidate.token
    hit_mcap = max(token.market_cap, float(candidate.observed_peak_market_cap or 0.0))
    risks, gaps = _split_labels(candidate.risk_labels)
    accent = RED if risks else BLUE
    kol = f" / {len(candidate.kol_buyers)} KOL" if candidate.kol_buyers else ""
    reason = risks[0] if risks else gaps[0] if gaps else candidate.read
    reason_color = RED if risks else MUTED
    return (
        '<tr><td style="padding:0 30px 10px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:18px;overflow:hidden">'
        "<tr>"
        f'<td width="5" bgcolor="{accent}" style="background:{accent};font-size:0;line-height:0">&nbsp;</td>'
        '<td style="padding:18px 18px 17px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:top;padding-right:12px">'
        f'<div style="{_txt(20, 850, INK, 1.12, -0.02)}">${_e(token.symbol)}</div>'
        f'<div style="{_txt(12, 550, MUTED, 1.5)};padding-top:5px">'
        f'{_e(_chain_name(token.chain_id))} / {_e(_age(candidate))} / hit {_e(money(hit_mcap))}{_e(kol)}</div>'
        "</td>"
        '<td align="right" style="vertical-align:top;white-space:nowrap">'
        f'<div style="{_txt(26, 850, INK, 1.0, -0.03)}">{_e(_size(candidate))}</div>'
        "</td></tr></table>"
        f'<div style="padding-top:14px">{_bar(share, accent if risks else BLUE, 7)}</div>'
        f'<div style="{_txt(13, 500, reason_color, 1.6)};padding-top:12px">{_e(reason)}</div>'
        f'<div style="padding-top:13px"><a href="{_e(token.url)}" target="_blank" '
        f'style="text-decoration:none;{_txt(12, 800, BLUE, 1.2, 0.06, "uppercase")}">Open chart</a></div>'
        "</td></tr></table></td></tr>"
    )


def _blocked_row(candidate: Candidate) -> str:
    reason = candidate.risk_labels[0] if candidate.risk_labels else "did not qualify"
    return (
        '<tr><td style="padding:0 30px 8px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SHEET};border:1px solid {LINE};border-radius:14px"><tr>'
        f'<td style="padding:14px 16px;vertical-align:top">'
        f'<span style="{_txt(14, 800, INK, 1.3)}">${_e(candidate.token.symbol)}</span>'
        f'<span style="{_txt(12, 500, MUTED, 1.3)}"> / {_e(_chain_name(candidate.token.chain_id))}</span>'
        f'<div style="{_txt(12, 500, MUTED, 1.55)};padding-top:5px">{_e(reason)}</div>'
        "</td>"
        f'<td align="right" style="padding:14px 16px;vertical-align:top;white-space:nowrap">'
        f'<span style="{_txt(14, 800, AMBER, 1.3)}">{_e(_size(candidate))}</span></td>'
        "</tr></table></td></tr>"
    )


def _pick_row(candidate: Candidate) -> str:
    return (
        '<tr><td style="padding:0 30px 10px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:18px"><tr>'
        f'<td style="padding:18px 18px 17px">'
        f'{_chip(candidate.track.upper(), NIGHT, SURFACE)}'
        f'<span style="{_txt(18, 850, INK, 1.25)};padding-left:9px">${_e(candidate.token.symbol)}</span>'
        f'<div style="{_txt(14, 450, INK_2, 1.7)};padding-top:12px">{_e(candidate.read)}</div>'
        f'<div style="padding-top:14px"><a href="{_e(candidate.token.url)}" target="_blank" '
        f'style="text-decoration:none;{_txt(12, 800, BLUE, 1.2, 0.06, "uppercase")}">Open chart</a></div>'
        "</td></tr></table></td></tr>"
    )


def _empty_state() -> str:
    return (
        '<tr><td style="padding:0 30px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:20px"><tr>'
        f'<td style="padding:28px 24px;{_txt(15, 450, MUTED, 1.7)}">'
        "Nothing cleared the floors today. An empty report is a result, not an outage."
        "</td></tr></table></td></tr>"
    )


def _footer(report_url: str) -> str:
    link = (
        f'<div style="padding-top:18px"><a href="{_e(report_url)}" target="_blank" '
        f'style="display:inline-block;background:{BLUE};color:{SURFACE};border-radius:999px;'
        f'padding:12px 18px;text-decoration:none;{_txt(13, 800, SURFACE, 1.0)}">Open full dashboard</a></div>'
        if report_url
        else ""
    )
    return (
        '<tr><td style="padding:30px 30px 0">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{NIGHT};border-radius:22px"><tr>'
        f'<td style="padding:24px 24px 25px">'
        f'<div style="{_txt(16, 850, SURFACE, 1.25)}">Read this like tape, not a recommendation.</div>'
        f'<div style="{_txt(13, 450, "#C8CFF3", 1.7)};padding-top:10px">'
        "A coin appears because it moved through the filters. It can still rug, fade, or go to zero."
        "</div>"
        f"{link}</td></tr></table></td></tr>"
    )


def _pulse_masthead(
    items: list[tuple[Candidate, int]],
    generated_at: datetime,
    window_hours: float,
) -> str:
    when = generated_at.strftime("%d %b %Y")
    clock = f"{generated_at.strftime('%H:%M')} {generated_at.tzname() or ''}".strip()
    noun = "runner" if len(items) == 1 else "runners"
    return (
        '<tr><td style="padding:0 14px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{NIGHT};border-radius:26px;overflow:hidden"><tr>'
        '<td style="padding:30px 30px 28px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:top">'
        f'<div style="{_txt(13, 800, "#BFC7FF", 1.2, 0.12, "uppercase")}">Confirmed runner alert</div>'
        f'<div style="{_txt(36, 850, SURFACE, 1.06, -0.035)};padding-top:12px">'
        'Fomo <span style="color:#8EA0FF">Onchain</span></div>'
        f'<div style="{_txt(16, 450, "#D7DCF8", 1.6)};padding-top:14px">'
        f'{len(items)} new qualifying {noun} appeared. No email is sent on an empty hour.</div>'
        '</td><td align="right" style="vertical-align:top;white-space:nowrap;padding-left:18px">'
        f'<div style="{_txt(13, 750, SURFACE, 1.2)}">{_e(when)}</div>'
        f'<div style="{_txt(12, 500, "#AAB3E8", 1.4)};padding-top:6px">{_e(clock)}</div>'
        '</td></tr></table>'
        f'<div style="height:1px;background:{LINE_DARK};line-height:1px;font-size:0;margin-top:26px">&nbsp;</div>'
        f'<div style="{_txt(12, 650, "#AAB3E8", 1.55)};padding-top:18px">'
        f'Rolling memory: {_e(f"{window_hours:g}")} hours. Duplicate mints are skipped after their first alert.'
        '</div></td></tr></table></td></tr>'
    )


def _pulse_runner(candidate: Candidate, pass_count: int, alert_level: str = "") -> str:
    token = candidate.token
    kol = f"{len(candidate.kol_buyers)} tracked wallets" if candidate.kol_buyers else "hourly market confirmation"
    pass_label = "scan" if pass_count == 1 else "scans"
    return (
        '<tr><td style="padding:18px 30px 0">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:22px;overflow:hidden"><tr>'
        f'<td width="6" bgcolor="{BLUE}" style="background:{BLUE};font-size:0;line-height:0">&nbsp;</td>'
        '<td style="padding:23px 23px 22px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:top;padding-right:16px">'
        f'<div style="{_txt(29, 850, INK, 1.05, -0.03)}">${_e(token.symbol)}</div>'
        f'<div style="{_txt(13, 550, MUTED, 1.5)};padding-top:7px">'
        f'{_e(token.name)} / {_e(_age(candidate))} / {_e(kol)}</div></td>'
        '<td align="right" style="vertical-align:top;white-space:nowrap">'
        f'<div style="{_txt(29, 850, BLUE, 1.0, -0.03)}">{pass_count} {pass_label}</div>'
        f'<div style="{_txt(11, 750, MUTED, 1.2, 0.08, "uppercase")};padding-top:7px">'
        f'{_e(alert_level + " milestone" if alert_level else "new runner")}</div>'
        '</td></tr></table>'
        f'<div style="{_txt(15, 450, INK_2, 1.65)};padding-top:19px">{_e(candidate.read)}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="padding-top:20px"><tr>'
        f'{_mini_stat("Market cap", money(token.market_cap))}'
        f'{_mini_stat("24h volume", money(token.volume_24h))}'
        f'{_mini_stat("Liquidity", money(token.liquidity_usd))}'
        f'{_mini_stat("Run", f"{candidate.run_multiple:.1f}x", BLUE)}'
        '</tr></table>'
        f'<div style="padding-top:20px"><a href="{_e(token.url)}" target="_blank" '
        f'style="display:inline-block;background:{INK};color:{SURFACE};border-radius:999px;'
        f'padding:13px 19px;text-decoration:none;{_txt(13, 800, SURFACE, 1.0)}">Open chart</a>'
        f'<div style="{_txt(11, 500, MUTED, 1.55)};padding-top:12px;word-break:break-all">CA: {_e(token.mint)}</div>'
        '</div></td></tr></table></td></tr>'
    )


def render_pulse_email(
    items: list[tuple],
    settings: Settings,
    generated_at: datetime,
) -> str:
    """Render only confirmed runners; callers must skip delivery when empty."""
    if not items:
        raise ValueError("pulse email requires at least one confirmed runner")
    report_url = str(settings.get("delivery", "report_url", "") or "")
    window_hours = float(settings.get("pulse", "window_hours", 24.0))
    rows = [_pulse_masthead(items, generated_at, window_hours)]
    unpacked = [_pulse_item(item) for item in items]
    rows.extend(_pulse_runner(candidate, passes, level) for candidate, passes, level in unpacked)
    rows.append(_footer(report_url))
    preheader = ", ".join(
        f"${candidate.token.symbol} crossed {level}" if level else f"${candidate.token.symbol} appeared as a new runner"
        for candidate, _, level in unpacked
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<title>Fomo Onchain runner alert</title></head>'
        f'<body style="margin:0;padding:0;background:{PAPER};-webkit-font-smoothing:antialiased">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{_e(preheader)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAPER}">'
        '<tr><td align="center" style="padding:22px 0 42px">'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:640px;max-width:640px;background:{PAPER}">{"".join(rows)}'
        '</table></td></tr></table></body></html>'
    )


def render_email(brief: Brief, settings: Settings) -> str:
    report_url = str(settings.get("delivery", "report_url", "") or "")
    runners = _newsletter_recap_candidates(brief, settings)
    rows: list[str] = []

    rows.append(_masthead(brief, runners))
    rows.append(_stat_band(brief, runners))

    if runners:
        rows.append(_lead_recap(runners[0]))
        rows.append(_section(
            "Runners of the day",
            "Ranked by the highest market cap reached in the last 24 hours. Faded moves remain in the record; material risks are written beneath them.",
        ))
        bands = (
            ("S", "$1M+ runners"),
            ("A", "$500K–$1M runners"),
            ("B", "$250K–$500K runners"),
        )
        # ASCII punctuation renders consistently in Gmail and Outlook.
        bands = (
            ("S", "$1M+ runners"),
            ("A", "$500K-$1M runners"),
            ("B", "$250K-$500K runners"),
        )
        for tier, title in bands:
            members = [candidate for candidate in runners if _candidate_tier(candidate) == tier]
            if members:
                rows.append(_peak_tier_section(title, members))
    else:
        rows.append(_section("Runners of the day"))
        rows.append(_empty_state())

    rows.append(_footer(report_url))

    when = brief.generated_at.strftime("%d %b %Y")
    preheader = (
        f"{len(runners)} runners, {sum(1 for c in runners if c.run_multiple >= 5)} did 5x+"
        if runners
        else "Nothing ran today that cleared the floors"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        f"<title>Fomo Onchain | {_e(when)}</title></head>"
        f'<body style="margin:0;padding:0;background:{PAPER};-webkit-font-smoothing:antialiased">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{_e(preheader)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAPER}">'
        '<tr><td align="center" style="padding:22px 0 42px">'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:640px;max-width:640px;background:{PAPER}">'
        f'{"".join(rows)}'
        "</table></td></tr></table></body></html>"
    )
