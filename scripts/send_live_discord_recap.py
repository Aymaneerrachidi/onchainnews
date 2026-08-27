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
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brief.config import load_settings  # noqa: E402
from brief.journal import kol_trade_count, rug_or_bundle, verified_window_multiple  # noqa: E402
from brief.links import fomo_token_url  # noqa: E402
from brief.lore import attach_lore  # noqa: E402
from brief.lore_style import humanize_lore  # noqa: E402
from brief.newsletter import write_recap  # noqa: E402
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
from brief.sources.text_quality import HYPE, PROMO, WALLET_TRACKER  # noqa: E402

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


async def _delete_existing_daily_recaps(channel_id: str, token: str) -> int:
    """Remove this bot's prior recap pages before publishing a replacement."""
    headers = {"Authorization": f"Bot {token}"}
    base = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    removed = 0
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(base, headers=headers, params={"limit": 20})
        response.raise_for_status()
        for message in response.json():
            if not bool((message.get("author") or {}).get("bot")):
                continue
            embeds = message.get("embeds") or []
            searchable = "\n".join(
                f"{embed.get('title') or ''}\n{embed.get('description') or ''}\n"
                f"{(embed.get('footer') or {}).get('text') or ''}"
                for embed in embeds
            )
            if not any(marker in searchable for marker in (
                "Daily Memecoin Recap",
                "More Solana Runners",
                "Cross-Chain Moves",
                "total runners",
                "Rolling 24h window",
            )):
                continue
            deleted = await client.delete(
                f"{base}/{message['id']}", headers=headers,
            )
            deleted.raise_for_status()
            removed += 1
    return removed
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
    "488SaFq6wHF2z2k6NLSD3PtoSkXDNZaPkJwxze11pump": (
        "community coin around Momo, the internet-famous photogenic Shiba Inu",
        "https://momocoin.org/",
    ),
    "DXXcq4tY5e4PbXybyBMnxZjHVmzv1GVrAXoW5TcC5kbu": (
        "older pump.fun pisscoin meme revived across 14 tracked KOL wallets",
        "https://kolexplorer.com/token/DXXcq4tY5e4PbXybyBMnxZjHVmzv1GVrAXoW5TcC5kbu",
    ),
    "5dkPngQmeqTUN57RqhxdA6xCaz7AKdzQoHLdk9xhpump": (
        "gamer-community token linked to the Cyberleek account",
        "https://aitrade.mytokencap.com/zh/sol/token/5dkPngQmeqTUN57RqhxdA6xCaz7AKdzQoHLdk9xhpump",
    ),
    "9Y3fY1kwYUTgLwBU7DZKkEwCsZYj3BrXQNP74ZNEpump": (
        "small-cat community meme with a dedicated project site",
        "https://www.the-smol-cat.fun/",
    ),
}

