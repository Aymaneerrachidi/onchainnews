"""Render and optionally send the final, researched August 24 recap.

This is intentionally snapshot-based. It does not discover or rescan tokens;
it publishes the contracts that survived the final editorial and safety audit.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brief.config import load_settings
from brief.delivery import send_email, write_html
from brief.links import fomo_token_url
from brief.render.discord import BRAND, LOGO, LOGO_REF, post_payload, webhook_urls


SUBJECT = "Fomo Onchain | Daily Memecoin Recap — August 24"
RECIPIENT = "ue06prog@gmail.com"

FONT = "-apple-system,'Helvetica Neue','Segoe UI',Arial,sans-serif"
PAPER = "#EEF0F7"
SURFACE = "#FFFFFF"
INK = "#111322"
MUTED = "#687085"
BLUE = "#405CF5"
LINE = "#DFE3EF"
NIGHT = "#12152A"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def token(symbol: str, chain: str, mint: str, move: str, why: str, source: str = "") -> str:
    trade = fomo_token_url(chain, mint)
    why_html = esc(why)
    if source:
        why_html = f'<a href="{esc(source)}" style="color:{INK};text-decoration:underline;text-decoration-color:#B8BFD4">{why_html}</a>'
    return (
        f'<div style="font-family:{FONT};font-size:16px;line-height:1.55;color:{INK};margin:0 0 9px">'
        f'<a href="{esc(trade)}" style="font-weight:800;color:{INK};text-decoration:none">${esc(symbol)}</a>'
        f' <span style="color:{MUTED}">→</span> <strong>{esc(move)}</strong>, {why_html}</div>'
    )


def bullet(text: str) -> str:
    return (
        '<tr><td style="width:18px;vertical-align:top;padding:2px 0 4px;'
        f'font-family:{FONT};font-size:15px;color:{BLUE}">•</td>'
        f'<td style="padding:2px 0 4px;font-family:{FONT};font-size:15px;line-height:1.5;color:{MUTED}">{esc(text)}</td></tr>'
    )


def section(title: str, body: str, notes: tuple[str, ...] = ()) -> str:
    note_html = ""
    if notes:
        note_html = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="margin-top:7px">'
            + "".join(bullet(note) for note in notes)
            + "</table>"
        )
    return (
        '<tr><td style="padding:0 30px 27px">'
        f'<div style="font-family:{FONT};font-size:19px;line-height:1.3;font-weight:800;color:{INK};margin:0 0 11px">{esc(title)}</div>'
        f'{body}{note_html}'
        '</td></tr>'
    )


def render() -> str:
    rows: list[str] = []
    rows.append(
        '<tr><td style="padding:34px 30px 29px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{NIGHT};border-radius:24px">'
        '<tr><td style="padding:30px 28px">'
        f'<div style="font-family:{FONT};font-size:12px;line-height:1.2;font-weight:750;letter-spacing:.12em;text-transform:uppercase;color:#8F9BE4">fomo onchain</div>'
        f'<div style="font-family:{FONT};font-size:32px;line-height:1.12;font-weight:850;color:#FFFFFF;margin-top:12px">Daily Memecoin Recap</div>'
        f'<div style="font-family:{FONT};font-size:15px;line-height:1.5;color:#B9C0DD;margin-top:9px">August 24 · what ran, what faded, and why it mattered.</div>'
        '</td></tr></table></td></tr>'
    )

    rows.append(section(
        "GTA 6 Leaks",
        token(
            "CYBERLEEK", "solana", "ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg",
            "$1M to $20.4M (20x)", "apparent GTA 6 build leaks became the day’s dominant story",
            "https://www.pcgamer.com/games/grand-theft-auto/who-is-cyberleek-what-we-know-about-the-gta-6-leaker/",
        ),
        (
            "Copyright strikes and new clips kept the story alive after the first leak.",
            "The token existed before the public leak campaign; 20 tracked KOL wallets traded it and 3 still held at the audit.",
        ),
    ))

    rows.append(section(
        "Onchain Products",
        token(
            "CC", "solana", "E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump",
            "hit $6.6M", "a live market for trading social accounts, with 3% accruing to the account owner",
            "https://creatorcapital.trade/",
        )
        + token(
            "AI", "robinhood", "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18",
            "+71% to $28.5M", "paired directly with tokenized NVDA; fees grow a stock-token vault and burn or lock AI",
            "https://artificialinu.com/",
        )
        + token(
            "PONS", "robinhood", "0x39dbed3a2bd333467115de45665cc57f813c4571",
            "+58% to $67.7M", "the main launchpad token for fixed-supply coins on Robinhood Chain",
            "https://docs.ponsfamily.com/",
        ),
    ))

    rows.append(section(
        "Viral Memes",
        token(
            "MORTY", "solana", "GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump",
            "hit $2.3M", "Gucci Morty edits spread across TikTok, Instagram, Shorts and X",
            "https://pump.fun/coin/GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump/article",
        )
        + token(
            "LOOKSMAX", "solana", "GPzpoXpD74E2C4CJNayuoyBqPQJEsPtdse3nhntrpump",
            "hit $1.9M", "the looksmax forum meme crossed onto Solana and drew 13 tracked KOL wallets",
            "https://looksmax.org/threads/looksmax-token.2311347/",
        )
        + token(
            "NEEGY", "solana", "6oGuFDbEeaSzTcvrmmd2MqfNYwHKXFoN7regcR22pump",
            "hit $1.1M", "the Fortnite-style TikTok character returned with a large existing meme footprint",
            "https://neegy.wtf/",
        ),
    ))

    rows.append(section(
        "BNB Rotation",
        token(
            "BNBCAT", "bsc", "0x3efbfff95576e1d23cf6ead0acd2e73f4d6a7777",
            "hit $7.2M", "the Binance-cat leader caught the chain’s cat rotation",
        )
        + token(
            "MAX", "bsc", "0xe9bc5c6a86caa44fd7b469bf3cc7c563e4f77777",
            "hit $3.5M", "a 3% trade tax routes funds toward a Giggle Academy cause",
            "https://www.maxbnb.meme/",
        )
        + token(
            "MEMESTOCK", "bsc", "0x6ff45323817d1d53bbb8a8dfba9245ae74057777",
            "hit $2.9M", "GME-onchain token distributing GMEB to eligible holders",
            "https://memestock.run/stock",
        )
        + token(
            "肥嘟嘟", "bsc", "0x03c59bbf8ba49ce79831403b86acbc40d3167777",
            "hit $1.5M", "new Chinese-language Flap meme that cleared $3M of daily volume",
            "https://calibertoken.com/token/bsc/0x03c59bbf8ba49ce79831403b86acbc40d3167777",
        )
        + token(
            "BICAT", "bsc", "0xdbc6333a7d8bcd95f96641eda4d095e69f207777",
            "hit $2.6M, then faded 75%", "the BNB counterpart to the cross-chain cat trade",
            "https://t.me/BICATBNB",
        ),
    ))

    rows.append(section(
        "Old Coins Back",
        token(
            "MIGGLES", "base", "0xb1a03eda10342529bbf8eb700a06c60441fef25d",
            "+58% to $4.6M", "Coinbase’s cat IP returned; the community has a formal brand licence and creator fund",
            "https://www.miggles.io/",
        )
        + token(
            "CHUD", "solana", "6yjNqPzTSanBWSa6dxVEgTjePXBrZ2FoHLDQwYwEsyM6",
            "hit $2.6M", "the established Chudjak community returned to the tape without a new outside catalyst",
            "https://www.chudjaksolana.xyz/",
        )
        + token(
            "CAPY", "solana", "7VENy6wCjBChAMGpjPQCPYJDeYJGFr5NZUxk7uQ7bonk",
            "+257% to $1.2M", "the old capybara community revived; 20 tracked KOL wallets traded the move",
            "https://thecapytoken.com/",
        )
        + token(
            "HMM", "robinhood", "0x7fe995a80075df3dc8ae11a9b82c7fe4202cd87f",
            "+37% to $17.1M", "the Thinking Cat meme led the cleaner Robinhood Chain rotation",
            "https://hmmmm.fun/",
        ),
    ))

    rows.append(section(
        "Major Meme Tape",
        token(
            "CATE", "solana", "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump",
            "-38% to $34.2M", "gave back part of its recent run after printing a $91.9M all-time high",
            "https://cate.meme/",
        ),
        ("No other established name in the supplied Solana, Ethereum, Base or BNB list cleared a verified ±30% move in this check.",),
    ))

    rows.append(
        '<tr><td style="padding:2px 30px 34px">'
        f'<div style="height:1px;background:{LINE};font-size:0;line-height:1px;margin-bottom:20px">&nbsp;</div>'
        f'<div style="font-family:{FONT};font-size:12px;line-height:1.55;color:{MUTED}">'
        'Contract-first recap. Tickers can be copied; every coin name links to its exact Fomo Family token page. '
        'Market-cap figures are observed intraday highs or the stated audit-time value, not guarantees of executable liquidity.'
        '</div></td></tr>'
    )

    document = (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(SUBJECT)}</title></head>'
        f'<body style="margin:0;padding:0;background:{PAPER};-webkit-font-smoothing:antialiased">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PAPER}"><tr><td align="center">'
        f'<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" style="width:640px;max-width:100%;background:{SURFACE}">'
        + "".join(rows)
        + '</table></td></tr></table></body></html>'
    )

    banned = ("GMGN", "$XYZ", "$JLY", "$CLAW", "$HOOKR", "$HEDGE", "$NPC", "$STONKBROKER", "$INDEX")
    for value in banned:
        if value.lower() in document.lower():
            raise RuntimeError(f"banned newsletter text found: {value}")
    return document


def discord_coin(symbol: str, chain: str, mint: str, move: str, why: str, source: str = "") -> str:
    links = f"[${symbol}]({fomo_token_url(chain, mint)}) → **{move}**, {why}"
    if source:
        links += f" ([source]({source}))"
    return links


def discord_payload() -> dict:
    sections = [
        (
            "GTA 6 Leaks",
            discord_coin(
                "CYBERLEEK", "solana", "ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg",
                "$1M to $20.4M (20x)", "apparent GTA 6 build leaks became the day’s dominant story",
                "https://www.pcgamer.com/games/grand-theft-auto/who-is-cyberleek-what-we-know-about-the-gta-6-leaker/",
            )
            + "\n• Copyright strikes and new clips kept the story alive."
            + "\n• 20 tracked KOL wallets traded it; 3 still held at the audit.",
        ),
        (
            "Onchain Products",
            "\n".join((
                discord_coin("CC", "solana", "E3i7sTY5QYEBh3itepnomZQt7Eh5kzmHFk1vkm2pump", "hit $6.6M", "live market for trading social accounts", "https://creatorcapital.trade/"),
                discord_coin("AI", "robinhood", "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18", "+71% to $28.5M", "paired with tokenized NVDA; fees grow a vault and burn or lock AI", "https://artificialinu.com/"),
                discord_coin("PONS", "robinhood", "0x39dbed3a2bd333467115de45665cc57f813c4571", "+58% to $67.7M", "main launchpad token for fixed-supply Robinhood Chain coins", "https://docs.ponsfamily.com/"),
            )),
        ),
        (
            "Viral Memes",
            "\n".join((
                discord_coin("MORTY", "solana", "GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump", "hit $2.3M", "Gucci Morty edits spread across TikTok, Instagram, Shorts and X", "https://pump.fun/coin/GUmbtfjSZkybSFgPibBcvwExEBdXwewJHR5PkTjzpump/article"),
                discord_coin("LOOKSMAX", "solana", "GPzpoXpD74E2C4CJNayuoyBqPQJEsPtdse3nhntrpump", "hit $1.9M", "the forum meme crossed onto Solana and drew 13 tracked KOL wallets", "https://looksmax.org/threads/looksmax-token.2311347/"),
                discord_coin("NEEGY", "solana", "6oGuFDbEeaSzTcvrmmd2MqfNYwHKXFoN7regcR22pump", "hit $1.1M", "Fortnite-style TikTok character revival", "https://neegy.wtf/"),
            )),
        ),
        (
            "BNB Rotation",
            "\n".join((
                discord_coin("BNBCAT", "bsc", "0x3efbfff95576e1d23cf6ead0acd2e73f4d6a7777", "hit $7.2M", "the Binance-cat leader caught the chain’s cat rotation"),
                discord_coin("MAX", "bsc", "0xe9bc5c6a86caa44fd7b469bf3cc7c563e4f77777", "hit $3.5M", "3% trade tax routes funds toward a Giggle Academy cause", "https://www.maxbnb.meme/"),
                discord_coin("MEMESTOCK", "bsc", "0x6ff45323817d1d53bbb8a8dfba9245ae74057777", "hit $2.9M", "GME-onchain token distributing GMEB to eligible holders", "https://memestock.run/stock"),
                discord_coin("肥嘟嘟", "bsc", "0x03c59bbf8ba49ce79831403b86acbc40d3167777", "hit $1.5M", "new Chinese-language Flap meme with over $3M daily volume", "https://calibertoken.com/token/bsc/0x03c59bbf8ba49ce79831403b86acbc40d3167777"),
                discord_coin("BICAT", "bsc", "0xdbc6333a7d8bcd95f96641eda4d095e69f207777", "hit $2.6M, then faded 75%", "the BNB counterpart to the cross-chain cat trade", "https://t.me/BICATBNB"),
            )),
        ),
        (
            "Old Coins Back",
            "\n".join((
                discord_coin("MIGGLES", "base", "0xb1a03eda10342529bbf8eb700a06c60441fef25d", "+58% to $4.6M", "Coinbase’s cat IP returned with a formal brand licence and creator fund", "https://www.miggles.io/"),
                discord_coin("CHUD", "solana", "6yjNqPzTSanBWSa6dxVEgTjePXBrZ2FoHLDQwYwEsyM6", "hit $2.6M", "established Chudjak community returned without a new outside catalyst", "https://www.chudjaksolana.xyz/"),
                discord_coin("CAPY", "solana", "7VENy6wCjBChAMGpjPQCPYJDeYJGFr5NZUxk7uQ7bonk", "+257% to $1.2M", "old capybara community revival; 20 tracked KOL wallets traded it", "https://thecapytoken.com/"),
                discord_coin("HMM", "robinhood", "0x7fe995a80075df3dc8ae11a9b82c7fe4202cd87f", "+37% to $17.1M", "Thinking Cat led the cleaner Robinhood Chain rotation", "https://hmmmm.fun/"),
            )),
        ),
        (
            "Major Meme Tape",
            discord_coin("CATE", "solana", "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump", "-38% to $34.2M", "gave back part of its recent run after a $91.9M all-time high", "https://cate.meme/"),
        ),
    ]
    embeds = [{
        "color": BRAND,
        "author": {"name": "fomo onchain", "icon_url": LOGO_REF},
        "thumbnail": {"url": LOGO_REF},
        "title": "Daily Memecoin Recap — August 24",
        "description": "What ran, what faded, and why it mattered.",
    }]
    embeds.extend({"color": BRAND, "title": title, "description": body} for title, body in sections)
    total = sum(len(str(embed.get("title", ""))) + len(str(embed.get("description", ""))) for embed in embeds)
    if len(embeds) > 10 or total > 5800:
        raise RuntimeError(f"Discord payload exceeds safe limits: embeds={len(embeds)} chars={total}")
    return {"username": "fomo onchain", "embeds": embeds}


async def main(send: bool, discord: bool) -> None:
    settings = load_settings(ROOT / "config.toml")
    recipients = [str(value).strip().lower() for value in settings.get("delivery", "email_to", [])]
    if recipients != [RECIPIENT]:
        raise RuntimeError(f"refusing delivery: expected only {RECIPIENT}, got {recipients}")

    content = render()
    preview = ROOT / "output" / "final-enriched-recap.html"
    write_html(preview, content)
    print(f"preview={preview}")
    print(f"subject={SUBJECT}")
    print(f"recipient={RECIPIENT}")
    if send:
        delivered = await send_email(settings, SUBJECT, content)
        print(f"delivered={delivered}")
    if discord:
        urls = webhook_urls()
        if not urls:
            raise RuntimeError("no Discord webhook is configured")
        payload = discord_payload()
        for url in urls:
            await post_payload(url, payload, ROOT / "web" / LOGO)
        print(f"discord_webhooks={len(urls)} discord_posts=1")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="send after rendering and validation")
    parser.add_argument("--discord", action="store_true", help="post the same recap to configured Discord webhooks")
    args = parser.parse_args()
    asyncio.run(main(args.send, args.discord))
