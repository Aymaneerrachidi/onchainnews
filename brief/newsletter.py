"""The recap, written the way a person would write it.

The template renderer produces a correct list and an obviously mechanical one:
every row the same shape, ordered by a number. What the client actually reads
each morning is grouped by story -- the Coinbase listing, the Chinese film, the
coins one trader's platform pulled up with it -- and each line says why the coin
moved, not merely that it did.

Grouping and phrasing are judgement, so a model does them. The facts are not:
every number, handle and link handed to the model comes from the day's own
evidence, and the prompt forbids adding anything else. A newsletter that invents
a Coinbase announcement is worse than no newsletter, so the failure mode is
always to fall back to the deterministic template rather than to guess.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx

from brief.config import Settings
from brief.journal import kol_trade_count
from datetime import datetime

from brief.models import Candidate
from brief.render.formatting import money

log = logging.getLogger("brief.newsletter")

API_URL = "https://api.openai.com/v1/chat/completions"
COHERE_URL = "https://api.cohere.com/v2/chat"


class NewsletterError(RuntimeError):
    """Raised when the model cannot produce a usable recap."""


def configured() -> bool:
    """Whether any writer is available. Research needs OpenAI specifically."""
    return bool(
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("COHERE_API_KEY", "").strip()
    )


def research_configured() -> bool:
    """Only OpenAI can search the web for us; Cohere dropped connectors in 2025."""
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _peak(candidate: Candidate) -> float:
    return max(
        float(candidate.peak_market_cap or 0),
        float(candidate.observed_peak_market_cap or 0),
        float(candidate.token.market_cap or 0),
    )


ID_LEAK = re.compile(r"\s*[\(\[]\s*c\d{2}\s*[\)\]]")


def _strip_ids(text: str) -> str:
    """Remove the internal coin handles the model writes into prose.

    Coins are addressed as c01, c02 so the model never has to emit a
    non-Latin ticker. Those handles are plumbing and occasionally survive
    into a sentence as "Memestock (c03)".
    """
    cleaned = ID_LEAK.sub("", text or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _age_words(hours: float | None) -> str | None:
    """"41 hours old", "6 days old": a string the model copies rather than a
    number it converts, after it turned 40.8 hours into "less than a day"."""
    if hours is None or hours <= 0:
        return None
    if hours < 48:
        return f"{hours:.0f} hours old"
    return f"{hours / 24:.0f} days old"


def _coin_facts(candidate: Candidate, settings: Settings) -> dict[str, Any]:
    """Everything known about one coin, and nothing that is not known."""
    token = candidate.token
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    facts: dict[str, Any] = {
        "id": "",  # assigned by build_facts; the model answers in ids, not tickers
        "symbol": token.symbol,
        "name": token.name,
        "chain": token.chain_id,
        "peak": money(_peak(candidate)),
        "volume24h": money(token.volume_24h),
        "age": _age_words(candidate.signals.age_hours),
        # Formatted, because the model repeats these verbatim and "139274
        # holders" reads like a machine wrote it.
        "holders": f"{candidate.safety.holder_count:,}" if candidate.safety.holder_count else None,
    }
    traded = kol_trade_count(candidate)
    if traded > 0:
        facts["trackedWalletsTraded"] = traded
    lifecycle = candidate.provider_evidence.get("lifecycle", {}) or {}
    drawdown = candidate.drawdown_from_peak_pct
    if lifecycle.get("peakIsSingleObservation"):
        # Say so explicitly, so the writer cannot read a missing drawdown as a
        # coin holding its high.
        facts["peakConfidence"] = "seen once; the high shown is simply where we found it"
    elif drawdown is not None and drawdown >= 40:
        facts["gaveBackPct"] = round(float(drawdown))
    elif drawdown is not None and drawdown <= 5:
        facts["stillNearHigh"] = True

    # A transfer tax changes whether a coin is worth touching at all.
    fee = float(gmgn.get("totalFee") or 0)
    fee_chains = {str(c).lower() for c in (settings.section("journal").get("fee_check_chains", []) or [])}
    if token.chain_id.lower() in fee_chains and 1 <= fee <= 50:
        facts["transferTaxPct"] = round(fee, 1)
    if candidate.lore:
        facts["meta"] = candidate.lore
    if token.socials:
        facts["socials"] = [str(link) for link in token.socials][:3]
    # Labels that are true of nearly every coin on a chain are noise, not risk.
    # Sending them invites the model to pad every line with the same clause.
    noise = (
        "unknown", "no contract safety source", "dexscreener boost", "only ",
        # True of half the board on any given day; prose built on them reads
        # like a disclaimer, not a recap.
        "ticker also used", "also used by", "no linked social", "recycled",
    )
    real_risks = [
        label for label in candidate.risk_labels
        if not any(term in label.lower() for term in noise)
    ]
    if real_risks:
        facts["risks"] = real_risks[:3]
    if gmgn.get("ctoFlag"):
        facts["communityTakeover"] = True
    if gmgn.get("twitterRenameCount"):
        facts["projectXAccountRenamedTimes"] = int(gmgn["twitterRenameCount"])
    if gmgn.get("launchpad"):
        facts["launchpad"] = str(gmgn["launchpad"])

    news = []
    for item in (candidate.news_evidence or [])[:3]:
        summary = str(item.get("summary") or "").strip()
        if summary:
            news.append({"summary": summary[:240], "url": item.get("url") or ""})
    if news:
        facts["news"] = news

    posts = []
    for item in (candidate.x_interactions or [])[:3]:
        posts.append({
            "handle": item.author_handle,
            "what": item.interaction,
            "summary": (item.summary or "")[:200],
            "url": item.url,
        })
    if posts:
        facts["xPosts"] = posts
    return facts


def recap_coins(runners: list[Candidate], tape: list[Candidate], limit: int) -> list[Candidate]:
    """The day's coins, biggest peak first, each appearing once."""
    seen: set[str] = set()
    ordered: list[Candidate] = []
    for candidate in sorted([*(tape or []), *(runners or [])], key=_peak, reverse=True):
        if candidate.token.mint in seen:
            continue
        seen.add(candidate.token.mint)
        ordered.append(candidate)
        if len(ordered) >= limit:
            break
    return ordered


