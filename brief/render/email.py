"""The daily report as an email.

Email clients are hostile to modern web design. Gmail strips many style blocks,
Outlook still prefers tables, web fonts are unreliable, and SVG does not belong
in a production inbox. This renderer is intentionally table-first and fully
inline-styled so the report reads like a polished morning memo everywhere.
"""
from __future__ import annotations

import html
import re
from datetime import datetime

from brief.config import Settings
from brief.links import fomo_token_url
from brief.lifecycle import tier_for_peak
from brief.journal import kol_trade_count
from brief.sources.openintel import (
    CAUSAL_POST,
    HYPE,
    PROMO,
    SUBSTANCE,
    WALLET_TRACKER,
)

# Whether a post from outside the trusted list may ever be quoted.
TRUSTED_ONLY = True
from brief.models import Brief, Candidate
from brief.newsletter import newsletter_coin_limit
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

FONT = "-apple-system,'Helvetica Neue','Segoe UI',Arial,sans-serif"

CHAIN_NAMES = {
    "solana": "Solana",
    "ethereum": "Ethereum",
    "bsc": "BNB Chain",
    "base": "Base",
    "robinhood": "Robinhood",
}

UNKNOWN_MARKERS = ("unknown", "unavailable", "no contract safety source", "not published")


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
    ]
    if tracking:
        parts.append(f"letter-spacing:{tracking}em")
    if transform:
        parts.append(f"text-transform:{transform}")
    return ";".join(parts)


def _chain_name(chain: str) -> str:
    return CHAIN_NAMES.get(str(chain or "").lower(), str(chain or "unknown"))


def _chart_url(candidate: Candidate) -> str:
    """Primary token action: open the asset on Fomo Family."""
    return fomo_token_url(candidate.token.chain_id, candidate.token.mint)


def _provider_neutral_email(body: str) -> str:
    """The product is Fomo Onchain; upstream vendors stay behind the curtain."""
    body = re.sub(r"(?i)\bGMGN-tagged\b", "tracked", body)
    body = re.sub(r"(?i)\bGMGN\b", "on-chain", body)
    return body


def _intraday_peak_pct(candidate: Candidate) -> float:
    evidence = candidate.provider_evidence.get("gmgn", {}) or {}
    value = evidence.get("kline24hPeakFromOpenPct")
    if value is not None:
        return float(value)
    return float(candidate.token.price_change_24h or 0.0)


def _intraday_multiple(candidate: Candidate) -> float:
    return max(0.0, 1.0 + _intraday_peak_pct(candidate) / 100.0)


def _window_open_market_cap(candidate: Candidate) -> float:
    evidence = candidate.provider_evidence.get("gmgn", {}) or {}
    peak_cap = float(evidence.get("kline24hPeakMarketCap") or 0.0)
    open_price = float(evidence.get("kline24hOpenPrice") or 0.0)
    high_price = float(evidence.get("kline24hHighPrice") or 0.0)
    if peak_cap > 0 and open_price > 0 and high_price > 0:
        return peak_cap * open_price / high_price
    return 0.0


def _window_path(candidate: Candidate) -> str:
    """Explain why an old or faded coin belongs in today's tape."""
    evidence = candidate.provider_evidence.get("gmgn", {}) or {}
    start = _window_open_market_cap(candidate)
    peak = max(
        float(candidate.token.market_cap or 0.0),
        float(candidate.peak_market_cap or 0.0),
        float(candidate.observed_peak_market_cap or 0.0),
        float(evidence.get("kline24hPeakMarketCap") or 0.0),
    )
    current = float(candidate.token.market_cap or 0.0)
    if start <= 0 or peak <= start:
        return ""
    path = f"24h path: {money(start)} to {money(peak)} at the high"
    drawdown = 100.0 * (1.0 - current / peak) if peak > 0 else 0.0
    if current > 0 and drawdown >= 10:
        path += f", then {money(current)} by the cutoff"
    close_change = evidence.get("kline24hChangePct")
    if close_change is not None and drawdown >= 25:
        path += f"; the move faded and finished {float(close_change):+.0f}%"
    return path


def email_subject(brief: Brief, settings: Settings) -> str:
    prefix = str(settings.get("delivery", "email_subject_prefix", "Fomo Onchain"))
    runners = _newsletter_recap_candidates(brief, settings)
    date = brief.generated_at.strftime("%d %b")
    if not runners:
        return f"{prefix} | quiet tape | {date}"
    top = max(runners, key=peak_cap)
    return f"{prefix} | ${top.token.symbol} hit {money(peak_cap(top))} and {len(runners) - 1} more | {date}"


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


def peak_cap(candidate: Candidate) -> float:
    """The highest market cap the coin is known to have reached today."""
    return max(
        float(candidate.peak_market_cap or 0),
        float(candidate.observed_peak_market_cap or 0),
        float(candidate.token.market_cap or 0),
    )


