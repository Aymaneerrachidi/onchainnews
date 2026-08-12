from __future__ import annotations

import re

from brief.models import Brief, Candidate
from brief.render.formatting import money, pct
from brief.render.markdown import render_markdown


def _runner_line(candidate: Candidate) -> str:
    token = candidate.token
    size = f"{candidate.run_multiple:.1f}x" if candidate.run_multiple >= 2 else pct(token.price_change_24h, 0)
    head = f"${token.symbol} {size} | {money(token.market_cap)} mc | {money(token.volume_24h)} vol"
    if candidate.x_interactions:
        head += f" | {len(candidate.x_interactions)} X match"
    if candidate.kol_buyers:
        head += f" | {len(candidate.kol_buyers)} wallets"
    extra = []
    if candidate.faded_from_peak is not None:
        extra.append(f"fading {pct(candidate.faded_from_peak, 0)} in the last hour")
    if candidate.risk_labels:
        extra.append(candidate.risk_labels[0])
    lines = [head, f"  CA {token.mint}"]
    if candidate.dex_evidence:
        lines.append(f"  WHY: {candidate.dex_evidence[0]}")
    if candidate.x_interactions:
        lead = candidate.x_interactions[0]
        lines.append(
            f"  X: @{lead.author_handle} {lead.interaction} ({lead.confidence}) - {lead.summary} {lead.url}"
        )
    elif candidate.catalyst:
        lines.append(f"  X: {candidate.catalyst}")
    if extra:
        lines.append(f"  RISK: {'; '.join(extra)}")
    return "\n".join(lines)


def render_digest(brief: Brief, report_url: str = "") -> str:
    """The morning message: what ran today, biggest first."""
    when = brief.generated_at.strftime("%a %d %b, %H:%M")
    runners = brief.runners
    fresh = sum(1 for c in runners if c.signals.age_hours is not None and c.signals.age_hours <= 24)
    big = sum(1 for c in runners if c.run_multiple >= 5)
    body = [
        f"RUNNERS TODAY - {when}",
        f"{len(runners)} ran | {fresh} launched today | {big} did 5x+ | {len(brief.blocked_runners)} disqualified",
        "",
    ]
    if runners:
        limit = 8
        body.extend(_runner_line(candidate) for candidate in runners[:limit])
        if len(runners) > limit:
            body.append(f"...and {len(runners) - limit} more in the full report.")
        body.append("")
    else:
        body += ["Nothing ran today that cleared the floors.", ""]

    if brief.kol_flagged:
        body.append("KOL CONVICTION")
        for c in brief.kol_flagged[:5]:
            names = ", ".join(c.kol_buyers[:5])
            more = f" +{len(c.kol_buyers) - 5}" if len(c.kol_buyers) > 5 else ""
            body.append(f"- ${c.token.symbol}: {len(c.kol_buyers)} wallets ({names}{more})")
        body.append("")
    if brief.kol_profit_table:
        body.append("WHERE THEY MADE MONEY")
        body.extend(
            f"- ${symbol}: {realised:+,.0f} SOL realised across {traders} wallet(s)"
            for _, symbol, realised, traders in brief.kol_profit_table[:5]
        )
        body.append("")
    if brief.lore_groups:
        groups = sorted(brief.lore_groups.items(), key=lambda item: len(item[1]), reverse=True)[:4]
        body.append("SHARED LORE")
        body.extend(
            f"- {name}: " + ", ".join("$" + c.token.symbol for c in members)
            for name, members in groups
        )
        body.append("")
    onchain = [finding for finding in brief.onchain if finding.status == "available"][:3]
    if onchain:
        body.append("ON-CHAIN FLAGS")
        body.extend(f"- ${finding.symbol}: {finding.headline}" for finding in onchain)
        body.append("")
    down = [status.name for status in brief.source_statuses if not status.available]
    if down:
        body.append(f"Degraded sources: {', '.join(down)}")
    if report_url:
        body.append(f"Full report: {report_url}")
    body.append("Data, not advice. Everything here can go to zero.")
    return "\n".join(body).strip()


def _chunk(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        addition = paragraph.strip() + "\n\n"
        if current and len(current) + len(addition) > limit:
            chunks.append(current.rstrip())
            current = ""
        while len(addition) > limit:
            chunks.append(addition[:limit])
            addition = addition[limit:]
        current += addition
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def render_telegram(brief: Brief, limit: int = 3900, *, digest: bool = True, report_url: str = "") -> list[str]:
    if digest:
        return _chunk(render_digest(brief, report_url), limit)
    text = render_markdown(brief)
    text = re.sub(r"^#{1,2} ", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("`", "").replace("---\n", "")
    return _chunk(text, limit)