# Intentionally written tape copy for today's sub-$1M runners. These are not
# machine-truncated versions of the lead stories, so they always end cleanly.
# Exact-contract context used by expanded Discord filter pages. The lead recap
# still compacts these to one line; filter views retain the full sentence.
FILTER_EDITORIAL: dict[str, tuple[str, str]] = {
    "0x4c57a356de114c5e1226cbf09066423d59b67777": (
        "Dark Cheems / black-Shiba community coin; the fresh BNB run followed renewed Totakeke chatter and an earlier public launch-and-buy signal attributed to Flap founder Cedric.",
        "https://twstalker.com/hashtag/%23totakeke",
    ),
    "0x49bac47750f3dcdba49350b5d74fd399e90f97c6": (
        "Community coin built around Robinhood's registered bull-with-sunglasses icon, with the group trying to turn the unused character into a chain-native mascot.",
        "https://t.me/s/bullcoinrh",
    ),
    "CxThkADKK4DDYqB8GBPaEAgRBzwxyPyUhFcBUmiAzN6N": (
        "Agent Heights is a working virtual office where autonomous agents occupy desks and execute assigned work; the token launched alongside its AnsemHack entry.",
        "https://lorescreener.com/entry/agent-heights",
    ),
    "5RY49DU5fBHHpqgpFPkbZWk4JdNCpNGRjf8pfiD4pump": (
        "Fresh Winfrey-the-Orca meme that surfaced through Solana hot-token and call feeds; no separate product or attributable outside catalyst was verified.",
        "https://tlmtr.io/ru/channels/2597061903-leo_bot1/posts",
    ),
    "0x45f82ac5d507e988f7406935da8eefe495a360e0": (
        "Brodie is based on the only office dog Robinhood publicly named, in a 2016 support-account post that the community revived a decade later.",
        "https://www.brodiehasfun.com/",
    ),
    "0x92ef5e9e7f80c071ac871691af1d4059dd4d7777": (
        "Chinese wordplay pairs 牛市 (bull market) with the near-homophone 牛屎 (bullshit), extending the viral Chinese bull-film meme into a market joke.",
        "https://niushi.gold/",
    ),
    "TwA2JbytoJh4ZJikWTtyXbTV1CE6gi64GHPi31Ypump": (
        "Older FOID contract returned to the tape; exact-contract pages verify the revival, but no public project story or fresh outside catalyst was found.",
        "https://www.solflare.com/prices/foid/TwA2JbytoJh4ZJikWTtyXbTV1CE6gi64GHPi31Ypump/",
    ),
    "0x3529e5b86e8749c8487a11ddc239c412228a40cc": (
        "The Robinhood Cat is a chain-native cat-mascot meme; the exact contract resurfaced after roughly a month of trading rather than on a new public catalyst.",
        "https://birdscan.io/token/0x3529E5B86e8749c8487a11ddc239C412228A40cc",
    ),
    "8mBC1RTCajBMiA35TfYUxizUn5rXtuiNgiJKWNMtpump": (
        "GIPP backs a six-agent Grokbot workspace for research, writing, outreach, operations, finance and support; Elon Musk reposted the builder and creator fees fund development.",
        "https://lorescreener.com/entry/gipp",
    ),
    "2NffKvfZTcFj2tyoY1Ev84PkqxA7DZnstyv6EwELpump": (
        "Sue is the 'cat wif helmet' image meme tied to an original social post; 11 tracked KOL wallets had traded the exact Solana contract during the audit.",
        "https://kolexplorer.com/token/2NffKvfZTcFj2tyoY1Ev84PkqxA7DZnstyv6EwELpump",
    ),
    "0x834dfc6c604ed4b89fc2230bbee47660cd07d0a2": (
        "Rintara is a playable Robinhood Chain game whose RIN token is designed for its marketplace, crafting, guilds and infrastructure rather than replacing ordinary in-game gold.",
        "https://twstalker.com/pijiu_hs",
    ),
    "H8xQ6poBjB9DTPMDTKWzWPrnxu4bDEhybxiouF8Ppump": (
        "Tokabu is the long-running 'Spirit of Gambling' mascot for casino-style crypto speculation; the exact contract previously secured XT and KCEX listings.",
        "https://xtsupport.zendesk.com/hc/en-us/articles/49244211108505-XT-Announcement-on-Launching-VIBE-VIBE-CAT-and-TOKABU-The-Spirit-of-Gambling",
    ),
    "0x64e36d5cccb5bacb0b250854331f68fbd4357777": (
        "BNC combines Binance Cat initials with BNC, an early name considered for BNB, positioning itself as the BNB-chain counterpart to the CASHCAT trade.",
        "https://twstalker.com/jiadaa888",
    ),
    "0xa7368f673535fd47abcd95a1c8430f990b227777": (
        "KAI is based on a dog repeatedly featured around Binance-community posts; fresh attention centered on International Dog Day photos and the owner acknowledging the contract.",
        "https://twstalker.com/BitBian",
    ),
    "0x3ce29e3c4876e656a28d5f28bc222d314408f17d": (
        "VladOS presents an autonomous Robinhood Chain desktop with trading, wallet, chart and memory modules; fees from its Lemon.fun launchpad and agent trading are routed to stakers.",
        "https://vlados.ai/",
    ),
}


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
    if candidate.x_interactions:
        return 190
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


