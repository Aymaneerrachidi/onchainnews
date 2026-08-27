"""Why a coin ran, found rather than guessed.

The on-chain scanner is good at telling us *which* coins mattered and useless at
telling us *why*. Without the why, the recap is numbers flying past: "$4.7M,
down 2%, 3,127 holders" says nothing a reader will forward. The human recap this
product is measured against says "牛来 -- terribly animated Chinese film that
went viral, translates to Bull Comes", and that sentence is the entire product.

That sentence is on the open web. This module goes and gets it, for free:
DuckDuckGo Lite rendered through Jina Reader needs no key and no account, and
Dexscreener publishes the project's own description for tokens that filled in a
profile. Nothing here is scraped from behind a login.

The hard part is not searching, it is not being fooled. Tickers are reused
constantly -- a search for BICAT returns Simon's Cat, a different coin on the
same chain -- so queries run most-specific first, every result must survive a
relevance test, and a coin with nothing findable gets nothing. An empty lore
field is the correct answer for most memecoins and is much cheaper than a
confident sentence about the wrong token.
"""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx

from brief.config import Settings
from brief.models import Candidate

log = logging.getLogger("brief.lore")

# Jina Reader renders a page to markdown. Pointed at DuckDuckGo's lite
# endpoint it is, in effect, a keyless search API.
READER = "https://r.jina.ai/"
DDG_LITE = "https://lite.duckduckgo.com/lite/?q="
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"

UA = {"User-Agent": "Mozilla/5.0 (compatible; fomo-onchain/1.0)"}

# Somebody explaining the coin: exchange academies, crypto press, the project.
EXPLANATORY = (
    "academy", "phemex.com", "bingx.com", "cointelegraph.com", "coindesk.com",
    "theblock.co", "decrypt.co", "binance.com/en/square", "medium.com",
    "mirror.xyz", "x.com", "twitter.com", "reddit.com", "wikipedia.org",
)
# Price pages and explorers. They prove the coin exists, which we already knew
# from having its contract address, and explain nothing.
PRICE_PAGE = (
    "scan.org", "etherscan", "bscscan", "basescan", "solscan", "web3.okx.com",
    "web3.bitget.com", "web3.binance.com", "coinmarketcap.com", "livecoinwatch",
    "geckoterminal.com", "dexscreener.com", "dextools", "thebittimes",
    "coinbird.com", "bitget.com/price", "gmgn.ai",
)
BOILERPLATE = re.compile(
    r"(live price chart|price\s+\w*\s*today|market cap.{0,14}(trading )?volume"
    r"|trading & price chart|track the latest|historical .{0,12}charts?"
    r"|how to buy|price prediction|24-hour trading volume|live .{0,10}price"
    r"|analyze & trade|buy & swap|news, predictions|buy, sell, convert"
    r"|most trusted platform|invest on the|price usd|usd today"
    r"|convert, and invest)",
    re.I,
)
# A scraped interface rather than a sentence: "Chart Bubble Map Trades Top
# Traders Holders Dev Tokens Pools Positions Orders 5m-0.97% 1h1.70%".
UI_DUMP = re.compile(r"(\d+[mhd]-?\d|bubble map|top traders|dev tokens|positions orders|buy vol)", re.I)


def looks_like_page_furniture(text: str) -> bool:
    """Whether this is a price page or a scraped dashboard rather than prose."""
    if BOILERPLATE.search(text or "") or UI_DUMP.search(text or ""):
        return True
    words = [w for w in (text or "").split() if w]
    if not words:
        return True
    # Sentences have small words in them. A UI dump is mostly labels.
    numeric = sum(1 for w in words if any(ch.isdigit() for ch in w))
    return len(words) > 8 and numeric / len(words) > 0.35

# Promotion rather than an account of anything.
SHILL = re.compile(
    r"(\d+\s*[x×]|🚀|💎|ape|send it"
    r"|lfg|dont miss|early gem|100x|moon)",
    re.I,
)
# Words that mean the sentence is telling you something about the coin
# rather than quoting its price.
CAUSAL = re.compile(
    r"(listed on|listing|announce|launch(ed|es)?|partnership|partnered"
    r"|acquired|integrat|went viral|viral|trending|posted about|tweeted"
    r"|shared|endorsed|backed by|invested|burn(ed|t)?|locked|airdrop"
    r"|takeover|cto\b|rebrand|migrat|upgrade|hack|exploit|rug"
    r"|surge[ds]?|spike[ds]?|rally|pump(ed|ing)?|jumped|soared|because)",
    re.I,
)


