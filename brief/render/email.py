"""The daily report as an email.

Email is not the web. Gmail strips `<style>` blocks it dislikes, drops web
fonts entirely, and removes SVG, so the brand's Aeonik and the overlay's QR
codes cannot survive the trip. Everything here is therefore tables and inline
styles, with a system stack chosen to sit as close to Aeonik as installed fonts
allow, and a proportional bar drawn from coloured table cells instead of an
image: it renders in every client, needs no downloads, and turns the list into
something readable in two seconds.

A QR is not needed here either. It exists on the overlay because nobody can tap
a television; in an inbox the address is a link.
"""
from __future__ import annotations

import html

from brief.config import Settings
from brief.models import Brief, Candidate
from brief.render.formatting import money, pct, ratio
from brief.render.html import report_picks


# Quiet inbox palette. The web report can be louder; email needs to read like
# an analyst note in Gmail, Apple Mail, and Outlook.
BLUE = "#3657E3"
BLUE_2 = "#5B36D6"
NAVY = "#17152B"
PAPER = "#F6F7FB"
SURFACE = "#FFFFFF"
SURFACE_2 = "#FAFAFD"
LINE = "#E5E7F0"
MUTED = "#666A7A"
SOFT = "#EEF1FF"
ALERT = "#D43D2A"
TRACK = "#ECEEF6"

# Aeonik cannot be delivered to an inbox, so this is the closest thing every
# reader already has: SF on Apple, Segoe on Windows, Helvetica elsewhere.
FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI','Helvetica Neue',"
        "Helvetica,Arial,sans-serif")

CHAIN_NAMES = {
    "solana": "Solana", "ethereum": "Ethereum", "bsc": "BNB Chain",
    "base": "Base", "robinhood": "Robinhood",
}


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _txt(size: int, weight: int, color: str, leading: float = 1.4,
         tracking: float = 0.0, transform: str = "") -> str:
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


def email_subject(brief: Brief, settings: Settings) -> str:
    prefix = str(settings.get("delivery", "email_subject_prefix", "fomo onchain"))
    runners = brief.runners
    if not runners:
        return f"{prefix} — nothing ran, {brief.generated_at.strftime('%d %b')}"
    top = max(runners, key=lambda c: c.run_multiple)
    # The subject line is the one part read before anything is opened, so it
    # carries the day's headline rather than repeating the date twice.
    return (
        f"{prefix} — ${top.token.symbol} {top.run_multiple:.0f}x"
        f" and {len(runners) - 1} more, {brief.generated_at.strftime('%d %b')}"
    )


def _chain_name(chain: str) -> str:
    return CHAIN_NAMES.get(str(chain or "").lower(), str(chain or "—"))


def _size(candidate: Candidate) -> str:
    if candidate.run_multiple >= 2:
        return f"{candidate.run_multiple:.1f}x"
    return pct(candidate.token.price_change_24h, 0)


def _age(candidate: Candidate) -> str:
    hours = candidate.signals.age_hours
    if hours is None:
        return "age unknown"
    if hours < 1:
        return f"{hours * 60:.0f} min old"
    return f"{hours:.0f}h old"


# A gap in the data is not a danger. Saying "concentration unknown" in the same
# red as "fading, down 15%" made every row look alarming, which is the same as
# no row looking alarming. Unknowns are set quiet; only measured risk is loud.
UNKNOWN_MARKERS = ("unknown", "no contract safety source", "not published")


def _is_unknown(label: str) -> bool:
    return any(marker in label.lower() for marker in UNKNOWN_MARKERS)


def _split_labels(labels: list[str]) -> tuple[list[str], list[str]]:
    risks = [l for l in labels if not _is_unknown(l)]
    gaps = [l for l in labels if _is_unknown(l)]
    return risks, gaps


def _chip(text: str, background: str, color: str = SURFACE) -> str:
    return (
        f'<span style="display:inline-block;background:{background};'
        f'padding:4px 7px;border-radius:6px;{_txt(10, 700, color, 1.0)}">'
        f"{_e(text)}</span>"
    )


def _run_bar(share: float, color: str = BLUE, height: int = 8) -> str:
    """A proportional bar built from two table cells.

    An image would need hosting and would be blocked by default in most
    clients; a bar made of cells always draws. Width encodes the size of the
    run against the biggest one in the same email, so the ranking is legible
    before a single number is read.
    """
    filled = max(3, min(100, round(share * 100)))
    rest = 100 - filled
    empty = (
        f'<td width="{rest}%" bgcolor="{TRACK}" '
        f'style="background:{TRACK};font-size:0;line-height:{height}px">&nbsp;</td>'
        if rest > 0 else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="border-collapse:separate;table-layout:fixed">'
        f'<tr><td width="{filled}%" bgcolor="{color}" '
        f'style="background:{color};font-size:0;line-height:{height}px">&nbsp;</td>{empty}</tr></table>'
    )


