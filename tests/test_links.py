from brief.links import fomo_token_url
from brief.render.qr import trade_url


def test_fomo_token_url_uses_chain_route():
    mint = "Ab1sTFNv2tV5DX1XpriwNehXgiJhdq2RQ5LtD5BXpump"
    assert fomo_token_url("solana", mint) == (
        "https://fomo.family/tokens/solana/"
        "Ab1sTFNv2tV5DX1XpriwNehXgiJhdq2RQ5LtD5BXpump"
    )
    assert fomo_token_url("bnb", "0xabc") == "https://fomo.family/tokens/bnb/0xabc"
    assert fomo_token_url("bsc", "0xabc") == "https://fomo.family/tokens/bnb/0xabc"


def test_overlay_trade_url_supports_chain_placeholder():
    template = "https://fomo.family/tokens/{chain}/{mint}"
    assert trade_url(template, "MINT", "TICKER", "base") == (
        "https://fomo.family/tokens/base/MINT"
    )
