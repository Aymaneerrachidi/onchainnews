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
from brief.newsletter import (  # noqa: E402
    newsletter_coin_limit,
    recap_coins,
    research_day,
    write_recap,
)
from brief.preflight import audit_brief  # noqa: E402
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
    risk_labels = [str(item) for item in (row.get("riskLabels") or [])]
    risk_text = " ".join(risk_labels).lower()
    mint_authority = row.get("mintAuthorityRenounced")
    freeze_authority = row.get("freezeAuthorityDisabled")
    # Snapshot schema v4 used ``false`` for both a measured live authority and
    # an unavailable provider answer.  The accompanying labels preserve the
    # distinction, so legacy snapshots can be rebuilt without turning an
    # unknown into a false rug verdict.
    if mint_authority is False and "mint authority/contract mintability not confirmed disabled" in risk_text:
        mint_authority = None
    if freeze_authority is False and "freeze/pause/blacklist powers not confirmed disabled" in risk_text:
        freeze_authority = None
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
        price_change_24h=float(row.get("change24h") or row.get("priceChange24h") or 0),
        price_change_1h=float(row.get("change1h") or 0),
    )
    candidate = Candidate(
        token=token,
        signals=_build(Signals, age_hours=float(row.get("ageHours") or 0)),
        safety=_build(
            SafetyReport,
            holder_count=row.get("holders"),
            top10_pct=row.get("top10Pct"),
            lp_locked_or_burned_pct=row.get("lpLockedPct"),
            mint_authority_renounced=mint_authority,
            freeze_authority_disabled=freeze_authority,
            risk_flags=row.get("securityFlags") or [],
            rugged=bool(row.get("rugged")),
            source=row.get("safetySource") or "unavailable",
        ),
        enrichment=_build(Enrichment),
    )
    candidate.peak_market_cap = float(row.get("peakMarketCap") or 0)
    candidate.observed_peak_market_cap = float(row.get("observedPeakMarketCap") or 0)
    candidate.start_market_cap = float(row.get("startMarketCap") or 0) or None
    candidate.run_multiple = float(row.get("runMultiple") or 1)
    candidate.peak_multiple = float(row.get("peakMultiple") or 0) or None
    candidate.drawdown_from_peak_pct = row.get("drawdownFromPeakPct")
    candidate.runner_tier = row.get("runnerTier") or ""
    candidate.round_trip = bool(row.get("roundTrip"))
    candidate.risk_labels = risk_labels
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
    candidate.scores = dict(row.get("scores") or {})
    candidate.score_confidence = dict(row.get("scoreConfidence") or {})
    candidate.score_components = dict(row.get("scoreComponents") or {})
    candidate.classification = str(row.get("classification") or "")
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
        pool = recap_coins(runners, tape, newsletter_coin_limit(settings))
        # The published snapshot already contains the narrative that was
        # validated against this exact runner set. Reusing it keeps a resend
        # faithful and avoids a second model call producing a different or
        # incomplete coin list. Older snapshots without narrative still use
        # the research/writer fallback below.
        narrative = snapshot.get("narrative") or {}
        if narrative.get("sections"):
            print(
                f"rebuilt from snapshot: {len(runners)} runners, "
                f"{len(narrative.get('sections', []))} preserved sections"
            )
        else:
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
            blocked_runners=[_candidate(row) for row in snapshot.get("blockedRunners") or []],
            headline_tape=tape,
            narrative=narrative,
            recap=snapshot.get("recap") or {},
        )
        html = render_email(brief, settings)
        subject = email_subject(brief, settings)

    if args.archive:
        print("refusing: archived HTML cannot prove the current exact-contract KOL and safety checks")
        return 1

    audit = audit_brief(brief, settings)
    print(f"preflight passed: {audit.candidate_count} exact contracts")

    # The recipient list is the argument list, not config.
    settings.values.setdefault("delivery", {})["email_to"] = list(args.recipients)
    sent = await send_email(settings, subject, html, audit=audit)
    print(f"subject: {subject}")
    print(f"delivered to {sent} recipient(s): {', '.join(args.recipients)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
