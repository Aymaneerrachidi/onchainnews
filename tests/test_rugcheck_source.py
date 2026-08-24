from __future__ import annotations

import pytest

from brief.sources.http import SourceError
from brief.sources.rugcheck import RugCheckSource


@pytest.mark.asyncio
async def test_rugcheck_opens_run_circuit_after_first_rate_limit():
    class LimitedHttp:
        def __init__(self):
            self.calls = 0

        async def get_json(self, *_args, **_kwargs):
            self.calls += 1
            raise SourceError("rugcheck request failed: HTTP 429")

    http = LimitedHttp()
    source = RugCheckSource(
        http,
        "https://rugcheck.test/v1",
        60,
        requests_per_minute=60_000,
    )

    with pytest.raises(SourceError, match="HTTP 429"):
        await source.report("FIRST")
    with pytest.raises(SourceError, match="circuit is open"):
        await source.report("SECOND")

    assert http.calls == 1