def _section(title: str, note: str = "") -> str:
    caption = (
        f'<div style="{_txt(12, 400, MUTED, 1.5)};padding-top:6px">{_e(note)}</div>'
        if note else ""
    )
    return (
        '<tr><td style="padding:28px 28px 10px">'
        f'<div style="{_txt(14, 700, NAVY, 1.25)}">{_e(title)}</div>'
        f"{caption}</td></tr>"
    )


def _hero(candidate: Candidate) -> str:
    """The biggest run of the day, given the room it earns."""
    token = candidate.token
    risks, gaps = _split_labels(candidate.risk_labels)
    flag_line = (
        f'<div style="{_txt(12, 500, "#FF9C88", 1.5)};padding-top:10px">'
        + _e(" · ".join(risks[:3])) + "</div>"
        if risks else ""
    )
    if gaps:
        flag_line += (
            f'<div style="{_txt(11, 400, "#8A88BC", 1.5)};padding-top:6px">'
            + _e(" · ".join(gaps[:2])) + "</div>"
        )
    kol = (
        f'<div style="padding-top:12px">{_chip(f"{len(candidate.kol_buyers)} tracked wallets bought", BLUE_2)}</div>'
        if candidate.kol_buyers else ""
    )
    return (
        '<tr><td style="padding:0 28px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:8px">'
        '<tr><td style="padding:22px 22px 20px">'
        # Symbol and the number, the number given display weight.
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="vertical-align:bottom">'
        f'<div style="{_txt(25, 700, NAVY, 1.05)}">${_e(token.symbol)}</div>'
        f'<div style="{_txt(13, 400, MUTED, 1.4)};padding-top:4px">'
        f"{_e(_chain_name(token.chain_id))} &middot; {_e(_age(candidate))}</div></td>"
        f'<td align="right" style="vertical-align:bottom">'
        f'<div style="{_txt(40, 700, NAVY, 1.0)}">{_e(_size(candidate))}</div>'
        f'<div style="{_txt(11, 500, MUTED, 1.2)};padding-top:2px">'
        "in 24 hours</div></td>"
        "</tr></table>"
        f'<div style="padding-top:16px">{_run_bar(1.0, BLUE)}</div>'
        # The four numbers that decide whether it is worth a look.
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="padding-top:18px"><tr>'
        + _hero_stat("Market cap", money(token.market_cap))
        + _hero_stat("Liquidity", money(token.liquidity_usd))
        + _hero_stat("Holders", f"{candidate.safety.holder_count:,}" if candidate.safety.holder_count else "—")
        + _hero_stat("Last hour", pct(token.price_change_1h, 0), True)
        + "</tr></table>"
        f'<div style="{_txt(14, 400, NAVY, 1.6)};padding-top:18px">'
        + _e(candidate.read) + "</div>"
        + flag_line + kol
        + f'<div style="padding-top:18px">'
        f'<a href="{_e(token.url)}" target="_blank" '
        f'style="display:inline-block;background:{BLUE};color:{SURFACE};'
        f'padding:12px 22px;border-radius:8px;text-decoration:none;'
        f'{_txt(13, 700, SURFACE, 1.0)}">Open the chart</a></div>'
        f'<div style="{_txt(11, 400, MUTED, 1.6)};padding-top:14px;word-break:break-all">'
        + _e(token.mint) + "</div>"
        "</td></tr></table></td></tr>"
    )


def _hero_stat(label: str, value: str, tinted: bool = False) -> str:
    colour = SURFACE
    if tinted and value.startswith("-"):
        colour = "#FF9C88"
    return (
        '<td width="25%" style="vertical-align:top;padding-right:8px">'
        f'<div style="{_txt(11, 500, MUTED, 1.2)}">{_e(label)}</div>'
        f'<div style="{_txt(15, 700, colour, 1.3)};padding-top:5px">{_e(value)}</div></td>'
    )


