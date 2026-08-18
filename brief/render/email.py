from __future__ import annotations

import html

from brief.config import Settings
from brief.models import Brief, Candidate
from brief.render.formatting import money, pct, ratio
from brief.render.html import report_picks


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def email_subject(brief: Brief, settings: Settings) -> str:
    prefix = str(settings.get("delivery", "email_subject_prefix", "Solana Brief"))
    return f"{prefix} — {brief.generated_at.strftime('%d %b %Y')}"


def _cell(value: str, label: str, color: str = "#1a1a1a") -> str:
    return (
        f'<td style="padding:8px 14px;border-top:1px solid #e3e1da;'
        f'font:600 13px/1.4 Arial,Helvetica,sans-serif;color:{color}">'
        f"{value}<br>"
        f'<span style="font:400 10px Arial,Helvetica,sans-serif;color:#8a877f;'
        f'letter-spacing:.08em">{label}</span></td>'
    )


def _runner_row(candidate: Candidate, index: int) -> str:
    token = candidate.token
    signal = candidate.signals
    age = "N/A" if signal.age_hours is None else (
        f"{signal.age_hours:.0f}H" if signal.age_hours < 48 else f"{signal.age_hours / 24:.0f}D"
    )
    multiple = f"{candidate.run_multiple:.1f}x" if candidate.run_multiple >= 2 else pct(token.price_change_24h, 0)
    labels = "".join(
        f'<li style="font:400 11px/1.5 Arial,Helvetica,sans-serif;color:#b3261e;margin:0 0 2px">{_e(label)}</li>'
        for label in candidate.risk_labels
    )
    kol = f'<span style="display:inline-block;background:#b3261e;color:#fff;font:700 10px Arial,Helvetica,sans-serif;padding:1px 6px;margin-right:4px">{len(candidate.kol_buyers)} KOL</span>' if candidate.kol_buyers else ""
    lore = f'<span style="display:inline-block;background:#1a1a1a;color:#f5f3ee;font:700 10px Arial,Helvetica,sans-serif;padding:1px 6px;margin-right:4px">{_e(candidate.lore)}</span>' if candidate.lore else ""
    read = candidate.read or "No summary available."
    return f"""
<tr style="mso-table-lspace:0;mso-table-rspace:0">
  <td style="padding:14px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:0;border-color:#e3e1da">
      <tr>
        <td style="padding:0 14px 0 0;font:800 15px Arial,Helvetica,sans-serif;color:#b3261e;vertical-align:top">${_e(token.symbol)}</td>
        <td style="padding:0 14px 0 0;font:700 15px Arial,Helvetica,sans-serif;color:#1a1a1a;color:#237343;vertical-align:top">{_e(multiple)}<br><span style="font:400 10px Arial,Helvetica,sans-serif;color:#8a877f;letter-spacing:.08em">RUN 24H</span></td>
        <td style="padding:0 14px 0 0;font:700 13px Arial,Helvetica,sans-serif;color:#1a1a1a;vertical-align:top">{_e(pct(token.price_change_1h, 0))}<br><span style="font:400 10px Arial,Helvetica,sans-serif;color:#8a877f;letter-spacing:.08em">1H</span></td>
        <td style="padding:0 14px 0 0;font:700 13px Arial,Helvetica,sans-serif;color:#1a1a1a;vertical-align:top">{_e(money(token.market_cap))}<br><span style="font:400 10px Arial,Helvetica,sans-serif;color:#8a877f;letter-spacing:.08em">MCAP</span></td>
        <td style="padding:0 14px 0 0;font:700 13px Arial,Helvetica,sans-serif;color:#1a1a1a;vertical-align:top">{_e(money(token.volume_24h))}<br><span style="font:400 10px Arial,Helvetica,sans-serif;color:#8a877f;letter-spacing:.08em">VOL 24H</span></td>
        <td style="padding:0;font:700 13px Arial,Helvetica,sans-serif;color:#1a1a1a;vertical-align:top">{_e(age)}<br><span style="font:400 10px Arial,Helvetica,sans-serif;color:#8a877f;letter-spacing:.08em">AGE</span></td>
      </tr>
      <tr>
        <td colspan="6" style="padding:8px 0 0">
          <p style="margin:0 0 8px;font:400 13px/1.55 Arial,Helvetica,sans-serif;color:#33302a">{_e(read)}</p>
          <p style="margin:0 0 4px">{kol}{lore}<span style="font:400 11px Arial,Helvetica,sans-serif;color:#8a877f">TURNOVER {_e(ratio(signal.turnover))} · 6H BUYS {_e("N/A" if signal.buy_imbalance_6h is None else f"{signal.buy_imbalance_6h:.0%}")} · MINT {_e(token.mint)}</span></p>
          <ul style="margin:0;padding-left:16px">{labels}</ul>
          <p style="margin:6px 0 0"><a href="{_e(token.url)}" style="color:#b3261e;font:700 11px Arial,Helvetica,sans-serif;text-decoration:underline" target="_blank">OPEN DEXSCREENER &#8599;</a></p>
        </td>
      </tr>
    </table>
  </td>
</tr>"""


