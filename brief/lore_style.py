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


def humanize_lore(value: object) -> str:
    """Keep research mechanics internal and present lore as editorial prose."""
    text = str(value or "").strip()
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
    return text
