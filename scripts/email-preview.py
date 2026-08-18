from __future__ import annotations

"""Render the branded email so the full design can be judged in one read.

Default behaviour builds a rich, hand-made sample Brief -- eight runners,
seven picks, disqualified coins, KOL/lore chips and a filled scorecard -- so
every part of the template is visible. --fixture falls back to building a
real Brief through the engine from tests/fixtures/run.json (which is sparse:
exactly one coin).

Writes output/email-preview.html (+ a -cta variant with the footer button).
--send delivers the rendered email straight to config.toml's delivery.email_to
via Resend, so the design can be checked in a real inbox without paying for a
full live run. output/*.html is gitignored.

Usage:
    uv run python scripts/email-preview.py
    uv run python scripts/email-preview.py --send
    uv run python scripts/email-preview.py --fixture
"""

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief.engine import build_brief  # noqa: E402
from brief.ledger import Ledger  # noqa: E402
from brief.models import (  # noqa: E402
    Brief,
    Candidate,
    Enrichment,
    OnChainFinding,
    SafetyReport,
    Scorecard,
    Signals,
    TokenSnapshot,
    TransactionWindow,
)
from brief.render.email import render_email  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 18, 6, 45, tzinfo=ZoneInfo("Europe/Paris"))


def _candidate(
    symbol: str,
    name: str,
    track: str,
    run_multiple: float,
    chg24: float,
    chg1: float,
    mcap: float,
    vol: float,
    age: float,
    turnover: float,
    buys6: float,
    holders: int,
    read: str,
    *,
    lore: str = "",
    kol: int = 0,
    chain: str = "solana",
    risks: list[str] | None = None,
) -> Candidate:
    """A believable runner built purely to feed the email renderer."""
    mint = f"BOLT{symbol}SOLMINTAAAAAAAAAAAAAAAAAAAAAAAAAA"
    token = TokenSnapshot(
        mint=mint[:44],
        symbol=symbol,
        name=name,
        chain_id=chain,
        pair_address=mint[:20].lower() + "p",
        url=f"https://dexscreener.com/{chain}/{mint[:30]}",
        price_usd=mcap / 1_000_000_000,
        market_cap=mcap,
        liquidity_usd=max(vol * 0.4, 35000),
        volume_24h=vol,
        volume_6h=vol * 0.62,
        price_change_24h=chg24,
        price_change_6h=chg24 * 0.72,
        pair_created_at=NOW - timedelta(hours=age),
        price_change_1h=chg1,
        price_change_5m=chg1 * 0.3,
        txns_1h=TransactionWindow(buys=int(vol / 220), sells=int(vol / 420)),
        txns_6h=TransactionWindow(buys=int(vol / 30), sells=int(vol / 60)),
        txns_24h=TransactionWindow(buys=int(vol / 4), sells=int(vol / 7)),
        volume_1h=vol / 12,
        socials=[{"type": "twitter", "url": "https://x.com/onchainnews"}],
        active_boosts=0,
        from_profile=False,
        meta_slugs={"solana"},
    )

    signals = Signals(
        turnover=turnover,
        acceleration=turnover,
        buy_imbalance_1h=buys6 * 0.9,
        buy_imbalance_6h=buys6,
        liquidity_depth=mcap / max(vol, 1),
        holder_growth_24h=int(holders * 0.05),
        maker_quality=140.0,
        age_hours=age,
    )
    safety = SafetyReport(
        mint=token.mint,
        mint_authority_renounced=True,
        freeze_authority_disabled=True,
        lp_locked_or_burned_pct=100.0,
        top10_pct=16.0,
        risk_flags=[],
        holder_count=holders,
    )
    enrichment = Enrichment(
        holder_count=holders,
        holder_change_24h=int(holders * 0.04),
        unique_makers_24h=min(int(vol / 900), 9999),
        mint_authority_renounced=True,
        freeze_authority_disabled=True,
    )
    return Candidate(
        token=token,
        signals=signals,
        safety=safety,
        enrichment=enrichment,
        track=track,
        run_multiple=run_multiple,
        risk_labels=risks or [],
        lore=lore,
        kol_buyers=[f"L{chr(65 + i)}" for i in range(kol)] if kol else [],
        read=read,
        dex_evidence=[],
        x_interactions=[],
    )