def _runner_row(candidate: Candidate, share: float) -> str:
    """One line of the ranking: name, size, bar, and what is wrong with it."""
    token = candidate.token
    risks, gaps = _split_labels(candidate.risk_labels)
    # The stripe earns attention only when something was actually measured wrong.
    accent = ALERT if risks else BLUE
    flag_line = (
        f'<div style="{_txt(11, 500, ALERT, 1.5)};padding-top:8px">'
        + _e(" · ".join(risks[:2])) + "</div>"
        if risks else ""
    )
    if gaps:
        flag_line += (
            f'<div style="{_txt(11, 400, MUTED, 1.5)};padding-top:{6 if risks else 8}px">'
            + _e(" · ".join(gaps[:2])) + "</div>"
        )
    kol = (
        f" &middot; <span style=\"color:{BLUE_2};font-weight:700\">"
        f"{len(candidate.kol_buyers)} KOL</span>"
        if candidate.kol_buyers else ""
    )
    return (
        '<tr><td style="padding:0 28px 8px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-left:3px solid {accent};border-radius:7px">'
        '<tr><td style="padding:16px 18px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="vertical-align:top">'
        f'<div style="{_txt(17, 700, NAVY, 1.2, -0.01)}">${_e(token.symbol)}</div>'
        f'<div style="{_txt(11, 400, MUTED, 1.5)};padding-top:3px">'
        f"{_e(_chain_name(token.chain_id))} &middot; {_e(_age(candidate))} &middot; "
        f"{_e(money(token.market_cap))} mcap{kol}</div></td>"
        '<td align="right" style="vertical-align:top;padding-left:12px">'
        f'<div style="{_txt(22, 700, NAVY, 1.0, -0.02)}">{_e(_size(candidate))}</div></td>'
        "</tr></table>"
        f'<div style="padding-top:12px">{_run_bar(share, BLUE, 6)}</div>'
        + flag_line
        + f'<div style="padding-top:12px">'
        f'<a href="{_e(token.url)}" target="_blank" '
        f'style="text-decoration:none;{_txt(11, 700, BLUE, 1.2, 0.08, "uppercase")}">'
        "Open chart &#8594;</a></div>"
        "</td></tr></table></td></tr>"
    )


def _blocked_row(candidate: Candidate) -> str:
    reason = candidate.risk_labels[0] if candidate.risk_labels else "did not qualify"
    return (
        '<tr><td style="padding:0 28px 6px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:7px"><tr>'
        f'<td style="padding:12px 16px;vertical-align:top">'
        f'<span style="{_txt(13, 700, NAVY, 1.3)}">${_e(candidate.token.symbol)}</span>'
        f'<span style="{_txt(11, 400, MUTED, 1.3)}"> &middot; '
        f"{_e(_chain_name(candidate.token.chain_id))}</span>"
        f'<div style="{_txt(11, 400, MUTED, 1.5)};padding-top:4px">{_e(reason)}</div></td>'
        f'<td align="right" style="padding:12px 16px;vertical-align:top;white-space:nowrap">'
        f'<span style="{_txt(13, 700, MUTED, 1.3)}">{_e(_size(candidate))}</span></td>'
        "</tr></table></td></tr>"
    )


def _stat_band(brief: Brief) -> str:
    runners = brief.runners
    chains = {c.token.chain_id.lower() for c in runners}
    big = sum(1 for c in runners if c.run_multiple >= 5)
    def plural(word: str, count: int) -> str:
        return word if count == 1 else word + "s"

    cells = [
        (plural("Runner", len(runners)), str(len(runners))),
        (plural("Chain", len(chains)), str(len(chains))),
        ("Did 5x+", str(big)),
        ("Filtered out", str(len(brief.blocked_runners))),
    ]
    tds = "".join(
        f'<td width="25%" align="center" style="padding:16px 4px;vertical-align:top">'
        f'<div style="{_txt(22, 700, NAVY, 1.0, -0.02)}">{_e(value)}</div>'
        f'<div style="{_txt(10, 700, MUTED, 1.2, 0.1, "uppercase")};padding-top:5px">'
        f"{_e(label)}</div></td>"
        for label, value in cells
    )
    return (
        '<tr><td style="padding:18px 28px 0">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border:1px solid {LINE};border-radius:8px"><tr>{tds}</tr></table></td></tr>'
    )


