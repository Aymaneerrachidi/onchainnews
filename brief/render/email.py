"""The daily report as an email.

Email clients are hostile to modern web design. Gmail strips many style blocks,
Outlook still prefers tables, web fonts are unreliable, and SVG does not belong
in a production inbox. This renderer is intentionally table-first and fully
inline-styled so the report reads like a polished morning memo everywhere.
"""
from __future__ import annotations

import html

from brief.config import Settings
from brief.models import Brief, Candidate
from brief.render.formatting import money, pct
from brief.render.html import report_picks


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
    runners = brief.runners
    date = brief.generated_at.strftime("%d %b")
    if not runners:
        return f"{prefix} | quiet tape | {date}"
    top = max(runners, key=lambda c: c.run_multiple)
    return f"{prefix} | ${top.token.symbol} {top.run_multiple:.0f}x and {len(runners) - 1} more | {date}"


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
    if candidate.run_multiple >= 2:
        return f"{candidate.run_multiple:.1f}x"
    return pct(candidate.token.price_change_24h, 0)


def _is_unknown(label: str) -> bool:
    return any(marker in label.lower() for marker in UNKNOWN_MARKERS)


def _split_labels(labels: list[str]) -> tuple[list[str], list[str]]:
    risks = [label for label in labels if not _is_unknown(label)]
    gaps = [label for label in labels if _is_unknown(label)]
    return risks, gaps


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


def _masthead(brief: Brief, runners: list[Candidate]) -> str:
    when = brief.generated_at.strftime("%d %b %Y")
    window = f"{brief.generated_at.strftime('%H:%M')} {brief.generated_at.tzname() or ''}".strip()
    lead = (
        f"{len(runners)} runners cleared the desk today. The strongest tape is first; weak structure is called out on the row."
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
        f'<div style="{_txt(13, 800, "#BFC7FF", 1.2, 0.12, "uppercase")}">Morning on-chain memo</div>'
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
        "Built for a fast first read: what ran, why it mattered, and what looked fragile."
        "</div>"
        "</td></tr></table></td></tr>"
    )


def _stat_band(brief: Brief, runners: list[Candidate]) -> str:
    chains = {candidate.token.chain_id.lower() for candidate in runners}
    fresh = sum(1 for c in runners if c.signals.age_hours is not None and c.signals.age_hours <= 24)
    big = sum(1 for c in runners if c.run_multiple >= 5)
    cells = [
        ("Runners", str(len(runners))),
        ("Fresh", str(fresh)),
        ("Did 5x+", str(big)),
        ("Filtered", str(len(brief.blocked_runners))),
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
        f'{_mini_stat("Market cap", money(token.market_cap))}'
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
        f'{_e(_chain_name(token.chain_id))} / {_e(_age(candidate))} / {_e(money(token.market_cap))} mcap{_e(kol)}</div>'
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


def render_email(brief: Brief, settings: Settings) -> str:
    report_url = str(settings.get("delivery", "report_url", "") or "")
    runners = sorted(brief.runners, key=lambda candidate: candidate.run_multiple, reverse=True)
    rows: list[str] = []

    rows.append(_masthead(brief, runners))
    rows.append(_stat_band(brief, runners))

    if runners:
        biggest = runners[0]
        top_multiple = max(biggest.run_multiple, 1.0)
        rows.append(_section("Biggest run", "Start here. One line for what happened, then the structure beneath it."))
        rows.append(_hero(biggest))

        rest = runners[1:]
        if rest:
            rows.append(_section("Runners of the day", "Fast scan. Bar length is the move relative to the top runner."))
            rows.extend(_runner_row(candidate, candidate.run_multiple / top_multiple) for candidate in rest)
    else:
        rows.append(_section("Runners of the day"))
        rows.append(_empty_state())

    if brief.blocked_runners:
        rows.append(_section("Ran, but disqualified", "Movement was there. Market structure was not."))
        rows.extend(_blocked_row(candidate) for candidate in brief.blocked_runners[:8])

    picks = report_picks(brief)
    if runners:
        picks = [candidate for candidate in picks if candidate.token.mint != runners[0].token.mint]
    if picks:
        rows.append(_section("Worth a closer look", "The quieter rows that still had a reason to exist."))
        rows.extend(_pick_row(candidate) for candidate in picks)
    else:
        rows.append(
            '<tr><td style="padding:24px 30px 0">'
            f'<div style="{_txt(13, 450, MUTED, 1.7)}">Nothing else cleared the bar for the editorial tracks today.</div>'
            "</td></tr>"
        )

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
