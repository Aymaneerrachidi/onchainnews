"""Send the latest recap to an ad-hoc list of addresses.

Rebuilds the email from the published snapshot rather than re-scanning the
chains: the coins, peaks and volumes are exactly the ones already reported.
The written recap is regenerated, because the research findings live on the
candidate objects a run holds in memory and are not part of the snapshot.

    uv run python scripts/resend-latest.py a@b.com c@d.com
    uv run python scripts/resend-latest.py --archive a@b.com   # byte-identical
                                                               # resend, if an
                                                               # archive exists
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief.config import load_settings  # noqa: E402
from brief.delivery import send_email  # noqa: E402
from brief.models import (  # noqa: E402
    Brief,
    Candidate,
    Enrichment,
    SafetyReport,
    Scorecard,
    Signals,
    TokenSnapshot,
    XInteraction,
)
from brief.newsletter import recap_coins, research_day, write_recap  # noqa: E402
from brief.render.email import email_subject, render_email  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _build(cls, **overrides):
    """Construct a dataclass, defaulting anything the snapshot does not carry."""
    params = inspect.signature(cls.__init__).parameters
    kwargs = {
        name: (0.0 if param.default is inspect.Parameter.empty else param.default)
        for name, param in params.items()
        if name != "self"
    }
    kwargs.update({k: v for k, v in overrides.items() if k in params})
    return cls(**kwargs)


def _candidate(row: dict) -> Candidate:
    token = _build(
        TokenSnapshot,
        mint=row.get("mint") or "",
        symbol=row.get("symbol") or "?",
        name=row.get("name") or "",
        chain_id=row.get("chain") or "",
        pair_address=row.get("pairAddress") or "",
        url=row.get("url") or "",
        dex_id=row.get("dex") or "",
        socials=row.get("socials") or [],
        market_cap=float(row.get("marketCap") or 0),
        liquidity_usd=float(row.get("liquidity") or 0),
        volume_24h=float(row.get("volume24h") or 0),
        price_change_24h=float(row.get("priceChange24h") or 0),
        price_change_1h=float(row.get("change1h") or 0),
    )
    candidate = Candidate(
        token=token,
        signals=_build(Signals, age_hours=float(row.get("ageHours") or 0)),
        safety=_build(SafetyReport, holder_count=row.get("holders")),
        enrichment=_build(Enrichment),
    )
    candidate.peak_market_cap = float(row.get("peakMarketCap") or 0)
    candidate.observed_peak_market_cap = float(row.get("observedPeakMarketCap") or 0)
    candidate.drawdown_from_peak_pct = row.get("drawdownFromPeakPct")
    candidate.runner_tier = row.get("runnerTier") or ""
    candidate.round_trip = bool(row.get("roundTrip"))
    candidate.risk_labels = row.get("riskLabels") or []
    candidate.read = row.get("read") or ""
    candidate.track = row.get("track") or ""
    candidate.lore = row.get("lore") or ""
    candidate.kol_buyers = row.get("kolBuyers") or []
    candidate.kol_sellers = row.get("kolSellers") or []
    candidate.kol_holders = row.get("kolHolders") or []
    # Evidence is what makes the email worth reading, and the snapshot carries
    # all of it. Without restoring these, a resend rebuilds the numbers and
    # silently drops every story, quote and stated cause.
    candidate.news_evidence = list(row.get("newsEvidence") or [])
    candidate.provider_evidence = dict(row.get("providerEvidence") or {})
    for post in row.get("xInteractions") or []:
        try:
            candidate.x_interactions.append(XInteraction(
                author_handle=str(post.get("handle") or ""),
                author_name=str(post.get("author") or post.get("handle") or ""),
                interaction=str(post.get("interaction") or "post"),
                summary=str(post.get("summary") or ""),
                url=str(post.get("url") or ""),
                created_at=datetime.fromisoformat(str(post.get("createdAt"))),
                confidence=str(post.get("confidence") or "medium"),
                matched_on=str(post.get("matchedOn") or ""),
                like_count=int(post.get("likes") or 0),
                repost_count=int(post.get("reposts") or 0),
                reply_count=int(post.get("replies") or 0),
            ))
        except (TypeError, ValueError):
            continue
    return candidate


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipients", nargs="+", help="addresses to send to")
    parser.add_argument("--archive", action="store_true",
                        help="send the archived copy verbatim instead of rebuilding")
    args = parser.parse_args()

    settings = load_settings(str(ROOT / "config.toml"))

    archive_path = ROOT / str(settings.get("delivery", "email_archive_path", "output/email-sent.html"))
    if args.archive and archive_path.exists() and archive_path.stat().st_size < 5000:
        print(f"refusing: archive at {archive_path} is {archive_path.stat().st_size} bytes, not a real email")
        return 1
    if args.archive:
        if not archive_path.exists():
            print(f"no archived email at {archive_path}")
            return 1
        html = archive_path.read_text(encoding="utf-8")
        subject_path = archive_path.with_suffix(".subject.txt")
        if subject_path.exists():
            subject = subject_path.read_text(encoding="utf-8").strip()
        else:
            # Older archives carry no subject; rebuild it from the snapshot the
            # email was rendered from, which is the same formula the run used.
            snapshot = json.loads((ROOT / str(settings.get("run", "json_path"))).read_text(encoding="utf-8"))
            brief = Brief(
                generated_at=datetime.fromisoformat(snapshot["generatedAt"]),
                scorecard=_build(Scorecard),
                metas=[], new_and_moving=[], ctos=[], follow_ups=[],
                onchain=[], excluded=[], source_statuses=[],
                runners=[_candidate(row) for row in snapshot.get("runners") or []],
                headline_tape=[_candidate(row) for row in snapshot.get("headlineTape") or []],
            )
            subject = email_subject(brief, settings)
    else:
        snapshot = json.loads((ROOT / str(settings.get("run", "json_path"))).read_text(encoding="utf-8"))
        runners = [_candidate(row) for row in snapshot.get("runners") or []]
        tape = [_candidate(row) for row in snapshot.get("headlineTape") or []]
        if not runners:
            print("snapshot holds no runners")
            return 1
        generated = datetime.fromisoformat(snapshot["generatedAt"])
        pool = recap_coins(runners, tape, int(settings.get("newsletter", "max_coins", 30) or 30))
        found = await research_day(pool, settings)
        narrative = await write_recap(pool, generated, settings) or {}
        print(f"rebuilt from snapshot: {len(runners)} runners, {found} researched, "
              f"{len(narrative.get('sections', []))} written sections")
        brief = Brief(
            generated_at=generated,
            scorecard=_build(Scorecard),
            metas=[], new_and_moving=[], ctos=[], follow_ups=[],
            onchain=[], excluded=[], source_statuses=[],
            runners=runners,
            headline_tape=tape,
            narrative=narrative,
        )
        html = render_email(brief, settings)
        subject = email_subject(brief, settings)

    # The recipient list is the argument list, not config.
    settings.values.setdefault("delivery", {})["email_to"] = list(args.recipients)
    sent = await send_email(settings, subject, html)
    print(f"subject: {subject}")
    print(f"delivered to {sent} recipient(s): {', '.join(args.recipients)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
