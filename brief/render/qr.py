"""QR codes for the stream overlay.

A viewer watching a stream cannot copy a contract address off the screen, so
every highlighted coin carries a QR that opens the coin in a trading app. The
matrix is generated here with a proven encoder rather than in the browser: a
malformed code is invisible until it is already on the broadcast.

The matrix ships as one string of "0"/"1" per row so the overlay only has to
draw squares, with no encoder of its own.
"""
from __future__ import annotations

import logging

from brief.links import fomo_chain_slug

try:
    import segno
except ImportError:  # pragma: no cover - exercised by the guard below
    # The QR is a convenience on one overlay. The daily report is the product,
    # so a dependency problem degrades the code away rather than losing the run.
    segno = None


log = logging.getLogger("brief.qr")

# Kept small on purpose: error correction "M" tolerates the compression and
# scaling a stream applies, while staying coarse enough to scan from a couch.
ERROR_CORRECTION = "m"


def trade_url(template: str, mint: str, symbol: str, chain: str = "") -> str:
    """Fill the configured deep link for one coin.

    The template is configuration because the destination is the client's
    referral link, and that changes without any code changing.
    """
    if not template:
        return ""
    return (
        template.replace("{mint}", mint)
        .replace("{symbol}", symbol.lstrip("$"))
        .replace("{chain}", fomo_chain_slug(chain))
        .replace("{MINT}", mint)
    )


def qr_matrix(payload: str) -> list[str]:
    """Rows of "0"/"1" for the given text, or [] if it cannot be encoded."""
    if not payload or segno is None:
        if segno is None:
            log.warning("segno_unavailable qr_codes_disabled")
        return []
    try:
        code = segno.make(payload, error=ERROR_CORRECTION, micro=False)
        return ["".join("1" if module else "0" for module in row) for row in code.matrix]
    except Exception as exc:  # pragma: no cover - encoder is deterministic
        log.warning("qr_encode_failed payload=%s error=%s", payload[:24], exc)
        return []
