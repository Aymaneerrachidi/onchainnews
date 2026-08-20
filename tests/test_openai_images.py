from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from brief.config import Settings
from brief.openai_images import generate_runner_background
from brief.pulse import render_signal_image


PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def candidate():
    token = SimpleNamespace(
        mint="So11111111111111111111111111111111111111112",
        symbol="RUNNER",
        name="Runner",
        chain_id="solana",
        url="https://dexscreener.com/solana/runner",
        market_cap=2_500_000,
        liquidity_usd=160_000,
        volume_24h=14_000_000,
        price_change_24h=300,
        price_change_1h=18,
        raw={"info": {}},
    )
    safety = SimpleNamespace(holder_count=1200)
    return SimpleNamespace(
        token=token,
        safety=safety,
        run_multiple=8.2,
        kol_buyers=["Ansem"],
        risk_labels=["thin pool"],
        read="$RUNNER keeps passing on real volume.",
        dex_evidence=["Turnover 5.6x with 54% 6h buys."],
    )


def settings(tmp_path):
    return Settings(
        root=tmp_path,
        values={
            "pulse": {
                "image_dir": "images",
                "required_passes": 3,
                "openai_image_model": "gpt-image-2",
                "openai_image_size": "1024x1536",
                "openai_image_quality": "medium",
                "openai_image_format": "png",
            }
        },
    )


async def test_generate_runner_background_calls_openai(monkeypatch, tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"data": [{"b64_json": PNG_1X1}]})

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2")

    image = await generate_runner_background(
        candidate(),
        [{"takenAt": "2026-08-20T00:00:00+00:00"}] * 3,
        settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )

    assert image == base64.b64decode(PNG_1X1)
    assert seen["url"] == "https://api.openai.com/v1/images/generations"
    assert seen["auth"] == "Bearer test-key"
    assert '"model":"gpt-image-2"' in seen["body"]
    assert '"size":"1024x1536"' in seen["body"]
    assert "$RUNNER" in seen["body"]


async def test_render_signal_image_overlays_openai_background(tmp_path):
    path = await render_signal_image(
        candidate(),
        [{"takenAt": "2026-08-20T00:00:00+00:00"}] * 3,
        settings(tmp_path),
        datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        background=base64.b64decode(PNG_1X1),
    )

    assert path.exists()
    assert path.name.startswith("RUNNER-So111111-")
    assert path.suffix == ".png"
