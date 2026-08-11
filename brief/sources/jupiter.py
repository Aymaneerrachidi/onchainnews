from __future__ import annotations

from brief.models import number
from brief.sources.http import CachedHttpClient


SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterSource:
    def __init__(self, http: CachedHttpClient, quote_url: str, price_url: str) -> None:
        self.http = http
        self.quote_url = quote_url
        self.price_url = price_url

    async def quote(self, input_mint: str, amount: int, slippage_bps: int = 500) -> dict:
        payload = await self.http.get_json(
            self.quote_url,
            params={
                "inputMint": input_mint,
                "outputMint": SOL_MINT,
                "amount": str(max(1, amount)),
                "slippageBps": slippage_bps,
                "restrictIntermediateTokens": "true",
            },
            family="jupiter",
            limit=60,
            ttl=30,
        )
        return payload if isinstance(payload, dict) else {}

    async def sellable_sol_under_impact(
        self,
        mint: str,
        supply_raw: float,
        *,
        max_impact_pct: float = 5.0,
        steps: int = 14,
    ) -> float | None:
        high = min(int(supply_raw), 18_446_744_073_709_551_615)
        if high <= 0:
            return None
        low = 1
        best_out = 0
        impact_limit = max_impact_pct / 100
        for _ in range(steps):
            if low > high:
                break
            amount = (low + high) // 2
            try:
                quote = await self.quote(mint, amount, int(max_impact_pct * 100))
                impact = number(quote.get("priceImpactPct"), default=1.0)
                out_amount = int(quote.get("outAmount") or 0)
                if out_amount > 0 and impact <= impact_limit:
                    best_out = max(best_out, out_amount)
                    low = amount + 1
                else:
                    high = amount - 1
            except Exception:
                high = amount - 1
        return best_out / 1_000_000_000 if best_out else None

    async def sol_price_usd(self) -> float | None:
        payload = await self.http.get_json(
            self.price_url,
            params={"ids": SOL_MINT},
            family="jupiter",
            limit=60,
            ttl=60,
        )
        item = (payload or {}).get(SOL_MINT) if isinstance(payload, dict) else None
        if isinstance(item, dict):
            return number(item.get("usdPrice") or item.get("price"), default=0) or None
        return None