def build_facts(coins: list[Candidate], generated_at: datetime, settings: Settings) -> dict[str, Any]:
    limit = int(settings.get("newsletter", "max_coins", 30) or 30)
    rows = [_coin_facts(candidate, settings) for candidate in coins[:limit]]
    for index, row in enumerate(rows, start=1):
        row["id"] = f"c{index:02d}"
    return {"date": generated_at.strftime("%B %d, %Y"), "coins": rows}


SYSTEM_PROMPT = """You write a daily on-chain memecoin recap for a trading streamer's audience.

Group the day's coins by the story behind them and say what happened, in the
voice of someone who watched the tape all day.

FACTS
- Use only the JSON given to you. Every number, handle, link and claim must come
  from it.
- Never invent news, posts, listings, partnerships, people or reasons. If a coin
  has no news attached, say only what the on-chain facts support.
- No price predictions. Never tell anyone to buy or sell.
- If a coin carries `peakConfidence`, you may state its high but NEVER say it
  is holding, at, or near that high, and never imply the move stuck. We saw it
  once and do not know what it did around that point.
- If a coin carries `transferTaxPct`, say so plainly. A tax on every trade is
  the most useful thing a reader can know about that coin.
- Do not guess what a ticker refers to or which meta it belongs to. "Pepe-cat
  crossover", "the wif variant", "creator-capital tape" are inventions unless
  the JSON's `meta`, `name` or `news` says so. If you do not know what a coin
  is, it is a coin that ran, and that is all you may call it.
- Group by something the JSON supports -- same chain, same age, same ticker
  language, a shared meta field, the same news. Never group on a resemblance
  you inferred yourself and then describe that resemblance as fact.
- Quote each peak using the exact `peak` string provided, and each age using
  the exact `age` string. Never restate an age in other units or round it:
  "41 hours old" is not "less than a day old".

GROUPING
- Build 2 to 6 sections, however many the day actually contains. Never split
  a story or repeat a coin just to reach a number. A section is a story several coins share: a chain's
  meta, a launchpad, a takeover wave, a ticker theme, a news event.
- Title each one like a headline a person wrote, not a category label. "Time To
  Learn Chinese" beats "Chinese Tokens". Do not number them.
- One coin can be its own section if it is the whole story.
- Sections should not all be the same size.

LINES
- The ticker is already printed for you. NEVER start the line with the ticker
  and never repeat it. Start at "hit <peak>" or at the reason.
- Vary every line. Never open two lines in a row the same way, and do not use
  the same construction more than twice in the whole email.
- Say the most interesting true thing, not the most available one. Volume and
  holder counts are the dullest facts you have: use them only when the number
  is genuinely remarkable.
- "hit <peak>, down <n>% from peak" is a template, and a template is the one
  thing this recap must never read like. Mention `gaveBackPct` or
  `stillNearHigh` only where it is the point of the line. Good lines look like:
    "hit $4.7M on the back of the film meme, and has barely given any back"
    "a 12-hour-old launch that reached $1.9M before 77% of it unwound"
    "hit $1.5M; 77 tracked wallets were in it, the most on the board"
    "hit $621k"
  A short line is fine. A repeated shape is not.

BULLETS
- Bullets are for stories, never for risk lists. NEVER write a bullet that
  enumerates risk labels.
- At most two per section, and only for something a reader could not guess: a
  news summary, a post with its url, a community takeover, a project account
  that was renamed, a coin that gave nearly all of its gain back.
- Never repeat a point already made anywhere in the email.
- A bullet that could be written about any group of coins ("varying degrees of
  success", "a range of performances", "some faded") is filler. Write no
  bullet rather than that bullet.

RISK
- Mention risk only where it changes how the coin should be read: a coin that
  round-tripped, a live rug flag, supply in very few hands.
- NEVER write "LP lock unknown", "concentration unknown", "no contract safety
  source" or "active Dexscreener boost". Those are missing data or advertising,
  true of most coins on that chain, and they belong nowhere in the prose.
- Never append a "risks:" clause to a line.

STYLE
- No emoji. No hype adjectives. No sign-off. Short sentences.

IDENTIFY EVERY COIN BY ITS `id` FIELD ("c01", "c02", ...) in the `id` field
only. The id is a handle for you and me, not for the reader: it must never
appear inside a `line`, a `bullet`, a `title` or the intro. Write "Memestock",
not "Memestock (c03)".
Some tickers are not written in the Latin alphabet and must not appear in your
output at all; the ticker is printed next to the line for you.

Return JSON exactly like this:
{"intro": "one short line setting up the day",
 "sections": [{"title": "...",
               "coins": [{"id": "c01", "line": "..."}],
               "bullets": ["...", "..."]}]}
"""