def _size(candidate: Candidate) -> str:
    return money(peak_cap(candidate))


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
    # The writer and renderer must use one cap. Separate 30/25 settings caused
    # five written rows to render without their peak, mint or trade link.
    limit = newsletter_coin_limit(settings)
    recap_rows = brief.recap.get("all", []) if brief.recap else []
    if recap_rows:
        # The machine recap deliberately retains every measured threshold
        # crossing, including contract-risk and wash-shaped moves. That is
        # useful audit tape, but it is not the public newsletter. Once the
        # engine has produced an approved runner set, never let a larger blocked
        # cap displace those names merely because the raw ledger sorts by peak.
        approved = []
        seen_identities: set[tuple[str, str]] = set()
        for candidate in brief.runners:
            if _candidate_tier(candidate) not in {"S", "A", "B"}:
                continue
            identity = (
                candidate.token.chain_id.strip().lower(),
                candidate.token.mint.strip().lower(),
            )
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
            approved.append(candidate)
        approved.sort(
            key=lambda candidate: float(
                candidate.peak_market_cap
                or candidate.observed_peak_market_cap
                or candidate.token.market_cap
            ),
            reverse=True,
        )
        return approved[:limit] if limit > 0 else approved
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
            peak_cap(candidate),
            candidate.token.volume_24h,
        ),
        reverse=True,
    )
    for candidate in observed:
        if limit > 0 and len(chosen) >= limit:
            break
        chosen.append(candidate)
        seen.add(candidate.token.mint)

    chosen.sort(
        key=lambda candidate: (
            candidate.token.chain_id.lower() == "solana",
            candidate.scores.get("runner", 0.0),
            peak_cap(candidate),
            candidate.token.volume_24h,
        ),
        reverse=True,
    )
    return chosen


class NewsletterIntegrityError(ValueError):
    """The email would publish incomplete or mismatched token identities."""


def _candidate_maps(candidates: list[Candidate]) -> tuple[dict[str, Candidate], dict[str, list[Candidate]]]:
    by_mint = {candidate.token.mint: candidate for candidate in candidates}
    by_symbol: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_symbol.setdefault(candidate.token.symbol.upper(), []).append(candidate)
    return by_mint, by_symbol


def _resolve_narrative_candidate(
    item: dict,
    by_mint: dict[str, Candidate],
    by_symbol: dict[str, list[Candidate]],
) -> Candidate | None:
    mint = str(item.get("mint") or "").strip()
    if mint:
        return by_mint.get(mint)
    matches = by_symbol.get(str(item.get("symbol") or "").strip().upper(), [])
    return matches[0] if len(matches) == 1 else None


def _validate_daily_email(brief: Brief, candidates: list[Candidate]) -> None:
    """Fail closed before delivery when the recap and market rows disagree."""
    approved_mints = {candidate.token.mint for candidate in brief.runners}
    blocked_mints = {candidate.token.mint for candidate in brief.blocked_runners}
    structured_recap_exists = bool((brief.recap or {}).get("all"))
    seen: set[str] = set()
    for candidate in candidates:
        token = candidate.token
        if not token.mint or not token.chain_id or peak_cap(candidate) <= 0:
            raise NewsletterIntegrityError(
                f"incomplete newsletter candidate: {token.symbol or token.mint or 'unknown'}"
            )
        # Production briefs have a structured audit recap and therefore a
        # final approved set: nothing outside it may publish. Older/offline
        # briefs without that stage may still use the explicit soft-caveat
        # fallback selected by `_newsletter_recap_candidates`.
        if token.mint not in approved_mints and (
            structured_recap_exists or token.mint not in blocked_mints
        ):
            raise NewsletterIntegrityError(f"blocked/unapproved mint in newsletter: {token.mint}")
        if token.mint in seen:
            raise NewsletterIntegrityError(f"duplicate mint in newsletter: {token.mint}")
        seen.add(token.mint)

    narrative = brief.narrative or {}
    if not narrative.get("sections"):
        return
    by_mint, by_symbol = _candidate_maps(candidates)
    narrative_mints: list[str] = []
    for section in narrative.get("sections") or []:
        for item in section.get("coins") or []:
            candidate = _resolve_narrative_candidate(item, by_mint, by_symbol)
            if candidate is None:
                identity = item.get("mint") or item.get("symbol") or "unknown"
                raise NewsletterIntegrityError(f"unresolved narrative coin: {identity}")
            narrative_mints.append(candidate.token.mint)
    if len(narrative_mints) != len(set(narrative_mints)):
        raise NewsletterIntegrityError("the written recap contains a duplicate coin")
    if set(narrative_mints) != set(by_mint):
        missing = set(by_mint) - set(narrative_mints)
        extra = set(narrative_mints) - set(by_mint)
        raise NewsletterIntegrityError(
            f"written/rendered coin mismatch: missing={len(missing)} extra={len(extra)}"
        )


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
    name_core = re.sub(r"(?i)\b(coin|token)\b", "", name).strip(" -_()")
    if name and name_core.lower() != symbol.lower():
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
    if gmgn.get("cto") and (kol_count or smart_count):
        return f"community takeover with {smart_count} smart-money and {kol_count} KOL wallets involved"
    if lore:
        return f"the {lore.lower()} narrative produced one of the window's strongest verified peaks"
    if descriptor not in {"solana", "base", "bnb chain", "ethereum", "unknown"}:
        return descriptor
    if renowned_traders >= 2:
        return (
            f"at least {renowned_traders} tracked KOL traders appeared: "
            f"{profitable_traders} profitable and {holding_traders} still holding"
        )
    if kol_count >= 20 and smart_count:
        return f"an unusually KOL-heavy move with {kol_count} KOL and {smart_count} smart-money wallets involved"
    if kol_count >= 2:
        outcome = "finished net profitable" if candidate.kol_realised_sol > 0.1 else "clustered around the same ticker"
        return f"KOL consensus trade: {kol_count} tracked wallets {outcome}"
    if smart_count:
        return f"{smart_count} smart-money wallet{'s' if smart_count != 1 else ''} appeared around the token"

    if candidate.drawdown_from_peak_pct is not None and candidate.drawdown_from_peak_pct >= 40:
        return f"an early runner that gave back {candidate.drawdown_from_peak_pct:.0f}% from its window high"
    if candidate.token.txns_24h.total >= 1_000:
        return f"a broad tape move with {candidate.token.txns_24h.total:,} trades in the window"
    return _descriptor(candidate)