def _saved_enrichment() -> dict[str, dict]:
    """Load exact-contract browser research without internal competitor copy."""
    paths = (
        ROOT / "output" / "enriched-current-all.json",
        ROOT / "output" / "enriched-current-72.json",
        ROOT / "output" / "enriched-current-40.json",
    )
    path = next((candidate for candidate in paths if candidate.exists()), None)
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row.get("mint") or "").strip().lower(): row
        for row in payload.get("coins") or []
        if isinstance(row, dict) and row.get("mint")
    }


def _apply_saved_enrichment(candidates: list) -> int:
    """Merge exact-contract browser research without exposing internal X leads."""
    researched = _saved_enrichment()
    applied = 0
    for candidate in candidates:
        row = researched.get(candidate.token.mint.lower())
        if not row:
            continue
        lore = _compact(humanize_lore(row.get("lore")), 260)
        if not lore or "mellometrics" in lore.casefold():
            continue
        web_research = row.get("webResearch") or {}
        sources = [
            str(url).strip() for url in web_research.get("sources") or []
            if str(url).strip() and "mellometrics" not in str(url).casefold()
        ]
        candidate.lore = lore
        candidate.provider_evidence["why"] = {
            "cause": lore,
            "sourceUrl": sources[0] if sources else "",
        }
        saved_news = [item for item in row.get("newsEvidence") or [] if isinstance(item, dict)]
        candidate.news_evidence = saved_news or candidate.news_evidence
        saved_x = [item for item in row.get("xInteractions") or [] if isinstance(item, dict)]
        if not saved_x:
            saved_x = [
                {
                    "author": str(item.get("source") or "X"),
                    "handle": str(item.get("source") or "X"),
                    "interaction": "exact_contract_evidence",
                    "summary": str(item.get("summary") or ""),
                    "url": str(item.get("url") or ""),
                    "confidence": str(item.get("confidence") or "confirmed"),
                    "matchedOn": str(item.get("matchedOn") or "exact contract"),
                }
                for item in saved_news
                if "x.com/" in str(item.get("url") or "")
            ]
        if saved_x:
            candidate.x_interactions = saved_x
        applied += 1
    return applied


def _merge_saved_enrichment_into_snapshot(snapshot: dict) -> int:
    """Persist researched lore for the dashboard and Discord filter buttons.

    The lead recap intentionally stays short. Interactive views read the raw
    snapshot, so they need the richer exact-contract explanation stored there
    instead of the generic no-X status generated during collection.
    """
    researched = _saved_enrichment()
    if not researched:
        return 0
    applied: set[tuple[str, str]] = set()
    for collection in ("runnerUniverse", "runners"):
        for row in snapshot.get(collection) or []:
            mint = str(row.get("mint") or "").strip()
            saved = researched.get(mint.lower())
            editorial = EDITORIAL.get(mint) or FILTER_EDITORIAL.get(mint)
            if not saved and not editorial:
                continue
            lore = _compact(humanize_lore(
                (saved or {}).get("lore")
                or ((saved or {}).get("webResearch") or {}).get("summary")
                or (editorial[0] if editorial else "")
            ), 420)
            if not lore or "mellometrics" in lore.casefold():
                continue
            sources = [
                str(url).strip()
                for url in ((saved or {}).get("webResearch") or {}).get("sources") or []
                if str(url).strip() and "mellometrics" not in str(url).casefold()
            ]
            if not sources and editorial and editorial[1]:
                sources = [editorial[1]]
            row["lore"] = lore
            provider = dict(row.get("providerEvidence") or {})
            provider["why"] = {
                "cause": lore,
                "sourceUrl": sources[0] if sources else "",
            }
            row["providerEvidence"] = provider
            applied.add((collection, mint.lower()))
    return len(applied)


