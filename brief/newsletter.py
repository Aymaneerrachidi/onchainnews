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
from brief.sources.gmgn import transfer_tax_pct
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
    gmgn = candidate.provider_evidence.get("gmgn", {}) or {}
    return max(
        float(candidate.peak_market_cap or 0),
        float(candidate.observed_peak_market_cap or 0),
        float(gmgn.get("kline24hPeakMarketCap") or 0),
        float(candidate.token.market_cap or 0),
    )


def newsletter_coin_limit(settings: Settings) -> int:
    """Return the publishing cap; zero means the complete verified runner set."""
    return max(0, int(settings.get("newsletter", "max_coins", 0) or 0))


ID_LEAK = re.compile(r"\s*[\(\[]\s*c\d{2}\s*[\)\]]")


MONEY_LEAD = re.compile(
    r"^(hit|reached|topped at|peaked at|touched)\s+\$[\d.,]+\s*[kKmMbB]?\s*[,;:-]?\s*",
    re.I,
)


def _drop_leading_peak(line: str) -> str:
    """The peak is printed beside the ticker, so a line that opens with it
    spends its only sentence saying what the reader can already see."""
    trimmed = MONEY_LEAD.sub("", line or "").strip()
    if not trimmed:
        return line
    return trimmed[0].upper() + trimmed[1:] if trimmed[:1].islower() else trimmed


def _words(text: str) -> set[str]:
    return {
        word for word in re.split(r"[^a-z0-9$]+", (text or "").lower())
        if len(word) > 3
    }


def _is_restatement(bullet: str, lines: list[str]) -> bool:
    """Whether a bullet just says a coin line again in other words.

    Bullets exist to add what the lines cannot hold. A section whose bullets
    repeat its own rows reads as padding, which is exactly how a machine
    writes when it has nothing further to say.
    """
    bullet_words = _words(bullet)
    if not bullet_words:
        return True
    for line in lines:
        line_words = _words(line)
        if not line_words:
            continue
        shared = len(bullet_words & line_words) / len(bullet_words)
        if shared >= 0.6:
            return True
    return False


PROFIT_CLAIM = re.compile(
    r"(turn(ed|ing)?\s+\$[\d.,]+\s*[kKmM]?\s+into"
    r"|\d+\s*x\s*(return|gain|profit|on his|on her|for one)"
    r"|made\s+\$[\d.,]+\s*[kKmM]?"
    r"|pnl|profit of \$|bagged|cashed out"
    r"|\$[\d.,]+\s*[kKmM]?\s+(in profit|profit|gain))",
    re.I,
)


def _is_profit_claim(text: str) -> bool:
    """Whether a bullet is somebody advertising their own trade.

    A stranger saying they turned $99 into $600k is not an event, and printing
    it with "claims" attached only launders it. These posts are the most
    engaged-with thing about a running coin, so they arrive constantly.
    """
    return bool(PROFIT_CLAIM.search(text or ""))


ATTRIBUTED = re.compile(
    r"(https?://|@\w|according to|says|said|posted|claims?"
    r"|announced|listed on|added to|described|reported|tied to|named after"
    r"|launched by|created by|per a|write[sr]?)",
    re.I,
)


def _carries_evidence(text: str) -> bool:
    """Whether a bullet brings something the coin rows cannot.

    A bullet exists to add outside material: a source, a person, a link, an
    event. Restating a row's own numbers in a sentence is padding, and word
    overlap does not catch it once the row has been trimmed. Requiring
    attribution does.
    """
    return bool(ATTRIBUTED.search(text or ""))


