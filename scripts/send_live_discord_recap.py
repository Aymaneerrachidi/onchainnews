"""Research, deduplicate, and send the latest snapshot as a concise Discord recap.

This does not rescan markets. It works from ``web/data/latest.json``, excludes
the permanent manual deny-list, enriches the remaining exact contracts, and
refuses delivery unless every contract appears exactly once in the final copy.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief.config import load_settings  # noqa: E402
from brief.journal import kol_trade_count, rug_or_bundle, verified_window_multiple  # noqa: E402
from brief.links import fomo_token_url  # noqa: E402
from brief.lore import attach_lore  # noqa: E402
from brief.newsletter import explain_runs, research_day, write_recap  # noqa: E402
from brief.render.discord import (  # noqa: E402
    BRAND,
    LOGO,
    LOGO_REF,
    bot_channel_ids,
    bot_token,
    interactive_market_components,
    post_bot_payload,
    post_payload,
    webhook_urls,
)
from brief.render.email import peak_cap  # noqa: E402
from brief.render.formatting import money  # noqa: E402
from brief.sources.openintel import HYPE, PROMO, WALLET_TRACKER  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

_resend_spec = importlib.util.spec_from_file_location(
    "fomo_resend_latest", ROOT / "scripts" / "resend-latest.py"
)
if _resend_spec is None or _resend_spec.loader is None:
    raise RuntimeError("could not load scripts/resend-latest.py")
_resend = importlib.util.module_from_spec(_resend_spec)
_resend_spec.loader.exec_module(_resend)
_candidate = _resend._candidate

MAX_DESCRIPTION = 3800
MAX_MESSAGE_EMBED_CHARS = 5800
MAX_MESSAGE_EMBEDS = 10
SPACE = re.compile(r"\s+")

# The five stories with enough attributable context to deserve more than a
# tape line.  Every other eligible contract is intentionally rendered as one
# sentence: result first, lore second.
FEATURE_STORIES: dict[str, dict[str, object]] = {
    "ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg": {
        "title": "GTA 6 Leaks",
        "summary": "the apparent GTA 6 build leaks became the day's dominant outside story",
        "bullets": [
            "Fresh clips and copyright takedowns kept CyberLeek in mainstream gaming news.",
            "The leaker burned roughly 27% of supply while continuing to use the token inside the campaign.",
        ],
        "source": "https://www.pcgamer.com/games/grand-theft-auto/who-is-cyberleek-what-we-know-about-the-gta-6-leaker/",
    },
    "CvjSaRcTmcrfutekYzrBEEdTWx1RmRTWDToqtXmCpump": {
        "title": "La Peace Takes Over TikTok",
        "summary": "Kai Cenat pronouncing Minecraft 'lapis' as 'La Peace' became a fast-moving TikTok and Reels meme",
        "bullets": [
            "The clip came from Kai and IShowSpeed's hardcore Minecraft stream after Speed mistook lapis for diamonds.",
            "TikTok and Reels edits recast Kai as a peaceful monk or philosopher dispensing ancient wisdom.",
        ],
        "source": "https://knowyourmeme.com/editorials/guides/what-is-the-la-peace-meme-the-viral-kai-cenat-and-ishowspeed-minecraft-memes-explained",
    },
    "GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump": {
        "title": "Gucci Morty",
        "summary": "designer-dressed Morty edits became the new short-form brainrot character",
        "bullets": [
            "The joke puts Morty in Gucci fits, bags and chains, replacing his usual anxious look with exaggerated swagger.",
            "Edits spread across TikTok, Instagram, YouTube Shorts and X during the token's run.",
        ],
        "source": "https://pump.fun/coin/GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump/article",
    },
    "0x39dbed3a2bd333467115de45665cc57f813c4571": {
        "title": "Pons Launchpad",
        "summary": "the main launchpad token for fixed-supply coins on Robinhood Chain",
        "bullets": [
            "Pons launches tokens directly into locked on-chain pools and routes protocol activity into PONS economics.",
            "The launchpad logged 1,476 launches and 525,000 PONS burned on August 23.",
        ],
        "source": "https://ponsinomics.com/",
    },
    "E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump": {
        "title": "Creator Capital",
        "summary": "a live market for trading the value of social-media accounts",
        "bullets": [
            "Users create and trade account markets on Solana instead of launching ordinary ticker memes.",
            "The protocol says each trade sends 1.5% to buy CC on-chain and burn it.",
        ],
        "source": "https://creatorcapital.trade/",
    },
}

# Contract-keyed findings confirmed during the editorial pass. These override
# thinner model summaries, never identity or measured market data.
EDITORIAL: dict[str, tuple[str, str]] = {
    "Ab1sTFNv2tV5DX1XpriwNehXgiJhdq2RQ5LtD5BXpump": (
        "dopamine-plus-meme wordplay about the rush of posting a meme that hits",
        "https://www.urbandictionary.com/define.php?term=Dopameme",
    ),
    "0x3efbfff95576e1d23cf6ead0acd2e73f4d6a7777": (
        "the self-styled official Cat of Binance, built as the BNB side of the cross-chain cat rotation",
        "http://binance-cat.com",
    ),
    "0xac77646bcff9d52e99800534192e0290933f4094": (
        "a Robinhood Chain Martians-versus-SpaceX meme launched through Pons",
        "https://dexscreener.com/robinhood/0xac77646bcff9d52e99800534192e0290933f4094",
    ),
    "C3bajJW843KN9Uu441JkXN7zVMs4VM2HvdAGyGiBpump": (
        "Anton Palkin's six-agent Grok trading desk; token fees were routed toward the public on-chain bot fund",
        "https://x.com/antpalkin/status/2091522720445927590",
    ),
    "0x133698a17b7b5c2b981555c56c2b00824f517c1b": (
        "a Robinhood Chain parody of founder Vlad Tenev, paired directly against INDEX",
        "https://dexpaprika.com/robinhood/pool/0x27bc64e7b46ecb6d0ff0db6abc5e7666b93f9262eb6d387719c4f4798f68d041",
    ),
    "ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg": (
        "apparent GTA 6 build leaks became the day's dominant story; copyright strikes and new clips kept it alive",
        "https://www.pcgamer.com/games/grand-theft-auto/who-is-cyberleek-what-we-know-about-the-gta-6-leaker/",
    ),
    "E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump": (
        "live market for trading social accounts, with protocol fees used to buy and burn CC",
        "https://creatorcapital.trade/",
    ),
    "0x39dbed3a2bd333467115de45665cc57f813c4571": (
        "main launchpad token for fixed-supply Robinhood Chain coins",
        "https://ponsfamily.com/launchpad",
    ),
    "GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump": (
        "Gucci Morty edits spread across TikTok, Instagram, Shorts and X",
        "https://pump.fun/coin/GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump/article",
    ),
    "GPzpoXpD74E2C4CJNayuoyBqPQJEsPtdse3nhntrpump": (
        "the token adopted by Looksmax.org, the men's aesthetics and self-improvement forum",
        "https://www.kcex.com/support/articles/43234799830942",
    ),
    "6oGuFDbEeaSzTcvrmmd2MqfNYwHKXFoN7regcR22pump": (
        "a long-nosed yellow TikTok character revival that the project tracks across thousands of videos",
        "https://neegy.wtf/",
    ),
    "0xe9bc5c6a86caa44fd7b469bf3cc7c563e4f77777": (
        "a 3% trade tax routes funds toward a Giggle Academy cause",
        "https://www.maxbnb.meme/",
    ),
    "0x6ff45323817d1d53bbb8a8dfba9245ae74057777": (
        "a GME-onchain token paired to GMEB that distributes GameStop exposure to eligible holders",
        "https://memestock.run/",
    ),
    "0x03c59bbf8ba49ce79831403b86acbc40d3167777": (
        "a Chinese wordplay meme around 'you've got quite the nerve,' using chubby imagery for 胆子肥",
        "https://calibertoken.com/token/bsc/0x03c59bbf8ba49ce79831403b86acbc40d3167777",
    ),
    "0xdbc6333a7d8bcd95f96641eda4d095e69f207777": (
        "the Binance Kitten: half black, half gold, built as BNB's side of the cat rotation",
        "https://bicatonbnb.life",
    ),
    "0xb1a03eda10342529bbf8eb700a06c60441fef25d": (
        "the Base-native Mr. Miggles cat community, now wrapped around education and creator initiatives",
        "https://www.miggles.io/",
    ),
    "6yjNqPzTSanBWSa6dxVEgTjePXBrZ2FoHLDQwYwEsyM6": (
        "the established Chudjak community returned without a new outside catalyst",
        "https://www.chudjaksolana.xyz/",
    ),
    "7VENy6wCjBChAMGpjPQCPYJDeYJGFr5NZUxk7uQ7bonk": (
        "the older capybara community revived, with broad tracked-wallet participation",
        "https://thecapytoken.com/",
    ),
    "4aSYV3VQRCPD8yBWwRwbSTLfq1s48UB2PAjCVEBdjups": (
        "a clip coin built around a video about chasing 100x returns",
        "https://youtu.be/mGLZRuqcYX0?t=63",
    ),
    "7fmHqRpJLgpVsJFTvrv24CEjkz9c4o5uVX4Zdur1hz35": (
        "a Schrödinger's-cat wordplay meme: Dinger is both alive and dead until the chart is opened",
        "http://schrodingersol.fun/",
    ),
    "0x16099f55662e02274a99bc73bea249c92bf8eb12": (
        "an older BNB cat revived around community claims that a CZ wallet bought and retained it",
        "https://web3.bitget.com/en/swap/bnb/0x16099F55662e02274a99Bc73bEa249c92BF8eb12",
    ),
    "0x56910d4409f3a0c78c64dd8d0545ff0705389870": (
        "a 3% trade-tax index that buys tokenized stocks on Robinhood Chain and distributes them to holders",
        "https://theindex.finance/",
    ),
    "0xb2000000000000000000004c27f6523082f41d01": (
        "the Base chain cat community, built around helmet PFPs and the community Crew Wall",
        "https://basecatonbase.com/",
    ),
    "0x5d68119ac1dc4bbe5c7f1c67d5d4c4410e2b7777": (
        "a BITMINE-name meme with no verified tie to the public company or an attributable project story",
        "https://dexscreener.com/bsc/0x5d68119ac1dc4bbe5c7f1c67d5d4c4410e2b7777",
    ),
    "0xa8b3dfcd90945b6393482394fa82bafa423b7777": (
        "a community coin built around CZ's 2023 #BinanceCat post, with no official Binance affiliation",
        "https://x.com/cz_binance/status/1691760768679629225",
    ),
    "0x28c74b28429df12d1f39f244f5c72fd472847a6b": (
        "a bald-head Base meme launched through BaseStonk; no separate product or outside catalyst surfaced",
        "https://x.com/BALDonBaseStonk",
    ),
    "0xf49725118cb0707b8706ffffe895f3ab16da7777": (
        "a meme reference to Wei Dai's b-money, cited on page nine of the Bitcoin whitepaper",
        "https://bitcoin.org/bitcoin.pdf#page=9",
    ),
    "CvjSaRcTmcrfutekYzrBEEdTWx1RmRTWDToqtXmCpump": (
        "Kai Cenat pronouncing Minecraft 'lapis' as 'La Peace' became a TikTok and Reels monk meme",
        "https://knowyourmeme.com/editorials/guides/what-is-the-la-peace-meme-the-viral-kai-cenat-and-ishowspeed-minecraft-memes-explained",
    ),
    "0xf1e9baa65d418a9025e1851dd2d37f1ad208bba3": (
        "a community-takeover meme launched on Uniswap v4 around the line 'we're rebuilding the internet'",
        "https://bitmart.zendesk.com/hc/en-us/articles/50809703491739-Ratspeak-RATSPEAK",
    ),
    "0xc2362aff2a2a4cc1f48cf3dab2c4e2605eb94ba3": (
        "a GameStop meme on Robinhood Chain whose site frames network fees as the answer to the 2021 buy-button halt",
        "https://gme.meme/",
    ),
    "0xb200000000000000000000046390aed221043f01": (
        "a community-run Base coin built around the shared blue-bottle mascot and its Juice Rush game",
        "https://basejuiceonbase.com/",
    ),
    "0x18e674231a58c239dc7daedcffe15ec3a24cff5c": (
        "a launchpad for programmable Uniswap v4 hook markets on Robinhood Chain",
        "https://hookr.fun/",
    ),
    "nWnDYs4dR57Cek8nsK1W8iWEqKiKZE1hW1CLvP3pump": (
        "a revival of the clown.mp4 / 'clussy' meme, whose clown-plus-pussy wordplay dates to 2017",
        "https://www.clussyworldorder.fun/",
    ),
    "EjAuFtMEP6LQDgLb11JU9bkcskhFYbgd9Q5NRXJppump": (
        "Caesar, the dog photo nicknamed Doge 2 because its expression resembles the original Doge",
        "https://knowyourmeme.com/sensitive/memes/doge-2-caesar",
    ),
    "0x0c5142bc58f9a61ab8c3d2085dd2f4e550c5ce0b": (
        "the Base meme that accelerated after Elon Musk replied with a fire emoji",
        "https://www.kucoin.com/news/flash/elon-musk-reacts-to-base-ecosystem-meme-coin-russell-token-surges-3x-before-retreating",
    ),
    "0xb2000000000000000000007bf6d5cbb0e24cb301": (
        "a Brian Armstrong / Coinbase-themed Base meme; it is not an official Coinbase product",
        "https://www.coinbase.com/en-es/price/coinbase-man-base-0xb2000000000000000000007bf6d5cbb0e24cb301",
    ),
    "0xfb1bcb6817f7b8fa86896d3253b16aeb7bd6a5a8": (
        "a sword-and-warrior meme built around the line 'one life, it's worth an attempt'",
        "https://worthonrh.com/",
    ),
    "0x0145acbccefbed6f303c420beeaaac72e905430b": (
        "ten permanently locked Uniswap v4 pools pairing PACK with six tokenized stocks and four memes",
        "https://2wolves.xyz/",
    ),
    "0x198dba421a7db566a90da5de7901abe3443b4444": (
        "a 'wish granted' wealth meme revived after the Pons founder revisited the small bag he bought before Pons succeeded",
        "https://www.sotwe.com/gatealphahq?lang=en",
    ),
    "0x20024e485c0b22b42855589700721b28320a7777": (
        "a marketplace for tokenized real-world assets with issuer verification and on-chain provenance",
        "https://prismassets.shop/",
    ),
    "0x0f61edbfe6cd86024c0f210c0695b08df55fdfc9": (
        "a Base-native token launchpad supporting equity pairs, token pairs and custom Uniswap v4 hooks",
        "https://basestonk.io/",
    ),
    "6fEaYuzirTMXFnFo7dGKHJs8wWVFPdh1bfZL9oRPpump": (
        "the Tiny Jesus devotional meme, built around hiding and finding a little Jesus figure",
        "https://jesus.lovesyou.fun/",
    ),
    "0x3a828e1ea6511e367d436c601e14cdb2b446896d": (
        "an older BNB meme linked to the Gatsby Bali Corgi account; no fresh outside catalyst was verified",
        "https://www.instagram.com/balicorgi/",
    ),
}

# Intentionally written tape copy for today's sub-$1M runners. These are not
# machine-truncated versions of the lead stories, so they always end cleanly.
TAIL_EDITORIAL: dict[str, str] = {
    "0x20024e485c0b22b42855589700721b28320a7777": "tokenized real-world asset marketplace",
    "0x28c74b28429df12d1f39f244f5c72fd472847a6b": "BaseStonk's bald-head meme",
    "0x5d68119ac1dc4bbe5c7f1c67d5d4c4410e2b7777": "BITMINE-name meme; no company tie verified",
    "nWnDYs4dR57Cek8nsK1W8iWEqKiKZE1hW1CLvP3pump": "clown.mp4 / 'clussy' meme revival",
    "4aSYV3VQRCPD8yBWwRwbSTLfq1s48UB2PAjCVEBdjups": "clip coin about chasing 100x returns",
    "7fmHqRpJLgpVsJFTvrv24CEjkz9c4o5uVX4Zdur1hz35": "Schrödinger's-cat wordplay meme",
    "0xa8b3dfcd90945b6393482394fa82bafa423b7777": "coin built around CZ's 2023 #BinanceCat post",
    "0x198dba421a7db566a90da5de7901abe3443b4444": "'wish granted' wealth meme linked to the Pons founder",
    "0x16099f55662e02274a99bc73bea249c92bf8eb12": "old BNB cat revived by claimed CZ-wallet history",
    "CvjSaRcTmcrfutekYzrBEEdTWx1RmRTWDToqtXmCpump": "Kai Cenat's viral 'lapis' / La Peace TikTok meme",
    "0x3a828e1ea6511e367d436c601e14cdb2b446896d": "old BNB meme linked to the Gatsby Bali Corgi",
    "EjAuFtMEP6LQDgLb11JU9bkcskhFYbgd9Q5NRXJppump": "Caesar, the dog photo nicknamed Doge 2",
    "6fEaYuzirTMXFnFo7dGKHJs8wWVFPdh1bfZL9oRPpump": "Tiny Jesus devotional meme",
}


def _compact(value: object, limit: int = 180) -> str:
    text = SPACE.sub(" ", str(value or "")).strip(" ,-–—")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _is_promo(text: str) -> bool:
    lowered = text.lower()
    return bool(
        PROMO.search(text) or HYPE.search(text) or WALLET_TRACKER.search(text)
        or "alpha telegram" in lowered or "massive profits" in lowered
    )


def _clean_existing_evidence(candidate) -> None:
    """Drop call-channel copy before web research and narrative writing."""
    why = candidate.provider_evidence.get("why", {}) or {}
    if _is_promo(str(why.get("cause") or "")):
        candidate.provider_evidence.pop("why", None)
    candidate.news_evidence = [
        item for item in candidate.news_evidence
        if not _is_promo(str(item.get("summary") or ""))
    ]


def _source(candidate) -> str:
    why = candidate.provider_evidence.get("why", {}) or {}
    source = str(why.get("sourceUrl") or "").strip()
    if source:
        return source
    for item in reversed(candidate.news_evidence or []):
        url = str(item.get("url") or "").strip()
        if url:
            return url
    return ""


def _fallback_line(candidate) -> str:
    name = _compact(candidate.token.name, 70)
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    cto = bool(gmgn.get("ctoFlag"))
    if cto:
        return f"{name or candidate.token.symbol} community takeover; no fresh public catalyst was verified"
    if name and name.lower() != candidate.token.symbol.lower():
        return f"{name}; no fresh public catalyst was verified"
    return "no fresh public catalyst was verified"


def _result(candidate) -> str:
    """The measured move followed by the latest MC captured for delivery."""
    peak = _verified_peak(candidate)
    current = float(candidate.token.market_cap or 0)
    measured_drawdown = ((peak - current) / peak) * 100.0 if peak > current else 0.0
    drawdown = max(
        float(candidate.drawdown_from_peak_pct or candidate.faded_from_peak or 0),
        measured_drawdown,
    )
    start = float(candidate.start_market_cap or 0)
    multiple = peak / start if start > 0 else float(candidate.peak_multiple or candidate.run_multiple or 0)
    change = float(candidate.token.price_change_24h or 0)
    if start > 0 and multiple >= 2:
        move = f"{money(start)} to {money(peak)} ({multiple:.1f}x)"
    elif drawdown >= 35:
        move = f"hit {money(peak)}, then faded {drawdown:.0f}%"
    elif change >= 25:
        move = f"+{change:.0f}% to {money(peak)}"
    else:
        move = f"hit {money(peak)}"
    current_text = money(current) if current > 0 else "unavailable"
    return f"{move} · now {current_text}"


def _lore_limit(candidate) -> int:
    """Leads get context; the sub-$1M tail reads like a compact tape."""
    return 96 if peak_cap(candidate) >= 1_000_000 else 58


def _verified_peak(candidate) -> float:
    """Highest peak verified inside the report window.

    GMGN's lifetime ATH is useful background, but it must not qualify a coin
    for a daily recap: stale or bad lifetime prints can otherwise turn a
    sub-$1M tape entry into a fake billion-dollar runner.
    """
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    return max(
        peak_cap(candidate),
        float(gmgn.get("kline24hPeakMarketCap") or 0),
    )


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


async def _refresh_current_market_caps(
    candidates: list,
    *,
    base_url: str = "https://api.dexscreener.com",
    transport=None,
) -> int:
    """Reprice the final exact contracts immediately before Discord renders."""
    grouped: dict[str, list] = {}
    for candidate in candidates:
        chain = str(candidate.token.chain_id or "").strip().lower()
        mint = str(candidate.token.mint or "").strip()
        if chain and mint:
            grouped.setdefault(chain, []).append(candidate)

    refreshed = 0
    lock = asyncio.Lock()

    async def fetch_batch(client: httpx.AsyncClient, chain: str, batch: list) -> None:
        nonlocal refreshed
        addresses = ",".join(candidate.token.mint for candidate in batch)
        try:
            response = await client.get(f"{base_url.rstrip('/')}/tokens/v1/{chain}/{addresses}")
            response.raise_for_status()
            pairs = response.json()
        except (httpx.HTTPError, ValueError):
            return
        if not isinstance(pairs, list):
            return

        for candidate in batch:
            mint = candidate.token.mint.lower()
            matches = [
                pair for pair in pairs
                if isinstance(pair, dict)
                and str((pair.get("baseToken") or {}).get("address") or "").lower() == mint
                and float(pair.get("marketCap") or 0) > 0
            ]
            if not matches:
                continue
            pair = max(matches, key=lambda item: float((item.get("liquidity") or {}).get("usd") or 0))
            current = float(pair.get("marketCap") or 0)
            candidate.token.market_cap = current
            candidate.token.liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or candidate.token.liquidity_usd)
            candidate.token.volume_24h = float((pair.get("volume") or {}).get("h24") or candidate.token.volume_24h)
            candidate.token.price_change_24h = float((pair.get("priceChange") or {}).get("h24") or candidate.token.price_change_24h)
            candidate.peak_market_cap = max(float(candidate.peak_market_cap or 0), current)
            candidate.observed_peak_market_cap = max(float(candidate.observed_peak_market_cap or 0), current)
            peak = _verified_peak(candidate)
            candidate.drawdown_from_peak_pct = max(0.0, (peak - current) / peak * 100.0) if peak else 0.0
            async with lock:
                refreshed += 1

    async with httpx.AsyncClient(timeout=20, transport=transport) as client:
        await asyncio.gather(*(
            fetch_batch(client, chain, rows[start:start + 30])
            for chain, rows in grouped.items()
            for start in range(0, len(rows), 30)
        ))
    return refreshed


def _daily_move_pct(candidate) -> float:
    """Best measured upside inside the trailing-day window."""
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    if int(gmgn.get("kline24hCandleCount") or 0) > 0:
        return max(
            float(gmgn.get("kline24hPeakFromOpenPct") or 0),
            float(gmgn.get("kline24hChangePct") or 0),
            float(candidate.token.price_change_24h or 0),
        )
    peak = _verified_peak(candidate)
    start = float(candidate.start_market_cap or 0)
    measured = ((peak / start) - 1.0) * 100.0 if start > 0 else 0.0
    return max(float(candidate.token.price_change_24h or 0), measured)


def _strong_entrapment_override(candidate, settings) -> bool:
    """Allow an entrapment warning only when every organic-market check is strong.

    Entrapment is useful evidence, but it can overstate risk on a broad, liquid
    market.  This exception is deliberately conjunctive: liquidity, volume,
    holder breadth, holder concentration and KOL participation must all clear
    their stronger publication thresholds.  It does not override any contract,
    authority, honeypot, rug, wash-trading or bundle finding.
    """
    section = settings.section("journal")
    if not bool(section.get("publication_entrapment_override_enabled", True)):
        return False

    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    holders = candidate.safety.holder_count
    if holders is None and gmgn.get("holderCount") is not None:
        holders = int(gmgn["holderCount"])
    top10 = candidate.safety.top10_pct
    if top10 is None and gmgn.get("top10Pct") is not None:
        top10 = float(gmgn["top10Pct"])

    return bool(
        float(candidate.token.liquidity_usd or 0)
        >= float(section.get("publication_entrapment_override_min_liquidity", 250_000) or 250_000)
        and float(candidate.token.volume_24h or 0)
        >= float(section.get("publication_entrapment_override_min_volume_24h", 1_000_000) or 1_000_000)
        and holders is not None
        and int(holders)
        >= int(section.get("publication_entrapment_override_min_holders", 5_000) or 5_000)
        and top10 is not None
        and float(top10)
        <= float(section.get("publication_entrapment_override_max_top10_pct", 20) or 20)
        and kol_trade_count(candidate)
        >= int(section.get("publication_entrapment_override_min_kols", 10) or 10)
    )


def _publication_safety_reasons(candidate, settings) -> list[str]:
    """Direct adverse evidence that must never reach the public recap."""
    section = settings.section("journal")
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    reasons = list(rug_or_bundle(candidate, settings))

    # GMGN's explicit honeypot verdict was present for VELVET but the old
    # ad-hoc recap path never consulted it. A direct verdict is a hard stop.
    if _truthy(gmgn.get("isHoneypot")):
        reasons.append("GMGN marks the contract as a honeypot")

    top10 = candidate.safety.top10_pct
    if top10 is None and gmgn.get("top10Pct") is not None:
        top10 = float(gmgn["top10Pct"])
    max_top10 = float(section.get("publication_max_top10_pct", 50) or 50)
    if top10 is not None and float(top10) > max_top10:
        reasons.append(f"top 10 holders control {float(top10):.1f}%")

    min_holders = int(section.get("publication_min_holders", 1_000) or 1_000)
    holders = candidate.safety.holder_count
    if holders is None and gmgn.get("holderCount") is not None:
        holders = int(gmgn["holderCount"])
    if holders is not None and int(holders) < min_holders:
        reasons.append(f"only {int(holders):,} holders")

    if candidate.token.chain_id.lower() == "solana":
        min_kols = int(section.get("publication_min_solana_kols", 1) or 1)
        if kol_trade_count(candidate) < min_kols:
            reasons.append(f"only {kol_trade_count(candidate)}/{min_kols} tracked KOL trades")
    else:
        # Coverage is thinner away from Solana, so those names use scarce
        # editorial slots only when the live market and wallet participation
        # are both substantial. This removes tiny index memes and 90% round
        # trips without weakening Solana discovery.
        min_other_kols = int(section.get("publication_min_other_kols", 5) or 5)
        if kol_trade_count(candidate) < min_other_kols:
            reasons.append(f"only {kol_trade_count(candidate)}/{min_other_kols} tracked KOL trades")
        min_other_liquidity = float(
            section.get("publication_min_other_liquidity", 100_000) or 100_000
        )
        if float(candidate.token.liquidity_usd or 0) < min_other_liquidity:
            reasons.append("non-Solana liquidity is below the publication floor")
        peak = _verified_peak(candidate)
        current = float(candidate.token.market_cap or 0)
        drawdown = ((peak - current) / peak) * 100.0 if peak > current else 0.0
        max_drawdown = float(section.get("publication_other_max_drawdown_pct", 80) or 80)
        if drawdown > max_drawdown:
            reasons.append(f"non-Solana runner round-tripped {drawdown:.0f}% from its peak")
        entrapment = gmgn.get("entrapmentRatio")
        max_entrapment = float(
            section.get("publication_other_max_entrapment_ratio", 0.50) or 0.50
        )
        if (
            entrapment is not None
            and float(entrapment) > max_entrapment
            and not _strong_entrapment_override(candidate, settings)
        ):
            reasons.append(f"entrapment-linked flow is {float(entrapment):.0%}")

    return list(dict.fromkeys(reasons))


def _required_old_move(candidate, settings) -> float:
    section = settings.section("journal")
    peak = _verified_peak(candidate)
    small_ceiling = float(section.get("old_coin_small_cap_ceiling", 10_000_000) or 10_000_000)
    large_floor = float(section.get("old_coin_large_cap_floor", 20_000_000) or 20_000_000)
    solana = candidate.token.chain_id.lower() == "solana"
    prefix = "publication_solana_old" if solana else "publication_other_old"
    defaults = (200.0, 125.0, 50.0) if solana else (400.0, 250.0, 100.0)
    if peak < small_ceiling:
        return float(section.get(f"{prefix}_small_move_pct", defaults[0]) or defaults[0])
    if peak >= large_floor:
        return float(section.get(f"{prefix}_large_move_pct", defaults[2]) or defaults[2])
    return float(section.get(f"{prefix}_mid_move_pct", defaults[1]) or defaults[1])


def _eligible_for_recap(candidate, settings) -> bool:
    """Keep fresh launches first and demand exceptional moves from old coins."""
    section = settings.section("journal")
    liquidity_floor = float(section.get("min_liquidity", 40_000) or 40_000)
    if float(candidate.token.liquidity_usd or 0) < liquidity_floor:
        return False
    if _publication_safety_reasons(candidate, settings):
        return False
    peak_floor = float(section.get("peak_market_cap_floor", 1_000_000) or 1_000_000)
    if _verified_peak(candidate) < peak_floor:
        return False

    age = candidate.signals.age_hours
    if age is None:
        return False
    age = float(age)
    fresh_hours = float(section.get("publication_fresh_hours", 24) or 24)
    if age <= fresh_hours:
        return True
    return _daily_move_pct(candidate) >= _required_old_move(candidate, settings)


def _recap_rank(candidate) -> tuple[float, ...]:
    """Prefer fresh, liquid, broadly traded names; penalise deep round trips."""
    peak = _verified_peak(candidate)
    current = float(candidate.token.market_cap or 0)
    drawdown = max(0.0, ((peak - current) / peak) * 100.0) if peak else 100.0
    fresh = float(candidate.signals.age_hours or 10_000) <= 24
    return (
        1.0 if fresh else 0.0,
        min(_daily_move_pct(candidate), 10_000.0),
        min(float(candidate.token.liquidity_usd or 0) / 100_000.0, 20.0),
        min(kol_trade_count(candidate), 50),
        -drawdown,
        peak,
    )


def _other_recap_rank(candidate, settings) -> tuple[float, ...]:
    """Protect meaningful large-cap moves from being buried by tiny launches.

    The cross-chain board is intentionally capped, but that cap previously
    ranked every fresh launch ahead of an established $20M+ coin.  CASHCAT
    therefore disappeared despite clearing the user's +30% large-cap rule.
    A verified large-cap mover now receives first priority inside the existing
    non-Solana allowance; all safety gates, including entrapment, still run
    before this ranking is reached.
    """
    section = settings.section("journal")
    large_floor = float(section.get("old_coin_large_cap_floor", 20_000_000) or 20_000_000)
    is_major_mover = (
        _verified_peak(candidate) >= large_floor
        and _daily_move_pct(candidate) >= _required_old_move(candidate, settings)
    )
    return (1.0 if is_major_mover else 0.0, *_recap_rank(candidate))


def _select_recap_candidates(candidates: list, settings) -> list:
    """Build a Solana-led board with exceptional-multiple overflow slots."""
    section = settings.section("journal")
    max_total = int(section.get("publication_max_coins", 15) or 15)
    standard_total = min(
        max_total,
        int(section.get("publication_standard_coins", 15) or 15),
    )
    overflow_multiple = float(
        section.get("publication_overflow_min_multiple", 5.0) or 5.0
    )
    max_other = int(section.get("publication_max_non_solana", 4) or 4)
    eligible = [candidate for candidate in candidates if _eligible_for_recap(candidate, settings)]
    solana = sorted(
        (candidate for candidate in eligible if candidate.token.chain_id.lower() == "solana"),
        key=_recap_rank,
        reverse=True,
    )
    others = sorted(
        (candidate for candidate in eligible if candidate.token.chain_id.lower() != "solana"),
        key=lambda candidate: _other_recap_rank(candidate, settings),
        reverse=True,
    )
    # Reserve the configured non-Solana allowance before filling the remaining
    # normal board with Solana. Slots above the normal edition size are earned
    # only by additional verified 5x-style moves, never by quota filling.
    base_others = others[:min(max_other, standard_total)]
    solana_slots = max(0, standard_total - len(base_others))
    selected = sorted(
        [*solana[:solana_slots], *base_others], key=_recap_rank, reverse=True
    )
    selected_mints = {candidate.token.mint for candidate in selected}
    other_count = sum(
        candidate.token.chain_id.lower() != "solana" for candidate in selected
    )
    overflow_pool = sorted(
        (candidate for candidate in eligible if candidate.token.mint not in selected_mints),
        key=_recap_rank,
        reverse=True,
    )
    for candidate in overflow_pool:
        if len(selected) >= max_total:
            break
        is_other = candidate.token.chain_id.lower() != "solana"
        if is_other and other_count >= max_other:
            continue
        if verified_window_multiple(candidate) < overflow_multiple:
            continue
        selected.append(candidate)
        selected_mints.add(candidate.token.mint)
        other_count += int(is_other)
    return sorted(selected, key=_recap_rank, reverse=True)


def _dedupe(rows: list[dict], excluded: set[str]) -> list:
    chosen = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        mint = str(row.get("mint") or "").strip()
        chain = str(row.get("chain") or "").strip().lower()
        identity = (chain, mint.lower())
        if not mint or mint.lower() in excluded or identity in seen:
            continue
        seen.add(identity)
        candidate = _candidate(row)
        _clean_existing_evidence(candidate)
        chosen.append(candidate)
    chosen.sort(key=peak_cap, reverse=True)
    return chosen


def _approved_layout(candidates: list, intro: str = "") -> dict:
    """Keep the approved recap shape stable regardless of model headings."""
    ordered = sorted(candidates, key=_verified_peak, reverse=True)
    leaders = ordered[:5]
    remaining = ordered[5:]
    solana = [candidate for candidate in remaining if candidate.token.chain_id.lower() == "solana"]
    cross_chain = [candidate for candidate in remaining if candidate.token.chain_id.lower() != "solana"]

    def section(title: str, members: list) -> dict:
        return {
            "title": title,
            "coins": [{"mint": candidate.token.mint, "line": ""} for candidate in members],
        }

    sections = [
        section("Market Leaders", leaders),
        section("More Solana Runners", solana),
        section("Cross-Chain Moves", cross_chain),
    ]
    return {
        "intro": _compact(intro, 220) or "Short version: what ran and why.",
        "sections": [item for item in sections if item["coins"]],
        "layout": "short",
    }


def _render_posts(candidates: list, narrative: dict, generated: datetime) -> list[dict]:
    by_mint = {candidate.token.mint: candidate for candidate in candidates}
    placed: set[str] = set()
    sections: list[tuple[str, list[str]]] = []

    for section in narrative.get("sections") or []:
        title = _compact(section.get("title"), 70)
        lines: list[str] = []
        for item in section.get("coins") or []:
            mint = str(item.get("mint") or "").strip()
            candidate = by_mint.get(mint)
            if candidate is None or mint in placed:
                continue
            placed.add(mint)
            override = EDITORIAL.get(mint)
            lore_limit = _lore_limit(candidate)
            editorial_copy = TAIL_EDITORIAL.get(mint) or (override[0] if override else "")
            line = (
                _compact(editorial_copy, lore_limit)
                if override
                else (_compact(item.get("line"), lore_limit) or _fallback_line(candidate))
            )
            source = override[1] if override else _source(candidate)
            links = f" · [src]({source})" if source else ""
            lines.append(
                f"[**${candidate.token.symbol}**]({fomo_token_url(candidate.token.chain_id, mint)}) "
                f"→ **{_result(candidate)}** — {line}{links}"
            )
        if title and lines:
            sections.append((title, lines))

    missing = [candidate for candidate in candidates if candidate.token.mint not in placed]
    if missing:
        lines = []
        for candidate in missing:
            mint = candidate.token.mint
            placed.add(mint)
            override = EDITORIAL.get(mint)
            source = override[1] if override else _source(candidate)
            links = f" · [src]({source})" if source else ""
            cause = TAIL_EDITORIAL.get(mint) or (override[0] if override else str(
                (candidate.provider_evidence.get("why", {}) or {}).get("cause") or ""
            ))
            lines.append(
                f"[**${candidate.token.symbol}**]({fomo_token_url(candidate.token.chain_id, mint)}) "
                f"→ **{_result(candidate)}** — "
                f"{_compact(cause, _lore_limit(candidate)) or _fallback_line(candidate)}{links}"
            )
        sections.append(("More Plays", lines))

    rendered_mints = [mint for mint in placed]
    expected = set(by_mint)
    if set(rendered_mints) != expected or len(rendered_mints) != len(expected):
        raise RuntimeError("refusing Discord delivery: final copy is missing or duplicating a contract")

    intro = _compact(narrative.get("intro"), 220) or "Short version: what ran and why."
    blocks: list[str] = []
    for title, lines in sections:
        # One runner per visual row. Dense inline paragraphs are technically
        # shorter but miserable to scan on both desktop and mobile Discord.
        block = f"**{title}**\n\n" + "\n\n".join(lines)
        if len(block) <= MAX_DESCRIPTION:
            blocks.append(block)
            continue
        current = f"**{title}**\n\n"
        for line in lines:
            if len(current) + len(line) + 1 > MAX_DESCRIPTION:
                blocks.append(current.rstrip())
                current = f"**{title} (cont.)**\n\n"
            current += line + "\n\n"
        if current.strip():
            blocks.append(current.rstrip())

    # Discord limits one embed description to 4,096 characters but allows up
    # to ten embeds in one message. Keep each editorial section in its own
    # embed instead of inventing an awkward "Solana Continued" message.
    descriptions = list(blocks)
    if descriptions:
        first = f"{intro}\n\n{descriptions[0]}" if intro else descriptions[0]
        if len(first) <= MAX_DESCRIPTION:
            descriptions[0] = first
        elif intro:
            descriptions.insert(0, intro)
    elif intro:
        descriptions = [intro]

    embeds: list[dict] = [
        {"color": BRAND, "description": description}
        for description in descriptions
    ]
    if embeds:
        embeds[0].update({
            "author": {"name": "fomo onchain", "icon_url": LOGO_REF},
            "thumbnail": {"url": LOGO_REF},
            "title": f"Daily Memecoin Recap — {generated.strftime('%B %d')}",
        })

    def embed_chars(embed: dict) -> int:
        return (
            len(str(embed.get("title") or ""))
            + len(str(embed.get("description") or ""))
            + len(str((embed.get("footer") or {}).get("text") or ""))
        )

    # Normally the approved 15-name edition is one Discord message containing
    # three clean section embeds. If a future edition exceeds Discord's shared
    # 6,000-character budget, split only at section boundaries.
    posts: list[dict] = []
    current_embeds: list[dict] = []
    current_chars = 0
    for embed in embeds:
        size = embed_chars(embed)
        if current_embeds and (
            len(current_embeds) >= MAX_MESSAGE_EMBEDS
            or current_chars + size > MAX_MESSAGE_EMBED_CHARS
        ):
            posts.append({"username": "fomo onchain", "embeds": current_embeds})
            current_embeds = []
            current_chars = 0
        current_embeds.append(embed)
        current_chars += size
    if current_embeds:
        posts.append({"username": "fomo onchain", "embeds": current_embeds})

    solana_count = sum(
        candidate.token.chain_id.lower() == "solana" for candidate in candidates
    )
    cross_chain_count = len(candidates) - solana_count
    footer = (
        f"{len(candidates)} runners · {solana_count} Solana · "
        f"{cross_chain_count} cross-chain · Rolling 24h window · verified at publication time"
    )
    if posts:
        posts[-1]["embeds"][-1]["footer"] = {"text": footer}

    # Validate the delivered representation, not only the intermediate set.
    # A previous section-splitting bug passed the pre-layout identity check and
    # then silently dropped rows from the Discord payload.
    rendered = "\n".join(
        str(embed.get("description") or "")
        for post in posts
        for embed in post.get("embeds") or []
    )
    bad_counts = {
        candidate.token.mint: rendered.count(
            fomo_token_url(candidate.token.chain_id, candidate.token.mint)
        )
        for candidate in candidates
    }
    bad_counts = {mint: count for mint, count in bad_counts.items() if count != 1}
    if bad_counts:
        raise RuntimeError(f"refusing Discord delivery: rendered contract counts {bad_counts}")
    return posts


def _render_compact_posts(candidates: list, generated: datetime) -> list[dict]:
    """Render five compact features, then exactly one line per other coin."""
    by_mint = {candidate.token.mint: candidate for candidate in candidates}
    placed: set[str] = set()
    blocks: list[str] = []

    for mint, story in FEATURE_STORIES.items():
        candidate = by_mint.get(mint)
        if candidate is None:
            continue
        placed.add(mint)
        source = str(story["source"])
        headline = (
            f"[**${candidate.token.symbol}**]({fomo_token_url(candidate.token.chain_id, mint)}) "
            f"→ **{_result(candidate)}** — {_compact(story['summary'], 150)} "
            f"· [source]({source})"
        )
        # One supporting line is enough in Discord. The linked source carries
        # the deeper read without turning the morning recap into an article.
        detail = _compact(story["bullets"][-1], 155)
        blocks.append(f"**{story['title']}**\n{headline}\n• {detail}")

    tail = [candidate for candidate in candidates if candidate.token.mint not in placed]
    tail.sort(key=_verified_peak, reverse=True)
    tail_lines: list[str] = []
    for candidate in tail:
        mint = candidate.token.mint
        placed.add(mint)
        override = EDITORIAL.get(mint)
        cause = TAIL_EDITORIAL.get(mint) or (override[0] if override else str(
            (candidate.provider_evidence.get("why", {}) or {}).get("cause") or ""
        ))
        tail_lines.append(
            f"[**${candidate.token.symbol}**]({fomo_token_url(candidate.token.chain_id, mint)}) "
            f"→ **{_result(candidate)}** — "
            f"{_compact(cause, 72) or _fallback_line(candidate)}"
        )

    if tail_lines:
        # Balanced chunks avoid an almost-empty final Discord post while
        # preserving generous whitespace between the one-sentence rows.
        chunk_size = 16
        for start in range(0, len(tail_lines), chunk_size):
            number = (start // chunk_size) + 1
            title = "More Runners" if number == 1 else f"More Runners (cont. {number})"
            block = f"**{title}**\n\n" + "\n".join(tail_lines[start:start + chunk_size])
            if len(block) > MAX_DESCRIPTION:
                raise RuntimeError(f"refusing Discord delivery: tail block {number} exceeds embed limit")
            blocks.append(block)

    expected = set(by_mint)
    if placed != expected or len(placed) != len(expected):
        raise RuntimeError("refusing Discord delivery: final copy is missing or duplicating a contract")

    intro = "What ran and why — five stories, then the tape."
    descriptions: list[str] = []
    current = intro
    for block in blocks:
        addition = ("\n\n" if current else "") + block
        if len(current) + len(addition) > MAX_DESCRIPTION:
            descriptions.append(current)
            current = block
        else:
            current += addition
    if current:
        descriptions.append(current)

    posts: list[dict] = []
    for index, description in enumerate(descriptions):
        embed = {"color": BRAND, "description": description}
        if index == 0:
            embed.update({
                "author": {"name": "fomo onchain", "icon_url": LOGO_REF},
                "thumbnail": {"url": LOGO_REF},
                "title": f"Daily Memecoin Recap — {generated.strftime('%B %d')}",
            })
        embed["footer"] = {"text": "Rolling 24h window · verified at publication time"}
        posts.append({"username": "fomo onchain", "embeds": [embed]})

    rendered = "\n".join(
        str(embed.get("description") or "")
        for post in posts
        for embed in post.get("embeds") or []
    )
    bad_counts = {
        candidate.token.mint: rendered.count(
            fomo_token_url(candidate.token.chain_id, candidate.token.mint)
        )
        for candidate in candidates
    }
    bad_counts = {mint: count for mint, count in bad_counts.items() if count != 1}
    if bad_counts:
        raise RuntimeError(f"refusing Discord delivery: rendered contract counts {bad_counts}")
    return posts


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--editorial-only", action="store_true",
        help="use the completed contract-keyed editorial audit without rerunning network research",
    )
    parser.add_argument(
        "--webhook-url", action="append", default=[],
        help="send only to this explicit webhook; repeat for multiple approval destinations",
    )
    args = parser.parse_args()

    settings = load_settings(str(ROOT / "config.toml"))
    snapshot = json.loads((ROOT / "web/data/latest.json").read_text(encoding="utf-8-sig"))
    excluded = {
        str(mint).strip().lower()
        for mint in settings.get("journal", "excluded_mints", []) or []
    }
    candidates = _select_recap_candidates(
        _dedupe(list(snapshot.get("runners") or []), excluded), settings
    )
    if not candidates:
        raise RuntimeError("latest snapshot has no publishable runners")

    # This command is explicitly the deep editorial pass, so cover the whole
    # final board rather than the normal morning-run budget.
    settings.values.setdefault("lore", {})["max_coins"] = len(candidates)
    settings.values.setdefault("lore", {})["concurrency"] = 4
    settings.values.setdefault("newsletter", {})["research_limit"] = len(candidates)
    settings.values.setdefault("newsletter", {})["research_concurrency"] = 4

    generated = datetime.fromisoformat(snapshot["generatedAt"])
    if args.editorial_only:
        lore_count = researched = explained = 0
        narrative = snapshot.get("narrative") or {
            "intro": "Short version: what ran and why.", "sections": []
        }
    else:
        lore_count = await attach_lore(candidates, settings)
        researched = await research_day(candidates, settings)
        explained = await explain_runs(candidates, settings)
        narrative = await write_recap(candidates, generated, settings) or {
            "intro": "Short version: what ran and why.", "sections": []
        }
    narrative = _approved_layout(candidates, str(narrative.get("intro") or ""))
    refreshed = await _refresh_current_market_caps(candidates)
    posts = _render_posts(candidates, narrative, generated)

    if args.dry_run:
        out = ROOT / "output" / "discord-recap-preview.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"dry_run=true coins={len(candidates)} posts={len(posts)} repriced={refreshed} output={out}")
        return 0

    token = bot_token()
    channels = bot_channel_ids() if not args.webhook_url else []
    urls = list(args.webhook_url) or ([] if token and channels else webhook_urls())
    if token and channels:
        for post in posts:
            post["components"] = interactive_market_components(
                report_date=generated.strftime("%Y%m%d")
            )
        for channel_id in channels:
            for index, payload in enumerate(posts):
                await post_bot_payload(
                    channel_id, payload, token,
                    ROOT / "web" / LOGO if index == 0 else None,
                )
    elif urls:
        for url in urls:
            for index, payload in enumerate(posts):
                await post_payload(url, payload, ROOT / "web" / LOGO if index == 0 else None)
    else:
        raise RuntimeError("no Discord bot channel or webhook configured")
    print(
        f"sent=true coins={len(candidates)} unique_contracts={len({c.token.mint for c in candidates})} "
        f"posts={len(posts)} bot_channels={len(channels)} webhooks={len(urls)} repriced={refreshed} "
        f"lore={lore_count} researched={researched} explained={explained}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