RESEARCH_URL = "https://api.openai.com/v1/responses"

RESEARCH_PROMPT = """You are a research assistant for an on-chain memecoin desk.

Search the web for what this specific token is and why it moved in the last two
days. You are looking for the story a trader would tell: what the name refers
to, a viral event behind it, an exchange listing, a well-known account that
posted about it, a community takeover.

Rules:
- Search before answering. Do not answer from memory.
- Be certain it is the same token: match the contract address, or the ticker
  together with the chain. Coins reuse tickers constantly, and the wrong coin's
  story is worse than no story.
- If you cannot find anything specific and verifiable, say so. An empty answer
  is correct and expected for most memecoins.
- Do not describe price action, market cap or charts. We already have those.

Return JSON only:
{"found": true|false,
 "what_it_is": "one sentence, or empty",
 "why_it_moved": "one sentence about the last two days, or empty",
 "sources": ["url", "url"]}"""


async def _research_one(client: httpx.AsyncClient, candidate: Candidate, model: str) -> None:
    token = candidate.token
    question = chr(10).join([
        f"Token ticker: {token.symbol}",
        f"Name: {token.name}",
        f"Chain: {token.chain_id}",
        f"Contract address: {token.mint}",
        f"Links on the pair: {', '.join(str(link) for link in (token.socials or [])[:3]) or 'none'}",
    ])
    request = {
        "model": model,
        "tools": [{"type": "web_search"}],
        "instructions": RESEARCH_PROMPT,
        "input": question,
    }
    headers = {
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY'].strip()}",
        "Content-Type": "application/json",
    }
    body = None
    # Search calls run in parallel and the account's limit is shared, so a 429
    # is expected rather than exceptional. Backing off once recovers the coin's
    # story instead of silently publishing it without one.
    for attempt in range(3):
        try:
            response = await client.post(RESEARCH_URL, headers=headers, json=request)
            if response.status_code == 429:
                if "insufficient_quota" in response.text or "credit" in response.text.lower():
                    log.warning("research_stopped reason=openai_credits_exhausted")
                    return
                if attempt < 2:
                    await asyncio.sleep(4 * (attempt + 1))
                    continue
            response.raise_for_status()
            body = response.json()
            break
        except httpx.HTTPError as exc:
            if attempt == 2:
                log.warning("research_failed mint=%s error=%s", token.mint, exc)
                return
            await asyncio.sleep(2 * (attempt + 1))
    if body is None:
        return

    text = "".join(
        part.get("text", "")
        for item in body.get("output", [])
        for part in (item.get("content") or [])
        if part.get("type") == "output_text"
    ).strip()
    if not text:
        return
    if text.startswith("```"):
        text = text.strip("`").split(chr(10), 1)[-1]
    try:
        found = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return
    if not found.get("found"):
        return
    what = str(found.get("what_it_is") or "").strip()
    why = str(found.get("why_it_moved") or "").strip()
    if not what and not why:
        return
    candidate.news_evidence.append({
        "kind": "research",
        "source": "web search",
        "summary": " ".join(part for part in (what, why) if part)[:400],
        "url": (found.get("sources") or [""])[0],
        "sources": [str(u) for u in (found.get("sources") or [])[:3]],
    })