def _reason_bits(candidate: Candidate) -> list[str]:
    bits: list[str] = []
    window_path = _window_path(candidate)
    if window_path:
        bits.append(window_path)
    elif candidate.drawdown_from_peak_pct is not None and candidate.drawdown_from_peak_pct >= 20:
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
        structure.append(f"{renowned} KOL")
    if candidate.safety.top10_pct is not None:
        structure.append(f"top 10 at {candidate.safety.top10_pct:.0f}%")
    bundler = gmgn.get("bundlerRate")
    insider = gmgn.get("insiderRate")
    if bundler is not None:
        structure.append(f"bundlers {float(bundler):.1%}")
    if insider is not None:
        structure.append(f"insiders {float(insider):.1%}")
    if structure:
        bits.append("Wallet structure: " + ", ".join(structure[:4]))
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
            bits.append(f"Top KOL outcomes: {names}")
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
        bits = [f"{count} KOL wallets traded", f"{profitable} profitable", f"{holding} still holding"]
        if abs(realised) >= 1:
            bits.append(f"realised {money(realised)}")
        if best and float(best.get("profitUsd") or 0) > 0:
            bits.append(f"best {best.get('name') or str(best.get('address') or '')[:8]} {money(float(best.get('profitUsd') or 0))}")
        return "KOL tape - " + "; ".join(bits)
    if candidate.kol_wallets_scanned:
        return f"Tracked wallets — none of {candidate.kol_wallets_scanned} scanned wallets touched it"
    return ""


def _theme_groups(brief: Brief, runners: list[Candidate]) -> list[tuple[str, list[Candidate]]]:
    by_mint = {candidate.token.mint: candidate for candidate in runners}
    used: set[str] = set()
    groups: list[tuple[str, list[Candidate]]] = []

    lore_groups = getattr(brief, "lore_groups", {}) or {}
    for lore, members in sorted(lore_groups.items(), key=lambda item: (len(item[1]), max(peak_cap(c) for c in item[1])), reverse=True):
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
    path = _window_path(candidate)
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
        f'Hit {_e(money(hit_mcap))}. {_e(thesis)}.</div>'
        + (
            f'<div style="{_txt(13, 600, MUTED, 1.55)};padding-top:10px">{_e(path)}</div>'
            if path
            else ""
        )
        + f'<div style="{_txt(13, 500, MUTED, 1.55)};padding-top:12px">'
        f'{_e(money(token.volume_24h))} volume / {_e(money(token.liquidity_usd))} liquidity / {_e(_age(candidate))}</div>'
        f'<div style="padding-top:17px"><a href="{_e(_chart_url(candidate))}" target="_blank" '
        f'style="display:inline-block;background:{INK};color:{SURFACE};border-radius:999px;'
        f'padding:12px 18px;text-decoration:none;{_txt(13, 800, SURFACE, 1.0)}">Trade on Fomo</a></div>'
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
    close_change = gmgn.get("kline24hChangePct")
    if close_change is not None and abs(float(close_change) - move_to_high) >= 20:
        facts.insert(1, f"finished {float(close_change):+.0f}%")
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
        f'<a href="{_e(_chart_url(candidate))}" target="_blank" style="color:{INK};text-decoration:none">${_e(token.symbol)}</a>'
        f' <span style="color:{MUTED};font-weight:500">-&gt; hit</span> {_e(money(hit_mcap))}'
        f'<span style="color:{MUTED};font-weight:500">, {_e(thesis)}</span></div>'
        f'<div style="{_txt(12, 600, SOFT, 1.45)};padding-top:4px">'
        f'{_e(" / ".join(facts))}</div>'
        f"{details}"
        '</td></tr>'
    )


def _move_line(candidate: Candidate) -> str:
    """How far it ran to the high, and where it finished.

    Kept from the old row because it is genuinely the second question after
    the peak. It failed there by sharing a line with six other statistics.
    """
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    to_high = gmgn.get("kline24hPeakFromOpenPct")
    if to_high is None:
        change = float(candidate.token.price_change_24h or 0)
        return f"{change:+.0f}% in 24h" if change else ""
    parts = [f"{float(to_high):+.0f}% to the high"]
    close = gmgn.get("kline24hChangePct")
    if close is not None and abs(float(close) - float(to_high)) >= 20:
        parts.append(f"finished {float(close):+.0f}%")
    return ", ".join(parts)


def _wallet_line(candidate: Candidate) -> str:
    """Tracked wallets and smart money, when either is worth reporting."""
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    flow = gmgn.get("walletFlow", {}) or {}
    kols = max(int(flow.get("kolCount") or 0), int(gmgn.get("kolCount") or 0),
               len(candidate.kol_buyers or []))
    smart = max(int(flow.get("smartMoneyCount") or 0), int(gmgn.get("smartMoneyCount") or 0))
    parts = []
    if kols:
        parts.append(f"{kols} tracked wallets")
    if smart:
        parts.append(f"{smart} smart money")
    return " &middot; ".join(parts)


# What a coin *does*, which is lore even when nothing happened today.
DESCRIBES = re.compile(
    r"\b(lets|allows|enables|you can|users can|holders can|works by"
    r"|built on|based on|named after|inspired by|refers to|stands for"
    r"|means|tied to|linked to|comes from|created by|founded by|run by"
    r"|backed by|trade the|distributes|rewards holders)\b",
    re.I,
)
# Somebody talking about their own position, which is not lore.
OWN_TRADE = re.compile(
    r"\b(after doing|i (bought|sold|aped|entered|exited)"
    r"|my (bag|entry|position)|healthy correction|waiting for it"
    r"|let.s get it|shakeout|breakout is|we.re so back)\b"
    r"|\b\d+\s*[xX]\s+(with|on)\b",
    re.I,
)