STORY_WORDS = re.compile(
    r"(is a|are a|inspired|named after|named by|tied to|based on"
    r"|refers? to|launched|created by|founded|viral|meme|themed|tribute"
    r"|parody|community|narrative|story|explained|what is|film"
    r"|movie|character|mascot|announced|listed on|partnership|airdrop"
    r"|rewards?|holders earn|platform|protocol|launchpad|game|event)",
    re.I,
)


@dataclass(slots=True)
class Finding:
    title: str
    url: str
    snippet: str
    score: int = 0        # is this the right coin?
    story: int = 0        # does it actually explain anything?
    matched: str = ""
    is_price_page: bool = False

    @property
    def text(self) -> str:
        return f"{self.title} {self.snippet}".strip()

    @property
    def exact_identity(self) -> bool:
        """Whether this page is contract-bound rather than ticker-matched."""
        return self.matched in {
            "contract address",
            "project's own site",
            "Dexscreener/GMGN linked website or Telegram",
        }


@dataclass(slots=True)
class CoinLore:
    findings: list[Finding] = field(default_factory=list)
    queries_run: int = 0

    @property
    def confidence(self) -> str:
        best = max((f.score for f in self.findings if f.story > 0), default=0)
        if best >= 8:
            return "confirmed"
        if best >= 5:
            return "strong"
        if best >= 3:
            return "possible"
        return "unknown"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\*\*|__", "", value or "")).strip()


def parse_results(markdown: str) -> list[Finding]:
    """Pull title, destination and snippet out of a rendered DDG Lite page."""
    findings: list[Finding] = []
    pattern = re.compile(
        # DDG Lite renders each hit as three lines: the linked title, the
        # snippet, then the display URL. The snippet is the line we want.
        r"\d+\.\[([^\]]+)\]\(https://duckduckgo\.com/l/\?uddg=([^&\)]+)[^\)]*\)"
        r"[ \t]*\n?([^\n]*)"
    )
    for match in pattern.finditer(markdown):
        title = _clean(match.group(1))
        url = urllib.parse.unquote(match.group(2))
        snippet = _clean(match.group(3))
        if not title or not url.startswith("http"):
            continue
        findings.append(Finding(title=title, url=url, snippet=snippet))
    return findings


def _tokens(value: str) -> set[str]:
    return {word for word in re.split(r"[^a-z0-9]+", (value or "").lower()) if len(word) > 2}


def score_finding(finding: Finding, candidate: Candidate) -> int:
    """How much this result deserves to be believed about *this* coin.

    A ticker on its own proves nothing: several live coins share most tickers.
    The contract address is unforgeable, the project's own domain is close
    behind, and a full non-generic name is decent. Everything else is noise
    until something corroborates it.
    """
    token = candidate.token
    haystack = f"{finding.text} {finding.url}".lower()
    domain = urllib.parse.urlparse(finding.url).netloc.lower()

    score = 0
    matched = ""
    if token.mint and token.mint.lower() in haystack:
        score += 8
        matched = "contract address"
    finding.is_price_page = any(host in domain for host in PRICE_PAGE)
    name = _clean(token.name)
    if name and len(name) >= 5 and name.lower() in haystack:
        score += 4
        matched = matched or "token name"
    # A CJK ticker is a whole word and far less ambiguous than a Latin one.
    symbol = (token.symbol or "").strip()
    if symbol and symbol.lower() in haystack:
        score += 3 if not symbol.isascii() else 1
        matched = matched or "ticker"
    if any(host in f"{domain}{urllib.parse.urlparse(finding.url).path}" for host in EXPLANATORY):
        score += 2
    for social in token.socials or []:
        host = urllib.parse.urlparse(str((social or {}).get("url") or social or "")).netloc.lower()
        if host and host in domain:
            score += 5
            matched = matched or "project's own site"
    if token.chain_id and token.chain_id.lower() in haystack:
        score += 1
    finding.score = max(0, score)
    finding.matched = matched

    # Identity and information are different questions. A block explorer page
    # matching the contract address proves the coin is real and says nothing
    # about why anyone bought it; the recap needs the second thing.
    #
    # An explanation is required, not rewarded: without one, the result scores
    # zero however authoritative the domain. Otherwise every "$TICKER $674K
    # 0xabc..." shill post on X qualifies, because it carries the address and
    # sits on a domain we trust for commentary.
    story = 0
    if CAUSAL.search(finding.text):
        # An event is the answer to "why did it run". A description of the
        # token is only ever the answer to "what is it".
        story = 6
    elif STORY_WORDS.search(finding.text):
        story = 3
        if len(finding.snippet) >= 80:
            story += 1
        if any(host in domain for host in EXPLANATORY):
            story += 3
        if finding.is_price_page:
            story -= 4
        if BOILERPLATE.search(finding.text):
            story -= 4
        # Ticker-and-numbers soup: an address, a price and a rocket. Common on
        # X, and it is promotion rather than an account of anything.
        body = re.sub(r"[^a-z ]", " ", finding.text.lower())
        words = [w for w in body.split() if len(w) > 2]
        if len(words) < 8 or SHILL.search(finding.text):
            story -= 4
    finding.story = max(0, story)
    return finding.score


