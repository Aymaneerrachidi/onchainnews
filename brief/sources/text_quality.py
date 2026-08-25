"""Provider-neutral quality checks for social and narrative evidence."""
from __future__ import annotations

import re


HYPE = re.compile(
    r"(to the moon|to billions|to a billion|\blfg\b|speak it (in)?to existence"
    r"|never sell|hold the line|we are so back|wagmi|gm fam|\bfamily\b"
    r"|send it|\bape (in|into)\b|next \d+x|\d+x from here|easy \d+x"
    r"|last chance|dont fade|don't fade|do not fade|still early"
    r"|get in early|early gem|hidden gem|100x|1000x"
    r"|buy now|entry|target|\btp\d?\b|\bsl\b|call channel|join (my|our|the)"
    r"|t\.me/|discord\.gg|telegram\.me|link in bio|pinned)",
    re.I,
)
SUBSTANCE = re.compile(
    r"(because|which means|the reason|turns out|it works|mechanism|rewards?"
    r"|dividend|distribut|buyback|burn|holders?|liquidity|volume|listing"
    r"|listed|launch(ed)?|migrat|community|treasury|supply|unlock|team"
    r"|founder|partner|announce|airdrop|snapshot|exited|sold|bought|accumulat"
    r"|holding|yield|gold|rwa|claim|portal|flip|listed on|delist"
    r"|salary|dividend|revenue|renounce|locked|unlock|reflection"
    r"|culture|generation|millennial|zoomer|worldview|humou?r|joke"
    r"|thesis|because of|the point is|what makes|why it|the idea"
    r"|building|shipped|roadmap|integrat|ecosystem|adoption|dev is"
    r"|reference|meaning|meme|nostalgia|icon"
    r"|compare|versus|\bvs\b|underestimat|different|built|building)",
    re.I,
)
PROMO = re.compile(
    r"("
    r"\bmy (private |vip |paid )?(tg|telegram|group|channel|discord|community)\b"
    r"|\bprivate (tg|telegram|group|channel)\b|\bjoin (tg|telegram|discord|now|us|me)\b"
    r"|link (in|on) (my )?bio|\bdm me\b|\bpinned\b|\bt\.me/|discord\.gg"
    r"|notification (on|bell)|turn on notifications"
    r"|\bcalled (it|at|this)\b|\bmy call\b|\bcall of the (day|week)\b"
    r"|\bi called\b|\bwe called\b|\bentry at\b|\bmy entry\b"
    r"|\d+(\.\d+)?\s*x\s*(gain|profit|return|from (my|our) call)"
    r"|another \d+(\.\d+)?\s*x|\d+(\.\d+)?\s*x\s+on\s+\$"
    r"|[\d.,]+\s*[kKmM]?\s*(->|=>|→)\s*[\d.,]+\s*[kKmM]"
    r"|turn(ed|ing)?\s+\$?[\d.,]+\s*[kKmM]?\s+(in)?to\s+(over\s+|more than\s+|almost\s+)?\$?[\d.,]+"
    r"|\bsold none\b|\bstill sold none\b|\bare you following\b|\bfollow me\b"
    r"|\bretire(d|s)? (you|him|her|from)\b|\bmassive wins?\b|\binsane (gain|profit)"
    r"|\bprofits? secured\b|\bsecuring (massive )?wins?\b"
    r"|\bdont miss\b|\bdon't miss\b|\bnext one\b|\bwho[' ]?s next\b"
    r"|\bif you believe in me\b|\btrust me\b|\bfollow for more\b"
    r")",
    re.I,
)
WALLET_TRACKER = re.compile(
    r"(this (whale|wallet|guy|trader|address)\b"
    r"|\bwhale (just |has )?(bought|sold|added|dumped|exited|accumulated)"
    r"|(bought|sold|added|dumped|grabbed)\s+\$[\d.,]+\s*[kKmM]?"
    r"|\bjust came back with\b|\bcame back for\b"
    r"|\d+\s*buys?,?\s+\d+\s*sells?"
    r"|\bon the book\b|\bin one clip\b"
    r"|\bfresh wallet\b|\bnew wallet (bought|opened)"
    r"|\bsmart money (bought|sold|is buying|is selling)\b"
    r"|\bholder has (finally )?(completely )?(exited|sold)"
    r"|\bpaperhand|\bdiamond ?hand)",
    re.I,
)
CONTRACT_PASTE = re.compile(
    r"(\bca\b\s*[:=]|\bcontract\b\s*[:=]|0x[0-9a-fA-F]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}pump\b)",
    re.I,
)
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u2764]")
CASHTAG = re.compile(r"[$#][A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff]*")
URL = re.compile(r"https?://\S+")
PRICE_TICK = re.compile(
    r"(\b(OI|oi)\s*\d+\s*(min|m|h)\b|\b\d+s\b.{0,12}(up|down)\s*\d"
    r"|(up|down)\s*\d+(\.\d+)?%\s*\$?[\d.,]+[KMB]?\s*(drop|rise|move)"
    r"|liquidation|funding rate|open interest)",
    re.I,
)
CAUSAL_POST = re.compile(
    r"(listed on|listing|now live|announce|launch(ed|ing)?|partnership"
    r"|collab|acquired|integrat|burn(ed|t)?|locked|airdrop|snapshot"
    r"|takeover|rebrand|migrat|upgrade|shipped|went viral|posted about"
    r"|picked up by|featured|added to|support(s|ed)? by)",
    re.I,
)


def post_substance(text: str) -> int:
    body = URL.sub(" ", text or "")
    body = CASHTAG.sub(" ", body)
    body = re.sub(r"@\w+", " ", body)
    body = re.sub(r"[^A-Za-z\u4e00-\u9fff ]+", " ", body)
    latin = [word for word in body.split() if len(word) > 2 and word.isascii()]
    cjk = len(re.findall(r"[\u4e00-\u9fff]", body)) // 2
    return len(latin) + cjk


def post_quality(
    text: str,
    followers: int,
    likes: int,
    reposts: int,
    verified: bool,
    min_reach: int = 3_000,
) -> int:
    words = post_substance(text)
    if words < 6:
        return 0
    if followers < min_reach and (likes + reposts * 2) < 80:
        return 0
    if HYPE.search(text or "") or PROMO.search(text or ""):
        return 0
    if len(EMOJI.findall(text or "")) >= 4:
        return 0
    if CONTRACT_PASTE.search(text or ""):
        return 0
    if WALLET_TRACKER.search(text or ""):
        return 0
    if len(set(CASHTAG.findall(text or ""))) > 4:
        return 0

    score = min(30, words)
    if CAUSAL_POST.search(text or ""):
        score += 40
    elif SUBSTANCE.search(text or ""):
        score += 25
    if verified:
        score += 5
    score += min(20, (likes + reposts * 2) // 10)
    score += min(15, followers // 5000)
    return score
