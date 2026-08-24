from __future__ import annotations

import time

import pytest

from brief.sources.birdeye import BirdeyeSource


class RecordingHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[float, str]] = []

    async def get_json(self, url: str, **kwargs):
        chain = str(kwargs["headers"]["x-chain"])
        self.calls.append((time.monotonic(), chain))
        address = "So11111111111111111111111111111111111111111" if chain == "solana" else "0xabc"
        return {
            "data": {
                "items": [
                    {
                        "address": address,
                        "liquidity": 50_000,
                        "market_cap": 1_000_000,
                    }
                ]
            }
        }


@pytest.mark.asyncio
async def test_requests_remain_spaced_when_switching_chains() -> None:
    http = RecordingHttp()
    source = BirdeyeSource(
        http,
        "https://birdeye.test",
        "test-key",
        60,
        page_size=1,
        request_interval=0.02,
    )

    await source.top_by_volume(1, 20_000, 250_000)
    source.chain = "base"
    await source.top_by_volume(1, 20_000, 250_000)

    assert [chain for _, chain in http.calls] == ["solana", "base"]
    assert http.calls[1][0] - http.calls[0][0] >= 0.018
