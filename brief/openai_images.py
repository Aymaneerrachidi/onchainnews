from __future__ import annotations

import base64
import os
from typing import Any

import httpx

from brief.config import Settings
from brief.models import Candidate
from brief.render.formatting import money, pct


class OpenAIImageError(RuntimeError):
    """Raised when the OpenAI image API cannot produce a usable image."""


def configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def runner_image_prompt(candidate: Candidate, passes: list[dict[str, Any]], settings: Settings) -> str:
    token = candidate.token
    chain = token.chain_id.upper()
    risk_bits = "; ".join(candidate.risk_labels[:3]) if candidate.risk_labels else "no major displayed flags"
    kol_bits = ", ".join(candidate.kol_buyers[:6]) if candidate.kol_buyers else "no tracked wallet names available"

    return f"""
Create a premium vertical crypto runner signal poster BACKGROUND for an automated on-chain market desk.

Style:
- energetic FOMO poster, glossy purple-blue studio lighting, memecoin launch energy
- soft 3D floating rounded cubes and a large 3D coin, inspired by a premium crypto ad
- leave the upper-left and center-left mostly clean for a huge text headline that will be added later
- place any 3D app/trading-card object toward the lower-right only
- include visual space for a circular coin logo near the lower-left
- avoid busy interface panels behind the headline area
- Solana accent energy, not generic Bitcoin branding
- professional enough to post on X

Important constraints:
- Do not render readable metric text, fake numbers, fake wallet addresses, fake tickers, or fake logos
- If text-like marks appear, make them abstract and illegible
- Do not include financial advice language
- No watermark

Token context for the mood only:
- token ${token.symbol} / {token.name}
- chain {chain}
- runner multiple {candidate.run_multiple:.1f}x
- market cap {money(token.market_cap)}
- volume 24h {money(token.volume_24h)}
- liquidity {money(token.liquidity_usd)}
- 1h change {pct(token.price_change_1h, 0)}
- sustained passes {len(passes)} of {settings.get("pulse", "required_passes", 3)}
- tracked wallets: {kol_bits}
- flags: {risk_bits}
- read: {candidate.read}
""".strip()


async def generate_runner_background(
    candidate: Candidate,
    passes: list[dict[str, Any]],
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise OpenAIImageError("OPENAI_API_KEY is unset")

    model = os.environ.get("OPENAI_IMAGE_MODEL", "").strip() or str(
        settings.get("pulse", "openai_image_model", "gpt-image-2")
    )
    body: dict[str, Any] = {
        "model": model,
        "prompt": runner_image_prompt(candidate, passes, settings),
        "size": str(settings.get("pulse", "openai_image_size", "1024x1536")),
        "quality": str(settings.get("pulse", "openai_image_quality", "medium")),
        "output_format": str(settings.get("pulse", "openai_image_format", "png")),
    }
    timeout = float(settings.get("pulse", "openai_image_timeout_seconds", 120.0))
    url = str(settings.get("pulse", "openai_image_url", "https://api.openai.com/v1/images/generations"))

    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )

    if response.status_code >= 400:
        raise OpenAIImageError(f"OpenAI image API returned HTTP {response.status_code}: {response.text[:300]}")

    try:
        payload = response.json()
        encoded = payload["data"][0]["b64_json"]
        image = base64.b64decode(encoded)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise OpenAIImageError("OpenAI image API response did not include b64_json image data") from exc

    if not image:
        raise OpenAIImageError("OpenAI image API returned an empty image")
    return image
