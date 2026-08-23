"""Send the researched version of the most recently published recap.

This is deliberately snapshot-based: it does not rescan or replace the day's
runner set. It attaches the editorial audit to the exact contracts readers saw
in the last email, then delivers the same verdicts to email and Discord.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief.config import load_settings  # noqa: E402
from brief.delivery import send_email  # noqa: E402
from brief.links import fomo_token_url  # noqa: E402
from brief.models import Brief, Scorecard  # noqa: E402
from brief.render.discord import (  # noqa: E402
    BRAND,
    LOGO,
    LOGO_REF,
    post_payload,
    webhook_urls,
)
from brief.render.email import render_email  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]

# The established snapshot rehydrator predates package-style script names and
# intentionally lives at ``resend-latest.py``. Load that exact implementation
# instead of maintaining a second, subtly different Candidate builder here.
_resend_spec = importlib.util.spec_from_file_location(
    "fomo_resend_latest", ROOT / "scripts" / "resend-latest.py"
)
if _resend_spec is None or _resend_spec.loader is None:
    raise RuntimeError("could not load scripts/resend-latest.py")
_resend_module = importlib.util.module_from_spec(_resend_spec)
_resend_spec.loader.exec_module(_resend_module)
_build = _resend_module._build
_candidate = _resend_module._candidate

# Contract keyed so a copied ticker can never inherit another token's story.
EDITORIAL = {
    "ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg": {
        "group": "Verified catalysts",
        "cause": "The apparent GTA 6 footage, copyright strikes and a 27% supply burn turned the leak campaign into the day's dominant trade.",
        "caveat": "The token existed before the first leak and public ownership scanners disagree sharply on concentration.",
        "source": "https://www.pcgamer.com/games/grand-theft-auto/who-is-cyberleek-what-we-know-about-the-gta-6-leaker/",
    },
    "0xa66d60d6b308c6839e59aae0016e227ba5b08e30": {
        "group": "Verified catalysts",
        "cause": "A viral Chinese animated film, Binance Alpha attention and a KuCoin spot listing gave the move a real outside catalyst.",
        "caveat": "Live market-cap sources now disagree, so the deepest liquid pool is the only defensible price reference.",
        "source": "https://www.kucoin.com/announcement/en-niulai-listed-on-kucoin",
    },
    "E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump": {
        "group": "Verified catalysts",
        "cause": "Creator Capital is a working social-account market whose fees buy and burn CC; Gate Alpha listed the exact contract.",
        "caveat": "An independent launch audit found 22 coordinated wallets with heavy early exposure, although those wallets later exited.",
        "source": "https://creatorcapital.trade/",
    },
    "0x6ff45323817d1d53bbb8a8dfba9245ae74057777": {
        "group": "Verified catalysts",
        "cause": "The GME-onchain concept has real mechanics: eligible activity distributes GMEB automatically to MEMESTOCK holders.",
        "caveat": "The project explicitly has no GameStop affiliation; the story is community-built rather than corporate.",
        "source": "https://www.memestock.run/stock",
    },
    "0xe9bc5c6a86caa44fd7b469bf3cc7c563e4f77777": {
        "group": "Verified catalysts",
        "cause": "A 3% trade tax routes funds toward a Giggle Academy cause, and the project's dashboard reported 258.2 BNB delivered.",
        "caveat": "MAX is not affiliated with or endorsed by Giggle Academy; a transferred CZ balance is not evidence that CZ bought it.",
        "source": "https://www.maxbnb.meme/",
    },
    "GPzpoXpD74E2C4CJNayuoyBqPQJEsPtdse3nhntrpump": {
        "group": "Real, but caveated",
        "cause": "The established Looksmax community account adopted the token, while public call records caught a 16x-19x early move.",
        "caveat": "The creator has launched about 81 tokens, the dev still holds and bundled-wallet exposure remained elevated.",
        "source": "https://www.jointheswarm.co.uk/t/GPzpoXpD74E2C4CJNayuoyBqPQJEsPtdse3nhntrpump",
    },
    "6oGuFDbEeaSzTcvrmmd2MqfNYwHKXFoN7regcR22pump": {
        "group": "Real, but caveated",
        "cause": "A real TikTok and Fortnite-style meme revival, with the community claiming 158M views and 10,600 related videos.",
        "caveat": "It is not affiliated with Epic Games or Fortnite and was a revival, not a fresh launch-day story.",
        "source": "https://neegy.wtf/",
    },
    "0x7987f03462200b3d8a072e02c89a8a41dcb124ee": {
        "group": "Real, but caveated",
        "cause": "Programmable is a working Uniswap v4-hook product for launching assets and programmable markets.",
        "caveat": "The product is real, but no specific new catalyst explained this particular 24-hour rebound.",
        "source": "https://programmable.family/token/0x7987f03462200b3D8A072E02C89A8A41dCB124EE",
    },
    "nWnDYs4dR57Cek8nsK1W8iWEqKiKZE1hW1CLvP3pump": {
        "group": "Real, but caveated",
        "cause": "CLUSSY traces to a specific viral video and an active cult community rather than a fabricated ticker-only narrative.",
        "caveat": "Only four tracked KOL wallets appeared and the creator has launched several tokens.",
        "source": "https://www.clussyworldorder.fun/",
    },
    "EjAuFtMEP6LQDgLb11JU9bkcskhFYbgd9Q5NRXJppump": {
        "group": "Real, but caveated",
        "cause": "The Caesar or Doge 2 meme was picked up by a community takeover and multiple early call groups before crossing $1M.",
        "caveat": "The creator has launched roughly 15 tokens and the holder scan found 19 sniper wallets.",
        "source": "https://screenerbot.io/dna/token/EjAuFtMEP6LQDgLb11JU9bkcskhFYbgd9Q5NRXJppump",
    },
    "6yjNqPzTSanBWSa6dxVEgTjePXBrZ2FoHLDQwYwEsyM6": {
        "group": "Revivals",
        "cause": "An established Chudjak community returned to the tape; this was a revival of an old token, not a new launch.",
        "caveat": "No fresh catalyst was found and the largest named profitable wallets had already exited.",
        "source": "https://www.chudjaksolana.xyz/",
    },
    "0x16099f55662e02274a99bc73bea249c92bf8eb12": {
        "group": "Revivals",
        "cause": "A large number of tracked BNB-chain wallets traded the revival, but no new product or public catalyst surfaced.",
        "caveat": "The pair is roughly 319 days old and has no working project website linked from the market.",
        "source": "https://dexscreener.com/bsc/0x5703eb2618974485e2880384408B2E839DF68712",
    },
    "0x3a828e1ea6511e367d436c601e14cdb2b446896d": {
        "group": "Revivals",
        "cause": "A March 2025 BNB-chain pool revived with broad tracked-wallet participation and relatively little detected bundling.",
        "caveat": "No credible new event explained why the old token returned now.",
        "source": "https://whattofarm.io/pairs/bnb-chain-pancakeswapv-gatsby-wbnb-created-2025-03-18",
    },
    "0xf49725118cb0707b8706ffffe895f3ab16da7777": {
        "group": "Revivals",
        "cause": "The meme references b-money, the pre-Bitcoin system cited on page nine of Satoshi's whitepaper.",
        "caveat": "The historical reference is real, but no independent product or adoption was found.",
        "source": "https://bitcoin.org/bitcoin.pdf#page=9",
    },
    "4aSYV3VQRCPD8yBWwRwbSTLfq1s48UB2PAjCVEBdjups": {
        "group": "Weak context",
        "cause": "The token borrowed a line from a generic video about chasing 100x returns; it was a clip derivative, not a project.",
        "caveat": "Bundled-wallet exposure and sniper participation were both elevated.",
        "source": "https://youtu.be/mGLZRuqcYX0?t=63",
    },
    "7fmHqRpJLgpVsJFTvrv24CEjkz9c4o5uVX4Zdur1hz35": {
        "group": "Weak context",
        "cause": "Schrodinger had a live site and social account, but the move was driven mainly by market promotion.",
        "caveat": "Only three tracked KOL wallets appeared and no fresh outside catalyst was found.",
        "source": "http://schrodingersol.fun",
    },
    "0xe1ce50807dcfe16774b6cc38e1c315019e977777": {
        "group": "Weak context",
        "cause": "BAUD has a working site describing an AI-agent currency and proof-of-cognition experiment.",
        "caveat": "No independent adoption was found, bundler exposure was elevated and most of the move had already faded.",
        "source": "https://baud.cash/",
    },
    "0xcb74970b86b9abf3d75748eb2c3bff53e5cd7777": {
        "group": "Weak context",
        "cause": "Community accounts connected NIANNIAN to Giggle Academy's Master Cat lore.",
        "caveat": "No official Giggle Academy endorsement was found; boosting and bundled-wallet exposure were both high.",
        "source": "https://gigglecat.xyz/",
    },
    "EC1PdREWpiwfWnmRb5VzGihzYakYpGz6ErwTdbXQpump": {
        "group": "Weak context",
        "cause": "A TikTok brainrot character produced real attention, but most searchable coverage came from call-channel promotion.",
        "caveat": "Fresh-wallet exposure was roughly 24% and the developer still held tokens.",
        "source": "https://x.com/Pumparello",
    },
    "BnhSAHEfQU1VaczcnmiqioJG1xnU7tfhX91wCkCopump": {
        "group": "Weak context",
        "cause": "A viral character and video created a usable meme source, with several smart wallets joining the move.",
        "caveat": "Only four tracked KOL wallets appeared, so the outside validation remained thin.",
        "source": "https://dexscreener.com/solana/E3H4To39g5m31DyVUGqr7shi4m3DpXUVLngBStfam7E8",
    },
    "0xb2000000000000000000008b79a7be3f03091001": {
        "group": "Weak context",
        "cause": "APPLE JUICE repurposed Cobie's old 'rotating into apple juice' joke as a Base token narrative.",
        "caveat": "The Cobie post was from March 2025, roughly 17 months before this token launched; it was not a fresh call.",
        "source": "https://x.com/cobie/status/1897241620497715293",
    },
    "0xb1a11e73f7e0441f41683cc1caad6dd0a57139f4": {
        "group": "Rejects",
        "cause": "A derivative launched from the broader 牛来 issuer ecosystem rather than an official extension of the film.",
        "caveat": "Top ten controlled about 97%, fresh wallets about 97%, 117 snipers appeared and no credible KOL buying was found.",
        "source": "https://www.tokenpost.kr/amp/news/cryptocurrency/395689",
    },
    "Ab1sTFNv2tV5DX1XpriwNehXgiJhdq2RQ5LtD5BXpump": {
        "group": "Rejects",
        "cause": "DOPAMEME printed a large move without a matching outside story or established community catalyst.",
        "caveat": "Fresh wallets were about 53%, with 45 snipers, elevated bundling, developer holdings and only two tracked KOLs.",
        "source": "https://dexscreener.com/solana/FmGbfThLpryD6irj9xwtoS4j7rRGnayuUmgHUNCBvxTr",
    },
    "GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump": {
        "group": "Rejects",
        "cause": "The Gucci Morty meme spread organically, but that did not translate into a clean token launch.",
        "caveat": "The creator launched about 110 tokens; 30 snipers and roughly 24% bundled-wallet exposure were detected.",
        "source": "https://www.reddit.com/r/Mortytown/comments/1vwe6y9/gucci_birdperson/",
    },
    "DfMD3k7WHE81TjEKpxKsVHABZma3EyrDo9rtuueCpump": {
        "group": "Rejects",
        "cause": "CATURN borrowed attention from a TikTok source but had no stronger independent catalyst.",
        "caveat": "The creator launched about 112 tokens, 64 snipers appeared, top ten held roughly 27% and the dev still held.",
        "source": "https://dexscreener.com/solana/6MB7ZDcQpwx1abYyLsUgwKJ8DA64ixLKpG2WXcrEowdH",
    },
    "0x198dba421a7db566a90da5de7901abe3443b4444": {
        "group": "Rejects",
        "cause": "The token rode Chinese meme attention and a weak historical wallet connection rather than a fresh endorsement.",
        "caveat": "Nine zero-cost wallets received about 25% through pre-trading transfers; most named profitable wallets later exited.",
        "source": "https://www.unhosted.ai/predictions/binance/0x198dba421a7db566a90da5de7901abe3443b4444/%E5%AF%8C%E8%B4%B5",
    },
    "0x4dcdf3451dfc114991283c2e5b72823d69882ba3": {
        "group": "Rejects",
        "cause": "BOTS borrowed attention from the Freebots AI-agent project rather than establishing its own catalyst.",
        "caveat": "The token appeared unclaimed and no evidence showed that the product builder launched or endorsed it.",
        "source": "https://dexscreener.com/base/0x9150f6e5faf636ae28e0c70634598d7ce9d7fd3931f7c0f5d7f75faf6016086c",
    },
}

GROUP_ORDER = ("Verified catalysts", "Real, but caveated", "Revivals", "Weak context", "Rejects")
GROUP_COLORS = {
    "Verified catalysts": 0x14B878,
    "Real, but caveated": BRAND,
    "Revivals": 0x7B49F4,
    "Weak context": 0xB77A22,
    "Rejects": 0xD84A3A,
}

# The reader-facing memo deliberately excludes the six contracts the audit
# rejected. This mirrors a trader's recap: a handful of themes, one line per
# coin, and bullets only where the day's main story needs them.
SHORT_RECAP = (
    {
        "title": "GTA 6 Leaks",
        "coins": (
            ("ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg", "$1M to $20.4M (20x)", "website publishing apparent GTA 6 leaks"),
        ),
        "bullets": (
            "The footage drew copyright strikes and the leaker kept posting.",
            "About 27% of supply was burned; the $1M figure was market value, not cash.",
            "The token existed before the first leak, tying the campaign and token promotion together.",
        ),
    },
    {
        "title": "Chinese Film Meta",
        "coins": (
            ("0xa66d60d6b308c6839e59aae0016e227ba5b08e30", "hit $9.2M", "viral animated film that reached Binance Alpha and KuCoin"),
        ),
        "bullets": (
            "The film passed RMB 18M at the box office while the meme spread.",
            "This was a real outside catalyst, although live market-cap sources now disagree.",
        ),
    },
    {
        "title": "Onchain Products",
        "coins": (
            ("E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump", "hit $5.3M", "trade social media accounts the same way you trade coins"),
            ("0x7987f03462200b3d8a072e02c89a8a41dcb124ee", "hit $973K", "launch programmable markets with Uniswap v4 hooks"),
            ("0xe1ce50807dcfe16774b6cc38e1c315019e977777", "hit $669K", "AI-agent currency and proof-of-cognition experiment"),
        ),
        "bullets": (),
    },
    {
        "title": "Internet Memes",
        "coins": (
            ("0x6ff45323817d1d53bbb8a8dfba9245ae74057777", "hit $3.6M", "GME-onchain token distributing GMEB to holders"),
            ("0xe9bc5c6a86caa44fd7b469bf3cc7c563e4f77777", "hit $3.5M", "trade tax routed 258.2 BNB toward a Giggle Academy cause"),
            ("GPzpoXpD74E2C4CJNayuoyBqPQJEsPtdse3nhntrpump", "hit $1.9M", "looksmax.org community token caught by several early callers"),
            ("6oGuFDbEeaSzTcvrmmd2MqfNYwHKXFoN7regcR22pump", "hit $1.7M", "TikTok and Fortnite-style meme revival"),
            ("nWnDYs4dR57Cek8nsK1W8iWEqKiKZE1hW1CLvP3pump", "hit $764K", "cult meme traced to a specific viral video"),
            ("EjAuFtMEP6LQDgLb11JU9bkcskhFYbgd9Q5NRXJppump", "hit $383K", "Caesar or Doge 2 meme taken over by the community"),
        ),
        "bullets": (),
    },
    {
        "title": "Old Coins Back",
        "coins": (
            ("6yjNqPzTSanBWSa6dxVEgTjePXBrZ2FoHLDQwYwEsyM6", "hit $5.5M", "established Chudjak community returned to the tape"),
            ("0x16099f55662e02274a99bc73bea249c92bf8eb12", "hit $969K", "old BNB cat token revived across tracked wallets"),
            ("0x3a828e1ea6511e367d436c601e14cdb2b446896d", "hit $426K", "March 2025 BNB token came back without a clear new catalyst"),
            ("0xf49725118cb0707b8706ffffe895f3ab16da7777", "hit $1.7M", "b-money reference from page nine of the Bitcoin whitepaper"),
        ),
        "bullets": (),
    },
    {
        "title": "More Plays",
        "coins": (
            ("4aSYV3VQRCPD8yBWwRwbSTLfq1s48UB2PAjCVEBdjups", "hit $777K", "clip token based on a video about chasing 100x returns"),
            ("7fmHqRpJLgpVsJFTvrv24CEjkz9c4o5uVX4Zdur1hz35", "hit $756K", "Schrodinger meme with a live site but no fresh catalyst"),
            ("0xcb74970b86b9abf3d75748eb2c3bff53e5cd7777", "hit $459K", "community token around Giggle Academy's Master Cat lore"),
            ("EC1PdREWpiwfWnmRb5VzGihzYakYpGz6ErwTdbXQpump", "hit $354K", "TikTok brainrot character pushed mainly by call groups"),
            ("BnhSAHEfQU1VaczcnmiqioJG1xnU7tfhX91wCkCopump", "hit $339K", "viral character picked up by several smart wallets"),
            ("0xb2000000000000000000008b79a7be3f03091001", "hit $410K", "token built around an old Cobie apple juice joke"),
        ),
        "bullets": (),
    },
)


def peak(row: dict) -> float:
    return max(
        float(row.get("peakMarketCap") or 0),
        float(row.get("observedPeakMarketCap") or 0),
        float(row.get("marketCap") or 0),
    )


def money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def short(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."


def discord_payloads(rows: list[dict]) -> list[dict]:
    by_mint = {str(row.get("mint") or ""): row for row in rows}
    embeds = [{
        "color": BRAND,
        "author": {"name": "fomo onchain", "icon_url": LOGO_REF},
        "thumbnail": {"url": LOGO_REF},
        "title": "Daily Memecoin Recap - August 23",
        "description": "GTA 6 leaks led the day. Short version: what ran and why.",
    }]
    for section in SHORT_RECAP:
        lines = []
        for mint, result, line in section["coins"]:
            row = by_mint[mint]
            info = EDITORIAL[mint]
            lines.append(
                f"[**${row.get('symbol', '?')}**]({fomo_token_url(row.get('chain', ''), mint)}) "
                f"-> **{result}**, {line} ([source]({info['source']}))"
            )
        lines.extend(f"- {note}" for note in section["bullets"])
        embeds.append({
            "color": BRAND,
            "title": section["title"],
            "description": "\n".join(lines),
        })
    return [{"username": "fomo onchain", "embeds": embeds}]


async def main() -> int:
    settings = load_settings(str(ROOT / "config.toml"))
    snapshot = json.loads((ROOT / "web/data/latest.json").read_text(encoding="utf-8-sig"))
    rows = list(snapshot.get("runners") or [])
    missing = [row.get("mint") for row in rows if row.get("mint") not in EDITORIAL]
    if missing:
        raise RuntimeError(f"editorial audit missing {len(missing)} contracts: {missing}")

    all_candidates = [_candidate(row) for row in rows]
    for candidate in all_candidates:
        info = EDITORIAL[candidate.token.mint]
        candidate.provider_evidence.setdefault("why", {}).update({
            "cause": info["cause"],
            "sourceUrl": info["source"],
            "editorialStatus": info["group"],
        })
        candidate.news_evidence.insert(0, {
            "summary": info["cause"],
            "url": info["source"],
            "source": "Fomo Onchain research",
        })
        candidate.risk_labels = [info["caveat"]] + list(candidate.risk_labels or [])

    by_mint = {candidate.token.mint: candidate for candidate in all_candidates}
    selected_mints = [mint for section in SHORT_RECAP for mint, _, _ in section["coins"]]
    candidates = [by_mint[mint] for mint in selected_mints]
    narrative = {
        "layout": "short",
        "intro": "GTA 6 leaks led the day. Here is what ran and why.",
        "sections": [
            {
                "title": section["title"],
                "coins": [
                    {"symbol": by_mint[mint].token.symbol, "result": result, "line": line}
                    for mint, result, line in section["coins"]
                ],
                "bullets": list(section["bullets"]),
            }
            for section in SHORT_RECAP
        ],
    }
    generated = datetime.fromisoformat(snapshot["generatedAt"])
    brief = Brief(
        generated_at=generated,
        scorecard=_build(Scorecard),
        metas=[], new_and_moving=[], ctos=[], follow_ups=[], onchain=[],
        excluded=[], source_statuses=[], runners=candidates, headline_tape=[],
        narrative=narrative,
    )
    settings.values.setdefault("delivery", {})["newsletter_observed_limit"] = 30
    html = render_email(brief, settings)
    subject = f"Fomo Onchain | GTA 6 leaks led the day | {generated.strftime('%d %b')}"

    # Config intentionally contains only the approval inbox. Do not silently
    # widen a researched test send to an old client list.
    recipients = list(settings.get("delivery", "email_to", []) or [])
    if recipients != ["ue06prog@gmail.com"]:
        raise RuntimeError(f"refusing unexpected recipient list: {recipients}")
    sent = await send_email(settings, subject, html)

    webhooks = webhook_urls()
    if not webhooks:
        raise RuntimeError("email sent, but no Discord webhook is configured")
    posts = discord_payloads(rows)
    for webhook in webhooks:
        for index, payload in enumerate(posts):
            await post_payload(
                webhook,
                payload,
                ROOT / "web" / LOGO if index == 0 else None,
            )
    print(
        f"email={sent} recipient={recipients[0]} "
        f"discord={len(webhooks)}-webhooks/{len(posts)}-posts subject={subject}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