NO_DATA = re.compile(
    r"[,;]?\s*(with )?no (news|story|lore|evidence|posts?)( attached| found| today)?"
    r"|[,;]?\s*nothing (found|attached)",
    re.I,
)


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
        # The mint is the identity. Tickers are presentation only and are
        # routinely reused by unrelated (and sometimes malicious) launches.
        "mint": token.mint,
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
    fee = transfer_tax_pct(gmgn)
    fee_chains = {str(c).lower() for c in (settings.section("journal").get("fee_check_chains", []) or [])}
    if token.chain_id.lower() in fee_chains and fee is not None and 1 <= fee <= 50:
        facts["transferTaxPct"] = round(fee, 1)
    cause = (candidate.provider_evidence.get("why", {}) or {}).get("cause")
    if cause:
        facts["whyItRan"] = cause
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
        # Our own gate vocabulary. Meaningless to a reader and it leaked into
        # a published line as "liquidity below publisher floor".
        "publisher floor", "publisher ceiling", "organic confirmations",
        "did not qualify", "below floor", "lore ", "has run before",
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
            "summary": (item.summary or "")[:160],
            "url": item.url,
        })
    if posts:
        facts["xPosts"] = posts
    return facts


def recap_coins(runners: list[Candidate], tape: list[Candidate], limit: int) -> list[Candidate]:
    """The public recap candidates, biggest peak first and approved only.

    The headline tape is useful for ordering, but it can contain candidates
    that the safety/editorial pass rejected. It must never broaden the public
    set. Previously a blocked DOPAMEME entered the writer through this merge,
    then rendered without a market cap because the email correctly could not
    resolve it among the approved runners.
    """
    approved = {candidate.token.mint: candidate for candidate in (runners or [])}
    seen: set[tuple[str, str]] = set()
    ordered: list[Candidate] = []
    for candidate in sorted([*(tape or []), *(runners or [])], key=_peak, reverse=True):
        candidate = approved.get(candidate.token.mint)
        if candidate is None:
            continue
        identity = (candidate.token.chain_id.strip().lower(), candidate.token.mint.strip().lower())
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(candidate)
        if limit > 0 and len(ordered) >= limit:
            break
    return ordered


def build_facts(coins: list[Candidate], generated_at: datetime, settings: Settings) -> dict[str, Any]:
    limit = newsletter_coin_limit(settings)
    selected = coins[:limit] if limit > 0 else coins
    rows = [_coin_facts(candidate, settings) for candidate in selected]
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
- EVERY coin in the JSON must appear in exactly one section. A coin with no
  story still ran and still belongs in the recap; put it with the coins it most
  resembles, or in a final catch-all section, and give it its peak and one true
  detail. Dropping a coin is the one unrecoverable mistake here.
- Build 3 to 6 sections. Most must hold two or more coins: the point of a
  section is that several coins share a story. A single-coin section is allowed
  only when that coin genuinely was the day's event, and at most twice.
- Never title a section after a coin. "Baby Catecoin" is a label; "Cate Down
  50% From Today's High" is what happened. If you cannot say what happened,
  group by the meta and title that. A section is a story several coins share: a chain's
  meta, a launchpad, a takeover wave, a ticker theme, a news event.
- Title each one like a headline a person wrote, not a category label. "Time To
  Learn Chinese" beats "Chinese Tokens". Do not number them.
- One coin can be its own section if it is the whole story.
- Sections should not all be the same size.

LINES
- The peak is printed beside the ticker already. Do NOT write "hit $5.4M"
  in the line as well; spend the line on what happened instead.
- The ticker is already printed for you. NEVER start the line with the ticker
  and never repeat it. Start at "hit <peak>" or at the reason.
- Vary every line. Never open two lines in a row the same way, and do not use
  the same construction more than twice in the whole email.
- Never narrate the absence of evidence. "no news attached", "no proof
  provided", "no new news today" are notes to me about our data, not sentences
  for a reader. If there is nothing to say beyond the numbers, say the numbers.
- Do not repeat someone's profit claim as a fact or as gossip. A stranger
  saying they turned $99 into $600k is not an event.
- When a coin has `whyItRan`, that is the line. Lead with it. It is the
  reason the reader opened the email.
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

VOICE
This is written by a trader who watched the tape all day, for people who were
also watching. Not a press release and not an analyst note.

- Short declarative sentences. Contractions. No hype adjectives, no emoji, no
  "notably", "impressively", "showcasing", "amid", "sentiment".
- Say the thing plainly: "huge dump in the last hour, no clear answers yet"
  beats "experienced a significant retracement".
- Admit what you do not know rather than papering over it. "idek how to explain
  this" is a real line from the recap this imitates, and it is better than an
  invented reason.
