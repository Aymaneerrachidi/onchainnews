"""Editorial style rules shared by website and delivery pipelines."""

from __future__ import annotations

import re


_IDENTITY_PREAMBLES = (
    "Exact-contract research identifies ",
    "Exact-contract Codex search identified ",
    "Deep exact-contract search identifies ",
)

_ROBOTIC_OPENERS = (
    r"^[^.!?]{1,40}'s move came with a linked post(?: from [^:]+)?:\s*",
    r"^[^.!?]{1,40} had an exact linked social source during the move[,;:]?\s*",
)

_NON_ENGLISH_SCRIPT = re.compile(
    r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff\u0600-\u06ff]"
)

_NON_LORE_PHRASES = (
    "lfg",
    "let's fucking go",
    "lets fucking go",
    "called this early",
    "called this at",
    "warned you",
    "earlier call",
    "from my call",
    "called at",
    "entry at",
    "insta dumped",
    "just aped",
    "buy now",
    "sell now",
    "send it",
    "easy x",
    "next leg",
    "still bullish",
    "looks bullish",
    "bullish on",
    "target is",
    "new ath",
    "ath soon",
    "bullish catalyst",
    "trading chatter",
    "social context cautionary",
    "no separate project story",
    "underlying story remains",
    "no credible public story",
    "no reliable project story",
    "no reliable origin story",
    "no independently verified",
    "exact-contract research",
    "contract-matched x trail",
    "contract-matched attention",
    "contract-matched post",
    "exact contract",
    "indexed publicly",
    "indexed social",
    "public posts",
    "social evidence",
    "social framing",
    "market pages",
    "primary documentation",
    "linked x trail",
    "linked social footprint",
    "available web trail",
    "visible trader attention",
    "does not prove",
    "does not clearly certify",
    "could not be verified",
    "could not verify",
    "no documented product",
    "qualified on trading strength",
)


def contains_untranslated_text(value: object) -> bool:
    """Reject raw non-English source copy before it reaches a recap."""
    return bool(_NON_ENGLISH_SCRIPT.search(str(value or "")))


def is_real_lore(value: object) -> bool:
    """Accept an origin, character, product, creator, or real catalyst—not market chatter."""
    text = str(value or "").strip()
    lowered = text.casefold()
    if not text or contains_untranslated_text(text):
        return False
    # Public lore must read as finished editorial copy. A dangling clause or
    # scraped caption without sentence-ending punctuation stays internal.
    if not re.search(r"[.!?](?:[\"'”’])?$", text):
        return False
    if any(phrase in lowered for phrase in _NON_LORE_PHRASES):
        return False
    # Reject performance-flex/call copy such as "20k -> 400k", "4x from my
    # call" and "at 300k MC". Those posts may help discovery, but never explain
    # what a coin is or why its story exists.
    if re.search(r"\$?\d+(?:\.\d+)?\s*[kmb]?\s*(?:-{1,2}>|→|to)\s*\$?\d+(?:\.\d+)?\s*[kmb]?\b", lowered):
        return False
    if re.search(r"\b\d+(?:\.\d+)?x\b|\b(?:mc|mcap|market cap)\s*(?:at|of|is)?\s*\$?\d", lowered):
        return False
    if re.search(r"(?:https?://|www\.|(?<!\w)@[A-Za-z0-9_]{1,30}\b)", text, re.I):
        return False
    # Raw contracts and ticker-only trading commentary are evidence, not lore.
    if re.search(r"\b0x[a-fA-F0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{40,64}\b", text):
        return False
    return True


def humanize_lore(value: object) -> str:
    """Keep research mechanics internal and present lore as editorial prose."""
    text = str(value or "").strip()
    # Evidence links and handles belong in source metadata, not the story.
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"(?:https?://|www\.)\S+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\w)@[A-Za-z0-9_]{1,30}\b", "", text)
    for pattern in _ROBOTIC_OPENERS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    for preamble in _IDENTITY_PREAMBLES:
        text = text.replace(preamble, "")
    text = text.replace("X: ", "").replace(" Lore: ", " ")
    text = text.replace("is associated in indexed social research with", "is linked by public posts to")
    text = text.replace("is described in exact-contract social evidence as", "is presented as")
    text = text.replace("exact-contract exchange announcements corroborate the identity", "exchange listings support the identity")
    text = text.replace("exact-contract social evidence", "public posts")
    text = text.replace("exact-contract market pages", "market pages")
    text = re.sub(r"\s+\d+\s+(?:likes?|replies?|views?)\b.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bToken\s+[1-9A-HJ-NP-Za-km-z]{32,64}\b.*$", "", text)
    text = text.replace("â€¦", "…")
    text = text.replace(", but found no ", "; no ")
    text = text.replace(",' but found no ", ". No ")
    text = re.sub(
        r"(^|\. )this ([A-Za-z0-9$]+) token as ",
        lambda match: f"{match.group(1)}{match.group(2)} is presented as ",
        text,
    )
    text = re.sub(
        r"(^|\. )([A-Za-z0-9$]+) as ([^;]+); no ",
        lambda match: f"{match.group(1)}{match.group(2)} is presented as {match.group(3)}; no ",
        text,
    )
    text = re.sub(
        r"Deep exact-contract web and X search found no reliable (.+?) for ([^.]+)\.",
        lambda match: f"{match.group(2)} has no independently verified {match.group(1)}.",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"Exact-contract research found no reliable (.+?)\.",
        lambda match: f"No independently verified {match.group(1)} was found.",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip(" \t\r\n-–—·,;:")