async def research_day(coins: list[Candidate], settings: Settings) -> int:
    """Find the real story behind the day's biggest coins.

    The writer is forbidden from inventing lore, which leaves it describing
    price action. This fills that gap with searched, cited material: what the
    name refers to, the viral clip behind it, the listing, the account that
    posted. Anything not found stays absent -- a coin with no story is simply a
    coin that ran.
    """
    if not bool(settings.get("newsletter", "research_enabled", False)) or not research_configured():
        return 0
    model = str(settings.get("newsletter", "research_model", "gpt-5.5"))
    timeout = float(settings.get("newsletter", "research_timeout_seconds", 180))
    targets = coins[:int(settings.get("newsletter", "research_limit", 12) or 12)]

    semaphore = asyncio.Semaphore(int(settings.get("newsletter", "research_concurrency", 4) or 4))

    async with httpx.AsyncClient(timeout=timeout) as client:
        async def guarded(candidate: Candidate) -> None:
            async with semaphore:
                await _research_one(client, candidate, model)

        await asyncio.gather(*(guarded(c) for c in targets), return_exceptions=True)

    return sum(
        1 for candidate in targets
        if any(item.get("kind") == "research" for item in candidate.news_evidence)
    )


async def write_recap(coins: list[Candidate], generated_at: datetime, settings: Settings, *, transport=None) -> dict[str, Any] | None:
    """Ask a model to group and phrase the day. None means use the template.

    The configured provider goes first; if it fails for any reason -- quota,
    a 422 on an odd payload, a malformed answer -- the other provider is tried
    before giving up, so one vendor's bad morning does not strip the recap.
    """
    if not bool(settings.get("newsletter", "enabled", False)):
        return None
    if not configured():
        log.info("newsletter_skipped reason=no_writer_key")
        return None
    facts = build_facts(coins, generated_at, settings)
    if not facts["coins"]:
        return None

    preferred = str(settings.get("newsletter", "provider", "openai")).strip().lower()
    order = [preferred] + [p for p in ("openai", "cohere") if p != preferred]
    for provider in order:
        key_name = "COHERE_API_KEY" if provider == "cohere" else "OPENAI_API_KEY"
        if not os.environ.get(key_name, "").strip():
            continue
        written = await _write_with(provider, facts, settings, transport=transport)
        if written:
            return written
        log.info("newsletter_provider_failed provider=%s trying_next=true", provider)
    return None