# Public, contract-keyed conclusions from the completed exhaustive X audit.
# Keeping these contract keyed prevents generic tickers (notably INDEX) from
# inheriting unrelated posts. Internal-only monitoring accounts are excluded.
X_AUDIT_CONTEXT = {
    "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump": {
        "context": "X: PumpfunEco reported that $CATE gained 134% during the scan window.",
        "source": "https://x.com/PumpfunEco/status/2092052053392597461",
    },
    "Ge87EtsjwRQbHaqQmKRno69RFTwh9bfSsm99XNxTpump": {
        "context": "X: PumpfunEco reported a $170K whale buy as $JIMOTHY wicked to a $26M market cap.",
        "source": "https://x.com/PumpfunEco/status/2092143302506160193",
    },
    "0x2c43c41e8de000db5c12264e627cb6f813d37777": {
        "context": "X: GeckoTerminal ranked $MEEKO first among its five trending tokens that day.",
        "source": "https://x.com/GeckoTerminal/status/2092254773370409266",
    },
    "GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump": {
        "context": "X: PumpfunEco ranked $MORTY among the most-traded Pump.fun coins with $6.5M in 24-hour volume.",
        "source": "https://x.com/PumpfunEco/status/2092205332667773098",
    },
    "0xd7321801caae694090694ff55a9323139f043b88": {
        "context": "X: theunipcs posted an explicitly bullish $JUGGERNAUT reply during the scan window.",
        "source": "https://x.com/theunipcs/status/2091918413283086840",
    },
    "4HxV2vqATQEjn1hYw3eR43x9b3w1Z5ZoNoV5byTbpump": {
        "context": "X: Martin Shkreli and goodalexander mentioned Citrini in replies; the token connection is unconfirmed.",
        "source": "https://x.com/goodalexander/status/2092210944340353530",
    },
    "0x39dbed3a2bd333467115de45665cc57f813c4571": {
        "context": "X: Pons reported 28.5% of supply burned and treasury buys using 80% of protocol fees.",
        "source": "https://x.com/ponsdotfamily/status/2091824449544663367",
    },
    "6b7KQsXqb6JR5Nmeer5zGRmo51dwDfttM5b5Nu2rpump": {
        "context": "X: CoinDesk reported Kylie's account was hacked to promote the coin before it crashed 68%.",
        "source": "https://x.com/CoinDesk/status/2092351344401064312",
    },
    "0xac77646bcff9d52e99800534192e0290933f4094": {
        "context": "X: XbtPika discussed $MARTIANS alongside an Elon quote/repost and a roughly $3M valuation.",
        "source": "https://x.com/XbtPika/status/2092344640007549146",
    },
    "GPzpoXpD74E2C4CJNayuoyBqPQJEsPtdse3nhntrpump": {
        "context": "X: PumpfunEco reported a trader turning a $187 $LOOKSMAX buy into a $26,260 gain, about 140x.",
        "source": "https://x.com/PumpfunEco/status/2091904755408794042",
    },
    "Ab1sTFNv2tV5DX1XpriwNehXgiJhdq2RQ5LtD5BXpump": {
        "context": "X: Moonshot announced that $DOPAMEME was verified on its platform.",
        "source": "https://x.com/moonshot/status/2092010619075494190",
    },
    "0x7fe995a80075df3dc8ae11a9b82c7fe4202cd87f": {
        "context": "X: Pons Ecosystem shared a Thinking Cat PFP generator; its connection to this token is unconfirmed.",
        "source": "https://x.com/PonsEcosystem/status/2091819686514020752",
    },
    "0x45f82ac5d507e988f7406935da8eefe495a360e0": {
        "context": "X: theunipcs included the exact $BRODIE cashtag in a post about his FOMO leaderboard position.",
        "source": "https://x.com/theunipcs/status/2091810338446455144",
    },
    "H8xQ6poBjB9DTPMDTKWzWPrnxu4bDEhybxiouF8Ppump": {
        "context": "X: PumpfunEco reported that $TOKABU was up 72% over 24 hours.",
        "source": "https://x.com/PumpfunEco/status/2092179930142364023",
    },
    "0x3ce29e3c4876e656a28d5f28bc222d314408f17d": {
        "context": "X: WhaleInsider described $VLADOS as a self-operating agent launched on Robinhood Chain.",
        "source": "https://x.com/WhaleInsider/status/2092189474146935154",
    },
}