def _usable_lore(text: str) -> str:
    """A sentence about the coin, or an empty string.

    Searched text arrives as whatever the page held: a contract address, a
    list of sibling tokens, a promoter pasting an address and a rocket. It was
    cleaned for quotes and left raw here, so exactly that reached print. The
    rule is the same in both places, and an empty row beats a dumped one.
    """
    cleaned = _clean_quote(text)
    if len(cleaned) < 30:
        return ""
    words = [w for w in cleaned.split() if w]
    if not words:
        return ""
    # A sentence has small words in it. A scraped panel is labels and figures.
    numeric = sum(1 for w in words if any(ch.isdigit() for ch in w))
    if numeric / len(words) > 0.3:
        return ""
    # Grammar is not information. "The shakeout has shaken and the breakout
    # is breakin" is a well-formed sentence that tells a reader nothing, so
    # the text has to report something or say what the coin is.
    if (HYPE.search(cleaned) or PROMO.search(cleaned)
            or WALLET_TRACKER.search(cleaned) or OWN_TRADE.search(cleaned)):
        return ""
    if not (CAUSAL_POST.search(cleaned) or SUBSTANCE.search(cleaned)
            or DESCRIBES.search(cleaned)):
        return ""
    return cleaned


def _runner_card(candidate: Candidate) -> str:
    """A tier runner.

    A coin with a story earns a sentence and two numbers: what it is worth now
    and the high it reached. A coin without one has nothing to say, so the
    numbers are the content and it gets the full tape instead.
    """
    # What happened, then what the coin is, then the mechanical read. The read
    # recites the numbers printed beside it, so it is the last resort.
    lore_text = " ".join(
        str((candidate.provider_evidence.get("why", {}) or {}).get("cause") or "").split()
    )
    why = candidate.provider_evidence.get("why", {}) or {}
    source_url = str(why.get("sourceUrl") or "").strip()
    if not source_url:
        source_url = next(
            (
                str(item.get("url") or "").strip()
                for item in (candidate.news_evidence or [])
                if str(item.get("url") or "").strip()
            ),
            "",
        )
    lore_text = _usable_lore(lore_text)
    if not lore_text:
        for item in candidate.news_evidence or []:
            summary = _usable_lore(str(item.get("summary") or ""))
            if summary:
                lore_text = summary[:220]
                break


    has_story = bool(
        (candidate.provider_evidence.get("why", {}) or {}).get("cause")
        or any(
            len(" ".join(str(item.get("summary") or "").split())) > 30
            for item in candidate.news_evidence or []
        )
    )

    if has_story:
        # The sentence is the point. Two numbers frame it and nothing else
        # competes: what it is worth now, and the high it reached.
        current = float(candidate.token.market_cap or 0)
        high = peak_cap(candidate)
        parts = []
        if current > 0:
            parts.append(f"{money(current)} now")
        if high > 0:
            parts.append(f"{money(high)} high")
        tail = (
            f'<div style="{_txt(13, 700, MUTED, 1.5, 0.02)};padding-top:10px">'
            + _e("   ".join(parts)).replace("   ", "&nbsp;&nbsp;&middot;&nbsp;&nbsp;")
            + "</div>"
            if parts else ""
        )
    else:
        move = _move_line(candidate)
        path = _window_path(candidate)
        tail = ""
        if move:
            tail += f'<div style="{_txt(13, 650, INK_2, 1.5)};padding-top:8px">{_e(move)}</div>'
        if path:
            tail += f'<div style="{_txt(12, 500, MUTED, 1.6)};padding-top:6px">{_e(path)}</div>'

    block = _coin_block(candidate, candidate.token.symbol, lore_text,
                        money(peak_cap(candidate)), spare=has_story)
    if not tail:
        if not source_url:
            return block
        tail = (
            f'<div style="padding-top:10px"><a href="{_e(source_url)}" target="_blank" '
            f'style="text-decoration:none;{_txt(12, 800, BLUE, 1.2, 0.05, "uppercase")}">'
            "Read source &#8599;</a></div>"
        )
    elif source_url:
        tail += (
            f'<div style="padding-top:10px"><a href="{_e(source_url)}" target="_blank" '
            f'style="text-decoration:none;{_txt(12, 800, BLUE, 1.2, 0.05, "uppercase")}">'
            "Read source &#8599;</a></div>"
        )
    # Append at the end of the card, not at the first closing tag, which
    # belongs to the ticker row and put the numbers above the sentence.
    close = block.rfind("</td></tr>")
    return block[:close] + tail + block[close:]