def query_ladder(candidate: Candidate) -> list[str]:
    """Most specific question first, so a strong answer ends the search early."""
    token = candidate.token
    chain = {"bsc": "BNB Chain", "base": "Base", "solana": "Solana", "ethereum": "Ethereum"}.get(
        (token.chain_id or "").lower(), token.chain_id or ""
    )
    name = _clean(token.name)
    symbol = _clean(token.symbol)
    queries: list[str] = []
    if token.mint:
        queries.append(f'"{token.mint}"')
    # A descriptive name is the single best search term we hold: "World
    # Humanoid Robot Games" finds the event, "WHRG" finds nothing.
    if name and name.lower() != symbol.lower() and len(name) >= 5:
        queries.append(f'"{name}" {chain} token')
    if symbol:
        queries.append(f"{symbol} {chain} meme coin")
        # The other queries ask what the coin is. This one asks what happened,
        # which is the sentence the recap is actually missing. People write
        # "why is X pumping" and search engines index the answers.
        queries.append(f"why is {symbol} pumping {chain}")
    return [q for q in queries if q.strip()][:4]


class LoreResearcher:
    def __init__(self, settings: Settings) -> None:
        section = settings.section("lore")
        self.enabled = bool(section.get("enabled", True))
        self.limit = int(section.get("max_coins", 15) or 15)
        self.min_score = int(section.get("min_score", 5) or 5)
        self.timeout = float(section.get("timeout_seconds", 45) or 45)
        self.concurrency = int(section.get("concurrency", 2) or 2)
        self.pause = float(section.get("pause_seconds", 1.5) or 1.5)
        self.per_coin_queries = int(section.get("queries_per_coin", 3) or 3)

    async def search(self, client: httpx.AsyncClient, query: str) -> list[Finding]:
        url = READER + DDG_LITE + urllib.parse.quote(query)
        try:
            response = await client.get(url, headers=UA, timeout=self.timeout)
            if response.status_code != 200:
                log.info("lore_search_status code=%s query=%s", response.status_code, query[:60])
                return []
            return parse_results(response.text)
        except httpx.HTTPError as exc:
            log.info("lore_search_failed query=%s error=%s", query[:60], type(exc).__name__)
            return []

    async def linked_pages(self, client: httpx.AsyncClient, candidate: Candidate) -> list[Finding]:
        """Read project-owned websites and public Telegram pages before search.

        Exact X posts are resolved earlier by ``attach_linked_x_posts``.  This
        is the second evidence lane: URLs supplied by Dexscreener/GMGN itself,
        including ``info.websites`` retained in the raw pair payload.
        """
        raw_info = (candidate.token.raw or {}).get("info") or {}
        links: list[str] = []
        for item in list(candidate.token.socials or []) + list(raw_info.get("websites") or []):
            url = str((item or {}).get("url") if isinstance(item, dict) else item or "").strip()
            if not url or re.search(r"(?:x|twitter)\.com/", url, re.I) or url in links:
                continue
            links.append(url)
        findings: list[Finding] = []
        for url in links[:3]:
            try:
                response = await client.get(READER + url, headers=UA, timeout=self.timeout)
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            lines = [_clean(line.lstrip("#>*- ")) for line in response.text.splitlines()]
            prose = [line for line in lines if len(line.split()) >= 6 and not looks_like_page_furniture(line)]
            snippet = " ".join(prose[:3])[:700]
            if not snippet or not STORY_WORDS.search(snippet):
                continue
            finding = Finding(title=candidate.token.name, url=url, snippet=snippet)
            finding.score = 7
            finding.story = 5 if CAUSAL.search(snippet) else 3
            finding.matched = "Dexscreener/GMGN linked website or Telegram"
            findings.append(finding)
        return findings

    async def research(self, client: httpx.AsyncClient, candidate: Candidate) -> CoinLore:
        lore = CoinLore()
        seen: set[str] = set()
        # Priority 2, after exact linked X: project-linked web and Telegram.
        lore.findings.extend(await self.linked_pages(client, candidate))
        if any(f.score >= 5 and f.story >= 5 for f in lore.findings):
            return lore
        seen.update(f.url for f in lore.findings)
        # Priority 3: broader exact-contract/name search.
        for query in query_ladder(candidate)[: self.per_coin_queries]:
            results = await self.search(client, query)
            lore.queries_run += 1
            for finding in results:
                if finding.url in seen:
                    continue
                seen.add(finding.url)
                score_finding(finding, candidate)
                if looks_like_page_furniture(finding.text):
                    continue
                # A ticker or even a reused name is only a search lead. It can
                # never become published lore unless the result itself is tied
                # to the exact contract or to a URL attached to that contract's
                # market profile.
                if finding.exact_identity and finding.score >= self.min_score and finding.story > 0:
                    lore.findings.append(finding)
            # Stop only when something both identifies the coin and explains
            # it. Identity alone is not worth ending the search for: the
            # contract-address query reliably finds price pages, and the story
            # almost always comes from the descriptive-name query after it.
            if any(f.score >= 5 and f.story >= 5 for f in lore.findings):
                break
            await asyncio.sleep(self.pause)
        lore.findings.sort(key=lambda f: (f.story, f.score), reverse=True)
        return lore