def _merge_exhaustive_x_context_into_snapshot(snapshot: dict) -> int:
    """Expose audited X evidence to every interactive Discord category view."""
    applied: set[tuple[str, str]] = set()
    for collection in ("runnerUniverse", "runners"):
        for row in snapshot.get(collection) or []:
            mint = str(row.get("mint") or "").strip()
            evidence = X_AUDIT_CONTEXT.get(mint)
            if not evidence:
                continue
            x_context = humanize_lore(evidence["context"])
            source = str(evidence["source"])
            normal_lore = _compact(row.get("lore"), 210)
            if normal_lore.startswith(x_context):
                combined = normal_lore
            else:
                combined = f"{x_context} {normal_lore}" if normal_lore else x_context

            row["lore"] = combined
            provider = dict(row.get("providerEvidence") or {})
            provider["why"] = {"cause": combined, "sourceUrl": source}
            row["providerEvidence"] = provider

            handle = source.split("x.com/", 1)[-1].split("/", 1)[0].lstrip("@")
            interactions = [
                item for item in (row.get("xInteractions") or [])
                if str(item.get("url") or "").strip() != source
            ]
            interactions.insert(0, {
                "author": handle,
                "handle": handle,
                "interaction": "audited_context",
                "summary": x_context,
                "url": source,
                "confidence": "verified_public_post",
                "matchedOn": "exact_contract_audit",
            })
            row["xInteractions"] = interactions
            applied.add((collection, mint.lower()))
    return len(applied)


def _apply_exhaustive_x_context(candidates: list) -> int:
    """Add verified public X context while retaining each coin's normal lore."""
    applied = 0
    for candidate in candidates:
        evidence = X_AUDIT_CONTEXT.get(candidate.token.mint)
        if not evidence:
            continue
        lore = _compact(candidate.lore, 210)
        x_context = humanize_lore(evidence["context"])
        candidate.lore = humanize_lore(f"{x_context} {lore}" if lore else x_context)
        candidate.provider_evidence["why"] = {
            "cause": candidate.lore,
            "sourceUrl": str(evidence["source"]),
        }
        applied += 1
    return applied


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