def _blocked_rows(brief: Brief) -> str:
    rows = "".join(
        f'<li style="margin:0 0 6px;font:400 12px/1.5 Arial,Helvetica,sans-serif;color:#33302a">'
        f'<b>${_e(candidate.token.symbol)}</b> {_e(pct(candidate.token.price_change_24h, 0))} — '
        f'{_e("; ".join(candidate.risk_labels))}</li>'
        for candidate in brief.blocked_runners
    )
    if not rows:
        rows = '<li style="font:400 12px Arial,Helvetica,sans-serif;color:#8a877f">No runner was disqualified today.</li>'
    return f'<ul style="margin:0;padding-left:18px">{rows}</ul>'


def render_email(brief: Brief, settings: Settings) -> str:
    """Flat, inline-styled standalone email. No <details>, no external CSS:
    the interactive report does not survive email clients."""
    generated = brief.generated_at.strftime("%a %d %b %Y / %H:%M %Z")
    window_start = brief.window_start or brief.generated_at
    window_text = (
        f"{window_start.strftime('%d %b / %H:%M')} &rarr; "
        f"{brief.generated_at.strftime('%d %b / %H:%M %Z')}"
    )
    picks = report_picks(brief)
    pick_rows = "".join(
        f"""
<tr>
  <td style="padding:12px 14px;border-top:1px solid #e3e1da">
    <p style="margin:0 0 4px">
      <span style="display:inline-block;background:#1a1a1a;color:#f5f3ee;font:700 10px Arial,Helvetica,sans-serif;padding:2px 7px;margin-right:8px">{_e(candidate.track)}</span>
      <b style="font:800 16px Arial,Helvetica,sans-serif;color:#1a1a1a">${_e(candidate.token.symbol)}</b>
      <span style="font:400 12px Arial,Helvetica,sans-serif;color:#8a877f"> — {_e(candidate.token.name)}</span>
    </p>
    <p style="margin:0 0 6px;font:400 13px/1.55 Arial,Helvetica,sans-serif;color:#33302a">{_e(candidate.read)}</p>
    <p style="margin:0"><a href="{_e(candidate.token.url)}" style="color:#b3261e;font:700 11px Arial,Helvetica,sans-serif" target="_blank">CHART &#8599;</a></p>
  </td>
</tr>"""
        for candidate in picks
    ) or (
        '<tr><td style="padding:16px 14px;font:400 12px Arial,Helvetica,sans-serif;color:#8a877f">'
        "NOTHING CLEARED THE BAR TODAY. AN EMPTY BRIEF IS A RESULT, NOT AN OUTAGE.</td></tr>"
    )
    runner_rows = "".join(
        _runner_row(candidate, index) for index, candidate in enumerate(brief.runners, 1)
    ) or (
        '<tr><td style="padding:16px 14px;font:400 12px Arial,Helvetica,sans-serif;color:#8a877f">'
        "NOTHING RAN TODAY THAT CLEARED THE FLOORS.</td></tr>"
    )
    scorecard = brief.scorecard
    sc_cells = "".join(
        f'<td style="padding:10px 14px;border-left:1px solid #e3e1da;text-align:center;vertical-align:top">'
        f'<span style="display:block;font:400 9px Arial,Helvetica,sans-serif;color:#8a877f;'
        f'letter-spacing:.08em;margin-bottom:4px">{label}</span>'
        f'<b style="font:800 14px Arial,Helvetica,sans-serif;color:#1a1a1a">{value}</b></td>'
        for label, value in (
            ("FEATURED", str(scorecard.featured_count)),
            ("72H Q1", pct(scorecard.featured_q1_72h)),
            ("72H MEDIAN", pct(scorecard.featured_median_72h)),
            ("72H Q3", pct(scorecard.featured_q3_72h)),
            ("72H UP", pct(scorecard.featured_up_pct_72h, 0)),
            ("LOSS &gt;90%", pct(scorecard.featured_crash_pct_72h, 0)),
        )
    )
    report_url = str(settings.get("delivery", "report_url", "") or "")
    footer_link = (
        f'<p style="margin:0 0 10px"><a href="{_e(report_url)}" '
        f'style="color:#b3261e;font:700 12px Arial,Helvetica,sans-serif" target="_blank">'
        f"OPEN THE FULL REPORT &#8599;</a></p>"
        if report_url
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(email_subject(brief, settings))}</title>
</head>
<body style="margin:0;padding:0;background:#f1efe9">
<div style="max-width:680px;margin:0 auto;background:#ffffff;font-family:Arial,Helvetica,sans-serif">
  <div style="background:#1a1a1a;color:#f5f3ee;padding:22px 26px;border-bottom:4px solid #b3261e">
    <p style="margin:0 0 4px;font:700 11px Arial,Helvetica,sans-serif;letter-spacing:.18em;color:#b3261e">SOLANA BRIEF</p>
    <h1 style="margin:0;font-size:26px;line-height:1.1;letter-spacing:-.02em">{_e(generated)}</h1>
    <p style="margin:10px 0 0;font:400 11px Arial,Helvetica,sans-serif;color:#a8a59c">WINDOW {_e(window_text)} &middot; {len(brief.runners)} RUNNERS &middot; {sum(1 for c in brief.runners if c.run_multiple >= 5)} DID 5X+ &middot; {len(brief.blocked_runners)} DISQUALIFIED</p>
  </div>
  <div style="border-bottom:3px solid #1a1a1a">
    <p style="margin:0;padding:12px 14px;font:800 13px Arial,Helvetica,sans-serif;background:#f5f3ee;border-bottom:1px solid #cfcdc5">THE READ &middot; {len(picks):02d} PICKS</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{pick_rows}</table>
  </div>
  <div style="border-bottom:3px solid #1a1a1a">
    <p style="margin:0;padding:12px 14px;font:800 13px Arial,Helvetica,sans-serif;background:#f5f3ee;border-bottom:1px solid #cfcdc5">RUNNERS OF THE DAY &middot; {len(brief.runners):02d}</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{runner_rows}</table>
  </div>
  <div style="border-bottom:3px solid #1a1a1a">
    <p style="margin:0;padding:12px 14px;font:800 13px Arial,Helvetica,sans-serif;background:#f5f3ee;border-bottom:1px solid #cfcdc5">RAN, BUT DISQUALIFIED &middot; {len(brief.blocked_runners):02d}</p>
    <div style="padding:14px">{_blocked_rows(brief)}</div>
  </div>
  <div style="border-bottom:3px solid #1a1a1a">
    <p style="margin:0;padding:12px 14px;font:800 13px Arial,Helvetica,sans-serif;background:#f5f3ee;border-bottom:1px solid #cfcdc5">30D OUTCOME SCORECARD</p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse"><tr>{sc_cells}</tr></table>
  </div>
  <div style="background:#f5f3ee;padding:18px 26px">
    {footer_link}
    <p style="margin:0;font:400 11px/1.6 Arial,Helvetica,sans-serif;color:#8a877f">Measured changes, never recommendations. Everything here can go to zero.</p>
  </div>
</div>
</body>
</html>"""