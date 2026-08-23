"""Canonical outbound links shared by email, Discord and overlays."""
from __future__ import annotations


CHAIN_SLUGS = {
    "sol": "solana",
    "solana": "solana",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "bnb": "bnb",
    "bnb chain": "bnb",
    "bnb_chain": "bnb",
    "bsc": "bnb",
    "base": "base",
}


def fomo_chain_slug(chain_id: str) -> str:
    raw = str(chain_id or "").strip().lower()
    return CHAIN_SLUGS.get(raw, raw)


def fomo_token_url(chain_id: str, mint: str) -> str:
    """The public Fomo Family token route for one chain and contract."""
    return f"https://fomo.family/tokens/{fomo_chain_slug(chain_id)}/{mint}"