def _render_posts(
    candidates: list,
    narrative: dict,
    generated: datetime,
    total_available: int | None = None,
) -> list[dict]:
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
            editorial_copy = candidate.lore if candidate.x_interactions else (
                TAIL_EDITORIAL.get(mint) or (override[0] if override else "")
            )
            line = (
                _compact(editorial_copy, lore_limit)
                if editorial_copy
                else (
                    _compact(item.get("line"), lore_limit)
                    or _compact(candidate.lore, lore_limit)
                    or _fallback_line(candidate)
                )
            )
            source = _source(candidate) if candidate.x_interactions else (
                override[1] if override else _source(candidate)
            )
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
            source = _source(candidate) if candidate.x_interactions else (
                override[1] if override else _source(candidate)
            )
            links = f" · [src]({source})" if source else ""
            cause = (candidate.lore if candidate.x_interactions else "") or TAIL_EDITORIAL.get(mint) or (override[0] if override else str(
                (candidate.provider_evidence.get("why", {}) or {}).get("cause") or ""
            )) or candidate.lore
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
    page_count = len(posts)
    total_available = max(len(candidates), int(total_available or 0))
    for page_index, post in enumerate(posts, start=1):
        footer = (
            f"{len(candidates)} featured · {total_available} total runners · "
            f"page {page_index}/{page_count} · "
            f"{solana_count} Solana · {cross_chain_count} cross-chain · "
            "Rolling 24h window · verified at publication time"
        )
        post["embeds"][-1]["footer"] = {"text": footer}

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
    parser.add_argument(
        "--max-coins", type=int, default=15,
        help="maximum runners on the public lead page (default: 15)",
    )
    args = parser.parse_args()

    settings = load_settings(str(ROOT / "config.toml"))
    snapshot_path = ROOT / "web/data/latest.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
    merged_enrichment = _merge_saved_enrichment_into_snapshot(snapshot)
    merged_x_context = _merge_exhaustive_x_context_into_snapshot(snapshot)
    if merged_enrichment or merged_x_context:
        snapshot_path.write_text(
            # Match the exporter format so updating a few lore fields does not
            # rewrite the entire generated snapshot or corrupt non-ASCII names.
            json.dumps(snapshot, ensure_ascii=True, indent=1),
            encoding="utf-8",
        )
    excluded = {
        str(mint).strip().lower()
        for mint in settings.get("journal", "excluded_mints", []) or []
    }
    source_rows = list(snapshot.get("runnerUniverse") or snapshot.get("runners") or [])
    settings.values.setdefault("journal", {})["publication_max_coins"] = max(1, args.max_coins)
    settings.values.setdefault("journal", {})["publication_standard_coins"] = max(1, args.max_coins)
    candidates = _select_recap_candidates(_dedupe(source_rows, excluded), settings)
    if not candidates:
        raise RuntimeError("latest snapshot has no publishable runners")

    # Preserve the full research universe while recording the exact shortlist
    # that Discord published. Button views use this field and therefore cannot
    # expose rejected rows that were absent from the recap itself.
    source_by_key = {
        (str(row.get("chain") or "").lower(), str(row.get("mint") or "").lower()): row
        for row in source_rows
    }
    published_rows = []
    for candidate in candidates:
        key = (candidate.token.chain_id.lower(), candidate.token.mint.lower())
        row = source_by_key.get(key)
        if row is not None:
            published_rows.append(row)
    if len(published_rows) != len(candidates):
        raise RuntimeError("refusing Discord delivery: could not persist the exact published shortlist")
    snapshot["discordPublishedRunners"] = published_rows
    snapshot["discordPublishedCount"] = len(published_rows)
    snapshot["discordPublishedAt"] = datetime.now(timezone.utc).isoformat()
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=True, indent=1),
        encoding="utf-8",
    )
    _apply_saved_enrichment(candidates)
    _apply_exhaustive_x_context(candidates)
    x_enriched = sum(bool(candidate.x_interactions) for candidate in candidates)

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
        researched = 0
        explained = 0
        narrative = await write_recap(candidates, generated, settings) or {
            "intro": "Short version: what ran and why.", "sections": []
        }
    narrative = _approved_layout(candidates, str(narrative.get("intro") or ""))
    refreshed = await _refresh_current_market_caps(candidates)
    total_available = len({
        (str(row.get("chain") or "").lower(), str(row.get("mint") or "").lower())
        for row in source_rows
    })
    posts = _render_posts(candidates, narrative, generated, total_available)

    if args.dry_run:
        out = ROOT / "output" / "discord-recap-preview.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"dry_run=true coins={len(candidates)} posts={len(posts)} repriced={refreshed} "
            f"x_enriched={x_enriched} snapshot_enriched={merged_enrichment} output={out}"
        )
        return 0

    token = bot_token()
    channels = bot_channel_ids() if not args.webhook_url else []
    urls = list(args.webhook_url) or ([] if token and channels else webhook_urls())
    if token and channels:
        # One control surface for the whole recap. Repeating four rows of
        # buttons under every continuation page makes a two-page recap look
        # like multiple independent reports.
        posts[-1]["components"] = interactive_market_components(
            report_date=generated.strftime("%Y%m%d"),
            runner_count=total_available,
        )
        removed = 0
        for channel_id in channels:
            removed += await _delete_existing_daily_recaps(channel_id, token)
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
        f"posts={len(posts)} replaced={removed if token and channels else 0} "
        f"bot_channels={len(channels)} webhooks={len(urls)} repriced={refreshed} "
        f"lore={lore_count} researched={researched} explained={explained} x_enriched={x_enriched}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