- The intro is one line about how THIS day felt, not a summary of the sections.
  Write it fresh from today's coins.
- Every example in these instructions shows you the REGISTER, never text to
  reuse. Copying a sample sentence into your answer is the worst thing you can
  do here: it makes the recap a form letter. If a phrase appears in these
  instructions, you may not use it.
- Section titles are statements about what happened, not labels: "Cate Down 50%
  From Today's High", "GTA 6 Leaks", "Cat Meta", "Major Memes Ripping". Never
  "Solana's Movers" or "BSC's Mixed Bag".
- Name people by their handle when the evidence names them, and link the post.
- The final bullet of the LAST section must be one open question about whatever
  the day left hanging -- what a reader would actually wonder tomorrow morning.
  This is required, and it is the only bullet that may exist without evidence
  behind it.

WORTH SAYING, IN ORDER
1. What the coin is or refers to, when the evidence says.
2. What happened today that a reader would not already know: a listing, a post,
   a takeover, a dump with no explanation, a burn, a leak.
3. Who was involved, by handle.
4. The peak, always.
5. Everything else, only if it changes the read.

Return JSON exactly like this:
{"intro": "one short line setting up the day",
 "sections": [{"title": "...",
               "coins": [{"id": "c01", "line": "..."}],
               "bullets": ["...", "..."]}]}
"""


RESEARCH_URL = "https://api.openai.com/v1/responses"

RESEARCH_PROMPT = """You are a research assistant for an on-chain memecoin desk.

Research this specific token deeply and determine what it is and why it moved
in the last two days. Start with the exact contract address, then inspect its
linked website and social profiles, and search the exact name/ticker together
with the chain. Look for the original meme, person, clip or real-world event,
as well as listings, product launches, burns, takeovers and credible posts.

Rules:
- Search before answering. Do not answer from memory.
- Open promising results rather than summarising search-result snippets. Check
  several independent paths when the first result is thin.
- Be certain it is the same token: match the contract address, or the ticker
  together with the chain. Coins reuse tickers constantly, and the wrong coin's
  story is worse than no story.
- At least one source must establish token identity. A generic article about a
  word or meme does not prove that this contract is its token.
- Do not treat bullishness, an entry, a price target, a call-group claim or an
  influencer saying they bought as news. Find a checkable fact or return empty.
- If you cannot find anything specific and verifiable, say so. An empty answer
  is correct and expected for most memecoins.
- Do not describe price action, market cap or charts. We already have those.
- Treat X posts as evidence, not copy. Never quote or lightly trim a post,
  profile bio, search snippet, engagement counters, or contract dump.
- Synthesize the context in fresh language: explain the meme/project first,
  then the concrete reason it mattered today. Write like a trader briefing a
  friend after following the tape, not like a search engine or press release.
- Give every coin its own sentence structure. Do not reuse stock openings such
  as "the move came from", "exact-contract research", or "X posts show".
- If the evidence is only trading chatter, calls, or an attached market page,
  return found=false instead of manufacturing lore from it.

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
    only_without_x = bool(settings.get("newsletter", "research_only_without_x", False))
    targets = [candidate for candidate in coins if not only_without_x or not candidate.x_interactions]
    limit = int(settings.get("newsletter", "research_limit", 12) or 0)
    if limit > 0:
        targets = targets[:limit]

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



EXPLAIN_PROMPT = """You are given coins that ran today, each with the evidence we
could find: searched descriptions, news items, and posts from accounts with reach.

For each coin, say WHY it ran, in one short sentence.

RULES
- Use only the evidence given for that coin. Never guess, never generalise from
  the ticker, never write market commentary.
- A cause is an event or a fact about the world: a listing, a viral clip, a
  famous account posting, a takeover, a burn, a lock, a film, a partnership, a
  product shipping. "Traders bought it" and "momentum" are not causes.
- If the evidence does not contain a cause, return an empty string for that
  coin. That is the correct answer for most memecoins and is much better than
  a guess. Do not pad.
- Name the person or platform when the evidence names them.
- No hype, no adjectives, no price talk, no advice. One sentence, under 22
  words.

Return JSON only:
{"why": [{"id": "c01", "cause": "..."}, {"id": "c02", "cause": ""}]}"""