def render_email(brief: Brief, settings: Settings) -> str:
    report_url = str(settings.get("delivery", "report_url", "") or "")
    runners = sorted(brief.runners, key=lambda c: c.run_multiple, reverse=True)
    when = brief.generated_at.strftime("%d %b %Y")
    window = f"{brief.generated_at.strftime('%H:%M')} {brief.generated_at.tzname() or ''}".strip()

    body: list[str] = []

    # ---- masthead ---------------------------------------------------------
    body.append(
        '<tr><td style="padding:0">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE};border-bottom:1px solid {LINE}"><tr><td style="padding:26px 28px 22px">'
        f'<div style="{_txt(24, 700, NAVY, 1.0)}">fomo <span style="color:{BLUE}">onchain</span></div>'
        f'<div style="{_txt(13, 400, MUTED, 1.5)};padding-top:8px">'
        "Everything that ran in the last 36 hours, ranked, with the risks on the row."
        "</div>"
        f'<div style="{_txt(12, 500, MUTED, 1.4)};padding-top:14px">'
        f"{_e(when)} &middot; {_e(window)}</div>"
        "</td></tr></table></td></tr>"
    )

    body.append(_stat_band(brief))

    # ---- the day ----------------------------------------------------------
    if runners:
        biggest = runners[0]
        top_multiple = max(biggest.run_multiple, 1.0)
        body.append(_section("Biggest run", "The one the day belonged to."))
        body.append(_hero(biggest))

        rest = runners[1:]
        if rest:
            body.append(_section(
                "Runners of the day",
                f"{len(rest)} more cleared the floors. Bar length is the run, "
                "relative to the biggest above.",
            ))
            for candidate in rest:
                body.append(_runner_row(candidate, candidate.run_multiple / top_multiple))
    else:
        body.append(_section("Runners of the day"))
        body.append(
            '<tr><td style="padding:0 28px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:{SURFACE};border:1px solid {LINE};border-radius:8px"><tr>'
            f'<td style="padding:26px 20px;{_txt(14, 400, MUTED, 1.6)}">'
            "Nothing cleared the floors today. An empty report is a result, "
            "not an outage.</td></tr></table></td></tr>"
        )

    # ---- what was kept out -------------------------------------------------
    if brief.blocked_runners:
        body.append(_section(
            "Ran, but disqualified",
            "These moved and were kept out: unsafe to hold, or a manufactured market.",
        ))
        for candidate in brief.blocked_runners[:8]:
            body.append(_blocked_row(candidate))

    # ---- editorial picks ---------------------------------------------------
    picks = report_picks(brief)
    if runners:
        picks = [p for p in picks if p.token.mint != runners[0].token.mint]
    if picks:
        body.append(_section("Worth a closer look"))
        for candidate in picks:
            body.append(
                '<tr><td style="padding:0 28px 8px">'
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                f'style="background:{SURFACE};border:1px solid {LINE};border-radius:7px"><tr>'
                f'<td style="padding:16px 18px">'
                f"{_chip(candidate.track, BLUE)}"
                f'<span style="{_txt(16, 700, NAVY, 1.3)};padding-left:8px">'
                f"${_e(candidate.token.symbol)}</span>"
                f'<div style="{_txt(13, 400, NAVY, 1.6)};padding-top:10px">'
                + _e(candidate.read) + "</div>"
                f'<div style="padding-top:12px">'
                f'<a href="{_e(candidate.token.url)}" target="_blank" '
                f'style="text-decoration:none;{_txt(11, 700, BLUE, 1.2, 0.08, "uppercase")}">'
                "Open chart &#8594;</a></div>"
                "</td></tr></table></td></tr>"
            )
    else:
        body.append(
            '<tr><td style="padding:24px 28px 0">'
            f'<div style="{_txt(12, 400, MUTED, 1.6)}">'
            "Nothing else cleared the bar for the editorial tracks today.</div></td></tr>"
        )

    # ---- footer ------------------------------------------------------------
    link = (
        f'<div style="padding-top:16px"><a href="{_e(report_url)}" target="_blank" '
        f'style="color:{BLUE};text-decoration:none;{_txt(12, 700, BLUE, 1.4)}">'
        "See the full report &#8594;</a></div>"
        if report_url else ""
    )
    body.append(
        '<tr><td style="padding:30px 28px 0">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{SURFACE_2};border:1px solid {LINE};border-radius:8px"><tr>'
        f'<td style="padding:22px">'
        f'<div style="{_txt(12, 400, MUTED, 1.7)}">'
        "Data, not advice. Everything here can go to zero. Coins are included "
        "because they moved, not because they are recommended."
        "</div>" + link +
        "</td></tr></table></td></tr>"
    )

    rows = "".join(body)
    preheader = (
        f"{len(runners)} runners across "
        f"{len({c.token.chain_id for c in runners})} chains"
        if runners else "Nothing ran today that cleared the floors"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        f"<title>fomo onchain — {_e(when)}</title></head>"
        f'<body style="margin:0;padding:0;background:{PAPER};'
        f'-webkit-font-smoothing:antialiased">'
        # Hidden line the inbox shows beside the subject.
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{_e(preheader)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{PAPER}"><tr><td align="center" style="padding:18px 0 36px">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:600px;max-width:600px;background:{PAPER}">'
        f"{rows}"
        "</table></td></tr></table></body></html>"
    )