def _theme_section(title: str, members: list[Candidate]) -> str:
    rows = [_story_head(title)]
    for index, candidate in enumerate(members):
        if index:
            rows.append(_rule(top=26))
        rows.append(_runner_card(candidate))
    return "".join(rows)


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
    big = sum(1 for c in runners if peak_cap(c) >= 1_000_000)
    cells = [
        ("Recap coins", str(len(runners))),
        ("Clean", str(len(brief.runners))),
        ("Fresh", str(fresh)),
        ("Hit $1M+", str(big)),
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
        f'<div style="{_txt(12, 750, MUTED, 1.2, 0.08, "uppercase")};padding-top:7px">peak market cap</div>'
        "</td></tr></table>"
        f'<div style="{_txt(17, 450, INK_2, 1.7)};padding-top:22px">{_e(candidate.read)}</div>'
        f'<div style="padding-top:22px">{_bar(1.0, BLUE, 10)}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="padding-top:20px"><tr>'
        f'{_mini_stat("Now", money(token.market_cap))}'
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
        f'<a href="{_e(_chart_url(candidate))}" target="_blank" '
        f'style="display:inline-block;background:{INK};color:{SURFACE};border-radius:999px;'
        f'padding:13px 19px;text-decoration:none;{_txt(13, 800, SURFACE, 1.0)}">Trade on Fomo</a>'
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
        f'{_e(_chain_name(token.chain_id))} / {_e(_age(candidate))}{_e(kol)}</div>'
        "</td>"
        '<td align="right" style="vertical-align:top;white-space:nowrap">'
        f'<div style="{_txt(26, 850, INK, 1.0, -0.03)}">{_e(_size(candidate))}</div>'
        "</td></tr></table>"
        f'<div style="padding-top:14px">{_bar(share, accent if risks else BLUE, 7)}</div>'
        f'<div style="{_txt(13, 500, reason_color, 1.6)};padding-top:12px">{_e(reason)}</div>'
        f'<div style="padding-top:13px"><a href="{_e(_chart_url(candidate))}" target="_blank" '
        f'style="text-decoration:none;{_txt(12, 800, BLUE, 1.2, 0.06, "uppercase")}">Trade on Fomo</a></div>'
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
        f'<div style="padding-top:14px"><a href="{_e(_chart_url(candidate))}" target="_blank" '
        f'style="text-decoration:none;{_txt(12, 800, BLUE, 1.2, 0.06, "uppercase")}">Trade on Fomo</a></div>'
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
        f'{_mini_stat("Peak", money(peak_cap(candidate)), BLUE)}'
        '</tr></table>'
        f'<div style="padding-top:20px"><a href="{_e(_chart_url(candidate))}" target="_blank" '
        f'style="display:inline-block;background:{INK};color:{SURFACE};border-radius:999px;'
        f'padding:13px 19px;text-decoration:none;{_txt(13, 800, SURFACE, 1.0)}">Trade on Fomo</a>'
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
    body = (
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
    return _provider_neutral_email(body)



def _tape_row(candidate: Candidate, rank: int) -> str:
    """One of the day's biggest markets, told rather than ranked."""
    token = candidate.token
    kol_count = kol_trade_count(candidate)
    kol = (
        f'<div style="{_txt(13, 650, VIOLET, 1.5)};padding-top:10px">'
        f"{kol_count} tracked wallets traded it</div>"
        if kol_count else ""
    )
    return (
        '<tr><td style="padding:0 30px 10px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:18px"><tr>'
        '<td style="padding:20px 22px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:top;padding-right:12px">'
        f'<div style="{_txt(12, 800, MUTED, 1.2, 0.10)}">{rank:02d}</div>'
        f'<div style="{_txt(23, 850, INK, 1.1, -0.025)};padding-top:6px">${_e(token.symbol)}</div>'
        f'<div style="{_txt(12, 550, MUTED, 1.5)};padding-top:5px">'
        f"{_e(_chain_name(token.chain_id))} / {_e(_age(candidate))}</div></td>"
        '<td align="right" style="vertical-align:top;white-space:nowrap">'
        f'<div style="{_txt(20, 850, INK, 1.0, -0.02)}">hit {_e(money(peak_cap(candidate)))}</div>'
        f'<div style="{_txt(12, 750, MUTED, 1.2, 0.06, "uppercase")};padding-top:6px">'
        f"{_e(money(token.volume_24h))} traded</div></td>"
        "</tr></table>"
        f'<div style="{_txt(15, 450, INK_2, 1.65)};padding-top:14px">{_e(candidate.read)}</div>'
        + kol +
        f'<div style="padding-top:13px"><a href="{_e(_chart_url(candidate))}" target="_blank" '
        f'style="text-decoration:none;{_txt(12, 800, BLUE, 1.2, 0.06, "uppercase")}">Trade on Fomo</a></div>'
        "</td></tr></table></td></tr>"
    )



# Labels that are either our own vocabulary or true of most coins on a chain.
# On the row they read as alarms, which makes the real alarms worth less.
SHOP_TALK = (
    "publisher floor", "publisher ceiling", "organic confirmations",
    "did not qualify", "below floor", "dexscreener boost", "ticker also used",
    "also used by", "no linked social", "lore ", "has run before",
)


def _is_shop_talk(label: str) -> bool:
    return any(term in label.lower() for term in SHOP_TALK)


def _rule(colour: str = LINE, height: int = 1, top: int = 0) -> str:
    """A hairline. The only separator this email uses."""
    return (
        f'<tr><td style="padding:{top}px 22px 0">'
        f'<div style="height:{height}px;background:{colour};font-size:0;line-height:{height}px">'
        "&nbsp;</div></td></tr>"
    )


def _kept_bar(kept: float) -> str:
    """How much of the high the coin still holds.

    The one graphic in the email, and the only place colour carries meaning
    rather than decoration. Every other number says how far a coin went; this
    says whether it stayed, which is the question at seven in the morning.
    """
    filled = max(2, min(100, round(kept * 100)))
    rest = 100 - filled
    colour = BLUE if kept >= 0.5 else (AMBER if kept >= 0.25 else RED)
    empty = (
        f'<td width="{rest}%" bgcolor="{LINE}" style="background:{LINE};'
        'font-size:0;line-height:4px">&nbsp;</td>'
        if rest > 0 else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="border-collapse:collapse;table-layout:fixed">'
        f'<tr><td width="{filled}%" bgcolor="{colour}" style="background:{colour};'
        f'font-size:0;line-height:4px">&nbsp;</td>{empty}</tr></table>'
    )


def _story_head(title: str, count: int = 0) -> str:
    """A section title with room above it. No label, no counter, no rule.

    The whitespace is the separator. A coin row cannot be mistaken for this
    because nothing else in the email is set at this size.
    """
    return (
        '<tr><td style="padding:52px 22px 0">'
        f'<div style="{_txt(24, 800, INK, 1.15, -0.025)}">{_e(title)}</div>'
        "</td></tr>"
    )


ADDRESS_IN_TEXT = re.compile(
    r"(0x[0-9a-fA-F]{40}|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b|\b\w+:[1-9A-HJ-NP-Za-km-z]{32,44})"
)
TIMESTAMP_DUMP = re.compile(r"\d{1,2}:\d{2}\s+[A-Z][a-z]+\s+\d{1,2}")


def _clean_quote(text: str) -> str:
    """The sentence, without the payload pasted around it."""
    cleaned = ADDRESS_IN_TEXT.sub("", text or "")
    cleaned = TIMESTAMP_DUMP.sub("", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return " ".join(cleaned.split())


def _quote(candidate: Candidate) -> str:
    """Somebody else's sentence, set as somebody else's sentence."""
    posts = sorted(
        candidate.x_interactions or [],
        key=lambda post: (post.like_count + post.repost_count * 2),
        reverse=True,
    )
    if not posts:
        return ""
    # Opinion is not evidence. A post saying the writer is bullish tells the
    # reader nothing the price does not, so only posts that report something
    # are shown. They are all still read when working out why a coin ran.
    trusted = [p for p in posts if p.matched_on == "trusted account"]
    if trusted:
        posts = trusted
    elif TRUSTED_ONLY:
        return ""
    else:
        posts = [p for p in posts if CAUSAL_POST.search(p.summary or "")]
    blocks = []
    seen_text: set[str] = set()
    for post in posts:
        if len(blocks) >= 2:
            break
        text = _clean_quote(str(post.summary or ""))[:180]
        # An account that posts the same announcement three times should not
        # crowd out a different account with something else to say.
        fingerprint = text[:60].lower()
        if len(text) < 25 or fingerprint in seen_text:
            continue
        seen_text.add(fingerprint)
        handle = f"@{_e(post.author_handle)}"
        if post.url:
            handle = (f'<a href="{_e(post.url)}" target="_blank" '
                      f'style="color:{VIOLET};text-decoration:none">{handle}</a>')
        blocks.append(
            f'<div style="padding:16px 0 0 14px;border-left:2px solid {LINE}">'
            f'<div style="{_txt(14, 450, MUTED, 1.6)}">{_e(text)}</div>'
            f'<div style="{_txt(12, 700, VIOLET, 1.4)};padding-top:6px">{handle}</div></div>'
        )
    return "".join(blocks)
    post = posts[0]
    text = " ".join(str(post.summary or "").split())[:180]
    if not text:
        return ""
    handle = f"@{_e(post.author_handle)}"
    if post.url:
        handle = (f'<a href="{_e(post.url)}" target="_blank" '
                  f'style="color:{VIOLET};text-decoration:none">{handle}</a>')
    return (
        f'<div style="padding:16px 0 0 14px;border-left:2px solid {LINE}">'
        f'<div style="{_txt(14, 450, MUTED, 1.6)}">{_e(text)}</div>'
        f'<div style="{_txt(12, 700, VIOLET, 1.4)};padding-top:6px">{handle}</div></div>'
    )


def _coin_block(candidate, symbol: str, line: str, peak: str, spare: bool = False) -> str:
    """One coin, given room rather than a border.

    Boxes around every row made the page feel packed and made four kinds of
    information look like one. These are the same four zones, separated by
    space, which is what a reader parses without being told to.
    """
    url = _chart_url(candidate) if candidate else ""
    ticker = f"${_e(symbol)}"
    if url:
        ticker = (f'<a href="{_e(url)}" target="_blank" '
                  f'style="text-decoration:none;color:{INK}">{ticker}</a>')

    body = ""
    if line:
        body = f'<div style="{_txt(16, 450, INK_2, 1.62)};padding-top:10px">{_e(line)}</div>'

    kept_block = ""
    facts_line = ""
    risk_line = ""
    if candidate is not None and not spare:
        high = peak_cap(candidate)
        current = float(candidate.token.market_cap or 0)
        single = (candidate.provider_evidence.get("lifecycle", {}) or {}).get(
            "peakIsSingleObservation"
        )
        if high > 0 and current > 0 and not single:
            kept = max(0.0, min(1.0, current / high))
            note = "held the high" if kept >= 0.95 else f"kept {kept * 100:.0f}%"
            kept_block = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                'border="0" style="padding-top:18px"><tr>'
                f'<td width="62%" style="vertical-align:middle">{_kept_bar(kept)}</td>'
                f'<td style="padding-left:12px;{_txt(12, 700, MUTED, 1.2, 0.02)}">'
                f"{_e(note)}</td></tr></table>"
            )

        facts = []
        hours = candidate.signals.age_hours
        if hours:
            facts.append(f"{hours:.0f}h old" if hours < 48 else f"{hours / 24:.0f}d old")
        if candidate.safety.holder_count:
            facts.append(f"{candidate.safety.holder_count:,} holders")
        traded = kol_trade_count(candidate)
        if traded:
            facts.append(f"{traded} wallets")
        facts.append(_chain_name(candidate.token.chain_id))
        # Wide spacing instead of a run of separator dots.
        facts_line = (
            f'<div style="{_txt(12, 600, MUTED, 1.9, 0.02)};padding-top:14px">'
            + "&nbsp;&nbsp;&nbsp;&nbsp;".join(_e(fact) for fact in facts)
            + "</div>"
        )
    if candidate is not None:
        risks = [
            label for label in (candidate.risk_labels or [])
            if not _is_unknown(label) and not _is_shop_talk(label)
        ]
        if risks:
            risk_line = (
                f'<div style="{_txt(12, 700, RED, 1.6)};padding-top:8px">{_e(risks[0])}</div>'
            )

    return (
        '<tr><td style="padding:30px 22px 0">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:baseline">'
        f'<span style="{_txt(20, 800, INK, 1.2, -0.015)}">{ticker}</span></td>'
        '<td align="right" style="vertical-align:baseline;white-space:nowrap">'
        f'<span style="{_txt(24, 800, BLUE, 1.1, -0.025)}">{_e(peak)}</span></td>'
        "</tr></table>"
        # The address, so a reader can copy it without leaving the email.
        + (
            f'<div style="{_txt(11, 500, MUTED, 1.5)};padding-top:7px;'
            f'word-break:break-all">{_e(candidate.token.mint)}</div>'
            if candidate is not None and candidate.token.mint else ""
        )
        + body + kept_block + facts_line + risk_line
        + (_quote(candidate) if candidate else "")
        + "</td></tr>"
    )

def _coin_card(candidate, symbol: str, line: str) -> str:
    peak = money(peak_cap(candidate)) if candidate else ""
    return _coin_block(candidate, symbol, _usable_lore(line) or line, peak)


def _coin_line(candidate, symbol: str) -> str:
    """A coin that ran without a story: ticker, peak, and where it ended."""
    peak = money(peak_cap(candidate)) if candidate else ""
    url = _chart_url(candidate) if candidate else ""
    ticker = f"${_e(symbol)}"
    if url:
        ticker = (f'<a href="{_e(url)}" target="_blank" style="text-decoration:none;'
                  f'color:{INK}">{ticker}</a>')
    tail = ""
    if candidate is not None:
        high = peak_cap(candidate)
        current = float(candidate.token.market_cap or 0)
        single = (candidate.provider_evidence.get("lifecycle", {}) or {}).get(
            "peakIsSingleObservation"
        )
        if high > 0 and current > 0 and not single:
            kept = max(0.0, min(1.0, current / high))
            tail = "held it" if kept >= 0.95 else f"kept {kept * 100:.0f}%"
    return (
        '<tr><td style="padding:0 18px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border-bottom:1px solid {LINE}"><tr>'
        f'<td style="padding:11px 14px;{_txt(15, 800, INK, 1.3)}">{ticker}</td>'
        f'<td align="right" style="padding:11px 14px;white-space:nowrap">'
        f'<span style="{_txt(15, 800, BLUE, 1.3)}">{_e(peak)}</span>'
        + (f'<span style="{_txt(12, 600, MUTED, 1.3)}"> &middot; {_e(tail)}</span>' if tail else "")
        + "</td></tr></table></td></tr>"
    )


def _story_note(text: str) -> str:
    """A dashed line of context under a section, as the recap it imitates writes."""
    return (
        '<tr><td style="padding:12px 18px 0">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="vertical-align:top;padding-right:10px;{_txt(15, 800, BLUE, 1.6)}">&mdash;</td>'
        f'<td style="{_txt(14, 450, MUTED, 1.65)}">{_e(text)}</td>'
        '</tr></table></td></tr>'
    )


def _narrative_rows(section: dict, by_symbol: dict) -> list[str]:
    """A story: its title, its coins, then the notes that belong to all of them."""
    coins = section.get("coins") or []
    compact = str(section.get("title") or "").strip().lower() in {"also ran", "the rest"}
    rows = [_story_head(str(section.get("title") or ""))]
    for index, item in enumerate(coins):
        symbol = str(item.get("symbol") or "")
        candidate = by_symbol.get(symbol.upper())
        if compact:
            rows.append(_coin_line(candidate, symbol))
            continue
        if index:
            rows.append(_rule(top=26))
        rows.append(_coin_card(candidate, symbol, str(item.get("line") or "")))
    for note in section.get("bullets") or []:
        rows.append(_story_note(str(note)))
    return rows


def _editorial_coin_row(candidate: Candidate | None, item: dict) -> str:
    """One flat, readable newsletter row for a researched coin.

    A 27-name recap became difficult to scan when every coin was a card and the
    same names also appeared in the section summary. This row keeps the useful
    hierarchy without nesting: result, explanation, caveat, then links.
    """
    symbol = str(item.get("symbol") or (candidate.token.symbol if candidate else "?"))
    thesis = _compact_copy(item.get("line"), 230)
    if not thesis and candidate is not None:
        thesis = _coin_thesis(candidate)
    high = money(peak_cap(candidate)) if candidate is not None else ""
    chart = _chart_url(candidate) if candidate is not None else ""
    mint = candidate.token.mint if candidate is not None else ""
    risks = [] if candidate is None else [
        label for label in (candidate.risk_labels or [])
        if not _is_unknown(label) and not _is_shop_talk(label)
    ]
    watch = _compact_copy(risks[0], 205) if risks else ""

    source = ""
    if candidate is not None:
        why = candidate.provider_evidence.get("why", {}) or {}
        source = str(why.get("sourceUrl") or "").strip()
        if not source:
            source = next(
                (
                    str(row.get("url") or "").strip()
                    for row in (candidate.news_evidence or [])
                    if str(row.get("url") or "").strip()
                ),
                "",
            )

    ticker = f"${_e(symbol)}"
    if chart:
        ticker = (
            f'<a href="{_e(chart)}" target="_blank" '
            f'style="color:{INK};text-decoration:none">{ticker}</a>'
        )
    links = []
    if chart:
        links.append(
            f'<a href="{_e(chart)}" target="_blank" '
            f'style="color:{BLUE};text-decoration:none">Fomo &#8599;</a>'
        )
    if source:
        links.append(
            f'<a href="{_e(source)}" target="_blank" '
            f'style="color:{BLUE};text-decoration:none">Source &#8599;</a>'
        )
    links_html = "&nbsp;&nbsp;&nbsp;".join(links)

    return (
        '<tr><td style="padding:0 22px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-bottom:1px solid {LINE}"><tr><td style="padding:20px 0 19px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="vertical-align:baseline;{_txt(18, 800, INK, 1.25, -0.015)}">{ticker}</td>'
        f'<td align="right" style="vertical-align:baseline;white-space:nowrap;{_txt(19, 800, BLUE, 1.2, -0.02)}">'
        f'{_e(high)}</td></tr></table>'
        + (
            f'<div style="{_txt(15, 450, INK_2, 1.58)};padding-top:8px">{_e(thesis)}</div>'
            if thesis else ""
        )
        + (
            f'<div style="{_txt(12, 650, RED, 1.55)};padding-top:8px">Watch: {_e(watch)}</div>'
            if watch else ""
        )
        + (
            f'<div style="{_txt(11, 500, SOFT, 1.45)};padding-top:9px;word-break:break-all">'
            f'{_e(mint)}</div>' if mint else ""
        )
        + (
            f'<div style="{_txt(12, 750, BLUE, 1.3)};padding-top:9px">{links_html}</div>'
            if links_html else ""
        )
        + '</td></tr></table></td></tr>'
    )


def _short_recap_coin(candidate: Candidate | None, item: dict) -> str:
    """Tweet-style recap line: result first, one plain-language reason."""
    symbol = str(item.get("symbol") or (candidate.token.symbol if candidate else "?"))
    result = str(item.get("result") or "").strip()
    if not result and candidate is not None:
        result = f"hit {money(peak_cap(candidate))}"
    line = _compact_copy(item.get("line"), 155)
    chart = _chart_url(candidate) if candidate is not None else ""
    source = ""
    if candidate is not None:
        why = candidate.provider_evidence.get("why", {}) or {}
        source = str(why.get("sourceUrl") or "").strip()

    ticker = f"${_e(symbol)}"
    if chart:
        ticker = (
            f'<a href="{_e(chart)}" target="_blank" '
            f'style="color:{INK};text-decoration:none">{ticker}</a>'
        )
    source_link = (
        f' <a href="{_e(source)}" target="_blank" '
        f'style="color:{MUTED};text-decoration:none;font-size:11px">source &#8599;</a>'
        if source else ""
    )
    description = f", {_e(line)}" if line else ""
    return (
        '<tr><td style="padding:0 22px">'
        f'<div style="border-bottom:1px solid {LINE};padding:10px 0;'
        f'{_txt(15, 450, INK_2, 1.5)}">'
        f'<span style="font-weight:800;color:{INK}">{ticker}</span> '
        f'<span style="color:{MUTED}">-&gt;</span> '
        f'<span style="font-weight:750;color:{INK}">{_e(result)}</span>'
        f'{description}{source_link}</div></td></tr>'
    )


def render_email(brief: Brief, settings: Settings) -> str:
    report_url = str(settings.get("delivery", "report_url", "") or "")
    runners = _newsletter_recap_candidates(brief, settings)
    _validate_daily_email(brief, runners)
    rows: list[str] = []

    rows.append(_masthead(brief, runners))

    narrative = brief.narrative or {}
    narrative_rendered = False
    if narrative.get("sections"):
        intro = str(narrative.get("intro") or "").strip()
        if intro:
            rows.append(
                '<tr><td style="padding:30px 18px 0">'
                f'<div style="{_txt(19, 500, INK_2, 1.5, -0.01)}">{_e(intro)}</div>'
                "</td></tr>"
            )
        # A researched recap is already organised into stories. Render each
        # coin once inside that story instead of summarising the same names and
        # then repeating them as a second stack of tier cards.
        by_mint, by_symbol = _candidate_maps(runners)
        for story in narrative["sections"]:
            title = str(story.get("title") or "").strip()
            coins = list(story.get("coins") or [])
            if not title or not coins:
                continue
            rows.append(_story_head(title))
            for item in coins:
                symbol = str(item.get("symbol") or "").upper()
                candidate = _resolve_narrative_candidate(item, by_mint, by_symbol)
                if narrative.get("layout") == "short":
                    rows.append(_short_recap_coin(candidate, item))
                else:
                    rows.append(_editorial_coin_row(candidate, item))
            notes = [str(note).strip() for note in (story.get("bullets") or []) if str(note).strip()]
            note_limit = 3 if narrative.get("layout") == "short" else 1
            for note in notes[:note_limit]:
                rows.append(_story_note(note))
            narrative_rendered = True

    # Fall back to the ranked tape whenever the writer produced nothing.
    tape = [] if narrative.get("sections") else [c for c in (brief.headline_tape or [])][:5]
    if tape:
        rows.append(_section(
            "What happened today",
            "The day's biggest markets by volume traded, whether or not they ran.",
        ))
        for index, candidate in enumerate(tape, start=1):
            rows.append(_tape_row(candidate, index))

    # The written recap already places every coin, so rendering the tier
    # sections underneath printed the whole board a second time: the email was
    # 145KB and Gmail clips at about 102KB, which is why readers were being
    # asked to open the full message. Tiers are the fallback, not a companion.
    if runners and not narrative_rendered:
        rows.append(_section(
            "Runners of the day",
            "What moved in the last 24 hours. Each line shows the high and whether the move held or faded.",
        ))
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
    elif not runners:
        rows.append(_section("Runners of the day"))
        rows.append(_empty_state())

    rows.append(_footer(report_url))

    when = brief.generated_at.strftime("%d %b %Y")
    preheader = (
        f"{len(runners)} runners, {sum(1 for c in runners if peak_cap(c) >= 1_000_000)} hit $1M+"
        if runners
        else "Nothing ran today that cleared the floors"
    )
    body = (
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
    return _provider_neutral_email(body)