def _sample_brief() -> Brief:
    """A full-looking report so the design is judged on a full page."""
    new = [
        _candidate("BOLT", "Boltz", "NEW", 6.1, 512, 38, 6_800_000, 2_140_000, 9, 3.1, 0.72, 4800,
                   "$BOLT went 4.1x in nine hours to a $6.8M cap on $2.1M of volume and is still printing +38% in the last hour.",
                   lore="dark launch", kol=3),
        _candidate("PINGU", "penguin posse", "new", 2.8, 188, 12, 2_300_000, 640_000, 20, 1.6, 0.62, 2100,
                   "$PINGU joined the penguin meta last night and ground from $700k to $2.3M with a buy-side that never went vertical.",
                   lore="penguin meta", kol=2),
        _candidate("MOCHI", "mochi", "new", 1.9, 61, 6, 1_400_000, 310_000, 14, 1.2, 0.55, 1300,
                   "$MOCHI stayed under most radars, +67% off a 14-hour base with 1,300 holders and no team wallet in the top ten."),
    ]
    movers = [
        _candidate("DIGE", "doge", "mover", 3.3, 72, 15, 4_400_000, 980_000, 22, 2.0, 0.66, 3400,
                   "$DIGE rekindled the dogs meta today, +72% to $4.4M with tracked wallets among the earlier buyers.",
                   lore="dogs meta", kol=2),
        _candidate("CATTO", "catto", "mover", 2.0, 44, 4, 2_100_000, 480_000, 60, 1.4, 0.58, 1900,
                   "$CATTO crossed the day on quiet tape, +44% with every checkpoint it normally trips on passed."),
        _candidate("BASED", "based", "mover", 2.4, 96, -11, 1_700_000, 520_000, 11, 1.9, 0.51, 0,
                   "$BASED ran 2.4x on Base before giving back 11% in the last hour.",
                   chain="base", risks=["fading, down 11% in the last hour",
                                        "concentration unknown", "LP lock unknown"]),
        _candidate("NIU", "niu niu", "mover", 5.4, 320, 4, 2_000_000, 900_000, 27, 2.2, 0.6, 0,
                   "$NIU is the day's biggest run off Solana, 5.4x on BNB Chain.",
                   chain="bsc", risks=["concentration unknown", "LP lock unknown"]),
    ]
    ctos = [
        _candidate("FLOPPY", "floppy", "cto", 1.8, 54, 9, 980_000, 260_000, 30, 1.8, 0.70, 940,
                   "$FLOPPY got taken over today, +54% from the claim open with the reserve address signalled away.",
                   lore="old token"),
    ]
    blocked = [
        _candidate("SHITH", "shi", "new", 2.8, 61, 5, 900_000, 3_900_000, 8, 22.0, 0.31, 100,
                   "A coin can move without a market behind it.",
                   risks=["live mint authority", "volume 26x pool depth", "under 200 holders"]),
        _candidate("PANDO", "pandzoo", "new", 3.0, 82, -6, 1_200_000, 700_000, 10, 5.0, 0.30, 160,
                   "A coin with no sellers is a coin you cannot sell.",
                   risks=["top 10 hold 55%", "tape is 41 wallets"]),
    ]
    return Brief(
        generated_at=NOW,
        window_start=NOW - timedelta(hours=24),
        scorecard=Scorecard(
            featured_count=6,
            featured_q1_72h=0.11,
            featured_median_72h=0.32,
            featured_q3_72h=0.68,
            featured_up_pct_72h=0.72,
            featured_crash_pct_72h=0.12,
        ),
        metas=[],
        new_and_moving=new,
        movers=movers,
        ctos=ctos,
        follow_ups=[],
        onchain=[OnChainFinding(mint="", symbol="", priority=0, headline="")],
        excluded=[],
        source_statuses=[],
        runners=[*new, *movers, *ctos],
        blocked_runners=[*blocked],
    )


async def load_brief(args: argparse.Namespace) -> Brief:
    """Sample by default; --fixture runs the real engine offline instead."""
    if not args.fixture:
        return _sample_brief()
    sys.path.append(str(SCRIPT / "tests"))
    from conftest import build_settings  # noqa: E402

    with TemporaryDirectory() as tmp:
        settings = build_settings(Path(tmp))
        ledger = Ledger(settings.path("run", "database_path"))
        try:
            return await build_brief(
                settings,
                ledger,
                commit=False,
                now=datetime(2026, 8, 6, 6, 45, tzinfo=ZoneInfo("Europe/Paris")),
            )
        finally:
            ledger.close()

async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-url", default="", help="set the footer CTA link")
    parser.add_argument("--fixture", action="store_true",
                        help="build from the engine fixture instead of the rich sample")
    parser.add_argument("--send", action="store_true",
                        help="deliver the rendered sample to delivery.email_to via Resend")
    parser.add_argument("--subject-prefix", default="",
                        help="override the email subject prefix so the sample thread is unmistakable")
    args = parser.parse_args()
    brief = await load_brief(args)

    if args.send:
        # Delivery reads the live config: real recipients, real sender, real
        # report URL. Only the brief stays sample-generated and offline.
        from brief.config import load_settings  # noqa: E402
        from brief.delivery import EmailDeliveryError, send_email  # noqa: E402
        from brief.render.email import email_subject  # noqa: E402

        real = load_settings(SCRIPT / "config.toml")
        if args.subject_prefix:
            real.values.setdefault("delivery", {})["email_subject_prefix"] = args.subject_prefix
        subject = email_subject(brief, real)
        body = render_email(brief, real)
        try:
            count = await send_email(real, subject, body)
        except EmailDeliveryError as exc:
            print(f"Email delivery failed: {exc}")
            raise SystemExit(1)
        print(f"Email digest delivered ({count} recipient(s)).")
        return

    output = SCRIPT / "output" / "email-preview.html"
    forbidden = ("<style", "class=", "<script", "<iframe", "<object", "<details")

    from brief.config import Settings  # noqa: E402

    plain = Settings(root=SCRIPT, values={"delivery": {}})
    body_without_cta = render_email(brief, plain)
    output.write_text(body_without_cta, encoding="utf-8")
    print(f"wrote {output}  ({len(body_without_cta):,} chars)")

    if args.report_url:
        cta = Settings(root=SCRIPT, values={"delivery": {"report_url": args.report_url}})
        body_with_cta = render_email(brief, cta)
        extra = output.with_name("email-preview-cta.html")
        extra.write_text(body_with_cta, encoding="utf-8")
        print(f"wrote {extra}  ({len(body_with_cta):,} chars)")

    leaks = [tok for tok in forbidden if tok in body_without_cta]
    print("flat-and-inline check:", "OK" if not leaks else f"LEAKS {leaks}")


if __name__ == "__main__":
    asyncio.run(main())