async def _write_with(provider: str, facts: dict[str, Any], settings: Settings, *, transport=None) -> dict[str, Any] | None:
    timeout = float(settings.get("newsletter", "timeout_seconds", 120))
    user_message = json.dumps(facts, ensure_ascii=False)
    if provider == "cohere":
        model = str(settings.get("newsletter", "cohere_model", "command-a-plus-05-2026"))
        url, key_name = COHERE_URL, "COHERE_API_KEY"
        payload = {
            "model": model,
            "response_format": {"type": "json_object"},
            # This model reasons before answering and, on a full day's coins,
            # spends the entire budget doing it and returns an empty answer.
            # The recap is a formatting job, not a reasoning one.
            "thinking": {"type": "disabled"},
            "max_tokens": int(settings.get("newsletter", "cohere_max_tokens", 8000) or 8000),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        }
    else:
        model = str(settings.get("newsletter", "model", "gpt-5.5"))
        url, key_name = API_URL, "OPENAI_API_KEY"
        payload = {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        }
    key = os.environ.get(key_name, "").strip()
    if not key:
        log.info("newsletter_skipped reason=no_%s", key_name.lower())
        return None

    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        if provider == "cohere":
            # Reasoning models answer with a thinking block before the text one.
            content = "".join(
                part.get("text", "")
                for part in (body.get("message", {}) or {}).get("content", [])
                if part.get("type") == "text"
            )
        else:
            content = body["choices"][0]["message"]["content"]
        recap = json.loads(content)
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        log.warning(
            "newsletter_failed provider=%s model=%s error=%s(%s)",
            provider, model, type(exc).__name__, exc,
        )
        return None

    sections = recap.get("sections")
    if not isinstance(sections, list) or not sections:
        return None

    # The model may only arrange coins the day actually produced. Anything it
    # names that we did not hand it is dropped rather than published.
    story_keys = ("news", "xPosts", "communityTakeover", "projectXAccountRenamedTimes")
    has_story = {
        str(coin["symbol"]).upper(): any(coin.get(key) for key in story_keys)
        for coin in facts["coins"]
    }
    by_id = {coin["id"]: str(coin["symbol"]) for coin in facts["coins"]}
    by_symbol = {str(coin["symbol"]).upper(): str(coin["symbol"]) for coin in facts["coins"]}
    clean: list[dict[str, Any]] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        coins = []
        for item in section.get("coins") or []:
            if not isinstance(item, dict):
                continue
            # Accept an id, or a ticker from a model that ignored the instruction.
            symbol = by_id.get(str(item.get("id", "")).strip().lower()) or by_symbol.get(
                str(item.get("symbol", "")).strip().upper()
            )
            if symbol:
                line = str(item.get("line", "")).strip()
                # The renderer prints the ticker; a model that starts the line
                # with it anyway produces "$CC CC hit". Remove it here, once.
                for prefix in (f"${symbol}", symbol):
                    if line.lower().startswith(prefix.lower()):
                        line = line[len(prefix):].lstrip(" :,-—–")
                        break
                coins.append({"symbol": symbol, "line": _strip_ids(line)})
        if not coins:
            continue
        bullets = [_strip_ids(str(b)) for b in (section.get("bullets") or []) if str(b).strip()][:2]
        if not any(has_story.get(c["symbol"].upper()) for c in coins):
            bullets = []
        clean.append({
            "title": _strip_ids(str(section.get("title") or ""))[:80],
            "coins": coins,
            "bullets": bullets,
        })
    if not clean:
        return None
    dropped = sum(len(s.get("coins") or []) for s in sections) - sum(len(s["coins"]) for s in clean)
    if dropped:
        log.info("newsletter_dropped_unknown_coins count=%s", dropped)
    return {
        "intro": _strip_ids(str(recap.get("intro") or ""))[:240],
        "sections": clean,
        "writer": f"{provider}/{model}",
    }