def _explain_facts(coins: list[Candidate]) -> list[dict[str, Any]]:
    """The evidence for each coin, and nothing else."""
    rows: list[dict[str, Any]] = []
    for index, candidate in enumerate(coins, start=1):
        evidence: list[str] = []
        for item in (candidate.news_evidence or [])[:3]:
            summary = " ".join(str(item.get("summary") or "").split())
            if summary:
                evidence.append(summary[:220])
        for post in (candidate.x_interactions or [])[:3]:
            text = " ".join(str(post.summary or "").split())
            if text:
                evidence.append(f"@{post.author_handle}: {text[:200]}")
        if not evidence:
            continue
        rows.append({
            "id": f"c{index:02d}",
            "mint": candidate.token.mint,
            "symbol": candidate.token.symbol,
            "chain": candidate.token.chain_id,
            "evidence": evidence,
        })
    return rows


async def explain_runs(coins: list[Candidate], settings: Settings, *, transport=None) -> int:
    """Attach a stated cause to every coin whose evidence contains one.

    The rest of the pipeline finds what a coin is. This asks the question the
    reader actually has, which is what happened today, and refuses to answer it
    when the evidence does not say.
    """
    if not bool(settings.get("newsletter", "explain_enabled", True)):
        return 0
    rows = _explain_facts(coins)
    if not rows:
        return 0

    provider = str(settings.get("newsletter", "provider", "openai")).strip().lower()
    order = [provider] + [p for p in ("openai", "cohere") if p != provider]
    payload_rows = {row["id"]: row for row in rows}

    for name in order:
        key = os.environ.get("COHERE_API_KEY" if name == "cohere" else "OPENAI_API_KEY", "").strip()
        if not key:
            continue
        if name == "cohere":
            url = COHERE_URL
            body = {
                "model": str(settings.get("newsletter", "cohere_model", "command-a-03-2025")),
                "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"},
                "max_tokens": 4000,
                "messages": [
                    {"role": "system", "content": EXPLAIN_PROMPT},
                    {"role": "user", "content": json.dumps({"coins": rows}, ensure_ascii=False)},
                ],
            }
        else:
            url = API_URL
            body = {
                "model": str(settings.get("newsletter", "model", "gpt-5.5")),
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": EXPLAIN_PROMPT},
                    {"role": "user", "content": json.dumps({"coins": rows}, ensure_ascii=False)},
                ],
            }
        try:
            async with httpx.AsyncClient(timeout=120, transport=transport) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=body,
                )
                response.raise_for_status()
                data = response.json()
            if name == "cohere":
                text = "".join(
                    part.get("text", "")
                    for part in (data.get("message", {}) or {}).get("content", [])
                    if part.get("type") == "text"
                )
            else:
                text = data["choices"][0]["message"]["content"]
            answer = json.loads(text[text.index("{"):text.rindex("}") + 1])
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            log.info("explain_failed provider=%s error=%s", name, type(exc).__name__)
            continue

        attached = 0
        by_mint = {c.token.mint: c for c in coins}
        for item in answer.get("why") or []:
            if not isinstance(item, dict):
                continue
            row = payload_rows.get(str(item.get("id", "")).strip().lower())
            cause = " ".join(str(item.get("cause") or "").split())
            if not row or len(cause) < 12:
                continue
            candidate = by_mint.get(str(row["mint"]))
            if candidate is None:
                continue
            candidate.provider_evidence.setdefault("why", {})["cause"] = cause[:200]
            attached += 1
        return attached
    return 0

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
        # An empty or unparseable answer is usually a payload the model could
        # not finish. Try once more with the quoted posts trimmed, which is
        # where nearly all the size is.
        lean = {
            "date": facts["date"],
            "coins": [
                {k: v for k, v in coin.items() if k not in ("xPosts", "news", "socials")}
                for coin in facts["coins"]
            ],
        }
        written = await _write_with(provider, lean, settings, transport=transport)
        if written:
            log.info("newsletter_recovered_on_lean_payload provider=%s", provider)
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
        str(coin["mint"]): any(coin.get(key) for key in story_keys)
        for coin in facts["coins"]
    }
    by_id = {coin["id"]: coin for coin in facts["coins"]}
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for coin in facts["coins"]:
        by_symbol.setdefault(str(coin["symbol"]).upper(), []).append(coin)
    clean: list[dict[str, Any]] = []
    seen_model_mints: set[str] = set()
    model_coin_items = 0
    accepted_model_coins = 0
    for section in sections:
        if not isinstance(section, dict):
            continue
        coins = []
        for item in section.get("coins") or []:
            if not isinstance(item, dict):
                continue
            model_coin_items += 1
            # Accept an id, or a ticker from a model that ignored the instruction.
            coin = by_id.get(str(item.get("id", "")).strip().lower())
            if coin is None:
                matches = by_symbol.get(str(item.get("symbol", "")).strip().upper(), [])
                # A reused ticker is ambiguous without the id/mint. Dropping
                # it is safer than attaching another token's market data.
                coin = matches[0] if len(matches) == 1 else None
            if coin:
                mint = str(coin["mint"])
                # A model can repeat a popular coin in two themes. The first
                # placement wins; the newsletter has one canonical coin list.
                if mint in seen_model_mints:
                    continue
                seen_model_mints.add(mint)
                accepted_model_coins += 1
                symbol = str(coin["symbol"])
                line = NO_DATA.sub("", str(item.get("line", ""))).strip(" ,;-")
                # The renderer prints the ticker; a model that starts the line
                # with it anyway produces "$CC CC hit". Remove it here, once.
                for prefix in (f"${symbol}", symbol):
                    if line.lower().startswith(prefix.lower()):
                        line = line[len(prefix):].lstrip(" :,-—–")
                        break
                coins.append({
                    "mint": str(coin["mint"]),
                    "symbol": symbol,
                    "line": _drop_leading_peak(_strip_ids(line)),
                })
        if not coins:
            continue
        bullets = [_strip_ids(str(b)) for b in (section.get("bullets") or []) if str(b).strip()]
        section_lines = [c["line"] for c in coins]
        bullets = [
            b for b in bullets
            if _carries_evidence(b)
            and not _is_restatement(b, section_lines)
            and not _is_profit_claim(b)
        ][:2]
        if not any(has_story.get(c["mint"]) for c in coins):
            bullets = []
        clean.append({
            "title": _strip_ids(str(section.get("title") or ""))[:80],
            "coins": coins,
            "bullets": bullets,
        })
    # Whatever the model did, every coin ships. A recap that silently drops
    # seventeen of twenty-one runners is worse than a plain list.
    placed = {c["mint"] for section in clean for c in section["coins"]}
    missing = [
        coin for coin in facts["coins"]
        if str(coin["mint"]) not in placed
    ]
    # The final question is the one bullet that may stand without evidence, so
    # it must survive both filters above.
    tail_question = ""
    for section in reversed(sections if isinstance(sections, list) else []):
        for bullet in reversed((section or {}).get("bullets") or []):
            text = _strip_ids(str(bullet))
            if text.endswith("?"):
                tail_question = text
                break
        if tail_question:
            break
    if tail_question and clean and tail_question not in clean[-1]["bullets"]:
        clean[-1]["bullets"] = [*clean[-1]["bullets"], tail_question][-2:]

    if missing and clean:
        clean.append({
            "title": "Also ran",
            "coins": [
                {
                    "mint": str(coin["mint"]),
                    "symbol": str(coin["symbol"]),
                    "line": f"hit {coin['peak']}" + (f", {coin['age']}" if coin.get("age") else ""),
                }
                for coin in missing
            ],
            "bullets": [],
        })
        log.info("newsletter_backfilled_coins count=%s", len(missing))

    if not clean:
        return None
    # Count only model-supplied entries that failed identity validation. The
    # backfilled "Also ran" section is added after validation, so including it
    # in this subtraction could produce nonsense such as count=-1.
    dropped = model_coin_items - accepted_model_coins
    if dropped:
        log.info("newsletter_dropped_unknown_coins count=%s", dropped)
    return {
        "intro": _strip_ids(str(recap.get("intro") or ""))[:240],
        "sections": clean,
        "writer": f"{provider}/{model}",
    }