async def profile_descriptions(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    """The projects' own descriptions, published free by Dexscreener."""
    try:
        response = await client.get(DEX_PROFILES, headers=UA, timeout=20)
        if response.status_code != 200:
            return {}
        rows = response.json()
    except (httpx.HTTPError, ValueError):
        return {}
    profiles: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        address = str(row.get("tokenAddress") or "")
        if not address:
            continue
        profiles[address.lower()] = {
            "description": _clean(str(row.get("description") or ""))[:400],
            "links": [str(link.get("url") or "") for link in (row.get("links") or []) if link.get("url")][:3],
            "cto": bool(row.get("cto")),
        }
    return profiles


async def attach_lore(coins: list[Candidate], settings: Settings) -> int:
    """Find and attach the story behind the day's biggest coins.

    Findings land in `news_evidence`, which the writer already reads, so no
    other part of the pipeline needs to know where they came from.
    """
    researcher = LoreResearcher(settings)
    if not researcher.enabled or not coins:
        return 0
    # Priority 1 is exact linked X. Coins that already have that stronger
    # evidence do not get overwritten by weaker website/search snippets.
    targets = [coin for coin in coins if not coin.x_interactions][: researcher.limit]
    semaphore = asyncio.Semaphore(researcher.concurrency)
    found = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        profiles = await profile_descriptions(client)

        async def one(candidate: Candidate) -> None:
            nonlocal found
            profile = profiles.get((candidate.token.mint or "").lower())
            if profile and profile.get("description"):
                candidate.news_evidence.append({
                    "kind": "lore",
                    "source": "project profile",
                    "summary": profile["description"],
                    "url": (profile.get("links") or [""])[0],
                    "confidence": "confirmed",
                })
                if profile.get("cto"):
                    candidate.provider_evidence.setdefault("gmgn", {})["ctoFlag"] = True
                found += 1
                return
            async with semaphore:
                lore = await researcher.research(client, candidate)
            if not lore.findings:
                return
            best = lore.findings[0]
            candidate.news_evidence.append({
                "kind": "lore",
                "source": urllib.parse.urlparse(best.url).netloc,
                "summary": best.snippet or best.title,
                "url": best.url,
                "confidence": lore.confidence,
                "matchedOn": best.matched,
            })
            found += 1

        await asyncio.gather(*(one(c) for c in targets), return_exceptions=True)
    return found
