from __future__ import annotations

import json

import httpx
import pytest

from brief.delivery import send_telegram
from brief.render.telegram_interactive import filter_rows, render_snapshot_message


def row(symbol: str, mint: str, chain: str, peak: float) -> dict:
    return {
        "symbol": symbol,
        "mint": mint,
        "chain": chain,
        "marketCap": peak * 0.8,
        "observedPeakMarketCap": peak,
        "liquidity": 100_000,
        "holders": 1234,
        "top10Pct": 12.5,
        "lore": f"{symbol} community lore.",
        "xInteractions": [],
    }


def test_interactive_message_has_fomo_links_filters_and_pagination() -> None:
    snapshot = {
        "generatedAt": "2026-08-26T04:45:00+00:00",
        "runnerUniverse": [
            *[row(f"SOL{i}", f"mint{i}", "solana", 300_000 + i) for i in range(9)],
            row("BNB", "0xabc", "bsc", 2_000_000),
        ],
    }
    payload = render_snapshot_message(snapshot)
    assert payload["parse_mode"] == "HTML"
    assert "FOMO ONCHAIN · DAILY RUNNERS" in payload["text"]
    assert "https://fomo.family/tokens/solana/mint8" in payload["text"]
    assert "page 1/2" in payload["text"]
    callbacks = [button.get("callback_data", "") for line in payload["reply_markup"]["inline_keyboard"] for button in line]
    assert "tg|filter|solana|all|20260826|0" in callbacks
    assert "tg|refresh|all|all|20260826|0" in callbacks
    assert all(len(value.encode()) <= 64 for value in callbacks)
    assert [item["symbol"] for item in filter_rows(snapshot, "bsc", "1m-10m")] == ["BNB"]


def test_all_caps_includes_validated_runner_below_250k() -> None:
    snapshot = {"runnerUniverse": [row("LOW", "low-mint", "solana", 218_000)]}
    assert [item["symbol"] for item in filter_rows(snapshot)] == ["LOW"]


@pytest.mark.asyncio
async def test_delivery_forwards_html_and_inline_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-id")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    message = {"text": "<b>Daily</b>", "parse_mode": "HTML", "reply_markup": {"inline_keyboard": []}}
    await send_telegram([message], transport=httpx.MockTransport(handler))
    assert captured["chat_id"] == "chat-id"
    assert captured["parse_mode"] == "HTML"
    assert captured["reply_markup"] == {"inline_keyboard": []}
